"""Real vendored loop + real filesystem/terminal tools; only model replies are scripted.

Run with `python -m unittest tests.test_hermes_agent -v` after installing .[agent].
No paid API, user files, or installed Hermes profile is used.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from omnilaw_evolution.config import load_config
from omnilaw_evolution.conversation import Conversation
from omnilaw_evolution.db import DB

AVAILABLE = sys.version_info >= (3, 14) and all(importlib.util.find_spec(m)
    for m in ("openai", "rich", "dotenv", "yaml", "browser_harness"))


@unittest.skipUnless(AVAILABLE, "install .[agent] with Python 3.14 for native integration")
class HermesIntegrationTests(unittest.TestCase):
    def test_interrupt_stops_a_real_command_and_conversation_can_continue(self):
        with tempfile.TemporaryDirectory(prefix="omnilaw-interrupt-test-") as tmp:
            cfg = load_config(overrides={"project_root": tmp, "state_dir": str(Path(tmp) / "state"),
                "model": {"provider": "deepseek", "model": "deepseek-flash"}, "chat": {"max_seconds": 60}})
            db = DB(cfg.db_path)
            self.addCleanup(db.close)
            calls, timers = [], []
            def event(message):
                if message.startswith("terminal：") and "sleep 30" in message and not timers:
                    timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT))
                    timers.append(timer)
                    timer.start()
            def complete(llm, messages, **options):
                if options.get("tools"):
                    calls.append(messages)
                message = {"role": "assistant", "content": "恢复成功"}
                if options.get("tools") and len(calls) == 1:
                    message = {"role": "assistant", "content": "", "tool_calls": [{"id": "sleep-call",
                        "type": "function", "function": {"name": "terminal", "arguments": '{"command":"sleep 30"}'}}]}
                return {"id": "cancel-test", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
                        "choices": [{"index": 0, "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            chat = Conversation(db, cfg, on_event=event)
            try:
                with mock.patch("omnilaw_evolution.llm.DeepSeekLLM.complete_raw", complete):
                    start = time.monotonic()
                    reply = chat.handle("运行 sleep 30，等待用户中断")
                    self.assertTrue(timers, reply)
                    self.assertLess(time.monotonic() - start, 20)
                    self.assertIn("恢复成功", chat.handle("继续聊天"))
                    self.assertIn("sleep-call", json.dumps(calls[-1]))
            finally:
                for timer in timers:
                    timer.cancel()
                chat.close()

    def test_real_loop_runs_repairs_verifies_and_resumes_with_memory(self):
        with tempfile.TemporaryDirectory(prefix="omnilaw-agent-test-") as tmp:
            root = Path(tmp)
            cfg = load_config(overrides={"project_root": tmp, "state_dir": str(root / "state"),
                "model": {"provider": "deepseek", "model": "deepseek-flash"},
                "chat": {"max_seconds": 120, "max_iterations": 20}})
            db = DB(cfg.db_path)
            self.addCleanup(db.close)
            steps = [
                ("runtime_info", {}),
                ("todo_list", {"todos": [{"id": "1", "content": "Repair script and verify", "status": "in_progress"}]}),
                ("write_file", {"path": str(root / "probe.py"), "content": "print(1 / 0)\n"}),
                ("terminal", {"command": "python probe.py"}),
                ("read_file", {"path": str(root / "probe.py")}),
                ("patch", {"path": str(root / "probe.py"), "old_string": "1 / 0", "new_string": "17 * 19"}),
                ("terminal", {"command": "python probe.py"}),
                ("project_command", {"command": "status", "args": []}),
                ("memory", {"target": "memory", "operations": [{"action": "add", "content": "测试项目代号：蓝鲸-323"}]}),
                ("todo_list", {"todos": [{"id": "1", "content": "Repair script and verify", "status": "completed"}]}),
            ]
            seen, schemas = [], []

            def complete(llm, messages, **options):
                llm.last_usage = {"input_tokens": 10, "output_tokens": 5}
                if not options.get("tools"):
                    # Upstream also asks its configured model for command-risk
                    # review / session summaries. Those calls use the SAME bridge.
                    content = "APPROVE" if "APPROVE, DENY, or ESCALATE" in str(messages) else "Script repair verified: 323."
                    message = {"role": "assistant", "content": content}
                else:
                    seen.append(messages)
                    schemas.append([t["function"]["name"] for t in options["tools"]])
                    index = len(seen) - 1
                    if index < len(steps):
                        name, args = steps[index]
                        message = {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "call-%d" % index, "type": "function", "function": {
                                "name": name, "arguments": json.dumps(args)}}]}
                    else:
                        message = {"role": "assistant", "content": "已修复并运行验证：323。"}
                return {"id": "test", "object": "chat.completion", "model": "deepseek-flash", "created": 1,
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

            with mock.patch("omnilaw_evolution.llm.DeepSeekLLM.complete_raw", complete):
                chat = Conversation(db, cfg, session="native-test")
                try:
                    reply = chat.handle("在当前目录创建测试脚本、运行、根据错误修正、再运行验证，并记录项目代号蓝鲸-323。")
                    self.assertIn("已修复", reply)
                    self.assertTrue((root / "probe.py").exists(), json.dumps(chat.history(), ensure_ascii=False))
                    self.assertEqual((root / "probe.py").read_text(), "print(17 * 19)\n")
                    transcript = json.dumps(chat.history(), ensure_ascii=False)
                    self.assertIn("ZeroDivisionError", transcript)
                    self.assertIn('323', transcript)
                    self.assertIn("队列：空", transcript)
                    self.assertIn("agent_pid", transcript)
                    self.assertNotIn("BLOCKED", transcript)
                    for required in ("terminal", "process_manage", "web_search", "web_extract", "browser_exec",
                                     "memory", "session_search", "skills_list", "todo_list", "project_command"):
                        self.assertIn(required, schemas[0])
                finally:
                    chat.close()
                resumed = Conversation(db, cfg, session="native-test")
                try:
                    resumed.handle("复核刚才结果")
                    self.assertIn("ZeroDivisionError", json.dumps(seen[-1]))
                finally:
                    resumed.close()
                fresh = Conversation(db, cfg, session="fresh-test")
                try:
                    fresh.handle("项目代号是什么？")
                    self.assertIn("蓝鲸-323", json.dumps(seen[-1], ensure_ascii=False))
                finally:
                    fresh.close()
            self.assertGreaterEqual(db.one("SELECT model_calls FROM budget_ledger")["model_calls"], len(seen))


if __name__ == "__main__":
    unittest.main()
