#!/usr/bin/env bash
# ======================================================================
#   BOSS直聘 智能接管平台 (OONE-JobScout) · macOS / Linux 一键启动器
# ======================================================================

set -e
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "======================================================================"
echo "  BOSS直聘 智能接管平台 (OONE-JobScout) · 开箱即用一键启动器"
echo "======================================================================"
echo ""

PY_BIN=""

# 1. 优先使用已存在的虚拟环境
if [ -f "$ROOT_DIR/.venv/bin/python" ]; then
    PY_BIN="$ROOT_DIR/.venv/bin/python"
    echo "[环境] 检测到项目专属虚拟环境: .venv"
fi

# 2. 若虚拟环境不存在，自动探测 Python / uv 并搭建
if [ -z "$PY_BIN" ]; then
    echo "[初始化] 正在初始化项目运行环境..."
    if command -v uv >/dev/null 2>&1; then
        echo "[初始化] 使用 uv 创建虚拟环境与安装依赖..."
        uv venv .venv
        PY_BIN="$ROOT_DIR/.venv/bin/python"
        uv pip install --python "$PY_BIN" -r requirements.txt
    elif command -v python3 >/dev/null 2>&1; then
        echo "[初始化] 使用系统 python3 创建虚拟环境与安装依赖..."
        python3 -m venv .venv
        PY_BIN="$ROOT_DIR/.venv/bin/python"
        "$PY_BIN" -m pip install -r requirements.txt
    fi
fi

# 3. 若环境仍未就绪，友好指引并退出
if [ -z "$PY_BIN" ] || [ ! -f "$PY_BIN" ]; then
    echo "======================================================================"
    echo "[错误] 未在当前系统中检测到 Python 3.10+ 或 uv 运行环境！"
    echo "请先安装 Python: brew install python 或 curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "======================================================================"
    exit 1
fi

# 4. 自动确保本地私有配置文件存在
if [ ! -f "$ROOT_DIR/config.local.json" ] && [ -f "$ROOT_DIR/config.local.example.json" ]; then
    cp "$ROOT_DIR/config.local.example.json" "$ROOT_DIR/config.local.json"
    echo "[配置] 已基于安全模板自动初始化 config.local.json"
fi

# 5. 自动检测并拉起独立调试 Chrome (端口 9335)
echo "[浏览器] 正在自检与准备专属调试浏览器 (端口 9335)..."
"$PY_BIN" -c "from boss_apply import qr_login; ok = qr_login.ensure_chrome_running(); print('[浏览器] ' + ('调试专用 Chrome 已就绪并在 9335 端口监听' if ok else '未能自动拉起 Chrome，请确认是否已安装 Chrome/Edge/Arc 浏览器'))" || true

# 6. 在默认浏览器中弹出 Web 审批工作台
WEB_URL="http://127.0.0.1:8788/?token=boss-apply"
echo "[服务] 正在唤醒现代 Web 审批工作台..."
echo "[访问] $WEB_URL"
echo ""

if command -v open >/dev/null 2>&1; then
    open "$WEB_URL" || true
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$WEB_URL" || true
fi

# 7. 启动 Web 控制台主服务
exec "$PY_BIN" -m boss_apply.web_server
