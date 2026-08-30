"""投递台账：jsonl 追加式，含打分理由与动作结果，可复盘可审计。"""
import json
import os
import time

from . import config as cfgmod


def append(row):
    row["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = cfgmod.state_path("ledger.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load_all():
    p = cfgmod.state_path("ledger.jsonl")
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def pending(cfg, min_score=None):
    """按分排序的、未沟通过的岗位。同一 href 去重（保留最高分）。"""
    min_score = cfg["min_score"] if min_score is None else min_score
    best = {}
    for r in load_all():
        if r.get("action") != "scan" or not r.get("href"):
            continue
        if r.get("score", 0) < min_score:
            continue
        h = r["href"]
        if h not in best or r["score"] > best[h]["score"]:
            best[h] = r
    greeted = {r.get("href") for r in load_all() if r.get("action") == "greet" and r.get("status") == "ok"}
    out = [r for h, r in best.items() if h not in greeted]
    out.sort(key=lambda r: -r["score"])
    return out


def stats():
    rows = load_all()
    today = time.strftime("%Y-%m-%d")
    scanned = [r for r in rows if r.get("action") == "scan"]
    greeted = [r for r in rows if r.get("action") == "greet"]
    return {
        "scan_total": len(scanned),
        "scan_today": len([r for r in scanned if (r.get("ts") or "").startswith(today)]),
        "greet_total": len([r for r in greeted if r.get("status") == "ok"]),
        "greet_today": len([r for r in greeted if r.get("status") == "ok" and (r.get("ts") or "").startswith(today)]),
        "top_jobs": [
            {"title": r.get("title"), "company": r.get("company"), "score": r.get("score")}
            for r in sorted(scanned, key=lambda r: -(r.get("score") or 0))[:5]
        ],
    }
