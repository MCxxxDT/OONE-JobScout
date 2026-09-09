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
# 画像沙箱化（2026-09-09）：真实 profile.local.json（冒烟实测产生）不得泄漏进 t0——
# 早期 ai_reply 断言基于硬编码画像语义，且 21/24 节的画像写入必须只落沙箱
from boss_apply import profile_store as _ps_herm
_ps_herm.PROFILE_PATH = os.path.join(DRY, "profile.local.json")

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
# 2026-09-08 沉心传媒复发案例：交换请求回执（同意/拒绝）不判待回复
c_sys3 = flows.parse_conv("22:41|张宏辉沉心传媒运营总监|您已经成功拒绝了对方交换微信请求", ops)
check("系统回执(拒绝交换微信)不判待回复", bool(c_sys3) and not c_sys3["needs_reply_guess"])
c_sys4 = flows.parse_conv("10:00|某HR|已同意和对方交换微信", ops)
check("系统回执(同意交换微信)不判待回复", bool(c_sys4) and not c_sys4["needs_reply_guess"])
c_sys5 = flows.parse_conv("10:00|某HR|您已成功同意对方交换电话请求", ops)
check("系统回执(同意交换电话)不判待回复", bool(c_sys5) and not c_sys5["needs_reply_guess"])
check("HR真实微信请求仍判待回复(转人工)", bool(flows.parse_conv("10:00|某HR|我想要和您交换微信，您是否同意", ops)))
# 侧栏预览含拒绝回执的完整行（复现23:04现场）
c_sys6 = flows.parse_conv("22:41|张宏辉沉心传媒运营总监|[送达]|您已经成功拒绝了对方交换微信请求", ops)
check("复现23:04现场:[送达]+拒绝回执不判待回复", bool(c_sys6) and not c_sys6["needs_reply_guess"])
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
check("卡片包含动作区", len(action_elem) == 1)

btns = action_elem[0].get("actions", [])
check("按钮为open_url跳转审批台(方案A)", all(b.get("url") for b in btns) and len(btns) >= 1)
check("跳转按钮携带token", all("token=" in (b.get("url") or "") for b in btns))
check("卡片含审批台操作说明", any("审批台" in json.dumps(e, ensure_ascii=False) for e in elements))

# 2. 结构测试 (不带推荐回复)
card_no_reply = _fb.build_interactive_card(
    company="某初创公司",
    last_msg="你好",
    reason="打招呼无需回复",
)
btns_no_reply = [e for e in card_no_reply["card"]["elements"] if e.get("tag") == "action"][0]["actions"]
check("无推荐回复时也有审批台跳转按钮", len(btns_no_reply) >= 1 and all(b.get("url") for b in btns_no_reply))

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
    flows.chat_reply = lambda c, comp, txt, **kw: {"ok": True, "mock": "reply", "comp": comp, "txt": txt}
    flows.chat_exchange_wechat = lambda c, comp, **kw: {"ok": True, "mock": "exchange_wechat", "comp": comp}
    flows.chat_send_resume = lambda c, comp, **kw: {"ok": True, "mock": "send_resume", "comp": comp}

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
    {"action": "reply", "status": "ok", "company": "A公司", "ts": _dt2.datetime.now().strftime("%Y-%m-%d") + " 14:05:00"},
    {"action": "reply", "status": "failed", "company": "B公司", "ts": _dt2.datetime.now().strftime("%Y-%m-%d") + " 14:05:00"},
    {"action": "dryrun_reply", "company": "C公司", "ts": _dt2.datetime.now().strftime("%Y-%m-%d") + " 14:05:00"},
    {"action": "human_alert_card", "company": "D公司", "ts": _dt2.datetime.now().strftime("%Y-%m-%d") + " 14:05:00", "dry_run": False},
    {"action": "human_alert_card", "company": "E公司", "ts": _dt2.datetime.now().strftime("%Y-%m-%d") + " 14:05:00", "dry_run": True},
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

print("== 13. 会话历史记忆注入（2026-09-08：聊天面板现场抓取，零漂移）==")

# 13.1 CHAT_HISTORY_JS 静态结构（真机侦察结论固化：.chat-message .im-list + item-myself/friend/system）
check("CHAT_HISTORY_JS 定位消息列表", ".chat-message .im-list" in _gr.CHAT_HISTORY_JS)
check("CHAT_HISTORY_JS 区分我方气泡", "item-myself" in _gr.CHAT_HISTORY_JS)
check("CHAT_HISTORY_JS 区分HR气泡", "item-friend" in _gr.CHAT_HISTORY_JS)
check("CHAT_HISTORY_JS 区分系统消息", "item-system" in _gr.CHAT_HISTORY_JS)
check("CHAT_HISTORY_JS 过滤已读噪声", "已读" in _gr.CHAT_HISTORY_JS)
check("CHAT_HISTORY_JS 过滤时间戳", "TIME_RE" in _gr.CHAT_HISTORY_JS)

# 13.2 解析层：MockSess 返回真实侦察结构（含时间戳/已读/按钮噪声）
class MockHistSess:
    def __init__(self, val):
        self.val = val
    def eval(self, js):
        return self.val

# 模拟壹网壹创真实会话提取结果（侦察实测 12 条的代表性子集）
hist_payload = json.dumps({
    "messages": [
        {"role": "me", "text": "您好，这是我的电话"},
        {"role": "system", "text": "请求交换电话已发送"},
        {"role": "hr", "text": "我想要一份您的附件简历，您是否同意"},
        {"role": "system", "text": "您的附件简历 简历 已发送给Boss点击查看附件"},
        {"role": "hr", "text": "现在在杭州吗，可以接受线下面试吗，预计到岗时间什么时候"},
        {"role": "me", "text": "您好！目前人在福州，对江浙沪方向的机会也一直持开放态度。初试方便先通过线上进行吗？"},
        {"role": "hr", "text": "目前实习生薪资3000（包括基础薪资2700…），可以接受吗"},
    ],
    "count": 7, "source": "dom",
})
h = _gr.get_active_conversation_history(MockHistSess(hist_payload))
check("历史解析返回列表", isinstance(h, list) and len(h) == 7)
check("历史解析保留发言方标注", h[0]["role"] == "me" and h[1]["role"] == "system" and h[2]["role"] == "hr")
check("历史解析保序（时间正序）", h[-1]["text"].startswith("目前实习生薪资"))

h_lim = _gr.get_active_conversation_history(MockHistSess(hist_payload), limit=3)
check("历史条数上限截尾（取最近）", len(h_lim) == 3 and h_lim[-1]["role"] == "hr")

check("无消息列表返回None", _gr.get_active_conversation_history(MockSess(json.dumps({"r": "no_list"}))) is None)
check("eval失败返回None", _gr.get_active_conversation_history(MockSess(None)) is None)

# 13.3 prompt 注入：带 history 的 conv
engine_hist = _air.AIReplyEngine(cfg)
p_no_hist = engine_hist.build_agent_prompt({"who": "测试HR", "last_msg": "您好"})
check("无history时prompt不含历史段", "【对话历史（最近" not in p_no_hist)
conv_hist = {
    "who": "韩女士壹网壹创HR", "last_msg": "目前实习生薪资3000，可以接受吗",
    "job": {"title": "电商运营实习生", "company": "壹网壹创", "salary": "130-180元/天"},
    "history": [
        {"role": "hr", "text": "现在在杭州吗，可以接受线下面试吗"},
        {"role": "me", "text": "您好！目前人在福州，初试方便先线上进行吗？"},
        {"role": "hr", "text": "目前实习生薪资3000，可以接受吗"},
    ],
}
p_hist = engine_hist.build_agent_prompt(conv_hist)
check("带history时prompt含对话历史段", "【对话历史" in p_hist)
check("历史段标注我方发言", "我方: 您好！目前人在福州" in p_hist)
check("历史段标注HR发言", "HR: 现在在杭州吗" in p_hist)
check("历史段条数标注正确", "最近3条" in p_hist)
check("历史与JD段共存", "会话关联岗位" in p_hist and "对话历史" in p_hist)
check("心法含防车轱辘话条目", "绝不车轱辘话" in p_hist)

