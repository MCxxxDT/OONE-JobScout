"""E1 决定性实验：裸 CDP over WebSocket（不发 Runtime.enable/Page.enable）。
若裸 CDP 开的页面能存活并渲染卡片 → playwright 会话特征即触发点。
"""
import json
import sys
import time
from urllib.parse import quote
from urllib.request import urlopen

import websocket

CDP_HTTP = "http://127.0.0.1:9335"

ver = json.loads(urlopen(CDP_HTTP + "/json/version", timeout=5).read().decode())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
print("已连上浏览器级 WebSocket")

_mid = [0]


def send(method, params=None, sid=None):
    _mid[0] += 1
    msg = {"id": _mid[0], "method": method, "params": params or {}}
    if sid:
        msg["sessionId"] = sid
    ws.send(json.dumps(msg))
    deadline = time.time() + 20
    while time.time() < deadline:
        data = json.loads(ws.recv())
        if data.get("id") == _mid[0]:
            if "error" in data:
                raise RuntimeError(json.dumps(data["error"], ensure_ascii=False)[:200])
            return data.get("result", {})
        # 其余是事件，跳过（我们不订阅任何域）
    raise TimeoutError(method)


t = send("Target.createTarget", {"url": "about:blank"})["targetId"]
print("新标签页:", t)
sid = send("Target.attachToTarget", {"targetId": t, "flatten": True})["sessionId"]
print("已attach（未enable任何域）")


def cmd(method, params=None):
    return send(method, params, sid=sid)


SEARCH = "https://www.zhipin.com/web/geek/job?query=%s&city=101210100" % quote("AI产品经理")

cmd("Page.navigate", {"url": SEARCH})
print("已导航:", SEARCH)

PROBE_JS = """
(() => {
  const sels = ['.job-card-wrapper', '.job-card-left', 'ul.job-list-box li', '.job-list-box', '.job-name', 'li[kb]'];
  const counts = {};
  for (const s of sels) counts[s] = document.querySelectorAll(s).length;
  return JSON.stringify({
    href: location.href.slice(0, 110),
    title: document.title.slice(0, 40),
    bodyLen: document.body ? document.body.innerText.length : -1,
    counts: counts
  });
})()
"""

for i in range(6):
    time.sleep(3)
    try:
        r = cmd("Runtime.evaluate", {"expression": PROBE_JS, "returnByValue": True})
        val = r.get("result", {}).get("value", "")
        print("[t+%ds]" % ((i + 1) * 3), val[:400])
        d = json.loads(val) if val else {}
        if d.get("bodyLen", 0) > 500 and any(v and isinstance(v, int) and v > 0 for v in d.get("counts", {}).values()):
            print("\n*** 页面存活且有内容 —— 裸CDP可行! ***")
            break
    except Exception as e:
        print("[t+%ds] evaluate失败: %s" % ((i + 1) * 3, str(e)[:150]))

# 顺带抓一页卡片数据证明端到端可行
EXTRACT_JS = """
(() => {
  const nodes = document.querySelectorAll('.job-card-wrapper, .job-card-left, ul.job-list-box li');
  const out = [];
  nodes.forEach(n => {
    const t = (s) => { const e = n.querySelector(s); return e ? e.innerText.trim() : ''; };
    const a = n.querySelector("a[href*='/job_detail/']");
    out.push({title: t('.job-name') || t('.job-title'), salary: t('.salary'), company: t('.company-name') || t('.company-info'), area: t('.job-area'), href: a ? a.getAttribute('href') : '', raw: n.innerText.replace(/\\n/g, '|').slice(0, 260)});
  });
  return JSON.stringify(out.slice(0, 15));
})()
"""
try:
    r = cmd("Runtime.evaluate", {"expression": EXTRACT_JS, "returnByValue": True})
    cards = json.loads(r.get("result", {}).get("value", "[]"))
    print("\n抓到卡片数:", len(cards))
    for c in cards[:6]:
        print("  -", c.get("title"), "|", c.get("salary"), "|", c.get("company"), "|", (c.get("href") or "")[:50])
except Exception as e:
    print("提取失败:", str(e)[:150])

try:
    send("Target.closeTarget", {"targetId": t})
except Exception:
    pass
ws.close()
