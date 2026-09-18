"""_infer_edit_scope/_is_edit_overreach（docs/ARCHITECTURE.md第20.2节，edit模式误改检测
从静态阈值1改为动态语义比对）的单元测试。

不调用真实LLM——monkeypatch get_light_chat_model返回受控的假模型，模拟
".with_structured_output(...).invoke(...)"链路，跟tests/test_orchestrator_gap_relevance_filter.py
同款风格。
"""
from __future__ import annotations

import pytest

from src import orchestrator
from src.orchestrator import (
    _infer_edit_scope,
    _is_edit_overreach,
    lifestyle_agent_node,
    pitfall_agent_node,
    route_draft_node,
)
from src.workspace import store
from src.workspace.schema import LIFESTYLE_FIELDS, RouteDay, RoutePlan, RoutePoi

from conftest import fake_agent_reply


# ---------------------------------------------------------------------------
# _infer_edit_scope / _is_edit_overreach 纯函数级测试
# ---------------------------------------------------------------------------


class _FakeVerdict:
    def __init__(self, covered: list[str]):
        self.covered = covered


class _FakeStructuredModel:
    def __init__(self, covered: list[str]):
        self._covered = covered

    def invoke(self, prompt: str):
        return _FakeVerdict(self._covered)


class _FakeLightModel:
    def __init__(self, covered: list[str]):
        self._covered = covered

    def with_structured_output(self, schema, method=None):
        return _FakeStructuredModel(self._covered)


def test_infer_edit_scope_returns_covered_set_from_model(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food", "play"]))

    scope = _infer_edit_scope("把Day2的美食和游玩都换一下", {"food", "outfit", "lodging", "play"})

    assert scope == {"food", "play"}


def test_infer_edit_scope_ignores_labels_outside_candidates(monkeypatch):
    """模型幻觉出候选集合之外的值时应该被过滤掉，不污染返回的范围。"""
    monkeypatch.setattr(
        orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food", "not_a_real_field"])
    )

    scope = _infer_edit_scope("换一下吃的", {"food", "outfit", "lodging", "play"})

    assert scope == {"food"}


def test_infer_edit_scope_merges_conflict_target_field(monkeypatch):
    """conflict_target非空时（系统核验打回重试），它指向的具体字段应该被并入返回范围——
    覆盖"因核验发现问题被指定回来修，顺带做了语义上合理的连带修正"场景。"""
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food"]))

    scope = _infer_edit_scope(
        "调整一下这条推荐", {"food", "outfit", "lodging", "play"}, conflict_target="lifestyle_agent.play"
    )

    assert scope == {"food", "play"}


def test_infer_edit_scope_merges_conflict_target_day(monkeypatch):
    """天数候选集合（route场景），conflict_target形如"route_agent.day2"——day前缀要剥掉
    才能匹配到候选集合里的整数day编号。"""
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["1"]))

    scope = _infer_edit_scope("把第一天改一下", {1, 2, 3}, conflict_target="route_agent.day2")

    assert scope == {1, 2}


def test_infer_edit_scope_empty_candidates_returns_empty_without_calling_model(monkeypatch):
    def fail_if_called(temperature=0):
        raise AssertionError("候选为空时不该调用模型")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", fail_if_called)

    assert _infer_edit_scope("随便改改", set()) == set()


def test_infer_edit_scope_returns_none_on_model_failure(monkeypatch):
    """LLM调用异常时返回None，供调用方降级回退到静态阈值1判断。"""

    def raise_error(temperature=0):
        raise RuntimeError("模型调用失败")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", raise_error)

    assert _infer_edit_scope("随便改改", {"food", "play"}) is None


