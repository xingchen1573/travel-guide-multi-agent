"""src/tools/material_collection_tools.py 的单元测试（去重逻辑，monkeypatch底层搜索函数，不联网不调LLM）。

    python -m pytest tests/test_material_collection_tools.py -v
"""
import src.materials.collectors.bilibili as bilibili_module
import src.materials.collectors.douyin as douyin_module
from src.tools.material_collection_tools import make_collection_tools


def test_search_bilibili_deduplicates_across_calls(monkeypatch):
    """seen集合应该跨多次调用生效——第二次搜到同一个bvid不应该再出现在结果里。"""
    call_count = []

    def fake_search_and_rank(keywords, top_n, topic, sort_by="综合", page=1):
        call_count.append(page)
        return [
            {"bvid": "BV1", "title": "视频1", "score": 0.9},
            {"bvid": "BV2", "title": "视频2", "score": 0.8},
        ]

    monkeypatch.setattr(bilibili_module, "search_and_rank", fake_search_and_rank)

    captured, seen = {}, set()
    tools = make_collection_tools("成都", captured, seen)
    search_bilibili = next(t for t in tools if t.name == "search_bilibili")

    first = search_bilibili.invoke({"keywords": ["成都攻略"]})
    assert "BV1" in first and "BV2" in first

    second = search_bilibili.invoke({"keywords": ["成都攻略"], "page": 2})
    assert "没有搜到新候选" in second  # 两次返回的是同一批bvid，第二次应该被去重掉


def test_search_bilibili_tags_platform_field(monkeypatch):
    monkeypatch.setattr(
        bilibili_module, "search_and_rank",
        lambda keywords, top_n, topic, sort_by="综合", page=1: [{"bvid": "BV1", "title": "视频1", "score": 0.9}],
    )
    captured, seen = {}, set()
    tools = make_collection_tools("成都", captured, seen)
    search_bilibili = next(t for t in tools if t.name == "search_bilibili")

    result = search_bilibili.invoke({"keywords": ["成都攻略"]})
    assert '"platform": "bilibili"' in result


def test_search_douyin_deduplicates_across_calls(monkeypatch):
    monkeypatch.setattr(douyin_module, "trigger_search", lambda keywords: None)
    monkeypatch.setattr(
        douyin_module, "list_ranked_candidates",
        lambda topic, top_n, sort_by="综合": [{"aweme_id": "111", "desc": "内容1", "score": 0.9}],
    )

    captured, seen = {}, set()
    tools = make_collection_tools("成都", captured, seen)
    search_douyin = next(t for t in tools if t.name == "search_douyin")

    first = search_douyin.invoke({"keywords": ["成都攻略"]})
    assert "111" in first

    second = search_douyin.invoke({"keywords": ["成都攻略"]})
    assert "没有搜到新候选" in second


def test_propose_candidates_writes_to_captured():
    captured, seen = {}, set()
    tools = make_collection_tools("成都", captured, seen)
    propose_candidates = next(t for t in tools if t.name == "propose_candidates")

    result = propose_candidates.invoke({
        "candidates": [{"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"}],
        "summary": "搜了成都攻略关键词",
    })

    assert "已提交1条候选" in result
    assert captured["candidates"] == [{"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"}]
    assert captured["summary"] == "搜了成都攻略关键词"


# ---------------------------------------------------------------------------
# platform硬过滤（2026-07-26新增）：不绑定对应搜索工具，模型根本没机会调用，
# 不是靠prompt软提示，见管理后台采集对话框的显式平台选择器。
# ---------------------------------------------------------------------------


def test_default_platform_all_exposes_both_search_tools():
    tools = make_collection_tools("成都", {}, set())
    names = {t.name for t in tools}
    assert names == {"search_bilibili", "search_douyin", "propose_candidates"}


def test_platform_bilibili_excludes_search_douyin_tool():
    tools = make_collection_tools("成都", {}, set(), platform="bilibili")
    names = {t.name for t in tools}
    assert names == {"search_bilibili", "propose_candidates"}


def test_platform_douyin_excludes_search_bilibili_tool():
    tools = make_collection_tools("成都", {}, set(), platform="douyin")
    names = {t.name for t in tools}
    assert names == {"search_douyin", "propose_candidates"}
