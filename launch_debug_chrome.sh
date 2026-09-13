#!/bin/bash
# ==============================================================================
# boss-apply · macOS / Linux 专用调试 Chrome 启动脚本
# 端口：9335 | 用户配置目录：~/chrome-cdp-profile
# ==============================================================================

PORT=9335
PROFILE="$HOME/chrome-cdp-profile"

# 1. 探测 Chrome 可执行文件路径
CHROME_BIN=""
if [ -f "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
elif [ -f "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    CHROME_BIN="$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
elif command -v google-chrome &> /dev/null; then
    CHROME_BIN="google-chrome"
elif command -v chromium-browser &> /dev/null; then
    CHROME_BIN="chromium-browser"
elif command -v chromium &> /dev/null; then
    CHROME_BIN="chromium"
fi

if [ -z "$CHROME_BIN" ]; then
    echo "❌ 错误：未找到 Google Chrome 浏览器！"
    echo "   请确保已安装 Google Chrome 并放置在 /Applications 目录下。"
    exit 1
fi

mkdir -p "$PROFILE"

echo "================================================================="
echo "  🚀 正在启动 BOSS直聘 专用调试 Chrome (端口: $PORT)..."
echo "  📁 配置文件存储路径: $PROFILE"
echo "================================================================="

"$CHROME_BIN" \
    --remote-debugging-port=$PORT \
    --user-data-dir="$PROFILE" \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking \
    --remote-allow-origins="*" \
    "https://www.zhipin.com/web/geek/chat" &

echo "✅ Chrome 已在后台启动！请在打开的浏览器中扫码登录 BOSS直聘。"
