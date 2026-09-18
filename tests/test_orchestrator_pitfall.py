"""避雷agent（pitfall_agent）的mock单元测试——docs/ARCHITECTURE.md第12.1节P1第5项。

不调用真实LLM：跟tests/test_orchestrator_core_nodes.py/test_orchestrator_edit_mode.py同款风格，
monkeypatch build_pitfall_agent/invoke_with_retry，用一个可控的副作用函数模拟"模型这次做了什么"。

误改检测默认用_fail_light_model autouse fixture让get_light_chat_model失败，_is_edit_overreach
降级回退静态阈值1判断（见docs/ARCHITECTURE.md第20.2节、tests/test_orchestrator_edit_mode.py
同款fixture说明）。
"""
import json

import pytest

from src import orchestrator
from src.materials import citation_check
from src.orchestrator import (
    MAX_RETRY,
    _compute_pitfall_source_counts,
    _route_edit_mode,
    _route_plan_mode,
    integration_agent_node,
    lifestyle_agent_node,
    pitfall_agent_node,
    pitfall_citation_check_node,
)
from src.workspace import store
from src.workspace.schema import (
    LIFESTYLE_FIELDS,
    ProjectPhase,
    RoutePlan,
    SourceRef,
)

from conftest import fake_agent_reply


@pytest.fixture(autouse=True)
def _fail_light_model(monkeypatch):
    def _raise(temperature=0):
        raise RuntimeError("测试环境不调用真实LLM")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", _raise)


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "pitfall-test"
    store.init_project(project_id)
    store.write_route_plan(project_id, actor="route_agent", route_plan=RoutePlan(days=[]))
    return project_id


def _seed_lifestyle(project_id: str, recommendation: str = "叶婆婆钵钵鸡", sources: list | None = None) -> None:
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": recommendation, "sources": sources or [], "claimed_entities": []}],
    )


def _write_pitfall(project_id: str, food_items: list | None = None) -> None:
    store.write_pitfall_output_patch(project_id, "pitfall_agent", food_items or [])


# ---------------------------------------------------------------------------
# pitfall_agent_node
# ---------------------------------------------------------------------------


