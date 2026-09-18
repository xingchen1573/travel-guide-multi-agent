"""src/tools/realtime_tools.py 新增的get_current_time/create_reminder两个工具的
单元测试（主动提醒机制v1，见docs/ARCHITECTURE.md第14.1节）。answer_query/propose_patch
两个既有工具的行为已经在tests/test_orchestrator_companion.py里通过实际节点调用间接
覆盖，这里只补新增部分。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.channels.feishu import bindings, reminders
from src.tools.realtime_tools import make_realtime_tools


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(bindings, "BINDINGS_PATH", tmp_path / "feishu" / "bindings.json")
    monkeypatch.setattr(reminders, "PROJECTS_DIR", tmp_path / "projects")


def _get_tool(name: str, project_id: str = "project-1"):
    tools = make_realtime_tools(project_id, {})
    return next(t for t in tools if t.name == name)


def test_get_current_time_returns_parseable_iso_string():
    tool = _get_tool("get_current_time")

    result = tool.invoke({})

    # 能被fromisoformat解析回去，说明确实是合法的ISO格式，且能被模型继续做加减推算。
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None


def test_create_reminder_writes_record_when_project_bound_to_chat():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    tool = _get_tool("create_reminder", project_id=project_id)
    trigger_at = datetime.now(timezone.utc).isoformat()

    result = tool.invoke({"trigger_at": trigger_at, "message": "该出发去高铁站了"})

    assert "已创建" in result
    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert len(stored) == 1
    assert stored[0]["chat_id"] == "chat-1"
    assert stored[0]["message"] == "该出发去高铁站了"
    assert stored[0]["project_id"] == project_id


def test_create_reminder_fails_gracefully_when_project_not_bound():
    tool = _get_tool("create_reminder", project_id="never-bound-project")

    result = tool.invoke({"trigger_at": datetime.now(timezone.utc).isoformat(), "message": "提醒文案"})

    assert "失败" in result
    assert reminders._read_all(reminders._reminders_path("never-bound-project")) == []


# ---------------------------------------------------------------------------
# create_reminder的repeat_interval_hours（2026-07-26新增）
# ---------------------------------------------------------------------------


def test_create_reminder_with_repeat_interval_hours_persists_it():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    tool = _get_tool("create_reminder", project_id=project_id)

    result = tool.invoke(
        {
            "trigger_at": datetime.now(timezone.utc).isoformat(),
            "message": "每天提醒",
            "repeat_interval_hours": 24,
        }
    )

    assert "重复" in result
    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert stored[0]["repeat_interval_hours"] == 24


def test_create_reminder_without_repeat_interval_hours_stores_none():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    tool = _get_tool("create_reminder", project_id=project_id)

    tool.invoke({"trigger_at": datetime.now(timezone.utc).isoformat(), "message": "一次性提醒"})

    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert stored[0]["repeat_interval_hours"] is None


# ---------------------------------------------------------------------------
# list_reminders（2026-07-26新增）
# ---------------------------------------------------------------------------


def test_list_reminders_reports_no_reminders_when_empty():
    tool = _get_tool("list_reminders", project_id="project-empty")

    result = tool.invoke({})

    assert "没有" in result


def test_list_reminders_includes_id_and_message_for_pending_reminders():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    record = reminders.create_reminder(
        project_id, "chat-1", datetime.now(timezone.utc).isoformat(), "该出发去高铁站了"
    )
    tool = _get_tool("list_reminders", project_id=project_id)

    result = tool.invoke({})

    assert record["id"] in result
    assert "该出发去高铁站了" in result


def test_list_reminders_marks_repeating_reminders():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    reminders.create_reminder(
        project_id, "chat-1", datetime.now(timezone.utc).isoformat(), "每天提醒", repeat_interval_hours=24
    )
    tool = _get_tool("list_reminders", project_id=project_id)

    result = tool.invoke({})

    assert "重复" in result


# ---------------------------------------------------------------------------
# cancel_reminder_tool（2026-07-26新增）
# ---------------------------------------------------------------------------


def test_cancel_reminder_tool_succeeds_for_real_id():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    record = reminders.create_reminder(
        project_id, "chat-1", datetime.now(timezone.utc).isoformat(), "待取消"
    )
    tool = _get_tool("cancel_reminder_tool", project_id=project_id)

    result = tool.invoke({"reminder_id": record["id"]})

    assert "成功" in result
    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert stored[0]["status"] == "cancelled"


def test_cancel_reminder_tool_fails_gracefully_for_fake_id():
    tool = _get_tool("cancel_reminder_tool", project_id="project-1")

    result = tool.invoke({"reminder_id": "fake-id-模型编造的"})

    assert "失败" in result


# ---------------------------------------------------------------------------
# update_reminder_tool（2026-07-26新增）
# ---------------------------------------------------------------------------


def test_update_reminder_tool_changes_message_only():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    record = reminders.create_reminder(
        project_id, "chat-1", datetime.now(timezone.utc).isoformat(), "原文案"
    )
    tool = _get_tool("update_reminder_tool", project_id=project_id)

    result = tool.invoke({"reminder_id": record["id"], "message": "新文案"})

    assert "成功" in result
    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert stored[0]["message"] == "新文案"


def test_update_reminder_tool_default_repeat_interval_hours_leaves_it_unchanged():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    record = reminders.create_reminder(
        project_id, "chat-1", datetime.now(timezone.utc).isoformat(), "原文案", repeat_interval_hours=6
    )
    tool = _get_tool("update_reminder_tool", project_id=project_id)

    tool.invoke({"reminder_id": record["id"], "message": "改文案不改重复规则"})

    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert stored[0]["repeat_interval_hours"] == 6


def test_update_reminder_tool_zero_clears_repeat_interval_hours():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    record = reminders.create_reminder(
        project_id, "chat-1", datetime.now(timezone.utc).isoformat(), "重复提醒", repeat_interval_hours=6
    )
    tool = _get_tool("update_reminder_tool", project_id=project_id)

    tool.invoke({"reminder_id": record["id"], "repeat_interval_hours": 0})

    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert stored[0]["repeat_interval_hours"] is None


def test_update_reminder_tool_positive_value_changes_repeat_interval_hours():
    bindings.get_or_create_binding("chat-1")
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    record = reminders.create_reminder(
        project_id, "chat-1", datetime.now(timezone.utc).isoformat(), "重复提醒", repeat_interval_hours=6
    )
    tool = _get_tool("update_reminder_tool", project_id=project_id)

    tool.invoke({"reminder_id": record["id"], "repeat_interval_hours": 12})

    stored = reminders._read_all(reminders._reminders_path(project_id))
    assert stored[0]["repeat_interval_hours"] == 12


def test_update_reminder_tool_fails_gracefully_for_fake_id():
    tool = _get_tool("update_reminder_tool", project_id="project-1")

    result = tool.invoke({"reminder_id": "fake-id", "message": "改不到"})

    assert "失败" in result

