"""diag v5：①裸CDP页内fetch joblist.json 验证API数据质量 ②摸详情页结构。
仅2次导航+1次fetch，只读。
"""
import json
import time
from urllib.parse import quote
from urllib.request import urlopen

import websocket

CDP_HTTP = "http://127.0.0.1:9335"

ver = json.loads(urlopen(CDP_HTTP + "/json/version", timeout=5).read().decode())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)

_mid = [0]


def send(method, params=None, sid=None):
    _mid[0] += 1
    msg = {"id": _mid[0], "method": method, "params": params or {}}
    if sid:
        msg["sessionId"] = sid
    ws.send(json.dumps(msg))
    deadline = time.time() + 30
    while time.time() < deadline:
        data = json.loads(ws.recv())
        if data.get("id") == _mid[0]:
            if "error" in data:
                raise RuntimeError(json.dumps(data["error"], ensure_ascii=False)[:200])
            return data.get("result", {})
    raise TimeoutError(method)


t = send("Target.createTarget", {"url": "about:blank"})["targetId"]
sid = send("Target.attachToTarget", {"targetId": t, "flatten": True})["sessionId"]


def cmd(method, params=None):
    return send(method, params, sid=sid)


def ev(js):
    r = cmd("Runtime.evaluate", {"expression": js, "returnByValue": True})
    res = r.get("result", {})
    if res.get("subtype") == "error" or "exceptionDetails" in r:
        return None
    return res.get("value")


def state():
    v = ev("""
(() => JSON.stringify({href: location.href.slice(0,100), blank: location.href==='about:blank',
  bodyLen: document.body ? document.body.innerText.length : -1}))()
""")
    try:
        return json.loads(v)
    except Exception:
        return None


print("=== 1) 打开搜索页（作为fetch宿主+cookie保障） ===")
cmd("Page.navigate", {"url": "https://www.zhipin.com/web/geek/job?query=%s&city=101210100" % quote("AI产品经理")})
time.sleep(6)
print("state:", json.dumps(state(), ensure_ascii=False))

print("=== 2) 页内 fetch joblist.json ===")
FETCH_JS = """
async () => {
  try {
    const u = '/wapi/zpgeek/search/joblist.json?scene=1&query=' + encodeURIComponent('AI产品经理')
            + '&city=101210100&page=1&pageSize=15&securityId=&pos=';
    const r = await fetch(u, {credentials: 'include', headers: {'accept': 'application/json'}});
    const j = await r.json();
    const L = (j.zpData && j.zpData.jobList) || [];
    const one = L[0] ? Object.keys(L[0]) : [];
    return JSON.stringify({status: r.status, count: L.length, keys: one.slice(0, 40),
      first: L[0] ? {jobName: L[0].jobName, salaryDesc: L[0].salaryDesc, brandName: L[0].brandName,
                     area: L[0].areaDistrict || L[0].cityDistrict, active: L[0].activeTimeDesc || L[0].bossOnlineTimeDesc || '',
                     jobLabels: L[0].jobLabels, skills: L[0].skills, encryptId: L[0].encryptJobId,
                     brandId: L[0].brandId} : null});
  } catch (e) { return JSON.stringify({err: String(e).slice(0, 200)}); }
}
"""
v = ev(FETCH_JS)
print((v or "(null)")[:1200])

print("=== 3) 摸详情页结构 ===")
d = json.loads(v) if v and v.startswith("{") else {}
eid = (d.get("first") or {}).get("encryptId") or "2d5eb476c30eb6640nF52d-8GVVQ"
cmd("Page.navigate", {"url": "https://www.zhipin.com/job_detail/%s.html" % eid})
time.sleep(6)
st = state()
print("state:", json.dumps(st, ensure_ascii=False))
if st and not st.get("blank") and st.get("bodyLen", 0) > 100:
    DJ = r"""
(() => {
  const body = document.body.innerText;
  const cands = ['.job-sec-text', '.job-detail-section', '.detail-content', '.job-detail',
                 '[class*=job-sec]', '[class*=detail-content]', '.job-banner', '.detail-figure'];
  const hits = {};
  for (const s of cands) { const el = document.querySelector(s); if (el) hits[s] = el.innerText.trim().length; }
  const am = body.match(/(刚刚活跃|今日活跃|\d+日内活跃|本周活跃|本月活跃|月内活跃|在线)/);
  const btn = document.querySelector('.btn-startchat, .btn-chat, [ka*=chat], .detail-op a');
  return JSON.stringify({hits: hits, active: am ? am[1] : null,
    chatBtn: btn ? {cls: btn.className.slice(0, 60), text: btn.innerText.trim().slice(0, 20)} : null,
    bodyHead: body.slice(0, 400).replace(/\n/g, '|')});
})()
"""
    v2 = ev(DJ)
    print((v2 or "(null)")[:1500])

send("Target.closeTarget", {"targetId": t})
ws.close()
