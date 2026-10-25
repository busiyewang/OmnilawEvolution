# Omnilaw Evolution

> 融汇万法，持续进化 —— 面向个人使用的自演化研究 Agent（第一阶段）

Omnilaw Evolution 读取网页、GitHub 源码和论文，把其中适用的方法写成**带出处的知识记录**，据此提出改进假设，
在**独立 checkout + 操作系统沙箱**中修改自己实际使用的源码模块，用**固定评测器**比较基线与候选，
只有满足预先声明的采用条件才可发布，并且可以**验证后回滚**。

第一阶段的进化对象是技术日报采集流程（`collector/pipeline.py`）实际调用的去重模块 `collector/dedup.py`。

## 当前状态（诚实版）

| 能力 | 状态 |
|---|---|
| 三类来源读取（网页 / GitHub@SHA / arXiv）、缓存、大小与超时限制、内容哈希去重、注入特征扫描 | ✅ 已实现，**已真实联网运行**（见 `reports/demo/DEMO_REPORT.md` §9） |
| 知识记录（主张、步骤、条件、局限、逐字定位、三级验证状态） | ✅ 已实现并运行（离线模拟抽取） |
| 改进规划、重复实验抑制、未覆盖失败 → 开放问题 | ✅ 已实现并运行（离线模拟规划） |
| 候选生成 → 范围守卫 → 沙箱评测 → 采用决策 → 发布 → 健康检查 → 回滚 | ✅ 已实现并运行 |
| 停止、超时、预算耗尽、断点恢复（累计预算）、同目标互斥 | ✅ 已实现并运行 |
| Claude 模型适配器（官方 `anthropic` SDK）与 Claude Code 编程执行器 | ⚠️ 已实现，**未实际运行**（本机无凭据，也未获付费授权） |
| 定时/后台运行、Hermes/飞书适配、自动采用 | ⏳ 第二、三阶段；默认关闭 |

演示用的是**离线模拟模型**（模式匹配 + 固定规划）和**模板执行器**（代码来自 `templates.py`，不是模型写的），
数据是**虚构的小规模标注集**。演示证明的是闭环机制可靠运行，不是通用去重能力的提升。

## 快速开始

需要 Python ≥ 3.9 和 Git，核心只用标准库。

```bash
git clone git@github.com:busiyewang/OmnilawEvolution.git omnilaw-evolution
cd omnilaw-evolution
python3 -m pip install -e .          # 或者不安装，用 PYTHONPATH=src python3 -m omnilaw_evolution ...

# 测试（约 25 秒，全部离线）
PYTHONPATH=src python3 -m unittest discover -s tests

# 完整演示：在 .demo/ 里新建演示实例，跑完整闭环，报告写到 reports/demo/
omnilaw-evolution demo --workdir .demo --report-out reports/demo          # 末尾含真实联网检查
omnilaw-evolution demo --workdir .demo --offline                          # 完全离线
```

演示会从当前源码树复制出一个新的 git 仓库作为“演示实例”，所有发布与回滚都发生在演示实例里，
不会改动你的开发仓库。需要在源码 checkout 中运行（它要读取 `eval/` 与 `fixtures/`）。

## 日常用法

```bash
cp omnilaw.example.json omnilaw.json     # 路径、预算、采用条件都在这里；凭据只写环境变量名
omnilaw-evolution init                   # 把当前 HEAD 登记为第一个稳定版本
omnilaw-evolution ingest --url https://arxiv.org/abs/2107.06499v2
omnilaw-evolution ingest --url https://github.com/ekzhu/datasketch/blob/master/datasketch/minhash.py   # 自动固定到 SHA
omnilaw-evolution ingest --snapshot fixtures/sources/manifest.json   # 离线快照
omnilaw-evolution research --topic "near-duplicate detection" --ingest 3
omnilaw-evolution propose --goal "减少重复，同时保留有新信息的材料"
omnilaw-evolution run --proposal 1 --mode shadow      # 只产出候选、补丁和报告
omnilaw-evolution status --run 1
omnilaw-evolution report --run 1
omnilaw-evolution stop --run 1                        # 另一个终端里
omnilaw-evolution resume --run 1 [--extend-candidates 2]
omnilaw-evolution apply --run 1                       # 仅 READY 可用；健康检查失败则不切换
omnilaw-evolution collect --input fixtures/batches/daily_2026-10-24.jsonl --out digest.md
omnilaw-evolution evaluate-release --split batch2     # 新批次复用，并记录新失败
omnilaw-evolution rollback --release 2                # 校验 SHA、依赖哈希、状态 schema、健康检查后切回
```

运行状态：`QUEUED → RESEARCHING → PLANNED → PATCHING → EVALUATING → READY | REJECTED`，
`READY → APPLIED → ROLLED_BACK`，任一执行阶段可进入 `FAILED / CANCELLED / BUDGET_EXHAUSTED`（可恢复）。

### 使用真实模型（未在本机验证）

```bash
python3 -m pip install -e '.[anthropic]'   # anthropic SDK 1.x 需要 Python ≥ 3.10
export ANTHROPIC_API_KEY=...               # 或 `ant auth login`
```

在 `omnilaw.json` 中设置 `"model": {"provider": "anthropic", "model": "claude-opus-5-5"}`，
以及 `"budget": {"max_model_calls": N, "max_daily_cost_usd": X}`。适配器默认开启服务端拒答回退
（`fallbacks: "default"`，beta），不需要可删掉 `llm.py` 中对应的两行。
编程执行器可改为 `"executor": {"kind": "claude-code"}`：它在候选目录里以无头模式调用 `claude` CLI，
同样受沙箱（禁读评测目录、写入限于候选目录）和范围守卫约束。

## 采用规则（默认，可在配置中修改）

* 候选只在开发集上筛选；开发集选中的一个变体在保留集上**只评一次**。
* 保留集 F1 至少提高 0.05；误删率 ≤ 5%；不得删除任何 `critical` 条目；≤ 50 ms/条；
  输出契约、确定性、空输入检查和受保护的契约测试全部通过。
* 没有候选满足条件时保留基线，结论就是“本轮未找到可采用版本”。

## 安全边界

* 候选只能写 `writable_paths`（默认只有 `collector/dedup.py`）；评测器、测试、配置、预算与发布代码受保护。
* 候选 checkout 用稀疏检出排除 `eval/`；候选代码运行在沙箱里，禁网、禁读评测数据与状态库。
* 外部网页、论文、仓库文本只作为数据：会被扫描注入特征并标记，从不执行，也不会改动配置；
  模型输出须通过 schema 校验，引文须能在原文中逐字定位。
* 默认 `auto_apply=false`、`background=false`、`max_daily_cost_usd=0`。

## 目录

```
src/omnilaw_evolution/   核心实现（见 docs/DESIGN.md §4）
  collector/             被进化的真实模块：pipeline.py（日报）→ dedup.py
eval/                    受保护的评测数据（dev / holdout / batch2 + MANIFEST）
fixtures/sources/        三类来源的固定快照 + 一个对抗性注入页面
fixtures/batches/        日报输入快照
scripts/build_datasets.py  标注数据生成脚本（全部虚构）
tests/                   48 个测试（单元 + 端到端 + 红队）
reports/demo/            一次实际演示的完整产物：报告、补丁、日报、来源与知识记录
docs/DESIGN.md           环境核对、参考项目核对与选型、架构、隔离设计、已知局限
```

## 许可证

MIT。`fixtures/sources/` 中的第三方内容见其中的 `NOTICE.md`。
