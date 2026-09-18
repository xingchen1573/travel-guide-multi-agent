import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import src.codex_skill.bridge as bridge
from src.codex_skill.bridge import (
    accept_route_risks,
    amap_driving_distance,
    amap_resolve_route_entity,
    append_trace,
    build_resume_plan,
    finalize_route_only,
    freeze_material_baseline,
    integration_delivery_inputs,
    prepare_fixed_route,
    prepare_route_only,
    reconcile_zero_claim_validation_from_trace,
    route_structure_sha256,
    validate_stage,
    write_route_facts_payload,
    write_route_plan_payload,
    write_stage_payload,
)
from src.materials.web_evidence import build_web_search_log_entry
from src.materials import (
    baseline,
    citation_check,
    project_evidence,
    route_feasibility_check,
    route_map_check,
)
from src.workspace import store
from src.workspace.schema import (
    PoiDetailFile,
    PoiDetailManifest,
    PoiDetailManifestEntry,
    RouteDay,
    RouteEntities,
    RouteEntityResolution,
    RouteExecutionPlan,
    RouteFeasibilityCheck,
    RouteFeasibilityReport,
    RoutePlan,
    RoutePoi,
    RouteOption,
    RouteSegment,
)


@pytest.fixture
def isolated_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(bridge, "RUNTIME_DIR", tmp_path / "runtime")
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
    return tmp_path


def _seed_source(project_id: str) -> None:
    store.init_project(project_id)
    store.write_requirements(
        project_id,
        "成都",
        {
            "destination": "成都",
            "days": 1,
            "style": "美食+人文，不想太赶",
            "notes": "不吃辣，喜欢拍照",
        },
    )
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    area="市中心",
                    pois=[RoutePoi(name="武侯祠", needs_detail=True)],
                    notes="按已确认路线游览。",
                )
            ],
            amap_verified=True,
        ),
    )


class _FakeBridgeTool:
    def __init__(self, name, result, calls):
        self.name = name
        self._result = result
        self._calls = calls

    def invoke(self, payload):
        self._calls.append((self.name, payload))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _route_only_payload(poi_name: str = "武侯祠") -> dict:
    return {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "area": "市中心",
                "pois": [{"name": poi_name}],
                "notes": "按用户偏好安排。",
                "claims": [
                    {
                        "target_path": "pois.0.name",
                        "claim_text": poi_name,
                        "claim_type": "poi_identity",
                        "support_mode": "unverified",
                    },
                    {
                        "target_path": "pois.0.name",
                        "claim_text": f"{poi_name}符合用户偏好，因此作为本日核心POI。",
                        "claim_type": "poi_selection",
                        "support_mode": "user_constraint",
                    },
                    {
                        "target_path": "notes",
                        "claim_text": "按用户偏好安排。",
                        "claim_type": "planning",
                        "support_mode": "user_constraint",
                    },
                ],
            }
        ]
    }


def test_route_amap_bridge_uses_route_research_context(
    isolated_projects, monkeypatch
):
    store.init_project("route-amap-source")
    store.write_requirements(
        "route-amap-source",
        "成都",
        {"destination": "成都", "days": 3},
    )
    prepare_route_only("route-amap-source", "route-amap")
    calls = []
    construction = {}

    def fake_make_amap_tools(topic, **kwargs):
        construction.update({"topic": topic, **kwargs})
        return [
            _FakeBridgeTool(
                "amap_resolve_route_entity",
                '{"status":"resolved","selected":{"provider_id":"poi-1"}}',
                calls,
            ),
            _FakeBridgeTool(
                "amap_driving_distance",
                "约 2 公里 / 0.2 小时（高德驾车实测）",
                calls,
            ),
        ]

    monkeypatch.setattr(bridge, "make_amap_tools", fake_make_amap_tools)

    entity = amap_resolve_route_entity(
        "route-amap",
        poi_name="武侯祠",
        city_hint="成都",
    )
    distance = amap_driving_distance(
        "route-amap",
        poi_a="武侯祠",
        poi_b="锦里古街",
        city="成都",
    )

    assert construction["topic"] == "成都"
    assert construction["project_id"] == "route-amap"
    assert construction["log_scope"] == "route_agent"
    assert construction["include_entity_resolution"] is True
    assert construction["research_context"] == {
        "actor": "route_agent",
        "stage": "route",
        "target_key": "route:all",
    }
    assert entity["result"]["selected"]["provider_id"] == "poi-1"
    assert distance["query"]["day"] is None
    assert calls == [
        (
            "amap_resolve_route_entity",
            {"poi_name": "武侯祠", "city_hint": "成都"},
        ),
        (
            "amap_driving_distance",
            {
                "poi_a": "武侯祠",
                "poi_b": "锦里古街",
                "city": "成都",
                "day": None,
            },
        ),
    ]


def test_route_amap_bridge_rejects_non_route_only_project_before_tool_build(
    isolated_projects, monkeypatch
):
    store.init_project("source-only")
    store.write_requirements(
        "source-only",
        "成都",
        {"destination": "成都", "days": 3},
    )
    monkeypatch.setattr(
        bridge,
        "make_amap_tools",
        lambda *args, **kwargs: pytest.fail("Amap tools should not be constructed"),
    )

    with pytest.raises(PermissionError, match="requirements-only Codex目标Project"):
        amap_resolve_route_entity(
            "source-only",
            poi_name="武侯祠",
            city_hint="成都",
        )


@pytest.mark.parametrize(
    "tool_result,expected_error",
    [
        ("not-json", "无效JSON"),
        (RuntimeError("provider unavailable"), "provider unavailable"),
    ],
)
def test_route_amap_entity_failures_are_single_call_and_fail_closed(
    isolated_projects,
    monkeypatch,
    tool_result,
    expected_error,
):
    _seed_source("amap-failure-source")
    prepare_route_only("amap-failure-source", "amap-failure-target")
    calls = []
    monkeypatch.setattr(
        bridge,
        "make_amap_tools",
        lambda *args, **kwargs: [
            _FakeBridgeTool("amap_resolve_route_entity", tool_result, calls)
        ],
    )

    with pytest.raises((ValueError, RuntimeError), match=expected_error):
        amap_resolve_route_entity(
            "amap-failure-target",
            poi_name="武侯祠",
            city_hint="成都市",
        )

    assert calls == [
        (
            "amap_resolve_route_entity",
            {"poi_name": "武侯祠", "city_hint": "成都市"},
        )
    ]


def test_route_amap_bridge_preserves_non_chengdu_destination(
    isolated_projects, monkeypatch
):
    store.init_project("hangzhou-source")
    store.write_requirements(
        "hangzhou-source",
        "杭州",
        {"destination": "杭州", "days": 1},
    )
    prepare_route_only("hangzhou-source", "hangzhou-target")
    calls = []
    construction = {}

    def fake_make_amap_tools(topic, **kwargs):
        construction.update({"topic": topic, **kwargs})
        return [
            _FakeBridgeTool(
                "amap_resolve_route_entity",
                '{"status":"resolved","selected":{"provider_id":"west-lake"}}',
                calls,
            )
        ]

    monkeypatch.setattr(bridge, "make_amap_tools", fake_make_amap_tools)

    result = amap_resolve_route_entity(
        "hangzhou-target",
        poi_name="西湖风景名胜区",
        city_hint="杭州市",
    )

    assert construction["topic"] == "杭州"
    assert result["result"]["selected"]["provider_id"] == "west-lake"
    assert calls == [
        (
            "amap_resolve_route_entity",
            {"poi_name": "西湖风景名胜区", "city_hint": "杭州市"},
        )
    ]


def _mark_grade_a(project_id: str) -> None:
    freeze_material_baseline(project_id)
    store.write_experiment_manifest(
        project_id,
        {
            "scenario_id": "strict-research",
            "experiment_version": project_id,
            "harness": "codex_skill",
            "comparison_grade": "A",
            "model": "gpt-5.6-sol",
        },
    )


def _append_material_research(
    project_id: str,
    *,
    stage: str,
    target_key: str,
    target_sha256: str | None = None,
) -> None:
    store.append_research_run(
        project_id,
        {
            "schema_version": "research-run-v1",
            "research_id": f"{stage}-{target_key}",
            "source_kind": "material",
            "stage": stage,
            "target_key": target_key,
            "target_sha256": target_sha256,
            "status": "success",
            "result_count": 1,
            "result_scopes": {"global_core": 1},
            "completed_at": "2026-09-05T00:00:00+00:00",
        },
    )


def test_prepare_route_only_copies_requirements_without_route_or_downstream(isolated_projects):
    _seed_source("source")
    legacy_path = store.workspace_dir("source") / "outputs" / "lifestyle.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"legacy": true}', encoding="utf-8")

    result = prepare_route_only("source", "target")

    assert result["mode"] == "full_plan_route_only"
    assert result["route_plan_inherited"] is False
    assert store.read_requirements("target")["topic"] == "成都"
    assert store.read_workspace("target", "route_plan.json") is None
    assert store.read_workspace("target", "outputs/lifestyle.json") is None
    progress = store.read_workspace("target", "progress.json")
    assert progress["state"] == "路线规划中"
    assert progress["skeleton_confirmed"] is False


def test_write_route_plan_payload_stops_at_confirmation_gate(isolated_projects):
    _seed_source("source")
    prepare_route_only("source", "target")

    result = write_route_plan_payload(
        "target",
        {
            "days": [
                {
                    "day": 1,
                    "city": "成都",
                    "area": "市中心",
                    "pois": [
                        {
                            "name": "武侯祠",
                            "brief": "",
                            "needs_detail": True,
                            "needs_detail_reason": "大型文化景区",
                        }
                    ],
                    "notes": "根据用户偏好安排。",
                    "claims": [
                            {
                                "target_path": "pois.0.name",
                                "subject": "武侯祠",
                                "claim_text": "武侯祠",
                            "claim_type": "poi_identity",
                            "support_mode": "unverified",
                                "claimed_entities": ["武侯祠"],
                                "evidence_refs": [],
                            },
                            {
                                "target_path": "pois.0.name",
                                "subject": "武侯祠",
                                "claim_text": "武侯祠符合用户的人文偏好，因此安排为本日核心POI。",
                                "claim_type": "poi_selection",
                                "support_mode": "user_constraint",
                                "claimed_entities": ["武侯祠"],
                                "evidence_refs": [],
                            },
                        {
                            "target_path": "pois.0.needs_detail_reason",
                            "subject": "武侯祠",
                            "claim_text": "大型文化景区",
                            "claim_type": "planning",
                            "support_mode": "estimate",
                            "claimed_entities": ["武侯祠"],
                            "evidence_refs": [],
                        },
                        {
                            "target_path": "notes",
                            "subject": "行程节奏",
                            "claim_text": "根据用户偏好安排。",
                            "claim_type": "planning",
                            "support_mode": "user_constraint",
                            "claimed_entities": [],
                            "evidence_refs": [],
                        }
                    ],
                }
            ]
        },
    )

    assert result["status"] == "awaiting_user_confirmation"
    assert result["skeleton_confirmed"] is False
    assert result["route_structure_sha256"]
    progress = store.read_workspace("target", "progress.json")
    assert progress["state"] == "骨架确认中"
    assert progress["skeleton_confirmed"] is False


def test_write_route_plan_payload_rejects_wrong_day_count_before_write(isolated_projects):
    _seed_source("source")
    prepare_route_only("source", "target")

    with pytest.raises(ValueError, match="day_count_mismatch"):
        write_route_plan_payload(
            "target",
            {
                "days": [
                    {"day": 1, "city": "成都", "pois": [{"name": "武侯祠"}]},
                    {"day": 2, "city": "成都", "pois": [{"name": "杜甫草堂"}]},
                ]
            },
        )

    assert store.read_workspace("target", "route_plan.json") is None


def test_write_route_plan_payload_requires_separate_poi_selection(isolated_projects):
    _seed_source("source")
    prepare_route_only("source", "target")

    with pytest.raises(ValueError, match="缺少poi_selection"):
        write_route_plan_payload(
            "target",
            {
                "days": [
                    {
                        "day": 1,
                        "city": "成都",
                        "pois": [{"name": "武侯祠"}],
                        "claims": [
                            {
                                "target_path": "pois.0.name",
                                "claim_text": "武侯祠",
                                "claim_type": "poi_identity",
                                "support_mode": "unverified",
                            }
                        ],
                    }
                ]
            },
        )

    assert store.read_workspace("target", "route_plan.json") is None


