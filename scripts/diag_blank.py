"""空白页跳转定时实验（只读+导航计时）：
1) 对照组 example.com 2) zhipin 首页 3) 记录每次 frame 导航事件的时间线
目的：定位 about:blank 跳变的精确时刻与触发方。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import browser, config as cfgmod

cfg = cfgmod.load()
b = browser.connect(cfg)
ctx = b.contexts[0]
page = ctx.new_page()

events = []
page.on("framenavigated", lambda f: events.append((time.time(), (f.url or "")[:90])))
errors = []
page.on("pageerror", lambda e: errors.append((time.time(), str(e)[:120])))


def poll(label, seconds):
    t0 = time.time()
    last = ""
    while time.time() - t0 < seconds:
        try:
            u = page.url or ""
        except Exception as e:
            u = "ERR:" + str(e)[:60]
        if u != last:
            print("  [%s %5.1fs] %s" % (label, time.time() - t0, u[:100]), flush=True)
            last = u
        time.sleep(0.7)


print("=== 1) 对照组 example.com ===", flush=True)
try:
    page.goto("https://example.com", timeout=20000)
    poll("CTRL", 8)
except Exception as e:
    print("  goto失败:", str(e)[:120])

print("=== 2) zhipin 首页 ===", flush=True)
try:
    page.goto("https://www.zhipin.com/", timeout=30000)
    try:
        print("  加载后标题:", page.title()[:50], flush=True)
    except Exception:
        pass
    poll("HOME", 30)
except Exception as e:
    print("  goto失败:", str(e)[:120])

print("=== 3) 导航事件时间线 ===", flush=True)
base = events[0][0] if events else time.time()
for t, u in events:
    print("  +%5.1fs  %s" % (t - base, u), flush=True)

if errors:
    print("=== 页面JS错误 ===", flush=True)
    for t, e in errors[-8:]:
        print("  +%5.1fs  %s" % (t - base, e), flush=True)

# 清理自己开的标签页（避免留垃圾tab）
try:
    page.close()
except Exception:
    pass
