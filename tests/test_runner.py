"""End-to-end tests on throwaway demo instances (offline, sandboxed)."""
import json
import os
import threading
import time
import unittest

from omnilaw_evolution import evaluator, runner, versions
from omnilaw_evolution.db import loads

from helpers import Instance


class Loop(unittest.TestCase):
    """Shared instance: good run -> READY -> apply -> rollback."""

    @classmethod
    def setUpClass(cls):
        cls.inst = Instance()
        cls.good, cls.contrast = cls.inst.ready()

    @classmethod
    def tearDownClass(cls):
        cls.inst.close()

    def run_proposal(self, pid, **kw):
        rid = runner.create_run(self.inst.db, self.inst.cfg, pid, **kw)
        return rid, runner.Runner(self.inst.db, self.inst.cfg, rid).execute()

    def test_1_good_hypothesis_ready_then_apply_and_rollback(self):
        db, cfg = self.inst.db, self.inst.cfg
        rid, state = self.run_proposal(self.good)
        self.assertEqual(state, "READY")
        run = db.get("runs", rid)
        ck = loads(run["checkpoint"])
        self.assertGreater(ck["holdout"]["candidate"]["metrics"]["f1"], ck["holdout"]["baseline"]["metrics"]["f1"])
        self.assertEqual(ck["holdout"]["candidate"]["metrics"]["false_deletions"], 0)
        # The patch touches exactly the target module.
        with open(run["patch_path"]) as f:
            patch = f.read()
        self.assertIn("+++ b/src/omnilaw_evolution/collector/dedup.py", patch)
        self.assertEqual(patch.count("+++ b/"), 1)
        # Same input snapshot -> rerun reproduces the raw result.
        again = evaluator.run_on_split(cfg, ck["patched"][ck["holdout"]["candidate"]["key"]]["path"], "holdout",
                                       os.path.join(self.inst.tmp, "rerun"))
        self.assertEqual(again["metrics"]["f1"], ck["holdout"]["candidate"]["metrics"]["f1"])

        base_rel = versions.current_release(db)
        rel = runner.apply_run(db, cfg, rid)
        self.assertEqual(versions.head_sha(versions.active_path(cfg)), run["candidate_sha"])
        self.assertTrue(loads(rel["health"])["healthy"])
        self.assertEqual(db.get("runs", rid)["state"], "APPLIED")
        with self.assertRaises(runner.RunError):
            runner.apply_run(db, cfg, rid)

        checks = runner.rollback_release(db, cfg, rel["id"])
        self.assertTrue(all(checks[k] for k in ("sha_matches", "deps_hash_matches", "state_schema_compatible",
                                                "healthy")))
        self.assertEqual(versions.head_sha(versions.active_path(cfg)), base_rel["sha"])
        self.assertEqual(db.get("runs", rid)["state"], "ROLLED_BACK")
        self.assertEqual(versions.current_release(db)["id"], base_rel["id"])

    def test_2_contrast_hypothesis_rejected(self):
        rid, state = self.run_proposal(self.contrast)
        self.assertEqual(state, "REJECTED")
        reasons = loads(self.inst.db.get("runs", rid)["decision"])["reasons"]
        self.assertIn("no variant passed dev screening", reasons[0])

    def test_3_scope_violation_rejected(self):
        rid, state = self.run_proposal(self.good, executor="redteam-scope")
        self.assertEqual(state, "REJECTED")
        reasons = " ".join(loads(self.inst.db.get("runs", rid)["decision"])["reasons"])
        self.assertIn("evaluator.py: protected path", reasons)
        self.assertIn("holdout.jsonl: protected path", reasons)
        # The stable eval data is untouched.
        with open(os.path.join(self.inst.cfg["eval_dir"], "datasets/dedup/holdout.jsonl")) as f:
            self.assertGreater(len(f.read()), 1000)

    def test_4_candidate_cannot_read_eval_or_use_network(self):
        if self.inst.cfg["sandbox"]["mode"] == "none" or evaluator.sandbox.detect_mode() == "none":
            self.skipTest("no OS sandbox available on this machine")
        rid, state = self.run_proposal(self.good, executor="redteam-exfil")
        ck = loads(self.inst.db.get("runs", rid)["checkpoint"])
        path = os.path.join(self.inst.cfg["experiments_dir"], "run-%d" % rid, "eval", "v0-dev", "per_item.json")
        with open(path) as f:
            reason = json.load(f)[0]["reason"]
        self.assertIn("'eval_read', 'blocked:", reason)
        self.assertIn("'network', 'blocked:", reason)
        self.assertNotIn("ALLOWED", reason)

    def test_5_candidate_cannot_see_eval_dir_in_checkout(self):
        path = versions.create_candidate(self.inst.cfg, 999, 0, self.inst.base_sha)
        self.assertFalse(os.path.exists(os.path.join(path, "eval")))
        self.assertTrue(os.path.exists(os.path.join(path, "src/omnilaw_evolution/collector/dedup.py")))

    def test_6_illegal_transition(self):
        rid = runner.create_run(self.inst.db, self.inst.cfg, self.good)
        with self.assertRaises(runner.RunError):
            runner.transition(self.inst.db, rid, "APPLIED")


