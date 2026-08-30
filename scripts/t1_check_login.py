"""T1 连通性验收：确认脚本接管的是你已登录的真实会话。

用法：venv/Scripts/python scripts/t1_check_login.py
前置：先运行 launch_debug_chrome.bat，并在该窗口内登录BOSS。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import browser, config as cfgmod

cfg = cfgmod.load()
page = browser.get_zhipin_page(cfg)
page.goto("https://www.zhipin.com/", timeout=30000)
page.wait_for_timeout(2500)

print("URL:", page.url)
logged = browser.is_logged_in(page)
print("登录状态:", "已登录 ✓" if logged else "未登录 → 请在调试Chrome窗口内扫码登录，然后重跑本脚本")

try:
    browser.check_risk(page)
    print("风控信号: 无")
except browser.RiskControl as e:
    print("风控信号:", e)

shot = cfgmod.state_path("t1_screenshot.png")
page.screenshot(path=shot)
print("截图:", shot)
print()
print("T1 通过标准：登录状态=已登录，且风控信号=无。通过后跑 t2_readonly_scan.py")
