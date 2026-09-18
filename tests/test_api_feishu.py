"""src/api/routers/feishu.py的FastAPI TestClient测试。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.channels.feishu import bindings, health, push_log
from src.workspace import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(bindings, "BINDINGS_PATH", tmp_path / "feishu" / "bindings.json")
    monkeypatch.setattr(health, "HEARTBEAT_PATH", tmp_path / "feishu" / "worker_heartbeat.json")
    monkeypatch.setattr(health, "HEARTBEAT_HISTORY_PATH", tmp_path / "feishu" / "heartbeat_history.jsonl")
    monkeypatch.setattr(push_log, "PUSH_LOG_PATH", tmp_path / "feishu" / "push_log.jsonl")
    yield


def _seed_project(project_id: str, requirements: dict) -> None:
    store.init_project(project_id)
    store.write_requirements(project_id, requirements["destination"], requirements)


# ---------------------------------------------------------------------------
# GET /api/feishu/bindings
# ---------------------------------------------------------------------------


def test_list_bindings_returns_empty_list_when_no_bindings():
    resp = client.get("/api/feishu/bindings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_bindings_enriches_with_project_summary_when_project_exists():
    bindings.get_or_create_binding("chat-1")
    _seed_project("feishu-chat-1", {"destination": "成都", "days": 3})

    resp = client.get("/api/feishu/bindings")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["chat_id"] == "chat-1"
    assert entry["project_id"] == "feishu-chat-1"
    assert entry["history"] == ["feishu-chat-1"]
    assert entry["project_exists"] is True
    assert entry["destination"] == "成都"
    assert entry["state"] == "需求收集中"
    assert entry["awaiting"] is None


def test_list_bindings_marks_project_not_exists_when_binding_orphaned():
    """chat_id绑定了project_id，但project目录被人工删掉/从未真正init_project过——
    不应该报错，只是project_exists=False且其余字段全部为None。"""
    bindings.get_or_create_binding("chat-orphan")

    resp = client.get("/api/feishu/bindings")
    body = resp.json()
    assert body[0]["project_exists"] is False
    assert body[0]["topic"] is None
    assert body[0]["state"] is None


def test_list_bindings_reflects_awaiting_state():
    bindings.get_or_create_binding("chat-1")
    bindings.set_awaiting("chat-1", "skeleton_confirm")

    resp = client.get("/api/feishu/bindings")
    assert resp.json()[0]["awaiting"] == "skeleton_confirm"


def test_list_bindings_sorted_by_chat_id():
    bindings.get_or_create_binding("chat-b")
    bindings.get_or_create_binding("chat-a")

    resp = client.get("/api/feishu/bindings")
    chat_ids = [entry["chat_id"] for entry in resp.json()]
    assert chat_ids == ["chat-a", "chat-b"]


# ---------------------------------------------------------------------------
# GET /api/feishu/health
# ---------------------------------------------------------------------------


def test_get_health_returns_not_running_when_no_heartbeat():
    resp = client.get("/api/feishu/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_running"


def test_get_health_returns_healthy_after_recent_heartbeat():
    health.write_heartbeat()

    resp = client.get("/api/feishu/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["pid"] is not None


def test_get_health_returns_stale_for_old_heartbeat():
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=health.STALE_THRESHOLD_SECONDS + 30)).isoformat()
    health.HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    health.HEARTBEAT_PATH.write_text(
        json.dumps({"pid": 999, "started_at": old_time, "last_heartbeat": old_time}),
        encoding="utf-8",
    )

    resp = client.get("/api/feishu/health")
    assert resp.json()["status"] == "stale"


# ---------------------------------------------------------------------------
# GET /api/feishu/push_log（2026-08-01新增）
# ---------------------------------------------------------------------------


def test_get_push_log_empty_when_no_log():
    resp = client.get("/api/feishu/push_log")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_push_log_returns_recent_entries():
    push_log.append_push_log("chat-1", "project-1", "message", True)
    push_log.append_push_log("chat-1", "project-1", "message", False, error="超时")

    resp = client.get("/api/feishu/push_log")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["success"] is False  # 最新的排最前面


def test_get_push_log_respects_limit_query_param():
    for i in range(5):
        push_log.append_push_log("chat-1", f"project-{i}", "message", True)

    resp = client.get("/api/feishu/push_log?limit=2")
    assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# GET /api/feishu/heartbeat_history（2026-08-01新增）
# ---------------------------------------------------------------------------


def test_get_heartbeat_history_empty_when_never_written():
    resp = client.get("/api/feishu/heartbeat_history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["history"] == []
    assert body["anomalies"] == []


def test_get_heartbeat_history_includes_anomalies():
    entries = [
        {"timestamp": "2026-08-01T00:00:00+00:00", "pid": 1},
        {"timestamp": "2026-08-01T00:05:00+00:00", "pid": 1},  # 5分钟间隔，超过阈值
    ]
    health.HEARTBEAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with health.HEARTBEAT_HISTORY_PATH.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    resp = client.get("/api/feishu/heartbeat_history")
    body = resp.json()
    assert len(body["history"]) == 2
    assert len(body["anomalies"]) == 1


# ---------------------------------------------------------------------------
# GET /api/feishu/activity_stats（2026-08-01新增）
# ---------------------------------------------------------------------------


def test_get_activity_stats_returns_aggregated_list():
    push_log.append_push_log("chat-1", "project-1", "message", True)
    push_log.append_push_log("chat-1", "project-1", "reminder", True)
    push_log.append_push_log("chat-2", "project-2", "message", False, error="失败")

    resp = client.get("/api/feishu/activity_stats")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    stat_by_project = {s["project_id"]: s for s in body}
    assert stat_by_project["project-1"]["push_count"] == 2
    assert stat_by_project["project-2"]["failure_count"] == 1


def test_get_activity_stats_empty_when_no_log():
    resp = client.get("/api/feishu/activity_stats")
    assert resp.json() == []
