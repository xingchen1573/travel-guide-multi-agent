"""src/api/routers/model_config.py的FastAPI TestClient测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import key_store, runtime_config
from src.api.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_override():
    runtime_config.set_model_override(None, "full")
    runtime_config.set_model_override(None, "light")
    yield
    runtime_config.set_model_override(None, "full")
    runtime_config.set_model_override(None, "light")


@pytest.fixture(autouse=True)
def isolated_key_store(tmp_path, monkeypatch):
    monkeypatch.setattr(key_store, "KEY_STORE_PATH", tmp_path / "runtime_config" / "api_keys.json")
    # 避免ensure_migrated()意外用这台机器真实的.env key池去seed
    monkeypatch.setattr("src.config.LLM_API_KEYS", [])
    monkeypatch.setattr("src.config.LLM_API_KEYS_LIGHT", [])
    monkeypatch.setattr("src.config.MIMO_TOKEN_PLAN_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_GPTLOAD_API_KEYS", [])
    monkeypatch.setattr("src.config.GPTLOAD_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_API_KEYS", [])
    monkeypatch.setattr("src.config.CUNAI_API_KEYS", [])
    monkeypatch.setattr("src.config.TAVILY_API_KEYS", [])
    monkeypatch.setattr("src.config.POSTER_IMAGE_API_KEY", None)


def test_get_model_config_returns_default_when_no_override(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "sensenova-v1")
    monkeypatch.setattr("src.config.MIMO_CHAT_MODEL", "mimo-v2.5")
    monkeypatch.setattr("src.config.MIMO_PRO_CHAT_MODEL", "mimo-v2.5-pro")
    key_store.add_key("full", "sk-abcdefgh")

    resp = client.get("/api/model_config")
    assert resp.status_code == 200
    body = resp.json()
    full = body["groups"]["full"]
    assert full["default_model"] == "sensenova-v1"
    assert full["effective_model"] == "sensenova-v1"
    assert full["override_active"] is False
    assert full["key_count"] == 1
    assert "light" in body["groups"]
    assert "mimo" in body["groups"]
    assert "effective_model" not in body["groups"]["mimo"]  # mimo没有模型名切换
    assert body["groups"]["mimo"]["available_models"] == ["mimo-v2.5", "mimo-v2.5-pro"]
    assert "tavily" in body["key_pools"]
    assert "poster" in body["key_pools"]
    assert isinstance(body["other_services"], list)
    assert not any(s["name"] == "Tavily 搜索" for s in body["other_services"])  # 已搬去key_pools
    assert any(s["name"] == "高德MCP" for s in body["other_services"])


def test_get_model_config_exposes_current_gptload_travel_route(monkeypatch):
    """管理页必须展示真正的长攻略主链，不能再把 full/CunAI/MiMo 的
    历史分组标签当成当前 Provider 路由。
    """
    monkeypatch.setattr("src.config.TRAVEL_MODEL_PROVIDER", "gptload")
    monkeypatch.setattr("src.config.GPTLOAD_BASE_URL", "https://gateway.example/gptload/v1")
    monkeypatch.setattr("src.config.GPTLOAD_CHAT_MODEL", "glm-current")
    key_store.add_key("mimo", "sk-gptload", channel="gptload")

    resp = client.get("/api/model_config")

    assert resp.status_code == 200
    body = resp.json()
    primary = body["primary_travel"]
    assert primary == {
        "label": "当前长攻略与结构化核验主链",
        "provider_group": "gptload",
        "provider": "GPT-Load",
        "base_url": "https://gateway.example/gptload/v1",
        "model": "glm-current",
        "usage": "路线、衣食住行、避雷、POI详情、文化、最终整合及结构化核验",
        "configured": True,
        "key_count": 1,
    }
    assert "9个业务agent共用主力模型" not in body["groups"]["full"]["label"]
    assert "非当前长攻略主链" in body["groups"]["cunai"]["usage"]
    assert "非当前长攻略主链" in body["groups"]["mimo"]["usage"]


def test_get_model_config_exposes_current_jiji_travel_route(monkeypatch):
    monkeypatch.setattr("src.config.TRAVEL_MODEL_PROVIDER", "jiji")
    monkeypatch.setattr("src.config.JIJI_BASE_URL", "https://jiji.example/v1")
    monkeypatch.setattr("src.config.JIJI_CHAT_MODEL", "glm-jiji")
    monkeypatch.setattr("src.config.JIJI_API_KEYS", ["sk-jiji"])

    resp = client.get("/api/model_config")

    assert resp.status_code == 200
    primary = resp.json()["primary_travel"]
    assert primary["provider_group"] == "jiji"
    assert primary["provider"] == "jiji.cc"
    assert primary["base_url"] == "https://jiji.example/v1"
    assert primary["model"] == "glm-jiji"
    assert primary["configured"] is True
    assert primary["key_count"] == 1


def test_get_model_config_deduplicates_identical_mimo_model_names(monkeypatch):
    """标准/Pro配成同一个模型时不应向前端返回重复列表，否则 React
    会产生重复 key 警告。
    """
    monkeypatch.setattr("src.config.MIMO_CHAT_MODEL", "same-model")
    monkeypatch.setattr("src.config.MIMO_PRO_CHAT_MODEL", "same-model")

    resp = client.get("/api/model_config")

    assert resp.status_code == 200
    assert resp.json()["groups"]["mimo"]["available_models"] == ["same-model"]


def test_put_model_sets_override_and_get_reflects_it(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "sensenova-v1")

    resp = client.put("/api/model_config/model", json={"model": "deepseek-v4", "group": "full"})
    assert resp.status_code == 200
    assert resp.json()["effective_model"] == "deepseek-v4"

    resp2 = client.get("/api/model_config")
    assert resp2.json()["groups"]["full"]["effective_model"] == "deepseek-v4"
    assert resp2.json()["groups"]["full"]["override_active"] is True


def test_put_model_none_clears_override(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "sensenova-v1")
    client.put("/api/model_config/model", json={"model": "deepseek-v4", "group": "full"})

    resp = client.put("/api/model_config/model", json={"model": None, "group": "full"})
    assert resp.status_code == 200
    assert resp.json()["effective_model"] == "sensenova-v1"


def test_put_model_defaults_to_full_group_when_omitted(monkeypatch):
    """group字段有默认值"full"，向后兼容旧的请求体（不带group字段）。"""
    monkeypatch.setattr("src.config.LLM_MODEL", "sensenova-v1")

    resp = client.put("/api/model_config/model", json={"model": "deepseek-v4"})
    assert resp.status_code == 200
    assert resp.json()["group"] == "full"
    assert resp.json()["effective_model"] == "deepseek-v4"


def test_light_group_override_independent_from_full(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "full-default")
    monkeypatch.setattr("src.config.LLM_LIGHT_MODEL", "light-default")

    client.put("/api/model_config/model", json={"model": "light-override", "group": "light"})

    resp = client.get("/api/model_config")
    body = resp.json()
    assert body["groups"]["light"]["effective_model"] == "light-override"
    assert body["groups"]["light"]["override_active"] is True
    assert body["groups"]["full"]["effective_model"] == "full-default"
    assert body["groups"]["full"]["override_active"] is False


# ---------------------------------------------------------------------------
# POST /test：手动可用性检测（2026-07-26新增，见CLAUDE.md已知问题第5条）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


def _make_fake_chat_openai(outcomes: dict):
    """outcomes: api_key -> "ok" 或 Exception实例。构造一个跟ChatOpenAI同名参数签名的
    假类，调用方按真实key拿到对应结果，不同key互不掩盖（跟RotatingChatOpenAI故意不同，
    见model_config.py::test_keys的设计说明）。"""

    class FakeChatOpenAI:
        def __init__(self, base_url, api_key, model, temperature, timeout):
            self.api_key = api_key
            self.model = model

        def invoke(self, prompt):
            outcome = outcomes[self.api_key]
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(outcome)

    return FakeChatOpenAI


def test_test_keys_reports_ok_for_specific_key_index(monkeypatch):
    key_store.add_key("full", "sk-key0000")
    key_store.add_key("full", "sk-key1111")
    monkeypatch.setattr("src.config.LLM_MODEL", "sensenova-v1")
    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI",
        _make_fake_chat_openai({"sk-key0000": "可用", "sk-key1111": "可用"}),
    )

    resp = client.post("/api/model_config/test", json={"group": "full", "key_index": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["group"] == "full"
    assert body["model"] == "sensenova-v1"
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["index"] == 0
    assert result["ok"] is True
    assert result["error"] is None
    assert result["response_snippet"] == "可用"
    assert result["latency_ms"] >= 0
    assert "sk-k" in result["masked"] and "key0000" not in result["masked"]  # 脱敏，不暴露明文


def test_test_keys_reports_failure_with_error_message(monkeypatch):
    key_store.add_key("full", "sk-badkey0")
    monkeypatch.setattr("src.config.LLM_MODEL", "sensenova-v1")
    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI",
        _make_fake_chat_openai({"sk-badkey0": RuntimeError("401 Unauthorized")}),
    )

    resp = client.post("/api/model_config/test", json={"group": "full", "key_index": 0})
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["ok"] is False
    assert "401 Unauthorized" in result["error"]
    assert result["response_snippet"] is None


def test_test_keys_tests_all_keys_when_index_omitted(monkeypatch):
    key_store.add_key("full", "sk-good0000")
    key_store.add_key("full", "sk-bad00000")
    monkeypatch.setattr("src.config.LLM_MODEL", "sensenova-v1")
    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI",
        _make_fake_chat_openai({"sk-good0000": "可用", "sk-bad00000": RuntimeError("限流")}),
    )

    resp = client.post("/api/model_config/test", json={"group": "full"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False


def test_test_keys_uses_light_group_keys_and_model(monkeypatch):
    key_store.add_key("full", "sk-fullkey0")
    key_store.add_key("light", "sk-lightkey")
    monkeypatch.setattr("src.config.LLM_LIGHT_MODEL", "sensenova-lite")
    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI",
        _make_fake_chat_openai({"sk-lightkey": "可用"}),
    )

    resp = client.post("/api/model_config/test", json={"group": "light", "key_index": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "sensenova-lite"
    assert body["results"][0]["ok"] is True


def test_test_keys_out_of_range_index_returns_empty_results():
    key_store.add_key("full", "sk-onlyone0")

    resp = client.post("/api/model_config/test", json={"group": "full", "key_index": 5})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# ---------------------------------------------------------------------------
# Key CRUD：POST/DELETE /{group}/keys（2026-08-01新增，推翻旧"不可编辑"政策）
# ---------------------------------------------------------------------------


def test_add_key_appends_to_group_and_returns_updated_status():
    resp = client.post("/api/model_config/full/keys", json={"key": "sk-newkey1"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["key_count"] == 1
    assert body["keys"][0]["masked"] != "sk-newkey1"  # 脱敏，不回传明文

    resp2 = client.post("/api/model_config/full/keys", json={"key": "sk-newkey2"})
    assert resp2.json()["key_count"] == 2


def test_add_key_rejects_unknown_group():
    resp = client.post("/api/model_config/unknown/keys", json={"key": "sk-x"})
    assert resp.status_code == 400


def test_add_key_mimo_requires_channel():
    resp = client.post("/api/model_config/mimo/keys", json={"key": "sk-x"})
    assert resp.status_code == 400


def test_add_key_mimo_rejects_invalid_channel():
    resp = client.post("/api/model_config/mimo/keys", json={"key": "sk-x", "channel": "bogus"})
    assert resp.status_code == 400


def test_add_key_mimo_with_valid_channel_succeeds():
    resp = client.post("/api/model_config/mimo/keys", json={"key": "tp-x", "channel": "token_plan"})
    assert resp.status_code == 201
    assert resp.json()["key_count"] == 1


def test_add_key_rejects_blank_key():
    resp = client.post("/api/model_config/full/keys", json={"key": "   "})
    assert resp.status_code == 400


def test_delete_key_removes_by_index():
    client.post("/api/model_config/full/keys", json={"key": "sk-key0"})
    client.post("/api/model_config/full/keys", json={"key": "sk-key1"})

    resp = client.delete("/api/model_config/full/keys/0")
    assert resp.status_code == 200
    assert resp.json()["key_count"] == 1


def test_delete_key_out_of_range_returns_404():
    resp = client.delete("/api/model_config/full/keys/5")
    assert resp.status_code == 404


def test_delete_key_rejects_unknown_group():
    resp = client.delete("/api/model_config/unknown/keys/0")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET ""：mimo分组+key_pools（tavily/poster）响应形状
# ---------------------------------------------------------------------------


def test_get_model_config_includes_mimo_group_and_key_pools():
    key_store.add_key("mimo", "tp-k1", channel="token_plan")
    key_store.add_key("tavily", "tvly-k1")
    key_store.add_key("poster", "sk-poster-k1")

    resp = client.get("/api/model_config")
    body = resp.json()

    assert body["groups"]["mimo"]["key_count"] == 1
    assert body["key_pools"]["tavily"]["key_count"] == 1
    assert body["key_pools"]["poster"]["key_count"] == 1
    assert "default_model" not in body["key_pools"]["tavily"]  # 纯key池，没有模型概念


def test_set_model_rejects_mimo_group():
    """mimo没有模型名运行时切换，PUT /model应该明确拒绝而不是静默接受。"""
    resp = client.put("/api/model_config/model", json={"model": "some-model", "group": "mimo"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /test：tavily/poster分组的测试逻辑
# ---------------------------------------------------------------------------


def test_test_keys_tavily_fires_real_search(monkeypatch):
    key_store.add_key("tavily", "tvly-k1")

    class _FakeResponse:
        ok = True
        status_code = 200

    calls = []

    def fake_post(url, json, timeout):
        calls.append(json)
        return _FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)

    resp = client.post("/api/model_config/test", json={"group": "tavily", "key_index": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["ok"] is True
    assert len(calls) == 1
    assert calls[0]["api_key"] == "tvly-k1"


def test_test_keys_poster_does_not_fire_real_generation(monkeypatch):
    key_store.add_key("poster", "sk-poster-k1")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("poster测试不应该发起真实HTTP调用")

    monkeypatch.setattr("requests.post", fail_if_called)

    resp = client.post("/api/model_config/test", json={"group": "poster"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["ok"] is None
    assert "不做自动测试" in body["results"][0]["error"]


def test_test_keys_rejects_unknown_group():
    resp = client.post("/api/model_config/test", json={"group": "unknown"})
    assert resp.status_code == 400
