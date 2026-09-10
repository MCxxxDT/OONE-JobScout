"""AI 智能决策与回复引擎。
纯 Agent / 大模型智能驱动架构：
1. 彻底废除任何确定性模板降级引擎；
2. 若有可用 Agent / 大模型驱动，按沟通策略 Prompt 动态拟人生成高情商回复；
3. 若无法调用 Agent 进行回复，则全权告知人工进行处理（action: "needs_human"）；
4. 工作时间闸门（Active Hours Gate）与消息时效过滤；
5. 隐私红线安全门禁（电话/微信意图二次强校验）。
"""
import datetime
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from . import config as cfgmod, greeter, ledger

# 候选人真实画像配置
CANDIDATE_PROFILE = {
    "name": "张烨韬",
    "school": "福建师范大学",
    "major": "数字媒体技术",
    "grad_year": 2027,
    "grade_desc": "2027届应届在读生（毕业班）",
    "current_city": "福州",
    "target_region": "江浙沪（杭州、上海等）",
    "availability": "目前常驻福州，强烈意向江浙沪，合适机会随时奔赴全职到岗（每周5天，长期全职实习直冲校招转正）",
    "salary_requirement": "实习薪资能覆盖江浙沪当地基础租房与生活开销（如日薪180-250+或月薪4k-6k+，如有房补亦可）",
    "target_roles": "AI产品经理 / Agent产品 / 大模型应用产品 / 商业化产品 / 2027届校招",
    "tech_highlights": "熟练FastMCP标准Server架构、Coze智能体中台编排、Trae原生Skills、CDP自动化、Python与微内核系统工程",
    "business_highlights": "15个月全职操盘200+人校园团队、单月GMV破10万、复购80%，具备过硬商业化嗅觉与即战力",
}

# 细分敏感意图模式（不阻断，采用太极回复并在后台异步提醒用户）
CONTACT_PATTERNS = [
    re.compile(r"(?:加|留|给|发|换)\s*个?\s*(?:微信|vx|v号|联系方式|手机|电话)", re.I),
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"(?:微信号|手机号|电话号|联系电话|邮箱|发送到邮箱|发一份pdf)", re.I),
]

LOCATION_PATTERNS = [
    re.compile(r"(?:在杭州吗|在上海吗|在本地吗|人在哪|目前在哪|常驻哪里|在福州吗|异地|来杭州|来上海|接受线下|线下面试吗)", re.I),
]

INTERVIEW_PATTERNS = [
    re.compile(r"(?:明天|后天|下周|今晚|上午|下午)?\s*\d{1,2}(?:点|:00|半)\s*(?:来|进行)?\s*(?:面试|线下面|沟通)", re.I),
    re.compile(r"(?:腾讯会议|钉钉会议|飞书会议|线上会议|来公司面试|现场面|线下面试|面试地址|宣讲会)", re.I),
]

SALARY_PATTERNS = [
    re.compile(r"(?:薪资待遇|期望薪资|底薪|提成|能接受.*薪资)", re.I),
]

# 高意向信号（对标 ai-job 高意向识别，2026-09-08 开源调研落地）：
# 强关键词=面试推进/录用信号，任一命中即高意向；弱关键词=薪资/到岗类洽谈信号，
# 需叠加 HR 发言 >=4 轮（真聊起来了）才算，避免把 HR 群发模板误判为高意向。
HIGH_INTENT_STRONG = ("面试", "二面", "三面", "笔试", "offer", "Offer", "OFFER", "录用", "入职", "报到")
HIGH_INTENT_WEAK = ("薪资", "待遇", "到岗", "实习期", "转正", "试用期", "五险", "房补", "餐补")


def detect_high_intent(conv: dict) -> Tuple[bool, str]:
    """识别高意向会话，供 daemon 将 needs_human 告警升级为飞书红色高优卡片，
    避免高价值机会淹没在普通告警里。返回 (是否高意向, 判定依据)。
    只扫描 HR 发言（last_msg 即 HR 最新发言 + history 中 role=hr 的条目），
    我方自己提到"面试/offer"不算 HR 高意向。"""
    history = conv.get("history") or []
    hr_turns = sum(1 for m in history if isinstance(m, dict) and m.get("role") == "hr")
    text = conv.get("last_msg") or ""
    for m in history[-6:]:
        if isinstance(m, dict) and m.get("role") == "hr":
            text += " " + (m.get("text") or "")
    if any(k in text for k in HIGH_INTENT_STRONG):
        return True, "strong_kw"
    if hr_turns >= 4 and any(k in text for k in HIGH_INTENT_WEAK):
        return True, "weak_kw+hr_turns=%d" % hr_turns
    return False, ""