def test_is_edit_overreach_true_when_changed_exceeds_inferred_scope(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food"]))

    assert _is_edit_overreach("换一下吃的", ["food", "play"], {"food", "outfit", "lodging", "play"}, None) is True


def test_is_edit_overreach_false_when_changed_within_inferred_scope(monkeypatch):
    """核心场景：用户请求天然涉及多个字段（"把Day2的美食和游玩都换一下"），实际改动也确实
    落在这个范围内——不应该被静态阈值1误判成越权。"""
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food", "play"]))

    assert (
        _is_edit_overreach(
            "把Day2的美食和游玩都换一下", ["food", "play"], {"food", "outfit", "lodging", "play"}, None
        )
        is False
    )


def test_is_edit_overreach_falls_back_to_static_threshold_on_model_failure(monkeypatch):
    """降级路径：LLM判断失败时退回len(changed) > 1，跟改动前的行为完全一致。"""

    def raise_error(temperature=0):
        raise RuntimeError("模型调用失败")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", raise_error)

    assert _is_edit_overreach("随便改改", ["food", "play"], {"food", "outfit", "lodging", "play"}, None) is True
    assert _is_edit_overreach("随便改改", ["food"], {"food", "outfit", "lodging", "play"}, None) is False


# ---------------------------------------------------------------------------
# 三处调用点集成测试：验证动态语义比对确实接管了原有的静态阈值判断
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "edit-scope-test"
    store.init_project(project_id)
    store.write_route_plan(project_id, actor="route_agent", route_plan=RoutePlan(days=[]))
    return project_id


def _seed_lifestyle(project_id: str, text: str = "原始内容") -> None:
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {"field": f, "day": 1, "recommendation": text, "sources": [], "claimed_entities": []}
            for f in LIFESTYLE_FIELDS
        ],
    )


def test_lifestyle_agent_node_no_conflict_when_multi_field_request_covers_all_changes(isolated_project, monkeypatch):
    """核心验证：用户请求「把Day2的美食和游玩都换一下」，agent正确执行后食物+游玩两个
    字段都变了（len(changed)==2）——旧的静态阈值1会误判越权，新机制应该正确放行。"""
    _seed_lifestyle(isolated_project)
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food", "play"]))
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": "food", "day": 1, "recommendation": "新美食", "sources": [], "claimed_entities": []},
                {"field": "play", "day": 1, "recommendation": "新玩法", "sources": [], "claimed_entities": []},
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": 3},
        "entry_target": "lifestyle_agent_node",
        "edit_request": "把Day2的美食和游玩都换一下",
        "last_agent_result": None,
    }
    result = lifestyle_agent_node(state)

    assert store.get_pending_conflict(isolated_project) is None
    assert set(result["edit_changed_lifestyle_fields"]) == {"food", "play"}


def test_lifestyle_agent_node_reports_conflict_when_changes_exceed_inferred_scope(isolated_project, monkeypatch):
    """反面验证：用户只要求改food，模型的推断范围也只有food，但实际改动了food+outfit两个
    字段——超出允许范围应该照样触发report_conflict。"""
    _seed_lifestyle(isolated_project)
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food"]))
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": "food", "day": 1, "recommendation": "新美食", "sources": [], "claimed_entities": []},
                {"field": "outfit", "day": 1, "recommendation": "意外改动的穿搭", "sources": [], "claimed_entities": []},
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": 3},
        "entry_target": "lifestyle_agent_node",
        "edit_request": "把Day1的美食换一下",
        "last_agent_result": None,
    }
    result = lifestyle_agent_node(state)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.issue_type == "lifestyle_edit_overreach"


def _seed_route(project_id: str, days: list[dict]) -> None:
    route_days = [
        RouteDay(
            day=d["day"],
            city=d.get("city", "成都"),
            area=d.get("area", ""),
            notes=d.get("notes", ""),
            pois=[RoutePoi(name=d.get("poi", f"Day{d['day']}景点"))],
        )
        for d in days
    ]
    store.write_route_plan(project_id, actor="route_agent", route_plan=RoutePlan(days=route_days))


