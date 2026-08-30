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


def _pick_conversation_js(company):
    """消息中心会话定位：优先按公司名匹配，缺省取最新一条（刚建连的会话排最前）。"""
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
  target.click();
  return JSON.stringify({r: 'clicked', picked: picked, head: head});
})()
""" % json.dumps(company or "", ensure_ascii=False)


def send_greeting_raw(sess, job, cfg):
    """裸CDP版打招呼（调用前调用方需已导航到职位详情页且 wait_ready 通过）。
    注意：点击"立即沟通"即可能建立沟通关系，视为消耗一次机会。
    2026-08-31 改版适配：点击后页内聊天面板不再出现，聊天界面改为
    /web/geek/chat（曾观察到新标签页形式，且该标签页短命会自关）。
    故统一路径 = 点按钮(建连+BOSS默认招呼) → 本标签页导航到消息中心
    → 按公司名点开会话(找不到则取最新一条,适用于刚建连场景) → 跟发自定义文案。
    步骤全部带埋点，任一步失败抛异常供上层记台账。"""
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
    inpage = isinstance(info, dict) and info.get("inputTag")
    conv_head = None
    if not inpage:
        # 3) 主路径：消息中心 /web/geek/chat → 点开会话
        sess.nav(rawcdp.BASE + "/web/geek/chat")
        sess.wait_ready(want_cards=False, timeout_s=12)
        clicked = None
        for i in range(8):
            time.sleep(1)
            clicked = _ev(sess, _pick_conversation_js((job.get("company") or "").strip()))
            if isinstance(clicked, dict) and clicked.get("r") == "clicked":
                break
        if not (isinstance(clicked, dict) and clicked.get("r") == "clicked"):
            raise RuntimeError("conversation not found on chat page: %r" % (clicked,))
        conv_head = clicked.get("head")
        info = None
        for i in range(12):
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
    return {"clicked": r1.get("cls"), "chat_href": info.get("href"), "conv": conv_head,
            "filled_len": r3.get("len"), "send_via": r4.get("via"), "post": post}
