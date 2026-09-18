"""Project-scoped supplemental evidence storage and retrieval.

Only records explicitly typed as ``evidence`` and tied to a source URL are
exposed to retrieval/citation. Legacy ``note-*.json`` files and agent working
notes are intentionally outside this layer.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import jieba
from pydantic import BaseModel

from src.config import PROJECTS_DIR
from src.materials.bm25_search import BM25SearchIndex, _clean_tokens
from src.materials.web_evidence import find_web_result_by_url


class ProjectEvidence(BaseModel):
    schema_version: str = "project-evidence-v2"
    note_id: str
    note_type: str = "evidence"
    summary: str
    evidence_excerpt: str
    actor: str
    # Agent work-unit provenance. Older project-evidence-v2 files omit this
    # field and remain readable through the empty-string default.
    log_scope: str = ""
    source_url: str
    source_title: str = ""
    source_query: str = ""
    source_observed_at: str = ""
    source_platform: str = ""
    created_at: str


def _materials_dir(project_id: str, projects_dir: Path | None = None) -> Path:
    return (projects_dir or PROJECTS_DIR) / project_id / "materials"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_source_url(source_url: str) -> bool:
    parsed = urlparse(source_url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def source_url_in_web_log(project_id: str, log_scope: str, source_url: str) -> bool:
    """Return whether this exact URL has a structured result in the scoped log."""
    if not log_scope or not _valid_source_url(source_url):
        return False
    log_path = PROJECTS_DIR / project_id / "agents" / log_scope / "web_search_log.jsonl"
    return find_web_result_by_url(log_path, source_url.strip()) is not None


def write_project_evidence(
    project_id: str,
    *,
    actor: str,
    log_scope: str,
    summary: str,
    source_url: str,
) -> ProjectEvidence:
    """Validate provenance and atomically create one project evidence file."""
    summary = summary.strip()
    source_url = source_url.strip()
    if not summary:
        raise ValueError("evidence summary cannot be empty")
    if not _valid_source_url(source_url):
        raise ValueError("evidence requires an http(s) source_url")
    log_path = PROJECTS_DIR / project_id / "agents" / log_scope / "web_search_log.jsonl"
    source_result = find_web_result_by_url(log_path, source_url)
    if source_result is None:
        raise ValueError(
            "source_url was not found as an exact result in this agent's structured "
            "project web-search log"
        )
    evidence_excerpt = source_result["excerpt"].strip()
    if not evidence_excerpt:
        raise ValueError("the matched web-search result has no evidence excerpt")

    evidence = ProjectEvidence(
        note_id=uuid.uuid4().hex,
        summary=summary,
        evidence_excerpt=evidence_excerpt,
        actor=actor,
        log_scope=log_scope,
        source_url=source_url,
        source_title=source_result["title"],
        source_query=source_result["query"],
        source_observed_at=source_result["observed_at"],
        source_platform=urlparse(source_url).netloc.lower(),
        created_at=_now(),
    )
    materials_dir = _materials_dir(project_id)
    materials_dir.mkdir(parents=True, exist_ok=True)
    final_path = materials_dir / f"evidence-{evidence.note_id}.json"
    temp_path = materials_dir / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False, indent=2)
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, final_path)
        dir_fd = os.open(materials_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return evidence


def list_project_evidence(project_id: str, projects_dir: Path | None = None) -> list[ProjectEvidence]:
    """Load valid v2 evidence; malformed and legacy notes remain non-citable."""
    materials_dir = _materials_dir(project_id, projects_dir)
    if not materials_dir.exists():
        return []
    evidence: list[ProjectEvidence] = []
    for path in sorted(materials_dir.glob("evidence-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            item = ProjectEvidence.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if (
            item.schema_version == "project-evidence-v2"
            and item.note_type == "evidence"
            and _valid_source_url(item.source_url)
            and bool(item.evidence_excerpt.strip())
        ):
            evidence.append(item)
    return evidence


def list_project_evidence_for_log_scope(
    project_id: str,
    log_scope: str,
    projects_dir: Path | None = None,
) -> list[ProjectEvidence]:
    """Return evidence attributable to one Agent work unit.

    New records must match ``log_scope`` exactly. Legacy records without the
    field are accepted only when their exact source URL appears in the target
    work unit's structured Web log. A record carrying another non-empty scope
    is never reassigned through URL fallback.
    """
    if not log_scope:
        return []
    root = projects_dir or PROJECTS_DIR
    log_path = root / project_id / "agents" / log_scope / "web_search_log.jsonl"
    scoped: list[ProjectEvidence] = []
    seen_note_ids: set[str] = set()
    seen_urls: set[str] = set()

    items = list_project_evidence(project_id, root)
    exact_scope = [item for item in items if item.log_scope == log_scope]
    legacy = [
        item
        for item in items
        if not item.log_scope and find_web_result_by_url(log_path, item.source_url) is not None
    ]
    for item in [*exact_scope, *legacy]:
        normalized_url = item.source_url.strip()
        if item.note_id in seen_note_ids or normalized_url in seen_urls:
            continue
        seen_note_ids.add(item.note_id)
        seen_urls.add(normalized_url)
        scoped.append(item)
    return scoped


def load_project_evidence_chunks(
    project_id: str, projects_dir: Path | None = None
) -> dict[str, str]:
    return {
        f"project-evidence-{item.note_id}": "\n".join(
            part for part in (item.source_title, item.evidence_excerpt) if part
        )
        for item in list_project_evidence(project_id, projects_dir)
    }


def retrieve_project_evidence(
    project_id: str,
    query: str,
    top_k: int = 5,
    *,
    log_scope: str | None = None,
) -> list[dict]:
    """Keyword-retrieve citable Web evidence, optionally within one work unit."""
    if not query.strip() or top_k <= 0:
        return []
    items = (
        list_project_evidence_for_log_scope(project_id, log_scope)
        if log_scope
        else list_project_evidence(project_id)
    )
    if not items:
        return []
    chunks = [
        {
            "chunk_id": f"project-evidence-{item.note_id}",
            "text": f"{item.source_title}\n{item.summary}\n{item.evidence_excerpt}",
        }
        for item in items
    ]
    by_chunk_id = {f"project-evidence-{item.note_id}": item for item in items}
    bm25_scores = dict(BM25SearchIndex(chunks).search(query, top_k=max(top_k * 3, 10)))

    query_tokens = set(_clean_tokens(jieba.cut(query)))
    ranked: list[tuple[str, float]] = []
    for chunk in chunks:
        cid = chunk["chunk_id"]
        text = chunk["text"]
        text_tokens = set(_clean_tokens(jieba.cut(text)))
        overlap = len(query_tokens & text_tokens)
        exact = 1 if query.strip() in text else 0
        score = bm25_scores.get(cid, 0.0) + overlap + exact * 5
        if score > 0:
            ranked.append((cid, score))
    ranked.sort(key=lambda row: row[1], reverse=True)

    results: list[dict] = []
    for cid, score in ranked[:top_k]:
        item = by_chunk_id[cid]
        results.append(
            {
                "content_snippet": item.evidence_excerpt,
                "summary": item.summary,
                "platform": "project_evidence",
                "source_platform": item.source_platform,
                "author": "",
                "doc_id": item.note_id,
                "chunk_id": cid,
                "entities": [],
                "title": item.source_title,
                "score": {"project_keyword": score},
                "scope": "project",
                "note_type": "evidence",
                "source_url": item.source_url,
                "log_scope": item.log_scope,
            }
        )
    return results
