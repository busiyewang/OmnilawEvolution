"""Daily collection pipeline. This is the code path the agent uses for its
technical digest; it imports whatever ``dedup`` the active release ships.

    python -m omnilaw_evolution.collector.pipeline --input items.jsonl --out digest.md
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict

from omnilaw_evolution.collector.dedup import dedup


def load_items(path):
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                obj.pop("label", None)  # labels are never visible to the pipeline
                items.append(obj)
    return items


def collect(items):
    decisions = dedup(items)
    if [d["id"] for d in decisions] != [i["id"] for i in items]:
        raise ValueError("dedup must return one decision per item, in input order")
    by_id = {i["id"]: i for i in items}
    kept = OrderedDict()
    for d in decisions:
        if d["duplicate_of"] is None:
            kept[d["id"]] = {"item": by_id[d["id"]], "also": []}
    for d in decisions:
        if d["duplicate_of"] is not None:
            if d["duplicate_of"] not in kept:
                raise ValueError("duplicate_of must reference a kept item: %s" % d["id"])
            kept[d["duplicate_of"]]["also"].append(by_id[d["id"]]["url"])
    return {"kept": list(kept.values()), "removed": sum(1 for d in decisions if d["duplicate_of"]),
            "decisions": decisions}


def render_digest(result, title="技术日报 / Tech Digest"):
    lines = ["# %s" % title, "",
             "共 %d 条，去重移除 %d 条。" % (len(result["kept"]) + result["removed"], result["removed"]), ""]
    for n, entry in enumerate(result["kept"], 1):
        it = entry["item"]
        lines.append("%d. [%s](%s)" % (n, it.get("title", "").replace("]", ")"), it.get("url", "")))
        if entry["also"]:
            lines.append("   - 另见 %d 个重复来源" % len(entry["also"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true", help="print decisions as JSON")
    args = ap.parse_args(argv)
    result = collect(load_items(args.input))
    if args.json:
        json.dump(result["decisions"], sys.stdout, ensure_ascii=False)
        return 0
    text = render_digest(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
