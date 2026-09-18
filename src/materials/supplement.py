"""Shared, project-isolated material-gap supplementation kernel."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field

from src.config import MATERIAL_LIBRARY_DIR, get_light_chat_model
from src.materials import citation_check
from src.materials.baseline import compute_material_snapshot, validate_material_baseline
from src.materials.gap_check import check_material_gap
from src.materials.ingest import ingest_topic, material_processing_status
from src.materials.research_service import record_external_research
from src.workspace import store


MAX_AUTO_MATERIAL_GAP_SUPPLEMENT = 10
MAX_AUTO_COLLECT_PER_TRIGGER = 2
GAP_SUPPLEMENT_CANDIDATE_POOL = 25

_TITLE_TARGET_SUFFIXES = ("市", "区", "县", "自治州", "景区", "风景区", "风景名胜区")
_ROUTE_LIFESTYLE_QUERY_SUFFIXES = ("介绍", "攻略")
_LIFESTYLE_FIELD_ANGLE_WORDS = {
    "food": "美食",
    "outfit": "穿搭",
    "lodging": "住宿",
    "play": "游玩",
}


class MaterialSupplementResult(list):
    """List-compatible result; only ``indexed`` grants a local repair."""

    def __init__(
        self,
        indexed: list[dict] | None = None,
        *,
        rejected: list[dict] | None = None,
        pending: list[dict] | None = None,
        status: str = "failed",
        repair_scope: dict[str, Any] | None = None,
        blocked_reason: str = "",
        base_corpus_sha256: str | None = None,
        overlay_sha256: str | None = None,
    ):
        super().__init__(indexed or [])
        self.indexed = self
        self.rejected = rejected or []
        self.pending = pending or []
        self.status = status
        self.repair_scope = repair_scope or {}
        self.blocked_reason = blocked_reason
        self.base_corpus_sha256 = base_corpus_sha256
        self.overlay_sha256 = overlay_sha256

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "indexed": list(self.indexed),
            "rejected": self.rejected,
            "pending": self.pending,
            "repair_scope": self.repair_scope,
            "repair_granted": self.status == "indexed" and bool(self.indexed),
            "base_corpus_sha256": self.base_corpus_sha256,
            "overlay_sha256": self.overlay_sha256,
            **({"blocked_reason": self.blocked_reason} if self.blocked_reason else {}),
        }


class _TitleRelevanceBatch(BaseModel):
    relevant_indices: list[int] = Field(
        description="真的可能包含指定目的地/主题相关内容的候选编号，从0开始"
    )


def title_mentions_supplement_target(topic: str, entity_hint: str, title: str) -> bool:
    normalized_title = re.sub(r"\s+", "", title or "").casefold()
    terms: set[str] = set()
    for value in (topic, entity_hint):
        for raw_term in str(value or "").split():
            term = re.sub(r"\s+", "", raw_term).casefold()
            if len(term) >= 2:
                terms.add(term)
            for suffix in _TITLE_TARGET_SUFFIXES:
                if term.endswith(suffix):
                    core = term[: -len(suffix)]
                    if len(core) >= 2:
                        terms.add(core)
    return any(term in normalized_title for term in terms)


def filter_relevant_candidates(
    topic: str, entity_hint: str, candidates: list[dict]
) -> list[dict]:
    candidates = [
        candidate
        for candidate in candidates
        if title_mentions_supplement_target(
            topic, entity_hint, candidate.get("title", "")
        )
    ]
    if not candidates:
        return []
    try:
        model = get_light_chat_model(temperature=0).with_structured_output(
            _TitleRelevanceBatch, method="json_mode"
        )
    except Exception:
        return candidates
    titles = "\n".join(
        f"{index}. {candidate.get('title', '')}"
        for index, candidate in enumerate(candidates)
    )
    try:
        verdict: _TitleRelevanceBatch = model.invoke(
            f"目的地/主题：{topic}\n"
            f"正在为「{topic}」补充关于「{entity_hint}」的真实旅游/生活类素材。\n"
            "选择真的可能包含当地相关真实内容的编号；排除游戏、段子、其它地区和无关话题。\n"
            f"候选标题：\n{titles}\n"
            "以JSON回复relevant_indices整数数组。"
        )
    except Exception:
        return candidates
    valid = {
        index
        for index in verdict.relevant_indices
        if isinstance(index, int) and 0 <= index < len(candidates)
    }
    return [candidate for index, candidate in enumerate(candidates) if index in valid]


def entity_gap_query_seeds(entity: str, angle_word: str | None = None) -> list[str]:
    seeds = [entity]
    if angle_word:
        seeds.append(f"{entity} {angle_word}")
    seeds.extend(f"{entity} {suffix}" for suffix in _ROUTE_LIFESTYLE_QUERY_SUFFIXES)
    return seeds


def _base_identity(project_id: str, topic: str) -> tuple[str | None, str | None, str]:
    baseline = store.read_workspace(project_id, "material_baseline.json") or {}
    if baseline:
        destination = str(baseline.get("destination") or topic)
        check = validate_material_baseline(
            baseline,
            topic=topic,
            destination=destination,
        )
        if check["status"] != "passed":
            return baseline.get("corpus_sha256"), None, "material_baseline_changed"
    overlay_dir = store.project_material_library_dir(project_id)
    overlay_sha = compute_material_snapshot(
        topic, material_library_dir=overlay_dir
    )["corpus_sha256"]
    return baseline.get("corpus_sha256"), overlay_sha, "passed"


def _record_supplement_research(
    project_id: str,
    *,
    stage: str,
    target_key: str,
    query: str,
    status: str,
    result_count: int,
    metadata: dict[str, Any],
) -> None:
    actor = {
        "route": "route_agent",
        "lifestyle": "lifestyle_agent",
        "poi_detail": "poi_detail_agent",
    }.get(stage, "material_gap_supplement")
    try:
        record_external_research(
            project_id,
            actor=actor,
            stage=stage,
            target_key=target_key,
            query=query,
            source_kind="supplement",
            status=status,
            result_count=result_count,
            metadata=metadata,
        )
    except (FileNotFoundError, ValueError):
        pass


def attempt_auto_material_supplement(
    project_id: str,
    topic: str,
    query_seeds: list[str],
    search_keywords: list[str],
    *,
    stage: str,
    target_key: str,
    repair_scope: dict[str, Any],
    limit: int,
    gap_checker=None,
    candidate_filter=None,
    ingest_fn=None,
    processing_status_fn=None,
) -> MaterialSupplementResult:
    """Run the existing gap/filter/collect gates against a Project overlay."""
    base_sha, overlay_before, baseline_status = _base_identity(project_id, topic)

    def result(status: str, **kwargs: Any) -> MaterialSupplementResult:
        value = MaterialSupplementResult(
            status=status,
            repair_scope=repair_scope,
            base_corpus_sha256=base_sha,
            overlay_sha256=kwargs.pop("overlay_sha256", overlay_before),
            **kwargs,
        )
        _record_supplement_research(
            project_id,
            stage=stage,
            target_key=target_key,
            query=query_seeds[0] if query_seeds else "",
            status=status,
            result_count=len(value.indexed),
            metadata=value.as_dict(),
        )
        return value

    if baseline_status != "passed":
        return result("blocked", blocked_reason=baseline_status)
    if limit <= 0 or (
        store.read_material_gap_supplement_count(project_id)
        >= MAX_AUTO_MATERIAL_GAP_SUPPLEMENT
    ):
        return result("budget_exhausted")

    from src.materials.collectors.bilibili import (
        CollectorDependencyError,
        _load_existing_source_urls,
        check_collector_dependencies,
        collect_one,
        search_and_rank,
    )

    try:
        check_collector_dependencies()
    except CollectorDependencyError as exc:
        store.record_material_gap_supplement_blocked(project_id, str(exc))
        return result("blocked", blocked_reason=str(exc))

    overlay_dir = store.project_material_library_dir(project_id)
    try:
        resolved_gap_checker = gap_checker or check_material_gap
        try:
            has_gap = resolved_gap_checker(
                topic,
                query_seeds,
                additional_library_dirs=[overlay_dir],
            )
        except TypeError:
            has_gap = resolved_gap_checker(topic, query_seeds)
    except Exception as exc:
        reason = f"素材缺口检测失败，跳过自动补采：{exc}"
        store.record_material_gap_supplement_blocked(project_id, reason)
        return result("blocked", blocked_reason=reason)
    if not has_gap:
        return result("not_a_gap")

    try:
        candidates = search_and_rank(
            search_keywords,
            top_n=GAP_SUPPLEMENT_CANDIDATE_POOL,
        )
        existing_urls = _load_existing_source_urls(
            topic, library_dir=MATERIAL_LIBRARY_DIR
        ) | _load_existing_source_urls(topic, library_dir=overlay_dir)
        candidates = [
            item for item in candidates if item.get("source_url") not in existing_urls
        ]
        candidates = (candidate_filter or filter_relevant_candidates)(
            topic, query_seeds[0], candidates
        )[:limit]
    except Exception as exc:
        return result("failed", blocked_reason=f"candidate_search_failed:{exc}")

    collected: list[tuple[dict, dict, str]] = []
    for candidate in candidates:
        reservation_id = store.reserve_material_gap_budget(
            project_id, MAX_AUTO_MATERIAL_GAP_SUPPLEMENT
        )
        if reservation_id is None:
            break
        try:
            try:
                entry = collect_one(topic, candidate, library_dir=overlay_dir)
            except TypeError:
                entry = collect_one(topic, candidate)
        except Exception:
            entry = None
        if entry is None:
            store.release_material_gap_budget(project_id, reservation_id)
            continue
        collected.append((candidate, entry, reservation_id))

    ingest_error = ""
    if collected:
        try:
            prefix = "project-supplement-" + hashlib.sha256(
                project_id.encode("utf-8")
            ).hexdigest()[:12] + "-"
            resolved_ingest = ingest_fn or ingest_topic
            try:
                resolved_ingest(
                    topic,
                    library_dir=overlay_dir,
                    doc_id_prefix=prefix,
                )
            except TypeError:
                resolved_ingest(topic)
        except Exception as exc:
            ingest_error = str(exc)

    resolved_status = processing_status_fn or material_processing_status
    try:
        statuses = resolved_status(
            topic,
            [entry.get("source_file", "") for _, entry, _ in collected],
            library_dir=overlay_dir,
        )
    except TypeError:
        statuses = resolved_status(
            topic, [entry.get("source_file", "") for _, entry, _ in collected]
        )
    indexed: list[dict] = []
    rejected: list[dict] = []
    pending: list[dict] = []
    for candidate, entry, reservation_id in collected:
        source_file = entry.get("source_file", "")
        status = statuses.get(source_file, "pending")
        public_item = {
            "title": candidate.get("title", ""),
            "author": candidate.get("author", ""),
            "source_url": candidate.get("source_url", ""),
        }
        audit_item = {
            "path": stage,
            "topic": topic,
            "status": status,
            "source_file": source_file,
            "overlay": True,
            "reservation_id": reservation_id,
            **public_item,
        }
        if ingest_error and status == "pending":
            audit_item["ingest_error"] = ingest_error
        store.record_material_gap_supplement_item(project_id, audit_item)
        if status == "indexed":
            store.finalize_material_gap_budget(project_id, reservation_id)
            indexed.append(public_item)
        elif status == "rejected":
            store.release_material_gap_budget(project_id, reservation_id)
            rejected.append(public_item)
        else:
            store.release_material_gap_budget(project_id, reservation_id)
            pending.append(public_item)

    overlay_after = compute_material_snapshot(
        topic, material_library_dir=overlay_dir
    )["corpus_sha256"]
    _base_after, _overlay_after_check, baseline_after_status = _base_identity(
        project_id, topic
    )
    if baseline_after_status != "passed":
        return result(
            "blocked",
            indexed=indexed,
            rejected=rejected,
            pending=pending,
            overlay_sha256=overlay_after,
            blocked_reason=baseline_after_status,
        )
    return result(
        "indexed" if indexed else "failed",
        indexed=indexed,
        rejected=rejected,
        pending=pending,
        overlay_sha256=overlay_after,
        blocked_reason=ingest_error,
    )


def attempt_route_gap_supplement(
    project_id: str, topic: str, issues: list[dict]
) -> MaterialSupplementResult:
    entities: set[str] = set()
    for issue in issues:
        missing = issue.get("missing_entities") or []
        if missing:
            entities.update(missing)
        else:
            for poi in issue.get("pois") or []:
                entities.update(citation_check._extract_poi_names(poi))
    indexed: list[dict] = []
    rejected: list[dict] = []
    pending: list[dict] = []
    last: MaterialSupplementResult | None = None
    for entity in sorted(entities):
        remaining = MAX_AUTO_COLLECT_PER_TRIGGER - len(indexed)
        if remaining <= 0:
            break
        seeds = entity_gap_query_seeds(entity)
        last = attempt_auto_material_supplement(
            project_id,
            topic,
            seeds,
            seeds,
            stage="route",
            target_key=f"route:entity:{entity}",
            repair_scope={"entity": entity},
            limit=remaining,
        )
        indexed.extend(last.indexed)
        rejected.extend(last.rejected)
        pending.extend(last.pending)
    return MaterialSupplementResult(
        indexed,
        rejected=rejected,
        pending=pending,
        status="indexed" if indexed else (last.status if last else "not_a_gap"),
        repair_scope={"entities": sorted(entities)},
        base_corpus_sha256=last.base_corpus_sha256 if last else None,
        overlay_sha256=last.overlay_sha256 if last else None,
    )


def _route_poi_entities_by_day(project_id: str) -> dict[int, list[str]]:
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    result: dict[int, list[str]] = {}
    for day in route_plan.get("days") or []:
        pois = [citation_check._poi_display_name(p) for p in (day.get("pois") or [])]
        result[int(day.get("day") or 0)] = [
            name
            for poi in pois
            for name in citation_check._extract_poi_names(poi)
        ]
    return result


def attempt_lifestyle_gap_supplement(
    project_id: str, topic: str, issues: list[dict]
) -> MaterialSupplementResult:
    indexed: list[dict] = []
    rejected: list[dict] = []
    pending: list[dict] = []
    seen: set[tuple[str, str]] = set()
    route_pois = _route_poi_entities_by_day(project_id)
    last: MaterialSupplementResult | None = None
    for issue in issues:
        field = str(issue.get("field") or "")
        day = int(issue.get("day") or 0)
        entities = issue.get("missing_entities") or route_pois.get(day, [])
        for entity in entities:
            key = (field, entity)
            if key in seen:
                continue
            seen.add(key)
            remaining = MAX_AUTO_COLLECT_PER_TRIGGER - len(indexed)
            if remaining <= 0:
                break
            seeds = entity_gap_query_seeds(
                entity, _LIFESTYLE_FIELD_ANGLE_WORDS.get(field)
            )
            last = attempt_auto_material_supplement(
                project_id,
                topic,
                seeds,
                seeds,
                stage="lifestyle",
                target_key=f"lifestyle:{field}:day:{day}:entity:{entity}",
                repair_scope={"field": field, "day": day, "entity": entity},
                limit=remaining,
            )
            indexed.extend(last.indexed)
            rejected.extend(last.rejected)
            pending.extend(last.pending)
    return MaterialSupplementResult(
        indexed,
        rejected=rejected,
        pending=pending,
        status="indexed" if indexed else (last.status if last else "not_a_gap"),
        repair_scope={"items": [list(item) for item in sorted(seen)]},
        base_corpus_sha256=last.base_corpus_sha256 if last else None,
        overlay_sha256=last.overlay_sha256 if last else None,
    )


def attempt_poi_gap_supplement(
    project_id: str,
    topic: str,
    *,
    day: int,
    poi_name: str,
) -> MaterialSupplementResult:
    seeds = [poi_name, f"{poi_name} 讲解", f"{poi_name} 攻略"]
    return attempt_auto_material_supplement(
        project_id,
        topic,
        seeds,
        seeds,
        stage="poi_detail",
        target_key=f"poi_detail:day:{day}:poi:{poi_name}",
        repair_scope={"day": day, "poi_name": poi_name},
        limit=MAX_AUTO_COLLECT_PER_TRIGGER,
    )
