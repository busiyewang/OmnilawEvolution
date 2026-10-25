"""Feishu (Lark) inbound adapter: event-subscription callback -> ChatRouter.

Supported: plain-text ``im.message.receive_v1`` events (schema 2.0), URL
verification, verification-token check, retry de-duplication by event id, and
replies through the Open API (``im/v1/messages/{id}/reply``) with a tenant
access token from app credentials. Encrypted events (Encrypt Key) need AES and
are not supported by this stdlib build: leave encryption off in the Feishu
console and rely on the verification token plus HTTPS in front of this server.

Long-connection mode (``run_long_connection``) receives the same events over
Feishu's WebSocket channel through the official ``lark-oapi`` SDK (optional
extra ``feishu``): no public HTTPS endpoint and no encryption setting needed;
the connection itself is authenticated with the app credentials.

Credentials are read from the environment variables named in
``chat.feishu``; nothing is stored in config or logs.

Not exercised against a real Feishu tenant in this repository (no app
credentials); tests drive it with recorded-shape events and a fake replier.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..db import DB, now
from ..chat import ChatRouter

API = "https://open.feishu.cn/open-apis"


class FeishuAPI:
    def __init__(self, cfg):
        fc = cfg["chat"]["feishu"]
        self.app_id = os.environ.get(fc["app_id_env"])
        self.app_secret = os.environ.get(fc["app_secret_env"])
        self._token, self._exp = None, 0

    def _post(self, path, body, token=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        if out.get("code", 0) != 0:
            raise RuntimeError("feishu api %s: %s" % (path, out.get("msg")))
        return out

    def token(self):
        if not (self.app_id and self.app_secret):
            raise RuntimeError("feishu app credentials not set in environment")
        if not self._token or time.time() > self._exp - 60:
            out = self._post("/auth/v3/tenant_access_token/internal",
                             {"app_id": self.app_id, "app_secret": self.app_secret})
            self._token, self._exp = out["tenant_access_token"], time.time() + out.get("expire", 3600)
        return self._token

    def reply(self, message_id, text):
        self._post("/im/v1/messages/%s/reply" % message_id,
                   {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}, self.token())


class FeishuEventHandler:
    def __init__(self, db_path, cfg, replier=None, run_async=True):
        self.db_path, self.cfg = db_path, cfg
        self.replier = replier or FeishuAPI(cfg).reply
        self.run_async = run_async
        self.token = os.environ.get(cfg["chat"]["feishu"]["verification_token_env"])

    def handle(self, body, trusted=False):
        """Return (http_status, response_dict). ``trusted`` is set for events that
        arrived over the authenticated long connection (no verification token)."""
        if "encrypt" in body:
            return 400, {"error": "encrypted events are not supported; disable Encrypt Key"}
        if body.get("type") == "url_verification":
            if not self.token or body.get("token") != self.token:
                return 403, {"error": "bad token"}
            return 200, {"challenge": body.get("challenge")}
        header = body.get("header") or {}
        if not trusted and (not self.token or header.get("token") != self.token):
            return 403, {"error": "bad token"}
        if header.get("event_type") != "im.message.receive_v1":
            return 200, {"ignored": header.get("event_type")}
        db = DB(self.db_path)
        try:
            eid = header.get("event_id") or ""
            if db.one("SELECT event_id FROM chat_events WHERE event_id=?", (eid,)):
                return 200, {"duplicate": eid}
            db.insert("chat_events", {"event_id": eid, "created_at": now()})
        finally:
            db.close()
        event = body.get("event") or {}
        msg = event.get("message") or {}
        user = ((event.get("sender") or {}).get("sender_id") or {}).get("open_id", "")
        if msg.get("message_type") != "text":
            return 200, {"ignored": "non-text message"}
        text = json.loads(msg.get("content") or "{}").get("text", "")
        text = re.sub(r"@_user_\d+\s*", "", text).strip()  # drop @bot mentions
        job = lambda: self._respond(user, text, msg.get("message_id"))  # noqa: E731
        if self.run_async:  # Feishu expects an answer within 3 s; long commands run after the 200
            threading.Thread(target=job, daemon=True).start()
        else:
            job()
        return 200, {"ok": True}

    def _respond(self, user, text, message_id):
        db = DB(self.db_path)
        try:
            reply = ChatRouter(db, self.cfg).handle(user, text, channel="feishu")
        finally:
            db.close()
        self.replier(message_id, reply)


def make_server(cfg, host="127.0.0.1", port=8787, handler=None):
    handler = handler or FeishuEventHandler(cfg.db_path, cfg)

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != "/feishu/events":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1000000:
                self.send_error(413)
                return
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                self.send_error(400)
                return
            status, out = handler.handle(body)
            data = json.dumps(out, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # keep request bodies out of logs
            pass

    return ThreadingHTTPServer((host, port), H)


def run_long_connection(cfg, handler=None, client_factory=None):
    """Receive message events over Feishu's long connection (blocks)."""
    fc = cfg["chat"]["feishu"]
    app_id, secret = os.environ.get(fc["app_id_env"]), os.environ.get(fc["app_secret_env"])
    if not (app_id and secret):
        raise RuntimeError("feishu app credentials not set (%s / %s)" % (fc["app_id_env"], fc["app_secret_env"]))
    import lark_oapi as lark  # optional dependency: pip install -e '.[feishu]'
    handler = handler or FeishuEventHandler(cfg.db_path, cfg)

    def on_message(data):
        handler.handle(json.loads(lark.JSON.marshal(data)), trusted=True)

    dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    factory = client_factory or (lambda: lark.ws.Client(app_id, secret, event_handler=dispatcher,
                                                        log_level=lark.LogLevel.INFO))
    client = factory()
    client.start()
    return dispatcher
