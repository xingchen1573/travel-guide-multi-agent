"""src/api/routers/materials.py的FastAPI TestClient测试。"""
from __future__ import annotations

import json
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import jobs
from src.api.app import app
from src.api.routers import materials
from src.materials import ingest, recycle_bin, topic_spaces, vector_store
from src.workspace import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_material_library(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(ingest, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(recycle_bin, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(topic_spaces, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(vector_store, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    jobs._jobs.clear()
    yield


def _write_entries(tmp_path, topic: str, entries: list[dict]) -> None:
    topic_dir = tmp_path / "index" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    with (topic_dir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _wait_until_done(task_type: str, key: str, timeout_steps: int = 50) -> None:
    for _ in range(timeout_steps):
        if not jobs.is_running(task_type, key):
            return
        threading.Event().wait(0.05)


def test_list_topics_empty():
    resp = client.get("/api/materials")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_topics_returns_counts(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "is_core": True},
        {"doc_id": "成都-001", "is_core": False},
    ])
    (tmp_path / "index" / "成都" / "chunks.jsonl").write_text('{"chunk_id": "c1"}\n', encoding="utf-8")

    resp = client.get("/api/materials")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["topic"] == "成都"
    assert body[0]["entry_count"] == 2
    assert body[0]["core_count"] == 1
    assert body[0]["chunk_count"] == 1
    assert body[0]["total_word_count"] == 0  # 两条entry都没有source_file，读不到raw原文


def test_list_topics_total_word_count_sums_raw_files(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "source_file": "a_原文.md"},
        {"doc_id": "成都-001", "source_file": "b_原文.md"},
    ])
    raw_dir = tmp_path / "raw" / "成都"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "a_原文.md").write_text("一二三四五", encoding="utf-8")  # 5字
    (raw_dir / "b_原文.md").write_text("六七八", encoding="utf-8")  # 3字

    resp = client.get("/api/materials")
    assert resp.status_code == 200
    assert resp.json()[0]["total_word_count"] == 8


def test_list_topics_hides_lock_only_and_empty_embedding_artifacts(tmp_path):
    lock_only = tmp_path / "index" / "__不存在的主题_smoke_test__"
    lock_only.mkdir(parents=True)
    (lock_only / ".ingest.lock").touch()
    embeddings = tmp_path / "index" / "测试城市" / "embeddings"
    embeddings.mkdir(parents=True)
    (embeddings / "id_map.json").write_text("[]", encoding="utf-8")

    resp = client.get("/api/materials")

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_topics_includes_active_and_trashed_project_references(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000", "is_core": True}])
    store.init_project("active-chengdu")
    store.write_requirements("active-chengdu", "成都", {"destination": "成都", "days": 3})
    store.init_project("trashed-chengdu")
    store.write_requirements("trashed-chengdu", "成都", {"destination": "成都", "days": 2})
    store.soft_delete_project("trashed-chengdu")

    body = client.get("/api/materials").json()

    assert body[0]["active_project_ids"] == ["active-chengdu"]
    assert body[0]["trashed_project_ids"] == ["trashed-chengdu"]


def test_get_topic_entries_returns_list(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000", "title": "示例标题", "is_core": True}])

    resp = client.get("/api/materials/成都")
    assert resp.status_code == 200
    body = resp.json()
    assert body == [{"doc_id": "成都-000", "title": "示例标题", "is_core": True}]


def test_get_topic_entries_not_found():
    resp = client.get("/api/materials/不存在的目的地")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /{topic}/entries/{doc_id}：chunk文本 + raw原文
# ---------------------------------------------------------------------------


def _write_chunks(tmp_path, topic: str, chunks: list[dict]) -> None:
    topic_dir = tmp_path / "index" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    with (topic_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")


def test_get_entry_detail_includes_chunks_and_raw_text(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "title": "示例标题", "is_core": True, "source_file": "示例_原文.md"},
    ])
    _write_chunks(tmp_path, "成都", [
        {"chunk_id": "成都-000-c001", "doc_id": "成都-000", "order": 1, "text": "第二段"},
        {"chunk_id": "成都-000-c000", "doc_id": "成都-000", "order": 0, "text": "第一段"},
        {"chunk_id": "其他文档-c000", "doc_id": "成都-001", "order": 0, "text": "不该出现"},
    ])
    raw_dir = tmp_path / "raw" / "成都"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "示例_原文.md").write_text("完整原文内容", encoding="utf-8")

    resp = client.get("/api/materials/成都/entries/成都-000")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entry"]["title"] == "示例标题"
    # chunks按order排序，且只包含这个doc_id自己的
    assert [c["chunk_id"] for c in body["chunks"]] == ["成都-000-c000", "成都-000-c001"]
    assert body["raw_text"] == "完整原文内容"


def test_get_entry_detail_raw_text_none_when_source_file_missing(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000", "title": "无原文", "is_core": True}])

    resp = client.get("/api/materials/成都/entries/成都-000")
    assert resp.status_code == 200
    assert resp.json()["raw_text"] is None


def test_get_entry_detail_not_found_topic(tmp_path):
    resp = client.get("/api/materials/不存在的目的地/entries/x-000")
    assert resp.status_code == 404


def test_get_entry_detail_not_found_doc_id(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000", "title": "示例"}])
    resp = client.get("/api/materials/成都/entries/成都-999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /{topic}/entries/{doc_id} + 素材回收站：软删除/恢复/永久删除
# ---------------------------------------------------------------------------


def _seed_recyclable_material(tmp_path) -> None:
    _write_entries(tmp_path, "成都", [{
        "doc_id": "成都-000",
        "title": "武侯祠攻略",
        "platform": "bilibili",
        "source_file": "武侯祠_原文.md",
        "is_core": True,
        "entities": ["武侯祠"],
    }])
    _write_chunks(tmp_path, "成都", [
        {"chunk_id": "成都-000-c000", "doc_id": "成都-000", "order": 0, "text": "第一段"},
        {"chunk_id": "成都-000-c001", "doc_id": "成都-000", "order": 1, "text": "第二段"},
    ])
    raw_dir = tmp_path / "raw" / "成都"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "武侯祠_原文.md").write_text("完整原文", encoding="utf-8")
    (raw_dir / "武侯祠_导读.md").write_text("导读", encoding="utf-8")
    (raw_dir / "manifest.json").write_text(json.dumps({
        "topic": "成都",
        "items": [{
            "source_file": "武侯祠_原文.md",
            "summary_file": "武侯祠_导读.md",
            "platform": "bilibili",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    vector_store.add_chunks(
        "成都",
        ["成都-000-c000", "成都-000-c001"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )


def test_delete_material_moves_all_recoverable_data_to_trash(tmp_path):
    _seed_recyclable_material(tmp_path)

    resp = client.delete("/api/materials/成都/entries/成都-000")

    assert resp.status_code == 200
    assert resp.json()["status"] == "trashed"
    assert resp.json()["chunk_count"] == 2
    assert client.get("/api/materials/成都").json() == []
    assert vector_store.load_id_map("成都") == []
    assert not (tmp_path / "raw" / "成都" / "武侯祠_原文.md").exists()
    assert not (tmp_path / "raw" / "成都" / "武侯祠_导读.md").exists()

    trash = client.get("/api/trash/materials")
    assert trash.status_code == 200
    assert trash.json()[0]["doc_id"] == "成都-000"
    assert trash.json()[0]["title"] == "武侯祠攻略"


def test_restore_material_restores_entry_chunks_raw_files_manifest_and_vectors(tmp_path):
    _seed_recyclable_material(tmp_path)
    assert client.delete("/api/materials/成都/entries/成都-000").status_code == 200

    resp = client.post("/api/trash/materials/成都/成都-000/restore")

    assert resp.status_code == 200
    assert resp.json() == {"topic": "成都", "doc_id": "成都-000", "status": "restored"}
    assert client.get("/api/materials/成都").json()[0]["title"] == "武侯祠攻略"
    detail = client.get("/api/materials/成都/entries/成都-000").json()
    assert [chunk["chunk_id"] for chunk in detail["chunks"]] == ["成都-000-c000", "成都-000-c001"]
    assert detail["raw_text"] == "完整原文"
    assert (tmp_path / "raw" / "成都" / "武侯祠_导读.md").read_text(encoding="utf-8") == "导读"
    assert vector_store.load_id_map("成都") == ["成都-000-c000", "成都-000-c001"]
    manifest = json.loads((tmp_path / "raw" / "成都" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["items"][0]["source_file"] == "武侯祠_原文.md"
    assert client.get("/api/trash/materials").json() == []


def test_permanently_delete_material_removes_recovery_record(tmp_path):
    _seed_recyclable_material(tmp_path)
    assert client.delete("/api/materials/成都/entries/成都-000").status_code == 200

    resp = client.delete("/api/trash/materials/成都/成都-000")

    assert resp.status_code == 200
    assert resp.json() == {"topic": "成都", "doc_id": "成都-000", "status": "permanently_deleted"}
    assert client.get("/api/trash/materials").json() == []
    assert not (tmp_path / "raw" / "成都" / "武侯祠_原文.md").exists()


def test_material_trash_actions_return_404_for_unknown_item():
    assert client.delete("/api/materials/成都/entries/missing").status_code == 404
    assert client.post("/api/trash/materials/成都/missing/restore").status_code == 404
    assert client.delete("/api/trash/materials/成都/missing").status_code == 404


# ---------------------------------------------------------------------------
# DELETE /{topic} + 素材空间回收站
# ---------------------------------------------------------------------------


def _seed_topic_space(tmp_path, topic: str = "成都") -> None:
    _write_entries(tmp_path, topic, [{
        "doc_id": f"{topic}-000",
        "title": "空间级回收测试",
        "platform": "bilibili",
        "source_file": "空间测试_原文.md",
        "is_core": True,
        "entities": [],
    }])
    _write_chunks(tmp_path, topic, [{
        "chunk_id": f"{topic}-000-c000",
        "doc_id": f"{topic}-000",
        "order": 0,
        "text": "空间测试chunk",
    }])
    raw_dir = tmp_path / "raw" / topic
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "空间测试_原文.md").write_text("空间测试原文", encoding="utf-8")
    (raw_dir / "manifest.json").write_text(json.dumps({
        "topic": topic,
        "items": [{"source_file": "空间测试_原文.md", "platform": "bilibili"}],
    }, ensure_ascii=False), encoding="utf-8")


def test_delete_topic_is_blocked_while_active_project_references_it(tmp_path):
    _seed_topic_space(tmp_path)
    store.init_project("active-chengdu")
    store.write_requirements("active-chengdu", "成都", {"destination": "成都", "days": 3})

    resp = client.delete("/api/materials/成都")

    assert resp.status_code == 409
    assert resp.json()["detail"]["active_project_ids"] == ["active-chengdu"]
    assert (tmp_path / "index" / "成都" / "entries.jsonl").exists()


def test_delete_and_restore_topic_moves_entire_space(tmp_path):
    _seed_topic_space(tmp_path)
    store.init_project("trashed-chengdu")
    store.write_requirements("trashed-chengdu", "成都", {"destination": "成都", "days": 2})
    store.soft_delete_project("trashed-chengdu")

    deleted = client.delete("/api/materials/成都")

    assert deleted.status_code == 200
    assert deleted.json()["status"] == "trashed"
    assert deleted.json()["entry_count"] == 1
    assert deleted.json()["trashed_project_ids"] == ["trashed-chengdu"]
    assert not (tmp_path / "raw" / "成都").exists()
    assert not (tmp_path / "index" / "成都").exists()
    assert client.get("/api/materials").json() == []
    assert client.get("/api/trash/material-topics").json()[0]["topic"] == "成都"

    restored = client.post("/api/trash/material-topics/成都/restore")

    assert restored.status_code == 200
    assert restored.json() == {"topic": "成都", "status": "restored"}
    assert (tmp_path / "raw" / "成都" / "空间测试_原文.md").read_text(encoding="utf-8") == "空间测试原文"
    assert client.get("/api/materials/成都").json()[0]["doc_id"] == "成都-000"
    assert client.get("/api/trash/material-topics").json() == []


def test_topic_recycle_preserves_nested_material_trash_records(tmp_path):
    _seed_topic_space(tmp_path)
    assert client.delete("/api/materials/成都/entries/成都-000").status_code == 200
    assert len(client.get("/api/trash/materials").json()) == 1

    assert client.delete("/api/materials/成都").status_code == 200
    assert client.get("/api/trash/materials").json() == []
    assert client.get("/api/trash/material-topics").json()[0]["trashed_material_count"] == 1

    assert client.post("/api/trash/material-topics/成都/restore").status_code == 200
    assert len(client.get("/api/trash/materials").json()) == 1


def test_permanently_delete_topic_removes_recovery_record(tmp_path):
    _seed_topic_space(tmp_path)
    assert client.delete("/api/materials/成都").status_code == 200

    resp = client.delete("/api/trash/material-topics/成都")

    assert resp.status_code == 200
    assert resp.json() == {"topic": "成都", "status": "permanently_deleted"}
    assert client.get("/api/trash/material-topics").json() == []
    assert client.post("/api/trash/material-topics/成都/restore").status_code == 404


# ---------------------------------------------------------------------------
# GET /{topic}/entity_stats：跨全库实体提及次数统计
# ---------------------------------------------------------------------------


def test_entity_stats_aggregates_counts_across_entries(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "entities": ["武侯祠", "锦里"]},
        {"doc_id": "成都-001", "entities": ["武侯祠", "熊猫基地"]},
        {"doc_id": "成都-002", "entities": ["武侯祠"]},
    ])

    resp = client.get("/api/materials/成都/entity_stats")
    assert resp.status_code == 200
    body = resp.json()

    # 按count降序：武侯祠(3) > 锦里/熊猫基地(各1，顺序不强求)
    assert body[0] == {
        "entity": "武侯祠", "count": 3,
        "doc_ids": ["成都-000", "成都-001", "成都-002"],
    }
    remaining = {item["entity"]: item["count"] for item in body[1:]}
    assert remaining == {"锦里": 1, "熊猫基地": 1}


def test_entity_stats_empty_topic_returns_empty_list(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000", "entities": []}])

    resp = client.get("/api/materials/成都/entity_stats")
    assert resp.status_code == 200
    assert resp.json() == []


def test_entity_stats_not_found_topic():
    resp = client.get("/api/materials/不存在的目的地/entity_stats")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /{topic}/collect：触发采集
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_collection_graph(monkeypatch):
    fake_module = types.ModuleType("src.agents.material_agent.collection_graph")
    calls = []

    def fake_run_material_collection(topic, request, auto_confirm=True, platform="all", log=None):
        calls.append((topic, request, auto_confirm, platform))
        return {"result_summary": "采集完成"}

    fake_module.run_material_collection = fake_run_material_collection
    monkeypatch.setitem(sys.modules, "src.agents.material_agent.collection_graph", fake_module)
    return fake_module, calls


def test_trigger_collect_returns_202_and_runs(fake_collection_graph):
    fake_module, calls = fake_collection_graph

    resp = client.post("/api/materials/成都/collect", json={"request": "给我3条成都最新攻略"})
    assert resp.status_code == 202
    assert resp.json() == {"topic": "成都", "status": "running"}

    _wait_until_done("collect_material", "成都")
    assert calls == [("成都", "给我3条成都最新攻略", True, "all")]
    status = client.get("/api/materials/成都/collect/status")
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"


def test_trigger_collect_passes_platform_filter(fake_collection_graph):
    """2026-07-26新增：CollectRequest.platform要真的透传到run_material_collection，
    不能有默认值"all"就悄悄丢掉用户显式选择的平台。"""
    fake_module, calls = fake_collection_graph

    resp = client.post(
        "/api/materials/成都/collect",
        json={"request": "给我3条抖音探店视频", "platform": "douyin"},
    )
    assert resp.status_code == 202

    _wait_until_done("collect_material", "成都")
    assert calls == [("成都", "给我3条抖音探店视频", True, "douyin")]


def test_trigger_collect_rejects_invalid_platform(fake_collection_graph):
    resp = client.post(
        "/api/materials/成都/collect",
        json={"request": "随便搜点", "platform": "xiaohongshu"},
    )
    assert resp.status_code == 422


def test_trigger_collect_concurrent_returns_409(fake_collection_graph):
    started = threading.Event()
    release = threading.Event()

    def blocking(topic, request, auto_confirm=True, platform="all", log=None):
        started.set()
        release.wait(timeout=5)

    fake_collection_graph[0].run_material_collection = blocking

    resp1 = client.post("/api/materials/成都/collect", json={"request": "第一次"})
    assert resp1.status_code == 202
    started.wait(timeout=5)

    resp2 = client.post("/api/materials/成都/collect", json={"request": "第二次"})
    assert resp2.status_code == 409

    release.set()
    _wait_until_done("collect_material", "成都")


def test_collect_status_not_found_when_never_triggered():
    resp = client.get("/api/materials/从未触发过/collect/status")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /{topic}/stats：素材库统计画像（2026-08-22 grilling讨论新增）
# ---------------------------------------------------------------------------


def _write_rejected(tmp_path, topic: str, records: list[dict]) -> None:
    topic_dir = tmp_path / "index" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    with (topic_dir / "rejected.jsonl").open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_raw(tmp_path, topic: str, filename: str, content: str) -> None:
    raw_dir = tmp_path / "raw" / topic
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / filename).write_text(content, encoding="utf-8")


def test_stats_not_found_topic():
    resp = client.get("/api/materials/不存在的目的地/stats")
    assert resp.status_code == 404


def test_stats_platform_breakdown_word_count_and_longest(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "platform": "bilibili", "title": "短的", "source_file": "a.md"},
        {"doc_id": "成都-001", "platform": "bilibili", "title": "长的", "source_file": "b.md"},
        {"doc_id": "成都-002", "platform": "douyin", "title": "唯一的", "source_file": "c.md"},
    ])
    _write_raw(tmp_path, "成都", "a.md", "1234567890")  # 10字
    _write_raw(tmp_path, "成都", "b.md", "1" * 20)  # 20字
    _write_raw(tmp_path, "成都", "c.md", "1" * 5)  # 5字

    resp = client.get("/api/materials/成都/stats")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_word_count"] == 35
    # 按字数降序：bilibili(30) > douyin(5)
    bilibili, douyin = body["platform_breakdown"]
    assert bilibili == {
        "platform": "bilibili", "count": 2, "word_count": 30, "percentage": pytest.approx(85.7),
        "longest": {"doc_id": "成都-001", "title": "长的", "word_count": 20},
    }
    assert douyin == {
        "platform": "douyin", "count": 1, "word_count": 5, "percentage": pytest.approx(14.3),
        "longest": {"doc_id": "成都-002", "title": "唯一的", "word_count": 5},
    }


def test_stats_platform_breakdown_missing_raw_file_counts_as_zero(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "platform": "xiaohongshu", "title": "无原文", "source_file": "missing.md"},
    ])

    resp = client.get("/api/materials/成都/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_breakdown"] == [{
        "platform": "xiaohongshu", "count": 1, "word_count": 0, "percentage": 0.0,
        "longest": {"doc_id": "成都-000", "title": "无原文", "word_count": 0},
    }]


def test_stats_age_distribution_buckets_and_unknown_by_platform(tmp_path):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=10)).strftime("%Y-%m-%d")
    half_year = (now - timedelta(days=150)).strftime("%Y-%m-%d")
    one_year = (now - timedelta(days=300)).strftime("%Y-%m-%d")
    old = (now - timedelta(days=1000)).strftime("%Y-%m-%d")

    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "platform": "bilibili", "publish_date": recent},
        {"doc_id": "成都-001", "platform": "bilibili", "publish_date": half_year},
        {"doc_id": "成都-002", "platform": "bilibili", "publish_date": one_year},
        {"doc_id": "成都-003", "platform": "bilibili", "publish_date": old},
        {"doc_id": "成都-004", "platform": "xiaohongshu", "publish_date": ""},
        {"doc_id": "成都-005", "platform": "bilibili", "publish_date": ""},
    ])

    resp = client.get("/api/materials/成都/stats")
    assert resp.status_code == 200
    buckets = {b["bucket"]: b for b in resp.json()["age_distribution"]}

    assert list(buckets.keys()) == ["近3个月", "近半年", "近1年", "1年以上", "未知"]
    assert buckets["近3个月"] == {"bucket": "近3个月", "count": 1, "by_platform": {"bilibili": 1}}
    assert buckets["近半年"] == {"bucket": "近半年", "count": 1, "by_platform": {"bilibili": 1}}
    assert buckets["近1年"] == {"bucket": "近1年", "count": 1, "by_platform": {"bilibili": 1}}
    assert buckets["1年以上"] == {"bucket": "1年以上", "count": 1, "by_platform": {"bilibili": 1}}
    # "未知"必须按平台拆开——不能笼统合并成一个数字（见grilling讨论：小红书采集路径
    # 完全没有publish_date来源，跟"偶尔缺失"是不同性质的问题）
    assert buckets["未知"] == {
        "bucket": "未知", "count": 2, "by_platform": {"xiaohongshu": 1, "bilibili": 1},
    }


