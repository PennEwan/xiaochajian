# 任务数据同步后台接口约定 v1（草案）

- 日期：2026-10-28；状态：**后台尚未实现**。客户端（`plugins/codex-task-recorder/lib/ctr/sync`）与本地模拟后台（`scripts/mock_sync_server.py`）按本文实现。
- 设计：`docs/superpowers/specs/2026-10-28-data-sync-design.md`

## 0. 边界

- 只接收用户在本机 **单独开启并同意同步之后** 记录的事件。客户端在上传前已完成授权过滤与脱敏，后台不应假设收到的是全部会话内容。
- 身份是 **匿名设备**：客户端不发送 ChatGPT 账号、邮箱或任何 ChatGPT 凭证。与充值后台（`docs/recharge-api.md`）没有任何关联，不共享令牌。
- 后台日志不得记录事件正文与 `device_token`。

## 1. 通用

- Base URL 可配置（`%LOCALAPPDATA%\CodexTaskRecorder\sync\config.json` 的 `base_url`，`ctr sync config --base-url …`），**默认未配置**。非本机地址必须是 `https://`。
- 请求与响应都是 `application/json; charset=utf-8`；请求头 `X-Ctr-Client: codex-task-recorder/<版本>`。
- 除注册外，请求都带 `Authorization: Bearer <device_token>`。`device_token` 由后台生成，**必须以 `ctrd_` 开头**（客户端据此在任何记录内容中识别并脱敏）。
- 错误统一格式：`{"error": {"code": "<错误码>", "message": "<可选>"}}`。
- 客户端不跟随重定向；后台不要对这些接口返回 3xx。
- 单次请求：连接、发送、等待响应各 15 秒。

通用错误：

| 状态 | code | 客户端行为 |
|---|---|---|
| 401 | `device_invalid` | 丢弃令牌，下一轮重新注册（得到新设备 ID） |
| 403 | `device_revoked` | 关闭本机同步，状态提示"后台已停用此设备" |
| 429 | `rate_limited` | 按 `Retry-After`（秒）等待；客户端把等待时间限制在 2–300 秒 |
| 5xx | 任意 | 指数退避重试（2 秒起，上限 300 秒） |

注册（第 2 节）失败时，无论什么状态码，客户端都只退避重试，不会因此丢弃任何事件。

## 2. 注册设备 `POST /v1/devices`

请求：
```json
{"client": "codex-task-recorder/0.2.0", "platform": "windows",
 "consent": {"version": 1, "accepted_at": "2026-10-28T10:00:00.000Z"}}
```
`201`：
```json
{"device_id": "dev_8f3c…", "device_token": "ctrd_…", "created_at": "2026-10-28T10:00:01Z"}
```

## 3. 设备信息 `GET /v1/devices/me`

`200`：
```json
{"device_id": "dev_8f3c…", "event_count": 1234, "session_count": 12,
 "last_event_at": "2026-10-28T11:00:00Z", "created_at": "2026-10-28T10:00:01Z"}
```
用于状态页显示"后台已收到多少条"。

## 4. 上传事件 `POST /v1/events/batch`

请求：
```json
{
  "schema": "ctr.sync.batch/v1",
  "batch_id": "b6c1…",
  "client": "codex-task-recorder/0.2.0",
  "capabilities": {
    "not_collected_by_policy": ["encrypted_reasoning", "raw_reasoning", "system_instructions_text",
                                "rate_limits", "account_info", "credential_file_contents",
                                "recorder_diagnostics"],
    "not_provided_by_codex": ["hook_source_timestamps", "post_tool_use_exit_code",
                              "stop_hook_on_failed_or_interrupted_turn",
                              "background_session_transcript"],
    "not_verified": ["PermissionRequest", "PostCompact", "SubagentStart", "SubagentStop"]
  },
  "events": [ { "schema": "ctr.event/v2", "event_id": "…", "seq": 17, "session_id": "…", "…": "…" } ]
}
```

- 每批 ≤ 200 条、正文 ≤ 1 MiB；单条事件超过 1 MiB 时单独成批。后台应接受至少 **4 MiB** 的正文。
- `batch_id` 每次发送都不同，只用于日志排查；**幂等键是 `event_id`**（在同一设备内唯一）。
- 同一会话的事件按 `seq` 递增出现；前一批确认之前客户端不会发送同一会话的后续事件。后台按 `(session_id, seq)` 排序展示；`seq` 可以有空洞（授权时段外的事件不上传）。
- 事件字段见第 6 节。

