from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from src import orchestrator
from src.materials import citation_check, timeliness_check
from src.orchestrator import (
    GraphState,
    await_route_structure_confirm_node,
    route_citation_check_node,
    route_draft_node,
    route_timeliness_check_node,
)
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store
from src.workspace.route_validation import route_structure_snapshot, validate_route_structure
from src.workspace.schema import (
    ProjectPhase,
    RouteDay,
    RouteFeasibilityCheck,
    RouteFeasibilityEvidence,
    RouteFeasibilityReport,
    RoutePlan,
    RoutePoi,
)


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(orchestrator, "PROJECTS_DIR", tmp_path)
    project_id = "route-structure-workflow"
    store.init_project(project_id)
    return project_id


def _plan(*, day2_poi: str = "熊猫基地", day2_notes: str = "原说明") -> RoutePlan:
    return RoutePlan(
        days=[
            RouteDay(day=1, city="成都", pois=[RoutePoi(name="宽窄巷子")]),
            RouteDay(day=2, city="成都", notes=day2_notes, pois=[RoutePoi(name=day2_poi)]),
            RouteDay(day=3, city="成都", pois=[RoutePoi(name="杜甫草堂")]),
        ]
    )


def _candidate_day2(*, poi: str = "人民公园", notes: str = "候选说明") -> dict:
    return RouteDay(day=2, city="成都", notes=notes, pois=[RoutePoi(name=poi)]).model_dump(mode="json")


def _candidate_day3(*, poi: str = "武侯祠", notes: str = "候选说明") -> dict:
    return RouteDay(day=3, city="成都", notes=notes, pois=[RoutePoi(name=poi)]).model_dump(mode="json")


def _confirm_graph():
    graph = StateGraph(GraphState)
    graph.add_node("confirm", await_route_structure_confirm_node)
    graph.add_edge(START, "confirm")
    graph.add_edge("confirm", END)
    return graph.compile(checkpointer=InMemorySaver())


def _revision_recovery_graph():
    graph = StateGraph(GraphState)
    graph.add_node("await_route_structure_confirm", await_route_structure_confirm_node)
    graph.add_node("route_draft", route_draft_node)
    graph.add_edge(START, "await_route_structure_confirm")
    graph.add_conditional_edges(
        "await_route_structure_confirm",
        lambda state: (
            "route_draft"
            if state.get("route_structure_revision_request")
            else END
        ),
    )
    graph.add_conditional_edges(
        "route_draft",
        lambda state: (
            "await_route_structure_confirm"
            if state.get("pending_route_structure_confirmation")
            else END
        ),
    )
    return graph.compile(checkpointer=InMemorySaver())


def test_validate_route_structure_enforces_exact_days_sequence_and_basic_content():
    invalid = {
        "days": [
            {"day": 1, "city": "成都", "pois": [{"name": "宽窄巷子"}]},
            {"day": 3, "city": "", "pois": []},
        ]
    }
    codes = {issue["code"] for issue in validate_route_structure(invalid, expected_days=3)}
    assert codes == {"day_count_mismatch", "day_sequence_invalid", "day_city_empty", "day_pois_empty"}


def test_route_write_modes_expose_disjoint_writers():
    facts = {tool.name for tool in make_workspace_tools("p", "route_agent", topic="成都", route_write_mode="facts_only")}
    proposal = {
        tool.name for tool in make_workspace_tools("p", "route_agent", topic="成都", route_write_mode="proposal_only")
    }
    assert "write_route_facts_patch" in facts
    assert "write_route_plan" not in facts
    assert "write_route_plan_patch" not in facts
    assert "write_route_change_proposal" in proposal
    assert "write_route_plan" not in proposal
    assert "write_route_facts_patch" not in proposal


def test_facts_patch_preserves_confirmed_structure(isolated_project):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    before = route_structure_snapshot(store.read_workspace(isolated_project, "route_plan.json"))

    store.write_route_facts_patch(
        isolated_project,
        "route_agent",
        2,
        notes="已补充预约提醒",
        sources=[{"platform": "web_verified", "chunk_id": "web-1"}],
    )

    after = store.read_workspace(isolated_project, "route_plan.json")
    assert route_structure_snapshot(after) == before
    assert after["days"][1]["notes"] == "已补充预约提醒"
    assert after["days"][1]["sources"][0]["chunk_id"] == "web-1"


