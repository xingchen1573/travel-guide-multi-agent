"""公共区域统一读写接口。

所有agent对 projects/<project_id>/workspace/ 下文件的读写都必须经过本模块，
不允许agent直接操作文件系统。职责：
1. 按 docs/AGENTS.md 第0节的权限矩阵，校验调用方(actor)是否有权写目标文件/字段。
2. 每次写操作后自动追加一条记录到 workspace/changelog.jsonl。
3. 提供 read_workspace / write_route_plan / write_agent_output / write_final_guide /
   report_conflict / update_progress 等函数，供 tools/ 里的工具函数调用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import fcntl

from travel_guide_kernel.paths import resolve_runtime_root
from travel_guide_kernel.schema import (
    LIFESTYLE_FIELDS,
    ConflictRecord,
    CultureOutput,
    FinalGuideBlocks,
    LifestyleCategory,
    LifestyleItem,
    FinalGuideMeta,
    LifestyleOutput,
    PitfallCategory,
    PitfallItem,
    PitfallOutput,
    PoiDetailFile,
    PoiDetailManifest,
    Progress,
    ProjectPhase,
    RouteDay,
    RouteEntities,
    RouteExecutionPlan,
    RouteFeasibilityReport,
    RouteOption,
    RoutePlan,
    RouteSegment,
    UserNote,
)
from travel_guide_kernel.route import route_structure_snapshot, validate_route_structure
from travel_guide_kernel.final_guide_sections import (
    compose_final_guide_sections,
    contract_body_markdown,
    normalize_final_guide_section_order,
    split_final_guide_sections,
)
from travel_guide_kernel.final_guide_blocks import (
    collect_upstream_claims,
    prepare_final_guide_blocks,
    render_final_guide_body,
    rendered_body_sha256,
)

_PACKAGE_PARENT = Path(__file__).resolve().parent.parent
_DEFAULT_RUNTIME_ROOT = _PACKAGE_PARENT if (_PACKAGE_PARENT / "src").is_dir() else Path.cwd()
RUNTIME_ROOT = resolve_runtime_root(_DEFAULT_RUNTIME_ROOT)
PROJECTS_DIR = RUNTIME_ROOT / "projects"
MATERIAL_LIBRARY_DIR = RUNTIME_ROOT / "data" / "material_library"

MAX_RETRY = 2
_EVIDENCE_CHECKS_LOCK = threading.Lock()
_PROJECT_RUN_LOCKS_GUARD = threading.Lock()
_PROJECT_RUN_LOCKS: dict[str, threading.Lock] = {}


class ProjectRunAlreadyActiveError(RuntimeError):
    """Raised when another process is already mutating the same Project plan."""

# --- 权限矩阵：谁能写什么，对应 docs/AGENTS.md 第0节 ---
WRITE_PERMISSIONS: dict[str, set[str]] = {
    "route_plan": {"route_agent"},
    "route_entities": {"route_agent"},
    "route_execution": {"route_agent"},
    "route_change_proposal": {"route_agent"},
    "route_change_apply": {"main_agent"},
    # 路线可执行性报告由系统核验步骤写，不允许Route Agent自行判定自己的路线通过。
    "route_feasibility": {"citation_checker"},
    # realtime_agent只有在陪伴阶段patch经主agent/用户审批通过后才会真正调用这个写入
    # （src/orchestrator.py的await_realtime_approval_node），不是它能直接决定的，
    # 见docs/ORCHESTRATION.md第3.2节。
    "lifestyle_output": {"lifestyle_agent", "realtime_agent"},
    # 避雷agent的产出，见docs/ARCHITECTURE.md第7节
    "pitfall_output": {"pitfall_agent"},
    # 文化科普agent的产出，见docs/AGENTS.md第8节
    "culture_output": {"culture_agent"},
    "final_guide": {"integration_agent"},
    # citation_checker：orchestrator里的系统级引用核验步骤（src/materials/citation_check.py），
    # 不是某个agent的LLM自己判断要不要report_conflict，是确定性代码+LLM判定的复合校验，见docs/ISSUES.md
    "conflict_report": {"integration_agent", "citation_checker"},
    "conflict_update": {"main_agent"},
    "progress": {"main_agent"},
    "core_material": {"material_agent"},
    # POI详情agent只写自己负责的那一个文件（并行多实例各写各的），见docs/WORKSPACE.md第3节
    "poi_detail": {"poi_detail_agent"},
    # manifest.json由join节点统一写（纯程序拼接，不是某个agent实例的产出），避免并发写同一
    # 文件的竞态——这里的actor是"main_agent"（编排层系统写入），不是"poi_detail_agent"
    "poi_details_manifest": {"main_agent"},
    # 完成后的路线edit可能让既有POI详情失效；由编排层把失效JSON移出活动目录，保留到
    # _stale/供审计和人工恢复，不允许业务agent自行删除/归档别人的产出。
    "poi_detail_archive": {"main_agent"},
    # 管理后台"POI图片"页面人工上传/删除真实照片——actor是人类（通过后台API发起），
    # 不是任何一个react-agent，"admin_user"是本项目第一个非agent的写入身份，见
    # docs/ARCHITECTURE.md第13.2节。这打破了此前"管理后台只读+唯一写操作是彻底重跑"的
    # 架构约定，是这次功能明确要做的事，不是意外放宽权限。
    "poi_photo": {"admin_user"},
    # 用户备注/annotation layer（docs/ARCHITECTURE.md第21节）——admin_user的第二个
    # resource_type，跟poi_photo同款不加鉴权。阶段1只有网页表单入口，实际写入身份
    # 仍统一记admin_user（不是"web_user"这类未来才会出现的身份）。
    "user_note": {"admin_user"},
}


class PermissionError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(action: str, actor: str) -> None:
    allowed = WRITE_PERMISSIONS.get(action, set())
    if actor not in allowed:
        raise PermissionError(
            f"actor={actor!r} 无权执行 {action!r}，允许的actor: {sorted(allowed)}"
        )


def _project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def _workspace_dir(project_id: str) -> Path:
    return _project_dir(project_id) / "workspace"


def workspace_dir(project_id: str) -> Path:
    """公开的workspace目录访问入口，供run脚本等展示文件路径用。"""
    return _workspace_dir(project_id)


def project_dir(project_id: str) -> Path:
    """公开的project根目录访问入口（workspace/的上一级，同级还有materials/、agents/），
    供管理后台API这类需要在project目录下新建其他子目录（如backups/）的调用方使用。"""
    return _project_dir(project_id)


def list_project_ids() -> list[str]:
    """列出PROJECTS_DIR下所有project_id，供管理后台的project列表接口使用。排除
    TRASH_DIR_NAME本身（回收站目录也是PROJECTS_DIR的直接子目录，不排除会被误当成
    一个名叫"_trash"的project）——回收站里的project_id在更深一层（PROJECTS_DIR/_trash/<id>/），
    不会被这层浅层遍历意外收录，只有"_trash"这个目录名本身需要显式排除。"""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(
        p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and p.name != TRASH_DIR_NAME
    )


# ---------- 回收站：软删除/恢复/永久删除 ----------

TRASH_DIR_NAME = "_trash"


def _trash_dir() -> Path:
    return PROJECTS_DIR / TRASH_DIR_NAME


def trashed_project_dir(project_id: str) -> Path:
    """公开的回收站内project目录访问入口，平行于project_dir()——供管理后台回收站
    列表接口读取已软删除project的requirements.json/progress.json等文件使用。"""
    return _trash_dir() / project_id


def is_trashed(project_id: str) -> bool:
    return trashed_project_dir(project_id).is_dir()


def list_trashed_project_ids() -> list[str]:
    """列出回收站里所有project_id，不存在_trash/目录时返回空列表。"""
    trash_dir = _trash_dir()
    if not trash_dir.exists():
        return []
    return sorted(p.name for p in trash_dir.iterdir() if p.is_dir())


def soft_delete_project(project_id: str) -> None:
    """把PROJECTS_DIR/<project_id>整个移到PROJECTS_DIR/_trash/<project_id>——语义是
    "移入回收区"，不是真的删除，管理后台回收站页面可以恢复。调用方（API层）负责先检查
    没有正在运行的任务（重跑/生成网站/生成海报）再调用这个函数。"""
    if project_id not in list_project_ids():
        raise FileNotFoundError(f"project {project_id!r} 不存在，无法删除")
    if is_trashed(project_id):
        raise ValueError(f"project {project_id!r} 已经在回收站里")
    trash_dir = _trash_dir()
    trash_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(_project_dir(project_id)), str(trashed_project_dir(project_id)))


def restore_project(project_id: str) -> None:
    """把PROJECTS_DIR/_trash/<project_id>移回PROJECTS_DIR/<project_id>。项目ID是
    由对话/编排流程生成的确定性命名（如feishu-{chat_id}），管理后台没有"用指定id
    新建project"的入口，正常情况下不会出现命名冲突——这里仍然防御性检查一次，
    避免万一冲突时静默覆盖已存在的project。"""
    if not is_trashed(project_id):
        raise FileNotFoundError(f"project {project_id!r} 不在回收站里，无法恢复")
    if project_id in list_project_ids():
        raise ValueError(f"project {project_id!r} 已经存在，无法恢复（命名冲突）")
    shutil.move(str(trashed_project_dir(project_id)), str(_project_dir(project_id)))


def permanently_delete_project(project_id: str) -> None:
    """彻底删除回收站里的project目录，不可恢复。"""
    if not is_trashed(project_id):
        raise FileNotFoundError(f"project {project_id!r} 不在回收站里，无法永久删除")
    shutil.rmtree(trashed_project_dir(project_id))


def _changelog_path(project_id: str) -> Path:
    return _workspace_dir(project_id) / "changelog.jsonl"


def log_event(project_id: str, actor: str, action: str, target: str, summary: str = "") -> None:
    """记一条不伴随文件写入的独立事件（如骨架确认收到无法识别的回复、重新追问）——
    跟其余写操作自动触发的changelog记录不同，这类事件没有对应的文件状态变化，
    但仍需要留痕供后续观察频率，见docs/ARCHITECTURE.md第19.1节。"""
    _append_changelog(project_id, actor, action, target, summary)


def _append_changelog(project_id: str, actor: str, action: str, target: str, summary: str = "") -> None:
    entry = {
        "timestamp": _now(),
        "actor": actor,
        "action": action,
        "target": target,
        "summary": summary,
    }
    path = _changelog_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_json_atomic(path: Path, data: dict) -> None:
    """Replace a JSON file atomically so concurrent readers never see a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


@contextmanager
def _evidence_checks_file_lock(project_id: str):
    """Serialize Evidence Checks read-modify-write cycles across agent processes."""
    lock_path = _workspace_dir(project_id) / ".evidence_checks.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _poi_status_file_lock(project_id: str):
    """Serialize the POI-detail/manifest terminal-state pair.

    The two JSON files cannot be replaced in one filesystem operation, so the
    lock plus idempotent writes form the transaction boundary used by both the
    generation join and the deterministic review repair.
    """
    lock_path = _workspace_dir(project_id) / ".poi_status.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------- 项目初始化 ----------


def init_project(project_id: str) -> None:
    ws = _workspace_dir(project_id)
    (ws / "outputs").mkdir(parents=True, exist_ok=True)
    (_project_dir(project_id) / "materials").mkdir(parents=True, exist_ok=True)
    (_project_dir(project_id) / "agents").mkdir(parents=True, exist_ok=True)

    progress_path = ws / "progress.json"
    if not progress_path.exists():
        progress = Progress(state=ProjectPhase.REQUIREMENTS, updated_at=_now())
        _write_json(progress_path, progress.model_dump(mode="json"))


def write_requirements(project_id: str, topic: str, requirements: dict) -> None:
    """持久化本次plan的topic/requirements，供edit模式（run_edit）后续复用，
    不用要求用户每次edit都重新填一遍完整需求。不属于agent可写工具，只在run_plan里调用，
    因此不经过_check权限校验（跟init_project一样是orchestrator内部的系统级写入）。"""
    path = _workspace_dir(project_id) / "requirements.json"
    _write_json(path, {"topic": topic, "requirements": requirements, "updated_at": _now()})


def read_requirements(project_id: str) -> Optional[dict]:
    return _read_json(_workspace_dir(project_id) / "requirements.json")


def write_fixed_route_input_snapshot(project_id: str, snapshot: dict) -> None:
    """Persist the user-visible fixed-route input without interpreting it."""
    if not isinstance(snapshot, dict):
        raise ValueError("fixed route input snapshot必须是JSON对象")
    _write_json(_workspace_dir(project_id) / "fixed_route_input_snapshot.json", snapshot)
    _append_changelog(
        project_id,
        "main_agent",
        "write",
        "fixed_route_input_snapshot.json",
        "写入固定路线原始输入快照",
    )


def write_material_baseline(project_id: str, baseline: dict) -> None:
    """Persist the immutable material and Prompt denominator for a formal run."""
    if not isinstance(baseline, dict):
        raise ValueError("material baseline必须是JSON对象")
    _write_json(_workspace_dir(project_id) / "material_baseline.json", baseline)
    _append_changelog(
        project_id,
        "main_agent",
        "write",
        "material_baseline.json",
        "写入素材与Prompt固定基线",
    )


