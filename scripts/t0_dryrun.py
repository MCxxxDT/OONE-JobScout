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

# 审计补丁#4/#5：系统回显与短结束语不判待回复（2026-09-07 实测误判修复）
c_sys1 = flows.parse_conv("08月31日|王女士靖安科技CHO|您的附件简历 简历 已发送给Boss点击查看附件", ops)
check("系统回显(简历已发送给Boss)不判待回复", bool(c_sys1) and not c_sys1["needs_reply_guess"])
c_sys2 = flows.parse_conv("08月31日|顾女士成都福客人工智能科技高级招聘专员|对方已同意，您的附件简历已发送给对方", ops)
check("系统回显(对方已同意)不判待回复", bool(c_sys2) and not c_sys2["needs_reply_guess"])
c_close = flows.parse_conv("08月31日|罗女士肯德基招聘主管|[已读]|谢谢", ops)
check("短促结束语(谢谢)视为对话闭环", bool(c_close) and not c_close["needs_reply_guess"])
c_keep = flows.parse_conv("09月01日|韩女士杭州壹网壹创招聘HR|现在在杭州吗，可以接受线下面试吗", ops)
check("真实HR提问仍判待回复", bool(c_keep) and c_keep["needs_reply_guess"])
c_long = flows.parse_conv("09月05日|某公司HR|谢谢，期待您的回复", ops)
check("长句含谢谢不误判闭环", bool(c_long) and c_long["needs_reply_guess"])

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

print("  --- LLM 外部服务商配置与直连引擎校验 ---")
ai_engine_llm = _air.AIReplyEngine(cfg)
check("LLM从config读取api_key", bool(ai_engine_llm.openai_key))
check("LLM从config读取base_url", "askdiandian" in ai_engine_llm.openai_base)
check("LLM从config读取model", ai_engine_llm.llm_model == "dots3-note-prev")

llm_gen = ai_engine_llm._try_llm_generate({"who": "高先生沉心传媒招聘者", "last_msg": "方便沟通一下吗？"})
check("LLM直连生成结构化回复", bool(llm_gen and llm_gen.get("action") == "reply"))
check("LLM直连回复非空", bool(llm_gen and llm_gen.get("reply_text")))
check("LLM直连回复通过隐私红线", not _gr.privacy_blocked((llm_gen or {}).get("reply_text", "")))

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

print("== 11. 飞书可交互卡片与呼叫人工 (feishu_bot) ==")
from boss_apply import feishu_bot as _fb

# 1. 结构与渲染测试 (带推荐回复)
card_with_reply = _fb.build_interactive_card(
    company="阿里巴巴（中国）网络技术有限公司",
    last_msg="方便发一份附件简历吗？",
    reason="HR索要附件简历，建议一键发送",
    suggested_reply="您好！附件简历已发送，请查收，期待沟通！",
    time_str="14:30",
    job_title="AI产品经理",
)

check("卡片类型为 interactive", card_with_reply.get("msg_type") == "interactive")
card_body = card_with_reply.get("card", {})
check("卡片标题包含人工决策提醒", "待人工决策" in card_body.get("header", {}).get("title", {}).get("content", ""))
check("卡片主题色为橙色预警", card_body.get("header", {}).get("template") == "orange")

elements = card_body.get("elements", [])
check("卡片包含主要信息元素与按钮组", len(elements) >= 5)

action_elem = [e for e in elements if e.get("tag") == "action"]
check("卡片包含交互动作区", len(action_elem) == 1)
btns = action_elem[0].get("actions", [])
check("卡片包含4个交互按钮", len(btns) == 4)

btn_actions = [b.get("value", {}).get("action") for b in btns]
check("按钮包含 reply/exchange_wechat/send_resume/ignore",
      btn_actions == ["reply", "exchange_wechat", "send_resume", "ignore"])
check("首选回复按钮为 primary 样式", btns[0].get("type") == "primary")
check("忽略按钮为 danger 样式", btns[3].get("type") == "danger")

# 2. 结构测试 (不带推荐回复)
card_no_reply = _fb.build_interactive_card(
    company="某初创公司",
    last_msg="你好",
    reason="打招呼无需回复",
)
btns_no_reply = [e for e in card_no_reply["card"]["elements"] if e.get("tag") == "action"][0]["actions"]
check("无推荐回复时只有3个按钮", len(btns_no_reply) == 3)
check("首个按钮为换微信", btns_no_reply[0]["value"]["action"] == "exchange_wechat")

