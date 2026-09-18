"""Structured entity resolution and deterministic Amap route anomaly checks."""
from __future__ import annotations

import re
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from src.tools import driving, geocode
from src.telemetry.provider_calls import record_provider_call
from src.workspace import store
from src.workspace.route_validation import route_structure_snapshot

_REFERENCE_RE = re.compile(r"用户参考[：:]\s*(\d+(?:\.\d+)?)\s*km、\s*(\d+(?:\.\d+)?)\s*h", re.I)
_ALIASES = {
    "省博": "甘肃省博物馆",
    "军马场": "山丹军马场",
    "翡翠湖": "大柴旦翡翠湖旅游景区",
    "茶卡": "茶卡盐湖景区",
    "七彩丹霞": "张掖七彩丹霞旅游景区",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_score(original_name: str, query_name: str, city_hint: str, item: dict) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    provider_name = str(item.get("provider_name") or "")
    admin = " ".join(
        str(item.get(field) or "") for field in ("province", "city", "district", "address")
    )
    if provider_name == query_name:
        score += 60
        reasons.append("provider_name_exact")
    elif query_name and (query_name in provider_name or provider_name in query_name):
        score += 40
        reasons.append("provider_name_contains")
    if original_name != query_name and original_name in provider_name:
        score += 10
        reasons.append("original_name_present")
    city_tokens = [token for token in re.split(r"[→/、,，·\s]+", city_hint) if len(token) >= 2]
    if any(token in admin for token in city_tokens):
        score += 25
        reasons.append("admin_hint_match")
    if item.get("provider_id"):
        score += 5
        reasons.append("provider_id_present")
    return score, reasons


def _admin_hint_tokens(city_hint: str) -> list[str]:
    return [
        token
        for token in re.split(r"[→/、,，·\s]+", city_hint)
        if len(token) >= 2
    ]


def _candidate_matches_admin(city_hint: str, item: dict) -> bool:
    tokens = _admin_hint_tokens(city_hint)
    if not tokens:
        return True
    admin = " ".join(
        str(item.get(field) or "") for field in ("province", "city", "district", "address")
    )
    return any(token in admin for token in tokens)


def resolve_entity(original_name: str, city_hint: str) -> dict[str, Any]:
    """Resolve a route label while preserving it separately from provider parameters."""
    if "/" in original_name or "／" in original_name:
        return {
            "original_name": original_name,
            "query_name": original_name,
            "status": "requires_human_confirmation",
            "confidence": 0.0,
            "selection_reason": ["composite_user_label_not_auto_collapsed"],
            "candidates": [],
            "selected": None,
        }
    query_name = _ALIASES.get(original_name, original_name)
    candidates = geocode.search_poi_candidates(query_name, city=city_hint)
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        score, reasons = _candidate_score(original_name, query_name, city_hint, candidate)
        admin_match = _candidate_matches_admin(city_hint, candidate)
        ranked.append(
            {
                **candidate,
                "score": score,
                "score_reasons": reasons,
                "rejected_reason": "" if admin_match else "admin_scope_mismatch",
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    eligible = [item for item in ranked if not item.get("rejected_reason")]
    selected = eligible[0] if eligible else None
    runner_up = eligible[1]["score"] if len(eligible) > 1 else -1
    if selected is None:
        status, confidence = "unresolved", 0.0
    elif selected["score"] < 45 or selected["score"] - runner_up < 10:
        status, confidence = "ambiguous", min(selected["score"] / 100, 0.69)
    else:
        status, confidence = "resolved", min(selected["score"] / 100, 0.99)
    return {
        "original_name": original_name,
        "query_name": query_name,
        "city_hint": city_hint,
        "status": status,
        "confidence": round(confidence, 2),
        "selection_reason": (
            (selected or {}).get("score_reasons", [])
            if selected
            else (["all_candidates_outside_admin_scope"] if ranked else [])
        ),
        "candidates": ranked,
        "selected": selected if status == "resolved" else None,
    }


def _route_structure_hash(route_plan: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            route_structure_snapshot(route_plan),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _saved_entity(entity: dict) -> dict[str, Any]:
    location = entity.get("location") or {}
    selected = {
        "provider_id": str(entity.get("provider_id") or ""),
        "provider_name": str(entity.get("provider_name") or entity.get("formal_name") or ""),
        "type": str(entity.get("type") or ""),
        "address": str(entity.get("address") or ""),
        "province": str(entity.get("province") or ""),
        "city": str(entity.get("city") or ""),
        "district": str(entity.get("district") or ""),
        "lng": location.get("lng"),
        "lat": location.get("lat"),
        "score": round(float(entity.get("confidence") or 0) * 100),
        "score_reasons": list(entity.get("selection_reason") or []),
    }
    candidates = []
    for candidate in entity.get("candidates") or []:
        item = dict(candidate)
        candidate_location = item.pop("location", None) or {}
        item["lng"] = candidate_location.get("lng")
        item["lat"] = candidate_location.get("lat")
        candidates.append(item)
    return {
        "original_name": str(entity.get("formal_name") or ""),
        "query_name": str(entity.get("provider_query") or entity.get("formal_name") or ""),
        "city_hint": "",
        "status": str(entity.get("status") or "unresolved"),
        "confidence": float(entity.get("confidence") or 0),
        "selection_reason": list(entity.get("selection_reason") or []),
        "candidates": candidates,
        "selected": selected if entity.get("status") == "resolved" else None,
        "resolution_source": "route_entities",
    }


def _entity_audit(entity: dict[str, Any]) -> dict[str, Any]:
    selected = entity.get("selected") or {}
    return {
        "original_name": str(entity.get("original_name") or ""),
        "query_name": str(entity.get("query_name") or ""),
        "resolution_source": str(entity.get("resolution_source") or "provider_search"),
        "provider_id": str(selected.get("provider_id") or ""),
        "provider_name": str(selected.get("provider_name") or ""),
        "province": str(selected.get("province") or ""),
        "city": str(selected.get("city") or ""),
        "district": str(selected.get("district") or ""),
        "lng": selected.get("lng"),
        "lat": selected.get("lat"),
    }


def _straight_line_km(a: dict, b: dict) -> float | None:
    try:
        lng1, lat1 = math.radians(float(a["lng"])), math.radians(float(a["lat"]))
        lng2, lat2 = math.radians(float(b["lng"])), math.radians(float(b["lat"]))
    except (KeyError, TypeError, ValueError):
        return None
    dlng = lng2 - lng1
    dlat = lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return round(6371.0088 * 2 * math.asin(math.sqrt(value)), 1)


def _declared_duration_h(leg: dict) -> float | None:
    duration = leg.get("duration_minutes")
    if isinstance(duration, (int, float)) and duration >= 0:
        return round(float(duration) / 60, 2)
    segments = leg.get("segments") or []
    durations = [item.get("duration_minutes") for item in segments if isinstance(item, dict)]
    if durations and all(isinstance(value, (int, float)) and value >= 0 for value in durations):
        return round(sum(float(value) for value in durations) / 60, 2)
    return None


def _user_reference(notes: str) -> tuple[float, float] | None:
    match = _REFERENCE_RE.search(notes or "")
    return (float(match.group(1)), float(match.group(2))) if match else None


def verify_route_map(project_id: str, topic: str, route_plan: dict, *, persist: bool = True) -> dict:
    """Resolve all route points, measure adjacent legs, and flag implausible results."""
    entity_cache: dict[tuple[str, str], dict] = {}
    days: list[dict] = []
    issues: list[dict] = []
    route_hash = _route_structure_hash(route_plan)
    saved_entities_raw = store.read_workspace(project_id, "route_entities.json") or {}
    saved_entity_index: dict[tuple[int, int], dict] = {}
    if isinstance(saved_entities_raw, dict):
        saved_hash = str(saved_entities_raw.get("route_plan_structure_hash") or "")
        if saved_hash and saved_hash != route_hash:
            issues.append(
                {
                    "code": "route_entities_stale",
                    "message": "已保存的路线实体对应旧路线结构，本次已回退为重新消歧",
                }
            )
        else:
            saved_entity_index = {
                (int(item.get("day") or 0), int(item.get("poi_index") or 0)): item
                for item in saved_entities_raw.get("entities") or []
                if isinstance(item, dict)
            }
    execution_raw = store.read_workspace(project_id, "route_execution.json") or {}
    execution_hash = str(execution_raw.get("route_plan_structure_hash") or "") if isinstance(execution_raw, dict) else ""
    execution_valid = not execution_hash or execution_hash == route_hash
    if execution_hash and not execution_valid:
        issues.append(
            {
                "code": "route_execution_stale",
                "message": "已保存的路线执行计划对应旧路线结构，本次未采用其中交通方式",
            }
        )
    leg_index = {
        (
            int(item.get("day") or 0),
            int(item.get("from_poi_index") or 0),
            int(item.get("to_poi_index") or 0),
        ): item
        for item in ((execution_raw.get("route_legs") or []) if isinstance(execution_raw, dict) and execution_valid else [])
        if isinstance(item, dict)
    }
    for day in route_plan.get("days") or []:
        day_no = day.get("day")
        city_hint = str(day.get("city") or "").strip()
        entities: list[dict] = []
        for poi_index, poi in enumerate(day.get("pois") or []):
            original_name = str(poi.get("name") or "")
            saved = saved_entity_index.get((int(day_no or 0), poi_index))
            if saved and str(saved.get("formal_name") or "") == original_name and saved.get("status") == "resolved":
                entity = _saved_entity(saved)
            elif saved and saved.get("status") == "requires_human_confirmation":
                entity = _saved_entity(saved)
            else:
                key = (original_name, city_hint)
                entity = entity_cache.setdefault(key, resolve_entity(original_name, city_hint))
                entity["resolution_source"] = "map_checker_search"
                record_provider_call(project_id, provider="amap", operation="place_search", status="success" if entity.get("candidates") else "empty", log_scope="route_agent", metadata={"day": day_no, "poi_name": original_name, "candidate_count": len(entity.get("candidates") or [])}, projects_dir=store.PROJECTS_DIR)
            entities.append(entity)
            if entity["status"] != "resolved":
                issues.append(
                    {
                        "code": f"route_entity_{entity['status']}",
                        "day": day_no,
                        "poi_name": original_name,
                        "message": "地图实体未达到自动采用阈值，需消歧或人工确认",
                    }
                )

        legs: list[dict] = []
        for from_index, (origin, destination) in enumerate(zip(entities, entities[1:])):
            to_index = from_index + 1
            declared_leg = leg_index.get((int(day_no or 0), from_index, to_index)) or {}
            transport_mode = str(declared_leg.get("transport_mode") or "driving")
            mode_source = "route_execution" if declared_leg else "legacy_default"
            if not origin.get("selected") or not destination.get("selected"):
                legs.append(
                    {
                        "from": origin["original_name"],
                        "to": destination["original_name"],
                        "status": "not_measured_unresolved_entity",
                        "transport_mode": transport_mode,
                        "mode_source": mode_source,
                    }
                )
                continue
            a, b = origin["selected"], destination["selected"]
            if transport_mode == "unknown":
                legs.append(
                    {
                        "from": origin["original_name"],
                        "to": destination["original_name"],
                        "transport_mode": transport_mode,
                        "mode_source": mode_source,
                        "status": "unknown_transport_mode",
                    }
                )
                issues.append(
                    {
                        "code": "route_map_transport_mode_unknown",
                        "day": day_no,
                        "message": f"{origin['original_name']}到{destination['original_name']}缺少交通方式",
                    }
                )
                continue
            if transport_mode not in {"driving", "taxi"}:
                if transport_mode == "mixed" and not declared_leg.get("segments"):
                    issues.append(
                        {
                            "code": "route_map_mixed_leg_missing_segments",
                            "day": day_no,
                            "message": f"{origin['original_name']}到{destination['original_name']}的mixed路段未拆分",
                        }
                    )
                segment_results: list[dict] = []
                if transport_mode == "mixed":
                    for segment_index, segment in enumerate(declared_leg.get("segments") or []):
                        segment_mode = str(segment.get("mode") or "unknown")
                        segment_entities: list[dict[str, Any]] = []
                        segment_distance = segment.get("distance_km")
                        segment_duration_h = (
                            round(float(segment["duration_minutes"]) / 60, 2)
                            if isinstance(segment.get("duration_minutes"), (int, float))
                            else None
                        )
                        segment_status = "declared_mode_specific"
                        if segment_mode in {"driving", "taxi"}:
                            for segment_name in (segment.get("from_name"), segment.get("to_name")):
                                if segment_name == origin["original_name"]:
                                    segment_entity = origin
                                elif segment_name == destination["original_name"]:
                                    segment_entity = destination
                                else:
                                    segment_entity = resolve_entity(str(segment_name or ""), city_hint)
                                    record_provider_call(
                                        project_id,
                                        provider="amap",
                                        operation="place_search",
                                        status="success" if segment_entity.get("candidates") else "empty",
                                        log_scope="route_agent",
                                        metadata={
                                            "day": day_no,
                                            "segment_index": segment_index,
                                            "poi_name": segment_name,
                                        },
                                        projects_dir=store.PROJECTS_DIR,
                                    )
                                segment_entities.append(segment_entity)
                            if all(item.get("selected") for item in segment_entities):
                                segment_a, segment_b = [item["selected"] for item in segment_entities]
                                driving_diagnostic: dict[str, str] = {}
                                segment_measured = driving.driving_distance(
                                    segment_a["lng"],
                                    segment_a["lat"],
                                    segment_b["lng"],
                                    segment_b["lat"],
                                    diagnostic=driving_diagnostic,
                                )
                                record_provider_call(
                                    project_id,
                                    provider="amap",
                                    operation="driving",
                                    status="success" if segment_measured is not None else "error",
                                    log_scope="route_agent",
                                    metadata={
                                        "day": day_no,
                                        "segment_index": segment_index,
                                        "transport_mode": segment_mode,
                                        "from_entity": _entity_audit(segment_entities[0]),
                                        "to_entity": _entity_audit(segment_entities[1]),
                                        "error_code": driving_diagnostic.get("code", ""),
                                        "error_detail": driving_diagnostic.get("detail", ""),
                                    },
                                    projects_dir=store.PROJECTS_DIR,
                                )
                                if segment_measured is not None:
                                    segment_distance = round(segment_measured[0] / 1000, 1)
                                    segment_duration_h = round(segment_measured[1] / 3600, 2)
                                    segment_status = "passed"
                                else:
                                    segment_status = "provider_error"
                            else:
                                segment_status = "unknown_unresolved_entity"
                        elif segment_mode == "unknown" or segment_duration_h is None:
                            segment_status = "unknown"
                        if segment_status in {"unknown", "unknown_unresolved_entity", "provider_error"}:
                            issues.append(
                                {
                                    "code": "route_map_mixed_segment_unknown",
                                    "day": day_no,
                                    "segment_index": segment_index,
                                    "message": (
                                        f"{segment.get('from_name')}到{segment.get('to_name')}"
                                        "的mixed子段缺少可采用的交通核验结果"
                                    ),
                                }
                            )
                        segment_results.append(
                            {
                                **segment,
                                "distance_km": segment_distance,
                                "duration_h": segment_duration_h,
                                "status": segment_status,
                                **(
                                    {"provider_error": driving_diagnostic}
                                    if segment_status == "provider_error" and driving_diagnostic
                                    else {}
                                ),
                                "from_entity": _entity_audit(segment_entities[0])
                                if len(segment_entities) == 2
                                else {},
                                "to_entity": _entity_audit(segment_entities[1])
                                if len(segment_entities) == 2
                                else {},
                            }
                        )
                distance_km = declared_leg.get("distance_km")
                if not isinstance(distance_km, (int, float)) and segment_results:
                    values = [item.get("distance_km") for item in segment_results]
                    if values and all(isinstance(value, (int, float)) for value in values):
                        distance_km = round(sum(float(value) for value in values), 1)
                if not isinstance(distance_km, (int, float)):
                    distance_km = _straight_line_km(a, b)
                duration_h = _declared_duration_h(declared_leg)
                anomalies = []
                if isinstance(distance_km, (int, float)) and distance_km > 800:
                    anomalies.append("spatial_separation_over_800km")
                    issues.append(
                        {
                            "code": "route_map_spatial_separation_over_800km",
                            "day": day_no,
                            "message": f"{origin['original_name']}到{destination['original_name']}空间距离异常",
                        }
                    )
                mixed_unknown = transport_mode == "mixed" and (
                    not segment_results
                    or any(
                        item.get("status")
                        in {"unknown", "unknown_unresolved_entity", "provider_error"}
                        for item in segment_results
                    )
                )
                legs.append(
                    {
                        "from": origin["original_name"],
                        "to": destination["original_name"],
                        "provider_from": a["provider_name"],
                        "provider_to": b["provider_name"],
                        "distance_km": round(float(distance_km), 1) if isinstance(distance_km, (int, float)) else None,
                        "duration_h": duration_h,
                        "transport_mode": transport_mode,
                        "mode_source": mode_source,
                        "status": (
                            "anomaly"
                            if anomalies
                            else ("unknown" if mixed_unknown else "mode_specific_not_driving")
                        ),
                        "anomalies": anomalies,
                        "segments": segment_results or declared_leg.get("segments") or [],
                    }
                )
                continue
            driving_diagnostic = {}
            measured = driving.driving_distance(
                a["lng"], a["lat"], b["lng"], b["lat"], diagnostic=driving_diagnostic
            )
            record_provider_call(
                project_id,
                provider="amap",
                operation="driving",
                status="success" if measured is not None else "error",
                log_scope="route_agent",
                metadata={
                    "day": day_no,
                    "from": origin["original_name"],
                    "to": destination["original_name"],
                    "transport_mode": transport_mode,
                    "from_entity": _entity_audit(origin),
                    "to_entity": _entity_audit(destination),
                    "error_code": driving_diagnostic.get("code", ""),
                    "error_detail": driving_diagnostic.get("detail", ""),
                },
                projects_dir=store.PROJECTS_DIR,
            )
            if measured is None:
                legs.append(
                    {
                        "from": origin["original_name"],
                        "to": destination["original_name"],
                        "transport_mode": transport_mode,
                        "mode_source": mode_source,
                        "status": "provider_error",
                        "provider_error": driving_diagnostic,
                        "from_entity": _entity_audit(origin),
                        "to_entity": _entity_audit(destination),
                    }
                )
                issues.append({"code": "route_map_leg_unavailable", "day": day_no, "message": f"无法测量{origin['original_name']}到{destination['original_name']}"})
                continue
            distance_m, duration_s = measured
            distance_km = round(distance_m / 1000, 1)
            duration_h = round(duration_s / 3600, 2)
            average_speed = round(distance_km / duration_h, 1) if duration_h else 0
            anomalies: list[str] = []
            if distance_km > 800:
                anomalies.append("adjacent_leg_over_800km")
            if average_speed > 120:
                anomalies.append("average_speed_over_120kmh")
            if distance_km > 20 and average_speed < 10:
                anomalies.append("average_speed_under_10kmh")
            for anomaly in anomalies:
                issues.append({"code": f"route_map_{anomaly}", "day": day_no, "message": f"{origin['original_name']}到{destination['original_name']}地图结果异常"})
            legs.append(
                {
                    "from": origin["original_name"],
                    "to": destination["original_name"],
                    "provider_from": a["provider_name"],
                    "provider_to": b["provider_name"],
                    "from_entity": _entity_audit(origin),
                    "to_entity": _entity_audit(destination),
                    "distance_km": distance_km,
                    "duration_h": duration_h,
                    "average_speed_kmh": average_speed,
                    "transport_mode": transport_mode,
                    "mode_source": mode_source,
                    "status": "anomaly" if anomalies else "passed",
                    "anomalies": anomalies,
                }
            )

        total_km = round(sum(float(item.get("distance_km") or 0) for item in legs), 1)
        total_h = round(sum(float(item.get("duration_h") or 0) for item in legs), 2)
        reference = _user_reference(str(day.get("notes") or ""))
        reference_check: dict[str, Any] | None = None
        if reference and total_km > 0:
            km_delta = round((total_km - reference[0]) / reference[0], 3)
            h_delta = round((total_h - reference[1]) / reference[1], 3)
            reference_check = {"user_distance_km": reference[0], "user_duration_h": reference[1], "distance_delta_ratio": km_delta, "duration_delta_ratio": h_delta}
            if abs(km_delta) > 0.35 or abs(h_delta) > 0.35:
                issues.append({"code": "route_map_user_reference_deviation", "day": day_no, "message": "高德聚合结果与用户参考值偏差超过35%"})
        attraction_count = sum(1 for poi in day.get("pois") or [] if poi.get("needs_detail"))
        estimated_activity_h = attraction_count * 1.5
        estimated_break_h = max(0.5, total_h * 0.12) if total_h else 0
        estimated_day_h = round(total_h + estimated_activity_h + estimated_break_h, 2)
        has_unmeasured_leg = any(
            item.get("duration_h") is None
            or item.get("status")
            in {"unknown", "unknown_transport_mode", "not_measured_unresolved_entity", "provider_error"}
            for item in legs
        )
        schedule_status = "unknown" if has_unmeasured_leg else ("warning" if estimated_day_h > 12 else "passed")
        if schedule_status == "unknown":
            issues.append({"code": "route_schedule_incomplete", "day": day_no, "message": "存在未测量路段，不能给出完整日程预算"})
        if schedule_status == "warning":
            issues.append({"code": "route_schedule_over_12h", "day": day_no, "message": "驾驶、基础游览与休息的保守日程估算超过12小时"})
        days.append(
            {
                "day": day_no,
                "entities": entities,
                "legs": legs,
                "totals": {"distance_km": total_km, "duration_h": total_h},
                "user_reference_check": reference_check,
                "schedule_check": {"status": schedule_status, "estimated_day_h": None if has_unmeasured_leg else estimated_day_h, "assumptions": {"hours_per_needs_detail_poi": 1.5, "break_ratio_of_driving": 0.12, "minimum_break_h": 0.5}},
            }
        )
    report = {
        "schema_version": "route-map-validation-v1",
        "project_id": project_id,
        "route_plan_structure_hash": route_hash,
        "provider": "高德Web服务API",
        "checked_at": _now(),
        "status": "needs_confirmation" if issues else "passed",
        "days": days,
        "issues": issues,
    }
    if persist:
        store.write_route_map_validation(project_id, report)
    return report
