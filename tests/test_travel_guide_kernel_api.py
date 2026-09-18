from __future__ import annotations

import json
import pickle
import subprocess
import sys
from pathlib import Path

from src import orchestrator
from src.codex_skill import bridge, preflight
from src.workspace import (
    fixed_route_setup,
    route_validation,
    schema as legacy_schema,
    store as legacy_store,
)
from travel_guide_kernel import models, schema as kernel_schema
from travel_guide_kernel.projects import prepare_fixed_route_project
from travel_guide_kernel.route import (
    route_structure_diff,
    route_structure_hash,
    route_structure_snapshot,
    validate_route_structure,
)
from travel_guide_kernel.workspace import store as kernel_store


ROOT = Path(__file__).resolve().parents[1]

ROUTE_MODEL_NAMES = (
    "EvidenceClaim",
    "EvidenceClaimEvidenceSnapshot",
    "RouteClaim",
    "RouteClaimEvidenceSnapshot",
    "RouteDay",
    "RouteEntities",
    "RouteEntityCandidate",
    "RouteEntityResolution",
    "RouteExecutionPlan",
    "RouteFeasibilityCheck",
    "RouteFeasibilityDateContext",
    "RouteFeasibilityEvidence",
    "RouteFeasibilityReport",
    "RouteGeoPoint",
    "RouteLeg",
    "RouteLegSegment",
    "RouteOperationalCommitment",
    "RoutePlan",
    "RoutePoi",
    "SourceRef",
)


def _route(*, poi: str = "武侯祠", notes: str = "说明") -> dict:
    return {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "area": "锦江区",
                "pois": [{"name": poi, "needs_detail": True}],
                "notes": notes,
                "sources": [{"platform": "official"}],
            }
        ]
    }


def test_route_facade_matches_legacy_contract():
    route = _route()

    assert route_structure_snapshot(route) == route_validation.route_structure_snapshot(route)
    assert route_structure_hash(route) == route_validation.route_structure_hash(route)
    assert validate_route_structure(route, expected_days=1) == []
    assert route_structure_diff(route, _route(notes="新说明")) == []
    assert route_structure_hash(route) == route_structure_hash(_route(notes="新说明"))
    assert route_structure_hash(route) != route_structure_hash(_route(poi="杜甫草堂"))


def test_both_consumers_and_legacy_path_share_one_route_implementation():
    assert route_validation.route_structure_snapshot is route_structure_snapshot
    assert route_validation.validate_route_structure is validate_route_structure
    assert bridge.route_structure_snapshot is route_structure_snapshot
    assert orchestrator.route_structure_snapshot is route_structure_snapshot
    assert orchestrator.route_structure_hash is route_structure_hash


def test_workspace_facade_preserves_one_store_module():
    assert kernel_store is legacy_store
    assert bridge.store is kernel_store
    assert preflight.store is kernel_store
    assert orchestrator.store is kernel_store
    assert fixed_route_setup.store is kernel_store


