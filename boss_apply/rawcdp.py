"""裸CDP层（反爬升级核心，2026-08-30实测定型）。

根因：playwright connect_over_cdp 会对每个页面 enable Runtime 等CDP域，
BOSS 安全JS检测到该特征后清空页面DOM（URL保留或跳about:blank），
表现为"搜索页加载完即被清空/闪退"。

对策：本模块只用"一次性CDP命令"（Target/Page/Runtime 的裸调用），
绝不 enable 任何事件域、绝不让 playwright 碰抓取页面 → 实测页面正常渲染。

设计：
- 一个 RawCDP 会话 = 一条浏览器级 WebSocket + 一个复用的标签页
- search_jobs / fetch_detail 均为导航+轮询+一次性 evaluate 提取
- 内置空页检测（被清空=质询未过）：自动重试一次，再失败抛 RiskControl（护栏熔断）
"""
import json
import re
import time
from urllib.parse import quote
from urllib.request import urlopen

import websocket

from .browser import RiskControl

BASE = "https://www.zhipin.com"
LIST_URL = BASE + "/web/geek/job?query={q}&city={c}&page={p}"

ACTIVE_RE = re.compile(r"(刚刚活跃|今日活跃|\d+日内活跃|本周活跃|本月活跃|月内活跃|在线)")

STATE_JS = """
(() => JSON.stringify({
  href: location.href,
  blank: location.href === 'about:blank',
  bodyLen: document.body ? document.body.innerText.length : -1,
  cards: document.querySelectorAll('li.job-card-box, li:has(.job-name)').length,
  captcha: !!(document.querySelector('#nc_1_wrapper') || document.querySelector('.nc-container') || document.querySelector("iframe[src*='captcha']")),
  security: /security-check|web\\/common\\/security/.test(location.href)
}))()
"""

CARD_JS = """
(() => {
  const out = [];
  let nodes = document.querySelectorAll('li.job-card-box');
  if (!nodes.length) nodes = document.querySelectorAll('li:has(a.job-name)');
  nodes.forEach(n => {
    const t = (s) => { const e = n.querySelector(s); return e ? e.innerText.trim() : ''; };
    const a = n.querySelector("a[href*='/job_detail/']");
    if (!a) return;
    out.push({
      title: t('.job-name'),
      href: a.getAttribute('href') || '',
      salary: t('.job-salary') || t('.salary'),
      company: t('.boss-name') || t('.company-name'),
      area: t('.company-location') || t('.job-area'),
      tags: Array.from(n.querySelectorAll('.tag-list li')).map(e => e.innerText.trim()).join(','),
      raw: n.innerText.replace(/\\n/g, '|').slice(0, 300)
    });
  });
  return JSON.stringify(out);
})()
"""

DETAIL_JS = """
(() => {
  const body = document.body ? document.body.innerText : '';
  const cands = ['.job-detail', '.job-sec-text', '.detail-content', '[class*=detail-content]', '[class*=job-sec]'];
  let text = '';
  for (const s of cands) {
    const el = document.querySelector(s);
    if (el && el.innerText.trim().length > 80) { text = el.innerText.trim(); break; }
  }
  if (!text) text = body.slice(0, 3000);
  const am = body.match(/(刚刚活跃|今日活跃|\\d+日内活跃|本周活跃|本月活跃|月内活跃|在线)/);
  return JSON.stringify({text: text.slice(0, 4000), active: am ? am[1] : ''});
})()
"""


def active_days(text):
    """与 scraper.boss_active_days 相同语义；未匹配返回 -1（未知=放行）。"""
    m = ACTIVE_RE.search(text or "")
    if not m:
        return -1
    s = m.group(1)
    if "刚刚" in s or "在线" in s or "今日" in s:
        return 0
    d = re.search(r"(\d+)", s)
    if d:
        return int(d.group(1))
    if "本周" in s:
        return 7
    if "本月" in s or "月内" in s:
        return 30
    return -1


