"""Build the delivery view consumed by the Integration Agent.

Workspace outputs retain complete evidence for audit and validation. The
Integration Agent only needs the upstream conclusions and stable claim IDs;
passing raw search results and evidence snapshots into its prompt duplicates
responsibilities and can make a short itinerary larger than a long one.
"""
from __future__ import annotations

from typing import Any

from src.workspace.agent_context import route_map_day_view


_INTEGRATION_INPUT_PATHS = {
    "route_plan.json",
    "route_map_validation.json",
    "route_feasibility.json",
    "outputs/lifestyle.json",
    "outputs/pitfall.json",
    "final_guide.blocks.json",
}

_RAW_EVIDENCE_KEYS = {
    "evidence",
    "evidence_refs",
    "evidence_snapshots",
    "claim_snapshots",
    "sources",
    # Pitfall uses this as an audit binding to the original Lifestyle item.
    # Integration already joins the two outputs by field/day; exposing the old
    # prose can reintroduce claim text that the delivery projection withheld.
    "target_recommendation",
}


def _strip_raw_evidence(
    value: Any,
    deliverable_claim_ids: set[str] | None = None,
) -> Any:
    if isinstance(value, list):
        return [_strip_raw_evidence(item, deliverable_claim_ids) for item in value]
    if not isinstance(value, dict):
        return value

    raw_claims = value.get("claims") if isinstance(value.get("claims"), list) else None
    filtered_claims = None
    if raw_claims is not None and deliverable_claim_ids is not None:
        filtered_claims = [
            claim
            for claim in raw_claims
            if isinstance(claim, dict)
            and str(claim.get("claim_id") or "") in deliverable_claim_ids
        ]

    projected: dict[str, Any] = {}
    for key, item in value.items():
        if key in _RAW_EVIDENCE_KEYS:
            continue
        if (
            key in {"recommendation", "warning"}
            and filtered_claims is not None
            and len(filtered_claims) != len(raw_claims or [])
        ):
            claim_texts = []
            for claim in filtered_claims:
                if str(claim.get("target_path") or key) != key:
                    continue
                claim_text = str(claim.get("claim_text") or "").strip()
                if claim_text and claim_text not in claim_texts:
                    claim_texts.append(claim_text)
            projected[key] = "；".join(claim_texts)
            continue
        if key == "claims" and isinstance(item, list) and deliverable_claim_ids is not None:
            projected[key] = [
                _strip_raw_evidence(claim, deliverable_claim_ids)
                for claim in (filtered_claims or [])
            ]
            continue
        if key == "upstream_claim_ids" and isinstance(item, list) and deliverable_claim_ids is not None:
            projected[key] = [
                claim_id for claim_id in item if str(claim_id) in deliverable_claim_ids
            ]
            continue
        if key == "technical_error":
            # Integration only needs to distinguish an evidence gap from a
            # technical failure. Provider/error details remain in Workspace.
            projected[key] = bool(item)
            continue
        projected[key] = _strip_raw_evidence(item, deliverable_claim_ids)
    return projected


def integration_delivery_view(
    relative_path: str,
    data: Any,
    *,
    deliverable_claim_ids: set[str] | None = None,
    route_plan: dict | None = None,
) -> Any:
    """Return the evidence-free view for known Integration input files."""
    normalized = str(relative_path or "").strip().lstrip("./")
    if normalized not in _INTEGRATION_INPUT_PATHS:
        return data
    if normalized == "route_map_validation.json":
        report = data if isinstance(data, dict) else {}
        route_days = [
            int(item.get("day") or 0)
            for item in ((route_plan or {}).get("days") or [])
            if isinstance(item, dict) and item.get("day") is not None
        ]
        views = [route_map_day_view(route_plan, report, day) for day in route_days]
        non_current = next(
            (view.get("status") for view in views if view.get("status") != "current"),
            None,
        )
        return {
            "route_plan_structure_hash": str(report.get("route_plan_structure_hash") or ""),
            "status": "current" if views and non_current is None else (non_current or "missing"),
            "validation_status": str(report.get("status") or "unknown"),
            "days": views,
        }
    if normalized == "route_feasibility.json" and isinstance(data, dict):
        # Passed checks have already completed their gatekeeping job. Replaying
        # every successful Web judgment into Integration adds no user-facing
        # instruction and made the Chengdu 3-day prompt larger than necessary.
        # Keep the aggregate outcome plus only checks that still require the
        # final guide to carry an impact, caveat, or action. The complete report
        # remains untouched in Workspace for audit and later review.
        projected = {
            "overall_status": data.get("overall_status"),
            "force_finalized": bool(data.get("force_finalized")),
            "checks": [
                check
                for check in (data.get("checks") or [])
                if isinstance(check, dict)
                and (
                    str(check.get("status") or "").lower() != "passed"
                    or bool(check.get("technical_error"))
                )
            ],
        }
        return _strip_raw_evidence(projected, deliverable_claim_ids)
    return _strip_raw_evidence(data, deliverable_claim_ids)
