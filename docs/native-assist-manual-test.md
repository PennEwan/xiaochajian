# Codex 原生助手（PoC）桌面 App 验收清单

状态：P4（2026-10-29）已在真实 Codex 桌面 App 中执行，结果见 `docs/native-p4-acceptance-report.md` 第 2 节。下面的清单可用于以后的版本复测。

前提：
- 插件经本地市场安装：`codex plugin marketplace add <本仓库>` → `codex plugin add codex-native-assist@ctr-local`（用 App 自带内核 `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe`）。
- 模拟充值后台：`python scripts\mock_recharge_server.py`（`http://127.0.0.1:18787`）。
- 只读观察（可选）：`python scripts\native_desktop_watch.py --seconds 3600 --every 2`，记录插件进程、内存和生命周期日志到 `.testdata\native-desktop\watch.jsonl`。
- 插件数据：`%USERPROFILE%\.codex\plugins\data\codex-native-assist-ctr-local\`（`assist.sqlite3`、`lifecycle.jsonl`、`recharge.log`）。

| # | 操作 | 预期 |
|---|---|---|
| D1 | 文件 → 退出（或 Ctrl+Q） | 插件进程在 1–2 s 内全部消失 |
| D2 | 重新打开 App，不操作 1 分钟 | 可能出现 2–3 个各存活数秒的探测实例，之后无会话就无进程 |
| D3 | 自定义 → 插件 → Codex 原生助手（PoC） | 显示 MCP 服务器与 2 个技能；没有任何 Hook 信任提示 |
| D4 | 新聊天发送「充值」 | 「请求批准」模式出现一次审批卡片；窗口弹出在最前面 |
| D4.1 | 窗口内确认账号 → 输入 `TEST-OK0x` → 确认兑换 → 关闭 | 窗口与 Codex 都显示兑换成功；对话、`~/.codex` 全部文件中搜不到该 CDK |
| D5 | 不操作 20 分钟以上 | 已加载会话的实例一直存活（心跳不断） |
| D6 | 「任务记录设置」→ 勾选同意 → 开启；再发一句普通消息 | 未勾选不能开启；普通消息 0–2 s 内入库 |
| D6.1 | 「关闭任务记录」；再发一句 | 约 1–2 s 内采集停止；之后的消息不入库 |
| D7 | 设置 → 插件 → 关闭本插件开关；在授权目录发一句；再打开开关，再发一句 | 关闭时实例全部结束；关闭期间的消息重新打开后也不入库；打开后的消息正常入库 |
| D8 | 再次退出并重启 | 同 D1 / D2 |
| 卸载 | 设置 → 插件 → 本插件 → 卸载（有会话在用插件时） | 卸载成功；`plugins\cache\…` 与配置项被删除；插件数据目录保留 |

自动化辅助（App 已打开时使用，操作真实 App 的界面）：
- `scripts\native_app_multisession.py --sessions 5 --in-project 3`：5 个会话并发、范围隔离、重复检查。
- `scripts\native_app_toggle_check.py`：插件页开关前后的采集检查（依赖界面布局，必要时按截图调整坐标）。
- `scripts\native_app_coverage.py --scope "1=all" --scope "2-=D:\ctr-testbed"`：与独立解析器逐条比对，计算覆盖率 / 缺失率 / 重复率。
