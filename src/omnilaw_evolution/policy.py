"""Write-scope guard and adoption rules. Both run in the orchestrator; a
candidate cannot influence them because their files are outside its writable
scope and evaluation reads them from the stable checkout."""
from __future__ import annotations

import os
import re
import subprocess


def _glob_re(pat):
    """Path glob: ``**`` spans directories, ``*`` and ``?`` stay inside one segment."""
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile(out + r"\Z")


def _match(path, patterns):
    return any(_glob_re(p).match(path) for p in patterns)


def changed_files(workdir, baseline_sha):
    """Every path that differs from the baseline: tracked edits, deletions and
    untracked files (ignored files included, so nothing can hide there)."""
    out = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"],
                         cwd=workdir, capture_output=True, text=True, check=True).stdout
    changes = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        if not e:
            i += 1
            continue
        status, path = e[:2], e[3:]
        if status[0] in "RC":
            i += 1  # rename source follows
        changes.append((status, path))
        i += 1
    committed = subprocess.run(["git", "diff", "--name-status", "-z", baseline_sha, "HEAD"], cwd=workdir,
                               capture_output=True, text=True, check=True).stdout.split("\0")
    for st, p in zip(committed[0::2], committed[1::2]):
        if st:
            changes.append((st, p))
    return changes


def check_scope(workdir, baseline_sha, writable, protected):
    """Return list of violations; empty means the change set is in scope."""
    violations = []
    changes = changed_files(workdir, baseline_sha)
    if not changes:
        violations.append({"path": None, "reason": "no change produced"})
    for status, path in changes:
        full = os.path.join(workdir, path)
        if os.path.islink(full):
            violations.append({"path": path, "reason": "symlink not allowed"})
        elif "D" in status:
            violations.append({"path": path, "reason": "deletion not allowed"})
        elif _match(path, protected):
            violations.append({"path": path, "reason": "protected path"})
        elif not _match(path, writable):
            violations.append({"path": path, "reason": "outside writable scope"})
    return violations


def decide(baseline, candidate, criteria, regression):
    """Adoption decision from holdout metrics. Returns (ready: bool, reasons)."""
    reasons = []
    primary = criteria["primary"]
    delta = candidate[primary] - baseline[primary]
    ok = True
    if delta < criteria["min_delta"]:
        ok = False
        reasons.append("%s delta %+.3f < required %+.3f" % (primary, delta, criteria["min_delta"]))
    else:
        reasons.append("%s delta %+.3f >= %+.3f" % (primary, delta, criteria["min_delta"]))
    if candidate["false_deletion_rate"] > criteria["max_false_deletion_rate"]:
        ok = False
        reasons.append("false-deletion rate %.3f > %.3f" % (candidate["false_deletion_rate"],
                                                           criteria["max_false_deletion_rate"]))
    if candidate["critical_lost"]:
        ok = False
        reasons.append("critical items removed: %s" % ", ".join(candidate["critical_lost"]))
    if candidate["ms_per_item"] > criteria["max_ms_per_item"]:
        ok = False
        reasons.append("%.2f ms/item > %.2f" % (candidate["ms_per_item"], criteria["max_ms_per_item"]))
    if criteria.get("require_regression_pass") and not regression.get("passed"):
        ok = False
        reasons.append("regression checks failed: %s" % "; ".join(regression.get("failures", [])[:5]))
    return ok, reasons
