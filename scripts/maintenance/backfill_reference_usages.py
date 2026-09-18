"""Backfill deterministic reference usage indexes for existing projects.

This migration only rebuilds the program-owned reference appendix and the
``final_guide.meta.json.references`` projection. It preserves the Integration
authored body and the existing POI appendix, and it never calls an LLM.

The generated ``cited_in`` values are current-state declarations reconstructed
from saved ``sources`` fields. They are not presented as historical model
reasoning or sentence-level support judgments.
"""
from __future__ import annotations

import argparse
import json
from typing import Optional

from src.materials import citation_check
from src.workspace import store
from src.workspace.final_guide_sections import (
    compose_final_guide_sections,
    split_final_guide_sections,
)


def _project_topic(project_id: str) -> str:
    saved = store.read_requirements(project_id) or {}
    requirements = saved.get("requirements") or {}
    topic = saved.get("topic") or requirements.get("destination") or requirements.get("topic")
    if not topic:
        raise ValueError(f"project {project_id!r} has no topic in requirements.json")
    return str(topic)


def backfill_project(project_id: str) -> dict:
    if project_id not in store.list_project_ids():
        raise FileNotFoundError(f"project {project_id!r} does not exist")

    current_content = store.read_workspace(project_id, "final_guide.md")
    current_meta = store.read_workspace(project_id, "final_guide.meta.json")
    if not isinstance(current_content, str) or not isinstance(current_meta, dict):
        raise ValueError(f"project {project_id!r} has no complete final guide")

    topic = _project_topic(project_id)
    route_plan = store.read_workspace(project_id, "route_plan.json")
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    poi_details = [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]

    cited_chunks = citation_check.collect_cited_chunks(
        route_plan if isinstance(route_plan, dict) else None,
        lifestyle if isinstance(lifestyle, dict) else None,
        pitfall if isinstance(pitfall, dict) else None,
        poi_details,
    )
    chunk_texts = citation_check.load_chunk_texts(topic, project_id)
    reference_appendix, references = citation_check.build_reference_section(cited_chunks, chunk_texts)

    body, poi_appendix, _old_reference_appendix = split_final_guide_sections(current_content)
    new_content = compose_final_guide_sections(body, poi_appendix, reference_appendix)
    changed = new_content != current_content or current_meta.get("references") != references
    if changed:
        new_meta = dict(current_meta)
        new_meta["references"] = references
        store.write_final_guide(project_id, "integration_agent", new_content, new_meta)
        store.refresh_final_guide_delivery_hash(project_id, actor="review_migration")

    usage_count = sum(len(reference.get("cited_in") or []) for reference in references)
    runtime_reference_count = sum(
        reference.get("platform") in {"web_verified", "amap"} for reference in references
    )
    missing_text_reference_count = sum(
        reference.get("chunk_id") not in chunk_texts
        and reference.get("platform") not in {"web_verified", "amap"}
        for reference in references
    )
    return {
        "project_id": project_id,
        "topic": topic,
        "changed": changed,
        "reference_count": len(references),
        "usage_count": usage_count,
        "runtime_reference_count": runtime_reference_count,
        "missing_text_reference_count": missing_text_reference_count,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill cited_in and excerpts from existing project source declarations."
    )
    parser.add_argument("project_ids", nargs="*", help="One or more existing project IDs")
    parser.add_argument("--all", action="store_true", help="Backfill every active project")
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
            results.append(backfill_project(project_id))
        except ValueError as exc:
            if args.all:
                results.append({"project_id": project_id, "skipped": str(exc)})
                continue
            results.append({"project_id": project_id, "error": f"ValueError: {exc}"})
        except Exception as exc:
            results.append({"project_id": project_id, "error": f"{type(exc).__name__}: {exc}"})

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any("error" in result for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
