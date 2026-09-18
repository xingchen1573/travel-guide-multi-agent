#!/usr/bin/env python3
"""Build a credential-free travel-guide demo through the production contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.delivery import run_generate_site as site_generator
from src.workspace import store
from src.workspace.final_guide_validation import validate_final_guide_contract
from src.workspace.schema import (
    PoiDetailFile,
    PoiDetailManifest,
    PoiDetailManifestEntry,
    ProjectPhase,
    RoutePlan,
)

DEFAULT_INPUT = Path(__file__).with_name("public_demo_input.json")
DEFAULT_OUTPUT = REPO_ROOT / "build" / "public-demo"
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def _load_input(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "public-deterministic-demo-v1":
        raise ValueError("不支持的公开演示输入版本")
    project_id = str(payload.get("project_id") or "")
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("公开演示project_id必须是安全的小写短横线标识")
    if not str(payload.get("fixed_now") or "").strip():
        raise ValueError("公开演示输入缺少fixed_now")
    return payload


def _delivery_digest(workspace: Path) -> tuple[str, list[str]]:
    files = sorted(
        path
        for path in workspace.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    )
    digest = hashlib.sha256()
    relative_files: list[str] = []
    for path in files:
        relative = path.relative_to(workspace).as_posix()
        relative_files.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), relative_files


def _contract_issues(project_id: str) -> list[dict[str, Any]]:
    requirements_document = store.read_workspace(project_id, "requirements.json") or {}
    requirements = (
        requirements_document.get("requirements", requirements_document)
        if isinstance(requirements_document, dict)
        else {}
    )
    return validate_final_guide_contract(
        requirements,
        store.read_workspace(project_id, "route_plan.json"),
        store.read_workspace(project_id, "route_feasibility.json"),
        store.read_workspace(project_id, "outputs/lifestyle.json"),
        store.read_workspace(project_id, "outputs/pitfall.json"),
        store.read_workspace(project_id, "final_guide.md"),
        store.read_workspace(project_id, "final_guide.meta.json"),
        store.read_workspace(project_id, "final_guide.blocks.json"),
        [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)],
        store.read_evidence_checks(project_id),
        store.read_workspace(project_id, "review_findings.jsonl") or [],
    )


def run_demo(
    *,
    input_path: Path = DEFAULT_INPUT,
    output_root: Path = DEFAULT_OUTPUT,
    force: bool = False,
) -> dict[str, Any]:
    """Replay the fixed sample and return a deterministic delivery report."""
    payload = _load_input(input_path)
    project_id = payload["project_id"]
    output_root = output_root.resolve()
    projects_dir = output_root / "projects"
    project_dir = projects_dir / project_id
    if project_dir.exists():
        if not force:
            raise FileExistsError(
                f"演示输出已存在: {project_dir}；如需重建请传入--force"
            )
        shutil.rmtree(project_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    original_projects_dir = store.PROJECTS_DIR
    original_now = store._now
    try:
        store.PROJECTS_DIR = projects_dir
        # Production writers own timestamps; freeze their clock so the replay digest is stable.
        store._now = lambda: payload["fixed_now"]

        store.init_project(project_id)
        requirements = payload["requirements"]
        store.write_requirements(
            project_id,
            topic=requirements["topic"],
            requirements=requirements["requirements"],
        )
        store.write_route_plan(
            project_id,
            actor="route_agent",
            route_plan=RoutePlan.model_validate(payload["route_plan"]),
        )
        store.write_agent_output_patch(
            project_id,
            actor="lifestyle_agent",
            items=payload["lifestyle_items"],
        )
        store.write_pitfall_output_patch(
            project_id,
            actor="pitfall_agent",
            items=payload["pitfall_items"],
        )

        manifest_entries: list[PoiDetailManifestEntry] = []
        for raw_detail in payload["poi_details"]:
            detail = PoiDetailFile.model_validate(raw_detail)
            filename = store.write_poi_detail(
                project_id,
                actor="poi_detail_agent",
                poi_detail=detail,
            )
            manifest_entries.append(
                PoiDetailManifestEntry(
                    poi_name=detail.poi_name,
                    day=detail.day,
                    file=filename,
                    citation_status=detail.citation_status,
                )
            )
        store.write_poi_details_manifest(
            project_id,
            actor="main_agent",
            manifest=PoiDetailManifest(generated=manifest_entries),
        )

        final_guide = payload["final_guide"]
        store.write_final_guide_blocks(
            project_id,
            actor="integration_agent",
            blocks=final_guide["blocks"],
            meta=dict(final_guide["meta"]),
        )
        issues = _contract_issues(project_id)
        if issues:
            raise RuntimeError(
                "公开演示未通过最终交付契约:\n"
                + json.dumps(issues, ensure_ascii=False, indent=2)
            )
        store.write_final_guide_contract_validation(
            project_id,
            status="passed",
            issues=[],
            actor="citation_checker",
            source="public_deterministic_demo",
        )
        store.update_progress(
            project_id,
            actor="main_agent",
            state=ProjectPhase.DONE,
            skeleton_confirmed=True,
            material_checked=True,
        )
        site_path = site_generator.generate_site(
            project_id,
            offline=True,
            standalone=True,
        )

        digest, files = _delivery_digest(store.workspace_dir(project_id))
        report = {
            "schema_version": "public-deterministic-demo-report-v1",
            "status": "passed",
            "project_id": project_id,
            "input_kind": "synthetic_frozen_sample",
            "external_calls": {
                "llm": 0,
                "web": 0,
                "amap": 0,
            },
            "contract_issue_count": 0,
            "route_day_count": len(payload["route_plan"]["days"]),
            "poi_detail_count": len(payload["poi_details"]),
            "delivery_sha256": digest,
            "delivery_files": files,
            "site": site_path.relative_to(output_root).as_posix(),
            "limitations": [
                "合成内容只验证工程链路，不证明真实旅游事实质量",
                "未调用任何Agent，也未执行路线地图、可执行性或素材事实核验",
                "contract_issue_count仅表示确定性的最终交付结构契约",
                "完整在线模式仍需要模型、Embedding、Web和地图等外部配置",
            ],
        }
        (output_root / "demo-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report
    finally:
        store.PROJECTS_DIR = original_projects_dir
        store._now = original_now


def main() -> int:
    parser = argparse.ArgumentParser(
        description="离线回放固定输入并生成结构化Project、Markdown和静态网站"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true", help="重建已存在的演示Project")
    args = parser.parse_args()
    report = run_demo(input_path=args.input, output_root=args.output, force=args.force)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