def test_pitfall_agent_plan_mode_prompt_includes_lifestyle_content(isolated_project, monkeypatch):
    _seed_lifestyle(isolated_project, recommendation="叶婆婆钵钵鸡")
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _write_pitfall(
            isolated_project,
            [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = pitfall_agent_node(state)

    assert "叶婆婆钵钵鸡" in captured_prompts[0]
    assert result["pitfall_agent_done"] is True
    assert result["last_agent_result"]["agent"] == "pitfall_agent"


def test_pitfall_agent_allows_legitimate_empty_result(isolated_project, monkeypatch):
    """核心行为：没查到避雷内容是合法结果，但必须为实际存在的Lifestyle键提交空warning。"""
    _seed_lifestyle(isolated_project)
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        _write_pitfall(
            isolated_project,
            [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = pitfall_agent_node(state)

    assert result["pitfall_agent_done"] is True


def test_pitfall_agent_retries_when_never_written(isolated_project, monkeypatch):
    """对照组：模型完全没调用write_agent_output（outputs/pitfall.json此前不存在，
    这次也没写）时，应该走重试逻辑，而不是被误判成"允许的空结果"直接放行。"""
    _seed_lifestyle(isolated_project)
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())

    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            _write_pitfall(
                isolated_project,
                [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
            )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = pitfall_agent_node(state)

    assert calls["n"] == 3  # 前两次没写，触发了重试
    assert result["pitfall_agent_done"] is True


def test_pitfall_agent_retries_until_full_lifestyle_coverage(isolated_project, monkeypatch):
    """全量生成不能只写一部分field/day后靠updated_at蒙混过关。"""
    store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [
            {"field": "food", "day": 1, "recommendation": "叶婆婆钵钵鸡", "sources": []},
            {"field": "play", "day": 1, "recommendation": "杜甫草堂", "sources": []},
        ],
    )
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _write_pitfall(
                isolated_project,
                [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
            )
        else:
            store.write_pitfall_output_patch(
                isolated_project,
                "pitfall_agent",
                [{"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": ""}],
            )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = pitfall_agent_node({"project_id": isolated_project, "topic": "成都", "mode": "plan"})

    assert calls["n"] == 2
    assert result["pitfall_agent_done"] is True


def test_pitfall_agent_requires_fresh_write_when_complete_output_already_exists(isolated_project, monkeypatch):
    """旧pitfall.json即使覆盖完整，也不能替代本轮Agent真正执行写入。"""
    _seed_lifestyle(isolated_project)
    _write_pitfall(
        isolated_project,
        [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
    )
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    with pytest.raises(RuntimeError, match="仍未完成必需的写入操作"):
        pitfall_agent_node({"project_id": isolated_project, "topic": "成都", "mode": "plan"})

    assert calls["n"] == 3


def test_pitfall_agent_resume_accepts_complete_output_from_pending_checkpoint(isolated_project, monkeypatch):
    """同一内层Checkpoint未完成，但工具副作用已完整落盘时，恢复不应重复研究。"""
    _seed_lifestyle(isolated_project)
    _write_pitfall(
        isolated_project,
        [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
    )

    class PendingSnapshot:
        values = {"messages": ["已有上下文"]}
        next = ("tools",)

    class PendingAgent:
        def get_state(self, config):
            assert config["configurable"]["thread_id"] == f"{isolated_project}:agent:pitfall_draft"
            return PendingSnapshot()

    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: PendingAgent())
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda *args, **kwargs: pytest.fail("完整落盘的pending checkpoint不应重新调用模型"),
    )
    monkeypatch.setattr(
        orchestrator.research_service,
        "retrieve_context",
        lambda *args, **kwargs: pytest.fail("恢复验收不应重新执行素材预检"),
    )

    result = pitfall_agent_node({"project_id": isolated_project, "topic": "成都", "mode": "plan"})

    assert result == {
        "pitfall_agent_done": True,
        "last_agent_result": {"agent": "pitfall_agent", "summary": "避雷检索完成"},
    }


def test_pitfall_agent_node_wires_inner_checkpoint(isolated_project, monkeypatch):
    """Pitfall正式生成应与Lifestyle一样把checkpointer/thread_id传入ReAct调用。"""
    _seed_lifestyle(isolated_project)
    sentinel_checkpointer = object()
    captured = {}

    class FakeAgent:
        checkpointer = sentinel_checkpointer

    def fake_clear(project_id, node_name, suffix=""):
        captured["cleared"] = (project_id, node_name, suffix)

    def fake_build(project_id, topic, **kwargs):
        captured["build_checkpointer"] = kwargs.get("checkpointer")
        return FakeAgent()

    def fake_invoke(agent, messages, **kwargs):
        captured["invoke_kwargs"] = kwargs
        _write_pitfall(
            isolated_project,
            [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "clear_agent_thread", fake_clear)
    monkeypatch.setattr(orchestrator, "get_agent_checkpointer", lambda project_id: sentinel_checkpointer)
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", fake_build)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    pitfall_agent_node({"project_id": isolated_project, "topic": "成都", "mode": "plan"})

    assert captured["cleared"] == (isolated_project, "pitfall_draft", "")
    assert captured["build_checkpointer"] is sentinel_checkpointer
    assert captured["invoke_kwargs"] == {
        "thread_id": f"{isolated_project}:agent:pitfall_draft",
        "resume_from_checkpoint": False,
    }


def test_pitfall_agent_plan_mode_partitions_multi_day_work_units(isolated_project, monkeypatch):
    """8天类输入不应再把所有推荐和检索结果堆进同一个ReAct线程。"""
    store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [
            {"field": "food", "day": 1, "recommendation": "Day1特色餐"},
            {"field": "food", "day": 2, "recommendation": "Day2特色餐"},
        ],
    )
    calls = []

    class EmptySnapshot:
        values = {}
        next = ()

    class FakeAgent:
        checkpointer = object()

        def get_state(self, config):
            return EmptySnapshot()

    def fake_build(project_id, topic, **kwargs):
        calls.append({"kind": "build", "work_unit": kwargs.get("convergence_work_unit")})
        return FakeAgent()

    def fake_invoke(agent, messages, **kwargs):
        prompt = messages[0]["content"]
        day = 1 if "本工作单元只处理Day1" in prompt else 2
        recommendation = f"Day{day}特色餐"
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [
                {
                    "field": "food",
                    "day": day,
                    "target_recommendation": recommendation,
                    "warning": "",
                }
            ],
        )
        calls.append({"kind": "invoke", "day": day, "prompt": prompt, "kwargs": kwargs})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_pitfall_agent", fake_build)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = pitfall_agent_node(
        {"project_id": isolated_project, "topic": "青甘大环线", "mode": "plan"}
    )

    builds = [call for call in calls if call["kind"] == "build"]
    invokes = [call for call in calls if call["kind"] == "invoke"]
    assert [call["work_unit"] for call in builds] == ["pitfall_day_1", "pitfall_day_2"]
    assert [call["day"] for call in invokes] == [1, 2]
    assert "Day2特色餐" not in invokes[0]["prompt"]
    assert "Day1特色餐" not in invokes[1]["prompt"]
    assert invokes[0]["kwargs"]["thread_id"].endswith(":pitfall_draft:day1")
    assert invokes[1]["kwargs"]["thread_id"].endswith(":pitfall_draft:day2")
    assert result["pitfall_agent_done"] is True


def test_pitfall_agent_resume_skips_completed_day_checkpoint(isolated_project, monkeypatch):
    """恢复时保留已经落盘的日期，只运行仍缺失的日级工作单元。"""
    store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [
            {"field": "food", "day": 1, "recommendation": "Day1特色餐"},
            {"field": "food", "day": 2, "recommendation": "Day2特色餐"},
        ],
    )
    store.write_pitfall_output_patch(
        isolated_project,
        "pitfall_agent",
        [
            {
                "field": "food",
                "day": 1,
                "target_recommendation": "Day1特色餐",
                "warning": "",
            }
        ],
    )
    invoked_days = []

    class Snapshot:
        def __init__(self, complete):
            self.values = {"messages": ["done"]} if complete else {}
            self.next = ()

    class FakeAgent:
        checkpointer = object()

        def get_state(self, config):
            return Snapshot(config["configurable"]["thread_id"].endswith(":day1"))

    def fake_invoke(agent, messages, **kwargs):
        invoked_days.append(2)
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [
                {
                    "field": "food",
                    "day": 2,
                    "target_recommendation": "Day2特色餐",
                    "warning": "",
                }
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(
        orchestrator,
        "build_pitfall_agent",
        lambda project_id, topic, **kwargs: FakeAgent(),
    )
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = pitfall_agent_node(
        {"project_id": isolated_project, "topic": "青甘大环线", "mode": "plan"}
    )

    assert invoked_days == [2]
    assert result["pitfall_agent_done"] is True


def test_pitfall_agent_edit_mode_prompt_contains_edit_request(isolated_project, monkeypatch):
    _seed_lifestyle(isolated_project)
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _write_pitfall(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "entry_target": "pitfall_agent_node",
        "edit_request": "再帮我查查叶婆婆钵钵鸡有没有坑",
        "last_agent_result": None,
    }
    pitfall_agent_node(state)

    assert "再帮我查查叶婆婆钵钵鸡有没有坑" in captured_prompts[0]


# ---------------------------------------------------------------------------
# 误改检测（docs/ISSUES.md第37条已知局限的修复）：跟lifestyle_agent_node同款字段级diff
# ---------------------------------------------------------------------------


def _seed_pitfall_with_two_fields(project_id: str) -> None:
    """模拟真实E2E复现过的场景：food字段有一条避雷内容（胡二哥药膳蹄花），
    play字段也有一条（杜甫草堂）。"""
    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [
            {"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": "药膳味较重"},
            {"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "纪念馆非原址"},
        ],
    )


def test_pitfall_agent_edit_no_conflict_when_only_target_field_changed(isolated_project, monkeypatch):
    """对照组：edit请求只涉及play字段，agent也确实只改了play字段——不应该触发report_conflict。"""
    _seed_lifestyle(isolated_project)
    _seed_pitfall_with_two_fields(isolated_project)
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [{"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "更新后的杜甫草堂避雷内容"}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "entry_target": "pitfall_agent_node", "edit_request": "再帮我查查杜甫草堂有没有坑",
        "last_agent_result": None,
    }
    result = pitfall_agent_node(state)

    assert store.get_pending_conflict(isolated_project) is None
    # docs/ISSUES.md第37补条：供pitfall_citation_check_node收窄核验范围
    assert result["edit_changed_pitfall_fields"] == ["play"]
    # item级diff（比字段更细一档，第37补条已知局限的修复）：play.day1的warning变了，
    # 应该被算作("play", 1)一个改动项
    assert result["edit_changed_pitfall_items"] == [["play", 1]]


def test_pitfall_agent_edit_item_level_only_flags_changed_day(isolated_project, monkeypatch):
    """字段内有多天时，只有真正变化的那一天应该出现在item级diff结果里——
    同字段其它天不该被无谓地牵连进核验scoping（docs/ISSUES.md第37补条已知局限）。"""
    _seed_lifestyle(isolated_project)
    store.write_pitfall_output_patch(
        isolated_project,
        "pitfall_agent",
        [
            {"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": "药膳味较重"},
            {"field": "food", "day": 2, "target_recommendation": "某某火锅", "warning": "人均消费偏高"},
        ],
    )
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        # 只提交day1的patch，day2不提交，验证store层自动保留原样
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [{"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": "更新后的药膳蹄花避雷内容"}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "entry_target": "pitfall_agent_node", "edit_request": "再帮我查查胡二哥药膳蹄花有没有坑",
        "last_agent_result": None,
    }
    result = pitfall_agent_node(state)

    assert result["edit_changed_pitfall_fields"] == ["food"]
    # 只有day1变了，day2不应该出现在item级diff结果里
    assert result["edit_changed_pitfall_items"] == [["food", 1]]


def test_pitfall_agent_edit_reports_conflict_when_unrelated_field_also_changed(isolated_project, monkeypatch):
    """真实复现过的bug场景（docs/ISSUES.md第37条）：用户只要求补充"杜甫草堂"（play字段）
    的避雷内容，agent却顺带清空了"food"字段里另一条不相关的避雷内容。字段级diff检测
    应该抓到这个跨字段误改，自动report_conflict打回复核。"""
    _seed_lifestyle(isolated_project)
    _seed_pitfall_with_two_fields(isolated_project)
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        # 模拟真实bug：play字段按要求更新了，但food字段的胡二哥药膳蹄花被意外清空
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
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "entry_target": "pitfall_agent_node", "edit_request": "再帮我查查杜甫草堂有没有坑",
        "last_agent_result": None,
    }
    result = pitfall_agent_node(state)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.target.split(".")[0] == "pitfall_agent"
    assert "2" in conflict.reason or "['food'" in conflict.reason
    assert conflict.issue_type == "pitfall_edit_overreach"
    # 越权检测和核验scoping是两个独立机制——即使触发了越权冲突，这次真实改动的字段依然
    # 应该被如实上报供下游核验（docs/ISSUES.md第37补条）。
    assert set(result["edit_changed_pitfall_fields"]) == {"food", "play"}


def test_pitfall_agent_edit_baseline_persists_across_retries(isolated_project, monkeypatch):
    """基线固定性验证（跟lifestyle_agent_node同款）：第一轮已经把edit_original_pitfall
    写进结果后，第二轮（模拟被打回重试）如果继续用state里的原始基线而不是"上一轮"的
    （已经错误的）快照对比，仍然应该能发现"相对最初版本，多个字段被改了"这件事。"""
    _seed_lifestyle(isolated_project)
    _seed_pitfall_with_two_fields(isolated_project)

    def fake_invoke_round1(agent, messages, **kwargs):
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [
                {"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": ""},
                {"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "第1轮改的内容"},
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke_round1)

    state = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "entry_target": "pitfall_agent_node", "edit_request": "再帮我查查杜甫草堂有没有坑",
        "last_agent_result": None,
    }
    result1 = pitfall_agent_node(state)
    assert "edit_original_pitfall" in result1
    original = result1["edit_original_pitfall"]
    assert original["food"]["items"][0]["warning"] == "药膳味较重"

    # 第2轮：状态里带着第1轮固定下来的基线，模拟模型在第1轮结果基础上"什么都不改"
    # （重新提交同样的错误内容、没有真正修复越权改动），diff检测理应仍然发现相对最初版本
    # changed了多个字段；同时满足新Harness的"本轮必须真实调用写入工具"要求。
    def fake_invoke_round2(agent, messages, **kwargs):
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [
                {"field": "food", "day": 1, "target_recommendation": "胡二哥药膳蹄花", "warning": ""},
                {"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "第1轮改的内容"},
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke_round2)
    state2 = {**state, "edit_original_pitfall": original}
    pitfall_agent_node(state2)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None  # 第2轮依然能识别出相对最初基线的越权改动


# ---------------------------------------------------------------------------
# 场景13回归测试：lifestyle_agent被避雷冲突打回重新生成时，pitfall_agent_done要重置
# ---------------------------------------------------------------------------


def test_lifestyle_agent_resets_pitfall_done_when_pushed_back_by_conflict(isolated_project, monkeypatch):
    """docs/ORCHESTRATION.md场景13：lifestyle的推荐内容变了，避雷agent必须针对新内容
    重新检索一遍——lifestyle_agent_node被target=lifestyle_agent的冲突打回重新生成时，
    返回值里pitfall_agent_done必须是False，否则避雷内容会跟最新推荐对不上。"""
    _seed_lifestyle(isolated_project)
    store.report_conflict(
        isolated_project, actor="integration_agent", target="lifestyle_agent.food",
        reason="避雷来源数明显超过原建议来源数，需要重新考虑该推荐",
    )
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": field, "day": 1, "recommendation": "换一家新推荐"}
                for field in LIFESTYLE_FIELDS
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "plan",
        "requirements": {"destination": "成都", "days": 3},
    }
    result = lifestyle_agent_node(state)

    assert result["pitfall_agent_done"] is False
    assert result["pitfall_citation_checked"] is False


def test_lifestyle_agent_does_not_touch_pitfall_done_when_no_conflict(isolated_project, monkeypatch):
    """对照组：正常首次生成（没有pending conflict）时，不应该无缘无故返回
    pitfall_agent_done这个key——避免掩盖"这次到底有没有触发重置逻辑"的信号。"""
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(
        orchestrator, "invoke_with_retry",
        lambda agent, messages, **kwargs: (
            store.write_agent_output_patch(
                isolated_project,
                "lifestyle_agent",
                [
                    {"field": field, "day": 1, "recommendation": "正常推荐"}
                    for field in LIFESTYLE_FIELDS
                ],
            ),
            fake_agent_reply(messages),
        )[1],
    )

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "plan",
        "requirements": {"destination": "成都", "days": 3},
    }
    result = lifestyle_agent_node(state)

    assert "pitfall_agent_done" not in result


# ---------------------------------------------------------------------------
# pitfall_citation_check_node
# ---------------------------------------------------------------------------


def test_pitfall_citation_check_passes_immediately(isolated_project, monkeypatch):
    monkeypatch.setattr(citation_check, "verify_pitfall_citations", lambda project_id, topic, fields=None, item_keys=None: [])

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = pitfall_citation_check_node(state)

    assert result["force_finalize"] is False
    assert result["pitfall_citation_checked"] is True


def test_pitfall_citation_check_reports_and_resolves_conflict_after_fix(isolated_project, monkeypatch):
    _write_pitfall(isolated_project, food_items=[{"field": "food", "day": 1, "target_recommendation": "x", "warning": "有卫生问题"}])
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", lambda agent, messages, **kwargs: None)

    calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"field": "food", "day": 1, "warning": "有卫生问题", "missing_entities": ["卫生问题"], "reason": "原文未提到"}]
        return []

    monkeypatch.setattr(citation_check, "verify_pitfall_citations", fake_verify)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = pitfall_citation_check_node(state)

    assert result["force_finalize"] is False
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "resolved"
    assert conflicts[-1]["target"] == "pitfall_agent.food"


