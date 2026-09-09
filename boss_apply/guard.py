"""安全护栏：限速、限次、城市配额、风控熔断。状态持久化，跨进程共享。"""
import datetime
import json
import os
import time

from . import config as cfgmod

_DEFAULT = {
    "date": "",
    "greet_count": 0,
    "search_count": 0,
    "city_counts": {},
    "paused_reason": None,
    "last_greet_ts": 0.0,
    "last_search_ts": 0.0,
    "scan_done_today": False,
}


def _today():
    return datetime.date.today().isoformat()


class Guard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = cfgmod.state_path("guard_state.json")
        self.s = dict(_DEFAULT)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.s.update(json.load(f))
            except Exception:
                pass
        self._rollover()

    def _rollover(self):
        if self.s["date"] != _today():
            self.s["date"] = _today()
            self.s["greet_count"] = 0
            self.s["search_count"] = 0
            self.s["city_counts"] = {}
            self.s["paused_reason"] = None
            self.s["scan_done_today"] = False
            self.save()

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.s, f, ensure_ascii=False, indent=2)

    # ---------- 风控熔断 ----------
    def pause(self, reason):
        self.s["paused_reason"] = reason
        self.save()

    def resume(self):
        self.s["paused_reason"] = None
        self.save()

    @property
    def paused(self):
        return self.s["paused_reason"]

    # ---------- 搜索（只读，低风险） ----------
    def check_search(self):
        if self.s["paused_reason"]:
            return False, "paused: %s" % self.s["paused_reason"]
        if self.s["search_count"] >= self.cfg.get("search_daily_limit", 600):
            return False, "search daily limit reached"
        lo, hi = self.cfg.get("search_interval_seconds", [1.5, 3.5])
        wait = max(0.0, self.s["last_search_ts"] + lo - time.time())
        return True, wait

    def record_search(self):
        self.s["search_count"] += 1
        self.s["last_search_ts"] = time.time()
        self.save()

    # ---------- 沟通（高风险） ----------
    def check_greet(self, city=None):
        if self.s["paused_reason"]:
            return False, "paused: %s (run resume_guard after manual check)" % self.s["paused_reason"]
        if self.s["greet_count"] >= self.cfg["daily_limit"]:
            return False, "daily greet limit reached (%d)" % self.cfg["daily_limit"]
        if city:
            left = self.city_left(city)
            if left <= 0:
                return False, "city quota exhausted: %s" % city
        lo = self.cfg.get("interval_seconds", [4, 10])[0]
        wait = max(0.0, self.s["last_greet_ts"] + lo - time.time())
        return True, wait

    def record_greet(self, city):
        clean = (city or "").strip().rstrip("市")
        self.s["greet_count"] += 1
        self.s["city_counts"][clean] = self.s["city_counts"].get(clean, 0) + 1
        self.s["last_greet_ts"] = time.time()
        self.save()

    def city_left(self, city):
        clean = (city or "").strip().rstrip("市")
        quota = None
        for c in self.cfg.get("cities", []):
            c_name = (c.get("name") or "").strip().rstrip("市")
            if c_name == clean:
                quota = c.get("quota")
                break
        if quota is None:
            # 动态城市默认配额（不低于 15，且不超过单日总上限）
            quota = min(self.cfg.get("default_city_quota", 15), self.cfg.get("daily_limit", 30))
        used = self.s["city_counts"].get(clean, 0)
        if clean != city and city in self.s["city_counts"]:
            used += self.s["city_counts"].get(city, 0)
        return max(0, quota - used)

    def summary(self):
        return {
            "date": self.s["date"],
            "greet_count": self.s["greet_count"],
            "daily_limit": self.cfg["daily_limit"],
            "city_counts": self.s["city_counts"],
            "search_count": self.s["search_count"],
            "paused_reason": self.s["paused_reason"],
            "scan_done_today": self.s.get("scan_done_today", False),
        }

    # ---------- 每日投递扫描标记 ----------
    def is_scan_done(self):
        return bool(self.s.get("scan_done_today", False))

    def mark_scan_done(self):
        self.s["scan_done_today"] = True
        self.save()
