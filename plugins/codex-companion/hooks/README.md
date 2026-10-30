# 这些 Hook 已经不随产品发布

`plugin.json` 里已没有 `"hooks"`，`packaging/build.py` 也不再打包这个目录。
插件现在只有一个 MCP 服务器 `companion_collector`（见 `../.mcp.json`）。

保留这些文件只为一件事：`tests/test_companion_dispatch.py` 仍然直接调用 `dispatch.py`，
用来保证开发版（`codex-task-recorder`）那套 Hook 的回归不被破坏。
产品里不会再有任何 Hook 被 Codex 执行，因此也没有「逐条信任 Hook」这件事。
