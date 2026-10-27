# Omnilaw Evolution — 设计与选型

版本 0.3.0，更新于 2026-10-26，对应 Hermes 集成提交 `2bb576b`。

当前系统包含两个执行流程：**本地智能体**使用 Hermes 的真实 `AIAgent` 循环和工具完成用户委托的任务；
**后台研究系统**继续按来源、知识、提案、候选、评测、人工采用的流程改进去重模块。
二者通过 `project_command` 和每日费用账本衔接，执行权限与发布规则分别管理。
进程关系、消息循环和存储设计见 [ARCHITECTURE.md](ARCHITECTURE.md)；
部署与验收记录见 [INTEGRATIONS.md](INTEGRATIONS.md)；第二阶段历史设计见 [PHASE2.md](PHASE2.md)。

## 1. 运行环境与依赖

### 1.1 当前运行方式（2026-10-26）

| 部分 | 要求 / 当前配置 |
|---|---|
| 研究核心、基础命令 | Python ≥ 3.9、标准库、SQLite、Git；可选供应商与语义模型另装依赖 |
| Hermes 本地智能体 | 当前适配器要求 **Python 3.14**；本机 `.venv` 为 3.14.4，安装 `.[agent]` |
| 源码来源 | 随包携带固定版本 Hermes 源码、清单和 MIT 许可证；运行不依赖原 Hermes 安装目录 |
| 推理服务 | 本地配置为 DeepSeek `deepseek-flash`；研究抽取、规划和本地聊天共用适配器与费用账本 |
| 本机工具 | 当前用户权限的终端、文件和进程；浏览器另需 Chrome / Chromium，使用独立无头配置 |
| 本地状态 | `omnilaw.json` 与 `.omnilaw/` 均被 Git 忽略；Key 在仓库外的凭据文件中 |
| 费用与后台 | 本机授权上限为每天 $5，`background=true`、`auto_apply=false`；发行默认仍是 mock、零付费额度、后台关闭 |

`chat.engine=hermes` 仅在 `model.provider=deepseek`、`user=local`、`channel=cli` 时启用本机工具。
依赖缺失会返回安装提示；`chat.engine=basic` 可显式使用只有项目命令工具的聊天模式。
Hermes 本地工具没有候选评测沙箱的隔离，详见第 5 节。

### 1.2 首版环境记录（2026-10-25，历史）

以下保留首版选型依据，不代表当前 Python、模型或 Hermes 安装状态。

| 项 | 实测 |
|---|---|
| 系统 | macOS 14.4.1 (Darwin 23.4.0), arm64, Apple M2 Pro, 32 GB 内存，可用磁盘约 62 GB |
| Python | `/usr/bin/python3` 3.9.6（另一个 Framework Python 3.7 启动即被系统杀死，不使用） |
| Git | 2.39.2 |
| 执行隔离 | `sandbox-exec` 可用并已验证（禁网、禁读指定目录、写入限制）；Docker CLI 存在但守护进程未运行 |
| 已有助手 | 未发现 Hermes 或 OpenClaw；存在 `claude` CLI、`codex` CLI |
| 模型凭据 | 环境中没有 `ANTHROPIC_API_KEY`；首版默认使用离线模拟适配器 |
| 网络 | 通过代理可访问 GitHub API、arXiv、Wikipedia；GitHub SSH 认证可用 |

因此首版只依赖 Python 标准库 + SQLite + Git，`sandbox-exec`（macOS）/`bwrap`（Linux）做执行隔离，
都不可用时降级为普通子进程并在每条评测记录里写明 `isolation=none`。

## 2. 参考项目与复用决策

### 2.1 当前采用：Hermes Agent 核心（2026-10-26）

直接复用 `NousResearch/hermes-agent` 的 `run_agent.AIAgent`、工具分发器、工具、记忆、技能、
会话检索和上下文压缩。固定提交为 `6dca67ed4dc5d11fb08e949737f69ed1a0737a7a`，MIT 许可证。
源码包仅包含固定提交中的文件，不复制用户配置、Key、记忆、会话或虚拟环境。
打包脚本、来源清单和许可证见 [源码说明](../src/omnilaw_evolution/_vendor/README.md)。

选择复用的原因是原项目聊天只有有限轮次的 `project_command`，无法独立检查本机、执行代码和根据结果纠错。
复用实际循环后，模型可以在同一任务中连续调用工具、观察错误、修复并验证。
Omnilaw 的适配代码负责本地进程生命周期、凭据与计费桥接、项目命令和持久化，保留原研究状态机。
上游唯一源码适配位于 `hermes_bootstrap.py`：跳过独立更新器和包管理器激活，由 Omnilaw 管理依赖。

