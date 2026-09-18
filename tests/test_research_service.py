import hashlib

import pytest

from src.materials import research_service
from src.workspace import store


@pytest.fixture
def research_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "research-project"
    store.init_project(project_id)
    store.write_requirements(
        project_id,
        "青海",
        {"destination": "青海", "days": 2},
    )
    store.write_material_baseline(
        project_id,
        {"schema_version": "material-baseline-v1", "corpus_sha256": "base-sha"},
    )
    return project_id


def _research_run(
    *,
    stage: str,
    target_key: str,
    target_sha256: str | None = None,
) -> dict:
    return {
        "schema_version": "research-run-v1",
        "research_id": f"{stage}-{target_key}-{target_sha256 or 'none'}",
        "source_kind": "material",
        "stage": stage,
        "target_key": target_key,
        "target_sha256": target_sha256,
        "status": "success",
        "result_count": 1,
        "result_scopes": {"global_core": 1},
        "completed_at": "2026-09-05T00:00:00+00:00",
    }


def test_retrieve_context_unifies_ugc_scopes_without_project_evidence(
    research_project, monkeypatch
):
    def fake_retrieve(topic, query, top_k, scopes):
        assert [scope for scope, _root in scopes] == [
            "global_core",
            "project_supplement",
        ]
        return (
            [
                {
                    "platform": "bilibili",
                    "doc_id": "global-doc",
                    "chunk_id": "global-chunk",
                    "content_snippet": "global material",
                    "scope": "global_core",
                },
                {
                    "platform": "bilibili",
                    "doc_id": "overlay-doc",
                    "chunk_id": "overlay-chunk",
                    "content_snippet": "overlay material",
                    "scope": "project_supplement",
                },
            ],
            {
                "status": "success",
                "modality_errors": {},
                "result_scopes": {"global_core": 1, "project_supplement": 1},
            },
        )

    monkeypatch.setattr(research_service, "retrieve_materials_union", fake_retrieve)
    monkeypatch.setattr(
        research_service,
        "compute_material_snapshot",
        lambda *args, **kwargs: {"corpus_sha256": "overlay-sha"},
    )

    result = research_service.retrieve_context(
        research_project,
        actor="travel-lifestyle",
        stage="lifestyle",
        target_key="lifestyle:day:1",
        query="青海湖怎么玩",
        top_k=5,
    )

    assert [item["scope"] for item in result["results"]] == [
        "global_core",
        "project_supplement",
    ]
    assert all(item["evidence_ref"]["chunk_id"] for item in result["results"])
    run = store.read_research_runs(research_project)[0]
    assert run["base_corpus_sha256"] == "base-sha"
    assert run["overlay_sha256"] == "overlay-sha"
    assert run["result_scopes"] == {
        "global_core": 1,
        "project_supplement": 1,
    }
    assert run["channel"] == "ugc"


def test_retrieve_context_keeps_bm25_results_when_embedding_fails(
    research_project, monkeypatch
):
    monkeypatch.setattr(
        research_service,
        "retrieve_materials_union",
        lambda *args, **kwargs: (
            [
                {
                    "platform": "bilibili",
                    "doc_id": "doc-2",
                    "chunk_id": "chunk-2",
                    "content_snippet": "BM25 result",
                    "scope": "global_core",
                }
            ],
            {
                "status": "degraded",
                "modality_errors": {"embedding": {"type": "APIConnectionError"}},
                "result_scopes": {"global_core": 1, "project_supplement": 0},
            },
        ),
    )
    monkeypatch.setattr(
        research_service,
        "compute_material_snapshot",
        lambda *args, **kwargs: {"corpus_sha256": "overlay-sha"},
    )

    result = research_service.retrieve_context(
        research_project,
        actor="poi_detail_agent",
        stage="poi_detail",
        target_key="poi_detail:day:1:poi:青海湖",
        query="青海湖内部路线",
    )

    assert [item["scope"] for item in result["results"]] == ["global_core"]
    assert result["research"]["status"] == "degraded"
    assert result["research"]["scope_errors"]["ugc"]["embedding"]


