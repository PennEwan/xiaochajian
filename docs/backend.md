# 采集后台 ctrsrv 运维手册

- 日期：2026-10-30；状态：**已实现**。代码在 `server/ctrsrv/`，标准库 + SQLite，单个进程，无第三方依赖；`ctrsrv.__version__ == "1.0.0"`。
- 客户端契约：`docs/sync-api.md`（记录器、原生插件）、`docs/collector-api.md`（collector 的四个接口）、`docs/recharge-api.md`（CDK 充值）。
- 运营模型：`docs/superpowers/specs/2026-11-01-collector-separation-design.md` §6。
- 部署文件：`server/ctrsrv.service`（systemd）、`server/serve.cmd`（Windows）、`server/nginx-ctrsrv.conf`（TLS 反代）。
- 本文的命令和「期望」输出都在本仓库的 Windows 开发机上真跑过；`device_id`、`device_token`、时间戳每次不同，格式一致。

## §0 这是什么

`ctrsrv` 是一台机器上的一个 Python 进程加一个 SQLite 文件，收两类东西。**事件**：记录器插件（`ctr.event/v2`）、原生插件（`csr.event/v1`）、采集组件 collector（`comp.event/v1`、`comp.session/v1`）用 `POST /v1/events/batch` 上传的批次，按 `(device_id, event_id)` 幂等写入，`tier` 为 `L0`–`L3`。**设备与服务状态**：匿名设备注册（`POST /v1/devices`，只回一个 `ctrd_…` 令牌）、服务条款版本（`GET /v1/service-state`）、用户终止（`POST /v1/service-state/terminate`）、心跳（`POST /v1/device/heartbeat`）、卸载上报（`POST /v1/device/uninstall`），外加充值后台的兑换记录（`POST /v1/redemptions`、`GET /v1/redemptions/{id}`）。它**故意不做**的事：不建账号、不碰 ChatGPT 凭证、不解密也不改写事件正文（原样存进 `events.body`）、不向客户端推任何指令（除了 `service-state` 里的 `state`）、不代替人工处理数据主体的删除申诉（只提供接口）、不履约充值（只记账，实际开通在别处）、也不自己结束 TLS——它说的是明文 HTTP。

## §1 五分钟跑起来

### 1.1 建库、加一个 CDK

```powershell
cd D:\codex-task-recorder
$env:PYTHONPATH = "$PWD\server"
$db = Join-Path $env:TEMP "ctrsrv-demo\ctrsrv.sqlite3"

python -m ctrsrv --db $db initdb
python -m ctrsrv --db $db add-cdk DEMO-AAAA-BBBB-CCCC --product "ChatGPT Plus 1 个月"
```

期望（实测）：

```json
{
  "ok": true,
  "db": "C:\\Users\\admin\\AppData\\Local\\Temp\\1\\ctrsrv-demo\\ctrsrv.sqlite3",
  "terms_version": 4,
  "terms_hash": "sha256:6ea54c784400719760989aa1be91466812b2ea4cb7fe0540950251f5ae003b4e"
}
{"ok": true, "added": 1}
```

`initdb` 只做一件事：按 `store.py` 的 `_SCHEMA` 建表（`CREATE TABLE IF NOT EXISTS`），并打印条款版本与哈希。重复执行无害。

**`--db` 必须写在子命令之前**。`python -m ctrsrv serve --db x` 会直接报错退出（`--db` 是顶层参数）：

```
usage: ctrsrv [-h] [--db DB]
              {serve,initdb,add-cdk,stats,devices,events,terminate,resume,purge,prune}
              ...
ctrsrv: error: unrecognized arguments: --db foo.sqlite3
```

### 1.2 起服务（第二个窗口，别关）

两条等价的路子，任选一条：

```powershell
# (a) 全用命令行参数
python -m ctrsrv --db $db serve --host 127.0.0.1 --port 8080 --admin-token demo-admin-token

# (b) 用环境变量（部署时推荐：令牌不出现在命令行里）
$env:CTRSRV_ADMIN_TOKEN = "demo-admin-token"
python -m ctrsrv --db $db serve
```

期望（实测，(a) 的横幅）：

```
ctrsrv 1.0.0 listening on http://127.0.0.1:8080  db=C:\Users\admin\AppData\Local\Temp\1\ctrsrv-demo\ctrsrv.sqlite3
admin: enabled
```

配置规则：`serve` 从 `CTRSRV_HOST`、`CTRSRV_PORT`、`CTRSRV_ADMIN_TOKEN` 读默认值，**显式传的 `--host`/`--port`/`--admin-token` 覆盖环境变量**（实测：环境里 `CTRSRV_PORT=18099`，命令行 `--port 18097`，最终监听 18097）。没有令牌时第二行是 `admin: DISABLED (set CTRSRV_ADMIN_TOKEN)`，`/admin/*` 全部 `403 admin_disabled`。

一个例外：**`CTRSRV_DB` 从 CLI 起服务时不生效**。`--db` 的 argparse 默认值是字面量 `data/ctrsrv.sqlite3`，`cmd_serve` 总是把它交给 `Config(db=...)`，于是覆盖了环境变量那条路。实测：设 `CTRSRV_DB=<tmp>\fromenv.sqlite3` 后裸跑 `serve`，横幅打的是 `db=data\ctrsrv.sqlite3`，`fromenv.sqlite3` 根本没建。库要放别处就老老实实传 `--db`（`server/ctrsrv.service` 就是这么写的）。

停服务按 `Ctrl+C`。

### 1.3 健康检查

```powershell
curl.exe -sS http://127.0.0.1:8080/v1/health
```

期望（实测）：

```json
{"ok": true, "version": "1.0.0", "terms_version": 4}
```

`/v1/health` 不要令牌，也不碰数据库，适合给负载均衡或 `curl` 探活。**用 `curl.exe`，不要用 `curl`**：Windows PowerShell 5.1 里 `curl` 是 `Invoke-WebRequest` 的别名。

### 1.4 一次完整往返：注册 → 传一批 → 读回来

```powershell
$reg = Join-Path $env:TEMP "ctrsrv-demo\register.json"
Set-Content $reg -Encoding utf8 -NoNewline -Value '{"client":"codex-companion-collector/1.0.0","platform":"windows","consent":{"version":4,"accepted_at":"2026-11-01T09:00:00Z"}}'

$me  = curl.exe -sS -X POST -H "Content-Type: application/json" --data-binary "@$reg" http://127.0.0.1:8080/v1/devices | ConvertFrom-Json
$tok = $me.device_token
$me | ConvertTo-Json -Compress
```

期望（实测，`device_id`/`device_token` 每次都是新的）：

```json
{"device_id":"dev_2d61051f53c37212","device_token":"ctrd_nNIA9MP51HZznv0xBnUm-8XNdxGHooKvLvuFicIbF98","created_at":"2026-10-30T09:42:23Z"}
```

`ConvertTo-Json` 会去掉空格；线上原文是 `{"device_id": "dev_…", "device_token": "ctrd_…", "created_at": "…"}`。令牌只在这一刻出现一次，客户端自己保存（DPAPI），后台只留 SHA-256。

