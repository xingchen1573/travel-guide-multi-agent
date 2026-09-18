from __future__ import annotations

import pytest

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


@pytest.fixture()
def regression_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "regression-project"
    workspace_store.init_project(project_id)
    observation = TravelReviewSystemObservation(
        root_cause="code",
        problem_type="manifest_mismatch",
        cause_key="code:regression_runner_contract",
        affected_layer="runtime_contract",
        summary="候选修复需要定向回归。",
        suggested_fix="修正回写契约。",
        suggested_test="运行精确的回归断言。",
        severity="high",
        confidence="high",
        is_systemic=True,
    )
    findings = []
    for index in range(2):
        finding = review_store.create_finding(
            project_id,
            problem_type="manifest_mismatch",
            root_cause="code",
            target={"file": f"outputs/poi_details/day{index + 1}.json"},
        )
        finding = review_store.update_finding(
            project_id,
            finding.finding_id,
            disposition="needs_rerun",
            status="open",
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
        findings.append(finding)
    candidate = review_index.aggregate_optimization_candidates()[0]
    return project_id, findings, candidate


def _advance_to_implementing(candidate_id: str) -> None:
    for status in ("triaged", "planned", "implementing"):
        review_index.update_candidate_state(candidate_id, status=status, actor_id="tester")


def _save_successful_run(
    candidate_id: str,
    *,
    run_id: str,
    case_type: str = "unit_test",
    code_fingerprint: str = "code-v1",
    prompt_hash: str = "prompt-v1",
) -> OptimizationRegressionRun:
    run = OptimizationRegressionRun(
        run_id=run_id,
        candidate_id=candidate_id,
        status="succeeded",
        case_ids=[f"case-{run_id}"],
        results=[OptimizationRegressionCaseResult(
            case_id=f"case-{run_id}",
            case_type=case_type,
            label="证据案例",
            status="passed",
            summary="通过",
        )],
        code_fingerprint=code_fingerprint,
        prompt_fingerprint={"prompt_hash": prompt_hash},
        created_at="2026-09-01T00:00:00+00:00",
        completed_at="2026-09-01T00:01:00+00:00",
    )
    return review_regression._save_run(run)


def test_sync_default_cases_is_idempotent(regression_candidate):
    _, findings, candidate = regression_candidate

    first = review_regression.sync_default_regression_cases(candidate)
    second = review_regression.sync_default_regression_cases(candidate)

    assert [item.case_id for item in first] == [item.case_id for item in second]
    finding_cases = [item for item in first if item.case_type == "original_finding"]
    assert len(finding_cases) == len(findings)
    assert all(item.status == "ready" for item in finding_cases)
    assert any(item.case_type == "unit_test" and item.status == "draft" for item in first)
    assert any(item.case_type == "counterexample" and item.status == "draft" for item in first)


def test_candidate_keeps_explicit_pair_lineage_and_target_scope(regression_candidate):
    project_id, _, candidate = regression_candidate
    for signal in review_store.read_improvement_signals(project_id):
        review_store.decide_improvement_signal(
            project_id,
            signal.signal_id,
            decision="accepted",
            actor_id="tester",
            comparison_pair_id="pair-regression-v1",
        )

    review_index.update_candidate_state(
        candidate["candidate_id"],
        status="triaged",
        target_scope="shared_kernel",
        change_ref="commit:abc123",
        validation_pair_ids=["pair-regression-v2"],
        actor_id="tester",
    )
    updated = next(
        item
        for item in review_index.aggregate_optimization_candidates()
        if item["candidate_id"] == candidate["candidate_id"]
    )

    assert updated["source_pair_ids"] == ["pair-regression-v1"]
    assert updated["target_scope"] == "shared_kernel"
    assert updated["change_ref"] == "commit:abc123"
    assert updated["validation_pair_ids"] == ["pair-regression-v2"]


def test_validate_test_selector_only_allows_repo_tests(regression_candidate):
    _, _, candidate = regression_candidate
    selector = "tests/test_review_loop.py::test_repeated_root_cause_creates_optimization_candidate"

    case = review_regression.create_regression_case(
        candidate["candidate_id"],
        case_type="unit_test",
        label="候选聚合回归",
        test_selector=selector,
    )

    assert case.status == "ready"
    assert case.test_selector == selector
    with pytest.raises(ValueError, match="tests/"):
        review_regression.validate_test_selector("../tests/test_review_loop.py")
    with pytest.raises(ValueError, match="tests/"):
        review_regression.validate_test_selector("tests/test_review_loop.py; echo unsafe")


def test_manual_monitoring_requires_successful_regression_run(regression_candidate):
    _, _, candidate = regression_candidate
    _advance_to_implementing(candidate["candidate_id"])

    with pytest.raises(ValueError, match="缺少属于当前候选的成功 Regression Run"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="monitoring",
            actor_id="tester",
        )


def test_finding_only_successful_run_cannot_enter_monitoring(regression_candidate, monkeypatch):
    _, _, candidate = regression_candidate
    _advance_to_implementing(candidate["candidate_id"])
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": "code-v1", "prompt": "prompt-v1"},
    )
    _save_successful_run(
        candidate["candidate_id"],
        run_id="regression-run-finding-only",
        case_type="original_finding",
    )

    with pytest.raises(ValueError, match="只有 Finding 状态复查"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="monitoring",
            actor_id="tester",
        )


