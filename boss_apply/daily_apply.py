"""每日自动智能投递管线（2026-09-09）。

三阶段管线：
  Phase 1 — 广撒网搜索（全城市×全关键词×N页，零发送只读）
  Phase 2 — JD 精读采集（fetch_detail 抓完整 JD，拟人间隔）
  Phase 3 — LLM 综合择优（批量打分 + 全局排序 + verdict 门禁）
  Phase 4 — 择优投递（Top N 打招呼，护栏配额保护）

与 daemon_auto_reply 守护进程集成：活跃窗口内每日一次，scan_done_today 标记防重。
"""
import json
import os
import time

from . import (browser, citycodes, config as cfgmod, guard as guardmod,
               ledger, llm_match, rawcdp, scorer)
from . import flows


# ---------------------------------------------------------------------------
# Phase 1+2: 广撒网搜索 + JD 精读
# ---------------------------------------------------------------------------

def collect_candidates(cfg, g, max_pages=3, fetch_detail=True):
    """遍历全部生效城市 × 关键词 × N 页，收集通过硬过滤的候选岗位。
    fetch_detail=True 时对每个候选逐一抓取完整 JD（拟人间隔）。
    返回 (candidates_list, stats_dict)。
    candidates_list 每项为 {job, detail, kw, city, base_score, base_reason}。"""
    eff = flows.effective_cities(cfg)
    kws = flows.effective_keywords(cfg)
    prefs = flows._prefs(cfg)
    job_mode = cfg.get("job_mode", "intern")

    # 已打过招呼的 href 集合
    greeted_hrefs = {r.get("href") for r in ledger.load_all()
                     if r.get("action") == "greet" and r.get("status") == "ok"}

    seen_hrefs = set()
    candidates = []
    stats = {"cities": 0, "keywords": len(kws), "pages_searched": 0,
             "raw_found": 0, "after_dedup": 0, "after_hard_filter": 0,
             "details_fetched": 0, "errors": []}

    for city_info in eff["cities"]:
        city = city_info["name"]
        code = city_info["code"]
        stats["cities"] += 1
        sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
        try:
            sess.open_tab()
            for kw in kws:
                for p in range(1, max_pages + 1):
                    ok, info = g.check_search()
                    if not ok:
                        stats["errors"].append("search limit: %s" % info)
                        break
                    # experience 门禁参数
                    if job_mode == "intern":
                        exp_code = "108"
                    elif job_mode == "campus":
                        exp_code = "102"
                    elif job_mode == "mix":
                        exp_code = "108" if p % 2 == 1 else "102"
                    else:
                        exp_code = None
                    try:
                        jobs = sess.search_jobs(kw, code, p, experience=exp_code)
                    except browser.RiskControl as e:
                        g.pause("risk: %s" % e)
                        stats["errors"].append("risk_control: %s" % str(e)[:100])
                        break
                    except Exception as e:
                        stats["errors"].append("search_error: %s" % str(e)[:100])
                        continue
                    g.record_search()
                    stats["pages_searched"] += 1
                    stats["raw_found"] += len(jobs)

                    for job in jobs:
                        href = job.get("href") or ""
                        # 去重：同 href 只保留第一次
                        if href in seen_hrefs:
                            continue
                        seen_hrefs.add(href)
                        stats["after_dedup"] += 1
                        # 已打过招呼
                        if href in greeted_hrefs:
                            continue
                        # 排斥偏好硬否决
                        vetoed, veto_word = flows.avoid_veto(job, prefs["avoid_jobs"])
                        if vetoed:
                            continue
                        # 基础硬过滤打分（kill 词、届别、薪资、活跃度）
                        s, why = scorer.score(job, "", cfg)
                        if s <= 0:
                            continue
                        stats["after_hard_filter"] += 1
                        job["job_mode"] = job_mode
                        job["experience"] = exp_code
                        job["city"] = city
                        candidates.append({
                            "job": job, "detail": "", "kw": kw,
                            "city": city, "base_score": s, "base_reason": why,
                        })
                    browser.human_wait(cfg, "page")
        finally:
            try:
                sess.close_tab()
                sess.close()
            except Exception:
                pass

    # Phase 2: JD 精读（可选）
    if fetch_detail and candidates:
        sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
        try:
            sess.open_tab()
            for item in candidates:
                try:
                    detail, active = sess.fetch_detail(item["job"])
                    item["detail"] = detail or ""
                    if active is not None and active >= 0:
                        item["job"]["boss_active"] = active
                    stats["details_fetched"] += 1
                except Exception:
                    item["detail"] = ""
                browser.human_wait(cfg, "page")
        finally:
            try:
                sess.close_tab()
                sess.close()
            except Exception:
                pass

    return candidates, stats