def test_pitfall_citation_fix_prompt_includes_findings_hint(isolated_project, monkeypatch):
    """Pitfall核验打回时应把早期工作笔记带回修复prompt，避免长上下文后忘记已找到的证据。"""
    monkeypatch.setattr(orchestrator, "PROJECTS_DIR", store.PROJECTS_DIR)
    findings_dir = store.PROJECTS_DIR / isolated_project / "agents" / "pitfall_agent"
    findings_dir.mkdir(parents=True)
    (findings_dir / "findings.jsonl").write_text(
        json.dumps({"fact": "叶婆婆钵钵鸡排队反馈=成都美食-021-c003"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    captured_messages = []

    def fake_invoke(agent, messages, **kwargs):
        captured_messages.append(messages)
        return None

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{
                "field": "food",
                "day": 1,
                "warning": "高峰期排队较久",
                "missing_entities": ["叶婆婆钵钵鸡"],
                "reason": "当前引用未支持排队反馈",
            }]
        return []

    monkeypatch.setattr(citation_check, "verify_pitfall_citations", fake_verify)

    pitfall_citation_check_node({"project_id": isolated_project, "topic": "成都", "mode": "plan"})

    assert len(captured_messages) == 1
    fix_prompt = captured_messages[0][0]["content"]
    assert "必要时使用检索工具补证" in fix_prompt
    assert "叶婆婆钵钵鸡排队反馈" in fix_prompt
    assert "成都美食-021-c003" in fix_prompt
    assert "先查自己记过的笔记" in fix_prompt


def test_pitfall_citation_check_force_finalizes_after_max_retry(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", lambda agent, messages, **kwargs: None)
    monkeypatch.setattr(
        citation_check, "verify_pitfall_citations",
        lambda project_id, topic, fields=None, item_keys=None: [{"field": "food", "day": 1, "warning": "有卫生问题", "missing_entities": ["卫生问题"], "reason": "原文未提到"}],
    )
    store.write_pitfall_output_patch(
        isolated_project, "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "尕面片", "warning": "有卫生问题"}],
    )

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = pitfall_citation_check_node(state)

    assert result["force_finalize"] is True
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "force_finalized"
    # docs/ISSUES.md第125条：耗尽MAX_RETRY后，issues里涉及的(field, day)应该被标记
    # citation_status=force_finalized。
    pitfall = store.read_pitfall_output(isolated_project)
    assert pitfall.food.items[0].citation_status == "force_finalized"


def test_pitfall_citation_check_scopes_verification_to_edit_changed_fields(isolated_project, monkeypatch):
    """跟citation_check_node同款scoping（docs/ISSUES.md第37补条修复）：edit模式下只核验
    这次编辑实际改动的字段，不应该把edit_changed_pitfall_fields之外的字段拉出来核验。"""
    captured = {}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        captured["fields"] = fields
        captured["item_keys"] = item_keys
        return []

    monkeypatch.setattr(citation_check, "verify_pitfall_citations", fake_verify)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "edit_changed_pitfall_fields": ["play"],
    }
    pitfall_citation_check_node(state)

    assert captured["fields"] == ["play"]
    # item_keys未设置时（这个测试没有edit_changed_pitfall_items），应保持None，
    # 不做item级过滤，只受fields级scoping控制——向后兼容旧state（没有item级diff字段的场景）。
    assert captured["item_keys"] is None


def test_pitfall_citation_check_scopes_verification_to_edit_changed_items(isolated_project, monkeypatch):
    """item级scoping（docs/ISSUES.md第37补条已知局限的修复）：edit_changed_pitfall_items
    设置时应该被转成set传给verify_pitfall_citations的item_keys参数。"""
    captured = {}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        captured["item_keys"] = item_keys
        return []

    monkeypatch.setattr(citation_check, "verify_pitfall_citations", fake_verify)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "edit_changed_pitfall_fields": ["food"],
        "edit_changed_pitfall_items": [["food", 1]],
    }
    pitfall_citation_check_node(state)

    assert captured["item_keys"] == {("food", 1)}


def test_pitfall_citation_check_falls_back_to_full_verification_without_changed_fields(isolated_project, monkeypatch):
    """edit模式下edit_changed_pitfall_fields未设置时（比如避雷agent这次是被lifestyle_agent
    变化级联触发的整体重新生成），应该回退到全量核验，不能误判成"这次什么都没改"。"""
    captured = {}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        captured["fields"] = fields
        captured["item_keys"] = item_keys
        return []

    monkeypatch.setattr(citation_check, "verify_pitfall_citations", fake_verify)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "edit"}
    pitfall_citation_check_node(state)

    assert captured["fields"] is None
    assert captured["item_keys"] is None


