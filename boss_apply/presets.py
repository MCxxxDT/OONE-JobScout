# -*- coding: utf-8 -*-
"""OONE-JobScout 岗位偏好模板解耦引擎 (Presets Engine)

解耦原本硬编码于 config.json 与 ai_reply.py 的岗位偏好。
消除对技术研发岗（前端/后端/算法）与商务销售岗的误杀，提供多品类开箱预设模板。
"""
from typing import Dict, Any, List, Optional
import copy

PRESETS: Dict[str, Dict[str, Any]] = {
    "ai_pm": {
        "id": "ai_pm",
        "name": "AI / 智能体产品经理",
        "badge": "🤖 AI / PM",
        "description": "聚焦 AI Agent、Prompt 工程、工作流编排、大模型应用与商业化 PRD，适合 AI 产品及解决方案方向。",
        "keywords": [
            "AI产品经理",
            "AI产品 实习",
            "Agent 产品经理",
            "大模型 产品",
            "AI应用 产品经理",
            "智能体 产品",
            "AIGC 产品",
            "AI产品助理",
            "商业化产品经理"
        ],
        "score_words": {
            "title_kill": ["销售", "客服", "电销", "地推", "后端", "前端", "算法", "开发工程师", "测试工程师", "实施工程师", "评测工程师"],
            "strong": [
                "agent", "智能体", "workflow", "prompt", "coze", "dify", "fastmcp", "mcp", "trae",
                "cursor", "claude code", "vibe coding", "大模型应用", "llm", "ai产品", "产品助理",
                "编排", "工具链", "agent harness", "fde", "ai native", "多模态"
            ],
            "medium": ["需求分析", "原型", "prd", "用户调研", "数据分析", "商业化", "变现", "增长", "私域", "to b", "tob", "b端", "saas"],
            "weak": ["模型训练", "算法研究", "运维", "sre", "devops", "销售", "客服", "电话销售", "地推", "c++", "嵌入式"],
            "kill": ["外包", "培训", "劳务派遣", "驻场", "收费", "押金", "刷单", "保险", "直销"]
        },
        "reply_stance": "product",
        "stance_intro": "见人下菜碟（结合JD研判，核心产品岗提升热情，杂役销售岗点到为止）"
    },
    "tech_dev": {
        "id": "tech_dev",
        "name": "技术研发 / 软件工程",
        "badge": "💻 技术研发",
        "description": "放行前端、后端、算法、架构师、全栈开发与 AI 工程师，聚焦技术栈深度、系统架构与工程难点。",
        "keywords": [
            "后端开发",
            "前端开发",
            "Python开发",
            "Java开发",
            "全栈工程师",
            "算法工程师",
            "大模型算法",
            "AI工程师",
            "软件工程师",
            "Golang开发"
        ],
        "score_words": {
            "title_kill": ["销售", "客服", "电销", "地推", "房产经纪", "业务员", "推广专员", "文案策划", "前台", "行政专员"],
            "strong": [
                "python", "golang", "go", "java", "c++", "rust", "后端", "前端", "fullstack", "react", "vue",
                "fastapi", "spring", "微服务", "架构", "算法", "llm", "agent", "pytorch", "深度学习",
                "分布式", "高并发", "mysql", "redis", "docker", "k8s", "linux", "性能优化"
            ],
            "medium": ["git", "restful", "api", "设计模式", "重构", "tdd", "ci/cd", "消息队列", "kafka", "mq", "单元测试"],
            "weak": ["销售", "客服", "电话销售", "地推", "文案", "平面设计", "商务"],
            "kill": ["外包", "培训", "劳务派遣", "驻场", "收费", "押金", "刷单", "保险", "直销"]
        },
        "reply_stance": "tech",
        "stance_intro": "专业务实，聚焦技术栈契合度、工程落地能力与架构思考，真诚交流技术细节与项目难点"
    },
    "sales_bd": {
        "id": "sales_bd",
        "name": "市场销售 / 商务拓展 (BD)",
        "badge": "🤝 商务 / BD",
        "description": "针对销售与 BD 岗位转为主动出击姿态，突出商务拓展、客户沉淀与抗压执行力，积极推动微信与建联接洽。",
        "keywords": [
            "商务拓展",
            "BD经理",
            "大客户销售",
            "渠道拓展",
            "KA销售",
            "ToB销售",
            "企业业务拓展",
            "销售经理"
        ],
        "score_words": {
            "title_kill": ["技术支持", "算法工程师", "前端开发", "后端开发", "测试工程师", "运维工程师", "软件开发"],
            "strong": [
                "bd", "商务拓展", "大客户", "ka", "to b", "tob", "渠道", "客户沉淀", "业务开拓", "客情维护",
                "方案宣讲", "商务谈判", "业绩转化", "复购", "政企", "saas销售", "解决方案销售", "开拓新客户"
            ],
            "medium": ["crm", "销售漏斗", "市场分析", "客户拜访", "商机拓展", "行业资源", "抗压能力", "招投标", "公关"],
            "weak": ["前端", "后端", "算法", "运维", "嵌入式", "代码编写"],
            "kill": ["培训", "收费", "押金", "刷单", "无责任底薪为零", "传销"]
        },
        "reply_stance": "sales",
        "stance_intro": "主动进取、热情礼貌，突出业务拓展与客户链接能力，积极而得体地推动微信与电话接洽"
    },
    "general_ops": {
        "id": "general_ops",
        "name": "通用运营 / 综合职能",
        "badge": "📈 运营 / 综合",
        "description": "涵盖用户运营、产品运营、项目管理、HRBP 与综合行政，强调业务闭环推进与跨部门协调能力。",
        "keywords": [
            "用户运营",
            "产品运营",
            "活动运营",
            "内容运营",
            "项目经理",
            "项目助理",
            "HRBP",
            "运营专员"
        ],
        "score_words": {
            "title_kill": ["电销", "地推", "算法工程师", "后端开发", "驱动开发", "硬件工程师"],
            "strong": [
                "运营", "用户增长", "活动策划", "数据分析", "转化率", "留存", "项目管理", "sop",
                "协调", "执行力", "复盘", "roi", "私域运营", "业务闭环"
            ],
            "medium": ["社群", "公众号", "小红书", "短视频", "文案", "表格", "沟通协调", "用户生命周期", "流程优化"],
            "weak": ["后端", "算法", "运维", "嵌入式", "c++"],
            "kill": ["外包", "培训", "劳务派遣", "收费", "押金", "刷单", "直销"]
        },
        "reply_stance": "general",
        "stance_intro": "条理清晰、稳健得体，突出项目落地、细节闭环与跨部门沟通协作优势"
    }
}

