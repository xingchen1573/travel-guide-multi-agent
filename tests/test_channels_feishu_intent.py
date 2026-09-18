"""src/channels/feishu/intent.py 的单元测试：自然语言→confirm/revise/approve/reject分类，
mock LLM返回，不调真实模型，跟tests/test_orchestrator_companion.py验证
_classify_companion_intent同款手法。
"""
from __future__ import annotations

import pytest

from src.channels.feishu import intent


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    def __init__(self, content):
        self._content = content

    def invoke(self, messages):
        return _FakeResponse(self._content)


# ---------------------------------------------------------------------------
# classify_skeleton_decision
# ---------------------------------------------------------------------------


def test_classify_skeleton_decision_confirm(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("confirm"))
    assert intent.classify_skeleton_decision("挺好的，就这样吧") == "confirm"


def test_classify_skeleton_decision_revise_carries_full_user_message(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("revise"))
    result = intent.classify_skeleton_decision("把第二天的武侯祠换成杜甫草堂")
    assert result == "revise:把第二天的武侯祠换成杜甫草堂"


def test_classify_skeleton_decision_falls_back_to_confirm_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("我不知道"))
    assert intent.classify_skeleton_decision("随便什么回复") == "confirm"


def test_classify_route_structure_decision_reasks_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("无法判断"))
    assert intent.classify_route_structure_decision("随便什么回复") is None


def test_classify_route_structure_decision_reasks_on_ambiguous_category_text(monkeypatch):
    monkeypatch.setattr(
        intent,
        "get_light_chat_model",
        lambda temperature=0: _FakeModel("可能keep，也可能confirm"),
    )
    assert intent.classify_route_structure_decision("随便什么回复") is None


def test_classify_route_structure_decision_maps_unclear_category_to_reask(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("unclear"))
    assert intent.classify_route_structure_decision("嗯，就这样吧") is None


def test_classify_route_structure_decision_reasks_on_classifier_failure(monkeypatch):
    def fail(temperature=0):
        raise TimeoutError("light model unavailable")

    monkeypatch.setattr(intent, "get_light_chat_model", fail)
    assert intent.classify_route_structure_decision("保留吧") is None


def test_classify_route_structure_revision_carries_feedback(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("revise"))
    assert intent.classify_route_structure_decision("换成同一区域的景点") == "revise:换成同一区域的景点"


# ---------------------------------------------------------------------------
# classify_realtime_approval_decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fake_response,expected",
    [("approve", "approve"), ("reject", "reject")],
)
def test_classify_realtime_approval_decision(monkeypatch, fake_response, expected):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel(fake_response))
    assert intent.classify_realtime_approval_decision("随便什么回复") == expected


def test_classify_realtime_approval_decision_falls_back_to_approve_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("嗯嗯嗯"))
    assert intent.classify_realtime_approval_decision("随便什么回复") == "approve"


# ---------------------------------------------------------------------------
# classify_wants_material_collection（2026-07-25新增，陪伴模式接入素材采集意图判断）
# ---------------------------------------------------------------------------


def test_classify_wants_material_collection_true_when_llm_says_yes(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("yes"))
    assert intent.classify_wants_material_collection("帮我再搜几条最新的攻略") is True


def test_classify_wants_material_collection_false_when_llm_says_no(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("no"))
    assert intent.classify_wants_material_collection("武侯祠现在还开着吗") is False


def test_classify_wants_material_collection_falls_back_to_false_on_unparseable_response(monkeypatch):
    """误触发一次真实采集的代价不小（真实网络开销+topic锁占用），解析失败时应该
    保守地不触发，不能默认触发。"""
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("不知道你在说什么"))
    assert intent.classify_wants_material_collection("随便什么回复") is False


# ---------------------------------------------------------------------------
# classify_poster_offer_decision（2026-07-26新增，完成消息主动询问要不要生成海报）
# ---------------------------------------------------------------------------


def test_classify_poster_offer_decision_yes(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("yes"))
    assert intent.classify_poster_offer_decision("要一张") == "yes"


