# Omnilaw Evolution —— 第二阶段演示报告

生成时间 2026-10-25 10:39:53 -0700，命令 `omnilaw-evolution demo2`。
模型适配器：**离线模拟**（未调用任何模型 API）；编程执行器：**模板**；来源发现：**录制的 arXiv 检索快照**（`fixtures/discovery/`，末尾附真实联网的增量检索与知识复查）。聊天消息以飞书 `im.message.receive_v1` 事件格式送入真实的飞书适配器，回复被截获而**没有发往真实飞书**。数据为虚构的小规模标注集，结果只说明机制。

- 演示实例基线提交 `4dcf90ebf5a2`；状态库 schema v2

## 1. 定时任务

- `daily-batch-check`：`0 8 * * *` → evaluate_release
- `weekly-review`：`0 9 * * 1` → knowledge_review

## 2. 用户通过聊天指定研究方向

> **ou_reader**：研究 news deduplication
>
> 已加入研究任务 task 1：news deduplication

**worker：研究 → 规划 → 影子运行**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| 1 | research | done | 发现 4，新 4，已见 0，不相关 0，入库 4，新知识 3 |
| 2 | propose | done | 新提案 [1, 2]，已测试跳过 []，影子运行任务 [3, 4]，失败研究任务 [] |
| 3 | run | done | 运行 1 → READY（缓存命中 0） |
| 4 | run | done | 运行 2 → REJECTED（缓存命中 1） |

通知（模拟通道，写入 outbox）：

- 运行 1 达到采用条件（f1 delta +0.803 >= +0.050）。回复“采用 1”并确认后生效；默认不自动采用。
- 运行 2 结束：REJECTED。no variant passed dev screening; v0 {'threshold': 0.3}: f1=0.800 false_deletion_rate=0.214 critical_lost=['d10', 'd13', 'd22']

- 来源 #4 被标记注入特征：override-instructions, pipe-to-shell, destructive-command, config-or-secret-reference, command-request（只作为数据保存，未产生知识，未执行其中命令）

## 3. 通过聊天采用（需管理员 + 一次性确认码）

> **ou_reader**：采用 1
>
> 权限不足：apply 需要管理员。

> **ou_owner**：采用 1
>
> 即将采用运行 1 为新版本。600 秒内回复“确认 74c3a9”执行。

> **ou_owner**：确认 74c3a9
>
> 已采用：运行 1 → 版本 #2 (49daead6262a)，健康检查通过。

> **ou_owner**：确认 74c3a9
>
> 确认码无效、已使用、已过期或不属于你。

## 4. 定时评测触发失败驱动研究

模拟时间 2026-10-26 08:00 调度触发：daily-batch-check→task 5

**worker：定时评测 + 失败驱动研究**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| 5 | evaluate_release | done | 版本 #2 F1=0.8571 失败 2 例；失败研究任务 [6, 7] |
| 6 | failure_research | done | 发现 5，新 5，已见 0，不相关 0，入库 5，新知识 6 |
| 8 | propose | done | 新提案 []，已测试跳过 [1, 2]，影子运行任务 []，失败研究任务 [] |
| 7 | failure_research | done | 发现 5，新 5，已见 0，不相关 1，入库 4，新知识 4 |
| 9 | propose | done | 新提案 []，已测试跳过 [1, 2]，影子运行任务 []，失败研究任务 [] |

通知（模拟通道，写入 outbox）：

- 版本 #2 在 batch2 上：F1 0.8571，误删 0，失败 2 例；新加入失败驱动研究 2 项。
- 失败驱动研究（paraphrase，主题“paraphrase identification”）：发现 5，新增 5，入库 5，新知识 6 条。
- 失败类别 paraphrase（2 例）已找到相关知识 [4, 5, 6, 7, 8, 9]（方法 paraphrase_classifier, semantic_embedding），但当前执行器无法实现：knowledge found but no code template: the template executor cannot implement it, and the methods need a new ML dependency (pyproject.toml is protected). Needs a human decision: approve the dependency and a model executor.
- 失败驱动研究（cross-language，主题“language-agnostic sentence embedding”）：发现 5，新增 5，入库 4，新知识 4 条。
- 失败类别 cross-language（1 例）已找到相关知识 [10, 12, 13]（方法 cross_lingual_embedding），但当前执行器无法实现：knowledge found but no code template: the template executor cannot implement it, and the methods need a new ML dependency (pyproject.toml is protected). Needs a human decision: approve the dependency and a model executor.