```powershell
$bat = Join-Path $env:TEMP "ctrsrv-demo\batch.json"
@'
{"schema":"comp.sync.batch/v1","batch_id":"b-demo-1","tier":"L2","events":[
 {"schema":"comp.event/v1","event_id":"evt-0001","seq":1,"session_id":"sess-demo","tier":"L2","kind":"user_prompt","ts":"2026-11-01T10:00:01Z","data":{"text":"hello"}},
 {"schema":"comp.event/v1","event_id":"evt-0002","seq":2,"session_id":"sess-demo","tier":"L3","kind":"tool_result","ts":"2026-11-01T10:00:02Z","data":{"ok":true}}]}
'@ | Set-Content $bat -Encoding utf8 -NoNewline

curl.exe -sS -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $tok" --data-binary "@$bat" http://127.0.0.1:8080/v1/events/batch
curl.exe -sS -H "Authorization: Bearer $tok" http://127.0.0.1:8080/v1/devices/me
```

期望（实测）：

```json
{"accepted": 2, "duplicates": 0, "rejected": 0, "results": [{"event_id": "evt-0001", "status": "accepted"}, {"event_id": "evt-0002", "status": "accepted"}]}
{"device_id": "dev_2d61051f53c37212", "event_count": 2, "session_count": 1, "last_event_at": "2026-11-01T10:00:02Z", "created_at": "2026-10-30T09:42:23Z", "tiers": {"L2": 1, "L3": 1}}
```

把同一批原样再发一次，幂等生效（实测）：

```json
{"accepted": 0, "duplicates": 2, "rejected": 0, "results": [{"event_id": "evt-0001", "status": "duplicate"}, {"event_id": "evt-0002", "status": "duplicate"}]}
```

在服务端确认数据真的落库（走 CLI，不经过 HTTP）：

```powershell
python -m ctrsrv --db $db events --limit 3
```

期望（实测）：

```
2026-10-30T09:42:23Z  dev_2d61051f53c37212  sess-demo  seq=1  L2 user_prompt          bytes=166
2026-10-30T09:42:23Z  dev_2d61051f53c37212  sess-demo  seq=2  L3 tool_result          bytes=161
```

### 1.5 两个 PowerShell 坑（都会让你以为后台坏了）

**坑一：内联 JSON 被吞引号。** PowerShell 5.1 把原生 exe 的参数重新拼一遍时不转义内部双引号，`-d '{"client":"x"}'` 到 `curl.exe` 手里已经没了引号：

```powershell
curl.exe -sS -X POST -H "Content-Type: application/json" -d '{"client":"x"}' http://127.0.0.1:8080/v1/devices
```

实测结果：

```json
{"error": {"code": "invalid_request", "message": "正文不是合法 JSON"}}
```

所以本文一律用 `--data-binary "@文件"`。文件带 UTF-8 BOM 也没关系：后台用 `utf-8-sig` 解码（实测 `Set-Content -Encoding utf8` 写出的 `0xEF 0xBB 0xBF` 开头正文被正常接收）。

**坑二：把 `serve` 放进管道会丢日志。** 日志走 stdout；`python … serve | Out-File` 会被 PowerShell 缓冲，看不实时。要么让它独占一个窗口，要么 `python … serve > serve.log 2>&1`（§5 的 systemd 用 journald）。

## §2 接口一览

路由完整表就是 `app.py` 的 `ROUTES`，按下表逐行对照。`ROUTES` 第四列（`needs_token`）在代码里**没人读**（`_dispatch` 取出来就丢掉，连参数名都写成 `_needs_token`）：鉴权完全由处理函数自己调 `self.auth()` / `self.auth_admin()` 决定，下表「鉴权」列是实际行为。

| 方法 | 路径 | 鉴权 | 成功 | 错误码（状态） |
|---|---|---|---|---|
| GET | `/v1/health` | 无 | `200 {"ok":true,"version":"1.0.0","terms_version":4}` | — |
| POST | `/v1/devices` | 无 | `201 {"device_id","device_token","created_at"}` | `400 invalid_request` |
| GET | `/v1/devices/me` | 设备 | `200 {"device_id","event_count","session_count","last_event_at","created_at","tiers"}` | `401 device_invalid`、`403 device_revoked` |
| DELETE | `/v1/devices/me/data` | 设备 | `200 {"ok":true,"deleted_events":N}` | `401 device_invalid`、`403 device_revoked` |
| DELETE | `/v1/devices/me` | 设备 | `200 {"revoked":true,"ok":true,"state":"terminated"}` | `401 device_invalid`、`403 device_revoked` |
| POST | `/v1/events/batch` | 设备 | `200 {"accepted","duplicates","rejected","results":[…]}` | `400 invalid_batch`、`401 device_invalid`、`403 device_revoked`、`413 batch_too_large`、`429 rate_limited` |
| GET | `/v1/service-state` | 设备 | `200 {"terms_version","terms_hash","grace_hours","state","terminated_at","effective_at","server_time"}` | `401 unauthorized`、`403 device_revoked` |
| POST | `/v1/service-state/terminate` | 设备 | `200 {"state":"terminated","terminated_at"}` | `401 unauthorized`、`403 device_revoked`（已卸载的设备连这个也拒） |
| POST | `/v1/device/heartbeat` | 设备 | `200 {"ok":true,"server_time"}` | `400 invalid_request`、`401 unauthorized`、`403 device_revoked`、`413 too_large` |
| POST | `/v1/device/uninstall` | 设备 | `200 {"ok":true}`（可重复调用，恒 200） | `401 unauthorized` |
| POST | `/v1/redemptions` | 无 | `201 {"redemption_id","status","replayed":false,"product","redeemed_at","message":null}`；同 `(cdk,account)` 重放为 `200 … "replayed":true` | `400 cdk_invalid`、`400 invalid_account`、`409 cdk_already_redeemed`、`410 cdk_expired` |
| GET | `/v1/redemptions/{id}` | 无 | `200`（同上，`replayed` 恒为 `false`） | `404 not_found` |
| GET | `/admin/stats` | 管理 | `200 {devices,active_devices,uninstalled_devices,events,sessions,tiers,kinds,redemptions,generated_at}` | `401 unauthorized`、`403 admin_disabled` |
| GET | `/admin/devices` | 管理 | `200 {"devices":[…设备行 + event_count/session_count]}` | 同上；`limit` 上限 1000、默认 200，`offset` 默认 0 |
| GET | `/admin/devices/{id}` | 管理 | `200 {"device":{…},"stats":{…}}` | `404 not_found` |
| DELETE | `/admin/devices/{id}` | 管理 | `200 {"ok":true,"deleted_events":N}`（不存在的 id 也是 `200` + `0`） | 只有鉴权错误 |
| POST | `/admin/devices/{id}/terminate` | 管理 | `200 {"state":"terminated","terminated_at"}` | — |
| POST | `/admin/devices/{id}/resume` | 管理 | `200 {"ok":true,"state":"active"}` | — |
| POST | `/admin/cdk` | 管理 | `201 {"ok":true}` | `400 invalid_request`（缺 `code`） |
| POST | `/admin/prune` | 管理 | `200 {"ok":true,"deleted_events":N}` | 同上；`days` 取 `body.days`，其次 `?days=`，默认 90，最小 1 |

### 2.1 鉴权怎么带

两种头，等价的同一个凭据：

```
Authorization: Bearer <token>        # 首选
X-Admin-Token: <token>               # 别名，运维方便
```

- 设备令牌（`ctrd_…`）走设备接口；管理令牌（`--admin-token` / `CTRSRV_ADMIN_TOKEN` 的值）走 `/admin/*`。
- `X-Admin-Token` 是**全局别名**，不只对 `/admin/*` 生效：`_bearer()` 先看 `Authorization`，没有再取这个头，然后当同一个 token 交给处理函数。实测：把设备令牌放进 `X-Admin-Token` 请求 `/v1/devices/me` 同样 200。它没有额外的信任含义，别在代理层把它当第二个门。
- 401 的错误码分两套：`sync-api.md` 的接口（注册后那些）回 `device_invalid`；`collector-api.md` 的四个接口（`/v1/service-state`、`/v1/service-state/terminate`、`/v1/device/heartbeat`、`/v1/device/uninstall`）回 `unauthorized`。这是 `COLLECTOR_PATHS` 常量决定的，客户端两种都当「丢令牌、重新注册」，分开只为日志好认。
- 管理端：令牌未配置（空）→ 任何 `/admin/*` 都 `403 admin_disabled`；配了但头不对 → `401 unauthorized`。

