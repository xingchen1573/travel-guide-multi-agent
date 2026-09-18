"""graph级别测试：驱动真实build_graph()编译出的完整生产图，只mock掉agent构建函数
（build_route_agent/build_integration_agent/build_poi_detail_agent）和真正调用LLM的
invoke_with_retry，保留真实的StateGraph节点/条件边——填补docs/CLAUDE.md已知问题第6条
提到的缺口：存量的节点单测普遍是"孤立调用单个节点函数、手工构造下一个节点需要的输入"，
从没有真正验证过"图接线本身对不对"（main_agent_router的条件边是否真的按依赖表接对了、
Send fan-out/join在完整图里的真实调度+归约行为）。

跟tests/test_orchestrator_skeleton_confirm.py的最小图思路刻意不同：那边是"只测一个节点
自身的interrupt/resume契约"，特意不引入build_graph()以避免要mock一整条下游pipeline；
这里恰恰相反，要验证的正是"多个节点之间通过main_agent_router真实路由连起来是否正确"，
必须用生产用的build_graph()本身，不能自建局部小图（那样反而绕开了要测的东西）。

两个场景都是历史上只在真实E2E/真实事故里出现过、从未被mock测试网覆盖过的多节点路径：
1. integration_agent报冲突（target=route_agent）-> 路由打回route_draft重新生成 ->
   _route_integration_phase的awaiting_integration_recheck强制先回integration_agent_node
   复核（不能直接再打回一次）-> 冲突resolve -> 推进到下一阶段。这条链路正是
   docs/ISSUES.md第32条真实死循环事故（_route_integration_phase标记的"该强制终结"信号
   从未传递给integration_agent_node）背后涉及的路由结构，此前完全没有graph级别验证过。
2. POI详情agent的Send fan-out/join：tests/test_orchestrator_poi_details.py只验证了
   _route_poi_detail_phase/poi_detail_agent_node/poi_details_join_node三个函数各自的
   行为（手工构造Send列表、手工拼接poi_detail_results），从没有真正让LangGraph自己完成
   Send派发+Annotated[list[dict], operator.add]归约+静态边join这一整套机制。
"""
import pytest
from langgraph.types import Command

from src import orchestrator
from src.materials import citation_check
from src.orchestrator import build_graph
from src.workspace import store
from src.workspace.schema import (
    LIFESTYLE_FIELDS,
    PoiDetailFile,
    PoiDetailManifest,
    PoiDetailManifestEntry,
    ProjectPhase,
    RouteDay,
    RouteFeasibilityReport,
    RouteOption,
    RoutePlan,
    RoutePoi,
    RouteSegment,
)

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda project_id, topic, requirements=None, days=None: RouteFeasibilityReport(overall_status="passed"),
    )
    project_id = "graph-level-test"
    store.init_project(project_id)
    return project_id


def _base_initial_state(project_id: str, days: int = 1) -> dict:
    """跳过material_agent/requirements/skeleton_confirm这几个前置阶段（本次测试要验证
    的是后半段的路由，前置阶段已经有独立的单测覆盖），靠material_checked=True + 提前
    写好progress.json/route_plan.json来让main_agent_router从正确的phase继续。"""
    return {
        "project_id": project_id,
        "topic": "成都",
        "mode": "plan",
        "requirements": {"destination": "成都", "days": days},
        "material_checked": True,
    }


def _write_valid_final_guide(project_id: str) -> None:
    """写入一份满足交付契约的最小测试攻略。"""
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json") or {}

    days = [day.get("day") for day in route_plan.get("days", []) if isinstance(day.get("day"), int)]
    merged_fields = ["route_plan"] if days else []
    for field in LIFESTYLE_FIELDS:
        if (lifestyle.get(field) or {}).get("items"):
            merged_fields.append(f"lifestyle.{field}")
    if any(
        str(item.get("warning", "")).strip()
        for field in LIFESTYLE_FIELDS
        for item in (pitfall.get(field) or {}).get("items", [])
        if isinstance(item, dict)
    ):
        merged_fields.append("pitfall")

    day_sections = "\n\n".join(f"## Day {day}\n行程安排与实用建议" for day in days)
    store.write_final_guide(
        project_id,
        actor="integration_agent",
        content_md=f"# 成都攻略\n\n## 行前必备清单\n证件与药品\n\n{day_sections}\n\n" + "正文内容" * 50,
        meta={"merged_fields": merged_fields, "checklist_included": True},
    )


def _write_complete_lifestyle_day(project_id: str, day: int = 1) -> None:
    """模拟 Lifestyle 当天工作单元的一次完整落盘。"""
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": field,
                "day": day,
                "recommendation": f"{field}建议",
                "sources": [],
                "claimed_entities": [],
            }
            for field in LIFESTYLE_FIELDS
        ],
    )


# ---------------------------------------------------------------------------
# 场景1：integration报冲突打回route_draft -> 强制先回integration复核 -> 冲突resolve
# ---------------------------------------------------------------------------


