"""Deterministic delivery-contract validation for the final travel guide."""
from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import ValidationError

from src.workspace.final_guide_blocks import (
    collect_upstream_claims,
    prepare_final_guide_blocks,
    render_final_guide_body,
    rendered_body_sha256,
)
from src.workspace.final_guide_sections import contract_body_markdown
from src.workspace.schema import FinalGuideBlocks, FinalGuideMeta, LIFESTYLE_FIELDS


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_DAY_PATTERNS = (
    re.compile(r"\bDay\s*0*(\d+)\b", re.IGNORECASE),
    re.compile(r"第\s*0*(\d+)\s*[天日]"),
)


def _issue(code: str, message: str, *evidence_paths: str, **details: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "evidence_paths": list(evidence_paths),
        **details,
    }


def _heading_texts(content_md: str) -> list[str]:
    return [match.group(1).strip() for match in _HEADING_RE.finditer(content_md)]


def _covered_days(content_md: str) -> set[int]:
    covered: set[int] = set()
    for heading in _heading_texts(content_md):
        for pattern in _DAY_PATTERNS:
            match = pattern.search(heading)
            if match:
                covered.add(int(match.group(1)))
                break
    return covered


def _canonical_merged_field(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower().replace(" ", "")
    if not lowered:
        return ""
    if lowered in {"route", "route_plan", "route_plan.days"} or lowered.startswith("route_plan"):
        return "route_plan"
    for field in LIFESTYLE_FIELDS:
        aliases = {
            field,
            f"lifestyle.{field}",
            f"lifestyle_agent.{field}",
        }
        if lowered in aliases or lowered.startswith(f"lifestyle_agent.{field}"):
            return f"lifestyle.{field}"
    pitfall_markers = ("pitfall", "bilei", "baolei", "avoidance", "避雷")
    if any(marker in lowered for marker in pitfall_markers):
        return "pitfall"
    return raw


def _expected_merged_fields(
    route_plan: Optional[dict], lifestyle: Optional[dict], pitfall: Optional[dict]
) -> set[str]:
    expected: set[str] = set()
    if (route_plan or {}).get("days"):
        expected.add("route_plan")
    for field in LIFESTYLE_FIELDS:
        section = (lifestyle or {}).get(field)
        if isinstance(section, dict) and section.get("items"):
            expected.add(f"lifestyle.{field}")
    for field in LIFESTYLE_FIELDS:
        section = (pitfall or {}).get(field)
        items = section.get("items", []) if isinstance(section, dict) else []
        if any(isinstance(item, dict) and str(item.get("warning", "")).strip() for item in items):
            expected.add("pitfall")
            break
    return expected


def validate_final_guide_contract(
    requirements: Optional[dict],
    route_plan: Optional[dict],
    feasibility: Optional[dict],
    lifestyle: Optional[dict],
    pitfall: Optional[dict],
    final_guide: Optional[str],
    final_meta: Optional[dict],
    final_blocks: Optional[dict] = None,
    poi_details: Optional[list[dict]] = None,
    evidence_checks: Optional[dict] = None,
    review_findings: Optional[list[dict]] = None,
) -> list[dict[str, Any]]:
    """Return deterministic delivery issues without reading or writing files.

    ``requirements`` and ``feasibility`` are accepted as part of the stable
    delivery-contract boundary. The initial rules deliberately rely only on
    structured facts that can be checked without interpreting prose quality.
    """
    del requirements, feasibility
    issues: list[dict[str, Any]] = []

    if not isinstance(final_guide, str) or not final_guide.strip():
        issues.append(_issue("final_guide_missing", "最终攻略不存在或正文为空", "final_guide.md"))
        return issues
    delivery_body = contract_body_markdown(final_guide)
    if len(delivery_body) <= 200:
        issues.append(
            _issue(
                "final_guide_too_short",
                f"最终攻略正文仅{len(delivery_body)}字符，未达到最低交付长度",
                "final_guide.md",
                actual_length=len(delivery_body),
                minimum_length=201,
            )
        )

    validated_meta: FinalGuideMeta | None = None
    if not isinstance(final_meta, dict):
        issues.append(_issue("invalid_final_meta", "最终攻略Meta缺失或不是JSON对象", "final_guide.meta.json"))
    else:
        try:
            validated_meta = FinalGuideMeta.model_validate(final_meta)
        except ValidationError as exc:
            issues.append(
                _issue(
                    "invalid_final_meta",
                    f"最终攻略Meta未通过Schema校验：{exc.errors(include_url=False)}",
                    "final_guide.meta.json",
                )
            )

    route_days = {
        raw_day.get("day")
        for raw_day in ((route_plan or {}).get("days") or [])
        if isinstance(raw_day, dict) and isinstance(raw_day.get("day"), int)
    }
    missing_days = sorted(route_days - _covered_days(delivery_body))
    for day in missing_days:
        issues.append(
            _issue(
                "missing_day",
                f"最终攻略缺少Day{day}的天级标题",
                f"route_plan.json:/days/{day - 1}",
                "final_guide.md",
                day=day,
            )
        )

    checklist_declared = bool(validated_meta and validated_meta.checklist_included)
    checklist_heading = any("清单" in heading for heading in _heading_texts(delivery_body))
    if not checklist_declared or not checklist_heading:
        evidence = ["final_guide.meta.json:/checklist_included", "final_guide.md"]
        issues.append(
            _issue(
                "checklist_missing",
                "最终攻略未同时满足checklist_included=true且正文包含清单章节",
                *evidence,
                checklist_declared=checklist_declared,
                checklist_heading=checklist_heading,
            )
        )

    if validated_meta is not None:
        declared = {_canonical_merged_field(value) for value in validated_meta.merged_fields}
        declared.discard("")
        for field in sorted(_expected_merged_fields(route_plan, lifestyle, pitfall) - declared):
            issues.append(
                _issue(
                    "merged_field_missing",
                    f"上游存在{field}产出，但Meta.merged_fields未申报该模块",
                    "final_guide.meta.json:/merged_fields",
                    field=field,
                )
            )

    needs_detail_targets = {
        (day.get("day"), poi.get("name"))
        for day in ((route_plan or {}).get("days") or [])
        if isinstance(day, dict)
        for poi in (day.get("pois") or [])
        if isinstance(poi, dict) and poi.get("needs_detail")
    }
    for detail in poi_details or []:
        if not isinstance(detail, dict):
            continue
        target = (detail.get("day"), detail.get("poi_name"))
        if target not in needs_detail_targets or any(
            route.get("route_segments") for route in detail.get("routes") or []
        ):
            continue
        issues.append(
            _issue(
                "poi_detail_content_empty",
                f"Day{target[0]} {target[1]}标记needs_detail但没有形成内部路线",
                "outputs/poi_details",
                day=target[0],
                poi_name=target[1],
            )
        )

    upstream_claims = collect_upstream_claims(
        route_plan,
        lifestyle,
        pitfall,
        poi_details,
        evidence_checks=evidence_checks,
        review_findings=review_findings,
    )
    validated_blocks: FinalGuideBlocks | None = None
    if isinstance(final_blocks, dict):
        try:
            validated_blocks = FinalGuideBlocks.model_validate(final_blocks)
        except ValidationError as exc:
            issues.append(
                _issue(
                    "invalid_final_guide_blocks",
                    f"最终攻略正文块未通过Schema校验：{exc.errors(include_url=False)}",
                    "final_guide.blocks.json",
                )
            )
    elif final_blocks is not None:
        issues.append(
            _issue(
                "invalid_final_guide_blocks",
                "最终攻略正文块不是JSON对象",
                "final_guide.blocks.json",
            )
        )

    expects_blocks = bool(upstream_claims) or bool(
        validated_meta and validated_meta.body_source == "blocks_v1"
    )
    if expects_blocks and validated_blocks is None and not any(
        issue["code"] == "invalid_final_guide_blocks" for issue in issues
    ):
        issues.append(
            _issue(
                "final_guide_blocks_missing",
                "上游已有事实级claims，但最终攻略没有保存正文块映射",
                "final_guide.blocks.json",
                "final_guide.md",
                upstream_claim_count=len(upstream_claims),
            )
        )

    if validated_blocks is not None:
        body_hash = rendered_body_sha256(delivery_body)
        rendered_body = render_final_guide_body(validated_blocks.blocks)
        if validated_blocks.rendered_body_sha256 != body_hash or rendered_body != delivery_body:
            issues.append(
                _issue(
                    "final_guide_blocks_stale",
                    "正文块渲染结果与当前final_guide.md正文不一致",
                    "final_guide.blocks.json",
                    "final_guide.md",
                    expected_body_sha256=body_hash,
                    actual_body_sha256=validated_blocks.rendered_body_sha256,
                )
            )
        try:
            rebuilt = prepare_final_guide_blocks(
                [block.model_dump(mode="json") for block in validated_blocks.blocks],
                upstream_claims,
            )
            if [block.model_dump(mode="json") for block in rebuilt] != [
                block.model_dump(mode="json") for block in validated_blocks.blocks
            ]:
                issues.append(
                    _issue(
                        "final_guide_claim_snapshots_stale",
                        "正文块保存的claim快照与当前上游产物不一致",
                        "final_guide.blocks.json",
                        "route_plan.json",
                        "outputs/lifestyle.json",
                        "outputs/pitfall.json",
                    )
                )
        except ValueError as exc:
            issues.append(
                _issue(
                    "invalid_final_guide_claim_mapping",
                    f"最终攻略存在无效的claim映射：{exc}",
                    "final_guide.blocks.json",
                )
            )

        if validated_meta is not None:
            if validated_meta.body_source != "blocks_v1":
                issues.append(
                    _issue(
                        "final_guide_blocks_meta_mismatch",
                        "正文块存在，但Meta.body_source未标记为blocks_v1",
                        "final_guide.meta.json:/body_source",
                        "final_guide.blocks.json",
                    )
                )
            if validated_meta.blocks_count != len(validated_blocks.blocks):
                issues.append(
                    _issue(
                        "final_guide_blocks_count_mismatch",
                        "Meta.blocks_count与实际正文块数量不一致",
                        "final_guide.meta.json:/blocks_count",
                        "final_guide.blocks.json:/blocks",
                    )
                )

    return issues
