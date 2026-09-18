"""Optimization Candidate 的最小持续观察闭环。

当前只观察确定性状态扫描器已经能识别的三个问题类型。观察单位固定为一个
已完成项目的产物 revision；窗口配置来自一次成功的定向回归，不提供通用 detector、
多 exposure unit 或可编辑策略层。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.review import index as review_index
from src.review import regression as review_regression
from src.review import store as review_store
from src.review.models import MonitoringObservation, MonitoringWindow
from src.workspace import store as workspace_store


_LOCK = threading.RLock()
_CHECKERS: dict[str, dict[str, Any]] = {
    "manifest_mismatch": {
        "checker_id": "state_scanner:manifest_mismatch",
        "required_outputs": ["outputs/poi_details/manifest.json"],
        "problem_types": ["manifest_mismatch"],
    },
    "force_finalized_residue": {
        "checker_id": "state_scanner:force_finalized_residue",
        "required_outputs": [
            "route_plan.json",
            "outputs/lifestyle.json",
            "outputs/pitfall.json",
            "outputs/poi_details/manifest.json",
            "final_guide.meta.json",
        ],
        "problem_types": ["force_finalized_residue"],
    },
    "final_guide_contract_violation": {
        "checker_id": "state_scanner:final_guide_contract_violation",
        "required_outputs": ["final_guide.meta.json"],
        "problem_types": ["final_guide_contract_violation"],
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _index_dir() -> Path:
    return workspace_store.PROJECTS_DIR.parent / "runtime" / "review_index"


def _windows_path() -> Path:
    return _index_dir() / "monitoring_windows.jsonl"


def _observations_path() -> Path:
    return _index_dir() / "monitoring_observations.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
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


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _checker_for_problem(problem_type: str) -> dict[str, Any] | None:
    return _CHECKERS.get(problem_type)


def _candidate_problem_type(candidate: dict[str, Any]) -> str:
    cause_key = str(candidate.get("cause_key") or "")
    return str(candidate.get("problem_type") or cause_key.rsplit(":", 1)[-1].rsplit(".", 1)[-1])


def supports_candidate(candidate: dict[str, Any]) -> bool:
    return _checker_for_problem(_candidate_problem_type(candidate)) is not None


def _checker_by_id(checker_id: str) -> dict[str, Any] | None:
    return next(
        (value for value in _CHECKERS.values() if value["checker_id"] == checker_id),
        None,
    )


def checker_version(checker_id: str) -> str:
    checker = _checker_by_id(checker_id)
    if checker is None:
        return ""
    digest = hashlib.sha256()
    digest.update(json.dumps(checker, sort_keys=True).encode("utf-8"))
    for function in (review_store._state_issue_explanation, review_store._canonical_state_observations):
        digest.update(inspect.getsource(function).encode("utf-8"))
    return digest.hexdigest()


@lru_cache(maxsize=1)
def current_release_fingerprints() -> dict[str, str]:
    return review_regression.compute_release_fingerprints()


def read_monitoring_windows(candidate_id: str | None = None) -> list[MonitoringWindow]:
    windows: list[MonitoringWindow] = []
    for raw in _read_jsonl(_windows_path()):
        try:
            window = MonitoringWindow.model_validate(raw)
        except ValueError:
            continue
        if candidate_id is None or window.candidate_id == candidate_id:
            windows.append(window)
    return sorted(windows, key=lambda item: (item.created_at, item.window_id))


def active_monitoring_window(candidate_id: str) -> MonitoringWindow | None:
    windows = read_monitoring_windows(candidate_id)
    return windows[-1] if windows else None


def active_monitoring_windows() -> list[MonitoringWindow]:
    latest: dict[str, MonitoringWindow] = {}
    for window in read_monitoring_windows():
        latest[window.candidate_id] = window
    return list(latest.values())


def _validated_regression_fingerprints(candidate_id: str, run_id: str) -> dict[str, str]:
    run = review_regression.read_regression_run(run_id)
    if run is None:
        raise ValueError("Monitoring Window 必须关联当前候选的 Regression Run")
    evidence = review_regression.validate_regression_run_evidence(candidate_id, run)
    return {"code": evidence["code"], "prompt": evidence["prompt"]}


def create_monitoring_window(
    candidate: dict[str, Any],
    source_regression_run_id: str,
    *,
    minimum_elapsed_days: int = 14,
    minimum_samples: int | None = None,
    actor: str = "system",
    started_at: str | None = None,
) -> MonitoringWindow:
    candidate_id = str(candidate.get("candidate_id") or "")
    problem_type = _candidate_problem_type(candidate)
    checker = _checker_for_problem(problem_type)
    if not candidate_id or checker is None:
        raise ValueError(f"当前候选没有内置 Monitoring checker: {problem_type or candidate_id}")
    with _LOCK:
        existing = next(
            (
                window
                for window in read_monitoring_windows(candidate_id)
                if window.source_regression_run_id == source_regression_run_id
            ),
            None,
        )
        if existing is not None:
            return existing
    fingerprints = _validated_regression_fingerprints(candidate_id, source_regression_run_id)
    now = _now()
    window = MonitoringWindow(
        window_id=f"monitoring-window-{uuid.uuid4().hex[:16]}",
        candidate_id=candidate_id,
        source_regression_run_id=source_regression_run_id,
        checker_id=checker["checker_id"],
        checker_version=checker_version(checker["checker_id"]),
        code_fingerprint=fingerprints["code"],
        prompt_fingerprint=fingerprints["prompt"],
        started_at=started_at or now,
        minimum_elapsed_days=minimum_elapsed_days,
        minimum_samples=minimum_samples or (10 if candidate.get("severity") == "high" else 5),
        created_by=actor,
        created_at=now,
    )
    with _LOCK:
        existing = next(
            (
                item
                for item in read_monitoring_windows(candidate_id)
                if item.source_regression_run_id == source_regression_run_id
            ),
            None,
        )
        if existing is not None:
            return existing
        _append_jsonl(_windows_path(), window.model_dump(mode="json"))
    return window


def read_monitoring_observations(
    candidate_id: str | None = None,
    *,
    window_id: str | None = None,
) -> list[MonitoringObservation]:
    observations: list[MonitoringObservation] = []
    for raw in _read_jsonl(_observations_path()):
        try:
            observation = MonitoringObservation.model_validate(raw)
        except ValueError:
            continue
        if candidate_id is not None and observation.candidate_id != candidate_id:
            continue
        if window_id is not None and observation.window_id != window_id:
            continue
        observations.append(observation)
    return sorted(observations, key=lambda item: (item.observed_at, item.event_id))


def append_monitoring_observation(observation: MonitoringObservation) -> MonitoringObservation:
    with _LOCK:
        existing = {item.event_id: item for item in read_monitoring_observations()}
        if observation.event_id in existing:
            return existing[observation.event_id]
        _append_jsonl(_observations_path(), observation.model_dump(mode="json"))
    return observation


def _candidate_status(candidate_id: str) -> str:
    state = review_index.read_candidate_state(candidate_id)
    return state.status if state else "new"


def _window_is_current(window: MonitoringWindow, release: dict[str, str] | None = None) -> bool:
    current = release or current_release_fingerprints()
    return (
        window.code_fingerprint == current.get("code")
        and window.prompt_fingerprint == current.get("prompt")
        and window.checker_version == checker_version(window.checker_id)
    )


def _project_is_applicable(project_id: str, checker: dict[str, Any]) -> bool:
    if workspace_store.read_progress(project_id).state.value != "完成":
        return False
    workspace = workspace_store.workspace_dir(project_id)
    return all((workspace / relative).is_file() for relative in checker["required_outputs"])


def _matching_findings(
    project_id: str,
    scan: dict[str, Any],
    problem_types: set[str],
) -> tuple[bool, list[str]]:
    canonical_keys = {
        str(observation.get("canonical_key") or "")
        for observation in scan.get("observations", [])
        if isinstance(observation, dict) and observation.get("problem_type") in problem_types
    }
    canonical_keys.discard("")
    finding_ids = [
        finding.finding_id
        for finding in review_store.read_findings(project_id)
        if finding.problem_type in problem_types and finding.canonical_key in canonical_keys
    ]
    return bool(canonical_keys), sorted(set(finding_ids))


def _event_id(window_id: str, project_id: str, project_revision: str, outcome: str) -> str:
    value = f"{window_id}:{project_id}:{project_revision}:{outcome}"
    return f"monitoring-observation-{hashlib.sha256(value.encode()).hexdigest()[:20]}"


def observe_state_scan(project_id: str, scan: dict[str, Any]) -> list[MonitoringObservation]:
    created: list[MonitoringObservation] = []
    revision = str(scan.get("project_revision") or "")
    observed_at = str(scan.get("observed_at") or _now())
    revision_created_at = _parse_datetime(str(scan.get("revision_created_at") or ""))
    for window in active_monitoring_windows():
        checker = _checker_by_id(window.checker_id)
        if (
            not checker
            or _candidate_status(window.candidate_id) not in {"monitoring", "verified"}
            or not _window_is_current(window)
            or not revision
            or not _project_is_applicable(project_id, checker)
        ):
            continue
        window_start = _parse_datetime(window.started_at)
        if revision_created_at and window_start and revision_created_at < window_start:
            continue
        matched, finding_ids = _matching_findings(
            project_id,
            scan,
            set(checker["problem_types"]),
        )
        outcome = "hit" if matched else "clear"
        created.append(append_monitoring_observation(MonitoringObservation(
            event_id=_event_id(window.window_id, project_id, revision, outcome),
            window_id=window.window_id,
            candidate_id=window.candidate_id,
            project_id=project_id,
            project_revision=revision,
            outcome=outcome,
            finding_ids=finding_ids,
            reason="状态扫描命中同类症状" if matched else "状态扫描未发现同类症状",
            observed_at=observed_at,
        )))
    return created


def _fallback_project_revision(project_id: str, checker: dict[str, Any]) -> str:
    workspace = workspace_store.workspace_dir(project_id)
    digest = hashlib.sha256()
    for relative in checker["required_outputs"]:
        path = workspace / relative
        digest.update(relative.encode("utf-8"))
        if path.is_file():
            stat = path.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
    return digest.hexdigest()


def observe_state_scan_failure(project_id: str, error: str) -> list[MonitoringObservation]:
    created: list[MonitoringObservation] = []
    observed_at = _now()
    for window in active_monitoring_windows():
        checker = _checker_by_id(window.checker_id)
        if (
            not checker
            or _candidate_status(window.candidate_id) not in {"monitoring", "verified"}
            or not _window_is_current(window)
            or not _project_is_applicable(project_id, checker)
        ):
            continue
        revision = _fallback_project_revision(project_id, checker)
        created.append(append_monitoring_observation(MonitoringObservation(
            event_id=_event_id(window.window_id, project_id, revision, "failed"),
            window_id=window.window_id,
            candidate_id=window.candidate_id,
            project_id=project_id,
            project_revision=revision,
            outcome="failed",
            reason=str(error)[:500],
            observed_at=observed_at,
        )))
    return created


def monitoring_summary(
    candidate_id: str,
    *,
    release_fingerprints: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    window = active_monitoring_window(candidate_id)
    if window is None:
        return {
            "candidate_id": candidate_id,
            "status": "not_started",
            "window": None,
            "sample_count": 0,
            "hit_count": 0,
            "failed_count": 0,
            "hit_rate": None,
            "elapsed_days": 0,
            "unmet_gates": ["monitoring_window_created"],
        }
    observations = read_monitoring_observations(candidate_id, window_id=window.window_id)
    samples = {(item.project_id, item.project_revision) for item in observations if item.outcome in {"clear", "hit"}}
    hits = {(item.project_id, item.project_revision) for item in observations if item.outcome == "hit"}
    latest = observations[-1] if observations else None
    current_time = now or datetime.now(timezone.utc)
    started = _parse_datetime(window.started_at) or current_time
    elapsed_days = max(0.0, (current_time - started).total_seconds() / 86400)
    release_current = _window_is_current(window, release_fingerprints)
    gates = {
        "candidate_in_monitoring": _candidate_status(candidate_id) in {"monitoring", "verified"},
        "release_current": release_current,
        "minimum_elapsed_days_met": elapsed_days >= window.minimum_elapsed_days,
        "minimum_samples_met": len(samples) >= window.minimum_samples,
        "no_checker_hits": not hits,
        "latest_scan_succeeded": latest is None or latest.outcome != "failed",
    }
    if not release_current:
        status = "stale"
    elif hits:
        status = "breached"
    elif all(gates.values()):
        status = "ready"
    else:
        status = "collecting"
    return {
        "candidate_id": candidate_id,
        "status": status,
        "window": window.model_dump(mode="json"),
        "sample_count": len(samples),
        "hit_count": len(hits),
        "failed_count": sum(item.outcome == "failed" for item in observations),
        "hit_rate": len(hits) / len(samples) if samples else None,
        "elapsed_days": round(elapsed_days, 3),
        "minimum_elapsed_days": window.minimum_elapsed_days,
        "minimum_samples": window.minimum_samples,
        "latest_observation": latest.model_dump(mode="json") if latest else None,
        "unmet_gates": [name for name, passed in gates.items() if not passed],
    }
