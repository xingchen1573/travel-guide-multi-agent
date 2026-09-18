"""呈现层产出触发+预览（机制D：run_generate_site.py/run_generate_poster.py函数化接入
后台任务框架，见docs/ARCHITECTURE.md第13.2节）。

生成结果（site.html/poster.png）不经过store.py的写入权限校验体系——这是呈现层产物，
跟坐标/图片数据一样只在呈现层流转，不进产出区，`run_generate_site.py`/
`run_generate_poster.py`本身就是直接写workspace_dir(project_id)下的文件，这里复用
同一套逻辑，不重新实现一遍。

海报生成是按次收费的真实API调用，`generate_poster(confirm=False)`跳过CLI默认的命令行
input()二次确认（那个交互在后台线程里会卡死）——等效的二次确认改由前端在真正发起
`POST .../generate_poster`前弹窗完成，这里的POST本身就是"已确认，请执行"的语义，
不再额外加一层确认。

版本历史（2026-07-26新增）：`jobs.trigger_generate_site`/`trigger_generate_poster`
生成前会把旧产物归档到`workspace/history/<kind>/<timestamp>/`（见`src/api/jobs.py`的
`_archive_previous_output`），这里新增对应的只读列表+读取接口，管理后台呈现层页面
可以回看历次生成结果，不再是"生成一次覆盖一次、旧版本直接丢失"。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.api import jobs
from src.workspace import store

router = APIRouter(prefix="/api/projects", tags=["presentation"])


class DailyPosterRequest(BaseModel):
    day: int


def _history_dir(project_id: str, kind: str) -> Path:
    return store.workspace_dir(project_id) / "history" / kind


def _list_history_timestamps(project_id: str, kind: str) -> list[str]:
    history_dir = _history_dir(project_id, kind)
    if not history_dir.exists():
        return []
    return sorted((p.name for p in history_dir.iterdir() if p.is_dir()), reverse=True)


def _asset_path(root: Path, relative_path: str) -> Path:
    """Resolve a generated-site asset without allowing path traversal."""
    root = root.resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=404, detail="网站资源不存在")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="网站资源不存在")
    return path


def _imported_poster_path(project_id: str, scope: str = "overall", day: int | None = None) -> Path | None:
    """Return a checked-in/imported poster asset when no generated workspace file exists."""
    if "qinggan" not in project_id.lower():
        return None
    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / ("整体.png" if scope == "overall" else f"day{day}.png")
    return path if path.is_file() else None


def _poster_asset_info(project_id: str) -> dict:
    workspace = store.workspace_dir(project_id)
    overall_generated = workspace / "poster.png"
    overall_imported = _imported_poster_path(project_id)
    days: dict[str, dict[str, str | bool | None]] = {}
    for day in range(1, 9):
        generated = workspace / f"poster-day{day}.png"
        imported = _imported_poster_path(project_id, "day", day)
        if generated.is_file():
            days[str(day)] = {"available": True, "source": "generated", "path": f"poster-day{day}.png"}
        elif imported:
            days[str(day)] = {"available": True, "source": "imported", "path": imported.name}
        else:
            days[str(day)] = {"available": False, "source": None, "path": None}
    if overall_generated.is_file():
        overall = {"available": True, "source": "generated", "path": "poster.png"}
    elif overall_imported:
        overall = {"available": True, "source": "imported", "path": overall_imported.name}
    else:
        overall = {"available": False, "source": None, "path": None}
    return {"overall": overall, "days": days}


@router.post("/{project_id}/generate_site", status_code=202)
def trigger_generate_site(project_id: str) -> dict:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    try:
        jobs.trigger_generate_site(project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"project_id": project_id, "status": "running"}


@router.get("/{project_id}/generate_site/status")
def get_generate_site_status(project_id: str) -> dict | None:
    status = jobs.get_status("generate_site", project_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 没有生成网站的任务记录")
    return status


@router.get("/{project_id}/site")
def get_generated_site(project_id: str) -> FileResponse:
    path = store.workspace_dir(project_id) / "site.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 还没有生成过site.html")
    return FileResponse(path, media_type="text/html")


@router.get("/{project_id}/site_assets/{asset_path:path}")
def get_generated_site_asset(project_id: str, asset_path: str) -> FileResponse:
    """Serve full-resolution images emitted beside the current site.html."""
    assets_dir = store.workspace_dir(project_id) / "site_assets"
    return FileResponse(_asset_path(assets_dir, asset_path))


@router.get("/{project_id}/site/download")
def download_generated_site(project_id: str) -> FileResponse:
    """跟GET .../site同一份site.html，只是加Content-Disposition:attachment让浏览器
    触发下载而不是内联展示——HTML中的完整图片通过同项目site_assets在线加载；供后台
    "下载副本"按钮+site.html内嵌"⬇下载本页副本"链接共用，见docs/ARCHITECTURE.md第21.4节。"""
    path = store.workspace_dir(project_id) / "site.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 还没有生成过site.html")
    return FileResponse(
        path, media_type="text/html", filename="site.html",
        content_disposition_type="attachment",
    )


@router.get("/{project_id}/site/history")
def list_site_history(project_id: str) -> list[str]:
    """列出历次生成的网站版本时间戳（新到旧排序），供管理后台呈现层页面展示版本历史。"""
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    return _list_history_timestamps(project_id, "site")


@router.get("/{project_id}/site/history/site_assets/{asset_path:path}")
def get_site_history_asset(project_id: str, asset_path: str) -> FileResponse:
    """History pages reuse immutable, content-hashed assets kept in the project workspace."""
    assets_dir = store.workspace_dir(project_id) / "site_assets"
    return FileResponse(_asset_path(assets_dir, asset_path))


@router.get("/{project_id}/site/history/{timestamp}")
def get_site_history_version(project_id: str, timestamp: str) -> FileResponse:
    path = _history_dir(project_id, "site") / timestamp / "site.html"
    if not path.exists():
        raise HTTPException(
            status_code=404, detail=f"project {project_id!r} 没有时间戳为 {timestamp!r} 的历史网站版本"
        )
    return FileResponse(path, media_type="text/html")


@router.post("/{project_id}/generate_poster", status_code=202)
def trigger_generate_poster(project_id: str) -> dict:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    try:
        jobs.trigger_generate_poster(project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"project_id": project_id, "status": "running"}


@router.get("/{project_id}/generate_poster/status")
def get_generate_poster_status(project_id: str) -> dict | None:
    status = jobs.get_status("generate_poster", project_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 没有生成海报的任务记录")
    return status


@router.post("/{project_id}/generate_daily_poster", status_code=202)
def trigger_generate_daily_poster(project_id: str, body: DailyPosterRequest) -> dict:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    if body.day < 1 or body.day > 8:
        raise HTTPException(status_code=400, detail="单日海报只支持Day1到Day8")
    try:
        jobs.trigger_generate_daily_poster(project_id, body.day)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"project_id": project_id, "day": body.day, "status": "running"}


@router.get("/{project_id}/generate_daily_poster/{day}/status")
def get_generate_daily_poster_status(project_id: str, day: int) -> dict | None:
    if day < 1 or day > 8:
        raise HTTPException(status_code=400, detail="单日海报只支持Day1到Day8")
    status = jobs.get_status("generate_poster_daily", f"{project_id}:{day}")
    if status is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 没有Day{day}海报任务记录")
    return status


@router.get("/{project_id}/poster")
def get_generated_poster(project_id: str) -> FileResponse:
    path = store.workspace_dir(project_id) / "poster.png"
    if not path.exists():
        path = _imported_poster_path(project_id) or path
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 还没有可用的整体海报")
    return FileResponse(path, media_type="image/png")


@router.get("/{project_id}/poster/day/{day}")
def get_generated_daily_poster(project_id: str, day: int) -> FileResponse:
    if day < 1 or day > 8:
        raise HTTPException(status_code=400, detail="单日海报只支持Day1到Day8")
    path = store.workspace_dir(project_id) / f"poster-day{day}.png"
    if not path.exists():
        path = _imported_poster_path(project_id, "day", day) or path
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 还没有可用的Day{day}海报")
    return FileResponse(path, media_type="image/png")


@router.get("/{project_id}/poster/assets")
def get_poster_assets(project_id: str) -> dict:
    """Describe current overall/daily poster assets for the management page."""
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    return _poster_asset_info(project_id)


@router.get("/{project_id}/poster/prompt")
def get_poster_prompt(project_id: str) -> dict:
    """读取当前海报对应的poster_prompt.txt（run_generate_poster.py留的审计存档，
    记录实际喂给生图模型的内容，供人工核对内容有没有被模型篡改，见该脚本docstring）。
    """
    path = store.workspace_dir(project_id) / "poster_prompt.txt"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 还没有生成过海报，没有prompt存档")
    return {"prompt": path.read_text(encoding="utf-8")}


@router.get("/{project_id}/poster/day/{day}/prompt")
def get_daily_poster_prompt(project_id: str, day: int) -> dict:
    if day < 1 or day > 8:
        raise HTTPException(status_code=400, detail="单日海报只支持Day1到Day8")
    path = store.workspace_dir(project_id) / f"poster-day{day}_prompt.txt"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 还没有Day{day}海报prompt存档")
    return {"prompt": path.read_text(encoding="utf-8")}


@router.get("/{project_id}/poster/history")
def list_poster_history(project_id: str) -> list[str]:
    """列出历次生成的海报版本时间戳（新到旧排序）。"""
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    return _list_history_timestamps(project_id, "poster")


@router.get("/{project_id}/poster/day/{day}/history")
def list_daily_poster_history(project_id: str, day: int) -> list[str]:
    if day < 1 or day > 8:
        raise HTTPException(status_code=400, detail="单日海报只支持Day1到Day8")
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    return _list_history_timestamps(project_id, f"poster-day{day}")


@router.get("/{project_id}/poster/history/{timestamp}")
def get_poster_history_version(project_id: str, timestamp: str) -> FileResponse:
    path = _history_dir(project_id, "poster") / timestamp / "poster.png"
    if not path.exists():
        raise HTTPException(
            status_code=404, detail=f"project {project_id!r} 没有时间戳为 {timestamp!r} 的历史海报版本"
        )
    return FileResponse(path, media_type="image/png")


@router.get("/{project_id}/poster/day/{day}/history/{timestamp}")
def get_daily_poster_history_version(project_id: str, day: int, timestamp: str) -> FileResponse:
    if day < 1 or day > 8:
        raise HTTPException(status_code=400, detail="单日海报只支持Day1到Day8")
    path = _history_dir(project_id, f"poster-day{day}") / timestamp / f"poster-day{day}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 没有Day{day}历史海报版本")
    return FileResponse(path, media_type="image/png")


@router.get("/{project_id}/poster/history/{timestamp}/prompt")
def get_poster_history_prompt(project_id: str, timestamp: str) -> dict:
    """读取历史某一版海报对应的poster_prompt.txt存档。"""
    path = _history_dir(project_id, "poster") / timestamp / "poster_prompt.txt"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"project {project_id!r} 时间戳 {timestamp!r} 没有对应的prompt存档",
        )
    return {"prompt": path.read_text(encoding="utf-8")}


@router.get("/{project_id}/poster/day/{day}/history/{timestamp}/prompt")
def get_daily_poster_history_prompt(project_id: str, day: int, timestamp: str) -> dict:
    if day < 1 or day > 8:
        raise HTTPException(status_code=400, detail="单日海报只支持Day1到Day8")
    path = _history_dir(project_id, f"poster-day{day}") / timestamp / f"poster-day{day}_prompt.txt"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 没有Day{day}历史prompt存档")
    return {"prompt": path.read_text(encoding="utf-8")}
