from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.review import index as review_index
from src.review import monitoring as review_monitoring
from src.review import regression as review_regression
from src.review import store as review_store
from src.review.models import (
    OptimizationRegressionCaseResult,
    OptimizationRegressionRun,
    TravelReviewSystemObservation,
)
from src.workspace import store as workspace_store
from src.workspace.schema import ProjectPhase


@pytest.fixture()
def monitoring_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(
        review_monitoring,
        "current_release_fingerprints",
        lambda: {"code": "code-v1", "prompt": "prompt-v1"},
    )
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": "code-v1", "prompt": "prompt-v1"},
    )
    review_store._LOCKS.clear()
    project_id = "monitoring-project"
    workspace_store.init_project(project_id)
    workspace_store.update_progress(project_id, "main_agent", state=ProjectPhase.DONE)
    observation = TravelReviewSystemObservation(
        root_cause="code",
        problem_type="manifest_mismatch",
        cause_key="code:manifest_mismatch",
        affected_layer="runtime_contract",
        summary="manifest 状态需要持续观察。",
        severity="high",
        confidence="high",
        is_systemic=True,
    )
    for index in range(2):
        finding = review_store.create_finding(
            project_id,
            problem_type="manifest_mismatch",
            root_cause="code",
            target={"file": f"outputs/poi_details/day{index + 1}.json"},
        )
        signal = review_store.append_improvement_signals(
            project_id,
            finding_id=finding.finding_id,
            recommendation_id=f"recommendation-{index}",
            observations=[observation],
        )[0]
        review_store.decide_improvement_signal(
            project_id,
            signal.signal_id,
            decision="accepted",
            actor_id="tester",
        )
    candidate = review_index.aggregate_optimization_candidates()[0]
    for status in ("triaged", "planned", "implementing"):
        review_index.update_candidate_state(candidate["candidate_id"], status=status, actor_id="tester")
    review_regression._save_run(OptimizationRegressionRun(
        run_id="regression-run-passed",
        candidate_id=candidate["candidate_id"],
        status="succeeded",
        case_ids=["case-monitoring-proof"],
        results=[OptimizationRegressionCaseResult(
            case_id="case-monitoring-proof",
            case_type="unit_test",
            label="Monitoring证据测试",
            status="passed",
            summary="通过",
        )],
        code_fingerprint="code-v1",
        prompt_fingerprint={"prompt_hash": "prompt-v1"},
        created_at="2026-09-01T00:00:00+00:00",
        completed_at="2026-09-01T00:01:00+00:00",
    ))
    review_index.update_candidate_state(
        candidate["candidate_id"],
        status="monitoring",
        actor_id="tester",
    )
    candidate = next(
        item
        for item in review_index.aggregate_optimization_candidates()
        if item["candidate_id"] == candidate["candidate_id"]
    )
    return project_id, candidate


def _create_window(candidate: dict, **overrides):
    return review_monitoring.create_monitoring_window(
        candidate,
        "regression-run-passed",
        minimum_elapsed_days=overrides.pop("minimum_elapsed_days", 0),
        minimum_samples=overrides.pop("minimum_samples", 1),
        started_at=overrides.pop(
            "started_at",
            (datetime.now(timezone.utc) - timedelta(days=15)).isoformat(),
        ),
        **overrides,
    )


def _write_manifest(project_id: str, generated: list[dict]) -> None:
    path = workspace_store.workspace_dir(project_id) / "outputs" / "poi_details" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated": generated}, ensure_ascii=False), encoding="utf-8")


def test_missing_window_is_not_started(monitoring_candidate):
    _, candidate = monitoring_candidate

    summary = review_monitoring.monitoring_summary(candidate["candidate_id"])

    assert summary["status"] == "not_started"
    assert summary["sample_count"] == 0
    assert summary["hit_rate"] is None
    assert summary["unmet_gates"] == ["monitoring_window_created"]


