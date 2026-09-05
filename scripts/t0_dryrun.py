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

print("== 9. AI 回复引擎与守护时间闸门（ai_reply）==")
import datetime as _dt
from boss_apply import ai_reply as _air

# 时间闸门
t_active = _dt.datetime(2026, 9, 6, 14, 0, 0)
t_night = _dt.datetime(2026, 9, 6, 0, 30, 0)
t_boundary_start = _dt.datetime(2026, 9, 6, 9, 30, 0)
t_boundary_end = _dt.datetime(2026, 9, 6, 20, 30, 0)
t_outside_early = _dt.datetime(2026, 9, 6, 9, 29, 0)
t_outside_late = _dt.datetime(2026, 9, 6, 20, 31, 0)

check("工作时间内判定放行", _air.is_active_hour(t_active, "09:30-20:30"))
check("工作时间起始边界放行", _air.is_active_hour(t_boundary_start, "09:30-20:30"))
check("工作时间结束边界放行", _air.is_active_hour(t_boundary_end, "09:30-20:30"))
check("早于工作时间拦截", not _air.is_active_hour(t_outside_early, "09:30-20:30"))
check("晚于工作时间拦截", not _air.is_active_hour(t_outside_late, "09:30-20:30"))
check("凌晨时段拦截", not _air.is_active_hour(t_night, "09:30-20:30"))
check("夜间计算距离下一工作时段为正数", _air.seconds_until_next_active(t_night, "09:30-20:30") > 3600)

# 消息时效
check("当天HH:MM时间放行", _air.is_recent_message("23:19"))
check("刚刚/分钟前放行", _air.is_recent_message("10分钟前"))
check("昨天在24h范围内放行", _air.is_recent_message("昨天 15:20", max_age_hours=24))
check("历史日期被拦截", not _air.is_recent_message("09月01日", max_age_hours=24))
check("8月旧消息被拦截", not _air.is_recent_message("08月31日", max_age_hours=24))

# AI 意图判定与纯 Agent 驱动回复校验
print("  --- 纯 Agent 架构校验：无可用 Agent 时坚决不使用模板降级，全权转人工 ---")
ai_engine_raw = _air.AIReplyEngine(cfg)
ai_engine_raw.openrouter_key = ""
ai_engine_raw.openai_key = ""

check("彻底废除确定性模板降级函数", not hasattr(ai_engine_raw, "_generate_template_reply"))

r_no_agent = ai_engine_raw.decide_and_generate({"who": "测试HR", "last_msg": "方便加个微信详聊吗？"})
check("无Agent可用时转人工处理", r_no_agent["action"] == "needs_human", str(r_no_agent))
check("无Agent可用时严禁返回死板模板文案", r_no_agent["reply_text"] == "")
check("无Agent可用时生成人工接管提醒notice", bool(r_no_agent.get("notice")))
check("无Agent可用时理由注明转人工", "转人工" in r_no_agent["reason"])
check("消息来源标识为人工转交", r_no_agent["source"] == "human_handoff")

r_empty = ai_engine_raw.decide_and_generate({"who": "测试HR", "last_msg": "  "})
check("空消息直接跳过", r_empty["action"] == "skip")

print("  --- Agent 决策 Prompt 构建校验 ---")
prompt_test = ai_engine_raw.build_agent_prompt({
    "who": "杭州某独角兽HR",
    "last_msg": "你现在在杭州吗？期望薪资多少？"
})
check("Prompt注入张烨韬画像", "张烨韬" in prompt_test and "2027" in prompt_test)
check("Prompt包含常驻福州", "福州" in prompt_test)
check("Prompt包含核心意向江浙沪", "江浙沪" in prompt_test)
check("Prompt包含薪资租房生活底线", "生活" in prompt_test and "租房" in prompt_test)
check("Prompt包含三不原则", "三不原则" in prompt_test)
check("Prompt包含反索JD心法", "JD" in prompt_test)
check("Prompt包含初试线上提议", "线上" in prompt_test)
check("Prompt包含严禁泄露手机号", "手机号" in prompt_test)

print("  --- Agent 驱动拟人动态回复与安全红线门禁校验 ---")
def mock_agent(conv, prompt):
    msg = conv.get("last_msg", "")
    if "微信" in msg:
        return {
            "action": "reply",
            "reply_text": "您好！在平台沟通比较方便及时，方便先发一份岗位详细JD供我拜读了解一下吗？谢谢！",
            "reason": "Agent引导平台沟通并索要JD",
            "notice": "HR索要微信，Agent已太极留存平台并索要JD"
        }
    if "杭州" in msg or "线下" in msg:
        return {
            "action": "reply",
            "reply_text": "您好！我目前常驻福州，因为非常看好江浙沪及贵司该方向，随时可奔赴到岗！初试方便先线上进行吗？谢谢！",
            "reason": "Agent真诚告知在福州并提议线上初试",
            "notice": "HR询问地点，Agent已告知在福州并提议线上"
        }
    if "期望薪资" in msg:
        return {
            "action": "reply",
            "reply_text": "您好！考虑跨城赴江浙沪全职实习，主要希望能覆盖当地基础租房与生活开销；核心最看重业务匹配度，方便发一份JD吗？",
            "reason": "Agent说明生活底线并索JD"
        }
    return {
        "action": "reply",
        "reply_text": "您好！感谢您的关注，我对该方向很有兴趣，方便发一份岗位详细JD深入了解一下吗？谢谢！",
        "reason": "Agent通用动态回复"
    }

