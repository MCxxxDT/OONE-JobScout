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
# 当日候选池软存储（避免调大上限或重复投递时重新搜索打分消耗 Token 与算力）
# ---------------------------------------------------------------------------

def _today_str():
    import datetime
    return datetime.date.today().isoformat()


def get_candidate_pool_path(dry_run=False):
    target = "daily_candidate_pool_dryrun.json" if dry_run else "daily_candidate_pool.json"
    return cfgmod.state_path(target)


def save_candidate_pool(candidates, cfg, dry_run=False):
    """将所有通过硬过滤且已评分排序的候选保存到当日候选池软存储文件。"""
    if not candidates:
        return
    import datetime
    job_mode = cfg.get("job_mode", "intern")
    pool_data = {
        "date": _today_str(),
        "job_mode": job_mode,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(candidates),
        "candidates": candidates
    }
    path = get_candidate_pool_path(dry_run)
    try:
        if hasattr(cfgmod, "atomic_save_json"):
            cfgmod.atomic_save_json(path, pool_data, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(pool_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [候选池警告] 保存当日候选池失败: {e}")


def load_candidate_pool(cfg, dry_run=False):
    """读取当日候选池。如果文件不存在、跨天或 job_mode 不匹配，返回 None。"""
    path = get_candidate_pool_path(dry_run)
    if not os.path.exists(path):
        if dry_run and os.path.exists(cfgmod.state_path("daily_candidate_pool.json")):
            path = cfgmod.state_path("daily_candidate_pool.json")
        else:
            return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        today = _today_str()
        if data.get("date") != today:
            return None
        cfg_mode = cfg.get("job_mode", "intern")
        if data.get("job_mode") and data.get("job_mode") != cfg_mode:
            return None
        return data
    except Exception:
        return None


def clear_candidate_pool(dry_run=False):
    """清除当日候选池缓存（用户修改求职偏好、城市或模态时调用）。"""
    for fname in ("daily_candidate_pool.json", "daily_candidate_pool_dryrun.json"):
        p = cfgmod.state_path(fname)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Phase 1+2: 广撒网搜索 + JD 精读
# ---------------------------------------------------------------------------

def collect_candidates(cfg, g, max_pages=3, fetch_detail=True, initial_candidates=None):
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
    if initial_candidates:
        candidates = list(initial_candidates)
        for ic in candidates:
            h = (ic.get("job") or {}).get("href") or ic.get("href") or ""
            if h:
                seen_hrefs.add(h)

    stats = {"cities": 0, "keywords": len(kws), "pages_searched": 0,
             "raw_found": len(candidates), "after_dedup": len(seen_hrefs), "after_hard_filter": len(candidates),
             "details_fetched": 0, "errors": []}

    is_silent = bool((cfg.get("browser") or {}).get("silent_mode", True))

    # 校招/实习专区名企计划直通：主动抓取 /school 专区大厂名企项目
    campus_companies = []
    if job_mode in ("intern", "campus", "mix"):
        try:
            sess_camp = rawcdp.RawCDP(cfg["cdp_endpoint"])
            sess_camp.open_tab(background=is_silent)
            try:
                recs = sess_camp.fetch_campus_recommendations()
                import re
                for r in recs:
                    cname = r.get("company") or ""
                    pure_name = re.sub(r"(?:202\d|届|秋季|春季|校园|校招|招聘|顶尖|技术|人才|计划).*", "", cname).strip()
                    if pure_name and len(pure_name) >= 2 and pure_name not in campus_companies:
                        campus_companies.append(pure_name)
                if campus_companies:
                    print(f"  [校招专区名企] 成功锁定 {len(campus_companies)} 家专区名企直通计划: {'、'.join(campus_companies[:6])}...")
            finally:
                sess_camp.close_tab()
                sess_camp.close()
        except Exception as e:
            pass

    for city_info in eff["cities"]:
        city = city_info["name"]
        code = city_info["code"]
        stats["cities"] += 1
        print(f"  [城市扫描] 正在扫描城市: {city} (关键词: {len(kws)} 个, 最大页数: {max_pages} 页)...")
        sess = rawcdp.RawCDP(cfg["cdp_endpoint"])
        try:
            sess.open_tab(background=is_silent)
            city_kws = list(kws)
            if campus_companies and city_info == eff["cities"][0]:
                for cmp in campus_companies[:3]:
                    combo = f"{cmp} {kws[0]}" if kws else cmp
                    if combo not in city_kws:
                        city_kws.append(combo)

            for kw in city_kws:
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
        # 只针对尚无 detail 的候选精读，已缓存详情的跳过二次抓取
        need_fetch = [c for c in candidates if not c.get("detail")]
        detail_targets = sorted(need_fetch, key=lambda x: -x["base_score"])[:max_details]
        if detail_targets:
            print(f"  [Phase 2] 开始 JD 精读 (从 {len(candidates)} 个候选优先精读增量头部 {len(detail_targets)} 个岗位)...")
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

    # 构造 LLM 输入：只为尚未打分的条目调用 LLM，若已带有 score/verdict（如来自候选池复用）则零 Token 跳过
    need_llm_indices = [idx for idx, it in enumerate(candidates) if it.get("score") is None]
    llm_res = {}
    if need_llm_indices:
        llm_jobs = [{**candidates[idx]["job"], "detail": candidates[idx]["detail"]} for idx in need_llm_indices]
        raw_llm_res = llm_match.match_batch(llm_jobs, cfg)
        if raw_llm_res:
            for sub_i, orig_i in enumerate(need_llm_indices):
                if sub_i in raw_llm_res:
                    llm_res[orig_i] = raw_llm_res[sub_i]

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
            "jd_text": item.get("detail") or "",
            "eligible": True,
        }

        if item.get("score") is not None and item.get("verdict"):
            entry["score"] = item["score"]
            entry["verdict"] = item["verdict"]
            entry["reason"] = item.get("reason") or ""
            entry["score_source"] = item.get("score_source") or "cache_pool"
        elif llm_res and i in llm_res:
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

    # 【核心突破】在截取 top_n 前，将全部通过门禁且已排好序的高质量候选原子持久化至当日候选池软存储！
    save_candidate_pool(plan, cfg, dry_run=dry_run)

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

def scan_and_apply_daily(cfg, top_n=None, dry_run=False):
    """每日自动投递入口：搜索 → 精读 → 择优 → 投递。
    top_n: 本次投递目标上限（支持日内额度补偿模式传入差额，默认读取 apply_top_n）。
    dry_run=True 时只跑 Phase 1~3（生成计划），不执行 Phase 4 投递。
    返回完整的管线执行报告。"""
    daemon_cfg = cfg.get("daemon") or {}
    max_pages = int(daemon_cfg.get("apply_max_pages", 5))
    if top_n is None:
        top_n = int(daemon_cfg.get("apply_top_n", 50))
    top_n = max(1, int(top_n))
    fetch_detail = bool(daemon_cfg.get("apply_fetch_detail", True))

    g = guardmod.Guard(cfg)
    report = {"phase": "init", "dry_run": dry_run}

    # Step 0: 检查当日候选池软存储（避免调大上限或重复投递时重新搜索打分消耗 Token）
    pool_data = load_candidate_pool(cfg, dry_run=dry_run)
    cached_un_greeted = []
    if pool_data and pool_data.get("candidates"):
        all_pool = pool_data["candidates"]
        greeted_hrefs = {r.get("href") for r in ledger.load_all()
                         if r.get("action") == "greet" and r.get("status") == "ok"}
        cached_un_greeted = [c for c in all_pool if c.get("href") and c.get("href") not in greeted_hrefs and c.get("eligible", True)]

        # 若缓存池中未投递候选足以满足本次需求，直接复用出计划并投递，零 Token 消耗秒级响应！
        if len(cached_un_greeted) >= top_n:
            print(f"  [⚡ 命中当日候选池软存储] 发现今日已打分优质候选池可用（总储备: {len(all_pool)}，待投递: {len(cached_un_greeted)}，本次复用前: {top_n} 岗）")
            print(f"     -> 跳过 Phase 1~3 网络全网抓取与大模型打分，零 Token 消耗秒级直达投递！")
            plan = cached_un_greeted[:top_n]
            import uuid, datetime
            cur_plan_id = str(uuid.uuid4())
            cur_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in plan:
                item["plan_id"] = cur_plan_id
                item["created_at"] = cur_time
                item["dry_run"] = dry_run

            target_name = "daily_plan_dryrun.json" if dry_run else "daily_plan.json"
            plan_path = cfgmod.state_path(target_name)
            if hasattr(cfgmod, "atomic_save_json"):
                cfgmod.atomic_save_json(plan_path, plan, indent=2)
            else:
                with open(plan_path, "w", encoding="utf-8") as f:
                    json.dump(plan, f, ensure_ascii=False, indent=2)
            if dry_run:
                compat_path = cfgmod.state_path("daily_plan.json")
                try:
                    if hasattr(cfgmod, "atomic_save_json"):
                        cfgmod.atomic_save_json(compat_path, plan, indent=2)
                except Exception:
                    pass

            report["cache_hit"] = True
            report["cached_available"] = len(cached_un_greeted)
            report["plan_id"] = cur_plan_id
            report["plan_count"] = len(plan)
            report["rank"] = {
                "plan_id": cur_plan_id,
                "created_at": cur_time,
                "dry_run": dry_run,
                "total_candidates": len(all_pool),
                "after_verdict_gate": len(cached_un_greeted),
                "plan_count": len(plan),
                "llm_available": True,
                "score_source": "cache_pool",
                "plan_path": plan_path,
            }
            if plan:
                top3_str = " | ".join("%s-%s(%s分)" % (j.get("company"), j.get("title"), j.get("score")) for j in plan[:3])
                print("  [复用 Top 3] %s" % top3_str)

            if dry_run:
                report["phase"] = "dry_run_complete"
                print(f"  [DRY-RUN] 投递计划已从软缓存生成（{len(plan)} 个），仿真模式不执行实际投递。")
                ledger.append({"action": "daily_apply_summary", "phase": "dry_run_complete",
                               "plan_count": len(plan), "dry_run": True, "source": "cache_pool"})
                return report

            print(f"  [每日投递 Phase 4] 择优投递复用计划 Top {len(plan)} 个岗位...")
            try:
                exec_res = execute_daily_plan(cfg, g, top_n=top_n, plan_file=plan_path)
                report["execute"] = exec_res
                report["phase"] = "complete"
                executed = exec_res.get("executed", 0)
                print(f"  [Phase 4 完成] 实际投递: {executed} 个岗位")
                ledger.append({
                    "action": "daily_apply_summary", "phase": "complete",
                    "plan_count": len(plan), "executed": executed,
                    "dry_run": False, "source": "cache_pool"
                })
            except Exception as e:
                report["phase"] = "execute_error"
                report["error"] = str(e)[:300]
                print(f"  [Phase 4 异常] {e}")
            return report

    # 构造部分缓存候选（如果已有部分缓存未投候选，将其注入 candidates 避免重复抓取与二次评分）
    initial_candidates = []
    if cached_un_greeted:
        print(f"  [⚡ 候选池增量复用] 发现 {len(cached_un_greeted)} 个未投递已评分候选，将作为基底候选并仅对缺口执行增量采集...")
        for item in cached_un_greeted:
            initial_candidates.append({
                "job": {
                    "title": item.get("title") or "",
                    "company": item.get("company") or "",
                    "salary": item.get("salary") or "",
                    "city": item.get("city") or "",
                    "href": item.get("href") or "",
                    "tags": item.get("tags") or "",
                    "job_mode": item.get("job_mode") or "",
                    "experience": item.get("experience") or "",
                    "campus_specs": item.get("campus_specs") or {},
                },
                "detail": item.get("jd_text") or "",
                "kw": item.get("kw") or "",
                "city": item.get("city") or "",
                "base_score": item.get("score") or 0,
                "base_reason": item.get("reason") or "",
                "campus_specs": item.get("campus_specs") or {},
                "is_eligible": True,
                "score": item.get("score"),
                "verdict": item.get("verdict"),
                "reason": item.get("reason"),
                "score_source": item.get("score_source") or "cache_pool"
            })

    # Phase 1+2
    print("  [每日投递 Phase 1+2] 广撒网搜索 + JD 精读...")
    try:
        candidates, collect_stats = collect_candidates(
            cfg, g, max_pages=max_pages, fetch_detail=fetch_detail, initial_candidates=initial_candidates)
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
