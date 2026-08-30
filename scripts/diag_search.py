"""搜索页只读诊断：页面真实状态 + 候选选择器命中统计。
用法：python scripts/diag_search.py [关键词] [城市码]
绝不点击，只 goto + 读 DOM。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import quote

from boss_apply import browser, config as cfgmod

kw = sys.argv[1] if len(sys.argv) > 1 else "AI产品经理"
city_code = sys.argv[2] if len(sys.argv) > 2 else "101210100"

cfg = cfgmod.load()
page = browser.get_zhipin_page(cfg)

url = "https://www.zhipin.com/web/geek/job?query=%s&city=%s" % (quote(kw), city_code)
page.goto(url, timeout=30000)
page.wait_for_timeout(8000)  # 给足渲染时间

print("=" * 60)
print("最终URL:", page.url)
print("页面标题:", page.title())
try:
    cookies = page.context.cookies("https://www.zhipin.com")
    names = [c["name"] for c in cookies]
    print("cookies:", ",".join(sorted(names)))
    print("__zp_stoken__ 存在:", "__zp_stoken__" in names)
    print("wt2 存在:", "wt2" in names)
except Exception as e:
    print("cookie读取失败:", e)

print("-" * 60)
print("候选选择器命中数：")
candidates = [
    ".job-card-wrapper", ".job-card-left", ".job-card-box",
    "ul.job-list-box li", ".job-list-box", "li.job-card-wrapper",
    ".job-card-body", ".job-card-footer", ".job-card-right",
    ".job-name", ".salary", ".company-name", ".job-area",
    ".job-list-page", ".search-job-result", ".job-card-wrapper a",
    ".rec-job-card", ".job-card-wrap", "[ka='search_list_jid']",
]
for sel in candidates:
    try:
        n = page.locator(sel).count()
    except Exception as e:
        n = "ERR:%s" % str(e)[:40]
    print("  %-28s %s" % (sel, n))

print("-" * 60)
try:
    body = page.inner_text("body") or ""
    print("正文前600字：")
    print(body[:600].replace("\n", " ⏎ "))
except Exception as e:
    print("正文读取失败:", e)

shot = cfgmod.state_path("diag_search.png")
page.screenshot(path=shot, full_page=False)
print("-" * 60)
print("截图:", shot)
