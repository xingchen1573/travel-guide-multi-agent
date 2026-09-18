"""src/tools/driving.py + amap_tools.py 的 amap_driving_distance 的单元测试
（纯逻辑，mock requests调用，不联网）。

    python -m pytest tests/test_driving.py -v
"""
from __future__ import annotations

import json

import pytest
import requests

import src.tools.driving as m
from src.tools import amap_tools
from src.workspace import store
from src.workspace.route_validation import route_structure_hash
from src.workspace.schema import RouteDay, RoutePlan, RoutePoi


class _FakeResponse:
    def __init__(self, payload=None, raise_on_json=False):
        self._payload = payload
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("invalid json")
        return self._payload


def _ok_payload(distance="370000", duration="12600"):
    return {"status": "1", "route": {"paths": [{"distance": distance, "duration": duration}]}}


# ---------- driving_distance ----------

def test_driving_distance_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", None)
    diagnostic = {}
    assert m.driving_distance(
        104.05, 30.64, 100.27, 38.94, diagnostic=diagnostic
    ) is None
    assert diagnostic == {"code": "missing_api_key", "detail": ""}


def test_driving_distance_success(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return _FakeResponse(_ok_payload())

    monkeypatch.setattr(m.requests, "get", fake_get)

    result = m.driving_distance(104.05, 30.64, 100.27, 38.94)
    assert result == (370000, 12600)
    # 经纬度应拼成 "lng,lat" 字符串传给origin/destination
    assert captured["params"]["origin"] == "104.05,30.64"
    assert captured["params"]["destination"] == "100.27,38.94"


def test_driving_distance_status_not_ok_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"}))
    diagnostic = {}
    assert m.driving_distance(
        104.05, 30.64, 100.27, 38.94, diagnostic=diagnostic
    ) is None
    assert diagnostic == {"code": "api_error", "detail": "10001: INVALID_USER_KEY"}


def test_driving_distance_retries_once_after_qps_limit(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    responses = iter(
        [
            _FakeResponse(
                {
                    "status": "0",
                    "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                    "infocode": "10021",
                }
            ),
            _FakeResponse(_ok_payload()),
        ]
    )
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: next(responses))
    sleeps = []
    monkeypatch.setattr(m.time, "sleep", sleeps.append)
    diagnostic = {}

    assert m.driving_distance(
        104.05, 30.64, 100.27, 38.94, diagnostic=diagnostic
    ) == (370000, 12600)
    assert sleeps == [1.1]
    assert diagnostic == {
        "code": "",
        "detail": "",
        "attempts": 2,
        "recovered_from": "api_error:10021",
    }


def test_driving_distance_empty_paths_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse({"status": "1", "route": {"paths": []}}))
    assert m.driving_distance(104.05, 30.64, 100.27, 38.94) is None


def test_driving_distance_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse(raise_on_json=True))
    assert m.driving_distance(104.05, 30.64, 100.27, 38.94) is None


def test_driving_distance_non_numeric_fields_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse(_ok_payload(distance="abc", duration="def")))
    assert m.driving_distance(104.05, 30.64, 100.27, 38.94) is None


def test_driving_distance_zero_distance_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse(_ok_payload(distance="0", duration="0")))
    assert m.driving_distance(104.05, 30.64, 100.27, 38.94) is None


def test_driving_distance_network_error_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")

    def fake_get(*a, **k):
        raise requests.RequestException("connection timeout")

    monkeypatch.setattr(m.requests, "get", fake_get)
    diagnostic = {}
    assert m.driving_distance(
        104.05, 30.64, 100.27, 38.94, diagnostic=diagnostic
    ) is None
    assert diagnostic["code"] == "request_error"
    assert "connection timeout" in diagnostic["detail"]


# ---------- get_cached_or_driving ----------

def test_get_cached_or_driving_cache_miss_then_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "driving_cache.json")
    monkeypatch.setattr(m, "get_cached_or_geocode", lambda topic, poi, city="": (104.05, 30.64) if poi == "A" else (100.27, 38.94))

    call_count = {"n": 0}

    def fake_driving(olng, olat, dlng, dlat):
        call_count["n"] += 1
        return (370000, 12600)

    monkeypatch.setattr(m, "driving_distance", fake_driving)

    first = m.get_cached_or_driving("青甘大环线", "A", "B")
    assert first == (370000, 12600)
    assert call_count["n"] == 1

    # 缓存文件应已写入，key 是无序对（sorted 后 A 在 B 前）
    cache = json.loads((tmp_path / "driving_cache.json").read_text(encoding="utf-8"))
    assert "青甘大环线|A|B" in cache

    # 第二次应命中缓存，不再调 driving_distance
    second = m.get_cached_or_driving("青甘大环线", "A", "B")
    assert second == (370000, 12600)
    assert call_count["n"] == 1


