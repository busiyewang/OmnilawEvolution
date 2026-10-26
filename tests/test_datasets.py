"""Frozen public data, label boundaries, reproducibility and evaluation integrity."""
import collections
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from omnilaw_evolution import evaluator
from omnilaw_evolution.config import load_config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("import_pawsx", ROOT / "scripts/import_pawsx.py")
pawsx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pawsx)


class PublicDataset(unittest.TestCase):
    def test_balanced_pairs_and_no_cross_split_source_sentence_leakage(self):
        seen_texts, seen_pairs = set(), set()
        for split in ("dev", "holdout", "batch2"):
            pairs = pawsx.load_snapshot(split)
            self.assertEqual(len(pairs), 120)
            counts = collections.Counter((p["languages"], p["label"]) for p in pairs)
            self.assertEqual(set(counts), {(m, label) for m in pawsx.MODES for label in (0, 1)})
            self.assertEqual(set(counts.values()), {15})
            for pair in pairs:
                key = (pair["source_split"], pair["source_line"])
                self.assertNotIn(key, seen_pairs)
                texts = {pawsx.normalise(t) for lang in ("en", "zh") for t in pair[lang]}
                self.assertFalse(texts & seen_texts)
                self.assertFalse(texts & {"", "ns"})
                seen_pairs.add(key)
                seen_texts.update(texts)

    def test_public_labels_have_provenance_and_isolated_pair_contracts(self):
        for split in ("dev", "holdout", "batch2"):
            rows = evaluator.load_split(str(ROOT / "eval"), split)
            self.assertGreaterEqual(len(rows), 250)
            public = [r for r in rows if "source" in r["label"]]
            self.assertEqual(len(public), 240)
            self.assertEqual(len({r["url"] for r in public}), 240)
            cases = collections.Counter(r["label"]["case"] for r in public)
            self.assertEqual(cases["paraphrase"], 30)
            self.assertEqual(cases["cross-language"], 30)
            self.assertEqual(cases["hard-negative"], 60)
            for first, second in zip(public[::2], public[1::2]):
                a, b = first["label"], second["label"]
                self.assertEqual(a["group"], b["group"])
                self.assertEqual(a["source"]["line"], b["source"]["line"])
                self.assertEqual(a["cluster"] == b["cluster"], not b["critical"])
                self.assertEqual(a["source"]["revision"], pawsx.REVISION)

    def test_offline_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, str(ROOT / "scripts/build_datasets.py"), "--out", tmp],
                           check=True, capture_output=True)
            for name in ("dev.jsonl", "holdout.jsonl", "batch2.jsonl", "MANIFEST.json"):
                self.assertEqual((Path(tmp) / name).read_bytes(),
                                 (ROOT / "eval/datasets/dedup" / name).read_bytes())

    def test_data_change_without_manifest_update_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "datasets/dedup"
            subprocess.run([sys.executable, str(ROOT / "scripts/build_datasets.py"),
                            "--profile", "demo", "--out", str(dest)], check=True, capture_output=True)
            with (dest / "holdout.jsonl").open("ab") as f:
                f.write(b"\n")
            for call in (lambda: evaluator.evaluator_version(tmp),
                         lambda: evaluator.load_split(tmp, "holdout"),
                         lambda: evaluator.split_sha(tmp, "holdout")):
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    call()

    def test_grouped_harness_hides_labels_and_resets_each_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src/omnilaw_evolution/collector"
            package.mkdir(parents=True)
            (package.parent / "__init__.py").write_text("")
            (package / "__init__.py").write_text("")
            (package / "dedup.py").write_text(
                "def dedup(items):\n"
                "    assert all(set(i) == {'id', 'url', 'title', 'text', 'lang', 'published'} for i in items)\n"
                "    return [{'id': i['id'], 'duplicate_of': items[0]['id'] if n else None, 'reason': 'test'}\n"
                "            for n, i in enumerate(items)]\n")
            cfg = load_config(overrides={"project_root": tmp, "state_dir": str(root / "state"),
                                         "eval_dir": str(ROOT / "eval")})
            scratch = root / "scratch"
            result = evaluator.run_on_split(cfg, tmp, "dev", str(scratch))
            self.assertTrue(result["ok"], result)
            public = [i for i in result["per_item"] if i["id"].startswith("pawsx-")]
            self.assertEqual(len(public), 240)
            for first, second in zip(public[::2], public[1::2]):
                self.assertIsNone(first["duplicate_of"])
                self.assertEqual(second["duplicate_of"], first["id"])
            payload = json.loads((scratch / "items.json").read_text())
            self.assertEqual(len(payload["groups"]), 121)

    def test_candidate_cannot_link_to_another_evaluation_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src/omnilaw_evolution/collector"
            package.mkdir(parents=True)
            (package.parent / "__init__.py").write_text("")
            (package / "__init__.py").write_text("")
            (package / "dedup.py").write_text(
                "first = None\n"
                "def dedup(items):\n"
                "    global first\n"
                "    if items and first is None: first = items[0]['id']\n"
                "    return [{'id': i['id'], 'duplicate_of': None if i['id'] == first else first}\n"
                "            for i in items]\n")
            cfg = load_config(overrides={"project_root": tmp, "state_dir": str(root / "state"),
                                         "eval_dir": str(ROOT / "eval")})
            result = evaluator.run_on_split(cfg, tmp, "dev", str(root / "scratch"))
            self.assertFalse(result["ok"])
            self.assertIsNone(result["metrics"])
            self.assertTrue(any("not an earlier kept item" in failure
                                for failure in result["regression"]["failures"]))


if __name__ == "__main__":
    unittest.main()
