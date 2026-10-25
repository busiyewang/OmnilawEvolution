# Omnilaw Evolution — 架构、设计模式与参考项目

版本 0.3.0，2026-10-25。本文回答三个问题：

1. 项目整体是怎么搭起来的；
2. 代码里用了哪些设计模式，为什么用；
3. 参考了哪些项目，怎么参考的，借了什么、没借什么。

环境实测和选型细节见 [DESIGN.md](DESIGN.md)；第二阶段机制见 [PHASE2.md](PHASE2.md)；真实能力接入见
[INTEGRATIONS.md](INTEGRATIONS.md)。

规模：核心约 5.8k 行 Python（标准库 + SQLite + Git），测试约 1.5k 行，97 个测试。

---

## 1. 整体架构

### 1.1 一句话

**一个程序在规则约束下改进自己的一个真实模块。**
它先读外部资料，形成带出处的知识，再据此提出假设，在隔离环境里生成候选代码，用自己改不了的评测器打分。
只有达到预先写定的条件，才允许人确认发布；发布后可以验证并回滚。

### 1.2 分层

```
┌──────────────────────── 入口层 ────────────────────────┐
│ cli.py   chat.py(ChatRouter)   adapters/feishu.py      │  人与渠道
│ journal.py  定期日报/周报、知识库、代码变更记录          │  对外记录
│ worker.py(Worker / daemon)   taskqueue.py(队列 + cron) │  后台与触发
└───────────────────────────┬────────────────────────────┘
                            │ 任务 / 命令
┌──────────────────────── 编排层 ────────────────────────┐
│ runner.py     运行状态机 + 检查点 + 预算 + 互斥锁        │
│ planner.py    知识 → 假设（提案），开放问题分类           │
│ budget.py / costs.py   单任务预算、每日账本、费用守卫     │
└──────┬──────────────┬──────────────┬──────────────┬────┘
       │              │              │              │
┌──── 研究 ───┐ ┌─── 生成 ────┐ ┌─── 评测 ───┐ ┌─── 发布 ────┐
│ discovery   │ │ executors   │ │ evaluator  │ │ versions    │
│ sources     │ │ templates   │ │ eval_harness│ │ (worktree + │
│ knowledge   │ │ llm         │ │ sandbox    │ │  active 链接)│
│ review      │ │ policy(范围)│ │ policy(采用)│ │ healthcheck │
└─────────────┘ └─────────────┘ └────────────┘ └─────────────┘
                            │
┌──────────────────────── 被进化对象 ─────────────────────┐
│ collector/pipeline.py → collector/dedup.py（唯一可写）  │
│ collector/semantic.py（受保护的句向量运行时）            │
└────────────────────────────────────────────────────────┘
┌──────────────────────── 存储 ──────────────────────────┐
│ SQLite（WAL）  文件（来源快照/补丁/逐样例结果）  Git      │
└────────────────────────────────────────────────────────┘
```

依赖方向只能**从上往下**。被进化的 `collector/` 不导入编排层的任何东西，所以候选代码运行时完全不知道
评测器、预算和状态库的存在。

### 1.3 一轮完整流程

```
主题 / 失败类别
   │ discovery.discover_incremental      已见记录、相关性打分、积压
   ▼
来源（网页 / GitHub@SHA / arXiv）        sources.Fetcher：超时、大小上限、缓存、注入扫描
   │ knowledge.extract_for_source        引用必须能在原文中逐字定位，否则丢弃
   ▼
知识（主张 / 步骤 / 条件 / 局限 / 位置 / 验证等级）
   │ planner.propose                     必须引用知识 id；相同提案不重建
   ▼
提案（目标模块、方法、变体、采用条件——此时还没有任何候选代码）
   │ runner.Runner.execute
   ├─ PATCHING    versions.create_candidate   独立 worktree，稀疏检出排除 eval/
   │              executors.write_candidate   模板 / Claude Code
   │              policy.check_scope          git 全量变更，越界即拒
   ├─ EVALUATING  evaluator.cached_run_on_split  沙箱内只给无标签数据
   │              开发集筛选全部变体 → 选一个 → 保留集只评一次
   │              policy.decide               预设规则，程序判定
   ▼
READY / REJECTED
   │ 人确认（CLI 或聊天 + 一次性确认码）
   ▼
APPLIED   versions.apply_sha：发布 worktree → 沙箱健康检查 → 原子切换 active
   │ 定时 evaluate_release 在新批次上复核 → 新失败写入 failures → 失败驱动研究
   ▼
ROLLED_BACK（需要时）：校验 SHA、依赖哈希、schema 兼容、健康检查
```

