"""System-level route feasibility checks built from the confirmed route structure."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from src.config import get_structured_chat_model as get_chat_model
from src.structured_output import bind_structured_json
from src.materials import timeliness_check
from src.materials.web_evidence import (
    find_latest_web_search_entry,
    render_web_search_log_entry,
)
from src.tools.web_search_tools import make_web_search_tools
from src.workspace import store
from src.workspace.route_validation import route_structure_snapshot
from src.workspace.schema import (
    RouteFeasibilityCheck,
    RouteFeasibilityDateContext,
    RouteFeasibilityEvidence,
    RouteFeasibilityReport,
)


_POI_CATEGORIES = {
    "poi_operational_status",
    "reservation_or_quota",
    "entry_qualification",
    "opening_window",
    "last_entry",
    "seasonal_closure",
    "physical_state_change",
    "schedule_vs_opening_window",
    "user_goal_alignment",
}
_POI_CATEGORY_ORDER = (
    "poi_operational_status",
    "reservation_or_quota",
    "entry_qualification",
    "opening_window",
    "last_entry",
    "seasonal_closure",
    "physical_state_change",
    "schedule_vs_opening_window",
    "user_goal_alignment",
)
_CLAIM_CATEGORY_MARKERS = {
    "reservation_or_quota": (
        "预约", "限流", "名额", "预售", "实名", "reservation", "quota",
    ),
    "entry_qualification": (
        "入场资格", "入园资格", "入馆资格", "证件", "身份证", "护照", "免票", "优惠票",
        "年龄限制", "qualification", "eligibility",
    ),
    "opening_window": (
        "开放时间", "营业时间", "全天开放", "开放至", "开放到", "营业至", "营业到",
        "开馆", "闭馆", "开园", "闭园",
        "opening", "business hours",
    ),
    "last_entry": (
        "停止入园", "停止入馆", "停止入场", "停止进入", "停止售票", "停止检票", "最晚入场",
        "last entry",
    ),
    "seasonal_closure": (
        "季节性关闭", "季节性封闭", "封山", "冬季关闭", "雨季关闭", "雪季关闭", "枯水期",
        "seasonal closure",
    ),
    "physical_state_change": (
        "水位", "干涸", "结冰", "塌方", "落石", "泥石流", "施工", "封闭",
        "physical state",
    ),
}
_CATEGORY_QUERY_VARIANTS = {
    "poi_operational_status": (
        "最新状态 官方公告 暂停开放 恢复开放",
        "当前运营 临时关闭 恢复运营 地址变更",
        "政府 文旅 本地生活 最新消息 关闭 重开",
    ),
    "reservation_or_quota": (
        "预约 限流 官方",
        "参观须知 预约规则",
        "票务 预约名额 最新公告",
    ),
    "entry_qualification": (
        "入场要求 证件 官方",
        "参观资格 优惠免票规则",
        "入园限制 最新公告",
    ),
    "opening_window": (
        "开放时间 营业时间 官方",
        "最新开放安排 闭馆时间",
        "当前开放 今日营业",
    ),
    "last_entry": (
        "停止入园 停止售票 官方",
        "最晚入场 闭馆时间",
        "参观时间 最新公告",
    ),
    "seasonal_closure": (
        "季节开放 临时关闭 官方",
        "封山 封闭 季节公告",
        "政府 文旅 当前开放状态",
    ),
    "physical_state_change": (
        "当前状态 现场情况 官方",
        "施工 封闭 状态变化",
        "政府 公告 最新现场情况",
    ),
    "schedule_vs_opening_window": (
        "开放时间 最晚入场 官方",
        "营业时间 行程时间 最新",
        "参观时间 停止入园 公告",
    ),
    "user_goal_alignment": (
        "体验时间 开放时间 官方",
        "体验条件 最新须知",
        "可执行时间 官方公告",
    ),
}
_SEARCH_FAILURE_MARKERS = (
    "联网搜索未配置",
    "联网搜索没有找到相关结果",
    "联网搜索请求失败",
    "联网搜索所有已配置的key都不可用",
)
_TIME_PATTERN = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
_TRAVELER_RESTRICTION_MARKERS = (
    "老人", "儿童", "孩子", "带娃", "学生", "孕妇", "轮椅", "残障", "军人", "外籍", "护照",
)
_EXPLICIT_GOAL_MARKERS = (
    "想", "希望", "期待", "一定要", "必须", "重点", "专门",
    "体验", "观看", "欣赏", "拍摄", "拍照", "打卡", "朝圣",
    "日出", "日落", "云海", "星空", "夜景",
)
_NEGATED_GOAL_MARKERS = ("不想去", "不去", "避开", "跳过", "取消")
_OBSERVABLE_GOAL_TERMS = ("日出", "日落", "云海", "星空", "夜景")
_EXPLICIT_OPERATIONAL_STATUS_MARKERS = (
    "正常开放",
    "暂停开放",
    "恢复开放",
    "临时关闭",
    "恢复运营",
    "暂停运营",
    "停运",
)


class PoiFeasibilityFinding(BaseModel):
    category: Literal[
        "poi_operational_status",
        "reservation_or_quota",
        "entry_qualification",
        "opening_window",
        "last_entry",
        "seasonal_closure",
        "physical_state_change",
        "schedule_vs_opening_window",
        "user_goal_alignment",
    ]
    status: Literal["passed", "warning", "blocked", "unknown"]
    summary: str
    requires_structure_change: bool = False
    suggested_action: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_model_field_variants(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if not str(normalized.get("summary") or "").strip():
            for key in ("reason", "rationale", "evidence", "recommendation"):
                candidate = normalized.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    normalized["summary"] = candidate.strip()
                    break
        if not str(normalized.get("suggested_action") or "").strip():
            recommendation = normalized.get("recommendation")
            if isinstance(recommendation, str) and recommendation.strip():
                normalized["suggested_action"] = recommendation.strip()
        return normalized


class PoiOperationalEvent(BaseModel):
    entity: str
    event_type: Literal["closure", "reopening", "restriction", "other"]
    event_date: str = ""
    publication_date: str = ""
    source_type: Literal["official", "government", "verified_web", "ugc", "unknown"] = "unknown"
    summary: str = ""
    applies_on_applicable_date: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_source_type(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        raw = str(normalized.get("source_type") or "").strip().casefold()
        if raw in {"official", "government", "verified_web", "ugc", "unknown"}:
            return normalized
        if any(marker in raw for marker in ("government", "bureau", "gov", "政府", "文旅局")):
            normalized["source_type"] = "government"
        elif any(marker in raw for marker in ("official", "announcement", "公告", "官网", "官方")):
            normalized["source_type"] = "official"
        elif any(marker in raw for marker in ("news", "web", "media", "新闻", "媒体")):
            normalized["source_type"] = "verified_web"
        elif any(marker in raw for marker in ("ugc", "social", "forum", "用户", "论坛")):
            normalized["source_type"] = "ugc"
        else:
            normalized["source_type"] = "unknown"
        return normalized


class PoiFeasibilityVerdict(BaseModel):
    findings: list[PoiFeasibilityFinding] = Field(min_length=1)
    operational_events: list[PoiOperationalEvent] = Field(default_factory=list)


class PoiRetryQueryPlan(BaseModel):
    query: str
    reason: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return datetime.now().astimezone().date()


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _traveler_constraints(requirements: Optional[dict]) -> str:
    notes = str((requirements or {}).get("notes") or "").strip()
    clauses = [item.strip() for item in re.split(r"[\n。；;]+", notes) if item.strip()]
    relevant = [
        clause
        for clause in clauses
        if any(marker in clause for marker in _TRAVELER_RESTRICTION_MARKERS)
    ]
    return "；".join(relevant[:3])[:160]


def _named_poi_goal_text(requirements: Optional[dict], poi_name: str) -> str:
    """Return a positive experience goal that explicitly names this POI.

    Not every style preference becomes a route gate. This only protects a
    concrete, named experience from being silently erased by facts-only
    repairs that are allowed to rewrite route notes.
    """
    requirements = requirements or {}
    names = [poi_name, *_target_name_parts(poi_name)]
    names = [name.strip() for name in names if len(name.strip()) >= 2]
    for source in (requirements.get("style"), requirements.get("notes")):
        for clause in re.split(r"[\n。；;,，]+", str(source or "")):
            clause = clause.strip()
            if not clause or not any(name in clause for name in names):
                continue
            if any(marker in clause for marker in _NEGATED_GOAL_MARKERS):
                continue
            if any(marker in clause for marker in _EXPLICIT_GOAL_MARKERS):
                return clause[:160]
    return ""


def _goal_query_text(goal_text: str, poi_name: str) -> str:
    text = str(goal_text or "")
    for marker in (*_NEGATED_GOAL_MARKERS, *_EXPLICIT_GOAL_MARKERS[:7], "体验", "观看", "欣赏"):
        text = text.replace(marker, " ")
    for name in [poi_name, *_target_name_parts(poi_name)]:
        text = text.replace(name, " ")
    return re.sub(r"\s+", " ", text).strip()[:60]


def _missing_observable_goal_terms(goal_text: str, route_notes: str) -> list[str]:
    return [
        term
        for term in _OBSERVABLE_GOAL_TERMS
        if term in str(goal_text or "") and term not in str(route_notes or "")
    ]


def _date_context(requirements: Optional[dict], today: Optional[date] = None) -> RouteFeasibilityDateContext:
    requirements = requirements or {}
    today = today or _today()
    start_date = str(requirements.get("travel_start_date") or "").strip() or None
    end_date = str(requirements.get("travel_end_date") or "").strip() or None
    source_text = str(requirements.get("travel_date_text") or "").strip()
    precision = str(requirements.get("travel_date_precision") or "").strip()

    if start_date:
        try:
            date.fromisoformat(start_date)
            precision = "exact"
        except ValueError:
            start_date = None
    if precision not in {"exact", "month", "season", "unknown"}:
        if re.search(r"(?:^|\D)(?:1[0-2]|0?[1-9])月", source_text):
            precision = "month"
        elif any(marker in source_text for marker in ("春季", "夏季", "秋季", "冬季", "雨季", "雪季")):
            precision = "season"
        else:
            precision = "unknown"
    if precision == "exact" and not start_date:
        precision = "unknown"

    return RouteFeasibilityDateContext(
        precision=precision,
        start_date=start_date,
        end_date=end_date,
        source_text=source_text,
        query_baseline_date=today.isoformat(),
    )


def _applicable_date(context: RouteFeasibilityDateContext, day_number: int) -> str:
    if context.precision == "exact" and context.start_date:
        return (date.fromisoformat(context.start_date) + timedelta(days=day_number - 1)).isoformat()
    if context.source_text:
        return context.source_text
    return context.query_baseline_date


def _query_year(context: RouteFeasibilityDateContext) -> int:
    if context.start_date:
        return date.fromisoformat(context.start_date).year
    match = re.search(r"(20\d{2})", context.source_text)
    return int(match.group(1)) if match else date.fromisoformat(context.query_baseline_date).year


def _target_name_parts(name: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[/、·－-]+", name) if part.strip()]
    return parts or ([name.strip()] if name.strip() else [])


def _claim_matches_poi(claim: dict, poi_name: str) -> bool:
    normalized_target = _normalize_name(poi_name)
    target_parts = {
        _normalize_name(term)
        for term in _target_name_parts(poi_name)
        if len(_normalize_name(term)) >= 2
    }
    declared_entities = [
        str(value or "")
        for value in [claim.get("subject"), *(claim.get("claimed_entities") or [])]
        if str(value or "").strip()
    ]
    if declared_entities:
        normalized_entities = [_normalize_name(value) for value in declared_entities]
        return any(
            entity and (
                entity == normalized_target
                or entity in target_parts
            )
            for entity in normalized_entities
        )
    claim_text = _normalize_name(str(claim.get("claim_text") or ""))
    if not claim_text:
        return False
    if normalized_target and normalized_target in claim_text:
        return True
    # 两字地域词（如“玉林”）很容易误关联到“玉林小酒馆”等其他实体。
    return any(len(part) >= 3 and part in claim_text for part in target_parts)


def _matched_poi_claims(raw_day: dict, poi: dict) -> list[dict]:
    name = str(poi.get("name") or "").strip()
    structured_claims = [
        claim
        for claim in (raw_day.get("claims") or [])
        if isinstance(claim, dict) and _claim_matches_poi(claim, name)
    ]
    if raw_day.get("claims"):
        return structured_claims

    # 历史路线可能只有brief/notes。只读取明确属于该POI或直接点名该POI的文本，
    # 不根据地点名称后缀猜测它应当具备哪些规则。
    legacy_texts = [
        str(poi.get("brief") or "").strip(),
        str(poi.get("needs_detail_reason") or "").strip(),
    ]
    target_terms = [term for term in [name, *_target_name_parts(name)] if len(term) >= 2]
    for clause in re.split(r"[\n。；;]+", str(raw_day.get("notes") or "")):
        clause = clause.strip()
        if clause and any(term in clause for term in target_terms):
            legacy_texts.append(clause)
    return [
        {"claim_text": text, "claim_type": "legacy_route_text", "claimed_entities": [name]}
        for text in legacy_texts
        if text
    ]


def _poi_categories(claims: list[dict]) -> list[str]:
    categories = {"poi_operational_status"}
    combined_claim_text = " ".join(
        f"{claim.get('claim_type') or ''} {claim.get('claim_text') or ''}"
        for claim in claims
    ).casefold()
    for category, markers in _CLAIM_CATEGORY_MARKERS.items():
        if any(marker.casefold() in combined_claim_text for marker in markers):
            categories.add(category)
    if _TIME_PATTERN.search(combined_claim_text) and categories.intersection(
        {"reservation_or_quota", "opening_window", "last_entry"}
    ):
        categories.add("schedule_vs_opening_window")
    return [category for category in _POI_CATEGORY_ORDER if category in categories]


def _has_explicit_operational_status_claim(claims: list[dict]) -> bool:
    text = " ".join(str(claim.get("claim_text") or "") for claim in claims)
    return any(marker in text for marker in _EXPLICIT_OPERATIONAL_STATUS_MARKERS)


def _commitment_categories(commitment: dict) -> set[str]:
    text = " ".join(
        str(commitment.get(field) or "")
        for field in ("action", "category", "notes")
    ).casefold()
    categories: set[str] = set()
    if any(marker in text for marker in ("预约", "预订", "订票", "抢票", "reserve", "book")):
        categories.add("reservation_or_quota")
    if any(marker in text for marker in ("证件", "身份证", "护照", "资格", "credential")):
        categories.add("entry_qualification")
    if any(marker in text for marker in ("开放", "营业", "入园时间", "入馆时间", "opening")):
        categories.add("opening_window")
    return categories


def _matched_poi_commitments(
    route_execution: Optional[dict],
    day_number: int,
    poi_index: int,
    poi_name: str,
) -> list[dict]:
    matched = []
    normalized_name = _normalize_name(poi_name)
    for commitment in (route_execution or {}).get("operational_commitments", []) or []:
        if not isinstance(commitment, dict):
            continue
        if str(commitment.get("status") or "planned") == "cancelled":
            continue
        target_day = int(commitment.get("target_day") or 0)
        target_index = int(commitment.get("target_poi_index") or 0)
        target_name = _normalize_name(str(commitment.get("target_name") or ""))
        if target_day != day_number:
            continue
        if target_index == poi_index or (target_name and target_name == normalized_name):
            matched.append(commitment)
    return matched


def _coverage_for_category(target: dict, category: str) -> tuple[str, list[str]]:
    commitments = target.get("operational_commitments") or []
    matching_commitments = [
        item for item in commitments if category in _commitment_categories(item)
    ]
    if category == "reservation_or_quota":
        if matching_commitments:
            return "covered", []
        return "missing", ["reservation_action_missing"]
    if category == "user_goal_alignment":
        missing = _missing_observable_goal_terms(
            str(target.get("user_goal_text") or ""),
            str(target.get("notes") or ""),
        )
        return ("missing", ["user_goal_not_scheduled"]) if missing else ("covered", [])
    if matching_commitments:
        return "covered", []
    return "not_applicable", []


def _warning_only_reports_missing_route_measure(summary: str, suggested_action: str) -> bool:
    text = f"{summary} {suggested_action}"
    return any(
        marker in text
        for marker in (
            "路线未安排",
            "路线中未安排",
            "notes未写",
            "notes缺少",
            "尚未安排预约",
            "缺少预约措施",
            "补充预约",
        )
    )


def _query_target_name(name: str) -> str:
    return re.sub(r"[/、·－-]+", " ", name).strip()


def _query_identity_terms(name: str) -> set[str]:
    normalized_target = _normalize_name(name)
    terms = {normalized_target} if len(normalized_target) >= 2 else set()
    terms.update(
        _normalize_name(part)
        for part in _target_name_parts(name)
        if len(_normalize_name(part)) >= 3
    )
    return terms


def _route_entity_kind(provider_type: str) -> str:
    """Map Amap's hierarchical type to the small set needed by this gate."""
    value = str(provider_type or "")
    if any(marker in value for marker in ("火车站", "机场", "长途汽车站", "交通枢纽")):
        return "transport_hub"
    if any(marker in value for marker in ("住宿服务", "宾馆酒店", "旅馆招待所")):
        return "lodging"
    if any(marker in value for marker in ("普通地名", "乡镇级政府", "区县级地名", "地市级地名")):
        return "settlement"
    if any(marker in value for marker in ("风景名胜", "博物馆", "展览馆", "寺庙道观")):
        return "visit_site"
    return "unknown"


