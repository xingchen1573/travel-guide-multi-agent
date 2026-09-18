"""Normalize logs and snapshots without treating them as current-state truth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from src.execution_state import parse_time

DEFAULT_ACTIVITY_WINDOW_SECONDS = 15 * 60.0

COMPLETED_EVENTS = {"completed", "complete", "succeeded", "success", "result_saved"}
ERROR_EVENTS = {"error", "failed", "exception"}
WAITING_USER_EVENTS = {
    "hitl",
    "human_input_required",
    "waiting_user",
    "awaiting_approval",
    "await_skeleton_confirm",
    "await_realtime_approval",
}
BUSINESS_ACTIVITY_EVENTS = {
    "execution_started",
    "step_started",
    "step_progress",
    "progress",
    "tool_started",
    "tool_completed",
    "model_started",
    "model_completed",
}
SHELL_EVENTS = {"received", "recorded", "persisted", "dispatched", "delivered", "accepted"}


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def derive_event_status(
    events: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    activity_window_seconds: float = DEFAULT_ACTIVITY_WINDOW_SECONDS,
) -> str:
    """Project an event history into a display status.

    Receipt/delivery shell events are deliberately insufficient evidence for
    ``running``. A stale unfinished execution is shown as ``interrupted``.
    """
    ordered = sorted(
        (dict(event) for event in events),
        key=lambda event: str(event.get("timestamp") or event.get("created_at") or ""),
    )
    if not ordered:
        return "queued"
    kinds = [str(event.get("event_type") or event.get("type") or event.get("status") or "").lower() for event in ordered]
    for kind in reversed(kinds):
        if kind in COMPLETED_EVENTS:
            return "succeeded"
        if kind in ERROR_EVENTS:
            return "failed"
        if kind in WAITING_USER_EVENTS:
            return "waiting_user"
    business = [event for event, kind in zip(ordered, kinds) if kind in BUSINESS_ACTIVITY_EVENTS]
    if not business:
        return "queued"
    last = business[-1]
    timestamp = parse_time(str(last.get("timestamp") or last.get("created_at") or ""))
    if timestamp and _now(now) - timestamp <= timedelta(seconds=activity_window_seconds):
        return "running"
    return "interrupted"


def normalize_run_status(
    snapshot: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    activity_window_seconds: float = DEFAULT_ACTIVITY_WINDOW_SECONDS,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    result = dict(snapshot)
    events = result.get("events")
    if isinstance(events, list):
        result["status"] = derive_event_status(
            events,
            now=now,
            activity_window_seconds=activity_window_seconds,
        )
        return result

    raw = str(result.get("status") or "").lower()
    node = str(result.get("node") or "").lower()
    if raw in {"done", "completed", "complete", "success", "succeeded"}:
        result["status"] = "succeeded"
        return result
    if raw in {"error", "failed"}:
        result["status"] = "failed"
        return result
    if raw in {"cancelled", "interrupted"}:
        result["status"] = raw
        return result
    if raw in {"waiting_user", "waiting_external", "hitl"} or node.startswith("await_"):
        result["status"] = "waiting_user"
        return result
    if raw != "running":
        return result

    last_activity = parse_time(
        str(
            result.get("last_progress_at")
            or result.get("updated_at")
            or result.get("started_at")
            or ""
        )
    )
    if last_activity is None or _now(now) - last_activity > timedelta(seconds=activity_window_seconds):
        result["status"] = "interrupted"
        result["status_reason"] = "观测快照没有近期活动证据"
        result["health_status"] = "orphaned"
    else:
        result["health_status"] = result.get("health_status") or "healthy"
    return result
