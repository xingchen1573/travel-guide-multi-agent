"""src/api/runtime_config.py单元测试：运行时模型覆盖 + key池状态展示
（full/light/mimo/tavily/poster共5个分组，2026-08-01从"只读展示"扩展为
配合key_store CRUD后的状态组装层）。"""
from __future__ import annotations

import pytest

from src.api import key_store, runtime_config


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


def test_get_model_override_defaults_to_none():
    assert runtime_config.get_model_override() is None


def test_set_model_override_and_get_effective_model(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "default-model")
    assert runtime_config.get_effective_model() == "default-model"

    runtime_config.set_model_override("new-model")
    assert runtime_config.get_model_override() == "new-model"
    assert runtime_config.get_effective_model() == "new-model"


def test_set_model_override_none_clears_it(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "default-model")
    runtime_config.set_model_override("new-model")
    runtime_config.set_model_override(None)
    assert runtime_config.get_model_override() is None
    assert runtime_config.get_effective_model() == "default-model"


def test_set_model_override_empty_string_treated_as_clear(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "default-model")
    runtime_config.set_model_override("new-model")
    runtime_config.set_model_override("")
    assert runtime_config.get_model_override() is None


def test_light_group_override_independent_from_full(monkeypatch):
    monkeypatch.setattr("src.config.LLM_MODEL", "full-default")
    monkeypatch.setattr("src.config.LLM_LIGHT_MODEL", "light-default")

    runtime_config.set_model_override("full-override", "full")

    assert runtime_config.get_effective_model("full") == "full-override"
    assert runtime_config.get_effective_model("light") == "light-default"


def test_mask_key_short_key_all_masked():
    assert runtime_config._mask_key("abc123") == "*" * 6


def test_mask_key_long_key_shows_prefix_and_suffix():
    assert runtime_config._mask_key("sk-abcdefghijklmnop") == "sk-a...mnop"


def test_get_key_status_no_keys():
    status = runtime_config.get_key_status()
    assert status == {"key_count": 0, "current_key_index": None, "keys": []}


def test_get_key_status_multiple_keys_marks_current(monkeypatch):
    key_store.add_key("full", "sk-key1xxxx")
    key_store.add_key("full", "sk-key2xxxx")
    key_store.add_key("full", "sk-key3xxxx")
    monkeypatch.setattr("src.llm_rotation._current_key_index", {"full": 1, "light": 0, "mimo": 0})

    status = runtime_config.get_key_status()
    assert status["key_count"] == 3
    assert status["current_key_index"] == 1
    assert status["keys"][1]["is_current"] is True
    assert status["keys"][0]["is_current"] is False
    assert all("masked" in k for k in status["keys"])


def test_get_key_status_light_group_reads_independent_index(monkeypatch):
    key_store.add_key("light", "sk-a")
    key_store.add_key("light", "sk-b")
    monkeypatch.setattr("src.llm_rotation._current_key_index", {"full": 0, "light": 1, "mimo": 0})

    status = runtime_config.get_key_status("light")
    assert status["current_key_index"] == 1
    assert status["keys"][1]["is_current"] is True


def test_get_key_status_mimo_group_reads_independent_index(monkeypatch):
    key_store.add_key("mimo", "tp-a", channel="token_plan")
    key_store.add_key("mimo", "legacy-a", channel="legacy")
    monkeypatch.setattr("src.llm_rotation._current_key_index", {"full": 0, "light": 0, "mimo": 1})

    status = runtime_config.get_key_status("mimo")
    assert status["key_count"] == 2
    assert status["current_key_index"] == 1
    assert status["keys"][1]["is_current"] is True


def test_get_key_status_tavily_group_reads_web_search_tools_index(monkeypatch):
    key_store.add_key("tavily", "tvly-a")
    key_store.add_key("tavily", "tvly-b")
    monkeypatch.setattr("src.tools.web_search_tools._current_key_index", 1)

    status = runtime_config.get_key_status("tavily")
    assert status["current_key_index"] == 1
    assert status["keys"][1]["is_current"] is True


def test_get_key_status_poster_group_always_reports_index_zero():
    key_store.add_key("poster", "sk-poster-a")
    key_store.add_key("poster", "sk-poster-b")

    status = runtime_config.get_key_status("poster")
    assert status["current_key_index"] == 0
    assert status["keys"][0]["is_current"] is True
    assert status["keys"][1]["is_current"] is False


def test_get_key_status_poster_group_no_keys_reports_none_index():
    status = runtime_config.get_key_status("poster")
    assert status["current_key_index"] is None
