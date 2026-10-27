"""Configuration. Paths come from the config file (or defaults relative to it);
nothing is hard-coded to a user name or OS directory. Credentials are only
referenced by environment-variable name."""
from __future__ import annotations

import copy
import json
import os

DEFAULTS = {
    "project_root": ".",
    "state_dir": ".omnilaw/state",
    "experiments_dir": ".omnilaw/experiments",
    "eval_dir": None,                       # default: <project_root>/eval
    "target_module": "src/omnilaw_evolution/collector/dedup.py",
    # Paths a candidate may change. Everything else is rejected by the policy guard.
    "writable_paths": ["src/omnilaw_evolution/collector/dedup.py"],
    # Never writable, even if a writable glob would match.
    "protected_paths": [
        "eval/**", "tests/**", "omnilaw.json", "omnilaw.example.json", "pyproject.toml",
        "src/omnilaw_evolution/*.py", "src/omnilaw_evolution/adapters/**",
        "src/omnilaw_evolution/collector/pipeline.py",
        "src/omnilaw_evolution/collector/healthcheck.py", "src/omnilaw_evolution/collector/__init__.py",
        "src/omnilaw_evolution/collector/semantic.py",
    ],
    "model": {"provider": "mock", "model": "claude-opus-5-5", "api_key_env": "ANTHROPIC_API_KEY",
              "effort": "medium", "price_per_mtok": None},
    "executor": {"kind": "template", "claude_cli": "claude"},
    "sandbox": {"mode": "auto"},            # auto | sandbox-exec | bwrap | none
    "budget": {
        "max_model_calls": 20,
        "max_candidates": 4,
        "max_wall_seconds": 900,
        "max_download_bytes": 5000000,
        "max_process_seconds": 60,
        "max_daily_cost_usd": 0,            # 0 = no paid calls allowed
        "max_holdout_evals_per_day": 40,
    },
    "criteria": {
        "primary": "f1",
        "min_delta": 0.05,
        "min_relative_error_reduction": 0.5,
        "max_false_deletion_rate": 0.05,
        "max_ms_per_item": 50.0,
        "require_regression_pass": True,
    },
    "fetch": {"timeout": 20, "max_bytes": 2000000, "user_agent": "omnilaw-evolution/0.1 (research bot)"},
    "research": {
        "seed_urls": [], "arxiv_max_results": 3, "github_max_results": 3,
        "min_relevance": 0.5, "max_ingest_per_task": 3, "cooldown_hours": 72,
        "positive_terms": ["duplicate", "dedup", "near-duplicate", "similarity", "paraphrase", "去重"],
        "negative_terms": ["image", "video", "speech", "audio", "sign", "phishing"],
        "snapshot_manifest": None,          # replay a recorded discovery feed instead of searching live
    },
    # Research topic for each failure class that has no covering knowledge yet.
    "failure_topics": {"cross-language": "language-agnostic sentence embedding",
                       "paraphrase": "paraphrase identification"},
    "queue": {
        "goal": "减少技术日报中的重复材料，同时保留带有新信息的条目",
        "auto_shadow_runs": True,           # shadow runs only produce candidates + reports
        "poll_seconds": 60,
    },
    # Cross-task limits per calendar day, enforced by the worker.
    "daily_budget": {"max_tasks": 50, "max_model_calls": 100, "max_download_bytes": 50000000,
                     "max_candidates": 20},
    "review_after_days": 30,
    # Periodic records of research and code changes (journal task / `journal` command).
    # dir: absolute, or relative to project_root; default <state_dir>/journal.
    "journal": {"dir": None, "max_patch_lines": 120},
    "chat": {
        "engine": "hermes",                # local DeepSeek CLI: Hermes; basic keeps project-only tools
        "max_iterations": 30,
        "max_seconds": 900,
        "history_turns": 20,
        "max_context_chars": 60000,
        "max_tool_rounds": 3,
        "allowed_users": [],                # chat user ids allowed to use read-only commands
        "admins": [],                       # ids allowed to stop/resume/apply/rollback
        "confirm_ttl_seconds": 600,
        "feishu": {"verification_token_env": "FEISHU_VERIFICATION_TOKEN",
                   "app_id_env": "FEISHU_APP_ID", "app_secret_env": "FEISHU_APP_SECRET"},
    },
    "notify": {"kind": "mock",              # mock (outbox only) | stdout | feishu_webhook
               "webhook_url_env": "FEISHU_WEBHOOK_URL", "webhook_secret_env": "FEISHU_WEBHOOK_SECRET"},
    # Optional sentence-embedding runtime (collector/semantic.py). A human turns it on
    # after approving the dependency; the pinned model is fetched with `models fetch`.
    "semantic": {"enabled": False},
    # Hermes skills as a second evolution target (docs/SKILL_EVOLUTION.md). When enabled,
    # Hermes stages skill writes instead of saving them; they become candidates that
    # need an agent-task evaluation and a human confirmation. Eval spend has its own
    # daily cap (0 = no paid evaluation runs) inside budget.max_daily_cost_usd.
    "skill_evolution": {
        "enabled": False,
        "max_daily_cost_usd": 0,
        "max_usd_per_run": 0.5,
        "baseline_reuse_days": 7,
        "max_holdout_candidates_per_day": 2,
        "repeats": {"dev": 2, "holdout": 3},
        "min_holdout_gain": 2,              # extra successful runs over the baseline
        "max_cost_ratio": 1.3,              # cost per success, candidate vs baseline
        "toolsets": ["terminal", "file", "todo", "skills", "code_execution"],
    },
    "auto_apply": False,
    "background": False,                    # the daemon refuses to start unless this is true
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    def __init__(self, data, path=None):
        super().__init__(data)
        self.path = path
        base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
        for key in ("project_root", "state_dir", "experiments_dir", "eval_dir"):
            if self.get(key):
                p = os.path.expanduser(self[key])
                self[key] = os.path.realpath(p if os.path.isabs(p) else os.path.join(base, p))
        snap = self.get("research", {}).get("snapshot_manifest")
        if snap and not os.path.isabs(snap):
            self["research"]["snapshot_manifest"] = os.path.realpath(os.path.join(base, snap))
        if not self.get("eval_dir"):
            self["eval_dir"] = os.path.join(self["project_root"], "eval")

    @property
    def db_path(self):
        return os.path.join(self["state_dir"], "omnilaw.sqlite3")

    def subdir(self, *names):
        p = os.path.join(self["state_dir"], *names)
        os.makedirs(p, exist_ok=True)
        return p


def load_config(path=None, overrides=None):
    path = path or os.environ.get("OMNILAW_CONFIG") or "omnilaw.json"
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        path = None
    return Config(_merge(_merge(DEFAULTS, data), overrides), path)


def write_config(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