# ---------- 通用读（所有agent可用） ----------


def read_workspace(project_id: str, relative_path: str) -> Optional[dict | str | list]:
    path = _workspace_dir(project_id) / relative_path
    if not path.exists():
        return None
    if path.is_dir():
        # agent有时会传目录路径（比如"outputs"而不是"outputs/lifestyle.json"），
        # 之前直接read_text会在Windows上报PermissionError，改成列出目录内容更友好
        return [p.name for p in sorted(path.iterdir())]
    if path.suffix == ".json":
        return _read_json(path)
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return path.read_text(encoding="utf-8")


@contextmanager
def _research_runs_file_lock(project_id: str):
    lock_path = _workspace_dir(project_id) / ".research_runs.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def append_research_run(project_id: str, entry: dict) -> None:
    """Append one service-authored research observation across Agent processes."""
    path = _workspace_dir(project_id) / "research_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _research_runs_file_lock(project_id):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def read_research_runs(project_id: str) -> list[dict]:
    data = read_workspace(project_id, "research_runs.jsonl")
    return data if isinstance(data, list) else []


def project_material_library_dir(project_id: str) -> Path:
    """Project-local overlay used by runtime material supplementation."""
    return _project_dir(project_id) / "materials" / "library"


def read_evidence_checks(project_id: str) -> dict:
    """Read the checker-owned explanation projection for the current project."""
    data = _read_json(_workspace_dir(project_id) / "evidence_checks.json")
    if data:
        data.setdefault("history", [])
        return data
    return {
        "version": 2,
        "updated_at": "",
        "route": [],
        "lifestyle": [],
        "pitfall": [],
        "poi_detail": [],
        "history": [],
    }


def read_validation_runs(project_id: str) -> list[dict]:
    """Read append-only stage validation outcomes used by recovery planning."""
    value = read_workspace(project_id, "validation_runs.jsonl")
    return [item for item in (value or []) if isinstance(item, dict)]


def append_validation_run(project_id: str, record: dict) -> dict:
    """Persist one stage-level validation result without replacing claim evidence."""
    if not isinstance(record, dict):
        raise ValueError("validation run必须是JSON对象")
    required = {"stage", "scope", "observation_mode", "status", "artifact_sha256"}
    missing = sorted(required - set(record))
    if missing:
        raise ValueError(f"validation run缺少字段: {missing}")
    entry = {
        "schema_version": "stage-validation-v1",
        "checked_at": _now(),
        **record,
    }
    path = _workspace_dir(project_id) / "validation_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _append_changelog(
        project_id,
        "citation_checker",
        "append",
        "validation_runs.jsonl",
        f"{entry['stage']}:{entry['scope']}={entry['status']}",
    )
    return entry


def write_route_map_validation(project_id: str, report: dict) -> None:
    """Write the checker-owned entity, distance, anomaly, and schedule report."""
    if not isinstance(report, dict):
        raise ValueError("route map validation必须是JSON对象")
    _write_json(_workspace_dir(project_id) / "route_map_validation.json", report)
    _append_changelog(
        project_id,
        "citation_checker",
        "write",
        "route_map_validation.json",
        f"地图路线校验={report.get('status', 'unknown')}",
    )


def _route_structure_hash(route_plan: dict) -> str:
    snapshot = route_structure_snapshot(route_plan)
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_route_entities(
    project_id: str,
    actor: str,
    route_entities: RouteEntities,
) -> RouteEntities:
    """Persist canonical provider identities bound to the current route structure."""
    _check("route_entities", actor)
    route_plan = read_workspace(project_id, "route_plan.json")
    if not isinstance(route_plan, dict):
        raise ValueError("route_plan.json不存在，不能写入路线实体")
    current_hash = _route_structure_hash(route_plan)
    if route_entities.route_plan_structure_hash and route_entities.route_plan_structure_hash != current_hash:
        raise ValueError("route_entities对应的路线结构已经过期")

    days = {
        int(day.get("day") or 0): day
        for day in route_plan.get("days") or []
        if isinstance(day, dict)
    }
    seen: set[tuple[int, int]] = set()
    for entity in route_entities.entities:
        key = (entity.day, entity.poi_index)
        if key in seen:
            raise ValueError(f"route_entities重复提交Day{entity.day}.pois.{entity.poi_index}")
        seen.add(key)
        day = days.get(entity.day)
        pois = day.get("pois") or [] if day else []
        if entity.poi_index < 0 or entity.poi_index >= len(pois):
            raise ValueError(f"route_entities引用不存在的Day{entity.day}.pois.{entity.poi_index}")
        poi = pois[entity.poi_index]
        formal_name = str(poi.get("name") if isinstance(poi, dict) else poi).strip()
        if entity.formal_name != formal_name:
            raise ValueError(
                f"route_entities名称与正式路线不一致: Day{entity.day}.pois.{entity.poi_index}="
                f"{formal_name!r}, submitted={entity.formal_name!r}"
            )
        if entity.status == "resolved" and (
            not entity.provider_id or entity.location is None
        ):
            raise ValueError("resolved路线实体必须包含provider_id和location")

    route_entities.route_plan_structure_hash = current_hash
    route_entities.updated_at = _now()
    _write_json(
        _workspace_dir(project_id) / "route_entities.json",
        route_entities.model_dump(mode="json"),
    )
    _append_changelog(
        project_id,
        actor,
        "write",
        "route_entities.json",
        f"更新路线实体: {len(route_entities.entities)}项",
    )
    return route_entities


def read_route_entities(project_id: str) -> Optional[RouteEntities]:
    data = _read_json(_workspace_dir(project_id) / "route_entities.json")
    return RouteEntities.model_validate(data) if data else None


def write_route_execution(
    project_id: str,
    actor: str,
    execution: RouteExecutionPlan,
) -> RouteExecutionPlan:
    """Persist route leg modes and cross-day operational commitments."""
    _check("route_execution", actor)
    route_plan = read_workspace(project_id, "route_plan.json")
    if not isinstance(route_plan, dict):
        raise ValueError("route_plan.json不存在，不能写入路线执行计划")
    current_hash = _route_structure_hash(route_plan)
    if execution.route_plan_structure_hash and execution.route_plan_structure_hash != current_hash:
        raise ValueError("route_execution对应的路线结构已经过期")

    days = {
        int(day.get("day") or 0): day
        for day in route_plan.get("days") or []
        if isinstance(day, dict)
    }
    seen_legs: set[tuple[int, int, int]] = set()
    for leg in execution.route_legs:
        key = (leg.day, leg.from_poi_index, leg.to_poi_index)
        if key in seen_legs:
            raise ValueError(f"route_execution重复提交路段{key}")
        seen_legs.add(key)
        day = days.get(leg.day)
        pois = day.get("pois") or [] if day else []
        if (
            leg.from_poi_index < 0
            or leg.to_poi_index < 0
            or leg.from_poi_index >= len(pois)
            or leg.to_poi_index >= len(pois)
        ):
            raise ValueError(f"route_execution路段引用不存在的POI: {key}")
        from_name = str(pois[leg.from_poi_index].get("name") or "")
        to_name = str(pois[leg.to_poi_index].get("name") or "")
        if leg.from_name and leg.from_name != from_name:
            raise ValueError(f"route_execution路段起点名称不一致: {leg.from_name!r} != {from_name!r}")
        if leg.to_name and leg.to_name != to_name:
            raise ValueError(f"route_execution路段终点名称不一致: {leg.to_name!r} != {to_name!r}")
        leg.from_name = from_name
        leg.to_name = to_name
        if leg.transport_mode == "mixed" and not leg.segments:
            raise ValueError("mixed路段必须提交segments")

    seen_commitments: set[str] = set()
    for index, commitment in enumerate(execution.operational_commitments):
        day = days.get(commitment.target_day)
        pois = day.get("pois") or [] if day else []
        if commitment.target_poi_index < 0 or commitment.target_poi_index >= len(pois):
            raise ValueError(
                f"operational_commitment引用不存在的Day{commitment.target_day}."
                f"pois.{commitment.target_poi_index}"
            )
        target_name = str(pois[commitment.target_poi_index].get("name") or "")
        if commitment.target_name and commitment.target_name != target_name:
            raise ValueError(
                f"operational_commitment目标名称不一致: {commitment.target_name!r} != {target_name!r}"
            )
        commitment.target_name = target_name
        if not commitment.commitment_id:
            seed = (
                f"{commitment.action}|{commitment.category}|{commitment.target_day}|"
                f"{commitment.target_poi_index}|{','.join(commitment.objects)}|{index}"
            )
            commitment.commitment_id = "route-commitment-" + hashlib.sha1(
                seed.encode("utf-8")
            ).hexdigest()[:16]
        if commitment.commitment_id in seen_commitments:
            raise ValueError(f"operational_commitment ID重复: {commitment.commitment_id}")
        seen_commitments.add(commitment.commitment_id)

    execution.route_plan_structure_hash = current_hash
    execution.updated_at = _now()
    _write_json(
        _workspace_dir(project_id) / "route_execution.json",
        execution.model_dump(mode="json"),
    )
    _append_changelog(
        project_id,
        actor,
        "write",
        "route_execution.json",
        f"更新路线执行计划: {len(execution.route_legs)}路段, "
        f"{len(execution.operational_commitments)}项操作",
    )
    return execution


def read_route_execution(project_id: str) -> Optional[RouteExecutionPlan]:
    data = _read_json(_workspace_dir(project_id) / "route_execution.json")
    return RouteExecutionPlan.model_validate(data) if data else None


def write_experiment_manifest(project_id: str, manifest: dict) -> None:
    """Persist project-local experiment registration consumed by the indexer."""
    required = {"scenario_id", "experiment_version", "harness"}
    missing = sorted(required - set(manifest))
    if missing:
        raise ValueError(f"experiment manifest缺少字段: {missing}")
    payload = {
        "schema_version": "experiment-manifest-v1",
        "project_id": project_id,
        **manifest,
        "updated_at": _now(),
    }
    _write_json(_workspace_dir(project_id) / "experiment.json", payload)
    _append_changelog(
        project_id,
        "main_agent",
        "write",
        "experiment.json",
        f"登记实验: {payload['experiment_version']}",
    )


def append_post_run_correction(project_id: str, correction: dict) -> dict:
    """Append an audit correction without mutating the original stage artifact."""
    if not isinstance(correction, dict) or not correction.get("correction_type"):
        raise ValueError("post-run correction必须包含correction_type")
    entry = {
        "schema_version": "post-run-correction-v1",
        "recorded_at": _now(),
        **correction,
    }
    path = _workspace_dir(project_id) / "post_run_corrections.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _append_changelog(
        project_id,
        "main_agent",
        "append",
        "post_run_corrections.jsonl",
        str(entry.get("summary") or entry["correction_type"]),
    )
    return entry


def write_evidence_checks(
    project_id: str,
    section: str,
    checks: list[dict],
    *,
    replace_prefixes: Optional[list[str]] = None,
    archive_all_replaced: bool = False,
    archive_reason: str = "",
) -> dict:
    """Persist all checker outcomes without changing the existing issue workflow.

    Scoped edits replace only matching target keys, so rechecking one day or one
    lifestyle field does not erase explanation records for untouched outputs.
    """
    if section not in {"route", "lifestyle", "pitfall", "poi_detail"}:
        raise ValueError(f"unknown evidence check section: {section}")
    if replace_prefixes == []:
        return read_evidence_checks(project_id)

    path = _workspace_dir(project_id) / "evidence_checks.json"
    with _EVIDENCE_CHECKS_LOCK:
        with _evidence_checks_file_lock(project_id):
            data = read_evidence_checks(project_id)
            current = list(data.get(section) or [])
            if replace_prefixes is None:
                retained: list[dict] = []
            else:
                def is_replaced(item: dict) -> bool:
                    target_key = str(item.get("target_key") or "")
                    return any(
                        target_key == prefix
                        or target_key.startswith(
                            prefix if prefix.endswith(":") else f"{prefix}:"
                        )
                        for prefix in replace_prefixes
                    )

                retained = [item for item in current if not is_replaced(item)]

            replaced = [item for item in current if is_replaced(item)] if replace_prefixes is not None else current
            failed_target_keys = {
                str(item.get("target_key") or "")
                for item in replaced
                if item.get("result") in {"fail", "error", "inconclusive"}
            }
            archived_checks = (
                replaced
                if archive_all_replaced
                else [
                    item for item in replaced
                    if str(item.get("target_key") or "") in failed_target_keys
                ]
            )
            if archived_checks:
                history = list(data.get("history") or [])
                history_entry = {
                    "section": section,
                    "archived_at": _now(),
                    "replace_prefixes": list(replace_prefixes or []),
                    "checks": archived_checks,
                }
                if archive_reason:
                    history_entry["reason"] = archive_reason
                history.append(history_entry)
                data["history"] = history[-100:]

            data[section] = sorted(
                retained + checks,
                key=lambda item: (
                    str(item.get("target_key") or ""),
                    str(item.get("scope") or ""),
                ),
            )
            data["version"] = 2
            data["updated_at"] = _now()
            _write_json_atomic(path, data)
            return data


