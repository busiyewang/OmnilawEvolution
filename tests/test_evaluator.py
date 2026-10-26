import unittest

from omnilaw_evolution import evaluator
from omnilaw_evolution.eval_harness import evaluate_groups


def row(iid, cluster, critical=False):
    return {"id": iid, "label": {"cluster": cluster, "case": "x", "critical": critical}}


ROWS = [row("a", "c1", True), row("b", "c1"), row("c", "c2", True), row("d", "c3", True), row("e", "c3")]


def dec(**dups):
    return [{"id": r["id"], "duplicate_of": dups.get(r["id"])} for r in ROWS]


class Score(unittest.TestCase):
    def test_perfect(self):
        m, _ = evaluator.score(ROWS, dec(b="a", e="d"), 0.001)
        self.assertEqual((m["precision"], m["recall"], m["f1"], m["false_deletions"]), (1.0, 1.0, 1.0, 0))

    def test_baseline_like_misses(self):
        m, per = evaluator.score(ROWS, dec(), 0.001)
        self.assertEqual(m["recall"], 0.0)
        self.assertEqual(m["precision"], 1.0)
        self.assertEqual([p["outcome"] for p in per].count("missed-duplicate"), 2)

    def test_false_deletion_of_unique_critical_item(self):
        m, _ = evaluator.score(ROWS, dec(b="a", c="a", e="d"), 0.001)
        self.assertEqual(m["false_deletions"], 1)
        self.assertEqual(m["critical_lost"], ["c"])
        self.assertAlmostEqual(m["false_deletion_rate"], 1 / 3, places=3)
        self.assertAlmostEqual(m["precision"], 2 / 3, places=3)

    def test_wrong_link_is_not_true_positive(self):
        m, _ = evaluator.score(ROWS, dec(b="a", e="a"), 0.001)
        self.assertEqual(m["wrong_links"], 1)
        self.assertEqual(m["true_positive"], 1)

    def test_contract_failures(self):
        items = [{"id": r["id"]} for r in ROWS]
        self.assertEqual(evaluator.contract_failures(items, dec(b="a")), [])
        self.assertTrue(evaluator.contract_failures(items, dec(b="e")))      # points forward
        self.assertTrue(evaluator.contract_failures(items, dec()[:-1]))     # missing item

    def test_group_flattening_does_not_hide_a_non_list_return(self):
        with self.assertRaisesRegex(TypeError, "must return a list"):
            evaluate_groups(lambda items: tuple(dec()), [[{"id": "a"}]])


if __name__ == "__main__":
    unittest.main()
