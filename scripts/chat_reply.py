"""按公司名回复 HR（CLI 包装）。核心逻辑 flows.chat_reply（隐私红线同源）。
退出码：0=ok，1=failed，2=blocked_privacy。
用法：venv/Scripts/python scripts/chat_reply.py <公司名或会话关键词> "<回复文本>"
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, flows

if len(sys.argv) < 3:
    print("用法: chat_reply.py <公司名或会话关键词> <回复文本>")
    sys.exit(1)
r = flows.chat_reply(cfgmod.load(), sys.argv[1], sys.argv[2])
print(json.dumps(r, ensure_ascii=False))
sys.exit(0 if r.get("ok") else (2 if r.get("blocked") == "privacy" else 1))
