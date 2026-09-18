"""全局素材空间的发现、Project引用统计和整空间回收站。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.config import MATERIAL_LIBRARY_DIR
from src.materials.ingest import _topic_ingest_lock
from src.workspace import store


class TopicInUseError(ValueError):
    def __init__(self, topic: str, project_ids: list[str]):
        self.topic = topic
        self.project_ids = project_ids
        super().__init__(f"素材空间 {topic!r} 仍被 {len(project_ids)} 个正常 Project 使用")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _topic_paths(topic: str) -> tuple[Path, Path]:
    return MATERIAL_LIBRARY_DIR / "raw" / topic, MATERIAL_LIBRARY_DIR / "index" / topic


def _has_managed_topic_state(topic: str) -> bool:
    """忽略仅有.ingest.lock或空FAISS目录的测试/历史残留。"""
    raw_dir, index_dir = _topic_paths(topic)
    if (raw_dir / "manifest.json").exists():
        return True
    return any(
        (index_dir / filename).exists()
        for filename in ("entries.jsonl", "chunks.jsonl", "rejected.jsonl", "insights.json")
    )


def list_active_topics() -> list[str]:
    candidates: set[str] = set()
    for layer in ("raw", "index"):
        root = MATERIAL_LIBRARY_DIR / layer
        if root.exists():
            candidates.update(path.name for path in root.iterdir() if path.is_dir())
    return sorted(topic for topic in candidates if _has_managed_topic_state(topic))


def _saved_topic(path: Path) -> str | None:
    saved = _read_json(path)
    if not saved:
        return None
    return saved.get("topic") or (saved.get("requirements") or {}).get("destination")


def topic_project_references(topic: str) -> dict[str, list[str]]:
    active = []
    for project_id in store.list_project_ids():
        if _saved_topic(store.workspace_dir(project_id) / "requirements.json") == topic:
            active.append(project_id)

    trashed = []
    for project_id in store.list_trashed_project_ids():
        path = store.trashed_project_dir(project_id) / "workspace" / "requirements.json"
        if _saved_topic(path) == topic:
            trashed.append(project_id)
    return {"active_project_ids": active, "trashed_project_ids": trashed}


def _record_key(topic: str) -> str:
    return hashlib.sha256(topic.encode("utf-8")).hexdigest()[:24]


def _trash_root() -> Path:
    return MATERIAL_LIBRARY_DIR / "_trash" / "topics"


def _record_dir(topic: str) -> Path:
    return _trash_root() / _record_key(topic)


def _record_path(topic: str) -> Path:
    return _record_dir(topic) / "record.json"


def _load_record(topic: str) -> dict | None:
    return _read_json(_record_path(topic))


@contextmanager
def _management_lock(topic: str):
    lock_path = MATERIAL_LIBRARY_DIR / "_trash" / ".locks" / f"{_record_key(topic)}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _material_trash_dirs(topic: str) -> list[Path]:
    root = MATERIAL_LIBRARY_DIR / "_trash" / "materials"
    if not root.exists():
        return []
    matches = []
    for record_path in root.glob("*/record.json"):
        record = _read_json(record_path)
        if record and record.get("topic") == topic:
            matches.append(record_path.parent)
    return matches


def topic_summary(record: dict) -> dict:
    return {
        "resource_type": "material_topic",
        "topic": record["topic"],
        "entry_count": record.get("entry_count", 0),
        "chunk_count": record.get("chunk_count", 0),
        "raw_file_count": record.get("raw_file_count", 0),
        "trashed_material_count": record.get("trashed_material_count", 0),
        "active_project_ids": record.get("active_project_ids", []),
        "trashed_project_ids": record.get("trashed_project_ids", []),
        "deleted_at": record.get("deleted_at"),
    }


def _apply_soft_delete(record: dict) -> None:
    topic = record["topic"]
    record_dir = _record_dir(topic)
    raw_dir, index_dir = _topic_paths(topic)

    for source, target_name in ((raw_dir, "raw"), (index_dir, "index")):
        target = record_dir / target_name
        if source.exists() and not target.exists():
            shutil.move(str(source), str(target))

    material_records_dir = record_dir / "material_records"
    material_records_dir.mkdir(parents=True, exist_ok=True)
    for source in _material_trash_dirs(topic):
        target = material_records_dir / source.name
        if not target.exists():
            shutil.move(str(source), str(target))


def soft_delete_topic(topic: str) -> dict:
    with _management_lock(topic):
        existing = _load_record(topic)
        if existing is not None:
            _apply_soft_delete(existing)
            return topic_summary(existing)

        if not _has_managed_topic_state(topic):
            raise FileNotFoundError(f"素材空间 {topic!r} 不存在")

        references = topic_project_references(topic)
        if references["active_project_ids"]:
            raise TopicInUseError(topic, references["active_project_ids"])

        raw_dir, index_dir = _topic_paths(topic)
        record = {
            "version": 1,
            "resource_type": "material_topic",
            "topic": topic,
            "deleted_at": _now(),
            "entry_count": len(_read_jsonl(index_dir / "entries.jsonl")),
            "chunk_count": len(_read_jsonl(index_dir / "chunks.jsonl")),
            "raw_file_count": sum(1 for path in raw_dir.rglob("*") if path.is_file()) if raw_dir.exists() else 0,
            "trashed_material_count": len(_material_trash_dirs(topic)),
            **references,
        }
        _write_json(_record_path(topic), record)
        with _topic_ingest_lock(topic):
            _apply_soft_delete(record)
        return topic_summary(record)


def list_trashed_topics() -> list[dict]:
    root = _trash_root()
    if not root.exists():
        return []
    records = []
    for path in root.glob("*/record.json"):
        record = _read_json(path)
        if record:
            records.append(topic_summary(record))
    return sorted(records, key=lambda item: item.get("deleted_at") or "", reverse=True)


def _ensure_restore_targets_available(topic: str, record_dir: Path) -> None:
    raw_dir, index_dir = _topic_paths(topic)
    if raw_dir.exists() and any(raw_dir.iterdir()):
        raise ValueError(f"素材空间 {topic!r} 已存在raw数据，无法恢复")
    if index_dir.exists():
        meaningful = [path for path in index_dir.iterdir() if path.name != ".ingest.lock"]
        if meaningful:
            raise ValueError(f"素材空间 {topic!r} 已存在index数据，无法恢复")

    material_root = MATERIAL_LIBRARY_DIR / "_trash" / "materials"
    for source in (record_dir / "material_records").glob("*") if (record_dir / "material_records").exists() else []:
        if (material_root / source.name).exists():
            raise ValueError(f"素材空间 {topic!r} 的单条素材回收记录发生冲突，无法恢复")


def restore_topic(topic: str) -> dict:
    with _management_lock(topic):
        record = _load_record(topic)
        if record is None:
            raise FileNotFoundError(f"素材空间 {topic!r} 不在回收站里")
        record_dir = _record_dir(topic)
        _ensure_restore_targets_available(topic, record_dir)

        raw_dir, index_dir = _topic_paths(topic)
        archived_raw = record_dir / "raw"
        if archived_raw.exists():
            if raw_dir.exists():
                raw_dir.rmdir()
            raw_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archived_raw), str(raw_dir))

        with _topic_ingest_lock(topic):
            archived_index = record_dir / "index"
            if archived_index.exists():
                index_dir.mkdir(parents=True, exist_ok=True)
                for source in archived_index.iterdir():
                    if source.name == ".ingest.lock":
                        continue
                    shutil.move(str(source), str(index_dir / source.name))
                shutil.rmtree(archived_index)

        material_root = MATERIAL_LIBRARY_DIR / "_trash" / "materials"
        material_records_dir = record_dir / "material_records"
        if material_records_dir.exists():
            material_root.mkdir(parents=True, exist_ok=True)
            for source in material_records_dir.iterdir():
                shutil.move(str(source), str(material_root / source.name))

        shutil.rmtree(record_dir)
        return {"topic": topic, "status": "restored"}


def permanently_delete_topic(topic: str) -> dict:
    with _management_lock(topic):
        record = _load_record(topic)
        if record is None:
            raise FileNotFoundError(f"素材空间 {topic!r} 不在回收站里")
        shutil.rmtree(_record_dir(topic))
        return {"topic": topic, "status": "permanently_deleted"}