`200`：
```json
{"accepted": 198, "duplicates": 2, "rejected": 0,
 "results": [{"event_id": "…", "status": "accepted"},
             {"event_id": "…", "status": "duplicate"},
             {"event_id": "…", "status": "rejected", "error": {"code": "invalid_event"}}]}
```

- **幂等（必须）**：已存过的 `event_id` 返回 `duplicate`，不重复存储。客户端在超时、连接中断后会原样重发。
- `results` 必须覆盖请求里的每个 `event_id`；不完整时客户端视为结果未知并重发。
- `rejected` 只用于无法修复的单条错误（例如缺少 `event_id`），客户端计数后跳过，不再重发。

整批错误：

| 状态 | code | 客户端行为 |
|---|---|---|
| 400 | `invalid_batch` | 批量减半定位；单条仍 400 → 计为拒收并跳过 |
| 400 | 其他错误码或无法解析的正文 | 视为暂时错误，退避重试（例如中间代理返回的错误页），不拒收事件 |
| 413 | `batch_too_large`（或代理的 413） | 批量减半；单条仍 413 → 计为拒收并跳过 |

**后台对整批有意见时必须返回 `400 invalid_batch`**：只有这个错误码会让客户端拆批并最终放弃某条事件。

## 5. 删除与撤销

### 5.1 删除已上传数据 `DELETE /v1/devices/me/data`
删除本设备上传过的全部事件。`200`：`{"deleted_events": 1234}`；后台也可以异步删除并返回 `202 {"status": "scheduled"}`。重复调用返回 `200 {"deleted_events": 0}`。

### 5.2 撤销设备 `DELETE /v1/devices/me`
撤销后该设备进入 `terminated`：`POST /v1/events/batch` 及其余已认证接口返回 **403 `device_revoked`**（不是 401：401 会让客户端重新注册、把已撤销的设备又造回来）。只有三个路径仍然应答——`GET /v1/service-state`（返回 `state: terminated`，采集器据此停下）、重复的 `POST /v1/service-state/terminate`、重复的 `POST /v1/device/uninstall`。

`200`：`{"revoked": true, "state": "terminated"}`；对已撤销的设备重复调用 `DELETE /v1/devices/me` 返回 403，客户端视为已完成。

> 实现见 `server/ctrsrv/app.py`（`revoke_device` / `auth` 与 `TERMINATED_ALLOWED`）。错误码语义与 5.1 的说明一致：401 / 403 都当作"设备已不存在"，视为完成。

客户端撤销授权时的顺序：先 5.1（用户选择删除时），再 5.2。离线时这两个操作保存在本机，联网后由上传进程执行（在任何上传之前）。

客户端对这两个接口的错误处理：网络错误、429、5xx → 保留并重试；401 / 403 → 设备已不存在，视为完成；其他 4xx → 后台不会接受，记录结果后视为完成，并如实告诉用户"后台未能删除"。所以 **5.1 不可用时请返回 5xx，而不是 404**，否则客户端会认为删除无法完成。

## 6. 事件 `ctr.event/v2`

```json
{
  "schema": "ctr.event/v2",
  "event_id": "uuid4",
  "seq": 17,
  "kind": "tool_result",
  "recorded_at": "2026-10-28T10:00:00.123Z",
  "source_ts": "2026-10-28T10:00:00.100Z",
  "session_id": "…", "session_kind": "standard | background",
  "turn_id": "… | null", "tool_use_id": "… | null", "item_id": "… | null", "agent_id": "… | null",
  "model": "gpt-6-luna | null",
  "relations": {},
  "source": {"type": "hook | transcript | derived", "name": "…", "codex_version": "…",
             "originator": "…", "transcript_offset": 1234},
  "granularity": "model_tool_call | ui_item | null",
  "completeness": {"status": "complete | partial | truncated | unavailable", "missing": [], "note": ""},
  "redaction": {"applied": true, "counts": {"openai_key": 1}, "upload_pass": {}},
  "data": {}
}
```

- `kind` 与 `data` 内容见记录器设计 6.4 节；新增 `turn_coverage`（数据同步设计 3.1 节）。
- `completeness.status` 为 `unavailable` 的事件表示"预期存在但来源没有提供"，内容不填充。
- `hook_invocation` 上传时只含 `payload_keys`、`transcript_path_present`、`notice_emitted`、`duration_ms`。
- 字符串字段上限 64 KiB，超出部分截断并在 `completeness.truncated_fields` 中给出原长度与 sha256。

## 7. 模拟后台

`python scripts\mock_sync_server.py [--port 18788] [--store events.jsonl]`。实现以上全部接口；控制接口（仅模拟后台）：

