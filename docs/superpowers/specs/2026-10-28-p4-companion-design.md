# Codex 助手包（P4）— 一体化安装包设计说明

- 日期：2026-10-28；状态：用户已选定方案 A（Inno Setup + 自带 Python 运行时 + 合并为一个插件），按本文实施
- 前置：P1 充值（`2026-10-28-cdk-recharge-design.md`）、P2 记录器（`2026-10-28-codex-task-recorder-design.md`）、P3 数据同步（`2026-10-28-data-sync-design.md`）
- 接口约定：更新清单 `docs/update-api.md`；充值、同步沿用 `docs/recharge-api.md`、`docs/sync-api.md`

## 1. 目标与约束

把 P1 充值与 P3 授权数据同步整合为**一个** Windows 安装包「Codex 助手包」：用户下载、安装一次，完成插件启用、官方信任确认与充值账号绑定；需要充值时打开小窗口输入 CDK；开启任务记录后正常使用 Codex 即可记录并上传；不需要一直开着充值窗口；有清晰的采集状态、暂停入口与卸载方式。

技术约束：保留官方 Windows Codex App；只用原生插件、Hooks 和必要的本地组件；不改 Codex 核心文件；尽量少的安装步骤和依赖；自有组件可更新但须经用户授权；卸载时停止采集、清理后台任务并还原修改过的配置；插件禁用或授权撤销后独立组件不得继续采集；不影响原生模型选择、会话、工具调用与现有小助手。

用户决定（2026-10-28）：
- **一个开关**：任务记录与上传共用一份授权（同意 → 记录 + 上传；暂停 → 都停；撤销 → 都停，可删除后台数据）。本地仅记录模式只保留给开发者 CLI。
- **账号绑定 = 充值账号绑定**（P1 小窗口确认 ChatGPT 账号）。上传数据仍归属匿名设备，不关联账号。
- 名称：**Codex 助手包**，插件 ID `codex-companion@codex-companion`（插件 ID 是信任键的一部分，改名需重新信任）。
- 方案 A：Inno Setup 按用户安装（不需要管理员）+ 插件内置 Python 3.12 运行时 + P1/P3 合并为一个插件。

## 2. 前期实测结论（临时 `CODEX_HOME`，App 内核 0.163.0-alpha.10.2）

| 项目 | 结论 |
|---|---|
| 版本号与信任 | 插件版本 0.1.0 → 0.2.0：缓存路径变化，**Hook 哈希不变**（按 hooks.json 模板计算，不含替换后的路径） |
| 自带运行时 | `& "${PLUGIN_ROOT}\runtime\python.exe" "${PLUGIN_ROOT}\hooks\x.py"` 被内核接受，并在 PowerShell 下执行插件缓存中的私有 python.exe；`decision: block` 生效；Hook 端到端 301 ms |
| 卸载残留 | `codex plugin remove` + `marketplace remove` 后，`config.toml` 中 `[hooks.state."<插件>:…"] trusted_hash` **仍然保留** → 卸载程序须自己清理 |
| 插件禁用 | `[plugins."…"] enabled = false` 时 `hooks/list` 不再列出该插件的 Hook |
| 关闭单个 Hook | `[hooks.state."<键>"] enabled = false` → `hooks/list` 中 `enabled: false` |
| 原地刷新 | 同版本 `codex plugin add` 在私有运行时被进程占用时仍成功（0.07 s，只写有变化的文件）；旧版本目录被删除 |
| 禁用后再 add | `codex plugin add` 会把用户禁用的插件**改回 `enabled = true`** → 安装 / 更新须记住并恢复用户的选择 |
| 环境变量 | Hook 进程继承 app-server 的环境变量 |

## 3. 安装包与目录

产物：`dist\CodexCompanion-Setup-<版本>.exe`（Inno Setup，按用户安装，中文向导，约 10 MB）。

