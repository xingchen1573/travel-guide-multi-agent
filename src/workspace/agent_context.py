"""Small, role-specific views over authoritative Workspace data."""
from __future__ import annotations

from typing import Any

from src.workspace.route_validation import route_structure_hash
from src.workspace.schema import LIFESTYLE_FIELDS


def route_day_view(route_plan: dict | None, day: int) -> dict[str, Any]:
    """Return the route structure a day-scoped role actually needs."""
    for route_day in (route_plan or {}).get("days", []) or []:
        if route_day.get("day") != day:
            continue
        return {
            "day": day,
            "city": route_day.get("city", ""),
            "area": route_day.get("area", ""),
            "pois": [
                {
                    "name": poi.get("name", ""),
                    "brief": poi.get("brief", ""),
                    "needs_detail": bool(poi.get("needs_detail")),
                }
                for poi in (route_day.get("pois") or [])
            ],
            "notes": route_day.get("notes", ""),
        }
    return {"day": day, "city": "", "area": "", "pois": [], "notes": ""}


def _route_map_entity_view(entity: dict[str, Any]) -> dict[str, Any]:
    selected = entity.get("selected") if isinstance(entity.get("selected"), dict) else {}
    return {
        "original_name": str(entity.get("original_name") or ""),
        "status": str(entity.get("status") or "unresolved"),
        "provider_id": str(selected.get("provider_id") or ""),
        "provider_name": str(selected.get("provider_name") or ""),
        "province": str(selected.get("province") or ""),
        "city": str(selected.get("city") or ""),
        "district": str(selected.get("district") or ""),
        "lng": selected.get("lng"),
        "lat": selected.get("lat"),
    }


def route_map_day_view(
    route_plan: dict | None,
    route_map_validation: dict | None,
    day: int,
) -> dict[str, Any]:
    """Return one current route day's canonical map facts without raw diagnostics."""
    report = route_map_validation if isinstance(route_map_validation, dict) else {}
    current_hash = route_structure_hash(route_plan)
    report_hash = str(report.get("route_plan_structure_hash") or "")
    base = {
        "day": day,
        "route_plan_structure_hash": report_hash,
        "current_route_plan_structure_hash": current_hash,
    }
    if not report:
        return {**base, "status": "missing", "entities": [], "legs": [], "totals": {}}
    if not report_hash or report_hash != current_hash:
        return {**base, "status": "stale", "entities": [], "legs": [], "totals": {}}

    raw_day = next(
        (
            item
            for item in (report.get("days") or [])
            if isinstance(item, dict) and item.get("day") == day
        ),
        None,
    )
    if raw_day is None:
        return {**base, "status": "missing_day", "entities": [], "legs": [], "totals": {}}

    route_day = next(
        (
            item
            for item in (route_plan or {}).get("days", []) or []
            if isinstance(item, dict) and item.get("day") == day
        ),
        None,
    )
    expected_leg_count = (
        max(0, len(route_day.get("pois") or []) - 1)
        if isinstance(route_day, dict)
        else None
    )
    legs = []
    raw_legs = [leg for leg in raw_day.get("legs") or [] if isinstance(leg, dict)]
    for leg in raw_legs:
        status = str(leg.get("status") or "unknown")
        projected_leg = {
            "from": str(leg.get("from") or ""),
            "to": str(leg.get("to") or ""),
            "provider_from": str(leg.get("provider_from") or ""),
            "provider_to": str(leg.get("provider_to") or ""),
            "transport_mode": str(leg.get("transport_mode") or ""),
            "status": status,
        }
        if status == "passed":
            projected_leg["distance_km"] = leg.get("distance_km")
            projected_leg["duration_h"] = leg.get("duration_h")
        legs.append(projected_leg)

    all_expected_legs_passed = (
        expected_leg_count is not None
        and len(raw_legs) == expected_leg_count
        and all(str(leg.get("status") or "unknown") == "passed" for leg in raw_legs)
    )
    return {
        **base,
        "status": "current",
        "validation_status": str(report.get("status") or "unknown"),
        "entities": [
            _route_map_entity_view(entity)
            for entity in (raw_day.get("entities") or [])
            if isinstance(entity, dict)
        ],
        "legs": legs,
        "totals": (
            {
                "distance_km": (raw_day.get("totals") or {}).get("distance_km"),
                "duration_h": (raw_day.get("totals") or {}).get("duration_h"),
            }
            if all_expected_legs_passed
            else {}
        ),
    }


def user_requirement_view(requirements: dict | None) -> dict[str, Any]:
    """Remove orchestration-only keys while retaining user constraints."""
    return {
        key: value
        for key, value in (requirements or {}).items()
        if not str(key).startswith("_")
    }


def lifestyle_day_complete(lifestyle: dict | None, day: int) -> bool:
    return all(
        any(item.get("day") == day for item in ((lifestyle or {}).get(field) or {}).get("items", []) or [])
        for field in LIFESTYLE_FIELDS
    )


def lifestyle_for_pitfall(lifestyle: dict | None, *, day: int | None = None) -> dict[str, Any]:
    """Pitfall needs recommendation targets, not Lifestyle evidence internals."""
    projected: dict[str, Any] = {}
    for field in LIFESTYLE_FIELDS:
        items = []
        for item in ((lifestyle or {}).get(field) or {}).get("items", []) or []:
            if day is not None and item.get("day") != day:
                continue
            items.append(
                {
                    "day": item.get("day"),
                    "recommendation": item.get("recommendation", ""),
                    "claimed_entities": item.get("claimed_entities", []),
                    "citation_status": item.get("citation_status", "passed"),
                }
            )
        projected[field] = {"items": items}
    return projected
