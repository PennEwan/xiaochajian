@echo off
rem Codex starts this plugin's MCP server through cmd.exe (like Codex's own bundled plugins) so that no
rem file inside the plugin folder stays in use while it runs: assist.exe is copied once per build into
rem Codex's plugin data folder and started from there. Uninstalling a plugin whose exe is running fails
rem on Windows and leaves a half-deleted plugin folder (measured with the Codex App).
setlocal
set "ASSIST_PLUGIN_ROOT=%~dp0.."
set "SHADOW="
for /f "usebackq delims=" %%P in (`"%~dp0assist.exe" shadow`) do set "SHADOW=%%P"
if not defined SHADOW set "SHADOW=%~dp0assist.exe"
rem Codex starts us with the plugin folder as working directory; a process's working directory cannot be
rem deleted, so move away from it before starting the server (which inherits the new one).
cd /d "%USERPROFILE%"
"%SHADOW%" %*
