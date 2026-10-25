"""Periodic records: what the agent learned and how its code changed.

Each journal covers the window since the previous journal of the same period
(daily / weekly) and is written as Markdown under ``journal.dir``:

* ``daily/<date>.md`` or ``weekly/<year>-W<week>.md`` — new sources, new
  knowledge (claim, steps, conditions, limitations, verbatim quote, location),
  verification changes, source changes; finished runs with decisions and the
  candidate patch; releases applied / rolled back with their code diff; open
  problems; usage and cost.
* ``KNOWLEDGE_BASE.md`` — every knowledge record so far, grouped by method.
* ``CHANGELOG.md`` — every release of the evolved module with its diff stat.

Everything is read from the state DB and Git; nothing is summarised by a
model, so the records cannot invent results. Local paths are replaced by
``<repo>`` / ``~`` so the files can be shared.
"""
from __future__ import annotations

import datetime as dt
import os

from . import versions
from .db import loads, now

FMT = "%Y-%m-%dT%H:%M:%S%z"
PERIODS = {"daily": dt.timedelta(days=1), "weekly": dt.timedelta(days=7)}
VERIFICATION_ZH = {"source_claim": "仅来源声称", "local_repro_fail": "本地复现失败",
                   "local_repro_pass": "本地复现通过", "effective_in_user_task": "真实批次有效"}
