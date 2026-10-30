# Codex Task Recorder（原型 v0.1）

面向主动参与模型评测、明确授权的用户：在 **Windows 官方 Codex 桌面 App** 中，通过官方生命周期 Hooks 把测试会话记录为本地 JSONL。不修改官方程序。默认只在本机记录；**数据同步**（上传到后台）需另行开启并同意。

## 本地会话记录解析器（PoC，不使用 Hooks）

只读解析用户指定目录里的 Codex `rollout-*.jsonl`：增量读取（SQLite 保存进度）、标准化事件、命令行统计、导出 JSONL。不改 Codex 程序 / 配置 / 会话文件，不联网，不读 `auth.json`。

```bat
csr.cmd scan  --dir %USERPROFILE%\.codex\sessions\2026\10\28 --session 01a18266   :: 只读扫描一次
csr.cmd stats                                  :: 统计（终端不显示正文）
csr.cmd watch --dir <会话目录>                  :: 前台定时扫描，Ctrl+C 退出
csr.cmd export --out D:\csr-exports\events.jsonl :: 导出（脱敏后的完整正文）
csr.cmd purge --all                            :: 删除本工具保存的全部数据
```

说明、测试结果、字段覆盖率、限制：`docs/session-reader.md`；设计：`docs/superpowers/specs/2026-10-29-session-reader-design.md`。

## Codex 原生助手（原生插件 PoC，不用 Hooks）

只装一个 Codex 插件（`codex-native-assist`），不用 Hooks、不改模型 Provider、没有独立安装包或常驻程序。插件随包一个约 140 KB 的 `bin\assist.exe`（Windows 自带的 .NET Framework 与 `winsqlite3.dll`，无需 Python / Node），由 Codex 经 `cmd.exe /d /c bin\launch.cmd` 作为本地 MCP Server 按会话启动（实际运行的是插件数据目录里的副本，插件目录不被占用，可随时卸载 / 更新）。

| 操作 | 方式 |
|---|---|
| 安装（开发） | `native\assist\build.cmd` 构建 exe → `codex plugin marketplace add <本仓库>` → `codex plugin add codex-native-assist@ctr-local`（用 App 自带内核）。新会话立即生效 |
| 充值 | **固定入口**：侧边栏「…」→「CDK 充值」（可固定到导航栏）→ 点「打开充值窗口」，不经过模型、无审批；也可在对话中说「充值」。弹出小助手样式的原生卡片 → 首次确认账号 → 输入 CDK。CDK 不进对话、不是工具参数、不写日志 |
| 任务记录 | 说「任务记录设置」→ 授权窗口中勾选同意才开启；说「关闭任务记录」、窗口中「关闭记录」或在插件页关闭本插件即停止（撤销提交后不会再写入任何记录） |
| 数据 | `%USERPROFILE%\.codex\plugins\data\codex-native-assist-<市场>\`：`assist.sqlite3`（授权、事件、问题）、`lifecycle.jsonl`、`recharge.log`、DPAPI 加密的 `binding.bin`。**卸载插件不会删除该目录** |

- 可行性结论：`docs/native-plugin-feasibility.md`；真实 App 验收、并发、停止延迟、覆盖率、回归：`docs/native-p4-acceptance-report.md`；验收清单：`docs/native-assist-manual-test.md`；固定充值入口：`docs/native-recharge-entry-spike.md`。
- PoC 的充值服务指向模拟后台 `http://127.0.0.1:18787`（`plugin-config.json`，卡片底部注明「当前连接的是本机模拟后台」）；先运行 `python scripts\mock_recharge_server.py`。
- **上传**：开启任务记录即包含上传，不单独授权。负责采集的进程每轮采集后把新事件分批上传（接口见 `docs/sync-api.md` §8），后台确认后立即从本机删除；未确认的留在本机，下次重发。服务地址 `sync_base_url` **目前为空**（`plugin-config.json`，或 `assist.exe config --sync-url <url>`），为空时只存本机。
- **调试期默认开启任务记录**：`plugins/codex-native-assist/plugin-config.json` 中 `"debug_recording_default_on": true`，每个数据库首次运行插件时自动开启（范围：全部会话，状态显示「调试版默认开启」）；之后手动关闭会保持关闭。**发布前必须改为 `false`**，恢复为用户在授权窗口勾选同意后才开启。测试与验收脚本用 `ASSIST_RECORDING_DEFAULT=off` 固定按发布行为运行。
- 命令行：`assist.exe status`（授权与采集状态）、`pause`（关闭记录）、`delete-data`（删除已记录数据）。
- 测试：`tests\test_native_assist.py`（`set PYTHONPATH=plugins\codex-task-recorder\lib;plugins\codex-cdk-recharge\lib;tests`）；`scripts\native_acceptance.py`（App 内核：多会话、停止延迟、长时间空闲、补读、归档、覆盖率）；`scripts\native_lifecycle_probe.py`；`scripts\native_window_e2e.py`；`scripts\native_app_*.py`（驱动真实 App）；`scripts\native_regression.py`（四个环境的回归基线）。

