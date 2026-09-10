"""经验记忆库与自我学习引擎 (Experience Memory & In-Context Self-Learning).
持久化记录用户评分（1-10分）、人工优化示范文本与历史高情商会话。
在生成 Prompt 时做语义与关键词相似度检索，实现调用外部 LLM 架构下的持续经验回流与“越用越聪明”。
"""
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from . import config as cfgmod

MEMORY_FILE = "experience_memory.jsonl"


def _state_path() -> str:
    return cfgmod.state_path(MEMORY_FILE)


def record_feedback(
    company: str,
    job: str,
    hr_msg: str,
    ai_reply: str,
    score: int = 8,
    optimized_text: str = "",
    source: str = "web_console",
    extra: Optional[dict] = None
) -> dict:
    """持久化保存一条点评或人工优化示范。
    - score: 1-10 分，默认 8 分
    - optimized_text: 真人优化示范文本（如提供，则在后续 retrieval 中作为最高权重真实样本）
    """
    p = _state_path()
    try:
        score_val = max(1, min(10, int(score)))
    except (ValueError, TypeError):
        score_val = 8

    raw_key = f"{company}_{job}_{hr_msg}_{time.strftime('%Y%m%d')}"
    item_id = "exp_" + hashlib.md5(raw_key.encode("utf-8")).hexdigest()[:12]

    row = {
        "id": item_id,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "company": str(company or "").strip(),
        "job": str(job or "").strip(),
        "hr_msg": str(hr_msg or "").strip(),
        "ai_reply": str(ai_reply or "").strip(),
        "score": score_val,
        "optimized_text": str(optimized_text or "").strip(),
        "source": source,
    }
    if extra and isinstance(extra, dict):
        row["extra"] = extra

    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load_all() -> List[Dict[str, Any]]:
    """加载全部经验记录。"""
    p = _state_path()
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _compute_overlap_score(query: str, target: str) -> float:
    """计算简易字符与分词重合度（针对中文短语）。"""
    if not query or not target:
        return 0.0
    q_clean = set(re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]{2,}", query))
    t_clean = set(re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]{2,}", target))
    if not q_clean or not t_clean:
        q_chars = set(c for c in query if "\u4e00" <= c <= "\u9fa5")
        t_chars = set(c for c in target if "\u4e00" <= c <= "\u9fa5")
        if not q_chars or not t_chars:
            return 0.0
        return len(q_chars & t_chars) / max(len(q_chars), 1)
    return len(q_clean & t_clean) / max(len(q_clean), 1)


def find_similar_demonstrations(
    hr_msg: str,
    job_title: str = "",
    min_score: int = 7,
    top_k: int = 2
) -> List[Dict[str, Any]]:
    """基于当前 HR 输入及岗位，在经验库中检索相似度最高的高分/真人优化示范案例。"""
    rows = load_all()
    if not rows:
        return []

    candidates = []
    for r in rows:
        opt = (r.get("optimized_text") or "").strip()
        sc = r.get("score", 0)
        if not opt and sc < min_score:
            continue

        r_msg = r.get("hr_msg", "")
        r_job = r.get("job", "")

        msg_sim = _compute_overlap_score(hr_msg, r_msg)
        job_sim = _compute_overlap_score(job_title, r_job)

        total_sim = msg_sim * 0.75 + job_sim * 0.25
        if opt:
            total_sim += 0.25

        if total_sim > 0.15:
            gold_reply = opt if opt else r.get("ai_reply", "")
            candidates.append({
                "sim": total_sim,
                "hr_msg": r_msg,
                "gold_reply": gold_reply,
                "is_human_optimized": bool(opt),
                "score": sc,
                "company": r.get("company", ""),
                "job": r_job,
            })

    candidates.sort(key=lambda x: -x["sim"])
    return candidates[:top_k]


def format_fewshot_prompt(hr_msg: str, job_title: str = "") -> str:
    """将检索到的历史高分示范与人工优化案例格式化为 Prompt 注入段落。"""
    demos = find_similar_demonstrations(hr_msg, job_title=job_title, top_k=2)
    if not demos:
        return ""

    lines = [
        "【过往沟通真实高分示范与人工优化经验（极为宝贵，请参考其情商分寸与说话节奏，结合本次场景自主变奏）】:"
    ]
    for idx, d in enumerate(demos, 1):
        tag = "（★候选人本人优化改写示范）" if d["is_human_optimized"] else f"（用户实测打分: {d['score']}分）"
        lines.append(f"{idx}. HR曾发问：“{d['hr_msg']}”")
        lines.append(f"   高分回应示范{tag}：“{d['gold_reply']}”")
    lines.append("注意：以上先例旨在示范真实人类从容、真实、不卑不亢的情感温度，严禁机械死记硬背，结合当前上下文自主组织语言。\n")
    return "\n".join(lines) + "\n"
