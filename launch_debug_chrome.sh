#!/bin/bash
# ==============================================================================
# boss-apply · macOS / Linux 专用调试 Chrome 启动脚本
# 端口：9335 | 用户配置目录：~/chrome-cdp-profile
# ==============================================================================

PORT=9335
PROFILE="$HOME/chrome-cdp-profile"

# 1. 探测 Chrome / Chromium 系列浏览器可执行文件路径
CHROME_BIN=""
BROWSER_NAME=""

# 优先级：Google Chrome -> Microsoft Edge -> Brave -> Chromium -> Arc
CANDIDATES=(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome:Google Chrome"
    "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome:Google Chrome"
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge:Microsoft Edge"
    "$HOME/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge:Microsoft Edge"
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser:Brave Browser"
    "$HOME/Applications/Brave Browser.app/Contents/MacOS/Brave Browser:Brave Browser"
    "/Applications/Chromium.app/Contents/MacOS/Chromium:Chromium"
    "/Applications/Arc.app/Contents/MacOS/Arc:Arc Browser"
)

for item in "${CANDIDATES[@]}"; do
    path="${item%%:*}"
    name="${item##*:}"
    if [ -f "$path" ]; then
        CHROME_BIN="$path"
        BROWSER_NAME="$name"
        break
    fi
done

# Linux 环境变量或命令行别名兜底
if [ -z "$CHROME_BIN" ]; then
    if command -v google-chrome &> /dev/null; then
        CHROME_BIN="google-chrome"
        BROWSER_NAME="Google Chrome"
    elif command -v microsoft-edge &> /dev/null; then
        CHROME_BIN="microsoft-edge"
        BROWSER_NAME="Microsoft Edge"
    elif command -v brave-browser &> /dev/null; then
        CHROME_BIN="brave-browser"
        BROWSER_NAME="Brave Browser"
    elif command -v chromium-browser &> /dev/null; then
        CHROME_BIN="chromium-browser"
        BROWSER_NAME="Chromium"
    elif command -v chromium &> /dev/null; then
        CHROME_BIN="chromium"
        BROWSER_NAME="Chromium"
    fi
fi

if [ -z "$CHROME_BIN" ]; then
    echo "================================================================="
    echo "⚠️  未在当前 Mac 上检测到已安装的 Chrome 或 Chromium 系列浏览器！"
    echo "================================================================="
    echo "自动化投递需要借助 Chromium 内核浏览器的远程调试接口（CDP）。"
    echo "推荐以下任意一种快速安装方式："
    echo ""
    echo "  👉 方式 A（终端 1 行命令安装，推荐）："
    echo "     brew install --cask google-chrome"
    echo "     （或者安装 Edge：brew install --cask microsoft-edge）"
    echo ""
    echo "  👉 方式 B（官网直接下载安装包，国内免翻）："
    echo "     Chrome 官网下载: https://www.google.cn/chrome/"
    echo "     Edge 官网下载:   https://www.microsoft.com/edge"
    echo "================================================================="
    exit 1
fi

mkdir -p "$PROFILE"

echo "================================================================="
echo "  🚀 正在启动 BOSS直聘 专用调试 ${BROWSER_NAME:-Chrome} (端口: $PORT)..."
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

echo "✅ ${BROWSER_NAME:-Chrome} 已在后台启动！请在打开的浏览器中扫码登录 BOSS直聘。"
