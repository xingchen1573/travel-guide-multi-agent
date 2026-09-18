"""B站搜索结果的筛选与排序（纯逻辑，可单测，不联网）。

字段来自 bilibili-api 的 search.search_by_type(VIDEO) 真实返回（已实测确认，
见 sandbox/bilibili_search/_bs_out.json）：
  play(播放/int) like(点赞/int) favorites(收藏/int) danmaku(弹幕/int)
  duration("MM:SS"或"HH:MM:SS"字符串) pubdate(Unix秒) author bvid title(含<em>标签) description tag(逗号分隔话题)

排序信号（和用户确认的优先级一致）：高播放 / 高赞 / 时长适中(5~20min) / 发布时间近(1年内)。
硬过滤：时长<3min 或 >30min 丢；发布>2年 丢。
"""
from __future__ import annotations

import math
import re
import time

# 硬过滤边界
_MIN_DURATION_SEC = 3 * 60
_MAX_DURATION_SEC = 30 * 60
_MAX_AGE_SEC = 2 * 365 * 24 * 3600

# 时长适配打分的理想区间（这个区间内给满分，两侧衰减）
_IDEAL_MIN_SEC = 5 * 60
_IDEAL_MAX_SEC = 20 * 60

# 综合分权重（和用户确认的优先级：播放>点赞>时长>时新）
_W_PLAY = 0.4
_W_LIKE = 0.3
_W_DURATION = 0.2
_W_RECENCY = 0.1

_EM_TAG_RE = re.compile(r"</?em[^>]*>")


def clean_title(title: str) -> str:
    """去掉搜索结果里高亮关键词的 <em class="keyword">...</em> 标签。"""
    return _EM_TAG_RE.sub("", title or "").strip()


def parse_duration(duration: str) -> int:
    """把 "MM:SS" 或 "HH:MM:SS" 解析成总秒数。解析不了返回0（会被硬过滤丢掉）。"""
    if not duration:
        return 0
    parts = duration.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def _duration_fit_score(sec: int) -> float:
    """时长在理想区间内给1.0，两侧线性衰减到过滤边界处为0。"""
    if _IDEAL_MIN_SEC <= sec <= _IDEAL_MAX_SEC:
        return 1.0
    if sec < _IDEAL_MIN_SEC:
        span = _IDEAL_MIN_SEC - _MIN_DURATION_SEC
        return max(0.0, (sec - _MIN_DURATION_SEC) / span) if span else 0.0
    span = _MAX_DURATION_SEC - _IDEAL_MAX_SEC
    return max(0.0, (_MAX_DURATION_SEC - sec) / span) if span else 0.0


def _recency_score(pubdate: int, now: int) -> float:
    """发布越近分越高：1年内接近1.0，线性衰减到2年为0。"""
    age = now - pubdate
    if age <= 0:
        return 1.0
    return max(0.0, 1.0 - age / _MAX_AGE_SEC)


def _log_norm(value: int, cap: int) -> float:
    """播放/点赞这类长尾计数用 log 归一化，避免头部超高播放视频把分数拉爆。"""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1 + value) / math.log10(1 + cap))


def passes_hard_filter(item: dict, now: int | None = None) -> bool:
    now = now if now is not None else int(time.time())
    sec = parse_duration(item.get("duration", ""))
    if sec < _MIN_DURATION_SEC or sec > _MAX_DURATION_SEC:
        return False
    pubdate = item.get("pubdate", 0)
    if pubdate and (now - pubdate) > _MAX_AGE_SEC:
        return False
    return True


def score_item(item: dict, now: int | None = None) -> float:
    now = now if now is not None else int(time.time())
    play = _log_norm(item.get("play", 0), cap=2_000_000)
    like = _log_norm(item.get("like", 0), cap=200_000)
    dur = _duration_fit_score(parse_duration(item.get("duration", "")))
    rec = _recency_score(item.get("pubdate", 0), now)
    return _W_PLAY * play + _W_LIKE * like + _W_DURATION * dur + _W_RECENCY * rec


def _hottest_score(item: dict) -> float:
    """纯热度排序：只看播放+点赞（log归一化后等权相加），不掺入时长/时新度。
    用于用户明确要"最火的"这类单一维度需求——综合分会被其他维度稀释掉排序结果，见docs/ISSUES.md第14条。
    """
    play = _log_norm(item.get("play", 0), cap=2_000_000)
    like = _log_norm(item.get("like", 0), cap=200_000)
    return play + like


_SORT_KEY_FUNCS = {
    "综合": lambda item, now: score_item(item, now),
    "最新": lambda item, now: item.get("pubdate", 0),
    "最热": lambda item, now: _hottest_score(item),
}


def rank_candidates(results: list[dict], top_n: int = 10, now: int | None = None, sort_by: str = "综合") -> list[dict]:
    """对 search 结果做硬过滤 + 排序，返回精简后的候选列表（带 score 和清洗过的字段）。

    sort_by："综合"（默认，播放/点赞/时长/时新加权）、"最新"（纯按发布时间）、
    "最热"（纯按播放+点赞，不看时长/时新度）。score字段固定用综合分展示（方便对照），
    实际排序顺序由sort_by决定，不影响score字段本身的计算方式。
    """
    if sort_by not in _SORT_KEY_FUNCS:
        raise ValueError(f"sort_by={sort_by!r} 不合法，可选：{list(_SORT_KEY_FUNCS)}")

    now = now if now is not None else int(time.time())
    ranked = []
    for item in results:
        if item.get("type") != "video":
            continue
        if not passes_hard_filter(item, now):
            continue
        ranked.append(
            {
                "bvid": item.get("bvid", ""),
                "title": clean_title(item.get("title", "")),
                "author": item.get("author", ""),
                "duration": item.get("duration", ""),
                "play": item.get("play", 0),
                "like": item.get("like", 0),
                "favorites": item.get("favorites", 0),
                "pubdate": item.get("pubdate", 0),
                "description": item.get("description", ""),
                "tag": item.get("tag", ""),
                "source_url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
                "score": round(score_item(item, now), 4),
            }
        )

    sort_key = _SORT_KEY_FUNCS[sort_by]
    ranked.sort(key=lambda x: sort_key(x, now), reverse=True)
    return ranked[:top_n]
