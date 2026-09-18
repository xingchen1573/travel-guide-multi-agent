"""Shared material retrieval and auditable research coverage."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.materials.baseline import compute_material_snapshot
from src.materials.project_evidence import retrieve_project_evidence
from src.materials.retrieve import retrieve_materials_union
from src.workspace import store


class StrictEmbeddingRuntimeError(RuntimeError):
    """Raised after ledger persistence when a formal Codex Grade A run loses Embedding."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_context(project_id: str) -> tuple[str, str, str]:
    saved = store.read_requirements(project_id)
    if not isinstance(saved, dict):
        raise FileNotFoundError(f"project {project_id!r} 缺少 requirements.json")
    topic = str(saved.get("topic") or "").strip()
    requirements = saved.get("requirements") or {}
    destination = str(requirements.get("destination") or "").strip()
    if not topic or topic != destination:
        raise ValueError(
            f"material_topic_mismatch: topic={topic!r}, destination={destination!r}"
        )
    manifest = store.read_workspace(project_id, "experiment.json") or {}
    harness = str(manifest.get("harness") or "unknown")
    return topic, destination, harness


def _canonical_ref(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": result.get("platform", ""),
        "doc_id": result.get("doc_id", ""),
        "chunk_id": result.get("chunk_id", ""),
        "scope": result.get("scope", ""),
        **(
            {"source_url": result.get("source_url")}
            if result.get("source_url")
            else {}
        ),
    }


