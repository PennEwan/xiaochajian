@echo off
rem Scheduled-task launcher for the collector (CodexCompanion\Collector, at logon, per user).
rem Kept ASCII-only: cmd.exe reads batch files in the OEM code page and one non-ASCII byte
rem shifts the parser for every line below it.
rem Nothing here needs the Codex plugin to be installed or trusted: the collector reads the
rem rollout files Codex writes itself and uploads to the backend named in collector-config.json.
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "COLLECTOR_LIB=%PLUGIN_ROOT%\lib"
rem A dev checkout keeps ctr / cdkr / csr outside this plugin tree.
if exist "%PLUGIN_ROOT%\..\codex-task-recorder\lib" set "COLLECTOR_LIB=%COLLECTOR_LIB%;%PLUGIN_ROOT%\..\codex-task-recorder\lib"
if exist "%PLUGIN_ROOT%\..\codex-cdk-recharge\lib" set "COLLECTOR_LIB=%COLLECTOR_LIB%;%PLUGIN_ROOT%\..\codex-cdk-recharge\lib"
if exist "%PLUGIN_ROOT%\..\..\tools\session-reader\csr" set "COLLECTOR_LIB=%COLLECTOR_LIB%;%PLUGIN_ROOT%\..\..\tools\session-reader"
if defined PYTHONPATH set "COLLECTOR_LIB=%COLLECTOR_LIB%;%PYTHONPATH%"
set "PYTHONPATH=%COLLECTOR_LIB%"
set "PYTHONIOENCODING=utf-8"
rem Packaged builds carry a private CPython with pythonw.exe (no console window).
set "PY=%PLUGIN_ROOT%\runtime\pythonw.exe"
if exist "%PY%" goto launch
set "PY=pythonw"
if exist "%PLUGIN_ROOT%\runtime\python.exe" set "PY=%PLUGIN_ROOT%\runtime\python.exe"
:launch
rem A process's working directory cannot be deleted, and the install directory must stay
rem removable while the collector runs.
if defined TEMP cd /d "%TEMP%"
"%PY%" -m collector run %*
exit /b %ERRORLEVEL%
