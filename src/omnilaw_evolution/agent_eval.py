"""Agent-task evaluator for skill candidates (docs/SKILL_EVOLUTION.md §4.2-4.4).

A task is a directory ``<eval_dir>/agent_tasks/<split>/<id>/`` with ``task.json``
(instruction, category, related skills, limits), an initial ``workspace/`` and a
deterministic ``check.py`` (exit 0 = success; reads the workspace, never calls a
model). ``reference/`` holds files that solve the task; it is only used by
``check_suite`` to prove the checker can fail and pass; a ``solve.py`` script
(run on the workspace) can stand in for it. An optional ``setup.py`` prepares the
copied workspace before the agent starts, e.g. builds a Git history, so the suite
never stores nested repositories.

Each run copies the workspace and a skill-tree snapshot into a fresh scratch
directory and runs the pinned Hermes loop once (``hermes_worker.py --oneshot``)
under the OS sandbox: writes only inside the scratch directory, network only to
this process's model bridge, no reads of eval data, live agent state, the state
DB or credentials. Model calls go through the same budgeted bridge as chat.

Agent runs are not deterministic, so candidate and baseline skill trees are run
on the same tasks with repeats and compared by counts of successful runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time

from . import sandbox, skills
from .costs import SpendRefused
from .db import loads, now

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "hermes_worker.py")
SPLITS = ("dev", "holdout", "guard")

EVAL_IDENTITY = """你是 Omnilaw，运行在用户本机的个人智能体，核心执行循环来自 Hermes。默认用中文回答。
收到明确任务就持续执行：检查环境→必要的计划→调用工具→观察输出→修正错误→验证结果，完成后简要报告。
不要只提出计划或要求用户替你执行现有工具能做的步骤。当前目录就是任务的工作目录，只修改任务需要的文件。
相关技能可以用 skills 工具查看。失败如实说明，已执行和建议执行要区分。
"""


class EvalError(RuntimeError):
    pass


# ------------------------------------------------------------ task suite
def suite_dir(cfg):
    return os.path.join(cfg["eval_dir"], "agent_tasks")


def manifest(cfg):
    path = os.path.join(suite_dir(cfg), "MANIFEST.json")
    if not os.path.isfile(path):
        raise EvalError("no agent task suite at %s" % suite_dir(cfg))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dir_sha(path):
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            if name.endswith(".pyc") or name == ".DS_Store":
                continue
            p = os.path.join(root, name)
            h.update(os.path.relpath(p, path).encode() + b"\0")
            with open(p, "rb") as f:
                h.update(f.read())
            h.update(b"\0")
    return h.hexdigest()


def load_tasks(cfg, split, verify=True):
    if split not in SPLITS:
        raise EvalError("unknown agent task split: %s" % split)
    m = manifest(cfg)
    frozen = m.get("status") == "frozen"
    out = []
    for tid in m["splits"].get(split, []):
        d = os.path.join(suite_dir(cfg), split, tid)
        with open(os.path.join(d, "task.json"), encoding="utf-8") as f:
            task = json.load(f)
        sha = _dir_sha(d)
        if verify and frozen and m.get("sha256", {}).get("%s/%s" % (split, tid)) != sha:
            raise EvalError("agent task %s/%s changed after freezing; version the suite again" % (split, tid))
        out.append(dict(task, id=tid, split=split, dir=d, sha=sha,
                        skills=list(task.get("skills") or []),
                        max_turns=int(task.get("max_turns", 20)), max_seconds=int(task.get("max_seconds", 300))))
    return out


def evaluator_version(cfg):
    """Changes whenever the runner, worker, pinned Hermes, or any task changes."""
    h = hashlib.sha256()
    for p in (os.path.abspath(__file__), WORKER, os.path.join(skills.VENDOR, "hermes-manifest.json"),
              os.path.join(suite_dir(cfg), "MANIFEST.json")):
        with open(p, "rb") as f:
            h.update(f.read())
    for split in SPLITS:
        for t in load_tasks(cfg, split, verify=False):
            h.update(t["sha"].encode())
    return h.hexdigest()[:16]


def freeze(cfg, version):
    """Record each task's hash so later edits are detected. A human runs this
    after reviewing the drafts; candidates can never write ``eval/``."""
    path = os.path.join(suite_dir(cfg), "MANIFEST.json")
    m = manifest(cfg)
    m["sha256"] = {"%s/%s" % (s, t["id"]): t["sha"] for s in SPLITS for t in load_tasks(cfg, s, verify=False)}
    m["status"], m["version"], m["frozen_at"] = "frozen", version, now()
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)
    return m


def _task_script(cfg, task, name, workspace, scratch):
    """Run the task's own ``setup.py`` / ``solve.py`` on a workspace (no network)."""
    script = os.path.join(task["dir"], name)
    if not os.path.isfile(script):
        return
    res = sandbox.run([sys.executable, script, workspace], scratch=scratch, allow_write=[workspace],
                      timeout=120, cwd=workspace, mode=cfg["sandbox"]["mode"])
    if res["returncode"] != 0:
        raise EvalError("%s failed for %s/%s: %s" % (name, task["split"], task["id"], res["stderr"][-400:]))