```
%LOCALAPPDATA%\Programs\CodexCompanion\            安装目录（固定，不可选）
  marketplace\.agents\plugins\marketplace.json     本地插件市场 codex-companion
  marketplace\plugins\codex-companion\              插件（安装时由 Codex 复制到其插件缓存）
    .codex-plugin\plugin.json                       版本号在 1.x 内固定为 1.0.0（原地刷新、路径不变）
    hooks\hooks.json                                12 个事件 → 同一入口
    hooks\hook.py                                   入口：任何异常 exit 0；库加载失败时仍拦截 /充值
    runtime\                                        私有 Python 3.12（含 Tk），python312._pth 指向 ..\lib
    lib\ctr  lib\cdkr  lib\companion                记录与同步 / 充值 / 助手包（互不引用 ctr↔cdkr）
    product.json                                    产品版本、插件 ID、后台地址、兼容范围
  unins000.exe                                      卸载程序（「应用和功能」）
开始菜单\Codex 助手包\  Codex 助手包（控制面板）、Codex 充值、卸载 Codex 助手包
```

数据目录沿用：`%LOCALAPPDATA%\CodexTaskRecorder`（记录与同步）、`%LOCALAPPDATA%\CodexCdkRecharge`（充值）；新增 `%LOCALAPPDATA%\CodexCompanion`（授权、更新状态、配置备份、安装日志）。