# 13.4 chat_job_detail 集成管道（monkeypatch 全链路：RawCDP→点会话→抓岗位→抓历史）
class FakeSess13:
    def open_tab(self, url="about:blank"):
        return "tid"
    def nav(self, u):
        return {}
    def state(self):
        return {"blank": False, "bodyLen": 500}
    def eval(self, js):
        return "{}"
    def close_tab(self):
        pass
    def close(self):
        pass

_orig_rawcdp_cls = flows.rawcdp.RawCDP
_orig_open_conv13 = _gr._open_conversation_input
_orig_get_job13 = _gr.get_active_conversation_job
_orig_get_hist13 = _gr.get_active_conversation_history
try:
    flows.rawcdp.RawCDP = lambda ep: FakeSess13()
    _gr._open_conversation_input = lambda s, c, poll_s=12: ({"inputTag": "div"}, "head")
    _gr.get_active_conversation_job = lambda s: {"encryptJobId": "e13", "positionName": "AI产品经理",
                                                "companyName": "测试科技", "href": ""}
    _gr.get_active_conversation_history = lambda s, limit=20: [
        {"role": "hr", "text": "你好"}, {"role": "me", "text": "您好！"}]
    res13 = flows.chat_job_detail(cfg, company="测试科技", fetch_jd=False)
    check("chat_job_detail返回ok", res13.get("ok") is True)
    check("chat_job_detail携带history字段", isinstance(res13.get("history"), list) and len(res13["history"]) == 2)
    check("history发言方解析正确", res13["history"][0]["role"] == "hr" and res13["history"][1]["role"] == "me")

    # 历史提取异常不阻塞（增强项容错）
    def _raise_hist(s, limit=20):
        raise RuntimeError("boom")
    _gr.get_active_conversation_history = _raise_hist
    res13b = flows.chat_job_detail(cfg, company="测试科技", fetch_jd=False)
    check("历史提取异常不阻塞主流程", res13b.get("ok") is True and res13b.get("history") == [])

    # fetch_history=False 跳过提取
    _gr.get_active_conversation_history = lambda s, limit=20: [{"role": "hr", "text": "不应出现"}]
    res13c = flows.chat_job_detail(cfg, company="测试科技", fetch_jd=False, fetch_history=False)
    check("fetch_history=False不提取历史", res13c.get("ok") is True and res13c.get("history") == [])
finally:
    flows.rawcdp.RawCDP = _orig_rawcdp_cls
    _gr._open_conversation_input = _orig_open_conv13
    _gr.get_active_conversation_job = _orig_get_job13
    _gr.get_active_conversation_history = _orig_get_hist13

print("== 14. 拟人高斯延迟（2026-09-08 开源调研落地：真人节奏更接近正态）==")
from boss_apply import browser as _br

_vals = [_br.gauss_delay(12, 35) for _ in range(300)]
check("gauss延迟始终在区间内", all(12 <= v <= 35 for v in _vals))
check("gauss延迟均值接近区间中点", abs(sum(_vals) / len(_vals) - 23.5) < 2.0)
check("gauss零宽区间退化安全", _br.gauss_delay(5, 5) == 5)
check("gauss延迟中段密度高于端点", sum(1 for v in _vals if 18 <= v <= 29) > sum(1 for v in _vals if v < 15 or v > 32))

print("== 15. 高意向识别与告警升级（2026-09-08 开源调研落地：对标 ai-job）==")

hi1, w1 = _air.detect_high_intent({"who": "H", "last_msg": "下周三方便来公司线下面试吗", "history": []})
check("强关键词(面试邀约)判高意向", hi1 and w1 == "strong_kw")
hi1b, _ = _air.detect_high_intent({"who": "H", "last_msg": "好的", "history": [{"role": "hr", "text": "给你发offer流程了"}]})
check("强关键词(offer)判高意向", hi1b)
hi2, w2 = _air.detect_high_intent({"who": "H", "last_msg": "好的谢谢",
                                   "history": [{"role": "hr", "text": "期望薪资多少"}] * 4})
check("弱关键词+HR四轮判高意向", hi2 and "hr_turns=4" in w2)
hi3, _ = _air.detect_high_intent({"who": "H", "last_msg": "你好，看了你的简历方便聊聊吗", "history": []})
check("普通群发招呼不判高意向", not hi3)
hi4, _ = _air.detect_high_intent({"who": "H", "last_msg": "期望薪资多少", "history": [{"role": "hr", "text": "期望薪资"}]})
check("弱关键词轮数不足不判高意向", not hi4)
hi5, _ = _air.detect_high_intent({"who": "H", "last_msg": "好的", "history": [{"role": "me", "text": "面试聊过了"}] * 6})
check("仅我方发言提面试不误判(以HR发言为准)", not hi5)

card_hi = _fb.build_interactive_card(company="测试科技", last_msg="来线下面试吧", reason="x", high_intent=True)
check("高意向卡片红色模板", card_hi["card"]["header"]["template"] == "red")
check("高意向卡片标题带高意向标记", "高意向" in card_hi["card"]["header"]["title"]["content"])
check("高意向卡片含审批台跳转", any(b.get("url") for e in card_hi["card"]["elements"]
                                    if e.get("tag") == "action" for b in e.get("actions", [])))
card_norm15 = _fb.build_interactive_card(company="测试科技", last_msg="x", reason="y")
check("普通卡片保持橙色模板", card_norm15["card"]["header"]["template"] == "orange")

res15 = _fb.send_human_alert(cfg, {"company": "高意向科技", "last_msg": "发offer了",
                                   "reason": "测试高意向告警"}, dry_run=True, high_intent=True)
check("高意向告警dry-run返回ok", res15.get("ok") is True)
check("高意向告警卡片为红色", res15["card"]["card"]["header"]["template"] == "red")

print("== 16. 软收工与半月补回窗口（2026-09-08：22点收工+沟通完再下线+半月未回复会话补回）==")
import datetime as _dt16

# 16.1 半月时间窗口：M月D日 按日期差判定（相对今天构造，防测试日期漂移）
_today16 = _dt16.date.today()
_d_in = (_today16 - _dt16.timedelta(days=14)).strftime("%m月%d日")
_d_edge = (_today16 - _dt16.timedelta(days=15)).strftime("%m月%d日")
_d_out = (_today16 - _dt16.timedelta(days=20)).strftime("%m月%d日")
check("半月窗口内旧会话放行(14天前)", _air.is_recent_message(_d_in, max_age_hours=360))
check("半月边界放行(15天整)", _air.is_recent_message(_d_edge, max_age_hours=360))
check("超半月旧会话拦截(20天前)", not _air.is_recent_message(_d_out, max_age_hours=360))
check("24h窗口下旧会话仍拦截", not _air.is_recent_message(_d_in, max_age_hours=24))
_d_cross = (_today16 - _dt16.timedelta(days=10)).strftime("%m月%d日")
check("跨年场景日期解析不崩溃", isinstance(_air.is_recent_message(_d_cross, max_age_hours=360), bool))

# 16.2 daemon 配置读取（active_hours 22点收工 / max_age 360 / 软收工硬上限）
dc16 = cfg.get("daemon") or {}
check("daemon.active_hours配置22点收工", dc16.get("active_hours") == "09:30-22:00")
check("daemon.max_age_hours配置半月", dc16.get("max_age_hours") == 360)
check("daemon软收工硬上限配置", dc16.get("soft_close_hard_limit") == "23:30")