def _route_role(raw_day: dict, poi_index: int, entity_kind: str) -> str:
    pois = raw_day.get("pois") or []
    if len(pois) == 1:
        return "single_stop"
    if poi_index == 0:
        return "day_start"
    if poi_index == len(pois) - 1:
        return "overnight_anchor" if entity_kind in {"settlement", "lodging"} else "day_end"
    return "visit_stop"


def _route_check_profile(entity_kind: str, route_role: str) -> str:
    if entity_kind == "visit_site":
        return "visit_site"
    if entity_kind == "transport_hub":
        return "transport_service"
    if entity_kind in {"settlement", "lodging"}:
        return "anchor_claim_only"
    return "unknown_conservative"


def _route_entity_index(route_entities: Optional[dict]) -> dict[tuple[int, int], dict]:
    if not isinstance(route_entities, dict):
        return {}
    return {
        (int(item.get("day") or 0), int(item.get("poi_index") or 0)): item
        for item in route_entities.get("entities") or []
        if isinstance(item, dict) and item.get("status") == "resolved"
    }


def _poi_query_terms(categories: list[str], variant: int, entity_kind: str = "unknown") -> str:
    selected_variant = min(max(variant, 0), 2)
    terms: list[str] = []
    for category in categories or ["poi_operational_status"]:
        if category == "poi_operational_status" and entity_kind == "transport_hub":
            variants = (
                "车站运营 列车服务 进出站 最新公告",
                "车站服务 临时关闭 交通调整 官方",
                "铁路 车站 当前运营 最新消息",
            )
        else:
            variants = _CATEGORY_QUERY_VARIANTS.get(category)
        if not variants:
            continue
        for term in variants[selected_variant].split():
            if term not in terms:
                terms.append(term)
    return " ".join(terms)


