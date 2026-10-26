import os
import shutil
import subprocess
import tempfile
import unittest

from omnilaw_evolution import policy
from omnilaw_evolution.config import DEFAULTS
from omnilaw_evolution.versions import git

WRITABLE = DEFAULTS["writable_paths"]
PROTECTED = DEFAULTS["protected_paths"]
TARGET = "src/omnilaw_evolution/collector/dedup.py"


class ScopeGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for p in (TARGET, "src/omnilaw_evolution/evaluator.py", "eval/datasets/dedup/holdout.jsonl",
                  "tests/test_collector_contract.py"):
            os.makedirs(os.path.join(self.d, os.path.dirname(p)), exist_ok=True)
            with open(os.path.join(self.d, p), "w") as f:
                f.write("x\n")
        git(self.d, "init", "-q")
        git(self.d, "add", "-A")
        git(self.d, "commit", "-qm", "base")
        self.base = git(self.d, "rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, path, text="y\n"):
        full = os.path.join(self.d, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def check(self):
        return policy.check_scope(self.d, self.base, WRITABLE, PROTECTED)

    def test_target_only_is_allowed(self):
        self.write(TARGET)
        self.assertEqual(self.check(), [])

    def test_committed_target_change_is_allowed(self):
        self.write(TARGET)
        git(self.d, "commit", "-qam", "cand")
        self.assertEqual(self.check(), [])

    def test_evaluator_edit_rejected(self):
        self.write(TARGET)
        self.write("src/omnilaw_evolution/evaluator.py")
        reasons = {v["path"]: v["reason"] for v in self.check()}
        self.assertEqual(reasons, {"src/omnilaw_evolution/evaluator.py": "protected path"})

    def test_holdout_and_tests_rejected(self):
        self.write("eval/datasets/dedup/holdout.jsonl", "")
        os.remove(os.path.join(self.d, "tests/test_collector_contract.py"))
        reasons = {v["path"]: v["reason"] for v in self.check()}
        self.assertEqual(reasons["eval/datasets/dedup/holdout.jsonl"], "protected path")
        self.assertEqual(reasons["tests/test_collector_contract.py"], "deletion not allowed")

    def test_new_file_outside_scope_rejected(self):
        self.write("src/omnilaw_evolution/collector/helper.py")
        self.assertEqual(self.check()[0]["reason"], "outside writable scope")

    def test_ignored_file_cannot_hide(self):
        self.write(".gitignore", "secret/\n")
        self.write("secret/x.py")
        paths = {v["path"] for v in self.check()}
        self.assertIn("secret/x.py", paths)

    def test_symlink_rejected(self):
        os.remove(os.path.join(self.d, TARGET))
        os.symlink("/etc/hosts", os.path.join(self.d, TARGET))
        self.assertIn("symlink not allowed", [v["reason"] for v in self.check()])

    def test_no_change_rejected(self):
        self.assertEqual(self.check()[0]["reason"], "no change produced")


class Decide(unittest.TestCase):
    CRIT = DEFAULTS["criteria"]
    BASE = {"f1": 0.2, "false_deletion_rate": 0.0, "critical_lost": [], "ms_per_item": 0.01}

    def cand(self, **kw):
        m = dict(self.BASE, f1=0.9)
        m.update(kw)
        return m

    def test_ready_when_all_conditions_hold(self):
        ok, _ = policy.decide(self.BASE, self.cand(), self.CRIT, {"passed": True})
        self.assertTrue(ok)

    def test_reject_small_gain(self):
        ok, reasons = policy.decide(self.BASE, self.cand(f1=0.22), self.CRIT, {"passed": True})
        self.assertFalse(ok)
        self.assertIn("delta", reasons[0])

    def test_reject_false_deletions_even_with_gain(self):
        ok, _ = policy.decide(self.BASE, self.cand(false_deletion_rate=0.2), self.CRIT, {"passed": True})
        self.assertFalse(ok)

    def test_reject_critical_loss(self):
        ok, _ = policy.decide(self.BASE, self.cand(critical_lost=["x"]), self.CRIT, {"passed": True})
        self.assertFalse(ok)

    def test_reject_regression_failure(self):
        ok, _ = policy.decide(self.BASE, self.cand(), self.CRIT, {"passed": False, "failures": ["t"]})
        self.assertFalse(ok)

    def test_reject_too_slow(self):
        ok, _ = policy.decide(self.BASE, self.cand(ms_per_item=500), self.CRIT, {"passed": True})
        self.assertFalse(ok)

    def test_high_baseline_can_improve_without_impossible_absolute_gain(self):
        base = dict(self.BASE, f1=0.9565)
        ok, reasons = policy.decide(base, self.cand(f1=1.0), self.CRIT, {"passed": True})
        self.assertTrue(ok)
        self.assertIn("100.00%", reasons[1])

    def test_relative_route_threshold_and_zero_deletions(self):
        base = dict(self.BASE, f1=0.96)
        for score, deletions, accepted in [(0.98, 0, True), (0.979, 0, False), (0.99, 1, False)]:
            with self.subTest(score=score, deletions=deletions):
                cand = self.cand(f1=score, false_deletions=deletions, false_deletion_rate=deletions / 100)
                self.assertEqual(policy.decide(base, cand, self.CRIT, {"passed": True})[0], accepted)

    def test_perfect_unchanged_or_regressed_baselines_are_not_improvements(self):
        for before, after in [(1.0, 1.0), (0.96, 0.96), (0.96, 0.95)]:
            with self.subTest(before=before, after=after):
                self.assertFalse(policy.decide(dict(self.BASE, f1=before), self.cand(f1=after),
                                               self.CRIT, {"passed": True})[0])

    def test_relative_route_preserves_all_safety_checks(self):
        for changes, regression in [({"critical_lost": ["x"]}, {"passed": True}),
                                     ({"ms_per_item": 100}, {"passed": True}),
                                     ({}, {"passed": False, "failures": ["contract"]})]:
            self.assertFalse(policy.decide(dict(self.BASE, f1=0.96), self.cand(f1=1.0, **changes),
                                           self.CRIT, regression)[0])

    def test_legacy_or_disabled_relative_criteria_keep_absolute_rule(self):
        for crit in [{k: v for k, v in self.CRIT.items() if k != "min_relative_error_reduction"},
                     dict(self.CRIT, min_relative_error_reduction=None)]:
            self.assertFalse(policy.decide(dict(self.BASE, f1=0.9565), self.cand(f1=1.0),
                                           crit, {"passed": True})[0])


if __name__ == "__main__":
    unittest.main()
