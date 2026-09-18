"""管理后台后台任务的通用执行框架：SQLite 状态 + 进程内线程注册表。

背景（机制A，见docs/ARCHITECTURE.md第13.2节）：最初只有"重跑"一种任务，`_jobs`直接用
project_id当key。这次要支持四种任务类型（重跑/素材采集/生成网站/生成海报），且素材采集
不是针对单个project、是针对topic，任务key不能再假设"就是project_id"——改成
`(task_type, key)`二元组（拼成字符串`f"{task_type}:{key}"`存进同一个`_jobs`字典），
每种具体任务只需要提供一个"work_fn(log)"函数接入`trigger()`这个通用骨架，不需要各自
重新实现一遍加锁/状态机/异常捕获。

当前设计：
- SQLite 是任务当前状态和并发判断的事实源，进程重启后仍可协调历史活动任务。
- ``_jobs`` 只保留当前进程线程的展示缓存/旧测试兼容，不参与生产状态判断。
- 每个任务在后台`threading.Thread`里跑（不用FastAPI的`BackgroundTasks`，避免两套并发语义）。
- 新增`log: list[str]`字段：素材采集这类耗时任务，调用方可以通过`work_fn`拿到的`log`回调
  持续追加进度文本，前端轮询时可以展示"最新几条"而不是只有一个终态。
"""
from __future__ import annotations

import shutil
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from src.execution_state import (
    JobConflictError,
    configure_execution_context,
    environment_name,
    get_current_instance_id,
    get_job_service,
)
from src.workspace import store

_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_key(task_type: str, key: str) -> str:
    return f"{task_type}:{key}"


def get_status(task_type: str, key: str) -> Optional[dict]:
    persisted = get_job_service().get_latest_job(task_type, key)
    if persisted is not None:
        return persisted
    with _jobs_lock:
        job = _jobs.get(_job_key(task_type, key))
        return dict(job) if job else None


def is_running(task_type: str, key: str) -> bool:
    job = get_job_service().get_latest_job(task_type, key)
    if job is not None:
        return job["status"] in {"queued", "running"}
    with _jobs_lock:
        legacy = _jobs.get(_job_key(task_type, key))
        return bool(legacy and legacy["status"] in {"queued", "running"})


def _append_log(job_id: str, task_type: str, key: str, message: str) -> bool:
    if not get_job_service().append_log(job_id, message):
        return False
    with _jobs_lock:
        job = _jobs.get(_job_key(task_type, key))
        if job is not None:
            job["log"].append(message)
    return True


def _backup_workspace(project_id: str) -> Path:
    """重跑前把旧workspace/整体移走备份，语义是"清空重建"不是"断点续跑"。"""
    ws = store.workspace_dir(project_id)
    if not ws.exists():
        return ws
    backups_dir = store.project_dir(project_id) / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest = backups_dir / timestamp / "workspace"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws), str(dest))
    return dest


def _archive_previous_output(project_id: str, kind: str, filenames: list[str]) -> Optional[str]:
    """生成网站/海报前，把workspace_dir下已存在的旧产物移到history/<kind>/<timestamp>/，
    不是直接覆盖丢失（见docs/ARCHITECTURE.md第13.2节呈现层管理工具的版本历史需求）。

    kind："site"或"poster"，各自独立的历史目录，互不干扰。filenames是这次要归档的
    文件名清单（poster还带着poster_prompt.txt一起挪走，保持"图+审计存档"配套）。
    不存在旧文件时返回None，不创建空的历史目录。
    """
    ws = store.workspace_dir(project_id)
    existing = [f for f in filenames if (ws / f).exists()]
    if not existing:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest = ws / "history" / kind / timestamp
    dest.mkdir(parents=True, exist_ok=True)
    for filename in existing:
        shutil.move(str(ws / filename), str(dest / filename))
    return timestamp


def _run_job(
    job_id: str,
    task_type: str,
    key: str,
    owner_instance_id: str,
    work_fn: Callable[[Callable[[str], None]], Optional[dict]],
) -> None:
    """通用执行骨架：跑work_fn，成功则把返回的额外字段（如output_path）合并进任务状态，
    异常则捕获转成failed状态——调用方（后台线程）的异常绝不能静默崩溃，必须落到任务状态里。
    """
    job_key = _job_key(task_type, key)
    service = get_job_service()
    if not service.start_job(job_id, owner_instance_id):
        return

    def log(message: str) -> None:
        _append_log(job_id, task_type, key, message)

    try:
        extra = work_fn(log) or {}
        # 条件更新是终态并发保护：若协调器/取消操作已经先落终态，旧线程的迟到
        # 成功结果会被拒绝，不能把 interrupted/cancelled 覆盖回 succeeded。
        if service.succeed_job(job_id, extra):
            with _jobs_lock:
                if job_key in _jobs:
                    _jobs[job_key].update({
                        "status": "succeeded",
                        "error": None,
                        "finished_at": _now(),
                        **extra,
                    })
    except Exception as exc:  # noqa: BLE001 — 后台线程，异常必须被捕获转成任务状态
        tb = traceback.format_exc()
        # 只存进内存任务状态表还不够——独立跑的worker进程（如run_feishu_worker.py）
        # 没有任何API能查询这个内存表，异常会完全无声无息（真实联调时发现这个可观测性
        # 缺口：飞书群里卡在旧的进度消息不动，worker日志却什么都没打，白白花时间去猜）。
        # 打到stdout是本来就该有的最低限度可观测性，不是临时debug代码。
        print(f"[jobs] 任务 {task_type}:{key!r} 失败：{exc}\n{tb}")
        if service.fail_job(job_id, {"message": f"{exc}\n{tb}", "retryable": False}):
            with _jobs_lock:
                if job_key in _jobs:
                    _jobs[job_key].update({
                        "status": "failed",
                        "error": f"{exc}\n{tb}",
                        "finished_at": _now(),
                    })