def test_proposal_write_does_not_modify_active_route_and_apply_checks_base(isolated_project):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    before = store.read_workspace(isolated_project, "route_plan.json")
    proposal = store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2()],
        trigger="timeliness",
        reason="原POI闭园",
        evidence="官方公告",
    )
    assert store.read_workspace(isolated_project, "route_plan.json") == before
    assert proposal["status"] == "pending"

    store.decide_route_change_proposal(isolated_project, "main_agent", "apply")
    applied = store.read_workspace(isolated_project, "route_plan.json")
    assert applied["days"][1]["pois"][0]["name"] == "人民公园"


def test_confirmed_structure_proposal_applies_then_requires_both_route_checks(isolated_project):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2()],
        trigger="timeliness",
        reason="原POI闭园",
    )
    graph = _confirm_graph()
    config = {"configurable": {"thread_id": "route-structure-confirm"}}
    result = graph.invoke(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "pending_route_structure_confirmation": True,
            "route_structure_resume_check": "timeliness",
        },
        config,
    )
    assert "route_change_proposal" in result["__interrupt__"][0].value

    result = graph.invoke(Command(resume="confirm"), config)
    route = store.read_workspace(isolated_project, "route_plan.json")
    assert route["days"][1]["pois"][0]["name"] == "人民公园"
    assert result["route_citation_checked"] is False
    assert result["route_map_checked"] is False
    assert result["route_timeliness_checked"] is False
    assert result["route_recheck_days"] == [2]
    assert result["lifestyle_agent_done"] is False


def test_empty_route_structure_revision_reprompts_without_changing_proposal(
    isolated_project, monkeypatch
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    proposal = store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2()],
        trigger="timeliness",
        reason="原POI闭园",
    )
    graph = _revision_recovery_graph()
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: graph)
    config = {"configurable": {"thread_id": f"{isolated_project}:plan"}}
    graph.invoke(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "pending_route_structure_confirmation": True,
        },
        config,
    )

    result = orchestrator.resume_plan_route_decision(isolated_project, "revise:")

    assert result["status"] == "action_required"
    assert "__interrupt__" in result
    unchanged = store.read_route_change_proposal(isolated_project)
    assert unchanged["proposal_id"] == proposal["proposal_id"]
    assert unchanged["status"] == "pending"


def test_edit_proposal_rechecks_proposed_days_instead_of_stale_edit_days(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    original_route = store.read_workspace(isolated_project, "route_plan.json")
    store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day3()],
        trigger="integration_conflict",
        reason="Day3安排需要调整",
    )
    graph = _confirm_graph()
    config = {"configurable": {"thread_id": "route-structure-edit-scope"}}
    graph.invoke(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "mode": "edit",
            "edit_changed_route_days": [2],
            "pending_route_structure_confirmation": True,
            "route_structure_resume_check": "integration",
        },
        config,
    )
    confirmed = graph.invoke(Command(resume="confirm"), config)
    assert confirmed["route_recheck_days"] == [3]
    assert confirmed["poi_reconciliation_original_route_plan"] == original_route

    checked_days = []
    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda project_id, topic, days=None: checked_days.append(("citation", days)) or [],
    )
    citation_result = route_citation_check_node(confirmed)

    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda project_id, topic, requirements=None, days=None: (
            checked_days.append(("timeliness", days)) or RouteFeasibilityReport(overall_status="passed")
        ),
    )
    timeliness_result = route_timeliness_check_node({**confirmed, **citation_result})

    assert checked_days == [("citation", [3]), ("timeliness", [3])]
    assert timeliness_result["route_recheck_days"] is None


def test_plan_proposal_recheck_remains_full_route(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    checked_days = []
    monkeypatch.setattr(
        citation_check,
        "verify_route_citations",
        lambda project_id, topic, days=None: checked_days.append(days) or [],
    )

    route_citation_check_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "mode": "plan",
            "route_recheck_days": [3],
        }
    )

    assert checked_days == [None]