> **ou_reader**：问题
>
> paraphrase ×2 [blocked] knowledge found but no code template: the template executor cannot implement it, and the methods need a new ML dependency (pyproject.toml is protected). Needs a human decision: approve the dependency and a model executor.
> cross-language ×1 [blocked] knowledge found but no code template: the template executor cannot implement it, and the methods need a new ML dependency (pyproject.toml is protected). Needs a human decision: approve the dependency and a model executor.

## 5. 重复研究抑制与评测缓存

> **ou_reader**：研究 news deduplication
>
> 已加入研究任务 task 10：news deduplication

**worker：同主题再次研究**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| 10 | research | skipped | topic researched by task 1 at 2026-10-25T10:39:53-0700 (cooldown 72h) |

**worker：次日定时评测（结果来自缓存，受阻类别不再重复研究）**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| 11 | evaluate_release | done | 版本 #2 F1=0.8571 失败 2 例（缓存）；失败研究任务 [] |
| 12 | knowledge_review | done | 1:skipped, 2:skipped, 3:skipped, 5:skipped, 6:skipped |

通知（模拟通道，写入 outbox）：

- 版本 #2 在 batch2 上：F1 0.8571，误删 0，失败 2 例；新加入失败驱动研究 0 项。

**worker：同一提案重跑一次**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| 13 | run | done | 运行 3 → REJECTED（缓存命中 6） |

通知（模拟通道，写入 outbox）：

- 运行 3 结束：REJECTED。f1 delta +0.000 < required +0.050

该运行被拒绝是预期结果：它的基线是**当前**生效版本（已包含这项改动），F1 增量为 0。
运行 3 的 6 次评测中 6 次来自缓存（键 = collector 代码去文档字符串后的 AST 哈希 + 评测器版本 + 数据哈希；缓存命中不计入保留集曝光次数）。

缓存条目 10，累计命中 9。

## 6. 用户反馈

> **ou_reader**：重复 b02 b01
>
> 已记录 #1：b02 是 b01 的重复（待人工审核后可并入评测集）。

> **ou_reader**：保留 b10
>
> 已记录 #2：b10 不应被删除。

> **ou_reader**：反馈 日报里同一事件的中英文报道请合并
>
> 已记录反馈 #3。

> **ou_reader**：问题
>
> user-reported ×2 [untriaged] no mapping from this failure kind to research; needs triage
> paraphrase ×2 [blocked] knowledge found but no code template: the template executor cannot implement it, and the methods need a new ML dependency (pyproject.toml is protected). Needs a human decision: approve the dependency and a model executor.
> cross-language ×1 [blocked] knowledge found but no code template: the template executor cannot implement it, and the methods need a new ML dependency (pyproject.toml is protected). Needs a human decision: approve the dependency and a model executor.

## 7. 每日预算账本

把今日任务上限临时设为 14（已用 13），再加入 2 个任务：

**worker：预算受限**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| 14 | research | done | 发现 5，新 0，已见 5，不相关 0，入库 0，新知识 0 |
| — | — | idle | daily budget exhausted: tasks |

通知（模拟通道，写入 outbox）：

- 今日预算已用完（tasks），队列中的任务顺延到明天。

**worker：再次尝试（同一天不重复通知）**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| — | — | idle | daily budget exhausted: tasks |

**worker：恢复上限后继续（离线模式下知识复查记为跳过）**

| task | 类型 | 结果 | 摘要 |
|---|---|---|---|
| 15 | knowledge_review | done | 1:skipped, 2:skipped, 3:skipped |

> **ou_reader**：预算
>
> 今日用量：任务 15，模型调用 24，下载 0 字节，候选 8；剩余：candidates=12, download_bytes=50000000, model_calls=76, tasks=35