### 1.4 数据

| 存储 | 内容 | 原因 |
|---|---|---|
| SQLite（WAL） | 来源、知识、提案、运行、事件、评测、发布、失败、任务、日程、账本、缓存、outbox | 单文件、事务、并发读，标准库自带 |
| 文件 | 来源原文快照、补丁、逐样例结果、日志、报告 | 大对象和需要人读的产物 |
| Git | 基线、候选分支 `omnilaw/run-<id>-v<n>`、发布 worktree | 版本本来就该交给 Git；补丁、回滚、差异都是现成的 |

schema 只做**纯新增迁移**（`db.MIGRATIONS[v]["additive"]`），所以旧 schema 下记录的发布仍能回滚。

---

## 2. 设计模式

下面按用途分组。每个模式说明：它在哪里、解决什么问题、为什么不用更简单的写法。

### 2.1 可替换的实现：适配器 + 策略 + 工厂

| 接口 | 实现 | 工厂 |
|---|---|---|
| 模型 `extract_knowledge / plan` | `MockLLM`（离线、确定性）、`AnthropicLLM` | `llm.make_llm(cfg)` |
| 执行器 `write_candidate` | `TemplateExecutor`、`ClaudeCodeExecutor`、3 个红队执行器 | `executors.EXECUTORS` + `make_executor` |
| 来源提供者 `search` | `LiveProvider`（arXiv / GitHub）、`SnapshotProvider`（录制回放） | `discovery.make_provider` |
| 通知 `send` | `Notifier`（只写 outbox）、`StdoutNotifier`、`FeishuWebhookNotifier` | `notify.make_notifier` |
| 沙箱 | `sandbox-exec`、`bwrap`、`none` | `sandbox.detect_mode` |

**为什么用**：任务书要求“先离线可跑，再接真实服务”。同一个接口有离线实现和真实实现，演示和测试走离线
实现，结果完全可复现；接入真实服务只需要改配置，编排代码一行不动。0.3.0 接入 Claude、飞书长连接的时候，
runner、worker、planner 都没改。

**刻意的约束**：真实实现和模拟实现报告同样的字段。例如 `last_usage`：模拟实现给估算的 token 数，
并标 `estimated: True`；真实实现给实际 token 数和金额。报告不需要知道背后是哪个实现，也不会把估算当实测。

### 2.2 有限状态机 + 检查点（`runner.py`）

```
QUEUED → RESEARCHING → PLANNED → PATCHING → EVALUATING → READY | REJECTED
READY → APPLIED → ROLLED_BACK
任一执行状态 → FAILED | CANCELLED | BUDGET_EXHAUSTED（可恢复）
```

* 合法转移写在 `TRANSITIONS` 表里，`transition()` 拒绝表外的跳转，每次转移写一条 `events`。
* 每完成一个阶段写一次 `checkpoint`（JSON）。`resume` 从检查点继续，**预算按累计用量算**：
  恢复的运行从库里存的用量开始，不从 0 开始，否则“停止再恢复”就能绕开预算。
* `READY` 和 `APPLIED` 是两个状态：达到条件不等于已发布。这是从 MOSS 借来的关键区分。

**为什么不用一个大函数**：长任务会被停止、超时、预算耗尽、进程崩溃打断。没有显式状态和检查点，
就说不清“现在在哪一步”，恢复时也会把已经花掉的保留集曝光再算一遍。

### 2.3 守卫对象：规则写在代码里，不写在提示词里

| 守卫 | 位置 | 拦截什么 |
|---|---|---|
| 可写范围 | `policy.check_scope` | 候选改了 `writable_paths` 之外的文件，含删除、符号链接、ignored 文件 |
| 采用规则 | `policy.decide` | F1 增量、误删率、关键条目、延迟、回归测试 |
| 预算 | `budget.Budget.check` | 花费**之前**检查，超出就抛 `BudgetExhausted` |
| 每日账本 | `budget.DailyLedger` | 跨任务的每日上限，worker 领取任务前检查 |
| 费用 | `costs.SpendGuard.check` | 按最坏情况检查当日费用上限；未知价格直接拒绝 |
| 守护进程 | `worker.check_daemon_config` | 没开 `background`、没设每日预算、付费模型没配价格 |
| 聊天权限 | `chat.ChatRouter` | 读 / 写 / 管理三级权限，采用和回滚另需一次性确认码 |

共同原则：**模型可以提建议，但判定由程序做**。没有任何守卫依赖“在提示里告诉模型不要做某事”。
守卫都是先检查、再执行，拒绝时给出能操作的原因，比如“设置 model.price_per_mtok”。

