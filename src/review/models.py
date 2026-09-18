"""定向复核闭环的数据模型。

模型只负责结构化约束；项目级追加存储和 patch 应用在 ``src.review.store``。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FindingDisposition(str, Enum):
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"
    DEFERRED = "deferred"
    NEEDS_RERUN = "needs_rerun"


class FindingRootCause(str, Enum):
    CONTENT = "content"
    EVIDENCE = "evidence"
    DATA = "data"
    MODEL = "model"
    CODE = "code"
    PRESENTATION = "presentation"
    POLICY = "policy"


class TravelReviewProposedOperation(BaseModel):
    """模型提出的修改意图；不是可直接应用的 PatchRecord。"""

    item_key: str = Field(min_length=1)
    operation: Literal["replace", "add", "remove"]
    path: str = Field(min_length=1)
    proposed_value: Any = None
    reason: str = Field(min_length=1)
    requires_human_confirmation: bool = True


class ReviewClaimEvidence(BaseModel):
    """A single disputed claim with the evidence the reviewer actually used."""

    claim: str = Field(min_length=1, description="争议点名称或需要核验的具体断言。")
    target_path: str = Field(
        default="",
        description="争议点对应当前 JSON 的精确 JSON Pointer；无法定位时留空。",
    )
    original_text: str = Field(min_length=1, description="当前攻略中的原始表述，必须原样填写。")
    evidence_refs: list[str] = Field(
        min_length=1,
        description="本次调查实际读取过的 chunk_id、URL 或 search:<query>；不得填写未读取的来源。",
    )
    evidence_excerpt: str = Field(
        min_length=1,
        description="支持判断的原文短摘录，不得改写；无命中时填写检索结果说明。",
    )
    verdict: Literal["supported", "partial", "unsupported", "contradicted", "ambiguous"] = Field(
        description="证据对当前原始表述的支持程度。"
    )
    action: Literal["keep", "narrow", "add_source", "repair_reference", "sync_status", "rerun"] = Field(
        description="向后兼容的主处理动作；新结果还应分别填写 content_action 和 evidence_action。"
    )
    content_action: Literal["keep", "narrow", "rerun"] | None = Field(
        default=None,
        description="正文处理动作；用于区分正文是否需要改写。旧 recommendation 可为空。",
    )
    evidence_action: Literal["keep", "add_source", "repair_reference", "sync_status"] | None = Field(
        default=None,
        description="证据处理动作；用于区分引用修复与正文修改。旧 recommendation 可为空。",
    )
    proposed_text: str = ""
    rationale: str = Field(min_length=1, description="说明证据如何支持 verdict 与 action。")
    confidence: Literal["low", "medium", "high"] = "medium"


class ReviewRecommendation(BaseModel):
    """模型提供给人工审核的建议包。"""

    recommendation_id: str
    finding_id: str
    decision: str = "human_review"
    confidence: str = "medium"
    risk_level: str = "medium"
    summary: str = ""
    proposed_text: str = ""
    claim_reviews: list[ReviewClaimEvidence] = Field(default_factory=list)
    proposed_operations: list[TravelReviewProposedOperation] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    recommended_action: str = "human_review"
    model_name: str = ""
    model_group: str = ""
    model_provider: str = ""
    context_version: str = "review-context-v2"
    source_run_id: str | None = None
    created_at: str = ""


class ReviewExecutor(BaseModel):
    """复核执行器注册信息，供运维页面发现能力。"""

    executor_id: str
    name: str
    description: str = ""
    enabled: bool = True
    execution_mode: Literal["server", "bundle_bridge"] = "server"
    cost_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    supports_tools: bool = False
    supports_web: bool = False


class ReviewRun(BaseModel):
    """一次模型或 Skill 复核执行；结果默认只是候选建议。"""

    run_id: str
    project_id: str
    finding_id: str
    executor_id: str
    comparison_group_id: str | None = None
    channel: str = ""
    bundle_sha256: str = ""
    baseline_file_sha256: str = ""
    baseline_finding_revision: int = 0
    status: Literal[
        "queued",
        "running",
        "waiting_user",
        "waiting_external",
        "validating",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    ] = "queued"
    stage: str = "freeze_baseline"
    progress_message: str = ""
    recommendation_id: str | None = None
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    status_reason: str | None = None
    health_status: Literal["healthy", "stalled", "orphaned"] = "healthy"
    owner_instance_id: str | None = None
    last_heartbeat_at: str | None = None
    last_progress_at: str | None = None
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    promoted_at: str | None = None
    superseded_at: str | None = None


class ReviewItemDecision(BaseModel):
    """人工对建议包中单个条目的追加式判断。"""

    decision_id: str
    project_id: str
    finding_id: str
    recommendation_id: str
    item_key: str
    item_index: int = Field(ge=0)
    item_kind: Literal["claim", "legacy_suggestion"]
    item_label: str = Field(min_length=1)
    disposition: Literal["accepted", "keep_original", "edited", "deferred", "needs_rerun"]
    edited_text: str = ""
    edited_value: Any = None
    has_edited_value: bool = False
    notes: str = ""
    actor_type: str = "human"
    actor_id: str = "admin_user"
    created_at: str = ""


class ReviewRecommendationHistoryEntry(BaseModel):
    """一个 finding 下 recommendation 的只读审计投影。"""

    recommendation: ReviewRecommendation
    is_current: bool = False
    lifecycle_status: Literal["current", "superseded", "candidate"] = "candidate"
    decision_count: int = 0
    total_items: int = 0
    run_id: str | None = None
    run_status: str | None = None
    executor_id: str | None = None
    promoted_at: str | None = None
    superseded_at: str | None = None


class PatchOperation(BaseModel):
    """受限 JSON Pointer 操作。

    MVP 支持 replace/add/remove；每条操作记录应用前的值和是否存在，便于
    版本校验、冲突检测与反向 rollback。
    """

    op: Literal["replace", "add", "remove"]
    path: str
    value: Any = None
    before: Any = None
    before_exists: bool = False


class PatchRecord(BaseModel):
    patch_id: str
    project_id: str
    finding_id: str
    file: str
    base_sha256: str
    operations: list[PatchOperation] = Field(default_factory=list)
    status: str = "proposed"  # proposed/applied/rejected/patch_conflict/rolled_back
    actor_type: str = "model"
    actor_id: str = ""
    parent_patch_id: str | None = None
    created_at: str = ""
    applied_at: str | None = None
    updated_at: str = ""
    impact_class: str = "mechanical"
    cascade_plan: dict[str, Any] = Field(default_factory=dict)


class ReviewFinding(BaseModel):
    finding_id: str
    project_id: str
    baseline_version: str = ""
    target: dict[str, Any] = Field(default_factory=dict)
    problem_type: str
    disposition: FindingDisposition | str = FindingDisposition.DEFERRED
    root_cause: FindingRootCause | str = FindingRootCause.CONTENT
    severity: str = "medium"
    rerun_cost: str = "medium"
    evidence_refs: list[str] = Field(default_factory=list)
    before: Any = None
    after: Any = None
    repair_action: str = ""
    verification_method: str = ""
    verification_status: str = "pending"
    verification_actor_type: str = ""
    verification_actor_id: str = ""
    needs_rerun: bool = False
    status: Literal["open", "in_review", "awaiting_apply", "verifying", "closed"] | str = "open"
    patch_id: str | None = None
    recommendation_id: str | None = None
    occurrences: int = 1
    revision: int = 1
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    source_review_id: str | None = None
    issue_summary: str = ""
    review_question: str = ""
    observed_value: Any = None
    canonical_key: str = ""
    finding_kind: Literal[
        "content_review",
        "state_residue",
        "consistency",
        "derived_mirror",
    ] = "content_review"
    observed_fingerprint: str = ""
    resolved_fingerprint: str = ""
    superseded_by: str | None = None


class ReviewQueueGroup(BaseModel):
    """复核队列的只读展示投影；所有写操作仍落到 primary finding。"""

    group_id: str
    primary_finding_id: str
    related_finding_ids: list[str] = Field(default_factory=list)
    title: str
    summary: str = ""
    object_label: str = ""
    finding_kind: str = "content_review"
    root_cause: str = "content"
    effective_status: str = "open"
    severity: str = "medium"
    model_status: str = "not_started"
    decision_progress: dict[str, int] = Field(default_factory=lambda: {"decided": 0, "total": 0})
    mirror_count: int = 0
    technical: dict[str, Any] = Field(default_factory=dict)


class ReviewDecisionFinalization(BaseModel):
    finding: ReviewFinding
    outcome: Literal["closed", "awaiting_apply", "deferred", "needs_rerun"]
    patch_id: str | None = None
    effective_status: str = "needs_review"
    message: str = ""


class TravelReviewSystemObservation(BaseModel):
    """复核过程中发现的代码、数据或展示层问题。"""

    root_cause: FindingRootCause | str
    problem_type: str = Field(min_length=1)
    cause_key: str = ""
    affected_layer: str = ""
    summary: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    representative_targets: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    suggested_test: str = ""
    severity: Literal["low", "medium", "high"] = "medium"
    confidence: Literal["low", "medium", "high"] = "medium"
    migration_required: bool = False
    regression_scope: list[str] = Field(default_factory=list)
    is_systemic: bool = False


class ImprovementSignal(BaseModel):
    """经确定性门禁导入、等待人工裁决的系统改进信号。"""

    signal_id: str
    project_id: str
    finding_id: str
    recommendation_id: str
    review_run_id: str = ""
    source_type: str = "codex_skill"
    comparison_pair_id: str | None = None
    cause_key: str
    root_cause: FindingRootCause | str
    affected_layer: str
    problem_type: str
    hypothesis: str
    evidence_refs: list[str] = Field(default_factory=list)
    representative_targets: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    suggested_test: str = ""
    severity: Literal["low", "medium", "high"] = "medium"
    confidence: Literal["low", "medium", "high"] = "medium"
    migration_required: bool = False
    regression_scope: list[str] = Field(default_factory=list)
    decision: Literal["pending", "accepted", "dismissed", "deferred", "merged", "reclassified"] = "pending"
    decision_reason: str = ""
    decided_by: str = ""
    baseline: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class OptimizationCandidateState(BaseModel):
    """跨项目候选的人工作业状态；聚合统计本身可随时重建。"""

    candidate_id: str
    cause_key: str
    status: Literal[
        "new",
        "triaged",
        "planned",
        "implementing",
        "monitoring",
        "verified",
        "dismissed",
        "deferred",
    ] = "new"
    owner: str = ""
    notes: str = ""
    verification_summary: str = ""
    target_scope: Literal[
        "undecided",
        "shared_kernel",
        "langgraph_harness",
        "codex_skill_harness",
        "multiple",
    ] = "undecided"
    change_ref: str = ""
    validation_pair_ids: list[str] = Field(default_factory=list)
    updated_by: str = ""
    updated_at: str = ""


class OptimizationRegressionCase(BaseModel):
    """候选修复的一个可执行或待补充回归案例。"""

    case_id: str
    candidate_id: str
    case_type: Literal[
        "original_finding",
        "cross_project_finding",
        "unit_test",
        "counterexample",
        "fixed_eval",
    ]
    label: str
    project_id: str = ""
    finding_id: str = ""
    test_selector: str = ""
    scenario_id: str = ""
    expected_outcome: str = "resolved"
    config: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "ready", "disabled"] = "draft"
    created_by: str = "system"
    created_at: str = ""
    updated_at: str = ""


class OptimizationRegressionCaseResult(BaseModel):
    case_id: str
    case_type: str
    label: str
    status: Literal["passed", "failed", "blocked", "skipped"]
    summary: str
    duration_seconds: float = 0
    details: dict[str, Any] = Field(default_factory=dict)


class OptimizationRegressionRun(BaseModel):
    """一次候选级定向回放，运行状态同时投影到统一 JobService。"""

    run_id: str
    candidate_id: str
    case_ids: list[str] = Field(default_factory=list)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"] = "queued"
    stage: str = "queued"
    progress_message: str = ""
    results: list[OptimizationRegressionCaseResult] = Field(default_factory=list)
    prompt_fingerprint: dict[str, Any] = Field(default_factory=dict)
    code_fingerprint: str = ""
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    advance_to_monitoring: bool = True
    created_by: str = "admin_user"
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""


class MonitoringWindow(BaseModel):
    """一次候选修复对应的最小观察窗口。"""

    window_id: str
    candidate_id: str
    source_regression_run_id: str
    checker_id: str = Field(min_length=1)
    checker_version: str = Field(min_length=1)
    code_fingerprint: str = Field(min_length=1)
    prompt_fingerprint: str = Field(min_length=1)
    started_at: str
    minimum_elapsed_days: int = Field(default=14, ge=0)
    minimum_samples: int = Field(default=5, ge=1)
    created_by: str = "system"
    created_at: str


class MonitoringObservation(BaseModel):
    """内置 checker 对一个项目产物 revision 的扫描事实。"""

    event_id: str
    window_id: str
    candidate_id: str
    project_id: str
    project_revision: str = Field(min_length=1)
    outcome: Literal["clear", "hit", "failed"]
    finding_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    observed_at: str


class TravelReviewEvidenceRead(BaseModel):
    """模型在本次运行中实际读取的证据。"""

    ref: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    source_type: str = "unknown"


class TravelReviewTrace(BaseModel):
    """Skill 或外部强模型的运行轨迹。"""

    mode: Literal["direct_repo", "bundle"] = "bundle"
    status: Literal["completed", "failed"] = "completed"
    model_name: str = ""
    model_group: str = ""
    model_provider: str = ""
    started_at: str = ""
    completed_at: str = ""
    evidence_reads: list[TravelReviewEvidenceRead] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class TravelReviewResult(BaseModel):
    """旅游攻略定向复核 Skill 的可移植结果包。"""

    schema_version: Literal["travel-review-result-v1"] = "travel-review-result-v1"
    bundle_sha256: str = Field(min_length=64, max_length=64)
    recommendation: ReviewRecommendation
    system_observations: list[TravelReviewSystemObservation] = Field(default_factory=list)
    proposed_operations: list[TravelReviewProposedOperation] = Field(default_factory=list)
    review_trace: TravelReviewTrace = Field(default_factory=TravelReviewTrace)
    validation: dict[str, Any] = Field(default_factory=dict)