def test_grade_a_codex_retrieval_persists_ledger_then_blocks_systemic_embedding_failure(
    research_project, monkeypatch
):
    store.write_experiment_manifest(
        research_project,
        {
            "scenario_id": "strict-embedding",
            "experiment_version": research_project,
            "harness": "codex_skill",
            "comparison_grade": "A",
            "model": "gpt-5.6-sol",
        },
    )
    monkeypatch.setattr(
        research_service,
        "retrieve_materials_union",
        lambda *args, **kwargs: (
            [
                {
                    "platform": "bilibili",
                    "doc_id": "bm25-doc",
                    "chunk_id": "bm25-chunk",
                    "content_snippet": "BM25 fallback result",
                    "scope": "global_core",
                }
            ],
            {
                "status": "degraded",
                "available_modalities": ["bm25"],
                "modality_errors": {
                    "embedding": {"type": "APIConnectionError"}
                },
                "result_scopes": {"global_core": 1, "project_supplement": 0},
            },
        ),
    )
    monkeypatch.setattr(
        research_service,
        "compute_material_snapshot",
        lambda *args, **kwargs: {"corpus_sha256": "overlay-sha"},
    )

    with pytest.raises(
        research_service.StrictEmbeddingRuntimeError,
        match="grade_a_embedding_runtime_unavailable",
    ):
        research_service.retrieve_context(
            research_project,
            actor="lifestyle_agent",
            stage="lifestyle",
            target_key="lifestyle:day:1",
            query="青海湖怎么玩",
        )

    run = store.read_research_runs(research_project)[0]
    assert run["status"] == "degraded"
    assert run["result_count"] == 1
    assert run["technical_block"] == {
        "status": "blocked",
        "code": "grade_a_embedding_runtime_unavailable",
        "modality": "embedding",
        "available_modalities": ["bm25"],
        "message": (
            "Grade A正式运行的Embedding通道在预检后系统性失效；"
            "已保留BM25结果与Research Ledger，但本工作单元不能继续。"
        ),
    }


def test_grade_a_codex_retrieval_keeps_running_when_one_vector_scope_fails(
    research_project, monkeypatch
):
    store.write_experiment_manifest(
        research_project,
        {
            "scenario_id": "strict-embedding",
            "experiment_version": research_project,
            "harness": "codex_skill",
            "comparison_grade": "A",
            "model": "gpt-5.6-sol",
        },
    )
    monkeypatch.setattr(
        research_service,
        "retrieve_materials_union",
        lambda *args, **kwargs: (
            [],
            {
                "status": "degraded",
                "available_modalities": ["embedding", "bm25"],
                "modality_errors": {
                    "embedding_scopes": {
                        "project_supplement": {"type": "FileNotFoundError"}
                    }
                },
                "result_scopes": {"global_core": 0, "project_supplement": 0},
            },
        ),
    )
    monkeypatch.setattr(
        research_service,
        "compute_material_snapshot",
        lambda *args, **kwargs: {"corpus_sha256": "overlay-sha"},
    )

    result = research_service.retrieve_context(
        research_project,
        actor="lifestyle_agent",
        stage="lifestyle",
        target_key="lifestyle:day:1",
        query="青海湖怎么玩",
    )

    assert result["research"]["status"] == "degraded"
    assert "technical_block" not in result["research"]


def test_grade_a_diagnostic_continuation_retains_bm25_degradation(
    research_project, monkeypatch
):
    store.write_experiment_manifest(
        research_project,
        {
            "scenario_id": "strict-embedding",
            "experiment_version": research_project,
            "harness": "codex_skill",
            "comparison_grade": "A",
            "model": "gpt-5.6-sol",
        },
    )
    trace_path = (
        store.project_dir(research_project)
        / "agents"
        / "codex_skill"
        / "trace.jsonl"
    )
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        '{"event":"run_start","status":"diagnostic_continuation",'
        '"continuation_mode":"post_fix_contaminated_continuation"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        research_service,
        "retrieve_materials_union",
        lambda *args, **kwargs: (
            [
                {
                    "platform": "bilibili",
                    "doc_id": "bm25-doc",
                    "chunk_id": "bm25-chunk",
                    "content_snippet": "BM25 fallback result",
                    "scope": "global_core",
                }
            ],
            {
                "status": "degraded",
                "available_modalities": ["bm25"],
                "modality_errors": {
                    "embedding": {"type": "APIConnectionError"}
                },
                "result_scopes": {"global_core": 1, "project_supplement": 0},
            },
        ),
    )
    monkeypatch.setattr(
        research_service,
        "compute_material_snapshot",
        lambda *args, **kwargs: {"corpus_sha256": "overlay-sha"},
    )

    result = research_service.retrieve_context(
        research_project,
        actor="lifestyle_agent",
        stage="lifestyle",
        target_key="lifestyle:day:1",
        query="青海湖怎么玩",
    )

    assert result["research"]["status"] == "degraded"
    assert "technical_block" not in result["research"]