### 2.4 纵深防御：源码隔离 + 执行隔离 + 范围检查

一层不够，所以有三层：

1. **源码隔离**：候选在独立 git worktree 里生成，稀疏检出排除 `/eval/`，生成阶段根本看不到保留集。
2. **执行隔离**：候选代码只在沙箱里跑，禁网，禁读评测目录和状态库，只能写本次评测的临时目录。
3. **事后检查**：`check_scope` 用 git 全量变更兜底，前两层漏掉的写入也会被发现。

每层都有**红队执行器**专门攻击：`redteam-scope` 篡改评测器、`redteam-exfil` 偷读保留集并尝试联网、
`redteam-slow` 写死循环。这些攻击写成测试，每次都会跑。

### 2.5 生成与评判分离（独立评测器）

* `eval_harness.py` 在沙箱里运行：只拿到**无标签**条目，调用候选的 `dedup()`，输出决策和耗时。
* `evaluator.score` 在评测进程里拿标签打分。候选永远拿不到标签，也就无法“对着答案改”。
* 评测器版本 = 评测代码 + 数据清单的哈希，记录在每次运行中。改评测器属于另一类变更，需要重建基线。
* 开发集和保留集分离：变体只在开发集上筛选，保留集每轮只评一次，每天的曝光次数有上限；
  保留集上的失败样例不写入规划器能读的 `failures` 表。

这是整个项目最重要的模式：**生成候选的一方不能影响打分的一方**。

### 2.6 蓝绿发布 + 原子切换（`versions.py`）

* 每个发布是 `state/releases/<sha12>` 下的一个 git worktree；`active` 是指向其中一个的符号链接。
* 切换方式：先建临时链接，再用 `os.replace` 原子替换（POSIX 保证原子性），不会出现“切了一半”的状态。
* 切换前，先在沙箱里对新发布跑健康检查（真实 pipeline + 冒烟数据）；不通过就不切。
* 回滚前校验目标版本的 SHA、依赖哈希、schema 兼容性，并重新做健康检查。

### 2.7 可靠的后台处理：队列 + 去重键 + 发件箱

| 模式 | 位置 | 作用 |
|---|---|---|
| 原子领取 | `taskqueue.claim`：`BEGIN IMMEDIATE` | 多个 worker 不会领到同一个任务 |
| 幂等键 | `tasks.dedupe_key` | 同一个键最多一个活动任务，例如 `research:<主题>`、`failure:<类别>` |
| 冷却期 | `Worker._recently_done` | 72 小时内研究过的主题不再研究；“新可实现方法”触发的重新规划用独立的键，防止循环 |
| 退避重试 | `retry_or_fail` | 临时失败按退避重试，超过次数才算失败 |
| 延期 | `defer` | 预算或费用用完时顺延到明天，不算失败 |
| 发件箱 | `notify.Notifier` → `outbox` 表 | 通知先落库再投递；投递失败不影响任务，`once_key` 防止刷屏 |
| 事件去重 | `chat_events(event_id)` | 飞书重试推送同一事件时只处理一次 |
| 过期恢复 | `recover_stale` | worker 崩溃后留下的 `running` 任务，在守护进程启动时恢复为排队 |
| 心跳 | `taskqueue.heartbeat` | `state/daemon/heartbeat.json`，方便确认后台还活着 |

### 2.8 内容寻址缓存（`evaluator.cached_run_on_split`）

缓存键 = collector 目录的**去掉文档字符串后的 AST 哈希** + 评测器版本 + 数据切分哈希。

* 用 AST 而不是文件字节：生成的模块会在文档字符串里写运行编号，按字节哈希永远命中不了；
  去掉文档字符串后，行为相同的代码哈希相同。
* 只缓存确定性结果；崩溃和超时总是重跑。
* 缓存命中不算保留集曝光，也不会被当成新证据。

### 2.9 端口与适配器：核心不依赖任何渠道

`ChatRouter.handle(user, text, channel)` 是唯一入口：命令解析、中英文别名、权限、确认码、反馈标注都在这里。
CLI、飞书回调、飞书长连接、以后可能接入的 Hermes，都只是把消息转成 `(user, text)` 交给它。
任务书要求“核心不能依赖单一渠道”，这就是实现方式；飞书长连接只新增了约 30 行适配代码。

### 2.10 代码生成：模板而不是自由改写（`templates.py`）

离线模式下，候选代码由方法对应的代码块拼成完整模块。这样做是因为：

