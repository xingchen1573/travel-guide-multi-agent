"""src/tools/poi_photo.py 的单元测试（纯逻辑，mock requests调用，不联网）。

    python -m pytest tests/test_poi_photo.py -v
"""
from __future__ import annotations

import io
import json

import pytest
import requests
from PIL import Image

import src.tools.poi_photo as m


def _jpeg_bytes(width: int, height: int = 300) -> bytes:
    """现场生成一张真实可解码的JPEG，供_download_as_base64的分辨率校验测试使用。"""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(80, 140, 200)).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload=None, raise_on_json=False, content=b"", headers=None, raise_on_status=False):
        self._payload = payload
        self._raise_on_json = raise_on_json
        self.content = content
        self.headers = headers or {}
        self._raise_on_status = raise_on_status

    def json(self):
        if self._raise_on_json:
            raise ValueError("invalid json")
        return self._payload

    def raise_for_status(self):
        if self._raise_on_status:
            raise requests.RequestException("bad status")


def _ok_payload(names=("都江堰景区",), photo_url="http://store.is.autonavi.com/showpic/abc123"):
    return {
        "status": "1",
        "pois": [{"name": n, "photos": [{"url": photo_url}]} for n in names],
    }


# ---------- _is_relevant_match ----------

def test_is_relevant_match_rejects_substring_false_positive():
    """真实E2E跑测坐实的bug：'正门·西门'查询只返回'西门子助听器'这一个候选，
    '西门'恰好是'西门子'的前缀子串，但语义完全不相关，必须拒绝。"""
    assert m._is_relevant_match("正门·西门", "西门子助听器(南门中心店)") is False


def test_is_relevant_match_accepts_real_matches():
    """真实E2E跑测中确认相关的候选（含分词后共享token、以及候选完整包含查询词
    两种情况）都应通过。"""
    assert m._is_relevant_match("大廨", "杜甫草堂-大廨") is True
    assert m._is_relevant_match("诗史堂", "杜甫草堂-诗史堂") is True
    assert m._is_relevant_match('大门·"汉昭烈庙"匾额与讲解接待处', "汉昭烈庙") is True
    assert m._is_relevant_match("红墙夹道", "红墙夹道") is True


def test_is_relevant_match_rejects_unrelated_candidate():
    assert m._is_relevant_match("工部祠（讲解亦称诗圣祠）", "成都武侯祠博物馆") is False


def test_is_relevant_match_rejects_generic_stopword_only_overlap():
    """真实E2E跑测（青甘大环线）坐实的第二个bug：'景区入口 + 观光车起乘'（张掖七彩
    丹霞的途经点）跟'沙坡头旅游景区'（几百公里外宁夏中卫的另一个景区）仅因共享
    '景区'这个几乎任何景点都会出现的通用词，就通过了朴素的token重叠校验。
    通用词需要从token重叠判断里排除。"""
    assert m._is_relevant_match("景区入口 + 观光车起乘", "沙坡头旅游景区") is False
    assert m._is_relevant_match("夜场入口购票", "国家体育场") is False


def test_is_relevant_match_rejects_short_generic_suffix_token():
    """真实E2E跑测（青甘大环线，紧接上一个bug修复后又坐实的第三个bug）：'望海台站'
    （大柴旦翡翠湖的途经点）跟东莞的公交站'铜雀台站(公交站)'仅因共享'台站'这个
    2字通用后缀token就被判定相关（jieba分词后两者都是"XX+台站"）。停用词表打地鼠
    式列举跟不上，把token重叠的最低长度门槛从2字提到3字才是根治——具体地名标识
    （"汉昭烈""七彩丹霞"）几乎都在3字以上，2字token几乎都是通用后缀/前缀。"""
    assert m._is_relevant_match("望海台站", "铜雀台站(公交站)") is False


def test_is_relevant_match_accepts_poi_overview_level_names():
    """配图粒度降级到POI整体级别后（见run_generate_site.py演变记录），常见的
    完整景区名查询-候选对应能通过：候选名比查询词多了"景区"这类通用后缀，
    靠核心专名（如"莫高窟""嘉峪关"）的token重叠或包含关系通过。"""
    assert m._is_relevant_match("莫高窟", "莫高窟景区") is True
    assert m._is_relevant_match("大柴旦翡翠湖", "大柴旦翡翠湖") is True
    assert m._is_relevant_match("嘉峪关关城", "嘉峪关文物景区") is True


# ---------- search_poi_photo_url ----------

def test_search_poi_photo_url_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", None)
    assert m.search_poi_photo_url("都江堰景区") is None


