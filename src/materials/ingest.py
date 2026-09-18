"""素材入库：raw/<topic>/ -> index/<topic>/entries.jsonl + chunks.jsonl + embeddings/

对应 docs/ARCHITECTURE.md 第8.1节：单条元数据提取，不做跨条目比对，append-only。
每条素材：①LLM结构化实体抽取（entries.jsonl）；②按平台选择分段策略切成chunk（chunks.jsonl）；
③对每个chunk做embedding、写入FAISS索引（vector_store.py），检索时按chunk级返回，
可以追溯到具体是哪一段文字支撑了某条建议，不再是"参考了整篇文章"这种粗粒度。
"""
from __future__ import annotations

import fcntl
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from src.config import MATERIAL_LIBRARY_DIR, get_embeddings, get_structured_chat_model
from src.materials import vector_store
from src.materials.chunkers import chunk_content
from src.materials.citation_check import _entity_matches
from src.structured_output import bind_structured_json
from src.workspace.schema import MaterialChunk, MaterialEntry


_MIN_CONTENT_LENGTH = 20  # 内容长度门禁阈值（docs/ARCHITECTURE.md第8.1节），只挡明显无意义的极短内容


class _ContentAnalysis(BaseModel):
    """LLM结构化输出：是否相关判断 + 实体抽取，同一次调用完成，不新增LLM成本。"""

    is_relevant: bool = Field(
        description="这段内容是否真的是关于目的地的旅游/生活类真实经验分享。"
        "如果主要讲的是别的城市/地区、是网络热梗或影视吐槽段子剪辑、是纯广告软文，则为false"
    )
    reason: str = Field(description="判断理由，20字以内")
    entities: list[str] = Field(
        description="尽量列出文本中提到的所有具体地名/店名/景点名，不要只挑一两个最显眼的，"
        "但只能是文本中确实出现过的词，不要因为追求数量而编造或引申，去掉泛称，如目的地城市本身不算"
    )


def _analyze_content(content: str, topic: str) -> _ContentAnalysis:
    model = get_structured_chat_model(temperature=0)
    # method="json_mode"：deepseek-v4-flash不支持json_schema响应格式（400 INVALID_REQUEST_BODY），
    # function_calling方式强制的tool_choice在模型"思考模式"下也会报错（且触发条件似乎和具体prompt
    # 有关，不是每次都稳定复现）。json_mode不强制tool_choice，是目前验证下来最稳的方式，见docs/ISSUES.md
    structured = bind_structured_json(model, _ContentAnalysis)
    result: _ContentAnalysis = structured.invoke(
        f"这段文本据称是关于「{topic}」旅游/生活的攻略素材。请判断：\n"
        f"1. is_relevant：这段内容是否真的是在讲{topic}当地的真实旅游/生活经验"
        f"（吃喝住行/景点/避坑等）。如果主要讲的是别的城市/地区、是网络热梗或影视吐槽段子剪辑、"
        f"是纯广告软文，则is_relevant=false。\n"
        f"2. reason：简短说明判断理由（20字以内）。\n"
        f"3. entities：尽量列出文本中提到的所有具体地名/店名/景点名（去掉「{topic}」这种泛称），"
        f"不要只挑一两个最显眼的，但只能是文本中确实出现过的词，不要因为追求数量而编造或引申。\n"
        f"请以JSON格式回复，包含is_relevant(布尔值)、reason(字符串)、entities(字符串数组)三个字段。\n\n"
        f"文本内容：\n{content[:2000]}"
    )
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _topic_dir(topic: str, layer: str, library_dir: Path | None = None) -> Path:
    return (library_dir or MATERIAL_LIBRARY_DIR) / layer / topic


def _load_existing_doc_source_files(
    topic: str, *, library_dir: Path | None = None
) -> set[str]:
    entries_path = _topic_dir(topic, "index", library_dir) / "entries.jsonl"
    if not entries_path.exists():
        return set()
    ids = set()
    for line in entries_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.add(json.loads(line)["source_file"])
    return ids


