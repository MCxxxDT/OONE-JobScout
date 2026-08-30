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


def score(job, detail, cfg):
    """返回 (score, reasons)。0 = 一票否决。"""
    w = cfg.get("score_words", {})
    strong = w.get("strong", [])
    medium = w.get("medium", [])
    weak = w.get("weak", [])
    kill = w.get("kill", [])

    title = job.get("title", "")
    company = job.get("company") or ""
    for k in kill:
        if k in title:
            return 0, "kill: title contains %r" % k
        if k in company:
            return 0, "kill: company contains %r" % k

    # 岗位类型门槛（仅看标题）：销售/客服/前后端/算法等非PM岗，JD关键词堆分也无效
    for k in w.get("title_kill", []):
        if k in title:
            return 0, "kill: title role-gate %r" % k

    # boss_active: -1/None = 新版卡片无此字段（未知），放行；详情页会补验
    ba = job.get("boss_active", 999)
    if ba is not None and ba >= 0 and ba > cfg.get("boss_active_max_days", 14):
        return 0, "boss inactive > %d days" % cfg.get("boss_active_max_days", 14)

    lo, hi = salary_k(job.get("salary", ""))
    if lo is not None:
        smin, smax = cfg.get("salary_range", [3, 30])
        if hi < smin or lo > smax:
            return 0, "salary out of range: %s" % job.get("salary")

    for b in cfg.get("blacklist_companies", []):
        if b and b in (job.get("company") or ""):
            return 0, "blacklisted company"

    # 届别不符（如只收28届而用户27届）：标题或JD明确标注且范围不含目标届 → 一票否决
    if jie_excludes((title or "") + "\n" + (detail or ""), cfg.get("target_jie", 27)):
        return 0, "kill: 届别标注不含%d届" % cfg.get("target_jie", 27)

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

    boost = company_boost(job.get("company"), cfg)
    if boost:
        s += boost
        reasons.append("+%s company" % boost)

    if "实习" in title or "实习生" in title:
        s += 3
        reasons.append("+3 intern")
    if "产品" in title:
        s += 2
        reasons.append("+2 pm")

    return round(s, 1), "; ".join(reasons)
