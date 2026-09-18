"""orchestrator.py核心节点的直接mock单元测试——补齐CLAUDE.md已知问题第6条提到的缺口：
之前只给`_route_integration_phase`（纯路由函数）和edit模式的`lifestyle_agent_node`/
`integration_agent_node`补过测试，`route_draft_node`/`route_citation_check_node`/
`citation_check_node`/`material_agent_node`/`collect_requirements_node`这几个plan模式
主链路节点此前从来没有被直接invoke测试过，只能靠跑真实E2E验证（贵、慢）。

跟tests/test_orchestrator_poi_details.py同款风格：monkeypatch build_route_agent/
build_lifestyle_agent/invoke_with_retry/citation_check.verify_*_citations，不调真实LLM。
"""
import hashlib
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from src import orchestrator
from src.materials import citation_check
from src.orchestrator import (
    MAX_RETRY,
    _has_failed_write_draft,
    _findings_retry_hint,
    _missing_entities_retry_hint,
    citation_check_node,
    collect_requirements_node,
    integration_agent_node,
    lifestyle_agent_node,
    material_agent_node,
    route_citation_check_node,
    route_draft_node,
    route_timeliness_check_node,
)
from src.workspace import store
from src.workspace.route_validation import route_structure_hash
from src.workspace.schema import (
    EvidenceClaimEvidenceSnapshot,
    ProjectPhase,
    RouteClaim,
    RouteDay,
    RouteFeasibilityCheck,
    RouteFeasibilityReport,
    RoutePlan,
    RoutePoi,
    SourceRef,
)

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(orchestrator, "PROJECTS_DIR", tmp_path)
    project_id = "core-node-test"
    store.init_project(project_id)
    return project_id


def _base_requirements():
    return {"destination": "成都", "days": 3, "style": "休闲", "budget": "中等", "notes": "无"}


def test_has_failed_write_draft_requires_matching_tool_call():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "write_final_guide", "args": {"blocks": "[...]"}, "id": "call-1"}],
        ),
        ToolMessage(
            content="工具参数校验失败：blocks: Input should be a valid list",
            tool_call_id="call-1",
        ),
    ]

    assert _has_failed_write_draft(messages, "write_final_guide") is True
    assert _has_failed_write_draft(messages, "write_route_facts_patch") is False


# ---------------------------------------------------------------------------
# material_agent_node
# ---------------------------------------------------------------------------


def test_material_agent_node_reports_new_entries_when_ingest_succeeds(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "ingest_topic", lambda topic: [{"doc_id": "a"}, {"doc_id": "b"}])

    result = material_agent_node({"project_id": isolated_project, "topic": "成都"})

    assert result["material_checked"] is True
    assert "新增2条" in result["last_agent_result"]["summary"]


def test_material_agent_node_handles_missing_raw_gracefully(isolated_project, monkeypatch):
    """v1 MVP阶段目的地完全没有素材时不应该抛异常中断流程，如实告知即可。"""

    def raise_not_found(topic):
        raise FileNotFoundError()

    monkeypatch.setattr(orchestrator, "ingest_topic", raise_not_found)

    result = material_agent_node({"project_id": isolated_project, "topic": "丽江"})

    assert result["material_checked"] is True
    assert "暂无素材库内容" in result["last_agent_result"]["summary"]


# ---------------------------------------------------------------------------
# collect_requirements_node
# ---------------------------------------------------------------------------


def test_collect_requirements_node_advances_progress_to_skeleton_confirm(isolated_project):
    result = collect_requirements_node({"project_id": isolated_project})

    assert store.read_progress(isolated_project).state.value == "骨架确认中"
    assert result["last_agent_result"]["agent"] == "main_agent"


# ---------------------------------------------------------------------------
# route_draft_node
# ---------------------------------------------------------------------------


def _seed_route_plan_written(project_id: str) -> None:
    store.write_route_plan(
        project_id,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(day=1, city="成都", pois=[RoutePoi(name="宽窄巷子")]),
                RouteDay(day=2, city="成都", pois=[RoutePoi(name="熊猫基地")]),
                RouteDay(day=3, city="成都", pois=[RoutePoi(name="杜甫草堂")]),
            ]
        ),
    )


def test_route_draft_node_first_time_generation(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": _base_requirements(),
    }
    result = route_draft_node(state)

    assert result["route_plan_drafted"] is True
    assert result["revision_request"] is None
    assert "awaiting_integration_recheck" not in result
    assert "destination" not in captured_prompts[0]  # 用的是具体值，不是字面量键名
    assert "成都" in captured_prompts[0]
    assert "3天" in captured_prompts[0]


def test_route_draft_node_first_time_generation_includes_must_visit_and_style_interpretation(
    isolated_project, monkeypatch
):
    """docs/ARCHITECTURE.md第18.6节：route_draft_node默认分支的prompt必须把must_visit/
    style_interpretation拼进去，route_agent才能据此遵守强约束/调整排点逻辑。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    requirements = _base_requirements()
    requirements["must_visit"] = [{"name": "熊猫基地", "day": 2}, {"name": "都江堰", "day": None}]
    requirements["style_interpretation"] = {
        "poi_density": "偏少",
        "stay_duration": "偏多",
        "needs_detail_bias": None,
        "flexible_time": None,
    }
    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": requirements,
    }
    route_draft_node(state)

    prompt = captured_prompts[0]
    assert "熊猫基地（第2天）" in prompt
    assert "都江堰（未指定具体哪天）" in prompt
    assert "每天POI数量密度=偏少" in prompt
    assert "单点停留时长倾向=偏多" in prompt
    # null的维度不应该出现在prompt里
    assert "needs_detail判断倾向" not in prompt
    assert "预留弹性时间倾向" not in prompt


def test_route_draft_node_first_time_generation_without_must_visit_shows_placeholder(
    isolated_project, monkeypatch
):
    """没有must_visit/style_interpretation时（CLI等既有调用方常见情况），prompt里
    应该展示"无"这类占位而不是报错/留空字符串，保持向后兼容。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": _base_requirements(),
    }
    route_draft_node(state)

    prompt = captured_prompts[0]
    assert "必去地点=无" in prompt
    assert "风格解读=无" in prompt


def test_route_draft_node_uses_revision_prompt_when_revision_requested(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": _base_requirements(),
        "revision_request": "把第二天改成都江堰",
    }
    route_draft_node(state)

    assert "用户要求调整骨架：把第二天改成都江堰" in captured_prompts[0]


def test_route_draft_node_conflict_driven_regeneration_sets_recheck_flag(isolated_project, monkeypatch):
    """被integration_agent的report_conflict打回（target=route_agent）重新生成时，
    prompt要带上冲突原因，且必须设置awaiting_integration_recheck，供_route_integration_phase
    判断"子agent刚修复完，必须先回整合复核"。同时必须重置lifestyle_agent_done/
    pitfall_agent_done/pitfall_citation_checked（docs/ORCHESTRATION.md场景13此前遗漏的
    一个应用场景，见docs/ISSUES.md第32条已知局限②的后续排查修复）——路线骨架重新生成后，
    衣食住行agent的既有推荐可能已经跟新路线对不上，不能让lifestyle_agent_done停留在True。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    store.report_conflict(isolated_project, actor="integration_agent", target="route_agent", reason="Day2路线距离过远")

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": _base_requirements(),
        "integration_retry_count": 0,
    }
    result = route_draft_node(state)

    assert "Day2路线距离过远" in captured_prompts[0]
    assert result["awaiting_integration_recheck"] is True
    assert result["integration_retry_count"] == 1
    assert result["lifestyle_agent_done"] is False
    assert result["pitfall_agent_done"] is False
    assert result["pitfall_citation_checked"] is False


def test_route_draft_node_does_not_touch_lifestyle_done_when_no_conflict(isolated_project, monkeypatch):
    """对照组：正常首次生成/revision场景（没有pending conflict）不应该无缘无故返回
    lifestyle_agent_done这个key——避免掩盖"这次到底有没有触发重置逻辑"的信号，
    跟lifestyle_agent_node同款对照测试（test_lifestyle_agent_does_not_touch_pitfall_done_when_no_conflict）
    是同一个思路。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": _base_requirements(),
    }
    result = route_draft_node(state)

    assert "lifestyle_agent_done" not in result
    assert "pitfall_agent_done" not in result


def test_route_draft_node_retries_when_write_verification_fails_then_succeeds(isolated_project, monkeypatch):
    """agent第一次只输出文字总结没有真正写入时，_invoke_with_write_verification应该
    追加一次重试prompt，重试后写入成功就不再报错。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements()}
    result = route_draft_node(state)

    assert calls["n"] == 2
    assert result["route_plan_drafted"] is True


def test_route_draft_node_resumes_pending_inner_checkpoint(isolated_project, monkeypatch):
    class PendingAgent:
        checkpointer = object()

        def get_state(self, config):
            return SimpleNamespace(values={"messages": ["existing"]}, next=("agent",))

    captured = {}
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda project_id, topic, **kwargs: PendingAgent(),
    )
    monkeypatch.setattr(
        orchestrator,
        "reset_web_search_log",
        lambda *args, **kwargs: pytest.fail("pending checkpoint不应清空Web日志"),
    )
    monkeypatch.setattr(
        orchestrator,
        "reset_amap_evidence_log",
        lambda *args, **kwargs: pytest.fail("pending checkpoint不应清空高德日志"),
    )
    monkeypatch.setattr(
        orchestrator,
        "clear_agent_thread",
        lambda *args, **kwargs: pytest.fail("pending checkpoint不应被清空"),
    )

    def fake_invoke(agent, prompt, **kwargs):
        captured.update(kwargs)
        _seed_route_plan_written(isolated_project)

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    result = route_draft_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "requirements": _base_requirements(),
        }
    )

    assert result["route_plan_drafted"] is True
    assert captured["resume_from_checkpoint"] is True
    assert captured["thread_id"].endswith(":agent:route_draft")


def test_lifestyle_agent_node_resumes_pending_inner_checkpoint(isolated_project, monkeypatch):
    class PendingAgent:
        checkpointer = object()

        def get_state(self, config):
            return SimpleNamespace(values={"messages": ["existing"]}, next=("agent",))

    captured = {}
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent",
        lambda project_id, topic, **kwargs: PendingAgent(),
    )
    monkeypatch.setattr(
        orchestrator,
        "reset_web_search_log",
        lambda *args, **kwargs: pytest.fail("pending checkpoint不应清空Web日志"),
    )
    monkeypatch.setattr(
        orchestrator,
        "clear_agent_thread",
        lambda *args, **kwargs: pytest.fail("pending checkpoint不应被清空"),
    )

    def fake_invoke(agent, prompt, **kwargs):
        captured.update(kwargs)
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": field, "day": 1, "recommendation": field}
                for field in ("food", "outfit", "lodging", "play")
            ],
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都")]),
    )

    result = lifestyle_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "requirements": _base_requirements(),
        }
    )

    assert result["lifestyle_agent_done"] is True
    assert captured["resume_from_checkpoint"] is True
    assert captured["thread_id"].endswith(":agent:lifestyle_draft")


def test_lifestyle_plan_runs_one_isolated_work_unit_per_day(isolated_project, monkeypatch):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")]),
                RouteDay(day=2, city="成都", pois=[RoutePoi(name="杜甫草堂")]),
                RouteDay(day=3, city="成都", pois=[RoutePoi(name="东郊记忆")]),
            ]
        ),
    )
    prompts = []
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent",
        lambda project_id, topic, **kwargs: object(),
    )

    def fake_invoke(agent, messages, **kwargs):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        day = next(day for day in (1, 2, 3) if f"只处理Day{day}" in prompt)
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": field, "day": day, "recommendation": f"Day{day}-{field}"}
                for field in ("food", "outfit", "lodging", "play")
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = lifestyle_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "mode": "plan",
            "turn_id": "turn1",
            "requirements": _base_requirements(),
        }
    )

    assert result["lifestyle_agent_done"] is True
    assert len(prompts) == 3
    assert all("当天路线地图事实" in prompt for prompt in prompts)
    assert "武侯祠" in prompts[0] and "杜甫草堂" not in prompts[0]
    assert "杜甫草堂" in prompts[1] and "东郊记忆" not in prompts[1]
    assert "东郊记忆" in prompts[2] and "武侯祠" not in prompts[2]


# ---------------------------------------------------------------------------
# route_draft_node: must_visit确定性校验+自动打回（docs/ARCHITECTURE.md第18.7节）
# ---------------------------------------------------------------------------


def _seed_route_plan_with_days(project_id: str, days_pois: dict) -> None:
    """days_pois: {day_num: [poi_name, ...]}"""
    store.write_route_plan(
        project_id,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(day=day, city="成都", pois=[RoutePoi(name=name) for name in names])
                for day, names in days_pois.items()
            ]
        ),
    )


def test_route_draft_node_no_retry_when_must_visit_all_present(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地"], 3: ["杜甫草堂"]})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    requirements = _base_requirements()
    requirements["must_visit"] = [{"name": "熊猫基地", "day": 2}]
    state = {"project_id": isolated_project, "topic": "成都", "requirements": requirements}
    result = route_draft_node(state)

    # 只有首次生成这一次调用，没有触发任何必去地点打回重试
    assert calls["n"] == 1
    assert result["force_finalize"] is False
    assert store.get_pending_conflict(isolated_project) is None


def test_route_draft_node_retries_and_resolves_when_must_visit_missing_then_fixed(
    isolated_project, monkeypatch
):
    """完全遗漏场景：第一次生成漏了都江堰，应该自动report_conflict打回重新生成；
    第二次生成补上后，conflict应该被resolve，不留pending记录。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地"], 3: ["杜甫草堂"]})
        else:
            _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地", "都江堰"], 3: ["杜甫草堂"]})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    requirements = _base_requirements()
    requirements["must_visit"] = [{"name": "都江堰", "day": 2}]
    state = {"project_id": isolated_project, "topic": "成都", "requirements": requirements}
    result = route_draft_node(state)

    assert calls["n"] == 2
    assert result["force_finalize"] is False
    assert store.get_pending_conflict(isolated_project) is None


