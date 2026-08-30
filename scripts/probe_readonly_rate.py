"""只读频率探针：阶梯频率搜索，定位风控触发档位（仅测试账号）。
用法：venv/Scripts/python scripts/probe_readonly_rate.py
注意：本脚本不发任何沟通，但会高频访问搜索页。触发风控会立即停止并输出档位。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, flows, guard

cfg = cfgmod.load()
g = guard.Guard(cfg)

steps = [[3.0, 6], [2.0, 6], [1.0, 6], [0.5, 6]]
r = flows.probe_readonly(cfg, steps)
print(json.dumps(r, ensure_ascii=False, indent=2))
print()
print("结论解读：verdict 说明风控在哪一档触发。若全部通过，说明只读维度还有余量，")
print("但沟通维度（打招呼）永远不要做频率探针——那种触发是不可逆的账号伤害。")
