"""飞书推送成功/失败日志——补上此前"推送成功/失败日志仍是占位未做"这个已知缺口
（见docs/ARCHITECTURE.md第13.2节域8）。

推送尝试（消息发送/编辑/图片/主动提醒）此前只有print()/traceback.print_exc()打到
stdout，没有任何持久化记录——worker进程日志一旦被清理或没人盯着，推送失败就完全
无声无息。这里新增一个全局（不是per-project）的jsonl追加日志，管理后台飞书运维页
用它汇总展示"最近推送情况"+"按project/chat聚合的活跃度统计"。

全局存储（不是per-project）的原因：飞书运维页天生是跨project的汇总视图，按project
分散存储会让"查最近失败的推送"这类查询变成O(project数量)次文件读取；全局单文件只需
读一次尾部N行，跟heartbeat_history.jsonl同一个存储层级/同一种"渠道层调度元数据，
不进任何project的workspace/"定位。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Optional

from src.config import DATA_DIR

PUSH_LOG_PATH = DATA_DIR / "feishu" / "push_log.jsonl"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_push_log(
    chat_id: str,
    project_id: Optional[str],
    push_type: str,
    success: bool,
    error: Optional[str] = None,
) -> None:
    """追加一条推送记录。project_id可以是None——某些call site（比如收到第一条消息、
    binding还没解析完成前）确实还不知道project_id，记null也比跳过这条日志好（至少
    还能看到"这次推送尝试发生过、成不成功"）。push_type用简短标识区分推送场景
    （"reminder"/"message"/"image"等），不做枚举校验——调用方约定俗成传一致的值即可，
    没有必要为这个内部日志字段引入额外的类型系统开销。"""
    entry = {
        "timestamp": _now(),
        "chat_id": chat_id,
        "project_id": project_id,
        "push_type": push_type,
        "success": success,
        "error": error,
    }
    with _lock:
        PUSH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PUSH_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_push_log(limit: int = 200) -> list[dict]:
    """返回最近limit条记录，最新的排在最前面。jsonl文件在个人使用规模下很小，
    直接整个读入内存再倒序切片即可，不需要反向seek这类优化。"""
    if not PUSH_LOG_PATH.exists():
        return []
    lines = PUSH_LOG_PATH.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    entries.reverse()
    return entries[:limit]


def compute_activity_stats() -> list[dict]:
    """按project_id聚合推送次数/成功数/失败数/最后一次推送时间，project_id为None时
    退化到按chat_id聚合（这类记录多半发生在project还没解析出来的早期阶段，用chat_id
    分组好过完全丢弃这条统计信息）。按最后推送时间降序排列（最活跃的排在最前面）。"""
    if not PUSH_LOG_PATH.exists():
        return []
    lines = PUSH_LOG_PATH.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]

    grouped: dict[str, dict] = {}
    for entry in entries:
        key = entry.get("project_id") or f"chat:{entry['chat_id']}"
        stat = grouped.setdefault(
            key,
            {
                "project_id": entry.get("project_id"),
                "chat_id": entry["chat_id"],
                "push_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "last_push_at": entry["timestamp"],
            },
        )
        stat["push_count"] += 1
        if entry["success"]:
            stat["success_count"] += 1
        else:
            stat["failure_count"] += 1
        if entry["timestamp"] > stat["last_push_at"]:
            stat["last_push_at"] = entry["timestamp"]

    return sorted(grouped.values(), key=lambda s: s["last_push_at"], reverse=True)
