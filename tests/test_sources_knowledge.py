import os
import unittest
import urllib.request
from unittest import mock

from omnilaw_evolution import knowledge, sources
from omnilaw_evolution.budget import Budget, BudgetExhausted
from omnilaw_evolution.db import loads
from omnilaw_evolution.llm import MockLLM
from omnilaw_evolution.schema import KNOWLEDGE_SCHEMA, SchemaError, validate

from helpers import Instance


class Sources(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()

    def tearDown(self):
        self.inst.close()

    def test_snapshot_ingest_records_provenance_and_dedups_by_hash(self):
        first = self.inst.ingest()
        self.assertEqual(len(first), 4)
        self.assertTrue(all(created for _, created in first))
        again = self.inst.ingest()
        self.assertEqual([sid for sid, _ in again], [sid for sid, _ in first])
        self.assertFalse(any(created for _, created in again))
        kinds = {s["kind"]: s for s in self.inst.db.all("SELECT * FROM sources")}
        self.assertEqual(kinds["github"]["revision"], "c7eaaeea3f39909f906a9cf8ad4d21a878cb4e07")
        self.assertEqual(kinds["github"]["license"], "MIT")
        self.assertEqual(kinds["paper"]["read_status"], "excerpt")

    def test_injection_page_is_flagged_and_inert(self):
        with open(self.inst.cfg_path, "rb") as f:
            cfg_before = f.read()
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("no network allowed")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no process allowed")), \
                mock.patch("os.system", side_effect=AssertionError("no shell allowed")):
            self.inst.ingest()
            ids, _, _ = self.inst.propose()
        src = self.inst.db.one("SELECT * FROM sources WHERE url LIKE '%attacker%'")
        kinds = {f["kind"] for f in loads(src["flags"])}
        self.assertTrue({"override-instructions", "pipe-to-shell", "destructive-command",
                         "config-or-secret-reference"} <= kinds)
        self.assertEqual(self.inst.db.one("SELECT COUNT(*) AS n FROM knowledge WHERE source_id=?",
                                          (src["id"],))["n"], 0)
        with open(self.inst.cfg_path, "rb") as f:
            self.assertEqual(f.read(), cfg_before)
        self.assertEqual(self.inst.cfg["writable_paths"], ["src/omnilaw_evolution/collector/dedup.py"])
        for pid in ids:
            p = self.inst.db.get("proposals", pid)
            self.assertNotIn(src["id"], [self.inst.db.get("knowledge", k)["source_id"]
                                         for k in loads(p["knowledge_ids"])])

    def test_classify(self):
        self.assertEqual(sources.classify("https://github.com/a/b/blob/main/x.py"), "github")
        self.assertEqual(sources.classify("https://arxiv.org/abs/2107.06499v2"), "paper")
        self.assertEqual(sources.classify("https://example.com/post"), "web")

    def test_download_budget_enforced(self):
        f = sources.Fetcher(self.inst.cfg, Budget({"max_download_bytes": 10}))
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"x" * 100
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(BudgetExhausted):
                f.get("https://example.com/big")

    def test_fetch_size_cap(self):
        self.inst.cfg["fetch"]["max_bytes"] = 50
        f = sources.Fetcher(self.inst.cfg)
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"x" * 51
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(ValueError):
                f.get("https://example.com/huge")


class Knowledge(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.inst.ingest()

    def tearDown(self):
        self.inst.close()

    def test_extraction_cites_exact_location(self):
        self.inst.propose()
        for k in self.inst.db.all("SELECT * FROM knowledge"):
            src = self.inst.db.get("sources", k["source_id"])
            with open(src["path"], encoding="utf-8") as f:
                text = f.read()
            start, end = [int(x) for x in k["locator"].split()[1].split("-")]
            self.assertEqual(text[start:end], k["excerpt"])
        methods = {k["method"] for k in self.inst.db.all("SELECT method FROM knowledge")}
        self.assertEqual(methods, {"url_canonicalization", "shingle_jaccard", "minhash_lsh"})

    def test_quote_not_in_source_is_dropped(self):
        class Liar(MockLLM):
            def extract_knowledge(self, source, text):
                return [{"method": "made_up", "claim": "c", "steps": "s", "conditions": "c", "limitations": "l",
                         "quote": "this sentence does not appear anywhere", "applicable": True}]
        src = self.inst.db.one("SELECT * FROM sources WHERE kind='web' ORDER BY id")
        self.assertEqual(knowledge.extract_for_source(self.inst.db, Liar(), src), [])

    def test_malformed_model_output_rejected(self):
        with self.assertRaises(SchemaError):
            validate({"items": [{"method": "x"}]}, KNOWLEDGE_SCHEMA)
        with self.assertRaises(SchemaError):
            validate({"items": [], "run_command": "rm -rf /"}, KNOWLEDGE_SCHEMA)

    def test_verification_never_downgrades(self):
        self.inst.propose()
        kid = self.inst.db.one("SELECT id FROM knowledge")["id"]
        knowledge.mark_verification(self.inst.db, [kid], "local_repro_pass", "run-1")
        knowledge.mark_verification(self.inst.db, [kid], "local_repro_fail", "run-2")
        row = self.inst.db.get("knowledge", kid)
        self.assertEqual(row["verification"], "local_repro_pass")
        self.assertEqual(loads(row["experiments"]), ["run-1", "run-2"])

    def test_model_call_budget_stops_extraction(self):
        with self.assertRaises(BudgetExhausted):
            self.inst.propose(Budget({"max_model_calls": 1}))


if __name__ == "__main__":
    unittest.main()
