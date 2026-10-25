"""Phase 2 unit tests: migrations, queue, cron, ledger, discovery, review, notify."""
import datetime as dt
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from omnilaw_evolution import db as dbmod
from omnilaw_evolution import review, taskqueue
from omnilaw_evolution.budget import Budget, DailyLedger
from omnilaw_evolution.config import load_config
from omnilaw_evolution.db import DB, loads
from omnilaw_evolution.discovery import SnapshotProvider, candidate_key, discover_incremental, relevance
from omnilaw_evolution.notify import FeishuWebhookNotifier, Notifier, feishu_sign

from helpers import Instance


def tmpcfg(**extra):
    return load_config(None, dict({"state_dir": tempfile.mkdtemp(), "project_root": tempfile.mkdtemp()}, **extra))


class Migration(unittest.TestCase):
    def test_v1_database_is_upgraded_additively(self):
        path = os.path.join(tempfile.mkdtemp(), "old.sqlite3")
        conn = sqlite3.connect(path)
        conn.executescript(dbmod.SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO sources(kind,url,fetched_at,content_hash,read_status,path) "
                     "VALUES ('web','u','t','h','fulltext','p')")
        conn.commit()
        conn.close()
        db = DB(path)
        self.assertEqual(db.schema_version(), 2)
        self.assertEqual(db.one("SELECT url FROM sources")["url"], "u")  # data kept
        self.assertIsNone(db.one("SELECT supersedes FROM sources")["supersedes"])
        DB(path)  # idempotent
        self.assertTrue(dbmod.schema_compatible(1, 2))
        self.assertFalse(dbmod.schema_compatible(3, 2))


