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

import argparse
parser = argparse.ArgumentParser(description="T3 单次沟通实弹验证")
parser.add_argument("-y", "--yes", action="store_true", help="跳过确认直接执行")
parser.add_argument("--idx", type=int, default=None, help="指定计划中的岗位序号(从1开始)")
cli_args, _ = parser.parse_known_args()

if not cli_args.yes and cfg.get("profile") == "real":
    ans = input("当前 profile=real（将使用含个人信息的文案）。确认继续？(y/N) ")
    if ans.strip().lower() != "y":
        print("已取消。建议先用 profile=test 验证链路。")
        sys.exit(0)

plan_file = cfgmod.state_path("daily_plan.json")
if os.path.exists(plan_file):
    try:
        with open(plan_file, "r", encoding="utf-8") as f:
            jobs = json.load(f)
    except Exception:
        jobs = []
else:
    jobs = ledger.pending(cfg)

if not jobs:
    print("没有可投岗位。请先生成 daily_plan 或跑扫描。")
    sys.exit(0)

greeted_hrefs = set()
for r in ledger.load_all():
    if r.get("action") == "greet" and r.get("status") == "ok":
        h = r.get("href")
        if h:
            greeted_hrefs.add(h)

if cli_args.idx is not None and 1 <= cli_args.idx <= len(jobs):
    top = jobs[cli_args.idx - 1]
else:
    pending_jobs = [j for j in jobs if (j.get("href") or "") not in greeted_hrefs]
    if not pending_jobs:
        print("所有候选岗位均已打过招呼！")
        sys.exit(0)
    top = pending_jobs[0]

print("目标：[%s] %s | %s | score=%s" % (top.get("city"), top.get("title"), top.get("company"), top.get("score")))
r = flows.execute_jobs(cfg, g, [top], max_count=1)
print(json.dumps(r, ensure_ascii=False, indent=2))
print()
print("T3 通过标准：executed=1 且 status ok；然后到 BOSS 消息列表确认 HR 收到招呼语。")
