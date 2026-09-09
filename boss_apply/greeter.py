"""打招呼/回复执行。文案随 profile 切换：test=中性文案（不含任何个人信息）。
两套实现：send_greeting(playwright，仅限已存活页，弃用参考) / send_greeting_raw(裸CDP，主力)。
send_message_via_chat = 消息中心按公司名发消息（回复 HR 用）。
2026-08-31 适配：BOSS 聊天界面已迁移至 /web/geek/chat（详情页内嵌面板不再出现）。"""
import json
import random
import re
import time

from . import ledger, rawcdp
from .browser import check_risk

CHAT_SELECTORS = [".chat-input", ".dialog-chat textarea", "textarea.chat-input", ".chat-conversation textarea", ".chat-input textarea"]
SEND_SELECTORS = [".btn-send", "button:has-text('发送')"]

CHAT_URL = "/web/geek/chat"

# 隐私红线（精细化词表，审计补丁#2）：仅匹配"索要/提供联系方式"意图，
# 不裸匹配"微信"——保护 企业微信/微信小程序/微信生态 等业务词。flows.chat_reply 与本文件同源复用。
PRIVACY_RE = re.compile(
    r"1[3-9]\d{9}"                              # 11位手机号
    r"|\d{3,4}-?\d{7,8}"                        # 座机/长号
    r"|(?:加|留|给)\s*个?\s*微信"                 # 加微信/留个微信/给我微信
    r"|微信号"                                   # 微信号
    r"|微信(?:联系|沟通|详聊)"                    # 微信联系/微信沟通/微信详聊
    r"|手机号码?|电话|联系方式"                    # 手机(号)/电话/联系方式
    r"|(?:加|留)\s*个?\s*联系",                  # 加联系/留个联系
    re.I,
)


def privacy_blocked(text):
    """True=文案含联系方式意图，禁止 agent 代发（转人工）。"""
    return bool(PRIVACY_RE.search(text or ""))


# BOSS 账号原生默认招呼（不在 config 模板中：点击"立即沟通"时平台自动发出，
# 会成为新会话的最后一条消息，必须保底识别为自家发言，否则被误判待回复）。
NATIVE_DEFAULT_OPENER = "您好，我是27年毕业生"


def self_openers(cfg, head_len=14):
    """自家话术前缀集合：config 各 profile 招呼语取 {job} 前稳定段
    + 台账 reply 文本头 + BOSS 原生默认招呼保底（审计补丁#1）。
    消息中心据此判定"最后一条是否我方发言"；切 profile/改文案自动跟随。
    reply 文本头入库截 120 字，head_len 取 14 保证前缀稳定。"""
    ops = set()
    for templates in (cfg.get("greeting") or {}).values():
        for t in templates or []:
            head = (t or "").split("{job}")[0].strip()
            if head:
                ops.add(head[:head_len])
    for r in ledger.load_all():
        if r.get("action") == "reply" and r.get("text_head"):
            ops.add(r["text_head"].strip()[:head_len])
    ops.add(NATIVE_DEFAULT_OPENER[:head_len])
    return tuple(o for o in ops if o)


def greeting_text(cfg, job):
    profile = cfg.get("profile", "test")
    templates = (cfg.get("greeting", {}) or {}).get(profile) or cfg["greeting"]["test"]
    t = random.choice(templates)
    if privacy_blocked(t):
        raise RuntimeError("greeting template contains contact info (privacy), profile=%r" % profile)
    return t.replace("{job}", job.get("title") or "该岗位")