## 8. 通过聊天回滚

> **ou_owner**：回滚 2
>
> 即将回滚版本 2。600 秒内回复“确认 3ea655”执行。

> **ou_owner**：确认 3ea655
>
> 已回滚版本 2。校验：SHA=True 依赖=True 状态兼容=True 健康=True；当前 4dcf90ebf5a2

> **ou_reader**：状态
>
> 当前版本：#1 4dcf90ebf5a2
> 队列：done=14, skipped=1
> 最近运行：#3 REJECTED, #2 REJECTED, #1 ROLLED_BACK
> 今日剩余预算：candidates=12, download_bytes=50000000, model_calls=76, tasks=35

## 9. 后台运行开关

- 默认配置下守护进程拒绝启动：background operation is disabled (set "background": true in the config)
- 显式开启 `background` 并设置每日预算后运行 1 个周期：心跳 2026-10-25T10:39:58-0700，处理任务 0，队列 {'done': 14, 'skipped': 1}

## 10. 真实联网：增量检索与知识复查

- 实时检索“paraphrase identification”：返回 6 条，其中 3 条此前已见过（不重复下载），新条目 3 条，判为不相关 0 条，本次入库 1 条，错误 无。
  - 新：google-research-datasets/paws（相关性 1.00）
  - 新：nelson-liu/paraphrase-id-tensorflow（相关性 1.00）
  - 新：wasiahmad/paraphrase_identification（相关性 1.00）
- 复查 https://en.wikipedia.org/w/index.php?title=URI_normalization&oldid=1359957963 → **unchanged**：latest revision is still oldid=1359957963
- 复查 https://github.com/ekzhu/datasketch/blob/c7eaaeea3f39909f906a9cf8ad4d21a878cb4e07/datasket → **unchanged**：branch master moved to 24e27cd72108 but datasketch/minhash.py is byte-identical
- 复查 https://arxiv.org/abs/2107.06499v2 → **changed**：same version v2, full text now available

## 汇总

| 任务类型 | 状态 | 数量 |
|---|---|---|
| evaluate_release | done | 2 |
| failure_research | done | 2 |
| knowledge_review | done | 2 |
| propose | done | 3 |
| research | done | 2 |
| research | skipped | 1 |
| run | done | 3 |

知识（方法 / 验证状态 / 复查状态 / 来源读取级别）：

- #1 `url_canonicalization` effective_in_user_task / current / fulltext — https://en.wikipedia.org/w/index.php?title=URI_normalization&oldid=1359957963
- #2 `minhash_lsh` source_claim / current / fulltext — https://github.com/ekzhu/datasketch/blob/c7eaaeea3f39909f906a9cf8ad4d21a878cb4e07/datasketch/minhash.py
- #3 `shingle_jaccard` effective_in_user_task / source_changed / excerpt — https://arxiv.org/abs/2107.06499v2
- #4 `paraphrase_classifier` source_claim / current / abstract — https://arxiv.org/abs/1604.00503v1
- #5 `semantic_embedding` source_claim / current / abstract — https://arxiv.org/abs/2406.15066v1
- #6 `paraphrase_classifier` source_claim / current / abstract — https://arxiv.org/abs/2406.15066v1
- #7 `paraphrase_classifier` source_claim / current / abstract — https://arxiv.org/abs/2212.06933v3
- #8 `paraphrase_classifier` source_claim / current / abstract — https://arxiv.org/abs/1704.04565v2
- #9 `paraphrase_classifier` source_claim / current / abstract — https://arxiv.org/abs/2010.03725v1
- #10 `cross_lingual_embedding` source_claim / current / abstract — https://arxiv.org/abs/2302.08387v2
- #11 `semantic_embedding` source_claim / current / abstract — https://arxiv.org/abs/2007.01852v2
- #12 `cross_lingual_embedding` source_claim / current / abstract — https://arxiv.org/abs/2007.01852v2
- #13 `cross_lingual_embedding` source_claim / current / abstract — https://arxiv.org/abs/2603.08282v1

总耗时 22.1 秒。

