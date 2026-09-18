"""src/api/routers/user_notes.py的FastAPI TestClient测试：网页表单+后台API的增/改/删/
列出用户备注（用户备注/annotation layer，docs/ARCHITECTURE.md第21节）。跟
test_api_poi_photos.py同款风格——同步小写写操作，actor统一记admin_user。
"""
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


def _seed_project(project_id: str) -> None:
    store.init_project(project_id)


# ---------------------------------------------------------------------------
# GET /user_notes
# ---------------------------------------------------------------------------


def test_get_user_notes_empty_initially():
    _seed_project("proj1")
    resp = client.get("/api/projects/proj1/user_notes")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_user_notes_404_when_project_missing():
    resp = client.get("/api/projects/does-not-exist/user_notes")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /user_notes
# ---------------------------------------------------------------------------


def test_create_user_note_trip_level():
    _seed_project("proj1")
    resp = client.post("/api/projects/proj1/user_notes", json={"note_text": "整体很紧凑"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["note_text"] == "整体很紧凑"
    assert data["day"] is None
    assert data["poi_name"] == ""
    assert data["source"] == "web"


def test_create_user_note_poi_level():
    _seed_project("proj1")
    resp = client.post(
        "/api/projects/proj1/user_notes",
        json={"note_text": "要提前订", "day": 2, "poi_name": "宽窄巷子"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["day"] == 2
    assert data["poi_name"] == "宽窄巷子"


def test_create_user_note_404_when_project_missing():
    resp = client.post("/api/projects/does-not-exist/user_notes", json={"note_text": "x"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET/POST/PUT/DELETE round trip
# ---------------------------------------------------------------------------


def test_user_notes_full_round_trip():
    _seed_project("proj1")

    create_resp = client.post("/api/projects/proj1/user_notes", json={"note_text": "第一版"})
    assert create_resp.status_code == 200
    note_id = create_resp.json()["note_id"]

    list_resp = client.get("/api/projects/proj1/user_notes")
    assert len(list_resp.json()) == 1

    update_resp = client.put(
        f"/api/projects/proj1/user_notes/{note_id}", json={"note_text": "第二版", "day": 1}
    )
    assert update_resp.status_code == 200
    new_note_id = update_resp.json()["note_id"]
    assert new_note_id != note_id
    assert update_resp.json()["note_text"] == "第二版"

    list_resp = client.get("/api/projects/proj1/user_notes")
    data = list_resp.json()
    assert len(data) == 1
    assert data[0]["note_id"] == new_note_id

    delete_resp = client.delete(f"/api/projects/proj1/user_notes/{new_note_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json()["deleted"] is True

    list_resp = client.get("/api/projects/proj1/user_notes")
    assert list_resp.json() == []


def test_update_user_note_404_when_note_missing():
    _seed_project("proj1")
    resp = client.put("/api/projects/proj1/user_notes/does-not-exist", json={"note_text": "x"})
    assert resp.status_code == 404


def test_delete_user_note_404_when_note_missing():
    _seed_project("proj1")
    resp = client.delete("/api/projects/proj1/user_notes/does-not-exist")
    assert resp.status_code == 404
