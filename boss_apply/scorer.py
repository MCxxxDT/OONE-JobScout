"""打分器：关键词加权（移植 radar.py 思路）+ 公司池加成 + 硬性过滤。"""
import re

SALARY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)\s*([Kk万])")

# 届别标注：数字(段) + 届，如 28届 / 2028届 / 26-28届 / 26/27/28届。(?<!第) 避开"第28届大赛"
JIE_TOKEN_RE = re.compile(r"(?<!第)(\d{2,4}(?:\s*[-~/、,，]\s*\d{2,4})*)\s*届")


def jie_excludes(text, target=27):
    """岗位明确标注的毕业届别不含 target → True（27届用户应跳过28届岗）。
    范围写法（26-28届/26~28届）按连续区间展开，含 target 则放行；
    斜杠/顿号列表按离散点处理（26/28届 = 排除27）；无届别标注 → False。"""
    t = text or ""
    if "届" not in t:
        return False
    nums = []
    for m in JIE_TOKEN_RE.finditer(t):
        part = m.group(1)
        ns = [int(x) for x in re.findall(r"\d{2,4}", part)]
        ns = [n % 100 if n >= 1000 else n for n in ns]
        ns = [n for n in ns if 20 <= n <= 35]  # 合理届别窗口，滤掉 salary 26-28K 之类
        if not ns:
            continue
        if len(ns) >= 2 and re.search(r"[-~]", part):
            nums.extend(range(min(ns), max(ns) + 1))
        else:
            nums.extend(ns)
    return bool(nums) and target not in nums


def salary_k(text):
    """返回 (lo, hi)，单位 K/月；日薪/时薪等无法解析时返回 (None, None)。"""
    if not text:
        return (None, None)
    m = SALARY_RE.search(text)
    if not m:
        return (None, None)
    lo, hi, unit = float(m.group(1)), float(m.group(2)), m.group(3)
    if unit == "万":
        lo, hi = lo * 10, hi * 10
    return (lo, hi)


def company_boost(company, cfg):
    pool = cfg.get("company_pool", {})
    c = (company or "").lower()
    t1 = pool.get("tier1", {})
    for name in t1.get("names", []):
        if name.lower() in c:
            return t1.get("boost", 3)
    t2 = pool.get("tier2", {})
    for name in t2.get("names", []):
        if name.lower() in c:
            return t2.get("boost", 2.5)
    return 0.0


def check_eligibility(job, detail, cfg):
    """硬性资格门禁（阶段 A）：公司黑名单、kill 词、岗位门槛、活跃度、薪资区间、届别。
    返回 (is_eligible: bool, reason: str)。一票否决项在此裁定。
    """
    w = cfg.get("score_words", {})
    title = job.get("title") or ""
    company = job.get("company") or ""

    # 1. 公司黑名单
    for b in cfg.get("blacklist_companies", []):
        if b and b in company:
            return False, "blacklisted company"

    # 2. kill 词（标题/公司命中即否决）
    for k in w.get("kill", []):
        if k in title:
            return False, "kill: title contains %r" % k
        if k in company:
            return False, "kill: company contains %r" % k

    # 3. 岗位类型门槛（仅看标题）：销售/客服/前后端/算法等非 PM 岗，JD 关键词堆分也无效
    # 若用户显式将某技术方向设为 want_jobs，则不应被 title_kill 误杀
    prefs_want = [pw.lower() for pw in (cfg.get("prefs", {}).get("want_jobs") or [])]
    for k in w.get("title_kill", []):
        if k in title:
            if any(k in pw or pw in k for pw in prefs_want):
                continue
            return False, "kill: title role-gate %r" % k

    # 4. boss_active: -1/None = 新版卡片无此字段（未知），放行；详情页会补验
    ba = job.get("boss_active", -1)
    if ba is not None:
        try:
            ba_int = int(ba)
            if ba_int >= 0 and ba_int > cfg.get("boss_active_max_days", 14):
                return False, "boss inactive > %d days" % cfg.get("boss_active_max_days", 14)
        except (ValueError, TypeError):
            pass

    # 5. 薪资区间
    lo, hi = salary_k(job.get("salary", ""))
    if lo is not None:
        smin, smax = cfg.get("salary_range", [3, 30])
        if hi < smin or lo > smax:
            return False, "salary out of range: %s" % job.get("salary")

    # 6. 届别不符（如只收28届而用户27届）：标题或JD明确标注且范围不含目标届 → 一票否决
    if jie_excludes((title or "") + "\n" + (detail or ""), cfg.get("target_jie", 27)):
        return False, "kill: 届别标注不含%d届" % cfg.get("target_jie", 27)

    # 7. 用户偏好排斥词硬否决（若配置）
    prefs_avoid = (cfg.get("prefs") or {}).get("avoid_jobs") or []
    for aj in prefs_avoid:
        if aj and (aj in title or aj in company):
            return False, "avoid_jobs: %s" % aj

    return True, "eligible"


def calculate_matching_score(job, detail, cfg):
    """偏好与相关度打分（阶段 B）：仅对通过资格门禁的岗位打分。
    包含 strong/medium/weak 关键词权重、公司层级加成、实习/PM偏好与时令规格动态调优。
    返回 (score: float, reasons: str)。即使得分为 0 也代表符合资格的零关键词候选。
    """
    w = cfg.get("score_words", {})
    strong = w.get("strong", [])
    medium = w.get("medium", [])
    weak = w.get("weak", [])
    title = job.get("title") or ""
    company = job.get("company") or ""

    s = 0.0
    reasons = []
    jd = ((title or "") + "\n" + (detail or "") + "\n" + (job.get("tags") or "")).lower()
    for word in strong:
        if word in jd:
            s += 3
            reasons.append("+3 strong:%s" % word)
    for word in medium:
        if word in jd:
            s += 1
            reasons.append("+1 med:%s" % word)
    for word in weak:
        if word in jd:
            s -= 4
            reasons.append("-4 weak:%s" % word)

    boost = company_boost(company, cfg)
    if boost:
        s += boost
        reasons.append("+%s company" % boost)

    if "实习" in title or "实习生" in title:
        s += 3
        reasons.append("+3 intern")
    if "产品" in title:
        s += 2
        reasons.append("+2 pm")

    # 校园与时令规格动态调优（转正机会 +2.0，出勤天数匹配 +1.0，立即到岗 +0.5）
    specs = job.get("campus_specs")
    if specs is None:
        try:
            from .campus_engine import InternSpecExtractor
            specs = InternSpecExtractor.extract_specs(
                jd_text=detail or "",
                tags=job.get("tags") or "",
                title=title or ""
            )
        except Exception:
            specs = None

    if specs:
        if specs.get("has_conversion_chance") or specs.get("conversion_prob", 0) >= 0.7:
            s += 2.0
            reasons.append("+2.0 conversion")
        days = specs.get("days_per_week")
        if days is not None and days <= 5:
            s += 1.0
            reasons.append(f"+1.0 days_match:{days}d")
        if specs.get("immediate_onboarding"):
            s += 0.5
            reasons.append("+0.5 immediate_onboarding")

    return max(0.0, round(s, 1)), "; ".join(reasons) if reasons else "zero_keyword_pass"


def score(job, detail, cfg):
    """返回 (score, reasons)。0 = 一票否决或零关键词。
    向下兼容既有调用点：先走资格门禁，未通过直接返回 0.0 与否决原因；通过则计算匹配得分。
    """
    eligible, reason = check_eligibility(job, detail, cfg)
    if not eligible:
        return 0.0, reason
    return calculate_matching_score(job, detail, cfg)