def test_integration_conflict_bounces_to_route_draft_then_back_for_recheck(isolated_project, monkeypatch):
    """route被target=route_agent的冲突打回重新生成后，2026-07-26修复：会连带重置
    lifestyle_agent_done/pitfall_agent_done（docs/ISSUES.md第32条已知局限②），所以
    这条级联比修复前多两跳——route_draft->lifestyle_agent_node->pitfall_agent_node->
    integration_agent_node复核，而不是直接route_draft->integration_agent_node。"""
    store.write_route_plan(
        isolated_project,
        actor="route_agent",
        route_plan=RoutePlan(days=[RouteDay(day=1, city="成都", area="武侯区", pois=[RoutePoi(name="武侯祠")])]),
    )
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.INTEGRATING)
    conflict = store.report_conflict(
        isolated_project,
        actor="citation_checker",
        target="route_agent",
        reason="路线引用核验未通过：武侯祠缺失依据",
    )

    route_agent_calls = {"n": 0}
    lifestyle_agent_calls = {"n": 0}
    pitfall_agent_calls = {"n": 0}
    integration_agent_calls = {"n": 0}

    def fake_structure_proposal(project_id, topic, **kwargs):
        route_agent_calls["n"] += 1
        current = store.read_route_plan(project_id)
        changed = current.days[0].model_dump(mode="json")
        changed["area"] = "锦江区"
        return store.write_route_change_proposal(
            project_id,
            "route_agent",
            changed_days=[changed],
            trigger=kwargs["trigger"],
            reason=kwargs["reason"],
            evidence=kwargs.get("evidence", ""),
        )

    def fake_build_lifestyle_agent(project_id, topic, **kwargs):
        lifestyle_agent_calls["n"] += 1
        return object()

    def fake_build_pitfall_agent(project_id, topic, **kwargs):
        pitfall_agent_calls["n"] += 1
        return object()

    def fake_build_integration_agent(project_id, topic, **kwargs):
        integration_agent_calls["n"] += 1
        return object()

    def fake_invoke(agent, messages, **kwargs):
        # 用prompt内容区分当前是哪个节点在调用——多个节点共享同一个被monkeypatch的
        # invoke_with_retry，靠文案里的关键句子消歧义，跟tests/test_orchestrator_poi_details.py
        # 靠调用次数消歧义是同一类技巧的变体。
        prompt = messages[0]["content"] if messages else ""
        if "请检查两者是否存在有明确上游依据的跨模块冲突" in prompt:
            _write_valid_final_guide(isolated_project)
        elif "请结合素材库检索结果" in prompt and "food、outfit、lodging、play" in prompt:
            _write_complete_lifestyle_day(isolated_project)
        elif "当天衣食住行推荐：" in prompt and "warning留空" in prompt:
            _write_empty_pitfall_for_lifestyle(isolated_project)
        # route_draft_node的prompt分支不需要额外动作：route_plan.json已经是有效内容，
        # verify_fn (`bool(route_plan.get("days"))`) 不需要新的写入就能通过。
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "_request_route_structure_proposal", fake_structure_proposal)
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", fake_build_lifestyle_agent)
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", fake_build_pitfall_agent)
    monkeypatch.setattr(orchestrator, "build_integration_agent", fake_build_integration_agent)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", lambda project_id, topic, fields=None, item_keys=None: [])
    monkeypatch.setattr(citation_check, "verify_pitfall_citations", lambda project_id, topic, fields=None, item_keys=None: [])
    monkeypatch.setattr(citation_check, "verify_route_citations", lambda project_id, topic, days=None: [])
    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda project_id, topic, requirements=None, days=None: RouteFeasibilityReport(overall_status="passed"),
    )

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:graph-test-integration"}}

    result = graph.invoke(_base_initial_state(isolated_project), config)
    assert "__interrupt__" in result
    assert store.read_route_plan(isolated_project).days[0].area == "武侯区"
    result = graph.invoke(Command(resume="confirm"), config)

    assert "__interrupt__" not in result
    # 真正走过route_draft(重新生成骨架)、lifestyle_agent_node、pitfall_agent_node
    # （被route_draft_node重置的下游得到重新执行的机会）、integration_agent_node(复核)
    # 各一次，不是被路由函数短路跳过、也没有因为路由错误重复调用。
    assert route_agent_calls["n"] == 1
    assert lifestyle_agent_calls["n"] == 1
    assert pitfall_agent_calls["n"] == 1
    assert integration_agent_calls["n"] == 1

    # 没有needs_detail的POI，"树木生成中"阶段直接跳过，最终推进到"完成"。
    assert store.read_progress(isolated_project).state == ProjectPhase.DONE

    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert len(conflicts) == 1
    assert conflicts[0]["id"] == conflict.id
    assert conflicts[0]["status"] == "resolved"

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "正文内容" in guide
    assert "参考来源" in guide  # _replace_reference_section程序追加的章节，证明integration真的跑完了


