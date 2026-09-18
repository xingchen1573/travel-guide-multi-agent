"""src/channels/feishu/bindings.py 的单元测试：chat_id↔project_id绑定+awaiting状态读写
+一个群多次旅程（active_project_id/history）+旧数据就地迁移，纯文件IO逻辑，不涉及
LLM/网络。见docs/ARCHITECTURE.md第14.4节。"""
from __future__ import annotations

import json

import pytest

from src.channels.feishu import bindings


@pytest.fixture(autouse=True)
def isolated_bindings_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bindings, "BINDINGS_PATH", tmp_path / "feishu" / "bindings.json")


def test_get_binding_returns_none_when_not_bound():
    assert bindings.get_binding("chat-1") is None


def test_get_or_create_binding_creates_deterministic_project_id():
    binding = bindings.get_or_create_binding("chat-1")
    assert binding == {
        "active_project_id": "feishu-chat-1",
        "history": ["feishu-chat-1"],
        "awaiting": None,
        "pending_new_journey_message": None,
        "_is_new_user": True,
    }
    assert bindings.get_binding("chat-1") == binding


def test_get_or_create_binding_is_idempotent_and_does_not_reset_awaiting():
    bindings.get_or_create_binding("chat-1")
    bindings.set_awaiting("chat-1", "requirements")

    binding = bindings.get_or_create_binding("chat-1")

    assert binding["awaiting"] == "requirements"


def test_different_chat_ids_get_independent_bindings():
    bindings.get_or_create_binding("chat-1")
    bindings.get_or_create_binding("chat-2")

    assert bindings.get_binding("chat-1")["active_project_id"] == "feishu-chat-1"
    assert bindings.get_binding("chat-2")["active_project_id"] == "feishu-chat-2"


def test_set_awaiting_updates_existing_binding():
    bindings.get_or_create_binding("chat-1")

    bindings.set_awaiting("chat-1", "skeleton_confirm")

    assert bindings.get_binding("chat-1")["awaiting"] == "skeleton_confirm"


def test_set_awaiting_can_clear_back_to_none():
    bindings.get_or_create_binding("chat-1")
    bindings.set_awaiting("chat-1", "realtime_approval")

    bindings.set_awaiting("chat-1", None)

    assert bindings.get_binding("chat-1")["awaiting"] is None


def test_set_awaiting_raises_when_chat_not_bound_yet():
    with pytest.raises(ValueError, match="还没有绑定"):
        bindings.set_awaiting("never-seen-chat", "requirements")


def test_bindings_persist_across_module_level_reads():
    """验证真的落盘了，不是只存在内存里——用同样的BINDINGS_PATH重新读一次文件内容。"""
    bindings.get_or_create_binding("chat-1")

    raw = bindings.BINDINGS_PATH.read_text(encoding="utf-8")
    assert "feishu-chat-1" in raw


# ---------------------------------------------------------------------------
# get_chat_id_for_project：反查方向（project_id -> chat_id），供主动提醒机制的
# create_reminder工具使用（见docs/ARCHITECTURE.md第14.1节）
# ---------------------------------------------------------------------------


def test_get_chat_id_for_project_finds_matching_chat():
    bindings.get_or_create_binding("chat-1")

    assert bindings.get_chat_id_for_project("feishu-chat-1") == "chat-1"


def test_get_chat_id_for_project_returns_none_when_not_bound():
    assert bindings.get_chat_id_for_project("never-bound-project") is None


def test_get_chat_id_for_project_distinguishes_multiple_bindings():
    bindings.get_or_create_binding("chat-1")
    bindings.get_or_create_binding("chat-2")

    assert bindings.get_chat_id_for_project("feishu-chat-2") == "chat-2"


def test_get_chat_id_for_project_finds_non_active_historical_project():
    """一个群多次旅程后，旧的（非active）project_id依然应该能被反查到——
    reminder创建时固化的chat_id映射关系不受"是否还是active"影响。"""
    bindings.get_or_create_binding("chat-1")
    old_project_id = bindings.get_binding("chat-1")["active_project_id"]
    bindings.start_new_journey("chat-1")

    assert bindings.get_chat_id_for_project(old_project_id) == "chat-1"


