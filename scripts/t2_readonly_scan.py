"""T2 只读扫描：搜索+详情+打分+台账，0次沟通。用法：
    venv/Scripts/python scripts/t2_readonly_scan.py [城市] [页数/关键词]
例：t2_readonly_scan.py 杭州 1
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, flows, guard

cfg = cfgmod.load()
g = guard.Guard(cfg)
city = sys.argv[1] if len(sys.argv) > 1 else "杭州"
max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 1

r = flows.scan_city(cfg, g, city, max_pages=max_pages)
print(json.dumps(r, ensure_ascii=False, indent=2))
print()
print("T2 通过标准：found>0 且无 PAUSED。然后人工翻阅台账 state/ledger.jsonl，检查打分是否合理。")
