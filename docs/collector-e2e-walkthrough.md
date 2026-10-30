# 从安装到看见数据上传 — 完整跑通手册

目标：装一次、用一次、在后台看到数据。全程约 15 分钟。Windows，按顺序做，每一步都有"怎么确认"。

---

## 第 0 步 · 准备模拟后台（必须先起，装完就会连它）

开一个 PowerShell 窗口（**这个窗口要一直开着**）：

```powershell
cd D:\codex-task-recorder
python scripts\mock_sync_server.py --port 18788 --store $env:TEMP\mock-store.json
```

确认它活着（另开一个窗口）：

```powershell
curl.exe -s http://127.0.0.1:18788/_mock/stats
```

期望：`{"devices":0,"stats":{...},"events":{}}` —— 现在还没设备，正常。

> 装好的包是 `--profile mock`，后台地址固定 `http://127.0.0.1:18788`，所以这个 mock 必须在这台机器的 18788 上跑。

---

## 第 1 步 · 安装

```powershell
.\dist\CodexCompanion-Setup-1.5.3.exe
```

安装向导里会出现一页 **「本产品为会话记录服务」**：只有说明和一个「下一步」，**没有勾选框**。看完点下一步，装完即可（按用户安装，不弹 UAC）。

确认装上了：

```powershell
schtasks /query /tn "CodexCompanion\Collector" /fo list
Get-Content "$env:LOCALAPPDATA\CodexCompanion\contract.json"
```

期望：任务存在（`Status: Ready`，触发=登录时）；`contract.json` 里 `terms_version: 4`、`terminated_at: null`。

---

## 第 2 步 · 自检（一条命令看全部）

```powershell
cd D:\codex-task-recorder
python scripts\collector_installed_check.py
```

它会逐项打 PASS/FAIL：安装产物、合同留痕、后台地址、计划任务、采集器状态、后台收到多少条、收到了哪些档位。

刚装完、还没用 Codex 时，期望长这样：

```
[PASS] 安装产物存在 lib/collector/run.py
[PASS] 安装产物存在 lib/csr/reader.py
[PASS] 安装产物存在 bin/collector.cmd
[PASS] 安装产物存在 .mcp.json
[PASS] product.json 可读 — codex-companion@codex-companion
[ info ] 后台地址: http://127.0.0.1:18788
[PASS] 合同已留痕 terms_version — 4
[PASS] 条款哈希已记录
[PASS] 合同未终止 — —
[PASS] collector-config.json 有后台地址 — http://127.0.0.1:18788
[PASS] 登录计划任务已注册（CodexCompanion\Collector） — Status: Ready
[ info ] 运行中: True
[ info ] gate: active
[PASS] 后台可达 — http://127.0.0.1:18788
[FAIL] 后台已收到事件 — 0 条        ← 还没用 Codex，正常，下一步就有了
```

> 计划任务在**登录时**触发。刚装完没注销过的话，手动起一次：
> ```powershell
> & "$env:LOCALAPPDATA\Programs\CodexCompanion\runtime\python.exe" -m collector task status
> "C:\…\bin\collector.cmd"        :: 或直接双击安装目录里的这个文件
> ```

---

## 第 3 步 · 让 Codex 产生会话

打开 Codex 桌面 App，随便聊两轮（要能让模型回话，带一次工具调用更好，例如让它列一下目录）。

然后确认盘上真的有会话文件：

```powershell
Get-ChildItem "$env:USERPROFILE\.codex\sessions" -Recurse -Filter rollout-*.jsonl |
  Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name,Length,LastWriteTime
```

期望：有今天日期的 `rollout-*.jsonl` 文件。

> **不想用 Codex 也能测**：往上述目录塞一个合成的 rollout 文件即可（内容格式见 `tests\native_rollout.py`），采集器只认文件，不认是谁写的。

---

## 第 4 步 · 看它采到了什么

```powershell
& "$env:LOCALAPPDATA\Programs\CodexCompanion\runtime\python.exe" -m collector status
```

期望：`记录中 · 已采集 N 条 · 待上传 M 条`

想在本地先看队列（不上传）：

```powershell
$d = "$env:LOCALAPPDATA\CodexCompanion"
Get-ChildItem "$d\spool" -Recurse -Filter *.jsonl | ForEach-Object {
  "{0}/{1}: {2} 行" -f $_.Directory.Name, $_.Name, (Get-Content $_.FullName).Count
}
```

期望：`L0` 最多、`L3` 次之、`L1` 少量、`L2` 通常为 0（同一事件上 L3 取代 L2）。

不想等 2 秒轮询，可以立刻采一次：

```powershell
& "$env:LOCALAPPDATA\Programs\CodexCompanion\runtime\python.exe" -m collector --data-dir "$env:LOCALAPPDATA\CodexCompanion" scan
```

