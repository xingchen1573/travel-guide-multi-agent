"""Project initialization APIs shared by travel-guide runtimes."""

from __future__ import annotations

from typing import Any, Literal


def prepare_fixed_route_project(
    source_project_id: str,
    target_project_id: str,
    *,
    harness: Literal["langgraph", "codex_skill"],
    model: str | None = None,
    provider: str | None = None,
    comparison_grade: str = "C",
    scenario_id: str | None = None,
    require_comparison_sidecars: bool = False,
    preflight_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare an isolated project from one confirmed fixed-route input."""
    from src.workspace.fixed_route_setup import (
        prepare_fixed_route_project as _prepare_fixed_route_project,
    )

    return _prepare_fixed_route_project(
        source_project_id,
        target_project_id,
        harness=harness,
        model=model,
        provider=provider,
        comparison_grade=comparison_grade,
        scenario_id=scenario_id,
        require_comparison_sidecars=require_comparison_sidecars,
        preflight_receipt=preflight_receipt,
    )


__all__ = ["prepare_fixed_route_project"]
