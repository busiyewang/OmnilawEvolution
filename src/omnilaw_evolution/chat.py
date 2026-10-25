"""Channel-independent chat commands. Every adapter (local CLI, Feishu, a
future Hermes bridge) passes ``(user, text)`` here and gets a reply string, so
there is one set of state logic for all channels.

Permissions: read-only commands need the user in ``chat.allowed_users``;
stop/cancel/resume need ``chat.admins``; apply/rollback additionally need a
one-time confirmation code. The local CLI user is an admin.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import time

from . import runner, taskqueue, versions
from .budget import DailyLedger
from .db import loads, now
from .discovery import normalise_topic

ALIASES = {
    "帮助": "help", "状态": "status", "队列": "queue", "运行": "runs", "报告": "report", "研究": "research",
    "反馈": "feedback", "重复": "dup", "保留": "keep", "停止": "stop", "取消": "cancel", "恢复": "resume",
    "采用": "apply", "回滚": "rollback", "确认": "confirm", "版本": "releases", "预算": "budget",
    "问题": "problems", "日志": "journal", "日报": "journal",
}
READ = {"help", "status", "queue", "runs", "report", "releases", "budget", "problems", "journal"}
WRITE = {"research", "feedback", "dup", "keep"}          # allowed users
ADMIN = {"stop", "cancel", "resume", "apply", "rollback", "confirm"}
CONFIRM = {"apply", "rollback"}

HELP = """可用命令（中英文均可）：
  状态 status | 队列 queue | 运行 runs | 报告 report <run> | 版本 releases | 预算 budget | 问题 problems
  日志 journal [weekly]  自上次日报（或周报）以来的研究与代码变更摘要
  研究 research <主题>            —— 加入研究任务
  反馈 feedback <文字>            —— 记录反馈
  重复 dup <条目> <原条目>        —— 标注漏掉的重复
  保留 keep <条目>                —— 标注不该删除的条目
  停止 stop <run> | 取消 cancel <task> | 恢复 resume <run>     （管理员）
  采用 apply <run> | 回滚 rollback <release> → 需再发送“确认 <码>”（管理员）"""


class ChatRouter:
    def __init__(self, db, cfg, notifier=None, local_admin="local"):
        self.db, self.cfg, self.notifier = db, cfg, notifier
        self.local_admin = local_admin

    # -- permissions
    def role(self, user, channel):
        cc = self.cfg["chat"]
        if channel == "cli" and user == self.local_admin:
            return "admin"
        if user in cc.get("admins", []):
            return "admin"
        if user in cc.get("allowed_users", []):
            return "user"
        return None

    def handle(self, user, text, channel="cli"):
        try:
            parts = shlex.split(text.strip())
        except ValueError:
            parts = text.strip().split()
        if not parts:
            return HELP
        cmd = ALIASES.get(parts[0], parts[0].lower())
        args = parts[1:]
        role = self.role(user, channel)
        if role is None:
            return "未授权：用户 %s 不在 chat.allowed_users / chat.admins 中。" % user
        if cmd in ADMIN and role != "admin":
            return "权限不足：%s 需要管理员。" % cmd
        if cmd not in READ | WRITE | ADMIN:
            return "未知命令 %r。\n%s" % (parts[0], HELP)
        if cmd in CONFIRM:
            return self._ask_confirm(user, cmd, args)
        try:
            return getattr(self, "cmd_" + cmd)(user, args, channel)
        except (runner.RunError, taskqueue.QueueError, ValueError, RuntimeError, IndexError) as exc:
            return "失败：%s" % exc

    # -- confirmation
    def _ask_confirm(self, user, cmd, args):
        if len(args) != 1 or not args[0].isdigit():
            return "用法：%s <编号>" % cmd
        code = secrets.token_hex(3)
        ttl = self.cfg["chat"].get("confirm_ttl_seconds", 600)
        self.db.insert("chat_confirmations", {"code": code, "user": user, "command": json.dumps([cmd, args[0]]),
                                              "created_at": now(), "expires_at": time.time() + ttl})
        what = "采用运行 %s 为新版本" % args[0] if cmd == "apply" else "回滚版本 %s" % args[0]
        return "即将%s。%d 秒内回复“确认 %s”执行。" % (what, ttl, code)

    def cmd_confirm(self, user, args, channel):
        if not args:
            return "用法：确认 <码>"
        row = self.db.one("SELECT * FROM chat_confirmations WHERE code=?", (args[0],))
        if not row or row["used"] or row["user"] != user or row["expires_at"] < time.time():
            return "确认码无效、已使用、已过期或不属于你。"
        self.db.execute("UPDATE chat_confirmations SET used=1 WHERE code=?", (args[0],))
        cmd, target = json.loads(row["command"])
        if cmd == "apply":
            rel = runner.apply_run(self.db, self.cfg, int(target), policy_name="chat:%s" % user)
            return "已采用：运行 %s → 版本 #%d (%s)，健康检查通过。" % (target, rel["id"], rel["sha"][:12])
        checks = runner.rollback_release(self.db, self.cfg, int(target))
        return "已回滚版本 %s。校验：SHA=%s 依赖=%s 状态兼容=%s 健康=%s；当前 %s" % (
            target, checks["sha_matches"], checks["deps_hash_matches"], checks["state_schema_compatible"],
            checks["healthy"], versions.head_sha(versions.active_path(self.cfg))[:12])

    # -- read-only
    def cmd_help(self, user, args, channel):
        return HELP

    def cmd_status(self, user, args, channel):
        rel = versions.current_release(self.db)
        q = taskqueue.summary(self.db)
        runs = self.db.all("SELECT id, state FROM runs ORDER BY id DESC LIMIT 5")
        ready = self.db.all("SELECT id FROM runs WHERE state='READY'")
        lines = ["当前版本：#%s %s" % (rel["id"], rel["sha"][:12]) if rel else "尚未初始化版本",
                 "队列：%s" % (", ".join("%s=%d" % kv for kv in sorted(q.items())) or "空"),
                 "最近运行：%s" % (", ".join("#%d %s" % (r["id"], r["state"]) for r in runs) or "无")]
        if ready:
            lines.append("待采用：%s（回复“采用 <run>”）" % ", ".join("#%d" % r["id"] for r in ready))
        left = DailyLedger(self.db, self.cfg["daily_budget"]).remaining()
        lines.append("今日剩余预算：%s" % ", ".join("%s=%s" % kv for kv in sorted(left.items())))
        return "\n".join(lines)

    def cmd_queue(self, user, args, channel):
        rows = taskqueue.listing(self.db, limit=10)
        if not rows:
            return "队列为空。"
        return "\n".join("task %d %s [%s] %s%s" % (r["id"], r["kind"], r["status"], r["origin"],
                                                   (" — " + r["error"][:80]) if r["error"] else "") for r in rows)

    def cmd_runs(self, user, args, channel):
        rows = self.db.all("SELECT id, proposal_id, executor, state FROM runs ORDER BY id DESC LIMIT 10")
        return "\n".join("run %d proposal %d %s → %s" % (r["id"], r["proposal_id"], r["executor"], r["state"])
                         for r in rows) or "暂无运行。"

    def cmd_report(self, user, args, channel):
        run = self.db.get("runs", int(args[0]))
        if not run:
            return "没有运行 %s" % args[0]
        dec = loads(run["decision"], {}) or {}
        ck = loads(run["checkpoint"], {})
        hold = ck.get("holdout", {})
        lines = ["运行 %d：%s" % (run["id"], run["state"])]
        if hold.get("candidate", {}).get("metrics"):
            b, c = hold["baseline"]["metrics"], hold["candidate"]["metrics"]
            lines.append("保留集 F1 %.3f → %.3f，误删 %d，关键条目丢失 %s" % (
                b["f1"], c["f1"], c["false_deletions"], c["critical_lost"] or "无"))
        lines += ["- " + r for r in dec.get("reasons", [])[:5]]
        if run["error"]:
            lines.append("错误：" + run["error"])
        path = os.path.join(self.cfg["state_dir"], "reports", "run-%d.md" % run["id"])
        lines.append("完整报告：%s" % ("已生成" if os.path.exists(path) else "运行 `omnilaw-evolution report --run %d`"
                                       % run["id"]))
        return "\n".join(lines)

    def cmd_releases(self, user, args, channel):
        rows = self.db.all("SELECT id, sha, status, run_id FROM releases ORDER BY id DESC LIMIT 10")
        return "\n".join("#%d %s %s (run %s)" % (r["id"], r["sha"][:12], r["status"], r["run_id"] or "-")
                         for r in rows) or "暂无版本。"

    def cmd_budget(self, user, args, channel):
        ledger = DailyLedger(self.db, self.cfg["daily_budget"])
        row = ledger.row()
        return "今日用量：任务 %d，模型调用 %d，下载 %d 字节，候选 %d；剩余：%s" % (
            row["tasks"], row["model_calls"], row["download_bytes"], row["candidates"],
            ", ".join("%s=%s" % kv for kv in sorted(ledger.remaining().items())))

    def cmd_journal(self, user, args, channel):
        from . import journal
        period = "weekly" if args and args[0].lower() in ("weekly", "周", "周报") else "daily"
        return "自上次%s以来：\n%s" % ("周报" if period == "weekly" else "日报", journal.preview(self.db, self.cfg, period))

    def cmd_problems(self, user, args, channel):
        from .planner import open_problems
        probs = open_problems(self.db, self.db.all("SELECT * FROM knowledge"), self.cfg)
        if not probs:
            return "没有未解决的失败类别。"
        return "\n".join("%s ×%d [%s] %s" % (p["case_type"], p["count"], p["status"], p.get("note", ""))
                         for p in probs)

    # -- writes
    def cmd_research(self, user, args, channel):
        topic = " ".join(args).strip()
        if not topic:
            return "用法：研究 <主题>"
        tid, created = taskqueue.enqueue(self.db, "research", {"topic": topic}, origin="chat:%s" % user,
                                         priority=30, dedupe_key="research:" + normalise_topic(topic))
        return ("已加入研究任务 task %d：%s" if created else "同主题任务 task %d 已在队列中：%s") % (tid, topic)

    def _feedback(self, user, text, item=None, label=None, channel="cli"):
        fid = self.db.insert("feedback", {"origin": channel, "user": user, "text": text, "item_id": item,
                                          "label": label, "created_at": now()})
        if item and label:
            # A user label is a failure record planning can see (split 'user').
            kind = "missed-duplicate" if label.startswith("duplicate-of:") else "false-deletion"
            runner._add_failure(self.db, {"split": "user", "item_id": item, "kind": kind,
                                          "case_type": "user-reported", "detail": "%s (feedback #%d by %s)"
                                          % (label, fid, user)})
            path = os.path.join(self.cfg.subdir("feedback"), "cases.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"feedback_id": fid, "item_id": item, "label": label, "user": user,
                                    "at": now()}, ensure_ascii=False) + "\n")
        return fid

    def cmd_feedback(self, user, args, channel):
        text = " ".join(args).strip()
        if not text:
            return "用法：反馈 <文字>"
        return "已记录反馈 #%d。" % self._feedback(user, text, channel=channel)

    def cmd_dup(self, user, args, channel):
        if len(args) != 2:
            return "用法：重复 <条目> <原条目>"
        fid = self._feedback(user, "duplicate: %s of %s" % tuple(args), args[0], "duplicate-of:" + args[1], channel)
        return "已记录 #%d：%s 是 %s 的重复（待人工审核后可并入评测集）。" % (fid, args[0], args[1])

    def cmd_keep(self, user, args, channel):
        if len(args) != 1:
            return "用法：保留 <条目>"
        fid = self._feedback(user, "keep: %s" % args[0], args[0], "keep", channel)
        return "已记录 #%d：%s 不应被删除。" % (fid, args[0])

    # -- admin
    def cmd_stop(self, user, args, channel):
        r = runner.request_stop(self.db, int(args[0]))
        return "已请求停止运行 %s（当前状态 %s）。" % (args[0], r["state"])

    def cmd_cancel(self, user, args, channel):
        return "task %s：%s" % (args[0], taskqueue.cancel(self.db, int(args[0])))

    def cmd_resume(self, user, args, channel):
        run = self.db.get("runs", int(args[0]))
        if not run or run["state"] not in runner.RESUMABLE:
            return "运行 %s 不可恢复。" % args[0]
        tid, _ = taskqueue.enqueue(self.db, "run", {"resume_run_id": int(args[0])}, origin="chat:%s" % user,
                                   priority=20, dedupe_key="resume:%s" % args[0])
        return "已加入恢复任务 task %d（运行 %s，沿用累计预算）。" % (tid, args[0])