def run_check(cfg, task, workspace, scratch):
    res = sandbox.run([sys.executable, os.path.join(task["dir"], "check.py"), workspace],
                      scratch=scratch, timeout=120, cwd=scratch, mode=cfg["sandbox"]["mode"])
    tail = (res["stdout"] + res["stderr"]).strip()[-600:]
    return res["returncode"] == 0, tail


def check_suite(cfg, splits=SPLITS):
    """No model involved: the initial workspace must fail its checker and the
    workspace with ``reference/`` overlaid must pass it."""
    base = os.path.join(cfg["experiments_dir"], "agent-suite-check")
    shutil.rmtree(base, ignore_errors=True)
    report = []
    for split in splits:
        for t in load_tasks(cfg, split, verify=False):
            row = {"task": "%s/%s" % (split, t["id"]), "category": t.get("category")}
            for label in ("initial", "reference"):
                run_dir = os.path.join(base, split, t["id"], label)
                ws = os.path.join(run_dir, "workspace")
                _copy_workspace(cfg, t, ws, os.path.join(run_dir, "setup"))
                if label == "reference":
                    ref = os.path.join(t["dir"], "reference")
                    if os.path.isfile(os.path.join(t["dir"], "solve.py")):
                        _task_script(cfg, t, "solve.py", ws, os.path.join(run_dir, "solve"))
                    elif os.path.isdir(ref):
                        shutil.copytree(ref, ws, dirs_exist_ok=True)
                    else:
                        row["reference"] = "missing"
                        continue
                ok, tail = run_check(cfg, t, ws, os.path.join(base, split, t["id"], label, "check"))
                row[label] = "pass" if ok else "fail"
                if (label == "initial") == ok:
                    row.setdefault("problems", []).append("%s %s: %s" % (label, row[label], tail[-200:]))
            report.append(row)
    return report


def _copy_workspace(cfg, task, dest, scratch):
    src = os.path.join(task["dir"], "workspace")
    if os.path.isdir(src):
        shutil.copytree(src, dest)
    else:
        os.makedirs(dest)
    _task_script(cfg, task, "setup.py", dest, scratch)