def test_systemic_flag_defaults_to_local_only(monitoring_candidate):
    project_id, _ = monitoring_candidate
    finding = review_store.create_finding(
        project_id,
        problem_type="local_wording",
        root_cause="content",
        target={"file": "final_guide.md"},
    )
    local_observation = TravelReviewSystemObservation(
        root_cause="content",
        problem_type="local_wording",
        summary="只影响当前项目的一处措辞。",
    )

    signals = review_store.append_improvement_signals(
        project_id,
        finding_id=finding.finding_id,
        recommendation_id="recommendation-local",
        observations=[local_observation],
    )

    assert local_observation.is_systemic is False
    assert signals == []


def test_window_creation_is_idempotent_per_regression_run(monitoring_candidate):
    _, candidate = monitoring_candidate

    first = _create_window(candidate)
    second = _create_window(candidate)

    assert second.window_id == first.window_id
    assert len(review_monitoring.read_monitoring_windows(candidate["candidate_id"])) == 1


def test_clear_revision_is_idempotent_and_can_become_ready(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)
    _write_manifest(project_id, [])

    review_store.sync_state_findings(project_id)
    review_store.sync_state_findings(project_id)
    observations = review_monitoring.read_monitoring_observations(candidate["candidate_id"])
    summary = review_monitoring.monitoring_summary(candidate["candidate_id"])

    assert len(observations) == 1
    assert observations[0].outcome == "clear"
    assert summary["sample_count"] == 1
    assert summary["hit_rate"] == 0
    assert summary["status"] == "ready"


def test_hit_revision_breaches_window(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)
    _write_manifest(project_id, [{"file": "missing.json", "citation_status": "passed"}])

    review_store.sync_state_findings(project_id)
    observation = review_monitoring.read_monitoring_observations(candidate["candidate_id"])[0]
    summary = review_monitoring.monitoring_summary(candidate["candidate_id"])

    assert observation.outcome == "hit"
    assert observation.finding_ids
    assert summary["sample_count"] == 1
    assert summary["hit_count"] == 1
    assert summary["hit_rate"] == 1
    assert summary["status"] == "breached"


def test_collecting_monitoring_cannot_be_verified(monitoring_candidate):
    _, candidate = monitoring_candidate
    _create_window(candidate, minimum_samples=2)

    with pytest.raises(ValueError, match="仍在 collecting"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="verified",
            verification_summary="人工确认回归已通过。",
            actor_id="tester",
        )


def test_missing_monitoring_window_cannot_be_verified(monitoring_candidate):
    _, candidate = monitoring_candidate

    with pytest.raises(ValueError, match="必须创建 Monitoring Window"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="verified",
            verification_summary="没有观察窗不能通过。",
            actor_id="tester",
        )


def test_ready_monitoring_with_summary_can_be_verified(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)
    _write_manifest(project_id, [])
    review_store.sync_state_findings(project_id)

    state = review_index.update_candidate_state(
        candidate["candidate_id"],
        status="verified",
        verification_summary="观察窗满足时间和样本门槛，未发现复发。",
        actor_id="tester",
    )

    assert state.status == "verified"


def test_stale_monitoring_cannot_be_verified(monitoring_candidate, monkeypatch):
    _, candidate = monitoring_candidate
    _create_window(candidate)
    monkeypatch.setattr(
        review_monitoring,
        "current_release_fingerprints",
        lambda: {"code": "code-v2", "prompt": "prompt-v1"},
    )

    with pytest.raises(ValueError, match="Monitoring 已 stale"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="verified",
            verification_summary="旧窗口摘要。",
            actor_id="tester",
        )


def test_breached_monitoring_cannot_be_verified(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)
    _write_manifest(project_id, [{"file": "missing.json", "citation_status": "passed"}])
    review_store.sync_state_findings(project_id)

    with pytest.raises(ValueError, match="Monitoring 已 breached"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="verified",
            verification_summary="存在复发时不能通过。",
            actor_id="tester",
        )
    assert review_index.read_candidate_state(candidate["candidate_id"]).status == "monitoring"


