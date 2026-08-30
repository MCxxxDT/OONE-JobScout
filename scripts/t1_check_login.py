"""T1 连通性验收：调试Chrome里BOSS登录态与风控信号（裸CDP版）。

用法：venv/Scripts/python scripts/t1_check_login.py
前置：先运行 launch_debug_chrome.bat，并在该窗口内登录BOSS。
退出码：0=已登录且无风控，1=未登录，2=风控信号，3=页面加载失败。
历史：2026-08-31 起走 flows.login_state（裸CDP）——playwright 版在已登录
profile 上会被BOSS安全JS清空页面并误报"未登录"（见 HANDOVER.md 第6节）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, flows

r = flows.login_state(cfgmod.load())
print("URL:", r.get("url", ""))
print("截图:", r.get("shot") or "(无)")
if r.get("error"):
    print("页面加载失败:", r["error"])
    sys.exit(3)
if r.get("risk"):
    print("风控信号:", r["risk"])
    sys.exit(2)
print("风控信号: 无")
if r.get("logged_in"):
    print("登录状态: 已登录 ✓")
    print()
    print("T1 通过。下一步可跑 t2_readonly_scan.py")
    sys.exit(0)
print("登录状态: 未登录 → 请在调试Chrome窗口内扫码登录，然后重跑本脚本")
sys.exit(1)
