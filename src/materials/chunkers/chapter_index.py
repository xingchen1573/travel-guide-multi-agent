"""剥离"章节导览"类结构化目录块——纯预处理，供 chunk_content() 在分发给具体
chunker 之前统一调用，跟平台无关（不是B站/抖音专属格式）。

背景（详见 docs/ISSUES.md 第21条第3点）：部分转写/摘要工具会在正文前面生成一段
"## 章节导览\n- 00:30 交通\n- 00:48 住哪\n..."这样的时间戳目录，本身没有任何
实质描述，只是关键词罗列。这类chunk在BM25检索里因为塞满高频关键词天然占便宜，
会跟真实有内容的chunk抢排名（跟第9条剥离转写导出工具文件头是同一类根因、不同
具体形态）。同一份素材如果已经有ASR完整转写版本，desc版的章节目录block更是
纯冗余——真实案例：成都-002(douyin desc) 全文就是一段章节导览+标题，而它的
ASR孪生版 成都-012(douyin_asr) 已经完整覆盖同等信息量。

设计原则：只精确匹配"## 章节导览"这个具体标题+连续的"- MM:SS 关键词"列表项，
不做宽泛的"看起来像列表就剥离"，避免误伤未来真正有内容的markdown列表。
"""
from __future__ import annotations

import re

# "## 章节导览"标题 + 紧随其后的一个或多个 "- MM:SS 内容" 列表行，整块剥离。
# re.MULTILINE让^/$匹配行首行尾；块可能出现在正文任意位置（目前观察到的案例在开头，
# 但不假设位置），后面允许跟空行或其它内容。
_CHAPTER_INDEX_RE = re.compile(
    r"^##\s*章节导览\s*\n(?:^-\s*\d{1,2}:\d{2}\s+.+\n?)+",
    re.MULTILINE,
)


def strip_chapter_index(content: str) -> str:
    """剥离content里的"章节导览"结构化目录块，返回剩余文本（去除首尾空白）。
    没有匹配到时原样返回。
    """
    stripped = _CHAPTER_INDEX_RE.sub("", content)
    return stripped.strip()
