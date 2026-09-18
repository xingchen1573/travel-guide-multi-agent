from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from travel_guide_kernel.paths import RUNTIME_ROOT_ENV, resolve_runtime_root


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_path_probe(
    tmp_path: Path, runtime_root: str, *, write_project: bool = False
) -> dict[str, object]:
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    project_id = f"runtime-path-probe-{tmp_path.name}"
    script = """
import json
import os

import run_feishu_worker
from src import config, llm_trace
from src.api import key_store
from src.channels.feishu import bindings, health, push_log
from src.execution_state import default_database_path
from src.experiments import index as experiment_index
from src.workspace import store

project_id = os.environ["RUNTIME_PATH_TEST_PROJECT_ID"]
if os.environ["RUNTIME_PATH_TEST_WRITE_PROJECT"] == "1":
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "route_agent")
    llm_trace.append_trace({"ok": True})

print(json.dumps({
    "code_root": str(config.CODE_ROOT),
    "project_root": str(config.PROJECT_ROOT),
    "runtime_root": str(config.RUNTIME_ROOT),
    "data_dir": str(config.DATA_DIR),
    "runtime_dir": str(config.RUNTIME_DIR),
    "projects_dir": str(config.PROJECTS_DIR),
    "material_library_dir": str(config.MATERIAL_LIBRARY_DIR),
    "agents_dir": str(config.AGENTS_DIR),
    "key_store_path": str(key_store.KEY_STORE_PATH),
    "bindings_path": str(bindings.BINDINGS_PATH),
    "heartbeat_path": str(health.HEARTBEAT_PATH),
    "push_log_path": str(push_log.PUSH_LOG_PATH),
    "execution_db": str(default_database_path()),
    "experiment_db": str(experiment_index.DEFAULT_DB_PATH),
    "worker_lock": str(run_feishu_worker.LOCK_PATH),
    "project_exists": store.project_dir(project_id).is_dir(),
    "checkout_project_exists": (config.CODE_ROOT / "projects" / project_id).is_dir(),
    "trace_exists": (
        config.PROJECTS_DIR
        / project_id
        / "agents"
        / "route_agent"
        / "trace.jsonl"
    ).is_file(),
}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env[RUNTIME_ROOT_ENV] = runtime_root
    env["RUNTIME_PATH_TEST_PROJECT_ID"] = project_id
    env["RUNTIME_PATH_TEST_WRITE_PROJECT"] = "1" if write_project else "0"
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("EXECUTION_STATE_DB", None)
    env.pop("EXPERIMENT_INDEX_DB", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=launch_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_resolve_runtime_root_defaults_to_code_root(tmp_path):
    assert resolve_runtime_root(tmp_path, {}) == tmp_path.resolve()


def test_default_layout_remains_inside_checkout(tmp_path):
    result = _run_path_probe(tmp_path, "")

    assert result["code_root"] == str(REPO_ROOT)
    assert result["project_root"] == str(REPO_ROOT)
    assert result["runtime_root"] == str(REPO_ROOT)
    assert result["data_dir"] == str(REPO_ROOT / "data")
    assert result["runtime_dir"] == str(REPO_ROOT / "runtime")
    assert result["projects_dir"] == str(REPO_ROOT / "projects")
    assert result["material_library_dir"] == str(
        REPO_ROOT / "data" / "material_library"
    )
    assert result["agents_dir"] == str(REPO_ROOT / "src" / "agents")
    assert result["project_exists"] is False
    assert result["checkout_project_exists"] is False
    assert result["trace_exists"] is False


def test_external_runtime_root_moves_writable_state_and_ignores_cwd(tmp_path):
    runtime_root = (tmp_path / "external-runtime").resolve()
    result = _run_path_probe(tmp_path, str(runtime_root), write_project=True)

    assert result["code_root"] == str(REPO_ROOT)
    assert result["project_root"] == str(REPO_ROOT)
    assert result["agents_dir"] == str(REPO_ROOT / "src" / "agents")
    assert result["runtime_root"] == str(runtime_root)
    assert result["data_dir"] == str(runtime_root / "data")
    assert result["runtime_dir"] == str(runtime_root / "runtime")
    assert result["projects_dir"] == str(runtime_root / "projects")
    assert result["material_library_dir"] == str(
        runtime_root / "data" / "material_library"
    )
    assert result["key_store_path"] == str(
        runtime_root / "data" / "runtime_config" / "api_keys.json"
    )
    assert result["bindings_path"] == str(
        runtime_root / "data" / "feishu" / "bindings.json"
    )
    assert result["heartbeat_path"] == str(
        runtime_root / "data" / "feishu" / "worker_heartbeat.json"
    )
    assert result["push_log_path"] == str(
        runtime_root / "data" / "feishu" / "push_log.jsonl"
    )
    assert result["execution_db"] == str(
        runtime_root / "runtime" / "execution_state.sqlite3"
    )
    assert result["experiment_db"] == str(
        runtime_root / "runtime" / "experiment_index.sqlite3"
    )
    assert result["worker_lock"] == str(runtime_root / "data" / "feishu_ws_worker.lock")
    assert result["project_exists"] is True
    assert result["checkout_project_exists"] is False
    assert result["trace_exists"] is True
