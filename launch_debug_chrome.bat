@echo off
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE=C:\Users\LENOVO\chrome-cdp-profile"

start "" "%CHROME%" --remote-debugging-port=9335 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --disable-background-networking --remote-allow-origins=* --start-maximized "https://www.zhipin.com/web/geek/chat"

echo.
echo Verify: open http://127.0.0.1:9335/json/version in any browser tab.
echo Keep this Chrome window open while running any script.
pause
