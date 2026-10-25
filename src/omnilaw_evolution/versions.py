"""Versions: candidate checkouts, releases and the active pointer.

* Candidates are git worktrees under ``experiments_dir`` with ``/eval/``
  excluded by sparse checkout, committed on ``omnilaw/run-<id>-v<n>`` branches.
* A release is a detached worktree under ``<state_dir>/releases/<sha12>``.
* ``<state_dir>/releases/active`` is a symlink to the live release, switched
  atomically. The collector always runs from it. Rollback re-points it after
  verifying dependencies, state schema and health.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys

from . import sandbox
from .db import SCHEMA_VERSION, now, schema_compatible

GIT_ID = ["-c", "user.name=omnilaw-evolution", "-c", "user.email=omnilaw-evolution@localhost",
          "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]


def git(cwd, *args, check=True):
    return subprocess.run(["git"] + GIT_ID + list(args), cwd=cwd, capture_output=True, text=True,
                          check=check).stdout.strip()


def head_sha(root):
    return git(root, "rev-parse", "HEAD")


def deps_hash(root):
    h = hashlib.sha256()
    for name in ("pyproject.toml", "requirements.lock"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()[:16]


def _remove_worktree(project_root, path):
    if os.path.exists(path):
        git(project_root, "worktree", "remove", "--force", path, check=False)
        shutil.rmtree(path, ignore_errors=True)
    git(project_root, "worktree", "prune", check=False)


def create_candidate(cfg, run_id, idx, baseline_sha):
    root = cfg["project_root"]
    path = os.path.join(cfg["experiments_dir"], "run-%d" % run_id, "v%d" % idx)
    branch = "omnilaw/run-%d-v%d" % (run_id, idx)
    _remove_worktree(root, path)  # idempotent on resume
    git(root, "branch", "-D", branch, check=False)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    git(root, "worktree", "add", "--no-checkout", "-b", branch, path, baseline_sha)
    git(path, "config", "extensions.worktreeConfig", "true")
    git(path, "sparse-checkout", "set", "--no-cone", "/*", "!/eval/")
    git(path, "checkout", branch)
    if os.path.exists(os.path.join(path, "eval")):
        raise RuntimeError("sparse checkout failed to exclude eval/ from candidate")
    return path


def commit_candidate(path, writable, message):
    for p in writable:
        if os.path.exists(os.path.join(path, p)):
            git(path, "add", "--", p)
    git(path, "commit", "-m", message)
    return head_sha(path)


def patch_text(root, base, cand):
    return git(root, "diff", base, cand)


def releases_dir(cfg):
    return cfg.subdir("releases")


def active_path(cfg):
    link = os.path.join(releases_dir(cfg), "active")
    return os.path.realpath(link) if os.path.islink(link) else None


def ensure_release_tree(cfg, sha):
    path = os.path.join(releases_dir(cfg), sha[:12])
    if os.path.isdir(path) and head_sha(path) == sha:
        return path
    _remove_worktree(cfg["project_root"], path)
    git(cfg["project_root"], "worktree", "add", "--detach", path, sha)
    return path


def switch_active(cfg, path):
    link = os.path.join(releases_dir(cfg), "active")
    tmp = link + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(path, tmp)
    os.replace(tmp, link)  # atomic on POSIX


def healthcheck(cfg, root):
    scratch = os.path.join(releases_dir(cfg), ".health-%s" % os.path.basename(root))
    res = sandbox.run([sys.executable, "-B", "-m", "omnilaw_evolution.collector.healthcheck"],
                      scratch=scratch, cwd=root, network=False, timeout=cfg["budget"]["max_process_seconds"],
                      mode=cfg["sandbox"]["mode"], env_extra={"PYTHONPATH": os.path.join(root, "src")})
    try:
        report = json.loads(res["stdout"].strip().splitlines()[-1])
    except Exception:
        report = {"healthy": False, "problems": ["unreadable output: " + res["stderr"][-300:]]}
    report.update(deps_hash=deps_hash(root), sha=head_sha(root), isolation=res["isolation"])
    report["healthy"] = bool(report.get("healthy")) and res["returncode"] == 0
    return report


def current_release(db):
    return db.one("SELECT * FROM releases WHERE status='active' ORDER BY id DESC LIMIT 1")


def init_releases(db, cfg):
    rel = current_release(db)
    if rel:
        return rel
    sha = head_sha(cfg["project_root"])
    path = ensure_release_tree(cfg, sha)
    health = healthcheck(cfg, path)
    if not health["healthy"]:
        raise RuntimeError("initial release is unhealthy: %s" % health)
    rid = db.insert("releases", {"sha": sha, "run_id": None, "policy": "initial", "deps_hash": deps_hash(path),
                                 "schema_version": SCHEMA_VERSION, "health": health, "status": "active",
                                 "previous_release_id": None, "path": path, "created_at": now(),
                                 "updated_at": now()})
    switch_active(cfg, path)
    return db.get("releases", rid)


def apply_sha(db, cfg, sha, run_id, policy):
    prev = current_release(db)
    path = ensure_release_tree(cfg, sha)
    health = healthcheck(cfg, path)
    if not health["healthy"]:
        raise RuntimeError("candidate release failed health check; active release unchanged: %s"
                           % health["problems"])
    rid = db.insert("releases", {"sha": sha, "run_id": run_id, "policy": policy, "deps_hash": deps_hash(path),
                                 "schema_version": SCHEMA_VERSION, "health": health, "status": "active",
                                 "previous_release_id": prev["id"] if prev else None, "path": path,
                                 "created_at": now(), "updated_at": now()})
    switch_active(cfg, path)
    if prev:
        db.update("releases", prev["id"], status="superseded", updated_at=now())
    return db.get("releases", rid)


def rollback(db, cfg, release_id, to_release_id=None):
    rel = db.get("releases", release_id)
    if not rel:
        raise ValueError("no release %s" % release_id)
    target_id = to_release_id or rel["previous_release_id"]
    if not target_id:
        raise ValueError("release %s has no previous release to roll back to" % release_id)
    target = db.get("releases", target_id)
    path = ensure_release_tree(cfg, target["sha"])
    checks = {"sha_matches": head_sha(path) == target["sha"],
              "deps_hash_matches": deps_hash(path) == target["deps_hash"],
              "state_schema_compatible": schema_compatible(target["schema_version"], db.schema_version())}
    health = healthcheck(cfg, path)
    checks["healthy"] = health["healthy"]
    if not all(checks.values()):
        raise RuntimeError("rollback target failed verification: %s" % checks)
    switch_active(cfg, path)
    db.update("releases", release_id, status="rolled_back", updated_at=now())
    db.update("releases", target_id, status="active", health=health, updated_at=now())
    checks["active_path"] = active_path(cfg)
    checks["health"] = health
    return checks