# ---------- progress ----------


def read_progress(project_id: str) -> Progress:
    data = _read_json(_workspace_dir(project_id) / "progress.json")
    if data is None:
        return Progress()
    return Progress.model_validate(data)


def update_progress(project_id: str, actor: str, **updates: Any) -> Progress:
    _check("progress", actor)
    progress = read_progress(project_id)
    data = progress.model_dump()
    data.update(updates)
    data["updated_at"] = _now()
    new_progress = Progress.model_validate(data)
    _write_json(_workspace_dir(project_id) / "progress.json", new_progress.model_dump(mode="json"))
    _append_changelog(project_id, actor, "write", "progress.json", f"state -> {new_progress.state.value}")
    return new_progress


# ---------- route_plan ----------


def write_route_plan(project_id: str, actor: str, route_plan: RoutePlan) -> None:
    """整份覆盖写——用于骨架首次生成、以及涉及增删/重排天数的结构性调整（用户要求"加一天"/
    "减少一天"这类场景）。这类调整天然需要模型看到整体结构才能判断插入位置/重新排序，
    不适合按天patch表达，所以保留这条整份写入路径，跟write_route_plan_patch并存
    （方案讨论见docs/ISSUES.md第112条，跟lifestyle/pitfall"只有patch、不支持删除"的
    选择不同——route现有代码已经把增删天数当成合法操作，见_changed_route_days的注释）。
    """
    _check("route_plan", actor)
    previous = read_route_plan(project_id)
    route_plan.updated_at = _now()
    if not route_plan.created_at:
        route_plan.created_at = route_plan.updated_at
    _write_json(_workspace_dir(project_id) / "route_plan.json", route_plan.model_dump(mode="json"))
    if previous is not None and previous.days != route_plan.days:
        write_evidence_checks(
            project_id,
            "route",
            [],
            replace_prefixes=["route:"],
            archive_all_replaced=True,
            archive_reason="route_plan_replaced",
        )
    _append_changelog(project_id, actor, "write", "route_plan.json", "更新路线规划")


def write_route_plan_patch(project_id: str, actor: str, days: list[dict], amap_verified: Optional[bool] = None) -> RoutePlan:
    """按天增量合并写入route_plan——agent只需要提交本次新增/修改的那几天，未提交的天数
    保持store里已有内容不变，不要求agent原样抄写未改动天数。跟write_agent_output_patch
    （lifestyle版本，见docs/ISSUES.md第110条）同一个模式，区别是route_plan.json只有一级
    "天"坐标，不是lifestyle那种(field, day)两级坐标，所以这里直接用day数字做key，
    不需要touched_fields/last_updated_field这类多字段场景才需要的概念。

    只适用于"改某天内容"这类场景——不支持通过这个函数增删天数（提交一个新day编号会被
    当成"新增这一天"接受，但如果是用户明确要求"加一天"/"减少一天"，应该用整份覆盖的
    write_route_plan，因为这类结构性调整通常伴随天数重排/编号整体变化，模型需要看到
    完整上下文才能正确处理，见docs/ISSUES.md第112条）。

    amap_verified留空（None）时保留原值不变；显式传True/False才会更新这个全局标志——
    这是因为它是RoutePlan级别的单一flag，不是按天维度的属性，patch某一天的内容不应该
    意外影响这个标志的既有状态。
    """
    _check("route_plan", actor)
    current = read_route_plan(project_id) or RoutePlan()
    by_day: dict[int, RouteDay] = {d.day: d for d in current.days}
    previous_by_day: dict[int, RouteDay] = dict(by_day)
    touched_days: list[int] = []
    for raw in days:
        day = RouteDay.model_validate(raw)
        by_day[day.day] = day
        touched_days.append(day.day)

    route_plan = RoutePlan(
        days=[by_day[d] for d in sorted(by_day)],
        amap_verified=current.amap_verified if amap_verified is None else amap_verified,
        created_at=current.created_at,
    )
    route_plan.updated_at = _now()
    if not route_plan.created_at:
        route_plan.created_at = route_plan.updated_at
    _write_json(_workspace_dir(project_id) / "route_plan.json", route_plan.model_dump(mode="json"))
    changed_days = sorted(
        day
        for day in set(touched_days)
        if previous_by_day.get(day) != by_day.get(day)
    )
    if changed_days:
        write_evidence_checks(
            project_id,
            "route",
            [],
            replace_prefixes=[f"route:day:{day}" for day in changed_days],
            archive_all_replaced=True,
            archive_reason="route_day_replaced",
        )
    _append_changelog(
        project_id, actor, "write", "route_plan.json", f"更新天数: {','.join(str(d) for d in sorted(touched_days))}"
    )
    return route_plan


def write_route_facts_patch(
    project_id: str,
    actor: str,
    day: int,
    *,
    notes: Optional[str] = None,
    sources: Optional[list[dict]] = None,
    claims: Optional[list[dict]] = None,
) -> RoutePlan:
    """Update only factual/citation fields of one existing route day.

    This is the only route-plan writer exposed to citation/timeliness repair
    instances. Structural fields are absent from the function signature, so an
    automated evidence repair cannot rename/reorder POIs or add/remove days.
    """
    _check("route_plan", actor)
    if notes is None and sources is None and claims is None:
        raise ValueError("notes、sources和claims不能同时为空")
    current = read_route_plan(project_id)
    if current is None:
        raise ValueError("route_plan.json不存在，无法做事实修复")

    by_day = {item.day: item for item in current.days}
    if day not in by_day:
        raise ValueError(f"Day{day}不存在，事实修复不能新增天数")
    original_day = by_day[day]
    data = original_day.model_dump(mode="json")
    if notes is not None and data.get("claims") and claims is None:
        raise ValueError("Day已启用事实级证据链；修改notes时必须同步提交claims，避免证据链过期")
    if notes is not None:
        data["notes"] = notes
    if sources is not None:
        data["sources"] = sources
    if claims is not None:
        data["claims"] = claims
    by_day[day] = RouteDay.model_validate(data)

    route_plan = RoutePlan(
        days=[by_day[item.day] for item in current.days],
        amap_verified=current.amap_verified,
        created_at=current.created_at,
    )
    route_plan.updated_at = _now()
    _write_json(_workspace_dir(project_id) / "route_plan.json", route_plan.model_dump(mode="json"))
    if by_day[day] != original_day:
        write_evidence_checks(
            project_id,
            "route",
            [],
            replace_prefixes=[f"route:day:{day}"],
            archive_all_replaced=True,
            archive_reason="route_facts_changed",
        )
    changed_fields = [
        name
        for name, value in (("notes", notes), ("sources", sources), ("claims", claims))
        if value is not None
    ]
    _append_changelog(
        project_id,
        actor,
        "write",
        "route_plan.json",
        f"事实修复Day{day}: {','.join(changed_fields)}",
    )
    return route_plan


def _route_change_proposal_path(project_id: str) -> Path:
    return _workspace_dir(project_id) / "route_change_proposal.json"


def write_route_change_proposal(
    project_id: str,
    actor: str,
    *,
    changed_days: list[dict],
    trigger: str,
    reason: str,
    evidence: str = "",
    work_unit_id: str = "",
) -> dict:
    """Persist a structural route candidate without changing route_plan.json."""
    _check("route_change_proposal", actor)
    if not changed_days:
        raise ValueError("changed_days不能为空")
    current = read_route_plan(project_id)
    if current is None:
        raise ValueError("route_plan.json不存在，无法生成结构调整提案")

    current_data = current.model_dump(mode="json")
    by_day = {item["day"]: item for item in current_data["days"]}
    normalized_changed_days = []
    seen_days: set[int] = set()
    for raw_day in changed_days:
        day = RouteDay.model_validate(raw_day).model_dump(mode="json")
        if day["day"] in seen_days:
            raise ValueError(f"changed_days不能重复提交Day{day['day']}")
        seen_days.add(day["day"])
        if day["day"] not in by_day:
            raise ValueError("结构提案当前只允许替换既有天数，不能静默新增天数")
        by_day[day["day"]] = day
        normalized_changed_days.append(day)
    candidate = {
        **current_data,
        "days": [by_day[item["day"]] for item in current_data["days"]],
    }
    structure_issues = validate_route_structure(candidate)
    if structure_issues:
        raise ValueError(f"结构提案本身不合法：{structure_issues}")

    now = _now()
    proposal = {
        "schema_version": "route-change-proposal-v1",
        "proposal_id": uuid.uuid4().hex,
        "work_unit_id": work_unit_id,
        "status": "pending",
        "trigger": trigger,
        "reason": reason,
        "evidence": evidence,
        "base_structure": route_structure_snapshot(current_data),
        "before_days": [
            item for item in current_data["days"] if item["day"] in {d["day"] for d in normalized_changed_days}
        ],
        "proposed_days": [by_day[d["day"]] for d in normalized_changed_days],
        "candidate_route_plan": candidate,
        "created_at": now,
        "decided_at": None,
    }
    _write_json(_route_change_proposal_path(project_id), proposal)
    _append_changelog(project_id, actor, "write", "route_change_proposal.json", f"结构调整提案：{trigger}")
    return proposal


def read_route_change_proposal(project_id: str) -> Optional[dict]:
    return _read_json(_route_change_proposal_path(project_id))


def decide_route_change_proposal(project_id: str, actor: str, decision: str) -> dict:
    """Apply or keep the current route proposal with stale-base protection."""
    _check("route_change_apply", actor)
    if decision not in {"apply", "keep", "supersede"}:
        raise ValueError("decision必须是apply、keep或supersede")
    proposal = read_route_change_proposal(project_id)
    if not proposal or proposal.get("status") != "pending":
        raise ValueError("没有待确认的路线结构调整提案")

    if decision == "apply":
        current = read_route_plan(project_id)
        current_data = current.model_dump(mode="json") if current else None
        if route_structure_snapshot(current_data) != proposal.get("base_structure"):
            raise ValueError("route_plan已在等待确认期间发生变化，不能应用过期提案")
        candidate = proposal.get("candidate_route_plan") or {}
        issues = validate_route_structure(candidate)
        if issues:
            raise ValueError(f"待应用的结构提案不合法：{issues}")
        route_plan = RoutePlan.model_validate(candidate)
        route_plan.updated_at = _now()
        _write_json(_workspace_dir(project_id) / "route_plan.json", route_plan.model_dump(mode="json"))
        _append_changelog(project_id, actor, "write", "route_plan.json", "用户确认应用路线结构调整提案")

    proposal["status"] = {
        "apply": "applied",
        "keep": "kept_original",
        "supersede": "superseded",
    }[decision]
    proposal["decided_at"] = _now()
    _write_json(_route_change_proposal_path(project_id), proposal)
    _append_changelog(
        project_id,
        actor,
        "route_change_decision",
        "route_change_proposal.json",
        proposal["status"],
    )
    return proposal


def read_route_plan(project_id: str) -> Optional[RoutePlan]:
    data = _read_json(_workspace_dir(project_id) / "route_plan.json")
    return RoutePlan.model_validate(data) if data else None


def write_route_feasibility(
    project_id: str,
    actor: str,
    report: RouteFeasibilityReport,
) -> RouteFeasibilityReport:
    _check("route_feasibility", actor)
    _write_json(
        _workspace_dir(project_id) / "route_feasibility.json",
        report.model_dump(mode="json"),
    )
    _append_changelog(
        project_id,
        actor,
        "write",
        "route_feasibility.json",
        f"路线可执行性核验: {report.overall_status}",
    )
    return report


def read_route_feasibility(project_id: str) -> Optional[RouteFeasibilityReport]:
    data = _read_json(_workspace_dir(project_id) / "route_feasibility.json")
    return RouteFeasibilityReport.model_validate(data) if data else None


# ---------- lifestyle output ----------


def write_agent_output_patch(project_id: str, actor: str, items: list[dict]) -> LifestyleOutput:
    """按(field, day)增量合并写入lifestyle产出——agent只需要提交本次新增/修改的条目，
    未提交的(field, day)组合保持store里已有内容不变，不要求agent原样抄写未改动部分。

    背景：旧版write_agent_output要求整份LifestyleOutput覆盖写入，edit模式下agent必须
    把当前完整产出inline进prompt看一遍再原样抄回未改动字段，8天场景inline内容可到50KB，
    且"要求模型抄不改的部分"本身就是误改风险来源（抄错/漏抄/顺手改动）。改成patch后，
    模型只需要提交它真正生成/修改的条目，物理上不需要看到、也不需要复制未改动内容。

    不支持删除语义——items里没有出现的(field, day)组合始终保留原值，没有办法通过这个
    函数删除某个已有条目（如果确实需要删除，属于后续独立需求，见方案讨论）。
    """
    _check("lifestyle_output", actor)
    current = read_lifestyle_output(project_id) or LifestyleOutput()
    by_field_day: dict[str, dict[int, LifestyleItem]] = {
        f: {it.day: it for it in getattr(current, f).items} for f in LIFESTYLE_FIELDS
    }
    touched_fields: set[str] = set()
    for raw in items:
        field = raw.get("field")
        if field not in LIFESTYLE_FIELDS:
            raise ValueError(f"未知字段: {field!r}，必须是 {LIFESTYLE_FIELDS} 之一")
        item = LifestyleItem.model_validate({k: v for k, v in raw.items() if k != "field"})
        by_field_day[field][item.day] = item
        touched_fields.add(field)

    output = LifestyleOutput(
        **{
            f: LifestyleCategory(
                items=[by_field_day[f][d] for d in sorted(by_field_day[f], key=lambda x: (x is None, x))]
            )
            for f in LIFESTYLE_FIELDS
        }
    )
    output.updated_at = _now()
    output.last_updated_field = ",".join(sorted(touched_fields))
    path = _workspace_dir(project_id) / "outputs" / "lifestyle.json"
    _write_json(path, output.model_dump(mode="json"))
    _append_changelog(
        project_id, actor, "write", "outputs/lifestyle.json", f"更新字段: {output.last_updated_field or '无'}"
    )
    return output