def test_route_draft_node_day_mismatch_counts_as_missing(isolated_project, monkeypatch):
    """day错位视同遗漏：都江堰确实出现在行程里，但排到了Day1而不是用户指定的Day2，
    应该被识别成缺失并触发打回。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _seed_route_plan_with_days(isolated_project, {1: ["都江堰"], 2: ["熊猫基地"], 3: ["杜甫草堂"]})
        else:
            _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地", "都江堰"], 3: ["杜甫草堂"]})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    requirements = _base_requirements()
    requirements["must_visit"] = [{"name": "都江堰", "day": 2}]
    state = {"project_id": isolated_project, "topic": "成都", "requirements": requirements}
    result = route_draft_node(state)

    assert calls["n"] == 2
    assert result["force_finalize"] is False


def test_route_draft_node_blocks_confirmation_after_max_retry_still_missing(isolated_project, monkeypatch):
    """确认前硬约束耗尽重试后仍失败时应停在骨架确认，而不是force finalize放行。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        # 每次都不写入都江堰，模拟agent持续遗漏
        _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地"], 3: ["杜甫草堂"]})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    requirements = _base_requirements()
    requirements["must_visit"] = [{"name": "都江堰", "day": 2}]
    state = {"project_id": isolated_project, "topic": "成都", "requirements": requirements}
    result = route_draft_node(state)

    # 首次生成(invoke_with_retry未被计入，_invoke_with_write_verification内部走的是
    # invoke_with_retry mock，即calls["n"]==1) + MAX_RETRY轮修正重试
    assert calls["n"] == 1 + MAX_RETRY
    assert result["force_finalize"] is False
    assert result["route_hard_constraints_blocked"] is True
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "pending"


def test_route_draft_node_rechecks_must_visit_after_revision(isolated_project, monkeypatch):
    """骨架确认阶段的revision仍要重新检查原有硬约束，不能顺手删掉必去地点。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地"], 3: ["杜甫草堂"]})
        else:
            _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地", "都江堰"], 3: ["杜甫草堂"]})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    requirements = _base_requirements()
    requirements["must_visit"] = [{"name": "都江堰", "day": 2}]
    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": requirements,
        "revision_request": "把第一天改成宽窄巷子",
    }
    result = route_draft_node(state)

    assert calls["n"] == 2
    assert result["force_finalize"] is False
    assert store.get_pending_conflict(isolated_project) is None


def test_route_draft_node_must_visit_check_skipped_for_direct_edit(isolated_project, monkeypatch):
    """edit模式定点修改场景（entry_target=route_draft）同样不触发must_visit自动打回。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子"], 2: ["熊猫基地"]})
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        _seed_route_plan_with_days(isolated_project, {1: ["宽窄巷子改"], 2: ["熊猫基地"]})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    requirements = _base_requirements()
    requirements["must_visit"] = [{"name": "都江堰", "day": 2}]
    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": requirements,
        "mode": "edit",
        "edit_request": "把第一天改成宽窄巷子改",
        "entry_target": "route_draft",
    }
    result = route_draft_node(state)

    assert calls["n"] == 1
    assert result["force_finalize"] is False


def test_missing_must_visit_items_day_specified_and_unspecified():
    route_plan = {
        "days": [
            {"day": 1, "pois": [{"name": "宽窄巷子"}]},
            {"day": 2, "pois": [{"name": "熊猫基地"}]},
        ]
    }
    # 指定day=2的都江堰不在day2，视同遗漏；未指定day的春熙路不在任何一天，也是遗漏；
    # 未指定day的熊猫基地在day2里，不算遗漏
    must_visit = [
        {"name": "都江堰", "day": 2},
        {"name": "春熙路", "day": None},
        {"name": "熊猫基地", "day": None},
    ]
    missing = orchestrator._missing_must_visit_items(must_visit, route_plan)
    assert [m["name"] for m in missing] == ["都江堰", "春熙路"]


def test_invoke_with_write_verification_retry_carries_original_context(isolated_project, monkeypatch):
    """回归测试（2026-07-21，真实E2E chengdu-3d-e2e-v20首次触发）：_invoke_with_write_verification
    的重试调用必须携带累积的完整对话历史，不能只发一条孤立的retry_note——build_route_agent等
    agent构建函数都没有绑定checkpointer，create_react_agent编译出的图在没有checkpointer时
    两次独立invoke()调用之间完全无状态共享，孤立的retry_note会让模型看不到原始任务
    （目的地/天数/需求），等于让它凭空编内容。这里直接断言重试时传给invoke_with_retry的
    messages列表里包含第一条消息（原始prompt），而不是只断言"最终成功了"这种间接验证。
    见docs/ISSUES.md。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    captured_message_lists = []

    def fake_invoke(agent, messages, **kwargs):
        captured_message_lists.append(messages)
        if len(captured_message_lists) >= 2:
            _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements()}
    route_draft_node(state)

    assert len(captured_message_lists) == 2
    first_call_messages = captured_message_lists[0]
    retry_call_messages = captured_message_lists[1]
    assert len(first_call_messages) == 1  # 第一次调用只有原始prompt这一条
    assert len(retry_call_messages) > 1  # 重试调用必须比第一次多（累积了历史）
    # 关键断言：重试调用里必须能看到第一次调用的原始prompt内容（目的地信息），
    # 而不是只有retry_note这一条孤立消息
    original_prompt_content = first_call_messages[0]["content"]
    retry_contents = [m.get("content", m) if isinstance(m, dict) else getattr(m, "content", "") for m in retry_call_messages]
    assert original_prompt_content in retry_contents


def test_invoke_with_write_verification_retries_with_latest_write_error(monkeypatch):
    completed = {"value": False}
    captured_message_lists = []

    def fake_invoke(agent, messages, **kwargs):
        captured_message_lists.append(list(messages))
        if len(captured_message_lists) == 1:
            return {
                "messages": list(messages)
                + [
                    {"role": "assistant", "content": "", "tool_calls": [{"name": "write_final_guide"}]},
                    {"role": "tool", "content": "写入失败，请检查参数格式后重试：blocks[0].section不能为空"},
                ]
            }
        completed["value"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        "agent",
        "生成完整攻略",
        verify_fn=lambda: completed["value"],
        retry_note="请调用write_final_guide",
        max_retries=1,
    )

    retry_note = captured_message_lists[1][-1]["content"]
    assert "blocks[0].section不能为空" in retry_note
    assert "只修正这个校验错误" in retry_note
    assert "不要从头重写" in retry_note


def test_invoke_with_write_verification_exhaustion_reports_latest_write_error(monkeypatch):
    write_errors = [
        "工具参数校验失败：blocks[0].section不能为空",
        "工具参数校验失败：blocks[1].markdown不能为空",
        "工具参数校验失败：meta.merged_fields缺少lifestyle.play",
    ]
    calls = {"count": 0}

    def fake_invoke(agent, messages, **kwargs):
        call_index = calls["count"]
        calls["count"] += 1
        call_id = f"write-{call_index}"
        return {
            "messages": list(messages)
            + [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"name": "write_final_guide", "args": {}, "id": call_id}
                    ],
                },
                {
                    "role": "tool",
                    "content": write_errors[call_index],
                    "tool_call_id": call_id,
                },
            ]
        }

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    with pytest.raises(RuntimeError) as exc_info:
        orchestrator._invoke_with_write_verification(
            "agent",
            "生成完整攻略",
            verify_fn=lambda: False,
            retry_note="请调用write_final_guide",
            max_retries=2,
        )

    assert calls["count"] == 3
    assert write_errors[-1] in str(exc_info.value)
    assert write_errors[-2] not in str(exc_info.value)


def test_invoke_with_write_verification_uses_forceful_suffix_when_agent_says_but_doesnt_call(
    isolated_project, monkeypatch
):
    """docs/ISSUES.md第80条：2026-07-29真实复现过lifestyle_agent反复说"现在调用/立即写入"，
    但连续几轮回复的tool_calls始终为空这种子模式。_invoke_with_write_verification应该识别出
    这种"说了却没做"的情况，追加_FORCEFUL_RETRY_SUFFIX强化措辞，而不是原样重发通用retry_note。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    captured_message_lists = []

    def fake_invoke(agent, messages, **kwargs):
        captured_message_lists.append(list(messages))
        if len(captured_message_lists) == 1:
            # 第一次调用：模拟agent只用文字说"现在调用"，没有真正发起tool_calls
            return {"messages": list(messages) + [{"role": "assistant", "content": "好的，我现在调用write_route_plan写入。"}]}
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements()}
    route_draft_node(state)

    assert len(captured_message_lists) == 2
    retry_messages = captured_message_lists[1]
    last_retry_content = retry_messages[-1]["content"]
    assert orchestrator._FORCEFUL_RETRY_SUFFIX in last_retry_content


def test_said_would_call_tool_but_didnt_detects_intent_without_tool_calls():
    """_said_would_call_tool_but_didnt的核心判断逻辑：最后一条消息没有tool_calls，
    但文字内容里出现"现在调用/立即写入"这类意图表述时应该返回True；有真正tool_calls时
    即使文字也提到"现在调用"也应该返回False（已经真的调用了，不算"说了没做"）。"""

    class _FakeMsg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    assert orchestrator._said_would_call_tool_but_didnt(
        [_FakeMsg("好的，我立即调用write_agent_output写入完整产出。")]
    ) is True
    assert orchestrator._said_would_call_tool_but_didnt(
        [_FakeMsg("素材库检索没有找到峨眉山相关的素材，以下是方向性建议。")]
    ) is False
    assert orchestrator._said_would_call_tool_but_didnt(
        [_FakeMsg("现在调用write_agent_output。", tool_calls=[{"name": "write_agent_output"}])]
    ) is False
    assert orchestrator._said_would_call_tool_but_didnt([]) is False


def test_count_trailing_empty_retrievals_counts_consecutive_tool_messages():
    """docs/ISSUES.md第80条：_count_trailing_empty_retrievals要跳过AIMessage/HumanMessage
    （真实react-agent消息序列是AIMessage(tool_call)→ToolMessage交替出现的），只用ToolMessage
    本身的内容判断连续性；遇到非空结果（或非检索类工具结果）要停止计数，不能被中间的AI消息打断。"""

    class _FakeAI:
        def __init__(self):
            self.content = ""
            self.tool_calls = [{"name": "retrieve_materials", "args": {}, "id": "x"}]

    class _FakeTool:
        def __init__(self, content):
            self.content = content

    # 3次连续空检索，中间穿插AIMessage，应该数出3
    messages = [
        _FakeAI(), _FakeTool("没有检索到相关素材。"),
        _FakeAI(), _FakeTool("未检索到相关内容。"),
        _FakeAI(), _FakeTool("没有检索到相关素材。"),
    ]
    assert orchestrator._count_trailing_empty_retrievals(messages) == 3

    # 末尾插入一次真实检索结果后又是空结果——只应该数到最后这次空结果之前遇到非空结果为止
    messages_with_hit = messages + [_FakeAI(), _FakeTool('[{"content_snippet": "真实内容"}]'), _FakeAI(), _FakeTool("没有检索到相关素材。")]
    assert orchestrator._count_trailing_empty_retrievals(messages_with_hit) == 1

    # 空列表/全是非ToolMessage应该返回0
    assert orchestrator._count_trailing_empty_retrievals([]) == 0
    assert orchestrator._count_trailing_empty_retrievals([_FakeAI()]) == 0


