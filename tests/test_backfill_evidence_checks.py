import json

from scripts.maintenance import backfill_evidence_checks
from src.materials import citation_check
from src.workspace import store
from src.workspace.schema import RouteDay, RoutePlan


def test_deterministic_backfill_records_current_state_without_model(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    monkeypatch.setattr(
        citation_check,
        "get_chat_model",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )

    project_id = "historical-project"
    topic = "测试目的地"
    store.init_project(project_id)
    store.write_requirements(project_id, topic, {"destination": topic})
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="测试城市",
                    pois=[{"name": "真实景点"}, {"name": "别名景点"}],
                    sources=[{"platform": "bilibili", "chunk_id": "route-c1"}],
                )
            ]
        ),
    )
    topic_dir = material_dir / "index" / topic
    topic_dir.mkdir(parents=True)
    (topic_dir / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "route-c1", "text": "真实景点是当地的主要景点。"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = backfill_evidence_checks.backfill_project(project_id)

    assert result["errors"] == []
    checks = store.read_evidence_checks(project_id)["route"]
    by_scope = {check["scope"]: check for check in checks}
    assert by_scope["source_availability"]["result"] == "pass"
    assert by_scope["entity_identity"]["result"] == "not_checked"
    assert by_scope["entity_identity"]["method"] == "model_checks_disabled"
    assert by_scope["entity_identity"]["observation_mode"] == "backfill_current_state"
    assert by_scope["substantive_claim"]["result"] == "not_checked"
