#!/usr/bin/env bash
# No environment creation or dependency installation during removal.
set -e
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
    PY_BIN="$(command -v python3)"
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    PY_BIN="$ROOT_DIR/.venv/bin/python"
else
    echo '找不到 Python 3.10+；请用已有系统 Python 运行 boss_apply/uninstall.py。' >&2
    exit 1
fi
cd "${TMPDIR:-/tmp}"
exec "$PY_BIN" -B "$ROOT_DIR/boss_apply/uninstall.py" --root "$ROOT_DIR" "$@"