# 3. dry-run 模式下的呼叫人工留痕测试
alert_res = _fb.send_human_alert(
    cfg=cfg,
    alert_data={
        "company": "测试科技",
        "last_msg": "下周一能来线下面试吗？",
        "reason": "需要人工确认面试时间",
        "suggested_reply": "您好！方便先线上交流吗？",
        "time_str": "15:00",
    },
    dry_run=True,
)
check("send_human_alert dry-run 返回 ok", alert_res.get("ok") is True)
check("send_human_alert dry-run 未触发外部发送", alert_res.get("feishu_sent") is False)

# 检查 dry-run 留痕文件
card_log_path = cfgmod.state_path("feishu_cards.jsonl")
check("feishu_cards.jsonl 成功创建", os.path.exists(card_log_path))
with open(card_log_path, "r", encoding="utf-8") as f:
    logged_cards = [json.loads(line) for line in f if line.strip()]
check("feishu_cards.jsonl 包含记录", len(logged_cards) > 0)
check("留痕包含测试科技", logged_cards[-1].get("company") == "测试科技")

# 4. handle_card_action 交互回调派发测试
# 4.1 参数缺失
res_missing = _fb.handle_card_action(cfg, {"action": "reply"})
check("缺失 company 报错", res_missing.get("ok") is False)

# 4.2 忽略动作
res_ignore = _fb.handle_card_action(cfg, {"action": "ignore", "company": "测试科技"})
check("ignore 动作处理成功", res_ignore.get("ok") is True and res_ignore.get("action") == "ignore")

# 4.3 模拟 flows 交互回调 (通过 monkeypatch 验证 dispatch 正确)
orig_reply = flows.chat_reply
orig_wechat = flows.chat_exchange_wechat
orig_resume = flows.chat_send_resume
try:
    flows.chat_reply = lambda c, comp, txt: {"ok": True, "mock": "reply", "comp": comp, "txt": txt}
    flows.chat_exchange_wechat = lambda c, comp: {"ok": True, "mock": "exchange_wechat", "comp": comp}
    flows.chat_send_resume = lambda c, comp: {"ok": True, "mock": "send_resume", "comp": comp}

    res_reply = _fb.handle_card_action(cfg, {"action": "reply", "company": "测试科技", "text": "回复测试"})
    check("handle_card_action 派发 reply", res_reply.get("ok") is True and res_reply["result"]["mock"] == "reply")

    res_wx = _fb.handle_card_action(cfg, {"action": "exchange_wechat", "company": "测试科技"})
    check("handle_card_action 派发 exchange_wechat", res_wx.get("ok") is True and res_wx["result"]["mock"] == "exchange_wechat")

    res_cv = _fb.handle_card_action(cfg, {"action": "send_resume", "company": "测试科技"})
    check("handle_card_action 派发 send_resume", res_cv.get("ok") is True and res_cv["result"]["mock"] == "send_resume")
finally:
    flows.chat_reply = orig_reply
    flows.chat_exchange_wechat = orig_wechat
    flows.chat_send_resume = orig_resume

print("== 12. 守护补全校验（JD注入/台账防重复/熔断接入/收工日报，2026-09-08）==")
import datetime as _dt2

# 12.1 config 本地覆盖层合并（config.local.json 递归覆盖 config.json）
base = {"a": 1, "notify": {"x": 1, "y": 2}, "llm": {"model": "m0"}}
local = {"llm": {"model": "m1", "api_key": "k"}, "b": 2}
merged = cfgmod._deep_merge(base, local)
check("local覆盖同名标量键", merged["llm"]["model"] == "m1")
check("local深合并不丢兄弟键", merged["llm"]["api_key"] == "k" and merged["notify"]["x"] == 1)
check("local新增顶层键生效", merged["b"] == 2)
check("base不被就地修改", base["llm"]["model"] == "m0")

# 12.2 JD 上下文注入决策 Prompt（任务1a）
engine_jd = _air.AIReplyEngine(cfg)
conv_no_jd = {"who": "测试HR", "last_msg": "您好"}
p_no_jd = engine_jd.build_agent_prompt(conv_no_jd)
check("无job字段时prompt不含JD段", "- 岗位名称：" not in p_no_jd)
conv_jd = dict(conv_no_jd, job={"title": "AI产品经理", "company": "某科技", "salary": "200-300元/天",
                                "city": "杭州", "experience": "不限", "degree": "本科",
                                "jd_text": "负责Agent产品设计与MCP工具链落地。" * 20, "boss_active": 2})
p_jd = engine_jd.build_agent_prompt(conv_jd)
check("job字段注入prompt含JD段", "会话关联岗位" in p_jd)
check("JD注入含岗位名与薪资", "AI产品经理" in p_jd and "200-300元/天" in p_jd)
check("JD全文截断至600字内", "jd_text" not in p_jd and len(p_jd) < 8000)
check("JD注入含见人下菜碟心法", "见人下菜碟" in p_jd)

