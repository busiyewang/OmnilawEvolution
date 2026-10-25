"""Collector deduplication module (evolution target).

Contract used by the collector pipeline and the evaluator:

    dedup(items) -> list of {"id": str, "duplicate_of": str | None, "reason": str}

One decision per input item, in input order. ``duplicate_of`` must name an
earlier item that was kept. Baseline: two items are duplicates only when their
URL strings are byte-identical.
"""
from __future__ import annotations


def dedup(items):
    seen = {}
    decisions = []
    for item in items:
        key = item.get("url", "")
        if key in seen:
            decisions.append({"id": item["id"], "duplicate_of": seen[key], "reason": "same-url"})
        else:
            seen[key] = item["id"]
            decisions.append({"id": item["id"], "duplicate_of": None, "reason": "kept"})
    return decisions
