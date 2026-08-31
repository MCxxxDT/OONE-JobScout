"""打招呼/回复执行。文案随 profile 切换：test=中性文案（不含任何个人信息）。
两套实现：send_greeting(playwright，仅限已存活页，弃用参考) / send_greeting_raw(裸CDP，主力)。
send_message_via_chat = 消息中心按公司名发消息（回复 HR 用）。
2026-08-31 适配：BOSS 聊天界面已迁移至 /web/geek/chat（详情页内嵌面板不再出现）。"""
import json
import random
import time

from . import rawcdp
from .browser import check_risk

CHAT_SELECTORS = [".chat-input", ".dialog-chat textarea", "textarea.chat-input", ".chat-conversation textarea", ".chat-input textarea"]
SEND_SELECTORS = [".btn-send", "button:has-text('发送')"]

CHAT_URL = "/web/geek/chat"


def greeting_text(cfg, job):
    profile = cfg.get("profile", "test")
    templates = (cfg.get("greeting", {}) or {}).get(profile) or cfg["greeting"]["test"]
    t = random.choice(templates)
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
  return JSON.stringify({
    href: location.href.slice(0, 100),
    inputTag: inp ? (inp.tagName + '|' + String(inp.className || '').slice(0, 40)) : null,
    inputLen: inp ? String(inp.value || inp.textContent || '').length : -1,
    sendDisabled: send ? /disabled/.test(String(send.className)) : null,
    sureBtn: !!document.querySelector('.btn-sure-v2'),
    bodyLen: document.body ? document.body.innerText.length : 0
  });
})()
"""


def _pick_conversation_js(company):
    """消息中心会话定位：优先按公司名匹配，缺省取最新一条。
    不直接 el.click()（BOSS 列表项对合成 click 无响应，2026-08-31 实测），
    返回元素视口坐标，由调用方走 CDP Input.dispatchMouseEvent 派发受信任点击。"""
    return """
(() => {
  const company = %s;
  const lis = Array.from(document.querySelectorAll('li'));
  const isConv = (li) => {
    const t = li.innerText || '';
    return t.length > 12 && /\\d{1,2}:\\d{2}/.test(t);
  };
  let target = null, picked = 'none';
  if (company) {
    for (const li of lis) {
      if ((li.innerText || '').indexOf(company) >= 0 && isConv(li)) { target = li; picked = 'company'; break; }
    }
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

_SURE_JS = "(() => { const b = document.querySelector('.btn-sure-v2'); if (b) { b.click(); return 'ok'; } return 'miss'; })()"


def _open_conversation_input(sess, company, poll_s=12):
    """消息中心点开会话并等输入框就绪。返回 (info, conv_head)；失败返回 (None, head或None)。"""
    sess.nav(rawcdp.BASE + CHAT_URL)
    sess.wait_ready(want_cards=False, timeout_s=12)
    clicked = None
    for i in range(8):
        time.sleep(1)
        clicked = _ev(sess, _pick_conversation_js(company))
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
        if isinstance(info, dict) and info.get("sureBtn"):
            sess.eval(_SURE_JS)
    return None, head


def _do_send(sess):
    """弹窗点掉 → 按钮优先（disabled则等1s重试）→ 回车兜底。返回 r4 dict。"""
    _ev(sess, _SURE_JS)
    r = _ev(sess, _SEND_JS)
    if isinstance(r, dict) and r.get("r") == "btn_disabled_wait":
        time.sleep(1.0)
        _ev(sess, _SURE_JS)
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