TERMINAL = ("READY", "REJECTED", "APPLIED", "ROLLED_BACK", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED")


def journal_dir(cfg):
    d = (cfg.get("journal") or {}).get("dir")
    if d:
        d = os.path.expanduser(d)
        d = d if os.path.isabs(d) else os.path.join(cfg["project_root"], d)
    else:
        d = os.path.join(cfg["state_dir"], "journal")
    os.makedirs(d, exist_ok=True)
    return d


def _stamp(t):
    return t.astimezone().strftime(FMT)


def window(db, period, until=None):
    """(since, until) as local datetimes: from the end of the previous journal
    of this period, or one period back for the first one. The window is
    half-open, so consecutive journals neither overlap nor leave gaps."""
    until = until or dt.datetime.now().astimezone()
    last = db.one("SELECT until FROM journals WHERE period=? ORDER BY id DESC LIMIT 1", (period,))
    since = dt.datetime.strptime(last["until"], FMT) if last else until - PERIODS[period]
    return since, until


def _in(col):
    return "%s >= ? AND %s < ?" % (col, col)


def collect(db, cfg, since, until):
    """Everything that happened in [since, until), straight from the DB."""
    a, b = _stamp(since), _stamp(until)
    sources = db.all("SELECT * FROM sources WHERE %s ORDER BY id" % _in("fetched_at"), (a, b))
    knowledge = db.all("SELECT k.*, s.url, s.title AS source_title, s.revision, s.read_status, s.kind AS source_kind "
                       "FROM knowledge k JOIN sources s ON s.id=k.source_id WHERE %s ORDER BY k.id"
                       % _in("k.created_at"), (a, b))
    reverified = db.all("SELECT k.*, s.url FROM knowledge k JOIN sources s ON s.id=k.source_id "
                        "WHERE %s AND k.created_at < ? ORDER BY k.id" % _in("k.reviewed_at"), (a, b, a))
    reviewed = db.all("SELECT * FROM sources WHERE %s ORDER BY id" % _in("reviewed_at"), (a, b))
    runs = db.all("SELECT r.*, p.methods, p.hypothesis, p.knowledge_ids FROM runs r "
                  "JOIN proposals p ON p.id=r.proposal_id WHERE %s AND r.state IN (%s) ORDER BY r.id"
                  % (_in("r.updated_at"), ",".join("?" * len(TERMINAL))), (a, b) + TERMINAL)
    releases = db.all("SELECT * FROM releases WHERE %s OR %s ORDER BY id"
                      % (_in("created_at"), _in("updated_at")), (a, b, a, b))
    failures = db.all("SELECT split, case_type, kind, COUNT(*) AS n FROM failures WHERE %s "
                      "GROUP BY split, case_type, kind ORDER BY n DESC" % _in("created_at"), (a, b))
    ledger = db.all("SELECT * FROM budget_ledger WHERE day >= ? AND day <= ? ORDER BY day",
                    (since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d")))
    from .planner import open_problems
    problems = open_problems(db, db.all("SELECT * FROM knowledge"), cfg)
    return {"since": a, "until": b, "sources": sources, "knowledge": knowledge, "reverified": reverified,
            "reviewed": reviewed, "runs": runs, "releases": releases, "failures": failures, "ledger": ledger,
            "problems": problems}


# ------------------------------------------------------------------ rendering
def _clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _read(path, max_lines):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["… （共 %d 行，已截断）" % len(lines)]
    return "\n".join(lines)


def _release_diff(cfg, db, rel, max_lines):
    prev = db.get("releases", rel["previous_release_id"]) if rel["previous_release_id"] else None
    if not prev or prev["sha"] == rel["sha"]:
        return None, None
    paths = list(cfg["writable_paths"])
    root = cfg["project_root"]
    stat = versions.git(root, "diff", "--stat", prev["sha"], rel["sha"], "--", *paths, check=False)
    patch = versions.git(root, "diff", prev["sha"], rel["sha"], "--", *paths, check=False)
    lines = patch.splitlines()
    if len(lines) > max_lines:
        patch = "\n".join(lines[:max_lines] + ["… （共 %d 行，已截断）" % len(lines)])
    return stat, patch


def _holdout(db, run_id):
    rows = db.all("SELECT variant, metrics FROM evaluations WHERE run_id=? AND split='holdout' ORDER BY id",
                  (run_id,))
    out = {}
    for r in rows:
        m = loads(r["metrics"]) or {}
        out["baseline" if r["variant"] == "baseline" else "candidate"] = m
    return out


def _applied(data):
    """Releases created in the window from a run (the initial release has no run)."""
    return [r for r in data["releases"] if r["run_id"] and r["created_at"] >= data["since"]]


def render(db, cfg, data, title):
    jc = cfg.get("journal") or {}
    max_patch = int(jc.get("max_patch_lines", 120))
    L = ["# %s" % title, "",
         "时间范围：%s ～ %s。内容直接取自状态库和 Git，不经模型总结。" % (data["since"], data["until"]), ""]
    n_ready = sum(1 for r in data["runs"] if r["state"] == "READY")
    applied = _applied(data)
    rolled = [r for r in data["releases"] if r["status"] == "rolled_back" and r["updated_at"] >= data["since"]]
    L += ["## 概览", "",
          "| 新来源 | 新知识 | 结束的运行 | 达到采用条件 | 新发布 | 回滚 |", "|---|---|---|---|---|---|",
          "| %d | %d | %d | %d | %d | %d |" % (len(data["sources"]), len(data["knowledge"]), len(data["runs"]),
                                             n_ready, len(applied), len(rolled)), ""]

    # --- research
    L += ["## 一、研究与知识", ""]
    if data["sources"]:
        L += ["### 新读取的来源", "", "| # | 类型 | 标题 | 版本 | 读取 | 标记 |", "|---|---|---|---|---|---|"]
        for s in data["sources"]:
            flags = loads(s["flags"], []) or []
            L.append("| %d | %s | [%s](%s) | %s | %s | %s |" % (
                s["id"], s["kind"], _clip(s["title"] or s["url"], 70).replace("|", "/"), s["url"],
                _clip(s["revision"] or "", 14), s["read_status"],
                ("⚠ " + ", ".join(sorted({f.get("kind", str(f)) if isinstance(f, dict) else str(f)
                                          for f in flags}))) if flags else ""))
        L.append("")
    else:
        L += ["本期没有读取新来源。", ""]
    if data["knowledge"]:
        L += ["### 新知识", ""]
        for k in data["knowledge"]:
            L += ["#### #%d %s（%s%s）" % (k["id"], k["method"], VERIFICATION_ZH.get(k["verification"], k["verification"]),
                                         "" if k["applicable"] else "，判定当前不适用"),
                  "",
                  "- 出处：[%s](%s) %s，%s，%s" % (_clip(k["source_title"] or k["url"], 80), k["url"],
                                                k["revision"] or "", k["read_status"], k["locator"]),
                  "- 主张：%s" % k["claim"],
                  "- 做法：%s" % k["steps"],
                  "- 适用条件：%s" % k["conditions"],
                  "- 局限：%s" % k["limitations"],
                  "- 原文摘录：> %s" % _clip(k["excerpt"], 320),
                  "- 抽取方式：%s" % ("离线模式匹配（不是模型理解）" if k["extractor"] == "mock" else k["extractor"]),
                  ""]
    else:
        L += ["本期没有新增知识。", ""]
    if data["reverified"]:
        L += ["### 验证状态更新", "", "| 知识 | 方法 | 当前状态 | 依据的实验 |", "|---|---|---|---|"]
        for k in data["reverified"]:
            L.append("| #%d | %s | %s | %s |" % (k["id"], k["method"],
                                                 VERIFICATION_ZH.get(k["verification"], k["verification"]),
                                                 ", ".join(loads(k["experiments"], []) or [])))
        L.append("")
    if data["reviewed"]:
        L += ["### 来源复查", ""]
        for s in data["reviewed"]:
            L.append("- #%d %s：%s" % (s["id"], s["url"], s["review_note"] or "已复查"))
        L.append("")

    # --- code
    L += ["## 二、代码更新", ""]
    if data["runs"]:
        L += ["### 结束的实验运行", "",
              "| 运行 | 方法 | 结果 | 保留集 F1（基线 → 候选） | 原因 |", "|---|---|---|---|---|"]
        for r in data["runs"]:
            dec = loads(r["decision"], {}) or {}
            h = _holdout(db, r["id"])
            f1 = ("%s → %s" % (h["baseline"].get("f1"), h["candidate"].get("f1"))
                  if h.get("baseline") and h.get("candidate") else "—")
            L.append("| %d | %s | %s | %s | %s |" % (r["id"], ", ".join(loads(r["methods"], [])), r["state"], f1,
                                                     _clip("; ".join(dec.get("reasons", [])) or r["error"] or "",
                                                           120).replace("|", "/")))
        L.append("")
        for r in data["runs"]:
            if r["state"] in ("READY", "APPLIED") and r["patch_path"]:
                patch = _read(r["patch_path"], max_patch)
                if patch:
                    L += ["#### 运行 %d 的候选补丁（%s）" % (r["id"], r["state"]), "",
                          "假设：%s" % r["hypothesis"], "", "```diff", patch, "```", ""]
    else:
        L += ["本期没有结束的实验运行。", ""]
    if applied or rolled:
        L += ["### 发布与回滚", ""]
        for rel in applied:
            stat, patch = _release_diff(cfg, db, rel, max_patch)
            L += ["- 发布 #%d：`%s`，来自运行 %s，方式 %s，健康检查 %s" % (
                rel["id"], rel["sha"][:12], rel["run_id"], rel["policy"],
                "通过" if (loads(rel["health"], {}) or {}).get("healthy") else rel["health"])]
            if stat:
                L += ["", "```", stat, "```", "", "```diff", patch, "```", ""]
        for rel in rolled:
            L.append("- 回滚：发布 #%d（`%s`）已被回滚" % (rel["id"], rel["sha"][:12]))
        L.append("")
    else:
        L += ["本期没有发布或回滚，线上仍是版本 #%s。" % ((versions.current_release(db) or {}).get("id")), ""]

    # --- problems and usage
    L += ["## 三、失败与开放问题", ""]
    if data["failures"]:
        L += ["| 数据 | 类别 | 结果 | 数量 |", "|---|---|---|---|"]
        L += ["| %s | %s | %s | %d |" % (f["split"], f["case_type"], f["kind"], f["n"]) for f in data["failures"]]
        L.append("")
    if data["problems"]:
        for p in data["problems"]:
            L.append("- %s（%d 例）：%s%s" % (p["case_type"], p["count"], p["status"],
                                           "，" + p["note"] if p.get("note") else ""))
        L.append("")
    if not data["failures"] and not data["problems"]:
        L += ["无。", ""]

    L += ["## 四、用量", ""]
    if data["ledger"]:
        L += ["| 日期 | 任务 | 模型调用 | 输入 token | 输出 token | 下载 | 候选 | 费用 |",
              "|---|---|---|---|---|---|---|---|"]
        for d in data["ledger"]:
            cost = d["cost_usd"]
            L.append("| %s | %d | %d | %d | %d | %.1f MB | %d | %s |" % (
                d["day"], d["tasks"], d["model_calls"], d["input_tokens"], d["output_tokens"],
                (d["download_bytes"] or 0) / 1e6, d["candidates"],
                "$%.4f" % cost if cost else ("$0（离线模拟模型）" if cfg["model"]["provider"] == "mock"
                                              else "未记录")))
        L.append("")
        if cfg["model"]["provider"] == "mock":
            L += ["离线模拟模型的 token 数是按字符数估算的，不是计费数字。", ""]
    else:
        L += ["无记录。", ""]
    L += ["---", "", "说明：当前模型为 `%s`，执行器为 `%s`；评测数据是小规模虚构标注集，指标反映机制而不是真实新闻流效果。"
          % (cfg["model"]["provider"], cfg["executor"]["kind"]), ""]
    return "\n".join(L)


def render_knowledge_base(db):
    rows = db.all("SELECT k.*, s.url, s.title AS source_title, s.revision, s.read_status FROM knowledge k "
                  "JOIN sources s ON s.id=k.source_id ORDER BY k.method, k.id")
    L = ["# 知识库", "", "更新于 %s。共 %d 条知识，按方法分组；“来源已变化”的旧条目保留不删。" % (now(), len(rows)), ""]
    method = None
    for k in rows:
        if k["method"] != method:
            method = k["method"]
            L += ["## %s" % method, ""]
        status = VERIFICATION_ZH.get(k["verification"], k["verification"])
        if k.get("review_status") == "source_changed":
            status += "，来源已变化"
        L += ["- **#%d**（%s%s）%s" % (k["id"], status, "" if k["applicable"] else "，不适用", _clip(k["claim"], 200)),
              "  - 出处：[%s](%s) %s · %s · %s" % (_clip(k["source_title"] or k["url"], 70), k["url"],
                                                k["revision"] or "", k["read_status"], k["locator"]),
              "  - 局限：%s" % _clip(k["limitations"], 200)]
        exps = loads(k["experiments"], []) or []
        if exps:
            L.append("  - 实验：%s" % ", ".join(exps))
    L.append("")
    return "\n".join(L)


def render_changelog(db, cfg):
    L = ["# 代码变更记录", "", "被进化模块：`%s`。更新于 %s。" % (cfg["target_module"], now()), ""]
    for rel in db.all("SELECT * FROM releases ORDER BY id DESC"):
        L.append("## 发布 #%d · `%s` · %s" % (rel["id"], rel["sha"][:12], rel["created_at"]))
        L.append("")
        L.append("- 状态：%s；方式：%s；来自运行：%s；上一版本：%s" % (
            rel["status"], rel["policy"], rel["run_id"] or "—", rel["previous_release_id"] or "—"))
        if rel["run_id"]:
            run = db.get("runs", rel["run_id"])
            prop = db.get("proposals", run["proposal_id"]) if run else None
            if prop:
                L.append("- 方法：%s；依据知识：%s" % (", ".join(loads(prop["methods"], [])),
                                                ", ".join("#%s" % i for i in loads(prop["knowledge_ids"], []))))
                L.append("- 假设：%s" % _clip(prop["hypothesis"], 240))
        stat, _ = _release_diff(cfg, db, rel, 0)
        if stat:
            L += ["", "```", stat, "```"]
        L.append("")
    return "\n".join(L)


def _scrub(cfg, text):
    for real, alias in ((cfg["project_root"], "<repo>"), (os.path.expanduser("~"), "~")):
        text = text.replace(real, alias)
    return text


def summary(data):
    kn = data["knowledge"]
    runs = data["runs"]
    lines = ["新来源 %d，新知识 %d，结束的运行 %d（达到采用条件 %d），新发布 %d。" % (
        len(data["sources"]), len(kn), len(runs), sum(1 for r in runs if r["state"] == "READY"),
        len(_applied(data)))]
    for k in kn[:5]:
        lines.append("· 知识 #%d %s ← %s" % (k["id"], k["method"], _clip(k["source_title"] or k["url"], 50)))
    for r in runs[:5]:
        lines.append("· 运行 %d %s：%s" % (r["id"], ", ".join(loads(r["methods"], [])), r["state"]))
    return "\n".join(lines)


def write(db, cfg, period="daily", until=None):
    """Write the period journal and refresh the cumulative files. Returns a dict
    with paths and a short summary for notifications."""
    if period not in PERIODS:
        raise ValueError("period must be one of %s" % sorted(PERIODS))
    since, until = window(db, period, until)
    data = collect(db, cfg, since, until)
    if period == "daily":
        name, title = until.strftime("%Y-%m-%d"), "研究与进化日报 %s" % until.strftime("%Y-%m-%d")
    else:
        y, w, _ = until.isocalendar()
        name, title = "%d-W%02d" % (y, w), "研究与进化周报 %d 年第 %d 周" % (y, w)
    base = journal_dir(cfg)
    os.makedirs(os.path.join(base, period), exist_ok=True)
    path = os.path.join(base, period, name + ".md")
    files = {path: render(db, cfg, data, title), os.path.join(base, "KNOWLEDGE_BASE.md"): render_knowledge_base(db),
             os.path.join(base, "CHANGELOG.md"): render_changelog(db, cfg)}
    for p, text in files.items():
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_scrub(cfg, text))
        os.replace(tmp, p)
    counts = {"sources": len(data["sources"]), "knowledge": len(data["knowledge"]), "runs": len(data["runs"]),
              "releases": len(_applied(data))}
    db.insert("journals", {"period": period, "since": data["since"], "until": data["until"], "path": path,
                           "counts": counts, "created_at": now()})
    return {"period": period, "path": path, "since": data["since"], "until": data["until"], "counts": counts,
            "knowledge_base": os.path.join(base, "KNOWLEDGE_BASE.md"), "changelog": os.path.join(base, "CHANGELOG.md"),
            "summary": summary(data)}


def preview(db, cfg, period="daily"):
    """Summary of the current window without writing anything (for chat)."""
    since, until = window(db, period)
    return summary(collect(db, cfg, since, until))