# ---------------------------------------------------------------------------
# Phase 3: LLM 综合择优
# ---------------------------------------------------------------------------

def rank_and_plan(candidates, cfg, top_n=None):
    """LLM 批量打分 → 全局排序 → 截取 Top N → 写 daily_plan.json。
    LLM 不可用时回退 base_score 排序。
    返回 (plan_list, rank_stats)。"""
    if top_n is None:
        top_n = int((cfg.get("daemon") or {}).get("apply_top_n", 50))
    min_verdict_order = {"veto": 0, "low": 1, "medium": 2, "high": 3}
    min_verdict = str((cfg.get("job_fit_gate") or {}).get("min_verdict") or "medium")
    min_order = min_verdict_order.get(min_verdict, 2)

    # 构造 LLM 输入
    llm_jobs = [{**item["job"], "detail": item["detail"]} for item in candidates]
    llm_res = llm_match.match_batch(llm_jobs, cfg)

    plan = []
    for i, item in enumerate(candidates):
        job = item["job"]
        entry = {
            "title": job.get("title") or "",
            "company": job.get("company") or "",
            "salary": job.get("salary") or "",
            "city": item["city"],
            "href": job.get("href") or "",
            "tags": job.get("tags") or "",
            "kw": item["kw"],
            "job_mode": job.get("job_mode") or "",
            "experience": job.get("experience") or "",
            "detail_head": (item["detail"] or "")[:300],
        }
        if llm_res and i in llm_res:
            m = llm_res[i]
            entry["score"] = m["score"]
            entry["verdict"] = m.get("verdict") or "low"
            entry["reason"] = m.get("reason") or ""
            entry["score_source"] = "llm"
        else:
            entry["score"] = item["base_score"]
            entry["verdict"] = "medium" if item["base_score"] >= 8 else "low"
            entry["reason"] = item["base_reason"]
            entry["score_source"] = "keywords"

        # verdict 门禁
        v_order = min_verdict_order.get(entry["verdict"], 0)
        if v_order < min_order:
            continue
        plan.append(entry)

    # 全局排序
    plan.sort(key=lambda x: -x["score"])
    plan = plan[:top_n]

    # 写入 daily_plan.json
    plan_path = cfgmod.state_path("daily_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    rank_stats = {
        "total_candidates": len(candidates),
        "after_verdict_gate": len([e for e in plan]),
        "plan_count": len(plan),
        "llm_available": llm_res is not None,
        "plan_path": plan_path,
    }
    return plan, rank_stats


# ---------------------------------------------------------------------------
# Phase 4: 择优投递
# ---------------------------------------------------------------------------

def execute_daily_plan(cfg, g, top_n=None):
    """读取 daily_plan.json → execute_jobs 投递。返回 execute 结果。"""
    if top_n is None:
        top_n = int((cfg.get("daemon") or {}).get("apply_top_n", 50))
    plan_path = cfgmod.state_path("daily_plan.json")
    if not os.path.exists(plan_path):
        return {"error": "daily_plan.json not found, run rank_and_plan first"}
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    plan = plan[:top_n]
    if not plan:
        return {"executed": 0, "results": [], "guard": g.summary(), "note": "plan empty"}

    # 写台账：投递计划生成
    ledger.append({
        "action": "daily_plan_generated",
        "count": len(plan),
        "top3": [{"title": j.get("title"), "company": j.get("company"),
                  "score": j.get("score")} for j in plan[:3]],
    })

    return flows.execute_jobs(cfg, g, plan, max_count=top_n)


