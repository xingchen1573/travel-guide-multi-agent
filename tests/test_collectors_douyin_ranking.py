"""src/materials/collectors/douyin_ranking.py 的单元测试（纯逻辑，不联网）。

    python -m pytest tests/test_collectors_douyin_ranking.py -v
"""
from src.materials.collectors.douyin_ranking import passes_hard_filter, rank_candidates, score_item

_NOW = 1_700_000_000  # 固定"当前时间"，保证时新性打分可复现


def test_passes_hard_filter_rejects_empty_desc():
    assert passes_hard_filter({"desc": "", "create_time": _NOW}, now=_NOW) is False
    assert passes_hard_filter({"desc": "   ", "create_time": _NOW}, now=_NOW) is False
    assert passes_hard_filter({"desc": "有内容", "create_time": _NOW}, now=_NOW) is True


def test_passes_hard_filter_rejects_too_old():
    three_years_ago = _NOW - 3 * 365 * 24 * 3600
    assert passes_hard_filter({"desc": "有内容", "create_time": three_years_ago}, now=_NOW) is False


def test_score_prefers_higher_liked_count():
    base = {"desc": "x", "create_time": _NOW, "comment_count": "100", "share_count": "50", "collected_count": "50"}
    high = {**base, "liked_count": "200000"}
    low = {**base, "liked_count": "10"}
    assert score_item(high, now=_NOW) > score_item(low, now=_NOW)


def test_score_tolerates_string_and_missing_counts():
    # 字段值是字符串数字，且可能缺失，不应该报错
    item = {"desc": "x", "create_time": _NOW, "liked_count": "1000"}
    assert score_item(item, now=_NOW) > 0
    empty = {"desc": "x", "create_time": _NOW}
    assert score_item(empty, now=_NOW) >= 0  # 全空时不报错，热度分为0


def test_rank_candidates_filters_empty_desc_and_sorts_desc():
    results = [
        {"aweme_id": "1", "desc": "有内容1", "liked_count": "100", "comment_count": "10", "share_count": "5", "collected_count": "5", "create_time": _NOW},
        {"aweme_id": "2", "desc": "有内容2", "liked_count": "500000", "comment_count": "50000", "share_count": "200000", "collected_count": "200000", "create_time": _NOW},
        {"aweme_id": "3", "desc": "", "liked_count": "999999", "comment_count": "99999", "share_count": "99999", "collected_count": "99999", "create_time": _NOW},  # 空desc应被剔除
    ]
    ranked = rank_candidates(results, top_n=10, now=_NOW)
    aweme_ids = [c["aweme_id"] for c in ranked]
    assert aweme_ids == ["2", "1"]  # "3"(空desc)被剔除，且按分数降序


def test_rank_candidates_respects_top_n():
    results = [
        {"aweme_id": str(i), "desc": f"内容{i}", "liked_count": str(i * 1000), "create_time": _NOW}
        for i in range(1, 6)
    ]
    ranked = rank_candidates(results, top_n=2, now=_NOW)
    assert len(ranked) == 2


def test_rank_candidates_source_url_fallback_when_missing_aweme_url():
    results = [{"aweme_id": "123456789012345", "desc": "内容", "create_time": _NOW}]
    ranked = rank_candidates(results, top_n=10, now=_NOW)
    assert ranked[0]["source_url"] == "https://www.douyin.com/video/123456789012345"


# ---------- sort_by 多维度排序（综合/最新/最热）----------


def test_rank_candidates_sort_by_latest_ignores_engagement():
    results = [
        {"aweme_id": "OLD_HOT", "desc": "x", "liked_count": "500000", "create_time": _NOW - 300 * 24 * 3600},
        {"aweme_id": "NEW_COLD", "desc": "x", "liked_count": "10", "create_time": _NOW},
    ]
    ranked = rank_candidates(results, top_n=10, now=_NOW, sort_by="最新")
    assert [c["aweme_id"] for c in ranked] == ["NEW_COLD", "OLD_HOT"]


def test_rank_candidates_sort_by_hottest_ignores_recency():
    results = [
        {"aweme_id": "OLD_HOT", "desc": "x", "liked_count": "500000", "comment_count": "50000", "share_count": "200000", "collected_count": "200000", "create_time": _NOW - 300 * 24 * 3600},
        {"aweme_id": "NEW_COLD", "desc": "x", "liked_count": "10", "create_time": _NOW},
    ]
    ranked = rank_candidates(results, top_n=10, now=_NOW, sort_by="最热")
    assert [c["aweme_id"] for c in ranked] == ["OLD_HOT", "NEW_COLD"]


def test_rank_candidates_rejects_invalid_sort_by():
    try:
        rank_candidates([], sort_by="不存在的模式")
        assert False, "应该拒绝非法sort_by"
    except ValueError:
        pass
