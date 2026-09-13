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

    is_silent = bool((cfg.get("browser") or {}).get("silent_mode", True))
    for city_info in eff["cities"]:
        city = city_info["name"]
        code = city_info["code"]
        stats["cities"] += 1
        print(f"  [城市扫描] 正在扫描城市: {city} (关键词: {len(kws)} 个, 最大页数: {max_pages} 页)...")
        sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
        try:
            sess.open_tab(background=is_silent)
            for kw in kws:
                for p in range(1, max_pages + 1):
                    ok, info = g.check_search()
                    if not ok:
                        print(f"    ! [{city}] 搜索门禁拦截: {info}")
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
                        print(f"    ! [{city}] 遭遇风控: {e}")
                        break
                    except Exception as e:
                        stats["errors"].append("search_error: %s" % str(e)[:100])
                        continue
                    g.record_search()
                    stats["pages_searched"] += 1
                    stats["raw_found"] += len(jobs)
                    print(f"    > [{city}] 关键词: {kw} (第 {p} 页) -> 发现 {len(jobs)} 个岗位")

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

                        # 阶段 A：硬性资格门禁（公司黑名单、kill 词、岗位门槛、活跃度、薪资、届别等）
                        is_eligible, inelig_reason = scorer.check_eligibility(job, "", cfg)
                        if not is_eligible:
                            continue

                        # 阶段 B：偏好与关键词相关度打分（即使零关键词，只要通过硬资格也保留供后续初筛）
                        s, why = scorer.calculate_matching_score(job, "", cfg)
                        stats["after_hard_filter"] += 1
                        job["job_mode"] = job_mode
                        job["experience"] = exp_code
                        job["city"] = city
                        try:
                            from .campus_engine import InternSpecExtractor
                            specs = InternSpecExtractor.extract_specs(
                                jd_text="",
                                tags=job.get("tags") or "",
                                title=job.get("title") or ""
                            )
                        except Exception:
                            specs = {}
                        job["campus_specs"] = specs
                        candidates.append({
                            "job": job, "detail": "", "kw": kw,
                            "city": city, "base_score": s, "base_reason": why,
                            "campus_specs": specs,
                            "is_eligible": True,
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
        top_n_cfg = int((cfg.get("daemon") or {}).get("apply_top_n", 50))
        max_details = max(75, top_n_cfg + 25)
        detail_targets = sorted(candidates, key=lambda x: -x["base_score"])[:max_details]
        print(f"  [Phase 2] 开始 JD 精读 (从 {len(candidates)} 个候选优先精读头部 {len(detail_targets)} 个岗位)...")
        sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
        try:
            sess.open_tab(background=is_silent)
            for idx, item in enumerate(detail_targets, 1):
                try:
                    detail, active = sess.fetch_detail(item["job"])
                    item["detail"] = detail or ""
                    if active is not None and active >= 0:
                        item["job"]["boss_active"] = active
                    # Module 2: JD 精读后使用完整 JD 全文重抽 campus_specs 并重算打分
                    try:
                        from .campus_engine import InternSpecExtractor
                        specs = InternSpecExtractor.extract_specs(
                            jd_text=detail or "",
                            tags=item["job"].get("tags") or "",
                            title=item["job"].get("title") or ""
                        )
                    except Exception:
                        specs = item.get("campus_specs") or {}
                    item["campus_specs"] = specs
                    item["job"]["campus_specs"] = specs

                    # 精读后再次核查硬性资格门禁（如详情页标注了届别排斥或活跃度超限）
                    new_eligible, new_inelig = scorer.check_eligibility(item["job"], detail or "", cfg)
                    if not new_eligible:
                        item["is_eligible"] = False
                        item["ineligible_reason"] = new_inelig
                        item["base_score"] = 0.0
                        item["base_reason"] = new_inelig
                    else:
                        new_s, new_why = scorer.calculate_matching_score(item["job"], detail or "", cfg)
                        item["is_eligible"] = True
                        item["base_score"] = new_s
                        item["base_reason"] = new_why
                    stats["details_fetched"] += 1
                    if idx % 10 == 0 or idx == len(detail_targets):
                        print(f"    > JD 精读进度: [{idx}/{len(detail_targets)}] ({item['job'].get('company')})")
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

def rank_and_plan(candidates, cfg, top_n=None, plan_file=None, dry_run=False):
    """LLM 批量打分 → 全局排序 → 截取 Top N → 写 daily_plan.json。
    绑定 plan_id 与时间戳，隔离 dry_run 计划。
    LLM 不可用时回退 base_score 排序。
    返回 (plan_list, rank_stats)。"""
    import datetime
    import uuid

    plan_id = str(uuid.uuid4())
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
        base_s = item.get("base_score", 0)
        base_why = item.get("base_reason", "")

        # 阶段 A 硬性资格门禁核查：若显式标记 veto、base_score 为 0 且带否决字样，或 check_eligibility 判定不符，不可被模型覆盖
        is_eligible = item.get("is_eligible", True)
        if item.get("veto", False) or (base_s <= 0 and any(k in str(base_why) for k in ("kill", "届别", "boss inactive", "salary out", "blacklist", "role-gate", "avoid_jobs", "不符"))):
            is_eligible = False

        if is_eligible:
            recheck_ok, recheck_reason = scorer.check_eligibility(job, item.get("detail", ""), cfg)
            if not recheck_ok:
                is_eligible = False
                base_why = recheck_reason

        # 硬条件否决候选彻底排除，杜绝进入投递计划
        if not is_eligible:
            continue

        specs = item.get("campus_specs") or job.get("campus_specs")
        if not specs:
            try:
                from .campus_engine import InternSpecExtractor
                specs = InternSpecExtractor.extract_specs(
                    jd_text=item.get("detail") or "",
                    tags=job.get("tags") or "",
                    title=job.get("title") or ""
                )
            except Exception:
                specs = {}

        entry = {
            "plan_id": plan_id,
            "created_at": created_at,
            "dry_run": dry_run,
            "title": job.get("title") or "",
            "company": job.get("company") or "",
            "salary": job.get("salary") or "",
            "city": item["city"],
            "href": job.get("href") or "",
            "tags": job.get("tags") or "",
            "kw": item["kw"],
            "job_mode": job.get("job_mode") or "",
            "experience": job.get("experience") or "",
            "campus_specs": specs,
            "detail_head": (item["detail"] or "")[:300],
            "eligible": True,
        }

        if llm_res and i in llm_res:
            m = llm_res[i]
            entry["score"] = m["score"]
            entry["verdict"] = m.get("verdict") or "low"
            entry["reason"] = m.get("reason") or ""
            entry["score_source"] = "llm"
        else:
            entry["score"] = base_s
            entry["verdict"] = "medium" if base_s >= 8 else ("low" if base_s > 0 else "zero_keyword")
            entry["reason"] = base_why
            entry["score_source"] = "keywords"

        # verdict 门禁
        v_order = min_verdict_order.get(entry["verdict"], 0)
        if v_order < min_order:
            continue
        plan.append(entry)

    # 全局排序
    plan.sort(key=lambda x: -x["score"])
    plan = plan[:top_n]

    # 写入计划文件（优先使用原子落盘，隔离 dry_run 目标文件）
    if plan_file is None:
        target_name = "daily_plan_dryrun.json" if dry_run else "daily_plan.json"
        plan_path = cfgmod.state_path(target_name)
    elif os.path.isabs(plan_file):
        plan_path = plan_file
    else:
        plan_path = cfgmod.state_path(plan_file)

    if hasattr(cfgmod, "atomic_save_json"):
        cfgmod.atomic_save_json(plan_path, plan, indent=2)
    else:
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

    # 兼容性镜像：如果在 dry_run 且未指定特定文件，同步更新 daily_plan.json 便于已有单测读取
    if dry_run and plan_file is None:
        compat_path = cfgmod.state_path("daily_plan.json")
        try:
            if hasattr(cfgmod, "atomic_save_json"):
                cfgmod.atomic_save_json(compat_path, plan, indent=2)
            else:
                with open(compat_path, "w", encoding="utf-8") as f:
                    json.dump(plan, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    rank_stats = {
        "plan_id": plan_id,
        "created_at": created_at,
        "dry_run": dry_run,
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

def execute_daily_plan(cfg, g, top_n=None, dry_run=False, plan_file=None):
    """读取 daily_plan.json → execute_jobs 投递。返回 execute 结果。
    F01: dry_run=True 时杜绝任何外部写操作与实际点击，返回动作仿真统计。
    增加仿真计划隔离保护：实弹模式下严禁混入执行标记为 dry_run 的仿真计划。"""
    if top_n is None:
        top_n = int((cfg.get("daemon") or {}).get("apply_top_n", 50))

    if plan_file is None:
        target_name = "daily_plan_dryrun.json" if dry_run else "daily_plan.json"
        plan_path = cfgmod.state_path(target_name)
        if not os.path.exists(plan_path):
            plan_path = cfgmod.state_path("daily_plan.json")
    elif os.path.isabs(plan_file):
        plan_path = plan_file
    else:
        plan_path = cfgmod.state_path(plan_file)

    if not os.path.exists(plan_path):
        return {"error": "plan file not found: %s, run rank_and_plan first" % os.path.basename(plan_path)}
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    plan = plan[:top_n]
    if not plan:
        return {"executed": 0, "results": [], "guard": g.summary(), "note": "plan empty"}

    # F01: 模拟模式下零外部写操作
    if dry_run:
        return {
            "executed": 0,
            "simulated": len(plan),
            "results": [{"title": j.get("title"), "company": j.get("company"), "dry_run": True} for j in plan],
            "guard": g.summary(),
            "dry_run": True,
            "plan_id": (plan[0].get("plan_id") if plan else None),
            "note": "dry-run simulation mode, zero external writes executed"
        }

    # 实弹安全门禁：严禁实弹执行纯仿真生成的 dry_run 计划
    if any(j.get("dry_run") is True for j in plan):
        return {
            "executed": 0,
            "error": "SAFETY_GATE: Cannot execute simulated dry_run plan in live mode. Please generate a live plan first.",
            "guard": g.summary(),
            "dry_run": False
        }

    # 写台账：投递计划生成
    ledger.append({
        "action": "daily_plan_generated",
        "plan_id": (plan[0].get("plan_id") if plan else None),
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
    max_pages = int(daemon_cfg.get("apply_max_pages", 5))
    top_n = int(daemon_cfg.get("apply_top_n", 50))
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
        plan, rank_stats = rank_and_plan(candidates, cfg, top_n=top_n, dry_run=dry_run)
        report["rank"] = rank_stats
        report["plan_id"] = rank_stats.get("plan_id")
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
