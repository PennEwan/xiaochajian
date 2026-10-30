# Codex 本地会话记录解析器（PoC）：使用说明与测试报告

日期：2026-10-29　工具版本：csr 0.1.0　设计：`docs/superpowers/specs/2026-10-29-session-reader-design.md`

只读地解析用户**明确指定目录**里的 Codex `rollout-*.jsonl`，增量读取、SQLite 保存进度与标准化事件、命令行展示统计、导出 JSONL。不使用 Hooks，不修改 Codex 程序、配置或会话文件，不联网，不读 `auth.json`，不后台驻留。

## 1. 安装与运行

要求：Windows，Python 3.12+（只用标准库）。不需要安装：仓库根目录的 `csr.cmd` 会设置 `PYTHONPATH`（`tools\session-reader` 和复用脱敏规则的 `plugins\codex-task-recorder\lib`）。

```bat
:: 1. 扫描一次（只读）。--session 可选，只处理该会话（ID 或前缀）
csr.cmd scan --dir %USERPROFILE%\.codex\sessions\2026\10\28 --session 01a18266-5a69

:: 2. 统计（终端不显示任何正文）
csr.cmd stats
csr.cmd stats --json

:: 3. 一边使用 Codex，一边前台定时扫描新内容（默认 2 秒，Ctrl+C 退出，进度已保存）
csr.cmd watch --dir %USERPROFILE%\.codex\sessions\2026\10\29 --interval 2

:: 4. 字段覆盖率
csr.cmd coverage
csr.cmd coverage --markdown

:: 5. 导出标准化事件（含脱敏后的完整正文）
csr.cmd export --out D:\csr-exports\events.jsonl [--session ID]

:: 6. 删除本工具保存的数据
csr.cmd purge --session 01a18266        :: 一个会话的记录 + 包含它的导出文件
csr.cmd purge --all                     :: 数据库 + 本工具产生的全部导出文件（需输入 y，或加 --yes）
```

