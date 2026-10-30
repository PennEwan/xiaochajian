@echo off
rem Build plugins\codex-native-assist\bin\assist.exe with the C# compiler that ships with .NET Framework 4.x.
rem No SDK, no NuGet: users only need Windows (10/11 include .NET Framework 4.8).
setlocal
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
set FW=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319
set OUT=%~dp0..\..\plugins\codex-native-assist\bin\assist.exe
"%CSC%" /nologo /target:winexe /platform:anycpu /optimize+ /utf8output /codepage:65001 ^
  /out:"%OUT%" /win32manifest:"%~dp0assist.manifest" ^
  /r:System.dll /r:System.Core.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll ^
  /r:System.Security.dll /r:"%FW%\System.Web.Extensions.dll" ^
  "%~dp0src\*.cs"
exit /b %ERRORLEVEL%