def _load_rejected_source_files(
    topic: str, *, library_dir: Path | None = None
) -> set[str]:
    """已经判断过"与topic不相关、故意跳过入库"的source_file集合。

    用途：避免下次重跑ingest_topic时对同一条素材重复调用LLM判断（is_relevant的判断
    结果不会变，没必要每次都重新花一次LLM调用去问同一个问题）。
    """
    rejected_path = _topic_dir(topic, "index", library_dir) / "rejected.jsonl"
    if not rejected_path.exists():
        return set()
    ids = set()
    for line in rejected_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.add(json.loads(line)["source_file"])
    return ids


def material_processing_status(
    topic: str,
    source_files: list[str],
    *,
    library_dir: Path | None = None,
) -> dict[str, str]:
    """Return indexed/rejected/pending after ingest; indexed requires a chunk."""
    index_dir = _topic_dir(topic, "index", library_dir)
    doc_id_by_source: dict[str, str] = {}
    entries_path = index_dir / "entries.jsonl"
    if entries_path.exists():
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                doc_id_by_source[entry["source_file"]] = entry["doc_id"]
    chunk_doc_ids: set[str] = set()
    chunks_path = index_dir / "chunks.jsonl"
    if chunks_path.exists():
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                chunk_doc_ids.add(json.loads(line)["doc_id"])
    indexed = {
        source_file for source_file, doc_id in doc_id_by_source.items() if doc_id in chunk_doc_ids
    }
    rejected = _load_rejected_source_files(topic, library_dir=library_dir)
    return {
        source_file: (
            "indexed" if source_file in indexed else "rejected" if source_file in rejected else "pending"
        )
        for source_file in source_files
    }


def _next_doc_number(
    topic: str,
    *,
    library_dir: Path | None = None,
    doc_id_prefix: str | None = None,
) -> int:
    """Return the next unused numeric suffix for a topic's doc_id.

    ``doc_id`` used to be derived from the current manifest position. That is
    not stable: appending/inserting a later manifest item can reuse a number
    already written to the append-only index and make two unrelated documents
    share one doc_id. Allocate from the existing index instead, so incremental
    material collection can never collide with historical entries.
    """
    entries_path = _topic_dir(topic, "index", library_dir) / "entries.jsonl"
    counter_path = _topic_dir(topic, "index", library_dir) / ".doc_id_counter"
    prefix = doc_id_prefix or f"{topic}-"
    maximum = -1
    if counter_path.exists():
        try:
            maximum = max(maximum, int(counter_path.read_text(encoding="utf-8").strip()) - 1)
        except (TypeError, ValueError):
            pass
    if entries_path.exists():
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                doc_id = str(json.loads(line).get("doc_id") or "")
            except json.JSONDecodeError:
                continue
            if not doc_id.startswith(prefix):
                continue
            suffix = doc_id[len(prefix) :]
            if suffix.isdigit():
                maximum = max(maximum, int(suffix))
    return maximum + 1


def _persist_next_doc_number(
    topic: str, next_number: int, *, library_dir: Path | None = None
) -> None:
    counter_path = _topic_dir(topic, "index", library_dir) / ".doc_id_counter"
    counter_path.write_text(str(next_number), encoding="utf-8")