# 16.3 daemon_skip 幂等（skip 决策留痕，HR 未再回复不重复决策）
rows16 = [
    {"action": "daemon_skip", "company": "F公司", "ts": _dt16.datetime.now().strftime("%Y-%m-%d") + " 21:00:00", "dry_run": False},
    {"action": "daemon_skip", "company": "G公司", "ts": _dt16.datetime.now().strftime("%Y-%m-%d") + " 21:00:00", "dry_run": True},
]
check("已skip且HR未再回复→不再重复决策", _dm.already_skipped(rows16, "F公司", "20:50"))
check("HR在skip后再发新消息→放行重决策", not _dm.already_skipped(rows16, "F公司", "21:30"))
check("dry-run的skip不算已决策", not _dm.already_skipped(rows16, "G公司", "20:50"))

# 16.4 飞书卡片审批台跳转（方案A）
card16 = _fb.build_interactive_card(company="测试科技", last_msg="x", reason="y")
check("卡片含审批台说明行", any("审批台" in json.dumps(e, ensure_ascii=False)
                                 for e in card16["card"]["elements"]))
card_hi16 = _fb.build_interactive_card(company="测试科技", last_msg="x", reason="y", high_intent=True)
check("高意向卡片同样含说明行", any("审批台" in json.dumps(e, ensure_ascii=False)
                                   for e in card_hi16["card"]["elements"]))

print("== 17. 发言方回执判定（2026-09-08 修复：[送达]/[已读]=我方最后发言，平台权威信号）==")
# 沉心传媒 bug 场景：用户手打"直接boss说吧"（不在任何 opener 集合）+ 平台[送达]回执
# 修复前：前缀法失败 → 误判 HR 发言 → daemon 答非所问；修复后：回执法接住
c_bug = flows.parse_conv("22:17|张宏辉沉心传媒运营总监|[送达]|直接boss说吧", ops)
check("bug复现场景:[送达]+手打消息判我方发言", bool(c_bug) and not c_bug["needs_reply_guess"])
# 修复后核心断言：带 [送达] 回执 = 我方发言，即使文本不在 openers
c_fix1 = flows.parse_conv("08月31日|张宏辉沉心传媒运营总监|[送达]|直接boss说吧", ops)
check("回执法:[送达]手打消息判我方发言", bool(c_fix1) and not c_fix1["needs_reply_guess"])
c_fix2 = flows.parse_conv("昨天|高先生沉心传媒招聘者|[已读]|您好！感谢您耐心介绍~", ops)
check("回执法:[已读]判我方发言", bool(c_fix2) and not c_fix2["needs_reply_guess"])
# 无回执的 HR 发言仍正常判待回复
c_hr = flows.parse_conv("10:34|韩女士壹网壹创HR|目前实习生薪资3000可以接受吗", ops)
check("无回执HR发言仍判待回复", bool(c_hr) and c_hr["needs_reply_guess"] and c_hr["status"] == "")
# 双通道兼容：前缀法依然生效（无回执+我方模板前缀）
c_pf = flows.parse_conv("02:42|赵先生新美虹星总经理|" + g0, ops)
check("前缀法通道依然生效", bool(c_pf) and not c_pf["needs_reply_guess"])
# 实弹回归验证：今晚 daemon 已回的会话现在应显示我方发言（模拟侧栏行）
for row in ledger.load_all():
    if row.get("action") == "reply" and row.get("status") == "ok" and row.get("ts", "").startswith("2026-09-08"):
        c_ok = flows.parse_conv("%s|%s|[送达]|%s" % ("22:00", row.get("company", ""), (row.get("text_head") or "")[:50]), ops)
        if c_ok:
            check("已回会话带[送达]不判待回复(%s)" % row.get("company", "")[:10], not c_ok["needs_reply_guess"])
            break

print("== 18. 审批台 Web 服务（方案A：局域网工作台）==")
import importlib.util as _ilu
_aw_spec = _ilu.spec_from_file_location("approval_web", os.path.join(cfgmod.ROOT, "scripts", "approval_web.py"))
_aw = _ilu.module_from_spec(_aw_spec)
_aw_spec.loader.exec_module(_aw)

# 18.1 token 认证
check("错误token返回401", _aw._check_token(cfg, "wrong") is False)
_want18 = (cfg.get("web") or {}).get("token") or "boss-apply"
check("正确token通过认证", _aw._check_token(cfg, _want18) is True)

# 18.2 首页路由（HTML）
from fastapi.testclient import TestClient as _TC
_tc18 = _TC(_aw.app)
_r401 = _tc18.get("/", params={"token": "wrong"})
check("首页错误token返回401页面", _r401.status_code == 401)
_r_ok = _tc18.get("/", params={"token": _want18})
check("首页正确token返回工作台页面", _r_ok.status_code == 200 and "审批台" in _r_ok.text)
check("首页包含可编辑回复框与actReply", "replyText" in _r_ok.text and "actReply" in _r_ok.text)
check("首页包含伴随发送按钮与同意换微信", "agree_wechat" in _r_ok.text and "actWithText" in _r_ok.text)

# 18.3 overview API
_r_ov = _tc18.get("/api/overview", params={"token": _want18})
_d18 = _r_ov.json()
check("overview返回统计结构", "counts" in _d18 and "pending" in _d18["counts"] and "today" in _d18)
check("overview返回护栏状态", "guard" in _d18 and "paused_reason" in _d18["guard"])
check("overview返回台账流水", isinstance(_d18.get("ledger"), list))
check("overview无token返回401", _tc18.get("/api/overview").status_code == 401)

# 18.4 action 端点：参数校验（不真发，仅校验拒绝路径）
_r_bad = _tc18.post("/api/action", params={"token": _want18}, json={"action": "hack", "company": "x"})
check("非法action被拒绝", _r_bad.status_code == 400)
_r_bad2 = _tc18.post("/api/action", params={"token": _want18}, json={"action": "reply", "company": "x"})
check("空回复文本被拒绝", _r_bad2.status_code == 400)
_r_ign = _tc18.post("/api/action", params={"token": _want18}, json={"action": "ignore", "company": "测试科技-审批台"})
_d_ign = _r_ign.json()
check("ignore操作走handle_card_action管线", _d_ign.get("ok") is True and _d_ign.get("action") == "ignore")

print("== 19. 工具栏按钮受信任点击修复（2026-09-08 换微信误发换电话事故）==")
# 19.1 静态结构断言：修复三要素齐全
_src19 = _gr.TOOLBAR_BTN_POS_JS + _gr.VISIBLE_SURE_DIALOG_JS
check("按钮定位JS输出受信任坐标", "getBoundingClientRect" in _gr.TOOLBAR_BTN_POS_JS)
check("弹窗确认JS过滤display:none", "display === 'none'" in _gr.VISIBLE_SURE_DIALOG_JS)
check("弹窗确认JS过滤visibility:hidden", "visibility === 'hidden'" in _gr.VISIBLE_SURE_DIALOG_JS)
import inspect as _insp
_wx_src = _insp.getsource(_gr.exchange_wechat_via_chat)
check("换微信用受信任点击", "_trusted_click" in _wx_src)
check("换微信弹窗标题必须含微信", '"微信" not in title' in _wx_src and '"电话" in title' in _wx_src)
check("换微信含电话计数安全断言", "phone_after > phone_before" in _wx_src)
check("换微信核验交换微信消息计数", "请求交换微信" in _wx_src)
_resume_src = _insp.getsource(_gr.send_resume_via_chat)
check("发简历用受信任点击", "_trusted_click" in _resume_src)
check("发简历核验消息条目增量", "total_msgs() > before" in _resume_src)
# 19.2 flows 层如实上报
import boss_apply.flows as _fl19
_wx_flow_src = _insp.getsource(_fl19.chat_exchange_wechat)
check("flows换微信ok随status如实判定", 'r.get("status") == "ok"' in _wx_flow_src)

