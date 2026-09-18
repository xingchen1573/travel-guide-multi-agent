"""src/api/routers/materials.py 新增的 /insights + /insights/regenerate 路由测试。

2026-08-01更新：/insights/regenerate从同步阻塞改成接入jobs.py通用任务框架
（202+轮询状态，跟重跑/采集/生成网站同款模式），202/409相关测试同步更新；
422（素材不足）这条快失败路径仍然保留同步校验，不受影响。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api import jobs
from src.api.app import app
from src.api.routers import materials
from src.materials import insights as insights_module
from src.workspace.schema import TopicInsights

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_material_library(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIAL_LIBRARY_DIR", tmp_path)
    jobs._jobs.clear()
    yield


def _write_entries(tmp_path, topic: str, entries: list[dict]) -> None:
    topic_dir = tmp_path / "index" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    with (topic_dir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def test_get_insights_404_when_topic_missing():
    resp = client.get("/api/materials/不存在的主题/insights")
    assert resp.status_code == 404


def test_get_insights_returns_none_when_not_generated_yet(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000"}])

    resp = client.get("/api/materials/成都/insights")

    assert resp.status_code == 200
    assert resp.json() is None


def test_get_insights_returns_existing_file(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000"}])
    index_dir = tmp_path / "index" / "成都"
    payload = {
        "topic": "成都", "generated_at": "2026-08-01T00:00:00+00:00", "model": "sensenova-6.7-flash-lite",
        "based_on_entry_count": 1, "must_visit": [], "food": [], "pitfalls": [], "tips": [], "other": [],
    }
    (index_dir / "insights.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resp = client.get("/api/materials/成都/insights")

    assert resp.status_code == 200
    assert resp.json()["based_on_entry_count"] == 1


def test_regenerate_insights_404_when_topic_missing():
    resp = client.post("/api/materials/不存在的主题/insights/regenerate")
    assert resp.status_code == 404


def test_regenerate_insights_422_when_below_min_entries(tmp_path, monkeypatch):
    _write_entries(tmp_path, "峨眉山", [{"doc_id": "峨眉山-000"}])
    monkeypatch.setattr(insights_module, "generate_insights", lambda topic: None)

    resp = client.post("/api/materials/峨眉山/insights/regenerate")

    assert resp.status_code == 422


def test_regenerate_insights_triggers_background_task(tmp_path, monkeypatch):
    _write_entries(tmp_path, "成都", [{"doc_id": f"成都-{i:03d}"} for i in range(5)])
    monkeypatch.setattr(jobs, "trigger_regenerate_insights", lambda topic: None)

    resp = client.post("/api/materials/成都/insights/regenerate")

    assert resp.status_code == 202
    assert resp.json() == {"topic": "成都", "status": "running"}


def test_regenerate_insights_concurrent_returns_409(tmp_path, monkeypatch):
    _write_entries(tmp_path, "成都", [{"doc_id": f"成都-{i:03d}"} for i in range(5)])

    def already_running(topic):
        raise RuntimeError(f"topic {topic!r} 已经在重新生成洞察中")

    monkeypatch.setattr(jobs, "trigger_regenerate_insights", already_running)

    resp = client.post("/api/materials/成都/insights/regenerate")

    assert resp.status_code == 409


def test_regenerate_insights_status_not_found():
    resp = client.get("/api/materials/成都/insights/regenerate/status")
    assert resp.status_code == 404


def test_regenerate_insights_status_returns_job_state():
    fake_result = TopicInsights(topic="成都", based_on_entry_count=53, model="sensenova-6.7-flash-lite")
    jobs._jobs["regenerate_insights:成都"] = {
        "status": "succeeded",
        "error": None,
        "started_at": "t0",
        "finished_at": "t1",
        "log": [],
        "result": fake_result.model_dump(),
    }

    resp = client.get("/api/materials/成都/insights/regenerate/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["result"]["based_on_entry_count"] == 53
