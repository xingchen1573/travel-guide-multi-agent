from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.codex_skill.bridge import build_resume_plan, route_structure_sha256
from src.config import PROJECT_ROOT
from src.workspace.final_guide_blocks import collect_upstream_claims

SCHEMA_VERSION = "1"
CATALOG_PATH = Path(__file__).with_name("catalog.json")


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    # JSON strings may legally contain Unicode line/paragraph separators. JSONL
    # records in this project are delimited only by a physical LF byte.
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def load_catalog(path: Path | None = None) -> dict[str, dict[str, Any]]:
    payload = _read_json(path or CATALOG_PATH, {}) or {}
    return {
        str(item["project_id"]): item
        for item in payload.get("experiments", [])
        if isinstance(item, dict) and item.get("project_id")
    }


def load_comparison_pairs(path: Path | None = None) -> list[dict[str, Any]]:
    """Return explicitly registered 1:1 comparison pairs.

    A pair is deliberately separate from the looser experiment ``relations``
    graph: relations describe lineage, while a comparison pair is the unit that
    can support a two-harness conclusion.
    """
    payload = _read_json(path or CATALOG_PATH, {}) or {}
    return [
        dict(item)
        for item in payload.get("comparison_pairs", [])
        if isinstance(item, dict) and item.get("pair_id")
    ]