def test_invoke_with_write_verification_switches_to_fallback_agent_after_repeated_empty_retrievals(
    isolated_project, monkeypatch
):
    """docs/ISSUES.md第80条：prompt规则和few-shot都无法可靠解决"素材库空白+反复检索无果+
    忘记调用写入工具"这个问题，新增结构性兜底——检测到连续空检索达到阈值后，最后一次重试
    改用fallback_agent_factory()构建的受限agent。这里验证：达到阈值时确实调用了
    fallback_agent_factory，且传给它的agent对象被用于最后一次invoke_with_retry。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: "primary_agent")
    fallback_calls = {"n": 0}

    def fake_fallback_factory():
        fallback_calls["n"] += 1
        return "fallback_agent"

    class _FakeAI:
        def __init__(self):
            self.content = ""
            self.tool_calls = [{"name": "retrieve_materials", "args": {}, "id": "x"}]

    class _FakeTool:
        def __init__(self, content):
            self.content = content

    invoked_agents = []

    def fake_invoke(agent, messages, **kwargs):
        invoked_agents.append(agent)
        if len(invoked_agents) <= 2:
            # 前两次调用（首次+第1次重试）都模拟"连续检索3次全部落空，末尾只有文字总结"
            return {
                "messages": list(messages)
                + [
                    _FakeAI(), _FakeTool("没有检索到相关素材。"),
                    _FakeAI(), _FakeTool("未检索到相关内容。"),
                    _FakeAI(), _FakeTool("没有检索到相关素材。"),
                ]
            }
        # 第3次调用（也就是fallback_agent_factory生效后的最后一次重试）才真正写入
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    def verify_fn():
        return bool((store.read_workspace(isolated_project, "route_plan.json") or {}).get("days"))

    orchestrator._invoke_with_write_verification(
        "primary_agent",
        "prompt",
        verify_fn=verify_fn,
        retry_note="请调用工具",
        fallback_agent_factory=fake_fallback_factory,
    )

    assert fallback_calls["n"] == 1  # fallback只在最后一次重试被构建了一次
    assert invoked_agents[-1] == "fallback_agent"  # 最后一次invoke_with_retry用的确实是fallback agent
    assert invoked_agents[0] == "primary_agent"  # 前面的调用仍然用原agent


def test_invoke_with_write_verification_accepts_write_before_recursion_error(monkeypatch):
    """Agent已成功写入时，返回前的GraphRecursionError不能覆盖成功事实。"""
    class _CheckpointAgent:
        checkpointer = object()

    completed = {"value": False}

    def fake_invoke(agent, messages, **kwargs):
        completed["value"] = True
        raise orchestrator.GraphRecursionError("boundary reached after write")

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        _CheckpointAgent(),
        "Day2 task",
        verify_fn=lambda: completed["value"],
        retry_note="write Day2 now",
        thread_id="project:agent:lifestyle:day2",
        resume_from_checkpoint=True,
    )


def test_invoke_with_write_verification_accepts_write_before_context_budget_error(monkeypatch):
    """A successful side effect must win over the next-turn context stop."""
    completed = {"value": False}

    def fake_invoke(agent, messages, **kwargs):
        completed["value"] = True
        raise orchestrator.ContextBudgetExceededError("latest write draft is large")

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        "agent",
        "Pitfall task",
        verify_fn=lambda: completed["value"],
        retry_note="write Pitfall now",
    )


def test_invoke_with_write_verification_repairs_failed_checkpoint_write_after_context_stop(
    monkeypatch,
):
    """A hard context stop may hand its preserved write draft to the existing child retry."""
    failed_args = {"days": [{"day": 1, "city": "成都"}]}
    failed_error = "写入失败，请检查参数格式后重试：pois.0.brief缺少claim"

    class _CheckpointAgent:
        checkpointer = object()

        def get_state(self, config):
            assert config["configurable"]["thread_id"] == "project:agent:route_draft"
            return SimpleNamespace(
                values={
                    "messages": [
                        {"role": "tool", "content": "旧检索结果", "tool_call_id": "research-1"},
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_route_plan",
                                    "args": failed_args,
                                    "id": "write-1",
                                }
                            ],
                        ),
                        ToolMessage(content=failed_error, tool_call_id="write-1"),
                        ToolMessage(content="后续无关工具结果", tool_call_id="research-2"),
                    ]
                },
                next=("pre_model_hook",),
            )

    agent = _CheckpointAgent()
    completed = {"value": False}
    calls = []
    original_prompt = "完整路线底稿" * 5000

    def fake_invoke(active_agent, messages, **kwargs):
        calls.append((list(messages), dict(kwargs)))
        if len(calls) == 1:
            raise orchestrator.ContextBudgetExceededError("hard budget")
        if len(calls) == 2:
            return {
                "messages": list(messages)
                + [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_route_plan",
                                "args": failed_args,
                                "id": "write-2",
                            }
                        ],
                    ),
                    ToolMessage(content="路线已保存", tool_call_id="write-2"),
                ]
            }
        completed["value"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        agent,
        original_prompt,
        verify_fn=lambda: completed["value"],
        retry_note="只修复写入错误",
        max_retries=2,
        thread_id="project:agent:route_draft",
        resume_from_checkpoint=True,
        checkpoint_repair_write_tools=("write_route_plan", "write_route_plan_patch"),
    )

    repair_messages, repair_kwargs = calls[1]
    assert len(repair_messages) == 4
    assert repair_messages[0]["content"] != original_prompt
    assert len(repair_messages[0]["content"]) < 200
    assert "只修正错误指出的参数" in repair_messages[0]["content"]
    assert repair_messages[1]["tool_calls"][0]["args"] == failed_args
    assert repair_messages[2]["content"] == failed_error
    assert "只修正这个校验错误" in repair_messages[3]["content"]
    assert all("旧检索结果" not in str(message) for message in repair_messages)
    assert repair_kwargs["thread_id"].startswith(
        "project:agent:route_draft:write-verification:1:"
    )
    second_retry_messages, second_retry_kwargs = calls[2]
    assert second_retry_messages[-1]["content"] == "只修复写入错误"
    assert failed_error not in second_retry_messages[-1]["content"]
    assert second_retry_kwargs["thread_id"].startswith(
        "project:agent:route_draft:write-verification:2:"
    )


def test_invoke_with_write_verification_keeps_context_stop_without_failed_write_draft(
    monkeypatch,
):
    class _CheckpointAgent:
        checkpointer = object()

        def get_state(self, config):
            return SimpleNamespace(
                values={"messages": [{"role": "assistant", "content": "仍在研究"}]},
                next=("pre_model_hook",),
            )

    def fake_invoke(agent, messages, **kwargs):
        raise orchestrator.ContextBudgetExceededError("hard budget")

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    with pytest.raises(orchestrator.ContextBudgetExceededError, match="hard budget"):
        orchestrator._invoke_with_write_verification(
            _CheckpointAgent(),
            "继续研究",
            verify_fn=lambda: False,
            retry_note="完成写入",
            thread_id="project:agent:route_draft",
            resume_from_checkpoint=True,
        )


def test_invoke_with_write_verification_does_not_revive_failure_after_later_write_success(
    monkeypatch,
):
    class _CheckpointAgent:
        checkpointer = object()

        def get_state(self, config):
            return SimpleNamespace(
                values={
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_route_plan",
                                    "args": {"days": [{"day": 1}]},
                                    "id": "write-failed",
                                }
                            ],
                        ),
                        ToolMessage(
                            content="工具参数校验失败：缺少pois",
                            tool_call_id="write-failed",
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_route_plan",
                                    "args": {"days": [{"day": 1, "pois": []}]},
                                    "id": "write-success",
                                }
                            ],
                        ),
                        ToolMessage(content="路线已保存", tool_call_id="write-success"),
                    ]
                },
                next=("pre_model_hook",),
            )

    calls = []

    def fake_invoke(agent, messages, **kwargs):
        calls.append(kwargs)
        raise orchestrator.ContextBudgetExceededError("hard budget")

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    with pytest.raises(orchestrator.ContextBudgetExceededError, match="hard budget"):
        orchestrator._invoke_with_write_verification(
            _CheckpointAgent(),
            "继续路线任务",
            verify_fn=lambda: False,
            retry_note="完成写入",
            thread_id="project:agent:route_draft",
            resume_from_checkpoint=True,
            checkpoint_repair_write_tools=("write_route_plan",),
        )

    assert len(calls) == 1


def test_invoke_with_write_verification_uses_child_thread_for_checkpointed_retry(
    monkeypatch,
):
    """写入提醒不能遗漏thread_id，也不能复用研究主thread追加重复历史。"""
    class _CheckpointAgent:
        checkpointer = object()

    completed = {"value": False}
    calls = []

    def fake_invoke(agent, messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            completed["value"] = True
        return {"messages": list(messages) + [{"role": "assistant", "content": ""}]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        _CheckpointAgent(),
        "POI task",
        verify_fn=lambda: completed["value"],
        retry_note="write POI now",
        max_retries=1,
        thread_id="project:agent:poi:day5",
    )

    assert calls[0]["thread_id"] == "project:agent:poi:day5"
    assert calls[1]["thread_id"].startswith(
        "project:agent:poi:day5:write-verification:1:"
    )
    assert calls[1]["thread_id"] != calls[0]["thread_id"]


def test_invoke_with_write_verification_uses_final_fallback_after_nonempty_research(monkeypatch):
    """Research can succeed while the model still omits the required write call."""
    completed = {"value": False}
    invoked_agents = []

    def fake_invoke(agent, messages, **kwargs):
        invoked_agents.append(agent)
        if agent == "write_only_agent":
            completed["value"] = True
        return {
            "messages": list(messages)
            + [{"role": "assistant", "content": "已有足够证据，下面给出总结。"}]
        }

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        "primary_agent",
        "Day4 task",
        verify_fn=lambda: completed["value"],
        retry_note="write Day4 now",
        max_retries=1,
        fallback_agent_factory=lambda: "write_only_agent",
    )

    assert invoked_agents == ["primary_agent", "write_only_agent"]


def test_invoke_with_write_verification_accepts_write_before_retry_fallback_recursion_error(
    monkeypatch,
):
    """普通调用返回但未写入，外层最后一次切受限Agent后也要按副作用验收。"""
    completed = {"value": False}
    calls = {"count": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["count"] += 1
        if agent == "write_only_agent":
            completed["value"] = True
            raise orchestrator.GraphRecursionError("boundary reached after write")
        return {
            "messages": list(messages) + [
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "没有检索到相关素材。"},
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "未检索到相关内容。"},
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "没有检索到相关素材。"},
            ]
        }

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        "primary_agent",
        "Day3 task",
        verify_fn=lambda: completed["value"],
        retry_note="write Day3 now",
        max_retries=1,
        fallback_agent_factory=lambda: "write_only_agent",
    )

    assert calls["count"] == 2


def test_write_only_fallback_accepts_write_before_context_budget_error(monkeypatch):
    completed = {"value": False}

    def fake_invoke(agent, messages, **kwargs):
        if agent == "write_only_agent":
            completed["value"] = True
            raise orchestrator.ContextBudgetExceededError("post-write request is too large")
        return {"messages": list(messages) + [{"role": "assistant", "content": "summary"}]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    orchestrator._invoke_with_write_verification(
        "primary_agent",
        "Pitfall task",
        verify_fn=lambda: completed["value"],
        retry_note="write Pitfall now",
        max_retries=1,
        fallback_agent_factory=lambda: "write_only_agent",
    )


# ---------------------------------------------------------------------------
# _compress_retrieval_history / _summarize_retrieval_result（token优化，2026-08-06）
# ---------------------------------------------------------------------------


class _FakeAIWithToolCall:
    def __init__(self, tool_calls):
        self.content = ""
        self.tool_calls = tool_calls


class _FakeToolMessage:
    """跟真实ToolMessage一样带tool_call_id，用于关联回发起调用的tool_calls['id']。"""

    def __init__(self, content, tool_call_id):
        self.content = content
        self.tool_call_id = tool_call_id


def test_summarize_retrieval_result_compresses_chunk_full_text():
    raw = json.dumps(
        [
            {
                "chunk_id": "成都-001-c003",
                "platform": "bilibili",
                "author": "up主A",
                "content_snippet": "武侯祠是三国蜀汉丞相诸葛亮的祠堂" * 10,
            }
        ],
        ensure_ascii=False,
    )
    summarized = orchestrator._summarize_retrieval_result(raw)
    assert "成都-001-c003" in summarized
    assert "武侯祠是三国蜀汉丞相诸葛亮的祠堂" * 10 not in summarized  # 全文被压缩
    assert len(summarized) < len(raw)


def test_summarize_retrieval_result_leaves_non_json_content_untouched():
    # "没有检索到相关素材。"这类短句不是JSON，必须原样保留——
    # _count_trailing_empty_retrievals靠关键词匹配这类内容判断连续空检索。
    assert orchestrator._summarize_retrieval_result("没有检索到相关素材。") == "没有检索到相关素材。"


def test_compress_retrieval_history_only_touches_retrieve_materials_tool_messages():
    messages = [
        {"role": "user", "content": "原始任务"},
        _FakeAIWithToolCall([{"name": "retrieve_materials", "id": "call-1"}]),
        _FakeToolMessage(
            json.dumps([{"chunk_id": "c001", "platform": "bilibili", "author": "A", "content_snippet": "全文" * 50}]),
            tool_call_id="call-1",
        ),
        _FakeAIWithToolCall([{"name": "write_route_plan", "id": "call-2"}]),
        _FakeToolMessage("写入失败：days字段缺失day3", tool_call_id="call-2"),
    ]

    compressed = orchestrator._compress_retrieval_history(messages)

    assert compressed[0] is messages[0]  # 原始任务prompt完全不受影响
    assert compressed[1] is messages[1]  # AIMessage的tool_calls意图不受影响
    assert "c001" in compressed[2].content
    assert "全文" * 50 not in compressed[2].content  # retrieve_materials结果被压缩
    assert compressed[3] is messages[3]
    # write_route_plan的报错信息必须原样保留，这是重试要修的直接依据
    assert compressed[4].content == "写入失败：days字段缺失day3"


def test_compress_retrieval_history_also_compresses_project_evidence_workspace_and_web():
    """2026-08-11扩展：_COMPRESSIBLE_RETRIEVAL_TOOLS从只有retrieve_materials扩展到
    也覆盖read_workspace/web_search（跟src/context_editing.py的pre_model_hook共用同一套
    按工具类型分发的摘要函数）。"""
    long_route_plan = json.dumps({"days": [{"day": i, "city": "成都"} for i in range(30)]}, ensure_ascii=False)
    long_web_search = (
        "- 武侯祠简介：" + "历史悠久的三国遗迹" * 20 + "（来源：http://x.com/a）"
    )
    long_project_evidence = json.dumps(
        [
            {
                "chunk_id": "project-evidence-1",
                "title": "武侯祠官方说明",
                "source_platform": "official",
                "source_url": "https://example.com/wuhou",
                "content_snippet": "项目证据原文" * 200,
            }
        ],
        ensure_ascii=False,
    )
    messages = [
        {"role": "user", "content": "原始任务"},
        _FakeAIWithToolCall([{"name": "read_workspace", "id": "call-1"}]),
        _FakeToolMessage(long_route_plan, tool_call_id="call-1"),
        _FakeAIWithToolCall([{"name": "web_search", "id": "call-2"}]),
        _FakeToolMessage(long_web_search, tool_call_id="call-2"),
        _FakeAIWithToolCall([{"name": "retrieve_project_evidence", "id": "call-3"}]),
        _FakeToolMessage(long_project_evidence, tool_call_id="call-3"),
    ]

    compressed = orchestrator._compress_retrieval_history(messages)

    assert len(compressed[2].content) < len(long_route_plan)
    assert "不要重复读取同一整份文件" in compressed[2].content
    assert len(compressed[4].content) < len(long_web_search)
    assert "http://x.com/a" in compressed[4].content  # 来源URL保留
    assert len(compressed[6].content) < len(long_project_evidence)
    assert "project-evidence-1" in compressed[6].content
    assert "https://example.com/wuhou" in compressed[6].content


def test_invoke_with_write_verification_compresses_retrieval_history_across_retries(
    isolated_project, monkeypatch
):
    """端到端验证：重试累加的历史里，第1轮retrieve_materials返回的chunk全文，
    传给第2次invoke_with_retry时已经被压缩，不再包含原始长文本。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    long_snippet = "这是一段很长的素材原文" * 100
    captured_message_lists = []

    def fake_invoke(agent, messages, **kwargs):
        captured_message_lists.append(messages)
        if len(captured_message_lists) == 1:
            return {
                "messages": list(messages)
                + [
                    _FakeAIWithToolCall([{"name": "retrieve_materials", "id": "call-1"}]),
                    _FakeToolMessage(
                        json.dumps(
                            [{"chunk_id": "c001", "platform": "bilibili", "author": "A", "content_snippet": long_snippet}]
                        ),
                        tool_call_id="call-1",
                    ),
                ]
            }
        _seed_route_plan_written(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements()}
    route_draft_node(state)

    assert len(captured_message_lists) == 2
    retry_messages = captured_message_lists[1]
    retry_contents = [m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "") for m in retry_messages]
    assert not any(long_snippet in c for c in retry_contents)
    assert any("c001" in c for c in retry_contents)  # chunk_id这条记忆本身要保留


