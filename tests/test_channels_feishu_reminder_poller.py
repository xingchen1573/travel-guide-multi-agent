"""src/channels/feishu/reminder_poller.py 的单元测试：run_once的推送成功/失败两种场景、
消息前缀、以及start_polling_thread真的按interval周期性调用。不涉及真实飞书API——
send_fn由测试注入fake实现。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.channels.feishu import push_log, reminder_poller, reminders


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(push_log, "PUSH_LOG_PATH", tmp_path / "feishu" / "push_log.jsonl")


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# ---------------------------------------------------------------------------
# run_once：单次轮询的推送成功/失败两种场景
# ---------------------------------------------------------------------------


def test_run_once_sends_due_reminder_with_prefix_and_marks_sent():
    record = reminders.create_reminder(
        "project-1", "chat-1", _iso(timedelta(minutes=-5)), "该出发去高铁站了"
    )
    sent = []

    reminder_poller.run_once(lambda chat_id, text: sent.append((chat_id, text)))

    assert sent == [("chat-1", "【提醒】该出发去高铁站了")]
    stored = reminders._read_all(reminders._reminders_path("project-1"))
    assert stored[0]["id"] == record["id"]
    assert stored[0]["status"] == "sent"


def test_run_once_ignores_reminders_not_yet_due():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(hours=1)), "还没到")
    sent = []

    reminder_poller.run_once(lambda chat_id, text: sent.append((chat_id, text)))

    assert sent == []


def test_run_once_marks_retry_when_send_fails():
    record = reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")

    def _failing_send(chat_id, text):
        raise RuntimeError("模拟发送失败")

    reminder_poller.run_once(_failing_send)

    stored = reminders._read_all(reminders._reminders_path("project-1"))
    assert stored[0]["id"] == record["id"]
    assert stored[0]["status"] == "pending"
    assert stored[0]["retry_count"] == 1


def test_run_once_one_failure_does_not_block_other_due_reminders():
    """一条推送失败不应该影响其余到期任务的推送——每条独立try/except。"""
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "会失败")
    reminders.create_reminder("project-2", "chat-2", _iso(timedelta(minutes=-5)), "会成功")
    sent = []

    def _send(chat_id, text):
        if chat_id == "chat-1":
            raise RuntimeError("模拟发送失败")
        sent.append((chat_id, text))

    reminder_poller.run_once(_send)

    assert sent == [("chat-2", "【提醒】会成功")]
    project1_records = reminders._read_all(reminders._reminders_path("project-1"))
    project2_records = reminders._read_all(reminders._reminders_path("project-2"))
    assert project1_records[0]["status"] == "pending"
    assert project2_records[0]["status"] == "sent"


# ---------------------------------------------------------------------------
# 推送日志（2026-08-01新增）：每次推送尝试（成功/失败）都应该记一条日志
# ---------------------------------------------------------------------------


def test_run_once_logs_successful_push():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "该出发去高铁站了")

    reminder_poller.run_once(lambda chat_id, text: None)

    entries = push_log.read_push_log()
    assert len(entries) == 1
    assert entries[0]["chat_id"] == "chat-1"
    assert entries[0]["project_id"] == "project-1"
    assert entries[0]["push_type"] == "reminder"
    assert entries[0]["success"] is True
    assert entries[0]["error"] is None


def test_run_once_logs_failed_push():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")

    def _failing_send(chat_id, text):
        raise RuntimeError("模拟发送失败")

    reminder_poller.run_once(_failing_send)

    entries = push_log.read_push_log()
    assert len(entries) == 1
    assert entries[0]["success"] is False
    assert "模拟发送失败" in entries[0]["error"]


# ---------------------------------------------------------------------------
# start_polling_thread：周期性调用
# ---------------------------------------------------------------------------


def test_start_polling_thread_calls_run_once_periodically():
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")
    call_count = threading.Event()
    sent = []

    def _send(chat_id, text):
        sent.append((chat_id, text))
        call_count.set()

    thread = reminder_poller.start_polling_thread(_send, interval_seconds=0.05)
    try:
        # timeout故意留了较大余量（相对0.05s的interval是100倍）——这是真实挂钟时间的
        # 线程调度测试，2026-07-25用systematic-debugging复现过：并行跑两份全量测试套件
        # 抢CPU时，2s的timeout会被真实饿死导致这条测试偶发失败（docs/ISSUES.md第53条），
        # 5s是在"测试不无限拖慢"和"给足调度余量"之间的经验取舍，不保证100%消除，只是
        # 大幅降低触发概率。
        assert call_count.wait(timeout=5), "轮询线程应该在interval后至少调用过一次run_once"
        assert sent
    finally:
        thread._stop_event.set()
        thread.join(timeout=1)


def test_start_polling_thread_survives_send_fn_exception():
    """send_fn抛异常时轮询循环本身不应该整体退出——下一轮还能继续扫描。"""
    reminders.create_reminder("project-1", "chat-1", _iso(timedelta(minutes=-5)), "到期")
    calls = []

    def _always_failing(chat_id, text):
        calls.append(1)
        raise RuntimeError("每次都失败")

    thread = reminder_poller.start_polling_thread(_always_failing, interval_seconds=0.05)
    try:
        time.sleep(0.2)
        assert thread.is_alive()
        assert len(calls) >= 1
    finally:
        thread._stop_event.set()
        thread.join(timeout=1)
