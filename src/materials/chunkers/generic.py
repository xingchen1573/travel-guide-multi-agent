"""默认分段策略：适用于没有固定结构的内容（小红书图文、抖音文案等）。
先按自然段落切，段落过短的合并到目标长度附近，过长的按句子边界再切一刀。
不依赖任何平台特有的格式标记。
"""
from __future__ import annotations

import re

_TARGET_SIZE = 400  # 目标chunk长度（字符数），不是硬上限
_MAX_SIZE = 600  # 超过这个长度就强制在句子边界处切开

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?])")


def _split_paragraphs(content: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n|\n", content) if p.strip()]


def _split_long_paragraph(paragraph: str) -> list[str]:
    if len(paragraph) <= _MAX_SIZE:
        return [paragraph]
    sentences = [s for s in _SENTENCE_BOUNDARY_RE.split(paragraph) if s.strip()]
    chunks, buf = [], ""
    for s in sentences:
        if len(buf) + len(s) > _TARGET_SIZE and buf:
            chunks.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        chunks.append(buf)
    return chunks


def chunk_generic(content: str) -> list[str]:
    paragraphs = _split_paragraphs(content)
    # 先把过长的段落切开
    pieces: list[str] = []
    for p in paragraphs:
        pieces.extend(_split_long_paragraph(p))

    # 再把过短的片段和相邻片段合并，避免产生大量几个字的碎片chunk
    merged: list[str] = []
    buf = ""
    for piece in pieces:
        if len(buf) + len(piece) <= _TARGET_SIZE:
            buf = f"{buf}\n{piece}" if buf else piece
        else:
            if buf:
                merged.append(buf)
            buf = piece
    if buf:
        merged.append(buf)
    return merged
