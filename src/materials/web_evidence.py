"""Structured web-search evidence shared by tools and citation readers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


WEB_SEARCH_LOG_SCHEMA_VERSION = "web-search-log-v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_web_results(results: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        normalized.append(
            {
                "title": str(raw.get("title") or "").strip(),
                "excerpt": str(raw.get("excerpt") or raw.get("content") or "").strip(),
                "url": str(raw.get("url") or "").strip(),
            }
        )
    return normalized


def build_web_search_log_entry(query: str, results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": WEB_SEARCH_LOG_SCHEMA_VERSION,
        "query": query,
        "created_at": _now(),
        "results": normalize_web_results(results),
    }


def render_web_results(results: Iterable[dict[str, Any]]) -> str:
    lines = []
    for result in normalize_web_results(results):
        lines.append(
            f"- {result['title']}：{result['excerpt']}（来源：{result['url']}）"
        )
    return "\n".join(lines)


def render_web_search_log_entry(entry: dict[str, Any]) -> str:
    """Render v2 logs and retain read compatibility with legacy v1 logs."""
    if entry.get("schema_version") == WEB_SEARCH_LOG_SCHEMA_VERSION:
        return render_web_results(entry.get("results") or [])
    return str(entry.get("result") or "")


def find_web_result_by_url(log_path: Path, source_url: str) -> dict[str, str] | None:
    """Return the newest exact URL match from a structured v2 log only."""
    if not log_path.exists():
        return None
    lines = log_path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (OSError, ValueError):
            continue
        if entry.get("schema_version") != WEB_SEARCH_LOG_SCHEMA_VERSION:
            continue
        for result in reversed(normalize_web_results(entry.get("results") or [])):
            if result["url"] == source_url:
                return {
                    **result,
                    "query": str(entry.get("query") or ""),
                    "observed_at": str(entry.get("created_at") or ""),
                }
    return None


def find_latest_web_search_entry(log_path: Path, query: str) -> dict[str, Any] | None:
    """Return the newest structured log entry for an exact query."""
    if not log_path.exists():
        return None
    for line in reversed(log_path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (OSError, ValueError):
            continue
        if (
            entry.get("schema_version") == WEB_SEARCH_LOG_SCHEMA_VERSION
            and str(entry.get("query") or "") == query
        ):
            return {
                "query": query,
                "created_at": str(entry.get("created_at") or ""),
                "results": normalize_web_results(entry.get("results") or []),
            }
    return None
