"""高德地图工具。amap_query是MCP接口的stub（未配置AMAP_MCP_API_KEY时返回提示文字，
让agent能优雅降级，而不是报错中断）；amap_driving_distance是真实可用的驾车距离工具，
复用AMAP_GEOCODE_API_KEY（跟src/tools/geocode.py同一个key）+ src/tools/driving.py的REST封装。

两种key的分工见src/config.py的注释：AMAP_MCP_API_KEY是MCP服务接入key（至今未申请，
amap_query一直是stub）；AMAP_GEOCODE_API_KEY是Web服务REST接口key（已配置可用，
geocode.py用它做地理编码，driving.py用它做驾车路线规划）。两者访问的是同一套高德数据，
功能基本等价，REST接入更直接不需要处理MCP协议。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import tool

from src.config import AMAP_GEOCODE_API_KEY, AMAP_MCP_API_KEY, PROJECTS_DIR
from src.tools import driving
from src.telemetry.provider_calls import record_provider_call
from src.workspace import store
from src.workspace.route_validation import route_structure_hash


def reset_amap_evidence_log(project_id: str, log_scope: str = "route_agent") -> None:
    """Start a fresh project-scoped Amap evidence ledger for one route attempt."""
    (PROJECTS_DIR / project_id / "agents" / log_scope / "amap_evidence_log.jsonl").unlink(
        missing_ok=True
    )


def _append_amap_evidence(
    project_id: str,
    log_scope: str,
    *,
    evidence_id: str,
    topic: str,
    poi_a: str,
    poi_b: str,
    city: str,
    distance_km: float,
    duration_h: float,
    source: str = "live_query",
    route_map_day: int | None = None,
) -> None:
    path = PROJECTS_DIR / project_id / "agents" / log_scope / "amap_evidence_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": "amap-evidence-v1",
        "evidence_id": evidence_id,
        "topic": topic,
        "poi_a": poi_a,
        "poi_b": poi_b,
        "city": city,
        "distance_km": distance_km,
        "duration_h": duration_h,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "provider": "高德Web服务API",
        "source": source,
        "route_map_day": route_map_day,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _normalized_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _route_entity_aliases(entity: dict[str, Any]) -> set[str]:
    selected = entity.get("selected") if isinstance(entity.get("selected"), dict) else {}
    return {
        name
        for name in (
            _normalized_name(entity.get("original_name")),
            _normalized_name(entity.get("query_name")),
            _normalized_name(selected.get("provider_name")),
        )
        if name
    }


def _route_entity_coordinates(entity: dict[str, Any]) -> tuple[float, float] | None:
    selected = entity.get("selected") if isinstance(entity.get("selected"), dict) else {}
    try:
        return float(selected["lng"]), float(selected["lat"])
    except (KeyError, TypeError, ValueError):
        return None


def _route_names(route_plan: dict[str, Any], day: int | None) -> set[str]:
    names: set[str] = set()
    for route_day in route_plan.get("days") or []:
        if not isinstance(route_day, dict):
            continue
        if day is not None and route_day.get("day") != day:
            continue
        for poi in route_day.get("pois") or []:
            if isinstance(poi, dict) and _normalized_name(poi.get("name")):
                names.add(_normalized_name(poi.get("name")))
    return names


def _route_map_distance_source(
    project_id: str | None,
    poi_a: str,
    poi_b: str,
    day: int | None,
) -> dict[str, Any]:
    """Resolve route anchors against the current Route Map without fuzzy guessing."""
    if not project_id:
        return {"status": "not_applicable"}
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    report = store.read_workspace(project_id, "route_map_validation.json") or {}
    normalized_inputs = {_normalized_name(poi_a), _normalized_name(poi_b)}
    current_route_names = _route_names(route_plan, day)
    report_aliases = {
        alias
        for route_day in (report.get("days") or [])
        if isinstance(route_day, dict)
        for entity in (route_day.get("entities") or [])
        if isinstance(entity, dict)
        for alias in _route_entity_aliases(entity)
    }
    requests_route_anchor = bool(normalized_inputs & (current_route_names | report_aliases))
    if not report:
        return {"status": "route_map_missing" if requests_route_anchor else "not_applicable"}
    if str(report.get("route_plan_structure_hash") or "") != route_structure_hash(route_plan):
        return {"status": "route_map_stale" if requests_route_anchor else "not_applicable"}

    candidate_days = [
        item
        for item in (report.get("days") or [])
        if isinstance(item, dict) and (day is None or item.get("day") == day)
    ]
    if day is not None and not candidate_days:
        return {"status": "route_map_day_missing" if requests_route_anchor else "not_applicable"}

    def matches_in_days(
        name: str, days: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        normalized = _normalized_name(name)
        return [
            (route_day, entity)
            for route_day in days
            for entity in (route_day.get("entities") or [])
            if isinstance(entity, dict) and normalized in _route_entity_aliases(entity)
        ]

    matches_a = matches_in_days(poi_a, candidate_days)
    matches_b = matches_in_days(poi_b, candidate_days)
    if len(matches_a) > 1 or len(matches_b) > 1:
        return {"status": "route_entity_ambiguous"}
    if day is not None:
        all_days = [item for item in (report.get("days") or []) if isinstance(item, dict)]
        if (
            (not matches_a and matches_in_days(poi_a, all_days))
            or (not matches_b and matches_in_days(poi_b, all_days))
        ):
            return {"status": "route_entity_day_mismatch"}
    if not matches_a and not matches_b:
        return {
            "status": "route_entity_day_mismatch"
            if day is not None and requests_route_anchor
            else "not_applicable"
        }

    entity_a = matches_a[0] if matches_a else None
    entity_b = matches_b[0] if matches_b else None
    if entity_a and entity_b:
        day_a, raw_a = entity_a
        day_b, raw_b = entity_b
        original_a = str(raw_a.get("original_name") or "")
        original_b = str(raw_b.get("original_name") or "")
        if day_a.get("day") == day_b.get("day"):
            matching_leg = next(
                (
                    leg
                    for leg in (day_a.get("legs") or [])
                    if isinstance(leg, dict)
                    and {str(leg.get("from") or ""), str(leg.get("to") or "")}
                    == {original_a, original_b}
                ),
                None,
            )
            if matching_leg is not None:
                if (
                    matching_leg.get("transport_mode") in {"driving", "taxi"}
                    and matching_leg.get("status") == "passed"
                    and isinstance(matching_leg.get("distance_km"), (int, float))
                    and isinstance(matching_leg.get("duration_h"), (int, float))
                ):
                    return {
                        "status": "persisted_leg",
                        "day": int(day_a.get("day") or 0),
                        "distance_km": float(matching_leg["distance_km"]),
                        "duration_h": float(matching_leg["duration_h"]),
                    }
                return {"status": "route_map_leg_unavailable"}

        coordinates_a = _route_entity_coordinates(raw_a)
        coordinates_b = _route_entity_coordinates(raw_b)
        if coordinates_a is None or coordinates_b is None:
            return {"status": "route_entity_unresolved"}
        return {
            "status": "canonical_coordinates",
            "origin": coordinates_a,
            "destination": coordinates_b,
        }

    route_side, free_name, route_is_origin = (
        (entity_a, poi_b, True) if entity_a else (entity_b, poi_a, False)
    )
    coordinates = _route_entity_coordinates(route_side[1]) if route_side else None
    if coordinates is None:
        return {"status": "route_entity_unresolved"}
    return {
        "status": "one_canonical_coordinate",
        "route_coordinate": coordinates,
        "free_name": free_name,
        "route_is_origin": route_is_origin,
    }


def _format_metric(value: float) -> str:
    return f"{value:g}"


def make_amap_tools(
    topic: str = "",
    project_id: str | None = None,
    log_scope: str = "route_agent",
    *,
    include_entity_resolution: bool = False,
    research_context: dict | None = None,
) -> list:
    """构建高德地图工具列表。

    topic进闭包绑定（不暴露给agent），用于driving缓存key——跟geocode缓存key
    f"{topic}|{poi_name}"一致。realtime_agent等不需要核距离的调用方可以不传topic
    （默认空字符串，缓存key退化为"|a|b"，跟geocode空topic同款，功能不损）。
    """
    def _record_research(
        *, query: str, status: str, result_count: int = 0, metadata: dict | None = None
    ) -> None:
        if not project_id or not research_context:
            return
        from src.materials.research_service import record_external_research

        record_external_research(
            project_id,
            actor=str(research_context.get("actor") or "unknown_agent"),
            stage=str(research_context.get("stage") or "unknown"),
            target_key=str(research_context.get("target_key") or log_scope),
            query=query,
            source_kind="amap",
            status=status,
            result_count=result_count,
            metadata={"log_scope": log_scope, **(metadata or {})},
        )

    @tool
    def amap_query(query: str) -> str:
        """查询高德地图数据（POI真实性/开放时间），用于校验景点是否真实存在、开放时间是否匹配。
        注：当前为MCP stub，未配置AMAP_MCP_API_KEY时返回提示文字，需基于素材自行判断。"""
        if not AMAP_MCP_API_KEY:
            if project_id:
                record_provider_call(project_id, provider="amap", operation="mcp_query", status="unavailable", log_scope=log_scope, metadata={"reason": "mcp_key_missing"}, projects_dir=PROJECTS_DIR)
            _record_research(
                query=query,
                status="unavailable",
                metadata={"reason": "mcp_key_missing"},
            )
            return "高德MCP未配置，跳过结构化校验，请基于素材内容自行判断POI真实性/开放时间等信息的合理性。"
        # TODO: 真实接入高德MCP
        if project_id:
            record_provider_call(project_id, provider="amap", operation="mcp_query", status="not_implemented", log_scope=log_scope, projects_dir=PROJECTS_DIR)
        _record_research(query=query, status="unavailable", metadata={"reason": "not_implemented"})
        return "高德MCP未实现"

    @tool
    def amap_resolve_route_entity(poi_name: str, city_hint: str) -> str:
        """Resolve one formal route label to structured Amap candidates.

        Returns the preserved formal name, provider query, ranked candidates,
        administrative rejection reasons, selected provider ID/coordinates,
        confidence, and resolution status for route_entities.json.
        """
        if not AMAP_GEOCODE_API_KEY:
            if project_id:
                record_provider_call(
                    project_id,
                    provider="amap",
                    operation="place_search",
                    status="unavailable",
                    log_scope=log_scope,
                    metadata={"reason": "geocode_key_missing", "poi_name": poi_name},
                    projects_dir=PROJECTS_DIR,
                )
            _record_research(
                query=f"{poi_name} {city_hint}",
                status="unavailable",
                metadata={"reason": "geocode_key_missing"},
            )
            return json.dumps(
                {
                    "original_name": poi_name,
                    "city_hint": city_hint,
                    "status": "unresolved",
                    "reason": "amap_geocode_key_missing",
                    "candidates": [],
                    "selected": None,
                },
                ensure_ascii=False,
            )
        from src.materials.route_map_check import resolve_entity

        started = time.monotonic()
        result = resolve_entity(poi_name, city_hint)
        if project_id:
            record_provider_call(
                project_id,
                provider="amap",
                operation="place_search",
                status="success" if result.get("candidates") else "empty",
                duration_ms=round((time.monotonic() - started) * 1000),
                log_scope=log_scope,
                metadata={
                    "poi_name": poi_name,
                    "city_hint": city_hint,
                    "resolution_status": result.get("status"),
                    "candidate_count": len(result.get("candidates") or []),
                },
                projects_dir=PROJECTS_DIR,
            )
        _record_research(
            query=f"{poi_name} {city_hint}",
            status="success" if result.get("candidates") else "empty",
            result_count=len(result.get("candidates") or []),
            metadata={"resolution_status": result.get("status")},
        )
        return json.dumps(result, ensure_ascii=False)

    @tool
    def amap_driving_distance(
        poi_a: str,
        poi_b: str,
        city: str = "",
        day: int | None = None,
    ) -> str:
        """查询两个POI之间的驾车距离和预估时长（用于校验单天通勤是否合理，不要凭印象写"370km需3.5小时"，
        先用这个工具查实再写进notes）。poi_a/poi_b是两个地点名称（如"莫高窟""张掖七彩丹霞"），
        city可选用于消歧（如同名景点在多个城市）；查询正式路线点时应传day。当前路线Hash下已有的
        Route Map相邻路段会直接复用，不再重新地理编码。返回"约 X 公里 / Y 小时（高德驾车实测）"或
        未配置key/查不到时的提示文字。"""
        map_source = _route_map_distance_source(project_id, poi_a, poi_b, day)
        source_status = str(map_source.get("status") or "not_applicable")
        result = None
        evidence_source = "live_query"
        route_map_day = None
        started = time.monotonic()

        if source_status == "persisted_leg":
            distance_km = float(map_source["distance_km"])
            duration_h = float(map_source["duration_h"])
            result = (round(distance_km * 1000), round(duration_h * 3600))
            evidence_source = "route_map_validation"
            route_map_day = int(map_source.get("day") or 0)
        elif source_status == "canonical_coordinates":
            result = driving.get_cached_or_driving_coordinates(
                topic,
                poi_a,
                map_source["origin"],
                poi_b,
                map_source["destination"],
            )
            evidence_source = "route_map_entities"
            route_map_day = day
        elif source_status == "one_canonical_coordinate":
            if not AMAP_GEOCODE_API_KEY:
                result = None
            else:
                free_coordinate = driving.get_cached_or_geocode(
                    topic,
                    str(map_source["free_name"]),
                    city=city,
                )
                if free_coordinate is not None:
                    if map_source["route_is_origin"]:
                        origin, destination = map_source["route_coordinate"], free_coordinate
                    else:
                        origin, destination = free_coordinate, map_source["route_coordinate"]
                    result = driving.get_cached_or_driving_coordinates(
                        topic,
                        poi_a,
                        origin,
                        poi_b,
                        destination,
                    )
            evidence_source = "route_map_anchor"
            route_map_day = day
        elif source_status in {
            "route_map_missing",
            "route_map_stale",
            "route_map_day_missing",
            "route_entity_ambiguous",
            "route_entity_day_mismatch",
            "route_entity_unresolved",
            "route_map_leg_unavailable",
        }:
            reason_messages = {
                "route_map_missing": "当前正式路线尚无Route Map结果",
                "route_map_stale": "Route Map对应旧路线结构",
                "route_map_day_missing": "Route Map缺少指定日期",
                "route_entity_ambiguous": "正式路线实体存在多个匹配",
                "route_entity_day_mismatch": "查询地点不属于指定Day的正式路线",
                "route_entity_unresolved": "正式路线实体尚未解析出唯一坐标",
                "route_map_leg_unavailable": "该相邻路段没有可复用的驾车测量",
            }
            _record_research(
                query=f"{poi_a} 到 {poi_b} {city}",
                status="unavailable",
                metadata={"reason": source_status, "day": day},
            )
            return (
                f"{reason_messages[source_status]}，不能绕过Route Map重新按名称猜测坐标。"
                "请先完成路线地图核验或明确消歧；当前内容不要写精确距离。"
            )
        elif not AMAP_GEOCODE_API_KEY:
            if project_id:
                record_provider_call(project_id, provider="amap", operation="driving", status="unavailable", log_scope=log_scope, metadata={"reason": "geocode_key_missing"}, projects_dir=PROJECTS_DIR)
            _record_research(
                query=f"{poi_a} 到 {poi_b} {city}",
                status="unavailable",
                metadata={"reason": "geocode_key_missing"},
            )
            return (
                "高德距离校验未配置（缺少GEOCODE key），无法查询驾车距离，"
                "请基于素材内容自行判断通勤距离，并如实标注是估算值。"
            )
        else:
            result = driving.get_cached_or_driving(topic, poi_a, poi_b, city=city)
        if project_id and evidence_source != "route_map_validation":
            record_provider_call(
                project_id,
                provider="amap",
                operation="driving",
                status="success" if result is not None else "error",
                duration_ms=round((time.monotonic() - started) * 1000),
                log_scope=log_scope,
                metadata={
                    "poi_a": poi_a,
                    "poi_b": poi_b,
                    "city": city,
                    "day": day,
                    "source": evidence_source,
                },
                projects_dir=PROJECTS_DIR,
            )
        if result is None:
            _record_research(
                query=f"{poi_a} 到 {poi_b} {city}",
                status="error",
            )
            return (
                f"无法获取「{poi_a}」或「{poi_b}」的坐标或驾车距离，跳过校验，"
                "请基于素材内容自行判断通勤距离，并如实标注是估算值。"
            )

        distance_m, duration_s = result
        if evidence_source == "live_query":
            distance_km = round(distance_m / 1000)
            duration_h = round(duration_s / 3600, 1)
        else:
            distance_km = round(distance_m / 1000, 1)
            duration_h = round(duration_s / 3600, 2)
        evidence_id = "amap-evidence-" + hashlib.sha1(
            f"{topic}|{poi_a}|{poi_b}|{city}|{distance_km}|{duration_h}".encode("utf-8")
        ).hexdigest()[:16]
        if project_id:
            _append_amap_evidence(
                project_id,
                log_scope,
                evidence_id=evidence_id,
                topic=topic,
                poi_a=poi_a,
                poi_b=poi_b,
                city=city,
                distance_km=distance_km,
                duration_h=duration_h,
                source=evidence_source,
                route_map_day=route_map_day,
            )
        _record_research(
            query=f"{poi_a} 到 {poi_b} {city}",
            status="success",
            result_count=1,
            metadata={
                "evidence_id": evidence_id,
                "source": evidence_source,
                "day": route_map_day,
            },
        )
        return (
            f"约 {_format_metric(distance_km)} 公里 / {_format_metric(duration_h)} 小时"
            f"（高德驾车实测，来源：高德Web服务API，"
            f"可写进notes作为通勤依据，优于凭印象估算）。"
            f"事实引用请使用platform='amap', doc_id='{poi_a}→{poi_b}', "
            f"chunk_id='{evidence_id}'。"
        )

    tools = [amap_query, amap_driving_distance]
    if include_entity_resolution:
        tools.insert(1, amap_resolve_route_entity)
    return tools
