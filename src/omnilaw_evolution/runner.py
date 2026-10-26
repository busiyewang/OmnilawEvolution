"""Experiment runs as a persisted state machine.

    QUEUED -> RESEARCHING -> PLANNED -> PATCHING -> EVALUATING
    EVALUATING -> REJECTED | READY;  READY -> APPLIED -> ROLLED_BACK
    any active state -> FAILED | CANCELLED | BUDGET_EXHAUSTED

Each stage writes a checkpoint, so ``resume`` continues where the run stopped
and keeps its cumulative budget. Candidate generation sees only the dev split;
the holdout split is scored once, for the dev-selected variant, by the
evaluator.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time

from . import evaluator, policy, sandbox, versions
from .budget import Budget, BudgetExhausted
from .db import loads, now
from .executors import make_executor
from .knowledge import mark_verification
from .planner import provenance_lines

ACTIVE = ("QUEUED", "RESEARCHING", "PLANNED", "PATCHING", "EVALUATING")
TERMINAL = ("REJECTED", "READY", "APPLIED", "ROLLED_BACK", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED")
RESUMABLE = ("FAILED", "CANCELLED", "BUDGET_EXHAUSTED")
TRANSITIONS = {
    "QUEUED": {"RESEARCHING"}, "RESEARCHING": {"PLANNED"}, "PLANNED": {"PATCHING"},
    "PATCHING": {"EVALUATING", "REJECTED"}, "EVALUATING": {"REJECTED", "READY"},
    "READY": {"APPLIED"}, "APPLIED": {"ROLLED_BACK"},
}
for _s in ACTIVE:
    TRANSITIONS.setdefault(_s, set()).update({"FAILED", "CANCELLED", "BUDGET_EXHAUSTED"})
for _s in RESUMABLE:
    TRANSITIONS[_s] = set(ACTIVE)


class Stopped(Exception):
    pass


class RunError(Exception):
    pass


def transition(db, run_id, new, **fields):
    run = db.get("runs", run_id)
    if new not in TRANSITIONS.get(run["state"], set()):
        raise RunError("illegal transition %s -> %s for run %d" % (run["state"], new, run_id))
    db.update("runs", run_id, state=new, updated_at=now(), **fields)
    db.event(run_id, "state", {"from": run["state"], "to": new})


@contextlib.contextmanager
def goal_lock(cfg, goal):
    path = os.path.join(cfg.subdir("locks"), "goal-%s.lock" % hashlib.sha256(goal.encode()).hexdigest()[:12])
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise RunError("another run for the same goal is in progress")
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def create_run(db, cfg, proposal_id, mode="shadow", executor=None, budget_overrides=None):
    proposal = db.get("proposals", proposal_id)
    if not proposal:
        raise RunError("no proposal %s" % proposal_id)
    _require_current_evaluator(cfg, proposal.get("evaluator_version"))
    limits = dict(cfg["budget"], **(budget_overrides or {}))
    return db.insert("runs", {
        "proposal_id": proposal_id, "state": "QUEUED", "mode": mode, "budget": limits,
        "executor": executor or cfg["executor"]["kind"], "model_config": cfg["model"],
        "created_at": now(), "updated_at": now()})


def _require_current_evaluator(cfg, version):
    if version != evaluator.evaluator_version(cfg["eval_dir"]):
        raise RunError("evaluation data or rules changed; create a new proposal and run to re-evaluate the baseline")


def request_stop(db, run_id):
    db.update("runs", run_id, stop_requested=1, updated_at=now())
    run = db.get("runs", run_id)
    db.event(run_id, "stop-requested", None)
    if run["state"] in ACTIVE and not _pid_alive(run["pid"]):
        transition(db, run_id, "CANCELLED", error="stopped while not executing")
    return db.get("runs", run_id)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class Runner:
    def __init__(self, db, cfg, run_id):
        self.db, self.cfg, self.run_id = db, cfg, run_id
        run = db.get("runs", run_id)
        self.proposal = db.get("proposals", run["proposal_id"])
        self.ck = loads(run["checkpoint"], {})
        self.budget = Budget(loads(run["budget"]), loads(run["usage"], {}), on_change=self._save_usage)
        self.exec = make_executor(cfg, run["executor"])
        self.dir = os.path.join(cfg["experiments_dir"], "run-%d" % run_id)
        os.makedirs(self.dir, exist_ok=True)

    # -- persistence helpers
    def _save_usage(self, usage):
        self.db.update("runs", self.run_id, usage=usage, updated_at=now())

    def _save_ck(self):
        self.db.update("runs", self.run_id, checkpoint=self.ck, updated_at=now())

    def stop_requested(self):
        return bool(self.db.one("SELECT stop_requested FROM runs WHERE id=?", (self.run_id,))["stop_requested"])

    def _guard(self):
        if self.stop_requested():
            raise Stopped()
        self.budget.tick()

    def state(self):
        return self.db.get("runs", self.run_id)["state"]

    # -- driver
    def execute(self):
        with goal_lock(self.cfg, self.proposal["goal"]):
            self.db.update("runs", self.run_id, pid=os.getpid())
            try:
                _require_current_evaluator(self.cfg, self.ck.get("planned", {}).get(
                    "evaluator_version", self.proposal.get("evaluator_version")))
                if "researched" not in self.ck:
                    self._enter("RESEARCHING")
                    self.research()
                if "planned" not in self.ck:
                    self._enter("PLANNED")
                    self.plan()
                if "patched_done" not in self.ck:
                    self._enter("PATCHING")
                    if not self.patch():
                        return self.state()
                self._enter("EVALUATING")
                self.evaluate()
            except Stopped:
                transition(self.db, self.run_id, "CANCELLED", error="stop requested")
            except BudgetExhausted as exc:
                transition(self.db, self.run_id, "BUDGET_EXHAUSTED", error=str(exc))
            except sandbox.SandboxStopped:
                transition(self.db, self.run_id, "CANCELLED", error="stop requested during sandboxed process")
            except Exception as exc:  # recorded, never swallowed silently
                if self.state() in ACTIVE:
                    transition(self.db, self.run_id, "FAILED", error="%s: %s" % (type(exc).__name__, exc))
                else:
                    raise
            finally:
                self.db.update("runs", self.run_id, pid=None, usage=self.budget.usage)
        return self.state()

    def _enter(self, state):
        if self.state() != state:
            transition(self.db, self.run_id, state)
        self._guard()

    # -- stages
    def research(self):
        """Confirm every cited knowledge row and its source exist; freeze provenance."""
        prov = provenance_lines(self.db, self.proposal)
        if not prov:
            raise RunError("proposal cites no knowledge")
        self.ck["researched"] = {"provenance": prov}
        self._save_ck()

    def plan(self):
        """Freeze baseline, data split, evaluator and criteria before any candidate exists."""
        active = versions.current_release(self.db)
        if not active:
            raise RunError("no active release; run `omnilaw-evolution init` first")
        self.ck["planned"] = {
            "baseline_sha": active["sha"], "baseline_path": active["path"],
            "split_version": evaluator.split_version(self.cfg["eval_dir"]),
            "evaluator_version": evaluator.evaluator_version(self.cfg["eval_dir"]),
            "criteria": loads(self.proposal["criteria"]),
            "variants": loads(self.proposal["variants"]),
        }
        self.db.update("runs", self.run_id, baseline_sha=active["sha"],
                       split_version=self.ck["planned"]["split_version"],
                       evaluator_version=self.ck["planned"]["evaluator_version"])
        self._save_ck()

    def patch(self):
        planned = self.ck["planned"]
        patched = self.ck.setdefault("patched", {})
        prov = self.ck["researched"]["provenance"]
        proposal = dict(self.proposal, methods=loads(self.proposal["methods"]))
        for idx, variant in enumerate(planned["variants"]):
            key = str(idx)
            if key in patched:
                continue
            self._guard()
            self.budget.charge("candidates")
            path = versions.create_candidate(self.cfg, self.run_id, idx, planned["baseline_sha"])
            info = self.exec.write_candidate(path, proposal, variant, self.run_id, idx, prov,
                                             budget=self.budget, stop_check=self.stop_requested)
            violations = policy.check_scope(path, planned["baseline_sha"], self.cfg["writable_paths"],
                                            self.cfg["protected_paths"])
            entry = {"variant": variant, "path": path, "executor": self.exec.name, "summary": info["summary"],
                     "violations": violations}
            if violations:
                entry["status"] = "rejected-scope"
                self.db.event(self.run_id, "scope-violation", violations)
            else:
                entry["sha"] = versions.commit_candidate(
                    path, self.cfg["writable_paths"],
                    "omnilaw run %d variant %d: %s %s" % (self.run_id, idx, proposal["methods"], variant))
                entry["status"] = "patched"
            patched[key] = entry
            self._save_ck()
        self.ck["patched_done"] = True
        self._save_ck()
        if not [e for e in patched.values() if e["status"] == "patched"]:
            reasons = ["all variants rejected by write-scope guard"] + sorted({
                "%s: %s" % (v["path"], v["reason"]) for e in patched.values() for v in e["violations"]})
            self._finish("REJECTED", reasons, None, None)
            return False
        return True

    def _process_timeout(self):
        # Per-run limit (may be overridden at create time), capped by remaining wall budget.
        remaining = self.budget.remaining_wall()
        limit = self.budget.limits["max_process_seconds"]
        return limit if remaining is None else max(0.5, min(limit, remaining))

    def _eval(self, label, code_root, split, sha):
        self._guard()
        _require_current_evaluator(self.cfg, self.ck["planned"]["evaluator_version"])
        scratch = os.path.join(self.dir, "eval", "%s-%s" % (label, split))
        try:
            res = evaluator.cached_run_on_split(self.db, self.cfg, code_root, split, scratch,
                                                stop_check=self.stop_requested, timeout=self._process_timeout())
        except sandbox.SandboxTimeout as exc:
            res = {"split": split, "ok": False, "metrics": None, "per_item": [], "isolation": "n/a",
                   "process_seconds": None, "regression": {"passed": False, "failures": ["timeout: %s" % exc]}}
        self.budget.add(sandbox_seconds=res.get("process_seconds") or 0)
        os.makedirs(scratch, exist_ok=True)  # a cache hit never created it
        per_item_path = os.path.join(scratch, "per_item.json")
        with open(per_item_path, "w", encoding="utf-8") as f:
            json.dump(res["per_item"], f, ensure_ascii=False, indent=1)
        self.db.insert("evaluations", {
            "run_id": self.run_id, "variant": label, "split": split, "code_sha": sha,
            "metrics": res["metrics"] or {"error": res["regression"]["failures"]},
            "per_item_path": per_item_path, "runtime_s": res.get("process_seconds"),
            "failures": res["regression"]["failures"], "log_path": os.path.join(scratch, "stderr.txt"),
            "isolation": res.get("isolation") + (" (cached)" if res.get("cached") else "")
            if res.get("isolation") else None, "created_at": now()})
        if res.get("cached"):
            self.budget.add(cache_hits=1)
        return res

    def evaluate(self):
        planned = self.ck["planned"]
        crit = planned["criteria"]
        patched = self.ck["patched"]
        dev = self.ck.setdefault("dev", {})
        if "baseline" not in dev:
            r = self._eval("baseline", planned["baseline_path"], "dev", planned["baseline_sha"])
            dev["baseline"] = {"metrics": r["metrics"], "regression": r["regression"]}
            self._save_ck()
        for key, entry in sorted(patched.items()):
            if entry["status"] != "patched" or key in dev:
                continue
            r = self._eval("v" + key, entry["path"], "dev", entry["sha"])
            dev[key] = {"metrics": r["metrics"], "regression": r["regression"],
                        "per_item_path": os.path.join(self.dir, "eval", "v%s-dev" % key, "per_item.json")}
            self._save_ck()
        # Select on dev: must pass contract and false-deletion limit; maximise primary metric.
        viable = [(k, v) for k, v in dev.items() if k != "baseline" and v["metrics"]
                  and v["metrics"]["false_deletion_rate"] <= crit["max_false_deletion_rate"]
                  and not v["metrics"]["critical_lost"]]
        if not viable:
            reasons = ["no variant passed dev screening"]
            for k, v in sorted(dev.items()):
                if k == "baseline":
                    continue
                m = v["metrics"]
                reasons.append("v%s %s: %s" % (k, patched[k]["variant"], (
                    "f1=%.3f false_deletion_rate=%.3f critical_lost=%s" % (
                        m["f1"], m["false_deletion_rate"], m["critical_lost"]) if m
                    else "; ".join(v["regression"]["failures"]))))
            return self._finish("REJECTED", reasons, None, None)
        # Deterministic tie-break: fewer false deletions, then the planner's variant order.
        # (Measured runtime is not used: it is timing noise at this scale.)
        best_key = max(viable, key=lambda kv: (kv[1]["metrics"][crit["primary"]],
                                               -kv[1]["metrics"]["false_deletion_rate"], -int(kv[0])))[0]
        best = patched[best_key]
        if "dev_failures_recorded" not in self.ck:
            # Only the selected variant's dev failures are evidence about the current approach.
            with open(dev[best_key]["per_item_path"], encoding="utf-8") as f:
                self._record_failures(json.load(f), "dev")
            self.ck["dev_failures_recorded"] = best_key
            self._save_ck()
        hold = self.ck.setdefault("holdout", {})
        if "baseline" not in hold:
            self._check_holdout_exposure()
            r = self._eval("baseline", planned["baseline_path"], "holdout", planned["baseline_sha"])
            hold["baseline"] = {"metrics": r["metrics"]}
            self._save_ck()
        if "candidate" not in hold:
            r = self._eval("v" + best_key, best["path"], "holdout", best["sha"])
            hold["candidate"] = {"key": best_key, "metrics": r["metrics"], "regression": r["regression"],
                                 "isolation": r["isolation"], "outcomes": _outcome_counts(r["per_item"])}
            self._save_ck()
        if "unit_tests" not in self.ck:
            self._guard()
            try:
                self.ck["unit_tests"] = evaluator.run_unit_tests(self.cfg, best["path"],
                                                                 os.path.join(self.dir, "eval", "unittest"),
                                                                 stop_check=self.stop_requested,
                                                                 timeout=self._process_timeout())
            except sandbox.SandboxTimeout as exc:
                self.ck["unit_tests"] = {"passed": False, "output_tail": "timeout: %s" % exc, "isolation": "n/a"}
            self._save_ck()
        cand = hold["candidate"]
        regression = {"passed": cand["regression"]["passed"] and self.ck["unit_tests"]["passed"],
                      "failures": cand["regression"]["failures"] +
                      ([] if self.ck["unit_tests"]["passed"] else ["unit tests failed"])}
        if cand["metrics"] is None:
            return self._finish("REJECTED", ["candidate failed on holdout"] + regression["failures"], best_key, best)
        ready, reasons = policy.decide(hold["baseline"]["metrics"], cand["metrics"], crit, regression)
        self._finish("READY" if ready else "REJECTED", reasons, best_key, best)

    def _check_holdout_exposure(self):
        limit = self.cfg["budget"].get("max_holdout_evals_per_day", 40)
        n = self.db.one("SELECT COUNT(*) AS n FROM evaluations WHERE split='holdout' "
                        "AND created_at >= ? AND COALESCE(isolation, '') NOT LIKE '%(cached)'",
                        (time.strftime("%Y-%m-%dT00:00:00"),))["n"]
        if n + 2 > limit:
            raise RunError("holdout exposure limit reached today (%d/%d); refresh the holdout split or wait"
                           % (n, limit))

    def _record_failures(self, per_item, split):
        # Only dev/batch failures are stored where planning can read them; holdout stays blind.
        for it in per_item:
            if it["outcome"] != "ok":
                _add_failure(self.db, {"run_id": self.run_id, "split": split, "item_id": it["id"],
                                       "kind": it["outcome"], "case_type": it["case"], "detail": it["reason"]})

    def _finish(self, state, reasons, best_key, best):
        patch_path = None
        if best:
            patch_path = os.path.join(self.dir, "candidate.patch")
            with open(patch_path, "w", encoding="utf-8") as f:
                f.write(versions.patch_text(self.cfg["project_root"], self.ck["planned"]["baseline_sha"],
                                            best["sha"]) + "\n")
        decision = {"state": state, "reasons": reasons, "selected_variant": best_key,
                    "variant": best["variant"] if best else None}
        transition(self.db, self.run_id, state, decision=decision, patch_path=patch_path,
                   candidate_sha=best["sha"] if best else None)
        # Only a real evaluation of a method is evidence about the knowledge behind it:
        # red-team runs, scope rejections and crashed/timed-out candidates are not.
        dev_measured = any(v.get("metrics") for k, v in self.ck.get("dev", {}).items() if k != "baseline")
        if not self.exec.name.startswith("redteam") and dev_measured:
            methods = loads(self.proposal["methods"])
            kids = [kid for kid in loads(self.proposal["knowledge_ids"], [])
                    if (self.db.get("knowledge", kid) or {}).get("method") in methods]
            mark_verification(self.db, kids, "local_repro_pass" if state == "READY" else "local_repro_fail",
                              "run-%d" % self.run_id)
        self.db.update("proposals", self.proposal["id"], status="tested")


def _add_failure(db, row):
    """One open failure per (split, item, kind); repeats are not new evidence."""
    if db.one("SELECT id FROM failures WHERE status='open' AND split=? AND item_id=? AND kind=?",
              (row["split"], row["item_id"], row["kind"])):
        return
    db.insert("failures", dict(row, created_at=now()))


def _outcome_counts(per_item):
    out = {}
    for it in per_item:
        key = "%s/%s" % (it["outcome"], it["case"])
        out[key] = out.get(key, 0) + 1
    return out


def resume(db, cfg, run_id, extend=None):
    run = db.get("runs", run_id)
    if run["state"] not in RESUMABLE:
        raise RunError("run %d is %s; only %s can be resumed" % (run_id, run["state"], RESUMABLE))
    if _pid_alive(run["pid"]):
        raise RunError("run %d is still executing in pid %s" % (run_id, run["pid"]))
    if extend:
        limits = loads(run["budget"])
        for k, v in extend.items():
            limits[k] = limits.get(k, 0) + v
        db.update("runs", run_id, budget=limits)
        db.event(run_id, "budget-extended", extend)
    ck = loads(run["checkpoint"], {})
    stage = ("QUEUED" if "researched" not in ck else "RESEARCHING" if "planned" not in ck
             else "PLANNED" if "patched_done" not in ck else "PATCHING")
    db.update("runs", run_id, stop_requested=0, error=None)
    transition(db, run_id, stage)
    db.event(run_id, "resumed", {"from_checkpoint": sorted(ck)})
    return Runner(db, cfg, run_id).execute()


def apply_run(db, cfg, run_id, policy_name="manual"):
    run = db.get("runs", run_id)
    if run["state"] != "READY":
        raise RunError("run %d is %s; only READY runs can be applied" % (run_id, run["state"]))
    _require_current_evaluator(cfg, run["evaluator_version"])
    rel = versions.apply_sha(db, cfg, run["candidate_sha"], run_id, policy_name)
    transition(db, run_id, "APPLIED")
    return rel


def rollback_release(db, cfg, release_id, to_release_id=None):
    rel = db.get("releases", release_id)
    checks = versions.rollback(db, cfg, release_id, to_release_id)
    if rel and rel["run_id"]:
        run = db.get("runs", rel["run_id"])
        if run["state"] == "APPLIED":
            transition(db, rel["run_id"], "ROLLED_BACK")
    return checks


def _score_release(db, cfg, release, split):
    scratch = os.path.join(cfg["experiments_dir"], "release-%d" % release["id"], split)
    res = evaluator.cached_run_on_split(db, cfg, release["path"], split, scratch)
    os.makedirs(scratch, exist_ok=True)
    per_item_path = os.path.join(scratch, "per_item.json")
    with open(per_item_path, "w", encoding="utf-8") as f:
        json.dump(res["per_item"], f, ensure_ascii=False, indent=1)
    db.insert("evaluations", {"run_id": None, "variant": "release-%d" % release["id"], "split": split,
                              "code_sha": release["sha"], "metrics": res["metrics"] or {},
                              "per_item_path": per_item_path, "runtime_s": res["process_seconds"],
                              "failures": res["regression"]["failures"],
                              "isolation": res["isolation"] + (" (cached)" if res.get("cached") else ""),
                              "created_at": now()})
    return res


def evaluate_release(db, cfg, split):
    """Score the active release on another batch (e.g. batch2), next to the
    release it replaced, and store its failures so later planning starts from
    them instead of from zero."""
    release = versions.current_release(db)
    res = _score_release(db, cfg, release, split)
    for it in res["per_item"]:
        if it["outcome"] != "ok":
            _add_failure(db, {"release_id": release["id"], "split": split, "item_id": it["id"],
                              "kind": it["outcome"], "case_type": it["case"], "detail": it["reason"]})
    prev = db.get("releases", release["previous_release_id"]) if release["previous_release_id"] else None
    res["previous"] = None
    if prev:
        versions.ensure_release_tree(cfg, prev["sha"])
        pres = _score_release(db, cfg, prev, split)
        res["previous"] = {"release_id": prev["id"], "metrics": pres["metrics"]}
        if (res["ok"] and pres["ok"] and release["run_id"]
                and res["metrics"]["f1"] > pres["metrics"]["f1"]
                and res["metrics"]["false_deletion_rate"] <= cfg["criteria"]["max_false_deletion_rate"]):
            run = db.get("runs", release["run_id"])
            prop = db.get("proposals", run["proposal_id"])
            mark_verification(db, loads(prop["knowledge_ids"], []), "effective_in_user_task",
                              "release-%d:%s" % (release["id"], split))
    res["release_id"] = release["id"]
    return res
