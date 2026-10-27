# 真实能力接入（0.3.0）

更新于 2026-10-26，对应 Hermes 集成提交 `2bb576b`。
本文记录接入状态、部署方式与验收范围；早期语义实验保留原日期和结论，不作为当前运行配置。
核心设计见 [DESIGN.md](DESIGN.md)，进程、工具循环与存储见 [ARCHITECTURE.md](ARCHITECTURE.md)。

| 项 | 状态 |
|---|---|
| 句向量依赖与语义 / 跨语言去重 | 已实现，历史真实模型实验见 §1.4；当前本机 `semantic.enabled=false` |
| DeepSeek 与费用上限 | 已配置并完成真实调用验收；本机每日上限 $5；Key 仅在仓库外保存 |
| Hermes 本地智能体 | 已嵌入实际 `AIAgent` 与工具；连续修复、会话恢复、记忆、网页和浏览器见 §8 |
| 飞书 | ✅ 新增长连接模式（无需公网地址）；⏳ **等待应用凭据与 open_id**，未连接真实租户 |
| 后台服务 | 已安装 launchd 用户服务，集成后已重启；实时状态用 `service status` 核实 |

## 1. 语义 / 跨语言去重

### 1.1 选型

| 选项 | 结论 |
|---|---|
| sentence-transformers + PyTorch | 未选：PyTorch 安装体积大，导入要几秒 |
| **onnxruntime + tokenizers + numpy** | **选用**：依赖约 40 MB，导入快，CPU 推理 |
| 模型 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 50+ 语言（含中英），专为复述训练，Apache-2.0，ONNX 文件 470 MB |
| LaBSE | 未选：1.9 GB，对每条 50 ms 的延迟门槛太重 |

模型固定在 revision `e8f8c211…`，四个文件都有 sha256 校验（写在 `collector/semantic.py`）。
`omnilaw-evolution models fetch` 下载并校验，`models status` 重新校验哈希。

### 1.2 隔离与权限

* `collector/semantic.py` 是**受保护文件**（加入 `protected_paths`），候选只能 `import` 它，不能改它。
  它从不联网，所以能在禁网沙箱中运行；模型目录通过 `OMNILAW_MODEL_DIR`（非机密路径）传入沙箱。
* 语义方法只有在三项同时满足时才算“可实现”：配置 `semantic.enabled: true`（人工批准）、依赖可导入、
  模型文件哈希正确。否则这两类失败仍被标为 blocked，与第二阶段行为一致。
* `pyproject.toml` 提供可选依赖 `semantic`、`feishu`、`anthropic`；研究核心仍只用标准库，Python 3.9 可运行。
  当前另有 `agent` 依赖组，本地 Hermes 引擎要求 Python 3.14。
* 线程数固定为 4，同一输入的向量逐位相同（评测会检查确定性）；模型在模块导入时加载，不计入计时区间。

### 1.3 模板

在 URL 规范化和 shingle 两步之后加一步余弦相似度：同语言对用 `sem_threshold`，跨语言对用
`xlang_threshold`。开发集上看到同主题不同事件的条目（d13）相似度 0.80，跨语言重复（d19）0.797，
所以单一阈值分不开，两个阈值分开设。所有检查都在“记入已保留列表”之前完成，保证
`duplicate_of` 只指向确实保留的条目（有专门测试）。

### 1.4 实际结果（真实模型，sandbox-exec 内）

以下为 **2026-10-25.1 虚构演示集与旧采用规则**的历史结果，保留原结论。
新版正式评测及高分基线规则见 [`../eval/README.md`](../eval/README.md)。

完整链路自动跑通：batch2 定时评测 → 复述 / 跨语言失败 → 失败驱动研究 → 方法“现已可实现且尚未试验”
→ 自动重新规划 → 影子运行。

| 运行 | 方法 | 开发集 F1 | 保留集 F1（基线 0.9565） | 结论 |
|---|---|---|---|---|
| 提案 3 | URL + shingle + 同语言语义 | — | 0.9565（+0.000） | REJECTED |
| 提案 4 | URL + shingle + 同语言语义 + 跨语言 | 1.000（v0、v1） | **1.000（+0.043）** | **REJECTED：未达 +0.05** |

