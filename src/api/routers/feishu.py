"""飞书运维可视化v1只读接口（docs/ARCHITECTURE.md第13.2节域8）：群↔project绑定关系、
worker进程心跳健康状态、推送成功/失败日志、心跳历史趋势、活跃度统计。这几类数据本身
都不落在任何单个project的workspace/里（分别是data/feishu/bindings.json、
worker_heartbeat.json、push_log.jsonl、heartbeat_history.jsonl），直接复用
src/channels/feishu/bindings.py、health.py、push_log.py的读函数，不新增第二套读
数据逻辑——跟其余只读路由（projects.py/materials.py）的既有模式一致。

只读展示，不做任何写操作（不支持在网页上手动解绑/踢群，这类操作风险高于个人使用场景
下的价值，维持"飞书群关系只能通过真实群聊行为改变"的现状）。

**2026-08-01新增**：补上此前"推送成功/失败日志仍是占位未做"这个已知缺口——
`GET /push_log`（最近推送记录）、`GET /heartbeat_history`（心跳历史+疑似掉线时段）、
`GET /activity_stats`（按project/chat聚合的活跃度统计）。
"""
from __future__ import annotations

from src.channels.feishu import bindings, health, push_log
from src.workspace import store

from fastapi import APIRouter

router = APIRouter(prefix="/api/feishu", tags=["feishu"])


def _enrich_binding(chat_id: str, binding: dict) -> dict:
    project_id = binding.get("active_project_id")
    project_exists = project_id in store.list_project_ids() if project_id else False

    topic = destination = state = updated_at = None
    if project_exists:
        saved = store.read_requirements(project_id)
        requirements = (saved or {}).get("requirements", {})
        topic = (saved or {}).get("topic")
        destination = requirements.get("destination")
        progress = store.read_progress(project_id)
        state = progress.state.value
        updated_at = progress.updated_at

    return {
        "chat_id": chat_id,
        "project_id": project_id,
        "history": binding.get("history", []),
        "awaiting": binding.get("awaiting"),
        "project_exists": project_exists,
        "topic": topic,
        "destination": destination,
        "state": state,
        "updated_at": updated_at,
    }


@router.get("/bindings")
def list_feishu_bindings() -> list[dict]:
    """按chat_id升序排列——飞书群数量在个人使用场景下不会多到需要更复杂的排序/分页，
    升序保证结果确定性（方便测试断言，也方便用户在页面上定位固定位置的某个群）。"""
    all_bindings = bindings.list_bindings()
    return [_enrich_binding(chat_id, binding) for chat_id, binding in sorted(all_bindings.items())]


@router.get("/health")
def get_feishu_health() -> dict:
    return health.get_worker_health()


@router.get("/push_log")
def get_push_log(limit: int = 200) -> list[dict]:
    return push_log.read_push_log(limit=limit)


@router.get("/heartbeat_history")
def get_heartbeat_history(limit: int = 500) -> dict:
    history = health.read_heartbeat_history(limit=limit)
    return {"history": history, "anomalies": health.detect_anomaly_periods(history)}


@router.get("/activity_stats")
def get_activity_stats() -> list[dict]:
    return push_log.compute_activity_stats()