def read_lifestyle_output(project_id: str) -> Optional[LifestyleOutput]:
    data = _read_json(_workspace_dir(project_id) / "outputs" / "lifestyle.json")
    return LifestyleOutput.model_validate(data) if data else None


# ---------- pitfall output（避雷agent，见docs/WORKSPACE.md第2.3.2节） ----------


def write_pitfall_output_patch(project_id: str, actor: str, items: list[dict]) -> PitfallOutput:
    """按(field, day)增量合并写入避雷产出——跟write_agent_output_patch（lifestyle版本）
    完全同一个模式，见那边的完整原理说明（docs/ISSUES.md第110/111条）。

    跟lifestyle不同的一点：某个(field, day)确实没查到任何避雷内容是合法结果（warning
    留空即可），但这跟"这次patch没有提交这个(field, day)"是两回事——不提交就代表
    "这次调用不涉及这个组合"，store层保留它原有的值（可能是空warning，也可能是之前
    查到的具体内容），不会主动清空。首次生成时agent通常会对当天涉及的每个(field, day)
    组合都提交一条item（哪怕warning是空的），这样才能让"确实查过、没查到"和"这次
    压根没检索这个组合"在数据里区分开——这个区分本身是agent自己负责维护的语义，
    不是store层能替它判断的。"""
    _check("pitfall_output", actor)
    current = read_pitfall_output(project_id) or PitfallOutput()
    by_field_day: dict[str, dict[int, PitfallItem]] = {
        f: {it.day: it for it in getattr(current, f).items} for f in LIFESTYLE_FIELDS
    }
    touched_fields: set[str] = set()
    for raw in items:
        field = raw.get("field")
        if field not in LIFESTYLE_FIELDS:
            raise ValueError(f"未知字段: {field!r}，必须是 {LIFESTYLE_FIELDS} 之一")
        item = PitfallItem.model_validate(raw)
        by_field_day[field][item.day] = item
        touched_fields.add(field)

    output = PitfallOutput(
        **{
            f: PitfallCategory(
                items=[by_field_day[f][d] for d in sorted(by_field_day[f], key=lambda x: (x is None, x))]
            )
            for f in LIFESTYLE_FIELDS
        }
    )
    output.updated_at = _now()
    path = _workspace_dir(project_id) / "outputs" / "pitfall.json"
    _write_json(path, output.model_dump(mode="json"))
    _append_changelog(project_id, actor, "write", "outputs/pitfall.json", f"更新字段: {','.join(sorted(touched_fields)) or '无'}")
    return output


def read_pitfall_output(project_id: str) -> Optional[PitfallOutput]:
    data = _read_json(_workspace_dir(project_id) / "outputs" / "pitfall.json")
    return PitfallOutput.model_validate(data) if data else None


# ---------- culture output（文化科普agent，见docs/WORKSPACE.md第2.3.3节） ----------


def write_culture_output(project_id: str, actor: str, culture_output: CultureOutput) -> None:
    """整份覆盖写——culture_agent每次回答新问题时，负责把已有items读出来+追加新的一条
    再整份传回，这里不做服务端追加逻辑（跟lifestyle/pitfall那种按(field, day)增量patch
    的写入方式不同，culture是累加式历史记录，语义上必须整份覆盖）。"""
    _check("culture_output", actor)
    culture_output.updated_at = _now()
    path = _workspace_dir(project_id) / "outputs" / "culture.json"
    _write_json(path, culture_output.model_dump(mode="json"))
    _append_changelog(project_id, actor, "write", "outputs/culture.json", "新增一条文化科普问答")


def read_culture_output(project_id: str) -> Optional[CultureOutput]:
    data = _read_json(_workspace_dir(project_id) / "outputs" / "culture.json")
    return CultureOutput.model_validate(data) if data else None


# ---------- poi details（树木，见docs/WORKSPACE.md第2.3.1节） ----------


def poi_slug(poi_name: str) -> str:
    """把POI名称转成适合做文件名的slug。中文名称直接保留（文件系统支持，且比拼音转写
    更直观易读），只替换文件名不安全的字符；不引入拼音转写依赖，这不是本次范围要解决的问题。
    """
    cleaned = re.sub(r"[^\w一-鿿-]+", "_", poi_name.strip())
    return cleaned.strip("_") or "poi"


def _poi_detail_path(project_id: str, day: int, poi_name: str) -> Path:
    return _workspace_dir(project_id) / "outputs" / "poi_details" / f"day{day}_{poi_slug(poi_name)}.json"


def _clamp_single_recommended_route(poi_detail: PoiDetailFile) -> None:
    """多骨架备选场景（docs/ARCHITECTURE.md第3.4节第6点）：一个POI可能有多条routes，
    但只应该有一条recommended=True——poi_detail_agent（首次生成/edit入口切换推荐）都是
    LLM自主判断写入的，可能误标多条为true（比如"切换推荐"时只把新的一条设成true，忘了把
    原来的改回false）。这是可以确定性代码兜底的数据完整性问题，不依赖prompt自觉遵守：
    保留第一条recommended=True的路线，其余强制改为False。"零条true"是合法状态不处理
    （_sync_poi_details_into_final_guide会保留该语义，仅把第一条作为中性路线预览）。
    """
    seen_recommended = False
    for route in poi_detail.routes:
        if route.recommended:
            if seen_recommended:
                route.recommended = False
            else:
                seen_recommended = True


def write_poi_detail(project_id: str, actor: str, poi_detail: PoiDetailFile) -> str:
    """整份覆盖写——用于首次生成，以及涉及新增/删除路线这类结构性调整（这类调整需要模型
    对"这个POI到底该有几条路线"做整体判断，不适合按单条patch表达，跟write_route_plan
    "增删天数走整份覆盖"同一个理由，见write_poi_detail_patch的docstring）。返回相对
    workspace/outputs/poi_details/的文件名（供join节点写manifest用）。
    并行多实例各写各的文件（不同day/poi_slug对应不同路径），不存在并发写同一文件的竞态。"""
    _check("poi_detail", actor)
    poi_detail.updated_at = _now()
    if not poi_detail.created_at:
        poi_detail.created_at = poi_detail.updated_at
    _clamp_single_recommended_route(poi_detail)
    path = _poi_detail_path(project_id, poi_detail.day, poi_detail.poi_name)
    _write_json(path, poi_detail.model_dump(mode="json"))
    _append_changelog(
        project_id, actor, "write", f"outputs/poi_details/{path.name}", f"生成POI详情: {poi_detail.poi_name}"
    )
    return path.name


def write_poi_detail_patch(
    project_id: str,
    actor: str,
    day: int,
    poi_name: str,
    routes: list[dict],
    needs_detail_reason: Optional[str] = None,
) -> PoiDetailFile:
    """按route_name和segment.order层级合并写入单个POI详情文件。

    未提交的route_name保持原样；已有路线只更新本次显式提交的路线级字段，route_segments
    则按order原位替换或插入，未提交的途经点保持原样。这样引用核验只修一个途经点时，
    不需要让模型重新抄写整条路线。删除路线、删除途经点或整体重排仍应使用
    write_poi_detail整份覆盖，不能靠patch里省略对象表达删除。

    只适用于"改某条已有路线内容"/"切换推荐路线"这类场景——如果用户要求新增/删除整条
    路线，请改用整份覆盖的write_poi_detail：这类结构性调整通常伴随模型对"这个POI到底
    该有几条路线"的整体判断，不适合按单条patch表达，跟write_route_plan_patch不处理
    增删天数同一个理由（见docs/ISSUES.md相关记录）。跟route_plan_patch一致：提交一个
    此前不存在的route_name会被当成"新增这一条"接受，不做拦截，只是prompt引导模型
    优先用正确的工具。

    needs_detail_reason留空（None）时保留原值不变；显式传非None字符串（包括空字符串）
    才会更新——这是POI文件级标量字段，不属于任何一条route，patch某条路线内容不应该
    意外清空它。

    citation_status/auto_collected/retry_history这三个字段由核验流程
    （mark_poi_force_finalize）独立维护，不经过这个函数写入——合并时原样保留current
    现有值，不受这次patch影响。

    路线段的uploaded_photo_path由admin_user照片接口独立维护，模型patch无权修改；已有
    途经点无论是否提交该字段都保留旧值，新途经点则从空值开始。

    合并后仍然会跑_clamp_single_recommended_route（多条recommended=true时只保留
    第一条），校验对象是合并后的完整routes数组、不只是这次提交的子集——否则如果这次
    只提交了新recommended=true的那条、忘了把旧的改成false，只看子集发现不了旧路线
    仍然是true。

    这个POI详情文件此前不存在时抛ValueError——patch只能用于修改已有文件，首次生成
    必须用write_poi_detail。
    """
    _check("poi_detail", actor)
    current = read_poi_detail(project_id, day, poi_name)
    if current is None:
        raise ValueError(
            f"POI详情文件不存在（day={day}, poi_name={poi_name!r}），patch只能用于修改"
            f"已有文件——首次生成请用write_poi_detail（整份覆盖）。"
        )
    by_name: dict[str, RouteOption] = {r.route_name: r for r in current.routes}
    touched_names: list[str] = []
    touched_segments: list[str] = []
    for raw in routes:
        if not isinstance(raw, dict):
            raise ValueError("routes中的每一项必须是对象")
        route_name = str(raw.get("route_name") or "")
        existing = by_name.get(route_name)
        if existing is None:
            route = RouteOption.model_validate(raw)
            for segment in route.route_segments:
                segment.uploaded_photo_path = ""
            by_name[route.route_name] = route
            touched_names.append(route.route_name)
            touched_segments.extend(
                f"{route.route_name}#{segment.order}" for segment in route.route_segments
            )
            continue

        merged = existing.model_dump(mode="json")
        for field in ("suitable_for", "recommended", "recommend_reason"):
            if field in raw:
                merged[field] = raw[field]

        if "route_segments" in raw:
            raw_segments = raw.get("route_segments")
            if not isinstance(raw_segments, list):
                raise ValueError(
                    f"路线{route_name!r}的route_segments必须是数组；删除或重排请使用整份覆盖"
                )
            merged_segments = list(existing.route_segments)
            by_order: dict[int, int] = {}
            for index, segment in enumerate(merged_segments):
                if segment.order in by_order:
                    raise ValueError(
                        f"路线{route_name!r}存在重复order={segment.order}，无法安全执行局部patch"
                    )
                by_order[segment.order] = index

            submitted_orders: set[int] = set()
            for raw_segment in raw_segments:
                segment = RouteSegment.model_validate(raw_segment)
                if segment.order in submitted_orders:
                    raise ValueError(
                        f"路线{route_name!r}的patch重复提交order={segment.order}"
                    )
                submitted_orders.add(segment.order)
                touched_segments.append(f"{route_name}#{segment.order}")

                existing_index = by_order.get(segment.order)
                if existing_index is not None:
                    segment.uploaded_photo_path = merged_segments[
                        existing_index
                    ].uploaded_photo_path
                    merged_segments[existing_index] = segment
                    continue

                # 新途经点按order插入；省略已有途经点不表示删除。
                segment.uploaded_photo_path = ""
                insert_at = next(
                    (
                        index
                        for index, current_segment in enumerate(merged_segments)
                        if current_segment.order > segment.order
                    ),
                    len(merged_segments),
                )
                merged_segments.insert(insert_at, segment)
                by_order = {
                    current_segment.order: index
                    for index, current_segment in enumerate(merged_segments)
                }
            merged["route_segments"] = [
                segment.model_dump(mode="json") for segment in merged_segments
            ]

        route = RouteOption.model_validate(merged)
        by_name[route_name] = route
        touched_names.append(route_name)

    detail_data = current.model_dump(mode="json")
    detail_data["routes"] = [route.model_dump(mode="json") for route in by_name.values()]
    if needs_detail_reason is not None:
        detail_data["needs_detail_reason"] = needs_detail_reason
    poi_detail = PoiDetailFile.model_validate(detail_data)
    poi_detail.updated_at = _now()
    _clamp_single_recommended_route(poi_detail)
    path = _poi_detail_path(project_id, poi_detail.day, poi_detail.poi_name)
    _write_json(path, poi_detail.model_dump(mode="json"))
    _append_changelog(
        project_id, actor, "write", f"outputs/poi_details/{path.name}",
        f"更新路线: {','.join(touched_names) or '无'}"
        + (f"；途经点: {','.join(touched_segments)}" if touched_segments else ""),
    )
    return poi_detail


