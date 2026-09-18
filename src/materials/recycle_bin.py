"""素材条目的本地回收站：索引下架、原样恢复和永久删除。"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.config import MATERIAL_LIBRARY_DIR
from src.materials import vector_store
from src.materials.ingest import _topic_ingest_lock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    temporary.replace(path)


def _record_key(topic: str, doc_id: str) -> str:
    return hashlib.sha256(f"{topic}\0{doc_id}".encode("utf-8")).hexdigest()[:24]


def _trash_root() -> Path:
    return MATERIAL_LIBRARY_DIR / "_trash" / "materials"


def _record_dir(topic: str, doc_id: str) -> Path:
    return _trash_root() / _record_key(topic, doc_id)


def _record_path(topic: str, doc_id: str) -> Path:
    return _record_dir(topic, doc_id) / "record.json"


def _load_record(topic: str, doc_id: str) -> dict | None:
    path = _record_path(topic, doc_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_file_name(value: object) -> str:
    name = str(value or "")
    return name if name and Path(name).name == name else ""


def _topic_paths(topic: str) -> tuple[Path, Path, Path, Path]:
    index_dir = MATERIAL_LIBRARY_DIR / "index" / topic
    raw_dir = MATERIAL_LIBRARY_DIR / "raw" / topic
    return index_dir / "entries.jsonl", index_dir / "chunks.jsonl", raw_dir / "manifest.json", raw_dir


def _manifest_item_for_entry(manifest: dict, entry: dict) -> dict | None:
    source_file = entry.get("source_file")
    if not source_file:
        return None
    return next((item for item in manifest.get("items", []) if item.get("source_file") == source_file), None)


def _invalidate_topic_insights(topic: str) -> None:
    path = MATERIAL_LIBRARY_DIR / "index" / topic / "insights.json"
    if path.exists():
        path.unlink()


def _move_raw_files_to_trash(record: dict) -> None:
    _, _, _, raw_dir = _topic_paths(record["topic"])
    files_dir = _record_dir(record["topic"], record["doc_id"]) / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    for filename in record.get("raw_files", []):
        safe_name = _safe_file_name(filename)
        if not safe_name:
            continue
        source = raw_dir / safe_name
        target = files_dir / safe_name
        if source.exists() and not target.exists():
            shutil.move(str(source), str(target))


def _apply_soft_delete(record: dict) -> None:
    topic = record["topic"]
    doc_id = record["doc_id"]
    entries_path, chunks_path, manifest_path, _ = _topic_paths(topic)

    _write_jsonl(entries_path, [row for row in _read_jsonl(entries_path) if row.get("doc_id") != doc_id])
    _write_jsonl(chunks_path, [row for row in _read_jsonl(chunks_path) if row.get("doc_id") != doc_id])
    vector_store.remove_chunks(topic, [chunk["chunk_id"] for chunk in record.get("chunks", [])])

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_file = record.get("entry", {}).get("source_file")
        manifest["items"] = [item for item in manifest.get("items", []) if item.get("source_file") != source_file]
        _write_json(manifest_path, manifest)

    _move_raw_files_to_trash(record)
    _invalidate_topic_insights(topic)


def soft_delete_material(topic: str, doc_id: str) -> dict:
    """把单条素材的索引、向量和raw文件移入可恢复回收站。"""
    with _topic_ingest_lock(topic):
        existing_record = _load_record(topic, doc_id)
        if existing_record is not None:
            _apply_soft_delete(existing_record)
            return material_trash_summary(existing_record)

        entries_path, chunks_path, manifest_path, raw_dir = _topic_paths(topic)
        entries = _read_jsonl(entries_path)
        entry = next((row for row in entries if row.get("doc_id") == doc_id), None)
        if entry is None:
            raise FileNotFoundError(f"素材 {topic!r}/{doc_id!r} 不存在")

        chunks = [row for row in _read_jsonl(chunks_path) if row.get("doc_id") == doc_id]
        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"topic": topic, "items": []}
        manifest_item = _manifest_item_for_entry(manifest, entry)
        other_source_files = {row.get("source_file") for row in entries if row.get("doc_id") != doc_id}
        raw_files = []
        for candidate in (
            (manifest_item or {}).get("source_file") or entry.get("source_file"),
            (manifest_item or {}).get("summary_file"),
        ):
            safe_name = _safe_file_name(candidate)
            if safe_name and safe_name not in other_source_files and (raw_dir / safe_name).exists():
                raw_files.append(safe_name)

        record = {
            "version": 1,
            "resource_type": "material",
            "topic": topic,
            "doc_id": doc_id,
            "deleted_at": _now(),
            "entry": entry,
            "chunks": chunks,
            "vectors": vector_store.get_chunk_vectors(topic, chunk_ids),
            "manifest_item": manifest_item,
            "raw_files": raw_files,
        }
        _write_json(_record_path(topic, doc_id), record)
        _apply_soft_delete(record)
        return material_trash_summary(record)


def material_trash_summary(record: dict) -> dict:
    entry = record.get("entry", {})
    return {
        "resource_type": "material",
        "topic": record["topic"],
        "doc_id": record["doc_id"],
        "title": entry.get("title") or record["doc_id"],
        "platform": entry.get("platform") or "",
        "source_file": entry.get("source_file") or "",
        "chunk_count": len(record.get("chunks", [])),
        "deleted_at": record.get("deleted_at"),
    }


def list_trashed_materials() -> list[dict]:
    root = _trash_root()
    if not root.exists():
        return []
    records = []
    for path in root.glob("*/record.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(
        (material_trash_summary(record) for record in records),
        key=lambda item: item.get("deleted_at") or "",
        reverse=True,
    )


def restore_material(topic: str, doc_id: str) -> dict:
    with _topic_ingest_lock(topic):
        record = _load_record(topic, doc_id)
        if record is None:
            raise FileNotFoundError(f"素材 {topic!r}/{doc_id!r} 不在回收站里")

        entries_path, chunks_path, manifest_path, raw_dir = _topic_paths(topic)
        entries = _read_jsonl(entries_path)
        active_entry = next((row for row in entries if row.get("doc_id") == doc_id), None)
        if active_entry is not None and active_entry != record["entry"]:
            raise ValueError(f"素材 {topic!r}/{doc_id!r} 已存在，无法恢复（doc_id 冲突）")
        if active_entry is None:
            _write_jsonl(entries_path, [*entries, record["entry"]])

        chunks = _read_jsonl(chunks_path)
        existing_chunk_ids = {row.get("chunk_id") for row in chunks}
        missing_chunks = [chunk for chunk in record.get("chunks", []) if chunk.get("chunk_id") not in existing_chunk_ids]
        if missing_chunks:
            _write_jsonl(chunks_path, [*chunks, *missing_chunks])

        indexed_ids = set(vector_store.load_id_map(topic))
        vector_pairs = [
            (chunk["chunk_id"], record.get("vectors", {}).get(chunk["chunk_id"]))
            for chunk in record.get("chunks", [])
            if chunk.get("chunk_id") not in indexed_ids and record.get("vectors", {}).get(chunk.get("chunk_id"))
        ]
        if vector_pairs:
            vector_store.add_chunks(topic, [item[0] for item in vector_pairs], [item[1] for item in vector_pairs])

        raw_dir.mkdir(parents=True, exist_ok=True)
        files_dir = _record_dir(topic, doc_id) / "files"
        for filename in record.get("raw_files", []):
            safe_name = _safe_file_name(filename)
            if not safe_name:
                continue
            source = files_dir / safe_name
            target = raw_dir / safe_name
            if source.exists() and not target.exists():
                shutil.move(str(source), str(target))

        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"topic": topic, "items": []}
        manifest_item = record.get("manifest_item")
        if manifest_item and not any(item.get("source_file") == manifest_item.get("source_file") for item in manifest.get("items", [])):
            manifest.setdefault("items", []).append(manifest_item)
            _write_json(manifest_path, manifest)

        _invalidate_topic_insights(topic)
        shutil.rmtree(_record_dir(topic, doc_id))
        return {"topic": topic, "doc_id": doc_id, "status": "restored"}


def permanently_delete_material(topic: str, doc_id: str) -> dict:
    with _topic_ingest_lock(topic):
        record = _load_record(topic, doc_id)
        if record is None:
            raise FileNotFoundError(f"素材 {topic!r}/{doc_id!r} 不在回收站里")
        _apply_soft_delete(record)
        shutil.rmtree(_record_dir(topic, doc_id))
        return {"topic": topic, "doc_id": doc_id, "status": "permanently_deleted"}
