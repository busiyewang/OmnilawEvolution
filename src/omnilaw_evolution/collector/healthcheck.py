"""Release health check: run the real pipeline on a tiny built-in batch and
check the invariants a release must keep. Exit code 0 = healthy.

    python -m omnilaw_evolution.collector.healthcheck
"""
from __future__ import annotations

import json
import sys

from omnilaw_evolution.collector.pipeline import collect, render_digest

SMOKE = [
    {"id": "s1", "url": "https://example.org/a", "title": "Alpha release notes",
     "text": "Alpha 1.0 ships a new scheduler and drops Python 3.8 support.", "lang": "en"},
    {"id": "s2", "url": "https://example.org/a", "title": "Alpha release notes",
     "text": "Alpha 1.0 ships a new scheduler and drops Python 3.8 support.", "lang": "en"},
    {"id": "s3", "url": "https://example.org/b", "title": "Beta security advisory",
     "text": "Beta 2.3 fixes an authentication bypass; upgrade immediately.", "lang": "en"},
    {"id": "s4", "url": "https://example.org/c", "title": "伽马数据库发布 3.0",
     "text": "伽马数据库 3.0 新增列式存储，查询性能提升一倍。", "lang": "zh"},
]
MUST_KEEP = {"s1", "s3", "s4"}


def check():
    problems = []
    result = collect([dict(i) for i in SMOKE])
    kept = {e["item"]["id"] for e in result["kept"]}
    if not MUST_KEEP <= kept:
        problems.append("distinct items dropped: %s" % sorted(MUST_KEEP - kept))
    if "s2" in kept:
        problems.append("identical duplicate s2 kept")
    if collect([dict(i) for i in SMOKE])["decisions"] != result["decisions"]:
        problems.append("non-deterministic")
    if "伽马" not in render_digest(result):
        problems.append("digest rendering lost content")
    return problems


def main():
    try:
        problems = check()
    except Exception as exc:  # any crash is unhealthy
        problems = ["crash: %r" % exc]
    print(json.dumps({"healthy": not problems, "problems": problems}, ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