def _build_poi_query(
    city: str,
    name: str,
    categories: list[str],
    applicable_date: str,
    traveler_constraints: str = "",
    user_goal_text: str = "",
    entity_kind: str = "unknown",
    *,
    variant: int = 0,
) -> str:
    terms = _poi_query_terms(categories, variant, entity_kind)
    if traveler_constraints and "entry_qualification" in categories:
        terms += f" {traveler_constraints} 进入限制"
    if user_goal_text and "user_goal_alignment" in categories:
        goal_terms = _goal_query_text(user_goal_text, name)
        if goal_terms:
            terms += f" {goal_terms}"
    return f"{city} {_query_target_name(name)} {terms} {applicable_date}".strip()


def build_route_feasibility_targets(
    route_plan: Optional[dict],
    requirements: Optional[dict],
    days: Optional[list[int]] = None,
    route_execution: Optional[dict] = None,
    route_entities: Optional[dict] = None,
) -> tuple[list[dict], RouteFeasibilityDateContext]:
    context = _date_context(requirements)
    target_days = set(days) if days is not None else None
    targets: list[dict] = []
    seen: set[tuple] = set()
    traveler_constraints = _traveler_constraints(requirements)
    has_traveler_restrictions = any(
        marker in traveler_constraints for marker in _TRAVELER_RESTRICTION_MARKERS
    )
    entity_index = _route_entity_index(route_entities)

    for raw_day in (route_plan or {}).get("days", []) or []:
        day_number = int(raw_day.get("day") or 0)
        if target_days is not None and day_number not in target_days:
            continue
        applicable_date = _applicable_date(context, day_number)
        for poi_index, raw_poi in enumerate(raw_day.get("pois", []) or []):
            poi = raw_poi if isinstance(raw_poi, dict) else {"name": str(raw_poi)}
            name = str(poi.get("name") or "").strip()
            if not name:
                continue
            key = ("poi", day_number, _normalize_name(name), applicable_date)
            if key in seen:
                continue
            seen.add(key)
            day_notes = str(raw_day.get("notes") or "")
            matched_claims = _matched_poi_claims(raw_day, poi)
            categories = _poi_categories(matched_claims)
            resolved_entity = entity_index.get((day_number, poi_index)) or {}
            if str(resolved_entity.get("formal_name") or "") != name:
                resolved_entity = {}
            entity_kind = _route_entity_kind(str(resolved_entity.get("type") or ""))
            route_role = _route_role(raw_day, poi_index, entity_kind)
            check_profile = _route_check_profile(entity_kind, route_role)
            matched_commitments = _matched_poi_commitments(
                route_execution,
                day_number,
                poi_index,
                name,
            )
            commitment_categories = {
                category
                for commitment in matched_commitments
                for category in _commitment_categories(commitment)
            }
            categories = [
                category
                for category in _POI_CATEGORY_ORDER
                if category in set(categories).union(commitment_categories)
            ]
            # A city, town, or hotel used only as an itinerary anchor is not a
            # scenic venue. Keep only checks driven by explicit claims or
            # operational commitments instead of asking whether it is "open".
            if (
                check_profile == "anchor_claim_only"
                and not _has_explicit_operational_status_claim(matched_claims)
            ):
                categories = [
                    category for category in categories if category != "poi_operational_status"
                ]
            user_goal_text = _named_poi_goal_text(requirements, name)
            if user_goal_text:
                categories.append("user_goal_alignment")
            if not categories:
                continue
            targets.append(
                {
                    "day": day_number,
                    "target": name,
                    "target_type": "poi",
                    "city": str(raw_day.get("city") or ""),
                    "notes": day_notes,
                    "traveler_constraints": traveler_constraints,
                    "user_goal_text": user_goal_text,
                    "needs_detail": bool(poi.get("needs_detail")),
                    "entity_kind": entity_kind,
                    "route_role": route_role,
                    "check_profile": check_profile,
                    "claim_texts": [
                        str(claim.get("claim_text") or "").strip()
                        for claim in matched_claims
                        if str(claim.get("claim_text") or "").strip()
                    ],
                    "matched_route_claim_ids": [
                        str(claim.get("claim_id") or "")
                        for claim in matched_claims
                        if str(claim.get("claim_id") or "")
                    ],
                    "operational_commitments": matched_commitments,
                    "matched_commitment_ids": [
                        str(item.get("commitment_id") or "")
                        for item in matched_commitments
                        if str(item.get("commitment_id") or "")
                    ],
                    "categories": categories,
                    "applicable_date": applicable_date,
                    "date_precision": context.precision,
                    "date_source_text": context.source_text,
                    "query_baseline_date": context.query_baseline_date,
                    "query": _build_poi_query(
                        str(raw_day.get("city") or ""),
                        name,
                        categories,
                        applicable_date,
                        traveler_constraints if has_traveler_restrictions else "",
                        user_goal_text,
                        entity_kind,
                    ),
                }
            )

        road_ids = timeliness_check._extract_road_identifiers(str(raw_day.get("notes") or ""))
        if road_ids:
            key = ("road", day_number, tuple(road_ids), applicable_date)
            if key not in seen:
                seen.add(key)
                targets.append(
                    {
                        "day": day_number,
                        "target": " ".join(road_ids),
                        "target_type": "road",
                        "city": str(raw_day.get("city") or ""),
                        "notes": str(raw_day.get("notes") or ""),
                        "road_identifiers": road_ids,
                        "categories": ["road_operational_status"],
                        "applicable_date": applicable_date,
                        "date_precision": context.precision,
                        "date_source_text": context.source_text,
                        "query_baseline_date": context.query_baseline_date,
                        "query": timeliness_check._build_web_query(road_ids, year=_query_year(context)),
                    }
                )
    return targets, context