def test_integration_recheck_bounces_back_twice_then_terminates(isolated_project, monkeypatch):
    """"强制先回integration复核"这一步每次只弹回一次route_draft，不会重复消耗——
    第一轮复核发现问题依旧存在（integration自己重新report_conflict）时，应该正常走
    _route_integration_phase的常规路由（不是被awaiting_integration_recheck卡住反复
    弹回同一节点），再打回route_draft一次；第二轮修复真正成功后终结。这条"打回->
    复核->仍不通过->再打回->复核->通过"的两轮级联，正是docs/ISSUES.md第32条真实死循环
    事故涉及的路由结构，此前从未被graph级别测试验证过实际是否会正确收敛而不是卡住。

    2026-07-26修复后：每轮route_draft重新生成都会连带重置lifestyle_agent_done/
    pitfall_agent_done，所以每轮"打回route_draft"和"回integration复核"之间都会
    多经过lifestyle_agent_node/pitfall_agent_node各一次（两轮共各调用2次）。

    注意：只有"当前最新的pending冲突"（store.get_pending_conflict返回列表最后一条）会
    被真正resolve——第一轮那条更早的冲突会一直留在conflicts.jsonl里保持pending状态，
    这是store.get_pending_conflict"只看最新一条"这个既有实现的真实副作用，不是本次
    测试要修的bug，这里如实断言这个现象。"""
    store.write_route_plan(
        isolated_project,
        actor="route_agent",
        route_plan=RoutePlan(days=[RouteDay(day=1, city="成都", area="武侯区", pois=[RoutePoi(name="武侯祠")])]),
    )
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.INTEGRATING)
    original_conflict = store.report_conflict(
        isolated_project, actor="citation_checker", target="route_agent", reason="路线引用核验未通过",
    )

    route_agent_calls = {"n": 0}
    lifestyle_agent_calls = {"n": 0}
    pitfall_agent_calls = {"n": 0}
    integration_agent_calls = {"n": 0}
    integration_prompt_calls = {"n": 0}

    def fake_structure_proposal(project_id, topic, **kwargs):
        route_agent_calls["n"] += 1
        current = store.read_route_plan(project_id)
        changed = current.days[0].model_dump(mode="json")
        changed["area"] = f"修复区域{route_agent_calls['n']}"
        return store.write_route_change_proposal(
            project_id,
            "route_agent",
            changed_days=[changed],
            trigger=kwargs["trigger"],
            reason=kwargs["reason"],
            evidence=kwargs.get("evidence", ""),
        )

    monkeypatch.setattr(orchestrator, "_request_route_structure_proposal", fake_structure_proposal)
    monkeypatch.setattr(
        orchestrator, "build_lifestyle_agent",
        lambda project_id, topic, **kwargs: (lifestyle_agent_calls.__setitem__("n", lifestyle_agent_calls["n"] + 1), object())[1],
    )
    monkeypatch.setattr(
        orchestrator, "build_pitfall_agent",
        lambda project_id, topic, **kwargs: (pitfall_agent_calls.__setitem__("n", pitfall_agent_calls["n"] + 1), object())[1],
    )
    monkeypatch.setattr(
        orchestrator, "build_integration_agent",
        lambda project_id, topic, **kwargs: (integration_agent_calls.__setitem__("n", integration_agent_calls["n"] + 1), object())[1],
    )

    def fake_invoke(agent, messages, **kwargs):
        prompt = messages[0]["content"] if messages else ""
        if "请检查两者是否存在有明确上游依据的跨模块冲突" in prompt:
            integration_prompt_calls["n"] += 1
            if integration_prompt_calls["n"] == 1:
                # 第一轮复核：修复不完整，模型诚实地发现问题依旧存在，重新report一条新冲突。
                store.report_conflict(
                    isolated_project, actor="integration_agent", target="route_agent", reason="修复后依然缺失依据",
                )
            else:
                # 第二轮复核：这次真的修好了，写出最终攻略。
                _write_valid_final_guide(isolated_project)
        elif "请结合素材库检索结果" in prompt and "food、outfit、lodging、play" in prompt:
            _write_complete_lifestyle_day(isolated_project)
        elif "当天衣食住行推荐：" in prompt and "warning留空" in prompt:
            _write_empty_pitfall_for_lifestyle(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(citation_check, "verify_lifestyle_citations", lambda project_id, topic, fields=None, item_keys=None: [])
    monkeypatch.setattr(citation_check, "verify_pitfall_citations", lambda project_id, topic, fields=None, item_keys=None: [])
    monkeypatch.setattr(citation_check, "verify_route_citations", lambda project_id, topic, days=None: [])
    monkeypatch.setattr(
        orchestrator.route_feasibility_check,
        "verify_route_feasibility",
        lambda project_id, topic, requirements=None, days=None: RouteFeasibilityReport(overall_status="passed"),
    )

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:graph-test-integration-2"}}
    result = graph.invoke(_base_initial_state(isolated_project), config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume="confirm"), config)
    assert "__interrupt__" in result
    result = graph.invoke(Command(resume="confirm"), config)

    assert "__interrupt__" not in result
    # 两轮"打回route_draft -> lifestyle_agent_node -> pitfall_agent_node -> 强制回
    # integration复核"的完整级联，各自被调用2次（不多不少：不会因为路由bug额外多打回
    # 一次，也不会卡在同一节点原地打转）。
    assert route_agent_calls["n"] == 2
    assert lifestyle_agent_calls["n"] == 2
    assert pitfall_agent_calls["n"] == 2
    assert integration_agent_calls["n"] == 2
    assert integration_prompt_calls["n"] == 2

    assert store.read_progress(isolated_project).state == ProjectPhase.DONE
    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "正文内容" in guide

    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert len(conflicts) == 2
    assert conflicts[0]["id"] == original_conflict.id
    assert conflicts[0]["status"] == "resolved"
    assert conflicts[1]["status"] == "resolved"


