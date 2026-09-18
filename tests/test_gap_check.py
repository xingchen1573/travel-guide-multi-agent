"""src/materials/gap_check.py 的单元测试：不调用真实LLM/网络。

对应docs/ARCHITECTURE.md第16节。重点覆盖：
- score_threshold初筛正确过滤掉低分候选（不需要为此触发_verify_items/LLM调用）。
- 命中判定复用citation_check._verify_items（entity_identity_only=True），
  第一个问法没命中会尝试下一个，任意一个命中就短路返回False。
- 全部问法都没命中才返回True（真缺）。
- _query_core_entities/_normalize_bm25两个纯函数的边界行为。

真实检索校准过程（见gap_check.py模块docstring）已经用真实API对成都素材库验证过
score_threshold=0.3+字面/别名核对的组合能正确区分"真缺"和"共享通用词导致分数虚高"
两种情况，这里的mock测试只覆盖函数内部的分支逻辑，不重复验证真实检索质量。
"""
from __future__ import annotations

from src.materials import citation_check, gap_check
from src.materials.gap_check import (
    _normalize_bm25,
    _query_core_entities,
    check_material_gap,
)


def _fake_result(chunk_id: str, snippet: str, embedding: float | None, bm25: float | None) -> dict:
    return {
        "content_snippet": snippet,
        "chunk_id": chunk_id,
        "score": {"embedding": embedding, "bm25": bm25},
    }


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_normalize_bm25_none_returns_zero():
    assert _normalize_bm25(None) == 0.0


def test_normalize_bm25_caps_at_one():
    assert _normalize_bm25(30.0) == 1.0


def test_normalize_bm25_scales_linearly_below_cap():
    assert _normalize_bm25(7.5) == 0.5


def test_query_core_entities_strips_generic_words():
    assert _query_core_entities("武侯祠 讲解") == ["武侯祠"]


def test_query_core_entities_keeps_multiple_non_generic_words():
    assert _query_core_entities("都江堰 灌县古镇 攻略") == ["都江堰", "灌县古镇"]


def test_query_core_entities_falls_back_to_all_words_when_all_generic():
    # 理论上不应该发生（调用方生成规则总该留一个具体实体），但即使发生也不能返回空列表
    # ——空claimed_entities会被_verify_items判成"完全没有引用依据"的问题分支，语义不对。
    assert _query_core_entities("讲解 攻略") == ["讲解", "攻略"]


# ---------------------------------------------------------------------------
# check_material_gap
# ---------------------------------------------------------------------------


def test_check_material_gap_returns_false_when_first_seed_hits(monkeypatch):
    """第一个问法的候选分数达标+字面核对通过，应该短路返回False，不必尝试后面的问法。
    快照snippet里同时包含"武侯祠""刘备殿"这两个词的字面原文，确保档位A直接通过，
    不触发档位B/C的LLM调用（这是纯mock测试，不应该依赖真实API）。"""
    calls = {"n": 0}

    def fake_retrieve(topic, query, top_k=5):
        calls["n"] += 1
        return [_fake_result("c1", "进入武侯祠后，刘备殿在正中央，殿内供奉刘备像", 0.7, 10.0)]

    monkeypatch.setattr(gap_check, "retrieve_materials", fake_retrieve)

    result = check_material_gap("成都", ["武侯祠 刘备殿", "武侯祠 讲解"])

    assert result is False
    assert calls["n"] == 1  # 第一个问法就命中，不需要跑第二个


def test_check_material_gap_tries_next_seed_when_first_fails(monkeypatch):
    """第一个问法没命中（字面+别名都对不上），应该继续尝试第二个问法。"""
    call_log = []

    def fake_retrieve(topic, query, top_k=5):
        call_log.append(query)
        if query == "布达拉宫 讲解":
            return [_fake_result("c1", "这里是讲解设备租赁处，二十块钱一次", 0.4, 6.0)]
        return [_fake_result("c2", "布达拉宫本身没有出现在这段素材里", 0.6, 9.0)]

    def fake_verify_items(items, chunk_texts, entity_identity_only=False):
        # 第一次调用（针对"布达拉宫 讲解"这个问法）判定未命中；后续调用直接放行。
        if len(call_log) == 1:
            return [{"label": items[0]["label"], "missing_entities": ["布达拉宫"], "reason": "原文没提到"}]
        return []

    monkeypatch.setattr(gap_check, "retrieve_materials", fake_retrieve)
    monkeypatch.setattr(citation_check, "_verify_items", fake_verify_items)

    result = check_material_gap("成都", ["布达拉宫 讲解", "布达拉宫 攻略"])

    assert result is False
    assert call_log == ["布达拉宫 讲解", "布达拉宫 攻略"]


def test_check_material_gap_returns_true_when_all_seeds_fail(monkeypatch):
    def fake_retrieve(topic, query, top_k=5):
        return [_fake_result("c1", "不相关的内容，只是共享了几个通用词", 0.4, 6.0)]

    def fake_verify_items(items, chunk_texts, entity_identity_only=False):
        return [{"label": items[0]["label"], "missing_entities": items[0]["claimed_entities"], "reason": "原文没提到"}]

    monkeypatch.setattr(gap_check, "retrieve_materials", fake_retrieve)
    monkeypatch.setattr(citation_check, "_verify_items", fake_verify_items)

    result = check_material_gap("成都", ["西安兵马俑 一日游 讲解", "兵马俑 攻略"])

    assert result is True


def test_check_material_gap_low_score_skips_verify_items_entirely(monkeypatch):
    """所有候选分数都低于score_threshold时，应该直接判定该问法未命中，
    不需要为此调用_verify_items（避免为明显不沾边的候选触发可能带LLM调用的字面/别名核对）。"""
    verify_calls = {"n": 0}

    def fake_retrieve(topic, query, top_k=5):
        return [_fake_result("c1", "无关内容", 0.1, 1.0)]

    def fake_verify_items(*args, **kwargs):
        verify_calls["n"] += 1
        return []

    monkeypatch.setattr(gap_check, "retrieve_materials", fake_retrieve)
    monkeypatch.setattr(citation_check, "_verify_items", fake_verify_items)

    result = check_material_gap("成都", ["虚构地点 讲解"], score_threshold=0.3)

    assert result is True
    assert verify_calls["n"] == 0


def test_check_material_gap_returns_true_when_no_retrieval_results(monkeypatch):
    monkeypatch.setattr(gap_check, "retrieve_materials", lambda topic, query, top_k=5: [])

    result = check_material_gap("成都", ["某个完全没有素材库的目的地 攻略"])

    assert result is True


def test_check_material_gap_bm25_only_candidate_can_still_pass_threshold(monkeypatch):
    """embedding为None（没进embedding召回的top-k）但bm25分数够高时，combined分数
    应该取两者较大值，让这类候选依然有机会进入字面/别名核对（不能因为embedding缺失
    就直接判定分数不达标）。"""
    def fake_retrieve(topic, query, top_k=5):
        return [_fake_result("c1", "都江堰景区...到冠县古镇继续玩", None, 8.4)]

    monkeypatch.setattr(gap_check, "retrieve_materials", fake_retrieve)
    monkeypatch.setattr(
        citation_check, "_verify_items", lambda items, chunk_texts, entity_identity_only=False: []
    )

    result = check_material_gap("成都", ["都江堰 灌县古镇"], score_threshold=0.3)

    assert result is False