def _web_log_path(project_id: str) -> Path:
    return store.project_dir(project_id) / "agents" / "route_feasibility_check" / "web_search_log.jsonl"


def _target_search_history(project_id: str, target: dict) -> list[dict]:
    path = _web_log_path(project_id)
    if not path.exists():
        return []
    normalized_targets = _query_identity_terms(str(target.get("target") or ""))
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (OSError, ValueError):
            continue
        normalized_query = _normalize_name(_query_target_name(str(entry.get("query") or "")))
        if normalized_query and any(target in normalized_query for target in normalized_targets):
            entries.append(entry)
    return entries


def _retry_query_prompt(
    target: dict,
    previous_checks: list[RouteFeasibilityCheck],
    search_history: list[dict],
) -> str:
    prior_queries = [str(entry.get("query") or "") for entry in search_history]
    prior_results = []
    for entry in search_history[-2:]:
        prior_results.append(
            {
                "query": entry.get("query", ""),
                "results": [
                    {
                        "title": result.get("title", ""),
                        "excerpt": str(result.get("excerpt") or "")[:240],
                    }
                    for result in (entry.get("results") or [])[:5]
                ],
            }
        )
    check_context = [
        {
            "category": check.category,
            "status": check.status,
            "summary": check.summary,
            "suggested_action": check.suggested_action,
            "technical_error": check.technical_error,
        }
        for check in previous_checks
    ]
    date_precision = str(target.get("date_precision") or "unknown")
    query_baseline_date = str(
        target.get("query_baseline_date") or target.get("applicable_date") or ""
    )
    if date_precision == "exact":
        travel_date_context = f"实际旅行日期：{target.get('applicable_date') or ''}"
    elif date_precision in {"month", "season"}:
        travel_date_context = f"旅行时间范围：{target.get('applicable_date') or ''}"
    else:
        travel_date_context = "实际旅行日期：未提供"
    return (
        "请为旅游路线可执行性局部复核生成一条新的Web搜索Query，并以JSON返回query和reason。\n"
        f"城市：{target.get('city') or ''}\n"
        f"原路线目标：{target.get('target') or ''}\n"
        f"地图实体类型：{target.get('entity_kind') or 'unknown'}\n"
        f"路线角色：{target.get('route_role') or 'unknown'}\n"
        f"待复核类别：{target.get('categories') or []}\n"
        f"用户明确体验目标：{target.get('user_goal_text') or '未提供'}\n"
        f"查询最新规则的基准日：{query_baseline_date}\n"
        f"{travel_date_context}\n"
        f"路线原始事实：{target.get('claim_texts') or []}\n"
        f"上轮检查：{json.dumps(check_context, ensure_ascii=False)}\n"
        f"已经使用的Query：{json.dumps(prior_queries, ensure_ascii=False)}\n"
        f"上轮搜索摘要：{json.dumps(prior_results, ensure_ascii=False)}\n\n"
        "只生成一条用于补足缺失证据的Query，不回答事实结论。Query必须保留城市和原目标中的"
        "至少一个明确实体词，不得换成其他景点，不得与已有Query重复，长度不超过160个字符。"
        "优先针对上轮证据错配、来源不足或具体核验类别改写，不要堆砌无关旅游关键词。"
        "交通枢纽应查询车站/机场服务与交通调整，不得套用景区开闭园措辞。"
        "实际旅行日期未提供时，基准日只用于查找截至该日可获得的最新规则，不得把基准日写成"
        "参观日、路线日期或出行当日，也不得查询该日余票、当天名额或星期。"
    )


def _suggest_retry_query(
    target: dict,
    previous_checks: list[RouteFeasibilityCheck],
    search_history: list[dict],
) -> Optional[PoiRetryQueryPlan]:
    if not search_history:
        return None
    model = get_chat_model(
        temperature=0,
        max_tokens=800,
        disable_reasoning=True,
    )
    try:
        return bind_structured_json(model, PoiRetryQueryPlan).invoke(
            _retry_query_prompt(target, previous_checks, search_history)
        )
    except Exception:
        return None


def _valid_retry_query(query: str, target: dict, previous_queries: list[str]) -> bool:
    query = str(query or "").strip()
    if not query or len(query) > 160:
        return False
    normalized_query = _normalize_name(query)
    normalized_city = _normalize_name(str(target.get("city") or ""))
    if normalized_city and normalized_city not in normalized_query:
        return False
    target_terms = _query_identity_terms(str(target.get("target") or ""))
    if target_terms and not any(term in normalized_query for term in target_terms):
        return False
    if str(target.get("date_precision") or "unknown") == "unknown" and any(
        marker in query
        for marker in ("余票", "参观日", "路线日期", "出行日", "旅行日", "当日", "当天")
    ):
        return False
    normalized_previous = {_normalize_name(value) for value in previous_queries if value}
    return normalized_query not in normalized_previous


def _rewrite_scoped_retry_queries(
    project_id: str,
    targets: list[dict],
    previous: Optional[RouteFeasibilityReport] = None,
) -> list[dict]:
    rewritten: list[dict] = []
    for target in targets:
        if target.get("target_type") != "poi":
            rewritten.append(target)
            continue
        search_history = _target_search_history(project_id, target)
        previous_attempts = len(search_history)
        variant = 2 if previous_attempts >= 2 else 1
        previous_checks = [
            check
            for check in ((previous.checks if previous else []) or [])
            if check.day == int(target.get("day") or 0)
            and _normalize_name(check.target) == _normalize_name(str(target.get("target") or ""))
            and check.category in set(target.get("categories") or [])
        ]
        reusable_technical_errors = {
            "judge_error",
            "missing_category",
            "stream_interrupted",
            "provider_unavailable",
            "output_truncated",
        }
        if (
            search_history
            and previous_checks
            and all(check.status == "unknown" for check in previous_checks)
            and all(
                check.technical_error_class in reusable_technical_errors
                for check in previous_checks
            )
        ):
            latest_entry = search_history[-1]
            rewritten.append(
                {
                    **target,
                    "query": str(latest_entry.get("query") or target.get("query") or ""),
                    "query_variant": variant,
                    "query_source": "technical_retry_reuse",
                    "query_reason": "上轮Web证据已成功落盘，仅重新执行结构化判断",
                    "_reuse_web_entry": latest_entry,
                }
            )
            continue
        suggested = _suggest_retry_query(target, previous_checks, search_history)
        previous_queries = [str(entry.get("query") or "") for entry in search_history]
        if suggested and _valid_retry_query(suggested.query, target, previous_queries):
            rewritten.append(
                {
                    **target,
                    "query": suggested.query.strip(),
                    "query_variant": variant,
                    "query_source": "model_retry",
                    "query_reason": suggested.reason,
                }
            )
            continue
        rewritten.append(
            {
                **target,
                "query": _build_poi_query(
                    str(target.get("city") or ""),
                    str(target.get("target") or ""),
                    list(target.get("categories") or ["poi_operational_status"]),
                    str(target.get("applicable_date") or ""),
                    str(target.get("traveler_constraints") or ""),
                    str(target.get("user_goal_text") or ""),
                    str(target.get("entity_kind") or "unknown"),
                    variant=variant,
                ),
                "query_variant": variant,
                "query_source": "program_fallback",
            }
        )
    return rewritten