class RawCDP:
    """浏览器级裸CDP会话 + 一个复用标签页。"""

    def __init__(self, cdp_http="http://127.0.0.1:9333"):
        ver = json.loads(urlopen(cdp_http + "/json/version", timeout=5).read().decode())
        self.ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self._mid = 0
        self.tab_id = None
        self.sid = None

    # ---- 低层 ----
    def _send(self, method, params=None, sid=None):
        self._mid += 1
        msg = {"id": self._mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))
        deadline = time.time() + 30
        while time.time() < deadline:
            data = json.loads(self.ws.recv())
            if data.get("id") == self._mid:
                if "error" in data:
                    raise RuntimeError("%s: %s" % (method, json.dumps(data["error"], ensure_ascii=False)[:200]))
                return data.get("result", {})
        raise TimeoutError(method)

    def open_tab(self, url="about:blank"):
        self.tab_id = self._send("Target.createTarget", {"url": url})["targetId"]
        self.sid = self._send("Target.attachToTarget", {"targetId": self.tab_id, "flatten": True})["sessionId"]
        return self.tab_id

    def nav(self, url):
        return self._send("Page.navigate", {"url": url}, sid=self.sid)

    def eval(self, js):
        """一次性 Runtime.evaluate（不enable域）。awaitPromise 支持页内 async fetch。"""
        r = self._send("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, sid=self.sid)
        res = r.get("result", {})
        if res.get("subtype") == "error" or "exceptionDetails" in r:
            return None
        return res.get("value")

    def close_tab(self):
        if self.tab_id:
            try:
                self._send("Target.closeTarget", {"targetId": self.tab_id})
            except Exception:
                pass
            self.tab_id = None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    # ---- 高层状态 ----
    def state(self):
        v = self.eval(STATE_JS)
        try:
            return json.loads(v) if v else None
        except Exception:
            return None

    def wait_ready(self, want_cards=True, timeout_s=18, poll=1.0):
        """轮询直到就绪；只有真跳 about:blank / 验证码 / 安全页才算质询失败。
        注意：加载初期 bodyLen 小是正常现象，不能当作被清空（曾误判致详情全空）。"""
        deadline = time.time() + timeout_s
        retried = False
        while time.time() < deadline:
            st = self.state()
            if st:
                if st.get("captcha") or st.get("security"):
                    raise RiskControl("captcha/security on %s" % st.get("href", "")[:80])
                if st.get("blank"):
                    if not retried:
                        retried = True
                        return None  # 交由调用方重新导航（质询重试）
                    raise RiskControl("page wiped twice (challenge failed): %s" % st.get("href", "")[:80])
                if want_cards and st.get("cards", 0) > 0:
                    return st
                if not want_cards and st.get("bodyLen", 0) > 200:
                    return st
            time.sleep(poll)
        return None

    # ---- 业务 ----
    API_SALARY_JS = """
(async () => {
  try {
    const u = '/wapi/zpgeek/search/joblist.json?scene=1&query=%(q)s&city=%(c)s&page=%(p)d&pageSize=30&securityId=&pos=';
    const r = await fetch(u, {credentials: 'include', headers: {'accept': 'application/json'}});
    const j = await r.json();
    const L = (j.zpData && j.zpData.jobList) || [];
    return JSON.stringify(L.map(o => ({eid: o.encryptJobId || '', salary: o.salaryDesc || '', company: o.brandName || ''})));
  } catch (e) { return '[]'; }
})()
"""

    def search_jobs(self, keyword, city_code, page_no=1):
        url = LIST_URL.format(q=quote(keyword), c=city_code, p=page_no)
        for attempt in (1, 2):
            self.nav(url)
            st = self.wait_ready(want_cards=True, timeout_s=15)
            if st:
                break
            if attempt == 1:
                time.sleep(2)  # 质询重试
            else:
                raise RiskControl("search page not ready: %s" % url[:90])
        v = self.eval(CARD_JS)
        try:
            cards = json.loads(v) if v else []
        except Exception:
            cards = []
        # 新版卡片DOM不带薪资数字（异步/特殊渲染）→ 用前端同款API按encryptJobId回填
        api_js = self.API_SALARY_JS % {"q": quote(keyword), "c": city_code, "p": page_no}
        try:
            api = {a.get("eid"): a for a in json.loads(self.eval(api_js) or "[]") if a.get("eid")}
        except Exception:
            api = {}
        eid_re = re.compile(r"/job_detail/([^/]+?)\.html")
        for j in cards:
            m = eid_re.search(j.get("href") or "")
            a = api.get(m.group(1)) if m else None
            if a:
                if a.get("salary"):
                    j["salary"] = a["salary"]
                if not j.get("company") and a.get("company"):
                    j["company"] = a["company"]
        for j in cards:
            j["boss_active"] = active_days(j.get("raw", ""))
            j["keyword"] = keyword
        return cards

    def fetch_detail(self, job):
        """返回 (detail_text, active_days_int)。导航到详情页提取JD与活跃度。"""
        href = job.get("href") or ""
        if not href:
            return "", -1
        url = href if href.startswith("http") else BASE + href
        for attempt in (1, 2):
            self.nav(url)
            st = self.wait_ready(want_cards=False, timeout_s=12)
            if st:
                v = self.eval(DETAIL_JS)
                try:
                    d = json.loads(v) if v else {}
                except Exception:
                    d = {}
                return d.get("text", ""), active_days(d.get("active", ""))
            if attempt == 1:
                time.sleep(2)
            else:
                return "", -1  # 详情失败不熔断：降级为仅列表信息打分
        return "", -1
