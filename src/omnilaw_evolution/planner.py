"""Improvement planning: from goal + knowledge (+ open failures) to stored
proposals. Every proposal fixes the target module, the methods, the knowledge
it relies on, the variants to screen and the adoption criteria before any
candidate exists."""
from __future__ import annotations

from .db import loads, now
from .knowledge import extract_for_source, implementable_methods


def ensure_knowledge(db, llm, budget):
    """Run extraction for sources that have no knowledge rows yet."""
    created = []
    for src in db.all("SELECT * FROM sources WHERE id NOT IN (SELECT DISTINCT source_id FROM knowledge) "
                      "ORDER BY id"):
        budget.charge("model_calls")
        created += extract_for_source(db, llm, src)
        budget.add(input_tokens=llm.last_usage.get("input_tokens"),
                   output_tokens=llm.last_usage.get("output_tokens"), cost_usd=llm.last_usage.get("cost_usd"))
    return created


# Failure classes -> knowledge methods that would address them.
COVERS = {"cross-language": ("cross_lingual_embedding",),
          "paraphrase": ("semantic_embedding", "paraphrase_classifier"),
          "exact-url": ("url_canonicalization",), "tracking-params": ("url_canonicalization",),
          "scheme-and-www": ("url_canonicalization",), "fragment-and-slash": ("url_canonicalization",),
          "reprint": ("shingle_jaccard",), "near-duplicate-title": ("shingle_jaccard",)}


def open_problems(db, knowledge, cfg=None, include_covered=False):
    """Classify persisting failure classes:

    * needs_research - no stored knowledge addresses it; research topic attached
    * blocked        - knowledge exists, but no method has a code template, so the
                       configured executor cannot implement it (human decision needed)
    * untriaged      - a failure kind with no known mapping (e.g. user reports)
    Failures from the holdout split are never stored, so they never appear here.
    """
    topics = (cfg or {}).get("failure_topics", {})
    templated = set(implementable_methods(cfg))
    out = []
    for row in db.all("SELECT case_type, COUNT(*) AS n, GROUP_CONCAT(item_id) AS items FROM failures "
                      "WHERE status='open' GROUP BY case_type ORDER BY n DESC"):
        case = row["case_type"]
        entry = {"case_type": case, "count": row["n"], "items": row["items"]}
        needed = COVERS.get(case)
        if not needed:
            entry.update(status="untriaged", note="no mapping from this failure kind to research; needs triage")
            if case in topics:
                entry.update(status="needs_research", topic=topics[case])
            out.append(entry)
            continue
        found = [k for k in knowledge if k["method"] in needed and k["applicable"]]
        if not found:
            entry.update(status="needs_research", topic=topics.get(case, case.replace("-", " ")),
                         note="no stored knowledge covers this; research needed")
        elif not [k for k in found if k["method"] in templated]:
            entry.update(status="blocked", knowledge_ids=[k["id"] for k in found],
                         methods=sorted({k["method"] for k in found}),
                         note="knowledge found but no usable implementation: these methods need the optional "
                              "semantic runtime (or a model executor), which is not enabled or not installed. "
                              "Needs a human decision: approve the dependency (semantic.enabled) and fetch "
                              "the pinned model.")
        else:
            entry.update(status="covered", knowledge_ids=[k["id"] for k in found],
                         methods=sorted({k["method"] for k in found if k["method"] in templated}))
        out.append(entry)
    return out if include_covered else [e for e in out if e["status"] != "covered"]


def propose(db, cfg, llm, budget, goal):
    ensure_knowledge(db, llm, budget)
    knowledge = db.all("SELECT * FROM knowledge ORDER BY id")
    failures = db.all("SELECT item_id, case_type, kind, detail FROM failures WHERE status='open'")
    budget.charge("model_calls")
    plans = llm.plan(goal, knowledge, failures, implementable=implementable_methods(cfg))
    budget.add(input_tokens=llm.last_usage.get("input_tokens"), output_tokens=llm.last_usage.get("output_tokens"),
               cost_usd=llm.last_usage.get("cost_usd"))
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
    return ids, open_problems(db, knowledge, cfg), skipped


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
