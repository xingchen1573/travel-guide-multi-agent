"""Deterministic diagnosis and repair for POI manifest status drift.

This is intentionally separate from model review: both sides are structured
system state, so the only valid write is a compare-and-swap reconciliation to
an independently evidenced terminal status.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.workspace import store as workspace_store

_TERMINAL_STATUSES = {"passed", "force_finalized", "generation_failed"}
_FAILED_RESULTS = {"fail", "error", "inconclusive"}


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _manifest_index(target: dict[str, Any]) -> int | None:
    raw = target.get("manifest_index")
    if isinstance(raw, int) and raw >= 0:
        return raw
    match = re.fullmatch(r"/generated/(\d+)", str(target.get("json_path") or ""))
    return int(match.group(1)) if match else None


def _claim_records(value: Any) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if isinstance(value, dict):
        claim_id = str(value.get("claim_id") or "").strip()
        if claim_id:
            records[claim_id] = {
                "claim_id": claim_id,
                "claim_text": str(value.get("claim_text") or value.get("claim") or ""),
                "target_path": str(value.get("target_path") or ""),
            }
        for child in value.values():
            records.update(_claim_records(child))
    elif isinstance(value, list):
        for child in value:
            records.update(_claim_records(child))
    return records


def _current_checks(
    project_id: str,
    *,
    poi_name: str,
    claim_records: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    current_ids = set(claim_records)
    checks = workspace_store.read_evidence_checks(project_id).get("poi_detail") or []
    latest_by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in checks:
        if not isinstance(raw, dict):
            continue
        claim_id = str(raw.get("claim_id") or "")
        if claim_id not in current_ids or str(raw.get("poi_name") or "") != poi_name:
            continue
        if str(raw.get("observation_mode") or "live") != "live":
            continue
        key = (claim_id, str(raw.get("scope") or ""))
        if str(raw.get("checked_at") or "") >= str(latest_by_scope.get(key, {}).get("checked_at") or ""):
            latest_by_scope[key] = raw

    projection: list[dict[str, Any]] = []
    for raw in sorted(
        latest_by_scope.values(),
        key=lambda item: (str(item.get("claim_id") or ""), str(item.get("scope") or "")),
    ):
        evidence = []
        for item in raw.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            evidence.append({
                "ref": str(item.get("chunk_id") or item.get("ref") or ""),
                "excerpt": str(item.get("excerpt") or item.get("text") or "")[:420],
            })
        projection.append({
            "claim_id": str(raw.get("claim_id") or ""),
            "claim_text": str(raw.get("claim_text") or raw.get("target_text") or ""),
            "target_path": str(raw.get("target_path") or ""),
            "scope": str(raw.get("scope") or ""),
            "result": str(raw.get("result") or ""),
            "reason": str(raw.get("reason") or ""),
            "checked_at": str(raw.get("checked_at") or ""),
            "evidence": evidence,
        })
    blockers_by_claim: dict[str, dict[str, Any]] = {}
    for item in projection:
        if item["result"] in _FAILED_RESULTS:
            blockers_by_claim.setdefault(item["claim_id"], item)
    covered_ids = {item["claim_id"] for item in projection}
    complete_without_failure = bool(current_ids) and covered_ids == current_ids and not blockers_by_claim
    return projection, list(blockers_by_claim.values()), complete_without_failure


def _matching_validation_receipt(project_id: str, detail: dict[str, Any]) -> dict[str, Any] | None:
    artifact_sha = _canonical_sha256(detail)
    receipts = [
        item
        for item in workspace_store.read_validation_runs(project_id)
        if isinstance(item, dict)
        and item.get("stage") == "poi_detail"
        and item.get("scope") == "stage"
        and item.get("observation_mode") == "live"
        and item.get("artifact_sha256") == artifact_sha
    ]
    if not receipts:
        return None
    latest = max(receipts, key=lambda item: str(item.get("checked_at") or ""))
    return {
        "status": str(latest.get("status") or ""),
        "checked_at": str(latest.get("checked_at") or ""),
        "artifact_sha256": artifact_sha,
    }


def _terminal_result(project_id: str, *, day: int, poi_name: str) -> dict[str, Any] | None:
    path = workspace_store.project_dir(project_id) / "agents" / "codex_skill" / "poi_results.json"
    payload = _read_json(path)
    rows = payload.get("results") if isinstance(payload, dict) else payload
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        try:
            item_day = int(item.get("day") or 0)
        except (TypeError, ValueError):
            item_day = 0
        if item_day != day or str(item.get("poi_name") or "") != poi_name:
            continue
        status = str(item.get("citation_status") or "")
        if status not in _TERMINAL_STATUSES:
            continue
        observed_at = ""
        try:
            observed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            pass
        return {
            "status": status,
            "source": "agents/codex_skill/poi_results.json",
            "observed_at": observed_at,
        }
    return None


def build_manifest_consistency_context(project_id: str, finding: Any) -> dict[str, Any] | None:
    if getattr(finding, "problem_type", "") != "manifest_mismatch":
        return None
    target = dict(getattr(finding, "target", {}) or {})
    if str(target.get("file") or "") != "outputs/poi_details/manifest.json":
        return None

    manifest_path = workspace_store.workspace_dir(project_id) / "outputs" / "poi_details" / "manifest.json"
    manifest = _read_json(manifest_path)
    entries = manifest.get("generated") if isinstance(manifest, dict) else None
    index = _manifest_index(target)
    entry = entries[index] if isinstance(entries, list) and index is not None and index < len(entries) else None
    mismatch_value = str(target.get("mismatch") or "")
    expected_filename = str(target.get("detail_file") or target.get("missing_file") or "")
    if not expected_filename and ":" in mismatch_value:
        expected_filename = mismatch_value.split(":", 1)[1]
    target_moved = bool(
        isinstance(entry, dict)
        and expected_filename
        and str(entry.get("file") or "") != expected_filename
    )
    if not isinstance(entry, dict) or target_moved:
        return {
            "schema_version": "manifest-consistency-v1",
            "mismatch_kind": "unresolved_target",
            "executable": False,
            "blocking_reason": "manifest 条目已移动或不存在，请先重新扫描状态。",
            "observed_fingerprint": _canonical_sha256({"target": target, "entry": entry}),
        }

    filename = str(entry.get("file") or target.get("detail_file") or target.get("missing_file") or "")
    detail_path = workspace_store.workspace_dir(project_id) / "outputs" / "poi_details" / filename
    detail = _read_json(detail_path) if filename else None
    poi_name = str(entry.get("poi_name") or target.get("poi_name") or "")
    try:
        day = int(entry.get("day") or target.get("day") or 0)
    except (TypeError, ValueError):
        day = 0
    missing = not isinstance(detail, dict)
    mismatch_kind = "missing_detail" if missing else "status"

    claim_records = _claim_records(detail) if isinstance(detail, dict) else {}
    checks, blockers, checks_passed = _current_checks(
        project_id,
        poi_name=poi_name,
        claim_records=claim_records,
    ) if isinstance(detail, dict) else ([], [], False)
    receipt = _matching_validation_receipt(project_id, detail) if isinstance(detail, dict) else None
    terminal = _terminal_result(project_id, day=day, poi_name=poi_name)
    current_passed = bool(receipt and receipt.get("status") == "passed") or checks_passed

    authoritative_status: str | None = None
    authority_basis = ""
    authority_reason = ""
    confidence = "none"
    if not missing and current_passed and not blockers:
        authoritative_status = "passed"
        authority_basis = "current_live_validation"
        authority_reason = "当前文件的 live 核验已覆盖现有 Claim，且没有未通过项。"
        confidence = "high"
    elif not missing and blockers:
        authoritative_status = "force_finalized"
        authority_basis = "current_failed_checks"
        authority_reason = f"当前 Claim 仍有 {len(blockers)} 个未通过，不能把该 POI 标成 passed。"
        confidence = "high"

    manifest_status = str(entry.get("citation_status") or "")
    detail_status = str(detail.get("citation_status") or "") if isinstance(detail, dict) else "missing"
    recommended_action = ""
    action_target = ""
    if missing:
        recommended_action = "targeted_poi_detail_regeneration"
        action_target = filename
    elif authoritative_status and manifest_status != detail_status:
        if authoritative_status == manifest_status:
            recommended_action = "sync_detail_status"
            action_target = f"outputs/poi_details/{filename}"
        elif authoritative_status == detail_status:
            recommended_action = "sync_manifest_status"
            action_target = f"outputs/poi_details/manifest.json#/generated/{index}"

    guide = workspace_store.read_workspace(project_id, "final_guide.md") or ""
    mention_count = guide.count(poi_name) if poi_name and isinstance(guide, str) else 0
    fingerprint_payload = {
        "finding_id": getattr(finding, "finding_id", ""),
        "canonical_key": getattr(finding, "canonical_key", ""),
        "manifest_index": index,
        "entry": entry,
        "detail": detail,
        "checks": checks,
        "receipt": receipt,
        "terminal": terminal,
    }
    executable = bool(
        not missing
        and authoritative_status
        and recommended_action in {"sync_detail_status", "sync_manifest_status"}
    )
    if missing:
        blocking_reason = "POI 详情文件不存在，需定向重跑该 POI，不能用状态同步代替。"
    elif not authoritative_status:
        blocking_reason = "没有找到能绑定当前详情文件的核验证据，历史 Agent 终态仅作审计线索。"
    elif not recommended_action:
        blocking_reason = "两侧已经与权威终态一致，无需再同步。"
    else:
        blocking_reason = ""

    return {
        "schema_version": "manifest-consistency-v1",
        "mismatch_kind": mismatch_kind,
        "manifest_index": index,
        "day": day,
        "poi_name": poi_name,
        "detail_file": filename,
        "manifest": {
            "status": manifest_status,
            "updated_at": str((manifest or {}).get("updated_at") or ""),
            "json_pointer": f"/generated/{index}",
        },
        "detail": {
            "status": detail_status,
            "updated_at": str(detail.get("updated_at") or "") if isinstance(detail, dict) else "",
            "file": f"outputs/poi_details/{filename}" if filename else "",
        },
        "terminal_result": terminal,
        "validation_receipt": receipt,
        "current_claim_count": len(claim_records),
        "current_check_count": len(checks),
        "current_blocker_count": len(blockers),
        "current_blockers": blockers[:12],
        "authoritative_status": authoritative_status,
        "authority_basis": authority_basis,
        "authority_reason": authority_reason,
        "confidence": confidence,
        "recommended_action": recommended_action,
        "action_target": action_target,
        "executable": executable,
        "blocking_reason": blocking_reason,
        "observed_fingerprint": _canonical_sha256(fingerprint_payload),
        "project_impact": {
            "needs_rerun": missing,
            "generation_blocked": False,
            "final_guide_mention_count": mention_count,
            "will_refresh": ["manifest/detail 状态对", "最终攻略 POI 附录", "引用附录", "静态网站"],
            "note": (
                "同步后会关闭这条状态冲突；未通过 Claim 仍会作为独立内容问题保留在项目复核队列。"
                if authoritative_status in {"force_finalized", "generation_failed"}
                else "状态同步只修复系统事实，不会改写 POI 正文或重跑整条路线。"
            ),
        },
    }


def execute_manifest_consistency_repair(
    project_id: str,
    finding_id: str,
    *,
    expected_fingerprint: str,
    actor_id: str = "admin_user",
    site_builder: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """CAS-guarded deterministic repair followed by derived-output refresh."""
    from src.review import store as review_store
    from src.workspace import delivery

    finding = review_store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    context = build_manifest_consistency_context(project_id, finding)
    if context is None:
        raise ValueError("该 finding 不是 POI manifest 确定性一致性问题")
    if context["observed_fingerprint"] != expected_fingerprint:
        raise ValueError("状态已变化，请刷新诊断后再执行")
    if not context.get("executable"):
        raise ValueError(str(context.get("blocking_reason") or "当前问题不能机械同步"))

    changed = workspace_store.reconcile_poi_citation_status(
        project_id,
        int(context["day"]),
        str(context["poi_name"]),
        str(context["authoritative_status"]),
        actor=actor_id,
    )
    manifest = workspace_store.read_workspace(project_id, "outputs/poi_details/manifest.json") or {}
    results = [dict(item) for item in manifest.get("generated") or [] if isinstance(item, dict)]
    requirements = workspace_store.read_requirements(project_id) or {}
    topic = str(requirements.get("topic") or "")
    if topic:
        delivery.refresh_reference_section(project_id, topic)
    delivery.sync_poi_details_into_final_guide(project_id, results)

    from src.codex_skill.bridge import validate_stage

    validation = validate_stage(project_id, "final")
    if site_builder is None:
        from scripts.delivery.run_generate_site import generate_site

        site_builder = generate_site
    site_output = site_builder(project_id)
    review_store.sync_state_findings(project_id)
    refreshed = review_store.get_finding(project_id, finding_id)
    if refreshed is None or refreshed.status != "closed":
        raise RuntimeError("manifest 同步后重扫仍未关闭原 finding")
    workspace_store.log_event(
        project_id,
        actor_id,
        "reconcile_poi_manifest_status",
        str(context.get("action_target") or context.get("detail_file") or ""),
        f"{context['recommended_action']}: {context['authoritative_status']}",
    )
    return {
        "status": "completed",
        "changed": changed,
        "action": context["recommended_action"],
        "authoritative_status": context["authoritative_status"],
        "finding": refreshed.model_dump(mode="json"),
        "validation": validation,
        "site_output_path": str(site_output),
    }