def test_pitfall_citation_check_plan_mode_always_full_verification(isolated_project, monkeypatch):
    """plan模式下即使state里意外带着edit_changed_pitfall_fields/edit_changed_pitfall_items，
    也不应该被拿去做核验范围收窄——scoping只在edit模式下生效。"""
    captured = {}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        captured["fields"] = fields
        captured["item_keys"] = item_keys
        return []

    monkeypatch.setattr(citation_check, "verify_pitfall_citations", fake_verify)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "plan",
        "edit_changed_pitfall_fields": ["play"],
        "edit_changed_pitfall_items": [["play", 1]],
    }
    pitfall_citation_check_node(state)

    assert captured["fields"] is None
    assert captured["item_keys"] is None



# ---------------------------------------------------------------------------
# _compute_pitfall_source_counts
# ---------------------------------------------------------------------------


def test_compute_pitfall_source_counts_pairs_by_field_and_day():
    lifestyle = {
        "food": {"items": [{"day": 1, "sources": [{"chunk_id": "c1"}]}]},
        "outfit": {"items": []}, "lodging": {"items": []}, "play": {"items": []},
    }
    pitfall = {
        "food": {"items": [{"day": 1, "warning": "有问题", "sources": [{"chunk_id": "p1"}, {"chunk_id": "p2"}, {"chunk_id": "p3"}]}]},
        "outfit": {"items": []}, "lodging": {"items": []}, "play": {"items": []},
    }
    result = _compute_pitfall_source_counts(lifestyle, pitfall)
    assert result == {"food.day1": [3, 1]}


