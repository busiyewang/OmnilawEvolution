# Omnilaw Evolution — 设计与选型

版本 0.1.0（第一阶段），2026-10-25。第二阶段（0.2.0）的设计见 [PHASE2.md](PHASE2.md)。

## 1. 目标机器（本次开发实际环境）

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

## 2. 参考项目核对（2026-10-25）

| 项目 | 固定版本 | 许可证 | 核对结论 | 本项目的处理 |
|---|---|---|---|---|
| [MOSS](https://github.com/hkgai-official/Moss) | `5453f1fe` (2026-05-23) | Apache-2.0 | README 确认：失败批次 → 改写自身 TypeScript 源码 → 临时 worker 重放 → **用户明确授权后**替换容器，90 s 健康探测失败则回滚 | 借鉴流程：候选与发布分离、READY≠APPLIED、健康检查门控的切换与回滚。未复用代码（TypeScript/OpenClaw/Docker 栈） |
| [Darwin Gödel Machine](https://github.com/jennyzzt/dgm) | `a565fd2d` (2025-08-13) | Apache-2.0 | 研究代码，候选档案 + 编程基准筛选 | 借鉴“候选档案 + 固定评测器筛选”的思路（`runs`/`evaluations` 表）；首版不做开放式档案搜索 |
| [RD-Agent](https://github.com/microsoft/RD-Agent) | `484776c2` (2026-09-23) | MIT | 读论文 → 实现 → 实验反馈，面向数据科学，官方以 Linux 为主 | 借鉴“研究→实现→反馈”的阶段划分；不引入依赖 |
| [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve) | `5153a7d7` (2026-10-25) | Apache-2.0 | 评测器驱动的代码变体生成与筛选 | 借鉴“多个变体 → 评测器选择”；本项目把变体数放进预算，且只在开发集上筛选 |
| [GEPA](https://github.com/gepa-ai/gepa) | `7af5f0c5` (2026-10-22) | MIT | 基于执行记录反思优化文本参数 | 作为第二阶段候选优化器的备选；首版未用 |
| [Hermes Self-Evolution](https://github.com/NousResearch/hermes-agent-self-evolution) | `0a929e3a` (2026-06-17) | 仓库未声明 | README 状态表：仅 Phase 1（SKILL.md 优化）已实现，工具描述/系统提示/工具代码/持续循环均为 Planned | 不作为依赖；聊天与 Hermes 适配留到第二阶段 |
| [Awesome Self-Evolving Coding Agents](https://github.com/iSEngLab/Awesome-Self-Evolving-Coding-Agents) | `7f5ba6d3` (2026-10-04) | 仓库未声明 | 索引 | 仅用于发现，不作为效果证据 |

论文核对：arXiv 2605.22794v2（MOSS）、2505.22954v3（DGM）、2608.02636v1（多轮技能进化反馈）题目与任务书一致。
2608.02636 的结论（有效改进并非每轮出现、验证集与测试集收益可能不同）直接体现在本项目的规则中：
允许“本轮未找到可采用版本”，开发集筛选与保留集采用分离，保留集曝光次数受限。

**复用 vs 自建结论**：上述项目都是完整框架或研究代码，栈（TypeScript/Docker/DSPy）与首版需求（单机、离线可跑、
可审计）不匹配。首版全部自建，约 3.1k 行标准库 Python（另有约 600 行测试）；从中借用的是流程与约束设计，没有复制代码。

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
  llm.py         模型适配器：MockLLM（离线、确定性）与 AnthropicLLM（官方 SDK，结构化输出 + 本地 schema 校验）
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
```

数据：SQLite（sources / knowledge / proposals / runs / events / evaluations / releases / failures；
第二阶段 schema v2 以纯新增方式加入 tasks / schedules / discoveries / feedback / budget_ledger / eval_cache /
outbox / chat_confirmations / chat_events，旧版本记录的发布仍可回滚），文件保存来源快照、
补丁、逐样例结果、日志；Git 保存版本（候选分支 `omnilaw/run-<id>-v<n>`）。

## 5. 隔离与安全边界

* **源码隔离**：候选在独立 git worktree 中生成，稀疏检出排除 `/eval/`，候选生成阶段看不到保留集。
* **执行隔离**：候选代码只在沙箱中运行：禁网，禁止读 `eval_dir` 与状态库，只能写本次评测的临时目录。红队测试
  （`redteam-exfil`）验证读保留集和联网都被拦截。
* **可写范围**：只有 `writable_paths`（默认仅 `collector/dedup.py`）。评测器、测试、配置、预算、发布控制在
  `protected_paths`，越界、删除、符号链接、ignored 文件都会被拒绝（红队 `redteam-scope` 验证）。
* **评测器版本**：评测代码 + 数据清单的哈希记录在每次运行中；改评测器属于独立变更，需要重建基线。
* **保留集**：每轮只对开发集选出的一个变体评一次；每日曝光次数上限；保留集失败样例不写入规划可读的 `failures` 表。
* **外部文本**：只作为数据保存并扫描注入特征；模型输出必须通过 schema 校验，引用必须能在原文中逐字定位；
  任何来源文本都不会被执行或写入配置（测试中对网络、子进程、`os.system` 打桩验证）。
* **预算**：模型调用、候选数、下载字节、单进程时长、墙钟时间在编排器中强制执行；恢复沿用累计用量；
  价格未知时报告 token 数，不写 0 元。默认后台运行和自动发布均关闭。

## 6. 已知局限

* 演示数据是虚构的、按机制设计的小数据集（开发 26 / 保留 28 / 后续批次 16 条），阈值 0.3–0.6 在开发集上效果相同，
  说明数据分离度高，不代表真实新闻流上的效果。
* 离线 MockLLM 用模式匹配“读”来源、用固定规则产出两个假设；模板执行器渲染代码。真实模型路径
  （AnthropicLLM、ClaudeCodeExecutor）已实现但**未实际运行**（无凭据，且未获付费授权）。
* `sandbox-exec` 已被 Apple 标记为弃用，但在 macOS 14 上可用；Linux `bwrap` 路径未在本机测试。
* 释放/回滚管理的是本项目自己的 worktree 发布，不是容器；第三阶段如需容器，再接入 Docker。