def test_stats_age_distribution_unparseable_date_counts_as_unknown(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "platform": "bilibili", "publish_date": "不是日期"},
    ])

    resp = client.get("/api/materials/成都/stats")
    buckets = {b["bucket"]: b for b in resp.json()["age_distribution"]}
    assert buckets["未知"]["count"] == 1


def test_stats_core_ratio(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "is_core": True},
        {"doc_id": "成都-001", "is_core": True},
        {"doc_id": "成都-002", "is_core": False},
    ])

    resp = client.get("/api/materials/成都/stats")
    assert resp.json()["core_ratio"] == {"core_count": 2, "non_core_count": 1}


def test_stats_author_ranking_top_and_other(tmp_path):
    # 10位不同作者各贡献1条，超过_TOP_AUTHOR_LIMIT(8)——后2位应该折进"其他"
    entries = [{"doc_id": f"成都-{i:03d}", "author": f"作者{i}"} for i in range(10)]
    _write_entries(tmp_path, "成都", entries)

    resp = client.get("/api/materials/成都/stats")
    ranking = resp.json()["author_ranking"]
    assert len(ranking["top"]) == 8
    assert ranking["other_author_count"] == 2
    assert ranking["other_entry_count"] == 2
    assert all(item["count"] == 1 for item in ranking["top"])