此处的 **Hermes Agent** 与下面首版调研的 **Hermes Self-Evolution** 是不同仓库；后者没有作为依赖引入。

### 2.2 首版参考项目核对（2026-10-25，历史）

| 项目 | 固定版本 | 许可证 | 核对结论 | 本项目的处理 |
|---|---|---|---|---|
| [MOSS](https://github.com/hkgai-official/Moss) | `5453f1fe` (2026-05-23) | Apache-2.0 | README 确认：失败批次 → 改写自身 TypeScript 源码 → 临时 worker 重放 → **用户明确授权后**替换容器，90 s 健康探测失败则回滚 | 借鉴流程：候选与发布分离、READY≠APPLIED、健康检查门控的切换与回滚。未复用代码（TypeScript/OpenClaw/Docker 栈） |
| [Darwin Gödel Machine](https://github.com/jennyzzt/dgm) | `a565fd2d` (2025-08-13) | Apache-2.0 | 研究代码，候选档案 + 编程基准筛选 | 借鉴“候选档案 + 固定评测器筛选”的思路（`runs`/`evaluations` 表）；首版不做开放式档案搜索 |
| [RD-Agent](https://github.com/microsoft/RD-Agent) | `484776c2` (2026-09-23) | MIT | 读论文 → 实现 → 实验反馈，面向数据科学，官方以 Linux 为主 | 借鉴“研究→实现→反馈”的阶段划分；不引入依赖 |
| [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve) | `5153a7d7` (2026-10-25) | Apache-2.0 | 评测器驱动的代码变体生成与筛选 | 借鉴“多个变体 → 评测器选择”；本项目把变体数放进预算，且只在开发集上筛选 |
| [GEPA](https://github.com/gepa-ai/gepa) | `7af5f0c5` (2026-10-22) | MIT | 基于执行记录反思优化文本参数 | 作为第二阶段候选优化器的备选；首版未用 |
| [Hermes Self-Evolution](https://github.com/NousResearch/hermes-agent-self-evolution) | `0a929e3a` (2026-06-17) | 仓库未声明 | README 状态表：仅 Phase 1（SKILL.md 优化）已实现，工具描述/系统提示/工具代码/持续循环均为 Planned | 不作为依赖；当前集成的是上节另一个 MIT 许可的 Hermes Agent 仓库 |
| [Awesome Self-Evolving Coding Agents](https://github.com/iSEngLab/Awesome-Self-Evolving-Coding-Agents) | `7f5ba6d3` (2026-10-04) | 仓库未声明 | 索引 | 仅用于发现，不作为效果证据 |

论文核对：arXiv 2605.22794v2（MOSS）、2505.22954v3（DGM）、2608.02636v1（多轮技能进化反馈）题目与任务书一致。
2608.02636 的结论（有效改进并非每轮出现、验证集与测试集收益可能不同）直接体现在本项目的规则中：
允许“本轮未找到可采用版本”，开发集筛选与保留集采用分离，保留集曝光次数受限。

**首版复用 vs 自建结论**：上述项目都是完整框架或研究代码，栈（TypeScript/Docker/DSPy）与首版需求（单机、离线可跑、
可审计）不匹配。首版全部自建，约 3.1k 行标准库 Python（另有约 600 行测试）；从中借用的是流程与约束设计，没有复制代码。
2026-10-26 的 Hermes Agent 集成增加了直接源码复用，不再适用“整个项目全部自建”的描述。

## 3. 演示任务中实际学习的三类来源

| 类型 | 来源 | 固定版本 | 读取状态 | 产出 |
|---|---|---|---|---|
| 网页 | Wikipedia “URI normalization” | oldid=1359957963 | 全文 | `url_canonicalization`（采纳） |
| GitHub | ekzhu/datasketch `datasketch/minhash.py` | `c7eaaeea` | 全文（MIT） | `minhash_lsh`（记录，判定当前规模不适用，未采纳） |
| 论文 | Lee et al. 2022, arXiv:2107.06499v2 | v2 | 在线读过 HTML 全文；仓库只存摘要与 §4.2、附录 A 摘录 | `shingle_jaccard`（采纳，含复现差异说明） |

复现差异（记录在知识条目的 limitations 中）：论文在英文大规模训练语料上用空格分词 5-gram + MinHash(9000) +
编辑相似度 > 0.8；本项目每日批次只有几十条，改用**字符** 4-gram 的精确 Jaccard（中文无空格分词），阈值在开发集上筛选。
这不是对论文结论的复现，只是方法在本任务上的适配。

## 4. 架构

```
src/omnilaw_evolution/
  sources.py     来源发现与读取（网页 / GitHub@SHA / arXiv），超时、大小上限、缓存、内容哈希去重、注入扫描
  knowledge.py   知识记录：主张、方法、条件、局限、精确位置、验证等级（来源声称/本地复现/批次有效）
  llm.py         MockLLM、AnthropicLLM、DeepSeekLLM；研究 JSON 校验，DeepSeek 聊天 / 预算化原始响应
  planner.py     改进假设：引用知识 id，固定目标模块、方法、变体与采用条件；重复实验抑制；未覆盖失败 → 开放问题
  executors.py   编程执行器：TemplateExecutor（离线模板）/ ClaudeCodeExecutor（claude CLI，沙箱内）/ 红队执行器
  policy.py      可写范围守卫（git 全量变更含 ignored、删除、符号链接）+ 采用规则
  evaluator.py   独立评测：只把无标签条目交给沙箱内的候选代码，标签与打分留在评测进程
  sandbox.py     sandbox-exec / bwrap / none
  versions.py    候选 worktree（稀疏检出排除 eval/）、发布 worktree、active 符号链接原子切换、健康检查、回滚校验
  runner.py      状态机、检查点、停止、恢复、预算、同目标互斥锁
  report.py      Markdown 报告
  collector/     被进化的真实模块：pipeline.py（技术日报）调用 dedup.py

  # 第二阶段（详见 docs/PHASE2.md）
  taskqueue.py   任务队列（原子领取、重试、取消、去重键）+ 五字段 cron 调度
  worker.py      任务处理器、每日预算门控、通知、失败驱动研究、守护进程
  discovery.py   增量来源发现（已见记录、相关性打分、积压）；实时与快照两种提供者
  review.py      知识复查（修订号 / 分支 / 版本），新旧来源并存
  chat.py        渠道无关的聊天命令、权限、一次性确认码、反馈标注
  notify.py      outbox + mock / stdout / 飞书 webhook
  adapters/feishu.py  飞书事件回调与回复

  # 当前本地智能体（2026-10-26）
  conversation.py    自然语言与命令路由、会话锁、工具记录、上下文恢复；Hermes / basic 两种模式
  hermes_runtime.py  校验并解包源码，维护独立 HERMES_HOME、持久子进程、事件协议、中断与退出
  hermes_worker.py   嵌入上游 AIAgent，注册 runtime_info / project_command，转发进度、澄清与审批
  agent_bridge.py    仅本机回环的临时令牌接口，代理模型请求与项目命令，写入上下文检查点
  agent_web.py       本地 HTML / 文本提取提供者，限制下载并检查重定向地址
  costs.py          共享费用守卫；DeepSeek 调用前原子预占，返回有效 usage 后结算
  _vendor/          固定 Hermes 源码包、sha256 清单、上游许可证与适配说明
```

数据：主 SQLite 当前为 **schema v6**，保留研究、队列、日程、账本、通知等表，
v5 增加 `chat_turns`，v6 增加 `agent_sessions`，均为新增迁移。
`chat_turns` 保存逐轮工具与文字记录，`agent_sessions` 保存恢复所需的完整或压缩后上下文。
Hermes 的 `sessions.db`、记忆和技能按用户与渠道共享；运行时配置与日志按会话分开。
文件保存来源快照、补丁、逐样例结果、日志；Git 保存版本（候选分支 `omnilaw/run-<id>-v<n>`）。

## 5. 隔离与安全边界

### 5.1 后台研究候选

以下沙箱和可写范围限制针对 `runner` / 执行器生成、评测和发布的候选代码。

* **源码隔离**：候选在独立 git worktree 中生成，稀疏检出排除 `/eval/`，候选生成阶段看不到保留集。
* **执行隔离**：候选代码只在沙箱中运行：禁网，禁止读 `eval_dir` 与状态库，只能写本次评测的临时目录。红队测试
  （`redteam-exfil`）验证读保留集和联网都被拦截。
* **可写范围**：只有 `writable_paths`（默认仅 `collector/dedup.py`）。评测器、测试、配置、预算、发布控制在
  `protected_paths`，越界、删除、符号链接、ignored 文件都会被拒绝（红队 `redteam-scope` 验证）。
* **评测器版本**：覆盖评测代码、harness、采用规则、数据清单和实际数据；读取时校验哈希。
  提案冻结版本，跨版本运行、恢复和采用会被拒绝，必须重提案并重测基线。
* **保留集**：每轮只对开发集选出的一个变体评一次；每日曝光次数上限；保留集失败样例不写入规划可读的 `failures` 表。
* **外部文本**：只作为数据保存并扫描注入特征；模型输出必须通过 schema 校验，引用必须能在原文中逐字定位；
  任何来源文本都不会被执行或写入配置（测试中对网络、子进程、`os.system` 打桩验证）。
* **预算**：模型调用、候选数、下载字节、单进程时长、墙钟时间在编排器中强制执行；恢复沿用累计用量；
  价格未知时报告 token 数，不写 0 元。默认后台运行和自动发布均关闭。

### 5.2 本地智能体

* **宿主权限**：Hermes 终端、文件、浏览器运行于当前用户权限；独立子进程与 `HERMES_HOME` 隔开配置和状态，
  不构成操作系统沙箱。研究流程的 `writable_paths` 不限制本地终端能修改哪些文件。
* **授权入口**：本机工具只对本地 CLI 启用；飞书事件继续进入 `ChatRouter`。
  Hermes 的危险命令检查、终端确认与澄清回调保留，非交互审批默认拒绝。
* **项目发布**：`project_command` 经过原权限与参数校验，模型的命令 schema 不含 `confirm`；
  `/apply`、`/rollback` 必须由用户亲自 `/confirm`。本地 shell 不构成不可绕过的发布权限隔离，
  智能体另有不得绕过确认的行为约束。
* **凭据**：上游 Key 由宿主从仓库外 `~/.config/omnilaw-evolution/env` 加载，文件权限要求 600。
  子进程环境不继承供应商凭据，只获得随机临时代理令牌。宿主文件系统仍可由本地工具访问，
  不应把环境隔离描述成凭据文件的 OS 访问控制。
* **费用**：经桥接的主循环、重试、压缩、审核及子智能体请求共享 DeepSeek 费用守卫。
  模型和最大输出由宿主配置固定。每日 $5 是本机配置，不是写死的全局默认值；
  shell 自行调用外部服务产生的消费不在此账本覆盖范围内。
* **恢复**：中断不会撤销已发生的文件写入或其他副作用。恢复时缺失的工具结果标为“未知”，
  先检查实际状态，不能把上下文恢复等同于事务回滚或工具恰好执行一次。

## 6. 已知局限

* 演示仍使用虚构小数据集（26 / 28 / 16 条）。正式评测已扩展为 266 / 268 / 256 条，加入固定版本的
  PAWS-X 中英文人工标注句对。百科句对与新闻流存在领域差异，跨语言配对属于派生任务，尚未经过额外人工复核；
  不能据此声称真实新闻流上的效果。来源和协议见 `eval/README.md`。
* 离线 MockLLM 用模式匹配“读”来源、用固定规则产出假设；后台 `executor.kind=template` 仍渲染模板代码。
  DeepSeek 本地智能体已完成真实调用验收，但这不代表后台研究已自动改用自由编程执行器。
  AnthropicLLM、ClaudeCodeExecutor 的实现与离线测试不等于已完成相应供应商的真实付费验收。
* 本地智能体复用 Hermes 核心并不等于完整复制 Hermes 产品：未接入语音、图片生成、全部聊天平台或桌面 UI；
  支持模型与运行环境以 Omnilaw 的适配代码为准，当前原生引擎仅接 DeepSeek、要求 Python 3.14。
* 模型输出先由宿主完整接收，再转换为 SDK 兼容 SSE；终端可显示工具进度，但没有上游逐 token 流式输出。
* DeepSeek 按配置的完整上下文容量与最大输出预占费用；接近每日上限时可能提前拒绝。
  网络结果不明、进程崩溃或缺少有效 usage 时保留保守金额，不能把账本直接当作供应商结算单。
* `sandbox-exec` 已被 Apple 标记为弃用，但在 macOS 14 上可用；Linux `bwrap` 路径未在本机测试。
* 释放/回滚管理的是本项目自己的 worktree 发布，不是容器；第三阶段如需容器，再接入 Docker。
