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
| 语义 / 跨语言去重（多语言句向量，ONNX，固定版本 + 哈希） | ✅ 已实现，**真实模型已在沙箱内运行**；旧版演示 +0.043 未采用的历史结论保留；新版评测已扩充公开样本并支持高分基线的相对改进（见 `eval/README.md`） |
| 每日费用上限（调用前按最坏情况检查，未知价格拒绝付费调用） | ✅ 已实现并测试（模拟客户端） |
| Claude 模型适配器（`anthropic` SDK）与 Claude Code 编程执行器 | ⚠️ 已实现，**等待 API 密钥与费用上限**，未发出付费请求 |
| 飞书：回调模式 + 长连接模式（`lark-oapi`，无需公网地址） | ⚠️ 已实现，**等待应用凭据**，未连接真实租户 |
| 后台服务（launchd） | ✅ **已在本机安装并运行**；`service install/uninstall/status` |
| 自动采用 | ⏳ 第三阶段；默认关闭 |

演示用的是**离线模拟模型**（模式匹配 + 固定规划）和**模板执行器**（代码来自 `templates.py`，不是模型写的），
数据是**虚构的小规模标注集**。演示证明的是闭环机制可靠运行，不是通用去重能力的提升。

默认正式评测已升级为 **790 条**（开发 266 / 保留 268 / 后续批次 256）：原有 70 条流程样本加上
720 条公开 PAWS-X 句子，覆盖中英文复述、双向跨语言和难负例。来源版本、哈希、采样规则与局限见
[`eval/README.md`](eval/README.md)。它仍是外部代理评测，不能替代真实新闻流的人工审核标注。

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

### 本地自然语言聊天

本地控制台现在直接使用随项目分发的 **Hermes AIAgent 核心**。配置 DeepSeek 后，
可以自然聊天，也可以让它检查本机进程、读写代码、执行命令、根据错误修复并重新验证。
安装智能体依赖需要 Python 3.14；后台研究与基础命令仍保留标准库运行方式：

```bash
.venv/bin/python -m pip install -e '.[agent]'
.venv/bin/omnilaw-evolution chat
```

会话保存在本地 SQLite 中，默认进入 `default` 会话并恢复上下文。`/new` 开始新会话且保留旧记录，
`/history` 查看最近记录，`/tools` 查看实际加载工具，`chat --session <名称>` 切换会话，
`/exit` 或 Ctrl-D 退出，Ctrl-C 中断本轮。修改版本后，应退出旧控制台再启动。

| 能力 | 接入方式 |
| --- | --- |
| 连续执行与纠错 | 直接复用 Hermes 的模型→工具→结果→下一轮循环，默认最多 30 轮、15 分钟 |
| 本机操作 | terminal、process_manage；runtime_info 返回真实 PID、目录与 daemon 心跳 |
| 编程 | read_file、write_file、patch、search_files、execute_code |
| 研究 | web_search（DDGS 无需额外 Key）、web_extract（本地提取网页正文）、browser_exec |
| 任务管理 | todo_list、clarify；delegate_task 可拆分复杂任务，模型请求仍走同一预算接口 |
| 持久记忆与技能 | Hermes memory、skills_list/skill_view/skill_manage，独立的 Omnilaw 数据目录 |
| 历史和长任务 | 保存完整工具记录、session_search、Hermes 上下文压缩，重启后恢复压缩后的上下文 |
| 原有研究系统 | project_command 连接状态、队列、报告、预算、反馈、实验与发布 |

`chat.engine` 默认为 `hermes`，仅本地 `local` 用户的 CLI 启用本机工具；飞书和其他身份不自动获得 shell 权限。
设置 `"chat": {"engine": "basic"}` 可使用原来的轻量项目聊天（最近 20 轮、有限字符上下文、最多 3 次工具调用）。
Hermes 依赖缺失时会提示安装，不会悄悄切换成能力不足的聊天模式。

项目命令仍经过权限检查。研究流程的采用和回滚需要用户亲自输入 `/confirm <码>`。
本地智能体的 shell 使用当前用户权限，是真实本机操作，不是研究候选的沙箱。
保留 Hermes 的危险命令审核与确认；非交互调用不能代替用户回答确认。
`/status`、`/queue` 等命令及原有中文命令仍直接执行，不消耗模型调用。
聊天与后台研究共享每日费用账本；DeepSeek 请求在发送前原子预占保守费用，成功后结算，
避免聊天和后台任务同时读取相同余额。Hermes 的主循环、风险审核、压缩、历史摘要和子智能体的模型调用，
统一经过只绑定 `127.0.0.1`、使用临时令牌的模型接口。上游模型、价格、最大输出由宿主固定；
DeepSeek Key 不传给智能体子进程。额度检查不覆盖用户自行通过 shell 发起的外部服务消费。

