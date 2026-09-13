@echo off
setlocal enabledelayedexpansion

set "PORT=9335"
set "PROFILE=%USERPROFILE%\chrome-cdp-profile"

:: 1. 探测 Chrome / Edge / Brave 可执行文件
set "BROWSER_BIN="
set "BROWSER_NAME="

if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "BROWSER_BIN=C:\Program Files\Google\Chrome\Application\chrome.exe"
    set "BROWSER_NAME=Google Chrome"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "BROWSER_BIN=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    set "BROWSER_NAME=Google Chrome"
) else if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    set "BROWSER_BIN=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
    set "BROWSER_NAME=Google Chrome"
) else if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    set "BROWSER_BIN=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    set "BROWSER_NAME=Microsoft Edge"
) else if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    set "BROWSER_BIN=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    set "BROWSER_NAME=Microsoft Edge"
) else if exist "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe" (
    set "BROWSER_BIN=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"
    set "BROWSER_NAME=Microsoft Edge"
) else if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
    set "BROWSER_BIN=C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    set "BROWSER_NAME=Brave Browser"
)

if "%BROWSER_BIN%"=="" (
    echo =================================================================
    echo [错误] 未在当前 Windows 设备上找到 Chrome、Edge 或 Brave 浏览器！
    echo =================================================================
    echo 自动化需要借助 Chromium 内核浏览器的远程调试协议 (CDP)。
    echo 推荐安装：
    echo   - Google Chrome: https://www.google.cn/chrome/
    echo   - Microsoft Edge: https://www.microsoft.com/edge
    echo =================================================================
    pause
    exit /b 1
)

if not exist "%PROFILE%" mkdir "%PROFILE%"

echo =================================================================
echo   正在启动 BOSS直聘 专用调试 !BROWSER_NAME! (端口: %PORT%)...
echo   配置文件目录: %PROFILE%
echo =================================================================

start "" "%BROWSER_BIN%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --disable-background-networking --remote-allow-origins=* --start-maximized "https://www.zhipin.com/web/geek/chat"

echo.
echo [就绪] 浏览器已在独立窗口启动。
echo 请在弹出的浏览器窗口中扫码登录 BOSS直聘（登录态会自动保存在本地 Profile）。
echo 运行自动化脚本或后台守护进程时，请保持此浏览器窗口处于打开状态。
echo 验证调试接口是否连通: 打开浏览器访问 http://127.0.0.1:%PORT%/json/version
echo.
pause
