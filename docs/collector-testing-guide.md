# 独立采集器（P5）测试指南

> 只想走一遍「装 → 用 → 看到数据上传」：看 **[collector-e2e-walkthrough.md](collector-e2e-walkthrough.md)**（8 步，15 分钟）。
> 本文是分层测试：从单测到安装包，每层都能独立跑。

从快到慢四层，命令都在仓库根目录执行；Windows + PowerShell。

```bat
set PYTHONPATH=plugins\codex-companion\lib;plugins\codex-task-recorder\lib;plugins\codex-cdk-recharge\lib;tools\session-reader;tests
set PYTHONIOENCODING=utf-8
```

---

## 第 0 层 · 五分钟版

三条命令，判断这套东西是不是活的：

```bat
python -m pytest tests\test_collector.py tests\test_collector_mock.py -q
python scripts\collector_acceptance.py
python -m pytest tests -q -k "setup or companion or sync"
```

期望：`41 passed` / `11/11 passed · ALL PASS` / `128 passed`。

---

## 第 1 层 · 单元与集成测试

| 命令 | 覆盖 | 期望 |
|---|---|---|
| `python -m pytest tests\test_collector.py -q` | 分级 L0–L3、spool 分档与确认、合同终止/恢复、reader 幂等、IPC 往返与错误令牌、任务 XML、遥测汇总 | `28 passed`/`41 passed`（随用例增长） |
| `python -m pytest tests\test_collector_mock.py -q` | 服务端四个接口：service-state、terminate 幂等、heartbeat 413、uninstall→403 | `10 passed` |
| `python -m pytest tests\test_sync_hostgate.py tests\test_sync_worker.py -q` | `gate` 解耦没有破坏原同步链路 | 全绿 |
| `python -m pytest tests -q` | 全量 | 仅 `tests/test_cdkr_*` 会失败（那是充值模块的在途改动，与本组件无关） |

> 只想跑本组件的全部用例：`python -m pytest tests -q -k "collector"`

---

## 第 2 层 · 端到端验收（11 项，最重要的一条）

```bat
python scripts\collector_acceptance.py
```

它自己起模拟后台、造 Codex rollout 文件、跑真实 CLI，验证三件事：

```
[PASS] backend accepted records
[PASS] all tiers delivered (L0/L1/L3)
[PASS] telemetry summary delivered
[PASS] full session content delivered (L3)
[PASS] spool drained (local queue empty once acknowledged)
[PASS] hostgate sees the plugin as disabled     ← 插件被禁用
[PASS] collector gate still allows recording    ← 采集器照采（核心）
[PASS] collector stop exits cleanly
[PASS] termination recorded in the contract
[PASS] next run refuses to record
[PASS] no further uploads after stop
```

**没有这 11 个 PASS，"采集与插件分离"就是没落地。**

---

## 第 3 层 · 本机联调（手工，看得到每一步）

### 3.1 起模拟后台

```bat
start python scripts\mock_sync_server.py --port 18788 --store %TEMP%\mock.json
```

### 3.2 准备一个数据目录

```bat
set D=%TEMP%\coll-demo
set COMPANION_DATA_DIR=%D%
set CODEX_HOME=%D%\codexhome
python -m collector --data-dir %D% contract --init
```

期望：打印 `terms_version: 4` 与 `terms_hash: sha256:6ea54c78…`。

### 3.3 让它认为服务是 active 的（真实环境由后台下发）

```bat
python -c "import json,pathlib,time,os; d=pathlib.Path(os.environ['COMPANION_DATA_DIR']); (d/'service-state.json').write_text(json.dumps({'state':'active','terms_version':4,'grace_hours':72,'fetched_ts':time.time()}),encoding='utf-8')"
python -c "import json,pathlib,os; d=pathlib.Path(os.environ['COMPANION_DATA_DIR']); (d/'collector-config.json').write_text(json.dumps({'base_url':'http://127.0.0.1:18788'}),encoding='utf-8')"
```

### 3.4 采一次，看队列

```bat
python -m collector scan --data-dir %D%
```

期望：

```
gate: active
{"records": 31, "scan": {... "files": 1, "new_events": 11 ...}, "spool": {"L0": 12, "L1": 8, "L2": 0, "L3": 11}}
```

`L2: 0` 是正常的——同一事件上 **L3 取代 L2**。

### 3.5 起常驻进程，看 IPC

```bat
start python -m collector run --data-dir %D%
timeout /t 3 >nul
python -m collector status --data-dir %D%
```

期望：`记录中 · 已采集 11 条 · 待上传 31 条`

```bat
python -m collector pause  --data-dir %D%   → {"ok": true, "paused": true, ...}
python -m collector resume --data-dir %D%   → {"ok": true, "paused": false, ...}
```

### 3.6 上传到模拟后台

```bat
python -m collector run --once --data-dir %D%
curl http://127.0.0.1:18788/_mock/events
```

期望：事件里能看到 `comp.event/v1`（tier L0/L1/L3）与 `comp.session/v1`；`status` 里 `待上传 0 条`。

### 3.7 停：真停

```bat
python -m collector stop --data-dir %D%
```

期望：返回里 `gate: contract_terminated`；常驻进程 **1 秒内**退出；再 `run --once` 直接返回不采不上传。

### 3.8 清理

```bat
python -m collector purge --data-dir %D%
rmdir /s /q %D%
```

---

## 第 4 层 · 打包版（`build\stage-*`，安装包真正会装的东西）

这一步验证"打出来的东西"而不是"源码"，会用到私有 Python 运行时。

```bat
set P=D:\codex-task-recorder\build\stage-1.5.2\marketplace\plugins\codex-companion
set D=D:\codex-task-recorder\build\coll-pkg-run
set COMPANION_DATA_DIR=%D%
set CODEX_HOME=%D%\codexhome
```