### 2.2 设备处于「已终止 / 已卸载」时的应答

`auth()` 里两条规则叠加，顺序是先看卸载、再看终止：

| 设备状态 | 还能用的接口 | 其它一律 |
|---|---|---|
| `terminated`（用户按了停止，或 `DELETE /v1/devices/me`，或管理端 terminate） | `GET /v1/service-state`（200，`state:"terminated"`）、重复 `POST /v1/service-state/terminate`（200，同一个 `terminated_at`）、重复 `POST /v1/device/uninstall`（200） | `403 device_revoked`「设备已终止记录」 |
| 已卸载（`uninstalled_at` 有值） | 只有重复 `POST /v1/device/uninstall`（200） | `403 device_revoked`「设备已卸载」，**连 `/v1/service-state` 也拒** |

实测（一台设备走完整矩阵）：

```
terminate                -> 200 {"state":"terminated","terminated_at":"2026-10-30T09:42:46Z"}
service-state            -> 200 {"state":"terminated","terminated_at":"2026-10-30T09:42:46Z", …}
terminate (again)        -> 200 同一个 terminated_at
events/batch             -> 403 {"code":"device_revoked","message":"设备已终止记录"}
devices/me               -> 403 同上
devices/me/data (DELETE) -> 403 同上     ← 见 §4.3：删数据必须在终止之前
device/heartbeat         -> 403 同上
device/uninstall         -> 200 {"ok":true}
device/uninstall (again) -> 200 {"ok":true}
service-state            -> 403 {"code":"device_revoked","message":"设备已卸载"}
```

要点：**终止的设备还能读到自己的结论**（这是 collector 学会停下来、并确认「后台已停用此设备」的唯一通道）；**卸载之后连结论都读不到**。`DELETE /v1/devices/me` 只是把设备标成 `terminated`（`terminated_reason='revoked'`），并不吊销令牌——令牌照样能打 `service-state`，但打不动任何别的接口。

### 2.3 通用约定

- 错误体统一为 `{"error":{"code":"…","message":"中文"}}`；`429` 额外带 `Retry-After: 5`。
- **不发任何 3xx**：客户端把 POST 上的 3xx 当「结果未知」。路径不匹配（含方法不对，比如 `POST /v1/health`）一律 `404 {"error":{"code":"not_found","message":"没有这个接口"}}`。
- 路径末尾的 `/` 会被抹掉（`/v1/health/` 等价 `/v1/health`）。查询串参数先写入 `query`，路径捕获组（`{id}`）再覆盖同名项。
- **请求必须带 `Content-Length`**。`_json_body()` 只读这个头，没有就当空正文：chunked 的 `POST /v1/events/batch` 实测得到 `400 invalid_batch "events 必须是数组"`，chunked 的 `POST /v1/devices` 实测**照样 201**、只是 `client`/`platform` 都是 `null`（多出一条空设备）。本仓库的客户端都带长度（Python `urllib` 传 `data=bytes` 会自动加，原生充值路径显式设了 `req.ContentLength`），但自建脚本要注意；反代那边的取舍见 §5.4。
- `/v1/redemptions` 与 `/admin/*` 之间的边界：兑换接口**没有**任何令牌（`recharge-api.md` §1），所以 nginx 那边要限速（§5.4）。

## §3 数据模型

一个 SQLite 文件，WAL，`synchronous=NORMAL`，一个连接 + 一把 `RLock`。六张表（DDL 就在 `store.py::_SCHEMA`）：

| 表 | 主键 | 内容 |
|---|---|---|
| `devices` | `device_id`（`dev_` + 16 hex） | `token_hash`（**唯一**）、`client`、`platform`、`created_at`、`last_seen_at`、`terms_version`、`terms_hash`、`terms_accepted_at`、`state`（`active`/`terminated`）、`terminated_at`、`terminated_reason`（`server`/`revoked`）、`uninstalled_at`、`uninstall_calls` |
| `events` | `(device_id, event_id)` | 一行一条事件。`schema`、`tier`、`session_id`、`seq`、`kind`、`ts`、`model`、`batch_id`、`received_at`，以及 `body`（整条事件的原始 JSON） |
| `batches` | `(device_id, batch_id)` | 运维台账：这批送了几条（`events`/`accepted`/`duplicates`/`rejected`）与到达时间。**同一个 `batch_id` 重发会累加**，不是覆盖 |
| `heartbeats` | `device_id` | 每台设备**最新一条**心跳：`received_at`、`count`（累计收到几次）、`body`（正文原样） |
| `redemptions` | `redemption_id`（`rdm_` + 24 hex）；唯一 `(cdk_hash, account)` | 兑换记录：`status`、`product`、`error`、`created_at`、`updated_at` |
| `cdk` | `code_hash` | CDK 清单：`product`、`expires_at`、`used_by`、`used_at`、`redemption_id` |

索引：`events(device_id, session_id, seq)`、`events(received_at)`、`events(device_id, tier)`。

三个必须记住的落点：

1. **`tier`（`L0`–`L3`）在 `events.tier` 列**，值来自事件正文的 `tier` 字段。后台**不校验**它：不在白名单里的值照收，缺失就是 `NULL`，统计里那个桶叫 `"?"`。批次级的 `tier` 只落在 `batches.tier`，不影响每条事件。
2. **幂等键是 `(device_id, event_id)`**，即 `events` 的主键，靠 `INSERT OR IGNORE` + `cur.rowcount` 区分 `accepted` / `duplicate`。同一 `event_id` 跨设备不冲突；`batch_id` 只是日志/台账，每次发送都该不同。
3. **设备令牌只存 SHA-256**：`token_hash = sha256(token)` 的 64 位小写十六进制，`token` 本身在 `create_device` 返回之后不再存在于任何地方——建库文件里搜不到明文（实测），`/admin/devices` 与 `devices --json` 也只给 `token_hash`。CDK 同理：存 `sha256(upper(strip(code)))`。

**所有“条数”都是记录数，不是逻辑事件数。** `stats.events`、`stats.tiers`、`stats.kinds`、`/v1/devices/me` 的 `event_count`/`tiers` 全都是 `SELECT COUNT(*)` 的行数。同一个逻辑事件如果以两个档位各存了一条（不同 `event_id`），就计两次。实测：同一会话同一个 `seq`、同一个 `kind` 的 `coverage_probe` 分别以 `L0` 与 `L3` 上传，`/admin/stats` 给出

```json
{"events":5, "sessions":3, "tiers":{"?":1,"L0":1,"L2":1,"L3":2},
 "kinds":{"coverage_probe":2, "user_prompt":1, "tool_result":1, "?":1}}
```

`coverage_probe: 2` 就是一个 prompt 的两条记录。做「收了多少条用户对话」这类报表时按 `tier` 去重或只看 `L2`/`L3`，别直接读 `kinds`。

