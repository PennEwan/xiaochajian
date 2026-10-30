# 采集组件后台接口约定 v1（草案）

- 日期：2026-11-01；状态：**后台尚未实现**。客户端（`plugins/codex-companion/lib/collector`）与本地模拟后台（`scripts/mock_sync_server.py`）按本文实现。
- 设计：`docs/superpowers/specs/2026-11-01-collector-separation-design.md` 第 6 节。
- 事件上传（注册、`POST /v1/events/batch`、幂等、拆批、退避、`device_revoked`）沿用 `docs/sync-api.md`，本文只写 collector 新增的四个接口，以及模拟后台的三个观察接口。

## 0. 边界

- collector 是**独立组件**：从 Codex 自己写到盘上的会话文件采集，不读插件信任状态、不要求 Codex 在运行。插件被停用、被 `codex plugin remove` 删除、Codex 关闭，都不影响它继续采集与上传。
- 身份仍是**匿名设备**：注册、`Authorization: Bearer ctrd_…`、`device_token` 的规则与 `docs/sync-api.md` 第 1–2 节完全相同。collector 不发送 ChatGPT 账号、邮箱或任何 Codex 凭证，也不与充值后台（`docs/recharge-api.md`）共享令牌。
- 本文的四个接口只做两件事：告诉 collector **后台要求它做什么**（第 2、3 节），以及让后台看到它**还在不在、积压多少**（第 4、5 节）。它们不承载事件正文。
- 后台日志不得记录事件正文、心跳正文与 `device_token`。

## 1. 通用

- Base URL 与注册流程同 `docs/sync-api.md` 第 1 节；**未配置地址时 collector 不上传**，采集内容留在本机 spool（设计第 5 节）。非本机地址必须是 `https://`。
- 请求与响应都是 `application/json; charset=utf-8`；请求头 `X-Ctr-Client: codex-companion-collector/<版本>`。
- 四个接口都要求 `Authorization: Bearer ctrd_…`：令牌来自 `POST /v1/devices`（`docs/sync-api.md` 第 2 节），由 collector 自己保存（DPAPI），与插件无关。
- 错误统一格式：`{"error": {"code": "<错误码>"}}`。
- 客户端不跟随重定向；后台不要对这些接口返回 3xx（POST 收到 3xx 视为「结果未知」，同 `docs/recharge-api.md` 第 7 节）。
- 单次请求：连接、发送、等待响应各 15 秒，不是整个请求的总时限。

| 状态 | code | 客户端行为 |
|---|---|---|
| 401 | `unauthorized` | 令牌无效：丢弃令牌（含 DPAPI 文件），下一轮重新注册，得到新的 `device_id`；本机数据与 spool 不动 |
| 400 | `invalid_request` | 请求体不是 JSON 对象：记录错误码，放弃这一次请求，不重试（下个周期重来） |
| 403 | `device_revoked` | 设备已不存在（卸载上报过或被后台撤销）：**停止上传**，本机数据全部保留，状态显示「后台已停用此设备」，不再重试、不再注册 |
| 413 | `too_large` | 心跳正文超过 4 KiB：裁剪正文后在下个周期重发，不原样重试 |
| 429 | `rate_limited` | 按 `Retry-After`（秒）等待，客户端把等待限制在 2–300 秒 |
| 5xx | 任意 | 指数退避重试（2 秒起，上限 300 秒） |

注：`docs/sync-api.md` 的接口在 401 时返回 `device_invalid`；**只有本文这四个接口用 `unauthorized`**。两者的客户端处理相同（丢弃令牌、重新注册），分开只是为了让后台日志能区分是哪个组件报的错。

## 2. 服务状态 `GET /v1/service-state`

```
GET /v1/service-state
Authorization: Bearer ctrd_…
200
```

