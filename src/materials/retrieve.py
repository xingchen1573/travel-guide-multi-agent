"""素材检索：embedding语义检索 + BM25关键词检索，RRF融合排序（混合检索）。

背景（详见 docs/ISSUES.md）：单纯embedding检索对专有名词精确匹配不擅长——
真实复现过"东郊记忆"这个查询，真正包含该词的chunk因为混杂了太多其他信息，
embedding相似度被稀释排到第25位。BM25基于词频统计，天然擅长这类精确关键词匹配，
两路结果用RRF（Reciprocal Rank Fusion，只看排名不看原始分数，避免两种不同量纲
的分数直接相加没有意义）融合，取长补短。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config import MATERIAL_LIBRARY_DIR, get_embeddings
from src.materials import bm25_search, vector_store
from src.materials.citation_check import _entity_matches

_RRF_K = 60  # RRF平滑常数，业界常用默认值，排名靠前的差异被放大，靠后的被压平

# BM25权重更高：真实复现过"东郊记忆"这类专有名词查询，embedding因为chunk内容混杂
# 多个话题导致相似度被稀释、排序完全失灵，而BM25精确命中词面时排第一——对这种查询，
# BM25信号明显更可信。等权重融合时两路各自"排名第一"的贡献是相等的，会导致
# BM25精确命中的结果被embedding的模糊匹配结果拉低，所以给BM25更高权重，
# 让"精确关键词命中"这个强信号能真正体现出来，不被embedding稀释掉。
_EMBEDDING_WEIGHT = 1.0
_BM25_WEIGHT = 2.0


def _load_consensus_summaries(
    topic: str, library_dir: Path | None = None
) -> list[str]:
    """读取 index/<topic>/insights.json，取出全部confidence="consensus"结论的summary文本。

    容错：文件不存在（素材数<5条从未生成过insights.json）或读取/解析失败时，
    直接返回空列表，不报错、不影响调用方（见docs/ARCHITECTURE.md第8.2节——
    这是软信号，insights.json缺失是正常情况，不是异常）。
    """
    insights_path = (library_dir or MATERIAL_LIBRARY_DIR) / "index" / topic / "insights.json"
    if not insights_path.exists():
        return []
    try:
        data = json.loads(insights_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    summaries = []
    for field in ["must_visit", "food", "pitfalls", "tips", "other"]:
        for item in data.get(field, []) or []:
            if item.get("confidence") == "consensus" and item.get("summary"):
                summaries.append(item["summary"])
    return summaries


def _rrf_fuse(rankings: list[tuple[list[str], float]]) -> list[str]:
    """输入多路排序结果，每路是(chunk_id列表, 权重)，返回融合后的chunk_id顺序。"""
    scores: dict[str, float] = {}
    for ranking, weight in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (_RRF_K + rank + 1)
    return sorted(scores, key=lambda cid: scores[cid], reverse=True)


def _load_scope(
    topic: str,
    scope: str,
    library_dir: Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    root = library_dir or MATERIAL_LIBRARY_DIR
    chunks_path = root / "index" / topic / "chunks.jsonl"
    if not chunks_path.exists():
        return {}, {}

    chunks: dict[str, dict[str, Any]] = {}
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        chunk = json.loads(line)
        internal_id = f"{scope}\x00{chunk['chunk_id']}"
        chunks[internal_id] = {**chunk, "scope": scope, "library_dir": library_dir}

    entries: dict[str, dict[str, Any]] = {}
    entries_path = root / "index" / topic / "entries.jsonl"
    if entries_path.exists():
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            entries[f"{scope}\x00{entry['doc_id']}"] = entry
    return chunks, entries


def retrieve_materials_union(
    topic: str,
    query: str,
    top_k: int = 5,
    *,
    scopes: list[tuple[str, Path | None]],
) -> tuple[list[dict], dict[str, Any]]:
    """Retrieve one logical UGC pool across physically separate material roots.

    BM25 is built once over the combined candidates. Vector indexes remain
    physically separate, but share one query vector and one global ranking.
    Either modality may fail without suppressing results from the other.
    """
    chunk_by_internal_id: dict[str, dict[str, Any]] = {}
    entry_by_internal_id: dict[str, dict[str, Any]] = {}
    for scope, library_dir in scopes:
        chunks, entries = _load_scope(topic, scope, library_dir)
        chunk_by_internal_id.update(chunks)
        entry_by_internal_id.update(entries)

    diagnostics: dict[str, Any] = {
        "status": "empty",
        "modality_errors": {},
        "available_modalities": [],
        "corpus_scopes": {
            scope: sum(
                1 for chunk in chunk_by_internal_id.values() if chunk["scope"] == scope
            )
            for scope, _library_dir in scopes
        },
    }
    if not chunk_by_internal_id:
        return [], diagnostics

    recall_k = max(top_k * 3, 15)
    embedding_hits: list[tuple[str, float]] = []
    try:
        query_vector = get_embeddings().embed_query(query)
        per_scope_hits: list[tuple[str, float]] = []
        successful_embedding_scopes = 0
        for scope, library_dir in scopes:
            try:
                hits = vector_store.search(
                    topic, query_vector, top_k=recall_k, library_dir=library_dir
                )
            except Exception as exc:
                diagnostics["modality_errors"].setdefault("embedding_scopes", {})[
                    scope
                ] = {"type": type(exc).__name__, "message": str(exc)}
                continue
            successful_embedding_scopes += 1
            per_scope_hits.extend(
                (f"{scope}\x00{chunk_id}", score) for chunk_id, score in hits
            )
        embedding_hits = sorted(per_scope_hits, key=lambda item: item[1], reverse=True)[
            :recall_k
        ]
        if successful_embedding_scopes:
            diagnostics["available_modalities"].append("embedding")
        else:
            diagnostics["modality_errors"]["embedding"] = {
                "type": "AllScopesFailed",
                "message": "all configured UGC vector scopes failed",
            }
    except Exception as exc:
        diagnostics["modality_errors"]["embedding"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }

    bm25_hits: list[tuple[str, float]] = []
    try:
        combined_chunks = [
            {"chunk_id": internal_id, "text": chunk["text"]}
            for internal_id, chunk in chunk_by_internal_id.items()
        ]
        bm25_hits = bm25_search.BM25SearchIndex(combined_chunks).search(
            query, top_k=recall_k
        )
        diagnostics["available_modalities"].append("bm25")
    except Exception as exc:
        diagnostics["modality_errors"]["bm25"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }

    if not diagnostics["available_modalities"]:
        diagnostics["status"] = "error"
        return [], diagnostics

    fused_ids = _rrf_fuse(
        [
            ([cid for cid, _ in embedding_hits], _EMBEDDING_WEIGHT),
            ([cid for cid, _ in bm25_hits], _BM25_WEIGHT),
        ]
    )
    embedding_score_by_id = dict(embedding_hits)
    bm25_score_by_id = dict(bm25_hits)
    consensus_by_scope = {
        scope: _load_consensus_summaries(topic, library_dir)
        for scope, library_dir in scopes
    }

    results: list[dict] = []
    for internal_id in fused_ids[:top_k]:
        chunk = chunk_by_internal_id.get(internal_id)
        if not chunk:
            continue
        scope = chunk["scope"]
        entry = entry_by_internal_id.get(f"{scope}\x00{chunk['doc_id']}", {})
        consensus_hint = None
        entities = entry.get("entities", [])
        for entity in entities:
            for summary in consensus_by_scope.get(scope, []):
                if entity and _entity_matches(entity, summary):
                    consensus_hint = summary
                    break
            if consensus_hint:
                break
        results.append(
            {
                "content_snippet": chunk["text"],
                "platform": entry.get("platform", ""),
                "author": entry.get("author", ""),
                "doc_id": chunk["doc_id"],
                "chunk_id": chunk["chunk_id"],
                "entities": entities,
                "title": entry.get("title", ""),
                "score": {
                    "embedding": embedding_score_by_id.get(internal_id),
                    "bm25": bm25_score_by_id.get(internal_id),
                },
                "consensus_hint": consensus_hint,
                "scope": scope,
                "source_url": entry.get("source_url", ""),
            }
        )

    diagnostics["status"] = (
        "degraded"
        if diagnostics["modality_errors"]
        else ("success" if results else "empty")
    )
    diagnostics["result_scopes"] = {
        scope: sum(1 for result in results if result["scope"] == scope)
        for scope, _library_dir in scopes
    }
    return results, diagnostics


def retrieve_materials(
    topic: str,
    query: str,
    top_k: int = 5,
    *,
    library_dir: Path | None = None,
) -> list[dict]:
    """在 index/<topic>/ 下做混合检索（embedding语义 + BM25关键词，RRF融合），
    返回带来源信息的结果。

    返回结构: [{"content_snippet", "platform", "author", "doc_id", "chunk_id", "entities", "title", "score"}]
    """
    scope = "project_supplement" if library_dir is not None else "global_core"
    results, diagnostics = retrieve_materials_union(
        topic,
        query,
        top_k,
        scopes=[(scope, library_dir)],
    )
    if diagnostics["status"] == "error":
        raise RuntimeError(
            "material retrieval failed: "
            + json.dumps(diagnostics["modality_errors"], ensure_ascii=False)
        )
    return results
