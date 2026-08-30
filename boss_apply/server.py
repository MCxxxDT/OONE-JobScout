"""FastMCP Server：把 boss-apply 挂进任意 MCP 客户端（Trae / WorkBuddy / Claude 等）。

启动：
    venv/Scripts/python server.py            # stdio（MCP 客户端拉起方式）
    venv/Scripts/python server.py --http     # 调试用 HTTP 模式 127.0.0.1:8765/mcp
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP

from boss_apply import config as cfgmod, flows, guard as guardmod, ledger
from boss_apply import browser

mcp = FastMCP("boss-apply")


def _cfg():
    return cfgmod.load()


def _g(cfg):
    return guardmod.Guard(cfg)


@mcp.tool()
def check_login() -> dict:
    """连接调试Chrome(9222)，检查BOSS直聘登录状态与风控信号。每次开工先调用。"""
    cfg = _cfg()
    page = browser.get_zhipin_page(cfg)
    page.goto("https://www.zhipin.com/", timeout=30000)
    page.wait_for_timeout(2500)
    ok = browser.is_logged_in(page)
    risk = None
    try:
        browser.check_risk(page)
    except browser.RiskControl as e:
        risk = str(e)
        _g(cfg).pause("risk: %s" % risk)
    return {"logged_in": ok, "risk": risk, "url": page.url,
            "hint": None if ok else "请在调试Chrome窗口内扫码登录BOSS后重试"}


@mcp.tool()
def scan_and_score(city: str, keywords: list = None, max_pages: int = 2, fetch_detail: bool = True) -> dict:
    """只读扫描一个城市：搜索岗位、抓JD详情、打分、写台账。绝不发送沟通。"""
    cfg = _cfg()
    return flows.scan_city(cfg, _g(cfg), city, keywords, max_pages, fetch_detail)


@mcp.tool()
def build_plan(min_score: float = None, limit: int = 15) -> dict:
    """从台账挑高分岗位生成 state/plan.json（review模式：只生成不投递）。人工确认后调用 execute_plan。"""
    return flows.build_plan(_cfg(), min_score, limit)


@mcp.tool()
def execute_plan(max_count: int = 10, plan_file: str = "plan.json") -> dict:
    """执行已确认的投递计划。护栏强制生效：4-10秒随机间隔、每日上限、城市配额、风控即停。"""
    cfg = _cfg()
    return flows.execute_plan(cfg, _g(cfg), max_count, plan_file)


@mcp.tool()
def probe_readonly_rate(steps: list = None) -> dict:
    """只读频率探针（仅在测试账号上使用）：阶梯频率搜索定位风控触发档位。不产生任何沟通。"""
    return flows.probe_readonly(_cfg(), steps)


@mcp.tool()
def stats() -> dict:
    """台账统计 + 护栏当前状态。"""
    cfg = _cfg()
    return {"ledger": ledger.stats(), "guard": _g(cfg).summary()}


@mcp.tool()
def resume_guard() -> dict:
    """人工确认页面无风控后，解除暂停状态。"""
    g = _g(_cfg())
    g.resume()
    return {"resumed": True, "guard": g.summary()}


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run(transport="http", host="127.0.0.1", port=8765)
    else:
        mcp.run()