def test_write_route_plan_payload_persists_entity_and_execution_sidecars(isolated_projects):
    _seed_source("source")
    prepare_route_only("source", "target")

    result = write_route_plan_payload(
        "target",
        {
            "days": [
                {
                    "day": 1,
                    "city": "成都",
                    "pois": [{"name": "武侯祠"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "武侯祠",
                            "claim_type": "poi_identity",
                            "support_mode": "unverified",
                        },
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "武侯祠符合用户的人文偏好，因此作为本日核心POI。",
                            "claim_type": "poi_selection",
                            "support_mode": "user_constraint",
                        },
                    ],
                }
            ],
            "route_entities": {
                "entities": [
                    {
                        "day": 1,
                        "poi_index": 0,
                        "formal_name": "武侯祠",
                        "provider_query": "成都武侯祠博物馆",
                        "provider_id": "wuhouci",
                        "provider_name": "成都武侯祠博物馆",
                        "province": "四川省",
                        "city": "成都市",
                        "district": "武侯区",
                        "location": {"lng": 104.04, "lat": 30.64},
                        "confidence": 0.98,
                        "status": "resolved",
                    }
                ]
            },
            "route_execution": {
                "operational_commitments": [
                    {
                        "action": "出发前复核开放公告",
                        "category": "opening_recheck",
                        "target_day": 1,
                        "target_poi_index": 0,
                        "target_name": "武侯祠",
                    }
                ]
            },
        },
    )

    assert result["route_entities_written"] is True
    assert result["route_execution_written"] is True
    assert store.read_route_entities("target").entities[0].provider_id == "wuhouci"
    execution = store.read_route_execution("target")
    assert execution.operational_commitments[0].target_name == "武侯祠"
    assert execution.operational_commitments[0].commitment_id.startswith("route-commitment-")


def test_write_route_plan_payload_same_sha_retry_only_fills_missing_sidecars(
    isolated_projects,
):
    _seed_source("retry-source")
    prepare_route_only("retry-source", "retry-target")
    write_route_plan_payload("retry-target", _route_only_payload())
    route_path = store.workspace_dir("retry-target") / "route_plan.json"
    original_route_bytes = route_path.read_bytes()
    retry_payload = {
        **_route_only_payload(),
        "route_entities": {"entities": []},
        "route_execution": {"route_legs": []},
    }

    recovered = write_route_plan_payload("retry-target", retry_payload)

    assert recovered["route_plan_reused"] is True
    assert recovered["route_entities_written"] is True
    assert recovered["route_execution_written"] is True
    assert route_path.read_bytes() == original_route_bytes
    before_noop = _workspace_hashes("retry-target")

    repeated = write_route_plan_payload("retry-target", retry_payload)

    assert repeated["route_plan_reused"] is True
    assert repeated["route_entities_written"] is False
    assert repeated["route_execution_written"] is False
    assert _workspace_hashes("retry-target") == before_noop


def test_write_route_plan_payload_rejects_different_sha_before_side_effects(
    isolated_projects,
):
    _seed_source("replace-source")
    prepare_route_only("replace-source", "replace-target")
    write_route_plan_payload("replace-target", _route_only_payload())
    before = _workspace_hashes("replace-target")

    with pytest.raises(ValueError, match="different route structure"):
        write_route_plan_payload(
            "replace-target",
            {
                **_route_only_payload("杜甫草堂"),
                "route_entities": {"entities": []},
                "route_execution": {"route_legs": []},
            },
        )

    assert _workspace_hashes("replace-target") == before


def test_finalize_route_only_keeps_unconfirmed_gate(isolated_projects):
    _prepare_validated_route_only()

    result = finalize_route_only("target")

    assert result["status"] == "route_draft_completed"
    assert result["downstream_started"] is False
    assert result["skeleton_confirmed"] is False
    manifest = store.read_workspace("target", "experiment.json")
    assert manifest["completion_mode"] == "route_draft_completed"
    assert store.read_workspace("target", "progress.json")["skeleton_confirmed"] is False


@pytest.mark.parametrize(
    "relative_path",
    [
        "outputs/lifestyle.json",
        "outputs/pitfall.json",
        "final_guide.md",
        "final_guide.blocks.json",
        "final_guide.meta.json",
        "outputs/poi_details/manifest.json",
        "outputs/poi_details/day1_test.json",
    ],
)
def test_finalize_route_only_rejects_any_downstream_artifact(
    isolated_projects,
    relative_path,
):
    _prepare_validated_route_only("pollution-source", "pollution-target")
    artifact_path = store.workspace_dir("pollution-target") / relative_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        "{}" if artifact_path.suffix == ".json" else "污染产物",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="越界下游产物"):
        finalize_route_only("pollution-target")

    assert store.read_workspace("pollution-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


@pytest.mark.parametrize(
    "event",
    [
        {
            "event": "stage_start",
            "stage": "lifestyle",
            "summary": "越界启动Lifestyle",
        },
        {
            "event": "agent_delegated",
            "task_id": "task-lifestyle",
            "role": "travel-lifestyle",
            "summary": "越界委派Lifestyle",
        },
    ],
)
def test_finalize_route_only_rejects_downstream_trace_evidence(
    isolated_projects,
    event,
):
    _prepare_validated_route_only("trace-source", "trace-target")
    append_trace("trace-target", event)

    with pytest.raises(ValueError, match="越界下游执行记录"):
        finalize_route_only("trace-target")

    assert store.read_workspace("trace-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


def test_finalize_route_only_rejects_tampered_fixed_route_mode(isolated_projects):
    _prepare_validated_route_only("mode-source", "mode-target")
    manifest = store.read_workspace("mode-target", "experiment.json")
    manifest["mode"] = "fixed_route"
    store.write_experiment_manifest("mode-target", manifest)

    with pytest.raises(PermissionError, match="route_only_mode_invalid"):
        finalize_route_only("mode-target")

    assert store.read_workspace("mode-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


@pytest.mark.parametrize("checker", ["structure", "citation", "map", "feasibility"])
@pytest.mark.parametrize("failure", ["missing", "stale"])
def test_finalize_route_only_requires_latest_current_checker_receipts(
    isolated_projects,
    checker,
    failure,
):
    _seed_source("receipt-source")
    prepare_route_only("receipt-source", "receipt-target")
    write_route_plan_payload("receipt-target", _route_only_payload())
    _mark_route_completed("receipt-target")
    for current_checker in _ROUTE_CHECK_COMPONENTS:
        if current_checker != checker:
            _append_route_checker_receipt("receipt-target", current_checker)
    if failure == "stale":
        _append_route_checker_receipt("receipt-target", checker)
        _append_route_checker_receipt(
            "receipt-target",
            checker,
            route_sha="stale-route-sha",
        )

    with pytest.raises(ValueError, match=checker):
        finalize_route_only("receipt-target")

    assert store.read_workspace("receipt-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


@pytest.mark.parametrize(
    "checker,component_status,observation_mode",
    [
        ("structure", "blocked", "offline"),
        ("citation", "partial", "offline"),
        ("map", "unknown", "live"),
        ("feasibility", "unknown", "live"),
    ],
)
def test_finalize_route_only_rejects_nonlive_or_technical_incomplete_receipts(
    isolated_projects,
    checker,
    component_status,
    observation_mode,
):
    _prepare_validated_route_only("technical-source", "technical-target")
    if checker == "map":
        map_report = store.read_workspace("technical-target", "route_map_validation.json")
        map_report["status"] = "unknown"
        map_report["days"][0]["schedule_check"] = {"status": "unknown"}
        map_report["issues"] = [
            {
                "code": "route_map_provider_error",
                "day": 1,
                "message": "provider unavailable",
            }
        ]
        store.write_route_map_validation("technical-target", map_report)
    elif checker == "feasibility":
        route_plan = store.read_workspace("technical-target", "route_plan.json")
        store.write_route_feasibility(
            "technical-target",
            "citation_checker",
            RouteFeasibilityReport(
                route_plan_structure_hash=route_structure_sha256(route_plan),
                overall_status="unknown",
                checks=[
                    RouteFeasibilityCheck(
                        check_id="provider-unknown",
                        day=1,
                        target="武侯祠",
                        target_type="poi",
                        category="poi_operational_status",
                        status="unknown",
                        summary="provider unavailable",
                        technical_error="provider unavailable",
                        technical_error_class="provider_error",
                    )
                ],
            ),
        )
    _append_route_checker_receipt(
        "technical-target",
        checker,
        component_status=component_status,
        observation_mode=observation_mode,
    )

    with pytest.raises(ValueError, match=checker):
        finalize_route_only("technical-target")

    assert store.read_workspace("technical-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


@pytest.mark.parametrize("checker", ["citation", "map", "feasibility"])
def test_finalize_route_only_rejects_latest_stale_artifact_receipt(
    isolated_projects,
    checker,
):
    _prepare_validated_route_only("artifact-source", "artifact-target")
    _append_route_checker_receipt(
        "artifact-target",
        checker,
        artifact_sha="stale-artifact-sha",
    )

    with pytest.raises(ValueError, match=checker):
        finalize_route_only("artifact-target")

    assert store.read_workspace("artifact-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


def test_finalize_route_only_rejects_blocked_receipt_from_stale_expected_sha(
    isolated_projects,
    monkeypatch,
):
    _prepare_validated_route_only("expected-source", "expected-target")
    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda *args, **kwargs: [],
    )

    receipt = validate_stage(
        "expected-target",
        "route",
        live=True,
        route_checks=["citation"],
        expected_route_structure_sha256="stale-expected-route-sha",
    )

    assert receipt["status"] == "blocked"
    assert receipt["route_gate"]["component_statuses"]["route_citation"] == "passed"
    with pytest.raises(ValueError, match="citation_receipt_blocked"):
        finalize_route_only("expected-target")
    assert store.read_workspace("expected-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


def test_finalize_route_only_allows_business_findings_without_technical_error(
    isolated_projects,
):
    _prepare_validated_route_only("finding-source", "finding-target")
    route_plan = store.read_workspace("finding-target", "route_plan.json")
    route_sha = route_structure_sha256(route_plan)
    map_report = store.read_workspace("finding-target", "route_map_validation.json")
    map_report["status"] = "needs_confirmation"
    map_report["days"][0]["schedule_check"] = {"status": "warning"}
    map_report["issues"] = [
        {
            "code": "route_schedule_over_12h",
            "day": 1,
            "message": "保守日程超过12小时",
        }
    ]
    store.write_route_map_validation("finding-target", map_report)
    store.write_route_feasibility(
        "finding-target",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_sha,
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="reservation-unknown",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="unknown",
                    summary="暂时查不到预约规则",
                    suggested_action="出发前通过官方渠道重新核实",
                )
            ],
        ),
    )
    _append_route_checker_receipt(
        "finding-target", "map", component_status="needs_confirmation"
    )
    _append_route_checker_receipt(
        "finding-target", "feasibility", component_status="unknown"
    )

    result = finalize_route_only("finding-target")

    assert result["status"] == "route_draft_completed_with_findings"
    assert result["skeleton_confirmed"] is False
    assert store.read_workspace("finding-target", "experiment.json")[
        "completion_mode"
    ] == "route_draft_completed_with_findings"


def test_finalize_route_only_rejects_structure_change_finding(isolated_projects):
    _prepare_validated_route_only("blocked-source", "blocked-target")
    route_plan = store.read_workspace("blocked-target", "route_plan.json")
    store.write_route_feasibility(
        "blocked-target",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_structure_sha256(route_plan),
            overall_status="blocked",
            checks=[
                RouteFeasibilityCheck(
                    check_id="closed-poi",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="poi_operational_status",
                    status="blocked",
                    summary="当前关闭",
                    requires_structure_change=True,
                )
            ],
        ),
    )
    _append_route_checker_receipt(
        "blocked-target", "feasibility", component_status="blocked"
    )

    with pytest.raises(ValueError, match="action-required"):
        finalize_route_only("blocked-target")

    assert store.read_workspace("blocked-target", "experiment.json")[
        "completion_mode"
    ] == "in_progress"


def _lifestyle_item(field: str, recommendation: str) -> dict:
    return {
        "field": field,
        "day": 1,
        "recommendation": recommendation,
        "claims": [
            {
                "target_path": "recommendation",
                "claim_text": recommendation,
                "claim_type": field,
                "support_mode": "estimate",
                "claimed_entities": [],
                "evidence_refs": [],
            }
        ],
    }


@pytest.fixture
def deterministic_resume_validators(monkeypatch):
    monkeypatch.setattr(citation_check, "verify_route_citations", lambda *args, **kwargs: [])
    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", lambda *args, **kwargs: [])
    monkeypatch.setattr(citation_check, "verify_pitfall_citations", lambda *args, **kwargs: [])
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda *args, **kwargs: [])


def _workspace_hashes(project_id: str) -> dict[str, str]:
    workspace = store.workspace_dir(project_id)
    return {
        str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }


def _mark_route_completed(project_id: str) -> None:
    route_plan = store.read_workspace(project_id, "route_plan.json")
    route_sha = route_structure_sha256(route_plan)
    store.write_route_map_validation(
        project_id,
        {
            "schema_version": "route-map-validation-v1",
            "project_id": project_id,
            "route_plan_structure_hash": route_sha,
            "provider": "高德Web服务API",
            "checked_at": "2026-09-07T00:00:00+00:00",
            "status": "passed",
            "days": [
                {
                    "day": day["day"],
                    "entities": [
                        {
                            "original_name": poi["name"],
                            "status": "resolved",
                        }
                        for poi in day.get("pois") or []
                    ],
                    "legs": [
                        {
                            "from": left["name"],
                            "to": right["name"],
                            "transport_mode": "driving",
                            "status": "passed",
                            "distance_km": 1.0,
                            "duration_h": 0.1,
                        }
                        for left, right in zip(
                            day.get("pois") or [], (day.get("pois") or [])[1:]
                        )
                    ],
                    "schedule_check": {"status": "passed"},
                }
                for day in route_plan.get("days") or []
            ],
            "issues": [],
        },
    )
    store.write_route_feasibility(
        project_id,
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_sha,
            overall_status="passed",
        ),
    )
    store.write_evidence_checks(
        project_id,
        "route",
        [
            {
                "target_key": "route:day:1",
                "result": "pass",
                "observation_mode": "live",
                "checked_at": "2026-09-02T00:00:00+00:00",
            }
        ],
    )