服务状态是怎么算出来的：`GET /v1/service-state` 的 `state`/`terminated_at` 来自 `devices` 行；`terms_*`/`grace_hours` 来自 `terms.state_document()`；`effective_at` 取 `terms_accepted_at`，没有则 `created_at`。`POST /v1/service-state/terminate` 与 `DELETE /v1/devices/me` 都走 `terminate()`，时间戳用 `COALESCE`——**已经终止过的设备再终止一次，返回第一次的 `terminated_at`**（客户端依赖这个幂等）。恢复只有管理端能做：`POST /admin/devices/{id}/resume`（或 CLI `resume`）；它清 `state`/`terminated_at`/`terminated_reason`，**不清 `uninstalled_at`**（§4.4）。

## §4 运维

### 4.1 备份

**不要 `copy ctrsrv.sqlite3` 了事。** WAL 模式下最近的写入还在 `-wal` 里，主库文件可能只包含建表时的样子。实测：一个跑了 3 分钟的库，主文件 73 KB、`-wal` 766 KB，直接复制主文件再打开：

```
naive copy: {'devices': 0, 'events': 0, 'batches': 0, 'heartbeats': 0, 'cdk': 1, 'redemptions': 0}
```

同样的库用 SQLite 在线备份 API 复制的副本：`{'devices': 2, 'events': 0, 'batches': 3, 'heartbeats': 1, 'cdk': 3, 'redemptions': 1}` ——这才是真数据。

推荐两种做法：

```bash
# Linux（装 sqlite3 CLI），服务不用停，备份期间不阻塞写入
sqlite3 /var/lib/ctrsrv/ctrsrv.sqlite3 ".backup '/var/backups/ctrsrv/ctrsrv-$(date +%F).sqlite3'"
```

```powershell
# Windows，标准库自带 backup()，同样可以在服务运行时跑（实测：服务在线时备份，行数一致）
python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" `
  "$env:LOCALAPPDATA\ctrsrv\data\ctrsrv.sqlite3" "$env:USERPROFILE\backup\ctrsrv-2026-10-30.sqlite3"
```

彻底停服务之后，整组文件一起复制才安全：`ctrsrv.sqlite3` + `ctrsrv.sqlite3-wal`（`-shm` 可以不拷，SQLite 会自己重建）。想更干净就停服务后先 `PRAGMA wal_checkpoint(TRUNCATE);` 再拷。

### 4.2 保留期

```powershell
python -m ctrsrv --db $db prune --days 90        # CLI：没有下限保护
```

```powershell
# 管理接口：days 最小 1（0 会被夹到 1），默认 90
curl.exe -sS -X POST -H "X-Admin-Token: <admin-token>" -H "Content-Type: application/json" `
  --data-binary "@prune.json" http://127.0.0.1:8080/admin/prune      # 文件内容：{"days":90}
```

- 判据是 `events.received_at < now - days*86400`（UTC，字符串比较，格式 `%Y-%m-%dT%H:%M:%SZ`）。
- **只删 `events`**。实测（`prune --days 0`，删掉 5 条事件）：删前 `{devices:6, events:5, batches:4, heartbeats:0, cdk:1, redemptions:0}`，删后 `{devices:6, events:0, batches:4, heartbeats:0, cdk:1, redemptions:0}`。设备、批次台账、心跳、CDK 与兑换账都不会被保留期碰到。
- **CLI 的 `--days` 没有下限**：`prune --days 0` 等于清空所有事件，`--days -1` 的截断点落在未来，同样一次删光。管理接口那边 `max(1, days)` 兜住了（实测 `{"days":0}` → `deleted_events: 0`，一条没删）。给定时任务用固定正数。
- `prune` 不 `VACUUM`：文件不会变小，空页留着给后续写入复用。要回收磁盘得在维护窗口单独跑 `VACUUM`（需要额外的临时空间和写锁）。
- 建议策略：**每天 03:00 一次 `prune --days 90`**（保留期按合同/告知写）；每月一次 `VACUUM`。别在同一天既 `prune` 又 `VACUUM` 又备份。

### 4.3 数据主体请求

| 请求 | 怎么做 | 实际删了什么 | 保留了什么 |
|---|---|---|---|
| 「删掉我传到后台的数据，但设备还要用」 | 客户端调 `DELETE /v1/devices/me/data`（拿自己的设备令牌） | 只有 `events` 行；实测 `{"ok": true, "deleted_events": 2}`，重复调用 `deleted_events: 0` | `devices`、`batches`、`heartbeats` 都在，设备照旧可用（`/v1/devices/me` 仍 200，计数归零） |
| 「彻底删掉这台设备」（真正的删除请求） | `DELETE /admin/devices/{id}` | `events` + `batches` + `heartbeats` + `devices` 行 | `redemptions`/`cdk`**不动**：兑换账按 `account` 记，从来没有 `device_id` 字段，后台无法把一笔兑换归到某台设备。要按账号删充值记录得单独写 SQL |
| 一批设备一起删 | CLI 逐台 `purge`（`python -m ctrsrv --db $db purge <device_id>`） | 同上 | 同上 |
| 留痕 | 管理端 `GET /admin/devices/{id}` 里有 `terms_version`/`terms_hash`/`terms_accepted_at`/`uninstalled_at`/`uninstall_calls` | — | 删了就没了。需要留痕就先导出一份 JSON 再删 |

**顺序很重要：删数据必须在终止/卸载之前。** 设备一旦 `terminated`，`DELETE /v1/devices/me/data` 也回 `403 device_revoked`（§2.2 实测），只有管理端 `DELETE /admin/devices/{id}` 还能动它。客户端契约里的顺序（先 5.1 删数据、再 5.2 撤销；卸载时先删再 `uninstall`）刚好正确，但**不要**先点「停止记录」再去替用户删数据——那时已经没有删的通道了，只剩整台设备 purge。

`DELETE /admin/devices/{id}` 是核弹：事件、心跳、设备行全去，且**不可恢复**。执行前先 `GET /admin/devices/{id}` 存一份，确认 id（不存在的 id 也返回 `200 {"ok":true,"deleted_events":0}`，别以为删成功了）。

### 4.4 终止、恢复、卸载

```powershell
# 一台：走管理接口（等价 CLI `python -m ctrsrv --db $db terminate dev_xxx`）
curl.exe -sS -X POST -H "X-Admin-Token: <admin-token>" `
  http://127.0.0.1:8080/admin/devices/dev_2d61051f53c37212/terminate
