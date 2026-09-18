"""按平台/内容特征选择不同的分段策略，找不到对应策略时落到generic兜底。"""
from __future__ import annotations

from src.materials.chunkers.bilibili import chunk_bilibili_transcript
from src.materials.chunkers.chapter_index import strip_chapter_index
from src.materials.chunkers.flat_transcript import chunk_flat_transcript
from src.materials.chunkers.generic import chunk_generic

CHUNKER_REGISTRY = {
    # 带"发言人 00:00"时间戳标记的转写（某些转写导出工具的格式，如现有存量素材）
    "bilibili": chunk_bilibili_transcript,
    # MiMo ASR产出的纯flat无时间戳口播文本，必须走独立策略，
    # 不能误用bilibili的时间戳切分（会退化成一个巨型chunk，见 flat_transcript.py 顶部说明）
    "bilibili_asr": chunk_flat_transcript,
    "douyin_asr": chunk_flat_transcript,
}


def chunk_content(content: str, platform: str) -> list[str]:
    """返回切好的文本片段列表。平台没有专属策略时用generic兜底
    （抖音desc文案+话题标签、小红书图文等没有固定结构的短内容都走这条路）。

    "章节导览"结构化目录块的剥离在分发给具体chunker之前统一做——这不是某个
    平台专属的格式，B站/抖音的desc都可能出现，见docs/ISSUES.md第21条第3点。
    """
    content = strip_chapter_index(content)
    chunker = CHUNKER_REGISTRY.get(platform, chunk_generic)
    chunks = chunker(content)
    return [c.strip() for c in chunks if c.strip()]
