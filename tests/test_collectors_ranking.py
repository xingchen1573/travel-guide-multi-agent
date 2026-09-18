"""src/materials/collectors/ranking.py 的单元测试（纯逻辑，不联网）。

    python -m pytest tests/test_collectors_ranking.py -v
"""
from src.materials.collectors.ranking import (
    clean_title,
    parse_duration,
    passes_hard_filter,
    rank_candidates,
    score_item,
)

_NOW = 1_700_000_000  # 固定"当前时间"，保证时新性打分可复现


def test_parse_duration_mmss_and_hhmmss():
    assert parse_duration("10:52") == 652
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("7:02") == 422
    assert parse_duration("") == 0
    assert parse_duration("abc") == 0


def test_clean_title_strips_em_tags():
    assert clean_title('送你<em class="keyword">成都</em>三日攻略</em>') == "送你成都三日攻略"
    assert clean_title("普通标题") == "普通标题"


def test_hard_filter_rejects_too_short_and_too_long():
    assert passes_hard_filter({"duration": "10:00", "pubdate": _NOW}, now=_NOW) is True
    assert passes_hard_filter({"duration": "2:30", "pubdate": _NOW}, now=_NOW) is False  # <3min
    assert passes_hard_filter({"duration": "35:00", "pubdate": _NOW}, now=_NOW) is False  # >30min


def test_hard_filter_rejects_too_old():
    three_years_ago = _NOW - 3 * 365 * 24 * 3600
    assert passes_hard_filter({"duration": "10:00", "pubdate": three_years_ago}, now=_NOW) is False


def test_score_prefers_higher_play_when_others_equal():
    base = {"duration": "10:00", "pubdate": _NOW, "like": 1000}
    high = {**base, "play": 1_000_000}
    low = {**base, "play": 1000}
    assert score_item(high, now=_NOW) > score_item(low, now=_NOW)


def test_score_prefers_ideal_duration():
    base = {"play": 500_000, "like": 10_000, "pubdate": _NOW}
    ideal = {**base, "duration": "12:00"}      # 落在 5~20min 理想区间
    marginal = {**base, "duration": "4:00"}    # 通过硬过滤但偏短
    assert score_item(ideal, now=_NOW) > score_item(marginal, now=_NOW)


def test_rank_candidates_filters_non_video_and_sorts_desc():
    results = [
        {"type": "video", "bvid": "BV1", "duration": "12:00", "play": 100, "like": 10, "pubdate": _NOW},
        {"type": "video", "bvid": "BV2", "duration": "12:00", "play": 1_000_000, "like": 50_000, "pubdate": _NOW},
        {"type": "ketang", "bvid": "BVX", "duration": "12:00", "play": 9_999_999, "like": 99, "pubdate": _NOW},  # 非video应被剔除
        {"type": "video", "bvid": "BV3", "duration": "1:00", "play": 999, "like": 9, "pubdate": _NOW},  # 太短被硬过滤
    ]
    ranked = rank_candidates(results, top_n=10, now=_NOW)
    bvids = [c["bvid"] for c in ranked]
    assert bvids == ["BV2", "BV1"]  # BVX(非video)和BV3(太短)都被排除，且按分数降序
    assert ranked[0]["source_url"] == "https://www.bilibili.com/video/BV2"


def test_rank_candidates_respects_top_n():
    results = [
        {"type": "video", "bvid": f"BV{i}", "duration": "12:00", "play": i * 10000, "like": i * 100, "pubdate": _NOW}
        for i in range(1, 6)
    ]
    ranked = rank_candidates(results, top_n=2, now=_NOW)
    assert len(ranked) == 2


# ---------- sort_by 多维度排序（综合/最新/最热，见docs/ISSUES.md第14条）----------


def test_rank_candidates_sort_by_latest_ignores_engagement():
    # "最新"模式：即使播放/点赞低很多，发布时间更近的应该排在前面
    results = [
        {"type": "video", "bvid": "OLD_HOT", "duration": "12:00", "play": 5_000_000, "like": 500_000, "pubdate": _NOW - 365 * 24 * 3600},
        {"type": "video", "bvid": "NEW_COLD", "duration": "12:00", "play": 100, "like": 10, "pubdate": _NOW},
    ]
    ranked = rank_candidates(results, top_n=10, now=_NOW, sort_by="最新")
    assert [c["bvid"] for c in ranked] == ["NEW_COLD", "OLD_HOT"]


def test_rank_candidates_sort_by_hottest_ignores_recency():
    # "最热"模式：即使发布时间更久远，播放+点赞更高的应该排在前面
    results = [
        {"type": "video", "bvid": "OLD_HOT", "duration": "12:00", "play": 5_000_000, "like": 500_000, "pubdate": _NOW - 365 * 24 * 3600},
        {"type": "video", "bvid": "NEW_COLD", "duration": "12:00", "play": 100, "like": 10, "pubdate": _NOW},
    ]
    ranked = rank_candidates(results, top_n=10, now=_NOW, sort_by="最热")
    assert [c["bvid"] for c in ranked] == ["OLD_HOT", "NEW_COLD"]


def test_rank_candidates_sort_by_default_is_综合():
    results = [
        {"type": "video", "bvid": "BV1", "duration": "12:00", "play": 100, "like": 10, "pubdate": _NOW},
        {"type": "video", "bvid": "BV2", "duration": "12:00", "play": 1_000_000, "like": 50_000, "pubdate": _NOW},
    ]
    default_ranked = rank_candidates(results, top_n=10, now=_NOW)
    explicit_ranked = rank_candidates(results, top_n=10, now=_NOW, sort_by="综合")
    assert [c["bvid"] for c in default_ranked] == [c["bvid"] for c in explicit_ranked]


def test_rank_candidates_rejects_invalid_sort_by():
    try:
        rank_candidates([], sort_by="不存在的模式")
        assert False, "应该拒绝非法sort_by"
    except ValueError:
        pass
