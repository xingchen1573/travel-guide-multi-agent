"""Deterministic final-delivery assembly shared by orchestration harnesses."""
from __future__ import annotations

from src.materials import citation_check
from src.workspace import store
from src.workspace.final_guide_blocks import collect_upstream_claims
from src.workspace.final_guide_sections import (
    POI_SECTION_HEADING,
    compose_final_guide_sections,
    split_final_guide_sections,
)
from src.workspace.schema import (
    FinalGuideWarning,
    PoiDetailManifest,
    PoiDetailManifestEntry,
    ProjectPhase,
)


def _has_explicit_degradation(value: object) -> bool:
    """Return whether an artifact records an existing degraded terminal state."""
    if isinstance(value, dict):
        if value.get("citation_status") in {"force_finalized", "generation_failed"}:
            return True
        return any(_has_explicit_degradation(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_explicit_degradation(item) for item in value)
    return False


def _collect_operational_warnings(project_id: str) -> list[FinalGuideWarning]:
    """Project only POI operational findings without changing the route."""
    report = store.read_route_feasibility(project_id)
    if report is None:
        return []

    warnings: list[FinalGuideWarning] = []
    seen: set[str] = set()
    for check in report.checks:
        if (
            check.check_id in seen
            or check.target_type != "poi"
            or check.category
            not in {"poi_operational_status", "reservation_or_quota"}
            or check.status == "passed"
        ):
            continue
        seen.add(check.check_id)

        reservation_check = check.category == "reservation_or_quota"
        technical_error = bool(
            check.technical_error
            or check.technical_error_class
            or check.stream_completed is False
        )
        summary = check.summary.strip().rstrip("。")
        if reservation_check:
            if technical_error:
                warning_type = "verification_incomplete"
                message = "本次自动预约规则核验未完成，预约和名额条件仍需在出发前通过官方渠道确认。"
            elif check.status == "blocked":
                warning_type = "operational_restriction"
                message = (
                    f"核验记录提示：{summary}。出发前请再次确认预约和名额规则。"
                    if summary
                    else "核验记录存在预约或名额限制，出发前请再次确认。"
                )
            else:
                warning_type = "operational_uncertainty"
                suggested_action = check.suggested_action.strip().rstrip("。")
                action_is_reservation_specific = bool(suggested_action) and any(
                    term in suggested_action
                    for term in ("预约", "名额", "配额", "限额")
                )
                if action_is_reservation_specific:
                    message = (
                        f"核验记录提示：{summary}。{suggested_action}。"
                        if summary
                        else f"{suggested_action}。"
                    )
                else:
                    message = (
                        f"核验记录提示：{summary}。出发前请再次确认预约和名额规则。"
                        if summary
                        else "预约和名额规则可能变化，出发前请再次确认。"
                    )
        elif technical_error:
            warning_type = "verification_incomplete"
            message = "本次自动运营核验未完成，开放、进入或管制条件仍需在出发前通过官方渠道确认。"
        elif check.status == "blocked":
            warning_type = "operational_restriction"
            message = (
                f"核验记录提示：{summary}。出发前请再次确认当前开放和进入条件。"
                if summary
                else "核验记录存在运营限制，出发前请再次确认当前开放和进入条件。"
            )
        else:
            warning_type = "operational_uncertainty"
            message = (
                f"核验记录提示：{summary}。出发前请再次确认当前开放和进入条件。"
                if summary
                else "开放和进入条件可能变化，出发前请再次确认。"
            )

        warnings.append(
            FinalGuideWarning(
                day=check.day,
                poi_name=check.target,
                warning_type=warning_type,
                message=message,
                evidence_refs=[check.check_id],
            )
        )
    return warnings


def _warning_lines(warnings: list[FinalGuideWarning]) -> list[str]:
    if not warnings:
        return []
    lines = ["### 重要运营提醒", ""]
    for warning in warnings:
        lines.append(
            f"> **Day{warning.day} {warning.poi_name}**：{warning.message}"
        )
    lines.append("")
    return lines


def replace_reference_section(
    project_id: str,
    topic: str,
    cited_chunks: dict,
    chunk_texts: dict,
) -> None:
    """Replace the program-owned reference appendix and matching meta data."""
    content = store.read_workspace(project_id, "final_guide.md") or ""
    body, poi_appendix, _old_reference_appendix = split_final_guide_sections(content)
    ref_section, references = citation_check.build_reference_section(cited_chunks, chunk_texts)
    new_content = compose_final_guide_sections(body, poi_appendix, ref_section)

    meta = store.read_workspace(project_id, "final_guide.meta.json") or {}
    meta["references"] = references
    store.write_final_guide(project_id, "integration_agent", new_content, meta)


def refresh_reference_section(project_id: str, topic: str) -> None:
    """Rebuild references from all currently active structured outputs."""
    route_plan = store.read_workspace(project_id, "route_plan.json")
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    poi_details = [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]
    cited_chunks = citation_check.collect_cited_chunks(
        route_plan, lifestyle, pitfall, poi_details
    )
    chunk_texts = citation_check.load_chunk_texts(topic, project_id)
    replace_reference_section(project_id, topic, cited_chunks, chunk_texts)


def sync_poi_details_into_final_guide(project_id: str, results: list[dict]) -> None:
    """Rebuild the program-owned POI appendix without invoking a model."""
    content = store.read_workspace(project_id, "final_guide.md") or ""
    body, old_poi_appendix, reference_appendix = split_final_guide_sections(content)
    warnings = _collect_operational_warnings(project_id)

    if not results:
        if not old_poi_appendix and not warnings:
            return
        warning_appendix = ""
        if warnings:
            warning_appendix = "\n".join(
                [POI_SECTION_HEADING, "", *_warning_lines(warnings)]
            ).rstrip()
        new_content = compose_final_guide_sections(body, warning_appendix, reference_appendix)
        meta = store.read_workspace(project_id, "final_guide.meta.json") or {}
        meta["warnings"] = [warning.model_dump(mode="json") for warning in warnings]
        store.write_final_guide(project_id, "integration_agent", new_content, meta)
        store.refresh_final_guide_delivery_hash(project_id, actor="main_agent")
        return

    poi_details = [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]
    deliverable_claim_ids = set(
        collect_upstream_claims(
            store.read_workspace(project_id, "route_plan.json"),
            store.read_workspace(project_id, "outputs/lifestyle.json"),
            store.read_workspace(project_id, "outputs/pitfall.json"),
            poi_details,
            evidence_checks=store.read_evidence_checks(project_id),
            review_findings=store.read_workspace(project_id, "review_findings.jsonl") or [],
        )
    )

    def field_is_deliverable(segment, field: str) -> bool:
        claims = [claim for claim in segment.claims if claim.target_path == field]
        return not claims or all(claim.claim_id in deliverable_claim_ids for claim in claims)

    lines = [POI_SECTION_HEADING, "", *_warning_lines(warnings)]
    for result in results:
        day = result["day"]
        poi_name = result["poi_name"]
        citation_status = result["citation_status"]
        file_name = f"day{day}_{store.poi_slug(poi_name)}.json"
        detail = store.read_poi_detail(project_id, day, poi_name)

        lines.append(f"### Day{day} {poi_name}")
        if citation_status == "force_finalized":
            lines.append("> ⚠️ 部分内容未通过自动核验，已如实标注，仅供参考。")
            if detail is not None and detail.auto_collected:
                lines.append(
                    "> ✅ 系统检测到素材不足，已自动补充采集以下素材到素材库"
                    "（本次生成内容未变，下次重新生成本景点详情时有机会获得更完善的素材支持）："
                )
                for collected in detail.auto_collected:
                    author = f"（{collected['author']}）" if collected.get("author") else ""
                    lines.append(
                        f">   - [{collected.get('title', '')}]"
                        f"({collected.get('source_url', '')}){author}"
                    )
        if citation_status == "generation_failed":
            lines.append("> ⚠️ 该景点的深度玩法路线本次未能成功生成，可稍后单独重新生成。")
            lines.append("")
            continue
        if detail is None:
            lines.append(f"详情见 `outputs/poi_details/{file_name}`。")
            lines.append("")
            continue

        recommended = next((route for route in detail.routes if route.recommended), None)
        display_route = recommended or (detail.routes[0] if detail.routes else None)
        if display_route is not None:
            if recommended is not None:
                route_label = "推荐路线"
            elif len(detail.routes) > 1:
                route_label = "可选路线之一"
            else:
                route_label = "路线"
            waypoints = display_route.route_segments[:5]
            summary_parts = []
            for segment in waypoints:
                if not segment.point_name or not field_is_deliverable(segment, "point_name"):
                    continue
                if segment.highlight and field_is_deliverable(segment, "highlight"):
                    summary_parts.append(f"{segment.point_name}（{segment.highlight}）")
                else:
                    summary_parts.append(segment.point_name)
            if summary_parts:
                summary = "、".join(summary_parts)
                lines.append(f"{route_label}「{display_route.route_name}」途经：{summary}。")
            else:
                lines.append(
                    f"{route_label}「{display_route.route_name}」的具体途经点尚未通过自动核验，"
                    "已提交复核。"
                )
        lines.append(f"完整途经点顺序、建议停留时间见 `outputs/poi_details/{file_name}`。")
        lines.append("")

    new_content = compose_final_guide_sections(body, "\n".join(lines), reference_appendix)
    meta = store.read_workspace(project_id, "final_guide.meta.json") or {}
    meta["warnings"] = [warning.model_dump(mode="json") for warning in warnings]
    store.write_final_guide(project_id, "integration_agent", new_content, meta)
    store.refresh_final_guide_delivery_hash(project_id, actor="main_agent")


def finalize_poi_details(project_id: str, topic: str, results: list[dict]) -> dict:
    """Write the manifest, refresh appendices, and mark a full run complete."""
    allowed_statuses = {"passed", "force_finalized", "generation_failed"}
    for result in results:
        status = str(result.get("citation_status") or "")
        if status not in allowed_statuses:
            raise ValueError(f"unknown POI citation_status: {status or '<empty>'}")
    entries = [
        PoiDetailManifestEntry(
            poi_name=result["poi_name"],
            day=result["day"],
            file=f"day{result['day']}_{store.poi_slug(result['poi_name'])}.json",
            citation_status=result["citation_status"],
            failure_kind=result.get("failure_kind"),
            failure_summary=result.get("failure_summary", ""),
            generation_mode=result.get("generation_mode", "normal"),
            recovery_trigger=result.get("recovery_trigger"),
        )
        for result in results
    ]
    # The join result is the terminal authority.  Persist it back to each
    # generated detail before publishing the aggregate manifest so Codex and
    # LangGraph runs cannot leave two contradictory citation states behind.
    for result, entry in zip(results, entries):
        status = entry.citation_status
        day = entry.day
        poi_name = entry.poi_name
        if status == "passed":
            store.mark_poi_citation_passed(
                project_id,
                day,
                poi_name,
                actor="main_agent",
                retry_history=result.get("retry_history"),
            )
        elif status == "force_finalized":
            store.mark_poi_force_finalize(
                project_id,
                day,
                poi_name,
                actor="main_agent",
                auto_collected=result.get("auto_collected"),
                retry_history=result.get("retry_history"),
            )
        elif status == "generation_failed":
            store.mark_poi_generation_failed(
                project_id,
                day,
                poi_name,
                actor="main_agent",
                failure_summary=str(result.get("failure_summary") or "POI 详情生成失败"),
                retry_history=result.get("retry_history"),
            )
    manifest = PoiDetailManifest(generated=entries)
    store.write_poi_details_manifest(project_id, "main_agent", manifest)
    if results:
        refresh_reference_section(project_id, topic)
    sync_poi_details_into_final_guide(project_id, results)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    experiment = store.read_workspace(project_id, "experiment.json")
    if isinstance(experiment, dict):
        feasibility = store.read_workspace(project_id, "route_feasibility.json") or {}
        route_has_findings = any(
            isinstance(check, dict) and check.get("status") != "passed"
            for check in feasibility.get("checks") or []
        )
        content_has_degradation = _has_explicit_degradation(
            [
                store.read_workspace(project_id, "outputs/lifestyle.json") or {},
                store.read_workspace(project_id, "outputs/pitfall.json") or {},
                results,
            ]
        )
        updated = {
            key: value
            for key, value in experiment.items()
            if key not in {"schema_version", "project_id", "updated_at"}
        }
        updated["completion_mode"] = (
            "completed_with_degradation"
            if route_has_findings or content_has_degradation
            else "completed"
        )
        store.write_experiment_manifest(project_id, updated)
    return manifest.model_dump(mode="json")
