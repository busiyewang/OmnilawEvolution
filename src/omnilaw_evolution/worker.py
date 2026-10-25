"""Queue worker and daemon.

The worker claims one task at a time, gives it a budget capped by what is left
of today's ledger, runs the handler, records usage, and sends notifications.
Triggers feed the queue from four places: users (CLI/chat), schedules (cron),
failure records (open failure classes without covering knowledge) and other
tasks (research -> propose -> shadow runs).

Repeated research is suppressed at three levels: one live task per dedupe key;
a cooldown per research topic and per failure class; and the planner refusing
to recreate an experiment it already ran.
"""
from __future__ import annotations

import datetime as dt
import signal
import time

from . import runner, taskqueue
from .budget import Budget, BudgetExhausted, DailyLedger
from .db import loads, now
from .discovery import discover_incremental, make_provider, normalise_topic
from .llm import make_llm
from .notify import make_notifier
from .planner import ensure_knowledge, open_problems, propose
from .report import run_report
from .review import review_sources


def _tomorrow():
    return (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


class Worker:
    def __init__(self, db, cfg, notifier=None, provider=None, offline=False):
        self.db, self.cfg = db, cfg
        self.notifier = notifier or make_notifier(db, cfg)
        self.provider = provider
        self.offline = offline
        self.ledger = DailyLedger(db, cfg["daily_budget"])

    # ------------------------------------------------------------ driver
    def run_once(self, at=None):
        """Process at most one task. Returns a dict describing what happened,
        or None when nothing is runnable."""
        exhausted = self.ledger.exhausted()
        if exhausted:
            self.notifier.send("今日预算已用完（%s），队列中的任务顺延到明天。" % exhausted,
                               once_key="daily-exhausted")
            return {"idle": "daily budget exhausted: %s" % exhausted}
        task = taskqueue.claim(self.db, at)
        if not task:
            return None
        budget = Budget(self.ledger.task_limits(self.cfg["budget"]))
        outcome = {"task": task["id"], "kind": task["kind"]}
        try:
            result = getattr(self, "do_" + task["kind"])(task, budget)
            status = result.pop("_status", "done")
            if self.db.get("tasks", task["id"])["cancel_requested"]:
                status = "cancelled"
            taskqueue.finish(self.db, task["id"], status, result=result)
            outcome.update(status=status, result=result)
        except BudgetExhausted as exc:
            if self.ledger.exhausted():
                taskqueue.defer(self.db, task["id"], _tomorrow(), "daily budget: %s" % exc)
                outcome.update(status="deferred", error=str(exc))
            else:
                taskqueue.finish(self.db, task["id"], "failed", error=str(exc))
                outcome.update(status="failed", error=str(exc))
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            outcome.update(status=taskqueue.retry_or_fail(self.db, task, err), error=err)
            if outcome["status"] == "failed":
                self.notifier.send("任务 %d（%s）失败：%s" % (task["id"], task["kind"], err[:300]))
        finally:
            self.ledger.record(budget.usage)
        return outcome

    def run(self, max_tasks=None, at=None):
        done = []
        while max_tasks is None or len(done) < max_tasks:
            out = self.run_once(at)
            if out is None or "idle" in out:
                if out:
                    done.append(out)
                break
            done.append(out)
        return done

    # ------------------------------------------------------------ helpers
    def _recently_done(self, dedupe_key, hours):
        since = (dt.datetime.now() - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        return self.db.one("SELECT id, finished_at FROM tasks WHERE dedupe_key=? AND status='done' "
                           "AND finished_at >= ? ORDER BY id DESC LIMIT 1", (dedupe_key, since))

    def _provider(self, budget):
        return self.provider or make_provider(self.cfg, budget)

    def _research(self, task, budget, topic, key):
        cooldown = self.cfg["research"]["cooldown_hours"]
        prev = self._recently_done(key, cooldown)
        if prev and prev["id"] != task["id"] and not task["payload"].get("force"):
            return {"_status": "skipped", "reason": "topic researched by task %d at %s (cooldown %dh)"
                    % (prev["id"], prev["finished_at"], cooldown)}
        if self.db.get("tasks", task["id"])["dedupe_key"] != key:
            self.db.update("tasks", task["id"], dedupe_key=key)
        report = discover_incremental(self.db, self.cfg, topic, self._provider(budget))
        before = self.db.one("SELECT COUNT(*) AS n FROM knowledge")["n"]
        ensure_knowledge(self.db, make_llm(self.cfg), budget)
        report["new_knowledge"] = self.db.one("SELECT COUNT(*) AS n FROM knowledge")["n"] - before
        if report["new_knowledge"] or task["payload"].get("propose"):
            tid, _ = taskqueue.enqueue(self.db, "propose", {"goal": self.cfg["queue"]["goal"]},
                                       origin="task:%d" % task["id"], priority=40,
                                       dedupe_key="propose", parent_id=task["id"])
            report["propose_task"] = tid
        return report

    def _schedule_failure_research(self, parent_id):
        """Turn open failure classes into research tasks (or a blocked notice)."""
        probs = open_problems(self.db, self.db.all("SELECT * FROM knowledge"), self.cfg)
        queued = []
        for p in probs:
            if p["status"] == "needs_research":
                key = "failure:" + p["case_type"]
                if self._recently_done(key, self.cfg["research"]["cooldown_hours"]):
                    continue
                tid, created = taskqueue.enqueue(self.db, "failure_research",
                                                 {"case_type": p["case_type"], "topic": p["topic"]},
                                                 origin="failure", priority=45, dedupe_key=key, parent_id=parent_id)
                if created:
                    queued.append(tid)
            elif p["status"] == "blocked":
                self.notifier.send(
                    "失败类别 %s（%d 例）已找到相关知识 %s（方法 %s），但当前执行器无法实现：%s"
                    % (p["case_type"], p["count"], p["knowledge_ids"], ", ".join(p["methods"]), p["note"]),
                    once_key="blocked:" + p["case_type"])
        return probs, queued

    # ------------------------------------------------------------ handlers
    def do_research(self, task, budget):
        topic = task["payload"]["topic"]
        return self._research(task, budget, topic, "research:" + normalise_topic(topic))

    def do_failure_research(self, task, budget):
        p = task["payload"]
        report = self._research(task, budget, p["topic"], "failure:" + p["case_type"])
        if report.get("_status") != "skipped":
            probs = open_problems(self.db, self.db.all("SELECT * FROM knowledge"), self.cfg)
            report["after"] = [x for x in probs if x["case_type"] == p["case_type"]]
            self.notifier.send("失败驱动研究（%s，主题“%s”）：发现 %d，新增 %d，入库 %d，新知识 %d 条。" % (
                p["case_type"], p["topic"], report["found"], len(report["new"]), len(report["ingested"]),
                report["new_knowledge"]))
            self._schedule_failure_research(task["id"])
        return report

    def do_propose(self, task, budget):
        goal = task["payload"].get("goal") or self.cfg["queue"]["goal"]
        ids, probs, skipped = propose(self.db, self.cfg, make_llm(self.cfg), budget, goal)
        runs = []
        if self.cfg["queue"]["auto_shadow_runs"]:
            for pid in ids:
                tid, _ = taskqueue.enqueue(self.db, "run", {"proposal_id": pid}, origin="task:%d" % task["id"],
                                           priority=50, dedupe_key="run:proposal:%d" % pid, parent_id=task["id"])
                runs.append(tid)
        _, failure_tasks = self._schedule_failure_research(task["id"])
        return {"proposals": ids, "already_tested": skipped, "open_problems": probs, "run_tasks": runs,
                "failure_research_tasks": failure_tasks}

    def do_run(self, task, budget):
        p = task["payload"]
        if p.get("resume_run_id"):
            rid = p["resume_run_id"]
            self.db.update("tasks", task["id"], run_id=rid)
            state = runner.resume(self.db, self.cfg, rid)
        else:
            limits = {k: budget.limits[k] for k in ("max_model_calls", "max_candidates", "max_download_bytes")
                      if k in budget.limits}
            rid = runner.create_run(self.db, self.cfg, p["proposal_id"], executor=p.get("executor"),
                                    budget_overrides=limits)
            self.db.update("tasks", task["id"], run_id=rid)
            state = runner.Runner(self.db, self.cfg, rid).execute()
        usage = loads(self.db.get("runs", rid)["usage"], {})
        budget.add(**{k: usage.get(k) for k in ("model_calls", "input_tokens", "output_tokens", "candidates",
                                                "sandbox_seconds")})
        run_report(self.db, self.cfg, rid)
        dec = loads(self.db.get("runs", rid)["decision"], {}) or {}
        if state == "READY":
            self.notifier.send("运行 %d 达到采用条件（%s）。回复“采用 %d”并确认后生效；默认不自动采用。"
                               % (rid, "; ".join(dec.get("reasons", [])[:2]), rid))
        elif state in ("REJECTED", "FAILED", "BUDGET_EXHAUSTED", "CANCELLED"):
            self.notifier.send("运行 %d 结束：%s。%s" % (rid, state, "; ".join(dec.get("reasons", [])[:2])
                                                        or (self.db.get("runs", rid)["error"] or "")))
        return {"run_id": rid, "state": state, "cache_hits": usage.get("cache_hits", 0)}

    def do_evaluate_release(self, task, budget):
        split = task["payload"].get("split", "batch2")
        res = runner.evaluate_release(self.db, self.cfg, split)
        fails = [i for i in res["per_item"] if i["outcome"] != "ok"]
        probs, queued = self._schedule_failure_research(task["id"])
        m = res["metrics"] or {}
        self.notifier.send("版本 #%d 在 %s 上：F1 %s，误删 %s，失败 %d 例；新加入失败驱动研究 %d 项。" % (
            res["release_id"], split, m.get("f1"), m.get("false_deletions"), len(fails), len(queued)))
        return {"release": res["release_id"], "metrics": m, "cached": res.get("cached", False),
                "failures": [(i["id"], i["outcome"], i["case"]) for i in fails], "failure_research_tasks": queued}

    def do_knowledge_review(self, task, budget):
        p = task["payload"]
        report = review_sources(self.db, self.cfg, budget, limit=p.get("limit", 5),
                                older_than_days=p.get("older_than_days"), offline=self.offline or p.get("offline"))
        changed = [r for r in report if r["status"] == "changed"]
        if changed:
            ensure_knowledge(self.db, make_llm(self.cfg), budget)
            self.notifier.send("知识复查：%d 个来源有变化，相关知识已标记为“来源已变化”并抽取新版本。" % len(changed))
        return {"reviewed": report}


# ---------------------------------------------------------------- daemon
class DaemonRefused(Exception):
    pass


def daemon(db, cfg, max_cycles=None, sleep=time.sleep):
    """Background loop: schedule tick + drain the queue, every poll interval.
    Refuses to start unless ``background`` is true and a daily budget is set."""
    if not cfg.get("background"):
        raise DaemonRefused("background operation is disabled (set \"background\": true in the config)")
    db_ = cfg.get("daily_budget") or {}
    missing = [k for k in ("max_tasks", "max_model_calls", "max_download_bytes") if db_.get(k) is None]
    if missing:
        raise DaemonRefused("daily_budget must set %s before background operation" % ", ".join(missing))
    stop = {"flag": False}

    def _term(signum, frame):
        stop["flag"] = True
    old = signal.signal(signal.SIGTERM, _term)
    recovered = taskqueue.recover_stale(db)
    worker = Worker(db, cfg)
    cycles = 0
    try:
        while not stop["flag"] and (max_cycles is None or cycles < max_cycles):
            fired = taskqueue.tick(db)
            processed = worker.run(max_tasks=db_["max_tasks"])
            taskqueue.heartbeat(cfg, {"cycle": cycles, "fired": fired, "processed": len(processed),
                                      "recovered": recovered, "queue": taskqueue.summary(db)})
            cycles += 1
            waited = 0.0
            while (max_cycles is None or cycles < max_cycles) and not stop["flag"] \
                    and waited < cfg["queue"]["poll_seconds"]:
                sleep(1.0)  # short steps so SIGTERM is honoured promptly
                waited += 1.0
    finally:
        signal.signal(signal.SIGTERM, old)
    return cycles
