"""Agent boundary contracts, without paid calls or touching user profiles."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from omnilaw_evolution.agent_bridge import AgentBridge
from omnilaw_evolution.config import load_config
from omnilaw_evolution.conversation import Conversation
from omnilaw_evolution.costs import SpendRefused
from omnilaw_evolution.db import DB
from omnilaw_evolution.hermes_runtime import worker_environment


class AgentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(overrides={"state_dir": self.tmp.name,
            "model": {"provider": "deepseek", "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY",
                      "max_tokens": 4096, "price_per_mtok": {"input": 0.3, "output": 1.2}},
            "budget": {"max_daily_cost_usd": 5}})
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)
        self.chat = Conversation(self.db, self.cfg)
        self.bridge = AgentBridge(self.cfg, "local", "cli", self.chat._tool_schema())
        self.addCleanup(self.bridge.close)
        self.payload = {"messages": [{"role": "system", "content": "你是 Omnilaw，运行在用户本机的个人智能体"},
                                     {"role": "user", "content": "hello"}]}

    def test_wire_requires_token_even_on_loopback(self):
        req = urllib.request.Request(self.bridge.url + "/v1/chat/completions",
                                     data=json.dumps(self.payload).encode())
        with mock.patch.object(self.bridge, "completion") as complete:
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req)
            self.assertEqual(exc.exception.code, 401)
            exc.exception.close()
            complete.assert_not_called()

    def test_zero_budget_and_call_cap_block_before_upstream(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret-for-test"}), \
             mock.patch("urllib.request.urlopen") as network:
            self.cfg["budget"]["max_daily_cost_usd"] = 0
            with self.assertRaises(SpendRefused):
                self.bridge.completion(self.payload)
            self.cfg["budget"]["max_daily_cost_usd"] = 5
            self.cfg["daily_budget"]["max_model_calls"] = 0
            with self.assertRaises(SpendRefused):
                self.bridge.completion(self.payload)
            network.assert_not_called()

    def test_model_override_cannot_bypass_price_or_output_cap(self):
        response = {"id": "test", "choices": [{"message": {"role": "assistant", "content": "ok"},
                                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
        payload = dict(self.payload, model="unpriced-model", max_tokens=999999, stream=True,
                       thinking={"type": "enabled"})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret-for-test"}), \
             mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as network:
            self.assertEqual(self.bridge.completion(payload), response)
        sent = json.loads(network.call_args.args[0].data)
        self.assertEqual(sent["model"], "deepseek-flash")
        self.assertEqual(sent["max_tokens"], 4096)
        self.assertFalse(sent["stream"])
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(self.bridge.last_messages[-1]["content"], "ok")
        row = self.db.one("SELECT * FROM budget_ledger")
        self.assertEqual((row["model_calls"], row["input_tokens"], row["output_tokens"]), (1, 1000, 100))
        self.assertAlmostEqual(row["cost_usd"], 0.00042)

    def test_project_commands_use_existing_permissions_and_confirm_path(self):
        self.assertIn("队列", self.bridge.project_command({"command": "status", "args": []}))
        self.bridge.project_command({"command": "apply", "args": ["1"]})
        self.assertEqual(self.db.one("SELECT used FROM chat_confirmations")["used"], 0)
        with self.assertRaises(ValueError):
            self.bridge.project_command({"command": "confirm", "args": ["123"]})
        self.assertIsNone(self.db.one("SELECT * FROM releases"))

    def test_worker_does_not_inherit_provider_credentials_or_profile(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deep-secret", "ANTHROPIC_API_KEY": "other-secret",
                 "HERMES_HOME": "/private/user/profile", "HERMES_PROFILE": "private", "PYTHONPATH": "/old/hermes"}):
            env = worker_environment(Path(self.tmp.name), Path.cwd(), self.bridge)
        self.assertEqual(env["HERMES_HOME"], self.tmp.name)
        self.assertEqual(env["OPENAI_API_KEY"], self.bridge.token)
        for key in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "HERMES_PROFILE", "PYTHONPATH"):
            self.assertNotIn(key, env)

    def test_remote_chat_does_not_get_local_shell_engine(self):
        self.assertTrue(self.chat.use_hermes)
        self.assertFalse(Conversation(self.db, self.cfg, user="local", channel="feishu").use_hermes)
        self.assertFalse(Conversation(self.db, self.cfg, user="reader").use_hermes)

    def test_snapshot_survives_restart_and_slash_commands_are_not_duplicated(self):
        self.chat.handle("/status")
        self.assertEqual(len(self.chat._agent_context()), 2)
        other = Conversation(self.db, self.cfg)
        self.assertEqual(other._agent_context(), self.chat._agent_context())
        other.handle("/new")
        self.assertEqual(other._agent_context(), [])

    def test_interrupted_batch_is_repaired_before_followup(self):
        messages = [{"role": "user", "content": "run"}, {"role": "assistant", "content": "",
            "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "done"}]
        repaired = Conversation._repair_tool_tail(messages)
        self.assertEqual(repaired[-1]["tool_call_id"], "b")
        self.assertIn("结果未知", repaired[-1]["content"])
        self.assertEqual(Conversation._repair_tool_tail(repaired), repaired)

    def test_sse_reconstructs_tool_call_and_usage(self):
        message = {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"}}]}
        data = {"choices": [{"message": message, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
        raw = self.bridge.as_sse(data).decode()
        frames = [json.loads(s[6:]) for s in raw.split("\n\n") if s and s != "data: [DONE]"]
        self.assertEqual(frames[0]["choices"][0]["delta"]["tool_calls"][0]["index"], 0)
        self.assertEqual(frames[1]["usage"], data["usage"])
        self.assertNotIn("index", message["tool_calls"][0])

    def test_auxiliary_request_does_not_replace_conversation_recovery_snapshot(self):
        self.bridge.last_messages = [{"role": "user", "content": "real task"}]
        data = {"choices": [{"message": {"role": "assistant", "content": "APPROVE"}}]}
        with mock.patch("omnilaw_evolution.llm.DeepSeekLLM.complete_raw", return_value=data):
            self.bridge.completion({"messages": [{"role": "user", "content": "risk classification"}]})
        self.assertEqual(self.bridge.last_messages, [{"role": "user", "content": "real task"}])

    def test_crash_checkpoint_preserves_pending_tool_with_unknown_result(self):
        self.bridge.session = "default"
        assistant = {"role": "assistant", "content": "", "tool_calls": [{"id": "pending-1", "type": "function",
                     "function": {"name": "terminal", "arguments": '{"command":"touch marker"}'}}]}
        with mock.patch("omnilaw_evolution.llm.DeepSeekLLM.complete_raw",
                        return_value={"choices": [{"message": assistant}]}):
            self.bridge.completion(self.payload)
        resumed = Conversation(self.db, self.cfg)._agent_context()
        self.assertEqual(resumed[-1]["tool_call_id"], "pending-1")
        self.assertIn("结果未知", resumed[-1]["content"])

    def test_shutdown_waits_for_inflight_accounting(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def completion(payload):
            started.set()
            release.wait(5)
            finished.set()
        with mock.patch.object(self.bridge, "_completion", completion):
            caller = threading.Thread(target=self.bridge.completion, args=(self.payload,))
            caller.start()
            self.assertTrue(started.wait(2))
            closer = threading.Thread(target=self.bridge.close)
            closer.start()
            closer.join(0.7)
            self.assertTrue(closer.is_alive())
            release.set()
            caller.join(2)
            closer.join(2)
            self.assertTrue(finished.is_set())
            self.assertFalse(closer.is_alive())


if __name__ == "__main__":
    unittest.main()
