"""飞书worker连接健康状态v1：心跳文件机制。

背景（docs/ARCHITECTURE.md第13.2节域8"飞书运维（预留域）"）：run_feishu_worker.py是一个
独立长期存活进程，WS长连接的`.start()`是阻塞调用（见ws_client.py）。管理后台是另一个
独立进程（uvicorn），没有任何直接手段知道那个进程是否还活着、上次真正干活是什么时候——
需要一个跨进程可读的信号。

设计取舍：这里只做"进程心跳"，不是"WS连接本身的真实健康探测"——`lark.ws.Client`没有
暴露轻量级的"连接是否仍然存活"查询接口，真要做到那个粒度需要深入SDK内部状态或自己维护
一条探测消息，成本明显更高。心跳线程只要主循环没有被阻塞死（例如死锁、未捕获异常导致
线程退出）就会持续写入，这是"worker进程健康"的一个近似代理，不是"飞书那端连接真的通"的
证明——比如网络分区导致WS被服务端静默断开、但worker进程本身没有崩溃，这种情况心跳仍然
会显示healthy，是已知局限（跟主动提醒机制"能发现问题、不保证100%"是类似的定位）。

跟bindings.py/reminders.py同款"渠道层调度元数据"定位——存放在data/feishu/，不进任何
project的workspace/，不走src/workspace/store.py的权限校验体系。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from src.config import DATA_DIR

HEARTBEAT_PATH = DATA_DIR / "feishu" / "worker_heartbeat.json"

# 心跳历史（2026-08-01新增，补上"只有最新快照、没有历史趋势"这个缺口）：只存原始
# 时间戳事实，不存派生的健康状态——healthy/stale是"读取时"根据相邻时间戳间隔算出来的
# 概念，不是应该持久化的"事实"，见detect_anomaly_periods()。
HEARTBEAT_HISTORY_PATH = DATA_DIR / "feishu" / "heartbeat_history.jsonl"

# 判定"stale"（进程可能已经卡死/崩溃但没清理心跳文件）的阈值：轮询间隔的3倍，
# 留足够余量给一次GC暂停/慢速IO，不因为一次偶发延迟就误报不健康。
STALE_THRESHOLD_SECONDS = 60.0

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def write_heartbeat() -> None:
    """写一次心跳。第一次调用时把started_at固化下来（同一进程生命周期内不变），
    之后每次调用只更新last_heartbeat——供start_heartbeat_thread周期性调用。同时
    追加一条历史记录（同一个_lock内完成，write_heartbeat本来就已经持锁跑完整个
    函数体，追加历史顺带在这里做，不需要额外的锁）。"""
    with _lock:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        started_at = _now().isoformat()
        if HEARTBEAT_PATH.exists():
            try:
                existing = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
                if existing.get("pid") == os.getpid() and existing.get("started_at"):
                    started_at = existing["started_at"]
            except (json.JSONDecodeError, OSError):
                pass
        now_iso = _now().isoformat()
        data = {
            "pid": os.getpid(),
            "started_at": started_at,
            "last_heartbeat": now_iso,
        }
        HEARTBEAT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        HEARTBEAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HEARTBEAT_HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": now_iso, "pid": os.getpid()}, ensure_ascii=False) + "\n")


def read_heartbeat_history(limit: int = 500) -> list[dict]:
    """返回最近limit条心跳历史记录，按时间正序（旧到新）——方便下面
    detect_anomaly_periods()按顺序遍历相邻时间戳算间隔。"""
    if not HEARTBEAT_HISTORY_PATH.exists():
        return []
    lines = HEARTBEAT_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return entries[-limit:]


def detect_anomaly_periods(
    history: Optional[list[dict]] = None, gap_threshold_seconds: float = STALE_THRESHOLD_SECONDS
) -> list[dict]:
    """在心跳历史里找出"疑似掉线"的时间段——遍历相邻两条记录，如果间隔超过
    gap_threshold_seconds（默认复用STALE_THRESHOLD_SECONDS，不新增第二个魔数），
    就说明这段时间worker没有写心跳，判定为一次疑似掉线。history参数默认为None时
    调用read_heartbeat_history()读取，显式传入时用传入的值（方便单测不依赖文件IO
    直接构造历史列表）。"""
    if history is None:
        history = read_heartbeat_history()

    anomalies = []
    for prev, curr in zip(history, history[1:]):
        prev_ts = datetime.fromisoformat(prev["timestamp"])
        curr_ts = datetime.fromisoformat(curr["timestamp"])
        gap_seconds = (curr_ts - prev_ts).total_seconds()
        if gap_seconds > gap_threshold_seconds:
            anomalies.append({
                "started_at": prev["timestamp"],
                "ended_at": curr["timestamp"],
                "gap_seconds": gap_seconds,
            })
    return anomalies


def start_heartbeat_thread(interval_seconds: float = 20.0) -> threading.Thread:
    """起一个daemon后台线程，立刻写一次心跳，之后每interval_seconds秒写一次——
    跟reminder_poller.start_polling_thread同款手法，调用方不需要手动start。"""
    stop_event = threading.Event()
    write_heartbeat()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            write_heartbeat()

    thread = threading.Thread(target=_loop, daemon=True)
    thread._stop_event = stop_event  # type: ignore[attr-defined] — 供测试/主动停止时使用
    thread.start()
    return thread


def get_worker_health() -> dict:
    """管理后台调用的只读查询：从没写过心跳文件（worker从未启动过，或是旧版本
    还没有心跳机制）返回"not_running"；心跳存在但超过STALE_THRESHOLD_SECONDS
    没更新（进程可能卡死/崩溃）返回"stale"；否则"healthy"。"""
    if not HEARTBEAT_PATH.exists():
        return {
            "status": "not_running",
            "pid": None,
            "started_at": None,
            "last_heartbeat": None,
            "seconds_since_heartbeat": None,
        }

    try:
        data = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "status": "not_running",
            "pid": None,
            "started_at": None,
            "last_heartbeat": None,
            "seconds_since_heartbeat": None,
        }

    last_heartbeat_raw = data.get("last_heartbeat")
    try:
        last_heartbeat = datetime.fromisoformat(last_heartbeat_raw) if last_heartbeat_raw else None
    except ValueError:
        last_heartbeat = None

    if last_heartbeat is None:
        seconds_since = None
        status = "not_running"
    else:
        seconds_since = (_now() - last_heartbeat).total_seconds()
        status = "healthy" if seconds_since <= STALE_THRESHOLD_SECONDS else "stale"

    return {
        "status": status,
        "pid": data.get("pid"),
        "started_at": data.get("started_at"),
        "last_heartbeat": last_heartbeat_raw,
        "seconds_since_heartbeat": seconds_since,
    }