def test_compute_pitfall_source_counts_skips_empty_warning():
    pitfall = {
        "food": {"items": [{"day": 1, "warning": "", "sources": [{"chunk_id": "p1"}]}]},
        "outfit": {"items": []}, "lodging": {"items": []}, "play": {"items": []},
    }
    assert _compute_pitfall_source_counts(None, pitfall) == {}


# ---------------------------------------------------------------------------
# integration_agent_node：读取并使用pitfall内容
# ---------------------------------------------------------------------------


def test_integration_prompt_includes_pitfall_content_and_source_count_signal(isolated_project, monkeypatch):
    _seed_lifestyle(isolated_project, sources=[SourceRef(platform="bilibili", doc_id="d1", chunk_id="d1-c001")])
    _write_pitfall(
        isolated_project,
        food_items=[
            {
                "field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡",
                "warning": "有多条卫生问题投诉",
                "sources": [
                    {"platform": "bilibili", "doc_id": "d2", "chunk_id": "d2-c001"},
                    {"platform": "bilibili", "doc_id": "d3", "chunk_id": "d3-c001"},
                ],
            }
        ],
    )
    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        store.write_final_guide(isolated_project, actor="integration_agent", content_md="#攻略\n" + "内容" * 100, meta={})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": {"destination": "成都", "days": 3}, "mode": "plan"}
    integration_agent_node(state)

    prompt = captured_prompts[0]
    assert "有多条卫生问题投诉" in prompt
    assert '"food.day1": [2, 1]' in prompt