def test_current_executable_run_allows_manual_monitoring(regression_candidate, monkeypatch):
    _, _, candidate = regression_candidate
    _advance_to_implementing(candidate["candidate_id"])
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": "code-v1", "prompt": "prompt-v1"},
    )
    _save_successful_run(
        candidate["candidate_id"],
        run_id="regression-run-current-executable",
    )

    state = review_index.update_candidate_state(
        candidate["candidate_id"],
        status="monitoring",
        actor_id="tester",
    )

    assert state.status == "monitoring"


@pytest.mark.parametrize(
    ("current_code", "current_prompt"),
    [("code-v2", "prompt-v1"), ("code-v1", "prompt-v2")],
    ids=["stale-code", "stale-prompt"],
)
def test_stale_successful_run_cannot_reenter_monitoring(
    regression_candidate,
    monkeypatch,
    current_code,
    current_prompt,
):
    _, _, candidate = regression_candidate
    _advance_to_implementing(candidate["candidate_id"])
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": current_code, "prompt": current_prompt},
    )
    _save_successful_run(
        candidate["candidate_id"],
        run_id="regression-run-stale",
        code_fingerprint="code-v1",
        prompt_hash="prompt-v1",
    )

    with pytest.raises(ValueError, match="指纹已过期"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="monitoring",
            actor_id="tester",
        )


def test_candidate_without_checker_can_be_verified_with_current_regression(
    regression_candidate,
    monkeypatch,
):
    _, _, candidate = regression_candidate
    _advance_to_implementing(candidate["candidate_id"])
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": "code-v1", "prompt": "prompt-v1"},
    )
    monkeypatch.setattr(review_monitoring, "supports_candidate", lambda _candidate: False)
    _save_successful_run(
        candidate["candidate_id"],
        run_id="regression-run-no-checker-current",
    )
    review_index.update_candidate_state(
        candidate["candidate_id"],
        status="monitoring",
        actor_id="tester",
    )

    state = review_index.update_candidate_state(
        candidate["candidate_id"],
        status="verified",
        verification_summary="当前版本可执行回归通过，人工复核摘要已记录。",
        actor_id="tester",
    )

    assert state.status == "verified"


def test_candidate_without_checker_cannot_be_verified_without_executable_regression(
    regression_candidate,
    monkeypatch,
):
    _, _, candidate = regression_candidate
    _advance_to_implementing(candidate["candidate_id"])
    monkeypatch.setattr(
        review_regression,
        "compute_release_fingerprints",
        lambda: {"code": "code-v1", "prompt": "prompt-v1"},
    )
    monkeypatch.setattr(review_monitoring, "supports_candidate", lambda _candidate: False)
    run_id = "regression-run-no-checker-replaced"
    _save_successful_run(candidate["candidate_id"], run_id=run_id)
    review_index.update_candidate_state(
        candidate["candidate_id"],
        status="monitoring",
        actor_id="tester",
    )
    _save_successful_run(
        candidate["candidate_id"],
        run_id=run_id,
        case_type="original_finding",
    )

    with pytest.raises(ValueError, match="只有 Finding 状态复查"):
        review_index.update_candidate_state(
            candidate["candidate_id"],
            status="verified",
            verification_summary="只有人工摘要不足以验证。",
            actor_id="tester",
        )


def test_successful_run_advances_implementing_candidate_to_monitoring(
    regression_candidate,
    monkeypatch,
):
    project_id, findings, candidate = regression_candidate
    for finding in findings:
        review_store.record_verification(
            project_id,
            finding.finding_id,
            verification_status="passed",
            verification_method="targeted_regression",
            disposition="resolved",
            actor_type="human",
            actor_id="tester",
        )
    _advance_to_implementing(candidate["candidate_id"])

    monkeypatch.setattr(review_regression, "_prompt_fingerprint", lambda: {"prompt_hash": "prompt-123"})
    monkeypatch.setattr(review_regression, "compute_code_fingerprint", lambda: "code-123")

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(review_regression.threading, "Thread", ImmediateThread)
    cases = review_regression.sync_default_regression_cases(candidate)
    finding_case_ids = [item.case_id for item in cases if item.case_type == "original_finding"]
    test_case = review_regression.create_regression_case(
        candidate["candidate_id"],
        case_type="unit_test",
        label="候选聚合回归",
        test_selector="tests/test_review_loop.py::test_repeated_root_cause_creates_optimization_candidate",
    )

    started = review_regression.start_regression_run(
        candidate["candidate_id"],
        case_ids=[*finding_case_ids, test_case.case_id],
        actor_id="tester",
    )
    completed = review_regression.read_regression_run(started.run_id)
    refreshed = next(
        item
        for item in review_index.aggregate_optimization_candidates()
        if item["candidate_id"] == candidate["candidate_id"]
    )

    assert completed is not None
    assert completed.status == "succeeded"
    assert all(item.status == "passed" for item in completed.results)
    assert completed.code_fingerprint == "code-123"
    assert completed.prompt_fingerprint["prompt_hash"] == "prompt-123"
    assert refreshed["status"] == "monitoring"
    assert started.run_id in refreshed["verification_summary"]
    window = review_monitoring.active_monitoring_window(candidate["candidate_id"])
    assert window is not None
    assert window.source_regression_run_id == started.run_id
    assert window.code_fingerprint == "code-123"
    assert "持续观察窗口已启动" in completed.progress_message


