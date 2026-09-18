"""Thin adapter between Codex Skills and the existing travel-guide business kernel."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from src.config import RUNTIME_DIR
from src.materials import citation_check, route_feasibility_check, route_map_check
from src.materials.baseline import (
    build_material_baseline,
    compute_prompt_fingerprint,
    validate_material_baseline,
)
from src.materials.project_evidence import list_project_evidence, write_project_evidence
from src.materials.research_service import (
    record_external_research,
    research_status as build_research_status,
    research_coverage_issues,
    retrieve_context,
    retrieve_project_evidence_context,
)
from src.materials.web_evidence import find_latest_web_search_entry
from src.materials import supplement as material_supplement
from src.tools.amap_tools import make_amap_tools
from src.tools.web_search_tools import make_web_search_tools
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import delivery
from src.workspace.final_guide_blocks import collect_upstream_claims
from src.workspace.final_guide_validation import validate_final_guide_contract
from src.workspace.integration_context import integration_delivery_view
from travel_guide_kernel.models import (
    RouteEntities,
    RouteExecutionPlan,
    RouteFeasibilityReport,
    RoutePlan,
)
from travel_guide_kernel.projects import prepare_fixed_route_project
from travel_guide_kernel.route import (
    route_structure_hash,
    route_structure_snapshot,
    validate_route_structure,
)
from travel_guide_kernel.workspace import store
from src.workspace.schema import (
    LIFESTYLE_FIELDS,
    ProjectPhase,
)


Stage = Literal["route", "lifestyle", "pitfall", "integration", "poi_detail", "final"]
RouteCheck = Literal["structure", "citation", "map", "feasibility"]

_ROUTE_STRUCTURE_BLOCKING_CODES = {
    "days_empty",
    "day_count_mismatch",
    "day_sequence_invalid",
    "day_number_invalid",
    "day_not_object",
    "day_city_empty",
    "day_pois_empty",
}

_STAGE_WRITERS = {
    "lifestyle": ("lifestyle_agent", "write_agent_output"),
    "pitfall": ("pitfall_agent", "write_agent_output"),
    "integration": ("integration_agent", "write_final_guide"),
    "poi_detail": ("poi_detail_agent", "write_agent_output"),
}

_ROUTE_ONLY_DOWNSTREAM_STAGES = {
    "lifestyle",
    "pitfall",
    "integration",
    "poi_detail",
    "final",
}
_ROUTE_ONLY_DOWNSTREAM_ROLES = {
    "travel-lifestyle",
    "lifestyle_agent",
    "travel-pitfall",
    "pitfall_agent",
    "travel-integration",
    "integration_agent",
    "travel-poi-detail",
    "poi_detail_agent",
}

_TRACE_EVENTS = {
    "run_start",
    "stage_start",
    "agent_delegated",
    "agent_completed",
    "validation_result",
    "user_confirmation",
    "retry",
    "observation",
    "run_completed",
    "stage_completed",
    "test_result",
}

_TRACE_SCHEMA_VERSION = "codex-travel-guide-trace-v1"
_RESUME_COMPLETE_STATUSES = {"completed", "completed_with_degradation"}
_FINAL_STALE_CODES = {
    "final_guide_claim_snapshots_stale",
    "final_guide_blocks_stale",
}
_ROUTE_MAP_ACCEPTABLE_ISSUE_CODES = {
    "route_map_spatial_separation_over_800km",
    "route_map_adjacent_leg_over_800km",
    "route_map_average_speed_over_120kmh",
    "route_map_average_speed_under_10kmh",
    "route_map_user_reference_deviation",
    "route_schedule_over_12h",
}
_ROUTE_MAP_DRIVING_MODES = {"driving", "taxi"}
_ROUTE_MAP_NON_DRIVING_MODES = {
    "public_transit",
    "scenic_shuttle",
    "cable_car",
    "walking",
    "rail",
}
_ROUTE_MAP_KNOWN_MODES = {
    *_ROUTE_MAP_DRIVING_MODES,
    *_ROUTE_MAP_NON_DRIVING_MODES,
    "mixed",
}
_ROUTE_MAP_LEG_STATUSES = {"passed", "anomaly", "mode_specific_not_driving"}
_ROUTE_MAP_SEGMENT_STATUSES = {"passed", "anomaly", "declared_mode_specific"}
_ROUTE_MAP_SCHEMA_VERSION = "route-map-validation-v1"
_ROUTE_MAP_PROVIDER = "高德Web服务API"
_ROUTE_CHECK_COMPONENTS = {
    "structure": "route_structure",
    "citation": "route_citation",
    "map": "route_map",
    "feasibility": "route_feasibility",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def route_structure_sha256(route_plan: Optional[dict]) -> str:
    return route_structure_hash(route_plan)


def _positive_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _consume_strict_fixed_route_preflight(
    source_project_id: str,
    target_project_id: str,
) -> dict[str, Any]:
    """Load the target-bound persisted receipt for kernel-level validation."""
    path = RUNTIME_DIR / "preflight" / f"{target_project_id}.json"
    if not path.is_file():
        raise ValueError("Grade A prepare-fixed-route缺少已持久化的Live Preflight回执")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Grade A Live Preflight回执不可读") from exc
    if not isinstance(report, dict):
        raise ValueError("Grade A Live Preflight回执必须是JSON对象")

    return {
        **report,
        "report_path": str(path.relative_to(RUNTIME_DIR.parent)),
    }


def integration_delivery_inputs(project_id: str) -> dict[str, Any]:
    """Return the same claim-filtered Integration inputs used by LangGraph."""
    route_plan = store.read_workspace(project_id, "route_plan.json")
    feasibility = store.read_workspace(project_id, "route_feasibility.json") or {}
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    poi_details = [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]
    deliverable_claim_ids = set(
        collect_upstream_claims(
            route_plan,
            lifestyle,
            pitfall,
            poi_details,
            evidence_checks=store.read_evidence_checks(project_id),
            review_findings=store.read_workspace(project_id, "review_findings.jsonl") or [],
        )
    )
    return {
        "route_plan": integration_delivery_view(
            "route_plan.json", route_plan, deliverable_claim_ids=deliverable_claim_ids
        ),
        "route_feasibility": integration_delivery_view(
            "route_feasibility.json", feasibility
        ),
        "lifestyle": integration_delivery_view(
            "outputs/lifestyle.json", lifestyle, deliverable_claim_ids=deliverable_claim_ids
        ),
        "pitfall": integration_delivery_view(
            "outputs/pitfall.json", pitfall, deliverable_claim_ids=deliverable_claim_ids
        ),
        "deliverable_claim_ids": sorted(deliverable_claim_ids),
    }


def _requirements_context(project_id: str) -> tuple[str, dict]:
    saved = store.read_requirements(project_id)
    if not saved:
        raise FileNotFoundError(f"project {project_id!r} 缺少 requirements.json")
    topic = str(saved.get("topic") or "").strip()
    requirements = saved.get("requirements") or {}
    if not topic:
        raise ValueError(f"project {project_id!r} 的 topic 为空")
    if not isinstance(requirements, dict):
        raise ValueError("requirements必须是JSON对象")
    return topic, requirements


def _needs_detail_targets(route_plan: dict) -> list[dict]:
    targets = []
    for day in route_plan.get("days") or []:
        for poi in day.get("pois") or []:
            if isinstance(poi, dict) and poi.get("needs_detail"):
                targets.append(
                    {
                        "day": day.get("day"),
                        "poi_name": poi.get("name", ""),
                        "needs_detail_reason": poi.get("needs_detail_reason", ""),
                    }
                )
    return targets


def prepare_fixed_route(
    source_project_id: str,
    target_project_id: str,
    *,
    model: str | None = None,
    comparison_grade: str = "C",
    require_comparison_sidecars: bool = False,
) -> dict:
    """Create an isolated project containing only requirements and a confirmed route."""
    normalized_grade = comparison_grade.upper()
    preflight_receipt = (
        _consume_strict_fixed_route_preflight(source_project_id, target_project_id)
        if normalized_grade == "A"
        else None
    )
    result = prepare_fixed_route_project(
        source_project_id,
        target_project_id,
        harness="codex_skill",
        model=model,
        provider="Codex",
        comparison_grade=normalized_grade,
        require_comparison_sidecars=(
            require_comparison_sidecars or normalized_grade == "A"
        ),
        preflight_receipt=preflight_receipt,
    )
    result["source_valid_project_evidence_count"] = len(
        list_project_evidence(source_project_id)
    )
    return result


def freeze_material_baseline(project_id: str) -> dict[str, Any]:
    """Explicitly freeze a canonical input Project before formal preflight."""
    topic, requirements = _requirements_context(project_id)
    destination = str(requirements.get("destination") or "").strip()
    existing = store.read_workspace(project_id, "material_baseline.json")
    if isinstance(existing, dict):
        existing_check = validate_material_baseline(
            existing,
            topic=topic,
            destination=destination,
        )
        if existing_check["status"] != "passed":
            raise ValueError(
                "已有素材基线与当前输入不一致，正式对照不得覆盖重冻: "
                + json.dumps(existing_check["issues"], ensure_ascii=False)
            )
        return existing
    baseline = build_material_baseline(topic, destination)
    check = validate_material_baseline(
        baseline,
        topic=topic,
        destination=destination,
    )
    if check["status"] != "passed":
        raise ValueError(
            "不能冻结无效素材基线: " + json.dumps(check["issues"], ensure_ascii=False)
        )
    store.write_material_baseline(project_id, baseline)
    return baseline


def _research_scope(
    stage: str,
    *,
    day: int | None = None,
    poi_name: str | None = None,
    field: str | None = None,
) -> tuple[str, str, str]:
    actor_by_stage = {
        "route": "route_agent",
        "lifestyle": "lifestyle_agent",
        "pitfall": "pitfall_agent",
        "poi_detail": "poi_detail_agent",
    }
    actor = actor_by_stage.get(stage)
    if actor is None:
        raise ValueError(f"stage {stage!r} 不允许执行上游研究")
    if field and field not in LIFESTYLE_FIELDS:
        raise ValueError(f"field必须是{list(LIFESTYLE_FIELDS)}之一")
    if stage == "poi_detail":
        if day is None or not poi_name:
            raise ValueError("poi_detail研究必须提供day和poi_name")
        log_scope = f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}"
        target_key = f"poi_detail:day:{day}:poi:{poi_name}"
    elif stage == "lifestyle":
        if day is None:
            raise ValueError("lifestyle研究必须提供day")
        log_scope = f"lifestyle_agent/day{day}"
        target_key = f"lifestyle:{field or 'day'}:day:{day}"
    elif stage == "pitfall":
        if day is None or not field:
            raise ValueError("pitfall研究必须提供day和field")
        log_scope = "pitfall_agent"
        target_key = f"pitfall:{field}:day:{day}"
    else:
        log_scope = "route_agent"
        target_key = f"route:day:{day}" if day is not None else "route:all"
    return actor, log_scope, target_key


def material_context(
    project_id: str,
    *,
    stage: str,
    query: str,
    day: int | None = None,
    poi_name: str | None = None,
    field: str | None = None,
    top_k: int = 8,
) -> dict[str, Any]:
    actor, _log_scope, target_key = _research_scope(
        stage, day=day, poi_name=poi_name, field=field
    )
    target_sha256 = None
    if stage == "pitfall" and day is not None and field in LIFESTYLE_FIELDS:
        lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
        recommendation = next(
            (
                str(item.get("recommendation") or "")
                for item in (lifestyle.get(field) or {}).get("items", []) or []
                if item.get("day") == day
            ),
            "",
        )
        if recommendation:
            target_sha256 = hashlib.sha256(recommendation.encode("utf-8")).hexdigest()
    return retrieve_context(
        project_id,
        actor=actor,
        stage=stage,
        target_key=target_key,
        query=query,
        top_k=top_k,
        target_sha256=target_sha256,
        target_entity=poi_name if stage == "poi_detail" else None,
    )


def _requirements_identity(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    requirements = payload.get("requirements")
    if not isinstance(requirements, dict):
        return None
    return {
        "topic": str(payload.get("topic") or ""),
        "requirements": requirements,
    }


def _manifest_claims_route_only(manifest: Any) -> bool:
    if not isinstance(manifest, dict):
        return False
    relations = manifest.get("relations") or []
    return bool(
        manifest.get("mode") == "full_plan_route_only"
        or str(manifest.get("completion_mode") or "").startswith("route_draft_")
        or manifest.get("route_only_stop_state")
        or any(
            isinstance(relation, dict)
            and relation.get("type") == "requirements_only_source"
            for relation in relations
        )
    )


def _route_only_downstream_artifacts(project_id: str) -> list[str]:
    workspace = store.workspace_dir(project_id)
    artifacts = [
        relative_path
        for relative_path in (
            "outputs/lifestyle.json",
            "outputs/pitfall.json",
            "final_guide.md",
            "final_guide.blocks.json",
            "final_guide.meta.json",
        )
        if (workspace / relative_path).is_file()
    ]
    poi_dir = workspace / "outputs" / "poi_details"
    if poi_dir.is_dir():
        artifacts.extend(
            str(path.relative_to(workspace))
            for path in poi_dir.rglob("*")
            if path.is_file()
        )
    return sorted(set(artifacts))


def _route_only_downstream_trace_events(project_id: str) -> list[dict[str, Any]]:
    path = store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip()
        stage = str(item.get("stage") or "").strip()
        role = str(item.get("role") or item.get("agent") or "").strip()
        stage_lifecycle = event in {"stage_start", "stage_completed"}
        agent_lifecycle = event in {"agent_delegated", "agent_completed"}
        if (
            stage_lifecycle
            and stage in _ROUTE_ONLY_DOWNSTREAM_STAGES
            or agent_lifecycle
            and (
                stage in _ROUTE_ONLY_DOWNSTREAM_STAGES
                or role in _ROUTE_ONLY_DOWNSTREAM_ROLES
            )
        ):
            events.append(
                {
                    key: item.get(key)
                    for key in ("event", "stage", "role", "task_id", "timestamp")
                    if item.get(key) is not None
                }
            )
    return events


def _route_only_contract_issues(
    project_id: str,
    manifest: Any,
    *,
    expected_source_project_id: str | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(manifest, dict) or manifest.get("mode") != "full_plan_route_only":
        issues.append(
            {
                "code": "route_only_mode_invalid",
                "message": "route-only Project manifest mode必须为full_plan_route_only",
            }
        )
    if not isinstance(manifest, dict) or manifest.get("harness") != "codex_skill":
        issues.append(
            {
                "code": "route_only_manifest_invalid",
                "message": "route-only Project必须使用codex_skill manifest",
            }
        )
    if (
        not isinstance(manifest, dict)
        or manifest.get("inherited_downstream_outputs") is not False
    ):
        issues.append(
            {
                "code": "route_only_manifest_invalid",
                "message": "route-only Project必须明确禁止继承下游产物",
            }
        )
    relations = (manifest.get("relations") or []) if isinstance(manifest, dict) else []
    source_ids = [
        str(relation.get("project_id") or "").strip()
        for relation in relations
        if isinstance(relation, dict)
        and relation.get("type") == "requirements_only_source"
        and str(relation.get("project_id") or "").strip()
    ]
    if len(source_ids) != 1:
        issues.append(
            {
                "code": "route_only_source_relation_invalid",
                "message": "route-only Project必须且只能绑定一个requirements-only source",
            }
        )
        source_project_id = None
    else:
        source_project_id = source_ids[0]
    if (
        expected_source_project_id is not None
        and source_project_id != expected_source_project_id
    ):
        issues.append(
            {
                "code": "source_relation_mismatch",
                "message": "route-only Project绑定的source与本次请求不一致",
            }
        )
    if source_project_id:
        source_requirements = _requirements_identity(
            store.read_requirements(source_project_id)
        )
        target_requirements = _requirements_identity(store.read_requirements(project_id))
        if source_requirements is None or target_requirements is None:
            issues.append(
                {
                    "code": "route_only_requirements_missing",
                    "message": "route-only source或target缺少requirements",
                }
            )
        elif source_requirements != target_requirements:
            issues.append(
                {
                    "code": "route_only_requirements_mismatch",
                    "message": "route-only target requirements与source不一致",
                }
            )
    return source_project_id, issues


def _require_route_only_target(project_id: str) -> str:
    manifest = store.read_workspace(project_id, "experiment.json")
    source_project_id, issues = _route_only_contract_issues(project_id, manifest)
    if not _manifest_claims_route_only(manifest) or issues or not source_project_id:
        detail = ", ".join(str(item.get("code") or "unknown") for item in issues)
        raise PermissionError(
            "Route高德Bridge仅允许requirements-only Codex目标Project"
            + (f": {detail}" if detail else "")
        )
    return source_project_id


def _check_value(check: Any, key: str) -> Any:
    return check.get(key) if isinstance(check, dict) else getattr(check, key, None)


def _has_explicit_technical_error(check: Any) -> bool:
    return bool(
        str(_check_value(check, "technical_error") or "").strip()
        or str(_check_value(check, "technical_error_class") or "").strip()
        or _check_value(check, "stream_completed") is False
    )


def _exception_diagnostics(exc: Exception) -> dict:
    chain = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(
            {
                "class": type(current).__name__,
                "message": str(current),
                "repr": repr(current),
            }
        )
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return {
        "technical_error": str(exc),
        "technical_error_class": type(exc).__name__,
        "technical_error_repr": repr(exc),
        "technical_error_chain": chain,
        "technical_error_traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }


def _latest_evidence_error_context(project_id: str, stage: str) -> dict:
    evidence = store.read_evidence_checks(project_id)
    candidates = [
        item
        for item in evidence.get(stage, [])
        if isinstance(item, dict) and item.get("result") == "error"
    ]
    if not candidates:
        return {}
    latest = max(candidates, key=lambda item: str(item.get("checked_at") or ""))
    return {
        key: latest.get(source_key)
        for key, source_key in (
            ("technical_error_target_key", "target_key"),
            ("technical_error_scope", "scope"),
            ("technical_error_method", "method"),
        )
        if latest.get(source_key)
    }


def _route_amap_tool(project_id: str, tool_name: str):
    _require_route_only_target(project_id)
    topic, _requirements = _requirements_context(project_id)
    actor, log_scope, target_key = _research_scope("route")
    tools = make_amap_tools(
        topic,
        project_id=project_id,
        log_scope=log_scope,
        include_entity_resolution=True,
        research_context={
            "actor": actor,
            "stage": "route",
            "target_key": target_key,
        },
    )
    tool = next((item for item in tools if item.name == tool_name), None)
    if tool is None:
        raise RuntimeError(f"Route高德工具不可用: {tool_name}")
    return tool


def amap_resolve_route_entity(
    project_id: str,
    *,
    poi_name: str,
    city_hint: str,
) -> dict[str, Any]:
    raw = _route_amap_tool(project_id, "amap_resolve_route_entity").invoke(
        {"poi_name": poi_name, "city_hint": city_hint}
    )
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("amap_resolve_route_entity返回了无效JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("amap_resolve_route_entity返回值必须是JSON对象")
    return {
        "project_id": project_id,
        "stage": "route",
        "tool": "amap_resolve_route_entity",
        "result": result,
    }


def amap_driving_distance(
    project_id: str,
    *,
    poi_a: str,
    poi_b: str,
    city: str = "",
    day: int | None = None,
) -> dict[str, Any]:
    result = _route_amap_tool(project_id, "amap_driving_distance").invoke(
        {"poi_a": poi_a, "poi_b": poi_b, "city": city, "day": day}
    )
    return {
        "project_id": project_id,
        "stage": "route",
        "tool": "amap_driving_distance",
        "query": {
            "poi_a": poi_a,
            "poi_b": poi_b,
            "city": city,
            "day": day,
        },
        "result": result,
    }


def project_evidence_context(
    project_id: str,
    *,
    stage: str,
    query: str,
    day: int | None = None,
    poi_name: str | None = None,
    field: str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    if stage == "pitfall":
        raise PermissionError("Pitfall生产工具边界不包含Project Evidence检索")
    actor, log_scope, target_key = _research_scope(
        stage, day=day, poi_name=poi_name, field=field
    )
    return retrieve_project_evidence_context(
        project_id,
        actor=actor,
        stage=stage,
        target_key=target_key,
        log_scope=log_scope,
        query=query,
        top_k=top_k,
    )


def web_search(
    project_id: str,
    *,
    stage: str,
    query: str,
    day: int | None = None,
    poi_name: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    if stage == "pitfall":
        raise PermissionError("Pitfall生产工具边界不包含Web搜索")
    actor, log_scope, target_key = _research_scope(
        stage, day=day, poi_name=poi_name, field=field
    )
    log_path = store.project_dir(project_id) / "agents" / log_scope / "web_search_log.jsonl"
    tool = make_web_search_tools(project_id, log_scope)[0]
    output = tool.invoke({"query": query})
    entry = find_latest_web_search_entry(log_path, query)
    result_count = len((entry or {}).get("results") or [])
    status = "success" if result_count else "empty"
    research = record_external_research(
        project_id,
        actor=actor,
        stage=stage,
        target_key=target_key,
        query=query,
        source_kind="web",
        status=status,
        result_count=result_count,
        metadata={
            "log_scope": log_scope,
            "retrieved_urls": [
                str(item.get("url") or "")
                for item in ((entry or {}).get("results") or [])
                if item.get("url")
            ],
        },
    )
    return {"research": research, "log_scope": log_scope, "output": output}


def append_project_evidence(
    project_id: str,
    *,
    stage: str,
    summary: str,
    source_url: str,
    day: int | None = None,
    poi_name: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    if stage == "pitfall":
        raise PermissionError("Pitfall生产工具边界不包含Project Evidence写入")
    actor, log_scope, target_key = _research_scope(
        stage, day=day, poi_name=poi_name, field=field
    )
    evidence = write_project_evidence(
        project_id,
        actor=actor,
        log_scope=log_scope,
        summary=summary,
        source_url=source_url,
    )
    research = record_external_research(
        project_id,
        actor=actor,
        stage=stage,
        target_key=target_key,
        query=summary,
        source_kind="project_evidence",
        status="success",
        result_count=1,
        metadata={"note_id": evidence.note_id, "source_url": evidence.source_url},
    )
    return {
        "research": research,
        "evidence": evidence.model_dump(mode="json"),
        "evidence_ref": {
            "platform": "project_evidence",
            "doc_id": evidence.note_id,
            "chunk_id": f"project-evidence-{evidence.note_id}",
            "scope": "project_evidence",
            "source_url": evidence.source_url,
        },
    }


def research_status(project_id: str) -> dict[str, Any]:
    return build_research_status(project_id)


def supplement_material_gap(
    project_id: str,
    *,
    stage: str,
    expected_route_structure_sha256: str,
    expected_artifact_sha256: str,
    day: int | None = None,
    poi_name: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    """Run the fixed supplement policy for one failed work unit."""
    if stage not in {"route", "lifestyle", "poi_detail"}:
        raise ValueError("自动素材补采只开放给route、lifestyle和poi_detail")
    topic, _requirements = _requirements_context(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    actual_route_sha = route_structure_sha256(route_plan)
    if actual_route_sha != expected_route_structure_sha256:
        raise ValueError("confirmed_route_structure_changed")
    artifact = _validation_artifact(project_id, stage, day, poi_name)
    actual_artifact_sha = _canonical_sha256(artifact)
    if actual_artifact_sha != expected_artifact_sha256:
        raise ValueError("artifact_changed_since_validation")

    scope = _validation_scope(stage, day, poi_name)
    latest = next(
        (
            run
            for run in reversed(store.read_validation_runs(project_id))
            if run.get("stage") == stage
            and run.get("scope") == scope
            and run.get("artifact_sha256") == actual_artifact_sha
        ),
        None,
    )
    if not latest or latest.get("status") not in {"needs_repair", "blocked"}:
        raise ValueError("supplement_requires_current_failed_validation")
    issues = list(latest.get("issues") or [])

    if stage == "route":
        result = material_supplement.attempt_route_gap_supplement(
            project_id, topic, issues
        )
    elif stage == "lifestyle":
        if day is None or not field:
            raise ValueError("lifestyle补采必须提供day和field")
        scoped_issues = [
            issue
            for issue in issues
            if issue.get("day") == day and issue.get("field") == field
        ] or [{"day": day, "field": field, "missing_entities": []}]
        result = material_supplement.attempt_lifestyle_gap_supplement(
            project_id, topic, scoped_issues
        )
    else:
        if day is None or not poi_name:
            raise ValueError("poi_detail补采必须提供day和poi_name")
        result = material_supplement.attempt_poi_gap_supplement(
            project_id,
            topic,
            day=day,
            poi_name=poi_name,
        )
    return result.as_dict()


def prepare_route_only(source_project_id: str, target_project_id: str) -> dict:
    """Create an isolated project containing requirements and no inherited route."""
    target_dir = store.project_dir(target_project_id)
    if target_dir.exists():
        raise FileExistsError(f"target project already exists: {target_project_id}")

    saved_requirements = store.read_requirements(source_project_id)
    if not saved_requirements:
        raise FileNotFoundError(f"source project {source_project_id!r} 缺少 requirements.json")
    topic = str(saved_requirements.get("topic") or "").strip()
    requirements = saved_requirements.get("requirements") or {}
    if not topic:
        raise ValueError(f"source project {source_project_id!r} 的 topic 为空")
    if not isinstance(requirements, dict):
        raise ValueError("requirements必须是JSON对象")
    expected_days = requirements.get("days")
    if not isinstance(expected_days, int) or expected_days <= 0:
        raise ValueError("route-only实验要求 requirements.days 为正整数")

    store.init_project(target_project_id)
    store.write_requirements(target_project_id, topic, requirements)
    store.update_progress(
        target_project_id,
        actor="main_agent",
        state=ProjectPhase.ROUTE_PLANNING,
        skeleton_confirmed=False,
    )
    scenario_id = re.sub(r"-full-plan.*$", "", source_project_id) or source_project_id
    store.write_experiment_manifest(
        target_project_id,
        {
            "scenario_id": scenario_id,
            "experiment_version": target_project_id,
            "harness": "codex_skill",
            "mode": "full_plan_route_only",
            "model": None,
            "provider": "Codex",
            "comparison_grade": "C",
            "completion_mode": "in_progress",
            "inherited_downstream_outputs": False,
            "relations": [
                {
                    "type": "requirements_only_source",
                    "project_id": source_project_id,
                }
            ],
            "evidence_docs": [],
        },
    )
    return {
        "mode": "full_plan_route_only",
        "source_project_id": source_project_id,
        "project_id": target_project_id,
        "topic": topic,
        "requirements_sha256": _canonical_sha256(saved_requirements),
        "route_plan_inherited": False,
        "downstream_outputs_inherited": False,
        "project_evidence_inherited": False,
        "status": "ready_for_route_generation",
    }


def _existing_route_sidecar(
    project_id: str,
    filename: str,
    model_type: Any,
    route_sha: str,
) -> bool:
    payload = store.read_workspace(project_id, filename)
    if payload is None:
        return False
    try:
        model = model_type.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"existing {filename} is invalid") from exc
    if model.route_plan_structure_hash != route_sha:
        raise ValueError(f"existing {filename} is bound to a different route structure")
    return True


def write_route_plan_payload(project_id: str, payload: dict) -> dict:
    """Write a first full route draft and stop at the skeleton-confirmation gate."""
    _require_route_only_target(project_id)
    if not isinstance(payload, dict):
        raise ValueError("payload必须是JSON对象")
    unsupported = sorted(
        set(payload) - {"days", "amap_verified", "route_entities", "route_execution"}
    )
    if unsupported:
        raise ValueError(f"route plan写入不支持字段: {unsupported}")
    days = payload.get("days")
    if not isinstance(days, list) or not days:
        raise ValueError("route plan写入要求非空days数组")
    _topic, requirements = _requirements_context(project_id)
    expected_days = requirements.get("days")
    if not isinstance(expected_days, int):
        expected_days = None
    candidate = RoutePlan(days=days, amap_verified=bool(payload.get("amap_verified", False)))
    candidate_payload = candidate.model_dump(mode="json")
    candidate_sha = route_structure_sha256(candidate_payload)
    structure_issues = validate_route_structure(
        candidate_payload,
        expected_days=expected_days,
    )
    if structure_issues:
        raise ValueError(f"route draft structure is invalid: {structure_issues}")

    missing_claim_kinds: list[str] = []
    for day in days:
        if not isinstance(day, dict):
            continue
        claims = day.get("claims") or []
        for poi_index, poi in enumerate(day.get("pois") or []):
            if not isinstance(poi, dict) or not str(poi.get("name") or "").strip():
                continue
            target_path = f"pois.{poi_index}.name"
            target_claims = [
                claim
                for claim in claims
                if isinstance(claim, dict)
                and str(claim.get("target_path") or "")
                in {f"pois.{poi_index}", target_path}
            ]
            claim_types = {str(claim.get("claim_type") or "") for claim in target_claims}
            for required_type in ("poi_identity", "poi_selection"):
                if required_type not in claim_types:
                    missing_claim_kinds.append(
                        f"Day{day.get('day')}.{target_path}（{poi.get('name')}）缺少{required_type}"
                    )
    if missing_claim_kinds:
        raise ValueError(
            "自主路线的每个POI必须分别提交身份Claim和选择理由Claim："
            + "、".join(missing_claim_kinds)
        )
    route_entities_payload = payload.get("route_entities")
    route_entities_model = None
    if route_entities_payload is not None:
        if not isinstance(route_entities_payload, dict):
            raise ValueError("route_entities必须是JSON对象")
        route_entities_model = RouteEntities.model_validate(route_entities_payload)
        if (
            route_entities_model.route_plan_structure_hash
            and route_entities_model.route_plan_structure_hash != candidate_sha
        ):
            raise ValueError("route_entities is bound to a different route structure")
    route_execution_payload = payload.get("route_execution")
    route_execution_model = None
    if route_execution_payload is not None:
        if not isinstance(route_execution_payload, dict):
            raise ValueError("route_execution必须是JSON对象")
        route_execution_model = RouteExecutionPlan.model_validate(route_execution_payload)
        if (
            route_execution_model.route_plan_structure_hash
            and route_execution_model.route_plan_structure_hash != candidate_sha
        ):
            raise ValueError("route_execution is bound to a different route structure")

    existing_route = store.read_workspace(project_id, "route_plan.json")
    if isinstance(existing_route, dict):
        existing_sha = route_structure_sha256(existing_route)
        if existing_sha != candidate_sha:
            raise ValueError("route-only target already contains a different route structure")
    elif existing_route is not None:
        raise ValueError("existing route_plan.json is invalid")

    route_entities_present = _existing_route_sidecar(
        project_id,
        "route_entities.json",
        RouteEntities,
        candidate_sha,
    )
    route_execution_present = _existing_route_sidecar(
        project_id,
        "route_execution.json",
        RouteExecutionPlan,
        candidate_sha,
    )
    progress = store.read_progress(project_id)
    if progress.skeleton_confirmed:
        raise ValueError("route-only target skeleton is already confirmed")
    if progress.state not in {
        ProjectPhase.ROUTE_PLANNING,
        ProjectPhase.SKELETON_CONFIRM,
    }:
        raise ValueError("route-only target is outside the route confirmation boundary")

    route_plan_reused = isinstance(existing_route, dict)
    if route_plan_reused:
        result = "复用已写入的同结构路线草稿"
    else:
        topic, _requirements = _requirements_context(project_id)
        tools = make_workspace_tools(
            project_id,
            "route_agent",
            topic=topic,
            route_write_mode="full",
        )
        tool = next((item for item in tools if item.name == "write_route_plan"), None)
        if tool is None:
            raise RuntimeError("route full Workspace写入工具不可用")
        result = tool.invoke(
            {
                "days": days,
                "amap_verified": bool(payload.get("amap_verified", False)),
            }
        )
        if isinstance(result, str) and result.startswith("写入失败"):
            raise ValueError(result)

    route_entities_written = False
    if route_entities_model is not None and not route_entities_present:
        store.write_route_entities(
            project_id,
            "route_agent",
            route_entities_model,
        )
        route_entities_written = True
    route_execution_written = False
    if route_execution_model is not None and not route_execution_present:
        store.write_route_execution(
            project_id,
            "route_agent",
            route_execution_model,
        )
        route_execution_written = True

    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    persisted_issues = validate_route_structure(route_plan, expected_days=expected_days)
    if persisted_issues:
        raise RuntimeError(f"已写入route draft未通过结构校验: {persisted_issues}")
    if progress.state != ProjectPhase.SKELETON_CONFIRM:
        store.update_progress(
            project_id,
            actor="main_agent",
            state=ProjectPhase.SKELETON_CONFIRM,
            skeleton_confirmed=False,
        )
    return {
        "mode": "full_plan_route_only",
        "project_id": project_id,
        "stage": "route_draft",
        "result": result,
        "status": "awaiting_user_confirmation",
        "route_structure_sha256": route_structure_sha256(route_plan),
        "route_plan_reused": route_plan_reused,
        "needs_detail_targets": _needs_detail_targets(route_plan),
        "skeleton_confirmed": False,
        "route_entities_written": route_entities_written,
        "route_execution_written": route_execution_written,
    }


def write_route_entities_payload(project_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("route_entities payload必须是JSON对象")
    saved = store.write_route_entities(
        project_id,
        "route_agent",
        RouteEntities.model_validate(payload),
    )
    return saved.model_dump(mode="json")


def write_route_execution_payload(project_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("route_execution payload必须是JSON对象")
    saved = store.write_route_execution(
        project_id,
        "route_agent",
        RouteExecutionPlan.model_validate(payload),
    )
    return saved.model_dump(mode="json")


def _route_only_validation_state(
    project_id: str,
    route_plan: dict,
    *,
    expected_days: int | None,
) -> dict[str, Any]:
    route_sha = route_structure_sha256(route_plan)
    artifact_sha = _canonical_sha256(
        _validation_artifact(project_id, "route", None, None)
    )
    technical_issues: list[dict[str, Any]] = []
    blocking_issues = validate_route_structure(route_plan, expected_days=expected_days)
    business_issues: list[dict[str, Any]] = []
    receipts: dict[str, dict[str, Any]] = {}
    validation_runs = store.read_validation_runs(project_id)

    for checker, component in _ROUTE_CHECK_COMPONENTS.items():
        checker_runs = [
            run
            for run in validation_runs
            if isinstance(run, dict)
            and run.get("stage") == "route"
            and run.get("scope") == f"checker:{checker}"
        ]
        if not checker_runs:
            technical_issues.append(
                {
                    "code": f"route_only_{checker}_receipt_missing",
                    "message": f"route-only {checker} checker receipt is missing",
                }
            )
            continue
        receipt = checker_runs[-1]
        receipts[checker] = receipt
        component_statuses = (
            (receipt.get("route_gate") or {}).get("component_statuses") or {}
        )
        component_status = component_statuses.get(component)
        if receipt.get("route_checks") != [checker] or component_status is None:
            technical_issues.append(
                {
                    "code": f"route_only_{checker}_receipt_invalid",
                    "message": f"route-only {checker} checker receipt contract is invalid",
                }
            )
            continue
        receipt_issues = [
            issue
            for issue in (receipt.get("issues") or [])
            if isinstance(issue, dict)
        ]
        receipt_issue_codes = {
            str(code or "")
            for code in (receipt.get("issue_codes") or [])
            if str(code or "")
        }
        receipt_issue_codes.update(
            str(issue.get("code") or "")
            for issue in receipt_issues
            if str(issue.get("code") or "")
        )
        receipt_gate_status = str(
            (receipt.get("route_gate") or {}).get("status") or ""
        )
        receipt_status = str(receipt.get("status") or "")
        if receipt_status not in {"passed", "partial", "needs_repair", "blocked"} or (
            receipt_gate_status
            not in {"passed", "partial", "warning", "unknown", "blocked"}
        ):
            technical_issues.append(
                {
                    "code": f"route_only_{checker}_receipt_status_invalid",
                    "message": f"route-only {checker} checker receipt status is invalid",
                }
            )
            continue
        receipt_blocked = (
            receipt_status == "blocked"
            or receipt_gate_status == "blocked"
            or "confirmed_route_structure_changed" in receipt_issue_codes
            or any(code in _ROUTE_STRUCTURE_BLOCKING_CODES for code in receipt_issue_codes)
            or any(code.endswith("_blocked") for code in receipt_issue_codes)
        )
        if receipt_blocked:
            blocking_issues.append(
                {
                    "code": f"route_only_{checker}_receipt_blocked",
                    "message": f"route-only {checker} checker receipt is blocked",
                    "issue_codes": sorted(receipt_issue_codes),
                }
            )
            continue
        if any(_has_explicit_technical_error(issue) for issue in receipt_issues):
            technical_issues.append(
                {
                    "code": f"route_only_{checker}_receipt_technical_error",
                    "message": f"route-only {checker} checker receipt has a technical error",
                }
            )
            continue
        if component_status == "passed" and receipt_issue_codes:
            technical_issues.append(
                {
                    "code": f"route_only_{checker}_receipt_has_unresolved_issues",
                    "message": f"route-only {checker} checker receipt has unresolved issues",
                }
            )
            continue
        if receipt.get("route_structure_sha256") != route_sha:
            technical_issues.append(
                {
                    "code": f"route_only_{checker}_receipt_stale",
                    "message": f"route-only {checker} checker receipt is stale",
                }
            )
            continue
        if checker == "structure" and receipt.get("observation_mode") not in {
            "offline",
            "live",
        }:
            technical_issues.append(
                {
                    "code": "route_only_structure_receipt_mode_invalid",
                    "message": "route-only structure checker receipt mode is invalid",
                }
            )
            continue
        if checker != "structure":
            if receipt.get("observation_mode") != "live":
                technical_issues.append(
                    {
                        "code": f"route_only_{checker}_receipt_not_live",
                        "message": f"route-only {checker} checker receipt is not live",
                    }
                )
                continue
            if receipt.get("artifact_sha256") != artifact_sha:
                technical_issues.append(
                    {
                        "code": f"route_only_{checker}_artifact_stale",
                        "message": f"route-only {checker} checker artifact is stale",
                    }
                )
                continue
        if checker in {"structure", "citation"} and component_status != "passed":
            technical_issues.append(
                {
                    "code": f"route_only_{checker}_incomplete",
                    "message": f"route-only {checker} checker is incomplete",
                }
            )

    map_receipt = receipts.get("map")
    if map_receipt and not any(
        str(issue.get("code") or "").startswith("route_only_map_")
        for issue in technical_issues
    ):
        map_state = _route_map_resume_state(project_id, route_plan)
        technical_issues.extend(map_state["validation_issues"])
        technical_issues.extend(map_state["technical_issues"])
        business_issues.extend(map_state["business_issues"])
        map_status = str((map_state.get("report") or {}).get("status") or "")
        receipt_components = (
            (map_receipt.get("route_gate") or {}).get("component_statuses") or {}
        )
        receipt_status = str(receipt_components.get("route_map") or "")
        if (
            map_status not in {"passed", "needs_confirmation"}
            or receipt_status != map_status
        ):
            technical_issues.append(
                {
                    "code": "route_only_map_receipt_status_mismatch",
                    "message": "route-only map checker receipt does not match the current report",
                }
            )

    feasibility_receipt = receipts.get("feasibility")
    if feasibility_receipt and not any(
        str(issue.get("code") or "").startswith("route_only_feasibility_")
        for issue in technical_issues
    ):
        raw_feasibility = store.read_workspace(project_id, "route_feasibility.json")
        try:
            feasibility = RouteFeasibilityReport.model_validate(raw_feasibility)
        except Exception as exc:
            technical_issues.append(
                {
                    "code": "route_only_feasibility_report_invalid",
                    "message": "route-only feasibility report is invalid",
                    "error_type": type(exc).__name__,
                }
            )
        else:
            if feasibility.route_plan_structure_hash != route_sha:
                technical_issues.append(
                    {
                        "code": "route_only_feasibility_report_stale",
                        "message": "route-only feasibility report is stale",
                    }
                )
            receipt_components = (
                (feasibility_receipt.get("route_gate") or {}).get(
                    "component_statuses"
                )
                or {}
            )
            receipt_status = str(
                receipt_components.get("route_feasibility") or ""
            )
            if receipt_status != feasibility.overall_status:
                technical_issues.append(
                    {
                        "code": "route_only_feasibility_receipt_status_mismatch",
                        "message": "route-only feasibility checker receipt does not match the current report",
                    }
                )
            for check in feasibility.checks:
                issue = {
                    "code": f"route_feasibility_{check.status}",
                    "check_id": check.check_id,
                    "message": check.summary or "路线可执行性检查尚未通过",
                }
                if _has_explicit_technical_error(check):
                    technical_issues.append(issue)
                elif check.status == "blocked" or check.requires_structure_change:
                    blocking_issues.append(issue)
                elif check.status != "passed":
                    business_issues.append(issue)

    return {
        "route_structure_sha256": route_sha,
        "artifact_sha256": artifact_sha,
        "receipts": receipts,
        "technical_issues": technical_issues,
        "blocking_issues": blocking_issues,
        "business_issues": business_issues,
    }


def finalize_route_only(project_id: str) -> dict:
    """Close a route-only experiment without confirming or advancing the route."""
    _require_route_only_target(project_id)
    _topic, requirements = _requirements_context(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json")
    if not isinstance(route_plan, dict):
        raise FileNotFoundError(f"project {project_id!r} 缺少 route_plan.json")
    expected_days = requirements.get("days")
    if not isinstance(expected_days, int):
        expected_days = None
    progress = store.read_workspace(project_id, "progress.json") or {}
    if progress.get("state") != ProjectPhase.SKELETON_CONFIRM.value:
        raise ValueError("route-only封口要求Project停在骨架确认中")
    if progress.get("skeleton_confirmed") is not False:
        raise ValueError("route-only封口要求skeleton_confirmed=false")

    unexpected = _route_only_downstream_artifacts(project_id)
    if unexpected:
        raise ValueError(f"route-only项目出现越界下游产物: {unexpected}")
    downstream_trace = _route_only_downstream_trace_events(project_id)
    if downstream_trace:
        raise ValueError(f"route-only项目出现越界下游执行记录: {downstream_trace}")

    validation_state = _route_only_validation_state(
        project_id,
        route_plan,
        expected_days=expected_days,
    )
    if validation_state["blocking_issues"]:
        codes = ", ".join(
            str(item.get("code") or "unknown")
            for item in validation_state["blocking_issues"]
        )
        raise ValueError(f"route-only route has action-required findings: {codes}")
    if validation_state["technical_issues"]:
        codes = ", ".join(
            str(item.get("code") or "unknown")
            for item in validation_state["technical_issues"]
        )
        raise ValueError(f"route-only checker receipts are incomplete: {codes}")

    map_report = store.read_workspace(project_id, "route_map_validation.json") or {}
    feasibility = store.read_workspace(project_id, "route_feasibility.json") or {}
    has_findings = bool(validation_state["business_issues"])
    completion_mode = (
        "route_draft_completed_with_findings" if has_findings else "route_draft_completed"
    )
    manifest = store.read_workspace(project_id, "experiment.json") or {}
    manifest["completion_mode"] = completion_mode
    manifest["route_only_stop_state"] = ProjectPhase.SKELETON_CONFIRM.value
    manifest["skeleton_confirmed"] = False
    store.write_experiment_manifest(project_id, manifest)
    return {
        "mode": "full_plan_route_only",
        "project_id": project_id,
        "status": completion_mode,
        "route_structure_sha256": validation_state["route_structure_sha256"],
        "skeleton_confirmed": False,
        "downstream_started": False,
        "map_status": map_report.get("status") or "missing",
        "feasibility_status": feasibility.get("overall_status") or "missing",
    }


def _find_tool(project_id: str, stage: str, *, day: int | None, poi_name: str | None):
    actor, tool_name = _STAGE_WRITERS[stage]
    topic, _requirements = _requirements_context(project_id)
    if stage == "poi_detail" and (day is None or not poi_name):
        raise ValueError("poi_detail写入必须提供day和poi_name")
    tools = make_workspace_tools(
        project_id,
        actor,
        day=day,
        poi_name=poi_name,
        topic=topic,
    )
    return next((tool for tool in tools if tool.name == tool_name), None)


def write_stage_payload(
    project_id: str,
    stage: Literal["lifestyle", "pitfall", "integration", "poi_detail"],
    payload: dict,
    *,
    day: int | None = None,
    poi_name: str | None = None,
) -> dict:
    """Invoke the existing actor-bound Workspace tool for one business stage."""
    if stage not in _STAGE_WRITERS:
        raise ValueError(f"unsupported writable stage: {stage}")
    if not isinstance(payload, dict):
        raise ValueError("payload必须是JSON对象")
    tool = _find_tool(project_id, stage, day=day, poi_name=poi_name)
    if tool is None:
        raise RuntimeError(f"stage {stage!r} 没有找到对应Workspace写入工具")
    result = tool.invoke(payload)
    if isinstance(result, str) and result.startswith("写入失败"):
        raise ValueError(result)
    return {
        "project_id": project_id,
        "stage": stage,
        "result": result,
    }


def write_route_facts_payload(
    project_id: str,
    payload: dict,
    *,
    day: int,
    expected_route_structure_sha256: str,
) -> dict:
    """Apply a facts-only route repair while enforcing the confirmed structure hash."""
    if not isinstance(payload, dict):
        raise ValueError("payload必须是JSON对象")
    unsupported = sorted(set(payload) - {"notes", "sources", "claims"})
    if unsupported:
        raise ValueError(f"route facts-only写入不支持字段: {unsupported}")
    before = store.read_workspace(project_id, "route_plan.json") or {}
    before_sha = route_structure_sha256(before)
    if before_sha != expected_route_structure_sha256:
        raise ValueError(
            "写入前路线结构SHA不匹配: "
            f"expected={expected_route_structure_sha256}, actual={before_sha}"
        )
    topic, _requirements = _requirements_context(project_id)
    tools = make_workspace_tools(
        project_id,
        "route_agent",
        day=day,
        topic=topic,
        route_write_mode="facts_only",
    )
    tool = next((item for item in tools if item.name == "write_route_facts_patch"), None)
    if tool is None:
        raise RuntimeError("route facts-only Workspace写入工具不可用")
    result = tool.invoke({"day": day, **payload})
    if isinstance(result, str) and result.startswith("写入失败"):
        raise ValueError(result)
    after = store.read_workspace(project_id, "route_plan.json") or {}
    after_sha = route_structure_sha256(after)
    if after_sha != before_sha:
        raise RuntimeError(
            "route facts-only写入改变了已确认路线结构: "
            f"before={before_sha}, after={after_sha}"
        )
    return {
        "project_id": project_id,
        "stage": "route",
        "day": day,
        "result": result,
        "route_structure_sha256": after_sha,
    }


def _validation_artifact(project_id: str, stage: Stage, day: int | None, poi_name: str | None) -> Any:
    if stage == "route":
        return {
            "route_plan": store.read_workspace(project_id, "route_plan.json") or {},
            "route_entities": store.read_workspace(project_id, "route_entities.json") or {},
            "route_execution": store.read_workspace(project_id, "route_execution.json") or {},
        }
    if stage in {"lifestyle", "pitfall"}:
        return store.read_workspace(project_id, f"outputs/{stage}.json") or {}
    if stage == "poi_detail":
        detail = store.read_poi_detail(project_id, day, poi_name or "")
        return detail.model_dump(mode="json") if detail is not None else {}
    return {
        "guide": store.read_workspace(project_id, "final_guide.md") or "",
        "meta": store.read_workspace(project_id, "final_guide.meta.json") or {},
        "blocks": store.read_workspace(project_id, "final_guide.blocks.json") or {},
    }


def _validation_scope(stage: Stage, day: int | None, poi_name: str | None) -> str:
    if stage == "poi_detail":
        return f"day:{day}:poi:{poi_name or ''}"
    return "stage"


def reconcile_zero_claim_validation_from_trace(project_id: str, stage: str) -> dict:
    """Backfill a stage ledger entry from a preserved passed trace event.

    This migration is intentionally limited to a current zero-Claim artifact and
    does not rerun a model or rewrite business content.
    """
    if stage not in {"pitfall"}:
        raise ValueError("当前只允许迁移Pitfall零Claim校验结果")
    artifact = _validation_artifact(project_id, stage, None, None)
    if _claim_ids(artifact):
        raise ValueError(f"{stage}当前不是零Claim产物，不能使用该迁移")
    trace_path = store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"
    trace = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if trace_path.is_file() else []
    passed = [
        item
        for item in (trace or [])
        if isinstance(item, dict)
        and item.get("event") == "validation_result"
        and item.get("stage") == stage
        and item.get("status") == "passed"
    ]
    if not passed:
        raise ValueError(f"Trace中没有{stage} passed校验事件")
    event = max(passed, key=lambda item: str(item.get("timestamp") or ""))
    updated_at = str(artifact.get("updated_at") or "") if isinstance(artifact, dict) else ""
    observed_at = str(event.get("timestamp") or "")
    if updated_at and observed_at and updated_at > observed_at:
        raise ValueError(f"{stage}产物在Trace校验后已更新，不能迁移旧结果")
    entry = store.append_validation_run(
        project_id,
        {
            "stage": stage,
            "scope": "stage",
            "observation_mode": "live",
            "status": "passed",
            "artifact_sha256": _canonical_sha256(artifact),
            "route_structure_sha256": route_structure_sha256(
                store.read_workspace(project_id, "route_plan.json") or {}
            ),
            "issue_codes": [],
            "source": "codex_trace_reconciliation",
            "original_checked_at": observed_at,
        },
    )
    return {"project_id": project_id, "stage": stage, "validation_run": entry}


def _coverage_issues(project_id: str, stage: str) -> list[dict]:
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    route_days = {
        day.get("day")
        for day in route_plan.get("days") or []
        if isinstance(day, dict) and isinstance(day.get("day"), int)
    }
    if stage == "lifestyle":
        output = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
        expected = {(field, day) for field in LIFESTYLE_FIELDS for day in route_days}
        actual = {
            (field, item.get("day"))
            for field in LIFESTYLE_FIELDS
            for item in (output.get(field) or {}).get("items", []) or []
        }
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        return [
            {
                "code": "lifestyle_coverage_invalid",
                "message": "Lifestyle首次生成没有完整覆盖路线全部天数的四个字段",
                "missing": missing,
                "extra": extra,
            }
        ] if missing or extra else []

    if stage == "pitfall":
        lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
        pitfall = store.read_workspace(project_id, "outputs/pitfall.json") or {}
        expected = {
            (field, item.get("day")): item.get("recommendation", "")
            for field in LIFESTYLE_FIELDS
            for item in (lifestyle.get(field) or {}).get("items", []) or []
        }
        actual = {
            (field, item.get("day")): item.get("target_recommendation", "")
            for field in LIFESTYLE_FIELDS
            for item in (pitfall.get(field) or {}).get("items", []) or []
        }
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        mismatched = sorted(
            key for key in set(expected) & set(actual) if expected[key] != actual[key]
        )
        return [
            {
                "code": "pitfall_binding_invalid",
                "message": "Pitfall没有完整覆盖或正确绑定Lifestyle的(field, day)",
                "missing": missing,
                "extra": extra,
                "mismatched": mismatched,
            }
        ] if missing or extra or mismatched else []
    return []


def _validate_final_delivery(project_id: str, *, persist: bool = True) -> list[dict]:
    saved = store.read_requirements(project_id) or {}
    route_plan = store.read_workspace(project_id, "route_plan.json")
    feasibility = store.read_workspace(project_id, "route_feasibility.json")
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    final_guide = store.read_workspace(project_id, "final_guide.md")
    final_meta = store.read_workspace(project_id, "final_guide.meta.json")
    final_blocks = store.read_workspace(project_id, "final_guide.blocks.json")
    poi_details = [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]
    issues = validate_final_guide_contract(
        saved.get("requirements") or {},
        route_plan if isinstance(route_plan, dict) else None,
        feasibility if isinstance(feasibility, dict) else None,
        lifestyle if isinstance(lifestyle, dict) else None,
        pitfall if isinstance(pitfall, dict) else None,
        final_guide if isinstance(final_guide, str) else None,
        final_meta if isinstance(final_meta, dict) else None,
        final_blocks if isinstance(final_blocks, dict) else None,
        poi_details,
        store.read_evidence_checks(project_id),
        store.read_workspace(project_id, "review_findings.jsonl") or [],
    )
    if persist and isinstance(final_meta, dict):
        store.write_final_guide_contract_validation(
            project_id,
            status="retrying" if issues else "passed",
            issues=issues,
            source="codex_skill",
        )
    return issues


def validate_stage(
    project_id: str,
    stage: Stage,
    *,
    live: bool = False,
    day: int | None = None,
    poi_name: str | None = None,
    route_check_ids: Optional[list[str]] = None,
    route_checks: Optional[list[RouteCheck]] = None,
    expected_route_structure_sha256: str | None = None,
    persist: bool = True,
) -> dict:
    """Run existing validators plus narrow deterministic cross-stage checks."""
    topic, requirements = _requirements_context(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    issues: list[dict] = []
    checks: list[dict] = []

    saved_requirements = store.read_requirements(project_id) or {}
    destination = str((saved_requirements.get("requirements") or {}).get("destination") or "")
    baseline_check = validate_material_baseline(
        store.read_workspace(project_id, "material_baseline.json"),
        topic=topic,
        destination=destination,
    )
    manifest = store.read_workspace(project_id, "experiment.json") or {}
    strict_comparison = str(manifest.get("comparison_grade") or "").upper() == "A"
    if strict_comparison:
        issues.extend(baseline_check["issues"])
    checks.append(
        {
            "name": "material_baseline",
            "status": baseline_check["status"],
            "strict": strict_comparison,
            "issues": baseline_check["issues"],
        }
    )

    if route_checks and stage != "route":
        raise ValueError("route_checks只适用于route阶段")
    requested_route_checks = list(dict.fromkeys(route_checks or []))
    if stage == "route" and not requested_route_checks:
        requested_route_checks = (
            ["feasibility"]
            if route_check_ids is not None
            else ["structure", "citation", "map", "feasibility"]
        )
    if route_check_ids is not None and requested_route_checks != ["feasibility"]:
        raise ValueError("--check-id只能与单独的route feasibility检查一起使用")

    def provider_snapshot() -> dict[str, int]:
        counts: dict[str, int] = {}
        for path in store.project_dir(project_id).glob("agents/**/provider_calls.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                key = ":".join(
                    str(item.get(field) or "unknown")
                    for field in ("provider", "operation", "status")
                )
                counts[key] = counts.get(key, 0) + 1
        return counts

    def timed_check(name: str, payload: dict) -> dict:
        completed_at = _now()
        started_at = payload.pop("_started_at")
        started_monotonic = payload.pop("_started_monotonic")
        provider_before = payload.pop("_provider_before")
        provider_after = provider_snapshot()
        provider_calls = {
            key: provider_after.get(key, 0) - provider_before.get(key, 0)
            for key in set(provider_before).union(provider_after)
            if provider_after.get(key, 0) - provider_before.get(key, 0) > 0
        }
        return {
            "name": name,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": round((time.monotonic() - started_monotonic) * 1000, 2),
            "provider_call_count": sum(provider_calls.values()),
            "provider_calls": provider_calls,
            **payload,
        }

    def check_started() -> dict:
        return {
            "_started_at": _now(),
            "_started_monotonic": time.monotonic(),
            "_provider_before": provider_snapshot(),
        }

    current_route_sha = route_structure_sha256(route_plan)
    if expected_route_structure_sha256 and current_route_sha != expected_route_structure_sha256:
        issues.append(
            {
                "code": "confirmed_route_structure_changed",
                "message": "已确认路线结构在下游阶段被修改",
                "expected": expected_route_structure_sha256,
                "actual": current_route_sha,
            }
        )

    if stage == "route":
        if "structure" in requested_route_checks:
            timing = check_started()
            expected_days = requirements.get("days")
            if not isinstance(expected_days, int):
                expected_days = None
            structure_issues = validate_route_structure(route_plan, expected_days=expected_days)
            issues.extend(structure_issues)
            checks.append(
                timed_check(
                    "route_structure",
                    {
                        **timing,
                        "status": "blocked" if structure_issues else "passed",
                        "issues": structure_issues,
                    },
                )
            )

        if "citation" in requested_route_checks:
            timing = check_started()
            try:
                citation_issues = citation_check.verify_route_citations(
                    project_id,
                    topic,
                    allow_model_calls=live,
                    observation_mode="live" if live else "offline",
                    **({"persist": False} if not persist else {}),
                )
            except Exception as exc:
                citation_issues = [
                    {
                        "code": "route_citation_technical_error",
                        "message": "路线引用校验因技术错误未完成",
                        **_exception_diagnostics(exc),
                        **_latest_evidence_error_context(project_id, "route"),
                    }
                ]
                citation_status = "unknown"
            else:
                citation_status = (
                    "needs_repair"
                    if citation_issues
                    else ("passed" if live else "partial")
                )
            issues.extend({"code": "route_citation", **item} for item in citation_issues)
            checks.append(
                timed_check(
                    "route_citation",
                    {
                        **timing,
                        "status": citation_status,
                        "issues": citation_issues,
                    },
                )
            )

        if "map" in requested_route_checks:
            if live:
                timing = check_started()
                map_report = route_map_check.verify_route_map(
                    project_id,
                    topic,
                    route_plan,
                    persist=persist,
                )
                map_issues = list(map_report.get("issues") or [])
                issues.extend(map_issues)
                checks.append(
                    timed_check(
                        "route_map",
                        {
                            **timing,
                            "status": map_report.get("status"),
                            "issues": map_issues,
                        },
                    )
                )
            else:
                checks.append({"name": "route_map", "status": "not_run_offline"})

        if "feasibility" in requested_route_checks:
            if live:
                timing = check_started()
                report = route_feasibility_check.verify_route_feasibility(
                    project_id,
                    topic,
                    requirements=requirements,
                    check_ids=route_check_ids,
                    **({"persist": False} if not persist else {}),
                )
                unresolved = [
                    item.model_dump(mode="json")
                    for item in report.checks
                    if item.status != "passed"
                ]
                issues.extend(
                    {"code": f"route_feasibility_{item['status']}", **item}
                    for item in unresolved
                )
                checks.append(
                    timed_check(
                        "route_feasibility",
                        {
                            **timing,
                            "status": report.overall_status,
                            "overall_status": report.overall_status,
                            "requested_check_ids": route_check_ids,
                            "unresolved_check_ids": [item["check_id"] for item in unresolved],
                            "issues": unresolved,
                        },
                    )
                )
            else:
                checks.append({"name": "route_feasibility", "status": "not_run_offline"})

    elif stage == "lifestyle":
        coverage = _coverage_issues(project_id, stage)
        if strict_comparison:
            coverage.extend(research_coverage_issues(project_id, stage))
        citation_issues = citation_check.verify_lifestyle_citations(
            project_id,
            topic,
            allow_model_calls=live,
            observation_mode="live" if live else "offline",
            **({"persist": False} if not persist else {}),
        )
        issues.extend(coverage)
        issues.extend({"code": "lifestyle_citation", **item} for item in citation_issues)
        checks.extend(
            [
                {"name": "lifestyle_coverage", "issues": coverage},
                {"name": "lifestyle_citation", "issues": citation_issues},
            ]
        )

    elif stage == "pitfall":
        coverage = _coverage_issues(project_id, stage)
        if strict_comparison:
            coverage.extend(research_coverage_issues(project_id, stage))
        citation_issues = citation_check.verify_pitfall_citations(
            project_id,
            topic,
            allow_model_calls=live,
            observation_mode="live" if live else "offline",
            **({"persist": False} if not persist else {}),
        )
        issues.extend(coverage)
        issues.extend({"code": "pitfall_citation", **item} for item in citation_issues)
        checks.extend(
            [
                {"name": "pitfall_binding", "issues": coverage},
                {"name": "pitfall_citation", "issues": citation_issues},
            ]
        )

    elif stage == "poi_detail":
        if day is None or not poi_name:
            raise ValueError("poi_detail校验必须提供day和poi_name")
        detail = store.read_poi_detail(project_id, day, poi_name)
        if detail is None:
            issues.append(
                {
                    "code": "poi_detail_missing",
                    "message": f"Day{day} {poi_name} 没有生成POI详情文件",
                }
            )
            citation_issues = []
        else:
            if strict_comparison:
                research_issues = research_coverage_issues(
                    project_id, stage, day=day, poi_name=poi_name
                )
                issues.extend(research_issues)
                checks.append(
                    {"name": "poi_detail_research", "issues": research_issues}
                )
            if not any(route.route_segments for route in detail.routes):
                empty_issues = [
                    {
                        "code": "poi_detail_content_empty",
                        "message": (
                            f"Day{day} {poi_name}标记needs_detail但没有形成任何内部路线"
                        ),
                    }
                ]
                issues.extend(empty_issues)
                checks.append(
                    {"name": "poi_detail_content", "issues": empty_issues}
                )
            citation_issues = citation_check.verify_poi_detail_citations(
                detail.model_dump(mode="json"),
                topic,
                project_id=project_id,
                log_scope=f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}",
                allow_model_calls=live,
                observation_mode="live" if live else "offline",
                **({"persist": False} if not persist else {}),
            )
            issues.extend({"code": "poi_detail_citation", **item} for item in citation_issues)
        checks.append({"name": "poi_detail_citation", "issues": citation_issues})

    elif stage in {"integration", "final"}:
        final_issues = _validate_final_delivery(project_id, persist=persist)
        if stage == "integration":
            # Integration runs before POI fan-out. Empty POI content is owned by the
            # POI branch and the final join, not by the integration role.
            final_issues = [
                issue
                for issue in final_issues
                if issue.get("code") != "poi_detail_content_empty"
            ]
        issues.extend(final_issues)
        checks.append({"name": "final_guide_contract", "issues": final_issues})
    else:
        raise ValueError(f"unsupported stage: {stage}")

    blocked = any(
        issue.get("code")
        in {
            "confirmed_route_structure_changed",
            "material_topic_mismatch",
            "material_baseline_missing",
            "material_baseline_invalid",
            "material_baseline_namespace_changed",
            "material_index_empty",
            "material_index_not_synced",
            "material_corpus_changed",
            "prompt_fingerprint_changed",
            "model_boundary_changed",
            *_ROUTE_STRUCTURE_BLOCKING_CODES,
        }
        or str(issue.get("code", "")).endswith("_blocked")
        for issue in issues
    )
    external_stage = stage in {"route", "lifestyle", "pitfall", "poi_detail"}
    if blocked:
        status = "blocked"
    elif issues:
        status = "needs_repair"
    elif external_stage and not live:
        status = "partial"
    else:
        status = "passed"
    route_gate = None
    if stage == "route":
        component_statuses = {
            str(item.get("name") or ""): str(item.get("status") or "unknown")
            for item in checks
            if str(item.get("name") or "").startswith("route_")
        }
        observed_statuses = set(component_statuses.values())
        if "blocked" in observed_statuses or blocked:
            gate_status = "blocked"
        elif "unknown" in observed_statuses:
            gate_status = "unknown"
        elif observed_statuses.intersection({"warning", "needs_confirmation", "needs_repair"}):
            gate_status = "warning"
        elif observed_statuses.intersection({"partial", "not_run_offline"}):
            gate_status = "partial"
        else:
            gate_status = "passed"
        route_gate = {
            "status": gate_status,
            "requested_checks": requested_route_checks,
            "component_statuses": component_statuses,
            "unresolved_check_ids": [
                check_id
                for item in checks
                for check_id in (item.get("unresolved_check_ids") or [])
            ],
        }
    result = {
        "project_id": project_id,
        "stage": stage,
        "validation_mode": "live" if live else "offline",
        "status": status,
        "route_structure_sha256": current_route_sha,
        "issues": issues,
        "checks": checks,
        **({"route_gate": route_gate} if route_gate is not None else {}),
    }
    if persist:
        artifact = _validation_artifact(project_id, stage, day, poi_name)
        validation_scope = _validation_scope(stage, day, poi_name)
        if stage == "route" and len(requested_route_checks) == 1:
            validation_scope = f"checker:{requested_route_checks[0]}"
        store.append_validation_run(
            project_id,
            {
                "stage": stage,
                "scope": validation_scope,
                "observation_mode": "live" if live else "offline",
                "status": status,
                "artifact_sha256": _canonical_sha256(artifact),
                "route_structure_sha256": current_route_sha,
                "issue_codes": [str(item.get("code") or "") for item in issues],
                "issues": issues,
                "route_checks": requested_route_checks if stage == "route" else [],
                "route_gate": route_gate,
                "check_timings": [
                    {
                        key: item.get(key)
                        for key in (
                            "name",
                            "status",
                            "started_at",
                            "completed_at",
                            "duration_ms",
                            "provider_call_count",
                            "provider_calls",
                            "unresolved_check_ids",
                        )
                        if key in item
                    }
                    for item in checks
                ],
                "source": "codex_skill",
            },
        )
    return result


def _workspace_path(relative_path: str) -> str:
    return f"workspace/{relative_path}"


def _stage_record(stage: str, status: str, evidence: list[str], issues: list[dict]) -> dict:
    return {
        "stage": stage,
        "status": status,
        "evidence": evidence,
        "issues": issues,
    }


def _claim_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        claim_id = str(value.get("claim_id") or "").strip()
        if claim_id:
            found.add(claim_id)
        for child in value.values():
            found.update(_claim_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_claim_ids(child))
    return found


def _has_current_live_validation(
    project_id: str,
    section: str,
    artifact: Any,
    *,
    scope: str = "stage",
) -> bool:
    artifact_sha = _canonical_sha256(artifact)
    stage_runs = [
        item
        for item in store.read_validation_runs(project_id)
        if item.get("stage") == section
        and item.get("scope") == scope
        and item.get("observation_mode") == "live"
        and item.get("artifact_sha256") == artifact_sha
    ]
    if stage_runs:
        latest = max(stage_runs, key=lambda item: str(item.get("checked_at") or ""))
        return latest.get("status") == "passed"

    records = [
        item
        for item in (store.read_evidence_checks(project_id).get(section) or [])
        if isinstance(item, dict) and item.get("observation_mode") == "live"
    ]
    if not records:
        return False

    current_claim_ids = _claim_ids(artifact)
    if current_claim_ids:
        by_claim: dict[str, list[dict]] = {}
        for item in records:
            claim_id = str(item.get("claim_id") or "")
            if claim_id in current_claim_ids:
                by_claim.setdefault(claim_id, []).append(item)
        if set(by_claim) != current_claim_ids:
            return False
        groups = by_claim.values()
    else:
        by_target: dict[str, list[dict]] = {}
        for item in records:
            target_key = str(item.get("target_key") or "")
            if target_key:
                by_target.setdefault(target_key, []).append(item)
        if not by_target:
            return False
        groups = by_target.values()

    rejected = {"fail", "error", "inconclusive"}
    for group in groups:
        latest_time = max(str(item.get("checked_at") or "") for item in group)
        latest = [item for item in group if str(item.get("checked_at") or "") == latest_time]
        if any(str(item.get("result") or "") in rejected for item in latest):
            return False
    return True


def _citation_statuses(value: Any) -> set[str]:
    statuses: set[str] = set()
    if isinstance(value, dict):
        status = str(value.get("citation_status") or "").strip()
        if status:
            statuses.add(status)
        for child in value.values():
            statuses.update(_citation_statuses(child))
    elif isinstance(value, list):
        for child in value:
            statuses.update(_citation_statuses(child))
    return statuses


def _route_map_resume_state(project_id: str, route_plan: dict) -> dict[str, Any]:
    current_route_sha = route_structure_sha256(route_plan)
    report = store.read_workspace(project_id, "route_map_validation.json")
    validation_issues: list[dict[str, Any]] = []
    technical_issues: list[dict[str, Any]] = []
    business_issues: list[dict[str, Any]] = []
    if not isinstance(report, dict):
        validation_issues.append(
            {
                "code": "route_map_missing",
                "message": "当前路线缺少Route Map校验报告",
            }
        )
        return {
            "report": None,
            "validation_issues": validation_issues,
            "technical_issues": technical_issues,
            "business_issues": business_issues,
        }
    if report.get("route_plan_structure_hash") != current_route_sha:
        validation_issues.append(
            {
                "code": "route_map_stale",
                "message": "Route Map校验报告对应的路线结构Hash已不是当前路线",
            }
        )
        return {
            "report": report,
            "validation_issues": validation_issues,
            "technical_issues": technical_issues,
            "business_issues": business_issues,
        }

    if report.get("schema_version") != _ROUTE_MAP_SCHEMA_VERSION:
        validation_issues.append(
            {
                "code": "route_map_schema_invalid",
                "message": "Route Map报告Schema不符合当前生成契约",
            }
        )
    if report.get("project_id") != project_id:
        validation_issues.append(
            {
                "code": "route_map_project_mismatch",
                "message": "Route Map报告未绑定当前Project",
            }
        )
    if not str(report.get("checked_at") or "").strip():
        validation_issues.append(
            {
                "code": "route_map_checked_at_missing",
                "message": "Route Map报告缺少Live检查时间",
            }
        )
    if report.get("provider") != _ROUTE_MAP_PROVIDER:
        technical_issues.append(
            {
                "code": "route_map_provider_invalid",
                "message": "Route Map报告未记录当前受支持的高德Provider",
            }
        )

    report_status = str(report.get("status") or "unknown")
    if report_status not in {"passed", "needs_confirmation"}:
        technical_issues.append(
            {
                "code": f"route_map_report_{report_status}",
                "message": "Route Map报告本身没有可采用的技术终态",
            }
        )

    expected_days = {
        int(day.get("day") or 0): day
        for day in route_plan.get("days") or []
        if isinstance(day, dict)
    }
    report_days: dict[int, dict[str, Any]] = {}
    for map_day in report.get("days") or []:
        if not isinstance(map_day, dict):
            validation_issues.append(
                {
                    "code": "route_map_day_invalid",
                    "message": "Route Map day格式无效",
                }
            )
            continue
        try:
            day_no = int(map_day.get("day") or 0)
        except (TypeError, ValueError):
            day_no = 0
        if day_no <= 0 or day_no in report_days:
            validation_issues.append(
                {
                    "code": "route_map_day_invalid",
                    "day": map_day.get("day"),
                    "message": "Route Map day编号缺失、无效或重复",
                }
            )
            continue
        report_days[day_no] = map_day
    if set(report_days) != set(expected_days):
        validation_issues.append(
            {
                "code": "route_map_day_coverage_incomplete",
                "message": "Route Map没有完整覆盖当前路线全部天数",
                "missing_days": sorted(set(expected_days) - set(report_days)),
                "extra_days": sorted(set(report_days) - set(expected_days)),
            }
        )

    for day_no, route_day in expected_days.items():
        map_day = report_days.get(day_no)
        if not isinstance(map_day, dict):
            continue
        pois = [poi for poi in route_day.get("pois") or [] if isinstance(poi, dict)]
        entities = [
            item for item in map_day.get("entities") or [] if isinstance(item, dict)
        ]
        if len(entities) != len(pois):
            validation_issues.append(
                {
                    "code": "route_map_entity_coverage_incomplete",
                    "day": day_no,
                    "message": "Route Map实体数量与当前路线POI数量不一致",
                }
            )
        for entity_index, entity in enumerate(entities):
            if entity_index < len(pois) and str(
                entity.get("original_name") or ""
            ) != str(pois[entity_index].get("name") or ""):
                validation_issues.append(
                    {
                        "code": "route_map_entity_binding_mismatch",
                        "day": day_no,
                        "poi_index": entity_index,
                        "message": "Route Map实体顺序或名称未绑定当前路线POI",
                    }
                )
            status = str(entity.get("status") or "unknown")
            if status != "resolved":
                technical_issues.append(
                    {
                        "code": f"route_map_entity_{status}",
                        "day": day_no,
                        "poi_name": entity.get("original_name"),
                        "message": "Route Map存在未唯一解析的路线实体",
                    }
                )

        legs = [item for item in map_day.get("legs") or [] if isinstance(item, dict)]
        expected_leg_count = max(0, len(pois) - 1)
        if len(legs) != expected_leg_count:
            validation_issues.append(
                {
                    "code": "route_map_leg_coverage_incomplete",
                    "day": day_no,
                    "message": "Route Map相邻路段数量与当前路线不一致",
                    "expected": expected_leg_count,
                    "actual": len(legs),
                }
            )
        for leg_index, leg in enumerate(legs):
            if leg_index < expected_leg_count:
                expected_from = str(pois[leg_index].get("name") or "")
                expected_to = str(pois[leg_index + 1].get("name") or "")
                if (
                    str(leg.get("from") or "") != expected_from
                    or str(leg.get("to") or "") != expected_to
                ):
                    validation_issues.append(
                        {
                            "code": "route_map_leg_binding_mismatch",
                            "day": day_no,
                            "leg_index": leg_index,
                            "message": "Route Map相邻路段端点未绑定当前路线顺序",
                        }
                    )
            status = str(leg.get("status") or "unknown")
            transport_mode = str(leg.get("transport_mode") or "unknown")
            if transport_mode not in _ROUTE_MAP_KNOWN_MODES:
                technical_issues.append(
                    {
                        "code": "route_map_leg_transport_mode_unknown",
                        "day": day_no,
                        "leg_index": leg_index,
                        "message": "Route Map路段缺少当前生成契约支持的交通方式",
                    }
                )
                allowed_leg_statuses: set[str] | None = None
            elif transport_mode in _ROUTE_MAP_DRIVING_MODES:
                allowed_leg_statuses = {"passed", "anomaly"}
            else:
                allowed_leg_statuses = {"mode_specific_not_driving", "anomaly"}
            if status not in _ROUTE_MAP_LEG_STATUSES:
                technical_issues.append(
                    {
                        "code": f"route_map_leg_{status}",
                        "day": day_no,
                        "leg_index": leg_index,
                        "message": "Route Map路段缺少可采用的技术核验结果",
                    }
                )
            elif allowed_leg_statuses is not None and status not in allowed_leg_statuses:
                technical_issues.append(
                    {
                        "code": "route_map_leg_status_mode_mismatch",
                        "day": day_no,
                        "leg_index": leg_index,
                        "transport_mode": transport_mode,
                        "status": status,
                        "message": "Route Map路段状态与交通方式不符合当前生成契约",
                    }
                )
            elif (
                transport_mode in _ROUTE_MAP_DRIVING_MODES
                and status in {"passed", "anomaly"}
                and allowed_leg_statuses is not None
                and not (
                    _positive_finite_number(leg.get("distance_km"))
                    and _positive_finite_number(leg.get("duration_h"))
                )
            ):
                technical_issues.append(
                    {
                        "code": "route_map_leg_metrics_invalid",
                        "day": day_no,
                        "leg_index": leg_index,
                        "message": "Route Map驾车或出租车路段缺少有限正数距离/时长",
                    }
                )
            if (
                status == "anomaly"
                and allowed_leg_statuses is not None
                and status in allowed_leg_statuses
            ):
                business_issues.append(
                    {
                        "code": "route_map_leg_anomaly",
                        "day": day_no,
                        "leg_index": leg_index,
                        "message": f"{leg.get('from') or ''}到{leg.get('to') or ''}存在地图异常",
                    }
                )

            segments = [
                item for item in leg.get("segments") or [] if isinstance(item, dict)
            ]
            if transport_mode == "mixed" and not segments:
                technical_issues.append(
                    {
                        "code": "route_map_mixed_leg_missing_segments",
                        "day": day_no,
                        "leg_index": leg_index,
                        "message": "mixed路段缺少分段技术结果",
                    }
                )
            for segment_index, segment in enumerate(
                segments if transport_mode == "mixed" else []
            ):
                mode = str(segment.get("mode") or "unknown")
                segment_status = str(segment.get("status") or "unknown")
                if mode not in _ROUTE_MAP_KNOWN_MODES:
                    technical_issues.append(
                        {
                            "code": "route_map_segment_mode_unknown",
                            "day": day_no,
                            "leg_index": leg_index,
                            "segment_index": segment_index,
                            "message": "Route Map子段缺少当前生成契约支持的明确交通方式",
                        }
                    )
                    allowed_statuses: set[str] | None = None
                elif mode in _ROUTE_MAP_DRIVING_MODES:
                    allowed_statuses = {"passed", "anomaly"}
                else:
                    allowed_statuses = {"declared_mode_specific"}
                if segment_status not in _ROUTE_MAP_SEGMENT_STATUSES:
                    technical_issues.append(
                        {
                            "code": f"route_map_segment_{segment_status}",
                            "day": day_no,
                            "leg_index": leg_index,
                            "segment_index": segment_index,
                            "message": "Route Map子段缺少可采用的技术核验结果",
                        }
                    )
                elif allowed_statuses is not None and segment_status not in allowed_statuses:
                    technical_issues.append(
                        {
                            "code": "route_map_segment_status_mode_mismatch",
                            "day": day_no,
                            "leg_index": leg_index,
                            "segment_index": segment_index,
                            "transport_mode": mode,
                            "status": segment_status,
                            "message": "Route Map子段状态与交通方式不符合当前生成契约",
                        }
                    )
                elif (
                    mode in _ROUTE_MAP_DRIVING_MODES
                    and segment_status in {"passed", "anomaly"}
                    and allowed_statuses is not None
                    and not (
                        _positive_finite_number(segment.get("distance_km"))
                        and _positive_finite_number(segment.get("duration_h"))
                    )
                ):
                    technical_issues.append(
                        {
                            "code": "route_map_segment_metrics_invalid",
                            "day": day_no,
                            "leg_index": leg_index,
                            "segment_index": segment_index,
                            "message": "Route Map驾车或出租车子段缺少有限正数距离/时长",
                        }
                    )
                if (
                    segment_status == "anomaly"
                    and allowed_statuses is not None
                    and segment_status in allowed_statuses
                ):
                    business_issues.append(
                        {
                            "code": "route_map_segment_anomaly",
                            "day": day_no,
                            "leg_index": leg_index,
                            "segment_index": segment_index,
                            "message": "Route Map子段存在地图异常",
                        }
                    )

        schedule_check = map_day.get("schedule_check")
        schedule_status = str(
            schedule_check.get("status") or ""
            if isinstance(schedule_check, dict)
            else ""
        )
        if schedule_status == "warning":
            business_issues.append(
                {
                    "code": "route_schedule_over_12h",
                    "day": day_no,
                    "message": "Route Map日程预算需要用户确认",
                }
            )
        elif schedule_status != "passed":
            technical_issues.append(
                {
                    "code": f"route_schedule_{schedule_status or 'unknown'}",
                    "day": day_no,
                    "message": "Route Map日程预算尚无可采用的完整结果",
                }
            )

    for item in report.get("issues") or []:
        if not isinstance(item, dict):
            technical_issues.append(
                {"code": "route_map_issue_invalid", "message": "Route Map issue格式无效"}
            )
            continue
        normalized = {
            "code": str(item.get("code") or "route_map_issue_unknown"),
            "day": item.get("day"),
            "message": str(item.get("message") or "Route Map存在未分类问题"),
        }
        if normalized["code"] in _ROUTE_MAP_ACCEPTABLE_ISSUE_CODES:
            business_issues.append(normalized)
        else:
            technical_issues.append(normalized)

    def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        result: list[dict[str, Any]] = []
        for item in items:
            key = (
                item.get("code"),
                item.get("day"),
                item.get("leg_index"),
                item.get("segment_index"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    return {
        "report": report,
        "validation_issues": dedupe(validation_issues),
        "technical_issues": dedupe(technical_issues),
        "business_issues": dedupe(business_issues),
    }


def _route_resume_status(project_id: str, route_plan: dict, validation: dict) -> tuple[str, list[dict]]:
    issues = list(validation.get("issues") or [])
    if validation.get("status") == "blocked":
        return "blocked", issues

    map_state = _route_map_resume_state(project_id, route_plan)
    if map_state["validation_issues"]:
        return "needs_validation", list(map_state["validation_issues"]) + issues
    if map_state["technical_issues"]:
        return "blocked", list(map_state["technical_issues"]) + issues
    map_business_issues = list(map_state["business_issues"])

    if issues:
        if "force_finalized" in _citation_statuses(route_plan):
            return "completed_with_degradation", map_business_issues + issues
        return "needs_repair", map_business_issues + issues

    raw_feasibility = store.read_workspace(project_id, "route_feasibility.json")
    if not isinstance(raw_feasibility, dict):
        return "needs_validation", []
    try:
        feasibility = RouteFeasibilityReport.model_validate(raw_feasibility)
    except Exception as exc:
        return "needs_validation", [
            {
                "code": "route_feasibility_invalid",
                "message": "路线可执行性报告无法通过Schema校验",
                "error_type": type(exc).__name__,
            }
        ]
    if feasibility.route_plan_structure_hash != validation.get("route_structure_sha256"):
        return "needs_validation", [
            {
                "code": "route_feasibility_stale",
                "message": "路线可执行性报告对应的路线结构Hash已不是当前路线",
            }
        ]
    unresolved = [check for check in feasibility.checks if check.status != "passed"]
    resume_issues = [
        {
            "code": f"route_feasibility_{check.status}",
            "check_id": check.check_id,
            "message": check.summary or "路线可执行性检查尚未通过",
        }
        for check in unresolved
    ]
    unresolved_technical = [
        check for check in unresolved if _has_explicit_technical_error(check)
    ]
    if unresolved_technical:
        return "needs_validation", map_business_issues + resume_issues
    action_required = [
        check
        for check in unresolved
        if check.status == "blocked" or check.requires_structure_change
    ]
    if action_required or map_business_issues:
        combined_issues = map_business_issues + resume_issues
        if feasibility.force_finalized:
            return "completed_with_degradation", combined_issues
        return "needs_repair", combined_issues
    if unresolved:
        return "completed_with_degradation", resume_issues
    if _has_current_live_validation(
        project_id,
        "route",
        _validation_artifact(project_id, "route", None, None),
    ):
        return "completed", []
    return "needs_validation", []


def _output_resume_status(
    project_id: str,
    stage: str,
    artifact: dict,
    validation: dict,
) -> tuple[str, list[dict]]:
    issues = list(validation.get("issues") or [])
    coverage_codes = {"lifestyle_coverage_invalid", "pitfall_binding_invalid"}
    if any(issue.get("code") in coverage_codes for issue in issues):
        return "needs_repair", issues
    if issues:
        if "force_finalized" in _citation_statuses(artifact):
            return "completed_with_degradation", issues
        return "needs_repair", issues
    if "force_finalized" in _citation_statuses(artifact):
        return "completed_with_degradation", []
    if _has_current_live_validation(project_id, stage, artifact):
        return "completed", []
    return "needs_validation", []


def _manifest_data(project_id: str) -> dict | None:
    value = store.read_workspace(project_id, "outputs/poi_details/manifest.json")
    return value if isinstance(value, dict) else None


def _poi_target_statuses(project_id: str, route_plan: dict) -> tuple[list[dict], bool]:
    manifest = _manifest_data(project_id)
    entries = {
        (entry.get("day"), str(entry.get("poi_name") or "")): entry
        for entry in ((manifest or {}).get("generated") or [])
        if isinstance(entry, dict)
    }
    poi_dir = store.workspace_dir(project_id) / "outputs" / "poi_details"
    targets: list[dict] = []
    join_required = False
    manifest_updated_at = str((manifest or {}).get("updated_at") or "")

    for target in _needs_detail_targets(route_plan):
        day = target.get("day")
        poi_name = str(target.get("poi_name") or "")
        entry = entries.get((day, poi_name))
        result = {"day": day, "poi_name": poi_name}
        if entry is None:
            result.update({"status": "missing", "citation_status": None})
            targets.append(result)
            continue

        citation_status = str(entry.get("citation_status") or "")
        filename = str(entry.get("file") or "")
        path = poi_dir / filename if filename and Path(filename).name == filename else None
        detail = store.read_poi_detail(project_id, day, poi_name)
        result["citation_status"] = citation_status or None
        if citation_status == "generation_failed":
            result["status"] = "needs_repair" if detail is not None else "missing"
        elif path is None or not path.is_file() or detail is None:
            result["status"] = "missing"
        elif citation_status == "force_finalized":
            if not any(route.route_segments for route in detail.routes):
                result["status"] = "needs_repair"
                result["issues"] = [
                    {
                        "code": "poi_detail_content_empty",
                        "message": f"Day{day} {poi_name}的历史force-finalized文件仍为空",
                    }
                ]
            else:
                result["status"] = "completed_with_degradation"
        else:
            validation = validate_stage(
                project_id,
                "poi_detail",
                day=day,
                poi_name=poi_name,
                persist=False,
            )
            result["issues"] = list(validation.get("issues") or [])
            result["status"] = "needs_repair" if result["issues"] else "completed"

        if detail is not None:
            detail_updated_at = str(detail.updated_at or "")
            if detail_updated_at and manifest_updated_at and detail_updated_at > manifest_updated_at:
                join_required = True
            if citation_status and citation_status != detail.citation_status:
                join_required = True
        targets.append(result)

    final_meta = store.read_workspace(project_id, "final_guide.meta.json")
    final_updated_at = str((final_meta or {}).get("updated_at") or "") if isinstance(final_meta, dict) else ""
    if manifest_updated_at and final_updated_at and manifest_updated_at > final_updated_at:
        join_required = True
    return targets, join_required


def _poi_stage_status(poi_targets: list[dict]) -> str:
    statuses = {str(target.get("status") or "") for target in poi_targets}
    if not poi_targets:
        return "completed"
    if "needs_repair" in statuses:
        return "needs_repair"
    if "missing" in statuses:
        return "missing"
    if "completed_with_degradation" in statuses:
        return "completed_with_degradation"
    return "completed"


def _action_for_stage(stage: dict) -> dict | None:
    status = stage["status"]
    if status == "needs_validation":
        return {
            "action": "validate_stage",
            "stage": stage["stage"],
            "reason": "产物存在，但当前只读离线校验不能证明本轮Live核验已完成",
        }
    if status == "missing":
        return {
            "action": "run_stage",
            "stage": stage["stage"],
            "reason": "当前阶段必需产物不存在",
        }
    if status == "needs_repair":
        return {
            "action": "repair_stage",
            "stage": stage["stage"],
            "reason": "确定性校验发现当前产物需要由所属角色返修",
        }
    if status == "stale":
        return {
            "action": "repair_stage",
            "stage": stage["stage"],
            "reason": "当前契约明确证明产物与上游不一致",
        }
    if status == "blocked":
        return {
            "action": "resolve_dependency",
            "stage": stage["stage"],
            "reason": "上游依赖或路线根契约尚未满足",
        }
    return None


def _build_route_only_resume_plan(
    project_id: str,
    manifest: dict,
    requirements: Any,
    route_plan: Any,
    *,
    expected_route_sha: str | None,
) -> dict[str, Any]:
    current_route_sha = route_structure_sha256(
        route_plan if isinstance(route_plan, dict) else None
    )
    route_hash_matches = (
        expected_route_sha is None or current_route_sha == expected_route_sha
    )
    route_evidence = [
        path
        for path, exists in (
            (_workspace_path("requirements.json"), isinstance(requirements, dict)),
            (_workspace_path("route_plan.json"), isinstance(route_plan, dict)),
            (
                _workspace_path("route_feasibility.json"),
                isinstance(
                    store.read_workspace(project_id, "route_feasibility.json"), dict
                ),
            ),
            (
                _workspace_path("route_map_validation.json"),
                isinstance(
                    store.read_workspace(project_id, "route_map_validation.json"), dict
                ),
            ),
        )
        if exists
    ]
    _source_project_id, contract_issues = _route_only_contract_issues(
        project_id,
        manifest,
    )
    progress = store.read_progress(project_id)
    if contract_issues:
        route_stage = _stage_record("route", "blocked", route_evidence, contract_issues)
    elif not isinstance(requirements, dict) or not isinstance(route_plan, dict):
        missing_issues = []
        if not isinstance(requirements, dict):
            missing_issues.append(
                {"code": "requirements_missing", "message": "缺少requirements.json"}
            )
        if not isinstance(route_plan, dict):
            missing_issues.append(
                {"code": "route_plan_missing", "message": "缺少route_plan.json"}
            )
        route_stage = _stage_record("route", "missing", route_evidence, missing_issues)
    else:
        boundary_issues = []
        if progress.state != ProjectPhase.SKELETON_CONFIRM:
            boundary_issues.append(
                {
                    "code": "route_only_progress_outside_confirmation",
                    "message": "route-only Project必须停在骨架确认中",
                }
            )
        if progress.skeleton_confirmed is not False:
            boundary_issues.append(
                {
                    "code": "route_only_skeleton_confirmed",
                    "message": "route-only Project不得确认路线骨架",
                }
            )
        expected_days = (requirements.get("requirements") or {}).get("days")
        if not isinstance(expected_days, int):
            expected_days = None
        validation_state = _route_only_validation_state(
            project_id,
            route_plan,
            expected_days=expected_days,
        )
        if boundary_issues:
            route_stage = _stage_record(
                "route", "blocked", route_evidence, boundary_issues
            )
        elif validation_state["blocking_issues"]:
            route_stage = _stage_record(
                "route",
                "needs_repair",
                route_evidence,
                validation_state["blocking_issues"],
            )
        elif validation_state["technical_issues"]:
            route_stage = _stage_record(
                "route",
                "needs_validation",
                route_evidence,
                validation_state["technical_issues"],
            )
        elif validation_state["business_issues"]:
            route_stage = _stage_record(
                "route",
                "completed_with_degradation",
                route_evidence,
                validation_state["business_issues"],
            )
        else:
            route_stage = _stage_record("route", "completed", route_evidence, [])

    completed_manifest = str(manifest.get("completion_mode") or "").startswith(
        "route_draft_completed"
    )
    next_actions: list[dict[str, Any]] = []
    if route_stage["status"] not in _RESUME_COMPLETE_STATUSES:
        if route_stage["status"] != "blocked" and route_hash_matches:
            action = _action_for_stage(route_stage)
            if action:
                next_actions.append(action)
    elif not completed_manifest:
        next_actions.append(
            {
                "action": "finalize_route_only",
                "stage": "route",
                "reason": "四类路线回执已完成，只封口route-only实验并保持骨架未确认",
            }
        )

    if route_stage["status"] == "blocked" or not route_hash_matches:
        plan_status = "blocked"
        next_actions = []
    elif completed_manifest and route_stage["status"] == "completed":
        plan_status = "completed"
    elif completed_manifest and route_stage["status"] == "completed_with_degradation":
        plan_status = "completed_with_degradation"
    else:
        plan_status = "action_required"
    return {
        "project_id": project_id,
        "mode": "full_plan_route_only",
        "status": plan_status,
        "route_structure_sha256": current_route_sha,
        "route_hash_matches": route_hash_matches,
        "project_progress_state": progress.state.value,
        "stages": [route_stage],
        "poi_targets": [],
        "next_actions": next_actions,
    }


def build_resume_plan(project_id: str, expected_route_sha: str | None = None) -> dict[str, Any]:
    """Derive a read-only recovery plan from the current Workspace."""
    project_path = store.project_dir(project_id)
    if not project_path.is_dir():
        raise FileNotFoundError(f"project {project_id!r} 不存在")

    requirements = store.read_requirements(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json")
    manifest = store.read_workspace(project_id, "experiment.json") or {}
    if _manifest_claims_route_only(manifest):
        return _build_route_only_resume_plan(
            project_id,
            manifest,
            requirements,
            route_plan,
            expected_route_sha=expected_route_sha,
        )
    current_route_sha = route_structure_sha256(route_plan if isinstance(route_plan, dict) else None)
    route_hash_matches = expected_route_sha is None or current_route_sha == expected_route_sha
    stages: list[dict] = []

    route_evidence = [
        path
        for path, exists in (
            (_workspace_path("requirements.json"), isinstance(requirements, dict)),
            (_workspace_path("route_plan.json"), isinstance(route_plan, dict)),
            (
                _workspace_path("route_feasibility.json"),
                isinstance(store.read_workspace(project_id, "route_feasibility.json"), dict),
            ),
            (
                _workspace_path("route_map_validation.json"),
                isinstance(
                    store.read_workspace(project_id, "route_map_validation.json"), dict
                ),
            ),
        )
        if exists
    ]
    if not isinstance(requirements, dict) or not isinstance(route_plan, dict):
        route_issues = []
        if not isinstance(requirements, dict):
            route_issues.append({"code": "requirements_missing", "message": "缺少requirements.json"})
        if not isinstance(route_plan, dict):
            route_issues.append({"code": "route_plan_missing", "message": "缺少route_plan.json"})
        route_stage = _stage_record("route", "missing", route_evidence, route_issues)
    else:
        route_validation = validate_stage(
            project_id,
            "route",
            expected_route_structure_sha256=expected_route_sha,
            persist=False,
        )
        route_status, route_issues = _route_resume_status(project_id, route_plan, route_validation)
        route_stage = _stage_record("route", route_status, route_evidence, route_issues)
    stages.append(route_stage)

    route_ready = route_stage["status"] in _RESUME_COMPLETE_STATUSES
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    lifestyle_evidence = [_workspace_path("outputs/lifestyle.json")] if isinstance(lifestyle, dict) else []
    if not route_ready:
        lifestyle_stage = _stage_record("lifestyle", "blocked", lifestyle_evidence, [])
    elif not isinstance(lifestyle, dict):
        lifestyle_stage = _stage_record("lifestyle", "missing", lifestyle_evidence, [])
    else:
        validation = validate_stage(project_id, "lifestyle", persist=False)
        status, issues = _output_resume_status(project_id, "lifestyle", lifestyle, validation)
        lifestyle_stage = _stage_record("lifestyle", status, lifestyle_evidence, issues)
    stages.append(lifestyle_stage)

    lifestyle_ready = lifestyle_stage["status"] in _RESUME_COMPLETE_STATUSES
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    pitfall_evidence = [_workspace_path("outputs/pitfall.json")] if isinstance(pitfall, dict) else []
    if not lifestyle_ready:
        pitfall_stage = _stage_record("pitfall", "blocked", pitfall_evidence, [])
    elif not isinstance(pitfall, dict):
        pitfall_stage = _stage_record("pitfall", "missing", pitfall_evidence, [])
    else:
        validation = validate_stage(project_id, "pitfall", persist=False)
        status, issues = _output_resume_status(project_id, "pitfall", pitfall, validation)
        pitfall_stage = _stage_record("pitfall", status, pitfall_evidence, issues)
    stages.append(pitfall_stage)

    pitfall_ready = pitfall_stage["status"] in _RESUME_COMPLETE_STATUSES
    final_paths = {
        "final_guide.md": store.read_workspace(project_id, "final_guide.md"),
        "final_guide.meta.json": store.read_workspace(project_id, "final_guide.meta.json"),
        "final_guide.blocks.json": store.read_workspace(project_id, "final_guide.blocks.json"),
    }
    integration_evidence = [
        _workspace_path(path) for path, value in final_paths.items() if value is not None
    ]
    if not pitfall_ready:
        integration_stage = _stage_record("integration", "blocked", integration_evidence, [])
    elif any(value is None for value in final_paths.values()):
        integration_stage = _stage_record("integration", "missing", integration_evidence, [])
    else:
        validation = validate_stage(project_id, "integration", persist=False)
        integration_issues = list(validation.get("issues") or [])
        issue_codes = {issue.get("code") for issue in integration_issues}
        if issue_codes & _FINAL_STALE_CODES:
            integration_status = "stale"
        elif integration_issues:
            integration_status = "needs_repair"
        else:
            integration_status = "completed"
        integration_stage = _stage_record(
            "integration", integration_status, integration_evidence, integration_issues
        )
    stages.append(integration_stage)

    integration_ready = integration_stage["status"] in _RESUME_COMPLETE_STATUSES
    if isinstance(route_plan, dict):
        poi_targets, join_required = _poi_target_statuses(project_id, route_plan)
    else:
        poi_targets, join_required = [], False
    poi_evidence = (
        [_workspace_path("outputs/poi_details/manifest.json")] if _manifest_data(project_id) else []
    )
    if not integration_ready:
        poi_stage = _stage_record("poi_detail", "blocked", poi_evidence, [])
    else:
        poi_stage = _stage_record("poi_detail", _poi_stage_status(poi_targets), poi_evidence, [])
    stages.append(poi_stage)

    poi_ready = poi_stage["status"] in _RESUME_COMPLETE_STATUSES
    if not integration_ready or not poi_ready:
        final_stage = _stage_record("final", "blocked", integration_evidence + poi_evidence, [])
    elif join_required:
        final_stage = _stage_record(
            "final",
            "stale",
            integration_evidence + poi_evidence,
            [{"code": "poi_join_stale", "message": "POI当前内容晚于Manifest或最终交付Join"}],
        )
    else:
        degraded_stages = [
            stage["stage"]
            for stage in (
                route_stage,
                lifestyle_stage,
                pitfall_stage,
                integration_stage,
                poi_stage,
            )
            if stage["status"] == "completed_with_degradation"
        ]
        final_stage = _stage_record(
            "final",
            "completed_with_degradation"
            if degraded_stages
            else "completed",
            integration_evidence + poi_evidence,
            (
                [
                    {
                        "code": "required_stage_degraded",
                        "stages": degraded_stages,
                        "message": "必要阶段存在已接受的降级，最终交付必须保留同一边界",
                    }
                ]
                if degraded_stages
                else []
            ),
        )
    stages.append(final_stage)

    next_actions: list[dict] = []
    for stage in stages[:4]:
        if stage["status"] not in _RESUME_COMPLETE_STATUSES:
            action = _action_for_stage(stage)
            if action:
                next_actions.append(action)
            break
    else:
        incomplete_pois = [
            target
            for target in poi_targets
            if target.get("status") not in _RESUME_COMPLETE_STATUSES
        ]
        if incomplete_pois:
            next_actions.extend(
                {
                    "action": "delegate_poi_detail",
                    "stage": "poi_detail",
                    "day": target["day"],
                    "poi_name": target["poi_name"],
                    "reason": "只补跑当前Manifest中缺失或失败的POI分支",
                }
                for target in incomplete_pois
            )
        elif final_stage["status"] == "stale":
            next_actions.append(
                {
                    "action": "finalize_poi",
                    "stage": "final",
                    "reason": "POI内容已变化，只需重新执行POI Join和最终契约",
                }
            )
        elif final_stage["status"] == "completed_with_degradation":
            next_actions.append(
                {
                    "action": "review_degradation",
                    "stage": "final",
                    "reason": "交付可保留，但需要向用户展示显式降级边界",
                }
            )

    progress = store.read_progress(project_id)
    if route_stage["status"] == "blocked" or not route_hash_matches:
        plan_status = "blocked"
    elif final_stage["status"] == "completed":
        plan_status = "completed"
    elif final_stage["status"] == "completed_with_degradation":
        plan_status = "completed_with_degradation"
    else:
        plan_status = "action_required"
    return {
        "project_id": project_id,
        "mode": "fixed_route",
        "status": plan_status,
        "route_structure_sha256": current_route_sha,
        "route_hash_matches": route_hash_matches,
        "project_progress_state": progress.state.value,
        "stages": stages,
        "poi_targets": poi_targets,
        "next_actions": next_actions,
    }


def finalize_integration(project_id: str) -> dict:
    topic, _requirements = _requirements_context(project_id)
    delivery.refresh_reference_section(project_id, topic)
    return validate_stage(project_id, "integration")


def finalize_poi_details(project_id: str, results: list[dict]) -> dict:
    topic, _requirements = _requirements_context(project_id)
    manifest = delivery.finalize_poi_details(project_id, topic, results)
    validation = validate_stage(project_id, "final")
    return {"manifest": manifest, "validation": validation}


def accept_route_risks(
    project_id: str,
    *,
    expected_route_structure_sha256: str,
    note: str,
) -> dict:
    """Persist an explicit human decision to keep a confirmed route.

    The checker findings remain unchanged.  Only the existing report-level
    ``force_finalized`` marker is set so recovery and delivery can distinguish
    an accepted degradation from a passed route gate.
    """
    decision_note = str(note or "").strip()
    if not decision_note:
        raise ValueError("accept-route-risks必须提供用户确认说明")
    manifest = store.read_workspace(project_id, "experiment.json") or {}
    if _manifest_claims_route_only(manifest):
        raise ValueError("route-only Project不得接受路线风险或代理确认骨架")
    if store.read_progress(project_id).skeleton_confirmed is not True:
        raise ValueError("路线骨架尚未确认，不能接受路线风险")

    route_plan = store.read_workspace(project_id, "route_plan.json")
    if not isinstance(route_plan, dict):
        raise FileNotFoundError(f"project {project_id!r} 缺少 route_plan.json")
    current_route_sha = route_structure_sha256(route_plan)
    if current_route_sha != expected_route_structure_sha256:
        raise ValueError("confirmed_route_structure_changed")

    raw_report = store.read_workspace(project_id, "route_feasibility.json")
    if not isinstance(raw_report, dict):
        raise FileNotFoundError(f"project {project_id!r} 缺少 route_feasibility.json")
    report = RouteFeasibilityReport.model_validate(raw_report)
    if report.route_plan_structure_hash != current_route_sha:
        raise ValueError("route_feasibility_stale")

    unresolved = [check for check in report.checks if check.status != "passed"]
    technical = [
        check
        for check in unresolved
        if _has_explicit_technical_error(check)
    ]
    if technical:
        raise ValueError("不能把unknown、渠道、解析或工具技术失败当作用户接受的路线风险")
    if any(check.status == "unknown" for check in unresolved):
        raise ValueError("普通业务unknown只保留可见提示，不得进入force_finalized")

    map_state = _route_map_resume_state(project_id, route_plan)
    if map_state["validation_issues"]:
        raise ValueError(
            "Route Map需要重新Live校验: "
            + ", ".join(
                str(item.get("code") or "unknown")
                for item in map_state["validation_issues"]
            )
        )
    if map_state["technical_issues"]:
        raise ValueError(
            "不能人工放行Route Map技术失败: "
            + ", ".join(
                str(item.get("code") or "unknown")
                for item in map_state["technical_issues"]
            )
        )
    map_issues = list(map_state["business_issues"])
    if not unresolved and not map_issues:
        raise ValueError("当前路线没有需要接受的业务风险")

    report.force_finalized = True
    store.write_route_feasibility(project_id, "citation_checker", report)
    accepted_checks = [
        {
            "check_id": check.check_id,
            "day": check.day,
            "target": check.target,
            "category": check.category,
            "status": check.status,
        }
        for check in unresolved
    ]
    store.log_event(
        project_id,
        actor="main_agent",
        action="accept_route_risks",
        target="route_feasibility.json",
        summary=decision_note,
    )
    append_trace(
        project_id,
        {
            "event": "user_confirmation",
            "stage": "route",
            "status": "accepted_with_degradation",
            "summary": decision_note,
            "accepted_checks": accepted_checks,
            "accepted_map_issues": map_issues,
        },
    )
    return {
        "project_id": project_id,
        "status": "accepted_with_degradation",
        "route_structure_sha256": current_route_sha,
        "route_structure_unchanged": True,
        "force_finalized": True,
        "accepted_checks": accepted_checks,
        "accepted_map_issues": map_issues,
    }


def _formal_run_status_from_trace(path: Path, manifest: dict | None) -> str:
    statuses: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(item, dict)
                and item.get("event") == "run_completed"
                and not str(item.get("continuation_mode") or "").strip()
            ):
                status = str(item.get("status") or "").strip()
                if status:
                    statuses.append(status)
    if statuses:
        return statuses[-1]
    return str((manifest or {}).get("completion_mode") or "unknown")


def _trace_has_delegation(path: Path, task_id: str) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(item, dict)
            and item.get("event") == "agent_delegated"
            and str(item.get("task_id") or "").strip() == task_id
        ):
            return True
    return False


def append_trace(project_id: str, event: dict) -> dict:
    """Append an observable execution event; hidden reasoning is never recorded."""
    if not isinstance(event, dict):
        raise ValueError("trace event必须是JSON对象")
    event_type = str(event.get("event") or "").strip()
    if event_type not in _TRACE_EVENTS:
        raise ValueError(f"不支持的trace event: {event_type!r}")
    summary = str(event.get("summary") or "").strip()
    if not summary:
        raise ValueError("trace event必须包含非空summary")
    if event_type == "observation" and event.get("status") != "hypothesis":
        raise ValueError("observation必须标记status=hypothesis")
    if event_type in {"agent_delegated", "agent_completed"}:
        task_id = event.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError(f"{event_type} trace event必须包含非空task_id")
    path = store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"
    if event_type == "agent_completed" and not _trace_has_delegation(
        path,
        str(event["task_id"]).strip(),
    ):
        raise ValueError("agent_completed必须关联trace中已有agent_delegated task_id")

    record = dict(event)
    record["event"] = event_type
    record["summary"] = summary
    if event_type in {"agent_delegated", "agent_completed"}:
        record["task_id"] = str(event["task_id"]).strip()
    record.setdefault("timestamp", _now())
    record["project_id"] = project_id
    record["route_structure_sha256"] = route_structure_sha256(
        store.read_workspace(project_id, "route_plan.json")
    )
    manifest = (
        store.read_workspace(project_id, "experiment.json")
        if event_type == "run_completed"
        else None
    )
    continuation_mode = str(record.get("continuation_mode") or "").strip()
    if event_type == "run_completed" and continuation_mode:
        record["declared_comparison_grade"] = str(
            (manifest or {}).get("comparison_grade") or "unknown"
        )
        record["effective_comparison_grade"] = "C"
        record["formal_run_status"] = _formal_run_status_from_trace(path, manifest)
    if event_type == "agent_delegated":
        role_alias = str(event.get("role") or event.get("agent") or "").strip()
        normalized_role = {
            "travel-route": "route",
            "route_agent": "route",
            "travel-lifestyle": "lifestyle",
            "lifestyle_agent": "lifestyle",
            "travel-pitfall": "pitfall",
            "pitfall_agent": "pitfall",
            "travel-integration": "integration",
            "integration_agent": "integration",
            "travel-poi-detail": "poi_detail",
            "poi_detail_agent": "poi_detail",
        }.get(role_alias, role_alias)
        current_prompt = compute_prompt_fingerprint()
        role_prompt = (current_prompt.get("per_role") or {}).get(normalized_role)
        if role_prompt:
            record["prompt_fingerprint"] = {
                "role": normalized_role,
                "aggregate_sha256": current_prompt.get("aggregate_sha256"),
                "role_sha256": role_prompt.get("aggregate_sha256"),
                "files": role_prompt.get("files") or {},
            }
        manifest = store.read_workspace(project_id, "experiment.json") or {}
        record["host_model"] = manifest.get("model") or "host_managed"
        baseline = store.read_workspace(project_id, "material_baseline.json") or {}
        expected_prompt = baseline.get("prompt_fingerprint") or {}
        if (
            str(manifest.get("comparison_grade") or "").upper() == "A"
            and expected_prompt.get("aggregate_sha256")
            != current_prompt.get("aggregate_sha256")
        ):
            raise ValueError("prompt_fingerprint_changed")
    record["trace_schema_version"] = _TRACE_SCHEMA_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if event_type == "run_completed":
        if continuation_mode:
            reason_codes = record.get("reason_codes")
            if isinstance(reason_codes, str):
                reason_codes = [reason_codes]
            if not isinstance(reason_codes, list):
                reason_codes = []
            normalized_reason_codes = [
                str(reason).strip() for reason in reason_codes if str(reason).strip()
            ] or [continuation_mode]
            store.append_post_run_correction(
                project_id,
                {
                    "correction_type": "experiment_validity",
                    "declared_comparison_grade": record["declared_comparison_grade"],
                    "effective_comparison_grade": "C",
                    "formal_run_status": record["formal_run_status"],
                    "continuation_status": str(record.get("status") or "completed"),
                    "continuation_mode": continuation_mode,
                    "reason_codes": normalized_reason_codes,
                    "capture_mode": str(record.get("capture_mode") or "trace_observed"),
                    "continuation_completed_at": record.get("timestamp"),
                    "summary": summary,
                },
            )
        elif isinstance(manifest, dict):
            updated = {
                key: value
                for key, value in manifest.items()
                if key not in {"schema_version", "project_id", "updated_at"}
            }
            updated["completion_mode"] = str(record.get("status") or "completed")
            store.write_experiment_manifest(project_id, updated)
    return {"trace_path": str(path), "event": record}
