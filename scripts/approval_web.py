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
import asyncio
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
    priv = cfg.get("privacy_policy") or {}
    br = cfg.get("browser") or {}
    return {
        "llm": {
            "api_key_masked": secrets_mod.masked(llm.get("api_key") or ""),
            "key_source": _key_source(cfg),
            "base_url": llm.get("base_url") or "",
            "model": llm.get("model") or "",
        },
        "llm_match": {"enabled": (cfg.get("llm_match") or {}).get("enabled", True)},
        "privacy_policy": {
            "exchange_wechat": priv.get("exchange_wechat", "auto"),
            "send_resume": priv.get("send_resume", "auto"),
            "exchange_phone": priv.get("exchange_phone", "manual"),
            "contact_phone": priv.get("contact_phone", ""),
            "contact_wechat": priv.get("contact_wechat", ""),
        },
        "browser": {
            "silent_mode": br.get("silent_mode", True),
            "minimize_on_start": br.get("minimize_on_start", True),
        },
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

    # 隐私与自动化权限更新
    if "privacy_policy" in body and isinstance(body["privacy_policy"], dict):
        pol = body["privacy_policy"]
        clean_pol = {}
        for k in ("exchange_wechat", "send_resume", "exchange_phone"):
            if k in pol:
                v = str(pol[k]).strip()
                if v in ("auto", "high_intent_only", "manual", "disabled"):
                    clean_pol[k] = v
        for k in ("contact_phone", "contact_wechat"):
            if k in pol:
                clean_pol[k] = str(pol[k]).strip()
        if clean_pol:
            _write_local("privacy_policy", clean_pol)
            changed.append("隐私与自动化权限已更新")

    # 浏览器运行模式更新
    if "browser" in body and isinstance(body["browser"], dict):
        b_cfg = body["browser"]
        clean_b = {}
        if "silent_mode" in b_cfg:
            clean_b["silent_mode"] = bool(b_cfg["silent_mode"])
        if "minimize_on_start" in b_cfg:
            clean_b["minimize_on_start"] = bool(b_cfg["minimize_on_start"])
        if clean_b:
            _write_local("browser", clean_b)
            changed.append("浏览器运行设置已更新")

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
    ok, ms, err = await asyncio.to_thread(_llm_ping, base, key, model)
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
<title>BOSS求职守护 · 审批工作台</title>
<style>
  :root {
    --bg: #090d16;
    --card: #111827;
    --card-glass: rgba(17, 24, 39, 0.85);
    --border: rgba(255, 255, 255, 0.08);
    --txt: #f8fafc;
    --mut: #94a3b8;
    --mut-dark: #64748b;
    --acc: #38bdf8;
    --acc-glow: rgba(56, 189, 248, 0.15);
    --ok: #10b981;
    --ok-bg: rgba(16, 185, 129, 0.12);
    --warn: #f59e0b;
    --warn-bg: rgba(245, 158, 11, 0.12);
    --dan: #f43f5e;
    --dan-bg: rgba(244, 63, 94, 0.12);
    --wechat: #07c160;
    --wechat-bg: rgba(7, 193, 96, 0.12);
    --purple: #a855f7;
    --purple-bg: rgba(168, 85, 247, 0.12);
    --radius: 14px;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    background: radial-gradient(circle at 50% 0%, #172554 0%, var(--bg) 40%, var(--bg) 100%);
    color: var(--txt);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
    min-height: 100vh; padding: 20px 16px 60px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 960px; margin: 0 auto; }
  
  /* Navbar */
  .navbar {
    display: flex; justify-content: space-between; align-items: center;
    background: var(--card-glass); backdrop-filter: blur(16px);
    border: 1px solid var(--border); border-radius: var(--radius);
    padding: 14px 20px; margin-bottom: 20px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.3);
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand-icon {
    width: 38px; height: 38px; border-radius: 10px;
    background: linear-gradient(135deg, #0ea5e9, #6366f1);
    display: flex; align-items: center; justify-content: center;
    font-size: 19px; box-shadow: 0 0 16px rgba(14, 165, 233, 0.4);
  }
  .brand-text h1 { font-size: 17px; font-weight: 700; letter-spacing: -0.2px; line-height: 1.2; }
  .brand-text .status-line { font-size: 12px; color: var(--mut); display: flex; align-items: center; gap: 8px; margin-top: 3px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
  .dot-ok { background: var(--ok); box-shadow: 0 0 8px var(--ok); animation: pulse 2s infinite; }
  .dot-err { background: var(--dan); box-shadow: 0 0 8px var(--dan); }
  @keyframes pulse { 0% { opacity: 0.6; } 50% { opacity: 1; transform: scale(1.15); } 100% { opacity: 0.6; } }

  .nav-right { display: flex; align-items: center; gap: 12px; }
  .tabs { display: flex; background: rgba(0,0,0,0.25); border: 1px solid var(--border); border-radius: 10px; padding: 3px; }
  .tab-btn {
    border: 0; background: transparent; color: var(--mut); padding: 7px 14px;
    font-size: 13px; font-weight: 500; border-radius: 8px; cursor: pointer;
    transition: all 0.2s ease; display: flex; align-items: center; gap: 6px;
  }
  .tab-btn:hover { color: var(--txt); }
  .tab-btn.active { background: #1e293b; color: #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.3); }
  .badge {
    background: var(--dan); color: #fff; font-size: 10px; font-weight: 700;
    padding: 1px 6px; border-radius: 10px; line-height: 14px; display: inline-block;
  }
  .btn-refresh {
    background: #1e293b; border: 1px solid var(--border); color: var(--txt);
    padding: 7px 12px; border-radius: 8px; cursor: pointer; font-size: 13px;
    display: flex; align-items: center; gap: 5px; transition: all 0.2s;
  }
  .btn-refresh:hover { background: #334155; border-color: var(--acc); }

  /* KPI Stats */
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 22px; }
  .stat-card {
    background: var(--card-glass); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px; transition: transform 0.2s, border-color 0.2s;
  }
  .stat-card:hover { transform: translateY(-2px); border-color: rgba(255,255,255,0.15); }
  .stat-card .label { font-size: 12px; font-weight: 500; color: var(--mut); display: flex; justify-content: space-between; align-items: center; }
  .stat-card .val { font-size: 26px; font-weight: 700; margin-top: 8px; color: var(--txt); }
  .stat-card .val.c-acc { color: var(--acc); }
  .stat-card .val.c-ok { color: var(--ok); }
  .stat-card .val.c-warn { color: var(--warn); }
  .stat-card .val.c-dan { color: var(--dan); }
  .stat-card .hint { font-size: 11px; color: var(--mut-dark); margin-top: 4px; }

  /* Tab Views */
  .tab-content { display: none; }
  .tab-content.active { display: block; animation: fadeIn 0.25s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  /* Pending Section */
  .section-title { font-size: 16px; font-weight: 600; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
  .empty-box {
    background: var(--card-glass); border: 1px dashed rgba(255,255,255,0.15);
    border-radius: var(--radius); padding: 48px 24px; text-align: center; color: var(--mut);
  }
  .empty-icon { font-size: 40px; margin-bottom: 12px; display: block; }

  .conv-card {
    background: var(--card-glass); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 18px 20px; margin-bottom: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.25);
    transition: border-color 0.2s, box-shadow 0.2s;
  }
  .conv-card:hover { border-color: rgba(255,255,255,0.18); box-shadow: 0 8px 30px rgba(0,0,0,0.35); }
  .conv-card.hi { border-left: 4px solid var(--dan); }
  .conv-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
  .who-wrap { display: flex; align-items: center; gap: 10px; }
  .avatar {
    width: 36px; height: 36px; border-radius: 50%; background: linear-gradient(135deg, #334155, #475569);
    display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 14px; color: #fff;
  }
  .who-info .title { font-size: 15px; font-weight: 600; color: var(--txt); }
  .who-info .sub-t { font-size: 12px; color: var(--mut); margin-top: 2px; }
  .hi-badge {
    background: var(--dan-bg); color: var(--dan); border: 1px solid rgba(244, 63, 94, 0.3);
    font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 6px; display: inline-flex; align-items: center; gap: 4px;
  }

  /* Quotes & Reasons */
  .quote-box {
    background: rgba(15, 23, 42, 0.6); border-left: 3px solid var(--acc);
    border-radius: 0 8px 8px 0; padding: 10px 14px; font-size: 13px; color: #cbd5e1;
    margin-bottom: 10px; line-height: 1.5; white-space: pre-wrap;
  }
  .reason-box {
    background: var(--warn-bg); border: 1px solid rgba(245, 158, 11, 0.25);
    border-radius: 8px; padding: 8px 12px; font-size: 12px; color: #fbbf24;
    margin-bottom: 12px; display: flex; align-items: center; gap: 6px;
  }

  /* Draft Editor */
  .editor-wrap { margin-top: 12px; background: rgba(10, 15, 26, 0.6); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
  .editor-label { font-size: 12px; font-weight: 500; color: var(--mut); margin-bottom: 6px; display: flex; justify-content: space-between; }
  .editor-wrap textarea {
    width: 100%; min-height: 56px; background: transparent; border: 0; outline: 0;
    color: var(--txt); font-size: 13px; line-height: 1.5; resize: vertical;
    font-family: inherit;
  }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.06); }
  .chip {
    background: #1e293b; border: 1px solid var(--border); border-radius: 6px;
    padding: 3px 8px; font-size: 11px; color: var(--mut); cursor: pointer; transition: all 0.15s;
  }
  .chip:hover { background: #334155; color: #fff; border-color: var(--acc); }

  /* Action Buttons */
  .action-bar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
  .btn {
    border: 0; border-radius: 8px; padding: 8px 15px; font-size: 13px; font-weight: 500;
    cursor: pointer; display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s ease;
    text-decoration: none; color: #fff;
  }
  .btn:hover { transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn-primary { background: linear-gradient(135deg, #0284c7, #0369a1); box-shadow: 0 2px 10px rgba(2,132,199,0.3); }
  .btn-primary:hover { background: linear-gradient(135deg, #0369a1, #075985); }
  .btn-wechat { background: linear-gradient(135deg, #059669, #047857); box-shadow: 0 2px 10px rgba(5,150,105,0.25); }
  .btn-wechat:hover { background: linear-gradient(135deg, #047857, #065f46); }
  .btn-resume { background: linear-gradient(135deg, #4f46e5, #4338ca); box-shadow: 0 2px 10px rgba(79,70,229,0.25); }
  .btn-resume:hover { background: linear-gradient(135deg, #4338ca, #3730a3); }
  .btn-purple { background: linear-gradient(135deg, #9333ea, #7e22ce); box-shadow: 0 2px 10px rgba(147,51,234,0.25); }
  .btn-purple:hover { background: linear-gradient(135deg, #7e22ce, #6b21a8); }
  .btn-done { background: linear-gradient(135deg, #0d9488, #0f766e); box-shadow: 0 2px 10px rgba(13,148,136,0.25); }
  .btn-done:hover { background: linear-gradient(135deg, #0f766e, #115e59); }
  .btn-ghost { background: #1e293b; color: var(--mut); border: 1px solid var(--border); }
  .btn-ghost:hover { background: #334155; color: var(--txt); }

  /* Toast & Notification */
  #toastBox {
    position: fixed; top: 24px; right: 24px; z-index: 9999;
    display: flex; flex-direction: column; gap: 8px; pointer-events: none;
  }
  .toast {
    background: #1e293b; border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 18px; color: #fff; font-size: 13px; font-weight: 500;
    box-shadow: 0 10px 30px rgba(0,0,0,0.5); pointer-events: auto;
    animation: toastIn 0.25s cubic-bezier(0.16, 1, 0.3, 1); display: flex; align-items: center; gap: 8px;
  }
  .toast.success { border-color: var(--ok); background: #064e3b; }
  .toast.info { border-color: var(--acc); background: #0c4a6e; }
  .toast.error { border-color: var(--dan); background: #881337; }
  @keyframes toastIn { from { opacity: 0; transform: translateX(30px); } to { opacity: 1; transform: translateX(0); } }

  /* Ledger Stream */
  .ledger-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; gap: 10px; flex-wrap: wrap; }
  .search-input {
    background: #0f172a; border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 12px; color: var(--txt); font-size: 13px; width: 260px; outline: 0;
  }
  .search-input:focus { border-color: var(--acc); }
  .filter-pills { display: flex; gap: 6px; flex-wrap: wrap; }
  .filter-pill {
    background: #1e293b; border: 1px solid var(--border); border-radius: 6px;
    padding: 5px 11px; font-size: 12px; color: var(--mut); cursor: pointer; transition: all 0.15s;
  }
  .filter-pill:hover { background: #334155; color: #fff; }
  .filter-pill.active { background: var(--acc); color: #000; font-weight: 600; border-color: var(--acc); }

  .ledger-table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--card-glass); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .ledger-table th { background: #131d2e; text-align: left; padding: 11px 14px; font-size: 12px; color: var(--mut); font-weight: 600; border-bottom: 1px solid var(--border); }
  .ledger-table td { padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.04); color: var(--mut); }
  .ledger-table tr:hover td { background: rgba(255,255,255,0.02); color: var(--txt); }
  .tag-act { display: inline-block; padding: 2px 7px; border-radius: 5px; font-size: 11px; font-weight: 600; }
  .tag-reply { background: var(--acc-glow); color: var(--acc); }
  .tag-wechat { background: var(--wechat-bg); color: var(--wechat); }
  .tag-resume { background: var(--purple-bg); color: var(--purple); }
  .tag-gate { background: var(--warn-bg); color: var(--warn); }
  .tag-alert { background: var(--dan-bg); color: var(--dan); }
  .tag-other { background: #1e293b; color: var(--mut); }

  /* Settings Panel */
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 768px) { .grid-2 { grid-template-columns: 1fr; } .navbar { flex-direction: column; gap: 12px; align-items: stretch; } .nav-right { justify-content: space-between; } }
  .panel { background: var(--card-glass); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px; margin-bottom: 16px; }
  .panel h2 { font-size: 15px; font-weight: 600; margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
  .panel label { display: block; font-size: 12px; font-weight: 500; color: var(--mut); margin: 12px 0 5px; }
  .panel input[type=text], .panel input[type=password], .panel select, .panel textarea {
    width: 100%; background: #0b1120; color: var(--txt); border: 1px solid var(--border);
    border-radius: 8px; padding: 9px 12px; font-size: 13px; font-family: inherit; outline: 0; transition: border-color 0.2s;
  }
  .panel input[type=text]:focus, .panel input[type=password]:focus, .panel select:focus, .panel textarea:focus { border-color: var(--acc); }
  .panel textarea { min-height: 70px; resize: vertical; }

  /* Spinner */
  .spinner {
    width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%;
    border-top-color: #fff; animation: spin 0.6s linear infinite; display: inline-block; vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="wrap">
  <!-- Top Navbar -->
  <header class="navbar">
    <div class="brand">
      <div class="brand-icon">⚡</div>
      <div class="brand-text">
        <h1>BOSS求职守护 · 审批台</h1>
        <div class="status-line">
          <span id="guardPill" class="status-pill ok"><span class="dot dot-ok"></span> ● 守护运行中</span>
          <span>·</span>
          <span id="sub">正在同步…</span>
        </div>
      </div>
    </div>
    <div class="nav-right">
      <nav class="tabs">
        <button class="tab-btn active" data-tab="pending" onclick="switchTab('pending')">
          📋 待办审批 <span id="pendingBadge" class="badge" style="display:none">0</span>
        </button>
        <button class="tab-btn" data-tab="ledger" onclick="switchTab('ledger')">
          📜 实时台账
        </button>
        <button class="tab-btn" data-tab="settings" onclick="switchTab('settings')">
          ⚙️ 系统设置
        </button>
      </nav>
      <button class="btn-refresh" onclick="load(true)">🔄 刷新</button>
    </div>
  </header>

  <!-- KPI Top Cards -->
  <section class="stats" id="stats">
    <div class="stat-card">
      <div class="label"><span>待处理会话</span><span>📋</span></div>
      <div class="val c-dan" id="statPending">0</div>
      <div class="hint">需人工审核干预</div>
    </div>
    <div class="stat-card">
      <div class="label"><span>今日实发回复</span><span>💬</span></div>
      <div class="val c-ok" id="statTodayReplied">0</div>
      <div class="hint">拟人高斯发出</div>
    </div>
    <div class="stat-card">
      <div class="label"><span>今日巡检扫描</span><span>🔍</span></div>
      <div class="val c-acc" id="statTodayScanned">0</div>
      <div class="hint">消息中心雷达</div>
    </div>
    <div class="stat-card">
      <div class="label"><span>高意向猎聘</span><span>🔥</span></div>
      <div class="val c-warn" id="statHighIntent">0</div>
      <div class="hint">面试/Offer信号</div>
    </div>
    <div class="stat-card">
      <div class="label"><span>累计安全回复</span><span>🛡️</span></div>
      <div class="val" id="statRepliedTotal">0</div>
      <div class="hint">零封号留痕</div>
    </div>
  </section>

  <!-- Tab 1: 待办审批 -->
  <main id="tab-pending" class="tab-content active">
    <div class="section-title">📋 待人工决策（needs_human）</div>
    <div id="pending">加载中…</div>
    
    <div style="margin-top:24px">
      <details style="cursor:pointer;color:var(--mut);font-size:13px" id="resolvedBox">
        <summary style="padding:8px 0;font-weight:500">📁 查看近期已处理会话记录 (Recently Handled)</summary>
        <div id="resolvedList" style="margin-top:8px;background:var(--card-glass);border:1px solid var(--border);border-radius:10px;padding:12px 16px"></div>
      </details>
    </div>
  </main>

  <!-- Tab 2: 实时台账 -->
  <main id="tab-ledger" class="tab-content">
    <div class="ledger-bar">
      <div class="filter-pills">
        <span class="filter-pill active" data-filter="all" onclick="filterLedgerChip('all')">全部流水</span>
        <span class="filter-pill" data-filter="reply" onclick="filterLedgerChip('reply')">智能回复</span>
        <span class="filter-pill" data-filter="wechat" onclick="filterLedgerChip('wechat')">交换微信</span>
        <span class="filter-pill" data-filter="resume" onclick="filterLedgerChip('resume')">发送简历</span>
        <span class="filter-pill" data-filter="alert" onclick="filterLedgerChip('alert')">转人工告警</span>
        <span class="filter-pill" data-filter="gate" onclick="filterLedgerChip('gate')">门禁拦截</span>
      </div>
      <input type="text" id="ledgerFilter" class="search-input" placeholder="🔍 快速搜索公司或状态…" oninput="renderLedger()">
    </div>
    <div style="overflow-x:auto">
      <table class="ledger-table" id="ledger">
        <thead>
          <tr>
            <th style="width:140px">时间</th>
            <th style="width:110px">动作</th>
            <th style="width:200px">目标 / 会话</th>
            <th>状态 / 归因 / 回复摘要</th>
          </tr>
        </thead>
        <tbody id="ledgerBody"></tbody>
      </table>
    </div>
  </main>

  <!-- Tab 3: 系统设置 -->
  <main id="tab-settings" class="tab-content">
    <div id="settingsBox">
      <div class="grid-2">
        <div>
          <!-- LLM Card -->
          <div class="panel">
            <h2>🔑 大模型 LLM 配置</h2>
            <div style="font-size:12px;color:var(--mut);margin-bottom:10px" id="llmMeta">加载中…</div>
            <label>API Key（DPAPI 本机加密落盘，安全脱敏）</label>
            <input type="password" id="inKey" placeholder="留空 = 保持当前密钥不修改">
            <label>Base URL 端点</label>
            <input type="text" id="inBase">
            <label>模型名称</label>
            <input type="text" id="inModel">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:14px">
              <input type="checkbox" id="inLLMMatch" style="width:auto"> 启用 LLM 智能匹配（岗位打分，关闭则回退关键词词表）
            </label>
            <div class="action-bar" style="margin-top:16px">
              <button class="btn btn-primary" onclick="saveSettings()">保存配置</button>
              <button class="btn btn-ghost" onclick="testLLM()">测试连接</button>
              <span id="resLLM" style="font-size:12px;margin-left:8px"></span>
            </div>
          </div>

          <!-- Prefs Card -->
          <div class="panel">
            <h2>🎯 求职偏好（留空 = 大模型基于简历自主决断）</h2>
            <label>向往岗位（逗号/换行分隔，高亮优先沟通）</label>
            <textarea id="inWantJobs"></textarea>
            <label>排斥岗位（命中黑名单直接过滤，不耗 Token）</label>
            <textarea id="inAvoidJobs"></textarea>
            <div class="grid-2" style="margin-top:4px">
              <div><label>向往城市</label><textarea id="inWantCities"></textarea></div>
              <div><label>排斥城市</label><textarea id="inAvoidCities"></textarea></div>
            </div>
            <div class="action-bar" style="margin-top:16px">
              <button class="btn btn-primary" onclick="savePrefs()">保存偏好</button>
              <span id="resPrefs" style="font-size:12px;margin-left:8px"></span>
            </div>
            <div id="effInfo" style="font-size:12px;color:var(--mut);margin-top:12px;line-height:1.6"></div>
          </div>
        </div>

        <div>
          <!-- Profile Card -->
          <div class="panel">
            <h2>📄 简历与画像中心</h2>
            <div style="font-size:12px;color:var(--mut);margin-bottom:12px" id="profMeta">加载中…</div>
            <label>上传简历文件（支持 .pdf / .docx / .txt / .md，≤5MB）</label>
            <input type="file" id="inFile" accept=".pdf,.docx,.txt,.md" style="color:var(--mut);font-size:12px">
            <label style="margin-top:14px">或直接粘贴简历文本</label>
            <textarea id="inResume" placeholder="在此粘贴简历正文文本…" style="min-height:160px"></textarea>
            <div class="action-bar" style="margin-top:16px">
              <button class="btn btn-primary" onclick="saveProfile()">保存并由 AI 提炼画像</button>
              <span id="resProfile" style="font-size:12px;margin-left:8px"></span>
            </div>
          </div>

          <!-- Privacy & Automation Card -->
          <div class="panel">
            <h2>🛡️ 隐私保护与自动化权限</h2>
            <div style="font-size:12px;color:var(--mut);margin-bottom:12px;line-height:1.5">
              自主决定是否允许系统全自动执行敏感物理动作；内置 Prompt 防套话铁律与出信前物理正则拦截门禁，防范诱导套取联系方式。
            </div>
            <div class="grid-2" style="margin-top:8px">
              <div>
                <label>换微信权限</label>
                <select id="inPolicyWechat">
                  <option value="auto">全自动 (auto)</option>
                  <option value="high_intent_only">仅高意向自动 (high_intent)</option>
                  <option value="manual">必须人工审批 (manual)</option>
                  <option value="disabled">禁用该动作 (disabled)</option>
                </select>
              </div>
              <div>
                <label>发简历权限</label>
                <select id="inPolicyResume">
                  <option value="auto">全自动 (auto)</option>
                  <option value="high_intent_only">仅高意向自动 (high_intent)</option>
                  <option value="manual">必须人工审批 (manual)</option>
                  <option value="disabled">禁用该动作 (disabled)</option>
                </select>
              </div>
            </div>
            <div style="margin-top:8px">
              <label>换电话权限</label>
              <select id="inPolicyPhone">
                <option value="manual">必须人工审批 (manual，推荐)</option>
                <option value="high_intent_only">仅高意向自动 (high_intent)</option>
                <option value="auto">全自动 (auto)</option>
                <option value="disabled">禁用该动作 (disabled)</option>
              </select>
            </div>
            <div class="grid-2" style="margin-top:8px">
              <div>
                <label>个人真实手机号（配置后防泄密物理锁死）</label>
                <input type="text" id="inContactPhone" placeholder="例如：13800000000">
              </div>
              <div>
                <label>个人真实微信号（配置后防泄密物理锁死）</label>
                <input type="text" id="inContactWechat" placeholder="例如：wxid_xxxx">
              </div>
            </div>
            <div style="background:rgba(244,63,94,0.1);border:1px solid rgba(244,63,94,0.25);border-radius:8px;padding:10px 12px;font-size:12px;color:#fda4af;margin-top:12px;line-height:1.5">
              🔒 <strong>防套话安全铁律</strong>：模型严禁在文本中吐出明文联系方式；若 HR 催促或诱导索要电话微信，系统仅允许引导官方交换。若模型被攻破输出明文信息，底层正则门禁将物理拦截并立即转人工告警。
            </div>
            <div class="action-bar" style="margin-top:16px">
              <button class="btn btn-primary" onclick="savePrivacyPolicy()">保存隐私权限设置</button>
              <span id="resPrivacy" style="font-size:12px;margin-left:8px"></span>
            </div>
          </div>

          <!-- Browser Mode Card -->
          <div class="panel">
            <h2>🖥️ 浏览器后台运行设置</h2>
            <div style="font-size:12px;color:var(--mut);margin-bottom:12px">
              解决 BOSS 轮询时 Chrome 窗口时不时弹窗、置顶、抢占桌面输入焦点的问题。
            </div>
            <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;margin-top:10px;line-height:1.4">
              <input type="checkbox" id="inBrowserSilent" style="width:auto;margin-top:3px">
              <div>
                <strong style="color:var(--txt)">静默后台巡检模式 (Silent Background Mode)</strong>
                <div style="font-size:12px;color:var(--mut)">开启后通过 CDP 隐藏标签页执行页面操作，绝不抢占前台键盘输入焦点与激活置顶。</div>
              </div>
            </label>
            <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;margin-top:14px;line-height:1.4">
              <input type="checkbox" id="inBrowserMinimize" style="width:auto;margin-top:3px">
              <div>
                <strong style="color:var(--txt)">启动时窗口最小化 (Minimize On Start)</strong>
                <div style="font-size:12px;color:var(--mut)">启动脚本拉起 Chrome 时自动以最小化启动，避免巨大浏览器窗口覆盖主屏幕。</div>
              </div>
            </label>
            <div class="action-bar" style="margin-top:16px">
              <button class="btn btn-primary" onclick="saveBrowserSettings()">保存浏览器设置</button>
              <span id="resBrowser" style="font-size:12px;margin-left:8px"></span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </main>
</div>

<!-- Floating Toast Container -->
<div id="toastBox"></div>

<script>
let TOKEN = new URLSearchParams(location.search).get('token') || '';
let currentTab = 'pending';
let fullLedger = [];
let pendingData = [];
let resolvedData = [];
let ledgerFilter = 'all';

async function api(path, opts) {
  const r = await fetch(path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN), opts);
  if (r.status === 401) {
    document.body.innerHTML = '<div style="max-width:400px;margin:100px auto;text-align:center;color:#f87171;font-family:sans-serif;"><h1>401 身份未认证</h1><p style="margin-top:10px;color:#94a3b8;">Token 无效或已过期，请检查 config.web.token 或在 URL 末尾附加 ?token=你的token</p></div>';
    throw new Error('401');
  }
  return r.json();
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function showToast(msg, type = 'info') {
  const box = document.getElementById('toastBox');
  if (!box) return;
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  const icon = type === 'success' ? '✅' : (type === 'error' ? '❌' : 'ℹ️');
  t.innerHTML = `<span>${icon}</span><span>${esc(msg)}</span>`;
  box.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transform = 'translateX(20px)';
    t.style.transition = 'all 0.3s ease';
    setTimeout(() => t.remove(), 300);
  }, 3200);
}

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === 'tab-' + name);
  });
}

function addChip(i, text) {
  const box = document.getElementById('replyText' + i);
  if (!box) return;
  const cur = box.value.trim();
  if (!cur) {
    box.value = text;
  } else if (!cur.includes(text)) {
    box.value = cur + ' ' + text;
  }
  const countEl = document.getElementById('charCount' + i);
  if (countEl) countEl.textContent = '已输入 ' + box.value.length + ' 字';
  box.focus();
}

function clearDraft(i) {
  const box = document.getElementById('replyText' + i);
  if (box) {
    box.value = '';
    const countEl = document.getElementById('charCount' + i);
    if (countEl) countEl.textContent = '已输入 0 字';
    box.focus();
  }
}

async function load(isManual) {
  if (isManual) showToast('正在刷新工作台数据…', 'info');
  try {
    const d = await api('/api/overview');
    document.getElementById('sub').textContent = '已同步: ' + new Date().toLocaleTimeString();

    // 护栏状态
    const gp = document.getElementById('guardPill');
    if (d.guard.paused) {
      gp.innerHTML = '<span class="dot dot-err"></span> ⛔ 风控熔断: ' + esc(d.guard.paused);
    } else {
      gp.innerHTML = '<span class="dot dot-ok"></span> ● 守护运行中 (正常)';
    }

    // KPI 统计
    document.getElementById('statPending').textContent = d.counts.pending || 0;
    const badge = document.getElementById('pendingBadge');
    badge.textContent = d.counts.pending || 0;
    badge.style.display = d.counts.pending > 0 ? 'inline-block' : 'none';
    document.getElementById('statRepliedTotal').textContent = d.counts.replied_total || 0;
    document.getElementById('statTodayReplied').textContent = d.today.replied || 0;
    document.getElementById('statTodayScanned').textContent = d.today.scanned || 0;
    document.getElementById('statHighIntent').textContent = d.counts.high_intent || 0;

    pendingData = d.pending || [];
    resolvedData = d.resolved || [];
    window._pending = pendingData;
    fullLedger = d.ledger || [];

    renderPending();
    renderLedger();
    loadSettings();
    if (isManual) showToast('工作台数据已更新！', 'success');
  } catch(e) {
    console.error(e);
  }
}

function renderPending() {
  const p = document.getElementById('pending');
  if (!pendingData.length) {
    p.innerHTML = `
      <div class="empty-box">
        <span class="empty-icon">🎉</span>
        <h3 style="font-size:16px;color:#f8fafc;margin-bottom:6px">当前无待人工处理会话</h3>
        <p style="font-size:13px;max-width:440px;margin:0 auto;line-height:1.6">求职守护智能引擎正在后台持续巡检，当遇到电话隐私红线、高意向邀约或决策边界时将自动呈现在此。</p>
      </div>`;
  } else {
    p.innerHTML = pendingData.map((c, i) => `
      <div class="conv-card ${c.high_intent ? 'hi' : ''}">
        <div class="conv-head">
          <div class="who-wrap">
            <div class="avatar">${esc((c.company || 'H').slice(0, 1))}</div>
            <div class="who-info">
              <div class="title">${esc(c.company)}</div>
              <div class="sub-t">最后活跃：${esc(c.time || '刚刚')}</div>
            </div>
          </div>
          <div>
            ${c.high_intent ? '<span class="hi-badge">🔥 高意向邀约</span>' : ''}
          </div>
        </div>

        <div class="quote-box">
          <div style="font-size:11px;color:var(--mut);margin-bottom:4px">💬 HR 最新消息</div>
          <div>${c.last_msg ? esc(c.last_msg) : '<span style="color:var(--mut-dark)">（系统隐私拦截/对方发送联系方式，大模型已被门禁阻断，由人工接管）</span>'}</div>
        </div>

        <div class="reason-box">
          <span>🛡️ 拦截原因：</span><span>${esc(c.reason || '大模型触发安全策略')}</span>
        </div>

        <div class="editor-wrap">
          <div class="editor-label">
            <span>✍️ 回复文案（可直接在此编辑，修改后一键发送）</span>
            <span id="charCount${i}" style="color:var(--mut-dark);font-size:11px">已输入 ${(c.suggested || '').length} 字</span>
          </div>
          <textarea id="replyText${i}" oninput="document.getElementById('charCount${i}').textContent = '已输入 ' + this.value.length + ' 字'" placeholder="输入自定义回复文案…">${esc(c.suggested || '')}</textarea>
          <div class="chips">
            <span style="font-size:11px;color:var(--mut-dark);margin-right:4px;display:flex;align-items:center">快捷补齐:</span>
            <span class="chip" onclick="addChip(${i},'方便加微信详细沟通吗？')">+ 加微信</span>
            <span class="chip" onclick="addChip(${i},'稍后为您发送简历！')">+ 发简历</span>
            <span class="chip" onclick="addChip(${i},'可随时配合线上初试')">+ 约初试</span>
            <span class="chip" onclick="clearDraft(${i})">清空文案</span>
          </div>
        </div>

        <div class="action-bar">
          <button class="btn btn-primary" id="btnReply${i}" onclick="actReply(${i})">
            <span>✈️ 发送回复</span>
          </button>
          <button class="btn btn-wechat" id="btnWx${i}" onclick="actWithText(${i},'exchange_wechat')">
            <span>💬 换微信</span>
          </button>
          <button class="btn btn-resume" id="btnCv${i}" onclick="actWithText(${i},'send_resume')">
            <span>📄 发简历</span>
          </button>
          <button class="btn btn-purple" id="btnAgree${i}" onclick="actWithText(${i},'agree_wechat')">
            <span>🤝 同意换微信</span>
          </button>
          <button class="btn btn-done" id="btnDone${i}" onclick="act(${i},'mark_handled')" title="已在手机微信或BOSS端手动处理，直接标记为已完成消单">
            <span>✅ 标记已处理</span>
          </button>
          <button class="btn btn-ghost" onclick="act(${i},'ignore')">
            <span>✕ 忽略</span>
          </button>
          <div id="res${i}" style="margin-left:auto;font-size:12px;font-weight:500"></div>
        </div>
      </div>
    `).join('');
  }

  // 渲染近期已处理会话
  const rb = document.getElementById('resolvedList');
  if (rb) {
    if (!resolvedData.length) {
      rb.innerHTML = '<div style="color:var(--mut-dark);font-size:12px;padding:8px 0">暂无近期处理记录</div>';
    } else {
      rb.innerHTML = resolvedData.map(r => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:12px">
          <div><strong style="color:var(--txt)">${esc(r.company)}</strong> <span style="color:var(--mut)">(${esc(r.resolved_action || 'handled')})</span></div>
          <div style="color:var(--mut-dark)">${esc(r.resolved_time || '')}</div>
        </div>
      `).join('');
    }
  }
}

async function actReply(i) {
  const c = window._pending[i];
  const el = document.getElementById('res' + i);
  const btn = document.getElementById('btnReply' + i);
  const box = document.getElementById('replyText' + i);
  const text = (box ? box.value : (c.suggested || '')).trim();
  if (!text) {
    showToast('回复文案不能为空', 'error');
    if (el) { el.style.color = 'var(--dan)'; el.textContent = '❌ 回复内容不能为空'; }
    return;
  }
  if (btn) btn.innerHTML = '<span class="spinner"></span> 发送中…';
  if (el) { el.style.color = 'var(--acc)'; el.textContent = '正在通过 CDP 发送…'; }
  const body = { action: 'reply', company: c.company, text: text };
  try {
    const d = await api('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (d.ok) {
      showToast('回复消息已送达 ' + c.company + '！', 'success');
      if (el) { el.style.color = 'var(--ok)'; el.textContent = '✅ 已成功发送'; }
    } else {
      showToast('发送失败: ' + (d.error || '未知错误'), 'error');
      if (el) { el.style.color = 'var(--dan)'; el.textContent = '❌ ' + (d.error || '失败'); }
    }
  } catch(e) {
    showToast('网络或执行异常: ' + e, 'error');
  } finally {
    if (btn) btn.innerHTML = '<span>✈️ 发送回复</span>';
    setTimeout(() => load(false), 1200);
  }
}

async function actWithText(i, action) {
  const c = window._pending[i];
  const el = document.getElementById('res' + i);
  const box = document.getElementById('replyText' + i);
  const text = box ? box.value.trim() : '';
  const actNames = { exchange_wechat: '换微信', send_resume: '发简历', agree_wechat: '同意换微信' };
  const label = actNames[action] || action;
  
  if (el) { el.style.color = 'var(--acc)'; el.textContent = '正在执行【' + label + '】…'; }
  const body = { action: action, company: c.company };
  if (text) body.text = text;

  try {
    const d = await api('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const st = d.status || (d.result && d.result.status);
    if (d.ok) {
      if (st === 'already_sent' || st === 'already_agreed') {
        showToast('之前已向对方发起过' + label + '，无需重复发送（已标记完成）', 'info');
        if (el) { el.style.color = 'var(--acc)'; el.textContent = 'ℹ️ 之前已发起过，已为您标记完成'; }
      } else {
        showToast('【' + label + '】动作已在 BOSS 成功执行！', 'success');
        if (el) { el.style.color = 'var(--ok)'; el.textContent = '✅ 已执行成功'; }
      }
    } else {
      showToast('执行失败: ' + (d.error || '未知错误'), 'error');
      if (el) { el.style.color = 'var(--dan)'; el.textContent = '❌ ' + (d.error || '失败'); }
    }
  } catch(e) {
    showToast('网络或执行异常: ' + e, 'error');
  } finally {
    setTimeout(() => load(false), 1200);
  }
}

async function act(i, action) {
  const c = window._pending[i];
  const el = document.getElementById('res' + i);
  if (el) { el.style.color = 'var(--acc)'; el.textContent = '执行中…'; }
  const body = { action: action, company: c.company };
  if (action === 'reply') {
    const box = document.getElementById('replyText' + i);
    body.text = (box ? box.value : (c.suggested || '')).trim();
  }
  try {
    const d = await api('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (d.ok) {
      if (action === 'mark_handled') {
        showToast('已将该会话标记为已处理！', 'success');
        if (el) { el.style.color = 'var(--ok)'; el.textContent = '✅ 已标记完成'; }
      } else {
        showToast('已完成忽略操作', 'info');
        if (el) { el.style.color = 'var(--ok)'; el.textContent = '✅ 已忽略'; }
      }
    } else {
      showToast('操作失败: ' + (d.error || '未知'), 'error');
      if (el) { el.style.color = 'var(--dan)'; el.textContent = '❌ ' + (d.error || '失败'); }
    }
  } catch(e) {
    showToast('异常: ' + e, 'error');
  } finally {
    setTimeout(() => load(false), 1000);
  }
}

function filterLedgerChip(filterVal) {
  ledgerFilter = filterVal;
  document.querySelectorAll('.filter-pill').forEach(p => {
    p.classList.toggle('active', p.dataset.filter === filterVal);
  });
  renderLedger();
}

function renderLedger() {
  const tbody = document.getElementById('ledgerBody');
  if (!tbody) return;
  const kw = (document.getElementById('ledgerFilter') ? document.getElementById('ledgerFilter').value : '').trim().toLowerCase();
  
  const filtered = fullLedger.filter(r => {
    if (ledgerFilter !== 'all') {
      if (ledgerFilter === 'wechat' && !r.action.includes('wechat')) return false;
      else if (ledgerFilter === 'resume' && !r.action.includes('resume')) return false;
      else if (ledgerFilter === 'reply' && r.action !== 'reply') return false;
      else if (ledgerFilter === 'alert' && !r.action.includes('alert')) return false;
      else if (ledgerFilter === 'gate' && !r.action.includes('gate')) return false;
    }
    if (kw) {
      const line = (r.ts + ' ' + r.action + ' ' + r.company + ' ' + r.status).toLowerCase();
      if (!line.includes(kw)) return false;
    }
    return true;
  });

  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--mut-dark)">无匹配台账流水</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.slice(0, 40).map(r => {
    let tagCls = 'tag-other';
    let actName = r.action;
    if (r.action === 'reply') { tagCls = 'tag-reply'; actName = '智能回复'; }
    else if (r.action.includes('wechat')) { tagCls = 'tag-wechat'; actName = '交换微信'; }
    else if (r.action.includes('resume')) { tagCls = 'tag-resume'; actName = '发送简历'; }
    else if (r.action.includes('alert')) { tagCls = 'tag-alert'; actName = '转人工告警'; }
    else if (r.action.includes('gate')) { tagCls = 'tag-gate'; actName = '时间/门禁拦截'; }

    let stColor = 'var(--mut)';
    let st = r.status || '';
    if (st === 'ok') stColor = 'var(--ok)';
    else if (st === 'already_sent' || st === 'already_agreed') { stColor = 'var(--acc)'; st = '已发起(无需重发)'; }
    else if (st.includes('blocked') || st.includes('fail')) stColor = 'var(--dan)';

    return `
      <tr>
        <td style="white-space:nowrap;font-size:12px;color:var(--mut-dark)">${esc(r.ts)}</td>
        <td><span class="tag-act ${tagCls}">${esc(actName)}</span></td>
        <td style="font-weight:500;color:var(--txt)">${esc(r.company || '-')}</td>
        <td style="color:${stColor}">${esc(st.slice(0, 50))}</td>
      </tr>
    `;
  }).join('');
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

  const priv = s.privacy_policy || {};
  if (document.getElementById('inPolicyWechat')) document.getElementById('inPolicyWechat').value = priv.exchange_wechat || 'auto';
  if (document.getElementById('inPolicyResume')) document.getElementById('inPolicyResume').value = priv.send_resume || 'auto';
  if (document.getElementById('inPolicyPhone')) document.getElementById('inPolicyPhone').value = priv.exchange_phone || 'manual';
  if (document.getElementById('inContactPhone')) document.getElementById('inContactPhone').value = priv.contact_phone || '';
  if (document.getElementById('inContactWechat')) document.getElementById('inContactWechat').value = priv.contact_wechat || '';

  const br = s.browser || {};
  if (document.getElementById('inBrowserSilent')) document.getElementById('inBrowserSilent').checked = br.silent_mode !== false;
  if (document.getElementById('inBrowserMinimize')) document.getElementById('inBrowserMinimize').checked = br.minimize_on_start !== false;

  document.getElementById('profMeta').textContent = s.profile.has_resume
    ? `已存简历 ${s.profile.resume_chars} 字（${s.profile.source}，${s.profile.updated_at}）` + (s.profile.has_refined ? ` · 画像已提炼：${s.profile.refined_summary}` : ' · 画像未提炼')
    : '未上传简历（使用内置画像）';
  const eff = s.effective;
  document.getElementById('effInfo').innerHTML = '生效城市：' + (eff.cities.map(esc).join('、') || '（空）')
    + (eff.unknown_cities.length ? ` <span class="err" style="color:var(--dan)">未识别城市：${eff.unknown_cities.map(esc).join('、')}</span>` : '')
    + '<br>生效关键词：' + (eff.keywords.map(esc).join('、') || '（空）');
}

async function saveSettings() {
  const el = document.getElementById('resLLM');
  const body = { base_url: document.getElementById('inBase').value, model: document.getElementById('inModel').value,
                 llm_match_enabled: document.getElementById('inLLMMatch').checked };
  const k = document.getElementById('inKey').value.trim();
  if (k) body.api_key = k;
  const d = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (el) {
    el.style.color = d.ok ? 'var(--ok)' : 'var(--dan)';
    el.textContent = d.ok ? '✅ ' + (d.changed || []).join('；') : '❌ 保存失败';
  }
  if (d.ok) {
    showToast('LLM 配置保存成功！', 'success');
    document.getElementById('inKey').value = '';
    loadSettings();
  }
}

async function testLLM() {
  const el = document.getElementById('resLLM');
  if (el) { el.style.color = 'var(--acc)'; el.textContent = '测试中（深度思考约15-60s）…'; }
  const body = { base_url: document.getElementById('inBase').value, model: document.getElementById('inModel').value };
  const k = document.getElementById('inKey').value.trim();
  if (k) body.api_key = k;
  const d = await api('/api/settings/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (el) {
    el.style.color = d.ok ? 'var(--ok)' : 'var(--dan)';
    el.textContent = d.ok ? `✅ 连通（${d.latency_ms}ms）` : ('❌ ' + (d.error || '失败'));
  }
  if (d.ok) showToast(`大模型连通测试成功 (${d.latency_ms}ms)`, 'success');
  else showToast('连接失败: ' + (d.error || ''), 'error');
}

async function savePrefs() {
  const el = document.getElementById('resPrefs');
  const body = { want_jobs: document.getElementById('inWantJobs').value, avoid_jobs: document.getElementById('inAvoidJobs').value,
                 want_cities: document.getElementById('inWantCities').value, avoid_cities: document.getElementById('inAvoidCities').value };
  const d = await api('/api/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (el) {
    el.style.color = d.ok ? 'var(--ok)' : 'var(--dan)';
    el.textContent = d.ok ? '✅ 已保存（下轮扫描生效）' : '❌ 保存失败';
    if (d.unknown_cities && d.unknown_cities.length) el.textContent += ' ⚠ 未识别城市：' + d.unknown_cities.join('、');
  }
  if (d.ok) { showToast('求职偏好已保存！', 'success'); loadSettings(); }
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
      if (!text.trim()) {
        if (el) { el.style.color = 'var(--dan)'; el.textContent = '❌ 请选择文件或粘贴文本'; }
        showToast('请选择文件或粘贴文本', 'error');
        return;
      }
      d = await api('/api/profile', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text }) });
    }
    if (d.ok && d.refined) {
      if (el) { el.style.color = 'var(--ok)'; el.textContent = '✅ 已保存，画像提炼：' + (d.refined_summary || '完成'); }
      showToast('简历保存并提炼画像成功！', 'success');
    } else if (d.ok) {
      if (el) { el.style.color = 'var(--warn)'; el.textContent = '已保存简历但提炼失败：' + (d.refine_error || '未知'); }
      showToast('简历已存入但提炼失败', 'info');
    } else {
      if (el) { el.style.color = 'var(--dan)'; el.textContent = '❌ ' + (d.error || '失败'); }
      showToast('保存简历失败: ' + (d.error || ''), 'error');
    }
    loadSettings();
  } catch (e) {
    if (el) { el.style.color = 'var(--dan)'; el.textContent = '❌ ' + e; }
    showToast('保存异常: ' + e, 'error');
  }
}

async function savePrivacyPolicy() {
  const el = document.getElementById('resPrivacy');
  const body = {
    privacy_policy: {
      exchange_wechat: document.getElementById('inPolicyWechat').value,
      send_resume: document.getElementById('inPolicyResume').value,
      exchange_phone: document.getElementById('inPolicyPhone').value,
      contact_phone: document.getElementById('inContactPhone').value.trim(),
      contact_wechat: document.getElementById('inContactWechat').value.trim(),
    }
  };
  const d = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (el) {
    el.style.color = d.ok ? 'var(--ok)' : 'var(--dan)';
    el.textContent = d.ok ? '✅ ' + (d.changed || []).join('；') : '❌ 保存失败';
  }
  if (d.ok) {
    showToast('隐私与自动化权限设置已保存！', 'success');
    loadSettings();
  }
}

async function saveBrowserSettings() {
  const el = document.getElementById('resBrowser');
  const body = {
    browser: {
      silent_mode: document.getElementById('inBrowserSilent').checked,
      minimize_on_start: document.getElementById('inBrowserMinimize').checked,
    }
  };
  const d = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (el) {
    el.style.color = d.ok ? 'var(--ok)' : 'var(--dan)';
    el.textContent = d.ok ? '✅ ' + (d.changed || []).join('；') : '❌ 保存失败';
  }
  if (d.ok) {
    showToast('浏览器运行设置已保存！', 'success');
    loadSettings();
  }
}

load(false);
setInterval(() => load(false), 30000);
</script>
</body>
</html>"""


def _is_alert_resolved(alert_ts, company, rows):
    """判断该告警是否已被后续动作处理过（回复、换微信、发简历、同意换微信、忽略或标记完成）。"""
    valid_actions = {
        "reply": {"ok"},
        "exchange_wechat": {"ok", "already_sent"},
        "send_resume": {"ok", "already_sent"},
        "agree_wechat": {"ok", "already_agreed"},
        "card_ignore": None,
        "ignore": None,
        "mark_handled": None,
        "daemon_skip": None,
    }
    company_clean = (company or "").replace(" ", "").lower()
    for r in rows:
        row_comp = (r.get("company") or "").replace(" ", "").lower()
        if not row_comp or not company_clean:
            continue
        matched = (row_comp == company_clean or 
                   (len(company_clean) >= 2 and company_clean in row_comp) or 
                   (len(row_comp) >= 2 and row_comp in company_clean))
        if not matched:
            continue
        ts = r.get("ts") or ""
        if ts < alert_ts:
            continue
        act = r.get("action")
        if act in valid_actions:
            allowed_statuses = valid_actions[act]
            if allowed_statuses is None or r.get("status") in allowed_statuses:
                return True, act, ts, r.get("status")
    return False, None, None, None


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

    # 待人工：最近告警中，未被后续动作（回复、换微信、发简历、同意换微信、忽略等）处理过的会话
    seen = {}
    for r in alerts:
        c = r.get("company") or ""
        if not c:
            continue
        seen[c] = r  # 覆盖为最近一次告警

    pending = []
    resolved = []
    for c, a in seen.items():
        is_res, act, ts, st = _is_alert_resolved(a.get("ts") or "", c, rows)
        item = {
            "company": c,
            "last_msg": (a.get("last_msg") or "")[:500],
            "reason": (a.get("reason") or "")[:200],
            "suggested": a.get("suggested_reply") or "",
            "time": a.get("ts") or "",
            "high_intent": bool(a.get("high_intent")),
            "resolved_action": act,
            "resolved_time": ts,
            "resolved_status": st,
        }
        if is_res:
            resolved.append(item)
        else:
            pending.append(item)

    pending.sort(key=lambda x: x["time"], reverse=True)
    resolved.sort(key=lambda x: x.get("resolved_time") or x["time"], reverse=True)

    return {
        "counts": {
            "pending": len(pending),
            "replied_total": len(replied_ok),
            "high_intent": len([p for p in pending if p["high_intent"]]),
            "resolved_total": len(resolved),
        },
        "today": {
            "scanned": len([r for r in scans if (r.get("ts") or "").startswith(today)]),
            "replied": len([r for r in replied_ok if (r.get("ts") or "").startswith(today)]),
        },
        "guard": guardmod.Guard(cfg).summary(),
        "pending": pending[:100],
        "resolved": resolved[:100],
        "ledger": [
            {"ts": r.get("ts") or "", "action": r.get("action") or "",
             "company": r.get("company") or r.get("title") or "",
             "status": r.get("status") or r.get("reason") or r.get("text_head") or ""}
            for r in rows[-50:]
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
    if act not in ("reply", "exchange_wechat", "send_resume", "agree_wechat", "ignore", "mark_handled") or not company:
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