```json
{"state": "active",
 "terms_version": 4,
 "terms_hash": "sha256:6ea54c784400719760989aa1be91466812b2ea4cb7fe0540950251f5ae003b4e",
 "effective_at": "2026-11-01T10:00:00Z",
 "grace_hours": 72,
 "terminated_at": null,
 "server_time": "2026-11-01T10:00:00Z"}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `state` | `active` / `terminated` | 后台对该设备的结论。`terminated` 只能由第 3 节或后台管理端产生，客户端不能自己改回 `active` |
| `terms_version` | 整数 | 当前生效的服务条款版本；写入本机 `contract.json` 作为合同留痕（设计第 2 节） |
| `terms_hash` | 字符串 | `sha256:` + 64 位小写十六进制，条款文本的摘要；与本机 `contract.json` 记录的不同即为条款变更，按新版更新留痕 |
| `effective_at` | RFC3339 UTC | 这一版条款的生效时间 |
| `grace_hours` | 整数 | 离线宽限小时数，见第 6 节 |
| `terminated_at` | RFC3339 UTC / `null` | 终止时间；`state` 为 `active` 时必须为 `null` |
| `server_time` | RFC3339 UTC | 后台当前时间，用来排查与本机的时钟偏差；不用于覆盖本机时间 |

- 幂等、无副作用、可重复调用。
- `state` 为 `terminated` 时仍然返回 `200`（终止不是错误，是正常结论）。
- 未知字段忽略；`state` 出现本文没定义的取值时，客户端保持上一次已知状态并记录错误码，**不得**据此改变采集行为（避免新枚举值误停用户的采集）。

## 3. 终止服务 `POST /v1/service-state/terminate`

面板「停止记录」对应的调用：用户要求停止记录。

```
POST /v1/service-state/terminate
Authorization: Bearer ctrd_…
Content-Length: 0          ← 也可以发 {}，正文一律忽略
200
```

```json
{"state": "terminated", "terminated_at": "2026-11-01T10:00:00Z"}
```

- **幂等（必须）**：以设备为键。已经终止的设备再次调用返回**第一次**的 `terminated_at`，不刷新时间、不新建记录。客户端在超时、连接中断后会原样重发，依赖这一点。
- 终止之后 `GET /v1/service-state` 返回 `state: "terminated"` 和同一个 `terminated_at`。
- 终止**不删除任何数据**：后台已收到的事件保留，本机 spool 与本地库保留；删除走 `docs/sync-api.md` 第 5.1 节，由用户另选。
- 后台可以额外拒绝已终止设备的事件上传；模拟后台**故意不拒绝**，这样客户端没有按时停下来时，能在 `/_mock/events` 里直接看到（见第 7 节）。
- 终止是按设备记录的：一个 `device_token` 对应一台设备；要停掉同一用户的多台设备，逐个终止。

## 4. 心跳 `POST /v1/device/heartbeat`

```
POST /v1/device/heartbeat
Authorization: Bearer ctrd_…
Content-Type: application/json

{"client": "codex-companion-collector/1.0.0",
 "queue_depth": 12,
 "uploaded_total": 340,
 "tiers": {"L0": 120, "L1": 80, "L2": 140, "L3": 0},
 "service_state": "active"}
200
{"ok": true, "server_time": "2026-11-01T10:00:00Z"}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `client` | 字符串 | collector 版本，必填，用于确认线上跑的是哪一版 |
| `queue_depth` | 整数 | 当前待上传条数（spool 中未确认的事件）；验收看它的 P95（设计第 10 节：≤ 500 条） |
| `uploaded_total` | 整数 | 本设备累计已确认上传的条数（单调递增，随进程重启不清零） |
| `tiers` | 对象 | 各级条数的快照，键为 `L0`–`L3`；`L3` 为完整事件（默认与 L2 一样上传，同一事件上取代 L2；设计第 4 节） |
| `service_state` | `active` / `terminated` | 客户端当前认为的服务状态；与后台不一致说明客户端还没读到第 3 节的结论，是排查用的信号 |

- 正文上限 **4 KiB**；超过返回 `413 too_large`（与事件批次 4 MiB 的 `batch_too_large` 区分开）。
- 后台每个设备只保留**最新一条**心跳，覆盖不追加：心跳是观测值，不是账本。
- 缺失或类型不对的字段后台忽略，不因此返回 4xx：心跳不该打断采集。正文不是 JSON 对象时返回 `400 invalid_request`。
- 同一周期内重复发送没有副作用；**已经成功的心跳不重试**，下个周期重发即可（新心跳自带完整的 `queue_depth`）。

## 5. 卸载上报 `POST /v1/device/uninstall`

```
POST /v1/device/uninstall
Authorization: Bearer ctrd_…
200
{"ok": true}
```

