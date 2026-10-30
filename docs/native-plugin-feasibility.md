# Windows Codex 原生插件 PoC：可行性验证与 CDK 充值原型

- 日期：2026-10-29；平台：Windows Server 2025（10.0.26100）
- 目标版本：Codex 桌面 App `OpenAI.Codex 26.1013.4894.0`；App 内核 `codex-cli 0.163.0-alpha.10.2`
- 原型：`plugins/codex-native-assist/`（源码 `native/assist/`）
> **P4 更新（2026-10-29）**：本文是第一轮结论，第二轮在真实 App 中的验收结果见 `docs/native-p4-acceptance-report.md`。主要修正：数据目录改为 Codex 插件数据目录（App 是 MSIX 包，`%LOCALAPPDATA%` 会被重定向）；采集改为共享 SQLite；**在 App 插件页关闭开关时，Codex 会立即结束该插件的全部实例**（只有直接改配置时实例才会继续运行）；启动方式改为 `cmd.exe /d /c bin\launch.cmd`（影子副本），否则实例运行时无法卸载。下文对应处已标注。

- 标注：**【实测】** 本机验证；**【文档】** 官方文档；**【内核】** App 内核二进制中的校验文案（`codex.exe` 字符串）；**【待手测】** 需要在桌面 App 中亲自操作（见 `native-assist-manual-test.md`）

与前几轮（P0–P4）的区别：前几轮靠 `UserPromptSubmit` 等 Hooks（需要用户逐个信任），P4 还带独立安装包。本轮要求 **不用 Hooks、只装一个插件、不改 Provider、无常驻程序**，因此改为「插件 + 随包本地 MCP Server」。

## 0. 结论速览

| 验证项 | 结论 | 依据 |
|---|---|---|
| 插件目录结构与安装 | `.codex-plugin/plugin.json`（`mcpServers`、`skills` 指向插件内文件）+ `.mcp.json` + `skills/` + 随包文件。安装：市场（本地目录 / 仓库）→ `codex plugin marketplace add` → `codex plugin add <插件>@<市场>`，或 App 插件页安装。安装 = 复制到 `~/.codex/plugins/cache/<市场>/<插件>/<版本>/`，`config.toml` 只新增 `[marketplaces.*]` 与 `[plugins."…"] enabled = true` | 【实测】【文档】 |
| 能否打包本地 MCP Server | **能**。stdio `command` 必须是「裸可执行名或插件内的 `./` 路径」，`cwd` 只能是 `./`、`${PLUGIN_ROOT}`、`${PLUGIN_DATA}`。第一轮用 `command: ./bin/assist.exe` 实测可以启动；P4 改为与官方内置插件相同的 `cmd.exe /d /c bin\launch.cmd`（运行数据目录里的副本，避免卸载失败）。**不需要任何信任步骤**（只有 Hooks 需要） | 【实测】【内核】 |
| MCP Server 是否由 Codex 自动启动 | **是，但按会话懒启动**：内核启动、尚无会话时不启动；新建或加载会话时启动；**每个会话一个进程** | 【实测】 |
| 不调用插件时是否保持运行 | **是**：会话加载期间一直运行，与是否调用工具无关（空闲 300 秒，60 次心跳不断）。但它依附于「已加载的会话」，**不是后台常驻服务** | 【实测】 |
| 退出 / 重启 / 禁用 | 内核正常退出或被强杀：MCP Server 被 **Job Object 立即终止**（不发 stdin EOF，不留孤儿）；内核重启后不会自己拉起，直到有会话加载；直接改配置禁用插件时，已运行的实例不会停止（已打开的会话里模型仍能调用它的工具），只是新会话不再启动；`thread/unsubscribe` 也不会停止实例。**P4 实测桌面 App：插件页关闭开关会立即结束全部实例；退出 App 1 s 内全部结束** | 【实测】app-server |
| 不用 Hooks 持续读取授权范围内的会话 | **能，有条件**：MCP Server 以用户身份运行、不在沙箱里，可增量读取 `CODEX_HOME\sessions`（rollout）。只在有会话加载时运行；Codex 关闭期间产生的内容（例如 CLI 会话）在下次启动时按授权时段补读。rollout 不是官方稳定接口 | 【实测】 |
| 轻量充值入口 | **能**：插件 skill + MCP 工具 `open_recharge_window` → 原生小窗口。入口需要一次模型回合（用户说「充值」），首次调用需用户在 Codex 中批准（可选本会话 / 始终允许）；CDK 不经过模型、对话、工具参数和日志 | 【实测】 |

