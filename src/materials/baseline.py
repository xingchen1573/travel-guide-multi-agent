"""Deterministic material and prompt baselines for formal comparisons."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import (
    GPTLOAD_CHAT_MODEL,
    JIJI_CHAT_MODEL,
    LLM_LIGHT_MODEL,
    MATERIAL_LIBRARY_DIR,
    PROJECT_ROOT,
    TRAVEL_MODEL_PROVIDER,
)


SCHEMA_VERSION = "material-baseline-v1"

_ROLE_FILES = {
    "route": (
        "src/agents/route_agent/prompt.md",
        ".agents/skills/travel-guide-codex-orchestrator/references/roles/route.md",
        ".codex/agents/travel-route.toml",
    ),
    "lifestyle": (
        "src/agents/lifestyle_agent/prompt.md",
        ".agents/skills/travel-guide-codex-orchestrator/references/roles/lifestyle.md",
        ".codex/agents/travel-lifestyle.toml",
    ),
    "pitfall": (
        "src/agents/pitfall_agent/prompt.md",
        ".agents/skills/travel-guide-codex-orchestrator/references/roles/pitfall.md",
        ".codex/agents/travel-pitfall.toml",
    ),
    "integration": (
        "src/agents/integration_agent/prompt.md",
        ".agents/skills/travel-guide-codex-orchestrator/references/roles/integration.md",
        ".codex/agents/travel-integration.toml",
    ),
    "poi_detail": (
        "src/agents/poi_detail_agent/prompt.md",
        ".agents/skills/travel-guide-codex-orchestrator/references/roles/poi-detail.md",
        ".codex/agents/travel-poi-detail.toml",
    ),
}


def _structured_judge_boundary() -> dict[str, str]:
    provider = TRAVEL_MODEL_PROVIDER
    return {
        "structured_judge_provider": provider,
        "structured_judge_model": (
            JIJI_CHAT_MODEL if provider == "jiji" else GPTLOAD_CHAT_MODEL
        ),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _jsonl_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def compute_prompt_fingerprint(*, project_root: Path | None = None) -> dict[str, Any]:
    """Hash production prompts and Codex adapters without importing Agent builders."""
    project_root = project_root or PROJECT_ROOT
    per_role: dict[str, Any] = {}
    aggregate_inputs: list[dict[str, str | None]] = []
    for role, relative_paths in _ROLE_FILES.items():
        files = {
            relative_path: _file_sha256(project_root / relative_path)
            for relative_path in relative_paths
        }
        per_role[role] = {
            "aggregate_sha256": _canonical_sha256(files),
            "files": files,
        }
        aggregate_inputs.extend(
            {"path": path, "sha256": sha}
            for path, sha in sorted(files.items())
        )
    return {
        "aggregate_sha256": _canonical_sha256(aggregate_inputs),
        "per_role": per_role,
    }


def compute_material_snapshot(
    topic: str,
    *,
    material_library_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a content-addressed snapshot of one global material namespace."""
    material_library_dir = material_library_dir or MATERIAL_LIBRARY_DIR
    index_dir = material_library_dir / "index" / topic
    raw_dir = material_library_dir / "raw" / topic
    entries_path = index_dir / "entries.jsonl"
    chunks_path = index_dir / "chunks.jsonl"
    rejected_path = index_dir / "rejected.jsonl"
    manifest_path = raw_dir / "manifest.json"

    entries_sha = _file_sha256(entries_path)
    chunks_sha = _file_sha256(chunks_path)
    manifest_sha = _file_sha256(manifest_path)
    entry_count = _jsonl_count(entries_path)
    chunk_count = _jsonl_count(chunks_path)
    index_ready = bool(entries_sha and chunks_sha and entry_count and chunk_count)

    if not manifest_path.is_file():
        sync_status = "missing_raw_manifest"
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_files = {
                str(item.get("source_file") or "")
                for item in (manifest.get("items") or [])
                if isinstance(item, dict) and item.get("source_file")
            }
            processed_files: set[str] = set()
            for path in (entries_path, rejected_path):
                if not path.is_file():
                    continue
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    source_file = str(item.get("source_file") or "")
                    if source_file:
                        processed_files.add(source_file)
            sync_status = "synced" if manifest_files <= processed_files else "out_of_sync"
        except (OSError, json.JSONDecodeError, TypeError):
            sync_status = "invalid_raw_manifest"

    identity = {
        "topic": topic,
        "entries_sha256": entries_sha,
        "chunks_sha256": chunks_sha,
        "raw_manifest_sha256": manifest_sha,
    }
    return {
        "index_status": "ready" if index_ready else "empty",
        "sync_status": sync_status,
        "entry_count": entry_count,
        "chunk_count": chunk_count,
        **identity,
        "corpus_sha256": _canonical_sha256(identity),
    }


