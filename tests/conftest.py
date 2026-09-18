"""pytest共享fixture/辅助函数。

fake_agent_reply：orchestrator.py的_invoke_with_write_verification/_invoke_with_capture_verification
（2026-07-21修复，见docs/ISSUES.md）现在依赖invoke_with_retry的返回值里有"messages"这个key
（累积对话历史，供重试时携带完整上下文）——真实的create_react_agent.invoke()本来就返回
这个结构，这里给测试里monkeypatch的fake invoke_with_retry函数提供一个统一的构造方式，
避免每个测试各自拼一遍。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# conftest is loaded before test modules are collected. Set these defaults here
# so module-level ``TestClient(app)`` objects cannot touch the personal runtime
# database or start the automatic maintenance thread during collection.
_EXECUTION_TEST_DIR = tempfile.TemporaryDirectory(prefix="travel-agent-pytest-execution-")
os.environ.setdefault(
    "EXECUTION_STATE_DB",
    str(Path(_EXECUTION_TEST_DIR.name) / "execution_state.sqlite3"),
)
os.environ.setdefault("EXECUTION_MAINTENANCE_ENABLED", "false")


@pytest.fixture(autouse=True)
def isolated_execution_database(tmp_path):
    """Every pytest case gets a temporary execution database.

    This also ensures imported TestClient modules never reconcile the personal
    ``runtime/execution_state.sqlite3`` file.
    """
    from src.api import jobs
    from src.execution_state import JobService, configure_execution_context

    service = JobService(tmp_path / "execution_state.sqlite3")
    configure_execution_context(service, None)
    jobs._jobs.clear()
    yield service
    jobs._jobs.clear()
    configure_execution_context(service, None)


def fake_agent_reply(messages: list, note: str = "ok") -> dict:
    """模拟react-agent.invoke()的返回值：把新的一条assistant回复追加到传入的messages后面。"""
    return {"messages": list(messages) + [{"role": "assistant", "content": note}]}
