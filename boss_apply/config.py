import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config.json")
STATE_DIR = os.path.join(ROOT, "state")


def load():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def city_map(cfg):
    return {c["name"]: c for c in cfg["cities"]}


def state_path(name):
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)
