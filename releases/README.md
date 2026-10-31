# CDK 充值（codex-native-assist）发布包

`CDKRecharge-<版本>.zip` 是插件的完整分发目录，内容：

```
.agents/plugins/marketplace.json      marketplace 声明（名字 native-assist）
install.cmd / uninstall.cmd           安装、卸载
plugins/codex-native-assist/
    .codex-plugin/plugin.json         插件清单（显示名「CDK 充值」）
    .mcp.json                         MCP 服务器入口
    plugin-config.json                充值 / 上传地址
    bin/assist.exe                    采集器 + 充值窗口（同一个 exe）
    bin/launch.cmd
```

## 安装

1. 解压到任意目录（整个文件夹要留着，`uninstall.cmd` 依赖它自己的路径）。
2. 双击 `install.cmd`：依次执行 `codex plugin marketplace add` 和 `codex plugin add codex-native-assist@native-assist`。
3. 完全退出 Codex（含托盘图标）再打开，侧边栏会出现「CDK 充值」入口。

## 卸载

双击 `uninstall.cmd`：停后台采集器、删除登录自启、`plugin remove`、`marketplace remove`。

注意：在 Codex 插件页直接移除插件**不会**停止后台采集——采集器是独立进程 + 计划任务
`CodexNativeAssist\Collector`，要停就用 `uninstall.cmd`，或运行
`assist.exe pause` / `assist.exe daemon-stop`。

## 当前状态

- 版本：0.5.2
- `plugin-config.json` 里的 `recharge_base_url` / `sync_base_url` 指向本机 mock 服务
  （`127.0.0.1:18787` / `127.0.0.1:8080`），换成真实后台后才能给终端用户使用。
- 模型侧不暴露任何工具；充值入口只有 Codex 侧边栏。