延迟约 11 ms/条（门槛 50 ms），无误删，无关键条目丢失。

**没有被采用的原因要说清楚**：保留集 28 条里只有 1 条跨语言、0 条复述，基线已是 0.9565，
就算全部做对，F1 最多也只能提高 0.043，达不到预设的 +0.05。这是评测集覆盖不足，不是方法无效；
但规则是预先定的，**没有为了让它通过而改门槛或改数据**。

只读参考（batch2 不参与选择，不影响决策）：现行版本 F1 0.857，提案 4 的 v0 为 0.933。
2 条复述识别出 1 条（b15，相似度 0.92），b02 仍漏判；无误删。

后续已按用户要求扩充正式评测：加入 720 条公开 PAWS-X 句子，并增加“剩余 F1 差距减少 ≥ 50%
且零误删”的采用路径。这里的差距指 `1 − F1`，不是分类错误率。评测版本改变后必须重新测量基线；
旧候选不会因新规则被追认通过。真实新闻流标注仍需另外收集、审核。

## 2. 真实模型与费用上限

`costs.py` 根据 `model.price_per_mtok: {input, output}` 与 `budget.max_daily_cost_usd` 检查费用。
缺少价格或每日上限为 0 时拒绝付费调用；价格是部署配置，变更模型时必须同步核对。

| 路径 | 调用前 | 调用后 / 验证范围 |
|---|---|---|
| DeepSeek 研究、基础聊天 | 按完整 `context_tokens` 与 `max_tokens` 估算，用 SQLite 事务原子预占 | 有效 usage 按配置价格结算；已完成真实 DeepSeek 调用 |
| Hermes 模型请求 | 统一进入本机 `AgentBridge`，检查每日调用次数，再走同一 DeepSeek 预占路径 | 主循环、重试、审核、压缩与子智能体共用账本；桥接行为有自动化测试 |
| Anthropic 研究适配器 | 官方 SDK 计数输入 token，再按最大输出检查费用 | 返回 usage 后记账；保留已有实现，不声称完成真实付费验收或具备 DeepSeek 的原子预占 |
| Claude Code 研究执行器 | `--max-budget-usd` 取每候选上限与今日剩余中较小值 | 记录 `total_cost_usd`，缺少报告则按上限计；与本地 Hermes 编程是不同执行路径 |

DeepSeek 网络结果不明、5xx 或缺失有效 usage 时保留保守预占；4xx 释放本次预占。
适配器不自行重试，Hermes 发起的重试仍须重新经过守卫。费用账本用于本地限额，不能替代供应商账单。
达到每日上限时，后台任务按既有策略顺延；当前聊天返回额度错误，不代表已预约明天自动继续。
守护进程启动仍检查价格、每日上限和 `background` 配置。

Key 只在宿主读取，worker 只拿到临时代理令牌。模型、最大输出和关闭 thinking 的策略由宿主固定。
桥接只监听 `127.0.0.1`，上游响应先完整接收再生成 SDK 兼容 SSE。
本地 shell 自行访问外部付费服务的消费不在上述预算控制之内。

## 3. 飞书

新增 `serve-feishu --mode ws`：通过官方 `lark-oapi` SDK 的长连接接收事件。

* 不需要公网 HTTPS 地址和反向代理，飞书后台的“加密策略”也不用关。连接本身用应用凭据认证。
* 事件转成与回调模式相同的结构，交给同一个 `FeishuEventHandler`：同样按 event_id 去重，
  同样经过 `ChatRouter` 的权限和确认码。
* 测试把录制格式的事件交给 SDK 自己的分发器（`do_without_validation`，长连接客户端每帧调用的入口），
  验证能送到路由器并且重复投递会去重。**没有连接真实飞书租户。**

## 4. 凭据与后台服务

* 凭据文件 `~/.config/omnilaw-evolution/env`：在仓库之外，权限 600（权限更宽时拒绝加载），
  由 `omnilaw-evolution env` 生成空模板。CLI 和服务启动时只读取尚未设置的变量。
  **不要把密钥贴到聊天里。**
