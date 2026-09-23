@echo off
setlocal
chcp 65001 >nul 2>&1

set "PY_BIN="
if exist "%~dp0.venv\Scripts\python.exe" set "PY_BIN=%~dp0.venv\Scripts\python.exe"
if not defined PY_BIN where python >nul 2>&1 && set "PY_BIN=python"

if not defined PY_BIN (
    echo [错误] 未检测到 Python 环境！
    exit /b 1
)

"%PY_BIN%" "%~dp0scripts\worktree.py" %*
