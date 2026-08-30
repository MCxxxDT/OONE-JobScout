"""E2 实验：从存活标签页用 fetch 调 BOSS 搜索 API（同源、带Cookie、不导航）。
只读：一次 API GET。验证 joblist.json 是否可用。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import browser, config as cfgmod

cfg = cfgmod.load()
b = browser.connect(cfg)
ctx = b.contexts[0]

# 找任意 zhipin.com 源的标签页做 fetch 宿主（DOM空壳也行，只要 origin 对）
host = None
for p in ctx.pages:
    if "zhipin.com" in (p.url or ""):
        host = p
        break
if host is None:
    host = ctx.new_page()
    host.goto("https://www.zhipin.com/", timeout=30000)
    host.wait_for_timeout(3000)

print("宿主页URL:", (host.url or "")[:90])

JS = """
async () => {
  const q = 'AI产品经理', city = '101210100';
  const url = '/wapi/zpgeek/search/joblist.json?scene=1&query=' + encodeURIComponent(q)
            + '&city=' + city + '&page=1&pageSize=15&securityId=&pos=' ;
  try {
    const resp = await fetch(url, {credentials: 'include', headers: {'accept': 'application/json, text/plain, */*'}});
    const txt = await resp.text();
    return {status: resp.status, head: txt.slice(0, 900)};
  } catch (e) {
    return {status: 'FETCH_ERR', head: String(e).slice(0, 300)};
  }
}
"""
r = host.evaluate(JS)
print("HTTP状态:", r["status"])
print("响应开头:")
try:
    body = json.loads(r["head"] + ("}" * 0)) if False else r["head"]
    j = json.loads(r["head"]) if r["head"].strip().endswith("}") and r["status"] == 200 else None
except Exception:
    j = None
print(r["head"][:900])
