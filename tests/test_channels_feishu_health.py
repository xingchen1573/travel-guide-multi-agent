"""src/channels/feishu/health.py 的单元测试：worker心跳写入/健康状态判定，纯文件IO逻辑，
不涉及真实WS连接。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.channels.feishu import health


@pytest.fixture(autouse=True)
def isolated_heartbeat_path(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "HEARTBEAT_PATH", tmp_path / "feishu" / "worker_heartbeat.json")
    monkeypatch.setattr(health, "HEARTBEAT_HISTORY_PATH", tmp_path / "feishu" / "heartbeat_history.jsonl")


def test_get_worker_health_returns_not_running_when_never_written():
    result = health.get_worker_health()
    assert result["status"] == "not_running"
    assert result["pid"] is None
    assert result["last_heartbeat"] is None
    assert result["seconds_since_heartbeat"] is None


def test_write_heartbeat_creates_file_with_current_pid():
    health.write_heartbeat()

    data = json.loads(health.HEARTBEAT_PATH.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["started_at"]
    assert data["last_heartbeat"]


def test_get_worker_health_reports_healthy_right_after_write():
    health.write_heartbeat()

    result = health.get_worker_health()
    assert result["status"] == "healthy"
    assert result["pid"] == os.getpid()
    assert result["seconds_since_heartbeat"] < 5


def test_write_heartbeat_preserves_started_at_across_calls_from_same_pid():
    health.write_heartbeat()
    first_started_at = json.loads(health.HEARTBEAT_PATH.read_text(encoding="utf-8"))["started_at"]

    health.write_heartbeat()
    second = json.loads(health.HEARTBEAT_PATH.read_text(encoding="utf-8"))

    assert second["started_at"] == first_started_at


def test_write_heartbeat_resets_started_at_for_a_different_pid():
    health.write_heartbeat()

    stale = json.loads(health.HEARTBEAT_PATH.read_text(encoding="utf-8"))
    stale["pid"] = stale["pid"] + 1  # 模拟另一个进程写过的旧记录
    stale["started_at"] = "2020-01-01T00:00:00+00:00"
    health.HEARTBEAT_PATH.write_text(json.dumps(stale), encoding="utf-8")

    health.write_heartbeat()

    data = json.loads(health.HEARTBEAT_PATH.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["started_at"] != "2020-01-01T00:00:00+00:00"


def test_get_worker_health_reports_stale_when_heartbeat_too_old():
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=health.STALE_THRESHOLD_SECONDS + 30)).isoformat()
    health.HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    health.HEARTBEAT_PATH.write_text(
        json.dumps({"pid": 12345, "started_at": old_time, "last_heartbeat": old_time}),
        encoding="utf-8",
    )

    result = health.get_worker_health()
    assert result["status"] == "stale"
    assert result["pid"] == 12345
    assert result["seconds_since_heartbeat"] > health.STALE_THRESHOLD_SECONDS


def test_get_worker_health_handles_corrupt_json_gracefully():
    health.HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    health.HEARTBEAT_PATH.write_text("not valid json{{{", encoding="utf-8")

    result = health.get_worker_health()
    assert result["status"] == "not_running"


def test_start_heartbeat_thread_writes_immediately_and_stops_cleanly():
    thread = health.start_heartbeat_thread(interval_seconds=999)
    try:
        assert health.HEARTBEAT_PATH.exists()
        assert health.get_worker_health()["status"] == "healthy"
    finally:
        thread._stop_event.set()  # type: ignore[attr-defined]
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 心跳历史 + 疑似掉线检测（2026-08-01新增，补上"只有最新快照没有历史"这个缺口）
# ---------------------------------------------------------------------------


def test_write_heartbeat_appends_to_history():
    health.write_heartbeat()
    health.write_heartbeat()

    history = health.read_heartbeat_history()
    assert len(history) == 2
    assert all("timestamp" in h and "pid" in h for h in history)


def test_read_heartbeat_history_empty_when_never_written():
    assert health.read_heartbeat_history() == []


def test_read_heartbeat_history_returns_entries_oldest_first():
    entries = [
        {"timestamp": "2026-08-01T00:00:00+00:00", "pid": 1},
        {"timestamp": "2026-08-01T00:00:20+00:00", "pid": 1},
        {"timestamp": "2026-08-01T00:00:40+00:00", "pid": 1},
    ]
    health.HEARTBEAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with health.HEARTBEAT_HISTORY_PATH.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    result = health.read_heartbeat_history()
    assert [r["timestamp"] for r in result] == [e["timestamp"] for e in entries]


def test_read_heartbeat_history_respects_limit():
    entries = [{"timestamp": f"2026-08-01T00:{i:02d}:00+00:00", "pid": 1} for i in range(5)]
    health.HEARTBEAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with health.HEARTBEAT_HISTORY_PATH.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    result = health.read_heartbeat_history(limit=2)
    assert len(result) == 2
    assert result[-1]["timestamp"] == entries[-1]["timestamp"]  # 最近2条，仍保持时间正序


def test_detect_anomaly_periods_finds_gap_exceeding_threshold():
    history = [
        {"timestamp": "2026-08-01T00:00:00+00:00", "pid": 1},
        {"timestamp": "2026-08-01T00:00:20+00:00", "pid": 1},
        {"timestamp": "2026-08-01T00:05:00+00:00", "pid": 1},  # 跟上一条相差4分40秒，超过阈值
        {"timestamp": "2026-08-01T00:05:20+00:00", "pid": 1},
    ]

    anomalies = health.detect_anomaly_periods(history, gap_threshold_seconds=60.0)

    assert len(anomalies) == 1
    assert anomalies[0]["started_at"] == "2026-08-01T00:00:20+00:00"
    assert anomalies[0]["ended_at"] == "2026-08-01T00:05:00+00:00"
    assert anomalies[0]["gap_seconds"] == pytest.approx(280.0)


def test_detect_anomaly_periods_no_anomalies_when_regular_intervals():
    history = [
        {"timestamp": "2026-08-01T00:00:00+00:00", "pid": 1},
        {"timestamp": "2026-08-01T00:00:20+00:00", "pid": 1},
        {"timestamp": "2026-08-01T00:00:40+00:00", "pid": 1},
    ]

    assert health.detect_anomaly_periods(history, gap_threshold_seconds=60.0) == []


def test_detect_anomaly_periods_defaults_to_reading_from_file():
    health.write_heartbeat()
    health.write_heartbeat()

    # 默认不传history参数时，直接调用不应该报错，正常读文件（间隔很短，不应有异常）
    assert health.detect_anomaly_periods() == []


def test_detect_anomaly_periods_empty_or_single_entry_history():
    assert health.detect_anomaly_periods([]) == []
    assert health.detect_anomaly_periods([{"timestamp": "2026-08-01T00:00:00+00:00", "pid": 1}]) == []