# ---------------------------------------------------------------------------
# 路由：_route_plan_mode / _route_edit_mode
# ---------------------------------------------------------------------------


def test_route_plan_mode_goes_to_pitfall_agent_after_lifestyle_done(isolated_project):
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.PARALLEL_RESEARCH)
    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "plan",
        "material_checked": True,
        "route_citation_checked": True,
        "route_map_checked": True,
        "route_timeliness_checked": True,
        "lifestyle_agent_done": True,
        "pitfall_agent_done": False,
    }
    assert _route_plan_mode(state) == "pitfall_agent_node"


def test_route_plan_mode_goes_to_pitfall_citation_check_after_pitfall_done(isolated_project):
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.PARALLEL_RESEARCH)
    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "plan",
        "material_checked": True,
        "route_citation_checked": True,
        "route_map_checked": True,
        "route_timeliness_checked": True,
        "lifestyle_agent_done": True,
        "pitfall_agent_done": True,
        "pitfall_citation_checked": False,
    }
    assert _route_plan_mode(state) == "pitfall_citation_check_node"


def test_route_plan_mode_goes_to_citation_check_after_pitfall_citation_checked(isolated_project):
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.PARALLEL_RESEARCH)
    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "plan",
        "material_checked": True,
        "route_citation_checked": True,
        "route_map_checked": True,
        "route_timeliness_checked": True,
        "lifestyle_agent_done": True,
        "pitfall_agent_done": True,
        "pitfall_citation_checked": True,
    }
    assert _route_plan_mode(state) == "citation_check_node"