def test_incomplete_or_missing_required_output_is_skipped(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)

    review_store.sync_state_findings(project_id)
    assert review_monitoring.read_monitoring_observations(candidate["candidate_id"]) == []

    workspace_store.update_progress(project_id, "main_agent", state=ProjectPhase.INTEGRATING)
    _write_manifest(project_id, [])
    review_store.sync_state_findings(project_id)
    assert review_monitoring.read_monitoring_observations(candidate["candidate_id"]) == []


def test_failed_scan_is_not_a_sample_and_blocks_ready(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)
    _write_manifest(project_id, [])
    review_store.sync_state_findings(project_id)

    review_monitoring.observe_state_scan_failure(project_id, "scanner unavailable")
    summary = review_monitoring.monitoring_summary(candidate["candidate_id"])

    assert summary["sample_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["status"] == "collecting"
    assert "latest_scan_succeeded" in summary["unmet_gates"]


def test_revision_created_before_window_is_skipped(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _write_manifest(project_id, [])
    _create_window(
        candidate,
        started_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    )

    review_store.sync_state_findings(project_id)

    assert review_monitoring.read_monitoring_observations(candidate["candidate_id"]) == []


def test_release_change_marks_window_stale_and_stops_collection(
    monitoring_candidate,
    monkeypatch,
):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)
    _write_manifest(project_id, [])
    monkeypatch.setattr(
        review_monitoring,
        "current_release_fingerprints",
        lambda: {"code": "code-v2", "prompt": "prompt-v1"},
    )

    review_store.sync_state_findings(project_id)
    summary = review_monitoring.monitoring_summary(candidate["candidate_id"])

    assert review_monitoring.read_monitoring_observations(candidate["candidate_id"]) == []
    assert summary["status"] == "stale"
    assert "release_current" in summary["unmet_gates"]


def test_same_status_metadata_updates_do_not_reapply_entry_gates(monitoring_candidate, monkeypatch):
    project_id, candidate = monitoring_candidate
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": "code-v2", "prompt": "prompt-v2"},
    )

    monitoring_state = review_index.update_candidate_state(
        candidate["candidate_id"],
        status="monitoring",
        owner="quality-owner",
        notes="仅更新负责人和备注。",
        actor_id="tester",
    )
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": "code-v1", "prompt": "prompt-v1"},
    )
    _create_window(candidate)
    _write_manifest(project_id, [])
    review_store.sync_state_findings(project_id)
    review_index.update_candidate_state(
        candidate["candidate_id"],
        status="verified",
        verification_summary="既有验证摘要。",
        actor_id="tester",
    )
    monkeypatch.setattr(
        review_monitoring,
        "current_release_fingerprints",
        lambda: {"code": "code-v2", "prompt": "prompt-v2"},
    )

    verified_state = review_index.update_candidate_state(
        candidate["candidate_id"],
        status="verified",
        owner="next-owner",
        notes="继续维护元数据。",
        actor_id="tester",
    )

    assert monitoring_state.owner == "quality-owner"
    assert verified_state.status == "verified"
    assert verified_state.owner == "next-owner"
    assert verified_state.verification_summary == "既有验证摘要。"


def test_monitoring_api_is_read_only(monitoring_candidate):
    project_id, candidate = monitoring_candidate
    _create_window(candidate)
    _write_manifest(project_id, [])
    review_store.sync_state_findings(project_id)
    client = TestClient(app)

    current = client.get(
        f"/api/review-index/optimization-candidates/{candidate['candidate_id']}/monitoring"
    )
    observations = client.get(
        f"/api/review-index/optimization-candidates/{candidate['candidate_id']}/monitoring-observations"
    )
    removed_mutation = client.patch(
        f"/api/review-index/optimization-candidates/{candidate['candidate_id']}/monitoring-policy",
        json={},
    )
    removed_rebuild = client.post(
        f"/api/review-index/optimization-candidates/{candidate['candidate_id']}/monitoring/rebuild"
    )

    assert current.status_code == 200
    assert current.json()["status"] == "ready"
    assert observations.status_code == 200
    assert observations.json()[0]["outcome"] == "clear"
    assert removed_mutation.status_code in {404, 405}
    assert removed_rebuild.status_code in {404, 405}
