from __future__ import annotations

import json

import pytest

from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store
from src.workspace.integration_context import integration_delivery_view
from src.workspace.route_validation import route_structure_hash
from src.workspace.schema import RouteDay, RouteFeasibilityReport, RoutePlan, RoutePoi


def _tool(tools, name: str):
    return next(item for item in tools if item.name == name)


def test_delivery_view_removes_raw_evidence_but_preserves_claim_mapping():
    raw = {
        "food": {
            "items": [
                {
                    "day": 1,
                    "recommendation": "Try the teahouse.",
                    "sources": [{"chunk_id": "chunk-1"}],
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "claim_text": "The teahouse is in the park.",
                            "claim_type": "location",
                            "support_mode": "direct",
                            "evidence_refs": [{"chunk_id": "chunk-1"}],
                            "evidence_snapshots": [{"excerpt": "raw evidence"}],
                        }
                    ],
                }
            ]
        }
    }

    projected = integration_delivery_view("outputs/lifestyle.json", raw)

    item = projected["food"]["items"][0]
    claim = item["claims"][0]
    assert item["recommendation"] == "Try the teahouse."
    assert "sources" not in item
    assert claim["claim_id"] == "claim-1"
    assert claim["claim_text"] == "The teahouse is in the park."
    assert "evidence_refs" not in claim
    assert "evidence_snapshots" not in claim
    assert raw["food"]["items"][0]["claims"][0]["evidence_snapshots"]


def test_delivery_view_filters_failed_claim_ids_without_mutating_workspace_value():
    raw = {
        "food": {
            "items": [
                {
                    "day": 1,
                    "recommendation": "一条可用建议和一条待处理建议。",
                    "claims": [
                        {"claim_id": "claim-pass", "claim_text": "一条可用建议。"},
                        {"claim_id": "claim-fail", "claim_text": "一条待处理建议。"},
                    ],
                }
            ]
        }
    }

    projected = integration_delivery_view(
        "outputs/lifestyle.json",
        raw,
        deliverable_claim_ids={"claim-pass"},
    )

    assert [item["claim_id"] for item in projected["food"]["items"][0]["claims"]] == [
        "claim-pass"
    ]
    assert projected["food"]["items"][0]["recommendation"] == "一条可用建议。"
    assert len(raw["food"]["items"][0]["claims"]) == 2


def test_delivery_view_rebuilds_partial_pitfall_warning_from_deliverable_claims():
    raw = {
        "play": {
            "items": [
                {
                    "day": 2,
                    "target_recommendation": "原建议中包含已被拦截的事实。",
                    "warning": "一条有据风险，一条待核验风险。",
                    "claims": [
                        {
                            "claim_id": "claim-pass",
                            "target_path": "warning",
                            "claim_text": "一条有据风险。",
                        },
                        {
                            "claim_id": "claim-fail",
                            "target_path": "warning",
                            "claim_text": "一条待核验风险。",
                        },
                    ],
                }
            ]
        }
    }

    projected = integration_delivery_view(
        "outputs/pitfall.json",
        raw,
        deliverable_claim_ids={"claim-pass"},
    )

    assert projected["play"]["items"][0]["warning"] == "一条有据风险。"
    assert "target_recommendation" not in projected["play"]["items"][0]


def test_delivery_view_filters_old_final_block_mappings():
    raw = {
        "blocks": [
            {
                "block_id": "block-1",
                "upstream_claim_ids": ["claim-pass", "claim-fail"],
                "claim_snapshots": [{"claim_id": "claim-fail", "claim_text": "raw"}],
            }
        ]
    }

    projected = integration_delivery_view(
        "final_guide.blocks.json",
        raw,
        deliverable_claim_ids={"claim-pass"},
    )

    assert projected["blocks"][0]["upstream_claim_ids"] == ["claim-pass"]
    assert "claim_snapshots" not in projected["blocks"][0]


def test_delivery_view_keeps_feasibility_outcome_without_raw_results():
    raw = {
        "overall_status": "unknown",
        "checks": [
            {
                "check_id": "poi-1",
                "target": "Wuhou Shrine",
                "status": "unknown",
                "summary": "Automatic verification did not complete.",
                "suggested_action": "Check the official channel before departure.",
                "technical_error": "provider timeout with internal details",
                "evidence": [{"excerpt": "long web result"}],
            }
        ],
    }

    projected = integration_delivery_view("route_feasibility.json", raw)

    check = projected["checks"][0]
    assert check["status"] == "unknown"
    assert check["summary"] == "Automatic verification did not complete."
    assert check["suggested_action"] == "Check the official channel before departure."
    assert check["technical_error"] is True
    assert "evidence" not in check


