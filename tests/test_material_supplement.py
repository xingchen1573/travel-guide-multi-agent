import json

from src.materials import supplement
from src.materials.collectors import bilibili
from src.workspace import store


def test_supplement_writes_only_project_overlay_and_records_indexed_result(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(supplement, "MATERIAL_LIBRARY_DIR", tmp_path / "global-library")
    project_id = "overlay-isolation"
    store.init_project(project_id)
    store.write_requirements(
        project_id,
        "青甘大环线",
        {"destination": "青甘大环线", "days": 8},
    )
    monkeypatch.setattr(bilibili, "check_collector_dependencies", lambda: None)
    monkeypatch.setattr(
        bilibili,
        "search_and_rank",
        lambda keywords, top_n: [
            {
                "title": "青海湖景区游览路线",
                "author": "旅行作者",
                "source_url": "https://www.bilibili.com/video/BV1",
            }
        ],
    )
    monkeypatch.setattr(bilibili, "_load_existing_source_urls", lambda *a, **k: set())

    calls = {}

    def fake_collect(topic, candidate, *, library_dir=None):
        calls["collect_dir"] = library_dir
        return {"source_file": "qinghai-lake.md"}

    def fake_ingest(topic, *, library_dir=None, doc_id_prefix=""):
        calls["ingest_dir"] = library_dir
        calls["doc_id_prefix"] = doc_id_prefix
        return []

    def fake_status(topic, source_files, *, library_dir=None):
        calls["status_dir"] = library_dir
        return {"qinghai-lake.md": "indexed"}

    monkeypatch.setattr(bilibili, "collect_one", fake_collect)
    overlay_dir = store.project_material_library_dir(project_id)
    result = supplement.attempt_auto_material_supplement(
        project_id,
        "青甘大环线",
        ["青海湖 游览路线"],
        ["青海湖 游览路线"],
        stage="poi_detail",
        target_key="poi_detail:day:1:poi:青海湖",
        repair_scope={"day": 1, "poi_name": "青海湖"},
        limit=2,
        gap_checker=lambda *args, **kwargs: True,
        candidate_filter=lambda topic, entity, candidates: candidates,
        ingest_fn=fake_ingest,
        processing_status_fn=fake_status,
    )

    assert result.status == "indexed"
    assert result.as_dict()["repair_granted"] is True
    assert calls["collect_dir"] == overlay_dir
    assert calls["ingest_dir"] == overlay_dir
    assert calls["status_dir"] == overlay_dir
    assert calls["doc_id_prefix"].startswith("project-supplement-")
    assert store.read_material_gap_supplement_count(project_id) == 1
    assert not (tmp_path / "global-library").exists()
    audit = json.loads(
        (store.workspace_dir(project_id) / "material_gap_supplement.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit["items"][0]["overlay"] is True
    assert store.read_research_runs(project_id)[0]["source_kind"] == "supplement"


def test_supplement_does_not_grant_repair_when_base_drifts_during_collection(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    project_id = "base-drift"
    store.init_project(project_id)
    store.write_requirements(
        project_id,
        "青甘大环线",
        {"destination": "青甘大环线", "days": 8},
    )
    identities = iter(
        [
            ("base-sha", "overlay-before", "passed"),
            ("base-sha", "overlay-after", "material_baseline_changed"),
        ]
    )
    monkeypatch.setattr(supplement, "_base_identity", lambda *args: next(identities))
    monkeypatch.setattr(
        supplement,
        "compute_material_snapshot",
        lambda *args, **kwargs: {"corpus_sha256": "overlay-after"},
    )
    monkeypatch.setattr(bilibili, "check_collector_dependencies", lambda: None)
    monkeypatch.setattr(
        bilibili,
        "search_and_rank",
        lambda keywords, top_n: [
            {
                "title": "青海湖景区游览路线",
                "author": "旅行作者",
                "source_url": "https://www.bilibili.com/video/BV2",
            }
        ],
    )
    monkeypatch.setattr(bilibili, "_load_existing_source_urls", lambda *a, **k: set())
    monkeypatch.setattr(
        bilibili,
        "collect_one",
        lambda *args, **kwargs: {"source_file": "qinghai-lake.md"},
    )

    result = supplement.attempt_auto_material_supplement(
        project_id,
        "青甘大环线",
        ["青海湖 游览路线"],
        ["青海湖 游览路线"],
        stage="poi_detail",
        target_key="poi_detail:day:1:poi:青海湖",
        repair_scope={"day": 1, "poi_name": "青海湖"},
        limit=1,
        gap_checker=lambda *args, **kwargs: True,
        candidate_filter=lambda topic, entity, candidates: candidates,
        ingest_fn=lambda *args, **kwargs: [],
        processing_status_fn=lambda *args, **kwargs: {"qinghai-lake.md": "indexed"},
    )

    assert result.status == "blocked"
    assert result.blocked_reason == "material_baseline_changed"
    assert result.as_dict()["repair_granted"] is False
    assert result.indexed[0]["title"] == "青海湖景区游览路线"