print("== 20. DPAPI 密钥保管（2026-09-09 Web端Key加密，方案A升级）==")
import sys as _sys20
if _sys20.platform == "win32":
    from boss_apply import secrets as _sec
    _k20 = "ak_roundtrip_test_12345"
    _blob = _sec.protect(_k20)
    check("DPAPI加密后不含明文", _k20 not in _blob and "=" not in str(_k20))
    check("DPAPI解密roundtrip", _sec.unprotect(_blob) == _k20)
    _sec.set_secret("t0_test_key", _k20)
    check("secret落盘可读回", _sec.get_secret("t0_test_key") == _k20)
    _raw20 = open(os.path.join(cfgmod.STATE_DIR, "secrets.json"), encoding="utf-8").read()
    check("落盘文件不含明文key", _k20 not in _raw20)
    check("脱敏显示", _sec.masked("ak_3B5j8l02Sp0YpwW6mbndVwF4h5r33").startswith("ak_3B") and _sec.masked("ak_3B5j8l02Sp0YpwW6mbndVwF4h5r33").endswith("r33"))
    _sec.set_secret("t0_test_key", "")  # 清理
    check("空值删除secret", _sec.get_secret("t0_test_key") is None)
    # 优先级链：secrets(DPAPI) > config.local.json —— 在 dryrun 沙箱内验证
    _sec.set_secret("llm_api_key", "ak_dpapi_priority_test")
    _cfg20 = cfgmod.load()
    check("DPAPI密钥优先级最高", _cfg20["llm"]["api_key"] == "ak_dpapi_priority_test")
    _sec.set_secret("llm_api_key", "")
    check("清除DPAPI后回退config.local", cfgmod.load()["llm"]["api_key"].startswith("ak_"))
else:
    check("非Windows跳过DPAPI断言", True)

print("== 21. 简历管道（2026-09-09 上传→解析→提炼→画像替换）==")
from boss_apply import profile_store as _ps

# 21.1 文本提取：纯文本直通 / txt文件 / PDF roundtrip / 不支持类型
_t21 = "张烨韬 福建师范大学 数字媒体技术 2027届"
_out, _err = _ps.extract_text(_t21)
check("粘贴纯文本直通", _err is None and _out == _t21)
_txt_path = os.path.join(DRY, "resume.txt")
with open(_txt_path, "w", encoding="utf-8") as f:
    f.write(_t21)
_out, _err = _ps.extract_text(_txt_path)
check("txt文件解析", _err is None and _t21 in _out)
_out, _err = _ps.extract_text(os.path.join(DRY, "不存在.pdf"))
check("不存在文件报错", _err is not None and "不存在" in _err)
_png_path = os.path.join(DRY, "photo.png")
with open(_png_path, "wb") as f:
    f.write(b"\x89PNG fake")
_out, _err = _ps.extract_text(_png_path)
check("不支持类型报错", _err is not None and "不支持" in _err)
import pymupdf as _pm
_pdf_path = os.path.join(DRY, "resume.pdf")
_doc = _pm.open()
_doc.new_page().insert_text((72, 72), "Zhang Yetao FJNU DMT 2027")
_doc.save(_pdf_path)
_doc.close()
_out, _err = _ps.extract_text(_pdf_path)
check("PDF解析roundtrip", _err is None and "FJNU" in _out.replace(" ", " "))
_docx_path = os.path.join(DRY, "resume.docx")
import docx as _dx
_d = _dx.Document()
_d.add_paragraph("Zhang Yetao docx test")
_d.save(_docx_path)
_out, _err = _ps.extract_text(_docx_path)
check("docx解析roundtrip", _err is None and "docx test" in _out)

# 21.2 保存与画像提取（写入 t0 沙箱的 profile.local.json）
_ps.PROFILE_PATH = os.path.join(DRY, "profile.local.json")
_ps.save_resume("模拟简历全文：张烨韬，福建师大数媒技术，做过FastMCP工具链。", "paste")
check("简历落盘可读回", _ps.get_resume().startswith("模拟简历全文"))
check("profile_meta元信息", _ps.profile_meta()["has_resume"] is True and _ps.profile_meta()["resume_chars"] > 10)
check("无refined时load_profile为None", _ps.load_profile() is None)
# 写入伪造 refined 验证覆盖链
_d21 = _load_json21 = json.load(open(_ps.PROFILE_PATH, encoding="utf-8"))
_d21["refined"] = {"name": "张烨韬", "school": "福建师范大学", "summary": "AI产品方向",
                    "tech_highlights": ["FastMCP"], "grad_year": 0, "current_city": ""}
