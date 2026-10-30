# 独立采集器（P5）桌面手测清单

目的：在真实 Windows 桌面上确认「采集与插件分离」成立。每条都是可执行命令，做完打勾。
前置：仓库根目录、`python` 3.12 在 PATH；本机联调用 `python scripts\mock_sync_server.py`（默认 `127.0.0.1:18788`）。

```bat
set PYTHONPATH=plugins\codex-companion\lib;plugins\codex-task-recorder\lib;tools\session-reader;tests
set COMPANION_DATA_DIR=%TEMP%\coll-manual
set CODEX_HOME=%TEMP%\coll-manual\codexhome
```

## 1. 采样本机已有的 Codex 会话（不装插件、不开 Codex）

| 步 | 命令 | 期望 |
|---|---|---|
| 1.1 | `python -m collector contract --init --data-dir %COMPANION_DATA_DIR%` | 打印 `terms_version: 4` 与 `terms_hash: sha256:…` |
| 1.2 | 手工写一个 active 的服务状态（或连上模拟后台跑一次 `run --once`） | 生成 `service-state.json` |
| 1.3 | `python -m collector scan --data-dir %COMPANION_DATA_DIR%` | 第一行 `gate: active`，第二行 JSON 里 `records > 0`、`spool.L3 > 0` |
| 1.4 | 再跑一次 1.3 | `records` 为 0（同一批事件不重复进队列） |

- [ ] 1.1–1.4 通过。**此时没有安装任何插件**，数据来自 `%CODEX_HOME%\sessions\**\rollout-*.jsonl`。

## 2. 后台组件常驻与 IPC

| 步 | 命令 | 期望 |
|---|---|---|
| 2.1 | 另开一个窗口：`python -m collector run --data-dir %COMPANION_DATA_DIR%` | 无输出，进程常驻 |
| 2.2 | `python -m collector status --data-dir %COMPANION_DATA_DIR% --json` | `collector_running: true`、`recording: true`、`gate: active` |
| 2.3 | `python -m collector pause …` → `status` | `paused: true`、`recording: false` |
| 2.4 | `python -m collector resume …` → `status` | `paused: false` |
| 2.5 | 删掉 `%COMPANION_DATA_DIR%\ipc.token` 后 `status` | 走到 `collector_running: false`（文件状态回退），不报错退出 |

- [ ] 2.1–2.5 通过。

## 3. 插件生命周期不影响采集（核心验收）

| 步 | 操作 | 期望 |
|---|---|---|
| 3.1 | 在 Codex 插件页**禁用** Codex 助手包 | 采集器仍在跑，`status` 仍 `recording: true` |
| 3.2 | `codex plugin remove codex-companion@codex-companion` | 同上；`spool` 继续增长 |
| 3.3 | 完全退出 Codex App | 仍相同（采集只读盘上文件） |
| 3.4 | 自动化版：`python scripts\collector_acceptance.py` | 11 项全 PASS，其中含 `hostgate=plugin_disabled` 而 `collector gate=None` |

- [ ] 3.1–3.4 通过。

## 4. 停止是真停

| 步 | 命令 | 期望 |
|---|---|---|
| 4.1 | `python -m collector stop --data-dir %COMPANION_DATA_DIR%` | 返回 `recording: false`，`gate: contract_terminated` |
| 4.2 | 观察 2.1 那个窗口 | **1 秒内**退出到命令行 |
| 4.3 | `python -m collector status …` | `collector_running: false`，`recording: false` |
| 4.4 | `python -m collector run --once …` | 直接返回，不采集、不上传 |
| 4.5 | 看 `%COMPANION_DATA_DIR%\collector.log` | 有 `stop` 与 `exit` 两条，`reason: user` |

- [ ] 4.1–4.5 通过。

## 5. 计划任务（需要安装包，或手工指向插件目录）

| 步 | 命令 | 期望 |
|---|---|---|
| 5.1 | `python -m collector task install --data-dir %COMPANION_DATA_DIR%` | `{"ok": true, "detail": "created"}` |
| 5.2 | `schtasks /query /tn CodexCompanion\Collector /v /fo list` | 能在「任务计划程序」里看到，触发器=登录时，运行级别=最低 |
| 5.3 | 注销并重新登录 | 采集器自动起来（`status` → `collector_running: true`） |
| 5.4 | `python -m collector task uninstall --data-dir %COMPANION_DATA_DIR%` | `{"ok": true, "detail": "deleted"}`；再查 `schtasks` 已无此项 |

- [ ] 5.1–5.4 通过。**任务必须可查、可停**——这是本方案与"隐蔽常驻"的分界。

## 6. 上传与停止上传（模拟后台）

| 步 | 命令 | 期望 |
|---|---|---|
| 6.1 | `python scripts\mock_sync_server.py` | 监听 18788 |
| 6.2 | 在 `%COMPANION_DATA_DIR%\collector-config.json` 里把 `base_url` 设为 `http://127.0.0.1:18788` | — |
| 6.3 | `python -m collector run --once …` | `status` 里 `upload.uploaded > 0`，`spool` 归零 |
| 6.4 | `curl http://127.0.0.1:18788/_mock/events` | 能看到 `comp.event/v1`（L0/L1/L3）与 `comp.session/v1` |
| 6.5 | `collector stop` 后再 `run --once` | `/_mock/stats` 的事件数不再增长 |
| 6.6 | `curl -X POST http://127.0.0.1:18788/v1/device/uninstall -H "Authorization: Bearer <token>"` 后 `run --once` | 上传返回 403，`status` 里 `last_error: device_revoked`，**本机队列不清空** |

- [ ] 6.1–6.6 通过。

## 7. MCP 工具面

在 Codex 里（已启用助手包插件）后：

| 步 | 操作 | 期望 |
|---|---|---|
| 7.1 | 让 Codex 调用 `collector_status` | 返回 JSON，`collector_running: true`，含 `reader.events` / `spool.records` |
| 7.2 | 调用 `collector_pause` → `collector_status` | `paused: true` |
| 7.3 | 调用 `collector_resume` | 恢复 |
| 7.4 | 采集器未运行时调用 `collector_pause` | `isError: true` + 中文提示 + 错误码 `no_server` |

- [ ] 7.1–7.4 通过。

## 已知限制

- `python -m collector --data-dir X mcp` 与 `python -m collector mcp --data-dir X` 都支持；但 `-m collector.mcp` 只认 `--data-dir`。
- 首次 `scan` 会把历史会话全部读入（取决于 `%CODEX_HOME%\sessions` 大小）；之后按字节游标增量。
- `contract --init` 只写本机留痕；真实安装流程由安装包在安装步骤里调用同一条 `ensure_contract`。
