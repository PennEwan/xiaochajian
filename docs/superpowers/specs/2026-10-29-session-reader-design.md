# Codex 本地会话记录解析器（PoC）设计

日期：2026-10-29　状态：已实现（使用说明与测试报告：`docs/session-reader.md`）

## 1. 目标与边界

只读地解析用户**明确指定目录**下的 Codex `rollout-*.jsonl`，增量读取、标准化事件、SQLite 保存进度与结果，命令行展示统计并导出 JSONL。

- 不用 Hooks、不改 Codex 程序 / 配置 / 会话文件、不联网、不后台驻留、不读 `auth.json`、不访问其他目录（包括 `~/.codex` 下的各个 SQLite 库）。
- 用户决定：**指定目录即授权**。目录内会话的完整 Prompt 与模型输出（脱敏后）保存在本地数据库；终端摘要在任何情况下都不打印正文。
- 本轮不做：充值、云端同步、安装器、文件系统监听（用定时扫描）。

## 2. 实测文件格式（Codex Desktop 26.1013.4894.0 / 内核 0.163.0-alpha.10.2）

每行：`{timestamp, ordinal, type, payload, metadata?}`；`ordinal` 在文件内从 0 连续递增（本机 29 个文件、1548 次相邻全部连续）。文件名 `rollout-<本地时间>-<会话UUID>.jsonl`，首行为 `session_meta`。

| 记录 | 用途 | 处理 |
|---|---|---|
| `session_meta` | 会话 ID、版本、客户端、cwd | 映射；**丢弃** `creator_user_id` / `creator_account_id`，`base_instructions` 只记长度 |
| `turn_context` | 每回合模型、推理强度、摘要设置、审批 / 沙箱 | 映射 `model_config(scope=turn)`；模型变化派生 `model_changed` |
| `event_msg/thread_settings_applied` | 线程级设置（切换模型时先出现） | 映射 `model_config(scope=thread)` |
| `event_msg/task_started` / `task_complete` / `turn_aborted` / `error` | 回合生命周期，`task_complete.error` 为回合错误 | 映射 |
| `event_msg/item_completed:UserMessage` / `AgentMessage` | Prompt、模型公开消息（`phase` = commentary / final_answer） | 映射 |
| `event_msg/item_completed:Reasoning` | 公开推理摘要 `summary_text`（多数为空） | 映射；空则标“不可得” |
| `event_msg/item_completed:CommandExecution` / `McpToolCall` / `FileChange` | 工具执行条目（退出码、输出） | 映射 `tool_execution` |
| `response_item/custom_tool_call(_output)`、`function_call(_output)`、`local_shell_call`、`web_search_call` | 模型级工具调用与结果（`call_id` 关联） | 映射 `tool_call` / `tool_result`；无 turn_id，按当前回合推断并标注 |
| `token_usage_record` | 每次响应 / 回合 / 线程累计 Token | 映射 `token_usage` |
| `response_item/message`、`response_item/reasoning`、`event_msg/token_count` | 与上面重复（message 的 user/developer 角色含注入的系统指令） | 只计数 |
| `world_state`、`response_item/additional_tools`、`token_count.rate_limits`、`reasoning.encrypted_content` | 指令 / 环境、工具定义、账户额度、加密推理 | 按策略只计数，不存、不解密 |
| 其他 | — | 计为“未识别”，只记类型名 |

## 3. 模块（`tools/session-reader/csr/`，仅标准库 + SQLite）

| 模块 | 职责 |
|---|---|
| `reader` | 列出目录内 `rollout-*.jsonl`（默认不递归；解析后不在目录内的路径跳过）；Windows 以 `FILE_SHARE_READ|WRITE|DELETE` 打开、读完即关；从偏移量读到最后一个换行符；首部指纹 |
| `parser` | 一行 → 0..n 条 `csr.event/v1` 事件 + 问题 + 跳过计数；`Context` 保存当前回合、回合→模型、上一 ordinal 等，可序列化 |
| `storage` | SQLite：`files`（偏移、大小、mtime、首部指纹、残行字节、解析上下文）、`events`（主键去重）、`issues`、`skipped`、`exports`；每批事件与偏移在同一事务提交 |
| `scanner` | 编排：发现文件 → 检测替换 → 增量读取 → 解析 → 分批提交 |
| `summary` | 会话统计、字段覆盖率 |
| `cli` | `scan` / `watch` / `stats` / `coverage` / `export` / `purge` |

