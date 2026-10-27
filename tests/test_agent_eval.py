"""Agent-task evaluator: suite checks, sandboxed runs, accounting, decisions.

A small stand-in agent replaces Hermes here (it reads the skill snapshot and
calls the model bridge like the worker does); the native test at the end runs
the pinned Hermes loop with scripted model replies (Python 3.14 + .[agent]).
No paid API is used.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from omnilaw_evolution import agent_eval, skills
from omnilaw_evolution.config import load_config
from omnilaw_evolution.db import DB

AGENT = sys.version_info >= (3, 14) and importlib.util.find_spec("yaml") is not None

# Stand-in agent: solves the task only if the snapshot has a skill whose body
# contains MAGIC; makes one bridge call; probes the sandbox boundaries.
FAKE_AGENT = textwrap.dedent('''
    import json, os, socket, sys, urllib.request
    init = json.load(open(sys.argv[1]))
    home = os.environ["HERMES_HOME"]
    magic = False
    for root, _, files in os.walk(os.path.join(home, "skills")):
        for name in files:
            if name == "SKILL.md" and "MAGIC" in open(os.path.join(root, name)).read():
                magic = True
    req = urllib.request.Request(init["base_url"] + "/chat/completions",
        data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Authorization": "Bearer " + init["token"], "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    status = opener.open(req, timeout=10).status
    probes = {}
    try:
        open(PROBE_SECRET).read(); probes["eval_read"] = "allowed"
    except OSError:
        probes["eval_read"] = "denied"
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close(); probes["internet"] = "allowed"
    except OSError:
        probes["internet"] = "denied"
    if magic:
        with open(os.path.join(init["project_root"], "answer.txt"), "w") as f:
            f.write("42\\n")
    print(json.dumps({"type": "result", "reply": json.dumps(dict(probes, bridge=status, magic=magic))}))
''')

CHECK = textwrap.dedent('''
    import os, sys
    p = os.path.join(sys.argv[1], "answer.txt")
    ok = os.path.exists(p) and open(p).read().strip() == "42"
    print("ok" if ok else "answer.txt missing or wrong")
    sys.exit(0 if ok else 1)
''')

GUARD_CHECK = "import sys\nprint('always passes')\nsys.exit(0)\n"


def make_task(root, split, tid, category, skills_=(), check=CHECK, reference=True):
    d = os.path.join(root, split, tid)
    os.makedirs(os.path.join(d, "workspace"))
    with open(os.path.join(d, "workspace", "README.md"), "w") as f:
        f.write("Write 42 to answer.txt\n")
    with open(os.path.join(d, "task.json"), "w") as f:
        json.dump({"title": tid, "category": category, "skills": list(skills_),
                   "instruction": "Write the answer to answer.txt", "max_turns": 5, "max_seconds": 60}, f)
    with open(os.path.join(d, "check.py"), "w") as f:
        f.write(check)
    if reference:
        os.makedirs(os.path.join(d, "reference"))
        with open(os.path.join(d, "reference", "answer.txt"), "w") as f:
            f.write("42\n")


def fake_complete(llm, messages, **options):
    llm.last_usage = {"input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.002}
    message = {"role": "assistant", "content": "ok"}
    return {"id": "t", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


class EvalTestBase(unittest.TestCase):
    se = {"enabled": True, "max_daily_cost_usd": 1, "max_usd_per_run": 0.1}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="omnilaw-agent-eval-test-"))
        self.eval_dir = os.path.join(self.tmp, "eval")
        suite = os.path.join(self.eval_dir, "agent_tasks")
        make_task(suite, "dev", "d1", "data", ["answer-skill"])
        make_task(suite, "holdout", "h1", "data", ["answer-skill"])
        make_task(suite, "guard", "g1", "coding", check=GUARD_CHECK, reference=False)
        with open(os.path.join(suite, "MANIFEST.json"), "w") as f:
            json.dump({"version": "test.draft", "status": "draft",
                       "splits": {"dev": ["d1"], "holdout": ["h1"], "guard": ["g1"]}}, f)
        self.cfg = load_config(overrides={
            "project_root": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
            "experiments_dir": os.path.join(self.tmp, "exp"), "eval_dir": self.eval_dir,
            "model": {"provider": "deepseek", "model": "deepseek-flash", "price_per_mtok": {"input": 1, "output": 1}},
            "budget": {"max_daily_cost_usd": 5}, "skill_evolution": dict(self.se)})
        self.db = DB(self.cfg.db_path)
        self.repo = skills.ensure_initialized(self.db, self.cfg)
        self.agent = os.path.join(self.tmp, "fake_agent.py")
        with open(self.agent, "w") as f:  # the sandbox scrubs the environment, so the probe path is baked in
            f.write("PROBE_SECRET = %r\n" % os.path.join(suite, "dev", "d1", "check.py") + FAKE_AGENT)
        patcher = mock.patch("omnilaw_evolution.llm.DeepSeekLLM.complete_raw", fake_complete)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def candidate(self, body="MAGIC: write 42 to answer.txt"):
        def applier(cfg, payload, tree, scratch):
            d = os.path.join(tree, "custom", payload["name"])
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write("---\nname: %s\ndescription: d\n---\n%s\n" % (payload["name"], payload["content"]))
            return {"success": True}
        cid, _ = skills.add_candidate(self.db, self.repo.ident, "user",
                                      {"action": "create", "name": "answer-skill", "content": body}, "test")
        return skills.prepare(self.db, self.cfg, cid, applier=applier)["id"]

    def evaluate(self, cid):
        return agent_eval.evaluate(self.db, self.cfg, cid, agent_cmd=[sys.executable, self.agent])


class SuiteTests(EvalTestBase):
    def test_checkers_fail_initial_and_pass_reference(self):
        report = {r["task"]: r for r in agent_eval.check_suite(self.cfg, ("dev", "holdout"))}
        self.assertEqual(report["dev/d1"]["initial"], "fail")
        self.assertEqual(report["dev/d1"]["reference"], "pass")
        self.assertNotIn("problems", report["holdout/h1"])

    def test_freeze_detects_later_edits(self):
        agent_eval.freeze(self.cfg, "test.1")
        self.assertEqual(len(agent_eval.load_tasks(self.cfg, "dev")), 1)
        with open(os.path.join(self.eval_dir, "agent_tasks", "dev", "d1", "task.json"), "a") as f:
            f.write("\n")
        with self.assertRaises(agent_eval.EvalError):
            agent_eval.load_tasks(self.cfg, "dev")


class DraftSuiteTests(unittest.TestCase):
    def test_every_draft_checker_fails_initially_and_passes_on_reference(self):
        import runpy
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="omnilaw-suite-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts",
                              "build_agent_tasks.py")
        counts = runpy.run_path(script)["build"](os.path.join(tmp, "eval"))
        self.assertEqual(counts, {"dev": 16, "holdout": 16, "guard": 6})
        cfg = load_config(overrides={"project_root": tmp, "state_dir": os.path.join(tmp, "state"),
                                     "experiments_dir": os.path.join(tmp, "exp"), "eval_dir": os.path.join(tmp, "eval")})
        report = agent_eval.check_suite(cfg)
        self.assertEqual([r for r in report if r.get("problems") or r.get("reference") == "missing"], [])
        for split in ("dev", "holdout"):
            cats = [t["category"] for t in agent_eval.load_tasks(cfg, split)]
            self.assertEqual({c: cats.count(c) for c in set(cats)},
                             {"debugging": 4, "coding": 4, "data": 4, "ops": 4})


class RunTests(EvalTestBase):
    def test_run_is_sandboxed_accounted_and_checked(self):
        task = agent_eval.load_tasks(self.cfg, "dev")[0]
        tree = self.repo.export(self.repo.head(), os.path.join(self.tmp, "tree"))
        row = agent_eval.run_task(self.db, self.cfg, task, tree, self.repo.tree("HEAD"), arm="baseline", repeat=0,
                                  agent_cmd=[sys.executable, self.agent])
        probes = json.loads(row["detail"]["reply"])
        self.assertEqual(probes["bridge"], 200)
        if row["isolation"] == "sandbox-exec":
            self.assertEqual(probes["eval_read"], "denied")
            self.assertEqual(probes["internet"], "denied")
        self.assertEqual(row["success"], 0)  # no MAGIC skill in the seed tree
        self.assertEqual((row["calls"], row["input_tokens"]), (1, 1000))
        self.assertAlmostEqual(row["cost_usd"], 0.002)
        self.assertAlmostEqual(agent_eval.spent_today(self.db), 0.002)

    def test_zero_eval_cap_refuses_before_running(self):
        self.cfg["skill_evolution"]["max_daily_cost_usd"] = 0
        cid = self.candidate()
        cand = self.evaluate(cid)
        self.assertEqual(cand["state"], "PREPARED")
        self.assertIn("not allowed", cand["error"])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM agent_evals")["n"], 0)


class DecisionTests(EvalTestBase):
    def test_dev_pass_waits_for_frozen_suite_then_holdout_decides(self):
        cid = self.candidate()
        cand = self.evaluate(cid)
        self.assertEqual(cand["state"], "PREPARED")
        self.assertIn("draft", cand["error"])
        dev_runs = self.db.one("SELECT COUNT(*) AS n FROM agent_evals WHERE split='dev'")["n"]
        self.assertEqual(dev_runs, 4)  # 2 arms x 2 repeats
        agent_eval.freeze(self.cfg, "test.1")
        cand = self.evaluate(cid)
        self.assertEqual(cand["state"], "READY", cand["decision"])
        decision = json.loads(cand["decision"])
        self.assertEqual(decision["dev"]["candidate"]["success"], 2)
        self.assertEqual(decision["holdout"]["candidate"]["success"], 3)
        self.assertEqual(decision["holdout"]["baseline"]["success"], 0)
        self.assertEqual(decision["guard"]["candidate"]["success"], 3)
        # Freezing changed the evaluator version, so dev ran again; holdout + guard ran once.
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM agent_evals")["n"], 4 + 4 + 6 + 6)

    def test_baseline_runs_are_reused_across_candidates(self):
        agent_eval.freeze(self.cfg, "test.1")
        first = self.candidate()
        self.evaluate(first)
        n = self.db.one("SELECT COUNT(*) AS n FROM agent_evals WHERE arm='baseline'")["n"]
        second = self.candidate(body="no hint here")
        self.assertEqual(self.evaluate(second)["state"], "REJECTED")
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM agent_evals WHERE arm='baseline'")["n"], n)

    def test_candidate_without_covering_task_pauses(self):
        cid, _ = skills.add_candidate(self.db, self.repo.ident, "user",
                                      {"action": "create", "name": "unrelated", "content": "x"}, "t")
        cid = skills.prepare(self.db, self.cfg, cid, applier=lambda c, p, tree, s: (
            os.makedirs(os.path.join(tree, "x", "unrelated")),
            open(os.path.join(tree, "x", "unrelated", "SKILL.md"), "w").close(), {"success": True})[-1])["id"]
        cand = self.evaluate(cid)
        self.assertEqual(cand["state"], "PREPARED")
        self.assertIn("targets", cand["error"])
        skills.set_targets(self.db, cid, ["data"])
        self.assertEqual([t["id"] for t in agent_eval.relevant_tasks(
            self.cfg, self.db.get("skill_candidates", cid), "dev")], ["d1"])

    def test_holdout_rules(self):
        se = {"min_holdout_gain": 2, "max_cost_ratio": 1.3}
        t = lambda s, c, n=6: {"runs": n, "success": s, "cost_usd": c, "cost_per_success": round(c / s, 4) if s else None}
        ok, _ = agent_eval.decide_holdout(se, {"candidate": t(5, 0.5), "baseline": t(3, 0.3)},
                                          {"candidate": t(6, 0.6), "baseline": t(6, 0.6)})
        self.assertTrue(ok)
        ok, reasons = agent_eval.decide_holdout(se, {"candidate": t(5, 0.5), "baseline": t(3, 0.3)},
                                                {"candidate": t(5, 0.6), "baseline": t(6, 0.6)})
        self.assertFalse(ok)
        self.assertIn("regression", " ".join(reasons))
        ok, reasons = agent_eval.decide_holdout(se, {"candidate": t(5, 2.0), "baseline": t(3, 0.3)},
                                                {"candidate": t(0, 0, 0), "baseline": t(0, 0, 0)})
        self.assertFalse(ok)
        self.assertIn("too costly", " ".join(reasons))
        self.assertFalse(agent_eval.decide_dev({"candidate": t(0, 0), "baseline": t(0, 0)})[0])


@unittest.skipUnless(AGENT, "install .[agent] with Python 3.14 for the native Hermes loop")
class NativeHermesRunTests(EvalTestBase):
    def test_pinned_hermes_solves_a_task_in_the_sandbox(self):
        task = agent_eval.load_tasks(self.cfg, "dev")[0]
        tree = self.repo.export(self.repo.head(), os.path.join(self.tmp, "tree"))
        seen = []

        def complete(llm, messages, **options):
            llm.last_usage = {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0001}
            if options.get("tools"):
                seen.append([t["function"]["name"] for t in options["tools"]])
            if options.get("tools") and len(seen) == 1:
                message = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                           "function": {"name": "write_file", "arguments": json.dumps(
                               {"path": "answer.txt", "content": "42\n"})}}]}
            else:
                message = {"role": "assistant", "content": "已写入 answer.txt。"}
            return {"id": "t", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with mock.patch("omnilaw_evolution.llm.DeepSeekLLM.complete_raw", complete):
            row = agent_eval.run_task(self.db, self.cfg, task, tree, self.repo.tree("HEAD"),
                                      arm="baseline", repeat=0)
        self.assertEqual(row["success"], 1, row["detail"])
        self.assertIn("write_file", seen[0])
        self.assertNotIn("project_command", seen[0])
        self.assertGreaterEqual(row["calls"], 2)


if __name__ == "__main__":
    unittest.main()
