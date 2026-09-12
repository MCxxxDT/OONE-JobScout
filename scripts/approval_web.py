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
import subprocess
import sys
import time

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
    greeter, guard as guardmod, ledger, profile_store, qr_login, secrets as secrets_mod

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


def get_daemon_status():
    """检测守护进程（daemon_auto_reply.py）的物理真实存活状态与心跳。"""
    hb_path = cfgmod.state_path("daemon_heartbeat.json")
    if not os.path.exists(hb_path):
        return {"running": False, "reason": "no_heartbeat", "detail": "未检测到守护进程心跳文件"}
    try:
        with open(hb_path, "r", encoding="utf-8") as f:
            hb = json.load(f)
    except Exception:
        return {"running": False, "reason": "corrupt_heartbeat", "detail": "心跳文件损坏"}

    pid = hb.get("pid")
    ts = hb.get("ts", 0)
    status = hb.get("status", "unknown")
    age = time.time() - ts

    # 1. 进程存活性探测（Windows ctypes）
    is_alive = False
    if pid and pid > 0:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                exit_code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(handle)
                is_alive = (exit_code.value == 259)  # STILL_ACTIVE
        except Exception:
            is_alive = False

    if not is_alive:
        return {"running": False, "pid": pid, "status": "dead", "reason": "process_dead", "detail": f"进程 (PID {pid}) 已离线"}

    if status == "stopped":
        return {"running": False, "pid": pid, "status": "stopped", "reason": "stopped", "detail": "守护已主动停止"}

    if age > 1800 and status != "sleeping":
        return {"running": False, "pid": pid, "status": "stale", "reason": "heartbeat_timeout", "detail": f"心跳超时 ({int(age)}秒无响应)"}

    return {
        "running": True,
        "pid": pid,
        "status": status,
        "last_seen_seconds": round(age, 1),
        "time": hb.get("time", ""),
        "details": hb.get("details", {}),
    }


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
        "online_reply_enabled": bool(cfg.get("online_reply_enabled", False)),
        "auto_apply": {
            "enabled": daemon_cfg.get("auto_apply", True),
            "apply_window": daemon_cfg.get("apply_window", "10:00-14:00"),
            "apply_max_pages": daemon_cfg.get("apply_max_pages", 5),
            "apply_top_n": daemon_cfg.get("apply_top_n", 50),
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
    if "online_reply_enabled" in body:
        _write_local("online_reply_enabled", bool(body["online_reply_enabled"]))
        changed.append("全局在线回复门禁已更新")

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

    # 隐私泄密检测（挂接配置的 contact_phone 与 contact_wechat，100% 对齐线上 detect_privacy_leak 门禁）
    is_leak, leak_detail = air.detect_privacy_leak(cleaned_reply, cfg)
    privacy_blocked = is_leak or greeter.privacy_blocked(cleaned_reply)
    if not leak_detail and privacy_blocked:
        leak_detail = "文案中疑似含有联系方式意图或号码"

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
            "privacy_note": f"🚨 拦截！{leak_detail}，物理阻断发送" if privacy_blocked else "✅ 安全通过（已通过 contact_phone / contact_wechat 与关键词严密安全门禁）",
            "leak_detail": leak_detail,
            "contact_phone_checked": bool((cfg.get("privacy_policy") or {}).get("contact_phone")),
            "contact_wechat_checked": bool((cfg.get("privacy_policy") or {}).get("contact_wechat")),
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
<title>BOSS求职守护 · 审批台</title>
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

  /* Master App Layout with Left Sidebar */
  .app-layout {
    display: flex;
    min-height: 100vh;
    width: 100%;
    position: relative;
  }

  /* Left Sidebar Navigation */
  .app-sidebar {
    width: 220px;
    background: #ffffff;
    border-right: 1px solid #e2e8f0;
    display: flex;
    flex-direction: column;
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 1050;
    transition: width 0.28s cubic-bezier(0.4, 0, 0.2, 1), transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: 2px 0 16px rgba(0,0,0,0.02);
  }

  .sidebar-header {
    height: 64px;
    padding: 0 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid #f1f5f9;
  }
  .sidebar-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: inherit;
    overflow: hidden;
  }
  .sidebar-brand-logo {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    background: #0f172a;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    font-weight: 900;
    flex-shrink: 0;
    box-shadow: 0 4px 10px rgba(15,23,42,0.15);
  }
  .sidebar-brand-info {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .sidebar-brand-title {
    font-size: 14px;
    font-weight: 900;
    color: #0f172a;
    white-space: nowrap;
    letter-spacing: 0.2px;
  }
  .sidebar-brand-sub {
    font-size: 11px;
    color: #94a3b8;
    white-space: nowrap;
    font-weight: 600;
  }
  .sidebar-toggle-btn {
    width: 28px;
    height: 28px;
    border-radius: 8px;
    border: 1px solid #e2e8f0;
    background: #f8fafc;
    color: #64748b;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.2s;
    padding: 0;
  }
  .sidebar-toggle-btn:hover {
    background: #0f172a;
    color: #ffffff;
    border-color: #0f172a;
  }
  .sidebar-toggle-btn svg {
    width: 14px;
    height: 14px;
    stroke: currentColor;
    stroke-width: 2.2;
    fill: none;
    transition: transform 0.28s;
  }

  /* Sidebar Navigation Container */
  .sidebar-nav, .island-nav-row {
    padding: 14px 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    flex: 1;
    overflow-y: auto;
    width: 100%;
    margin-bottom: 0;
    background: transparent;
    position: static;
  }

  /* Sidebar Nav Items (inherits .island-capsule for test compatibility) */
  .island-capsule {
    width: 100%;
    height: 44px;
    border-radius: 12px;
    background: transparent;
    border: 1px solid transparent;
    color: #64748b;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding: 0 12px;
    cursor: pointer;
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: none;
    margin: 0;
    flex: none;
    user-select: none;
    overflow: hidden;
    white-space: nowrap;
  }
  .island-capsule:hover {
    background: #f8fafc;
    color: #0f172a;
  }
  .island-capsule.active {
    background: #0f172a;
    color: #ffffff;
    border-color: #0f172a;
    box-shadow: 0 4px 14px rgba(15, 23, 42, 0.16);
    flex: none;
  }
  .island-capsule:active { transform: scale(0.98); }

  .island-svg {
    width: 18px !important;
    height: 18px !important;
    min-width: 18px !important;
    min-height: 18px !important;
    stroke: currentColor !important;
    stroke-width: 2.2 !important;
    fill: none !important;
    stroke-linecap: round;
    stroke-linejoin: round;
    margin-right: 10px !important;
    flex-shrink: 0 !important;
    display: block;
  }
  .island-capsule.active .island-svg {
    stroke: #ffffff !important;
  }

  .capsule-text {
    opacity: 1 !important;
    max-width: 130px !important;
    font-size: 13px;
    font-weight: 700;
    display: inline-block;
    letter-spacing: 0.2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .capsule-badge {
    background: #ef4444; color: #fff; font-size: 10px; font-weight: 800;
    padding: 1px 6px; border-radius: 8px; margin-left: auto; line-height: 16px;
    display: inline-block;
  }

  /* Sidebar Footer */
  .sidebar-footer {
    padding: 14px 14px 16px;
    border-top: 1px solid #f1f5f9;
    background: #fafbfc;
  }
  .sidebar-status-pill {
    display: flex;
    align-items: center;
    font-size: 12px;
    font-weight: 700;
    color: #059669;
    margin-bottom: 4px;
  }
  .sidebar-sync-text {
    font-size: 11px;
    color: #94a3b8;
    font-weight: 500;
  }

  /* Collapsed Sidebar on Desktop */
  .app-sidebar.collapsed {
    width: 64px;
  }
  .app-sidebar.collapsed .sidebar-brand-info,
  .app-sidebar.collapsed .capsule-text,
  .app-sidebar.collapsed .sidebar-footer,
  .app-sidebar.collapsed .capsule-badge {
    display: none !important;
  }
  .app-sidebar.collapsed .sidebar-header {
    justify-content: center;
    padding: 0 8px;
  }
  .app-sidebar.collapsed .island-capsule {
    justify-content: center;
    padding: 0;
  }
  .app-sidebar.collapsed .island-capsule .island-svg {
    margin-right: 0 !important;
  }
  .app-sidebar.collapsed .sidebar-toggle-btn svg {
    transform: rotate(180deg);
  }

  /* App Main Layout */
  .app-main {
    flex: 1;
    margin-left: 220px;
    display: flex;
    flex-direction: column;
    min-width: 0;
    transition: margin-left 0.28s cubic-bezier(0.4, 0, 0.2, 1);
    background: var(--bg);
  }
  .app-sidebar.collapsed ~ .app-main {
    margin-left: 64px;
  }

  /* App Topbar Header */
  .app-topbar, .admin-header {
    height: 64px;
    background: rgba(255, 255, 255, 0.88);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-bottom: 1px solid #e2e8f0;
    position: sticky;
    top: 0;
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.015);
    margin-bottom: 0;
  }
  .hamburger-btn {
    display: none;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    width: 36px;
    height: 36px;
    align-items: center;
    justify-content: center;
    color: #0f172a;
    cursor: pointer;
    padding: 0;
  }
  .hamburger-btn:hover {
    background: #0f172a;
    color: #ffffff;
  }
  .hamburger-btn svg {
    width: 18px;
    height: 18px;
    stroke: currentColor;
    stroke-width: 2.2;
    fill: none;
  }
  .topbar-title {
    font-size: 16px;
    font-weight: 800;
    color: #0f172a;
    letter-spacing: 0.2px;
  }
  .app-body {
    padding: 24px 28px;
    max-width: 1400px;
    width: 100%;
    margin: 0 auto;
    flex: 1;
  }

  /* Top Sub-nav Segmented Control */
  .sub-nav-bar {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #e2e8f0;
    padding: 4px;
    border-radius: 14px;
    max-width: 100%;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .sub-nav-bar::-webkit-scrollbar { display: none; }
  .sub-nav-pill {
    padding: 7px 16px;
    border-radius: 10px;
    border: none;
    background: transparent;
    color: #64748b;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: all 0.2s ease;
    white-space: nowrap;
    text-decoration: none;
  }
  .sub-nav-pill:hover {
    color: #0f172a;
    background: rgba(255,255,255,0.6);
  }
  .sub-nav-pill.active {
    background: #ffffff;
    color: #0f172a;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
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

  /* Unified Ledger Event Cards */
  .ledger-event-card {
    background: #ffffff;
    border: 1.5px solid #e2e8f0;
    border-radius: 18px;
    padding: 18px 20px;
    margin-bottom: 16px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.02);
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .ledger-event-card:hover {
    border-color: #cbd5e1;
    box-shadow: 0 8px 24px rgba(0,0,0,0.04);
    transform: translateY(-1px);
  }
  .ledger-cycle-card {
    transition: all 0.2s ease;
  }
  .ledger-cycle-card:hover {
    border-color: #cbd5e1;
    background: #f1f5f9 !important;
  }
  .btn-score-pill {
    width: 32px;
    height: 32px;
    border-radius: 10px;
    border: 1px solid #cbd5e1;
    background: #ffffff;
    color: #334155;
    font-size: 12px;
    font-weight: 800;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .btn-score-pill:hover {
    border-color: #0f172a;
    color: #0f172a;
    background: #f8fafc;
  }
  .btn-score-pill.active {
    background: #0f172a;
    color: #ffffff;
    border-color: #0f172a;
    box-shadow: 0 2px 8px rgba(15,23,42,0.2);
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

  /* 评价与优化打分控件 */
  .btn-score-pill {
    width: 32px; height: 32px; border-radius: 9px; border: 1.5px solid #e2e8f0;
    background: #fff; color: #475569; font-size: 13px; font-weight: 800;
    display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; transition: all 0.18s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .btn-score-pill:hover { background: #f1f5f9; border-color: #cbd5e1; transform: translateY(-1px); }
  .btn-score-pill.active {
    background: #111; color: #fff; border-color: #111;
    box-shadow: 0 3px 8px rgba(0,0,0,0.18); transform: scale(1.08);
  }
  .resolved-card {
    background: #fff; border-radius: 18px; border: 1px solid rgba(0,0,0,0.05);
    box-shadow: 0 4px 12px rgba(0,0,0,0.02); transition: all 0.2s ease;
  }
  .resolved-card:hover { border-color: rgba(0,0,0,0.09); box-shadow: 0 6px 18px rgba(0,0,0,0.04); }

  /* Title & System Icons */
  .title-icon {
    width: 18px;
    height: 18px;
    max-width: 18px;
    max-height: 18px;
    stroke: currentColor;
    fill: none;
    stroke-width: 2.2;
    stroke-linecap: round;
    stroke-linejoin: round;
    margin-right: 8px;
    vertical-align: -3px;
    flex-shrink: 0;
    display: inline-block;
  }
  .title-icon.blue { color: #2563eb; }
  .title-icon.green { color: #10b981; }
  .title-icon.red { color: #ef4444; }
  .title-icon.purple { color: #8b5cf6; }
  .title-icon.amber { color: #f59e0b; }

  /* Settings Blocks & Desktop Calibration */
  .settings-block {
    background: #f8fafc; border: 1px solid #edf2f7;
    border-radius: 14px; padding: 18px 20px; margin-bottom: 0;
  }
  .settings-block h6 {
    font-weight: 800; font-size: 13px; margin-bottom: 12px; color: #111;
    border-left: 3.5px solid #111; padding-left: 8px; line-height: 1.3;
  }
  .settings-block label {
    font-size: 12px; font-weight: 600; color: #475569; margin: 8px 0 4px; display: block;
  }
  .settings-block input[type=text], .settings-block input[type=password], .settings-block input[type=number], .settings-block select, .settings-block textarea {
    width: 100%; background: #fff; border: 1.5px solid #e2e8f0; border-radius: 10px;
    padding: 7px 12px; font-size: 13px; font-family: inherit; outline: none; transition: all 0.2s ease; color: #1e293b;
  }
  .settings-block input[type=text]:focus, .settings-block input[type=password]:focus, .settings-block input[type=number]:focus, .settings-block select:focus, .settings-block textarea:focus {
    border-color: #0f172a; box-shadow: 0 0 0 3px rgba(15,23,42,0.06);
  }
  .settings-block textarea { min-height: 64px; resize: vertical; line-height: 1.45; }

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

  /* ============================================================
     Unified Mobile & Responsive Breakpoints (iPhone / Android)
     ============================================================ */
  @media (max-width: 991px) {
    .hamburger-btn { display: flex !important; }
    .sidebar-toggle-btn { display: none !important; }
    
    /* Left Sidebar: Drawer Mode */
    .app-sidebar {
      transform: translateX(-100%);
      box-shadow: 12px 0 40px rgba(0,0,0,0.22);
      width: 250px !important;
      position: fixed;
      top: 0; bottom: 0; left: 0;
      z-index: 1050;
    }
    .app-sidebar.show-mobile {
      transform: translateX(0);
    }
    .app-main {
      margin-left: 0 !important;
    }
    .sidebar-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.45);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      z-index: 1040;
    }
    .sidebar-backdrop.show-mobile {
      display: block;
    }

    /* Top Sticky Header */
    .app-topbar, .admin-header {
      padding: 0 16px;
      height: 56px;
    }
    .topbar-title {
      font-size: 14px;
    }

    /* Body & Panels */
    .app-body {
      padding: 12px 10px;
    }
    .panel-card {
      padding: 16px 12px;
      border-radius: 16px;
    }

    /* KPI Stats: Compact 2-column Grid */
    .stats-grid {
      grid-template-columns: repeat(2, 1fr) !important;
      gap: 8px !important;
      margin-bottom: 12px !important;
    }
    .stat-card {
      padding: 10px 12px !important;
      border-radius: 14px !important;
    }
    .stats-grid .stat-card:last-child:nth-child(odd) {
      grid-column: span 2;
    }
    .stat-card .val, .stat-card .stat-val {
      font-size: 18px !important;
      margin-top: 2px !important;
    }
    .stat-card .label, .stat-card .stat-label {
      font-size: 11px !important;
    }
    .stat-card .hint, .stat-card .stat-hint {
      font-size: 10px !important;
      margin-top: 2px !important;
    }
    .quota-track {
      margin-top: 4px;
      height: 3px;
    }

    /* Top Sub-nav Bar */
    .sub-nav-bar {
      width: 100%;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      display: flex;
    }
    .sub-nav-pill {
      font-size: 12px;
      padding: 6px 12px;
      flex-shrink: 0;
    }

    /* Gemini Chat Arena on Mobile */
    .chat-arena-card {
      height: 560px;
      border-radius: 18px;
    }
    .chat-arena-header {
      padding: 12px 14px;
    }
    .chat-flow-container {
      padding: 14px 12px;
      gap: 14px;
    }
    .chat-msg-row {
      max-width: 95%;
    }
    .chat-bubble {
      padding: 10px 14px;
      font-size: 13px;
      border-radius: 16px;
    }
    .chat-input-wrapper {
      padding: 10px 12px;
    }
    .chat-input-box {
      padding: 10px 12px;
      border-radius: 16px;
    }

    /* Filter pills & search */
    .filter-pills {
      overflow-x: auto;
      flex-wrap: nowrap;
      padding-bottom: 4px;
    }
    .filter-pill {
      white-space: nowrap;
      padding: 5px 12px;
      font-size: 11px;
    }
  }

  /* WeChat Web Style Centered Login Gate */
  .login-gate-overlay {
    position: fixed;
    inset: 0;
    z-index: 2100;
    background: rgba(241, 245, 249, 0.92);
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    transition: opacity 0.35s ease, visibility 0.35s ease;
  }
  .login-gate-card {
    background: #ffffff;
    width: 100%;
    max-width: 460px;
    border-radius: 28px;
    padding: 36px 32px 28px;
    box-shadow: 0 24px 64px rgba(15, 23, 42, 0.12), 0 2px 6px rgba(0,0,0,0.04);
    text-align: center;
    border: 1.5px solid rgba(226, 232, 240, 0.9);
    animation: gatePop 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes gatePop {
    0% { transform: scale(0.95); opacity: 0; }
    100% { transform: scale(1); opacity: 1; }
  }
  .login-gate-header { margin-bottom: 20px; }
  .login-gate-icon {
    width: 52px; height: 52px; border-radius: 16px;
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
    color: #fff; display: flex; align-items: center; justify-content: center;
    margin: 0 auto 14px; box-shadow: 0 8px 20px rgba(16, 185, 129, 0.25);
  }
  .login-gate-title { font-size: 20px; font-weight: 900; color: #0f172a; margin-bottom: 6px; letter-spacing: -0.3px; }
  .login-gate-sub { font-size: 13px; color: #64748b; margin-bottom: 0; }
  .login-qr-box {
    position: relative;
    width: 220px;
    height: 220px;
    margin: 0 auto 16px auto;
    background: #f8fafc;
    border-radius: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 2px solid #e2e8f0;
    overflow: hidden;
    box-shadow: inset 0 2px 6px rgba(0,0,0,0.03);
  }
  .login-gate-qrcode { width: 100%; height: 100%; object-fit: contain; padding: 10px; }
  .qr-gate-state {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 12px; width: 100%; height: 100%; padding: 16px; text-align: center;
  }
  .qr-gate-mask {
    position: absolute; inset: 0; background: rgba(255,255,255,0.94);
    backdrop-filter: blur(4px);
  }
  .login-gate-status {
    font-size: 14px; font-weight: 700; color: #0284c7; margin-bottom: 6px; min-height: 22px;
  }
  .login-gate-meta {
    font-size: 12px; color: #64748b; display: flex; align-items: center; justify-content: center; gap: 8px; margin-bottom: 20px;
  }
  .login-gate-guide {
    background: #f8fafc; border-radius: 14px; padding: 12px 14px;
    display: flex; justify-content: space-between; gap: 8px; margin-bottom: 20px; border: 1px solid #edf2f7;
  }
  .guide-step { font-size: 11px; color: #475569; display: flex; align-items: center; gap: 5px; font-weight: 500; }
  .step-num {
    width: 16px; height: 16px; border-radius: 50%; background: #0f172a; color: #fff;
    font-size: 10px; display: inline-flex; align-items: center; justify-content: center; font-weight: 700;
  }
  .login-gate-footer { border-top: 1px solid #f1f5f9; padding-top: 14px; }
  .btn-link-guest {
    background: none; border: none; font-size: 12px; color: #64748b; cursor: pointer;
    font-weight: 600; padding: 4px 10px; border-radius: 8px; transition: all 0.2s;
  }
  .btn-link-guest:hover { color: #0f172a; background: #f1f5f9; }

  /* User Profile Pill & Dropdown */
  .btn-user-profile {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 4px 12px 4px 5px;
    border-radius: 999px;
    background: #ffffff;
    border: 1.5px solid #e2e8f0;
    cursor: pointer;
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: 0 2px 6px rgba(0,0,0,0.02);
  }
  .btn-user-profile:hover {
    border-color: #cbd5e1;
    box-shadow: 0 4px 12px rgba(0,0,0,0.05);
    transform: translateY(-0.5px);
  }
  .user-avatar-box {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: linear-gradient(135deg, #0ea5e9 0%, #3b82f6 100%);
    color: #fff;
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    overflow: visible;
  }
  .user-avatar-box .pulse-dot {
    position: absolute;
    bottom: -1px;
    right: -1px;
    width: 9px;
    height: 9px;
    border: 2px solid #fff;
  }
  .avatar-placeholder {
    font-weight: 800;
    font-size: 13px;
    line-height: 1;
  }
  .user-name-label {
    font-size: 13px;
    font-weight: 800;
    color: #0f172a;
    line-height: 1.2;
  }
  .user-sub-label {
    font-size: 10px;
    color: #059669;
    font-weight: 600;
    line-height: 1.1;
  }
  .dropdown-chevron {
    color: #94a3b8;
    transition: transform 0.2s;
  }
  .profile-popover-card {
    position: absolute;
    right: 0;
    top: calc(100% + 8px);
    width: 310px;
    background: #ffffff;
    border-radius: 20px;
    border: 1.5px solid #e2e8f0;
    box-shadow: 0 16px 40px rgba(15, 23, 42, 0.12);
    padding: 18px;
    z-index: 1200;
    animation: popoverFade 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes popoverFade {
    0% { opacity: 0; transform: translateY(-6px); }
    100% { opacity: 1; transform: translateY(0); }
  }
  .popover-avatar-box {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: #3b82f6;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    box-shadow: 0 4px 10px rgba(59, 130, 246, 0.2);
  }
  .profile-meta-list {
    background: #f8fafc;
    border-radius: 14px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    border: 1px solid #edf2f7;
  }
  .profile-meta-item {
    display: flex;
    align-items: center;
    font-size: 11.5px;
  }
  .meta-icon { width: 18px; flex-shrink: 0; font-size: 12px; }
  .meta-label { color: #64748b; width: 64px; flex-shrink: 0; font-weight: 500; }
  .meta-val { color: #0f172a; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* Welcome Banner */
  .welcome-banner {
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    color: #ffffff;
    border-radius: 20px;
    padding: 18px 24px;
    margin-bottom: 24px;
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
    animation: bannerSlide 0.3s ease-out;
  }
  @keyframes bannerSlide {
    0% { transform: translateY(-8px); opacity: 0; }
    100% { transform: translateY(0); opacity: 1; }
  }
  .btn-white-action {
    background: #ffffff;
    color: #0f172a;
    border: none;
    padding: 8px 16px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 800;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    transition: all 0.2s;
  }
  .btn-white-action:hover {
    background: #f8fafc;
    transform: translateY(-1px);
  }
  .btn-dark-pill {
    background: rgba(255, 255, 255, 0.15);
    backdrop-filter: blur(10px);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.25);
    padding: 8px 16px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: all 0.2s;
  }
  .btn-dark-pill:hover {
    background: rgba(255, 255, 255, 0.25);
  }
  .btn-close-banner {
    background: none;
    border: none;
    color: rgba(255,255,255,0.6);
    width: 26px;
    height: 26px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    font-size: 13px;
    transition: all 0.2s;
  }
  .btn-close-banner:hover {
    color: #ffffff;
    background: rgba(255,255,255,0.1);
  }
</style>
</head>
<body>
<!-- Top Drop Floating Pill Toast -->
<div id="appToast" class="app-toast"></div>

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

<!-- WeChat Web Style Centered Login Gate -->
<div class="login-gate-overlay" id="loginGate" style="display:none">
  <div class="login-gate-card">
    <div class="login-gate-header">
      <div class="login-gate-icon">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
      </div>
      <h2 class="login-gate-title">BOSS 直聘扫码登录</h2>
      <p class="login-gate-sub">使用手机 App 扫码 · 自动提取真实画像 · 零重启热注入</p>
    </div>

    <!-- Centered QR Box -->
    <div class="login-qr-box" id="gateQrContainer">
      <div id="gateQrSpinner" class="qr-gate-state" style="display:flex">
        <div class="spinner-border text-primary" style="width:2.5rem;height:2.5rem" role="status"></div>
        <span style="font-size:12px;color:var(--mut);font-weight:500">正在与 Chrome CDP 同步原生二维码…</span>
      </div>
      <img id="gateQrImg" src="" alt="BOSS直聘登录二维码" class="login-gate-qrcode" style="display:none" />
      <div id="gateQrMask" class="qr-gate-state qr-gate-mask" style="display:none">
        <span style="font-size:32px">⌛</span>
        <span style="font-size:13px;font-weight:700;color:#ef4444">二维码已失效</span>
        <button class="btn-black" style="padding:6px 14px;font-size:12px;border-radius:10px;margin-top:4px" onclick="refreshGateQrCode()">点击刷新</button>
      </div>
    </div>

    <!-- Status Text & Countdown -->
    <div id="gateQrStatus" class="login-gate-status">
      请打开手机 <strong>BOSS直聘 App</strong> 扫码
    </div>
    <div class="login-gate-meta">
      <span>有效倒计时: <strong id="gateQrCountdown" style="color:#ef4444">180</strong> 秒</span>
      <span>·</span>
      <a href="javascript:void(0)" onclick="refreshGateQrCode()" style="color:#0ea5e9;text-decoration:none;font-weight:600">🔄 手动刷新</a>
    </div>

    <div class="login-gate-guide">
      <div class="guide-step"><span class="step-num">1</span> 打开手机 BOSS 直聘</div>
      <div class="guide-step"><span class="step-num">2</span> 点击【我的】>【扫一扫】</div>
      <div class="guide-step"><span class="step-num">3</span> 扫码并在手机上确认</div>
    </div>

    <!-- Guest / Skip link -->
    <div class="login-gate-footer">
      <button class="btn-link-guest" onclick="dismissLoginGate()">暂不登录，以访客模式查看大盘 →</button>
    </div>
  </div>
</div>

<!-- QR Code Scan Modal -->
<div class="modal-overlay" id="qrModal">
  <div class="modal-card" style="max-width:440px;position:relative;padding:32px 28px">
    <button type="button" onclick="closeQrModal()" style="position:absolute;top:16px;right:16px;border:none;background:rgba(0,0,0,0.05);width:32px;height:32px;border-radius:50%;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;color:#666">✕</button>
    <div style="width:48px;height:48px;background:rgba(16,185,129,0.1);color:#10b981;border-radius:16px;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:24px">
      ⚡
    </div>
    <div style="font-size:18px;font-weight:900;margin-bottom:4px;color:#111">扫码接入 BOSS直聘</div>
    <div style="font-size:12px;color:#64748b;margin-bottom:16px">安全热注入 Session Cookie · 零重启即刻生效</div>

    <div id="qrBoxContainer" style="position:relative;width:230px;height:230px;margin:0 auto 14px auto;background:#f8fafc;border-radius:18px;display:flex;align-items:center;justify-content:center;border:1.5px solid #e2e8f0;overflow:hidden">
      <!-- Spinner when loading -->
      <div id="qrSpinner" style="display:none;flex-direction:column;align-items:center;gap:10px">
        <div class="spinner-border text-primary" style="width:2.5rem;height:2.5rem" role="status"></div>
        <span style="font-size:12px;color:var(--mut)">正在捕获原生二维码…</span>
      </div>
      <!-- Real QR Image -->
      <img id="qrImg" src="" alt="BOSS登录二维码" style="display:none;width:100%;height:100%;object-fit:contain;padding:10px">
      <!-- Expired Mask -->
      <div id="qrMask" style="display:none;position:absolute;inset:0;background:rgba(255,255,255,0.92);backdrop-filter:blur(3px);flex-direction:column;align-items:center;justify-content:center;gap:10px">
        <span style="font-size:32px">⌛</span>
        <span style="font-size:13px;font-weight:700;color:#ef4444">二维码已失效</span>
        <button class="btn-black" style="padding:6px 14px;font-size:12px;border-radius:10px" onclick="refreshQrCode()">点击刷新</button>
      </div>
    </div>

    <div id="qrStatusText" style="font-size:13px;font-weight:600;color:#2563eb;margin-bottom:8px;min-height:20px">
      请打开手机 BOSS直聘 App 扫码
    </div>

    <div class="d-flex align-items-center justify-content-center gap-2 mb-3" style="font-size:11px;color:var(--mut)">
      <span>⏱️ 倒计时:</span>
      <strong id="qrCountdown" style="color:#ef4444;font-size:12px">180</strong>
      <span>秒</span>
    </div>

    <div class="d-flex gap-3">
      <button class="btn-action-light w-100 justify-content-center" style="padding:10px;border-radius:12px;font-size:13px" onclick="refreshQrCode()">🔄 刷新二维码</button>
      <button class="btn-black w-100 justify-content-center" style="padding:10px;border-radius:12px;font-size:13px" onclick="closeQrModal()">完成 / 关闭</button>
    </div>
  </div>
</div>

<div class="app-layout" id="appLayout">
  <!-- 1. Left Sidebar Navigation (Docked on Desktop, Drawer on Mobile) -->
  <aside class="app-sidebar" id="appSidebar">
    <div class="sidebar-header">
      <div class="sidebar-brand">
        <div class="sidebar-brand-logo">⚡</div>
        <div class="sidebar-brand-info">
          <div class="sidebar-brand-title">BOSS求职守护</div>
          <div class="sidebar-brand-sub">智能运营审批台</div>
        </div>
      </div>
      <button type="button" class="sidebar-toggle-btn d-none d-lg-flex" id="sidebarToggleBtn" onclick="toggleSidebarCollapse()" title="折叠/展开侧边栏">
        <svg width="18" height="18" viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
      <button type="button" class="btn-action-light d-flex d-lg-none" onclick="toggleMobileSidebar(false)" style="padding:4px 8px;border-radius:10px;font-size:12px;cursor:pointer" title="关闭主导航">
        ✕
      </button>
    </div>

    <!-- Navigation Items Container (Preserves id and island-capsule classes) -->
    <div class="sidebar-nav island-nav-row" id="adminTabsIsland">
      <div class="island-capsule active" data-tab="pending" onclick="switchTab('pending')">
        <svg class="island-svg" viewBox="0 0 24 24"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2m-6 9 2 2 4-4"></path></svg>
        <span class="capsule-text">待办审批</span>
        <span id="pendingBadge" class="capsule-badge" style="display:none">0</span>
      </div>
      <div class="island-capsule" data-tab="monitor" onclick="switchTab('monitor')">
        <svg class="island-svg" viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
        <span class="capsule-text">运行监控</span>
        <span id="sideDaemonBadge" class="capsule-badge" style="background:#10b981;color:#fff;display:none">ON</span>
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

    <!-- Sidebar Footer -->
    <div class="sidebar-footer">
      <div class="sidebar-status-pill" id="sideStatusPill">
        <span class="pulse-dot dot-red" id="sideStatusDot"></span>
        <span id="sideStatusText">守护离线</span>
      </div>
      <div class="sidebar-sync-text" id="sidebarSync">就绪同步</div>
    </div>
  </aside>

  <!-- Mobile Backdrop -->
  <div class="sidebar-backdrop" id="sidebarBackdrop" onclick="toggleMobileSidebar(false)"></div>

  <!-- 2. Main Content Area -->
  <div class="app-main">
    <!-- Top Sticky Bar -->
    <header class="app-topbar admin-header">
      <div class="d-flex align-items-center gap-3">
        <button type="button" class="hamburger-btn" onclick="toggleMobileSidebar()" title="展开/收起主导航">
          <svg width="18" height="18" viewBox="0 0 24 24"><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg>
        </button>
        <div>
          <div class="topbar-title" id="topbarTitle">待办审批</div>
          <div class="status-line" style="font-size:11px;color:var(--mut);display:flex;align-items:center;gap:6px">
            <span id="guardPill" class="soft-badge badge-rej" style="padding:2px 8px;font-size:10px"><span class="pulse-dot dot-red" style="width:6px;height:6px"></span> 正在检测…</span>
            <span>·</span>
            <span id="sub">正在同步…</span>
          </div>
        </div>
      </div>
        <!-- User Profile Pill & Dropdown -->
        <div class="user-profile-wrap" id="userProfileWrap" style="position:relative">
          <button id="btnBossAuth" class="btn-user-profile" onclick="toggleProfileDropdown()" title="个人画像与凭证状态">
            <div class="user-avatar-box">
              <img id="topUserAvatar" src="" alt="avatar" style="display:none;width:100%;height:100%;object-fit:cover;border-radius:50%" />
              <span id="topUserAvatarPlaceholder" class="avatar-placeholder">张</span>
              <span id="authDot" class="pulse-dot dot-green"></span>
            </div>
            <div class="user-info-text d-none d-sm-flex flex-column text-start">
              <span id="topUserName" class="user-name-label">张烨韬</span>
              <span id="authBtnText" class="user-sub-label">⚡ 已接入 BOSS</span>
            </div>
            <svg class="dropdown-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"></polyline></svg>
          </button>
          
          <!-- Dropdown Popover Card -->
          <div class="profile-popover-card" id="profileDropdown" style="display:none">
            <div class="d-flex align-items-center gap-3 mb-3 pb-3 border-bottom">
              <div class="popover-avatar-box">
                <img id="popoverAvatar" src="" alt="avatar" style="display:none;width:100%;height:100%;object-fit:cover;border-radius:50%" />
                <span id="popoverAvatarPlaceholder" class="avatar-placeholder" style="font-size:18px">张</span>
              </div>
              <div class="overflow-hidden">
                <div class="d-flex align-items-center gap-2">
                  <span id="popoverName" class="fw-bold" style="font-size:15px;color:#0f172a">张烨韬</span>
                  <span id="popoverGrade" class="soft-badge badge-pub" style="font-size:10px;padding:1px 6px">2027届</span>
                </div>
                <div id="popoverStatus" class="text-muted" style="font-size:11px;margin-top:2px">已连接BOSS · 在校可实习</div>
              </div>
            </div>

            <div class="profile-meta-list mb-3">
              <div class="profile-meta-item">
                <span class="meta-icon">🎓</span>
                <span class="meta-label">就读院校:</span>
                <span id="popoverSchool" class="meta-val">福建师范大学</span>
              </div>
              <div class="profile-meta-item">
                <span class="meta-icon">📚</span>
                <span class="meta-label">所学专业:</span>
                <span id="popoverMajor" class="meta-val">数字媒体技术</span>
              </div>
              <div class="profile-meta-item">
                <span class="meta-icon">📍</span>
                <span class="meta-label">常驻城市:</span>
                <span id="popoverCity" class="meta-val">福州市</span>
              </div>
              <div class="profile-meta-item">
                <span class="meta-icon">🕒</span>
                <span class="meta-label">同步时间:</span>
                <span id="popoverSyncTime" class="meta-val">刚刚</span>
              </div>
            </div>

            <div class="d-flex flex-column gap-2">
              <button class="btn-action-light w-100 justify-content-center" style="padding:8px 12px;font-size:12px;border-radius:10px;font-weight:600;color:#0284c7" onclick="syncBossProfile()">
                🔄 重新从 BOSS 同步资料
              </button>
              <button class="btn-action-light w-100 justify-content-center" style="padding:8px 12px;font-size:12px;border-radius:10px;color:#64748b" onclick="openQrModal()">
                ⚡ 重新扫码切换账号
              </button>
            </div>
          </div>
        </div>
        <button class="btn-action-light" style="padding:7px 14px;font-size:12px;border-radius:10px;border:1px solid rgba(59,130,246,0.3);background:rgba(59,130,246,0.08);color:#2563eb;display:inline-flex;align-items:center;gap:4px" onclick="triggerApplyNow()" title="执行今日候选岗位投递计划">
          <span>⚡</span>
          <span>今日投递</span>
        </button>
        <button class="btn-black" style="padding:7px 16px;font-size:12px;border-radius:10px" onclick="load(true)">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
          <span class="d-none d-sm-inline ms-1">刷新数据</span>
        </button>
      </div>
    </header>

    <!-- App Body Content -->
    <div class="app-body">
      <!-- Welcome Banner for Login Success & Quick Action -->
      <div id="welcomeBanner" class="welcome-banner" style="display:none">
        <div class="d-flex align-items-center gap-3">
          <div class="welcome-avatar-wrap">
            <img id="welcomeAvatar" src="" alt="avatar" style="display:none;width:44px;height:44px;border-radius:50%;object-fit:cover;border:2px solid rgba(255,255,255,0.3)" />
            <div id="welcomeAvatarPlaceholder" style="width:44px;height:44px;border-radius:50%;background:#10b981;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px">张</div>
          </div>
          <div>
            <div class="d-flex align-items-center gap-2">
              <span style="font-size:16px;font-weight:900" id="welcomeName">张烨韬</span>
              <span class="badge bg-success" style="font-size:10px;font-weight:600">BOSS 登录成功</span>
            </div>
            <div style="font-size:12px;opacity:0.85;margin-top:2px" id="welcomeDetail">
              福建师范大学 · 数字媒体技术 · 2027届 · 已与底座完成画像对齐
            </div>
          </div>
        </div>
        <div class="d-flex align-items-center gap-2">
          <button class="btn-white-action" onclick="triggerApplyNow()">
            ⚡ 立即投递今日候选 (Top 50)
          </button>
          <button class="btn-dark-pill" onclick="handleDaemonToggle('start')" id="welcomeDaemonBtn">
            ▶️ 开启常驻守护
          </button>
          <button type="button" class="btn-close-banner" onclick="dismissWelcomeBanner()" title="关闭横幅">✕</button>
        </div>
      </div>

      <!-- Tab 1: 待办审批 (聚焦待处理决策与运营大盘，独占 KPI 卡片) -->
      <main id="tab-pending" class="tab-content active">
        <!-- KPI Statistics Grid (Only shown in Tab 1) -->
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

        <!-- Top Sub-nav -->
        <div class="sub-nav-bar mb-3">
          <div class="sub-nav-pill active">⚡ 待人工决策（needs_human）</div>
        </div>
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
      </main>

      <!-- Tab: 运行监控 (守护进程一键启停与实时运行状态) -->
      <main id="tab-monitor" class="tab-content">
        <!-- Top Sub-nav -->
        <div class="sub-nav-bar mb-3">
          <div class="sub-nav-pill active">⚡ 守护进程实时控制与环境感知</div>
        </div>

        <div class="row g-4">
          <!-- Col 1: 守护进程一键启停 -->
          <div class="col-lg-6">
            <div class="panel-card h-100">
              <div class="d-flex justify-content-between align-items-center mb-3">
                <h5 class="fw-bold mb-0 d-flex align-items-center">
                  <svg class="title-icon green" viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
                  后台常驻守护进程
                </h5>
                <span id="daemonLiveBadge" class="soft-badge badge-rej">检测中</span>
              </div>
              
              <div class="p-3 mb-3" style="background:#f8fafc;border-radius:16px;border:1.5px solid #e2e8f0">
                <div class="d-flex align-items-center justify-content-between mb-2">
                  <span style="font-size:12px;color:var(--mut)">当前进程 PID:</span>
                  <strong id="daemonPid" style="font-size:14px;color:#111">-</strong>
                </div>
                <div class="d-flex align-items-center justify-content-between mb-2">
                  <span style="font-size:12px;color:var(--mut)">心跳活跃状态:</span>
                  <span id="daemonStatusText" style="font-size:12px;font-weight:600;color:#64748b">离线</span>
                </div>
                <div class="d-flex align-items-center justify-content-between mb-2">
                  <span style="font-size:12px;color:var(--mut)">最近心跳更新:</span>
                  <span id="daemonLastSeen" style="font-size:12px;color:#111">-</span>
                </div>
                <div class="d-flex align-items-center justify-content-between">
                  <span style="font-size:12px;color:var(--mut)">运行循环详情:</span>
                  <span id="daemonDetails" style="font-size:11px;color:var(--mut)">-</span>
                </div>
              </div>

              <div class="d-flex align-items-center gap-3">
                <button id="btnDaemonToggle" class="btn-black" style="padding:10px 22px;border-radius:12px;font-weight:700" onclick="handleDaemonToggle()">
                  ▶️ 一键启动常驻守护
                </button>
                <button class="btn-action-light" style="padding:10px 16px;border-radius:12px" onclick="load(true)">
                  🔄 刷新状态
                </button>
              </div>
              <div style="font-size:11px;color:var(--mut);margin-top:12px">
                提示：启动后将在 Windows 后台以守护进程常驻运行，定时轮询新消息、自动拟人回复并按时段投递。
              </div>
            </div>
          </div>

          <!-- Col 2: CDP 端口 & BOSS 鉴权状态 -->
          <div class="col-lg-6">
            <div class="panel-card h-100">
              <div class="d-flex justify-content-between align-items-center mb-3">
                <h5 class="fw-bold mb-0 d-flex align-items-center">
                  <svg class="title-icon blue" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                  Chrome CDP & BOSS 鉴权状态
                </h5>
                <span id="authLiveBadge" class="soft-badge badge-pub">已就绪</span>
              </div>

              <div class="p-3 mb-3" style="background:#f8fafc;border-radius:16px;border:1.5px solid #e2e8f0">
                <div class="d-flex align-items-center justify-content-between mb-2">
                  <span style="font-size:12px;color:var(--mut)">调试 Chrome CDP (9335):</span>
                  <strong id="cdpStatusText" style="font-size:13px;color:#10b981">🟢 端口已连接</strong>
                </div>
                <div class="d-flex align-items-center justify-content-between mb-2">
                  <span style="font-size:12px;color:var(--mut)">BOSS直聘登录态:</span>
                  <span id="bossLoginText" style="font-size:12px;font-weight:700;color:#10b981">🟢 已登录 (wt2 有效)</span>
                </div>
                <div class="d-flex align-items-center justify-content-between mb-2">
                  <span style="font-size:12px;color:var(--mut)">Session Cookie 凭证:</span>
                  <span id="cookiesCountText" style="font-size:12px;color:#111">已加载 14 条 Cookie</span>
                </div>
                <div class="d-flex align-items-center justify-content-between">
                  <span style="font-size:12px;color:var(--mut)">Windows DPAPI 持久化:</span>
                  <span id="dpapiStatusText" style="font-size:11px;color:#059669;font-weight:600">🔒 已安全加密落盘</span>
                </div>
              </div>

              <div class="d-flex align-items-center gap-3">
                <button class="btn-black" style="padding:10px 20px;border-radius:12px;background:#059669;color:#fff" onclick="openQrModal()">
                  ⚡ 扫码更新凭证
                </button>
                <button class="btn-action-light" style="padding:10px 16px;border-radius:12px" onclick="load(true)">
                  🔍 探测鉴权
                </button>
              </div>
              <div style="font-size:11px;color:var(--mut);margin-top:12px">
                若会话失效或需更换账号，可点击【扫码更新凭证】重新扫码，凭证将自动加密持久化并热注入。
              </div>
            </div>
          </div>
        </div>
      </main>

      <!-- Tab 2: 实时台账 (合二为一 · 运行流水与已处理会话功能叠加) -->
      <main id="tab-ledger" class="tab-content">
        <!-- Top Sub-nav: Filter Pills (每个板块的子导航键放在顶端) -->
        <div class="sub-nav-bar mb-3" id="ledgerSubNav">
          <button type="button" class="sub-nav-pill active" data-filter="all" onclick="filterLedgerChip('all')">
            ✨ 全部动态
          </button>
          <button type="button" class="sub-nav-pill" data-filter="reply" onclick="filterLedgerChip('reply')">
            💬 智能回复
          </button>
          <button type="button" class="sub-nav-pill" data-filter="wechat" onclick="filterLedgerChip('wechat')">
            🟢 交换微信
          </button>
          <button type="button" class="sub-nav-pill" data-filter="resume" onclick="filterLedgerChip('resume')">
            📄 发送简历
          </button>
          <button type="button" class="sub-nav-pill" data-filter="alert" onclick="filterLedgerChip('alert')">
            ⚠️ 人工介入/告警
          </button>
          <button type="button" class="sub-nav-pill" data-filter="scan" onclick="filterLedgerChip('scan')">
            🔍 系统巡检
          </button>
        </div>

        <div class="panel-card">
          <div class="d-flex flex-wrap gap-3 justify-content-between align-items-center mb-3">
            <div>
              <h5 class="fw-bold mb-1 d-flex align-items-center gap-2">
                <svg class="title-icon blue" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
                实时智能台账 · 运行流水与自学习闭环
              </h5>
              <div class="text-muted" style="font-size:12px">流水与对话完整合一：直观查看每条业务流水的双向对话与动作成效，直接打分与输入真人金句实现自学习</div>
            </div>
            <div class="admin-search-group" style="max-width:320px;width:100%">
              <svg class="spotlight-icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
              <input type="text" id="ledgerFilter" class="admin-search-input" placeholder="搜索公司、动作或关键词…" oninput="renderLedger(); toggleSearchClear()">
              <div class="admin-search-clear" id="ledgerFilterClear" onclick="clearLedgerSearch()" title="清空搜索">✕</div>
            </div>
          </div>

          <!-- Unified Event Feed Container -->
          <div id="ledgerFeedContainer">
            <div id="ledgerCardsList"></div>
          </div>

          <!-- Hidden compatibility elements for existing automation tests and handlers -->
          <table class="d-none" id="ledger"><tbody id="ledgerBody"></tbody></table>
          <div class="d-none" id="resolvedList"></div>
          <div class="d-none" id="ledgerEventsView"></div>
          <div class="d-none" id="ledgerResolvedView"></div>
        </div>
      </main>

  <!-- Tab 3: 系统设置 -->
  <main id="tab-settings" class="tab-content">
    <!-- Top Sub-nav Bar (每个板块的子导航键放在顶端) -->
    <div class="sub-nav-bar mb-3" id="settingsSubNav">
      <button type="button" class="sub-nav-pill active" data-sec="all" onclick="scrollSettingsSection('all')">⚙️ 全部设置</button>
      <button type="button" class="sub-nav-pill" data-sec="llm" onclick="scrollSettingsSection('llm')">🤖 大模型LLM</button>
      <button type="button" class="sub-nav-pill" data-sec="prefs" onclick="scrollSettingsSection('prefs')">🎯 求职偏好</button>
      <button type="button" class="sub-nav-pill" data-sec="privacy" onclick="scrollSettingsSection('privacy')">🛡️ 隐私与权限</button>
      <button type="button" class="sub-nav-pill" data-sec="auto" onclick="scrollSettingsSection('auto')">🚀 每日自动投递</button>
      <button type="button" class="sub-nav-pill" data-sec="browser" onclick="scrollSettingsSection('browser')">🌐 浏览器模式</button>
      <button type="button" class="sub-nav-pill" data-sec="resume" onclick="scrollSettingsSection('resume')">📄 简历画像</button>
    </div>
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
                <button type="button" class="btn-action-light text-danger" style="border-color:rgba(239,68,68,0.3);background:rgba(239,68,68,0.06)" onclick="clearApiKey()">清除 API Key</button>
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
              <div class="d-flex align-items-center justify-content-between p-3 mt-3" style="background:#fff;border:1.5px solid #e2e8f0;border-radius:14px">
                <div>
                  <strong style="font-size:13px;color:#111;display:block">🛡️ 全局在线真实回复门禁 (Online Reply Safety Gate)</strong>
                  <span style="font-size:11px;color:var(--mut)">关闭时处于安全沙箱模式（零真实外发，沙盒演练专用）；开启后后台允许真实发送消息</span>
                </div>
                <label class="form-switch-apple">
                  <input type="checkbox" id="inOnlineReply">
                  <span class="switch-slider"></span>
                </label>
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
    <!-- Mobile Sub-nav for Playground (尤其移动端快速切换) -->
    <div class="sub-nav-bar mb-3 d-flex d-lg-none" id="pgMobileSubNav">
      <button type="button" class="sub-nav-pill active" id="pgSubTabChat" onclick="switchPgSubTab('chat')">💬 聊天视窗</button>
      <button type="button" class="sub-nav-pill" id="pgSubTabSettings" onclick="switchPgSubTab('settings')">🎯 岗位与JD设定</button>
      <button type="button" class="sub-nav-pill" id="pgSubTabInspect" onclick="switchPgSubTab('inspect')">🔍 决策与门禁</button>
    </div>

    <div class="row g-4">
      <!-- Left Col: Gemini-style Chat Arena (会话聊天流) -->
      <div class="col-lg-7" id="pgChatCol">
        <div class="chat-arena-card">
          <!-- Chat Header -->
          <div class="chat-arena-header">
            <div class="d-flex align-items-center gap-3">
              <div class="chat-avatar hr" id="chatTargetAvatar">HR</div>
              <div>
                <div style="font-size:15px;font-weight:800;color:var(--txt)">
                  <span id="chatTargetCompany">收钱吧</span>
                  <span style="color:var(--mut-dark);margin:0 4px">·</span>
                  <span id="chatTargetJob" style="color:var(--mut);font-weight:600;font-size:13px">Ai产品经理（J11304）</span>
                </div>
                <div style="font-size:11px;color:var(--ok);font-weight:600;display:flex;align-items:center;gap:4px">
                  <span class="pulse-dot dot-green" style="width:6px;height:6px"></span>
                  沙盒推演就绪 (零物理外发)
                </div>
              </div>
            </div>
            <div class="d-flex align-items-center gap-2">
              <div class="d-none d-sm-flex align-items-center gap-1 me-2 p-1" style="background:#f1f5f9;border:1px solid #e2e8f0;border-radius:10px;font-size:11px;font-weight:700">
                <label class="d-inline-flex align-items-center gap-1 px-2 py-1" style="cursor:pointer;border-radius:8px;background:#fff;color:#0f172a;box-shadow:0 1px 3px rgba(0,0,0,0.05)" id="lblModeSingle">
                  <input type="radio" name="pgMode" id="pgModeSingleTurn" value="single" checked onchange="onPgModeChange()" style="display:none">
                  <span>✨ 首句沟通 (单轮)</span>
                </label>
                <label class="d-inline-flex align-items-center gap-1 px-2 py-1" style="cursor:pointer;border-radius:8px;color:#64748b" id="lblModeMulti">
                  <input type="radio" name="pgMode" id="pgModeMultiTurn" value="multi" onchange="onPgModeChange()" style="display:none">
                  <span>💬 连续推演 (多轮)</span>
                </label>
              </div>
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
              <div style="font-size:12px;color:var(--txt);font-weight:600;margin-top:14px;background:#f1f5f9;padding:8px 14px;border-radius:10px;display:inline-block">
                💡 右侧已预设各行各业名企真实岗位与真实JD（支持自由切换与自定义）。在下方输入模拟 HR 消息即可开始推演！
              </div>
            </div>
          </div>

          <!-- Chat Input Bottom Area -->
          <div class="chat-input-wrapper">
            <!-- Gemini-style Clean Input Box -->
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
      <div class="col-lg-5" id="pgRightCol">
        <!-- Panel 1: 当前沟通背景与岗位设定 (可查看与调整) -->
        <div id="pgSettingsCol">
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
            <div id="pgSettingsBlock" style="display:block">
              <!-- Presets -->
              <div class="mb-3">
                <label style="font-size:11px;font-weight:800;color:#64748b;margin-bottom:4px;display:block">
                  🎯 行业名企真实岗位预设（一键切换各行业）：
                </label>
                <select id="pgIndustrySelect" class="form-select form-select-sm" style="font-size:12px;border-radius:10px" onchange="onSelectIndustryPreset(this.value)">
                  <option value="ai_product" selected>🤖 人工智能 · 收钱吧 · Ai产品经理 (20-35K·14薪)</option>
                  <option value="ev_auto">🚗 智能制造/新能源 · 广志信息 · 智能座舱稳定性测试-沃尔沃 (10-12K)</option>
                  <option value="java_dev">💻 计算机软件/IT · 某大厂 · Java开发工程师 (15-30K·14薪)</option>
                  <option value="fin_quant">📈 金融证券/量化 · 某基金公司 · 期权量化研究员 (15-30K)</option>
                  <option value="cross_border">🌍 跨境电商/出海 · 睿联 · 跨境电商运营27届校招 (11-18K·14薪)</option>
                  <option value="robotics">🦾 具身智能/硬件 · 某知名AI企业 · 机器人运控算法 (100-200K·14薪)</option>
                  <option value="biomed">🧬 生物医药/医疗 · 吃货妞妞 · 生物信息工程师 (5-8K)</option>
                  <option value="custom">✏️ 自定义岗位与JD (手动输入)</option>
                </select>
              </div>

              <div class="row g-2 mb-2">
                <div class="col-6">
                  <label style="margin:0 0 2px">公司名称</label>
                  <input type="text" id="pgCompany" value="收钱吧" style="height:32px;font-size:12px" oninput="syncChatHeader()">
                </div>
                <div class="col-6">
                  <label style="margin:0 0 2px">岗位名称</label>
                  <input type="text" id="pgJobTitle" value="Ai产品经理（J11304）" style="height:32px;font-size:12px" oninput="syncChatHeader()">
                </div>
              </div>
              <div class="row g-2 mb-2">
                <div class="col-6">
                  <label style="margin:0 0 2px">薪资范围</label>
                  <input type="text" id="pgSalary" value="20-35K·14薪" style="height:32px;font-size:12px">
                </div>
                <div class="col-6">
                  <label style="margin:0 0 2px">工作地点</label>
                  <input type="text" id="pgCity" value="上海" style="height:32px;font-size:12px">
                </div>
              </div>
              <div class="mb-2">
                <label style="margin:0 0 2px">岗位 JD 详细描述（来自BOSS直聘真实抓取）</label>
                <textarea id="pgJd" style="height:85px;font-size:11px" placeholder="在此粘贴目标岗位JD…">【岗位职责】
1. AI 应用从 0 到 1 落地：围绕真实业务场景，负责 AI 应用的需求调研、方案设计、技术可行性判断、上线验证与持续迭代；可涉及上下文工程、RAG、Agent 等技术在产品中的应用。
2. 快速验证与产品打磨：能够运用 AI Coding 等方式，亲自完成原型、工作流或 Demo 的快速搭建与调试，验证方案可行性，并与研发团队共同推进正式落地。
3. 跨团队协同与流程建设：熟悉 AI 产品从需求评审、研发排期、测试验收至上线复盘的协作过程，协调产品、研发、算法、设计及业务等角色，高效推动项目交付。
4. 持续探索：持续关注大模型及 AI 应用的新能力，将技术边界转化为可验证、可落地的业务产品方案。
【任职资格】
1. 统招本科及以上学历，具备真实 AI 项目落地能力；了解大模型、RAG、上下文工程、Agent 等常见应用方式。</textarea>
              </div>
              <div class="mb-2">
                <div class="d-flex justify-content-between align-items-center">
                  <label style="margin:0 0 2px">前序对话历史（格式：我方:... 或 HR:...）</label>
                  <button type="button" class="btn-action-light" style="font-size:10px;padding:1px 6px" onclick="document.getElementById('pgHistory').value='';pgConversationHistory=[];">清空历史</button>
                </div>
                <textarea id="pgHistory" style="height:60px;font-size:11px" placeholder="空 = 首轮沟通。多轮沟通示例：&#10;HR: 在吗？&#10;我方: 您好，在的！"></textarea>
              </div>

              <!-- Collapsible Advanced Settings (Prompt overrides) -->
              <div class="d-flex justify-content-between align-items-center mt-2 pt-2" style="border-top:1px dashed #e2e8f0;cursor:pointer" onclick="togglePlaygroundAdv()">
                <span style="font-size:11px;font-weight:700;color:var(--mut)">⚙️ 高级人设与 Prompt 自定义</span>
                <span style="font-size:11px;color:var(--mut)" id="pgAdvArrow">▼ 展开</span>
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
        <div id="pgInspectCol">
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

              <!-- Safety Inspection Gates -->
              <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;padding:14px;margin-bottom:12px;font-size:12px">
                <div style="font-weight:800;color:var(--txt);margin-bottom:10px">🛡️ 4 重物理安全门禁审查清单</div>
                <div class="d-flex flex-column gap-2">
                  <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                    <span>1. 隐私泄露门禁 (防套手机/微信)</span>
                    <span id="pgGatePrivacy" class="soft-badge badge-pub">安全通过</span>
                  </div>
                  <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                    <span>2. 时窗门禁 (00:00-09:30 夜间静默)</span>
                    <span id="pgGateHours" class="soft-badge badge-pub">时窗开放</span>
                  </div>
                  <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                    <span>3. 动作授权策略 (policy_actions)</span>
                    <span id="pgGatePolicy" class="soft-badge badge-pub">允许执行</span>
                  </div>
                  <div class="d-flex justify-content-between align-items-center p-2" style="background:#fff;border-radius:8px">
                    <span>4. 线上发送状态 (沙盒安全隔离)</span>
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
    </div>
  </main>
    </div><!-- app-body -->
  </div><!-- app-main -->
</div><!-- app-layout -->

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
}

function toggleMobileSidebar(force) {
  const sidebar = document.getElementById('appSidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (!sidebar) return;
  const willShow = (typeof force === 'boolean') ? force : !sidebar.classList.contains('show-mobile');
  sidebar.classList.toggle('show-mobile', willShow);
  if (backdrop) backdrop.classList.toggle('show-mobile', willShow);
}

function toggleSidebarCollapse() {
  const sidebar = document.getElementById('appSidebar');
  if (sidebar) {
    sidebar.classList.toggle('collapsed');
    const isCol = sidebar.classList.contains('collapsed');
    try { localStorage.setItem('sidebar_collapsed', isCol ? '1' : '0'); } catch(e) {}
  }
}

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.island-capsule').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === 'tab-' + name);
  });
  const titles = {
    pending: '待办审批 · 决策处理',
    monitor: '运行监控 · 守护进程与环境感知',
    ledger: '实时台账 · 运行流水与自学习',
    settings: '系统设置 · 参数与风控',
    playground: '回复演练场 · 真实沙盒推演'
  };
  const titleEl = document.getElementById('topbarTitle');
  if (titleEl && titles[name]) titleEl.textContent = titles[name];
  toggleMobileSidebar(false);
  if (name === 'ledger') {
    renderResolved();
    renderLedger();
  }
  if (name === 'playground') {
    switchPgSubTab(currentPgSubTab || 'chat');
  }
}

function switchLedgerSubTab(sub) {
  const isEvents = (sub === 'events');
  const evView = document.getElementById('ledgerEventsView');
  const resView = document.getElementById('ledgerResolvedView');
  const btnEv = document.getElementById('subtab-ledger-events');
  const btnRes = document.getElementById('subtab-ledger-resolved');
  if (evView) evView.style.display = isEvents ? 'block' : 'none';
  if (resView) resView.style.display = isEvents ? 'none' : 'block';
  if (btnEv) btnEv.classList.toggle('active', isEvents);
  if (btnRes) btnRes.classList.toggle('active', !isEvents);
  if (!isEvents) {
    renderResolved();
  } else {
    renderLedger();
  }
}

let currentPgSubTab = 'chat';
function switchPgSubTab(sub) {
  currentPgSubTab = sub;
  ['Chat', 'Settings', 'Inspect'].forEach(name => {
    const btn = document.getElementById('pgSubTab' + name);
    if (btn) btn.classList.toggle('active', name.toLowerCase() === sub);
  });
  const chatCol = document.getElementById('pgChatCol');
  const rightCol = document.getElementById('pgRightCol');
  const settingsCol = document.getElementById('pgSettingsCol');
  const inspectCol = document.getElementById('pgInspectCol');

  if (window.innerWidth < 992) {
    if (sub === 'chat') {
      if (chatCol) chatCol.style.display = 'block';
      if (rightCol) rightCol.style.display = 'none';
    } else if (sub === 'settings') {
      if (chatCol) chatCol.style.display = 'none';
      if (rightCol) rightCol.style.display = 'block';
      if (settingsCol) settingsCol.style.display = 'block';
      if (inspectCol) inspectCol.style.display = 'none';
    } else if (sub === 'inspect') {
      if (chatCol) chatCol.style.display = 'none';
      if (rightCol) rightCol.style.display = 'block';
      if (settingsCol) settingsCol.style.display = 'none';
      if (inspectCol) inspectCol.style.display = 'block';
    }
  } else {
    if (chatCol) chatCol.style.display = '';
    if (rightCol) rightCol.style.display = '';
    if (settingsCol) settingsCol.style.display = '';
    if (inspectCol) inspectCol.style.display = '';
  }
}
window.addEventListener('resize', () => {
  if (currentTab === 'playground') switchPgSubTab(currentPgSubTab);
});

function onPgModeChange() {
  const isSingle = document.getElementById('pgModeSingleTurn') && document.getElementById('pgModeSingleTurn').checked;
  const lblSingle = document.getElementById('lblModeSingle');
  const lblMulti = document.getElementById('lblModeMulti');
  if (lblSingle && lblMulti) {
    if (isSingle) {
      lblSingle.style.background = '#fff';
      lblSingle.style.color = '#0f172a';
      lblSingle.style.boxShadow = '0 1px 3px rgba(0,0,0,0.05)';
      lblMulti.style.background = 'transparent';
      lblMulti.style.color = '#64748b';
      lblMulti.style.boxShadow = 'none';
    } else {
      lblMulti.style.background = '#fff';
      lblMulti.style.color = '#0f172a';
      lblMulti.style.boxShadow = '0 1px 3px rgba(0,0,0,0.05)';
      lblSingle.style.background = 'transparent';
      lblSingle.style.color = '#64748b';
      lblSingle.style.boxShadow = 'none';
    }
  }
  if (isSingle) {
    pgConversationHistory = [];
    if (document.getElementById('pgHistory')) document.getElementById('pgHistory').value = '';
    showToast('已切换至「首句沟通」模式：历史记录已重置清空，杜绝前序污染', 'info');
  } else {
    showToast('已切换至「连续推演」模式：多轮对话将累计上下文连续作答', 'info');
  }
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

function triggerApplyNow() {
  const inputTopN = document.getElementById('inApplyTopN') ? parseInt(document.getElementById('inApplyTopN').value) : 0;
  const cachedTopN = (window.__cachedSettings && window.__cachedSettings.auto_apply && window.__cachedSettings.auto_apply.apply_top_n) || 0;
  const topN = inputTopN > 0 ? inputTopN : (cachedTopN > 0 ? cachedTopN : 50);
  showConfirm(
    '⚡ 执行今日智能投递',
    `系统将对已生成的候选岗位计划（按单日上限最多 ${topN} 个，按评分从高到低排序）发起实弹打招呼投递。<br><br><span style="color:var(--dan);font-weight:600">注意：此操作将直接向 BOSS 直聘平台发送针对具体JD定制的打招呼消息并消耗今日投递配额。</span>`,
    async () => {
      showToast('正在执行今日投递计划…', 'info');
      try {
        const res = await api('/api/apply/now', {
          method: 'POST',
          body: JSON.stringify({ mode: 'execute_plan', top_n: topN })
        });
        if (res.ok) {
          const count = (res.result && res.result.executed) || 0;
          showToast(`今日投递完成！已成功投递 ${count} 个岗位`, 'success');
          load(true);
        } else {
          showToast(`投递未完成: ${res.error || (res.result && res.result.error) || '未知错误'}`, 'danger');
        }
      } catch (e) {
        showToast('请求异常: ' + e.message, 'danger');
      }
    }
  );
}

async function load(isManual) {
  if (isManual) showToast('正在刷新工作台数据…', 'info');
  try {
    const d = await api('/api/overview');
    document.getElementById('sub').textContent = '已同步: ' + new Date().toLocaleTimeString();

    // 真实进程与心跳探测
    const gp = document.getElementById('guardPill');
    const sideDot = document.getElementById('sideStatusDot');
    const sideText = document.getElementById('sideStatusText');
    const sidePill = document.getElementById('sideStatusPill');

    const daemon = d.daemon || {};
    const guard = d.guard || {};

    if (guard.paused) {
      if (gp) {
        gp.className = 'soft-badge badge-rej';
        gp.innerHTML = '<span class="pulse-dot dot-red"></span> ⛔ 风控熔断: ' + esc(guard.paused);
      }
      if (sideDot) sideDot.className = 'pulse-dot dot-red';
      if (sideText) sideText.textContent = '风控熔断停摆';
      if (sidePill) sidePill.style.borderColor = 'rgba(239,68,68,0.3)';
    } else if (!daemon.running) {
      if (gp) {
        gp.className = 'soft-badge badge-rej';
        gp.innerHTML = '<span class="pulse-dot dot-red"></span> 🔴 守护未运行 (后台进程离线)';
      }
      if (sideDot) sideDot.className = 'pulse-dot dot-red';
      if (sideText) sideText.textContent = '守护进程离线';
      if (sidePill) sidePill.style.borderColor = 'rgba(239,68,68,0.3)';
    } else {
      if (gp) {
        gp.className = 'soft-badge badge-pub';
        const stText = daemon.status === 'sleeping' ? '休眠巡检中' : '守护运行中';
        gp.innerHTML = `<span class="pulse-dot dot-green"></span> 🟢 ${stText} (PID ${daemon.pid || '已就绪'})`;
      }
      if (sideDot) sideDot.className = 'pulse-dot dot-green';
      if (sideText) sideText.textContent = daemon.status === 'sleeping' ? '休眠巡检中' : '守护运行中';
      if (sidePill) sidePill.style.borderColor = '';
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

    // 守护进程与运行监控 UI 联动
    window.__currentDaemon = daemon;
    const dPid = document.getElementById('daemonPid');
    const dStatus = document.getElementById('daemonStatusText');
    const dLast = document.getElementById('daemonLastSeen');
    const dDetails = document.getElementById('daemonDetails');
    const dBtn = document.getElementById('btnDaemonToggle');
    const dSideBadge = document.getElementById('sideDaemonBadge');
    const dLiveBadge = document.getElementById('daemonLiveBadge');

    if (dPid) dPid.textContent = daemon.pid ? `PID ${daemon.pid}` : '未运行';
    if (dStatus) {
      if (daemon.running) {
        dStatus.textContent = daemon.status === 'sleeping' ? '🟢 休眠巡检中' : '🟢 常驻运行中';
        dStatus.style.color = '#10b981';
      } else {
        dStatus.textContent = '🔴 离线未运行';
        dStatus.style.color = '#ef4444';
      }
    }
    if (dLast) dLast.textContent = (daemon.last_seen_seconds !== undefined) ? `${daemon.last_seen_seconds} 秒前 (${daemon.time || ''})` : '无心跳记录';
    if (dDetails) dDetails.textContent = daemon.details ? JSON.stringify(daemon.details) : '无详细数据';

    if (dBtn) {
      if (daemon.running) {
        dBtn.className = 'btn-action-light text-danger';
        dBtn.innerHTML = '⏹️ 停止守护进程';
        dBtn.onclick = () => handleDaemonToggle('stop');
      } else {
        dBtn.className = 'btn-black';
        dBtn.innerHTML = '▶️ 一键启动常驻守护';
        dBtn.onclick = () => handleDaemonToggle('start');
      }
    }
    if (dSideBadge) {
      dSideBadge.style.display = daemon.running ? 'inline-block' : 'none';
      dSideBadge.textContent = daemon.running ? (daemon.status === 'sleeping' ? 'SLEEP' : 'ON') : 'OFF';
      dSideBadge.style.background = daemon.running ? '#10b981' : '#94a3b8';
    }
    if (dLiveBadge) {
      dLiveBadge.textContent = daemon.running ? '常驻运行中' : '已离线';
      dLiveBadge.className = daemon.running ? 'soft-badge badge-pub' : 'soft-badge badge-rej';
    }

    // BOSS 鉴权状态联动
    const auth = d.auth || {};
    const userProfile = d.user_profile || (auth && auth.user_profile) || {};
    updateAuthStatusUI(auth, userProfile);

    renderPending();
    renderResolved();
    renderLedger();
    loadSettings();
    if (isManual) showToast('工作台数据已更新！', 'success');
  } catch(e) {
    console.error(e);
  }
}

let lastLoginState = null;
let loginGateDismissed = false;

function updateAuthStatusUI(auth, userProfile) {
  if (!auth) return;
  const btn = document.getElementById('btnBossAuth');
  const dot = document.getElementById('authDot');
  const txt = document.getElementById('authBtnText');
  const topAvatar = document.getElementById('topUserAvatar');
  const topAvatarPlaceholder = document.getElementById('topUserAvatarPlaceholder');
  const topName = document.getElementById('topUserName');
  const loginGate = document.getElementById('loginGate');
  const welcomeBanner = document.getElementById('welcomeBanner');

  const pName = (userProfile && userProfile.name) || '张烨韬';
  const pAvatar = (userProfile && userProfile.avatar) || '';
  const pSchool = (userProfile && userProfile.school) || '福建师范大学';
  const pMajor = (userProfile && userProfile.major) || '数字媒体技术';
  const pGrad = (userProfile && userProfile.grad_year) || '2027届';
  const pCity = (userProfile && userProfile.current_city) || '福州市';
  const pStatus = (userProfile && userProfile.status_desc) || '在校可实习';
  const pSynced = (userProfile && userProfile.synced_at) || '已对齐底座';

  if (auth.logged_in) {
    if (dot) dot.className = 'pulse-dot dot-green';
    if (txt) txt.textContent = '已接入 BOSS';
    if (topName) topName.textContent = pName;
    if (btn) {
      btn.style.borderColor = 'rgba(16,185,129,0.4)';
      btn.title = `BOSS已登录 · 候选人: ${pName} · 点击管理画像`;
    }
    if (pAvatar && topAvatar) {
      topAvatar.src = pAvatar;
      topAvatar.style.display = 'block';
      if (topAvatarPlaceholder) topAvatarPlaceholder.style.display = 'none';
    } else {
      if (topAvatar) topAvatar.style.display = 'none';
      if (topAvatarPlaceholder) {
        topAvatarPlaceholder.textContent = pName ? pName.slice(0, 1) : '张';
        topAvatarPlaceholder.style.display = 'inline-block';
      }
    }

    // 关闭扫码门禁
    if (loginGate) {
      loginGate.style.display = 'none';
    }

    // 若此前处于未登录，现在转为已登录，展示欢迎横幅
    if (lastLoginState === false && welcomeBanner) {
      showWelcomeBanner(userProfile);
    }
    lastLoginState = true;
  } else {
    if (dot) dot.className = 'pulse-dot dot-red';
    if (txt) txt.textContent = '未登录 BOSS';
    if (topName) topName.textContent = '未连接';
    if (btn) {
      btn.style.borderColor = 'rgba(239,68,68,0.3)';
      btn.title = '未检测到BOSS登录凭证 · 点击扫码接入';
    }
    if (topAvatar) topAvatar.style.display = 'none';
    if (topAvatarPlaceholder) {
      topAvatarPlaceholder.textContent = '未';
      topAvatarPlaceholder.style.display = 'inline-block';
    }

    // 未登录时，若未被手动跳过，展示居中全屏扫码门禁
    if (loginGate && !loginGateDismissed) {
      loginGate.style.display = 'flex';
      if (!currentQrUuid && !qrPollTimer) {
        refreshGateQrCode();
      }
    }
    lastLoginState = false;
  }

  // 填充下拉卡片信息
  const popName = document.getElementById('popoverName');
  const popGrade = document.getElementById('popoverGrade');
  const popStatus = document.getElementById('popoverStatus');
  const popSchool = document.getElementById('popoverSchool');
  const popMajor = document.getElementById('popoverMajor');
  const popCity = document.getElementById('popoverCity');
  const popSync = document.getElementById('popoverSyncTime');
  const popAvatar = document.getElementById('popoverAvatar');
  const popAvatarPlaceholder = document.getElementById('popoverAvatarPlaceholder');

  if (popName) popName.textContent = pName;
  if (popGrade) popGrade.textContent = pGrad;
  if (popStatus) popStatus.textContent = auth.logged_in ? `已连接BOSS · ${pStatus}` : '未连接 · 点击下方扫码登录';
  if (popSchool) popSchool.textContent = pSchool;
  if (popMajor) popMajor.textContent = pMajor;
  if (popCity) popCity.textContent = pCity;
  if (popSync) popSync.textContent = pSynced || '刚刚';

  if (pAvatar && popAvatar) {
    popAvatar.src = pAvatar;
    popAvatar.style.display = 'block';
    if (popAvatarPlaceholder) popAvatarPlaceholder.style.display = 'none';
  } else {
    if (popAvatar) popAvatar.style.display = 'none';
    if (popAvatarPlaceholder) {
      popAvatarPlaceholder.textContent = pName ? pName.slice(0, 1) : '张';
      popAvatarPlaceholder.style.display = 'inline-block';
    }
  }

  // 监控面板中的鉴权卡片
  const cdpTxt = document.getElementById('cdpStatusText');
  const bossTxt = document.getElementById('bossLoginText');
  const cookiesTxt = document.getElementById('cookiesCountText');
  const dpapiTxt = document.getElementById('dpapiStatusText');
  const authLive = document.getElementById('authLiveBadge');

  if (cdpTxt) {
    cdpTxt.textContent = auth.cdp_connected ? '🟢 端口已连接 (9335)' : '🔴 端口未连接 (9335)';
    cdpTxt.style.color = auth.cdp_connected ? '#10b981' : '#ef4444';
  }
  if (bossTxt) {
    bossTxt.textContent = auth.logged_in ? `🟢 已登录 (${auth.wt2_masked || '凭证有效'})` : '🟡 未登录 (需扫码)';
    bossTxt.style.color = auth.logged_in ? '#10b981' : '#f59e0b';
  }
  if (cookiesTxt) {
    cookiesTxt.textContent = `已加载 ${auth.cookies_count || 0} 条 Cookie`;
  }
  if (dpapiTxt) {
    dpapiTxt.textContent = auth.has_persisted ? '🔒 已安全加密落盘 (DPAPI)' : '⚠️ 尚未加密落盘';
    dpapiTxt.style.color = auth.has_persisted ? '#059669' : '#64748b';
  }
  if (authLive) {
    authLive.textContent = auth.logged_in ? '已就绪' : '未登录';
    authLive.className = auth.logged_in ? 'soft-badge badge-pub' : 'soft-badge badge-rej';
  }
}

function showWelcomeBanner(userProfile) {
  const wb = document.getElementById('welcomeBanner');
  if (!wb) return;
  const pName = (userProfile && userProfile.name) || '张烨韬';
  const pAvatar = (userProfile && userProfile.avatar) || '';
  const pSchool = (userProfile && userProfile.school) || '福建师范大学';
  const pMajor = (userProfile && userProfile.major) || '数字媒体技术';
  const pGrad = (userProfile && userProfile.grad_year) || '2027届';

  const wName = document.getElementById('welcomeName');
  const wDetail = document.getElementById('welcomeDetail');
  const wAvatar = document.getElementById('welcomeAvatar');
  const wPlaceholder = document.getElementById('welcomeAvatarPlaceholder');

  if (wName) wName.textContent = pName;
  if (wDetail) wDetail.textContent = `${pSchool} · ${pMajor} · ${pGrad} · 已与求职底座完成画像对齐`;

  if (pAvatar && wAvatar) {
    wAvatar.src = pAvatar;
    wAvatar.style.display = 'block';
    if (wPlaceholder) wPlaceholder.style.display = 'none';
  } else {
    if (wAvatar) wAvatar.style.display = 'none';
    if (wPlaceholder) {
      wPlaceholder.textContent = pName.slice(0, 1) || '张';
      wPlaceholder.style.display = 'flex';
    }
  }
  wb.style.display = 'flex';
}

function dismissWelcomeBanner() {
  const wb = document.getElementById('welcomeBanner');
  if (wb) wb.style.display = 'none';
}

function toggleProfileDropdown(e) {
  if (e) e.stopPropagation();
  const dd = document.getElementById('profileDropdown');
  if (!dd) return;
  dd.style.display = dd.style.display === 'none' || !dd.style.display ? 'block' : 'none';
}

document.addEventListener('click', (e) => {
  const wrap = document.getElementById('userProfileWrap');
  const dd = document.getElementById('profileDropdown');
  if (wrap && dd && !wrap.contains(e.target)) {
    dd.style.display = 'none';
  }
});

async function syncBossProfile() {
  showToast('正在从 BOSS 直聘同步真实个人画像与头像…', 'info');
  try {
    const res = await fetch('/api/auth/profile/sync?token=' + encodeURIComponent(TOKEN), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'}
    });
    const d = await res.json();
    if (d.ok && d.user_profile) {
      showToast(`🎉 个人资料同步完成！候选人：${d.user_profile.name} (${d.user_profile.school})`, 'success');
      load(false);
    } else {
      showToast('同步失败: ' + (d.error || '未能抓取到个人资料'), 'error');
    }
  } catch (e) {
    showToast('网络请求失败: ' + e.message, 'error');
  }
}

function dismissLoginGate() {
  loginGateDismissed = true;
  const gate = document.getElementById('loginGate');
  if (gate) gate.style.display = 'none';
  showToast('已进入访客预览模式，可随时点击右上角【扫码接入】登录', 'info');
}

async function refreshGateQrCode() {
  const spinner = document.getElementById('gateQrSpinner');
  const img = document.getElementById('gateQrImg');
  const mask = document.getElementById('gateQrMask');
  const statusText = document.getElementById('gateQrStatus');
  const countdownEl = document.getElementById('gateQrCountdown');

  if (spinner) spinner.style.display = 'flex';
  if (img) img.style.display = 'none';
  if (mask) mask.style.display = 'none';
  if (statusText) {
    statusText.innerHTML = '正在与 Chrome CDP 同步原生二维码…';
    statusText.style.color = '#0284c7';
  }
  await refreshQrCode();
}

async function handleDaemonToggle(action) {
  if (!action) {
    const isRunning = window.__currentDaemon && window.__currentDaemon.running;
    action = isRunning ? 'stop' : 'start';
  }
  showToast(action === 'start' ? '正在拉起常驻守护进程…' : '正在停止守护进程…', 'info');
  try {
    const res = await fetch('/api/daemon/toggle?token=' + encodeURIComponent(TOKEN), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action})
    });
    const d = await res.json();
    if (d.ok) {
      showToast(d.message || (action === 'start' ? '守护进程已启动' : '守护进程已停止'), 'success');
      setTimeout(() => load(false), 800);
    } else {
      showToast('操作失败: ' + (d.error || d.message || '未知错误'), 'error');
    }
  } catch (e) {
    showToast('网络请求失败: ' + e.message, 'error');
  }
}

async function clearApiKey() {
  if (!confirm("⚠️ 确认清除：确定要从 Windows DPAPI 安全存储中彻底清除 API Key 吗？清除后系统将回退到环境变量或提示未配置。")) {
    return;
  }
  showToast("正在清除 DPAPI 密钥…", "info");
  try {
    const res = await fetch("/api/auth/clear_key?token=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: "llm_api_key"})
    });
    const d = await res.json();
    if (d.ok) {
      showToast("DPAPI 密钥已清除！", "success");
      const inKey = document.getElementById("inKey");
      if (inKey) inKey.value = "";
      loadSettings();
    } else {
      showToast("清除失败: " + (d.error || "未知错误"), "error");
    }
  } catch (e) {
    showToast("网络请求失败: " + e.message, "error");
  }
}

let qrPollTimer = null;
let qrCountdownTimer = null;
let currentQrUuid = null;

async function openQrModal() {
  const modal = document.getElementById('qrModal');
  if (modal) modal.classList.add('active');
  await refreshQrCode();
}

function closeQrModal() {
  const modal = document.getElementById('qrModal');
  if (modal) modal.classList.remove('active');
  stopQrPolling();
}

function stopQrPolling() {
  if (qrPollTimer) {
    clearInterval(qrPollTimer);
    qrPollTimer = null;
  }
  if (qrCountdownTimer) {
    clearInterval(qrCountdownTimer);
    qrCountdownTimer = null;
  }
}

async function refreshQrCode() {
  stopQrPolling();
  const spinner = document.getElementById('qrSpinner');
  const img = document.getElementById('qrImg');
  const mask = document.getElementById('qrMask');
  const statusText = document.getElementById('qrStatusText');
  const countdownEl = document.getElementById('qrCountdown');

  const gSpinner = document.getElementById('gateQrSpinner');
  const gImg = document.getElementById('gateQrImg');
  const gMask = document.getElementById('gateQrMask');
  const gStatusText = document.getElementById('gateQrStatus');
  const gCountdownEl = document.getElementById('gateQrCountdown');

  if (spinner) spinner.style.display = 'flex';
  if (img) img.style.display = 'none';
  if (mask) mask.style.display = 'none';
  if (statusText) {
    statusText.textContent = '正在通过 Chrome CDP 捕获原生二维码…';
    statusText.style.color = '#2563eb';
  }

  if (gSpinner) gSpinner.style.display = 'flex';
  if (gImg) gImg.style.display = 'none';
  if (gMask) gMask.style.display = 'none';
  if (gStatusText) {
    gStatusText.textContent = '正在与 Chrome CDP 同步原生二维码…';
    gStatusText.style.color = '#0284c7';
  }

  try {
    const res = await fetch('/api/auth/qrcode/get?token=' + encodeURIComponent(TOKEN));
    const d = await res.json();
    if (d.ok && d.qrcode_base64 && d.uuid) {
      currentQrUuid = d.uuid;
      if (spinner) spinner.style.display = 'none';
      if (img) {
        img.src = d.qrcode_base64;
        img.style.display = 'block';
      }
      if (statusText) {
        statusText.textContent = '请打开手机 BOSS直聘 App 扫描上方二维码';
        statusText.style.color = '#2563eb';
      }

      if (gSpinner) gSpinner.style.display = 'none';
      if (gImg) {
        gImg.src = d.qrcode_base64;
        gImg.style.display = 'block';
      }
      if (gStatusText) {
        gStatusText.textContent = '请打开手机 BOSS直聘 App 扫码登录';
        gStatusText.style.color = '#0284c7';
      }

      let remain = d.expire_seconds || 180;
      if (countdownEl) countdownEl.textContent = remain;
      if (gCountdownEl) gCountdownEl.textContent = remain;

      qrCountdownTimer = setInterval(() => {
        remain--;
        const val = Math.max(0, remain);
        if (countdownEl) countdownEl.textContent = val;
        if (gCountdownEl) gCountdownEl.textContent = val;
        if (remain <= 0) {
          stopQrPolling();
          if (mask) mask.style.display = 'flex';
          if (gMask) gMask.style.display = 'flex';
          if (statusText) {
            statusText.textContent = '二维码已超时失效，请点击刷新';
            statusText.style.color = '#ef4444';
          }
          if (gStatusText) {
            gStatusText.textContent = '二维码已超时失效，请点击刷新';
            gStatusText.style.color = '#ef4444';
          }
        }
      }, 1000);

      startQrPolling(d.uuid);
    } else {
      if (spinner) spinner.style.display = 'none';
      if (gSpinner) gSpinner.style.display = 'none';
      const errMsg = '获取失败: ' + (d.error || '未能连接 Chrome');
      if (statusText) {
        statusText.textContent = errMsg;
        statusText.style.color = '#ef4444';
      }
      if (gStatusText) {
        gStatusText.textContent = errMsg;
        gStatusText.style.color = '#ef4444';
      }
      if (mask) mask.style.display = 'flex';
      if (gMask) gMask.style.display = 'flex';
    }
  } catch (e) {
    if (spinner) spinner.style.display = 'none';
    if (gSpinner) gSpinner.style.display = 'none';
    const errText = '网络错误: ' + e.message;
    if (statusText) {
      statusText.textContent = errText;
      statusText.style.color = '#ef4444';
    }
    if (gStatusText) {
      gStatusText.textContent = errText;
      gStatusText.style.color = '#ef4444';
    }
    if (mask) mask.style.display = 'flex';
    if (gMask) gMask.style.display = 'flex';
  }
}

function startQrPolling(uuid) {
  qrPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/auth/qrcode/status?uuid=${encodeURIComponent(uuid)}&token=${encodeURIComponent(TOKEN)}`);
      const d = await res.json();
      const statusText = document.getElementById('qrStatusText');
      const mask = document.getElementById('qrMask');
      const gStatusText = document.getElementById('gateQrStatus');
      const gMask = document.getElementById('gateQrMask');
      const gate = document.getElementById('loginGate');

      if (d.status === 'confirmed') {
        stopQrPolling();
        loginGateDismissed = false;
        const succMsg = '🎉 ' + (d.message || '扫码登录成功！已自动热生效');
        if (statusText) {
          statusText.textContent = succMsg;
          statusText.style.color = '#10b981';
        }
        if (gStatusText) {
          gStatusText.textContent = succMsg;
          gStatusText.style.color = '#10b981';
        }
        showToast('🎉 BOSS 直聘扫码登录成功！凭证已热注入生效！', 'success');
        if (gate) {
          gate.style.opacity = '0';
          setTimeout(() => { gate.style.display = 'none'; gate.style.opacity = '1'; }, 300);
        }
        setTimeout(() => {
          closeQrModal();
          load(true);
          if (d.user_profile) {
            showWelcomeBanner(d.user_profile);
          }
        }, 1000);
      } else if (d.status === 'scanned') {
        const scanMsg = '📱 手机已扫描，请在 BOSS直聘 App 点击【确认登录】…';
        if (statusText) {
          statusText.textContent = scanMsg;
          statusText.style.color = '#f59e0b';
        }
        if (gStatusText) {
          gStatusText.textContent = scanMsg;
          gStatusText.style.color = '#f59e0b';
        }
      } else if (d.status === 'expired') {
        stopQrPolling();
        if (mask) mask.style.display = 'flex';
        if (gMask) gMask.style.display = 'flex';
        const expMsg = '二维码已失效，请重新刷新';
        if (statusText) {
          statusText.textContent = expMsg;
          statusText.style.color = '#ef4444';
        }
        if (gStatusText) {
          gStatusText.textContent = expMsg;
          gStatusText.style.color = '#ef4444';
        }
      }
    } catch (e) {
      console.warn('QR polling error:', e);
    }
  }, 2000);
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
}

function renderResolved() {
  // 渲染近期已处理会话（点评打分 1-10 与真人优化示范自学习闭环）
  const rb = document.getElementById('resolvedList');
  if (rb) {
    if (!resolvedData.length) {
      rb.innerHTML = '<div style="color:var(--mut-dark);font-size:12px;padding:4px 0">暂无近期处理记录</div>';
    } else {
      rb.innerHTML = resolvedData.map((r, i) => {
        const curScore = r.score || 8;
        const hrText = r.last_msg || '（历史会话未记录到单句正文，记录已完成归档）';
        const hrSection = `
          <div class="quote-box py-2 px-3 mb-2" style="background:#f1f5f9;border-left:4px solid #64748b;border-radius:8px;font-size:12.5px">
            <div style="font-size:11px;font-weight:800;color:#475569;margin-bottom:3px;display:flex;align-items:center;gap:4px">
              💬 对方发送内容 (HR)
            </div>
            <div style="color:#1e293b;line-height:1.5;white-space:pre-wrap">${esc(hrText)}</div>
          </div>`;
        const aiText = r.suggested || ('【系统动作】已执行平台动作（' + (r.resolved_action || '完成') + '）');
        const aiSection = `
          <div class="quote-box py-2 px-3 mb-3" style="background:#ecfdf5;border-left:4px solid #10b981;border-radius:8px;font-size:12.5px">
            <div style="font-size:11px;font-weight:800;color:#047857;margin-bottom:3px;display:flex;align-items:center;gap:4px">
              🤖 Agent 实际回复内容
            </div>
            <div style="color:#064e3b;font-weight:500;line-height:1.5;white-space:pre-wrap">${esc(aiText)}</div>
          </div>`;
        const pills = [1,2,3,4,5,6,7,8,9,10].map(num => `
          <button type="button" class="btn-score-pill ${curScore === num ? 'active' : ''}" onclick="selectResolvedScore(${i}, ${num})">${num}</button>
        `).join('');

        return `
          <div class="resolved-card p-3 mb-3">
            <div class="d-flex justify-content-between align-items-center mb-2">
              <div>
                <strong style="font-size:14px;color:var(--txt)">${esc(r.company)}</strong>
                <span class="soft-badge badge-pub ms-2" style="font-size:11px">${esc(r.resolved_action || 'handled')}</span>
              </div>
              <div style="font-size:12px;color:var(--mut-dark)">${esc(r.resolved_time || r.time || '')}</div>
            </div>
            ${hrSection}
            ${aiSection}
            <div class="pt-2" style="border-top:1px dashed rgba(0,0,0,0.06)">
              <div class="d-flex justify-content-between align-items-center mb-2">
                <div style="font-size:12px;font-weight:700;color:var(--txt);display:flex;align-items:center;gap:6px">
                  ⭐ 回答点评打分（满分10分，未打分按默认8分计算）:
                  <span id="scoreBadge_${i}" class="soft-badge badge-ai" style="font-size:11px;font-weight:800">${curScore}分</span>
                </div>
                <div style="font-size:11px;color:var(--mut)">点击数字切换评分</div>
              </div>
              <div class="d-flex flex-wrap gap-1 mb-3" id="scorePills_${i}">
                ${pills}
              </div>
              <div class="mb-2">
                <label style="font-size:12px;font-weight:700;color:var(--txt);margin-bottom:4px;display:block">
                  ✍️ 人工输入优化内容（真人示范金句，自动沉淀至经验库实现自学习进化）：
                </label>
                <textarea id="optText_${i}" class="form-control" style="font-size:12px;border-radius:10px;resize:vertical;min-height:56px" placeholder="输入你认为更自然、更高情商的真人回复（例如：“真人在的，刚才回复太板正了哈哈…”），大模型在后续会话中将自动检索参考此黄金样本。">${esc(r.optimized_text || '')}</textarea>
              </div>
              <div class="d-flex justify-content-between align-items-center mt-2">
                <span id="fbStatus_${i}" style="font-size:12px;color:var(--mut)">${r.has_feedback ? '✅ 已有历史评价记录' : '未手动点评（默认8分）'}</span>
                <button class="btn-black btn-sm" style="padding:6px 14px;border-radius:10px;font-size:12px" onclick="saveResolvedFeedback(${i})">
                  💾 保存点评与优化
                </button>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }
  }
}

function selectResolvedScore(idx, score) {
  if (!resolvedData || !resolvedData[idx]) return;
  resolvedData[idx].score = score;
  const badge = document.getElementById('scoreBadge_' + idx);
  if (badge) badge.textContent = score + '分';
  const container = document.getElementById('scorePills_' + idx);
  if (container) {
    container.querySelectorAll('.btn-score-pill').forEach((btn, i) => {
      btn.classList.toggle('active', (i + 1) === score);
    });
  }
}

async function saveResolvedFeedback(idx) {
  if (!resolvedData || !resolvedData[idx]) return;
  const item = resolvedData[idx];
  const optBox = document.getElementById('optText_' + idx);
  const optText = optBox ? optBox.value.trim() : '';
  const score = item.score || 8;
  const statusEl = document.getElementById('fbStatus_' + idx);

  if (statusEl) statusEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>保存中…';

  const payload = {
    company: item.company,
    job: item.job || '',
    hr_msg: item.last_msg || '',
    ai_reply: item.suggested || '',
    score: score,
    optimized_text: optText,
    source: 'web_console'
  };

  try {
    const res = await fetch('/api/feedback?token=' + encodeURIComponent(TOKEN), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.ok) {
      showToast('点评与优化已存入经验库！大模型后续会话将自学习参考', 'success');
      item.has_feedback = true;
      item.optimized_text = optText;
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--ok)">✅ 点评已入库（' + score + '分）</span>';
    } else {
      showToast('保存失败: ' + (data.error || '未知错误'), 'error');
      if (statusEl) statusEl.textContent = '❌ 保存失败';
    }
  } catch (e) {
    showToast('网络错误: ' + e.message, 'error');
    if (statusEl) statusEl.textContent = '❌ 网络异常';
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
  document.querySelectorAll('#ledgerSubNav .sub-nav-pill, .filter-pill').forEach(p => {
    p.classList.toggle('active', (p.dataset.filter === filterVal || p.getAttribute('data-filter') === filterVal));
  });
  renderLedger();
}

function selectLedgerScore(idx, score) {
  if (!fullLedger || !fullLedger[idx]) return;
  fullLedger[idx].score = score;
  const badge = document.getElementById('ledgerScoreBadge_' + idx);
  if (badge) badge.textContent = score + '分';
  const container = document.getElementById('ledgerScorePills_' + idx);
  if (container) {
    container.querySelectorAll('.btn-score-pill').forEach((btn, i) => {
      btn.classList.toggle('active', (i + 1) === score);
    });
  }
}

async function saveLedgerFeedback(idx) {
  if (!fullLedger || !fullLedger[idx]) return;
  const item = fullLedger[idx];
  const optBox = document.getElementById('ledgerOptText_' + idx);
  const optText = optBox ? optBox.value.trim() : '';
  const score = item.score || 8;
  const statusEl = document.getElementById('ledgerFbStatus_' + idx);

  if (statusEl) statusEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>保存中…';

  const payload = {
    company: item.company,
    job: item.job || '',
    hr_msg: item.last_msg || '',
    ai_reply: item.suggested || ('【系统动作】' + (item.action || '完成')),
    score: score,
    optimized_text: optText,
    source: 'web_console_unified_ledger'
  };

  try {
    const res = await fetch('/api/feedback?token=' + encodeURIComponent(TOKEN), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.ok) {
      showToast('点评与优化已存入经验库！大模型后续会话将自学习参考', 'success');
      item.has_feedback = true;
      item.optimized_text = optText;
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--ok);font-weight:700">✅ 点评已入库（' + score + '分）</span>';
    } else {
      showToast('保存失败: ' + (data.error || '未知错误'), 'error');
      if (statusEl) statusEl.textContent = '❌ 保存失败';
    }
  } catch (e) {
    showToast('网络错误: ' + e.message, 'error');
    if (statusEl) statusEl.textContent = '❌ 网络异常';
  }
}

function scrollSettingsSection(sec) {
  document.querySelectorAll('#settingsSubNav .sub-nav-pill').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-sec') === sec);
  });
  if (sec === 'all') {
    const box = document.getElementById('settingsBox');
    if (box) box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }
  const elMap = {
    llm: document.getElementById('inKey'),
    prefs: document.getElementById('inJobMode'),
    privacy: document.getElementById('inPolicyWechat'),
    auto: document.getElementById('inAutoApplyEnabled'),
    browser: document.getElementById('inBrowserSilent'),
    resume: document.getElementById('profMeta')
  };
  const target = elMap[sec];
  if (target) {
    const card = target.closest('.panel-card') || target;
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function renderLedger() {
  const cardsList = document.getElementById('ledgerCardsList');
  const tbody = document.getElementById('ledgerBody');
  const kw = (document.getElementById('ledgerFilter') ? document.getElementById('ledgerFilter').value : '').trim().toLowerCase();
  
  const filtered = fullLedger.filter(r => {
    if (ledgerFilter !== 'all') {
      if (ledgerFilter === 'wechat' && !r.action.includes('wechat')) return false;
      else if (ledgerFilter === 'resume' && !r.action.includes('resume')) return false;
      else if (ledgerFilter === 'reply' && r.action !== 'reply') return false;
      else if (ledgerFilter === 'alert' && !r.action.includes('alert') && r.action !== 'human_alert_card') return false;
      else if (ledgerFilter === 'gate' && !r.action.includes('gate')) return false;
      else if (ledgerFilter === 'scan' && r.action !== 'daemon_cycle_summary' && r.action !== 'scan') return false;
    }
    if (kw) {
      const line = (r.ts + ' ' + r.action + ' ' + (r.company || '') + ' ' + (r.status || '') + ' ' + (r.last_msg || '') + ' ' + (r.suggested || '')).toLowerCase();
      if (!line.includes(kw)) return false;
    }
    return true;
  });

  // 1. 同步填充隐藏表格 (保留对旧测试/选择器的完全向下兼容)
  if (tbody) {
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center py-5 text-muted" style="font-size:13px">无匹配台账流水</td></tr>';
    } else {
      tbody.innerHTML = filtered.slice(0, 50).map(r => {
        let badgeCls = 'badge-blue';
        let actName = r.action;
        if (r.action === 'reply') { badgeCls = 'badge-pub'; actName = '智能回复'; }
        else if (r.action.includes('wechat')) { badgeCls = 'badge-pub'; actName = '交换微信'; }
        else if (r.action.includes('resume')) { badgeCls = 'badge-purple'; actName = '发送简历'; }
        else if (r.action.includes('alert') || r.action === 'human_alert_card') { badgeCls = 'badge-rej'; actName = '转人工告警'; }
        else if (r.action.includes('gate')) { badgeCls = 'badge-ai'; actName = '时间/门禁拦截'; }
        else if (r.action === 'daemon_cycle_summary') { badgeCls = 'badge-blue'; actName = '系统巡检'; }
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
  }

  // 2. 渲染合二为一的现代卡片时间线 (Unified Smart Timeline Cards)
  if (!cardsList) return;
  if (!filtered.length) {
    cardsList.innerHTML = '<div class="text-center py-5 text-muted" style="font-size:13px">暂无匹配的台账流水记录</div>';
    return;
  }

  cardsList.innerHTML = filtered.slice(0, 60).map((r, i) => {
    // 2.1 系统巡检事件轻量化呈现
    if (r.action === 'daemon_cycle_summary') {
      const scanned = r.scanned ?? (r.raw && r.raw.scanned) ?? '-';
      const candidates = r.candidates ?? (r.raw && r.raw.candidates) ?? '-';
      const replied = r.replied ?? (r.raw && r.raw.replied) ?? '-';
      const isDry = r.dry_run ?? (r.raw && r.raw.dry_run);
      return `
        <div class="ledger-cycle-card p-2 px-3 mb-2" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px">
          <div class="d-flex align-items-center gap-2">
            <span class="soft-badge badge-blue" style="font-size:10px;font-weight:800">🔍 雷达巡检</span>
            <span style="color:#334155;font-weight:600;font-size:12px">
              扫描会话 <strong>${scanned}</strong> 个 · 意向匹配 <strong>${candidates}</strong> 个 · 实际回复 <strong>${replied}</strong> 个
            </span>
            ${isDry ? '<span class="soft-badge badge-warn" style="font-size:9.5px;padding:2px 6px">演练模式</span>' : '<span class="soft-badge badge-pub" style="font-size:9.5px;padding:2px 6px">线上实弹</span>'}
          </div>
          <span style="color:var(--mut-dark);font-size:11px;font-weight:500">${esc(r.ts)}</span>
        </div>
      `;
    }

    // 2.2 业务会话与动作流水：富卡片呈现（HR原文 + Agent回复 + 1-10分打分 + 真人金句输入）
    let badgeCls = 'badge-blue';
    let actName = r.action;
    if (r.action === 'reply') { badgeCls = 'badge-pub'; actName = '💬 智能回复'; }
    else if (r.action.includes('wechat')) { badgeCls = 'badge-pub'; actName = '🟢 交换微信'; }
    else if (r.action.includes('resume')) { badgeCls = 'badge-purple'; actName = '📄 发送简历'; }
    else if (r.action.includes('alert') || r.action === 'human_alert_card') { badgeCls = 'badge-rej'; actName = '⚠️ 转人工待办'; }
    else if (r.action.includes('gate')) { badgeCls = 'badge-ai'; actName = '🛡️ 门禁拦截'; }
    else if (r.action === 'daemon_skip') { badgeCls = 'badge-blue'; actName = '⏭️ 规则跳过'; }

    let stBadgeCls = 'badge-blue';
    let st = r.status || '';
    if (st === 'ok') { stBadgeCls = 'badge-pub'; st = '✅ 执行成功'; }
    else if (st === 'already_sent' || st === 'already_agreed') { stBadgeCls = 'badge-blue'; st = 'ℹ️ 已发起过(无需重发)'; }
    else if (st.includes('blocked') || st.includes('fail')) { stBadgeCls = 'badge-rej'; }

    const curScore = r.score || 8;
    const hrText = r.last_msg || '';
    const aiText = r.suggested || ('【系统动作】已执行平台动作（' + actName + ' · ' + (r.status || '完成') + '）');

    const hrSection = hrText ? `
      <div class="quote-box py-2 px-3 mb-2" style="background:#f1f5f9;border-left:4px solid #64748b;border-radius:8px;font-size:12.5px">
        <div style="font-size:11px;font-weight:800;color:#475569;margin-bottom:3px;display:flex;align-items:center;gap:4px">
          💬 对方发送内容 (HR)
        </div>
        <div style="color:#1e293b;line-height:1.5;white-space:pre-wrap">${esc(hrText)}</div>
      </div>` : '';

    const aiSection = `
      <div class="quote-box py-2 px-3 mb-2" style="background:#ecfdf5;border-left:4px solid #10b981;border-radius:8px;font-size:12.5px">
        <div style="font-size:11px;font-weight:800;color:#047857;margin-bottom:3px;display:flex;align-items:center;gap:4px">
          🤖 Agent 实际回复 / 执行动作
        </div>
        <div style="color:#064e3b;font-weight:500;line-height:1.5;white-space:pre-wrap">${esc(aiText)}</div>
      </div>`;

    const pills = [1,2,3,4,5,6,7,8,9,10].map(num => `
      <button type="button" class="btn-score-pill ${curScore === num ? 'active' : ''}" onclick="selectLedgerScore(${i}, ${num})">${num}</button>
    `).join('');

    const interactiveLayer = `
      <div class="pt-2" style="border-top:1px dashed rgba(0,0,0,0.06)">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div style="font-size:12px;font-weight:700;color:var(--txt);display:flex;align-items:center;gap:6px">
            ⭐ 回答点评打分（1-10分）:
            <span id="ledgerScoreBadge_${i}" class="soft-badge badge-ai" style="font-size:11px;font-weight:800">${curScore}分</span>
          </div>
          <div style="font-size:11px;color:var(--mut)">点击数字评分</div>
        </div>
        <div class="d-flex flex-wrap gap-1 mb-2" id="ledgerScorePills_${i}">
          ${pills}
        </div>
        <div class="mb-2">
          <label style="font-size:11.5px;font-weight:700;color:var(--txt);margin-bottom:3px;display:block">
            ✍️ 真人示范优化输入（输入更高情商金句，大模型自动存入经验库自学习进化）：
          </label>
          <textarea id="ledgerOptText_${i}" class="form-control" style="font-size:12px;border-radius:10px;resize:vertical;min-height:50px" placeholder="输入你认为更自然、更高情商的真人回复（例如：“真人在的，刚才回复太正式了哈哈…”）。">${esc(r.optimized_text || '')}</textarea>
        </div>
        <div class="d-flex justify-content-between align-items-center mt-2">
          <span id="ledgerFbStatus_${i}" style="font-size:11.5px;color:var(--mut)">${r.has_feedback ? '✅ 已有历史评价记录' : '未手动点评（默认8分）'}</span>
          <button class="btn-black btn-sm" style="padding:5px 14px;border-radius:10px;font-size:12px" onclick="saveLedgerFeedback(${i})">
            💾 保存点评与优化
          </button>
        </div>
      </div>
    `;

    return `
      <div class="ledger-event-card p-3 mb-3">
        <div class="d-flex flex-wrap justify-content-between align-items-center mb-2 gap-2">
          <div class="d-flex align-items-center gap-2">
            <span class="soft-badge ${badgeCls}" style="font-size:11.5px;font-weight:700">${esc(actName)}</span>
            <strong style="font-size:14px;color:var(--txt)">${esc(r.company || '-')}</strong>
            <span class="soft-badge ${stBadgeCls}" style="font-size:10.5px">${esc(st.slice(0, 40))}</span>
          </div>
          <div style="font-size:11.5px;color:var(--mut);font-weight:500">${esc(r.ts)}</div>
        </div>
        ${hrSection}
        ${aiSection}
        ${interactiveLayer}
      </div>
    `;
  }).join('');
}

async function loadSettings() {
  const s = await api('/api/settings');
  window.__cachedSettings = s;
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
  if (document.getElementById('inOnlineReply')) document.getElementById('inOnlineReply').checked = Boolean(s.online_reply_enabled);

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
    },
    online_reply_enabled: document.getElementById('inOnlineReply') ? document.getElementById('inOnlineReply').checked : false
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

const REAL_INDUSTRY_PRESETS = {
  ai_product: {
    company: "收钱吧",
    job: "Ai产品经理（J11304）",
    salary: "20-35K·14薪",
    city: "上海",
    jd: "【岗位职责】\\n1. AI 应用从 0 到 1 落地：围绕真实业务场景，负责 AI 应用的需求调研、方案设计、技术可行性判断、上线验证与持续迭代；可涉及上下文工程、RAG、Agent 等技术在产品中的应用。\\n2. 快速验证与产品打磨：能够运用 AI Coding 等方式，亲自完成原型、工作流或 Demo 的快速搭建与调试，验证方案可行性，并与研发团队共同推进正式落地。\\n3. 跨团队协同与流程建设：熟悉 AI 产品从需求评审、研发排期、测试验收至上线复盘的协作过程，协调产品、研发、算法、设计及业务等角色，高效推动项目交付。\\n4. 持续探索：持续关注大模型及 AI 应用的新能力，将技术边界转化为可验证、可落地的业务产品方案。\\n【任职资格】\\n1. 计算机、软件工程、数据科学等技术相关专业本科及以上学历；2 年及以上产品经理经验。\\n2. 有真实 AI 应用从 0 到 1 的项目经验，具备较强的 AI 动手能力：了解大模型、RAG、上下文工程、Agent 等常见应用方式。"
  },
  ev_auto: {
    company: "广志信息",
    job: "智能座舱性能稳定性测试工程师-沃尔沃",
    salary: "10-12K",
    city: "上海",
    jd: "岗位职责：\\n1. 作为各模块接口，对接智能座舱测试接口需求；\\n2. 负责智能座舱性能、功耗&STR、系统&稳定性测试，含测试用例编写、测试执行、测试验收、基线版本测试、编写测试报告及缺陷跟踪回归；\\n3. 独立完成项目测试并输出报告；\\n4. 把控交付质量，识别并推动解决测试风险；\\n任职要求：\\n1. 具备智能座舱或车载IVI系统测试经验；\\n2. 具备良好的跨团队协同与质量推动意识。"
  },
  java_dev: {
    company: "某大型知名计算机软件公司",
    job: "Java开发  线上面试 大厂高薪",
    salary: "15-30K·14薪",
    city: "杭州",
    jd: "1、统招本科以上学历；\\n2、JAVA基础扎实，充分理解面向对象，熟悉io、nio、多线程、设计模式、通信协议等基础技术；熟悉JVM工作原理并掌握常见性能调优方法；\\n3、熟悉Spring、Springmvc、Mybatis等常用开发框架及特征，熟悉常用中间件Tomcat、Mq、Kafka、Redis等；\\n4、具备大型分布式系统高并发架构设计经验优先。"
  },
  fin_quant: {
    company: "某基金公司",
    job: "期权量化研究员",
    salary: "15-30K",
    city: "上海",
    jd: "工作职责：\\n1. 利用日内期权数据构建因子并回测因子有效性；\\n2. 开发波动率交易策略；\\n3. 协助构建机器学习波动率交易策略；\\n岗位要求：\\n1. 985/211 或海外名校，计算机、工程、统计、数学、物理和金融工程专业优先；\\n2. 硕士以上学历；\\n3. 熟悉Python机器学习建模实战，对衍生品与量化策略有扎实理解。"
  },
  cross_border: {
    company: "睿联",
    job: "跨境电商运营（27届校招）",
    salary: "11-18K·14薪",
    city: "深圳",
    jd: "工作职责：\\n1. 负责公司在跨境电商平台上的销售、运营和推广工作，制定并执行有效的运营策略，提高产品在目标市场的知名度和市场份额；\\n2. 深入学习产品知识，配合编辑团队制作高质量页面；\\n3. 监控和分析运营数据，根据数据反馈调整运营策略；\\n任职要求：\\n1. 统招本科2027届毕业生，英语六级以上，读写流利；具备良好的商业嗅觉与数据分析力。"
  },
  robotics: {
    company: "某大型人工智能公司",
    job: "机器人运功控制算法",
    salary: "100-200K·14薪",
    city: "苏州",
    jd: "核心职责：\\n1. 全栈运控框架搭建：主导构建包含业务主控流程、路径/轨迹规划、避障优化及底层执行的全栈控制框架；\\n2. 解决复杂工况下的高精度力控、全栈运动规划及全身协同控制（WBC）等核心技术难题；\\n3. 推动轮式双臂/人形机器人从样机到量产的落地。\\n任职要求：\\n1. 自动化、机器人、控制理论等相关专业硕士及以上学历；精通C++/Python与经典控制理论。"
  },
  biomed: {
    company: "吃货妞妞",
    job: "生物信息工程师",
    salary: "5-8K",
    city: "上海",
    jd: "岗位职责：\\n1、负责生物信息数据的分析与处理，支持相关科研及技术开发工作；\\n2、参与数据分析流程的设计与优化，提升分析效率与准确性；\\n3、协助完成多组学数据的整合分析，挖掘生物学意义；\\n4、配合团队完成项目所需的个性化生物信息分析任务；\\n任职要求：\\n1、具备生物信息学或相关领域的专业背景，熟悉Linux环境与Python/R数据处理。"
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

let pgConversationHistory = [];

function clearPlaygroundChat() {
  pgConversationHistory = [];
  const histEl = document.getElementById('pgHistory');
  if (histEl) histEl.value = '';
  const flow = document.getElementById('pgChatFlow');
  if (flow) {
    flow.innerHTML = `
      <div class="chat-welcome-box" id="pgChatWelcome">
        <div style="font-size:38px;margin-bottom:12px">✨</div>
        <div class="chat-welcome-title">AI 求职对话推演沙盒</div>
        <div class="chat-welcome-desc">
          在此模拟 HR 与候选人的真实对话。大模型将根据您设定的候选人画像、目标岗位 JD 与防套话铁律，实时推演并生成真人口语化回复。
        </div>
        <div style="font-size:12px;color:var(--txt);font-weight:600;margin-top:14px;background:#f1f5f9;padding:8px 14px;border-radius:10px;display:inline-block">
          💡 右侧已预设各行各业名企真实岗位与真实JD（支持自由切换与自定义）。在下方输入模拟 HR 消息即可开始推演！
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
          <button type="button" class="btn-action-light ms-2" style="font-size:10px;padding:2px 8px;border-radius:6px;height:22px;display:inline-flex;align-items:center" onclick="copyCleanedReply()" title="复制清洗后的回复文本">📋 复制</button>
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

function onSelectIndustryPreset(key) {
  if (key === 'custom') {
    showToast('已切换至自定义模式，您可以自由编辑右侧岗位设定与JD', 'info');
    return;
  }
  const p = REAL_INDUSTRY_PRESETS[key];
  if (!p) return;
  document.getElementById('pgCompany').value = p.company;
  document.getElementById('pgJobTitle').value = p.job;
  document.getElementById('pgSalary').value = p.salary;
  document.getElementById('pgCity').value = p.city;
  document.getElementById('pgJd').value = p.jd.replace(/\\n/g, String.fromCharCode(10));
  syncChatHeader();
  // 切换不同岗位时重置历史，杜绝旧公司对话串味
  pgConversationHistory = [];
  const histEl = document.getElementById('pgHistory');
  if (histEl) histEl.value = '';
  showToast(`已载入真实名企岗位：${p.company} · ${p.job}（历史已重置）`, 'info');
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
  
  const isSingle = document.getElementById('pgModeSingleTurn') ? document.getElementById('pgModeSingleTurn').checked : true;
  let histLines = [];

  if (isSingle) {
    // 【首句沟通模式】：无条件清空前序对话历史，确保 0 上下文污染
    pgConversationHistory = [];
    if (document.getElementById('pgHistory')) document.getElementById('pgHistory').value = '';
    histLines = [];
  } else {
    // 【多轮连续模式】：若 pgConversationHistory 为空但右侧文本框有手动填入的内容，先行解析同步
    if (pgConversationHistory.length === 0) {
      const rawHist = (document.getElementById('pgHistory') ? document.getElementById('pgHistory').value : '').trim();
      if (rawHist) {
        pgConversationHistory = String(rawHist).split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean).map(line => {
          if (line.startsWith('我方:') || line.startsWith('我方：')) {
            return { role: 'me', text: line.replace(/^我方[:：]/, '').trim() };
          } else {
            return { role: 'hr', text: line.replace(/^HR[:：]/, '').trim() };
          }
        });
      }
    }
    // 传给后端的历史：本轮之前的全部累计上下文（不含本条尚未作答的消息）
    histLines = pgConversationHistory.map(m => (m.role === 'hr' ? 'HR: ' : '我方: ') + m.text);
  }

  // 左侧渲染 HR 消息
  appendChatMessage('hr', msg);
  
  // 展现思考等待动效
  showChatThinking();

  const btn = document.getElementById('btnSimulate');
  const statusEl = document.getElementById('pgStatusHint');
  if (btn) btn.disabled = true;
  if (statusEl) { statusEl.style.color = 'var(--acc)'; statusEl.textContent = '大模型思考中…'; }

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

    if (!isSingle) {
      // 仅在多轮推演模式下，把当前 HR 消息与 Agent 回复一并沉淀入连续历史池
      pgConversationHistory.push({ role: 'hr', text: msg });
      pgConversationHistory.push({ role: 'me', text: replyText });

      // 实时同步回写到右侧历史输入框，保持多轮上下文完全透明与连贯
      if (document.getElementById('pgHistory')) {
        document.getElementById('pgHistory').value = pgConversationHistory.map(m => (m.role === 'hr' ? 'HR: ' : '我方: ') + m.text).join(String.fromCharCode(10));
      }
    }

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

load(false);
setInterval(() => load(false), 30000);
</script>
</body>
</html>"""


def _extract_dialog_texts(primary, resolver=None):
    """从单条记录或解析行中提取完整的【HR发言】与【Agent回复/动作】。"""
    hr_text = ""
    for src in (resolver, primary):
        if not src or not isinstance(src, dict):
            continue
        if src.get("last_msg"):
            t = str(src.get("last_msg")).strip()
            if t:
                hr_text = t
                break
        conv = src.get("conv") or ""
        if conv:
            lines = [l.strip() for l in conv.split("\n") if l.strip()]
            if len(lines) >= 3:
                hr_text = "\n".join(lines[2:])
            elif len(lines) >= 1:
                hr_text = lines[-1]
            if hr_text:
                break
        err = src.get("error") or ""
        if "head='" in err:
            try:
                head_part = err.split("head='")[1].split("')")[0]
                lines = [l.strip() for l in head_part.replace("\\n", "\n").split("\n") if l.strip()]
                if len(lines) >= 3:
                    hr_text = "\n".join(lines[2:])
                elif len(lines) >= 1:
                    hr_text = lines[-1]
                if hr_text:
                    break
            except Exception:
                pass

    ai_text = ""
    for src in (resolver, primary):
        if not src or not isinstance(src, dict):
            continue
        if src.get("reply_text"):
            t = str(src.get("reply_text")).strip()
            if t:
                ai_text = t
                break
        if src.get("text_head"):
            t = str(src.get("text_head")).strip()
            if t:
                ai_text = t
                break
        if src.get("suggested_reply"):
            t = str(src.get("suggested_reply")).strip()
            if t:
                ai_text = t
                break

    act = (resolver or primary or {}).get("action") or (primary or {}).get("resolved_action") or ""
    if not ai_text:
        if act == "exchange_wechat":
            ai_text = "【系统动作】已在沟通界面向对方发起官方交换微信申请"
        elif act == "send_resume":
            ai_text = "【系统动作】已在沟通界面向对方发送正式在线简历"
        elif act == "agree_wechat":
            ai_text = "【系统动作】已同意对方发起的交换微信邀请"
        elif act in ("card_ignore", "ignore", "daemon_skip"):
            ai_text = "【人工操作】已忽略/跳过该会话"
        elif act == "reply":
            ai_text = "【系统动作】已回复对方消息"

    if not hr_text:
        hr_text = "（历史对话未抓取到单句正文，已记录会话节点）"

    return hr_text, ai_text


def _is_alert_resolved(alert_ts, company, rows):
    """判断该告警是否已被后续动作处理过（回复、换微信、发简历、同意换微信、忽略或标记完成）。
    返回 (is_resolved, act, ts, status, resolving_row)
    """
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
                return True, act, ts, r.get("status"), r
    return False, None, None, None, None


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

    # 加载现有点评与优化记录 (经验记忆库)
    feedbacks = {}
    try:
        from boss_apply import experience
        for fb in experience.load_all():
            k1 = f"{fb.get('company')}_{fb.get('hr_msg', '')[:40]}"
            feedbacks[k1] = fb
            feedbacks[str(fb.get("company", ""))] = fb
    except Exception:
        pass

    pending = []
    resolved = []
    seen_companies = set()
    for c, a in seen.items():
        is_res, act, ts, st, r_res = _is_alert_resolved(a.get("ts") or "", c, rows)
        hr_text, ai_text = _extract_dialog_texts(a, r_res)
        fb = feedbacks.get(f"{c}_{hr_text[:40]}") or feedbacks.get(c) or {}
        item = {
            "company": c,
            "last_msg": hr_text[:500],
            "reason": (a.get("reason") or "")[:200],
            "suggested": ai_text,
            "time": a.get("ts") or "",
            "high_intent": bool(a.get("high_intent")),
            "resolved_action": act,
            "resolved_time": ts,
            "resolved_status": st,
            "score": fb.get("score", 8),
            "optimized_text": fb.get("optimized_text", ""),
            "has_feedback": bool(fb),
        }
        if is_res:
            resolved.append(item)
            seen_companies.add(c)
        else:
            pending.append(item)

    # 扩展已处理记录：纳入台账中实际成功的直接回复、换微信与发简历记录（扩大数据源，供点评与自学习）
    for r in reversed(rows[-150:]):
        act = r.get("action")
        st = r.get("status")
        comp = r.get("company")
        if not comp or comp in seen_companies:
            continue
        if act in ("reply", "exchange_wechat", "send_resume", "agree_wechat") and st in ("ok", "already_sent", "already_agreed"):
            hr_text, ai_text = _extract_dialog_texts(r, None)
            fb = feedbacks.get(f"{comp}_{hr_text[:40]}") or feedbacks.get(comp) or {}
            resolved.append({
                "company": comp,
                "last_msg": hr_text[:500],
                "reason": "常规会话交互",
                "suggested": ai_text,
                "time": r.get("ts") or "",
                "high_intent": False,
                "resolved_action": act,
                "resolved_time": r.get("ts") or "",
                "resolved_status": st,
                "score": fb.get("score", 8),
                "optimized_text": fb.get("optimized_text", ""),
                "has_feedback": bool(fb),
            })
            seen_companies.add(comp)
            if len(resolved) >= 50:
                break

    # 补充历史点评沉淀（若未被上述记录覆盖，完整回显）
    for fb in feedbacks.values():
        comp = fb.get("company")
        if not comp or comp in seen_companies:
            continue
        resolved.append({
            "company": comp,
            "last_msg": (fb.get("hr_msg") or "（历史真人点评会话）")[:500],
            "reason": "历史点评沉淀",
            "suggested": fb.get("ai_reply") or "（历史回复）",
            "time": fb.get("ts") or "",
            "high_intent": False,
            "resolved_action": "feedback_sample",
            "resolved_time": fb.get("ts") or "",
            "resolved_status": "scored",
            "score": fb.get("score", 8),
            "optimized_text": fb.get("optimized_text", ""),
            "has_feedback": True,
        })
        seen_companies.add(comp)

    pending.sort(key=lambda x: x["time"], reverse=True)
    resolved.sort(key=lambda x: x.get("resolved_time") or x["time"], reverse=True)

    enriched_ledger = []
    for r in reversed(rows[-120:]):
        act = r.get("action") or ""
        comp = r.get("company") or r.get("title") or ""
        st = r.get("status") or r.get("reason") or r.get("text_head") or ""
        hr_text, ai_text = _extract_dialog_texts(r, None)
        if hr_text == "（历史对话未抓取到单句正文，已记录会话节点）" and not r.get("last_msg") and not r.get("conv") and not r.get("error"):
            hr_clean = ""
        else:
            hr_clean = hr_text
        fb = feedbacks.get(f"{comp}_{hr_clean[:40]}") or feedbacks.get(comp) or {}
        item = {
            "ts": r.get("ts") or r.get("timestamp") or "",
            "action": act,
            "company": comp,
            "status": st,
            "reason": r.get("reason") or "",
            "last_msg": hr_clean[:500],
            "suggested": ai_text,
            "score": fb.get("score", 8),
            "optimized_text": fb.get("optimized_text", ""),
            "has_feedback": bool(fb),
        }
        if act == "daemon_cycle_summary":
            item["scanned"] = r.get("scanned", 0)
            item["candidates"] = r.get("candidates", 0)
            item["replied"] = r.get("replied", 0)
            item["dry_run"] = r.get("dry_run", False)
        enriched_ledger.append(item)

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
        "daemon": get_daemon_status(),
        "auth": qr_login.QRLoginManager().get_auth_status(cfg),
        "user_profile": qr_login.get_cached_user_profile(),
        "pending": pending[:100],
        "resolved": resolved[:100],
        "ledger": enriched_ledger,
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


@app.post("/api/feedback")
async def api_feedback(request: Request, token: str = ""):
    """接收用户在工作台对已处理回复的逐一点评（1-10分）与人工优化内容，沉淀入经验记忆库。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    company = (body.get("company") or "").strip()
    job = (body.get("job") or "").strip()
    hr_msg = (body.get("hr_msg") or "").strip()
    ai_reply = (body.get("ai_reply") or "").strip()
    score = body.get("score", 8)
    optimized_text = (body.get("optimized_text") or "").strip()
    source = (body.get("source") or "web_console").strip()

    if not company and not hr_msg:
        return JSONResponse({"ok": False, "error": "company or hr_msg required"}, status_code=400)

    try:
        from boss_apply import experience
        record = experience.record_feedback(
            company=company,
            job=job,
            hr_msg=hr_msg,
            ai_reply=ai_reply,
            score=score,
            optimized_text=optimized_text,
            source=source,
        )
        return {"ok": True, "record": record}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/apply/now")
async def api_apply_now(request: Request, token: str = ""):
    """立即执行今日投递计划（Phase 4）或全链路扫描投递（Phase 1~4）。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    mode = body.get("mode", "execute_plan")
    dry_run = bool(body.get("dry_run", False))
    daemon_cfg = cfg.get("daemon") or {}
    default_top_n = int(daemon_cfg.get("apply_top_n", 50))
    top_n = int(body.get("top_n") or default_top_n)

    from boss_apply import daily_apply
    g = guardmod.Guard(cfg)
    if mode == "full_scan":
        report = daily_apply.scan_and_apply_daily(cfg, dry_run=dry_run)
        if not dry_run and report.get("phase") == "complete":
            g.mark_scan_done()
        return {"ok": True, "mode": "full_scan", "report": report}
    else:
        exec_res = daily_apply.execute_daily_plan(cfg, g, top_n=top_n)
        if not dry_run and (exec_res.get("executed", 0) > 0 or not exec_res.get("error")):
            g.mark_scan_done()
        return {"ok": True, "mode": "execute_plan", "result": exec_res}


@app.post("/api/daemon/toggle")
async def api_daemon_toggle(request: Request, token: str = ""):
    """启动或停止后台常驻守护进程。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    action = body.get("action", "start")
    status = get_daemon_status()

    if action == "start":
        if status.get("running"):
            return {"ok": True, "message": f"守护进程已在运行中 (PID {status.get('pid')})", "pid": status.get("pid")}
        python_exe = sys.executable
        daemon_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daemon_auto_reply.py")
        cmd = [python_exe, "-u", daemon_script, "--loop"]
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        p = subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "message": f"已成功启动常驻守护进程 (PID {p.pid})", "pid": p.pid}
    elif action == "stop":
        pid = status.get("pid")
        if not status.get("running") or not pid:
            return {"ok": True, "message": "守护进程未在运行"}
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                from scripts.daemon_auto_reply import write_heartbeat
                write_heartbeat("stopped", {"reason": "manual_web_stop"})
            except Exception:
                pass
            return {"ok": True, "message": f"已停止守护进程 (PID {pid})"}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"error": "invalid_action"}, status_code=400)


@app.get("/api/auth/status")
def api_auth_status(token: str = ""):
    """获取当前 BOSS 直聘登录鉴权状态及 Chrome 实例信息。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return qr_login.QRLoginManager().get_auth_status(cfg)


@app.get("/api/auth/qrcode/get")
def api_auth_qrcode_get(token: str = ""):
    """获取原生登录二维码 Base64 及唯一跟踪 UUID。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    res = qr_login.QRLoginManager().get_qrcode(cfg)
    if not res.get("ok"):
        return JSONResponse(res, status_code=500)
    return res


@app.get("/api/auth/qrcode/status")
def api_auth_qrcode_status(uuid: str = "", token: str = ""):
    """轮询二维码扫码确认状态。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not uuid:
        return JSONResponse({"ok": False, "error": "missing_uuid"}, status_code=400)
    return qr_login.QRLoginManager().check_scan_status(uuid, cfg)


@app.get("/api/auth/profile")
def api_auth_profile(token: str = ""):
    """获取用户在 BOSS 直聘的个人真实资料（姓名、头像、学校、求职状态等）。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return qr_login.get_cached_user_profile()


@app.post("/api/auth/profile/sync")
async def api_auth_profile_sync(request: Request, token: str = ""):
    """从 BOSS 直聘主动触发同步真实个人资料并合并写入 profile.local.json。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    prof = qr_login.fetch_boss_user_profile(cfg)
    qr_login.sync_profile_to_local(prof)
    return {"ok": True, "user_profile": prof}


@app.post("/api/auth/clear_key")
async def api_auth_clear_key(request: Request, token: str = ""):
    """清除 Windows DPAPI 中的密钥并在必要时清除 config.local.json 中的 api_key。"""
    cfg = cfgmod.load()
    if not _check_token(cfg, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    key_name = body.get("name", "llm_api_key")
    secrets_mod.set_secret(key_name, "")
    if key_name == "llm_api_key":
        path = cfgmod.LOCAL_CFG_PATH
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
                if "llm" in local_data and isinstance(local_data["llm"], dict):
                    local_data["llm"].pop("api_key", None)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(local_data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
    return {"ok": True, "message": f"已成功清除 {key_name}"}


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