def _source_type(title: str, url: str) -> str:
    lowered = f"{title} {url}".casefold()
    if ".gov.cn" in lowered or "政府" in title or "交通运输" in title:
        return "government"
    if "amap.com" in lowered or "高德" in title:
        return "map"
    if any(marker in title for marker in ("官网", "官方", "景区公告")):
        return "official"
    return "verified_web"


def _evidence_for_query(project_id: str, query: str, fallback_text: str) -> list[RouteFeasibilityEvidence]:
    entry = find_latest_web_search_entry(_web_log_path(project_id), query)
    if entry:
        return [
            RouteFeasibilityEvidence(
                title=item.get("title", ""),
                url=item.get("url", ""),
                excerpt=item.get("excerpt", ""),
                source_type=_source_type(item.get("title", ""), item.get("url", "")),
                observed_at=entry.get("created_at", ""),
            )
            for item in entry.get("results", [])
        ]
    return [
        RouteFeasibilityEvidence(
            excerpt=fallback_text,
            source_type="unknown",
            observed_at=_now(),
        )
    ] if fallback_text else []


def _check_id(target: dict, category: str) -> str:
    raw = f"{target['target_type']}|{target['day']}|{target['target']}|{category}|{target['applicable_date']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _technical_error_class(reason: str) -> str:
    normalized = str(reason or "").casefold()
    if "模型未返回该检查类别" in reason:
        return "missing_category"
    if "incomplete chunked read" in normalized:
        return "stream_interrupted"
    if "temporarily unavailable" in normalized or "service unavailable" in normalized:
        return "provider_unavailable"
    if any(marker in normalized for marker in ("length limit", "truncated", "finish_reason='length'")):
        return "output_truncated"
    if "web_search" in normalized or "联网搜索" in reason:
        return "web_search_error"
    return "judge_error"


def _judge_execution_metadata(model) -> dict[str, str]:
    group = str(getattr(model, "key_group", "") or "")
    return {
        "judge_provider": "gptload" if group in {"gptload", "deepseek"} else group,
        "judge_model": str(getattr(model, "model_name", "") or ""),
    }


def _unknown_checks(
    target: dict,
    reason: str,
    *,
    technical_error: Optional[str] = None,
    diagnostics: Optional[dict] = None,
    evidence: Optional[list[RouteFeasibilityEvidence]] = None,
) -> list[RouteFeasibilityCheck]:
    diagnostic = dict(diagnostics or {})
    checked_at = _now()
    checks = []
    for category in target["categories"]:
        coverage_status, missing_requirement_codes = _coverage_for_category(target, category)
        checks.append(RouteFeasibilityCheck(
            check_id=_check_id(target, category),
            day=target["day"],
            target=target["target"],
            target_type="schedule" if category == "schedule_vs_opening_window" else target["target_type"],
            category=category,
            status="unknown",
            summary=reason,
            evidence=list(evidence or []),
            checked_at=checked_at,
            applicable_date=target["applicable_date"],
            suggested_action=(
                "自动核验暂未完成，稍后重试或交人工复核"
                if technical_error
                else "出发前通过官方渠道重新核实"
            ),
            technical_error=technical_error,
            technical_error_class=(
                str(diagnostic.get("technical_error_class") or "")
                or (_technical_error_class(technical_error) if technical_error else "")
            ),
            validation_attempt=int(target.get("validation_attempt") or 1),
            judge_provider=str(diagnostic.get("judge_provider") or ""),
            judge_model=str(diagnostic.get("judge_model") or ""),
            requested_categories=list(
                diagnostic.get("requested_categories") or target.get("categories") or []
            ),
            returned_categories=list(diagnostic.get("returned_categories") or []),
            stream_completed=diagnostic.get("stream_completed"),
            entity_kind=str(target.get("entity_kind") or "unknown"),
            route_role=str(target.get("route_role") or "unknown"),
            check_profile=str(target.get("check_profile") or "unknown_conservative"),
            coverage_status=coverage_status,
            matched_route_claim_ids=list(target.get("matched_route_claim_ids") or []),
            matched_commitment_ids=list(target.get("matched_commitment_ids") or []),
            missing_requirement_codes=missing_requirement_codes,
        ))
    return checks