# ---------------------------------------------------------------------------
# route_citation_check_node
# ---------------------------------------------------------------------------


def test_route_citation_check_node_passes_immediately_when_no_issues(isolated_project, monkeypatch):
    monkeypatch.setattr(citation_check, "verify_route_citations", lambda project_id, topic, days=None: [])

    state = {"project_id": isolated_project, "topic": "成都"}
    result = route_citation_check_node(state)

    assert result["route_citation_checked"] is True
    assert result["force_finalize"] is False
    assert store.read_workspace(isolated_project, "conflicts.jsonl") is None


def test_route_citation_check_node_honors_post_timeliness_days_in_plan_mode(
    isolated_project, monkeypatch
):
    observed_days = []

    def fake_verify(project_id, topic, days=None):
        observed_days.append(days)
        return []

    monkeypatch.setattr(citation_check, "verify_route_citations", fake_verify)

    result = route_citation_check_node(
        {
            "project_id": isolated_project,
            "topic": "峨眉山",
            "mode": "plan",
            "route_recheck_days": [1],
            "route_citation_after_timeliness": True,
        }
    )

    assert result["route_citation_checked"] is True
    assert result["route_recheck_days"] is None
    assert result["route_citation_after_timeliness"] is False
    assert observed_days == [[1]]


def test_route_citation_check_node_reports_and_resolves_conflict_after_fix(isolated_project, monkeypatch):
    """第一轮发现问题打回修复，第二轮核验通过——冲突应该被report后又resolve，
    不应该走到强制终结。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda agent, prompt, verify_fn=None, retry_note=None, **kwargs: None,
    )

    calls = {"n": 0}

    def fake_verify(project_id, topic, days=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"day": 1, "city": "成都", "area": "", "missing_entities": ["东郊记忆"], "reason": "原文未提到"}]
        return []

    monkeypatch.setattr(citation_check, "verify_route_citations", fake_verify)

    state = {"project_id": isolated_project, "topic": "成都"}
    result = route_citation_check_node(state)

    assert result["force_finalize"] is False
    assert calls["n"] == 2
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert len(conflicts) == 1
    assert conflicts[0]["status"] == "resolved"


def test_route_citation_check_repairs_each_day_with_current_day_snapshot(
    isolated_project, monkeypatch
):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    notes="Day1当前说明",
                    claims=[
                        RouteClaim(
                            claim_id="program-generated-id",
                            claim_text="武侯祠位于成都。",
                            evidence_snapshots=[
                                EvidenceClaimEvidenceSnapshot(
                                    source=SourceRef(platform="web", source_url="https://example.com"),
                                    excerpt="不应进入返修Prompt的长证据摘录" * 100,
                                )
                            ],
                        )
                    ],
                ),
                RouteDay(day=2, city="都江堰", notes="Day2当前说明"),
            ]
        ),
    )
    build_kwargs = []

    def fake_build(*args, **kwargs):
        build_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(orchestrator, "build_route_agent", fake_build)
    prompts = []
    verification_calls = []

    def fake_verified_invoke(agent, prompt, verify_fn=None, retry_note=None, **kwargs):
        prompts.append(prompt)
        assert verify_fn is not None
        assert verify_fn() is False
        day = 1 if "本工作单元只处理Day1" in prompt else 2
        plan = store.read_route_plan(isolated_project)
        next_day = next(item for item in plan.days if item.day == day)
        next_day.notes = f"Day{day}已完成事实修复"
        store.write_route_plan(isolated_project, "route_agent", plan)
        assert verify_fn() is True
        verification_calls.append((day, retry_note))

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_verified_invoke)
    calls = {"n": 0}

    def fake_verify(project_id, topic, days=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                {
                    "day": 2,
                    "city": "都江堰",
                    "area": "",
                    "missing_entities": ["青城山"],
                    "reason": "Day2证据不支持",
                },
                {
                    "day": 1,
                    "city": "成都",
                    "area": "",
                    "missing_entities": ["武侯祠"],
                    "reason": "Day1证据不支持",
                },
            ]
        return []

    monkeypatch.setattr(citation_check, "verify_route_citations", fake_verify)

    result = route_citation_check_node(
        {"project_id": isolated_project, "topic": "成都"}
    )

    assert result["force_finalize"] is False
    assert len(prompts) == 2
    assert "本工作单元只处理Day1" in prompts[0]
    assert "Day1当前说明" in prompts[0]
    assert "Day2当前说明" not in prompts[0]
    assert "武侯祠位于成都" in prompts[0]
    assert "claim_id" not in prompts[0]
    assert "evidence_snapshots" not in prompts[0]
    assert "不应进入返修Prompt的长证据摘录" not in prompts[0]
    assert "本工作单元只处理Day2" in prompts[1]
    assert "Day2当前说明" in prompts[1]
    assert "Day1当前说明" not in prompts[1]
    assert all("调用一次write_route_facts_patch" in prompt for prompt in prompts)
    assert all("返修不等于搜索" in prompt for prompt in prompts)
    assert all("证据对象或片段绑定错误" in prompt for prompt in prompts)
    assert all("不需要联网证明" in prompt for prompt in prompts)
    assert all("不要借返修增加问题清单之外" in prompt for prompt in prompts)
    assert all("处理完本日全部问题后统一写入一次" in prompt for prompt in prompts)
    assert [day for day, _ in verification_calls] == [1, 2]
    assert all("文字说明或准备继续处理都不算完成" in note for _, note in verification_calls)
    assert len(build_kwargs) == 2
    assert all(kwargs["checkpointer"] is False for kwargs in build_kwargs)
    assert [kwargs["convergence_work_unit"] for kwargs in build_kwargs] == [
        "route_facts_day_1",
        "route_facts_day_2",
    ]


def test_route_citation_check_accepts_written_facts_when_final_reply_exceeds_budget(
    isolated_project, monkeypatch
):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都", notes="修复前")]),
    )
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda *args, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        plan = store.read_route_plan(isolated_project)
        plan.days[0].notes = "工具已成功写入的修复"
        store.write_route_plan(isolated_project, "route_agent", plan)
        raise orchestrator.ContextBudgetExceededError("最终回复超出安全预算")

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    calls = {"n": 0}

    def fake_verify(project_id, topic, days=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                {
                    "day": 1,
                    "city": "成都",
                    "area": "",
                    "missing_entities": ["武侯祠"],
                    "reason": "引用不支持",
                }
            ]
        return []

    monkeypatch.setattr(citation_check, "verify_route_citations", fake_verify)

    result = route_citation_check_node(
        {"project_id": isolated_project, "topic": "成都"}
    )

    assert result["force_finalize"] is False
    assert calls["n"] == 2
    assert store.read_route_plan(isolated_project).days[0].notes == "工具已成功写入的修复"


def test_route_citation_check_node_force_finalizes_after_max_retry(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda agent, prompt, verify_fn=None, retry_note=None, **kwargs: None,
    )
    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda project_id, topic, days=None: [{"day": 1, "city": "成都", "area": "", "missing_entities": ["东郊记忆"], "reason": "原文未提到"}],
    )
    store.write_route_plan(
        isolated_project, "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都"), RouteDay(day=2, city="都江堰")]),
    )

    state = {"project_id": isolated_project, "topic": "成都"}
    result = route_citation_check_node(state)

    assert result["force_finalize"] is True
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "force_finalized"
    # docs/ISSUES.md第125条：耗尽MAX_RETRY后，issues里涉及的具体天应该被标记
    # citation_status=force_finalized，其它没出问题的天不受影响。
    plan = store.read_route_plan(isolated_project)
    by_day = {d.day: d for d in plan.days}
    assert by_day[1].citation_status == "force_finalized"
    assert by_day[2].citation_status == "passed"


def test_route_timeliness_check_node_force_finalizes_after_max_retry(isolated_project, monkeypatch):
    """可执行性warning多次修复仍未消除时应显式降级，不污染引用状态。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(
        orchestrator, "_invoke_with_write_verification",
        lambda agent, prompt, verify_fn=None, retry_note=None, **kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda project_id, topic, requirements=None, days=None, check_ids=None: RouteFeasibilityReport(
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="road-1",
                    day=1,
                    target="G227",
                    target_type="road",
                    category="road_operational_status",
                    status="warning",
                    summary="notes未提及封闭施工",
                )
            ],
        ),
    )
    store.write_route_plan(
        isolated_project, "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="西宁"), RouteDay(day=2, city="张掖")]),
    )

    state = {"project_id": isolated_project, "topic": "青甘大环线"}
    result = route_timeliness_check_node(state)

    assert result["force_finalize"] is True
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "force_finalized"
    plan = store.read_route_plan(isolated_project)
    by_day = {d.day: d for d in plan.days}
    assert by_day[1].citation_status == "passed"
    assert by_day[2].citation_status == "passed"
    assert store.read_route_feasibility(isolated_project).force_finalized is True