# ---------------------------------------------------------------------------
# 场景2：POI详情agent的Send fan-out/join在完整图里的真实调度
# ---------------------------------------------------------------------------


def _fake_poi_write(project_id: str, day: int, poi_name: str) -> None:
    detail = PoiDetailFile(
        poi_name=poi_name,
        day=day,
        routes=[
            RouteOption(
                route_name="经典线",
                recommended=True,
                route_segments=[
                    RouteSegment(order=1, point_name="山门", highlight="起点", sources=[]),
                    RouteSegment(order=2, point_name="核心景点", highlight="必看", sources=[]),
                ],
            )
        ],
    )
    store.write_poi_detail(project_id, actor="poi_detail_agent", poi_detail=detail)


def test_poi_detail_send_fanout_and_join_in_full_graph(isolated_project, monkeypatch):
    store.write_route_plan(
        isolated_project,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(day=1, city="成都", pois=[RoutePoi(name="青城山", needs_detail=True, needs_detail_reason="大型景区")]),
                RouteDay(day=2, city="成都", pois=[RoutePoi(name="都江堰", needs_detail=True, needs_detail_reason="大型景区")]),
            ]
        ),
    )
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都2日游\n正文内容", meta={})
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.POI_DETAIL)

    def fake_build_poi_detail_agent(project_id, topic, day, poi_name):
        # 用一个携带身份信息的普通dict当"agent"，供fake_invoke区分当前是哪个并行分支
        # （两个Send分支各自独立执行，看不到彼此的state，只能靠agent对象本身消歧义）。
        return {"day": day, "poi_name": poi_name}

    def fake_invoke(agent, messages, **kwargs):
        _fake_poi_write(isolated_project, agent["day"], agent["poi_name"])
        return fake_agent_reply(messages)

    def fake_verify(detail, topic, route_names=None, **kwargs):
        # "青城山"分支模拟核验一直不通过（耗尽MAX_RETRY后force_finalized），
        # "都江堰"分支模拟一次通过——验证两个并行分支互不影响。
        if detail.get("poi_name") == "青城山":
            return [
                {
                    "route_name": "经典线", "order": 1, "point_name": "山门",
                    "label": "经典线.途经点1「山门」", "chunk_ids": [],
                    "missing_entities": ["山门"], "reason": "原文没有提到",
                }
            ]
        return []

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", fake_build_poi_detail_agent)
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", fake_verify)
    # 素材缺口自动补充采集(docs/ARCHITECTURE.md第16节)不是这次要验证的行为，显式禁用，
    # 避免测试意外依赖真实素材库状态或触发真实网络调用。
    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", lambda *a, **k: [])

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:graph-test-poi"}}

    result = graph.invoke(_base_initial_state(isolated_project, days=2), config)

    assert "__interrupt__" not in result
    assert store.read_progress(isolated_project).state == ProjectPhase.DONE
    assert result["last_agent_result"]["agent"] == "poi_detail_agent"

    manifest = store.read_workspace(isolated_project, "outputs/poi_details/manifest.json")
    status_by_name = {e["poi_name"]: e["citation_status"] for e in manifest["generated"]}
    assert status_by_name == {"青城山": "force_finalized", "都江堰": "passed"}

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "青城山" in guide
    assert "都江堰" in guide
    assert "山门" in guide  # "都江堰"(passed)分支的途经点摘要被正确拼接进final_guide


def test_poi_detail_phase_skips_fanout_entirely_when_no_targets_in_full_graph(isolated_project):
    """没有任何needs_detail=true的POI时，完整图应该直接从"树木生成中"跳到"完成"，
    不应该产生任何poi_detail_agent_node的Send派发（不调用任何agent构建/LLM函数）。"""
    store.write_route_plan(
        isolated_project,
        actor="route_agent",
        route_plan=RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="宽窄巷子", needs_detail=False)])]),
    )
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都1日游\n正文内容", meta={})
    store.update_progress(isolated_project, actor="main_agent", state=ProjectPhase.POI_DETAIL)

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:graph-test-poi-skip"}}
    result = graph.invoke(_base_initial_state(isolated_project), config)

    assert "__interrupt__" not in result
    assert store.read_progress(isolated_project).state == ProjectPhase.DONE
    assert store.read_workspace(isolated_project, "outputs/poi_details/manifest.json") is None


