"""src/api/key_store.py 的单元测试：多key池的增删改+首次迁移逻辑，纯文件IO，
不涉及LLM/网络调用。"""
from __future__ import annotations

import pytest

from src.api import key_store


@pytest.fixture(autouse=True)
def isolated_key_store(tmp_path, monkeypatch):
    monkeypatch.setattr(key_store, "KEY_STORE_PATH", tmp_path / "runtime_config" / "api_keys.json")
    # 默认清空真实.env里的key，避免ensure_migrated()意外用这台机器的真实key池
    # 灌入测试隔离的临时存储——测试migration行为的用例会显式monkeypatch这些值。
    monkeypatch.setattr("src.config.LLM_API_KEYS", [])
    monkeypatch.setattr("src.config.LLM_API_KEYS_LIGHT", [])
    monkeypatch.setattr("src.config.MIMO_TOKEN_PLAN_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_GPTLOAD_API_KEYS", [])
    monkeypatch.setattr("src.config.GPTLOAD_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_API_KEYS", [])
    monkeypatch.setattr("src.config.CUNAI_API_KEYS", [])
    monkeypatch.setattr("src.config.TAVILY_API_KEYS", [])
    monkeypatch.setattr("src.config.POSTER_IMAGE_API_KEY", None)


def test_ensure_migrated_seeds_from_env_when_store_empty(monkeypatch):
    monkeypatch.setattr("src.config.LLM_API_KEYS", ["full-k1", "full-k2"])
    monkeypatch.setattr("src.config.LLM_API_KEYS_LIGHT", ["light-k1"])
    monkeypatch.setattr("src.config.MIMO_TOKEN_PLAN_API_KEYS", ["tp-k1"])
    monkeypatch.setattr("src.config.GPTLOAD_API_KEYS", ["gateway-k1"])
    monkeypatch.setattr("src.config.MIMO_API_KEYS", ["legacy-k1"])
    monkeypatch.setattr("src.config.CUNAI_API_KEYS", ["cunai-k1"])
    monkeypatch.setattr("src.config.TAVILY_API_KEYS", ["tvly-k1"])
    monkeypatch.setattr("src.config.POSTER_IMAGE_API_KEY", "poster-k1")

    key_store.ensure_migrated()

    assert key_store.list_keys("full") == ["full-k1", "full-k2"]
    assert key_store.list_keys("light") == ["light-k1"]
    assert key_store.list_keys("tavily") == ["tvly-k1"]
    assert key_store.list_keys("poster") == ["poster-k1"]
    assert key_store.list_keys("cunai") == ["cunai-k1"]
    mimo_entries = key_store.list_mimo_keys()
    assert mimo_entries == [
        {"key": "gateway-k1", "channel": "gptload"},
        {"key": "tp-k1", "channel": "token_plan"},
        {"key": "legacy-k1", "channel": "legacy"},
    ]


def test_ensure_migrated_seeds_empty_poster_list_when_unset(monkeypatch):
    monkeypatch.setattr("src.config.POSTER_IMAGE_API_KEY", None)

    key_store.ensure_migrated()

    assert key_store.list_keys("poster") == []


def test_ensure_migrated_is_idempotent_when_store_already_has_data():
    key_store.add_key("full", "manually-added-key")  # 首次调用触发migration，seed为空列表+这个key

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.config.LLM_API_KEYS", ["env-key-that-should-be-ignored"])
        key_store.ensure_migrated()  # 存储已有内容，不应该重新灌入已有 group

    assert key_store.list_keys("full") == ["manually-added-key"]


def test_ensure_migrated_backfills_missing_group_without_overwriting_existing(monkeypatch):
    """旧存储应补齐现行分组、保留已有值，并删除已下线分组。"""
    key_store.KEY_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    key_store.KEY_STORE_PATH.write_text(
        '{"full": ["existing-full-k1"], "retired_provider": ["obsolete-k1"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr("src.config.CUNAI_API_KEYS", ["cunai-from-env"])
    monkeypatch.setattr("src.config.LLM_API_KEYS", ["should-not-overwrite-full"])

    key_store.ensure_migrated()

    assert key_store.list_keys("full") == ["existing-full-k1"]  # 已有不覆盖
    assert key_store.list_keys("cunai") == ["cunai-from-env"]
    assert "retired_provider" not in key_store._read_all_unlocked()


def test_add_key_and_delete_key_round_trip():
    key_store.add_key("full", "k1")
    key_store.add_key("full", "k2")
    assert key_store.list_keys("full") == ["k1", "k2"]

    key_store.delete_key("full", 0)
    assert key_store.list_keys("full") == ["k2"]


def test_add_key_rejects_unknown_group():
    with pytest.raises(ValueError):
        key_store.add_key("unknown", "k1")


def test_add_key_rejects_blank_key():
    with pytest.raises(ValueError):
        key_store.add_key("full", "   ")


def test_add_key_mimo_requires_channel():
    with pytest.raises(ValueError):
        key_store.add_key("mimo", "k1")


def test_add_key_mimo_rejects_invalid_channel():
    with pytest.raises(ValueError):
        key_store.add_key("mimo", "k1", channel="bogus")


def test_list_mimo_keys_preserves_channel_field():
    key_store.add_key("mimo", "gateway-k1", channel="gptload")
    key_store.add_key("mimo", "tp-k1", channel="token_plan")
    key_store.add_key("mimo", "legacy-k1", channel="legacy")

    entries = key_store.list_mimo_keys()

    assert entries == [
        {"key": "gateway-k1", "channel": "gptload"},
        {"key": "tp-k1", "channel": "token_plan"},
        {"key": "legacy-k1", "channel": "legacy"},
    ]
    assert key_store.list_keys("mimo") == ["gateway-k1", "tp-k1", "legacy-k1"]


def test_add_mimo_key_keeps_channel_priority():
    key_store.add_key("mimo", "legacy-k1", channel="legacy")
    key_store.add_key("mimo", "tp-k1", channel="token_plan")
    key_store.add_key("mimo", "gateway-k1", channel="gptload")

    assert [entry["channel"] for entry in key_store.list_mimo_keys()] == [
        "gptload",
        "token_plan",
        "legacy",
    ]


def test_delete_key_out_of_range_raises():
    key_store.add_key("full", "k1")
    with pytest.raises(IndexError):
        key_store.delete_key("full", 5)


def test_mask_key_matches_existing_masking_behavior():
    assert key_store._mask_key("sk-abcdefghij") == "sk-a...ghij"
    assert key_store._mask_key("short") == "*****"
