from __future__ import annotations

import hashlib
import json

import pytest

from src import orchestrator
from src.materials import baseline
from src.orchestrator import main_agent_router, route_map_check_node
from src.workspace import store
from src.workspace.fixed_route_setup import prepare_fixed_route_project
from src.workspace.route_validation import route_structure_snapshot
from src.workspace.schema import (
    ProjectPhase,
    RouteDay,
    RouteEntities,
    RouteEntityResolution,
    RouteExecutionPlan,
    RouteGeoPoint,
    RouteLeg,
    RoutePlan,
    RoutePoi,
)


@pytest.fixture
def isolated_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    material_root = tmp_path / "material_library"
    index_dir = material_root / "index" / "成都"
    raw_dir = material_root / "raw" / "成都"
    index_dir.mkdir(parents=True)
    raw_dir.mkdir(parents=True)
    (index_dir / "entries.jsonl").write_text(
        '{"doc_id":"doc-1","source_file":"one.md"}\n', encoding="utf-8"
    )
    (index_dir / "chunks.jsonl").write_text(
        '{"chunk_id":"chunk-1","doc_id":"doc-1","text":"成都"}\n',
        encoding="utf-8",
    )
    (raw_dir / "manifest.json").write_text(
        '{"items":[{"source_file":"one.md"}]}', encoding="utf-8"
    )
    monkeypatch.setattr(baseline, "MATERIAL_LIBRARY_DIR", material_root)
    monkeypatch.setattr(store, "MATERIAL_LIBRARY_DIR", material_root)
    return tmp_path


def _route_sha(route: dict) -> str:
    payload = json.dumps(
        route_structure_snapshot(route),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seed_frozen_source(project_id: str) -> None:
    store.init_project(project_id)
    store.write_requirements(
        project_id,
        "成都",
        {"destination": "成都", "days": 1},
    )
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    pois=[
                        RoutePoi(name="武侯祠", needs_detail=True),
                        RoutePoi(name="锦里"),
                    ],
                )
            ]
        ),
    )
    route = store.read_workspace(project_id, "route_plan.json")
    route_sha = _route_sha(route)
    store.write_fixed_route_input_snapshot(
        project_id,
        {"source": "user_confirmed_table", "days": [{"day": 1, "route": "武侯祠→锦里"}]},
    )
    store.write_route_entities(
        project_id,
        "route_agent",
        RouteEntities(
            route_plan_structure_hash=route_sha,
            entities=[
                RouteEntityResolution(
                    day=1,
                    poi_index=0,
                    formal_name="武侯祠",
                    provider_id="wuhou",
                    provider_name="成都武侯祠博物馆",
                    location=RouteGeoPoint(lng=104.04, lat=30.64),
                    status="resolved",
                ),
                RouteEntityResolution(
                    day=1,
                    poi_index=1,
                    formal_name="锦里",
                    provider_id="jinli",
                    provider_name="锦里古街",
                    location=RouteGeoPoint(lng=104.04, lat=30.64),
                    status="resolved",
                ),
            ],
        ),
    )
    store.write_route_execution(
        project_id,
        "route_agent",
        RouteExecutionPlan(
            route_plan_structure_hash=route_sha,
            route_legs=[
                RouteLeg(
                    day=1,
                    from_poi_index=0,
                    to_poi_index=1,
                    transport_mode="walking",
                    duration_minutes=10,
                )
            ],
        ),
    )
    store.write_material_baseline(
        project_id, baseline.build_material_baseline("成都", "成都")
    )


def test_shared_fixed_route_setup_creates_only_requested_harness(isolated_projects):
    _seed_frozen_source("source")
    downstream = store.workspace_dir("source") / "outputs" / "lifestyle.json"
    downstream.write_text('{"legacy": true}', encoding="utf-8")

    result = prepare_fixed_route_project(
        "source",
        "langgraph-target",
        harness="langgraph",
        model="deepseek-v4-flash-0731",
        provider="gptload",
        require_comparison_sidecars=True,
    )

    assert result["harness"] == "langgraph"
    assert result["missing_comparison_sidecars"] == []
    assert not store.project_dir("codex-target").exists()
    assert store.read_workspace("langgraph-target", "outputs/lifestyle.json") is None
    assert store.read_workspace("langgraph-target", "fixed_route_input_snapshot.json")
    assert store.read_route_entities("langgraph-target").entities[0].provider_id == "wuhou"
    assert store.read_route_execution("langgraph-target").route_legs[0].transport_mode == "walking"
    imported_day = store.read_route_plan("langgraph-target").days[0]
    assert [claim.support_mode for claim in imported_day.claims] == [
        "user_constraint",
        "user_constraint",
    ]
    assert [claim.target_path for claim in imported_day.claims] == [
        "pois.0.name",
        "pois.1.name",
    ]
    assert all(claim.claim_id.startswith("route-claim-") for claim in imported_day.claims)
    manifest = store.read_workspace("langgraph-target", "experiment.json")
    assert manifest["harness"] == "langgraph"
    assert manifest["provider"] == "gptload"


def test_strict_fixed_route_setup_fails_before_creating_partial_target(isolated_projects):
    store.init_project("source")
    store.write_requirements("source", "成都", {"destination": "成都", "days": 1})
    store.write_route_plan(
        "source",
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])]),
    )

    with pytest.raises(FileNotFoundError, match="对照输入不完整"):
        prepare_fixed_route_project(
            "source",
            "target",
            harness="codex_skill",
            require_comparison_sidecars=True,
        )

    assert not store.project_dir("target").exists()


def test_grade_a_lower_api_rejects_truthy_but_unbound_preflight_receipt(
    isolated_projects,
):
    _seed_frozen_source("grade-a-source")

    with pytest.raises(ValueError, match="schema_version_mismatch"):
        prepare_fixed_route_project(
            "grade-a-source",
            "grade-a-target",
            harness="codex_skill",
            model="gpt-5.6-sol",
            comparison_grade="A",
            require_comparison_sidecars=True,
            preflight_receipt={"ready": True},
        )

    assert not store.project_dir("grade-a-target").exists()


def test_langgraph_router_places_map_gate_before_feasibility(isolated_projects):
    _seed_frozen_source("route-order")
    store.update_progress(
        "route-order",
        actor="main_agent",
        state=ProjectPhase.PARALLEL_RESEARCH,
        skeleton_confirmed=True,
    )
    state = {
        "project_id": "route-order",
        "topic": "成都",
        "mode": "plan",
        "route_citation_checked": True,
    }
    assert main_agent_router(state) == "route_map_check_node"
    state["route_map_checked"] = True
    assert main_agent_router(state) == "route_timeliness_check_node"


def test_route_map_gate_reuses_current_report_without_provider_calls(
    isolated_projects, monkeypatch
):
    _seed_frozen_source("map-reuse")
    route = store.read_workspace("map-reuse", "route_plan.json")
    store.write_route_map_validation(
        "map-reuse",
        {
            "route_plan_structure_hash": _route_sha(route),
            "status": "passed",
            "issues": [],
            "days": [],
        },
    )
    monkeypatch.setattr(
        orchestrator.route_map_check,
        "verify_route_map",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("当前路线报告不应重复调用高德")
        ),
    )

    result = route_map_check_node({"project_id": "map-reuse", "topic": "成都"})

    assert result["route_map_checked"] is True
    assert store.read_validation_runs("map-reuse")[-1]["reused_current_report"] is True
