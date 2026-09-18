"""管理后台"POI图片"页面：POI详情结构只读浏览 + 人工上传/替换/删除真实照片。

这是管理后台第一个真正的"人类发起的写操作"，正式打破此前"只读接口复用store.py的读
函数 + 唯一写操作是彻底重跑"的架构约定（docs/ARCHITECTURE.md第13.2节）。新写入身份
是"admin_user"（src/workspace/store.py的WRITE_PERMISSIONS["poi_photo"]），不是任何
一个agent——上传/删除都经过store.py的_check权限校验，不绕开这套模型。

上传是同步的小写操作（读文件字节+写磁盘+更新一个JSON字段），不接入jobs.py的机制A任务
框架——那套框架是为长耗时、需要状态轮询的任务设计的（重跑/采集/生成网站），这里的写入
在一次HTTP请求内就能完成，直接返回结果即可。

文件类型/大小校验见src/tools/poi_photo_upload.py；不做内容安全审核，见该模块docstring。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

from src.tools.poi_photo_upload import InvalidPhotoUpload, validate_upload
from src.workspace import store

router = APIRouter(prefix="/api/projects", tags=["poi_photos"])

_ACTOR = "admin_user"


def _require_project(project_id: str) -> None:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")


@router.get("/{project_id}/poi_details")
def get_poi_details(project_id: str) -> list[dict]:
    _require_project(project_id)
    return [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    try:
        return content, validate_upload(content)
    except InvalidPhotoUpload as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{project_id}/poi_photos/{day}/{poi_name}/overview")
async def upload_overview_photo(project_id: str, day: int, poi_name: str, file: UploadFile) -> dict:
    _require_project(project_id)
    content, ext = await _read_upload(file)
    try:
        detail = store.write_poi_overview_photo(project_id, _ACTOR, day, poi_name, content, ext)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return detail.model_dump(mode="json")


@router.delete("/{project_id}/poi_photos/{day}/{poi_name}/overview")
def delete_overview_photo(project_id: str, day: int, poi_name: str) -> dict:
    _require_project(project_id)
    try:
        detail = store.delete_poi_overview_photo(project_id, _ACTOR, day, poi_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return detail.model_dump(mode="json")


@router.get("/{project_id}/poi_photos/{day}/{poi_name}/overview")
def get_overview_photo(project_id: str, day: int, poi_name: str) -> FileResponse:
    _require_project(project_id)
    detail = store.read_poi_detail(project_id, day, poi_name)
    if detail is None or not detail.uploaded_overview_photo_path:
        raise HTTPException(status_code=404, detail="没有已上传的POI整体照片")
    path = store.workspace_dir(project_id) / detail.uploaded_overview_photo_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="没有已上传的POI整体照片")
    return FileResponse(path)


@router.post("/{project_id}/poi_photos/{day}/{poi_name}/segment")
async def upload_segment_photo(
    project_id: str, day: int, poi_name: str, route_name: str, order: int, file: UploadFile
) -> dict:
    _require_project(project_id)
    content, ext = await _read_upload(file)
    try:
        detail = store.write_poi_segment_photo(
            project_id, _ACTOR, day, poi_name, route_name, order, content, ext
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return detail.model_dump(mode="json")


@router.delete("/{project_id}/poi_photos/{day}/{poi_name}/segment")
def delete_segment_photo(project_id: str, day: int, poi_name: str, route_name: str, order: int) -> dict:
    _require_project(project_id)
    try:
        detail = store.delete_poi_segment_photo(project_id, _ACTOR, day, poi_name, route_name, order)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return detail.model_dump(mode="json")


def _find_segment_photo_path(project_id: str, day: int, poi_name: str, route_name: str, order: int) -> str:
    detail = store.read_poi_detail(project_id, day, poi_name)
    if detail is None:
        return ""
    for route in detail.routes:
        if route.route_name != route_name:
            continue
        for segment in route.route_segments:
            if segment.order == order:
                return segment.uploaded_photo_path
    return ""


@router.get("/{project_id}/poi_photos/{day}/{poi_name}/segment")
def get_segment_photo(project_id: str, day: int, poi_name: str, route_name: str, order: int) -> FileResponse:
    _require_project(project_id)
    relative_path = _find_segment_photo_path(project_id, day, poi_name, route_name, order)
    if not relative_path:
        raise HTTPException(status_code=404, detail="没有已上传的途经点照片")
    path = store.workspace_dir(project_id) / relative_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="没有已上传的途经点照片")
    return FileResponse(path)
