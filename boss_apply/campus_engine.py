"""实习/校招双模与时令引擎模块（2026-09-12 实施计划 Module 2）。

核心组件：
1. HiringClock: 招聘时令时钟（感知月份/季节生命周期，动态调整校招/实习推荐配比与时令紧迫感）
2. InternSpecExtractor: 结构化实习/校招规格抽取器（正则提取出勤天数、实习时长、立即到岗、转正概率等）
3. DifferentiatedGreeter: 差异化打招呼生成器（实习模式强调零课业/满勤/长期/即战力；校招模式强调统招应届/GMV战果/架构交付）
"""
import datetime
import re
from typing import Any, Dict, List, Optional, Union


class HiringClock:
    """招聘时令时钟：动态感知自然月生命周期与招聘潮汐。"""

    AUTUMN_FORMAL = "autumn_formal"            # 9-10月：秋招正式批黄金期
    AUTUMN_SUPPLEMENT = "autumn_supplement"    # 11-1月：秋招补录期
    SPRING_ADVANCE = "spring_advance"          # 2月：春招提前预热期
    SPRING_FORMAL = "spring_formal"            # 3-4月：春招金三银四
    DAILY_INTERN_ADVANCE = "daily_intern_advance"  # 5-8月：日常实习与秋招提前批

    @classmethod
    def get_current_stage(cls, dt: Optional[Union[datetime.datetime, datetime.date]] = None) -> str:
        """根据日期识别当前时令阶段 code。"""
        if dt is None:
            dt = datetime.datetime.now()
        month = dt.month
        if month in (9, 10):
            return cls.AUTUMN_FORMAL
        elif month in (11, 12, 1):
            return cls.AUTUMN_SUPPLEMENT
        elif month == 2:
            return cls.SPRING_ADVANCE
        elif month in (3, 4):
            return cls.SPRING_FORMAL
        else:  # 5, 6, 7, 8
            return cls.DAILY_INTERN_ADVANCE

    @classmethod
    def get_season_profile(cls, dt: Optional[Union[datetime.datetime, datetime.date]] = None) -> Dict[str, Any]:
        """返回当前时令阶段的详细画像配置字典：
        含阶段名称、推荐模态、校招与实习推荐配比、紧迫感文案修饰符及描述。
        """
        if dt is None:
            dt = datetime.datetime.now()
        month = dt.month
        year = dt.year
        stage = cls.get_current_stage(dt)

        grad_year = (year + 1) if month >= 6 else year
        if stage == cls.AUTUMN_FORMAL:
            stage_name = "秋招正式批黄金期"
            rec_mode = "mix"
            campus_ratio = 0.70
            intern_ratio = 0.30
            urgency_modifier = f"【秋招黄金档·直通{grad_year}正编】"
            desc = f"9-10月为秋招正式批与秋招黄金期，核心推荐70% {grad_year}届校招正编 + 30% 大厂转正实习。"
        elif stage == cls.AUTUMN_SUPPLEMENT:
            stage_name = "秋招补录期"
            rec_mode = "mix"
            campus_ratio = 0.50
            intern_ratio = 0.50
            urgency_modifier = "【秋招补录冲刺】"
            desc = "11-1月为秋招收尾与补录冲刺期，推荐校招补录与寒春长期实习各占50%。"
        elif stage == cls.SPRING_ADVANCE:
            stage_name = "春招提前预热期"
            rec_mode = "intern"
            campus_ratio = 0.40
            intern_ratio = 0.60
            urgency_modifier = "【春招早鸟预热】"
            desc = "2月为春招提前预热与寒假实习，可重点切入大厂转正实习。"
        elif stage == cls.SPRING_FORMAL:
            stage_name = "春招金三银四"
            rec_mode = "mix"
            campus_ratio = 0.60
            intern_ratio = 0.40
            urgency_modifier = "【金三银四·春招黄金期】"
            desc = "3-4月为春招金三银四决战期，推荐60%校招抢跑+40%春招转正实习。"
        else:  # DAILY_INTERN_ADVANCE
            stage_name = "日常实习与提前批"
            rec_mode = "intern"
            campus_ratio = 0.30
            intern_ratio = 0.70
            urgency_modifier = "【大厂实习急招】"
            desc = "5-8月为暑期实习与秋招提前批，主推可长期转正日常实习与提前批。"

        return {
            "stage": stage,
            "stage_name": stage_name,
            "month": month,
            "year": year,
            "recommended_mode": rec_mode,
            "campus_ratio": campus_ratio,
            "intern_ratio": intern_ratio,
            "mode_ratio": {"campus": campus_ratio, "intern": intern_ratio},
            "urgency_modifier": urgency_modifier,
            "description": desc,
        }


