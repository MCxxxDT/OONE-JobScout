"""T1 连通性验收：确认调试Chrome里BOSS登录态与风控信号（裸CDP版）。

用法：venv/Scripts/python scripts/t1_check_login.py
前置：先运行 launch_debug_chrome.bat，并在该窗口内登录BOSS。

历史：2026-08-31 起改用裸CDP。原 playwright 版 connect_over_cdp+goto 在
已登录 profile 上会被BOSS安全JS清空页面（URL回about:blank/白屏），
误报"未登录"；裸CDP开新标签页实测存活（见 HANDOVER.md 第6节）。
退出码：0=已登录且无风控，1=未登录，2=风控信号，3=页面加载失败。
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, rawcdp

CHECK_JS = """
(() => JSON.stringify({
  url: location.href,
  avatar: !!(document.querySelector('.nav-figure') || document.querySelector('.header-nav-figure')),
  loginBtn: !!document.querySelector('.header-login-btn') || (document.body ? /登录\\/注册/.test(document.body.innerText.slice(0, 3000)) : false),
  captcha: !!(document.querySelector('#nc_1_wrapper') || document.querySelector('.nc-container') || document.querySelector("iframe[src*='captcha']")),
  security: /security-check|web\\/common\\/security/.test(location.href)
}))()
"""


def main():
    cfg = cfgmod.load()
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab("https://www.zhipin.com/")
        st = None
        for _ in range(15):
            time.sleep(1)
            st = sess.state()
            if st and not st.get("blank") and st.get("bodyLen", 0) > 100:
                break
        if not st or st.get("blank") or st.get("bodyLen", 0) <= 0:
            print("页面加载失败（空白页）:", json.dumps(st, ensure_ascii=False))
            return 3
        if st.get("captcha") or st.get("security"):
            print("风控信号: 验证码/安全页拦截 →", st.get("href", "")[:90])
            return 2

        info = json.loads(sess.eval(CHECK_JS) or "{}")
        print("URL:", info.get("url", ""))

        shot = sess._send("Page.captureScreenshot", {"format": "png"}, sid=sess.sid)
        shot_path = cfgmod.state_path("t1_screenshot.png")
        with open(shot_path, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print("截图:", shot_path)

        if info.get("captcha") or info.get("security"):
            print("风控信号: 验证码/安全页组件")
            return 2
        print("风控信号: 无")
        if info.get("avatar") and not info.get("loginBtn"):
            print("登录状态: 已登录 ✓")
            print()
            print("T1 通过。下一步可跑 t2_readonly_scan.py")
            return 0
        if info.get("loginBtn"):
            print("登录状态: 未登录 → 请在调试Chrome窗口内扫码登录，然后重跑本脚本")
            return 1
        print("登录状态: 无法判定（首页无头像也无登录按钮，请人工看截图）")
        return 3
    finally:
        sess.close_tab()
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
