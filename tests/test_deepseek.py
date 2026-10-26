"""DeepSeek transport, output validation, and spending checks without paid calls."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from omnilaw_evolution.config import load_config
from omnilaw_evolution.costs import SpendGuard, SpendRefused
from omnilaw_evolution.db import DB
from omnilaw_evolution.llm import DeepSeekLLM, make_llm
from omnilaw_evolution.schema import SchemaError


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(overrides={
            "state_dir": self.tmp.name,
            "model": {"provider": "deepseek", "model": "deepseek-flash",
                      "api_key_env": "DEEPSEEK_API_KEY", "max_tokens": 4096,
                      "price_per_mtok": {"input": 0.3, "output": 1.2}},
            "budget": {"max_daily_cost_usd": 5}})
        self.db = DB(self.cfg.db_path)
        self.addCleanup(self.db.close)
        self.guard = SpendGuard(self.cfg, self.db)
        self.llm = DeepSeekLLM(self.cfg, guard=self.guard)
        env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-credential"})
        env.start()
        self.addCleanup(env.stop)
        self.source = {"id": 1, "title": "Source", "url": "https://example.com", "read_status": "full"}

    def response(self, content='{"items": []}', finish="stop", usage=True):
        data = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
        if usage:
            data["usage"] = {"prompt_tokens": 1000, "completion_tokens": 100}
        return io.BytesIO(json.dumps(data).encode())

    def test_factory_and_request_and_accounting(self):
        with mock.patch("omnilaw_evolution.costs.SpendGuard", return_value=self.guard):
            self.assertIsInstance(make_llm(self.cfg), DeepSeekLLM)
        with mock.patch("urllib.request.urlopen", return_value=self.response()) as call:
            self.assertEqual(self.llm.extract_knowledge(self.source, "source text"), [])
        req = call.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.deepseek.com/chat/completions")
        body = json.loads(req.data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertIn("JSON schema", body["messages"][0]["content"])
        self.assertNotIn("test-credential", req.data.decode())
        self.assertAlmostEqual(self.guard.spent_today(), 0.00042)

    def test_planning_uses_shared_schema(self):
        with mock.patch("urllib.request.urlopen", return_value=self.response('{"proposals": []}')):
            self.assertEqual(self.llm.plan("dedup", [], []), [])

    def test_budget_and_unknown_price_refuse_before_network(self):
        for cap, price in ((0, self.guard.price), (5, None), (0.001, self.guard.price)):
            self.guard.cap, self.guard.price = cap, price
            with mock.patch("urllib.request.urlopen") as call:
                with self.assertRaises(SpendRefused):
                    self.llm.extract_knowledge(self.source, "text")
                call.assert_not_called()

    def test_invalid_or_truncated_output_is_charged_and_rejected(self):
        for content, finish, error in (('{"items": "wrong"}', "stop", SchemaError),
                                       ('{"items": []}', "length", RuntimeError),
                                       ('', "stop", RuntimeError)):
            with mock.patch("urllib.request.urlopen", return_value=self.response(content, finish)):
                with self.assertRaises(error):
                    self.llm.extract_knowledge(self.source, "text")
        self.assertAlmostEqual(self.guard.spent_today(), 3 * 0.00042)

    def test_missing_usage_books_conservative_cost(self):
        with mock.patch("urllib.request.urlopen", return_value=self.response(usage=False)):
            with self.assertRaisesRegex(RuntimeError, "token usage"):
                self.llm.extract_knowledge(self.source, "text")
        self.assertAlmostEqual(self.guard.spent_today(), self.guard.cost(1048576, 4096))

    def test_null_usage_books_conservative_cost(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"usage": null}')):
            with self.assertRaisesRegex(RuntimeError, "token usage"):
                self.llm.extract_knowledge(self.source, "text")
        self.assertAlmostEqual(self.guard.spent_today(), self.guard.cost(1048576, 4096))

    def test_ambiguous_network_failure_is_charged_without_exposing_details(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("test-credential")) as call:
            with self.assertRaises(RuntimeError) as ctx:
                self.llm.extract_knowledge(self.source, "text")
        self.assertNotIn("test-credential", str(ctx.exception))
        self.assertEqual(call.call_count, 1)
        self.assertTrue(self.llm.last_usage["estimated"])

    def test_http_errors_do_not_echo_response_body(self):
        error = urllib.error.HTTPError("https://api.deepseek.com", 401, "test-credential", {},
                                       io.BytesIO(b"test-credential"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as ctx:
                self.llm.extract_knowledge(self.source, "text")
        self.assertNotIn("test-credential", str(ctx.exception))

    def test_missing_key_does_not_make_request(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("urllib.request.urlopen") as call:
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                self.llm.extract_knowledge(self.source, "text")
            call.assert_not_called()

    def test_chat_uses_native_messages_and_tool_results(self):
        tool = {"type": "function", "function": {"name": "project_command", "parameters": {"type": "object"}}}
        messages = [{"role": "user", "content": "当前状态？"}]
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "choices": [{
            "finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "project_command", "arguments": '{"command":"status","args":[]}'}}]}}]}
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as call:
            assistant = self.llm.chat(messages, tools=[tool])
        request = json.loads(call.call_args.args[0].data)
        self.assertEqual(request["messages"], messages)
        self.assertEqual(request["tools"], [tool])
        self.assertNotIn("response_format", request)
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-1")
        messages += [assistant, {"role": "tool", "tool_call_id": "call-1", "content": "队列为空"}]
        with mock.patch("urllib.request.urlopen", return_value=self.response("队列为空")) as call:
            self.assertEqual(self.llm.chat(messages)["content"], "队列为空")
        self.assertEqual(json.loads(call.call_args.args[0].data)["messages"][-1]["role"], "tool")