_ROUTE_CHECK_COMPONENTS = {
    "structure": "route_structure",
    "citation": "route_citation",
    "map": "route_map",
    "feasibility": "route_feasibility",
}


def _append_route_checker_receipt(
    project_id: str,
    checker: str,
    *,
    component_status: str = "passed",
    observation_mode: str | None = None,
    route_sha: str | None = None,
    artifact_sha: str | None = None,
) -> None:
    current_route = store.read_workspace(project_id, "route_plan.json")
    current_route_sha = route_structure_sha256(current_route)
    current_artifact_sha = bridge._canonical_sha256(
        bridge._validation_artifact(project_id, "route", None, None)
    )
    gate_status = (
        "warning"
        if component_status in {"needs_confirmation", "needs_repair", "warning"}
        else component_status
    )
    store.append_validation_run(
        project_id,
        {
            "stage": "route",
            "scope": f"checker:{checker}",
            "observation_mode": observation_mode
            or ("offline" if checker == "structure" else "live"),
            "status": (
                "passed"
                if component_status == "passed"
                else "needs_repair"
            ),
            "artifact_sha256": artifact_sha or current_artifact_sha,
            "route_structure_sha256": route_sha or current_route_sha,
            "issue_codes": [],
            "issues": [],
            "route_checks": [checker],
            "route_gate": {
                "status": gate_status,
                "requested_checks": [checker],
                "component_statuses": {
                    _ROUTE_CHECK_COMPONENTS[checker]: component_status
                },
                "unresolved_check_ids": [],
            },
        },
    )


def _append_all_route_checker_receipts(project_id: str) -> None:
    for checker in ("structure", "citation", "map", "feasibility"):
        _append_route_checker_receipt(project_id, checker)


def _prepare_validated_route_only(
    source_project_id: str = "source",
    target_project_id: str = "target",
) -> None:
    _seed_source(source_project_id)
    prepare_route_only(source_project_id, target_project_id)
    write_route_plan_payload(target_project_id, _route_only_payload())
    _mark_route_completed(target_project_id)
    _append_all_route_checker_receipts(target_project_id)


def _write_grade_a_preflight_receipt(
    root: Path,
    source_project_id: str,
    target_project_id: str,
) -> None:
    route_plan = store.read_workspace(source_project_id, "route_plan.json")
    if store.read_workspace(source_project_id, "fixed_route_input_snapshot.json") is None:
        store.write_fixed_route_input_snapshot(
            source_project_id,
            {"source": "user_confirmed_table", "days": [{"day": 1}]},
        )
    if store.read_workspace(source_project_id, "route_entities.json") is None:
        store.write_route_entities(
            source_project_id,
            "route_agent",
            RouteEntities(entities=[]),
        )
    if store.read_workspace(source_project_id, "route_execution.json") is None:
        store.write_route_execution(
            source_project_id,
            "route_agent",
            RouteExecutionPlan(route_legs=[]),
        )
    if store.read_workspace(source_project_id, "material_baseline.json") is None:
        store.write_material_baseline(
            source_project_id,
            baseline.build_material_baseline("成都", "成都"),
        )
    required = {
        "source_project",
        "target_project",
        "requirements",
        "route_plan",
        "fixed_route_input_snapshot",
        "route_entities",
        "route_execution",
        "material_baseline",
        "web",
        "amap",
        "embedding",
    }
    path = root / "runtime" / "preflight" / f"{target_project_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "codex-fixed-route-preflight-v2",
                "mode": "fixed_route",
                "source_project_id": source_project_id,
                "target_project_id": target_project_id,
                "observation_mode": "live",
                "comparison_sidecars_required": True,
                "embedding_required": True,
                "required_checks": sorted(required),
                "checked_at": "2026-09-07T00:00:00+00:00",
                "ready": True,
                "requirements_sha256": bridge._canonical_sha256(
                    store.read_requirements(source_project_id)
                ),
                "route_structure_sha256": route_structure_sha256(route_plan),
                "checks": {
                    name: {
                        "status": "passed",
                        **(
                            {"exists": False, "allow_existing": False}
                            if name == "target_project"
                            else {}
                        ),
                    }
                    for name in required
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_complete_lifestyle(project_id: str, *, mark_live: bool = True) -> None:
    write_stage_payload(
        project_id,
        "lifestyle",
        {
            "items": [
                _lifestyle_item("food", "选择不辣的本地小吃"),
                _lifestyle_item("outfit", "穿舒适步行鞋"),
                _lifestyle_item("lodging", "选择交通便利的住宿"),
                _lifestyle_item("play", "按确认路线控制游览节奏"),
            ]
        },
    )
    if not mark_live:
        return
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    checks = []
    for field in ("food", "outfit", "lodging", "play"):
        for item in lifestyle[field]["items"]:
            for claim in item.get("claims") or []:
                checks.append(
                    {
                        "target_key": f"lifestyle:{field}:day:{item['day']}:claim:{claim['claim_id']}",
                        "claim_id": claim["claim_id"],
                        "result": "pass",
                        "observation_mode": "live",
                        "checked_at": "2026-09-02T00:00:00+00:00",
                    }
                )
    store.write_evidence_checks(project_id, "lifestyle", checks)


def _write_complete_pitfall(project_id: str) -> None:
    write_stage_payload(
        project_id,
        "pitfall",
        {
            "items": [
                {"field": field, "day": 1, "target_recommendation": "由Writer重绑定", "warning": ""}
                for field in ("food", "outfit", "lodging", "play")
            ]
        },
    )
    store.write_evidence_checks(
        project_id,
        "pitfall",
        [
            {
                "target_key": "pitfall:food:day:1:item:0",
                "result": "pass",
                "observation_mode": "live",
                "checked_at": "2026-09-02T00:00:00+00:00",
            }
        ],
    )


def _write_valid_integration(project_id: str) -> str:
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    claim_id = lifestyle["food"]["items"][0]["claims"][0]["claim_id"]
    store.write_final_guide_blocks(
        project_id,
        "integration_agent",
        [
            {
                "section": "Day1路线",
                "markdown": "## Day 1\n\n" + "按确认路线安排轻松游览与休息。" * 20,
                "mapping_mode": "upstream",
                "upstream_claim_ids": [claim_id],
            },
            {
                "section": "行前清单",
                "markdown": "## 行前清单\n\n证件、药品、充电设备和舒适步行鞋。",
                "mapping_mode": "editorial",
                "mapping_note": "根据当前行程结构整理",
            },
        ],
        {
            "merged_fields": [
                "route_plan",
                "lifestyle.food",
                "lifestyle.outfit",
                "lifestyle.lodging",
                "lifestyle.play",
            ],
            "checklist_included": True,
        },
    )
    return claim_id


def _seed_resume_project(project_id: str, *, mark_lifestyle_live: bool = True) -> None:
    _seed_source(project_id)
    _mark_route_completed(project_id)
    _write_complete_lifestyle(project_id, mark_live=mark_lifestyle_live)
    if mark_lifestyle_live:
        _write_complete_pitfall(project_id)


def _set_route_pois(project_id: str, names: list[str]) -> None:
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    area="市中心",
                    pois=[RoutePoi(name=name, needs_detail=True) for name in names],
                    notes="按已确认路线游览。",
                )
            ]
        ),
    )


def _write_poi_manifest(project_id: str, entries: list[tuple[str, str]]) -> None:
    manifest_entries = []
    for poi_name, citation_status in entries:
        filename = f"day1_{store.poi_slug(poi_name)}.json"
        if citation_status != "generation_failed":
            store.write_poi_detail(
                project_id,
                "poi_detail_agent",
                PoiDetailFile(
                    poi_name=poi_name,
                    day=1,
                    routes=[
                        RouteOption(
                            route_name="基础游览线",
                            route_segments=[
                                RouteSegment(order=1, point_name=poi_name)
                            ],
                        )
                    ],
                    citation_status=citation_status,
                ),
            )
        manifest_entries.append(
            PoiDetailManifestEntry(
                poi_name=poi_name,
                day=1,
                file=filename,
                citation_status=citation_status,
            )
        )
    store.write_poi_details_manifest(
        project_id,
        "main_agent",
        PoiDetailManifest(generated=manifest_entries),
    )


