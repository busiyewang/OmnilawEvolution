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
