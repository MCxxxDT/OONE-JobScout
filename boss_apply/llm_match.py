"""LLM 站内智能匹配（2026-09-09 Web 工作台升级，替代关键词加权打分）。

设计：
- 单次 LLM 调用批量评估 ≤20 个岗位（deep 一批，控制成本与耗时）；
- 输入：简历提炼画像（profile_store.refined，缺省硬编码画像）+ 用户偏好四清单
  （留空=模型基于候选人背景自主决断）+ 岗位要素（标题/公司/薪资/tags/JD 截断）；
- 输出：per-job {score(0-37 对齐现打分体系), verdict: high|medium|low|veto, reason}；
- 失败/无 key 返回 None，调用方回退 scorer 关键词打分——增强项哲学：绝不阻塞扫描。
"""
import json
import re

from . import profile_store

_MATCH_PROMPT = """你是资深求职顾问，为候选人评估一批BOSS直聘岗位的适配度。

【评分体系】score 为 0-37 分（对齐既有打分体系）：
- 0 分（veto）：岗位方向与候选人严重不符（销售/地推/客服/外包/纯技术执行等）；
- 8 分以下（low）：沾边但核心要求不匹配；
- 8-19 分（medium）：方向匹配，可作为备选；
- 20 分以上（high）：方向高度契合，值得优先投递。
verdict ∈ {veto, low, medium, high}。

【候选人画像】
%s

【用户求职偏好】
%s

【岗位列表】（按序号对应输出）
%s

请严格输出纯 JSON 数组（无 markdown 包裹），每个元素：
{"i": 序号, "score": 数字, "verdict": "high|medium|low|veto", "reason": "30字内中文理由"}
注意：用户偏好未填写的维度，基于候选人背景自主判断；reason 要具体（命中什么/缺什么）。"""


def _prefs_block(cfg):
    p = cfg.get("prefs") or {}
    parts = []
    for key, label in (("want_jobs", "向往岗位"), ("avoid_jobs", "排斥岗位"),
                       ("want_cities", "向往城市"), ("avoid_cities", "排斥城市")):
        vals = [str(x).strip() for x in (p.get(key) or []) if str(x).strip()]
        parts.append("%s：%s" % (label, "、".join(vals) if vals else "未指定（由你自主决断）"))
    return "\n".join(parts)


def _profile_block(cfg):
    try:
        refined = profile_store.load_profile() or {}
    except Exception:
        refined = {}
    from .ai_reply import CANDIDATE_PROFILE
    prof = dict(CANDIDATE_PROFILE, **refined)
    lines = []
    for k in ("name", "school", "major", "grade_desc", "current_city", "target_region",
              "availability", "salary_requirement", "target_roles",
              "tech_highlights", "business_highlights", "summary", "highlights"):
        v = prof.get(k)
        if v:
            lines.append("- %s：%s" % (k, "；".join(v) if isinstance(v, list) else v))
    return "\n".join(lines)


def _job_block(jobs):
    lines = []
    for i, j in enumerate(jobs):
        detail = (j.get("detail") or j.get("jd_text") or "")[:400].replace("\n", " ")
        lines.append("%d. 标题：%s | 公司：%s | 薪资：%s | 标签：%s\n   JD：%s" % (
            i, j.get("title") or "?", j.get("company") or "?",
            j.get("salary") or "?", j.get("tags") or "-", detail or "无"))
    return "\n".join(lines)


def match_batch(jobs, cfg, batch_size=20):
    """批量 LLM 匹配。返回 {i: {"score","verdict","reason"}} 或 None（失败/无key）。
    jobs 为带 title/company/salary/tags/detail 的 dict 列表。"""
    llm = cfg.get("llm") or {}
    if not (llm.get("api_key") and llm.get("base_url")):
        return None
    if not (cfg.get("llm_match") or {}).get("enabled", True):
        return None
    out = {}
    for start in range(0, len(jobs), batch_size):
        batch = jobs[start:start + batch_size]
        res = _call_once(batch, cfg)
        if res is None:
            return None  # 任一批失败即整体回退（保守：宁可用稳定的关键词分也不半吊混合）
        out.update(res)
    return out or None


def _call_once(batch, cfg):
    llm = cfg.get("llm") or {}
    try:
        import urllib.request
        url = llm["base_url"].rstrip("/") + "/chat/completions"
        prompt = _MATCH_PROMPT % (_profile_block(cfg), _prefs_block(cfg), _job_block(batch))
        payload = {
            "model": llm.get("model") or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "你是求职顾问，严格输出纯JSON数组。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 8192,
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + llm["api_key"]})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (data["choices"][0]["message"].get("content") or "").strip()
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"```\s*$", "", content).strip()
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            return None
        out = {}
        for item in parsed:
            try:
                i = int(item.get("i"))
                verdict = str(item.get("verdict") or "low")
                score = max(0.0, min(37.0, float(item.get("score") or 0)))
                if verdict == "veto":
                    score = 0.0
                out[i] = {"score": round(score, 1), "verdict": verdict,
                          "reason": (item.get("reason") or "")[:80]}
            except Exception:
                continue
        return out if out else None
    except Exception:
        return None