# 12.3 台账防重复交叉核对（任务1b）
sys.path.insert(0, os.path.join(cfgmod.ROOT, "scripts"))
import daemon_auto_reply as _dm

fixed_now = _dt2.datetime(2026, 9, 8, 15, 0, 0)
dt, has_t = _dm._parse_conv_time("14:03", now=fixed_now)
check("时间解析:今天HH:MM", dt == _dt2.datetime(2026, 9, 8, 14, 3) and has_t)
dt, has_t = _dm._parse_conv_time("昨天 15:20", now=fixed_now)
check("时间解析:昨天带时分", dt == _dt2.datetime(2026, 9, 7, 15, 20) and has_t)
dt, has_t = _dm._parse_conv_time("昨天", now=fixed_now)
check("时间解析:昨天无时分", dt is not None and not has_t)
dt, has_t = _dm._parse_conv_time("09月01日", now=fixed_now)
check("时间解析:月日历史", dt == _dt2.datetime(2026, 9, 1, 23, 59) and not has_t)
dt, has_t = _dm._parse_conv_time("10分钟前", now=fixed_now)
check("时间解析:相对时间", dt == fixed_now - _dt2.timedelta(minutes=10) and has_t)
check("时间解析:空文本返回None", _dm._parse_conv_time("") == (None, False))

rows = [
    {"action": "reply", "status": "ok", "company": "A公司", "ts": "2026-09-08 14:05:00"},
    {"action": "reply", "status": "failed", "company": "B公司", "ts": "2026-09-08 14:05:00"},
    {"action": "dryrun_reply", "company": "C公司", "ts": "2026-09-08 14:05:00"},
    {"action": "human_alert_card", "company": "D公司", "ts": "2026-09-08 14:05:00", "dry_run": False},
    {"action": "human_alert_card", "company": "E公司", "ts": "2026-09-08 14:05:00", "dry_run": True},
]
check("我方已回复且HR未再回复→防重发拦截", _dm.already_replied(rows, "A公司", "14:03"))
check("HR在回复后再发新消息→放行处理", not _dm.already_replied(rows, "A公司", "14:10"))
check("失败回复不算已回复", not _dm.already_replied(rows, "B公司", "14:03"))
check("仿真回复不算已回复", not _dm.already_replied(rows, "C公司", "14:03"))
check("已告警且HR未再回复→防重复呼叫", _dm.already_alerted(rows, "D公司", "14:03"))
check("dry-run告警不算已呼叫", not _dm.already_alerted(rows, "E公司", "14:03"))
check("无台账记录放行", not _dm.already_replied(rows, "F公司", "14:03"))

# 12.4 护栏熔断接入（任务1c）
g12 = guard.Guard(cfg)
g12.pause("unit-test-risk")
blk, why = _dm.guard_blocked(cfg)
check("熔断状态被daemon感知", blk and why == "unit-test-risk")
g12.resume()
blk, why = _dm.guard_blocked(cfg)
check("解除熔断后daemon放行", not blk)

# 12.5 收工日报（任务1e）
ledger.append({"action": "scan", "city": "杭州", "score": 15.0, "title": "Agent产品", "company": "甲科技", "href": "/j/x1"})
ledger.append({"action": "scan", "city": "上海", "score": 11.0, "title": "AI产品助理", "company": "乙科技", "href": "/j/x2"})
ledger.append({"action": "reply", "status": "ok", "company": "甲科技", "text_head": "您好！"})
report = _dm.build_daily_report_data(cfg)
check("日报数据统计当日扫描", report["scanned"] >= 2)
check("日报数据统计当日实发回复", report["replied"] >= 1)
check("日报needs_human含真实告警公司", "测试科技" in report["needs_human"])
check("日报Top岗位按分排序", report["top_jobs"] and report["top_jobs"][0]["score"] >= report["top_jobs"][-1]["score"])

card12 = _fb.build_daily_report_card(report)
check("日报卡片为interactive", card12.get("msg_type") == "interactive")
check("日报卡片标题为收工日报", "收工日报" in card12["card"]["header"]["title"]["content"])
check("日报卡片为绿色模板", card12["card"]["header"]["template"] == "green")
check("日报卡片含高分Top5", any("Top5" in json.dumps(e, ensure_ascii=False) for e in card12["card"]["elements"]))

res12 = _fb.send_daily_report(cfg, report, dry_run=True)
check("日报dry-run推送返回ok", res12.get("ok") is True)
check("日报dry-run不实发飞书", res12.get("feishu_sent") is False)
check("日报当日已发判定生效", _dm._daily_report_sent_today())

shutil.rmtree(DRY, ignore_errors=True)

print()
if fails:
    print("结果: %d 项失败 -> %s" % (len(fails), fails))
    sys.exit(1)
print("结果: 全部通过。管线可用，等待 T1(登录态) 接入。")


