# 第二阶段：持续研究与反馈

版本 0.2.0，2026-10-25。（之后的真实能力接入见 `INTEGRATIONS.md`：语义方法已不再受阻，飞书新增长连接模式，后台服务已安装。）

本阶段在第一阶段单轮闭环之上增加：任务队列、四种触发源、增量来源发现、
知识复查、重复研究抑制、评测缓存、每日预算账本、聊天命令与飞书适配、通知、以及默认关闭的后台守护进程。

## 1. 任务与触发

任务类型（`tasks` 表）：`research`、`failure_research`、`knowledge_review`、`propose`、`run`、`evaluate_release`。

| 触发源 | 入口 | 产生的任务 |
|---|---|---|
| 用户 | `queue add`、`chat "研究 <主题>"`、飞书消息 | research / run（恢复）等 |
| 定时 | `schedule add --cron "0 8 * * *"`，`schedule tick` 或守护进程 | 任意类型，如每日 `evaluate_release`、每周 `knowledge_review` |
| 失败记录 | 评测或规划后发现未解决的失败类别 | `failure_research`（主题来自 `failure_topics`） |
| 任务链 | research 有新知识 → propose → 每个新提案一个影子 `run` | propose / run |

worker 每次领取一个任务（`BEGIN IMMEDIATE` 原子领取，按优先级），失败按退避重试至 `max_attempts`，
可取消（运行中的 run 任务会把停止请求传给对应运行，在下一个检查点结束）。worker 异常退出留下的
`running` 任务在守护进程启动时恢复为排队。

**影子运行会自动排队，采用永远不会自动发生**：READY 只发通知，采用必须由管理员在聊天或 CLI 中确认。

## 2. 失败如何变成研究

`planner.open_problems` 把开放失败（只来自开发集、后续批次和用户标注；保留集失败从不入库）分成：

* `needs_research`：没有知识覆盖 → 排队 `failure_research`；
* `blocked`：已有覆盖知识，但没有任何方法有代码模板，当前执行器无法实现 → 发一次通知，请人决策；
* `untriaged`：没有映射的失败类别（例如用户报告）→ 在“问题”命令中列出，等人工分类。

演示中的真实结果：batch2 上的 `paraphrase`（2 例）与开发集上的 `cross-language`（1 例）触发研究，
从 arXiv 检索到复述识别、LaBSE 等语言无关句向量的论文（**摘要级**），抽取出
`paraphrase_classifier`、`semantic_embedding`、`cross_lingual_embedding` 三类方法。它们都需要预训练模型和
ML 运行时：模板执行器做不了，引入依赖要改受保护的 `pyproject.toml`。系统因此把这两类问题标为 **blocked**
并通知，而不是伪造一个候选。要继续推进，需要你批准依赖和真实模型执行器（见第 8 节）。

## 3. 重复研究抑制（三层）

1. **活动任务去重**：同一 `dedupe_key`（如 `research:<规范化主题>`、`failure:<类别>`、`schedule:<名称>`）
   最多一个排队或运行中的任务。
2. **冷却期**：同一主题或失败类别在 `research.cooldown_hours`（默认 72 h）内完成过研究，再次请求记为
   `skipped` 并指向上次任务；`blocked` 类别不再重复研究，通知每天最多一次。
3. **实验去重**：规划器不会重建方法和变体完全相同的提案（第一阶段已有）。

## 4. 增量来源发现

`discoveries` 表记住每个检索到的候选（arXiv 按去版本号的 id，GitHub 按仓库）。再次检索时，见过的条目
只更新 `times_seen`，不重新下载；未达单次入库上限的相关条目留待下次（积压）。下载前先做相关性打分：
主题词覆盖率（带粗略 6 字符前缀词干）+ 正向词 − 负向词。**这是透明的关键词启发式，不是模型判断**，
误判明显（实测曾把钓鱼检测仓库打满分）。用户配置的种子 URL 免于相关性过滤，但其内容仍按不可信数据处理。

离线回放用 `fixtures/discovery/manifest.json`：2026-10-25 从 arXiv API 实录的两个主题检索结果，只存摘要。

## 5. 知识复查

`review` / `knowledge_review` 任务按 `review_after_days` 选出到期来源（每个来源链只看最新版本，
对抗性测试页面不复查）：

* Wikipedia：比较 `oldid` 与最新修订号；
* GitHub：取分支上该路径的最新提交，按内容哈希比较（分支前进但文件不变 → unchanged）；
* arXiv：比较版本号；同版本但从摘要/摘录升级到全文也记为 changed；
* 其他网页：绕过缓存重新抓取，比较哈希。

