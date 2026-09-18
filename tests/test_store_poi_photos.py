"""src/workspace/store.py 里POI图片人工上传相关写/读函数的单元测试（管理后台"POI图片"
页面的后端支撑，见src/api/routers/poi_photos.py）。跟test_store.py同款isolated_project
fixture风格，独立成文件是因为这批测试数量较多、主题独立（人类actor权限+文件生命周期）。
"""
from __future__ import annotations

import pytest

from src.workspace import store
from src.workspace.schema import PoiDetailFile, PoiDetailManifest, RouteOption, RouteSegment


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "test-project"
    store.init_project(project_id)
    return project_id


def _seed_poi_detail(project_id: str) -> None:
    detail = PoiDetailFile(
        poi_name="武侯祠",
        day=1,
        routes=[
            RouteOption(
                route_name="经典线",
                route_segments=[
                    RouteSegment(order=1, point_name="大门"),
                    RouteSegment(order=2, point_name="大廨"),
                ],
            )
        ],
    )
    store.write_poi_detail(project_id, actor="poi_detail_agent", poi_detail=detail)


_JPG = b"\xff\xd8\xff" + b"0" * 20
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


# ---------- write_poi_overview_photo / delete_poi_overview_photo ----------

def test_write_poi_overview_photo_sets_field_and_writes_file(isolated_project):
    _seed_poi_detail(isolated_project)
    result = store.write_poi_overview_photo(isolated_project, "admin_user", 1, "武侯祠", _JPG, "jpg")
    assert result.uploaded_overview_photo_path == "outputs/poi_photos/day1_武侯祠/overview.jpg"
    saved_path = store.workspace_dir(isolated_project) / result.uploaded_overview_photo_path
    assert saved_path.read_bytes() == _JPG
    # 落盘的POI详情文件也要能重新读出同一个字段，不只是内存里的返回值对
    reloaded = store.read_poi_detail(isolated_project, 1, "武侯祠")
    assert reloaded.uploaded_overview_photo_path == result.uploaded_overview_photo_path


def test_write_poi_overview_photo_rejects_non_admin_actor(isolated_project):
    _seed_poi_detail(isolated_project)
    with pytest.raises(store.PermissionError):
        store.write_poi_overview_photo(isolated_project, "route_agent", 1, "武侯祠", _JPG, "jpg")


def test_write_poi_overview_photo_missing_poi_raises_value_error(isolated_project):
    with pytest.raises(ValueError):
        store.write_poi_overview_photo(isolated_project, "admin_user", 1, "不存在的地方", _JPG, "jpg")


def test_write_poi_overview_photo_overwrite_removes_old_extension_variant(isolated_project):
    """覆盖上传换了格式（jpg->png）时，旧扩展名文件必须被清掉，不能留孤儿文件。"""
    _seed_poi_detail(isolated_project)
    store.write_poi_overview_photo(isolated_project, "admin_user", 1, "武侯祠", _JPG, "jpg")
    directory = store.workspace_dir(isolated_project) / "outputs" / "poi_photos" / "day1_武侯祠"
    assert (directory / "overview.jpg").exists()

    store.write_poi_overview_photo(isolated_project, "admin_user", 1, "武侯祠", _PNG, "png")
    assert not (directory / "overview.jpg").exists()
    assert (directory / "overview.png").exists()


def test_delete_poi_overview_photo_clears_field_and_removes_file(isolated_project):
    _seed_poi_detail(isolated_project)
    store.write_poi_overview_photo(isolated_project, "admin_user", 1, "武侯祠", _JPG, "jpg")
    directory = store.workspace_dir(isolated_project) / "outputs" / "poi_photos" / "day1_武侯祠"

    result = store.delete_poi_overview_photo(isolated_project, "admin_user", 1, "武侯祠")
    assert result.uploaded_overview_photo_path == ""
    assert not (directory / "overview.jpg").exists()


def test_delete_poi_overview_photo_rejects_non_admin_actor(isolated_project):
    _seed_poi_detail(isolated_project)
    store.write_poi_overview_photo(isolated_project, "admin_user", 1, "武侯祠", _JPG, "jpg")
    with pytest.raises(store.PermissionError):
        store.delete_poi_overview_photo(isolated_project, "poi_detail_agent", 1, "武侯祠")


# ---------- write_poi_segment_photo / delete_poi_segment_photo ----------

def test_write_poi_segment_photo_sets_field_on_correct_segment(isolated_project):
    _seed_poi_detail(isolated_project)
    result = store.write_poi_segment_photo(
        isolated_project, "admin_user", 1, "武侯祠", "经典线", 2, _JPG, "jpg"
    )
    segments = {s.order: s for s in result.routes[0].route_segments}
    assert segments[2].uploaded_photo_path == "outputs/poi_photos/day1_武侯祠/seg_经典线_2.jpg"
    # 没被指定的那个途经点不应该被意外写入
    assert segments[1].uploaded_photo_path == ""


def test_write_poi_segment_photo_missing_route_name_raises_value_error(isolated_project):
    _seed_poi_detail(isolated_project)
    with pytest.raises(ValueError):
        store.write_poi_segment_photo(
            isolated_project, "admin_user", 1, "武侯祠", "不存在的路线", 1, _JPG, "jpg"
        )


def test_write_poi_segment_photo_missing_order_raises_value_error(isolated_project):
    _seed_poi_detail(isolated_project)
    with pytest.raises(ValueError):
        store.write_poi_segment_photo(
            isolated_project, "admin_user", 1, "武侯祠", "经典线", 99, _JPG, "jpg"
        )


def test_write_poi_segment_photo_rejects_non_admin_actor(isolated_project):
    _seed_poi_detail(isolated_project)
    with pytest.raises(store.PermissionError):
        store.write_poi_segment_photo(
            isolated_project, "lifestyle_agent", 1, "武侯祠", "经典线", 1, _JPG, "jpg"
        )


def test_delete_poi_segment_photo_clears_field_and_removes_file(isolated_project):
    _seed_poi_detail(isolated_project)
    store.write_poi_segment_photo(isolated_project, "admin_user", 1, "武侯祠", "经典线", 1, _JPG, "jpg")
    directory = store.workspace_dir(isolated_project) / "outputs" / "poi_photos" / "day1_武侯祠"
    assert (directory / "seg_经典线_1.jpg").exists()

    result = store.delete_poi_segment_photo(isolated_project, "admin_user", 1, "武侯祠", "经典线", 1)
    segments = {s.order: s for s in result.routes[0].route_segments}
    assert segments[1].uploaded_photo_path == ""
    assert not (directory / "seg_经典线_1.jpg").exists()


# ---------- list_poi_details ----------

def test_list_poi_details_returns_generated_files(isolated_project):
    _seed_poi_detail(isolated_project)
    details = store.list_poi_details(isolated_project)
    assert len(details) == 1
    assert details[0].poi_name == "武侯祠"


def test_list_poi_details_excludes_manifest_and_empty_when_no_dir(isolated_project):
    assert store.list_poi_details(isolated_project) == []


def test_list_poi_details_uses_existing_manifest_as_authoritative_set(isolated_project):
    _seed_poi_detail(isolated_project)
    store.write_poi_details_manifest(
        isolated_project,
        actor="main_agent",
        manifest=PoiDetailManifest(generated=[]),
    )

    assert store.list_poi_details(isolated_project) == []
    assert [detail.poi_name for detail in store.list_poi_details(
        isolated_project, include_unmanifested=True
    )] == ["武侯祠"]
