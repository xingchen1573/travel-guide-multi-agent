"""Structured final-guide blocks and their mappings to upstream claims."""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Optional

from pydantic import ValidationError

from travel_guide_kernel.schema import FinalGuideBlock, FinalGuideClaimSnapshot


_BLOCKING_EVIDENCE_RESULTS = {"fail", "error", "inconclusive"}
_REVIEW_PASS_STATUSES = {"false_positive", "passed_review"}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rendered_body_sha256(body: str) -> str:
    return _sha256((body or "").strip())


def _register_claim(
    collected: dict[str, FinalGuideClaimSnapshot],
    raw_claim: Any,
    *,
    source_path: str,
    usage_key: str,
) -> None:
    if not isinstance(raw_claim, dict):
        return
    claim_id = str(raw_claim.get("claim_id") or "").strip()
    claim_text = str(raw_claim.get("claim_text") or "").strip()
    if not claim_id or not claim_text:
        return
    snapshot = FinalGuideClaimSnapshot(
        claim_id=claim_id,
        source_path=source_path,
        usage_key=usage_key,
        claim_text=claim_text,
        claim_type=str(raw_claim.get("claim_type") or "other"),
        support_mode=str(raw_claim.get("support_mode") or "unverified"),
        evidence_refs=raw_claim.get("evidence_refs") or [],
    )
    existing = collected.get(claim_id)
    if existing is not None and existing.model_dump(mode="json") != snapshot.model_dump(mode="json"):
        raise ValueError(f"上游claim_id重复且内容冲突: {claim_id}")
    collected[claim_id] = snapshot


def _latest_review_records(review_findings: Optional[list[dict]]) -> list[dict]:
    latest: dict[str, dict] = {}
    for item in review_findings or []:
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get("finding_id") or "").strip()
        if finding_id:
            latest[finding_id] = item
    return list(latest.values())


def _review_pass_keys(review_findings: Optional[list[dict]]) -> set[str]:
    passed: set[str] = set()
    for item in _latest_review_records(review_findings):
        if str(item.get("status") or "") != "closed":
            continue
        disposition = str(item.get("disposition") or "").split(".")[-1].lower()
        verification_status = str(item.get("verification_status") or "").lower()
        if disposition != "false_positive" and verification_status not in _REVIEW_PASS_STATUSES:
            continue
        canonical_key = str(item.get("canonical_key") or "").strip()
        if canonical_key:
            passed.add(canonical_key)
    return passed


def _blocked_claim_ids(evidence_checks: Optional[dict]) -> set[str]:
    blocked: set[str] = set()
    if not isinstance(evidence_checks, dict):
        return blocked
    for section in ("route", "lifestyle", "pitfall", "poi_detail"):
        for check in evidence_checks.get(section) or []:
            if not isinstance(check, dict):
                continue
            claim_id = str(check.get("claim_id") or "").strip()
            result = str(check.get("result") or "").strip().lower()
            if claim_id and result in _BLOCKING_EVIDENCE_RESULTS:
                blocked.add(claim_id)
    return blocked


def _review_allows_usage(usage_key: str, review_pass_keys: set[str]) -> bool:
    aliases = {usage_key}
    parts = usage_key.split(":")
    if len(parts) >= 3 and parts[:2] == ["route", "day"]:
        aliases.add(f"route_day:{parts[2]}")
    if len(parts) >= 5 and parts[:2] == ["poi_detail", "day"] and parts[3] == "poi":
        aliases.add(f"poi_detail:{parts[2]}:{parts[4]}")
    return any(
        alias == canonical_key or alias.startswith(f"{canonical_key}:")
        for alias in aliases
        for canonical_key in review_pass_keys
    )