## Codex 助手包（P4 一体化安装包）— 面向最终用户

把 P1 充值与 P3 授权数据同步合并为**一个安装包、一个 Codex 插件**（`codex-companion@codex-companion`），自带 Python 运行时，用户机器不需要任何依赖。

| 步骤 | 操作 |
|---|---|
| 安装 | 双击 `CodexCompanion-Setup-<版本>.exe`（按用户安装，不需要管理员）→ 向导里读一页「本产品为会话记录服务」→ 安装 |
| 启用 | 完全退出并重新打开 Codex → 插件 → Codex 助手包 → 信任 MCP 服务器（只有一个，不再有 12 个 Hook） |
| 充值 | Codex 中发送 `/充值`（或开始菜单「Codex 充值」）→ 输入 CDK → 确认提交 |
| 任务记录（自动） | 无需开关：随安装生效，后台组件读 Codex 会话文件并上传；面板「任务记录」一行显示记录中 / 已采集 N 条 / 待上传 M 条 |
| 停止 / 卸载 | 面板「停止记录」；或「设置 → 应用」卸载（先终止合同、停进程、删计划任务，再删文件） |

**用户手册：[`docs/user-install-guide.md`](docs/user-install-guide.md)**（P4 时期的那份 `p4-install-guide.md` 已标注过期点）。