def test_get_cached_or_driving_ab_ba_share_cache(tmp_path, monkeypatch):
    """A->B 和 B->A 应命中同一条缓存（无序对），不重复调 driving API。"""
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "driving_cache.json")
    monkeypatch.setattr(m, "get_cached_or_geocode", lambda topic, poi, city="": (104.05, 30.64) if poi == "A" else (100.27, 38.94))

    call_count = {"n": 0}

    def fake_driving(olng, olat, dlng, dlat):
        call_count["n"] += 1
        return (370000, 12600)

    monkeypatch.setattr(m, "driving_distance", fake_driving)

    # A->B 先填缓存
    m.get_cached_or_driving("青甘大环线", "A", "B")
    assert call_count["n"] == 1

    # B->A 应命中同一条缓存（sorted 后 key 都是 "青甘大环线|A|B"），不再调 driving
    m.get_cached_or_driving("青甘大环线", "B", "A")
    assert call_count["n"] == 1


def test_get_cached_or_driving_geocode_failure_returns_none(tmp_path, monkeypatch):
    """任一点坐标查不到时不调 driving API。"""
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "driving_cache.json")
    monkeypatch.setattr(m, "get_cached_or_geocode", lambda topic, poi, city="": None)

    driving_called = {"n": 0}

    def fake_driving(*a, **k):
        driving_called["n"] += 1
        return (370000, 12600)

    monkeypatch.setattr(m, "driving_distance", fake_driving)

    result = m.get_cached_or_driving("青甘大环线", "不存在的A", "B")
    assert result is None
    assert driving_called["n"] == 0
    # 失败不应写入缓存
    assert not (tmp_path / "driving_cache.json").exists()


def test_get_cached_or_driving_failure_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "driving_cache.json")
    monkeypatch.setattr(m, "get_cached_or_geocode", lambda topic, poi, city="": (104.05, 30.64) if poi == "A" else (100.27, 38.94))
    monkeypatch.setattr(m, "driving_distance", lambda *a, **k: None)

    result = m.get_cached_or_driving("青甘大环线", "A", "B")
    assert result is None
    # 失败不应写入缓存
    assert not (tmp_path / "driving_cache.json").exists()


