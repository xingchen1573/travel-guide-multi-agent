"""定向 patch 的影响评估与联动策略。

策略是确定性的：不调用模型、不修改内容，只告诉管理端应用 patch 后需要做哪些
后续动作。这样长路线项目可以把“改一处”和“重跑整条路线”明确区分开。
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable


_EVIDENCE_ONLY_FIELDS = {
    "auto_collected",
    "citation_status",
    "evidence_refs",
    "evidence_snapshots",
    "missing_entities",
    "retry_history",
    "sources",
}


def _target_days(target: dict[str, Any] | None) -> list[int]:
    target = target or {}
    values: list[Any] = [target.get("day"), *(target.get("days") or [])]
    result: list[int] = []
    for value in values:
        try:
            day = int(value)
        except (TypeError, ValueError):
            continue
        if day > 0 and day not in result:
            result.append(day)
    return sorted(result)


def classify_patch_impact(
    file: str,
    target: dict[str, Any] | None = None,
    operations: Iterable[dict[str, Any]] | None = None,
) -> str:
    """返回 presentation/mechanical/lifestyle/pitfall/poi/route_day/route_structure。"""
    path = PurePosixPath(file.replace("\\", "/"))
    name = path.name.lower()
    target = target or {}
    joined = f"{file} {target.get('section', '')} {target.get('field', '')}".lower()
    if name == "final_guide.md" or "presentation" in joined or "site" in joined:
        return "presentation"
    if name == "manifest.json" and "outputs/poi_details" in str(path):
        return "mechanical"
    if "outputs/lifestyle.json" in str(path):
        return "lifestyle"
    if "outputs/pitfall.json" in str(path):
        return "pitfall"
    if "outputs/poi_details/" in str(path):
        return "poi"
    if name == "route_plan.json" or "route_plan" in joined:
        ops = list(operations or [])
        pointers = " ".join(str(item.get("path", "")) for item in ops).lower()
        structural_tokens = ("/days", "/segments", "/options", "/city", "/pois")
        if any(token in pointers for token in structural_tokens) and not _target_days(target):
            return "route_structure"
        return "route_day"
    if "lifestyle" in joined:
        return "lifestyle"
    if "pitfall" in joined:
        return "pitfall"
    if "poi" in joined:
        return "poi"
    return "mechanical"


def _is_evidence_only_operation(operation: dict[str, Any]) -> bool:
    pointer = str(operation.get("path") or "").strip("/")
    if not pointer:
        return False
    segments = [segment.replace("~1", "/").replace("~0", "~") for segment in pointer.split("/")]
    return any(segment in _EVIDENCE_ONLY_FIELDS for segment in segments)


def build_cascade_plan(
    project_id: str,
    *,
    file: str,
    target: dict[str, Any] | None = None,
    operations: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成可展示、可审计的 patch 后续动作计划。"""
    operations = list(operations or [])
    impact = classify_patch_impact(file, target, operations)
    days = _target_days(target)
    content_impacts = {"lifestyle", "pitfall", "poi", "route_day", "route_structure"}
    requires_integration_execution = (
        impact in content_impacts
        and bool(operations)
        and not all(_is_evidence_only_operation(operation) for operation in operations)
    )
    requires_targeted_validation = (
        impact in content_impacts
        and bool(operations)
        and not requires_integration_execution
    )
    common = {
        "render_only": {
            "label": "重新生成呈现层",
            "scope": "presentation",
            "calls_llm": False,
            "required": True,
        },
        "state_scan": {
            "label": "执行确定性状态扫描",
            "scope": "project_state",
            "calls_llm": False,
            "required": True,
        },
        "targeted_review": {
            "label": "定向复核受影响内容",
            "scope": "targeted",
            "calls_llm": True,
            "required": True,
        },
        "integration_review": {
            "label": "复核整合结果与引用区块",
            "scope": "integration",
            "calls_llm": requires_integration_execution,
            "required": True,
        },
        "full_rerun": {
            "label": "重新运行完整路线图",
            "scope": "project",
            "calls_llm": True,
            "required": True,
        },
    }
    actions_by_impact = {
        "presentation": ["render_only", "state_scan"],
        "mechanical": ["state_scan"],
        "lifestyle": ["targeted_review", "integration_review", "render_only", "state_scan"],
        "pitfall": ["targeted_review", "integration_review", "render_only", "state_scan"],
        "poi": ["targeted_review", "integration_review", "render_only", "state_scan"],
        "route_day": ["targeted_review", "integration_review", "render_only", "state_scan"],
        "route_structure": ["full_rerun", "integration_review", "render_only", "state_scan"],
    }
    action_keys = actions_by_impact[impact]
    risk = "high" if impact == "route_structure" else "medium" if impact in {"route_day", "lifestyle", "pitfall", "poi"} else "low"
    return {
        "project_id": project_id,
        "file": file,
        "impact_class": impact,
        "risk_level": risk,
        "target_days": days,
        "needs_human_approval": impact in {"route_structure", "route_day", "lifestyle", "pitfall", "poi"},
        "requires_integration_execution": requires_integration_execution,
        "requires_targeted_validation": requires_targeted_validation,
        "execution_status": "planned",
        "actions": [common[key] | {"action": key} for key in action_keys],
        "explanation": {
            "presentation": "只影响已生成页面或引用展示，不需要模型重跑。",
            "mechanical": "属于状态/格式类变更，执行确定性扫描即可。",
            "lifestyle": "衣食住行内容可能影响整合结果，需对受影响类别做定向复核。",
            "pitfall": "避坑建议变化后需复核对应日程和整合引用。",
            "poi": "POI 详情变化后需复核该 POI 及其呈现层，不扩散到全路线。",
            "route_day": "单日路线变化可能影响当天交通与整合结果，默认定向复核。",
            "route_structure": "路线结构变化会改变多个下游输入，完整重跑由人工确认。",
        }[impact],
    }