1. **打包运行时能导入全部依赖**
   ```bat
   "%P%\runtime\python.exe" -c "import sys; sys.path.insert(0,r'%P%\lib'); import sqlite3, collector, collector.run, csr.reader; print('ok', collector.__version__, sqlite3.sqlite_version)"
   ```
   期望：`ok 1.1.0 3.49.1`（sqlite3 必须能导入，`csr` 和 `collector` 都依赖它）。

2. **计划任务用的那个 launcher 能起来**
   ```bat
   start "" /min cmd /d /c "%P%\bin\collector.cmd"
   timeout /t 5 >nul
   "%P%\runtime\python.exe" -m collector --data-dir %D% status
   ```
   期望：`记录中 · 已采集 N 条 · 待上传 M 条`（我实测 `11 条 / 31 条`）。

3. **MCP 走的是同一个 launcher**
   ```bat
   echo {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}} | cmd /d /c "%P%\bin\mcp.cmd" mcp
   ```
   期望：一行 JSON，`"serverInfo":{"name":"codex-companion-collector","version":"1.1.0"}`。

4. **停**
   ```bat
   "%P%\runtime\python.exe" -m collector --data-dir %D% stop
   ```

> **环境坑**：如果 `%TEMP%` / `%LOCALAPPDATA%` 被 ACL 限制（本机沙箱会这样），上面第 2 步会以 `PermissionError` 失败。把 `TMP`/`TEMP`/`COMPANION_DATA_DIR` 指到仓库内的目录即可（如上）。这是环境问题，不是产品问题。

---

## 第 5 层 · 安装包

```bat
python packaging\build.py --version 1.5.2 --profile mock --out D:\codex-task-recorder\dist
```

产物：`dist\CodexCompanion-Setup-1.5.2.exe`（自带运行时，约 13 MB，按用户安装，不需要管理员）。

安装与验证：

| 步 | 操作 | 期望 |
|---|---|---|
| 1 | 双击安装包 | 向导里有一页「本产品为会话记录服务」，**只有一个"下一步"，没有勾选框** |
| 2 | 装完看任务 | `schtasks /query /tn CodexCompanion\Collector /v /fo list` 能看到，触发=登录时，运行级别=最低 |
| 3 | 看数据目录 | `%LOCALAPPDATA%\CodexCompanion\` 里有 `contract.json`（`terms_version: 4`）、`collector-config.json`、`collector.log` |
| 4 | 注销重登 | 采集器自动起来：`"%LOCALAPPDATA%\Programs\CodexCompanion\runtime\python.exe" -m collector status` → `记录中` |
| 5 | 卸载 | 「设置 → 应用」→ 卸载：先写 `terminated_at`、再停进程、再删计划任务，最后才删文件；`schtasks /query` 已无此项 |

> `--profile mock` 会写 `127.0.0.1` 的后台地址，只适合本机联调；正式包用 `--profile prod --recharge-url … --sync-url … --update-url …`。

---

## 第 6 层 · 在 Codex 里测 MCP 工具

装好插件、重启 Codex、在插件页「全部信任」后，直接对 Codex 说：

| 说 | 期望 |
|---|---|
| 「查一下后台采集状态」 | 调用 `collector_status`，返回 JSON：`collector_running: true`、`recording: true`、`reader.events`、`spool.records` |
| 「暂停任务记录」 | 调用 `collector_pause`，再查状态 `paused: true` |
| 「恢复任务记录」 | `collector_resume`，`paused: false` |
| 「彻底停止任务记录」 | `collector_stop`，`gate: contract_terminated` |
| 「打开助手包面板」 | `collector_open_panel`，弹出面板（「任务记录」一行显示当前状态） |

采集器没起来时调 `collector_pause`，应当返回 `isError: true` + 「后台采集器没在运行」+ 错误码 `no_server`。

---

## 第 7 层 · 三个必须亲眼确认的行为

1. **插件卸了还在采**：在 Codex 插件页禁用 / `codex plugin remove` 之后，`collector status` 仍是 `记录中`，`_mock/events` 继续增长。
2. **Codex 关了还在采**：完全退出 Codex App，再往 `%CODEX_HOME%\sessions\…\rollout-*.jsonl` 追加几行，`scan` 能读到新事件。
3. **停止是真停**：`collector stop` 之后 1 秒内进程退出、`_mock/stats` 不再增长、`run --once` 立即返回。

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `gate: no_service_state` | 没有 `service-state.json`，也没在 72 小时宽限内 | 起模拟后台跑一次 `run --once`，或手工写 active（3.3） |
| `gate: contract_terminated` | 面板停过 / 卸载过 | 面板「恢复记录」；`uninstall` 原因的重装会自动恢复 |
| `collector_running: false` 但任务在 | 采集器崩了或没到登录触发 | `collector task status`；`python -m collector run` 前台上跑看报错 |
| 上传一直 0 | `base_url` 没配 | `collector-config.json` 里写 `base_url` |
| 上传 `device_revoked` | 后台说设备没了 | 本机队列保留，不重发；需要重新注册就换数据目录 |
| `csr not found` | 源码运行时 `tools\session-reader` 不在 `PYTHONPATH` | 按第 0 层的 `set PYTHONPATH=…` 设好 |
| 打包版 `PermissionError` | `%TEMP%` 的 ACL 限制 | 把 `TMP`/`TEMP`/数据目录指到仓库内（第 4 层那段） |
| `pytest` 里 `test_cdkr_*` 全红 | 充值模块在途改动 | 与本组件无关；用 `-k "not cdkr"` 跳过 |
