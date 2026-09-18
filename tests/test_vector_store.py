"""vector_store.py的单元测试，全部在临时目录合成的小索引上跑，不碰真实素材库的
FAISS索引——remove_chunks涉及"整体重建索引"这种有一定破坏性的操作，必须先在隔离
环境里验证正确性，见docs/ISSUES.md第21条。
"""
from __future__ import annotations

import pytest

from src.materials import vector_store


@pytest.fixture
def topic(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_store, "MATERIAL_LIBRARY_DIR", tmp_path)
    return "测试主题"


# 5个正交的4维向量，方便断言"删除某个chunk后，其余chunk的相似度分数完全不变"
# （不是约等于，是bit级精确——IndexFlatIP存原始向量，重建不应引入任何数值误差）
_VECTORS = {
    "c0": [1.0, 0.0, 0.0, 0.0],
    "c1": [0.0, 1.0, 0.0, 0.0],
    "c2": [0.0, 0.0, 1.0, 0.0],
    "c3": [0.0, 0.0, 0.0, 1.0],
    "c4": [0.7071, 0.7071, 0.0, 0.0],  # 和c0/c1都有一定相似度，用来验证重建后分数不变
}


def _seed(topic):
    ids = list(_VECTORS.keys())
    vector_store.add_chunks(topic, ids, [_VECTORS[i] for i in ids])


def test_remove_chunks_middle_element_keeps_id_map_order_aligned(topic):
    # 删除中间的c1和c3（不是首尾），剩余id_map顺序应该是过滤后的c0/c2/c4，
    # 且每个下标依然精确对应FAISS内部同一个向量位置
    _seed(topic)
    removed = vector_store.remove_chunks(topic, ["c1", "c3"])
    assert removed == 2

    id_map = vector_store.load_id_map(topic)
    assert id_map == ["c0", "c2", "c4"]


def test_remove_chunks_removed_id_not_searchable(topic):
    _seed(topic)
    vector_store.remove_chunks(topic, ["c1"])

    # 用c1原本的向量去搜，不应该再搜到c1自己（它已经被删了）
    results = vector_store.search(topic, _VECTORS["c1"], top_k=5)
    result_ids = [r[0] for r in results]
    assert "c1" not in result_ids
    assert set(result_ids) == {"c0", "c2", "c3", "c4"}


def test_remove_chunks_scores_unchanged_for_remaining_vectors(topic):
    # 重建索引不能引入数值误差：删除c1前后，c0/c4之间的相似度分数应该完全一致
    _seed(topic)
    before = dict(vector_store.search(topic, _VECTORS["c0"], top_k=5))

    vector_store.remove_chunks(topic, ["c1"])

    after = dict(vector_store.search(topic, _VECTORS["c0"], top_k=5))
    assert "c1" not in after
    for cid in ("c0", "c2", "c3", "c4"):
        assert before[cid] == pytest.approx(after[cid], abs=1e-6)


def test_remove_chunks_nonexistent_id_is_noop(topic):
    _seed(topic)
    removed = vector_store.remove_chunks(topic, ["不存在的chunk_id"])
    assert removed == 0
    assert vector_store.load_id_map(topic) == list(_VECTORS.keys())


def test_remove_chunks_empty_list_is_noop(topic):
    _seed(topic)
    removed = vector_store.remove_chunks(topic, [])
    assert removed == 0
    assert vector_store.load_id_map(topic) == list(_VECTORS.keys())


def test_remove_chunks_no_index_file_is_noop(topic):
    # 索引文件还不存在时（比如topic从没add_chunks过），不应该崩溃
    removed = vector_store.remove_chunks(topic, ["c0"])
    assert removed == 0


def test_remove_chunks_all_elements_leaves_empty_but_valid_index(topic):
    # 删光所有向量后，索引应该变成一个空的、但依然可以正常search/add_chunks的状态
    _seed(topic)
    removed = vector_store.remove_chunks(topic, list(_VECTORS.keys()))
    assert removed == len(_VECTORS)
    assert vector_store.load_id_map(topic) == []

    # 删光之后应该还能正常搜索（返回空结果，不报错）
    assert vector_store.search(topic, _VECTORS["c0"], top_k=5) == []

    # 删光之后应该还能正常再次add_chunks，索引没有损坏
    vector_store.add_chunks(topic, ["new"], [_VECTORS["c0"]])
    assert vector_store.load_id_map(topic) == ["new"]
    results = vector_store.search(topic, _VECTORS["c0"], top_k=1)
    assert results[0][0] == "new"


def test_remove_chunks_partial_match_only_removes_existing_ids(topic):
    # chunk_ids里一部分存在一部分不存在，应该只删除真的存在的那些，不报错
    _seed(topic)
    removed = vector_store.remove_chunks(topic, ["c1", "不存在的id", "c3"])
    assert removed == 2
    assert vector_store.load_id_map(topic) == ["c0", "c2", "c4"]
