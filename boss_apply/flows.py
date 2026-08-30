"""高层流程（scan / plan / execute / probe），server 与 scripts 共用。

反爬升级（2026-08-30）：scan/probe 全部改走裸CDP（rawcdp），
因为 playwright 的CDP会话特征会被BOSS安全JS检测并清空DOM。
execute（打招呼）暂保留 playwright 路径，T3 前需同样改造。
"""
import base64
import json
import os
import time

from . import browser, config as cfgmod, guard, greeter, ledger, rawcdp, scorer


def scan_city(cfg, g, city, keywords=None, max_pages=2, fetch_detail=True):
    """只读扫描一个城市（裸CDP）：搜索→(可选)抓详情→打分→写台账。不发送任何沟通。"""
    cm = {c["name"]: c for c in cfg["cities"]}
    if city not in cm:
        return {"error": "unknown city: %s" % city, "available": list(cm)}
    kws = keywords or cfg["keywords"]
    sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
    found, scored = 0, 0
    try:
        sess.open_tab()
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
                    detail = ""
                    if fetch_detail:
                        try:
                            detail, active = sess.fetch_detail(job)
                            if active is not None and active >= 0:
                                job["boss_active"] = active  # 详情页补验活跃度
                        except Exception:
                            detail = ""  # 详情失败降级：仅用列表信息打分
                    s, why = scorer.score(job, detail, cfg)
                    ledger.append({
                        "action": "scan", "city": city, "keyword": kw, "score": s, "reason": why,
                        "title": job.get("title"), "company": job.get("company"),
                        "salary": job.get("salary"), "href": job.get("href"),
                        "tags": job.get("tags"), "boss_active": job.get("boss_active"),
                        "detail_head": (detail or "")[:200],
                    })
                    if s > 0:
                        scored += 1
                    browser.human_wait(cfg, "page")
    finally:
        sess.close_tab()
        sess.close()
    return {"city": city, "found": found, "scored": scored, "guard": g.summary()}


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
