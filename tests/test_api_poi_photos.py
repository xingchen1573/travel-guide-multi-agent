"""src/api/routers/poi_photos.py的FastAPI TestClient测试：管理后台"POI图片"页面的
上传/删除/读取接口。这是管理后台第一个真正的写操作（人类actor=admin_user），见
docs/ARCHITECTURE.md第13.2节。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.api.app import app
from src.tools.poi_photo import _MIN_PHOTO_WIDTH_PX
from src.workspace import store
from src.workspace.schema import PoiDetailFile, RouteOption, RouteSegment

client = TestClient(app)


def _jpeg_bytes(width: int = _MIN_PHOTO_WIDTH_PX, height: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(60, 90, 150)).save(buf, format="JPEG")
    return buf.getvalue()


_JPG = _jpeg_bytes()


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    yield


def _seed_project_with_poi(project_id: str) -> None:
    store.init_project(project_id)
    detail = PoiDetailFile(
        poi_name="武侯祠",
        day=1,
        routes=[
            RouteOption(
                route_name="经典线",
                route_segments=[RouteSegment(order=1, point_name="大门")],
            )
        ],
    )
    store.write_poi_detail(project_id, actor="poi_detail_agent", poi_detail=detail)


# ---------------------------------------------------------------------------
# GET /poi_details
# ---------------------------------------------------------------------------


def test_get_poi_details_returns_generated_files():
    _seed_project_with_poi("proj1")
    resp = client.get("/api/projects/proj1/poi_details")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["poi_name"] == "武侯祠"


def test_get_poi_details_404_when_project_missing():
    resp = client.get("/api/projects/does-not-exist/poi_details")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# overview photo: upload / get / delete round trip
# ---------------------------------------------------------------------------


def test_overview_photo_upload_get_delete_round_trip():
    _seed_project_with_poi("proj1")

    resp = client.post(
        "/api/projects/proj1/poi_photos/1/武侯祠/overview",
        files={"file": ("photo.jpg", _JPG, "image/jpeg")},
    )
    assert resp.status_code == 200
    assert resp.json()["uploaded_overview_photo_path"] == "outputs/poi_photos/day1_武侯祠/overview.jpg"

    resp = client.get("/api/projects/proj1/poi_photos/1/武侯祠/overview")
    assert resp.status_code == 200
    assert resp.content == _JPG

    resp = client.delete("/api/projects/proj1/poi_photos/1/武侯祠/overview")
    assert resp.status_code == 200
    assert resp.json()["uploaded_overview_photo_path"] == ""

    resp = client.get("/api/projects/proj1/poi_photos/1/武侯祠/overview")
    assert resp.status_code == 404


def test_overview_photo_upload_rejects_invalid_file_type():
    _seed_project_with_poi("proj1")
    resp = client.post(
        "/api/projects/proj1/poi_photos/1/武侯祠/overview",
        files={"file": ("photo.jpg", b"not an image", "image/jpeg")},
    )
    assert resp.status_code == 400


def test_overview_photo_upload_rejects_low_resolution():
    _seed_project_with_poi("proj1")
    resp = client.post(
        "/api/projects/proj1/poi_photos/1/武侯祠/overview",
        files={"file": ("photo.jpg", _jpeg_bytes(_MIN_PHOTO_WIDTH_PX - 1), "image/jpeg")},
    )
    assert resp.status_code == 400
    assert "分辨率过低" in resp.json()["detail"]


def test_overview_photo_upload_404_when_project_missing():
    resp = client.post(
        "/api/projects/does-not-exist/poi_photos/1/武侯祠/overview",
        files={"file": ("photo.jpg", _JPG, "image/jpeg")},
    )
    assert resp.status_code == 404


def test_overview_photo_upload_404_when_poi_missing():
    store.init_project("proj1")
    resp = client.post(
        "/api/projects/proj1/poi_photos/1/不存在的地方/overview",
        files={"file": ("photo.jpg", _JPG, "image/jpeg")},
    )
    assert resp.status_code == 404


def test_get_overview_photo_404_when_never_uploaded():
    _seed_project_with_poi("proj1")
    resp = client.get("/api/projects/proj1/poi_photos/1/武侯祠/overview")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# segment photo: upload / get / delete round trip
# ---------------------------------------------------------------------------


def test_segment_photo_upload_get_delete_round_trip():
    _seed_project_with_poi("proj1")

    resp = client.post(
        "/api/projects/proj1/poi_photos/1/武侯祠/segment",
        params={"route_name": "经典线", "order": 1},
        files={"file": ("photo.jpg", _JPG, "image/jpeg")},
    )
    assert resp.status_code == 200
    segment = resp.json()["routes"][0]["route_segments"][0]
    assert segment["uploaded_photo_path"] == "outputs/poi_photos/day1_武侯祠/seg_经典线_1.jpg"

    resp = client.get(
        "/api/projects/proj1/poi_photos/1/武侯祠/segment", params={"route_name": "经典线", "order": 1}
    )
    assert resp.status_code == 200
    assert resp.content == _JPG

    resp = client.delete(
        "/api/projects/proj1/poi_photos/1/武侯祠/segment", params={"route_name": "经典线", "order": 1}
    )
    assert resp.status_code == 200
    segment = resp.json()["routes"][0]["route_segments"][0]
    assert segment["uploaded_photo_path"] == ""

    resp = client.get(
        "/api/projects/proj1/poi_photos/1/武侯祠/segment", params={"route_name": "经典线", "order": 1}
    )
    assert resp.status_code == 404


def test_segment_photo_upload_404_when_segment_missing():
    _seed_project_with_poi("proj1")
    resp = client.post(
        "/api/projects/proj1/poi_photos/1/武侯祠/segment",
        params={"route_name": "经典线", "order": 99},
        files={"file": ("photo.jpg", _JPG, "image/jpeg")},
    )
    assert resp.status_code == 404