def trigger(
    task_type: str,
    key: str,
    work_fn: Callable[[Callable[[str], None]], Optional[dict]],
    *,
    retry_of_job_id: str | None = None,
) -> None:
    """启动一次后台任务。调用方（各trigger_xxx函数）负责传入具体的work_fn，
    这里只负责并发锁+起后台线程+任务状态记账，跟任务类型无关。"""
    service = get_job_service()
    owner_instance_id = get_current_instance_id()
    if owner_instance_id is None:
        # CLI/单元级直接调用没有 FastAPI lifespan；仍注册一个明确 owner，避免新任务
        # 自己成为无 owner 的历史兼容记录。测试默认使用临时 SQLite。
        owner_instance_id = service.register_instance(
            role="standalone",
            environment=environment_name(),
        )
        configure_execution_context(service, owner_instance_id)
    try:
        persisted = service.create_job(
            task_type,
            key,
            environment=environment_name(),
            retry_of_job_id=retry_of_job_id,
        )
    except JobConflictError as exc:
        raise RuntimeError(str(exc)) from exc

    job_id = persisted["job_id"]
    job_key = _job_key(task_type, key)
    with _jobs_lock:
        _jobs[job_key] = {
            "job_id": job_id,
            "task_type": task_type,
            "key": key,
            "status": "queued",
            "error": None,
            "started_at": None,
            "finished_at": None,
            "log": [],
        }

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, task_type, key, owner_instance_id, work_fn),
        name=f"job-{task_type}-{job_id[-8:]}",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# 具体任务类型：各自只提供一个work_fn，接入上面的通用骨架
# ---------------------------------------------------------------------------


def trigger_rerun(project_id: str) -> None:
    """彻底重跑：语义是清空重建，不是断点续跑。调用方必须先自己判断project存在。"""
    def work(log: Callable[[str], None]) -> None:
        saved = store.read_requirements(project_id)
        if saved is None:
            raise ValueError(f"project {project_id!r} 没有找到 requirements.json，无法重跑")
        requirements = saved["requirements"]

        log("备份旧workspace...")
        _backup_workspace(project_id)

        # 懒加载：orchestrator.py依赖langgraph/langchain，只在真正触发重跑时才导入，
        # 避免只读接口/其他不涉及重跑的场景也被迫要求这套依赖装好。
        from src.orchestrator import run_plan

        log("开始重新调用run_plan...")
        run_plan(project_id, requirements)
        log("重跑完成")

    trigger("rerun", project_id, work)


def trigger_collect_material(topic: str, request: str, platform: str = "all") -> None:
    """触发核心素材agent的自主采集（提案->采集入库），auto_confirm=True跳过人工确认暂停
    （还没有真实交互界面能展示候选给人工点选前，暂停等于卡住流程，见
    src/agents/material_agent/collection_graph.py）。key用topic（不是project_id）——
    素材库是跨project共享的全局库，采集任务天然按目的地隔离，不是针对单个project。

    platform（2026-07-26新增）："all"/"bilibili"/"douyin"，透传给run_material_collection
    做工具级硬过滤（见管理后台采集对话框的显式平台选择器）。log回调（已有机制）现在会
    透传进run_material_collection，采集过程的中间步骤（第几轮/候选平台拆分/入库结果）
    会实时追加进这里的log列表，不再只有"开始采集"和最终摘要两条。
    """
    def work(log: Callable[[str], None]) -> dict:
        from src.agents.material_agent.collection_graph import run_material_collection

        log(f"开始采集: {topic} — {request}（平台：{platform}）")
        result = run_material_collection(topic, request, auto_confirm=True, platform=platform, log=log)
        summary = result.get("result_summary", "(无结果摘要)")
        log(summary)
        return {"result_summary": summary}

    trigger("collect_material", topic, work)


