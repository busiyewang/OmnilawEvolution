# 真实能力接入（0.3.0）

2026-10-25。第二阶段结束时有四项待人决策：语义依赖、真实模型、真实飞书、后台服务。用户全部批准。
本文记录每项做了什么、实际验证到什么程度、还缺什么。

| 项 | 状态 |
|---|---|
| 句向量依赖与语义 / 跨语言去重 | ✅ 已实现，**真实模型已在沙箱内运行**；本轮候选未达采用门槛（见 §1.4） |
| 费用上限与真实模型适配 | ✅ 费用守卫已实现并测试；⏳ **等待 API 密钥与每日上限**，未发出任何付费请求 |
| 飞书 | ✅ 新增长连接模式（无需公网地址）；⏳ **等待应用凭据与 open_id**，未连接真实租户 |
| 后台服务 | ✅ **已安装为 launchd 用户服务并在运行** |

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
* `pyproject.toml` 新增可选依赖 `semantic`、`feishu`、`anthropic`；核心仍只用标准库，Python 3.9 可运行。
* 线程数固定为 4，同一输入的向量逐位相同（评测会检查确定性）；模型在模块导入时加载，不计入计时区间。

### 1.3 模板

在 URL 规范化和 shingle 两步之后加一步余弦相似度：同语言对用 `sem_threshold`，跨语言对用
`xlang_threshold`。开发集上看到同主题不同事件的条目（d13）相似度 0.80，跨语言重复（d19）0.797，
所以单一阈值分不开，两个阈值分开设。所有检查都在“记入已保留列表”之前完成，保证
`duplicate_of` 只指向确实保留的条目（有专门测试）。

### 1.4 实际结果（真实模型，sandbox-exec 内）

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

要让语义方法有机会被公平评估，需要人来决定下面其中一项（都会改变评测器版本，需要重建基线）：

1. 扩充开发集和保留集，加入复述和跨语言样例。标注应由人审核，避免开发者“按方法出题”。
2. 改采用规则，例如在 F1 门槛之外，允许“相对错误率下降 ≥ 50% 且无误删”。

## 2. 真实模型与费用上限

第二阶段的配置里有 `max_daily_cost_usd`，**但代码从未检查过它**。这次补上（`costs.py`）：

* 价格从配置读取（`model.price_per_mtok: {input, output}`）。没配价格就拒绝付费调用，绝不把未知成本当作 0。
  Claude Opus 5.5 的官方价格是每百万 token 输入 $4、输出 $20（2026-10 核对）。
* 每次调用前，先用免费的 token 计数接口算出精确的输入 token，再按整个 `max_tokens` 估算最坏成本；
  “今日已花 + 最坏成本”超过上限就不发请求。调用后按实际 usage 记账到 `budget_ledger.cost_usd`。
* 达到上限时任务顺延到明天，当天只通知一次；配置问题（无价格、上限为 0）直接失败并通知。
* 去掉了服务端拒答回退：回退模型的价格不是配置的价格，会让成本不可控。
* Claude Code 执行器加上 `--max-budget-usd`（取每候选上限和今日剩余额度中较小的值），
  并记录 CLI 报告的 `total_cost_usd`；没报告成本时按上限记账，不按 0 记。
* 守护进程启动检查：配了付费模型却没配价格或上限，直接拒绝启动。

用模拟客户端测试过：价格计算、上限拦截（被拦的请求一次都没发出去）、顺延、执行器记账。
**没有发出过真实付费请求**。

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

## 5. 当前正式实例

* 配置：仓库根目录 `omnilaw.json`（已 gitignore），状态在 `.omnilaw/`。
* `semantic.enabled: true`；`background: true`。
* 每日预算：20 个任务、50 次模型调用、30 MB 下载、12 个候选。
* 模型仍是离线模拟（`mock`），执行器是模板执行器，所以目前**零费用**。
* 定时任务：
  * 每天 08:00 在 batch2 上评测现行版本；
  * 每周一 09:00 复查知识；
  * 每周三 09:00 研究 “near-duplicate news detection”。
* 通知目前只写 outbox（`omnilaw-evolution outbox` 查看）；配好飞书后改 `notify.kind`。

## 6. 你需要做的

1. **模型**：执行 `omnilaw-evolution env`，在生成的文件里填 `ANTHROPIC_API_KEY`。在 `omnilaw.json` 里设置：
   * `"model": {"provider": "anthropic", "price_per_mtok": {"input": 4, "output": 20}}`
   * `"budget": {"max_daily_cost_usd": 你的上限}`
   * 如果也想让 Claude Code 写代码：`"executor": {"kind": "claude-code", "max_usd_per_candidate": …}`

   然后执行 `service install` 重启服务。
2. **飞书**：在开放平台创建企业自建应用，开启机器人能力，事件订阅方式选“使用长连接接收事件”，
   订阅 `im.message.receive_v1`，开通接收和回复消息的权限。完成后：
   * 在 env 文件里填 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`；
   * 把你的 open_id 写进 `chat.allowed_users` 和 `chat.admins`；
   * 执行 `service install --services daemon feishu`。

   要让后台通知推到群里，再配置群自定义机器人的 `FEISHU_WEBHOOK_URL`，并设置 `"notify": {"kind": "feishu_webhook"}`。
3. **评测集**：在 §1.4 的两个方案里选一个，或者维持现状。
