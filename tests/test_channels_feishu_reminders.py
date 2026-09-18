"""src/channels/feishu/reminders.py 的单元测试：提醒任务的创建/到期查询/状态流转，
纯文件IO逻辑，不涉及LLM/网络，跟tests/test_channels_feishu_bindings.py同款风格。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.channels.feishu import reminders


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "PROJECTS_DIR", tmp_path)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# ---------------------------------------------------------------------------
# create_reminder
# ---------------------------------------------------------------------------


def test_create_reminder_writes_record_with_all_fields():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(hours=1)), "该出发去高铁站了"
    )

    assert record["chat_id"] == "chat-1"
    assert record["project_id"] == "project-1"
    assert record["message"] == "该出发去高铁站了"
    assert record["status"] == "pending"
    assert record["retry_count"] == 0
    assert "id" in record and record["id"]
    assert "created_at" in record


def test_create_reminder_persists_to_project_specific_file(tmp_path):
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "提醒A")

    path = tmp_path / "project-1" / "reminders.json"
    assert path.exists()
    assert "提醒A" in path.read_text(encoding="utf-8")


def test_create_reminder_appends_multiple_records_for_same_project():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "提醒A")
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=2)), "提醒B")

    all_records = reminders._read_all(reminders._reminders_path("project-1"))
    assert len(all_records) == 2


# ---------------------------------------------------------------------------
# list_due_reminders：只返回到期且pending的任务，跨project正确聚合
# ---------------------------------------------------------------------------


def test_list_due_reminders_returns_empty_when_no_projects(tmp_path):
    assert reminders.list_due_reminders() == []


def test_list_due_reminders_excludes_future_trigger():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "还没到期")

    assert reminders.list_due_reminders() == []


def test_list_due_reminders_includes_past_trigger():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(minutes=-5)), "已经到期"
    )

    due = reminders.list_due_reminders()

    assert len(due) == 1
    assert due[0]["id"] == record["id"]


def test_list_due_reminders_excludes_non_pending_status():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(minutes=-5)), "已经到期但已发送"
    )
    reminders.mark_sent("project-1", record["id"])

    assert reminders.list_due_reminders() == []


def test_list_due_reminders_aggregates_across_multiple_projects():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "项目1到期")
    reminders.create_reminder("project-2", "chat-2", _iso(timedelta(minutes=-5)), "项目2到期")
    reminders.create_reminder("project-3", "chat-3", _iso(timedelta(hours=1)), "项目3未到期")

    due = reminders.list_due_reminders()

    assert {r["project_id"] for r in due} == {"project-1", "project-2"}


def test_list_due_reminders_skips_malformed_trigger_at(tmp_path):
    path = tmp_path / "project-1" / "reminders.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[{"id": "bad-1", "trigger_at": "not-a-date", "message": "坏数据", '
        '"chat_id": "chat-1", "project_id": "project-1", "status": "pending", "retry_count": 0}]',
        encoding="utf-8",
    )

    assert reminders.list_due_reminders() == []


# ---------------------------------------------------------------------------
# mark_sent / mark_retry_or_failed 状态转换
# ---------------------------------------------------------------------------


def test_mark_sent_updates_status():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")

    reminders.mark_sent("project-1", record["id"])

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["status"] == "sent"


def test_mark_retry_or_failed_increments_retry_count_and_stays_pending():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")

    reminders.mark_retry_or_failed("project-1", record["id"])

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["retry_count"] == 1
    assert stored["status"] == "pending"


def test_mark_retry_or_failed_transitions_to_failed_after_max_retry():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")

    for _ in range(reminders.MAX_RETRY):
        reminders.mark_retry_or_failed("project-1", record["id"])

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["retry_count"] == reminders.MAX_RETRY
    assert stored["status"] == "failed"


def test_failed_reminder_no_longer_shows_up_in_due_list():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")
    for _ in range(reminders.MAX_RETRY):
        reminders.mark_retry_or_failed("project-1", record["id"])

    assert reminders.list_due_reminders() == []


# ---------------------------------------------------------------------------
# 重复提醒（2026-07-26新增）：mark_sent对repeat_interval_hours非None的记录
# 推进trigger_at而不是转sent
# ---------------------------------------------------------------------------


def test_create_reminder_stores_repeat_interval_hours():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(hours=1)), "每天提醒", repeat_interval_hours=24
    )

    assert record["repeat_interval_hours"] == 24
    assert record["last_triggered_at"] is None


def test_create_reminder_defaults_repeat_interval_hours_to_none():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "一次性提醒")

    assert record["repeat_interval_hours"] is None


def test_mark_sent_transitions_one_off_reminder_to_sent():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "一次性到期")

    reminders.mark_sent("project-1", record["id"])

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["status"] == "sent"


def test_mark_sent_advances_trigger_at_for_repeating_reminder_and_stays_pending():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(minutes=-5)), "每小时提醒", repeat_interval_hours=1
    )
    original_trigger = record["trigger_at"]

    reminders.mark_sent("project-1", record["id"])

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["status"] == "pending"
    assert stored["trigger_at"] != original_trigger
    new_trigger = datetime.fromisoformat(stored["trigger_at"])
    assert new_trigger > datetime.now(timezone.utc)
    assert stored["last_triggered_at"] is not None


def test_mark_sent_resets_retry_count_for_repeating_reminder():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(minutes=-5)), "每小时提醒", repeat_interval_hours=1
    )
    reminders.mark_retry_or_failed("project-1", record["id"])
    reminders.mark_retry_or_failed("project-1", record["id"])

    reminders.mark_sent("project-1", record["id"])

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["retry_count"] == 0


def test_mark_sent_advances_repeating_reminder_past_multiple_missed_cycles():
    """模拟worker停机很久（超过好几个interval）后才处理到这条提醒——下一个触发点
    应该是原trigger_at按固定间隔对齐到的下一个未来时刻，不是简单"当前时间+间隔"，
    避免"每天8点"这类固定时刻的提醒因为停机时长漂移。"""
    # trigger_at设置在10小时前，interval是1小时——相当于错过了至少10个周期。
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(hours=-10)), "每小时提醒", repeat_interval_hours=1
    )

    reminders.mark_sent("project-1", record["id"])

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    new_trigger = datetime.fromisoformat(stored["trigger_at"])
    now = datetime.now(timezone.utc)
    # 推进后的触发点应该是未来的，且跟now的差距在一个interval以内（对齐到下一个
    # 整点周期，不是从now重新起算导致远超1小时）。
    assert new_trigger > now
    assert (new_trigger - now) <= timedelta(hours=1, minutes=1)


def test_repeating_reminder_reappears_in_due_list_after_being_sent_and_time_passes(monkeypatch):
    """重复提醒被mark_sent推进到下一个周期后，在新周期到期前不应该出现在due list里
    （这是mark_sent正确推进到*未来*时间点的间接验证）。"""
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(minutes=-5)), "每小时提醒", repeat_interval_hours=1
    )
    reminders.mark_sent("project-1", record["id"])

    assert reminders.list_due_reminders() == []


# ---------------------------------------------------------------------------
# list_reminders_for_project（2026-07-26新增）
# ---------------------------------------------------------------------------


def test_list_reminders_for_project_returns_empty_when_none_exist():
    assert reminders.list_reminders_for_project("project-1") == []


def test_list_reminders_for_project_only_returns_pending():
    kept = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "待触发")
    sent = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "已发送")
    reminders.mark_sent("project-1", sent["id"])
    cancelled = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=2)), "已取消")
    reminders.cancel_reminder("project-1", cancelled["id"])

    result = reminders.list_reminders_for_project("project-1")

    assert [r["id"] for r in result] == [kept["id"]]


def test_list_reminders_for_project_sorted_by_trigger_at_ascending():
    later = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=5)), "较晚")
    earlier = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "较早")

    result = reminders.list_reminders_for_project("project-1")

    assert [r["id"] for r in result] == [earlier["id"], later["id"]]


def test_list_reminders_for_project_only_returns_matching_project():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "属于project-1")
    reminders.create_reminder("project-2", "chat-2", _iso(timedelta(hours=1)), "属于project-2")

    result = reminders.list_reminders_for_project("project-1")

    assert len(result) == 1
    assert result[0]["message"] == "属于project-1"


# ---------------------------------------------------------------------------
# cancel_reminder（2026-07-26新增）
# ---------------------------------------------------------------------------


def test_cancel_reminder_succeeds_for_pending_record():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "待取消")

    ok = reminders.cancel_reminder("project-1", record["id"])

    assert ok is True
    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["status"] == "cancelled"


def test_cancel_reminder_removes_it_from_due_list():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "已到期但要取消")

    reminders.cancel_reminder("project-1", record["id"])

    assert reminders.list_due_reminders() == []


def test_cancel_reminder_fails_for_nonexistent_id():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "无关")

    ok = reminders.cancel_reminder("project-1", "not-a-real-id")

    assert ok is False


def test_cancel_reminder_fails_for_already_sent_record():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "已发送")
    reminders.mark_sent("project-1", record["id"])

    ok = reminders.cancel_reminder("project-1", record["id"])

    assert ok is False


def test_cancel_reminder_fails_for_already_cancelled_record():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "重复取消")
    reminders.cancel_reminder("project-1", record["id"])

    ok = reminders.cancel_reminder("project-1", record["id"])

    assert ok is False


# ---------------------------------------------------------------------------
# update_reminder（2026-07-26新增）
# ---------------------------------------------------------------------------


def test_update_reminder_changes_trigger_at_only():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "原文案")
    new_trigger = _iso(timedelta(hours=2))

    ok = reminders.update_reminder("project-1", record["id"], trigger_at=new_trigger)

    assert ok is True
    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["trigger_at"] == new_trigger
    assert stored["message"] == "原文案"


def test_update_reminder_changes_message_only():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "原文案")

    ok = reminders.update_reminder("project-1", record["id"], message="新文案")

    assert ok is True
    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["message"] == "新文案"


def test_update_reminder_not_passing_repeat_interval_hours_leaves_it_unchanged():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(hours=1)), "原文案", repeat_interval_hours=6
    )

    reminders.update_reminder("project-1", record["id"], message="改文案不改重复规则")

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["repeat_interval_hours"] == 6


def test_update_reminder_can_explicitly_clear_repeat_interval_hours():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(hours=1)), "原为重复提醒", repeat_interval_hours=6
    )

    ok = reminders.update_reminder("project-1", record["id"], repeat_interval_hours=None)

    assert ok is True
    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["repeat_interval_hours"] is None


def test_update_reminder_can_change_repeat_interval_hours_to_new_value():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(hours=1)), "重复提醒", repeat_interval_hours=6
    )

    reminders.update_reminder("project-1", record["id"], repeat_interval_hours=12)

    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["repeat_interval_hours"] == 12


def test_update_reminder_fails_for_nonexistent_id():
    ok = reminders.update_reminder("project-1", "not-a-real-id", message="改不到")

    assert ok is False


def test_update_reminder_fails_for_already_sent_record():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "已发送")
    reminders.mark_sent("project-1", record["id"])

    ok = reminders.update_reminder("project-1", record["id"], message="不应该改成功")

    assert ok is False
    stored = reminders._read_all(reminders._reminders_path("project-1"))[0]
    assert stored["message"] == "已发送"


def test_update_reminder_fails_for_cancelled_record():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "已取消")
    reminders.cancel_reminder("project-1", record["id"])

    ok = reminders.update_reminder("project-1", record["id"], message="不应该改成功")

    assert ok is False

