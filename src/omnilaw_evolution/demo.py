"""Phase-1 walkthrough on a fresh demo instance.

Builds a git repository from the current source tree (so the developer's own
checkout is never modified), then runs: init -> ingest -> propose -> run
(good / contrast / red-team) -> apply -> collect -> later batch -> stop,
timeout, budget + resume -> rollback. Writes a Markdown summary.

Everything here uses the offline mock model and template executor unless
``live=True`` adds a live fetch check of the three real sources.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from . import runner, sources, versions
from .budget import Budget
from .config import load_config, write_config
from .db import DB, loads
from .llm import make_llm
from .planner import propose
from .report import run_report

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP = {".git", ".omnilaw", "__pycache__", ".demo", "reports", "omnilaw.json", ".pytest_cache"}
GOAL = "减少技术日报中的重复材料，同时保留带有新信息的条目 (reduce duplicates, keep items with new information)"
LIVE_URLS = [
    "https://en.wikipedia.org/wiki/URI_normalization",
    "https://github.com/ekzhu/datasketch/blob/c7eaaeea3f39909f906a9cf8ad4d21a878cb4e07/datasketch/minhash.py",
    "https://arxiv.org/abs/2107.06499v2",
]


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_instance(dest):
    if os.path.exists(dest):
        shutil.rmtree(dest)

    def ignore(d, names):
        return [n for n in names if n in SKIP or n.endswith(".pyc") or n.startswith(".demo")
                or os.path.realpath(os.path.join(d, n)) == os.path.realpath(dest)]
    shutil.copytree(REPO_ROOT, dest, ignore=ignore)
    versions.git(dest, "init", "-q", "-b", "main")
    versions.git(dest, "add", "-A")
    versions.git(dest, "commit", "-q", "-m", "demo instance baseline")
    return versions.head_sha(dest)


def run_demo(workdir, report_out=None, live=True):
    workdir = os.path.realpath(workdir)
    os.makedirs(workdir, exist_ok=True)
    out = []
    log = out.append
    t0 = time.time()
    instance = os.path.join(workdir, "instance")
    base_sha = build_instance(instance)
    cfg_path = os.path.join(workdir, "omnilaw.json")
    write_config(cfg_path, {"project_root": "instance", "state_dir": "state", "experiments_dir": "experiments"})
    cfg_hash_before = _sha256(cfg_path)
    cfg = load_config(cfg_path)
    db = DB(cfg.db_path)
    py = sys.executable

    def cli(*args, check=True):
        env = dict(os.environ, PYTHONPATH=os.path.join(REPO_ROOT, "src"))
        return subprocess.run([py, "-m", "omnilaw_evolution", "--config", cfg_path] + list(args), env=env,
                              capture_output=True, text=True, check=check)

    log("# Omnilaw Evolution — phase-1 demo report\n")
    log("Generated %s by `omnilaw-evolution demo`. Model adapter: **offline mock** (no model API was called); "
        "coding executor: **template** (code is rendered from templates in `templates.py`, not written by a "
        "model). Dataset: fictional, labelled, mechanism-demo scale — results do not show general capability.\n"
        % time.strftime("%Y-%m-%d %H:%M:%S %z"))
    log("- Python %s on %s; sandbox mode: `%s`" % (sys.version.split()[0], sys.platform,
                                                   __import__("omnilaw_evolution.sandbox", fromlist=["x"]).detect_mode(cfg["sandbox"]["mode"])))
    log("- Demo instance: fresh git repo built from the source tree, baseline commit `%s`" % base_sha)
    log("- Paths below are relative to the demo workdir; the exported copy puts run reports and patches in "
        "`runs/`, digests in `digests/`, source and knowledge records in `sources.json` / `knowledge.json`.\n")

    # 1. init + baseline digest
    rel1 = versions.init_releases(db, cfg)
    daily = os.path.join(instance, "fixtures", "batches", "daily_2026-10-24.jsonl")
    d_before = os.path.join(workdir, "digests", "digest-release1.md")
    cli("collect", "--input", daily, "--out", d_before)
    log("## 1. Baseline release\n")
    log("Release #%d = `%s`, health: %s. Daily digest via the active release: `%s`.\n"
        % (rel1["id"], rel1["sha"][:12], loads(rel1["health"])["healthy"], os.path.relpath(d_before, workdir)))

    # 2. ingest sources
    ing = sources.ingest_snapshot(db, cfg, os.path.join(instance, "fixtures", "sources", "manifest.json"))
    log("## 2. Sources read\n")
    log("| id | kind | read status | revision | license | injection flags |")
    log("|---|---|---|---|---|---|")
    for sid, _ in ing:
        s = db.get("sources", sid)
        flags = loads(s["flags"])
        log("| %d | %s | %s | `%s` | %s | %s |" % (s["id"], s["kind"], s["read_status"], s["revision"], s["license"],
                                                  ", ".join(f["kind"] for f in flags) or "—"))
    log("")

    # 3. propose
    budget = Budget(cfg["budget"])
    pids, problems, _ = propose(db, cfg, make_llm(cfg), budget, GOAL)
    log("## 3. Knowledge and proposals\n")
    for k in db.all("SELECT * FROM knowledge ORDER BY id"):
        log("- Knowledge #%d `%s` from source #%d at %s — applicable: %s" % (
            k["id"], k["method"], k["source_id"], k["locator"], bool(k["applicable"])))
    inj = db.one("SELECT id FROM sources WHERE url LIKE '%attacker%'")
    inj_k = db.one("SELECT COUNT(*) AS n FROM knowledge WHERE source_id=?", (inj["id"],))["n"]
    log("\nThe adversarial page (source #%d) was flagged and produced %d knowledge rows; its instructions were "
        "not executed (no fetch of attacker.example; config sha256 before/after: `%s` / `%s`)."
        % (inj["id"], inj_k, cfg_hash_before[:16], _sha256(cfg_path)[:16]))
    for pid in pids:
        p = db.get("proposals", pid)
        log("\n**Proposal #%d** methods=%s variants=%s\n\n> %s" % (pid, loads(p["methods"]), loads(p["variants"]),
                                                                 p["hypothesis"]))
    log("\nPlanning usage: %s model calls (mock), %s/%s est. tokens.\n" % (
        budget.usage["model_calls"], budget.usage["input_tokens"], budget.usage["output_tokens"]))
    good, contrast = pids[0], pids[1]

    # 4. runs
    log("## 4. Experiment runs\n")
    results = {}
    for label, pid, executor in (("main hypothesis", good, None), ("contrast hypothesis", contrast, None),
                                 ("red team: candidate edits evaluator + holdout", good, "redteam-scope")):
        rid = runner.create_run(db, cfg, pid, executor=executor)
        state = runner.Runner(db, cfg, rid).execute()
        path, _ = run_report(db, cfg, rid)
        results[label] = rid
        dec = loads(db.get("runs", rid)["decision"]) or {}
        log("### Run %d — %s → **%s**\n" % (rid, label, state))
        for r in dec.get("reasons", [])[:6]:
            log("- %s" % r)
        log("- Report: `%s`\n" % os.path.relpath(path, workdir))
    good_run = results["main hypothesis"]
    run_row = db.get("runs", good_run)
    if run_row["state"] != "READY":
        log("Main run did not reach READY; stopping the walkthrough here (no fabricated improvement).")
        return _finish(out, workdir, report_out, db, cfg, t0)

    # 5. apply + collect
    rel2 = runner.apply_run(db, cfg, good_run)
    d_after = os.path.join(workdir, "digests", "digest-release%d.md" % rel2["id"])
    cli("collect", "--input", daily, "--out", d_after)
    log("## 5. Adoption\n")
    log("Run %d applied as release #%d (`%s`), health: %s; active pointer → `%s`." % (
        good_run, rel2["id"], rel2["sha"][:12], loads(rel2["health"])["healthy"],
        os.path.relpath(versions.active_path(cfg), workdir)))
    with open(d_before, encoding="utf-8") as f:
        before_line = f.read().splitlines()[2]
    with open(d_after, encoding="utf-8") as f:
        after_line = f.read().splitlines()[2]
    log("Daily digest on the same snapshot — release #1: “%s” release #%d: “%s”\n" % (before_line, rel2["id"], after_line))
    log("Patch (`%s`):\n" % os.path.relpath(run_row["patch_path"], workdir))
    with open(run_row["patch_path"], encoding="utf-8") as f:
        patch = f.read()
    log("```diff\n%s\n```\n" % (patch if len(patch) < 9000 else patch[:9000] + "\n... (truncated in summary)"))

    # 6. later batch
    res = runner.evaluate_release(db, cfg, "batch2")
    log("## 6. A later batch (batch2) with the adopted release\n")
    prev = res["previous"]["metrics"] if res["previous"] else {}
    log("| release | precision | recall | f1 | false deletions | critical lost |")
    log("|---|---|---|---|---|---|")
    log("| #%d (previous) | %s | %s | %s | %s | %s |" % (res["previous"]["release_id"], prev.get("precision"),
                                                      prev.get("recall"), prev.get("f1"), prev.get("false_deletions"),
                                                      prev.get("critical_lost")))
    m = res["metrics"]
    log("| #%d (active) | %s | %s | %s | %s | %s |\n" % (res["release_id"], m["precision"], m["recall"], m["f1"],
                                                       m["false_deletions"], m["critical_lost"]))
    fails = [i for i in res["per_item"] if i["outcome"] != "ok"]
    log("New failures stored for planning: %s\n" % (", ".join("%s (%s, %s)" % (i["id"], i["outcome"], i["case"])
                                                            for i in fails) or "none"))
    pids2, problems2, skipped2 = propose(db, cfg, make_llm(cfg), Budget(cfg["budget"]), GOAL)
    log("Re-planning after batch2: new proposals %s; already-tested experiments skipped: %s; open problems:\n"
        % (pids2 or "none", [s["proposal"] for s in skipped2]))
    for p in problems2:
        log("- `%s` ×%d (%s) — %s" % (p["case_type"], p["count"], p["items"], p["note"]))
    log("")

    # 7. stop / timeout / budget
    log("## 7. Stop, timeout and budget\n")
    rid = runner.create_run(db, cfg, good, executor="redteam-slow", budget_overrides={"max_process_seconds": 2})
    state = runner.Runner(db, cfg, rid).execute()
    dec = loads(db.get("runs", rid)["decision"]) or {}
    log("- Timeout: run %d (candidate dedup never returns, 2 s process limit) → **%s**: %s" % (
        rid, state, "; ".join(dec.get("reasons", [])[1:3])))

    rid = runner.create_run(db, cfg, good, executor="redteam-slow", budget_overrides={"max_process_seconds": 120})
    env = dict(os.environ, PYTHONPATH=os.path.join(REPO_ROOT, "src"))
    proc = subprocess.Popen([py, "-c", "import sys; from omnilaw_evolution.config import load_config; "
                             "from omnilaw_evolution.db import DB; from omnilaw_evolution.runner import Runner; "
                             "c=load_config(sys.argv[1]); Runner(DB(c.db_path), c, int(sys.argv[2])).execute()",
                             cfg_path, str(rid)], env=env)
    deadline = time.time() + 60
    while time.time() < deadline and db.get("runs", rid)["state"] != "EVALUATING":
        time.sleep(0.2)
    time.sleep(1.0)
    t_stop = time.time()
    runner.request_stop(db, rid)
    proc.wait(timeout=60)
    log("- Stop: run %d executing in another process (pid %d) was stopped during evaluation; it ended in "
        "**%s** %.1f s after the request; checkpoint kept: %s" % (
            rid, proc.pid, db.get("runs", rid)["state"], time.time() - t_stop,
            sorted(loads(db.get("runs", rid)["checkpoint"]).keys())))

    rid = runner.create_run(db, cfg, good, budget_overrides={"max_candidates": 1})
    state = runner.Runner(db, cfg, rid).execute()
    usage = loads(db.get("runs", rid)["usage"])
    log("- Budget: run %d with max_candidates=1 → **%s** (%s); usage saved: candidates=%s" % (
        rid, state, db.get("runs", rid)["error"], usage["candidates"]))
    state = runner.resume(db, cfg, rid, extend={"max_candidates": 2})
    row = db.get("runs", rid)
    usage = loads(row["usage"])
    log("- Resume with +2 candidates → **%s**; cumulative candidates charged=%s (the first candidate was not "
        "regenerated). Baseline of this run is the *current* active release `%s`, which already contains the "
        "adopted change, so: %s\n" % (state, usage["candidates"], row["baseline_sha"][:12],
                                      "; ".join((loads(row["decision"]) or {}).get("reasons", []))))

    # 8. rollback
    checks = runner.rollback_release(db, cfg, rel2["id"])
    d_rb = os.path.join(workdir, "digests", "digest-after-rollback.md")
    cli("collect", "--input", daily, "--out", d_rb)
    log("## 8. Rollback\n")
    log("Rolled back release #%d → #%d. Checks: sha matches=%s, dependency hash matches=%s, state schema "
        "compatible=%s, health=%s. Active SHA now `%s` (baseline `%s`). Digest after rollback is byte-identical "
        "to the release-1 digest: %s.\n" % (
            rel2["id"], rel1["id"], checks["sha_matches"], checks["deps_hash_matches"],
            checks["state_schema_compatible"], checks["healthy"], versions.head_sha(versions.active_path(cfg))[:12],
            rel1["sha"][:12], _sha256(d_rb) == _sha256(d_before)))

    if live:
        log("## 9. Live fetch check (real network)\n")
        lb = Budget(cfg["budget"])
        for url in LIVE_URLS:
            try:
                sid, created = sources.ingest_url(db, cfg, url, lb)
                s = db.get("sources", sid)
                log("- %s → source #%d (%s, %s, revision `%s`, license %s)%s" % (
                    url, sid, s["kind"], s["read_status"], s["revision"], s["license"],
                    "" if created else " — identical content already stored (hash de-dup)"))
            except Exception as exc:
                log("- %s → not fetched: %s: %s" % (url, type(exc).__name__, exc))
        log("\nDownloaded %d bytes (cache in `state/cache`).\n" % lb.usage["download_bytes"])
    return _finish(out, workdir, report_out, db, cfg, t0)


def _finish(out, workdir, report_out, db, cfg, t0):
    out.append("## Summary of runs\n")
    out.append("| run | proposal | executor | state |")
    out.append("|---|---|---|---|")
    for r in db.all("SELECT id, proposal_id, executor, state FROM runs ORDER BY id"):
        out.append("| %d | %d | %s | %s |" % (r["id"], r["proposal_id"], r["executor"], r["state"]))
    out.append("\nKnowledge verification after the demo:\n")
    for k in db.all("SELECT id, method, verification, experiments FROM knowledge ORDER BY id"):
        out.append("- #%d `%s`: %s %s" % (k["id"], k["method"], k["verification"], loads(k["experiments"])))
    out.append("\nTotal demo wall time: %.1f s.\n" % (time.time() - t0))
    text = "\n".join(out) + "\n"
    path = os.path.join(workdir, "DEMO_REPORT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    for r in db.all("SELECT id FROM runs ORDER BY id"):
        run_report(db, cfg, r["id"])
    if report_out:
        # Exported files are meant to be shared: local absolute paths are replaced.
        def scrub(text):
            for real, alias in ((workdir, "<demo>"), (REPO_ROOT, "<repo>"), (os.path.expanduser("~"), "~")):
                text = text.replace(real, alias)
            return text

        def export(src_text, dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(scrub(src_text))

        def read(p):
            with open(p, encoding="utf-8") as f:
                return f.read()

        export(text, os.path.join(report_out, "DEMO_REPORT.md"))
        for name in os.listdir(cfg.subdir("reports")):
            export(read(os.path.join(cfg.subdir("reports"), name)), os.path.join(report_out, "runs", name))
        for r in db.all("SELECT id, patch_path FROM runs WHERE patch_path IS NOT NULL"):
            export(read(r["patch_path"]), os.path.join(report_out, "runs", "run-%d.patch" % r["id"]))
        ddir = os.path.join(workdir, "digests")
        for name in sorted(os.listdir(ddir)) if os.path.isdir(ddir) else []:
            export(read(os.path.join(ddir, name)), os.path.join(report_out, "digests", name))
        rows = db.all("SELECT id, kind, url, title, fetched_at, content_hash, revision, license, read_status, "
                      "origin, flags FROM sources ORDER BY id")
        export(json.dumps(rows, ensure_ascii=False, indent=2), os.path.join(report_out, "sources.json"))
        rows = db.all("SELECT id, source_id, method, claim, steps, conditions, limitations, locator, excerpt, "
                      "verification, applicable, experiments, extractor, created_at, reviewed_at "
                      "FROM knowledge ORDER BY id")
        export(json.dumps(rows, ensure_ascii=False, indent=2), os.path.join(report_out, "knowledge.json"))
    print(text)
    return path