def test_canonical_coordinates_ignore_legacy_name_only_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "driving_cache.json")
    (tmp_path / "driving_cache.json").write_text(
        json.dumps(
            {
                "青甘大环线|七彩丹霞|军马场": {
                    "distance_m": 101000,
                    "duration_s": 6120,
                    "cached_at": "2026-09-01",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = {"n": 0}

    def canonical_measure(*args):
        calls["n"] += 1
        return (188900, 11844)

    monkeypatch.setattr(m, "driving_distance", canonical_measure)

    result = m.get_cached_or_driving_coordinates(
        "青甘大环线",
        "七彩丹霞",
        (100.062804, 38.97201),
        "军马场",
        (101.364549, 38.063343),
    )

    assert result == (188900, 11844)
    assert calls["n"] == 1
    cache = json.loads((tmp_path / "driving_cache.json").read_text(encoding="utf-8"))
    saved = cache["青甘大环线|七彩丹霞|军马场"]
    assert saved["distance_m"] == 188900
    assert saved["endpoint_coordinates"] == [
        ["七彩丹霞", 100.062804, 38.97201],
        ["军马场", 101.364549, 38.063343],
    ]


# ---------- amap_driving_distance @tool ----------

def _invoke_tool(tool_list, name, args):
    """从工具列表里找到指定工具并调用（@tool 对象可直接当函数调）。"""
    for t in tool_list:
        if t.name == name:
            return t.invoke(args)
    raise AssertionError(f"tool {name} not found in {[t.name for t in tool_list]}")


def _route_entity(name, provider_name, provider_id, lng, lat):
    return {
        "original_name": name,
        "query_name": provider_name,
        "status": "resolved",
        "candidates": [{"provider_id": provider_id, "raw_provider_response": "不应下投"}],
        "selected": {
            "provider_id": provider_id,
            "provider_name": provider_name,
            "province": "甘肃省",
            "city": "张掖市",
            "district": "山丹县",
            "lng": lng,
            "lat": lat,
        },
    }


def _seed_route_map(
    tmp_path,
    monkeypatch,
    *,
    stale=False,
    transport_mode="driving",
    leg_status="passed",
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(amap_tools, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "driving_cache.json")
    project_id = "route-map-reuse"
    store.init_project(project_id)
    route_plan = RoutePlan(
        days=[
            RouteDay(
                day=7,
                city="甘肃",
                pois=[
                    RoutePoi(name="七彩丹霞"),
                    RoutePoi(name="军马场"),
                    RoutePoi(name="山丹"),
                ],
            )
        ]
    )
    store.write_route_plan(project_id, "route_agent", route_plan)
    route_plan_raw = store.read_workspace(project_id, "route_plan.json")
    report = {
        "schema_version": "route-map-validation-v1",
        "route_plan_structure_hash": (
            "stale-route-hash" if stale else route_structure_hash(route_plan_raw)
        ),
        "status": "passed",
        "days": [
            {
                "day": 7,
                "entities": [
                    _route_entity(
                        "七彩丹霞", "张掖七彩丹霞景区", "B0FFF63UG9", 100.062804, 38.97201
                    ),
                    _route_entity(
                        "军马场", "山丹皇家马场景区", "B0FFH2K8TM", 101.364549, 38.063343
                    ),
                    _route_entity("山丹", "山丹县", "B03A80HU9I", 101.088611, 38.7846),
                ],
                "legs": [
                    {
                        "from": "七彩丹霞",
                        "to": "军马场",
                        "provider_from": "张掖七彩丹霞景区",
                        "provider_to": "山丹皇家马场景区",
                        "distance_km": 188.9,
                        "duration_h": 3.29,
                        "transport_mode": transport_mode,
                        "status": leg_status,
                    },
                    {
                        "from": "军马场",
                        "to": "山丹",
                        "provider_from": "山丹皇家马场景区",
                        "provider_to": "山丹县",
                        "distance_km": 92.5,
                        "duration_h": 1.6,
                        "transport_mode": "driving",
                        "status": "passed",
                    },
                ],
                "totals": {"distance_km": 281.4, "duration_h": 4.89},
            }
        ],
    }
    store._write_json(store.workspace_dir(project_id) / "route_map_validation.json", report)
    return project_id


def test_amap_driving_distance_tool_no_key_returns_hint(monkeypatch):
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", None)
    tools = amap_tools.make_amap_tools(topic="青甘大环线")

    out = _invoke_tool(tools, "amap_driving_distance", {"poi_a": "莫高窟", "poi_b": "张掖七彩丹霞"})
    assert "未配置" in out or "缺少" in out
    assert "自行判断" in out


def test_amap_driving_distance_tool_geocode_failure_returns_hint(monkeypatch):
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "get_cached_or_geocode", lambda topic, poi, city="": None)

    tools = amap_tools.make_amap_tools(topic="青甘大环线")
    out = _invoke_tool(tools, "amap_driving_distance", {"poi_a": "不存在的A", "poi_b": "B"})
    assert "无法获取" in out
    assert "自行判断" in out


def test_amap_driving_distance_tool_success_formats_output(monkeypatch):
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "get_cached_or_geocode", lambda topic, poi, city="": (104.05, 30.64) if poi == "莫高窟" else (100.27, 38.94))
    monkeypatch.setattr(m, "driving_distance", lambda *a, **k: (370000, 12600))
    # 缓存隔离，避免跨测试串用
    monkeypatch.setattr(m, "_CACHE_PATH", __import__("pathlib").Path("/tmp/_test_driving_cache.json"))

    tools = amap_tools.make_amap_tools(topic="青甘大环线")
    out = _invoke_tool(tools, "amap_driving_distance", {"poi_a": "莫高窟", "poi_b": "张掖七彩丹霞"})
    # 370000m = 370km, 12600s = 3.5h
    assert "370 公里" in out
    assert "3.5 小时" in out
    assert "高德" in out


def test_amap_driving_distance_reuses_persisted_route_leg_without_provider(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch)
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", None)
    monkeypatch.setattr(
        m,
        "get_cached_or_geocode",
        lambda *args, **kwargs: pytest.fail("相邻路线段不应重新geocode"),
    )
    monkeypatch.setattr(
        m,
        "get_cached_or_driving_coordinates",
        lambda *args, **kwargs: pytest.fail("相邻路线段不应重新请求driving"),
    )

    tools = amap_tools.make_amap_tools(
        topic="青甘大环线", project_id=project_id, log_scope="lifestyle_agent"
    )
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "七彩丹霞", "poi_b": "军马场", "day": 7},
    )

    assert "188.9 公里" in out
    assert "3.29 小时" in out
    log_path = tmp_path / project_id / "agents" / "lifestyle_agent" / "amap_evidence_log.jsonl"
    evidence = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert evidence["source"] == "route_map_validation"
    assert evidence["route_map_day"] == 7
    assert not (
        tmp_path / project_id / "agents" / "lifestyle_agent" / "provider_calls.jsonl"
    ).exists()


def test_amap_driving_distance_reuses_persisted_taxi_leg(tmp_path, monkeypatch):
    project_id = _seed_route_map(tmp_path, monkeypatch, transport_mode="taxi")
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", None)
    monkeypatch.setattr(
        m,
        "get_cached_or_driving_coordinates",
        lambda *args, **kwargs: pytest.fail("已核验taxi路段不应重复请求driving"),
    )

    tools = amap_tools.make_amap_tools(
        topic="青甘大环线", project_id=project_id, log_scope="lifestyle_agent"
    )
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "七彩丹霞", "poi_b": "军马场", "day": 7},
    )

    assert "188.9 公里" in out and "3.29 小时" in out


