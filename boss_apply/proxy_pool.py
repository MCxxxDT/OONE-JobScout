"""
boss_apply/proxy_pool.py - 企业级高可用代理池与地域就近路由中枢

功能特性：
1. GeoAffinityRouter：
   - 华东 (杭州/上海/南京/苏州/宁波)
   - 华南 (深圳/广州/东莞/佛山)
   - 华中 (武汉/长沙/合肥/南昌)
   - 西南 (成都/重庆)
   提供城市归一化及地域就近映射 (get_region_for_city)，支持拼音/英文与中文双向解析。
2. ProxyPoolManager：
   - 支持 HTTP / HTTPS / SOCKS5 / 动态住宅代理 (Residential Proxy)；
   - 智能解析代理端点与地域标签（URI Fragment / Query / Dict 载荷 / 住宅网关特征）；
   - 自动清洗路由元数据 query/fragment，确保输出干净标准的可连接代理 URL；
   - 提供地域就近调度 (get_proxy(city))，优先匹配同大区出口节点，无节点时自动降级全局健康池；
   - 故障感知与自愈熔断：连续失败 3 次自动熔断隔离 (report_failure)；
   - 成功调用即刻复位健康状态 (report_success)；
   - 全维度健康监控与大盘指标透出 (get_stats)。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

logger = logging.getLogger("boss_apply.proxy_pool")


@dataclass
class ProxyNode:
    """代理节点数据模型"""
    url: str
    protocol: str = "http"
    region: str = ""
    failure_count: int = 0
    success_count: int = 0
    is_circuit_broken: bool = False
    last_used_at: float = 0.0
    last_failed_at: float = 0.0
    last_success_at: float = 0.0
    raw_endpoint: Any = field(default=None)

    @property
    def is_healthy(self) -> bool:
        return not self.is_circuit_broken

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "protocol": self.protocol,
            "region": self.region,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "is_healthy": self.is_healthy,
            "is_circuit_broken": self.is_circuit_broken,
            "last_used_at": self.last_used_at,
            "last_failed_at": self.last_failed_at,
            "last_success_at": self.last_success_at,
        }


class GeoAffinityRouter:
    """地域就近路由器"""

    DEFAULT_REGION_MAP: Dict[str, List[str]] = {
        "华东": ["杭州", "上海", "南京", "苏州", "宁波"],
        "华南": ["深圳", "广州", "东莞", "佛山"],
        "华中": ["武汉", "长沙", "合肥", "南昌"],
        "西南": ["成都", "重庆"],
    }

    CITY_PINYIN_MAP: Dict[str, str] = {
        "hangzhou": "华东", "shanghai": "华东", "nanjing": "华东", "suzhou": "华东", "ningbo": "华东",
        "shenzhen": "华南", "guangzhou": "华南", "dongguan": "华南", "foshan": "华南",
        "wuhan": "华中", "changsha": "华中", "hefei": "华中", "nanchang": "华中",
        "chengdu": "西南", "chongqing": "西南",
        "huadong": "华东", "huanan": "华南", "huazhong": "华中", "xinan": "西南",
        "east": "华东", "south": "华南", "central": "华中", "southwest": "西南",
    }

    def __init__(self, region_map: Optional[Dict[str, List[str]]] = None):
        self.region_map = dict(self.DEFAULT_REGION_MAP)
        if region_map:
            for reg, cities in region_map.items():
                if reg in self.region_map:
                    self.region_map[reg] = list(dict.fromkeys(self.region_map[reg] + cities))
                else:
                    self.region_map[reg] = list(cities)

        # 构建城市名（带/不带“市”）到大区的倒排索引
        self._city_to_region: Dict[str, str] = {}
        for region, cities in self.region_map.items():
            for c in cities:
                norm_c = c.strip().rstrip("市")
                if norm_c:
                    self._city_to_region[norm_c] = region
                    self._city_to_region[norm_c + "市"] = region

    def get_region_for_city(self, city: str) -> str:
        """根据城市名称获取对应大区（如 杭州 -> 华东，深圳 -> 华南）。
        支持中文名、拼音、英文及带'市'后缀。若未匹配或空输入则返回空字符串。
        """
        if not city or not isinstance(city, str):
            return ""

        c = city.strip()
        # 1. 精确匹配
        if c in self._city_to_region:
            return self._city_to_region[c]

        # 2. 去除“市”后缀匹配
        norm = c.rstrip("市")
        if norm in self._city_to_region:
            return self._city_to_region[norm]

        # 3. 拼音/英文匹配
        lower = c.lower()
        if lower in self.CITY_PINYIN_MAP:
            return self.CITY_PINYIN_MAP[lower]

        # 4. 包含子串匹配（如 '浙江省杭州市' 或 '深圳南山区'）
        for known_city, reg in self._city_to_region.items():
            if known_city in c:
                return reg

        for py, reg in self.CITY_PINYIN_MAP.items():
            if py in lower:
                return reg

        return ""

    def get_cities_for_region(self, region: str) -> List[str]:
        """获取指定大区覆盖的城市列表"""
        return list(self.region_map.get(region, []))


class ProxyPoolManager:
    """企业级代理池管理器"""

    def __init__(
        self,
        cfg: Optional[dict] = None,
        endpoints: Optional[List[Union[str, dict]]] = None,
        geo_affinity: Optional[bool] = None,
        max_failures: int = 3,
        router: Optional[GeoAffinityRouter] = None,
        enabled: Optional[bool] = None,
    ):
        self._lock = threading.Lock()
        self.router = router or GeoAffinityRouter()

        # 解析配置
        pcfg = {}
        if cfg:
            if "proxy_pool" in cfg and isinstance(cfg["proxy_pool"], dict):
                pcfg = cfg["proxy_pool"]
            elif isinstance(cfg, dict):
                pcfg = cfg

        if enabled is not None:
            self.enabled = enabled
        elif endpoints is not None:
            self.enabled = True
        else:
            self.enabled = bool(pcfg.get("enabled", False))

        self.provider = pcfg.get("provider", "custom")
        self.geo_affinity = (
            geo_affinity if geo_affinity is not None else pcfg.get("geo_affinity", True)
        )
        self.max_failures = max_failures or pcfg.get("max_failures", 3)

        self._nodes: List[ProxyNode] = []
        raw_endpoints = endpoints if endpoints is not None else pcfg.get("endpoints", [])
        for ep in raw_endpoints:
            self.add_proxy(ep)

    def _parse_endpoint(
        self, endpoint: Union[str, dict, ProxyNode], explicit_region: Optional[str] = None
    ) -> Optional[ProxyNode]:
        """解析多样化代理端点（字符串、字典或已初始化的 ProxyNode）"""
        if isinstance(endpoint, ProxyNode):
            if explicit_region:
                endpoint.region = explicit_region
            return endpoint

        if isinstance(endpoint, dict):
            url = endpoint.get("url") or endpoint.get("endpoint") or endpoint.get("proxy", "")
            if not url:
                return None
            region = explicit_region or endpoint.get("region") or ""
            city = endpoint.get("city")
            if not region and city:
                region = self.router.get_region_for_city(city)
            protocol = endpoint.get("protocol")
            if not protocol:
                parsed = urlparse(url)
                protocol = parsed.scheme if parsed.scheme else "http"
            return ProxyNode(
                url=url,
                protocol=protocol,
                region=region,
                raw_endpoint=endpoint,
            )

        if isinstance(endpoint, str):
            raw_str = endpoint.strip()
            if not raw_str:
                return None

            # 解析 URI Fragment 标签（如 http://1.2.3.4:8080#华东）并清洗
            fragment_region = ""
            if "#" in raw_str:
                main_part, frag = raw_str.split("#", 1)
                frag = frag.strip()
                if frag in self.router.region_map:
                    fragment_region = frag
                else:
                    reg = self.router.get_region_for_city(frag)
                    if reg:
                        fragment_region = reg
                url = main_part
            else:
                url = raw_str

            parsed = urlparse(url)
            protocol = parsed.scheme if parsed.scheme else "http"

            # 解析 Query 参数中的地域（如 ?region=华东 或 ?city=深圳）并将其从 URL 中剔除保持干净
            query_region = ""
            clean_url = url
            if parsed.query:
                qs = parse_qs(parsed.query)
                if "region" in qs and qs["region"]:
                    val = qs["region"][0]
                    if val in self.router.region_map:
                        query_region = val
                    else:
                        reg = self.router.get_region_for_city(val)
                        if reg:
                            query_region = reg
                    qs.pop("region", None)
                elif "city" in qs and qs["city"]:
                    val = qs["city"][0]
                    reg = self.router.get_region_for_city(val)
                    if reg:
                        query_region = reg
                    qs.pop("city", None)

                # 重构清洗后的 URL
                clean_query = urlencode(qs, doseq=True)
                clean_url = urlunparse((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    clean_query,
                    "",  # 清除 fragment
                ))
            else:
                # 确保无 fragment
                clean_url = urlunparse((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    "",
                    "",
                ))

            # 解析住宅代理/动态网关账号中的地域标识（如 customer-resi-hangzhou）
            inferred_region = ""
            lower_url = clean_url.lower()
            for known_city, reg in self.router._city_to_region.items():
                if known_city in clean_url:
                    inferred_region = reg
                    break
            if not inferred_region:
                for py, reg in self.router.CITY_PINYIN_MAP.items():
                    if py in lower_url:
                        inferred_region = reg
                        break
            if not inferred_region:
                for reg in self.router.region_map:
                    if reg in clean_url:
                        inferred_region = reg
                        break

            final_region = (
                explicit_region
                or fragment_region
                or query_region
                or inferred_region
                or ""
            )

            return ProxyNode(
                url=clean_url,
                protocol=protocol,
                region=final_region,
                raw_endpoint=endpoint,
            )

        return None

    def add_proxy(
        self, endpoint: Union[str, dict, ProxyNode], region: Optional[str] = None
    ) -> None:
        """向代理池中注册新节点"""
        node = self._parse_endpoint(endpoint, explicit_region=region)
        if not node:
            return
        with self._lock:
            # 避免重复添加相同 URL
            for existing in self._nodes:
                if existing.url == node.url:
                    if region:
                        existing.region = region
                    return
            self._nodes.append(node)

    def remove_proxy(self, proxy: str) -> bool:
        """从代理池中移除节点"""
        with self._lock:
            for i, n in enumerate(self._nodes):
                if n.url == proxy or proxy in n.url:
                    self._nodes.pop(i)
                    return True
        return False

    def get_proxy(self, city: str = "") -> Optional[str]:
        """获取可用代理。
        若启用地域就近路由且传入目标城市，优先匹配该城市所属大区的健康代理；
        若大区池无可用代理或未指定城市，平滑回退到全局健康代理；
        若全部节点熔断或代理池未启用，返回 None。
        """
        if not self.enabled:
            return None

        with self._lock:
            if not self._nodes:
                return None

            # 1. 尝试地域就近匹配
            if self.geo_affinity and city:
                target_region = self.router.get_region_for_city(city)
                if target_region:
                    regional_candidates = [
                        n for n in self._nodes if n.is_healthy and n.region == target_region
                    ]
                    if regional_candidates:
                        # 负载均衡：选择最久未用且失败计数最少的节点 (LRU + Least Failure)
                        picked = min(
                            regional_candidates,
                            key=lambda n: (n.last_used_at, n.failure_count),
                        )
                        picked.last_used_at = time.time()
                        return picked.url

            # 2. 全局健康池回退
            global_candidates = [n for n in self._nodes if n.is_healthy]
            if global_candidates:
                picked = min(
                    global_candidates,
                    key=lambda n: (n.last_used_at, n.failure_count),
                )
                picked.last_used_at = time.time()
                return picked.url

            # 3. 无健康节点可用
            return None

    def _find_node_locked(self, proxy: str) -> Optional[ProxyNode]:
        """内部按字符串或 URL 查找节点（需在持有锁状态下调用）"""
        if not proxy:
            return None
        norm_target = proxy.strip().rstrip("/")
        # 1. 精确匹配
        for n in self._nodes:
            if n.url == proxy or n.url.rstrip("/") == norm_target:
                return n
        # 2. 包含/子串匹配（如去除 schema 或带参数）
        for n in self._nodes:
            if norm_target in n.url or n.url in norm_target:
                return n
        return None

    def report_failure(self, proxy: str) -> None:
        """报告代理请求失败。
        连续失败次数递增，达到熔断阈值（默认 3 次）时自动熔断隔离。
        """
        with self._lock:
            node = self._find_node_locked(proxy)
            if not node:
                return
            node.failure_count += 1
            node.last_failed_at = time.time()
            if node.failure_count >= self.max_failures and not node.is_circuit_broken:
                node.is_circuit_broken = True
                logger.warning(
                    "代理节点 [%s] 连续失败 %d 次已触发熔断隔离 (大区: %s)",
                    node.url,
                    node.failure_count,
                    node.region or "通用",
                )

    def report_success(self, proxy: str) -> None:
        """报告代理请求成功。
        重置失败计数，解除熔断状态，恢复健康。
        """
        with self._lock:
            node = self._find_node_locked(proxy)
            if not node:
                return
            node.failure_count = 0
            node.is_circuit_broken = False
            node.success_count += 1
            node.last_success_at = time.time()

    def get_stats(self) -> dict:
        """返回代理池大盘全景指标：总代理数、各地域分布、活跃健康数、熔断数"""
        with self._lock:
            total = len(self._nodes)
            healthy = sum(1 for n in self._nodes if n.is_healthy)
            circuit_broken = sum(1 for n in self._nodes if n.is_circuit_broken)

            regional_distribution: Dict[str, int] = {}
            healthy_by_region: Dict[str, int] = {}
            broken_by_region: Dict[str, int] = {}

            for n in self._nodes:
                reg = n.region or "未指定"
                regional_distribution[reg] = regional_distribution.get(reg, 0) + 1
                if n.is_healthy:
                    healthy_by_region[reg] = healthy_by_region.get(reg, 0) + 1
                if n.is_circuit_broken:
                    broken_by_region[reg] = broken_by_region.get(reg, 0) + 1

            return {
                "total": total,
                "healthy": healthy,
                "circuit_broken": circuit_broken,
                "regions": regional_distribution,
                "regional_distribution": regional_distribution,
                "healthy_by_region": healthy_by_region,
                "broken_by_region": broken_by_region,
                "enabled": self.enabled,
                "geo_affinity": self.geo_affinity,
                "provider": self.provider,
            }

    def reset(self) -> None:
        """重置所有节点的失败计数与熔断状态"""
        with self._lock:
            for n in self._nodes:
                n.failure_count = 0
                n.is_circuit_broken = False

    def clear(self) -> None:
        """清空代理池"""
        with self._lock:
            self._nodes.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._nodes)


# 全局单例管理器
_default_manager: Optional[ProxyPoolManager] = None


def get_manager(cfg: Optional[dict] = None) -> ProxyPoolManager:
    """获取或初始化全局代理池管理器"""
    global _default_manager
    if _default_manager is None or cfg is not None:
        _default_manager = ProxyPoolManager(cfg=cfg)
    return _default_manager
