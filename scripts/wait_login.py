"""等待用户在调试Chrome内扫码登录（完全被动版——绝不触碰页面）。

用法：python scripts/wait_login.py [最长等待秒数，默认600]
原则：只读检测（枚举标签页+查DOM），不 goto、不 reload、不点击、不导航。
      v1 的教训：检测不到就强制 goto 首页，会打断扫码后的鉴权跳转链，
      表现为"一扫码就闪退"。本版本从物理上杜绝该问题。
退出码：0=登录成功，1=超时未登录，2=浏览器连接断开。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import browser, config as cfgmod

deadline_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 600

cfg = cfgmod.load()

print("被动值守中：每4秒只读检测一次登录态，绝不触碰页面（最长 %d 秒）..." % deadline_seconds, flush=True)
print("提示：二维码约5分钟过期，过期就点二维码图片刷新；如遇滑块验证请手动完成。", flush=True)

deadline = time.time() + deadline_seconds
ok = False
conn_fail = 0

while time.time() < deadline:
    time.sleep(4)
    # 1) 确认浏览器还活着（连续失败则报告退出）
    try:
        b = browser.connect(cfg)
        conn_fail = 0
    except Exception:
        conn_fail += 1
        if conn_fail >= 5:
            print("浏览器连接断开（连续 %d 次）：请确认调试Chrome窗口未被关闭，重跑本脚本。" % conn_fail)
            sys.exit(2)
        continue

    # 2) 只读枚举所有标签页，检查任意 zhipin 页是否出现登录态
    try:
        for ctx in b.contexts:
            for p in ctx.pages:
                if "zhipin.com" not in (p.url or ""):
                    continue
                try:
                    if browser.is_logged_in(p):
                        ok = True
                        break
                except Exception:
                    continue
            if ok:
                break
        if ok:
            break
    except Exception:
        # playwright 内部偶发 frame-detach 监听器噪声，非致命，继续轮询
        continue

if ok:
    # 成功后也只做只读收尾：截图当前 zhipin 页
    try:
        for ctx in b.contexts:
            for p in ctx.pages:
                if "zhipin.com" in (p.url or ""):
                    try:
                        p.screenshot(path=cfgmod.state_path("t1_screenshot.png"))
                        break
                    except Exception:
                        pass
    except Exception:
        pass
    print("登录状态: 已登录 ✓（被动检测，全程未触碰页面）")
    print("截图已更新: state/t1_screenshot.png")
    print("等待登录: 成功")
    sys.exit(0)

print("等待登录: 超时未登录。检查点：①扫码的是【调试Chrome窗口】（独立档案）②二维码是否过期（点击刷新）③手机上是否点了确认登录。")
sys.exit(1)