class Interrupts(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.good, _ = self.inst.ready()

    def tearDown(self):
        self.inst.close()

    def test_timeout_ends_and_records(self):
        db, cfg = self.inst.db, self.inst.cfg
        rid = runner.create_run(db, cfg, self.good, executor="redteam-slow",
                                budget_overrides={"max_process_seconds": 1})
        t = time.time()
        state = runner.Runner(db, cfg, rid).execute()
        self.assertEqual(state, "REJECTED")
        self.assertLess(time.time() - t, 30)
        self.assertIn("timeout", " ".join(loads(db.get("runs", rid)["decision"])["reasons"]))

    def test_stop_cancels_and_resume_continues(self):
        db, cfg = self.inst.db, self.inst.cfg
        rid = runner.create_run(db, cfg, self.good, executor="redteam-slow",
                                budget_overrides={"max_process_seconds": 120})
        from omnilaw_evolution.db import DB
        th = threading.Thread(target=lambda: runner.Runner(DB(cfg.db_path), cfg, rid).execute())
        th.start()
        deadline = time.time() + 60
        while db.get("runs", rid)["state"] != "EVALUATING" and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        runner.request_stop(db, rid)
        th.join(30)
        self.assertFalse(th.is_alive())
        run = db.get("runs", rid)
        self.assertEqual(run["state"], "CANCELLED")
        self.assertIn("patched_done", loads(run["checkpoint"]))
        # Resume keeps cumulative usage and does not regenerate candidates.
        before = loads(run["usage"])["candidates"]
        db.update("runs", rid, budget=dict(loads(run["budget"]), max_process_seconds=1))
        state = runner.resume(db, cfg, rid)
        self.assertEqual(state, "REJECTED")
        self.assertEqual(loads(db.get("runs", rid)["usage"])["candidates"], before)

    def test_budget_exhausted_then_resume_with_extension(self):
        db, cfg = self.inst.db, self.inst.cfg
        rid = runner.create_run(db, cfg, self.good, budget_overrides={"max_candidates": 1})
        self.assertEqual(runner.Runner(db, cfg, rid).execute(), "BUDGET_EXHAUSTED")
        self.assertEqual(loads(db.get("runs", rid)["usage"])["candidates"], 1)
        with self.assertRaises(runner.RunError):
            runner.apply_run(db, cfg, rid)
        self.assertEqual(runner.resume(db, cfg, rid, extend={"max_candidates": 2}), "READY")
        self.assertEqual(loads(db.get("runs", rid)["usage"])["candidates"], 3)

    def test_wall_clock_budget(self):
        db, cfg = self.inst.db, self.inst.cfg
        rid = runner.create_run(db, cfg, self.good, budget_overrides={"max_wall_seconds": 0})
        self.assertEqual(runner.Runner(db, cfg, rid).execute(), "BUDGET_EXHAUSTED")

    def test_goal_lock(self):
        with runner.goal_lock(self.inst.cfg, "g"):
            with self.assertRaises(runner.RunError):
                with runner.goal_lock(self.inst.cfg, "g"):
                    pass

    def test_unhealthy_release_is_not_activated(self):
        db, cfg = self.inst.db, self.inst.cfg
        before = versions.active_path(cfg)
        path = versions.create_candidate(cfg, 500, 0, self.inst.base_sha)
        with open(os.path.join(path, cfg["target_module"]), "w") as f:
            f.write("def dedup(items):\n    return []\n")
        sha = versions.commit_candidate(path, cfg["writable_paths"], "broken")
        with self.assertRaises(RuntimeError):
            versions.apply_sha(db, cfg, sha, None, "manual")
        self.assertEqual(versions.active_path(cfg), before)

    def test_replanning_skips_tested_and_reports_open_problems(self):
        db, cfg = self.inst.db, self.inst.cfg
        rid = runner.create_run(db, cfg, self.good)
        runner.Runner(db, cfg, rid).execute()
        runner.apply_run(db, cfg, rid)
        runner.evaluate_release(db, cfg, "batch2")
        ids, problems, skipped = self.inst.propose()
        self.assertEqual(ids, [])
        self.assertIn(self.good, [s["proposal"] for s in skipped])
        self.assertEqual({p["case_type"] for p in problems}, {"paraphrase", "cross-language"})


if __name__ == "__main__":
    unittest.main()
