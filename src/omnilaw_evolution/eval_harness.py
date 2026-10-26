"""Runs inside the sandbox. Loads ``dedup`` from a given code root, applies it
to unlabeled items and writes decisions plus timing. It never sees labels.

    python -I -B eval_harness.py <code_src_dir> <items.json> <out.json>
"""
import json
import sys
import time


def evaluate_groups(dedup, groups):
    decisions = []
    for items in groups:
        result = dedup([dict(i) for i in items])
        if not isinstance(result, list):
            raise TypeError("dedup must return a list for every batch")
        decisions.extend(result)
    return decisions


def main():
    src, inp, out = sys.argv[1:4]
    sys.path.insert(0, src)
    from omnilaw_evolution.collector.dedup import dedup  # noqa: E402

    with open(inp, encoding="utf-8") as f:
        groups = json.load(f)["groups"]
    t0 = time.perf_counter()
    first = evaluate_groups(dedup, groups)
    t1 = time.perf_counter()
    second = evaluate_groups(dedup, groups)
    empty = dedup([])
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"decisions": first, "deterministic": first == second, "empty_ok": empty == [],
                   "seconds": t1 - t0}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