* `omnilaw-evolution doctor`：列出哪些能力可用、还缺什么，只显示变量名，不显示值。
* `omnilaw-evolution service install|uninstall|status`：写入
  `~/Library/LaunchAgents/com.omnilaw.evolution.plist` 并用 `launchctl bootstrap` 启动；
  `--services daemon feishu` 会同时安装飞书长连接服务。安装前会做和守护进程相同的检查。
  服务用低优先级运行（Nice 10，后台 IO）；崩溃后重启，启动间隔至少 300 秒；收到 SIGTERM 后正常退出，不再重启。

## 5. 本机实例配置快照（2026-10-26）

* 配置：仓库根目录 `omnilaw.json`（已 gitignore），状态在 `.omnilaw/`。
* `semantic.enabled=false`、`background=true`、`auto_apply=false`。
* 每日预算：50 个任务、100 次模型调用、50,000,000 字节研究下载、20 个候选，费用上限 $5。
* 模型 `deepseek-flash`，最大输出 4,096 token、配置上下文容量 1,048,576 token、HTTP 超时 120 秒。
  当前配置价格为每百万 token 输入 $0.3、输出 $1.2；这里记录配置值，不作为实时价格声明。
* 本地 `chat.engine` 使用默认 `hermes`，最多 30 轮 / 900 秒；本机虚拟环境 Python 3.14.4。
* 后台仍是 `executor.kind=template`；本地智能体可使用文件和终端工具编程，不会因此替换后台候选执行器。
* 日程、队列与通知是实例数据，分别用 `schedule list`、`queue list`、`outbox` 查看。
  研究下载、候选等预算不应解释为本地 shell 或浏览器的全局资源限额。

以上不是新安装的默认值。新安装仍使用 mock，后台关闭，每日付费额度为 0；应显式配置再启动。

## 6. 定期记录：研究知识与代码更新

`journal.py` 定期把“学到了什么、代码改了什么”写成 Markdown，默认放在 `<state_dir>/journal/`
（可用 `journal.dir` 改到别处，比如仓库里的 `reports/journal`）：

| 文件 | 内容 |
|---|---|
| `daily/<日期>.md`、`weekly/<年>-W<周>.md` | 本期新读的来源（含注入标记）、新知识（主张、做法、条件、局限、原文摘录、位置、验证状态、抽取方式）、验证状态变化、来源复查；结束的运行（方法、结果、保留集 F1、原因）和达标候选的补丁；发布与回滚的代码 diff；新失败与开放问题；用量与费用 |
| `KNOWLEDGE_BASE.md` | 累计知识库，按方法分组，每次刷新 |
| `CHANGELOG.md` | 被进化模块的全部发布记录，含依据的知识、假设和 diff 统计 |

* 每期覆盖“上一期结束 → 现在”的半开区间，日报和周报各自衔接，不重不漏；记录写入 `journals` 表（schema v3，纯新增迁移）。
* 内容直接取自状态库和 Git，**不经模型总结**，所以不会编造结果；离线模式匹配抽取的知识和估算的 token 数都会注明。
* 本机路径替换为 `<repo>` / `~`，文件可以直接分享。
* 触发方式：
  * 定时任务 `journal`（例如每天 21:00 日报、每周日 20:00 周报），生成后发通知（摘要 + 路径）；
  * 命令行 `omnilaw-evolution journal`；
  * 聊天发送“日志”或“日志 周报”，只预览摘要，不写文件。

## 7. 安装、启动与升级

### 7.1 本地智能体