有变化时新版本作为新来源保存（`supersedes` 指向旧来源），旧知识标为 `source_changed`，新旧知识都保留，
不互相覆盖。真实联网复查结果见 `reports/demo2/DEMO2_REPORT.md` §10。

## 6. 评测缓存

键 = collector 目录（候选可导入的全部代码）去掉文档字符串后的 AST 哈希 + 评测器版本 + 数据切分哈希。
只缓存确定性结果（打分结果与契约失败），崩溃和超时总是重跑。缓存命中在评测记录里标注 `(cached)`，
**不计入保留集每日曝光次数**。演示中同一提案重跑时 6 次评测全部命中缓存。

## 7. 预算

* 单任务预算 = `budget`（与第一阶段相同），再被当天剩余额度封顶；
* 每日账本 `budget_ledger` 记录任务数、模型调用、token、下载字节、候选数、沙箱秒数；
* 任一每日上限用完后 worker 停止领取，当天只通知一次，任务顺延；
* 守护进程只有在 `"background": true` **且** `daily_budget` 设置了 `max_tasks`、`max_model_calls`、
  `max_download_bytes` 时才会启动。

## 8. 聊天与通知

所有渠道调用同一个 `ChatRouter.handle(user, text)`：

| 权限 | 命令 |
|---|---|
| `allowed_users` | 状态、队列、运行、报告、版本、预算、问题、研究、反馈、重复、保留 |
| `admins` | 停止、取消、恢复 |
| `admins` + 一次性确认码（默认 10 分钟、绑定用户、单次有效） | 采用、回滚 |

用户标注（“重复 b02 b01”、“保留 b10”）写入 `failures`（split=`user`）供规划使用，并追加到
`state/feedback/cases.jsonl`。并入评测集需要人工审核：改评测数据属于评测器版本变更，需要重建基线。

通知先写 `outbox` 表（审计），再按 `notify.kind` 投递：`mock`（只写表）、`stdout`、`feishu_webhook`
（自定义机器人，支持签名）。投递失败只记录，不影响 worker。

### 飞书接入（未在真实租户验证）

1. 在飞书开放平台创建企业自建应用，开启机器人，订阅事件 `im.message.receive_v1`，权限至少包含接收与回复消息；
   **不要开启 Encrypt Key**（本实现只用标准库，不支持 AES 解密）。
2. 环境变量：`FEISHU_VERIFICATION_TOKEN`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`；如需主动推送，
   再建一个群自定义机器人并设置 `FEISHU_WEBHOOK_URL`（可选 `FEISHU_WEBHOOK_SECRET`），配置 `"notify": {"kind": "feishu_webhook"}`。
3. 在配置中写入允许的 open_id：`"chat": {"allowed_users": [...], "admins": [...]}`。
4. `omnilaw-evolution serve-feishu --host 127.0.0.1 --port 8787`，用 HTTPS 反向代理把
   `/feishu/events` 暴露给飞书。处理在返回 200 之后异步执行（飞书要求 3 秒内响应），按 `event_id` 去重重试。

### Hermes

目标机器上没有安装 Hermes，所以没有写专用适配器。已有 Hermes 的机器可以直接让 Hermes 调用
`omnilaw-evolution chat --user <id> "<命令>"`（与飞书同一套权限逻辑），或用 Hermes 的定时任务执行
`omnilaw-evolution schedule tick && omnilaw-evolution worker --max-tasks 5`。

## 9. 后台运行

示例在 `docs/deploy/`（launchd、systemd、crontab），**均未安装**。启动前请在配置中显式设置
`background: true` 和每日预算；`daemon` 每 `queue.poll_seconds` 秒执行一次“调度 tick + 消化队列”，
写心跳到 `state/daemon/heartbeat.json`，收到 SIGTERM 后在当前任务结束时退出。

## 10. 实现、运行、假设

| 项 | 状态 |
|---|---|
| 队列、cron、worker、守护进程门控、预算账本 | 已实现，测试与演示中运行 |
| 增量发现（快照回放） | 已运行；**真实联网**增量检索已在演示 §10 运行 |
| 知识复查 | 变化检测逻辑用模拟响应测试；**真实联网**复查 3 个来源已运行 |
| 评测缓存、三层抑制、失败驱动研究、blocked 判定 | 已运行 |
| 聊天路由、权限、确认码、反馈 | 已运行（模拟飞书事件送入真实适配器） |
| 飞书回调服务（本机 HTTP） | 已在本机端口测试；**未连接真实飞书租户** |
| 飞书 webhook 推送（签名） | 对本机模拟服务器测试；**未向真实飞书发送** |
| 语义 / 跨语言去重 | **未实现**：需要依赖批准与模型执行器 |
| 后台常驻 | 代码完成；**未安装为系统服务** |
