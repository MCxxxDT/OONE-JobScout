"""按公司名（或会话关键词）在消息中心发送一条回复给 HR。
经 greeter.send_message_via_chat（裸CDP），写台账 action=reply。
用法：venv/Scripts/python scripts/chat_reply.py <公司名或会话关键词> "<回复文本>"
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, greeter, ledger, rawcdp


def main():
    if len(sys.argv) < 3:
        print("用法: chat_reply.py <公司名或会话关键词> <回复文本>")
        return 1
    company, text = sys.argv[1], sys.argv[2]
    sess = rawcdp.RawCDP(cfgmod.load()["cdp_endpoint"])
    try:
        sess.open_tab()
        r = greeter.send_message_via_chat(sess, company, text)
        ledger.append({"action": "reply", "status": "ok", "company": company,
                       "text_head": text[:120], "conv": r.get("conv")})
        print(json.dumps({"ok": True, "company": company, "result": r}, ensure_ascii=False))
        return 0
    except Exception as e:
        ledger.append({"action": "reply", "status": "failed", "company": company,
                       "error": str(e)[:200]})
        print(json.dumps({"ok": False, "company": company, "error": str(e)[:300]}, ensure_ascii=False))
        return 1
    finally:
        sess.close_tab()
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