- 语义是 **device_left**：设备**彻底不存在了**。除本节自身的重复调用外，此后该令牌的任何请求返回 `403 device_revoked`（事件上传见 `docs/sync-api.md`，本文四个接口相同），客户端据此停下。
- **可重复调用**：第二次及以后同样返回 `200 {"ok": true}`，`uninstalled_at` 保持第一次的时间（模拟后台在 `/_mock/uninstalls` 里给出上报次数）。
- 不可恢复：重装后 collector 重新注册，得到新的 `device_id`；旧设备的令牌不再可用，也不该复用。
- 调用顺序：用户选择「删除后台数据」时先 `DELETE /v1/devices/me/data`，再 `POST /v1/device/uninstall`；否则直接上报。离线时两者排队，联网后**先于任何上传**执行（同 `docs/sync-api.md` 第 5 节）。
- 卸载脚本最多重试 3 次（间隔 2、5、10 秒）；仍失败就照常清理本机数据并结束——用户已经卸载，不能因为网络把卸载卡住，后台没收到 beacon 只影响统计。

## 6. 客户端约定

| 项 | 约定 |
|---|---|
| 心跳 | 每 **10 分钟** 一次 `POST /v1/device/heartbeat`。采集进程启动 1 分钟后首发，进程重启立即补发一次；失败只等下一个周期 |
| 服务状态 | 每 **1 小时** `GET /v1/service-state`；进程启动时、以及心跳连续失败 3 次后立刻再拉一次。这个接口是后台唯一能把「停止」告诉客户端的地方，不能只在启动时读一次 |
| 离线宽限 | 连续 **72 小时**（响应里的 `grace_hours`）没有一次成功的 `service-state` 响应 → 视为无法确认服务仍然有效：**停止上传**（事件留在 spool，本机继续采集）；下一次成功读到 `state: "active"` 立即恢复上传 |
| terminate | `POST /v1/service-state/terminate` 就是面板上的「停止记录」，是**用户的终止决定**。客户端在 **5 分钟内**停止采集循环，并**完全停止上传**（spool 里剩下的也不发）；本机数据保留，只有用户主动选择时才删除 |
| `403 device_revoked` | 停止上传、保留本机数据、状态显示「后台已停用此设备」；不再重试，不再注册 |
| 卸载 | 上报 `POST /v1/device/uninstall`，然后停止采集、停止上传、清理本机数据（保留范围见设计第 7 节） |

「停」和「删」是两件事：终止与卸载都**不删除**本机已记录的内容，删除只在用户明确选择时发生。

## 7. 模拟后台

`scripts/mock_sync_server.py` 与 `docs/sync-api.md` 第 7 节是同一个进程、同一个端口（默认 18788，`--store` 落盘），本文四个接口与下面的观察接口都在其中：

| 接口 | 作用 |
|---|---|
| `GET /v1/service-state` | 见第 2 节；未知或缺失令牌返回 `401 unauthorized` |
| `POST /v1/service-state/terminate` | 见第 3 节；重复调用返回同一个 `terminated_at` |
| `POST /v1/device/heartbeat` | 见第 4 节；> 4 KiB 返回 `413 too_large` |
| `POST /v1/device/uninstall` | 见第 5 节；之后该设备的所有接口返回 `403 device_revoked`（事件上传也是），重复调用仍 `200` |

模拟后台固定值：`terms_version` 4、`terms_hash` 为条款文本的 SHA-256（`sha256:6ea54c784400719760989aa1be91466812b2ea4cb7fe0540950251f5ae003b4e`）、`effective_at` `2026-11-01T10:00:00Z`、`grace_hours` 72。终止按设备记录；`--store` 落盘时服务状态与心跳一起保存，重启后仍然有效。

观察接口（仅模拟后台）：

| 接口 | 返回 |
|---|---|
| `GET /_mock/service-state` | `{"service_state": {"<device_id>": {"state", "terms_version", "terminated_at", "updated_at", "uninstalled_at"}}}`：每台设备的服务状态与终止时间；`updated_at` 是最后一次写入时间，从未变过则为 `null` |
| `GET /_mock/heartbeats` | `{"heartbeats": {"<device_id>": {"received_at", "count", "bytes", "heartbeat": {…原始心跳…}}}}`：每台设备最新一条心跳，`count` 为收到的条数 |
| `GET /_mock/uninstalls` | `{"uninstalls": [{"device_id", "uninstalled_at", "calls"}]}`：已标记为「不存在」的设备，`calls` 为上报次数（验证幂等用） |

`POST /_mock/fail-next {"status": 503, "count": 1, "path": "/v1/service-state"}` 对本文四个接口同样生效（429 带 `Retry-After`），用来验证第 1 节的重试规则与第 6 节的离线宽限。

`POST /_mock/revoke-all` 只把设备标成「撤销」（之后一律 403 `device_revoked`），**不改** `service-state`：撤销是后台的管理动作，终止是用户的决定，两件事分开记录。
