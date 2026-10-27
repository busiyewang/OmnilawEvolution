"""Skill history, staged-write collection, candidate prepare / apply / rollback.

Replay uses a fake applier except in the native test, which runs the pinned
Hermes ``apply_skill_pending`` (Python 3.14 + .[agent]).
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

from omnilaw_evolution import skills
from omnilaw_evolution.config import load_config
from omnilaw_evolution.db import DB

AGENT = sys.version_info >= (3, 14) and importlib.util.find_spec("yaml") is not None


def write_skill(tree, name, body, category="custom"):
    d = os.path.join(tree, category, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: %s\ndescription: test skill\n---\n\n%s\n" % (name, body))


def fake_applier(cfg, payload, tree, scratch):
    if payload["action"] == "noop":
        return {"success": True}
    if payload["action"] == "fail":
        return {"success": False, "error": "rejected by linter"}
    write_skill(tree, payload["name"], payload.get("content", "body"))
    return {"success": True}


class SkillTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="omnilaw-skills-test-"))
        self.cfg = load_config(overrides={"project_root": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
                                          "experiments_dir": os.path.join(self.tmp, "exp"),
                                          "skill_evolution": {"enabled": True}})
        self.db = DB(self.cfg.db_path)
        self.repo = skills.ensure_initialized(self.db, self.cfg)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def candidate(self, name="probe", action="create", content="v1"):
        cid, _ = skills.add_candidate(self.db, self.repo.ident, "user",
                                      {"action": action, "name": name, "content": content}, "test " + name)
        return skills.prepare(self.db, self.cfg, cid, applier=fake_applier)

    def make_ready(self, cid):
        skills.transition(self.db, cid, "EVALUATING")
        skills.transition(self.db, cid, "READY")


class RepoTests(SkillTestBase):
    def test_init_seeds_bundled_skills_once_and_records_release(self):
        self.assertGreater(len(self.repo.skill_dirs("HEAD")), 40)
        self.assertEqual(self.repo.drift(), [])
        self.assertEqual(skills.active_release(self.db, self.repo.ident)["kind"], "seed")
        removed = sorted(self.repo.skill_dirs("HEAD"))[0]
        shutil.rmtree(os.path.join(self.repo.live, removed))
        skills.ensure_initialized(self.db, self.cfg)
        self.assertFalse(os.path.exists(os.path.join(self.repo.live, removed)))  # not restored
        self.assertTrue(any(p.startswith(removed) for p in self.repo.drift()))

    def test_hermes_telemetry_is_not_drift(self):
        with open(os.path.join(self.repo.live, ".usage.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(self.repo.live, ".curator_ledger.jsonl"), "w") as f:
            f.write("{}\n")
        self.assertEqual(self.repo.drift(), [])


class CandidateTests(SkillTestBase):
    def test_collect_moves_staged_writes_once(self):
        pending = os.path.join(self.repo.shared, "sessions", "s1", "pending", "skills")
        os.makedirs(pending)
        record = {"id": "ab12cd34", "summary": "create 'probe'", "origin": "foreground",
                  "payload": {"action": "create", "name": "probe", "content": "x"}}
        for name in ("ab12cd34.json", "dup.json"):
            with open(os.path.join(pending, name), "w") as f:
                json.dump(record, f)
        with open(os.path.join(pending, "broken.json"), "w") as f:
            f.write("{not json")
        out = skills.collect(self.db, self.cfg)
        self.assertEqual([o.get("new") for o in out if "candidate" in o], [True, False])
        self.assertTrue(any("error" in o for o in out))
        self.assertEqual(sorted(os.listdir(pending)), ["broken.json"])  # unreadable record is kept
        rows = self.db.all("SELECT origin, state, source_ref FROM skill_candidates")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "PENDING")
        self.assertTrue(rows[0]["source_ref"].endswith("/ab12cd34"))

    def test_prepare_commits_on_top_of_release_without_touching_live_tree(self):
        cand = self.candidate()
        self.assertEqual(cand["state"], "PREPARED")
        self.assertEqual(json.loads(cand["changed"]), ["probe"])
        self.assertEqual(cand["base_commit"], self.repo.head())
        with open(cand["patch_path"]) as f:
            self.assertIn("+v1", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.repo.live, "custom", "probe")))
        self.assertEqual(self.repo.drift(), [])

    def test_invalid_and_noop_replays(self):
        self.assertEqual(self.candidate(action="fail")["state"], "INVALID")
        noop = self.candidate(name="other", action="noop")
        self.assertEqual(noop["state"], "INVALID")
        self.assertIn("nothing", noop["error"])

    def test_apply_needs_ready_clean_tree_and_current_base(self):
        a, b = self.candidate("alpha")["id"], self.candidate("beta")["id"]
        with self.assertRaises(skills.SkillError):
            skills.apply_candidate(self.db, self.cfg, a)  # PREPARED, not evaluated
        self.make_ready(a)
        self.make_ready(b)
        stray = os.path.join(self.repo.live, "stray.txt")
        with open(stray, "w") as f:
            f.write("hand edit")
        with self.assertRaises(skills.SkillError):
            skills.apply_candidate(self.db, self.cfg, a)
        os.remove(stray)
        rel = skills.apply_candidate(self.db, self.cfg, a)
        self.assertEqual(rel["kind"], "candidate")
        self.assertTrue(os.path.isfile(os.path.join(self.repo.live, "custom", "alpha", "SKILL.md")))
        with self.assertRaises(skills.SkillError):
            skills.apply_candidate(self.db, self.cfg, b)  # evaluated against the old head
        self.assertEqual(self.db.get("skill_candidates", b)["state"], "STALE")

    def test_rollback_reverts_with_a_new_commit(self):
        a = self.candidate("alpha")["id"]
        self.make_ready(a)
        applied = skills.apply_candidate(self.db, self.cfg, a)
        rel = skills.rollback(self.db, self.cfg)
        self.assertEqual(rel["kind"], "rollback")
        self.assertNotEqual(rel["commit_sha"], applied["commit_sha"])
        self.assertFalse(os.path.exists(os.path.join(self.repo.live, "custom", "alpha")))
        self.assertEqual(self.db.get("skill_candidates", a)["state"], "ROLLED_BACK")
        self.assertEqual(self.db.get("skill_releases", applied["id"])["status"], "rolled_back")
        with self.assertRaises(skills.SkillError):
            skills.rollback(self.db, self.cfg)

    def test_propose_file_creates_or_rewrites(self):
        existing = os.path.basename(sorted(self.repo.skill_dirs("HEAD"))[0])
        c1, _ = skills.propose_file(self.db, self.cfg, existing, "---\nname: x\n---\nnew")
        c2, _ = skills.propose_file(self.db, self.cfg, "brand-new", "---\nname: y\n---\nbody", category="custom")
        self.assertEqual(json.loads(self.db.get("skill_candidates", c1)["payload"])["action"], "edit")
        p2 = json.loads(self.db.get("skill_candidates", c2)["payload"])
        self.assertEqual((p2["action"], p2["category"]), ("create", "custom"))


@unittest.skipUnless(AGENT, "install .[agent] with Python 3.14 for native skill replay")
class NativeReplayTests(SkillTestBase):
    def test_hermes_replays_a_staged_create_in_the_sandbox(self):
        content = ("---\nname: tidy-csv\ndescription: Normalise CSV files before analysis.\n---\n\n"
                   "# Tidy CSV\n\n## When to Use\n\n- A CSV has inconsistent headers.\n\n## Steps\n\n"
                   "1. Read the header row.\n2. Lower-case and strip names.\n")
        cid, _ = skills.add_candidate(self.db, self.repo.ident, "hermes",
                                      {"action": "create", "name": "tidy-csv", "category": "data",
                                       "content": content}, "create 'tidy-csv'")
        cand = skills.prepare(self.db, self.cfg, cid)
        self.assertEqual(cand["state"], "PREPARED", cand["error"])
        self.assertEqual(json.loads(cand["changed"]), ["tidy-csv"])
        with open(cand["patch_path"]) as f:
            self.assertIn("Lower-case", f.read())
        self.assertEqual(self.repo.drift(), [])


if __name__ == "__main__":
    unittest.main()