def test_amap_driving_distance_reuses_military_farm_to_shandan_without_zero(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch)
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", None)
    monkeypatch.setattr(
        m,
        "get_cached_or_geocode",
        lambda *args, **kwargs: pytest.fail("已核验相邻路段不应重新geocode"),
    )
    monkeypatch.setattr(
        m,
        "get_cached_or_driving_coordinates",
        lambda *args, **kwargs: pytest.fail("已核验相邻路段不应重新请求driving"),
    )

    tools = amap_tools.make_amap_tools(
        topic="青甘大环线", project_id=project_id, log_scope="lifestyle_agent"
    )
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "军马场", "poi_b": "山丹", "day": 7},
    )

    assert "92.5 公里" in out and "1.6 小时" in out
    assert "0 公里" not in out and "0 小时" not in out
    assert not (
        tmp_path / project_id / "agents" / "lifestyle_agent" / "provider_calls.jsonl"
    ).exists()


def test_amap_driving_distance_rejects_anomalous_leg_without_provider(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch, leg_status="anomaly")
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(
        m,
        "get_cached_or_geocode",
        lambda *args, **kwargs: pytest.fail("异常路段不得重新geocode"),
    )
    monkeypatch.setattr(
        m,
        "get_cached_or_driving_coordinates",
        lambda *args, **kwargs: pytest.fail("异常路段不得重新请求driving"),
    )

    tools = amap_tools.make_amap_tools(
        topic="青甘大环线", project_id=project_id, log_scope="lifestyle_agent"
    )
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "七彩丹霞", "poi_b": "军马场", "day": 7},
    )

    assert "没有可复用的驾车测量" in out
    assert "188.9" not in out and "3.29" not in out
    assert "不要写精确距离" in out
    assert not (
        tmp_path / project_id / "agents" / "lifestyle_agent" / "provider_calls.jsonl"
    ).exists()


def test_amap_driving_distance_geocodes_only_new_target_from_route_anchor(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch)
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    geocoded = []
    measured = {}

    def fake_geocode(topic, poi, city=""):
        geocoded.append((topic, poi, city))
        return (101.2, 38.5)

    def fake_measure(topic, poi_a, origin, poi_b, destination):
        measured.update(
            {
                "topic": topic,
                "poi_a": poi_a,
                "origin": origin,
                "poi_b": poi_b,
                "destination": destination,
            }
        )
        return (25000, 1800)

    monkeypatch.setattr(m, "get_cached_or_geocode", fake_geocode)
    monkeypatch.setattr(m, "get_cached_or_driving_coordinates", fake_measure)

    tools = amap_tools.make_amap_tools(
        topic="青甘大环线", project_id=project_id, log_scope="lifestyle_agent"
    )
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "军马场", "poi_b": "山丹酒店", "city": "张掖", "day": 7},
    )

    assert geocoded == [("青甘大环线", "山丹酒店", "张掖")]
    assert measured["origin"] == (101.364549, 38.063343)
    assert measured["destination"] == (101.2, 38.5)
    assert "25 公里" in out and "0.5 小时" in out


