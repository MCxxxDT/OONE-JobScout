@echo off
set "TARGET=D:\Tailscale"
set "MSI="

if exist "%~dp0..\tailscale-setup-amd64.msi" set "MSI=%~dp0..\tailscale-setup-amd64.msi"
if not defined MSI if exist "%USERPROFILE%\Downloads\tailscale-setup-amd64.msi" set "MSI=%USERPROFILE%\Downloads\tailscale-setup-amd64.msi"
if not defined MSI if exist "D:\Tailscale\tailscale-setup-amd64.msi" set "MSI=D:\Tailscale\tailscale-setup-amd64.msi"

if not defined MSI (
    echo ====================================================================
    echo [提示] 未找到 Tailscale 安装包 tailscale-setup-amd64.msi！
    echo 请前往官方下载安装包并放至项目目录或 Downloads 目录：
    echo https://tailscale.com/download/windows
    echo ====================================================================
    pause
    exit /b 1
)

echo 正在启动 Tailscale 安装向导（目标目录: %TARGET%）...
start msiexec.exe /i "%MSI%" INSTALLDIR="%TARGET%"
