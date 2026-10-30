@echo off
setlocal
set "PYTHONPATH=%~dp0tools\session-reader;%~dp0plugins\codex-task-recorder\lib"
set "PYTHONIOENCODING=utf-8"
python -m csr %*
exit /b %ERRORLEVEL%
