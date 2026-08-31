"""消息中心回复检测（裸CDP只读）。输出 JSON 会话列表，供 agent 决策回复。
判定启发式：最后一条消息以已知我方话术开头（默认招呼/测试跟发）= 待HR回复；
否则 = HR最后发言（大概率需要回复）。启发式不完美，消费方（agent）回复前
应点开会话核实全文，并与台账 action=reply 历史交叉核对防重复。
用法：venv/Scripts/python scripts/chat_check.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import rawcdp

LIST_JS = """
(() => {
  const isConv = (li) => { const t = li.innerText || ''; return t.length > 12 && /\\d{1,2}:\\d{2}/.test(t); };
  return JSON.stringify(Array.from(document.querySelectorAll('li')).filter(isConv)
    .map(li => (li.innerText || '').slice(0, 200)));
})()
"""

OUR_OPENERS = ("您好，我是27年毕业生", "您好，看到贵司", "您好，看到这个岗位")


def parse_conv(raw):
    """'02:42|赵先生新美虹星总经理|[送达]|您好，…' → 结构化。"""
    parts = [p for p in raw.replace("\n", "|").split("|") if p != ""]
    if len(parts) < 2:
        return None
    time_s = parts[0] if ":" in parts[0] else ""
    who = parts[1] if len(parts) > 1 else ""
    status = ""
    preview = ""
    rest = parts[2:]
    if rest and rest[0].startswith("["):
        status = rest[0]
        rest = rest[1:]
    preview = "|".join(rest)
    from_us = any(preview.startswith(op) for op in OUR_OPENERS)
    return {"time": time_s, "who": who, "status": status,
            "last_msg": preview[:120], "needs_reply_guess": bool(preview) and not from_us}


def main():
    sess = rawcdp.RawCDP("http://127.0.0.1:9335")
    try:
        sess.open_tab(rawcdp.BASE + "/web/geek/chat")
        for _ in range(15):
            import time
            time.sleep(1)
            st = sess.state()
            if st and not st.get("blank") and st.get("bodyLen", 0) > 100:
                break
        if st and (st.get("captcha") or st.get("security")):
            print(json.dumps({"error": "verify/security page", "state": st}, ensure_ascii=False))
            return 2
        v = sess.eval(LIST_JS)
        raws = json.loads(v) if v else []
        convs = [c for c in (parse_conv(r) for r in raws) if c]
        out = {"count": len(convs), "needs_reply": [c for c in convs if c["needs_reply_guess"]],
               "all": convs}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        sess.close_tab()
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
