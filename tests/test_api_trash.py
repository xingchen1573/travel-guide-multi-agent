"""src/api/routers/trash.py 的 FastAPI TestClient 测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.workspace import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    yield


def _seed_project(project_id: str, requirements: dict) -> None:
    store.init_project(project_id)
    store.write_requirements(project_id, requirements["destination"], requirements)


def test_list_trash_empty():
    resp = client.get("/api/trash")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_trash_returns_trashed_projects():
    _seed_project("chengdu-v20", {"destination": "成都", "days": 3})
    store.soft_delete_project("chengdu-v20")

    resp = client.get("/api/trash")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["project_id"] == "chengdu-v20"
    assert body[0]["destination"] == "成都"
    assert body[0]["days"] == 3


def test_restore_not_found_returns_404():
    resp = client.post("/api/trash/does-not-exist/restore")
    assert resp.status_code == 404


def test_restore_success():
    _seed_project("chengdu-v21", {"destination": "成都", "days": 2})
    store.soft_delete_project("chengdu-v21")

    resp = client.post("/api/trash/chengdu-v21/restore")
    assert resp.status_code == 200
    assert resp.json() == {"project_id": "chengdu-v21", "status": "restored"}
    assert "chengdu-v21" in store.list_project_ids()
    assert not store.is_trashed("chengdu-v21")


def test_permanent_delete_not_found_returns_404():
    resp = client.delete("/api/trash/does-not-exist")
    assert resp.status_code == 404


def test_permanent_delete_success_removes_directory():
    _seed_project("chengdu-v22", {"destination": "成都", "days": 2})
    store.soft_delete_project("chengdu-v22")

    resp = client.delete("/api/trash/chengdu-v22")
    assert resp.status_code == 200
    assert resp.json() == {"project_id": "chengdu-v22", "status": "permanently_deleted"}
    assert store.list_trashed_project_ids() == []
