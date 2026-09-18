"""复核运维执行器。

执行器只产出候选 recommendation；是否成为 finding 当前推荐由人工显式提升。
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.review import skill_bridge
from src.review import store as review_store
from src.review.models import ReviewExecutor, ReviewRun
from src.review.runtime import DEFAULT_REVIEW_MODEL_GROUP
from src.execution_state import (
    configure_execution_context,
    environment_name,
    get_current_instance_id,
    get_job_service,
)
from src.workspace import store as workspace_store


EXECUTORS = (
    ReviewExecutor(
        executor_id="standard_model",
        name="标准模型",
        description="运行现有只读旅游定向复核工作流，结果先保存为候选。",
        execution_mode="server",
        cost_level="medium",
        supports_tools=True,
        supports_web=True,
    ),
    ReviewExecutor(
        executor_id="codex_skill",
        name="Codex 任务包",
        description="创建冻结任务包；在外部使用 Codex Skill 复核后，再导入结构化结果。",
        execution_mode="bundle_bridge",
        cost_level="high",
        supports_tools=True,
        supports_web=True,
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_executors() -> list[ReviewExecutor]:
    return [item.model_copy() for item in EXECUTORS if item.enabled]


def _artifact_dir(project_id: str, run_id: str) -> Path:
    path = workspace_store.workspace_dir(project_id) / "review_runs" / "_executions" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _summarize_run_error(exc: Exception) -> tuple[str, bool]:
    """将 Provider 异常转成可展示摘要。

    原始错误仍保存在运行产物中；这里避免把 Cloudflare 验证页或
    其他大段 HTML 当成业务错误铺到页面上。
    """
    raw = str(exc).strip()
    lowered = raw.lower()
    if (
        "challenges.cloudflare.com" in lowered
        or "_cf_chl_opt" in lowered
        or ("<title>just a moment...</title>" in lowered and "enable javascript" in lowered)
    ):
        ray_match = re.search(r"cRay:\s*['\"]([^'\"]+)", raw)
        ray = f"（Ray ID: {ray_match.group(1)}）" if ray_match else ""
        return (
            "上游模型接口触发 Cloudflare 人机验证，未返回模型结果"
            f"{ray}。请稍后重试或切换模型渠道。",
            True,
        )
    if "<html" in lowered or "<!doctype html" in lowered:
        return "上游模型服务返回了网页错误响应，未生成复核结果。", True
    compact = " ".join(raw.split()) or type(exc).__name__
    if len(compact) > 600:
        compact = compact[:600] + "…"
    return compact, False


def _create_run(
    project_id: str,
    finding_id: str,
    *,
    executor_id: str,
    bundle: dict[str, Any],
    comparison_group_id: str | None,
    channel: str,
) -> ReviewRun:
    run_id = f"review-run-{uuid.uuid4().hex[:12]}"
    baseline = bundle.get("baseline") or {}
    artifact_dir = _artifact_dir(project_id, run_id)
    bundle_path = artifact_dir / "review_bundle.json"
    review_store._write_json_atomic(bundle_path, bundle)
    run = ReviewRun(
        run_id=run_id,
        project_id=project_id,
        finding_id=finding_id,
        executor_id=executor_id,
        comparison_group_id=comparison_group_id,
        channel=channel if executor_id == "standard_model" else "",
        bundle_sha256=str(bundle.get("bundle_sha256") or ""),
        baseline_file_sha256=str(baseline.get("file_sha256") or ""),
        baseline_finding_revision=int(baseline.get("finding_revision") or 0),
        status="queued" if executor_id == "standard_model" else "waiting_user",
        stage="freeze_baseline" if executor_id == "standard_model" else "await_codex_result",
        progress_message=(
            "已冻结目标版本，等待标准模型开始复核。"
            if executor_id == "standard_model"
            else "Codex 任务包已生成，等待外部复核并导入结果。"
        ),
        artifact_paths={"bundle": str(bundle_path.relative_to(workspace_store.workspace_dir(project_id)))},
        metrics={"scope_count": len(bundle.get("scope") or [])},
        created_at=_now(),
    )
    if executor_id == "standard_model":
        get_job_service().create_job(
            "review_run",
            run_id,
            job_id=run_id,
            environment=environment_name(),
        )
    return review_store.save_review_run(project_id, run)


def _run_standard(project_id: str, run_id: str) -> None:
    run = review_store.read_review_run(project_id, run_id)
    if run is None:
        return
    bundle = read_run_bundle(project_id, run_id)
    service = get_job_service()
    owner_instance_id = get_current_instance_id()
    if owner_instance_id is None:
        owner_instance_id = service.register_instance(
            role="standalone",
            environment=environment_name(),
        )
        configure_execution_context(service, owner_instance_id)
    if not service.start_job(run_id, owner_instance_id):
        return
    try:
        review_store.update_review_run(
            project_id,
            run_id,
            status="running",
            stage="read_context",
            progress_message="正在读取争议字段、历史失败点和目的地素材候选。",
            started_at=_now(),
            error=None,
        )
        service.update_progress(run_id, status_reason="正在读取复核上下文")
        skill_bridge._assert_baseline_unchanged(project_id, bundle)
        review_store.update_review_run(
            project_id,
            run_id,
            stage="generate_result",
            progress_message="正在调用标准模型生成逐条结构化复核结果。",
        )
        service.update_progress(run_id, status_reason="正在生成复核结果")
        from src.review.agent import generate_agentic_recommendation

        recommendation, result = generate_agentic_recommendation(
            project_id,
            run.finding_id,
            channel=run.channel or DEFAULT_REVIEW_MODEL_GROUP,
            promote=False,
        )
        skill_bridge._assert_baseline_unchanged(project_id, bundle)
        result_path = _artifact_dir(project_id, run_id) / "standard_result.json"
        review_store._write_json_atomic(
            result_path,
            {
                "recommendation": recommendation.model_dump(mode="json"),
                "model_result": result.model_dump(mode="json"),
                "bundle_sha256": run.bundle_sha256,
            },
        )
        if service.succeed_job(run_id, {"recommendation_id": recommendation.recommendation_id}):
            review_store.update_review_run(
                project_id,
                run_id,
                status="succeeded",
                stage="completed",
                progress_message="标准模型复核完成，已生成候选建议，尚未设为当前推荐。",
                recommendation_id=recommendation.recommendation_id,
                artifact_paths={**run.artifact_paths, "result": str(result_path.relative_to(workspace_store.workspace_dir(project_id)))},
                metrics={
                    **run.metrics,
                    "claim_count": len(recommendation.claim_reviews),
                    "context_version": recommendation.context_version,
                },
                completed_at=_now(),
            )
    except Exception as exc:  # noqa: BLE001 - 后台运行必须转成可见失败状态
        error_message, retryable = _summarize_run_error(exc)
        error_artifact = _artifact_dir(project_id, run_id) / "technical_error.json"
        artifact_paths = dict(run.artifact_paths)
        try:
            review_store._write_json_atomic(
                error_artifact,
                {
                    "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                    "message": str(exc),
                },
            )
            artifact_paths["error"] = str(
                error_artifact.relative_to(workspace_store.workspace_dir(project_id))
            )
        except OSError:
            pass
        if service.fail_job(run_id, {"message": error_message, "retryable": retryable}):
            review_store.update_review_run(
                project_id,
                run_id,
                status="failed",
                stage="failed",
                progress_message="标准模型复核执行失败，没有产生业务结论。",
                error=error_message,
                artifact_paths=artifact_paths,
                completed_at=_now(),
            )


def start_runs(
    project_id: str,
    finding_id: str,
    *,
    mode: str,
    channel: str = DEFAULT_REVIEW_MODEL_GROUP,
) -> list[ReviewRun]:
    finding = review_store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    if mode not in {"standard", "codex", "compare"}:
        raise ValueError(f"不支持的复核运行模式: {mode}")
    from src.review.agent import _assert_model_reviewable

    _assert_model_reviewable(finding)
    bundle = skill_bridge.export_review_bundle(project_id, finding_id)
    comparison_group_id = f"review-compare-{uuid.uuid4().hex[:12]}" if mode == "compare" else None
    executor_ids = {
        "standard": ["standard_model"],
        "codex": ["codex_skill"],
        "compare": ["standard_model", "codex_skill"],
    }[mode]
    runs = [
        _create_run(
            project_id,
            finding_id,
            executor_id=executor_id,
            bundle=bundle,
            comparison_group_id=comparison_group_id,
            channel=channel,
        )
        for executor_id in executor_ids
    ]
    for run in runs:
        if run.executor_id == "standard_model":
            threading.Thread(
                target=_run_standard,
                args=(project_id, run.run_id),
                daemon=True,
            ).start()
    return runs


def read_run_bundle(project_id: str, run_id: str) -> dict[str, Any]:
    run = review_store.read_review_run(project_id, run_id)
    if run is None:
        raise KeyError(f"review run 不存在: {run_id}")
    bundle_path = workspace_store.workspace_dir(project_id) / run.artifact_paths["bundle"]
    if not bundle_path.exists():
        raise FileNotFoundError("冻结复核任务包不存在")
    import json

    return json.loads(bundle_path.read_text(encoding="utf-8"))


def import_codex_result(project_id: str, run_id: str, result: dict[str, Any]) -> ReviewRun:
    run = review_store.read_review_run(project_id, run_id)
    if run is None:
        raise KeyError(f"review run 不存在: {run_id}")
    if run.executor_id != "codex_skill":
        raise ValueError("只有 Codex Skill 任务包运行可以导入外部结果")
    if run.status not in {"waiting_user", "waiting_external"}:
        raise ValueError(f"当前运行状态不允许导入结果: {run.status}")
    bundle = read_run_bundle(project_id, run_id)
    review_store.update_review_run(
        project_id,
        run_id,
        status="validating",
        stage="validate_result",
        progress_message="正在校验 bundle hash、冻结基线、条目覆盖和证据引用。",
        started_at=run.started_at or _now(),
        error=None,
    )
    try:
        recommendation = skill_bridge.import_review_result(
            project_id,
            bundle,
            result,
            promote=False,
            review_run_id=run_id,
            source_type="codex_skill",
        )
        result_path = _artifact_dir(project_id, run_id) / "codex_result.json"
        review_store._write_json_atomic(result_path, result)
        return review_store.update_review_run(
            project_id,
            run_id,
            status="succeeded",
            stage="completed",
            progress_message="Codex 复核结果校验通过，已生成候选建议，尚未设为当前推荐。",
            recommendation_id=recommendation.recommendation_id,
            artifact_paths={**run.artifact_paths, "result": str(result_path.relative_to(workspace_store.workspace_dir(project_id)))},
            metrics={**run.metrics, "claim_count": len(recommendation.claim_reviews), "validation": "passed"},
            completed_at=_now(),
        )
    except Exception as exc:
        review_store.update_review_run(
            project_id,
            run_id,
            status="failed",
            stage="failed",
            progress_message="Codex 复核结果未通过确定性校验，没有产生业务结论。",
            error=str(exc),
            completed_at=_now(),
        )
        raise


def comparison(project_id: str, comparison_group_id: str) -> dict[str, Any]:
    runs = [
        run for run in review_store.read_review_runs(project_id)
        if run.comparison_group_id == comparison_group_id
    ]
    recommendations: dict[str, Any] = {}
    by_path: dict[str, dict[str, Any]] = {}
    for run in runs:
        recommendation = (
            review_store.read_recommendation(project_id, run.recommendation_id)
            if run.recommendation_id
            else None
        )
        recommendations[run.executor_id] = recommendation.model_dump(mode="json") if recommendation else None
        if recommendation is None:
            continue
        for index, claim in enumerate(recommendation.claim_reviews):
            key = claim.target_path or f"claim:{index}"
            by_path.setdefault(key, {})[run.executor_id] = claim.model_dump(mode="json")
    items = []
    for target_path, claims in by_path.items():
        values = list(claims.values())
        comparable = len(values) == len(runs) and len(values) > 1
        aligned = comparable and len({
            (
                value.get("verdict"),
                value.get("content_action") or value.get("action"),
                value.get("evidence_action") or value.get("action"),
            )
            for value in values
        }) == 1
        items.append({"target_path": target_path, "claims": claims, "aligned": aligned})
    return {
        "comparison_group_id": comparison_group_id,
        "bundle_sha256": runs[0].bundle_sha256 if runs else "",
        "same_baseline": len({run.bundle_sha256 for run in runs}) <= 1,
        "runs": [run.model_dump(mode="json") for run in runs],
        "recommendations": recommendations,
        "items": items,
    }