def _is_length_limited_output_error(exc: Exception) -> bool:
    """Recognize only explicit provider/parser truncation signals."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        message = str(current).casefold()
        if any(marker in message for marker in (
            "length limit was reached",
            "finish_reason='length'",
            'finish_reason="length"',
            "finish_reason: length",
            "response was truncated",
        )):
            return True
        current = current.__cause__ or current.__context__
    return False


def _poi_judge_prompt(target: dict, web_result: str) -> str:
    date_precision = str(target.get("date_precision") or "unknown")
    query_baseline_date = str(
        target.get("query_baseline_date") or target.get("applicable_date") or ""
    )
    if date_precision == "exact":
        travel_date_context = f"实际旅行日期：{target.get('applicable_date') or ''}"
    elif date_precision in {"month", "season"}:
        travel_date_context = f"旅行时间范围：{target.get('applicable_date') or ''}"
    else:
        travel_date_context = "实际旅行日期：未提供"
    return (
        f"请核验旅游路线中的一个POI是否可执行，并以JSON格式回复，字段为"
        f"findings数组和operational_events数组。\n"
        f"POI：{target['target']}；城市：{target['city']}；行程第{target['day']}天；"
        f"地图实体类型：{target.get('entity_kind') or 'unknown'}；路线角色：{target.get('route_role') or 'unknown'}；"
        f"日期精度：{date_precision}；查询最新规则的基准日：{query_baseline_date}；"
        f"{travel_date_context}；"
        f"待判断类别：{target['categories']}。\n"
        f"路线中与该目标明确关联的事实：{target.get('claim_texts') or ['未提供额外事实']}\n"
        f"路线执行计划中已登记的跨日/操作承诺：{target.get('operational_commitments') or ['未登记']}\n"
        f"路线notes：{target['notes']}\n"
        f"用户/同行人约束：{target.get('traveler_constraints') or '未提供'}\n\n"
        f"用户明确体验目标：{target.get('user_goal_text') or '未提供'}\n\n"
        f"Web搜索结果：\n{web_result}\n\n"
        "每个待判断类别必须恰好返回一条finding，不得漏项。status只能是passed、warning、"
        "blocked、unknown：有当前且相关的可靠证据支持当前正常运营或与路线时间兼容时判passed；"
        "date_precision=unknown只表示用户尚未给出实际出行日，不能仅因此把已核实的当前事实降为warning，"
        "查询基准日不是旅行日期，不得据此推断用户出行日的星期、季节、节假日、余票或名额，也不得"
        "在summary中称其为参观日或路线日期。只总结搜索证据实际提供的常规规则和特殊日期规则，"
        "没有找到的日期分支不要补造。路线在常规与已知特殊规则下都可执行时可判passed并提醒确认；"
        "只有依赖延时开放、特定季节或其他日期条件才能按原计划执行时判warning。可以把出发前再次确认"
        "写入suggested_action。只有预约、限流、证件、开放窗口、最晚入园等"
        "会影响执行的条件与路线notes冲突，或缺失到可能让用户跑空时才判warning；闭园、实际日期不开放"
        "或核心自然状态已使原安排无法完成时判blocked并设置requires_structure_change=true；"
        "若路线执行计划已登记与该POI匹配的预约、订票或证件准备承诺，不得再以当天notes未重复写该动作"
        "作为warning；应把外部规则与路线内部覆盖情况分别总结。"
        "user_goal_alignment只核对用户明确点名的体验目标：路线notes必须明确写出该体验，并给出可执行的时间、衔接或"
        "条件化方案时判passed，不得要求保证日出、云海等自然现象必然出现；只写了景点游览"
        "却没有真正安排该体验时判warning；已知规则表明无法执行或必须改动已确认POI/天数结构时才判blocked。"
        "来源冲突、"
        "证据不足、目标身份无法确认或无法判断时判unknown。不能把搜索无结果当作passed，也不要仅凭"
        "较早的关闭公告判定当前仍关闭：若搜索结果同时出现关闭与恢复/重开信息，必须按事件发生日期"
        "建立运营状态时间线，以最新一条身份匹配且来源可靠的明确状态为当前结论，并在summary中说明"
        "关闭日期、恢复日期及仍需确认的条件；发布日期不能替代事件发生日期。"
        "operational_events只提取Web结果中明确出现的关闭(closure)、恢复开放"
        "(reopening)或限制(restriction)事件；每条包含entity、event_type、"
        "event_date、publication_date、source_type、summary和applies_on_applicable_date；"
        "closure和restriction事件都要判断applies_on_applicable_date：证据明确事件仍适用于"
        "实际旅行日期或旅行时间范围时为true；明确已结束、尚未开始、不适用于该日期，或同一实体"
        "当前权威开放/票务状态已经覆盖旧事件时为false；证据不足、日期不精确或实际旅行日期未提供"
        "时为null。reopening和other事件填null。不得仅根据公告年龄猜测事件已经失效，也不得让"
        "旧关闭/限制公告覆盖同一实体更新的权威开放或票务状态。source_type只能是"
        "official、government、verified_web、ugc或unknown。日期优先使用YYYY-MM-DD，"
        "原文没有明确日期时留空，不得推测。"
        "价格变化判blocked。每条finding只输出category、"
        "status、summary、requires_structure_change和suggested_action，summary保持简洁。"
    )


def _invoke_poi_verdict(model, prompt: str) -> PoiFeasibilityVerdict:
    return bind_structured_json(model, PoiFeasibilityVerdict).invoke(prompt)


def _judge_poi_target(
    target: dict,
    web_result: str,
    *,
    diagnostics: Optional[dict] = None,
) -> PoiFeasibilityVerdict:
    prompt = _poi_judge_prompt(target, web_result)
    model = get_chat_model(
        temperature=0,
        max_tokens=16000,
        disable_reasoning=True,
    )
    if diagnostics is not None:
        diagnostics.update(
            {
                **_judge_execution_metadata(model),
                "requested_categories": list(target.get("categories") or []),
                "returned_categories": [],
                "stream_completed": None,
            }
        )
    try:
        verdict = _invoke_poi_verdict(model, prompt)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.update(
                {
                    **_judge_execution_metadata(model),
                    "technical_error_class": _technical_error_class(str(exc)),
                    "stream_completed": False
                    if _technical_error_class(str(exc)) == "stream_interrupted"
                    else None,
                }
            )
        # Transport retry cannot see parser failures raised after a successful
        # response. Retry the same DeepSeek structured request once only for an
        # explicit length/truncation signal; ordinary schema bugs stay visible.
        if not _is_length_limited_output_error(exc):
            raise
        model = get_chat_model(temperature=0)
        try:
            verdict = _invoke_poi_verdict(model, prompt)
        except Exception as retry_exc:
            if diagnostics is not None:
                diagnostics.update(
                    {
                        **_judge_execution_metadata(model),
                        "technical_error_class": _technical_error_class(str(retry_exc)),
                        "stream_completed": False
                        if _technical_error_class(str(retry_exc)) == "stream_interrupted"
                        else None,
                    }
                )
            raise
    if diagnostics is not None:
        diagnostics.update(
            {
                **_judge_execution_metadata(model),
                "returned_categories": [item.category for item in verdict.findings],
                "technical_error_class": "",
                "stream_completed": True,
            }
        )
    return verdict


def _parse_event_date(value: str) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(20\d{2})[-/.\u5e74](\d{1,2})[-/.\u6708](\d{1,2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _event_matches_target(event: PoiOperationalEvent, target_name: str) -> bool:
    def compact(value: str) -> str:
        return re.sub(r"[\s（）()\u00b7/]+", "", value or "").casefold()

    event_name = compact(event.entity)
    target = compact(target_name)
    if not event_name or not target:
        return False
    if event_name in target or target in event_name:
        return True
    return any(term and term in event_name for term in _query_identity_terms(target_name))


def _apply_operational_timeline(
    target: dict,
    finding: PoiFeasibilityFinding,
    events: list[PoiOperationalEvent],
) -> PoiFeasibilityFinding:
    """Use dated, target-matched operational events to resolve stale status claims."""
    if finding.category != "poi_operational_status":
        return finding
    trusted = {"official", "government", "verified_web"}
    source_rank = {"verified_web": 1, "government": 2, "official": 3}
    exact_applicable_date = None
    if str(target.get("date_precision") or "unknown") == "exact":
        exact_applicable_date = _parse_event_date(str(target.get("applicable_date") or ""))
    candidates = [
        (parsed, source_rank.get(event.source_type, 0), event)
        for event in events
        if event.source_type in trusted
        if _event_matches_target(event, str(target.get("target") or ""))
        if (parsed := _parse_event_date(event.event_date)) is not None
        if exact_applicable_date is None or parsed <= exact_applicable_date
        if event.event_type in {"closure", "reopening", "restriction"}
    ]
    if not candidates:
        return finding
    candidates.sort(key=lambda item: (item[0], item[1]))
    latest_date, _, latest = candidates[-1]
    dated_events = [item for item in candidates if item[0] <= latest_date]
    closure_dates = [item[0] for item in dated_events if item[2].event_type == "closure"]
    reopening_dates = [item[0] for item in dated_events if item[2].event_type == "reopening"]
    timeline_parts = []
    if closure_dates:
        timeline_parts.append(f"关闭事件：{max(closure_dates).isoformat()}")
    if reopening_dates:
        timeline_parts.append(f"恢复事件：{max(reopening_dates).isoformat()}")
    timeline_summary = "；".join(timeline_parts)
    date_precision = str(target.get("date_precision") or "unknown")
    has_travel_window = bool(target.get("applicable_date")) and date_precision in {
        "exact",
        "month",
        "season",
    }
    explicitly_applies = (
        has_travel_window and latest.applies_on_applicable_date is True
    )

    if latest.event_type == "reopening":
        return finding.model_copy(
            update={
                "status": "passed",
                "summary": (
                    f"{timeline_summary}。同一实体的最新可靠事件为恢复开放。"
                    f"{latest.summary}"
                ).strip(),
                "requires_structure_change": False,
                "suggested_action": "未来出行前仍需通过官方渠道再次确认当日状态",
            }
        )
    if latest.event_type == "closure":
        if not explicitly_applies:
            if finding.status == "passed":
                return finding.model_copy(
                    update={
                        "summary": (
                            f"{timeline_summary}。当前权威开放/票务证据支持正常运营；"
                            f"旧关闭事件不能单独证明旅行时仍关闭。{finding.summary}"
                        ).strip(),
                        "requires_structure_change": False,
                        "suggested_action": "出发前复核同一实体的官方开放与票务状态",
                    }
                )
            return finding.model_copy(
                update={
                    "status": "warning" if finding.status == "warning" else "unknown",
                    "summary": (
                        f"{timeline_summary}。关闭事件是否仍适用于旅行时间尚不确定。"
                        f"{latest.summary}"
                    ).strip(),
                    "requires_structure_change": False,
                    "suggested_action": "出发前复核官方开放与票务状态；当前不自动改路线",
                }
            )
        return finding.model_copy(
            update={
                "status": "blocked",
                "summary": f"{timeline_summary}。同一实体的最新可靠事件为关闭。{latest.summary}".strip(),
                "requires_structure_change": True,
                "suggested_action": "确认恢复开放后再执行，否则生成路线结构调整提案",
            }
        )
    if (
        latest.event_type == "restriction"
        and explicitly_applies
        and finding.status == "passed"
    ):
        return finding.model_copy(
            update={
                "status": "warning",
                "summary": f"最新可靠事件为{latest_date.isoformat()}的运营限制。{latest.summary}".strip(),
                "suggested_action": "出发前核对限制条件是否影响当日路线",
            }
        )
    if latest.event_type == "restriction" and not explicitly_applies:
        if latest.applies_on_applicable_date is False and finding.status == "passed":
            return finding.model_copy(update={"requires_structure_change": False})
        return finding.model_copy(
            update={
                "status": "warning" if finding.status in {"passed", "warning"} else "unknown",
                "summary": (
                    f"最新可靠事件为{latest_date.isoformat()}的运营限制，但是否适用于旅行时间"
                    f"尚不确定。{latest.summary}"
                ).strip(),
                "requires_structure_change": False,
                "suggested_action": "出发前复核限制条件；当前不自动改路线",
            }
        )
    return finding


def _run_poi_target(project_id: str, target: dict, web_search_tool) -> list[RouteFeasibilityCheck]:
    requested_categories = list(target.get("categories") or [])
    reused_entry = target.get("_reuse_web_entry")
    if isinstance(reused_entry, dict):
        web_result = render_web_search_log_entry(reused_entry)
    else:
        try:
            web_result = web_search_tool.invoke({"query": target["query"]})
        except Exception as exc:
            reason = f"web_search失败：{exc}"
            return _unknown_checks(
                target,
                reason,
                technical_error=reason,
                diagnostics={
                    "requested_categories": requested_categories,
                    "technical_error_class": "web_search_error",
                },
            )
    if not web_result or any(marker in str(web_result) for marker in _SEARCH_FAILURE_MARKERS):
        reason = str(web_result or "web_search没有返回结果")
        technical_error = reason if any(
            marker in reason
            for marker in (
                "联网搜索未配置",
                "联网搜索请求失败",
                "联网搜索所有已配置的key都不可用",
            )
        ) else None
        return _unknown_checks(
            target,
            reason,
            technical_error=technical_error,
            diagnostics={
                "requested_categories": requested_categories,
                "technical_error_class": "web_search_error" if technical_error else "",
            },
        )

    evidence = _evidence_for_query(project_id, target["query"], str(web_result))
    judge_diagnostics: dict = {"requested_categories": requested_categories}
    try:
        verdict = _judge_poi_target(
            target,
            str(web_result),
            diagnostics=judge_diagnostics,
        )
    except Exception as exc:
        reason = f"可执行性判断失败：{exc}"
        return _unknown_checks(
            target,
            reason,
            technical_error=reason,
            diagnostics=judge_diagnostics,
            evidence=evidence,
        )

    by_category = {item.category: item for item in verdict.findings if item.category in _POI_CATEGORIES}
    checks: list[RouteFeasibilityCheck] = []
    checked_at = _now()
    for category in target["categories"]:
        finding = by_category.get(category)
        if finding is None:
            reason = "模型未返回该检查类别"
            checks.extend(
                _unknown_checks(
                    {**target, "categories": [category]},
                    reason,
                    technical_error=reason,
                    diagnostics={
                        **judge_diagnostics,
                        "technical_error_class": "missing_category",
                        "requested_categories": requested_categories,
                    },
                    evidence=evidence,
                )
            )
            continue
        finding = _apply_operational_timeline(target, finding, verdict.operational_events)
        status = finding.status
        summary = finding.summary
        suggested_action = finding.suggested_action
        coverage_status, missing_requirement_codes = _coverage_for_category(target, category)
        if (
            category == "reservation_or_quota"
            and coverage_status == "covered"
            and status == "warning"
            and _warning_only_reports_missing_route_measure(summary, suggested_action)
        ):
            status = "passed"
            summary = "路线执行计划已登记预约措施；" + summary
            suggested_action = "按已登记承诺完成预约，并在出发前复核官方规则"
        if category == "user_goal_alignment":
            missing_goal_terms = _missing_observable_goal_terms(
                str(target.get("user_goal_text") or ""),
                str(target.get("notes") or ""),
            )
            if missing_goal_terms and status == "passed":
                status = "warning"
                summary = (
                    "用户明确体验目标尚未在路线notes中落地："
                    + "、".join(missing_goal_terms)
                )
                suggested_action = "补充能实际尝试该体验的时间、住宿/交通衔接和天气备选"
        requires_change = finding.requires_structure_change or status == "blocked"
        checks.append(
            RouteFeasibilityCheck(
                check_id=_check_id(target, category),
                day=target["day"],
                target=target["target"],
                target_type="schedule" if category == "schedule_vs_opening_window" else "poi",
                category=category,
                status=status,
                summary=summary,
                evidence=evidence,
                checked_at=checked_at,
                applicable_date=target["applicable_date"],
                requires_structure_change=requires_change,
                suggested_action=suggested_action,
                validation_attempt=int(target.get("validation_attempt") or 1),
                judge_provider=str(judge_diagnostics.get("judge_provider") or ""),
                judge_model=str(judge_diagnostics.get("judge_model") or ""),
                requested_categories=requested_categories,
                returned_categories=list(judge_diagnostics.get("returned_categories") or []),
                stream_completed=judge_diagnostics.get("stream_completed"),
                entity_kind=str(target.get("entity_kind") or "unknown"),
                route_role=str(target.get("route_role") or "unknown"),
                check_profile=str(target.get("check_profile") or "unknown_conservative"),
                coverage_status=coverage_status,
                matched_route_claim_ids=list(target.get("matched_route_claim_ids") or []),
                matched_commitment_ids=list(target.get("matched_commitment_ids") or []),
                missing_requirement_codes=missing_requirement_codes,
            )
        )
    return checks


def _run_road_target(project_id: str, target: dict, web_search_tool) -> list[RouteFeasibilityCheck]:
    try:
        web_result = web_search_tool.invoke({"query": target["query"]})
    except Exception as exc:
        reason = f"web_search失败：{exc}"
        return _unknown_checks(target, reason, technical_error=reason)
    if not web_result or any(marker in str(web_result) for marker in _SEARCH_FAILURE_MARKERS):
        reason = str(web_result or "web_search没有返回结果")
        technical_error = reason if any(
            marker in reason
            for marker in (
                "联网搜索未配置",
                "联网搜索请求失败",
                "联网搜索所有已配置的key都不可用",
            )
        ) else None
        return _unknown_checks(target, reason, technical_error=technical_error)
    try:
        verdict = timeliness_check._judge_timeliness(target["notes"], str(web_result))
    except Exception as exc:
        reason = f"道路时效判断失败：{exc}"
        return _unknown_checks(target, reason, technical_error=reason)

    evidence = _evidence_for_query(project_id, target["query"], str(web_result))
    if verdict is None:
        reason = "道路时效判断没有返回结果"
        return _unknown_checks(target, reason, technical_error=reason)
    if verdict.requires_structure_change:
        status = "blocked"
        summary = verdict.structure_change_reason or verdict.reason
        suggested_action = "生成路线结构调整提案"
    elif verdict.missing_in_notes:
        status = "warning"
        summary = f"notes缺少：{'；'.join(verdict.missing_in_notes)}。{verdict.reason}"
        suggested_action = "补充道路状态、绕行和来源"
    else:
        status = "passed"
        summary = verdict.reason
        suggested_action = ""
        if target.get("date_precision") == "unknown" and not re.search(
            r"出发前|行前|出行前|临行前|再次核实|再次确认",
            target.get("notes", ""),
        ):
            status = "warning"
            summary = "未提供出行日期；当前道路结论只能作为查询基准日状态"
            suggested_action = "在notes中增加出发前复核道路状态的提醒"
    return [
        RouteFeasibilityCheck(
            check_id=_check_id(target, "road_operational_status"),
            day=target["day"],
            target=target["target"],
            target_type="road",
            category="road_operational_status",
            status=status,
            summary=summary,
            evidence=evidence,
            checked_at=_now(),
            applicable_date=target["applicable_date"],
            requires_structure_change=verdict.requires_structure_change,
            suggested_action=suggested_action,
        )
    ]


def _overall_status(checks: list[RouteFeasibilityCheck]) -> str:
    statuses = {check.status for check in checks}
    for status in ("blocked", "unknown", "warning", "passed"):
        if status in statuses:
            return status
    return "unknown"


def _feasibility_target_key(day: int, target: str, target_type: str) -> str:
    kind = "road" if target_type == "road" else "poi"
    digest = hashlib.sha256(_normalize_name(target).encode("utf-8")).hexdigest()[:12]
    return f"route_feasibility:day:{day}:{kind}:{digest}"


def _evidence_result(check: RouteFeasibilityCheck) -> str:
    if check.status == "passed":
        return "pass"
    if check.status == "blocked":
        return "fail"
    if check.status == "unknown" and check.technical_error:
        return "error"
    return "inconclusive"


def _persist_feasibility_evidence_checks(
    project_id: str,
    report: RouteFeasibilityReport,
    active_targets: list[dict],
    *,
    scoped: bool,
) -> None:
    target_metadata = {
        _feasibility_target_key(
            int(target.get("day") or 0),
            str(target.get("target") or ""),
            str(target.get("target_type") or "poi"),
        ): target
        for target in active_targets
    }
    if scoped and not target_metadata:
        return
    checks = []
    for check in report.checks:
        target_key = _feasibility_target_key(check.day, check.target, check.target_type)
        if scoped and target_key not in target_metadata:
            continue
        metadata = target_metadata.get(target_key, {})
        checks.append(
            {
                "target_type": "route_feasibility",
                "target_key": target_key,
                "target_text": check.target,
                "scope": check.category,
                "result": _evidence_result(check),
                "method": "web_search_and_structured_judge",
                "reason": check.summary,
                "evidence": [item.model_dump(mode="json") for item in check.evidence],
                "checked_at": check.checked_at,
                "day": check.day,
                "category": check.category,
                "status": check.status,
                "applicable_date": check.applicable_date,
                "suggested_action": check.suggested_action,
                "technical_error": check.technical_error,
                "technical_error_class": check.technical_error_class,
                "validation_attempt": check.validation_attempt,
                "judge_provider": check.judge_provider,
                "judge_model": check.judge_model,
                "requested_categories": check.requested_categories,
                "returned_categories": check.returned_categories,
                "stream_completed": check.stream_completed,
                "entity_kind": check.entity_kind,
                "route_role": check.route_role,
                "check_profile": check.check_profile,
                "coverage_status": check.coverage_status,
                "matched_route_claim_ids": check.matched_route_claim_ids,
                "matched_commitment_ids": check.matched_commitment_ids,
                "missing_requirement_codes": check.missing_requirement_codes,
                "query": metadata.get("query", ""),
                "query_source": metadata.get("query_source", "program_initial"),
                "query_reason": metadata.get("query_reason", ""),
            }
        )
    replace_prefixes = (
        sorted(target_metadata)
        if scoped
        else ["route_feasibility:"]
    )
    store.write_evidence_checks(
        project_id,
        "route",
        checks,
        replace_prefixes=replace_prefixes,
    )


def _structure_hash(route_plan: Optional[dict]) -> str:
    snapshot = route_structure_snapshot(route_plan)
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _select_target_checks(targets: list[dict], check_ids: Optional[list[str]]) -> list[dict]:
    """将复核范围收窄到未解决check_id；同一POI的多个类别仍共用一次Web搜索。"""
    if check_ids is None:
        return targets
    requested = set(check_ids)
    selected: list[dict] = []
    for target in targets:
        categories = [
            category
            for category in target["categories"]
            if _check_id(target, category) in requested
        ]
        if categories:
            selected.append({**target, "categories": categories})
    return selected


def _merge_scoped_checks(
    previous: Optional[RouteFeasibilityReport],
    new_checks: list[RouteFeasibilityCheck],
    structure_hash: str,
    context: RouteFeasibilityDateContext,
    days: Optional[list[int]],
    check_ids: Optional[list[str]],
) -> tuple[list[RouteFeasibilityCheck], bool]:
    """局部复核只替换它负责的检查，其他天/类别的已有报告保留。"""
    scoped = days is not None or check_ids is not None
    same_baseline = bool(
        previous
        and previous.route_plan_structure_hash == structure_hash
        and previous.requirements_date_context.model_dump(mode="json")
        == context.model_dump(mode="json")
    )
    if not scoped or not same_baseline:
        return new_checks, False

    if check_ids is not None:
        replaced_ids = set(check_ids)
        returned_ids = {check.check_id for check in new_checks}
        # facts_only修复不能靠删掉notes中的道路/钟点让目标“消失”后冒充通过。
        preserved = [
            check
            for check in previous.checks
            if check.check_id not in replaced_ids or check.check_id not in returned_ids
        ]
    else:
        scoped_days = set(days or [])
        preserved = [check for check in previous.checks if check.day not in scoped_days]

    merged_by_id = {check.check_id: check for check in preserved}
    merged_by_id.update({check.check_id: check for check in new_checks})
    merged = sorted(
        merged_by_id.values(),
        key=lambda check: (check.day, check.target, check.category, check.check_id),
    )
    force_finalized = bool(
        previous.force_finalized and any(check.status != "passed" for check in preserved)
    )
    return merged, force_finalized


def verify_route_feasibility(
    project_id: str,
    topic: str,
    requirements: Optional[dict] = None,
    days: Optional[list[int]] = None,
    check_ids: Optional[list[str]] = None,
    validation_attempt: int = 1,
    persist: bool = True,
) -> RouteFeasibilityReport:
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    route_execution = store.read_workspace(project_id, "route_execution.json") or {}
    route_entities = store.read_workspace(project_id, "route_entities.json") or {}
    structure_hash = _structure_hash(route_plan)
    if str(route_entities.get("route_plan_structure_hash") or "") != structure_hash:
        route_entities = {}
    previous = store.read_route_feasibility(project_id)
    targets, context = build_route_feasibility_targets(
        route_plan,
        requirements,
        days=days,
        route_execution=route_execution,
        route_entities=route_entities,
    )
    targets = _select_target_checks(targets, check_ids)
    if check_ids is not None and validation_attempt == 1 and previous:
        requested_ids = set(check_ids)
        prior_attempts = [
            check.validation_attempt
            for check in previous.checks
            if check.check_id in requested_ids
        ]
        if prior_attempts:
            validation_attempt = max(prior_attempts) + 1
    targets = [
        {**target, "validation_attempt": max(1, int(validation_attempt))}
        for target in targets
    ]
    if check_ids is not None:
        targets = _rewrite_scoped_retry_queries(project_id, targets, previous=previous)
    checks: list[RouteFeasibilityCheck] = []
    if targets:
        web_search_tool = make_web_search_tools(project_id, "route_feasibility_check")[0]
        for target in targets:
            if target["target_type"] == "poi":
                checks.extend(_run_poi_target(project_id, target, web_search_tool))
            elif target["target_type"] == "road":
                checks.extend(_run_road_target(project_id, target, web_search_tool))

    checks, force_finalized = _merge_scoped_checks(
        previous, checks, structure_hash, context, days, check_ids
    )

    report = RouteFeasibilityReport(
        route_plan_updated_at=str(route_plan.get("updated_at") or ""),
        route_plan_structure_hash=structure_hash,
        requirements_date_context=context,
        overall_status=_overall_status(checks),
        checks=checks,
        force_finalized=force_finalized,
        created_at=_now(),
    )
    if persist:
        saved = store.write_route_feasibility(project_id, "citation_checker", report)
        _persist_feasibility_evidence_checks(
            project_id,
            saved,
            targets,
            scoped=days is not None or check_ids is not None,
        )
        return saved
    return report
