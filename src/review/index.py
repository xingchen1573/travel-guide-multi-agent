"""跨项目复核与持续改进索引。

项目 workspace 仍是事实来源；全局索引只聚合经人工接受的 Improvement Signal。
候选统计可重建，候选的人工作业状态单独追加保存，避免重建时丢失。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.review import store
from src.review.models import ImprovementSignal, OptimizationCandidateState
from src.workspace import store as workspace_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _index_dir() -> Path:
    return workspace_store.PROJECTS_DIR.parent / "runtime" / "review_index"


def _index_path() -> Path:
    return _index_dir() / "optimization_candidates.jsonl"


def _candidate_states_path() -> Path:
    return _index_dir() / "candidate_states.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _candidate_id(cause_key: str) -> str:
    digest = hashlib.sha256(cause_key.encode("utf-8")).hexdigest()[:16]
    return f"optimization-{digest}"


def aggregate_findings(*, include_closed: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for project_id in workspace_store.list_project_ids():
        store.backfill_findings_from_review_log(project_id)
        store.sync_state_findings(project_id)
        primary_ids = {
            item.primary_finding_id
            for item in store.build_review_queue(project_id, include_closed=True)
        }
        for finding in store.read_findings(project_id):
            if finding.finding_id not in primary_ids or finding.superseded_by:
                continue
            if not include_closed and finding.status == "closed":
                continue
            item = finding.model_dump(mode="json")
            item["project_id"] = project_id
            rows.append(item)
    return sorted(rows, key=lambda item: item.get("updated_at", ""), reverse=True)


def aggregate_improvement_signals(*, decision: str | None = None) -> list[ImprovementSignal]:
    signals: list[ImprovementSignal] = []
    for project_id in workspace_store.list_project_ids():
        store.sync_improvement_signals_from_artifacts(project_id)
        for signal in store.read_improvement_signals(project_id):
            if decision is None or signal.decision == decision:
                signals.append(signal)
    return sorted(signals, key=lambda item: (item.updated_at, item.signal_id), reverse=True)


def _candidate_states() -> dict[str, OptimizationCandidateState]:
    latest: dict[str, OptimizationCandidateState] = {}
    for raw in _read_jsonl(_candidate_states_path()):
        try:
            state = OptimizationCandidateState.model_validate(raw)
        except ValueError:
            continue
        latest[state.candidate_id] = state
    return latest


def read_candidate_state(candidate_id: str) -> OptimizationCandidateState | None:
    """读取候选最新人工作业状态，不触发全局聚合或项目扫描。"""
    return _candidate_states().get(candidate_id)


def _finding_bucket(signal: ImprovementSignal, findings: dict[tuple[str, str], dict[str, Any]]) -> str:
    finding = findings.get((signal.project_id, signal.finding_id))
    if finding is None:
        return "active"
    if finding.get("disposition") == "false_positive":
        return "false_positive"
    if finding.get("status") == "closed":
        return "resolved"
    if finding.get("disposition") == "deferred":
        return "deferred"
    return "active"


def _should_promote(signals: list[ImprovementSignal], cross_project_threshold: int) -> bool:
    by_project: dict[str, int] = {}
    for signal in signals:
        by_project[signal.project_id] = by_project.get(signal.project_id, 0) + 1
    if any(count >= 2 for count in by_project.values()):
        return True
    if len(by_project) >= cross_project_threshold:
        return True
    return any(
        (_enum_text(signal.root_cause) == "code" and signal.severity == "high")
        or signal.cause_key.startswith("manifest:")
        or signal.cause_key.startswith("security:")
        or signal.cause_key.startswith("data_loss:")
        for signal in signals
    )


def aggregate_optimization_candidates(threshold: int = 3) -> list[dict[str, Any]]:
    """从 accepted signal 重建稳定候选；pending/dismissed 不污染候选统计。"""
    accepted = [
        item
        for item in aggregate_improvement_signals()
        if item.decision in {"accepted", "merged", "reclassified"}
    ]
    grouped: dict[str, list[ImprovementSignal]] = {}
    for signal in accepted:
        grouped.setdefault(signal.cause_key, []).append(signal)

    findings = {
        (str(item.get("project_id") or ""), str(item.get("finding_id") or "")): item
        for item in aggregate_findings(include_closed=True)
    }
    states = _candidate_states()
    candidates: list[dict[str, Any]] = []
    for cause_key, signals in grouped.items():
        if not _should_promote(signals, max(1, threshold)):
            continue
        candidate_id = _candidate_id(cause_key)
        state = states.get(candidate_id)
        bucket_counts = {"active": 0, "resolved": 0, "false_positive": 0, "deferred": 0}
        for signal in signals:
            bucket_counts[_finding_bucket(signal, findings)] += 1
        ordered = sorted(signals, key=lambda item: (item.created_at, item.signal_id))
        newest = ordered[-1]
        suggested_fixes = list(dict.fromkeys(item.suggested_fix for item in ordered if item.suggested_fix))
        suggested_tests = list(dict.fromkeys(item.suggested_test for item in ordered if item.suggested_test))
        project_ids = sorted({item.project_id for item in ordered})
        source_pair_ids = sorted({
            item.comparison_pair_id
            for item in ordered
            if item.comparison_pair_id
        })
        finding_ids = sorted({f"{item.project_id}:{item.finding_id}" for item in ordered})
        layers = sorted({item.affected_layer for item in ordered if item.affected_layer})
        severity_order = {"low": 0, "medium": 1, "high": 2}
        confidence_order = {"low": 0, "medium": 1, "high": 2}
        severity = max((item.severity for item in ordered), key=severity_order.get)
        confidence = max((item.confidence for item in ordered), key=confidence_order.get)
        candidates.append({
            "candidate_id": candidate_id,
            "cause_key": cause_key,
            "title": newest.hypothesis,
            "symptom_summary": newest.hypothesis,
            "root_cause": _enum_text(newest.root_cause),
            "affected_layer": layers[0] if len(layers) == 1 else "multiple",
            "affected_layers": layers,
            "problem_type": newest.problem_type,
            "confidence": confidence,
            "severity": severity,
            "signal_ids": [item.signal_id for item in ordered],
            "finding_ids": finding_ids,
            "project_ids": project_ids,
            "source_pair_ids": source_pair_ids,
            "occurrence_count": len(ordered),
            "active_count": bucket_counts["active"],
            "resolved_count": bucket_counts["resolved"],
            "false_positive_count": bucket_counts["false_positive"],
            "deferred_count": bucket_counts["deferred"],
            "status": state.status if state else "new",
            "owner": state.owner if state else "",
            "notes": state.notes if state else "",
            "verification_summary": state.verification_summary if state else "",
            "target_scope": state.target_scope if state else "undecided",
            "change_ref": state.change_ref if state else "",
            "validation_pair_ids": state.validation_pair_ids if state else [],
            "first_seen_at": ordered[0].created_at,
            "last_seen_at": newest.updated_at,
            "created_at": ordered[0].created_at,
            "migration_required": any(item.migration_required for item in ordered),
            "suggested_fix": suggested_fixes[0] if suggested_fixes else "",
            "suggested_test": suggested_tests[0] if suggested_tests else "",
            "suggested_actions": suggested_fixes[:3],
            "regression_plan": {
                "original_findings": finding_ids[:3],
                "cross_project_case_required": len(project_ids) > 1,
                "counterexample_required": True,
                "suggested_tests": suggested_tests[:5],
                "fixed_eval_scenario_required": len(project_ids) > 1 or severity == "high",
            },
            "representative_signals": [item.model_dump(mode="json") for item in reversed(ordered[-3:])],
        })
    candidates.sort(
        key=lambda item: (
            {"high": 2, "medium": 1, "low": 0}.get(item["severity"], 0),
            item["active_count"],
            item["last_seen_at"],
        ),
        reverse=True,
    )
    _write_jsonl_atomic(_index_path(), candidates)
    return candidates


def update_candidate_state(
    candidate_id: str,
    *,
    status: str,
    owner: str = "",
    notes: str = "",
    verification_summary: str = "",
    target_scope: str | None = None,
    change_ref: str | None = None,
    validation_pair_ids: list[str] | None = None,
    actor_id: str = "admin_user",
) -> OptimizationCandidateState:
    candidates = {item["candidate_id"]: item for item in aggregate_optimization_candidates()}
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise KeyError(f"Optimization Candidate 不存在: {candidate_id}")
    current_status = str(candidate.get("status") or "new")
    allowed_transitions = {
        "new": {"new", "triaged", "dismissed", "deferred"},
        "triaged": {"triaged", "planned", "dismissed", "deferred"},
        "planned": {"planned", "implementing", "deferred"},
        "implementing": {"implementing", "monitoring", "deferred"},
        "monitoring": {"monitoring", "verified", "implementing", "deferred"},
        "verified": {"verified", "monitoring"},
        "dismissed": {"dismissed", "triaged"},
        "deferred": {"deferred", "triaged"},
    }
    if status not in allowed_transitions.get(current_status, set()):
        raise ValueError(f"候选状态不允许从 {current_status} 直接变为 {status}")
    entering_new_stage = status != current_status
    effective_verification_summary = verification_summary
    if not entering_new_stage and not effective_verification_summary.strip():
        effective_verification_summary = str(candidate.get("verification_summary") or "")
    if entering_new_stage and status == "monitoring":
        from src.review import regression as review_regression

        review_regression.require_current_regression_evidence(candidate_id)
    if entering_new_stage and status == "verified":
        if not effective_verification_summary.strip():
            raise ValueError("候选标记 verified 前必须记录回归与观察证据摘要")
        from src.review import monitoring as review_monitoring

        if review_monitoring.supports_candidate(candidate):
            summary = review_monitoring.monitoring_summary(candidate_id)
            summary_status = str(summary.get("status") or "not_started")
            if summary_status == "not_started":
                raise ValueError("候选标记 verified 前必须创建 Monitoring Window")
            if summary_status == "collecting":
                raise ValueError("Monitoring 仍在 collecting，尚未满足时间、样本或扫描成功门槛")
            if summary_status == "stale":
                raise ValueError("Monitoring 已 stale，窗口指纹或 Checker 版本不再对应当前发布")
            if summary_status == "breached":
                raise ValueError("Monitoring 已 breached，存在同类问题复发记录")
            if summary_status != "ready":
                raise ValueError(f"Monitoring 尚未 ready: {summary_status}")
        else:
            from src.review import regression as review_regression

            review_regression.require_current_regression_evidence(candidate_id)
    state = OptimizationCandidateState(
        candidate_id=candidate_id,
        cause_key=candidate["cause_key"],
        status=status,
        owner=owner,
        notes=notes,
        verification_summary=effective_verification_summary,
        target_scope=target_scope if target_scope is not None else str(candidate.get("target_scope") or "undecided"),
        change_ref=change_ref if change_ref is not None else str(candidate.get("change_ref") or ""),
        validation_pair_ids=(
            validation_pair_ids
            if validation_pair_ids is not None
            else list(candidate.get("validation_pair_ids") or [])
        ),
        updated_by=actor_id,
        updated_at=_now(),
    )
    _append_jsonl(_candidate_states_path(), state.model_dump(mode="json"))
    aggregate_optimization_candidates()
    return state


def quality_metrics() -> dict[str, Any]:
    findings = aggregate_findings(include_closed=True)
    signals = aggregate_improvement_signals()
    candidates = aggregate_optimization_candidates()
    project_count = sum(
        (workspace_store.workspace_dir(project_id) / "progress.json").is_file()
        for project_id in workspace_store.list_project_ids()
    )
    accepted = [item for item in signals if item.decision in {"accepted", "merged", "reclassified"}]
    return {
        "project_count": project_count,
        "finding_count": len(findings),
        "active_finding_count": sum(item.get("status") != "closed" for item in findings),
        "pending_signal_count": sum(item.decision == "pending" for item in signals),
        "accepted_signal_count": len(accepted),
        "dismissed_signal_count": sum(item.decision == "dismissed" for item in signals),
        "candidate_count": len(candidates),
        "monitoring_candidate_count": sum(item["status"] == "monitoring" for item in candidates),
        "verified_candidate_count": sum(item["status"] == "verified" for item in candidates),
        "accepted_signals_per_100_projects": round((len(accepted) * 100 / project_count), 2) if project_count else 0,
        "generated_at": _now(),
    }


def rebuild_global_index(threshold: int = 3) -> list[dict[str, Any]]:
    return aggregate_optimization_candidates(threshold=max(1, threshold))


def read_global_candidates() -> list[dict[str, Any]]:
    return _read_jsonl(_index_path())
