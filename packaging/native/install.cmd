@echo off
rem Register the local marketplace and install the plugin. ASCII only: a .cmd is read in the OEM code page.
rem __VERSION__ is filled in by scripts\package_native.py.
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "CODEX="
for /f "delims=" %%D in ('dir /b /ad /o:d "%LOCALAPPDATA%\OpenAI\Codex\bin" 2^>nul') do if exist "%LOCALAPPDATA%\OpenAI\Codex\bin\%%D\codex.exe" set "CODEX=%LOCALAPPDATA%\OpenAI\Codex\bin\%%D\codex.exe"
if not defined CODEX (
  echo [FAIL] codex.exe not found under %LOCALAPPDATA%\OpenAI\Codex\bin - install and start the Codex App once.
  pause
  exit /b 1
)
rem A previous copy of this package may be registered from another folder; drop it so the path is this one.
"%CODEX%" plugin marketplace remove native-assist >nul 2>&1
echo [1/2] marketplace add "%ROOT%"
"%CODEX%" plugin marketplace add "%ROOT%"
if errorlevel 1 (
  echo [FAIL] marketplace add failed - see the codex message above.
  pause
  exit /b 1
)
echo [2/2] plugin add codex-native-assist@native-assist
"%CODEX%" plugin add codex-native-assist@native-assist
if errorlevel 1 (
  echo [FAIL] plugin add failed - see the codex message above.
  pause
  exit /b 1
)
echo.
echo [ OK ] installed (__VERSION__ - CDK recharge).
echo        1. Quit Codex completely, including the tray icon, then start it again.
echo        2. Recharge: click the CDK entry in the Codex sidebar. The model has no tools for it.
echo        Keep this folder where it is - uninstall.cmd reads it from here.
echo.
pause
