"""Backfill evidence/checker explanations for existing projects.

The default mode is deterministic: it reads saved outputs, sources, chunks and
runtime evidence logs, but does not call an LLM. Checks that require alias or
substantive-support judgment are recorded honestly as ``not_checked``.

Use ``--with-model`` only when a current-state re-evaluation with model calls is
intended. Backfilled records describe the current material state; they are not
presented as the exact historical checker run.
"""
from __future__ import annotations

import argparse
import json
from typing import Optional

from src.materials import citation_check
from src.workspace import store


def _project_topic(project_id: str) -> str:
    saved = store.read_requirements(project_id) or {}
    requirements = saved.get("requirements") or {}
    topic = saved.get("topic") or requirements.get("destination") or requirements.get("topic")
    if not topic:
        raise ValueError(f"project {project_id!r} has no topic in requirements.json")
    return str(topic)


def backfill_project(project_id: str, *, with_model: bool = False) -> dict:
    if project_id not in store.list_project_ids():
        raise FileNotFoundError(f"project {project_id!r} does not exist")

    topic = _project_topic(project_id)
    mode = "backfill_current_state"
    errors: list[dict] = []

    def run(section: str, callback) -> None:
        try:
            callback()
        except Exception as exc:  # Continue so one malformed output does not block the project.
            errors.append({"section": section, "error": f"{type(exc).__name__}: {exc}"})

    common = {"allow_model_calls": with_model, "observation_mode": mode}
    run("route", lambda: citation_check.verify_route_citations(project_id, topic, **common))
    run("lifestyle", lambda: citation_check.verify_lifestyle_citations(project_id, topic, **common))
    run("pitfall", lambda: citation_check.verify_pitfall_citations(project_id, topic, **common))

    poi_details = []
    try:
        poi_details = store.list_poi_details(project_id)
    except Exception as exc:
        errors.append({"section": "poi_detail", "error": f"{type(exc).__name__}: {exc}"})

    for detail in poi_details:
        detail_data = detail.model_dump(mode="json")
        log_scope = f"poi_detail_agent/day{detail.day}_{store.poi_slug(detail.poi_name)}"
        run(
            f"poi_detail:{detail.poi_name}",
            lambda data=detail_data, scope=log_scope: citation_check.verify_poi_detail_citations(
                data,
                topic,
                project_id=project_id,
                log_scope=scope,
                **common,
            ),
        )

    saved = store.read_evidence_checks(project_id)
    return {
        "project_id": project_id,
        "topic": topic,
        "mode": "with_model" if with_model else "deterministic",
        "counts": {
            section: len(saved.get(section) or [])
            for section in ("route", "lifestyle", "pitfall", "poi_detail")
        },
        "errors": errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill evidence_checks.json from existing project artifacts."
    )
    parser.add_argument("project_ids", nargs="*", help="One or more existing project IDs")
    parser.add_argument("--all", action="store_true", help="Backfill every active project")
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="Also run alias/substantive LLM judgments; may incur cost and use current evidence state.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.all and args.project_ids:
        raise SystemExit("use either --all or explicit project IDs, not both")
    project_ids = store.list_project_ids() if args.all else list(dict.fromkeys(args.project_ids))
    if not project_ids:
        raise SystemExit("provide at least one project ID or use --all")

    results = []
    for project_id in project_ids:
        try:
            results.append(backfill_project(project_id, with_model=args.with_model))
        except ValueError as exc:
            if args.all:
                results.append(
                    {
                        "project_id": project_id,
                        "mode": "with_model" if args.with_model else "deterministic",
                        "counts": {},
                        "errors": [],
                        "skipped": str(exc),
                    }
                )
                continue
            results.append(
                {
                    "project_id": project_id,
                    "mode": "with_model" if args.with_model else "deterministic",
                    "counts": {},
                    "errors": [{"section": "project", "error": f"ValueError: {exc}"}],
                }
            )
        except Exception as exc:
            results.append(
                {
                    "project_id": project_id,
                    "mode": "with_model" if args.with_model else "deterministic",
                    "counts": {},
                    "errors": [{"section": "project", "error": f"{type(exc).__name__}: {exc}"}],
                }
            )

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(result["errors"] for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
