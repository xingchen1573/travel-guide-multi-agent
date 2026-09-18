"""混合检索（BM25+embedding RRF融合）+ bilibili chunker文件头修复的单元测试。

背景见docs/ISSUES.md：真实复现过"东郊记忆"查询——单纯embedding检索排序完全失灵
（真正相关的chunk因混杂多话题被稀释到第25位，一段文件头垃圾元数据反而排第1）。
这里测两个独立修复点：①chunker精确剥离转写文件头；②bm25_search过滤掉score=0的
"假排名"结果（这是融合逻辑本身在开发过程中暴露的一个新bug，不是原计划要测的）。

BM25/chunker逻辑都是纯函数，不涉及embedding API调用，用临时目录构造假数据即可。
"""
import json

import pytest

from src.materials import bm25_search, retrieve
from src.materials.chunkers.bilibili import chunk_bilibili_transcript


def test_bilibili_chunker_strips_transcript_export_header():
    # 复现真实案例：转写导出工具在正文前塞了"<标识>_原文\n<日期时间>"这两行，
    # 不是有意义的正文内容，之前会被切成一个独立chunk污染检索排序
    content = (
        "1493062846-1-192_原文\n2026年07月12日 15:55\n"
        "发言人   00:00\n"
        "欢迎来到成都，这是座享誉天府之国的城市。"
    )
    chunks = chunk_bilibili_transcript(content)
    assert len(chunks) == 1
    assert "欢迎来到成都" in chunks[0]
    assert "1493062846" not in chunks[0]


def test_bilibili_chunker_keeps_normal_first_chunk_untouched():
    # 没有这种文件头格式的正常内容不应该被误删——防止过度矫正
    content = "发言人   00:00\n第一段正文内容"
    chunks = chunk_bilibili_transcript(content)
    assert chunks == ["第一段正文内容"]


def test_bilibili_chunker_no_header_pattern_preserves_content():
    # 内容开头恰好是"xxx_原文"但后面不是日期格式的，不应该被误判为文件头而删除
    content = "发言人   00:00\n关于_原文这个词的讨论内容，后面接着正常聊天"
    chunks = chunk_bilibili_transcript(content)
    assert len(chunks) == 1
    assert "关于_原文这个词" in chunks[0]