- 一个开关：记录与上传共用一份授权；在 Codex 中禁用插件或关闭其 Hook，上传进程也会自行停止。
- 更新：默认检查并提示，用户点击确认后才安装；可改为自动安装或不检查（`docs/update-api.md`）。
- Codex 版本不兼容时降级运行：停用插件并说明原因，充值窗口仍可从开始菜单打开。
- 文档：安装说明 `docs/p4-install-guide.md`；测试报告与已知限制 `docs/p4-test-report.md`；桌面手测 `docs/p4-manual-test.md`；设计 `docs/superpowers/specs/2026-10-28-p4-companion-design.md`。
- 构建：`python packaging\build.py --version 1.0.0 --recharge-url … --sync-url … --update-url …`（本机联调用 `--profile mock`），产物在 `dist\`。需要 Python 3.12 x64（含 Tk）与 Inno Setup 6。

下文是各模块的开发版（`.cmd` 入口、`ctr-local` 本地市场），与助手包不要同时启用（安装助手包时会询问卸载开发版）。

## 用户使用（开发版记录器）

| 步骤 | 操作 |
|---|---|
| 1 | 双击 **`安装.cmd`**：阅读同意说明，按 `Y` 同意 → 自动安装插件并开启记录 |
| 2 | 重启 Codex App → 插件 → Codex Task Recorder → 点 **「全部信任」**（Codex 强制要求用户亲自确认） |
| 3 | 正常使用。每个会话第一条消息后会出现「● Codex Task Recorder 正在本地记录本会话」 |
| 停止 | 双击 **`停止记录.cmd`**（立即生效）；恢复用 **`开始记录.cmd`** |
| 状态 | 双击 **`记录状态.cmd`**：是否在记录、记录了多少、Hook 是否已信任 |
| 卸载 | 双击 **`卸载.cmd`**：卸载插件，可选删除已记录数据 |

要求：Codex 桌面 App（已验证 26.1013.4894.0 / 内核 0.163.0-alpha.10.2）、Python 3.12+ 在 PATH 上。

## 记录什么、不记录什么

- 记录：用户 Prompt、模型公开回复、模型名与配置、工具调用 / 结果 / 错误、公开推理摘要（模型提供时）、时间戳、会话 / 回合 / 工具 ID。
- 不记录：密码、API Key、令牌、Cookie、私钥（规则脱敏）；凭证文件内容（auth.json、.env 等）；加密推理与原始推理；系统指令（只记哈希）；额度与账户信息。
- 每条事件标注 `source`（hook / transcript / derived）与 `completeness`（complete / partial / truncated / unavailable），拿不到的数据不填充。
- 数据位置：`%LOCALAPPDATA%\CodexTaskRecorder\sessions\<日期>\<会话ID>.jsonl`
- 桌面 App 自动发起的后台任务（无 transcript 的会话）也会触发 Hook：照常记录，标记 `session_kind=background`，报告中单独统计。
- 事件格式 `ctr.event/v2`：会话内递增的 `seq`（采集顺序）、每条事件的 `model`；每个回合结束时派生一条 `turn_coverage`，逐项说明捕获了什么、缺什么、为什么缺。

## 数据同步（P3 原型）

默认关闭。开启后，**开启之后**记录的内容由独立的后台上传进程持续上传；Codex 正常使用即可，不需要打开充值窗口，Hook 本身不联网。

| 步骤 | 操作 |
|---|---|
| 开启 | 双击 **`开启同步.cmd`**：阅读授权说明，按 `Y` 同意（如本地记录未开启会先询问记录同意） |
| 暂停 | 双击 **`暂停同步.cmd`**：之后的内容不上传；已排队的内容留在本机，恢复后继续上传 |
| 恢复 | 再次双击 **`开启同步.cmd`**（一行确认） |
| 状态 | 双击 **`同步状态.cmd`**：待上传 / 已上传条数、最近错误、上传进程、后台已收到条数 |
| 撤销 | 双击 **`撤销同步.cmd`**：停止上传、丢弃未上传内容，可选删除后台已上传数据；离线时联网后自动送达 |

- 身份：匿名设备（后台生成的设备 ID + DPAPI 加密保存的设备令牌），不发送 ChatGPT 账号信息。与充值插件不共享代码、令牌和数据。
- 同意绑定后台地址：更换后台须重新同意；本设备已在原后台注册时须先「撤销同步」（否则原后台的数据将无法再删除）。
- 队列就是本地会话文件：每个会话一个上传游标，后台确认后才前移；断网、后台停机、进程退出都不会丢；后台按 `event_id` 去重；同一会话按 `seq` 顺序发送。
- 上传前再脱敏一遍（含 CDK / 兑换码、`sess-` 会话密钥、令牌），并去掉记录器诊断信息（本机环境变量、进程链）。
- 后台尚未实现：接口约定见 `docs/sync-api.md`。同步服务地址默认未配置；本机调试先运行 `python scripts\mock_sync_server.py`，再 `ctr.cmd sync config --base-url http://127.0.0.1:18788`。
- 本机数据：`%LOCALAPPDATA%\CodexTaskRecorder\sync\`（`state.json` 授权时段、`device.bin`、`cursors\` 上传游标、`status.json`、只记操作类型的 `sync.log`）。
- 设计：`docs/superpowers/specs/2026-10-28-data-sync-design.md`；测试结果：`docs/sync-test-report.md`；桌面手测：`docs/sync-manual-test.md`。

## 独立采集器（P5）— 采集与插件分离

插件只是 UI。真正记录与上传的是**独立安装的后台组件** `collector`：它由用户级计划任务 `CodexCompanion\Collector` 在登录时启动，读 Codex 自己写的 `%CODEX_HOME%\sessions\**\rollout-*.jsonl`，因此**不看插件是否被信任、不要求 Codex 在运行**。在 Codex 里禁用插件、`codex plugin remove`、关掉 Codex App，采集与上传都继续。

| 操作 | 方式 |
|---|---|
| 运行 | 计划任务调用 `bin\collector.cmd` → `pythonw -m collector run`（无窗口） |
| 状态 | `python -m collector status`（一行）或 `--json`；MCP 工具 `collector_status` |
| 暂停 / 恢复 | `collector pause` / `resume`；MCP `collector_pause` / `collector_resume` |
| 停止（真停） | `collector stop`：写 `terminated_at` 并通知后台，下一轮即返回，不再记录 |
| 计划任务 | `collector task install` / `uninstall` / `status` |
| 条款留痕 | `collector contract --text`（`terms_version=4` + 全文哈希写入 `contract.json`） |
| 数据 | `%LOCALAPPDATA%\CodexCompanion\`：`spool\{L0,L1,L2,L3}\`、`reader.sqlite3`、`contract.json`、`service-state.json`、`collector-status.json`、`collector-config.json` |

- **采集分级**：L0 元数据 / L1 结构 / L2 正文 / L3 完整事件（含 provenance），另加每会话一条 `comp.session/v1` 遥测汇总。默认四级全开、全量上传；`sample_ratio`（默认 1.0）与 `code_mode`（`full`/`structure`/`hash`）可压量。
- **队列**：按档分目录、按 1 MiB 分段，确认后才删；`L0 300MB / L1 120MB / L2 400MB / L3 900MB` 上限 + 200MB 磁盘余量保护，超限整段丢最旧，L3 先于 L2、L2 先于 L1、L0 最后。
- **解耦点**：`ctr/sync/gate.py`。`hostgate` 只回答「插件是否还被信任」，`gate` 的结论是「插件可信 **或** 服务合同 `active`/宽限内」，并且 `terminated` 一律关闸。
- **接口**：`docs/collector-api.md`（4 个新接口 + 2 个事件 schema，后台需加白名单）；本机联调 `python scripts\mock_sync_server.py` 后在 `collector-config.json` 里配 `base_url`。
- **验收**：`python scripts\collector_acceptance.py`（11 项，含「插件禁用后仍采」「停止后不再上传」）；`python scripts\collector_installed_check.py`（装完后 15 项自检：任务、合同、配置、gate、后台收到的档位与条数）；`tests\test_collector.py`、`tests\test_collector_mock.py`。
- **手册**：从安装到看见数据上传 → `docs/collector-e2e-walkthrough.md`；分层测试 → `docs/collector-testing-guide.md`；桌面手测 → `docs/collector-manual-test.md`。
- 当前产物：`dist\CodexCompanion-Setup-1.5.3.exe`（自带运行时，13.2 MB，按用户安装；`--profile mock`，后台指向 `127.0.0.1:18788`）。
- 设计：`docs/superpowers/specs/2026-11-01-collector-separation-design.md`；接口：`docs/collector-api.md`。

## 后台服务（P6）— 真正接收数据的那一端

`server/ctrsrv/`：只用标准库 + 一个 SQLite 文件，无外部依赖。`python -m ctrsrv serve` 就是一个可用的部署形态；量大时同一套代码挂在 nginx 后面终止 TLS。

| 能力 | 位置 |
|---|---|
| 设备注册与令牌（只存 SHA-256） | `POST /v1/devices`、`GET/DELETE /v1/devices/me` |
| 事件摄入（`(device, event_id)` 幂等、按档统计、8 MiB 正文上限） | `POST /v1/events/batch`（接受 `ctr.event/v2`、`csr.event/v1`、`comp.event/v1`、`comp.session/v1`） |
| 服务状态（采集器据此决定采不采） | `GET /v1/service-state`、`POST /v1/service-state/terminate` |
| 心跳与卸载 beacon | `POST /v1/device/heartbeat`、`POST /v1/device/uninstall` |
| CDK 兑换（`(cdk, account)` 幂等，不重复扣卡） | `POST /v1/redemptions`、`GET /v1/redemptions/{id}` |
| 运维 | `/admin/*`（管理令牌）、`prune --days`、`devices/events/terminate/resume/purge` 命令行 |

- 条款一致性：`terms_version` / `terms_hash` 从采集器的 `contract.py` 导入，不单独维护；漂移会被测试抓住。
- 跑起来：`python -m ctrsrv initdb` → `add-cdk` → `serve`；配置走环境变量 `CTRSRV_DB` / `CTRSRV_HOST` / `CTRSRV_PORT` / `CTRSRV_ADMIN_TOKEN`（命令行参数优先）。
- **看数据**：网页 `http://127.0.0.1:8080/admin/ui?token=<管理令牌>`（设备 → 会话 → 单条 JSON）；命令行 `python -m ctrsrv view --latest`（按对话还原，去重只留最完整的一份）。详见 `docs/backend.md` §9。
- 部署：`server/ctrsrv.service`（systemd，令牌走 `EnvironmentFile`，不上命令行）、`server/serve.cmd`（Windows）、`server/nginx-ctrsrv.conf`（TLS 反代 + `/admin/` 白名单）。
- 客户端对接：`packaging\build.py --profile prod --sync-url https://你的域名` 打出来的安装包，采集器直接认这个地址（`product.json` → `collector.config.load` 回退），不需要再改配置文件。
- 验收：`python scripts\backend_e2e.py`（14 项：充值 → 采集 → 上传 → 后台统计 → 后台终止 → 客户端停采）；`tests\test_server_core.py`（19）、`tests\test_server_api.py`（25）。
- 文档：`docs/backend.md`（含接口表、数据模型、备份/保留期/DSAR、三种部署、容量与安全、附录 A 实测差异）；客户端契约：`docs/collector-api.md`、`docs/sync-api.md`、`docs/recharge-api.md`。

## CDK 充值插件（P1 原型）

在 Codex 输入框单独发送 **`/充值`** → 弹出「Codex 充值」小窗口 → 输入 CDK → 确认提交（无需绑定账号）。CDK 只在小窗口里输入，不进入 Codex 对话、日志或埋点。

| 步骤 | 操作 |
|---|---|
| 安装 | 双击 **`安装充值插件.cmd`** → 重启 Codex App → 插件 → Codex CDK 充值 → 点「信任」 |
| 使用 | Codex 输入框发送 `/充值`；或双击 **`充值.cmd`** 直接打开窗口 |
| 卸载 | 双击 **`卸载充值插件.cmd`** |
| 设置 | `cdkr.cmd config --base-url https://…`（充值服务地址）；`cdkr.cmd status` |

- 充值账号由用户在窗口中输入，随 CDK 一起发送给充值服务。插件不读取 `account/read` 或 `auth.json`，不发送任何 ChatGPT 登录凭证。
- 后台尚未实现：接口约定见 `docs/recharge-api.md`。充值服务地址默认未配置；本机调试先运行 `python scripts\mock_recharge_server.py`，再 `cdkr.cmd config --base-url http://127.0.0.1:18787`。
- 代价：UserPromptSubmit 不支持 matcher，每条提示词都会运行一次该 Hook（约 0.4 s）。
- 本机数据：`%LOCALAPPDATA%\CodexCdkRecharge\`（`config.json`、只记操作类型的 `cdkr.log`）。
- 设计：`docs/superpowers/specs/2026-10-28-cdk-recharge-design.md`；测试结果：`docs/recharge-test-report.md`。

## 目录

```
.agents/plugins/marketplace.json     本地插件市场 ctr-local
plugins/codex-task-recorder/          Codex 插件（清单、hooks.json、Python 记录器）
plugins/codex-cdk-recharge/           Codex 插件：/充值 弹窗（hooks.json、tkinter 窗口、后台客户端）
plugins/codex-companion/              P4 助手包插件：统一 Hook 入口、lib/companion（安装/卸载、控制面板、授权、更新、兼容）
plugins/codex-native-assist/          原生插件 PoC：.mcp.json + skills + bin\assist.exe（MCP Server、充值窗口、授权窗口、会话采集）
native/assist/                        assist.exe 的 C# 源码与 build.cmd（.NET Framework 自带的 csc.exe）
tools/session-reader/csr/            本地会话记录解析器（reader / parser / storage / scanner / summary / cli）
packaging/                           P4 构建：build.py、runtime.py（私有 Python）、companion.iss、make_hooks.py
*.cmd                                 用户双击入口；ctr.cmd / cdkr.cmd / csr.cmd 为底层 CLI
tests/                                单元测试（合成数据）
scripts/live_tests.py                 实测脚本（消耗模型额度）
scripts/live_recharge_hook.py         /充值 Hook 实测（app-server）
scripts/live_window_demo.py           充值窗口实测（真实窗口 + 模拟后台，逐步截图）
scripts/mock_recharge_server.py       充值模拟后台
scripts/mock_sync_server.py           数据同步模拟后台
scripts/live_sync_test.py             数据同步实测（app-server + 模拟后台，消耗模型额度）
scripts/p4_acceptance.py              P4 自动验收（真实安装包 + App 内核 + 模拟后台与模拟模型，不消耗额度）
scripts/mock_update_server.py        更新模拟后台；scripts/mock_model_server.py 模拟模型（Responses API）
scripts/csr_live_check.py            会话解析器真实会话验证（只读，分块回放 + 重启 + 损坏副本）
docs/hooks-support.md                 官方 App 的 Hooks 支持范围与限制
docs/coverage-report.md               事件覆盖率报告
docs/recharge-api.md                  充值后台接口约定
docs/recharge-test-report.md          充值插件测试结果与遗留问题
docs/recharge-manual-test.md          充值插件桌面 App 手动测试清单
docs/desktop-manual-test.md           桌面 App 手动测试清单
docs/sync-api.md                      数据同步后台接口约定
docs/sync-test-report.md              数据同步测试结果与遗留问题
docs/sync-manual-test.md              数据同步桌面 App 手动测试清单
docs/p4-install-guide.md              P4 安装说明
docs/p4-test-report.md                P4 测试报告与已知限制
docs/p4-manual-test.md                P4 桌面 App 手动测试清单
docs/update-api.md                    P4 更新服务接口约定
docs/session-reader.md                会话解析器使用说明、测试结果、覆盖率与限制
docs/superpowers/specs/               设计说明
```

## 开发

```bat
:: 单元测试
set PYTHONPATH=plugins\codex-task-recorder\lib;plugins\codex-cdk-recharge\lib;plugins\codex-companion\lib;tools\session-reader;tests
python -m unittest discover -s tests -t tests

:: P4：构建安装包并自动验收（临时 CODEX_HOME，不碰 ~/.codex；会真实安装再卸载）
python packaging\build.py --version 1.0.0 --profile mock
python packaging\build.py --version 1.0.1 --profile mock
python scripts\p4_acceptance.py

:: 仅限测试目录开启记录、实测、出报告
ctr.cmd enable --i-consent --cwd D:\ctr-testbed
python scripts\live_tests.py --alt-model gpt-5.6-luna
ctr.cmd report --copy-to docs\coverage-report.md   :: report 会先执行 flush，补读会话最后一个 Hook 之后写入的 transcript

:: 数据同步实测（自动启动模拟后台、开启同步，结束时撤销并删除）
python scripts\live_sync_test.py --alt-model gpt-5.6-luna
ctr.cmd sync run --once                            :: 前台运行一次上传进程（调试）
```

修改 Python 代码后运行 `ctr.cmd install` 即可原地更新插件，**不需要重新信任**；修改 `hooks/hooks.json` 会让对应 Hook 变为 `modified`，需要用户重新信任。