def test_keep_structure_preserves_complete_route_plan(isolated_project):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    store.report_conflict(
        isolated_project,
        actor="citation_checker",
        target="route_agent",
        reason="路线时效核验要求结构调整",
    )
    store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2(poi="人民公园", notes="熊猫基地闭园，保留原安排需现场再确认")],
        trigger="timeliness",
        reason="原POI闭园",
    )
    graph = _confirm_graph()
    config = {"configurable": {"thread_id": "route-structure-keep"}}
    before = store.read_workspace(isolated_project, "route_plan.json")
    graph.invoke(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "pending_route_structure_confirmation": True,
            "route_structure_resume_check": "timeliness",
        },
        config,
    )
    result = graph.invoke(Command(resume="keep"), config)

    route = store.read_workspace(isolated_project, "route_plan.json")
    assert route == before
    assert result["route_timeliness_checked"] is True


def test_keep_feasibility_block_does_not_change_citation_status(isolated_project):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    store.write_route_feasibility(
        isolated_project,
        "citation_checker",
        RouteFeasibilityReport(
            overall_status="blocked",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-closed",
                    day=2,
                    target="熊猫基地",
                    target_type="poi",
                    category="poi_operational_status",
                    status="blocked",
                    summary="长期闭园",
                    requires_structure_change=True,
                )
            ],
        ),
    )
    store.report_conflict(
        isolated_project,
        actor="citation_checker",
        target="route_agent",
        reason="路线可执行性核验要求结构调整",
        issue_type="route_feasibility_blocked",
    )
    store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2(poi="人民公园", notes="熊猫基地闭园，保留需出发前再确认")],
        trigger="timeliness",
        reason="原POI闭园",
    )
    graph = _confirm_graph()
    config = {"configurable": {"thread_id": "route-feasibility-keep"}}
    before = store.read_workspace(isolated_project, "route_plan.json")
    graph.invoke(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "pending_route_structure_confirmation": True,
            "route_structure_resume_check": "timeliness",
        },
        config,
    )

    graph.invoke(Command(resume="keep"), config)

    assert store.read_workspace(isolated_project, "route_plan.json") == before
    report = store.read_route_feasibility(isolated_project)
    assert report.overall_status == "blocked"
    assert report.force_finalized is True


def test_integration_route_conflict_creates_proposal_without_full_route_writer(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.INTEGRATING)
    conflict = store.report_conflict(
        isolated_project,
        actor="integration_agent",
        target="route_agent",
        reason="Day2跨区折返",
    )
    captured = {}

    def fake_request(project_id, topic, **kwargs):
        captured.update(kwargs)
        return {"proposal_id": "proposal-1"}

    monkeypatch.setattr(orchestrator, "_request_route_structure_proposal", fake_request)
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应构建full Route Agent")),
    )
    before = store.read_workspace(isolated_project, "route_plan.json")
    result = route_draft_node(
        {"project_id": isolated_project, "topic": "成都", "requirements": {"destination": "成都", "days": 3}}
    )
    assert captured["trigger"] == "integration_conflict"
    assert captured["source_identity"] == f"conflict:{conflict.id}"
    assert result["pending_route_structure_confirmation"] is True
    assert result["route_structure_resume_check"] == "integration"
    assert store.read_workspace(isolated_project, "route_plan.json") == before


def test_timeliness_structure_issue_goes_to_proposal_not_facts_repair(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda project_id, topic, requirements=None, days=None: RouteFeasibilityReport(
            overall_status="blocked",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-closed",
                    day=2,
                    target="熊猫基地",
                    target_type="poi",
                    category="poi_operational_status",
                    status="blocked",
                    summary="熊猫基地长期闭园，需要更换POI",
                    requires_structure_change=True,
                    evidence=[RouteFeasibilityEvidence(excerpt="官方闭园公告")],
                )
            ],
        ),
    )
    captured = {}

    def fake_request(project_id, topic, **kwargs):
        captured.update(kwargs)
        return {"proposal_id": "proposal-timeliness"}

    monkeypatch.setattr(orchestrator, "_request_route_structure_proposal", fake_request)
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应进入facts_only修复")),
    )
    result = route_timeliness_check_node({"project_id": isolated_project, "topic": "成都"})
    assert captured["trigger"] == "timeliness"
    assert captured["affected_days"] == [2]
    assert captured["source_identity"].startswith("route_feasibility:")
    assert result["pending_route_structure_confirmation"] is True
    assert result["route_timeliness_checked"] is False