# ---------------------------------------------------------------------------
# list_bindings：供管理后台飞书运维可视化只读展示使用（docs/ARCHITECTURE.md第13.2节域8）
# ---------------------------------------------------------------------------


def test_list_bindings_returns_empty_dict_when_no_bindings_file():
    assert bindings.list_bindings() == {}


def test_list_bindings_returns_all_bindings_keyed_by_chat_id():
    bindings.get_or_create_binding("chat-1")
    bindings.get_or_create_binding("chat-2")
    bindings.set_awaiting("chat-2", "skeleton_confirm")

    result = bindings.list_bindings()

    assert result["chat-1"]["active_project_id"] == "feishu-chat-1"
    assert result["chat-1"]["awaiting"] is None
    assert result["chat-2"]["active_project_id"] == "feishu-chat-2"
    assert result["chat-2"]["awaiting"] == "skeleton_confirm"


# ---------------------------------------------------------------------------
# 一个群多次旅程（docs/ARCHITECTURE.md第14.4节）：start_new_journey/
# pending_new_journey_message
# ---------------------------------------------------------------------------


def test_start_new_journey_generates_sequential_project_id():
    bindings.get_or_create_binding("chat-1")

    new_project_id = bindings.start_new_journey("chat-1")

    assert new_project_id == "feishu-chat-1-2"


def test_start_new_journey_appends_to_history_and_switches_active():
    bindings.get_or_create_binding("chat-1")
    first_project_id = bindings.get_binding("chat-1")["active_project_id"]

    new_project_id = bindings.start_new_journey("chat-1")

    binding = bindings.get_binding("chat-1")
    assert binding["active_project_id"] == new_project_id
    assert binding["history"] == [first_project_id, new_project_id]


def test_start_new_journey_resets_awaiting_and_pending_message():
    bindings.get_or_create_binding("chat-1")
    bindings.set_awaiting("chat-1", "requirements")
    bindings.set_pending_new_journey_message("chat-1", "我想去大理玩3天")

    bindings.start_new_journey("chat-1")

    binding = bindings.get_binding("chat-1")
    assert binding["awaiting"] is None
    assert binding["pending_new_journey_message"] is None


def test_start_new_journey_multiple_times_keeps_incrementing():
    bindings.get_or_create_binding("chat-1")
    bindings.start_new_journey("chat-1")
    third_project_id = bindings.start_new_journey("chat-1")

    assert third_project_id == "feishu-chat-1-3"
    assert bindings.get_binding("chat-1")["history"] == [
        "feishu-chat-1",
        "feishu-chat-1-2",
        "feishu-chat-1-3",
    ]


def test_start_new_journey_raises_when_chat_not_bound_yet():
    with pytest.raises(ValueError, match="还没有绑定"):
        bindings.start_new_journey("never-seen-chat")


def test_set_and_pop_pending_new_journey_message():
    bindings.get_or_create_binding("chat-1")

    bindings.set_pending_new_journey_message("chat-1", "我想去大理玩3天")
    assert bindings.get_binding("chat-1")["pending_new_journey_message"] == "我想去大理玩3天"

    popped = bindings.pop_pending_new_journey_message("chat-1")

    assert popped == "我想去大理玩3天"
    assert bindings.get_binding("chat-1")["pending_new_journey_message"] is None


def test_pop_pending_new_journey_message_returns_none_when_never_set():
    bindings.get_or_create_binding("chat-1")

    assert bindings.pop_pending_new_journey_message("chat-1") is None


def test_set_pending_new_journey_message_raises_when_chat_not_bound_yet():
    with pytest.raises(ValueError, match="还没有绑定"):
        bindings.set_pending_new_journey_message("never-seen-chat", "消息")


def test_pop_pending_new_journey_message_raises_when_chat_not_bound_yet():
    with pytest.raises(ValueError, match="还没有绑定"):
        bindings.pop_pending_new_journey_message("never-seen-chat")