# -> {"state": "terminated", "terminated_at": "2026-10-30T09:42:46Z"}
```

```powershell
# 全部：CLI 逐台来（实测可用，服务在线时也能跑：两个进程对一个 WAL 库并发读写没问题）
python -m ctrsrv --db $db devices --json | ForEach-Object {
  $d = $_ | ConvertFrom-Json
  python -m ctrsrv --db $db terminate $d.device_id
}
# 反过来恢复：
python -m ctrsrv --db $db devices --json | ForEach-Object {
  $d = $_ | ConvertFrom-Json
  python -m ctrsrv --db $db resume $d.device_id
}
```

终止是**按设备**的，没有「全局开关」：每台设备一个令牌，要停全部就得逐台终止（上面那个循环）。终止**不删数据**。

三个坑：

1. `terminate` 用 `COALESCE` 写时间与原因：一台曾被 `DELETE /v1/devices/me` 撤销过的设备（`terminated_reason='revoked'`）再被 `terminate`，返回的还是旧的 `terminated_at` 与 `revoked`，不会改成 `server`。想改原因只能直接改库。
2. `POST /v1/device/uninstall` 只写 `uninstalled_at` + `state=terminated`，`terminated_at` 仍是 `NULL`（所以卸载过的设备在管理端看起来是「terminated、无终止时间」，这是对的）。
3. **`resume` 不能撤销卸载。** `POST /admin/devices/{id}/resume` 只清 `state`/`terminated_*`，`uninstalled_at` 还在，实测恢复后 `GET /v1/devices/me` 依旧 `403 device_revoked`「设备已卸载」。卸载过的设备就是死了：重装会重新注册一个新 `device_id`，旧行只在统计里留个数（`uninstalled_devices`）。

### 4.5 日志

一行一个请求，格式是 `app.py::log_line` 写死的：

```
<RFC3339 UTC> <status 宽3> <耗时 宽6.1f>ms <路径>[ <备注>]
```

真实样本：

```
2026-10-30T09:42:23Z 200    0.3ms /v1/health
2026-10-30T09:42:23Z 201    0.0ms /v1/devices dev_2d61051f53c37212
2026-10-30T09:42:23Z 201    5.3ms /v1/devices
2026-10-30T09:43:17Z 400    0.3ms /v1/events/batch invalid_batch
2026-10-30T09:44:27Z 429    0.3ms /v1/events/batch rate_limited
2026-10-30T09:35:25Z 500    0.2ms /admin/devices ValueError
```

要点：

- 备注是**错误码**（`BadRequest` 的 `code`），500 一行给的是异常**类名**（`ValueError`），不是消息、不是 traceback。
- **有的请求出两行**：`/v1/devices`、`/v1/service-state/terminate`、`/v1/device/uninstall`、`/v1/redemptions` 的处理函数自己先 `log_line` 一次（备注是 `device_id` / `redemption_id`），随后 `_dispatch` 再记一次标准请求行——对这四个路径「一个请求一行」不成立。做统计时按「同一秒、同一路径」去重，或者只数没有备注的行。
- **没有令牌、没有正文、没有查询串**（`path` 取 `urlsplit().path`）：`?days=90` 不会出现，请求体一个字都不会出现。唯一能从日志看到的主体标识是 `/v1/redemptions/{id}` 路径里的 `redemption_id`（不含 CDK）。
- 日志走 stdout：systemd 下进 journald（`journalctl -u ctrsrv -f`），Windows 下由任务/服务管理器重定向（`server\serve.cmd` 不自带重定向）。没有轮转，长期部署要么交给 journald 的 `SystemMaxUse`，要么自己切。

### 4.6 CLI 速查（与 `--help` 一致）

```
usage: ctrsrv [-h] [--db DB]
              {serve,initdb,add-cdk,stats,devices,events,terminate,resume,purge,prune} ...
  --db DB       SQLite 路径（默认 data/ctrsrv.sqlite3）   ← 顶层参数，写在子命令前
```

| 子命令 | 参数 | 作用 |
|---|---|---|
| `serve` | `--host`（默认 `CTRSRV_HOST` 或 127.0.0.1）、`--port`（默认 `CTRSRV_PORT` 或 8080）、`--admin-token`（默认 `CTRSRV_ADMIN_TOKEN`；都没有则 `/admin` 全部 403） | 启动 HTTP 服务 |
| `initdb` | — | 建库并打印条款版本与哈希 |
| `add-cdk` | `codes…`、`--product`、`--expires` | 导入 CDK（可一次多个） |
| `stats` | — | 总量统计（JSON） |
| `devices` | `--limit`（200）、`--offset`（0）、`--json` | 设备列表 |
| `events` | `--device`、`--limit`（100）、`--offset`（0）、`--raw` | 事件列表（默认不打印正文） |
| `terminate` / `resume` / `purge` | `device` | 对一台设备终止 / 恢复 / 彻底删除 |
| `prune` | `--days`（90） | 按保留期删除事件 |

`serve --help` 的原文（实测）：

```
usage: ctrsrv serve [-h] [--host HOST] [--port PORT]
                    [--admin-token ADMIN_TOKEN]
  --host HOST           默认 CTRSRV_HOST 或 127.0.0.1
  --port PORT           默认 CTRSRV_PORT 或 8080
  --admin-token ADMIN_TOKEN
                        默认 CTRSRV_ADMIN_TOKEN；都没有则 /admin 全部 403 admin_disabled
