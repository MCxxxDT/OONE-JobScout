"""高层流程（scan / plan / execute / probe），server 与 scripts 共用。

反爬升级（2026-08-30）：scan/probe 全部改走裸CDP（rawcdp），
因为 playwright 的CDP会话特征会被BOSS安全JS检测并清空DOM。
execute（打招呼）暂保留 playwright 路径，T3 前需同样改造。
"""
import base64
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import browser, citycodes, config as cfgmod, guard, greeter, ledger, llm_match, rawcdp, scorer


def _prefs(cfg):
    p = cfg.get("prefs") or {}
    return {
        "want_jobs": [str(x).strip() for x in (p.get("want_jobs") or []) if str(x).strip()],
        "avoid_jobs": [str(x).strip() for x in (p.get("avoid_jobs") or []) if str(x).strip()],
        "want_cities": [str(x).strip() for x in (p.get("want_cities") or []) if str(x).strip()],
        "avoid_cities": [str(x).strip() for x in (p.get("avoid_cities") or []) if str(x).strip()],
    }


def effective_keywords(cfg):
    """生效扫描关键词：偏好向往岗位非空则替换默认词表（留空=沿用现有词表=LLM/系统默认决断）。"""
    return _prefs(cfg)["want_jobs"] or list(cfg.get("keywords") or [])


def effective_cities(cfg):
    """生效扫描城市集（含偏好过滤）。返回 {cities, unknown, excluded}。
    want_cities 非空：经 citycodes 解析替换默认城市集（未知城市名入 unknown 供 Web 提示；
    quota 沿用 config.json 已配置城市的值，未配置默认 15）；avoid_cities 一律剔除。"""
    prefs = _prefs(cfg)
    avoid = set(citycodes.expand_provinces(prefs["avoid_cities"]))
    cm = {c["name"]: c for c in cfg.get("cities") or []}
    if prefs["want_cities"]:
        resolved, unknown = citycodes.resolve_cities(prefs["want_cities"], cfg)
        for r in resolved:
            if r["name"] in cm and cm[r["name"]].get("quota"):
                r["quota"] = cm[r["name"]]["quota"]
        return {"cities": [r for r in resolved if r["name"] not in avoid],
                "unknown": unknown,
                "excluded": [r["name"] for r in resolved if r["name"] in avoid]}
    base = [dict(c) for c in cfg.get("cities") or [] if c.get("name") not in avoid]
    return {"cities": base, "unknown": [],
            "excluded": [c["name"] for c in cfg.get("cities") or [] if c.get("name") in avoid]}


def avoid_veto(job, avoid_jobs):
    """偏好排斥岗位硬否决：岗位标题或公司名命中任一排斥词 → (True, 命中词)。"""
    title = (job.get("title") or "")
    company = (job.get("company") or "")
    for k in (avoid_jobs or []):
        if k and (k in title or k in company):
            return True, k
    return False, ""


def judge_job_fit(job, cfg):
    """岗位适配门禁判定链（2026-09-09，零硬编码岗位词——规则词全部来自 Web 端 prefs）：
    ① prefs.avoid_jobs 命中标题/公司 → 拒绝 rule_blacklist（不烧 LLM）；
    ② prefs.want_jobs 命中标题 → 放行 rule_whitelist（不烧 LLM）；
    ③ 未命中/清单为空 → llm_match.match_one 语义兜底：verdict ≥ job_fit_gate.min_verdict
      放行（llm_match_pass），否则拒绝（llm_low_match）；LLM 不可用 fail-close（llm_unavailable）。
    返回 {allow, attribution, detail, verdict, score, hit_word}。"""
    prefs = _prefs(cfg)
    vetoed, hit = avoid_veto(job, prefs["avoid_jobs"])
    if vetoed:
        return {"allow": False, "attribution": "rule_blacklist",
                "detail": "命中排斥词「%s」" % hit, "verdict": "", "score": None, "hit_word": hit}
    title = job.get("title") or ""
    for k in prefs["want_jobs"]:
        if k and k in title:
            return {"allow": True, "attribution": "rule_whitelist",
                    "detail": "命中向往岗位「%s」" % k, "verdict": "", "score": None, "hit_word": k}
    m = llm_match.match_one(job, cfg)
    if m is None:
        return {"allow": False, "attribution": "llm_unavailable",
                "detail": "LLM 判定不可用，fail-close 拒绝（可稍后重试或人工在 BOSS App 操作）",
                "verdict": "", "score": None, "hit_word": ""}
    order = {"veto": 0, "low": 1, "medium": 2, "high": 3}
    min_verdict = str((cfg.get("job_fit_gate") or {}).get("min_verdict") or "medium")
    ok = order.get(str(m.get("verdict") or "low"), 0) >= order.get(min_verdict, 2)
    return {"allow": ok,
            "attribution": "llm_match_pass" if ok else "llm_low_match",
            "detail": "LLM: %s" % (m.get("reason") or ""),
            "verdict": m.get("verdict") or "", "score": m.get("score"), "hit_word": ""}


