"""Markdown reports for runs. Every number comes from the DB/evaluation files;
the report states what was simulated and what isolation was in effect."""
from __future__ import annotations

import os

from .db import loads

METRIC_COLS = ("n", "precision", "recall", "f1", "false_deletions", "false_deletion_rate", "wrong_links",
               "critical_lost", "ms_per_item")


def _metrics_row(label, m):
    if not m or "error" in m:
        return "| %s | %s |" % (label, " | ".join(["error"] + [""] * (len(METRIC_COLS) - 1)))
    vals = []
    for c in METRIC_COLS:
        v = m.get(c)
        vals.append(", ".join(v) if isinstance(v, list) and v else ("0" if isinstance(v, list) else str(v)))
    return "| %s | %s |" % (label, " | ".join(vals))


def _table(rows):
    head = "| variant | " + " | ".join(METRIC_COLS) + " |"
    sep = "|" + "---|" * (len(METRIC_COLS) + 1)
    return "\n".join([head, sep] + rows)


def run_report(db, cfg, run_id):
    run = db.get("runs", run_id)
    prop = db.get("proposals", run["proposal_id"])
    ck = loads(run["checkpoint"], {})
    usage = loads(run["usage"], {})
    decision = loads(run["decision"], {}) or {}
    model = loads(run["model_config"], {})
    lines = ["# Run %d — %s" % (run_id, run["state"]), "",
             "- Goal: %s" % prop["goal"],
             "- Proposal #%d (planner: `%s`)" % (prop["id"], prop["planner"]),
             "- Mode: `%s`; executor: `%s`; model adapter: `%s`%s" % (
                 run["mode"], run["executor"], model.get("provider"),
                 " (offline mock — no model was called)" if model.get("provider") == "mock" else ""),
             "- Baseline SHA: `%s`" % run["baseline_sha"],
             "- Candidate SHA: `%s`" % (run["candidate_sha"] or "—"),
             "- Data split version: `%s`; evaluator version: `%s`" % (run["split_version"], run["evaluator_version"]),
             "- Patch: `%s`" % (run["patch_path"] or "—"), ""]
    lines += ["## Hypothesis", "", "**Problem.** " + prop["problem"], "", "**Hypothesis.** " + prop["hypothesis"],
              "", "**Methods:** " + ", ".join(loads(prop["methods"])), ""]
    lines += ["## Sources and knowledge used", ""]
    for kid in loads(prop["knowledge_ids"], []):
        k = db.get("knowledge", kid)
        s = db.get("sources", k["source_id"])
        lines.append("- Knowledge #%d `%s` — verification now: **%s**" % (kid, k["method"], k["verification"]))
        lines.append("  - Source #%d (%s, %s): %s" % (s["id"], s["kind"], s["read_status"], s["url"]))
        lines.append("  - Revision `%s`; license %s; location %s" % (s["revision"], s["license"], k["locator"]))
        lines.append("  - Conditions: %s" % k["conditions"])
        lines.append("  - Limitations: %s" % k["limitations"])
    lines.append("")
    patched = ck.get("patched", {})
    if patched:
        lines += ["## Candidates", ""]
        for key, e in sorted(patched.items()):
            lines.append("- v%s %s — %s%s" % (key, e["variant"], e["status"],
                                             (" (sha `%s`)" % e["sha"][:12]) if e.get("sha") else ""))
            for v in e.get("violations", []):
                lines.append("  - violation: `%s` — %s" % (v["path"], v["reason"]))
        lines.append("")
    dev = ck.get("dev", {})
    if dev:
        rows = [_metrics_row("baseline", dev.get("baseline", {}).get("metrics"))]
        for key, v in sorted((k, v) for k, v in dev.items() if k != "baseline"):
            rows.append(_metrics_row("v%s %s" % (key, patched[key]["variant"]), v["metrics"]))
        lines += ["## Dev split (candidate selection)", "", _table(rows), ""]
    hold = ck.get("holdout", {})
    if hold:
        rows = [_metrics_row("baseline", hold.get("baseline", {}).get("metrics"))]
        if "candidate" in hold:
            c = hold["candidate"]
            rows.append(_metrics_row("v%s (selected)" % c["key"], c["metrics"]))
        lines += ["## Holdout split (adoption decision, scored once)", "", _table(rows), ""]
        if "candidate" in hold:
            lines.append("Outcome counts on holdout (outcome/case): " + ", ".join(
                "%s=%d" % kv for kv in sorted(hold["candidate"]["outcomes"].items())))
            lines.append("")
            lines.append("Isolation for candidate execution: `%s`." % hold["candidate"]["isolation"])
            lines.append("")
    if "unit_tests" in ck:
        lines += ["Protected contract tests on candidate: **%s**" % ("pass" if ck["unit_tests"]["passed"] else "FAIL"),
                  ""]
    lines += ["## Decision", "", "Current state **%s**; evaluation decision **%s**." % (
        run["state"], decision.get("state", "—")), ""]
    for r in decision.get("reasons", []):
        lines.append("- " + r)
    if run["error"]:
        lines.append("- error: " + run["error"])
    lines += ["", "## Cost", "",
              "- Model calls: %s (input tokens %s, output tokens %s%s)" % (
                  usage.get("model_calls", 0), usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                  ", estimated by the mock adapter" if model.get("provider") == "mock" else ""),
              "- Money: %s" % ("not applicable (no paid API call)" if model.get("provider") == "mock"
                               else (usage.get("cost_usd") or "unknown — price not configured; tokens above")),
              "- Candidates: %s; sandbox seconds: %.2f; wall seconds: %.2f" % (
                  usage.get("candidates", 0), usage.get("sandbox_seconds", 0) or 0, usage.get("wall_seconds", 0) or 0),
              ""]
    events = db.all("SELECT ts, kind, detail FROM events WHERE run_id=? ORDER BY id", (run_id,))
    lines += ["## Event log", ""] + ["- %s `%s` %s" % (e["ts"], e["kind"], e["detail"] or "") for e in events]
    path = os.path.join(cfg.subdir("reports"), "run-%d.md" % run_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path, "\n".join(lines) + "\n"
