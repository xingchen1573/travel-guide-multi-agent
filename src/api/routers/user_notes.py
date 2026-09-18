"""用户备注/annotation layer（docs/ARCHITECTURE.md第21节）：网页表单+后台API的增/改/删/列出。

跟src/api/routers/poi_photos.py同款模式——同步的小写写操作（一次HTTP请求内完成，不接入
jobs.py的机制A任务框架），actor统一记"admin_user"，不加鉴权（本地/内网自用，跟项目现有
src/api惯例一致）。权限校验/生命周期逻辑都在src/workspace/store.py，这里只是薄的HTTP层。

阶段1只有网页表单入口（写入身份仍统一记admin_user），飞书写入是阶段2范围，见ARCHITECTURE.md
第21.7节实现节奏。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.workspace import store

router = APIRouter(prefix="/api/projects", tags=["user_notes"])

_ACTOR = "admin_user"


def _require_project(project_id: str) -> None:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")


class UserNoteCreate(BaseModel):
    note_text: str
    day: int | None = None
    poi_name: str = ""


@router.get("/{project_id}/user_notes")
def get_user_notes(project_id: str) -> list[dict]:
    _require_project(project_id)
    return [note.model_dump(mode="json") for note in store.list_user_notes(project_id)]


@router.post("/{project_id}/user_notes")
def create_user_note(project_id: str, body: UserNoteCreate) -> dict:
    _require_project(project_id)
    note = store.write_user_note(
        project_id, _ACTOR, body.note_text, day=body.day, poi_name=body.poi_name, source="web",
    )
    return note.model_dump(mode="json")


@router.put("/{project_id}/user_notes/{note_id}")
def update_user_note(project_id: str, note_id: str, body: UserNoteCreate) -> dict:
    _require_project(project_id)
    try:
        note = store.update_user_note(
            project_id, _ACTOR, note_id, body.note_text, day=body.day, poi_name=body.poi_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return note.model_dump(mode="json")


@router.delete("/{project_id}/user_notes/{note_id}")
def delete_user_note(project_id: str, note_id: str) -> dict:
    _require_project(project_id)
    try:
        store.delete_user_note(project_id, _ACTOR, note_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"note_id": note_id, "deleted": True}
