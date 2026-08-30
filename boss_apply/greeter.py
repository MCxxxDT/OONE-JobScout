"""打招呼执行。文案随 profile 切换：test=中性文案（不含任何个人信息）。
两套实现：send_greeting(playwright，仅限已存活页) / send_greeting_raw(裸CDP，主力)。"""
import json
import random
import time

from .browser import check_risk

CHAT_SELECTORS = [".chat-input", ".dialog-chat textarea", "textarea.chat-input", ".chat-conversation textarea", ".chat-input textarea"]
SEND_SELECTORS = [".btn-send", "button:has-text('发送')"]


def greeting_text(cfg, job):
    profile = cfg.get("profile", "test")
    templates = (cfg.get("greeting", {}) or {}).get(profile) or cfg["greeting"]["test"]
    t = random.choice(templates)
    return t.replace("{job}", job.get("title") or "该岗位")


def send_greeting(page, job, cfg):
    """在当前职位详情页发送一条招呼。失败抛异常，由上层记台账并人工检查。"""
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


def send_greeting_raw(sess, job, cfg):
    """裸CDP版打招呼（调用前调用方需已导航到职位详情页且 wait_ready 通过）。
    注意：点击"立即沟通"即可能建立沟通关系，视为消耗一次机会。
    步骤全部带埋点，任一步失败抛异常供上层记台账。"""
    # 1) 找到并点击 立即沟通
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

    # 等待聊天面板（自包含定位，不依赖注入状态）；若出现确定类弹窗（首次沟通确认）自动点一次
    info = None
    confirmed = False
    for i in range(14):
        time.sleep(1)
        info = _ev(sess, _probe_js())
        if isinstance(info, dict) and info.get("inputTag"):
            break
        if isinstance(info, dict) and info.get("sureBtn") and not confirmed:
            confirmed = True
            sess.eval("(() => { const b = document.querySelector('.btn-sure-v2'); if (b) { b.click(); return 'ok'; } return 'miss'; })()")
    if not (isinstance(info, dict) and info.get("inputTag")):
        dump = sess.eval("""(() => JSON.stringify({href: location.href.slice(0, 90),
  bodyTail: (document.body ? document.body.innerText : '').slice(-300).replace(/\\n/g, '|'),
  hasSure: !!document.querySelector('.btn-sure-v2'),
  hasChatInput: !!document.querySelector('.chat-input'),
  ces: document.querySelectorAll('[contenteditable="true"]').length}))()""")
        raise RuntimeError("chat input not found after click; probe=%r dump=%s" % (info, dump))

    # 3) 填入招呼语（.chat-input 是 contenteditable，execCommand 会触发真实输入事件）
    text = greeting_text(cfg, job)
    fill_js = """
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
    r3 = _ev(sess, fill_js)
    if not (isinstance(r3, dict) and r3.get("r") == "filled"):
        raise RuntimeError("fill greeting failed: %r" % (r3,))

    # 4) 点发送；若按钮仍 disabled 则对输入框派发 Enter 键（BOSS 支持回车发送）
    r4 = _ev(sess, """
(() => {
  const send = document.querySelector('.btn-send, .btn-sendmsg');
  if (send && !/disabled/.test(String(send.className))) {
    send.click();
    return JSON.stringify({r: 'sent', via: 'btn', cls: String(send.className || '').slice(0, 40)});
  }
  let inp = document.querySelector('.chat-input[contenteditable="true"], .chat-input');
  if (!inp) return JSON.stringify({r: 'nosend'});
  inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return JSON.stringify({r: 'sent', via: 'enter'});
})()
""")
    if not (isinstance(r4, dict) and r4.get("r") == "sent"):
        raise RuntimeError("send failed: %r (chat state=%r)" % (r4, info))

    # 5) 发送后验证（输入框被清空 = 常见成功信号；最终以人工查看消息列表为准）
    time.sleep(1.5)
    post = _ev(sess, _probe_js())
    return {"clicked": r1.get("cls"), "chat_href": info.get("href"), "filled_len": r3.get("len"),
            "send_via": r4.get("via"), "post": post}
