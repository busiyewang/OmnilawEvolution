"""Real integrations (0.3.0): semantic runtime gating and templates, the paid
spend guard, the credentials file, Feishu long connection, the Claude Code
executor's cost accounting, and the daemon/service refusals."""
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

from omnilaw_evolution import ops, taskqueue
from omnilaw_evolution.collector import semantic
from omnilaw_evolution.costs import SpendGuard, SpendRefused
from omnilaw_evolution.evaluator import contract_failures, load_split, score
from omnilaw_evolution.knowledge import implementable_methods
from omnilaw_evolution.llm import AnthropicLLM, MockLLM
from omnilaw_evolution.templates import render_dedup
from omnilaw_evolution.worker import DaemonRefused, Worker, check_daemon_config

from helpers import Instance

SEMANTIC_OK = semantic.available()[0]
PRICE = {"input": 4.0, "output": 20.0}


def module_from(src):
    m = types.ModuleType("candidate")
    exec(compile(src, "candidate", "exec"), m.__dict__)
    return m


class SemanticGating(unittest.TestCase):
    def test_methods_need_config_approval_and_runtime(self):
        self.assertNotIn("semantic_embedding", implementable_methods({"semantic": {"enabled": False}}))
        with mock.patch.object(semantic, "available", return_value=(False, "missing")):
            self.assertNotIn("semantic_embedding", implementable_methods({"semantic": {"enabled": True}}))
        with mock.patch.object(semantic, "available", return_value=(True, "ok")):
            self.assertIn("cross_lingual_embedding", implementable_methods({"semantic": {"enabled": True}}))

    def test_mock_planner_uses_semantic_only_when_implementable(self):
        knowledge = [{"id": i + 1, "method": m, "applicable": 1} for i, m in enumerate(
            ["url_canonicalization", "shingle_jaccard", "semantic_embedding", "cross_lingual_embedding"])]
        base = MockLLM().plan("g", knowledge, [])
        self.assertFalse(any("semantic_embedding" in p["methods"] for p in base))
        full = MockLLM().plan("g", knowledge, [], implementable=implementable_methods({}) + [
            "semantic_embedding", "cross_lingual_embedding"])
        sem = [p for p in full if "semantic_embedding" in p["methods"]]
        self.assertEqual(len(sem), 1)
        self.assertEqual(sem[0]["methods"][:2], ["url_canonicalization", "shingle_jaccard"])

    def test_template_never_links_to_an_item_removed_by_a_later_stage(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        fake = types.ModuleType("omnilaw_evolution.collector.semantic")
        vecs = {"a": [1, 0], "b": [0.99, 0.141], "c": [0.99, 0.141]}
        fake.warm_up = lambda: None
        fake.encode = lambda texts: np.array([vecs[t.split()[0]] for t in texts], dtype=float)
        src = render_dedup(["shingle_jaccard", "semantic_embedding"], {"threshold": 0.6, "sem_threshold": 0.9},
                           0, 0, [])
        with mock.patch.dict("sys.modules", {"omnilaw_evolution.collector.semantic": fake}):
            import omnilaw_evolution.collector as pkg
            with mock.patch.object(pkg, "semantic", fake, create=True):
                m = module_from(src)
        items = [{"id": "1", "title": "a", "text": "x"}, {"id": "2", "title": "b", "text": "y"},
                 {"id": "3", "title": "c", "text": "y"}]
        out = m.dedup(items)
        self.assertEqual(contract_failures(items, out), [])
        self.assertEqual([d["duplicate_of"] for d in out], [None, "1", "1"])


@unittest.skipUnless(SEMANTIC_OK, "semantic runtime or pinned model not installed")
class SemanticRuntime(unittest.TestCase):
    def test_encode_is_deterministic_and_normalised(self):
        a = semantic.encode(["OpenAI 发布新模型", "OpenAI releases a new model"])
        b = semantic.encode(["OpenAI 发布新模型", "OpenAI releases a new model"])
        self.assertTrue((a == b).all())
        self.assertAlmostEqual(float((a[0] ** 2).sum()), 1.0, places=4)
        self.assertGreater(float(a[0] @ a[1]), 0.6)

    def test_cross_language_variant_on_dev(self):
        eval_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval")
        rows = load_split(eval_dir, "dev")
        items = [{k: v for k, v in r.items() if k != "label"} for r in rows]
        src = render_dedup(["url_canonicalization", "shingle_jaccard", "cross_lingual_embedding"],
                           {"threshold": 0.6, "k": 4, "xlang_threshold": 0.75}, 0, 0, [])
        out = module_from(src).dedup(items)
        metrics, per_item = score(rows, out, 0)
        xl = [i for i in per_item if i["case"] == "cross-language"]
        self.assertTrue(xl and all(i["outcome"] == "ok" for i in xl))
        self.assertEqual(metrics["false_deletions"], 0)


@unittest.skipUnless(SEMANTIC_OK, "semantic runtime or pinned model not installed")
class SemanticFlow(unittest.TestCase):
    """Approved runtime: failure research -> untried methods -> semantic
    proposal -> candidates evaluated in the sandbox with the real model."""

    def test_unblocked_methods_are_planned_and_evaluated(self):
        inst = Instance(extra={"semantic": {"enabled": True}})
        try:
            inst.cfg["research"]["snapshot_manifest"] = inst.discovery_manifest
            inst.init()
            db = inst.db
            taskqueue.enqueue(db, "research", {"topic": "news deduplication"})
            Worker(db, inst.cfg).run()
            taskqueue.enqueue(db, "evaluate_release", {"split": "batch2"})
            Worker(db, inst.cfg).run()
            sem = [p for p in db.all("SELECT * FROM proposals") if "semantic_embedding" in p["methods"]]
            self.assertTrue(sem)
            notes = [r["text"] for r in db.all("SELECT text FROM outbox")]
            self.assertTrue(any("现已可实现且尚未试验" in t for t in notes))
            self.assertFalse(any("当前执行器无法实现" in t for t in notes))
            for p in sem:
                run = db.one("SELECT * FROM runs WHERE proposal_id=?", (p["id"],))
                self.assertIn(run["state"], ("READY", "REJECTED"))
                evs = db.all("SELECT * FROM evaluations WHERE run_id=? AND variant!='baseline'", (run["id"],))
                self.assertTrue(evs)
                for e in evs:
                    self.assertIn(e["isolation"].split()[0], ("sandbox-exec", "bwrap", "none"))
                    self.assertIsNotNone(json.loads(e["metrics"]), "candidate crashed: %s" % e["failures"])
        finally:
            inst.close()


class FakeUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class FakeClient:
    def __init__(self, input_tokens=1000, text='{"items": []}'):
        self.calls = []
        outer = self

        class Messages:
            def count_tokens(self, **kw):
                outer.calls.append(("count", kw))
                return types.SimpleNamespace(input_tokens=input_tokens)

            def create(self, **kw):
                outer.calls.append(("create", kw))
                return types.SimpleNamespace(usage=FakeUsage(input_tokens, 500), stop_reason="end_turn",
                                             content=[types.SimpleNamespace(type="text", text=text)])
        self.messages = Messages()


class Spend(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()

    def tearDown(self):
        self.inst.close()

    def cfg(self, price=PRICE, cap=1.0):
        cfg = self.inst.cfg
        cfg["model"] = dict(cfg["model"], provider="anthropic", price_per_mtok=price)
        cfg["budget"] = dict(cfg["budget"], max_daily_cost_usd=cap)
        return cfg

    def test_unknown_price_or_zero_cap_refuses_without_calling(self):
        for price, cap in ((None, 1.0), (PRICE, 0)):
            client = FakeClient()
            llm = AnthropicLLM(self.cfg(price, cap), client=client, guard=SpendGuard(self.cfg(price, cap),
                                                                                    self.inst.db))
            with self.assertRaises(SpendRefused) as ctx:
                llm.extract_knowledge({"id": 1, "title": "t", "url": "u", "read_status": "full"}, "text")
            self.assertFalse(ctx.exception.cap_reached)
            self.assertEqual(client.calls, [])

    def test_calls_are_priced_recorded_and_capped(self):
        cfg = self.cfg(cap=0.35)
        guard = SpendGuard(cfg, self.inst.db)
        client = FakeClient(input_tokens=1000)
        llm = AnthropicLLM(cfg, client=client, guard=guard)
        llm.extract_knowledge({"id": 1, "title": "t", "url": "u", "read_status": "full"}, "text")
        # 1000 in * $4 + 500 out * $20 per MTok
        self.assertAlmostEqual(llm.last_usage["cost_usd"], 0.014)
        self.assertAlmostEqual(guard.spent_today(), 0.014)
        # worst case per call: 1000 in + 16000 out = $0.324; 0.014 + 0.324 fits 0.35, 0.028 + 0.324 does not
        llm.extract_knowledge({"id": 1, "title": "t", "url": "u", "read_status": "full"}, "text")
        with self.assertRaises(SpendRefused) as ctx:
            llm.extract_knowledge({"id": 1, "title": "t", "url": "u", "read_status": "full"}, "text")
        self.assertTrue(ctx.exception.cap_reached)
        self.assertEqual([c[0] for c in client.calls], ["count", "create", "count", "create", "count"])
        self.assertNotIn("extra_body", client.calls[1][1])  # no server-side fallback to an unpriced model

    def test_worker_defers_when_cost_cap_is_reached(self):
        self.inst.init()
        taskqueue.enqueue(self.inst.db, "research", {"topic": "x"})
        w = Worker(self.inst.db, self.inst.cfg)
        with mock.patch.object(Worker, "do_research", side_effect=SpendRefused("cap", cap_reached=True)):
            out = w.run_once()
        self.assertEqual(out["status"], "deferred")
        self.assertTrue(any("费用上限" in r["text"] for r in self.inst.db.all("SELECT text FROM outbox")))

    def test_daemon_refuses_paid_model_without_price_or_cap(self):
        cfg = self.inst.cfg
        cfg["background"] = True
        cfg["daily_budget"] = {"max_tasks": 5, "max_model_calls": 10, "max_download_bytes": 10 ** 7}
        check_daemon_config(cfg)  # mock model: fine
        self.cfg(price=None)
        with self.assertRaises(DaemonRefused):
            check_daemon_config(cfg)
        self.cfg(price=PRICE, cap=0)
        with self.assertRaises(DaemonRefused):
            check_daemon_config(cfg)
        self.cfg(price=PRICE, cap=2.0)
        check_daemon_config(cfg)


class ClaudeCodeCost(unittest.TestCase):
    def test_reported_cost_is_recorded_and_limit_passed(self):
        inst = Instance()
        try:
            cli = os.path.join(inst.tmp, "fake-claude")
            log = os.path.join(inst.tmp, "args.json")
            with open(cli, "w") as f:
                f.write("#!%s\nimport json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n"
                        "print(json.dumps({'result': 'edited', 'total_cost_usd': 0.12, 'is_error': False}))\n"
                        % (sys.executable, log))
            os.chmod(cli, 0o755)
            cfg = inst.cfg
            cfg["executor"] = {"kind": "claude-code", "claude_cli": cli, "max_usd_per_candidate": 0.5}
            cfg["budget"] = dict(cfg["budget"], max_daily_cost_usd=2.0)
            cfg["sandbox"] = {"mode": "none"}
            from omnilaw_evolution.executors import ClaudeCodeExecutor
            work = os.path.join(inst.tmp, "cand", "work")
            os.makedirs(work)
            out = ClaudeCodeExecutor(cfg).write_candidate(work, {"goal": "g", "hypothesis": "h", "methods": []},
                                                          {}, 1, 0, [])
            self.assertEqual(out["cost_usd"], 0.12)
            args = json.load(open(log))
            self.assertEqual(args[args.index("--max-budget-usd") + 1], "0.50")
            self.assertAlmostEqual(SpendGuard(cfg).spent_today(), 0.12)
        finally:
            inst.close()


class EnvFile(unittest.TestCase):
    def test_loads_names_and_refuses_open_permissions(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "env")
        with open(p, "w") as f:
            f.write("# c\nOMNILAW_TEST_A=1\nexport OMNILAW_TEST_B='two'\nPATH=nope\n")
        os.chmod(p, 0o644)
        with self.assertRaises(RuntimeError):
            ops.load_env_file(p)
        os.chmod(p, 0o600)
        with mock.patch.dict(os.environ, {}, clear=False):
            names = ops.load_env_file(p)
            self.assertEqual(names, ["OMNILAW_TEST_A", "OMNILAW_TEST_B"])  # PATH already set: untouched
            self.assertEqual(os.environ["OMNILAW_TEST_B"], "two")
            self.assertNotEqual(os.environ["PATH"], "nope")

    def test_template_is_private_and_has_no_values(self):
        inst = Instance()
        try:
            p = os.path.join(inst.tmp, "cfgdir", "env")
            path, created = ops.init_env_file(inst.cfg, p)
            self.assertTrue(created)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            lines = [l for l in open(path) if not l.startswith("#")]
            self.assertTrue(all(l.strip().endswith("=") for l in lines))
            self.assertFalse(ops.init_env_file(inst.cfg, p)[1])  # never overwritten
        finally:
            inst.close()


class FeishuLongConnection(unittest.TestCase):
    def test_events_reach_router_without_token(self):
        try:
            import lark_oapi  # noqa: F401
        except ImportError:
            self.skipTest("lark-oapi not installed")
        from omnilaw_evolution.adapters.feishu import FeishuEventHandler, run_long_connection
        inst = Instance(extra={"chat": {"allowed_users": ["ou_1"], "admins": []}})
        try:
            replies = []
            handler = FeishuEventHandler(inst.cfg.db_path, inst.cfg, replier=lambda m, t: replies.append((m, t)),
                                         run_async=False)
            started = []
            env = {"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "s"}
            with mock.patch.dict(os.environ, env):
                dispatcher = run_long_connection(inst.cfg, handler,
                                                 client_factory=lambda: types.SimpleNamespace(
                                                     start=lambda: started.append(True)))
            self.assertEqual(started, [True])
            body = {"schema": "2.0", "header": {"event_id": "ws1", "event_type": "im.message.receive_v1"},
                    "event": {"sender": {"sender_id": {"open_id": "ou_1"}},
                              "message": {"message_id": "om_9", "message_type": "text",
                                          "content": json.dumps({"text": "@_user_1 帮助"})}}}
            payload = json.dumps(body).encode()
            dispatcher.do_without_validation(payload)  # what the ws client calls per frame
            dispatcher.do_without_validation(payload)  # redelivery is de-duplicated
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0][0], "om_9")
        finally:
            inst.close()

    def test_missing_credentials(self):
        from omnilaw_evolution.adapters.feishu import run_long_connection
        inst = Instance()
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    run_long_connection(inst.cfg, client_factory=lambda: None)
        finally:
            inst.close()


class Service(unittest.TestCase):
    def test_install_refuses_like_the_daemon(self):
        inst = Instance()
        try:
            with self.assertRaises(DaemonRefused):
                ops.install_service(inst.cfg, inst.cfg_path)
        finally:
            inst.close()

    def test_definition_has_no_secrets(self):
        inst = Instance()
        try:
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret"}):
                d = ops.service_definition(inst.cfg, inst.cfg_path)
            self.assertNotIn("sk-secret", json.dumps(d))
            self.assertEqual(d["ProgramArguments"][-1], "daemon")
            self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})
        finally:
            inst.close()


if __name__ == "__main__":
    unittest.main()
