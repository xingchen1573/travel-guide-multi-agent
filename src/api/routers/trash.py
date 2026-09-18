"""回收站接口：Project和素材的查看、恢复、永久删除。

Project软删除入口在`DELETE /api/projects/{project_id}`；素材软删除入口在
`DELETE /api/materials/{topic}/entries/{doc_id}`。这里统一负责两类回收内容的读取和
后续处置，并用独立子路径区分素材资源。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from src.materials import recycle_bin, topic_spaces
from src.workspace import store

router = APIRouter(prefix="/api/trash", tags=["trash"])


def _trashed_project_summary(project_id: str) -> dict:
    """回收站列表用的精简摘要——不需要has_run_failure/has_pending_conflicts这类
    "是否可操作"的字段（trashed project不可操作，只能恢复或永久删除），直接从
    trashed_project_dir(project_id)/workspace/下读requirements.json/progress.json，
    不复用projects.py的_project_summary()（那个函数假设project在正常位置，
    store.read_requirements/read_progress找不到已经移到_trash/下的project）。
    """
    ws = store.trashed_project_dir(project_id) / "workspace"
    requirements_path = ws / "requirements.json"
    progress_path = ws / "progress.json"

    saved = None
    if requirements_path.exists():
        saved = json.loads(requirements_path.read_text(encoding="utf-8"))
    requirements = (saved or {}).get("requirements", {})

    progress = None
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))

    return {
        "project_id": project_id,
        "topic": (saved or {}).get("topic"),
        "destination": requirements.get("destination"),
        "days": requirements.get("days"),
        "state": (progress or {}).get("state"),
        "updated_at": (progress or {}).get("updated_at"),
    }


@router.get("")
def list_trash() -> list[dict]:
    return [_trashed_project_summary(pid) for pid in store.list_trashed_project_ids()]


@router.get("/materials")
def list_material_trash() -> list[dict]:
    return recycle_bin.list_trashed_materials()


@router.get("/material-topics")
def list_material_topic_trash() -> list[dict]:
    return topic_spaces.list_trashed_topics()


@router.post("/material-topics/{topic}/restore")
def restore_material_topic(topic: str) -> dict:
    try:
        return topic_spaces.restore_topic(topic)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/material-topics/{topic}")
def permanently_delete_material_topic(topic: str) -> dict:
    try:
        return topic_spaces.permanently_delete_topic(topic)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/materials/{topic}/{doc_id}/restore")
def restore_material(topic: str, doc_id: str) -> dict:
    try:
        return recycle_bin.restore_material(topic, doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/materials/{topic}/{doc_id}")
def permanently_delete_material(topic: str, doc_id: str) -> dict:
    try:
        return recycle_bin.permanently_delete_material(topic, doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project_id}/restore")
def restore(project_id: str) -> dict:
    if project_id not in store.list_trashed_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不在回收站里")
    try:
        store.restore_project(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"project_id": project_id, "status": "restored"}


@router.delete("/{project_id}")
def permanently_delete(project_id: str) -> dict:
    if project_id not in store.list_trashed_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不在回收站里")
    store.permanently_delete_project(project_id)
    return {"project_id": project_id, "status": "permanently_deleted"}