DEFAULT_PRESET_ID = "ai_pm"


def list_presets() -> List[Dict[str, Any]]:
    """返回供前端展示的预设包列表（精简展示字段）。"""
    out = []
    for pid, p in PRESETS.items():
        out.append({
            "id": pid,
            "name": p["name"],
            "badge": p["badge"],
            "description": p["description"],
            "reply_stance": p["reply_stance"],
            "keywords": p["keywords"],
        })
    return out


def get_preset(preset_id: Optional[str]) -> Dict[str, Any]:
    """获取指定预设；若不存在或为空，回退到默认 AI/PM 预设。"""
    if preset_id and preset_id in PRESETS:
        return copy.deepcopy(PRESETS[preset_id])
    return copy.deepcopy(PRESETS[DEFAULT_PRESET_ID])


def resolve_score_words(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """解析当前配置的打分词表：以激活的 preset 为准。
    若未配置 preset 字段，沿用历史 config.json 既有词表兼容老环境。
    """
    preset_id = cfg.get("preset")
    if not preset_id:
        return cfg.get("score_words") or copy.deepcopy(PRESETS[DEFAULT_PRESET_ID]["score_words"])

    active_preset = get_preset(preset_id)
    preset_sw = copy.deepcopy(active_preset["score_words"])

    # 仅当配置中显式存在用户级自定义微调 custom_score_words 时叠加
    custom = cfg.get("custom_score_words") or {}
    for k, v in custom.items():
        if v:
            preset_sw[k] = v
    return preset_sw


def resolve_title_kill(cfg: Dict[str, Any]) -> List[str]:
    """获取当前生效的岗位标题一票否决词列表。"""
    sw = resolve_score_words(cfg)
    return sw.get("title_kill", [])


def resolve_keywords(cfg: Dict[str, Any]) -> List[str]:
    """获取当前生效的默认检索关键词列表。"""
    # 优先使用用户偏好中的向往岗位
    prefs = cfg.get("prefs") or {}
    want_jobs = prefs.get("want_jobs") or []
    if want_jobs:
        return [str(j).strip() for j in want_jobs if str(j).strip()]

    # 其次看是否显式指定了 preset
    if "preset" in cfg:
        p = get_preset(cfg["preset"])
        return p.get("keywords") or []

    # 兜底沿用 config.json 中的关键词
    return cfg.get("keywords") or get_preset(DEFAULT_PRESET_ID)["keywords"]
