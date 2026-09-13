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

2026-08-31 升级（采纳 eatmoreduck/boss-zhipin-scraper #53 教训）：
- 列表主路径改为【被动捕获】：Network.enable 旁听页面自身发出的
  /wapi/zpgeek/search/joblist.json 响应（零注入请求）。程序注入的同步 XHR
  与页面自身请求特征不同，会被 BOSS 风控识别为异常环境（code 37）。
- 风控判定升级：code∈{31,37} 或 message 命中关键字（环境存在异常/访问频繁/
  操作太频繁/安全校验/滑块/验证）→ RESTRICTED → raise RiskControl（护栏熔断）。
- DOM+注入 fetch 保留为彻底兜底；捕获失败自动降级，行为不回退。
"""
import base64
import json
import re
import time
from urllib.parse import quote
from urllib.request import urlopen

import websocket

from .browser import RiskControl

BASE = "https://www.zhipin.com"
LIST_URL = BASE + "/web/geek/jobs?query={q}&city={c}&page={p}"

API_JOB_LIST_PATH = "/wapi/zpgeek/search/joblist.json"

# 风控码（会随平台策略变化，码表追不上时按 message 关键字兜底）
RESTRICTED_CODES = {31, 37}
RESTRICTED_KEYWORDS = ("环境存在异常", "访问频繁", "操作太频繁", "安全校验", "滑块", "验证")

ACTIVE_RE = re.compile(r"(刚刚活跃|今日活跃|\d+日内活跃|本周活跃|本月活跃|月内活跃|在线)")

STATE_JS = """
(() => JSON.stringify({
  href: location.href,
  blank: location.href === 'about:blank',
  bodyLen: document.body ? document.body.innerText.length : -1,
  cards: document.querySelectorAll('li.job-card-box, li:has(.job-name)').length,
  captcha: !!(document.querySelector('#nc_1_wrapper') || document.querySelector('.nc-container') || document.querySelector("iframe[src*='captcha']")),
  security: /security-check|web\\/common\\/security|passport\\/zp\\/verify/.test(location.href)
}))()
"""

LOGIN_JS = """
(() => JSON.stringify({
  url: location.href,
  avatar: !!(document.querySelector('.nav-figure') || document.querySelector('.header-nav-figure')),
  loginBtn: !!document.querySelector('.header-login-btn') || (document.body ? /登录\\/注册/.test(document.body.innerText.slice(0, 3000)) : false)
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

# 会话消息历史提取（CDP evaluate 注入）：
# 精确识别 CSS class 区分身份，过滤系统提示节点与杂音，返回结构化序列
CHAT_HISTORY_JS = """
(() => {
  const list = document.querySelector('.chat-message .im-list');
  if (!list) return JSON.stringify({r: 'no_list'});
  const TIME_RE = /^(?:\\d{2}-\\d{2} \\d{1,2}:\\d{2}|\\d{4}-\\d{2}-\\d{2}[ T]\\d{1,2}:\\d{2}.*|昨天.*|\\d{1,2}:\\d{2}|\\d{1,2}月\\d{1,2}日.*)$/;
  const DROP = new Set(['已读', '未读', '拒绝', '同意', '收下', '送达']);
  const SYS_PAT = /^(?:您已|您已经|已撤回|打招呼成功|对方已同意|对方请求|双方已交换|请求交换|您的附件简历)/;
  const msgs = [];
  for (const it of list.querySelectorAll('.message-item')) {
    const cls = String(it.className || '');
    const isSysNode = cls.includes('item-system') || cls.includes('chat-sysmsg') || cls.includes('sys-notice') || cls.includes('message-system') || cls.includes('chat-notice');
    let role = null;
    if (cls.includes('item-myself') || cls.includes('chat-item--right') || cls.includes('item-self')) {
      role = 'me';
    } else if (cls.includes('item-friend') || cls.includes('chat-item--left') || cls.includes('item-other')) {
      role = 'hr';
    } else if (isSysNode) {
      role = 'system';
    }
    if (!role) continue;
    const raw = (it.innerText || '').replace(/\\n/g, '|');
    const parts = raw.split('|').map(s => s.trim())
      .filter(s => s && !TIME_RE.test(s) && !DROP.has(s));
    const text = parts.join(' ').slice(0, 1000);
    if (!text) continue;
    if (isSysNode || SYS_PAT.test(text)) {
      role = 'system';
    }
    msgs.push({role: role, text: text});
  }
  return JSON.stringify({messages: msgs, count: msgs.length, source: 'dom'});
})()
"""


def clean_conversation_history(history):
    """过滤掉系统消息与系统提示，严格只返回求职者(me)与招聘方(hr)的真实消息序列：
    [{"role": "me" | "hr", "text": "..."}]"""
    if not history or not isinstance(history, list):
        return []
    sys_re = re.compile(r"^(?:您已|您已经|已撤回|打招呼成功|对方已同意|对方请求|请求交换|双方已交换|您的附件简历)")
    res = []
    for m in history:
        if not isinstance(m, dict):
            continue
        r = m.get("role")
        t = (m.get("text") or "").strip()
        if r in ("me", "hr") and t and not sys_re.search(t):
            res.append({"role": r, "text": t})
    return res



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


def classify_joblist_response(data):
    """判定 joblist 响应：ok / empty / restricted / unauthenticated（采纳 eatmoreduck 风控词表）。"""
    code = None
    if isinstance(data, dict):
        try:
            code = int(data.get("code")) if data.get("code") is not None else None
        except (TypeError, ValueError):
            code = None
        message = str(data.get("message") or data.get("msg") or "")
        if isinstance(code, int) and code in RESTRICTED_CODES:
            return "restricted", "code=%r %s" % (code, message)
        if code != 0:
            hit = next((k for k in RESTRICTED_KEYWORDS if k in message), None)
            if hit:
                return "restricted", "code=%r msg含%r" % (code, hit)
            return "response_error", "code=%r %s" % (code, message)
        zp = data.get("zpData")
        if not isinstance(zp, dict):
            return "response_error", "缺少 zpData"
        lst = zp.get("jobList")
        if not isinstance(lst, list):
            return "response_error", "缺少 jobList"
        if not lst:
            return "empty", "jobList 为空"
        if any((j.get("salaryDesc") or "").strip() for j in lst if isinstance(j, dict)):
            return "ok", ""
        return "unauthenticated", "无明文薪资（疑似未登录）"
    return "response_error", "响应非 JSON 对象"


def map_api_jobs(data, keyword=""):
    """把 joblist.json 原始条目映射为与 DOM 卡片一致的 job 字段（薪资直接取明文 salaryDesc）。"""
    if not isinstance(data, dict):
        return []
    lst = (data.get("zpData") or {}).get("jobList") or []
    out = []
    for o in lst:
        if not isinstance(o, dict):
            continue
        eid = str(o.get("encryptJobId") or "")
        if not eid:
            continue
        loc = "·".join(x for x in (o.get("cityName"), o.get("areaDistrict"), o.get("businessDistrict")) if x)
        tags = ",".join(x for x in (o.get("jobExperience"), o.get("jobDegree")) if x)
        # bossOnline 仅列表级字段：在线=0，缺失=未知(-1)放行，详情页会补验
        active = 0 if o.get("bossOnline") else -1
        out.append({
            "title": o.get("jobName") or "",
            "href": "/job_detail/%s.html" % eid,
            "salary": o.get("salaryDesc") or "",
            "company": o.get("brandName") or "",
            "area": loc,
            "tags": tags,
            "raw": "",
            "boss_active": active,
            "keyword": keyword,
        })
    return out


class RawCDP:
    """浏览器级裸CDP会话 + 一个复用标签页。"""

    def __init__(self, cdp_http="http://127.0.0.1:9335"):
        ver = json.loads(urlopen(cdp_http + "/json/version", timeout=5).read().decode())
        self.ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self._mid = 0
        self.tab_id = None
        self.sid = None
        self.events = []  # CDP 事件缓冲（被动捕获用；_send 等待响应期间到达的事件入此）
        self._passive_disabled = False  # 自适应金丝雀：本环境若取不到旁听响应体则本会话禁用被动捕获

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
            # 非本次响应的事件消息：入缓冲供被动捕获，不丢弃
            if "method" in data:
                self.events.append(data)
        raise TimeoutError(method)

    def drain_events(self, duration):
        """在 duration 秒内持续接收并缓冲 CDP 事件（不发送任何命令）。
        用于等待页面自身发出的请求完成（Network 域事件）。"""
        deadline = time.time() + duration
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return
                self.ws.settimeout(min(0.6, remaining))
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    break
                try:
                    r = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if "method" in r:
                    self.events.append(r)
        finally:
            self.ws.settimeout(30)

    def open_tab(self, url="about:blank", background=True):
        params = {"url": url}
        if background:
            params["background"] = True
        self.tab_id = self._send("Target.createTarget", params)["targetId"]
        self.sid = self._send("Target.attachToTarget", {"targetId": self.tab_id, "flatten": True})["sessionId"]
        if not background:
            try:
                self.activate_tab()
                self.restore_window()
            except Exception:
                pass
        return self.tab_id

    def activate_tab(self):
        """将当前标签页激活置顶到浏览器前台。"""
        if self.tab_id:
            try:
                return self._send("Target.activateTarget", {"targetId": self.tab_id})
            except Exception:
                pass
        return None

    def restore_window(self):
        """尝试通过 CDP 将当前浏览器窗口恢复为正常可视形态，避免最小化或被遮挡。"""
        try:
            target_id = self.tab_id
            if not target_id:
                targets = self._send("Target.getTargets").get("targetInfos", [])
                target_id = targets[0]["targetId"] if targets else None
            if target_id:
                win = self._send("Browser.getWindowForTarget", {"targetId": target_id})
                win_id = win.get("windowId")
                if win_id:
                    self._send("Browser.setWindowBounds", {"windowId": win_id, "bounds": {"windowState": "normal"}})
                    return True
        except Exception:
            pass
        return False

    def minimize_window(self):
        """尝试通过 CDP 将当前浏览器窗口最小化，避免抢占焦点或弹窗打扰用户。"""
        try:
            target_id = self.tab_id
            if not target_id:
                targets = self._send("Target.getTargets").get("targetInfos", [])
                target_id = targets[0]["targetId"] if targets else None
            if target_id:
                win = self._send("Browser.getWindowForTarget", {"targetId": target_id})
                win_id = win.get("windowId")
                if win_id:
                    self._send("Browser.setWindowBounds", {"windowId": win_id, "bounds": {"windowState": "minimized"}})
                    return True
        except Exception:
            pass
        return False

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

    # ---- 被动捕获（Network 域旁听，零注入请求）----
    def enable_network(self):
        return self._send("Network.enable", sid=self.sid)

    def disable_network(self):
        try:
            self._send("Network.disable", sid=self.sid)
        except Exception:
            pass

    def _completed_joblist_rids(self):
        """只统计本会话（sessionId 匹配）已完成的 joblist 请求，避免浏览器级
        WS 收到其他标签页的同路径请求导致 getResponseBody 报 No data found。"""
        req, fin = {}, set()
        for ev in self.events:
            if ev.get("sessionId") != self.sid:
                continue
            m = ev.get("method", "")
            p = ev.get("params", {})
            if m == "Network.requestWillBeSent":
                url = (p.get("request") or {}).get("url", "")
                if API_JOB_LIST_PATH in url:
                    req[p.get("requestId")] = True
            elif m == "Network.loadingFinished":
                fin.add(p.get("requestId"))
        return [rid for rid in req if rid in fin]

    def _get_response_body(self, request_id):
        try:
            r = self._send("Network.getResponseBody", {"requestId": request_id}, sid=self.sid)
        except Exception:
            return None
        res = r.get("result", {}) if isinstance(r, dict) else {}
        body = res.get("body", "")
        if res.get("base64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                return None
        return body

    def capture_joblist_response(self, timeout=25):
        """被动捕获下一次完成的 joblist 响应并解析 JSON；超时返回 None。
        调用前需 enable_network() 并已触发页面自身请求（如导航）。"""
        deadline = time.time() + timeout
        consumed = set()
        while time.time() < deadline:
            for rid in self._completed_joblist_rids():
                if rid in consumed:
                    continue
                consumed.add(rid)
                body = self._get_response_body(rid)
                if body:
                    try:
                        return json.loads(body)
                    except (json.JSONDecodeError, ValueError):
                        continue
            remain = deadline - time.time()
            if remain > 0:
                self.drain_events(min(0.6, remain))
        return None

    def _try_passive_search(self, url):
        """Network 旁听版搜索：返回 joblist dict / None（None=走 DOM 兜底）。
        restricted（code 31/37 或 message 命中风控关键字）→ raise RiskControl（护栏熔断）。
        自适应金丝雀：Chrome 151 实测旁听响应体常被修剪（getResponseBody 返回空串），
        故每会话仅首次尝试（timeout 8s）；取不到体即置 _passive_disabled，后续页零开销走 DOM。"""
        if self._passive_disabled:
            return None
        self.events = []
        try:
            self.enable_network()
            # 焦点仿真：后台标签页在页面看来保持「可见且有焦点」，否则 BOSS 会
            # 延迟甚至不发列表 API 请求（采纳 eatmoreduck create_page_session 做法）
            self._focus_emulation(True)
            self.nav(url)
            data = self.capture_joblist_response(timeout=8)
        finally:
            self._focus_emulation(False)
            self.disable_network()
        if data is None:
            self._passive_disabled = True  # 金丝雀判定：本环境取不到旁听体，本会话降级
            return None
        verdict, info = classify_joblist_response(data)
        if verdict == "restricted":
            raise RiskControl("boss restricted: %s" % info)
        if verdict in ("ok", "empty"):
            return data
        # 捕获到但判定非可用（如 unauthenticated/response_error）→ 走 DOM 兜底重验
        return None

    def _focus_emulation(self, enabled):
        try:
            self._send("Emulation.setFocusEmulationEnabled", {"enabled": enabled}, sid=self.sid)
            if enabled:
                self._send("Page.addScriptToEvaluateOnNewDocument", {
                    "source": "Object.defineProperty(document,'hidden',{get:()=>false});"
                              "Object.defineProperty(document,'visibilityState',{get:()=>'visible'});"
                              "Object.defineProperty(document,'webkitHidden',{get:()=>false});"
                              "Object.defineProperty(document,'webkitVisibilityState',{get:()=>'visible'});",
                }, sid=self.sid)
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
    let u = '/wapi/zpgeek/search/joblist.json?scene=1&query=%(q)s&city=%(c)s&page=%(p)d&pageSize=30&securityId=&pos=';
    const exp = %(exp)s;
    if (exp) u += '&experience=' + encodeURIComponent(exp);
    const r = await fetch(u, {credentials: 'include', headers: {'accept': 'application/json'}});
    const j = await r.json();
    const L = (j.zpData && j.zpData.jobList) || [];
    return JSON.stringify(L.map(o => ({eid: o.encryptJobId || '', salary: o.salaryDesc || '', company: o.brandName || ''})));
  } catch (e) { return '[]'; }
})()
"""

    def search_jobs(self, keyword, city_code, page_no=1, experience=None):
        url = LIST_URL.format(q=quote(keyword), c=city_code, p=page_no)
        if experience:
            url += "&experience=%s" % quote(str(experience))
        # 主路径：被动捕获页面自身的 joblist 响应（零注入请求，采纳 eatmoreduck #53 教训）
        try:
            data = self._try_passive_search(url)
            if data is not None:
                jobs = map_api_jobs(data, keyword)
                if jobs:
                    return jobs
        except RiskControl:
            raise
        except Exception:
            pass  # 捕获异常 → 走 DOM 兜底，不降级能力
        # 兜底：DOM 卡片解析 + 注入 fetch 回填薪资（保持历史行为）
        for attempt in (1, 2):
            self.nav(url)
            st = self.wait_ready(want_cards=True, timeout_s=15)
            if st:
                break
            if attempt == 1:
                time.sleep(2)  # 质询重试
            else:
                final_st = self.state()
                if final_st and not final_st.get("captcha") and not final_st.get("security") and not final_st.get("blank") and final_st.get("bodyLen", 0) > 200:
                    return []
                raise RiskControl("search page not ready: %s" % url[:90])
        v = self.eval(CARD_JS)
        try:
            cards = json.loads(v) if v else []
        except Exception:
            cards = []
        # 新版卡片DOM不带薪资数字（异步/特殊渲染）→ 用前端同款API按encryptJobId回填
        api_js = self.API_SALARY_JS % {
            "q": quote(keyword), "c": city_code, "p": page_no,
            "exp": json.dumps(str(experience)) if experience else "null"
        }
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

    def fetch_campus_recommendations(self):
        """抓取 /school/ 校园招聘专区的瞰荐名企与 2027 届在招项目。"""
        url = BASE + "/school/?ka=tab_school_recruit_click"
        self.nav(url)
        time.sleep(2)
        js = """
        (() => {
            const items = [];
            document.querySelectorAll('[class*="recommend"] [class*="item"], [class*="kanjian"] [class*="item"], [class*="company-card"]').forEach(el => {
                const title = el.querySelector('[class*="title"], h3, h4, .name')?.innerText?.trim() || '';
                const desc = el.querySelector('[class*="desc"], [class*="count"], p')?.innerText?.trim() || '';
                const campus_link = el.querySelector('a[href*="experience=102"]')?.href || '';
                const intern_link = el.querySelector('a[href*="experience=108"]')?.href || '';
                const general_link = el.querySelector('a')?.href || '';
                if (title) {
                    items.push({
                        company: title,
                        desc: desc,
                        campus_url: campus_link,
                        intern_url: intern_link,
                        url: campus_link || intern_link || general_link
                    });
                }
            });
            return JSON.stringify(items);
        })()
        """
        try:
            res = self.eval(js)
            return json.loads(res) if res else []
        except Exception:
            return []

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
                d = {}
                for _ in range(8):
                    v = self.eval(DETAIL_JS)
                    try:
                        d = json.loads(v) if v else {}
                    except Exception:
                        d = {}
                    if d.get("text") and len(d["text"]) > 100:
                        break
                    time.sleep(0.5)
                return d.get("text", ""), active_days(d.get("active", ""))
            if attempt == 1:
                time.sleep(2)
            else:
                return "", -1  # 详情失败不熔断：降级为仅列表信息打分
        return "", -1
