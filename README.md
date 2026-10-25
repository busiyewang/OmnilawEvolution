# Omnilaw Evolution

> 融汇万法，持续进化 —— 面向个人使用的自演化研究 Agent（第一、二阶段 + 真实能力接入 0.3.0）

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
| **第二阶段**：任务队列；用户、定时、失败记录、任务链四种触发 | ✅ 已实现并运行（见 `reports/demo2/DEMO2_REPORT.md`） |
| 增量来源发现、三层重复研究抑制、评测缓存、每日预算账本 | ✅ 已实现并运行；增量检索已真实联网运行 |
| 知识复查（Wikipedia 修订号 / GitHub 分支 / arXiv 版本） | ✅ 已实现；真实联网复查已运行 |
| 聊天命令（中英文，权限 + 一次性确认码）、反馈标注、通知 outbox | ✅ 已实现并运行 |
| 语义 / 跨语言去重（多语言句向量，ONNX，固定版本 + 哈希） | ✅ 已实现，**真实模型已在沙箱内运行**；本轮候选保留集 +0.043，未达 +0.05 门槛，未采用（见 `docs/INTEGRATIONS.md`） |
| 每日费用上限（调用前按最坏情况检查，未知价格拒绝付费调用） | ✅ 已实现并测试（模拟客户端） |
| Claude 模型适配器（`anthropic` SDK）与 Claude Code 编程执行器 | ⚠️ 已实现，**等待 API 密钥与费用上限**，未发出付费请求 |
| 飞书：回调模式 + 长连接模式（`lark-oapi`，无需公网地址） | ⚠️ 已实现，**等待应用凭据**，未连接真实租户 |
| 后台服务（launchd） | ✅ **已在本机安装并运行**；`service install/uninstall/status` |
| 自动采用 | ⏳ 第三阶段；默认关闭 |

演示用的是**离线模拟模型**（模式匹配 + 固定规划）和**模板执行器**（代码来自 `templates.py`，不是模型写的），
数据是**虚构的小规模标注集**。演示证明的是闭环机制可靠运行，不是通用去重能力的提升。

## 快速开始

需要 Python ≥ 3.9 和 Git，核心只用标准库。可选能力需要 Python ≥ 3.10：
`pip install -e '.[semantic]'`（句向量）、`'.[feishu]'`（飞书长连接）、`'.[anthropic]'`（Claude），或 `'.[all]'`。

```bash
git clone git@github.com:busiyewang/OmnilawEvolution.git omnilaw-evolution
cd omnilaw-evolution
python3 -m pip install -e .          # 或者不安装，用 PYTHONPATH=src python3 -m omnilaw_evolution ...

# 测试（Python 3.9 约 5 分钟；缺可选依赖的测试会跳过）
PYTHONPATH=src python3 -m unittest discover -s tests

# 第一阶段演示：在 .demo/ 里新建演示实例，跑完整闭环，报告写到 reports/demo/
omnilaw-evolution demo --workdir .demo --report-out reports/demo          # 末尾含真实联网检查
omnilaw-evolution demo --workdir .demo --offline                          # 完全离线

# 第二阶段演示：队列 + 定时 + 失败驱动研究 + 抑制 + 缓存 + 聊天（模拟飞书事件）
omnilaw-evolution demo2 --workdir .demo2 --report-out reports/demo2       # 末尾含真实联网检索与复查
omnilaw-evolution demo2 --workdir .demo2 --offline
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

### 持续研究（第二阶段）

```bash
omnilaw-evolution queue add --kind research --topic "paraphrase identification"
omnilaw-evolution queue list
omnilaw-evolution schedule add --name daily-check --cron "0 8 * * *" --kind evaluate_release --payload '{"split":"batch2"}'
omnilaw-evolution schedule add --name weekly-review --cron "0 9 * * 1" --kind knowledge_review
omnilaw-evolution schedule tick                       # 把到期的定时任务放入队列
omnilaw-evolution worker --max-tasks 10               # 在今日预算内消化队列
omnilaw-evolution chat "状态"                          # 本地聊天（与飞书同一套命令）；不带参数进入交互模式
omnilaw-evolution feedback --item b02 --duplicate-of b01
omnilaw-evolution review --limit 5                    # 知识复查（联网）
omnilaw-evolution budget                              # 今日用量与剩余
omnilaw-evolution outbox                              # 通知记录
omnilaw-evolution serve-feishu --port 8787            # 飞书事件回调（接入步骤见 docs/PHASE2.md）
omnilaw-evolution daemon                              # 后台循环：需 background=true 且设置每日预算
```

### 真实能力（0.3.0）

```bash
omnilaw-evolution models fetch       # 下载并校验固定版本的多语言句向量模型（470 MB），再设 "semantic": {"enabled": true}
omnilaw-evolution env                # 在 ~/.config/omnilaw-evolution/env 生成凭据模板（权限 600，仓库之外）
omnilaw-evolution doctor             # 哪些能力可用、还缺什么（只显示变量名）
omnilaw-evolution serve-feishu --mode ws                  # 飞书长连接，无需公网地址
omnilaw-evolution service install [--services daemon feishu]   # 安装为 launchd 服务
```

接入细节、真实结果和剩余步骤见 [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md)。

触发、抑制、缓存、复查、权限与飞书接入的说明见 [`docs/PHASE2.md`](docs/PHASE2.md)。

### 使用真实模型（等待凭据，未发出付费请求）

```bash
python3 -m pip install -e '.[anthropic]'   # 需要 Python ≥ 3.10
omnilaw-evolution env                      # 在生成的文件里填 ANTHROPIC_API_KEY，不要写进仓库
```

在 `omnilaw.json` 中设置 `"model": {"provider": "anthropic", "model": "claude-opus-5-5",
"price_per_mtok": {"input": 4, "output": 20}}` 和 `"budget": {"max_daily_cost_usd": X}`。
每次调用前先精确计数输入 token，按最坏情况检查当日上限；没配价格就拒绝调用。
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
fixtures/discovery/      录制的 arXiv 检索结果（摘要级），用于离线回放增量发现
tests/                   95 个测试（单元 + 端到端 + 红队 + 第二阶段流程 + 真实能力接入）
reports/demo/            第一阶段演示产物：报告、补丁、日报、来源与知识记录
reports/demo2/           第二阶段演示报告（中文）
docs/DESIGN.md           环境核对、参考项目核对与选型、架构、隔离设计、已知局限
docs/PHASE2.md           第二阶段：触发、抑制、缓存、复查、预算、聊天与飞书接入
docs/INTEGRATIONS.md     0.3.0：句向量、费用上限、飞书长连接、后台服务；真实结果与剩余步骤
docs/deploy/             launchd / systemd / crontab 示例（未安装）
```

## 许可证

MIT。`fixtures/sources/` 中的第三方内容见其中的 `NOTICE.md`。