def is_active_hour(now: Optional[datetime.datetime] = None, active_hours: str = "09:30-20:30") -> bool:
    """判断当前时间是否处于工作时间窗口内（默认 09:30~20:30）。
    夜间及非工作时段严禁自动对外发送，以防风控。
    """
    if now is None:
        now = datetime.datetime.now()
    try:
        start_s, end_s = active_hours.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        start_t = datetime.time(sh, sm)
        end_t = datetime.time(eh, em)
        cur_t = now.time()
        return start_t <= cur_t <= end_t
    except Exception:
        return False


def seconds_until_next_active(now: Optional[datetime.datetime] = None, active_hours: str = "09:30-20:30") -> int:
    """计算距离下一个工作时间窗口开始的秒数。用于夜间休眠调度。"""
    if now is None:
        now = datetime.datetime.now()
    try:
        start_s, _ = active_hours.split("-")
        sh, sm = map(int, start_s.split(":"))
        today_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if now < today_start:
            return max(60, int((today_start - now).total_seconds()))
        tomorrow_start = today_start + datetime.timedelta(days=1)
        return max(60, int((tomorrow_start - now).total_seconds()))
    except Exception:
        return 3600


def is_recent_message(time_str: str, max_age_hours: int = 24) -> bool:
    """根据会话列表的时间文本判断是否为近期新消息。
    - '23:19', '14:03' (当天时间) -> True
    - '刚刚', '10分钟前', '2小时前' -> True
    - '昨天' -> 在 24h~48h 判定范围内放行
    - '09月01日', '08月31日' -> 按与今天的日期差判定（含跨年），差值天数
      <= max_age_hours/24 才放行（2026-09-08：半月窗口 max_age_hours=360 时，
      半个月内的旧会话也纳入回复范围，用户明确要求补回这些未回复会话）
    """
    if not time_str:
        return False
    ts = str(time_str).strip()
    if any(k in ts for k in ("刚刚", "分钟", "小时")):
        return True
    if ":" in ts and "月" not in ts and "年" not in ts and "天" not in ts:
        return True
    if "昨天" in ts:
        return max_age_hours >= 24
    m = re.match(r"(\d{1,2})月(\d{1,2})日", ts)
    if m:
        try:
            today = datetime.date.today()
            d = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
            if d > today:  # 跨年：无年份文本显示的是去年日期
                d = d.replace(year=today.year - 1)
            return (today - d).days <= max_age_hours / 24.0
        except ValueError:
            return False
    return False


# 客服式八股文铁血禁令（命中即过滤，严禁人机感）
FORBIDDEN_PHRASES = (
    "感谢您的详细介绍",
    "感谢您的详细说明",
    "感谢您的介绍",
    "感谢详细介绍",
    "期待进一步沟通",
    "期待与您进一步沟通",
    "期待与您的进一步沟通",
    "希望能有机会加入",
    "希望有机会加入",
    "非常荣幸",
    "祝好",
    "祝工作顺利",
    "祝您生活愉快",
    "祝您工作顺利",
)


