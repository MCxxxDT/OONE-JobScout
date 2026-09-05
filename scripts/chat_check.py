"""消息中心回复检测（只读，CLI 包装）。核心逻辑 flows.chat_inbox。
输出 needs_reply（待回复）/needs_human（索要联系方式，禁止代发转人工）。
退出码：0=正常，2=风控/加载失败。
用法：venv/Scripts/python scripts/chat_check.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, flows

out = flows.chat_inbox(cfgmod.load())
print(json.dumps(out, ensure_ascii=False, indent=1))
sys.exit(2 if out.get("error") else 0)
