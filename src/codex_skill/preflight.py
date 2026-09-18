"""Preflight checks for Codex-native travel-guide runs."""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import Any

import requests

from src.api import key_store
from src.codex_skill.bridge import (
    _manifest_claims_route_only,
    _route_only_contract_issues,
    _route_only_downstream_artifacts,
    route_structure_sha256,
)
from src.config import AMAP_GEOCODE_API_KEY, RUNTIME_DIR, get_embeddings
from src.materials.baseline import validate_material_baseline
from src.tools.geocode import search_poi_candidates
from travel_guide_kernel.models import RouteEntities, RouteExecutionPlan
from travel_guide_kernel.workspace import store

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_EMBEDDING_PROBE_TEXT = "Codex travel guide embedding preflight probe"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _probe_web() -> dict:
    keys = key_store.list_keys("tavily")
    if not keys:
        return {"status": "failed", "reason": "tavily_key_missing", "attempts": 0}
    errors: list[str] = []
    for index, key in enumerate(keys, start=1):
        try:
            response = requests.post(
                _TAVILY_SEARCH_URL,
                json={
                    "api_key": key,
                    "query": "兰州西站 官方",
                    "search_depth": "basic",
                    "max_results": 1,
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            errors.append(type(exc).__name__)
            continue
        if response.ok and (response.json().get("results") or []):
            return {"status": "passed", "attempts": index, "http_status": response.status_code}
        errors.append(f"http_{response.status_code}")
    return {"status": "failed", "reason": "all_tavily_keys_unavailable", "attempts": len(keys), "errors": errors}


def _probe_amap() -> dict:
    if not AMAP_GEOCODE_API_KEY:
        return {"status": "failed", "reason": "amap_geocode_key_missing", "attempts": 0}
    candidates = search_poi_candidates("兰州西站", city="兰州")
    if not candidates:
        return {"status": "failed", "reason": "amap_candidate_probe_empty", "attempts": 1}
    first = candidates[0]
    return {
        "status": "passed",
        "attempts": 1,
        "sample": {
            "provider_id": first.get("provider_id"),
            "provider_name": first.get("provider_name"),
            "city": first.get("city"),
        },
    }


def _probe_embedding() -> dict:
    """Verify the configured Embedding channel without logging request secrets or vectors."""
    started_at = time.perf_counter()
    try:
        vector = get_embeddings().embed_query(_EMBEDDING_PROBE_TEXT)
    except Exception as exc:
        error_type = type(exc).__name__
        config_missing = isinstance(exc, RuntimeError) and str(exc).startswith(
            "缺少embedding配置"
        )
        return {
            "status": "failed",
            "reason": "embedding_config_missing" if config_missing else "embedding_probe_error",
            "attempts": 0 if config_missing else 1,
            "duration_ms": max(0, round((time.perf_counter() - started_at) * 1000)),
            "error_type": error_type,
            "error_summary": (
                "Embedding configuration is incomplete"
                if config_missing
                else f"Embedding probe failed ({error_type})"
            ),
        }

    duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
    if not isinstance(vector, (list, tuple)) or not vector:
        return {
            "status": "failed",
            "reason": "embedding_probe_empty_vector",
            "attempts": 1,
            "duration_ms": duration_ms,
            "error_type": "ValueError",
            "error_summary": "Embedding probe returned an empty vector",
        }
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in vector):
        return {
            "status": "failed",
            "reason": "embedding_probe_non_numeric_vector",
            "attempts": 1,
            "duration_ms": duration_ms,
            "error_type": "TypeError",
            "error_summary": "Embedding probe returned a non-numeric vector",
        }
    if not all(math.isfinite(float(value)) for value in vector):
        return {
            "status": "failed",
            "reason": "embedding_probe_non_finite_vector",
            "attempts": 1,
            "duration_ms": duration_ms,
            "error_type": "ValueError",
            "error_summary": "Embedding probe returned a non-finite vector",
        }
    return {
        "status": "passed",
        "dimensions": len(vector),
        "attempts": 1,
        "duration_ms": duration_ms,
    }


def _material_topic_check(requirements: Any) -> dict:
    requirements_payload = (
        requirements.get("requirements")
        if isinstance(requirements, dict)
        else {}
    )
    if not isinstance(requirements_payload, dict):
        requirements_payload = {}
    topic = str((requirements or {}).get("topic") or "").strip() if isinstance(requirements, dict) else ""
    destination = str(requirements_payload.get("destination") or "").strip()
    if not topic or topic != destination:
        return {
            "status": "failed",
            "reason": "material_topic_mismatch",
            "topic": topic,
            "destination": destination,
        }
    return {
        "status": "passed",
        "topic": topic,
        "destination": destination,
    }


def _route_only_target_check(
    source_project_id: str,
    target_project_id: str,
    *,
    allow_existing_target: bool,
) -> dict:
    target_exists = store.project_dir(target_project_id).exists()
    base = {
        "exists": target_exists,
        "allow_existing": allow_existing_target,
    }
    if not target_exists:
        return {"status": "passed", **base}
    if not allow_existing_target:
        return {"status": "failed", "reason": "target_exists", **base}

    manifest = store.read_workspace(target_project_id, "experiment.json")
    source_id, issues = _route_only_contract_issues(
        target_project_id,
        manifest,
        expected_source_project_id=source_project_id,
    )
    if not _manifest_claims_route_only(manifest):
        return {
            "status": "failed",
            "reason": "route_only_manifest_invalid",
            **base,
        }
    issue_codes = {str(item.get("code") or "") for item in issues}
    downstream_artifacts = _route_only_downstream_artifacts(target_project_id)
    if "source_relation_mismatch" in issue_codes:
        reason = "source_relation_mismatch"
    elif "route_only_requirements_mismatch" in issue_codes:
        reason = "requirements_mismatch"
    elif issues:
        reason = "route_only_manifest_invalid"
    elif downstream_artifacts:
        return {
            "status": "failed",
            "reason": "route_only_downstream_artifacts_present",
            "downstream_artifacts": downstream_artifacts,
            **base,
        }
    else:
        return {
            "status": "passed",
            "source_project_id": source_id,
            **base,
        }
    return {
        "status": "failed",
        "reason": reason,
        "issue_codes": sorted(issue_codes),
        **base,
    }


def _comparison_sidecar_check(
    source_project_id: str,
    filename: str,
    route_sha: str | None,
) -> dict:
    payload = store.read_workspace(source_project_id, filename)
    if not isinstance(payload, dict):
        return {"status": "missing", "reason": "comparison_sidecar_missing"}
    try:
        if filename == "route_entities.json":
            model = RouteEntities.model_validate(payload)
            sidecar_sha = model.route_plan_structure_hash
        elif filename == "route_execution.json":
            model = RouteExecutionPlan.model_validate(payload)
            sidecar_sha = model.route_plan_structure_hash
        else:
            sidecar_sha = str(payload.get("route_structure_sha256") or "")
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "comparison_sidecar_invalid",
            "error_type": type(exc).__name__,
        }
    if sidecar_sha and route_sha and sidecar_sha != route_sha:
        return {
            "status": "failed",
            "reason": "comparison_sidecar_stale",
            "route_structure_sha256": sidecar_sha,
        }
    return {
        "status": "passed",
        **({"route_structure_sha256": sidecar_sha} if sidecar_sha else {}),
    }


