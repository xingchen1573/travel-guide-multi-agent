"""Run the production LangGraph from a canonical confirmed-route input."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.codex_skill.bridge import route_structure_sha256
from src.llm_rotation import primary_travel_model_identity
from src.orchestrator import (
    prepare_langgraph_fixed_route,
    run_plan,
)
from src.workspace import store


def _prepare(
    source_project: str,
    target_project: str,
    *,
    model: str,
    provider: str,
    comparison_grade: str,
) -> tuple[dict, str]:
    prepared = prepare_langgraph_fixed_route(
        source_project,
        target_project,
        model=model,
        provider=provider,
        comparison_grade=comparison_grade,
        require_comparison_sidecars=comparison_grade == "A",
    )
    saved = store.read_requirements(target_project)
    if not saved or not isinstance(saved.get("requirements"), dict):
        raise ValueError(f"target Project has no valid requirements: {target_project}")
    return dict(saved["requirements"]), str(prepared["route_structure_sha256"])


def _interrupt_payload(result: dict) -> dict:
    raw = (result.get("__interrupt__") or [None])[0]
    payload = getattr(raw, "value", raw)
    return payload if isinstance(payload, dict) else {}


def main() -> None:
    model_identity = primary_travel_model_identity()
    parser = argparse.ArgumentParser()
    parser.add_argument("source_project")
    parser.add_argument("target_project")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an existing target Project from its LangGraph checkpoint",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="create and verify the isolated target without starting run_plan",
    )
    parser.add_argument("--model", default=model_identity["model"])
    parser.add_argument("--provider", default=model_identity["provider"])
    parser.add_argument("--comparison-grade", choices=["A", "B", "C"], default="A")
    args = parser.parse_args()

    if args.resume:
        saved = store.read_requirements(args.target_project)
        route_plan = store.read_workspace(args.target_project, "route_plan.json")
        if not saved or not isinstance(saved.get("requirements"), dict):
            raise ValueError(f"target Project has no saved requirements: {args.target_project}")
        if not isinstance(route_plan, dict):
            raise ValueError(f"target Project has no route_plan: {args.target_project}")
        requirements = dict(saved["requirements"])
        expected_sha = route_structure_sha256(route_plan)
    else:
        requirements, expected_sha = _prepare(
            args.source_project,
            args.target_project,
            model=args.model,
            provider=args.provider,
            comparison_grade=args.comparison_grade,
        )

    if args.prepare_only:
        print(
            json.dumps(
                {
                    "event": "langgraph_fixed_route_prepared",
                    "source_project": args.source_project,
                    "target_project": args.target_project,
                    "route_structure_sha256": expected_sha,
                    "model": args.model,
                    "provider": args.provider,
                    "comparison_grade": args.comparison_grade,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return

    print(
        json.dumps(
            {
                "event": "langgraph_fixed_route_started",
                "source_project": args.source_project,
                "target_project": args.target_project,
                "route_structure_sha256": expected_sha,
                "resume": args.resume,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    result = run_plan(args.target_project, requirements)
    interrupt_payload = _interrupt_payload(result)
    proposal = interrupt_payload.get("route_change_proposal") or {}
    action_required = "__interrupt__" in result
    final_route = store.read_workspace(args.target_project, "route_plan.json")
    final_sha = route_structure_sha256(final_route if isinstance(final_route, dict) else None)
    progress = store.read_progress(args.target_project)
    summary = {
        "event": (
            "langgraph_fixed_route_action_required"
            if action_required
            else "langgraph_fixed_route_finished"
        ),
        "status": "action_required" if action_required else "completed",
        "target_project": args.target_project,
        "progress": progress.state.value,
        "expected_route_structure_sha256": expected_sha,
        "final_route_structure_sha256": final_sha,
        "route_structure_unchanged": final_sha == expected_sha,
        "last_agent_result": result.get("last_agent_result"),
        "interrupted": "__interrupt__" in result,
        "proposal_id": proposal.get("proposal_id") if action_required else None,
        "proposal_reason": proposal.get("reason") if action_required else None,
        "proposal_evidence": proposal.get("evidence") if action_required else None,
        "decision_options": (
            ["keep", "confirm", "revise:<意见>"] if proposal else []
        ),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