def test_prepare_fixed_route_isolates_downstream_outputs_and_legacy_notes(isolated_projects):
    source = "source-chengdu"
    target = "codex-chengdu"
    _seed_source(source)
    store.write_agent_output_patch(
        source,
        "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": "旧答案"}],
    )
    materials_dir = store.project_dir(source) / "materials"
    (materials_dir / "note-000.json").write_text(
        json.dumps({"content": "旧式笔记"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = prepare_fixed_route(source, target)

    assert result["mode"] == "fixed_route"
    assert result["inherited_amap_verified"] is True
    assert result["amap_rechecked_in_new_project"] is False
    assert result["valid_project_evidence_copied"] == 0
    assert result["needs_detail_targets"] == [
        {"day": 1, "poi_name": "武侯祠", "needs_detail_reason": ""}
    ]
    assert store.read_requirements(target)["requirements"]["days"] == 1
    assert store.read_workspace(target, "outputs/lifestyle.json") is None
    assert not (store.project_dir(target) / "materials" / "note-000.json").exists()
    assert store.read_progress(target).skeleton_confirmed is True


def test_prepare_fixed_route_records_strict_comparison_metadata(isolated_projects):
    _seed_source("source")
    store.write_fixed_route_input_snapshot(
        "source",
        {"source": "user_confirmed_table", "days": [{"day": 1, "route": "武侯祠"}]},
    )
    store.write_route_entities(
        "source",
        "route_agent",
        RouteEntities(
            entities=[
                RouteEntityResolution(
                    day=1,
                    poi_index=0,
                    formal_name="武侯祠",
                    provider_query="成都武侯祠博物馆",
                    status="requires_human_confirmation",
                )
            ]
        ),
    )
    store.write_route_execution(
        "source",
        "route_agent",
        RouteExecutionPlan(route_legs=[]),
    )
    store.write_material_baseline(
        "source", baseline.build_material_baseline("成都", "成都")
    )
    _write_grade_a_preflight_receipt(isolated_projects, "source", "target")

    result = prepare_fixed_route(
        "source",
        "target",
        model="gpt-5.6-sol",
        comparison_grade="A",
        require_comparison_sidecars=True,
    )

    assert result["missing_comparison_sidecars"] == []
    manifest = store.read_workspace("target", "experiment.json")
    assert manifest["model"] == "gpt-5.6-sol"
    assert manifest["provider"] == "Codex"
    assert manifest["comparison_grade"] == "A"
    assert manifest["preflight_receipt"]["embedding_passed"] is True


def test_grade_a_prepare_requires_bound_live_preflight_before_target_creation(
    isolated_projects,
):
    _seed_source("source-no-receipt")

    with pytest.raises(ValueError, match="缺少已持久化的Live Preflight回执"):
        prepare_fixed_route(
            "source-no-receipt",
            "target-no-receipt",
            model="gpt-5.6-sol",
            comparison_grade="A",
        )

    assert not store.project_dir("target-no-receipt").exists()


def test_grade_a_prepare_rejects_receipt_after_requirements_drift(isolated_projects):
    _seed_source("source-drift")
    _write_grade_a_preflight_receipt(
        isolated_projects,
        "source-drift",
        "target-drift",
    )
    store.write_requirements(
        "source-drift",
        "成都",
        {
            "destination": "成都",
            "days": 1,
            "style": "历史人文",
            "notes": "当前输入已经变化",
        },
    )

    with pytest.raises(ValueError, match="requirements_sha256_mismatch"):
        prepare_fixed_route(
            "source-drift",
            "target-drift",
            model="gpt-5.6-sol",
            comparison_grade="A",
        )

    assert not store.project_dir("target-drift").exists()


def test_bridge_uses_existing_lifestyle_and_pitfall_writers(isolated_projects):
    _seed_source("source")
    prepared = prepare_fixed_route("source", "target")

    write_stage_payload(
        "target",
        "lifestyle",
        {"items": [_lifestyle_item("food", "选择不辣的本地小吃")]},
    )
    incomplete = validate_stage(
        "target",
        "lifestyle",
        expected_route_structure_sha256=prepared["route_structure_sha256"],
    )
    assert incomplete["status"] == "needs_repair"
    assert any(issue["code"] == "lifestyle_coverage_invalid" for issue in incomplete["issues"])

    write_stage_payload(
        "target",
        "lifestyle",
        {
            "items": [
                _lifestyle_item("outfit", "穿舒适步行鞋"),
                _lifestyle_item("lodging", "选择交通便利的住宿"),
                _lifestyle_item("play", "按确认路线控制游览节奏"),
            ]
        },
    )
    complete = validate_stage(
        "target",
        "lifestyle",
        expected_route_structure_sha256=prepared["route_structure_sha256"],
    )
    assert complete["status"] == "partial"
    assert not any(issue["code"] == "lifestyle_coverage_invalid" for issue in complete["issues"])

    pitfall_items = [
        {"field": field, "day": 1, "target_recommendation": "模型摘要", "warning": ""}
        for field in ("food", "outfit", "lodging", "play")
    ]
    write_stage_payload("target", "pitfall", {"items": pitfall_items})
    pitfall = store.read_workspace("target", "outputs/pitfall.json")
    lifestyle = store.read_workspace("target", "outputs/lifestyle.json")
    assert pitfall["food"]["items"][0]["target_recommendation"] == lifestyle["food"]["items"][0]["recommendation"]

    pitfall_validation = validate_stage(
        "target",
        "pitfall",
        expected_route_structure_sha256=prepared["route_structure_sha256"],
    )
    assert pitfall_validation["status"] == "partial"
    assert not any(issue["code"] == "pitfall_binding_invalid" for issue in pitfall_validation["issues"])


def test_bridge_rejects_new_claim_without_explicit_support_mode(isolated_projects):
    _seed_source("source")
    prepare_fixed_route("source", "target")

    with pytest.raises(ValueError, match="必须显式填写support_mode"):
        write_stage_payload(
            "target",
            "lifestyle",
            {
                "items": [
                    {
                        "field": "play",
                        "day": 1,
                        "recommendation": "武侯祠适合作为本日收尾。",
                        "claims": [
                            {
                                "target_path": "recommendation",
                                "claim_text": "武侯祠适合作为本日收尾。",
                                "claim_type": "play",
                                "claimed_entities": ["武侯祠"],
                                "evidence_refs": [],
                            }
                        ],
                    }
                ]
            },
        )

    assert store.read_workspace("target", "outputs/lifestyle.json") is None


def test_poi_validation_preserves_exact_claim_repair_fields(
    isolated_projects, monkeypatch
):
    _seed_source("source")
    prepare_fixed_route("source", "target")
    store.write_poi_detail(
        "target",
        "poi_detail_agent",
        PoiDetailFile(
            poi_name="武侯祠",
            day=1,
            routes=[RouteOption(route_name="基础游览线")],
        ),
    )
    expected_issue = {
        "claim_id": "poi-claim-1",
        "claim_text": "武侯祠当前开放，适合作为本日收尾。",
        "target_path": "routes.0.summary",
        "support_mode": "direct",
        "route_name": "基础游览线",
        "reason": "外部开放事实和路线编排建议不能共用direct Claim",
    }
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: [expected_issue.copy()],
    )

    result = validate_stage(
        "target",
        "poi_detail",
        day=1,
        poi_name="武侯祠",
    )

    assert result["status"] == "needs_repair"
    issue = next(item for item in result["issues"] if item["code"] == "poi_detail_citation")
    assert {key: issue[key] for key in expected_issue} == expected_issue


def test_validation_blocks_silent_confirmed_route_change(isolated_projects):
    _seed_source("source")
    prepared = prepare_fixed_route("source", "target")
    store.write_route_plan(
        "target",
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    area="市中心",
                    pois=[RoutePoi(name="杜甫草堂")],
                )
            ]
        ),
    )

    result = validate_stage(
        "target",
        "route",
        expected_route_structure_sha256=prepared["route_structure_sha256"],
    )
    assert result["status"] == "blocked"
    assert any(issue["code"] == "confirmed_route_structure_changed" for issue in result["issues"])


def test_route_facts_bridge_preserves_confirmed_structure(isolated_projects):
    _seed_source("route-facts")
    expected = route_structure_sha256(store.read_workspace("route-facts", "route_plan.json"))

    result = write_route_facts_payload(
        "route-facts",
        {"notes": "核验后补充预约提醒。"},
        day=1,
        expected_route_structure_sha256=expected,
    )

    assert result["route_structure_sha256"] == expected
    route = store.read_workspace("route-facts", "route_plan.json")
    assert route["days"][0]["notes"] == "核验后补充预约提醒。"
    assert route["days"][0]["pois"][0]["name"] == "武侯祠"


def test_route_facts_bridge_rejects_structural_fields(isolated_projects):
    _seed_source("route-facts-reject")
    expected = route_structure_sha256(store.read_workspace("route-facts-reject", "route_plan.json"))

    with pytest.raises(ValueError, match="不支持字段"):
        write_route_facts_payload(
            "route-facts-reject",
            {"pois": []},
            day=1,
            expected_route_structure_sha256=expected,
        )


def test_route_structure_issue_blocks_without_expected_hash(isolated_projects):
    _seed_source("source")
    prepare_fixed_route("source", "target")
    route_path = store.project_dir("target") / "workspace" / "route_plan.json"
    route = json.loads(route_path.read_text(encoding="utf-8"))
    route["days"][0]["city"] = ""
    route_path.write_text(json.dumps(route, ensure_ascii=False), encoding="utf-8")

    result = validate_stage("target", "route")

    assert result["status"] == "blocked"
    assert any(issue["code"] == "day_city_empty" for issue in result["issues"])


def test_route_scoped_retry_forwards_check_ids_without_repeating_citations(
    isolated_projects, monkeypatch
):
    _seed_source("source")
    prepared = prepare_fixed_route("source", "target")
    calls = []

    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("局部可执行性重试不应重复路线引用核验")
        ),
    )

    def fake_verify(project_id, topic, **kwargs):
        calls.append(kwargs)
        return RouteFeasibilityReport(overall_status="passed")

    monkeypatch.setattr(route_feasibility_check, "verify_route_feasibility", fake_verify)

    result = validate_stage(
        "target",
        "route",
        live=True,
        route_check_ids=["poi-status", "poi-opening"],
        expected_route_structure_sha256=prepared["route_structure_sha256"],
    )

    assert result["status"] == "passed"
    assert calls == [
        {
            "requirements": store.read_requirements("target")["requirements"],
            "check_ids": ["poi-status", "poi-opening"],
        }
    ]
    assert all(check["name"] != "route_citation" for check in result["checks"])
    route_feasibility = next(
        check for check in result["checks"] if check["name"] == "route_feasibility"
    )
    assert route_feasibility["requested_check_ids"] == ["poi-status", "poi-opening"]
    assert result["route_gate"]["requested_checks"] == ["feasibility"]


def test_route_checker_can_run_map_independently_with_timing_and_scope(
    isolated_projects,
    monkeypatch,
):
    _seed_source("source")
    prepared = prepare_fixed_route("source", "target")
    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("独立Map检查不应运行Citation")
        ),
    )
    monkeypatch.setattr(
        route_feasibility_check,
        "verify_route_feasibility",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("独立Map检查不应运行Feasibility")
        ),
    )
    monkeypatch.setattr(
        route_map_check,
        "verify_route_map",
        lambda *args, **kwargs: {"status": "passed", "issues": []},
    )

    result = validate_stage(
        "target",
        "route",
        live=True,
        route_checks=["map"],
        expected_route_structure_sha256=prepared["route_structure_sha256"],
    )

    assert result["status"] == "passed"
    assert result["route_gate"]["status"] == "passed"
    assert result["route_gate"]["component_statuses"] == {"route_map": "passed"}
    assert [check["name"] for check in result["checks"]] == [
        "material_baseline",
        "route_map",
    ]
    assert result["checks"][1]["duration_ms"] >= 0
    assert result["checks"][1]["provider_call_count"] == 0
    validation_run = store.read_validation_runs("target")[-1]
    assert validation_run["scope"] == "checker:map"
    assert validation_run["route_checks"] == ["map"]
    assert validation_run["route_gate"]["status"] == "passed"


def test_route_citation_provider_failure_persists_technical_receipt(
    isolated_projects,
    monkeypatch,
):
    from langchain_core.exceptions import OutputParserException

    _prepare_validated_route_only("citation-source", "citation-target")
    route_plan = store.read_workspace("citation-target", "route_plan.json")
    route_sha = route_structure_sha256(route_plan)
    calls = 0

    def fail_citation_check(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise OutputParserException(
            "CitationVerdict cannot contain both reason and answer"
        )

    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        fail_citation_check,
    )

    result = validate_stage(
        "citation-target",
        "route",
        live=True,
        route_checks=["citation"],
        expected_route_structure_sha256=route_sha,
    )

    assert calls == 1
    assert result["status"] == "needs_repair"
    assert result["route_gate"]["status"] == "unknown"
    citation_result = result["checks"][1]
    assert citation_result["name"] == "route_citation"
    assert citation_result["status"] == "unknown"
    assert (
        citation_result["issues"][0]["technical_error_class"]
        == "OutputParserException"
    )
    assert "OutputParserException(" in citation_result["issues"][0]["technical_error_repr"]
    assert "fail_citation_check" in citation_result["issues"][0]["technical_error_traceback"]
    assert citation_result["issues"][0]["technical_error_chain"][0]["class"] == "OutputParserException"
    receipt = store.read_validation_runs("citation-target")[-1]
    assert receipt["scope"] == "checker:citation"
    assert receipt["observation_mode"] == "live"
    assert receipt["route_structure_sha256"] == route_sha
    assert receipt["artifact_sha256"] == bridge._canonical_sha256(
        bridge._validation_artifact("citation-target", "route", None, None)
    )
    assert receipt["route_gate"]["component_statuses"] == {
        "route_citation": "unknown"
    }
    with pytest.raises(ValueError, match="route_only_citation_receipt_technical_error"):
        finalize_route_only("citation-target")

    plan = build_resume_plan("citation-target", expected_route_sha=route_sha)

    assert plan["mode"] == "full_plan_route_only"
    assert [stage["stage"] for stage in plan["stages"]] == ["route"]
    assert plan["stages"][0]["status"] == "needs_validation"
    assert plan["poi_targets"] == []
    assert plan["next_actions"][0]["action"] == "validate_stage"


