"""安全护栏：限速、限次、城市配额、风控熔断。状态持久化，跨进程共享与事务一致性。

F02 优化：
- 引入跨平台原子文件锁（_guard_file_lock），确保多实例/多进程并发读写无竞争；
- 每次检查配额前执行 reload() 确保读取最新磁盘数据，避免快照覆盖与漏计；
- 提供 acquire_greet_slot 原子预占方法，在锁内完成检查与扣减，彻底杜绝并发超发。
"""
import contextlib
import datetime
import errno
import json
import os
import sys
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


@contextlib.contextmanager
def _guard_file_lock(lock_path, timeout=5.0, timeout_s=None):
    """跨平台操作系统级排他文件锁，确保多实例/多进程并发读写无竞争。
    抢锁超时坚决抛出 TimeoutError，绝不裸奔放行。
    """
    if timeout_s is not None:
        timeout = timeout_s
    start_time = time.time()
    lock_dir = os.path.dirname(lock_path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    f = open(lock_path, "a+b")
    try:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
    except Exception:
        pass
    locked = False
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (IOError, OSError):
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"获取 Guard 文件锁超时 ({timeout}s): {lock_path}")
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            f.close()
        except Exception:
            pass


class Guard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = cfgmod.state_path("guard_state.json")
        self.lock_path = self.path + ".lock"
        self.s = dict(_DEFAULT)
        self.reload()

    def reload(self, in_lock=False):
        """从磁盘重载最新状态，保证多实例快照实时一致。"""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
                    if isinstance(disk_data, dict):
                        self.s.update(disk_data)
            except Exception:
                pass
        self._rollover(in_lock=in_lock)

    def _rollover(self, in_lock=False):
        if self.s.get("date") != _today():
            self.s["date"] = _today()
            self.s["greet_count"] = 0
            self.s["search_count"] = 0
            self.s["city_counts"] = {}
            self.s["paused_reason"] = None
            self.s["scan_done_today"] = False
            if in_lock:
                self._save_unlocked()
            else:
                self.save()

    def _save_unlocked(self):
        """原子写入状态文件（临时文件替换），防止写一半崩溃。"""
        if hasattr(cfgmod, "atomic_save_json"):
            cfgmod.atomic_save_json(self.path, self.s, indent=2)
        else:
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.s, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)

    def save(self):
        """加文件锁持久化。"""
        with _guard_file_lock(self.lock_path):
            self._save_unlocked()

    # ---------- 风控熔断 ----------
    def pause(self, reason):
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            self.s["paused_reason"] = reason
            self._save_unlocked()

    def resume(self):
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            self.s["paused_reason"] = None
            self._save_unlocked()

    @property
    def paused(self):
        return self.s.get("paused_reason")

    # ---------- 搜索（只读，低风险） ----------
    def check_search(self):
        self.reload()
        if self.s["paused_reason"]:
            return False, "paused: %s" % self.s["paused_reason"]
        if self.s["search_count"] >= self.cfg.get("search_daily_limit", 600):
            return False, "search daily limit reached"
        lo, hi = self.cfg.get("search_interval_seconds", [1.5, 3.5])
        wait = max(0.0, self.s["last_search_ts"] + lo - time.time())
        return True, wait

    def record_search(self):
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            self.s["search_count"] += 1
            self.s["last_search_ts"] = time.time()
            self._save_unlocked()

    # ---------- 沟通（高风险） ----------
    def _check_greet_internal(self, city=None):
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

    def check_greet(self, city=None):
        """检查是否允许发起打招呼（实时刷新最新磁盘数据）。"""
        self.reload()
        return self._check_greet_internal(city)

    def record_greet(self, city):
        """记录打招呼并持久化（带文件锁原子更新）。"""
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            clean = (city or "").strip().rstrip("市")
            self.s["greet_count"] += 1
            self.s["city_counts"][clean] = self.s["city_counts"].get(clean, 0) + 1
            self.s["last_greet_ts"] = time.time()
            self._save_unlocked()

    def acquire_greet_slot(self, city=None):
        """原子预占沟通配额：在文件锁保护下，重载最新状态、执行门禁校验并直接原子递增。
        返回 (ok, info_or_wait)。彻底解决并发竞争与超额风险。"""
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            ok, res = self._check_greet_internal(city)
            if not ok:
                return False, res
            clean = (city or "").strip().rstrip("市")
            self.s["greet_count"] += 1
            self.s["city_counts"][clean] = self.s["city_counts"].get(clean, 0) + 1
            self.s["last_greet_ts"] = time.time()
            self._save_unlocked()
            return True, res

    def release_greet_slot(self, city=None):
        """释放已预占但确定未实际发送的沟通配额（安全回滚）。"""
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            clean = (city or "").strip().rstrip("市")
            self.s["greet_count"] = max(0, self.s.get("greet_count", 1) - 1)
            if clean and clean in self.s.get("city_counts", {}):
                self.s["city_counts"][clean] = max(0, self.s["city_counts"][clean] - 1)
            self._save_unlocked()

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

    def has_unfilled_quota(self, cfg=None):
        """检查今日是否还有未用完的投递配额。
        返回 (has_gap: bool, gap_count: int, target_quota: int, greeted: int)"""
        self.reload()
        c = cfg or self.cfg
        daemon_cfg = c.get("daemon") or {}
        limit = int(c.get("daily_limit", 50))
        top_n = int(daemon_cfg.get("apply_top_n", 50))
        target_quota = min(limit, top_n)
        greeted = int(self.s.get("greet_count", 0))
        gap = max(0, target_quota - greeted)
        return (gap > 0), gap, target_quota, greeted

    def summary(self):
        self.reload()
        has_gap, gap, target, greeted = self.has_unfilled_quota()
        return {
            "date": self.s["date"],
            "greet_count": self.s["greet_count"],
            "daily_limit": self.cfg["daily_limit"],
            "target_quota": target,
            "quota_left": gap,
            "city_counts": self.s["city_counts"],
            "search_count": self.s["search_count"],
            "paused_reason": self.s["paused_reason"],
            "scan_done_today": self.s.get("scan_done_today", False),
        }

    # ---------- 每日投递扫描标记 ----------
    def is_scan_done(self):
        self.reload()
        return bool(self.s.get("scan_done_today", False))

    def mark_scan_done(self):
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            self.s["scan_done_today"] = True
            self._save_unlocked()

    def reset_scan_done(self):
        """重置今日扫描完成标记，允许守护进程或主动调用重新触发补投扫描。"""
        with _guard_file_lock(self.lock_path):
            self.reload(in_lock=True)
            self.s["scan_done_today"] = False
            self._save_unlocked()
