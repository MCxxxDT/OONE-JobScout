"""岗位抓取（只读）+ 详情抓取。选择器尽量宽容，遇到改版时优先改这里。"""
import re
from urllib.parse import quote

from .browser import check_risk

LIST_URL = "https://www.zhipin.com/web/geek/job?query={q}&city={c}&page={p}"
BASE = "https://www.zhipin.com"

ACTIVE_RE = re.compile(r"(刚刚活跃|今日活跃|\d+日内活跃|本周活跃|本月活跃|月内活跃|在线)")


def boss_active_days(text):
    m = ACTIVE_RE.search(text or "")
    if not m:
        return 999
    s = m.group(1)
    if "刚刚" in s or "在线" in s or "今日" in s:
        return 0
    d = re.search(r"(\d+)", s)
    if d:
        return int(d.group(1))
    if "本周" in s:
        return 7
    if "本月" in s or "月内" in s:
        return 30
    return 999


def parse_card(card):
    def txt(sel):
        try:
            el = card.query_selector(sel)
            return el.inner_text().strip() if el else ""
        except Exception:
            return ""

    href = ""
    try:
        link = card.query_selector("a[href*='/job_detail/']")
        if link:
            href = link.get_attribute("href") or ""
    except Exception:
        pass

    whole = ""
    try:
        whole = card.inner_text() or ""
    except Exception:
        pass

    return {
        "title": txt(".job-name"),
        "area": txt(".job-area"),
        "salary": txt(".salary"),
        "company": txt(".company-name"),
        "tags": txt(".tag-list"),
        "href": href,
        "boss_active": boss_active_days(whole),
        "raw": whole[:300],
    }


def search_jobs(page, keyword, city_code, page_no=1, experience=None):
    url = LIST_URL.format(q=quote(keyword), c=city_code, p=page_no)
    if experience:
        url += "&experience=%s" % quote(str(experience))
    page.goto(url, timeout=30000)
    page.wait_for_selector(".job-card-wrapper", timeout=15000)
    check_risk(page)
    cards = page.query_selector_all(".job-card-wrapper")
    jobs = []
    for c in cards:
        j = parse_card(c)
        if j["title"]:
            j["keyword"] = keyword
            jobs.append(j)
    return jobs


def fetch_detail(page, job):
    """同 tab 打开详情页抓 JD 全文。返回详情文本（可能为空）。"""
    href = job.get("href") or ""
    if not href:
        return ""
    url = href if href.startswith("http") else BASE + href
    page.goto(url, timeout=30000)
    try:
        page.wait_for_selector(".job-sec-text", timeout=8000)
        check_risk(page)
        return page.inner_text(".job-sec-text") or ""
    except Exception:
        return ""
