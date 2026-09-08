"""BOSS直聘 求职守护审批台：局域网 Web 工作台（FastAPI，零外部依赖新增）。

定位（2026-09-08 方案A）：
- 本机/局域网手机浏览器访问的轻量工作台，替代飞书卡片按钮（webhook 不支持交互回调）；
- 看板：待人工处理会话、今日统计、护栏状态、最近台账流水；
- 操作：一键发送推荐回复 / 官方换微信 / 发简历 / 忽略（复用 feishu_bot.handle_card_action 管线，
  即飞书卡片设计的同一条操作路由，含隐私红线与台账留痕）；
- 安全：token 查询参数认证（config.web.token 或环境变量 APPROVAL_TOKEN），默认仅绑定 127.0.0.1；
  局域网手机访问需把 host 改为本机局域网 IP（0.0.0.0 + 防火墙放行），外网经 Tailscale。

启动：
  python scripts/approval_web.py                       # 127.0.0.1:8788
  python scripts/approval_web.py --host 0.0.0.0        # 局域网手机访问（含 Tailscale IP）
  token 默认取 config.local.json 的 web.token，缺省 "boss-apply"
  访问：http://127.0.0.1:8788/?token=boss-apply
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from boss_apply import ai_reply as air, config as cfgmod, feishu_bot, flows, guard as guardmod, ledger

app = FastAPI(title="boss-apply 审批台")

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BOSS求职守护 · 审批台</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --txt:#e2e8f0; --mut:#94a3b8; --acc:#38bdf8; --ok:#34d399; --warn:#fbbf24; --dan:#f87171; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font-family:-apple-system,"Microsoft YaHei",sans-serif; padding:16px; max-width:860px; margin:0 auto; }
  h1 { font-size:20px; margin-bottom:4px; } .sub { color:var(--mut); font-size:12px; margin-bottom:16px; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; margin-bottom:16px; }
  .stat { background:var(--card); border-radius:10px; padding:12px; text-align:center; }
  .stat b { display:block; font-size:22px; color:var(--acc); }
  .stat span { font-size:12px; color:var(--mut); }
  .conv { background:var(--card); border-radius:10px; padding:14px; margin-bottom:10px; }
  .conv .who { font-weight:700; font-size:15px; }
  .conv .who .hi { color:var(--dan); font-size:12px; }
  .conv .msg { color:var(--mut); font-size:13px; margin:6px 0; white-space:pre-wrap; }
  .conv .draft { background:#0f172a; border-radius:6px; padding:8px; font-size:13px; margin:6px 0; }
  .btns { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
  button { border:0; border-radius:8px; padding:9px 14px; font-size:13px; cursor:pointer; color:#fff; }
  .b1 { background:var(--acc); } .b2 { background:#475569; } .b3 { background:var(--dan); }
  .ok { color:var(--ok); font-size:13px; margin-left:8px; }
  .err { color:var(--dan); font-size:13px; margin-left:8px; }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  td { padding:4px 6px; border-bottom:1px solid #334155; color:var(--mut); }
  .muted { color:var(--mut); font-size:12px; }
</style>
</head>
<body>
<h1>🤖 BOSS求职守护 · 审批台</h1>
<div class="sub" id="sub">加载中…</div>
<div class="stats" id="stats"></div>
<h1 style="font-size:16px">📋 待人工决策（needs_human）</h1>
<div id="pending">加载中…</div>
<h1 style="font-size:16px;margin-top:16px">📜 最近台账流水</h1>
<table id="ledger"></table>
<script>
let TOKEN = new URLSearchParams(location.search).get('token') || '';
async function api(path, opts) {
  const r = await fetch(path + '?token=' + encodeURIComponent(TOKEN), opts);
  if (r.status === 401) { document.body.innerHTML = '<h1>401</h1><p>token 无效，请检查 config web.token</p>'; throw new Error('401'); }
  return r.json();
}
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
async function load() {
  const d = await api('/api/overview');
  document.getElementById('sub').textContent = '刷新于 ' + new Date().toLocaleTimeString() + ' · 护栏: ' + (d.guard.paused ? '⛔熔断:' + d.guard.paused : '✅正常') + ' · 今日扫描 ' + d.today.scanned + ' / 实发回复 ' + d.today.replied;
  document.getElementById('stats').innerHTML = `
    <div class="stat"><b>${d.counts.pending}</b><span>待人工</span></div>
    <div class="stat"><b>${d.counts.replied_total}</b><span>累计回复</span></div>
    <div class="stat"><b>${d.today.replied}</b><span>今日实发</span></div>
    <div class="stat"><b>${d.today.scanned}</b><span>今日扫描</span></div>
    <div class="stat"><b>${d.counts.high_intent}</b><span>高意向🔥</span></div>`;
  const p = document.getElementById('pending');
  if (!d.pending.length) { p.innerHTML = '<div class="muted">暂无待处理会话，守护一切正常 🎉</div>'; }
  else p.innerHTML = d.pending.map((c, i) => `
    <div class="conv">
      <div class="who">${esc(c.company)} ${c.high_intent ? '<span class="hi">🔥高意向</span>' : ''} <span class="muted">${esc(c.time)}</span></div>
      <div class="msg">HR: ${esc(c.last_msg)}</div>
      <div class="msg muted">决策理由: ${esc(c.reason)}</div>
      ${c.suggested ? `<div class="draft">📝 推荐回复: ${esc(c.suggested)}</div>` : ''}
      <div class="btns">
        ${c.suggested ? `<button class="b1" onclick="act(${i},'reply')">发送推荐回复</button>` : ''}
        <button class="b2" onclick="act(${i},'exchange_wechat')">换微信</button>
        <button class="b2" onclick="act(${i},'send_resume')">发简历</button>
        <button class="b3" onclick="act(${i},'ignore')">忽略</button>
        <span id="res${i}" class="ok"></span>
      </div>
    </div>`).join('');
  window._pending = d.pending;
  document.getElementById('ledger').innerHTML = d.ledger.map(r => `<tr><td>${esc(r.ts)}</td><td>${esc(r.action)}</td><td>${esc(r.company || r.title || '')}</td><td>${esc((r.status || r.reason || r.text_head || '').slice(0, 40))}</td></tr>`).join('');
}
async function act(i, action) {
  const c = window._pending[i];
  const el = document.getElementById('res' + i);
  el.className = 'ok'; el.textContent = '执行中…';
  const body = { action: action, company: c.company };
  if (action === 'reply') body.text = c.suggested;
  const d = await api('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  el.className = d.ok ? 'ok' : 'err';
  el.textContent = d.ok ? '✅ 已执行' : ('❌ ' + (d.error || '失败'));
  setTimeout(load, 1500);
}
load(); setInterval(load, 30000);
</script>
</body>
</html>"""