def read_poi_detail(project_id: str, day: int, poi_name: str) -> Optional[PoiDetailFile]:
    data = _read_json(_poi_detail_path(project_id, day, poi_name))
    return PoiDetailFile.model_validate(data) if data else None


def list_poi_details(
    project_id: str, *, include_unmanifested: bool = False
) -> list[PoiDetailFile]:
    """列出当前有效的POI详情。

    manifest存在时以``generated[].file``为权威活动集合；只有历史项目或生成中项目完全
    没有manifest时才回退到扫描活动目录。``include_unmanifested``只供路线edit后的
    reconciliation审计活动目录残留使用，普通消费端不应开启。
    """
    poi_dir = _workspace_dir(project_id) / "outputs" / "poi_details"
    if not poi_dir.exists():
        return []

    manifest = _read_json(poi_dir / "manifest.json")
    if isinstance(manifest, dict) and not include_unmanifested:
        details = []
        for entry in manifest.get("generated", []) or []:
            if not isinstance(entry, dict):
                continue
            filename = str(entry.get("file") or "")
            if not filename or Path(filename).name != filename:
                continue
            path = poi_dir / filename
            if path.is_file():
                details.append(
                    PoiDetailFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
                )
        return details

    details = []
    for path in sorted(poi_dir.glob("day*.json")):
        if path.name == "manifest.json":
            continue
        details.append(PoiDetailFile.model_validate(json.loads(path.read_text(encoding="utf-8"))))
    return details


def archive_poi_detail(project_id: str, actor: str, day: int, poi_name: str) -> Optional[str]:
    """把失效POI详情移出活动目录并返回相对workspace路径；源文件不存在时返回None。

    不直接删除是为了保留citation状态、历史路线以及人工上传照片路径等审计信息。归档目录
    位于``outputs/poi_details/_stale/``，不会被活动目录的``day*.json``扫描命中。
    """
    _check("poi_detail_archive", actor)
    source = _poi_detail_path(project_id, day, poi_name)
    if not source.exists():
        return None

    stale_dir = source.parent / "_stale"
    stale_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = stale_dir / f"{stamp}_{uuid.uuid4().hex[:8]}_{source.name}"
    shutil.move(str(source), str(destination))
    relative = destination.relative_to(_workspace_dir(project_id)).as_posix()
    _append_changelog(
        project_id,
        actor,
        "archive",
        f"outputs/poi_details/{source.name}",
        f"路线修改后POI详情失效，归档到 {relative}",
    )
    return relative


# ---------- outputs/poi_photos/day{N}_{poi_slug}/（管理后台人工上传，见docs/ISSUES.md） ----------


def _poi_photos_dir(project_id: str, day: int, poi_name: str) -> Path:
    return _workspace_dir(project_id) / "outputs" / "poi_photos" / f"day{day}_{poi_slug(poi_name)}"


def _clear_photo_variants(directory: Path, stem: str) -> None:
    """覆盖上传/删除前清掉同一槽位此前任意扩展名的旧文件——用户换格式重传时（比如把
    overview.jpg换成overview.png）不能只覆盖同名文件，否则旧扩展名的文件会残留成孤儿。"""
    if not directory.exists():
        return
    for ext in ("jpg", "png", "webp"):
        stale = directory / f"{stem}.{ext}"
        if stale.exists():
            stale.unlink()


def _require_poi_detail(project_id: str, day: int, poi_name: str) -> PoiDetailFile:
    current = read_poi_detail(project_id, day, poi_name)
    if current is None:
        raise ValueError(f"POI详情文件不存在（day={day}, poi_name={poi_name!r}），无法上传照片")
    return current


def write_poi_overview_photo(
    project_id: str, actor: str, day: int, poi_name: str, content: bytes, ext: str
) -> PoiDetailFile:
    """写入/覆盖POI整体照片——用户手动上传，跟高德自动查图（src/tools/poi_photo.py）
    互补，优先级见run_generate_site.py的_attach_poi_overview_photos。调用方必须已经
    用src/tools/poi_photo_upload.py校验过content的大小/类型，这里不重复校验。"""
    _check("poi_photo", actor)
    current = _require_poi_detail(project_id, day, poi_name)
    directory = _poi_photos_dir(project_id, day, poi_name)
    _clear_photo_variants(directory, "overview")
    directory.mkdir(parents=True, exist_ok=True)
    photo_path = directory / f"overview.{ext}"
    photo_path.write_bytes(content)
    current.uploaded_overview_photo_path = str(photo_path.relative_to(_workspace_dir(project_id)))
    current.updated_at = _now()
    _write_json(_poi_detail_path(project_id, day, poi_name), current.model_dump(mode="json"))
    _append_changelog(
        project_id, actor, "write", f"outputs/poi_details/day{day}_{poi_slug(poi_name)}.json",
        f"上传POI整体照片: {poi_name}",
    )
    return current


def delete_poi_overview_photo(project_id: str, actor: str, day: int, poi_name: str) -> PoiDetailFile:
    """删除POI整体照片——清空字段后展示逻辑自动回退到高德自动查图/占位框，不需要额外逻辑。"""
    _check("poi_photo", actor)
    current = _require_poi_detail(project_id, day, poi_name)
    _clear_photo_variants(_poi_photos_dir(project_id, day, poi_name), "overview")
    current.uploaded_overview_photo_path = ""
    current.updated_at = _now()
    _write_json(_poi_detail_path(project_id, day, poi_name), current.model_dump(mode="json"))
    _append_changelog(
        project_id, actor, "write", f"outputs/poi_details/day{day}_{poi_slug(poi_name)}.json",
        f"删除POI整体照片: {poi_name}",
    )
    return current


def _find_segment(current: PoiDetailFile, route_name: str, order: int) -> RouteSegment:
    for route in current.routes:
        if route.route_name != route_name:
            continue
        for segment in route.route_segments:
            if segment.order == order:
                return segment
    raise ValueError(
        f"未找到途经点（route_name={route_name!r}, order={order}），无法上传照片"
    )


def write_poi_segment_photo(
    project_id: str, actor: str, day: int, poi_name: str, route_name: str, order: int, content: bytes, ext: str
) -> PoiDetailFile:
    """写入/覆盖途经点照片——用户手动指定，不存在自动匹配图文不符的风险（跟POI整体照片
    的高德自动查图不同），是这次新增的展示位置（此前途经点卡片一直是纯CSS占位框.img-ph，
    没有任何图片来源，无论是自动还是人工）。"""
    _check("poi_photo", actor)
    current = _require_poi_detail(project_id, day, poi_name)
    segment = _find_segment(current, route_name, order)
    stem = f"seg_{poi_slug(route_name)}_{order}"
    directory = _poi_photos_dir(project_id, day, poi_name)
    _clear_photo_variants(directory, stem)
    directory.mkdir(parents=True, exist_ok=True)
    photo_path = directory / f"{stem}.{ext}"
    photo_path.write_bytes(content)
    segment.uploaded_photo_path = str(photo_path.relative_to(_workspace_dir(project_id)))
    current.updated_at = _now()
    _write_json(_poi_detail_path(project_id, day, poi_name), current.model_dump(mode="json"))
    _append_changelog(
        project_id, actor, "write", f"outputs/poi_details/day{day}_{poi_slug(poi_name)}.json",
        f"上传途经点照片: {poi_name} / {route_name} / 第{order}站",
    )
    return current


def delete_poi_segment_photo(
    project_id: str, actor: str, day: int, poi_name: str, route_name: str, order: int
) -> PoiDetailFile:
    """删除途经点照片——清空字段后展示逻辑自动回退到占位框（途经点没有自动查图路径）。"""
    _check("poi_photo", actor)
    current = _require_poi_detail(project_id, day, poi_name)
    segment = _find_segment(current, route_name, order)
    stem = f"seg_{poi_slug(route_name)}_{order}"
    _clear_photo_variants(_poi_photos_dir(project_id, day, poi_name), stem)
    segment.uploaded_photo_path = ""
    current.updated_at = _now()
    _write_json(_poi_detail_path(project_id, day, poi_name), current.model_dump(mode="json"))
    _append_changelog(
        project_id, actor, "write", f"outputs/poi_details/day{day}_{poi_slug(poi_name)}.json",
        f"删除途经点照片: {poi_name} / {route_name} / 第{order}站",
    )
    return current


def write_poi_details_manifest(project_id: str, actor: str, manifest: PoiDetailManifest) -> None:
    """由join节点统一写，不是各并行分支各自写——避免并发写同一文件的竞态，见docs/ORCHESTRATION.md第3.3节。"""
    _check("poi_details_manifest", actor)
    manifest.updated_at = _now()
    path = _workspace_dir(project_id) / "outputs" / "poi_details" / "manifest.json"
    with _poi_status_file_lock(project_id):
        _write_json_atomic(path, manifest.model_dump(mode="json"))
    _append_changelog(project_id, actor, "write", "outputs/poi_details/manifest.json", "登记本次trip生成的POI详情列表")


def _persist_poi_citation_status(
    project_id: str,
    day: int,
    poi_name: str,
    *,
    citation_status: str,
    actor: str,
    retry_history: list[str] | None = None,
    auto_collected: list[dict] | None = None,
    failure_summary: str = "",
    failure_kind: str | None = None,
) -> bool:
    """Persist one POI terminal status to its detail and manifest row.

    A missing detail remains a regeneration problem; this helper never creates
    an empty deliverable merely to make the manifest look consistent.
    """
    if citation_status not in {"passed", "force_finalized", "generation_failed"}:
        raise ValueError(f"unsupported POI citation_status: {citation_status}")

    path = _poi_detail_path(project_id, day, poi_name)
    manifest_path = _workspace_dir(project_id) / "outputs" / "poi_details" / "manifest.json"
    detail_changed = False
    manifest_changed = False
    with _poi_status_file_lock(project_id):
        detail = _read_json(path)
        if detail is None:
            return False
        if citation_status == "force_finalized" and not any(
            route.get("route_segments") for route in detail.get("routes") or []
        ):
            raise ValueError(
                f"poi_detail_content_empty: Day{day} {poi_name}没有含途经点的可交付路线，"
                "不能用force_finalized把写入合同失败迁移成引用降级"
            )

        expected_review = citation_status != "passed"
        if detail.get("citation_status") != citation_status:
            detail["citation_status"] = citation_status
            detail_changed = True
        if bool(detail.get("human_review_required")) != expected_review:
            detail["human_review_required"] = expected_review
            detail_changed = True
        if bool(detail.get("evidence_gap")):
            detail["evidence_gap"] = False
            detail_changed = True
        if citation_status == "passed" and "missing_entities" in detail:
            detail.pop("missing_entities", None)
            detail_changed = True
        if retry_history is not None and detail.get("retry_history") != retry_history:
            detail["retry_history"] = retry_history
            detail_changed = True
        if auto_collected is not None and detail.get("auto_collected") != auto_collected:
            detail["auto_collected"] = auto_collected
            detail_changed = True
        if detail_changed:
            detail["updated_at"] = _now()
            _write_json_atomic(path, detail)

        manifest = _read_json(manifest_path)
        if isinstance(manifest, dict):
            for entry in manifest.get("generated", []) or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("file") != path.name and not (
                    entry.get("day") == day and str(entry.get("poi_name") or "") == poi_name
                ):
                    continue
                if entry.get("citation_status") != citation_status:
                    entry["citation_status"] = citation_status
                    manifest_changed = True
                expected_failure_kind = failure_kind if citation_status == "generation_failed" else None
                expected_failure_summary = failure_summary if citation_status == "generation_failed" else ""
                if entry.get("failure_kind") != expected_failure_kind:
                    entry["failure_kind"] = expected_failure_kind
                    manifest_changed = True
                if str(entry.get("failure_summary") or "") != expected_failure_summary:
                    entry["failure_summary"] = expected_failure_summary
                    manifest_changed = True
                break
            if manifest_changed:
                manifest["updated_at"] = _now()
                _write_json_atomic(manifest_path, manifest)

    if detail_changed or manifest_changed:
        action = {
            "passed": "citation_passed",
            "force_finalized": "force_finalize",
            "generation_failed": "generation_failed",
        }[citation_status]
        summary = {
            "passed": f"POI「{poi_name}」引用通过确认",
            "force_finalized": f"POI「{poi_name}」终态同步为force_finalized",
            "generation_failed": f"POI「{poi_name}」写入合同失败：{failure_summary}",
        }[citation_status]
        _append_changelog(
            project_id,
            actor,
            action,
            f"outputs/poi_details/{path.name}",
            summary,
        )
    return detail_changed or manifest_changed


