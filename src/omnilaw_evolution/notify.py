"""Notifications. Every message is first written to the ``outbox`` table (the
audit trail and what the mock channel shows); delivery is then attempted by
the configured channel. Channels: mock (outbox only), stdout, feishu_webhook."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

from .db import now


class Notifier:
    channel = "mock"

    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg

    def send(self, text, target=None, once_key=None):
        """Queue and deliver a message. ``once_key`` suppresses repeats of the
        same notification within a day."""
        if once_key:
            since = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
            if self.db.one("SELECT id FROM outbox WHERE target=? AND created_at >= ?", ("once:" + once_key, since)):
                return None
            target = "once:" + once_key
        oid = self.db.insert("outbox", {"channel": self.channel, "target": target, "text": text,
                                        "created_at": now()})
        try:
            self.deliver(text)
            self.db.update("outbox", oid, status="sent", sent_at=now())
        except Exception as exc:  # delivery failure must not break the worker
            self.db.update("outbox", oid, status="failed", error="%s: %s" % (type(exc).__name__, exc))
        return oid

    def deliver(self, text):
        pass  # mock: the outbox row is the delivery


class StdoutNotifier(Notifier):
    channel = "stdout"

    def deliver(self, text):
        print("[omnilaw] " + text, file=sys.stderr)


def feishu_sign(timestamp, secret):
    """Feishu custom-bot signature: base64(HMAC-SHA256(key=f"{ts}\\n{secret}", msg=b""))."""
    key = ("%s\n%s" % (timestamp, secret)).encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", digestmod=hashlib.sha256).digest()).decode()


class FeishuWebhookNotifier(Notifier):
    """Feishu (Lark) custom bot webhook. URL and optional signing secret come
    from environment variables named in config; nothing secret is stored."""
    channel = "feishu_webhook"

    def deliver(self, text):
        nc = self.cfg["notify"]
        url = os.environ.get(nc["webhook_url_env"])
        if not url:
            raise RuntimeError("env %s not set" % nc["webhook_url_env"])
        body = {"msg_type": "text", "content": {"text": text}}
        secret = os.environ.get(nc.get("webhook_secret_env") or "")
        if secret:
            ts = str(int(time.time()))
            body.update(timestamp=ts, sign=feishu_sign(ts, secret))
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            reply = json.loads(resp.read().decode("utf-8") or "{}")
        if reply.get("code", 0) not in (0, None):
            raise RuntimeError("feishu webhook error: %s" % reply)


def make_notifier(db, cfg):
    kind = cfg["notify"]["kind"]
    return {"mock": Notifier, "stdout": StdoutNotifier, "feishu_webhook": FeishuWebhookNotifier}[kind](db, cfg)
