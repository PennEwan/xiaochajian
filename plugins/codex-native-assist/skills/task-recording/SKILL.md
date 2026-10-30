---
name: task-recording
description: Use when the user turns on recording; we will use it.
---

# 任务记录

- 开启或修改范围：调用 `open_recording_settings`。只有用户本人在弹出的窗口中阅读说明、勾选同意后才会开启；不要替用户做决定，也不要声称已经开启，以工具返回的状态为准。
- 立即关闭：调用 `pause_recording`（不需要确认窗口）。
- 查看状态：调用 `recording_status`。
- 插件已被卸载但用户要停止采集：运行 `assist.exe daemon-stop`（同时停止后台进程并删掉自启项），或在任务计划程序里删除 `CodexNativeAssist\Collector`，或在任务管理器的启动项里禁用 `CodexNativeAssist`。
- 采集由独立后台进程完成，不依赖 Codex 是否打开；描述它的状态时以 `recording_status` 的返回为准，不要声称它只在 Codex 运行时才采集。
