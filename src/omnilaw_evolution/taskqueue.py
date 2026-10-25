"""Persistent task queue (SQLite).

Task kinds: research, failure_research, knowledge_review, propose, run,
evaluate_release. A task is claimed atomically, retried with back-off up to
``max_attempts``, and can be cancelled. ``dedupe_key`` keeps at most one live
task per key, which is the first layer of repeated-research suppression.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from .db import loads, now

KINDS = ("research", "failure_research", "knowledge_review", "propose", "run", "evaluate_release", "journal")
LIVE = ("queued", "running", "deferred")
FINAL = ("done", "failed", "cancelled", "skipped")


class QueueError(Exception):
    pass


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def enqueue(db, kind, payload=None, origin="user", priority=50, dedupe_key=None, not_before=None,
            max_attempts=2, parent_id=None):
    """Add a task. Returns (task_id, created). If a live task with the same
    dedupe_key exists, returns it instead of creating a duplicate."""
    if kind not in KINDS:
        raise QueueError("unknown task kind %r" % kind)
    if dedupe_key:
        live = db.one("SELECT id FROM tasks WHERE dedupe_key=? AND status IN ('queued','running','deferred')",
                      (dedupe_key,))
        if live:
            return live["id"], False
    tid = db.insert("tasks", {"kind": kind, "payload": payload or {}, "origin": origin, "priority": priority,
                              "dedupe_key": dedupe_key, "not_before": not_before, "max_attempts": max_attempts,
                              "parent_id": parent_id, "created_at": now(), "updated_at": now()})
    return tid, True


def claim(db, at=None):
    """Atomically take the highest-priority runnable task (lower number first)."""
    at = _iso(at or dt.datetime.now())
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.one("SELECT * FROM tasks WHERE status IN ('queued','deferred') AND cancel_requested=0 "
                     "AND (not_before IS NULL OR not_before <= ?) ORDER BY priority, id LIMIT 1", (at,))
        if row:
            db.execute("UPDATE tasks SET status='running', attempts=attempts+1, started_at=?, updated_at=? "
                       "WHERE id=?", (now(), now(), row["id"]))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    if row:
        row = db.get("tasks", row["id"])
        row["payload"] = loads(row["payload"], {})
    return row


def finish(db, task_id, status, result=None, error=None):
    assert status in FINAL
    db.update("tasks", task_id, status=status, result=result, error=error, finished_at=now(), updated_at=now())


def retry_or_fail(db, task, error, backoff_seconds=300):
    if task["attempts"] < task["max_attempts"]:
        nb = dt.datetime.now() + dt.timedelta(seconds=backoff_seconds * task["attempts"])
        db.update("tasks", task["id"], status="queued", not_before=_iso(nb), error=error, updated_at=now())
        return "queued"
    finish(db, task["id"], "failed", error=error)
    return "failed"


def defer(db, task_id, until, reason):
    db.update("tasks", task_id, status="deferred", not_before=_iso(until), error=reason, updated_at=now())


def cancel(db, task_id):
    """Cancel a task. A running task gets ``cancel_requested``; the worker (and
    the run it drives) stop at the next checkpoint."""
    t = db.get("tasks", task_id)
    if not t:
        raise QueueError("no task %s" % task_id)
    if t["status"] in FINAL:
        return t["status"]
    if t["status"] == "running":
        db.update("tasks", task_id, cancel_requested=1, updated_at=now())
        if t["run_id"]:
            from .runner import request_stop
            request_stop(db, t["run_id"])
        return "cancel_requested"
    finish(db, task_id, "cancelled", error="cancelled before start")
    return "cancelled"


def recover_stale(db):
    """Tasks left 'running' by a worker that died go back to the queue."""
    n = 0
    for t in db.all("SELECT id FROM tasks WHERE status='running'"):
        db.update("tasks", t["id"], status="queued", error="worker exited while running", updated_at=now())
        n += 1
    return n


def summary(db):
    rows = db.all("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def listing(db, limit=20, status=None):
    sql = "SELECT id, kind, status, origin, priority, dedupe_key, not_before, attempts, run_id, error, created_at " \
          "FROM tasks"
    args = ()
    if status:
        sql += " WHERE status=?"
        args = (status,)
    return db.all(sql + " ORDER BY id DESC LIMIT %d" % int(limit), args)


# ---------------------------------------------------------------- cron
def _field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b:
            raise ValueError("cron field out of range: %s" % spec)
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr):
    """Five-field cron: minute hour day-of-month month day-of-week (0=Sunday).
    Supports ``*``, lists, ranges and steps. Day-of-month and day-of-week are
    combined with OR when both are restricted (as in Vixie cron)."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron needs 5 fields: %r" % expr)
    m, h, dom, mon, dow = parts
    return {"minute": _field(m, 0, 59), "hour": _field(h, 0, 23), "dom": _field(dom, 1, 31),
            "month": _field(mon, 1, 12), "dow": {d % 7 for d in _field(dow, 0, 7)},
            "dom_any": dom == "*", "dow_any": dow == "*"}


def cron_matches(c, t):
    if t.minute not in c["minute"] or t.hour not in c["hour"] or t.month not in c["month"]:
        return False
    dom_ok = t.day in c["dom"]
    dow_ok = (t.isoweekday() % 7) in c["dow"]
    if c["dom_any"] or c["dow_any"]:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def next_fire(expr, after):
    c = parse_cron(expr)
    t = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if cron_matches(c, t):
            return t
        t += dt.timedelta(minutes=1)
    raise ValueError("cron never fires: %r" % expr)


def add_schedule(db, name, cron, kind, payload=None):
    parse_cron(cron)
    if kind not in KINDS:
        raise QueueError("unknown task kind %r" % kind)
    if db.one("SELECT id FROM schedules WHERE name=?", (name,)):
        db.execute("UPDATE schedules SET cron=?, kind=?, payload=?, enabled=1 WHERE name=?",
                   (cron, kind, json.dumps(payload or {}, ensure_ascii=False), name))
        return db.one("SELECT id FROM schedules WHERE name=?", (name,))["id"]
    return db.insert("schedules", {"name": name, "cron": cron, "kind": kind, "payload": payload or {},
                                   "created_at": now()})


def tick(db, at=None):
    """Enqueue every schedule whose cron fired since its last firing (at most
    once per schedule per tick, so a long pause does not create a backlog)."""
    at = (at or dt.datetime.now()).replace(second=0, microsecond=0)
    fired = []
    for s in db.all("SELECT * FROM schedules WHERE enabled=1"):
        last = dt.datetime.strptime(s["last_fired_at"], "%Y-%m-%dT%H:%M:%S") if s["last_fired_at"] \
            else dt.datetime.strptime(s["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
        if next_fire(s["cron"], last) <= at:
            tid, created = enqueue(db, s["kind"], loads(s["payload"], {}), origin="schedule:%s" % s["name"],
                                   dedupe_key="schedule:%s" % s["name"])
            db.execute("UPDATE schedules SET last_fired_at=? WHERE id=?", (_iso(at), s["id"]))
            fired.append({"schedule": s["name"], "task": tid, "new": created})
    return fired


def heartbeat(cfg, info):
    path = os.path.join(cfg.subdir("daemon"), "heartbeat.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(info, at=now(), pid=os.getpid()), f)
    return path