# ---------------------------------------------------------------------------
# 旧数据就地静默迁移：真实data/feishu/bindings.json可能还留着旧schema
# （{"project_id":..., "awaiting":...}），读取路径应该原地升级并写回。
# ---------------------------------------------------------------------------


def _write_legacy_bindings(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_get_binding_migrates_legacy_schema_in_place():
    _write_legacy_bindings(
        bindings.BINDINGS_PATH,
        {"chat-legacy": {"project_id": "feishu-chat-legacy", "awaiting": "requirements"}},
    )

    binding = bindings.get_binding("chat-legacy")

    assert binding == {
        "active_project_id": "feishu-chat-legacy",
        "history": ["feishu-chat-legacy"],
        "awaiting": "requirements",
        "pending_new_journey_message": None,
    }
    # 确认真的写回了磁盘，不是只在内存里临时转换
    raw = json.loads(bindings.BINDINGS_PATH.read_text(encoding="utf-8"))
    assert raw["chat-legacy"]["active_project_id"] == "feishu-chat-legacy"


def test_get_or_create_binding_migrates_legacy_schema():
    _write_legacy_bindings(
        bindings.BINDINGS_PATH,
        {"chat-legacy": {"project_id": "feishu-chat-legacy", "awaiting": None}},
    )

    binding = bindings.get_or_create_binding("chat-legacy")

    assert binding["active_project_id"] == "feishu-chat-legacy"
    assert binding["history"] == ["feishu-chat-legacy"]


def test_list_bindings_migrates_all_legacy_entries():
    _write_legacy_bindings(
        bindings.BINDINGS_PATH,
        {
            "chat-legacy-1": {"project_id": "feishu-chat-legacy-1", "awaiting": None},
            "chat-legacy-2": {"project_id": "feishu-chat-legacy-2", "awaiting": "skeleton_confirm"},
        },
    )

    result = bindings.list_bindings()

    assert result["chat-legacy-1"]["active_project_id"] == "feishu-chat-legacy-1"
    assert result["chat-legacy-2"]["active_project_id"] == "feishu-chat-legacy-2"
    assert result["chat-legacy-2"]["awaiting"] == "skeleton_confirm"


def test_legacy_binding_can_start_new_journey_after_migration():
    """迁移后的旧记录应该能正常使用新功能（history已经是[旧project_id]，
    新旅程从序号2开始）。"""
    _write_legacy_bindings(
        bindings.BINDINGS_PATH,
        {"chat-legacy": {"project_id": "feishu-chat-legacy", "awaiting": None}},
    )

    new_project_id = bindings.start_new_journey("chat-legacy")

    assert new_project_id == "feishu-chat-legacy-2"


# ---------------------------------------------------------------------------
# unbind_project：管理后台软删除project时自动解绑飞书群（2026-08-01新增）
# ---------------------------------------------------------------------------


def test_unbind_project_clears_active_project_id_for_matching_bindings():
    bindings.get_or_create_binding("chat-1")

    affected = bindings.unbind_project("feishu-chat-1")

    assert affected == ["chat-1"]
    assert bindings.get_binding("chat-1")["active_project_id"] is None


def test_unbind_project_leaves_history_untouched():
    bindings.get_or_create_binding("chat-1")

    bindings.unbind_project("feishu-chat-1")

    assert bindings.get_binding("chat-1")["history"] == ["feishu-chat-1"]


def test_unbind_project_no_matching_bindings_returns_empty_list():
    bindings.get_or_create_binding("chat-1")

    affected = bindings.unbind_project("some-other-project")

    assert affected == []
    assert bindings.get_binding("chat-1")["active_project_id"] == "feishu-chat-1"


def test_unbind_project_leaves_other_chats_bindings_alone():
    bindings.get_or_create_binding("chat-1")
    bindings.get_or_create_binding("chat-2")

    affected = bindings.unbind_project("feishu-chat-1")

    assert affected == ["chat-1"]
    assert bindings.get_binding("chat-2")["active_project_id"] == "feishu-chat-2"