def trigger_regenerate_insights(topic: str) -> None:
    """触发洞察重新生成（轻量模型调用，量级几秒到几十秒）。此前是同步阻塞的HTTP请求，
    用户切走页面再切回来会看到"中断了"的假象（其实是浏览器发出的那次fetch还在后台跑，
    只是承载"生成中"状态的页面组件已经卸载）——2026-08-01改成跟重跑/采集/生成网站
    同款jobs框架，切走再切回、刷新页面都能查到真实进度，不再依赖页面组件生命周期。
    key用topic（不是project_id）——素材库跨project共享，同一topic重复触发直接409，
    跟trigger_collect_material同一个理由。"""
    def work(log: Callable[[str], None]) -> dict:
        from src.materials.insights import generate_insights

        log(f"开始为「{topic}」重新生成洞察...")
        result = generate_insights(topic)
        if result is None:
            # 正常情况下不会走到这里——路由层已经在触发前做过entry数前置校验，
            # 这里只是双重保险（比如触发和真正执行之间素材被并发删除的极端场景）。
            raise ValueError(f"素材不足（entry数<5），暂无法为「{topic}」生成洞察")
        log("洞察生成完成")
        return {"result": result.model_dump()}

    trigger("regenerate_insights", topic, work)


def trigger_generate_site(project_id: str) -> None:
    """触发静态展示网站生成（纯程序拼接，不调用LLM），见run_generate_site.py。

    生成前先把已存在的旧site.html归档到history/site/<timestamp>/（不是直接覆盖丢失，
    管理后台呈现层页面需要能回看历史版本），见_archive_previous_output。
    """
    def work(log: Callable[[str], None]) -> dict:
        from scripts.delivery.run_generate_site import generate_site

        archived_at = _archive_previous_output(project_id, "site", ["site.html"])
        if archived_at:
            log(f"已归档旧版本网站到 history/site/{archived_at}/")

        log("开始生成网站...")
        output_path = generate_site(project_id)
        log(f"网站已生成: {output_path}")
        return {"output_path": str(output_path)}

    trigger("generate_site", project_id, work)


def trigger_review_validation(project_id: str) -> None:
    """Validate all applied pending review patches as one bounded project batch."""
    def work(log: Callable[[str], None]) -> dict:
        from src.review.validation import run_pending_patch_validation
        from scripts.delivery.run_generate_site import generate_site

        def build_site(target_project_id: str) -> Path:
            archived_at = _archive_previous_output(target_project_id, "site", ["site.html"])
            if archived_at:
                log(f"已归档旧版本网站到 history/site/{archived_at}/")
            return generate_site(target_project_id)

        result = run_pending_patch_validation(
            project_id,
            log=log,
            site_builder=build_site,
        )
        return {
            "validation_status": result.get("status"),
            "checked_at": result.get("checked_at"),
            "scope": result.get("scope"),
            "issue_count": result.get("issue_count", 0),
            "issues": result.get("issues", {}),
            "site_output_path": result.get("site_output_path", ""),
        }

    trigger("review_validation", project_id, work)


def trigger_generate_poster(project_id: str) -> None:
    """触发概览海报生成（调用gpt-image-2生图API，按次收费）。confirm=False跳过
    run_generate_poster.py默认的命令行input()二次确认——那个交互在后台线程里会卡死，
    等效的"二次确认"改由前端在真正发起这个请求前弹窗完成（见docs/ARCHITECTURE.md第13.2节）。

    生成前先把已存在的旧poster.png+poster_prompt.txt一起归档到history/poster/<timestamp>/
    （图和对应的审计存档要配套挪走，不能图归档了、prompt却留在原地对不上），见
    _archive_previous_output。
    """
    def work(log: Callable[[str], None]) -> dict:
        from scripts.delivery.run_generate_poster import generate_poster

        archived_at = _archive_previous_output(
            project_id, "poster", ["poster.png", "poster_prompt.txt"]
        )
        if archived_at:
            log(f"已归档旧版本海报到 history/poster/{archived_at}/")

        log("开始调用生图API（按次收费）...")
        output_path = generate_poster(project_id, confirm=False)
        log(f"海报已生成: {output_path}")
        return {"output_path": str(output_path)}

    trigger("generate_poster", project_id, work)


def trigger_generate_daily_poster(project_id: str, day: int) -> None:
    """触发指定Day的单日海报生成，和8天整体海报使用独立产物与历史目录。"""
    if day < 1 or day > 8:
        raise ValueError("单日海报只支持Day1到Day8")
    task_type = "generate_poster_daily"
    task_key = f"{project_id}:{day}"

    def work(log: Callable[[str], None]) -> dict:
        from scripts.delivery.run_generate_poster import generate_poster

        stem = f"poster-day{day}"
        archived_at = _archive_previous_output(
            project_id,
            stem,
            [f"{stem}.png", f"{stem}_prompt.txt"],
        )
        if archived_at:
            log(f"已归档旧版本Day{day}海报到 history/{stem}/{archived_at}/")

        log(f"开始生成Day{day}单日海报（调用生图API，按次收费）...")
        output_path = generate_poster(project_id, confirm=False, scope="day", day=day)
        log(f"Day{day}海报已生成: {output_path}")
        return {"output_path": str(output_path), "day": day}

    trigger(task_type, task_key, work)
