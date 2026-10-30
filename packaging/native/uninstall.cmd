@echo off
rem Stop the background recorder and its autostart entry first, then remove the plugin.
setlocal
set "CODEX="
for /f "delims=" %%D in ('dir /b /ad /o:d "%LOCALAPPDATA%\OpenAI\Codex\bin" 2^>nul') do if exist "%LOCALAPPDATA%\OpenAI\Codex\bin\%%D\codex.exe" set "CODEX=%LOCALAPPDATA%\OpenAI\Codex\bin\%%D\codex.exe"
set "EXE="
for /f "delims=" %%H in ('dir /b /ad "%USERPROFILE%\.codex\plugins\cache\native-assist\codex-native-assist" 2^>nul') do if exist "%USERPROFILE%\.codex\plugins\cache\native-assist\codex-native-assist\%%H\bin\assist.exe" set "EXE=%USERPROFILE%\.codex\plugins\cache\native-assist\codex-native-assist\%%H\bin\assist.exe"
if not defined EXE for %%P in ("%LOCALAPPDATA%\CodexNativeAssist\bin\assist-*.exe") do set "EXE=%%~fP"
if defined EXE (
  echo [1/3] stopping the background recorder
  "%EXE%" daemon-stop
) else (
  echo [1/3] no assist.exe found; removing the autostart entry only
  schtasks /delete /f /tn "CodexNativeAssist\Collector" >nul 2>&1
  reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v CodexNativeAssist /f >nul 2>&1
)
if not defined CODEX (
  echo [FAIL] codex.exe not found - remove the plugin from the Codex App UI instead.
  pause
  exit /b 1
)
echo [2/3] plugin remove codex-native-assist@native-assist
"%CODEX%" plugin remove codex-native-assist@native-assist
echo [3/3] marketplace remove native-assist
"%CODEX%" plugin marketplace remove native-assist
echo.
echo [ OK ] uninstalled; the recorder was stopped and its autostart entry removed.
echo        Recorded data stays in %USERPROFILE%\.codex\plugins\data\codex-native-assist-native-assist
echo        unless you delete that folder.
echo.
pause