def test_route_draft_node_no_conflict_when_request_covers_multiple_days(isolated_project, monkeypatch):
    """route场景同款验证：用户请求「把Day1和Day2都调整一下顺序」天然涉及两天，
    不应该被静态阈值1误判越权。"""
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}, {"day": 2, "area": "郊区"}, {"day": 3, "area": "市区"}])
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["1", "2"]))
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        _seed_route(
            isolated_project,
            [{"day": 1, "area": "调整后市区"}, {"day": 2, "area": "调整后郊区"}, {"day": 3, "area": "市区"}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": 3},
        "entry_target": "route_draft",
        "edit_request": "把Day1和Day2都调整一下顺序",
        "last_agent_result": None,
    }
    result = route_draft_node(state)

    assert store.get_pending_conflict(isolated_project) is None
    assert set(result["edit_changed_route_days"]) == {1, 2}


def test_route_draft_node_reports_conflict_when_days_exceed_inferred_scope(isolated_project, monkeypatch):
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}, {"day": 2, "area": "郊区"}, {"day": 3, "area": "市区"}])
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["2"]))
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        _seed_route(
            isolated_project,
            [{"day": 1, "area": "意外改动"}, {"day": 2, "area": "都江堰"}, {"day": 3, "area": "市区"}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": 3},
        "entry_target": "route_draft",
        "edit_request": "把第二天改成都江堰",
        "last_agent_result": None,
    }
    route_draft_node(state)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.issue_type == "route_edit_overreach"


def _seed_pitfall_with_two_fields(project_id: str) -> None:
    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [
            {"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": "药膳味较重"},
            {"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "纪念馆非原址"},
        ],
    )


def test_pitfall_agent_node_no_conflict_when_request_covers_both_fields(isolated_project, monkeypatch):
    """pitfall场景同款验证：用户请求「杜甫草堂和胡二哥药膳蹄花都再查一下」天然涉及
    food+play两个字段，不应该被静态阈值1误判越权。"""
    _seed_lifestyle(isolated_project)
    _seed_pitfall_with_two_fields(isolated_project)
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["food", "play"]))
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [
                {"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": "更新后的内容"},
                {"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "更新后的内容"},
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "entry_target": "pitfall_agent_node",
        "edit_request": "杜甫草堂和胡二哥药膳蹄花都再查一下",
        "last_agent_result": None,
    }
    result = pitfall_agent_node(state)

    assert store.get_pending_conflict(isolated_project) is None
    assert set(result["edit_changed_pitfall_fields"]) == {"food", "play"}


def test_pitfall_agent_node_reports_conflict_when_changes_exceed_inferred_scope(isolated_project, monkeypatch):
    _seed_lifestyle(isolated_project)
    _seed_pitfall_with_two_fields(isolated_project)
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["play"]))
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [
                {"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": ""},
                {"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "更新后的内容"},
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "entry_target": "pitfall_agent_node",
        "edit_request": "再帮我查查杜甫草堂有没有坑",
        "last_agent_result": None,
    }
    result = pitfall_agent_node(state)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.issue_type == "pitfall_edit_overreach"


def test_route_draft_node_conflict_target_merged_into_allowed_scope(isolated_project, monkeypatch):
    """系统核验打回重试场景：pending conflict.target指向day2，即使用户原始edit_request
    只字面提到day1，day2也应该被并入允许范围（"顺带做了语义上合理的连带修正"）。"""
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}, {"day": 2, "area": "郊区"}, {"day": 3, "area": "市区"}])
    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel(["1"]))
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    store.report_conflict(
        isolated_project, actor="citation_checker", target="route_agent.day2", reason="Day2引用核验未通过"
    )

    def fake_invoke(agent, messages, **kwargs):
        _seed_route(
            isolated_project,
            [{"day": 1, "area": "修好的市区"}, {"day": 2, "area": "修好的郊区"}, {"day": 3, "area": "市区"}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": 3},
        "entry_target": "route_draft",
        "edit_request": "把第一天修一下",
        "last_agent_result": None,
    }
    route_draft_node(state)

    # route_draft_node本身不负责resolve既有的pending conflict（那是citation_check重试循环
    # 的职责），这里只断言它没有额外触发一条新的越权report_conflict——day1+day2都在允许
    # 范围内（day1来自edit_request语义推断，day2来自conflict_target并入），不应该被判越权。
    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.issue_type != "route_edit_overreach"