# ---------------------------------------------------------------------------
# 场景3：edit模式三种入口（route_draft/lifestyle_agent_node/pitfall_agent_node）
# 在完整图里的真实路由级联——填补docs/CLAUDE.md已知问题第6条"仍未覆盖"清单里点名的
# 缺口。此前`_route_edit_mode`的路由决策只在`tests/test_orchestrator_edit_mode.py`里
# 用手工构造的state dict直接调用函数验证过，从没有真正让`build_graph()`编译出的完整图
# 驱动多个节点接力执行——尤其是docs/ISSUES.md第46条描述的具体bug（route接入edit入口后，
# "citation_checker"消歧义如果退化成旧的flag顺序推断heuristic，会导致pitfall_agent在
# route→lifestyle级联场景下被静默跳过），这次在graph级别真实复现验证。
#
# 为了让测试聚焦在"路由级联对不对"而不是"核验逻辑本身对不对"（那是test_materials.py的
# 职责范围），这里直接mock掉citation_check.verify_route_citations/verify_lifestyle_citations/
# verify_pitfall_citations让核验永远零成本通过——跟tests/test_orchestrator_core_nodes.py
# 里"只测路由分支"的用例是同一个思路。
# ---------------------------------------------------------------------------


def _edit_initial_state(project_id: str, entry_target: str, edit_request: str, days: int = 2) -> dict:
    return {
        "project_id": project_id,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": days},
        "entry_target": entry_target,
        "edit_request": edit_request,
    }


def _seed_edit_route_plan(project_id: str) -> None:
    store.write_route_plan(
        project_id,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(day=1, city="成都", area="市区", pois=[RoutePoi(name="武侯祠")]),
                RouteDay(day=2, city="成都", area="老城区", pois=[RoutePoi(name="锦里")]),
            ]
        ),
    )


def _seed_edit_lifestyle(project_id: str) -> None:
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {"field": f, "day": 1, "recommendation": "原始美食推荐", "sources": [], "claimed_entities": []}
            for f in ["food", "outfit", "lodging", "play"]
        ],
    )


def _seed_edit_pitfall(project_id: str) -> None:
    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "原始美食推荐", "warning": ""}],
    )


def _write_empty_pitfall_for_lifestyle(project_id: str) -> None:
    """模拟正式Agent完整检查：每个现有Lifestyle键都提交一条空warning。"""
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
    items = [
        {
            "field": field,
            "day": item["day"],
            "target_recommendation": item["recommendation"],
            "warning": "",
        }
        for field in LIFESTYLE_FIELDS
        for item in lifestyle.get(field, {}).get("items", [])
    ]
    store.write_pitfall_output_patch(project_id, "pitfall_agent", items)


