"""BM25关键词检索：跟embedding语义检索互补的稀疏检索。

背景（详见 docs/ISSUES.md）：真实复现过一次"东郊记忆"这个专有名词查询，
embedding检索排序完全失效——真正相关的chunk因为混杂了太多其他地名信息，
相似度被稀释排到第25位，反而是一段无意义的元数据文本排第1。这不是"给模型的
材料不够"，是检索环节本身对精确关键词匹配不擅长——这正是BM25的强项，两者互补
（RRF融合，见retrieve.py），不是互相替代。

不持久化索引：当前素材规模很小（单主题几十个chunk），每次检索时直接从chunks.jsonl
构建BM25语料，用jieba分词（中文没有空格分隔，BM25基于词粒度打分需要先分词）。
如果未来素材量大幅增长，需要改成持久化索引（参考vector_store.py的做法），
现在的规模下每次重新构建的开销可以忽略，不值得为此增加复杂度。
"""
from __future__ import annotations

import json
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

from src.config import MATERIAL_LIBRARY_DIR

# jieba.cut会把空格本身切成独立token（不会自动丢弃），中英文混排的查询/文本尤其容易
# 产生大量空格token（比如"IFS 太古里"这种查询、多标签用双空格分隔的抖音desc格式）。
# 真实复现过：某条抖音desc因为标签间用双空格分隔，被切出11个空格token（148条语料里
# 空格token数量最高），BM25的IDF机制反而给这个高频但完全无意义的token算出了不小的
# 分数贡献，导致一条和查询语义毫无关系的内容获得了纯粹由标点/空白噪音撑起来的高分匹配。
# 只过滤纯空白/空token，不做更激进的停用词表——避免引入维护成本更高的规则，见docs/ISSUES.md。
def _clean_tokens(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t.strip()]


def _load_chunks(topic: str, library_dir: Path | None = None) -> list[dict]:
    chunks_path = (library_dir or MATERIAL_LIBRARY_DIR) / "index" / topic / "chunks.jsonl"
    if not chunks_path.exists():
        return []
    return [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class BM25SearchIndex:
    """可复用的主题内 BM25 索引，避免一批查询重复分词和建索引。"""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        corpus_tokens = [_clean_tokens(jieba.cut(chunk["text"])) for chunk in chunks]
        self.index = BM25Okapi(corpus_tokens) if corpus_tokens else None

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if not self.index or not query.strip():
            return []
        query_tokens = _clean_tokens(jieba.cut(query))
        scores = self.index.get_scores(query_tokens)
        ranked = sorted(
            zip((chunk["chunk_id"] for chunk in self.chunks), scores),
            key=lambda item: item[1],
            reverse=True,
        )
        return [(chunk_id, score) for chunk_id, score in ranked if score > 0][:top_k]


def build_index(topic: str, *, library_dir: Path | None = None) -> BM25SearchIndex | None:
    chunks = _load_chunks(topic, library_dir)
    return BM25SearchIndex(chunks) if chunks else None


def search(
    topic: str,
    query: str,
    top_k: int = 10,
    *,
    library_dir: Path | None = None,
) -> list[tuple[str, float]]:
    """返回 [(chunk_id, bm25_score), ...]，按分数降序，只包含score>0的结果。

    真实踩过的坑：rank_bm25对完全不含任何query词的文档也会打0分，sorted是稳定排序，
    这些0分文档会按语料原始顺序排在结果列表里，如果不过滤掉直接拿去做排名融合（RRF），
    这些"假排名"会被当作有意义的相关性信号，反而稀释/挤掉真正靠BM25分数取胜的结果——
    真实复现过一次，"东郊记忆"查询里真正相关的chunk因为这个bug在融合结果里被排到第9。
    """
    index = build_index(topic, library_dir=library_dir)
    return index.search(query, top_k=top_k) if index else []
