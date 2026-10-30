@echo off
rem Codex starts this plugin's collector MCP server through cmd.exe (the same way Codex's own bundled
rem plugins do), so the launcher path stays stable whatever the current directory is. The plugin
rem folder itself is only ever read: the collector keeps its data under %LOCALAPPDATA%.
rem Keep this file ASCII-only: cmd.exe reads batch files in the OEM code page, and one non-ASCII
rem byte shifts the parser so that every line below it loses its first characters.
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "MCP_LIB=%PLUGIN_ROOT%\lib"
rem A dev checkout has no runtime\ and keeps ctr / cdkr in their own plugin trees.
if exist "%PLUGIN_ROOT%\..\codex-task-recorder\lib" set "MCP_LIB=%MCP_LIB%;%PLUGIN_ROOT%\..\codex-task-recorder\lib"
if exist "%PLUGIN_ROOT%\..\codex-cdk-recharge\lib" set "MCP_LIB=%MCP_LIB%;%PLUGIN_ROOT%\..\codex-cdk-recharge\lib"
if defined PYTHONPATH set "MCP_LIB=%MCP_LIB%;%PYTHONPATH%"
set "PYTHONPATH=%MCP_LIB%"
set "PYTHONIOENCODING=utf-8"
rem Packaged builds carry their own private CPython; a dev checkout uses the python on PATH.
set "PY=%PLUGIN_ROOT%\runtime\python.exe"
if exist "%PY%" goto launch
set "PY=python"
:launch
rem Codex starts us with the plugin folder as working directory, and a process's working directory
rem cannot be deleted, so move away from it: the plugin must stay removable while the server runs.
if defined USERPROFILE cd /d "%USERPROFILE%"
rem .mcp.json passes "mcp" as the subcommand; both "mcp" and no argument start the same server.
set "MCP_ARGS=%*"
if /i "%MCP_ARGS%"=="mcp" set "MCP_ARGS="
"%PY%" -m collector mcp %MCP_ARGS%
set "MCP_RC=%ERRORLEVEL%"
exit /b %MCP_RC%