def test_route_citation_empty_assertion_receipt_preserves_failure_context(
    isolated_projects,
    monkeypatch,
):
    _prepare_validated_route_only("assertion-source", "assertion-target")
    route_plan = store.read_workspace("assertion-target", "route_plan.json")
    route_sha = route_structure_sha256(route_plan)

    def fail_after_persisting_check(*args, **kwargs):
        store.write_evidence_checks(
            "assertion-target",
            "route",
            [
                {
                    "target_type": "route_claim",
                    "target_key": "route:day:3:claim:claim-1",
                    "target_text": "假日海滩",
                    "scope": "substantive_claim",
                    "result": "error",
                    "method": "substantive_support",
                    "reason": "具体事实支持判断调用失败：",
                    "cited_sources": [],
                    "cited_chunk_ids": [],
                    "checked_chunk_ids": [],
                    "evidence": [],
                    "missing_entities": [],
                    "observation_mode": "live",
                }
            ],
            replace_prefixes=["route:"],
        )
        raise AssertionError()

    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        fail_after_persisting_check,
    )

    result = validate_stage(
        "assertion-target",
        "route",
        live=True,
        route_checks=["citation"],
        expected_route_structure_sha256=route_sha,
    )

    issue = result["checks"][1]["issues"][0]
    assert issue["technical_error_class"] == "AssertionError"
    assert issue["technical_error"] == ""
    assert issue["technical_error_repr"] == "AssertionError()"
    assert issue["technical_error_chain"] == [
        {"class": "AssertionError", "message": "", "repr": "AssertionError()"}
    ]
    assert "fail_after_persisting_check" in issue["technical_error_traceback"]
    assert issue["technical_error_target_key"] == "route:day:3:claim:claim-1"
    assert issue["technical_error_scope"] == "substantive_claim"
    assert issue["technical_error_method"] == "substantive_support"


def test_trace_accepts_observable_hypothesis_and_rejects_unclassified_observation(isolated_projects):
    store.init_project("trace-project")
    result = append_trace(
        "trace-project",
        {
            "event": "observation",
            "stage": "pitfall",
            "summary": "Pitfall只需读取当前Lifestyle推荐",
            "status": "hypothesis",
        },
    )
    trace_path = store.project_dir("trace-project") / "agents" / "codex_skill" / "trace.jsonl"
    assert result["trace_path"] == str(trace_path)
    assert json.loads(trace_path.read_text(encoding="utf-8"))["event"] == "observation"

    with pytest.raises(ValueError, match="status=hypothesis"):
        append_trace(
            "trace-project",
            {"event": "observation", "summary": "未经分类的观察"},
        )


def test_run_completed_updates_project_experiment_manifest(isolated_projects):
    _seed_source("manifest-source")
    prepare_fixed_route("manifest-source", "manifest-target")

    append_trace(
        "manifest-target",
        {
            "event": "run_completed",
            "summary": "固定路线实验完成",
            "status": "completed_with_degradation",
        },
    )

    manifest = store.read_workspace("manifest-target", "experiment.json")
    assert manifest["completion_mode"] == "completed_with_degradation"


def test_continuation_run_preserves_formal_terminal_status_and_appends_validity_correction(
    isolated_projects,
):
    _seed_source("continuation-source")
    _write_grade_a_preflight_receipt(
        isolated_projects,
        "continuation-source",
        "continuation-target",
    )
    prepare_fixed_route(
        "continuation-source",
        "continuation-target",
        model="gpt-5.6-sol",
        comparison_grade="A",
    )
    append_trace(
        "continuation-target",
        {
            "event": "run_completed",
            "summary": "正式运行在Pitfall合同校验失败后封存",
            "status": "validation_failed_preserved",
        },
    )

    append_trace(
        "continuation-target",
        {
            "event": "run_completed",
            "summary": "修复接线后的诊断续跑完成当前产物",
            "status": "completed_with_documented_degradation",
            "continuation_mode": "post_fix_contaminated_continuation",
            "original_run_status": "validation_failed_preserved",
            "declared_comparison_grade": "C",
            "effective_comparison_grade": "A",
            "formal_run_status": "completed",
        },
    )

    manifest = store.read_workspace("continuation-target", "experiment.json")
    assert manifest["completion_mode"] == "validation_failed_preserved"
    correction_path = (
        store.workspace_dir("continuation-target") / "post_run_corrections.jsonl"
    )
    corrections = [
        json.loads(line)
        for line in correction_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert corrections[-1] == {
        **{
            "schema_version": "post-run-correction-v1",
            "recorded_at": corrections[-1]["recorded_at"],
        },
        "correction_type": "experiment_validity",
        "declared_comparison_grade": "A",
        "effective_comparison_grade": "C",
        "formal_run_status": "validation_failed_preserved",
        "continuation_status": "completed_with_documented_degradation",
        "continuation_mode": "post_fix_contaminated_continuation",
        "reason_codes": ["post_fix_contaminated_continuation"],
        "capture_mode": "trace_observed",
        "continuation_completed_at": corrections[-1]["continuation_completed_at"],
        "summary": "修复接线后的诊断续跑完成当前产物",
    }


def test_accept_route_risks_preserves_findings_and_records_user_confirmation(
    isolated_projects,
):
    _seed_source("source")
    prepared = prepare_fixed_route("source", "target")
    report = RouteFeasibilityReport(
        route_plan_structure_hash=prepared["route_structure_sha256"],
        overall_status="blocked",
        checks=[
            RouteFeasibilityCheck(
                check_id="east-tai",
                day=1,
                target="武侯祠",
                target_type="poi",
                category="poi_operational_status",
                status="blocked",
                summary="用户已知情的当前运营风险",
                requires_structure_change=True,
            )
        ],
    )
    store.write_route_feasibility("target", "citation_checker", report)
    store.workspace_dir("target").joinpath("route_map_validation.json").write_text(
        json.dumps(
            {
                "schema_version": "route-map-validation-v1",
                "project_id": "target",
                "route_plan_structure_hash": prepared["route_structure_sha256"],
                "provider": "高德Web服务API",
                "checked_at": "2026-09-07T00:00:00+00:00",
                "status": "needs_confirmation",
                "days": [
                    {
                        "day": 1,
                        "entities": [
                            {"original_name": "武侯祠", "status": "resolved"}
                        ],
                        "legs": [],
                        "schedule_check": {"status": "warning"},
                    }
                ],
                "issues": [
                    {
                        "code": "route_schedule_over_12h",
                        "day": 1,
                        "message": "保守日程超过12小时",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = accept_route_risks(
        "target",
        expected_route_structure_sha256=prepared["route_structure_sha256"],
        note="用户已知情并明确保留固定路线",
    )

    saved = store.read_route_feasibility("target")
    assert result["status"] == "accepted_with_degradation"
    assert result["route_structure_unchanged"] is True
    assert saved.force_finalized is True
    assert saved.checks[0].status == "blocked"
    trace_path = store.project_dir("target") / "agents" / "codex_skill" / "trace.jsonl"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["event"] == "user_confirmation"
    assert trace["accepted_checks"][0]["check_id"] == "east-tai"
    assert trace["accepted_map_issues"][0]["code"] == "route_schedule_over_12h"


def test_accept_route_risks_rejects_technical_unknown(isolated_projects):
    _seed_source("source")
    prepared = prepare_fixed_route("source", "target")
    store.write_route_feasibility(
        "target",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=prepared["route_structure_sha256"],
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="provider-error",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="poi_operational_status",
                    status="unknown",
                    technical_error="provider timeout",
                    technical_error_class="provider_unavailable",
                )
            ],
        ),
    )

    with pytest.raises(ValueError, match="技术失败"):
        accept_route_risks(
            "target",
            expected_route_structure_sha256=prepared["route_structure_sha256"],
            note="用户希望继续",
        )


def test_accept_route_risks_rejects_business_unknown(isolated_projects):
    _seed_source("business-unknown-source")
    prepared = prepare_fixed_route("business-unknown-source", "business-unknown-target")
    _mark_route_completed("business-unknown-target")
    store.write_route_feasibility(
        "business-unknown-target",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=prepared["route_structure_sha256"],
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="reservation-unknown",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="unknown",
                    summary="暂时查不到预约规则",
                    suggested_action="出发前通过官方渠道重新核实",
                )
            ],
        ),
    )

    with pytest.raises(ValueError, match="业务unknown"):
        accept_route_risks(
            "business-unknown-target",
            expected_route_structure_sha256=prepared["route_structure_sha256"],
            note="用户希望继续",
        )

    assert store.read_route_feasibility("business-unknown-target").force_finalized is False
    assert not (
        store.project_dir("business-unknown-target")
        / "agents"
        / "codex_skill"
        / "trace.jsonl"
    ).exists()


def test_accept_route_risks_rejects_route_only_project(isolated_projects):
    _prepare_validated_route_only("route-only-risk-source", "route-only-risk-target")
    route_plan = store.read_workspace("route-only-risk-target", "route_plan.json")
    route_sha = route_structure_sha256(route_plan)
    store.write_route_feasibility(
        "route-only-risk-target",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_sha,
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="business-warning",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="opening_window",
                    status="warning",
                    summary="开放窗口较紧",
                )
            ],
        ),
    )

    with pytest.raises(ValueError, match="route-only"):
        accept_route_risks(
            "route-only-risk-target",
            expected_route_structure_sha256=route_sha,
            note="用户希望继续",
        )

    assert store.read_route_feasibility("route-only-risk-target").force_finalized is False
    assert not (
        store.project_dir("route-only-risk-target")
        / "agents"
        / "codex_skill"
        / "trace.jsonl"
    ).exists()


def test_accept_route_risks_rejects_unconfirmed_skeleton(isolated_projects):
    _seed_source("unconfirmed-source")
    prepared = prepare_fixed_route("unconfirmed-source", "unconfirmed-target")
    _mark_route_completed("unconfirmed-target")
    store.write_route_feasibility(
        "unconfirmed-target",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=prepared["route_structure_sha256"],
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="business-warning",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="opening_window",
                    status="warning",
                    summary="开放窗口较紧",
                )
            ],
        ),
    )
    store.update_progress(
        "unconfirmed-target",
        actor="main_agent",
        skeleton_confirmed=False,
    )

    with pytest.raises(ValueError, match="骨架尚未确认"):
        accept_route_risks(
            "unconfirmed-target",
            expected_route_structure_sha256=prepared["route_structure_sha256"],
            note="用户希望继续",
        )

    assert store.read_route_feasibility("unconfirmed-target").force_finalized is False