def test_route_edit_mode_entry_target_pitfall_agent(isolated_project):
    """文档场景12：用户完成后单独@避雷agent。"""
    state = {"project_id": isolated_project, "entry_target": "pitfall_agent_node", "last_agent_result": None}
    assert _route_edit_mode(state) == "pitfall_agent_node"


def test_route_edit_mode_pitfall_agent_goes_to_its_own_citation_check(isolated_project):
    state = {
        "project_id": isolated_project,
        "last_agent_result": {"agent": "pitfall_agent"},
        "pitfall_citation_checked": False,
    }
    assert _route_edit_mode(state) == "pitfall_citation_check_node"


def test_route_edit_mode_lifestyle_citation_checker_cascades_to_pitfall(isolated_project):
    """lifestyle自己的citation_check_node刚跑完（pitfall_citation_checked还没True），
    应该识别出这是lifestyle那次核验、级联到pitfall_agent_node，而不是误判成pitfall
    自己的核验已经跑完直接冲去整合。"""
    state = {
        "project_id": isolated_project,
        "last_agent_result": {"agent": "citation_checker"},
        "edit_citation_checked": True,
        "pitfall_citation_checked": False,
        "pitfall_agent_done": False,
    }
    assert _route_edit_mode(state) == "pitfall_agent_node"


def test_route_edit_mode_pitfall_citation_checker_cascades_to_integration(isolated_project):
    """pitfall自己的citation_check_node刚跑完（pitfall_citation_checked=True），
    应该正确识别并级联到整合阶段，而不是被误判成lifestyle那次、绕回pitfall_agent_node死循环。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="#原始攻略\n内容", meta={})
    state = {
        "project_id": isolated_project,
        "last_agent_result": {"agent": "citation_checker"},
        "edit_citation_checked": True,
        "pitfall_citation_checked": True,
        "pitfall_agent_done": True,
        "integration_done": False,
    }
    assert _route_edit_mode(state) == "integration_agent_node"


def test_route_integration_phase_for_pitfall_agent_target(isolated_project):
    from src.orchestrator import _route_integration_phase

    store.report_conflict(isolated_project, actor="citation_checker", target="pitfall_agent.food", reason="避雷内容缺乏依据")
    state = {"project_id": isolated_project, "integration_retry_count": 0}
    assert _route_integration_phase(state) == "pitfall_agent_node"