| 接口 | 作用 |
|---|---|
| `POST /_mock/fail-next {"status": 503, "count": 1}` | 接下来 `count` 次请求返回该状态（429 带 `Retry-After`，默认 1，可用 `retry_after` 指定）；可选 `"code"` 指定错误码、`"path"` 只让该路径的请求失败 |
| `POST /_mock/fail-next {"mode": "drop_after_store"}` | 下一个批次存下后直接断开连接，不返回响应 |
| `POST /_mock/revoke-all` | 停用所有设备（之后返回 403 `device_revoked`） |
| `GET /_mock/events` | 收到的全部事件（按设备、会话、seq 排序；`_arrival` 为到达顺序） |
| `GET /_mock/envelopes` | 每个批次除事件外的部分（schema、batch_id、capabilities） |
| `GET /_mock/stats` | 各设备事件数、重复数、拒收数、批次数 |

采集组件（collector）的四个新接口 `GET /v1/service-state`、`POST /v1/service-state/terminate`、`POST /v1/device/heartbeat`、`POST /v1/device/uninstall` 与它们的 `_mock/service-state`、`_mock/heartbeats`、`_mock/uninstalls` 观察接口，由同一个模拟后台提供，约定见 `docs/collector-api.md`。

## 8. 原生插件（codex-native-assist）的上传（2026-10-29）

原生插件沿用第 1–4 节的接口与错误约定（注册、`Authorization: Bearer ctrd_…`、`POST /v1/events/batch`、幂等、拆批、退避）。与上面的记录器版本有以下不同：

- **授权**：没有单独的同步开关。开启任务记录即包含上传（产品方会告知用户），授权窗口的说明也已写明。关闭任务记录或在 Codex 中禁用插件后不再上传，未上传的内容留在本机。
- **地址**：配置项 `sync_base_url`。优先级：环境变量 `ASSIST_SYNC_URL` > 数据目录 `config.json`（`assist.exe config --sync-url <url>` 写入，`--sync-reset` 清除）> 插件包内 `plugin-config.json`。**默认为空**，此时不上传，数据暂存本机，状态显示「未配置服务地址」。非本机地址必须是 `https://`。
- **请求头**：`X-Ctr-Client: codex-native-assist/<版本>`。
- **注册**：`{"client": "codex-native-assist/<版本>", "platform": "windows", "consent": {"version": <授权版本号>, "accepted_at": "<授权时间>"}}`。设备令牌用 DPAPI（当前用户）加密，保存在数据目录的 `sync.bin` 中。后台地址变了会重新注册。
- **批次**：
  - `schema` 为 `assist.sync.batch/v1`，不含 `capabilities`；
  - `events` 里是原生事件 `csr.event/v1`，即本机库中保存的正文（已按授权过滤、已脱敏），另外加两个字段：
    - `event_id` = `event_key`，格式为 `<session_id>:<ordinal>:<idx>`，在同一设备内唯一；
    - `seq` = `ordinal × 1000 + idx`，同一会话内递增。
  - 每批最多 200 条、正文最多 1 MiB，按写入顺序发送，一批确认后才发下一批。
- **上传后删除本机数据**：
  - 后台对某条事件返回 `accepted`、`duplicate` 或 `rejected` 后，客户端在同一个事务里把它从本机库删除，并累加计数；
  - 没有在 `results` 中出现的事件留在本机，下次重发。
  - 删除后的空间会回收：新库开启 `auto_vacuum=INCREMENTAL`，旧库在空闲页超过 1 MiB 时做一次 `VACUUM` 转换。
- **节奏**：
  - 由采集负责人进程在每轮采集（约 2 秒一轮）后执行，每次最多 5 批；
  - 失败时按 2 秒起、最多 300 秒指数退避；
  - 429 按 `Retry-After`（2–300 秒）等待；
  - 401 时丢弃令牌，下次重新注册；
  - 403 `device_revoked` 时停止上传，本机数据保留，直到重新配置地址。
- **状态**：`recording_status` 工具和 `assist.exe status` 中的 `collector.sync` 字段包括：
  - `configured`、`state`（`not_configured` / `idle` / `uploading` / `retrying` / `revoked`）；
  - `uploaded`、`duplicates`、`rejected`、`last_upload_at`、`last_error`（只记录错误码）。
- **后台需要注意**：
  - 接受 `csr.event/v1` 事件（模拟后台已支持）；
  - 以 `(设备, event_id)` 去重；
  - 同一事件可能因为「结果未知」被重发，也可能因为本机会话文件被改写后重新采集而再次出现，都应返回 `duplicate`。
