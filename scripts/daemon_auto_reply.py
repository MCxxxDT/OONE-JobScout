"""消息中心后台自动巡检与智能回复守护脚本。
支持：
- 单次排查（--once）与常驻后台守护（--loop）；
- 仿真干跑（--dry-run），绝不碰真实发送；
- 工作时间闸门（--active-hours 默认 09:30-20:30，非工作时段强制待命休眠）；
- 轮询间隔可从 config.json 的 daemon 段读取（interval-min/max，CLI 显式传参优先）；
- 拟人化打字等待（--typing-delay-min 12 --typing-delay-max 35）；
- 消息时效过滤（--max-age-hours 默认 24，杜绝历史冷会话误回）；
- 决策前 JD 上下文注入（flows.chat_job_detail，只读），随候选人画像一同进入决策 prompt；
- 台账防重复交叉核对：我方已回复/已告警且 HR 未再回复的会话不再处理；
- 护栏熔断接入：guard paused 即停巡检并飞书告警（flows.chat_reply 本身不受 guard 约束）；
- 收工日报：活跃窗口结束（20:30 后）当日只推送一次（台账 daily_report 去重）；
- --no-llm 强制禁用大模型（引擎无 Key 状态，全部走 needs_human 转人工，零 API 消耗）；
- 敏感意图坚决转人工（needs_human 写台账与告警）。

用法：
  python scripts/daemon_auto_reply.py --once --dry-run
  python scripts/daemon_auto_reply.py --loop
  python scripts/daemon_auto_reply.py --once --dry-run --no-llm   # 验证转人工告警链路
"""
import argparse
import datetime
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from boss_apply import ai_reply as air, config as cfgmod, feishu_bot, flows, guard as guardmod, ledger


# ---------------------------------------------------------------------------
# 台账防重复交叉核对（补全项 b）
# server.py chat_inbox 准则要求"与台账 action=reply 历史交叉核对防重复"：
# 同一会话我方已回复（或已告警）而 HR 未再回复时，不得重复发送/重复呼叫。
# ---------------------------------------------------------------------------

def _parse_conv_time(time_str, now=None):
    """会话列表时间文本 → (HR最后一条消息的估计时刻, 是否含精确时分)。
    支持 'HH:MM'（今天）、'昨天[ HH:MM]'、'M月D日'、'刚刚/N分钟前/N小时前'。"""
    now = now or datetime.datetime.now()
    ts = (time_str or "").strip()
    if not ts:
        return None, False
    try:
        if "刚刚" in ts:
            return now, True
        m = re.match(r"(\d+)\s*分钟前", ts)
        if m:
            return now - datetime.timedelta(minutes=int(m.group(1))), True
        m = re.match(r"(\d+)\s*小时前", ts)
        if m:
            return now - datetime.timedelta(hours=int(m.group(1))), True

        hm = None
        m = re.search(r"(\d{1,2}):(\d{2})", ts)
        if m:
            hm = datetime.time(int(m.group(1)), int(m.group(2)))

        if "昨天" in ts:
            base = now - datetime.timedelta(days=1)
            if hm:
                return base.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0), True
            return base.replace(hour=0, minute=0, second=0, microsecond=0), False

        m = re.match(r"(\d{1,2})月(\d{1,2})日", ts)
        if m:
            d = datetime.datetime(now.year, int(m.group(1)), int(m.group(2)))
            if d > now:
                d = d.replace(year=now.year - 1)
            if hm:
                d = d.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
                return d, True
            return d.replace(hour=23, minute=59, second=0, microsecond=0), False

        if hm:  # 纯 HH:MM → 今天
            return now.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0), True
    except Exception:
        return None, False
    return None, False