def build_material_baseline(
    topic: str,
    destination: str,
    *,
    material_library_dir: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    snapshot = compute_material_snapshot(
        topic,
        material_library_dir=material_library_dir,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "topic": topic,
        "destination": destination,
        **snapshot,
        "prompt_fingerprint": compute_prompt_fingerprint(project_root=project_root),
        "model_boundary": {
            **_structured_judge_boundary(),
            "light_filter_model": LLM_LIGHT_MODEL,
            "codex_host_model": "host_managed",
        },
        "frozen_at": _now(),
    }


def validate_material_baseline(
    baseline: Any,
    *,
    topic: str,
    destination: str,
    material_library_dir: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Compare a frozen sidecar with the current repository and corpus state."""
    issues: list[dict[str, Any]] = []
    if topic != destination:
        issues.append(
            {
                "code": "material_topic_mismatch",
                "expected": destination,
                "actual": topic,
            }
        )
    if not isinstance(baseline, dict):
        issues.append({"code": "material_baseline_missing"})
        return {"status": "failed", "issues": issues, "current": None}
    if baseline.get("schema_version") != SCHEMA_VERSION:
        issues.append(
            {
                "code": "material_baseline_invalid",
                "expected_schema_version": SCHEMA_VERSION,
                "actual_schema_version": baseline.get("schema_version"),
            }
        )
    if baseline.get("topic") != topic or baseline.get("destination") != destination:
        issues.append(
            {
                "code": "material_baseline_namespace_changed",
                "expected": {
                    "topic": baseline.get("topic"),
                    "destination": baseline.get("destination"),
                },
                "actual": {"topic": topic, "destination": destination},
            }
        )

    current_snapshot = compute_material_snapshot(
        topic,
        material_library_dir=material_library_dir,
    )
    if current_snapshot["index_status"] != "ready":
        issues.append({"code": "material_index_empty"})
    if current_snapshot["sync_status"] != "synced":
        issues.append(
            {
                "code": "material_index_not_synced",
                "sync_status": current_snapshot["sync_status"],
            }
        )
    for field in (
        "entries_sha256",
        "chunks_sha256",
        "raw_manifest_sha256",
        "corpus_sha256",
    ):
        if baseline.get(field) != current_snapshot.get(field):
            issues.append(
                {
                    "code": "material_corpus_changed",
                    "field": field,
                    "expected": baseline.get(field),
                    "actual": current_snapshot.get(field),
                }
            )

    current_prompt = compute_prompt_fingerprint(project_root=project_root)
    expected_prompt = baseline.get("prompt_fingerprint") or {}
    if expected_prompt.get("aggregate_sha256") != current_prompt.get("aggregate_sha256"):
        issues.append(
            {
                "code": "prompt_fingerprint_changed",
                "expected": expected_prompt.get("aggregate_sha256"),
                "actual": current_prompt.get("aggregate_sha256"),
            }
        )
    expected_models = baseline.get("model_boundary") or {}
    current_models = {
        **_structured_judge_boundary(),
        "light_filter_model": LLM_LIGHT_MODEL,
        "codex_host_model": "host_managed",
    }
    for field in ("structured_judge_model", "light_filter_model"):
        if expected_models.get(field) != current_models.get(field):
            issues.append(
                {
                    "code": "model_boundary_changed",
                    "field": field,
                    "expected": expected_models.get(field),
                    "actual": current_models.get(field),
                }
            )
    # Legacy baselines did not record the Provider. Keep them readable; every
    # newly frozen baseline records and validates the explicit group.
    if (
        expected_models.get("structured_judge_provider") is not None
        and expected_models.get("structured_judge_provider")
        != current_models.get("structured_judge_provider")
    ):
        issues.append(
            {
                "code": "model_boundary_changed",
                "field": "structured_judge_provider",
                "expected": expected_models.get("structured_judge_provider"),
                "actual": current_models.get("structured_judge_provider"),
            }
        )
    return {
        "status": "passed" if not issues else "failed",
        "issues": issues,
        "current": {
            **current_snapshot,
            "prompt_fingerprint": current_prompt,
            "model_boundary": current_models,
        },
    }
