"""diag v3：验证"质询-自愈"理论。
流程：新标签页 → 先加载首页等质询自愈 → 再进搜索页 → 主帧URL轮询 + 内容快照。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import quote

from boss_apply import browser, config as cfgmod

cfg = cfgmod.load()
b = browser.connect(cfg)
ctx = b.contexts[0]
page = ctx.new_page()


def main_url():
    try:
        return page.url or ""
    except Exception:
        return "ERR"


def watch(label, seconds):
    t0 = time.time()
    last = None
    while time.time() - t0 < seconds:
        u = main_url()
        if u != last:
            print("  [%s %5.1fs] %s" % (label, time.time() - t0, u[:110]), flush=True)
            last = u
        time.sleep(0.5)


def content_peek(label):
    try:
        c = page.content() or ""
        c = c.replace("\n", " ")
        print("  [%s] 内容(%d字): %s" % (label, len(c), c[:180]), flush=True)
    except Exception as e:
        print("  [%s] content失败: %s" % (label, str(e)[:80]), flush=True)


print("=== STEP1 暖页：首页（预期 质询→自愈） ===", flush=True)
page.goto("https://www.zhipin.com/", timeout=30000)
watch("WARM", 12)
content_peek("WARM尾部")

print("=== STEP2 进搜索页：AI产品经理 & 杭州 ===", flush=True)
page.goto("https://www.zhipin.com/web/geek/job?query=%s&city=101210100" % quote("AI产品经理"), timeout=30000)
watch("SRCH", 15)
content_peek("SRCH尾部")

if main_url() == "about:blank":
    print("=== STEP3 仍空白 → 重试一次 ===", flush=True)
    page.goto("https://www.zhipin.com/web/geek/job?query=%s&city=101210100" % quote("AI产品经理"), timeout=30000)
    watch("RETRY", 15)
    content_peek("RETRY尾部")

print("=== 结果 ===", flush=True)
for sel in [".job-card-wrapper", ".job-card-left", "ul.job-list-box li", ".job-list-box", ".job-name"]:
    try:
        print("  %-24s %s" % (sel, page.locator(sel).count()), flush=True)
    except Exception:
        print("  %-24s ERR" % sel, flush=True)
print("最终URL:", main_url())

shot = cfgmod.state_path("diag_v3.png")
page.screenshot(path=shot)
print("截图:", shot)
try:
    page.close()
except Exception:
    pass
