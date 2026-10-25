"""Budget enforcement. Runs in the orchestrator, outside candidate code.
Usage is cumulative across stop/resume: a resumed run starts from the usage
stored in its row, not from zero."""
from __future__ import annotations

import time


class BudgetExhausted(Exception):
    def __init__(self, kind, used, limit):
        super().__init__("budget exhausted: %s used %s of %s" % (kind, used, limit))
        self.kind, self.used, self.limit = kind, used, limit


LIMIT_KEYS = {
    "model_calls": "max_model_calls",
    "candidates": "max_candidates",
    "download_bytes": "max_download_bytes",
}


class Budget:
    def __init__(self, limits, usage=None, on_change=None):
        self.limits = dict(limits)
        self.usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "candidates": 0,
                      "download_bytes": 0, "wall_seconds": 0.0, "sandbox_seconds": 0.0,
                      "cost_usd": None, "cost_note": "unknown price; tokens reported instead"}
        self.usage.update(usage or {})
        self._started = time.time()
        self._wall_base = float(self.usage.get("wall_seconds", 0.0))
        self.on_change = on_change

    def _limit(self, kind):
        return self.limits.get(LIMIT_KEYS[kind])

    def check(self, kind, amount=1):
        """Raise before spending if the charge would exceed the limit."""
        self.tick()
        limit = self._limit(kind)
        if limit is not None and self.usage[kind] + amount > limit:
            raise BudgetExhausted(kind, self.usage[kind], limit)

    def charge(self, kind, amount=1):
        self.check(kind, amount)
        self.usage[kind] += amount
        self._changed()

    def add(self, **amounts):
        for k, v in amounts.items():
            if v is None:
                continue
            self.usage[k] = (self.usage.get(k) or 0) + v
        self._changed()

    def tick(self):
        self.usage["wall_seconds"] = round(self._wall_base + time.time() - self._started, 3)
        limit = self.limits.get("max_wall_seconds")
        if limit is not None and self.usage["wall_seconds"] > limit:
            raise BudgetExhausted("wall_seconds", self.usage["wall_seconds"], limit)

    def remaining_wall(self):
        limit = self.limits.get("max_wall_seconds")
        if limit is None:
            return None
        return max(0.0, limit - (self._wall_base + time.time() - self._started))

    def _changed(self):
        if self.on_change:
            self.on_change(self.usage)


# ------------------------------------------------------------ daily ledger
DAILY_FIELDS = ("model_calls", "input_tokens", "output_tokens", "download_bytes", "candidates",
                "sandbox_seconds", "tasks")
DAILY_LIMITS = {"model_calls": "max_model_calls", "download_bytes": "max_download_bytes",
                "candidates": "max_candidates", "tasks": "max_tasks"}


def today():
    return time.strftime("%Y-%m-%d")


class DailyLedger:
    """Cross-task usage per calendar day. The worker checks it before claiming
    work and records each task's usage after; background operation therefore
    stops at the configured daily limit rather than running unbounded."""

    def __init__(self, db, limits):
        self.db, self.limits = db, dict(limits or {})

    def row(self, day=None):
        day = day or today()
        self.db.execute("INSERT OR IGNORE INTO budget_ledger(day) VALUES (?)", (day,))
        return self.db.one("SELECT * FROM budget_ledger WHERE day=?", (day,))

    def remaining(self, day=None):
        r = self.row(day)
        out = {}
        for field, key in DAILY_LIMITS.items():
            if self.limits.get(key) is not None:
                out[field] = self.limits[key] - (r[field] or 0)
        return out

    def exhausted(self, day=None):
        """Name of the first exhausted daily limit, or None."""
        for field, left in self.remaining(day).items():
            if left <= 0:
                return field
        return None

    def task_limits(self, base):
        """Per-task budget limits capped by what is left today."""
        limits = dict(base)
        left = self.remaining()
        for field, key in (("model_calls", "max_model_calls"), ("download_bytes", "max_download_bytes"),
                           ("candidates", "max_candidates")):
            if field in left:
                limits[key] = max(0, min(limits.get(key, left[field]), left[field]))
        return limits

    def record(self, usage, tasks=1, day=None):
        self.row(day)
        sets = ", ".join("%s = %s + ?" % (f, f) for f in DAILY_FIELDS)
        vals = [(usage.get(f) or 0) for f in DAILY_FIELDS[:-1]] + [tasks]
        self.db.execute("UPDATE budget_ledger SET %s WHERE day=?" % sets, vals + [day or today()])