@pytest.fixture
def fake_chunks_topic(tmp_path, monkeypatch):
    monkeypatch.setattr(bm25_search, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic_dir = tmp_path / "index" / "测试主题"
    topic_dir.mkdir(parents=True)
    chunks = [
        {"chunk_id": "c001", "text": "东郊记忆是由旧工业厂房改造而成的文化创意产业园区"},
        {"chunk_id": "c002", "text": "太古里步行街是成都著名的购物区域"},
        {"chunk_id": "c003", "text": "武侯祠是三国文化爱好者必去的景点"},
    ]
    with (topic_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return "测试主题"


def test_bm25_search_finds_exact_keyword_match(fake_chunks_topic):
    results = bm25_search.search(fake_chunks_topic, "东郊记忆", top_k=5)
    assert results[0][0] == "c001"
    assert results[0][1] > 0


def test_bm25_search_excludes_zero_score_results(fake_chunks_topic):
    # 核心回归测试：查询词的分词结果如果一个token都不在语料任何chunk里出现过，
    # 不应该有任何结果返回——之前的bug是这类真正0分的结果会被保留，参与RRF
    # 融合时产生"假排名"信号。用平假名+乱码字符构造一个确保跟jieba分词后的
    # 中文语料没有任何token重叠的查询（避免像"的"这种停用词被词典意外分出来共享）
    results = bm25_search.search(fake_chunks_topic, "ｚｚｚ日本語テスト", top_k=5)
    assert results == []


def test_bm25_search_partial_match_ranks_below_full_match(fake_chunks_topic):
    results = bm25_search.search(fake_chunks_topic, "太古里 武侯祠", top_k=5)
    result_ids = [cid for cid, _ in results]
    assert "c002" in result_ids
    assert "c003" in result_ids
    assert "c001" not in result_ids  # 完全不相关的东郊记忆不应该出现


def test_bm25_search_empty_topic_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bm25_search, "MATERIAL_LIBRARY_DIR", tmp_path)
    assert bm25_search.search("不存在的主题", "任意查询", top_k=5) == []


def test_bm25_index_can_be_reused_for_multiple_queries(fake_chunks_topic):
    index = bm25_search.build_index(fake_chunks_topic)
    assert index is not None
    assert index.search("东郊记忆", top_k=1)[0][0] == "c001"
    assert index.search("武侯祠", top_k=1)[0][0] == "c003"


@pytest.fixture
def whitespace_noise_topic(tmp_path, monkeypatch):
    # 复现真实案例（见docs/ISSUES.md第21条）：抖音desc常见的"标签间双空格"格式，
    # jieba.cut会把空格切成独立token，之前没有过滤，导致这类chunk在中英文混排
    # 查询（查询本身也会产生空格token）下获得跟语义完全无关的高分匹配。
    # 用4个文档而不是2个——BM25的IDF公式在N=2、df=1时恰好等于0（log((2-1+0.5)/(1+0.5))=0），
    # 是小语料的退化情况，不是真实148条语料会出现的场景，多几个不相关的填充文档
    # 避免测试本身踩到这个和本次修复无关的数学巧合。
    monkeypatch.setattr(bm25_search, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic_dir = tmp_path / "index" / "测试主题"
    topic_dir.mkdir(parents=True)
    chunks = [
        {
            "chunk_id": "noisy",
            # 大量双空格分隔的标签，不包含查询里任何一个实际词，只有空格token重叠
            "text": "随便逛逛成都  #青年创作者计划  #交换世界计划  #旅游攻略  #超会玩派对  #标签",
        },
        {"chunk_id": "relevant", "text": "第一站去IFS跟熊猫合影，然后去太古里逛逛，旁边就是大慈寺"},
        {"chunk_id": "filler1", "text": "武侯祠是三国文化爱好者必去的景点，红墙夹道很出片"},
        {"chunk_id": "filler2", "text": "杜甫草堂适合安静漫步，感受诗圣文化氛围"},
    ]
    with (topic_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return "测试主题"


def test_bm25_search_ignores_whitespace_tokens(whitespace_noise_topic):
    # 核心回归测试：中英文混排查询产生的空格token不应该让完全不相关的内容
    # 获得虚假的高分匹配，真正相关的chunk应该排在前面
    results = bm25_search.search(whitespace_noise_topic, "IFS 太古里 大慈寺", top_k=5)
    result_ids = [cid for cid, _ in results]
    assert "noisy" not in result_ids
    assert result_ids[0] == "relevant"


# ---------- retrieve_materials的consensus_hint软信号（docs/ARCHITECTURE.md第8.2节）----------
# entities字段字面命中insights.json里某条confidence="consensus"结论的summary时，
# 给检索结果附加consensus_hint字段。三种边界情况：命中/insights.json不存在/entity不匹配。


class _FakeEmbeddings:
    def embed_query(self, text):
        return [0.0] * 4


@pytest.fixture
def consensus_topic(tmp_path, monkeypatch):
    """构造一个带entries.jsonl+chunks.jsonl+insights.json的假主题，
    monkeypatch掉retrieve.py用到的三个模块的MATERIAL_LIBRARY_DIR + get_embeddings，
    让retrieve_materials完全离线跑（不触发真实embedding API调用）。
    """
    monkeypatch.setattr("src.materials.retrieve.MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(bm25_search, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr("src.materials.vector_store.MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(retrieve, "get_embeddings", lambda: _FakeEmbeddings())

    topic_dir = tmp_path / "index" / "测试主题"
    topic_dir.mkdir(parents=True)

    # BM25在N=2、df=1时IDF公式退化为0（同test_retrieval.py上方whitespace_noise_topic
    # 的注释说明），单文档语料会导致目标chunk也拿到0分被过滤掉——加两条不相关的填充chunk
    # 让语料规模脱离这个数学退化区间，不影响consensus_hint本身的匹配逻辑。
    chunks = [
        {"chunk_id": "c001", "doc_id": "doc001", "text": "熊猫基地是成都热门景点，建议早起排队入园"},
        {"chunk_id": "c002", "doc_id": "doc002", "text": "武侯祠是三国文化爱好者必去的景点"},
        {"chunk_id": "c003", "doc_id": "doc003", "text": "杜甫草堂适合安静漫步，感受诗圣文化氛围"},
    ]
    with (topic_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    entries = [
        {"doc_id": "doc001", "platform": "bilibili", "title": "成都游记", "entities": ["熊猫基地"]},
        {"doc_id": "doc002", "platform": "bilibili", "title": "武侯祠游记", "entities": ["武侯祠"]},
        {"doc_id": "doc003", "platform": "bilibili", "title": "草堂游记", "entities": ["杜甫草堂"]},
    ]
    with (topic_dir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return topic_dir


def test_retrieve_materials_attaches_consensus_hint_on_entity_match(consensus_topic):
    (consensus_topic / "insights.json").write_text(
        json.dumps(
            {
                "must_visit": [
                    {
                        "summary": "熊猫基地是成都标志性景点，需早起排队",
                        "confidence": "consensus",
                        "sources": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    results = retrieve.retrieve_materials("测试主题", "熊猫基地", top_k=1)
    assert len(results) == 1
    assert results[0]["chunk_id"] == "c001"
    assert results[0]["consensus_hint"] == "熊猫基地是成都标志性景点，需早起排队"


def test_retrieve_materials_no_consensus_hint_without_insights_file(consensus_topic):
    # insights.json文件不存在（素材数<5条从未生成过）：容错跳过，不报错，consensus_hint为None
    results = retrieve.retrieve_materials("测试主题", "熊猫基地", top_k=1)
    assert len(results) == 1
    assert results[0]["chunk_id"] == "c001"
    assert results[0]["consensus_hint"] is None


def test_retrieve_materials_no_consensus_hint_when_entity_not_matching(consensus_topic):
    # insights.json存在，但里面的consensus结论跟这条结果的entities完全不相关
    (consensus_topic / "insights.json").write_text(
        json.dumps(
            {
                "food": [
                    {
                        "summary": "冒菜是成都特色美食，推荐老式卤水冒菜",
                        "confidence": "consensus",
                        "sources": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    results = retrieve.retrieve_materials("测试主题", "熊猫基地", top_k=1)
    assert len(results) == 1
    assert results[0]["chunk_id"] == "c001"
    assert results[0]["consensus_hint"] is None
