"""Paid model spend. Every paid call is priced from the configured per-MTok
rates and checked against the daily dollar cap *before* it is sent, using a
worst-case estimate (counted input tokens + the full output allowance).
Actual cost is recorded in the daily ledger afterwards.

An unknown price is never treated as zero: without ``model.price_per_mtok``
paid calls are refused, and so are all calls when ``max_daily_cost_usd`` is 0.
"""
from __future__ import annotations

from .budget import today
from .db import DB


class SpendRefused(RuntimeError):
    """``cap_reached`` is true when waiting for tomorrow helps; false for a
    configuration problem (no price, zero cap) that needs a human."""

    def __init__(self, message, cap_reached=False):
        super().__init__(message)
        self.cap_reached = cap_reached


def price(cfg):
    """(input_usd_per_mtok, output_usd_per_mtok) or None when not configured."""
    p = (cfg.get("model") or {}).get("price_per_mtok")
    if not isinstance(p, dict):
        return None
    try:
        return float(p["input"]), float(p["output"])
    except (KeyError, TypeError, ValueError):
        return None


class SpendGuard:
    def __init__(self, cfg, db=None):
        self.cap = float(cfg["budget"].get("max_daily_cost_usd") or 0)
        self.price = price(cfg)
        self.db = db or DB(cfg.db_path)

    def cost(self, input_tokens, output_tokens):
        pin, pout = self.price
        return round(((input_tokens or 0) * pin + (output_tokens or 0) * pout) / 1e6, 6)

    def spent_today(self):
        self.db.execute("INSERT OR IGNORE INTO budget_ledger(day) VALUES (?)", (today(),))
        return float(self.db.one("SELECT cost_usd FROM budget_ledger WHERE day=?", (today(),))["cost_usd"] or 0)

    def remaining(self):
        return max(0.0, self.cap - self.spent_today())

    def check(self, worst_case_usd, need_price=True):
        """Raise unless a call costing up to ``worst_case_usd`` fits today's cap.
        ``need_price=False`` for callers that get the cost reported (Claude Code CLI)."""
        if need_price and self.price is None:
            raise SpendRefused("model.price_per_mtok not configured: cost would be unknown, paid call refused")
        if self.cap <= 0:
            raise SpendRefused("budget.max_daily_cost_usd is 0: paid calls are not allowed")
        spent = self.spent_today()
        if spent + worst_case_usd > self.cap:
            raise SpendRefused("daily cost cap: spent $%.4f, this call could cost up to $%.4f, cap $%.2f"
                               % (spent, worst_case_usd, self.cap), cap_reached=True)

    def record(self, usd):
        self.spent_today()
        self.db.execute("UPDATE budget_ledger SET cost_usd = COALESCE(cost_usd, 0) + ? WHERE day=?", (float(usd or 0), today()))
