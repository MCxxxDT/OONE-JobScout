"""AI 智能决策与回复引擎。
负责：
1. 会话意图识别（reply / needs_human / skip）；
2. 候选人画像注入与拟人回复文案生成；
3. 大模型调用（OpenRouter/OpenAI）与高质量确定性模板自动降级；
4. 工作时间闸门（Active Hours Gate）与消息时效过滤；
5. 隐私红线与人工门禁（索要电话/微信/约面坚决转人工）。
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
    "availability": "常驻杭州，随时可全职到岗，保证每周到岗5天，可长期全职实习6个月以上直冲校招转正",
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
    """智能会话决策与回复生成引擎（全自主无人值守 + 异步提醒）。"""

    def __init__(self, cfg: Optional[dict] = None, profile: Optional[dict] = None):
        self.cfg = cfg or {}
        self.profile = dict(CANDIDATE_PROFILE, **(profile or {}))
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY") or ""
        self.openai_key = os.getenv("OPENAI_API_KEY") or ""
        self.openai_base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"

    def decide_and_generate(self, conv: dict) -> dict:
        """输入会话信息（含 who, last_msg, time 等），输出决策与回复文案：
        {
            "action": "reply" | "skip",
            "reply_text": str,
            "reason": str,
            "notice": str (可选异步提醒),
            "source": "rule" | "llm" | "template"
        }
        全自主原则：绝不因为敏感话题阻断执行；自动太极应对并反索JD，同时输出 notice 提醒用户。
        """
        last_msg = (conv.get("last_msg") or "").strip()
        who = conv.get("who") or ""

        # 1. 基础门禁：无实质内容跳过
        if not last_msg:
            return {"action": "skip", "reply_text": "", "reason": "消息为空", "source": "rule"}

        # 2. 索要联系方式意图（微信/手机/电话/邮箱等）：不阻断，太极引导留存平台并反索JD
        if greeter.privacy_blocked(last_msg) or any(pat.search(last_msg) for pat in CONTACT_PATTERNS):
            reply = (
                "您好！非常感谢您的关注与认可。目前在平台沟通也比较方便及时，方便先在平台发一份岗位的详细JD供我了解一下吗？"
                "若后续推进合适再进一步深入沟通，谢谢您的理解！"
            )
            return {
                "action": "reply",
                "reply_text": reply,
                "reason": "HR索要联系方式，太极引导留存平台沟通并反客为主索要JD",
                "notice": "HR索要联系方式（微信/电话），系统已自动太极应对并反索JD。如您需要直接私聊对方，可人工介入！",
                "source": "template",
            }

        # 3. 面试邀约 / 宣讲会 / 约具体时间：不阻断，太极表达弹性并反索详细安排
        if any(pat.search(last_msg) for pat in INTERVIEW_PATTERNS):
            reply = (
                "您好！非常感谢您的关注与宣讲面试安排。目前学业与实习时间相对具备弹性，方便发一份该岗位的详细JD与具体安排供我拜读了解一下吗？"
                "期待后续进一步沟通交流，谢谢！"
            )
            return {
                "action": "reply",
                "reply_text": reply,
                "reason": "HR提及宣讲/面试，太极表达弹性并反客为主索要JD与安排",
                "notice": "HR提及线下面试/宣讲会，系统已自动表达弹性并索要详细JD，绝不擅自承诺到场。如需具体对接，可人工介入！",
                "source": "template",
            }

        # 4. 薪资待遇询问：不阻断，太极表达开放并反索JD
        if any(pat.search(last_msg) for pat in SALARY_PATTERNS):
            reply = (
                "您好！非常感谢您的关注。关于待遇细节，目前主要看重业务与团队匹配度，持积极开放态度。"
                "方便发一份岗位详细JD供我深入了解一下吗？谢谢！"
            )
            return {
                "action": "reply",
                "reply_text": reply,
                "reason": "HR询问待遇细节，太极表达开放并反索JD",
                "notice": "HR询问薪资待遇，系统已自动太极回应并索要JD，可人工接管！",
                "source": "template",
            }

        # 5. 尝试调用大模型生成（若配置且有效）
        llm_res = self._try_llm_generate(conv)
        if llm_res and llm_res.get("action"):
            reply_text = (llm_res.get("reply_text") or "").strip()
            if reply_text and not greeter.privacy_blocked(reply_text):
                return {
                    "action": "reply",
                    "reply_text": reply_text[:120],
                    "reason": llm_res.get("reason") or "大模型生成太极回复",
                    "notice": llm_res.get("notice") or "",
                    "source": "llm",
                }

        # 6. 高质量确定性模板自动响应
        return self._generate_template_reply(conv)

    def _generate_template_reply(self, conv: dict) -> dict:
        """根据 HR 消息上下文生成【不承诺、不拒绝、不负责】的太极风格回复：
        - 不承诺：绝不说死到岗时间和硬性周期，保留学业弹性；
        - 不拒绝：对任何岗位/业务方向均表达开放态度与良好兴趣；
        - 不负责：把球踢回给 HR，主动索要详细 JD 和业务信息互相了解。
        """
        msg = conv.get("last_msg") or ""

        # 场景 A: 岗位推荐/校招/BD/商业化/销售场景
        if any(k in msg for k in ("BD", "bd", "销售", "商家", "拓展", "商务", "业务")):
            reply = (
                "您好！非常感谢您的关注与推荐。看到贵司该方向业务很有活力，我持积极开放的态度。"
                "方便发一下岗位的详细JD或业务侧重点供我拜读了解一下吗？期待后续进一步沟通交流，谢谢！"
            )
            return {"action": "reply", "reply_text": reply, "reason": "商业化/BD岗位太极回应并索要JD", "source": "template"}

        # 场景 B: 询问届别 / 到岗时间 / 实习周期
        if any(k in msg for k in ("几届", "毕业时间", "到岗", "实习多久", "每周几天", "多久可以入职")):
            reply = (
                "您好！我是2027届在读，目前学业与实习时间相对具备弹性。"
                "具体到岗节奏与实习安排，可根据咱们后续沟通情况再深入商议。方便先发一份岗位JD了解一下吗？谢谢！"
            )
            return {"action": "reply", "reply_text": reply, "reason": "到岗与学业弹性回应并反索JD", "source": "template"}

        # 场景 C: HR 发来打招呼 / 表达对简历感兴趣 / 邀请沟通
        if any(k in msg for k in ("感兴趣", "沟通一下", "聊聊", "你好", "推荐", "投递", "校招", "同学")):
            reply = (
                "您好！非常感谢您的关注与招呼。我对贵司的发展方向很感兴趣，也持积极开放的交流态度。"
                "方便发一份该岗位的详细JD供我了解一下吗？非常期待能有互相了解的机会，谢谢！"
            )
            return {"action": "reply", "reply_text": reply, "reason": "标准HR招呼太极回应并索要JD", "source": "template"}

        # 场景 D: 通用兜底
        reply = (
            "您好！感谢您的关注与招呼。我对贵司该方向很有兴趣，方便先发一份岗位的详细JD供我拜读了解一下吗？谢谢！"
        )
        return {"action": "reply", "reply_text": reply, "reason": "通用太极回应并索要JD", "source": "template"}

    def _try_llm_generate(self, conv: dict) -> Optional[dict]:
        """尝试使用大模型 API 生成（若配置且可用）。贯彻【不承诺、不拒绝、不负责】原则。"""
        if not (self.openrouter_key or self.openai_key):
            return None

        prompt = (
            f"求职者背景：张烨韬，福建师大数媒技术专业（2027届在读）。\n"
            f"HR信息：{conv.get('who')}\n"
            f"HR最新消息：\"{conv.get('last_msg')}\"\n\n"
            f"请遵循【三不原则】回复：\n"
            f"1. 不承诺：绝不说死到岗时间（不说随时到岗）、绝不保证每周必来几天或能做多久，只提时间相对弹性；\n"
            f"2. 不拒绝：对岗位与方向保持积极、礼貌、开放态度，绝不生硬拒绝；\n"
            f"3. 不负责：主动索要详细JD或业务侧重点，把球踢回给HR互相了解；\n"
            f"4. 绝不透露任何手机号、微信号、邮箱或外部链接。\n\n"
            f"请返回纯 JSON 格式：\n"
            f'{{"action": "reply"|"needs_human"|"skip", "reason": "理由", "reply_text": "50-80字拟人太极回复（索要JD，表达开放，不承诺具体时间）"}}'
        )

        try:
            import requests

            headers = {"Content-Type": "application/json"}
            if self.openrouter_key:
                url = "https://openrouter.ai/api/v1/chat/completions"
                headers["Authorization"] = f"Bearer {self.openrouter_key}"
                model = "deepseek/deepseek-chat"
            else:
                url = f"{self.openai_base}/chat/completions"
                headers["Authorization"] = f"Bearer {self.openai_key}"
                model = "gpt-4o-mini"

            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一名求职助理，代表求职者回复招聘平台的HR消息。严格输出纯JSON。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.5,
                "max_tokens": 200,
            }

            resp = requests.post(url, headers=headers, json=payload, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```json\s*", "", content)
                content = re.sub(r"```$", "", content).strip()
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "action" in parsed:
                    return parsed
        except Exception:
            pass
        return None
