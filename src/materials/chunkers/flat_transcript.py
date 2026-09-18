"""纯flat口播转写文本的分段策略：适用于MiMo ASR产出的、没有任何
"发言人 00:00"时间戳标记的连续口播文字（platform=bilibili_asr/douyin_asr）。

**为什么不能复用 chunk_bilibili_transcript**：那个策略靠 "发言人 00:00" 这种
时间戳标记切段（见 bilibili.py）。ASR产出里根本没有这种标记，split一个都匹配不到，
整篇会退化成一个巨型chunk——直接重演 docs/ISSUES.md 第9条那场检索灾难
（一个chunk混杂太多话题→embedding语义信号被稀释→专有名词检索排序失效）。
所以flat文本必须单独一套按语义长度切分的策略。

**为什么带句子级重叠**：口播是连续叙述，一句话的指代/上下文经常跨越chunk边界
（"这家店...它家的招牌是..."被切开后，后半段丢了"这家店"是谁）。让相邻chunk
共享尾部一两句，能保住跨边界的上下文，语义更完整，代价是同一句话会进多个chunk，
但检索是RRF融合排序、返回top_k，轻微重复不影响（见 retrieve.py）。
"""
from __future__ import annotations

import re

_TARGET_SIZE = 350  # 目标chunk长度（字符数），累积到这个长度附近就在句子边界切
_OVERLAP_SIZE = 80   # 相邻chunk之间保留的重叠长度（字符数），从上一个chunk尾部按整句回捞

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?])")


def _split_sentences(content: str) -> list[str]:
    # 先按空白/换行归一成连续文本，再按句末标点切成整句
    normalized = re.sub(r"\s+", "", content)
    return [s for s in _SENTENCE_BOUNDARY_RE.split(normalized) if s]


def _tail_overlap(sentences: list[str]) -> list[str]:
    """从已完成的一个chunk的句子列表里，取尾部若干整句作为下一个chunk的重叠前缀，
    总长度不超过 _OVERLAP_SIZE（按整句回捞，不切碎句子）。"""
    overlap: list[str] = []
    length = 0
    for s in reversed(sentences):
        if length + len(s) > _OVERLAP_SIZE and overlap:
            break
        overlap.insert(0, s)
        length += len(s)
    return overlap


def chunk_flat_transcript(content: str) -> list[str]:
    sentences = _split_sentences(content)
    if not sentences:
        return []

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for s in sentences:
        buf.append(s)
        buf_len += len(s)
        if buf_len >= _TARGET_SIZE:
            chunks.append("".join(buf))
            buf = _tail_overlap(buf)
            buf_len = sum(len(x) for x in buf)

    # 收尾：剩余内容成最后一个chunk。若它完全被上一个chunk的重叠尾部覆盖（没有新增
    # 实质内容），就并回上一个chunk，避免产出一个纯重复的碎片chunk。
    if buf:
        tail = "".join(buf)
        if chunks and chunks[-1].endswith(tail):
            pass
        else:
            chunks.append(tail)

    return chunks
