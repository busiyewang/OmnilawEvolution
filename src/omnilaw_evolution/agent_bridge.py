"""Loopback transport for the embedded agent. Owns credentials and accounting.

Hermes main calls, retries, compression and recall all use this endpoint. Each
request gets its own SQLite connection and the same atomic dollar reservation
as the research worker. No upstream key is passed to the agent subprocess.
"""
from __future__ import annotations

import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import shlex
import threading
import time

from .budget import DailyLedger, today
from .chat import ChatRouter
from .costs import SpendGuard, SpendRefused
from .db import DB, now
from .llm import DeepSeekLLM
from .schema import validate


class AgentBridge:
    def __init__(self, cfg, user, channel, command_schema, session=None, run_cap_usd=None):
        """``command_schema=None`` disables project commands (evaluation runs).
        ``run_cap_usd`` stops serving model calls once this bridge's settled
        spend reaches it; ``usage`` accumulates what this bridge spent."""
        self.cfg, self.user, self.channel = cfg, user, channel
        self.command_schema = command_schema
        self.session = session
        self.run_cap_usd = run_cap_usd
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "estimated": False}
        self.refused, self.errors = [], []  # messages only; no request bodies
        self.token = secrets.token_urlsafe(32)
        self.last_messages = []
        self._lock = threading.Lock()
        self._requests = threading.Condition()
        self._active = 0
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # request logs must not contain tokens or conversation text

            def _send(self, status, data, content_type="application/json"):
                body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # caller interrupted; upstream spend was already accounted

            def _authorized(self):
                if hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + bridge.token):
                    return True
                self._send(401, {"error": {"message": "Unauthorized", "type": "authentication_error"}})
                return False

            def do_GET(self):
                if self._authorized():
                    if self.path.rstrip("/") == "/v1/models":
                        self._send(200, {"object": "list", "data": [{"id": bridge.cfg["model"]["model"],
                            "object": "model", "owned_by": "omnilaw", "created": 0}]})
                    else:
                        self._send(404, {"error": {"message": "Unknown endpoint"}})

            def do_POST(self):
                if not self._authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16 * 1024 * 1024:
                        raise ValueError("Request size exceeds limit")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("Expected a JSON object")
                    if self.path == "/project-command" and bridge.command_schema is not None:
                        self._send(200, {"result": bridge.project_command(data)})
                    elif self.path.rstrip("/") == "/v1/chat/completions":
                        response = bridge.completion(data)
                        if data.get("stream"):
                            self._send(200, bridge.as_sse(response), "text/event-stream")
                        else:
                            self._send(200, response)
                    else:
                        self._send(404, {"error": {"message": "Unknown endpoint"}})
                except SpendRefused as exc:
                    bridge.refused.append(str(exc))
                    # Non-retriable: do not spend 10 retries on a daily-cap refusal.
                    self._send(402, {"error": {"message": str(exc), "type": "budget_exhausted"}})
                except (ValueError, TypeError, KeyError) as exc:
                    self._send(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
                except (OSError, RuntimeError) as exc:
                    bridge.errors.append(str(exc)[:200])
                    self._send(400, {"error": {"message": str(exc), "type": "upstream_error"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def project_command(self, args):
        args = validate(args, self.command_schema)
        db = DB(self.cfg.db_path)
        try:
            return ChatRouter(db, self.cfg).handle(self.user,
                shlex.join([args["command"]] + args["args"]), self.channel)
        finally:
            db.close()

    def completion(self, payload):
        with self._requests:
            self._active += 1
        try:
            return self._completion(payload)
        finally:
            with self._requests:
                self._active -= 1
                self._requests.notify_all()

    def _completion(self, payload):
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty array")
        primary = any(m.get("role") == "system" and "你是 Omnilaw，运行在用户本机的个人智能体" in str(m.get("content"))
                      for m in messages)
        if primary:
            with self._lock:
                self.last_messages = copy.deepcopy(messages)
        if self.run_cap_usd is not None:
            with self._lock:
                if self.usage["cost_usd"] >= self.run_cap_usd:
                    raise SpendRefused("本次运行费用已达上限 $%.2f。" % self.run_cap_usd)
        db = DB(self.cfg.db_path)
        llm = DeepSeekLLM(self.cfg, guard=SpendGuard(self.cfg, db))
        day = today()
        ledger = DailyLedger(db, self.cfg["daily_budget"])
        try:
            if primary:
                self._checkpoint(db, messages)
            ledger.row()
            cap = self.cfg["daily_budget"].get("max_model_calls")
            sql, args = "UPDATE budget_ledger SET model_calls=model_calls+1 WHERE day=?", [day]
            if cap is not None:
                sql += " AND model_calls < ?"
                args.append(cap)
            if db.execute(sql, args).rowcount != 1:
                raise SpendRefused("今日模型调用次数已用完。", cap_reached=True)
            options = {k: v for k, v in payload.items() if k != "messages"}
            result = llm.complete_raw(messages, **options)
            if primary:
                with self._lock:
                    self.last_messages = copy.deepcopy(messages) + [result["choices"][0]["message"]]
                    self._checkpoint(db, self.last_messages)
            return result
        finally:
            usage = llm.last_usage
            if usage:
                with self._lock:
                    self.usage["calls"] += 1
                    for k in ("input_tokens", "output_tokens", "cost_usd"):
                        self.usage[k] += usage.get(k) or 0
                    self.usage["estimated"] = self.usage["estimated"] or bool(usage.get("estimated"))
            ledger.record({"input_tokens": usage.get("input_tokens", 0),
                           "output_tokens": usage.get("output_tokens", 0)}, tasks=0, day=day)
            db.close()

    def _checkpoint(self, db, messages):
        if self.session is not None:
            db.execute("INSERT INTO agent_sessions(owner,channel,session,messages,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(owner,channel,session) DO UPDATE SET messages=excluded.messages,updated_at=excluded.updated_at",
                (self.user, self.channel, self.session, json.dumps(messages, ensure_ascii=False), now()))

    @staticmethod
    def as_sse(response):
        """SDK-compatible chunks. Upstream is buffered so settlement is atomic."""
        choice = response["choices"][0]
        message = copy.deepcopy(choice["message"])
        for i, call in enumerate(message.get("tool_calls") or []):
            call["index"] = i
        base = {"id": response.get("id", "omnilaw"), "object": "chat.completion.chunk",
                "created": response.get("created", int(time.time())), "model": response.get("model", "")}
        chunks = [dict(base, choices=[{"index": 0, "delta": message, "finish_reason": None}]),
                  dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}],
                       usage=response.get("usage"))]
        return ("".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks)
                + "data: [DONE]\n\n").encode()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # A cancelled SDK request need not cancel the upstream HTTP request.
        # Let its reservation settle before the CLI process exits. A genuine
        # network timeout still retains the documented conservative reservation.
        with self._requests:
            self._requests.wait_for(lambda: self._active == 0,
                timeout=float(self.cfg["model"].get("timeout_seconds", 120)) + 2)