# ------------------------------------------------------------ one run
def _usage_names(home):
    try:
        with open(os.path.join(home, "skills", ".usage.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    records = data.get("skills", data) if isinstance(data, dict) else {}
    return sorted(name for name, rec in records.items() if isinstance(rec, dict)
                  and any(isinstance(rec.get(k), int) and rec[k] > 0 for k in ("use_count", "view_count")))


def spent_today(db):
    row = db.one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_evals WHERE created_at >= ?",
                 (time.strftime("%Y-%m-%dT00:00:00"),))
    return float(row["c"])


def check_budget(db, cfg):
    se = cfg["skill_evolution"]
    cap, per_run = float(se.get("max_daily_cost_usd") or 0), float(se.get("max_usd_per_run") or 0)
    if cap <= 0 or per_run <= 0:
        raise SpendRefused("skill_evolution.max_daily_cost_usd / max_usd_per_run is 0: evaluation runs are not allowed")
    spent = spent_today(db)
    if spent + per_run > cap:
        raise SpendRefused("skill evaluation cap: spent $%.4f today, a run may cost up to $%.2f, cap $%.2f"
                           % (spent, per_run, cap), cap_reached=True)


def run_task(db, cfg, task, tree_dir, tree_sha, *, arm, repeat, candidate_id=None, agent_cmd=None):
    """Run the agent once on ``task`` with the skills in ``tree_dir``; check the
    result; store and return the ``agent_evals`` row."""
    from .agent_bridge import AgentBridge
    from .hermes_runtime import hermes_settings, prepare_runtime

    check_budget(db, cfg)
    se = cfg["skill_evolution"]
    scratch = os.path.join(cfg["experiments_dir"], "agent-eval", "%s-%s-%s-%s-r%d-%d" % (
        task["split"], task["id"], arm, tree_sha[:8], repeat, int(time.time() * 1000)))
    ws, home = os.path.join(scratch, "workspace"), os.path.join(scratch, "home")
    _copy_workspace(cfg, task, ws, os.path.join(scratch, "setup"))
    shutil.copytree(tree_dir, os.path.join(home, "skills"))
    source = None if agent_cmd else str(prepare_runtime(cfg))
    bridge = AgentBridge(cfg, "eval", "eval", None, run_cap_usd=float(se["max_usd_per_run"]))
    detail, result, isolation, started = {}, {}, None, time.time()
    try:
        with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
            json.dump(hermes_settings(cfg, bridge.url, ws, task["max_turns"], True), f, indent=2)
        init = {"source": source, "shared": home, "session_id": "eval-%s-%d" % (task["id"], repeat),
                "model": cfg["model"]["model"], "base_url": bridge.url + "/v1", "token": bridge.token,
                "project_root": ws, "state_dir": home, "parent_pid": os.getpid(),
                "toolsets": list(se.get("toolsets") or []), "max_iterations": task["max_turns"],
                "max_seconds": task["max_seconds"], "max_tokens": int(cfg["model"].get("max_tokens", 4096)),
                "task": task["instruction"], "identity": EVAL_IDENTITY, "command_schema": None}
        init_path = os.path.join(scratch, "init.json")
        with open(init_path, "w", encoding="utf-8") as f:
            json.dump(init, f, ensure_ascii=False)
        cmd = list(agent_cmd or [sys.executable, WORKER, "--oneshot"]) + [init_path]
        env = {"HERMES_HOME": home, "HOME": home, "TERMINAL_CWD": ws, "TERMINAL_ENV": "local",
               "HERMES_INTERACTIVE": "0", "OPENAI_BASE_URL": bridge.url + "/v1", "OPENAI_API_KEY": bridge.token,
               "NO_PROXY": "localhost,127.0.0.1,::1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
               "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", os.defpath)}
        try:
            res = sandbox.run(cmd, scratch=os.path.join(scratch, "proc"), allow_write=[scratch],
                              deny_read=skills.protected_reads(cfg), network=int(bridge.url.rsplit(":", 1)[1]),
                              timeout=task["max_seconds"] + 60, cwd=ws, mode=cfg["sandbox"]["mode"], env_extra=env)
            isolation = res["isolation"]
            for line in res["stdout"].splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") in ("result", "failed"):
                    result = event
            if not result:
                detail["agent_error"] = "no result (exit %s): %s" % (res["returncode"], res["stderr"][-400:])
        except sandbox.SandboxTimeout as exc:
            detail["agent_error"] = "timeout: %s" % exc
    finally:
        bridge.close()
    if result.get("type") == "failed" or result.get("error"):
        detail["agent_error"] = str(result.get("message") or result.get("error"))[:400]
    detail["reply"] = str(result.get("reply") or "")[-400:]
    if bridge.refused:
        detail["refused"] = bridge.refused[-1]
    ok, tail = run_check(cfg, task, ws, os.path.join(scratch, "check"))
    detail["check"] = tail
    u = bridge.usage
    # A global budget refusal or a dead model transport says nothing about the
    # skills: the run is void and evaluation pauses.
    void = any("daily" in r or "今日" in r for r in bridge.refused) or (u["calls"] == 0 and bridge.errors)
    row = {"candidate_id": candidate_id, "arm": arm, "skills_tree": tree_sha, "split": task["split"],
           "task_id": task["id"], "task_sha": task["sha"], "evaluator_version": evaluator_version(cfg),
           "model": cfg["model"]["model"], "repeat": repeat, "success": None if void else int(ok),
           "calls": u["calls"], "seconds": round(time.time() - started, 2), "input_tokens": u["input_tokens"],
           "output_tokens": u["output_tokens"], "cost_usd": round(u["cost_usd"], 6),
           "skills_used": _usage_names(home), "detail": detail, "scratch": scratch,
           "isolation": isolation, "created_at": now()}
    row["id"] = db.insert("agent_evals", row)
    if void:
        why = (bridge.refused or bridge.errors)[-1]
        if bridge.refused:
            raise SpendRefused("model budget refused during an evaluation run: %s" % why, cap_reached=True)
        raise EvalError("model transport failed during an evaluation run: %s" % why)
    return row


