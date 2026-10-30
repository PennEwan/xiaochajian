# 固定充值入口验证（不经过模型的直接调用）

日期：2026-10-29　环境：Windows Codex App 26.1013.4894.0，内核 codex.exe 0.163.0-alpha.10.2，插件 codex-native-assist 0.3.0（入口验证）/ 0.3.1（原生卡片样式）

## 问题

能不能通过插件提供一个固定的 `/recharge` 入口：用户点击或选择后直接打开 CDK 充值窗口，不再依赖模型判断是否调用 `open_recharge_window`。

## 结论

| 方案 | 当前 App 是否支持 | 是否经过模型 | 依据 |
|---|---|---|---|
| 插件自带 `/recharge` 斜杠命令 | **不支持** | — | 本地插件的 commands 会被转换成 skill；`prompts/list` 只在 ChatGPT 连接器路径中使用 |
| 输入框 `/` 中选择插件 skill | 支持 | **经过模型** | 输入 `/cdk` 会出现「Codex Native Assist: Cdk Recharge」（skill）。选中后插入的是 skill 引用，发送后仍是一次模型对话，由模型决定是否调用工具 |
| **侧边栏 MCP App 入口 + 页面按钮** | **支持（已实测）** | **不经过模型** | 见下文 |
| 侧边栏 quickAction 小按钮 | 代码存在，当前界面不显示 | 不经过模型 | 只在 legacy 侧边栏布局中渲染；当前 App 使用导航栏（rail）布局，由服务端开关控制 |

**建议**：采用「侧边栏 MCP App 入口」作为固定充值入口，保留 cdk-recharge skill 作为自然语言兜底（用户说"充值"时仍可用）。不要尝试实现真正的 `/recharge` 斜杠命令。

## 实现方式（0.3.0，`native/assist/src/AppEntry.cs`）

- 新增工具 `recharge_launcher`，其 `_meta` 包含以下字段：
  - `ui.resourceUri = ui://native-assist/recharge.html`
  - `ui.visibility = ["app"]`
  - `openai/outputTemplate`
  - `openai/ui.entrypoints = [{type:"global", quickAction:{target:{type:"tool", name:"open_recharge_window", arguments:{wait:false}}}}]`
- MCP Server 增加 `resources` 能力：
  - `resources/list` 返回上述页面；
  - `resources/read` 返回页面 HTML，MIME 为 `text/html;profile=mcp-app`。
- 页面只有一个「打开充值窗口」按钮，通过 MCP Apps 桥接（`tools/call`）调用 `open_recharge_window`。页面**没有任何输入框**，CDK 仍然只在本地原生窗口中输入。

## 实测结果

### 真实 App

1. 插件安装后，「CDK 充值」出现在侧边栏「…」菜单中（带图标），可以固定到左侧导航栏。
2. 点击入口：App 渲染插件的 ui:// 页面（全页），同时为该页面启动一个 MCP 实例（父进程为 cmd.exe，即插件的 launch.cmd）。
3. 点击页面按钮：
   - 原生充值窗口在 **0.57 s** 内弹出；
   - **没有审批卡片，没有模型回合，没有新增 rollout 文件**；
   - 插件日志 `tool_request` 中 `tool=open_recharge_window`，`_meta` 仅有 `thread_id/threadId/progressToken`。
4. 当前界面中的 quickAction：鼠标悬停导航栏图标时只显示 tooltip；「…」菜单行只有固定按钮。代码分析结果如下：
   - quickAction 只在侧边栏「直达目的地」列表行中渲染；
   - 当 `navigationRailEnabled` 为 true 时，这个列表是空的（`m = o ? [] : p`）；
   - 布局取值为 `legacy` / `rail-only` / `rail-with-contextual-sidebar`，由服务端开关决定，用户无法设置。

### 内核 + Mock 模型（隔离的 CODEX_HOME，脚本为一次性探针）

- 发给模型的工具列表包含 `open_recharge_window` 等 5 个工具，**不包含 `recharge_launcher`**：`visibility:["app"]` 生效，模型看不到也调用不了这个入口工具。
- Mock 模型尝试调用 `recharge_launcher` 时没有匹配到工具，回合正常结束，没有产生 MCP 调用。
- app-server `mcpServer/tool/call`（App 页面按钮使用的通道）直接调用 `recharge_launcher`：
  - 结果为 `isError=false`；
  - 没有审批请求，没有模型请求；
  - 插件侧耗时 1 ms。

### 单元测试

`tests/test_native_assist.py` 共 33 项，全部通过。新增 `test_app_entry_launcher_and_resource`，覆盖以下内容：
- 入口元数据；
- `visibility=["app"]`；
- quickAction 的目标工具；
- `resources/list` 和 `resources/read`；
- 页面不含输入框；
- 未知 URI 返回 -32002。

## 限制与风险

- `openai/ui` 的 entrypoints/quickAction、MCP Apps 的 `ui://` 页面属于 App 内部能力，**没有公开文档**。本结论只对上面列出的版本成立，App 升级后需要重新验证（逆向依据：`app.asar` 中的 `app-initial-*.js`）。MCP Apps 功能开关当前为开启状态。
- 入口默认在「…」菜单里，用户需要自己固定到导航栏，插件无法代替用户固定。
- 打开入口页面会额外启动一个 MCP 实例（与线程实例相同，在 Job Object 中，App 退出时一并结束），不是常驻进程。
- 插件在 Codex 中被禁用后，入口随插件一起消失。已打开的线程仍保留旧实例，此时 `open_recharge_window` 会返回「本插件已在 Codex 插件页中禁用」。
- 该方案不改模型 Provider、不用 Hooks、不改信任状态。页面按钮走的是 App 自身的 `mcpServer/tool/call`，没有绕过权限校验。
- 未验证：
  - 输入 `/recharge` 能否筛出 skill（`/cdk` 已验证）；
  - `thread` 类型 entrypoint 的效果。

## 0.3.1：卡片改为小助手悬浮窗样式

- **原生充值窗口**（`RechargeWindow.cs` + `Card.cs`）沿用 `codex-cdk-recharge` 的设计数值（`paint.py`）：
  - 无边框圆角卡片，宽 340、圆角 30；
  - 极光底：145° 线性渐变 + 三团径向光，以 42% 叠在 `#edf3f8` 上；
  - 「C O D E X 充值」页眉、状态灯、✕ 关闭；
  - 套餐胶囊、圆角输入框、蓝色胶囊按钮；
  - 卡片随状态文字变高，可拖动，Esc 关闭。
  - 按钮和输入框仍是真实的 Win32 控件，键盘、BM_CLICK 和窗口自动化都照常可用。
  - 未就绪时按钮显示「重新读取」，与 Python 版一致。
- 侧边栏页面保持原样（按用户要求不改）。
- 实测：
  - `scripts/native_window_e2e.py` 5/5 通过：绑定、兑换成功、无效 CDK、重复打开、取消、授权窗口、CDK 泄漏检查；
  - `tests/test_native_assist.py` 33/33 通过；
  - 真实 App 中，点击侧边栏页面按钮后原生卡片弹出（工具耗时 237 ms），账号读取约 5.5 s 后进入「已绑定」状态；`recharge.log` 中不含 CDK 和邮箱。
- 升级注意：App 会让最早为侧边栏页面启动的插件实例一直运行。安装新版本后，该实例仍是旧版本，要等它被结束才会换成新版本。结束的方式有：
  - 在插件页关闭再打开本插件；
  - 重启 App；
  - 结束该实例进程。
