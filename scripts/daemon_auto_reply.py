"""消息中心后台自动巡检与智能回复守护脚本。
支持：
- 单次排查（--once）与常驻后台守护（--loop）；
- 仿真干跑（--dry-run），绝不碰真实发送；
- 工作时间闸门（--active-hours 默认 09:30-20:30，非工作时段强制待命休眠）；
- 随机抖动轮询间隔（--interval-min 15 --interval-max 25）；
- 拟人化打字等待（--typing-delay-min 12 --typing-delay-max 35）；
- 消息时效过滤（--max-age-hours 默认 24，杜绝历史冷会话误回）；
- 敏感意图坚决转人工（needs_human 写台账与告警）。

用法：
  python scripts/daemon_auto_reply.py --once --dry-run
  python scripts/daemon_auto_reply.py --loop
"""
import argparse
import datetime
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from boss_apply import ai_reply as air, config as cfgmod, feishu_bot, flows, ledger


def run_cycle(cfg, engine, args):
    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now_str}] ======= 开启新一轮巡检 =======")

    # 1. 检查工作时间闸门
    in_active = air.is_active_hour(now, args.active_hours)
    if not in_active:
        secs = air.seconds_until_next_active(now, args.active_hours)
        hours = secs / 3600.0
        if args.dry_run:
            print(f"  [时间闸门-仿真] 当前处于非工作时段（{args.active_hours}），由于指定了 --dry-run，继续仿真评估...")
        else:
            print(f"  [时间闸门-拦截] 当前处于非工作时段（{args.active_hours}）。为防风控，严禁对外发送！")
            print(f"  [时间闸门-待命] 距离下一工作时段还需约 {hours:.1f} 小时（{secs} 秒），自动进入休眠。")
            ledger.append({
                "action": "daemon_gate_sleep",
                "current_time": now_str,
                "active_hours": args.active_hours,
                "wait_seconds": secs,
            })
            return {"status": "outside_active_hours", "wait_seconds": secs}

    # 2. 裸CDP读取消息中心
    print("  [CDP] 正在拉取消息中心会话列表...")
    inbox = flows.chat_inbox(cfg)
    if inbox.get("error"):
        print(f"  [CDP-ERROR] 消息中心拉取失败: {inbox.get('error')}")
        ledger.append({"action": "daemon_error", "error": inbox.get("error")})
        return {"status": "error", "error": inbox.get("error")}

    all_convs = inbox.get("all", [])
    # 全自主无人值守：汇总所有待回复与平台检测会话（去重）
    pool = inbox.get("needs_reply", []) + inbox.get("needs_human", [])
    seen = set()
    all_pending = []
    for c in pool:
        k = c.get("who", "")
        if k and k not in seen:
            seen.add(k)
            all_pending.append(c)

    print(f"  [巡检汇总] 消息列表共 {len(all_convs)} 项 | 待处理活跃会话: {len(all_pending)} 项")

    # 3. 过滤时效（默认仅处理 24 小时以内的新消息）
    candidates = []
    for c in all_pending:
        t_str = c.get("time", "")
        if air.is_recent_message(t_str, max_age_hours=args.max_age_hours):
            candidates.append(c)
        else:
            print(f"  [时效跳过] 历史旧会话: {c.get('who')} ({t_str}) - 摘要: {c.get('last_msg')[:30]}")

    print(f"  [时效筛选] 24小时内待处理活跃候选: {len(candidates)} 项")

    # 4. 逐条决策与处理（全自主推进，绝不因敏感意图阻断）
    replied_count = 0
    for conv in candidates:
        if replied_count >= args.max_replies_per_cycle:
            print(f"  [限额拦截] 本轮已达到最大回复条数（{args.max_replies_per_cycle} 条），剩余将在下个周期处理。")
            break

        who = conv.get("who") or ""
        last_msg = conv.get("last_msg") or ""
        print(f"\n  ----------------------------------------")
        print(f"  [会话目标] {who} (时间: {conv.get('time')})")
        print(f"  [HR最新消息] {last_msg}")

        decision = engine.decide_and_generate(conv)
        action = decision.get("action")
        reason = decision.get("reason")
        notice = decision.get("notice")
        reply_text = decision.get("reply_text", "")
        source = decision.get("source", "unknown")

        if notice:
            print(f"  [📢异步提醒] {notice}")
            ledger.append({
                "action": "notice_alert",
                "company": who,
                "notice": notice,
                "last_msg": last_msg,
            })

        if action == "skip":
            print(f"  [决策: 跳过] 原因: {reason}")
            continue

        if action == "needs_human":
            print(f"  [决策: 无法通过Agent回复 -> 触发飞书交互卡片呼叫人工] 原因: {reason}")
            alert_res = feishu_bot.send_human_alert(
                cfg=cfg,
                alert_data={
                    "company": who,
                    "last_msg": last_msg,
                    "reason": reason,
                    "notice": notice,
                    "suggested_reply": reply_text or decision.get("suggested_reply"),
                    "time_str": conv.get("time"),
                    "job_title": conv.get("title") or conv.get("job"),
                },
                dry_run=args.dry_run,
            )
            if args.dry_run:
                status_str = "仿真留痕"
            elif alert_res.get("feishu_sent"):
                status_str = "已推送飞书"
            else:
                err_msg = alert_res.get("feishu_error") or "未配置Webhook"
                status_str = f"未发送 ({err_msg})"
            print(f"  [呼叫人工] 状态: {status_str}")
            continue

        if action == "reply":
            print(f"  [决策: 自动太极回复] 来源: {source} | 理由: {reason}")
            print(f"  [回复文案] {reply_text}")

            if args.dry_run:
                print("  [DRY-RUN 仿真] 本次处于仿真模式，不向 CDP 发送实际点击与输入。")
                ledger.append({
                    "action": "dryrun_reply",
                    "company": who,
                    "last_msg": last_msg,
                    "reply_text": reply_text,
                    "source": source,
                    "reason": reason,
                })
                replied_count += 1
            else:
                # 拟人化打字等待
                delay = random.uniform(args.typing_delay_min, args.typing_delay_max)
                print(f"  [拟人等待] 模拟阅读与输入打字，等待 {delay:.1f} 秒...")
                time.sleep(delay)

                # 实际调用 flows.chat_reply 按公司名/会话标识匹配回复
                res = flows.chat_reply(cfg, who, reply_text)
                if res.get("ok"):
                    print(f"  [发送成功] 已送达 {who}！")
                    replied_count += 1
                else:
                    print(f"  [发送失败] 错误: {res.get('error') or res.get('blocked')}")

    print(f"\n  [周期结束] 本轮处理完成: 实际/仿真回复 {replied_count} 条。")
    ledger.append({
        "action": "daemon_cycle_summary",
        "timestamp": now_str,
        "scanned": len(all_convs),
        "candidates": len(candidates),
        "replied": replied_count,
        "dry_run": args.dry_run,
    })
    return {"status": "ok", "replied": replied_count, "candidates": len(candidates)}