def test_edit_route_entry_cascades_through_lifestyle_and_pitfall_to_integration(isolated_project, monkeypatch):
    """docs/ISSUES.md第46条真实bug场景的graph级别复现：route_draft作为edit入口完成后，
    应该真实级联route_citation_check_node -> lifestyle_agent_node -> citation_check_node ->
    pitfall_agent_node -> pitfall_citation_check_node -> integration_agent_node，全程不静默
    跳过任何一个agent——尤其是pitfall_agent，这正是当初真实E2E（route-edit-e2e-v1）验证过、
    但从没有mock graph级别测试保护过的路径。"""
    _seed_edit_route_plan(isolated_project)
    store.write_poi_detail(
        isolated_project,
        actor="poi_detail_agent",
        poi_detail=PoiDetailFile(poi_name="锦里", day=2, routes=[]),
    )
    store.write_poi_details_manifest(
        isolated_project,
        actor="main_agent",
        manifest=PoiDetailManifest(
            generated=[
                PoiDetailManifestEntry(
                    poi_name="锦里",
                    day=2,
                    file="day2_锦里.json",
                    citation_status="passed",
                )
            ]
        ),
    )

    calls = {"route": 0, "lifestyle": 0, "pitfall": 0, "integration": 0}

    def _counted(key):
        def _build(*args, **kwargs):
            calls[key] += 1
            return object()

        return _build

    monkeypatch.setattr(orchestrator, "build_route_agent", _counted("route"))
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", _counted("lifestyle"))
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", _counted("pitfall"))
    monkeypatch.setattr(orchestrator, "build_integration_agent", _counted("integration"))

    def fake_invoke(agent, messages, **kwargs):
        prompt = messages[0]["content"] if messages else ""
        if "调用write_route_plan_patch时只需要提交你新生成/修改的那几天" in prompt:
            # route_draft的定点编辑分支：只patch day2，day1自动原样保留
            store.write_route_plan_patch(
                isolated_project,
                "route_agent",
                [{"day": 2, "city": "成都", "area": "都江堰", "pois": [{"name": "都江堰景区"}]}],
            )
        elif "请结合素材库检索结果" in prompt and "food、outfit、lodging、play" in prompt:
            # lifestyle_agent_node被级联触发，并按同角色的逐日工作单元重新生成。
            day = 1 if "只处理Day1" in prompt else 2
            _write_complete_lifestyle_day(isolated_project, day=day)
        elif "当天衣食住行推荐：" in prompt and "warning留空" in prompt:
            # pitfall_agent_node同样被级联触发
            _write_empty_pitfall_for_lifestyle(isolated_project)
        elif "请检查两者是否存在有明确上游依据的跨模块冲突" in prompt:
            _write_valid_final_guide(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(citation_check, "verify_route_citations", lambda project_id, topic, days=None: [])
    monkeypatch.setattr(
        citation_check, "verify_lifestyle_citations",
        lambda project_id, topic, fields=None, item_keys=None: [],
    )
    monkeypatch.setattr(
        citation_check, "verify_pitfall_citations",
        lambda project_id, topic, fields=None, item_keys=None: [],
    )

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:edit-graph-route"}}
    result = graph.invoke(
        _edit_initial_state(isolated_project, "route_draft", "把第二天改成去都江堰"), config
    )

    assert "__interrupt__" not in result
    assert calls["route"] == 1
    assert calls["lifestyle"] == 2  # 两天各一个同角色工作单元，没有被跳过
    assert calls["pitfall"] == 2  # 两天各一个同角色工作单元，且没有被静默跳过
    assert calls["integration"] == 1

    route_plan = store.read_workspace(isolated_project, "route_plan.json")
    assert route_plan["days"][0]["area"] == "市区"  # day1未被误改
    assert route_plan["days"][1]["area"] == "都江堰"  # day2按要求改动
    assert result["edit_changed_route_days"] == [2]

    assert result["last_agent_result"]["agent"] == "integration_agent"
    assert store.read_progress(isolated_project).state == ProjectPhase.DONE
    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "正文内容" in guide
    assert store.read_poi_detail(isolated_project, 2, "锦里") is None
    assert store.list_poi_details(isolated_project) == []
    archived = list(
        (store.workspace_dir(isolated_project) / "outputs" / "poi_details" / "_stale").glob(
            "*_day2_锦里.json"
        )
    )
    assert len(archived) == 1
    # 全程没有产生任何误改/核验冲突——干净的级联，不是打回重试凑出来的结果
    assert not (store.read_workspace(isolated_project, "conflicts.jsonl") or [])


def test_edit_lifestyle_entry_cascades_to_pitfall_and_integration(isolated_project, monkeypatch):
    """lifestyle_agent_node作为edit入口（用户直接@的定点修改），完成后应该级联到
    pitfall_agent_node（依赖表downstream）再到integration_agent_node，全程不跳过pitfall。"""
    _seed_edit_route_plan(isolated_project)
    _seed_edit_lifestyle(isolated_project)

    calls = {"route": 0, "lifestyle": 0, "pitfall": 0, "integration": 0}

    def _counted(key):
        def _build(*args, **kwargs):
            calls[key] += 1
            return object()

        return _build

    monkeypatch.setattr(orchestrator, "build_route_agent", _counted("route"))
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", _counted("lifestyle"))
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", _counted("pitfall"))
    monkeypatch.setattr(orchestrator, "build_integration_agent", _counted("integration"))

    def fake_invoke(agent, messages, **kwargs):
        prompt = messages[0]["content"] if messages else ""
        if "请判断这个要求具体涉及food/outfit/lodging/play中的哪个字段、哪一天" in prompt:
            # lifestyle_agent_node的定点编辑分支：只提交food字段的改动，其余字段不提交、
            # 由store层自动保留原样
            store.write_agent_output_patch(
                isolated_project,
                "lifestyle_agent",
                [{"field": "food", "day": 1, "recommendation": "换成甜品店", "sources": [], "claimed_entities": []}],
            )
        elif "当天衣食住行推荐：" in prompt and "warning留空" in prompt:
            _write_empty_pitfall_for_lifestyle(isolated_project)
        elif "请检查两者是否存在有明确上游依据的跨模块冲突" in prompt:
            _write_valid_final_guide(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(
        citation_check, "verify_lifestyle_citations",
        lambda project_id, topic, fields=None, item_keys=None: [],
    )
    monkeypatch.setattr(
        citation_check, "verify_pitfall_citations",
        lambda project_id, topic, fields=None, item_keys=None: [],
    )

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:edit-graph-lifestyle"}}
    result = graph.invoke(
        _edit_initial_state(isolated_project, "lifestyle_agent_node", "Day1午餐换一家甜品店"), config
    )

    assert "__interrupt__" not in result
    assert calls["route"] == 0  # route没有被触碰，不应该被误触发
    assert calls["lifestyle"] == 1
    assert calls["pitfall"] == 1  # 关键：pitfall_agent级联被正确调用，没有跳过
    assert calls["integration"] == 1
    assert result["edit_changed_lifestyle_fields"] == ["food"]  # 只改了1个字段，没有误改检测

    assert result["last_agent_result"]["agent"] == "integration_agent"
    assert store.read_progress(isolated_project).state == ProjectPhase.DONE
    assert not (store.read_workspace(isolated_project, "conflicts.jsonl") or [])


def test_edit_pitfall_entry_goes_directly_to_integration_without_cascading_upstream(isolated_project, monkeypatch):
    """pitfall_agent_node作为edit入口——它在AGENT_DEPENDENCY_TABLE里downstream=[]（没有
    下游），完成核验后应该直接进整合阶段，不应该反过来触发route_agent/lifestyle_agent
    重新生成（依赖表是单向的，不存在"下游反过来触发上游"这回事）。"""
    _seed_edit_route_plan(isolated_project)
    _seed_edit_lifestyle(isolated_project)
    _seed_edit_pitfall(isolated_project)

    calls = {"route": 0, "lifestyle": 0, "pitfall": 0, "integration": 0}

    def _counted(key):
        def _build(*args, **kwargs):
            calls[key] += 1
            return object()

        return _build

    monkeypatch.setattr(orchestrator, "build_route_agent", _counted("route"))
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", _counted("lifestyle"))
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", _counted("pitfall"))
    monkeypatch.setattr(orchestrator, "build_integration_agent", _counted("integration"))

    def fake_invoke(agent, messages, **kwargs):
        prompt = messages[0]["content"] if messages else ""
        if "只针对相关的具体推荐" in prompt:
            # pitfall_agent_node的定点编辑分支：只提交play字段的patch，food不提交、
            # 由store层自动保留原样
            store.write_pitfall_output_patch(
                isolated_project,
                "pitfall_agent",
                [{"field": "play", "day": 1, "target_recommendation": "杜甫草堂", "warning": "部分游客反馈排队较久"}],
            )
        elif "请检查两者是否存在有明确上游依据的跨模块冲突" in prompt:
            _write_valid_final_guide(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(
        citation_check, "verify_pitfall_citations",
        lambda project_id, topic, fields=None, item_keys=None: [],
    )

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:edit-graph-pitfall"}}
    result = graph.invoke(
        _edit_initial_state(isolated_project, "pitfall_agent_node", "给杜甫草堂补充避雷内容"), config
    )

    assert "__interrupt__" not in result
    assert calls["route"] == 0  # 依赖表是单向的，pitfall没有下游，不应该反向触发route
    assert calls["lifestyle"] == 0  # 同上，不应该反向触发lifestyle
    assert calls["pitfall"] == 1
    assert calls["integration"] == 1  # pitfall直接feeds_integration，不需要经过lifestyle
    assert result["edit_changed_pitfall_fields"] == ["play"]

    assert result["last_agent_result"]["agent"] == "integration_agent"
    assert store.read_progress(isolated_project).state == ProjectPhase.DONE
    assert not (store.read_workspace(isolated_project, "conflicts.jsonl") or [])


# ---------------------------------------------------------------------------
# 场景4：误改检测触发report_conflict后再级联——填补CLAUDE.md已知问题第6条/
# docs/ISSUES.md第56条已知局限第2点点名的缺口。场景3的三个edit入口级联测试都刻意
# 只改1个字段/1天，确保len(changed)<=1不触发误改检测（"干净级联"），从未真实驱动过
# "误改->report_conflict->_route_integration_phase打回->强制复核->resolve"这条支线。
# ---------------------------------------------------------------------------


def test_lifestyle_misedit_conflict_bounces_back_and_resolves_in_full_graph(isolated_project, monkeypatch):
    """场景：lifestyle_agent_node作为edit入口，用户只要求改Day1午餐（food字段），但第一次
    生成越权多改了outfit字段——触发lifestyle_agent_node自己的误改检测（>1字段视为可疑）
    report_conflict(target="lifestyle_agent.food")。这条冲突不会被citation_check_node
    处理（它核验的是引用真实性，不是越权范围），要等级联走完pitfall_agent才会被
    _route_integration_phase读到并弹回lifestyle_agent_node——第二次生成正确地只改food
    字段，awaiting_integration_recheck强制先回integration_agent_node复核（不能直接
    再打回一次），最终冲突被resolve、正常终结在"完成"状态。"""
    _seed_edit_route_plan(isolated_project)
    _seed_edit_lifestyle(isolated_project)

    calls = {"route": 0, "lifestyle": 0, "pitfall": 0, "integration": 0}

    def _counted(key):
        def _build(*args, **kwargs):
            calls[key] += 1
            return object()

        return _build

    monkeypatch.setattr(orchestrator, "build_route_agent", _counted("route"))
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", _counted("lifestyle"))
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", _counted("pitfall"))
    monkeypatch.setattr(orchestrator, "build_integration_agent", _counted("integration"))

    lifestyle_prompt_calls = {"n": 0}

    def fake_invoke(agent, messages, **kwargs):
        prompt = messages[0]["content"] if messages else ""
        if "请判断这个要求具体涉及food/outfit/lodging/play中的哪个字段、哪一天" in prompt:
            lifestyle_prompt_calls["n"] += 1
            if lifestyle_prompt_calls["n"] == 1:
                # 第一次：越权多改了outfit字段（用户只要求改food）
                store.write_agent_output_patch(
                    isolated_project,
                    "lifestyle_agent",
                    [
                        {"field": "food", "day": 1, "recommendation": "换成甜品店", "sources": [], "claimed_entities": []},
                        {"field": "outfit", "day": 1, "recommendation": "误改的穿搭建议", "sources": [], "claimed_entities": []},
                    ],
                )
            else:
                # 第二次（被弹回复核后重新生成）：这次正确地只改food，同时把误改的outfit
                # 显式改回原值（patch语义下，不重新提交outfit它会一直停留在第一轮误改的值上，
                # 所以这里必须显式提交一条outfit patch把它纠正回原样，不能只提交food）
                store.write_agent_output_patch(
                    isolated_project,
                    "lifestyle_agent",
                    [
                        {"field": "food", "day": 1, "recommendation": "换成甜品店", "sources": [], "claimed_entities": []},
                        {"field": "outfit", "day": 1, "recommendation": "原始美食推荐", "sources": [], "claimed_entities": []},
                    ],
                )
        elif "当天衣食住行推荐：" in prompt and "warning留空" in prompt:
            _write_empty_pitfall_for_lifestyle(isolated_project)
        elif "请检查两者是否存在有明确上游依据的跨模块冲突" in prompt:
            _write_valid_final_guide(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(
        citation_check, "verify_lifestyle_citations",
        lambda project_id, topic, fields=None, item_keys=None: [],
    )
    monkeypatch.setattr(
        citation_check, "verify_pitfall_citations",
        lambda project_id, topic, fields=None, item_keys=None: [],
    )

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:edit-graph-lifestyle-misedit"}}
    result = graph.invoke(
        _edit_initial_state(isolated_project, "lifestyle_agent_node", "Day1午餐换一家甜品店"), config
    )

    assert "__interrupt__" not in result
    assert calls["route"] == 0  # route没有被触碰
    assert calls["lifestyle"] == 2  # 第一次越权改动 + 被弹回后的第二次修正
    assert calls["pitfall"] == 2  # 两轮lifestyle改动各自级联触发一次pitfall重新检索（下游done标志被重置）
    assert calls["integration"] == 1  # 强制复核只需integration真正跑一次（发现问题已修复，直接resolve）

    lifestyle_output = store.read_workspace(isolated_project, "outputs/lifestyle.json")
    assert lifestyle_output["food"]["items"][0]["recommendation"] == "换成甜品店"
    assert lifestyle_output["outfit"]["items"][0]["recommendation"] == "原始美食推荐"  # 误改已被纠正回原样

    assert result["last_agent_result"]["agent"] == "integration_agent"
    assert store.read_progress(isolated_project).state == ProjectPhase.DONE

    # 误改触发的冲突最终应该被resolve，不应该留下pending记录，也不应该被强制终结
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert len(conflicts) == 1
    assert conflicts[0]["target"] == "lifestyle_agent.food"
    assert conflicts[0]["status"] == "resolved"


# ---------------------------------------------------------------------------
# POI详情agent的edit入口（poi_detail_edit_node，docs/ARCHITECTURE.md第3.4节第9点，
# 2026-07-26接入）：跟lifestyle/pitfall/route三种既有edit入口不同，这是"一跳结束"——
# 核验内嵌在节点自身，不级联citation_check_node/pitfall_agent/integration_agent。
# 此前tests/test_orchestrator_poi_detail_edit.py只验证过直接调用poi_detail_edit_node/
# _route_edit_mode这两个函数各自的行为，从没有真正让build_graph()编译出的完整图
# 调度过entry_target="poi_detail_edit_node"这条路径。
# ---------------------------------------------------------------------------


def test_edit_poi_detail_entry_ends_in_one_hop_without_touching_other_agents(isolated_project, monkeypatch):
    """entry_target="poi_detail_edit_node"经完整图调度：只应该调用poi_detail_agent一次，
    不触碰route/lifestyle/pitfall/integration，最终结果里last_agent_result确实是
    poi_detail_agent，且没有"__interrupt__"（不会意外卡在某个中断节点）。"""
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[RouteOption(route_name="经典线", recommended=True, route_segments=[
            RouteSegment(order=1, point_name="山门", highlight="起点", sources=[]),
        ])],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都1日游", meta={})

    calls = {"route": 0, "lifestyle": 0, "pitfall": 0, "integration": 0, "poi_detail": 0}

    def _counted(key):
        def _build(*args, **kwargs):
            calls[key] += 1
            return object()

        return _build

    monkeypatch.setattr(orchestrator, "build_route_agent", _counted("route"))
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", _counted("lifestyle"))
    monkeypatch.setattr(orchestrator, "build_pitfall_agent", _counted("pitfall"))
    monkeypatch.setattr(orchestrator, "build_integration_agent", _counted("integration"))
    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", _counted("poi_detail"))

    def fake_invoke(agent, messages, **kwargs):
        detail = PoiDetailFile(
            poi_name="武侯祠", day=1,
            routes=[RouteOption(route_name="经典线", recommended=True, route_segments=[
                RouteSegment(order=1, point_name="山门", highlight="全新描述", sources=[]),
            ])],
        )
        store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda detail, topic, route_names=None, **kwargs: [])

    graph = build_graph(isolated_project)
    config = {"configurable": {"thread_id": f"{isolated_project}:edit-graph-poi-detail"}}
    state = _edit_initial_state(isolated_project, "poi_detail_edit_node", "把山门的描述改一下")
    state["day"] = 1
    state["poi_name"] = "武侯祠"
    result = graph.invoke(state, config)

    assert "__interrupt__" not in result
    assert calls == {"route": 0, "lifestyle": 0, "pitfall": 0, "integration": 0, "poi_detail": 1}
    assert result["last_agent_result"]["agent"] == "poi_detail_agent"

    saved = store.read_poi_detail(isolated_project, 1, "武侯祠")
    assert saved.routes[0].route_segments[0].highlight == "全新描述"