def test_search_poi_photo_url_rejects_irrelevant_top_candidate(monkeypatch):
    """单个候选（甚至是唯一候选）但跟查询词语义不相关时（模糊匹配误判），应返回
    None，不能因为"高德给的排名第一"就采信——真正的把关在_is_relevant_match。"""
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(
        m.requests, "get",
        lambda *a, **k: _FakeResponse(_ok_payload(
            names=("西门子助听器(南门中心店)",),
            photo_url="http://store.is.autonavi.com/showpic/wrongshop",
        )),
    )
    assert m.search_poi_photo_url("正门·西门") is None


def test_search_poi_photo_url_success(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return _FakeResponse(_ok_payload())

    monkeypatch.setattr(m.requests, "get", fake_get)

    result = m.search_poi_photo_url("都江堰景区")
    assert result == "http://store.is.autonavi.com/showpic/abc123"
    assert captured["params"]["keywords"] == "都江堰景区"
    assert "city" not in captured["params"]


def test_search_poi_photo_url_takes_top_result_even_with_multiple_distinct_candidates(monkeypatch):
    """完整POI名搜索天然会带出很多同名周边结果（停车场/酒店/讲解服务等），不再
    要求"候选唯一"——这是从途经点级别方案迁移过来时刻意放宽的规则（见
    run_generate_site.py"POI真实照片"一节的演变记录），否则几乎所有查询都会
    因为"多候选"被放弃、0命中。首位结果仍需通过_is_relevant_match才采信。"""
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(
        m.requests, "get",
        lambda *a, **k: _FakeResponse(_ok_payload(names=("都江堰景区", "都江堰景区停车场", "都江堰景区讲解服务"))),
    )
    assert m.search_poi_photo_url("都江堰景区") == "http://store.is.autonavi.com/showpic/abc123"


def test_search_poi_photo_url_status_not_ok_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse({"status": "0"}))
    assert m.search_poi_photo_url("都江堰景区") is None


def test_search_poi_photo_url_empty_pois_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse({"status": "1", "pois": []}))
    assert m.search_poi_photo_url("都江堰景区") is None


def test_search_poi_photo_url_no_photos_returns_none(monkeypatch):
    """POI存在但photos字段为空（冷门景点常见情况）应返回None。"""
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(
        m.requests, "get",
        lambda *a, **k: _FakeResponse({"status": "1", "pois": [{"name": "冷门小店", "photos": []}]}),
    )
    assert m.search_poi_photo_url("冷门小店") is None


def test_search_poi_photo_url_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse(raise_on_json=True))
    assert m.search_poi_photo_url("都江堰景区") is None


def test_search_poi_photo_url_network_error_returns_none(monkeypatch):
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    monkeypatch.setattr(m, "_throttle", lambda: None)

    def fake_get(*a, **k):
        raise requests.RequestException("connection timeout")

    monkeypatch.setattr(m.requests, "get", fake_get)
    assert m.search_poi_photo_url("都江堰景区") is None


def test_search_poi_photo_url_throttles_before_request(monkeypatch):
    """每次真实请求前应调用节流函数，避免撞高德QPS限流。"""
    monkeypatch.setattr(m, "AMAP_GEOCODE_API_KEY", "fake-key")
    throttle_called = {"n": 0}
    monkeypatch.setattr(m, "_throttle", lambda: throttle_called.__setitem__("n", throttle_called["n"] + 1))
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResponse(_ok_payload()))

    m.search_poi_photo_url("都江堰景区")
    assert throttle_called["n"] == 1


# ---------- _download_as_base64 ----------

def test_download_as_base64_success(monkeypatch):
    content = _jpeg_bytes(m._MIN_PHOTO_WIDTH_PX)
    monkeypatch.setattr(
        m.requests, "get",
        lambda url, timeout=None: _FakeResponse(content=content, headers={"Content-Type": "image/jpeg"}),
    )
    result = m._download_as_base64("http://example.com/pic.jpg")
    assert result is not None
    encoded, mime = result
    assert mime == "image/jpeg"
    import base64
    assert base64.b64decode(encoded) == content


def test_download_as_base64_network_error_returns_none(monkeypatch):
    def fake_get(*a, **k):
        raise requests.RequestException("timeout")

    monkeypatch.setattr(m.requests, "get", fake_get)
    assert m._download_as_base64("http://example.com/pic.jpg") is None


def test_download_as_base64_bad_status_returns_none(monkeypatch):
    monkeypatch.setattr(
        m.requests, "get",
        lambda url, timeout=None: _FakeResponse(content=b"", raise_on_status=True),
    )
    assert m._download_as_base64("http://example.com/pic.jpg") is None


def test_download_as_base64_non_image_content_type_defaults_to_jpeg(monkeypatch):
    """Content-Type异常（非image/*）时兜底成image/jpeg，不因此丢弃已下载的数据。"""
    content = _jpeg_bytes(m._MIN_PHOTO_WIDTH_PX)
    monkeypatch.setattr(
        m.requests, "get",
        lambda url, timeout=None: _FakeResponse(content=content, headers={"Content-Type": "text/html"}),
    )
    result = m._download_as_base64("http://example.com/pic.jpg")
    assert result is not None
    _, mime = result
    assert mime == "image/jpeg"