def reconcile_poi_citation_status(
    project_id: str,
    day: int,
    poi_name: str,
    citation_status: str,
    *,
    actor: str = "review_engine",
) -> bool:
    """Deterministically reconcile an already-generated POI status pair."""
    detail_path = _poi_detail_path(project_id, day, poi_name)
    detail = _read_json(detail_path) or {}
    manifest_failure_kind: str | None = None
    manifest_failure_summary = ""
    manifest = _read_json(_workspace_dir(project_id) / "outputs" / "poi_details" / "manifest.json")
    if isinstance(manifest, dict):
        for entry in manifest.get("generated", []) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("file") == detail_path.name or (
                entry.get("day") == day and str(entry.get("poi_name") or "") == poi_name
            ):
                manifest_failure_kind = str(entry.get("failure_kind") or "") or None
                manifest_failure_summary = str(entry.get("failure_summary") or "")
                break
    return _persist_poi_citation_status(
        project_id,
        day,
        poi_name,
        citation_status=citation_status,
        actor=actor,
        retry_history=list(detail.get("retry_history") or []),
        auto_collected=(
            list(detail.get("auto_collected") or [])
            if citation_status == "force_finalized"
            else None
        ),
        failure_summary=manifest_failure_summary or str(detail.get("failure_summary") or ""),
        failure_kind=manifest_failure_kind or (str(detail.get("failure_kind") or "") or None),
    )


def mark_poi_force_finalize(
    project_id: str,
    day: int,
    poi_name: str,
    actor: str = "citation_checker",
    auto_collected: list[dict] | None = None,
    retry_history: list[str] | None = None,
) -> None:
    """按POI粒度的强制终结标记，不能复用project粒度的mark_force_finalize——那个函数会把
    conflicts.jsonl里所有pending冲突标记为force_finalized，会误伤"整个project是否完成"这个
    判断（其他POI、甚至route/lifestyle阶段的核验状态都不该被一个POI的核验失败连累）。
    这里只改这一个POI详情文件及其已存在的manifest条目，不碰conflicts.jsonl，
    不影响其他POI或project级别的完成判断。见docs/ORCHESTRATION.md第3.3节"实现时要注意"第1点。

    auto_collected（docs/ARCHITECTURE.md第16节新增）：调用方在项目级自动补充采集预算内、
    且真的判定为素材缺口时，已经真实下载/转写/入库成功的条目（不是候选，是已经真的进了
    素材库的内容）。为空说明这次没有真正补充到素材（预算耗尽/判定不是真缺/采集本身失败）。
    此前这里还有一条`collection_suggestions`——把未经过滤的候选原样呈现、供"人工审核"
    的兜底路径，但从未真正有人工审核入口消费这个字段，唯一的出口是被直接写进
    final_guide.md，真实撞见过一条不相关的成人内容视频标题混入其中、触发了下游judge
    评测的内容审核拦截，已于docs/ISSUES.md第98条移除这个字段和对应的呈现逻辑。

    retry_history（docs/ISSUES.md第98条新增）：每一轮核验打回的具体理由文本，此前只用于
    生成给模型看的重试prompt、用完即焚，事后完全查不到"这个POI每一轮到底因为什么被打回"，
    只能靠重新跑一遍核验反推。这里把它跟auto_collected一起持久化，补上这个可观测性缺口。

    调用方是系统级核验步骤（跟mark_force_finalize同款actor习惯，见_check("conflict_update", ...)），
    不是poi_detail_agent本人在写自己的产出，所以直接落盘而不经过write_poi_detail的
    actor==poi_detail_agent权限校验（那个校验是防止别的agent冒充poi_detail_agent写产出区，
    不适用于编排层自己做的系统级终结标记）。
    """
    _persist_poi_citation_status(
        project_id,
        day,
        poi_name,
        citation_status="force_finalized",
        actor=actor,
        auto_collected=auto_collected if auto_collected else None,
        retry_history=retry_history if retry_history else None,
    )


def mark_poi_generation_failed(
    project_id: str,
    day: int,
    poi_name: str,
    *,
    failure_summary: str,
    actor: str = "citation_checker",
    retry_history: list[str] | None = None,
) -> None:
    """Persist an explicit POI write-contract failure without blessing empty content."""
    _persist_poi_citation_status(
        project_id,
        day,
        poi_name,
        citation_status="generation_failed",
        actor=actor,
        retry_history=retry_history if retry_history else None,
        failure_summary=failure_summary,
        failure_kind="write_contract_failure",
    )


def mark_poi_citation_passed(
    project_id: str,
    day: int,
    poi_name: str,
    actor: str = "citation_checker",
    retry_history: list[str] | None = None,
) -> None:
    """历史force-finalized POI重新核验通过后恢复为passed。

    retry_history继续保留作审计证据；这里只更新当前有效状态，避免patch合并逻辑为了保持
    系统字段而把旧的force_finalized永久粘在已经完成双重确认的内容上。
    """
    _persist_poi_citation_status(
        project_id,
        day,
        poi_name,
        citation_status="passed",
        actor=actor,
        retry_history=retry_history if retry_history else None,
    )


def _mark_scoped_citation_passed(
    project_id: str,
    *,
    file: str,
    keys: set[tuple[str, int]] | set[int],
    actor: str,
) -> None:
    path = _workspace_dir(project_id) / file
    data = _read_json(path)
    if not isinstance(data, dict):
        return
    changed = False
    if file == "route_plan.json":
        for item in data.get("days", []) or []:
            if item.get("day") in keys and item.get("citation_status") != "passed":
                item["citation_status"] = "passed"
                item.pop("missing_entities", None)
                changed = True
    else:
        for field, day in keys:
            for item in data.get(field, {}).get("items", []) or []:
                if item.get("day") == day and item.get("citation_status") != "passed":
                    item["citation_status"] = "passed"
                    item.pop("missing_entities", None)
                    changed = True
    if not changed:
        return
    _write_json(path, data)
    _append_changelog(project_id, actor, "citation_passed", file, f"定向引用验收通过：{sorted(keys)}")


def mark_route_days_citation_passed(
    project_id: str,
    days: list[int],
    actor: str = "citation_checker",
) -> None:
    _mark_scoped_citation_passed(
        project_id, file="route_plan.json", keys=set(days), actor=actor
    )


def mark_lifestyle_items_citation_passed(
    project_id: str,
    keys: list[tuple[str, int]],
    actor: str = "citation_checker",
) -> None:
    _mark_scoped_citation_passed(
        project_id, file="outputs/lifestyle.json", keys=set(keys), actor=actor
    )


def mark_pitfall_items_citation_passed(
    project_id: str,
    keys: list[tuple[str, int]],
    actor: str = "citation_checker",
) -> None:
    _mark_scoped_citation_passed(
        project_id, file="outputs/pitfall.json", keys=set(keys), actor=actor
    )


def mark_route_days_force_finalized(
    project_id: str,
    days: list[int],
    actor: str = "citation_checker",
    missing_entities_by_day: dict[int, list[str]] | None = None,
) -> None:
    """按天粒度标记route_plan.json里对应RouteDay.citation_status（见docs/ISSUES.md第125条）。

    背景：POI详情有citation_status字段+网站树木页横幅，能直接看出"这条内容有没有真正
    通过核验"；route/lifestyle/pitfall层此前完全没有对等机制——force_finalize只更新了
    conflicts.jsonl，没有落到具体内容本身，事后只能翻历史记录人工核对。这里跟
    mark_poi_force_finalize同样的设计：不复用project粒度的mark_force_finalize（会误伤
    其他day的完成判断），只改route_plan.json自己的字段，不碰conflicts.jsonl。

    days是最终耗尽MAX_RETRY那一轮issues里涉及的day集合——不是"曾经有问题"的全部历史天数，
    只标记最后一轮仍未解决的那些天。

    missing_entities_by_day（可选，见docs/ISSUES.md第137条前后的entity级粒度下沉）：
    {day: [entity, ...]}，最后一轮核验未通过的具体entity列表，写入RouteDay.missing_entities，
    供前端精确到entity级展示，而不是整天笼统标注。不传时保持原有行为不变（该字段留空）。
    """
    path = _workspace_dir(project_id) / "route_plan.json"
    plan = _read_json(path)
    if plan is None:
        return
    missing_entities_by_day = missing_entities_by_day or {}
    for day_obj in plan.get("days", []):
        if day_obj.get("day") in days:
            day_obj["citation_status"] = "force_finalized"
            entities = missing_entities_by_day.get(day_obj.get("day"))
            if entities:
                day_obj["missing_entities"] = entities
    _write_json(path, plan)
    _append_changelog(project_id, actor, "force_finalize", "route_plan.json", f"Day{days}打回上限耗尽，强制终结")


def mark_lifestyle_items_force_finalized(
    project_id: str,
    keys: list[tuple[str, int]],
    actor: str = "citation_checker",
    missing_entities_by_key: dict[tuple[str, int], list[str]] | None = None,
) -> None:
    """按(field, day)粒度标记outputs/lifestyle.json里对应item的citation_status，
    跟mark_route_days_force_finalized同一个设计意图（见docs/ISSUES.md第125条）。

    missing_entities_by_key（可选，同mark_route_days_force_finalized）：
    {(field, day): [entity, ...]}，写入对应item.missing_entities。
    """
    path = _workspace_dir(project_id) / "outputs" / "lifestyle.json"
    data = _read_json(path)
    if data is None:
        return
    key_set = set(keys)
    missing_entities_by_key = missing_entities_by_key or {}
    for field, day in key_set:
        for item in data.get(field, {}).get("items", []):
            if item.get("day") == day:
                item["citation_status"] = "force_finalized"
                entities = missing_entities_by_key.get((field, day))
                if entities:
                    item["missing_entities"] = entities
    _write_json(path, data)
    _append_changelog(project_id, actor, "force_finalize", "outputs/lifestyle.json", f"{sorted(key_set)}打回上限耗尽，强制终结")


def mark_pitfall_items_force_finalized(
    project_id: str,
    keys: list[tuple[str, int]],
    actor: str = "citation_checker",
    missing_entities_by_key: dict[tuple[str, int], list[str]] | None = None,
) -> None:
    """跟mark_lifestyle_items_force_finalized同构，标记outputs/pitfall.json。"""
    path = _workspace_dir(project_id) / "outputs" / "pitfall.json"
    data = _read_json(path)
    if data is None:
        return
    key_set = set(keys)
    missing_entities_by_key = missing_entities_by_key or {}
    for field, day in key_set:
        for item in data.get(field, {}).get("items", []):
            if item.get("day") == day:
                item["citation_status"] = "force_finalized"
                entities = missing_entities_by_key.get((field, day))
                if entities:
                    item["missing_entities"] = entities
    _write_json(path, data)
    _append_changelog(project_id, actor, "force_finalize", "outputs/pitfall.json", f"{sorted(key_set)}打回上限耗尽，强制终结")


# ---------- outputs/user_notes.jsonl（用户备注/annotation layer，docs/ARCHITECTURE.md第21节） ----------


def _user_notes_path(project_id: str) -> Path:
    return _workspace_dir(project_id) / "outputs" / "user_notes.jsonl"


def _read_user_notes_raw(project_id: str) -> list[dict]:
    path = _user_notes_path(project_id)
    if not path.exists():
        return []
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            notes.append(json.loads(line))
    return notes


def _append_user_note_raw(project_id: str, note: dict) -> None:
    path = _user_notes_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")


def _rewrite_user_notes_raw(project_id: str, notes: list[dict]) -> None:
    """改/删都要在已有条目上翻转deleted/superseded_by字段——append-only底本本身不能
    改历史行的内容，但同一个note_id只应该有一份"当前状态"，所以这里整体重写文件
    （条目数量级是"用户手写备注"，不会大到重写成本明显，跟其它jsonl文件的append-only
    是两回事：changelog/material_gap这类是纯事件流从不改写旧行，这里改的是同一个
    note_id的生命周期状态，语义上更接近"软删+新增"而不是"事件溯源"）。"""
    path = _user_notes_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for note in notes:
            f.write(json.dumps(note, ensure_ascii=False) + "\n")


def write_user_note(
    project_id: str,
    actor: str,
    note_text: str,
    day: Optional[int] = None,
    poi_name: str = "",
    source: str = "web",
) -> UserNote:
    """新增一条用户备注。day/poi_name两级可选绑定：只填day=天级；都填=景点级；都不填=
    行程级（渲染到独立备注区）。跨重跑不清——备注是用户资产，不属于agent产出，见
    docs/ARCHITECTURE.md第21.2节。"""
    _check("user_note", actor)
    note = UserNote(
        note_id=uuid.uuid4().hex[:12],
        day=day,
        poi_name=poi_name,
        note_text=note_text,
        source=source,
        created_at=_now(),
    )
    _append_user_note_raw(project_id, note.model_dump(mode="json"))
    _append_changelog(project_id, actor, "write", "outputs/user_notes.jsonl", f"新增用户备注: {note_text[:30]}")
    return note


def list_user_notes(project_id: str) -> list[UserNote]:
    """只返回未删的备注，按创建时间顺序。"""
    return [
        UserNote.model_validate(n) for n in _read_user_notes_raw(project_id) if not n.get("deleted")
    ]


def _require_undeleted_note(notes: list[dict], note_id: str) -> dict:
    for note in notes:
        if note["note_id"] == note_id and not note.get("deleted"):
            return note
    raise ValueError(f"用户备注不存在或已删除（note_id={note_id!r}）")