---

## 第 5 步 · 看数据到后台（关键一步）

```powershell
curl.exe -s http://127.0.0.1:18788/_mock/stats
curl.exe -s http://127.0.0.1:18788/_mock/events | ConvertFrom-Json | ForEach-Object { $_.events } |
  Group-Object tier | Select-Object Name,Count
curl.exe -s http://127.0.0.1:18788/_mock/events | ConvertFrom-Json | ForEach-Object { $_.events } |
  Where-Object { $_.kind -eq 'user_prompt' } | Select-Object -First 1
```

期望：

| 看什么 | 期望 |
|---|---|
| `_mock/stats` | `devices: 1`，`events` 里该设备条数 > 0 |
| 分档统计 | `L0` 有，`L1`/`L3` 视会话内容出现 |
| 一条 `user_prompt` | 里面有 `session_id`、`ts`、`model`、`data`（你刚才问的那句话） |
| schema | `comp.event/v1` 与 `comp.session/v1` |

再跑一次自检，这次应该全绿：

```powershell
python scripts\collector_installed_check.py
```

期望结尾：`ALL PASS — 采集器已装好，并且数据正在到达后台`

---

## 第 6 步 · 验证"插件没了还在采"

| 步 | 操作 | 怎么确认 |
|---|---|---|
| 6.1 | 在 Codex 插件页**禁用** Codex 助手包 | `status` 仍是 `记录中` |
| 6.2 | `codex plugin remove codex-companion@codex-companion` | 同上；继续用 Codex 聊两句，`_mock/stats` 继续涨 |
| 6.3 | 完全退出 Codex App | 往 rollout 文件追加几行，再 `scan`，条数上涨 |
| 6.4 | 上面任意一步之后 | `python scripts\collector_installed_check.py` 仍全绿（`gate: active`） |

---

## 第 7 步 · 停止 / 恢复

```powershell
$py = "$env:LOCALAPPDATA\Programs\CodexCompanion\runtime\python.exe"
& $py -m collector stop                  # 真停：写终止时间，进程 1 秒内退出
& $py -m collector status                # → 已停止 · gate: contract_terminated
& $py -m collector run --once            # → 直接返回，不采不上传
& $py -m collector resume                # 用户自己停的，可以恢复
& $py -m collector status                # → 记录中
```

期望：`stop` 后 `_mock/stats` 不再增长；`resume` 后恢复增长。

---

## 第 8 步 · 卸载

「设置 → 应用 → Codex 助手包 → 卸载」，或开始菜单「卸载 Codex 助手包」。

```powershell
schtasks /query /tn "CodexCompanion\Collector"     :: 期望：找不到
Get-Process pythonw -ErrorAction SilentlyContinue   :: 期望：没有采集器残留
Get-Content "$env:LOCALAPPDATA\CodexCompanion\contract.json" | Select-String terminated_at
```

期望：任务已删除、进程已退出、`contract.json` 里 `terminated_at` 有值（`terminated_reason: uninstall`）。

> 卸载**保数据**的话，重装会开一份新合同自动恢复记录；`terminated_reason: user` 的停止则不会被重装偷偷打开。

---

## 卡住时按这张表查

| 现象 | 原因 | 处理 |
|---|---|---|
| 自检报"后台不可达" | mock 没起 / 端口不是 18788 | 第 0 步重起；`curl.exe -s http://127.0.0.1:18788/_mock/stats` |
| `collector-config.json 有后台地址` FAIL | 安装包不是 mock profile，或装的是旧包 | 用 `--profile mock` 重打包；或手写 `{"base_url":"http://127.0.0.1:18788"}` |
| `gate: no_service_state` | 采集器还没连过后台 | 起 mock 后 `run --once` 一次，它会自己拉 `service-state` |
| `后台已收到事件 0 条` | 还没产生会话 / 采集器没跑 | `status` 看 `已采集`；为 0 就检查 `%USERPROFILE%\.codex\sessions` 有没有今天的 rollout |
| `已采集 N 条 · 待上传 M 条` 一直不动 | 上传失败 | `Get-Content "$env:LOCALAPPDATA\CodexCompanion\collector-status.json"` 看 `upload.last_error` |
| `device_revoked` | 后台把设备注销了（点过卸载） | 换一个数据目录重新注册 |
| 计划任务不在 | 装的时候 `COMPANION_DATA_DIR` 被设过（测试模式会跳过） | 去掉该环境变量重装，或 `python -m collector task install` |
| `PermissionError` 写不进 `%TEMP%` | 本机沙箱 ACL | 把 `TMP`/`TEMP` 指到工作区内目录再跑 |