def run_fixed_route_preflight(
    source_project_id: str,
    target_project_id: str,
    *,
    live: bool = False,
    allow_existing_target: bool = False,
    require_comparison_sidecars: bool = False,
    require_embedding: bool = False,
    persist: bool = True,
) -> dict:
    source_exists = store.project_dir(source_project_id).is_dir()
    target_exists = store.project_dir(target_project_id).exists()
    requirements = store.read_requirements(source_project_id) if source_exists else None
    route_plan = store.read_workspace(source_project_id, "route_plan.json") if source_exists else None
    route_sha = route_structure_sha256(route_plan) if isinstance(route_plan, dict) else None
    requirements_payload = (
        requirements.get("requirements")
        if isinstance(requirements, dict)
        else {}
    )
    if not isinstance(requirements_payload, dict):
        requirements_payload = {}
    topic = str((requirements or {}).get("topic") or "") if isinstance(requirements, dict) else ""
    destination = str(requirements_payload.get("destination") or "")
    material_baseline = store.read_workspace(source_project_id, "material_baseline.json")
    baseline_check = validate_material_baseline(
        material_baseline,
        topic=topic,
        destination=destination,
    )
    checks = {
        "source_project": {"status": "passed" if source_exists else "failed"},
        "target_project": {
            "status": "passed" if allow_existing_target or not target_exists else "failed",
            "exists": target_exists,
            "allow_existing": allow_existing_target,
        },
        "requirements": {"status": "passed" if isinstance(requirements, dict) else "failed"},
        "route_plan": {"status": "passed" if isinstance(route_plan, dict) else "failed"},
        "fixed_route_input_snapshot": _comparison_sidecar_check(
            source_project_id,
            "fixed_route_input_snapshot.json",
            route_sha,
        ),
        "route_entities": _comparison_sidecar_check(
            source_project_id,
            "route_entities.json",
            route_sha,
        ),
        "route_execution": _comparison_sidecar_check(
            source_project_id,
            "route_execution.json",
            route_sha,
        ),
        "material_baseline": baseline_check,
        "web": _probe_web() if live else {"status": "not_run", "reason": "offline_preflight"},
        "amap": _probe_amap() if live else {"status": "not_run", "reason": "offline_preflight"},
        "codex_model": {
            "status": "host_managed",
            "reason": "Codex task model credentials and usage are not exposed to repository code",
        },
    }
    if require_embedding:
        checks["embedding"] = (
            _probe_embedding()
            if live
            else {"status": "not_run", "reason": "offline_preflight"}
        )
    required_names = ["source_project", "target_project", "requirements", "route_plan"]
    if require_comparison_sidecars:
        required_names.extend(
            [
                "fixed_route_input_snapshot",
                "route_entities",
                "route_execution",
                "material_baseline",
            ]
        )
    if live:
        required_names.extend(["web", "amap"])
        if require_embedding:
            required_names.append("embedding")
    ready = all(checks[name]["status"] == "passed" for name in required_names)
    report = {
        "schema_version": "codex-fixed-route-preflight-v2",
        "mode": "fixed_route",
        "source_project_id": source_project_id,
        "target_project_id": target_project_id,
        "observation_mode": "live" if live else "offline",
        "comparison_sidecars_required": require_comparison_sidecars,
        "embedding_required": require_embedding,
        "required_checks": required_names,
        "checked_at": _now(),
        "ready": ready,
        "requirements_sha256": _sha(requirements) if isinstance(requirements, dict) else None,
        "route_structure_sha256": route_sha,
        "checks": checks,
    }
    if persist:
        path = RUNTIME_DIR / "preflight" / f"{target_project_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path.relative_to(RUNTIME_DIR.parent))
    return report