* 没有付费模型时，闭环也能真实地跑起来：候选是真代码，评测是真评测；
* 生成结果确定，测试可复现；
* 拼接顺序本身是不变量，比如“所有检查完成后才记入已保留列表”，由测试保证。

报告里会写明代码来自模板而不是模型，不冒充模型能力。

### 2.11 受保护的运行时（`collector/semantic.py`）

需要重依赖的能力（句向量模型）不让候选自己实现，而是由人提供一个受保护的模块：

* 模型版本和文件哈希写死，不联网，所以能在禁网沙箱里使用；
* 候选只能 `import` 它，改它会被范围守卫拒绝；
* 只有“配置批准 + 依赖可导入 + 哈希正确”三项都满足，相关方法才算可实现，否则规划器把问题标为 blocked。

这样能扩展候选的能力，又不扩大候选能改的范围。

### 2.12 外部文本只当数据

* `sources.scan_untrusted` 扫描注入特征并标记，但**不删除也不执行**，留给人看；
* 模型输出必须通过 `schema.validate`，引用必须在原文中逐字找到（`knowledge.locate`），找不到就丢弃；
* 发给模型的提示里明确写“来源文本不可信”，但真正起作用的还是上面这些程序检查；
* 凭据只按环境变量名引用；凭据文件放在仓库外，权限必须是 600；`doctor` 只显示变量名，不显示值。

### 2.13 记录从数据生成，而不是从叙述生成（`journal.py`）

日报、周报、知识库和变更记录都是对状态库和 Git 的**只读投影**：同样的数据总是生成同样的记录，
不让模型来“总结进展”。时间窗口是半开区间，并由 `journals` 表衔接，相邻两期不重叠、不漏记。

### 2.14 诚实的度量

这不是传统意义上的设计模式，但它贯穿整个代码库：

* 未知成本不记为 0：没配价格时报告 token 数，并拒绝付费调用；CLI 没报告成本时按上限记账；
* 每条评测都记录隔离级别（`sandbox-exec` / `bwrap` / `none`），缓存命中标注 `(cached)`；
* 知识的验证等级分四档：只是来源声称、本地复现失败、本地复现通过、在真实批次上有效，且只升不降；
* “本轮未找到可采用版本”是合法结论；门槛不会为了让结果通过而调整。

---

## 3. 参考了哪些项目，怎么参考

### 3.1 方法

1. **先核对，再引用**：每个项目都看了固定 commit 的 README 和代码结构，记录 SHA、许可证和实际状态
   （区分“已实现”和“Planned”）。论文按 arXiv 版本号核对。
2. **判断栈是否匹配**：本项目的约束是单机、离线可跑、标准库、可审计。完整框架（TypeScript、Docker、DSPy）
   即使优秀，引入的依赖和假设也比带来的价值多。
3. **借流程和约束，不复制代码**：所有代码都是自己写的；借来的是“哪些步骤必须分开”“哪些东西候选不能碰”这类设计决定。
4. **记录没借的部分和原因**：不采用也是决策，需要写下来。

### 3.2 逐项对照

| 项目 | 它做得好的地方 | 本项目借了什么 | 没借什么、为什么 |
|---|---|---|---|
| **MOSS**（hkgai-official/Moss，`5453f1fe`，Apache-2.0） | 真实系统上的自我修改闭环：失败批次 → 改写自身源码 → 临时 worker 重放 → **用户明确授权**才替换 → 健康探测失败自动回滚 | `READY ≠ APPLIED`；健康检查门控的切换；可验证回滚；“失败批次驱动下一轮”（第二阶段的 `evaluate_release` → 失败驱动研究） | TypeScript / OpenClaw / Docker 栈；容器级替换（本项目用 worktree + 符号链接，单机更轻） |
| **Darwin Gödel Machine**（jennyzzt/dgm，`a565fd2d`，Apache-2.0） | 候选档案 + 固定基准筛选，证明“自改代码”可以由评测驱动 | 候选和评测结果都入库（`runs` / `evaluations`），评测器固定、候选不可改 | 开放式档案搜索与大规模基准：成本高，单机日报场景用不上 |
| **RD-Agent**（microsoft/RD-Agent，`484776c2`，MIT） | 研究 → 实现 → 实验反馈的阶段划分清楚 | 研究、知识、规划、实现、评测拆成独立模块和状态 | 面向数据科学竞赛，依赖重、以 Linux 为主 |
| **OpenEvolve**（`5153a7d7`，Apache-2.0） | 评测器驱动的多变体生成与筛选 | 一个提案带多个变体，由评测器选择 | 变体数量放进预算；筛选只在开发集上做，保留集只评选中的那一个（OpenEvolve 本身不强调这一点） |
| **GEPA**（gepa-ai/gepa，`7af5f0c5`，MIT） | 从执行记录反思，优化文本参数 | 思路：失败样例（`failures`）反馈给规划器 | 暂未引入，作为以后的候选优化器备选 |
| **Hermes Self-Evolution**（NousResearch，`0a929e3a`，未声明许可证） | 技能文件（SKILL.md）的自我优化 | 核对后确认只有 Phase 1 已实现，其余是 Planned | 不作为依赖；许可证未声明；本机没装 Hermes，通过 CLI 接入的方式写在文档里 |
| **Awesome Self-Evolving Coding Agents** | 索引 | 用来发现项目 | 只用于发现，不作为效果证据 |
| 论文 arXiv 2608.02636v1（多轮技能进化反馈） | 实证：有效改进不是每轮都有；验证集收益和测试集收益可能不一致 | 允许“本轮无可采用版本”；开发集与保留集分离；保留集曝光次数受限 | — |

