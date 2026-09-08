"""BOSS直聘 求职守护审批台：局域网 Web 工作台（FastAPI）。

定位（2026-09-08 方案A + 2026-09-09 升级）：
- 看板：待人工处理会话、今日统计、护栏状态、最近台账流水；
- 操作：一键发送推荐回复 / 官方换微信 / 发简历 / 忽略（复用 feishu_bot.handle_card_action）；
- 设置（2026-09-09）：API Key 管理（DPAPI 加密落盘 + 测试连接）、求职偏好四项
  （向往/排斥 岗位×城市，留空=LLM 自主决断）、简历上传（pdf/docx/txt/md + 粘贴，
  LLM 提炼结构化画像）；
- 安全：token 查询参数认证（config.web.token），默认绑定 127.0.0.1，外网经 Tailscale。

启动：
  python scripts/approval_web.py                       # 127.0.0.1:8788
  python scripts/approval_web.py --host 0.0.0.0        # 局域网手机访问（含 Tailscale IP）
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

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

from boss_apply import ai_reply as air, config as cfgmod, feishu_bot, flows, \
    guard as guardmod, ledger, profile_store, secrets as secrets_mod

app = FastAPI(title="boss-apply 审批台")

ALLOWED_EXTS = (".pdf", ".docx", ".txt", ".md")
MAX_UPLOAD = 5 * 1024 * 1024  # 5MB


def _web_cfg(cfg):
    return cfg.get("web") or {}


def _check_token(cfg, token):
    want = _web_cfg(cfg).get("token") or os.getenv("APPROVAL_TOKEN") or "boss-apply"
    return bool(token) and token == want


def _write_local(section_key, section_updates):
    """深合并写入 config.local.json 的指定顶层段（保留其他段）。"""
    path = cfgmod.LOCAL_CFG_PATH
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    cur = data.get(section_key) or {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(section_updates or {})
    data[section_key] = cur
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _split_list(text):
    """逗号/顿号/换行分隔的文本 → 去空列表。"""
    if not text:
        return []
    out = []
    for part in str(text).replace("，", ",").replace("、", ",").replace("\n", ",").split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


def _key_source(cfg):
    """当前生效 LLM Key 的来源标注。"""
    if secrets_mod.has_secret("llm_api_key"):
        return "DPAPI（web端保存）"
    llm = cfg.get("llm") or {}
    if llm.get("api_key"):
        return "config.local.json"
    if os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"):
        return "环境变量"
    return "未配置"


def _llm_ping(base_url, api_key, model):
    """极小请求测试连通性。返回 (ok, latency_ms, error)。"""
    import time as _t
    import urllib.request
    try:
        t0 = _t.time()
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": 10}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=60) as resp:
            json.loads(resp.read().decode("utf-8"))
        return True, int((_t.time() - t0) * 1000), None
    except Exception as e:
        return False, 0, str(e)[:150]


@app.get("/api/settings")
def api_settings_get(token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    llm = cfg.get("llm") or {}
    prefs = cfg.get("prefs") or {}
    eff = flows.effective_cities(cfg)
    return {
        "llm": {
            "api_key_masked": secrets_mod.masked(llm.get("api_key") or ""),
            "key_source": _key_source(cfg),
            "base_url": llm.get("base_url") or "",
            "model": llm.get("model") or "",
        },
        "llm_match": {"enabled": (cfg.get("llm_match") or {}).get("enabled", True)},
        "prefs": {
            "want_jobs": prefs.get("want_jobs") or [],
            "avoid_jobs": prefs.get("avoid_jobs") or [],
            "want_cities": prefs.get("want_cities") or [],
            "avoid_cities": prefs.get("avoid_cities") or [],
        },
        "effective": {
            "cities": [c["name"] for c in eff["cities"]],
            "unknown_cities": eff["unknown"],
            "keywords": flows.effective_keywords(cfg)[:12],
        },
        "profile": profile_store.profile_meta(),
    }


@app.post("/api/settings")
async def api_settings_post(request: Request, token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    changed = []
    if body.get("clear_key"):
        secrets_mod.set_secret("llm_api_key", "")
        changed.append("已清除DPAPI密钥")
    elif "api_key" in body:
        key = (body.get("api_key") or "").strip()
        if key:
            secrets_mod.set_secret("llm_api_key", key)
            changed.append("API Key 已加密保存")
    llm_updates = {}
    if body.get("base_url"):
        llm_updates["base_url"] = body["base_url"].strip()
    if body.get("model"):
        llm_updates["model"] = body["model"].strip()
    if llm_updates:
        _write_local("llm", llm_updates)
        changed.append("LLM端点已更新")
    if "llm_match_enabled" in body:
        _write_local("llm_match", {"enabled": bool(body["llm_match_enabled"])})
        changed.append("智能匹配开关已更新")
    return {"ok": True, "changed": changed or ["无变更"]}


@app.post("/api/settings/test")
async def api_settings_test(request: Request, token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    llm = cfg.get("llm") or {}
    base = (body.get("base_url") or llm.get("base_url") or "").strip()
    model = (body.get("model") or llm.get("model") or "").strip()
    key = (body.get("api_key") or "").strip() or llm.get("api_key") or ""
    if not (base and key):
        return {"ok": False, "error": "缺少 base_url 或 api_key"}
    ok, ms, err = _llm_ping(base, key, model)
    return {"ok": ok, "latency_ms": ms, "error": err}


@app.post("/api/prefs")
async def api_prefs_post(request: Request, token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    prefs = {
        "want_jobs": _split_list(body.get("want_jobs")),
        "avoid_jobs": _split_list(body.get("avoid_jobs")),
        "want_cities": _split_list(body.get("want_cities")),
        "avoid_cities": _split_list(body.get("avoid_cities")),
    }
    _write_local("prefs", prefs)
    cfg2 = cfgmod.load()
    eff = flows.effective_cities(cfg2)
    return {"ok": True, "prefs": prefs,
            "effective_cities": [c["name"] for c in eff["cities"]],
            "unknown_cities": eff["unknown"]}


@app.get("/api/profile")
def api_profile_get(token: str = ""):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    meta = profile_store.profile_meta()
    meta["resume_preview"] = profile_store.get_resume()[:500]
    return meta


@app.post("/api/profile")
async def api_profile_post(request: Request, token: str = "",
                           file: UploadFile = File(None)):
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    text = ""
    source = "paste"
    if file is not None and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            return JSONResponse({"ok": False, "error": "仅支持 .pdf/.docx/.txt/.md"}, status_code=400)
        content = await file.read()
        if len(content) > MAX_UPLOAD:
            return JSONResponse({"ok": False, "error": "文件超过5MB上限"}, status_code=400)
        tmp = os.path.join(cfgmod.STATE_DIR, "_upload_resume" + ext)
        os.makedirs(cfgmod.STATE_DIR, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(content)
        text, err = profile_store.extract_text(tmp)
        try:
            os.remove(tmp)
        except Exception:
            pass
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        source = "file:" + ext
    else:
        body = await request.json()
        text = (body or {}).get("text") or ""
    if not text.strip():
        return JSONResponse({"ok": False, "error": "简历内容为空"}, status_code=400)
    profile_store.save_resume(text, source)
    refined, err = profile_store.refine_profile(text, cfg)
    meta = profile_store.profile_meta()
    meta["resume_preview"] = text[:300]
    if err:
        return {"ok": True, "saved": True, "refined": False, "refine_error": err, "meta": meta}
    return {"ok": True, "saved": True, "refined": True,
            "refined_summary": (refined or {}).get("summary") or "", "meta": meta}


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
  h1 { font-size:20px; margin-bottom:4px; } h2 { font-size:15px; margin:18px 0 8px; }
  .sub { color:var(--mut); font-size:12px; margin-bottom:16px; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:10px; margin-bottom:16px; }
  .stat { background:var(--card); border-radius:10px; padding:12px; text-align:center; }
  .stat b { display:block; font-size:22px; color:var(--acc); }
  .stat span { font-size:12px; color:var(--mut); }
  .conv { background:var(--card); border-radius:10px; padding:14px; margin-bottom:10px; }
  .conv .who { font-weight:700; font-size:15px; }
  .conv .who .hi { color:var(--dan); font-size:12px; }
  .conv .msg { color:var(--mut); font-size:13px; margin:6px 0; white-space:pre-wrap; }
  .conv .draft { background:#0f172a; border-radius:6px; padding:8px; font-size:13px; margin:6px 0; }
  .btns { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; align-items:center; }
  button { border:0; border-radius:8px; padding:9px 14px; font-size:13px; cursor:pointer; color:#fff; background:#475569; }
  .b1 { background:var(--acc); } .b3 { background:var(--dan); }
  .ok { color:var(--ok); font-size:13px; } .err { color:var(--dan); font-size:13px; }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  td { padding:4px 6px; border-bottom:1px solid #334155; color:var(--mut); }
  .muted { color:var(--mut); font-size:12px; }
  .panel { background:var(--card); border-radius:10px; padding:14px; margin-bottom:12px; }
  .panel label { display:block; font-size:12px; color:var(--mut); margin:10px 0 4px; }
  .panel input[type=text], .panel input[type=password], .panel textarea {
    width:100%; background:#0f172a; color:var(--txt); border:1px solid #334155; border-radius:8px; padding:8px 10px; font-size:13px; }
  .panel textarea { min-height:56px; resize:vertical; }
  .row2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  @media (max-width:640px){ .row2 { grid-template-columns:1fr; } }
  .tag { display:inline-block; background:#334155; border-radius:6px; padding:2px 8px; font-size:11px; margin:2px; color:var(--mut); }
  details summary { cursor:pointer; font-size:15px; margin:18px 0 8px; color:var(--acc); }
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

<details id="settingsBox"><summary>⚙️ 设置（API Key / 求职偏好 / 简历上传）</summary>
  <div class="panel">
    <h2 style="margin:0 0 4px">🔑 LLM 配置</h2>
    <div class="muted" id="llmMeta">加载中…</div>
    <label>API Key（保存后 DPAPI 加密落盘，绑定本机）</label>
    <input type="password" id="inKey" placeholder="留空=不修改">
    <div class="row2">
      <div><label>Base URL</label><input type="text" id="inBase"></div>
      <div><label>模型</label><input type="text" id="inModel"></div>
    </div>
    <label><input type="checkbox" id="inLLMMatch" style="width:auto"> 启用 LLM 智能匹配（扫描岗位智能打分，关闭则用关键词词表）</label>
    <div class="btns">
      <button class="b1" onclick="saveSettings()">保存</button>
      <button onclick="testLLM()">测试连接</button>
      <span id="resLLM"></span>
    </div>
  </div>
  <div class="panel">
    <h2 style="margin:0 0 4px">🎯 求职偏好（留空 = LLM 基于简历自主决断）</h2>
    <div class="row2">
      <div><label>向往岗位（逗号/换行分隔）</label><textarea id="inWantJobs"></textarea></div>
      <div><label>排斥岗位（命中直接过滤）</label><textarea id="inAvoidJobs"></textarea></div>
    </div>
    <div class="row2">
      <div><label>向往城市（替换扫描城市集）</label><textarea id="inWantCities"></textarea></div>
      <div><label>排斥城市（从扫描集剔除）</label><textarea id="inAvoidCities"></textarea></div>
    </div>
    <div class="btns"><button class="b1" onclick="savePrefs()">保存偏好</button><span id="resPrefs"></span></div>
    <div class="muted" id="effInfo" style="margin-top:8px"></div>
  </div>
  <div class="panel">
    <h2 style="margin:0 0 4px">📄 简历（替换内置候选人画像）</h2>
    <div class="muted" id="profMeta">加载中…</div>
    <label>上传文件（.pdf/.docx/.txt/.md，≤5MB）</label>
    <input type="file" id="inFile" accept=".pdf,.docx,.txt,.md" style="color:var(--mut);font-size:12px;">
    <label>或粘贴简历全文</label>
    <textarea id="inResume" placeholder="粘贴简历文本…" style="min-height:100px"></textarea>
    <div class="btns"><button class="b1" onclick="saveProfile()">保存并提炼画像</button><span id="resProfile"></span></div>
  </div>
</details>

<script>
let TOKEN = new URLSearchParams(location.search).get('token') || '';
async function api(path, opts) {
  const r = await fetch(path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN), opts);
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
        <button onclick="act(${i},'exchange_wechat')">换微信</button>
        <button onclick="act(${i},'send_resume')">发简历</button>
        <button class="b3" onclick="act(${i},'ignore')">忽略</button>
        <span id="res${i}" class="ok"></span>
      </div>
    </div>`).join('');
  window._pending = d.pending;
  document.getElementById('ledger').innerHTML = d.ledger.map(r => `<tr><td>${esc(r.ts)}</td><td>${esc(r.action)}</td><td>${esc(r.company || r.title || '')}</td><td>${esc((r.status || r.reason || r.text_head || '').slice(0, 40))}</td></tr>`).join('');
  loadSettings();
}
async function loadSettings() {
  const s = await api('/api/settings');
  document.getElementById('llmMeta').textContent = `当前Key：${s.llm.api_key_masked || '未配置'}（来源：${s.llm.key_source}）`;
  document.getElementById('inBase').value = s.llm.base_url || '';
  document.getElementById('inModel').value = s.llm.model || '';
  document.getElementById('inLLMMatch').checked = s.llm_match.enabled;
  document.getElementById('inWantJobs').value = (s.prefs.want_jobs || []).join('，');
  document.getElementById('inAvoidJobs').value = (s.prefs.avoid_jobs || []).join('，');
  document.getElementById('inWantCities').value = (s.prefs.want_cities || []).join('，');
  document.getElementById('inAvoidCities').value = (s.prefs.avoid_cities || []).join('，');
  document.getElementById('profMeta').textContent = s.profile.has_resume
    ? `已存简历 ${s.profile.resume_chars} 字（${s.profile.source}，${s.profile.updated_at}）` + (s.profile.has_refined ? ` · 画像已提炼：${s.profile.refined_summary}` : ' · 画像未提炼')
    : '未上传简历（使用内置画像）';
  const eff = s.effective;
  document.getElementById('effInfo').innerHTML = '生效城市：' + (eff.cities.map(esc).join('、') || '（空）')
    + (eff.unknown_cities.length ? ` <span class="err">未识别城市：${eff.unknown_cities.map(esc).join('、')}</span>` : '')
    + '<br>生效关键词：' + (eff.keywords.map(esc).join('、') || '（空）');
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
async function saveSettings() {
  const el = document.getElementById('resLLM');
  const body = { base_url: document.getElementById('inBase').value, model: document.getElementById('inModel').value,
                 llm_match_enabled: document.getElementById('inLLMMatch').checked };
  const k = document.getElementById('inKey').value.trim();
  if (k) body.api_key = k;
  const d = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  el.className = d.ok ? 'ok' : 'err';
  el.textContent = d.ok ? '✅ ' + (d.changed || []).join('；') : '❌ 保存失败';
  if (d.ok) { document.getElementById('inKey').value = ''; loadSettings(); }
}
async function testLLM() {
  const el = document.getElementById('resLLM');
  el.className = 'ok'; el.textContent = '测试中（深度思考约15-60s）…';
  const body = { base_url: document.getElementById('inBase').value, model: document.getElementById('inModel').value };
  const k = document.getElementById('inKey').value.trim();
  if (k) body.api_key = k;
  const d = await api('/api/settings/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  el.className = d.ok ? 'ok' : 'err';
  el.textContent = d.ok ? `✅ 连通（${d.latency_ms}ms）` : ('❌ ' + (d.error || '失败'));
}
async function savePrefs() {
  const el = document.getElementById('resPrefs');
  const body = { want_jobs: document.getElementById('inWantJobs').value, avoid_jobs: document.getElementById('inAvoidJobs').value,
                 want_cities: document.getElementById('inWantCities').value, avoid_cities: document.getElementById('inAvoidCities').value };
  const d = await api('/api/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  el.className = d.ok ? 'ok' : 'err';
  el.textContent = d.ok ? '✅ 已保存（下轮扫描生效）' : '❌ 保存失败';
  if (d.unknown_cities && d.unknown_cities.length) el.textContent += ' ⚠ 未识别城市：' + d.unknown_cities.join('、');
  if (d.ok) loadSettings();
}
async function saveProfile() {
  const el = document.getElementById('resProfile');
  const f = document.getElementById('inFile').files[0];
  try {
    let d;
    if (f) {
      const fd = new FormData(); fd.append('file', f);
      d = await api('/api/profile', { method: 'POST', body: fd });
    } else {
      const text = document.getElementById('inResume').value;
      if (!text.trim()) { el.className = 'err'; el.textContent = '❌ 请选择文件或粘贴文本'; return; }
      d = await api('/api/profile', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text }) });
    }
    if (d.ok && d.refined) { el.className = 'ok'; el.textContent = '✅ 已保存，画像提炼：' + (d.refined_summary || '完成'); }
    else if (d.ok) { el.className = 'err'; el.textContent = '已保存简历但提炼失败：' + (d.refine_error || '未知'); }
    else { el.className = 'err'; el.textContent = '❌ ' + (d.error || '失败'); }
    loadSettings();
  } catch (e) { el.className = 'err'; el.textContent = '❌ ' + e; }
}
load(); setInterval(load, 30000);
</script>
</body>
</html>"""


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

    # 待人工：最近告警中，HR 未再回复（无 reply ok 晚于告警）的会话
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