def load_catalog_with_project_manifests(
    *, projects_dir: Path, catalog_path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Merge explicit catalog entries with project-local experiment manifests."""
    catalog = load_catalog(catalog_path)
    for path in sorted(projects_dir.glob("*/workspace/experiment.json")):
        manifest = _read_json(path, {}) or {}
        project_id = str(manifest.get("project_id") or path.parents[1].name)
        if not project_id or project_id in catalog:
            continue
        if manifest.get("harness") not in {"codex_skill", "langgraph"}:
            continue
        catalog[project_id] = {
            key: value
            for key, value in manifest.items()
            if key not in {"schema_version", "updated_at"}
        }
    return catalog


def _evidence(value: Any, source_paths: Iterable[str], method: str, *, observed: bool = False, missing: str | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "value_kind": "unknown" if value is None else ("observed" if observed else "derived"),
        "source_paths": list(source_paths),
        "calculation_method": method if value is not None else None,
        "missing_reason": missing if value is None else None,
    }


def _iter_items(output: dict[str, Any] | None) -> Iterable[dict[str, Any]]:
    for field in ("food", "outfit", "lodging", "play"):
        section = (output or {}).get(field)
        for item in section.get("items", []) if isinstance(section, dict) else []:
            if isinstance(item, dict):
                yield item


def _raw_claims(route: dict, lifestyle: dict, pitfall: dict, poi_details: list[dict]) -> dict[str, dict[str, Any]]:
    claims: dict[str, dict[str, Any]] = {}

    def add(rows: Iterable[Any]) -> None:
        for row in rows:
            if isinstance(row, dict) and row.get("claim_id"):
                claims[str(row["claim_id"])] = row

    for day in route.get("days", []):
        if isinstance(day, dict):
            add(day.get("claims", []))
    for output in (lifestyle, pitfall):
        for item in _iter_items(output):
            add(item.get("claims", []))
    for detail in poi_details:
        for route_item in detail.get("routes", []):
            if not isinstance(route_item, dict):
                continue
            for segment in route_item.get("route_segments", []):
                if isinstance(segment, dict):
                    add(segment.get("claims", []))
    return claims


def _mapped_claim_ids(blocks: dict[str, Any]) -> set[str]:
    return {
        str(claim_id)
        for block in blocks.get("blocks", [])
        if isinstance(block, dict)
        for claim_id in block.get("upstream_claim_ids", [])
        if claim_id
    }


def _force_finalized_count(values: Iterable[Any]) -> int:
    count = 0

    def walk(value: Any) -> None:
        nonlocal count
        if isinstance(value, dict):
            if value.get("citation_status") == "force_finalized" or value.get("status") == "force_finalized":
                count += 1
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for value in values:
        walk(value)
    return count


def _route_gate(
    feasibility: dict[str, Any],
    validation_runs: list[dict[str, Any]],
    route: dict[str, Any],
    route_entities: dict[str, Any],
    route_execution: dict[str, Any],
) -> dict[str, Any]:
    counts = {"passed_count": 0, "warning_count": 0, "blocked_count": 0, "unknown_count": 0, "technical_error_count": 0}
    coverage_counts = {"covered": 0, "partial": 0, "missing": 0, "not_applicable": 0, "unknown": 0}
    for check in feasibility.get("checks", []) if isinstance(feasibility, dict) else []:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "unknown").lower()
        if status == "passed":
            counts["passed_count"] += 1
        elif status == "warning":
            counts["warning_count"] += 1
        elif status in {"blocked", "failed", "fail"}:
            counts["blocked_count"] += 1
        elif status == "technical_error" or check.get("technical_error"):
            counts["technical_error_count"] += 1
        else:
            counts["unknown_count"] += 1
        coverage = str(check.get("coverage_status") or "unknown")
        coverage_counts[coverage if coverage in coverage_counts else "unknown"] += 1

    route_artifact = {
        "route_plan": route,
        "route_entities": route_entities,
        "route_execution": route_execution,
    }
    artifact_sha = hashlib.sha256(
        json.dumps(
            route_artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    live_component_statuses: dict[str, str] = {}
    offline_component_statuses: dict[str, str] = {}
    live_unresolved_check_ids: list[str] = []
    for run in validation_runs:
        if run.get("stage") != "route" or run.get("artifact_sha256") != artifact_sha:
            continue
        gate = run.get("route_gate") or {}
        observation_mode = str(run.get("observation_mode") or "offline")
        target_statuses = (
            live_component_statuses
            if observation_mode == "live"
            else offline_component_statuses
        )
        target_statuses.update(
            {
                str(key): str(value)
                for key, value in (gate.get("component_statuses") or {}).items()
            }
        )
        if (
            observation_mode == "live"
            and "route_feasibility" in (gate.get("component_statuses") or {})
        ):
            live_unresolved_check_ids = list(gate.get("unresolved_check_ids") or [])

    effective_component_statuses = {
        key: value
        for key, value in offline_component_statuses.items()
        if value != "not_run_offline"
    }
    effective_component_statuses.update(live_component_statuses)

    entity_status_counts: dict[str, int] = {}
    for entity in route_entities.get("entities", []) if isinstance(route_entities, dict) else []:
        status = str(entity.get("status") or "unknown")
        entity_status_counts[status] = entity_status_counts.get(status, 0) + 1
    transport_mode_counts: dict[str, int] = {}
    for leg in route_execution.get("route_legs", []) if isinstance(route_execution, dict) else []:
        mode = str(leg.get("transport_mode") or "unknown")
        transport_mode_counts[mode] = transport_mode_counts.get(mode, 0) + 1

    return {
        **counts,
        "coverage_counts": coverage_counts,
        "latest_live_component_statuses": live_component_statuses,
        "latest_offline_component_statuses": offline_component_statuses,
        "effective_component_statuses": effective_component_statuses,
        "live_unresolved_check_ids": live_unresolved_check_ids,
        "component_statuses": effective_component_statuses,
        "unresolved_check_ids": live_unresolved_check_ids,
        "entity_status_counts": entity_status_counts,
        "transport_mode_counts": transport_mode_counts,
    }


def _research_metrics(
    research_runs: list[dict[str, Any]], material_baseline: dict[str, Any]
) -> dict[str, Any]:
    source_kind_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    scope_hit_counts: dict[str, int] = {}
    scope_error_counts: dict[str, int] = {}
    modality_error_counts: dict[str, int] = {}
    empty_result_count = 0
    result_count = 0
    for run in research_runs:
        source_kind = str(run.get("source_kind") or "unknown")
        channel = str(run.get("channel") or source_kind)
        stage = str(run.get("stage") or "unknown")
        status = str(run.get("status") or "unknown")
        source_kind_counts[source_kind] = source_kind_counts.get(source_kind, 0) + 1
        channel_counts[channel] = channel_counts.get(channel, 0) + 1
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
        result_count += int(run.get("result_count") or 0)
        if status == "empty":
            empty_result_count += 1
        for scope, count in (run.get("result_scopes") or {}).items():
            scope_hit_counts[str(scope)] = scope_hit_counts.get(str(scope), 0) + int(
                count or 0
            )
        for scope in (run.get("scope_errors") or {}):
            scope_error_counts[str(scope)] = scope_error_counts.get(str(scope), 0) + 1
        for modality in (
            ((run.get("retrieval_diagnostics") or {}).get("modality_errors") or {})
        ):
            modality_error_counts[str(modality)] = (
                modality_error_counts.get(str(modality), 0) + 1
            )
    return {
        "call_count": len(research_runs),
        "result_count": result_count,
        "empty_result_count": empty_result_count,
        "source_kind_counts": source_kind_counts,
        "channel_counts": channel_counts,
        "stage_counts": stage_counts,
        "status_counts": status_counts,
        "scope_hit_counts": scope_hit_counts,
        "scope_error_counts": scope_error_counts,
        "modality_error_counts": modality_error_counts,
        "base_corpus_sha256": material_baseline.get("corpus_sha256"),
        "prompt_fingerprint_sha256": (
            material_baseline.get("prompt_fingerprint") or {}
        ).get("aggregate_sha256"),
        "per_role_prompt_fingerprint": (
            material_baseline.get("prompt_fingerprint") or {}
        ).get("per_role", {}),
    }


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _derived_run_id(project_id: str, started_at: str | None) -> str | None:
    if not started_at:
        return None
    digest = hashlib.sha256(f"{project_id}:{started_at}".encode("utf-8")).hexdigest()
    return f"reconstructed-{digest[:16]}"


def _run_segments(project_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    active_start: dict[str, Any] | None = None
    unpaired_start_count = 0
    unpaired_completion_count = 0

    for row in rows:
        event = row.get("event")
        if event == "run_start":
            if active_start is not None:
                unpaired_start_count += 1
            active_start = row
            continue
        if event != "run_completed":
            continue
        if active_start is None:
            unpaired_completion_count += 1
            continue

        started_dt = _timestamp(active_start.get("timestamp") or active_start.get("ts"))
        completed_dt = _timestamp(row.get("timestamp") or row.get("ts"))
        started_at = started_dt.isoformat() if started_dt else None
        completed_at = completed_dt.isoformat() if completed_dt else None
        continuation_mode = str(
            row.get("continuation_mode") or active_start.get("continuation_mode") or ""
        ).strip()
        explicit_continuation = bool(continuation_mode) or any(
            marker in str(value or "").lower()
            for marker in ("continuation", "diagnostic")
            for value in (
                active_start.get("stage"),
                active_start.get("status"),
                active_start.get("summary"),
                row.get("stage"),
                row.get("status"),
                row.get("summary"),
            )
        )
        wall = (
            (completed_dt - started_dt).total_seconds()
            if started_dt is not None and completed_dt is not None
            else None
        )
        segments.append(
            {
                "run_id": _derived_run_id(project_id, started_at),
                "run_kind": "continuation" if explicit_continuation else "formal",
                "capture_mode": "reconstructed",
                "started_at": started_at,
                "completed_at": completed_at,
                "status": str(row.get("status") or "unknown"),
                "continuation_mode": continuation_mode or None,
                "wall_clock_seconds": wall,
                "missing_reason": (
                    None
                    if wall is not None
                    else "run_start或run_completed缺少可解析时间"
                ),
            }
        )
        active_start = None

    if active_start is not None:
        unpaired_start_count += 1

    formal_run: dict[str, Any] | None = None
    continuation_runs: list[dict[str, Any]] = []
    for segment in segments:
        if formal_run is None and segment["run_kind"] == "formal":
            formal_run = segment
            continue
        continuation = dict(segment)
        continuation["run_kind"] = "continuation"
        if not continuation.get("continuation_mode"):
            continuation["continuation_mode"] = "unspecified_continuation"
        continuation_runs.append(continuation)

    missing_reason = None
    if unpaired_start_count or unpaired_completion_count:
        missing_reason = "Trace存在未配对的run_start或run_completed事件"
    if not segments:
        missing_reason = "Trace缺少成对的run_start与run_completed事件"
    return {
        "formal_run": formal_run,
        "continuation_runs": continuation_runs,
        "capture_mode": "reconstructed" if segments else "legacy_fallback",
        "paired_run_count": len(segments),
        "unpaired_start_count": unpaired_start_count,
        "unpaired_completion_count": unpaired_completion_count,
        "missing_reason": missing_reason,
    }


def _execution(project_dir: Path, harness: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if harness == "codex_skill":
        paths = [project_dir / "agents" / "codex_skill" / "trace.jsonl"]
    else:
        paths = sorted((project_dir / "agents").glob("**/trace.jsonl"))
    rows = [row for path in paths for row in _read_jsonl(path)]
    timestamps = [_timestamp(row.get("timestamp") or row.get("ts")) for row in rows]
    timestamps = [value for value in timestamps if value is not None]
    trace_started_dt = min(timestamps) if timestamps else None
    trace_started = trace_started_dt.isoformat() if trace_started_dt else None
    completed_events = [row for row in rows if row.get("event") == "run_completed"]
    completed_dt = _timestamp(completed_events[-1].get("timestamp")) if completed_events else None
    trace_completed = completed_dt.isoformat() if completed_dt else None
    trace_span = (
        (completed_dt - trace_started_dt).total_seconds()
        if completed_dt is not None and trace_started_dt is not None
        else None
    )

    if harness == "codex_skill":
        segmentation = _run_segments(project_dir.name, rows)
        formal_run = segmentation["formal_run"]
        if formal_run is not None:
            started = formal_run["started_at"]
            completed = formal_run["completed_at"]
            wall = formal_run["wall_clock_seconds"]
            wall_method = "formal_run_completed_at_minus_run_start"
            wall_missing = formal_run.get("missing_reason")
        else:
            started = trace_started
            completed = trace_completed
            wall = trace_span
            wall_method = "legacy_trace_span_fallback"
            wall_missing = segmentation["missing_reason"]
            formal_run = {
                "run_id": None,
                "run_kind": "formal",
                "capture_mode": "legacy_fallback",
                "started_at": started,
                "completed_at": completed,
                "status": str(completed_events[-1].get("status") or "unknown")
                if completed_events
                else "unknown",
                "continuation_mode": None,
                "wall_clock_seconds": wall,
                "missing_reason": wall_missing,
            }
        delegated = 0
        business_retry = 0
        transport_retry = 0
        legacy_business_retry = 0
        legacy_transport_retry = 0
        explicit_business_retry = 0
        explicit_transport_retry = 0
        local_claim_repair_rounds = 0
        locally_repaired_claims = 0
        recovery = 0
        takeover = 0
        human = 0
        reasons: list[str] = []
        for row in rows:
            event = row.get("event")
            if event == "agent_delegated":
                delegated += 1
            elif event == "agent_fanout":
                delegated += len(row.get("tasks") or [])
            if event == "agent_completed":
                legacy_business_retry += sum(int(row.get(key) or 0) for key in ("content_repair_rounds", "business_content_repair_rounds", "validation_repair_rounds", "repair_rounds", "write_repair_rounds", "metadata_repair_rounds"))
                legacy_transport_retry += int(row.get("technical_retries") or 0)
                local_claim_repairs = int(row.get("local_claim_repairs") or 0)
                locally_repaired_claims += local_claim_repairs
                if local_claim_repairs > 0:
                    local_claim_repair_rounds += 1
                takeover += int(row.get("takeovers") or int(bool(row.get("takeover"))))
                human += int(row.get("human_interventions") or 0)
            if event == "retry":
                recovery += 1
                status = str(row.get("status") or "")
                if status == "content_repair":
                    explicit_business_retry += 1
                elif status in {"provider_network_failure", "transport_retry"}:
                    explicit_transport_retry += 1
            status = str(row.get("status") or "")
            if "degrad" in status or status in {"warning", "force_finalized"}:
                summary = str(row.get("warning_reason") or row.get("summary") or status)
                if summary and summary not in reasons:
                    reasons.append(summary)
        business_retry = (
            explicit_business_retry
            or legacy_business_retry
            or local_claim_repair_rounds
        )
        transport_retry = explicit_transport_retry or legacy_transport_retry
        execution = {
            "started_at": _evidence(started, ["agents/codex_skill/trace.jsonl"], "minimum_trace_timestamp", observed=True, missing="Codex中央Trace缺少可解析时间"),
            "completed_at": _evidence(completed, ["agents/codex_skill/trace.jsonl"], "run_completed_timestamp", observed=True, missing="尚无run_completed事件"),
            "wall_clock_seconds": _evidence(wall, ["agents/codex_skill/trace.jsonl"], wall_method, missing=wall_missing or "运行未完成或时间戳不完整"),
            "project_trace_span_seconds": _evidence(trace_span, ["agents/codex_skill/trace.jsonl"], "last_run_completed_minus_minimum_trace_timestamp", missing="运行未完成或时间戳不完整"),
            "formal_run": formal_run,
            "continuation_runs": segmentation["continuation_runs"],
            "run_segmentation": {
                key: value
                for key, value in segmentation.items()
                if key not in {"formal_run", "continuation_runs"}
            },
            "agent_task_count": _evidence(delegated, ["agents/codex_skill/trace.jsonl"], "count_agent_delegated_and_fanout_tasks"),
            "business_retry_count": _evidence(business_retry, ["agents/codex_skill/trace.jsonl"], "count_content_repair_retry_events_with_legacy_fallback"),
            "locally_repaired_claim_count": _evidence(locally_repaired_claims, ["agents/codex_skill/trace.jsonl"], "sum_agent_completed_local_claim_repairs"),
            "transport_retry_count": _evidence(transport_retry, ["agents/codex_skill/trace.jsonl"], "count_transport_retry_events_with_legacy_fallback"),
            "recovery_count": _evidence(recovery, ["agents/codex_skill/trace.jsonl"], "count_retry_events"),
            "takeover_count": _evidence(takeover, ["agents/codex_skill/trace.jsonl"], "sum_explicit_takeover_fields"),
            "human_intervention_count": _evidence(human, ["agents/codex_skill/trace.jsonl"], "sum_explicit_human_interventions"),
            "degradation_reasons": reasons,
        }
    else:
        rel_paths = [path.relative_to(project_dir).as_posix() for path in paths]
        execution = {
            "started_at": _evidence(trace_started, rel_paths, "minimum_llm_trace_timestamp", observed=True, missing="LangGraph Trace缺少可解析时间"),
            "completed_at": _evidence(None, rel_paths, "", missing="LangGraph历史Trace没有统一run_completed事件"),
            "wall_clock_seconds": _evidence(None, rel_paths, "", missing="缺少可靠的统一运行结束时间"),
            "project_trace_span_seconds": _evidence(None, rel_paths, "", missing="缺少统一run_completed事件"),
            "formal_run": None,
            "continuation_runs": [],
            "run_segmentation": {
                "capture_mode": "unavailable",
                "paired_run_count": 0,
                "unpaired_start_count": 0,
                "unpaired_completion_count": 0,
                "missing_reason": "LangGraph历史Trace没有统一运行边界",
            },
            "agent_task_count": _evidence(None, rel_paths, "", missing="模型调用与Agent任务不是同一指标"),
            "business_retry_count": _evidence(None, rel_paths, "", missing="历史LangGraph未统一区分业务返修"),
            "locally_repaired_claim_count": _evidence(None, rel_paths, "", missing="历史LangGraph未记录局部修复Claim数"),
            "transport_retry_count": _evidence(None, rel_paths, "", missing="历史LangGraph未统一区分传输重试"),
            "recovery_count": _evidence(None, rel_paths, "", missing="Checkpoint存在不等于发生恢复"),
            "takeover_count": _evidence(None, rel_paths, "", missing="历史运行未统一记录接管"),
            "human_intervention_count": _evidence(None, rel_paths, "", missing="历史运行未统一记录人工介入"),
            "degradation_reasons": [],
        }
    return execution, rows


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _route_map_measurement_count(route_map: dict[str, Any]) -> int:
    count = 0
    for day in route_map.get("days", []) if isinstance(route_map, dict) else []:
        if not isinstance(day, dict):
            continue
        for leg in day.get("legs", []):
            if not isinstance(leg, dict):
                continue
            transport_mode = str(leg.get("transport_mode") or "").strip()
            if transport_mode == "mixed":
                candidates = [
                    segment
                    for segment in leg.get("segments", [])
                    if isinstance(segment, dict)
                    and str(segment.get("mode") or "").strip() in {"driving", "taxi"}
                ]
            elif transport_mode and transport_mode not in {"driving", "taxi"}:
                candidates = []
            else:
                # Historical Route Map rows predate transport_mode and used driving by default.
                candidates = [leg]
            for candidate in candidates:
                if candidate.get("status") not in {"passed", "anomaly"}:
                    continue
                if _is_finite_number(candidate.get("distance_km")) and _is_finite_number(
                    candidate.get("duration_h")
                ):
                    count += 1
    return count


def _tool_and_context(project_dir: Path, harness: str, trace_rows: list[dict[str, Any]], *, model_trace_complete: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    web_paths = sorted(project_dir.glob("agents/**/web_search_log.jsonl"))
    amap_paths = sorted(project_dir.glob("agents/**/amap_evidence_log.jsonl"))
    context_paths = sorted(project_dir.glob("agents/**/context_events.jsonl"))
    web_count = sum(len(_read_jsonl(path)) for path in web_paths)
    amap_log_rows = [row for path in amap_paths for row in _read_jsonl(path)]
    amap_count = len(amap_log_rows)
    route_map_reuse_log_count = sum(
        str(row.get("source") or "") == "route_map_validation"
        for row in amap_log_rows
    )
    route_map_path = project_dir / "workspace" / "route_map_validation.json"
    route_map = _read_json(route_map_path, {}) or {}
    route_map_measurement_count = _route_map_measurement_count(route_map)
    trace_paths = sorted(project_dir.glob("agents/**/trace.jsonl")) if harness == "langgraph" else []
    trace_complete = harness == "langgraph" and model_trace_complete and bool(trace_paths)
    usage_rows = [row for row in trace_rows if isinstance(row.get("usage"), dict)]
    model_calls = len(usage_rows) if trace_complete else None
    input_tokens = sum(int(row["usage"].get("input") or 0) for row in usage_rows) if trace_complete else None
    output_tokens = sum(int(row["usage"].get("output") or 0) for row in usage_rows) if trace_complete else None
    reasoning_known = trace_complete and all(row["usage"].get("reasoning") is not None for row in usage_rows)
    reasoning_tokens = sum(int(row["usage"].get("reasoning") or 0) for row in usage_rows) if reasoning_known else None
    provider_paths = sorted(project_dir.glob("agents/**/provider_calls.jsonl"))
    provider_rows = [row for path in provider_paths for row in _read_jsonl(path)]
    provider_rel_paths = [path.relative_to(project_dir).as_posix() for path in provider_paths]
    web_rows = [row for row in provider_rows if row.get("provider") == "web"]
    amap_rows = [row for row in provider_rows if row.get("provider") == "amap"]
    web_attempts = len(web_rows)
    web_successes = sum(
        row.get("status") == "success" for row in web_rows
    )
    amap_attempts = len(amap_rows)
    host_usage_missing = (
        "codex_host_usage_not_exposed" if harness == "codex_skill" else None
    )
    tool_usage = {
        "web_persisted_query_count": _evidence(web_count, [p.relative_to(project_dir).as_posix() for p in web_paths], "count_valid_jsonl_records"),
        "web_attempt_count": _evidence(web_attempts if web_rows else None, provider_rel_paths, "count_provider_call_rows(provider=web)", observed=True, missing="尚无Web provider调用日志"),
        "web_success_count": _evidence(web_successes if web_rows else None, provider_rel_paths, "count_provider_call_rows(provider=web,status=success)", observed=True, missing="尚无Web provider调用日志"),
        "amap_persisted_record_count": _evidence(amap_count, [p.relative_to(project_dir).as_posix() for p in amap_paths], "count_valid_jsonl_records"),
        "amap_evidence_log_record_count": _evidence(amap_count, [p.relative_to(project_dir).as_posix() for p in amap_paths], "count_valid_amap_evidence_jsonl_records"),
        "amap_route_map_reuse_log_record_count": _evidence(route_map_reuse_log_count, [p.relative_to(project_dir).as_posix() for p in amap_paths], "count_amap_evidence_rows(source=route_map_validation)"),
        "amap_route_map_measurement_count": _evidence(route_map_measurement_count, ["workspace/route_map_validation.json"] if route_map_path.is_file() else [], "count_numeric_passed_or_anomaly_route_map_measurements"),
        "amap_persisted_total_count": _evidence(amap_count - route_map_reuse_log_count + route_map_measurement_count, [p.relative_to(project_dir).as_posix() for p in amap_paths] + (["workspace/route_map_validation.json"] if route_map_path.is_file() else []), "sum_amap_log_rows_excluding_route_map_reuse_plus_route_map_measurements"),
        "amap_attempt_count": _evidence(amap_attempts if amap_rows else None, provider_rel_paths, "count_provider_call_rows(provider=amap)", observed=True, missing="尚无高德provider调用日志"),
        "model_call_count": _evidence(model_calls, [p.relative_to(project_dir).as_posix() for p in trace_paths], "count_trace_rows_with_usage", missing=host_usage_missing or "模型调用Trace不完整"),
        "input_tokens": _evidence(input_tokens, [p.relative_to(project_dir).as_posix() for p in trace_paths], "sum_usage_input", missing=host_usage_missing or "Token埋点不完整"),
        "output_tokens": _evidence(output_tokens, [p.relative_to(project_dir).as_posix() for p in trace_paths], "sum_usage_output", missing=host_usage_missing or "Token埋点不完整"),
        "reasoning_tokens": _evidence(reasoning_tokens, [p.relative_to(project_dir).as_posix() for p in trace_paths], "sum_usage_reasoning", missing=host_usage_missing or "至少一条模型调用未记录reasoning token"),
        "monetary_cost": _evidence(None, [], "", missing=host_usage_missing or "当前未记录可审计费用"),
    }
    context_rows = [row for path in context_paths for row in _read_jsonl(path)]
    compactions = sum(str(row.get("status") or "") == "compacted" for row in context_rows)
    exceeded = sum(str(row.get("status") or "") in {"budget_exceeded", "context_budget_exceeded"} for row in context_rows)
    context = {
        "compaction_count": _evidence(compactions if context_paths else (0 if harness == "codex_skill" else None), [p.relative_to(project_dir).as_posix() for p in context_paths], "count_status_compacted", missing="没有上下文事件日志"),
        "context_budget_exceeded_count": _evidence(exceeded if context_paths else (0 if harness == "codex_skill" else None), [p.relative_to(project_dir).as_posix() for p in context_paths], "count_budget_exceeded_events", missing="没有上下文事件日志"),
        "history_message_count": _evidence(None, [], "", missing="缺少跨Harness统一的最终历史消息口径"),
        "failed_draft_preserved": _evidence(None, [], "", missing="需要专项恢复审计，不能仅凭文件存在判断"),
    }
    return tool_usage, context


def _stage_rows(route: dict, feasibility: dict, lifestyle: dict, pitfall: dict, manifest: dict, final_meta: dict, final_exists: bool) -> list[dict[str, Any]]:
    manifest_rows = manifest.get("generated", []) if isinstance(manifest, dict) else []
    poi_statuses = [str(item.get("citation_status") or "unknown") for item in manifest_rows if isinstance(item, dict)]
    poi_status = (
        "generation_failed"
        if "generation_failed" in poi_statuses
        else "force_finalized"
        if "force_finalized" in poi_statuses
        else "passed"
        if poi_statuses
        else "unknown"
    )
    contract = final_meta.get("contract_validation", {}) if isinstance(final_meta, dict) else {}
    return [
        {"key": "prepare", "label": "Prepare", "status": "passed" if route else "failed", "evidence_paths": ["workspace/requirements.json", "workspace/route_plan.json"]},
        {"key": "route", "label": "Route Gate", "status": str(feasibility.get("overall_status") or "unknown"), "evidence_paths": ["workspace/route_feasibility.json", "workspace/route_map_validation.json", "workspace/evidence_checks.json"]},
        {"key": "lifestyle", "label": "Lifestyle", "status": "force_finalized" if _force_finalized_count([lifestyle]) else ("passed" if lifestyle else "unknown"), "evidence_paths": ["workspace/outputs/lifestyle.json"]},
        {"key": "pitfall", "label": "Pitfall", "status": "force_finalized" if _force_finalized_count([pitfall]) else ("passed" if pitfall else "unknown"), "evidence_paths": ["workspace/outputs/pitfall.json"]},
        {"key": "integration", "label": "Integration", "status": str(contract.get("status") or ("passed" if final_meta else "unknown")), "evidence_paths": ["workspace/final_guide.blocks.json", "workspace/final_guide.meta.json"]},
        {"key": "poi", "label": "POI Fan-out", "status": poi_status, "evidence_paths": ["workspace/outputs/poi_details/manifest.json"]},
        {"key": "final", "label": "Final Delivery", "status": str(contract.get("status") or ("passed" if final_exists else "unknown")), "evidence_paths": ["workspace/final_guide.md", "workspace/final_guide.meta.json"]},
    ]


def _poi_depth_rows(
    route: dict[str, Any],
    manifest: dict[str, Any],
    poi_details: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe structural content depth without changing citation semantics."""
    detail_by_key = {
        (int(detail.get("day") or 0), str(detail.get("poi_name") or "")): detail
        for detail in poi_details
        if isinstance(detail, dict)
    }
    manifest_by_key = {
        (int(item.get("day") or 0), str(item.get("poi_name") or "")): item
        for item in manifest.get("generated", [])
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    for day in route.get("days", []) if isinstance(route, dict) else []:
        if not isinstance(day, dict):
            continue
        day_no = int(day.get("day") or 0)
        for poi in day.get("pois", []) or []:
            if not isinstance(poi, dict) or not poi.get("needs_detail"):
                continue
            poi_name = str(poi.get("name") or "")
            key = (day_no, poi_name)
            detail = detail_by_key.get(key) or {}
            manifest_item = manifest_by_key.get(key) or {}
            routes = [item for item in detail.get("routes", []) if isinstance(item, dict)]
            route_count = len(routes)
            recommended_route_count = sum(bool(item.get("recommended")) for item in routes)
            segment_count = sum(
                len(item.get("route_segments", []) or []) for item in routes
            )
            recommended_segment_count = sum(
                len(item.get("route_segments", []) or [])
                for item in routes
                if item.get("recommended")
            )
            claim_count = sum(
                len(segment.get("claims", []) or [])
                for item in routes
                for segment in item.get("route_segments", []) or []
                if isinstance(segment, dict)
            )
            generation_status = str(
                manifest_item.get("citation_status")
                or ("generated" if detail else "missing")
            )
            citation_status = str(
                detail.get("citation_status")
                or manifest_item.get("citation_status")
                or "unknown"
            )
            if not detail:
                depth_status = "missing"
            elif route_count == 0:
                depth_status = "empty"
            elif segment_count == 0:
                depth_status = "route_outline"
            elif recommended_segment_count > 0:
                depth_status = "recommended_route"
            else:
                depth_status = "executable_route"
            rows.append(
                {
                    "day": day_no,
                    "poi_name": poi_name,
                    "generation_status": generation_status,
                    "citation_status": citation_status,
                    "content_depth": depth_status,
                    "route_count": route_count,
                    "recommended_route_count": recommended_route_count,
                    "segment_count": segment_count,
                    "claim_count": claim_count,
                }
            )
    return rows


def _source_fingerprint(project_dir: Path, catalog_entry: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(catalog_entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for path in sorted(project_dir.rglob("*")):
        if (
            not path.is_file()
            or path.name == "metrics.json"
            or path.name.startswith("checkpoint.sqlite")
            or path.suffix in {".sqlite", ".sqlite3", ".wal", ".shm"}
        ):
            continue
        digest.update(path.relative_to(project_dir).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _experiment_identity(
    catalog_entry: dict[str, Any],
    experiment_manifest: dict[str, Any],
    post_run_corrections: list[dict[str, Any]],
    *,
    delivery_status: str,
    execution: dict[str, Any],
) -> dict[str, Any]:
    validity_corrections = [
        item
        for item in post_run_corrections
        if item.get("correction_type") == "experiment_validity"
    ]
    latest = validity_corrections[-1] if validity_corrections else {}
    stored_grade = (
        experiment_manifest.get("comparison_grade")
        or catalog_entry.get("comparison_grade")
    )
    declared_grade = stored_grade
    corrected_grade = latest.get("effective_comparison_grade")
    grade_rank = {"A": 0, "B": 1, "C": 2}
    normalized_declared = str(declared_grade or "").upper()
    normalized_corrected = str(corrected_grade or "").upper()
    continuation_runs = list(execution.get("continuation_runs") or [])
    traced_continuation = continuation_runs[-1] if continuation_runs else {}
    continuation_mode = (
        latest.get("continuation_mode")
        or traced_continuation.get("continuation_mode")
    )
    if continuation_runs or latest.get("continuation_mode"):
        effective_grade = "C"
    elif normalized_declared in grade_rank and normalized_corrected in grade_rank:
        effective_grade = max(
            (normalized_declared, normalized_corrected),
            key=lambda value: grade_rank[value],
        )
    else:
        effective_grade = declared_grade
    stored_completion = (
        experiment_manifest.get("completion_mode")
        or catalog_entry.get("completion_mode")
    )
    formal_run = execution.get("formal_run") or {}
    traced_formal_status = str(formal_run.get("status") or "").strip()
    formal_run_status = (
        traced_formal_status
        if traced_formal_status and traced_formal_status != "unknown"
        else stored_completion
    )
    continuation_status = (
        latest.get("continuation_status") or traced_continuation.get("status")
    )
    artifact_status = (
        "deliverable"
        if delivery_status == "passed"
        else "unknown"
        if delivery_status == "unknown"
        else "not_deliverable"
    )
    return {
        "completion_mode": formal_run_status,
        "declared_comparison_grade": declared_grade,
        "comparison_grade": effective_grade,
        "formal_run_status": formal_run_status,
        "continuation_status": continuation_status,
        "continuation_completion_mode": continuation_status,
        "continuation_mode": continuation_mode,
        "artifact_status": artifact_status,
        "experiment_validity": {
            "correction_applied": bool(latest),
            "reason_codes": list(latest.get("reason_codes") or []),
            "capture_mode": latest.get("capture_mode"),
            "recorded_at": latest.get("recorded_at"),
        },
    }


def extract_project_metrics(project_id: str, catalog_entry: dict[str, Any], *, projects_dir: Path) -> dict[str, Any]:
    project_dir = projects_dir / project_id
    workspace = project_dir / "workspace"
    if not workspace.is_dir():
        raise FileNotFoundError(f"Project不存在或缺少workspace: {project_id}")
    requirements = _read_json(workspace / "requirements.json", {}) or {}
    route = _read_json(workspace / "route_plan.json", {}) or {}
    route_entities = _read_json(workspace / "route_entities.json", {}) or {}
    route_execution = _read_json(workspace / "route_execution.json", {}) or {}
    feasibility = _read_json(workspace / "route_feasibility.json", {}) or {}
    lifestyle = _read_json(workspace / "outputs" / "lifestyle.json", {}) or {}
    pitfall = _read_json(workspace / "outputs" / "pitfall.json", {}) or {}
    manifest = _read_json(workspace / "outputs" / "poi_details" / "manifest.json", {}) or {}
    final_meta = _read_json(workspace / "final_guide.meta.json", {}) or {}
    blocks = _read_json(workspace / "final_guide.blocks.json", {}) or {}
    evidence_checks = _read_json(workspace / "evidence_checks.json", {}) or {}
    review_findings = _read_jsonl(workspace / "review_findings.jsonl")
    poi_details = [_read_json(path, {}) or {} for path in sorted((workspace / "outputs" / "poi_details").glob("day*.json"))]
    raw_claims = _raw_claims(route, lifestyle, pitfall, poi_details)
    deliverable = collect_upstream_claims(route, lifestyle, pitfall, poi_details, evidence_checks=evidence_checks, review_findings=review_findings)
    mapped = _mapped_claim_ids(blocks)
    poi_claim_ids = set(_raw_claims({}, {}, {}, poi_details))
    poi_projected = (poi_claim_ids & set(deliverable)) - mapped
    withheld = set(raw_claims) - set(deliverable)
    contract = final_meta.get("contract_validation", {}) if isinstance(final_meta, dict) else {}
    progress = _read_json(workspace / "progress.json", {}) or {}
    route_map = _read_json(workspace / "route_map_validation.json", {}) or {}
    experiment_manifest = _read_json(workspace / "experiment.json", {}) or {}
    post_run_corrections = _read_jsonl(workspace / "post_run_corrections.jsonl")
    validation_runs = _read_jsonl(workspace / "validation_runs.jsonl")
    research_runs = _read_jsonl(workspace / "research_runs.jsonl")
    material_baseline = _read_json(workspace / "material_baseline.json", {}) or {}
    req_body = requirements.get("requirements", {}) if isinstance(requirements, dict) else {}
    route_sha = str(feasibility.get("route_plan_structure_hash") or route_structure_sha256(route)) if route else None
    harness = str(catalog_entry["harness"])
    execution, trace_rows = _execution(project_dir, harness)
    tool_usage, context = _tool_and_context(
        project_dir,
        harness,
        trace_rows,
        model_trace_complete=bool(catalog_entry.get("model_trace_complete")),
    )
    route_days = route.get("days", []) if isinstance(route, dict) else []
    needs_detail = sum(bool(poi.get("needs_detail")) for day in route_days if isinstance(day, dict) for poi in day.get("pois", []) if isinstance(poi, dict))
    poi_routes = sum(len(detail.get("routes", [])) for detail in poi_details)
    poi_segments = sum(len(route_item.get("route_segments", [])) for detail in poi_details for route_item in detail.get("routes", []) if isinstance(route_item, dict))
    poi_depth_rows = _poi_depth_rows(route, manifest, poi_details)
    poi_depth_counts = {
        status: sum(item["content_depth"] == status for item in poi_depth_rows)
        for status in (
            "missing",
            "empty",
            "route_outline",
            "executable_route",
            "recommended_route",
        )
    }
    source_fingerprint = _source_fingerprint(project_dir, catalog_entry)
    completed_events = [row for row in trace_rows if row.get("event") == "run_completed"]
    latest_completion = completed_events[-1] if completed_events else {}
    delivery_status = str(
        contract.get("status")
        or ("passed" if (workspace / "final_guide.md").is_file() else "unknown")
    )
    experiment_identity = _experiment_identity(
        catalog_entry,
        experiment_manifest,
        post_run_corrections,
        delivery_status=delivery_status,
        execution=execution,
    )
    route_risk_status = str(
        route_map.get("status") or feasibility.get("overall_status") or "unknown"
    )
    route_risk_corrections = [
        item
        for item in post_run_corrections
        if item.get("correction_type") == "route_risk_status"
    ]
    if route_risk_corrections:
        route_risk_status = str(route_risk_corrections[-1].get("current_status") or route_risk_status)
    recovery_status = str(latest_completion.get("resume_plan_status") or "unknown")
    # A completed run with a passing delivery contract does not need workflow
    # recovery. Route risk and content degradation remain visible through their
    # separate status dimensions instead of being mislabeled as unfinished work.
    if progress.get("state") == "完成" and delivery_status == "passed":
        recovery_status = "completed"
    else:
        try:
            recovery_status = str(build_resume_plan(project_id).get("status") or recovery_status)
        except Exception:
            pass
    protocol_status = (
        "deviated"
        if "protocol" in str(latest_completion.get("status") or "")
        else "conformant_or_unknown"
    )
    overall_status = "deliverable"
    if delivery_status != "passed":
        overall_status = "not_deliverable"
    elif route_risk_status not in {"passed", "unknown"}:
        overall_status = "deliverable_with_route_confirmation"
    elif recovery_status not in {"completed", "unknown"}:
        overall_status = "deliverable_with_recovery_attention"
    manifest_rows = manifest.get("generated", []) if isinstance(manifest, dict) else []
    successful_poi_entries = sum(
        isinstance(item, dict)
        and item.get("citation_status") not in {"generation_failed", "force_finalized"}
        and (workspace / "outputs" / "poi_details" / str(item.get("file") or "")).is_file()
        for item in manifest_rows
    )
    poi_completeness_status = (
        "not_applicable"
        if needs_detail == 0
        else "passed"
        if successful_poi_entries >= needs_detail
        else "attention_required"
    )
    attention: list[str] = []
    if delivery_status != "passed":
        attention.append("delivery")
    if route_risk_status not in {"passed", "unknown"}:
        attention.append("route_risk")
    if poi_completeness_status == "attention_required":
        attention.append("poi_completeness")
    if recovery_status not in {"completed", "unknown"}:
        attention.append("recovery")
    candidate_evidence_paths = [
        "workspace/requirements.json", "workspace/route_plan.json", "workspace/route_entities.json",
        "workspace/route_execution.json", "workspace/route_map_validation.json", "workspace/route_feasibility.json",
        "workspace/evidence_checks.json", "workspace/outputs/lifestyle.json", "workspace/outputs/pitfall.json",
        "workspace/outputs/poi_details/manifest.json", "workspace/final_guide.blocks.json", "workspace/final_guide.meta.json",
        "workspace/final_guide.md", "workspace/experiment.json", "workspace/validation_runs.jsonl",
        "workspace/post_run_corrections.jsonl",
        "workspace/material_baseline.json", "workspace/research_runs.jsonl",
    ]
    if harness == "codex_skill":
        candidate_evidence_paths.append("agents/codex_skill/trace.jsonl")
    else:
        candidate_evidence_paths.extend(path.relative_to(project_dir).as_posix() for path in sorted(project_dir.glob("agents/**/trace.jsonl")))
        candidate_evidence_paths.extend(path.relative_to(project_dir).as_posix() for path in sorted(project_dir.glob("agents/**/context_events.jsonl")))
    candidate_evidence_paths.extend(catalog_entry.get("evidence_docs") or [])
    evidence_paths = []
    for relative_path in candidate_evidence_paths:
        evidence_base = PROJECT_ROOT if relative_path.startswith("interview/") else project_dir
        if (evidence_base / relative_path).is_file():
            evidence_paths.append(relative_path)
    latest_source_time = max((path.stat().st_mtime for path in project_dir.rglob("*") if path.is_file() and path.name != "metrics.json"), default=0)
    metrics = {
        "identity": {
            "schema_version": SCHEMA_VERSION,
            "is_experiment": True,
            "project_id": project_id,
            "scenario_id": catalog_entry["scenario_id"],
            "experiment_version": catalog_entry.get("experiment_version"),
            "harness": harness,
            "destination": req_body.get("destination") or requirements.get("topic"),
            "trip_days": len(route_days) if route_days else req_body.get("days"),
            "route_sha": route_sha,
            "requirements_sha": hashlib.sha256(json.dumps(req_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() if req_body else None,
            "project_status": progress.get("state"),
            **experiment_identity,
            "model": catalog_entry.get("model"),
            "provider": catalog_entry.get("provider"),
            "inherited_downstream_outputs": catalog_entry.get("inherited_downstream_outputs"),
            "extraction_time": datetime.fromtimestamp(latest_source_time, tz=timezone.utc).isoformat() if latest_source_time else None,
        },
        "outputs": {
            "route_day_count": len(route_days),
            "must_visit_count": sum(len(day.get("pois", [])) for day in route_days if isinstance(day, dict)),
            "needs_detail_target_count": needs_detail,
            "lifestyle_item_count": sum(1 for _ in _iter_items(lifestyle)),
            "pitfall_item_count": sum(1 for _ in _iter_items(pitfall)),
            "integration_block_count": len(blocks.get("blocks", [])),
            "poi_detail_count": len(poi_details),
            "poi_detail_success_count": successful_poi_entries,
            "poi_route_count": poi_routes,
            "poi_segment_count": poi_segments,
            "poi_depth_missing_count": poi_depth_counts["missing"],
            "poi_depth_empty_count": poi_depth_counts["empty"],
            "poi_depth_outline_count": poi_depth_counts["route_outline"],
            "poi_depth_executable_count": poi_depth_counts["executable_route"],
            "poi_depth_recommended_count": poi_depth_counts["recommended_route"],
            "final_guide_exists": (workspace / "final_guide.md").is_file(),
            "site_exists": (project_dir / "presentation" / "site").exists() or (workspace / "site").exists(),
            "poster_exists": any(project_dir.glob("presentation/**/*poster*")),
        },
        "quality_contract": {
            "raw_claim_count": len(raw_claims),
            "deliverable_claim_count": len(deliverable),
            "withheld_claim_count": len(withheld),
            "mapped_unique_claim_count": len(mapped),
            "poi_appendix_projected_claim_count": len(poi_projected),
            "withheld_claim_leak_count": len(withheld & mapped),
            "force_finalized_count": _force_finalized_count([route, feasibility, lifestyle, pitfall, manifest, *poi_details]),
            "final_contract_issue_count": len(contract.get("issues", [])) if isinstance(contract, dict) else 0,
        },
        "route_gate": _route_gate(
            feasibility,
            validation_runs,
            route,
            route_entities,
            route_execution,
        ),
        "research": _research_metrics(research_runs, material_baseline),
        "status": {
            "overall": overall_status,
            "delivery": delivery_status,
            "route_risk": route_risk_status,
            "poi_completeness": poi_completeness_status,
            "recovery": recovery_status,
            "protocol": protocol_status,
            "attention": attention,
        },
        "execution": execution,
        "tool_usage": tool_usage,
        "context": context,
        "stages": _stage_rows(route, feasibility, lifestyle, pitfall, manifest, final_meta, (workspace / "final_guide.md").is_file()),
        "poi_details": poi_depth_rows,
        "evidence_paths": sorted(dict.fromkeys(path for path in evidence_paths if path)),
        "relations": catalog_entry.get("relations") or [],
        "source_fingerprint": source_fingerprint,
    }
    metrics["missing_metrics"] = [
        {"group": group, "field": field, "reason": value["missing_reason"]}
        for group in ("execution", "tool_usage", "context")
        for field, value in metrics[group].items()
        if isinstance(value, dict) and value.get("value_kind") == "unknown"
    ]
    return metrics


def write_metrics(metrics: dict[str, Any], *, projects_dir: Path) -> Path:
    path = projects_dir / metrics["identity"]["project_id"] / "workspace" / "metrics.json"
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
