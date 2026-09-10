@echo off
chcp 65001 >nul
title BOSS-Apply Mobile Tunnel
cd /d "%~dp0"
python -u scripts\start_mobile_tunnel.py
if errorlevel 1 pause
