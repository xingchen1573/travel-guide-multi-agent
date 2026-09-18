"""B站视频转写文本的分段策略：按"发言人 00:00"这种时间戳标记切，
每个标记之间是语义完整的一段话，比固定字数窗口更合理。
"""
from __future__ import annotations

import re

# 匹配形如 "发言人   00:00" 或 "发言人 01:36" 这种时间戳行
_SPEAKER_TIMESTAMP_RE = re.compile(r"^发言人\s+\d{2}:\d{2}\s*$", re.MULTILINE)

# 匹配转写导出工具自带的文件头："<标识>_原文\n<YYYY年MM月DD日 HH:MM>"这种固定两行格式
# （真实复现过：这两行被切成独立chunk后，embedding检索时反而排到前面污染结果，
# 见docs/ISSUES.md——不是有意义的正文内容，是导出工具塞进去的文件标识+导出时间戳）。
# 注意：这里精确匹配这一种具体格式，不是宽泛的"内容太短/太杂就过滤"，
# 避免以后误伤合法的简短内容（比如未来可能接入的评论区文本）。
_TRANSCRIPT_HEADER_RE = re.compile(r"^.+_原文\s*\n\d{4}年\d{2}月\d{2}日\s+\d{2}:\d{2}\s*$")


def chunk_bilibili_transcript(content: str) -> list[str]:
    parts = _SPEAKER_TIMESTAMP_RE.split(content)
    stripped = [p.strip() for p in parts if p.strip()]
    if stripped and _TRANSCRIPT_HEADER_RE.match(stripped[0]):
        stripped = stripped[1:]  # 第一段就是导出工具的文件头，丢弃，不当作正文chunk
    return stripped
