"""消息中心回复检测（裸CDP只读）。输出 JSON 会话列表，供 agent 决策回复。
判定启发式：最后一条消息以已知我方话术开头（默认招呼/测试跟发）= 待HR回复；
否则 = HR最后发言（大概率需要回复）。启发式不完美，消费方（agent）回复前
应点开会话核实全文，并与台账 action=reply 历史交叉核对防重复。
隐私红线：HR 索要电话/微信等联系方式时 → needs_human=true，**禁止 agent 代发，
必须转人工由用户本人回复**（needs_human 不会进 needs_reply 自动队列）。
用法：venv/Scripts/python scripts/chat_check.py
"""
import json
import os
import re
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

# 隐私红线：HR 索要联系方式 / 消息含电话号码 → 标记 needs_human，禁止 agent 代发
PHONE_REQUEST = re.compile(r"电话|手机号|手机号码|联系方式|加个?微信|加个?联系|留个?电话", re.I)
PHONE_LIKE = re.compile(r"1[3-9]\d{9}|\d{3,4}-?\d{7,8}")


def contact_requested(msg):
    """最后一条消息是否索要/包含联系方式。命中=true 表示必须转人工。"""
    if not msg:
        return False
    return bool(PHONE_REQUEST.search(msg) or PHONE_LIKE.search(msg))


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
    human = contact_requested(preview)
    return {"time": time_s, "who": who, "status": status,
            "last_msg": preview[:120], "needs_reply_guess": bool(preview) and not from_us,
            "needs_human": human}


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
        out = {"count": len(convs),
               "needs_human": [c for c in convs if c["needs_human"]],
               "needs_reply": [c for c in convs if c["needs_reply_guess"] and not c["needs_human"]],
               "all": convs}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        sess.close_tab()
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
