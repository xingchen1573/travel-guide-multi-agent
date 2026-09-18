"""src/channels/feishu/push_log.py 的单元测试：推送日志追加+读取+活跃度聚合，
纯文件IO逻辑，不涉及LLM/网络。"""
from __future__ import annotations

import pytest

from src.channels.feishu import push_log


@pytest.fixture(autouse=True)
def isolated_push_log_path(tmp_path, monkeypatch):
    monkeypatch.setattr(push_log, "PUSH_LOG_PATH", tmp_path / "feishu" / "push_log.jsonl")


def test_read_push_log_empty_when_file_does_not_exist():
    assert push_log.read_push_log() == []


def test_append_and_read_push_log_round_trip():
    push_log.append_push_log("chat-1", "project-1", "message", True)

    entries = push_log.read_push_log()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["chat_id"] == "chat-1"
    assert entry["project_id"] == "project-1"
    assert entry["push_type"] == "message"
    assert entry["success"] is True
    assert entry["error"] is None
    assert "timestamp" in entry


def test_append_push_log_handles_null_project_id():
    push_log.append_push_log("chat-1", None, "message", False, error="binding not resolved yet")

    entries = push_log.read_push_log()
    assert entries[0]["project_id"] is None
    assert entries[0]["error"] == "binding not resolved yet"


def test_read_push_log_respects_limit_and_most_recent_first():
    for i in range(5):
        push_log.append_push_log("chat-1", f"project-{i}", "message", True)

    entries = push_log.read_push_log(limit=2)
    assert len(entries) == 2
    assert entries[0]["project_id"] == "project-4"
    assert entries[1]["project_id"] == "project-3"


def test_compute_activity_stats_aggregates_by_project_id():
    push_log.append_push_log("chat-1", "project-1", "message", True)
    push_log.append_push_log("chat-1", "project-1", "message", False, error="timeout")
    push_log.append_push_log("chat-2", "project-2", "reminder", True)

    stats = push_log.compute_activity_stats()
    assert len(stats) == 2

    stat_by_project = {s["project_id"]: s for s in stats}
    assert stat_by_project["project-1"]["push_count"] == 2
    assert stat_by_project["project-1"]["success_count"] == 1
    assert stat_by_project["project-1"]["failure_count"] == 1
    assert stat_by_project["project-2"]["push_count"] == 1
    assert stat_by_project["project-2"]["success_count"] == 1


def test_compute_activity_stats_falls_back_to_chat_id_when_project_id_null():
    push_log.append_push_log("chat-1", None, "message", True)

    stats = push_log.compute_activity_stats()
    assert len(stats) == 1
    assert stats[0]["project_id"] is None
    assert stats[0]["chat_id"] == "chat-1"
    assert stats[0]["push_count"] == 1


def test_compute_activity_stats_sorted_by_last_push_descending():
    push_log.append_push_log("chat-1", "project-old", "message", True)
    push_log.append_push_log("chat-2", "project-new", "message", True)

    stats = push_log.compute_activity_stats()
    assert stats[0]["project_id"] == "project-new"
    assert stats[1]["project_id"] == "project-old"


def test_compute_activity_stats_empty_when_no_log():
    assert push_log.compute_activity_stats() == []
