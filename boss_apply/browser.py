"""CDP 连接管理 + 风控信号检测。只连接真实 Chrome（9222），绝不 launch 新浏览器。"""
import time

from playwright.sync_api import sync_playwright

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
