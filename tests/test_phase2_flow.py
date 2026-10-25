"""Phase 2 end-to-end: chat -> queue -> worker -> research/propose/run ->
failure-triggered research -> suppression -> cache -> Feishu adapter -> daemon."""
import json
import os
import threading
import time
import unittest
import urllib.request
from unittest import mock

from omnilaw_evolution import runner, taskqueue, versions
from omnilaw_evolution.adapters.feishu import FeishuEventHandler, make_server
from omnilaw_evolution.chat import ChatRouter
from omnilaw_evolution.db import loads
from omnilaw_evolution.worker import DaemonRefused, Worker, daemon

from helpers import Instance


def make(extra=None):
    inst = Instance(extra=dict({"chat": {"allowed_users": ["u1"], "admins": ["boss"]}}, **(extra or {})))
    inst.cfg["research"]["snapshot_manifest"] = inst.discovery_manifest
    inst.init()
    return inst


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = make()
        cls.db, cls.cfg = cls.inst.db, cls.inst.cfg
        cls.chat = ChatRouter(cls.db, cls.cfg)
        cls.worker = Worker(cls.db, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.inst.close()

    def outbox(self):
        return [r["text"] for r in self.db.all("SELECT text FROM outbox ORDER BY id")]

    def test_1_chat_research_to_ready_run(self):
        reply = self.chat.handle("u1", "研究 news deduplication", channel="feishu")
        self.assertIn("已加入研究任务", reply)
        done = self.worker.run()
        kinds = [(d["kind"], d["status"]) for d in done]
        self.assertEqual(kinds[0], ("research", "done"))
        self.assertIn(("propose", "done"), kinds)
        states = [d["result"]["state"] for d in done if d["kind"] == "run"]
        self.assertEqual(sorted(states), ["READY", "REJECTED"])
        self.assertTrue(any("达到采用条件" in t for t in self.outbox()))
        # nothing was applied automatically
        self.assertEqual(versions.current_release(self.db)["policy"], "initial")

    def test_2_apply_via_chat_needs_admin_and_confirmation(self):
        rid = self.db.one("SELECT id FROM runs WHERE state='READY'")["id"]
        self.assertIn("权限不足", self.chat.handle("u1", "采用 %d" % rid, channel="feishu"))
        ask = self.chat.handle("boss", "采用 %d" % rid, channel="feishu")
        code = ask.split("确认 ")[1].split("”")[0]
        self.assertIn("权限不足", self.chat.handle("u1", "确认 " + code, channel="feishu"))
        out = self.chat.handle("boss", "确认 " + code, channel="feishu")
        self.assertIn("已采用", out)
        self.assertIn("无效", self.chat.handle("boss", "确认 " + code, channel="feishu"))  # single use
        self.assertEqual(versions.current_release(self.db)["run_id"], rid)

    def test_3_scheduled_evaluation_triggers_failure_research_then_blocks(self):
        import datetime as dt
        taskqueue.add_schedule(self.db, "daily-check", "0 8 * * *", "evaluate_release", {"split": "batch2"})
        self.db.execute("UPDATE schedules SET created_at='2026-10-25T07:00:00'")
        fired = taskqueue.tick(self.db, dt.datetime(2026, 10, 25, 8, 0))
        self.assertEqual(len(fired), 1)
        done = self.worker.run()
        kinds = [d["kind"] for d in done]
        self.assertEqual(kinds[0], "evaluate_release")
        self.assertIn("failure_research", kinds)
        fr = [d for d in done if d["kind"] == "failure_research"]
        topics = {self.db.get("tasks", d["task"])["dedupe_key"] for d in fr}
        self.assertIn("failure:paraphrase", topics)
        after = self.chat.handle("u1", "问题", channel="feishu")
        self.assertIn("paraphrase", after)
        self.assertIn("blocked", after)
        blocked = [t for t in self.outbox() if "当前执行器无法实现" in t]
        self.assertTrue(blocked)
        # A blocked class is not researched again and its notice is not repeated.
        n_tasks = self.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
        self.worker._schedule_failure_research(None)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM tasks")["n"], n_tasks)
        self.assertEqual(len([t for t in self.outbox() if "当前执行器无法实现" in t]), len(blocked))

    def test_4_repeated_research_suppressed(self):
        self.chat.handle("u1", "研究 news deduplication", channel="feishu")
        out = self.worker.run_once()
        self.assertEqual(out["status"], "skipped")
        self.assertIn("cooldown", out["result"]["reason"])

    def test_5_cache_reuses_identical_evaluations(self):
        pid = self.db.one("SELECT id FROM proposals ORDER BY id")["id"]
        rid = runner.create_run(self.db, self.cfg, pid)
        runner.Runner(self.db, self.cfg, rid).execute()
        usage = loads(self.db.get("runs", rid)["usage"])
        self.assertGreaterEqual(usage.get("cache_hits", 0), 3)  # variants on dev were evaluated before
        cached = self.db.one("SELECT COUNT(*) AS n FROM evaluations WHERE run_id=? AND isolation LIKE '%(cached)'",
                             (rid,))["n"]
        self.assertGreaterEqual(cached, 3)

    def test_6_feedback_becomes_failure_record(self):
        self.assertIn("已记录", self.chat.handle("u1", "重复 b02 b01", channel="feishu"))
        row = self.db.one("SELECT * FROM failures WHERE split='user' AND item_id='b02'")
        self.assertEqual(row["kind"], "missed-duplicate")
        self.assertIn("untriaged", self.chat.handle("u1", "问题", channel="feishu"))

    def test_7_unauthorised_and_unknown(self):
        self.assertIn("未授权", self.chat.handle("stranger", "状态", channel="feishu"))
        self.assertIn("未知命令", self.chat.handle("u1", "rm -rf /", channel="feishu"))
        self.assertIn("当前版本", self.chat.handle("local", "状态"))

    def test_8_cancel_running_run_task(self):
        pid = self.db.one("SELECT id FROM proposals ORDER BY id")["id"]
        tid, _ = taskqueue.enqueue(self.db, "run", {"proposal_id": pid, "executor": "redteam-slow"}, priority=1)
        from omnilaw_evolution.db import DB
        th = threading.Thread(target=lambda: Worker(DB(self.cfg.db_path), self.cfg).run_once())
        th.start()
        deadline = time.time() + 60
        while time.time() < deadline:
            t = self.db.get("tasks", tid)
            if t["run_id"] and self.db.get("runs", t["run_id"])["state"] == "EVALUATING":
                break
            time.sleep(0.1)
        time.sleep(0.3)
        self.assertIn("cancel_requested", self.chat.handle("boss", "取消 %d" % tid, channel="feishu"))
        th.join(60)
        t = self.db.get("tasks", tid)
        self.assertEqual(t["status"], "cancelled")
        self.assertEqual(self.db.get("runs", t["run_id"])["state"], "CANCELLED")


class Budgets(unittest.TestCase):
    def test_daily_budget_stops_worker_and_notifies_once(self):
        inst = make({"daily_budget": {"max_tasks": 1, "max_model_calls": 100, "max_download_bytes": 10 ** 8}})
        try:
            taskqueue.enqueue(inst.db, "research", {"topic": "news deduplication"})
            w = Worker(inst.db, inst.cfg)
            out = w.run()
            self.assertEqual(out[0]["status"], "done")
            self.assertIn("idle", out[-1])
            self.assertEqual(inst.db.one("SELECT status FROM tasks WHERE kind='propose'")["status"], "queued")
            w.run()
            notes = [r["text"] for r in inst.db.all("SELECT text FROM outbox") if "今日预算已用完" in r["text"]]
            self.assertEqual(len(notes), 1)
        finally:
            inst.close()

    def test_daemon_refuses_without_explicit_opt_in(self):
        inst = make()
        try:
            with self.assertRaises(DaemonRefused):
                daemon(inst.db, inst.cfg, max_cycles=1)
            inst.cfg["background"] = True
            inst.cfg["daily_budget"] = {"max_tasks": 5}
            with self.assertRaises(DaemonRefused):
                daemon(inst.db, inst.cfg, max_cycles=1)
            inst.cfg["daily_budget"] = {"max_tasks": 5, "max_model_calls": 10, "max_download_bytes": 10 ** 7}
            taskqueue.enqueue(inst.db, "research", {"topic": "news deduplication"})
            self.assertEqual(daemon(inst.db, inst.cfg, max_cycles=1, sleep=lambda s: None), 1)
            hb = json.load(open(os.path.join(inst.cfg["state_dir"], "daemon", "heartbeat.json")))
            self.assertGreaterEqual(hb["processed"], 1)
        finally:
            inst.close()


class Feishu(unittest.TestCase):
    def setUp(self):
        self.inst = make()
        self.replies = []
        self.env = mock.patch.dict(os.environ, {"FEISHU_VERIFICATION_TOKEN": "tok", "NO_PROXY": "*",
                                                "no_proxy": "*"})
        self.env.start()
        self.handler = FeishuEventHandler(self.inst.cfg.db_path, self.inst.cfg,
                                          replier=lambda mid, text: self.replies.append((mid, text)),
                                          run_async=False)

    def tearDown(self):
        self.env.stop()
        self.inst.close()

    def event(self, text, eid="e1", user="u1", token="tok"):
        return {"schema": "2.0", "header": {"event_id": eid, "event_type": "im.message.receive_v1",
                                             "token": token},
                "event": {"sender": {"sender_id": {"open_id": user}},
                          "message": {"message_id": "om_1", "message_type": "text",
                                      "content": json.dumps({"text": "@_user_1 " + text})}}}

    def test_over_http(self):
        srv = make_server(self.inst.cfg, port=0, handler=self.handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        url = "http://127.0.0.1:%d/feishu/events" % srv.server_port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(body):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            try:
                with opener.open(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        try:
            self.assertEqual(post({"type": "url_verification", "token": "tok", "challenge": "c1"}),
                             (200, {"challenge": "c1"}))
            self.assertEqual(post({"type": "url_verification", "token": "bad", "challenge": "c1"})[0], 403)
            self.assertEqual(post(self.event("状态", token="bad"))[0], 403)
            self.assertEqual(post(self.event("状态"))[0], 200)
            self.assertEqual(post(self.event("状态"))[1], {"duplicate": "e1"})  # Feishu retry
            self.assertEqual(post({"encrypt": "xxx"})[0], 400)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(self.replies[0][0], "om_1")
        self.assertIn("当前版本", self.replies[0][1])

    def test_stranger_gets_refusal(self):
        self.handler.handle(self.event("回滚 1", eid="e9", user="intruder"))
        self.assertIn("未授权", self.replies[0][1])


if __name__ == "__main__":
    unittest.main()
