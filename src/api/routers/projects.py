"""Project相关只读接口 + 唯一的写操作（重跑）。

只读部分直接复用src/workspace/store.py的读函数，不重新实现一遍文件解析逻辑。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from src.api import jobs
from src.channels.feishu import bindings
from src.observability import normalize_run_status
from src.review import store as review_store
from src.workspace import store

router = APIRouter(prefix="/api/projects", tags=["projects"])

PROJECT_TASK_TYPES = ("rerun", "generate_site", "generate_poster")


def _latest_project_job(project_id: str) -> Optional[dict]:
    candidates = [jobs.get_status(task_type, project_id) for task_type in PROJECT_TASK_TYPES]
    persisted = [job for job in candidates if job is not None]
    if not persisted:
        return None
    return max(
        persisted,
        key=lambda job: str(job.get("created_at") or job.get("started_at") or ""),
    )


def _visible_project_ids() -> list[str]:
    """Return initialized projects, excluding stray test/output directories.

    ``init_project`` always creates ``workspace/progress.json``. During a rerun
    the old workspace is briefly archived before the replacement is created, so
    an active SQLite-backed project task also keeps the project visible.
    """
    visible: list[str] = []
    for project_id in store.list_project_ids():
        progress_path = store.workspace_dir(project_id) / "progress.json"
        latest_job = _latest_project_job(project_id)
        if progress_path.is_file() or (
            latest_job is not None and latest_job.get("status") in {"queued", "running"}
        ):
            visible.append(project_id)
    return visible


def _pending_conflict_count(project_id: str) -> int:
    conflicts = store.read_workspace(project_id, "conflicts.jsonl") or []
    return sum(c.get("status") == "pending" for c in conflicts)


def _review_counts(project_id: str) -> dict[str, int]:
    """Read the current projection without mutating Workspace from a list request."""
    findings = review_store.read_findings(project_id)
    return {
        "review_findings_total": len(findings),
        "review_findings_open": sum(item.status != "closed" for item in findings),
        "review_findings_needs_rerun": sum(
            item.status != "closed" and item.disposition == "needs_rerun"
            for item in findings
        ),
    }


def _current_evidence_checks(route_plan: Optional[dict], evidence_checks: dict) -> dict:
    """Project route checks onto the claims that exist in the current plan.

    Historical workspaces may still contain active-looking rows for a replaced
    claim.  Keep those records in the persisted audit history, but never expose
    them as current output status merely because they share the same Day prefix.
    """
    if not route_plan:
        return evidence_checks

    claim_ids_by_day: dict[int, set[str]] = {}
    days_with_claim_contract: set[int] = set()
    for day in route_plan.get("days") or []:
        try:
            day_number = int(day.get("day") or 0)
        except (TypeError, ValueError):
            continue
        claims = day.get("claims") or []
        if claims:
            days_with_claim_contract.add(day_number)
        claim_ids_by_day[day_number] = {
            str(claim.get("claim_id") or "")
            for claim in claims
            if claim.get("claim_id")
        }

    current_route_checks: list[dict] = []
    for check in evidence_checks.get("route") or []:
        target_key = str(check.get("target_key") or "")
        parts = target_key.split(":")
        if len(parts) < 3 or parts[0] != "route" or parts[1] != "day":
            current_route_checks.append(check)
            continue
        try:
            day_number = int(parts[2])
        except ValueError:
            current_route_checks.append(check)
            continue
        if day_number not in claim_ids_by_day:
            continue
        if day_number in days_with_claim_contract:
            if str(check.get("claim_id") or "") in claim_ids_by_day[day_number]:
                current_route_checks.append(check)
            continue
        if target_key == f"route:day:{day_number}":
            current_route_checks.append(check)

    return {**evidence_checks, "route": current_route_checks}


def _project_quality_summary(project_id: str) -> dict:
    """Derive independent quality dimensions from existing workspace facts."""
    final_meta = store.read_workspace(project_id, "final_guide.meta.json") or {}
    contract = final_meta.get("contract_validation") or {}
    final_exists = isinstance(store.read_workspace(project_id, "final_guide.md"), str)
    delivery_status = str(contract.get("status") or ("passed" if final_exists else "unknown"))

    route_map = store.read_workspace(project_id, "route_map_validation.json") or {}
    feasibility = store.read_workspace(project_id, "route_feasibility.json") or {}
    route_risk_status = str(
        route_map.get("status") or feasibility.get("overall_status") or "unknown"
    )

    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    expected_poi = sum(
        bool(poi.get("needs_detail"))
        for day in route_plan.get("days", [])
        if isinstance(day, dict)
        for poi in day.get("pois", [])
        if isinstance(poi, dict)
    )
    manifest = store.read_workspace(project_id, "outputs/poi_details/manifest.json") or {}
    completed_poi = 0
    for entry in manifest.get("generated", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("citation_status") in {"generation_failed", "force_finalized"}:
            continue
        file_name = str(entry.get("file") or "")
        if file_name and (store.workspace_dir(project_id) / "outputs" / "poi_details" / file_name).is_file():
            completed_poi += 1
    state_validation = review_store.validate_project_state(project_id)
    force_finalized_count = len(state_validation.get("raw_force_finalized_paths") or [])
    progress = store.read_progress(project_id)
    completion_mode = None
    if progress.state.value == "完成":
        completion_mode = (
            "completed_with_degradation"
            if force_finalized_count
            else "completed"
        )
    return {
        "delivery_status": delivery_status,
        "route_risk_status": route_risk_status,
        "poi_detail_expected": expected_poi,
        "poi_detail_completed": completed_poi,
        "completion_mode": completion_mode,
        "force_finalized_count": force_finalized_count,
    }


def _has_run_failure(project_id: str, run_status: Optional[dict] = None) -> bool:
    """run_status.json是单文件覆写的：node执行前先写running，执行完成后覆盖成done/failed。
    最后一次写入是failed，就意味着从那次失败之后再没有任何node成功执行过、覆盖掉这个失败态——
    不需要额外的时间戳比较，这个信号本身就是权威的"最近一次graph执行是否卡在失败节点"。

    真实背景（用户在管理后台发现）：progress.json的state只在阶段跳转成功时更新，节点内部
    失败（如lifestyle_agent多次重试仍未完成写入）会让run_plan()直接抛异常退出，state永远
    停留在崩溃前最后一次成功跳转的阶段（比如"并行调研中"），列表页/总览页顶部进度条对此
    完全没有可见信号，只有点进详情页"实时Agent运行状态"区块才能发现。这里补上这个信号，
    供列表页/总览页展示醒目的失败标记，不是真的在运行。
    """
    if run_status is None:
        run_status = normalize_run_status(store.read_run_status(project_id))
    return bool(run_status) and run_status.get("status") == "failed"


def _project_summary(project_id: str) -> dict:
    saved = store.read_requirements(project_id)
    progress = store.read_progress(project_id)
    requirements = (saved or {}).get("requirements", {})
    latest_job = _latest_project_job(project_id)
    observed_run = normalize_run_status(store.read_run_status(project_id))
    pending_conflict_count = _pending_conflict_count(project_id)
    review_counts = _review_counts(project_id)
    return {
        "project_id": project_id,
        "topic": (saved or {}).get("topic"),
        "destination": requirements.get("destination"),
        "days": requirements.get("days"),
        "style": requirements.get("style"),
        "budget": requirements.get("budget"),
        "notes": requirements.get("notes"),
        # docs/ARCHITECTURE.md第18节：用户主导路线设定，只读展示，无编辑入口。
        "must_visit": requirements.get("must_visit") or [],
        "style_interpretation": requirements.get("style_interpretation"),
        "state": progress.state.value,
        "updated_at": progress.updated_at,
        "has_pending_conflicts": pending_conflict_count > 0,
        "pending_conflict_count": pending_conflict_count,
        "has_open_findings": review_counts["review_findings_open"] > 0,
        "has_run_failure": (
            _has_run_failure(project_id, observed_run)
            or (latest_job or {}).get("status") == "failed"
        ),
        "latest_job_status": (latest_job or {}).get("status"),
        "latest_job_health_status": (latest_job or {}).get("health_status"),
        "observed_run_status": (observed_run or {}).get("status"),
        **_project_quality_summary(project_id),
        **review_counts,
    }


@router.get("")
def list_projects() -> list[dict]:
    return [_project_summary(pid) for pid in _visible_project_ids()]


def _issue_type_stats() -> list[dict]:
    """扫描所有project的conflicts.jsonl，按issue_type聚合计数（docs/ARCHITECTURE.md第19.2节）。
    不新增独立存储，直接复用现有conflicts.jsonl；旧记录没有issue_type字段时落进"unspecified"
    分组，不丢弃，方便判断"未打标签的历史冲突"占比。
    """
    counts: dict[str, int] = {}
    for project_id in _visible_project_ids():
        conflicts = store.read_workspace(project_id, "conflicts.jsonl") or []
        for c in conflicts:
            if c.get("status") != "pending":
                continue
            issue_type = c.get("issue_type") or "unspecified"
            counts[issue_type] = counts.get(issue_type, 0) + 1
    return sorted(
        ({"issue_type": k, "count": v} for k, v in counts.items()),
        key=lambda item: item["count"],
        reverse=True,
    )


@router.get("/issue_type_stats")
def get_issue_type_stats() -> list[dict]:
    """路由必须放在`/{project_id}`之前——否则`{project_id}`会先匹配掉字面路径
    `/issue_type_stats`，把它当成一个project_id去查找（FastAPI/Starlette按注册顺序匹配）。"""
    return _issue_type_stats()


@router.get("/{project_id}")
def get_project(project_id: str) -> dict:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    progress = store.read_progress(project_id)
    return {
        **_project_summary(project_id),
        "progress": {
            "state": progress.state.value,
            "agents": progress.agents,
            "skeleton_confirmed": progress.skeleton_confirmed,
            "material_checked": progress.material_checked,
            "updated_at": progress.updated_at,
        },
    }


@router.get("/{project_id}/outputs")
def get_project_outputs(project_id: str) -> dict:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    route_plan = store.read_workspace(project_id, "route_plan.json")
    evidence_checks = _current_evidence_checks(
        route_plan,
        store.read_evidence_checks(project_id),
    )
    return {
        "route_plan": route_plan,
        "route_map_validation": store.read_workspace(project_id, "route_map_validation.json"),
        "route_feasibility": store.read_workspace(project_id, "route_feasibility.json"),
        "lifestyle": store.read_workspace(project_id, "outputs/lifestyle.json"),
        "pitfall": store.read_workspace(project_id, "outputs/pitfall.json"),
        "poi_details": [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)],
        "evidence_checks": evidence_checks,
        "final_guide": store.read_workspace(project_id, "final_guide.md"),
        "final_guide_meta": store.read_workspace(project_id, "final_guide.meta.json"),
        "final_guide_blocks": (
            blocks.model_dump(mode="json")
            if (blocks := store.read_final_guide_blocks(project_id)) is not None
            else None
        ),
    }


@router.get("/{project_id}/timeline")
def get_project_timeline(project_id: str) -> list[dict]:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")

    changelog = store.read_workspace(project_id, "changelog.jsonl") or []
    conflicts = store.read_workspace(project_id, "conflicts.jsonl") or []
    review_store.backfill_findings_from_review_log(project_id)
    review_store.sync_state_findings(project_id)
    findings = review_store.read_findings(project_id)

    events = []
    for entry in changelog:
        events.append({"type": "write", "timestamp": entry.get("timestamp"), **entry})
    for entry in conflicts:
        events.append({"type": "conflict", "timestamp": entry.get("created_at"), **entry})
    # 当前 finding 投影：历史冲突继续保留，但时间线同时展示它现在的复核状态。
    for finding in findings:
        events.append({
            "type": "review_finding",
            "timestamp": finding.updated_at or finding.created_at,
            "finding_id": finding.finding_id,
            "status": finding.status,
            "disposition": str(finding.disposition),
            "verification_status": finding.verification_status,
            "target": finding.target,
            "problem_type": finding.problem_type,
            "summary": finding.issue_summary or finding.notes,
            "source_review_id": finding.source_review_id,
        })

    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


@router.post("/{project_id}/rerun", status_code=202)
def rerun_project(project_id: str) -> dict:
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    if store.read_requirements(project_id) is None:
        raise HTTPException(
            status_code=400,
            detail=f"project {project_id!r} 没有 requirements.json，无法重跑",
        )
    try:
        jobs.trigger_rerun(project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"project_id": project_id, "status": "running"}


@router.get("/{project_id}/rerun/status")
def get_rerun_status(project_id: str) -> Optional[dict]:
    status = jobs.get_status("rerun", project_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 没有重跑任务记录")
    return status


@router.get("/{project_id}/run_status")
def get_run_status(project_id: str) -> Optional[dict]:
    """机制B（docs/ARCHITECTURE.md第13.2节）：读orchestrator节点执行时写的run_status.json，
    展示当前在跑哪个node/第几次调用/开始时间——不管这次执行来自CLI（run_mvp.py）还是
    后台API触发的重跑，落盘的文件都是同一个，前端轮询这一个接口即可。project不存在时
    404；project存在但从没跑过图（run_status.json还没生成过）时返回None，不是404，
    区分"project不存在"和"project存在但还没有运行记录"这两种不同语义。
    """
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    return normalize_run_status(store.read_run_status(project_id))


def _is_project_task_running(project_id: str) -> bool:
    """检查这个project是否有正在跑的后台任务——只检查以project_id为key的任务类型
    （重跑/生成网站/生成海报），不检查素材采集（collect_material的key是topic，不是
    project_id，是跨project共享的全局任务，不属于"这个project正在运行"的范畴）。"""
    return any(
        jobs.is_running(task_type, project_id)
        for task_type in PROJECT_TASK_TYPES
    )


@router.delete("/{project_id}")
def delete_project(project_id: str) -> dict:
    """软删除：移入回收站（PROJECTS_DIR/_trash/<project_id>/），管理后台"回收站"页面
    可以恢复或永久删除。有正在运行的后台任务（重跑/生成网站/生成海报）时拒绝删除，
    避免任务执行到一半时project目录被移走导致任务失败在一半状态。删除成功后顺带
    解除该project在飞书侧的绑定关系（如果有），避免该飞书群继续路由消息到一个
    已经被移除的project——见src/channels/feishu/bindings.py::unbind_project。"""
    if project_id not in store.list_project_ids():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} 不存在")
    if _is_project_task_running(project_id):
        raise HTTPException(
            status_code=409,
            detail=f"project {project_id!r} 有正在运行的任务，暂时无法删除",
        )
    store.soft_delete_project(project_id)
    bindings.unbind_project(project_id)
    return {"project_id": project_id, "status": "trashed"}