def test_route_feasibility_warning_repairs_facts_then_passes(isolated_project, monkeypatch):
    reports = iter(
        [
            RouteFeasibilityReport(
                overall_status="warning",
                checks=[
                    RouteFeasibilityCheck(
                        check_id="poi-reservation",
                        day=1,
                        target="莫高窟",
                        target_type="poi",
                        category="reservation_or_quota",
                        status="warning",
                        summary="需预约，notes未提醒",
                    )
                ],
            ),
            RouteFeasibilityReport(overall_status="passed"),
        ]
    )
    verify_calls = []

    def fake_verify(*args, **kwargs):
        verify_calls.append(kwargs)
        return next(reports)

    monkeypatch.setattr(orchestrator.route_feasibility_check, "verify_route_feasibility", fake_verify)
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda *args, **kwargs: object())
    repair_calls = []
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda agent, prompt, **kwargs: repair_calls.append(prompt),
    )

    result = route_timeliness_check_node(
        {"project_id": isolated_project, "topic": "敦煌", "requirements": {"travel_start_date": "2026-10-02"}}
    )

    assert result["force_finalize"] is False
    assert len(repair_calls) == 1
    assert "预约" in repair_calls[0]
    assert "check_ids" not in verify_calls[0]
    assert verify_calls[1]["check_ids"] == ["poi-reservation"]


def test_route_feasibility_fact_repair_schedules_scoped_citation_recheck(
    isolated_project, monkeypatch
):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="敦煌", notes="原路线备注")]),
    )
    reports = iter(
        [
            RouteFeasibilityReport(
                overall_status="warning",
                checks=[
                    RouteFeasibilityCheck(
                        check_id="poi-reservation",
                        day=1,
                        target="莫高窟",
                        target_type="poi",
                        category="reservation_or_quota",
                        status="warning",
                        summary="需预约，notes未提醒",
                    )
                ],
            ),
            RouteFeasibilityReport(overall_status="passed"),
        ]
    )
    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda *args, **kwargs: next(reports),
    )
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda *args, **kwargs: object())

    def write_repaired_facts(agent, prompt, verify_fn=None, **kwargs):
        store.write_route_facts_patch(
            isolated_project,
            "route_agent",
            1,
            notes="莫高窟需提前预约。",
        )
        assert verify_fn is not None and verify_fn()

    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        write_repaired_facts,
    )

    result = route_timeliness_check_node(
        {
            "project_id": isolated_project,
            "topic": "敦煌",
            "route_citation_checked": True,
        }
    )

    assert result["route_timeliness_checked"] is True
    assert result["route_citation_checked"] is False
    assert result["route_recheck_days"] == [1]
    assert result["route_citation_after_timeliness"] is True


def test_route_feasibility_technical_unknown_retries_without_route_agent(
    isolated_project, monkeypatch
):
    verify_calls = []

    def fake_verify(*args, **kwargs):
        verify_calls.append(kwargs)
        return RouteFeasibilityReport(
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-unknown",
                    day=1,
                    target="莫高窟",
                    target_type="poi",
                    category="poi_operational_status",
                    status="unknown",
                    summary="web_search失败",
                    technical_error="timeout",
                    technical_error_class="provider_timeout",
                )
            ],
        )

    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        fake_verify,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("技术性unknown不应打回Route Agent")),
    )

    result = route_timeliness_check_node({"project_id": isolated_project, "topic": "敦煌"})

    assert result["force_finalize"] is True
    assert len(verify_calls) == orchestrator.MAX_RETRY + 1
    assert store.read_route_feasibility(isolated_project).force_finalized is True


def test_route_feasibility_unknown_with_error_class_only_is_retryable(
    isolated_project, monkeypatch
):
    verify_calls = []

    def fake_verify(*args, **kwargs):
        verify_calls.append(kwargs)
        return RouteFeasibilityReport(
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-missing-category",
                    day=1,
                    target="洱海生态廊道",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="unknown",
                    summary="结构化结果漏掉预约分类",
                    technical_error_class="missing_category",
                )
            ],
        )

    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        fake_verify,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("技术性unknown不应打回Route Agent")
        ),
    )

    result = route_timeliness_check_node(
        {"project_id": isolated_project, "topic": "大理"}
    )

    assert result["force_finalize"] is True
    assert len(verify_calls) == orchestrator.MAX_RETRY + 1


def test_route_feasibility_incomplete_stream_is_retryable(
    isolated_project, monkeypatch
):
    verify_calls = []

    def fake_verify(*args, **kwargs):
        verify_calls.append(kwargs)
        return RouteFeasibilityReport(
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-incomplete-stream",
                    day=1,
                    target="洱海生态廊道",
                    target_type="poi",
                    category="poi_operational_status",
                    status="unknown",
                    summary="Provider流在完整证据前结束",
                    stream_completed=False,
                )
            ],
        )

    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        fake_verify,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("技术性unknown不应打回Route Agent")
        ),
    )

    result = route_timeliness_check_node(
        {"project_id": isolated_project, "topic": "大理"}
    )

    assert result["force_finalize"] is True
    assert len(verify_calls) == orchestrator.MAX_RETRY + 1


def test_route_feasibility_business_unknown_is_visible_without_retry_or_degradation(
    isolated_project, monkeypatch
):
    verify_calls = []
    report = RouteFeasibilityReport(
        overall_status="unknown",
        checks=[
            RouteFeasibilityCheck(
                check_id="poi-reservation-unknown",
                day=1,
                target="洱海生态廊道",
                target_type="poi",
                category="reservation_or_quota",
                status="unknown",
                summary="预约规则暂查不到",
                suggested_action="出发前通过官方渠道复核预约规则",
            )
        ],
    )

    def fake_verify(*args, **kwargs):
        verify_calls.append(kwargs)
        store.write_route_feasibility(isolated_project, "citation_checker", report)
        return report

    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        fake_verify,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("业务unknown不应打回Route Agent")
        ),
    )

    result = route_timeliness_check_node(
        {"project_id": isolated_project, "topic": "大理"}
    )

    assert len(verify_calls) == 1
    assert result["force_finalize"] is False
    assert "出发前复核提醒" in result["last_agent_result"]["summary"]
    saved = store.read_route_feasibility(isolated_project)
    assert saved.force_finalized is False
    assert saved.checks[0].suggested_action == "出发前通过官方渠道复核预约规则"
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert not [item for item in conflicts if item.get("status") == "force_finalized"]


def test_route_feasibility_mixed_unknown_retries_only_technical_checks(
    isolated_project, monkeypatch
):
    verify_calls = []
    business = RouteFeasibilityCheck(
        check_id="reservation-business-unknown",
        day=1,
        target="洱海生态廊道",
        target_type="poi",
        category="reservation_or_quota",
        status="unknown",
        summary="预约规则暂查不到",
        suggested_action="出发前通过官方渠道复核",
    )
    technical = RouteFeasibilityCheck(
        check_id="status-technical-unknown",
        day=1,
        target="洱海生态廊道",
        target_type="poi",
        category="poi_operational_status",
        status="unknown",
        summary="查询中断",
        technical_error="stream interrupted",
        technical_error_class="stream_interrupted",
    )
    repaired = RouteFeasibilityCheck(
        check_id="status-technical-unknown",
        day=1,
        target="洱海生态廊道",
        target_type="poi",
        category="poi_operational_status",
        status="passed",
        summary="当前开放",
    )
    reports = iter(
        [
            RouteFeasibilityReport(overall_status="unknown", checks=[business, technical]),
            RouteFeasibilityReport(overall_status="unknown", checks=[business, repaired]),
        ]
    )

    def fake_verify(*args, **kwargs):
        verify_calls.append(kwargs)
        report = next(reports)
        store.write_route_feasibility(isolated_project, "citation_checker", report)
        return report

    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        fake_verify,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unknown重试不应打回Route Agent")
        ),
    )

    result = route_timeliness_check_node(
        {"project_id": isolated_project, "topic": "大理"}
    )

    assert result["force_finalize"] is False
    assert verify_calls[1]["check_ids"] == ["status-technical-unknown"]
    saved = store.read_route_feasibility(isolated_project)
    assert saved.force_finalized is False
    assert next(check for check in saved.checks if check.check_id == business.check_id).status == "unknown"


def test_route_citation_check_node_preserves_sticky_force_finalize_flag(isolated_project, monkeypatch):
    """粘性标志验证：如果state里force_finalize已经是True（比如理论上被更早的步骤设置过），
    即使这一轮核验直接通过，也不能被无脑覆盖回False。"""
    monkeypatch.setattr(citation_check, "verify_route_citations", lambda project_id, topic, days=None: [])

    state = {"project_id": isolated_project, "topic": "成都", "force_finalize": True}
    result = route_citation_check_node(state)

    assert result["force_finalize"] is True


def test_route_citation_check_node_resolves_stale_conflict_from_earlier_invocation(isolated_project, monkeypatch):
    """docs/ISSUES.md第138条：如果这个project在更早的一次node调用里已经耗尽MAX_RETRY
    被force_finalize过（比如route_agent因为下游冲突被重新触发重写），这次全新调用
    （全量核验，非edit模式收窄）如果核验通过，应该能找到并resolve掉那条过时的
    force_finalized记录，而不是留一条永久对不上号的历史记录。"""
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda agent, prompt, verify_fn=None, retry_note=None, **kwargs: None,
    )
    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda project_id, topic, days=None: [{"day": 1, "city": "成都", "area": "", "missing_entities": ["东郊记忆"], "reason": "原文未提到"}],
    )
    store.write_route_plan(
        isolated_project, "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都")]),
    )
    state = {"project_id": isolated_project, "topic": "成都"}
    first_result = route_citation_check_node(state)
    assert first_result["force_finalize"] is True
    stale_id = store.read_workspace(isolated_project, "conflicts.jsonl")[-1]["id"]

    # 模拟"下游冲突触发route_agent重写后，这次调用核验直接通过"——全新调用，
    # last_record_id理应从None开始，但应该能靠get_latest_unresolved_conflict找回
    # 上一次调用留下的那条force_finalized记录并resolve掉。
    monkeypatch.setattr(citation_check, "verify_route_citations", lambda project_id, topic, days=None: [])
    second_result = route_citation_check_node(state)

    assert second_result["force_finalize"] is False
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    stale_record = next(c for c in conflicts if c["id"] == stale_id)
    assert stale_record["status"] == "resolved"


# ---------------------------------------------------------------------------
# citation_check_node
# ---------------------------------------------------------------------------


def _seed_lifestyle_written(project_id: str, text: str = "推荐去东郊记忆拍照") -> None:
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": text, "sources": [], "claimed_entities": ["东郊记忆"]}],
    )


def test_citation_check_node_passes_immediately_and_advances_progress(isolated_project, monkeypatch):
    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", lambda project_id, topic, fields=None, item_keys=None: [])

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = citation_check_node(state)

    assert result["force_finalize"] is False
    assert "edit_citation_checked" not in result
    assert store.read_progress(isolated_project).state.value == "整合中"


def test_citation_check_node_retry_uses_fewshot_free_agent_builder(isolated_project, monkeypatch):
    """2026-08-11修复：citation_check_node打回重试必须用build_lifestyle_agent_retry_fix
    （不拼_FEWSHOT_TRAJECTORY），不能用build_lifestyle_agent（拼了）——真实E2E复现过：
    citation_check_node只发一条孤立fix_prompt（不带这次任务的原始历史），build_lifestyle_agent
    拼的_FEWSHOT_TRAJECTORY（"某某镇/某某广场"格式示例）排在fix_prompt前面，模型误把这个
    假任务当真，判断fix_prompt里的真实地名跟自己任务无关，拒绝按核验反馈修正，维持few-shot
    格式的占位内容原样提交。这个测试直接锁定"citation_check_node调用的是哪个builder"，
    不能只测行为结果（后者monkeypatch错函数名也会通过，之前发生过）。"""
    _seed_lifestyle_written(isolated_project)
    called_with_wrong_builder = {"flag": False}

    def wrong_builder(project_id, topic, **kwargs):
        called_with_wrong_builder["flag"] = True
        return object()

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", wrong_builder)
    retry_builder_kwargs = []

    def retry_builder(project_id, topic, **kwargs):
        retry_builder_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent_retry_fix", retry_builder)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", lambda agent, messages, **kwargs: None)
    monkeypatch.setattr(
        citation_check,
        "verify_lifestyle_citations",
        lambda project_id, topic, fields=None, item_keys=None: [
            {
                "field": "food",
                "day": 1,
                "recommendation": "推荐去东郊记忆拍照",
                "missing_entities": ["东郊记忆"],
                "reason": "原文未提到",
            }
        ]
        if not called_with_wrong_builder["flag"]
        else [],
    )

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    citation_check_node(state)

    assert called_with_wrong_builder["flag"] is False  # build_lifestyle_agent（带fewshot）不应该被调用
    assert retry_builder_kwargs == [
        {"convergence_work_unit": "lifestyle_citation_day_1_attempt_1"},
        {"convergence_work_unit": "lifestyle_citation_day_1_attempt_2"},
    ]


