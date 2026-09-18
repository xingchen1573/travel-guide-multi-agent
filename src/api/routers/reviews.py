"""定向复核中心 MVP API。

这里只提供 finding/patch/review 的薄接口，业务规则集中在 ``src.review.store``，
避免后台接口再复制一套文件写入逻辑。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.review import store as review_store
from src.review import advisor as review_advisor
from src.review import executors as review_executors
from src.review import index as review_index
from src.review import monitoring as review_monitoring
from src.review import policy as review_policy
from src.review import regression as review_regression
from src.review import validation as review_validation
from src.experiments import index as experiment_index
from src.api import jobs
from src.review.models import ReviewRecommendation
from src.review.runtime import DEFAULT_REVIEW_MODEL_GROUP
from src.workspace import store

router = APIRouter(prefix="/api/projects", tags=["reviews"])


def _require_project(project_id: str) -> None:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")


class FindingCreateRequest(BaseModel):
    problem_type: str
    target: dict[str, Any] = Field(default_factory=dict)
    root_cause: str = "content"
    severity: str = "medium"
    rerun_cost: str = "medium"
    evidence_refs: list[str] = Field(default_factory=list)
    before: Any = None
    notes: str = ""


class PatchProposeRequest(BaseModel):
    finding_id: str
    file: str
    operations: list[dict[str, Any]]
    actor_id: str = "admin_user"


class DecisionRequest(BaseModel):
    actor_id: str = "admin_user"
    reason: str = ""


class VerificationRequest(BaseModel):
    verification_status: str
    verification_method: str
    disposition: str = "resolved"
    actor_type: str = "human"
    actor_id: str = "admin_user"
    notes: str = ""
    recommendation_id: str | None = None


class ItemDecisionRequest(BaseModel):
    recommendation_id: str
    item_key: str
    item_index: int = Field(ge=0)
    item_kind: str
    item_label: str
    disposition: str
    edited_text: str = ""
    edited_value: Any = None
    has_edited_value: bool = False
    notes: str = ""
    actor_type: str = "human"
    actor_id: str = "admin_user"


class RecommendationRequest(BaseModel):
    current_content: Any = None
    evidence: list[dict[str, Any]] | list[str] = Field(default_factory=list)
    light: bool = False
    model_name: str = ""
    # 定向复核默认走 GPT-Load 的 GLM 模型渠道。
    channel: str = DEFAULT_REVIEW_MODEL_GROUP
    # 默认使用只读 agentic 调查；设为 false 才走一次性结构化复核。
    agentic: bool = True


class ReviewRunRequest(BaseModel):
    mode: str = "standard"
    channel: str = DEFAULT_REVIEW_MODEL_GROUP


class ManifestConsistencyRepairRequest(BaseModel):
    observed_fingerprint: str
    actor_id: str = "admin_user"


class CodexResultRequest(BaseModel):
    result: dict[str, Any]


class PromoteRecommendationRequest(BaseModel):
    actor_id: str = "admin_user"
    confirm_replace_decisions: bool = False


class FinalizeItemDecisionsRequest(BaseModel):
    recommendation_id: str
    actor_id: str = "admin_user"


class CascadePreviewRequest(BaseModel):
    file: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    target: dict[str, Any] = Field(default_factory=dict)


class ImprovementSignalDecisionRequest(BaseModel):
    decision: str
    reason: str = ""
    actor_id: str = "admin_user"
    cause_key: str = ""
    root_cause: str = ""
    affected_layer: str = ""
    comparison_pair_id: str | None = None


class OptimizationCandidateStateRequest(BaseModel):
    status: str
    owner: str = ""
    notes: str = ""
    verification_summary: str = ""
    target_scope: str | None = None
    change_ref: str | None = None
    validation_pair_ids: list[str] | None = None
    actor_id: str = "admin_user"


class RegressionCaseRequest(BaseModel):
    case_type: str
    label: str
    project_id: str = ""
    finding_id: str = ""
    test_selector: str = ""
    scenario_id: str = ""
    expected_outcome: str = "resolved"
    config: dict[str, Any] = Field(default_factory=dict)
    actor_id: str = "admin_user"


class RegressionRunRequest(BaseModel):
    case_ids: list[str] = Field(default_factory=list)
    advance_to_monitoring: bool = True
    actor_id: str = "admin_user"


@router.get("/{project_id}/review-findings")
def list_review_findings(project_id: str, include_closed: bool = True) -> list[dict]:
    _require_project(project_id)
    review_store.backfill_findings_from_review_log(project_id)
    review_store.sync_state_findings(project_id)
    return [
        item.model_dump(mode="json")
        for item in review_store.read_findings(project_id)
        if include_closed or item.status != "closed"
    ]


@router.get("/{project_id}/review-queue")
def list_review_queue(project_id: str, include_closed: bool = True) -> list[dict]:
    _require_project(project_id)
    review_store.backfill_findings_from_review_log(project_id)
    review_store.sync_state_findings(project_id)
    return [
        item.model_dump(mode="json")
        for item in review_store.build_review_queue(project_id, include_closed=include_closed)
    ]


@router.post("/{project_id}/review-findings")
def create_review_finding(project_id: str, request: FindingCreateRequest) -> dict:
    _require_project(project_id)
    finding = review_store.create_finding(
        project_id,
        problem_type=request.problem_type,
        target=request.target,
        root_cause=request.root_cause,
        severity=request.severity,
        rerun_cost=request.rerun_cost,
        evidence_refs=request.evidence_refs,
        before=request.before,
        notes=request.notes,
        actor_type="human",
        actor_id="admin_user",
    )
    return finding.model_dump(mode="json")


@router.get("/{project_id}/review-findings/{finding_id}")
def get_review_finding(project_id: str, finding_id: str) -> dict:
    _require_project(project_id)
    finding = review_store.get_finding(project_id, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"finding {finding_id!r} 不存在")
    return finding.model_dump(mode="json")


@router.post("/{project_id}/review-patches")
def propose_review_patch(project_id: str, request: PatchProposeRequest) -> dict:
    _require_project(project_id)
    try:
        patch = review_store.propose_patch(
            project_id,
            finding_id=request.finding_id,
            file=request.file,
            operations=request.operations,
            actor_type="human",
            actor_id=request.actor_id,
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return patch.model_dump(mode="json")


@router.get("/{project_id}/review-patches/{patch_id}")
def get_review_patch(project_id: str, patch_id: str) -> dict:
    _require_project(project_id)
    try:
        patch = review_store.read_patch(project_id, patch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return patch.model_dump(mode="json")


@router.post("/{project_id}/review-cascade/preview")
def preview_review_cascade(project_id: str, request: CascadePreviewRequest) -> dict:
    """只计算 patch 影响与后续动作，不写文件、不调用模型。"""
    _require_project(project_id)
    return review_policy.build_cascade_plan(
        project_id,
        file=request.file,
        target=request.target,
        operations=request.operations,
    )


@router.post("/{project_id}/review-patches/{patch_id}/apply")
def apply_review_patch(project_id: str, patch_id: str, request: DecisionRequest) -> dict:
    _require_project(project_id)
    try:
        patch = review_store.apply_patch(project_id, patch_id, actor_type="human", actor_id=request.actor_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return patch.model_dump(mode="json")


@router.get("/{project_id}/review-validation/preview")
def preview_review_validation(project_id: str) -> dict:
    _require_project(project_id)
    return review_validation.build_pending_validation_scope(project_id)


@router.post("/{project_id}/review-validation/start", status_code=202)
def start_review_validation(project_id: str) -> dict:
    _require_project(project_id)
    preview = review_validation.build_pending_validation_scope(project_id)
    if not preview["patch_count"]:
        raise HTTPException(status_code=409, detail="当前项目没有等待验收的已应用 Patch")
    if preview["route_structure_patch_ids"]:
        raise HTTPException(status_code=409, detail="路线结构 Patch 请返回既有路线变更流程处理")
    if preview["unresolved_patch_ids"]:
        raise HTTPException(status_code=409, detail="部分 Patch 缺少可定向验收的精确定位")
    try:
        jobs.trigger_review_validation(project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "queued", "scope": preview}


@router.get("/{project_id}/review-validation/status")
def get_review_validation_status(project_id: str) -> dict | None:
    _require_project(project_id)
    return jobs.get_status("review_validation", project_id)


@router.post("/{project_id}/review-patches/{patch_id}/reject")
def reject_review_patch(project_id: str, patch_id: str, request: DecisionRequest) -> dict:
    _require_project(project_id)
    try:
        patch = review_store.reject_patch(
            project_id,
            patch_id,
            actor_type="human",
            actor_id=request.actor_id,
            reason=request.reason,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return patch.model_dump(mode="json")


@router.post("/{project_id}/review-patches/{patch_id}/rollback")
def rollback_review_patch(project_id: str, patch_id: str, request: DecisionRequest) -> dict:
    _require_project(project_id)
    try:
        patch = review_store.rollback_patch(project_id, patch_id, actor_type="human", actor_id=request.actor_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return patch.model_dump(mode="json")


@router.post("/{project_id}/review-findings/{finding_id}/verification")
def record_review_verification(project_id: str, finding_id: str, request: VerificationRequest) -> dict:
    _require_project(project_id)
    try:
        finding = review_store.record_verification(
            project_id,
            finding_id,
            verification_status=request.verification_status,
            verification_method=request.verification_method,
            disposition=request.disposition,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            notes=request.notes,
            recommendation_id=request.recommendation_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return finding.model_dump(mode="json")


@router.get("/{project_id}/review-findings/{finding_id}/item-decisions")
def get_review_item_decisions(project_id: str, finding_id: str, recommendation_id: str) -> list[dict]:
    _require_project(project_id)
    finding = review_store.get_finding(project_id, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"finding {finding_id!r} 不存在")
    return [item.model_dump(mode="json") for item in review_store.read_item_decisions(project_id, recommendation_id)]


@router.post("/{project_id}/review-findings/{finding_id}/item-decisions")
def record_review_item_decision(project_id: str, finding_id: str, request: ItemDecisionRequest) -> dict:
    _require_project(project_id)
    try:
        decision = review_store.record_item_decision(
            project_id,
            finding_id,
            request.recommendation_id,
            item_key=request.item_key,
            item_index=request.item_index,
            item_kind=request.item_kind,
            item_label=request.item_label,
            disposition=request.disposition,
            edited_text=request.edited_text,
            edited_value=request.edited_value,
            has_edited_value=request.has_edited_value,
            notes=request.notes,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return decision.model_dump(mode="json")


@router.post("/{project_id}/review-findings/{finding_id}/finalize")
def finalize_review_item_decisions(project_id: str, finding_id: str, request: FinalizeItemDecisionsRequest) -> dict:
    _require_project(project_id)
    try:
        result = review_store.finalize_item_decisions(
            project_id,
            finding_id,
            request.recommendation_id,
            actor_id=request.actor_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@router.post("/{project_id}/review-findings/{finding_id}/recommendation")
def generate_review_recommendation(project_id: str, finding_id: str, request: RecommendationRequest) -> dict:
    _require_project(project_id)
    try:
        context = review_store.build_review_context(project_id, finding_id)
        if request.agentic:
            from src.review.agent import generate_agentic_recommendation

            recommendation, result = generate_agentic_recommendation(
                project_id,
                finding_id,
                channel=request.channel,
            )
        else:
            recommendation, result = review_advisor.generate_recommendation(
                project_id,
                finding_id,
                current_content=request.current_content if request.current_content is not None else context["current_content"],
                evidence=request.evidence or context["evidence"],
                light=request.light,
                model_name=request.model_name,
                channel=request.channel,
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # 模型供应商错误需要由调用方看到，但不泄露堆栈
        raise HTTPException(status_code=502, detail=f"定向复核模型调用失败: {exc}") from exc
    return {"recommendation": recommendation.model_dump(mode="json"), "result": result.model_dump(mode="json"), "context": context}


@router.get("/{project_id}/review-findings/{finding_id}/context")
def get_review_context(project_id: str, finding_id: str) -> dict:
    _require_project(project_id)
    try:
        return review_store.build_review_context(project_id, finding_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project_id}/review-findings/{finding_id}/deterministic-repair")
def execute_deterministic_review_repair(
    project_id: str,
    finding_id: str,
    request: ManifestConsistencyRepairRequest,
) -> dict:
    """Apply a fingerprint-guarded state reconciliation without an LLM."""
    _require_project(project_id)
    from src.review.manifest_consistency import execute_manifest_consistency_repair

    try:
        return execute_manifest_consistency_repair(
            project_id,
            finding_id,
            expected_fingerprint=request.observed_fingerprint,
            actor_id=request.actor_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{project_id}/review-findings/{finding_id}/recommendation")
def get_review_recommendation(project_id: str, finding_id: str) -> dict:
    _require_project(project_id)
    finding = review_store.get_finding(project_id, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"finding {finding_id!r} 不存在")
    if not finding.recommendation_id:
        raise HTTPException(status_code=404, detail="该 finding 尚未生成模型建议")
    recommendation = review_store.read_recommendation(project_id, finding.recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="模型建议记录不存在")
    return recommendation.model_dump(mode="json")


@router.get("/{project_id}/review-findings/{finding_id}/recommendations")
def list_review_recommendation_history(project_id: str, finding_id: str) -> list[dict]:
    _require_project(project_id)
    try:
        history = review_store.build_recommendation_history(project_id, finding_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [item.model_dump(mode="json") for item in history]


@router.get("/{project_id}/review-recommendations/{recommendation_id}")
def get_review_recommendation_by_id(project_id: str, recommendation_id: str) -> dict:
    _require_project(project_id)
    recommendation = review_store.read_recommendation(project_id, recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="模型建议记录不存在")
    return recommendation.model_dump(mode="json")


@router.post("/{project_id}/review-findings/{finding_id}/runs")
def create_review_runs(project_id: str, finding_id: str, request: ReviewRunRequest) -> list[dict]:
    _require_project(project_id)
    try:
        runs = review_executors.start_runs(
            project_id,
            finding_id,
            mode=request.mode,
            channel=request.channel,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return [run.model_dump(mode="json") for run in runs]


@router.get("/{project_id}/review-findings/{finding_id}/runs")
def list_review_runs(project_id: str, finding_id: str) -> list[dict]:
    _require_project(project_id)
    if review_store.get_finding(project_id, finding_id) is None:
        raise HTTPException(status_code=404, detail=f"finding {finding_id!r} 不存在")
    return [run.model_dump(mode="json") for run in review_store.read_review_runs(project_id, finding_id)]


@router.get("/{project_id}/review-runs/{run_id}")
def get_review_run(project_id: str, run_id: str) -> dict:
    _require_project(project_id)
    run = review_store.read_review_run(project_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"review run {run_id!r} 不存在")
    return run.model_dump(mode="json")


@router.get("/{project_id}/review-runs/{run_id}/bundle")
def get_review_run_bundle(project_id: str, run_id: str) -> dict:
    _require_project(project_id)
    try:
        return review_executors.read_run_bundle(project_id, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc


@router.post("/{project_id}/review-runs/{run_id}/result")
def import_review_run_result(project_id: str, run_id: str, request: CodexResultRequest) -> dict:
    _require_project(project_id)
    try:
        run = review_executors.import_codex_result(project_id, run_id, request.result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.model_dump(mode="json")


@router.post("/{project_id}/review-runs/{run_id}/promote")
def promote_review_run(project_id: str, run_id: str, request: PromoteRecommendationRequest) -> dict:
    _require_project(project_id)
    run = review_store.read_review_run(project_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"review run {run_id!r} 不存在")
    if run.status != "succeeded" or not run.recommendation_id:
        raise HTTPException(status_code=409, detail="只有已成功并生成候选建议的运行可以设为当前推荐")
    try:
        current_finding = review_store.get_finding(project_id, run.finding_id)
        if (
            current_finding
            and current_finding.recommendation_id
            and current_finding.recommendation_id != run.recommendation_id
        ):
            existing_decisions = review_store.read_item_decisions(project_id, current_finding.recommendation_id)
            if existing_decisions and not request.confirm_replace_decisions:
                raise RuntimeError(
                    f"replace_confirmation_required: 当前推荐已有 {len(existing_decisions)} 条人工决定；确认后可替换，旧决定仍保留在历史记录中"
                )
        review_store.assert_review_run_baseline_current(project_id, run)
        finding = review_store.promote_recommendation(
            project_id,
            run.finding_id,
            run.recommendation_id,
            actor_id=request.actor_id,
        )
        review_store.update_review_run(project_id, run_id, promoted_at=review_store._now())
        for previous in review_store.read_review_runs(project_id, run.finding_id):
            if previous.run_id != run_id and previous.recommendation_id and not previous.superseded_at:
                review_store.update_review_run(project_id, previous.run_id, superseded_at=review_store._now())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return finding.model_dump(mode="json")


@router.get("/{project_id}/review-comparisons/{comparison_group_id}")
def get_review_comparison(project_id: str, comparison_group_id: str) -> dict:
    _require_project(project_id)
    result = review_executors.comparison(project_id, comparison_group_id)
    if not result["runs"]:
        raise HTTPException(status_code=404, detail="对照运行不存在")
    return result


@router.get("/{project_id}/review-state")
def validate_review_state(project_id: str) -> dict:
    _require_project(project_id)
    review_store.sync_state_findings(project_id)
    return review_store.validate_project_state(project_id)


@router.get("/{project_id}/optimization-candidates")
def optimization_candidates(project_id: str, threshold: int = 3) -> list[dict]:
    _require_project(project_id)
    return [
        item
        for item in review_index.aggregate_optimization_candidates(threshold=max(1, threshold))
        if project_id in item.get("project_ids", [])
    ]


@router.get("/{project_id}/improvement-signals")
def list_improvement_signals(project_id: str, decision: str | None = None) -> list[dict]:
    _require_project(project_id)
    review_store.sync_improvement_signals_from_artifacts(project_id)
    return [
        item.model_dump(mode="json")
        for item in review_store.read_improvement_signals(project_id)
        if decision is None or item.decision == decision
    ]


@router.post("/{project_id}/improvement-signals/{signal_id}/decision")
def decide_improvement_signal(
    project_id: str,
    signal_id: str,
    request: ImprovementSignalDecisionRequest,
) -> dict:
    _require_project(project_id)
    if request.comparison_pair_id:
        pair = experiment_index.get_comparison_pair(request.comparison_pair_id)
        if pair is None:
            raise HTTPException(status_code=400, detail="Comparison Pair 不存在")
        if pair.get("status") != "complete":
            raise HTTPException(status_code=400, detail="Comparison Pair 尚未完整，不能作为反哺来源")
        if project_id not in {
            pair["codex_project_id"],
            pair["langgraph_project_id"],
        }:
            raise HTTPException(status_code=400, detail="Signal Project 不属于所选 Comparison Pair")
    try:
        signal = review_store.decide_improvement_signal(
            project_id,
            signal_id,
            decision=request.decision,
            reason=request.reason,
            actor_id=request.actor_id,
            cause_key=request.cause_key,
            root_cause=request.root_cause,
            affected_layer=request.affected_layer,
            comparison_pair_id=request.comparison_pair_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    review_index.aggregate_optimization_candidates()
    return signal.model_dump(mode="json")


index_router = APIRouter(prefix="/api/review-index", tags=["reviews"])

executor_router = APIRouter(prefix="/api", tags=["reviews"])


@executor_router.get("/review-executors")
def review_executor_registry() -> list[dict]:
    return [item.model_dump(mode="json") for item in review_executors.list_executors()]


@index_router.get("/findings")
def global_review_findings(include_closed: bool = True) -> list[dict]:
    return review_index.aggregate_findings(include_closed=include_closed)


@index_router.get("/optimization-candidates")
def global_optimization_candidates(threshold: int = 3) -> list[dict]:
    review_index.aggregate_optimization_candidates(threshold=max(1, threshold))
    return review_index.read_global_candidates()


@index_router.patch("/optimization-candidates/{candidate_id}")
def update_optimization_candidate(
    candidate_id: str,
    request: OptimizationCandidateStateRequest,
) -> dict:
    if request.validation_pair_ids:
        pairs = {
            pair["pair_id"]: pair
            for pair in experiment_index.list_comparison_pairs()
        }
        for pair_id in request.validation_pair_ids:
            pair = pairs.get(pair_id)
            if pair is None:
                raise HTTPException(status_code=400, detail=f"Comparison Pair 不存在: {pair_id}")
            if pair["status"] != "complete":
                raise HTTPException(status_code=400, detail=f"Comparison Pair 尚未完成: {pair_id}")
    try:
        state = review_index.update_candidate_state(
            candidate_id,
            status=request.status,
            owner=request.owner,
            notes=request.notes,
            verification_summary=request.verification_summary,
            target_scope=request.target_scope,
            change_ref=request.change_ref,
            validation_pair_ids=request.validation_pair_ids,
            actor_id=request.actor_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return state.model_dump(mode="json")


@index_router.get("/optimization-candidates/{candidate_id}/regression-cases")
def list_candidate_regression_cases(candidate_id: str) -> list[dict]:
    try:
        candidates = {
            item["candidate_id"]: item
            for item in review_index.aggregate_optimization_candidates()
        }
        candidate = candidates[candidate_id]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Optimization Candidate 不存在: {candidate_id}") from exc
    review_regression.sync_default_regression_cases(candidate)
    return [
        item.model_dump(mode="json")
        for item in review_regression.read_regression_cases(candidate_id)
    ]


@index_router.post("/optimization-candidates/{candidate_id}/regression-cases")
def create_candidate_regression_case(candidate_id: str, request: RegressionCaseRequest) -> dict:
    try:
        case = review_regression.create_regression_case(
            candidate_id,
            case_type=request.case_type,
            label=request.label,
            project_id=request.project_id,
            finding_id=request.finding_id,
            test_selector=request.test_selector,
            scenario_id=request.scenario_id,
            expected_outcome=request.expected_outcome,
            config=request.config,
            actor_id=request.actor_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return case.model_dump(mode="json")


@index_router.get("/optimization-candidates/{candidate_id}/regression-runs")
def list_candidate_regression_runs(candidate_id: str) -> list[dict]:
    return [
        item.model_dump(mode="json")
        for item in review_regression.read_regression_runs(candidate_id)
    ]


@index_router.get("/optimization-candidates/{candidate_id}/monitoring")
def get_candidate_monitoring(candidate_id: str) -> dict:
    candidates = {
        item["candidate_id"]: item
        for item in review_index.aggregate_optimization_candidates()
    }
    if candidate_id not in candidates:
        raise HTTPException(status_code=404, detail=f"Optimization Candidate 不存在: {candidate_id}")
    return review_monitoring.monitoring_summary(candidate_id)


@index_router.get("/optimization-candidates/{candidate_id}/monitoring-observations")
def list_candidate_monitoring_observations(candidate_id: str) -> list[dict]:
    candidates = {
        item["candidate_id"]: item
        for item in review_index.aggregate_optimization_candidates()
    }
    if candidate_id not in candidates:
        raise HTTPException(status_code=404, detail=f"Optimization Candidate 不存在: {candidate_id}")
    return [
        item.model_dump(mode="json")
        for item in review_monitoring.read_monitoring_observations(candidate_id)
    ]


@index_router.post("/optimization-candidates/{candidate_id}/regression-runs")
def start_candidate_regression_run(candidate_id: str, request: RegressionRunRequest) -> dict:
    try:
        run = review_regression.start_regression_run(
            candidate_id,
            case_ids=request.case_ids or None,
            advance_to_monitoring=request.advance_to_monitoring,
            actor_id=request.actor_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.model_dump(mode="json")


@index_router.get("/regression-runs/{run_id}")
def get_candidate_regression_run(run_id: str) -> dict:
    run = review_regression.read_regression_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Regression Run 不存在: {run_id}")
    return run.model_dump(mode="json")


@index_router.get("/eval-scenarios")
def list_review_eval_scenarios() -> list[str]:
    return review_regression.list_eval_scenarios()


@index_router.get("/quality-metrics")
def global_quality_metrics() -> dict:
    return review_index.quality_metrics()