# ------------------------------------------------------------ candidate evaluation
def relevant_tasks(cfg, cand, split):
    changed, targets = set(loads(cand["changed"], [])), set(loads(cand["targets"], []))
    return [t for t in load_tasks(cfg, split) if changed & set(t["skills"]) or t.get("category") in targets]


def _rows(db, cfg, tree_sha, task, version):
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(
        time.time() - 86400 * float(cfg["skill_evolution"].get("baseline_reuse_days", 7))))
    return db.all("SELECT * FROM agent_evals WHERE skills_tree=? AND task_sha=? AND evaluator_version=? "
                  "AND model=? AND success IS NOT NULL AND created_at >= ? ORDER BY id",
                  (tree_sha, task["sha"], version, cfg["model"]["model"], since))


def _ensure_runs(db, cfg, task, arms, repeats, candidate_id, agent_cmd, stop_check):
    """Top up each arm to ``repeats`` recorded runs; reuse recent identical runs."""
    version = evaluator_version(cfg)
    out = {}
    for arm, (tree_dir, tree_sha) in arms.items():
        rows = _rows(db, cfg, tree_sha, task, version)[:repeats]
        while len(rows) < repeats:
            if stop_check and stop_check():
                raise EvalError("stop requested")
            rows.append(run_task(db, cfg, task, tree_dir, tree_sha, arm=arm, repeat=len(rows),
                                 candidate_id=candidate_id, agent_cmd=agent_cmd))
        out[arm] = rows
    return out


def _tally(per_task, arm, tasks):
    rows = [r for t in tasks for r in per_task[t["id"]][arm]]
    succ = sum(r["success"] for r in rows)
    cost = sum(r["cost_usd"] or 0 for r in rows)
    return {"runs": len(rows), "success": succ, "cost_usd": round(cost, 4),
            "cost_per_success": round(cost / succ, 4) if succ else None}


def _holdout_exposure(db, cfg, cid):
    limit = int(cfg["skill_evolution"].get("max_holdout_candidates_per_day", 2))
    seen = {r["candidate_id"] for r in db.all(
        "SELECT DISTINCT candidate_id FROM agent_evals WHERE split IN ('holdout','guard') AND arm='candidate' "
        "AND created_at >= ?", (time.strftime("%Y-%m-%dT00:00:00"),))}
    if cid not in seen and len(seen) >= limit:
        raise EvalError("holdout exposure limit reached today (%d candidates)" % limit)


def decide_dev(dev):
    c, b = dev["candidate"]["success"], dev["baseline"]["success"]
    ok = c > 0 and c >= b
    return ok, ["dev: candidate %d/%d vs baseline %d/%d successful runs" % (
        c, dev["candidate"]["runs"], b, dev["baseline"]["runs"]) + ("" if ok else " (needs >= baseline and > 0)")]