def update_user_note(
    project_id: str,
    actor: str,
    note_id: str,
    note_text: str,
    day: Optional[int] = None,
    poi_name: str = "",
    source: str = "admin",
) -> UserNote:
    """改=软删旧条+append新条（旧条superseded_by指向新note_id），不是原地改写——
    保留完整历史，见docs/ARCHITECTURE.md第21.2节。"""
    _check("user_note", actor)
    notes = _read_user_notes_raw(project_id)
    old = _require_undeleted_note(notes, note_id)
    new_note = UserNote(
        note_id=uuid.uuid4().hex[:12],
        day=day,
        poi_name=poi_name,
        note_text=note_text,
        source=source,
        created_at=_now(),
    )
    old["deleted"] = True
    old["superseded_by"] = new_note.note_id
    notes.append(new_note.model_dump(mode="json"))
    _rewrite_user_notes_raw(project_id, notes)
    _append_changelog(project_id, actor, "write", "outputs/user_notes.jsonl", f"修改用户备注: {note_id} -> {new_note.note_id}")
    return new_note


def delete_user_note(project_id: str, actor: str, note_id: str) -> None:
    """软删——旧条标deleted=true，跨重跑不清。"""
    _check("user_note", actor)
    notes = _read_user_notes_raw(project_id)
    note = _require_undeleted_note(notes, note_id)
    note["deleted"] = True
    _rewrite_user_notes_raw(project_id, notes)
    _append_changelog(project_id, actor, "write", "outputs/user_notes.jsonl", f"删除用户备注: {note_id}")


# ---------- final guide ----------


def _read_valid_final_guide_blocks(project_id: str) -> Optional[FinalGuideBlocks]:
    raw = _read_json(_workspace_dir(project_id) / "final_guide.blocks.json")
    if not isinstance(raw, dict):
        return None
    try:
        return FinalGuideBlocks.model_validate(raw)
    except Exception:
        return None


def read_final_guide_blocks(project_id: str) -> Optional[FinalGuideBlocks]:
    """只返回与当前final_guide.md正文哈希一致的块映射。"""
    blocks = _read_valid_final_guide_blocks(project_id)
    if blocks is None:
        return None
    content = read_workspace(project_id, "final_guide.md")
    if not isinstance(content, str):
        return None
    if blocks.rendered_body_sha256 != rendered_body_sha256(contract_body_markdown(content)):
        return None
    return blocks


def write_final_guide(project_id: str, actor: str, content_md: str, meta: dict) -> None:
    _check("final_guide", actor)
    if not isinstance(meta, dict):
        raise ValueError("final_guide meta必须是JSON对象")
    if not isinstance(content_md, str):
        raise ValueError("final_guide content_md必须是字符串")
    FinalGuideMeta.model_validate(meta)
    ws = _workspace_dir(project_id)
    body_hash = rendered_body_sha256(contract_body_markdown(content_md))
    blocks = _read_valid_final_guide_blocks(project_id)
    if blocks is not None and blocks.rendered_body_sha256 == body_hash:
        meta["body_source"] = "blocks_v1"
        meta["blocks_schema_version"] = blocks.schema_version
        meta["blocks_count"] = len(blocks.blocks)
        meta["mapped_claim_count"] = sum(
            len(block.upstream_claim_ids) for block in blocks.blocks
        )
    else:
        meta["body_source"] = "legacy_markdown"
        meta["blocks_schema_version"] = ""
        meta["blocks_count"] = 0
        meta["mapped_claim_count"] = 0
    FinalGuideMeta.model_validate(meta)
    (ws / "final_guide.md").write_text(content_md, encoding="utf-8")
    meta["updated_at"] = _now()
    _write_json(ws / "final_guide.meta.json", meta)
    _append_changelog(project_id, actor, "write", "final_guide.md", "生成/更新最终攻略")


def write_final_guide_blocks(
    project_id: str,
    actor: str,
    blocks: list[dict],
    meta: dict,
) -> FinalGuideBlocks:
    """校验正文块与上游claim映射，再由程序渲染final_guide.md。"""
    _check("final_guide", actor)
    if not isinstance(meta, dict):
        raise ValueError("final_guide meta必须是JSON对象")
    FinalGuideMeta.model_validate(meta)

    route_plan = read_workspace(project_id, "route_plan.json")
    lifestyle = read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = read_workspace(project_id, "outputs/pitfall.json")
    poi_details = [detail.model_dump(mode="json") for detail in list_poi_details(project_id)]
    upstream_claims = collect_upstream_claims(
        route_plan if isinstance(route_plan, dict) else None,
        lifestyle if isinstance(lifestyle, dict) else None,
        pitfall if isinstance(pitfall, dict) else None,
        poi_details,
        evidence_checks=read_evidence_checks(project_id),
        review_findings=(read_workspace(project_id, "review_findings.jsonl") or []),
    )
    prepared = prepare_final_guide_blocks(blocks, upstream_claims)
    body = render_final_guide_body(prepared)
    if contract_body_markdown(body) != body:
        raise ValueError("正文块不能撰写程序管理的POI附录或参考来源章节")

    ws = _workspace_dir(project_id)
    old_content = read_workspace(project_id, "final_guide.md")
    _old_body, poi_appendix, reference_appendix = split_final_guide_sections(
        old_content if isinstance(old_content, str) else ""
    )
    now = _now()
    previous = _read_valid_final_guide_blocks(project_id)
    payload = FinalGuideBlocks(
        blocks=prepared,
        rendered_body_sha256=rendered_body_sha256(body),
        created_at=previous.created_at if previous and previous.created_at else now,
        updated_at=now,
    )
    _write_json(ws / "final_guide.blocks.json", payload.model_dump(mode="json"))
    content_md = compose_final_guide_sections(body, poi_appendix, reference_appendix)
    write_final_guide(project_id, actor, content_md, meta)
    _append_changelog(
        project_id,
        actor,
        "write",
        "final_guide.blocks.json",
        f"写入{len(prepared)}个可追溯正文块",
    )
    return payload


def backfill_final_guide_merged_fields(
    project_id: str,
    fields: list[str],
    *,
    actor: str = "review_migration",
) -> None:
    """Append deterministically inferred legacy declarations without rewriting prose."""
    meta_path = _workspace_dir(project_id) / "final_guide.meta.json"
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        raise FileNotFoundError(f"项目{project_id!r}缺少final_guide.meta.json")
    merged_fields = list(meta.get("merged_fields") or [])
    added = [field for field in fields if field not in merged_fields]
    if not added:
        return
    meta["merged_fields"] = merged_fields + added
    meta["updated_at"] = _now()
    FinalGuideMeta.model_validate(meta)
    _write_json(meta_path, meta)
    _append_changelog(
        project_id,
        actor,
        "migrate",
        "final_guide.meta.json",
        f"历史最终攻略Meta补充merged_fields={added}",
    )


def write_final_guide_contract_validation(
    project_id: str,
    *,
    status: str,
    issues: list[dict],
    actor: str = "citation_checker",
    source: str = "runtime",
) -> None:
    """Persist the program-owned final-guide contract result in its meta file."""
    if status not in {"passed", "retrying", "force_finalized"}:
        raise ValueError(f"不支持的最终攻略契约状态: {status}")
    ws = _workspace_dir(project_id)
    meta_path = ws / "final_guide.meta.json"
    meta = _read_json(meta_path) or {}
    guide_path = ws / "final_guide.md"
    guide_content = guide_path.read_text(encoding="utf-8") if guide_path.exists() else ""
    body_content = contract_body_markdown(guide_content)
    meta["contract_validation"] = {
        "status": status,
        "issues": issues,
        "body_sha256": hashlib.sha256(body_content.encode("utf-8")).hexdigest(),
        "guide_sha256": hashlib.sha256(guide_content.encode("utf-8")).hexdigest(),
        "checked_at": _now(),
        "source": source,
    }
    meta["updated_at"] = _now()
    _write_json(meta_path, meta)
    _append_changelog(
        project_id,
        actor,
        "validate",
        "final_guide.meta.json",
        f"最终攻略交付契约: {status}，issues={len(issues)}",
    )


def refresh_final_guide_delivery_hash(
    project_id: str,
    *,
    actor: str = "main_agent",
) -> bool:
    """Refresh the full-delivery hash without changing the validated contract result.

    Program-owned appendices may change after Integration validation. They may update
    ``guide_sha256``, but must not change the Integration-authored body that was checked.
    Legacy records without ``body_sha256`` are upgraded deterministically.
    """
    ws = _workspace_dir(project_id)
    meta_path = ws / "final_guide.meta.json"
    guide_path = ws / "final_guide.md"
    meta = _read_json(meta_path)
    if not isinstance(meta, dict) or not guide_path.exists():
        return False
    contract = meta.get("contract_validation")
    if not isinstance(contract, dict) or not contract.get("status"):
        return False

    guide_content = guide_path.read_text(encoding="utf-8")
    body_hash = hashlib.sha256(contract_body_markdown(guide_content).encode("utf-8")).hexdigest()
    previous_body_hash = str(contract.get("body_sha256") or "")
    if previous_body_hash and previous_body_hash != body_hash:
        raise RuntimeError(
            f"项目{project_id!r}的最终攻略主正文在契约校验后发生了变化，拒绝刷新交付哈希"
        )

    guide_hash = hashlib.sha256(guide_content.encode("utf-8")).hexdigest()
    if previous_body_hash == body_hash and contract.get("guide_sha256") == guide_hash:
        return False
    contract["body_sha256"] = body_hash
    contract["guide_sha256"] = guide_hash
    contract["delivered_at"] = _now()
    meta["contract_validation"] = contract
    meta["updated_at"] = _now()
    FinalGuideMeta.model_validate(meta)
    _write_json(meta_path, meta)
    _append_changelog(
        project_id,
        actor,
        "refresh_delivery_hash",
        "final_guide.meta.json",
        "刷新最终攻略完整交付哈希",
    )
    return True


def normalize_final_guide_program_sections(
    project_id: str,
    *,
    actor: str = "review_migration",
) -> bool:
    """Normalize program-owned appendices without rewriting Integration prose."""
    ws = _workspace_dir(project_id)
    guide_path = ws / "final_guide.md"
    meta_path = ws / "final_guide.meta.json"
    if not guide_path.exists():
        return False
    current = guide_path.read_text(encoding="utf-8")
    normalized = normalize_final_guide_section_order(current)
    if normalized == current:
        return False
    meta = _read_json(meta_path) or {}
    meta["updated_at"] = _now()
    FinalGuideMeta.model_validate(meta)
    guide_path.write_text(normalized, encoding="utf-8")
    _write_json(meta_path, meta)
    _append_changelog(
        project_id,
        actor,
        "migrate",
        "final_guide.md",
        "规范化最终攻略程序附录顺序：POI在前，参考来源在末尾",
    )
    return True


# ---------- conflicts ----------


def report_conflict(
    project_id: str,
    actor: str,
    target: str,
    reason: str,
    issues: Optional[list[dict]] = None,
    issue_type: str = "",
) -> ConflictRecord:
    _check("conflict_report", actor)
    record = ConflictRecord(
        id=str(uuid.uuid4())[:8],
        reported_by=actor,
        target=target,
        reason=reason,
        issues=issues or [],
        created_at=_now(),
        issue_type=issue_type,
    )
    path = _workspace_dir(project_id) / "conflicts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")
    _append_changelog(project_id, actor, "report_conflict", "conflicts.jsonl", reason)
    return record


