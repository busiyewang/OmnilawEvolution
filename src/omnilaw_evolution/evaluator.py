"""Independent evaluator. Reads labelled splits from the stable eval directory,
gives candidate code only the unlabeled items (inside the sandbox), and scores
the decisions itself.

Metrics (per split):
  precision / recall / F1 over "this item is a duplicate of an earlier kept
  item from the same gold cluster"; false deletions = removed items that were
  the first of their cluster (information lost); critical_lost = removed items
  labelled critical; ms_per_item from the timed dedup call.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys

from . import sandbox

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_harness.py")


def evaluator_version(eval_dir):
    h = hashlib.sha256()
    for p in (os.path.abspath(__file__), HARNESS, os.path.join(os.path.dirname(__file__), "policy.py"),
              os.path.join(eval_dir, "datasets", "dedup", "MANIFEST.json")):
        with open(p, "rb") as f:
            h.update(f.read())
    for split in ("dev", "holdout", "batch2"):
        h.update(_split_bytes(eval_dir, split))
    return h.hexdigest()[:16]


def split_version(eval_dir):
    with open(os.path.join(eval_dir, "datasets", "dedup", "MANIFEST.json"), encoding="utf-8") as f:
        return json.load(f)["version"]


def _split_bytes(eval_dir, split):
    with open(os.path.join(eval_dir, "datasets", "dedup", "MANIFEST.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    if split not in manifest["splits"] or split not in ("dev", "holdout", "batch2"):
        raise ValueError("unknown evaluation split: %s" % split)
    with open(os.path.join(eval_dir, "datasets", "dedup", split + ".jsonl"), "rb") as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest() != manifest["splits"][split]["sha256"]:
        raise ValueError("evaluation data hash mismatch for %s; rebuild and version the dataset" % split)
    return data


def load_split(eval_dir, split):
    rows = [json.loads(line) for line in _split_bytes(eval_dir, split).decode("utf-8").splitlines() if line.strip()]
    return rows


def protected_reads(cfg):
    """Paths candidate code must not read: labelled data and the state DB."""
    db = cfg.db_path
    return [cfg["eval_dir"], db, db + "-wal", db + "-shm"]


def contract_failures(items, decisions):
    fails = []
    ids = [i["id"] for i in items]
    if not isinstance(decisions, list) or [d.get("id") for d in decisions] != ids:
        return ["decisions do not cover every item exactly once in input order"]
    kept = set()
    for d in decisions:
        dup = d.get("duplicate_of")
        if dup is None:
            kept.add(d["id"])
        elif dup not in kept:
            fails.append("%s points to %s which is not an earlier kept item" % (d["id"], dup))
    return fails


def score(rows, decisions, seconds):
    label = {r["id"]: r["label"] for r in rows}
    first_of = {}
    gold_dup = {}
    for r in rows:
        c = r["label"]["cluster"]
        gold_dup[r["id"]] = c in first_of
        first_of.setdefault(c, r["id"])
    tp = removed = false_del = wrong_link = 0
    per_item, critical_lost = [], []
    for d in decisions:
        iid, dup = d["id"], d.get("duplicate_of")
        lab = label[iid]
        outcome = "ok"
        if dup is not None:
            removed += 1
            if gold_dup[iid] and label[dup]["cluster"] == lab["cluster"]:
                tp += 1
            elif gold_dup[iid]:
                wrong_link += 1
                outcome = "wrong-link"
            else:
                false_del += 1
                outcome = "false-deletion"
            if lab["critical"] and not gold_dup[iid]:
                critical_lost.append(iid)
        elif gold_dup[iid]:
            outcome = "missed-duplicate"
        per_item.append({"id": iid, "case": lab["case"], "cluster": lab["cluster"], "gold_duplicate": gold_dup[iid],
                         "duplicate_of": dup, "reason": d.get("reason", ""), "outcome": outcome})
    gold = sum(gold_dup.values())
    uniques = len(rows) - gold
    precision = tp / removed if removed else 1.0
    recall = tp / gold if gold else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    metrics = {"n": len(rows), "gold_duplicates": gold, "removed": removed, "true_positive": tp,
               "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
               "false_deletions": false_del, "false_deletion_rate": round(false_del / uniques, 4) if uniques else 0.0,
               "wrong_links": wrong_link, "critical_lost": critical_lost,
               "ms_per_item": round(1000.0 * seconds / max(1, len(rows)), 4)}
    return metrics, per_item


def run_on_split(cfg, code_root, split, scratch, stop_check=None, timeout=None):
    """Evaluate the dedup module under ``code_root`` on ``split``. Returns dict
    with metrics, per_item, regression info, isolation level and seconds."""
    rows = load_split(cfg["eval_dir"], split)
    items = [{k: v for k, v in r.items() if k != "label"} for r in rows]
    # PAWS-X labels describe independent pairs, not arbitrary relations between
    # pairs. Keep the fictional daily feed as one batch and each source pair as
    # its own batch. No group identifiers or provenance reach candidate code.
    groups = []
    previous = object()
    for row, item in zip(rows, items):
        group = row["label"].get("group", "feed")
        if group != previous:
            groups.append([])
        groups[-1].append(item)
        previous = group
    os.makedirs(scratch, exist_ok=True)
    inp, out = os.path.join(scratch, "items.json"), os.path.join(scratch, "decisions.json")
    with open(inp, "w", encoding="utf-8") as f:
        json.dump({"groups": groups}, f, ensure_ascii=False)
    res = sandbox.run([sys.executable, "-I", "-B", HARNESS, os.path.join(code_root, "src"), inp, out],
                      scratch=scratch, deny_read=protected_reads(cfg), network=False,
                      timeout=timeout or cfg["budget"]["max_process_seconds"], mode=cfg["sandbox"]["mode"],
                      stop_check=stop_check)
    result = {"split": split, "isolation": res["isolation"], "process_seconds": res["seconds"],
              "returncode": res["returncode"], "stderr_tail": res["stderr"][-1500:]}
    if res["returncode"] != 0 or not os.path.exists(out):
        result.update(ok=False, metrics=None, per_item=[],
                      regression={"passed": False, "failures": ["candidate crashed: " + res["stderr"][-300:]]})
        return result
    with open(out, encoding="utf-8") as f:
        payload = json.load(f)
    fails = contract_failures(items, payload["decisions"])
    if not fails:
        offset = 0
        for group in groups:
            fails.extend(contract_failures(group, payload["decisions"][offset:offset + len(group)]))
            offset += len(group)
    if not payload.get("deterministic"):
        fails.append("non-deterministic output")
    if not payload.get("empty_ok"):
        fails.append("empty input not handled")
    if fails:
        result.update(ok=False, metrics=None, per_item=[], regression={"passed": False, "failures": fails})
        return result
    metrics, per_item = score(rows, payload["decisions"], payload["seconds"])
    result.update(ok=True, metrics=metrics, per_item=per_item, regression={"passed": True, "failures": []})
    return result


def module_hash(code_root):
    """Hash of everything candidate code can import from the package
    (the collector directory). Two code roots with equal hashes behave the same
    under the harness."""
    base = os.path.join(code_root, "src", "omnilaw_evolution", "collector")
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            h.update(os.path.relpath(full, base).encode() + b"\0")
            with open(full, "rb") as f:
                h.update(_behaviour_bytes(name, f.read()))
    return h.hexdigest()


def _behaviour_bytes(name, data):
    """Python sources are hashed by their AST without docstrings, so comments
    and provenance headers (which name the run) do not defeat the cache."""
    if not name.endswith(".py"):
        return data
    try:
        tree = ast.parse(data)
    except SyntaxError:
        return data
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree).encode()


def split_sha(eval_dir, split):
    return hashlib.sha256(_split_bytes(eval_dir, split)).hexdigest()


def cached_run_on_split(db, cfg, code_root, split, scratch, stop_check=None, timeout=None):
    """``run_on_split`` with an experiment cache. Only deterministic outcomes
    (scored results and contract failures) are cached; crashes and timeouts
    are always re-run. A cache hit is not a new exposure of the split."""
    mh, ev, ss = module_hash(code_root), evaluator_version(cfg["eval_dir"]), split_sha(cfg["eval_dir"], split)
    key = hashlib.sha256(("%s|%s|%s|%s" % (mh, ev, split, ss)).encode()).hexdigest()
    row = db.one("SELECT result FROM eval_cache WHERE key=?", (key,))
    if row:
        db.execute("UPDATE eval_cache SET hits=hits+1 WHERE key=?", (key,))
        res = json.loads(row["result"])
        res.update(cached=True, process_seconds=0.0, cache_key=key[:16])
        return res
    res = run_on_split(cfg, code_root, split, scratch, stop_check=stop_check, timeout=timeout)
    crashed = any(f.startswith("candidate crashed") for f in res["regression"]["failures"])
    if not crashed:
        from .db import now
        db.execute("INSERT OR REPLACE INTO eval_cache(key, module_hash, evaluator_version, split, split_sha, "
                   "result, created_at) VALUES (?,?,?,?,?,?,?)",
                   (key, mh, ev, split, ss, json.dumps(res, ensure_ascii=False), now()))
    res.update(cached=False, cache_key=key[:16])
    return res


def run_unit_tests(cfg, code_root, scratch, stop_check=None, timeout=None):
    """Run the protected collector contract tests against the candidate code."""
    res = sandbox.run([sys.executable, "-B", "-m", "unittest", "-q", "tests.test_collector_contract"],
                      scratch=scratch, cwd=code_root, deny_read=protected_reads(cfg),
                      network=False, timeout=timeout or cfg["budget"]["max_process_seconds"],
                      mode=cfg["sandbox"]["mode"], stop_check=stop_check, env_extra={"PYTHONPATH": os.path.join(code_root, "src")})
    return {"passed": res["returncode"] == 0, "output_tail": (res["stdout"] + res["stderr"])[-1500:],
            "isolation": res["isolation"]}


def git_sha(path):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True,
                          check=True).stdout.strip()
