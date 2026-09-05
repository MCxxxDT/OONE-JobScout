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
    "grade_desc": "2027届在读",
    "availability": "随时可到岗，可长期实习6个月以上，每周到岗5天",
    "target_roles": "AI产品经理 / Agent产品 / 大模型应用产品 / 商业化产品 / 实习",
    "tech_highlights": "熟练FastMCP、Coze智能体中台编排、Trae原生Skills、CDP自动化、Python与系统工程",
    "business_highlights": "曾带200+人校园团队实现单月GMV破10万、复购80%、私域月沉淀5000+，具备较强商业化与ToB沟通能力",
}

# 高敏感意图关键词（必须拦截转人工）
SENSITIVE_PATTERNS = [
    re.compile(r"(?:加|留|给|发|换)\s*个?\s*(?:微信|vx|v号|联系方式|手机|电话)", re.I),
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"(?:微信号|手机号|电话号|联系电话|邮箱|发送到邮箱|发一份pdf)", re.I),
    re.compile(r"(?:明天|后天|下周|今晚|上午|下午)?\s*\d{1,2}(?:点|:00|半)\s*(?:来|进行)?\s*(?:面试|线下面|沟通)", re.I),
    re.compile(r"(?:腾讯会议|钉钉会议|飞书会议|线上会议|来公司面试|现场面|线下面试|面试地址)", re.I),
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
    """智能会话决策与回复生成引擎。"""

    def __init__(self, cfg: Optional[dict] = None, profile: Optional[dict] = None):
        self.cfg = cfg or {}
        self.profile = dict(CANDIDATE_PROFILE, **(profile or {}))
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY") or ""
        self.openai_key = os.getenv("OPENAI_API_KEY") or ""
        self.openai_base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"

    def decide_and_generate(self, conv: dict) -> dict:
        """输入会话信息（含 who, last_msg, time 等），输出决策与回复文案：
        {
            "action": "reply" | "needs_human" | "skip",
            "reply_text": str,
            "reason": str,
            "source": "rule" | "llm" | "template"
        }
        """
        last_msg = (conv.get("last_msg") or "").strip()
        who = conv.get("who") or ""

        # 1. 基础门禁：无实质内容
        if not last_msg:
            return {"action": "skip", "reply_text": "", "reason": "消息为空", "source": "rule"}

        # 2. 隐私与敏感意图拦截（强制转人工）
        if greeter.privacy_blocked(last_msg):
            return {
                "action": "needs_human",
                "reply_text": "",
                "reason": "命中隐私安全词表（含手机/微信联系方式）",
                "source": "rule",
            }

        for pat in SENSITIVE_PATTERNS:
            if pat.search(last_msg):
                return {
                    "action": "needs_human",
                    "reply_text": "",
                    "reason": "命中高敏感意图（索要联系方式/面试邀约/薪资谈判）",
                    "source": "rule",
                }

        # 3. 尝试调用大模型生成（若可用且有效）
        llm_res = self._try_llm_generate(conv)
        if llm_res and llm_res.get("action"):
            # 严格二次校验 LLM 生成的内容
            reply_text = (llm_res.get("reply_text") or "").strip()
            if llm_res["action"] == "reply" and reply_text:
                if greeter.privacy_blocked(reply_text):
                    return {
                        "action": "needs_human",
                        "reply_text": "",
                        "reason": "大模型生成文案含联系方式，安全拦截转人工",
                        "source": "rule",
                    }
                return {
                    "action": "reply",
                    "reply_text": reply_text[:120],
                    "reason": llm_res.get("reason") or "大模型生成",
                    "source": "llm",
                }
            elif llm_res["action"] in ("needs_human", "skip"):
                return {
                    "action": llm_res["action"],
                    "reply_text": "",
                    "reason": llm_res.get("reason") or "大模型意图判定",
                    "source": "llm",
                }

        # 4. 高质量确定性模板自动降级
        return self._generate_template_reply(conv)

    def _generate_template_reply(self, conv: dict) -> dict:
        """根据 HR 消息上下文与关键词，生成精准贴合的拟人回复。"""
        msg = conv.get("last_msg") or ""
        grad_year = self.profile.get("grad_year", 2027)

        # 场景 A: 岗位推荐/校招/BD/商业化场景
        if any(k in msg for k in ("BD", "bd", "销售", "商家", "拓展", "商务")):
            reply = (
                f"您好！感谢您的关注与推荐。我是福建师大{grad_year}届在读，可实习6个月以上且每周到岗5天。"
                f"之前带过200人校园团队做过单月10万+GMV业务，对贵司商业拓展方向很感兴趣，期待能有进一步沟通机会，谢谢！"
            )
            return {"action": "reply", "reply_text": reply, "reason": "匹配商业化/BD岗位推荐场景", "source": "template"}

        # 场景 B: 询问届别 / 到岗时间 / 实习周期
        if any(k in msg for k in ("几届", "毕业时间", "到岗", "实习多久", "每周几天", "多久可以入职")):
            reply = (
                f"您好！我是{grad_year}届在读，随时可到岗，可长期实习6个月以上且保证每周到岗5天，期待与您详细沟通！"
            )
            return {"action": "reply", "reply_text": reply, "reason": "回答届别与到岗实习周期", "source": "template"}

        # 场景 C: HR 发来打招呼 / 表达对简历感兴趣 / 邀请沟通
        if any(k in msg for k in ("感兴趣", "沟通一下", "聊聊", "你好", "推荐", "投递", "校招", "同学")):
            reply = (
                f"您好！非常感谢您的关注与招呼。我对贵司的发展方向非常感兴趣，我是{grad_year}届在读，可实习6个月以上每周到岗5天，期待能与您进一步沟通！"
            )
            return {"action": "reply", "reply_text": reply, "reason": "标准HR招呼与意向响应", "source": "template"}

        # 场景 D: 通用兜底表达兴趣并询问详细 JD
        reply = (
            f"您好！感谢关注。我对贵司该方向很感兴趣，我是{grad_year}届在读，可稳定实习6个月以上，方便发一下岗位的详细JD吗？谢谢！"
        )
        return {"action": "reply", "reply_text": reply, "reason": "通用友好回应并询问JD", "source": "template"}

    def _try_llm_generate(self, conv: dict) -> Optional[dict]:
        """尝试使用大模型 API 进行意图识别与回复文案生成（若 API 无法接通则返回 None）。"""
        if not (self.openrouter_key or self.openai_key):
            return None

        prompt = (
            f"求职者画像：姓名【{self.profile['name']}】，{self.profile['school']}{self.profile['major']}（{self.profile['grade_desc']}）。\n"
            f"到岗情况：{self.profile['availability']}。\n"
            f"技术与业务亮点：{self.profile['tech_highlights']}；{self.profile['business_highlights']}。\n\n"
            f"当前 HR 信息：{conv.get('who')}\n"
            f"HR 最新消息：\"{conv.get('last_msg')}\"\n\n"
            f"请判断意图并按以下 JSON 格式返回（不带 markdown 标记）：\n"
            f'{{"action": "reply"|"needs_human"|"skip", "reason": "分类理由", "reply_text": "拟人回复文案（50-100字，亲切得体真诚，杜绝AI套话，绝对不留电话微信）"}}'
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
