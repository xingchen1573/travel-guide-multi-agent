"""Applied review Patch validation with bounded, project-level batching."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.materials import citation_check
from src.review import store as review_store
from src.workspace import store as workspace_store


LogFn = Callable[[str], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pointer_tokens(pointer: str) -> list[str]:
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer.strip("/").split("/")
        if token
    ]


def _pointer_get(value: Any, tokens: list[str]) -> Any:
    current = value
    for token in tokens:
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise KeyError(token)
    return current


def _nearest_day(project_id: str, file: str, pointer: str) -> int | None:
    document = workspace_store.read_workspace(project_id, file)
    if not isinstance(document, (dict, list)):
        return None
    tokens = _pointer_tokens(pointer)
    for length in range(len(tokens), -1, -1):
        try:
            value = _pointer_get(document, tokens[:length])
        except (KeyError, IndexError, ValueError):
            continue
        if isinstance(value, dict):
            try:
                day = int(value.get("day"))
            except (TypeError, ValueError):
                continue
            if day > 0:
                return day
    return None


def _patch_pointer(patch: review_store.PatchRecord, finding: review_store.ReviewFinding | None) -> str:
    target_pointer = str((finding.target or {}).get("json_path") or "") if finding else ""
    if target_pointer:
        return target_pointer
    return str(patch.operations[0].path or "") if patch.operations else ""


def build_pending_validation_scope(project_id: str) -> dict[str, Any]:
    patches = review_store.read_pending_validation_patches(project_id)
    lifestyle_items: set[tuple[str, int]] = set()
    pitfall_items: set[tuple[str, int]] = set()
    poi_files: set[str] = set()
    route_days: set[int] = set()
    route_structure_patch_ids: list[str] = []
    unresolved_patch_ids: list[str] = []

    for patch in patches:
        finding = review_store.get_finding(project_id, patch.finding_id)
        pointer = _patch_pointer(patch, finding)
        tokens = _pointer_tokens(pointer)
        observed = finding.observed_value if finding and isinstance(finding.observed_value, dict) else {}
        target = finding.target if finding else {}
        try:
            day = int(target.get("day") or observed.get("day") or 0)
        except (TypeError, ValueError):
            day = 0
        if day <= 0:
            day = _nearest_day(project_id, patch.file, pointer) or 0

        if patch.impact_class == "route_structure":
            route_structure_patch_ids.append(patch.patch_id)
        elif patch.impact_class == "route_day":
            if day > 0:
                route_days.add(day)
            else:
                unresolved_patch_ids.append(patch.patch_id)
        elif patch.impact_class in {"lifestyle", "pitfall"}:
            if tokens and day > 0:
                field = tokens[0]
                target_items = lifestyle_items if patch.impact_class == "lifestyle" else pitfall_items
                target_items.add((field, day))
            else:
                unresolved_patch_ids.append(patch.patch_id)
        elif patch.impact_class == "poi":
            if Path(patch.file).name == "manifest.json":
                unresolved_patch_ids.append(patch.patch_id)
            else:
                poi_files.add(patch.file)

    return {
        "project_id": project_id,
        "patch_ids": [patch.patch_id for patch in patches],
        "patch_count": len(patches),
        "lifestyle_items": [list(item) for item in sorted(lifestyle_items)],
        "pitfall_items": [list(item) for item in sorted(pitfall_items)],
        "poi_files": sorted(poi_files),
        "route_days": sorted(route_days),
        "requires_integration": any(
            bool(patch.cascade_plan.get("requires_integration_execution")) for patch in patches
        ),
        "route_structure_patch_ids": route_structure_patch_ids,
        "unresolved_patch_ids": unresolved_patch_ids,
        "can_start": bool(patches) and not route_structure_patch_ids and not unresolved_patch_ids,
    }


def _topic(project_id: str) -> str:
    requirements = workspace_store.read_requirements(project_id) or {}
    body = requirements.get("requirements", requirements) if isinstance(requirements, dict) else {}
    return str(requirements.get("topic") or body.get("destination") or body.get("topic") or "")


def _validation_failure(
    project_id: str,
    patches: list[review_store.PatchRecord],
    issues: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    issue_count = sum(len(items) for items in issues.values())
    useful_fields = {
        "label", "reason", "field", "day", "file", "route_name", "point_name",
        "target_key", "target_path", "result", "method", "code", "message", "target", "status",
    }
    compact_issues = {
        category: [
            {key: value for key, value in item.items() if key in useful_fields}
            for item in items[:20]
        ]
        for category, items in issues.items()
    }
    details = {
        "status": "failed",
        "checked_at": _now(),
        "issue_count": issue_count,
        "issues": compact_issues,
    }
    for patch in patches:
        review_store.record_patch_validation_status(
            project_id,
            patch.patch_id,
            execution_status="validation_failed",
            details=details,
        )
        review_store.update_finding(
            project_id,
            patch.finding_id,
            disposition="deferred",
            status="open",
            verification_status="failed_targeted_validation",
            verification_method="batched_patch_validation",
            verification_actor_type="system",
            verification_actor_id="review_validation",
            notes=f"本批定向验收发现 {issue_count} 个问题，已返回复核队列；不会自动重试。",
        )
    return details


def run_pending_patch_validation(
    project_id: str,
    *,
    allow_model_calls: bool = True,
    integration_runner: Callable[[str, str, str], Any] | None = None,
    site_builder: Callable[[str], Any] | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    emit = log or (lambda _message: None)
    patches = review_store.read_pending_validation_patches(project_id)
    scope = build_pending_validation_scope(project_id)
    if not patches:
        raise ValueError("当前项目没有等待验收的已应用 Patch")
    if scope["route_structure_patch_ids"]:
        raise ValueError("路线结构 Patch 不能进入普通批次验收，请返回既有路线变更流程处理")
    if scope["unresolved_patch_ids"]:
        raise ValueError(
            "部分 Patch 无法解析出定向验收范围，请先补充精确定位："
            + ", ".join(scope["unresolved_patch_ids"])
        )

    topic = _topic(project_id)
    issues: dict[str, list[dict[str, Any]]] = {}
    lifestyle_items = {tuple(item) for item in scope["lifestyle_items"]}
    pitfall_items = {tuple(item) for item in scope["pitfall_items"]}

    if lifestyle_items:
        emit(f"定向核验 Lifestyle：{len(lifestyle_items)} 个字段/Day")
        result = citation_check.verify_lifestyle_citations(
            project_id,
            topic,
            fields=sorted({field for field, _day in lifestyle_items}),
            item_keys=lifestyle_items,
            allow_model_calls=allow_model_calls,
        )
        if result:
            issues["lifestyle"] = result
    if pitfall_items:
        emit(f"定向核验 Pitfall：{len(pitfall_items)} 个字段/Day")
        result = citation_check.verify_pitfall_citations(
            project_id,
            topic,
            fields=sorted({field for field, _day in pitfall_items}),
            item_keys=pitfall_items,
            allow_model_calls=allow_model_calls,
        )
        if result:
            issues["pitfall"] = result
    if scope["route_days"]:
        emit(f"定向核验 Route：Day {scope['route_days']}")
        result = citation_check.verify_route_citations(
            project_id,
            topic,
            days=scope["route_days"],
            allow_model_calls=allow_model_calls,
        )
        if result:
            issues["route"] = result
    for file in scope["poi_files"]:
        emit(f"定向核验 POI：{Path(file).name}")
        detail = workspace_store.read_workspace(project_id, file)
        if not isinstance(detail, dict):
            issues.setdefault("poi", []).append({"file": file, "reason": "POI 详情文件不存在或不是 JSON 对象"})
            continue
        result = citation_check.verify_poi_detail_citations(
            detail,
            topic,
            project_id=project_id,
            log_scope=f"review_validation/{Path(file).stem}",
            allow_model_calls=allow_model_calls,
        )
        if result:
            issues.setdefault("poi", []).extend(result)

    if issues:
        emit("定向验收未通过，Finding 已返回复核队列")
        return _validation_failure(project_id, patches, issues)

    if scope["requires_integration"]:
        emit("定向核验通过，开始一次 Integration 节点局部重组")
        if integration_runner is None:
            from src.orchestrator import run_edit

            integration_runner = run_edit
        prior_conflict = workspace_store.get_pending_conflict(project_id)
        summary = json.dumps(
            {
                "lifestyle_items": scope["lifestyle_items"],
                "pitfall_items": scope["pitfall_items"],
                "poi_files": scope["poi_files"],
                "route_days": scope["route_days"],
            },
            ensure_ascii=False,
        )
        integration_runner(
            project_id,
            "integration_agent_node",
            f"人工已批准并应用本批局部 Patch。只重新整合这些受影响范围，保持其他内容不变：{summary}",
        )
        current_conflict = workspace_store.get_pending_conflict(project_id)
        new_conflict = (
            current_conflict is not None
            and (prior_conflict is None or current_conflict.id != prior_conflict.id)
        )
        final_meta = workspace_store.read_workspace(project_id, "final_guide.meta.json")
        contract = (
            final_meta.get("contract_validation", {})
            if isinstance(final_meta, dict)
            else {}
        )
        contract_status = str(contract.get("status") or "missing")
        if new_conflict or contract_status != "passed":
            integration_issues = [
                dict(issue)
                for issue in (contract.get("issues") or [])
                if isinstance(issue, dict)
            ]
            if new_conflict:
                integration_issues.append({
                    "code": "integration_conflict",
                    "message": current_conflict.reason,
                    "reason": current_conflict.reason,
                    "target": current_conflict.target,
                })
            if not integration_issues:
                integration_issues.append({
                    "code": "final_guide_contract_not_passed",
                    "message": f"最终攻略交付契约状态为 {contract_status}",
                    "reason": "Integration 完成后只有交付契约 passed 才能关闭本批 Finding。",
                    "status": contract_status,
                })
            emit("Integration 或最终交付契约未通过，Finding 已返回复核队列")
            return _validation_failure(
                project_id,
                patches,
                {"final_guide_contract": integration_issues},
            )

    workspace_store.mark_lifestyle_items_citation_passed(
        project_id, [tuple(item) for item in scope["lifestyle_items"]], actor="review_validation"
    )
    workspace_store.mark_pitfall_items_citation_passed(
        project_id, [tuple(item) for item in scope["pitfall_items"]], actor="review_validation"
    )
    workspace_store.mark_route_days_citation_passed(
        project_id, scope["route_days"], actor="review_validation"
    )
    for file in scope["poi_files"]:
        detail = workspace_store.read_workspace(project_id, file)
        if not isinstance(detail, dict):
            continue
        try:
            day = int(detail.get("day") or 0)
        except (TypeError, ValueError):
            day = 0
        poi_name = str(detail.get("poi_name") or "")
        if day > 0 and poi_name:
            workspace_store.mark_poi_citation_passed(
                project_id, day, poi_name, actor="review_validation"
            )

    if site_builder is None:
        from scripts.delivery.run_generate_site import generate_site

        site_builder = generate_site
    emit("重建一次最终网站")
    output_path = site_builder(project_id)

    details = {
        "status": "passed",
        "checked_at": _now(),
        "scope": scope,
        "site_output_path": str(output_path),
    }
    for patch in patches:
        review_store.record_patch_validation_status(
            project_id,
            patch.patch_id,
            execution_status="completed",
            details=details,
        )
        review_store.record_verification(
            project_id,
            patch.finding_id,
            verification_status="passed_batched_validation",
            verification_method="batched_patch_validation",
            disposition="resolved",
            actor_type="system",
            actor_id="review_validation",
            notes="人工批准的 Patch 已完成定向 Checker、必要的单次 Integration 和网站重建。",
        )
    review_store.sync_state_findings(project_id)
    emit("本批验收完成")
    return details