- 数据库默认在 `%LOCALAPPDATA%\CodexSessionReader\csr.sqlite3`，可用 `--db` 指定。数据库与导出文件不允许放在被扫描目录或 Codex 数据目录（`CODEX_HOME`，默认 `%USERPROFILE%\.codex`）内，否则命令拒绝执行（退出码 2）。
- 授权范围：**指定的目录即授权范围**（用户 2026-10-29 选定）。目录内（默认不含子目录，`--recursive` 才包含）所有会话的完整 Prompt 与模型输出脱敏后保存在本地数据库；用 `--session` 可以进一步缩小范围，不匹配的文件不会被打开。
- Codex 会话按日期分目录（`sessions\YYYY\MM\DD\`），指定目录时请选测试会话所在的日期目录，或把测试会话复制到单独目录后再指定。

## 2. 模块

| 模块 | 职责 |
|---|---|
| `csr/reader.py` | 列出目录内 `rollout-*.jsonl`（解析后不在目录内的链接 / 联接点跳过）；Windows 以读 / 写 / 删除共享方式打开、读完即关；从偏移量读到最后一个完整行 |
| `csr/parser.py` | 一行 → 0..n 条 `csr.event/v1` 事件 + 问题 + 跳过计数；可序列化的解析上下文（当前回合、回合→模型、上一 ordinal） |
| `csr/storage.py` | SQLite：`files`（偏移、大小、mtime、首部指纹、残行、上下文）、`events`、`issues`、`skipped`、`exports`；一批事件与新偏移同一事务提交 |
| `csr/scanner.py` | 编排一次扫描：变化检测 → 替换检测 → 增量读取 → 解析 → 分批提交 |
| `csr/summary.py` | 会话统计、字段覆盖率 |
| `csr/cli.py` | 命令行 |

增量读取要点：
- 偏移量只推进到最后一个完整行之后，残行不消费；`files.pending` 记录残行字节数，下次扫描与新增内容拼接续读。残行以文件本身为缓存，进程崩溃也不会丢。
- 去重：`event_key = <会话ID>:<ordinal>:<序号>` 为主键；每批在 `BEGIN IMMEDIATE` 事务里先核对库中偏移未被其他进程推进。
- 大小与 mtime 都没变的文件直接跳过；文件变短或开头 4 KB 指纹改变时视为被替换，删除旧结果并从头重读，记录 `file_rewritten`。
- 写库失败时整批回滚并记录 `store_failed`，下次从上次提交处重读。

## 3. 标准化事件

`csr.event/v1`，字段说明见设计文档第 5 节。kind 包括：`session_meta`、`model_config`（scope = turn / thread）、`model_changed`、`turn_started`、`turn_completed`（status = completed / error）、`turn_error`、`turn_aborted`、`user_prompt`、`assistant_message`（phase = commentary / final_answer）、`reasoning_summary`、`tool_call`、`tool_result`、`tool_execution`、`token_usage`。

- 拿不到的值保留 `null`，并在 `missing` 写原因，例如 `{"field": "summary", "reason": "模型未返回公开推理摘要（加密推理内容不解密）"}`。
- 正文经 `ctr.redact` 脱敏（API Key、令牌、Cookie、私钥、口令赋值、CDK 等）；输入涉及 `auth.json`、`.env`、私钥等凭证文件的工具调用，其输出替换为 `[REDACTED:sensitive_file]`；单个字符串超过 256 K 字符时截断，并记录原长与 sha256。
- 从不保存：`session_meta.creator_user_id / creator_account_id`、系统指令全文（只记长度）、`world_state`、`rate_limits`、加密推理、工具定义。

## 4. 测试结果

### 4.1 单元测试（合成样本，形状与 0.163.0-alpha.10.2 实测一致）

```bat
set PYTHONPATH=plugins\codex-task-recorder\lib;tools\session-reader;tests
python -m unittest test_csr_reader test_csr_parser test_csr_scanner test_csr_summary test_csr_cli
```

结果：**71 个测试全部通过**（reader 12，parser 20，scanner 16，summary 6，cli 17）。

| 验收场景 | 测试 | 结果 |
|---|---|---|
| 1. 连续提交三条 Prompt | `ScenarioTest.test_scenario1_three_prompts_in_order` | 通过：3 条 Prompt，按回合顺序，turn_id 正确 |
| 2. 模型回复与工具调用 | `test_scenario2_replies_and_tool_calls`、`TurnTest.*` | 通过：commentary / final_answer、call_id 关联、退出码与成败 |
| 3. 中途切换模型 | `test_scenario3_model_switch`、`EdgeTurnTest.test_model_switch_between_turns` | 通过：派生 `model_changed`；各事件归属正确模型；切换后首个回合的 `turn_started` 取线程级新设置 |
| 4. 会话文件追加 | `test_scenario4_*`、`test_any_byte_split_with_restarts_matches_a_single_pass` | 通过：随机字节位置分块追加，结果与一次性解析逐条一致 |
| 5. 退出并恢复 | `test_scenario5_*`、`test_failed_commit_rolls_back_and_next_scan_recovers`、`test_parser_context_survives_restart` | 通过：重启后无重复；写库失败整批回滚，之后完整恢复 |
| 6. 不完整行与异常记录 | `test_scenario6_*`、`BadInputTest.*` | 通过：残行待续读；坏 JSON / 非对象 / 缺 type / payload 非对象 / 非 UTF-8 记为解析失败；ordinal 断档单独计数，不与坏行重复计 |
| 只读与不干扰 Codex | `test_source_files_are_never_modified`、`SharedOpenTest.*` | 通过：源文件哈希与 mtime 不变；读取期间文件可被重命名 / 删除（对照：普通 `open()` 会让重命名失败） |
| 替换检测 | `test_replaced_file_is_reparsed_from_scratch`、`test_same_size_rewrite_is_detected_by_head_fingerprint` | 通过 |
| 权限边界 | `SafetyTest.*`、`test_export_inside_scanned_or_codex_directory_is_refused` | 通过：数据库 / 导出不能写进被扫描目录或 Codex 目录 |
| 脱敏 | `RedactionTest.*`、`test_maps_config_and_drops_account_identifiers` | 通过 |

关键测试做过变异检查：把“偏移只推进到最后一个换行”改成“推进到文件末尾”，有 5 个测试失败；把“恢复解析上下文”改成“新建上下文”，有 3 个测试失败。

全仓库 `python -m unittest discover -s tests -t tests`：515 个测试，514 个通过。失败的 1 个是 `test_companion_update.BackgroundTest.test_auto_mode_installs_a_verified_release`（P4 助手包，本轮未改动）：该用例没有隔离 `recharge_window_open()`，本机存在 `Local\CodexCdkRecharge.Window` 互斥量（有充值窗口开着）时返回 `deferred`，属于环境依赖。

### 4.2 真实会话验证

用户指定的测试会话：`01a18266-5a69-7a92-a786-be8e4f80663f`（2026-10-28，Codex Desktop，cwd = `D:\ctr-testbed`，239,587 字节，61 行）。

```bat
set PYTHONPATH=tools\session-reader;plugins\codex-task-recorder\lib
python scripts\csr_live_check.py --session-file %USERPROFILE%\.codex\sessions\2026\10\28\rollout-2026-10-28T20-05-08-01a18266-5a69-7a92-a786-be8e4f80663f.jsonl
```

| 检查 | 结果 |
|---|---|
| 直接扫描 Codex 日期目录时只打开指定会话文件 | 通过 |
| 事件主键无重复 | 通过（33 条事件） |
| 真实文件无解析失败 | 通过 |
| 回放：同一文件按随机字节位置分 78 次追加，每 4 次“重启”一次（共 19 次），中途模拟 1 次写库失败；77 次扫描遇到未写完的行 | 结果与一次性扫描**逐条一致**，统计一致 |
| 回放中只多出那一次模拟失败的 `store_failed` 记录 | 通过 |
| 损坏副本：一行截半 + 一行乱码 + 未写完的尾行 | 解析失败 2，ordinal 断档 0，其余事件照常解析（33 → 32，少的正是被截断行产生的 1 条），残行 20 字节待续读 |
| 源文件前后大小、mtime、sha256 一致 | 通过 |

CLI 实测（`csr.cmd`，临时数据库）：第一次 `scan` +33 事件，第二次显示“无变化”；`export` 33 条，导出文件里没有账户标识、加密推理、额度信息、系统指令；导出到 `.codex` 目录、数据库放进被扫描目录都被拒绝；`purge --all` 删除了数据库和导出文件。

`csr stats` 输出（真实会话）：

```
会话 01a18266-5a69-7a92-a786-be8e4f80663f
  来源文件    rollout-2026-10-28T20-05-08-01a18266-5a69-7a92-a786-be8e4f80663f.jsonl 已读 239,587/239,587 字节
  客户端      Codex Desktop · 内核 0.163.0-alpha.10.2 · 2026-10-28T12:05:16.430Z → 2026-10-28T12:08:02.203Z
  用户任务    3 次（回合 3：完成 2，出错 0，中断 1，未结束 0）
  模型        gpt-6-luna[xhigh]×2 → gpt-6.1-sol[xhigh]×1（切换 1 次）
  模型回复    4 条（final_answer 2，commentary 2）
  工具调用    模型级 1 次（有结果 1，无结果 0）；执行条目 2 个（CommandExecution 2；失败 0）
  推理摘要    公开 1 条，不可得 3 条
  Token       合计 85,024（输入 84,599，缓存 83,712，输出 425，推理 210）
  回合错误    0
  解析失败    0 行
  缺失        ordinal 断档 0 条；无结果工具调用 0；未结束回合 0；含缺失字段的事件 3（summary×3）
  其他问题    无
  未存储记录  duplicate:event_msg/token_count×4，duplicate:response_item/message×18，duplicate:response_item/reasoning×4，omitted:encrypted_reasoning×4，omitted:rate_limits×4，omitted:response_item/additional_tools×1，omitted:session_meta.creator_ids×1，omitted:world_state×2