def test_legacy_store_alias_preserves_import_order_and_patch_state():
    for modules in (
        "from travel_guide_kernel.workspace import store as kernel_store; "
        "from src.workspace import store as legacy_store",
        "from src.workspace import store as legacy_store; "
        "from travel_guide_kernel.workspace import store as kernel_store",
    ):
        code = (
            f"{modules}; "
            "assert kernel_store is legacy_store; "
            "sentinel = object(); legacy_store.PROJECTS_DIR = sentinel; "
            "assert kernel_store.PROJECTS_DIR is sentinel"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


def test_workspace_schema_facade_reexports_kernel_classes():
    assert legacy_schema.Progress is kernel_schema.Progress
    assert legacy_schema.PoiDetailFile is kernel_schema.PoiDetailFile
    assert legacy_schema.FinalGuideBlocks is kernel_schema.FinalGuideBlocks


def test_legacy_workspace_schema_pickle_path_still_resolves():
    assert pickle.loads(b"csrc.workspace.schema\nProgress\n.") is kernel_schema.Progress


def test_consumers_import_store_through_workspace_facade():
    code = """
import json
import sys
import types

sentinel = object()
workspace = types.ModuleType('travel_guide_kernel.workspace')
workspace.store = sentinel
sys.modules['travel_guide_kernel.workspace'] = workspace

from src import orchestrator
from src.codex_skill import bridge, preflight
from src.workspace import fixed_route_setup

print(json.dumps({
    'bridge': bridge.store is sentinel,
    'preflight': preflight.store is sentinel,
    'orchestrator': orchestrator.store is sentinel,
    'fixed_route_setup': fixed_route_setup.store is sentinel,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "bridge": True,
        "preflight": True,
        "orchestrator": True,
        "fixed_route_setup": True,
    }


def test_route_models_are_available_from_public_module():
    plan = models.RoutePlan.model_validate(_route())

    assert plan.days[0].pois[0].name == "武侯祠"
    assert set(models.__all__) >= set(ROUTE_MODEL_NAMES)
    for name in ROUTE_MODEL_NAMES:
        kernel_model = getattr(models, name)
        assert kernel_model is getattr(legacy_schema, name)
        assert kernel_model.__module__ == "travel_guide_kernel.models"


def test_route_model_groups_round_trip_through_legacy_facade():
    payloads = {
        "RoutePlan": {
            "days": [
                {
                    "day": 1,
                    "city": "成都",
                    "pois": [{"name": "武侯祠", "needs_detail": True}],
                    "claims": [
                        {
                            "claim_id": "route-claim-1",
                            "claim_text": "武侯祠安排在第一天",
                            "evidence_refs": [{"platform": "official"}],
                            "evidence_snapshots": [
                                {
                                    "source": {"platform": "official"},
                                    "excerpt": "开放信息",
                                    "source_kind": "material",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "RouteEntities": {
            "route_plan_structure_hash": "route-hash",
            "entities": [
                {
                    "day": 1,
                    "poi_index": 0,
                    "formal_name": "武侯祠",
                    "location": {"lng": 104.047, "lat": 30.645},
                    "candidates": [
                        {
                            "provider_id": "amap-1",
                            "provider_name": "成都武侯祠博物馆",
                            "score": 0.98,
                        }
                    ],
                    "status": "resolved",
                }
            ],
        },
        "RouteExecutionPlan": {
            "route_plan_structure_hash": "route-hash",
            "route_legs": [
                {
                    "day": 1,
                    "from_poi_index": 0,
                    "to_poi_index": 1,
                    "transport_mode": "walking",
                    "segments": [
                        {
                            "from_name": "武侯祠",
                            "to_name": "锦里",
                            "mode": "walking",
                            "duration_minutes": 8,
                        }
                    ],
                }
            ],
            "operational_commitments": [
                {
                    "action": "核实预约规则",
                    "category": "reservation",
                    "target_day": 1,
                    "target_poi_index": 0,
                }
            ],
        },
        "RouteFeasibilityReport": {
            "route_plan_structure_hash": "route-hash",
            "requirements_date_context": {
                "precision": "exact",
                "start_date": "2026-09-09",
            },
            "overall_status": "warning",
            "checks": [
                {
                    "check_id": "opening-1",
                    "day": 1,
                    "target": "武侯祠",
                    "target_type": "poi",
                    "category": "opening_window",
                    "status": "warning",
                    "coverage_status": "covered",
                    "evidence": [
                        {
                            "title": "官方开放信息",
                            "url": "https://example.com/official",
                            "source_type": "official",
                        }
                    ],
                }
            ],
        },
    }

    for name, payload in payloads.items():
        kernel_model = getattr(models, name)
        dumped = kernel_model.model_validate(payload).model_dump(mode="json")
        legacy_value = getattr(legacy_schema, name).model_validate(dumped)

        assert type(legacy_value) is kernel_model
        assert legacy_value.model_dump(mode="json") == dumped


def test_fixed_route_project_facade_delegates_lazily(monkeypatch):
    from src.workspace import fixed_route_setup

    captured = {}

    def fake_prepare(source_project_id, target_project_id, **kwargs):
        captured.update(
            source_project_id=source_project_id,
            target_project_id=target_project_id,
            **kwargs,
        )
        return {"project_id": target_project_id}

    monkeypatch.setattr(fixed_route_setup, "prepare_fixed_route_project", fake_prepare)

    result = prepare_fixed_route_project(
        "source",
        "target",
        harness="codex_skill",
        model="model-a",
    )

    assert result == {"project_id": "target"}
    assert captured["harness"] == "codex_skill"
    assert captured["model"] == "model-a"


def test_package_root_has_no_runtime_side_effect_imports():
    code = """
import json
import sys
import travel_guide_kernel
print(json.dumps({
    'bridge': 'src.codex_skill.bridge' in sys.modules,
    'orchestrator': 'src.orchestrator' in sys.modules,
    'providers': any(name.startswith('src.providers') for name in sys.modules),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "bridge": False,
        "orchestrator": False,
        "providers": False,
    }


def test_route_module_is_independent_of_host_runtime_imports():
    code = """
import json
import sys
import travel_guide_kernel.route
print(json.dumps(sorted(name for name in sys.modules if name == 'src' or name.startswith('src.'))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


def test_route_models_module_is_independent_of_host_runtime_imports():
    code = """
import json
import sys
import travel_guide_kernel.models
print(json.dumps(sorted(name for name in sys.modules if name == 'src' or name.startswith('src.'))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


def test_consumer_import_order_has_no_cycle():
    for modules in (
        "from travel_guide_kernel.models import RoutePlan; "
        "from src.workspace.schema import RoutePlan as LegacyRoutePlan; "
        "assert RoutePlan is LegacyRoutePlan",
        "from src.workspace.schema import RoutePlan as LegacyRoutePlan; "
        "from travel_guide_kernel.models import RoutePlan; "
        "assert RoutePlan is LegacyRoutePlan",
        "import travel_guide_kernel.workspace; import src.workspace.fixed_route_setup; "
        "import src.codex_skill.preflight; import src.orchestrator",
        "import src.orchestrator; import src.codex_skill.preflight; "
        "import src.workspace.fixed_route_setup; import travel_guide_kernel.workspace",
    ):
        subprocess.run(
            [sys.executable, "-c", modules],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
