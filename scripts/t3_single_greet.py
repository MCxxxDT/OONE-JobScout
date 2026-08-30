"""T3 单次沟通：对当前最高分岗位发 1 条招呼（链路验证，仍建议在测试账号上跑）。
用法：venv/Scripts/python scripts/t3_single_greet.py
前置：T1/T2 已通过；台账里已有 scan 记录。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, flows, guard, ledger

cfg = cfgmod.load()
g = guard.Guard(cfg)

if cfg.get("profile") == "real":
    ans = input("当前 profile=real（将使用含个人信息的文案）。确认继续？(y/N) ")
    if ans.strip().lower() != "y":
        print("已取消。建议先用 profile=test 验证链路。")
        sys.exit(0)

jobs = ledger.pending(cfg)
if not jobs:
    print("台账中没有可投岗位。先跑 t2_readonly_scan.py")
    sys.exit(0)

top = jobs[0]
print("目标：[%s] %s | %s | score=%s" % (top.get("city"), top.get("title"), top.get("company"), top.get("score")))
r = flows.execute_jobs(cfg, g, [top], max_count=1)
print(json.dumps(r, ensure_ascii=False, indent=2))
print()
print("T3 通过标准：executed=1 且 status ok；然后到 BOSS 消息列表确认 HR 收到招呼语。")
