"""diag v6：侦察沟通弹窗的真实DOM（按钮/输入框/已发消息）。
对象：整数智能（已建立连接，重开聊天不会重复打招呼）。
"""
import json
import sys
import time

sys.path.insert(0, ".")

from boss_apply import config as cfgmod, rawcdp

cfg = cfgmod.load()
s = rawcdp.RawCDP(cfg["cdp_endpoint"])
s.open_tab()

# 整数智能的详情页
s.nav("https://www.zhipin.com/job_detail/25461b057487c9240nJz39m4E1VU.html")
st = s.wait_ready(want_cards=False, timeout_s=15)
print("详情页就绪:", bool(st), "| bodyLen:", (st or {}).get("bodyLen"))

r = s.eval("""
(() => {
  const btns = [];
  const primary = document.querySelector('.btn-startchat');
  if (primary) btns.push(primary);
  document.querySelectorAll('a,button,span').forEach(e => {
    const t = (e.innerText || '').trim();
    if (t === '立即沟通' || t === '继续沟通') btns.push(e);
  });
  if (!btns.length) return JSON.stringify({r: 'notfound'});
  btns[0].click();
  return JSON.stringify({r: 'clicked', cls: String(btns[0].className || '').slice(0, 60)});
})()
""")
print("点击:", r)
time.sleep(4)

RECON = r"""
(() => {
  const dump = [];
  document.querySelectorAll('button, [class*=send], [class*=btn]').forEach(e => {
    const t = (e.innerText || '').trim().slice(0, 16);
    if (t || String(e.className || '').match(/send/i)) dump.push({tag: e.tagName, cls: String(e.className || '').slice(0, 60), text: t});
  });
  const inputs = [];
  document.querySelectorAll('textarea, input[type=text], [contenteditable="true"]').forEach(e => {
    inputs.push({tag: e.tagName, cls: String(e.className || '').slice(0, 60),
                 ce: e.getAttribute('contenteditable'),
                 val: (e.value || e.textContent || '').slice(0, 60),
                 ph: e.getAttribute('placeholder') || ''});
  });
  const dlg = document.querySelector('.dialog-chat, [class*=dialog], [class*=modal], [class*=chat-conversation]');
  const body = document.body.innerText;
  return JSON.stringify({
    href: location.href.slice(0, 90),
    buttons: dump.slice(0, 25),
    inputs: inputs,
    dialogCls: dlg ? String(dlg.className).slice(0, 60) : null,
    bodyTail: body.slice(-350).replace(/\n/g, '|')
  });
})()
"""
v = s.eval(RECON)
print(json.dumps(json.loads(v), ensure_ascii=False, indent=1)[:2600])

s.close_tab()
s.close()