class InternSpecExtractor:
    """结构化实习/校招规格抽取器：从 JD 全文与职位标签提取结构化出勤、时长、到岗与转正信息。"""

    # 正则规则表
    # 1. 出勤天数：每周天数
    _RE_DAYS_TAG = re.compile(r"([1-7])\s*天\s*[/／每]\s*(?:周|星期)")
    _RE_DAYS_RANGE = re.compile(r"([1-7])\s*[-~至到/]\s*([1-7])\s*天")
    _RE_DAYS_PER_WEEK = re.compile(
        r"(?:(?:每周|每星期|出勤|到岗|一周)\s*([1-7])\s*天)|"
        r"(?:([1-7])\s*天\s*[/／每]\s*(?:周|星期))|"
        r"(?:([1-7])\s*天及以上)|"
        r"(?:至少\s*([1-7])\s*天)"
    )

    # 2. 实习月数：连续月数
    _RE_MONTHS_RANGE = re.compile(r"(\d+)\s*[-~至到/]\s*(\d+)\s*个?月")
    _RE_MONTHS = re.compile(
        r"(?:(?:至少|最低|连续|能够实习|保证实习|实习期|实习)\s*(\d+)\s*个?月)|"
        r"(?:(\d+)\s*个?月\s*及?以上)|"
        r"(?:(\d+)\s*个?月)"
    )

    # 3. 立即到岗 / 紧迫度
    _RE_IMMEDIATE = re.compile(
        r"(?:尽快|立即|即刻|随时|近期|快速|快速入职|尽快入职|立即到岗|随时到岗|急招|招满即止|急聘)"
    )

    # 4. 转正机会（否定与肯定）
    _RE_CONVERSION_NEG = re.compile(
        r"(?:不(?:提供|承诺|支持|具备)|无|没有|不招)\s*(?:转正|校招转正|转正hc|hc)|"
        r"(?:暂无|暂不考虑)\s*转正|"
        r"纯日常实习|不转正|无转正机会"
    )
    _RE_CONVERSION_POS = re.compile(
        r"(?:表现(?:优异|优秀|良好)?[可者]?(?:申请|获得|提供|享有)?转正|"
        r"有转正(?:机会|名额|考核|通道|hc)?|"
        r"可转正|"
        r"提供转正|"
        r"转正实习|"
        r"转正率|"
        r"校招转正|"
        r"转正hc|"
        r"留用机会|"
        r"优秀可转正|"
        r"表现好可转正|"
        r"表现突出可转正|"
        r"优秀者可留用|"
        r"转正概率高)"
    )

    @classmethod
    def extract_specs(
        cls,
        jd_text: str = "",
        tags: str = "",
        title: str = ""
    ) -> Dict[str, Any]:
        """从 JD 全文、标签和岗位标题中抽取结构化实习/校招规格。"""
        full_text = f"{title}\n{tags}\n{jd_text}"
        raw_matched: List[str] = []

        # 1. 抽取 days_per_week
        days_per_week: Optional[int] = None
        # 先看标签（如 "4天/周"）
        m_tag = cls._RE_DAYS_TAG.search(tags)
        if m_tag:
            days_per_week = int(m_tag.group(1))
            raw_matched.append(f"tag_days:{days_per_week}")
        else:
            # 查找区间
            m_range = cls._RE_DAYS_RANGE.search(full_text)
            if m_range:
                d1, d2 = int(m_range.group(1)), int(m_range.group(2))
                days_per_week = min(d1, d2)  # 取最低出勤要求天数
                raw_matched.append(f"range_days:{d1}-{d2}")
            else:
                m_single = cls._RE_DAYS_PER_WEEK.search(full_text)
                if m_single:
                    for g in m_single.groups():
                        if g:
                            days_per_week = int(g)
                            raw_matched.append(f"single_days:{days_per_week}")
                            break

        # 2. 抽取 duration_months
        duration_months: Optional[int] = None
        m_mrange = cls._RE_MONTHS_RANGE.search(full_text)
        if m_mrange:
            m1, m2 = int(m_mrange.group(1)), int(m_mrange.group(2))
            duration_months = min(m1, m2)
            raw_matched.append(f"range_months:{m1}-{m2}")
        else:
            m_msingle = cls._RE_MONTHS.search(full_text)
            if m_msingle:
                for g in m_msingle.groups():
                    if g:
                        duration_months = int(g)
                        raw_matched.append(f"single_months:{duration_months}")
                        break

        # 3. 抽取 immediate_onboarding
        immediate_onboarding = bool(cls._RE_IMMEDIATE.search(full_text))
        if immediate_onboarding:
            raw_matched.append("immediate_onboarding")

        # 4. 抽取 has_conversion_chance 与 conversion_prob
        has_conversion = False
        prob = 0.25

        if cls._RE_CONVERSION_NEG.search(full_text):
            has_conversion = False
            prob = 0.0
            raw_matched.append("conversion_neg")
        elif cls._RE_CONVERSION_POS.search(full_text):
            has_conversion = True
            raw_matched.append("conversion_pos")
            if any(k in full_text for k in ("校招实习", "面向27届", "面向2027届", "转正率高", "转正hc充足", "留用率高")):
                prob = 0.95
            elif any(k in full_text for k in ("表现优异可转正", "表现优秀可转正", "表现好可转正")):
                prob = 0.85
            elif "转正机会" in full_text or "可转正" in full_text or "提供转正" in full_text:
                prob = 0.80
            else:
                prob = 0.75
        else:
            # 标题特征
            if "转正" in title or "校招" in title:
                has_conversion = True
                prob = 0.85
                raw_matched.append("title_conversion")
            elif "日常实习" in title:
                has_conversion = False
                prob = 0.10
                raw_matched.append("title_daily_intern")
            else:
                has_conversion = False
                prob = 0.25

        return {
            "days_per_week": days_per_week,
            "duration_months": duration_months,
            "immediate_onboarding": immediate_onboarding,
            "has_conversion_chance": has_conversion,
            "conversion_prob": round(prob, 2),
            "raw_matched": raw_matched,
        }