模型源代码固定在 [Hermes 提交 6dca67e](https://github.com/NousResearch/hermes-agent/tree/6dca67ed4dc5d11fb08e949737f69ed1a0737a7a)，
源码包和 MIT 许可证随项目分发，来源、校验和及唯一的启动器适配见
[`src/omnilaw_evolution/_vendor/README.md`](src/omnilaw_evolution/_vendor/README.md)。
不需要本机原有 Hermes 源码目录；不会复制其配置、Key、记忆或用户会话。
Omnilaw 自己的记忆、技能、会话和运行时位于 `state_dir/agent` 与 `state_dir/runtime`，均不提交 Git。
浏览器使用 Hermes 的 browser-harness，本机需有可用的 Chromium/Chrome；语音、图片生成、聊天平台和桌面 UI 不在此集成范围。

### 真实能力（0.3.0）

```bash
omnilaw-evolution models fetch       # 下载并校验固定版本的多语言句向量模型（470 MB），再设 "semantic": {"enabled": true}
omnilaw-evolution env                # 在 ~/.config/omnilaw-evolution/env 生成凭据模板（权限 600，仓库之外）
omnilaw-evolution doctor             # 哪些能力可用、还缺什么（只显示变量名）
omnilaw-evolution serve-feishu --mode ws                  # 飞书长连接，无需公网地址
omnilaw-evolution service install [--services daemon feishu]   # 安装为 launchd 服务
omnilaw-evolution journal [--period weekly] [--preview]        # 研究与代码变更的日报/周报 + 知识库 + 变更记录
omnilaw-evolution schedule add --name daily-journal --cron "0 21 * * *" --kind journal --payload '{"period":"daily"}'
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

### DeepSeek 接入

基础研究流程支持 DeepSeek，无需安装额外 SDK；完整本地智能体安装上面的 `.[agent]`。在本地 `omnilaw.json` 中配置：

```json
{
  "model": {
    "provider": "deepseek",
    "model": "deepseek-flash",
    "api_key_env": "DEEPSEEK_API_KEY",
    "max_tokens": 4096,
    "context_tokens": 1048576,
    "timeout_seconds": 120,
    "price_per_mtok": {"input": 0.3, "output": 1.2}
  },
  "budget": {"max_daily_cost_usd": 5}
}
```

运行 `omnilaw-evolution env`，在仓库外的 `~/.config/omnilaw-evolution/env` 中填写
`DEEPSEEK_API_KEY`，文件权限保持 `600`；CLI 会自动读取。用 `omnilaw-evolution doctor` 检查配置。
DeepSeek 用于知识抽取与方案规划；`executor.kind=template` 仍由模板生成候选代码。

上述价格按 2026-10-26 [官方价格表](https://api-docs.deepseek.com/quick_start/pricing/)
的 Flash 高峰、输入缓存未命中费率配置；本地账本保守计费，实际账单可能更低。
调用前按完整上下文容量加最大输出检查预算，因此剩余额度不足一次保守估算时会提前停止。
模型或价格调整后应同步更新配置。HTTP 传输失败或响应缺少用量时按保守估算记账，不自动重试。
JSON 输出还需通过本地 schema 校验。DeepSeek 使用原子额度预占；其他供应商仍沿用现有费用检查机制。

## 采用规则（默认，可在配置中修改）

* 候选只在开发集上筛选；开发集选中的一个变体在保留集上**只评一次**。
* 保留集 F1 严格提高，且满足其一：绝对提高 ≥ 0.05；或剩余 F1 差距减少 ≥ 50% 且零误删。
  后者计算为 `(候选 F1 − 基线 F1) / (1 − 基线 F1)`，基线满分时不适用；可设
  `criteria.min_relative_error_reduction=null` 关闭此路径。
* 两条路径都要求：误删率 ≤ 5%；不得删除任何 `critical` 条目；≤ 50 ms/条；
  输出契约、确定性、空输入检查和受保护的契约测试全部通过。
* 没有候选满足条件时保留基线，结论就是“本轮未找到可采用版本”。
* 数据或评测规则变更后，旧提案和旧 READY 候选不能继续运行或直接发布；重新执行 `propose`、`run`，
  在同一新版评测下重新测量基线与候选。不会自动采用，也不会改写历史结论。

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
scripts/build_datasets.py  离线构建正式评测 / 虚构演示数据
scripts/import_pawsx.py    从固定版本 TSV 导入可溯源公开样本
fixtures/discovery/      录制的 arXiv 检索结果（摘要级），用于离线回放增量发现
tests/                   单元 + 端到端 + 红队 + 真实能力接入 + 数据溯源与评测版本测试
reports/demo/            第一阶段演示产物：报告、补丁、日报、来源与知识记录
reports/demo2/           第二阶段演示报告（中文）
docs/DESIGN.md           环境核对、参考项目核对与选型、架构、隔离设计、已知局限
docs/ARCHITECTURE.md     整体架构、设计模式、参考项目的借鉴方式与取舍
docs/PHASE2.md           第二阶段：触发、抑制、缓存、复查、预算、聊天与飞书接入
docs/INTEGRATIONS.md     0.3.0：句向量、费用上限、飞书长连接、后台服务；真实结果与剩余步骤
docs/deploy/             launchd / systemd / crontab 示例（未安装）
```

## 许可证

MIT。`fixtures/sources/` 中的第三方内容见其中的 `NOTICE.md`。