依赖：脱敏复用 `ctr.redact`（纯函数，与 Hooks 无关），`csr.cmd` 设置 `PYTHONPATH`。

## 4. 增量读取与一致性

- 偏移量只推进到最后一个完整行之后；不完整的尾行不消费，`files.pending_bytes` 记录其长度，下次扫描与新增内容拼接后续读（以文件本身作为残行缓存：进程崩溃也不会丢残行）。
- 去重键 `event_key = <session_id>:<ordinal>:<序号>`（无 ordinal 时用 `@<字节偏移>`），`INSERT OR IGNORE`。
- 一批事件、问题、跳过计数、新偏移、解析上下文在一个 `BEGIN IMMEDIATE` 事务内提交；提交前核对库中偏移未被其他进程推进，否则放弃本批。
- 文件大小与 mtime 都未变 → 跳过。文件变短或首部 4 KB 指纹变化 → 视为被替换：删除该文件的已有结果，记录 `file_rewritten`，从头重读。
- 退出后重启：从 `files` 恢复偏移与上下文，继续读取。

## 5. 标准化事件 `csr.event/v1`

```json
{"schema": "csr.event/v1", "event_key": "…:15:0", "session_id": "…", "turn_id": "…",
 "turn_id_inferred": false, "ordinal": 15, "ts": "2026-10-28T12:05:20.123Z",
 "kind": "user_prompt", "model": "gpt-6-luna", "item_id": "…", "call_id": null,
 "data": {"text": "…"}, "missing": [{"field": "…", "reason": "…"}],
 "source": {"file": "rollout-….jsonl", "offset": 12345, "record": "event_msg/item_completed:UserMessage",
            "codex_version": "0.163.0-alpha.10.2"},
 "redactions": {}, "truncated": []}
```

kind：`session_meta`、`model_config`、`model_changed`、`turn_started`、`turn_completed`、`turn_aborted`、`turn_error`、`user_prompt`、`assistant_message`、`reasoning_summary`、`tool_call`、`tool_result`、`tool_execution`、`token_usage`。

- `model`：该事件所在回合的 `turn_context.model`，回合尚未出现 `turn_context` 时取最近的线程级设置。
- 拿不到的值填 `null`，并在 `missing` 写明原因；不尝试补全。
- 正文字段全部经过 `ctr.redact` 脱敏；输入涉及凭证文件（`auth.json`、`.env`、私钥等）的工具调用，其输出替换为占位符；单个字符串超过 256 K 字符截断并记录原长与 sha256。

## 6. 统计口径（终端不打印正文）

- 用户提交任务次数 = `user_prompt` 数（另列回合数：完成 / 出错 / 中断 / 未结束）。
- 使用过的模型 = 各回合 `turn_context.model`（附推理强度、回合数），切换次数 = `model_changed` 数。
- 模型回复数 = `assistant_message` 数（分 final_answer / commentary）。
- 工具调用次数 = 模型级 `tool_call` 数（有 / 无结果）+ 执行条目 `tool_execution` 数（按类型、失败数）。
- 解析失败 = `issues` 中 parse 类（坏 JSON、非对象、缺 type、payload 非对象、编码错误）。
- 缺失事件 = ordinal 断档（扣除同位置的解析失败行）+ 无结果的工具调用 + 未结束回合 + 含缺失字段的事件。

## 7. 本地数据与删除

- 数据库默认 `%LOCALAPPDATA%\CodexSessionReader\csr.sqlite3`，可用 `--db` 指定；数据库与导出文件不得位于被扫描目录或 Codex 数据目录（`CODEX_HOME`，默认 `~/.codex`）内。
- `purge --session ID` 删除该会话的全部记录及包含它的导出文件；`purge --all` 删除数据库与本工具产生的全部导出文件。

## 8. 测试

- 单元测试（合成样本，形状与实测一致）：三条连续 Prompt；模型回复与工具调用；中途切换模型；文件追加；退出并恢复（含事务中途失败回滚）；不完整行与异常记录；文件替换；读取期间文件可被重命名 / 删除；只读（源文件哈希与 mtime 不变）。
- 真实验证：用户指定测试会话 `01a18266-5a69-7a92-a786-be8e4f80663f`（10/28，Codex Desktop，cwd=D:\ctr-testbed）。直接扫描一次；再把同一文件按任意字节位置分块追加到临时目录，期间多次“重启”，结果必须与一次性扫描逐条一致；源文件前后哈希一致。
