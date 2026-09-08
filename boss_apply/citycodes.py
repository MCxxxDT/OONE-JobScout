"""BOSS直聘城市名 → 城市码映射表（2026-09-09 偏好配置支持）。

码源说明：BOSS 城市码与 weather.com.cn 标准城市码同系（config.json 既有 7 城
实扫验证一致：杭州101210100/上海101020100 等）。下表全部条目均经真实搜索
URL 逐码验证（返回岗位数 > 0 才保留，验证脚本见 commit 记录）。
用户可在 config.local.json 的 citycodes_extra 段追加自定义映射。
"""
from . import config as cfgmod

# 已验证城市码表（城市名小写归一后匹配）
CITY_CODES = {
    "北京": "101010100",
    "上海": "101020100",
    "天津": "101030100",
    "重庆": "101040100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "南京": "101190100",
    "苏州": "101190400",
    "无锡": "101190200",
    "常州": "101191100",
    "南通": "101190600",
    "宁波": "101210400",
    "嘉兴": "101210300",
    "绍兴": "101210500",
    "金华": "101210900",
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "泉州": "101230500",
    "武汉": "101200100",
    "长沙": "101250100",
    "郑州": "101180100",
    "南昌": "101240100",
    "济南": "101120100",
    "青岛": "101120200",
    "西安": "101110100",
    "成都": "101270100",
    "贵阳": "101260100",
    "昆明": "101290100",
    "南宁": "101300100",
    "珠海": "101280700",
    "佛山": "101280800",
    "东莞": "101281600",
    "中山": "101281700",
    "惠州": "101280300",
    "沈阳": "101070100",
    "大连": "101070200",
    "哈尔滨": "101050100",
    "长春": "101060100",
    "石家庄": "101090100",
    "太原": "101100100",
}


def lookup(name, cfg=None):
    """城市名 → 城市码。优先 config 的 citycodes_extra 追加映射，再查内置表。
    找不到返回 None。"""
    name = (name or "").strip()
    if not name:
        return None
    extra = {}
    if cfg:
        extra = {str(k).strip().lower(): v for k, v in (cfg.get("citycodes_extra") or {}).items()}
    table = {k.lower(): v for k, v in CITY_CODES.items()}
    return extra.get(name.lower()) or table.get(name.lower())


def resolve_cities(names, cfg=None):
    """批量城市名解析。返回 (resolved, unknown)：
    resolved=[{name, code}]，unknown=[未识别的城市名]（不报错，供 Web 端提示）。"""
    resolved, unknown = [], []
    for n in (names or []):
        code = lookup(n, cfg)
        if code:
            resolved.append({"name": (n or "").strip(), "code": code, "quota": 15})
        else:
            unknown.append((n or "").strip())
    return resolved, unknown
