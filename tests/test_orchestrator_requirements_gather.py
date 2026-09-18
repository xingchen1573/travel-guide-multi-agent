"""requirements_gather_node的graph级别测试（飞书接入第一期新增，真正的多轮需求收集）。

跟tests/test_orchestrator_skeleton_confirm.py同款手法：不引入完整生产图，搭一个只含
这个节点自身的最小StateGraph，驱动真实的interrupt()/Command(resume=...)机制。

LLM结构化抽取（_extract_requirements_fields）直接monkeypatch掉，不调真实模型——
跟其余orchestrator测试monkeypatch build_xxx_agent/verify_xxx_citations是同一个做法。
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from src import orchestrator
from src.orchestrator import GraphState, requirements_gather_node
from src.workspace import store


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "requirements-gather-test"
    store.init_project(project_id)
    return project_id


def _build_minimal_gather_graph():
    """跟真实main_agent_router一样，requirements未收集完整时要自环回到同一个节点
    （见_route_plan_mode），不能只跑一次就结束——否则测不出"多轮"追问的效果。"""

    def _router(state: GraphState) -> str:
        if orchestrator._missing_required_fields(state.get("requirements") or {}):
            return "requirements_gather_node"
        return END

    graph = StateGraph(GraphState)
    graph.add_node("requirements_gather_node", requirements_gather_node)
    graph.add_conditional_edges(START, _router)
    graph.add_conditional_edges("requirements_gather_node", _router)
    return graph.compile(checkpointer=InMemorySaver())


def _fake_extract(mapping: dict):
    """monkeypatch用：按用户回复原文精确匹配返回预先准备好的抽取结果，
    不识别的回复返回空dict（模拟"这句话没提取出任何字段"）。"""

    def _extract(user_message: str) -> dict:
        return dict(mapping.get(user_message, {}))

    return _extract


# ---------------------------------------------------------------------------
# 首次调用：还没有任何draft时，图应该立刻暂停问"目的地+天数"
# ---------------------------------------------------------------------------


def test_first_call_pauses_asking_destination_and_days(isolated_project):
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    result = graph.invoke({"project_id": isolated_project}, config)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert "想去哪里旅游" in payload["question"]
    assert "打算玩几天" in payload["question"]
    assert payload["draft"] == {}


# ---------------------------------------------------------------------------
# 必需字段一次性给全：应该跳过必需字段追问，直接问可选偏好
# ---------------------------------------------------------------------------


def test_answering_both_required_fields_advances_to_optional_question(isolated_project, monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract({"成都玩3天": {"destination": "成都", "days": 3}}),
    )
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    result = graph.invoke(Command(resume="成都玩3天"), config)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert "偏好" in payload["question"]
    assert payload["draft"]["destination"] == "成都"
    assert payload["draft"]["days"] == 3
    # 内部记账字段不应该泄露到展示payload里
    assert "_optional_asked" not in payload["draft"]


# ---------------------------------------------------------------------------
# 必需字段分两轮问齐：第一轮只答目的地，第二轮补天数，仍然继续追问可选偏好
# ---------------------------------------------------------------------------


def test_required_fields_can_be_filled_across_multiple_rounds(isolated_project, monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract({
            "去成都": {"destination": "成都"},
            "玩3天": {"days": 3},
        }),
    )
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    still_missing = graph.invoke(Command(resume="去成都"), config)

    # 天数还缺，应该继续追问，而不是误判成"齐全了"
    assert "__interrupt__" in still_missing
    assert "打算玩几天" in still_missing["__interrupt__"][0].value["question"]
    assert "想去哪里旅游" not in still_missing["__interrupt__"][0].value["question"]

    optional_round = graph.invoke(Command(resume="玩3天"), config)
    assert "__interrupt__" in optional_round
    assert "偏好" in optional_round["__interrupt__"][0].value["question"]


# ---------------------------------------------------------------------------
# 用户在可选偏好轮回答"没有"：应该落默认值并真正完成收集，不再继续追问
# ---------------------------------------------------------------------------


def test_optional_round_no_preference_falls_back_to_defaults_and_completes(isolated_project, monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract({
            "成都玩3天": {"destination": "成都", "days": 3},
            "没有": {},
        }),
    )
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    graph.invoke(Command(resume="成都玩3天"), config)
    result = graph.invoke(Command(resume="没有"), config)

    assert "__interrupt__" not in result
    assert result["requirements"] == {
        "destination": "成都",
        "days": 3,
        "style": "不限",
        "budget": "不限",
        "notes": "无",
        # origin/transport 是新增的可选字段（用户没说出发地/交通方式时落默认值），
        # route_agent 第13条用 "origin != '未告知'" 判断要不要给城际大交通方案。
        "origin": "未告知",
        "transport": "不限",
    }
    assert result["topic"] == "成都"
    assert result["last_agent_result"]["agent"] == "main_agent"


def test_optional_round_with_explicit_preferences_keeps_them_not_defaults(isolated_project, monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract({
            "成都玩3天": {"destination": "成都", "days": 3},
            "美食为主，预算不限，不吃辣": {"style": "美食为主", "budget": "不限", "notes": "不吃辣"},
        }),
    )
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    graph.invoke(Command(resume="成都玩3天"), config)
    result = graph.invoke(Command(resume="美食为主，预算不限，不吃辣"), config)

    assert "__interrupt__" not in result
    assert result["requirements"]["style"] == "美食为主"
    assert result["requirements"]["notes"] == "不吃辣"


# ---------------------------------------------------------------------------
# _missing_required_fields / _route_plan_mode 的分支判断
# ---------------------------------------------------------------------------


def test_missing_required_fields_only_checks_destination_and_days():
    assert orchestrator._missing_required_fields({}) == ["destination", "days"]
    assert orchestrator._missing_required_fields({"destination": "成都"}) == ["days"]
    assert orchestrator._missing_required_fields({"destination": "成都", "days": 3}) == []
    # style/budget/notes缺失不影响判断——它们是可选字段，不是硬性必需
    assert orchestrator._missing_required_fields({"destination": "成都", "days": 3, "style": None}) == []


def test_route_plan_mode_routes_to_gather_node_when_requirements_incomplete(isolated_project):
    state = {
        "project_id": isolated_project,
        "topic": "",
        "requirements": {},
        "material_checked": True,
    }
    assert orchestrator._route_plan_mode(state) == "requirements_gather_node"


def test_route_plan_mode_routes_to_legacy_collect_requirements_when_requirements_complete(isolated_project):
    """CLI/API等一次性传入完整requirements的既有调用方不应该被路由进多轮收集节点，
    这是run_mvp.py等既有用法的向后兼容保证。"""
    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": {"destination": "成都", "days": 3, "style": "休闲", "budget": "中等", "notes": "无"},
        "material_checked": True,
    }
    assert orchestrator._route_plan_mode(state) == "collect_requirements"


# ---------------------------------------------------------------------------
# must_visit / style_interpretation（docs/ARCHITECTURE.md第18节）
# ---------------------------------------------------------------------------


def test_merge_must_visit_appends_new_and_overwrites_same_name_day():
    existing = [{"name": "熊猫基地", "day": None}]
    new = [{"name": "熊猫基地", "day": 2}, {"name": "都江堰", "day": 3}]
    merged = orchestrator._merge_must_visit(existing, new)
    assert merged == [
        {"name": "熊猫基地", "day": 2},
        {"name": "都江堰", "day": 3},
    ]


def test_must_visit_confirm_question_lists_all_items_with_day_or_unspecified():
    question = orchestrator._must_visit_confirm_question(
        [{"name": "熊猫基地", "day": 2}, {"name": "都江堰", "day": None}]
    )
    assert "熊猫基地（第2天）" in question
    assert "都江堰（未指定具体哪天）" in question


def test_full_flow_asks_must_visit_confirmation_before_finalizing(isolated_project, monkeypatch):
    """必需+可选字段收集完、且用户提到了必去地点时，不能直接定稿——必须先弹出一轮
    "我理解您是...对吗"的确认，用户明确confirm才真正写入state["requirements"]。"""
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract(
            {
                "成都玩3天，一定要去熊猫基地": {
                    "destination": "成都",
                    "days": 3,
                    "must_visit": [{"name": "熊猫基地", "day": None}],
                },
                "没有": {},
            }
        ),
    )
    monkeypatch.setattr(orchestrator, "_classify_must_visit_confirmation", lambda answer: "确认" in answer)
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    graph.invoke(Command(resume="成都玩3天，一定要去熊猫基地"), config)
    optional_round = graph.invoke(Command(resume="没有"), config)

    # 可选字段问完，理应转入must_visit确认这一轮，而不是直接定稿
    assert "__interrupt__" in optional_round
    payload = optional_round["__interrupt__"][0].value
    assert "熊猫基地" in payload["question"]
    assert "确认" in payload["question"]

    confirmed = graph.invoke(Command(resume="确认无误"), config)
    assert "__interrupt__" not in confirmed
    assert confirmed["requirements"]["must_visit"] == [{"name": "熊猫基地", "day": None}]


def test_must_visit_correction_merges_and_reprompts_until_confirmed(isolated_project, monkeypatch):
    """用户对确认轮提出修正（不是简单说"对"）：合并修正内容，重新展示确认，
    不会被静默当成确认通过——跟await_skeleton_confirm_node同款循环确认模式。"""
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract(
            {
                "成都玩3天，一定要去熊猫基地": {
                    "destination": "成都",
                    "days": 3,
                    "must_visit": [{"name": "熊猫基地", "day": None}],
                },
                "没有": {},
                "不对，是第2天": {"must_visit": [{"name": "熊猫基地", "day": 2}]},
            }
        ),
    )
    monkeypatch.setattr(orchestrator, "_classify_must_visit_confirmation", lambda answer: "确认" in answer)
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    graph.invoke(Command(resume="成都玩3天，一定要去熊猫基地"), config)
    graph.invoke(Command(resume="没有"), config)

    corrected = graph.invoke(Command(resume="不对，是第2天"), config)
    # 提出修正后还没有明确确认，应该继续停在确认轮，不能被误判成通过
    assert "__interrupt__" in corrected
    payload = corrected["__interrupt__"][0].value
    assert "第2天" in payload["question"]

    confirmed = graph.invoke(Command(resume="确认"), config)
    assert "__interrupt__" not in confirmed
    assert confirmed["requirements"]["must_visit"] == [{"name": "熊猫基地", "day": 2}]


def test_no_must_visit_skips_confirmation_round(isolated_project, monkeypatch):
    """用户完全没提必去地点/草稿时，可选轮问完直接定稿，不应该凭空多出一轮确认。"""
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract({"成都玩3天": {"destination": "成都", "days": 3}, "没有": {}}),
    )
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    graph.invoke(Command(resume="成都玩3天"), config)
    result = graph.invoke(Command(resume="没有"), config)

    assert "__interrupt__" not in result
    assert "must_visit" not in result["requirements"]


def test_style_interpretation_passes_through_without_separate_confirmation(isolated_project, monkeypatch):
    """style_interpretation是从style自由文本派生的软信号，不需要用户单独确认——
    可选轮问完（且没有must_visit）应该直接定稿，style_interpretation原样出现在requirements里。"""
    monkeypatch.setattr(
        orchestrator,
        "_extract_requirements_fields",
        _fake_extract(
            {
                "成都玩3天": {"destination": "成都", "days": 3},
                "深度游，慢慢逛": {
                    "style": "深度游，慢慢逛",
                    "style_interpretation": {
                        "poi_density": "偏少",
                        "stay_duration": "偏多",
                        "needs_detail_bias": "偏多",
                        "flexible_time": "偏多",
                    },
                },
            }
        ),
    )
    graph = _build_minimal_gather_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:gather-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    graph.invoke(Command(resume="成都玩3天"), config)
    result = graph.invoke(Command(resume="深度游，慢慢逛"), config)

    assert "__interrupt__" not in result
    assert result["requirements"]["style_interpretation"] == {
        "poi_density": "偏少",
        "stay_duration": "偏多",
        "needs_detail_bias": "偏多",
        "flexible_time": "偏多",
    }
