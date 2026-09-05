"""T0 离线干跑：不连浏览器、不碰BOSS，用假岗位数据验证全链路——
打分 → 台账 → 去重 → 计划生成 → 护栏（配额/间隔/熔断）。
状态写入 state_dryrun/ 并在结束时自动清除，不污染真实台账。
用法：venv/Scripts/python scripts/t0_dryrun.py
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss_apply import config as cfgmod, flows, guard, ledger, scorer

DRY = os.path.join(cfgmod.ROOT, "state_dryrun")
shutil.rmtree(DRY, ignore_errors=True)
os.makedirs(DRY, exist_ok=True)
cfgmod.STATE_DIR = DRY

cfg = cfgmod.load()

FAKE = [
    {"href": "/job_detail/f1", "city": "杭州", "title": "AI产品经理实习生",
     "company": "杭州深度求索科技有限公司", "salary": "300-400元/天", "tags": "Agent Coze",
     "boss_active": 0,
     "detail": "负责Agent产品设计与迭代，熟悉Coze/Dify/工作流编排，有Vibe Coding经验者优先。"},
    {"href": "/job_detail/f2", "city": "杭州", "title": "大模型产品运营专员",
     "company": "某外包服务有限公司", "salary": "5-8K", "tags": "", "boss_active": 1,
     "detail": "负责大模型产品的用户增长与运营。"},
    {"href": "/job_detail/f3", "city": "上海", "title": "AI产品经理",
     "company": "上海阶跃星辰智能科技有限公司", "salary": "15-25K", "tags": "多模态",
     "boss_active": 30,
     "detail": "负责多模态大模型产品定义与评测。"},
    {"href": "/job_detail/f4", "city": "上海", "title": "算法工程师（模型训练方向）",
     "company": "某科技有限公司", "salary": "20-40K", "tags": "", "boss_active": 2,
     "detail": "负责大模型预训练与对齐算法研究。"},
    {"href": "/job_detail/f5", "city": "上海", "title": "Agent产品经理",
     "company": "语核科技（上海）有限公司", "salary": "8-12K", "tags": "",
     "boss_active": 3,
     "detail": "负责Agent应用产品规划，熟悉MCP、FastMCP、workflow编排，商业化场景落地。"},
    {"href": "/job_detail/f6", "city": "杭州", "title": "AI产品经理（限28届）",
     "company": "某互联网科技有限公司", "salary": "20-35K", "tags": "",
     "boss_active": 1,
     "detail": "负责Agent产品的设计与落地，熟悉MCP与workflow编排。"},
    {"href": "/job_detail/f7", "city": "上海", "title": "Agent产品经理",
     "company": "某智能科技有限公司", "salary": "10-15K", "tags": "",
     "boss_active": 2,
     "detail": "负责Agent应用产品规划，欢迎26-28届在校生投递，熟悉workflow编排。"},
]

fails = []


def check(name, cond, info=""):
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, info))
    if not cond:
        fails.append(name)


print("== 1. 打分器 ==")
scores = {}
for j in FAKE:
    s, why = scorer.score(j, j["detail"], cfg)
    scores[j["href"]] = s
    print("  %-18s %6.1f  %s" % (j["title"], s, why[:70]))

check("DeepSeek岗高分通过", scores["/job_detail/f1"] >= 12, "=%.1f" % scores["/job_detail/f1"])
check("外包公司一票否决(公司名)", scores["/job_detail/f2"] == 0)
check("BOSS不活跃一票否决", scores["/job_detail/f3"] == 0)
check("算法训练方向被压分", scores["/job_detail/f4"] < cfg["min_score"], "=%.1f" % scores["/job_detail/f4"])
check("语核Agent岗高分通过", scores["/job_detail/f5"] >= 12, "=%.1f" % scores["/job_detail/f5"])
check("限28届岗一票否决", scores["/job_detail/f6"] == 0, "=%.1f" % scores["/job_detail/f6"])
check("26-28届范围岗放行(含27)", scores["/job_detail/f7"] >= cfg["min_score"], "=%.1f" % scores["/job_detail/f7"])
s_slash, _ = scorer.score({"title": "AI产品经理（26/28届）", "company": "某科技", "salary": "", "tags": "", "boss_active": 0}, "", cfg)
check("离散届别列表26/28届被拒", s_slash == 0, "=%.1f" % s_slash)
s_conf, _ = scorer.score({"title": "AI产品经理", "company": "某科技", "salary": "", "tags": "", "boss_active": 0},
                         "负责第28届创新大赛的产品组织，Agent方向。", cfg)
check("'第28届大赛'不误杀", s_conf > 0, "=%.1f" % s_conf)
s_sales, _ = scorer.score({"title": "销售（AI、大模型方向）", "company": "阿里云计算有限公司",
                           "salary": "30-60K·15薪", "tags": "", "boss_active": 0},
                          "负责AI与大模型产品的agent/智能体/prompt方案落地。", cfg)
check("销售岗JD堆分仍被title_kill拦", s_sales == 0, "=%.1f" % s_sales)
s_dev, _ = scorer.score({"title": "大模型后端开发", "company": "某技术公司",
                         "salary": "12-16K", "tags": "", "boss_active": 0},
                        "负责大模型Agent后端开发，workflow编排。", cfg)
check("后端开发岗被title_kill拦", s_dev == 0, "=%.1f" % s_dev)

print("== 2. 台账与去重 ==")
for j in FAKE:
    ledger.append({"action": "scan", "city": j["city"], "score": scores[j["href"]],
                   "title": j["title"], "company": j["company"], "href": j["href"]})
ledger.append({"action": "scan", "city": "杭州", "score": 5.0, "title": "AI产品经理实习生",
               "company": "旧记录", "href": "/job_detail/f1"})  # 同岗位旧低分记录
pend = ledger.pending(cfg)
check("同岗位保留最高分", all(p["score"] > 5 for p in pend if p["href"] == "/job_detail/f1"))
check("计划只含达标岗", [p["href"] for p in pend] == ["/job_detail/f1", "/job_detail/f5", "/job_detail/f7"],
      str([p["href"] for p in pend]))

print("== 3. 计划生成 ==")
plan = flows.build_plan(cfg)
check("plan.json 生成 3 条", plan["count"] == 3)

print("== 4. 护栏 ==")
g = guard.Guard(cfg)
# 防重复跟发：已有 greet ok 的岗位被 filter_greeted 剔除
ledger.append({"action": "greet", "status": "ok", "href": "/job_detail/f1", "title": "dup", "ts": "x"})
fj = flows.filter_greeted([{"href": "/job_detail/f1", "title": "dup"}, {"href": "/job_detail/f5", "title": "keep"}])
check("已沟通岗位不再重复执行", [j["href"] for j in fj] == ["/job_detail/f5"], str([j["href"] for j in fj]))
ok, _ = g.check_greet("杭州")
check("初始可沟通", ok)
for _ in range(2):
    g.record_greet("杭州")
check("城市配额递减", g.city_left("杭州") == 23, "left=%d" % g.city_left("杭州"))
for _ in range(23):
    g.record_greet("杭州")
ok, msg = g.check_greet("杭州")
check("配额耗尽拦截", not ok, msg)
ok, _ = g.check_greet("上海")
check("其他城市不受影响", ok)
g.pause("unit-test")
ok, msg = g.check_greet("上海")
check("熔断拦截", not ok, msg)
g.resume()
ok, _ = g.check_greet("上海")
check("解除恢复", ok)

print("== 5. 风控判定纯函数（rawcdp，2026-08-31 采纳 eatmoreduck 词表）==")
from boss_apply import rawcdp as _raw
c_ok, _ = _raw.classify_joblist_response({"code": 0, "zpData": {"jobList": [{"salaryDesc": "20-40K"}]}})
check("code0+明文薪资=ok", c_ok == "ok")
c_r1, _ = _raw.classify_joblist_response({"code": 37, "message": "环境存在异常"})
check("code37 风控拒停", c_r1 == "restricted")
c_r2, _ = _raw.classify_joblist_response({"code": 999, "message": "操作太频繁"})
check("未知码按关键字兜底风控", c_r2 == "restricted")
c_em, _ = _raw.classify_joblist_response({"code": 0, "zpData": {"jobList": []}})
check("空 jobList 判 empty", c_em == "empty")

print("== 6. 隐私校验（精细化词表，审计补丁#2）==")
from boss_apply import greeter as _gr
check("内置文案全部过隐私校验",
      all(not _gr.privacy_blocked(t) for ts in cfg["greeting"].values() for t in ts))
check("11位手机号被拦截", _gr.privacy_blocked("打我电话13812345678"))
check("加个微信被拦截", _gr.privacy_blocked("加个微信聊"))
check("留个微信被拦截", _gr.privacy_blocked("方便留个微信吗"))
check("微信号被拦截", _gr.privacy_blocked("你微信号多少"))
check("微信联系被拦截", _gr.privacy_blocked("咱们微信联系"))
check("企业微信业务词不误伤", not _gr.privacy_blocked("熟悉企业微信协同办公"))
check("微信小程序业务词不误伤", not _gr.privacy_blocked("负责微信小程序全栈开发"))
check("微信生态业务词不误伤", not _gr.privacy_blocked("做过微信生态商业化"))

print("== 7. 自家话术识别（self_openers，审计补丁#1）==")
ops = _gr.self_openers(cfg)
g0 = cfg["greeting"]["test"][0]
check("test招呼语前缀可自识别", any(g0.startswith(o) for o in ops))
r0 = cfg["greeting"]["real"][0]
ops_real = _gr.self_openers(dict(cfg, profile="real"))
check("real招呼语前缀可自识别(切real不误判)", any(r0.startswith(o) for o in ops_real))
ledger.append({"action": "reply", "status": "ok", "company": "dryrun",
               "text_head": "您好！感谢您的关注与招呼，方便发一下JD"})
check("台账reply文本头进openers", "您好！感谢您的关注与招呼，方" in _gr.self_openers(cfg))
check("BOSS原生默认招呼保底", "您好，我是27年毕业生" in _gr.self_openers(cfg))

print("== 8. 会话解析（flows.parse_conv）==")
c1 = flows.parse_conv("02:42|赵先生新美虹星总经理|[送达]|" + g0, ops)
check("自家招呼结尾→不需回复", bool(c1) and not c1["needs_reply_guess"] and not c1["needs_human"])
c2 = flows.parse_conv("14:03|秦女士淘宝闪购校招HR|你好，方便聊聊吗", ops)
check("HR最后发言→待回复", bool(c2) and c2["needs_reply_guess"] and not c2["needs_human"])
c3 = flows.parse_conv("14:03|王先生某公司HR|方便留个微信吗", ops)
check("HR索微信→转人工", bool(c3) and c3["needs_human"])
c4 = flows.parse_conv("16:53|华先生Talking猎头顾问|[送达]|您好，我是27年毕业生，对岗位很感兴趣", ops)
check("原生默认招呼结尾→不需回复", bool(c4) and not c4["needs_reply_guess"])
c5 = flows.parse_conv("08月31日|秦女士淘宝闪购校招HR|[送达]|" + g0, ops)
check("历史日期会话解析正确且自家发言不需回复", bool(c5) and c5["time"] == "08月31日" and not c5["needs_reply_guess"])

shutil.rmtree(DRY, ignore_errors=True)

print()
if fails:
    print("结果: %d 项失败 -> %s" % (len(fails), fails))
    sys.exit(1)
print("结果: 全部通过。管线可用，等待 T1(登录态) 接入。")
