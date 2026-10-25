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

import hashlib
import json
import os
import subprocess
import sys

from . import sandbox

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_harness.py")


def evaluator_version(eval_dir):
    h = hashlib.sha256()
    for p in (os.path.abspath(__file__), HARNESS, os.path.join(eval_dir, "datasets", "dedup", "MANIFEST.json")):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def split_version(eval_dir):
    with open(os.path.join(eval_dir, "datasets", "dedup", "MANIFEST.json"), encoding="utf-8") as f:
        return json.load(f)["version"]


def load_split(eval_dir, split):
    rows = []
    with open(os.path.join(eval_dir, "datasets", "dedup", split + ".jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
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
    os.makedirs(scratch, exist_ok=True)
    inp, out = os.path.join(scratch, "items.json"), os.path.join(scratch, "decisions.json")
    with open(inp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
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