# ---------------------------------------------------------------------------
# 入口函数：串联四阶段
# ---------------------------------------------------------------------------

def scan_and_apply_daily(cfg, dry_run=False):
    """每日自动投递入口：搜索 → 精读 → 择优 → 投递。
    dry_run=True 时只跑 Phase 1~3（生成计划），不执行 Phase 4 投递。
    返回完整的管线执行报告。"""
    daemon_cfg = cfg.get("daemon") or {}
    max_pages = int(daemon_cfg.get("apply_max_pages", 3))
    top_n = int(daemon_cfg.get("apply_top_n", 15))
    fetch_detail = bool(daemon_cfg.get("apply_fetch_detail", True))

    g = guardmod.Guard(cfg)
    report = {"phase": "init", "dry_run": dry_run}

    # Phase 1+2
    print("  [每日投递 Phase 1+2] 广撒网搜索 + JD 精读...")
    try:
        candidates, collect_stats = collect_candidates(
            cfg, g, max_pages=max_pages, fetch_detail=fetch_detail)
        report["collect"] = collect_stats
        report["candidates_count"] = len(candidates)
        print(f"  [Phase 1+2 完成] 候选池: {len(candidates)} 个岗位 "
              f"(搜索 {collect_stats['pages_searched']} 页, "
              f"原始 {collect_stats['raw_found']}, "
              f"JD精读 {collect_stats['details_fetched']})")
    except Exception as e:
        report["phase"] = "collect_error"
        report["error"] = str(e)[:300]
        print(f"  [Phase 1+2 异常] {e}")
        return report

    if not candidates:
        report["phase"] = "no_candidates"
        print("  [Phase 1+2] 无候选岗位通过硬过滤，本日投递结束。")
        ledger.append({"action": "daily_apply_summary", "phase": "no_candidates",
                       "stats": collect_stats, "dry_run": dry_run})
        return report

    # Phase 3
    print("  [每日投递 Phase 3] LLM 综合择优打分...")
    try:
        plan, rank_stats = rank_and_plan(candidates, cfg, top_n=top_n)
        report["rank"] = rank_stats
        report["plan_count"] = len(plan)
        print(f"  [Phase 3 完成] 投递计划: {len(plan)} 个岗位 "
              f"(LLM: {'可用' if rank_stats['llm_available'] else '不可用，回退词表分'})")
        if plan:
            top3_str = " | ".join("%s-%s(%s分)" % (j.get("company"), j.get("title"), j.get("score")) for j in plan[:3])
            print("  [Top 3] %s" % top3_str)
    except Exception as e:
        report["phase"] = "rank_error"
        report["error"] = str(e)[:300]
        print(f"  [Phase 3 异常] {e}")
        return report

    if not plan:
        report["phase"] = "no_qualified"
        print("  [Phase 3] 无岗位通过 verdict 门禁，本日投递结束。")
        ledger.append({"action": "daily_apply_summary", "phase": "no_qualified",
                       "stats": {**collect_stats, **rank_stats}, "dry_run": dry_run})
        return report

    # Phase 4
    if dry_run:
        report["phase"] = "dry_run_complete"
        print(f"  [DRY-RUN] 投递计划已生成（{len(plan)} 个），仿真模式不执行实际投递。")
        ledger.append({"action": "daily_apply_summary", "phase": "dry_run_complete",
                       "plan_count": len(plan), "dry_run": True})
        return report

    print(f"  [每日投递 Phase 4] 择优投递 Top {len(plan)} 个岗位...")
    try:
        exec_res = execute_daily_plan(cfg, g, top_n=top_n)
        report["execute"] = exec_res
        report["phase"] = "complete"
        executed = exec_res.get("executed", 0)
        print(f"  [Phase 4 完成] 实际投递: {executed} 个岗位")
        ledger.append({
            "action": "daily_apply_summary", "phase": "complete",
            "plan_count": len(plan), "executed": executed,
            "dry_run": False,
        })
    except Exception as e:
        report["phase"] = "execute_error"
        report["error"] = str(e)[:300]
        print(f"  [Phase 4 异常] {e}")

    return report
