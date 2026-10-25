"""Improvement planning: from goal + knowledge (+ open failures) to stored
proposals. Every proposal fixes the target module, the methods, the knowledge
it relies on, the variants to screen and the adoption criteria before any
candidate exists."""
from __future__ import annotations

from .db import loads, now
from .knowledge import extract_for_source


def ensure_knowledge(db, llm, budget):
    """Run extraction for sources that have no knowledge rows yet."""
    created = []
    for src in db.all("SELECT * FROM sources WHERE id NOT IN (SELECT DISTINCT source_id FROM knowledge) "
                      "ORDER BY id"):
        budget.charge("model_calls")
        created += extract_for_source(db, llm, src)
        budget.add(input_tokens=llm.last_usage.get("input_tokens"),
                   output_tokens=llm.last_usage.get("output_tokens"))
    return created


def open_problems(db, knowledge):
    """Failure classes that persist and that no stored method addresses. These
    are reported as research directions instead of being retried blindly."""
    covers = {"cross-language": ("translation", "cross_lingual", "multilingual_embedding"),
              "paraphrase": ("paraphrase", "semantic_embedding", "multilingual_embedding")}
    methods = {k["method"] for k in knowledge}
    out = []
    for row in db.all("SELECT case_type, COUNT(*) AS n, GROUP_CONCAT(item_id) AS items FROM failures "
                      "WHERE status='open' GROUP BY case_type ORDER BY n DESC"):
        needed = covers.get(row["case_type"])
        if needed and not methods.intersection(needed):
            out.append({"case_type": row["case_type"], "count": row["n"], "items": row["items"],
                        "note": "no stored knowledge covers this; research needed (e.g. topic '%s')"
                                % ("cross-lingual near-duplicate detection" if row["case_type"] == "cross-language"
                                   else "paraphrase detection")})
    return out


def propose(db, cfg, llm, budget, goal):
    ensure_knowledge(db, llm, budget)
    knowledge = db.all("SELECT * FROM knowledge ORDER BY id")
    failures = db.all("SELECT item_id, case_type, kind, detail FROM failures WHERE status='open'")
    budget.charge("model_calls")
    plans = llm.plan(goal, knowledge, failures)
    budget.add(input_tokens=llm.last_usage.get("input_tokens"), output_tokens=llm.last_usage.get("output_tokens"))
    valid_ids = {k["id"] for k in knowledge}
    ids, skipped = [], []
    for p in plans:
        cited = [k for k in p["knowledge_ids"] if k in valid_ids]
        if not cited:
            continue  # a proposal without evidence is not stored
        variants = p["variants"][: cfg["budget"]["max_candidates"]]
        seen = _same_proposal(db, p["methods"], variants)
        if seen:
            # Repeated research suppression: the same experiment is not rerun.
            skipped.append({"proposal": seen["id"], "status": seen["status"], "methods": p["methods"]})
            continue
        pid = db.insert("proposals", {
            "goal": goal, "problem": p["problem"], "module": cfg["target_module"],
            "hypothesis": p["hypothesis"], "methods": p["methods"], "knowledge_ids": cited,
            "variants": variants,
            "criteria": cfg["criteria"], "planner": llm.name, "created_at": now()})
        ids.append(pid)
    return ids, open_problems(db, knowledge), skipped


def _same_proposal(db, methods, variants):
    for row in db.all("SELECT id, status, methods, variants FROM proposals"):
        if loads(row["methods"]) == methods and loads(row["variants"]) == variants:
            return row
    return None


def provenance_lines(db, proposal):
    lines = []
    for kid in loads(proposal["knowledge_ids"], []):
        k = db.get("knowledge", kid)
        s = db.get("sources", k["source_id"])
        lines.append("%s <- knowledge #%d: %s @ %s [%s; %s]" % (
            k["method"], kid, s["url"], k["locator"], s["revision"], s["read_status"]))
    return lines
