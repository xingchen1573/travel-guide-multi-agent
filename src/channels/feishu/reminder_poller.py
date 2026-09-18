"""主动提醒机制v1的触发与推送轮询（docs/ARCHITECTURE.md第14.1节）。

调度器挂在run_feishu_worker.py里（不新增独立进程、不挂管理后台）——run_feishu_worker.py
本来就要保持WS长连接常驻，且推送提醒本身需要用它持有的连接对象主动发消息（不是被动回复
一条已收到的消息），调度和推送在同一进程里天然闭环，不需要跨进程通信。

跟service.py的_start_progress_poller同一种"threading.Thread + daemon=True"手法，
但这里是independent的常驻轮询（每60秒扫一次），不是跟着某一轮对话生命周期走的临时轮询。

send_fn由调用方注入（run_feishu_worker.py传入真实的飞书发送函数），不在这个模块里直接
依赖lark_oapi——跟service.py"业务逻辑不直接依赖SDK，靠注入的ReplyChannel完成发送"是
同一个解耦原则，方便单元测试注入fake发送函数。
"""
from __future__ import annotations

import threading
import traceback
from typing import Callable

from src.channels.feishu import push_log, reminders

REMINDER_PREFIX = "【提醒】"


def run_once(send_fn: Callable[[str, str], None]) -> None:
    """扫一次所有到期的pending提醒，逐条推送。send_fn(chat_id, text)负责真正发送，
    抛异常视为本次推送失败。每条提醒独立try/except——一条推送失败不应该影响其余
    到期任务的推送。每次推送尝试（成功/失败）都记一条推送日志（2026-08-01新增），
    project_id/chat_id在record里已经现成，直接在这里记日志，不需要往send_fn的
    签名里额外塞project_id（那样会牵连run_feishu_worker.py的_send_reminder签名）。"""
    for record in reminders.list_due_reminders():
        text = f"{REMINDER_PREFIX}{record['message']}"
        try:
            send_fn(record["chat_id"], text)
        except Exception as exc:  # noqa: BLE001 — 单条推送失败不能拖垮整轮轮询
            traceback.print_exc()
            push_log.append_push_log(record["chat_id"], record["project_id"], "reminder", False, str(exc))
            reminders.mark_retry_or_failed(record["project_id"], record["id"])
        else:
            push_log.append_push_log(record["chat_id"], record["project_id"], "reminder", True, None)
            reminders.mark_sent(record["project_id"], record["id"])


def start_polling_thread(send_fn: Callable[[str, str], None], interval_seconds: float = 60.0) -> threading.Thread:
    """起一个daemon后台线程，每interval_seconds秒调用一次run_once。返回的Thread对象
    供调用方需要时join/检查存活状态，调用方不需要手动start（已经start好了）。"""
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                run_once(send_fn)
            except Exception:  # noqa: BLE001 — 轮询循环本身不能因为一次异常而整体退出
                traceback.print_exc()

    thread = threading.Thread(target=_loop, daemon=True)
    thread._stop_event = stop_event  # type: ignore[attr-defined] — 供测试/主动停止时使用
    thread.start()
    return thread
