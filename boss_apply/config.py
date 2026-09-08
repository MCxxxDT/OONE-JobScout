import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config.json")
# 本地敏感覆盖层（api_key 等，git 忽略）：同名键递归覆盖 config.json
LOCAL_CFG_PATH = os.path.join(ROOT, "config.local.json")
STATE_DIR = os.path.join(ROOT, "state")


def _deep_merge(base: dict, override: dict) -> dict:
    """override 递归覆盖 base：dict 深合并，其余类型直接替换。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if os.path.exists(LOCAL_CFG_PATH):
        with open(LOCAL_CFG_PATH, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, json.load(f))
    # 敏感值优先级：DPAPI secrets.json（Web 端保存，加密落盘）
    # > config.local.json > 环境变量。仅覆盖已存在的键路径，不改变其余结构。
    try:
        from . import secrets as _sec
        key = _sec.get_secret("llm_api_key")
        if key:
            cfg.setdefault("llm", {})["api_key"] = key
    except Exception:
        pass  # 非 Windows/无密文：按原链路（config.local > env）解析
    return cfg


def city_map(cfg):
    return {c["name"]: c for c in cfg["cities"]}


def state_path(name):
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)
