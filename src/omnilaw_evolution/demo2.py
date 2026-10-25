"""Phase-2 walkthrough (report in Chinese).

Fresh demo instance; everything is driven through the queue, schedules and
chat commands (delivered as simulated Feishu events to the real adapter with a
fake reply sender). Offline by default via the recorded discovery feed;
``live=True`` adds a real incremental search and a real knowledge review.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

from . import taskqueue, versions
from .adapters.feishu import FeishuEventHandler
from .budget import DailyLedger
from .config import load_config, write_config
from .db import DB, loads
from .demo import build_instance
from .discovery import LiveProvider, discover_incremental
from .review import review_sources
from .worker import DaemonRefused, Worker, daemon


class FakeFeishu:
    """Delivers chat text as Feishu im.message.receive_v1 events to the real
    handler and captures what it would reply."""

    def __init__(self, cfg):
        self.replies = []
        self.n = 0
        os.environ.setdefault("FEISHU_VERIFICATION_TOKEN", "demo-token")
        self.handler = FeishuEventHandler(cfg.db_path, cfg, replier=lambda mid, t: self.replies.append(t),
                                          run_async=False)

    def say(self, user, text):
        self.n += 1
        body = {"schema": "2.0", "header": {"event_id": "demo-%d" % self.n, "event_type": "im.message.receive_v1",
                                             "token": os.environ["FEISHU_VERIFICATION_TOKEN"]},
                "event": {"sender": {"sender_id": {"open_id": user}},
                          "message": {"message_id": "om_demo_%d" % self.n, "message_type": "text",
                                      "content": json.dumps({"text": "@_user_1 " + text}, ensure_ascii=False)}}}
        before = len(self.replies)
        self.handler.handle(body)
        return self.replies[before] if len(self.replies) > before else "(无回复)"


def run_demo2(workdir, report_out=None, live=True):
    workdir = os.path.realpath(workdir)
    os.makedirs(workdir, exist_ok=True)
    out = []
    log = out.append
    t0 = time.time()
    instance = os.path.join(workdir, "instance")
    base_sha = build_instance(instance)
    cfg_path = os.path.join(workdir, "omnilaw.json")
    write_config(cfg_path, {
        "project_root": "instance", "state_dir": "state", "experiments_dir": "experiments",
        "research": {"snapshot_manifest": "instance/fixtures/discovery/manifest.json", "max_ingest_per_task": 5},
        "chat": {"allowed_users": ["ou_reader"], "admins": ["ou_owner"]},
    })
    cfg = load_config(cfg_path)
    db = DB(cfg.db_path)
    versions.init_releases(db, cfg)
    feishu = FakeFeishu(cfg)
    worker = Worker(db, cfg, offline=True)
    outbox_seen = [0]

    def chat(user, text):
        reply = feishu.say(user, text)
        log("> **%s**：%s\n>\n> " % (user, text) + reply.replace("\n", "\n> ") + "\n")
        return reply

    def drain(title, at=None):
        done = worker.run(at=at)
        log("**worker：%s**\n" % title)
        log("| task | 类型 | 结果 | 摘要 |")
        log("|---|---|---|---|")
        for d in done:
            if "idle" in d:
                log("| — | — | idle | %s |" % d["idle"])
                continue
            log("| %d | %s | %s | %s |" % (d["task"], d["kind"], d["status"], _brief(d)))
        new = db.all("SELECT text FROM outbox ORDER BY id")[outbox_seen[0]:]
        outbox_seen[0] += len(new)
        if new:
            log("\n通知（模拟通道，写入 outbox）：\n")
            for n in new:
                log("- " + n["text"])
        log("")
        return done

    log("# Omnilaw Evolution —— 第二阶段演示报告\n")
    log("生成时间 %s，命令 `omnilaw-evolution demo2`。" % time.strftime("%Y-%m-%d %H:%M:%S %z"))
    log("模型适配器：**离线模拟**（未调用任何模型 API）；编程执行器：**模板**；来源发现：**录制的 arXiv 检索快照**"
        "（`fixtures/discovery/`，%s）。聊天消息以飞书 `im.message.receive_v1` 事件格式送入真实的飞书适配器，"
        "回复被截获而**没有发往真实飞书**。数据为虚构的小规模标注集，结果只说明机制。\n" % (
            "末尾附真实联网的增量检索与知识复查" if live else "未联网"))
    log("- 演示实例基线提交 `%s`；状态库 schema v%d\n" % (base_sha[:12], db.schema_version()))

    # 1. schedules
    taskqueue.add_schedule(db, "daily-batch-check", "0 8 * * *", "evaluate_release", {"split": "batch2"})
    taskqueue.add_schedule(db, "weekly-review", "0 9 * * 1", "knowledge_review", {"limit": 5})
    db.execute("UPDATE schedules SET created_at='2026-10-25T07:00:00'")
    log("## 1. 定时任务\n")
    for s in db.all("SELECT name, cron, kind FROM schedules"):
        log("- `%s`：`%s` → %s" % (s["name"], s["cron"], s["kind"]))
    log("")

    # 2. user-directed research through chat
    log("## 2. 用户通过聊天指定研究方向\n")
    chat("ou_reader", "研究 news deduplication")
    drain("研究 → 规划 → 影子运行")
    for s in db.all("SELECT id, kind, read_status, flags FROM sources ORDER BY id"):
        flags = [f["kind"] for f in loads(s["flags"])]
        if flags:
            log("- 来源 #%d 被标记注入特征：%s（只作为数据保存，未产生知识，未执行其中命令）" % (s["id"], ", ".join(flags)))
    log("")

    # 3. adoption through chat with confirmation
    log("## 3. 通过聊天采用（需管理员 + 一次性确认码）\n")
    ready = db.one("SELECT id FROM runs WHERE state='READY' ORDER BY id")
    chat("ou_reader", "采用 %d" % ready["id"])
    ask = chat("ou_owner", "采用 %d" % ready["id"])
    code = ask.split("确认 ")[1].split("”")[0]
    chat("ou_owner", "确认 %s" % code)
    chat("ou_owner", "确认 %s" % code)

    # 4. schedule fires -> failure-triggered research
    log("## 4. 定时评测触发失败驱动研究\n")
    fired = taskqueue.tick(db, dt.datetime(2026, 10, 26, 8, 0))
    log("模拟时间 2026-10-26 08:00 调度触发：%s\n" % ", ".join("%s→task %d" % (f["schedule"], f["task"]) for f in fired))
    drain("定时评测 + 失败驱动研究")
    chat("ou_reader", "问题")

    # 5. suppression + cache
    log("## 5. 重复研究抑制与评测缓存\n")
    chat("ou_reader", "研究 news deduplication")
    drain("同主题再次研究")
    fired = taskqueue.tick(db, dt.datetime(2026, 10, 27, 8, 0))
    drain("次日定时评测（结果来自缓存，受阻类别不再重复研究）")
    pid = db.one("SELECT id FROM proposals ORDER BY id")["id"]
    taskqueue.enqueue(db, "run", {"proposal_id": pid}, origin="demo")
    done = drain("同一提案重跑一次")
    rid = [d for d in done if d.get("kind") == "run"][0]["result"]["run_id"]
    ev = db.all("SELECT variant, split, isolation FROM evaluations WHERE run_id=?", (rid,))
    log("该运行被拒绝是预期结果：它的基线是**当前**生效版本（已包含这项改动），F1 增量为 0。")
    log("运行 %d 的 %d 次评测中 %d 次来自缓存（键 = collector 代码去文档字符串后的 AST 哈希 + 评测器版本 + 数据哈希；"
        "缓存命中不计入保留集曝光次数）。\n" % (rid, len(ev), sum(1 for e in ev if "(cached)" in (e["isolation"] or ""))))
    hits = db.one("SELECT COALESCE(SUM(hits),0) AS h, COUNT(*) AS n FROM eval_cache")
    log("缓存条目 %d，累计命中 %d。\n" % (hits["n"], hits["h"]))

    # 6. feedback
    log("## 6. 用户反馈\n")
    chat("ou_reader", "重复 b02 b01")
    chat("ou_reader", "保留 b10")
    chat("ou_reader", "反馈 日报里同一事件的中英文报道请合并")
    chat("ou_reader", "问题")

    # 7. daily budget
    log("## 7. 每日预算账本\n")
    used = DailyLedger(db, cfg["daily_budget"]).row()["tasks"]
    worker.ledger.limits["max_tasks"] = used + 1
    taskqueue.enqueue(db, "research", {"topic": "paraphrase identification", "force": True}, origin="demo")
    taskqueue.enqueue(db, "knowledge_review", {"limit": 3}, origin="demo")
    log("把今日任务上限临时设为 %d（已用 %d），再加入 2 个任务：\n" % (used + 1, used))
    drain("预算受限")
    drain("再次尝试（同一天不重复通知）")
    worker.ledger.limits["max_tasks"] = cfg["daily_budget"]["max_tasks"]
    drain("恢复上限后继续（离线模式下知识复查记为跳过）")
    chat("ou_reader", "预算")

    # 8. rollback through chat
    log("## 8. 通过聊天回滚\n")
    rel = versions.current_release(db)
    ask = chat("ou_owner", "回滚 %d" % rel["id"])
    code = ask.split("确认 ")[1].split("”")[0]
    chat("ou_owner", "确认 %s" % code)
    chat("ou_reader", "状态")

    # 9. daemon gate
    log("## 9. 后台运行开关\n")
    try:
        daemon(db, cfg, max_cycles=1)
    except DaemonRefused as exc:
        log("- 默认配置下守护进程拒绝启动：%s" % exc)
    cfg["background"] = True
    n = daemon(db, cfg, max_cycles=1, sleep=lambda s: None)
    hb = json.load(open(os.path.join(cfg["state_dir"], "daemon", "heartbeat.json")))
    log("- 显式开启 `background` 并设置每日预算后运行 %d 个周期：心跳 %s，处理任务 %d，队列 %s\n" % (
        n, hb["at"], hb["processed"], hb["queue"]))
    cfg["background"] = False

    if live:
        log("## 10. 真实联网：增量检索与知识复查\n")
        prov = LiveProvider(cfg)
        rep = discover_incremental(db, cfg, "paraphrase identification", prov, max_ingest=1)
        log("- 实时检索“paraphrase identification”：返回 %d 条，其中 %d 条此前已见过（不重复下载），新条目 %d 条，"
            "判为不相关 %d 条，本次入库 %d 条，错误 %s。" % (
                rep["found"], len(rep["seen_before"]), len(rep["new"]), len(rep["irrelevant"]),
                len(rep["ingested"]), rep["errors"] or "无"))
        for item in rep["new"]:
            log("  - 新：%s（相关性 %.2f）" % (item["title"][:90], item["relevance"]))
        rv = review_sources(db, cfg, limit=3, older_than_days=0)
        for r in rv:
            log("- 复查 %s → **%s**：%s" % (r["url"][:90], r["status"], r.get("note", "")))
        log("")

    log("## 汇总\n")
    log("| 任务类型 | 状态 | 数量 |")
    log("|---|---|---|")
    for r in db.all("SELECT kind, status, COUNT(*) AS n FROM tasks GROUP BY kind, status ORDER BY kind, status"):
        log("| %s | %s | %d |" % (r["kind"], r["status"], r["n"]))
    log("\n知识（方法 / 验证状态 / 复查状态 / 来源读取级别）：\n")
    for k in db.all("SELECT k.id, k.method, k.verification, k.review_status, s.read_status, s.url FROM knowledge k "
                    "JOIN sources s ON s.id = k.source_id ORDER BY k.id"):
        log("- #%d `%s` %s / %s / %s — %s" % (k["id"], k["method"], k["verification"], k["review_status"],
                                             k["read_status"], k["url"]))
    log("\n总耗时 %.1f 秒。\n" % (time.time() - t0))
    text = "\n".join(out) + "\n"
    for real, alias in ((workdir, "<demo>"), (os.path.expanduser("~"), "~")):
        text = text.replace(real, alias)
    path = os.path.join(workdir, "DEMO2_REPORT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if report_out:
        os.makedirs(report_out, exist_ok=True)
        with open(os.path.join(report_out, "DEMO2_REPORT.md"), "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return path


def _brief(d):
    r = d.get("result") or {}
    if d.get("error"):
        return d["error"][:120]
    k = d["kind"]
    if r.get("reason"):
        return r["reason"]
    if k in ("research", "failure_research"):
        return "发现 %s，新 %d，已见 %d，不相关 %d，入库 %d，新知识 %s" % (
            r.get("found"), len(r.get("new", [])), len(r.get("seen_before", [])), len(r.get("irrelevant", [])),
            len(r.get("ingested", [])), r.get("new_knowledge"))
    if k == "propose":
        return "新提案 %s，已测试跳过 %s，影子运行任务 %s，失败研究任务 %s" % (
            r.get("proposals"), [s["proposal"] for s in r.get("already_tested", [])], r.get("run_tasks"),
            r.get("failure_research_tasks"))
    if k == "run":
        return "运行 %s → %s（缓存命中 %s）" % (r.get("run_id"), r.get("state"), r.get("cache_hits"))
    if k == "evaluate_release":
        m = r.get("metrics") or {}
        return "版本 #%s F1=%s 失败 %d 例%s；失败研究任务 %s" % (r.get("release"), m.get("f1"), len(r.get("failures", [])),
                                                    "（缓存）" if r.get("cached") else "", r.get("failure_research_tasks"))
    if k == "knowledge_review":
        return ", ".join("%s:%s" % (x["source_id"], x["status"]) for x in r.get("reviewed", [])) or "无到期来源"
    return json.dumps(r, ensure_ascii=False)[:120]
