"""Pure route-structure contracts shared by Codex and LangGraph."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional


def route_structure_snapshot(route_plan: Optional[dict]) -> dict:
    """Return the user-visible and behavioral structure of a route plan."""
    days = []
    for raw_day in (route_plan or {}).get("days", []) or []:
        pois = []
        for raw_poi in raw_day.get("pois", []) or []:
            if isinstance(raw_poi, dict):
                pois.append(
                    {
                        "name": raw_poi.get("name", ""),
                        "needs_detail": bool(raw_poi.get("needs_detail", False)),
                    }
                )
            else:
                pois.append({"name": str(raw_poi), "needs_detail": False})
        days.append(
            {
                "day": raw_day.get("day"),
                "city": raw_day.get("city", ""),
                "area": raw_day.get("area", ""),
                "pois": pois,
            }
        )
    return {"days": days}


def route_structure_hash(route_plan: Optional[dict]) -> str:
    """Return the stable hash shared by route-dependent artifacts."""
    return hashlib.sha256(
        json.dumps(
            route_structure_snapshot(route_plan),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_route_structure(
    route_plan: Optional[dict],
    expected_days: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Return deterministic structural issues for a route plan."""
    issues: list[dict[str, Any]] = []
    days = (route_plan or {}).get("days")
    if not isinstance(days, list) or not days:
        return [{"code": "days_empty", "message": "路线天数不能为空"}]

    numbers = [day.get("day") if isinstance(day, dict) else None for day in days]
    if expected_days is not None:
        expected = list(range(1, expected_days + 1))
        if len(days) != expected_days:
            issues.append(
                {
                    "code": "day_count_mismatch",
                    "message": f"用户要求{expected_days}天，但生成了{len(days)}天",
                    "expected": expected_days,
                    "actual": len(days),
                }
            )
        if numbers != expected:
            issues.append(
                {
                    "code": "day_sequence_invalid",
                    "message": f"day编号必须严格为{expected}，实际为{numbers}",
                    "expected": expected,
                    "actual": numbers,
                }
            )
    else:
        if any(
            not isinstance(number, int) or isinstance(number, bool) or number < 1
            for number in numbers
        ):
            issues.append(
                {
                    "code": "day_number_invalid",
                    "message": f"day必须是从1开始的正整数，实际为{numbers}",
                    "actual": numbers,
                }
            )
        elif numbers != list(range(1, len(numbers) + 1)):
            issues.append(
                {
                    "code": "day_sequence_invalid",
                    "message": f"day编号必须连续且按顺序排列，实际为{numbers}",
                    "actual": numbers,
                }
            )

    for index, raw_day in enumerate(days, start=1):
        if not isinstance(raw_day, dict):
            issues.append(
                {
                    "code": "day_not_object",
                    "message": f"第{index}项不是有效的天级对象",
                }
            )
            continue
        day_number = raw_day.get("day", index)
        if not str(raw_day.get("city", "")).strip():
            issues.append(
                {
                    "code": "day_city_empty",
                    "day": day_number,
                    "message": f"Day{day_number}缺少city",
                }
            )
        pois = raw_day.get("pois")
        valid_names = []
        if isinstance(pois, list):
            for poi in pois:
                name = poi.get("name", "") if isinstance(poi, dict) else str(poi)
                if str(name).strip():
                    valid_names.append(str(name).strip())
        if not valid_names:
            issues.append(
                {
                    "code": "day_pois_empty",
                    "day": day_number,
                    "message": f"Day{day_number}至少需要一个名称非空的POI",
                }
            )
    return issues


def route_structure_diff(before: Optional[dict], after: Optional[dict]) -> list[dict[str, Any]]:
    """Return a compact day-level diff between two structural snapshots."""
    before_days = route_structure_snapshot(before).get("days") or []
    after_days = route_structure_snapshot(after).get("days") or []
    before_by_day = {day.get("day"): day for day in before_days}
    after_by_day = {day.get("day"): day for day in after_days}
    changed = []
    for day in sorted(
        set(before_by_day) | set(after_by_day),
        key=lambda value: (value is None, str(value)),
    ):
        old = before_by_day.get(day)
        new = after_by_day.get(day)
        if old != new:
            changed.append({"day": day, "before": old, "after": new})
    if [day.get("day") for day in before_days] != [day.get("day") for day in after_days]:
        return [{"day": None, "before": before_days, "after": after_days}]
    return changed


__all__ = [
    "route_structure_diff",
    "route_structure_hash",
    "route_structure_snapshot",
    "validate_route_structure",
]