def test_citation_check_node_repairs_lifestyle_issues_in_day_work_units(
    isolated_project, monkeypatch
):
    _seed_lifestyle_written(isolated_project)
    work_units = []
    prompts = []

    def retry_builder(project_id, topic, **kwargs):
        work_units.append(kwargs["convergence_work_unit"])
        return object()

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent_retry_fix", retry_builder)
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: prompts.append(messages[0]["content"]),
    )

    calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None, claim_ids=None):
        calls["n"] += 1
        if calls["n"] > 1:
            return []
        return [
            {
                "field": "food",
                "day": 1,
                "recommendation": "Day1问题建议",
                "missing_entities": ["地点A"],
                "reason": "引用不支持",
            },
            {
                "field": "play",
                "day": 2,
                "recommendation": "Day2问题建议",
                "missing_entities": ["地点B"],
                "reason": "引用不支持",
            },
        ]

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    result = citation_check_node(
        {"project_id": isolated_project, "topic": "青甘大环线", "mode": "plan"}
    )

    assert result["force_finalize"] is False
    assert work_units == [
        "lifestyle_citation_day_1_attempt_1",
        "lifestyle_citation_day_2_attempt_1",
    ]
    assert "只处理Day1" in prompts[0]
    assert "Day2问题建议" not in prompts[0]
    assert "只处理Day2" in prompts[1]
    assert "Day1问题建议" not in prompts[1]


def test_citation_check_node_reuses_exact_passes_only_for_interrupted_repair(
    isolated_project, monkeypatch
):
    _seed_lifestyle_written(isolated_project)
    artifact = store.read_workspace(isolated_project, "outputs/lifestyle.json")
    orchestrator._append_lifestyle_repair_run(
        isolated_project,
        repair_id="repair-resume",
        status="artifact_written",
        artifact_sha256=orchestrator._lifestyle_artifact_sha(artifact),
        claim_ids={"claim-a"},
        item_keys={("food", 1)},
        repair_round=1,
    )
    calls = []

    def fake_verify(project_id, topic, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    result = citation_check_node(
        {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    )

    assert result["force_finalize"] is False
    assert calls == [
        {
            "fields": None,
            "item_keys": None,
            "claim_ids": {"claim-a"},
            "reuse_existing_completed_claims": True,
        }
    ]


@pytest.mark.parametrize(
    "stop_error",
    [
        GraphRecursionError("research did not converge"),
        orchestrator.ContextBudgetExceededError("research exceeded context budget"),
    ],
)
def test_citation_check_node_uses_write_only_fallback_after_deterministic_research_stop(
    isolated_project, monkeypatch, stop_error
):
    _seed_lifestyle_written(isolated_project)
    research_agent = object()
    write_only_agent = object()
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent_retry_fix",
        lambda project_id, topic, **kwargs: research_agent,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent_retry_write_only",
        lambda project_id, topic: write_only_agent,
    )

    verify_calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None, claim_ids=None):
        verify_calls["n"] += 1
        if verify_calls["n"] > 1:
            return []
        return [
            {
                "field": "food",
                "day": 1,
                "recommendation": "推荐去东郊记忆拍照",
                "missing_entities": ["东郊记忆"],
                "reason": "引用不支持",
            }
        ]

    invoke_calls = []
    write_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        invoke_calls.append((agent, kwargs.get("recursion_limit")))
        if agent is research_agent:
            raise stop_error
        write_prompts.append(messages[0]["content"])
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {
                    "field": "food",
                    "day": 1,
                    "recommendation": "Day1按路线就近用餐，具体店铺以现场为准。",
                    "sources": [],
                    "claimed_entities": [],
                }
            ],
        )
        raise GraphRecursionError("write completed on final allowed step")

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = citation_check_node(
        {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    )

    assert result["force_finalize"] is False
    assert invoke_calls == [(research_agent, 40), (write_only_agent, 30)]
    assert "研究已经结束，不能联网或继续检索" in write_prompts[0]
    assert "必要时使用检索工具补证" not in write_prompts[0]
    assert "受影响条目的完整精简JSON" in write_prompts[0]
    assert "就近用餐" in store.read_lifestyle_output(isolated_project).food.items[0].recommendation


def test_citation_check_node_accepts_successful_repair_write_signal(
    isolated_project, monkeypatch
):
    _seed_lifestyle_written(isolated_project)
    research_agent = object()
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent_retry_fix",
        lambda project_id, topic, **kwargs: research_agent,
    )
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent_retry_write_only",
        lambda project_id, topic: pytest.fail("成功写入后不应进入受限写入兜底"),
    )

    verify_calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None, claim_ids=None):
        verify_calls["n"] += 1
        if verify_calls["n"] > 1:
            return []
        return [
            {
                "field": "food",
                "day": 1,
                "recommendation": "推荐去东郊记忆拍照",
                "missing_entities": ["东郊记忆"],
                "reason": "引用不支持",
            }
        ]

    def fake_invoke(agent, messages, **kwargs):
        assert agent is research_agent
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {
                    "field": "food",
                    "day": 1,
                    "recommendation": "Day1按路线就近用餐，具体店铺以现场为准。",
                    "sources": [],
                    "claimed_entities": [],
                }
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = citation_check_node(
        {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    )

    assert result["force_finalize"] is False
    assert verify_calls["n"] == 2
    assert "就近用餐" in store.read_lifestyle_output(isolated_project).food.items[0].recommendation


def test_lifestyle_agent_retry_fix_prompt_has_no_fewshot_trajectory():
    """build_lifestyle_agent_retry_fix的prompt必须是纯system_prompt字符串，不是
    _prompt_with_fewshot闭包——直接断言构造出的agent的prompt不含"某某镇"/"某某广场"
    这类_FEWSHOT_TRAJECTORY专属占位文本。"""
    from src.agents.lifestyle_agent.agent import _FEWSHOT_TRAJECTORY

    fewshot_first_content = _FEWSHOT_TRAJECTORY[0].content
    assert "某某镇" in fewshot_first_content  # 确认这份fixture本身真的包含占位符，不是误判


def test_citation_check_node_reports_and_resolves_conflict_after_fix(isolated_project, monkeypatch):
    _seed_lifestyle_written(isolated_project)
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent_retry_fix", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", lambda agent, messages, **kwargs: None)

    calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                {
                    "field": "food",
                    "day": 1,
                    "recommendation": "推荐去东郊记忆拍照",
                    "missing_entities": ["东郊记忆"],
                    "reason": "原文未提到",
                }
            ]
        return []

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = citation_check_node(state)

    assert result["force_finalize"] is False
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert len(conflicts) == 1
    assert conflicts[0]["status"] == "resolved"
    assert conflicts[0]["target"] == "lifestyle_agent.food"


def test_citation_check_node_rechecks_only_failed_and_changed_claims(
    isolated_project, monkeypatch
):
    store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "保留已通过建议，修改问题建议。",
                "claims": [
                    {
                        "claim_id": "claim-pass",
                        "target_path": "recommendation",
                        "claim_text": "已通过的建议",
                        "support_mode": "estimate",
                    },
                    {
                        "claim_id": "claim-bad",
                        "target_path": "recommendation",
                        "claim_text": "待修改的建议",
                        "support_mode": "estimate",
                    },
                ],
            }
        ],
    )
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent_retry_fix",
        lambda project_id, topic, **kwargs: object(),
    )

    calls = []

    def fake_verify(project_id, topic, fields=None, item_keys=None, claim_ids=None):
        calls.append(claim_ids)
        if len(calls) == 1:
            return [
                {
                    "field": "food",
                    "day": 1,
                    "recommendation": "保留已通过建议，修改问题建议。",
                    "claim_id": "claim-bad",
                    "missing_entities": [],
                    "reason": "引用不支持",
                }
            ]
        return []

    def fake_invoke(agent, messages, **kwargs):
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {
                    "field": "food",
                    "day": 1,
                    "recommendation": "保留已通过建议，修改问题建议。",
                    "claims": [
                        {
                            "claim_id": "claim-pass",
                            "target_path": "recommendation",
                            "claim_text": "已通过的建议",
                            "support_mode": "estimate",
                        },
                        {
                            "claim_id": "claim-bad-new",
                            "target_path": "recommendation",
                            "claim_text": "已修改的建议",
                            "support_mode": "estimate",
                        },
                    ],
                }
            ],
        )

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = citation_check_node(
        {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    )

    assert result["force_finalize"] is False
    assert calls == [None, {"claim-bad", "claim-bad-new"}]


def test_citation_check_node_force_finalizes_after_max_retry(isolated_project, monkeypatch):
    _seed_lifestyle_written(isolated_project)
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent_retry_fix", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", lambda agent, messages, **kwargs: None)
    monkeypatch.setattr(
        citation_check,
        "verify_lifestyle_citations",
        lambda project_id, topic, fields=None, item_keys=None: [
            {
                "field": "food",
                "day": 1,
                "recommendation": "推荐去东郊记忆拍照",
                "missing_entities": ["东郊记忆"],
                "reason": "原文未提到",
            }
        ],
    )

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = citation_check_node(state)

    assert result["force_finalize"] is True
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "force_finalized"
    # docs/ISSUES.md第125条：耗尽MAX_RETRY后，issues里涉及的(field, day)应该被标记
    # citation_status=force_finalized。
    lifestyle = store.read_lifestyle_output(isolated_project)
    assert lifestyle.food.items[0].citation_status == "force_finalized"


# ---------------------------------------------------------------------------
# _missing_entities_retry_hint（docs/ISSUES.md第66条真实案例修复）
# ---------------------------------------------------------------------------


def test_missing_entities_retry_hint_lists_all_distinct_entities():
    issues = [
        {"missing_entities": ["葱油酥", "椒盐酥"]},
        {"missing_entities": ["锦里古街"]},
        {"missing_entities": ["葱油酥"]},  # 重复，不应该出现两次
    ]
    hint = _missing_entities_retry_hint(issues)
    assert "葱油酥" in hint
    assert "椒盐酥" in hint
    assert "锦里古街" in hint
    assert hint.count("葱油酥") == 1
    assert "单独发起一次精确检索" in hint


def test_missing_entities_retry_hint_empty_when_no_missing_entities():
    issues = [{"missing_entities": []}, {"missing_entities": []}]
    assert _missing_entities_retry_hint(issues) == ""


# ---------------------------------------------------------------------------
# _findings_retry_hint（docs/ISSUES.md第131条真实案例修复）
# ---------------------------------------------------------------------------


