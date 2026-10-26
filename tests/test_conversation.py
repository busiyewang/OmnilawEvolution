import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from omnilaw_evolution.config import load_config
from omnilaw_evolution.conversation import Conversation
from omnilaw_evolution.costs import SpendGuard, SpendRefused
from omnilaw_evolution.db import DB


def answer(text):
    return {"role": "assistant", "content": text}


def tool(command, args=None):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call-1", "type": "function", "function": {"name": "project_command",
        "arguments": json.dumps({"command": command, "args": args or []})}}]}


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.last_usage = {}

    def chat(self, messages, tools=None):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        self.last_usage = {"input_tokens": 10, "output_tokens": 5}
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(overrides={"state_dir": self.tmp.name,
            "model": {"provider": "deepseek"}, "daily_budget": {"max_model_calls": 20},
            "chat": {"allowed_users": ["alice", "bob"]}})
        self.db = DB(os.path.join(self.tmp.name, "state.sqlite3"))
        self.addCleanup(self.db.close)

    def chat(self, replies, **kwargs):
        return Conversation(self.db, self.cfg, llm=FakeLLM(replies), **kwargs)

    def test_greeting_reaches_model_and_history_survives_restart(self):
        first = self.chat([answer("你好，小王")])
        self.assertEqual(first.handle("你好，我叫小王"), "你好，小王")
        second = self.chat([answer("你叫小王")])
        self.assertEqual(second.handle("我叫什么？"), "你叫小王")
        messages = second.llm.calls[0][0]
        self.assertEqual([m["content"] for m in messages[1:]], ["你好，我叫小王", "你好，小王", "我叫什么？"])
        row = self.db.one("SELECT model_calls,input_tokens,output_tokens FROM budget_ledger")
        self.assertEqual(row, {"model_calls": 2, "input_tokens": 20, "output_tokens": 10})

    def test_status_tool_receives_real_output_before_answer(self):
        chat = self.chat([tool("status"), answer("当前还没有研究任务。")])
        self.assertIn("没有研究", chat.handle("帮我看看目前运行得怎么样"))
        tool_result = chat.llm.calls[1][0][-1]
        self.assertEqual(tool_result["role"], "tool")
        self.assertIn("队列：空", tool_result["content"])

    def test_research_enqueues_a_real_task(self):
        chat = self.chat([tool("research", ["news deduplication"]), answer("已提交研究任务。")])
        chat.handle("帮我研究一下新闻去重的方法")
        task = self.db.one("SELECT kind,payload,status FROM tasks")
        self.assertEqual(task["kind"], "research")
        self.assertEqual(json.loads(task["payload"])["topic"], "news deduplication")
        self.assertEqual(task["status"], "queued")

    def test_model_cannot_confirm_or_run_shell(self):
        for command in ("confirm", "rm"):
            chat = self.chat([tool(command, ["123456"]), answer("不能执行。")], session=command)
            chat.handle("试试执行")
            self.assertIn("失败", chat.llm.calls[1][0][-1]["content"])
        self.assertIsNone(self.db.one("SELECT * FROM chat_confirmations"))

    def test_apply_still_requires_user_confirmation(self):
        chat = self.chat([tool("apply", ["1"]), answer("请先确认。")])
        chat.handle("请采用运行1")
        row = self.db.one("SELECT * FROM chat_confirmations")
        self.assertEqual(row["used"], 0)
        self.assertIsNone(self.db.one("SELECT * FROM releases"))

    def test_unauthorised_does_not_call_model_or_read_history(self):
        chat = self.chat([], user="stranger", channel="feishu")
        self.assertIn("未授权", chat.handle("你好"))
        self.assertIn("未授权", chat.handle("/history"))
        self.assertEqual(chat.llm.calls, [])

    def test_non_admin_cannot_use_admin_tools(self):
        chat = self.chat([tool("apply", ["1"]), answer("没有权限。")], user="alice", channel="test")
        chat.handle("请采用运行1")
        schema = chat.llm.calls[0][1][0]["function"]["parameters"]
        self.assertNotIn("apply", schema["properties"]["command"]["enum"])
        self.assertIsNone(self.db.one("SELECT * FROM chat_confirmations"))

    def test_sessions_users_and_channels_are_separate(self):
        self.chat([answer("private")], user="alice", channel="test").handle("私密消息")
        for kw in ({"user": "bob", "channel": "test"}, {"user": "alice", "channel": "cli"},
                   {"user": "alice", "channel": "test", "session": "other"}):
            chat = self.chat([answer("你好")], **kw)
            chat.handle("你好")
            self.assertEqual(len(chat.llm.calls[0][0]), 2)

    def test_new_preserves_old_transcript_and_clears_context(self):
        chat = self.chat([answer("旧回答"), answer("新回答")])
        chat.handle("旧问题")
        self.assertIn("新会话", chat.handle("/new"))
        self.assertNotEqual(chat.session, "default")
        chat.handle("新问题")
        self.assertEqual(len(chat.llm.calls[1][0]), 2)
        self.assertIn("旧回答", self.chat([]).handle("/history"))

    def test_commands_do_not_spend_tokens(self):
        chat = self.chat([])
        self.assertIn("队列", chat.handle("/status"))
        self.assertIn("队列", chat.handle("状态"))
        self.assertIn("未知命令", chat.handle("/nonsense"))
        self.assertEqual(chat.llm.calls, [])

    def test_daily_call_limit_refuses_before_model(self):
        self.cfg["daily_budget"]["max_model_calls"] = 0
        chat = self.chat([])
        self.assertIn("调用次数已用完", chat.handle("你好"))
        self.assertEqual(chat.llm.calls, [])

    def test_tool_loop_is_bounded(self):
        self.cfg["chat"]["max_tool_rounds"] = 1
        chat = self.chat([tool("status"), tool("research", ["should not run"])])
        self.assertIn("工具调用限制", chat.handle("查询状态"))
        self.assertIsNone(chat.llm.calls[1][1])
        self.assertIsNone(self.db.one("SELECT * FROM tasks"))

    def test_api_failure_keeps_completed_tool_result(self):
        chat = self.chat([tool("research", ["dedup"]), RuntimeError("network failed")])
        reply = chat.handle("帮我研究去重")
        self.assertIn("本轮未完成", reply)
        self.assertIn("已加入研究任务", reply)
        self.assertEqual(len(chat.history()), 4)

    def test_batched_tools_are_sequential_and_total_is_bounded(self):
        self.cfg["chat"]["max_tool_rounds"] = 2
        batch = tool("status")
        batch["tool_calls"] += [tool("queue")["tool_calls"][0], tool("research", ["must not run"])["tool_calls"][0]]
        for i, call in enumerate(batch["tool_calls"]):
            call["id"] = "call-%d" % i
        chat = self.chat([batch, answer("状态已查询。")])
        chat.handle("查一下状态和任务队列")
        results = [m for m in chat.llm.calls[1][0] if m["role"] == "tool"]
        self.assertEqual(len(results), 3)
        self.assertIn("队列", results[0]["content"])
        self.assertIn("队列为空", results[1]["content"])
        self.assertIn("未执行", results[2]["content"])
        self.assertIsNone(self.db.one("SELECT * FROM tasks"))
        self.assertIsNone(chat.llm.calls[1][1])

    def test_interruption_remains_usable(self):
        chat = self.chat([KeyboardInterrupt(), answer("你好")])
        self.assertIn("已中断", chat.handle("第一次"))
        self.assertEqual(chat.handle("第二次"), "你好")

    def test_history_trims_whole_turns(self):
        self.cfg["chat"]["history_turns"] = 1
        chat = self.chat([answer("一"), tool("status"), answer("二"), answer("三")])
        chat.handle("第一问")
        chat.handle("第二问")
        chat.handle("第三问")
        messages = chat.llm.calls[-1][0]
        self.assertNotIn("第一问", [m.get("content") for m in messages])
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "assistant", "user"])

    def test_atomic_spend_reservation_prevents_overbooking(self):
        self.cfg["budget"]["max_daily_cost_usd"] = 1
        self.cfg["model"]["price_per_mtok"] = {"input": 0.3, "output": 1.2}
        a = SpendGuard(self.cfg, self.db)
        other_db = DB(self.db.path)
        self.addCleanup(other_db.close)
        b = SpendGuard(self.cfg, other_db)
        day = a.reserve(0.7)
        with self.assertRaises(SpendRefused):
            b.reserve(0.7)
        a.settle(day, 0.7, 0.1)
        b.reserve(0.7)
        self.assertAlmostEqual(a.spent_today(), 0.8)

    def test_piped_cli_chats_and_exits(self):
        from omnilaw_evolution.cli import main
        llm = FakeLLM([answer("你好！")])
        with mock.patch("omnilaw_evolution.cli.load_config", return_value=self.cfg), \
             mock.patch("omnilaw_evolution.conversation.make_llm", return_value=llm), \
             mock.patch("sys.stdin", io.StringIO("你好\n/exit\n")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["chat", "--session", "cli-test"]), 0)
        self.assertIn("直接输入消息即可聊天", out.getvalue())
        self.assertIn("你好！", out.getvalue())


if __name__ == "__main__":
    unittest.main()