def _job_fit_gate(cfg, company):
    """岗位适配门禁前置：只读抓会话关联岗位 → judge_job_fit → 台账留痕（action=job_fit_gate，
    归因字段 attribution）。岗位信息不可得时 fail-close（job_info_unavailable）——
    连岗位都无法确认，不盲发高敏实弹动作。"""
    try:
        jd_res = chat_job_detail(cfg, company=company, fetch_history=False)
    except Exception as e:
        jd_res = {"ok": False, "error": str(e)[:200]}
    job = (jd_res.get("job") or {}) if jd_res.get("ok") else {}
    if jd_res.get("ok") and (job.get("title") or job.get("company")):
        verdict = judge_job_fit(job, cfg)
    else:
        verdict = {"allow": False, "attribution": "job_info_unavailable",
                   "detail": "无法获取会话关联岗位（%s）" % (jd_res.get("error") or "会话无岗位信息"),
                   "verdict": "", "score": None, "hit_word": ""}
    ledger.append({
        "action": "job_fit_gate", "company": company, "job_title": job.get("title") or "",
        "allow": verdict["allow"], "attribution": verdict["attribution"],
        "hit_word": verdict.get("hit_word") or "", "verdict": verdict.get("verdict") or "",
        "score": verdict.get("score"), "detail": verdict.get("detail") or "",
    })
    return verdict

# 消息中心会话列表提取（裸CDP一次性 evaluate，从 scripts/chat_check.py 迁入）
CHAT_LIST_JS = """
(() => {
  const isConv = (li) => { const t = li.innerText || ''; return t.length > 12 && /(?:\\d{1,2}:\\d{2}|\\d{1,2}月\\d{1,2}日|昨天|\\d{4}年)/.test(t); };
  return JSON.stringify(Array.from(document.querySelectorAll('li')).filter(isConv)
    .map(li => (li.innerText || '').slice(0, 200)));
})()
"""


def scan_city(cfg, g, city, keywords=None, max_pages=2, fetch_detail=True):
    """只读扫描一个城市（裸CDP）：搜索→(可选)抓详情→打分→写台账。不发送任何沟通。
    偏好注入（2026-09-09）：向往岗位替换默认关键词；城市集经 effective_cities 校验
    （不在生效城市集/被排斥 → 拒扫）；排斥岗位命中 → 硬否决记台账（score=0）。"""
    eff = effective_cities(cfg)
    cm = {c["name"]: c for c in eff["cities"]}
    prefs = _prefs(cfg)
    if city not in cm:
        return {"error": "city %r not in effective scan set (prefs filtered or unknown)" % city,
                "available": [c["name"] for c in eff["cities"]],
                "unknown_cities": eff["unknown"]}
    kws = keywords or effective_keywords(cfg)
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    found, scored = 0, 0
    llm_scored = 0
    job_mode = cfg.get("job_mode", "intern")  # intern(108) | campus(102) | mix | all(None)
    try:
        sess.open_tab()
        # 两段式：先收集通过硬过滤（kill/届别/薪资/active/排斥偏好）的岗位，
        # 词表分只作回退基线；随后 llm_match 批量智能打分覆盖（失败/无key沿用词表分）
        candidates = []
        for kw in kws:
            for p in range(1, max_pages + 1):
                ok, info = g.check_search()
                if not ok:
                    return {"city": city, "found": found, "scored": scored, "stopped": info}
                # 解析当前页经验门禁参数
                if job_mode == "intern":
                    exp_code = "108"
                elif job_mode == "campus":
                    exp_code = "102"
                elif job_mode == "mix":
                    exp_code = "108" if p % 2 == 1 else "102"
                else:
                    exp_code = None
                try:
                    jobs = sess.search_jobs(kw, cm[city]["code"], p, experience=exp_code)
                except browser.RiskControl as e:
                    g.pause("risk: %s" % e)
                    return {"city": city, "found": found, "scored": scored, "PAUSED": str(e)}
                g.record_search()
                found += len(jobs)
                for job in jobs:
                    job["job_mode"] = job_mode
                    job["experience"] = exp_code
                    # 偏好排斥岗位硬否决（在打分前，LLM 匹配也不会触达）
                    vetoed, veto_word = avoid_veto(job, prefs["avoid_jobs"])
                    if vetoed:
                        ledger.append({
                            "action": "scan", "city": city, "keyword": kw, "score": 0,
                            "job_mode": job_mode, "experience": exp_code,
                            "reason": "prefs avoid: title/company hit %r" % veto_word,
                            "title": job.get("title"), "company": job.get("company"),
                            "salary": job.get("salary"), "href": job.get("href"),
                            "tags": job.get("tags"), "boss_active": job.get("boss_active"),
                        })
                        continue
                    detail = ""
                    if fetch_detail:
                        try:
                            detail, active = sess.fetch_detail(job)
                            if active is not None and active >= 0:
                                job["boss_active"] = active  # 详情页补验活跃度
                        except Exception:
                            detail = ""  # 详情失败降级：仅用列表信息打分
                    s, why = scorer.score(job, detail, cfg)
                    if s <= 0:
                        ledger.append({
                            "action": "scan", "city": city, "keyword": kw, "score": 0, "reason": why,
                            "title": job.get("title"), "company": job.get("company"),
                            "salary": job.get("salary"), "href": job.get("href"),
                            "tags": job.get("tags"), "boss_active": job.get("boss_active"),
                            "detail_head": (detail or "")[:200],
                            "score_source": "hard_filter",
                        })
                        continue
                    candidates.append((job, detail, kw, s, why))
                    browser.human_wait(cfg, "page")
        # LLM 智能匹配批量覆盖（增强项：失败回退词表分，绝不阻塞）
        llm_res = llm_match.match_batch(
            [{**j, "detail": d} for (j, d, _kw, _s, _why) in candidates], cfg)
        for i, (job, detail, kw, s, why) in enumerate(candidates):
            final_s, final_why, source, verdict = s, why, "keywords", ""
            if llm_res and i in llm_res:
                m = llm_res[i]
                final_s, final_why = m["score"], "llm: " + m["reason"]
                source, verdict = "llm", m.get("verdict") or ""
                llm_scored += 1
            ledger.append({
                "action": "scan", "city": city, "keyword": kw, "score": final_s, "reason": final_why,
                "title": job.get("title"), "company": job.get("company"),
                "salary": job.get("salary"), "href": job.get("href"),
                "tags": job.get("tags"), "boss_active": job.get("boss_active"),
                "detail_head": (detail or "")[:200],
                "score_source": source, "verdict": verdict,
            })
            if final_s > 0:
                scored += 1
    finally:
        sess.close_tab()
        sess.close()
    return {"city": city, "found": found, "scored": scored, "llm_scored": llm_scored,
            "score_source": "llm" if llm_scored else "keywords", "guard": g.summary()}