def _web_cfg(cfg):
    return cfg.get("web") or {}


def _check_token(cfg, token):
    want = _web_cfg(cfg).get("token") or os.getenv("APPROVAL_TOKEN") or "boss-apply"
    return bool(token) and token == want


@app.get("/", response_class=HTMLResponse)
def index(token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return HTMLResponse("<h1>401</h1><p>token 无效。访问: /?token=你的token（config web.token，缺省 boss-apply）</p>", status_code=401)
    return PAGE


@app.get("/api/overview")
def api_overview(token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = ledger.load_all()
    today = datetime.date.today().isoformat()

    # 统计
    replied_ok = [r for r in rows if r.get("action") == "reply" and r.get("status") == "ok"]
    scans = [r for r in rows if r.get("action") == "scan"]
    alerts = [r for r in rows if r.get("action") == "human_alert_card"]

    # 待人工：最近 7 天告警中，HR 未再回复（无 reply ok 晚于告警）的会话
    seen = {}
    for r in alerts:
        c = r.get("company") or ""
        if not c:
            continue
        seen[c] = r  # 覆盖为最近一次告警
    pending = []
    replied_rows = [r for r in rows if r.get("action") == "reply" and r.get("status") == "ok"]
    for c, a in seen.items():
        last_reply = max((r.get("ts") or "" for r in replied_rows if r.get("company") == c), default="")
        if last_reply >= (a.get("ts") or ""):
            continue  # 已回复过，不待处理
        pending.append({
            "company": c,
            "last_msg": (a.get("last_msg") or "")[:200],
            "reason": (a.get("reason") or "")[:150],
            "suggested": a.get("suggested_reply") or "",
            "time": a.get("ts") or "",
            "high_intent": bool(a.get("high_intent")),
        })
    pending.sort(key=lambda x: x["time"], reverse=True)

    return {
        "counts": {
            "pending": len(pending),
            "replied_total": len(replied_ok),
            "high_intent": len([p for p in pending if p["high_intent"]]),
        },
        "today": {
            "scanned": len([r for r in scans if (r.get("ts") or "").startswith(today)]),
            "replied": len([r for r in replied_ok if (r.get("ts") or "").startswith(today)]),
        },
        "guard": guardmod.Guard(cfg).summary(),
        "pending": pending[:20],
        "ledger": [
            {"ts": r.get("ts") or "", "action": r.get("action") or "",
             "company": r.get("company") or r.get("title") or "",
             "status": r.get("status") or r.get("reason") or r.get("text_head") or ""}
            for r in rows[-30:]
        ][::-1],
    }


@app.post("/api/action")
async def api_action(request: Request, token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    act = body.get("action")
    company = body.get("company") or ""
    if act not in ("reply", "exchange_wechat", "send_resume", "ignore") or not company:
        return JSONResponse({"ok": False, "error": "invalid action or company"}, status_code=400)
    if act == "reply" and not (body.get("text") or "").strip():
        return JSONResponse({"ok": False, "error": "reply text empty"}, status_code=400)
    # 复用飞书卡片设计的同一条操作路由（隐私红线+台账留痕都在里面）
    res = feishu_bot.handle_card_action(cfg, body)
    return res


def main():
    parser = argparse.ArgumentParser(description="BOSS直聘求职守护审批台（局域网Web工作台）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（手机局域网访问用 0.0.0.0，外网走 Tailscale）")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    import uvicorn
    cfg = cfgmod.load()
    token = _web_cfg(cfg).get("token") or os.getenv("APPROVAL_TOKEN") or "boss-apply"
    print("=" * 58)
    print("  BOSS直聘求职守护 · 审批台")
    print("  访问: http://%s:%d/?token=%s" % (args.host, args.port, token))
    print("  手机(同WiFi/Tailscale): 先查本机IP, 用 http://<本机IP>:%d/?token=%s" % (args.port, token))
    print("=" * 58)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
