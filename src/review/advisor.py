"""定向模型建议与复核。

这是模型层的薄适配：模型只处理一个 finding 及其证据，不负责直接写业务文件。
模型结果先持久化为 recommendation，随后由人工或策略层决定是否生成/应用 patch。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.config import get_chat_model, get_light_chat_model
from src.llm_rotation import build_chat_model
from src.review import store
from src.review.models import ReviewClaimEvidence
from src.review.runtime import REVIEW_MODEL_GROUPS


class ModelReviewResult(BaseModel):
    passed: bool = False
    issues: list[str] = Field(default_factory=list)
    summary: str = ""
    proposed_text: str = ""
    claim_reviews: list[ReviewClaimEvidence] = Field(min_length=1)
    risk_level: str = "medium"
    confidence: str = "medium"
    recommended_action: str = "human_review"
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def align_passed_with_claim_actions(self) -> "ModelReviewResult":
        """需要修改或补证据的 claim 不能同时被标成整单通过。"""
        resolved_without_patch = all(
            item.action == "sync_status"
            or (item.action == "keep" and item.verdict == "supported")
            for item in self.claim_reviews
        )
        if not resolved_without_patch:
            self.passed = False
        return self


def persist_recommendation(
    project_id: str,
    finding: store.ReviewFinding,
    parsed: ModelReviewResult,
    *,
    model: Any,
    model_name: str = "",
    context_version: str = "review-context-v2",
    promote: bool = True,
) -> store.ReviewRecommendation:
    """把模型已经提交的结构化结果写入 recommendation，不修改业务正文。"""
    actual_model = model_name or str(getattr(model, "model_name", "") or getattr(model, "model", "") or "model")
    model_group = str(getattr(model, "key_group", ""))
    model_provider = type(model).__name__
    claim_reviews = [
        item if isinstance(item, ReviewClaimEvidence) else ReviewClaimEvidence.model_validate(item)
        for item in parsed.claim_reviews
    ]
    evidence_refs = list(dict.fromkeys(
        [*finding.evidence_refs, *(ref for item in claim_reviews for ref in item.evidence_refs)]
    ))
    recommendation = store.ReviewRecommendation(
        recommendation_id=f"recommendation-{uuid.uuid4().hex[:12]}",
        finding_id=finding.finding_id,
        decision="accept_patch" if parsed.passed else "human_review",
        confidence=parsed.confidence,
        risk_level=parsed.risk_level,
        summary=parsed.summary,
        proposed_text=parsed.proposed_text,
        claim_reviews=claim_reviews,
        evidence_refs=evidence_refs,
        uncertainties=parsed.uncertainties,
        recommended_action=parsed.recommended_action,
        model_name=actual_model,
        model_group=model_group,
        model_provider=model_provider,
        context_version=context_version,
    )
    store.append_recommendation(project_id, recommendation, promote=promote)
    return recommendation


def build_review_prompt(finding: dict[str, Any], current_content: Any, evidence: list[dict[str, Any]] | list[str]) -> str:
    """生成短、单目标的复核提示，避免把整条长路线重新塞给模型。"""
    return (
        "你是旅游攻略定向复核员。只判断当前这个问题单，不重新生成整条路线。\n"
        "请结合修改前内容和证据，判断是否支持当前表述；如需修补，给出最小改写。\n"
        "特别注意：引用核验失败不等于内容本身错误。逐条区分：内容被证据支持但引用挂错、内容只有部分支持、缺少证据、与证据明确矛盾、以及仅有历史状态残留。\n"
        "对路线和POI中有价值的地点/游览顺序，不要因为当前chunk未命中就建议删除；优先建议补挂真实来源、替换来源、收窄具体断言或加‘以现场为准’。只有证据明确相反时才建议删除。\n"
        "如果问题单聚合了多个途经点，请逐点判断，不要把‘整体不支持’作为默认结论。\n"
        "不要凭空补充票价、开放时间、店名或路线细节；不确定时使用条件式表达。\n"
        "claim_reviews 是主要结果，summary 只是它的短摘要。每个争议点必须恰好对应一条 claim_review。"
        "严格使用字段：claim、target_path、original_text、evidence_refs、evidence_excerpt、verdict、action、content_action、evidence_action、proposed_text、rationale、confidence。"
        "target_path 应填写争议字段在当前 JSON 中的 JSON Pointer，无法定位时留空并说明。"
        "verdict 只能是 supported/partial/unsupported/contradicted/ambiguous；"
        "action 只能是 keep/narrow/add_source/repair_reference/sync_status/rerun。"
        "content_action 只能是 keep/narrow/rerun；evidence_action 只能是 keep/add_source/repair_reference/sync_status。"
        "正文和证据可同时需要处理，例如 content_action=narrow、evidence_action=repair_reference；action 填主动作。"
        "proposed_text 必须是人工可直接审阅的修改后正文，不得只写‘建议删除/考虑收窄’等操作说明；"
        "content_action=keep 时 proposed_text 写‘保持原文’，rationale 不得同时要求删除、取消时长或收窄正文。"
        "没有实际读取过的来源不得写入 evidence_refs；字段不得留空。\n"
        "请严格返回结构化结果。\n\n"
        f"问题单：{json.dumps(finding, ensure_ascii=False)}\n"
        f"当前内容：{json.dumps(current_content, ensure_ascii=False)}\n"
        f"证据：{json.dumps(evidence, ensure_ascii=False)}"
    )


def generate_recommendation(
    project_id: str,
    finding_id: str,
    *,
    current_content: Any,
    evidence: list[dict[str, Any]] | list[str],
    light: bool = False,
    model_name: str = "",
    channel: str = "",
    promote: bool = True,
) -> tuple[store.ReviewRecommendation, ModelReviewResult]:
    """调用指定渠道生成建议包；不直接修改业务内容。

    ``channel`` 为空时保留旧的 full/light 行为；后台复核接口默认
    显式传入 GPT-Load 渠道，与主生成流程的运行状态隔离。
    """
    finding = store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    if channel:
        if channel not in REVIEW_MODEL_GROUPS:
            raise ValueError(f"不支持的复核模型渠道: {channel}")
        model = build_chat_model(group=channel, temperature=0)
    else:
        model = (get_light_chat_model if light else get_chat_model)(temperature=0)
    # 复用项目现有的默认 structured-output 适配，避免强制指定某个中转渠道未必支持的
    # json_schema/json_mode 实现；具体模型能力由当前 provider 适配层决定。
    structured = model.with_structured_output(ModelReviewResult)
    result = structured.invoke(
        [{"role": "user", "content": build_review_prompt(finding.model_dump(mode="json"), current_content, evidence)}]
    )
    parsed = result if isinstance(result, ModelReviewResult) else ModelReviewResult.model_validate(result)
    recommendation = persist_recommendation(
        project_id,
        finding,
        parsed,
        model=model,
        model_name=model_name,
        context_version="review-context-v2",
        promote=promote,
    )
    return recommendation, parsed