def test_findings_retry_hint_includes_recorded_facts(isolated_project):
    """真实复现场景：lifestyle_agent用record_finding记过"火星营地=095-c000"这类
    专名对照，打回时应该把这条记录原样附带进提示，而不是让模型重新检索或凭空配chunk_id。"""
    findings_dir = orchestrator.PROJECTS_DIR / isolated_project / "agents" / "lifestyle_agent"
    findings_dir.mkdir(parents=True)
    (findings_dir / "findings.jsonl").write_text(
        json.dumps({"fact": "火星营地=青甘大环线-095-c000（胶囊舱约1000元/晚）"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    hint = _findings_retry_hint(isolated_project, "lifestyle_agent")
    assert "火星营地" in hint
    assert "095-c000" in hint
    assert "先查自己记过的笔记" in hint


def test_findings_retry_hint_empty_when_no_findings_file(isolated_project):
    """这个agent这次run没调用过record_finding（没留下findings.jsonl）时，返回空字符串，
    不应该报错或产生误导性的空提示块。"""
    assert _findings_retry_hint(isolated_project, "lifestyle_agent") == ""


def test_findings_retry_hint_empty_for_pitfall_without_findings_file(isolated_project):
    """pitfall_agent本轮没有调用record_finding时，同样应该返回空字符串。"""
    assert _findings_retry_hint(isolated_project, "pitfall_agent") == ""


def test_findings_retry_hint_includes_multiple_facts(isolated_project):
    """多条record_finding记录都应该被包含进提示，不只是第一条。"""
    findings_dir = orchestrator.PROJECTS_DIR / isolated_project / "agents" / "route_agent"
    findings_dir.mkdir(parents=True)
    (findings_dir / "findings.jsonl").write_text(
        "\n".join(
            json.dumps({"fact": f}, ensure_ascii=False)
            for f in ["茶卡盐湖门票60元", "大柴旦翡翠湖=青甘大环线-130-c001"]
        )
        + "\n",
        encoding="utf-8",
    )
    hint = _findings_retry_hint(isolated_project, "route_agent")
    assert "茶卡盐湖门票60元" in hint
    assert "130-c001" in hint


def test_citation_check_node_fix_prompt_includes_findings_hint(isolated_project, monkeypatch):
    """端到端回归：citation_check_node打回时，fix_prompt里应该真的带上findings.jsonl
    的内容，不只是_findings_retry_hint函数本身返回正确——同款诉求对应
    test_citation_check_node_fix_prompt_includes_targeted_retry_hint（第66条那个）。"""
    _seed_lifestyle_written(isolated_project)
    findings_dir = orchestrator.PROJECTS_DIR / isolated_project / "agents" / "lifestyle_agent"
    findings_dir.mkdir(parents=True)
    (findings_dir / "findings.jsonl").write_text(
        json.dumps({"fact": "火星营地=青甘大环线-095-c000"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent_retry_fix", lambda project_id, topic, **kwargs: object())

    captured_messages = []

    def fake_invoke_with_retry(agent, messages, **kwargs):
        captured_messages.append(messages)
        return None

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke_with_retry)

    calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                {
                    "field": "food",
                    "day": 3,
                    "recommendation": "可以去火星营地方向吃自热火锅",
                    "missing_entities": ["火星营地"],
                    "reason": "原文只提到火星1号公路，未提及火星营地",
                }
            ]
        return []

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    state = {"project_id": isolated_project, "topic": "青甘大环线", "mode": "plan"}
    citation_check_node(state)

    assert len(captured_messages) == 1
    fix_prompt_content = captured_messages[0][0]["content"]
    assert "火星营地" in fix_prompt_content
    assert "095-c000" in fix_prompt_content
    assert "先查自己记过的笔记" in fix_prompt_content


def test_citation_check_node_fix_prompt_targets_failed_claims(isolated_project, monkeypatch):
    """普通引用返修应保留缺失实体，并只要求针对失败条目检查或补证。"""
    _seed_lifestyle_written(isolated_project)
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent_retry_fix", lambda project_id, topic, **kwargs: object())

    captured_messages = []

    def fake_invoke_with_retry(agent, messages, **kwargs):
        captured_messages.append(messages)
        return None

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke_with_retry)

    calls = {"n": 0}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                {
                    "field": "food",
                    "day": 3,
                    "recommendation": "可以买葱油酥和椒盐酥等伴手礼",
                    "missing_entities": ["葱油酥", "椒盐酥"],
                    "reason": "原文只提到糕点，未具体提及这两个名称",
                }
            ]
        return []

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    citation_check_node(state)

    assert len(captured_messages) == 1
    fix_prompt_content = captured_messages[0][0]["content"]
    assert "葱油酥" in fix_prompt_content
    assert "椒盐酥" in fix_prompt_content
    assert "针对这些具体条目" in fix_prompt_content
    assert "必要时使用检索工具补证" in fix_prompt_content
    assert "其他没被指出问题的条目不用改" in fix_prompt_content


def test_citation_check_node_preserves_sticky_force_finalize_flag_from_route_check(isolated_project, monkeypatch):
    """跟route_citation_check_node同款粘性标志验证：route阶段核验已经放弃修复
    （force_finalize=True）时，即使lifestyle这一轮核验通过，也不能被覆盖回False，
    否则integration_agent收不到"如实标注未解决问题"的提示（docs/ISSUES.md）。"""
    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", lambda project_id, topic, fields=None, item_keys=None: [])

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan", "force_finalize": True}
    result = citation_check_node(state)

    assert result["force_finalize"] is True


def test_citation_check_node_sets_edit_citation_checked_flag_in_edit_mode(isolated_project, monkeypatch):
    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", lambda project_id, topic, fields=None, item_keys=None: [])

    state = {"project_id": isolated_project, "topic": "成都", "mode": "edit"}
    result = citation_check_node(state)

    assert result["edit_citation_checked"] is True


def test_citation_check_node_scopes_verification_to_edit_changed_fields(isolated_project, monkeypatch):
    """docs/ISSUES.md第37补条修复：edit模式下只核验这次编辑实际改动的字段，
    不应该把edit_changed_lifestyle_fields之外的字段也重新拉出来核验。"""
    captured = {}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        captured["fields"] = fields
        return []

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "edit",
        "edit_changed_lifestyle_fields": ["play"],
    }
    citation_check_node(state)

    assert captured["fields"] == ["play"]


def test_citation_check_node_falls_back_to_full_verification_without_changed_fields(isolated_project, monkeypatch):
    """edit模式下如果edit_changed_lifestyle_fields没有被设置（比如lifestyle_agent这次是被
    上游级联触发整体重新生成，不是用户直接定点编辑），应该回退到全量核验（fields=None），
    不能因为state里缺这个key就误判成"这次什么都没改"。"""
    captured = {}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        captured["fields"] = fields
        return []

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "edit"}
    citation_check_node(state)

    assert captured["fields"] is None


def test_citation_check_node_plan_mode_always_full_verification(isolated_project, monkeypatch):
    """plan模式下即使state里意外带着edit_changed_lifestyle_fields（比如陈旧的测试数据），
    也不应该被拿去做核验范围收窄——scoping只在edit模式下生效。"""
    captured = {}

    def fake_verify(project_id, topic, fields=None, item_keys=None):
        captured["fields"] = fields
        return []

    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", fake_verify)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "mode": "plan",
        "edit_changed_lifestyle_fields": ["play"],
    }
    citation_check_node(state)

    assert captured["fields"] is None


# ---------------------------------------------------------------------------
# integration_agent_node：整合重试上限耗尽的死循环回归测试
#
# 真实E2E（2026-07-21，chengdu-3d-e2e-v18）首次触发_route_integration_phase的
# MAX_RETRY分支时发现的死循环bug：那个函数是LangGraph条件边路由函数，只能返回
# 下一跳节点名，没有能力把"该强制终结了"这个事实写回GraphState，它对上限的处理
# 方式是直接调用store.mark_force_finalize()写磁盘，但这个磁盘副作用从未被
# integration_agent_node读取过——那个节点只认state.get("force_finalize")这一个
# 信号源，而这个key只会被route_citation_check_node/citation_check_node（核验层面）
# 设置，从不会被_route_integration_phase的MAX_RETRY分支设置。结果：一旦触发到
# 上限，会不断把不受限制的prompt喂给模型，模型每次都诚实地发现同一个真实冲突并
# 调用report_conflict，形成死循环——真实E2E里5分钟内产生了24条冲突记录，直到
# 进程被手动杀掉。详见docs/ISSUES.md。
# ---------------------------------------------------------------------------


def test_integration_agent_node_forces_finalize_note_when_retry_count_at_max(isolated_project, monkeypatch):
    """核心回归场景：即使state里的force_finalize粘性标志从未被设置过（route/lifestyle
    核验阶段都通过了，冲突纯粹是整合阶段自己发现的），只要integration_retry_count已经
    达到MAX_RETRY（_route_integration_phase判断上限用的同一个权威计数器），
    integration_agent_node喂给模型的prompt里就必须包含"不允许再调用report_conflict"
    这道禁令——否则模型会诚实地再报一次同样的冲突，形成死循环。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="#原始攻略\n" + "内容" * 100, meta={})
    store.report_conflict(isolated_project, actor="integration_agent", target="lifestyle_agent.play", reason="结构性冲突：路线和游玩安排的天数错位")

    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        # 模拟模型看到prompt后仍然诚实地发现同一个真实冲突并再次report_conflict——
        # 如果修复没生效，这是完全合理的模型行为（没人告诉它必须停止）。
        store.report_conflict(isolated_project, actor="integration_agent", target="lifestyle_agent.play", reason="结构性冲突依旧：路线和游玩安排的天数错位")
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": {"destination": "成都", "days": 3},
        "mode": "plan",
        "integration_retry_count": MAX_RETRY,  # _route_integration_phase已判定达到上限
        # 关键：这里没有"force_finalize": True——粘性标志从未被设置过，
        # 这正是bug复现的必要条件。
    }
    integration_agent_node(state)

    assert captured_prompts, "invoke_with_retry应该被调用过"
    assert "不允许再调用report_conflict" in captured_prompts[0]


def test_integration_agent_node_no_force_note_when_retry_count_below_max(isolated_project, monkeypatch):
    """对照组：integration_retry_count还没到MAX_RETRY时，不应该出现强制终结的禁令，
    模型仍然应该被允许正常report_conflict——确认修复没有过度触发。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="#原始攻略\n" + "内容" * 100, meta={})
    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        store.write_final_guide(
            isolated_project, actor="integration_agent", content_md="#原始攻略\n" + "内容" * 100, meta={}
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": {"destination": "成都", "days": 3},
        "mode": "plan",
        "integration_retry_count": MAX_RETRY - 1,
    }
    integration_agent_node(state)

    assert "不允许再调用report_conflict" not in captured_prompts[0]


# ---------------------------------------------------------------------------
# integration_agent_node：plan模式路径的其余分支——此前只有edit模式
# （test_orchestrator_edit_mode.py）和上面的"force_note"死循环回归场景被直接invoke测试过，
# grounding（真实chunk原文注入prompt）、无冲突时推进到"树木生成中"、冲突target区分是否
# 计入integration_retry_count这几条plan模式分支从未被覆盖，见docs/ARCHITECTURE.md第12.1节
# P0任务。跟其余测试同款风格：不调真实LLM，monkeypatch build_integration_agent/
# invoke_with_retry/citation_check.load_chunk_texts。
# ---------------------------------------------------------------------------


def _seed_route_and_lifestyle_with_citation(project_id: str, chunk_id: str = "c001") -> None:
    route_plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                area="武侯区",
                pois=[RoutePoi(name="武侯祠")],
                sources=[SourceRef(platform="bilibili", author="up主", doc_id="d1", chunk_id=chunk_id)],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=route_plan)
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {"field": f, "day": 1, "recommendation": "推荐去武侯祠", "sources": [], "claimed_entities": []}
            for f in ["food", "outfit", "lodging", "play"]
        ],
    )


def _write_current_route_map(project_id: str) -> None:
    route_plan = store.read_workspace(project_id, "route_plan.json")
    names = [poi["name"] for poi in route_plan["days"][0]["pois"]]
    entities = [
        {
            "original_name": name,
            "query_name": name,
            "status": "resolved",
            "candidates": [{"raw_provider_response": "不得投影"}],
            "selected": {
                "provider_id": f"provider-{index}",
                "provider_name": f"{name}官方实体",
                "province": "四川省",
                "city": "成都市",
                "district": "武侯区",
                "lng": 104.04 + index * 0.01,
                "lat": 30.64 + index * 0.01,
            },
        }
        for index, name in enumerate(names)
    ]
    legs = []
    if len(names) >= 2:
        legs.append(
            {
                "from": names[0],
                "to": names[1],
                "provider_from": f"{names[0]}官方实体",
                "provider_to": f"{names[1]}官方实体",
                "distance_km": 1.2,
                "duration_h": 0.1,
                "transport_mode": "driving",
                "status": "passed",
            }
        )
    store._write_json(
        store.workspace_dir(project_id) / "route_map_validation.json",
        {
            "route_plan_structure_hash": route_structure_hash(route_plan),
            "status": "passed",
            "days": [
                {
                    "day": 1,
                    "entities": entities,
                    "legs": legs,
                    "totals": {
                        "distance_km": 1.2 if legs else 0,
                        "duration_h": 0.1 if legs else 0,
                    },
                }
            ],
        },
    )


def _mock_integration_agent_writes_guide(monkeypatch, project_id: str, captured_prompts: list) -> None:
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        store.write_final_guide(
            project_id,
            actor="integration_agent",
            content_md="# 攻略\n\n## 行前必备清单\n证件与药品\n\n## Day 1\n" + "内容" * 100,
            meta={
                "merged_fields": [
                    "route_plan",
                    "lifestyle.food",
                    "lifestyle.outfit",
                    "lifestyle.lodging",
                    "lifestyle.play",
                ],
                "checklist_included": True,
            },
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)


def test_integration_agent_node_resume_prompt_includes_latest_checkpoint_write_error(
    isolated_project, monkeypatch
):
    old_error = "工具参数校验失败：blocks[0].section不能为空"
    latest_error = "工具参数校验失败：meta.merged_fields缺少lifestyle.play"
    checkpoint_messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_final_guide", "args": {}, "id": "write-old"}
            ],
        ),
        ToolMessage(content=old_error, tool_call_id="write-old"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_final_guide", "args": {}, "id": "write-latest"}
            ],
        ),
        ToolMessage(content=latest_error, tool_call_id="write-latest"),
    ]

    class FailedCheckpointAgent:
        checkpointer = object()

        def get_state(self, config):
            return SimpleNamespace(
                values={"messages": checkpoint_messages},
                next=("tools",),
            )

    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        orchestrator,
        "build_integration_agent",
        lambda project_id, topic, **kwargs: FailedCheckpointAgent(),
    )
    monkeypatch.setattr(
        orchestrator,
        "clear_agent_thread",
        lambda *args, **kwargs: pytest.fail("失败checkpoint草稿不应被清空"),
    )
    captured = {}

    def fake_verified_invoke(agent, prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md=(
                "# 攻略\n\n## 行前必备清单\n证件与药品\n\n## Day 1\n"
                + "内容" * 100
            ),
            meta={
                "merged_fields": [
                    "route_plan",
                    "lifestyle.food",
                    "lifestyle.outfit",
                    "lifestyle.lodging",
                    "lifestyle.play",
                ],
                "checklist_included": True,
            },
        )

    monkeypatch.setattr(
        orchestrator, "_invoke_with_write_verification", fake_verified_invoke
    )

    integration_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "requirements": {**_base_requirements(), "days": 1},
            "mode": "plan",
        }
    )

    assert captured["resume_from_checkpoint"] is True
    assert latest_error in captured["prompt"]
    assert old_error not in captured["prompt"]