@contextmanager
def _topic_ingest_lock(topic: str, *, library_dir: Path | None = None):
    """Serialize append-only ingestion across threads and processes."""
    lock_path = _topic_dir(topic, "index", library_dir) / ".ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def remove_from_index(
    topic: str,
    doc_ids: list[str],
    *,
    library_dir: Path | None = None,
) -> dict:
    """从 index/<topic>/ 的 entries.jsonl + chunks.jsonl + FAISS索引 三处一并移除
    指定doc_id的记录，结构化过滤重写（load→过滤→dump），不手改JSON文件本体
    （跟 collectors/ingest_to_raw.py 的 remove_from_manifest 是同一个思路，见docs/ISSUES.md）。

    只清索引层，不动 raw/ 层的原始文件——万一以后想重新评估这条素材，或者上游的
    分段/质量判断规则改进了，raw原文还在，可以重新走一遍 ingest_topic 处理。

    返回 {"removed_entries": [...], "removed_chunk_ids": [...]}，方便调用方确认
    删除范围符合预期、记录审计信息。doc_ids里找不到对应记录的部分不报错，直接忽略。
    """
    if not doc_ids:
        return {"removed_entries": [], "removed_chunk_ids": []}

    to_remove = set(doc_ids)
    index_dir = _topic_dir(topic, "index", library_dir)
    entries_path = index_dir / "entries.jsonl"
    chunks_path = index_dir / "chunks.jsonl"

    removed_entries: list[dict] = []
    if entries_path.exists():
        kept_lines = []
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("doc_id") in to_remove:
                removed_entries.append(entry)
            else:
                kept_lines.append(line)
        entries_path.write_text(
            "\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8"
        )

    removed_chunk_ids: list[str] = []
    if chunks_path.exists():
        kept_lines = []
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("doc_id") in to_remove:
                removed_chunk_ids.append(chunk["chunk_id"])
            else:
                kept_lines.append(line)
        chunks_path.write_text(
            "\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8"
        )

    if removed_chunk_ids:
        vector_store.remove_chunks(
            topic, removed_chunk_ids, library_dir=library_dir
        )

    return {"removed_entries": removed_entries, "removed_chunk_ids": removed_chunk_ids}


def ingest_topic(
    topic: str,
    *,
    library_dir: Path | None = None,
    doc_id_prefix: str | None = None,
) -> list[MaterialEntry]:
    """Run one topic ingestion under a cross-process per-topic lock."""
    with _topic_ingest_lock(topic, library_dir=library_dir):
        return _ingest_topic_unlocked(
            topic,
            library_dir=library_dir,
            doc_id_prefix=doc_id_prefix,
        )