### 3.1 私有运行时（构建时用本机 CPython 3.12 x64 生成）
- 根目录：`python.exe`、`pythonw.exe`、`python312.dll`、`python3.dll`、`vcruntime140*.dll`、`LICENSE.txt`。
- `DLLs\`：全部 `.pyd` / `.dll`，去掉 `_test*`、`_ctypes_test`、`_msi`、`_sqlite3`、`sqlite3.dll`、`winsound`。
- `python312.zip`：标准库编译后的 `.pyc`（无源码重新编译开销），去掉 `test`、`idlelib`、`turtledemo`、`ensurepip`、`venv`、`lib2to3`、`pydoc_data`、`site-packages`、`sqlite3`。
- `tcl\`：`tcl8.6`（去掉 `tzdata`）、`tk8.6`（去掉 `demos`）、`tcl8`。
- `python312._pth`：`python312.zip`、`DLLs`、`..\lib`；不导入 `site`（启动更快，`PYTHONPATH` 被忽略——拉起子进程统一用 `-m`，依赖 `._pth` 找到 lib）。

### 3.2 Hook 定义
每个事件一条 `commandWindows`：`& "${PLUGIN_ROOT}\runtime\python.exe" "${PLUGIN_ROOT}\hooks\hook.py"`；异步与超时沿用记录器（UserPromptSubmit 同步 10 s、Interrupt / SessionEnd 同步 3 s，其余 async）。

## 4. 组件（`lib/companion`）

| 模块 | 职责 |
|---|---|
| `product.py` | 读取插件根目录的 `product.json`；开发时用默认值 |
| `dispatch.py` | Hook 分发：UserPromptSubmit 先识别 `/充值`（交给 `cdkr.trigger`）与 `/记录`（打开控制面板），命中则拦截、不再记录；否则交给 `ctr.hook.handle`（使用助手包的提示文案）；附加更新提示；按需拉起更新检查。**每个事件只启动一个 Python 进程**（原先充值与记录各一个） |
| `codexcfg.py` | `CODEX_HOME` 定位；读取 `config.toml`（插件安装 / 启用、Hook 关闭 / 信任）；**精确删除**本插件的表并校验 |
| `compat.py` | App 版本（`Get-AppxPackage OpenAI.Codex`）、内核版本、能力探测 → 结论与降级提示 |
| `setup.py` | 安装 / 修复 / 卸载 / 升级前停止进程 的步骤（Inno 调用） |
| `authz.py` | 一个开关：同意并开启、暂停、恢复、撤销（可删后台 / 本地数据）、汇总状态；调用 `ctr.state` 与 `ctr.sync` 现有接口 |
| `procs.py` | 结束本产品的进程（可执行文件位于安装目录或插件缓存下的 python/pythonw，排除自身） |
| `update.py` | 更新清单、24 小时节流的后台检查、下载与 SHA-256 校验、启动安装程序 |
| `panel.py` | 控制面板（tkinter，沿用充值窗口的卡片外观 `cdkr.paint`） |
| `cli.py` / `__main__.py` | `python -m companion …`（安装程序、开始菜单、测试脚本使用） |
| `log.py` | 只记固定标识与数字 |

对现有模块的修改（保持向后兼容，开发版插件行为不变）：
- `ctr.hook.handle(..., notices=None)`：提示文案可由宿主传入；`not_enabled` 为 `None` 时不提示（助手包用户可能只用充值）。
- `ctr.sync.hostgate`（新）：上传进程判断宿主插件是否仍被允许运行（第 6 节）；`worker` 每一轮检查，`launcher.kick` 拉起前检查。
- `cdkr`：无改动（窗口、流程、Hook 触发逻辑原样复用）。

## 5. 流程

### 5.1 安装（全新）
1. Inno 复制文件 → 运行 `runtime\python.exe -m companion setup install --result <tmp>\result.json`：
   1. 找 App 内核；找不到 → `no_codex`（文件已安装，插件未注册；提示安装 / 打开一次 Codex 后在控制面板点「重新检测」）。
   2. 兼容检查（第 8 节）；`incompatible` → 不注册插件，给出降级提示。
   3. 备份 `config.toml` 到 `%LOCALAPPDATA%\CodexCompanion\backup\`。
   4. 发现开发版插件（`codex-task-recorder@ctr-local` / `codex-cdk-recharge@ctr-local`）→ 结果中列出；Inno 询问是否卸载（否则会重复记录、`/充值` 弹两次）→ `setup remove-legacy`。
   5. `codex plugin marketplace add <安装目录>\marketplace`（同名市场指向别处时先移除）→ `codex plugin add codex-companion@codex-companion`。
   6. 把产品配置的充值 / 同步后台地址写入两个模块的配置（同步：本设备已在别的后台注册时不覆盖，面板提示）。
   7. `hooks/list` 确认 12 个 Hook 已加载，记录信任数。
2. Inno 根据结果显示提示（兼容性警告 / 降级提示 / 开发版插件）；完成页勾选「打开 Codex 助手包」。
3. 「重新检测」= 修复：插件未注册而 Codex 兼容时重新注册；Codex 不兼容而插件仍注册时停用插件。面板从插件缓存运行（`/记录`）时，安装目录取自安装时记录的位置。
4. 控制面板引导清单（实时）：① 检测到 Codex ② 插件已安装 ③ **重启 Codex → 插件 → Codex 助手包 → 点「全部信任」**（用户亲自确认，本产品不写信任项）④ 绑定充值账号（打开充值窗口）⑤ 任务记录：未开启 →「开启」。

### 5.2 Hook（每次）
`hook.py` → `companion.dispatch`：
1. UserPromptSubmit 且提示词以 `/充值`、`／充值` 开头 → `cdkr.trigger.handle`（拉起窗口、block）；以 `/记录`、`／记录` 开头 → 拉起控制面板、block。被拦截的提示词**不进入记录器**（CDK 根本不会到达记录器）。
2. 否则 `ctr.hook.handle(payload, notices=助手包文案)`：未授权时不记录、不提示；授权时与 P3 完全相同（记录、同步提示、拉起上传进程）。
3. 有新版本且未关闭更新检查 → 在提示中附一句（每天最多一次）。
4. 更新检查 24 小时未做 → 分离拉起 `pythonw -m companion update check`（Hook 本身不联网）。
5. 任何异常吞掉，exit 0。

提示文案：
- 记录并上传中：`● Codex 助手包正在记录本会话并上传（你已授权）。查看或暂停：发送 /记录，或打开开始菜单「Codex 助手包」`
- 只在本机记录（同步被后台停用等）：`● Codex 助手包正在本机记录本会话（当前未上传）。查看或暂停：发送 /记录`

### 5.3 授权（一个开关，`authz`）
| 操作 | 效果 |
|---|---|
| 开启 | 显示合并的授权说明（第 9 节）→ 同意 → `ctr.state.enable` + 首次同意时上传游标从当前末尾开始 + `sync.state.enable(后台地址)` + 拉起上传进程；记录助手包同意版本 |
| 暂停 | `ctr.state.disable` + `sync.state.pause`：之后不记录、不上传；已在队列中的内容留在本机 |
| 恢复 | 同意版本未变时一键恢复（两者都开） |
| 撤销 | `ctr.state.disable` + `sync.state.revoke(可选删除后台)` + 立即尝试送达（拿到上传进程锁，最多等 5 s）；可选删除本机记录；清除同意记录；如实显示后台结果 |

插件在 Codex 中被禁用时，「开启 / 恢复」给出提示：先在 Codex 中启用插件。

### 5.4 卸载（「应用和功能」或开始菜单）
Inno 卸载程序先询问：①「是否撤销授权并删除后台已上传的数据？」②「是否删除本机记录、充值绑定与设置？」（静默卸载：都选否），再运行 `setup uninstall [--delete-remote] [--purge-local]`：
1. 暂停授权；选了 ① 或选了 ② 且设备已注册 → 撤销并立即送达；离线送不到 → 如实提示。卸载后不保留任何常驻组件；若同时选了 ②，只保留发送撤销所需的同步状态、设备凭证与后台地址，**联网后重新安装即自动发送**。
2. 停止上传进程（等锁 5 s，超时结束进程）、控制面板、充值窗口、更新检查（`procs`）。
3. `codex plugin remove` → `codex plugin marketplace remove codex-companion`（App 内核不在时跳过）。
4. 精确删除 `config.toml` 中残留的 `[hooks.state."codex-companion@codex-companion:…"]`、`[plugins."codex-companion@codex-companion"]`、`[marketplaces.codex-companion]` 表；先备份；删后用 `tomllib` 解析并确认没有本产品的键，否则还原原文并报告。
5. 删除残留的插件缓存与插件数据目录（`~/.codex/plugins/cache/codex-companion`、`~/.codex/plugins/data/codex-companion-codex-companion`）。
6. 选了 ② → 删除三个数据目录（例外见第 1 步）；否则保留并告知位置。配置已还原并校验通过后，删除安装 / 卸载时留下的 config.toml 备份（其中可能有密钥）。
7. Inno 删除安装目录、开始菜单、卸载登记。

本产品**不创建**计划任务、服务、开机启动项；上传进程与更新检查只在需要时由 Hook / 面板拉起，空闲自动退出。

### 5.5 升级与更新（`update`）
- 设置（面板）：`notify`（默认：检查并提示，用户点击才安装）/ `auto`（用户主动开启：后台下载校验后静默安装；需要重新信任的版本仍需点击）/ `off`（不检查）。
- 检查：Hook 每 24 小时最多拉起一次后台检查；打开面板时检查。请求只带 `User-Agent: CodexCompanion/<版本>`，不带任何标识。
- 清单（`docs/update-api.md`）：`version`、`url`（https）、`sha256`、`size`、`min_kernel`、`requires_retrust`、`notes`。
- 下载到 `%LOCALAPPDATA%\CodexCompanion\updates\`，校验大小与 SHA-256 后才执行；运行 `setup.exe /SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE=1`。
- 安装程序 `PrepareToInstall`：用旧版运行时执行 `setup prepare-upgrade`，只停止常驻的 `pythonw` 进程（面板、充值窗口、上传进程、更新检查）；Hook 进程（`python.exe`，在 Codex 的插件缓存中）不受影响。之后同 5.1，但保留授权与数据，且不再询问开发版插件；插件版本号不变 → 原地刷新，不需要重新信任（`hooks.json` 改动时清单标 `requires_retrust`，面板提前告知）。
- 用户在 Codex 中禁用了插件时更新：`plugin add` 会重新启用（实测），安装步骤随后通过 `config/value/write` 恢复禁用，并且不把"Hook 未加载"误判为不兼容。
- `/UPDATE=1`（用户在面板点击）安装后重新打开面板；`/UPDATE=auto`（自动更新）不打开、不抢焦点；充值窗口开着时自动更新推迟到下次检查。`SetupMutex` 防止两个安装程序同时运行。

## 6. 插件禁用 / 授权撤销后不再采集（`ctr.sync.hostgate`）

上传进程每轮（2 s）读取 `config.toml`（按修改时间缓存）判断宿主插件：
- 宿主插件 ID：插件根目录 `product.json` 的 `plugin_id`；没有时从缓存路径推断（`…\plugins\cache\<市场>\<插件>\<版本>\lib`）；都没有（开发时从仓库运行）→ 不设门槛。
- 不允许运行：插件不在配置中（已卸载）、`enabled = false`（已禁用）、本插件任一 Hook `enabled = false`（被关闭）、任一 Hook 没有 `trusted_hash`（未信任 / 信任被撤销）、配置读不了（本轮按不允许处理）。
- 不允许时：不组批、不注册、不上传，状态写入原因，上传进程退出；`launcher.kick` 也不拉起。待送达的撤销 / 删除请求仍会发送（这是用户的删除请求，不是采集）。
- Hook 本身：插件禁用或未信任时 Codex 不会调用，因此不会产生新记录。
- 授权撤销 / 暂停：P3 已有，上传进程 2 秒内退出。
- 已知边界：Hook 定义被改动（`modified`）只能由 `hooks/list` 判断，上传进程不调用 app-server；面板会显示。

## 7. 控制面板（`panel`）

无边框圆角卡片（沿用充值窗口外观），宽 380，可拖动，单实例（命名互斥量）。
- 状态行：Codex（App / 内核版本、兼容结论）；插件（已启用 / 已禁用 / 未安装）；Hook（已信任 n/12 / 待信任 → 指引）；充值账号（已绑定邮箱 / 未绑定）；任务记录（● 记录并上传中 / ○ 已暂停 / 未开启；待上传、已上传、最近上传、最近错误；上传进程是否运行、被宿主门槛拦下的原因）；版本与更新。
- 按钮：开启 / 暂停 / 恢复任务记录；撤销授权…；充值；检查更新 / 更新到 x.y；更新方式；重新检测；卸载…（打开卸载程序）。
- 读取状态（含 `hooks/list` 约 3 s、`account/read`）在工作线程中进行，界面只在主线程更新（同充值窗口）。

## 8. 版本兼容与降级提示（`compat`）

`product.json`：`min_kernel`（已测最低，0.163.0）、`tested_kernels`、`tested_apps`。

| 结论 | 条件 | 行为与提示 |
|---|---|---|
| `ok` | 内核版本在已测列表 | 正常 |
| `untested` | 高于最低版本但未测 | 正常安装；提示「当前 Codex 版本未经验证，如遇问题可在面板暂停任务记录」 |
| `incompatible` | 低于最低版本；或缺少 `plugin` 命令 / `hooks/list`；或安装后 Hook 未加载 | 不注册（或移除）插件；**降级提示**：「当前 Codex（内核 X）不支持本助手包需要的插件 Hooks：/充值 与任务记录不可用，已停用插件。仍可从开始菜单打开「Codex 充值」（如果账号读取可用）。升级 Codex 后在面板点「重新检测」即可恢复。」 |
| `unknown` | 读不到内核版本（超时、App 正在更新） | 不做任何改动，不阻止记录；提示稍后重新检测 |
| `no_codex` | 找不到 App 内核 | 提示安装 / 打开一次 Codex 后重新检测 |

面板每次打开都重新检测；Codex 自动升级后若变为不兼容而插件仍注册，面板说明情况并提供出路：「重新检测」停用插件（或先「暂停任务记录」——暂停在任何状态下都可用）。降级提示中"已停用插件"只在插件确实被停用后显示。

## 9. 授权说明（开启任务记录时显示）

合并 P2 记录说明与 P3 同步说明：记录与上传的内容清单、不采集与脱敏清单、后台地址、匿名设备身份、与充值无关、暂停 / 撤销 / 删除方式（面板与 `/记录`）、脱敏局限提示。同意版本 `p4-1`；文本变化需重新同意。

## 10. 构建（`packaging/`）

`python packaging\build.py --version 1.0.0 [--profile mock|prod] [--recharge-url …] [--sync-url …] [--update-url …]`：
1. 生成私有运行时（3.1），缓存到 `build\runtime-3.12.x\`。
2. 组装插件：`plugins/codex-companion`（清单、hooks、`lib/companion`）+ `lib/ctr` + `lib/cdkr` + 运行时 + 生成的 `product.json`；生成市场清单。
3. 用私有运行时冒烟测试（导入全部模块、Tk 创建窗口后销毁、Hook 入口处理一条合成事件）。
4. ISCC 编译 `packaging/companion.iss` → `dist\CodexCompanion-Setup-<版本>.exe`，输出 SHA-256。

构建依赖（只在构建机上）：Python 3.12 x64（含 Tk）、Inno Setup 6。用户机器不需要任何依赖。

## 11. 测试与验收

### 11.1 单元测试（unittest）
dispatch（`/充值`、`/记录` 拦截且不进入记录器；普通提示交给记录器；文案；更新提示；异常 exit 0；只读一次 stdin）、codexcfg（插件 / Hook 状态解析、精确删除表、坏文件、校验失败回滚）、hostgate（各种拒绝原因、缓存、开发模式无门槛）与 worker / launcher 集成、compat（各结论）、setup（假内核：安装、市场冲突、开发版插件、卸载顺序、配置还原、内核缺失）、authz（开启 / 暂停 / 恢复 / 撤销与 ctr / sync 状态一致、插件禁用时拒绝开启）、update（清单校验、版本比较、节流、SHA-256 失败不执行、模式）、panel（真实 Tk 控件，同充值窗口测试做法）、运行时清单。

### 11.2 自动验收（`scripts/p4_acceptance.py`，临时 `CODEX_HOME` + 真实 App 内核 + 模拟后台；被拦截的提示词不调用模型）
| 场景 | 方法 |
|---|---|
| 全新安装 | 静默运行真实安装包 → 市场 / 插件已注册、12 个 Hook 已加载；记录安装前后 `config.toml` |
| 充值成功 | `/充值` 经 Hook 拉起窗口（互斥量出现）、提示词被拦截；窗口流程对模拟后台兑换成功（窗口自动化） |
| 授权开启与关闭 | 开启 → 跑回合 → 模拟后台收到事件；暂停 → 无新事件；撤销并删除 → 后台清空 |
| 插件关闭、重启 | `enabled = false` → Hook 不再加载、上传进程 2 s 内退出且不上传；恢复 → 继续 |
| Codex 重启 | 关闭并重新启动 app-server → Hook 仍可信、记录与上传继续 |
| 插件卸载与恢复 | 静默卸载 → `config.toml` 与安装前一致、无残留进程 / 目录；重新安装 → 恢复 |
| 网络异常 | 模拟后台停机 / `fail-next` → 本地排队、恢复后补传无重复；更新检查失败不打扰 |
| 版本不兼容 | 假内核报告旧版本 → 不注册插件、给出降级提示；报告未测新版本 → 警告后正常 |

信任步骤：在临时 `CODEX_HOME` 中由脚本写入 `trusted_hash`，模拟用户点击（仅限临时目录）。

### 11.3 桌面 App 实测（需用户参与）
在真实环境：先备份并卸载开发版 → 安装 → 用户在 App 中「全部信任」→ `/充值` 绑定并兑换（模拟后台）→ 开启任务记录、完成一个真实任务、后台收到 → 在 App 中禁用插件 / 重新启用 → 重启 App → 卸载 → 核对 `config.toml` → 重新安装。清单见 `docs/p4-manual-test.md`。

### 11.4 交付物
安装包、`docs/p4-install-guide.md`（安装说明）、`docs/p4-test-report.md`（测试报告与已知限制）、`docs/update-api.md`、`docs/p4-manual-test.md`、README 更新。

## 12. 风险与已知限制
| 项目 | 说明 |
|---|---|
| 未签名 | 没有代码签名证书：SmartScreen 显示「未知发布者」，需点「仍要运行」 |
| 后台未实现 | 充值、同步、更新三个后台都只有接口约定与模拟后台 |
| 信任无法自动化 | 用户必须在 App 中亲自点「全部信任」；`hooks.json` 改动后需重新信任 |
| 离线卸载 | 选择删除后台数据但离线时无法送达，且卸载后不保留常驻组件补发 |
| `modified` 检测 | 上传进程只能从配置判断「未信任 / 已关闭」，Hook 定义被改动只有面板能看到 |
| 每条提示词的开销 | UserPromptSubmit 无 matcher：每条提示词运行一次入口（合并后约 0.3–0.4 s，原来两个 Hook 各一次） |
| Codex 自动升级 | 内核升级后的兼容性只能在下次打开面板 / 安装时检测；Hook 入口对未知字段宽容，异常一律 exit 0 |