def test_integration_plan_mode_prompt_excludes_grounding_chunk_text(isolated_project, monkeypatch):
    """token优化：integration_agent的prompt不再inline chunk全文（原第8条grounding机制）——
    逐条引用真实性核验已由citation_check系节点承担（它们各自独立读原文核验，不受这里影响），
    这里的"二次核查"是冗余的，且是单次prompt里体积最大的一块（8天长线场景尤其明显）。
    真实chunk原文仍然要经过cited_chunks/chunk_texts流入_replace_reference_section
    机械生成参考来源表，只是不再经过LLM看到，见本函数下方的
    test_integration_reference_section_still_uses_real_chunk_text。"""
    _seed_route_and_lifestyle_with_citation(isolated_project, chunk_id="c001")
    monkeypatch.setattr(
        citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "武侯祠原文：三国蜀汉丞相祠堂，门票60元"}
    )
    captured_prompts: list = []
    _mock_integration_agent_writes_guide(monkeypatch, isolated_project, captured_prompts)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    integration_agent_node(state)

    prompt = captured_prompts[0]
    assert "武侯祠原文：三国蜀汉丞相祠堂，门票60元" not in prompt
    assert "[c001]" not in prompt
    assert "只有status=passed的路段可作为精确距离与时长的唯一来源" in prompt
    assert "anomaly只表示风险，不得引用其数值" in prompt
    assert "不要凭自己的地理知识重新判断地点距离" in prompt
    assert "推荐地点离路线过远" not in prompt


def test_integration_plan_mode_does_not_reuse_stale_final_blocks(
    isolated_project, monkeypatch
):
    _seed_route_and_lifestyle_with_citation(isolated_project)
    store._write_json(
        store._workspace_dir(isolated_project) / "final_guide.blocks.json",
        {
            "blocks": [
                {
                    "block_id": "stale-block",
                    "section": "day",
                    "markdown": "STALE_WITHHELD_FACT",
                    "upstream_claim_ids": [],
                }
            ]
        },
    )
    captured_prompts: list = []
    _mock_integration_agent_writes_guide(monkeypatch, isolated_project, captured_prompts)

    integration_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "requirements": _base_requirements(),
            "mode": "plan",
        }
    )

    assert "STALE_WITHHELD_FACT" not in captured_prompts[0]
    assert "已有最终攻略正文块（局部修改时保留未受影响块）：null" in captured_prompts[0]


def test_integration_prompt_preserves_feasibility_unknown(isolated_project, monkeypatch):
    _seed_route_and_lifestyle_with_citation(isolated_project)
    store.write_route_feasibility(
        isolated_project,
        "citation_checker",
        RouteFeasibilityReport(
            overall_status="unknown",
            force_finalized=True,
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-unknown",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="poi_operational_status",
                    status="unknown",
                    summary="官方来源暂时无法访问",
                )
            ],
        ),
    )
    captured_prompts: list = []
    _mock_integration_agent_writes_guide(monkeypatch, isolated_project, captured_prompts)

    integration_agent_node(
        {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    )

    prompt = captured_prompts[0]
    assert "路线可执行性核验" in prompt
    assert "官方来源暂时无法访问" in prompt
    assert "运营提醒由程序Join统一投影" in prompt
    assert "没有technical_error的unknown写成现有证据不足" not in prompt
    assert "带technical_error的unknown只写成本次自动核验暂未完成" not in prompt
    assert "不得据此删除、缩短、替换或重排POI" in prompt


def test_integration_prompt_reuses_current_route_map_facts(isolated_project, monkeypatch):
    _seed_route_and_lifestyle_with_citation(isolated_project)
    route_plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                area="武侯区",
                pois=[RoutePoi(name="武侯祠"), RoutePoi(name="锦里")],
            )
        ]
    )
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=route_plan)
    _write_current_route_map(isolated_project)
    captured_prompts: list = []
    _mock_integration_agent_writes_guide(monkeypatch, isolated_project, captured_prompts)

    integration_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "requirements": _base_requirements(),
            "mode": "plan",
        }
    )

    prompt = captured_prompts[0]
    assert '"provider_name": "武侯祠官方实体"' in prompt
    assert '"distance_km": 1.2' in prompt
    assert '"duration_h": 0.1' in prompt
    assert "raw_provider_response" not in prompt


def test_integration_prompt_uses_delivery_view_without_raw_evidence(
    isolated_project, monkeypatch
):
    _seed_route_and_lifestyle_with_citation(isolated_project)
    lifestyle = store.read_workspace(isolated_project, "outputs/lifestyle.json")
    lifestyle["food"]["items"][0]["claims"] = [
        {
            "claim_id": "lifestyle-claim-food-day1",
            "target_path": "recommendation",
            "claim_text": "Day 1推荐在武侯祠附近用餐。",
            "claim_type": "recommendation",
            "support_mode": "direct",
            "evidence_refs": [{"platform": "bilibili", "chunk_id": "c001"}],
            "evidence_snapshots": [
                {
                    "source": {"platform": "bilibili", "chunk_id": "c001"},
                    "excerpt": "不应进入Integration Prompt的Lifestyle原始证据",
                }
            ],
        }
    ]
    store._write_json(
        store._workspace_dir(isolated_project) / "outputs/lifestyle.json", lifestyle
    )
    store.write_route_feasibility(
        isolated_project,
        "citation_checker",
        RouteFeasibilityReport(
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-warning",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="warning",
                    summary="建议出发前确认预约要求",
                    evidence=[
                        {
                            "title": "原始网页",
                            "excerpt": "不应进入Integration Prompt的Web原始证据",
                        }
                    ],
                )
            ],
        ),
    )
    captured_prompts: list = []
    captured_retry_limits: list[int] = []
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda *args, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        captured_retry_limits.append(kwargs.get("max_retries"))
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md="# 攻略\n\n## 行前必备清单\n证件与药品\n\n## Day 1\n" + "内容" * 100,
            meta={
                "merged_fields": [
                    "route_plan",
                    "lifestyle.food",
                    "lifestyle.outfit",
                    "lifestyle.lodging",
                    "lifestyle.play",
                ],
                "checklist_included": True,
            },
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    integration_agent_node(
        {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    )

    prompt = captured_prompts[0]
    assert "建议出发前确认预约要求" in prompt
    assert "lifestyle-claim-food-day1" in prompt
    assert "Day 1推荐在武侯祠附近用餐。" in prompt
    assert "不应进入Integration Prompt的Lifestyle原始证据" not in prompt
    assert "不应进入Integration Prompt的Web原始证据" not in prompt
    assert captured_retry_limits == [1]


def test_integration_reference_section_still_uses_real_chunk_text(isolated_project, monkeypatch):
    """prompt里不再看chunk原文，但final_guide.md末尾的参考来源表仍然是程序机械生成
    （build_reference_section，不经过LLM），必须继续正确带上真实原文摘要——
    这是第8条第2层修复，跟本次prompt瘦身无关，不能被误伤。"""
    _seed_route_and_lifestyle_with_citation(isolated_project, chunk_id="c001")
    monkeypatch.setattr(
        citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "武侯祠原文：三国蜀汉丞相祠堂，门票60元"}
    )
    captured_prompts: list = []
    _mock_integration_agent_writes_guide(monkeypatch, isolated_project, captured_prompts)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    integration_agent_node(state)

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "武侯祠原文：三国蜀汉丞相祠堂，门票60元" in guide
    validation = store.read_workspace(isolated_project, "final_guide.meta.json")["contract_validation"]
    assert validation["body_sha256"]
    assert validation["guide_sha256"] == hashlib.sha256(guide.encode("utf-8")).hexdigest()


def test_integration_validates_after_reference_section_normalization(isolated_project, monkeypatch):
    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "原文"})
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md=(
                "# 攻略\n\n正文开头\n\n## 参考来源\n模型自写来源\n\n"
                "## 行前必备清单\n证件与药品\n\n## Day 1\n" + "内容" * 120
            ),
            meta={
                "merged_fields": [
                    "route_plan",
                    "lifestyle.food",
                    "lifestyle.outfit",
                    "lifestyle.lodging",
                    "lifestyle.play",
                ],
                "checklist_included": True,
            },
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    integration_agent_node(
        {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    )

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert {issue["code"] for issue in conflict.issues} >= {"final_guide_too_short", "missing_day", "checklist_missing"}


def test_integration_plan_mode_no_conflict_advances_to_poi_detail_phase(isolated_project, monkeypatch):
    """plan模式独有的转场：没有冲突时下一阶段应该是"树木生成中"（POI_DETAIL），
    不是edit模式那样维持在"完成"——这条分支此前从未被直接invoke测试覆盖过。"""
    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "原文"})
    captured_prompts: list = []
    _mock_integration_agent_writes_guide(monkeypatch, isolated_project, captured_prompts)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    result = integration_agent_node(state)

    assert result["integration_done"] is True
    assert store.read_progress(isolated_project).state == ProjectPhase.POI_DETAIL


def test_integration_plan_mode_contract_failure_bounces_to_itself(isolated_project, monkeypatch):
    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "原文"})
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md="# 攻略\n\n## 行前必备清单\n" + "内容" * 120,
            meta={"merged_fields": ["route_plan"], "checklist_included": True},
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    result = integration_agent_node(
        {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    )

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.target == "integration_agent"
    assert conflict.issue_type == "final_guide_contract"
    assert {issue["code"] for issue in conflict.issues} == {"missing_day", "merged_field_missing"}
    assert result["integration_retry_count"] == 1
    assert store.read_workspace(isolated_project, "final_guide.meta.json")["contract_validation"]["status"] == "retrying"
    assert store.read_progress(isolated_project).state != ProjectPhase.POI_DETAIL


def test_integration_contract_failure_at_retry_limit_force_finalizes_for_review(isolated_project, monkeypatch):
    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "原文"})
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md="# 攻略\n\n## 行前必备清单\n" + "内容" * 120,
            meta={"merged_fields": ["route_plan"], "checklist_included": True},
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    result = integration_agent_node({
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": _base_requirements(),
        "mode": "plan",
        "integration_retry_count": MAX_RETRY,
    })

    assert store.get_pending_conflict(isolated_project) is None
    validation = store.read_workspace(isolated_project, "final_guide.meta.json")["contract_validation"]
    assert validation["status"] == "force_finalized"
    assert {issue["code"] for issue in validation["issues"]} == {"missing_day", "merged_field_missing"}
    assert "integration_retry_count" not in result
    assert store.read_progress(isolated_project).state == ProjectPhase.POI_DETAIL


def test_integration_plan_mode_new_conflict_targeting_lifestyle_does_not_increment_own_retry_count(
    isolated_project, monkeypatch
):
    """区分"谁的retry_count"：模型这一轮判断衣食住行产出和路线冲突、调用report_conflict
    （target=lifestyle_agent.xxx）时，这不是integration_agent自己造成的重试，
    不应该递增state["integration_retry_count"]——只有target是integration_agent自己
    （比如长度骤降自检）时才该递增，见integration_agent_node末尾的条件合并逻辑。"""
    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "原文"})
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.report_conflict(
            isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason="推荐的辣味菜品和用户忌口冲突"
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    result = integration_agent_node(state)

    assert "integration_retry_count" not in result
    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None and conflict.target == "lifestyle_agent.food"
    # 有新冲突时不应该推进阶段
    assert store.read_progress(isolated_project).state != ProjectPhase.POI_DETAIL


def test_integration_plan_mode_resolves_prior_conflict_when_this_round_is_clean(isolated_project, monkeypatch):
    """上一轮打回的冲突，这一轮模型修复干净（没有再report_conflict）时，
    应该被resolve掉，不能悬空留着一条pending记录挡住后续路由判断。"""
    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "原文"})
    prior = store.report_conflict(
        isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason="上一轮的问题"
    )
    captured_prompts: list = []
    _mock_integration_agent_writes_guide(monkeypatch, isolated_project, captured_prompts)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "requirements": _base_requirements(),
        "mode": "plan",
        "integration_retry_count": 1,
    }
    integration_agent_node(state)

    assert store.get_pending_conflict(isolated_project) is None
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[0]["id"] == prior.id
    assert conflicts[0]["status"] == "resolved"


def test_integration_plan_mode_skips_length_drop_self_check(isolated_project, monkeypatch):
    """对照组：长度骤降自检（见test_integration_diff_detection_reports_conflict_on_length_drop，
    tests/test_orchestrator_edit_mode.py）只在edit模式（is_direct_edit_target=True）生效——
    plan模式下即使模型把正文写得很短，也不应该被这条启发式规则误伤（plan模式本来就是
    "第一次生成"或"级联全量重新汇总"，没有"相对原文"这个基线可比较，见函数里
    `if is_direct_edit_target:` 的判断范围）。"""
    _seed_route_and_lifestyle_with_citation(isolated_project)
    monkeypatch.setattr(citation_check, "load_chunk_texts", lambda topic, project_id=None: {"c001": "原文"})
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        # 故意写一份很短的攻略（>200字符门槛，但明显比edit模式测试里骤降的场景更短）
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md="# 攻略\n\n## 行前必备清单\n证件与药品\n\n## Day 1\n" + "短" * 250,
            meta={
                "merged_fields": [
                    "route_plan",
                    "lifestyle.food",
                    "lifestyle.outfit",
                    "lifestyle.lodging",
                    "lifestyle.play",
                ],
                "checklist_included": True,
            },
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "topic": "成都", "requirements": _base_requirements(), "mode": "plan"}
    integration_agent_node(state)

    assert store.get_pending_conflict(isolated_project) is None
