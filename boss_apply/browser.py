"""CDP 连接管理 + 风控信号检测（遗留 playwright 层）。
2026-09-08 处置评估（任务3）：check_login 已走 flows.login_state → rawcdp（裸CDP），
不依赖本模块；核心链路（server/flows/daemon）仅复用 RiskControl 异常类（rawcdp 在用）
与 human_wait 拟人等待。playwright 连接函数仅供遗留诊断脚本（scripts/diag_*.py、
wait_login.py）使用，其依赖改为 connect() 内懒加载——playwright 未安装时核心链路照常可用。"""
import time

_pw = None
_browser = None

RISK_SELECTORS = ["#nc_1_wrapper", ".nc-container", "iframe[src*='captcha']"]


class RiskControl(Exception):
    """触发风控信号（安全验证页 / 滑块）。捕获后必须立即暂停全流程。"""


def connect(cfg):
    global _pw, _browser
    if _browser is not None:
        try:
            if _browser.is_connected():
                return _browser
        except Exception:
            pass
    from playwright.sync_api import sync_playwright  # 懒加载：仅遗留诊断脚本触达
    _pw = sync_playwright().start()
    _browser = _pw.chromium.connect_over_cdp(cfg["cdp_endpoint"])
    return _browser


def get_zhipin_page(cfg, create=True):
    browser = connect(cfg)
    ctx = browser.contexts[0]
    for p in ctx.pages:
        if "zhipin.com" in (p.url or ""):
            return p
    if create:
        p = ctx.new_page()
        p.goto("https://www.zhipin.com/", timeout=30000)
        return p
    return None


def check_risk(page):
    """抛 RiskControl = 发现风控信号。调用方必须 pause 护栏并停止。"""
    url = page.url or ""
    if "security-check" in url or "web/common/security" in url:
        raise RiskControl("security-check page: %s" % url)
    for sel in RISK_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                raise RiskControl("captcha component detected: %s" % sel)
        except RiskControl:
            raise
        except Exception:
            pass


def is_logged_in(page):
    """True=已登录（检测到头像）；False=未登录（检测到登录按钮）。"""
    try:
        for sel in [".nav-figure", ".header-nav-figure"]:
            if page.locator(sel).count() > 0:
                return True
        for sel in [".header-login-btn", "text=登录/注册"]:
            if page.locator(sel).count() > 0:
                return False
    except Exception:
        pass
    return False


def human_wait(cfg, kind="page"):
    import random
    if kind == "greet":
        lo, hi = cfg.get("interval_seconds", [4, 10])
    elif kind == "search":
        lo, hi = cfg.get("search_interval_seconds", [1.5, 3.5])
    else:
        lo, hi = 2.5, 3.5
    time.sleep(random.uniform(lo, hi))


def shutdown():
    global _pw, _browser
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = None
    _browser = None
