"""omnilaw-evolution command line."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import runner, sources, versions
from .budget import Budget, BudgetExhausted
from .config import load_config
from .db import DB, loads
from .llm import make_llm
from .planner import propose
from .report import run_report


def _ctx(args):
    cfg = load_config(args.config)
    return cfg, DB(cfg.db_path)


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(args):
    cfg, db = _ctx(args)
    rel = versions.init_releases(db, cfg)
    _print({"active_release": rel["id"], "sha": rel["sha"], "path": rel["path"], "state_dir": cfg["state_dir"]})


def cmd_research(args):
    cfg, db = _ctx(args)
    budget = Budget(cfg["budget"])
    found = sources.discover(cfg, args.topic, budget)
    ingested = []
    for f in found[: args.ingest]:
        if f["kind"] in ("paper", "github", "web"):
            try:
                sid, created = sources.ingest_url(db, cfg, f["url"], budget)
                ingested.append({"url": f["url"], "source_id": sid, "new": created})
            except BudgetExhausted as exc:
                ingested.append({"url": f["url"], "error": str(exc)})
                break
            except Exception as exc:
                ingested.append({"url": f["url"], "error": "%s: %s" % (type(exc).__name__, exc)})
    _print({"found": found, "ingested": ingested, "download_bytes": budget.usage["download_bytes"]})


def cmd_ingest(args):
    cfg, db = _ctx(args)
    out = []
    if args.snapshot:
        for sid, created in sources.ingest_snapshot(db, cfg, args.snapshot):
            out.append({"source_id": sid, "new": created})
    for url in args.url or []:
        sid, created = sources.ingest_url(db, cfg, url, Budget(cfg["budget"]))
        out.append({"url": url, "source_id": sid, "new": created})
    for o in out:
        s = db.get("sources", o["source_id"])
        o.update(kind=s["kind"], read_status=s["read_status"], revision=s["revision"], flags=loads(s["flags"]))
    _print(out)


def cmd_sources(args):
    cfg, db = _ctx(args)
    _print(db.all("SELECT id, kind, url, revision, license, read_status, origin, flags FROM sources ORDER BY id"))


def cmd_knowledge(args):
    cfg, db = _ctx(args)
    _print(db.all("SELECT id, source_id, method, verification, applicable, locator, experiments FROM knowledge "
                  "ORDER BY id"))


def cmd_propose(args):
    cfg, db = _ctx(args)
    budget = Budget(cfg["budget"])
    ids, problems, skipped = propose(db, cfg, make_llm(cfg), budget, args.goal)
    _print({"proposals": [dict(db.get("proposals", i), methods=loads(db.get("proposals", i)["methods"])) for i in ids],
            "already_tested": skipped, "open_problems": problems, "usage": budget.usage})


def cmd_run(args):
    cfg, db = _ctx(args)
    rid = runner.create_run(db, cfg, args.proposal, args.mode, executor=args.executor)
    print("run %d started" % rid, file=sys.stderr)
    state = runner.Runner(db, cfg, rid).execute()
    path, _ = run_report(db, cfg, rid)
    _print({"run": rid, "state": state, "report": path, "decision": loads(db.get("runs", rid)["decision"])})


def cmd_status(args):
    cfg, db = _ctx(args)
    if args.run:
        r = db.get("runs", args.run)
        _print({k: (loads(v) if k in ("usage", "decision", "budget") else v) for k, v in r.items()
                if k != "checkpoint"})
    else:
        _print({"active_release": versions.current_release(db),
                "runs": db.all("SELECT id, proposal_id, state, updated_at FROM runs ORDER BY id DESC LIMIT 20")})


def cmd_report(args):
    cfg, db = _ctx(args)
    path, text = run_report(db, cfg, args.run)
    print(text)
    print("(written to %s)" % path, file=sys.stderr)


def cmd_stop(args):
    cfg, db = _ctx(args)
    r = runner.request_stop(db, args.run)
    _print({"run": args.run, "state": r["state"], "stop_requested": bool(r["stop_requested"])})


def cmd_resume(args):
    cfg, db = _ctx(args)
    extend = {}
    if args.extend_model_calls:
        extend["max_model_calls"] = args.extend_model_calls
    if args.extend_candidates:
        extend["max_candidates"] = args.extend_candidates
    if args.extend_wall_seconds:
        extend["max_wall_seconds"] = args.extend_wall_seconds
    state = runner.resume(db, cfg, args.run, extend or None)
    _print({"run": args.run, "state": state, "report": run_report(db, cfg, args.run)[0]})


def cmd_apply(args):
    cfg, db = _ctx(args)
    rel = runner.apply_run(db, cfg, args.run)
    _print({"release": rel["id"], "sha": rel["sha"], "health": loads(rel["health"]), "active": versions.active_path(cfg)})


def cmd_rollback(args):
    cfg, db = _ctx(args)
    _print(runner.rollback_release(db, cfg, args.release, args.to))


def cmd_releases(args):
    cfg, db = _ctx(args)
    _print(db.all("SELECT id, sha, run_id, policy, status, previous_release_id, deps_hash, schema_version, path "
                  "FROM releases ORDER BY id"))


def cmd_collect(args):
    """Daily collection through the active release (sandboxed, no network)."""
    from . import sandbox
    cfg, db = _ctx(args)
    root = versions.active_path(cfg)
    if not root:
        sys.exit("no active release; run init first")
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    res = sandbox.run([sys.executable, "-B", "-m", "omnilaw_evolution.collector.pipeline",
                       "--input", os.path.abspath(args.input), "--out", out],
                      scratch=os.path.join(cfg.subdir("collect"), "scratch"), cwd=root,
                      allow_write=[os.path.dirname(out)], deny_read=[cfg["eval_dir"]], network=False,
                      timeout=cfg["budget"]["max_process_seconds"], mode=cfg["sandbox"]["mode"],
                      env_extra={"PYTHONPATH": os.path.join(root, "src")})
    if res["returncode"] != 0:
        sys.exit(res["stderr"])
    _print({"release_sha": versions.head_sha(root), "digest": out, "isolation": res["isolation"]})


def cmd_evaluate_release(args):
    cfg, db = _ctx(args)
    res = runner.evaluate_release(db, cfg, args.split)
    _print({"release": res["release_id"], "split": args.split, "metrics": res["metrics"],
            "previous_release": res["previous"],
            "failures": [i for i in res["per_item"] if i["outcome"] != "ok"]})


def cmd_demo(args):
    from .demo import run_demo
    run_demo(args.workdir, report_out=args.report_out, live=not args.offline)


def build_parser():
    ap = argparse.ArgumentParser(prog="omnilaw-evolution", description="Self-evolving research agent (phase 1)")
    ap.add_argument("--config", help="config file (default $OMNILAW_CONFIG or ./omnilaw.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    add("init", cmd_init, "register the current HEAD as the first active release")
    p = add("research", cmd_research, "discover sources for a topic (arXiv, GitHub, seeds) and ingest some")
    p.add_argument("--topic", required=True)
    p.add_argument("--ingest", type=int, default=3)
    p = add("ingest", cmd_ingest, "ingest a URL (web / GitHub file / arXiv) or a snapshot manifest")
    p.add_argument("--url", action="append")
    p.add_argument("--snapshot")
    add("sources", cmd_sources, "list sources")
    add("knowledge", cmd_knowledge, "list knowledge records")
    p = add("propose", cmd_propose, "extract knowledge and create proposals for a goal")
    p.add_argument("--goal", required=True)
    p = add("run", cmd_run, "run a proposal end to end (shadow: produce candidate + report only)")
    p.add_argument("--proposal", type=int, required=True)
    p.add_argument("--mode", default="shadow", choices=["shadow"])
    p.add_argument("--executor", choices=["template", "claude-code", "redteam-scope", "redteam-slow",
                                          "redteam-exfil"])
    p = add("status", cmd_status, "show run or overall status")
    p.add_argument("--run", type=int)
    p = add("report", cmd_report, "write and print the Markdown report of a run")
    p.add_argument("--run", type=int, required=True)
    p = add("stop", cmd_stop, "request a run to stop")
    p.add_argument("--run", type=int, required=True)
    p = add("resume", cmd_resume, "resume a stopped / failed / budget-exhausted run")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--extend-model-calls", type=int)
    p.add_argument("--extend-wall-seconds", type=int)
    p.add_argument("--extend-candidates", type=int)
    p = add("apply", cmd_apply, "adopt a READY run as the active release (health-checked)")
    p.add_argument("--run", type=int, required=True)
    p = add("rollback", cmd_rollback, "switch back from a release to its predecessor (or --to)")
    p.add_argument("--release", type=int, required=True)
    p.add_argument("--to", type=int)
    add("releases", cmd_releases, "list releases")
    p = add("collect", cmd_collect, "build a digest from an item snapshot using the active release")
    p.add_argument("--input", required=True)
    p.add_argument("--out", required=True)
    p = add("evaluate-release", cmd_evaluate_release, "score the active release on another batch")
    p.add_argument("--split", default="batch2")
    p = add("demo", cmd_demo, "build a demo instance and run the full phase-1 walkthrough")
    p.add_argument("--workdir", required=True)
    p.add_argument("--report-out")
    p.add_argument("--offline", action="store_true", help="skip the live fetch check")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (runner.RunError, ValueError, RuntimeError) as exc:
        sys.exit("error: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
