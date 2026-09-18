"""SQLite-backed execution state, runtime leases, and orphan reconciliation.

The database is the source of truth for current lifecycle state. Human-readable
logs and project artifacts remain historical evidence and never keep a job
alive by themselves.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import RUNTIME_DIR

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
DEFAULT_RECONCILE_INTERVAL_SECONDS = 60.0
DEFAULT_OWNER_TIMEOUT_SECONDS = 60.0
DEFAULT_STALLED_AFTER_SECONDS = 15 * 60.0
DEFAULT_LEGACY_OWNER_GRACE_SECONDS = 24 * 60 * 60.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def running_under_pytest() -> bool:
    return (
        any(name == "pytest" or name.startswith("_pytest") for name in sys.modules)
        or bool(os.getenv("PYTEST_CURRENT_TEST"))
        or any("pytest" in argument.lower() for argument in sys.argv[:2])
    )


def default_database_path() -> Path:
    configured = os.getenv("EXECUTION_STATE_DB")
    if configured:
        return Path(configured).expanduser()
    if running_under_pytest():
        root = Path(tempfile.gettempdir()) / f"travel-agent-execution-{os.getpid()}"
        return root / "execution_state.sqlite3"
    return RUNTIME_DIR / "execution_state.sqlite3"


class JobConflictError(RuntimeError):
    """Raised when the same logical task already has an active execution."""


class JobService:
    """Small SQLite service with one connection per operation.

    Connections are intentionally short-lived. WAL plus ``BEGIN IMMEDIATE`` for
    state transitions follows the project's lightweight threaded concurrency
    model without sharing a connection across worker threads.
    """

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or default_database_path())
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.database_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def ensure_schema(self) -> None:
        with self._schema_lock:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_instances (
                        instance_id TEXT PRIMARY KEY,
                        role TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        pid INTEGER NOT NULL,
                        started_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        stopped_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        task_type TEXT NOT NULL,
                        task_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        owner_instance_id TEXT,
                        status_reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        last_heartbeat_at TEXT,
                        last_progress_at TEXT,
                        finished_at TEXT,
                        health_status TEXT NOT NULL DEFAULT 'healthy',
                        environment TEXT NOT NULL DEFAULT 'default',
                        attempt INTEGER NOT NULL DEFAULT 1,
                        retry_of_job_id TEXT,
                        result_json TEXT,
                        error_json TEXT,
                        log_json TEXT NOT NULL DEFAULT '[]'
                    );

                    CREATE TABLE IF NOT EXISTS job_steps (
                        job_id TEXT NOT NULL,
                        step_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        status_reason TEXT,
                        started_at TEXT,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT,
                        PRIMARY KEY (job_id, step_key),
                        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_runtime_instances_heartbeat
                        ON runtime_instances(heartbeat_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_active
                        ON jobs(status, owner_instance_id, updated_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_logical
                        ON jobs(task_type, task_key, created_at DESC);
                    """
                )
                self._ensure_columns(
                    conn,
                    "runtime_instances",
                    {
                        "role": "TEXT NOT NULL DEFAULT 'backend'",
                        "environment": "TEXT NOT NULL DEFAULT 'default'",
                        "pid": "INTEGER NOT NULL DEFAULT 0",
                        "started_at": "TEXT NOT NULL DEFAULT ''",
                        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
                        "stopped_at": "TEXT",
                    },
                )
                conn.execute("UPDATE jobs SET status = 'succeeded' WHERE status = 'success'")
                self._ensure_columns(
                    conn,
                    "jobs",
                    {
                        "owner_instance_id": "TEXT",
                        "status_reason": "TEXT",
                        "started_at": "TEXT",
                        "last_heartbeat_at": "TEXT",
                        "last_progress_at": "TEXT",
                        "finished_at": "TEXT",
                        "health_status": "TEXT NOT NULL DEFAULT 'healthy'",
                        "environment": "TEXT NOT NULL DEFAULT 'default'",
                        "attempt": "INTEGER NOT NULL DEFAULT 1",
                        "retry_of_job_id": "TEXT",
                        "result_json": "TEXT",
                        "error_json": "TEXT",
                        "log_json": "TEXT NOT NULL DEFAULT '[]'",
                        "created_at": "TEXT NOT NULL DEFAULT ''",
                        "updated_at": "TEXT NOT NULL DEFAULT ''",
                    },
                )

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def register_instance(
        self,
        *,
        role: str,
        environment: str,
        pid: int | None = None,
        instance_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        current = isoformat(now)
        instance_id = instance_id or f"{role}-{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO runtime_instances
                   (instance_id, role, environment, pid, started_at, heartbeat_at, stopped_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (instance_id, role, environment, pid or os.getpid(), current, current),
            )
        return instance_id

    def heartbeat_instance(self, instance_id: str, *, now: datetime | None = None) -> bool:
        current = isoformat(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE runtime_instances SET heartbeat_at = ?
                   WHERE instance_id = ? AND stopped_at IS NULL""",
                (current, instance_id),
            ).rowcount
            if changed:
                conn.execute(
                    """UPDATE jobs SET last_heartbeat_at = ?, updated_at = ?
                       WHERE owner_instance_id = ? AND status IN ('queued', 'running')""",
                    (current, current, instance_id),
                )
            conn.commit()
        return bool(changed)

    def stop_instance(self, instance_id: str, *, now: datetime | None = None) -> bool:
        current = isoformat(now)
        with self._connect() as conn:
            changed = conn.execute(
                """UPDATE runtime_instances
                   SET stopped_at = COALESCE(stopped_at, ?), heartbeat_at = ?
                   WHERE instance_id = ?""",
                (current, current, instance_id),
            ).rowcount
        return bool(changed)

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_job(
        self,
        task_type: str,
        task_key: str,
        *,
        environment: str = "default",
        retry_of_job_id: str | None = None,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = isoformat(now)
        job_id = job_id or f"job-{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """SELECT job_id FROM jobs
                   WHERE task_type = ? AND task_key = ? AND status IN ('queued', 'running')
                   LIMIT 1""",
                (task_type, task_key),
            ).fetchone()
            if active:
                conn.rollback()
                raise JobConflictError(f"任务 {task_type}:{task_key!r} 已经在执行中")
            previous_attempt = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS value FROM jobs WHERE task_type = ? AND task_key = ?",
                (task_type, task_key),
            ).fetchone()["value"]
            conn.execute(
                """INSERT INTO jobs
                   (job_id, task_type, task_key, status, owner_instance_id, status_reason,
                    created_at, updated_at, started_at, last_heartbeat_at,
                    last_progress_at, finished_at, health_status, environment, attempt,
                    retry_of_job_id, result_json, error_json, log_json)
                   VALUES (?, ?, ?, 'queued', NULL, NULL, ?, ?, NULL, NULL, ?, NULL,
                           'healthy', ?, ?, ?, NULL, NULL, '[]')""",
                (
                    job_id,
                    task_type,
                    task_key,
                    current,
                    current,
                    current,
                    environment,
                    int(previous_attempt) + 1,
                    retry_of_job_id,
                ),
            )
            conn.commit()
        return self.get_job(job_id) or {}

    def start_job(
        self,
        job_id: str,
        owner_instance_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = isoformat(now)
        with self._connect() as conn:
            changed = conn.execute(
                """UPDATE jobs
                   SET status = 'running', owner_instance_id = ?, started_at = COALESCE(started_at, ?),
                       last_heartbeat_at = ?, last_progress_at = ?, updated_at = ?,
                       health_status = 'healthy', status_reason = NULL
                   WHERE job_id = ? AND status = 'queued'""",
                (owner_instance_id, current, current, current, current, job_id),
            ).rowcount
        return bool(changed)

    def append_log(self, job_id: str, message: str, *, now: datetime | None = None) -> bool:
        current = isoformat(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT log_json FROM jobs WHERE job_id = ? AND status IN ('queued', 'running')",
                (job_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            try:
                logs = json.loads(row["log_json"] or "[]")
            except json.JSONDecodeError:
                logs = []
            logs.append(str(message))
            conn.execute(
                """UPDATE jobs SET log_json = ?, last_progress_at = ?, updated_at = ?,
                   health_status = 'healthy' WHERE job_id = ? AND status IN ('queued', 'running')""",
                (json.dumps(logs, ensure_ascii=False), current, current, job_id),
            )
            conn.commit()
        return True

    def update_progress(
        self,
        job_id: str,
        *,
        status_reason: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        current = isoformat(now)
        with self._connect() as conn:
            changed = conn.execute(
                """UPDATE jobs SET last_progress_at = ?, updated_at = ?, health_status = 'healthy',
                   status_reason = COALESCE(?, status_reason)
                   WHERE job_id = ? AND status IN ('queued', 'running')""",
                (current, current, status_reason, job_id),
            ).rowcount
        return bool(changed)

    def update_step(
        self,
        job_id: str,
        step_key: str,
        status: str,
        *,
        status_reason: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        current = isoformat(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ? AND status IN ('queued', 'running')",
                (job_id,),
            ).fetchone()
            if active is None:
                conn.rollback()
                return False
            conn.execute(
                """INSERT INTO job_steps
                   (job_id, step_key, status, status_reason, started_at, updated_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, step_key) DO UPDATE SET
                     status = excluded.status,
                     status_reason = excluded.status_reason,
                     updated_at = excluded.updated_at,
                     finished_at = excluded.finished_at""",
                (
                    job_id,
                    step_key,
                    status,
                    status_reason,
                    current if status == "running" else None,
                    current,
                    current if status in TERMINAL_STATUSES else None,
                ),
            )
            conn.execute(
                "UPDATE jobs SET last_progress_at = ?, updated_at = ?, health_status = 'healthy' WHERE job_id = ?",
                (current, current, job_id),
            )
            conn.commit()
        return True

    def succeed_job(
        self, job_id: str, result: dict[str, Any] | None = None, *, now: datetime | None = None
    ) -> bool:
        return self._finish_job(job_id, "succeeded", result=result, now=now)

    def fail_job(
        self, job_id: str, error: dict[str, Any] | str, *, now: datetime | None = None
    ) -> bool:
        payload = error if isinstance(error, dict) else {"message": str(error), "retryable": False}
        return self._finish_job(job_id, "failed", error=payload, now=now)

    def cancel_job(self, job_id: str, reason: str, *, now: datetime | None = None) -> bool:
        return self._finish_job(job_id, "cancelled", reason=reason, now=now)

    def interrupt_job(self, job_id: str, reason: str, *, now: datetime | None = None) -> bool:
        error = {"kind": "interrupted", "message": reason, "retryable": True}
        return self._finish_job(
            job_id,
            "interrupted",
            reason=reason,
            error=error,
            health_status="orphaned",
            now=now,
        )

    def _finish_job(
        self,
        job_id: str,
        status: str,
        *,
        reason: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        health_status: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"不是终态: {status}")
        current = isoformat(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE jobs SET status = ?, status_reason = ?, result_json = ?, error_json = ?,
                   finished_at = ?, updated_at = ?, health_status = COALESCE(?, health_status)
                   WHERE job_id = ? AND status IN ('queued', 'running')""",
                (
                    status,
                    reason,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    json.dumps(error, ensure_ascii=False) if error is not None else None,
                    current,
                    current,
                    health_status,
                    job_id,
                ),
            ).rowcount
            if changed and status == "interrupted":
                conn.execute(
                    """UPDATE job_steps SET status = 'interrupted', status_reason = ?,
                       finished_at = ?, updated_at = ?
                       WHERE job_id = ? AND status = 'running'""",
                    (reason, current, current, job_id),
                )
            conn.commit()
        return bool(changed)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._deserialize_job(row) if row else None

    def get_latest_job(self, task_type: str, task_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs WHERE task_type = ? AND task_key = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (task_type, task_key),
            ).fetchone()
        return self._deserialize_job(row) if row else None

    def list_steps(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_steps WHERE job_id = ? ORDER BY started_at, step_key", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_active_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        return [self._deserialize_job(row) for row in rows]

    def _deserialize_job(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target, fallback in (
            ("result_json", "result", None),
            ("error_json", "error_detail", None),
            ("log_json", "log", []),
        ):
            try:
                item[target] = json.loads(item.get(source) or "null")
            except json.JSONDecodeError:
                item[target] = fallback
        error_detail = item.get("error_detail")
        item["error"] = error_detail.get("message") if isinstance(error_detail, dict) else None
        item["key"] = item["task_key"]
        result = item.pop("result", None)
        if isinstance(result, dict):
            item.update(result)
        item.pop("result_json", None)
        item.pop("error_json", None)
        item.pop("log_json", None)
        return item

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        owner_timeout_seconds: float = DEFAULT_OWNER_TIMEOUT_SECONDS,
        stalled_after_seconds: float = DEFAULT_STALLED_AFTER_SECONDS,
        legacy_owner_grace_seconds: float = DEFAULT_LEGACY_OWNER_GRACE_SECONDS,
    ) -> dict[str, list[str]]:
        """Two-phase, idempotent reconciliation of active jobs."""
        current = now or utc_now()
        active = self.list_active_jobs()
        owners = {
            owner_id: self.get_instance(owner_id)
            for owner_id in {job.get("owner_instance_id") for job in active if job.get("owner_instance_id")}
        }
        interrupts: list[tuple[str, str]] = []
        health_updates: list[tuple[str, str]] = []

        for job in active:
            owner_id = job.get("owner_instance_id")
            if not owner_id:
                last = parse_time(job.get("updated_at") or job.get("created_at"))
                if last and current - last > timedelta(seconds=legacy_owner_grace_seconds):
                    interrupts.append((job["job_id"], "历史活动任务没有 owner，已超过升级宽限期"))
                continue
            owner = owners.get(owner_id)
            if owner is None:
                interrupts.append((job["job_id"], f"owner 实例不存在: {owner_id}"))
                continue
            if owner.get("stopped_at"):
                interrupts.append((job["job_id"], f"owner 实例已正常停止: {owner_id}"))
                continue
            heartbeat = parse_time(owner.get("heartbeat_at"))
            if heartbeat is None or current - heartbeat > timedelta(seconds=owner_timeout_seconds):
                interrupts.append((job["job_id"], f"owner 心跳超过 {int(owner_timeout_seconds)} 秒: {owner_id}"))
                continue
            progress = parse_time(job.get("last_progress_at") or job.get("started_at"))
            if job["status"] == "running" and progress and current - progress > timedelta(seconds=stalled_after_seconds):
                health_updates.append((job["job_id"], "stalled"))
            else:
                health_updates.append((job["job_id"], "healthy"))

        interrupted: list[str] = []
        for job_id, reason in interrupts:
            if self.interrupt_job(job_id, reason, now=current):
                interrupted.append(job_id)

        updated_health: list[str] = []
        if health_updates:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for job_id, health in health_updates:
                    changed = conn.execute(
                        """UPDATE jobs SET health_status = ?, updated_at = ?
                           WHERE job_id = ? AND status IN ('queued', 'running')""",
                        (health, isoformat(current), job_id),
                    ).rowcount
                    if changed:
                        updated_health.append(job_id)
                conn.commit()
        return {"interrupted": interrupted, "health_updated": updated_health}

    def clear_all(self) -> None:
        """Test helper; production code must never use this."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM job_steps")
            conn.execute("DELETE FROM jobs")
            conn.execute("DELETE FROM runtime_instances")
            conn.commit()


_context_lock = threading.Lock()
_current_service: JobService | None = None
_current_instance_id: str | None = None


def get_job_service() -> JobService:
    global _current_service
    with _context_lock:
        if _current_service is None:
            _current_service = JobService()
        return _current_service


def configure_execution_context(service: JobService, instance_id: str | None) -> None:
    global _current_service, _current_instance_id
    with _context_lock:
        _current_service = service
        _current_instance_id = instance_id


def get_current_instance_id() -> str | None:
    with _context_lock:
        return _current_instance_id


@dataclass
class RuntimeManager:
    service: JobService
    role: str = "backend"
    environment: str = "default"
    enabled: bool = True
    coordinator: bool = True
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS
    owner_timeout_seconds: float = DEFAULT_OWNER_TIMEOUT_SECONDS
    stalled_after_seconds: float = DEFAULT_STALLED_AFTER_SECONDS
    legacy_owner_grace_seconds: float = DEFAULT_LEGACY_OWNER_GRACE_SECONDS

    def __post_init__(self) -> None:
        self.instance_id: str | None = None
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or self.thread is not None:
            return
        self.stop_event.clear()
        self.instance_id = self.service.register_instance(
            role=self.role,
            environment=self.environment,
        )
        configure_execution_context(self.service, self.instance_id)
        if self.coordinator:
            self._reconcile()
        self.thread = threading.Thread(
            target=self._run,
            name=f"execution-maintenance-{self.role}",
            daemon=True,
        )
        self.thread.start()

    def _reconcile(self) -> None:
        self.service.reconcile(
            owner_timeout_seconds=self.owner_timeout_seconds,
            stalled_after_seconds=self.stalled_after_seconds,
            legacy_owner_grace_seconds=self.legacy_owner_grace_seconds,
        )

    def _run(self) -> None:
        next_reconcile = time.monotonic() + self.reconcile_interval_seconds
        while not self.stop_event.wait(self.heartbeat_interval_seconds):
            if not self.instance_id:
                return
            self.service.heartbeat_instance(self.instance_id)
            if self.coordinator and time.monotonic() >= next_reconcile:
                self._reconcile()
                next_reconcile = time.monotonic() + self.reconcile_interval_seconds

    def stop(self) -> None:
        if not self.enabled or not self.instance_id:
            return
        self.stop_event.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.heartbeat_interval_seconds * 2))
        self.service.stop_instance(self.instance_id)
        if self.coordinator:
            self._reconcile()
        configure_execution_context(self.service, None)
        self.thread = None


def maintenance_enabled_by_default() -> bool:
    raw = os.getenv("EXECUTION_MAINTENANCE_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return not running_under_pytest()


def environment_name() -> str:
    return os.getenv("APP_ENVIRONMENT", os.getenv("ENVIRONMENT", "default"))
