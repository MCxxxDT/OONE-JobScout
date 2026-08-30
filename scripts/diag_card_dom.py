"""diag v4：裸CDP摸清新版搜索卡片DOM结构。
输出：.job-name 的祖先链 class、第一张卡片 outerHTML、列表容器信息。
"""
import json
import time
from urllib.parse import quote
from urllib.request import urlopen

import websocket

CDP_HTTP = "http://127.0.0.1:9333"

ver = json.loads(urlopen(CDP_HTTP + "/json/version", timeout=5).read().decode())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=25, suppress_origin=True)

_mid = [0]


def send(method, params=None, sid=None):
    _mid[0] += 1
    msg = {"id": _mid[0], "method": method, "params": params or {}}
    if sid:
        msg["sessionId"] = sid
    ws.send(json.dumps(msg))
    deadline = time.time() + 25
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


cmd("Page.navigate", {"url": "https://www.zhipin.com/web/geek/job?query=%s&city=101210100" % quote("AI产品经理")})
time.sleep(6)

JS = r"""
(() => {
  const names = document.querySelectorAll('.job-name');
  if (!names.length) return JSON.stringify({err: 'no .job-name'});
  const card = names[0].closest('li') || names[0].closest('div[class*=card]') || names[0].parentElement.parentElement;
  const ul = names[0].closest('ul');
  const chain = [];
  let n = names[0];
  for (let i = 0; i < 6 && n; i++) { chain.push(n.tagName + '.' + (n.className || '')); n = n.parentElement; }
  return JSON.stringify({
    nameCount: names.length,
    ancestorChain: chain,
    ulClass: ul ? ul.className : null,
    liClass: card ? card.className : null,
    liTag: card ? card.tagName : null,
    liCount: ul ? ul.children.length : 0,
    firstCardHTML: card ? card.outerHTML.slice(0, 2200) : null
  });
})()
"""
r = cmd("Runtime.evaluate", {"expression": JS, "returnByValue": True})
d = json.loads(r.get("result", {}).get("value", "{}"))
print("名字数:", d.get("nameCount"))
print("祖先链:", " -> ".join(d.get("ancestorChain", [])))
print("UL class:", d.get("ulClass"))
print("卡片标签/class:", d.get("liTag"), "|", d.get("liClass"))
print("UL下子节点数:", d.get("liCount"))
print("---- 第一张卡片 outerHTML ----")
print(d.get("firstCardHTML", "(none)")[:2200])

try:
    send("Target.closeTarget", {"targetId": t})
except Exception:
    pass
ws.close()
