"""高德驾车路线规划（两点 POI 名称 -> 驾车距离/时长），供 route_agent 核实单天通勤合理性。

跟src/tools/geocode.py同一种"没有真实key时优雅降级"模式：未配置AMAP_GEOCODE_API_KEY时
直接返回None，调用方（amap_tools.py的amap_driving_distance @tool）据此返回提示文字，
不阻塞agent流程。

跟geocode.py同样是纯函数、不是LangChain @tool——@tool包装在amap_tools.py里，这里只做
REST调用+缓存。职责跟geocode.py对称：geocode核"单点坐标"，driving核"两点驾车距离"。

缓存文件data/material_library/driving_cache.json跟geocode_cache.json分开存（value结构不同：
坐标是{lng,lat} vs 距离是{distance_m,duration_s}；key维度不同：单点 vs 两点对），但同样
跨project全局共享——同一条"成都|武侯祠|杜甫草堂"会被多个project反复命中，不重复消耗
API额度，跟素材库"跨project共享"的既有设计精神一致。

缓存key用无序对（sorted([poi_a, poi_b])），让A->B和B->A命中同一条记录——高德driving
接口AB和BA的距离/时长基本对称，没必要查两次。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from src.config import AMAP_GEOCODE_API_KEY, MATERIAL_LIBRARY_DIR
from src.tools.geocode import get_cached_or_geocode

_DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"
_CACHE_PATH = MATERIAL_LIBRARY_DIR / "driving_cache.json"


def driving_distance(
    origin_lng: float,
    origin_lat: float,
    dest_lng: float,
    dest_lat: float,
    *,
    diagnostic: Optional[dict] = None,
) -> Optional[tuple[int, int]]:
    """调高德Web服务API驾车路线规划接口，返回(distance_meters, duration_seconds)。

    未配置AMAP_GEOCODE_API_KEY、查询无结果、返回格式异常、网络请求失败时均返回None
    （不抛出异常）——agent流程容错优先，不能因为一次距离查询失败中断整个路线规划。
    """
    def fail(code: str, detail: str = "") -> None:
        if diagnostic is not None:
            diagnostic.update({"code": code, "detail": detail})

    if not AMAP_GEOCODE_API_KEY:
        fail("missing_api_key")
        return None

    params = {
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "key": AMAP_GEOCODE_API_KEY,
        "strategy": 2,  # 2=距离最短优先（跟"通勤合理性校验"目的最贴合）
    }

    recovered_from_qps = False
    for attempt in range(2):
        try:
            resp = requests.get(_DRIVING_URL, params=params, timeout=10)
            data = resp.json()
        except requests.RequestException as exc:
            fail("request_error", f"{type(exc).__name__}: {exc}")
            return None
        except ValueError as exc:
            fail("invalid_json", str(exc))
            return None

        if data.get("status") == "1":
            break
        infocode = str(data.get("infocode") or "")
        detail = ": ".join(
            part
            for part in (infocode, str(data.get("info") or ""))
            if part
        )
        if infocode == "10021" and attempt == 0:
            recovered_from_qps = True
            time.sleep(1.1)
            continue
        fail("api_error", detail)
        return None
    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        fail("no_route")
        return None

    try:
        distance_m = int(paths[0].get("distance", 0))
        duration_s = int(paths[0].get("duration", 0))
    except (ValueError, TypeError) as exc:
        fail("invalid_metrics", str(exc))
        return None

    if distance_m <= 0 or duration_s <= 0:
        fail("non_positive_metrics", f"distance={distance_m},duration={duration_s}")
        return None
    if diagnostic is not None:
        diagnostic.update(
            {
                "code": "",
                "detail": "",
                "attempts": 2 if recovered_from_qps else 1,
                "recovered_from": "api_error:10021" if recovered_from_qps else "",
            }
        )
    return distance_m, duration_s


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


def _driving_cache_key(topic: str, poi_a: str, poi_b: str) -> str:
    a, b = sorted([poi_a, poi_b])
    return f"{topic}|{a}|{b}"


def _endpoint_coordinates(
    poi_a: str,
    origin: tuple[float, float],
    poi_b: str,
    destination: tuple[float, float],
) -> list[list[object]]:
    """Return a direction-independent coordinate signature for a named pair."""
    return [
        [name, round(float(coordinates[0]), 6), round(float(coordinates[1]), 6)]
        for name, coordinates in sorted(
            [(poi_a, origin), (poi_b, destination)], key=lambda item: item[0]
        )
    ]


def get_cached_or_driving_coordinates(
    topic: str,
    poi_a: str,
    origin: tuple[float, float],
    poi_b: str,
    destination: tuple[float, float],
) -> Optional[tuple[int, int]]:
    """Measure canonical endpoints without trusting an older name-only cache hit."""
    cache_key = _driving_cache_key(topic, poi_a, poi_b)
    endpoint_coordinates = _endpoint_coordinates(
        poi_a, origin, poi_b, destination
    )
    cache = _load_cache()
    cached = cache.get(cache_key)
    if cached and cached.get("endpoint_coordinates") == endpoint_coordinates:
        return cached["distance_m"], cached["duration_s"]

    result = driving_distance(
        origin[0],
        origin[1],
        destination[0],
        destination[1],
    )
    if result is None:
        return None

    distance_m, duration_s = result
    cache[cache_key] = {
        "distance_m": distance_m,
        "duration_s": duration_s,
        "endpoint_coordinates": endpoint_coordinates,
        "cached_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
    }
    _save_cache(cache)
    return result


def get_cached_or_driving(
    topic: str, poi_a: str, poi_b: str, city: str = ""
) -> Optional[tuple[int, int]]:
    """先查全局缓存，未命中则geocode两点->调driving_distance->写回缓存。

    复用geocode的缓存（get_cached_or_geocode）拿两点经纬度，不重复缓存坐标——driving
    缓存只缓存"两点驾车距离"这一层。任一点坐标查不到时直接return None，不调driving API。

    查询失败（driving_distance返回None）不写入缓存——避免一次网络抖动或未配key导致的
    失败被永久缓存成"查不到"，下次配了key或网络恢复后应该能重新尝试（跟geocode一致）。
    """
    cache_key = _driving_cache_key(topic, poi_a, poi_b)
    cache = _load_cache()

    cached = cache.get(cache_key)
    if cached:
        return cached["distance_m"], cached["duration_s"]

    origin = get_cached_or_geocode(topic, poi_a, city=city)
    dest = get_cached_or_geocode(topic, poi_b, city=city)
    if origin is None or dest is None:
        return None

    return get_cached_or_driving_coordinates(topic, poi_a, origin, poi_b, dest)