def _last_action_dt(rows, who, action, status=None, exclude_dry_run=False):
    """台账中该公司指定动作的最近时间（append-only 台账全量扫描）。"""
    best = None
    for r in rows:
        if r.get("action") != action or r.get("company") != who:
            continue
        if status and r.get("status") != status:
            continue
        if exclude_dry_run and r.get("dry_run"):
            continue
        try:
            dt = datetime.datetime.strptime(r.get("ts") or "", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if best is None or dt > best:
            best = dt
    return best


def _handled_since_hr_msg(rows, who, conv_time, action, status=None, exclude_dry_run=False):
    """核心判定：我方指定动作发生在会话中 HR 最后一条消息之后 → HR 未再回复 → 已处理。"""
    if not who:
        return False
    last = _last_action_dt(rows, who, action, status, exclude_dry_run)
    if last is None:
        return False
    hr_dt, has_time = _parse_conv_time(conv_time)
    if hr_dt is None:
        return False  # 时间无法解析，宁可不拦也不误拦
    if has_time:
        return last >= hr_dt
    return last.date() > hr_dt.date()  # 仅日期粒度：严格晚于该日才拦


def already_replied(rows, who, conv_time):
    """防重发：该公司已有成功回复(action=reply, status=ok)且发生在 HR 最后消息之后。"""
    return _handled_since_hr_msg(rows, who, conv_time, "reply", status="ok")


def already_alerted(rows, who, conv_time):
    """防重复呼叫：该会话已发过人工告警卡片(非dry-run)且发生在 HR 最后消息之后。"""
    return _handled_since_hr_msg(rows, who, conv_time, "human_alert_card", exclude_dry_run=True)


# ---------------------------------------------------------------------------
# 护栏熔断核查（补全项 c）
# ---------------------------------------------------------------------------

def guard_blocked(cfg):
    """护栏熔断状态：paused_reason 非空即熔断（风控信号触发后需人工 resume_guard）。"""
    g = guardmod.Guard(cfg)
    reason = g.paused
    return (reason is not None), reason


# ---------------------------------------------------------------------------
# 收工日报（补全项 e）
# ---------------------------------------------------------------------------

def build_daily_report_data(cfg):
    """从台账汇总当日运行数据（扫描/实发回复/转人工清单/高分岗位Top5/护栏状态）。"""
    today = datetime.date.today().isoformat()
    rows = ledger.load_all()
    scans = [r for r in rows if r.get("action") == "scan" and (r.get("ts") or "").startswith(today)]
    replies = [r for r in rows if r.get("action") == "reply" and r.get("status") == "ok"
               and (r.get("ts") or "").startswith(today)]
    needs_human = []
    for r in rows:
        if r.get("action") == "human_alert_card" and (r.get("ts") or "").startswith(today):
            c = r.get("company")
            if c and c not in needs_human:
                needs_human.append(c)
    top = sorted(scans, key=lambda r: -(r.get("score") or 0))[:5]
    g = guardmod.Guard(cfg)
    return {
        "date": today,
        "scanned": len(scans),
        "replied": len(replies),
        "needs_human": needs_human,
        "needs_human_count": len(needs_human),
        "top_jobs": [{"title": r.get("title"), "company": r.get("company"), "score": r.get("score")} for r in top],
        "guard_paused": g.paused,
    }


def _daily_report_sent_today():
    today = datetime.date.today().isoformat()
    return any(r.get("action") == "daily_report" and (r.get("ts") or "").startswith(today)
               for r in ledger.load_all())


def _maybe_send_daily_report(cfg, args):
    """收工日报触发：当前时间已过活跃窗口结束点（如 20:30）且当日未发过 → 推送一次。
    错过当晚不补发（数据仍完整保留在台账中）。"""
    now = datetime.datetime.now()
    try:
        _, end_s = args.active_hours.split("-")
        eh, em = map(int, end_s.split(":"))
        if now.time() < datetime.time(eh, em):
            return  # 活跃窗口尚未结束
    except Exception:
        return
    if _daily_report_sent_today():
        return
    report = build_daily_report_data(cfg)
    res = feishu_bot.send_daily_report(cfg, report, dry_run=False)
    print(f"  [收工日报] 已生成并推送：扫描 {report['scanned']} | 回复 {report['replied']} | "
          f"转人工 {report['needs_human_count']} 项 | 飞书送达: {res.get('feishu_sent')}")
    if not res.get("feishu_sent"):
        print(f"  [收工日报] 飞书未送达原因: {res.get('feishu_error')}")


# ---------------------------------------------------------------------------
# 巡检主循环
# ---------------------------------------------------------------------------

def run_cycle(cfg, engine, args, st=None):
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
            # 收工日报：窗口结束后的当晚推送一次（其余夜晚时段为空操作）
            try:
                _maybe_send_daily_report(cfg, args)
            except Exception as e:
                print(f"  [收工日报-异常] {e}")
            print(f"  [时间闸门-待命] 距离下一工作时段还需约 {hours:.1f} 小时（{secs} 秒），自动进入休眠。")
            ledger.append({
                "action": "daemon_gate_sleep",
                "current_time": now_str,
                "active_hours": args.active_hours,
                "wait_seconds": secs,
            })
            return {"status": "outside_active_hours", "wait_seconds": secs}

    # 2. 护栏熔断核查（补全项 c：flows.chat_reply 不受 guard 约束，daemon 每轮先查）
    paused, pause_reason = guard_blocked(cfg)
    if paused:
        print(f"  [护栏熔断] 检测到风控熔断状态，本轮巡检全停: {pause_reason}")
        ledger.append({"action": "daemon_guard_paused", "current_time": now_str,
                       "reason": str(pause_reason)[:200]})
        if not (st or {}).get("guard_alerted"):
            # 熔断事件首次发现时飞书告警一次（持续熔断期间不重复轰炸）
            feishu_bot.send_human_alert(
                cfg=cfg,
                alert_data={
                    "company": "【系统】护栏熔断",
                    "last_msg": f"守护进程检测到风控熔断：{pause_reason}",
                    "reason": "熔断即停：请人工确认页面安全（滑块/验证码）后调用 resume_guard 恢复巡检",
                    "time_str": now.strftime("%H:%M"),
                },
                dry_run=args.dry_run,
            )
            if st is not None:
                st["guard_alerted"] = True
        return {"status": "guard_paused", "reason": str(pause_reason)}
    if st is not None:
        st["guard_alerted"] = False  # 正常巡检中，重置熔断告警状态

    # 3. 裸CDP读取消息中心
    print("  [CDP] 正在拉取消息中心会话列表...")
    try:
        inbox = flows.chat_inbox(cfg)
    except Exception as e:
        print(f"  [CDP-ERROR] 消息中心拉取异常: {e}")
        ledger.append({"action": "daemon_error", "error": str(e)[:200]})
        return {"status": "error", "error": str(e)[:200]}
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

    # 4. 过滤时效（默认仅处理 24 小时以内的新消息）
    candidates = []
    for c in all_pending:
        t_str = c.get("time", "")
        if air.is_recent_message(t_str, max_age_hours=args.max_age_hours):
            candidates.append(c)
        else:
            print(f"  [时效跳过] 历史旧会话: {c.get('who')} ({t_str}) - 摘要: {str(c.get('last_msg'))[:30]}")

    print(f"  [时效筛选] {args.max_age_hours}小时内待处理活跃候选: {len(candidates)} 项")

    # 台账一次性载入，供防重复交叉核对（补全项 b）
    ledger_rows = ledger.load_all()

    # 5. 逐条决策与处理（全自主推进，绝不因敏感意图阻断）
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

        # 5.1 台账防重复交叉核对（补全项 b）：已回复/已告警且 HR 未再回复 → 跳过
        if already_replied(ledger_rows, who, conv.get("time")):
            print(f"  [防重复] 台账显示我方已成功回复且 HR 未再回复，跳过不重发。")
            continue
        if already_alerted(ledger_rows, who, conv.get("time")):
            print(f"  [防重复] 该会话已呼叫过人工且 HR 未再回复，跳过重复告警。")
            continue

        # 5.2 JD 上下文注入（补全项 a）：决策前先取会话关联岗位详情（只读，零发送）
        # 同一次点开会话顺路抓取聊天历史（记忆注入：与 HR 所见零漂移）
        try:
            jd_res = flows.chat_job_detail(cfg, company=who)
        except Exception as e:
            jd_res = {"ok": False, "error": str(e)[:200]}
        if jd_res.get("ok"):
            conv["job"] = jd_res.get("job") or {}
            conv["history"] = jd_res.get("history") or []
            _j = conv["job"]
            print(f"  [JD注入] {_j.get('title') or '?'} | {_j.get('salary') or '?'} | "
                  f"{_j.get('city') or '?'} | JD全文 {_j.get('jd_text') and len(_j['jd_text']) or 0} 字")
            print(f"  [记忆注入] 会话历史 {len(conv['history'])} 条（我方/HR/系统已标注）")
        else:
            print(f"  [JD注入] 未取到岗位详情（{str(jd_res.get('error'))[:80]}），按无JD上下文决策。")

        # 5.3 AI 决策（conv 已携带 job 字段 → 决策 prompt 注入 JD 与对话历史）
        decision = engine.decide_and_generate(conv)
        action = decision.get("action")
        reason = decision.get("reason")
        notice = decision.get("notice")
        reply_text = decision.get("reply_text", "")
        source = decision.get("source", "unknown")

        # 5.4 高意向识别（开源调研落地）：needs_human 告警升级红色卡片；回复留痕打标
        hi_flag, hi_why = air.detect_high_intent(conv)
        if hi_flag:
            print(f"  [🔥高意向] 检测到高意向信号（{hi_why}），告警与台账升级标记")

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
                    "job_title": (conv.get("job") or {}).get("title") or conv.get("title") or conv.get("job"),
                },
                dry_run=args.dry_run,
                high_intent=hi_flag,
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
                    "job_title": (conv.get("job") or {}).get("title") or "",
                    "high_intent": hi_flag,
                })
                replied_count += 1
            else:
                # 发送前护栏复查：熔断即停本轮（补全项 c）
                paused_now, pause_reason_now = guard_blocked(cfg)
                if paused_now:
                    print(f"  [护栏熔断] 发送前复查发现熔断: {pause_reason_now}，本轮剩余会话全部停止。")
                    ledger.append({"action": "daemon_guard_paused", "reason": str(pause_reason_now)[:200],
                                   "note": "pre-send recheck"})
                    break

                # 拟人化打字等待（高斯分布：真人打字节奏更接近正态）
                delay = behav.gauss_delay(args.typing_delay_min, args.typing_delay_max)
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
    parser.add_argument("--no-llm", action="store_true",
                        help="强制禁用大模型直连（引擎置为无Key状态，全部转人工，零API消耗；用于验证人工兜底链路）")
    parser.add_argument("--active-hours", default="09:30-20:30", help="允许对外发送的活跃时间窗口 (默认 09:30-20:30)")
    parser.add_argument("--interval-min", type=float, default=None,
                        help="轮询最小间隔（分钟；未指定时读 config.daemon.interval_min，缺省15）")
    parser.add_argument("--interval-max", type=float, default=None,
                        help="轮询最大间隔（分钟；未指定时读 config.daemon.interval_max，缺省25）")
    parser.add_argument("--max-replies-per-cycle", type=int, default=2, help="单轮最大回复数量（防刷屏，默认2）")
    parser.add_argument("--max-age-hours", type=int, default=24, help="只处理X小时以内的近期消息（默认24）")
    parser.add_argument("--typing-delay-min", type=float, default=12.0, help="拟人打字最小等待秒数（默认12）")
    parser.add_argument("--typing-delay-max", type=float, default=35.0, help="拟人打字最大等待秒数（默认35）")

    args = parser.parse_args()

    if not args.once and not args.loop:
        args.once = True  # 默认单次

    cfg = cfgmod.load()

    # 轮询间隔解析（补全项 d）：CLI 显式传参 > config.json daemon 段 > 硬编码默认
    daemon_cfg = cfg.get("daemon") or {}
    interval_src = "CLI"
    if args.interval_min is None:
        args.interval_min = float(daemon_cfg.get("interval_min", 15.0))
        interval_src = "config"
    if args.interval_max is None:
        args.interval_max = float(daemon_cfg.get("interval_max", 25.0))
        interval_src = "config" if interval_src == "config" else "CLI"
    if args.interval_max < args.interval_min:
        args.interval_max = args.interval_min

    engine = air.AIReplyEngine(cfg)
    if args.no_llm:
        engine.openai_key = ""
        engine.openrouter_key = ""

    print("==========================================================")
    print("  BOSS直聘消息中心智能巡检守护系统启动")
    print(f"  模式: {'常驻后台循环 (--loop)' if args.loop else '单次排查 (--once)'}")
    print(f"  仿真模式: {'开启 (--dry-run, 纯仿真不发送)' if args.dry_run else '实弹发送'}")
    print(f"  LLM 直连: {'禁用 (--no-llm, 全部转人工)' if args.no_llm else engine.llm_model}")
    print(f"  工作时间闸门: {args.active_hours}")
    print(f"  轮询间隔: {args.interval_min} ~ {args.interval_max} 分钟 (加随机抖动, 来源: {interval_src})")
    print(f"  单轮回复上限: {args.max_replies_per_cycle} 条 | 消息时效: {args.max_age_hours} 小时内")
    print("==========================================================")

    if args.once:
        run_cycle(cfg, engine, args)
        return

    # 常驻循环
    st = {"guard_alerted": False}
    while True:
        res = run_cycle(cfg, engine, args, st)
        status = res.get("status")
        if status == "outside_active_hours":
            wait_s = res.get("wait_seconds", 3600)
            # 按不超过30分钟分段休眠，便于随时响应或感知系统时钟
            chunk = min(wait_s, 1800)
            print(f"[DAEMON] 非工作时间休眠中... 本段休眠 {chunk/60:.1f} 分钟 (总需等待 {wait_s/3600:.1f} 小时)")
            time.sleep(chunk)
            continue
        if status == "guard_paused":
            # 熔断休眠：分段等待人工 resume_guard，解除后自动恢复巡检（不退出进程）
            print(f"[DAEMON] 护栏熔断中，休眠 30 分钟后复查（人工处理页面并 resume_guard 后自动恢复）...")
            time.sleep(1800)
            continue

        base_mins = random.uniform(args.interval_min, args.interval_max)
        jitter_s = random.uniform(-45.0, 45.0)
        sleep_s = max(60, int(base_mins * 60 + jitter_s))
        print(f"\n[DAEMON] 巡检完毕。拟人随机休眠 {sleep_s/60:.1f} 分钟后开始下一轮...")
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