def collect_upstream_claims(
    route_plan: Optional[dict],
    lifestyle: Optional[dict],
    pitfall: Optional[dict],
    poi_details: Optional[Iterable[dict]] = None,
    *,
    evidence_checks: Optional[dict] = None,
    review_findings: Optional[list[dict]] = None,
) -> dict[str, FinalGuideClaimSnapshot]:
    """Collect current claims that may be delivered as upstream facts.

    Existing projects without an evidence ledger keep their legacy behavior.
    When current claim-level checks exist, unresolved machine failures are held
    out of the verified upstream pool. A closed false-positive/passed review on
    the owning object restores eligibility without mutating the business JSON.
    Estimates and user constraints normally have no blocking checker result and
    therefore remain available under their original support mode.
    """
    collected: dict[str, FinalGuideClaimSnapshot] = {}
    blocked_claim_ids = _blocked_claim_ids(evidence_checks)
    review_pass_keys = _review_pass_keys(review_findings)

    def register(raw_claim: Any, *, source_path: str, usage_key: str) -> None:
        claim_id = str(raw_claim.get("claim_id") or "").strip() if isinstance(raw_claim, dict) else ""
        if claim_id in blocked_claim_ids and not _review_allows_usage(usage_key, review_pass_keys):
            return
        _register_claim(
            collected,
            raw_claim,
            source_path=source_path,
            usage_key=usage_key,
        )

    for day_index, day in enumerate((route_plan or {}).get("days") or []):
        if not isinstance(day, dict):
            continue
        day_number = day.get("day")
        usage_key = f"route:day:{day_number}"
        for claim_index, claim in enumerate(day.get("claims") or []):
            register(
                claim,
                source_path=f"route_plan.json:/days/{day_index}/claims/{claim_index}",
                usage_key=usage_key,
            )

    for owner, output in (("lifestyle", lifestyle), ("pitfall", pitfall)):
        for field in ("food", "outfit", "lodging", "play"):
            section = (output or {}).get(field)
            items = section.get("items") or [] if isinstance(section, dict) else []
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                usage_key = f"{owner}:{field}:day:{item.get('day')}:item:{item_index}"
                for claim_index, claim in enumerate(item.get("claims") or []):
                    register(
                        claim,
                        source_path=(
                            f"outputs/{owner}.json:/{field}/items/{item_index}/claims/{claim_index}"
                        ),
                        usage_key=usage_key,
                    )

    for detail_index, detail in enumerate(poi_details or []):
        if not isinstance(detail, dict):
            continue
        day_number = detail.get("day")
        poi_name = detail.get("poi_name", "")
        for route_index, route in enumerate(detail.get("routes") or []):
            if not isinstance(route, dict):
                continue
            route_name = route.get("route_name", "")
            for segment_index, segment in enumerate(route.get("route_segments") or []):
                if not isinstance(segment, dict):
                    continue
                order = segment.get("order")
                if order is None:
                    order = segment_index + 1
                usage_key = (
                    f"poi_detail:day:{day_number}:poi:{poi_name}:"
                    f"route:{route_name}:order:{order}"
                )
                for claim_index, claim in enumerate(segment.get("claims") or []):
                    register(
                        claim,
                        source_path=(
                            f"outputs/poi_details[{detail_index}]:/routes/{route_index}/"
                            f"route_segments/{segment_index}/claims/{claim_index}"
                        ),
                        usage_key=usage_key,
                    )

    return collected


def _block_id(block: FinalGuideBlock) -> str:
    seed = "|".join(
        [
            block.section,
            block.mapping_mode,
            block.markdown,
            *block.upstream_claim_ids,
        ]
    )
    return f"guide-block-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"


def prepare_final_guide_blocks(
    raw_blocks: list[dict],
    upstream_claims: dict[str, FinalGuideClaimSnapshot],
) -> list[FinalGuideBlock]:
    """Validate model input and replace all model-supplied snapshots with current data."""
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ValueError("blocks必须是非空数组")

    prepared: list[FinalGuideBlock] = []
    seen_block_ids: set[str] = set()
    for index, raw_block in enumerate(raw_blocks):
        if not isinstance(raw_block, dict):
            raise ValueError(f"blocks[{index}]必须是JSON对象")
        candidate = dict(raw_block)
        candidate["section"] = str(candidate.get("section") or "").strip()
        candidate["markdown"] = str(candidate.get("markdown") or "").strip()
        candidate["mapping_note"] = str(candidate.get("mapping_note") or "").strip()
        candidate["upstream_claim_ids"] = list(candidate.get("upstream_claim_ids") or [])
        candidate["block_id"] = ""
        candidate["claim_snapshots"] = []
        if not candidate["section"]:
            raise ValueError(f"blocks[{index}].section不能为空")
        if not candidate["markdown"]:
            raise ValueError(f"blocks[{index}].markdown不能为空")
        try:
            block = FinalGuideBlock.model_validate(candidate)
        except ValidationError as exc:
            raise ValueError(f"blocks[{index}]未通过Schema校验: {exc}") from exc

        claim_ids = [str(claim_id).strip() for claim_id in block.upstream_claim_ids if str(claim_id).strip()]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError(f"blocks[{index}].upstream_claim_ids不能重复")
        block.upstream_claim_ids = claim_ids

        if block.mapping_mode == "upstream":
            if not claim_ids:
                raise ValueError(f"blocks[{index}]是upstream块，必须填upstream_claim_ids")
            missing = [claim_id for claim_id in claim_ids if claim_id not in upstream_claims]
            if missing:
                raise ValueError(f"blocks[{index}]引用了不存在的上游claim: {missing}")
            block.claim_snapshots = [upstream_claims[claim_id] for claim_id in claim_ids]
        elif claim_ids:
            raise ValueError(
                f"blocks[{index}]的mapping_mode={block.mapping_mode}，不应填upstream_claim_ids"
            )

        if block.mapping_mode == "unverified" and not block.mapping_note:
            raise ValueError(f"blocks[{index}]是未核验内容，必须说明mapping_note")

        block.block_id = _block_id(block)
        if block.block_id in seen_block_ids:
            raise ValueError(f"blocks[{index}]与前面的正文块完全重复")
        seen_block_ids.add(block.block_id)
        prepared.append(block)

    return prepared


def render_final_guide_body(blocks: Iterable[FinalGuideBlock]) -> str:
    return "\n\n".join(block.markdown.strip() for block in blocks if block.markdown.strip()).strip()