def run_route_only_preflight(
    source_project_id: str,
    target_project_id: str,
    *,
    live: bool = False,
    allow_existing_target: bool = False,
    persist: bool = True,
) -> dict:
    """Check dependencies for a requirements-only Codex route-generation run."""
    source_exists = store.project_dir(source_project_id).is_dir()
    requirements = store.read_requirements(source_project_id) if source_exists else None
    checks = {
        "source_project": {"status": "passed" if source_exists else "failed"},
        "target_project": _route_only_target_check(
            source_project_id,
            target_project_id,
            allow_existing_target=allow_existing_target,
        ),
        "requirements": {"status": "passed" if isinstance(requirements, dict) else "failed"},
        "material_topic": _material_topic_check(requirements),
        "web": _probe_web() if live else {"status": "not_run", "reason": "offline_preflight"},
        "amap": _probe_amap() if live else {"status": "not_run", "reason": "offline_preflight"},
        "codex_model": {
            "status": "host_managed",
            "reason": "Codex task model credentials and usage are not exposed to repository code",
        },
    }
    required_names = ["source_project", "target_project", "requirements", "material_topic"]
    if live:
        required_names.extend(["web", "amap"])
    ready = all(checks[name]["status"] == "passed" for name in required_names)
    report = {
        "schema_version": "codex-full-plan-route-preflight-v1",
        "mode": "full_plan_route_only",
        "source_project_id": source_project_id,
        "target_project_id": target_project_id,
        "observation_mode": "live" if live else "offline",
        "required_checks": required_names,
        "checked_at": _now(),
        "ready": ready,
        "requirements_sha256": _sha(requirements) if isinstance(requirements, dict) else None,
        "route_structure_sha256": None,
        "checks": checks,
    }
    if persist:
        path = RUNTIME_DIR / "preflight" / f"{target_project_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path.relative_to(RUNTIME_DIR.parent))
    return report