在源码 checkout 中使用 Python 3.14 创建虚拟环境并安装可选依赖；已有可用 `.venv` 时直接安装即可：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -e '.[agent]'
.venv/bin/omnilaw-evolution env
```

在仓库外生成的 env 文件中填写 `DEEPSEEK_API_KEY`，保持权限 600。
首次部署时将 `omnilaw.example.json` 复制为 `omnilaw.json`，已有配置时不要覆盖；
按 [README 的 DeepSeek 配置](../README.md#deepseek-接入) 设置 provider、模型、价格与授权额度。
本机已完成这些配置，无需重复填写 Key。

```bash
.venv/bin/omnilaw-evolution doctor
.venv/bin/omnilaw-evolution chat
```

`/tools` 查看实际加载工具；`/new` 新建会话；`/history` 查看记录；
`chat --session <名称>` 恢复指定会话；`Ctrl-C` 中断当前任务，`/exit` 或 Ctrl-D 退出。
已有文件修改不会因中断自动撤销。浏览器需系统 Chrome / Chromium，或配置 `chat.browser_executable`。

升级源码或依赖后退出旧控制台，再重新运行 `chat`。后台服务需要另行重启：

```bash
.venv/bin/omnilaw-evolution --config omnilaw.json service install --services daemon
.venv/bin/omnilaw-evolution service status
```

服务安装要求已启用后台且配置有效预算。模型代理是控制台期间的临时回环端口，项目没有通过该端口提供管理网页。
运行不依赖本机原有 Hermes 目录；源码固定版本、许可证与校验和见
[`_vendor/README.md`](../src/omnilaw_evolution/_vendor/README.md)。

### 7.2 可选接入

* **飞书**：准备应用凭据与 open_id，启用机器人、长连接事件订阅及接收回复权限。
  env 文件填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`，配置 `chat.allowed_users` / `chat.admins`，
  再安装 `.[feishu]` 并执行 `service install --services daemon feishu`。事件仍只进入项目命令路由。
  群通知另配 `FEISHU_WEBHOOK_URL` 和 `notify.kind=feishu_webhook`。
* **语义方法**：安装 `.[semantic]`、运行 `models fetch` 校验固定模型，确认启用后设置 `semantic.enabled=true`。
  历史模型实验结果不代表每次安装都已开启该能力。
* **Anthropic / Claude Code**：仍可按 README 配置研究适配器和候选执行器，不能直接替换当前仅接 DeepSeek 的 Hermes 桥接。
* **正式评测**：已扩展为 790 条代理评测样本；真实新闻流的人工标注与领域验证仍是后续工作。

## 8. 验收记录与限制（2026-10-26）

| 验收层次 | 已验证内容 | 解释 |
|---|---|---|
| 全量自动化 | 157 项测试：152 通过、5 跳过；最终生命周期调整后另跑桥接与原生循环 14 项通过 | 为本次代码集成的验收记录，日志留在本地 `.omnilaw/acceptance/`，不随 Git 分发 |
| 实际 Hermes 循环 + 脚本模型响应 | 写文件、执行真实失败命令、读取、修补、复跑成功；项目命令、记忆、会话恢复；中断真实长命令后继续聊天 | 模型响应受测试控制，工具和上游循环真实运行，不计为真实模型智能水平评测 |
| 真实 DeepSeek + 本机工具 | 查询当前 PID 与监听端口；修复出错 Python 文件并重跑，得到 `OMNILAW_REPAIR_OK 4.0`、退出码 0 | 验证连续工具执行和纠错，未据此声称所有编程任务都能完成 |
| 真实网页与浏览器 | DDGS 搜索、`example.com` 正文提取；无头 Chrome 访问并读取标题与正文 | 验证已配置后端，不代表所有网站或登录操作均可用 |
| 费用与权限测试 | 回环令牌、零额度与调用次数拦截、宿主模型覆盖、凭据环境隔离、远端身份不启用本机工具、发布确认路径 | 校验守卫实现，不把本机 shell 视为强隔离环境 |
| 持久化与退出测试 | 主会话检查点、辅助请求不覆盖上下文、缺失工具结果修复、SSE 兼容、退出等待费用结算 | 恢复上下文不等于回滚文件或保证工具只执行一次 |

本机 `/tools` 核对为 21 个工具，涵盖终端、文件、网页、浏览器、任务、澄清、记忆、技能、历史与研究命令。
其中未逐一完成真实业务验收的工具，不因“已加载”就宣称全部验证通过。
完整 Hermes 产品的语音、图片生成、各聊天平台与桌面 UI 未纳入本次集成；
模型输出是缓冲后转 SSE，后台研究候选仍遵循原来的独立评测和人工采用流程。
