"""Optimization Candidate 的定向回放与回归执行。

执行入口只接受结构化、白名单案例：历史 Finding 状态复查、tests/ 下的 pytest
selector，以及 eval/scenarios 下的固定场景。完整 eval 有真实模型和联网成本，只在
用户显式创建 Regression Run 后运行。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.execution_state import (
    configure_execution_context,
    environment_name,
    get_current_instance_id,
    get_job_service,
)
from src.review import store as review_store
from src.review.models import (
    OptimizationRegressionCase,
    OptimizationRegressionCaseResult,
    OptimizationRegressionRun,
)
from src.workspace import store as workspace_store


_LOCK = threading.RLock()
_TEST_SELECTOR_RE = re.compile(
    r"^tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_\[\].-]+)*$"
)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUN_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}
_EXECUTABLE_CASE_TYPES = {"unit_test", "counterexample", "fixed_eval"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _index_dir() -> Path:
    return workspace_store.PROJECTS_DIR.parent / "runtime" / "review_index"


def _cases_path() -> Path:
    return _index_dir() / "regression_cases.jsonl"


def _runs_dir() -> Path:
    path = _index_dir() / "regression_runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_path(run_id: str) -> Path:
    return _runs_dir() / run_id / "run.json"


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


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


def _write_json_atomic(path: Path, value: Any) -> None:
    review_store._write_json_atomic(path, value)


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _candidate(candidate_id: str) -> dict[str, Any]:
    from src.review import index as review_index

    candidate = next(
        (
            item
            for item in review_index.aggregate_optimization_candidates()
            if item["candidate_id"] == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise KeyError(f"Optimization Candidate 不存在: {candidate_id}")
    return candidate


def read_regression_cases(candidate_id: str | None = None) -> list[OptimizationRegressionCase]:
    latest: dict[str, OptimizationRegressionCase] = {}
    for raw in _read_jsonl(_cases_path()):
        try:
            case = OptimizationRegressionCase.model_validate(raw)
        except ValueError:
            continue
        latest[case.case_id] = case
    cases = [item for item in latest.values() if candidate_id is None or item.candidate_id == candidate_id]
    return sorted(cases, key=lambda item: (item.created_at, item.case_id))


def _save_case(case: OptimizationRegressionCase) -> OptimizationRegressionCase:
    with _LOCK:
        _append_jsonl(_cases_path(), case.model_dump(mode="json"))
    return case


def sync_default_regression_cases(candidate: dict[str, Any]) -> list[OptimizationRegressionCase]:
    """从候选代表 Finding 和建议测试幂等生成回归案例草稿。"""
    candidate_id = str(candidate["candidate_id"])
    existing = {item.case_id: item for item in read_regression_cases(candidate_id)}
    now = _now()
    created: list[OptimizationRegressionCase] = []
    finding_refs = list(candidate.get("finding_ids") or [])
    first_project = finding_refs[0].split(":", 1)[0] if finding_refs else ""
    for index, ref in enumerate(finding_refs):
        if ":" not in ref:
            continue
        project_id, finding_id = ref.split(":", 1)
        case_type = "original_finding" if index == 0 or project_id == first_project else "cross_project_finding"
        case_id = _stable_id("regression-case", candidate_id, case_type, project_id, finding_id)
        if case_id in existing:
            continue
        case = OptimizationRegressionCase(
            case_id=case_id,
            candidate_id=candidate_id,
            case_type=case_type,
            label=("原始失败 Finding" if case_type == "original_finding" else "跨项目 Finding") + f" · {finding_id}",
            project_id=project_id,
            finding_id=finding_id,
            expected_outcome="not_active",
            status="ready",
            created_at=now,
            updated_at=now,
        )
        _save_case(case)
        existing[case_id] = case
        created.append(case)

    for index, suggested_test in enumerate((candidate.get("regression_plan") or {}).get("suggested_tests") or []):
        case_id = _stable_id("regression-case", candidate_id, "unit_test", str(index), str(suggested_test))
        if case_id in existing:
            continue
        case = OptimizationRegressionCase(
            case_id=case_id,
            candidate_id=candidate_id,
            case_type="unit_test",
            label=str(suggested_test),
            config={"suggested_test": suggested_test},
            status="draft",
            created_at=now,
            updated_at=now,
        )
        _save_case(case)
        existing[case_id] = case
        created.append(case)

    if (candidate.get("regression_plan") or {}).get("counterexample_required"):
        case_id = _stable_id("regression-case", candidate_id, "counterexample", "draft")
        if case_id not in existing:
            case = OptimizationRegressionCase(
                case_id=case_id,
                candidate_id=candidate_id,
                case_type="counterexample",
                label="待绑定一个不会被新规则误伤的反例测试",
                status="draft",
                created_at=now,
                updated_at=now,
            )
            _save_case(case)
            created.append(case)
    return read_regression_cases(candidate_id)


def create_regression_case(
    candidate_id: str,
    *,
    case_type: str,
    label: str,
    project_id: str = "",
    finding_id: str = "",
    test_selector: str = "",
    scenario_id: str = "",
    expected_outcome: str = "resolved",
    config: dict[str, Any] | None = None,
    actor_id: str = "admin_user",
) -> OptimizationRegressionCase:
    _candidate(candidate_id)
    allowed_types = {
        "original_finding",
        "cross_project_finding",
        "unit_test",
        "counterexample",
        "fixed_eval",
    }
    if case_type not in allowed_types:
        raise ValueError(f"不支持的 regression case 类型: {case_type}")
    if not label.strip():
        raise ValueError("regression case label 不能为空")
    status = "ready"
    if case_type in {"original_finding", "cross_project_finding"}:
        if not project_id or not finding_id:
            raise ValueError("Finding 回放必须指定 project_id 和 finding_id")
        if review_store.get_finding(project_id, finding_id) is None:
            raise KeyError(f"Finding 不存在: {project_id}:{finding_id}")
    elif case_type in {"unit_test", "counterexample"}:
        validate_test_selector(test_selector)
    elif case_type == "fixed_eval":
        if scenario_id not in list_eval_scenarios():
            raise ValueError(f"固定 eval 场景不存在: {scenario_id}")
    now = _now()
    case = OptimizationRegressionCase(
        case_id=f"regression-case-{uuid.uuid4().hex[:16]}",
        candidate_id=candidate_id,
        case_type=case_type,
        label=label.strip(),
        project_id=project_id,
        finding_id=finding_id,
        test_selector=test_selector,
        scenario_id=scenario_id,
        expected_outcome=expected_outcome,
        config=config or {},
        status=status,
        created_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    return _save_case(case)


def validate_test_selector(selector: str) -> str:
    value = selector.strip()
    if not _TEST_SELECTOR_RE.fullmatch(value) or ".." in Path(value.split("::", 1)[0]).parts:
        raise ValueError("pytest selector 只能指向 tests/ 下的具体 .py 文件或测试节点")
    test_file = _REPO_ROOT / value.split("::", 1)[0]
    if not test_file.exists():
        raise ValueError(f"pytest selector 文件不存在: {value}")
    return value


def list_eval_scenarios() -> list[str]:
    from run_scenario_eval import list_scenario_ids

    return list_scenario_ids()


def compute_code_fingerprint() -> str:
    digest = hashlib.sha256()
    roots = [
        _REPO_ROOT / "src",
        _REPO_ROOT / "tests",
        _REPO_ROOT / ".agents" / "skills" / "travel-guide-targeted-review",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file() and item.suffix in {".py", ".md", ".json"}):
            digest.update(str(path.relative_to(_REPO_ROOT)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _prompt_fingerprint() -> dict[str, Any]:
    from run_scenario_eval import compute_prompt_fingerprint

    return compute_prompt_fingerprint()


def compute_release_fingerprints() -> dict[str, str]:
    """返回 Monitoring Policy 用于版本比较的当前发布指纹。"""
    prompt = _prompt_fingerprint()
    return {
        "code": compute_code_fingerprint(),
        "prompt": str(prompt.get("prompt_hash") or ""),
    }


def _save_run(run: OptimizationRegressionRun) -> OptimizationRegressionRun:
    with _LOCK:
        _write_json_atomic(_run_path(run.run_id), run.model_dump(mode="json"))
    return run


def read_regression_run(run_id: str) -> OptimizationRegressionRun | None:
    path = _run_path(run_id)
    if not path.exists():
        return None
    try:
        run = OptimizationRegressionRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    job = get_job_service().get_job(run_id)
    if job and job.get("status") in _RUN_STATUSES:
        mapped = "succeeded" if job["status"] == "succeeded" else job["status"]
        if mapped != run.status and mapped in _RUN_STATUSES:
            run = run.model_copy(update={"status": mapped})
    return run


def read_regression_runs(candidate_id: str) -> list[OptimizationRegressionRun]:
    runs: list[OptimizationRegressionRun] = []
    for path in _runs_dir().glob("regression-run-*/run.json"):
        run = read_regression_run(path.parent.name)
        if run and run.candidate_id == candidate_id:
            runs.append(run)
    return sorted(runs, key=lambda item: (item.created_at, item.run_id), reverse=True)


def validate_regression_run_evidence(
    candidate_id: str,
    run: OptimizationRegressionRun,
    *,
    release_fingerprints: dict[str, str] | None = None,
) -> dict[str, str]:
    """校验一次 Run 是否能作为当前版本的候选阶段证据。"""
    if run.candidate_id != candidate_id:
        raise ValueError("Regression Run 不属于当前 Optimization Candidate")
    if run.status != "succeeded":
        raise ValueError("缺少属于当前候选的成功 Regression Run")
    executable_results = [
        item
        for item in run.results
        if item.case_type in _EXECUTABLE_CASE_TYPES and item.status == "passed"
    ]
    if not executable_results:
        result_types = {item.case_type for item in run.results}
        if result_types and result_types.issubset({"original_finding", "cross_project_finding"}):
            raise ValueError("Regression Run 只有 Finding 状态复查，缺少可执行测试或固定评测证据")
        raise ValueError("Regression Run 缺少通过的可执行测试或固定评测证据")
    prompt_hash = str(run.prompt_fingerprint.get("prompt_hash") or "")
    if not run.code_fingerprint or not prompt_hash:
        raise ValueError("Regression Run 缺少完整 code/prompt fingerprint")
    current = release_fingerprints or compute_release_fingerprints()
    if (
        run.code_fingerprint != str(current.get("code") or "")
        or prompt_hash != str(current.get("prompt") or "")
    ):
        raise ValueError("Regression Run 指纹已过期，不能作为当前代码和 Prompt 版本的证据")
    return {
        "run_id": run.run_id,
        "code": run.code_fingerprint,
        "prompt": prompt_hash,
    }


def require_current_regression_evidence(candidate_id: str) -> OptimizationRegressionRun:
    """返回最新的当前版本可执行成功 Run，并给出可操作的失败原因。"""
    succeeded_runs = [item for item in read_regression_runs(candidate_id) if item.status == "succeeded"]
    if not succeeded_runs:
        raise ValueError("缺少属于当前候选的成功 Regression Run")

    errors: list[str] = []
    release = compute_release_fingerprints()
    for run in succeeded_runs:
        try:
            validate_regression_run_evidence(
                candidate_id,
                run,
                release_fingerprints=release,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        return run

    for expected in (
        "Regression Run 指纹已过期",
        "Regression Run 缺少完整 code/prompt fingerprint",
        "Regression Run 只有 Finding 状态复查",
        "Regression Run 缺少通过的可执行测试或固定评测证据",
    ):
        match = next((item for item in errors if item.startswith(expected)), None)
        if match:
            raise ValueError(match)
    raise ValueError(errors[0] if errors else "缺少属于当前候选的成功 Regression Run")


def _execute_finding_case(case: OptimizationRegressionCase) -> OptimizationRegressionCaseResult:
    finding = review_store.get_finding(case.project_id, case.finding_id)
    if finding is None:
        return OptimizationRegressionCaseResult(
            case_id=case.case_id,
            case_type=case.case_type,
            label=case.label,
            status="blocked",
            summary="Finding 已不存在，无法比较修复前后状态。",
        )
    expected = case.expected_outcome or "not_active"
    if expected == "resolved":
        passed = finding.status == "closed" and str(finding.disposition) in {"resolved", "FindingDisposition.RESOLVED"}
    elif expected == "false_positive":
        passed = str(finding.disposition) in {"false_positive", "FindingDisposition.FALSE_POSITIVE"}
    else:
        passed = finding.status == "closed"
    return OptimizationRegressionCaseResult(
        case_id=case.case_id,
        case_type=case.case_type,
        label=case.label,
        status="passed" if passed else "failed",
        summary=(
            f"Finding 当前为 {finding.status}/{getattr(finding.disposition, 'value', finding.disposition)}，符合 {expected}。"
            if passed
            else f"Finding 仍为 {finding.status}/{getattr(finding.disposition, 'value', finding.disposition)}，尚未达到 {expected}。"
        ),
        details={
            "project_id": case.project_id,
            "finding_id": case.finding_id,
            "status": finding.status,
            "disposition": getattr(finding.disposition, "value", finding.disposition),
            "verification_status": finding.verification_status,
        },
    )


def _execute_pytest_case(case: OptimizationRegressionCase) -> OptimizationRegressionCaseResult:
    selector = validate_test_selector(case.test_selector)
    timeout_seconds = min(max(int(case.config.get("timeout_seconds") or 300), 10), 900)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", selector, "-q"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}".strip()
        return OptimizationRegressionCaseResult(
            case_id=case.case_id,
            case_type=case.case_type,
            label=case.label,
            status="passed" if completed.returncode == 0 else "failed",
            summary=("定向 pytest 通过。" if completed.returncode == 0 else f"定向 pytest 失败，退出码 {completed.returncode}。"),
            duration_seconds=round(time.monotonic() - started, 2),
            details={"selector": selector, "returncode": completed.returncode, "output": output[-8000:]},
        )
    except subprocess.TimeoutExpired as exc:
        return OptimizationRegressionCaseResult(
            case_id=case.case_id,
            case_type=case.case_type,
            label=case.label,
            status="blocked",
            summary=f"定向 pytest 超过 {timeout_seconds} 秒。",
            duration_seconds=round(time.monotonic() - started, 2),
            details={"selector": selector, "timeout_seconds": timeout_seconds, "output": str(exc)[-2000:]},
        )


def _judge_average(judge_scores: dict[str, Any]) -> float | None:
    values = [
        float(value)
        for key, value in judge_scores.items()
        if key.endswith("_score") and isinstance(value, (int, float))
    ]
    return sum(values) / len(values) if values else None


def _execute_eval_case(case: OptimizationRegressionCase, run_id: str) -> OptimizationRegressionCaseResult:
    from run_scenario_eval import RUNS_DIR, run_scenario

    started = time.monotonic()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{run_id[-6:]}"
    summary = run_scenario(case.scenario_id, timestamp)
    fatal_error = str(summary.get("fatal_error") or "")
    major_issues = [
        item
        for item in summary.get("issues") or []
        if item.get("severity") in {"blocker", "major"}
    ]
    judge_scores = summary.get("judge_scores") or {}
    judge_error = str(judge_scores.get("error") or "") if isinstance(judge_scores, dict) else "judge 输出无效"
    average = _judge_average(judge_scores) if isinstance(judge_scores, dict) else None
    minimum = float(case.config.get("minimum_average_score") or 3.5)
    passed = not fatal_error and not major_issues and not judge_error and average is not None and average >= minimum
    artifact = RUNS_DIR / timestamp / case.scenario_id
    return OptimizationRegressionCaseResult(
        case_id=case.case_id,
        case_type=case.case_type,
        label=case.label,
        status="passed" if passed else "failed",
        summary=(
            f"固定场景通过，Judge 平均分 {average:.2f}。"
            if passed and average is not None
            else fatal_error or judge_error or f"存在 {len(major_issues)} 个 major/blocker 问题，Judge 平均分 {average}."
        ),
        duration_seconds=round(time.monotonic() - started, 2),
        details={
            "scenario_id": case.scenario_id,
            "minimum_average_score": minimum,
            "judge_average": average,
            "major_issues": major_issues,
            "summary": summary,
            "artifact_path": str(artifact.relative_to(_REPO_ROOT)),
        },
    )


def _execute_case(case: OptimizationRegressionCase, run_id: str) -> OptimizationRegressionCaseResult:
    started = time.monotonic()
    if case.case_type in {"original_finding", "cross_project_finding"}:
        result = _execute_finding_case(case)
    elif case.case_type in {"unit_test", "counterexample"}:
        result = _execute_pytest_case(case)
    elif case.case_type == "fixed_eval":
        result = _execute_eval_case(case, run_id)
    else:
        result = OptimizationRegressionCaseResult(
            case_id=case.case_id,
            case_type=case.case_type,
            label=case.label,
            status="blocked",
            summary=f"不支持的 case type: {case.case_type}",
        )
    if result.duration_seconds == 0:
        result.duration_seconds = round(time.monotonic() - started, 2)
    return result


def _start_monitoring_after_regression(
    run: OptimizationRegressionRun,
    *,
    result_count: int,
) -> dict[str, str]:
    """启动受支持候选的观察窗口；失败不改变已通过的回归结论。"""
    from src.review import index as review_index
    from src.review import monitoring as review_monitoring

    candidate = _candidate(run.candidate_id)
    if candidate.get("status") != "implementing":
        return {"status": "skipped", "reason": "候选当前不在 implementing 状态"}
    window = None
    try:
        if review_monitoring.supports_candidate(candidate):
            window = review_monitoring.create_monitoring_window(
                candidate,
                run.run_id,
                actor="regression_runner",
                started_at=run.completed_at or _now(),
            )
        window_note = f"；window={window.window_id}" if window else "；当前问题暂无内置 checker"
        review_index.update_candidate_state(
            run.candidate_id,
            status="monitoring",
            owner=str(candidate.get("owner") or ""),
            notes=str(candidate.get("notes") or ""),
            verification_summary=(
                f"{run.run_id} 通过 {result_count} 个定向回归案例；"
                f"code={run.code_fingerprint[:12]}，"
                f"prompt={run.prompt_fingerprint.get('prompt_hash', '')[:12]}"
                f"{window_note}。"
            ),
            actor_id="regression_runner",
        )
    except Exception as exc:  # noqa: BLE001 - 观察启动失败不得覆盖回归成功事实
        return {"status": "failed", "reason": str(exc)[:500]}
    if window is None:
        return {"status": "unsupported", "reason": "当前问题暂无内置 Monitoring checker"}
    return {"status": "started", "window_id": window.window_id}


def _execute_run(run_id: str) -> None:
    run = read_regression_run(run_id)
    if run is None:
        return
    service = get_job_service()
    owner_instance_id = get_current_instance_id()
    if owner_instance_id is None:
        owner_instance_id = service.register_instance(role="standalone", environment=environment_name())
        configure_execution_context(service, owner_instance_id)
    if not service.start_job(run_id, owner_instance_id):
        return
    try:
        run = run.model_copy(update={
            "status": "running",
            "stage": "fingerprint",
            "progress_message": "正在记录代码与 Prompt 指纹。",
            "prompt_fingerprint": _prompt_fingerprint(),
            "code_fingerprint": compute_code_fingerprint(),
            "started_at": _now(),
        })
        _save_run(run)
        cases = {item.case_id: item for item in read_regression_cases(run.candidate_id)}
        results: list[OptimizationRegressionCaseResult] = []
        for index, case_id in enumerate(run.case_ids, start=1):
            case = cases.get(case_id)
            if case is None:
                result = OptimizationRegressionCaseResult(
                    case_id=case_id,
                    case_type="unknown",
                    label=case_id,
                    status="blocked",
                    summary="回归案例不存在或已被移除。",
                )
            else:
                service.update_progress(run_id, status_reason=f"正在执行 {index}/{len(run.case_ids)}：{case.label}")
                run = run.model_copy(update={
                    "stage": f"case:{case.case_type}",
                    "progress_message": f"正在执行 {index}/{len(run.case_ids)}：{case.label}",
                })
                _save_run(run)
                result = _execute_case(case, run_id)
            results.append(result)
            run = run.model_copy(update={"results": results})
            _save_run(run)

        succeeded = bool(results) and all(item.status == "passed" for item in results)
        executable_proof_case_ids = {
            case_id
            for case_id in run.case_ids
            if cases.get(case_id) and cases[case_id].case_type in _EXECUTABLE_CASE_TYPES
        }
        has_executable_proof = bool(executable_proof_case_ids)
        artifact_paths = {
            item.case_id: str(item.details["artifact_path"])
            for item in results
            if item.details.get("artifact_path")
        }
        completed = run.model_copy(update={
            "status": "succeeded" if succeeded else "failed",
            "stage": "completed" if succeeded else "failed",
            "progress_message": (
                "全部定向回归通过。"
                if succeeded and has_executable_proof
                else "Finding 状态复查通过，但未包含可执行测试，不推进候选状态。"
                if succeeded
                else "部分定向回归未通过。"
            ),
            "results": results,
            "artifact_paths": artifact_paths,
            "completed_at": _now(),
        })
        _save_run(completed)
        payload = {
            "candidate_id": run.candidate_id,
            "passed": sum(item.status == "passed" for item in results),
            "failed": sum(item.status != "passed" for item in results),
            "has_executable_proof": has_executable_proof,
            "code_fingerprint": completed.code_fingerprint,
            "prompt_hash": completed.prompt_fingerprint.get("prompt_hash", ""),
        }
        if succeeded:
            service.succeed_job(run_id, payload)
            monitoring_result = {"status": "not_requested"}
            if run.advance_to_monitoring and has_executable_proof:
                monitoring_result = _start_monitoring_after_regression(
                    completed,
                    result_count=len(results),
                )
                if monitoring_result["status"] == "started":
                    completed = completed.model_copy(update={
                        "progress_message": "全部定向回归通过，持续观察窗口已启动。",
                    })
                elif monitoring_result["status"] == "unsupported":
                    completed = completed.model_copy(update={
                        "progress_message": "全部定向回归通过；当前问题暂无内置 checker，未启动自动观察。",
                    })
                elif monitoring_result["status"] == "failed":
                    completed = completed.model_copy(update={
                        "progress_message": (
                            "全部定向回归通过，但持续观察启动失败："
                            f"{monitoring_result.get('reason', '未知错误')}"
                        ),
                    })
                _save_run(completed)
        else:
            service.fail_job(run_id, {"message": completed.progress_message, "retryable": True, **payload})
    except Exception as exc:  # noqa: BLE001 - 后台任务必须持久化失败
        tb = traceback.format_exc()
        failed = (read_regression_run(run_id) or run).model_copy(update={
            "status": "failed",
            "stage": "failed",
            "progress_message": "回归执行异常终止。",
            "error": f"{exc}\n{tb}",
            "completed_at": _now(),
        })
        _save_run(failed)
        service.fail_job(run_id, {"message": str(exc), "traceback": tb, "retryable": True})


def start_regression_run(
    candidate_id: str,
    *,
    case_ids: list[str] | None = None,
    advance_to_monitoring: bool = True,
    actor_id: str = "admin_user",
) -> OptimizationRegressionRun:
    candidate = _candidate(candidate_id)
    cases = sync_default_regression_cases(candidate)
    selected = [item for item in cases if item.status == "ready" and (not case_ids or item.case_id in case_ids)]
    if not selected:
        raise ValueError("没有可执行的 ready regression case")
    requested = set(case_ids or [])
    missing = requested - {item.case_id for item in selected}
    if missing:
        raise ValueError(f"选择了不存在、未就绪或不属于该候选的 regression case: {sorted(missing)}")
    unconfirmed_eval_cases = [
        item.label
        for item in selected
        if item.case_type == "fixed_eval" and not item.config.get("cost_confirmed")
    ]
    if unconfirmed_eval_cases:
        raise ValueError("固定 eval 会产生真实模型与联网成本，必须在 case.config 中确认 cost_confirmed=true")
    run_id = f"regression-run-{uuid.uuid4().hex[:16]}"
    run = OptimizationRegressionRun(
        run_id=run_id,
        candidate_id=candidate_id,
        case_ids=[item.case_id for item in selected],
        progress_message="回归任务已排队。",
        advance_to_monitoring=advance_to_monitoring,
        created_by=actor_id,
        created_at=_now(),
    )
    _save_run(run)
    get_job_service().create_job(
        "optimization_regression",
        candidate_id,
        job_id=run_id,
        environment=environment_name(),
    )
    thread = threading.Thread(
        target=_execute_run,
        args=(run_id,),
        name=f"regression-{run_id[-8:]}",
        daemon=True,
    )
    thread.start()
    return run