def test_accept_route_risks_rejects_route_map_provider_failure(isolated_projects):
    _seed_source("source-map-provider")
    _set_route_pois("source-map-provider", ["武侯祠", "杜甫草堂"])
    prepared = prepare_fixed_route("source-map-provider", "target-map-provider")
    store.write_route_feasibility(
        "target-map-provider",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=prepared["route_structure_sha256"],
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="business-warning",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="opening_window",
                    status="warning",
                    summary="开放窗口较紧，需要用户知情",
                )
            ],
        ),
    )
    store.write_route_map_validation(
        "target-map-provider",
        {
            "schema_version": "route-map-validation-v1",
            "project_id": "target-map-provider",
            "route_plan_structure_hash": prepared["route_structure_sha256"],
            "provider": "高德Web服务API",
            "checked_at": "2026-09-07T00:00:00+00:00",
            "status": "needs_confirmation",
            "days": [
                {
                    "day": 1,
                    "entities": [
                        {"original_name": "武侯祠", "status": "resolved"},
                        {"original_name": "杜甫草堂", "status": "resolved"},
                    ],
                    "legs": [
                        {
                            "from": "武侯祠",
                            "to": "杜甫草堂",
                            "transport_mode": "driving",
                            "status": "provider_error",
                        }
                    ],
                    "schedule_check": {"status": "unknown"},
                }
            ],
            "issues": [
                {
                    "code": "route_map_leg_unavailable",
                    "day": 1,
                    "message": "驾车Provider未返回结果",
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="Route Map技术失败"):
        accept_route_risks(
            "target-map-provider",
            expected_route_structure_sha256=prepared["route_structure_sha256"],
            note="用户希望继续",
        )

    assert store.read_route_feasibility("target-map-provider").force_finalized is False


def test_accept_route_risks_allows_map_only_schedule_warning(isolated_projects):
    _seed_source("source-map-warning")
    prepared = prepare_fixed_route("source-map-warning", "target-map-warning")
    store.write_route_feasibility(
        "target-map-warning",
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=prepared["route_structure_sha256"],
            overall_status="passed",
            checks=[],
        ),
    )
    store.write_route_map_validation(
        "target-map-warning",
        {
            "schema_version": "route-map-validation-v1",
            "project_id": "target-map-warning",
            "route_plan_structure_hash": prepared["route_structure_sha256"],
            "provider": "高德Web服务API",
            "checked_at": "2026-09-07T00:00:00+00:00",
            "status": "needs_confirmation",
            "days": [
                {
                    "day": 1,
                    "entities": [
                        {"original_name": "武侯祠", "status": "resolved"}
                    ],
                    "legs": [],
                    "schedule_check": {"status": "warning"},
                }
            ],
            "issues": [
                {
                    "code": "route_schedule_over_12h",
                    "day": 1,
                    "message": "保守日程超过12小时",
                }
            ],
        },
    )

    result = accept_route_risks(
        "target-map-warning",
        expected_route_structure_sha256=prepared["route_structure_sha256"],
        note="用户知情并保留日程",
    )

    assert result["accepted_checks"] == []
    assert [item["code"] for item in result["accepted_map_issues"]] == [
        "route_schedule_over_12h"
    ]
    assert store.read_route_feasibility("target-map-warning").force_finalized is True


@pytest.mark.parametrize(
    "invalid_kind,expected_code",
    [
        ("driving_leg_missing_duration", "route_map_leg_metrics_invalid"),
        ("mixed_driving_segment_missing_distance", "route_map_segment_metrics_invalid"),
    ],
)
def test_accept_route_risks_rejects_incomplete_driving_metrics(
    isolated_projects, invalid_kind, expected_code
):
    project_id = f"accept-map-metrics-{invalid_kind}"
    _seed_source(project_id)
    _set_route_pois(project_id, ["武侯祠", "杜甫草堂"])
    _mark_route_completed(project_id)
    store.update_progress(
        project_id,
        actor="main_agent",
        skeleton_confirmed=True,
    )
    route_plan = store.read_workspace(project_id, "route_plan.json")
    route_sha = route_structure_sha256(route_plan)
    store.write_route_feasibility(
        project_id,
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_sha,
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="business-warning",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="opening_window",
                    status="warning",
                    summary="开放窗口较紧，需要用户知情",
                )
            ],
        ),
    )
    report = store.read_workspace(project_id, "route_map_validation.json")
    leg = report["days"][0]["legs"][0]
    if invalid_kind == "driving_leg_missing_duration":
        leg.pop("duration_h")
    else:
        leg["transport_mode"] = "mixed"
        leg["status"] = "mode_specific_not_driving"
        leg["segments"] = [
            {
                "from_name": "武侯祠",
                "to_name": "杜甫草堂",
                "mode": "driving",
                "status": "passed",
                "duration_h": 0.1,
            }
        ]
    store.write_route_map_validation(project_id, report)

    with pytest.raises(ValueError, match="Route Map技术失败") as exc_info:
        accept_route_risks(
            project_id,
            expected_route_structure_sha256=route_sha,
            note="用户希望继续",
        )

    assert expected_code in str(exc_info.value)
    assert store.read_route_feasibility(project_id).force_finalized is False


def test_codex_integration_inputs_use_shared_claim_projection(isolated_projects):
    _seed_source("source")
    prepare_fixed_route("source", "target")
    write_stage_payload(
        "target",
        "lifestyle",
        {"items": [_lifestyle_item("food", "选择不辣的本地小吃")]},
    )
    lifestyle = store.read_workspace("target", "outputs/lifestyle.json")
    claim_id = lifestyle["food"]["items"][0]["claims"][0]["claim_id"]
    store.write_evidence_checks(
        "target",
        "lifestyle",
        [{"target_key": "lifestyle:food:day:1", "claim_id": claim_id, "result": "fail"}],
    )

    result = integration_delivery_inputs("target")

    assert claim_id not in result["deliverable_claim_ids"]
    assert result["lifestyle"]["food"]["items"][0]["claims"] == []
    assert store.read_workspace("target", "outputs/lifestyle.json")["food"]["items"][0][
        "claims"
    ][0]["claim_id"] == claim_id


def test_resume_plan_starts_with_route_validation_for_confirmed_route_only(
    isolated_projects, deterministic_resume_validators
):
    _seed_source("resume-route")

    plan = build_resume_plan("resume-route")

    assert plan["stages"][0]["status"] == "needs_validation"
    assert plan["next_actions"] == [
        {
            "action": "validate_stage",
            "stage": "route",
            "reason": "产物存在，但当前只读离线校验不能证明本轮Live核验已完成",
        }
    ]


def test_resume_plan_route_only_returns_before_any_downstream_read(
    isolated_projects,
    monkeypatch,
):
    _prepare_validated_route_only("resume-only-source", "resume-only-target")
    original_read_workspace = store.read_workspace

    def fail_on_downstream(project_id, relative_path):
        if relative_path.startswith("outputs/") or relative_path.startswith("final_guide"):
            pytest.fail(f"route-only resume read downstream artifact: {relative_path}")
        return original_read_workspace(project_id, relative_path)

    monkeypatch.setattr(store, "read_workspace", fail_on_downstream)

    plan = build_resume_plan("resume-only-target")

    assert plan["mode"] == "full_plan_route_only"
    assert [stage["stage"] for stage in plan["stages"]] == ["route"]
    assert plan["poi_targets"] == []
    assert plan["status"] == "action_required"
    assert plan["next_actions"] == [
        {
            "action": "finalize_route_only",
            "stage": "route",
            "reason": "四类路线回执已完成，只封口route-only实验并保持骨架未确认",
        }
    ]


def test_resume_plan_route_only_completed_manifest_has_no_downstream_action(
    isolated_projects,
):
    _prepare_validated_route_only("resume-finished-source", "resume-finished-target")
    finalize_route_only("resume-finished-target")

    plan = build_resume_plan("resume-finished-target")

    assert plan["mode"] == "full_plan_route_only"
    assert plan["status"] == "completed"
    assert [stage["stage"] for stage in plan["stages"]] == ["route"]
    assert plan["next_actions"] == []


def test_resume_plan_route_only_rejects_confirmed_or_advanced_boundary(
    isolated_projects,
):
    _prepare_validated_route_only("resume-boundary-source", "resume-boundary-target")
    store.update_progress(
        "resume-boundary-target",
        actor="main_agent",
        skeleton_confirmed=True,
    )

    plan = build_resume_plan("resume-boundary-target")

    assert plan["mode"] == "full_plan_route_only"
    assert plan["status"] == "blocked"
    assert plan["stages"][0]["status"] == "blocked"
    assert plan["poi_targets"] == []
    assert plan["next_actions"] == []


def test_resume_plan_route_only_manifest_failure_does_not_fall_back_to_downstream(
    isolated_projects,
):
    _prepare_validated_route_only("resume-contract-source", "resume-contract-target")
    manifest = store.read_workspace("resume-contract-target", "experiment.json")
    manifest["relations"] = [
        {"type": "requirements_only_source", "project_id": "wrong-source"}
    ]
    store.write_experiment_manifest("resume-contract-target", manifest)

    plan = build_resume_plan("resume-contract-target")

    assert plan["mode"] == "full_plan_route_only"
    assert plan["status"] == "blocked"
    assert [stage["stage"] for stage in plan["stages"]] == ["route"]
    assert plan["poi_targets"] == []
    assert plan["next_actions"] == []


def test_resume_plan_business_unknown_is_visible_degradation_not_route_blocker(
    isolated_projects,
    deterministic_resume_validators,
):
    project_id = "resume-business-unknown"
    _seed_resume_project(project_id)
    _write_poi_manifest(project_id, [("武侯祠", "passed")])
    _write_valid_integration(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json")
    store.write_route_feasibility(
        project_id,
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_structure_sha256(route_plan),
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="reservation-unknown",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="unknown",
                    summary="暂时查不到预约规则",
                    suggested_action="出发前通过官方渠道重新核实",
                )
            ],
        ),
    )

    plan = build_resume_plan(project_id)

    route_stage = next(stage for stage in plan["stages"] if stage["stage"] == "route")
    assert route_stage["status"] == "completed_with_degradation"
    assert route_stage["issues"][0]["check_id"] == "reservation-unknown"
    assert plan["status"] == "completed_with_degradation"


def test_resume_plan_business_warning_is_visible_degradation_not_route_repair(
    isolated_projects,
    deterministic_resume_validators,
):
    project_id = "resume-business-warning"
    _seed_resume_project(project_id)
    _write_poi_manifest(project_id, [("武侯祠", "passed")])
    _write_valid_integration(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json")
    store.write_route_feasibility(
        project_id,
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_structure_sha256(route_plan),
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="reservation-warning",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="warning",
                    summary="预约名额可能临近出发才开放",
                    suggested_action="出发前通过官方渠道重新核实",
                )
            ],
        ),
    )

    plan = build_resume_plan(project_id)

    route_stage = next(stage for stage in plan["stages"] if stage["stage"] == "route")
    assert route_stage["status"] == "completed_with_degradation"
    assert route_stage["issues"][0]["check_id"] == "reservation-warning"
    assert plan["status"] == "completed_with_degradation"
    assert not any(
        action.get("stage") == "route" and action.get("action") == "repair_stage"
        for action in plan["next_actions"]
    )


def test_resume_plan_technical_unknown_requests_validation_recovery(
    isolated_projects,
    deterministic_resume_validators,
):
    project_id = "resume-technical-unknown"
    _seed_source(project_id)
    _mark_route_completed(project_id)
    store.update_progress(
        project_id,
        actor="main_agent",
        skeleton_confirmed=True,
    )
    route_plan = store.read_workspace(project_id, "route_plan.json")
    store.write_route_feasibility(
        project_id,
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_structure_sha256(route_plan),
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="provider-unknown",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="poi_operational_status",
                    status="unknown",
                    summary="provider unavailable",
                    technical_error_class="provider_error",
                )
            ],
        ),
    )

    plan = build_resume_plan(project_id)

    assert plan["stages"][0]["status"] == "needs_validation"
    assert plan["next_actions"][0]["action"] == "validate_stage"


def test_resume_plan_blocks_globally_when_confirmed_route_hash_changes(
    isolated_projects, deterministic_resume_validators
):
    _seed_source("resume-hash")
    expected = route_structure_sha256(store.read_workspace("resume-hash", "route_plan.json"))
    _set_route_pois("resume-hash", ["杜甫草堂"])

    plan = build_resume_plan("resume-hash", expected_route_sha=expected)

    assert plan["status"] == "blocked"
    assert plan["route_hash_matches"] is False
    assert plan["stages"][0]["status"] == "blocked"


def test_resume_plan_blocks_route_map_provider_failure(
    isolated_projects, deterministic_resume_validators, monkeypatch
):
    project_id = "resume-map-provider-failure"
    _seed_source(project_id)
    _set_route_pois(project_id, ["武侯祠", "杜甫草堂"])
    _mark_route_completed(project_id)
    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda *args, **kwargs: [{"message": "另有路线引用需要修复"}],
    )
    route_plan = store.read_workspace(project_id, "route_plan.json")
    store.write_route_map_validation(
        project_id,
        {
            "schema_version": "route-map-validation-v1",
            "project_id": project_id,
            "route_plan_structure_hash": route_structure_sha256(route_plan),
            "provider": "高德Web服务API",
            "checked_at": "2026-09-07T00:00:00+00:00",
            "status": "unknown",
            "days": [
                {
                    "day": 1,
                    "entities": [
                        {"original_name": "武侯祠", "status": "resolved"},
                        {"original_name": "杜甫草堂", "status": "resolved"},
                    ],
                    "legs": [
                        {
                            "from": "武侯祠",
                            "to": "杜甫草堂",
                            "transport_mode": "driving",
                            "status": "provider_error",
                        }
                    ],
                    "schedule_check": {"status": "unknown"},
                }
            ],
            "issues": [
                {
                    "code": "route_map_leg_unavailable",
                    "day": 1,
                    "message": "驾车Provider未返回结果",
                }
            ],
        },
    )

    plan = build_resume_plan(project_id)

    route_stage = next(stage for stage in plan["stages"] if stage["stage"] == "route")
    assert plan["status"] == "blocked"
    assert route_stage["status"] == "blocked"
    assert {
        issue["code"] for issue in route_stage["issues"]
    } >= {"route_map_leg_provider_error", "route_schedule_unknown"}


