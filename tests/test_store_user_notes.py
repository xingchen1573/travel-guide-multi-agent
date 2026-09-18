"""src/workspace/store.py 里用户备注/annotation layer相关写/读函数的单元测试（网页表单+
后台API的后端支撑，见src/api/routers/user_notes.py，docs/ARCHITECTURE.md第21节）。
跟test_store_poi_photos.py同款isolated_project fixture风格。
"""
from __future__ import annotations

import pytest

from src.workspace import store


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "test-project"
    store.init_project(project_id)
    return project_id


# ---------- write_user_note ----------


def test_write_user_note_trip_level(isolated_project):
    note = store.write_user_note(isolated_project, "admin_user", "整体节奏很紧凑")
    assert note.day is None
    assert note.poi_name == ""
    assert note.note_text == "整体节奏很紧凑"
    assert note.source == "web"
    assert note.deleted is False
    assert note.superseded_by is None

    path = store.workspace_dir(isolated_project) / "outputs" / "user_notes.jsonl"
    assert path.exists()
    assert path.read_text(encoding="utf-8").count("\n") == 1


def test_write_user_note_day_level(isolated_project):
    note = store.write_user_note(isolated_project, "admin_user", "这天很累", day=2)
    assert note.day == 2
    assert note.poi_name == ""


def test_write_user_note_poi_level(isolated_project):
    note = store.write_user_note(isolated_project, "admin_user", "要提前订", day=2, poi_name="宽窄巷子")
    assert note.day == 2
    assert note.poi_name == "宽窄巷子"


def test_write_user_note_rejects_non_admin_actor(isolated_project):
    with pytest.raises(store.PermissionError):
        store.write_user_note(isolated_project, "route_agent", "不应该写入")


def test_write_user_note_appends_multiple(isolated_project):
    store.write_user_note(isolated_project, "admin_user", "备注1")
    store.write_user_note(isolated_project, "admin_user", "备注2")
    notes = store.list_user_notes(isolated_project)
    assert len(notes) == 2
    assert {n.note_text for n in notes} == {"备注1", "备注2"}


# ---------- list_user_notes ----------


def test_list_user_notes_empty_when_no_file(isolated_project):
    assert store.list_user_notes(isolated_project) == []


def test_list_user_notes_excludes_deleted(isolated_project):
    note = store.write_user_note(isolated_project, "admin_user", "会被删掉")
    store.delete_user_note(isolated_project, "admin_user", note.note_id)
    assert store.list_user_notes(isolated_project) == []


# ---------- update_user_note ----------


def test_update_user_note_supersedes_old_and_appends_new(isolated_project):
    old = store.write_user_note(isolated_project, "admin_user", "旧内容", day=1)
    new = store.update_user_note(isolated_project, "admin_user", old.note_id, "新内容", day=2)

    assert new.note_id != old.note_id
    assert new.note_text == "新内容"
    assert new.day == 2

    notes = store.list_user_notes(isolated_project)
    assert len(notes) == 1
    assert notes[0].note_id == new.note_id


def test_update_user_note_old_record_marked_deleted_and_superseded(isolated_project):
    old = store.write_user_note(isolated_project, "admin_user", "旧内容")
    new = store.update_user_note(isolated_project, "admin_user", old.note_id, "新内容")

    raw = store._read_user_notes_raw(isolated_project)
    old_raw = next(n for n in raw if n["note_id"] == old.note_id)
    assert old_raw["deleted"] is True
    assert old_raw["superseded_by"] == new.note_id


def test_update_user_note_missing_note_id_raises_value_error(isolated_project):
    with pytest.raises(ValueError):
        store.update_user_note(isolated_project, "admin_user", "不存在的id", "新内容")


def test_update_user_note_rejects_non_admin_actor(isolated_project):
    old = store.write_user_note(isolated_project, "admin_user", "旧内容")
    with pytest.raises(store.PermissionError):
        store.update_user_note(isolated_project, "lifestyle_agent", old.note_id, "新内容")


def test_update_already_deleted_note_raises_value_error(isolated_project):
    old = store.write_user_note(isolated_project, "admin_user", "旧内容")
    store.delete_user_note(isolated_project, "admin_user", old.note_id)
    with pytest.raises(ValueError):
        store.update_user_note(isolated_project, "admin_user", old.note_id, "新内容")


# ---------- delete_user_note ----------


def test_delete_user_note_marks_deleted(isolated_project):
    note = store.write_user_note(isolated_project, "admin_user", "待删除")
    store.delete_user_note(isolated_project, "admin_user", note.note_id)

    raw = store._read_user_notes_raw(isolated_project)
    assert raw[0]["deleted"] is True


def test_delete_user_note_missing_note_id_raises_value_error(isolated_project):
    with pytest.raises(ValueError):
        store.delete_user_note(isolated_project, "admin_user", "不存在的id")


def test_delete_user_note_rejects_non_admin_actor(isolated_project):
    note = store.write_user_note(isolated_project, "admin_user", "待删除")
    with pytest.raises(store.PermissionError):
        store.delete_user_note(isolated_project, "route_agent", note.note_id)


def test_delete_already_deleted_note_raises_value_error(isolated_project):
    note = store.write_user_note(isolated_project, "admin_user", "待删除")
    store.delete_user_note(isolated_project, "admin_user", note.note_id)
    with pytest.raises(ValueError):
        store.delete_user_note(isolated_project, "admin_user", note.note_id)


# ---------- 跨重跑不清（append-only底本不被彻底重跑覆盖，这里只验证store层不会主动清空） ----------


def test_user_notes_survive_across_multiple_writes_and_reads(isolated_project):
    store.write_user_note(isolated_project, "admin_user", "第一条", day=1)
    store.write_user_note(isolated_project, "admin_user", "第二条", day=2, poi_name="武侯祠")
    notes = store.list_user_notes(isolated_project)
    assert len(notes) == 2
    assert notes[0].note_text == "第一条"
    assert notes[1].note_text == "第二条"