def test_structure_proposal_prompt_inlines_all_affected_day_drafts(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    captured = {}
    invoke_count = 0

    def fake_build(*args, **kwargs):
        agent = SimpleNamespace(
            checkpointer=None,
            work_unit_id=kwargs["proposal_work_unit_id"],
        )
        return agent

    monkeypatch.setattr(orchestrator, "build_route_agent", fake_build)

    def fake_invoke(agent, prompt, **kwargs):
        nonlocal invoke_count
        invoke_count += 1
        captured["prompt"] = prompt
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[_candidate_day2(poi="人民公园")],
            trigger="timeliness",
            reason="Day2原POI不可执行",
            evidence="官方公告",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    proposal = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="Day2与Day3需要调整",
        evidence="官方公告",
        affected_days=[2, 3],
        source_identity="route_feasibility:checks-a",
    )
    recovered = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="同一检查的摘要文案发生变化",
        evidence="同一证据的展示文本发生变化",
        affected_days=[2, 3],
        source_identity="route_feasibility:checks-a",
    )

    assert proposal["status"] == "pending"
    assert recovered["proposal_id"] == proposal["proposal_id"]
    assert invoke_count == 1
    assert '"day": 2' in captured["prompt"]
    assert '"day": 3' in captured["prompt"]
    assert "完整提交底稿" in captured["prompt"]
    assert "无需再次调用read_workspace" in captured["prompt"]


def test_structure_proposal_resumes_pending_failed_write_checkpoint(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    captured = {}

    class _PendingAgent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            return SimpleNamespace(values={"messages": ["preserved"]}, next=("pre_model_hook",))

    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: _PendingAgent(kwargs["proposal_work_unit_id"]),
    )
    monkeypatch.setattr(
        orchestrator,
        "clear_agent_thread_family",
        lambda *args, **kwargs: captured.setdefault("cleared_thread", args[1]),
    )
    monkeypatch.setattr(
        orchestrator,
        "reset_web_search_log",
        lambda *args, **kwargs: pytest.fail("pending proposal logs must be preserved"),
    )
    monkeypatch.setattr(
        orchestrator,
        "reset_amap_evidence_log",
        lambda *args, **kwargs: pytest.fail("pending proposal logs must be preserved"),
    )

    def fake_invoke(agent, prompt, **kwargs):
        captured.update(kwargs)
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[_candidate_day2(poi="人民公园")],
            trigger="timeliness",
            reason="Day2原POI不可执行",
            evidence="官方公告",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="Day2需要调整",
        evidence="官方公告",
        affected_days=[2],
        source_identity="route_feasibility:checks-a",
    )

    assert captured["resume_from_checkpoint"] is True
    assert captured["checkpoint_repair_write_tools"] == (
        "write_route_change_proposal",
    )
    assert ":agent:route_structure_proposal:work-" in captured["thread_id"]
    assert "cleared_thread" not in captured


