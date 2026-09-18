"""主动提醒机制v1（docs/ARCHITECTURE.md第14.1节）：提醒任务的存储+到期查询。

跟bindings.py同级，属于"渠道层调度元数据"——存放在projects/<id>/reminders.json，
不进workspace/、不走src/workspace/store.py的权限校验体系（那套体系管的是"project内部
谁能写公共区文件"，这里是"什么时候该给这个project对应的飞书群推一条消息"，是完全不同
层面的调度信息，定位跟workspace/run_status.json更接近）。

每条记录字段：
- id：uuid4，创建时生成。
- trigger_at：模型算好的绝对时间戳（ISO格式字符串）。重复提醒每次成功推送后会被
  推进到下一个周期，不是固定不变的。
- message：提醒文案。
- chat_id：创建时就固化，不是触发时再反查bindings.json——避免依赖"project_id按
  chat_id确定性生成"这个未来可能变化的隐式推导规则（docs/VISION.md第3.8节"一个群
  多次旅程"一旦落地，这条推导就会失效）。
- project_id：冗余存一份，方便list_due_reminders跨project扫描时知道记录属于哪个project
  （否则调用方还要额外传一次）。
- status：pending / sent / failed / cancelled。
- retry_count：推送失败的累计次数，达到5次改成failed放弃重试（重复提醒同样适用这条
  上限，连续失败大概率是真出了问题，v1不做"跳过这次、继续下个周期"这类更复杂的恢复）。
- created_at：创建时间戳，纯记录用途，不参与任何判断逻辑。
- repeat_interval_hours：可选，非None表示这是一条重复提醒，每次成功推送后trigger_at
  自动推进这么多小时并保持pending，而不是标记为sent；None表示一次性提醒（v1此前唯一
  支持的形态）。
- last_triggered_at：最近一次成功推送的时间戳，None表示还从未成功推送过，纯记录/排查
  用途，不参与判断逻辑。

跨线程/跨轮询的文件读写用同一个全局锁——同一进程内loop worker和create_reminder工具
可能并发访问同一个project的reminders.json，参考bindings.py的threading.Lock模式。
用一把全局锁而不是每个project一把：v1量级下（几个project、几条提醒）没有性能问题，
多把锁反而多一处"锁粒度和文件粒度要对应"的心智负担。

**取消/修改/重复规则（2026-07-26新增，此前明确排入下一阶段，见docs/ARCHITECTURE.md
第14.1节）**：cancel_reminder/update_reminder都只对status=="pending"的记录生效——
已经推送过（sent）、已经放弃重试（failed）、已经取消过（cancelled）的记录不能再被
取消/修改，这是故意的：不做"撤销一条已经发出去的推送"这种没有意义的操作，也不允许
"取消一条已取消的提醒"这类静默no-op被误当成成功，两个函数都返回bool，找不到符合条件
的记录时返回False，调用方（realtime_tools.py）据此给模型/用户明确的失败反馈，不能
假装操作成功了。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.config import PROJECTS_DIR

MAX_RETRY = 5

_lock = threading.Lock()


def _reminders_path(project_id: str) -> Path:
    return PROJECTS_DIR / project_id / "reminders.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_all(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def create_reminder(
    project_id: str,
    chat_id: str,
    trigger_at: str,
    message: str,
    repeat_interval_hours: Optional[float] = None,
) -> dict:
    """新建一条待触发的提醒任务，落盘到projects/<project_id>/reminders.json。

    repeat_interval_hours为None时是一次性提醒（v1此前唯一支持的形态）；非None时是
    重复提醒，每次成功推送后trigger_at会自动推进这么多小时并保持pending，见mark_sent。
    """
    record = {
        "id": str(uuid.uuid4()),
        "trigger_at": trigger_at,
        "message": message,
        "chat_id": chat_id,
        "project_id": project_id,
        "status": "pending",
        "retry_count": 0,
        "created_at": _now(),
        "repeat_interval_hours": repeat_interval_hours,
        "last_triggered_at": None,
    }
    with _lock:
        path = _reminders_path(project_id)
        records = _read_all(path)
        records.append(record)
        _write_all(path, records)
    return record


def list_due_reminders() -> list[dict]:
    """跨所有project目录扫描，找出status=="pending"且trigger_at已过期的任务。

    不建全局索引——当前量级下"扫全部目录"不是瓶颈，额外维护一份索引反而多一处
    "两处数据要保持同步"的bug来源（跟触发与推送这条设计原则一致，见ARCHITECTURE.md
    第14.1节）。trigger_at解析失败的记录跳过不报错，避免一条脏数据挡住其余正常任务
    的扫描。
    """
    if not PROJECTS_DIR.exists():
        return []

    now = datetime.now(timezone.utc)
    due: list[dict] = []
    with _lock:
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            path = project_dir / "reminders.json"
            for record in _read_all(path):
                if record.get("status") != "pending":
                    continue
                try:
                    trigger_at = datetime.fromisoformat(record["trigger_at"])
                except (KeyError, ValueError):
                    continue
                if trigger_at.tzinfo is None:
                    trigger_at = trigger_at.replace(tzinfo=timezone.utc)
                if trigger_at <= now:
                    due.append(record)
    return due


def list_reminders_for_project(project_id: str) -> list[dict]:
    """返回该project下所有status=="pending"的提醒，按trigger_at升序排列——供
    list_reminders工具展示给用户"接下来还有哪些提醒待触发"。已发送/已放弃重试/已取消
    的记录不返回，这个函数是给用户看"未来"的，不是完整历史审计（要查历史直接看
    reminders.json文件本身）。trigger_at解析失败的记录排到最后，不因为一条脏数据
    让整个列表报错。
    """
    with _lock:
        path = _reminders_path(project_id)
        records = [r for r in _read_all(path) if r.get("status") == "pending"]

    def _sort_key(record: dict) -> datetime:
        try:
            parsed = datetime.fromisoformat(record["trigger_at"])
        except (KeyError, ValueError):
            return datetime.max.replace(tzinfo=timezone.utc)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    return sorted(records, key=_sort_key)


def mark_sent(project_id: str, reminder_id: str) -> None:
    """一次性提醒：标记为sent，不再出现在list_due_reminders里。

    重复提醒（repeat_interval_hours非None）：**不**标记为sent，而是把trigger_at推进
    到下一个未来的触发点、retry_count清零、记录last_triggered_at——保持pending，等
    下一个周期自然再次被list_due_reminders捞到。"下一个未来的触发点"用循环累加而不是
    直接"当前时间+interval"：如果worker停机了一段时间导致错过了好几个周期，从原定
    trigger_at按固定间隔推算，不会因为停机时长不是interval的整数倍就产生时间漂移，
    也不会因为"直接从now开始算"而让本该在固定时刻触发的重复提醒（比如"每天8点"）悄悄
    偏移到停机恢复的时间点。
    """
    with _lock:
        path = _reminders_path(project_id)
        records = _read_all(path)
        now = datetime.now(timezone.utc)
        for record in records:
            if record.get("id") != reminder_id:
                continue
            record["last_triggered_at"] = _now()
            interval_hours = record.get("repeat_interval_hours")
            if interval_hours:
                try:
                    next_trigger = datetime.fromisoformat(record["trigger_at"])
                except (KeyError, ValueError):
                    next_trigger = now
                if next_trigger.tzinfo is None:
                    next_trigger = next_trigger.replace(tzinfo=timezone.utc)
                while next_trigger <= now:
                    next_trigger += timedelta(hours=interval_hours)
                record["trigger_at"] = next_trigger.isoformat()
                record["retry_count"] = 0
                # status保持pending，不置为sent——重复提醒不会"结束"
            else:
                record["status"] = "sent"
            break
        _write_all(path, records)


def mark_retry_or_failed(project_id: str, reminder_id: str) -> None:
    """推送失败：重试次数+1，达到MAX_RETRY次后改成failed放弃重试，否则保持pending
    等下一轮（run_feishu_worker.py里60秒一次的轮询）自动重试。重复提醒达到MAX_RETRY
    后同样转failed彻底放弃——v1不做"这个周期失败了、跳过继续等下一个周期"这类更复杂
    的恢复策略，连续失败大概率是真出了问题（比如飞书群被移除机器人），需要人工介入。"""
    with _lock:
        path = _reminders_path(project_id)
        records = _read_all(path)
        for record in records:
            if record.get("id") == reminder_id:
                record["retry_count"] = record.get("retry_count", 0) + 1
                if record["retry_count"] >= MAX_RETRY:
                    record["status"] = "failed"
                break
        _write_all(path, records)


def cancel_reminder(project_id: str, reminder_id: str) -> bool:
    """取消一条待触发的提醒。只对status=="pending"的记录生效——已经推送过/已经放弃
    重试/已经取消过的记录不能再被取消，返回False告知调用方这次取消没有实际生效，
    不能静默假装成功。"""
    with _lock:
        path = _reminders_path(project_id)
        records = _read_all(path)
        found = False
        for record in records:
            if record.get("id") == reminder_id and record.get("status") == "pending":
                record["status"] = "cancelled"
                found = True
                break
        if found:
            _write_all(path, records)
        return found


_UNSET = object()


def update_reminder(
    project_id: str,
    reminder_id: str,
    *,
    trigger_at: Optional[str] = None,
    message: Optional[str] = None,
    repeat_interval_hours=_UNSET,
) -> bool:
    """修改一条待触发提醒的trigger_at/message/repeat_interval_hours，只传要改的字段
    （None/不传表示这个字段不变）。repeat_interval_hours用专门的哨兵值区分"不传"和
    "显式传None清除重复规则、改回一次性提醒"这两种不同意图。只对status=="pending"
    的记录生效，同cancel_reminder的理由，返回bool而不是抛异常/静默no-op。"""
    with _lock:
        path = _reminders_path(project_id)
        records = _read_all(path)
        found = False
        for record in records:
            if record.get("id") == reminder_id and record.get("status") == "pending":
                if trigger_at is not None:
                    record["trigger_at"] = trigger_at
                if message is not None:
                    record["message"] = message
                if repeat_interval_hours is not _UNSET:
                    record["repeat_interval_hours"] = repeat_interval_hours
                found = True
                break
        if found:
            _write_all(path, records)
        return found