def test_monitoring_start_failure_does_not_overwrite_regression_success(
    regression_candidate,
    monkeypatch,
):
    _, _, candidate = regression_candidate
    _advance_to_implementing(candidate["candidate_id"])
    completed = OptimizationRegressionRun(
        run_id="regression-run-monitoring-failure",
        candidate_id=candidate["candidate_id"],
        status="succeeded",
        code_fingerprint="code-123",
        prompt_fingerprint={"prompt_hash": "prompt-123"},
        completed_at="2026-08-28T00:00:00+00:00",
    )

    def fail_to_create(*_args, **_kwargs):
        raise OSError("monitoring storage unavailable")

    monkeypatch.setattr(review_monitoring, "create_monitoring_window", fail_to_create)
    result = review_regression._start_monitoring_after_regression(completed, result_count=2)
    refreshed = next(
        item
        for item in review_index.aggregate_optimization_candidates()
        if item["candidate_id"] == candidate["candidate_id"]
    )

    assert completed.status == "succeeded"
    assert result["status"] == "failed"
    assert "monitoring storage unavailable" in result["reason"]
    assert refreshed["status"] == "implementing"


def test_finding_state_check_alone_does_not_advance_candidate(regression_candidate, monkeypatch):
    project_id, findings, candidate = regression_candidate
    for finding in findings:
        review_store.record_verification(
            project_id,
            finding.finding_id,
            verification_status="passed",
            verification_method="targeted_review",
            disposition="resolved",
            actor_type="human",
            actor_id="tester",
        )
    _advance_to_implementing(candidate["candidate_id"])
    monkeypatch.setattr(review_regression, "_prompt_fingerprint", lambda: {"prompt_hash": "prompt-123"})
    monkeypatch.setattr(review_regression, "compute_code_fingerprint", lambda: "code-123")

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(review_regression.threading, "Thread", ImmediateThread)
    cases = review_regression.sync_default_regression_cases(candidate)
    selected = next(item for item in cases if item.case_type == "original_finding")

    started = review_regression.start_regression_run(
        candidate["candidate_id"],
        case_ids=[selected.case_id],
        actor_id="tester",
    )
    completed = review_regression.read_regression_run(started.run_id)
    refreshed = next(
        item
        for item in review_index.aggregate_optimization_candidates()
        if item["candidate_id"] == candidate["candidate_id"]
    )

    assert completed is not None
    assert completed.status == "succeeded"
    assert "未包含可执行测试" in completed.progress_message
    assert refreshed["status"] == "implementing"


def test_failed_run_keeps_candidate_out_of_monitoring(regression_candidate, monkeypatch):
    _, _, candidate = regression_candidate
    monkeypatch.setattr(review_regression, "_prompt_fingerprint", lambda: {"prompt_hash": "prompt-123"})
    monkeypatch.setattr(review_regression, "compute_code_fingerprint", lambda: "code-123")

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(review_regression.threading, "Thread", ImmediateThread)
    cases = review_regression.sync_default_regression_cases(candidate)
    selected = next(item for item in cases if item.case_type == "original_finding")

    started = review_regression.start_regression_run(
        candidate["candidate_id"],
        case_ids=[selected.case_id],
        actor_id="tester",
    )
    completed = review_regression.read_regression_run(started.run_id)
    refreshed = next(
        item
        for item in review_index.aggregate_optimization_candidates()
        if item["candidate_id"] == candidate["candidate_id"]
    )

    assert completed is not None
    assert completed.status == "failed"
    assert completed.results[0].status == "failed"
    assert refreshed["status"] == "new"


def test_fixed_eval_requires_explicit_cost_confirmation(regression_candidate, monkeypatch):
    _, _, candidate = regression_candidate
    monkeypatch.setattr(review_regression, "list_eval_scenarios", lambda: ["chengdu_3d"])
    case = review_regression.create_regression_case(
        candidate["candidate_id"],
        case_type="fixed_eval",
        label="成都 3 日固定评测",
        scenario_id="chengdu_3d",
    )

    with pytest.raises(ValueError, match="cost_confirmed=true"):
        review_regression.start_regression_run(
            candidate["candidate_id"],
            case_ids=[case.case_id],
        )
