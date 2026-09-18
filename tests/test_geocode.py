"""src/tools/geocode.py 的单元测试（纯逻辑，mock requests调用，不联网）。

    python -m pytest tests/test_geocode.py -v
"""
from __future__ import annotations

import json

import pytest
import requests

import src.tools.geocode as m


class _FakeResponse:
    def __init__(self, payload=None, raise_on_json=False):
        self._payload = payload
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("invalid json")
        return self._payload


def test_geocode_poi_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", None)
    assert m.geocode_poi("武侯祠", city="成都") is None


def test_geocode_poi_success(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    # city传入时会先经_resolve_city_scope()规范化，这里跳过该步骤专注测geocode_poi本身
    monkeypatch.setattr(m, "_resolve_city_scope", lambda city_raw: city_raw)

    def fake_get(url, params=None, timeout=None):
        assert params["keywords"] == "武侯祠"
        assert params["city"] == "成都"
        return _FakeResponse({"status": "1", "pois": [{"location": "104.05,30.64"}]})

    monkeypatch.setattr(m.requests, "get", fake_get)

    result = m.geocode_poi("武侯祠", city="成都")
    assert result == (104.05, 30.64)


def test_geocode_poi_no_city_scope_skips_city_param(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")

    def fake_get(url, params=None, timeout=None):
        assert "city" not in params
        return _FakeResponse({"status": "1", "pois": [{"location": "104.05,30.64"}]})

    monkeypatch.setattr(m.requests, "get", fake_get)

    result = m.geocode_poi("武侯祠")
    assert result == (104.05, 30.64)


def test_geocode_poi_empty_result_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse({"status": "1", "pois": []}))
    assert m.geocode_poi("不存在的地名") is None


def test_geocode_poi_status_not_ok_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse({"status": "0", "info": "INVALID_USER_KEY"}))
    assert m.geocode_poi("武侯祠") is None


def test_geocode_poi_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse(raise_on_json=True))
    assert m.geocode_poi("武侯祠") is None


def test_geocode_poi_malformed_location_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(
        m.requests, "get", lambda *a, **k: _FakeResponse({"status": "1", "pois": [{"location": "bad-format"}]})
    )
    assert m.geocode_poi("武侯祠") is None


def test_geocode_poi_network_error_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")

    def fake_get(*a, **k):
        raise requests.RequestException("connection timeout")

    monkeypatch.setattr(m.requests, "get", fake_get)
    assert m.geocode_poi("武侯祠") is None


def test_resolve_city_scope_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", None)
    assert m._resolve_city_scope("海西冷湖") == ""


def test_resolve_city_scope_empty_input_returns_empty(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    assert m._resolve_city_scope("") == ""


def test_resolve_city_scope_splits_and_tries_tokens_in_order(tmp_path, monkeypatch):
    """多段字符串按分隔符拆分，逐段尝试直到解析出非空city。"""
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")

    calls = []

    def fake_get(url, params=None, timeout=None):
        token = params["address"]
        calls.append(token)
        if token == "海西":
            return _FakeResponse({"status": "1", "geocodes": [{"city": "海西蒙古族藏族自治州"}]})
        return _FakeResponse({"status": "1", "geocodes": []})

    monkeypatch.setattr(m.requests, "get", fake_get)

    result = m._resolve_city_scope("海西→酒泉敦煌")
    assert result == "海西蒙古族藏族自治州"
    assert calls == ["海西"]


def test_resolve_city_scope_falls_back_to_next_token(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")

    def fake_get(url, params=None, timeout=None):
        token = params["address"]
        if token == "阿克塞":
            return _FakeResponse({"status": "1", "geocodes": [{"city": "酒泉市"}]})
        return _FakeResponse({"status": "1", "geocodes": []})

    monkeypatch.setattr(m.requests, "get", fake_get)

    result = m._resolve_city_scope("酒泉敦煌→阿克塞")
    assert result == "酒泉市"


def test_resolve_city_scope_uses_province_fallback_when_city_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")
    monkeypatch.setattr(
        m.requests, "get",
        lambda *a, **k: _FakeResponse({"status": "1", "geocodes": [{"city": [], "province": "青海省"}]}),
    )
    assert m._resolve_city_scope("某地") == "青海省"


def test_resolve_city_scope_all_tokens_fail_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse({"status": "0"}))
    assert m._resolve_city_scope("不存在的地名") == ""


def test_resolve_city_scope_cache_hit_skips_network(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")

    call_count = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse({"status": "1", "geocodes": [{"city": "酒泉市"}]})

    monkeypatch.setattr(m.requests, "get", fake_get)

    first = m._resolve_city_scope("酒泉")
    assert first == "酒泉市"
    assert call_count["n"] == 1

    second = m._resolve_city_scope("酒泉")
    assert second == "酒泉市"
    assert call_count["n"] == 1


def test_get_cached_or_geocode_cache_miss_then_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")

    call_count = {"n": 0}

    def fake_geocode_poi(poi_name, city=""):
        call_count["n"] += 1
        return (104.05, 30.64)

    monkeypatch.setattr(m, "geocode_poi", fake_geocode_poi)

    first = m.get_cached_or_geocode("成都", "武侯祠", city="成都")
    assert first == (104.05, 30.64)
    assert call_count["n"] == 1

    # 缓存文件应已写入
    cache = json.loads((tmp_path / "geocode_cache.json").read_text(encoding="utf-8"))
    assert "成都|武侯祠" in cache

    # 第二次调用应命中缓存，不再调用geocode_poi
    second = m.get_cached_or_geocode("成都", "武侯祠", city="成都")
    assert second == (104.05, 30.64)
    assert call_count["n"] == 1


def test_get_cached_or_geocode_failure_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")
    monkeypatch.setattr(m, "geocode_poi", lambda poi_name, city="": None)

    result = m.get_cached_or_geocode("成都", "不存在的地名")
    assert result is None
    # 失败不应写入缓存文件（甚至不创建缓存文件）
    assert not (tmp_path / "geocode_cache.json").exists()


def test_get_cached_or_geocode_different_topics_isolated(tmp_path, monkeypatch):
    """同名POI在不同topic下应该是独立的缓存key，不会互相串用坐标。"""
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "geocode_cache.json")

    responses = {"成都": (104.05, 30.64), "北京": (116.4, 39.9)}
    monkeypatch.setattr(m, "geocode_poi", lambda poi_name, city="": responses.get(city))

    chengdu_result = m.get_cached_or_geocode("成都", "人民公园", city="成都")
    beijing_result = m.get_cached_or_geocode("北京", "人民公园", city="北京")

    assert chengdu_result == (104.05, 30.64)
    assert beijing_result == (116.4, 39.9)