ai_engine_agent = _air.AIReplyEngine(cfg, agent_generator=mock_agent)

# 场景 A: 索要微信
r_agent_wx = ai_engine_agent.decide_and_generate({"who": "测试HR", "last_msg": "方便加个微信详聊吗？"})
check("Agent驱动处理微信索要判为reply", r_agent_wx["action"] == "reply")
check("Agent回复文案通过隐私强校验", not _gr.privacy_blocked(r_agent_wx["reply_text"]))
check("Agent回复文案反索JD", "JD" in r_agent_wx["reply_text"])

# 场景 B: 询问地点与能否线下
r_agent_loc = ai_engine_agent.decide_and_generate({"who": "杭州HR", "last_msg": "请问你现在在杭州吗，可以接受线下面试吗？"})
check("Agent驱动地点询问判为reply", r_agent_loc["action"] == "reply")
check("Agent回复说明常驻福州", "福州" in r_agent_loc["reply_text"])
check("Agent回复提议线上初试", "线上" in r_agent_loc["reply_text"])
check("Agent回复通过隐私强校验", not _gr.privacy_blocked(r_agent_loc["reply_text"]))

# 场景 C: 询问薪资与生活底线
r_agent_sal = ai_engine_agent.decide_and_generate({"who": "上海HR", "last_msg": "请问你的期望薪资是多少？"})
check("Agent驱动薪资询问判为reply", r_agent_sal["action"] == "reply")
check("Agent回复体现租房与生活底线", "生活" in r_agent_sal["reply_text"] and "租房" in r_agent_sal["reply_text"])
check("Agent回复通过隐私强校验", not _gr.privacy_blocked(r_agent_sal["reply_text"]))

# 场景 D: 安全红线门禁（若 Agent 输出包含真实手机号，强制转人工）
def leaking_agent(conv, prompt):
    return {"action": "reply", "reply_text": "请联系我电话13812345678详聊"}

r_agent_leak = ai_engine_agent.decide_and_generate({"who": "测试HR", "last_msg": "电话多少"}, agent_generator=leaking_agent)
check("Agent泄露手机号被安全门禁拦截转人工", r_agent_leak["action"] == "needs_human")
check("门禁转人工清空发信文案", r_agent_leak["reply_text"] == "")
check("来源标识为privacy_guard", r_agent_leak["source"] == "privacy_guard")

print("== 10. 岗位详情提取与 FastMCP 工具 (get_active_job_detail) ==")
from boss_apply import server as _srv

check("ACTIVE_JOB_JS 语法存在", "chat-position-content" in _gr.ACTIVE_JOB_JS)
check("ACTIVE_JOB_JS 覆盖 encryptJobId 字段", "encryptJobId" in _gr.ACTIVE_JOB_JS)
check("ACTIVE_JOB_JS 覆盖 salaryDesc 字段", "salaryDesc" in _gr.ACTIVE_JOB_JS)
check("ACTIVE_JOB_JS 包含 DOM fallback", "fallback: true" in _gr.ACTIVE_JOB_JS)

class MockSess:
    def __init__(self, val):
        self.val = val
    def eval(self, js):
        return self.val

s_ok = MockSess(json.dumps({"encryptJobId": "test_eid", "positionName": "AI产品经理", "companyName": "淘宝闪购"}))
parsed_job = _gr.get_active_conversation_job(s_ok)
check("get_active_conversation_job 正确提取有效岗位", parsed_job and parsed_job.get("encryptJobId") == "test_eid")

s_none = MockSess(json.dumps({"r": "no_active_job"}))
check("get_active_conversation_job 无岗位时返回 None", _gr.get_active_conversation_job(s_none) is None)

# 验证 FastMCP 注册了 get_active_job_detail
import asyncio
mcp_tool_names = [t.name for t in asyncio.run(_srv.mcp.list_tools())]
check("FastMCP 注册了 get_active_job_detail", "get_active_job_detail" in mcp_tool_names, str(mcp_tool_names))

shutil.rmtree(DRY, ignore_errors=True)

print()
if fails:
    print("结果: %d 项失败 -> %s" % (len(fails), fails))
    sys.exit(1)
print("结果: 全部通过。管线可用，等待 T1(登录态) 接入。")