```

## §5 部署

前提：Python ≥ 3.10，仓库里的 `server/` 目录（含 `ctrsrv/` 包），一个可写目录放库文件。`python -m ctrsrv` 需要 `PYTHONPATH` 指向 `server/`（或 `cd server` 后执行）。

### 5.1 配置入口：环境变量第一

| 变量 | 默认 | 说明 |
|---|---|---|
| `CTRSRV_HOST` | `127.0.0.1` | 监听地址。**不要改成 `0.0.0.0`**——见本节末尾 |
| `CTRSRV_PORT` | `8080` | 监听端口 |
| `CTRSRV_ADMIN_TOKEN` | 无 | 管理令牌。为空 = `/admin/*` 全 403 |
| `CTRSRV_DB` | `data/ctrsrv.sqlite3` | **只有直接构造 `Config()` 时生效**；走 CLI 请用 `--db`（§1.2） |

- 同名命令行参数（`--host`/`--port`/`--admin-token`）在**显式传入时覆盖环境变量**，不传就保留环境的值。
- 令牌放环境变量而不是命令行：命令行会出现在 `ps aux`、`systemctl status` 的 `ExecStart`、Windows 的任务管理器/`wmic process` 里。实测 `server\serve.cmd` 起出来的进程命令行就是 `python -m ctrsrv serve --host 127.0.0.1 --port 8080`，令牌不在里面。
- 运行期没有「重新读环境变量」：改完变量要重启进程；`deploy` 时用 `EnvironmentFile` 或服务管理器的环境设置，别改用户级变量。

**这个后台只会明文 HTTP。** 它没有 TLS、没有 HSTS、没有证书管理；`--host 0.0.0.0` 直接把 `ctrd_…` 令牌、事件正文和管理令牌暴露在链路上。**永远只监听 `127.0.0.1`，公网入口一律走 §5.4 的 nginx**（客户端契约本身也要求非本机地址是 `https://`）。

### 5.2 Linux：systemd（推荐）

文件：`server/ctrsrv.service`。装法（路径按需改）：

```bash
sudo useradd --system --home /var/lib/ctrsrv --shell /usr/sbin/nologin ctrsrv
sudo install -d -o ctrsrv -g ctrsrv /opt/ctrsrv
sudo cp -r server docs /opt/ctrsrv/
sudo -u ctrsrv env PYTHONPATH=/opt/ctrsrv/server /usr/bin/python3 \
     -m ctrsrv --db /var/lib/ctrsrv/ctrsrv.sqlite3 initdb
sudo install -d -m 0750 -o root -g ctrsrv /etc/ctrsrv
printf 'CTRSRV_ADMIN_TOKEN=%s\n' "$(head -c 32 /dev/urandom | base64)" \
     | sudo tee /etc/ctrsrv/admin.env >/dev/null && sudo chmod 0640 /etc/ctrsrv/admin.env
sudo cp server/ctrsrv.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ctrsrv
systemctl status ctrsrv --no-pager && journalctl -u ctrsrv -n 20 --no-pager
```

单元里三处要你确认：

- `ExecStart=/opt/ctrsrv/.venv/bin/python …` —— 解释器绝对路径占位，改成你机器上的 `python3`（无第三方依赖，venv 可选）。
- `Environment=PYTHONPATH=/opt/ctrsrv/server` —— 没有它 `python -m ctrsrv` 直接 `No module named ctrsrv`。
- `Environment=CTRSRV_HOST=127.0.0.1` / `CTRSRV_PORT=8080` + `EnvironmentFile=-/etc/ctrsrv/admin.env` —— 地址、端口、令牌全走环境，`ExecStart` 里只有 `--db /var/lib/ctrsrv/ctrsrv.sqlite3`（`CTRSRV_DB` 从 CLI 不生效，见 §5.1）。把令牌写在 `ExecStart` 上会让它出现在 `ps` 和 `systemctl status` 里，别那么干。

`StateDirectory=ctrsrv` 让 systemd 建 `/var/lib/ctrsrv` 并交给 `ctrsrv` 用户；配 `ProtectSystem=strict` 时这是**唯一**可写路径，所以 `--db` 必须落在这下面，否则启动即失败。`Restart=always` + `RestartSec=3`：进程被杀、端口被占、库被锁，都会自己重来。`SIGTERM` 直接终止进程，`serve()` 里的 `finally`（关库）不会执行——WAL 模式下这是安全的，下次打开时 SQLite 自动重放，不用手工修库。

### 5.3 Windows：启动时跑的调度任务（或 nssm 服务）

文件：`server\serve.cmd`（纯 ASCII；`cmd.exe` 按 OEM 代码页解析批处理，混进一个非 ASCII 字节会让它后面的每一行解析错位）。它做三件事：把 `PYTHONPATH` 指到脚本自己所在目录、`cd /d %LOCALAPPDATA%\ctrsrv`（`--db` 默认是相对路径 `data\ctrsrv.sqlite3`，这样库不会落进仓库）、然后跑

```
python -m ctrsrv serve --host 127.0.0.1 --port 8080
```

它**不传** `--admin-token`，令牌从环境变量来。真实输出（实测，`cmd /c server\serve.cmd`，环境里只有 `CTRSRV_ADMIN_TOKEN`）：

```
[ctrsrv] PYTHONPATH=D:\codex-task-recorder\server
[ctrsrv] cwd=C:\Users\admin\AppData\Local\ctrsrv
ctrsrv 1.0.0 listening on http://127.0.0.1:8080  db=data\ctrsrv.sqlite3
admin: enabled
```

配合的验证：`/v1/health` 200；`X-Admin-Token: <env 里的令牌>` 与 `Authorization: Bearer <同一个>` 都是 200；库落在 `%LOCALAPPDATA%\ctrsrv\data\`；进程命令行里没有令牌。

**调度任务（开机启动）**——本机实测可建可删（任务建完立刻删了，从未运行）：

```cmd
schtasks /create /tn ctrsrv /tr "cmd.exe /c D:\codex-task-recorder\server\serve.cmd" ^
         /sc onstart /ru "%USERDOMAIN%\%USERNAME%" /f
schtasks /run /tn ctrsrv
schtasks /query /tn ctrsrv /v /fo list
schtasks /delete /tn ctrsrv /f
```

注意：`/sc onstart` 的任务默认以**登录用户**身份在开机时起，域策略或「只在用户登录时运行」的机器上不会真的提前起来；要更可靠就把任务设成「不管用户是否登录都运行 + 最高权限」（`/rl highest`，通常需要管理员）。这个任务也没有自动重启：进程崩了任务就结束了。

**nssm（当成真正的 Windows 服务，需管理员）**：

```cmd
nssm install ctrsrv "cmd.exe" "/c D:\codex-task-recorder\server\serve.cmd"
nssm set ctrsrv AppDirectory "%LOCALAPPDATA%\ctrsrv"
nssm set ctrsrv AppStdout "%LOCALAPPDATA%\ctrsrv\logs\serve.log"
nssm set ctrsrv AppStderr "%LOCALAPPDATA%\ctrsrv\logs\serve.err.log"
nssm set ctrsrv AppRotateFiles 1
nssm set ctrsrv AppExit Default Restart
nssm set ctrsrv AppEnvironmentExtra "CTRSRV_ADMIN_TOKEN=<32 位随机串>"
nssm start ctrsrv
```

本机没有 `nssm`，这一段没跑过；`schtasks` 那段跑过。无论哪种，都要保证 `%LOCALAPPDATA%` 是服务账户自己的（nssm 用 LocalSystem 时它是 `C:\Windows\system32\config\systemprofile\AppData\Local`），或者干脆在 `nssm set ctrsrv AppDirectory` 里换成固定目录并改 `serve.cmd`。

### 5.4 公网入口：nginx 上 TLS

文件：`server/nginx-ctrsrv.conf`（带注释的完整 server 块）。放进 `conf.d/` 后本文件已在 `http {}` 内，所以 `limit_req_zone` 可以直接写在里面：

```bash
sudo install -m 0644 server/nginx-ctrsrv.conf /etc/nginx/conf.d/ctrsrv.conf
sudo sed -i 's/ctrsrv\.example\.com/后台域名/g' /etc/nginx/conf.d/ctrsrv.conf
sudo nginx -t && sudo systemctl reload nginx
```

关键点（文件注释里也写了一遍）：

- `client_max_body_size 9m;` 比后台的 8 MiB 上限高 1 MiB：让超限由**后台**回 `413`（`batch_too_large`，心跳路径是 `too_large`），客户端认这个码会拆批；如果 nginx 先拦，客户端拿到的是 HTML 错误页，只能当暂时错误一直重试。
- `proxy_request_buffering off;`：一批 8 MiB 不要先落盘再转发，也让后台能凭 `Content-Length` 立刻回 413。**代价**：nginx 会把 `Transfer-Encoding: chunked` 原样转给后台，而后台只认 `Content-Length`（§2.3）。本仓库客户端都带长度，所以默认这么配；如果有第三方客户端会发 chunked，就改成 `proxy_request_buffering on`（nginx 缓冲完整批、带上 `Content-Length` 转发，代价是每批最多落盘 9 MB）。
- `proxy_redirect off;`：后台从不返回 3xx，别让 nginx 把 POST 变成 301（客户端会把它当「结果未知」）。
- `/admin/` 加内网 allowlist + `limit_req`：管理令牌能删数据、能停设备，一旦泄露就是全量破坏。
- `location = /v1/redemptions` 单独限速，因为兑换接口没有令牌（`recharge-api.md` §1），不限速就是给人撞 CDK 用。
- HTTP/2 那一行：nginx ≥ 1.25.1 用 `http2 on;`，更老的换成 `listen 443 ssl http2;`。

这台开发机没有 nginx，`nginx -t` 没跑过；文件是按官方指令语义手写的，上线前务必在目标机 `nginx -t`。

## §6 容量与限制

| 项 | 值 | 说明 |
|---|---|---|
| 进程模型 | `ThreadingHTTPServer` + 一个 `Store` 连接 + `RLock` | 并发只解决连接，**写入全部串行**：每一批在锁内逐条 `INSERT OR IGNORE`，提交一次 |
| 实测插入速度 | 120 批 × 200 条 = 24 000 条，1.43 秒（≈16 700 条/秒，本机 NVMe） | 直接调 `store.insert_events`，没走 HTTP；走 HTTP 的单批耗时实测 0.4–5.9 ms |
| 单条体积 | 实测 200 字节 payload → 约 660 B/条（含行开销与索引） | 按 1 万条/天估算 ≈ 6.6 MB/天，90 天 ≈ 600 MB |
| 传输上限 | 8 MiB（`MAX_BODY`） | 超了：普通路径 `413 batch_too_large`「正文超过上限」；`/v1/device/heartbeat` 路径 `413 too_large`「正文超过上限」 |
| 单条事件上限 | 1 MiB（`MAX_EVENT_BODY`） | 超了这条进 `results` 且 `status:"rejected"`、`error.code:"event_too_large"`，整批不失败 |
| 心跳正文 | 4 KiB | 4 KiB–8 MiB 之间由处理函数拦：`413 too_large`「心跳正文超过 4 KiB」（实测 5 KiB 的正文得到这个） |
| 单批条数 | 500（`len(events) > 500` → `400 invalid_batch`「每批最多 500 条」） | 客户端契约只要求 ≤200/批，后台收得更宽，但别依赖 |
| 请求分帧 | 必须 `Content-Length` | 不支持 `Transfer-Encoding: chunked`：chunked 正文被当空正文（§2.3 实测：事件批 400，注册接口会建出一条空设备） |
| 限速 | 每个 `device_id` 每 60 秒 **120 批**，进程内内存计数 | 实测第 121 批开始 `429` + `Retry-After: 5`；**进程重启即清零**，多进程部署等于限速乘 N |
| 计数口径 | `events`/`tiers`/`kinds` 都是**记录数** | 同一逻辑事件按档位存多条就计多次（§3 实测） |
| 库文件 | 单文件，WAL | 备份/迁移就是一个文件（WAL 在场时要按 §4.1 来） |

什么时候该换 Postgres：单文件 + 单写锁这套在**几百台设备、每天百万级事件**以内很够用；出现下面任一情况就动手迁移，不要硬撑：

- 设备数 > 500，或每天新增事件 > 200 万条（`prune` 开始跑不完、`events` 超过几十 GB）；
- 需要 2 个以上写进程（横向扩副本、`nginx` 后面挂两台）——限速器是进程内的，写锁也只在单进程内；
- 需要在线只读副本 / BI 直接连库 / 跨机房；
- 需要按时间分区、按账号直接删除（现在 `redemptions` 没有 `device_id`，只能写 SQL）。

日常盯三个数：

1. `python -m ctrsrv --db $db stats` 的 `events` 总量与增长斜率（涨得比设想的快，要么改保留期，要么换库）；
2. `prune` 的耗时（跨过 1 分钟就说明该缩短保留期、分表或迁移；SQLite 的 `DELETE` 是逐行的，越大越慢）；
3. 库文件 + `-wal` 的合计大小（`-wal` 长期偏大说明有长事务或读操作拖住了 checkpoint）。

## §7 安全

**令牌。**

- 设备令牌 `ctrd_` + `token_urlsafe(32)`（48 字符），只在 `POST /v1/devices` 的 201 响应里出现一次；服务端只存 `sha256(token)`，`devices.token_hash` 唯一。**丢了令牌 = 丢设备**：没有找回接口，重新注册会得到新 `device_id`。
- 令牌走 `Authorization: Bearer`，或同一个值的 `X-Admin-Token`（别名，全局生效，见 §2.1）。后台不校验 `X-Ctr-Client`、`X-Cdkr-Client` 这类客户端标识头，也不校验 `Origin`/`Referer`——它假设调用方是 CLI/采集进程，不是浏览器。
- 设备令牌能做的：读自己设备的统计、删自己的事件（终止前）、终止自己、上报心跳/卸载。**不能**读别的事件正文（没有「读取事件」的客户端接口，`/v1/devices/me` 只回计数），不能动别的设备。

**管理令牌。**

- 两种给法，值相同：`--admin-token`，或 `CTRSRV_ADMIN_TOKEN` 环境变量（命令行参数优先）。部署时**只走环境变量**：命令行上的密钥会进 `ps`、`systemctl status`、Windows 进程列表、shell 历史。`server/ctrsrv.service` 与 `server\serve.cmd` 都不把它放到命令行上。
- 未设置（空）时：**每一个 `/admin/*` 都回 `403 admin_disabled`**，包括 `/admin/stats`；不是 404，也不是「只读放行」。实测：只设 `X-Admin-Token` 而没有配置令牌 → `403 admin_disabled`。
- 设置了但请求头不对：`401 unauthorized`（实测两种头都试过）。
- 为什么 `terminate` / `prune` / `delete` 必须挂在管理令牌下：它们是不可逆的对外效果——`terminate` 会让对方的 collector 在 5 分钟内停采集并停止上传（用户侧可见的服务终止），`prune` 按天批量删事件，`DELETE /admin/devices/{id}` 把一台设备的一切抹掉。这些都是**运维的决定**，不能是任何一个持设备令牌的客户端能触发的动作；管理面还有个现实理由：它能看到全部设备、`token_hash`、兑换记录，泄露等于全量暴露。
- 管理面没有速率限制、没有 2FA、没有 IP 白名单（这层由 nginx 兜，见 §5.4）。管理令牌要 ≥32 字节随机（`head -c 32 /dev/urandom | base64`），只放在 `0640` 的文件或服务环境里。

**记什么、不记什么。**

- 日志只有：时间、状态码、耗时、路径、错误码（§4.5）。**没有** `Authorization` 头、没有 `X-Admin-Token`、没有令牌、没有请求体、没有事件正文、没有查询串、没有客户端 IP（要 IP 得靠 nginx 的 access log）。
- 库里**有**：事件正文（`events.body`，原样保存，这是产品功能）、**每台设备最新一条心跳正文**（`heartbeats.body`，含 `queue_depth`/`uploaded_total`/`tiers`/客户端版本）、`client`/`platform` 字符串、`terms_hash`、`token_hash`。心跳正文虽然不进日志，但它确实落库——对外说明数据范围时别说「后台只存计数」。
- 库文件权限：`UMask=0027`（systemd 单元里设），Linux 下 `ctrsrv` 用户独占；备份文件同等级保护，里面是全部原始会话。

**传输。**

- 明文 HTTP。公网必须 TLS（nginx，§5.4），客户端契约本身也要求非本机地址是 `https://`。后台不看 `X-Forwarded-Proto`，也没有任何「信任代理头」的逻辑：谁都能直连 `127.0.0.1:8080`，所以端口只绑回环。
- 令牌与 CDK 都不出现在 URL 里（CDK 只在 body，回显里只有 `redemption_id`）。

**已知缺口（不在这版修，部署时自己兜）。**

- `/v1/redemptions` 无令牌、无内建限速：CDK 撞库只能靠 nginx 限速 + CDK 本身足够长。
- `/v1/devices`（注册）无限制、且接受空正文：批量注册可以灌爆 `devices` 表。需要的话在 nginx 上对 `/v1/devices` 加 `limit_req`。
- 终止之后设备自己删不了数据（§4.3）：删除通道只剩管理端 purge，运维流程要按这个顺序设计。
- 管理端返回 `token_hash`：不是明文，但等于给每个令牌一个稳定指纹，别把 `/admin/*` 的响应贴到工单里。

## §8 与客户端的一致性

`GET /v1/service-state` 里的 `terms_version`（4）、`terms_hash`（`sha256:6ea54c78…b4e`）、`grace_hours`（72）不是后台自己编的：`terms.py` 在导入时**执行**采集组件的 `contract.py` 并把 `TERMS_VERSION`/`TERMS_TEXT`/`TERMS_HASH` 拿过来。查找顺序：

1. `<仓库根>/plugins/codex-companion/lib/collector/contract.py`（checkout / 打包产物里都在）；
2. `<server 的上一级>/collector/contract.py`（独立部署时的摆放位置）；
3. 都找不到才用 `terms.py` 里写死的常量（`TERMS_TEXT` + `sha256:` 摘要）。

推论：

- **克隆本仓库运行**：条款与客户端天然一致，不用管。
- **只拷 `server/` 单独部署**：`terms_hash` 用的是兜底常量。一旦有人在 `contract.py` 里改了条款文本（客户端会把它记进本机 `contract.json` 作为合同留痕），后台继续报旧哈希，两边就对不上了 —— 这时**必须**同步 `terms.py` 的常量，或者把 `collector/contract.py` 放到 `<server 的上一级>/collector/` 让它自动接管。
- 漂移有测试兜：`tests/test_server_core.py::test_terms_match_the_collector_client` 断言 `TERMS_VERSION`/`TERMS_HASH`/`GRACE_HOURS` 与 `collector.contract` 完全相等。它用 `pytest.importorskip("collector.contract")`，**`collector` 不在 `sys.path` 上时整个测试会 skip**（看着是绿的，其实什么都没验）。跑法：

```powershell
$env:PYTHONPATH="server;plugins\codex-companion\lib"; python -m pytest tests\test_server_core.py -q -k terms
# 1 passed
```

改条款的流程（顺带）：改 `contract.py` 的 `TERMS_TEXT` 并把 `TERMS_VERSION` 加 1 → 上面的测试会红 → 更新 `terms.py` 兜底常量（或确保 `contract.py` 可达）→ 重新跑测试 → 部署。客户端下次 `GET /v1/service-state` 看到新哈希就按新版留痕，不用改代码。

## §9 看数据：网页与命令行（`/admin/ui`、`ctrsrv view`）

后台没有前端构建、没有 JS，页面由服务端直接渲染（`server/ctrsrv/web.py`）。两个入口，看的是同一份数据。

### 9.1 网页

```powershell
python -m ctrsrv --db data\ctrsrv.sqlite3 serve --port 8080 --admin-token <令牌>
```

浏览器打开（**第一次要带 `?token=`，之后靠 cookie**）：

```
http://127.0.0.1:8080/admin/ui?token=<令牌>
```

| 页面 | 看到什么 |
|---|---|
| `/admin/ui` | 设备列表：状态、事件数、会话数、最近活跃、客户端版本、是否已卸载；顶部是总量与按档位分布 |
| `/admin/ui/devices/{device_id}` | 这台设备的会话列表：记录数、档位分布、时间范围、内容概览（点了进会话） |
| `/admin/ui/devices/{id}/sessions/{session_id}` | 会话时间线：到达时间 · 档位 · 类型 · seq · 内容摘要 · `JSON` 链接 |
| `/admin/ui/devices/{id}/events/{event_id}` | 单条记录的完整 JSON（格式化） |

- 鉴权与 `/admin/*` 相同：`Authorization: Bearer`、`X-Admin-Token`、`?token=`（首次命中后写 `ctrsrv_admin` cookie，`HttpOnly; SameSite=Strict`）、或该 cookie。
- 没带凭证 → `401 unauthorized`；路径里的 id 不对 → `404 not_found`。
- 事件 id 里含 `#`，页面里的链接已做 URL 编码（`%23`）。

命令行也能取同样的页面，方便放内网/贴给同事：

```powershell
curl.exe -s "http://127.0.0.1:8080/admin/ui?token=<令牌>" -o ui.html
```

### 9.2 命令行（推荐：看"对话本身"）

```powershell
python -m ctrsrv --db data\ctrsrv.sqlite3 view --latest
```

输出是按会话还原的文字流（同一逻辑事件在 L0/L1/L3 各有记录，这里只显示最完整的一份）：

```
会话 55555555-4444-3333-2222-111111111111（设备 dev_8962924e06fff836，31 条记录）

[10:14:35] L1 session_meta         {"session_id": "5555…", "cli_version": "0.163.0-alpha.10.2", …}
[10:14:35] L3 user_prompt          我这个登录接口报 401，帮我看看
[10:14:35] L1 tool_call            {"tool_name": "exec", "input": "run('pytest tests/test_auth.py -q')", …}
[10:14:35] L3 tool_result          Exit code: 0
ok
[10:14:35] L3 assistant_message    看 auth.py 第 42 行：token 过期没有刷新
```

| 命令 | 用途 |
|---|---|
| `view --latest` | 最近收到的那条事件所在的会话 |
| `view --device <id>` | 列出这台设备的全部会话（id、条数、档位、最后时间） |
| `view --device <id> --session <sid>` | 指定会话的完整时间线 |
| `view --latest --raw` | 原始 `comp.event/v1` JSON 行（给程序吃） |

`events --raw` 是"按记录"看（含 L0/L1/L2/L3 每一份），`view` 是"按对话"看（去重、只留最完整的一份）。两者读同一个库。

## 附录 A：实测与 `docs/*-api.md` 的差异

按代码实现写本文时发现的契约偏差。**没有改代码**，这里只记录，运维/客户端对接时按「实现」一列准备。（此前记录过的三条已在上游修掉，不再是偏差：`POST /v1/device/uninstall` 现在可重复 200；`DELETE /v1/devices/me` 之后设备一律 403；`X-Admin-Token` 别名与 `CTRSRV_*` 环境变量都已生效。）

| # | 接口 | 文档说 | 实现（实测） | 影响 |
|---|---|---|---|---|
| 1 | 所有 POST | 未提分帧 | 只认 `Content-Length`；chunked 正文被当空正文 | 事件批 `400 invalid_batch`；**注册接口会 201 建出一条 `client=null` 的空设备**。自建客户端必须带长度 |
| 2 | 配置 | — | `CTRSRV_DB` 从 CLI 起服务时不生效（`--db` 的 argparse 默认值覆盖了它） | 用 `--db`；别指望环境变量换库路径 |
| 3 | `GET /v1/devices/me` | 五个字段（`sync-api.md` §3） | 多一个 `tiers` 字段；`last_event_at` 取事件 `ts` 的最大值，都没有 `ts` 才退回 `received_at` | 兼容（客户端忽略未知字段） |
| 4 | `DELETE /v1/devices/me/data` | `200 {"deleted_events":N}`（`sync-api.md` §5.1） | `200 {"ok":true,"deleted_events":N}`；**设备终止后调用变 403** | 多一个字段是兼容的；403 会改变客户端「删除失败」的判断，先删再终止 |
| 5 | `POST /v1/events/batch` | 每批 ≤200 条、正文 ≤1 MiB、后台应至少接受 4 MiB（`sync-api.md` §4） | 收 ≤500 条、正文 ≤8 MiB、单条事件 ≤1 MiB | 更宽松，客户端不会因此失败 |
| 6 | 限速 | 文档只提 `429 rate_limited` + `Retry-After` | 每设备 120 批/60 秒，`Retry-After: 5` 固定 | 客户端按 2–300 秒退避，后台只让等 5 秒，可能连撞几次 |
| 7 | `GET /v1/health` | 未在契约中定义 | 返回 `{ok, version, terms_version}`，不含 `terms_hash` | 只适合探活，别拿它做条款校验 |
| 8 | 参数校验 | — | `/admin/devices?limit=abc` → `500 {"code":"server_error"}`（`int()` 抛 `ValueError`，被兜底异常吞掉） | 管理脚本传参前自己校验 |
| 9 | `prune` 下限 | — | 管理接口 `days` 夹到 ≥1；CLI `prune --days 0` 会清空全部事件 | 定时任务别用 0/负数 |
| 10 | 恢复语义 | `resume` 让设备回到 `active` | `resume` 只清 `state`/`terminated_*`，不清 `uninstalled_at` | 卸载过的设备恢复后仍是 403「设备已卸载」，只能重新注册 |

另外两处「文档没写但运维必须知道」的行为，正文里已经展开：`prune` 只删 `events`（不动 `batches`/`heartbeats`/`devices`/`redemptions`/`cdk`）；`DELETE /admin/devices/{id}` 也删不到 `redemptions`（那里没有 `device_id`）。