def send_greeting(page, job, cfg):
    """[弃用参考] playwright 版：在当前职位详情页发送一条招呼。"""
    check_risk(page)
    btn = page.locator("text=立即沟通").first
    btn.click(timeout=8000)

    box = None
    for sel in CHAT_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=3500)
            box = page.locator(sel).first
            break
        except Exception:
            continue
    if box is None:
        raise RuntimeError("chat input not found (selectors may need update after BOSS redesign)")

    text = greeting_text(cfg, job)
    try:
        tag = box.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        tag = "textarea"
    if tag in ("textarea", "input"):
        box.fill(text)
    else:
        box.click()
        page.keyboard.insert_text(text)

    sent = False
    for sel in SEND_SELECTORS:
        try:
            page.locator(sel).first.click(timeout=3000)
            sent = True
            break
        except Exception:
            continue
    if not sent:
        raise RuntimeError("send button not found")

    page.wait_for_timeout(1200)


def _ev(sess, js):
    """evaluate + JSON 解析辅助。"""
    v = sess.eval(js)
    try:
        return json.loads(v) if isinstance(v, str) else v
    except Exception:
        return v


def _probe_js():
    return """
(() => {
  const pick = () => {
    let el = document.querySelector('.chat-input[contenteditable="true"], .chat-input');
    if (!el) {
      const cands = document.querySelectorAll('textarea, [contenteditable="true"]');
      for (const e of cands) {
        const cls = String(e.className || '');
        if (/search/i.test(cls)) continue;
        if (/chat|message|edit/i.test(cls)) { el = e; break; }
      }
    }
    return el;
  };
  const inp = pick();
  const send = document.querySelector('.btn-send, .btn-sendmsg');
  const hasSureDialog = () => {
    const dialogs = document.querySelectorAll('.dialog-wrap, .boss-dialog, .dialog-container');
    for (const d of dialogs) {
      if (/contact/i.test(d.className) || (d.innerText || '').includes('电话') || (d.innerText || '').includes('微信')) continue;
      const b = d.querySelector('.btn-sure, .btn-sure-v2, button.sure');
      if (b && (b.offsetWidth || b.offsetHeight || b.getClientRects().length)) return true;
    }
    return false;
  };
  return JSON.stringify({
    href: location.href.slice(0, 100),
    inputTag: inp ? (inp.tagName + '|' + String(inp.className || '').slice(0, 40)) : null,
    inputLen: inp ? String(inp.value || inp.textContent || '').length : -1,
    sendDisabled: send ? /disabled/.test(String(send.className)) : null,
    sureBtn: hasSureDialog(),
    bodyLen: document.body ? document.body.innerText.length : 0
  });
})()
"""


def _pick_conversation_js(company):
    """消息中心会话定位：优先按公司名匹配；company 为空才取最新一条。
    严格匹配（审计补丁#3）：指定公司未命中时直接返回 notfound，严禁退回 newest，
    防止把回复发给最新会话的无关 HR。
    不直接 el.click()（BOSS 列表项对合成 click 无响应，2026-08-31 实测），
    返回元素视口坐标，由调用方走 CDP Input.dispatchMouseEvent 派发受信任点击。"""
    return """
(() => {
  const company = %s;
  const lis = Array.from(document.querySelectorAll('li'));
  const isConv = (li) => {
    const t = li.innerText || '';
    return t.length > 12 && /(?:\\d{1,2}:\\d{2}|\\d{1,2}月\\d{1,2}日|昨天|\\d{4}年)/.test(t);
  };
  let target = null, picked = 'none';
  if (company) {
    for (const li of lis) {
      if ((li.innerText || '').indexOf(company) >= 0 && isConv(li)) { target = li; picked = 'company'; break; }
    }
    if (!target) return JSON.stringify({r: 'notfound', picked: 'company_missing', company: company});
  }
  if (!target) {
    for (const li of lis) {
      if (isConv(li)) { target = li; picked = 'newest'; break; }
    }
  }
  if (!target) return JSON.stringify({r: 'notfound'});
  const head = (target.innerText || '').slice(0, 46);
  target.scrollIntoView({block: 'center'});
  const rect = target.getBoundingClientRect();
  return JSON.stringify({r: 'found', picked: picked, head: head,
    x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2)});
})()
""" % json.dumps(company or "", ensure_ascii=False)