def test_delivery_view_omits_passed_feasibility_checks_but_keeps_overall_status():
    raw = {
        "overall_status": "passed",
        "force_finalized": False,
        "checks": [
            {
                "check_id": "poi-pass",
                "target": "Wuhou Shrine",
                "status": "passed",
                "summary": "Open normally.",
                "evidence": [{"excerpt": "long web result"}],
            }
        ],
    }

    projected = integration_delivery_view("route_feasibility.json", raw)

    assert projected == {
        "overall_status": "passed",
        "force_finalized": False,
        "checks": [],
    }


def test_route_map_delivery_view_keeps_canonical_facts_without_raw_diagnostics():
    route_plan = {
        "days": [
            {
                "day": 7,
                "city": "甘肃",
                "area": "",
                "pois": [
                    {"name": "七彩丹霞", "needs_detail": False},
                    {"name": "军马场", "needs_detail": True},
                ],
            }
        ]
    }
    report = {
        "route_plan_structure_hash": route_structure_hash(route_plan),
        "status": "passed",
        "days": [
            {
                "day": 7,
                "entities": [
                    {
                        "original_name": "军马场",
                        "query_name": "山丹皇家马场景区",
                        "status": "resolved",
                        "candidates": [{"raw_provider_response": "不应进入Integration"}],
                        "selected": {
                            "provider_id": "B0FFH2K8TM",
                            "provider_name": "山丹皇家马场景区",
                            "province": "甘肃省",
                            "city": "张掖市",
                            "district": "山丹县",
                            "lng": 101.364549,
                            "lat": 38.063343,
                        },
                    }
                ],
                "legs": [
                    {
                        "from": "七彩丹霞",
                        "to": "军马场",
                        "provider_from": "张掖七彩丹霞景区",
                        "provider_to": "山丹皇家马场景区",
                        "distance_km": 188.9,
                        "duration_h": 3.29,
                        "transport_mode": "driving",
                        "status": "passed",
                        "provider_response": "不应进入Integration",
                    }
                ],
                "totals": {"distance_km": 188.9, "duration_h": 3.29},
                "schedule_check": {"raw": "不应进入Integration"},
            }
        ],
        "issues": [{"raw_provider_response": "不应进入Integration"}],
    }

    projected = integration_delivery_view(
        "route_map_validation.json", report, route_plan=route_plan
    )

    assert projected["status"] == "current"
    day = projected["days"][0]
    assert day["entities"] == [
        {
            "original_name": "军马场",
            "status": "resolved",
            "provider_id": "B0FFH2K8TM",
            "provider_name": "山丹皇家马场景区",
            "province": "甘肃省",
            "city": "张掖市",
            "district": "山丹县",
            "lng": 101.364549,
            "lat": 38.063343,
        }
    ]
    assert day["legs"][0]["distance_km"] == 188.9
    assert day["legs"][0]["duration_h"] == 3.29
    assert day["totals"] == {"distance_km": 188.9, "duration_h": 3.29}
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "raw_provider_response" not in serialized
    assert "provider_response" not in serialized
    assert "schedule_check" not in serialized


@pytest.mark.parametrize(
    "unsafe_status",
    [
        "anomaly",
        "provider_error",
        "not_measured_unresolved_entity",
        "unknown",
        "unknown_transport_mode",
        "mode_specific_not_driving",
    ],
)
def test_route_map_delivery_view_hides_untrusted_leg_measurements_and_totals(
    unsafe_status,
):
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [
                    {"name": "武侯祠"},
                    {"name": "锦里"},
                    {"name": "杜甫草堂"},
                ],
            }
        ]
    }
    report = {
        "route_plan_structure_hash": route_structure_hash(route_plan),
        "status": "needs_confirmation",
        "days": [
            {
                "day": 1,
                "entities": [],
                "legs": [
                    {
                        "from": "武侯祠",
                        "to": "锦里",
                        "distance_km": 1.2,
                        "duration_h": 0.1,
                        "transport_mode": "driving",
                        "status": "passed",
                    },
                    {
                        "from": "锦里",
                        "to": "杜甫草堂",
                        "distance_km": 999.9,
                        "duration_h": 99.9,
                        "transport_mode": "driving",
                        "status": unsafe_status,
                    },
                ],
                "totals": {"distance_km": 1001.1, "duration_h": 100.0},
            }
        ],
    }

    projected = integration_delivery_view(
        "route_map_validation.json", report, route_plan=route_plan
    )

    day = projected["days"][0]
    assert day["legs"][0]["distance_km"] == 1.2
    assert day["legs"][0]["duration_h"] == 0.1
    assert "distance_km" not in day["legs"][1]
    assert "duration_h" not in day["legs"][1]
    assert day["totals"] == {}


