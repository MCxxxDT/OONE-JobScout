"""BOSS直聘智能接管平台 · Web 工作台标准模块入口 (F06 修复)

支持通过标准命令行启动：
    python -m boss_apply.web_server
或作为 ASGI 规范应用导入：
    from boss_apply.web_server import app
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.approval_web import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
