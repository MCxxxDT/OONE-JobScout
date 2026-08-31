@echo off
rem Launch Chrome with remote debugging (CDP) on port 9335
rem NOTE: D: root is NOT writable on this machine - profile MUST live under C:\Users
rem Profile is an isolated dir: keeps login state, does NOT touch your daily browser
rem Port history: 9222 deprecated (Chrome 136 default-profile port trap), 9333 taken by another crawler project -> 9335 since 2026-08-31
set CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe
set PROFILE=C:\Users\LENOVO\chrome-cdp-profile

rem 2026-08-31: --remote-allow-origins=* 让 CDP 客户端（含不 suppress_origin 的第三方工具，
rem 如 eatmoreduck scraper）能连上 Chrome 136+（默认拒绝带 Origin 的调试 WS）
start "" "%CHROME%" --remote-debugging-port=9335 --user-data-dir=%PROFILE% --no-first-run --no-default-browser-check --disable-background-networking --remote-allow-origins=*

echo.
echo Verify: open http://127.0.0.1:9335/json/version in any browser tab.
echo Then scan the BOSS login QR code ONCE inside this window.
echo Keep this Chrome window open while running any script.
pause