def test_resume_plan_rejects_missing_schedule_check(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-map-missing-schedule"
    _seed_source(project_id)
    _mark_route_completed(project_id)
    report = store.read_workspace(project_id, "route_map_validation.json")
    report["days"][0].pop("schedule_check")
    store.write_route_map_validation(project_id, report)

    plan = build_resume_plan(project_id)

    route_stage = next(stage for stage in plan["stages"] if stage["stage"] == "route")
    assert plan["status"] == "blocked"
    assert route_stage["status"] == "blocked"
    assert "route_schedule_unknown" in {
        issue["code"] for issue in route_stage["issues"]
    }


@pytest.mark.parametrize(
    "transport_mode,status,expected_code",
    [
        ("", "passed", "route_map_leg_transport_mode_unknown"),
        ("driving", "mode_specific_not_driving", "route_map_leg_status_mode_mismatch"),
    ],
)
def test_resume_plan_rejects_route_map_mode_status_inconsistency(
    isolated_projects,
    deterministic_resume_validators,
    transport_mode,
    status,
    expected_code,
):
    project_id = f"resume-map-mode-{transport_mode or 'empty'}-{status}"
    _seed_source(project_id)
    _set_route_pois(project_id, ["武侯祠", "杜甫草堂"])
    _mark_route_completed(project_id)
    report = store.read_workspace(project_id, "route_map_validation.json")
    leg = report["days"][0]["legs"][0]
    leg["transport_mode"] = transport_mode
    leg["status"] = status
    store.write_route_map_validation(project_id, report)

    plan = build_resume_plan(project_id)

    route_stage = next(stage for stage in plan["stages"] if stage["stage"] == "route")
    assert plan["status"] == "blocked"
    assert route_stage["status"] == "blocked"
    assert expected_code in {issue["code"] for issue in route_stage["issues"]}


def test_accept_route_risks_rejects_same_hash_but_incomplete_map_contract(
    isolated_projects,
):
    project_id = "accept-incomplete-map-contract"
    _seed_source(project_id)
    _mark_route_completed(project_id)
    store.update_progress(
        project_id,
        actor="main_agent",
        skeleton_confirmed=True,
    )
    route_plan = store.read_workspace(project_id, "route_plan.json")
    report = store.read_workspace(project_id, "route_map_validation.json")
    report.pop("project_id")
    store.write_route_map_validation(project_id, report)

    with pytest.raises(ValueError, match="Route Map需要重新Live校验"):
        accept_route_risks(
            project_id,
            expected_route_structure_sha256=route_structure_sha256(route_plan),
            note="用户希望继续",
        )


def test_resume_plan_maps_offline_lifestyle_partial_to_needs_validation(
    isolated_projects, deterministic_resume_validators
):
    _seed_resume_project("resume-lifestyle-partial", mark_lifestyle_live=False)

    plan = build_resume_plan("resume-lifestyle-partial")

    lifestyle = next(stage for stage in plan["stages"] if stage["stage"] == "lifestyle")
    assert lifestyle["status"] == "needs_validation"
    assert plan["next_actions"][0]["stage"] == "lifestyle"


def test_zero_claim_live_validation_is_visible_to_resume(
    isolated_projects, deterministic_resume_validators
):
    _seed_source("resume-zero-claim")
    _mark_route_completed("resume-zero-claim")
    _write_complete_lifestyle("resume-zero-claim")
    write_stage_payload(
        "resume-zero-claim",
        "pitfall",
        {
            "items": [
                {"field": field, "day": 1, "target_recommendation": "由Writer重绑定", "warning": ""}
                for field in ("food", "outfit", "lodging", "play")
            ]
        },
    )

    validation = validate_stage("resume-zero-claim", "pitfall", live=True)
    plan = build_resume_plan("resume-zero-claim")
    pitfall = next(stage for stage in plan["stages"] if stage["stage"] == "pitfall")

    assert validation["status"] == "passed"
    assert store.read_evidence_checks("resume-zero-claim")["pitfall"] == []
    assert pitfall["status"] == "completed"
    assert store.read_validation_runs("resume-zero-claim")[-1]["artifact_sha256"]


def test_grade_a_lifestyle_requires_day_level_research_ledger(
    isolated_projects, deterministic_resume_validators
):
    project_id = "strict-lifestyle-research"
    _seed_source(project_id)
    _mark_grade_a(project_id)
    _write_complete_lifestyle(project_id, mark_live=False)

    missing = validate_stage(project_id, "lifestyle", live=True)
    assert missing["status"] == "needs_repair"
    assert any(issue["code"] == "lifestyle_research_missing" for issue in missing["issues"])

    _append_material_research(
        project_id,
        stage="lifestyle",
        target_key="lifestyle:day:1",
    )
    assert validate_stage(project_id, "lifestyle", live=True)["status"] == "passed"


def test_grade_a_pitfall_research_must_match_current_recommendation_sha(
    isolated_projects, deterministic_resume_validators
):
    project_id = "strict-pitfall-research"
    _seed_source(project_id)
    _mark_grade_a(project_id)
    _write_complete_lifestyle(project_id, mark_live=False)
    _write_complete_pitfall(project_id)

    for field in ("food", "outfit", "lodging", "play"):
        _append_material_research(
            project_id,
            stage="pitfall",
            target_key=f"pitfall:{field}:day:1",
            target_sha256="stale-sha",
        )
    stale = validate_stage(project_id, "pitfall", live=True)
    assert stale["status"] == "needs_repair"
    assert any(issue["code"] == "pitfall_research_missing" for issue in stale["issues"])

    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    for field in ("food", "outfit", "lodging", "play"):
        recommendation = lifestyle[field]["items"][0]["recommendation"]
        _append_material_research(
            project_id,
            stage="pitfall",
            target_key=f"pitfall:{field}:day:1",
            target_sha256=hashlib.sha256(recommendation.encode("utf-8")).hexdigest(),
        )
    assert validate_stage(project_id, "pitfall", live=True)["status"] == "passed"


def test_pitfall_research_binding_invalidates_only_changed_lifestyle_target(
    isolated_projects, deterministic_resume_validators
):
    project_id = "pitfall-binding-single-target-change"
    _seed_source(project_id)
    _mark_grade_a(project_id)
    _write_complete_lifestyle(project_id, mark_live=False)
    _write_complete_pitfall(project_id)

    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    for field in ("food", "outfit", "lodging", "play"):
        recommendation = lifestyle[field]["items"][0]["recommendation"]
        _append_material_research(
            project_id,
            stage="pitfall",
            target_key=f"pitfall:{field}:day:1",
            target_sha256=hashlib.sha256(recommendation.encode("utf-8")).hexdigest(),
        )
    assert validate_stage(project_id, "pitfall", live=True)["status"] == "passed"

    lifestyle_path = store.workspace_dir(project_id) / "outputs" / "lifestyle.json"
    lifestyle["food"]["items"][0]["recommendation"] = "更新后的不辣餐食建议"
    lifestyle_path.write_text(
        json.dumps(lifestyle, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = validate_stage(project_id, "pitfall", live=True)

    assert result["status"] == "needs_repair"
    assert {issue["code"] for issue in result["issues"]} == {
        "pitfall_binding_invalid",
        "pitfall_research_missing",
    }
    binding_issue = next(
        issue for issue in result["issues"] if issue["code"] == "pitfall_binding_invalid"
    )
    assert binding_issue["missing"] == []
    assert binding_issue["extra"] == []
    assert binding_issue["mismatched"] == [("food", 1)]
    research_issue = next(
        issue for issue in result["issues"] if issue["code"] == "pitfall_research_missing"
    )
    assert research_issue["missing"] == []
    assert research_issue["stale"] == [["food", 1]]


def test_pitfall_material_context_binds_current_recommendation_sha(
    isolated_projects, monkeypatch
):
    project_id = "pitfall-material-context-sha"
    _seed_source(project_id)
    _write_complete_lifestyle(project_id, mark_live=False)
    captured: dict[str, object] = {}

    def fake_retrieve_context(*args, **kwargs):
        captured.update(kwargs)
        return {"results": [], "research": {"status": "empty"}}

    monkeypatch.setattr(bridge, "retrieve_context", fake_retrieve_context)

    bridge.material_context(
        project_id,
        stage="pitfall",
        field="food",
        day=1,
        query="当前美食推荐的真实体验风险",
    )

    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    recommendation = lifestyle["food"]["items"][0]["recommendation"]
    assert captured["target_key"] == "pitfall:food:day:1"
    assert captured["target_sha256"] == hashlib.sha256(
        recommendation.encode("utf-8")
    ).hexdigest()


def test_grade_a_empty_poi_cannot_be_force_finalized(
    isolated_projects, deterministic_resume_validators
):
    project_id = "strict-empty-poi"
    _seed_source(project_id)
    _mark_grade_a(project_id)
    _append_material_research(
        project_id,
        stage="poi_detail",
        target_key="poi_detail:day:1:poi:武侯祠",
    )
    store.write_poi_detail(
        project_id,
        "poi_detail_agent",
        PoiDetailFile(poi_name="武侯祠", day=1, routes=[], citation_status="passed"),
    )

    empty = validate_stage(
        project_id,
        "poi_detail",
        day=1,
        poi_name="武侯祠",
        live=True,
    )
    assert empty["status"] == "needs_repair"
    assert any(issue["code"] == "poi_detail_content_empty" for issue in empty["issues"])

    with pytest.raises(ValueError, match="poi_detail_content_empty"):
        store.mark_poi_force_finalize(project_id, 1, "武侯祠")
    terminal = validate_stage(
        project_id,
        "poi_detail",
        day=1,
        poi_name="武侯祠",
        live=True,
    )
    assert terminal["status"] == "needs_repair"
    assert any(issue["code"] == "poi_detail_content_empty" for issue in terminal["issues"])


def test_zero_claim_validation_can_be_reconciled_from_preserved_trace(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-zero-trace"
    _seed_source(project_id)
    _mark_route_completed(project_id)
    _write_complete_lifestyle(project_id)
    write_stage_payload(
        project_id,
        "pitfall",
        {
            "items": [
                {"field": field, "day": 1, "target_recommendation": "由Writer重绑定", "warning": ""}
                for field in ("food", "outfit", "lodging", "play")
            ]
        },
    )
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    trace_path = store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(
            {
                "event": "validation_result",
                "stage": "pitfall",
                "status": "passed",
                "timestamp": pitfall["updated_at"],
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    reconcile_zero_claim_validation_from_trace(project_id, "pitfall")
    plan = build_resume_plan(project_id)

    assert next(stage for stage in plan["stages"] if stage["stage"] == "pitfall")["status"] == "completed"


def test_resume_plan_returns_only_lifestyle_repair_for_incomplete_coverage(
    isolated_projects, deterministic_resume_validators
):
    _seed_source("resume-lifestyle-repair")
    _mark_route_completed("resume-lifestyle-repair")
    write_stage_payload(
        "resume-lifestyle-repair",
        "lifestyle",
        {"items": [_lifestyle_item("food", "只写了一个维度")]},
    )

    plan = build_resume_plan("resume-lifestyle-repair")

    lifestyle = next(stage for stage in plan["stages"] if stage["stage"] == "lifestyle")
    assert lifestyle["status"] == "needs_repair"
    assert [action["stage"] for action in plan["next_actions"]] == ["lifestyle"]


def test_resume_plan_detects_pitfall_binding_after_lifestyle_changes(
    isolated_projects, deterministic_resume_validators
):
    _seed_resume_project("resume-pitfall-stale")
    lifestyle_path = store.workspace_dir("resume-pitfall-stale") / "outputs" / "lifestyle.json"
    lifestyle = json.loads(lifestyle_path.read_text(encoding="utf-8"))
    lifestyle["food"]["items"][0]["recommendation"] = "更新后的不辣餐食建议"
    lifestyle_path.write_text(json.dumps(lifestyle, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = build_resume_plan("resume-pitfall-stale")

    pitfall = next(stage for stage in plan["stages"] if stage["stage"] == "pitfall")
    assert pitfall["status"] == "needs_repair"
    assert {issue["code"] for issue in pitfall["issues"]} == {"pitfall_binding_invalid"}


def test_resume_plan_marks_integration_stale_for_changed_claim_snapshot(
    isolated_projects, deterministic_resume_validators
):
    _seed_resume_project("resume-integration-stale")
    claim_id = _write_valid_integration("resume-integration-stale")
    lifestyle_path = store.workspace_dir("resume-integration-stale") / "outputs" / "lifestyle.json"
    lifestyle = json.loads(lifestyle_path.read_text(encoding="utf-8"))
    claim = lifestyle["food"]["items"][0]["claims"][0]
    assert claim["claim_id"] == claim_id
    claim["claim_text"] = "已修改的上游事实"
    lifestyle_path.write_text(json.dumps(lifestyle, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = build_resume_plan("resume-integration-stale")

    integration = next(stage for stage in plan["stages"] if stage["stage"] == "integration")
    assert integration["status"] == "stale"
    assert "final_guide_claim_snapshots_stale" in {
        issue["code"] for issue in integration["issues"]
    }


def test_resume_plan_only_delegates_missing_poi_manifest_branch(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-poi-one-missing"
    _seed_source(project_id)
    _set_route_pois(project_id, ["武侯祠", "杜甫草堂", "金沙遗址"])
    _mark_route_completed(project_id)
    _write_complete_lifestyle(project_id)
    _write_complete_pitfall(project_id)
    _write_valid_integration(project_id)
    _write_poi_manifest(project_id, [("武侯祠", "passed"), ("杜甫草堂", "passed")])

    plan = build_resume_plan(project_id)

    assert [(item["poi_name"], item["status"]) for item in plan["poi_targets"]] == [
        ("武侯祠", "completed"),
        ("杜甫草堂", "completed"),
        ("金沙遗址", "missing"),
    ]
    assert [(action["poi_name"], action["action"]) for action in plan["next_actions"]] == [
        ("金沙遗址", "delegate_poi_detail")
    ]


def test_resume_plan_preserves_force_finalized_poi_as_degradation(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-poi-degraded"
    _seed_resume_project(project_id)
    _write_valid_integration(project_id)
    _write_poi_manifest(project_id, [("武侯祠", "force_finalized")])

    plan = build_resume_plan(project_id)

    assert plan["poi_targets"][0]["status"] == "completed_with_degradation"
    assert plan["poi_targets"][0]["citation_status"] == "force_finalized"


def test_resume_plan_propagates_required_stage_degradation_to_final_and_plan(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-route-degraded"
    _seed_resume_project(project_id)
    _write_poi_manifest(project_id, [("武侯祠", "passed")])
    _write_valid_integration(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json")
    store.write_route_feasibility(
        project_id,
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=route_structure_sha256(route_plan),
            overall_status="warning",
            force_finalized=True,
            checks=[
                RouteFeasibilityCheck(
                    check_id="accepted-route-risk",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="poi_operational_status",
                    status="warning",
                    summary="用户已确认保留的路线风险",
                )
            ],
        ),
    )

    plan = build_resume_plan(project_id)

    by_stage = {stage["stage"]: stage for stage in plan["stages"]}
    assert by_stage["route"]["status"] == "completed_with_degradation"
    assert by_stage["final"]["status"] == "completed_with_degradation"
    assert plan["status"] == "completed_with_degradation"
    assert plan["next_actions"] == [
        {
            "action": "review_degradation",
            "stage": "final",
            "reason": "交付可保留，但需要向用户展示显式降级边界",
        }
    ]


def test_resume_plan_generation_failed_poi_prevents_completion(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-poi-failed"
    _seed_resume_project(project_id)
    _write_valid_integration(project_id)
    _write_poi_manifest(project_id, [("武侯祠", "generation_failed")])

    plan = build_resume_plan(project_id)

    assert plan["status"] != "completed"
    assert plan["poi_targets"][0]["status"] == "missing"
    assert plan["next_actions"][0]["action"] == "delegate_poi_detail"


def test_resume_plan_ignores_unmanifested_poi_file(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-poi-unmanifested"
    _seed_resume_project(project_id)
    _write_valid_integration(project_id)
    store.write_poi_detail(
        project_id,
        "poi_detail_agent",
        PoiDetailFile(poi_name="武侯祠", day=1, routes=[]),
    )
    store.write_poi_details_manifest(
        project_id,
        "main_agent",
        PoiDetailManifest(generated=[]),
    )

    plan = build_resume_plan(project_id)

    assert plan["poi_targets"][0]["status"] == "missing"


def test_resume_plan_does_not_trust_trace_stage_completed_without_workspace(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-trace-not-state"
    _seed_source(project_id)
    _mark_route_completed(project_id)
    append_trace(
        project_id,
        {"event": "stage_completed", "stage": "lifestyle", "summary": "历史上曾完成Lifestyle"},
    )

    plan = build_resume_plan(project_id)

    lifestyle = next(stage for stage in plan["stages"] if stage["stage"] == "lifestyle")
    assert lifestyle["status"] == "missing"


def test_resume_plan_works_without_trace_file(
    isolated_projects, deterministic_resume_validators
):
    _seed_source("resume-no-trace")

    plan = build_resume_plan("resume-no-trace")

    assert plan["project_id"] == "resume-no-trace"
    assert not (store.project_dir("resume-no-trace") / "agents" / "codex_skill" / "trace.jsonl").exists()


def test_resume_plan_and_cli_leave_workspace_hash_unchanged(
    isolated_projects, monkeypatch, capsys
):
    project_id = "resume-read-only"
    _seed_source(project_id)
    before = _workspace_hashes(project_id)

    direct = build_resume_plan(project_id)
    after_direct = _workspace_hashes(project_id)

    script_path = (
        Path(__file__).parents[1]
        / ".agents"
        / "skills"
        / "travel-guide-codex-orchestrator"
        / "scripts"
        / "travel_guide_bridge.py"
    )
    spec = importlib.util.spec_from_file_location("travel_guide_bridge_cli_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [str(script_path), "resume-plan", project_id])
    assert module.main() == 0
    cli_output = json.loads(capsys.readouterr().out)

    assert direct["project_id"] == project_id
    assert cli_output["project_id"] == project_id
    assert before == after_direct == _workspace_hashes(project_id)


def test_resume_plan_poi_change_only_requests_join_not_integration(
    isolated_projects, deterministic_resume_validators
):
    project_id = "resume-poi-rejoin"
    _seed_resume_project(project_id)
    _write_poi_manifest(project_id, [("武侯祠", "passed")])
    _write_valid_integration(project_id)
    store.write_poi_detail(
        project_id,
        "poi_detail_agent",
        PoiDetailFile(poi_name="武侯祠", day=1, routes=[], citation_status="passed"),
    )

    plan = build_resume_plan(project_id)

    assert plan["next_actions"][0]["action"] == "delegate_poi_detail"
    assert plan["next_actions"][0]["poi_name"] == "武侯祠"


def test_trace_schema_rejects_unknown_event_and_adds_automatic_fields(isolated_projects):
    _seed_source("trace-schema")

    result = append_trace(
        "trace-schema",
        {"event": "test_result", "summary": "恢复计划只读测试通过"},
    )
    event = result["event"]

    assert event["project_id"] == "trace-schema"
    assert event["route_structure_sha256"] == route_structure_sha256(
        store.read_workspace("trace-schema", "route_plan.json")
    )
    assert event["trace_schema_version"] == "codex-travel-guide-trace-v1"
    assert event["timestamp"]
    with pytest.raises(ValueError, match="不支持的trace event"):
        append_trace("trace-schema", {"event": "agent_fanout", "summary": "未注册事件"})


def test_agent_delegated_trace_records_role_prompt_and_host_model(isolated_projects):
    project_id = "trace-agent-fingerprint"
    _seed_source(project_id)
    prompt_fingerprint = baseline.compute_prompt_fingerprint()
    store.write_material_baseline(
        project_id,
        {
            "schema_version": "material-baseline-v1",
            "corpus_sha256": "fixed-corpus",
            "prompt_fingerprint": prompt_fingerprint,
        },
    )
    store.write_experiment_manifest(
        project_id,
        {
            "scenario_id": "same-case",
            "experiment_version": "2026-09-05-v1",
            "harness": "codex_skill",
            "comparison_grade": "A",
            "model": "gpt-5.6-sol",
        },
    )

    result = append_trace(
        project_id,
        {
            "event": "agent_delegated",
            "task_id": "task-lifestyle-day-1",
            "role": "travel-lifestyle",
            "summary": "委派Lifestyle日级工作单元",
        },
    )

    event = result["event"]
    assert event["host_model"] == "gpt-5.6-sol"
    assert event["task_id"] == "task-lifestyle-day-1"
    assert event["prompt_fingerprint"]["role"] == "lifestyle"
    assert event["prompt_fingerprint"]["aggregate_sha256"] == prompt_fingerprint[
        "aggregate_sha256"
    ]


@pytest.mark.parametrize("event_type", ["agent_delegated", "agent_completed"])
@pytest.mark.parametrize("task_id", [None, "   "])
def test_agent_trace_requires_nonempty_task_id_before_writing(
    isolated_projects,
    event_type,
    task_id,
):
    project_id = f"trace-task-id-{event_type}-{task_id is None}"
    _seed_source(project_id)
    trace_path = store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"

    with pytest.raises(ValueError, match="task_id"):
        append_trace(
            project_id,
            {
                "event": event_type,
                "task_id": task_id,
                "summary": "agent lifecycle",
            },
        )

    assert not trace_path.exists()


def test_agent_trace_reuses_same_task_id_across_delegated_and_completed(
    isolated_projects,
):
    project_id = "trace-task-lifecycle"
    _seed_source(project_id)
    task_id = "task-route-validation"

    append_trace(
        project_id,
        {
            "event": "agent_delegated",
            "task_id": task_id,
            "role": "travel-route",
            "summary": "委派路线校验",
        },
    )
    append_trace(
        project_id,
        {
            "event": "agent_completed",
            "task_id": task_id,
            "role": "travel-route",
            "summary": "路线校验完成",
        },
    )

    trace_path = store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [event["task_id"] for event in events] == [task_id, task_id]


@pytest.mark.parametrize("delegated_task_id", [None, "task-other"])
def test_agent_completed_trace_requires_matching_prior_delegation(
    isolated_projects,
    delegated_task_id,
):
    project_id = f"trace-completion-link-{delegated_task_id or 'orphan'}"
    _seed_source(project_id)
    trace_path = store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"
    if delegated_task_id:
        append_trace(
            project_id,
            {
                "event": "agent_delegated",
                "task_id": delegated_task_id,
                "role": "travel-route",
                "summary": "委派路线校验",
            },
        )
    before = trace_path.read_bytes() if trace_path.exists() else None

    with pytest.raises(ValueError, match="已有agent_delegated"):
        append_trace(
            project_id,
            {
                "event": "agent_completed",
                "task_id": "task-completed",
                "role": "travel-route",
                "summary": "路线校验完成",
            },
        )

    after = trace_path.read_bytes() if trace_path.exists() else None
    assert after == before


def test_freeze_material_baseline_is_idempotent_and_refuses_drift(isolated_projects):
    project_id = "immutable-material-baseline"
    _seed_source(project_id)

    frozen = freeze_material_baseline(project_id)
    assert freeze_material_baseline(project_id) == frozen

    chunks_path = isolated_projects / "material_library" / "index" / "成都" / "chunks.jsonl"
    chunks_path.write_text(
        '{"chunk_id":"chunk-2","doc_id":"doc-1","text":"changed"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不得覆盖重冻"):
        freeze_material_baseline(project_id)
    assert store.read_workspace(project_id, "material_baseline.json") == frozen


def test_web_search_ledger_counts_results_not_log_rows(isolated_projects, monkeypatch):
    project_id = "web-search-ledger"
    _seed_source(project_id)

    class FakeWebTool:
        def __init__(self, log_path):
            self.log_path = log_path

        def invoke(self, payload):
            entry = build_web_search_log_entry(
                payload["query"],
                [
                    {
                        "title": "官方公告",
                        "excerpt": "开放信息",
                        "url": "https://example.com/official",
                    },
                    {
                        "title": "交通提示",
                        "excerpt": "交通信息",
                        "url": "https://example.com/traffic",
                    },
                ],
            )
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
            return "two rendered results"

    def fake_factory(pid, log_scope):
        log_path = store.project_dir(pid) / "agents" / log_scope / "web_search_log.jsonl"
        return [FakeWebTool(log_path)]

    monkeypatch.setattr(bridge, "make_web_search_tools", fake_factory)

    result = bridge.web_search(
        project_id,
        stage="route",
        day=1,
        query="武侯祠 当前开放公告",
    )

    assert result["research"]["status"] == "success"
    assert result["research"]["result_count"] == 2
    assert store.read_research_runs(project_id)[0]["result_count"] == 2
