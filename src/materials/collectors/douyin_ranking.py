"""抖音搜索结果的筛选与排序（纯逻辑，可单测，不联网）。

字段来自 MediaCrawler --type search 产出的真实数据（已实测确认，
见 sandbox/MediaCrawler/data/douyin/jsonl/search_contents_*.jsonl）：
  aweme_id aweme_url desc create_time(Unix秒) nickname
  liked_count/comment_count/share_count/collected_count(均为字符串数字)

和B站ranking.py的定位一致，但有两处关键不同（见docs/ISSUES.md第14条）：
1. 抖音短视频没有时长字段，不做B站那种"3~30分钟"时长硬过滤/适配打分。
2. 互动信号更丰富（4个维度：点赞/评论/分享/收藏），综合分沿用log归一化+加权，
   但热度信号本身是这4个维度的组合，不只是播放+点赞两个。

排序信号：高点赞 / 高评论 / 高分享 / 高收藏 / 发布时间近(1年内)。
硬过滤：发布>2年 丢；desc完全为空 丢（没有可判断的文本内容，排序也没有意义）。
"""
from __future__ import annotations

import math
import time

# 硬过滤边界
_MAX_AGE_SEC = 2 * 365 * 24 * 3600

# 综合分权重（互动信号里点赞最能反映内容质量，其次评论/分享，收藏权重最低——
# 抖音收藏行为比点赞更随意，信号强度弱一些，这是经验设定，不是精确校准过的）
_W_LIKE = 0.35
_W_COMMENT = 0.2
_W_SHARE = 0.2
_W_COLLECT = 0.15
_W_RECENCY = 0.1


def _to_int(value) -> int:
    """字段值是字符串数字（如'258640'），容错转换，转不了返回0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _recency_score(create_time: int, now: int) -> float:
    """发布越近分越高：1年内接近1.0，线性衰减到2年为0。"""
    age = now - create_time
    if age <= 0:
        return 1.0
    return max(0.0, 1.0 - age / _MAX_AGE_SEC)


def _log_norm(value: int, cap: int) -> float:
    """点赞/评论等长尾计数用log归一化，避免头部爆款视频把分数拉爆。"""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1 + value) / math.log10(1 + cap))


def passes_hard_filter(item: dict, now: int | None = None) -> bool:
    now = now if now is not None else int(time.time())
    if not (item.get("desc") or "").strip():
        return False
    create_time = item.get("create_time", 0)
    if create_time and (now - create_time) > _MAX_AGE_SEC:
        return False
    return True


def score_item(item: dict, now: int | None = None) -> float:
    now = now if now is not None else int(time.time())
    like = _log_norm(_to_int(item.get("liked_count")), cap=500_000)
    comment = _log_norm(_to_int(item.get("comment_count")), cap=50_000)
    share = _log_norm(_to_int(item.get("share_count")), cap=200_000)
    collect = _log_norm(_to_int(item.get("collected_count")), cap=200_000)
    rec = _recency_score(item.get("create_time", 0), now)
    return _W_LIKE * like + _W_COMMENT * comment + _W_SHARE * share + _W_COLLECT * collect + _W_RECENCY * rec


def _hottest_score(item: dict) -> float:
    """纯热度排序：只看互动信号（点赞+评论+分享+收藏，log归一化后等权相加），不掺入时新度。"""
    like = _log_norm(_to_int(item.get("liked_count")), cap=500_000)
    comment = _log_norm(_to_int(item.get("comment_count")), cap=50_000)
    share = _log_norm(_to_int(item.get("share_count")), cap=200_000)
    collect = _log_norm(_to_int(item.get("collected_count")), cap=200_000)
    return like + comment + share + collect


_SORT_KEY_FUNCS = {
    "综合": lambda item, now: score_item(item, now),
    "最新": lambda item, now: item.get("create_time", 0),
    "最热": lambda item, now: _hottest_score(item),
}


def rank_candidates(results: list[dict], top_n: int = 10, now: int | None = None, sort_by: str = "综合") -> list[dict]:
    """对search结果做硬过滤 + 排序，返回精简后的候选列表（带score和清洗过的字段）。

    sort_by："综合"（默认，点赞/评论/分享/收藏/时新加权）、"最新"（纯按发布时间）、
    "最热"（纯按互动信号，不看时新度）。
    """
    if sort_by not in _SORT_KEY_FUNCS:
        raise ValueError(f"sort_by={sort_by!r} 不合法，可选：{list(_SORT_KEY_FUNCS)}")

    now = now if now is not None else int(time.time())
    ranked = []
    for item in results:
        if not passes_hard_filter(item, now):
            continue
        aweme_id = item.get("aweme_id", "")
        ranked.append(
            {
                "aweme_id": aweme_id,
                "desc": item.get("desc", ""),
                "nickname": item.get("nickname", ""),
                "liked_count": _to_int(item.get("liked_count")),
                "comment_count": _to_int(item.get("comment_count")),
                "share_count": _to_int(item.get("share_count")),
                "collected_count": _to_int(item.get("collected_count")),
                "create_time": item.get("create_time", 0),
                "source_url": item.get("aweme_url", f"https://www.douyin.com/video/{aweme_id}"),
                "score": round(score_item(item, now), 4),
            }
        )

    sort_key = _SORT_KEY_FUNCS[sort_by]
    ranked.sort(key=lambda x: sort_key(x, now), reverse=True)
    return ranked[:top_n]
