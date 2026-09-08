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
    - '09月01日', '08月31日' -> 历史旧会话，默认跳过
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
    return False


class AIReplyEngine:
    """纯 Agent / 大模型驱动的会话决策与回复生成引擎（彻底废除确定性模板降级）。"""

    def __init__(
        self,
        cfg: Optional[dict] = None,
        profile: Optional[dict] = None,
        agent_generator: Optional[Any] = None,
    ):
        self.cfg = cfg or {}
        self.profile = dict(CANDIDATE_PROFILE, **(profile or {}))
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

        return (
            f"【候选人真实画像】\n"
            f"- 姓名：{self.profile['name']}\n"
            f"- 学历与专业：{self.profile['school']} · {self.profile['major']}\n"
            f"- 毕业届别与状态：{self.profile['grade_desc']}（目前2026年9月处于秋招黄金期，毕业设计已交付，无在校日常课程）\n"
            f"- 常驻地与意向城市：目前常驻【{self.profile['current_city']}】，核心意向奔赴【{self.profile['target_region']}】发展\n"
            f"- 到岗与稳定性：{self.profile['availability']}\n"
            f"- 薪资底线诉求：{self.profile['salary_requirement']}\n"
            f"- 核心优势：全栈MCP/Agent工程落地经验 + 200人团队月操盘10万GMV的商业化即战力\n"
            f"{jd_section}\n"
            f"【当前HR与最新消息】\n"
            f"- 对话方：{who}\n"
            f"- HR最新消息：\"{last_msg}\"\n\n"
            f"【Agent 沟通策略与心法】\n"
            f"1. 问常驻地 / 能否线下面试（如在杭州吗/在本地吗/人在哪/接受线下吗）：真诚告知目前常驻福州，表达强烈意向奔赴江浙沪，并提议初试先通过线上（腾讯会议等方式）高效推进，合适随时到岗；\n"
            f"2. 问期望薪资（如期望薪资是多少/对实习待遇有什么要求）：说明考虑跨城前往江浙沪全职实习，主要希望能覆盖当地基础租房与生活开销（如日薪180-250左右或有房补即可）；核心依然最看重业务与团队匹配度，并索要详细JD；\n"
            f"3. 索要联系方式（微信/电话/邮箱）：引导留存平台沟通更及时，反客为主索要岗位详细JD；\n"
            f"4. 邀约面试 / 问到岗时间：说明学业与时间相对具备弹性，主动索要岗位JD与具体安排，绝不擅自承诺死时间；\n"
            f"5. 三不原则：不承诺（时间保留弹性）、不拒绝（保持积极开放）、不负责（主动索要详细JD互相了解）；\n"
            f"6. 若上方提供了【会话关联岗位与JD】：结合JD研判含金量后见人下菜碟——大模型/Agent核心产品岗可显著提升热情并呼应JD匹配点；披着AI外衣的销售/地推/杂役岗保持礼貌太极、点到为止，不深聊不主动推进；\n"
            f"7. 绝对隐私红线：严禁在文案中输出真实11位手机号、座机电话、微信号或外部链接。\n\n"
            f"请输出纯 JSON 格式：\n"
            f'{{"action": "reply"|"needs_human"|"skip", "reason": "理由", "reply_text": "50-100字拟人高情商回复（表达开放，反索JD）"}}'
        )

    def decide_and_generate(self, conv: dict, agent_generator: Optional[Any] = None) -> dict:
        """输入会话信息，驱动 Agent 生成回复。
        若无可用 Agent 或生成失败，坚决不使用确定性模板降级，全权告知人工进行处理。
        """
        last_msg = (conv.get("last_msg") or "").strip()
        who = conv.get("who") or ""

        # 1. 基础门禁：无实质内容跳过
        if not last_msg:
            return {"action": "skip", "reply_text": "", "reason": "消息为空", "source": "rule"}

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
            if act == "reply":
                reply_text = (agent_res.get("reply_text") or "").strip()
                if not reply_text:
                    pass  # 回复为空，落入人工处理
                elif greeter.privacy_blocked(reply_text):
                    # Agent 产出文案触犯隐私红线（如泄露手机号/电话），安全门禁强制转人工
                    return {
                        "action": "needs_human",
                        "reply_text": "",
                        "reason": "Agent生成文案包含联系方式/电话隐私，安全门禁拦截并转人工",
                        "notice": f"HR [{who}] 发来消息: \"{last_msg}\"，Agent回复文案包含敏感联系方式，已拦截并转人工！",
                        "source": "privacy_guard",
                    }
                else:
                    return {
                        "action": "reply",
                        "reply_text": reply_text[:120],
                        "reason": agent_res.get("reason") or "Agent智能生成拟人回复",
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
        """尝试使用大模型 API 作为 Agent 生成回复。"""
        if not (self.openrouter_key or self.openai_key):
            return None

        if not prompt:
            prompt = self.build_agent_prompt(conv)

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
                    {"role": "system", "content": "你是一名求职助理 Agent，代表求职者回复招聘平台HR消息。严格输出纯JSON。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.5,
                # 推理模型（dots3-note-prev 等）会先输出 reasoning_content（思考过程，
                # 实测一次占 ~1500+ token）再输出正文 content；额度不足时 content 被
                # 截断/为空导致 JSON 解析失败。4096 保证思考+正文完整产出。
                "max_tokens": 4096,
            }

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                if resp.status == 200:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    msg = resp_data["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    content = re.sub(r"^```json\s*", "", content)
                    content = re.sub(r"```$", "", content).strip()
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "action" in parsed:
                        return parsed
        except Exception:
            pass
        return None