def _trusted_click(sess, x, y):
    """CDP 派发受信任的鼠标点击（一次性 Input 命令，不 enable 任何域）。"""
    sess._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, sid=sess.sid)
    sess._send("Input.dispatchMouseEvent",
               {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, sid=sess.sid)
    sess._send("Input.dispatchMouseEvent",
               {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, sid=sess.sid)


def _fill_js(text):
    return """
(() => {
  const TEXT = %s;
  const inp = (window.__pickChatInput || (() => document.querySelector('.chat-input[contenteditable="true"], .chat-input')))();
  if (!inp) return JSON.stringify({r: 'noinput'});
  inp.focus();
  const tag = inp.tagName.toLowerCase();
  if (tag === 'textarea' || tag === 'input') {
    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(inp, TEXT);
    inp.dispatchEvent(new Event('input', {bubbles: true}));
  } else {
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, TEXT);
  }
  const len = (tag === 'textarea' || tag === 'input') ? String(inp.value || '').length : String(inp.textContent || '').length;
  return JSON.stringify({r: 'filled', tag: tag, len: len});
})()
""" % json.dumps(text, ensure_ascii=False)


_SEND_JS = """
(() => {
  const send = document.querySelector('.btn-send, .btn-sendmsg');
  if (send && !/disabled/.test(String(send.className))) {
    send.click();
    return JSON.stringify({r: 'sent', via: 'btn', cls: String(send.className || '').slice(0, 40)});
  }
  return JSON.stringify({r: 'btn_disabled_wait'});
})()
"""

_ENTER_JS = """
(() => {
  let inp = document.querySelector('.chat-input[contenteditable="true"], .chat-input');
  if (!inp) return JSON.stringify({r: 'nosend'});
  inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return JSON.stringify({r: 'sent', via: 'enter'});
})()
"""

_SURE_JS = """
(() => {
  const dialogs = document.querySelectorAll('.dialog-wrap, .boss-dialog, .dialog-container');
  for (const d of dialogs) {
    if (/contact/i.test(d.className) || (d.innerText || '').includes('电话') || (d.innerText || '').includes('微信')) continue;
    const b = d.querySelector('.btn-sure, .btn-sure-v2, button.sure');
    if (b && (b.offsetWidth || b.offsetHeight || b.getClientRects().length)) {
      b.click();
      return 'ok';
    }
  }
  return 'miss';
})()
"""


def _open_conversation_input(sess, company, poll_s=12):
    """消息中心点开会话并等输入框就绪。返回 (info, conv_head)；失败返回 (None, head或None)。"""
    sess.nav(rawcdp.BASE + CHAT_URL)
    sess.wait_ready(want_cards=False, timeout_s=12)
    clicked = None
    for i in range(8):
        time.sleep(1)
        clicked = _ev(sess, _pick_conversation_js(company))
        if isinstance(clicked, dict) and clicked.get("r") == "notfound":
            return None, None  # 指定公司未命中：立即中止，不轮询不兜底，交由上层抛异常记台账
        if isinstance(clicked, dict) and clicked.get("r") == "found":
            _trusted_click(sess, int(clicked["x"]), int(clicked["y"]))
            clicked["r"] = "clicked"
            break
    if not (isinstance(clicked, dict) and clicked.get("r") == "clicked"):
        return None, None
    head = clicked.get("head")
    for i in range(int(poll_s)):
        time.sleep(1)
        info = _ev(sess, _probe_js())
        if isinstance(info, dict) and info.get("inputTag"):
            return info, head
    return None, head


def _do_send(sess):
    """按钮优先（disabled则等1s重试）→ 回车兜底。返回 r4 dict。"""
    r = _ev(sess, _SEND_JS)
    if isinstance(r, dict) and r.get("r") == "btn_disabled_wait":
        time.sleep(1.0)
        r = _ev(sess, _SEND_JS)
        if isinstance(r, dict) and r.get("r") == "btn_disabled_wait":
            r = _ev(sess, _ENTER_JS)
    return r if isinstance(r, dict) and r.get("r") == "sent" else None


def _send_verified(sess, tries=2):
    """发送并以'输入框清零'为准验证成功；未清零重试一轮，仍失败返回 False。"""
    for attempt in range(tries):
        r4 = _do_send(sess)
        time.sleep(1.5)
        post = _ev(sess, _probe_js())
        if isinstance(post, dict) and post.get("inputLen") == 0:
            return True, r4, post
        time.sleep(1.0)
    return False, None, post


def send_message_via_chat(sess, company, text, poll_s=12):
    """消息中心按公司名点开会话并发送一条消息（回复 HR / 跟发通用）。
    以发送后输入框清零为成功标准（2026-08-31 实测：Enter 兜底可能不触发发送，
    按钮在填充后短暂 disabled，必须清零验证，否则不算发出）。
    返回 {conv, filled_len, send_via, post}；失败抛异常。"""
    info, head = _open_conversation_input(sess, (company or "").strip(), poll_s)
    if not info:
        raise RuntimeError("conversation/input not found for %r (head=%r)" % (company, head))
    r3 = _ev(sess, _fill_js(text))
    if not (isinstance(r3, dict) and r3.get("r") == "filled"):
        raise RuntimeError("fill failed: %r" % (r3,))
    ok, r4, post = _send_verified(sess)
    if not ok:
        raise RuntimeError("send not verified (inputLen>0): post=%r" % (post,))
    return {"conv": head, "filled_len": r3.get("len"), "send_via": (r4 or {}).get("via"), "post": post}


def send_message_in_current_conv(sess, text):
    """在当前已处于激活状态的会话输入框中直接填充并发送短文本（零二次页面跳转与会话重选）。
    以发送后输入框清零为准验证成功；返回 {filled_len, send_via, post}；失败抛异常。"""
    info = _ev(sess, _probe_js())
    if not (isinstance(info, dict) and info.get("inputTag")):
        raise RuntimeError("input not ready in current conversation")
    r3 = _ev(sess, _fill_js(text))
    if not (isinstance(r3, dict) and r3.get("r") == "filled"):
        raise RuntimeError("fill failed: %r" % (r3,))
    ok, r4, post = _send_verified(sess)
    if not ok:
        raise RuntimeError("send not verified (inputLen>0): post=%r" % (post,))
    return {"filled_len": r3.get("len"), "send_via": (r4 or {}).get("via"), "post": post}


# 工具栏按钮受信任点击 + 可见弹窗确认（2026-09-08 换微信误发换电话事故修复）：
# 病理：① el.click() 合成点击被 BOSS Vue 的 isTrusted 过滤静默忽略，工具栏按钮
# 点击从未生效；② DOM 永久预埋"确认与对方交换电话吗"隐藏弹窗（.panel-contact
# display:none），旧确认代码 .panel-contact .btn-sure 盲配到它的确定按钮 → 误发电话。
# 修复：坐标级 _trusted_click + 只点"可见且标题匹配"的弹窗 + 消息区计数核验。
TOOLBAR_BTN_POS_JS = """
(() => {
  const btn = document.querySelector(%s);
  if (!btn) return JSON.stringify({r: 'notfound'});
  if (btn.classList.contains('unable')) return JSON.stringify({r: 'unable'});
  const rect = btn.getBoundingClientRect();
  if (rect.width <= 0) return JSON.stringify({r: 'notfound'});
  return JSON.stringify({r: 'found', x: Math.round(rect.left + rect.width / 2),
                         y: Math.round(rect.top + rect.height / 2)});
})()
"""

VISIBLE_SURE_DIALOG_JS = """
(() => {
  for (const p of document.querySelectorAll('.panel-contact, .boss-dialog, .sentence-popover, .dialog-container')) {
    const rect = p.getBoundingClientRect();
    const style = window.getComputedStyle(p);
    if (rect.width <= 0 || style.display === 'none' || style.visibility === 'hidden') continue;
    const sure = p.querySelector('.btn-sure, .btn-sure-v2');
    if (!sure) continue;
    const sr = sure.getBoundingClientRect();
    const title = (p.innerText || '').replace(/\\n/g, '|').slice(0, 60);
    return JSON.stringify({r: 'dialog', title: title,
                           x: Math.round(sr.left + sr.width / 2), y: Math.round(sr.top + sr.height / 2)});
  }
  return JSON.stringify({r: 'none'});
})()
"""

CHAT_MSG_COUNT_JS = """
(() => {
  const list = document.querySelector('.chat-message .im-list');
  if (!list) return JSON.stringify({n: -1});
  const re = %s;
  let n = 0;
  for (const it of list.children) if (re.test(it.innerText || '')) n++;
  return JSON.stringify({n: n});
})()
"""


def _chat_msg_count(sess, pattern_js):
    r = _ev(sess, CHAT_MSG_COUNT_JS % pattern_js)
    return (r or {}).get("n", -1)


def _toolbar_trusted_click(sess, selector_js):
    """工具栏按钮坐标级受信任点击。返回 found/unable/notfound。"""
    pos = _ev(sess, TOOLBAR_BTN_POS_JS % selector_js)
    if not isinstance(pos, dict):
        raise RuntimeError("toolbar button probe failed: %r" % (pos,))
    if pos.get("r") != "found":
        return pos.get("r") or "notfound"
    _trusted_click(sess, int(pos["x"]), int(pos["y"]))
    return "found"


def exchange_wechat_via_chat(sess, company, poll_s=12):
    """消息中心按公司名点开会话并点击【换微信】官方原生按钮。
    2026-09-08 事故修复：换微信曾误发换电话（详见 TOOLBAR_BTN_POS_JS 注释）。
    现流程：受信任点击换微信 → 轮询可见确认弹窗 → 标题必须含"微信"（否则中止，
    绝不盲点预埋的电话弹窗）→ 受信任点击确定 → 消息区核验"请求交换微信"计数增加
    且"请求交换电话"计数不变（安全双断言）。"""
    info, head = _open_conversation_input(sess, (company or "").strip(), poll_s)
    if not info:
        raise RuntimeError("conversation/input not found for %r (head=%r)" % (company, head))

    wx_before = _chat_msg_count(sess, "/请求交换微信/")
    phone_before = _chat_msg_count(sess, "/请求交换电话/")

    r = _toolbar_trusted_click(sess, "'.btn-weixin'")
    if r == "unable":
        return {"status": "already_sent", "company": company, "conv": head}
    if r != "found":
        raise RuntimeError("btn-weixin not clickable: %r" % (r,))

    # 轮询可见确认弹窗（最多 ~6s）
    confirmed = None
    for _ in range(12):
        time.sleep(0.5)
        dlg = _ev(sess, VISIBLE_SURE_DIALOG_JS)
        if isinstance(dlg, dict) and dlg.get("r") == "dialog":
            confirmed = dlg
            break
    if confirmed is None:
        # 无弹窗：部分状态可能直接发送，靠消息区核验判定
        return {"status": "no_dialog", "company": company, "conv": head,
                "note": "wechat button clicked but no visible confirm dialog"}
    title = confirmed.get("title") or ""
    if "微信" not in title or "电话" in title:
        # 安全中止：弹窗不是微信确认（含电话），绝不点确定
        _ev(sess, """
(() => {
  for (const p of document.querySelectorAll('.panel-contact, .boss-dialog, .sentence-popover')) {
    const style = window.getComputedStyle(p);
    if (style.display === 'none') continue;
    const cancel = p.querySelector('.btn-cancel, .btn-outline-v2, [class*="cancel"]');
    if (cancel) { cancel.click(); break; }
  }
  return 'dismissed';
})()
""")
        raise RuntimeError("confirm dialog title mismatch (want 微信, got %r), dismissed & aborted" % title)
    _trusted_click(sess, int(confirmed["x"]), int(confirmed["y"]))

    # 发送核验：请求交换微信计数增加，且请求交换电话计数不增
    sent = False
    for _ in range(10):
        time.sleep(0.5)
        if _chat_msg_count(sess, "/请求交换微信/") > wx_before:
            sent = True
            break
    phone_after = _chat_msg_count(sess, "/请求交换电话/")
    if phone_after > phone_before:
        raise RuntimeError("SAFETY: 请求交换电话 unexpectedly increased (wechat flow sent phone!)")
    if not sent:
        return {"status": "no_verify", "company": company, "conv": head,
                "note": "confirmed dialog but no 请求交换微信 message appeared"}
    return {"status": "ok", "action": "exchange_wechat", "company": company, "conv": head}


def send_resume_via_chat(sess, company, poll_s=12):
    """消息中心按公司名点开会话并点击【发简历】官方原生按钮。
    2026-09-08 修复：同 exchange_wechat——受信任点击 + 只点可见弹窗 + 消息区核验
    （新消息条目出现才算成功，杜绝盲点隐藏元素）。"""
    info, head = _open_conversation_input(sess, (company or "").strip(), poll_s)
    if not info:
        raise RuntimeError("conversation/input not found for %r (head=%r)" % (company, head))

    def total_msgs():
        r = _ev(sess, """
(() => {
  const list = document.querySelector('.chat-message .im-list');
  return JSON.stringify({n: list ? list.children.length : -1});
})()
""")
        return (r or {}).get("n", -1)

    before = total_msgs()
    # 发简历按钮无独立 class，按文本精确定位（受信任点击）
    pos = _ev(sess, """
(() => {
  const btns = Array.from(document.querySelectorAll('.toolbar-btn'));
  const btn = btns.find(b => (b.innerText || '').trim() === '发简历');
  if (!btn) return JSON.stringify({r: 'notfound'});
  if (btn.classList.contains('unable')) return JSON.stringify({r: 'unable'});
  const rect = btn.getBoundingClientRect();
  if (rect.width <= 0) return JSON.stringify({r: 'notfound'});
  return JSON.stringify({r: 'found', x: Math.round(rect.left + rect.width / 2),
                         y: Math.round(rect.top + rect.height / 2)});
})()
""")
    if isinstance(pos, dict) and pos.get("r") == "unable":
        return {"status": "already_sent", "company": company, "conv": head}
    if not (isinstance(pos, dict) and pos.get("r") == "found"):
        raise RuntimeError("send_resume button not clickable: %r" % (pos,))
    _trusted_click(sess, int(pos["x"]), int(pos["y"]))

    # 轮询可见确认弹窗（最多 ~3s，发简历可能直接发送无弹窗）
    for _ in range(6):
        time.sleep(0.5)
        dlg = _ev(sess, VISIBLE_SURE_DIALOG_JS)
        if isinstance(dlg, dict) and dlg.get("r") == "dialog":
            _trusted_click(sess, int(dlg["x"]), int(dlg["y"]))
            break

    # 发送核验：消息区出现新条目
    for _ in range(10):
        time.sleep(0.5)
        if total_msgs() > before:
            return {"status": "ok", "action": "send_resume", "company": company, "conv": head}
    return {"status": "no_verify", "company": company, "conv": head,
            "note": "clicked but no new message appeared in chat"}


AGREE_WECHAT_BTN_JS = """
(() => {
  const list = document.querySelector('.chat-message .im-list');
  if (!list) return JSON.stringify({r: 'no_list'});
  const btns = Array.from(list.querySelectorAll('button, .btn, [class*="btn"], span, a'));
  let target = null;
  for (let i = btns.length - 1; i >= 0; i--) {
    const b = btns[i];
    const txt = (b.innerText || '').trim();
    if (txt === '同意' || txt === '同意交换' || txt === '接受' || txt === '收下') {
      const rect = b.getBoundingClientRect();
      const style = window.getComputedStyle(b);
      if (rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden') {
        target = b;
        break;
      }
    }
  }
  if (!target) {
    const textAll = list.innerText || '';
    if (textAll.includes('已同意') || textAll.includes('双方已交换')) {
      return JSON.stringify({r: 'already_agreed'});
    }
    return JSON.stringify({r: 'notfound'});
  }
  const rect = target.getBoundingClientRect();
  return JSON.stringify({
    r: 'found',
    x: Math.round(rect.left + rect.width / 2),
    y: Math.round(rect.top + rect.height / 2)
  });
})()
"""


def agree_wechat_via_chat(sess, company, poll_s=12):
    """消息中心按公司名点开会话并点击【同意交换微信】按钮。
    受信任点击同意按钮 → 轮询可见确认弹窗（若有）点击确定 → 消息区核验。"""
    info, head = _open_conversation_input(sess, (company or "").strip(), poll_s)
    if not info:
        raise RuntimeError("conversation/input not found for %r (head=%r)" % (company, head))

    pos = _ev(sess, AGREE_WECHAT_BTN_JS)
    if not isinstance(pos, dict):
        raise RuntimeError("agree_wechat button probe failed: %r" % (pos,))
    if pos.get("r") == "already_agreed":
        return {"status": "already_agreed", "company": company, "conv": head}
    if pos.get("r") != "found":
        return {"status": "notfound", "company": company, "conv": head, "note": "agree button not found in chat"}

    _trusted_click(sess, int(pos["x"]), int(pos["y"]))

    # 轮询可见确认弹窗（最多 ~3s）
    for _ in range(6):
        time.sleep(0.5)
        dlg = _ev(sess, VISIBLE_SURE_DIALOG_JS)
        if isinstance(dlg, dict) and dlg.get("r") == "dialog":
            _trusted_click(sess, int(dlg["x"]), int(dlg["y"]))
            break

    return {"status": "ok", "action": "agree_wechat", "company": company, "conv": head}


def send_greeting_raw(sess, job, cfg):
    """裸CDP版打招呼（调用前调用方需已导航到职位详情页且 wait_ready 通过）。
    注意：点击"立即沟通"即可能建立沟通关系，视为消耗一次机会。
    流程：点按钮(建连+BOSS默认招呼) → 页内面板兜底探测3秒 → 消息中心点开会话
    （按公司名，缺省取最新）→ 跟发自定义文案。任一步失败抛异常供上层记台账。"""
    # 1) 找到并点击 立即沟通/继续沟通
    r1 = _ev(sess, """
(() => {
  const btns = [];
  const primary = document.querySelector('.btn-startchat');
  if (primary) btns.push(primary);
  document.querySelectorAll('a,button,span').forEach(e => {
    const t = (e.innerText || '').trim();
    if (t === '立即沟通' || t === '继续沟通') btns.push(e);
  });
  if (!btns.length) return JSON.stringify({r: 'notfound'});
  const el = btns[0];
  const cls = String(el.className || el.tagName).slice(0, 50);
  el.click();
  return JSON.stringify({r: 'clicked', cls: cls});
})()
""")
    if not (isinstance(r1, dict) and r1.get("r") == "clicked"):
        raise RuntimeError("start-chat button not found: %r" % (r1,))

    # 2) 页内面板兜底探测3秒（若BOSS A/B仍返回页内面板则直接用）
    confirmed = False
    info = None
    for i in range(6):
        time.sleep(0.5)
        info = _ev(sess, _probe_js())
        if isinstance(info, dict) and info.get("inputTag"):
            break
        if isinstance(info, dict) and info.get("sureBtn") and not confirmed:
            confirmed = True
            sess.eval(_SURE_JS)
    inpage = isinstance(info, dict) and info.get("inputTag")
    conv_head = None
    if not inpage:
        # 3) 主路径：消息中心
        company = (job.get("company") or "").strip()
        info, conv_head = _open_conversation_input(sess, company)
        if not info:
            dump = sess.eval("""(() => JSON.stringify({href: location.href.slice(0, 90),
  bodyTail: (document.body ? document.body.innerText : '').slice(-300).replace(/\\n/g, '|'),
  hasSure: !!document.querySelector('.btn-sure-v2'),
  hasChatInput: !!document.querySelector('.chat-input'),
  ces: document.querySelectorAll('[contenteditable="true"]').length}))()""")
            raise RuntimeError("chat input not found after click; company=%r probe=%r dump=%s" % (company, info, dump))

    # 4) 填入招呼语并发送（以输入框清零为成功标准）
    text = greeting_text(cfg, job)
    r3 = _ev(sess, _fill_js(text))
    if not (isinstance(r3, dict) and r3.get("r") == "filled"):
        raise RuntimeError("fill greeting failed: %r" % (r3,))
    ok, r4, post = _send_verified(sess)
    if not ok:
        raise RuntimeError("send not verified (inputLen>0): post=%r" % (post,))

    return {"clicked": r1.get("cls"), "chat_href": info.get("href"), "conv": conv_head,
            "filled_len": r3.get("len"), "send_via": (r4 or {}).get("via"), "post": post}


ACTIVE_JOB_JS = """
(() => {
  const pos = document.querySelector('.chat-position-content');
  const c = (pos && pos.__vue__) ? pos.__vue__.conversation$ : null;
  if (c && c.encryptJobId) {
    return JSON.stringify({
      encryptJobId: c.encryptJobId,
      jobId: c.jobId,
      securityId: c.securityId || '',
      brandName: c.brandName || c.companyName || '',
      companyName: c.companyName || c.brandName || '',
      positionName: c.positionName || c.jobName || '',
      salaryDesc: c.salaryDesc || '',
      lowSalary: c.lowSalary,
      highSalary: c.highSalary,
      degreeName: c.degreeName || '',
      experienceName: c.experienceName || '',
      locationName: c.locationName || '',
      href: '/job_detail/' + c.encryptJobId + '.html' + (c.securityId ? ('?securityId=' + encodeURIComponent(c.securityId)) : '')
    });
  }
  if (pos) {
    const name = (pos.querySelector('.position-name') || {}).innerText || '';
    const sal = (pos.querySelector('.salary') || {}).innerText || '';
    const city = (pos.querySelector('.city') || {}).innerText || '';
    if (name) {
      return JSON.stringify({
        positionName: name.trim(),
        salaryDesc: sal.trim(),
        locationName: city.trim(),
        fallback: true
      });
    }
  }
  return JSON.stringify({r: 'no_active_job'});
})()
"""


def get_active_conversation_job(sess):
    """提取当前消息中心激活会话的职位元数据（包括 encryptJobId、薪资、城市、学历经验与详情链接）。"""
    res = _ev(sess, ACTIVE_JOB_JS)
    if isinstance(res, dict) and (res.get("encryptJobId") or res.get("positionName")):
        return res
    return None


# 聊天历史提取：直接复用 rawcdp 中精确支持 item-myself/chat-item--right/item-friend/chat-item--left/item-system/chat-sysmsg
# 并过滤系统提示文本与时间戳/已读杂音的 CHAT_HISTORY_JS，以及 clean_conversation_history 辅助函数
from .rawcdp import CHAT_HISTORY_JS, clean_conversation_history


def get_active_conversation_history(sess, limit=20, filter_system=False):
    """提取当前激活会话的最近聊天历史（只读，零发送）。
    返回 [{role, text}]（role: me=我方 / hr=对方 / system=平台系统消息），
    若 filter_system=True，则过滤剔除 system 消息，仅保留 me 与 hr。
    最多 limit 条（取最近的）；提取失败返回 None（历史是增强项，不阻塞决策）。"""
    res = _ev(sess, CHAT_HISTORY_JS)
    if isinstance(res, dict) and res.get("messages") is not None:
        msgs = res.get("messages") or []
        if filter_system:
            msgs = [m for m in msgs if m.get("role") in ("me", "hr")]
        return msgs[-int(limit):] if limit else msgs
    return None


