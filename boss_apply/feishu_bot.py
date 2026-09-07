"""飞书可交互卡片与人工呼叫模块。
支持：
1. 生成飞书富文本交互卡片（Interactive Card），含 HR 消息、Agent 建议分析及一键操作按钮；
2. 按钮动作闭环：一键发送推荐文案、官方换微信、推送简历、忽略；
3. 本地与跨端多通道告警：飞书 Webhook 实时卡片推送 + Windows 本机提示音/弹窗；
4. 仿真干跑与台账留痕：dry-run 模式下将卡片 JSON 写入 state/feishu_cards.jsonl。
"""
import datetime
import json
import os
import sys
from typing import Any, Dict, Optional

from . import config as cfgmod, flows, ledger


def build_interactive_card(
    company: str,
    last_msg: str,
    reason: str,
    suggested_reply: Optional[str] = None,
    time_str: Optional[str] = None,
    job_title: Optional[str] = None,
) -> Dict[str, Any]:
    """构建飞书标准交互式卡片 JSON (Interactive Card)。"""
    time_display = time_str or datetime.datetime.now().strftime("%H:%M")
    job_display = f" · {job_title}" if job_title else ""

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📌 HR与公司**：{company}{job_display}\n**⏰ 接收时间**：{time_display}",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**💬 HR最新发言**：\n> {last_msg}",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**🧠 Agent 决策建议**：\n{reason}",
            },
        },
    ]

    if suggested_reply:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📝 推荐回复草稿**：\n```text\n{suggested_reply}\n```",
            },
        })

    elements.append({"tag": "hr"})

    # 交互式按钮组（类似 OpenClaw / Hermes 交互体验）
    actions = []
    if suggested_reply:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📤 一键发送推荐回复"},
            "type": "primary",
            "value": {
                "action": "reply",
                "company": company,
                "text": suggested_reply,
            },
        })

    actions.extend([
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "💬 官方换微信"},
            "type": "default",
            "value": {
                "action": "exchange_wechat",
                "company": company,
            },
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📄 发送附件简历"},
            "type": "default",
            "value": {
                "action": "send_resume",
                "company": company,
            },
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🚫 忽略此条"},
            "type": "danger",
            "value": {
                "action": "ignore",
                "company": company,
            },
        },
    ])

    elements.append({
        "tag": "action",
        "actions": actions,
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "🚨 BOSS直聘 · HR消息待人工决策",
                },
                "template": "orange",
            },
            "elements": elements,
        },
    }


def send_human_alert(cfg: dict, alert_data: dict, dry_run: bool = False) -> dict:
    """呼叫人工：构建卡片并发送通知（飞书 Webhook + Windows 声音/提示）。"""
    company = alert_data.get("company") or alert_data.get("who") or "未知HR"
    last_msg = alert_data.get("last_msg") or ""
    reason = alert_data.get("reason") or alert_data.get("notice") or "HR发来消息需人工查看决策"
    suggested_reply = alert_data.get("suggested_reply") or alert_data.get("reply_text") or None
    time_str = alert_data.get("time_str") or alert_data.get("time")

    card = build_interactive_card(
        company=company,
        last_msg=last_msg,
        reason=reason,
        suggested_reply=suggested_reply,
        time_str=time_str,
    )

    notify_cfg = cfg.get("notify") or {}
    feishu_webhook = notify_cfg.get("feishu_webhook") or os.getenv("FEISHU_WEBHOOK_URL") or ""
    enable_sound = notify_cfg.get("enable_sound", True)

    # 1. 本机系统声音提示（Windows 蜂鸣高优先级提示音）
    if enable_sound and not dry_run:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

    # 2. 仿真留痕台账
    card_log_path = cfgmod.state_path("feishu_cards.jsonl")
    try:
        with open(card_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "company": company,
                "dry_run": dry_run,
                "card": card,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 3. 飞书 Webhook 实弹推送
    feishu_sent = False
    feishu_error = None
    if feishu_webhook and not dry_run:
        try:
            import requests
            resp = requests.post(feishu_webhook, json=card, timeout=6)
            if resp.status_code == 200:
                res_data = resp.json()
                if res_data.get("code") == 0 or res_data.get("StatusCode") == 0:
                    feishu_sent = True
                else:
                    feishu_error = f"Feishu API error: {res_data}"
            else:
                feishu_error = f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as e:
            feishu_error = str(e)

    ledger.append({
        "action": "human_alert_card",
        "company": company,
        "feishu_sent": feishu_sent,
        "feishu_error": feishu_error,
        "dry_run": dry_run,
        "reason": reason,
    })

    return {
        "ok": True,
        "card": card,
        "feishu_sent": feishu_sent,
        "feishu_error": feishu_error,
        "dry_run": dry_run,
    }


def handle_card_action(cfg: dict, action_payload: dict) -> dict:
    """处理卡片点击回调或远程交互动作。
    action_payload 形如：
    {"action": "reply", "company": "淘宝闪购", "text": "收到，谢谢您！"}
    {"action": "exchange_wechat", "company": "淘宝闪购"}
    {"action": "send_resume", "company": "淘宝闪购"}
    {"action": "ignore", "company": "淘宝闪购"}
    """
    act = action_payload.get("action")
    company = action_payload.get("company") or ""

    if not act or not company:
        return {"ok": False, "error": "missing action or company"}

    if act == "reply":
        text = action_payload.get("text") or ""
        if not text:
            return {"ok": False, "error": "reply text is empty"}
        res = flows.chat_reply(cfg, company, text)
        return {"ok": res.get("ok", False), "action": act, "company": company, "result": res}

    if act == "exchange_wechat":
        res = flows.chat_exchange_wechat(cfg, company)
        return {"ok": res.get("ok", False), "action": act, "company": company, "result": res}

    if act == "send_resume":
        res = flows.chat_send_resume(cfg, company)
        return {"ok": res.get("ok", False), "action": act, "company": company, "result": res}

    if act == "ignore":
        ledger.append({
            "action": "card_ignore",
            "company": company,
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return {"ok": True, "action": "ignore", "company": company}

    return {"ok": False, "error": f"unknown action: {act}"}