def test_amap_driving_distance_uses_canonical_coordinates_for_nonadjacent_route_points(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch)
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    measured = []
    monkeypatch.setattr(
        m,
        "get_cached_or_geocode",
        lambda *args, **kwargs: pytest.fail("正式路线点不得重新geocode"),
    )

    def fake_measure(topic, poi_a, origin, poi_b, destination):
        measured.append((topic, poi_a, origin, poi_b, destination))
        return (210000, 14400)

    monkeypatch.setattr(m, "get_cached_or_driving_coordinates", fake_measure)
    tools = amap_tools.make_amap_tools(topic="青甘大环线", project_id=project_id)
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "七彩丹霞", "poi_b": "山丹", "day": 7},
    )

    assert measured == [
        (
            "青甘大环线",
            "七彩丹霞",
            (100.062804, 38.97201),
            "山丹",
            (101.088611, 38.7846),
        )
    ]
    assert "210 公里" in out and "4 小时" in out


def test_amap_driving_distance_rejects_stale_route_map_instead_of_guessing(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch, stale=True)
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(
        m,
        "get_cached_or_driving",
        lambda *args, **kwargs: pytest.fail("过期Route Map不得回退为自由文本猜测"),
    )

    tools = amap_tools.make_amap_tools(
        topic="青甘大环线", project_id=project_id, log_scope="lifestyle_agent"
    )
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "军马场", "poi_b": "山丹", "day": 7},
    )

    assert "对应旧路线结构" in out
    assert "不要写精确距离" in out
    assert not (
        tmp_path / project_id / "agents" / "lifestyle_agent" / "provider_calls.jsonl"
    ).exists()


def test_amap_driving_distance_allows_new_target_containing_route_name(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch)
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    geocoded = []

    def fake_geocode(topic, poi, city=""):
        geocoded.append((topic, poi, city))
        return (101.2, 38.5)

    monkeypatch.setattr(m, "get_cached_or_geocode", fake_geocode)
    monkeypatch.setattr(
        m,
        "get_cached_or_driving_coordinates",
        lambda *args, **kwargs: (12000, 900),
    )

    tools = amap_tools.make_amap_tools(topic="青甘大环线", project_id=project_id)
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "军马场", "poi_b": "军马场附近酒店", "day": 7},
    )

    assert geocoded == [("青甘大环线", "军马场附近酒店", "")]
    assert "12 公里" in out and "0.25 小时" in out


def test_route_map_source_rejects_route_entity_from_another_day(monkeypatch):
    route_plan = {
        "days": [
            {"day": 1, "city": "成都", "area": "", "pois": [{"name": "武侯祠"}]},
            {"day": 2, "city": "成都", "area": "", "pois": [{"name": "杜甫草堂"}]},
        ]
    }
    report = {
        "route_plan_structure_hash": route_structure_hash(route_plan),
        "days": [
            {
                "day": 1,
                "entities": [_route_entity("武侯祠", "成都武侯祠博物馆", "wuhou", 104.04, 30.64)],
                "legs": [],
            },
            {
                "day": 2,
                "entities": [_route_entity("杜甫草堂", "成都杜甫草堂博物馆", "dufu", 104.03, 30.66)],
                "legs": [],
            },
        ],
    }

    monkeypatch.setattr(
        store,
        "read_workspace",
        lambda _project_id, path: route_plan if path == "route_plan.json" else report,
    )

    result = amap_tools._route_map_distance_source(
        "project", "杜甫草堂", "春熙路酒店", 1
    )

    assert result == {"status": "route_entity_day_mismatch"}