```

## 5. 事件字段覆盖率（真实会话 01a18266…）

| 字段 | 来源记录 | 适用数 | 有值 | 覆盖率 | 说明 |
|---|---|---|---|---|---|
| Session ID | session_meta.id / 文件名 | 33 | 33 | 100% |  |
| Turn ID | 各记录 turn_id | 24 | 24 | 100% | 其中 2 条按当前回合推断（模型级工具调用记录本身不带 turn_id） |
| 时间戳 | 每行 timestamp | 33 | 33 | 100% |  |
| 用户 Prompt | item_completed:UserMessage | 3 | 3 | 100% |  |
| 模型公开消息 | item_completed:AgentMessage | 4 | 4 | 100% | phase 区分 commentary / final_answer |
| 模型名称 | turn_context.model（按回合） | 3 | 3 | 100% |  |
| 推理强度 | turn_context.effort | 3 | 3 | 100% |  |
| 推理摘要设置 | turn_context.summary | 3 | 3 | 100% |  |
| 工具调用名称 | custom_tool_call / function_call | 1 | 1 | 100% |  |
| 工具调用参数 | 同上 input / arguments | 1 | 1 | 100% |  |
| 工具调用结果 | *_call_output（按 call_id 关联） | 1 | 1 | 100% |  |
| 工具执行结果（退出码 / 状态） | item_completed:CommandExecution / McpToolCall | 2 | 2 | 100% | 执行条目与模型级调用之间没有关联 ID |
| 公开推理摘要 | item_completed:Reasoning.summary_text | 4 | 1 | 25% | 加密推理 4 条未解密 |
| 回合结束事件 | task_complete / turn_aborted | 3 | 3 | 100% | 出错 0，中断 1 |
| Token 使用量 | token_usage_record | 2 | 2 | 100% | 被中断的回合没有 token_usage_record |
| 首 Token 时延 | task_complete.time_to_first_token_ms | 2 | 2 | 100% |  |

本机全部 29 个 rollout 文件（同一版本）的结构统计：Reasoning 条目 97 条，只有 7 条带公开摘要；`task_complete.error` 出现 45 次（44 次 `server_overloaded`），可映射为回合错误；ordinal 1548 次相邻全部连续。

## 6. 无法获得的数据及原因

| 数据 | 原因 |
|---|---|
| 大多数回合的推理摘要 | 模型没有返回（`summary_text` 为空）。本机所有回合的摘要设置 `turn_context.summary` 都是 `none`，但仍有 7/97 条返回了摘要，所以无法从记录判断“为什么没有”；摘要设置为其他值时的表现，本机没有样本，也不按要求修改 Codex 配置去制造样本 |
| 推理原文 | 只有 `response_item/reasoning.encrypted_content`（加密），按要求不解密；`raw_content` 实测全部为空 |
| 模型级工具调用与命令执行条目的对应关系 | `custom_tool_call` 是代码模式 `exec`，一次可以执行多条命令；`call_id` 与执行条目的 `item_id` 不同，记录里没有关联字段，只能按回合归属 |
| 模型级工具调用的 turn_id | `custom_tool_call(_output)` 本身不带 turn_id，按“当前进行中的回合”推断并标记 `turn_id_inferred`；回合结束后才出现的调用不推断，记为缺失 |
| 被中断回合的 Token 用量 | 中断的回合没有 `token_usage_record`（`token_count` 的累计值也没有增加） |
| 中断原因细节 | `turn_aborted.reason` 只有 `interrupted` 这类枚举，没有更多说明 |
| 系统指令 / 开发者消息全文、工具定义、额度信息、账户标识 | 记录里有，但按数据边界不保存（只记长度或计数） |
| 已归档的会话 | Codex 把旧会话移到 `archived_sessions\*.jsonl.zst`（zstd 压缩），Python 3.12 标准库无法解压；本工具也只读取用户指定目录 |
| Codex 其他本地数据库（`state_5.sqlite`、`thread_history_1.sqlite`、`logs_2.sqlite` 等） | 不在授权范围内，没有访问 |

## 7. 兼容性限制（当前 Codex 版本）

- 只在 **Codex Desktop 26.1013.4894.0 / 内核 0.163.0-alpha.10.2** 的 rollout 上验证过；本机 `sessions` 下 29 个文件全部是这个版本，更早的版本已被归档压缩，无法对照。
- Codex 文档说明 rollout 格式不是稳定接口。本工具只映射实测出现过的记录类型；未知类型、未知 `item_completed` 条目只按类型名计入“未存储记录 / unrecognized”，不会报错，也不影响其他记录。格式变化后，统计里的 `unrecognized:*` 计数会上升，需要按新样本补映射。
- 旧版 Codex 的 `event_msg/user_message`、`agent_message`、`agent_reasoning`、`exec_command_end` 等记录本机没有样本，**没有映射**（会计为 unrecognized），避免在无法验证的情况下重复计数。
- `function_call`、`local_shell_call`、`web_search_call`、`FileChange` 按公开格式映射，本机样本里没有出现，未经真实数据验证。
- 依赖 `ordinal` 字段做去重和断档检测；没有 ordinal 的记录退回用字节偏移作键，此时无法检测断档。
- 子代理（multi-agent）会话、`/compact` 压缩后的记录、权限请求在本机样本中没有出现，未验证。
- 文件被 Codex 改写（变短或开头改变）时整体重读；同样大小、同一 mtime 的原地改写无法检测（Codex 的 rollout 是只追加写入，实测没有出现过）。
- `watch` 用定时扫描，没有文件系统监听；新内容最晚在一个扫描间隔后显示。
