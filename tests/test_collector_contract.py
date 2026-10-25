"""Contract tests for the collector's dedup module. Protected: candidates
cannot edit tests/, and the evaluator runs this file against every candidate."""
import unittest

from omnilaw_evolution.collector.dedup import dedup
from omnilaw_evolution.collector.pipeline import collect

ITEMS = [
    {"id": "a", "url": "https://x.example/1", "title": "One", "text": "first story about databases", "lang": "en"},
    {"id": "b", "url": "https://x.example/1", "title": "One", "text": "first story about databases", "lang": "en"},
    {"id": "c", "url": "https://x.example/2", "title": "Two", "text": "a completely different topic: rockets",
     "lang": "en"},
    {"id": "d", "url": "https://y.example/3", "title": "三", "text": "第三条新闻，内容独立。", "lang": "zh"},
]


class DedupContract(unittest.TestCase):
    def test_one_decision_per_item_in_order(self):
        out = dedup([dict(i) for i in ITEMS])
        self.assertEqual([d["id"] for d in out], [i["id"] for i in ITEMS])

    def test_duplicate_points_to_earlier_kept_item(self):
        kept = set()
        for d in dedup([dict(i) for i in ITEMS]):
            if d["duplicate_of"] is None:
                kept.add(d["id"])
            else:
                self.assertIn(d["duplicate_of"], kept)

    def test_identical_url_removed_distinct_kept(self):
        out = {d["id"]: d["duplicate_of"] for d in dedup([dict(i) for i in ITEMS])}
        self.assertEqual(out["b"], "a")
        self.assertIsNone(out["a"])
        self.assertIsNone(out["c"])
        self.assertIsNone(out["d"])

    def test_empty_and_single(self):
        self.assertEqual(dedup([]), [])
        self.assertEqual(dedup([dict(ITEMS[0])])[0]["duplicate_of"], None)

    def test_does_not_mutate_input(self):
        items = [dict(i) for i in ITEMS]
        dedup(items)
        self.assertEqual(items, ITEMS)

    def test_pipeline_uses_module(self):
        result = collect([dict(i) for i in ITEMS])
        self.assertEqual(result["removed"], 1)


if __name__ == "__main__":
    unittest.main()