def _strict_codex_grade_a(project_id: str) -> bool:
    manifest = store.read_workspace(project_id, "experiment.json") or {}
    if not isinstance(manifest, dict):
        return False
    return (
        str(manifest.get("harness") or "") == "codex_skill"
        and str(manifest.get("comparison_grade") or "").upper() == "A"
        and not _codex_run_is_continuation(project_id)
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _codex_run_is_continuation(project_id: str) -> bool:
    """Treat a contaminated/diagnostic continuation as effective Grade C."""
    workspace = store.workspace_dir(project_id)
    corrections = _read_jsonl(workspace / "post_run_corrections.jsonl")
    validity_corrections = [
        row
        for row in corrections
        if row.get("correction_type") == "experiment_validity"
    ]
    if validity_corrections:
        latest = validity_corrections[-1]
        if latest.get("continuation_mode"):
            return True

    trace_path = (
        store.project_dir(project_id) / "agents" / "codex_skill" / "trace.jsonl"
    )
    active_start: dict[str, Any] | None = None
    for row in _read_jsonl(trace_path):
        event = str(row.get("event") or "")
        if event == "run_start":
            active_start = row
        elif event == "run_completed":
            active_start = None
    if not active_start:
        return False
    markers = " ".join(
        str(active_start.get(field) or "").lower()
        for field in (
            "continuation_mode",
            "status",
            "stage",
            "summary",
            "capture_mode",
        )
    )
    return any(marker in markers for marker in ("continuation", "diagnostic"))


def _embedding_systemically_unavailable(diagnostics: dict[str, Any]) -> bool:
    errors = diagnostics.get("modality_errors") or {}
    available = {
        str(name) for name in (diagnostics.get("available_modalities") or [])
    }
    return isinstance(errors, dict) and "embedding" in errors and "embedding" not in available


def retrieve_context(
    project_id: str,
    *,
    actor: str,
    stage: str,
    target_key: str,
    query: str,
    top_k: int = 8,
    target_sha256: str | None = None,
    target_entity: str | None = None,
) -> dict[str, Any]:
    """Search the unified UGC pool and append a research ledger row."""
    if not query.strip():
        raise ValueError("query不能为空")
    if top_k <= 0 or top_k > 20:
        raise ValueError("top_k必须在1到20之间")
    topic, _destination, harness = _project_context(project_id)
    started_at = _now()
    base = store.read_workspace(project_id, "material_baseline.json") or {}
    overlay_dir = store.project_material_library_dir(project_id)
    effective_query = query.strip()
    normalized_target = (target_entity or "").strip()
    if normalized_target and normalized_target not in effective_query:
        effective_query = f"{normalized_target} {effective_query}"
    results, diagnostics = retrieve_materials_union(
        topic,
        effective_query,
        top_k,
        scopes=[
            ("global_core", None),
            ("project_supplement", overlay_dir),
        ],
    )
    status = diagnostics["status"]

    completed_at = _now()
    result_scopes = diagnostics.get("result_scopes") or {
        "global_core": 0,
        "project_supplement": 0,
    }
    entry = {
        "schema_version": "research-run-v1",
        "research_id": uuid.uuid4().hex,
        "project_id": project_id,
        "harness": harness,
        "actor": actor,
        "stage": stage,
        "target_key": target_key,
        **({"target_sha256": target_sha256} if target_sha256 else {}),
        "query": effective_query,
        **({"requested_query": query} if effective_query != query else {}),
        "source_kind": "material",
        "channel": "ugc",
        "topic": topic,
        "base_corpus_sha256": base.get("corpus_sha256"),
        "overlay_sha256": compute_material_snapshot(
            topic, material_library_dir=overlay_dir
        )["corpus_sha256"],
        "result_count": len(results),
        "result_scopes": result_scopes,
        "retrieved_evidence_refs": [_canonical_ref(result) for result in results],
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        **(
            {"scope_errors": {"ugc": diagnostics["modality_errors"]}}
            if diagnostics.get("modality_errors")
            else {}
        ),
        "retrieval_diagnostics": diagnostics,
    }
    strict_embedding_block = (
        _strict_codex_grade_a(project_id)
        and _embedding_systemically_unavailable(diagnostics)
    )
    if strict_embedding_block:
        entry["technical_block"] = {
            "status": "blocked",
            "code": "grade_a_embedding_runtime_unavailable",
            "modality": "embedding",
            "available_modalities": list(diagnostics.get("available_modalities") or []),
            "message": (
                "Grade A正式运行的Embedding通道在预检后系统性失效；"
                "已保留BM25结果与Research Ledger，但本工作单元不能继续。"
            ),
        }
    store.append_research_run(project_id, entry)
    if strict_embedding_block:
        raise StrictEmbeddingRuntimeError("grade_a_embedding_runtime_unavailable")
    return {
        "research": entry,
        "results": [
            {**result, "evidence_ref": _canonical_ref(result)} for result in results
        ],
    }


def retrieve_project_evidence_context(
    project_id: str,
    *,
    actor: str,
    stage: str,
    target_key: str,
    log_scope: str,
    query: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """Explicitly search citable Web evidence owned by the current work unit."""
    if not query.strip():
        raise ValueError("query不能为空")
    if not log_scope.strip():
        raise ValueError("Project Evidence检索必须提供log_scope")
    if top_k <= 0 or top_k > 20:
        raise ValueError("top_k必须在1到20之间")
    topic, _destination, harness = _project_context(project_id)
    started_at = _now()
    try:
        results = retrieve_project_evidence(
            project_id,
            query,
            top_k,
            log_scope=log_scope,
        )
        status = "success" if results else "empty"
        error = None
    except Exception as exc:
        results = []
        status = "error"
        error = {"type": type(exc).__name__, "message": str(exc)}
    completed_at = _now()
    entry = {
        "schema_version": "research-run-v1",
        "research_id": uuid.uuid4().hex,
        "project_id": project_id,
        "harness": harness,
        "actor": actor,
        "stage": stage,
        "target_key": target_key,
        "query": query,
        "source_kind": "project_evidence",
        "channel": "web_evidence",
        "topic": topic,
        "result_count": len(results),
        "result_scopes": {"project_evidence": len(results)},
        "retrieved_evidence_refs": [_canonical_ref(result) for result in results],
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "metadata": {"log_scope": log_scope},
        **({"error": error} if error else {}),
    }
    store.append_research_run(project_id, entry)
    return {
        "research": entry,
        "results": [
            {**result, "evidence_ref": _canonical_ref(result)} for result in results
        ],
    }


def record_external_research(
    project_id: str,
    *,
    actor: str,
    stage: str,
    target_key: str,
    query: str,
    source_kind: str,
    status: str,
    result_count: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record Web/Amap/supplement calls performed by a shared adapter."""
    topic, _destination, harness = _project_context(project_id)
    now = _now()
    base = store.read_workspace(project_id, "material_baseline.json") or {}
    overlay_dir = store.project_material_library_dir(project_id)
    channel = {
        "web": "web",
        "project_evidence": "web_evidence",
        "supplement": "ugc_supplement",
        "amap": "amap",
    }.get(source_kind, source_kind)
    entry = {
        "schema_version": "research-run-v1",
        "research_id": uuid.uuid4().hex,
        "project_id": project_id,
        "harness": harness,
        "actor": actor,
        "stage": stage,
        "target_key": target_key,
        "query": query,
        "source_kind": source_kind,
        "channel": channel,
        "topic": topic,
        "base_corpus_sha256": base.get("corpus_sha256"),
        "overlay_sha256": compute_material_snapshot(
            topic, material_library_dir=overlay_dir
        )["corpus_sha256"],
        "result_count": result_count,
        "result_scopes": {},
        "status": status,
        "started_at": now,
        "completed_at": now,
        "metadata": metadata or {},
    }
    store.append_research_run(project_id, entry)
    return entry


def research_status(project_id: str) -> dict[str, Any]:
    runs = store.read_research_runs(project_id)
    targets: dict[str, dict[str, Any]] = {}
    source_kind_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    scope_hit_counts: dict[str, int] = {}
    for run in runs:
        target = str(run.get("target_key") or "unknown")
        item = targets.setdefault(
            target,
            {
                "call_count": 0,
                "result_count": 0,
                "statuses": {},
                "source_kinds": {},
                "channels": {},
                "result_scopes": {},
                "latest_completed_at": None,
            },
        )
        item["call_count"] += 1
        item["result_count"] += int(run.get("result_count") or 0)
        status = str(run.get("status") or "unknown")
        source_kind = str(run.get("source_kind") or "unknown")
        channel = str(run.get("channel") or source_kind)
        item["statuses"][status] = item["statuses"].get(status, 0) + 1
        item["source_kinds"][source_kind] = item["source_kinds"].get(source_kind, 0) + 1
        source_kind_counts[source_kind] = source_kind_counts.get(source_kind, 0) + 1
        item["channels"][channel] = item["channels"].get(channel, 0) + 1
        channel_counts[channel] = channel_counts.get(channel, 0) + 1
        for scope, count in (run.get("result_scopes") or {}).items():
            item["result_scopes"][scope] = item["result_scopes"].get(scope, 0) + int(count or 0)
            scope_hit_counts[scope] = scope_hit_counts.get(scope, 0) + int(count or 0)
        item["latest_completed_at"] = run.get("completed_at")
    return {
        "schema_version": "research-status-v1",
        "project_id": project_id,
        "run_count": len(runs),
        "source_kind_counts": source_kind_counts,
        "channel_counts": channel_counts,
        "scope_hit_counts": scope_hit_counts,
        "targets": targets,
    }


def research_coverage_issues(
    project_id: str,
    stage: str,
    *,
    day: int | None = None,
    poi_name: str | None = None,
) -> list[dict[str, Any]]:
    """Verify that required work units have material observations."""
    runs = [
        run
        for run in store.read_research_runs(project_id)
        if run.get("source_kind") == "material"
        and run.get("status") in {"success", "empty", "degraded"}
    ]
    if stage == "lifestyle":
        route = store.read_workspace(project_id, "route_plan.json") or {}
        days = [
            int(item.get("day"))
            for item in route.get("days") or []
            if isinstance(item, dict) and isinstance(item.get("day"), int)
        ]
        missing = [
            value
            for value in days
            if not any(
                run.get("stage") == "lifestyle"
                and str(run.get("target_key") or "")
                in {
                    f"lifestyle:day:{value}",
                    f"lifestyle:food:day:{value}",
                    f"lifestyle:outfit:day:{value}",
                    f"lifestyle:lodging:day:{value}",
                    f"lifestyle:play:day:{value}",
                }
                for run in runs
            )
        ]
        return (
            [
                {
                    "code": "lifestyle_research_missing",
                    "message": "Lifestyle存在未执行素材检索的日级工作单元",
                    "missing_days": missing,
                }
            ]
            if missing
            else []
        )

    if stage == "pitfall":
        lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
        missing: list[list[Any]] = []
        stale: list[list[Any]] = []
        for field in ("food", "outfit", "lodging", "play"):
            for item in (lifestyle.get(field) or {}).get("items", []) or []:
                item_day = item.get("day")
                recommendation = str(item.get("recommendation") or "")
                expected_sha = hashlib.sha256(recommendation.encode("utf-8")).hexdigest()
                target = f"pitfall:{field}:day:{item_day}"
                matching = [
                    run
                    for run in runs
                    if run.get("stage") == "pitfall"
                    and run.get("target_key") == target
                ]
                if not matching:
                    missing.append([field, item_day])
                elif not any(run.get("target_sha256") == expected_sha for run in matching):
                    stale.append([field, item_day])
        return (
            [
                {
                    "code": "pitfall_research_missing",
                    "message": "Pitfall空结果不能证明已逐项检索当前Lifestyle推荐",
                    "missing": missing,
                    "stale": stale,
                }
            ]
            if missing or stale
            else []
        )

    if stage == "poi_detail":
        if day is None or not poi_name:
            return [{"code": "poi_detail_research_missing"}]
        target = f"poi_detail:day:{day}:poi:{poi_name}"
        if not any(
            run.get("stage") == "poi_detail" and run.get("target_key") == target
            for run in runs
        ):
            return [
                {
                    "code": "poi_detail_research_missing",
                    "message": f"Day{day} {poi_name}没有可见素材检索记录",
                }
            ]
    return []