def login_state(cfg, poll_s=15):
    """裸CDP开临时标签页检查BOSS登录态/风控（t1脚本与MCP server共用）。
    历史：playwright 版 connect+goto 在已登录 profile 上会被BOSS安全JS清空页面
    并误报"未登录"（2026-08-31 实测），故统一走裸CDP。
    返回 {logged_in, risk, url, shot, hint, error?}；risk 非空时护栏自动 pause。"""
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab("https://www.zhipin.com/")
        st = None
        for _ in range(int(poll_s)):
            time.sleep(1)
            st = sess.state()
            if st and not st.get("blank") and st.get("bodyLen", 0) > 100:
                break
        if not st or st.get("blank") or st.get("bodyLen", 0) <= 0:
            return {"logged_in": False, "risk": None, "url": (st or {}).get("href", ""),
                    "shot": "", "hint": None, "error": "page blank or load failed"}
        if st.get("captcha") or st.get("security"):
            guard.Guard(cfg).pause("risk: login_state captcha/security")
            return {"logged_in": False, "risk": "captcha/security", "url": st.get("href", ""),
                    "shot": "", "hint": "页面被风控质询，人工确认后 run resume_guard"}
        info = {}
        try:
            info = json.loads(sess.eval(rawcdp.LOGIN_JS) or "{}")
        except Exception:
            info = {}
        shot_path = ""
        try:
            shot = sess._send("Page.captureScreenshot", {"format": "png"}, sid=sess.sid)
            shot_path = cfgmod.state_path("login_state.png")
            with open(shot_path, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
        except Exception:
            pass
        logged = bool(info.get("avatar")) and not info.get("loginBtn")
        return {"logged_in": logged, "risk": None, "url": info.get("url", st.get("href", "")),
                "shot": shot_path,
                "hint": None if logged else "请在调试Chrome窗口内扫码登录BOSS后重试"}
    finally:
        sess.close_tab()
        sess.close()


def build_plan(cfg, min_score=None, limit=15):
    jobs = ledger.pending(cfg, min_score)
    plan = jobs[:limit]
    path = cfgmod.state_path("plan.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return {
        "plan_file": path, "count": len(plan),
        "jobs": [{"title": j.get("title"), "company": j.get("company"), "score": j.get("score"), "city": j.get("city")} for j in plan],
    }


def filter_greeted(jobs):
    """剔除台账中已有 greet ok 记录的岗位，防止重复跟发。"""
    greeted = {r.get("href") for r in ledger.load_all()
               if r.get("action") == "greet" and r.get("status") == "ok"}
    return [j for j in jobs if not (j.get("href") and j["href"] in greeted)]


# 平台系统回显消息（简历送达回执/对方已同意/交换请求处理回执），非 HR 真实发言，不应视为待回复。
# 2026-09-08 沉心传媒复发案例：用户婉拒微信交换后，侧栏最新预览变成
# "您已经成功拒绝了对方交换微信请求"（系统回执），旧词表未覆盖 → 被误判为
# HR 发言 → daemon 答非所问又回了一条。交换请求的同意/拒绝回执全部纳入。
# 2026-09-09 加固：全面覆盖各类系统回执、交换提示、打招呼成功、撤回等系统非对话消息。
SYSTEM_MSG_RE = re.compile(
    r"^您的附件简历"
    r"|已发送给(?:Boss|对方)"
    r"|^对方已同意"
    r"|^对方请求交换"
    r"|^请求交换(?:微信|电话|联系方式)"
    r"|^双方已交换"
    r"|^打招呼成功"
    r"|^已撤回"
    r"|^您已(?:经)?(?:成功)?(?:同意|拒绝)"
    r"|您已(?:经)?(?:成功)?(?:同意|拒绝)"
    r"|(?:成功)?(?:同意|拒绝)(?:了)?(?:对方)?交换(?:微信|电话|联系方式)"
    r"|(?:微信|电话)(?:交换|已交换)"
    r"|^已(?:同意|拒绝)和对方交换"
    r"|对方发起了交换"
    r"|交换请求已发送"
)
# 短促礼貌结束语（精确匹配，HR 独发即视为对话自然闭环）
CLOSING_WORDS = ("谢谢", "感谢", "好的", "好嘞", "收到", "嗯嗯", "ok", "OK")


# 消息状态回执（2026-09-08 实测）：[送达]/[已读] 只出现在我方最后发言的会话上——
# 平台只对我方发出的消息回执；HR 发的消息无此标记。这是判定发言方的权威信号，
# 修复"用户手打的短消息（不在任何 opener 前缀集合）被误判为 HR 发言"的 bug
# （实测案例：用户对沉心传媒手打"直接boss说吧"被误判，导致 daemon 答非所问）。
SELF_MSG_STATUS = ("[送达]", "[已读]")


def parse_conv(raw, openers):
    """消息中心会话行 → 结构化。raw 形如 '02:42|赵先生新美虹星总经理|[送达]|您好，…'。
    from_us 双通道判定（2026-09-08 修复）：
      ① 前缀法（原有）：最后一条以 openers 任一前缀开头（含原生默认招呼）；
      ② 回执法（新增，权威）：会话带 [送达]/[已读] 状态回执 = 我方最后发言。
    needs_human=最后一条含联系方式意图（greeter.privacy_blocked，索要或发送均拦截）；
    系统回显（审计补丁#4）与短结束语（审计补丁#5）不判待回复。"""
    parts = [p for p in raw.replace("\n", "|").split("|") if p != ""]
    if len(parts) < 2:
        return None
    time_s = parts[0] if any(k in parts[0] for k in (":", "月", "年", "昨天")) else ""
    who = parts[1] if len(parts) > 1 else ""
    rest = parts[2:]
    status = ""
    if rest and rest[0].startswith("["):
        status = rest[0]
        rest = rest[1:]
    preview = "|".join(rest)
    from_us = (any(preview.startswith(op) for op in (openers or ()))
               or status in SELF_MSG_STATUS)
    is_system = bool(SYSTEM_MSG_RE.search(preview))
    is_closing = preview in CLOSING_WORDS
    return {"time": time_s, "who": who, "status": status,
            "last_msg": preview[:120],
            "needs_reply_guess": bool(preview) and not from_us and not is_system and not is_closing,
            "needs_human": not is_system and greeter.privacy_blocked(preview)}


def chat_inbox(cfg):
    """消息中心只读巡检（裸CDP，零发送）。返回 needs_reply（待回复）/needs_human
    （索要联系方式，禁止代发）/all。verify/安全页 → pause 护栏并返回 error
    （与 login_state 行为对齐，人工确认后 resume_guard）。"""
    ops = greeter.self_openers(cfg) or (greeter.NATIVE_DEFAULT_OPENER,)
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab(rawcdp.BASE + "/web/geek/chat")
        st = None
        for _ in range(15):
            time.sleep(1)
            st = sess.state()
            if st and not st.get("blank") and st.get("bodyLen", 0) > 100:
                break
        if st and (st.get("captcha") or st.get("security")):
            guard.Guard(cfg).pause("risk: chat_inbox captcha/security")
            return {"error": "verify/security page", "state": st}
        v = sess.eval(CHAT_LIST_JS)
        raws = json.loads(v) if v else []
        convs = [c for c in (parse_conv(r, ops) for r in raws) if c]
        return {"count": len(convs),
                "needs_human": [c for c in convs if c["needs_human"]],
                "needs_reply": [c for c in convs if c["needs_reply_guess"] and not c["needs_human"]],
                "all": convs}
    finally:
        sess.close_tab()
        sess.close()


def chat_reply(cfg, company, text, force=False):
    """按公司名回复 HR 一条消息（经 greeter.send_message_via_chat），写台账 action=reply。
    隐私红线：文案含联系方式意图 → 拒绝发送（blocked_privacy），转人工。
    安全门禁：online_reply_enabled 为 False 时（非人工审批 force）物理拦截，零消息发往线上真实 HR。
    公司未在会话列表命中时 greeter 层直接抛异常中止，绝不退回最新会话（审计补丁#3）。"""
    if not force and not cfg.get("online_reply_enabled", True):
        ledger.append({"action": "reply", "status": "intercepted_safety_gate", "company": company,
                       "text_head": (text or "")[:120],
                       "note": "online_reply_enabled 为 false，线上发送已被全局安全门禁拦截"})
        return {"ok": False, "intercepted": True, "reason": "online_reply_enabled_false", "company": company}
    if greeter.privacy_blocked(text):
        ledger.append({"action": "reply", "status": "blocked_privacy", "company": company,
                       "text_head": (text or "")[:120],
                       "note": "文案含联系方式（电话/微信等），拒绝代发，转人工回复"})
        return {"ok": False, "blocked": "privacy", "company": company}
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.send_message_via_chat(sess, company, text)
        r_status = (r or {}).get("status", "ok")
        if r_status == "unknown":
            ledger.append({"action": "reply", "status": "unknown", "company": company,
                           "text_head": (text or "")[:120], "conv": (r or {}).get("conv"),
                           "note": "发送结果无法核验，已挂起等待下轮确认"})
            return {"ok": False, "status": "unknown", "company": company, "result": r}
        ledger.append({"action": "reply", "status": "ok", "company": company,
                       "text_head": (text or "")[:120], "conv": (r or {}).get("conv")})
        return {"ok": True, "company": company, "result": r}
    except Exception as e:
        ledger.append({"action": "reply", "status": "failed", "company": company,
                       "error": str(e)[:200]})
        return {"ok": False, "company": company, "error": str(e)[:300]}
    finally:
        sess.close_tab()
        sess.close()


def check_privacy_permission(action: str, cfg: dict, hi_flag: bool = False, force: bool = False) -> Tuple[bool, str]:
    """核查动作是否允许根据 privacy_policy 执行。
    策略模式:
      - disabled: 该动作禁用，任何外部调用与普通审批均禁止执行
      - manual: 仅允许人工显式审批 (force=True)
      - high_intent_only: 仅高意向会话 (hi_flag=True) 或人工显式审批 (force=True) 放行
      - auto: 允许自动执行或人工审批
    非法未知模式（如拼写错误 manul）默认安全阻断并报错。
    """
    pol = (cfg or {}).get("privacy_policy") or {}
    key_map = {
        "exchange_wechat": "exchange_wechat",
        "agree_wechat": "exchange_wechat",
        "send_resume": "send_resume",
        "exchange_phone": "exchange_phone",
    }
    pol_key = key_map.get(action)
    if not pol_key:
        return True, ""

    default_mode = "manual" if pol_key == "exchange_phone" else "auto"
    mode = pol.get(pol_key, default_mode)

    action_cn = {
        "exchange_wechat": "换微信",
        "agree_wechat": "同意换微信",
        "send_resume": "发简历",
        "exchange_phone": "换电话",
    }.get(action, action)

    VALID_MODES = {"auto", "high_intent_only", "manual", "disabled"}
    if mode not in VALID_MODES:
        return False, f"{action_cn}策略模式未知或非法[{mode}]（仅支持 auto/high_intent_only/manual/disabled），默认安全拦截"

    if mode == "disabled":
        return False, f"{action_cn}动作已被用户设置为[禁用]（disabled），禁止任何执行"

    if force:
        return True, ""

    if mode == "auto":
        return True, ""
    elif mode == "high_intent_only":
        if hi_flag:
            return True, ""
        return False, f"{action_cn}策略为[仅高意向自动]，当前会话未达到高意向标准，转人工审批"
    elif mode == "manual":
        return False, f"{action_cn}策略为[必须人工审批]，已转入人工审批台"

    return False, f"{action_cn}策略未命中有效放行规则，默认安全拦截"


def chat_exchange_wechat(cfg, company, reply_text="", force=False, hi_flag=False):
    """按公司名点开会话并点击【换微信】官方按钮。写台账 action=exchange_wechat。
    受统一 privacy_policy、岗位适配门禁（_job_fit_gate）与在线回复安全门禁保护。"""
    perm_ok, perm_reason = check_privacy_permission("exchange_wechat", cfg, hi_flag=hi_flag, force=force)
    if not perm_ok:
        ledger.append({"action": "exchange_wechat", "status": "blocked_privacy_policy",
                       "company": company, "reason": perm_reason})
        return {"ok": False, "blocked": "privacy_policy", "company": company, "reason": perm_reason}

    if not force:
        gate = _job_fit_gate(cfg, company)
        if not gate["allow"]:
            ledger.append({"action": "exchange_wechat", "status": "blocked_job_fit",
                           "company": company,
                           "reason": "%s: %s" % (gate["attribution"], gate["detail"])})
            return {"ok": False, "blocked": "job_fit", "company": company,
                    "attribution": gate["attribution"], "detail": gate["detail"]}
    if not force and not cfg.get("online_reply_enabled", True):
        ledger.append({"action": "exchange_wechat", "status": "intercepted_safety_gate", "company": company,
                       "note": "online_reply_enabled 为 false，线上换微信已被全局安全门禁拦截"})
        return {"ok": False, "intercepted": True, "reason": "online_reply_enabled_false", "company": company}
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.exchange_wechat_via_chat(sess, company)
        text_res = None
        if reply_text and r.get("status") in ("ok", "already_sent"):
            try:
                time.sleep(1.0)
                tr = greeter.send_message_in_current_conv(sess, reply_text)
                text_res = {"status": "ok", "result": tr}
            except Exception as te:
                text_res = {"status": "failed", "error": str(te)[:200]}
        ledger.append({"action": "exchange_wechat", "status": r.get("status", "ok"),
                       "company": company, "conv": r.get("conv"),
                       "text_sent": bool(text_res and text_res.get("status") == "ok")})
        is_ok = (r.get("status") == "ok") or (r.get("status") == "already_sent")
        return {"ok": is_ok, "status": r.get("status"), "company": company, "result": r, "text_result": text_res}
    except Exception as e:
        ledger.append({"action": "exchange_wechat", "status": "failed",
                       "company": company, "error": str(e)[:200]})
        return {"ok": False, "company": company, "error": str(e)[:300]}
    finally:
        sess.close_tab()
        sess.close()


def chat_send_resume(cfg, company, reply_text="", force=False, hi_flag=False):
    """按公司名点开会话并点击【发简历】官方按钮。写台账 action=send_resume。
    受统一 privacy_policy、岗位适配门禁（_job_fit_gate）与在线回复安全门禁保护。"""
    perm_ok, perm_reason = check_privacy_permission("send_resume", cfg, hi_flag=hi_flag, force=force)
    if not perm_ok:
        ledger.append({"action": "send_resume", "status": "blocked_privacy_policy",
                       "company": company, "reason": perm_reason})
        return {"ok": False, "blocked": "privacy_policy", "company": company, "reason": perm_reason}

    if not force:
        gate = _job_fit_gate(cfg, company)
        if not gate["allow"]:
            ledger.append({"action": "send_resume", "status": "blocked_job_fit",
                           "company": company,
                           "reason": "%s: %s" % (gate["attribution"], gate["detail"])})
            return {"ok": False, "blocked": "job_fit", "company": company,
                    "attribution": gate["attribution"], "detail": gate["detail"]}
    if not force and not cfg.get("online_reply_enabled", True):
        ledger.append({"action": "send_resume", "status": "intercepted_safety_gate", "company": company,
                       "note": "online_reply_enabled 为 false，线上发简历已被全局安全门禁拦截"})
        return {"ok": False, "intercepted": True, "reason": "online_reply_enabled_false", "company": company}
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.send_resume_via_chat(sess, company)
        text_res = None
        if reply_text and r.get("status") in ("ok", "already_sent"):
            try:
                time.sleep(1.0)
                tr = greeter.send_message_in_current_conv(sess, reply_text)
                text_res = {"status": "ok", "result": tr}
            except Exception as te:
                text_res = {"status": "failed", "error": str(te)[:200]}
        ledger.append({"action": "send_resume", "status": r.get("status", "ok"),
                       "company": company, "conv": r.get("conv"),
                       "text_sent": bool(text_res and text_res.get("status") == "ok")})
        is_ok = (r.get("status") == "ok") or (r.get("status") == "already_sent")
        return {"ok": is_ok, "status": r.get("status"), "company": company, "result": r, "text_result": text_res}
    except Exception as e:
        ledger.append({"action": "send_resume", "status": "failed",
                       "company": company, "error": str(e)[:200]})
        return {"ok": False, "company": company, "error": str(e)[:300]}
    finally:
        sess.close_tab()
        sess.close()


def chat_agree_wechat(cfg, company, reply_text="", force=False, hi_flag=False):
    """按公司名点开会话并点击【同意交换微信】官方按钮。写台账 action=agree_wechat。
    受统一 privacy_policy、岗位适配门禁（_job_fit_gate）与在线回复安全门禁保护。若传入 reply_text，在同会话中伴随发送短文本。"""
    perm_ok, perm_reason = check_privacy_permission("agree_wechat", cfg, hi_flag=hi_flag, force=force)
    if not perm_ok:
        ledger.append({"action": "agree_wechat", "status": "blocked_privacy_policy",
                       "company": company, "reason": perm_reason})
        return {"ok": False, "blocked": "privacy_policy", "company": company, "reason": perm_reason}

    if not force:
        gate = _job_fit_gate(cfg, company)
        if not gate["allow"]:
            ledger.append({"action": "agree_wechat", "status": "blocked_job_fit",
                           "company": company,
                           "reason": "%s: %s" % (gate["attribution"], gate["detail"])})
            return {"ok": False, "blocked": "job_fit", "company": company,
                    "attribution": gate["attribution"], "detail": gate["detail"]}
    if not force and not cfg.get("online_reply_enabled", True):
        ledger.append({"action": "agree_wechat", "status": "intercepted_safety_gate", "company": company,
                       "note": "online_reply_enabled 为 false，线上同意换微信已被全局安全门禁拦截"})
        return {"ok": False, "intercepted": True, "reason": "online_reply_enabled_false", "company": company}
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.agree_wechat_via_chat(sess, company)
        text_res = None
        if reply_text and r.get("status") in ("ok", "already_agreed"):
            try:
                time.sleep(1.0)
                tr = greeter.send_message_in_current_conv(sess, reply_text)
                text_res = {"status": "ok", "result": tr}
            except Exception as te:
                text_res = {"status": "failed", "error": str(te)[:200]}
        ledger.append({"action": "agree_wechat", "status": r.get("status", "ok"),
                       "company": company, "conv": r.get("conv"),
                       "text_sent": bool(text_res and text_res.get("status") == "ok")})
        is_ok = (r.get("status") == "ok") or (r.get("status") == "already_agreed")
        return {"ok": is_ok, "status": r.get("status"), "company": company, "result": r, "text_result": text_res}
    except Exception as e:
        ledger.append({"action": "agree_wechat", "status": "failed",
                       "company": company, "error": str(e)[:200]})
        return {"ok": False, "company": company, "error": str(e)[:300]}
    finally:
        sess.close_tab()
        sess.close()



def chat_job_detail(cfg, company=None, fetch_jd=True, fetch_history=True):
    """获取会话关联岗位的完整元数据与JD详情（只读操作，零发送）。
    若指定 company 则点开该公司的会话；若未指定则直接提取当前激活会话。
    若 fetch_jd=True 且拿到 href，则在独立后台标签页中加载并抓取完整的岗位职责与任职要求。
    若 fetch_history=True，则顺路提取该会话最近聊天历史（同一次点开会话，零额外开页），
    返回 history=[{role: me|hr|system, text}]，供 Agent 把握上下文避免重复作答。
    供 Agent 在制定回复策略或'见人下菜碟'时全面研判岗位含金量。"""
    company_name = (company or "").strip()
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab(rawcdp.BASE + "/web/geek/chat")
        for _ in range(12):
            time.sleep(1)
            st = sess.state()
            if st and not st.get("blank") and st.get("bodyLen", 0) > 100:
                break
        if company_name:
            info, head = greeter._open_conversation_input(sess, company_name)
            if not info:
                return {"ok": False, "company": company_name, "error": "conversation not found for %r" % company_name}
        else:
            info, head = greeter._open_conversation_input(sess, "")
            if not info:
                return {"ok": False, "company": "", "error": "no conversation found in chat inbox"}

        job_info = greeter.get_active_conversation_job(sess)
        if not job_info:
            return {"ok": False, "company": company_name, "error": "no active job found in conversation"}

        # 聊天历史提取（增强项：失败/为空不阻塞，返回空列表）
        history = []
        if fetch_history:
            try:
                history = greeter.get_active_conversation_history(sess) or []
            except Exception:
                history = []

        sess.close_tab()

        jd_text = ""
        boss_active = -1
        if fetch_jd and job_info.get("href"):
            sess.open_tab()
            jd_text, boss_active = sess.fetch_detail(job_info)
            sess.close_tab()

        return {
            "ok": True,
            "company": company_name or job_info.get("companyName") or "",
            "history": history,
            "job": {
                "title": job_info.get("positionName") or job_info.get("title") or "",
                "company": job_info.get("companyName") or job_info.get("brandName") or "",
                "salary": job_info.get("salaryDesc") or "",
                "city": job_info.get("locationName") or "",
                "degree": job_info.get("degreeName") or "",
                "experience": job_info.get("experienceName") or "",
                "href": job_info.get("href") or "",
                "encryptJobId": job_info.get("encryptJobId") or "",
                "jd_text": jd_text,
                "boss_active": boss_active
            }
        }
    except Exception as e:
        return {"ok": False, "company": company_name, "error": str(e)[:300]}
    finally:
        sess.close_tab()
        sess.close()


def execute_jobs(cfg, g, jobs, max_count=10, dry_run=False):
    """带护栏执行打招呼（裸CDP版）。任何风控信号 → 立即熔断并写台账。
    F01: dry_run=True 时杜绝任何 CDP 标签页开启与外部写操作。"""
    jobs = filter_greeted(jobs)
    if dry_run:
        sim_count = min(len(jobs), max_count)
        print(f"  [投递执行·模拟模式] 待投递岗位池: {len(jobs)} 个 (仿真执行 {sim_count} 个，零外部写操作)")
        return {
            "executed": 0,
            "simulated": sim_count,
            "results": [{"title": j.get("title"), "company": j.get("company"), "dry_run": True} for j in jobs[:max_count]],
            "guard": g.summary(),
            "dry_run": True
        }
    done, results = 0, []
    is_silent = bool(((cfg or {}).get("browser") or {}).get("silent_mode", True))
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    print(f"  [投递执行] 待投递岗位池: {len(jobs)} 个 (本次上限: {max_count})")
    try:
        sess.open_tab(background=is_silent)
        for idx, job in enumerate(jobs, 1):
            if done >= max_count:
                break
            city = job.get("city") or "杭州"
            company = job.get("company") or ""
            title = job.get("title") or ""
            print(f"\n  [{idx}/{len(jobs)}] 正在处理: {company} - {title} ({city}, {job.get('score')}分)...")
            if hasattr(g, "acquire_greet_slot"):
                ok, info = g.acquire_greet_slot(city)
            else:
                ok, info = g.check_greet(city)
            if not ok:
                print(f"  [{idx}/{len(jobs)}] 护栏熔断或配额不足拦截: {info}")
                results.append({"stopped": info})
                break
            slot_acquired = True
            sent_successfully = False
            try:
                href = job.get("href") or ""
                if not href:
                    raise RuntimeError("job has no href")
                full = href if href.startswith("http") else rawcdp.BASE + href
                st = None
                for _attempt in (1, 2):
                    sess.nav(full)
                    st = sess.wait_ready(want_cards=False, timeout_s=12)
                    if st:
                        break
                    time.sleep(2)
                if not st:
                    raise RuntimeError("detail page not ready: %s" % full[:80])
                greet_res = greeter.send_greeting_raw(sess, job, cfg)
                greeting_status = (greet_res or {}).get("status", "ok")
                greeting_text_sent = (greet_res or {}).get("greeting") or ""
                if greeting_status == "unknown":
                    if hasattr(g, "release_greet_slot"):
                        try:
                            g.release_greet_slot(city)
                        except Exception:
                            pass
                    ledger.append({"action": "greet", "status": "unknown", "city": city,
                                   "title": job.get("title"), "company": job.get("company"),
                                   "href": href, "score": job.get("score"),
                                   "greeting": greeting_text_sent,
                                   "note": "投递状态无法核验(已挂起并回滚槽位)"})
                    results.append({"ok": False, "status": "unknown", "title": job.get("title"),
                                    "note": "投递状态无法核验(已挂起)"})
                    print(f"  [{idx}/{len(jobs)}] 投递状态未知(已挂起并回滚槽位)")
                else:
                    sent_successfully = True
                    if not hasattr(g, "acquire_greet_slot"):
                        g.record_greet(city)
                    ledger.append({"action": "greet", "status": "ok", "city": city,
                                   "title": job.get("title"), "company": job.get("company"),
                                   "href": href, "score": job.get("score"),
                                   "greeting": greeting_text_sent})
                    done += 1
                    results.append({"ok": True, "title": job.get("title"), "company": job.get("company"),
                                    "greeting": greeting_text_sent})
                    print(f"  [{idx}/{len(jobs)}] 实弹送达成功! 专属开场白: {greeting_text_sent}")
            except browser.RiskControl as e:
                g.pause("security_verification: %s" % e)
                ledger.append({"action": "greet", "status": "risk_paused", "reason": str(e), "title": job.get("title")})
                results.append({"PAUSED": str(e)})
                print(f"\n🚨 [风控熔断] 岗位打招呼遭遇 BOSS 安全验证/风控拦截: {e}！")
                print("👉 已紧急挂起所有自动化请求，并将 Chrome 调至前台可视，等待人工完成安全验证！\n")
                try:
                    sess.restore_window()
                    sess.activate_tab()
                except Exception:
                    pass
                try:
                    feishu_bot.send_text_or_webhook(
                        cfg,
                        f"🚨【BOSS直聘风控安全验证预警】\n自动打招呼时遭遇平台安全验证/风控拦截，所有后续投递已自动熔断挂起！\n请前往桌面在已调至前台的 Chrome 浏览器中手动完成滑块验证后，在 Web 控制台点击恢复运行。"
                    )
                except Exception:
                    pass
                break
            except Exception as e:
                if not sent_successfully and hasattr(g, "release_greet_slot"):
                    try:
                        g.release_greet_slot(city)
                    except Exception:
                        pass
                ledger.append({"action": "greet", "status": "failed", "error": str(e)[:200],
                               "title": job.get("title"), "company": job.get("company"), "href": job.get("href")})
                results.append({"ok": False, "title": job.get("title"), "error": str(e)[:120]})
                print(f"  [{idx}/{len(jobs)}] 投递异常（已释放预占）: {e}")
            browser.human_wait(cfg, "greet")
    finally:
        sess.close_tab()
        sess.close()
    return {"executed": done, "results": results, "guard": g.summary()}


def execute_plan(cfg, g, max_count=10, plan_file="plan.json"):
    path = plan_file if os.path.isabs(plan_file) else cfgmod.state_path(plan_file)
    if not os.path.exists(path):
        return {"error": "plan not found, run build_plan first"}
    with open(path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    return execute_jobs(cfg, g, plan, max_count)


def probe_readonly(cfg, steps=None):
    """只读频率探针（裸CDP版，仅测试账号）：阶梯频率搜索，定位风控触发档位。不产生任何沟通。"""
    steps = steps or [[3.0, 6], [2.0, 6], [1.0, 6], [0.5, 6]]
    kws = ["AI产品经理", "AI产品", "Agent 产品", "大模型 产品"]
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    log = []
    ki = 0
    try:
        sess.open_tab()
        for step in steps:
            interval, times = float(step[0]), int(step[1])
            for i in range(times):
                try:
                    jobs = sess.search_jobs(kws[ki % len(kws)], "101210100", (i % 3) + 1)
                except browser.RiskControl as e:
                    return {"verdict": "风控触发于 interval=%ss 档（第%d次请求）" % (interval, i + 1),
                            "failed_step": {"interval": interval, "index": i + 1}, "log": log, "risk": str(e)}
                ki += 1
                log.append({"interval": interval, "i": i + 1, "ok": True, "found": len(jobs)})
                time.sleep(interval)
    finally:
        sess.close_tab()
        sess.close()
    return {"verdict": "全部档位未触发风控信号", "log": log}


def fetch_campus_portal_companies(cfg):
    """访问 /school/ 校园招聘专区，抓取当前名企直聘专场与2027校招/实习项目。"""
    sess = rawcdp.RawCDP(cfg.get("cdp_endpoint", "http://127.0.0.1:9335"))
    try:
        sess.open_tab()
        return sess.fetch_campus_recommendations()
    finally:
        try:
            sess.close_tab()
            sess.close()
        except Exception:
            pass
