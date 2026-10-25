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
        "src/omnilaw_evolution/*.py", "src/omnilaw_evolution/collector/pipeline.py",
        "src/omnilaw_evolution/collector/healthcheck.py", "src/omnilaw_evolution/collector/__init__.py",
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
        "max_false_deletion_rate": 0.05,
        "max_ms_per_item": 50.0,
        "require_regression_pass": True,
    },
    "fetch": {"timeout": 20, "max_bytes": 2000000, "user_agent": "omnilaw-evolution/0.1 (research bot)"},
    "research": {"seed_urls": [], "arxiv_max_results": 3, "github_max_results": 3},
    "notify": {"kind": "mock"},             # chat adapters are a later phase
    "auto_apply": False,
    "background": False,
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
