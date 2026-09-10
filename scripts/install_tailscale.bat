@echo off
chcp 65001 >nul
title Tailscale D盘一键安装器

echo ========================================================
echo           Tailscale D 盘安装部署向导
echo ========================================================
echo.

set "MSI_PATH=D:\LENOVO\Tailscale\tailscale-setup-amd64.msi"
if not exist "%MSI_PATH%" (
    echo [ERROR] 未找到安装包：%MSI_PATH%
    pause
    exit /b 1
)

:: 优先检测 D:\Tailscale（如果用户已在根目录手动创建）
if exist "D:\Tailscale" (
    set "TARGET_DIR=D:\Tailscale"
) else (
    set "TARGET_DIR=D:\LENOVO\Tailscale"
)

echo [1] 安装源文件: %MSI_PATH%
echo [2] 目标安装目录: %TARGET_DIR%
echo.
echo 即将唤起 Windows 管理员提权安装向导...
echo 请在屏幕弹出的【用户账户控制 (UAC)】窗口中点击【是】以允许安装网络适配器驱动。
echo.

powershell -NoProfile -Command "Start-Process msiexec.exe -ArgumentList '/i \"\"%MSI_PATH%\"\" INSTALLDIR=\"\"%TARGET_DIR%\"\"' -Verb RunAs"

echo.
echo [*] 已唤起安装向导！
echo [*] 安装完成后，Tailscale 将常驻在 Windows 右下角任务栏。
echo ========================================================
pause