def test_structure_proposal_reuses_only_matching_work_unit_checkpoint(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    seen_thread_ids = []

    class _Agent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            seen_thread_ids.append(config["configurable"]["thread_id"])
            return SimpleNamespace(values={"messages": ["preserved"]}, next=("pre_model_hook",))

    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: _Agent(kwargs["proposal_work_unit_id"]),
    )
    monkeypatch.setattr(orchestrator, "clear_agent_thread_family", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_web_search_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_amap_evidence_log", lambda *args, **kwargs: None)

    def fake_invoke(agent, prompt, **kwargs):
        trigger = (
            "integration_conflict"
            if "触发来源：integration_conflict" in prompt
            else "timeliness"
        )
        changed_days = [
            _candidate_day3() if "Day3需要调整" in prompt else _candidate_day2(poi="人民公园")
        ]
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=changed_days,
            trigger=trigger,
            reason="Day2需要调整",
            evidence="官方公告",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="Day2需要调整",
        evidence="官方公告",
        affected_days=[2],
        source_identity="conflict:source-a",
    )
    first_thread_id = seen_thread_ids[-1]
    store.decide_route_change_proposal(isolated_project, "main_agent", "supersede")

    orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="integration_conflict",
        reason="Day2需要调整",
        evidence="官方公告",
        affected_days=[2],
        source_identity="conflict:source-a",
    )
    second_thread_id = seen_thread_ids[-1]

    store.decide_route_change_proposal(isolated_project, "main_agent", "supersede")
    orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="Day3需要调整",
        evidence="官方公告",
        affected_days=[3],
        source_identity="conflict:source-a",
    )
    third_thread_id = seen_thread_ids[-1]

    store.decide_route_change_proposal(isolated_project, "main_agent", "supersede")
    store.write_route_plan(
        isolated_project,
        "route_agent",
        _plan(day2_poi="锦里"),
    )
    orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="Day2需要调整",
        evidence="官方公告",
        affected_days=[2],
        source_identity="conflict:source-a",
    )
    fourth_thread_id = seen_thread_ids[-1]

    fourth = store.read_route_change_proposal(isolated_project)
    store.decide_route_change_proposal(isolated_project, "main_agent", "supersede")
    orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="Day2需要按用户意见调整",
        evidence="官方公告",
        affected_days=[2],
        replaces_proposal_id=fourth["proposal_id"],
        source_identity="proposal:revision-a",
    )
    revision_thread_id = seen_thread_ids[-1]

    assert first_thread_id != second_thread_id
    assert first_thread_id != third_thread_id
    assert first_thread_id != fourth_thread_id
    assert fourth_thread_id != revision_thread_id

    store.decide_route_change_proposal(isolated_project, "main_agent", "supersede")
    orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="同路线同触发但来自另一条finding",
        evidence="官方公告",
        affected_days=[2],
        source_identity="conflict:source-b",
    )
    unrelated_source_thread_id = seen_thread_ids[-1]

    assert fourth_thread_id != unrelated_source_thread_id


def test_structure_proposal_stable_work_unit_ignores_reason_and_evidence_text_drift(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    built_work_unit_ids = []
    invoke_count = 0

    class _Agent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            return SimpleNamespace(values={}, next=())

    def fake_build(*args, **kwargs):
        built_work_unit_ids.append(kwargs["proposal_work_unit_id"])
        return _Agent(kwargs["proposal_work_unit_id"])

    monkeypatch.setattr(orchestrator, "build_route_agent", fake_build)
    monkeypatch.setattr(orchestrator, "clear_agent_thread_family", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_web_search_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_amap_evidence_log", lambda *args, **kwargs: None)

    def fake_invoke(agent, prompt, **kwargs):
        nonlocal invoke_count
        invoke_count += 1
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[_candidate_day2(poi="人民公园")],
            trigger="timeliness",
            reason="首次摘要",
            evidence="证据A、证据B",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    first = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="首次摘要",
        evidence="证据A、证据B",
        affected_days=[2],
        source_identity="route_feasibility:checks-a",
    )
    second = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="同一问题的另一种摘要措辞",
        evidence="证据B、证据A",
        affected_days=[2],
        source_identity="route_feasibility:checks-a",
    )

    assert len(built_work_unit_ids) == 1
    assert invoke_count == 1
    assert second["proposal_id"] == first["proposal_id"]


def test_route_structure_revision_failure_resumes_from_route_draft_checkpoint(
    isolated_project,
    monkeypatch,
):
    requirements = {"destination": "成都", "days": 3}
    store.write_requirements(isolated_project, "成都", requirements)
    store.write_route_plan(isolated_project, "route_agent", _plan())
    original = store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2(poi="人民公园")],
        trigger="timeliness",
        reason="Day2原POI不可执行",
        evidence="官方公告",
    )
    graph = _revision_recovery_graph()
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: graph)
    attempts = 0

    def flaky_request(project_id, topic, **kwargs):
        nonlocal attempts
        attempts += 1
        assert kwargs["replaces_proposal_id"] == original["proposal_id"]
        assert store.read_route_change_proposal(project_id)["status"] == "superseded"
        if attempts == 1:
            raise RuntimeError("proposal generation interrupted")
        return store.write_route_change_proposal(
            project_id,
            "route_agent",
            changed_days=[_candidate_day2(poi="文殊院")],
            trigger=kwargs["trigger"],
            reason=kwargs["reason"],
            evidence=kwargs["evidence"],
        )

    monkeypatch.setattr(orchestrator, "_request_route_structure_proposal", flaky_request)
    config = {"configurable": {"thread_id": f"{isolated_project}:plan"}}
    first = graph.invoke(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "mode": "plan",
            "requirements": requirements,
            "pending_route_structure_confirmation": True,
            "route_structure_resume_check": "timeliness",
        },
        config,
    )
    assert "__interrupt__" in first

    with pytest.raises(RuntimeError, match="proposal generation interrupted"):
        orchestrator.resume_plan_route_decision(
            isolated_project,
            "revise:换成同一区域且适合拍照的地点",
        )

    failed_state = graph.get_state(config)
    assert failed_state.next == ("route_draft",)
    assert failed_state.values["route_structure_revision_request"] == "换成同一区域且适合拍照的地点"
    assert store.read_route_change_proposal(isolated_project)["status"] == "superseded"

    resumed = orchestrator.run_plan(isolated_project, requirements)

    assert attempts == 2
    assert resumed["status"] == "action_required"
    assert "__interrupt__" in resumed
    replacement = store.read_route_change_proposal(isolated_project)
    assert replacement["status"] == "pending"
    assert replacement["proposal_id"] != original["proposal_id"]
    assert replacement["proposed_days"][0]["pois"][0]["name"] == "文殊院"