def test_project_evidence_retrieval_is_explicit_and_log_scope_bound(
    research_project, monkeypatch
):
    captured = {}

    def fake_project_retrieve(project_id, query, top_k, *, log_scope=None):
        captured["log_scope"] = log_scope
        return [
            {
                "platform": "project_evidence",
                "doc_id": "note-2",
                "chunk_id": "project-evidence-note-2",
                "content_snippet": "official current route",
                "source_url": "https://example.com/route",
                "scope": "project",
            }
        ]

    monkeypatch.setattr(research_service, "retrieve_project_evidence", fake_project_retrieve)
    result = research_service.retrieve_project_evidence_context(
        research_project,
        actor="poi_detail_agent",
        stage="poi_detail",
        target_key="poi_detail:day:1:poi:青海湖",
        log_scope="poi_detail_agent/day1_青海湖",
        query="青海湖 开放",
    )

    assert captured["log_scope"] == "poi_detail_agent/day1_青海湖"
    assert result["research"]["channel"] == "web_evidence"
    assert result["results"][0]["scope"] == "project"


def test_research_coverage_is_bound_to_day_item_sha_and_exact_poi(research_project):
    workspace = store.workspace_dir(research_project)
    (workspace / "route_plan.json").write_text(
        '{"days":[{"day":1,"pois":[{"name":"青海湖","needs_detail":true}]},'
        '{"day":2,"pois":[{"name":"茶卡","needs_detail":false}]}]}',
        encoding="utf-8",
    )
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    (workspace / "outputs" / "lifestyle.json").write_text(
        '{"food":{"items":[{"day":1,"recommendation":"青海湖附近午餐"}]},'
        '"outfit":{"items":[]},"lodging":{"items":[]},"play":{"items":[]}}',
        encoding="utf-8",
    )
    store.append_research_run(
        research_project,
        _research_run(stage="lifestyle", target_key="lifestyle:day:1"),
    )

    lifestyle_issues = research_service.research_coverage_issues(
        research_project, "lifestyle"
    )
    assert lifestyle_issues[0]["missing_days"] == [2]

    store.append_research_run(
        research_project,
        _research_run(
            stage="pitfall",
            target_key="pitfall:food:day:1",
            target_sha256="stale-sha",
        ),
    )
    pitfall_issues = research_service.research_coverage_issues(
        research_project, "pitfall"
    )
    assert pitfall_issues[0]["stale"] == [["food", 1]]

    current_sha = hashlib.sha256("青海湖附近午餐".encode("utf-8")).hexdigest()
    store.append_research_run(
        research_project,
        _research_run(
            stage="pitfall",
            target_key="pitfall:food:day:1",
            target_sha256=current_sha,
        ),
    )
    assert research_service.research_coverage_issues(research_project, "pitfall") == []

    assert research_service.research_coverage_issues(
        research_project, "poi_detail", day=1, poi_name="青海湖"
    )[0]["code"] == "poi_detail_research_missing"
    store.append_research_run(
        research_project,
        _research_run(
            stage="poi_detail",
            target_key="poi_detail:day:1:poi:青海湖",
        ),
    )
    assert research_service.research_coverage_issues(
        research_project, "poi_detail", day=1, poi_name="青海湖"
    ) == []


def test_research_service_rejects_topic_destination_mismatch(research_project):
    store.write_requirements(
        research_project,
        "青海素材",
        {"destination": "青海", "days": 2},
    )

    with pytest.raises(ValueError, match="material_topic_mismatch"):
        research_service.retrieve_context(
            research_project,
            actor="travel-lifestyle",
            stage="lifestyle",
            target_key="lifestyle:day:1",
            query="青海湖",
        )
