"""Cost-capped real-Web smoke runner for the route feasibility service."""
from __future__ import annotations

import argparse
import json
from typing import Optional

from src.materials import route_feasibility_check as feasibility
from src.workspace import store


def _parse_days(raw: Optional[str]) -> Optional[list[int]]:
    if not raw:
        return None
    days = sorted({int(item.strip()) for item in raw.split(",") if item.strip()})
    if not days or any(day < 1 for day in days):
        raise ValueError("--days must contain positive integers")
    return days


def build_smoke_plan(
    project_id: str,
    days: Optional[list[int]] = None,
    max_targets: int = 3,
    target_names: Optional[list[str]] = None,
) -> dict:
    if max_targets < 1:
        raise ValueError("max_targets must be positive")
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    saved = store.read_requirements(project_id) or {}
    requirements = saved.get("requirements") or saved
    targets, context = feasibility.build_route_feasibility_targets(
        route_plan, requirements, days=days
    )
    requested_names = {
        feasibility._normalize_name(name) for name in (target_names or []) if name.strip()
    }
    if requested_names:
        targets = [
            target
            for target in targets
            if feasibility._normalize_name(target["target"]) in requested_names
        ]
    selected = targets[:max_targets]
    check_ids = [
        feasibility._check_id(target, category)
        for target in selected
        for category in target["categories"]
    ]
    return {
        "project_id": project_id,
        "topic": saved.get("topic") or "",
        "days": days,
        "date_context": context.model_dump(mode="json"),
        "available_target_count": len(targets),
        "selected_target_count": len(selected),
        "estimated_web_searches": len(selected),
        "estimated_judgments": len(selected),
        "check_ids": check_ids,
        "targets": selected,
        "requirements": requirements,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview a small route-feasibility smoke run; add --execute to make real calls."
    )
    parser.add_argument("project_id")
    parser.add_argument("--topic", default="", help="Defaults to workspace/requirements.json topic")
    parser.add_argument("--days", help="Comma-separated route days, for example 1,2")
    parser.add_argument("--target", action="append", default=[], help="Exact target name; repeatable")
    parser.add_argument("--max-targets", type=int, default=3)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Make real Web/model calls. Without this flag the command is preview-only.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        days = _parse_days(args.days)
        plan = build_smoke_plan(
            args.project_id,
            days=days,
            max_targets=args.max_targets,
            target_names=args.target,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    preview = {
        key: value
        for key, value in plan.items()
        if key not in {"requirements", "targets"}
    }
    preview["targets"] = [
        {
            "day": target["day"],
            "target": target["target"],
            "target_type": target["target_type"],
            "categories": target["categories"],
            "applicable_date": target["applicable_date"],
            "query": target["query"],
        }
        for target in plan["targets"]
    ]
    preview["mode"] = "execute" if args.execute else "preview"
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    if not plan["targets"]:
        print(json.dumps({"error": "no feasibility targets selected"}, ensure_ascii=False))
        return 2

    topic = args.topic or plan["topic"]
    if not topic:
        print(json.dumps({"error": "topic is required via --topic or requirements.json"}, ensure_ascii=False))
        return 2
    report = feasibility.verify_route_feasibility(
        args.project_id,
        topic,
        requirements=plan["requirements"],
        days=days,
        check_ids=plan["check_ids"],
        persist=False,
    )
    selected_ids = set(plan["check_ids"])
    smoke_checks = [check for check in report.checks if check.check_id in selected_ids]
    result = {
        "schema_version": report.schema_version,
        "overall_status": feasibility._overall_status(smoke_checks),
        "checks": [check.model_dump(mode="json") for check in smoke_checks],
        "persisted": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
