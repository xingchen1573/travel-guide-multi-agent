"""项目复核系统与旅游攻略定向复核 Skill 的稳定交换边界。

本模块只导出冻结证据包、校验外部结果并追加 recommendation。它不调用
模型、不创建或应用 patch，也不修改攻略业务文件。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.review import agent as review_agent
from src.review import store as review_store
from src.review.models import ReviewRecommendation, TravelReviewResult
from src.workspace import store as workspace_store


BUNDLE_SCHEMA_VERSION = "travel-review-bundle-v1"
RESULT_SCHEMA_VERSION = "travel-review-result-v1"
SKILL_CONTEXT_VERSION = "travel-review-skill-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _payload_sha256(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("bundle_sha256", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _safe_target_path(project_id: str, relative_file: str) -> Path | None:
    if not relative_file:
        return None
    root = workspace_store.workspace_dir(project_id).resolve()
    path = (root / relative_file).resolve()
    if root not in path.parents or path.suffix != ".json":
        raise ValueError("复核目标必须是 project workspace 内的 JSON 文件")
    return path


def _scope_original_text(item: dict[str, Any]) -> str:
    return json.dumps(
        {
            "route_name": item.get("route_name"),
            "point_name": item.get("point_name"),
            "highlight": item.get("current_claim"),
            "suggested_duration": item.get("suggested_duration"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _fallback_scope_item(
    finding: review_store.ReviewFinding,
    context: dict[str, Any],
    pointer: str,
) -> dict[str, Any]:
    current = context.get("current_content")
    return {
        "route_name": "",
        "point_name": finding.problem_type,
        "target_path": pointer,
        "current_claim": json.dumps(current, ensure_ascii=False, sort_keys=True),
        "suggested_duration": "",
        "direct_evidence": [],
        "alternative_candidates": [],
    }


def export_review_bundle(project_id: str, finding_id: str) -> dict[str, Any]:
    """导出不含模型结论的冻结复核包。"""
    finding = review_store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    review_agent._assert_model_reviewable(finding)

    topic = review_agent._requirements_topic(project_id)
    context = review_store.build_review_context(project_id, finding_id)
    file, pointer = review_agent._target_file_and_pointer(finding.target or {})
    target_path = _safe_target_path(project_id, file)
    packet = review_agent._build_disputed_evidence_packet(topic, finding)
    packet_items = list(packet.get("items") or [])
    if not packet_items:
        if not pointer:
            raise ValueError("问题单缺少可复核的精确 JSON Pointer，请人工补充定位信息")
        packet_items = [_fallback_scope_item(finding, context, pointer)]

    scope: list[dict[str, Any]] = []
    allowed_refs: set[str] = set()
    for index, raw_item in enumerate(packet_items):
        item = dict(raw_item)
        item["item_key"] = f"claim:{item.get('target_path') or index}"
        item["original_text"] = _scope_original_text(item)
        scope.append(item)
        for evidence in item.get("direct_evidence") or []:
            if evidence.get("found") and evidence.get("ref"):
                allowed_refs.add(str(evidence["ref"]))
        for evidence in item.get("alternative_candidates") or []:
            if evidence.get("ref"):
                allowed_refs.add(str(evidence["ref"]))

    recommendations = review_store.read_recommendations_for_finding(project_id, finding_id)
    bundle: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "project": {
            "project_id": project_id,
            "topic": topic,
        },
        "finding": finding.model_dump(mode="json"),
        "baseline": {
            "file": file,
            "json_pointer": pointer,
            "file_sha256": review_store.file_sha256(target_path) if target_path and target_path.exists() else "",
            "finding_revision": finding.revision,
            "recommendation_id": finding.recommendation_id,
        },
        "scope": scope,
        "evidence": {
            "warning": packet.get("warning", ""),
            "groups": context.get("evidence_groups") or {},
            "allowed_refs": sorted(allowed_refs),
        },
        "history": {
            "recommendation_ids": [item.recommendation_id for item in recommendations],
            "current_recommendation_id": finding.recommendation_id,
        },
        "policy": {
            "verdicts": ["supported", "partial", "unsupported", "contradicted", "ambiguous"],
            "actions": ["keep", "narrow", "add_source", "repair_reference", "sync_status", "rerun"],
            "review_only": True,
            "may_apply_patch": False,
        },
        "generated_at": _now(),
    }
    bundle["bundle_sha256"] = _payload_sha256(bundle)
    return bundle


def _evidence_texts(bundle: dict[str, Any]) -> dict[str, list[str]]:
    texts: dict[str, list[str]] = {}
    for item in bundle.get("scope") or []:
        for evidence in item.get("direct_evidence") or []:
            if evidence.get("found") and evidence.get("ref") and evidence.get("text"):
                texts.setdefault(str(evidence["ref"]), []).append(str(evidence["text"]))
        for evidence in item.get("alternative_candidates") or []:
            if evidence.get("ref") and evidence.get("text"):
                texts.setdefault(str(evidence["ref"]), []).append(str(evidence["text"]))
    for evidence in (bundle.get("evidence") or {}).get("groups", {}).get("source_excerpts", []) or []:
        source = evidence.get("source") or {}
        ref = source.get("chunk_id")
        if ref and evidence.get("text"):
            texts.setdefault(str(ref), []).append(str(evidence["text"]))
    return texts


def _claim_key(item: dict[str, Any], index: int) -> str:
    return str(item.get("target_path") or f"__index__:{index}")


def _contains_excerpt(excerpt: str, candidates: list[str]) -> bool:
    compact_excerpt = " ".join(excerpt.split())
    if compact_excerpt in {"没有检索到相关素材", "未检索到相关素材", "没有找到相关证据"}:
        return True
    return any(
        compact_excerpt in " ".join(candidate.split())
        or " ".join(candidate.split()) in compact_excerpt
        for candidate in candidates
        if candidate.strip()
    )


def _mentions_false_official_claim(text: str) -> bool:
    cleaned = text.replace("非官方", "").replace("不是官方", "").replace("不得称为官方", "")
    return bool(re.search(r"(?:来自|属于|这是|为)官方|官方来源", cleaned))


def validate_review_result(
    bundle: dict[str, Any],
    raw_result: dict[str, Any] | TravelReviewResult,
) -> TravelReviewResult:
    """校验模型输出是否足以安全导入现有 recommendation。"""
    errors: list[str] = []
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append("不支持的 review bundle 版本")
    expected_bundle_hash = _payload_sha256(bundle)
    if bundle.get("bundle_sha256") != expected_bundle_hash:
        errors.append("review bundle 内容与 bundle_sha256 不一致")

    try:
        result = raw_result if isinstance(raw_result, TravelReviewResult) else TravelReviewResult.model_validate(raw_result)
    except ValueError as exc:
        raise ValueError(f"review result schema 校验失败: {exc}") from exc

    if result.schema_version != RESULT_SCHEMA_VERSION:
        errors.append("不支持的 review result 版本")
    if result.bundle_sha256 != expected_bundle_hash:
        errors.append("review result 没有绑定当前 review bundle")
    finding = bundle.get("finding") or {}
    if result.recommendation.finding_id != finding.get("finding_id"):
        errors.append("recommendation.finding_id 与 bundle 不一致")
    if result.review_trace.status != "completed" or result.review_trace.errors:
        errors.append("模型运行未成功完成，不能形成业务 recommendation")

    expected_scope = list(bundle.get("scope") or [])
    claims = result.recommendation.claim_reviews
    if len(claims) != len(expected_scope):
        errors.append(f"claim 数量为 {len(claims)}，预期 {len(expected_scope)}")

    evidence_texts = _evidence_texts(bundle)
    allowed_refs = set((bundle.get("evidence") or {}).get("allowed_refs") or [])
    trace_reads = {item.ref: item for item in result.review_trace.evidence_reads}
    allowed_refs.update(trace_reads)

    action_verdicts = {
        "keep": {"supported", "partial"},
        "sync_status": {"supported", "partial"},
        "narrow": {"partial", "unsupported", "contradicted", "ambiguous"},
        "add_source": {"supported", "partial", "ambiguous"},
        "repair_reference": {"supported", "partial", "contradicted"},
        "rerun": {"supported", "partial", "unsupported", "contradicted", "ambiguous"},
    }
    for index, (scope_item, claim) in enumerate(zip(expected_scope, claims)):
        expected_key = _claim_key(scope_item, index)
        actual_key = claim.target_path or f"__index__:{index}"
        if actual_key != expected_key:
            errors.append(f"第 {index + 1} 条 target_path 不一致: {claim.target_path!r}")
        if claim.original_text != scope_item.get("original_text"):
            errors.append(f"第 {index + 1} 条 original_text 不是 bundle 中的当前原文")
        if claim.verdict not in action_verdicts.get(claim.action, set()):
            errors.append(f"第 {index + 1} 条 verdict={claim.verdict} 与 action={claim.action} 不一致")
        if claim.content_action == "keep" and claim.action in {"narrow", "rerun"}:
            errors.append(f"第 {index + 1} 条 content_action=keep 与主 action={claim.action} 冲突")
        keep_change_pattern = re.compile(
            r"(?:删除|取消|移除|收窄|改写|改为).{0,12}(?:正文|描述|表述|时长|分钟|数字)"
            r"|(?:正文|描述|表述|时长|分钟|数字).{0,12}(?:删除|取消|移除|收窄|改写|改为)"
        )
        proposed_text_changes_body = bool(re.search(
            r"(?:删除|取消|移除|收窄).{0,8}(?:正文|描述|表述|固定时长|分钟|数字)",
            claim.proposed_text,
        ))
        if claim.content_action == "keep" and (
            keep_change_pattern.search(claim.rationale) or proposed_text_changes_body
        ):
            errors.append(
                f"第 {index + 1} 条 content_action=keep，但理由或建议要求修改正文"
            )
        if claim.content_action == "narrow" and claim.proposed_text.strip() in {"", "保持原文"}:
            errors.append(f"第 {index + 1} 条 content_action=narrow 缺少正文最小改写")
        if claim.content_action == "rerun" and claim.action != "rerun":
            errors.append(f"第 {index + 1} 条 content_action=rerun 时主 action 必须是 rerun")
        evidence_primary_actions = {"add_source", "repair_reference", "sync_status"}
        if claim.evidence_action in evidence_primary_actions and claim.action not in {
            claim.evidence_action,
            "narrow",
            "rerun",
        }:
            errors.append(
                f"第 {index + 1} 条 evidence_action={claim.evidence_action} 与主 action={claim.action} 冲突"
            )
        if claim.action in {"narrow", "add_source", "repair_reference"} and claim.proposed_text.strip() in {"", "保持原文"}:
            errors.append(f"第 {index + 1} 条 {claim.action} 缺少可执行的最小修改建议")

        unknown_refs = [ref for ref in claim.evidence_refs if ref not in allowed_refs]
        if unknown_refs:
            errors.append(f"第 {index + 1} 条引用了未读取证据: {unknown_refs}")
        candidate_texts: list[str] = []
        for ref in claim.evidence_refs:
            candidate_texts.extend(evidence_texts.get(ref, []))
            trace_read = trace_reads.get(ref)
            if trace_read is not None:
                candidate_texts.append(trace_read.excerpt)
                if trace_read.source_type in {"third_party", "ugc"} and _mentions_false_official_claim(
                    f"{claim.evidence_excerpt} {claim.rationale}"
                ):
                    errors.append(f"第 {index + 1} 条把第三方或 UGC 证据称为官方来源")
        if not _contains_excerpt(claim.evidence_excerpt, candidate_texts):
            errors.append(f"第 {index + 1} 条 evidence_excerpt 无法在已读取证据中定位")

        rationale_says_duration_missing = any(
            marker in claim.rationale
            for marker in ("时长无依据", "时长没有证据", "停留分钟无对应", "没有提供具体时长", "无原文对应")
        )
        original_durations = set(re.findall(r"\d+\s*(?:[-–~至]\s*\d+)?\s*分钟", claim.original_text))
        if rationale_says_duration_missing and any(value in claim.proposed_text for value in original_durations):
            errors.append(f"第 {index + 1} 条称时长无依据，但 proposed_text 仍保留相同固定时长")

    scope_keys = {_claim_key(item, index) for index, item in enumerate(expected_scope)}
    for operation in result.proposed_operations:
        if operation.item_key not in {item.get("item_key") for item in expected_scope}:
            errors.append(f"proposed operation 不属于当前 scope: {operation.item_key}")
        if operation.path not in scope_keys and not any(
            key.startswith(operation.path.rstrip("/") + "/") or operation.path.startswith(key.rstrip("/") + "/")
            for key in scope_keys
            if key and not key.startswith("__index__")
        ):
            errors.append(f"proposed operation 路径越界: {operation.path}")
        if not operation.requires_human_confirmation:
            errors.append("Skill 结果不得绕过人工确认直接应用内容修改")

    system_allowed_refs = set(allowed_refs)
    system_allowed_refs.update(str(item) for item in (bundle.get("history") or {}).get("recommendation_ids") or [])
    system_allowed_refs.add(str(finding.get("finding_id") or ""))
    scope_targets = {
        str(item.get("target_path") or "")
        for item in expected_scope
        if item.get("target_path")
    }
    target_file = str((finding.get("target") or {}).get("file") or "")
    system_target_prefixes = ("code:", "system:", "prompt:", "schema:", "api:", "frontend:", "test:")
    unsafe_fix_markers = ("绕过人工", "直接发布", "自动部署", "自动修改生产", "跳过审批")
    for index, observation in enumerate(result.system_observations):
        label = f"第 {index + 1} 条 system observation"
        try:
            observation.cause_key = review_store.normalize_cause_key(
                observation.root_cause,
                observation.problem_type,
                observation.cause_key,
            )
        except ValueError as exc:
            errors.append(f"{label} {exc}")
        unknown_refs = [ref for ref in observation.evidence_refs if ref not in system_allowed_refs]
        if unknown_refs:
            errors.append(f"{label} 引用了未读取证据: {unknown_refs}")
        for target in observation.representative_targets:
            pointer = target.split("#", 1)[-1] if "#" in target else target
            file_matches = bool(target_file and target.startswith(target_file))
            if not (
                target in scope_targets
                or pointer in scope_targets
                or file_matches
                or target.startswith(system_target_prefixes)
            ):
                errors.append(f"{label} representative target 越界: {target}")
        if any(marker in observation.suggested_fix for marker in unsafe_fix_markers):
            errors.append(f"{label} suggested_fix 试图绕过工程审批")
        if observation.severity == "high" and not observation.suggested_test.strip():
            errors.append(f"{label} 为 high severity，但缺少 suggested_test")

    if errors:
        raise ValueError("review result 未通过确定性门禁:\n- " + "\n- ".join(errors))
    result.validation = {
        **result.validation,
        "status": "passed",
        "validated_at": _now(),
        "scope_count": len(expected_scope),
    }
    return result


def _assert_baseline_unchanged(project_id: str, bundle: dict[str, Any]) -> None:
    baseline = bundle.get("baseline") or {}
    relative_file = str(baseline.get("file") or "")
    expected_hash = str(baseline.get("file_sha256") or "")
    path = _safe_target_path(project_id, relative_file)
    if expected_hash:
        if path is None or not path.exists() or review_store.file_sha256(path) != expected_hash:
            raise RuntimeError("review bundle baseline 已变化，必须重新导出证据包")
    finding_id = str((bundle.get("finding") or {}).get("finding_id") or "")
    finding = review_store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    if finding.revision != int(baseline.get("finding_revision") or 0):
        raise RuntimeError("finding revision 已变化，必须重新导出证据包")


def import_review_result(
    project_id: str,
    bundle: dict[str, Any],
    raw_result: dict[str, Any] | TravelReviewResult,
    *,
    promote: bool = True,
    review_run_id: str = "",
    source_type: str = "external_skill",
) -> ReviewRecommendation:
    """导入已验证结果；追加 recommendation、运行附件和待裁决系统信号。"""
    if project_id != str((bundle.get("project") or {}).get("project_id") or ""):
        raise ValueError("project_id 与 review bundle 不一致")
    validated = validate_review_result(bundle, raw_result)
    _assert_baseline_unchanged(project_id, bundle)

    draft = validated.recommendation
    recommendation = draft.model_copy(update={
        "recommendation_id": f"recommendation-{uuid.uuid4().hex[:12]}",
        "finding_id": str((bundle.get("finding") or {}).get("finding_id") or ""),
        "model_name": draft.model_name or validated.review_trace.model_name,
        "model_group": draft.model_group or validated.review_trace.model_group,
        "model_provider": draft.model_provider or validated.review_trace.model_provider,
        "context_version": SKILL_CONTEXT_VERSION,
        "proposed_operations": validated.proposed_operations,
        "created_at": "",
    })
    review_store.append_recommendation(project_id, recommendation, promote=promote)

    run_dir = workspace_store.workspace_dir(project_id) / "review_runs" / recommendation.recommendation_id
    artifact = validated.model_dump(mode="json")
    artifact["recommendation"] = recommendation.model_dump(mode="json")
    review_store._write_json_atomic(run_dir / "travel_review_result.json", artifact)
    review_store.append_improvement_signals(
        project_id,
        finding_id=recommendation.finding_id,
        recommendation_id=recommendation.recommendation_id,
        observations=validated.system_observations,
        review_run_id=review_run_id,
        source_type=source_type,
        baseline={
            "bundle_sha256": validated.bundle_sha256,
            "finding_revision": int((bundle.get("baseline") or {}).get("finding_revision") or 0),
        },
    )
    return recommendation