def test_route_map_delivery_view_hides_totals_when_expected_leg_is_missing():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [{"name": "武侯祠"}, {"name": "锦里"}],
            }
        ]
    }
    report = {
        "route_plan_structure_hash": route_structure_hash(route_plan),
        "status": "passed",
        "days": [
            {
                "day": 1,
                "entities": [],
                "legs": [],
                "totals": {"distance_km": 0, "duration_h": 0},
            }
        ],
    }

    projected = integration_delivery_view(
        "route_map_validation.json", report, route_plan=route_plan
    )

    assert projected["days"][0]["totals"] == {}


def test_route_map_delivery_view_rejects_stale_hash():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "area": "",
                "pois": [{"name": "武侯祠", "needs_detail": False}],
            }
        ]
    }
    report = {
        "route_plan_structure_hash": "old-hash",
        "status": "passed",
        "days": [{"day": 1, "entities": [{"original_name": "武侯祠"}], "legs": []}],
    }

    projected = integration_delivery_view(
        "route_map_validation.json", report, route_plan=route_plan
    )

    assert projected["status"] == "stale"
    assert projected["days"][0]["entities"] == []
    assert projected["days"][0]["legs"] == []


def test_integration_read_workspace_projects_current_route_map(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "integration-route-map"
    store.init_project(project_id)
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    pois=[RoutePoi(name="武侯祠"), RoutePoi(name="锦里")],
                )
            ]
        ),
    )
    route_plan = store.read_workspace(project_id, "route_plan.json")
    store._write_json(
        store.workspace_dir(project_id) / "route_map_validation.json",
        {
            "route_plan_structure_hash": route_structure_hash(route_plan),
            "status": "passed",
            "days": [
                {
                    "day": 1,
                    "entities": [],
                    "legs": [
                        {
                            "from": "武侯祠",
                            "to": "锦里",
                            "distance_km": 1.2,
                            "duration_h": 0.1,
                            "transport_mode": "driving",
                            "status": "passed",
                        }
                    ],
                    "totals": {"distance_km": 1.2, "duration_h": 0.1},
                }
            ],
        },
    )

    read_workspace = _tool(
        make_workspace_tools(project_id, "integration_agent"), "read_workspace"
    )
    result = json.loads(
        read_workspace.invoke({"relative_path": "route_map_validation.json"})
    )

    assert result["status"] == "current"
    assert result["days"][0]["legs"][0]["distance_km"] == 1.2


def test_integration_read_workspace_returns_delivery_view(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "integration-context-test"
    store.init_project(project_id)
    raw = {
        "overall_status": "passed",
        "checks": [
            {
                "check_id": "poi-1",
                "day": 1,
                "target": "Wuhou Shrine",
                "target_type": "poi",
                "category": "poi_operational_status",
                "status": "passed",
                "summary": "Open normally.",
                "evidence": [{"excerpt": "long web result"}],
            }
        ],
    }
    store.write_route_feasibility(
        project_id, "citation_checker", RouteFeasibilityReport.model_validate(raw)
    )

    read_workspace = _tool(make_workspace_tools(project_id, "integration_agent"), "read_workspace")
    result = json.loads(read_workspace.invoke({"relative_path": "route_feasibility.json"}))

    assert result == {
        "overall_status": "passed",
        "force_finalized": False,
        "checks": [],
    }
    route_read = _tool(make_workspace_tools(project_id, "route_agent"), "read_workspace")
    route_result = json.loads(route_read.invoke({"relative_path": "route_feasibility.json"}))
    assert route_result["checks"][0]["evidence"][0]["excerpt"] == "long web result"
    persisted = store.read_workspace(project_id, "route_feasibility.json")
    assert persisted["checks"][0]["evidence"][0]["excerpt"] == "long web result"


def test_integration_read_workspace_hides_machine_failed_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "integration-filtered-claims"
    store.init_project(project_id)
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "选择一家具体餐厅。",
                "claims": [
                    {
                        "target_path": "recommendation",
                        "claim_text": "选择一家具体餐厅。",
                        "claim_type": "food",
                        "support_mode": "estimate",
                    }
                ],
            }
        ],
    )
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    claim_id = lifestyle["food"]["items"][0]["claims"][0]["claim_id"]
    store.write_evidence_checks(
        project_id,
        "lifestyle",
        [{"target_key": "lifestyle:food:day:1", "claim_id": claim_id, "result": "fail"}],
    )

    read_workspace = _tool(make_workspace_tools(project_id, "integration_agent"), "read_workspace")
    result = json.loads(read_workspace.invoke({"relative_path": "outputs/lifestyle.json"}))

    assert result["food"]["items"][0]["claims"] == []
    assert result["food"]["items"][0]["recommendation"] == ""
    persisted = store.read_workspace(project_id, "outputs/lifestyle.json")
    assert persisted["food"]["items"][0]["claims"][0]["claim_id"] == claim_id
