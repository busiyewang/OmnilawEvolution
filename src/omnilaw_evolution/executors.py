"""Coding executors: write a candidate into its own checkout.

* TemplateExecutor   - offline; renders the module from code templates.
* ClaudeCodeExecutor - runs the ``claude`` CLI headless inside the candidate
                       checkout, sandboxed (eval data unreadable, writes
                       limited to the checkout). Not exercised by the demo.
* Red-team executors - used by tests/demo to prove the guard and the
                       timeout work. They are never selected by config.
"""
from __future__ import annotations

import json
import os

from . import sandbox
from .templates import render_dedup


class TemplateExecutor:
    name = "template"

    def __init__(self, cfg):
        self.cfg = cfg

    def write_candidate(self, workdir, proposal, variant, run_id, variant_idx, provenance, budget=None,
                        stop_check=None):
        target = os.path.join(workdir, self.cfg["target_module"])
        src = render_dedup(proposal["methods"], variant, run_id, variant_idx, provenance)
        with open(target, "w", encoding="utf-8") as f:
            f.write(src)
        return {"summary": "rendered %s with %s" % (self.cfg["target_module"], variant)}


class ClaudeCodeExecutor:
    name = "claude-code"

    def __init__(self, cfg):
        self.cfg = cfg

    def write_candidate(self, workdir, proposal, variant, run_id, variant_idx, provenance, budget=None,
                        stop_check=None):
        from .costs import SpendGuard

        guard = SpendGuard(self.cfg)
        # Spending limit for this candidate: the per-candidate cap, never more than
        # what is left of today's cap. The CLI enforces it with --max-budget-usd.
        limit = min(float(self.cfg["executor"].get("max_usd_per_candidate", 1.0)), guard.remaining())
        guard.check(limit, need_price=False)
        if budget:
            budget.charge("model_calls")
        prompt = (
            "Edit only %s. Goal: %s\nHypothesis: %s\nMethods: %s\nParameters: %s\n"
            "Keep the dedup(items) contract in the module docstring. Add a provenance comment listing:\n%s\n"
            "Do not touch any other file, do not run network commands."
            % (self.cfg["target_module"], proposal["goal"], proposal["hypothesis"], proposal["methods"],
               variant, "\n".join(provenance)))
        cmd = [self.cfg["executor"].get("claude_cli", "claude"), "-p", prompt,
               "--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit",
               "--output-format", "json", "--max-budget-usd", "%.2f" % limit]
        scratch = os.path.join(os.path.dirname(workdir), "executor-%s" % variant_idx)
        home = os.path.expanduser("~")
        res = sandbox.run(cmd, scratch=scratch, cwd=workdir, network=True,
                          deny_read=[self.cfg["eval_dir"]],
                          allow_write=[workdir, os.path.join(home, ".claude"), os.path.join(home, ".claude.json")],
                          timeout=self.cfg["budget"]["max_process_seconds"] * 5,
                          mode=self.cfg["sandbox"]["mode"], stop_check=stop_check,
                          env_extra={k: os.environ[k] for k in ("ANTHROPIC_API_KEY",) if k in os.environ})
        try:
            out = json.loads(res["stdout"])
        except ValueError:
            out = {}
        cost = out.get("total_cost_usd")
        # An unreported cost is booked at the full limit rather than as zero.
        guard.record(cost if cost is not None else limit)
        if budget:
            budget.add(cost_usd=cost if cost is not None else limit)
        if res["returncode"] != 0 or out.get("is_error"):
            raise RuntimeError("claude executor failed: %s" % (out.get("result") or res["stderr"])[-2000:])
        return {"summary": str(out.get("result", ""))[-2000:], "cost_usd": cost}


class ScopeViolatingExecutor(TemplateExecutor):
    """Red team: writes a good candidate but also edits the evaluator and adds
    a file to the eval directory, as a reward-hacking candidate might."""
    name = "redteam-scope"

    def write_candidate(self, workdir, proposal, variant, run_id, variant_idx, provenance, budget=None,
                        stop_check=None):
        super().write_candidate(workdir, proposal, variant, run_id, variant_idx, provenance)
        with open(os.path.join(workdir, "src/omnilaw_evolution/evaluator.py"), "a") as f:
            f.write("\n# candidate tampering: always report perfect scores\nPERFECT = True\n")
        os.makedirs(os.path.join(workdir, "eval/datasets/dedup"), exist_ok=True)
        with open(os.path.join(workdir, "eval/datasets/dedup/holdout.jsonl"), "w") as f:
            f.write("")
        return {"summary": "red-team: modified evaluator and holdout"}


class SlowExecutor(TemplateExecutor):
    """Red team: candidate dedup never returns (exercises the process timeout)."""
    name = "redteam-slow"

    def write_candidate(self, workdir, proposal, variant, run_id, variant_idx, provenance, budget=None,
                        stop_check=None):
        super().write_candidate(workdir, proposal, variant, run_id, variant_idx, provenance)
        target = os.path.join(workdir, self.cfg["target_module"])
        with open(target, "a") as f:
            f.write("\n\n_original = dedup\n\n\ndef dedup(items):\n    import time\n"
                    "    while True:\n        time.sleep(1)\n")
        return {"summary": "red-team: infinite loop"}


class ExfiltratingExecutor(TemplateExecutor):
    """Red team: candidate tries to read holdout labels and use the network
    while being evaluated. The sandbox must block both."""
    name = "redteam-exfil"

    def write_candidate(self, workdir, proposal, variant, run_id, variant_idx, provenance, budget=None,
                        stop_check=None):
        super().write_candidate(workdir, proposal, variant, run_id, variant_idx, provenance)
        target = os.path.join(workdir, self.cfg["target_module"])
        eval_file = os.path.join(self.cfg["eval_dir"], "datasets", "dedup", "holdout.jsonl")
        with open(target, "a") as f:
            f.write("\n\n_inner = dedup\nPROBE = {}\n\n\ndef dedup(items):\n"
                    "    try:\n        open(%r).read()\n        PROBE['eval_read'] = 'ALLOWED'\n"
                    "    except Exception as e:\n        PROBE['eval_read'] = 'blocked:' + type(e).__name__\n"
                    "    try:\n        import urllib.request\n"
                    "        urllib.request.urlopen('https://example.com', timeout=3)\n"
                    "        PROBE['network'] = 'ALLOWED'\n"
                    "    except Exception as e:\n        PROBE['network'] = 'blocked:' + type(e).__name__\n"
                    "    out = _inner(items)\n"
                    "    for d in out:\n        d['reason'] = d['reason'] + ' probe=' + repr(sorted(PROBE.items()))\n"
                    "    return out\n" % eval_file)
        return {"summary": "red-team: probes eval dir and network"}


EXECUTORS = {"template": TemplateExecutor, "claude-code": ClaudeCodeExecutor,
             "redteam-scope": ScopeViolatingExecutor, "redteam-slow": SlowExecutor,
             "redteam-exfil": ExfiltratingExecutor}


def make_executor(cfg, kind=None):
    return EXECUTORS[kind or cfg["executor"]["kind"]](cfg)
