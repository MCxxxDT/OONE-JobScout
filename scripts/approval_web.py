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
import re
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
    greeter, guard as guardmod, ledger, profile_store, secrets as secrets_mod

app = FastAPI(title="boss-apply 审批台")

ALLOWED_EXTS = (".pdf", ".docx", ".txt", ".md")
MAX_UPLOAD = 5 * 1024 * 1024  # 5MB


def _web_cfg(cfg):
    return cfg.get("web") or {}


def _check_token(cfg, token):
    want = _web_cfg(cfg).get("token") or os.getenv("APPROVAL_TOKEN") or "boss-apply"
    return bool(token) and token == want


def _write_local(section_key, section_updates):
    """深合并或直接写入 config.local.json 的指定顶层字段（保留其他段）。"""
    path = cfgmod.LOCAL_CFG_PATH
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if isinstance(section_updates, dict):
        cur = data.get(section_key) or {}
        if not isinstance(cur, dict):
            cur = {}
        cur.update(section_updates)
        data[section_key] = cur
    else:
        data[section_key] = section_updates
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
    daemon_cfg = cfg.get("daemon") or {}
    return {
        "job_mode": cfg.get("job_mode", "intern"),
        "auto_apply": {
            "enabled": daemon_cfg.get("auto_apply", True),
            "apply_window": daemon_cfg.get("apply_window", "10:00-14:00"),
            "apply_max_pages": daemon_cfg.get("apply_max_pages", 3),
            "apply_top_n": daemon_cfg.get("apply_top_n", 15),
            "apply_fetch_detail": daemon_cfg.get("apply_fetch_detail", True),
        },
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

    # 求职定向模态更新 (experience 门禁)
    if "job_mode" in body:
        jm = str(body["job_mode"]).strip().lower()
        if jm in ("intern", "campus", "mix", "all"):
            _write_local("job_mode", jm)
            changed.append("求职定向模态已更新为 %s" % jm)

    # 每日自动投递设置更新
    if "auto_apply" in body and isinstance(body["auto_apply"], dict):
        aa = body["auto_apply"]
        clean_aa = {}
        if "enabled" in aa:
            clean_aa["auto_apply"] = bool(aa["enabled"])
        if "apply_window" in aa:
            clean_aa["apply_window"] = str(aa["apply_window"]).strip()
        if "apply_max_pages" in aa:
            clean_aa["apply_max_pages"] = max(1, min(10, int(aa["apply_max_pages"])))
        if "apply_top_n" in aa:
            clean_aa["apply_top_n"] = max(1, min(50, int(aa["apply_top_n"])))
        if "apply_fetch_detail" in aa:
            clean_aa["apply_fetch_detail"] = bool(aa["apply_fetch_detail"])
        if clean_aa:
            _write_local("daemon", clean_aa)
            changed.append("每日自动投递设置已更新")

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
    if "job_mode" in body:
        jm = str(body["job_mode"]).strip().lower()
        if jm in ("intern", "campus", "mix", "all"):
            _write_local("job_mode", jm)
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


@app.post("/api/playground/simulate")
async def api_playground_simulate(request: Request, token: str = ""):
    """回复演练场沙盒推演：接收模拟消息与JD，构建完整 Prompt，调用底层 LLM 并输出全景穿透与安全审计。
    绝不向线上发出任何真实消息。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json() or {}

    msg = (body.get("message") or "").strip()
    company = (body.get("company") or "").strip() or "模拟HR"
    job_title = (body.get("job_title") or "").strip() or "AI产品经理实习生"
    salary = (body.get("salary") or "").strip()
    city = (body.get("city") or "").strip()
    jd = (body.get("jd") or "").strip()
    raw_history = body.get("history") or []

    history = []
    if isinstance(raw_history, list):
        for item in raw_history:
            if isinstance(item, dict):
                history.append({
                    "role": item.get("role") or "me",
                    "text": item.get("text") or ""
                })
            elif isinstance(item, str) and item.strip():
                s = item.strip()
                if s.startswith("我方:") or s.startswith("me:"):
                    history.append({"role": "me", "text": s.split(":", 1)[1].strip()})
                elif s.startswith("HR:") or s.startswith("hr:"):
                    history.append({"role": "hr", "text": s.split(":", 1)[1].strip()})
                else:
                    history.append({"role": "hr", "text": s})

    conv = {
        "who": company,
        "last_msg": msg,
        "time": "刚刚",
        "job": {
            "title": job_title,
            "company": company,
            "salary": salary,
            "city": city,
            "jd_text": jd,
        },
        "history": history,
    }

    engine = air.AIReplyEngine(cfg)
    hi_flag, hi_why = air.detect_high_intent(conv)

    system_prompt = (body.get("custom_system_prompt") or "").strip() or (
        "你是一名求职助理 Agent，代表求职者回复招聘平台HR消息。严格输出纯JSON。真人口语化，杜绝客服八股文，严禁使用任何Markdown标记（如**），保持整句完整自然收尾。"
    )
    user_prompt = (body.get("custom_user_prompt") or "").strip() or engine.build_agent_prompt(conv)

    t0 = datetime.datetime.now()
    llm_res = None
    latency_ms = 0
    raw_llm_text = ""
    reasoning_content = ""
    llm_error = None

    has_key = bool(engine.openai_key or engine.openrouter_key)
    if has_key:
        try:
            import urllib.request
            headers = {"Content-Type": "application/json", "User-Agent": "boss-apply/1.0"}
            if engine.openrouter_key:
                url = "https://openrouter.ai/api/v1/chat/completions"
                headers["Authorization"] = f"Bearer {engine.openrouter_key}"
                model = engine.llm_model if engine.llm_model != "gpt-4o-mini" else "deepseek/deepseek-chat"
            else:
                base = engine.openai_base.rstrip("/")
                url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
                headers["Authorization"] = f"Bearer {engine.openai_key}"
                model = engine.llm_model

            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.5,
                "max_tokens": 8192,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                latency_ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
                if resp.status == 200:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    choice_msg = resp_data["choices"][0]["message"]
                    raw_llm_text = (choice_msg.get("content") or "").strip()
                    reasoning_content = (choice_msg.get("reasoning_content") or "").strip()

                    content = re.sub(r"^```(?:json)?\s*", "", raw_llm_text)
                    content = re.sub(r"```$", "", content).strip()
                    try:
                        llm_res = json.loads(content)
                    except Exception as pe:
                        llm_error = f"JSON解析失败: {pe}"
                else:
                    llm_error = f"HTTP {resp.status}: {resp.read().decode('utf-8')[:200]}"
        except Exception as e:
            latency_ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
            llm_error = str(e)
    else:
        llm_error = "未配置 LLM API Key（请在设置面板中填入 Key 或配置环境变量）"

    parsed_decision = {}
    if isinstance(llm_res, dict) and "action" in llm_res:
        parsed_decision = dict(llm_res)
    else:
        if any(w in msg for w in ("简历", "附件", "作品")):
            parsed_decision = {
                "action": "send_resume",
                "reason": "HR 索要简历，触发官方简历投递决策",
                "suggested_reply": "好的，已为您发送附件简历，请查收！"
            }
        elif any(w in msg for w in ("微信", "电话", "手机", "联系方式")):
            parsed_decision = {
                "action": "exchange_wechat",
                "reason": "HR 提及联系方式，引导使用平台官方安全交换功能",
                "suggested_reply": "已向您发起平台交换微信请求，请点击同意~"
            }
        elif any(w in msg for w in ("谢谢", "好的", "收到", "ok", "OK", "感谢")):
            parsed_decision = {
                "action": "skip",
                "reason": "HR 发送礼貌结束语，会话自然闭环，跳过回复",
                "suggested_reply": ""
            }
        else:
            parsed_decision = {
                "action": "reply",
                "reason": "常规业务沟通，基于画像与JD生成拟人化回答",
                "suggested_reply": "您好！目前人在福州，随时可以奔赴现场实习，期待与贵团队进一步交流！"
            }

    raw_suggested = parsed_decision.get("reply_text") or parsed_decision.get("suggested_reply") or ""
    cleaned_reply = air.sanitize_and_clean_reply(raw_suggested, max_chars=150)
    parsed_decision["reply_text"] = cleaned_reply

    privacy_blocked = greeter.privacy_blocked(cleaned_reply)

    # 隐私策略审查
    from scripts.daemon_auto_reply import check_privacy_permission
    allow_policy, policy_reason = check_privacy_permission(parsed_decision.get("action", "reply"), cfg, hi_flag)

    return {
        "ok": bool(llm_res is not None),
        "llm_called": has_key,
        "llm_error": llm_error,
        "latency_ms": latency_ms,
        "conv": conv,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "raw_llm_output": raw_llm_text,
        "reasoning_content": reasoning_content,
        "parsed_decision": parsed_decision,
        "cleaned_reply": cleaned_reply,
        "safety_audit": {
            "privacy_blocked": privacy_blocked,
            "privacy_note": "🚨 拦截！文案中疑似含有明文电话或微信号，物理阻断发送" if privacy_blocked else "✅ 安全通过（未检测到明文联系方式泄露）",
            "high_intent": hi_flag,
            "high_intent_reason": hi_why or "常规沟通",
            "policy_allow": allow_policy,
            "policy_reason": policy_reason,
            "online_reply_enabled": cfg.get("online_reply_enabled", True),
            "safety_mode": "🛡️ 处于安全拦截模式 (online_reply_enabled=false)，零消息发送线上真实HR",
        }
    }

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>BOSS求职守护 · 运营中枢</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #f4f5f7;
    --card: #ffffff;
    --txt: #111111;
    --mut: #64748b;
    --mut-dark: #94a3b8;
    --border: rgba(0, 0, 0, 0.05);
    --border-light: rgba(0, 0, 0, 0.03);
    --acc: #0ea5e9;
    --ok: #10b981;
    --warn: #f59e0b;
    --dan: #ef4444;
    --wechat: #07c160;
    --purple: #8b5cf6;
    --radius: 20px;
    --radius-lg: 24px;
    --shadow-sm: 0 4px 14px rgba(0, 0, 0, 0.025);
    --shadow-md: 0 10px 30px rgba(0, 0, 0, 0.035);
    --shadow-lg: 0 20px 50px rgba(0, 0, 0, 0.06);
  }

  body {
    background-color: var(--bg);
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", Roboto, sans-serif;
    color: var(--txt);
    -webkit-font-smoothing: antialiased;
    padding-bottom: 60px;
    min-height: 100vh;
  }

  .wrap { max-width: 1180px; margin: 0 auto; padding: 0 20px; }

  /* Sticky Top Header */
  .admin-header {
    background: rgba(255, 255, 255, 0.9);
    backdrop-filter: blur(25px);
    -webkit-backdrop-filter: blur(25px);
    color: #111;
    padding: 14px 32px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.02);
    margin-bottom: 20px;
    position: sticky;
    top: 0;
    z-index: 1000;
    border-bottom: 1px solid rgba(0,0,0,0.04);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .logo-squircle {
    width: 42px; height: 42px; border-radius: 12px; margin-right: 14px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.06), inset 0 1px 0 rgba(255,255,255,1);
    background: #111; color: #fff; display: flex; align-items: center; justify-content: center;
    font-size: 20px; flex-shrink: 0;
  }

  .brand-text h1 { font-size: 18px; font-weight: 900; letter-spacing: 0.3px; line-height: 1.2; margin: 0; }
  .brand-text .status-line { font-size: 12px; color: var(--mut); display: flex; align-items: center; gap: 8px; margin-top: 3px; font-weight: 500; }

  /* Pulse Dots */
  .pulse-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-green { background: #10b981; animation: pulseG 2s infinite; }
  .dot-blue { background: #3b82f6; animation: pulseB 2s infinite; }
  .dot-red { background: #ef4444; animation: pulseR 2s infinite; }
  @keyframes pulseG { 0% { box-shadow: 0 0 0 0 rgba(16,185,129,0.5); } 70% { box-shadow: 0 0 0 8px rgba(16,185,129,0); } 100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); } }
  @keyframes pulseB { 0% { box-shadow: 0 0 0 0 rgba(59,130,246,0.5); } 70% { box-shadow: 0 0 0 8px rgba(59,130,246,0); } 100% { box-shadow: 0 0 0 0 rgba(59,130,246,0); } }
  @keyframes pulseR { 0% { box-shadow: 0 0 0 0 rgba(239,68,68,0.5); } 70% { box-shadow: 0 0 0 8px rgba(239,68,68,0); } 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0); } }

  /* Signature Dynamic Island Capsule Navigation */
  .island-nav-row {
    display: flex;
    gap: 10px;
    width: 100%;
    margin-bottom: 25px;
    padding: 8px 6px;
    position: sticky;
    top: 72px;
    z-index: 990;
    background: rgba(244, 245, 247, 0.92);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-radius: 0 0 20px 20px;
  }

  .island-capsule {
    flex: 1;
    height: 52px;
    border-radius: 26px;
    background: rgba(255, 255, 255, 0.7);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(255, 255, 255, 0.9);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.02);
    color: #64748b;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.45s cubic-bezier(0.32, 0.72, 0, 1.2);
    overflow: hidden;
    white-space: nowrap;
    user-select: none;
  }

  .island-capsule.active {
    flex: 2.8;
    background: #ffffff;
    color: #111111;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.05);
    border-color: #ffffff;
  }
  .island-capsule:active { transform: scale(0.97); }

  .island-svg {
    width: 20px !important;
    height: 20px !important;
    min-width: 20px !important;
    min-height: 20px !important;
    stroke: currentColor !important;
    stroke-width: 2.2 !important;
    fill: none !important;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: margin 0.35s;
    flex-shrink: 0 !important;
    display: block;
  }
  .island-capsule.active .island-svg { margin-right: 8px; }

  .capsule-text {
    opacity: 0; max-width: 0; font-size: 14px; font-weight: 800;
    transition: all 0.35s ease; display: inline-block; letter-spacing: 0.3px;
  }
  .island-capsule.active .capsule-text { opacity: 1; max-width: 160px; }

  .capsule-badge {
    background: #ef4444; color: #fff; font-size: 11px; font-weight: 800;
    padding: 1px 7px; border-radius: 10px; margin-left: 6px; line-height: 16px;
    display: inline-block;
  }

  /* KPI Stats Grid */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 25px;
  }
  .stat-card {
    background: #ffffff;
    border: 1px solid rgba(0,0,0,0.03);
    border-radius: var(--radius);
    padding: 20px 22px;
    box-shadow: var(--shadow-sm);
    transition: transform 0.2s, box-shadow 0.2s;
  }
  .stat-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-md); }
  .stat-card .label, .stat-card .stat-label { font-size: 13px; font-weight: 700; color: var(--mut); display: flex; justify-content: space-between; align-items: center; }
  .stat-card .val, .stat-card .stat-val { font-size: 30px; font-weight: 900; margin-top: 8px; color: var(--txt); letter-spacing: -0.5px; }
  .stat-card .val.c-acc, .stat-card .stat-val.c-acc { color: #0284c7; }
  .stat-card .val.c-ok, .stat-card .stat-val.c-ok { color: #059669; }
  .stat-card .val.c-warn, .stat-card .stat-val.c-warn { color: #d97706; }
  .stat-card .val.c-dan, .stat-card .stat-val.c-dan { color: #dc2626; }
  .stat-card .hint, .stat-card .stat-hint { font-size: 11px; color: var(--mut-dark); margin-top: 4px; font-weight: 500; }

  /* Quota Progress Bar */
  .quota-track { width: 100%; height: 4px; background: #e2e8f0; border-radius: 4px; margin-top: 8px; overflow: hidden; }
  .quota-fill { height: 100%; width: 0%; background: #10b981; border-radius: 4px; transition: width 0.5s ease; }

  /* Main Tab Content Panels */
  .tab-content { display: none; }
  .tab-content.active { display: block; animation: fadeIn 0.25s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  .panel-card {
    background: #fff;
    padding: 28px;
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-md);
    margin-bottom: 25px;
    border: 1px solid rgba(0,0,0,0.03);
    position: relative;
  }

  /* Spotlight Search Group */
  .admin-search-group { position: relative; width: 100%; max-width: 420px; z-index: 100; display: flex; align-items: center; }
  .spotlight-icon { position: absolute; left: 16px; width: 18px; height: 18px; stroke: #94a3b8; stroke-width: 2.5; fill: none; pointer-events: none; transition: stroke 0.3s; z-index: 2; }
  .admin-search-input {
    width: 100%; border-radius: 24px; background: #fff; border: 1.5px solid #e2e8f0;
    padding: 12px 42px 12px 46px; outline: none; transition: all 0.25s ease;
    font-size: 13px; font-weight: 600; color: #111; box-shadow: 0 2px 8px rgba(0,0,0,0.02);
  }
  .admin-search-input:focus { background: #fff; border-color: #111; box-shadow: 0 10px 25px rgba(0,0,0,0.06); }
  .admin-search-group:focus-within .spotlight-icon { stroke: #111; }
  .admin-search-clear {
    position: absolute; right: 14px; width: 22px; height: 22px; background: #e2e8f0; color: #64748b;
    border-radius: 50%; display: none; align-items: center; justify-content: center; cursor: pointer;
    font-size: 12px; font-weight: bold; transition: all 0.2s; z-index: 2;
  }
  .admin-search-clear:hover { background: #cbd5e1; color: #111; transform: scale(1.1); }

  /* Custom Floating Table */
  .table-custom { border-collapse: separate; border-spacing: 0 10px; margin-top: -10px; width: 100%; }
  .table-custom th { border: none; font-weight: 800; color: #94a3b8; text-transform: uppercase; font-size: 11px; letter-spacing: 1.2px; padding: 0 20px 8px; }
  .table-custom td { background: #fff; padding: 18px 20px; vertical-align: middle; border-top: 1px solid #f8fafc; border-bottom: 1px solid #f8fafc; }
  .table-custom td:first-child { border-top-left-radius: 18px; border-bottom-left-radius: 18px; border-left: 1px solid #f8fafc; }
  .table-custom td:last-child { border-top-right-radius: 18px; border-bottom-right-radius: 18px; border-right: 1px solid #f8fafc; }
  .table-custom tbody tr { transition: all 0.25s ease; }
  .table-custom tbody tr:hover { transform: translateY(-2px); box-shadow: 0 8px 25px rgba(0,0,0,0.03); }

  /* Soft Badges */
  .soft-badge { font-weight: 800; padding: 5px 12px; border-radius: 10px; font-size: 11px; border: 1px solid transparent; letter-spacing: 0.3px; display: inline-block; }
  .badge-pub { background: rgba(16,185,129,0.08); color: #059669; border-color: rgba(16,185,129,0.15); }
  .badge-rej { background: rgba(239,68,68,0.08); color: #dc2626; border-color: rgba(239,68,68,0.15); }
  .badge-ai { background: rgba(245,158,11,0.08); color: #d97706; border-color: rgba(245,158,11,0.15); }
  .badge-blue { background: rgba(14,165,233,0.08); color: #0284c7; border-color: rgba(14,165,233,0.15); }
  .badge-purple { background: rgba(139,92,246,0.08); color: #7c3aed; border-color: rgba(139,92,246,0.15); }

  /* Conv Card (Pending Review) */
  .conv-card {
    background: #ffffff;
    border-radius: 20px;
    padding: 22px 24px;
    margin-bottom: 20px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.02);
    border: 1px solid rgba(0,0,0,0.035);
    transition: all 0.25s ease;
  }
  .conv-card:hover { box-shadow: 0 10px 30px rgba(0,0,0,0.04); transform: translateY(-1px); }
  .conv-card.hi { border-left: 5px solid #ef4444; }

  .avatar-circle {
    width: 42px; height: 42px; border-radius: 14px; background: #111; color: #fff;
    display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 15px;
    box-shadow: 0 4px 10px rgba(0,0,0,0.08);
  }

  .quote-box {
    background: #f8fafc; border-left: 4px solid #111;
    border-radius: 0 14px 14px 0; padding: 14px 18px; font-size: 14px; color: #1e293b;
    margin: 14px 0; line-height: 1.6; white-space: pre-wrap; font-weight: 500;
  }
  .reason-box {
    background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.2);
    border-radius: 12px; padding: 10px 14px; font-size: 13px; color: #b45309;
    margin-bottom: 14px; line-height: 1.5; font-weight: 500;
  }

  /* Quick Chip Buttons */
  .chips-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
  .chip-btn {
    background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 16px;
    padding: 5px 12px; font-size: 12px; font-weight: 600; color: #475569;
    cursor: pointer; transition: all 0.15s ease;
  }
  .chip-btn:hover { background: #111; color: #fff; border-color: #111; transform: translateY(-1px); }

  /* Reply Text Box */
  .reply-textarea {
    width: 100%; border-radius: 14px; background: #f8fafc; border: 1.5px solid #e2e8f0;
    padding: 12px 16px; font-size: 14px; color: #111; outline: none; transition: all 0.2s;
    min-height: 75px; resize: vertical; font-family: inherit;
  }
  .reply-textarea:focus { background: #fff; border-color: #111; box-shadow: 0 6px 20px rgba(0,0,0,0.04); }

  /* Buttons */
  .btn-black {
    background: #111; color: #fff; border: none; border-radius: 16px;
    padding: 10px 20px; font-weight: 700; font-size: 13px; transition: 0.2s;
    box-shadow: 0 4px 14px rgba(0,0,0,0.12); display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
  }
  .btn-black:hover { background: #262626; transform: translateY(-1px); box-shadow: 0 6px 18px rgba(0,0,0,0.16); color: #fff; }
  .btn-black:active { transform: scale(0.97); }

  .btn-action-wechat {
    background: #07c160; color: #fff; border: none; border-radius: 16px;
    padding: 10px 18px; font-weight: 700; font-size: 13px; transition: 0.2s;
    box-shadow: 0 4px 12px rgba(7, 193, 96, 0.25); display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
  }
  .btn-action-wechat:hover { background: #06ad56; transform: translateY(-1px); color: #fff; }
  .btn-action-wechat:active { transform: scale(0.97); }

  .btn-action-resume {
    background: #8b5cf6; color: #fff; border: none; border-radius: 16px;
    padding: 10px 18px; font-weight: 700; font-size: 13px; transition: 0.2s;
    box-shadow: 0 4px 12px rgba(139, 92, 246, 0.25); display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
  }
  .btn-action-resume:hover { background: #7c3aed; transform: translateY(-1px); color: #fff; }
  .btn-action-resume:active { transform: scale(0.97); }

  .btn-action-light {
    background: #f1f5f9; color: #475569; border: 1px solid #e2e8f0; border-radius: 16px;
    padding: 10px 16px; font-weight: 700; font-size: 13px; transition: 0.2s; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-action-light:hover { background: #e2e8f0; color: #111; }
  .btn-action-light:active { transform: scale(0.97); }

  .btn-action-nuke {
    background: #fee2e2; color: #ef4444; border: none; border-radius: 16px;
    padding: 10px 16px; font-weight: 700; font-size: 13px; transition: 0.2s; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-action-nuke:hover { background: #ef4444; color: #fff; box-shadow: 0 4px 12px rgba(239,68,68,0.25); }
  .btn-action-nuke:active { transform: scale(0.97); }

  /* Settings Blocks */
  .settings-block {
    background: #fafafa; border: 1px solid rgba(0,0,0,0.04);
    border-radius: 18px; padding: 24px; margin-bottom: 20px;
  }
  .settings-block h6 {
    font-weight: 800; font-size: 14px; margin-bottom: 14px; color: #111;
    border-left: 4px solid #111; padding-left: 10px; line-height: 1.3;
  }
  .settings-block label {
    font-size: 12px; font-weight: 600; color: #64748b; margin: 10px 0 5px; display: block;
  }
  .settings-block input[type=text], .settings-block input[type=password], .settings-block select, .settings-block textarea {
    width: 100%; background: #fff; border: 1.5px solid #e2e8f0; border-radius: 12px;
    padding: 10px 14px; font-size: 13px; font-family: inherit; outline: none; transition: border-color 0.2s; color: #111;
  }
  .settings-block input[type=text]:focus, .settings-block input[type=password]:focus, .settings-block select:focus, .settings-block textarea:focus {
    border-color: #111; box-shadow: 0 4px 12px rgba(0,0,0,0.03);
  }
  .settings-block textarea { min-height: 60px; resize: vertical; }

  /* App Toast */
  .app-toast {
    position: fixed; top: -100px; left: 50%; transform: translateX(-50%);
    background: rgba(17,17,17,0.95); color: #fff; padding: 14px 28px;
    border-radius: 30px; font-size: 14px; font-weight: 800; z-index: 99999;
    transition: top 0.4s cubic-bezier(0.32, 0.72, 0, 1.2);
    box-shadow: 0 15px 35px rgba(0,0,0,0.15); pointer-events: none;
    display: flex; align-items: center; gap: 8px;
  }
  .app-toast.show { top: 30px; }

  /* Filter Pills */
  .filter-pills { display: flex; gap: 8px; flex-wrap: wrap; }
  .filter-pill {
    background: #fff; border: 1.5px solid #e2e8f0; border-radius: 20px;
    padding: 6px 15px; font-size: 12px; font-weight: 700; color: #64748b;
    cursor: pointer; transition: all 0.2s; user-select: none;
  }
  .filter-pill:hover { background: #f8fafc; color: #111; border-color: #cbd5e1; }
  .filter-pill.active { background: #111; color: #fff; border-color: #111; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }

  /* Confirmation Modal */
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.35);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    z-index: 100000; justify-content: center; align-items: center;
    opacity: 0; transition: opacity 0.25s ease;
  }
  .modal-overlay.active { display: flex; opacity: 1; }
  .modal-card {
    background: #fff; padding: 36px 32px; border-radius: 28px; width: 90%; max-width: 420px;
    text-align: center; transform: scale(0.92); transition: transform 0.3s cubic-bezier(0.32, 0.72, 0, 1.2);
    box-shadow: 0 30px 60px rgba(0,0,0,0.18); border: 1px solid rgba(0,0,0,0.04);
  }
  .modal-overlay.active .modal-card { transform: scale(1); }

  /* Apple Style Switch */
  .form-switch-apple { position: relative; display: inline-block; width: 46px; height: 26px; flex-shrink: 0; }
  .form-switch-apple input { opacity: 0; width: 0; height: 0; }
  .switch-slider {
    position: absolute; cursor: pointer; inset: 0; background-color: #e2e8f0;
    transition: .3s cubic-bezier(0.16, 1, 0.3, 1); border-radius: 26px;
  }
  .switch-slider:before {
    position: absolute; content: ""; height: 20px; width: 20px; left: 3px; bottom: 3px;
    background-color: white; transition: .3s cubic-bezier(0.16, 1, 0.3, 1); border-radius: 50%;
    box-shadow: 0 2px 5px rgba(0,0,0,0.2);
  }
  .form-switch-apple input:checked + .switch-slider { background-color: #111; }
  .form-switch-apple input:checked + .switch-slider:before { transform: translateX(20px); }

  /* Mobile Responsive */
  /* Mobile Responsive */
  @media (max-width: 768px) {
    .admin-header { padding: 12px 16px; }
    .brand-text h1 { font-size: 15px; }
    .island-nav-row { top: 62px; padding: 6px 0; }
    .island-capsule { height: 46px; }
    .panel-card { padding: 18px; border-radius: 20px; }
  }

  /* Gemini-style Chat Arena Styles */
  .chat-arena-card {
    background: #fff; border-radius: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.035);
    border: 1px solid rgba(0,0,0,0.05); display: flex; flex-direction: column;
    height: 820px; overflow: hidden; position: relative;
  }
  .chat-arena-header {
    padding: 16px 22px; border-bottom: 1px solid #f1f5f9; display: flex;
    justify-content: space-between; align-items: center; background: #fafafa; flex-shrink: 0;
  }
  .chat-avatar {
    width: 38px; height: 38px; border-radius: 12px; display: flex; align-items: center;
    justify-content: center; font-weight: 800; font-size: 15px; flex-shrink: 0;
  }
  .chat-avatar.hr { background: #e0e7ff; color: #4338ca; }
  .chat-avatar.agent { background: #0f172a; color: #fff; }
  
  .chat-flow-container {
    flex: 1; padding: 22px; overflow-y: auto; display: flex; flex-direction: column;
    gap: 20px; background: #fdfdfd; scroll-behavior: smooth;
  }
  .chat-welcome-box {
    text-align: center; padding: 60px 20px; margin: auto; max-width: 460px;
  }
  .chat-welcome-title {
    font-size: 20px; font-weight: 900; color: var(--txt); margin-bottom: 8px; letter-spacing: -0.3px;
  }
  .chat-welcome-desc {
    font-size: 13px; color: var(--mut); line-height: 1.6; margin-bottom: 20px;
  }
  
  .chat-msg-row {
    display: flex; gap: 12px; max-width: 88%; animation: fadeInMsg 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes fadeInMsg {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .chat-msg-row.hr { align-self: flex-start; }
  .chat-msg-row.agent { align-self: flex-end; flex-direction: row-reverse; }
  
  .chat-bubble {
    padding: 14px 18px; border-radius: 20px; font-size: 14px; line-height: 1.6;
    word-break: break-word; box-shadow: 0 2px 8px rgba(0,0,0,0.02); position: relative;
  }
  .chat-msg-row.hr .chat-bubble {
    background: #f1f5f9; color: #1e293b; border-top-left-radius: 4px; border: 1px solid #e2e8f0;
  }
  .chat-msg-row.agent .chat-bubble {
    background: #0f172a; color: #f8fafc; border-top-right-radius: 4px; border: 1px solid #1e293b;
  }
  .chat-bubble-meta {
    display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 11px; color: #94a3b8;
  }
  .chat-msg-row.agent .chat-bubble-meta { justify-content: flex-end; color: #94a3b8; }
  
  .chat-bubble-action-badge {
    display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 8px;
    font-size: 11px; font-weight: 700; background: rgba(255,255,255,0.15); color: #fff;
  }
  
  /* Gemini-style Input Box */
  .chat-input-wrapper {
    padding: 14px 18px 18px; background: #fff; border-top: 1px solid #f1f5f9; flex-shrink: 0;
  }
  .preset-chips-scroll {
    display: flex; gap: 8px; overflow-x: auto; padding-bottom: 10px; margin-bottom: 6px;
    scrollbar-width: thin;
  }
  .preset-chip {
    background: #f8fafc; border: 1.5px solid #e2e8f0; border-radius: 20px;
    padding: 6px 13px; font-size: 12px; font-weight: 700; color: #475569;
    cursor: pointer; transition: all 0.2s; user-select: none; display: inline-flex;
    align-items: center; gap: 4px; white-space: nowrap; flex-shrink: 0;
  }
  .preset-chip:hover { background: #0f172a; color: #fff; border-color: #0f172a; transform: translateY(-1px); }
  
  .chat-input-box {
    background: #f8fafc; border: 1.5px solid #e2e8f0; border-radius: 20px;
    padding: 12px 16px; transition: all 0.2s; display: flex; flex-direction: column; gap: 8px;
  }
  .chat-input-box:focus-within {
    border-color: #0f172a; background: #fff; box-shadow: 0 4px 20px rgba(0,0,0,0.06);
  }
  .chat-input-textarea {
    width: 100%; border: none; outline: none; background: transparent;
    font-size: 14px; font-family: inherit; color: #0f172a; resize: none;
    max-height: 110px; line-height: 1.5; min-height: 24px;
  }
  .chat-input-actions {
    display: flex; justify-content: space-between; align-items: center;
  }
  .chat-send-btn {
    background: #0f172a; color: #fff; border: none; border-radius: 12px;
    padding: 7px 16px; font-size: 13px; font-weight: 700; display: inline-flex;
    align-items: center; gap: 6px; cursor: pointer; transition: all 0.2s;
  }
  .chat-send-btn:hover { background: #334155; transform: translateY(-1px); }
  .chat-send-btn:active { transform: scale(0.96); }

  /* Thinking Animation Bubble */
  .chat-thinking-bubble {
    display: inline-flex; align-items: center; gap: 6px; padding: 12px 18px;
    background: #f1f5f9; border-radius: 18px; border-top-right-radius: 4px;
    font-size: 13px; color: #64748b; font-weight: 600;
  }
  .thinking-dot {
    width: 6px; height: 6px; background: #64748b; border-radius: 50%;
    animation: thinkingBounce 1.4s infinite ease-in-out both;
  }
  .thinking-dot:nth-child(1) { animation-delay: -0.32s; }
  .thinking-dot:nth-child(2) { animation-delay: -0.16s; }
  @keyframes thinkingBounce {
    0%, 80%, 100% { transform: scale(0); }
    40% { transform: scale(1); }
  }

  .prompt-view-code {
    background: #0f172a; color: #e2e8f0; border-radius: 14px; padding: 16px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 12px; max-height: 380px; overflow-y: auto; white-space: pre-wrap;
    word-break: break-word; line-height: 1.6; border: 1px solid #1e293b;
  }
  .prompt-tab-pill {
    padding: 6px 14px; border-radius: 12px; font-size: 11px; font-weight: 800; cursor: pointer;
    background: #f1f5f9; color: #64748b; border: 1px solid transparent; transition: all 0.2s;
  }
  .prompt-tab-pill.active { background: #111; color: #fff; }
</style>
</head>
<body>
<!-- Top Drop Floating Pill Toast -->
<div id="appToast" class="app-toast"></div>
<div id="toastBox" style="display:none"></div>

<!-- Safe Confirmation Modal -->
<div class="modal-overlay" id="confirmModal">
  <div class="modal-card">
    <div style="width:52px;height:52px;background:#fee2e2;color:#ef4444;border-radius:18px;display:flex;align-items:center;justify-content:center;margin:0 auto 16px;font-size:24px">
      ⚠️
    </div>
    <div id="confirmTitle" style="font-size:18px;font-weight:900;margin-bottom:8px;color:#111">确认执行操作</div>
    <div id="confirmDesc" style="font-size:13px;color:#64748b;line-height:1.6;margin-bottom:26px">确定要执行此操作吗？</div>
    <div class="d-flex gap-3">
      <button class="btn-action-light w-100 justify-content-center" style="padding:12px;border-radius:14px" onclick="closeConfirm()">取消</button>
      <button class="btn-black w-100 justify-content-center" style="padding:12px;border-radius:14px" id="confirmBtn" onclick="executeConfirm()">确定执行</button>
    </div>
  </div>
</div>

<!-- Top Sticky Navbar -->
<header class="admin-header">
  <div class="d-flex align-items-center">
    <div class="logo-squircle">⚡</div>
    <div class="brand-text">
      <h1>BOSS求职守护 · 审批台</h1>
      <div class="status-line">
        <span id="guardPill" class="soft-badge badge-pub"><span class="pulse-dot dot-green"></span> 守护运行中</span>
        <span>·</span>
        <span id="sub">正在同步…</span>
      </div>
    </div>
  </div>
  <div class="d-flex align-items-center gap-3">
    <button class="btn-black" style="padding:9px 20px;font-size:13px" onclick="load(true)">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
      刷新数据
    </button>
  </div>
</header>

<div class="wrap">
  <!-- Dynamic Island Capsule Navigation -->
  <div class="island-nav-row" id="adminTabsIsland">
    <div class="island-capsule active" data-tab="pending" onclick="switchTab('pending')">
      <svg class="island-svg" viewBox="0 0 24 24"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2m-6 9 2 2 4-4"></path></svg>
      <span class="capsule-text">待办审批</span>
      <span id="pendingBadge" class="capsule-badge" style="display:none">0</span>
    </div>
    <div class="island-capsule" data-tab="ledger" onclick="switchTab('ledger')">
      <svg class="island-svg" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
      <span class="capsule-text">实时台账</span>
    </div>
    <div class="island-capsule" data-tab="settings" onclick="switchTab('settings')">
      <svg class="island-svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
      <span class="capsule-text">系统设置</span>
    </div>
    <div class="island-capsule" data-tab="playground" onclick="switchTab('playground')">
      <svg class="island-svg" viewBox="0 0 24 24"><path d="M10 2v7.31M14 9.3V1.99M8.5 2h7M14 9.3a6.5 6.5 0 1 1-4 0"/></svg>
      <span class="capsule-text">回复演练场</span>
      <span class="capsule-badge" style="background:#8b5cf6;color:#fff;display:inline-block">🧪</span>
    </div>
  </div>

  <!-- KPI Statistics Grid -->
  <section class="stats-grid" id="stats">
    <div class="stat-card">
      <div class="stat-label"><span>待处理会话</span><span>📋</span></div>
      <div class="stat-val c-dan" id="statPending">0</div>
      <div class="stat-hint">需人工审核干预</div>
    </div>
    <div class="stat-card">
      <div class="stat-label"><span>今日实发回复</span><span>💬</span></div>
      <div class="stat-val c-ok" id="statTodayReplied">0</div>
      <div class="quota-track"><div class="quota-fill" id="quotaFill"></div></div>
      <div class="stat-hint" id="quotaHint">拟人高斯发出</div>
    </div>
    <div class="stat-card">
      <div class="stat-label"><span>今日巡检扫描</span><span>🔍</span></div>
      <div class="stat-val c-acc" id="statTodayScanned">0</div>
      <div class="stat-hint">消息中心雷达</div>
    </div>
    <div class="stat-card">
      <div class="stat-label"><span>高意向猎聘</span><span>🔥</span></div>
      <div class="stat-val c-warn" id="statHighIntent">0</div>
      <div class="stat-hint">面试/Offer信号</div>
    </div>
    <div class="stat-card">
      <div class="stat-label"><span>累计安全回复</span><span>🛡️</span></div>
      <div class="stat-val" id="statRepliedTotal">0</div>
      <div class="stat-hint">零封号留痕</div>
    </div>
  </section>

  <!-- Tab 1: 待办审批 -->
  <main id="tab-pending" class="tab-content active">
    <div class="panel-card mb-4">
      <div class="d-flex justify-content-between align-items-center mb-3">
        <h5 class="fw-bold mb-0 d-flex align-items-center">
          <svg class="title-icon red" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
          待人工决策（needs_human）
        </h5>
        <span class="text-muted" style="font-size:12px;font-weight:500">优先处理触发安全门禁与高意向邀约的会话</span>
      </div>
      <div id="pending">加载中…</div>
    </div>
    <div class="panel-card">
      <details style="cursor:pointer;color:var(--mut);font-size:13px" id="resolvedBox">
        <summary style="padding:4px 0;font-weight:700;color:var(--txt)">📁 查看近期已处理会话记录 (Recently Handled)</summary>
        <div id="resolvedList" style="margin-top:12px;background:#f8fafc;border:1px solid rgba(0,0,0,0.04);border-radius:14px;padding:14px 18px"></div>
      </details>
    </div>
  </main>

  <!-- Tab 2: 实时台账 -->
  <main id="tab-ledger" class="tab-content">
    <div class="panel-card">
      <div class="d-flex flex-wrap gap-3 justify-content-between align-items-center mb-4">
        <div class="filter-pills">
          <span class="filter-pill active" data-filter="all" onclick="filterLedgerChip('all')">全部流水</span>
          <span class="filter-pill" data-filter="reply" onclick="filterLedgerChip('reply')">智能回复</span>
          <span class="filter-pill" data-filter="wechat" onclick="filterLedgerChip('wechat')">交换微信</span>
          <span class="filter-pill" data-filter="resume" onclick="filterLedgerChip('resume')">发送简历</span>
          <span class="filter-pill" data-filter="alert" onclick="filterLedgerChip('alert')">转人工告警</span>
          <span class="filter-pill" data-filter="gate" onclick="filterLedgerChip('gate')">门禁拦截</span>
        </div>
        <div class="admin-search-group" style="max-width:340px">
          <svg class="spotlight-icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
          <input type="text" id="ledgerFilter" class="admin-search-input" placeholder="快速搜索公司、动作或状态…" oninput="renderLedger(); toggleSearchClear()">
          <div class="admin-search-clear" id="ledgerFilterClear" onclick="clearLedgerSearch()" title="清空搜索">✕</div>
        </div>
      </div>
      <div class="table-responsive">
        <table class="table table-custom align-middle" id="ledger">
          <thead>
            <tr>
              <th style="width:160px">时间</th>
              <th style="width:130px">动作</th>
              <th style="width:220px">目标 / 会话</th>
              <th>状态 / 归因 / 回复摘要</th>
            </tr>
          </thead>
          <tbody id="ledgerBody"></tbody>
        </table>
      </div>
    </div>
  </main>

  <!-- Tab 3: 系统设置 -->
  <main id="tab-settings" class="tab-content">
    <div id="settingsBox">
      <div class="row g-4">
        <!-- Col 1 -->
        <div class="col-lg-6">
          <!-- LLM Settings -->
          <div class="panel-card">
            <h5 class="fw-bold mb-3 d-flex align-items-center">
              <svg class="title-icon blue" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              大模型 LLM 配置
            </h5>
            <div class="settings-block">
              <div style="font-size:12px;color:var(--mut);margin-bottom:12px" id="llmMeta">加载中…</div>
              <label>API Key（DPAPI 本机加密落盘，安全脱敏）</label>
              <input type="password" id="inKey" placeholder="留空 = 保持当前密钥不修改">
              <label>Base URL 端点</label>
              <input type="text" id="inBase">
              <label>模型名称</label>
              <input type="text" id="inModel">
              <div class="d-flex align-items-center justify-content-between p-3 mt-3" style="background:#fff;border:1.5px solid #e2e8f0;border-radius:14px">
                <div>
                  <strong style="font-size:13px;color:#111;display:block">启用 LLM 智能匹配打分</strong>
                  <span style="font-size:11px;color:var(--mut)">开启后调用大模型对岗位深度打分，关闭则回退关键词词表</span>
                </div>
                <label class="form-switch-apple">
                  <input type="checkbox" id="inLLMMatch">
                  <span class="switch-slider"></span>
                </label>
              </div>
              <div class="d-flex align-items-center gap-3 mt-4">
                <button class="btn-black" onclick="saveSettings()">保存配置</button>
                <button class="btn-action-light" onclick="testLLM()">测试连接</button>
                <span id="resLLM" style="font-size:12px"></span>
              </div>
            </div>
          </div>

          <!-- Prefs Card -->
          <div class="panel-card">
            <h5 class="fw-bold mb-3 d-flex align-items-center">
              <svg class="title-icon green" viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
              求职偏好设置
            </h5>
            <div class="settings-block">
              <div style="font-size:12px;color:var(--mut);margin-bottom:10px">留空 = 大模型基于简历与岗位上下文自主决断</div>
              <label>🎯 求职定向模态（自动锁定底层 experience 参数，杜绝社招经验门禁）</label>
              <select id="inJobMode" style="width:100%;padding:9px 12px;border:1.5px solid #e2e8f0;border-radius:12px;font-size:13px;font-weight:700;background:#fff;margin-bottom:12px;color:#111">
                <option value="intern">🎯 大厂高薪实习 (在校生专属 · experience=108)</option>
                <option value="campus">🎓 2027届秋招正式批 (应届生专属 · experience=102)</option>
                <option value="mix">⚡ 并驾齐驱混合模式 (实习与校招交替轮巡)</option>
                <option value="all">🌐 全网不限经验 (历史向下兼容模式)</option>
              </select>
              <label>向往岗位（逗号/换行分隔，高亮优先沟通）</label>
              <textarea id="inWantJobs"></textarea>
              <label>排斥岗位（命中黑名单直接过滤，不耗 Token）</label>
              <textarea id="inAvoidJobs"></textarea>
              <div class="row g-2 mt-1">
                <div class="col-6"><label>向往城市</label><textarea id="inWantCities"></textarea></div>
                <div class="col-6"><label>排斥城市</label><textarea id="inAvoidCities"></textarea></div>
              </div>
              <div class="d-flex align-items-center gap-3 mt-4">
                <button class="btn-black" onclick="savePrefs()">保存偏好</button>
                <span id="resPrefs" style="font-size:12px"></span>
              </div>
              <div id="effInfo" style="font-size:12px;color:var(--mut);margin-top:14px;line-height:1.6"></div>
            </div>
          </div>

          <!-- Auto-Apply Card -->
          <div class="panel-card">
            <h5 class="fw-bold mb-3 d-flex align-items-center">
              <svg class="title-icon purple" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              每日自动智能投递
            </h5>
            <div class="settings-block">
              <div style="font-size:12px;color:var(--mut);margin-bottom:12px">
                在工作时间窗口内，由大模型遍历全城岗位、精读JD、综合评估择优生成计划并自动投递。
              </div>
              <div class="d-flex align-items-center justify-content-between p-3" style="background:#fff;border:1.5px solid #e2e8f0;border-radius:14px">
                <div>
                  <strong style="font-size:13px;color:#111;display:block">启用每日自动投递 (Auto Apply)</strong>
                  <span style="font-size:11px;color:var(--mut)">开启后后台守护进程每日自动启动一轮多阶段全城扫描与择优投递</span>
                </div>
                <label class="form-switch-apple">
                  <input type="checkbox" id="inAutoApplyEnabled" checked>
                  <span class="switch-slider"></span>
                </label>
              </div>
              <div class="row g-2 mt-2">
                <div class="col-6">
                  <label>投递扫描时段（推荐避开早晚高峰）</label>
                  <input type="text" id="inApplyWindow" placeholder="10:00-14:00" value="10:00-14:00">
                </div>
                <div class="col-6">
                  <label>单日择优投递上限（Top N）</label>
                  <input type="number" id="inApplyTopN" min="1" max="50" value="15">
                </div>
              </div>
              <div class="row g-2 mt-1">
                <div class="col-6">
                  <label>每词搜索深度（页数）</label>
                  <input type="number" id="inApplyMaxPages" min="1" max="10" value="3">
                </div>
                <div class="col-6">
                  <label>JD 精读深度评估</label>
                  <select id="inApplyFetchDetail">
                    <option value="true">开启（加载详情页精读JD，更精准）</option>
                    <option value="false">关闭（仅依列表卡片粗打分，速度快）</option>
                  </select>
                </div>
              </div>
              <div class="d-flex align-items-center gap-3 mt-4">
                <button class="btn-black" onclick="saveAutoApply()">保存投递设置</button>
                <span id="resAutoApply" style="font-size:12px"></span>
              </div>
            </div>
          </div>
        </div>

        <!-- Col 2 -->
        <div class="col-lg-6">
          <!-- Profile Card -->
          <div class="panel-card">
            <h5 class="fw-bold mb-3 d-flex align-items-center">
              <svg class="title-icon blue" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
              简历与画像中心
            </h5>
            <div class="settings-block">
              <div style="font-size:12px;color:var(--mut);margin-bottom:12px" id="profMeta">加载中…</div>
              <label>上传简历文件（支持 .pdf / .docx / .txt / .md，≤5MB）</label>
              <input type="file" id="inFile" accept=".pdf,.docx,.txt,.md" style="color:var(--mut);font-size:12px;padding:8px">
              <label style="margin-top:14px">或直接粘贴简历文本</label>
              <textarea id="inResume" placeholder="在此粘贴简历正文文本…" style="min-height:130px"></textarea>
              <div class="d-flex align-items-center gap-3 mt-4">
                <button class="btn-black" onclick="saveProfile()">保存并由 AI 提炼画像</button>
                <span id="resProfile" style="font-size:12px"></span>
              </div>
            </div>
          </div>

          <!-- Privacy & Automation Card -->
          <div class="panel-card">
            <h5 class="fw-bold mb-3 d-flex align-items-center">
              <svg class="title-icon red" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
              隐私保护与自动化权限
            </h5>
            <div class="settings-block">
              <div style="font-size:12px;color:var(--mut);margin-bottom:12px;line-height:1.5">
                自主决定敏感物理动作执行级别；出信前物理正则拦截门禁，严禁泄露联系方式。
              </div>
              <div class="row g-2">
                <div class="col-6">
                  <label>换微信权限</label>
                  <select id="inPolicyWechat">
                    <option value="auto">全自动 (auto)</option>
                    <option value="high_intent_only">仅高意向自动 (high_intent)</option>
                    <option value="manual">必须人工审批 (manual)</option>
                    <option value="disabled">禁用该动作 (disabled)</option>
                  </select>
                </div>
                <div class="col-6">
                  <label>发简历权限</label>
                  <select id="inPolicyResume">
                    <option value="auto">全自动 (auto)</option>
                    <option value="high_intent_only">仅高意向自动 (high_intent)</option>
                    <option value="manual">必须人工审批 (manual)</option>
                    <option value="disabled">禁用该动作 (disabled)</option>
                  </select>
                </div>
              </div>
              <div class="mt-2">
                <label>换电话权限</label>
                <select id="inPolicyPhone">
                  <option value="manual">必须人工审批 (manual，推荐)</option>
                  <option value="high_intent_only">仅高意向自动 (high_intent)</option>
                  <option value="auto">全自动 (auto)</option>
                  <option value="disabled">禁用该动作 (disabled)</option>
                </select>
              </div>
              <div class="row g-2 mt-1">
                <div class="col-6">
                  <label>个人真实手机号（配置后防泄密物理锁死）</label>
                  <input type="text" id="inContactPhone" placeholder="例如：13800000000">
                </div>
                <div class="col-6">
                  <label>个人真实微信号（配置后防泄密物理锁死）</label>
                  <input type="text" id="inContactWechat" placeholder="例如：wxid_xxxx">
                </div>
              </div>
              <div style="background:rgba(239,68,68,0.06);border:1px solid rgba(239,68,68,0.18);border-radius:12px;padding:12px 14px;font-size:12px;color:#dc2626;margin-top:14px;line-height:1.5">
                🔒 <strong>防套话安全铁律</strong>：模型严禁在文本中吐出明文联系方式；若 HR 催促或诱导索要电话微信，系统仅允许引导官方交换。若模型被攻破输出明文信息，底层正则门禁将物理拦截并立即转人工告警。
              </div>
              <div class="d-flex align-items-center gap-3 mt-4">
                <button class="btn-black" onclick="savePrivacyPolicy()">保存隐私权限设置</button>
                <span id="resPrivacy" style="font-size:12px"></span>
              </div>
            </div>
          </div>

          <!-- Browser Mode Card -->
          <div class="panel-card">
            <h5 class="fw-bold mb-3 d-flex align-items-center">
              <svg class="title-icon" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
              浏览器后台运行设置
            </h5>
            <div class="settings-block">
              <div style="font-size:12px;color:var(--mut);margin-bottom:12px">
                解决 BOSS 轮询时 Chrome 窗口时不时弹窗、置顶、抢占桌面输入焦点的问题。
              </div>
              <div class="d-flex align-items-center justify-content-between p-3 mt-3" style="background:#fff;border:1.5px solid #e2e8f0;border-radius:14px">
                <div>
                  <strong style="font-size:13px;color:#111;display:block">静默后台巡检模式 (Silent Mode)</strong>
                  <span style="font-size:11px;color:var(--mut)">开启后通过 CDP 隐藏标签页执行操作，绝不抢占前台键盘输入焦点与激活置顶</span>
                </div>
                <label class="form-switch-apple">
                  <input type="checkbox" id="inBrowserSilent">
                  <span class="switch-slider"></span>
                </label>
              </div>
              <div class="d-flex align-items-center justify-content-between p-3 mt-3" style="background:#fff;border:1.5px solid #e2e8f0;border-radius:14px">
                <div>
                  <strong style="font-size:13px;color:#111;display:block">启动时窗口最小化 (Minimize On Start)</strong>
                  <span style="font-size:11px;color:var(--mut)">启动脚本拉起 Chrome 时自动以最小化启动，避免巨大浏览器窗口覆盖主屏幕</span>
                </div>
                <label class="form-switch-apple">
                  <input type="checkbox" id="inBrowserMinimize">
                  <span class="switch-slider"></span>
                </label>
              </div>
              <div class="d-flex align-items-center gap-3 mt-4">
                <button class="btn-black" onclick="saveBrowserSettings()">保存浏览器设置</button>
                <span id="resBrowser" style="font-size:12px"></span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </main>

  <!-- Tab 4: 回复演练场 (Playground) -->
  <main id="tab-playground" class="tab-content">
    <div class="row g-4">
      <!-- Left Col: Gemini-style Chat Arena (会话聊天流) -->
      <div class="col-lg-7">
        <div class="chat-arena-card">
          <!-- Chat Header -->
          <div class="chat-arena-header">
            <div class="d-flex align-items-center gap-3">
              <div class="chat-avatar hr" id="chatTargetAvatar">HR</div>
              <div>
                <div style="font-size:15px;font-weight:800;color:var(--txt)">
                  <span id="chatTargetCompany">米哈游 · 人力资源部</span>
                  <span style="color:var(--mut-dark);margin:0 4px">·</span>
                  <span id="chatTargetJob" style="color:var(--mut);font-weight:600;font-size:13px">AI产品经理实习生</span>
                </div>
                <div style="font-size:11px;color:var(--ok);font-weight:600;display:flex;align-items:center;gap:4px">
                  <span class="pulse-dot dot-green" style="width:6px;height:6px"></span>
                  沙盒推演就绪 (零物理外发)
                </div>
              </div>
            </div>
            <div class="d-flex align-items-center gap-2">
              <button class="btn-action-light" style="padding:4px 12px;font-size:11px;border-radius:12px" onclick="clearPlaygroundChat()">
                🗑️ 清空会话
              </button>
            </div>
          </div>

          <!-- Chat Flow Scrollable Area -->
          <div class="chat-flow-container" id="pgChatFlow">
            <!-- Welcome Empty State -->
            <div class="chat-welcome-box" id="pgChatWelcome">
              <div style="font-size:38px;margin-bottom:12px">✨</div>
              <div class="chat-welcome-title">AI 求职对话推演沙盒</div>
              <div class="chat-welcome-desc">
                在此模拟 HR 与候选人的真实对话。大模型将根据您设定的候选人画像、目标岗位 JD 与防套话铁律，实时推演并生成真人口语化回复。
              </div>
              <div style="font-size:11px;color:var(--mut);font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">
                👇 点击下方场景胶囊，一键载入并推演：
              </div>
            </div>
          </div>

          <!-- Chat Input Bottom Area -->
          <div class="chat-input-wrapper">
            <!-- Preset Chips Horizontal Scroll -->
            <div class="preset-chips-scroll">
              <span class="preset-chip" onclick="loadPlaygroundPreset('birthday_wechat')">🎂 生日蛋糕+要微信 (复合意图)</span>
              <span class="preset-chip" onclick="loadPlaygroundPreset('ask_resume')">📄 索要简历</span>
              <span class="preset-chip" onclick="loadPlaygroundPreset('arrival_time')">📅 到岗与毕业</span>
              <span class="preset-chip" onclick="loadPlaygroundPreset('ask_wechat')">🔒 索要微信电话 (套话测试)</span>
              <span class="preset-chip" onclick="loadPlaygroundPreset('salary')">💰 询问期望薪资</span>
              <span class="preset-chip" onclick="loadPlaygroundPreset('interview_offline')">🏢 询问能否线下面试</span>
              <span class="preset-chip" onclick="loadPlaygroundPreset('closing')">☕ 礼貌闭环 (好的谢谢)</span>
            </div>

            <!-- Gemini-style Input Box -->
            <div class="chat-input-box">
              <textarea id="pgMsg" class="chat-input-textarea" rows="2" placeholder="输入模拟 HR 发来的消息…（按 Enter 运行推演，Shift+Enter 换行）"></textarea>
              <div class="chat-input-actions">
                <span id="pgStatusHint" style="font-size:12px;color:var(--mut);font-weight:500">按 Enter 发送</span>
                <button class="chat-send-btn" id="btnSimulate" onclick="runPlaygroundSimulation()">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                  <span>立即推演</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Right Col: 当前设定调整面板 + 推演透视与安全审查 -->
      <div class="col-lg-5">
        <!-- Panel 1: 当前沟通背景与岗位设定 (可查看与调整) -->
        <div class="panel-card mb-3" style="padding:20px">
          <div class="d-flex justify-content-between align-items-center mb-2">
            <h6 class="fw-bold mb-0 d-flex align-items-center" style="font-size:14px">
              <svg class="title-icon blue" style="width:16px;height:16px" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              目标岗位与背景设定
            </h6>
            <span style="font-size:11px;color:var(--mut);cursor:pointer" onclick="togglePlaygroundSettings()">
              <span id="pgSettingsArrow">▼ 折叠/展开</span>
            </span>
          </div>
          <div style="font-size:11px;color:var(--mut);margin-bottom:10px">
            实时设定影响大模型对 JD 契合度与沟通语气的决策，修改后下一条推演立即生效：
          </div>

          <div id="pgSettingsBlock" class="settings-block" style="padding:14px;margin-bottom:0">
            <div class="row g-2">
              <div class="col-6">
                <label style="margin:2px 0 3px">公司名称</label>
                <input type="text" id="pgCompany" placeholder="如：米哈游" value="米哈游 · 人力资源部" oninput="syncChatHeader()">
              </div>
              <div class="col-6">
                <label style="margin:2px 0 3px">岗位名称</label>
                <input type="text" id="pgJobTitle" placeholder="如：AI产品经理实习生" value="AI产品经理实习生" oninput="syncChatHeader()">
              </div>
            </div>

            <div class="row g-2 mt-1">
              <div class="col-6">
                <label style="margin:2px 0 3px">薪资范围</label>
                <input type="text" id="pgSalary" placeholder="如：350-450元/天" value="350-450元/天">
              </div>
              <div class="col-6">
                <label style="margin:2px 0 3px">工作地点</label>
                <input type="text" id="pgCity" placeholder="如：上海" value="上海">
              </div>
            </div>

            <div class="mt-2">
              <label style="margin:2px 0 3px">岗位 JD 详细描述 (可直接粘贴企业JD)</label>
              <textarea id="pgJd" style="height:70px;font-size:12px" placeholder="粘贴岗位职责与任职要求…">职责：参与米哈游大模型工具链设计与Agent工作流搭建；任职要求：统招本科2027届，具备优秀的逻辑与沟通表达能力，每周到岗5天，实习6个月以上。</textarea>
            </div>

            <div class="mt-2">
              <label style="margin:2px 0 3px">前序对话历史 (格式：我方: ... 或 HR: ...)</label>
              <textarea id="pgHistory" style="height:55px;font-size:12px" placeholder="我方: 您好，这是我的经历简介&#10;HR: 同学你好"></textarea>
            </div>

            <!-- Advanced Prompt Settings Collapsible -->
            <div class="mt-2 pt-2" style="border-top:1px dashed #e2e8f0">
              <div class="d-flex align-items-center justify-content-between" style="cursor:pointer" onclick="togglePlaygroundAdv()">
                <span style="font-size:11px;font-weight:700;color:var(--txt)">⚙️ 高级人设与 Prompt 自定义</span>
                <span id="pgAdvArrow" style="font-size:11px;color:var(--mut)">▼ 展开</span>
              </div>
              <div id="pgAdvBlock" style="display:none;margin-top:8px">
                <label style="margin:2px 0 3px">自定义 System Prompt（留空使用默认人设约束）</label>
                <textarea id="pgCustomSys" style="height:50px;font-size:11px" placeholder="留空使用系统默认人设与三不原则"></textarea>
                <label style="margin:4px 0 3px">自定义 User Prompt 覆盖（留空根据画像与JD组装）</label>
                <textarea id="pgCustomUser" style="height:60px;font-size:11px" placeholder="留空自动由系统画像与上下文动态组装"></textarea>
              </div>
            </div>
          </div>
        </div>

        <!-- Panel 2: 推演全景透视与安全审查 -->
        <div class="panel-card mb-3" style="padding:20px">
          <div class="d-flex justify-content-between align-items-center mb-3">
            <h6 class="fw-bold mb-0 d-flex align-items-center" style="font-size:14px">
              <svg class="title-icon green" style="width:16px;height:16px" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
              决策透视与安全门禁
            </h6>
            <div id="pgHeaderBadges" class="d-flex gap-2">
              <span class="soft-badge badge-pub">沙盒拦截锁死</span>
            </div>
          </div>

          <div id="pgResultBox">
            <!-- Action & Decision Block -->
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;padding:14px;margin-bottom:12px">
              <div class="d-flex justify-content-between align-items-center mb-2">
                <div class="d-flex align-items-center gap-2">
                  <span style="font-size:12px;font-weight:800;color:var(--txt)">🎯 决策动作:</span>
                  <span id="pgActionBadge" class="soft-badge badge-blue">待推演</span>
                </div>
                <div style="font-size:11px;color:var(--mut);font-weight:600" id="pgLatency">耗时: -</div>
              </div>
              <div style="font-size:12px;color:var(--mut);line-height:1.5">
                <strong style="color:var(--txt)">决策归因：</strong><span id="pgReasonText">请在左侧发送消息进行推演</span>
              </div>
            </div>

            <!-- Safety Gate Checklist -->
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;padding:14px;margin-bottom:12px">
              <div style="font-size:12px;font-weight:800;color:var(--txt);margin-bottom:10px">🛡️ 4 重物理安全门禁审查清单</div>
              <div class="d-flex flex-column gap-2" style="font-size:12px">
                <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                  <span>🔒 隐私防套话审查 (电话/微信号)</span>
                  <span id="pgGatePrivacy" class="soft-badge badge-pub">✅ 安全通过</span>
                </div>
                <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                  <span>🔥 意向识别与打标</span>
                  <span id="pgGateIntent" class="soft-badge badge-blue">常规意向</span>
                </div>
                <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                  <span>⚙️ Web 隐私权限策略校验</span>
                  <span id="pgGatePolicy" class="soft-badge badge-pub">允许执行</span>
                </div>
                <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                  <span>🛡️ 线上发送状态 (安全隔离)</span>
                  <span id="pgGateOnline" class="soft-badge badge-rej">🛡️ 物理拦截 (零外发)</span>
                </div>
              </div>
            </div>

            <!-- Deep Prompt & Raw LLM Inspector -->
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;padding:14px">
              <div class="d-flex justify-content-between align-items-center mb-2">
                <div style="font-size:12px;font-weight:800;color:var(--txt)">🔍 完整上下文穿透查看</div>
                <div class="d-flex gap-1">
                  <span class="prompt-tab-pill active" id="pillTabUser" onclick="switchPromptInspectorTab('user')">User Prompt</span>
                  <span class="prompt-tab-pill" id="pillTabSys" onclick="switchPromptInspectorTab('sys')">System Prompt</span>
                  <span class="prompt-tab-pill" id="pillTabRaw" onclick="switchPromptInspectorTab('raw')">LLM 原生 JSON</span>
                  <span class="prompt-tab-pill" id="pillTabThink" onclick="switchPromptInspectorTab('think')" style="display:none">思考过程</span>
                </div>
              </div>
              <div class="d-flex justify-content-end mb-2">
                <button class="btn-action-light" style="padding:2px 8px;font-size:11px" onclick="copyCurrentPromptInspector()">📋 复制当前代码</button>
              </div>
              <pre id="pgPromptCode" class="prompt-view-code" style="max-height:260px">// 推演后在此穿透查看完整 Prompt 与大模型原生输出</pre>
            </div>
          </div>
        </div>
      </div>
    </div>
  </main>
</div>

<script>
let TOKEN = new URLSearchParams(location.search).get('token') || '';
if (!TOKEN) {
  const match = document.cookie.match(/(?:^|;\\s*)boss_apply_token=([^;]*)/);
  TOKEN = match ? decodeURIComponent(match[1]) : (localStorage.getItem('boss_apply_token') || '');
}
if (TOKEN) {
  localStorage.setItem('boss_apply_token', TOKEN);
  document.cookie = 'boss_apply_token=' + encodeURIComponent(TOKEN) + ';path=/;max-age=2592000';
}
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
  const pill = document.getElementById('appToast');
  if (pill) {
    const icon = type === 'success' ? '✅ ' : (type === 'error' ? '❌ ' : 'ℹ️ ');
    pill.innerHTML = `<span>${icon}</span><span>${esc(msg)}</span>`;
    pill.style.background = type === 'error' ? '#ef4444' : (type === 'success' ? '#111' : '#1e293b');
    pill.classList.add('show');
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => {
      pill.classList.remove('show');
    }, 3200);
  }
  const box = document.getElementById('toastBox');
  if (box) {
    const t = document.createElement('div');
    t.className = 'toast ' + type;
    t.innerHTML = `<span>${esc(msg)}</span>`;
    box.appendChild(t);
    setTimeout(() => t.remove(), 3200);
  }
}

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.island-capsule').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
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
      gp.className = 'soft-badge badge-rej';
      gp.innerHTML = '<span class="pulse-dot dot-red"></span> ⛔ 风控熔断: ' + esc(d.guard.paused);
    } else {
      gp.className = 'soft-badge badge-pub';
      gp.innerHTML = '<span class="pulse-dot dot-green"></span> 守护运行中 (正常)';
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

    // 配额进度与提示
    const maxReplied = (d.guard && d.guard.max_replies_per_day) || 30;
    const curReplied = d.today.replied || 0;
    const qFill = document.getElementById('quotaFill');
    if (qFill) qFill.style.width = Math.min(100, Math.round(curReplied / maxReplied * 100)) + '%';
    const qHint = document.getElementById('quotaHint');
    if (qHint) qHint.textContent = `今日已发 ${curReplied} / 配额上限 ${maxReplied}`;

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

let _confirmCallback = null;
function askConfirm(title, desc, onOk) {
  const m = document.getElementById('confirmModal');
  if (!m) { if (onOk) onOk(); return; }
  document.getElementById('confirmTitle').textContent = title || '确认执行操作';
  document.getElementById('confirmDesc').textContent = desc || '确定要执行此操作吗？';
  _confirmCallback = onOk;
  m.classList.add('active');
}

function closeConfirm() {
  const m = document.getElementById('confirmModal');
  if (m) m.classList.remove('active');
  _confirmCallback = null;
}

function executeConfirm() {
  const cb = _confirmCallback;
  closeConfirm();
  if (cb) cb();
}

function toggleSearchClear() {
  const el = document.getElementById('ledgerFilter');
  const btn = document.getElementById('ledgerFilterClear');
  if (btn) btn.style.display = (el && el.value.trim()) ? 'flex' : 'none';
}

function clearLedgerSearch() {
  const el = document.getElementById('ledgerFilter');
  if (el) {
    el.value = '';
    renderLedger();
    toggleSearchClear();
    el.focus();
  }
}

function renderPending() {
  const p = document.getElementById('pending');
  if (!pendingData.length) {
    p.innerHTML = `
      <div class="empty-box" style="padding:40px 20px;text-align:center">
        <span style="font-size:36px;display:block;margin-bottom:12px">🎉</span>
        <h4 style="font-size:16px;font-weight:800;color:var(--txt);margin-bottom:6px">当前无待人工处理会话</h4>
        <p style="font-size:13px;color:var(--mut);max-width:460px;margin:0 auto;line-height:1.6">求职守护智能引擎正在后台持续巡检，当遇到电话隐私红线、高意向邀约或决策边界时将自动呈现在此。</p>
      </div>`;
    return;
  }
  p.innerHTML = pendingData.map((c, i) => `
    <div class="conv-card ${c.high_intent ? 'hi' : ''}">
      <div class="d-flex justify-content-between align-items-center mb-3">
        <div class="d-flex align-items-center gap-3">
          <div class="avatar-circle">${esc((c.company || 'H').slice(0, 1))}</div>
          <div>
            <div style="font-size:16px;font-weight:800;color:var(--txt)">${esc(c.company)}</div>
            <div style="font-size:12px;color:var(--mut);font-weight:500">最后活跃：${esc(c.time || '刚刚')}</div>
          </div>
        </div>
        <div>
          ${c.high_intent ? '<span class="soft-badge badge-ai">🔥 高意向邀约</span>' : '<span class="soft-badge badge-pub">已拦截</span>'}
        </div>
      </div>

      <div class="quote-box">
        <div style="font-size:11px;font-weight:700;color:var(--mut);margin-bottom:4px;text-transform:uppercase;letter-spacing:0.5px">💬 HR 最新消息</div>
        <div>${c.last_msg ? esc(c.last_msg) : '<span style="color:var(--mut-dark)">（系统隐私拦截/对方发送联系方式，大模型已被门禁阻断，由人工接管）</span>'}</div>
      </div>

      <div class="reason-box">
        <span>🛡️ 拦截原因：</span><span>${esc(c.reason || '大模型触发安全策略')}</span>
      </div>

      <div class="my-3">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <span style="font-size:12px;font-weight:700;color:var(--txt)">✍️ 回复文案（可直接在此编辑，修改后一键发送）</span>
          <span id="charCount${i}" style="color:var(--mut);font-size:11px">已输入 ${(c.suggested || '').length} 字</span>
        </div>
        <textarea id="replyText${i}" class="reply-textarea" oninput="document.getElementById('charCount${i}').textContent = '已输入 ' + this.value.length + ' 字'" placeholder="在此输入或微调自定义回复文案…">${esc(c.suggested || '')}</textarea>
        <div class="chips-row">
          <span style="font-size:11px;color:var(--mut);display:flex;align-items:center;font-weight:700;margin-right:2px">快捷补齐:</span>
          <span class="chip-btn" onclick="addChip(${i},'方便加微信详细沟通吗？')">+ 加微信</span>
          <span class="chip-btn" onclick="addChip(${i},'稍后为您发送简历！')">+ 发简历</span>
          <span class="chip-btn" onclick="addChip(${i},'可随时配合线上初试')">+ 约初试</span>
          <span class="chip-btn" onclick="clearDraft(${i})">清空文案</span>
        </div>
      </div>

      <div class="d-flex flex-wrap align-items-center gap-2 mt-3 pt-2" style="border-top:1px solid rgba(0,0,0,0.04)">
        <button class="btn-black" id="btnReply${i}" onclick="actReply(${i})">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
          发送回复
        </button>
        <button class="btn-action-wechat" id="btnWx${i}" onclick="actWithText(${i},'exchange_wechat')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
          换微信
        </button>
        <button class="btn-action-resume" id="btnCv${i}" onclick="actWithText(${i},'send_resume')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          发简历
        </button>
        <button class="btn-action-light" id="btnAgree${i}" onclick="actWithText(${i},'agree_wechat')">
          🤝 同意换微信
        </button>
        <button class="btn-action-light" id="btnDone${i}" onclick="act(${i},'mark_handled')" title="已在手机微信或BOSS端手动处理，直接标记为已完成消单">
          ✅ 标记已处理
        </button>
        <button class="btn-action-nuke" onclick="askConfirm('确认忽略该会话？', '忽略后系统本轮将不再跟进该企业消息，直到对方再次发信。', () => act(${i},'ignore'))">
          ✕ 忽略
        </button>
        <div id="res${i}" style="margin-left:auto;font-size:12px;font-weight:700"></div>
      </div>
    </div>
  `).join('');

  // 渲染近期已处理会话
  const rb = document.getElementById('resolvedList');
  if (rb) {
    if (!resolvedData.length) {
      rb.innerHTML = '<div style="color:var(--mut-dark);font-size:12px;padding:4px 0">暂无近期处理记录</div>';
    } else {
      rb.innerHTML = resolvedData.map(r => `
        <div class="d-flex justify-content-between align-items-center py-2" style="border-bottom:1px solid rgba(0,0,0,0.03);font-size:12px">
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
  if (btn) btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> 发送中…';
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
    if (btn) btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg> 发送回复';
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
    tbody.innerHTML = '<tr><td colspan="4" class="text-center py-5 text-muted" style="font-size:13px">无匹配台账流水</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.slice(0, 50).map(r => {
    let badgeCls = 'badge-blue';
    let actName = r.action;
    if (r.action === 'reply') { badgeCls = 'badge-pub'; actName = '智能回复'; }
    else if (r.action.includes('wechat')) { badgeCls = 'badge-pub'; actName = '交换微信'; }
    else if (r.action.includes('resume')) { badgeCls = 'badge-purple'; actName = '发送简历'; }
    else if (r.action.includes('alert')) { badgeCls = 'badge-rej'; actName = '转人工告警'; }
    else if (r.action.includes('gate')) { badgeCls = 'badge-ai'; actName = '时间/门禁拦截'; }

    let stColor = 'var(--mut)';
    let st = r.status || '';
    if (st === 'ok') stColor = 'var(--ok)';
    else if (st === 'already_sent' || st === 'already_agreed') { stColor = 'var(--acc)'; st = '已发起(无需重发)'; }
    else if (st.includes('blocked') || st.includes('fail')) stColor = 'var(--dan)';

    return `
      <tr>
        <td style="white-space:nowrap;font-size:12px;color:var(--mut);font-weight:600">${esc(r.ts)}</td>
        <td><span class="soft-badge ${badgeCls}">${esc(actName)}</span></td>
        <td style="font-weight:700;color:var(--txt)">${esc(r.company || '-')}</td>
        <td style="color:${stColor};font-weight:500">${esc(st.slice(0, 60))}</td>
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
  if (document.getElementById('inJobMode')) document.getElementById('inJobMode').value = s.job_mode || 'intern';

  const priv = s.privacy_policy || {};
  if (document.getElementById('inPolicyWechat')) document.getElementById('inPolicyWechat').value = priv.exchange_wechat || 'auto';
  if (document.getElementById('inPolicyResume')) document.getElementById('inPolicyResume').value = priv.send_resume || 'auto';
  if (document.getElementById('inPolicyPhone')) document.getElementById('inPolicyPhone').value = priv.exchange_phone || 'manual';
  if (document.getElementById('inContactPhone')) document.getElementById('inContactPhone').value = priv.contact_phone || '';
  if (document.getElementById('inContactWechat')) document.getElementById('inContactWechat').value = priv.contact_wechat || '';

  const br = s.browser || {};
  if (document.getElementById('inBrowserSilent')) document.getElementById('inBrowserSilent').checked = br.silent_mode !== false;
  if (document.getElementById('inBrowserMinimize')) document.getElementById('inBrowserMinimize').checked = br.minimize_on_start !== false;

  const aa = s.auto_apply || {};
  if (document.getElementById('inAutoApplyEnabled')) document.getElementById('inAutoApplyEnabled').checked = aa.enabled !== false;
  if (document.getElementById('inApplyWindow')) document.getElementById('inApplyWindow').value = aa.apply_window || '10:00-14:00';
  if (document.getElementById('inApplyTopN')) document.getElementById('inApplyTopN').value = aa.apply_top_n || 15;
  if (document.getElementById('inApplyMaxPages')) document.getElementById('inApplyMaxPages').value = aa.apply_max_pages || 3;
  if (document.getElementById('inApplyFetchDetail')) document.getElementById('inApplyFetchDetail').value = String(aa.apply_fetch_detail !== false);

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
  const body = {
    want_jobs: document.getElementById('inWantJobs').value,
    avoid_jobs: document.getElementById('inAvoidJobs').value,
    want_cities: document.getElementById('inWantCities').value,
    avoid_cities: document.getElementById('inAvoidCities').value,
    job_mode: document.getElementById('inJobMode') ? document.getElementById('inJobMode').value : 'intern'
  };
  const d = await api('/api/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (el) {
    el.style.color = d.ok ? 'var(--ok)' : 'var(--dan)';
    el.textContent = d.ok ? '✅ 已保存（下轮扫描生效）' : '❌ 保存失败';
    if (d.unknown_cities && d.unknown_cities.length) el.textContent += ' ⚠ 未识别城市：' + d.unknown_cities.join('、');
  }
  if (d.ok) { showToast('求职偏好与模态已保存！', 'success'); loadSettings(); }
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

async function saveAutoApply() {
  const el = document.getElementById('resAutoApply');
  const body = {
    auto_apply: {
      enabled: document.getElementById('inAutoApplyEnabled').checked,
      apply_window: document.getElementById('inApplyWindow').value.trim(),
      apply_top_n: parseInt(document.getElementById('inApplyTopN').value) || 15,
      apply_max_pages: parseInt(document.getElementById('inApplyMaxPages').value) || 3,
      apply_fetch_detail: document.getElementById('inApplyFetchDetail').value === 'true',
    }
  };
  const d = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (el) {
    el.style.color = d.ok ? 'var(--ok)' : 'var(--dan)';
    el.textContent = d.ok ? '✅ ' + (d.changed || []).join('；') : '❌ 保存失败';
  }
  if (d.ok) {
    showToast('每日自动投递设置已保存！', 'success');
    loadSettings();
  }
}

// Playground Simulation State & Logic
let currentPgData = null;
let currentInspectorTab = 'user';
let pgChatMessages = [];

const PG_PRESETS = {
  birthday_wechat: {
    msg: "目前岗位还开放，你方便告诉我一下你的生日吗，我们会给每个新入职的人准备生日蛋糕，然后你顺便可以发一下微信给我",
    company: "米哈游 · 人力资源部",
    job: "AI产品经理实习生",
    salary: "350-450元/天",
    city: "上海",
    jd: "职责：参与米哈游大模型工具链设计与Agent工作流搭建；任职要求：统招本科2027届，具备优秀的逻辑与沟通表达能力，每周到岗5天，实习6个月以上。",
    history: "我方: 您好！非常关注米哈游在AI与内容生产方向的探索，这是我的基本情况，希望有机会交流！\\nHR: 目前岗位还开放，你方便告诉我一下你的生日吗，我们会给每个新入职的人准备生日蛋糕，然后你顺便可以发一下微信给我"
  },
  ask_resume: {
    msg: "你好！看你的项目经历很契合，方便发一份完整的附件简历给我看看吗？",
    company: "美团 · 核心本地商业",
    job: "AI产品经理实习生",
    salary: "250-350元/天",
    city: "北京/上海",
    jd: "职责：参与美团商户智能化与Agent产品搭建；任职要求：统招本科27届，具备大模型应用与工作流搭建经验，每周到岗5天，实习6个月以上。",
    history: "我方: 您好！非常关注贵团队的 Agent 业务落地，这是我的基本情况。\\nHR: 收到，看项目经历很契合，方便发一份完整的附件简历给我看看吗？"
  },
  arrival_time: {
    msg: "同学你好，目前在校还是已经可以出来实习了？最快什么时候可以到岗？能实习几个月？",
    company: "小红书 · 社区技术部",
    job: "大模型产品实习生",
    salary: "300-400元/天",
    city: "上海",
    jd: "职责：负责小红书创作者端大模型辅助写作功能；任职要求：统招本科在读，毕业设计已交付无日常课程羁绊，可随时现场到岗，2027届毕业优先转正。",
    history: "我方: 您好！我对创作者端大模型工具非常感兴趣，希望有机会交流！\\nHR: 同学你好，目前在校还是已经可以出来实习了？最快什么时候可以到岗？能实习几个月？"
  },
  ask_wechat: {
    msg: "平台打字不太方便，留个你的微信或者电话吧，我让业务主管直接加你电话沟通！",
    company: "某知名猎头/AI初创",
    job: "AI商业化产品",
    salary: "200-300元/天",
    city: "杭州",
    jd: "职责：AI 应用场景落地与客户对接；任职要求：大专及以上，沟通能力强。",
    history: "HR: 平台打字不太方便，留个你的微信或者电话吧，我让业务主管直接加你电话沟通！"
  },
  salary: {
    msg: "请问同学你目前的期望薪资是多少？从福州跨城过来能接受我们的实习津贴吗？",
    company: "网易 · 伏羲实验室",
    job: "AI算法与产品协同实习生",
    salary: "180-250元/天",
    city: "杭州",
    jd: "职责：参与游戏化与具身智能产品评估；任职要求：统招本科，了解多模态技术。",
    history: "我方: 您好！对伏羲实验室的大模型方向很感兴趣！\\nHR: 请问同学你目前的期望薪资是多少？从福州跨城过来能接受我们的实习津贴吗？"
  },
  interview_offline: {
    msg: "明天下午两点或者周五下午，方便直接来上海杨浦现场面试吗？",
    company: "商汤科技 · 基础模型部",
    job: "大模型评估产品实习生",
    salary: "250-300元/天",
    city: "上海",
    jd: "职责：参与模型 Eval 体系搭建；任职要求：2027届本科，逻辑清晰。",
    history: "我方: 您好，这是我的经历简介，期待交流！\\nHR: 明天下午两点或者周五下午，方便直接来上海杨浦现场面试吗？"
  },
  closing: {
    msg: "好的，收到！我先同步给部门主管评估一下，谢谢同学！",
    company: "阿里巴巴 · 淘天集团",
    job: "淘天AI产品实习生",
    salary: "250-350元/天",
    city: "杭州",
    jd: "职责：淘天商家端智能经营工具设计。",
    history: "我方: 好的，附件简历已为您发出，期待您的反馈！\\nHR: 好的，收到！我先同步给部门主管评估一下，谢谢同学！"
  }
};

function syncChatHeader() {
  const comp = (document.getElementById('pgCompany') ? document.getElementById('pgCompany').value.trim() : '') || '模拟HR';
  const job = (document.getElementById('pgJobTitle') ? document.getElementById('pgJobTitle').value.trim() : '') || '实习生';
  const compEl = document.getElementById('chatTargetCompany');
  const jobEl = document.getElementById('chatTargetJob');
  const avEl = document.getElementById('chatTargetAvatar');
  if (compEl) compEl.textContent = comp;
  if (jobEl) jobEl.textContent = job;
  if (avEl) avEl.textContent = (comp.replace(/[^\u4e00-\u9fa5a-zA-Z0-9]/g, '') || 'H').slice(0, 1);
}

function togglePlaygroundSettings() {
  const b = document.getElementById('pgSettingsBlock');
  const arrow = document.getElementById('pgSettingsArrow');
  if (!b) return;
  const isShow = b.style.display !== 'none';
  b.style.display = isShow ? 'none' : 'block';
  if (arrow) arrow.textContent = isShow ? '▼ 展开设定' : '▲ 折叠设定';
}

function clearPlaygroundChat() {
  pgChatMessages = [];
  const flow = document.getElementById('pgChatFlow');
  if (flow) {
    flow.innerHTML = `
      <div class="chat-welcome-box" id="pgChatWelcome">
        <div style="font-size:38px;margin-bottom:12px">✨</div>
        <div class="chat-welcome-title">AI 求职对话推演沙盒</div>
        <div class="chat-welcome-desc">
          在此模拟 HR 与候选人的真实对话。大模型将根据您设定的候选人画像、目标岗位 JD 与防套话铁律，实时推演并生成真人口语化回复。
        </div>
        <div style="font-size:11px;color:var(--mut);font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">
          👇 点击下方场景胶囊，一键载入并推演：
        </div>
      </div>
    `;
  }
  showToast('会话已清空', 'info');
}

function appendChatMessage(role, text, meta) {
  const welcome = document.getElementById('pgChatWelcome');
  if (welcome) welcome.style.display = 'none';

  const flow = document.getElementById('pgChatFlow');
  if (!flow) return;

  const row = document.createElement('div');
  row.className = 'chat-msg-row ' + role;

  const timeStr = new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});

  if (role === 'hr') {
    const compName = (document.getElementById('pgCompany') ? document.getElementById('pgCompany').value : 'HR');
    const avatarChar = (compName.replace(/[^\u4e00-\u9fa5a-zA-Z0-9]/g, '') || 'H').slice(0, 1);
    row.innerHTML = `
      <div class="chat-avatar hr">${esc(avatarChar)}</div>
      <div>
        <div class="chat-bubble">${esc(text)}</div>
        <div class="chat-bubble-meta">
          <span>${esc(timeStr)}</span>
        </div>
      </div>
    `;
  } else {
    const actBadge = meta && meta.action ? `<span class="chat-bubble-action-badge">${esc(meta.action)}</span>` : '';
    const latency = meta && meta.latency_ms ? `<span>· 耗时 ${meta.latency_ms}ms</span>` : '';
    row.innerHTML = `
      <div class="chat-avatar agent">AI</div>
      <div>
        <div class="chat-bubble">
          ${esc(text)}
        </div>
        <div class="chat-bubble-meta">
          ${actBadge}
          ${latency}
          <span>· ${esc(timeStr)}</span>
        </div>
      </div>
    `;
  }

  flow.appendChild(row);
  flow.scrollTop = flow.scrollHeight;
}

function showChatThinking() {
  const flow = document.getElementById('pgChatFlow');
  if (!flow) return null;
  const welcome = document.getElementById('pgChatWelcome');
  if (welcome) welcome.style.display = 'none';

  const row = document.createElement('div');
  row.className = 'chat-msg-row agent';
  row.id = 'chatThinkingRow';
  row.innerHTML = `
    <div class="chat-avatar agent">AI</div>
    <div>
      <div class="chat-thinking-bubble">
        <span>Agent 深度思考推演中</span>
        <div class="d-flex align-items-center gap-1 ms-1">
          <div class="thinking-dot"></div>
          <div class="thinking-dot"></div>
          <div class="thinking-dot"></div>
        </div>
      </div>
    </div>
  `;
  flow.appendChild(row);
  flow.scrollTop = flow.scrollHeight;
  return row;
}

function removeChatThinking() {
  const row = document.getElementById('chatThinkingRow');
  if (row) row.remove();
}

function loadPlaygroundPreset(key) {
  const p = PG_PRESETS[key];
  if (!p) return;
  document.getElementById('pgCompany').value = p.company;
  document.getElementById('pgJobTitle').value = p.job;
  document.getElementById('pgSalary').value = p.salary;
  document.getElementById('pgCity').value = p.city;
  document.getElementById('pgJd').value = p.jd;
  document.getElementById('pgHistory').value = p.history;
  syncChatHeader();
  document.getElementById('pgMsg').value = p.msg;
  showToast(`已载入场景：${p.company} · ${p.job}`, 'info');
  runPlaygroundSimulation();
}

function togglePlaygroundAdv() {
  const b = document.getElementById('pgAdvBlock');
  const arrow = document.getElementById('pgAdvArrow');
  if (!b) return;
  const isShow = b.style.display !== 'none';
  b.style.display = isShow ? 'none' : 'block';
  if (arrow) arrow.textContent = isShow ? '▼ 展开' : '▲ 收起';
}

async function runPlaygroundSimulation() {
  const msgInput = document.getElementById('pgMsg');
  const msg = (msgInput ? msgInput.value : '').trim();
  if (!msg) {
    showToast('请输入 HR 发送的消息', 'error');
    if (msgInput) msgInput.focus();
    return;
  }
  
  // 清空输入框
  if (msgInput) msgInput.value = '';
  
  // 左侧渲染 HR 消息
  appendChatMessage('hr', msg);
  
  // 展现思考等待动效
  showChatThinking();

  const btn = document.getElementById('btnSimulate');
  const statusEl = document.getElementById('pgStatusHint');
  if (btn) btn.disabled = true;
  if (statusEl) { statusEl.style.color = 'var(--acc)'; statusEl.textContent = '大模型思考中…'; }

  const histLines = (document.getElementById('pgHistory') ? document.getElementById('pgHistory').value : '')
    .split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);

  const payload = {
    message: msg,
    company: document.getElementById('pgCompany') ? document.getElementById('pgCompany').value.trim() : '',
    job_title: document.getElementById('pgJobTitle') ? document.getElementById('pgJobTitle').value.trim() : '',
    salary: document.getElementById('pgSalary') ? document.getElementById('pgSalary').value.trim() : '',
    city: document.getElementById('pgCity') ? document.getElementById('pgCity').value.trim() : '',
    jd: document.getElementById('pgJd') ? document.getElementById('pgJd').value.trim() : '',
    history: histLines,
    custom_system_prompt: document.getElementById('pgCustomSys') ? document.getElementById('pgCustomSys').value.trim() : '',
    custom_user_prompt: document.getElementById('pgCustomUser') ? document.getElementById('pgCustomUser').value.trim() : ''
  };

  try {
    const d = await api('/api/playground/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    currentPgData = d;
    window.currentPgData = d;
    
    // 移除等待动效
    removeChatThinking();
    
    // 左侧渲染 Agent 回复
    const dec = d.parsed_decision || {};
    const act = dec.action || 'reply';
    const replyText = d.cleaned_reply || dec.reply_text || dec.suggested_reply || `(执行动作：${act})`;
    appendChatMessage('agent', replyText, { action: act, latency_ms: d.latency_ms });

    // 右侧更新透视与门禁数据
    renderPlaygroundResult(d);
    showToast(`推演完成！耗时 ${d.latency_ms}ms`, 'success');
    if (statusEl) { statusEl.style.color = 'var(--ok)'; statusEl.textContent = `推演完成 (${d.latency_ms}ms)`; }
  } catch(e) {
    removeChatThinking();
    showToast('推演异常: ' + e, 'error');
    if (statusEl) { statusEl.style.color = 'var(--dan)'; statusEl.textContent = '推演异常: ' + e; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderPlaygroundResult(d) {
  // 1. Header Badges
  const badgesContainer = document.getElementById('pgHeaderBadges');
  if (badgesContainer) {
    let modeBadge = d.safety_audit && !d.safety_audit.online_reply_enabled
      ? '<span class="soft-badge badge-rej">🛡️ 线上拦截锁死</span>'
      : '<span class="soft-badge badge-ai">线上回复开启</span>';
    let llmBadge = d.llm_called
      ? '<span class="soft-badge badge-pub">🤖 LLM 直通</span>'
      : '<span class="soft-badge badge-blue">⚡ 启发式离线</span>';
    badgesContainer.innerHTML = modeBadge + ' ' + llmBadge;
  }

  // 2. Action & Decision
  const dec = d.parsed_decision || {};
  const act = dec.action || 'reply';
  const badgeEl = document.getElementById('pgActionBadge');
  if (badgeEl) {
    badgeEl.textContent = act;
    if (act === 'reply') badgeEl.className = 'soft-badge badge-pub';
    else if (act === 'exchange_wechat') badgeEl.className = 'soft-badge badge-pub';
    else if (act === 'send_resume') badgeEl.className = 'soft-badge badge-purple';
    else if (act === 'skip') badgeEl.className = 'soft-badge badge-blue';
    else if (act === 'needs_human') badgeEl.className = 'soft-badge badge-rej';
  }
  const latEl = document.getElementById('pgLatency');
  if (latEl) latEl.textContent = `耗时: ${d.latency_ms || 0}ms` + (d.llm_error ? ` · 提示: ${d.llm_error}` : '');
  const rzEl = document.getElementById('pgReasonText');
  if (rzEl) rzEl.textContent = dec.reason || '-';

  // Safety Gates
  const audit = d.safety_audit || {};
  const privEl = document.getElementById('pgGatePrivacy');
  if (privEl) {
    if (audit.privacy_blocked) {
      privEl.className = 'soft-badge badge-rej';
      privEl.textContent = '🚨 拦截！疑似泄露联系方式';
    } else {
      privEl.className = 'soft-badge badge-pub';
      privEl.textContent = '✅ 安全通过 (无明文泄露)';
    }
  }

  const intentEl = document.getElementById('pgGateIntent');
  if (intentEl) {
    if (audit.high_intent) {
      intentEl.className = 'soft-badge badge-ai';
      intentEl.textContent = `🔥 高意向信号 (${esc(audit.high_intent_reason)})`;
    } else {
      intentEl.className = 'soft-badge badge-blue';
      intentEl.textContent = '常规业务意向';
    }
  }

  const polEl = document.getElementById('pgGatePolicy');
  if (polEl) {
    if (audit.policy_allow) {
      polEl.className = 'soft-badge badge-pub';
      polEl.textContent = '✅ 依配置允许自动执行';
    } else {
      polEl.className = 'soft-badge badge-rej';
      polEl.textContent = `🔒 权限门禁转人工 (${esc(audit.policy_reason || '需审批')})`;
    }
  }

  const onlineEl = document.getElementById('pgGateOnline');
  if (onlineEl) {
    if (!audit.online_reply_enabled) {
      onlineEl.className = 'soft-badge badge-rej';
      onlineEl.textContent = '🛡️ 已物理拦截 (安全沙箱)';
    } else {
      onlineEl.className = 'soft-badge badge-pub';
      onlineEl.textContent = '⚠️ 允许线上真实发送';
    }
  }

  // Inspector
  const thinkPill = document.getElementById('pillTabThink');
  if (thinkPill) {
    thinkPill.style.display = d.reasoning_content ? 'inline-block' : 'none';
  }
  switchPromptInspectorTab(currentInspectorTab);
}

function switchPromptInspectorTab(tab) {
  currentInspectorTab = tab;
  ['user', 'sys', 'raw', 'think'].forEach(t => {
    const el = document.getElementById('pillTab' + t.charAt(0).toUpperCase() + t.slice(1));
    if (el) el.classList.toggle('active', t === tab);
  });

  const pre = document.getElementById('pgPromptCode');
  if (!pre || !currentPgData) return;

  if (tab === 'user') {
    pre.textContent = currentPgData.user_prompt || '(无 User Prompt)';
  } else if (tab === 'sys') {
    pre.textContent = currentPgData.system_prompt || '(无 System Prompt)';
  } else if (tab === 'raw') {
    if (currentPgData.raw_llm_output) {
      pre.textContent = currentPgData.raw_llm_output;
    } else {
      pre.textContent = JSON.stringify(currentPgData.parsed_decision || {}, null, 2);
    }
  } else if (tab === 'think') {
    pre.textContent = currentPgData.reasoning_content || '(模型无 reasoning_content 思考过程)';
  }
}

function copyCurrentPromptInspector() {
  const pre = document.getElementById('pgPromptCode');
  if (!pre || !pre.textContent) return;
  navigator.clipboard.writeText(pre.textContent).then(() => {
    showToast('Prompt 内容已复制！', 'success');
  }).catch(() => {
    showToast('复制失败，请手动选取', 'error');
  });
}

// 绑定输入框键盘回车事件
document.addEventListener('DOMContentLoaded', () => {
  const msgInput = document.getElementById('pgMsg');
  if (msgInput) {
    msgInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        runPlaygroundSimulation();
      }
    });
  }
});

function copyCleanedReply() {
  if (!currentPgData) return;
  const text = currentPgData.cleaned_reply || (currentPgData.parsed_decision || {}).reply_text || '';
  if (!text) { showToast('无文案可复制', 'info'); return; }
  navigator.clipboard.writeText(text).then(() => {
    showToast('回复文案已复制到剪贴板！', 'success');
  }).catch(() => {
    showToast('复制失败，请手动选取', 'error');
  });
}

function copyCurrentPromptInspector() {
  const pre = document.getElementById('pgPromptCode');
  if (!pre || !pre.textContent) return;
  navigator.clipboard.writeText(pre.textContent).then(() => {
    showToast('Prompt 内容已复制！', 'success');
  }).catch(() => {
    showToast('复制失败，请手动选取', 'error');
  });
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


AUTH_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOSS求职守护 · 工作台认证</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; margin: 0; }
  .auth-card { background: #fff; border-radius: 24px; box-shadow: 0 12px 36px rgba(0,0,0,0.06); padding: 36px; max-width: 440px; width: 100%; text-align: center; border: 1px solid rgba(0,0,0,0.05); }
  .logo-icon { width: 56px; height: 56px; background: #0ea5e9; border-radius: 18px; display: inline-flex; align-items: center; justify-content: center; color: #fff; margin-bottom: 20px; box-shadow: 0 8px 20px rgba(14,165,233,0.3); }
  .btn-quick { display: block; width: 100%; padding: 13px; background: #0f172a; color: #fff; border-radius: 14px; text-decoration: none; font-weight: 700; font-size: 14px; margin-top: 20px; transition: all 0.2s; box-shadow: 0 4px 14px rgba(15,23,42,0.2); }
  .btn-quick:hover { background: #1e293b; color: #fff; transform: translateY(-1px); }
</style>
<script>
  const saved = localStorage.getItem('boss_apply_token');
  if (saved && saved !== 'wrong') {
    location.replace('/?token=' + encodeURIComponent(saved));
  }
</script>
</head>
<body>
<div class="auth-card">
  <div class="logo-icon">
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
  </div>
  <h4 style="font-weight:800;color:#0f172a;margin-bottom:8px">工作台访问认证</h4>
  <p style="font-size:13px;color:#64748b;line-height:1.6;margin-bottom:20px">
    您当前未携带 Token 或 Token 无效。<br>如在个人电脑本机访问，可直接点击下方一键进入：
  </p>
  <a class="btn-quick" href="/?token=boss-apply">⚡ 一键使用默认 Token (boss-apply) 进入</a>
  <div style="margin:22px 0 16px;font-size:12px;color:#94a3b8;position:relative">
    <hr style="margin:0 0 12px;opacity:0.1">或输入自定义 Token
  </div>
  <form onsubmit="event.preventDefault(); const v=document.getElementById('tok').value.trim(); if(v) location.href='/?token='+encodeURIComponent(v);">
    <input type="text" id="tok" class="form-control" placeholder="输入 Token (如 boss-apply)" style="border-radius:12px;font-size:13px;padding:11px 14px;margin-bottom:12px;text-align:center">
    <button type="submit" class="btn btn-outline-secondary w-100" style="border-radius:12px;font-size:13px;font-weight:600;padding:10px">确认进入</button>
  </form>
</div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request, token: str = ""):
    cfg = cfgmod.load()
    if not token:
        token = request.cookies.get("boss_apply_token", "")
    if not _check_token(cfg, token):
        return HTMLResponse(AUTH_PAGE, status_code=401)
    resp = HTMLResponse(PAGE)
    resp.set_cookie("boss_apply_token", token, max_age=30 * 86400, httponly=False)
    return resp


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
        "guard": {
            **guardmod.Guard(cfg).summary(),
            "paused": guardmod.Guard(cfg).paused,
            "max_replies_per_day": cfg.get("daily_limit", 30),
        },
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
    host_display = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print("=" * 62)
    print("  BOSS直聘求职守护 · 运营中枢 (Web工作台)")
    print("  本机访问: http://%s:%d/?token=%s" % (host_display, args.port, token))
    print("  手机/局域网: http://<本机IP>:%d/?token=%s" % (args.port, token))
    print("=" * 62)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
