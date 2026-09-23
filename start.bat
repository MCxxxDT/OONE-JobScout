@echo off
setlocal enabledelayedexpansion
title BOSS直聘 智能接管平台 (OONE-JobScout)
chcp 65001 >nul 2>&1

cd /d "%~dp0"

echo ======================================================================
echo   BOSS直聘 智能接管平台 (OONE-JobScout) · 开箱即用一键启动器
echo ======================================================================
echo.

set "PY_BIN="

:: 1. 优先使用已存在的虚拟环境
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_BIN=%~dp0.venv\Scripts\python.exe"
    echo [环境] 检测到项目专属虚拟环境: .venv
)

:: 2. 若虚拟环境不存在，自动探测 Python / uv 并静默搭建
if not defined PY_BIN (
    echo [初始化] 正在初始化项目运行环境...
    
    :: 探测 uv (极速安装器)
    where uv >nul 2>&1
    if !errorlevel! equ 0 (
        echo [初始化] 使用 uv 创建虚拟环境与安装依赖...
        uv venv .venv
        if exist "%~dp0.venv\Scripts\python.exe" (
            set "PY_BIN=%~dp0.venv\Scripts\python.exe"
            uv pip install --python "!PY_BIN!" -r requirements.txt
        )
    )

    :: 探测 python
    if not defined PY_BIN (
        where python >nul 2>&1
        if !errorlevel! equ 0 (
            echo [初始化] 使用系统 Python 创建虚拟环境与安装依赖...
            python -m venv .venv
            if exist "%~dp0.venv\Scripts\python.exe" (
                set "PY_BIN=%~dp0.venv\Scripts\python.exe"
                "!PY_BIN!" -m pip install -r requirements.txt
            )
        )
    )

    :: 探测 py -3
    if not defined PY_BIN (
        where py >nul 2>&1
        if !errorlevel! equ 0 (
            echo [初始化] 使用 py -3 创建虚拟环境与安装依赖...
            py -3 -m venv .venv
            if exist "%~dp0.venv\Scripts\python.exe" (
                set "PY_BIN=%~dp0.venv\Scripts\python.exe"
                "!PY_BIN!" -m pip install -r requirements.txt
            )
        )
    )
)

:: 3. 若环境仍未就绪，友好指引并退出
if not defined PY_BIN (
    echo.
    echo ======================================================================
    echo [错误] 未在当前系统中检测到 Python 3.10+ 或 uv 运行环境！
    echo ======================================================================
    echo 请任选一种方式安装 Python 环境（开箱即用只需安装一次）：
    echo   1. 推荐极速安装 uv:
    echo      在 PowerShell 中运行: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo   2. 或直接下载安装 Python 官方安装包:
    echo      https://www.python.org/downloads/
    echo      （注意：安装时务必勾选 "Add python.exe to PATH"）
    echo ======================================================================
    echo.
    pause
    exit /b 1
)

:: 4. 自动确保本地私有配置文件存在
if not exist "%~dp0config.local.json" (
    if exist "%~dp0config.local.example.json" (
        copy "%~dp0config.local.example.json" "%~dp0config.local.json" >nul
        echo [配置] 已基于安全模板自动初始化 config.local.json
    )
)

:: 5. 自动检测并拉起独立调试 Chrome (端口 9335)
echo [浏览器] 正在自检与准备专属调试浏览器 (端口 9335)...
"!PY_BIN!" -c "from boss_apply import qr_login; ok = qr_login.ensure_chrome_running(); print('[浏览器] ' + ('调试专用 Chrome 已就绪并在 9335 端口监听' if ok else '未能自动拉起 Chrome，请确认是否已安装 Chrome/Edge 浏览器'))"

:: 6. 在默认浏览器中弹出 Web 审批工作台
echo [服务] 正在唤醒现代 Web 审批工作台...
echo [访问] http://127.0.0.1:8788/?token=boss-apply
echo.

start "" "http://127.0.0.1:8788/?token=boss-apply"

:: 7. 启动 Web 控制台主服务
"!PY_BIN!" -m boss_apply.web_server

if %errorlevel% neq 0 (
    echo.
    echo [提示] Web 服务已停止运行。
    pause
)
