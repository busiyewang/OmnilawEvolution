"""Replay one staged Hermes skill write with the pinned upstream code.

Runs sandboxed, with ``HERMES_HOME`` pointing at a scratch home whose ``skills``
is the candidate tree. Prints one JSON line: the upstream tool result.
"""
from __future__ import annotations

import json
import sys


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        init = json.load(f)
    real_stdout, sys.stdout = sys.stdout, sys.stderr  # upstream diagnostics stay off the protocol line
    try:
        sys.path.insert(0, init["source"])
        import hermes_bootstrap  # noqa: F401 -- includes import-path hardening
        from tools.skill_manager_tool import apply_skill_pending

        raw = apply_skill_pending(init["payload"])
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            result = {"success": False, "error": str(raw)[:2000]}
    except Exception as exc:
        result = {"success": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    real_stdout.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    main()