def test_stats_author_ranking_no_other_when_within_limit(tmp_path):
    _write_entries(tmp_path, "成都", [
        {"doc_id": "成都-000", "author": "牛油果の日常"},
        {"doc_id": "成都-001", "author": "牛油果の日常"},
        {"doc_id": "成都-002", "author": "青林知青"},
    ])

    resp = client.get("/api/materials/成都/stats")
    ranking = resp.json()["author_ranking"]
    assert {"author": "牛油果の日常", "count": 2} in ranking["top"]
    assert {"author": "青林知青", "count": 1} in ranking["top"]
    assert ranking["other_author_count"] == 0
    assert ranking["other_entry_count"] == 0


def test_stats_rejection_reason_split(tmp_path):
    _write_entries(tmp_path, "成都", [{"doc_id": "成都-000"}])
    _write_rejected(tmp_path, "成都", [
        {"source_file": "x.md", "title": "太短了", "reason": "内容过短（15字，低于20字阈值），入库前跳过"},
        {"source_file": "y.md", "title": "跑题了", "reason": "文本主要讲松潘而非成都"},
        {"source_file": "z.md", "title": "也跑题", "reason": "未提及成都，可能是其他城市景点"},
    ])

    resp = client.get("/api/materials/成都/stats")
    rejection = resp.json()["rejection_stats"]
    assert rejection["too_short_count"] == 1
    assert rejection["irrelevant_count"] == 2
    assert {"source_file": "y.md", "title": "跑题了", "reason": "文本主要讲松潘而非成都"} in rejection["irrelevant_reasons"]
    assert {"source_file": "z.md", "title": "也跑题", "reason": "未提及成都，可能是其他城市景点"} in rejection["irrelevant_reasons"]


def test_stats_empty_topic_returns_zeroed_structure(tmp_path):
    _write_entries(tmp_path, "成都", [])

    resp = client.get("/api/materials/成都/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entry_count"] == 0
    assert body["total_word_count"] == 0
    assert body["platform_breakdown"] == []
    assert body["core_ratio"] == {"core_count": 0, "non_core_count": 0}
    assert body["author_ranking"] == {"top": [], "other_author_count": 0, "other_entry_count": 0}
    assert body["rejection_stats"] == {"too_short_count": 0, "irrelevant_count": 0, "irrelevant_reasons": []}