def test_structure_proposal_does_not_reuse_pending_file_with_mismatched_work_unit(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    stale = store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2(poi="人民公园")],
        trigger="timeliness",
        reason="旧工作单元",
        evidence="旧证据",
        work_unit_id="unrelated-work-unit",
    )
    calls = []

    class _FreshAgent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            return SimpleNamespace(values={"messages": ["old completed"]}, next=())

    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: _FreshAgent(kwargs["proposal_work_unit_id"]),
    )
    monkeypatch.setattr(orchestrator, "clear_agent_thread_family", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_web_search_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_amap_evidence_log", lambda *args, **kwargs: None)

    def fake_invoke(agent, prompt, **kwargs):
        calls.append(kwargs)
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[_candidate_day2(poi="文殊院")],
            trigger="timeliness",
            reason="当前工作单元",
            evidence="当前证据",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    proposal = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="当前工作单元",
        evidence="当前证据",
        affected_days=[2],
        source_identity="route_feasibility:checks-current",
    )

    assert calls and calls[0]["resume_from_checkpoint"] is False
    assert proposal["proposal_id"] != stale["proposal_id"]


def test_structure_proposal_does_not_reuse_exact_work_unit_without_structure_diff(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    invokes = 0

    class _Agent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            return SimpleNamespace(
                values={"messages": ["pending repair"]},
                next=("pre_model_hook",),
            )

    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: _Agent(kwargs["proposal_work_unit_id"]),
    )
    monkeypatch.setattr(orchestrator, "clear_agent_thread_family", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_web_search_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_amap_evidence_log", lambda *args, **kwargs: None)

    def fake_invoke(agent, prompt, **kwargs):
        nonlocal invokes
        invokes += 1
        changed_day = _candidate_day2(poi="人民公园")
        if invokes == 1:
            changed_day = _candidate_day2(poi="熊猫基地")
            changed_day["notes"] = "只改事实文案，没有结构变化"
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[changed_day],
            trigger="timeliness",
            reason="Day2需要调整",
            evidence="官方公告",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    with pytest.raises(RuntimeError, match="未生成可确认"):
        orchestrator._request_route_structure_proposal(
            isolated_project,
            "成都",
            trigger="timeliness",
            reason="Day2需要调整",
            evidence="官方公告",
            affected_days=[2],
            source_identity="route_feasibility:checks-no-diff",
        )

    proposal = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="同一检查恢复",
        evidence="官方公告",
        affected_days=[2],
        source_identity="route_feasibility:checks-no-diff",
    )

    assert invokes == 2
    assert proposal["proposed_days"][0]["pois"][0]["name"] == "人民公园"


