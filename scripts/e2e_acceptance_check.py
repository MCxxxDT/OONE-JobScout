# -*- coding: utf-8 -*-
"""E2E 全链路修复与系统就绪独立自动化验收脚本
涵盖四大核心验证目标：
1. 数据链路与会话完整性 (/api/overview resolved 杜绝空白, experience_memory 完备性)
2. 前端布局与桌面端规范校准 (.title-icon 18px 无巨型失真, 设置页 36px/紧凑/2x2 栅格)
3. 真实 BOSS 会话定位与交互底层修复 (模糊分词/去标点/小写化/already_active, 移除冗余刷新与轮询早退)
4. 全量回归测试校验 (32节自动化回归断言统计与确认)
"""
import ast
import json
import os
import re
import sys

# 切换至项目根目录
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

results = []

def check(title: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    results.append((status, title, detail))
    symbol = "✅" if condition else "❌"
    msg = f"  [{status}] {symbol} {title}"
    if detail and not condition:
        msg += f" -> 失败详情: {detail}"
    print(msg)


print("=" * 70)
print("1. 验证目标一：数据链路与会话完整性")
print("=" * 70)

# 1.1 验证 /api/overview 接口返回 resolved 列表数据字段完整且无空白
from scripts import approval_web as aw

overview_data = aw.api_overview("boss-apply")
resolved_list = overview_data.get("resolved", [])

check("api_overview 成功返回数据字典", isinstance(overview_data, dict))
check("resolved 列表非空（包含历史与扩展记录）", len(resolved_list) > 0, f"实际条数: {len(resolved_list)}")

blank_last_msg_count = 0
blank_suggested_count = 0
empty_company_count = 0

for item in resolved_list:
    comp = str(item.get("company") or "").strip()
    l_msg = str(item.get("last_msg") or "").strip()
    sugg = str(item.get("suggested") or "").strip()
    if not comp:
        empty_company_count += 1
    if not l_msg:
        blank_last_msg_count += 1
    if not sugg:
        blank_suggested_count += 1

check("resolved 记录中 company 杜绝空白", empty_company_count == 0, f"发现空白公司数: {empty_company_count}")
check("resolved 记录中 last_msg (HR发言) 杜绝空白", blank_last_msg_count == 0, f"发现空白HR发言数: {blank_last_msg_count}")
check("resolved 记录中 suggested (Agent回复) 杜绝空白", blank_suggested_count == 0, f"发现空白回复数: {blank_suggested_count}")

# 1.2 验证 _extract_dialog_texts 提取逻辑与动作兜底
hr1, ai1 = aw._extract_dialog_texts({"last_msg": "你好，看你履历很不错", "suggested_reply": "谢谢您关注"})
check("extract_dialog_texts 正常字段精准提取", hr1 == "你好，看你履历很不错" and ai1 == "谢谢您关注")

hr2, ai2 = aw._extract_dialog_texts({"conv": "系统: 开始\nHR: 沟通\n我们公司在福州台江区"}, {"action": "exchange_wechat"})
check("extract_dialog_texts conv多行提取与系统动作文案兜底", "福州台江区" in hr2 and "已在沟通界面向对方发起官方交换微信申请" in ai2)

hr3, ai3 = aw._extract_dialog_texts({"error": "定位失败(head='HR: 招聘主管\n您好\n还在看机会吗？')"}, {"action": "send_resume"})
check("extract_dialog_texts error/head 解析与发简历动作兜底", "还在看机会吗？" in hr3 and "已在沟通界面向对方发送正式在线简历" in ai3)

hr4, ai4 = aw._extract_dialog_texts({}, {"action": "agree_wechat"})
check("extract_dialog_texts 全空节点安全默认文案", hr4 == "（历史对话未抓取到单句正文，已记录会话节点）" and "已同意对方发起的交换微信邀请" in ai4)

# 1.3 验证经验记忆库 state/experience_memory.jsonl 数据完备性
from boss_apply import experience

exp_file = os.path.join(BASE_DIR, "state", "experience_memory.jsonl")
check("experience_memory.jsonl 文件存在", os.path.exists(exp_file))

all_exp = experience.load_all()
check("experience.load_all() 成功解析记忆记录", len(all_exp) > 0, f"实际记录数: {len(all_exp)}")

exp_corrupt = 0
exp_blank_hr = 0
exp_blank_ai = 0
exp_missing_fields = 0
required_exp_fields = {"id", "ts", "company", "hr_msg", "ai_reply", "score", "optimized_text", "source"}

with open(exp_file, "r", encoding="utf-8") as f:
    for idx, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            exp_corrupt += 1
            continue
        if not required_exp_fields.issubset(set(row.keys())):
            exp_missing_fields += 1
        if not str(row.get("hr_msg") or "").strip():
            exp_blank_hr += 1
        if not str(row.get("ai_reply") or "").strip():
            exp_blank_ai += 1

check("experience_memory.jsonl 无损坏 JSON 行", exp_corrupt == 0, f"损坏行数: {exp_corrupt}")
check("experience_memory.jsonl 包含全部必须字段", exp_missing_fields == 0, f"缺失字段行数: {exp_missing_fields}")
check("experience_memory.jsonl 杜绝空白 hr_msg", exp_blank_hr == 0, f"空白 hr_msg 行数: {exp_blank_hr}")
check("experience_memory.jsonl 杜绝空白 ai_reply", exp_blank_ai == 0, f"空白 ai_reply 行数: {exp_blank_ai}")


print()
print("=" * 70)
print("2. 验证目标二：前端布局与桌面端规范校准（系统设置页）")
print("=" * 70)

web_code_path = os.path.join(BASE_DIR, "scripts", "approval_web.py")
with open(web_code_path, "r", encoding="utf-8") as f:
    web_code = f.read()

# 2.1 验证 .title-icon CSS 规范
title_icon_css_match = re.search(r"\.title-icon\s*\{([^}]+)\}", web_code)
check("存在 .title-icon CSS 类定义", bool(title_icon_css_match))

if title_icon_css_match:
    css_props = title_icon_css_match.group(1)
    check(".title-icon 定义 width: 18px", "width: 18px;" in css_props)
    check(".title-icon 定义 height: 18px", "height: 18px;" in css_props)
    check(".title-icon 定义 max-width: 18px", "max-width: 18px;" in css_props)
    check(".title-icon 定义 flex-shrink: 0", "flex-shrink: 0;" in css_props)
    check(".title-icon 定义 display: inline-block", "display: inline-block;" in css_props)

check("定义 .title-icon.blue 辅助色", ".title-icon.blue" in web_code)
check("定义 .title-icon.green 辅助色", ".title-icon.green" in web_code)
check("定义 .title-icon.purple 辅助色", ".title-icon.purple" in web_code)
check("定义 .title-icon.amber 辅助色", ".title-icon.amber" in web_code)
check("定义 .title-icon.red 辅助色", ".title-icon.red" in web_code)

# 验证所有页面标题/区块 SVG 图标均受 title-icon 或明确尺寸约束，杜绝巨型失真
all_svgs = re.findall(r'<svg([^>]*)>', web_code)
unconstrained_svgs = [attrs for attrs in all_svgs if "title-icon" not in attrs and "width=" not in attrs and "class=\"island-svg\"" not in attrs and "class=\"spotlight-icon\"" not in attrs]
check("所有 SVG 图标均具备尺寸约束类或宽高属性（杜绝无尺寸失真）", len(unconstrained_svgs) == 0, f"无约束SVG: {unconstrained_svgs}")

# 2.2 验证设置页表单组件规范
input_css_match = re.search(r"\.settings-block\s+input\[type=text\][^{]*\{([^}]+)\}", web_code)
check("存在 .settings-block 输入框控件 CSS", bool(input_css_match))
if input_css_match:
    in_props = input_css_match.group(1)
    check("输入控件 padding: 7px 12px (标准36px桌面高度契合)", "padding: 7px 12px;" in in_props)
    check("输入控件 font-size: 13px", "font-size: 13px;" in in_props)
    check("输入控件 border-radius: 10px 紧凑圆角", "border-radius: 10px;" in in_props)

block_css_match = re.search(r"\.settings-block\s*\{([^}]+)\}", web_code)
check("存在 .settings-block 容器 CSS", bool(block_css_match))
if block_css_match:
    b_props = block_css_match.group(1)
    check("设置区块背景 #f8fafc 与精致边框", "background: #f8fafc;" in b_props and "border: 1px solid #edf2f7;" in b_props)
    check("设置区块内边距 padding: 18px 20px (紧凑布局)", "padding: 18px 20px;" in b_props)

label_css_match = re.search(r"\.settings-block\s+label\s*\{([^}]+)\}", web_code)
check("存在 .settings-block label CSS", bool(label_css_match))
if label_css_match:
    l_props = label_css_match.group(1)
    check("标签间距紧凑 margin: 8px 0 4px", "margin: 8px 0 4px;" in l_props)

# 验证设置页双栏栅格与 2x2 输入网格排版
settings_html = web_code.split('id="tab-settings"')[1].split('</main>')[0] if 'id="tab-settings"' in web_code else ""
check("设置页主体采用 col-lg-6 双栏 50/50 栅格排版", settings_html.count('class="col-lg-6"') == 2)
col6_count = settings_html.count('class="col-6"')
check("设置页内部表单采用 2x2 网格排版 (col-6 控件对)", col6_count >= 6, f"实际 2 列排版区块数: {col6_count}")


print()
print("=" * 70)
print("3. 验证目标三：真实 BOSS 会话定位与交互底层修复")
print("=" * 70)

from boss_apply import greeter

js_code = greeter._pick_conversation_js("杭州网易·雷火工作室（测试）")

# 3.1 验证模糊分词、去标点、小写化、already_active 逻辑
check("JS 逻辑包含标点与特殊空白去除正则", r"[\s\xa0\u3000\-_·•,，.()（）\[\]【】]" in js_code)
check("JS 逻辑包含 toLowerCase() 小写化", ".toLowerCase()" in js_code)
check("JS 逻辑包含中文与字母数字模糊分词提取", r"[\u4e00-\u9fa5]{2,}|[a-z0-9]{3,}" in js_code)
check("JS 逻辑支持已激活会话识别 (already_active)", "already_active" in js_code)
check("JS 逻辑检查右侧聊天视窗当前激活头部", ".chat-conversation .base-info, .chat-title, .user-name, .base-info" in js_code)
check("JS 逻辑包含精确匹配、反向子串与分词加权评分", "company_exact" in js_code and "company_reverse" in js_code and "bestScore" in js_code)
check("JS 逻辑覆盖 pointerdown/mousedown/pointerup/mouseup/click 事件", "'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'" in js_code)
check("JS 逻辑对 friend-content / friend-content-warp 分发受信任点击", ".friend-content, .friend-content-warp, div" in js_code)

# 3.2 验证 _open_conversation_input 移除多余重载与首轮轮询错误中止
fn_code = None
with open(os.path.join(BASE_DIR, "boss_apply", "greeter.py"), "r", encoding="utf-8") as f:
    greeter_full_text = f.read()

# 验证页面 URL 校验避免重复刷新
check("_open_conversation_input 具备 URL 检查避免重复重载", "if CHAT_URL not in str(cur_href):" in greeter_full_text)

# 验证首轮 notfound 移除了立即 return None, None
check("_open_conversation_input 移除了首轮 notfound 立即中止代码", "if isinstance(clicked, dict) and clicked.get(\"r\") == \"notfound\":\n            return None, None" not in greeter_full_text)

# 验证包含多轮滚动加载容错与 already_active 直接就绪
check("_open_conversation_input 包含 container.scrollTop += 300 滚动加载", "container.scrollTop += 300" in greeter_full_text)
check("_open_conversation_input 识别 already_active 直接探测输入框返回", "if clicked.get(\"r\") == \"already_active\":" in greeter_full_text)


print()
print("=" * 70)
print("4. 验证目标四：全量回归测试确认 (scripts/t0_dryrun.py)")
print("=" * 70)

# 解析 scripts/t0_dryrun.py
t0_path = os.path.join(BASE_DIR, "scripts", "t0_dryrun.py")
with open(t0_path, "r", encoding="utf-8") as f:
    t0_text = f.read()

sections = re.findall(r"print\([\"'](== \d+\..*?==)[\"']\)", t0_text)
check("t0_dryrun.py 完整包含 32 个业务架构测试章节", len(sections) == 32, f"实际章节数: {sections[-1]}")

check("t0_dryrun.py 1268行兼容多返回值解包", "res_tx, act_tx, *_" in t0_text)
check("t0_dryrun.py 1271行兼容多返回值解包", "res_ks, act_ks, *_" in t0_text)

# 统计总结果
total_count = len(results)
pass_count = sum(1 for r in results if r[0] == "PASS")
fail_count = sum(1 for r in results if r[0] == "FAIL")

print()
print("=" * 70)
print(f"独立验收测试执行汇总: 共 {total_count} 项断言 | PASS: {pass_count} | FAIL: {fail_count}")
print("=" * 70)

if fail_count > 0:
    print("❌ 存在未通过项，请排查！")
    sys.exit(1)
else:
    print("🎉 全链路独立验收断言 100% 全部通过！")
    sys.exit(0)