def sanitize_and_clean_reply(text: str, max_chars: int = 150) -> str:
    """清理回复文本中的 Markdown 标记、八股文禁令短语，并确保句子完整结束（杜绝截断半句话/半个字）。"""
    if not text:
        return ""
    # 1. 过滤八股文禁令短语
    for ban in FORBIDDEN_PHRASES:
        text = text.replace(ban, "")

    # 2. 剥除 Markdown 格式标记（BOSS 聊天为移动端纯文本，加粗 ** 会暴露为源码源码感）
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("*", "").replace("`", "").replace("#", "")

    # 3. 清理首尾多余空白和非法起手标点
    text = re.sub(r"^[，。！!？?\s]+", "", text).strip()
    text = re.sub(r"[\s\t\r]+", " ", text).strip()

    # 4. 若未超出上限，直接返回整句（绝不盲目截断）
    if len(text) <= max_chars:
        return text

    # 5. 超出上限时：寻找最后一个完整句子结束标点，杜绝生硬切断字词
    truncated = text[:max_chars]
    last_stop = max(
        truncated.rfind("。"),
        truncated.rfind("！"),
        truncated.rfind("!"),
        truncated.rfind("？"),
        truncated.rfind("?"),
        truncated.rfind("\n"),
    )
    if last_stop >= 25:
        return truncated[:last_stop + 1].strip()

    # 次级保底：若无句末标点，找逗号/分号断句并收尾
    last_comma = max(truncated.rfind("，"), truncated.rfind(","), truncated.rfind("；"), truncated.rfind(";"))
    if last_comma >= 25:
        return truncated[:last_comma].strip() + "。"

    # 最底线保底：剥除末尾可能的断裂字符并补句号
    res = truncated.rstrip("，,；;、的和与且在但若方")
    if not re.search(r"[。！？!?]$", res):
        res += "。"
    return res


def detect_privacy_leak(text: str, cfg: Optional[dict] = None, profile: Optional[dict] = None) -> Tuple[bool, str]:
    """物理检测回复文本中是否存在敏感个人信息泄密（防套话防诱导终极防线）。
    返回 (是否泄密, 拦截详情)。
    """
    if not text:
        return False, ""

    # 1. 11 位手机号（中国大陆手机号，含空格/连字符如 138-1234-5678）
    if re.search(r"(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)", text):
        return True, "包含11位手机号码"

    # 2. 座机号码 / 长固定电话
    if re.search(r"(?<!\d)\d{3,4}-?\d{7,8}(?!\d)", text):
        return True, "包含固定电话号码"

    # 3. 检查用户显式配置的联系方式（privacy_policy 或 CANDIDATE_PROFILE）
    cfg = cfg or {}
    pol = cfg.get("privacy_policy") or {}
    conf_phone = str(pol.get("contact_phone") or "").strip()
    if conf_phone and len(conf_phone) >= 7 and conf_phone in text:
        return True, f"包含配置的手机号 ({conf_phone})"

    conf_wx = str(pol.get("contact_wechat") or "").strip()
    if conf_wx and len(conf_wx) >= 4 and conf_wx.lower() in text.lower():
        return True, f"包含配置的微信号 ({conf_wx})"

    # 4. 常见微信号/联系方式明文吐出模式：如 "我的微信是xxx", "微信: xxx", "加我微信xxx", "微信号:xxx"
    wx_leak_re = re.compile(
        r"(?:我(?:的)?(?:微信|vx|v号|微信号?)|加我微信|微信号(?:是|为)?|联系电话(?:是|为)?)\s*[:：是为]?\s*([a-zA-Z0-9_-]{5,25})",
        re.I
    )
    m = wx_leak_re.search(text)
    if m:
        candidate = m.group(1).lower()
        whitelist = {"wechat", "weixin", "phone", "number", "agent", "ready", "fuzhou", "hangzhou", "shanghai"}
        if candidate not in whitelist:
            return True, f"疑似明文吐出个人微信号/联系方式 ({m.group(0)})"

    return False, ""


