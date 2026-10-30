@echo off
rem ===========================================================================
rem ctrsrv Windows launcher.
rem
rem ASCII ONLY. cmd.exe parses .cmd files in the OEM code page; one non-ASCII
rem byte desynchronises every line below it. Do not paste Chinese here.
rem
rem What it does:
rem   1. PYTHONPATH := the directory of this file (it contains the ctrsrv package)
rem   2. cwd := %LOCALAPPDATA%\ctrsrv   because --db defaults to the relative
rem      path data\ctrsrv.sqlite3; this keeps the database out of the checkout
rem   3. run: python -m ctrsrv serve --host 127.0.0.1 --port 8080
rem
rem Configuration: the server reads CTRSRV_HOST / CTRSRV_PORT / CTRSRV_ADMIN_TOKEN
rem from the environment; a flag overrides the env var. This launcher pins
rem --host/--port to loopback and does NOT pass --admin-token, so the token stays
rem in the environment and never appears on a command line (Task Manager /
rem `wmic process`). Unset token means every /admin/* answers 403 admin_disabled.
rem NOTE: CTRSRV_DB is not honoured through the CLI (--db always carries a
rem default), so pass --db explicitly if the database must live elsewhere.
rem
rem Usage: server\serve.cmd
rem ===========================================================================
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "PYTHONPATH=%HERE%"

if not exist "%LOCALAPPDATA%\ctrsrv" mkdir "%LOCALAPPDATA%\ctrsrv" >nul 2>&1
cd /d "%LOCALAPPDATA%\ctrsrv"

echo [ctrsrv] PYTHONPATH=%PYTHONPATH%
echo [ctrsrv] cwd=%CD%
python -m ctrsrv serve --host 127.0.0.1 --port 8080
set "RC=%ERRORLEVEL%"
echo [ctrsrv] exit code %RC%
endlocal & exit /b %RC%