class DifferentiatedGreeter:
    """差异化打招呼生成器：针对实习与校招模式定制高转化沟通话术。
    严格遵循事实真实性底线，绝无硬编码虚构经历。
    """

    @classmethod
    def generate_greeting(
        cls,
        job: Dict[str, Any],
        job_mode: Optional[str] = None,
        specs: Optional[Dict[str, Any]] = None,
        cfg: Optional[Dict[str, Any]] = None,
        profile: Optional[Dict[str, Any]] = None,
    ) -> str:
        """根据 job_mode（'intern' 或 'campus'）及候选人真实画像生成差异化打招呼话术。
        空画像或未确认关键事实时，一律回退至中性安全真实文案。
        """
        cfg = cfg or {}
        mode = job_mode or job.get("job_mode") or cfg.get("job_mode") or "intern"
        title = (job.get("title") or "该岗位").strip()
        city = (job.get("city") or "").strip()

        # 加载真实画像（若外部未显式传入）
        prof = profile
        if prof is None:
            try:
                from . import profile_store
                prof = profile_store.load()
            except Exception:
                prof = {}
        prof = prof or {}

        # 检查画像是否具备确认的事实字段
        has_facts = any(bool(prof.get(k)) for k in ("grade_desc", "grad_year", "school", "major", "tech_highlights", "business_highlights", "availability"))

        # 空画像或缺乏事实支撑时：直接返回专业、中性、安全真实的开场白
        city_phrase = f"在{city}的" if city else ""
        if not has_facts:
            return f"您好！看到贵司正在招聘{city_phrase}{title}岗位，我对该方向非常感兴趣，希望能与您进一步沟通了解具体要求，谢谢！"

        # 有画像时，基于实际已配置的事实组装
        grade_desc = prof.get("grade_desc") or (f"{prof['grad_year']}届应届生" if prof.get("grad_year") else "")
        grade_part = f"我是{grade_desc}，" if grade_desc else ""
        
        tech_hl = prof.get("tech_highlights") or ""
        biz_hl = prof.get("business_highlights") or ""
        avail = prof.get("availability") or ""
        target_city = city or prof.get("target_region") or ""
        city_target_part = f"意向在{target_city}全职发展，" if target_city else ""

        if mode == "intern":
            avail_part = f"{avail}，" if avail else ""
            hl_part = f"具备{tech_hl}实践经验，" if tech_hl else (f"具备{biz_hl}，" if biz_hl else "")
            text = f"您好！看到贵司招聘{title}，{grade_part}{avail_part}{hl_part}{city_target_part}诚意应聘，期待深入交流！"
        elif mode == "campus":
            hl_part = f"拥有{tech_hl}交付能力并具备{biz_hl}，" if (tech_hl and biz_hl) else (f"拥有{tech_hl}交付能力，" if tech_hl else (f"具备{biz_hl}，" if biz_hl else ""))
            text = f"您好！看到贵司{title}校招机会，{grade_part}{hl_part}{city_target_part}诚意应聘，期待深入探讨！"
        else:
            hl_part = f"具备{tech_hl}与{biz_hl}能力，" if (tech_hl and biz_hl) else (f"具备{tech_hl or biz_hl}，" if (tech_hl or biz_hl) else "")
            text = f"您好！关注到贵司{title}岗位，{grade_part}{hl_part}{city_target_part}诚意应聘，期待深入交流！"

        # 检查隐私安全，若有泄露则保底清理
        from .greeter import privacy_blocked
        if privacy_blocked(text):
            text = f"您好！看到贵司招聘{title}，我对该方向很感兴趣，期待能与您深入沟通了解具体要求，谢谢！"

        return text
