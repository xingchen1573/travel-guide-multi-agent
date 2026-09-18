from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.api import jobs
from src.api.app import create_app
from src.execution_state import JobService, RuntimeManager, configure_execution_context
from src.observability import derive_event_status, normalize_run_status


BASE = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


def _active_job(service: JobService, owner: str, *, progress_at: datetime = BASE) -> str:
    job = service.create_job("demo", "key", now=BASE)
    assert service.start_job(job["job_id"], owner, now=BASE)
    service.update_progress(job["job_id"], now=progress_at)
    return job["job_id"]


def test_runtime_instance_register_heartbeat_and_stop(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    instance_id = service.register_instance(role="backend", environment="test", pid=123, now=BASE)
    assert service.get_instance(instance_id)["heartbeat_at"] == BASE.isoformat()

    heartbeat = BASE + timedelta(seconds=15)
    assert service.heartbeat_instance(instance_id, now=heartbeat)
    assert service.get_instance(instance_id)["heartbeat_at"] == heartbeat.isoformat()

    stopped = BASE + timedelta(seconds=20)
    assert service.stop_instance(instance_id, now=stopped)
    assert service.get_instance(instance_id)["stopped_at"] == stopped.isoformat()


def test_fastapi_lifespan_starts_and_stops_runtime_manager(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    app = create_app(runtime_enabled=True, job_service=service)
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        manager = app.state.runtime_manager
        assert manager.thread is not None and manager.thread.is_alive()
        instance_id = manager.instance_id
        assert service.get_instance(instance_id)["stopped_at"] is None
    assert manager.thread is None
    assert service.get_instance(instance_id)["stopped_at"] is not None


def test_maintenance_thread_runs_periodically_and_joins(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    calls = []
    original = service.reconcile

    def counted(**kwargs):
        calls.append(1)
        return original(**kwargs)

    service.reconcile = counted  # type: ignore[method-assign]
    manager = RuntimeManager(
        service=service,
        enabled=True,
        heartbeat_interval_seconds=0.01,
        reconcile_interval_seconds=0.02,
    )
    manager.start()
    threading.Event().wait(0.06)
    thread = manager.thread
    manager.stop()

    assert len(calls) >= 2
    assert thread is not None and not thread.is_alive()


def test_job_execution_binds_current_owner(isolated_execution_database):
    service = isolated_execution_database
    owner = service.register_instance(role="backend", environment="test")
    configure_execution_context(service, owner)
    jobs.trigger("owner-test", "a", lambda log: {"value": 1})

    for _ in range(100):
        current = service.get_latest_job("owner-test", "a")
        if current and current["status"] == "succeeded":
            break
        threading.Event().wait(0.01)

    assert current["owner_instance_id"] == owner
    assert current["status"] == "succeeded"


def test_stopped_owner_interrupts_job_and_running_steps(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    owner = service.register_instance(role="backend", environment="test", now=BASE)
    job_id = _active_job(service, owner)
    assert service.update_step(job_id, "model", "running", now=BASE)
    service.stop_instance(owner, now=BASE + timedelta(seconds=10))

    result = service.reconcile(now=BASE + timedelta(seconds=11))
    job = service.get_job(job_id)
    assert result["interrupted"] == [job_id]
    assert job["status"] == "interrupted"
    assert job["health_status"] == "orphaned"
    assert job["error_detail"]["retryable"] is True
    assert service.list_steps(job_id)[0]["status"] == "interrupted"


def test_expired_owner_heartbeat_interrupts_job(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    owner = service.register_instance(role="backend", environment="test", now=BASE)
    job_id = _active_job(service, owner)
    service.reconcile(now=BASE + timedelta(seconds=61), owner_timeout_seconds=60)
    assert service.get_job(job_id)["status"] == "interrupted"


def test_live_owner_without_progress_marks_job_stalled(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    owner = service.register_instance(role="backend", environment="test", now=BASE)
    job_id = _active_job(service, owner)
    service.heartbeat_instance(owner, now=BASE + timedelta(minutes=16))
    service.reconcile(
        now=BASE + timedelta(minutes=16),
        owner_timeout_seconds=60,
        stalled_after_seconds=15 * 60,
    )
    job = service.get_job(job_id)
    assert job["status"] == "running"
    assert job["health_status"] == "stalled"


def test_legacy_job_without_owner_is_interrupted_after_grace(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    job = service.create_job("legacy", "old", now=BASE)
    service.reconcile(
        now=BASE + timedelta(hours=25),
        legacy_owner_grace_seconds=24 * 60 * 60,
    )
    assert service.get_job(job["job_id"])["status"] == "interrupted"


def test_interrupted_terminal_state_rejects_late_thread_updates(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    owner = service.register_instance(role="backend", environment="test", now=BASE)
    job_id = _active_job(service, owner)
    assert service.interrupt_job(job_id, "owner lost", now=BASE + timedelta(seconds=1))

    assert not service.update_progress(job_id, now=BASE + timedelta(seconds=2))
    assert not service.succeed_job(job_id, {"late": True}, now=BASE + timedelta(seconds=3))
    job = service.get_job(job_id)
    assert job["status"] == "interrupted"
    assert "late" not in job


def test_reconciliation_is_idempotent(tmp_path):
    service = JobService(tmp_path / "state.sqlite3")
    owner = service.register_instance(role="backend", environment="test", now=BASE)
    job_id = _active_job(service, owner)
    service.stop_instance(owner, now=BASE + timedelta(seconds=1))

    first = service.reconcile(now=BASE + timedelta(seconds=2))
    snapshot = service.get_job(job_id)
    second = service.reconcile(now=BASE + timedelta(seconds=3))
    assert first["interrupted"] == [job_id]
    assert second["interrupted"] == []
    assert service.get_job(job_id) == snapshot


def test_hitl_observation_maps_to_waiting_user():
    assert derive_event_status([{"event_type": "human_input_required", "timestamp": BASE.isoformat()}]) == "waiting_user"
    assert normalize_run_status({"node": "await_skeleton_confirm", "status": "running"})["status"] == "waiting_user"


def test_stale_unfinished_observation_maps_to_interrupted():
    status = normalize_run_status(
        {"node": "route_agent", "status": "running", "started_at": BASE.isoformat()},
        now=BASE + timedelta(minutes=16),
    )
    assert status["status"] == "interrupted"
    assert status["health_status"] == "orphaned"


def test_shell_events_do_not_imply_running():
    events = [
        {"event_type": "received", "timestamp": BASE.isoformat()},
        {"event_type": "delivered", "timestamp": (BASE + timedelta(seconds=1)).isoformat()},
    ]
    assert derive_event_status(events, now=BASE + timedelta(seconds=2)) == "queued"


def test_running_observation_without_timestamp_is_not_treated_as_live():
    status = normalize_run_status({"node": "route_agent", "status": "running"}, now=BASE)
    assert status["status"] == "interrupted"