def main():
    parser = argparse.ArgumentParser(description="BOSS直聘消息中心后台自动巡检与智能回复守护")
    parser.add_argument("--once", action="store_true", help="仅执行单次巡检并退出")
    parser.add_argument("--loop", action="store_true", help="常驻后台持续循环巡检")
    parser.add_argument("--dry-run", action="store_true", help="仿真干跑：拉取会话并生成回复，但不触发实际发送")
    parser.add_argument("--active-hours", default="09:30-20:30", help="允许对外发送的活跃时间窗口 (默认 09:30-20:30)")
    parser.add_argument("--interval-min", type=float, default=15.0, help="轮询最小间隔（分钟，默认15）")
    parser.add_argument("--interval-max", type=float, default=25.0, help="轮询最大间隔（分钟，默认25）")
    parser.add_argument("--max-replies-per-cycle", type=int, default=2, help="单轮最大回复数量（防刷屏，默认2）")
    parser.add_argument("--max-age-hours", type=int, default=24, help="只处理X小时以内的近期消息（默认24）")
    parser.add_argument("--typing-delay-min", type=float, default=12.0, help="拟人打字最小等待秒数（默认12）")
    parser.add_argument("--typing-delay-max", type=float, default=35.0, help="拟人打字最大等待秒数（默认35）")

    args = parser.parse_args()

    if not args.once and not args.loop:
        args.once = True  # 默认单次

    cfg = cfgmod.load()
    engine = air.AIReplyEngine(cfg)

    print("==========================================================")
    print("  BOSS直聘消息中心智能巡检守护系统启动")
    print(f"  模式: {'常驻后台循环 (--loop)' if args.loop else '单次排查 (--once)'}")
    print(f"  仿真模式: {'开启 (--dry-run, 纯仿真不发送)' if args.dry_run else '实弹发送'}")
    print(f"  工作时间闸门: {args.active_hours}")
    print(f"  轮询间隔: {args.interval_min} ~ {args.interval_max} 分钟 (加随机抖动)")
    print(f"  单轮回复上限: {args.max_replies_per_cycle} 条 | 消息时效: {args.max_age_hours} 小时内")
    print("==========================================================")

    if args.once:
        run_cycle(cfg, engine, args)
        return

    # 常驻循环
    while True:
        res = run_cycle(cfg, engine, args)
        if res.get("status") == "outside_active_hours":
            wait_s = res.get("wait_seconds", 3600)
            # 按不超过30分钟分段休眠，便于随时响应或感知系统时钟
            chunk = min(wait_s, 1800)
            print(f"[DAEMON] 非工作时间休眠中... 本段休眠 {chunk/60:.1f} 分钟 (总需等待 {wait_s/3600:.1f} 小时)")
            time.sleep(chunk)
            continue

        base_mins = random.uniform(args.interval_min, args.interval_max)
        jitter_s = random.uniform(-45.0, 45.0)
        sleep_s = max(60, int(base_mins * 60 + jitter_s))
        print(f"\n[DAEMON] 巡检完毕。拟人随机休眠 {sleep_s/60:.1f} 分钟后开始下一轮...")
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