def test_structure_revision_does_not_reuse_unrelated_pending_file_without_lineage_checkpoint(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    source = store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2(poi="人民公园")],
        trigger="timeliness",
        reason="原始候选",
        evidence="官方公告",
    )
    store.decide_route_change_proposal(isolated_project, "main_agent", "supersede")
    unrelated = store.write_route_change_proposal(
        isolated_project,
        "route_agent",
        changed_days=[_candidate_day2(poi="锦里")],
        trigger="timeliness",
        reason="无关候选",
        evidence="另一条证据",
    )
    calls = []

    class _FreshAgent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            return SimpleNamespace(values={}, next=())

    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: _FreshAgent(kwargs["proposal_work_unit_id"]),
    )
    monkeypatch.setattr(orchestrator, "clear_agent_thread_family", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_web_search_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_amap_evidence_log", lambda *args, **kwargs: None)

    def fake_invoke(agent, prompt, **kwargs):
        calls.append(kwargs)
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[_candidate_day2(poi="文殊院")],
            trigger="timeliness",
            reason="按用户修订意见生成",
            evidence="官方公告",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    proposal = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="原始候选需调整",
        evidence="官方公告",
        revision_feedback="换成同一区域且适合拍照的地点",
        affected_days=[2],
        replaces_proposal_id=source["proposal_id"],
        source_identity=f"proposal:{source['proposal_id']}:feedback:photo",
    )

    assert calls and calls[0]["resume_from_checkpoint"] is False
    assert proposal["proposal_id"] != unrelated["proposal_id"]
    assert proposal["proposed_days"][0]["pois"][0]["name"] == "文殊院"


def test_structure_proposal_reuses_exact_persisted_work_unit_without_regeneration(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    cleared = []
    invokes = []

    class _Agent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            return SimpleNamespace(values={}, next=())

    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: _Agent(kwargs["proposal_work_unit_id"]),
    )
    monkeypatch.setattr(
        orchestrator,
        "clear_agent_thread_family",
        lambda project_id, thread_id: cleared.append(thread_id),
    )
    monkeypatch.setattr(
        orchestrator,
        "reset_web_search_log",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "reset_amap_evidence_log", lambda *args, **kwargs: None)

    def fake_invoke(agent, prompt, **kwargs):
        invokes.append(kwargs["thread_id"])
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[_candidate_day2(poi="人民公园")],
            trigger="timeliness",
            reason="当前工作单元",
            evidence="当前证据",
            work_unit_id=agent.work_unit_id,
        )

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)

    first = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="当前工作单元",
        evidence="当前证据",
        affected_days=[2],
        source_identity="route_feasibility:checks-current",
    )
    second = orchestrator._request_route_structure_proposal(
        isolated_project,
        "成都",
        trigger="timeliness",
        reason="同一来源的另一种摘要",
        evidence="证据顺序发生变化",
        affected_days=[2],
        source_identity="route_feasibility:checks-current",
    )

    assert second == first
    assert first["work_unit_id"]
    assert len(invokes) == 1
    assert len(cleared) == 1


def test_structure_proposal_checkpoint_survives_root_node_crash_until_await_node(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    checkpoint_alive = {"value": False}
    invoke_count = 0
    clear_states = []

    class _Agent:
        checkpointer = object()

        def __init__(self, work_unit_id):
            self.work_unit_id = work_unit_id

        def get_state(self, config):
            values = {"messages": ["proposal written"]} if checkpoint_alive["value"] else {}
            return SimpleNamespace(values=values, next=())

    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda *args, **kwargs: _Agent(kwargs["proposal_work_unit_id"]),
    )
    monkeypatch.setattr(orchestrator, "reset_web_search_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "reset_amap_evidence_log", lambda *args, **kwargs: None)

    def fake_clear(project_id, thread_id):
        clear_states.append(checkpoint_alive["value"])
        checkpoint_alive["value"] = False

    monkeypatch.setattr(orchestrator, "clear_agent_thread_family", fake_clear)

    def fake_invoke(agent, prompt, **kwargs):
        nonlocal invoke_count
        invoke_count += 1
        store.write_route_change_proposal(
            isolated_project,
            "route_agent",
            changed_days=[_candidate_day2(poi="文殊院")],
            trigger="timeliness",
            reason="Day2不可执行",
            evidence="官方公告",
            work_unit_id=agent.work_unit_id,
        )
        checkpoint_alive["value"] = True

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", fake_invoke)
    crashed = {"value": False}

    def proposal_node(state):
        proposal = orchestrator._request_route_structure_proposal(
            isolated_project,
            "成都",
            trigger="timeliness",
            reason="Day2不可执行",
            evidence="官方公告",
            affected_days=[2],
            source_identity="route_feasibility:checks-crash",
        )
        if not crashed["value"]:
            crashed["value"] = True
            assert checkpoint_alive["value"] is True
            raise RuntimeError("crash before root checkpoint")
        return {
            "pending_route_structure_confirmation": True,
            "last_agent_result": {
                "agent": "route_agent",
                "summary": proposal["proposal_id"],
            },
        }

    graph_builder = StateGraph(GraphState)
    graph_builder.add_node("proposal", proposal_node)
    graph_builder.add_node("await", await_route_structure_confirm_node)
    graph_builder.add_edge(START, "proposal")
    graph_builder.add_edge("proposal", "await")
    graph_builder.add_edge("await", END)
    graph = graph_builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"{isolated_project}:crash-window"}}

    with pytest.raises(RuntimeError, match="crash before root checkpoint"):
        graph.invoke(
            {
                "project_id": isolated_project,
                "topic": "成都",
                "pending_route_structure_confirmation": False,
            },
            config,
        )

    assert invoke_count == 1
    assert checkpoint_alive["value"] is True

    resumed = graph.invoke(None, config)

    assert "__interrupt__" in resumed
    assert invoke_count == 1
    assert checkpoint_alive["value"] is False
    assert clear_states == [False, True]