def test_route_map_source_requires_day_for_duplicate_route_entity(monkeypatch):
    route_plan = {
        "days": [
            {"day": 1, "city": "成都", "area": "", "pois": [{"name": "老街"}]},
            {"day": 2, "city": "成都", "area": "", "pois": [{"name": "老街"}]},
        ]
    }
    report = {
        "route_plan_structure_hash": route_structure_hash(route_plan),
        "days": [
            {"day": 1, "entities": [_route_entity("老街", "甲老街", "a", 104.0, 30.6)], "legs": []},
            {"day": 2, "entities": [_route_entity("老街", "乙老街", "b", 105.0, 31.6)], "legs": []},
        ],
    }
    monkeypatch.setattr(
        store,
        "read_workspace",
        lambda _project_id, path: route_plan if path == "route_plan.json" else report,
    )

    result = amap_tools._route_map_distance_source("project", "老街", "酒店", None)

    assert result == {"status": "route_entity_ambiguous"}


def test_amap_driving_distance_preserves_legacy_format_for_non_route_places(
    tmp_path, monkeypatch
):
    project_id = _seed_route_map(tmp_path, monkeypatch)
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "fake-key")
    calls = []

    def fake_legacy(topic, poi_a, poi_b, city=""):
        calls.append((topic, poi_a, poi_b, city))
        return (12345, 4567)

    monkeypatch.setattr(m, "get_cached_or_driving", fake_legacy)
    tools = amap_tools.make_amap_tools(topic="青甘大环线", project_id=project_id)
    out = _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "春熙路酒店", "poi_b": "玉林餐厅", "city": "成都"},
    )

    assert calls == [("青甘大环线", "春熙路酒店", "玉林餐厅", "成都")]
    assert "12 公里" in out and "1.3 小时" in out
    assert "12.3 公里" not in out and "1.27 小时" not in out


def test_amap_tool_records_research_channel_when_context_is_bound(monkeypatch):
    from src.materials import research_service

    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", None)
    calls = []
    monkeypatch.setattr(
        research_service,
        "record_external_research",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    tools = amap_tools.make_amap_tools(
        topic="青甘大环线",
        project_id="project-1",
        log_scope="poi_detail_agent/day4_东台吉乃尔湖",
        research_context={
            "actor": "poi_detail_agent",
            "stage": "poi_detail",
            "target_key": "poi_detail:day:4:poi:东台吉乃尔湖",
        },
    )

    _invoke_tool(
        tools,
        "amap_driving_distance",
        {"poi_a": "东台吉乃尔湖", "poi_b": "大柴旦", "city": "海西"},
    )

    assert calls[0][1]["source_kind"] == "amap"
    assert calls[0][1]["status"] == "unavailable"
    assert calls[0][1]["target_key"] == "poi_detail:day:4:poi:东台吉乃尔湖"


def test_make_amap_tools_returns_query_and_driving_by_default():
    """非路线角色默认不暴露实体消歧工具。"""
    tools = amap_tools.make_amap_tools(topic="青甘大环线")
    names = [t.name for t in tools]
    assert "amap_query" in names
    assert "amap_driving_distance" in names
    assert "amap_resolve_route_entity" not in names


def test_make_amap_tools_can_expose_structured_route_entity_resolution(monkeypatch):
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "test-key")
    from src.materials import route_map_check

    monkeypatch.setattr(
        route_map_check,
        "resolve_entity",
        lambda poi_name, city_hint: {
            "original_name": poi_name,
            "query_name": "成都武侯祠博物馆",
            "city_hint": city_hint,
            "status": "resolved",
            "confidence": 0.98,
            "candidates": [{"provider_id": "wuhouci"}],
            "selected": {"provider_id": "wuhouci", "lng": 104.04, "lat": 30.64},
        },
    )
    tools = amap_tools.make_amap_tools(
        topic="成都",
        include_entity_resolution=True,
    )

    result = json.loads(
        _invoke_tool(
            tools,
            "amap_resolve_route_entity",
            {"poi_name": "武侯祠", "city_hint": "成都市"},
        )
    )

    assert result["status"] == "resolved"
    assert result["selected"]["provider_id"] == "wuhouci"


def test_make_amap_tools_topic_defaults_empty_for_realtime():
    """realtime_agent 等不传 topic 时应能正常构造（topic 默认空字符串）。"""
    tools = amap_tools.make_amap_tools()
    names = [t.name for t in tools]
    assert "amap_driving_distance" in names
