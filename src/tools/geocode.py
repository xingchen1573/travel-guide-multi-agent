"""森林页真实地图用的地理编码（POI名称 -> 经纬度），呈现层专用（见run_generate_site.py）。

跟src/tools/amap_tools.py/web_search_tools.py同一种"没有真实key时优雅降级"模式：
未配置AMAP_GEOCODE_API_KEY时直接返回None，调用方（run_generate_site.py）据此把地图
区域降级为POI列表展示，不阻塞整个网站生成。

这里是纯函数，不是LangChain @tool——amap_tools.py的amap_query是绑给agent在对话里
调用的工具，这个模块是呈现层脚本（run_generate_site.py）直接调用的确定性REST API封装，
职责不同：坐标数据只在呈现层流转，不进入LangGraph状态机/agent产出区。

缓存文件data/material_library/geocode_cache.json是跨project全局共享的（不是project
级别），因为同一个目的地的同名POI会被多个project反复查询，全局缓存能让后续project
直接命中、不重复消耗API额度——跟素材库"跨project共享"的既有设计精神一致。

用的是高德"关键词POI搜索"接口（/v3/place/text），不是"结构化地址编码"接口
（/v3/geocode/geo）——后者对景点类专名不可靠，实测"莫高窟"无论传什么city参数，
地址编码接口都会解析成深圳一个同名小地名，因为它做的是"地址→坐标"结构化匹配，
不是"关键词→POI"搜索。调用方传入的city（如route_plan.json的day.city）常是
"海西→酒泉敦煌"这类多段中转描述，不是规范行政区名，所以先经_resolve_city_scope()
拆分并逐段查询规范化，再作为软性排序偏好（不设citylimit硬过滤——day.city横跨多个
地级行政区的中转日，POI可能不在首段城市的行政边界内，硬过滤会误伤）传给POI搜索。
详见docs/ISSUES.md青甘大环线地图绕行贵阳/广州的诊断记录。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.config import AMAP_GEOCODE_API_KEY, MATERIAL_LIBRARY_DIR

_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
_PLACE_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
_CACHE_PATH = MATERIAL_LIBRARY_DIR / "geocode_cache.json"
_CITY_SCOPE_TOKEN_SPLIT = re.compile(r"[→/、,，·]")


def _resolve_city_scope(city_raw: str) -> str:
    """把day.city这类原始多段中转描述规范化成高德认可的行政区名。

    按分隔符拆分后逐段调/v3/geocode/geo（轻量的城市级查询，不查POI），取第一个
    能解析出非空city（或province兜底）的分段。无key/全部分段解析失败时返回""
    （不抛出异常，交由调用方决定是否带city参数）。结果按city_raw原文缓存，
    避免每次都重新调用。
    """
    if not city_raw:
        return ""
    if not AMAP_GEOCODE_API_KEY:
        return ""

    cache_key = f"__cityhint__|{city_raw}"
    cache = _load_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached.get("resolved", "")

    tokens = [t.strip() for t in _CITY_SCOPE_TOKEN_SPLIT.split(city_raw) if t.strip()]
    resolved = ""
    for token in tokens:
        try:
            resp = requests.get(
                _GEOCODE_URL,
                params={"address": token, "key": AMAP_GEOCODE_API_KEY},
                timeout=10,
            )
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue

        if data.get("status") != "1":
            continue
        geocodes = data.get("geocodes") or []
        if not geocodes:
            continue

        resolved = geocodes[0].get("city") or geocodes[0].get("province") or ""
        if resolved:
            break

    cache[cache_key] = {"resolved": resolved, "cached_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")}
    _save_cache(cache)
    return resolved


def geocode_poi(poi_name: str, city: str = "") -> Optional[tuple[float, float]]:
    """调高德Web服务API POI关键词搜索接口，返回(lng, lat)。

    未配置AMAP_GEOCODE_API_KEY、查询无结果、返回格式异常、网络请求失败时均返回None
    （不抛出异常）——呈现层脚本容错优先，不能因为一个POI查询失败中断整个网站生成。
    """
    if not AMAP_GEOCODE_API_KEY:
        return None

    params = {"keywords": poi_name, "key": AMAP_GEOCODE_API_KEY}
    if city:
        resolved_city = _resolve_city_scope(city)
        if resolved_city:
            params["city"] = resolved_city

    try:
        resp = requests.get(_PLACE_SEARCH_URL, params=params, timeout=10)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    if data.get("status") != "1":
        return None
    pois = data.get("pois") or []
    if not pois:
        return None

    location = pois[0].get("location", "")
    try:
        lng_str, lat_str = location.split(",")
        return float(lng_str), float(lat_str)
    except (ValueError, AttributeError):
        return None


def search_poi_candidates(poi_name: str, city: str = "") -> list[dict[str, Any]]:
    """Return structured Amap candidates instead of silently selecting the first POI."""
    if not AMAP_GEOCODE_API_KEY:
        return []
    params: dict[str, Any] = {
        "keywords": poi_name,
        "key": AMAP_GEOCODE_API_KEY,
        "offset": 10,
        "page": 1,
        "extensions": "base",
    }
    resolved_city = _resolve_city_scope(city) if city else ""
    if resolved_city:
        params["city"] = resolved_city
    try:
        resp = requests.get(_PLACE_SEARCH_URL, params=params, timeout=10)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []
    if data.get("status") != "1":
        return []
    candidates: list[dict[str, Any]] = []
    for poi in data.get("pois") or []:
        location = str(poi.get("location") or "")
        try:
            lng_raw, lat_raw = location.split(",")
            lng, lat = float(lng_raw), float(lat_raw)
        except (ValueError, AttributeError):
            continue
        candidates.append(
            {
                "provider_id": str(poi.get("id") or ""),
                "provider_name": str(poi.get("name") or ""),
                "type": str(poi.get("type") or ""),
                "address": str(poi.get("address") or ""),
                "province": str(poi.get("pname") or ""),
                "city": str(poi.get("cityname") or ""),
                "district": str(poi.get("adname") or ""),
                "lng": lng,
                "lat": lat,
            }
        )
    return candidates


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_cached_or_geocode(topic: str, poi_name: str, city: str = "") -> Optional[tuple[float, float]]:
    """先查全局缓存，未命中则调geocode_poi()，成功后写回缓存再返回。

    查询失败（geocode_poi返回None）不写入缓存——避免一次网络抖动或未配key导致的
    失败被永久缓存成"查不到"，下次配了key或网络恢复后应该能重新尝试。
    """
    resolved_city = _resolve_city_scope(city) if city else ""
    cache_key = f"{topic}|{resolved_city}|{poi_name}" if resolved_city else f"{topic}|{poi_name}"
    cache = _load_cache()

    cached = cache.get(cache_key)
    if cached:
        return cached["lng"], cached["lat"]

    result = geocode_poi(poi_name, city=city)
    if result is None:
        return None

    lng, lat = result
    cache[cache_key] = {"lng": lng, "lat": lat, "cached_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")}
    _save_cache(cache)
    return result
