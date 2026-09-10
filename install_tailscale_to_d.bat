@echo off
chcp 65001 >nul
cd /d "%~dp0"
call scripts\install_tailscale.bat