class Queue(unittest.TestCase):
    def setUp(self):
        self.db = DB(os.path.join(tempfile.mkdtemp(), "q.sqlite3"))

    def test_dedupe_priority_and_not_before(self):
        a, created = taskqueue.enqueue(self.db, "research", {"topic": "x"}, dedupe_key="k")
        b, created2 = taskqueue.enqueue(self.db, "research", {"topic": "x"}, dedupe_key="k")
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(a, b)
        later = (dt.datetime.now() + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        c, _ = taskqueue.enqueue(self.db, "propose", priority=10, not_before=later)
        d, _ = taskqueue.enqueue(self.db, "propose", priority=20)
        self.assertEqual(taskqueue.claim(self.db)["id"], d)          # c not yet due
        self.assertEqual(taskqueue.claim(self.db)["id"], a)
        self.assertIsNone(taskqueue.claim(self.db))
        self.assertEqual(taskqueue.claim(self.db, dt.datetime.now() + dt.timedelta(hours=2))["id"], c)

    def test_retry_then_fail(self):
        t, _ = taskqueue.enqueue(self.db, "propose", max_attempts=2)
        task = taskqueue.claim(self.db)
        self.assertEqual(taskqueue.retry_or_fail(self.db, task, "boom", backoff_seconds=0), "queued")
        task = taskqueue.claim(self.db, dt.datetime.now() + dt.timedelta(seconds=1))
        self.assertEqual(taskqueue.retry_or_fail(self.db, task, "boom"), "failed")
        self.assertEqual(self.db.get("tasks", t)["status"], "failed")

    def test_cancel_and_recover(self):
        t1, _ = taskqueue.enqueue(self.db, "propose")
        self.assertEqual(taskqueue.cancel(self.db, t1), "cancelled")
        t2, _ = taskqueue.enqueue(self.db, "propose")
        taskqueue.claim(self.db)
        self.assertEqual(taskqueue.cancel(self.db, t2), "cancel_requested")
        self.assertEqual(taskqueue.recover_stale(self.db), 1)

    def test_unknown_kind(self):
        with self.assertRaises(taskqueue.QueueError):
            taskqueue.enqueue(self.db, "rm -rf")


class Cron(unittest.TestCase):
    def test_next_fire(self):
        t = dt.datetime(2026, 10, 25, 7, 30)  # a Sunday
        self.assertEqual(taskqueue.next_fire("0 8 * * *", t), dt.datetime(2026, 10, 25, 8, 0))
        self.assertEqual(taskqueue.next_fire("*/15 * * * *", t), dt.datetime(2026, 10, 25, 7, 45))
        self.assertEqual(taskqueue.next_fire("0 9 * * 1", t), dt.datetime(2026, 10, 26, 9, 0))  # Monday
        self.assertEqual(taskqueue.next_fire("0 0 1 * *", t), dt.datetime(2026, 11, 1, 0, 0))
        for bad in ("* * * *", "61 * * * *", "0 25 * * *"):
            with self.assertRaises(ValueError):
                taskqueue.parse_cron(bad)

    def test_tick_fires_once_per_due_time(self):
        db = DB(os.path.join(tempfile.mkdtemp(), "c.sqlite3"))
        taskqueue.add_schedule(db, "daily", "0 8 * * *", "evaluate_release", {"split": "batch2"})
        db.execute("UPDATE schedules SET created_at='2026-10-25T07:00:00'")
        self.assertEqual(taskqueue.tick(db, dt.datetime(2026, 10, 25, 7, 59)), [])
        fired = taskqueue.tick(db, dt.datetime(2026, 10, 25, 8, 0))
        self.assertEqual(len(fired), 1)
        self.assertEqual(taskqueue.tick(db, dt.datetime(2026, 10, 25, 12, 0)), [])
        self.assertEqual(len(taskqueue.tick(db, dt.datetime(2026, 10, 26, 8, 0))), 1)
        # live dedupe: the second firing found the first task still queued
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM tasks")["n"], 1)


class Ledger(unittest.TestCase):
    def test_caps_and_exhaustion(self):
        db = DB(os.path.join(tempfile.mkdtemp(), "l.sqlite3"))
        led = DailyLedger(db, {"max_model_calls": 5, "max_tasks": 2, "max_download_bytes": 100})
        self.assertEqual(led.task_limits({"max_model_calls": 20, "max_download_bytes": 1000})["max_model_calls"], 5)
        led.record({"model_calls": 4, "download_bytes": 10})
        self.assertEqual(led.task_limits({"max_model_calls": 20})["max_model_calls"], 1)
        self.assertIsNone(led.exhausted())
        led.record({"model_calls": 1})
        self.assertEqual(led.exhausted(), "model_calls")


class Discovery(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(extra={"research": {"max_ingest_per_task": 2}})
        self.provider = SnapshotProvider(self.inst.cfg, self.inst.discovery_manifest)

    def tearDown(self):
        self.inst.close()

    def test_incremental(self):
        topic = "language-agnostic sentence embedding"
        r1 = discover_incremental(self.inst.db, self.inst.cfg, topic, self.provider)
        self.assertEqual(r1["found"], 5)
        self.assertEqual(len(r1["ingested"]), 2)
        r2 = discover_incremental(self.inst.db, self.inst.cfg, topic, self.provider)
        self.assertEqual(r2["new"], [])                     # nothing new to score
        self.assertEqual(len(r2["seen_before"]), 5)
        ingested_twice = {i["key"] for i in r1["ingested"]} & {i["key"] for i in r2["ingested"]}
        self.assertEqual(ingested_twice, set())             # backlog continues, no re-download
        statuses = {r["status"] for r in self.inst.db.all("SELECT status FROM discoveries")}
        self.assertIn("ingested", statuses)

    def test_relevance_and_keys(self):
        cfg = self.inst.cfg
        self.assertGreater(relevance("paraphrase identification", "Paraphrase identification review", "", cfg), 0.9)
        self.assertLess(relevance("paraphrase identification", "Paraphrase identification for video speech",
                                  "", cfg), 0.5)
        self.assertEqual(candidate_key("paper", "http://arxiv.org/abs/2007.01852v2"),
                         candidate_key("paper", "https://arxiv.org/abs/2007.01852v1"))
        self.assertEqual(candidate_key("github", "https://github.com/Foo/Bar/blob/x/y.py"), "github:foo/bar")

    def test_irrelevant_not_downloaded(self):
        class P:
            name = "fake"

            def search(self, topic):
                return [{"kind": "paper", "url": "https://arxiv.org/abs/2401.00001v1",
                         "title": "Video sign language translation", "summary": "speech audio"}]

            def ingest(self, db, cand):
                raise AssertionError("must not download irrelevant candidates")
        r = discover_incremental(self.inst.db, self.inst.cfg, "paraphrase identification", P())
        self.assertEqual(len(r["irrelevant"]), 1)


class FakeFetcher:
    """Maps URL substrings to responses for review tests."""

    def __init__(self, routes):
        self.routes = routes

    def _find(self, url):
        for k, v in self.routes.items():
            if k in url:
                return v
        raise AssertionError("unexpected fetch " + url)

    def get(self, url, accept=None):
        v = self._find(url)
        return v if isinstance(v, bytes) else v.encode("utf-8")

    def get_json(self, url):
        return json.loads(self.get(url).decode("utf-8"))


class Review(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.inst.ingest()
        self.inst.propose()
        self.db = self.inst.db
        with open(self.db.one("SELECT path FROM sources WHERE kind='github'")["path"]) as f:
            self.gh_text = f.read()

    def tearDown(self):
        self.inst.close()

    def routes(self, wiki_rev="1359957963", gh_head="c7eaaeea3f39909f906a9cf8ad4d21a878cb4e07", gh_text=None,
               arxiv_ver="v2"):
        wiki_api = json.dumps({"query": {"pages": {"1": {"title": "URI normalization",
                                                         "revisions": [{"revid": int(wiki_rev)}]}}}})
        feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2107.06499%s</id>'
                '<title>Dedup</title><author><name>A</name></author><summary>abstract text</summary></entry></feed>'
                % arxiv_ver)
        return {"w/api.php": wiki_api, "action=raw": "new wiki text about fragments",
                "commits?path=": json.dumps([{"sha": gh_head}]),
                "/repos/ekzhu/datasketch/commits/": json.dumps({"sha": gh_head}),
                "/repos/ekzhu/datasketch/license": json.dumps({"license": {"spdx_id": "MIT"}}),
                "raw.githubusercontent.com": gh_text if gh_text is not None else self.gh_text,
                "export.arxiv.org": feed, "arxiv.org/html": "<html>short</html>"}

    def test_unchanged_sources(self):
        rep = review.review_sources(self.db, self.inst.cfg, older_than_days=0, fetcher=FakeFetcher(self.routes()))
        by = {r["url"]: r for r in rep}
        self.assertTrue(all(r["status"] == "unchanged" for r in rep), rep)
        self.assertEqual(len(by), 3)  # the adversarial page is never reviewed
        self.assertTrue(all(k["review_status"] == "current" for k in self.db.all("SELECT * FROM knowledge")))

    def test_moved_branch_identical_file_is_unchanged(self):
        rep = review.review_sources(self.db, self.inst.cfg, older_than_days=0,
                                    fetcher=FakeFetcher(self.routes(gh_head="d" * 40)))
        gh = [r for r in rep if "github" in r["url"]][0]
        self.assertEqual(gh["status"], "unchanged")
        self.assertIn("byte-identical", gh["note"])

    def test_changed_sources_superseded_and_knowledge_flagged(self):
        rep = review.review_sources(self.db, self.inst.cfg, older_than_days=0, fetcher=FakeFetcher(
            self.routes(wiki_rev="1400000000", gh_head="e" * 40, gh_text="def jaccard(): pass  # changed",
                        arxiv_ver="v3")))
        self.assertEqual({r["status"] for r in rep}, {"changed"})
        for r in rep:
            new = self.db.get("sources", r["new_source_id"])
            self.assertEqual(new["supersedes"], r["source_id"])
        flagged = {k["review_status"] for k in self.db.all("SELECT * FROM knowledge")}
        self.assertEqual(flagged, {"source_changed"})
        # Reviewing again only looks at the newest version of each source.
        due = review.due_sources(self.db, 0, 10)
        self.assertEqual({d["id"] for d in due} & {r["source_id"] for r in rep}, set())

    def test_offline_skips(self):
        rep = review.review_sources(self.db, self.inst.cfg, older_than_days=0, offline=True)
        self.assertEqual({r["status"] for r in rep}, {"skipped"})


class Notify(unittest.TestCase):
    def test_once_key(self):
        db = DB(os.path.join(tempfile.mkdtemp(), "n.sqlite3"))
        n = Notifier(db, tmpcfg())
        self.assertIsNotNone(n.send("a", once_key="k"))
        self.assertIsNone(n.send("a", once_key="k"))
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM outbox")["n"], 1)

    def test_feishu_webhook_signed(self):
        got = {}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"code":0}')

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        db = DB(os.path.join(tempfile.mkdtemp(), "n.sqlite3"))
        env = {"FEISHU_WEBHOOK_URL": "http://127.0.0.1:%d/hook" % srv.server_port, "FEISHU_WEBHOOK_SECRET": "s3"}
        with mock.patch.dict(os.environ, env), mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            oid = FeishuWebhookNotifier(db, tmpcfg()).send("运行 1 达到采用条件")
        srv.server_close()
        self.assertEqual(db.get("outbox", oid)["status"], "sent")
        body = got["body"]
        self.assertEqual(body["content"]["text"], "运行 1 达到采用条件")
        self.assertEqual(body["sign"], feishu_sign(body["timestamp"], "s3"))

    def test_webhook_failure_recorded_not_raised(self):
        db = DB(os.path.join(tempfile.mkdtemp(), "n.sqlite3"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FEISHU_WEBHOOK_URL", None)
            oid = FeishuWebhookNotifier(db, tmpcfg()).send("x")
        self.assertEqual(db.get("outbox", oid)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