def test_download_as_base64_rejects_below_min_width(monkeypatch):
    """高德接口返回的图分辨率异常小（低于_MIN_PHOTO_WIDTH_PX）时应视为不可用，
    跟网络失败走同一条'返回None、不写入缓存'路径。"""
    content = _jpeg_bytes(m._MIN_PHOTO_WIDTH_PX - 1)
    monkeypatch.setattr(
        m.requests, "get",
        lambda url, timeout=None: _FakeResponse(content=content, headers={"Content-Type": "image/jpeg"}),
    )
    assert m._download_as_base64("http://example.com/pic.jpg") is None


def test_download_as_base64_accepts_at_min_width_threshold(monkeypatch):
    content = _jpeg_bytes(m._MIN_PHOTO_WIDTH_PX)
    monkeypatch.setattr(
        m.requests, "get",
        lambda url, timeout=None: _FakeResponse(content=content, headers={"Content-Type": "image/jpeg"}),
    )
    assert m._download_as_base64("http://example.com/pic.jpg") is not None


def test_download_as_base64_rejects_corrupted_image_data(monkeypatch):
    """真实场景里现有16张缓存图基本都是500px典型值，这条测试覆盖'下载到的字节
    根本不是可解码图片'的损坏场景。"""
    monkeypatch.setattr(
        m.requests, "get",
        lambda url, timeout=None: _FakeResponse(content=b"\xff\xd8fake-jpeg-bytes", headers={"Content-Type": "image/jpeg"}),
    )
    assert m._download_as_base64("http://example.com/pic.jpg") is None


# ---------- get_cached_or_poi_overview_photo ----------

def test_get_cached_or_poi_overview_photo_cache_miss_then_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "poi_photo_cache.json")
    monkeypatch.setattr(m, "search_poi_photo_url", lambda name: "http://example.com/pic.jpg")

    call_count = {"n": 0}

    def fake_download(url):
        call_count["n"] += 1
        return "base64data", "image/jpeg"

    monkeypatch.setattr(m, "_download_as_base64", fake_download)

    first = m.get_cached_or_poi_overview_photo("成都3日游", "都江堰景区")
    assert first == "data:image/jpeg;base64,base64data"
    assert call_count["n"] == 1

    cache = json.loads((tmp_path / "poi_photo_cache.json").read_text(encoding="utf-8"))
    assert "成都3日游|都江堰景区" in cache

    second = m.get_cached_or_poi_overview_photo("成都3日游", "都江堰景区")
    assert second == "data:image/jpeg;base64,base64data"
    assert call_count["n"] == 1


def test_get_cached_or_poi_overview_photo_search_failure_returns_none_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "poi_photo_cache.json")
    monkeypatch.setattr(m, "search_poi_photo_url", lambda name: None)

    download_called = {"n": 0}
    monkeypatch.setattr(m, "_download_as_base64", lambda url: download_called.__setitem__("n", 1))

    result = m.get_cached_or_poi_overview_photo("成都3日游", "冷门小店")
    assert result is None
    assert download_called["n"] == 0
    assert not (tmp_path / "poi_photo_cache.json").exists()


def test_get_cached_or_poi_overview_photo_download_failure_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "poi_photo_cache.json")
    monkeypatch.setattr(m, "search_poi_photo_url", lambda name: "http://example.com/pic.jpg")
    monkeypatch.setattr(m, "_download_as_base64", lambda url: None)

    result = m.get_cached_or_poi_overview_photo("成都3日游", "都江堰景区")
    assert result is None
    assert not (tmp_path / "poi_photo_cache.json").exists()


def test_get_cached_or_poi_overview_photo_different_topics_share_global_cache(tmp_path, monkeypatch):
    """跨project（不同topic）同名POI应各自缓存（cache_key含topic），
    但不同topic各自查询一次都能命中同一份全局缓存文件，不冲突。"""
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "poi_photo_cache.json")
    monkeypatch.setattr(m, "search_poi_photo_url", lambda name: "http://example.com/pic.jpg")
    monkeypatch.setattr(m, "_download_as_base64", lambda url: ("base64data", "image/jpeg"))

    m.get_cached_or_poi_overview_photo("成都3日游A", "都江堰景区")
    m.get_cached_or_poi_overview_photo("成都3日游B", "都江堰景区")

    cache = json.loads((tmp_path / "poi_photo_cache.json").read_text(encoding="utf-8"))
    assert "成都3日游A|都江堰景区" in cache
    assert "成都3日游B|都江堰景区" in cache