def test_timeliness_structure_issue_preserves_integration_return_phase(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, "route_agent", _plan())
    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda project_id, topic, requirements=None, days=None: RouteFeasibilityReport(
            overall_status="blocked",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-closed",
                    day=2,
                    target="熊猫基地",
                    target_type="poi",
                    category="poi_operational_status",
                    status="blocked",
                    summary="需要更换POI",
                    requires_structure_change=True,
                    evidence=[RouteFeasibilityEvidence(excerpt="官方公告")],
                )
            ],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_request_route_structure_proposal",
        lambda *args, **kwargs: {"proposal_id": "nested-proposal"},
    )
    result = route_timeliness_check_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "route_structure_resume_check": "integration",
            "awaiting_integration_recheck": True,
        }
    )
    assert result["route_structure_resume_check"] == "integration"


def test_timeliness_keeps_structure_issue_when_notes_already_describe_risk(isolated_project, monkeypatch):
    plan = _plan()
    plan.days[1].notes = "沿G227前往景区；官方已公告闭园，但路线仍保留该POI"
    store.write_route_plan(isolated_project, "route_agent", plan)

    class FakeWebTool:
        def invoke(self, payload):
            return "官方公告：景区长期闭园"

    monkeypatch.setattr(timeliness_check, "make_web_search_tools", lambda *args, **kwargs: [FakeWebTool()])
    monkeypatch.setattr(
        timeliness_check,
        "_judge_timeliness",
        lambda notes, evidence: timeliness_check.TimelinessVerdict(
            has_timeliness_info=True,
            missing_in_notes=[],
            reason="notes已写明闭园，但路线仍不可执行",
            requires_structure_change=True,
            structure_change_reason="需要替换闭园POI",
        ),
    )
    issues = timeliness_check.verify_route_timeliness(isolated_project, "成都")
    assert len(issues) == 1
    assert issues[0]["requires_structure_change"] is True


def test_timeliness_web_query_uses_current_year():
    from datetime import datetime, timezone

    assert str(datetime.now(timezone.utc).year) in timeliness_check._build_web_query(["G227"])


def test_route_draft_retries_when_requested_day_count_is_wrong(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda *args, **kwargs: object())
    calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        calls["n"] += 1
        plan = _plan()
        if calls["n"] == 1:
            plan.days = plan.days[:2]
        store.write_route_plan(isolated_project, "route_agent", plan)
        return {"messages": messages}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    result = route_draft_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "requirements": {"destination": "成都", "days": 3},
        }
    )
    assert calls["n"] == 2
    assert result["route_hard_constraints_blocked"] is False


def test_revision_constraint_delta_only_removes_named_place():
    requirements = {
        "destination": "成都",
        "days": 3,
        "must_visit": [{"name": "都江堰", "day": 2}, {"name": "熊猫基地", "day": 3}],
    }
    updated, changes = orchestrator._apply_skeleton_revision_constraint_delta(
        requirements, "取消都江堰，熊猫基地仍然保留"
    )
    assert updated["must_visit"] == [{"name": "熊猫基地", "day": 3}]
    assert changes == ["取消必去地点都江堰"]
