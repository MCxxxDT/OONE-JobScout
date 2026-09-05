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


mcp = FastMCP("boss-apply")


def _cfg():
    return cfgmod.load()


def _g(cfg):
    return guardmod.Guard(cfg)


@mcp.tool()
def check_login() -> dict:
    """连接调试Chrome(9335)，检查BOSS直聘登录状态与风控信号（裸CDP，不触发反爬清空）。每次开工先调用。"""
    return flows.login_state(_cfg())


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
def chat_inbox() -> dict:
    """消息中心只读巡检：needs_reply=待回复会话，needs_human=HR索要联系方式（禁止代发，转人工）。
    agent 回复前必须先调用本工具，并与台账 action=reply 历史交叉核对防重复；回复前点开会话核实全文。"""
    return flows.chat_inbox(_cfg())


@mcp.tool()
def chat_reply(company: str, text: str) -> dict:
    """按公司名回复 HR 一条消息。文案含电话/微信等联系方式会被拒绝(blocked_privacy)转人工；
    公司未命中会话列表即中止，绝不发给其他会话；发送以输入框清零为成功标准。
    回复文案不得编造经历或承诺到岗时间以外的条件。"""
    return flows.chat_reply(_cfg(), company, text)


@mcp.tool()
def exchange_wechat(company: str) -> dict:
    """按公司名在消息中心点开会话，并点击官方【换微信】按钮发起官方交换请求。
    【Agent使用准则】：仅用于 Tier 1 理想目标企业（大厂/AI独角兽核心研发或产品团队，非销售地推苦力），
    且 HR 表达出积极沟通意向或索要微信时调用。严禁对普通/地推销售岗滥用！"""
    return flows.chat_exchange_wechat(_cfg(), company)


@mcp.tool()
def send_resume(company: str) -> dict:
    """按公司名在消息中心点开会话，并点击官方【发简历】按钮推送在线/附件简历卡片供 HR 预览。
    【Agent使用准则】：用于 HR 索要简历或表达浓厚意向时主动推送。"""
    return flows.chat_send_resume(_cfg(), company)


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
