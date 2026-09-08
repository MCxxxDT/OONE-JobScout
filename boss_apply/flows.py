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
    avoid = set(prefs["avoid_cities"])
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
                try:
                    jobs = sess.search_jobs(kw, cm[city]["code"], p)
                except browser.RiskControl as e:
                    g.pause("risk: %s" % e)
                    return {"city": city, "found": found, "scored": scored, "PAUSED": str(e)}
                g.record_search()
                found += len(jobs)
                for job in jobs:
                    # 偏好排斥岗位硬否决（在打分前，LLM 匹配也不会触达）
                    vetoed, veto_word = avoid_veto(job, prefs["avoid_jobs"])
                    if vetoed:
                        ledger.append({
                            "action": "scan", "city": city, "keyword": kw, "score": 0,
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
SYSTEM_MSG_RE = re.compile(
    r"^您的附件简历"
    r"|已发送给(?:Boss|对方)"
    r"|^对方已同意"
    r"|(?:成功)?(?:同意|拒绝)(?:了)?对方交换(?:微信|电话|联系方式)"
    r"|(?:微信|电话)(?:交换|已交换)"
    r"|^已(?:同意|拒绝)和对方交换"
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
            "needs_human": greeter.privacy_blocked(preview)}


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


def chat_reply(cfg, company, text):
    """按公司名回复 HR 一条消息（经 greeter.send_message_via_chat），写台账 action=reply。
    隐私红线：文案含联系方式意图 → 拒绝发送（blocked_privacy），转人工。
    公司未在会话列表命中时 greeter 层直接抛异常中止，绝不退回最新会话（审计补丁#3）。"""
    if greeter.privacy_blocked(text):
        ledger.append({"action": "reply", "status": "blocked_privacy", "company": company,
                       "text_head": (text or "")[:120],
                       "note": "文案含联系方式（电话/微信等），拒绝代发，转人工回复"})
        return {"ok": False, "blocked": "privacy", "company": company}
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.send_message_via_chat(sess, company, text)
        ledger.append({"action": "reply", "status": "ok", "company": company,
                       "text_head": (text or "")[:120], "conv": r.get("conv")})
        return {"ok": True, "company": company, "result": r}
    except Exception as e:
        ledger.append({"action": "reply", "status": "failed", "company": company,
                       "error": str(e)[:200]})
        return {"ok": False, "company": company, "error": str(e)[:300]}
    finally:
        sess.close_tab()
        sess.close()


def chat_exchange_wechat(cfg, company):
    """按公司名点开会话并点击【换微信】官方按钮。写台账 action=exchange_wechat。
    2026-09-08：greeter 层已加固（受信任点击+弹窗标题校验），no_dialog/no_verify
    状态如实返回 ok=False（不再谎报成功）。
    2026-09-09：岗位适配门禁——执行前 judge_job_fit 判定（prefs 规则优先/LLM 语义兜底/
    零硬编码），拒绝时归因留痕（job_fit_gate + status=blocked_job_fit），防销售地推岗误发。"""
    gate = _job_fit_gate(cfg, company)
    if not gate["allow"]:
        ledger.append({"action": "exchange_wechat", "status": "blocked_job_fit",
                       "company": company,
                       "reason": "%s: %s" % (gate["attribution"], gate["detail"])})
        return {"ok": False, "blocked": "job_fit", "company": company,
                "attribution": gate["attribution"], "detail": gate["detail"]}
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.exchange_wechat_via_chat(sess, company)
        ledger.append({"action": "exchange_wechat", "status": r.get("status", "ok"),
                       "company": company, "conv": r.get("conv")})
        return {"ok": r.get("status") == "ok", "company": company, "result": r}
    except Exception as e:
        ledger.append({"action": "exchange_wechat", "status": "failed",
                       "company": company, "error": str(e)[:200]})
        return {"ok": False, "company": company, "error": str(e)[:300]}
    finally:
        sess.close_tab()
        sess.close()


def chat_send_resume(cfg, company):
    """按公司名点开会话并点击【发简历】官方按钮。写台账 action=send_resume。
    2026-09-08：greeter 层已加固，no_verify 状态如实返回 ok=False。
    2026-09-09：岗位适配门禁——与换微信同链路（judge_job_fit），拒绝归因留痕。"""
    gate = _job_fit_gate(cfg, company)
    if not gate["allow"]:
        ledger.append({"action": "send_resume", "status": "blocked_job_fit",
                       "company": company,
                       "reason": "%s: %s" % (gate["attribution"], gate["detail"])})
        return {"ok": False, "blocked": "job_fit", "company": company,
                "attribution": gate["attribution"], "detail": gate["detail"]}
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.send_resume_via_chat(sess, company)
        ledger.append({"action": "send_resume", "status": r.get("status", "ok"),
                       "company": company, "conv": r.get("conv")})
        return {"ok": r.get("status") == "ok", "company": company, "result": r}
    except Exception as e:
        ledger.append({"action": "send_resume", "status": "failed",
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


def execute_jobs(cfg, g, jobs, max_count=10):
    """带护栏执行打招呼（裸CDP版）。任何风控信号 → 立即熔断并写台账。"""
    jobs = filter_greeted(jobs)
    done, results = 0, []
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    try:
        sess.open_tab()
        for job in jobs:
            if done >= max_count:
                break
            city = job.get("city") or "杭州"
            ok, info = g.check_greet(city)
            if not ok:
                results.append({"stopped": info})
                break
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
                greeter.send_greeting_raw(sess, job, cfg)
                g.record_greet(city)
                ledger.append({"action": "greet", "status": "ok", "city": city,
                               "title": job.get("title"), "company": job.get("company"),
                               "href": href, "score": job.get("score")})
                done += 1
                results.append({"ok": True, "title": job.get("title"), "company": job.get("company")})
            except browser.RiskControl as e:
                g.pause("risk: %s" % e)
                ledger.append({"action": "greet", "status": "risk_paused", "reason": str(e), "title": job.get("title")})
                results.append({"PAUSED": str(e)})
                break
            except Exception as e:
                ledger.append({"action": "greet", "status": "failed", "error": str(e)[:200],
                               "title": job.get("title"), "company": job.get("company"), "href": job.get("href")})
                results.append({"ok": False, "title": job.get("title"), "error": str(e)[:120]})
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