class AIReplyEngine:
    """纯 Agent / 大模型驱动的会话决策与回复生成引擎（彻底废除确定性模板降级）。"""

    def __init__(
        self,
        cfg: Optional[dict] = None,
        profile: Optional[dict] = None,
        agent_generator: Optional[Any] = None,
    ):
        self.cfg = cfg or {}
        # 画像优先级：显式传入 > 简历提炼（profile_store.refined）> 硬编码默认
        try:
            from . import profile_store
            refined = profile_store.load_profile() or {}
        except Exception:
            refined = {}
        self.profile = {**CANDIDATE_PROFILE, **refined, **(profile or {})}
        self.agent_generator = agent_generator
        llm_cfg = self.cfg.get("llm") or {}
        if llm_cfg.get("api_key"):
            self.openai_key = llm_cfg["api_key"]
            self.openai_base = llm_cfg.get("base_url") or "https://api.openai.com/v1"
            self.llm_model = llm_cfg.get("model") or "gpt-4o-mini"
            self.openrouter_key = llm_cfg.get("openrouter_key") or ""
        else:
            self.openrouter_key = os.getenv("OPENROUTER_API_KEY") or ""
            self.openai_key = os.getenv("OPENAI_API_KEY") or ""
            self.openai_base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
            self.llm_model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

    def build_agent_prompt(self, conv: dict) -> str:
        """根据当前会话构建供 Agent/大模型思考的标准化决策 Prompt 与心法。
        conv 可携带 job 字段（flows.chat_job_detail 结果）：注入岗位元数据与 JD 全文，
        供模型研判岗位含金量后"见人下菜碟"（核心产品岗提热情，销售地推岗保持太极）。"""
        who = conv.get("who") or "HR"
        last_msg = (conv.get("last_msg") or "").strip()

        jd_section = ""
        jd = conv.get("job") or {}
        if isinstance(jd, dict) and jd:
            lines = []
            if jd.get("title"):
                lines.append(f"- 岗位名称：{jd['title']}")
            if jd.get("company"):
                lines.append(f"- 公司：{jd['company']}")
            if jd.get("salary"):
                lines.append(f"- 薪资：{jd['salary']}")
            if jd.get("city"):
                lines.append(f"- 城市：{jd['city']}")
            if jd.get("experience"):
                lines.append(f"- 经验要求：{jd['experience']}")
            if jd.get("degree"):
                lines.append(f"- 学历要求：{jd['degree']}")
            if jd.get("boss_active") is not None and jd.get("boss_active") >= 0:
                lines.append(f"- BOSS最近活跃：{jd['boss_active']}天前（越小越活跃）")
            jd_text = (jd.get("jd_text") or "").strip()
            if jd_text:
                lines.append("- JD全文（截取）：\n" + jd_text[:600])
            if lines:
                jd_section = "\n【会话关联岗位与JD（供研判含金量）】\n" + "\n".join(lines) + "\n"

        # 对话历史注入（daemon 每轮从聊天面板现场抓取，严格过滤系统提示与噪声）
        history_section = ""
        history = conv.get("history") or []
        if isinstance(history, list) and history:
            role_label = {"me": "我方", "hr": "HR"}
            h_lines = []
            for m in history:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                if role not in ("me", "hr"):
                    continue
                text = (m.get("text") or "").strip()
                if text:
                    h_lines.append(f"{role_label[role]}: {text}")
            if h_lines:
                history_section = (
                    "\n【对话历史（最近%d条，已标注发言方，按时间正序）】\n" % len(h_lines)
                    + "\n".join(h_lines) + "\n"
                )

        # 用户求职偏好注入（2026-09-09：留空的维度由模型基于候选人背景自主决断）
        prefs_section = ""
        prefs = (self.cfg.get("prefs") or {})
        want_jobs = [str(x).strip() for x in (prefs.get("want_jobs") or []) if str(x).strip()]
        avoid_jobs = [str(x).strip() for x in (prefs.get("avoid_jobs") or []) if str(x).strip()]
        want_cities = [str(x).strip() for x in (prefs.get("want_cities") or []) if str(x).strip()]
        avoid_cities = [str(x).strip() for x in (prefs.get("avoid_cities") or []) if str(x).strip()]
        if want_jobs or avoid_jobs or want_cities or avoid_cities:
            pl = []
            if want_jobs:
                pl.append("- 向往岗位方向：" + "、".join(want_jobs))
            if avoid_jobs:
                pl.append("- 排斥岗位方向（命中则保持太极、点到为止）：" + "、".join(avoid_jobs))
            if want_cities:
                pl.append("- 向往城市：" + "、".join(want_cities))
            if avoid_cities:
                pl.append("- 排斥城市（相关机会优先降低热情）：" + "、".join(avoid_cities))
            pl.append("- 未列出的维度由你基于候选人背景自主决断")
            prefs_section = "\n【用户求职偏好】\n" + "\n".join(pl) + "\n"

        def _fmt(val):
            if isinstance(val, list):
                return "、".join(str(x) for x in val if str(x).strip())
            return str(val).strip() if val is not None else ""

        hl_parts = []
        for k in ("tech_highlights", "business_highlights", "highlights", "summary"):
            s = _fmt(self.profile.get(k))
            if s:
                hl_parts.append(s)
        highlights_str = "；".join(hl_parts) if hl_parts else "具备扎实的产品与工程落地实践"

        cur_city = self.profile.get('current_city', '福州')
        target_reg = self.profile.get('target_region', '江浙沪')
        grade_desc = self.profile.get('grade_desc', '2027届应届在读生')
        status_desc = self.profile.get('status', '目前处于实习/校招求职阶段，可稳定全职到岗')

        return (
            f"【候选人真实画像】\n"
            f"- 姓名：{self.profile.get('name', '张烨韬')}\n"
            f"- 学历与专业：{self.profile.get('school', '')} · {self.profile.get('major', '')}\n"
            f"- 毕业届别与状态：{grade_desc}（{status_desc}）\n"
            f"- 常驻地与意向城市：目前常驻【{cur_city}】，核心意向奔赴【{target_reg}】发展\n"
            f"- 到岗与稳定性：{self.profile.get('availability', '合适机会随时奔赴全职到岗')}\n"
            f"- 薪资底线诉求：{self.profile.get('salary_requirement', '实习薪资能覆盖租房与生活开销')}\n"
            f"- 核心优势：{highlights_str}\n"
            f"{prefs_section}"
            f"{jd_section}"
            f"{history_section}\n"
            f"【当前HR与最新消息】\n"
            f"- 对话方：{who}\n"
            f"- HR最新消息：\"{last_msg}\"\n\n"
            f"【Agent 沟通策略与铁律（拒绝人机，真人口语化，直接高效）】\n"
            f"1. 【铁血禁令（绝对禁止）】：严禁出现“感谢您的详细介绍”、“期待进一步沟通”、“希望有机会加入”、“非常荣幸”、“祝好”等任何客服式/三段式礼貌八股废话！开门见山直接说事；\n"
            f"2. 【严禁无脑索要JD】：严禁无脑索要“完整JD/汇报线/技术栈”——除非 HR 仅发了“在吗/发个简历”且上下文完全没有任何职位信息，否则一律禁止主动索要 JD；\n"
            f"3. 【闭合性问题直球回答】：遇到诸如“能否线下面试？”、“早九晚七能接受吗？”、“目前在职还是离职？”等闭合提问，必须在 15 字内基于候选人实际情况直截了当明确回答（结合候选人画像中常驻地/到岗时间，能即直言能，不能或需协调亦如实简练告知），绝不允许顾左右而言他或打太极；\n"
            f"4. 【问常驻地 / 地点】：结合候选人画像如实告知常驻【{cur_city}】，意向奔赴【{target_reg}】；初试可提议先通过线上高效推进；\n"
            f"5. 【问薪资 / 待遇】：说明跨城前往【{target_reg}】发展，主要希望能覆盖当地基础租房与生活开销即可（参考诉求：{self.profile.get('salary_requirement', '满足基础租房与生活开销')}），以业务和团队匹配为主；\n"
            f"6. 【三不原则与见人下菜碟】：不承诺死时间、不拒绝机会、见人下菜碟（结合JD研判，核心岗提升热情，杂役销售岗点到为止；严禁泄露真实11位手机号、微信号）；\n"
            f"7. 【动作协同与多意图/复合问题处理规则】：\n"
            f"   - 【复合问题必须完整回应】：若 HR 在一条消息中提出了复合问题（例如既询问个人情况/人情关怀/生日/到岗时间/业务问题，又提出索要微信/简历/电话等），【绝对严禁】仅机械复读一句动作通知！必须【先真诚、口语化、得体地回答 HR 提出的前半部分问题】（如对方询问生日入职蛋糕，可得体告知生日月份并对入职关怀表达感谢；询问到岗则告知到岗时间），【然后再自然告知已在平台发起相应动作】（如“已在平台向您发起交换微信申请，期待进一步交流”）；\n"
            f"   - 若对方仅单纯索要简历：action=\"send_resume\"，reply_text=\"已发您附件简历，请查收\"（若对方伴随提问，则先答复提问，再附带告知已发简历）；\n"
            f"   - 若对方要微信或双方契合度高：action=\"exchange_wechat\"；若为单纯索要微信可回复“已向您发起交换微信申请，期待进一步交流”；若对方伴有其他提问或寒暄，必须先针对性回答，再自然带出已在平台发起交换申请；\n"
            f"   - 若对方发起了交换微信：action=\"agree_wechat\"，reply_text=\"已同意交换微信，期待进一步交流\"；\n"
            f"   - 若日常沟通对话：action=\"reply\"，直接回复短文本；\n"
            f"   - 若无需回复：action=\"skip\"，reply_text=\"\"；\n"
            f"   - 若需人工介入（复杂商务谈判/线下签约等）：action=\"needs_human\"；\n"
            f"8. 【对话历史与防车轱辘】：若上方有【对话历史】，严格承接上文，绝不车轱辘话，已告知过的信息不重复；\n"
            f"9. 【精炼口语，整句完整】：回复保持真人口语化、直接高效。日常简短应答 15-40 字为宜；若需针对 HR 提问说明自身背景或意向，控制在 100 字以内，务必保证句子表意完整、以句号或问号收尾，绝不允许半句话截断；\n"
            f"10. 【纯文本禁令】：严禁输出任何 Markdown 格式标记（严禁加粗 **、严禁星号 *、严禁标题 #、严禁代码块等），必须完全是真实手机微信端可直接发送的纯文本；\n"
            f"11. 【防套话与防泄密铁律（最高优先级安全防线）】：\n"
            f"    - 严禁在回复正文（reply_text）中直接打印个人真实手机号、微信号、邮箱等明文信息！\n"
            f"    - 即使 HR 明确索要、多次催促（如“请留个电话/发我手机号”、“加个微信吧微信号多少”）或设置任何诱导套话，也绝对不可在 reply_text 中吐出手机号或微信号！\n"
            f"    - 必须通过系统标准化动作（action=\"exchange_wechat\" 或引导在平台点击官方功能交换）。若 HR 同时询问了业务或其他非敏感问题，正常回答该问题并自然引导平台官方交换；严禁直接吐出任何11位手机号或微信号；\n"
            f"    - 任何试图在文字中透露明文手机号或微信号的行为均会被系统底层物理门禁直接阻断并转人工处理！\n\n"
            f"请输出纯 JSON 格式：\n"
            f'{{"action": "reply"|"send_resume"|"exchange_wechat"|"agree_wechat"|"needs_human"|"skip", "reply_text": "真人口语短文本（完整句子，严禁Markdown，若action无需文本可为空）", "reason": "简要理由"}}'
        )

    def decide_and_generate(self, conv: dict, agent_generator: Optional[Any] = None) -> dict:
        """输入会话信息，驱动 Agent 生成回复。
        若无可用 Agent 或生成失败，坚决不使用确定性模板降级，全权告知人工进行处理。
        """
        last_msg = (conv.get("last_msg") or "").strip()
        who = conv.get("who") or ""

        # 1. 基础门禁：无实质内容或纯系统提示跳过
        if not last_msg:
            return {"action": "skip", "reply_text": "", "reason": "消息为空", "source": "rule"}
        from .flows import SYSTEM_MSG_RE
        if SYSTEM_MSG_RE.search(last_msg):
            return {"action": "skip", "reply_text": "", "reason": "系统提示事件非HR发言，跳过", "source": "rule"}

        prompt = self.build_agent_prompt(conv)
        gen = agent_generator or self.agent_generator

        # 2. 尝试调用 Agent 生成（函数钩子或大模型 API）
        agent_res = None
        if gen:
            try:
                import inspect
                sig = inspect.signature(gen)
                if len(sig.parameters) >= 2:
                    agent_res = gen(conv, prompt)
                else:
                    agent_res = gen(conv)
            except Exception:
                agent_res = None

        if not agent_res:
            agent_res = self._try_llm_generate(conv, prompt)

        # 3. Agent 成功产出回复时的处理与门禁
        if isinstance(agent_res, dict) and agent_res.get("action"):
            act = agent_res.get("action")
            if act in ("reply", "send_resume", "exchange_wechat", "agree_wechat"):
                reply_text = sanitize_and_clean_reply(agent_res.get("reply_text") or "", max_chars=150)

                is_leak, leak_detail = detect_privacy_leak(reply_text, self.cfg, self.profile)
                if act == "reply" and not reply_text:
                    pass  # 回复为空，落入人工处理
                elif is_leak:
                    # 触发物理防套话防泄密门禁：坚决不向外发送明文隐私，转人工
                    return {
                        "action": "needs_human",
                        "reply_text": "",
                        "blocked_privacy_leak": True,
                        "reason": f"模型回复文案触发物理防套话防泄密门禁（{leak_detail}），安全拦截并转人工",
                        "notice": f"HR [{who}] 发来消息: \"{last_msg}\"，模型回复疑似明文透露隐私（{leak_detail}），已物理拦截并转人工！",
                        "source": "privacy_guard",
                    }
                elif reply_text and greeter.privacy_blocked(reply_text):
                    # Agent 产出文案触犯原有隐私红线（如泄露手机号/电话），安全门禁强制转人工
                    return {
                        "action": "needs_human",
                        "reply_text": "",
                        "reason": "Agent生成文案包含联系方式/电话隐私，安全门禁拦截并转人工",
                        "notice": f"HR [{who}] 发来消息: \"{last_msg}\"，Agent回复文案包含敏感联系方式，已拦截并转人工！",
                        "source": "privacy_guard",
                    }
                else:
                    return {
                        "action": act,
                        "reply_text": reply_text,
                        "reason": agent_res.get("reason") or f"Agent执行动作: {act}",
                        "notice": agent_res.get("notice") or "",
                        "source": agent_res.get("source") or "agent",
                    }
            elif act in ("needs_human", "skip"):
                return agent_res

        # 4. 无法通过 Agent 进行回复时：坚决废除确定性模板降级，全权告知人工进行处理
        notice_msg = f"HR [{who}] 发来消息: \"{last_msg}\"，当前无法通过 Agent 进行智能回复，请人工查看并处理！"
        return {
            "action": "needs_human",
            "reply_text": "",
            "reason": "无法调用 Agent/大模型进行智能回复（已禁用确定性模板降级），转人工处理",
            "notice": notice_msg,
            "source": "human_handoff",
        }

    def _try_llm_generate(self, conv: dict, prompt: Optional[str] = None) -> Optional[dict]:
        """尝试使用大模型 API 作为 Agent 生成回复。
        瞬时故障（网络抖动/中转偶发 5xx）单次重试：同路径重试，非模板降级，
        不违反"无法生成就转人工"的哲学（2026-09-08 实测瞬时失败 3 次/全天）。"""
        if not (self.openrouter_key or self.openai_key):
            return None

        if not prompt:
            prompt = self.build_agent_prompt(conv)

        for attempt in range(2):  # 1 次原始调用 + 1 次重试
            parsed = self._llm_call_once(prompt)
            if parsed is not None:
                return parsed
            if attempt == 0:
                time.sleep(2)  # 瞬时故障退避 2s 后重试一次
        return None

    def _llm_call_once(self, prompt: str) -> Optional[dict]:
        """单次 LLM 调用：返回解析后的 {action,...} dict；任何异常/格式异常返回 None。"""
        try:
            import urllib.request

            headers = {"Content-Type": "application/json", "User-Agent": "boss-apply/1.0"}
            if self.openrouter_key:
                url = "https://openrouter.ai/api/v1/chat/completions"
                headers["Authorization"] = f"Bearer {self.openrouter_key}"
                model = self.llm_model if self.llm_model != "gpt-4o-mini" else "deepseek/deepseek-chat"
            else:
                base = self.openai_base.rstrip("/")
                url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
                headers["Authorization"] = f"Bearer {self.openai_key}"
                model = self.llm_model

            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一名求职助理 Agent，代表求职者回复招聘平台HR消息。严格输出纯JSON。真人口语化，杜绝客服八股文，严禁使用任何Markdown标记（如**），保持整句完整自然收尾。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.5,
                # 推理模型（dots3-note-prev 等）深度思考默认开启，思考过程（reasoning_content）
                # 与正文共享 max_tokens 输出额度，实测思考可达 3000+ token；8192 留足余量。
                "max_tokens": 8192,
            }

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers)
            # 深度思考出字慢（实测一次决策 15-40s），25s 超时是偶发失败主因，放宽至 60s
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    msg = resp_data["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    content = re.sub(r"^```(?:json)?\s*", "", content)
                    content = re.sub(r"```$", "", content).strip()
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "action" in parsed:
                        act = parsed.get("action")
                        if act in ("reply", "send_resume", "exchange_wechat", "agree_wechat", "needs_human", "skip"):
                            parsed["reply_text"] = sanitize_and_clean_reply(parsed.get("reply_text") or "", max_chars=150)
                            return parsed
        except Exception:
            pass
        return None

