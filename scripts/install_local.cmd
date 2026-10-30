@echo off
rem Double-clickable launcher for install_local.py. Works from any directory: the script path is
rem resolved from this file's own location, not from the current directory.
rem Keep this file ASCII-only: cmd.exe reads batch files in the OEM code page.
setlocal
rem The script prints UTF-8; a cp936 console would show mojibake. Scoped to this window only.
chcp 65001 >nul 2>nul
set "HERE=%~dp0"
set "SCRIPT=%HERE%install_local.py"
if not exist "%SCRIPT%" (
  echo Script not found: %SCRIPT%
  pause
  exit /b 1
)
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo Python 3 not found on PATH. Install it, or run install_local.py with any python 3.10+.
  pause
  exit /b 1
)
%PY% "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Finished with code %RC%.
pause
exit /b %RC%
