"""Append-only provider call telemetry shared by both orchestration harnesses."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import PROJECTS_DIR


def record_provider_call(
    project_id: str,
    *,
    provider: str,
    operation: str,
    status: str,
    duration_ms: int | None = None,
    log_scope: str = "system",
    metadata: dict[str, Any] | None = None,
    projects_dir: Path | None = None,
) -> None:
    if not project_id:
        return
    path = (projects_dir or PROJECTS_DIR) / project_id / "agents" / log_scope / "provider_calls.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": "provider-call-v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "operation": operation,
        "status": status,
        "duration_ms": duration_ms,
        "metadata": metadata or {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