def test_classify_poster_offer_decision_no(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("no"))
    assert intent.classify_poster_offer_decision("不用了") == "no"


def test_classify_poster_offer_decision_falls_back_to_no_on_unparseable_response(monkeypatch):
    """生成海报是真实收费的API调用，解析失败时应该保守地不生成。"""
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("嗯？"))
    assert intent.classify_poster_offer_decision("随便什么回复") == "no"


def _explode(temperature=0):
    raise AssertionError("不应该调用LLM——这条消息应该被关键词快速通道拦下")


def test_classify_poster_offer_decision_fastpath_skips_llm_call(monkeypatch):
    """2026-08-20真实事故回归测试：陪伴阶段问"第二天的行程"这类明显无关的问题，
    落在awaiting=poster_offer期间时，不应该调用LLM（避免商汤relay慢/超时把
    整轮turn拖住十几分钟），必须零延迟判"no"。"""
    monkeypatch.setattr(intent, "get_light_chat_model", _explode)
    assert intent.classify_poster_offer_decision("第二天的行程是怎么安排的？") == "no"


def test_classify_poster_offer_decision_fastpath_does_not_shadow_poster_mentions(monkeypatch):
    """消息里明确提到"海报"时，即使同时含有快速通道关键词，也不能被快速通道拦下——
    必须交给LLM正常判断，不能让快速通道反过来吞掉真实的海报请求。"""
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("yes"))
    assert intent.classify_poster_offer_decision("把第二天的行程做成海报发我") == "yes"


# ---------------------------------------------------------------------------
# classify_wants_user_note（2026-08-21新增，飞书写入入口，见docs/ARCHITECTURE.md第21节）
# ---------------------------------------------------------------------------


def test_classify_wants_user_note_true_when_llm_says_yes(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("yes"))
    assert intent.classify_wants_user_note("记一下Day2宽窄巷子这家火锅鱼要提前订") is True


def test_classify_wants_user_note_false_when_llm_says_no(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("no"))
    # 用户在正常提问/查信息，不是在要求记一条备注
    assert intent.classify_wants_user_note("武侯祠现在还开着吗") is False
    assert intent.classify_wants_user_note("帮我改一下Day2的美食推荐") is False


def test_classify_wants_user_note_falls_back_to_false_on_unparseable_response(monkeypatch):
    """误判代价是一次结构化抽取LLM调用+重建site，且会打断正常陪伴问答，解析失败时应该
    保守地不触发，跟classify_wants_material_collection同一个安全方向。"""
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("不知道你在说什么"))
    assert intent.classify_wants_user_note("随便什么回复") is False


# ---------------------------------------------------------------------------
# classify_wants_new_journey / classify_new_journey_offer_decision
# （2026-07-26新增，一个群多次旅程，见docs/ARCHITECTURE.md第14.4节）
# ---------------------------------------------------------------------------


def test_classify_wants_new_journey_true_when_llm_says_yes(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("yes"))
    assert intent.classify_wants_new_journey("帮我规划一次去大理的3天旅程") is True


def test_classify_wants_new_journey_false_when_llm_says_no(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("no"))
    assert intent.classify_wants_new_journey("大理好玩吗") is False


def test_classify_wants_new_journey_falls_back_to_false_on_unparseable_response(monkeypatch):
    """误判代价是打断正常陪伴问答，解析失败时应该保守地不触发。"""
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("不知道你在说什么"))
    assert intent.classify_wants_new_journey("随便什么回复") is False


def test_classify_new_journey_offer_decision_yes(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("yes"))
    assert intent.classify_new_journey_offer_decision("要，开始吧") == "yes"


def test_classify_new_journey_offer_decision_no(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("no"))
    assert intent.classify_new_journey_offer_decision("不用了") == "no"


def test_classify_new_journey_offer_decision_falls_back_to_no_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(intent, "get_light_chat_model", lambda temperature=0: _FakeModel("嗯？"))
    assert intent.classify_new_journey_offer_decision("随便什么回复") == "no"