def get_pending_conflict(project_id: str) -> Optional[ConflictRecord]:
    path = _workspace_dir(project_id) / "conflicts.jsonl"
    if not path.exists():
        return None
    records = [ConflictRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pending = [r for r in records if r.status == "pending"]
    return pending[-1] if pending else None


def get_latest_unresolved_conflict(
    project_id: str, target: str, reason_prefix: Optional[str] = None
) -> Optional[ConflictRecord]:
    """按target精确匹配，返回最近一条状态不是"resolved"的冲突记录（pending或
    force_finalized），跨越同一个project可能发生的多次node调用（见docs/ISSUES.md
    第138条前后）。

    背景：route_citation_check_node/route_timeliness_check_node这类MAX_RETRY循环，
    `last_record_id`是函数内的局部变量，只能追踪"这一次node调用"内部的冲突记录链条——
    如果同一个project在这次调用结束后（比如耗尽MAX_RETRY被force_finalize）又被
    重新调用了一次（route_agent因为下游冲突被重新触发重写），新调用的`last_record_id`
    从None开始，完全不知道上一次调用遗留的那条force_finalized记录，即使这次调用
    最终核验通过，也不会去resolve那条早已过时的记录——导致conflicts.jsonl里永久留着
    一条"看起来还在force_finalized"的记录，即使实际内容早已修好（真实撞见：
    route_plan.json当前citation_status全部是passed，但conflicts.jsonl里有2条
    route_agent的记录永远停在force_finalized）。这会污染`run_scenario_eval.py`的
    `force_finalized_count`统计和管理后台的冲突历史展示。

    reason_prefix（可选）：route_citation_check_node和route_timeliness_check_node
    的报告都用target="route_agent"（下游route_draft_node靠这个字符串识别"这是route层
    的冲突"，不能改），但两者是完全不同性质的检查（引用真实性 vs 时效性），各自的
    reason分别以"路线引用核验未通过"/"路线时效核验未通过"开头——不传这个参数会把
    两种检查的冲突记录混在一起，可能出现"timeliness核验通过后，误把citation_check
    留下的force_finalized记录也一起resolve掉"这种张冠李戴。传入时按reason前缀再筛一次，
    只在同一类检查内部追溯"上一次调用"。

    调用方应该只在"这次核验范围是全量、没有做天数/字段收窄"时使用这个函数的返回值
    去初始化`last_record_id`（而不是无脑传None）——如果这次是edit模式的收窄核验，
    上一次遗留的记录可能覆盖的是完全不同的天数/字段，贸然继承它、又在核验通过时
    顺手resolve掉，会错误地把一个跟这次核验范围无关、可能依然存在的问题standing一并
    掩盖掉，见调用方docstring里的这条限制。
    """
    path = _workspace_dir(project_id) / "conflicts.jsonl"
    if not path.exists():
        return None
    records = [ConflictRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    matching = [
        r for r in records
        if r.target == target and r.status != "resolved"
        and (reason_prefix is None or r.reason.startswith(reason_prefix))
    ]
    return matching[-1] if matching else None


def _rewrite_conflicts(project_id: str, records: list[ConflictRecord]) -> None:
    path = _workspace_dir(project_id) / "conflicts.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")


def increment_retry_count(project_id: str, conflict_id: str, actor: str = "main_agent") -> ConflictRecord:
    _check("conflict_update", actor)
    path = _workspace_dir(project_id) / "conflicts.jsonl"
    records = [ConflictRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    updated = None
    for r in records:
        if r.id == conflict_id:
            r.retry_count += 1
            updated = r
    _rewrite_conflicts(project_id, records)
    _append_changelog(project_id, actor, "retry", "conflicts.jsonl", f"打回 conflict={conflict_id}")
    return updated


def resolve_conflict(project_id: str, conflict_id: str, actor: str = "integration_agent") -> None:
    path = _workspace_dir(project_id) / "conflicts.jsonl"
    if not path.exists():
        return
    records = [ConflictRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in records:
        if r.id == conflict_id:
            r.status = "resolved"
    _rewrite_conflicts(project_id, records)
    _append_changelog(project_id, actor, "resolve", "conflicts.jsonl", f"冲突已解决 id={conflict_id}")


def mark_force_finalize(project_id: str, actor: str = "main_agent") -> None:
    _check("conflict_update", actor)
    path = _workspace_dir(project_id) / "conflicts.jsonl"
    if not path.exists():
        return
    records = [ConflictRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in records:
        if r.status == "pending":
            r.status = "force_finalized"
    _rewrite_conflicts(project_id, records)
    _append_changelog(project_id, actor, "force_finalize", "conflicts.jsonl", "打回上限耗尽，强制终结")


# ---------- run status（机制B：Agent运行状态的跨进程持久化通道，见docs/ARCHITECTURE.md第13.2节）----------


def write_run_status(project_id: str, status: dict) -> None:
    """orchestrator.py的_track_run_status装饰器在每个node执行前后调用，记录当前在跑哪个node、
    第几次调用、开始/结束时间，供管理后台近实时轮询展示。跟site.html/poster.png同款处理——这是
    瞬时运行态信息，不是需要长期保留/校验的产出数据，不经过_check权限校验（调用方是编排层本身
    包出来的装饰器，不是某个agent在冒充别的agent写东西）。project可能来自CLI（run_mvp.py）也
    可能来自后台API触发的重跑，落盘依据的是state里的project_id，两种触发来源天然兼容。
    """
    path = _workspace_dir(project_id) / "run_status.json"
    _write_json(path, status)


def read_run_status(project_id: str) -> Optional[dict]:
    return _read_json(_workspace_dir(project_id) / "run_status.json")


@contextmanager
def project_plan_run_lock(project_id: str):
    """Allow only one active plan/resume execution for a Project.

    LangGraph checkpoints make a crashed process resumable, but they do not make
    two simultaneous resumes safe. Both processes can continue old child graphs
    and write the shared Workspace. Keep the active process running and reject a
    duplicate invocation immediately instead of letting their checkpoints mix.
    """
    workspace = _workspace_dir(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    lock_path = workspace / ".plan_run.lock"
    with _PROJECT_RUN_LOCKS_GUARD:
        thread_lock = _PROJECT_RUN_LOCKS.setdefault(project_id, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise ProjectRunAlreadyActiveError(
            f"Project {project_id!r} already has an active plan execution in this process"
        )
    lock_file = None
    lock_fd = None
    file_lock_acquired = False
    try:
        # Historical Projects already have the old metadata lock file. Reuse its
        # inode without rewriting PID/timestamps; new Projects lock the workspace
        # directory itself so a completed run_plan() recall has no file side effect.
        if lock_path.is_file():
            lock_file = lock_path.open("r+", encoding="utf-8")
            lock_fd = lock_file.fileno()
        else:
            lock_fd = os.open(workspace, os.O_RDONLY)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            file_lock_acquired = True
        except BlockingIOError as exc:
            raise ProjectRunAlreadyActiveError(
                f"Project {project_id!r} already has an active plan execution"
            ) from exc
        yield
    finally:
        try:
            if file_lock_acquired and lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            if lock_file is not None:
                lock_file.close()
            elif lock_fd is not None:
                os.close(lock_fd)
            thread_lock.release()


# ---------- 素材缺口自动补充计数器（docs/ARCHITECTURE.md第16节）----------

# 用同一把锁保护"读旧计数->改->写回"这个环节的原子性。POI详情agent是LangGraph Send API
# 并行fan-out（多个poi_detail_agent_node实例可能在同一次graph.invoke()里被派发到不同
# 线程真并发执行，LangGraph的Pregel执行器对同一superstep内的多个node默认用线程池并发跑），
# 如果多个分支同时"读计数->判断<上限->+1->写回"，会出现经典的竞态：都读到count=9（<10），
# 都认为自己有名额，各自+1写回，最终count被写成10而不是应该的11——表面上没超但实际上
# 多批了一次采集。这里选择的做法是：只让"读计数+原子性占用一个名额"这个动作被锁保护
# （耗时是一次磁盘读写，很快），真正耗时的下载/转写（collect_one）留在锁外执行——
# 既保证了计数不失真，又不会让Send并行分支因为这把锁而被迫串行跑完整个下载+ASR流程。
_MATERIAL_GAP_LOCK = threading.Lock()


def _material_gap_path(project_id: str) -> Path:
    return _workspace_dir(project_id) / "material_gap_supplement.json"


@contextmanager
def _material_gap_file_lock(project_id: str):
    """Serialize budget reservations across Codex processes and LangGraph threads."""
    lock_path = _workspace_dir(project_id) / ".material_gap_supplement.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _MATERIAL_GAP_LOCK, lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _material_gap_data(project_id: str) -> dict:
    data = _read_json(_material_gap_path(project_id)) or {}
    data.setdefault("schema_version", "material-gap-supplement-v2")
    data.setdefault("count", 0)
    data.setdefault("items", [])
    data.setdefault("reservations", [])
    return data


def read_material_gap_supplement_count(project_id: str) -> int:
    """当前项目累计"自动补充采集成功"的条数（不含预定后又失败被释放的名额）。"""
    data = _read_json(_material_gap_path(project_id))
    return (data or {}).get("count", 0)


def try_consume_material_gap_budget(project_id: str, max_total: int) -> bool:
    """兼容旧调用的计数式预算接口；新共享补采使用 reservation ID。

    线程安全地尝试为一次真正的自动采集占用一个名额：如果当前累计数已经达到
    max_total上限，不做任何改动、返回False（调用方应放弃这次自动采集，回退到
    "只呈现候选、人工确认"这条既有路径）；否则原子地把计数+1并返回True。

    调用方必须在拿到True之后才去真正执行下载/转写（collect_one）——如果之后采集
    其实失败了，必须调用release_material_gap_budget把这个名额还回去，不能让失败的
    尝试白白永久占用总量上限（否则"最多自动采集10条"会在实际远不到10条真实内容时
    就被提前锁死，达不到限量自动化本来的目的）。
    """
    with _material_gap_file_lock(project_id):
        data = _material_gap_data(project_id)
        active = len(data.get("reservations") or [])
        if int(data.get("count") or 0) + active >= max_total:
            return False
        data["count"] = int(data.get("count") or 0) + 1
        data["updated_at"] = _now()
        _write_json_atomic(_material_gap_path(project_id), data)
        return True


def reserve_material_gap_budget(project_id: str, max_total: int) -> str | None:
    """Reserve one supplement slot and return an ownership token."""
    with _material_gap_file_lock(project_id):
        data = _material_gap_data(project_id)
        active = len(data.get("reservations") or [])
        if int(data.get("count") or 0) + active >= max_total:
            return None
        reservation_id = uuid.uuid4().hex
        data["reservations"].append(
            {"reservation_id": reservation_id, "reserved_at": _now()}
        )
        data["updated_at"] = _now()
        _write_json_atomic(_material_gap_path(project_id), data)
        return reservation_id


def finalize_material_gap_budget(project_id: str, reservation_id: str) -> bool:
    """Convert exactly one owned reservation into a successful indexed item."""
    with _material_gap_file_lock(project_id):
        data = _material_gap_data(project_id)
        before = len(data["reservations"])
        data["reservations"] = [
            item
            for item in data["reservations"]
            if item.get("reservation_id") != reservation_id
        ]
        if len(data["reservations"]) == before:
            return False
        data["count"] = int(data.get("count") or 0) + 1
        data["updated_at"] = _now()
        _write_json_atomic(_material_gap_path(project_id), data)
        return True


def release_material_gap_budget(
    project_id: str, reservation_id: str | None = None
) -> None:
    """Release one owned reservation, or one legacy counter slot."""
    with _material_gap_file_lock(project_id):
        data = _material_gap_data(project_id)
        if reservation_id:
            data["reservations"] = [
                item
                for item in data["reservations"]
                if item.get("reservation_id") != reservation_id
            ]
        else:
            data["count"] = max(int(data.get("count") or 0) - 1, 0)
        data["updated_at"] = _now()
        _write_json_atomic(_material_gap_path(project_id), data)


def record_material_gap_supplement_item(project_id: str, entry: dict) -> None:
    """追加一条采集/ingest审计记录，不改变成功计数或 reservation。"""
    with _material_gap_file_lock(project_id):
        data = _material_gap_data(project_id)
        data.setdefault("items", []).append(entry)
        data["updated_at"] = _now()
        _write_json_atomic(_material_gap_path(project_id), data)


def record_material_gap_supplement_blocked(project_id: str, reason: str) -> None:
    """自动补充采集因环境依赖缺失（系统级ffmpeg/ffprobe、MIMO_API_KEY未配置）而整体
    无法执行时调用——跟record_material_gap_supplement_item（记录"采集成功了什么"）
    对称，这个记录"为什么完全没能尝试采集"。

    见docs/ISSUES.md第77条：此前这类环境问题会被静默吞掉、只在stdout打一行容易被
    忽略的日志，导致"入库0条"被误诊断成"素材库该目的地真的搜不到内容"，排查了很久
    才发现跟内容质量无关。现在显式留痕到material_gap_supplement.json的blocked列表
    +changelog，事后排查"为什么没有素材"时第一眼就能看到，不需要重新调试一遍。
    """
    with _material_gap_file_lock(project_id):
        data = _material_gap_data(project_id)
        data.setdefault("blocked", []).append({"reason": reason, "timestamp": _now()})
        data["updated_at"] = _now()
        _write_json_atomic(_material_gap_path(project_id), data)
    _append_changelog(
        project_id, actor="material_gap_supplement", action="blocked",
        target="material_gap_supplement.json", summary=reason,
    )


# ---------- 素材库（全局，不绑定project_id） ----------


def material_index_exists(topic: str) -> bool:
    entries_path = MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl"
    return entries_path.exists() and entries_path.stat().st_size > 0


def material_needs_sync(topic: str) -> Optional[bool]:
    """判断该topic的raw素材有没有还没处理进index的新文件。

    返回 True  = 有raw但存在未入库的新文件，需要跑ingest_topic
    返回 False = raw和index已经同步，不需要跑
    返回 None  = 连raw/manifest.json都不存在，这不是"需要同步"，是"完全没有素材"，
                 调用方需要另外处理（v1提示用户手动放，v2调用真实抓取API）
    """
    raw_manifest = MATERIAL_LIBRARY_DIR / "raw" / topic / "manifest.json"
    if not raw_manifest.exists():
        return None

    manifest = json.loads(raw_manifest.read_text(encoding="utf-8"))
    manifest_files = {item["source_file"] for item in manifest.get("items", [])}

    processed_files: set[str] = set()
    entries_path = MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl"
    if entries_path.exists():
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                processed_files.add(json.loads(line)["source_file"])

    rejected_path = MATERIAL_LIBRARY_DIR / "index" / topic / "rejected.jsonl"
    if rejected_path.exists():
        for line in rejected_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                processed_files.add(json.loads(line)["source_file"])

    return bool(manifest_files - processed_files)
