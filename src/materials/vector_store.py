"""FAISS本地向量索引的加载/保存/增量写入/删除/检索，按主题(topic)分文件存储。

设计：纯本地文件索引（不起向量DB服务），用IndexFlatIP（内积，向量先归一化实现余弦相似度）。
id_map.json是一个chunk_id的有序列表，列表下标 == FAISS内部向量id，这条对应关系必须
始终成立。新增向量走增量路径（append到id_map末尾 + index.add()，顺序天然对齐）；
删除向量没有增量API，只能整体重建索引（见remove_chunks，docs/ISSUES.md第21条）。

Windows坑：faiss-cpu底层用的是ANSI窄字符fopen，不支持含中文等非ASCII字符的路径
（我们的项目目录本身就在中文路径下，topic文件夹名也是中文，如"成都"）。
所以faiss.read_index/write_index一律不直接对着真实路径操作，而是先在系统临时目录
（ASCII安全）里读写，再用Python自身的文件拷贝（不受这个限制）挪到真实路径。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import faiss
import numpy as np

from src.config import MATERIAL_LIBRARY_DIR


def _embeddings_dir(topic: str, library_dir: Path | None = None) -> Path:
    return (library_dir or MATERIAL_LIBRARY_DIR) / "index" / topic / "embeddings"


def _index_path(topic: str, library_dir: Path | None = None) -> Path:
    return _embeddings_dir(topic, library_dir) / "faiss.index"


def _id_map_path(topic: str, library_dir: Path | None = None) -> Path:
    return _embeddings_dir(topic, library_dir) / "id_map.json"


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1e-8
    return vectors / norms


def _read_index_safe(path: Path):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "index.faiss"
        shutil.copyfile(path, tmp_path)  # Python级别的拷贝，不走faiss内部fopen，不受路径编码限制
        return faiss.read_index(str(tmp_path))


def _write_index_safe(index, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "index.faiss"
        faiss.write_index(index, str(tmp_path))  # 先写到ASCII安全的临时目录
        shutil.copyfile(tmp_path, path)  # 再拷贝到真实的（可能含中文的）目标路径


def load_id_map(topic: str, *, library_dir: Path | None = None) -> list[str]:
    path = _id_map_path(topic, library_dir)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def add_chunks(
    topic: str,
    chunk_ids: list[str],
    vectors: list[list[float]],
    *,
    library_dir: Path | None = None,
) -> None:
    """把新的chunk向量追加进这个主题的FAISS索引，chunk_ids顺序要和vectors一一对应。"""
    if not chunk_ids:
        return

    _embeddings_dir(topic, library_dir).mkdir(parents=True, exist_ok=True)
    dim = len(vectors[0])
    arr = _normalize(np.array(vectors, dtype="float32"))

    index_path = _index_path(topic, library_dir)
    if index_path.exists():
        index = _read_index_safe(index_path)
    else:
        index = faiss.IndexFlatIP(dim)

    index.add(arr)
    _write_index_safe(index, index_path)

    id_map = load_id_map(topic, library_dir=library_dir)
    id_map.extend(chunk_ids)
    _id_map_path(topic, library_dir).write_text(
        json.dumps(id_map, ensure_ascii=False), encoding="utf-8"
    )


def get_chunk_vectors(
    topic: str,
    chunk_ids: list[str],
    *,
    library_dir: Path | None = None,
) -> dict[str, list[float]]:
    """读取指定chunk当前存储的向量，供可恢复删除保存本地快照。"""
    if not chunk_ids:
        return {}

    index_path = _index_path(topic, library_dir)
    if not index_path.exists():
        return {}

    index = _read_index_safe(index_path)
    id_map = load_id_map(topic, library_dir=library_dir)
    wanted = set(chunk_ids)
    positions = [(position, chunk_id) for position, chunk_id in enumerate(id_map) if chunk_id in wanted]
    if not positions:
        return {}

    vectors = index.reconstruct_n(0, index.ntotal)
    return {chunk_id: vectors[position].tolist() for position, chunk_id in positions}


def remove_chunks(
    topic: str,
    chunk_ids: list[str],
    *,
    library_dir: Path | None = None,
) -> int:
    """从FAISS索引里彻底移除指定chunk_id对应的向量，返回实际删除的数量。

    IndexFlatIP不支持增量删除（见文件顶部说明"只做追加，不做删除/重排"），唯一办法
    是整体重建：用reconstruct_n读出所有已存向量，按id_map过滤掉要删的，重新建一个
    干净的IndexFlatIP写回去，id_map同步重写，保证"下标==FAISS内部向量id"这条不变式
    在重建后依然成立。IndexFlatIP存的是原始向量（不像IVF/PQ做过有损压缩），
    reconstruct_n读出来的值和写入时精确一致，不会引入重建误差。

    找不到的chunk_id直接忽略（不报错）；chunk_ids为空、索引文件不存在、或者
    chunk_ids里没有任何一个真的在id_map里时，不做任何操作，返回0。
    """
    if not chunk_ids:
        return 0

    index_path = _index_path(topic, library_dir)
    if not index_path.exists():
        return 0

    index = _read_index_safe(index_path)
    id_map = load_id_map(topic, library_dir=library_dir)

    to_remove = set(chunk_ids)
    keep_positions = [i for i, cid in enumerate(id_map) if cid not in to_remove]
    removed_count = len(id_map) - len(keep_positions)
    if removed_count == 0:
        return 0

    new_index = faiss.IndexFlatIP(index.d)
    if keep_positions:
        vectors = index.reconstruct_n(0, index.ntotal)
        new_index.add(vectors[keep_positions])

    new_id_map = [id_map[i] for i in keep_positions]

    _write_index_safe(new_index, index_path)
    _id_map_path(topic, library_dir).write_text(
        json.dumps(new_id_map, ensure_ascii=False), encoding="utf-8"
    )
    return removed_count


def search(
    topic: str,
    query_vector: list[float],
    top_k: int = 5,
    *,
    library_dir: Path | None = None,
) -> list[tuple[str, float]]:
    """返回 [(chunk_id, similarity_score), ...]，按相似度降序。"""
    index_path = _index_path(topic, library_dir)
    if not index_path.exists():
        return []

    index = _read_index_safe(index_path)
    if index.ntotal == 0:
        # remove_chunks可能把索引删空（见docs/ISSUES.md第21条）——索引文件存在但
        # 里面没有任何向量，这是之前只有add_chunks时不会出现的状态（加了就至少有1条）。
        # faiss.search要求k>0，ntotal=0时min(top_k, 0)=0会直接触发底层assert崩溃，
        # 提前返回空列表，这是唯一正确的语义（没有向量，当然搜不出任何结果）。
        return []
    id_map = load_id_map(topic, library_dir=library_dir)

    q = _normalize(np.array([query_vector], dtype="float32"))
    scores, indices = index.search(q, min(top_k, index.ntotal))

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        results.append((id_map[idx], float(score)))
    return results