## 1. 插件结构与安装（实测）

```
plugins/codex-native-assist/
  .codex-plugin/plugin.json   name / version / interface；"skills": "./skills/"，"mcpServers": "./.mcp.json"
  .mcp.json                   native_assist：command cmd.exe，args [/d /c bin\launch.cmd mcp]，cwd .（P4）
  skills/cdk-recharge/SKILL.md     何时调用 open_recharge_window，禁止在对话中索要 CDK
  skills/task-recording/SKILL.md   开启只能经授权窗口；关闭 / 查询
  bin/assist.exe              88 KB，.NET Framework 4.x（Windows 10 1903+ / 11 自带），无其他依赖
  plugin-config.json          PoC：充值服务地址（模拟后台 127.0.0.1:18787，标记 mock）
```

- 官方内置插件（`code-review`、`codex-app-tools`）用的也是这一结构：`plugin.json` 里 `"mcpServers": "./.mcp.json"`。它们借 App 注入的 `CODEX_MCP_NODE_PATH` 用 Codex 自带的 Node 运行；该变量未对第三方公开、随版本变化（内置启动脚本里有 6 级回退），所以本原型 **不依赖它**，改用 Windows 自带的 .NET Framework。
- 安装后插件文件被复制进 `~/.codex/plugins/cache/...`；MCP Server 的工作目录就是这个缓存目录（实测 `cwd` = `...\cache\nat-probe\codex-native-assist\0.1.0`）。
- MCP Server 拿到的环境变量是**白名单**（实测）：`APPDATA COMSPEC HOMEDRIVE HOMEPATH LOCALAPPDATA PATH PATHEXT PROGRAMDATA PROGRAMFILES… SYSTEMDRIVE SYSTEMROOT TEMP TMP USERDOMAIN USERNAME USERPROFILE WINDIR`，外加 `.mcp.json` 的 `env_vars` 里列出且存在的变量（本插件转发 `CODEX_HOME`、`CODEX_CLI_PATH`）。**`PLUGIN_ROOT`、`PLUGIN_DATA` 不会传给 MCP Server**（它们只给 Hooks），第一轮数据目录用 `%LOCALAPPDATA%\CodexNativeAssist\`；P4 在真实 App 中发现 MSIX 重定向，改为 Codex 插件数据目录 `~/.codex/plugins/data/codex-native-assist-<市场>`（由插件所在缓存路径推出）。
- 模型看到的是一个命名空间 `mcp__native_assist`，下面 5 个工具；Codex 会在每个工具描述后追加「This tool is part of plugin `…`」。
- **工具审批（实测）**：内核按工具注解决定是否审批。`open_recharge_window` 如实标注了 `openWorldHint: true`（窗口会访问外部充值服务），内核要求用户批准：发出 `mcpServer/elicitation/request`（`_meta.codex_approval_kind = "mcp_tool_call"`，消息 `Allow the native_assist MCP server to run tool "open_recharge_window"?`，`persist: ["session", "always"]`，即可选「本会话允许 / 始终允许」）；在 `approval_policy = never` 下则直接失败（`MCP tool call requires approval, but approval policy is never`）。其余 4 个工具（只读或仅本机）不需要审批。桌面 App 中审批卡片的样子 **【待手测】**。

## 2. MCP Server 生命周期实测

方法：`scripts/native_lifecycle_probe.py`。用 App 自带内核启动独立的 `codex app-server`（与桌面 App 相同的协议和进程模型），临时 `CODEX_HOME`、模拟模型（不耗额度、不碰 `~/.codex`）。证据来自两个独立来源：Server 自己的生命周期日志（start / heartbeat / exit 原因）和系统进程表。结果：`.testdata/native-probe/results-run2.json`（空闲 300 秒的完整一轮）、`results-run3.json`（J 步带充值窗口与审批）。

| 步骤 | 观察 | 结论 |
|---|---|---|
| A 内核启动并初始化，无会话 | 6 秒内没有 assist.exe | 不随内核启动 |
| B `thread/start` | 立即出现 1 个进程，父进程 codex.exe（app-server）；`in_job = true`；`initialize` 客户端 `codex-mcp-client 0.163.0-alpha.10.2` | 会话启动时自动拉起 |
| C 模拟模型调用 `plugin_diagnostics` | `mcpToolCall` completed，无审批请求 | 工具链路通 |
| D 空闲 300 秒 | 同一 PID 存活，60 次心跳（5 秒一次），无退出 | 不调用也一直运行 |
| E 第二个 `thread/start` | 第 2 个进程 | **每会话一个实例** |
| F `thread/unsubscribe` 第二个会话 | 进程仍在 | 取消订阅不回收 |
| G 运行中禁用插件（`config/value/write plugins.<id>.enabled=false`），再在已有会话发一轮、再新建会话 | 已有 2 个实例不退出；**已有会话里模型仍能看到并调用全部 5 个工具**；新会话不再启动实例；本插件自查 `config.toml` 后该轮内容 0 条被采集 | **禁用不停止已运行实例**，需插件自己守住开关 |
| H 关闭 app-server 的 stdin（正常退出） | 所有实例消失，**日志里没有任何 exit 记录**（来不及收到 EOF） | 随内核一起被终止 |
| I 重启 app-server | 无会话时 0 个；`thread/start` 后 1 个 | 重启后按会话重新拉起 |
| J 打开充值窗口（经审批）后强杀 app-server | 强杀前：2 个 MCP 实例 + 1 个窗口进程（MCP 实例的子进程）；10 秒内**全部消失**，包括窗口；Server 设为「父进程退出也不自退」仍被清掉 | Job Object（KILL_ON_JOB_CLOSE）兜底，不留孤儿；窗口随 Codex 一起结束 |

补充数字：assist.exe 收到 `initialize` 后 114 ms 应答；单实例常驻内存约 25 MB、13 个线程。

**这意味着什么**：MCP Server 的「存在」≠ 后台常驻能力。它的生命周期 = 会话加载期间；用户不打开 Codex 或没有加载会话时，没有任何本插件进程。被 Codex 结束时没有任何通知（不能依赖退出时的收尾逻辑），所以本原型的所有状态写入都是原子的、可重入的。

桌面 App 部分（退出 / 重启是否自动恢复会话、插件页开关、默认审批）需要在 App 中操作，清单见 `native-assist-manual-test.md`，结果待补。

## 3. 不用 Hooks 读取会话（实测）

- 权限：MCP Server 是普通用户进程，不受 Codex 沙箱约束（沙箱只管模型执行的命令），能读 `CODEX_HOME\sessions` 与 `archived_sessions`。
- **坑（实测）**：Codex 在会话加载期间一直持有 rollout 文件句柄，NTFS 只在句柄关闭时刷新目录项里的大小和修改时间——`dir` 看到的仍是创建时的大小。必须打开文件按句柄取长度，否则看不到新内容。
- 采集器设计：
  - 多个实例（每会话一个）用命名互斥量选出**唯一的采集者**；采集者被 Codex 结束后互斥量被遗弃，其他实例 5 秒内接手（单测覆盖）。
  - 每个 rollout 一个字节游标；只读完整行（Codex 写到一半的行留到下一轮）；按文件名记游标，会话被归档移动后不重复。
  - 授权时段：`consent.json` 记录每段开启时间。**只采集时间戳落在开启时段内的行**，所以关闭立即生效，关闭期间的内容以后也不会被补读。新文件按时间戳二分定位到首次授权时刻，不整读历史（2 万行的历史只读了不到 1000 行）。
  - 范围：全部会话，或只采集工作目录在指定文件夹内的会话。
  - 内容：白名单——用户与助手消息、工具调用与输出、推理**摘要**、回合起止、模型配置；丢弃 developer/system 指令、加密推理、`base_instructions`、账号与额度（`token_count`/`rate_limits`、`creator_account_id`）。所有字符串经脱敏（移植自记录器规则：API Key、令牌、私钥、Cookie、CDK / 兑换码等）。
  - 输出：第一轮为 JSONL；P4 起为插件数据目录下的 `assist.sqlite3`（事件按 `会话:ordinal:序号` 唯一），只在本机，本原型不上传。
- 授权：开启**只能**在原生「任务记录授权」窗口里由用户勾选同意后点击（没有任何可由模型调用的开启工具）；关闭有三条路：说「关闭任务记录」（`pause_recording`）、窗口中「关闭记录」、插件页禁用插件。
- 因为禁用插件不会停止已运行的实例（第 2 节 G），采集器每轮读取 `config.toml`（只读），发现本插件被禁用就停止采集；开窗口的工具也会拒绝。
- 实测（app-server，K/L 步）：授权后发一条含 `sk-proj-…` 的消息，模型回合结束、会话空闲时采集器就写出记录，密钥已脱敏；模型调用 `pause_recording` 后的新消息没有被采集。

## 4. CDK 充值原型（任务二）

流程：用户说「充值」→ 模型按 skill 调用 `open_recharge_window`（无参数）→ MCP Server 以独立进程拉起 `assist.exe recharge` 窗口并等待 → 窗口读取 Codex 登录账号（官方 `account/read`，只有邮箱、套餐、账号 ID）→ 首次确认绑定 → 用户输入 CDK → `POST /v1/redemptions`（模拟后台）→ 窗口显示结果 → 关闭后窗口进程打印一行**不含 CDK** 的结果 → 工具返回「兑换成功：ChatGPT Plus 1 个月（模拟）」之类的状态。

- UI：WinForms（.NET Framework 自带），单个 88 KB exe，无前端框架；窗口单实例（再次调用会把已开窗口提到前面并返回 `already_open`）；首次出现置顶，之后取消置顶。
- CDK 隔离：只存在于窗口进程内存。不是工具参数，不经过 MCP Server 进程，不写盘、不进日志、不进命令行；窗口中只显示掩码；返回给 Codex 的只有状态和商品名。用户若把 CDK 发在对话里，skill 要求模型不使用并提醒改在窗口中输入；采集器的脱敏规则也会处理这类文本。
- 后台交互沿用 `docs/recharge-api.md`（P1 定的接口约定与模拟后台）：结果未知时用同一请求重试并依赖后台按 `(cdk, 账号)` 幂等；区分「请求未发出（CDK 未使用）」与「结果未确认」；`processing` 轮询；不跟随重定向；绑定令牌用 DPAPI 加密保存；非本机地址必须 https。
- 实测（`scripts/native_window_e2e.py`，真实窗口 + 真实 `account/read` + 模拟后台，用 Win32 消息驱动窗口）：
  - 绑定 → `TEST-OK01` → 窗口「充值成功」，工具返回「兑换成功：ChatGPT Plus 1 个月（模拟）。」
  - `TEST-NOPE` → 窗口「无效」，工具返回 `cdk_invalid`；窗口开着时再次调用 → `already_open`
  - 打开后直接关闭 → `cancelled`
  - 泄漏检查：MCP Server 全部输出、生命周期日志、充值日志、授权与采集文件中都没有 CDK；发往后台的 URL 中没有 CDK。

## 5. 测试

| 类别 | 内容 | 结果 |
|---|---|---|
| 单元测试 `tests/test_native_assist.py` | MCP 握手 / 工具清单（CDK 不是任何工具参数、没有开启记录的工具）/ 错误码 / stdin 关闭退出；采集者选举与接手；采集器：未授权不采、授权时段与暂停、内容白名单与脱敏、半行与增量、Codex 持有句柄时的增长、文件夹范围、大文件二分定位、归档后不重复、插件被禁用即停（开窗口的工具拒绝）；兑换流程：绑定、成功、重复、格式错误、无效、过期、开通失败、处理中→成功 / 失败、超时后幂等重试、绑定失效、503、429、重定向、后台不可达、非 https 拒绝、测试入口默认关闭 | 20 个全部通过 |
| 生命周期探针 `scripts/native_lifecycle_probe.py` | 第 2 节 A–J，第 3 节 K–L | 通过 |
| 窗口端到端 `scripts/native_window_e2e.py` | 第 4 节 5 个场景（含授权窗口：未勾选时「开启记录」不可点） | 全部通过 |
| 桌面 App | `native-assist-manual-test.md` D1–D8 | 【待手测】 |

运行：`native\assist\build.cmd`；`set PYTHONPATH=plugins\codex-task-recorder\lib;plugins\codex-cdk-recharge\lib;tests` 后 `python -m unittest tests.test_native_assist`（或在 `tests` 目录下 `python -m unittest test_native_assist`）；`python scripts\native_lifecycle_probe.py --idle 300`；`python scripts\native_window_e2e.py`。

## 6. 限制与风险（如实说明）

1. **没有真正的后台常驻**：插件进程只在 App 有已加载会话时存在；Codex 关闭期间什么都不运行。官方插件机制里没有「随 App 启动、无会话也常驻」的入口；要做到只能装独立常驻程序或计划任务——按要求不做。补偿：下次运行时按授权时段补读。
2. **每会话一个实例**：开 10 个会话就是 10 个 assist.exe（每个约 25 MB）。采集用互斥量保证只有一个在干活；充值窗口单实例。
3. **直接改配置禁用插件时不会结束已运行实例**，已打开会话里的模型也仍能调用本插件的工具（内核实测；App 的插件页开关则会直接结束实例）。本插件自己读 `config.toml`（只读）守住开关：停止采集，两个开窗口的工具直接拒绝（「关闭记录」与状态查询仍可用）。P4 实测：实例运行时卸载会失败并留下残缺目录，已改为影子副本启动解决。
4. **Codex 结束 MCP Server 时不打招呼**（Job Object 直接终止）：窗口进程也在同一个 Job 中，**关闭 Codex 会连带关闭打开着的充值窗口**（实测，第 2 节 J）；正在兑换时被关的话，结果靠后台幂等，再次提交同一 CDK 即可确认，不会重复扣除。不用 `CREATE_BREAKAWAY_FROM_JOB` 让窗口脱离 Codex 的 Job——那等于留下 Codex 管不到的进程。
5. **入口依赖模型**：用户说「充值」后由模型决定调用工具，会消耗一次回合；模型偶尔可能不调用（skill 与 MCP instructions 已写明）。没有不经模型的按钮入口（MCP Apps / elicitation 能在 App 内出界面，但 CDK 会经过 Codex 的界面与协议，可能进入会话记录，本轮未采用）。
6. **rollout 不是稳定接口**：字段可能随 Codex 版本变化；采集器只解析白名单类型，未知类型直接跳过。
7. **充值工具需要用户批准一次**（实测）：因为如实标注了 `openWorldHint`。用户可选「本会话允许 / 始终允许」。本原型不通过改注解或在 `.mcp.json` 里设置 `default_tools_approval_mode` 来免掉这一步——那属于替用户放宽权限校验。若产品上要去掉这一步，应由用户在 Codex 中自行选择「始终允许」。
8. **采集开销**：采集者每 2 秒打开一遍所有 rollout 文件取长度（本机 29 个文件可忽略）；会话文件上千时应改为「近期文件高频、其余低频」或加 `FileSystemWatcher`。心跳日志 30 秒一行，5 MB 轮转。
9. **生产化前**：exe 需要代码签名（避免 SmartScreen / 杀软误报）；充值服务地址改为正式 https 地址并去掉 mock 标记；后台履约方式须单独做合规核查（`recharge-api.md` 第 0 节）。
