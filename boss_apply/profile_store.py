"""简历存储与画像提炼（2026-09-09 Web 工作台升级）。

链路：Web 端上传/粘贴简历 → extract_text 解析文本 → save_resume 落
profile.local.json（gitignore）→ refine_profile 调一次 LLM 提炼结构化画像 →
AIReplyEngine / llm_match 优先使用提炼画像（降级硬编码 CANDIDATE_PROFILE）。
"""
import datetime
import json
import os
import time

from . import config as cfgmod

PROFILE_PATH = os.path.join(cfgmod.ROOT, "profile.local.json")

# 提炼 prompt：输出字段对齐 ai_reply.CANDIDATE_PROFILE（键名一致可直接覆盖）
_REFINE_PROMPT = """你是简历解析助手。把下面的简历文本提炼为结构化候选人画像 JSON。

要求：
1. 严格输出纯 JSON（无 markdown 代码块包裹），键为：
   name, school, major, grad_year(数字), grade_desc, current_city,
   target_region, availability, salary_requirement, target_roles,
   tech_highlights, business_highlights, summary(80字内一句话画像), highlights(3-5条核心亮点数组)
2. 简历中缺失的字段给空字符串/空数组，绝不编造；
3. grad_year 无法判断时给 0；
4. tech/business_highlights 提炼最有竞争力的 2-3 条短语。

简历文本：
"""


def extract_text(path_or_text):
    """简历 → 文本。参数是文件路径（.pdf/.docx/.txt/.md）则解析，是纯文本则直通。
    返回 (text, error)：error 非 None 表示解析失败。"""
    s = (path_or_text or "").strip()
    if not s:
        return "", "空内容"
    ext = os.path.splitext(s)[1].lower()
    known_ext = ext in (".pdf", ".docx", ".txt", ".md")
    # 非已知扩展名且文件不存在 → 视为粘贴的纯文本直通
    if not known_ext and not os.path.exists(s):
        return s, None
    if not os.path.exists(s):
        return "", "文件不存在: %s" % s[:60]
    try:
        if ext == ".pdf":
            import pymupdf
            doc = pymupdf.open(s)
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            return text.strip(), (None if text.strip() else "PDF 无可提取文本（可能是扫描件）")
        if ext in (".docx",):
            import docx
            d = docx.Document(s)
            text = "\n".join(p.text for p in d.paragraphs)
            for t in d.tables:  # 表格常承载经历信息
                for row in t.rows:
                    text += "\n" + " | ".join(c.text.strip() for c in row.cells)
            return text.strip(), (None if text.strip() else "Word 文档无文本")
        if ext in (".txt", ".md"):
            with open(s, "r", encoding="utf-8") as f:
                return f.read().strip(), None
        return "", "不支持的文件类型（仅 .pdf/.docx/.txt/.md）"
    except Exception as e:
        return "", "解析失败: %s" % str(e)[:100]


def save_resume(text, source):
    """保存简历原文（不动 refined——由 refine 成功后单独写入）。"""
    data = _load()
    data["resume_text"] = (text or "").strip()
    data["source"] = source or "paste"
    data["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save(data)
    return data


def _load():
    if not os.path.exists(PROFILE_PATH):
        return {}
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_resume():
    return _load().get("resume_text") or ""


def clear_resume():
    """彻底清空已存简历原文与提炼画像。"""
    data = {
        "resume_text": "",
        "source": "",
        "updated_at": "",
        "refined": None,
    }
    _save(data)
    return data


def fetch_boss_online_resume(cfg=None):
    """通过 CDP 从当前 BOSS 直聘会话中抓取在线完整简历文本。
    返回 (resume_text, error)。
    抓取成功后自动写入 save_resume(text, source='boss_online') 并尝试 refine_profile。
    """
    cfg = cfg or cfgmod.load()
    cdp_http = cfg.get("cdp_endpoint", "http://127.0.0.1:9335")
    try:
        from urllib.request import urlopen
        urlopen(cdp_http + "/json/version", timeout=3)
    except Exception as e:
        return "", "无法连接 Chrome CDP 实例: %s" % str(e)[:100]

    from . import rawcdp
    sess = rawcdp.RawCDP(cdp_http)
    resume_text = ""
    try:
        sess.open_tab("https://www.zhipin.com/web/geek/resume")
        time.sleep(3.5)
        js = """
        (() => {
            const content = document.querySelector('.resume-content') ||
                            document.querySelector('.resume-box') ||
                            document.querySelector('.main-content') ||
                            document.querySelector('.resume-detail') ||
                            document.body;
            if (!content) return '';
            const clone = content.cloneNode(true);
            const removes = clone.querySelectorAll('.nav, .header, .footer, .btn, button, .dialog, script, style');
            removes.forEach(el => el.remove());
            return clone.innerText || '';
        })()
        """
        raw_res = sess.eval(js) or ""
        lines = [l.strip() for l in raw_res.splitlines() if l.strip()]
        resume_text = "\n".join(lines)
    except Exception as e:
        return "", "CDP 抓取在线简历失败: %s" % str(e)[:150]
    finally:
        sess.close_tab()
        sess.close()

    if not resume_text or len(resume_text) < 30:
        return "", "未能从 BOSS 在线简历页提取到有效文本（可能未登录或页面结构异常）"

    save_resume(resume_text, source="boss_online")
    try:
        refine_profile(resume_text, cfg)
    except Exception:
        pass
    return resume_text, None


def load_profile():
    """返回提炼画像 dict（无则 None）。字段键与 CANDIDATE_PROFILE 对齐可直接覆盖。"""
    refined = _load().get("refined")
    if isinstance(refined, dict) and refined.get("name"):
        out = {}
        for k, v in refined.items():
            if v not in ("", None, [], 0):
                out[k] = v
        return out or None
    return None


def profile_meta():
    """Web 端展示用：简历与画像的元信息（不含全文）。"""
    data = _load()
    return {
        "has_resume": bool(data.get("resume_text")),
        "resume_chars": len(data.get("resume_text") or ""),
        "source": data.get("source") or "",
        "updated_at": data.get("updated_at") or "",
        "has_refined": bool(load_profile()),
        "refined_name": (data.get("refined") or {}).get("name") or "",
        "refined_summary": (data.get("refined") or {}).get("summary") or "",
    }


def refine_profile(text, cfg):
    """调 LLM 把简历文本提炼为结构化画像。成功写入 refined 并返回 dict；失败返回 (None, error)。"""
    llm = cfg.get("llm") or {}
    if not (llm.get("api_key") and llm.get("base_url")):
        return None, "未配置 LLM（先在设置页保存 API Key）"
    try:
        import urllib.request
        url = llm["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": llm.get("model") or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "你是简历解析助手，严格输出纯JSON。"},
                {"role": "user", "content": _REFINE_PROMPT + (text or "")[:8000]},
            ],
            "temperature": 0.3,
            "max_tokens": 8192,
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + llm["api_key"]})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (data["choices"][0]["message"].get("content") or "").strip()
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        refined = json.loads(content)
        if not isinstance(refined, dict) or not refined.get("name"):
            return None, "LLM 返回画像缺少 name 字段"
        refined["refined_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        d = _load()
        d["refined"] = refined
        _save(d)
        return refined, None
    except Exception as e:
        return None, "提炼失败: %s" % str(e)[:150]
