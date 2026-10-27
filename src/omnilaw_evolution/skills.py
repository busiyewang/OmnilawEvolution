"""Hermes skills as a second evolution target (docs/SKILL_EVOLUTION.md).

The live skill directory of an agent identity (``<state>/agent/<identity>/skills``)
is the work tree of a separate Git directory (``skills.git`` next to it), so a
release is a commit, and a rollback is a revert. With ``skill_evolution.enabled``,
Hermes runs with ``skills.write_approval`` on: it stages each skill write under
``<HERMES_HOME>/pending/skills/<id>.json`` instead of saving it. ``collect`` turns
those records into candidates. ``prepare`` replays a candidate with Hermes' own
``apply_skill_pending`` on a copy of the released tree and commits the result.
Only an evaluated (READY) candidate can be applied, and only by a human.

    PENDING -> PREPARED | INVALID;  PREPARED -> EVALUATING -> READY | REJECTED
    READY -> APPLIED | STALE;  APPLIED -> ROLLED_BACK;  open states -> DISCARDED

Staged payloads are untrusted text: they are stored and replayed as data, never
executed. Replay runs in the sandbox without network.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

from . import sandbox
from .db import loads, now

OPEN = ("PENDING", "PREPARED", "EVALUATING", "READY")
TRANSITIONS = {
    "PENDING": {"PREPARED", "INVALID", "DISCARDED"},
    "PREPARED": {"EVALUATING", "DISCARDED", "PENDING"},
    "EVALUATING": {"READY", "REJECTED", "PREPARED", "DISCARDED"},
    "READY": {"APPLIED", "STALE", "DISCARDED"},
    "APPLIED": {"ROLLED_BACK"},
    "STALE": {"PENDING", "DISCARDED"},
}
# Hermes bookkeeping inside the skill directory; telemetry, not skill content.
GIT_EXCLUDE = [".usage.json", ".usage.json.*", ".curator_*", ".archive/", "*.lock", ".hub/", ".tmp*"]
SEED_MARKER = ".bundled-skills-seeded"
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
APPLY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes_skill_apply.py")


class SkillError(RuntimeError):
    pass


def identity(user="local", channel="cli"):
    return hashlib.sha256(json.dumps([user, channel]).encode()).hexdigest()[:20]


def enabled(cfg):
    return bool((cfg.get("skill_evolution") or {}).get("enabled"))


# ------------------------------------------------------------ repository
class SkillRepo:
    """Git history of one identity's skills, kept outside the skill tree."""

    def __init__(self, cfg, ident=None):
        self.cfg = cfg
        self.ident = ident or identity()
        self.shared = cfg.subdir("agent", self.ident)
        self.live = os.path.join(self.shared, "skills")
        self.git_dir = os.path.join(self.shared, "skills.git")

    def git(self, *args, work_tree=None, index=None, check=True, binary=False):
        env = dict(os.environ, GIT_AUTHOR_NAME="omnilaw", GIT_AUTHOR_EMAIL="omnilaw@localhost",
                   GIT_COMMITTER_NAME="omnilaw", GIT_COMMITTER_EMAIL="omnilaw@localhost")
        if index:
            env["GIT_INDEX_FILE"] = index
        cmd = ["git", "--git-dir", self.git_dir, "--work-tree", work_tree or self.live] + list(args)
        res = subprocess.run(cmd, capture_output=True, env=env)
        if check and res.returncode != 0:
            raise SkillError("git %s failed: %s" % (args[0], res.stderr.decode("utf-8", "replace").strip()))
        return res.stdout if binary else res.stdout.decode("utf-8", "replace").strip()

    def initialized(self):
        return os.path.isfile(os.path.join(self.git_dir, "HEAD"))

    def init(self):
        """Seed the bundled skills once (same marker as the Hermes runtime) and
        commit the tree as the first release. Idempotent."""
        os.makedirs(self.live, mode=0o700, exist_ok=True)
        seed_bundled(self.shared)
        if self.initialized():
            return self.head()
        subprocess.run(["git", "init", "--quiet", "--bare", self.git_dir], check=True, capture_output=True)
        self.git("config", "core.bare", "false")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        with open(os.path.join(self.git_dir, "info", "exclude"), "a", encoding="utf-8") as f:
            f.write("\n".join(GIT_EXCLUDE) + "\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "--allow-empty", "-m", "seed: skills at enablement")
        return self.head()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def tree(self, commit):
        return self.git("rev-parse", commit + "^{tree}")

    def drift(self):
        """Files in the live tree that differ from the released commit."""
        out = self.git("status", "--porcelain", "--untracked-files=all")
        return [line[3:] for line in out.splitlines() if line.strip()]

    def export(self, commit, dest):
        """Write the tree of ``commit`` into an empty directory."""
        os.makedirs(dest, exist_ok=True)
        data = self.git("archive", "--format=tar", commit, binary=True)
        with tempfile.TemporaryFile() as tmp:
            tmp.write(data)
            tmp.seek(0)
            with tarfile.open(fileobj=tmp) as tar:
                for member in tar.getmembers():
                    target = os.path.realpath(os.path.join(dest, member.name))
                    if not (member.isfile() or member.isdir()) or not target.startswith(os.path.realpath(dest)):
                        raise SkillError("unexpected entry in skill tree: %s" % member.name)
                tar.extractall(dest, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        return dest

    def commit_dir(self, work_dir, parent, message, ref):
        """Commit an arbitrary directory as a child of ``parent`` without touching
        the live tree or its index."""
        index = os.path.join(tempfile.mkdtemp(prefix="omnilaw-skill-index-"), "index")
        try:
            self.git("add", "-A", work_tree=work_dir, index=index)
            tree = self.git("write-tree", work_tree=work_dir, index=index)
        finally:
            shutil.rmtree(os.path.dirname(index), ignore_errors=True)
        sha = self.git("commit-tree", tree, "-p", parent, "-m", message)
        self.git("update-ref", ref, sha)
        return sha

    def skill_dirs(self, commit):
        files = self.git("ls-tree", "-r", "--name-only", commit).splitlines()
        return {os.path.dirname(f) for f in files if os.path.basename(f) == "SKILL.md"}

    def changed_skills(self, base, commit):
        paths = self.git("diff", "--name-only", base, commit).splitlines()
        dirs = self.skill_dirs(base) | self.skill_dirs(commit)
        out = set()
        for p in paths:
            parts = p.split("/")
            hit = next(("/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)
                        if "/".join(parts[:i]) in dirs), None)
            out.add(os.path.basename(hit) if hit else p)
        return sorted(out)

    def diff(self, base, commit):
        return self.git("diff", base, commit)

    def find_skill(self, commit, name):
        for d in self.skill_dirs(commit):
            if os.path.basename(d) == name:
                return d
        return None

    def fast_forward(self, commit):
        self.git("merge", "--quiet", "--ff-only", commit)
        return self.head()

    def revert(self, commit):
        self.git("revert", "--no-edit", commit)
        return self.head()


def bundled_skill_members():
    """Skill files in the pinned, hash-verified Hermes bundle (no Python 3.14 needed)."""
    with open(os.path.join(VENDOR, "hermes-manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    bundle = os.path.join(VENDOR, "hermes-core.tar.gz")
    h = hashlib.sha256()
    with open(bundle, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != manifest["sha256"]:
        raise SkillError("Hermes source bundle hash mismatch")
    with tarfile.open(bundle) as tar:
        for m in tar:
            if m.isfile() and m.name.startswith("skills/") and os.path.basename(m.name) != "AGENTS.md":
                yield m.name[len("skills/"):], tar.extractfile(m).read()


def seed_bundled(shared):
    """Copy the bundled skills into ``<shared>/skills`` once; never restores a skill
    the user or agent removed later. The Hermes runtime uses the same marker."""
    marker = os.path.join(shared, SEED_MARKER)
    if os.path.exists(marker):
        return False
    live = os.path.join(shared, "skills")
    for rel, data in bundled_skill_members():
        target = os.path.realpath(os.path.join(live, rel))
        if not target.startswith(os.path.realpath(live) + os.sep):
            raise SkillError("unexpected path in bundle: %s" % rel)
        if not os.path.exists(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
    with open(marker, "w") as f:
        f.write("bundle\n")
    return True


# ------------------------------------------------------------ candidates
def transition(db, cid, new, **fields):
    cand = db.get("skill_candidates", cid)
    if new not in TRANSITIONS.get(cand["state"], set()):
        raise SkillError("illegal transition %s -> %s for skill candidate %d" % (cand["state"], new, cid))
    db.update("skill_candidates", cid, state=new, updated_at=now(), **fields)
    db.event(None, "skill-candidate", {"id": cid, "from": cand["state"], "to": new})


def _payload_key(ident, payload):
    return hashlib.sha256(json.dumps([ident, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _duplicate(db, ident, payload):
    key = _payload_key(ident, payload)
    for row in db.all("SELECT id, identity, payload FROM skill_candidates WHERE state IN (%s)"
                      % ",".join("?" * len(OPEN)), OPEN):
        if _payload_key(row["identity"], loads(row["payload"])) == key:
            return row["id"]
    return None


def add_candidate(db, ident, origin, payload, summary, source_ref=None, note=None, targets=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("action"), str) or not payload.get("name"):
        raise SkillError("skill payload needs an action and a skill name")
    dup = _duplicate(db, ident, payload)
    if dup:
        return dup, False
    cid = db.insert("skill_candidates", {
        "identity": ident, "origin": origin, "source_ref": source_ref, "summary": (summary or "")[:500],
        "payload": payload, "note": note, "targets": list(targets or []), "state": "PENDING",
        "created_at": now(), "updated_at": now()})
    return cid, True


def collect(db, cfg):
    """Move every staged Hermes skill write into ``skill_candidates``."""
    root = cfg.subdir("agent")
    found = []
    for ident in sorted(os.listdir(root)):
        sessions = os.path.join(root, ident, "sessions")
        if not os.path.isdir(sessions):
            continue
        for sid in sorted(os.listdir(sessions)):
            pending = os.path.join(sessions, sid, "pending", "skills")
            if not os.path.isdir(pending):
                continue
            for name in sorted(os.listdir(pending)):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(pending, name)
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        record = json.load(f)
                    payload = record["payload"]
                    cid, new = add_candidate(db, ident, "hermes", payload, record.get("summary") or "",
                                             source_ref="%s/%s" % (sid[:12], record.get("id") or name[:-5]),
                                             note=record.get("origin"))
                    found.append({"candidate": cid, "new": new, "summary": record.get("summary")})
                except (OSError, ValueError, KeyError, TypeError, SkillError) as exc:
                    found.append({"file": path, "error": "%s: %s" % (type(exc).__name__, exc)})
                    continue
                os.remove(path)
    return found


def propose_file(db, cfg, name, content, category=None, note=None, targets=None, ident=None):
    """A human-submitted skill (create, or full rewrite of an existing one)."""
    repo = SkillRepo(cfg, ident)
    repo.init()
    exists = repo.find_skill(repo.head(), name) is not None
    payload = {"action": "edit" if exists else "create", "name": name, "content": content}
    if category and not exists:
        payload["category"] = category
    summary = "%s '%s' (%d chars)" % ("rewrite" if exists else "create", name, len(content))
    return add_candidate(db, repo.ident, "user", payload, summary, note=note, targets=targets)


def hermes_apply(cfg, payload, tree_dir, scratch):
    """Replay a staged write with the pinned Hermes ``apply_skill_pending`` on
    ``tree_dir``. Needs the agent runtime (Python 3.14 + .[agent])."""
    from .hermes_runtime import prepare_runtime

    source = str(prepare_runtime(cfg))
    home = os.path.join(scratch, "home")
    os.makedirs(home, exist_ok=True)
    os.symlink(os.path.realpath(tree_dir), os.path.join(home, "skills"))
    init = os.path.join(scratch, "apply.json")
    with open(init, "w", encoding="utf-8") as f:
        json.dump({"source": source, "payload": payload}, f, ensure_ascii=False)
    res = sandbox.run([sys.executable, APPLY_SCRIPT, init], scratch=os.path.join(scratch, "proc"),
                      allow_write=[home, tree_dir], deny_read=protected_reads(cfg), timeout=120,
                      cwd=scratch, mode=cfg["sandbox"]["mode"],
                      env_extra={"HERMES_HOME": home, "HOME": home, "PYTHONUTF8": "1"})
    lines = [l for l in res["stdout"].splitlines() if l.startswith("{")]
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        result = {"success": False, "error": "no result from skill replay (exit %s): %s"
                  % (res["returncode"], res["stderr"][-400:])}
    return result


def protected_reads(cfg):
    """What agent-side processes must not read: labelled tasks, live agent state,
    the state DB and credentials."""
    home = os.path.expanduser("~")
    return [cfg["eval_dir"], os.path.join(cfg["state_dir"], "agent"), cfg.db_path,
            cfg.db_path + "-wal", cfg.db_path + "-shm",
            os.path.join(home, ".config", "omnilaw-evolution"), os.path.join(home, ".ssh"),
            os.path.join(home, ".hermes")]


def prepare(db, cfg, cid, applier=None):
    """Build the candidate commit on top of the released head."""
    cand = db.get("skill_candidates", cid)
    if cand["state"] != "PENDING":
        raise SkillError("candidate %d is %s; only PENDING candidates are prepared" % (cid, cand["state"]))
    repo = SkillRepo(cfg, cand["identity"])
    repo.init()
    base = repo.head()
    work = os.path.join(cfg["experiments_dir"], "skill-%d" % cid)
    shutil.rmtree(work, ignore_errors=True)
    tree = repo.export(base, os.path.join(work, "tree"))
    result = (applier or hermes_apply)(cfg, loads(cand["payload"]), tree, work)
    if not result.get("success"):
        transition(db, cid, "INVALID", error=str(result.get("error") or result)[:2000])
        return db.get("skill_candidates", cid)
    sha = repo.commit_dir(tree, base, "candidate %d (%s): %s" % (cid, cand["origin"], cand["summary"][:200]),
                          "refs/candidates/%d" % cid)
    changed = repo.changed_skills(base, sha)
    if repo.tree(sha) == repo.tree(base):
        transition(db, cid, "INVALID", base_commit=base, error="replay changed nothing")
        return db.get("skill_candidates", cid)
    patch = os.path.join(work, "candidate.patch")
    with open(patch, "w", encoding="utf-8") as f:
        f.write(repo.diff(base, sha) + "\n")
    transition(db, cid, "PREPARED", base_commit=base, commit_sha=sha, changed=changed, patch_path=patch, error=None)
    return db.get("skill_candidates", cid)


def set_targets(db, cid, targets):
    cand = db.get("skill_candidates", cid)
    if cand["state"] not in OPEN:
        raise SkillError("candidate %d is %s" % (cid, cand["state"]))
    db.update("skill_candidates", cid, targets=list(targets), updated_at=now())


def discard(db, cid, reason="discarded by user"):
    transition(db, cid, "DISCARDED", error=reason)


# ------------------------------------------------------------ releases
def active_release(db, ident):
    return db.one("SELECT * FROM skill_releases WHERE identity=? AND status='active' ORDER BY id DESC LIMIT 1",
                  (ident,))


def ensure_initialized(db, cfg, ident=None):
    repo = SkillRepo(cfg, ident)
    head = repo.init()
    if not active_release(db, repo.ident):
        db.insert("skill_releases", {"identity": repo.ident, "commit_sha": head, "kind": "seed",
                                     "status": "active", "created_at": now()})
    return repo


def _require_clean(repo):
    drift = repo.drift()
    if drift:
        raise SkillError("live skills differ from the released version (%s); review with `skills status`"
                         % ", ".join(drift[:5]))


def apply_candidate(db, cfg, cid):
    cand = db.get("skill_candidates", cid)
    if cand["state"] != "READY":
        raise SkillError("candidate %d is %s; only READY candidates can be applied" % (cid, cand["state"]))
    repo = ensure_initialized(db, cfg, cand["identity"])
    _require_clean(repo)
    if repo.head() != cand["base_commit"]:
        transition(db, cid, "STALE", error="skills changed since evaluation; prepare and evaluate again")
        raise SkillError("candidate %d was evaluated against an older skill version; it is now STALE" % cid)
    head = repo.fast_forward(cand["commit_sha"])
    prev = active_release(db, repo.ident)
    rid = db.insert("skill_releases", {"identity": repo.ident, "commit_sha": head, "candidate_id": cid,
                                       "kind": "candidate", "previous_id": prev["id"] if prev else None,
                                       "status": "active", "created_at": now()})
    if prev:
        db.update("skill_releases", prev["id"], status="superseded")
    transition(db, cid, "APPLIED")
    return db.get("skill_releases", rid)


def rollback(db, cfg, ident=None):
    """Revert the active candidate release with a new commit."""
    repo = ensure_initialized(db, cfg, ident)
    rel = active_release(db, repo.ident)
    if not rel or rel["kind"] != "candidate":
        raise SkillError("the active skill release is not a candidate release; nothing to roll back")
    _require_clean(repo)
    head = repo.revert(rel["commit_sha"])
    rid = db.insert("skill_releases", {"identity": repo.ident, "commit_sha": head, "candidate_id": rel["candidate_id"],
                                       "kind": "rollback", "previous_id": rel["id"], "status": "active",
                                       "created_at": now()})
    db.update("skill_releases", rel["id"], status="rolled_back")
    transition(db, rel["candidate_id"], "ROLLED_BACK")
    return db.get("skill_releases", rid)


def status(db, cfg, ident=None):
    repo = SkillRepo(cfg, ident)
    out = {"enabled": enabled(cfg), "identity": repo.ident, "initialized": repo.initialized()}
    if repo.initialized():
        out.update(head=repo.head()[:12], skills=len(repo.skill_dirs("HEAD")), drift=repo.drift())
        rel = active_release(db, repo.ident)
        out["release"] = {k: rel[k] for k in ("id", "kind", "candidate_id", "created_at")} if rel else None
    out["candidates"] = db.all("SELECT id, origin, state, summary, changed, targets, error FROM skill_candidates "
                               "WHERE identity=? ORDER BY id DESC LIMIT 20", (repo.ident,))
    return out