def decide_holdout(se, rel, guard):
    reasons, ok = [], True
    gain = rel["candidate"]["success"] - rel["baseline"]["success"]
    need = int(se.get("min_holdout_gain", 2))
    reasons.append("holdout: candidate %d vs baseline %d successful runs of %d (gain %+d, need >= %d)" % (
        rel["candidate"]["success"], rel["baseline"]["success"], rel["candidate"]["runs"], gain, need))
    ok &= gain >= need
    if guard["candidate"]["runs"]:
        g_ok = guard["candidate"]["success"] >= guard["baseline"]["success"]
        reasons.append("guard: candidate %d vs baseline %d of %d%s" % (
            guard["candidate"]["success"], guard["baseline"]["success"], guard["candidate"]["runs"],
            "" if g_ok else " (regression)"))
        ok &= g_ok
    cb, cc = rel["baseline"]["cost_per_success"], rel["candidate"]["cost_per_success"]
    ratio = float(se.get("max_cost_ratio", 1.3))
    if cb and cc:
        c_ok = cc <= cb * ratio
        reasons.append("cost per success: $%.4f vs $%.4f (limit x%.2f)%s" % (cc, cb, ratio, "" if c_ok else " (too costly)"))
        ok &= c_ok
    return ok, reasons


def evaluate(db, cfg, cid, agent_cmd=None, stop_check=None):
    """dev screening, then (if passed) holdout + guard decision. Budget refusals
    pause the candidate (back to PREPARED); recorded runs are reused on retry."""
    cand = db.get("skill_candidates", cid)
    if cand["state"] not in ("PREPARED", "EVALUATING"):
        raise EvalError("candidate %d is %s; prepare it first" % (cid, cand["state"]))
    if cand["state"] == "PREPARED":
        skills.transition(db, cid, "EVALUATING", error=None)
    repo = skills.SkillRepo(cfg, cand["identity"])
    work = os.path.join(cfg["experiments_dir"], "skill-%d" % cid)
    arms = {}
    for arm, commit in (("baseline", cand["base_commit"]), ("candidate", cand["commit_sha"])):
        d = os.path.join(work, "eval-tree-" + arm)
        shutil.rmtree(d, ignore_errors=True)
        arms[arm] = (repo.export(commit, d), repo.tree(commit))
    se = cfg["skill_evolution"]
    try:
        dev_tasks = relevant_tasks(cfg, cand, "dev")
        if not dev_tasks:
            raise EvalError("no dev task covers skills %s or targets %s; set targets with `skills target`"
                            % (loads(cand["changed"]), loads(cand["targets"])))
        per = {t["id"]: _ensure_runs(db, cfg, t, arms, int(se["repeats"]["dev"]), cid, agent_cmd, stop_check)
               for t in dev_tasks}
        dev = {arm: _tally(per, arm, dev_tasks) for arm in arms}
        ok, reasons = decide_dev(dev)
        decision = {"dev": dev, "dev_tasks": [t["id"] for t in dev_tasks], "reasons": reasons}
        if not ok:
            skills.transition(db, cid, "REJECTED", decision=decision)
            return db.get("skill_candidates", cid)
        if manifest(cfg).get("status") != "frozen":
            raise EvalError("the agent task suite is a draft; freeze it after review before holdout evaluation")
        rel_tasks = relevant_tasks(cfg, cand, "holdout")
        if not rel_tasks:
            raise EvalError("no holdout task covers this candidate")
        _holdout_exposure(db, cfg, cid)
        guard_tasks = load_tasks(cfg, "guard")
        n = int(se["repeats"]["holdout"])
        per = {t["id"]: _ensure_runs(db, cfg, t, arms, n, cid, agent_cmd, stop_check) for t in rel_tasks}
        per.update({t["id"]: _ensure_runs(db, cfg, t, arms, n, cid, agent_cmd, stop_check) for t in guard_tasks})
        rel = {arm: _tally(per, arm, rel_tasks) for arm in arms}
        guard = {arm: _tally(per, arm, guard_tasks) for arm in arms}
        ok, more = decide_holdout(se, rel, guard)
        decision.update(holdout=rel, guard=guard, holdout_tasks=[t["id"] for t in rel_tasks],
                        reasons=reasons + more)
        skills.transition(db, cid, "READY" if ok else "REJECTED", decision=decision)
    except (SpendRefused, EvalError) as exc:
        skills.transition(db, cid, "PREPARED", error="paused: %s" % exc)
    return db.get("skill_candidates", cid)
