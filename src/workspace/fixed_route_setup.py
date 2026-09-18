"""Prepare one isolated fixed-route project from a canonical input project."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from src.materials.baseline import validate_material_baseline
from src.workspace.route_validation import route_structure_snapshot, validate_route_structure
from src.workspace.schema import ProjectPhase
from travel_guide_kernel.models import (
    RouteClaim,
    RouteDay,
    RouteEntities,
    RouteExecutionPlan,
    RoutePlan,
)
from travel_guide_kernel.workspace import store


Harness = Literal["langgraph", "codex_skill"]

_STRICT_PREFLIGHT_REQUIRED_CHECKS = {
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


def _fixed_route_claim_id(day: int, target_path: str, claim_type: str, claim_text: str) -> str:
    seed = "|".join([str(day), target_path, claim_type, claim_text])
    return f"route-claim-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"


def _fixed_route_claim(
    day: int,
    target_path: str,
    claim_text: str,
    *,
    claim_type: str,
    support_mode: Literal["user_constraint", "estimate"] = "user_constraint",
    subject: str = "",
    claimed_entities: list[str] | None = None,
) -> RouteClaim:
    return RouteClaim(
        claim_id=_fixed_route_claim_id(day, target_path, claim_type, claim_text),
        target_path=target_path,
        subject=subject,
        claim_text=claim_text,
        claim_type=claim_type,
        support_mode=support_mode,
        claimed_entities=claimed_entities or [],
    )


def _project_fixed_route_claims(route_plan: dict[str, Any]) -> RoutePlan:
    """Represent imported user-confirmed structure as auditable claim-level facts."""
    plan = RoutePlan.model_validate(route_plan)
    projected_days: list[RouteDay] = []
    for day in plan.days:
        if day.claims:
            projected_days.append(day)
            continue

        claims: list[RouteClaim] = []
        if day.notes.strip():
            claims.append(
                _fixed_route_claim(
                    day.day,
                    "notes",
                    day.notes.strip(),
                    claim_type="user_route_note",
                )
            )
        for index, poi in enumerate(day.pois):
            poi_name = poi.name.strip()
            if not poi_name:
                continue
            claims.append(
                _fixed_route_claim(
                    day.day,
                    f"pois.{index}.name",
                    poi_name,
                    claim_type="poi_identity",
                    subject=poi_name,
                    claimed_entities=[poi_name],
                )
            )
            if poi.brief.strip():
                claims.append(
                    _fixed_route_claim(
                        day.day,
                        f"pois.{index}.brief",
                        poi.brief.strip(),
                        claim_type="user_route_note",
                        subject=poi_name,
                        claimed_entities=[poi_name],
                    )
                )
            if poi.needs_detail_reason.strip():
                claims.append(
                    _fixed_route_claim(
                        day.day,
                        f"pois.{index}.needs_detail_reason",
                        poi.needs_detail_reason.strip(),
                        claim_type="detail_scope_assessment",
                        support_mode="estimate",
                        subject=poi_name,
                        claimed_entities=[poi_name],
                    )
                )
        projected_days.append(day.model_copy(update={"claims": claims, "sources": []}))
    return plan.model_copy(update={"days": projected_days})


def _structure_sha(route_plan: dict[str, Any]) -> str:
    payload = json.dumps(
        route_structure_snapshot(route_plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_grade_a_preflight_receipt(
    receipt: dict[str, Any] | None,
    *,
    source_project_id: str,
    target_project_id: str,
    saved_requirements: dict[str, Any],
    route_sha: str,
) -> dict[str, Any]:
    """Bind a Codex Grade A receipt to the exact inputs prepared below."""
    if not isinstance(receipt, dict):
        raise ValueError("Codex Grade A固定路线准备必须消费Live Preflight回执")

    expected_requirements_sha = _canonical_sha(saved_requirements)
    checks = receipt.get("checks") if isinstance(receipt.get("checks"), dict) else {}
    required_checks = {
        str(name) for name in (receipt.get("required_checks") or []) if str(name)
    }
    failures: list[str] = []
    expected_fields = {
        "schema_version": "codex-fixed-route-preflight-v2",
        "mode": "fixed_route",
        "source_project_id": source_project_id,
        "target_project_id": target_project_id,
        "observation_mode": "live",
        "requirements_sha256": expected_requirements_sha,
        "route_structure_sha256": route_sha,
    }
    for field, expected in expected_fields.items():
        if receipt.get(field) != expected:
            failures.append(f"{field}_mismatch")
    if receipt.get("ready") is not True:
        failures.append("ready_not_true")
    if receipt.get("comparison_sidecars_required") is not True:
        failures.append("comparison_sidecars_not_required")
    if receipt.get("embedding_required") is not True:
        failures.append("embedding_not_required")
    if not _STRICT_PREFLIGHT_REQUIRED_CHECKS.issubset(required_checks):
        failures.append("required_checks_incomplete")
    for name in sorted(_STRICT_PREFLIGHT_REQUIRED_CHECKS):
        check = checks.get(name)
        if not isinstance(check, dict) or check.get("status") != "passed":
            failures.append(f"{name}_not_passed")
    target_check = checks.get("target_project") or {}
    if not isinstance(target_check, dict) or target_check.get("exists") is not False:
        failures.append("target_existed_during_preflight")
    if not str(receipt.get("checked_at") or "").strip():
        failures.append("checked_at_missing")
    if failures:
        raise ValueError(
            "Grade A Live Preflight回执与当前输入不一致或未完整通过: "
            + ", ".join(failures)
        )

    return {
        "schema_version": receipt["schema_version"],
        **(
            {"report_path": str(receipt["report_path"])}
            if str(receipt.get("report_path") or "").strip()
            else {}
        ),
        "checked_at": receipt["checked_at"],
        "source_project_id": source_project_id,
        "target_project_id": target_project_id,
        "requirements_sha256": expected_requirements_sha,
        "route_structure_sha256": route_sha,
        "observation_mode": "live",
        "ready": True,
        "comparison_sidecars_passed": True,
        "embedding_passed": True,
    }


def _needs_detail_targets(route_plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "day": day.get("day"),
            "poi_name": poi.get("name", ""),
            "needs_detail_reason": poi.get("needs_detail_reason", ""),
        }
        for day in route_plan.get("days") or []
        for poi in day.get("pois") or []
        if isinstance(poi, dict) and poi.get("needs_detail")
    ]


def prepare_fixed_route_project(
    source_project_id: str,
    target_project_id: str,
    *,
    harness: Harness,
    model: str | None = None,
    provider: str | None = None,
    comparison_grade: str = "C",
    scenario_id: str | None = None,
    require_comparison_sidecars: bool = False,
    preflight_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one target project without inheriting any downstream answers.

    The source project is the shared comparison denominator.  Calling this
    function never implies that the peer Harness must also be created or run.
    """
    if store.project_dir(target_project_id).exists():
        raise FileExistsError(f"target project already exists: {target_project_id}")
    if comparison_grade.upper() == "A" and not model:
        raise ValueError("A级正式对照必须显式记录内容生成模型")
    saved_requirements = store.read_requirements(source_project_id)
    route_plan = store.read_workspace(source_project_id, "route_plan.json")
    if not isinstance(saved_requirements, dict):
        raise FileNotFoundError(f"source project {source_project_id!r} 缺少 requirements.json")
    if not isinstance(route_plan, dict):
        raise FileNotFoundError(f"source project {source_project_id!r} 缺少 route_plan.json")

    topic = str(saved_requirements.get("topic") or "").strip()
    requirements = saved_requirements.get("requirements") or {}
    destination = str(requirements.get("destination") or "").strip()
    expected_days = requirements.get("days") if isinstance(requirements, dict) else None
    if not isinstance(expected_days, int):
        expected_days = None
    structure_issues = validate_route_structure(route_plan, expected_days=expected_days)
    if structure_issues:
        raise ValueError(f"source route structure is invalid: {structure_issues}")

    route_sha = _structure_sha(route_plan)
    validated_preflight_receipt = (
        _validate_grade_a_preflight_receipt(
            preflight_receipt,
            source_project_id=source_project_id,
            target_project_id=target_project_id,
            saved_requirements=saved_requirements,
            route_sha=route_sha,
        )
        if harness == "codex_skill" and comparison_grade.upper() == "A"
        else None
    )
    snapshot = store.read_workspace(source_project_id, "fixed_route_input_snapshot.json")
    material_baseline = store.read_workspace(source_project_id, "material_baseline.json")
    entities_raw = store.read_workspace(source_project_id, "route_entities.json")
    execution_raw = store.read_workspace(source_project_id, "route_execution.json")
    missing_sidecars = [
        name
        for name, value in (
            ("fixed_route_input_snapshot.json", snapshot),
            ("route_entities.json", entities_raw),
            ("route_execution.json", execution_raw),
            ("material_baseline.json", material_baseline),
        )
        if not isinstance(value, dict)
    ]
    if require_comparison_sidecars and missing_sidecars:
        raise FileNotFoundError(
            "固定路线对照输入不完整: " + "、".join(missing_sidecars)
        )

    baseline_check = validate_material_baseline(
        material_baseline,
        topic=topic,
        destination=destination,
    )
    if require_comparison_sidecars and baseline_check["status"] != "passed":
        raise ValueError(
            "固定路线素材基线无效: "
            + json.dumps(baseline_check["issues"], ensure_ascii=False)
        )

    entities = RouteEntities.model_validate(entities_raw) if isinstance(entities_raw, dict) else None
    execution = (
        RouteExecutionPlan.model_validate(execution_raw)
        if isinstance(execution_raw, dict)
        else None
    )
    for name, value in (("route_entities", entities), ("route_execution", execution)):
        if value and value.route_plan_structure_hash and value.route_plan_structure_hash != route_sha:
            raise ValueError(f"{name}对应的路线结构已过期")

    # All source validation happens before the target directory is created.
    store.init_project(target_project_id)
    store.write_requirements(target_project_id, topic, requirements)
    store.write_route_plan(
        target_project_id,
        "route_agent",
        _project_fixed_route_claims(route_plan),
    )
    if isinstance(snapshot, dict):
        store.write_fixed_route_input_snapshot(target_project_id, snapshot)
    if isinstance(material_baseline, dict):
        store.write_material_baseline(target_project_id, material_baseline)
    if entities:
        store.write_route_entities(target_project_id, "route_agent", entities)
    if execution:
        store.write_route_execution(target_project_id, "route_agent", execution)
    store.update_progress(
        target_project_id,
        actor="main_agent",
        state=ProjectPhase.PARALLEL_RESEARCH,
        skeleton_confirmed=True,
    )

    resolved_scenario_id = scenario_id or (
        re.sub(r"-input-\d{8}.*$", "", source_project_id) or source_project_id
    )
    store.write_experiment_manifest(
        target_project_id,
        {
            "scenario_id": resolved_scenario_id,
            "experiment_version": target_project_id,
            "harness": harness,
            "model": model,
            "provider": provider,
            "comparison_grade": comparison_grade,
            "completion_mode": "in_progress",
            "inherited_downstream_outputs": False,
            "relations": [
                {
                    "relation_type": "source_input",
                    "target_project_id": source_project_id,
                    "note": "从唯一固定路线输入独立创建",
                }
            ],
            "evidence_docs": [],
            **(
                {"preflight_receipt": validated_preflight_receipt}
                if validated_preflight_receipt
                else {}
            ),
        },
    )

    imported_route = store.read_workspace(target_project_id, "route_plan.json") or {}
    return {
        "mode": "fixed_route",
        "source_project_id": source_project_id,
        "project_id": target_project_id,
        "harness": harness,
        "topic": topic,
        "route_structure_sha256": _structure_sha(imported_route),
        "route_structure_snapshot": route_structure_snapshot(imported_route),
        "copied_input_files": [
            "requirements.json",
            "route_plan.json",
            *([] if not isinstance(snapshot, dict) else ["fixed_route_input_snapshot.json"]),
            *([] if entities is None else ["route_entities.json"]),
            *([] if execution is None else ["route_execution.json"]),
            *([] if not isinstance(material_baseline, dict) else ["material_baseline.json"]),
        ],
        "missing_comparison_sidecars": missing_sidecars,
        "material_baseline_status": baseline_check["status"],
        "material_corpus_sha256": (
            material_baseline.get("corpus_sha256")
            if isinstance(material_baseline, dict)
            else None
        ),
        "prompt_fingerprint_sha256": (
            (material_baseline.get("prompt_fingerprint") or {}).get("aggregate_sha256")
            if isinstance(material_baseline, dict)
            else None
        ),
        "inherited_amap_verified": bool(imported_route.get("amap_verified")),
        "amap_rechecked_in_new_project": False,
        "valid_project_evidence_copied": 0,
        "needs_detail_targets": _needs_detail_targets(imported_route),
        **(
            {"preflight_receipt": validated_preflight_receipt}
            if validated_preflight_receipt
            else {}
        ),
    }
