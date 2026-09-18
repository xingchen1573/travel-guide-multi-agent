from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.config import PROJECT_ROOT, PROJECTS_DIR
from src.experiments import index as experiment_index

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


@router.get("")
def list_experiments(destination: str | None = None, harness: str | None = None, status: str | None = None) -> list[dict]:
    return experiment_index.list_experiments(destination=destination, harness=harness, status=status)


@router.post("/reindex")
def reindex_experiments() -> dict:
    return experiment_index.rebuild_index()


@router.get("/pairs")
def list_comparison_pairs(status: str | None = None) -> list[dict]:
    return experiment_index.list_comparison_pairs(status=status)


@router.get("/pairs/{pair_id}")
def get_comparison_pair(pair_id: str) -> dict:
    result = experiment_index.get_comparison_pair(pair_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"comparison pair {pair_id!r} 不存在")
    return result


@router.get("/{project_id}")
def get_experiment(project_id: str) -> dict:
    result = experiment_index.get_experiment(project_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"experiment {project_id!r} 不存在")
    return result


@router.get("/{project_id}/evidence")
def get_experiment_evidence(project_id: str, path: str = Query(min_length=1)) -> FileResponse:
    detail = experiment_index.get_experiment(project_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"experiment {project_id!r} 不存在")
    allowed = set(detail["metrics"].get("evidence_paths") or [])
    if path not in allowed:
        raise HTTPException(status_code=403, detail="该路径未登记为实验原始证据")
    base = PROJECT_ROOT if path.startswith("interview/") else PROJECTS_DIR / project_id
    target = (base / path).resolve()
    if base.resolve() not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="证据文件不存在")
    return FileResponse(target)