def _ingest_topic_unlocked(
    topic: str,
    *,
    library_dir: Path | None = None,
    doc_id_prefix: str | None = None,
) -> list[MaterialEntry]:
    """处理 raw/<topic>/ 下尚未入库的素材：抽实体、切chunk、embedding入FAISS索引。

    返回本次新增的文档级条目列表（幂等：已经处理过的 source_file 会被跳过）。
    """
    raw_dir = _topic_dir(topic, "raw", library_dir)
    manifest_path = raw_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"找不到 {manifest_path}，请先在 raw/{topic}/ 下建好 manifest.json")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = _load_existing_doc_source_files(
        topic, library_dir=library_dir
    ) | _load_rejected_source_files(topic, library_dir=library_dir)

    index_dir = _topic_dir(topic, "index", library_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    entries_path = index_dir / "entries.jsonl"
    chunks_path = index_dir / "chunks.jsonl"
    rejected_path = index_dir / "rejected.jsonl"

    embeddings_client = get_embeddings()
    embedding_model_name = embeddings_client.model

    new_entries: list[MaterialEntry] = []
    resolved_doc_id_prefix = doc_id_prefix or f"{topic}-"
    next_doc_number = _next_doc_number(
        topic,
        library_dir=library_dir,
        doc_id_prefix=resolved_doc_id_prefix,
    )
    with entries_path.open("a", encoding="utf-8") as entries_f, \
         chunks_path.open("a", encoding="utf-8") as chunks_f, \
         rejected_path.open("a", encoding="utf-8") as rejected_f:
        for item in manifest.get("items", []):
            source_file = item["source_file"]
            if source_file in existing:
                continue  # 已入库或已判定不相关，跳过不重复处理

            source_path = raw_dir / source_file
            content = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
            if not content:
                continue

            if len(content) < _MIN_CONTENT_LENGTH:
                # 内容长度门禁（docs/ARCHITECTURE.md第8.1节）：原文过短几乎不可能承载可检索的
                # 具体细节，不值得花一次LLM调用判断，直接跳过并入rejected.jsonl（不新开文件）。
                rejected_f.write(json.dumps({
                    "source_file": source_file,
                    "title": item.get("title", ""),
                    "reason": f"内容过短（{len(content)}字，低于{_MIN_CONTENT_LENGTH}字阈值），入库前跳过",
                    "rejected_at": _now(),
                }, ensure_ascii=False) + "\n")
                print(f"  [跳过-内容过短] {source_file}：{len(content)}字")
                continue

            doc_id = f"{resolved_doc_id_prefix}{next_doc_number:03d}"
            platform = item.get("platform", "")

            analysis = _analyze_content(content, topic)
            if not analysis.is_relevant:
                # 不相关内容（无关地区/网络热梗/段子/广告）完全不写entries/chunks/FAISS索引——
                # retrieve.py不按is_core过滤，标is_core=false挡不住被检索到，必须整条跳过。
                # raw/下的原始文件不删，判断结论只记在index层，方便以后复核/调整判断标准。
                rejected_f.write(json.dumps({
                    "source_file": source_file,
                    "title": item.get("title", ""),
                    "reason": analysis.reason,
                    "rejected_at": _now(),
                }, ensure_ascii=False) + "\n")
                print(f"  [跳过-不相关] {source_file}：{analysis.reason}")
                continue

            entities = [e for e in analysis.entities if e and _entity_matches(e, content)]
            entry = MaterialEntry(
                doc_id=doc_id,
                topic=topic,
                source_file=source_file,
                platform=platform,
                author=item.get("author", ""),
                source_url=item.get("source_url", ""),
                publish_date=item.get("publish_date", ""),
                title=item.get("title", ""),
                is_core=item.get("is_core", True),
                entities=entities,
            )
            entries_f.write(entry.model_dump_json() + "\n")
            new_entries.append(entry)
            next_doc_number += 1

            # ---- 分段 + embedding ----
            texts = chunk_content(content, platform)
            if not texts:
                continue

            vectors = embeddings_client.embed_documents(texts)
            chunk_ids = [f"{doc_id}-c{order:03d}" for order in range(len(texts))]
            embedded_at = _now()

            for order, (chunk_id, text) in enumerate(zip(chunk_ids, texts)):
                chunk = MaterialChunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    topic=topic,
                    order=order,
                    text=text,
                    embedding_model=embedding_model_name,
                    embedded_at=embedded_at,
                )
                chunks_f.write(chunk.model_dump_json() + "\n")

            vector_store.add_chunks(
                topic, chunk_ids, vectors, library_dir=library_dir
            )

        _persist_next_doc_number(topic, next_doc_number, library_dir=library_dir)

    if new_entries and library_dir is None:  # overlay不生成全局洞察文件
        try:
            from src.materials.insights import generate_insights
            generate_insights(topic)
        except Exception as exc:  # noqa: BLE001 - 洞察生成失败不能影响素材入库这个更核心的结果
            print(f"[ingest] 洞察生成失败（不影响本次入库结果）：{exc}")

    return new_entries


if __name__ == "__main__":
    # 命令行入口：python -m src.materials.ingest <topic>
    # 此前这里没有 __main__ 入口，`python -m src.materials.ingest 成都` 会静默什么都不做
    # （退出码0、无输出），容易误判为"跑成功了但没有增量"，见 docs/ISSUES.md 第12条。
    if len(sys.argv) != 2:
        print(f"用法: python -m src.materials.ingest <topic>", file=sys.stderr)
        sys.exit(1)
    topic_arg = sys.argv[1]
    added = ingest_topic(topic_arg)
    print(f"新增 {len(added)} 条素材已入库到 index/{topic_arg}/")
    for e in added:
        print(f"  - {e.doc_id} {e.title}")