json.dump(_d21, open(_ps.PROFILE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
_rp = _ps.load_profile()
check("refined读取并剔除空值键", _rp == {"name": "张烨韬", "school": "福建师范大学", "summary": "AI产品方向", "tech_highlights": ["FastMCP"]})
# 引擎画像合并：refined 覆盖硬编码同名字段
_eng21 = _air.AIReplyEngine(cfg)
check("引擎画像refined覆盖硬编码", _eng21.profile["school"] == "福建师范大学")
check("引擎画像refined新增键保留", _eng21.profile.get("summary") == "AI产品方向")
check("引擎画像硬编码键兜底", "三不原则" in _eng21.build_agent_prompt({"who": "H", "last_msg": "hi"}))

print("== 22. 偏好配置注入链（2026-09-09 向往/排斥 岗位×城市，留空=默认决断）==")
from boss_apply import citycodes as _cc

# 22.1 城市码表与解析
check("城市码表≥40城", len(_cc.CITY_CODES) >= 40)
check("城市码查找", _cc.lookup("杭州") == "101210100" and _cc.lookup(" 北京 ") == "101010100")
check("未知城市返回None", _cc.lookup("马尔代夫") is None)
res_cc, unk_cc = _cc.resolve_cities(["杭州", "不存在市"], None)
check("批量解析含未知报告", len(res_cc) == 1 and res_cc[0]["code"] == "101210100" and unk_cc == ["不存在市"])
check("extra映射优先", _cc.lookup("杭州", {"citycodes_extra": {"杭州": "999999"}}) == "999999")

# 22.2 effective_cities / keywords（无偏好=默认；有偏好=替换/剔除）
cfg22a = dict(cfg)
ec_a = flows.effective_cities(cfg22a)
check("无偏好回退默认城市集", len(ec_a["cities"]) == len(cfg["cities"]) and not ec_a["unknown"])
check("无偏好回退默认关键词", flows.effective_keywords(cfg22a) == cfg["keywords"])
cfg22b = dict(cfg, prefs={"want_jobs": ["AI产品经理", "Agent产品"],
                         "avoid_jobs": ["销售", "地推"],
                         "want_cities": ["杭州", "北京", "不存在市"],
                         "avoid_cities": ["北京"]})
ec_b = flows.effective_cities(cfg22b)
check("向往城市替换城市集", [c["name"] for c in ec_b["cities"]] == ["杭州"])
check("向往城市继承已配quota", ec_b["cities"][0]["quota"] == 25)
check("排斥城市剔除并报告", ec_b["excluded"] == ["北京"] and "北京" not in [c["name"] for c in ec_b["cities"]])
check("未知向往城市报告", ec_b["unknown"] == ["不存在市"])
check("向往岗位替换关键词", flows.effective_keywords(cfg22b) == ["AI产品经理", "Agent产品"])
cfg22c = dict(cfg, prefs={"avoid_cities": ["深圳"]})
ec_c = flows.effective_cities(cfg22c)
check("仅排斥城市时从默认集剔除", "深圳" not in [c["name"] for c in ec_c["cities"]] and ec_c["excluded"] == ["深圳"])

# 22.3 排斥岗位硬否决
v1, w1v = flows.avoid_veto({"title": "AI产品销售", "company": "x"}, ["销售"])
check("排斥词命中标题否决", v1 and w1v == "销售")
v2, _ = flows.avoid_veto({"title": "AI产品经理", "company": "外包之家"}, ["销售"])
check("未命中不否决", not v2)
v3, w3 = flows.avoid_veto({"title": "PM", "company": "某销售公司"}, ["销售"])
check("排斥词命中公司名否决", v3 and w3 == "销售")

# 22.4 偏好注入决策 prompt
eng22 = _air.AIReplyEngine(cfg22b)
p22 = eng22.build_agent_prompt({"who": "H", "last_msg": "hi"})
check("prompt含向往岗位", "AI产品经理" in p22 and "向往岗位方向" in p22)
check("prompt含排斥岗位", "地推" in p22)
check("prompt含自主决断说明", "自主决断" in p22)
p22n = _air.AIReplyEngine(dict(cfg)).build_agent_prompt({"who": "H", "last_msg": "hi"})
check("无偏好时prompt不含偏好段", "用户求职偏好" not in p22n)

print("== 23. LLM 站内智能匹配（2026-09-09 批量打分替代关键词加权，失败回退）==")
from boss_apply import llm_match as _lm

# 23.1 无 key / 关闭开关 → None（回退路径）
check("llm_match开关默认开启", (cfg.get("llm_match") or {}).get("enabled") is True)
cfg23_off = dict(cfg, llm_match={"enabled": False})
check("开关关闭返回None", _lm.match_batch([{"title": "x"}], cfg23_off) is None)
cfg23_nokey = dict(cfg, llm={"api_key": "", "base_url": ""})
check("无key返回None", _lm.match_batch([{"title": "x"}], cfg23_nokey) is None)

# 23.2 _call_once mock：伪造 LLM 返回解析（monkeypatch urllib）
class _FakeResp23:
    def __init__(self, body):
        self._body = body
        self.status = 200
    def read(self):
        return json.dumps({"choices": [{"message": {"content": self._body}}]}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

_orig_urlopen23 = None
try:
    import urllib.request as _ur23
    _orig_urlopen23 = _ur23.urlopen

    def _fake_urlopen(req, timeout=0):
        body = '[{"i": 0, "score": 25.5, "verdict": "high", "reason": "Agent方向高度契合"},' \
               '{"i": 1, "score": 99, "verdict": "veto", "reason": "销售岗"},' \
               '{"i": 2, "score": 3, "verdict": "low", "reason": "不相关"}]'
        return _FakeResp23("```json\n" + body + "\n```")
    _ur23.urlopen = _fake_urlopen
    jobs23 = [{"title": "Agent产品经理", "company": "A", "salary": "8-12K", "tags": "", "detail": "MCP"},
              {"title": "销售专员", "company": "B", "salary": "5-8K", "tags": "", "detail": ""},
              {"title": "行政助理", "company": "C", "salary": "4-6K", "tags": "", "detail": ""}]
    res23 = _lm.match_batch(jobs23, cfg)
    check("批量匹配解析成功", isinstance(res23, dict) and len(res23) == 3)
    check("LLM分数与verdict落位", res23[0]["score"] == 25.5 and res23[0]["verdict"] == "high")
    check("veto强制归零分", res23[1]["score"] == 0 and res23[1]["verdict"] == "veto")
    check("超上限分数被钳制", res23[1]["score"] <= 37)
    check("reason保留", "Agent方向" in res23[0]["reason"])
    # LLM 故障 → match_batch 整体 None（回退关键词）
    def _fake_err(req, timeout=0):
        raise RuntimeError("boom")
    _ur23.urlopen = _fake_err
    check("LLM故障返回None回退", _lm.match_batch(jobs23, cfg) is None)
    # 空 prompt 组件健全性
    check("偏好块含未指定说明", "未指定（由你自主决断）" in _lm._prefs_block(cfg))
    check("画像块含姓名", "张烨韬" in _lm._profile_block(cfg))
finally:
    if _orig_urlopen23 is not None:
        _ur23.urlopen = _orig_urlopen23

print("== 24. Web 设置端点（2026-09-09 Key/偏好/简历，沙箱重定向本地配置）==")
# 重定向 LOCAL_CFG_PATH 到沙箱（_write_local 与 load 都经 cfgmod 动态读取）
_orig_local_path24 = cfgmod.LOCAL_CFG_PATH
cfgmod.LOCAL_CFG_PATH = os.path.join(DRY, "config.local.json")
# 伪造 refine 跳过真实 LLM 调用
_orig_refine24 = _ps.refine_profile
_ps.refine_profile = lambda text, cfg: (None, "t0跳过LLM提炼")
try:
    _tc24 = _TC(_aw.app)
    _want24 = (cfg.get("web") or {}).get("token") or "boss-apply"

    # 24.1 GET /api/settings
    _s24 = _tc24.get("/api/settings", params={"token": _want24}).json()
    check("settings返回LLM脱敏信息", "api_key_masked" in _s24["llm"] and "key_source" in _s24["llm"])
    check("settings返回偏好结构", set(_s24["prefs"].keys()) == {"want_jobs", "avoid_jobs", "want_cities", "avoid_cities"})
    check("settings返回生效城市", isinstance(_s24["effective"]["cities"], list) and len(_s24["effective"]["cities"]) >= 1)
    check("settings返回画像元信息", "has_resume" in _s24["profile"])
    check("settings无token返回401", _tc24.get("/api/settings").status_code == 401)

    # 24.2 POST /api/settings（key→DPAPI 沙箱 secrets）
    _r24 = _tc24.post("/api/settings", params={"token": _want24},
                      json={"api_key": "ak_web_test_123", "base_url": "http://x/v1", "model": "m-test",
                            "llm_match_enabled": False}).json()
    check("settings保存返回变更清单", _r24.get("ok") is True and any("加密" in c for c in _r24["changed"]))
    _s24b = _tc24.get("/api/settings", params={"token": _want24}).json()
    check("保存后key来源为DPAPI", _s24b["llm"]["key_source"].startswith("DPAPI"))
    check("保存后base_url生效", _s24b["llm"]["base_url"] == "http://x/v1")
    check("智能匹配开关可关", _s24b["llm_match"]["enabled"] is False)
    # 清除 key（clear_key）
    _tc24.post("/api/settings", params={"token": _want24}, json={"clear_key": True})
    _s24c = _tc24.get("/api/settings", params={"token": _want24}).json()
    check("clear_key清除DPAPI密钥", not _s24c["llm"]["key_source"].startswith("DPAPI"))

    # 24.3 测试连接（无效地址快速失败）
    _t24 = _tc24.post("/api/settings/test", params={"token": _want24},
                      json={"base_url": "http://127.0.0.1:1/v1", "api_key": "k", "model": "m"}).json()
    check("测试连接无效地址报错", _t24.get("ok") is False and _t24.get("error"))

    # 24.4 POST /api/prefs（写沙箱本地配置）
    _p24 = _tc24.post("/api/prefs", params={"token": _want24},
                      json={"want_jobs": "AI产品，Agent产品", "avoid_jobs": "销售、地推",
                            "want_cities": "杭州\n北京", "avoid_cities": ""}).json()
    check("prefs保存成功", _p24.get("ok") is True)
    check("prefs分隔符解析", _p24["prefs"]["want_jobs"] == ["AI产品", "Agent产品"]
          and _p24["prefs"]["avoid_jobs"] == ["销售", "地推"]
          and _p24["prefs"]["want_cities"] == ["杭州", "北京"])
    check("prefs返回生效城市", "杭州" in _p24["effective_cities"] and "北京" in _p24["effective_cities"])

    # 24.5 POST /api/profile（粘贴文本，refine 被 mock 跳过）
    _r24p = _tc24.post("/api/profile", params={"token": _want24},
                       json={"text": "测试简历：张烨韬，做过FastMCP工具链与Coze中台。"}).json()
    check("简历保存成功", _r24p.get("ok") is True and _r24p.get("saved") is True)
    check("refine失败如实上报", _r24p.get("refined") is False and "跳过" in _r24p.get("refine_error", ""))
    _g24p = _tc24.get("/api/profile", params={"token": _want24}).json()
    check("profile读取返回预览", _g24p.get("has_resume") is True and "测试简历" in _g24p.get("resume_preview", ""))

    # 24.6 上传文件端点（txt multipart）
    _f24 = _tc24.post("/api/profile", params={"token": _want24},
                      files={"file": ("resume.txt", "文件上传测试：张烨韬 FastMCP".encode("utf-8"), "text/plain")}).json()
    check("文件上传保存成功", _f24.get("ok") is True and _f24.get("saved") is True)
    _bad24 = _tc24.post("/api/profile", params={"token": _want24},
                        files={"file": ("x.exe", b"MZ", "application/x-msdownload")}).json()
    check("不支持扩展名被拒", _bad24.get("ok") is False)

    # 24.7 页面含设置区
    _pg24 = _tc24.get("/", params={"token": _want24}).text
    check("页面含设置面板", "settingsBox" in _pg24 and "测试连接" in _pg24)
    check("页面含简历上传控件", "inFile" in _pg24 and "inResume" in _pg24)
finally:
    cfgmod.LOCAL_CFG_PATH = _orig_local_path24
    _ps.refine_profile = _orig_refine24


print("== 25. 岗位适配门禁 job_fit_gate（2026-09-09 配置优先/LLM兜底/归因留痕/零硬编码）==")
from boss_apply import llm_match as _lm25

cfg25 = dict(cfg)
cfg25["prefs"] = {"avoid_jobs": ["BD", "地推"], "want_jobs": ["AI产品"],
                  "want_cities": [], "avoid_cities": []}
cfg25["job_fit_gate"] = {"min_verdict": "medium"}

_orig_mo25 = _lm25.match_one
_orig_cjd25 = flows.chat_job_detail
try:
    # 25.1 排斥清单命中（公司名含BD）→ rule_blacklist，LLM 零调用
    _calls25 = {"n": 0}

    def _fake_llm25(job, c):
        _calls25["n"] += 1
        return {"score": 30.0, "verdict": "high", "reason": "不应被调用"}

    _lm25.match_one = _fake_llm25
    g25 = flows.judge_job_fit({"title": "商家BD", "company": "万葭灯火", "salary": "8-13K",
                                "city": "杭州", "jd_text": "渠道开发地推"}, cfg25)
    check("排斥命中拒绝(rule_blacklist)", g25["allow"] is False and g25["attribution"] == "rule_blacklist"
          and g25["hit_word"] == "BD")
    check("排斥命中不烧LLM", _calls25["n"] == 0)

    # 25.2 向往清单命中（标题）→ rule_whitelist
    g25b = flows.judge_job_fit({"title": "AI产品经理", "company": "某公司", "salary": "",
                                "city": "杭州", "jd_text": ""}, cfg25)
    check("向往命中放行(rule_whitelist)", g25b["allow"] is True and g25b["attribution"] == "rule_whitelist")

    # 25.3 未命中 + LLM low → llm_low_match
    _lm25.match_one = lambda job, c: {"score": 5.0, "verdict": "low", "reason": "销售地推岗"}
    g25c = flows.judge_job_fit({"title": "商务拓展", "company": "某公司", "salary": "",
                                "city": "", "jd_text": ""}, cfg25)
    check("LLM低分拒绝(llm_low_match)", g25c["allow"] is False and g25c["attribution"] == "llm_low_match"
          and g25c["verdict"] == "low" and g25c["score"] == 5.0)

    # 25.4 未命中 + LLM medium → llm_match_pass
    _lm25.match_one = lambda job, c: {"score": 15.0, "verdict": "medium", "reason": "方向匹配"}
    g25d = flows.judge_job_fit({"title": "产品运营", "company": "某公司", "salary": "",
                                "city": "", "jd_text": ""}, cfg25)
    check("LLM中分放行(llm_match_pass)", g25d["allow"] is True and g25d["attribution"] == "llm_match_pass")

    # 25.5 偏好全空 + LLM veto → 拒绝（LLM 兜底独立于清单）
    cfg25e = dict(cfg)
    cfg25e["prefs"] = {"avoid_jobs": [], "want_jobs": [], "want_cities": [], "avoid_cities": []}
    _lm25.match_one = lambda job, c: {"score": 0.0, "verdict": "veto", "reason": "销售岗"}
    g25e = flows.judge_job_fit({"title": "销售专员", "company": "某公司", "salary": "",
                                "city": "", "jd_text": ""}, cfg25e)
    check("偏好空veto拒绝", g25e["allow"] is False and g25e["attribution"] == "llm_low_match")

    # 25.6 LLM 不可用 → fail-close
    _lm25.match_one = lambda job, c: None
    g25f = flows.judge_job_fit({"title": "产品经理", "company": "某公司", "salary": "",
                                "city": "", "jd_text": ""}, cfg25e)
    check("LLM不可用fail-close", g25f["allow"] is False and g25f["attribution"] == "llm_unavailable")

    # 25.7 min_verdict=high 收紧：medium 被拒（阈值来自配置，零硬编码）
    cfg25g = dict(cfg25)
    cfg25g["job_fit_gate"] = {"min_verdict": "high"}
    _lm25.match_one = lambda job, c: {"score": 15.0, "verdict": "medium", "reason": "备选"}
    g25g = flows.judge_job_fit({"title": "产品运营", "company": "某公司", "salary": "",
                                "city": "", "jd_text": ""}, cfg25g)
    check("阈值high收紧拦截", g25g["allow"] is False and g25g["attribution"] == "llm_low_match")

    # 25.8 全链留痕：mock chat_job_detail → _job_fit_gate 写台账 job_fit_gate（归因可审计）
    flows.chat_job_detail = lambda c, company=None, fetch_jd=True, fetch_history=True: {
        "ok": True, "company": company,
        "job": {"title": "商家BD", "company": company, "salary": "8-13K",
                "city": "杭州", "jd_text": "地推"}}
    g25h = flows._job_fit_gate(cfg25, "字节跳动万葭灯火")
    check("门禁拦截返回归因", g25h["allow"] is False and g25h["attribution"] == "rule_blacklist")
    _lg25 = [r for r in ledger.load_all() if r.get("action") == "job_fit_gate"][-1]
    check("job_fit_gate台账留痕", _lg25["company"] == "字节跳动万葭灯火"
          and _lg25["attribution"] == "rule_blacklist" and _lg25["hit_word"] == "BD"
          and _lg25["allow"] is False and _lg25["job_title"] == "商家BD")

    # 25.9 拦截路径不触 CDP：chat_exchange_wechat / chat_send_resume 直接 blocked
    _r25x = flows.chat_exchange_wechat(cfg25, "字节跳动万葭灯火")
    check("exchange_wechat被门禁拦截", _r25x.get("ok") is False and _r25x.get("blocked") == "job_fit"
          and _r25x.get("attribution") == "rule_blacklist")
    _r25y = flows.chat_send_resume(cfg25, "字节跳动万葭灯火")
    check("send_resume被门禁拦截", _r25y.get("ok") is False and _r25y.get("blocked") == "job_fit")
    _ex25 = [r for r in ledger.load_all() if r.get("action") == "exchange_wechat"
             and r.get("status") == "blocked_job_fit"]
    check("拦截台账status=blocked_job_fit", bool(_ex25) and _ex25[-1]["company"] == "字节跳动万葭灯火"
          and "rule_blacklist" in _ex25[-1]["reason"])

    # 25.10 岗位信息不可得 → job_info_unavailable fail-close
    flows.chat_job_detail = lambda c, company=None, fetch_jd=True, fetch_history=True: {
        "ok": False, "error": "conversation not found"}
    g25i = flows._job_fit_gate(cfg25, "不存在公司")
    check("岗位不可得fail-close", g25i["allow"] is False
          and g25i["attribution"] == "job_info_unavailable")
finally:
    _lm25.match_one = _orig_mo25
    flows.chat_job_detail = _orig_cjd25


print("== 26. 凌晨时窗红线（2026-09-09 热修：00:00-活跃开始前硬休眠零发送）==")
import datetime as _dt26
import daemon_auto_reply as _dam26
_k26 = _dam26._out_of_window_kind
check("凌晨00:00硬休眠", _k26(_dt26.datetime(2026, 9, 9, 0, 0), "09:30-22:00", "23:30") == "hard_sleep")
check("凌晨03:00硬休眠", _k26(_dt26.datetime(2026, 9, 9, 3, 0), "09:30-22:00", "23:30") == "hard_sleep")
check("早08:59硬休眠", _k26(_dt26.datetime(2026, 9, 9, 8, 59), "09:30-22:00", "23:30") == "hard_sleep")
check("晚22:30软收工", _k26(_dt26.datetime(2026, 9, 9, 22, 30), "09:30-22:00", "23:30") == "soft_close")
check("晚23:35硬休眠", _k26(_dt26.datetime(2026, 9, 9, 23, 35), "09:30-22:00", "23:30") == "hard_sleep")

print("== 27. 沟通缺陷与动作断链修复校验（身份防混淆/系统消息过滤/动作分发解耦/真人感禁令）==")

# 27.1 系统提示正则是涵盖所有系统回执与事件
check("SYSTEM_MSG_RE拦截您已拒绝交换微信", bool(flows.SYSTEM_MSG_RE.search("您已拒绝交换微信")))
check("SYSTEM_MSG_RE拦截对方请求交换微信", bool(flows.SYSTEM_MSG_RE.search("对方请求交换微信")))
check("SYSTEM_MSG_RE拦截双方已交换微信", bool(flows.SYSTEM_MSG_RE.search("双方已交换微信")))
check("SYSTEM_MSG_RE拦截打招呼成功", bool(flows.SYSTEM_MSG_RE.search("打招呼成功，请等待回复")))
check("SYSTEM_MSG_RE拦截已撤回", bool(flows.SYSTEM_MSG_RE.search("已撤回一条消息")))

# 27.2 历史解析清洗 clean_conversation_history 剔除系统消息
raw_history_samples = [
    {"role": "me", "text": "您好！"},
    {"role": "system", "text": "打招呼成功"},
    {"role": "hr", "text": "发份简历来看看"},
    {"role": "system", "text": "对方请求交换微信"},
    {"role": "system", "text": "您已拒绝交换微信"},
]
cleaned_history = _gr.clean_conversation_history(raw_history_samples)
check("clean_conversation_history剔除系统提示", len(cleaned_history) == 2)
check("clean_conversation_history仅保留me与hr", [m["role"] for m in cleaned_history] == ["me", "hr"])

# 27.3 ai_reply 支持动作扩展（send_resume, exchange_wechat, agree_wechat）
engine_act = _air.AIReplyEngine(cfg)
res_cv = engine_act.decide_and_generate(
    {"who": "测试HR", "last_msg": "发个简历"},
    agent_generator=lambda c, p: {"action": "send_resume", "reply_text": "感谢您的详细介绍，已发您简历", "reason": "HR索要简历"}
)
check("decide_and_generate支持send_resume", res_cv["action"] == "send_resume")
check("八股文被自动清洗", "感谢您的详细介绍" not in res_cv["reply_text"])
check("send_resume保留有效伴随文本", "已发您简历" in res_cv["reply_text"])

res_agree = engine_act.decide_and_generate(
    {"who": "测试HR", "last_msg": "加个微信"},
    agent_generator=lambda c, p: {"action": "agree_wechat", "reply_text": "好的，已同意交换微信"}
)
check("decide_and_generate支持agree_wechat", res_agree["action"] == "agree_wechat")

# 27.4 系统消息作为 last_msg 时自动判定为 skip
res_sys_skip = engine_act.decide_and_generate({"who": "测试HR", "last_msg": "您已拒绝交换微信"})
check("系统提示last_msg直接被安全门禁skip", res_sys_skip["action"] == "skip")

# 27.5 chat_agree_wechat 集成管线
orig_agree = flows.chat_agree_wechat
try:
    flows.chat_agree_wechat = lambda c, comp: {"ok": True, "mock": "agree_wechat", "comp": comp}
    res_card_agree = _fb.handle_card_action(cfg, {"action": "agree_wechat", "company": "测试科技"})
    check("handle_card_action派发agree_wechat", res_card_agree.get("ok") is True and res_card_agree["result"]["mock"] == "agree_wechat")
finally:
    flows.chat_agree_wechat = orig_agree

# 27.6 handle_card_action 传递伴随文本验证
orig_ex = flows.chat_exchange_wechat
try:
    received = {}
    def mock_ex(c, comp, reply_text="", force=False):
        received["comp"] = comp
        received["reply_text"] = reply_text
        received["force"] = force
        return {"ok": True, "status": "ok"}
    flows.chat_exchange_wechat = mock_ex
    res_card_ex = _fb.handle_card_action(cfg, {"action": "exchange_wechat", "company": "腾讯互娱", "text": "加您微信了，请查收"})
    check("handle_card_action传递伴随文本", res_card_ex.get("ok") is True and received["reply_text"] == "加您微信了，请查收" and received["force"] is True)
finally:
    flows.chat_exchange_wechat = orig_ex

# 27.7 文本完整度与防截断清洗（Markdown剥除/防中途腰斩/句末完整）
raw_md_text = "好的华先生，我这就去整理发送。想请您多帮忙留意下贵司在杭州或北京的**实习/应届AI产品岗**。我有全栈Agent工程落地和商业化实操经验。"
cleaned_md = _air.sanitize_and_clean_reply(raw_md_text, max_chars=150)
check("sanitize剥除Markdown粗体标记", "**" not in cleaned_md and "实习/应届AI产品岗" in cleaned_md)
check("sanitize合理长度不截断整句", len(cleaned_md) == len(raw_md_text) - 4)

long_over_text = "好的华先生，我这就去整理发送。考虑到社招岗位与我的阶段不符，想请您多帮忙留意下贵司在杭州或北京的实习/应届AI产品岗。我有全栈Agent工程落地和商业化实操经验，对Vibe Coding和大模型协同办公方向非常感兴趣且有深度实践。方便的话想请教下您这边有相关的机会吗？"
safe_cut = _air.sanitize_and_clean_reply(long_over_text, max_chars=110)
check("sanitize超长安全截断于句号不挂残词", safe_cut.endswith("。") and not safe_cut.endswith("方。") and not safe_cut.endswith("方"))


print("== 28. 第一优先级硬编码与逻辑死锁修复校验 ==")
# 28.1 城市码剥离 '市' 后缀容错
from boss_apply import citycodes as _cc
check("城市名带市正确匹配城市码", _cc.lookup("北京市") == "101010100" and _cc.lookup("杭州市") == "101210100")
check("城市名不带市保持正确匹配", _cc.lookup("北京") == "101010100")

# 28.2 动态城市配额漏洞修复（不在静态配置中的新意向城市不再为 0 拦截）
_g28 = guard.Guard(cfg)
check("静态配置中城市配额正常", _g28.city_left("上海") > 0)
check("动态新城市赋予单城默认配额不为0", _g28.city_left("苏州") == 15)
check("城市名带市与不带市归一化", _g28.city_left("苏州市") == 15)
_g28.record_greet("苏州市")
check("记录打招呼后带市和不带市共享消耗", _g28.city_left("苏州") == 14 and _g28.city_left("苏州市") == 14)

# 28.3 聊天历史字符截断放宽至 1000 字符
from boss_apply import rawcdp as _rc
check("DOM历史消息切片已扩容至1000字符", ".slice(0, 1000)" in _rc.CHAT_HISTORY_JS and ".slice(0, 120)" not in _rc.CHAT_HISTORY_JS)

# 28.4 审批台短公司名消单与标记已处理
from scripts import approval_web as _aw
dummy_rows = [
    {"action": "human_alert_card", "company": "腾讯", "ts": "2026-09-09 18:00:00"},
    {"action": "mark_handled", "company": "腾讯科技", "ts": "2026-09-09 18:05:00", "status": "ok"},
    {"action": "human_alert_card", "company": "快手", "ts": "2026-09-09 18:00:00"},
    {"action": "exchange_wechat", "company": "快手", "ts": "2026-09-09 18:06:00", "status": "already_sent"},
]
res_tx, act_tx, _, _ = _aw._is_alert_resolved("2026-09-09 18:00:00", "腾讯", dummy_rows)
check("短公司名(2字符)子串正确消单", res_tx is True and act_tx == "mark_handled")

res_ks, act_ks, _, _ = _aw._is_alert_resolved("2026-09-09 18:00:00", "快手", dummy_rows)
check("短公司名already_sent正确消单", res_ks is True and act_ks == "exchange_wechat")

# 28.5 人工审批显式派发 force=True 放行门禁
orig_fit = flows._job_fit_gate
try:
    flows._job_fit_gate = lambda c, comp: {"allow": False, "attribution": "test_block", "detail": "blocked"}
    # 未指定 force 时应被门禁拦截
    res_blocked = flows.chat_exchange_wechat(cfg, "测试销售公司")
    check("默认门禁正常拦截不符岗位", res_blocked.get("ok") is False and res_blocked.get("blocked") == "job_fit")
    
    # 模拟人工审批台传入 force=True
    from boss_apply import greeter
    orig_wx_flow = greeter.exchange_wechat_via_chat
    try:
        greeter.exchange_wechat_via_chat = lambda s, comp: {"status": "ok", "conv": "mock"}
        res_forced = flows.chat_exchange_wechat(cfg, "测试销售公司", force=True)
        check("人工审批force=True放行跳过门禁", res_forced.get("ok") is True)
    finally:
        greeter.exchange_wechat_via_chat = orig_wx_flow
finally:
    flows._job_fit_gate = orig_fit


print("== 29. 隐私权限自设、防套话门禁与浏览器后台静默运行 ==")

# 29.1 动作权限策略 (check_privacy_permission)
from scripts import daemon_auto_reply as _dar

cfg_priv = {
    "privacy_policy": {
        "exchange_wechat": "high_intent_only",
        "send_resume": "auto",
        "exchange_phone": "manual",
    }
}
# 发简历 auto 放行
p_res_auto, _ = _dar.check_privacy_permission("send_resume", cfg_priv, hi_flag=False)
check("发简历auto模式在普通意向下放行", p_res_auto is True)

# 换微信 high_intent_only：非高意向拦截，高意向放行
p_wx_low, reason_wx_low = _dar.check_privacy_permission("exchange_wechat", cfg_priv, hi_flag=False)
check("换微信仅高意向模式在非高意向下拦截", p_wx_low is False and "仅高意向" in reason_wx_low)

p_wx_hi, _ = _dar.check_privacy_permission("exchange_wechat", cfg_priv, hi_flag=True)
check("换微信仅高意向模式在高意向下放行", p_wx_hi is True)

# 换电话 manual：无论是否高意向均拦截转人工
p_ph, reason_ph = _dar.check_privacy_permission("exchange_phone", cfg_priv, hi_flag=True)
check("换电话manual模式无论是否高意向均拦截转人工", p_ph is False and "人工审批" in reason_ph)

# 动作 disabled
cfg_priv_dis = {"privacy_policy": {"exchange_wechat": "disabled"}}
p_dis, r_dis = _dar.check_privacy_permission("exchange_wechat", cfg_priv_dis, hi_flag=True)
check("动作disabled模式禁止自动执行", p_dis is False and "禁用" in r_dis)

# 29.2 物理防套话与防泄密门禁 (detect_privacy_leak)
from boss_apply import ai_reply as _air

# 11 位手机号泄露拦截
leak_phone, r_leak_phone = _air.detect_privacy_leak("我的手机号是13912345678，可以加我沟通")
check("回复文案含11位手机号被物理门禁拦截", leak_phone is True and "手机号" in r_leak_phone)

# 用户配置联系方式拦截
cfg_user_contact = {
    "privacy_policy": {
        "contact_phone": "18888889999",
        "contact_wechat": "zyt_creative_2026",
    }
}
leak_cfg_wx, _ = _air.detect_privacy_leak("我的微信是zyt_creative_2026，随时联系", cfg=cfg_user_contact)
check("回复文案包含配置的微信号被物理门禁拦截", leak_cfg_wx is True)

# 常见微信号吐出模式拦截
leak_pattern, _ = _air.detect_privacy_leak("加我微信abc_123456吧")
check("回复文案包含常见微信吐出句式被物理门禁拦截", leak_pattern is True)

# 正常业务短语与平台动作不误伤
leak_safe, _ = _air.detect_privacy_leak("已向您发起交换微信申请，请查收")
check("平台标准动作文案不被误伤", leak_safe is False)

leak_biz, _ = _air.detect_privacy_leak("做过微信生态商业化与Agent开发")
check("普通业务词微信生态不被误伤", leak_biz is False)

# 29.3 RawCDP open_tab background=True 与 minimize_window
import inspect
sig = inspect.signature(_rc.RawCDP.open_tab)
check("RawCDP.open_tab 默认 background=True", sig.parameters["background"].default is True)
check("RawCDP 具备 minimize_window 方法", hasattr(_rc.RawCDP, "minimize_window"))

# 29.4 Web 端 Settings API 读写 privacy_policy 与 browser
from fastapi.testclient import TestClient
client = TestClient(_aw.app)
resp_get = client.get("/api/settings?token=boss-apply")
check("Settings GET 包含 privacy_policy 节点", resp_get.status_code == 200 and "privacy_policy" in resp_get.json())
check("Settings GET 包含 browser 节点", "browser" in resp_get.json())

# 测试 POST 写入
post_payload = {
    "privacy_policy": {
        "exchange_wechat": "high_intent_only",
        "send_resume": "auto",
        "exchange_phone": "manual",
        "contact_phone": "13800138000",
        "contact_wechat": "test_wx_id",
    },
    "browser": {
        "silent_mode": True,
        "minimize_on_start": True,
    }
}
resp_post = client.post("/api/settings?token=boss-apply", json=post_payload)
check("Settings POST 成功更新权限与浏览器设置", resp_post.status_code == 200 and resp_post.json().get("ok") is True)

# 回读验证
resp_get2 = client.get("/api/settings?token=boss-apply")
pol_saved = resp_get2.json().get("privacy_policy") or {}
br_saved = resp_get2.json().get("browser") or {}
check("Settings 回读 privacy_policy 正确保存", pol_saved.get("exchange_wechat") == "high_intent_only" and pol_saved.get("contact_phone") == "13800138000")
check("Settings 回读 browser 正确保存", br_saved.get("silent_mode") is True and br_saved.get("minimize_on_start") is True)


shutil.rmtree(DRY, ignore_errors=True)

print()
if fails:
    print("结果: %d 项失败 -> %s" % (len(fails), fails))
    sys.exit(1)
print("结果: 全部通过。管线可用，等待 T1(登录态) 接入。")