### 3.3 演示任务里实际学习的资料（系统自己读的）

| 来源 | 系统学到的方法 | 处理 |
|---|---|---|
| Wikipedia “URI normalization”（oldid 固定） | URL 规范化 | 采用 |
| ekzhu/datasketch `minhash.py`（SHA 固定，MIT） | MinHash / LSH | 记录，但判定当前规模不适用，未用 |
| Lee et al. 2022，arXiv:2107.06499v2 | n-gram Jaccard 近重复 | 采用并本地化：中文没有空格分词，改用字符 4-gram 精确 Jaccard；这一差异记在知识的 limitations 里 |
| arXiv 复述识别、LaBSE 等论文（失败驱动研究，摘要级） | 语义 / 跨语言句向量 | 0.3.0 批准依赖后已实现；本轮未达采用门槛 |

### 3.4 0.3.0 选用的第三方组件

| 组件 | 优点 | 为什么选它 |
|---|---|---|
| `onnxruntime` + `tokenizers` | 依赖小、导入快、CPU 推理、线程数固定时结果确定 | 对比 PyTorch：体积和导入时间都小一个数量级，能满足每条 50 ms 的门槛 |
| `paraphrase-multilingual-MiniLM-L12-v2` | 专为复述训练，覆盖 50+ 语言（含中英），Apache-2.0 | 对比 LaBSE：1.9 GB，太重 |
| 官方 `anthropic` SDK | 结构化输出、免费的 token 计数接口 | token 计数让费用守卫可以在调用**前**算出精确的输入成本 |
| 官方 `lark-oapi` SDK | 长连接接收事件 | 不需要公网地址和反向代理，个人电脑也能用 |

---

## 4. 和参考项目相比，本项目的取舍

| 维度 | 本项目的选择 | 代价 |
|---|---|---|
| 依赖 | 核心只用标准库 | 一些轮子要自己写（cron 解析、队列、沙箱封装） |
| 隔离 | worktree + 系统沙箱，不用 Docker | `sandbox-exec` 已被 Apple 标记弃用；Linux `bwrap` 路径没在本机测过 |
| 进化范围 | 只允许改一个文件 | 能力增长慢，但每一步都能审计、能回滚 |
| 采用 | 默认必须人工确认 | 不全自动；自动采用留到第三阶段，而且默认关闭 |
| 评测 | 小规模虚构数据，规则预先写定 | 数据覆盖不足时，好方法也可能过不了门槛（0.3.0 语义方法就是这样），需要人来扩充数据 |

---

## 5. 扩展点

| 想做的事 | 改哪里 | 不需要改的 |
|---|---|---|
| 接入新模型厂商 | 新增一个实现 `extract_knowledge / plan` 的适配器，在 `make_llm` 里登记 | runner、planner、worker |
| 新的编程执行器 | 实现 `write_candidate`，登记到 `EXECUTORS` | 范围守卫和评测会自动生效 |
| 新聊天渠道 | 把消息转成 `ChatRouter.handle(user, text)` | 权限、确认码、命令 |
| 新的进化目标模块 | 配置 `target_module` / `writable_paths`，准备评测数据和契约测试 | 状态机、发布、回滚 |
| 新的去重方法 | 在 `templates.py` 加代码块，在 `knowledge.METHOD_CATALOG` 加条目 | 评测器 |
| 新的来源类型 | 扩展 `sources.Fetcher` / `discovery` 的提供者 | 知识与规划 |
