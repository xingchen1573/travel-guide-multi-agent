""""树木生成中"阶段（POI详情agent，Send API并行fan-out+join）的单元测试。

对应docs/ORCHESTRATION.md场景21/22/23，覆盖三个场景：
1. 有needs_detail的POI正常并行完成（fan-out派发正确 + join正确汇总/写manifest/反向同步final_guide）。
2. 某个POI核验打回耗尽（force_finalized）不阻塞其他POI（各自独立，互不影响）。
3. 没有任何needs_detail=true的POI时，直接跳过整个阶段，不fan-out零个任务。

不调用真实LLM——monkeypatch build_poi_detail_agent/invoke_with_retry（跟
tests/test_orchestrator_edit_mode.py同款风格），citation_check.verify_poi_detail_citations
用可控的假实现模拟核验通过/一直不通过两种情况。
"""
import hashlib

import pytest
from types import SimpleNamespace

from langgraph.graph import END
from langgraph.types import Send

from src import orchestrator
from src.llm_rotation import ProviderRetryExhaustedError
from src.materials import citation_check
from src.orchestrator import (
    MAX_RETRY,
    _needs_detail_targets,
    _reconcile_poi_details_after_route_edit,
    _route_poi_detail_phase,
    _sync_poi_details_into_final_guide,
    poi_detail_agent_node,
    poi_details_join_node,
)
from src.workspace import delivery, store
from src.workspace.final_guide_sections import POI_SECTION_HEADING
from src.workspace.schema import (
    EvidenceClaim,
    PoiDetailFile,
    PoiDetailManifest,
    PoiDetailManifestEntry,
    RouteDay,
    RouteFeasibilityCheck,
    RouteFeasibilityReport,
    RoutePlan,
    RoutePoi,
    RouteOption,
    RouteSegment,
)

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "poi-detail-test"
    store.init_project(project_id)
    return project_id


def _seed_route_plan(project_id: str, pois_config: list[tuple[int, str, bool]]) -> None:
    """pois_config: [(day, poi_name, needs_detail), ...]，每个day各一个POI，方便测试。"""
    days = []
    for day, name, needs_detail in pois_config:
        days.append(
            RouteDay(
                day=day,
                city="成都",
                pois=[
                    RoutePoi(
                        name=name,
                        brief="一句话简介",
                        needs_detail=needs_detail,
                        needs_detail_reason="大型景区，值得深挖" if needs_detail else "",
                    )
                ],
            )
        )
    store.write_route_plan(project_id, actor="route_agent", route_plan=RoutePlan(days=days))


def _fake_poi_write(project_id: str, day: int, poi_name: str) -> None:
    """模拟poi_detail_agent真正调用write_agent_output的效果：直接调store.write_poi_detail。"""
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


def _poi_citation_issue() -> dict:
    return {
        "route_name": "经典线",
        "order": 1,
        "point_name": "山门",
        "label": "经典线.途经点1「山门」",
        "chunk_ids": ["chunk-1"],
        "missing_entities": [],
        "reason": "引用没有完整支持具体表述",
        "claim_id": "poi-claim-route-advice",
        "claim_text": "素材显示两个点位相邻，本方案沿用并适合顺路游览",
        "claim_type": "fact",
        "support_mode": "direct",
        "target_path": "highlight",
    }


def _write_manifest(project_id: str, entries: list[tuple[int, str]]) -> None:
    store.write_poi_details_manifest(
        project_id,
        actor="main_agent",
        manifest=PoiDetailManifest(
            generated=[
                PoiDetailManifestEntry(
                    day=day,
                    poi_name=poi_name,
                    file=f"day{day}_{store.poi_slug(poi_name)}.json",
                    citation_status="passed",
                )
                for day, poi_name in entries
            ]
        ),
    )


@pytest.mark.parametrize(
    "current_route",
    [
        {"days": []},
        {"days": [{"day": 2, "pois": [{"name": "青城山", "needs_detail": True}]}]},
        {"days": [{"day": 1, "pois": [{"name": "青城山", "needs_detail": False}]}]},
    ],
    ids=["poi_removed", "poi_moved_to_another_day", "needs_detail_cancelled"],
)
def test_reconcile_archives_route_invalidated_poi_details(isolated_project, current_route):
    original_route = {
        "days": [{"day": 1, "pois": [{"name": "青城山", "needs_detail": True}]}]
    }
    _fake_poi_write(isolated_project, 1, "青城山")
    _write_manifest(isolated_project, [(1, "青城山")])

    results = _reconcile_poi_details_after_route_edit(
        isolated_project, original_route, current_route
    )

    assert results == []
    assert store.read_poi_detail(isolated_project, 1, "青城山") is None
    assert store.list_poi_details(isolated_project) == []
    manifest = store.read_workspace(isolated_project, "outputs/poi_details/manifest.json")
    assert manifest["generated"] == []
    archived = list(
        (store.workspace_dir(isolated_project) / "outputs" / "poi_details" / "_stale").glob(
            "*_day1_青城山.json"
        )
    )
    assert len(archived) == 1


def test_reconcile_keeps_manual_detail_when_same_poi_remains_in_route(isolated_project):
    original_route = {
        "days": [{"day": 1, "pois": [{"name": "太古里", "needs_detail": False}]}]
    }
    current_route = {
        "days": [{"day": 1, "pois": [{"name": "太古里", "needs_detail": False}]}]
    }
    _fake_poi_write(isolated_project, 1, "太古里")
    _write_manifest(isolated_project, [(1, "太古里")])

    results = _reconcile_poi_details_after_route_edit(
        isolated_project, original_route, current_route
    )

    assert results == [{"day": 1, "poi_name": "太古里", "citation_status": "passed"}]
    assert store.read_poi_detail(isolated_project, 1, "太古里") is not None
    assert [detail.poi_name for detail in store.list_poi_details(isolated_project)] == ["太古里"]


def test_sync_empty_results_removes_stale_poi_appendix(isolated_project):
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 成都攻略\n\n正文",
        meta={},
    )
    _fake_poi_write(isolated_project, 1, "青城山")
    _sync_poi_details_into_final_guide(
        isolated_project,
        [{"day": 1, "poi_name": "青城山", "citation_status": "passed"}],
    )
    assert POI_SECTION_HEADING in store.read_workspace(isolated_project, "final_guide.md")

    _sync_poi_details_into_final_guide(isolated_project, [])

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert POI_SECTION_HEADING not in guide
    assert "青城山" not in guide
    assert "# 成都攻略\n\n正文" in guide


def test_join_projects_operational_warning_without_changing_route(isolated_project):
    _seed_route_plan(isolated_project, [(4, "东台吉乃尔湖", True)])
    route_before = store.read_workspace(isolated_project, "route_plan.json")
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 青甘攻略\n\n正文",
        meta={},
    )
    store.write_route_feasibility(
        isolated_project,
        "citation_checker",
        RouteFeasibilityReport(
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-operational-day4-dongtai",
                    day=4,
                    target="东台吉乃尔湖",
                    target_type="poi",
                    category="poi_operational_status",
                    status="warning",
                    summary="开放和进入条件存在时效变化",
                    suggested_action="出发前复核官方开放状态；当前不自动改路线",
                )
            ],
        ),
    )

    _sync_poi_details_into_final_guide(isolated_project, [])

    guide = store.read_workspace(isolated_project, "final_guide.md")
    meta = store.read_workspace(isolated_project, "final_guide.meta.json")
    assert "重要运营提醒" in guide
    assert "Day4 东台吉乃尔湖" in guide
    assert "出发前请再次确认当前开放和进入条件" in guide
    assert "当前不自动改路线" not in guide
    assert meta["warnings"] == [
        {
            "day": 4,
            "poi_name": "东台吉乃尔湖",
            "warning_type": "operational_uncertainty",
            "message": "核验记录提示：开放和进入条件存在时效变化。出发前请再次确认当前开放和进入条件。",
            "evidence_refs": ["poi-operational-day4-dongtai"],
        }
    ]
    assert store.read_workspace(isolated_project, "route_plan.json") == route_before


def test_join_projects_business_unknown_with_predeparture_action(isolated_project):
    _seed_route_plan(isolated_project, [(1, "洱海生态廊道", True)])
    route_before = store.read_workspace(isolated_project, "route_plan.json")
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 大理攻略\n\n正文",
        meta={},
    )
    store.write_route_feasibility(
        isolated_project,
        "citation_checker",
        RouteFeasibilityReport(
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="poi-reservation-day1-erhai",
                    day=1,
                    target="洱海生态廊道",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="unknown",
                    summary="预约规则暂查不到",
                    suggested_action="出发前通过官方渠道复核预约规则",
                )
            ],
        ),
    )

    _sync_poi_details_into_final_guide(isolated_project, [])

    guide = store.read_workspace(isolated_project, "final_guide.md")
    meta = store.read_workspace(isolated_project, "final_guide.meta.json")
    assert "重要运营提醒" in guide
    assert "Day1 洱海生态廊道" in guide
    assert "预约规则暂查不到" in guide
    assert "出发前通过官方渠道复核预约规则" in guide
    assert meta["warnings"] == [
        {
            "day": 1,
            "poi_name": "洱海生态廊道",
            "warning_type": "operational_uncertainty",
            "message": "核验记录提示：预约规则暂查不到。出发前通过官方渠道复核预约规则。",
            "evidence_refs": ["poi-reservation-day1-erhai"],
        }
    ]
    assert store.read_workspace(isolated_project, "route_plan.json") == route_before


def test_join_projects_reservation_unknown_as_operational_uncertainty(
    isolated_project,
):
    _seed_route_plan(isolated_project, [(1, "武侯祠", True)])
    route_before = store.read_workspace(isolated_project, "route_plan.json")
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 成都攻略\n\n正文",
        meta={},
    )
    store.write_route_feasibility(
        isolated_project,
        "citation_checker",
        RouteFeasibilityReport(
            overall_status="unknown",
            checks=[
                RouteFeasibilityCheck(
                    check_id="reservation-day1-wuhouci",
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="reservation_or_quota",
                    status="unknown",
                    summary="暂时查不到预约规则",
                    suggested_action="出发前通过官方渠道重新核实",
                )
            ],
        ),
    )

    _sync_poi_details_into_final_guide(isolated_project, [])

    guide = store.read_workspace(isolated_project, "final_guide.md")
    meta = store.read_workspace(isolated_project, "final_guide.meta.json")
    assert "暂时查不到预约规则" in guide
    assert "出发前请再次确认预约和名额规则" in guide
    assert meta["warnings"] == [
        {
            "day": 1,
            "poi_name": "武侯祠",
            "warning_type": "operational_uncertainty",
            "message": "核验记录提示：暂时查不到预约规则。出发前请再次确认预约和名额规则。",
            "evidence_refs": ["reservation-day1-wuhouci"],
        }
    ]
    assert store.read_workspace(isolated_project, "route_plan.json") == route_before


# ---------------------------------------------------------------------------
# 场景23：没有任何needs_detail=true的POI，跳过整个阶段
# ---------------------------------------------------------------------------


def test_needs_detail_targets_empty_when_all_false():
    route_plan = {
        "days": [
            {"day": 1, "pois": [{"name": "宽窄巷子", "needs_detail": False}]},
            {"day": 2, "pois": [{"name": "太古里", "needs_detail": False}]},
        ]
    }
    assert _needs_detail_targets(route_plan) == []


def test_route_poi_detail_phase_skips_when_no_targets(isolated_project):
    _seed_route_plan(isolated_project, [(1, "宽窄巷子", False), (2, "太古里", False)])
    state = {"project_id": isolated_project, "topic": "成都"}

    result = _route_poi_detail_phase(state)

    assert result == END
    progress = store.read_progress(isolated_project)
    assert progress.state.value == "完成"


def test_route_poi_detail_phase_does_not_fanout_zero_tasks(isolated_project):
    """没有needs_detail POI时不应该产生任何Send（哪怕列表为空也不该走fan-out分支）。"""
    _seed_route_plan(isolated_project, [(1, "宽窄巷子", False)])
    state = {"project_id": isolated_project, "topic": "成都"}

    result = _route_poi_detail_phase(state)

    assert not isinstance(result, list)


# ---------------------------------------------------------------------------
# 场景21：有needs_detail的POI正常并行完成
# ---------------------------------------------------------------------------


def test_route_poi_detail_phase_fans_out_send_per_poi(isolated_project):
    _seed_route_plan(isolated_project, [(1, "青城山", True), (2, "都江堰", True), (3, "太古里", False)])
    state = {"project_id": isolated_project, "topic": "成都"}

    result = _route_poi_detail_phase(state)

    assert isinstance(result, list)
    assert len(result) == 2  # 只有needs_detail=true的两个POI，太古里被排除
    assert all(isinstance(s, Send) for s in result)
    assert all(s.node == "poi_detail_agent_node" for s in result)
    poi_names = {s.arg["poi_name"] for s in result}
    assert poi_names == {"青城山", "都江堰"}
    for s in result:
        assert s.arg["project_id"] == isolated_project
        assert s.arg["topic"] == "成都"
        assert "needs_detail_reason" in s.arg


def test_poi_detail_agent_node_completes_and_returns_result(isolated_project, monkeypatch):
    """单个并行分支正常完成：写入文件+核验通过+返回值累加进poi_detail_results。"""
    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: (_fake_poi_write(isolated_project, 2, "青城山"), fake_agent_reply(messages))[1],
    )
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda detail, topic, route_names=None, **kwargs: [])

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "day": 2,
        "poi_name": "青城山",
        "needs_detail_reason": "大型景区",
    }
    result = poi_detail_agent_node(state)

    assert result["poi_detail_results"] == [{"day": 2, "poi_name": "青城山", "citation_status": "passed"}]
    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert detail is not None
    assert detail.citation_status == "passed"


def test_poi_initial_pass_without_failure_history_is_not_double_checked(
    isolated_project, monkeypatch
):
    _fake_poi_write(isolated_project, 2, "青城山")
    calls = {"verify": 0}

    def verify_once(*args, **kwargs):
        calls["verify"] += 1
        return []

    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", verify_once)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project, "成都", 2, "青城山", object(), route_names=["经典线"]
    )

    assert status == "passed"
    assert calls["verify"] == 1


def test_poi_existing_failure_pass_conflict_stays_force_finalized_without_repair(
    isolated_project, monkeypatch
):
    _fake_poi_write(isolated_project, 2, "青城山")
    store.mark_poi_force_finalize(
        isolated_project,
        2,
        "青城山",
        retry_history=["第1轮：经典线途经点「山门」：旧核验失败"],
    )
    results = iter([[], [_poi_citation_issue()]])
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: next(results),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda *args, **kwargs: pytest.fail("核验冲突不应继续调用返修Agent"),
    )

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project, "成都", 2, "青城山", object(), route_names=["经典线"]
    )

    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert status == "force_finalized"
    assert detail.citation_status == "force_finalized"
    assert detail.retry_history[-1].startswith("通过确认冲突：")


def test_poi_post_patch_pass_conflict_stops_without_second_repair(
    isolated_project, monkeypatch
):
    _fake_poi_write(isolated_project, 2, "青城山")
    results = iter([[_poi_citation_issue()], [], [_poi_citation_issue()]])
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: next(results),
    )
    calls = {"repair": 0}

    def repair_once(*args, **kwargs):
        calls["repair"] += 1
        return fake_agent_reply(args[1])

    monkeypatch.setattr(orchestrator, "invoke_with_retry", repair_once)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project, "成都", 2, "青城山", object(), route_names=["经典线"]
    )

    assert status == "force_finalized"
    assert calls["repair"] == 1
    assert store.read_poi_detail(isolated_project, 2, "青城山").retry_history[-1].startswith(
        "通过确认冲突："
    )


def test_poi_post_patch_two_passes_restore_historical_force_finalized(
    isolated_project, monkeypatch
):
    _fake_poi_write(isolated_project, 2, "青城山")
    store.mark_poi_force_finalize(
        isolated_project,
        2,
        "青城山",
        retry_history=["第1轮：经典线途经点「山门」：旧核验失败"],
    )
    results = iter([[_poi_citation_issue()], [], []])
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: next(results),
    )
    calls = {"repair": 0}

    def repair_once(*args, **kwargs):
        calls["repair"] += 1
        return fake_agent_reply(args[1])

    monkeypatch.setattr(orchestrator, "invoke_with_retry", repair_once)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project, "成都", 2, "青城山", object(), route_names=["经典线"]
    )

    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert status == "passed"
    assert calls["repair"] == 1
    assert detail.citation_status == "passed"
    assert detail.retry_history[0].startswith("第1轮：")


def test_poi_repair_splits_same_route_into_segment_work_units(
    isolated_project, monkeypatch
):
    """同一路线多个失败途经点分别返修；每次Prompt只携带当前途经点。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    issues = [
        _poi_citation_issue(),
        {
            **_poi_citation_issue(),
            "order": 2,
            "point_name": "核心景点",
            "label": "经典线.途经点2「核心景点」",
        },
    ]
    verify_scopes = []
    verify_results = iter([issues, [], []])

    def verify(*args, **kwargs):
        verify_scopes.append(kwargs.get("segment_keys"))
        return next(verify_results)

    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", verify)
    prompts = []

    def repair_segment(*args, **kwargs):
        prompt = args[1][0]["content"]
        prompts.append(prompt)
        order = 1 if "order=1" in prompt else 2
        point_name = "山门" if order == 1 else "核心景点"
        store.write_poi_detail_patch(
            isolated_project,
            "poi_detail_agent",
            2,
            "青城山",
            [
                {
                    "route_name": "经典线",
                    "route_segments": [
                        {
                            "order": order,
                            "point_name": point_name,
                            "highlight": f"order={order}已收窄",
                        }
                    ],
                }
            ],
        )
        return fake_agent_reply(args[1])

    monkeypatch.setattr(orchestrator, "invoke_with_retry", repair_segment)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        object(),
        route_names=["经典线"],
    )

    assert status == "passed"
    assert len(prompts) == 2
    assert "山门" in prompts[0] and "核心景点" not in prompts[0]
    assert "核心景点" in prompts[1] and "山门" not in prompts[1]
    assert "claim_id=poi-claim-route-advice" in prompts[0]
    assert "当前support_mode=direct" in prompts[0]
    assert "本方案沿用并适合顺路游览" in prompts[0]
    assert "只有原文直接支持的部分可保留为direct" in prompts[0]
    expected_scope = [("经典线", 1), ("经典线", 2)]
    assert verify_scopes == [None, expected_scope, expected_scope]
    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert [segment.highlight for segment in detail.routes[0].route_segments] == [
        "order=1已收窄",
        "order=2已收窄",
    ]


def test_poi_patch_timeout_retries_same_business_attempt_once(isolated_project, monkeypatch):
    """传输超时原样重试一次，不应直接消耗下一轮业务返修或force finalize。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    results = iter([[_poi_citation_issue()], [], []])
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: next(results),
    )

    class _Timeout(Exception):
        pass

    monkeypatch.setattr(orchestrator.openai, "APITimeoutError", _Timeout)
    sleeps: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda *args, **kwargs: object(),
    )
    calls = {"repair": 0}

    def timeout_then_pass(*args, **kwargs):
        calls["repair"] += 1
        if calls["repair"] == 1:
            raise _Timeout("Request timed out")
        assert kwargs["max_retries"] == 1
        assert kwargs["recursion_limit"] == 10
        return fake_agent_reply(args[1])

    monkeypatch.setattr(orchestrator, "invoke_with_retry", timeout_then_pass)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        SimpleNamespace(checkpointer=object()),
        route_names=["经典线"],
    )

    assert status == "passed"
    assert calls["repair"] == 2
    assert sleeps == [30.0]
    assert store.read_poi_detail(isolated_project, 2, "青城山").citation_status == "passed"


def test_poi_patch_gateway_error_retries_once_after_retry_after(isolated_project, monkeypatch):
    """502/503/504是技术失败：遵循retry_after原样重试，不消耗业务轮次。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    results = iter([[_poi_citation_issue()], [], []])
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: next(results),
    )

    class _GatewayError(Exception):
        status_code = 504
        body = {"retry_after": 120}

    monkeypatch.setattr(orchestrator.openai, "APIStatusError", _GatewayError)
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda *args, **kwargs: object(),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", sleeps.append)
    calls = {"repair": 0}

    def gateway_then_pass(*args, **kwargs):
        calls["repair"] += 1
        assert kwargs["max_retries"] == 1
        assert kwargs["recursion_limit"] == 10
        if calls["repair"] == 1:
            raise _GatewayError("Gateway timeout")
        return fake_agent_reply(args[1])

    monkeypatch.setattr(orchestrator, "invoke_with_retry", gateway_then_pass)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        SimpleNamespace(checkpointer=object()),
        route_names=["经典线"],
    )

    assert status == "passed"
    assert calls["repair"] == 2
    assert sleeps == [120.0]
    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert detail.citation_status == "passed"


def test_poi_verifier_gateway_error_retries_same_read_once(isolated_project, monkeypatch):
    """无副作用核验遇到502/503/504时原样重试，不应让整个POI分支退出。"""
    _fake_poi_write(isolated_project, 2, "青城山")

    class _GatewayError(Exception):
        status_code = 502
        body = {"retry_after": 0}

    monkeypatch.setattr(orchestrator.openai, "APIStatusError", _GatewayError)
    sleeps: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", sleeps.append)
    calls = {"verify": 0}

    def gateway_then_pass(*args, **kwargs):
        calls["verify"] += 1
        if calls["verify"] == 1:
            raise _GatewayError("Bad gateway")
        return []

    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        gateway_then_pass,
    )

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        object(),
        route_names=["经典线"],
    )

    assert status == "passed"
    assert calls["verify"] == 2
    assert sleeps == [30.0]


def test_poi_verifier_non_transport_error_is_not_retried(isolated_project, monkeypatch):
    """内容/程序错误不伪装成渠道波动，仍立即暴露。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    calls = {"verify": 0}

    def invalid_result(*args, **kwargs):
        calls["verify"] += 1
        raise ValueError("invalid verdict")

    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        invalid_result,
    )

    with pytest.raises(ValueError, match="invalid verdict"):
        orchestrator._run_poi_detail_citation_loop(
            isolated_project,
            "成都",
            2,
            "青城山",
            object(),
            route_names=["经典线"],
        )

    assert calls["verify"] == 1


def test_poi_verifier_provider_aborted_json_generation_is_retried(
    isolated_project, monkeypatch
):
    """Provider明确要求retry的response_format中止按技术故障退避。"""
    _fake_poi_write(isolated_project, 2, "青城山")

    class _StructuredGenerationError(Exception):
        pass

    monkeypatch.setattr(orchestrator.openai, "APIError", _StructuredGenerationError)
    sleeps: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", sleeps.append)
    calls = {"verify": 0}

    def aborted_then_pass(*args, **kwargs):
        calls["verify"] += 1
        if calls["verify"] == 1:
            raise _StructuredGenerationError(
                "Model output became abnormal while generating a JSON response "
                "for response_format. The generation was aborted because the "
                "partial output may be incomplete or invalid JSON. Please retry."
            )
        return []

    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        aborted_then_pass,
    )

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        object(),
        route_names=["经典线"],
    )

    assert status == "passed"
    assert calls["verify"] == 2
    assert sleeps == [30.0]


def test_poi_patch_transport_backoff_exhausts_six_retries_in_one_business_attempt(
    isolated_project, monkeypatch
):
    """持续504最多执行初次+6次，同一业务轮次内按固定阶梯退避后再保守降级。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: [_poi_citation_issue()],
    )

    class _GatewayError(Exception):
        status_code = 504
        body = {}

    monkeypatch.setattr(orchestrator.openai, "APIStatusError", _GatewayError)
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda *args, **kwargs: object(),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", sleeps.append)
    calls = {"repair": 0}

    def always_gateway(*args, **kwargs):
        calls["repair"] += 1
        raise _GatewayError("Gateway timeout")

    monkeypatch.setattr(orchestrator, "invoke_with_retry", always_gateway)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        SimpleNamespace(checkpointer=object()),
        route_names=["经典线"],
    )

    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert status == "force_finalized"
    assert calls["repair"] == 7
    assert sleeps == [30.0, 60.0, 90.0, 120.0, 180.0, 240.0]
    assert "技术退避耗尽" in detail.retry_history[-1]


def test_poi_patch_does_not_replay_exhausted_provider_retry(
    isolated_project, monkeypatch
):
    """Provider内层已耗尽时，POI专用长退避不得再放大请求数。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: [_poi_citation_issue()],
    )
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda *args, **kwargs: object(),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", sleeps.append)
    calls = {"repair": 0}

    def exhausted_provider(*args, **kwargs):
        calls["repair"] += 1
        raise ProviderRetryExhaustedError(
            provider_group="gptload",
            attempts=5,
            last_error=ConnectionError("same-entry attempts exhausted"),
        )

    monkeypatch.setattr(orchestrator, "invoke_with_retry", exhausted_provider)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        SimpleNamespace(checkpointer=object()),
        route_names=["经典线"],
    )

    assert status == "force_finalized"
    assert calls["repair"] == 1
    assert sleeps == []


def test_poi_patch_accepts_write_before_recursion_error(isolated_project, monkeypatch):
    """局部路线已真实变化时，写入后的图步数异常不能把成功Patch误判为未落盘。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    results = iter([[_poi_citation_issue()], [], []])
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: next(results),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda *args, **kwargs: object(),
    )
    calls = {"repair": 0}

    def write_then_hit_limit(*args, **kwargs):
        calls["repair"] += 1
        detail = store.read_poi_detail(isolated_project, 2, "青城山")
        detail.routes[0].route_segments[0].highlight = "已收窄"
        store.write_poi_detail(isolated_project, "poi_detail_agent", detail)
        raise orchestrator.GraphRecursionError("boundary reached after write")

    monkeypatch.setattr(orchestrator, "invoke_with_retry", write_then_hit_limit)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "成都",
        2,
        "青城山",
        SimpleNamespace(checkpointer=object()),
        route_names=["经典线"],
    )

    assert status == "passed"
    assert calls["repair"] == 1
    assert (
        store.read_poi_detail(isolated_project, 2, "青城山")
        .routes[0]
        .route_segments[0]
        .highlight
        == "已收窄"
    )


def test_poi_detail_agent_node_resumes_existing_agent_checkpoint_without_resetting_web_log(
    isolated_project, monkeypatch
):
    """同一turn的POI分支恢复时传input=None+稳定thread_id，且不清空已有web证据日志。"""
    calls = {}

    class _CheckpointedAgent:
        checkpointer = object()

        def get_state(self, config):
            calls["state_config"] = config
            return SimpleNamespace(values={"messages": ["partial"]}, next=("tools",))

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda *args, **kwargs: _CheckpointedAgent())
    monkeypatch.setattr(
        orchestrator,
        "reset_web_search_log",
        lambda *args, **kwargs: calls.setdefault("reset", True),
    )

    def fake_invoke(agent, messages, **kwargs):
        calls["invoke_kwargs"] = kwargs
        _fake_poi_write(isolated_project, 2, "青城山")
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda *args, **kwargs: [])

    result = poi_detail_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "day": 2,
            "poi_name": "青城山",
            "needs_detail_reason": "大型景区",
            "turn_id": "turn-1",
        }
    )

    assert result["poi_detail_results"] == [
        {"day": 2, "poi_name": "青城山", "citation_status": "passed"}
    ]
    assert "reset" not in calls
    assert calls["invoke_kwargs"]["thread_id"] == (
        "poi-detail-test:agent:poi_detail_draft:turn-1:day2_青城山"
    )
    assert calls["invoke_kwargs"]["resume_from_checkpoint"] is True


def test_poi_details_join_writes_manifest_and_syncs_final_guide(isolated_project, monkeypatch):
    """join节点：汇总所有分支结果写manifest.json，纯程序拼接反向同步final_guide.md，
    并把progress.state置为"完成"。不调用任何LLM（不需要monkeypatch任何agent构建函数）。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    _fake_poi_write(isolated_project, 3, "都江堰")
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都3日游\n正文内容", meta={})
    store.write_experiment_manifest(
        isolated_project,
        {
            "scenario_id": "join-test",
            "experiment_version": "join-test-v1",
            "harness": "langgraph",
            "completion_mode": "in_progress",
        },
    )

    state = {
        "project_id": isolated_project,
        "poi_detail_results": [
            {"day": 2, "poi_name": "青城山", "citation_status": "passed"},
            {"day": 3, "poi_name": "都江堰", "citation_status": "passed"},
        ],
    }
    result = poi_details_join_node(state)

    manifest = store.read_workspace(isolated_project, "outputs/poi_details/manifest.json")
    assert len(manifest["generated"]) == 2
    names = {e["poi_name"] for e in manifest["generated"]}
    assert names == {"青城山", "都江堰"}

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "青城山" in guide
    assert "都江堰" in guide
    assert "山门" in guide  # 途经点摘要确实被拼接进去了
    assert "# 成都3日游\n正文内容" in guide  # 原有正文保留，不是覆盖

    progress = store.read_progress(isolated_project)
    assert progress.state.value == "完成"
    experiment = store.read_workspace(isolated_project, "experiment.json")
    assert experiment["completion_mode"] == "completed"
    assert result["last_agent_result"]["agent"] == "poi_detail_agent"


def test_finalization_marks_nested_upstream_force_finalized_as_degradation(
    isolated_project,
):
    store.write_experiment_manifest(
        isolated_project,
        {
            "scenario_id": "upstream-degradation-test",
            "experiment_version": "upstream-degradation-test-v1",
            "harness": "langgraph",
            "completion_mode": "in_progress",
        },
    )
    lifestyle_path = (
        store.workspace_dir(isolated_project) / "outputs" / "lifestyle.json"
    )
    lifestyle_path.parent.mkdir(parents=True, exist_ok=True)
    lifestyle_path.write_text(
        '{"food":{"items":[{"citation_status":"force_finalized"}]}}',
        encoding="utf-8",
    )

    delivery.finalize_poi_details(isolated_project, "成都", [])

    experiment = store.read_workspace(isolated_project, "experiment.json")
    assert experiment["completion_mode"] == "completed_with_degradation"


def test_poi_appendix_omits_failed_claim_field_but_preserves_raw_detail(isolated_project):
    detail = PoiDetailFile(
        poi_name="杜甫草堂",
        day=2,
        routes=[
            RouteOption(
                route_name="人文主线",
                recommended=True,
                route_segments=[
                    RouteSegment(
                        order=1,
                        point_name="正门与大廨",
                        highlight="未经支持的具体位置关系",
                        claims=[
                            EvidenceClaim(
                                claim_id="poi-point-pass",
                                target_path="point_name",
                                claim_text="途经正门与大廨。",
                                support_mode="direct",
                            ),
                            EvidenceClaim(
                                claim_id="poi-highlight-fail",
                                target_path="highlight",
                                claim_text="未经支持的具体位置关系。",
                                support_mode="direct",
                            ),
                        ],
                    )
                ],
            )
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)
    store.write_evidence_checks(
        isolated_project,
        "poi_detail",
        [
            {"claim_id": "poi-point-pass", "result": "pass"},
            {"claim_id": "poi-highlight-fail", "result": "fail"},
        ],
    )
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 成都攻略\n正文",
        meta={},
    )

    poi_details_join_node(
        {
            "project_id": isolated_project,
            "poi_detail_results": [
                {"day": 2, "poi_name": "杜甫草堂", "citation_status": "force_finalized"},
            ],
        }
    )

    guide = store.read_workspace(isolated_project, "final_guide.md")
    persisted = store.read_poi_detail(isolated_project, 2, "杜甫草堂")
    assert "正门与大廨" in guide
    assert "未经支持的具体位置关系" not in guide
    assert persisted.routes[0].route_segments[0].highlight == "未经支持的具体位置关系"


def test_poi_appendix_uses_review_fallback_when_all_waypoints_are_filtered(isolated_project):
    detail = PoiDetailFile(
        poi_name="杜甫草堂",
        day=2,
        routes=[
            RouteOption(
                route_name="人文主线",
                recommended=True,
                route_segments=[
                    RouteSegment(
                        order=1,
                        point_name="大廨",
                        claims=[
                            EvidenceClaim(
                                claim_id="poi-point-fail",
                                target_path="point_name",
                                claim_text="途经大廨。",
                                support_mode="direct",
                            )
                        ],
                    )
                ],
            )
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)
    store.write_evidence_checks(
        isolated_project,
        "poi_detail",
        [{"claim_id": "poi-point-fail", "result": "fail"}],
    )
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 成都攻略\n正文",
        meta={},
    )

    poi_details_join_node(
        {
            "project_id": isolated_project,
            "poi_detail_results": [
                {"day": 2, "poi_name": "杜甫草堂", "citation_status": "force_finalized"},
            ],
        }
    )

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "具体途经点尚未通过自动核验，已提交复核" in guide
    assert "途经：。" not in guide


def test_poi_details_join_preserves_zero_recommended_semantics(isolated_project):
    """零推荐是合法状态，回写只能中性预览第一条，不能偷偷改写成模型推荐。"""
    store.write_poi_detail(
        isolated_project,
        actor="poi_detail_agent",
        poi_detail=PoiDetailFile(
            poi_name="嘉峪关关城",
            day=7,
            routes=[
                RouteOption(
                    route_name="关城主体线",
                    recommended=False,
                    route_segments=[
                        RouteSegment(order=1, point_name="东闸门", highlight="进入关城"),
                    ],
                ),
                RouteOption(
                    route_name="悬壁长城线",
                    recommended=False,
                    route_segments=[
                        RouteSegment(order=1, point_name="山脚", highlight="开始登城"),
                    ],
                ),
            ],
        ),
    )
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 青甘行程\n正文内容",
        meta={},
    )

    poi_details_join_node(
        {
            "project_id": isolated_project,
            "poi_detail_results": [
                {"day": 7, "poi_name": "嘉峪关关城", "citation_status": "passed"},
            ],
        }
    )

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "可选路线之一「关城主体线」途经：东闸门（进入关城）。" in guide
    assert "推荐路线「关城主体线」" not in guide


def test_poi_details_join_rebuilds_reference_section_with_poi_sources(isolated_project):
    """用户反馈"参考来源"表格看着比实际引用少：join节点执行前，integration_agent_node
    已经用collect_cited_chunks(route_plan, lifestyle, pitfall)生成过一版参考来源表，
    但那时POI详情还没生成，表格里必然没有POI详情层的引用。join节点应该在所有POI详情
    写完后，重新收集全量引用（含poi_details）并覆盖旧版参考来源表，而不是让旧表一直
    留着少列一整层来源。"""
    store.write_route_plan(
        isolated_project,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])],
        ),
    )
    store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "钟水饺",
                "sources": [{"platform": "bilibili", "author": "", "doc_id": "d1", "chunk_id": "d1-c001"}],
                "claimed_entities": [],
            }
        ],
    )
    store.write_poi_detail(
        isolated_project,
        actor="poi_detail_agent",
        poi_detail=PoiDetailFile(
            poi_name="青城山",
            day=2,
            routes=[
                RouteOption(
                    route_name="经典线",
                    recommended=True,
                    route_segments=[
                        RouteSegment(
                            order=1,
                            point_name="山门",
                            highlight="起点",
                            sources=[{"platform": "bilibili", "author": "Z", "doc_id": "d4", "chunk_id": "d4-c001"}],
                        ),
                    ],
                )
            ],
        ),
    )
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 成都3日游\n正文内容\n\n---\n\n## 📚 参考来源（程序生成，非模型自行撰写）\n\n"
        "| 平台 | doc_id | chunk_id | 原文片段 |\n|---|---|---|---|\n"
        "| bilibili | d1 | d1-c001 | ⚠️原文未找到，此引用可能有误 |",
        meta={},
    )
    store.write_final_guide_contract_validation(
        isolated_project,
        status="passed",
        issues=[],
    )
    before_meta = store.read_workspace(isolated_project, "final_guide.meta.json")
    body_hash = before_meta["contract_validation"]["body_sha256"]

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "poi_detail_results": [{"day": 2, "poi_name": "青城山", "citation_status": "passed"}],
    }
    poi_details_join_node(state)

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "d1-c001" in guide  # 整合阶段已收集的lifestyle引用保留
    assert "d4-c001" in guide  # 之前被漏掉的POI详情层引用，join节点补全后出现
    assert "青城山" in guide  # "🌳 重点景点深度玩法"章节仍然被正确追加，没有被参考来源重写冲掉
    assert guide.index("重点景点深度玩法") < guide.index("参考来源")

    meta = store.read_workspace(isolated_project, "final_guide.meta.json")
    ref_chunk_ids = {r["chunk_id"] for r in meta["references"]}
    assert ref_chunk_ids == {"d1-c001", "d4-c001"}
    contract = meta["contract_validation"]
    assert contract["body_sha256"] == body_hash
    assert contract["guide_sha256"] == hashlib.sha256(guide.encode("utf-8")).hexdigest()

    # 同一join重跑时，参考来源重建会面对已存在的POI附录；必须保留它，
    # 并且不能反复追加出多份程序章节。
    poi_details_join_node(state)
    rebuilt = store.read_workspace(isolated_project, "final_guide.md")
    assert rebuilt.count("重点景点深度玩法") == 1
    assert rebuilt.index("重点景点深度玩法") < rebuilt.index("参考来源")
    rebuilt_contract = store.read_workspace(isolated_project, "final_guide.meta.json")["contract_validation"]
    assert rebuilt_contract["guide_sha256"] == hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 场景22：某个POI核验打回耗尽，不阻塞其他POI
# ---------------------------------------------------------------------------


def test_one_poi_force_finalized_does_not_block_others(isolated_project, monkeypatch):
    """"青城山"分支的citation_check打回MAX_RETRY次仍不过 -> force_finalized，
    如实标注但不影响"都江堰"分支正常完成。两个分支各自独立调用，验证互不干扰。"""
    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    # 第一次调用触发写入，后续fix_prompt调用什么都不做（模拟屡教不改）
    calls = {"n": 0}

    def invoke_side_effect(agent, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _fake_poi_write(isolated_project, 2, "青城山")
        # 之后的调用（核验打回的修正prompt）不改变内容，问题依旧
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)

    def fake_verify_always_fails(detail, topic, route_names=None, **kwargs):
        return [
            {
                "route_name": "经典线",
                "order": 1,
                "point_name": "山门",
                "label": "经典线.途经点1「山门」",
                "chunk_ids": [],
                "missing_entities": ["山门"],
                "reason": "原文没有提到",
            }
        ]

    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", fake_verify_always_fails)
    # 打回耗尽会走到_attempt_auto_material_supplement（docs/ARCHITECTURE.md第16节），
    # 不mock的话会真的调用check_material_gap/search_and_rank（真实网络+embedding调用）
    # 甚至collect_one/ingest_topic（真实写入生产素材库），见docs/ISSUES.md第78条。
    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", lambda *a, **kw: [])

    state_a = {
        "project_id": isolated_project,
        "topic": "成都",
        "day": 2,
        "poi_name": "青城山",
        "needs_detail_reason": "大型景区",
    }
    result_a = poi_detail_agent_node(state_a)

    assert result_a["poi_detail_results"] == [{"day": 2, "poi_name": "青城山", "citation_status": "force_finalized"}]
    detail_a = store.read_poi_detail(isolated_project, 2, "青城山")
    assert detail_a.citation_status == "force_finalized"

    # 独立的另一个分支：核验一次通过，不受"青城山"分支打回耗尽的影响
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda detail, topic, route_names=None, **kwargs: [])
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: (_fake_poi_write(isolated_project, 3, "都江堰"), fake_agent_reply(messages))[1],
    )
    state_b = {
        "project_id": isolated_project,
        "topic": "成都",
        "day": 3,
        "poi_name": "都江堰",
        "needs_detail_reason": "大型景区",
    }
    result_b = poi_detail_agent_node(state_b)

    assert result_b["poi_detail_results"] == [{"day": 3, "poi_name": "都江堰", "citation_status": "passed"}]
    detail_b = store.read_poi_detail(isolated_project, 3, "都江堰")
    assert detail_b.citation_status == "passed"

    # join节点汇总两者：manifest里如实记录不同的citation_status，不因一个force_finalized
    # 就阻塞整体进入"完成"
    join_state = {
        "project_id": isolated_project,
        "poi_detail_results": [
            {"day": 2, "poi_name": "青城山", "citation_status": "force_finalized"},
            {"day": 3, "poi_name": "都江堰", "citation_status": "passed"},
        ],
    }
    poi_details_join_node(join_state)

    manifest = store.read_workspace(isolated_project, "outputs/poi_details/manifest.json")
    status_by_name = {e["poi_name"]: e["citation_status"] for e in manifest["generated"]}
    assert status_by_name == {"青城山": "force_finalized", "都江堰": "passed"}

    progress = store.read_progress(isolated_project)
    assert progress.state.value == "完成"  # 没有被"青城山"的失败阻塞

    # 项目级别的force_finalize粘性标志不应该被这个POI粒度的失败影响——验证不能复用project粒度机制
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert not any(c.get("status") == "force_finalized" for c in conflicts)


# ---------------------------------------------------------------------------
# 真实E2E回归测试（2026-07-21，chengdu-3d-e2e-v19）：某个POI分支的agent多次尝试后
# 完全没有调用write_agent_output（不是核验没通过，是压根没产出任何内容），
# _invoke_with_write_verification按设计会抛RuntimeError——但这是Send并行分支之一，
# 如果不捕获，异常会冲垮整个graph.invoke()，让另外已经成功完成的POI也白白陪葬：
# join节点永远不会执行，project永久卡在"树木生成中"无法自然恢复。
# ---------------------------------------------------------------------------


def test_poi_detail_agent_node_handles_total_write_failure_without_crashing(isolated_project, monkeypatch):
    """单个分支多次尝试后仍未写入任何内容时，poi_detail_agent_node必须体面返回
    citation_status="generation_failed"，而不是让RuntimeError冒泡出去冲垮整个graph。"""
    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())
    # 模拟agent每次都只输出文字总结、从不调用write_agent_output——_invoke_with_write_verification
    # 内部会重试到耗尽后抛RuntimeError。
    monkeypatch.setattr(orchestrator, "invoke_with_retry", lambda agent, messages, **kwargs: fake_agent_reply(messages))
    monkeypatch.setattr(
        orchestrator,
        "_attempt_poi_provider_recovery",
        lambda **kwargs: pytest.fail("write_contract_failure不应触发Provider恢复"),
    )

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "day": 3,
        "poi_name": "大熊猫繁育研究基地",
        "needs_detail_reason": "大型园区，值得深挖",
    }
    result = poi_detail_agent_node(state)

    failure = result["poi_detail_results"][0]
    assert failure["day"] == 3
    assert failure["poi_name"] == "大熊猫繁育研究基地"
    assert failure["citation_status"] == "generation_failed"
    assert failure["failure_kind"] == "write_contract_failure"
    assert "仍未完成必需的写入" in failure["failure_summary"]
    # 确实没有写入任何文件——这不是"写了但核验没过"，是压根没有产出
    assert store.read_poi_detail(isolated_project, 3, "大熊猫繁育研究基地") is None


def test_empty_poi_is_returned_to_same_role_for_bounded_nonempty_repair(
    isolated_project, monkeypatch
):
    store.write_poi_detail(
        isolated_project,
        "poi_detail_agent",
        PoiDetailFile(poi_name="东台吉乃尔湖", day=4, routes=[]),
    )
    prompts = []

    def repair(agent, prompt, **kwargs):
        prompts.append(prompt)
        _fake_poi_write(isolated_project, 4, "东台吉乃尔湖")
        assert kwargs["verify_fn"]()

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", repair)
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda *args, **kwargs: [],
    )

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "青甘大环线",
        4,
        "东台吉乃尔湖",
        object(),
    )

    detail = store.read_poi_detail(isolated_project, 4, "东台吉乃尔湖")
    assert status == "passed"
    assert detail.routes[0].route_segments
    assert detail.evidence_gap is False
    assert any("content_empty" in item for item in detail.retry_history)
    assert "职责是写攻略和提醒风险" in prompts[0]
    assert "不是决定固定路线中的景点去留" in prompts[0]


def test_empty_poi_repair_exhaustion_is_generation_failure_not_force_finalize(
    isolated_project, monkeypatch
):
    store.write_poi_detail(
        isolated_project,
        "poi_detail_agent",
        PoiDetailFile(poi_name="东台吉乃尔湖", day=4, routes=[]),
    )
    calls = {"count": 0}

    def no_write(agent, messages, **kwargs):
        calls["count"] += 1
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", no_write)

    status = orchestrator._run_poi_detail_citation_loop(
        isolated_project,
        "青甘大环线",
        4,
        "东台吉乃尔湖",
        object(),
    )

    detail = store.read_poi_detail(isolated_project, 4, "东台吉乃尔湖")
    assert status == "generation_failed"
    assert detail.citation_status == "generation_failed"
    assert detail.human_review_required is True
    assert detail.evidence_gap is False
    assert detail.routes == []
    assert calls["count"] == 2


def test_poi_detail_agent_node_records_provider_failure_without_hiding_it(
    isolated_project, monkeypatch
):
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda project_id, topic, day, poi_name: object(),
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("GPT-Load upstream 504 gateway timeout")
        ),
    )
    recovery_calls = []
    monkeypatch.setattr(
        orchestrator,
        "_attempt_poi_provider_recovery",
        lambda **kwargs: (recovery_calls.append(kwargs) or (False, "recovery also timed out")),
    )

    result = poi_detail_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "day": 3,
            "poi_name": "大熊猫繁育研究基地",
            "needs_detail_reason": "大型园区，值得深挖",
        }
    )

    failure = result["poi_detail_results"][0]
    assert failure["citation_status"] == "generation_failed"
    assert failure["failure_kind"] == "provider_or_tool_failure"
    assert "504" in failure["failure_summary"]
    assert failure["generation_mode"] == "provider_recovery"
    assert failure["recovery_trigger"] == "provider_retry_exhausted"
    assert len(recovery_calls) == 1


def test_poi_provider_failure_recovery_writes_then_enters_normal_citation_loop(
    isolated_project, monkeypatch
):
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("GPT-Load upstream 504 gateway timeout")
        ),
    )
    recovery_calls = []

    def recover(**kwargs):
        recovery_calls.append(kwargs)
        _fake_poi_write(isolated_project, 5, "鸣沙山月牙泉")
        return True, ""

    monkeypatch.setattr(orchestrator, "_attempt_poi_provider_recovery", recover)
    citation_calls = []
    monkeypatch.setattr(
        orchestrator,
        "_run_poi_detail_citation_loop",
        lambda *args, **kwargs: (citation_calls.append((args, kwargs)) or "passed"),
    )

    result = poi_detail_agent_node(
        {
            "project_id": isolated_project,
            "topic": "青甘",
            "day": 5,
            "poi_name": "鸣沙山月牙泉",
            "needs_detail_reason": "大型景区",
        }
    )

    assert result["poi_detail_results"] == [
        {
            "day": 5,
            "poi_name": "鸣沙山月牙泉",
            "citation_status": "passed",
            "generation_mode": "provider_recovery",
            "recovery_trigger": "provider_retry_exhausted",
        }
    ]
    assert len(recovery_calls) == 1
    assert len(citation_calls) == 1


def test_poi_provider_error_after_durable_write_skips_recovery(
    isolated_project, monkeypatch
):
    monkeypatch.setattr(
        orchestrator,
        "build_poi_detail_agent",
        lambda *args, **kwargs: object(),
    )

    def write_then_fail(*args, **kwargs):
        _fake_poi_write(isolated_project, 2, "青城山")
        raise RuntimeError("GPT-Load upstream 504 gateway timeout")

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", write_then_fail)
    monkeypatch.setattr(
        orchestrator,
        "_attempt_poi_provider_recovery",
        lambda **kwargs: pytest.fail("已有持久化POI文件时不应触发恢复"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_poi_detail_citation_loop",
        lambda *args, **kwargs: "passed",
    )

    result = poi_detail_agent_node(
        {
            "project_id": isolated_project,
            "topic": "成都",
            "day": 2,
            "poi_name": "青城山",
            "needs_detail_reason": "大型景区",
        }
    )

    assert result["poi_detail_results"] == [
        {"day": 2, "poi_name": "青城山", "citation_status": "passed"}
    ]


def test_attempt_poi_provider_recovery_is_single_stateless_write_attempt(
    isolated_project, monkeypatch
):
    captured = {}
    events = []
    recovery_agent = object()
    monkeypatch.setattr(
        orchestrator,
        "_poi_recovery_evidence_view",
        lambda project_id, log_scope: [
            {
                "chunk_id": "project-evidence-1",
                "title": "官方公告",
                "summary": "景区开放",
                "source_url": "https://example.com/poi",
                "source_query": "景区 开放",
                "source_observed_at": "2026-09-03T00:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_poi_recovery_working_notes",
        lambda project_id, log_scope: [{"fact": "票价待核对", "created_at": ""}],
    )
    monkeypatch.setattr(
        orchestrator,
        "append_context_event",
        events.append,
    )
    monkeypatch.setattr(
        "src.llm_rotation.poi_recovery_model_identity",
        lambda: {"provider": "ooioo", "model": "deepseek-v4-flash"},
    )

    def build_agent(*args, **kwargs):
        captured["build_kwargs"] = kwargs
        return recovery_agent

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", build_agent)

    def invoke(agent, prompt, **kwargs):
        captured["agent"] = agent
        captured["prompt"] = prompt
        captured["invoke_kwargs"] = kwargs
        _fake_poi_write(isolated_project, 5, "鸣沙山月牙泉")

    monkeypatch.setattr(orchestrator, "_invoke_with_write_verification", invoke)

    recovered, summary = orchestrator._attempt_poi_provider_recovery(
        project_id=isolated_project,
        topic="青甘",
        day=5,
        poi_name="鸣沙山月牙泉",
        needs_detail_reason="大型景区",
        route_plan={"days": []},
        requirements={"requirements": {"style": "摄影"}},
    )

    assert recovered is True
    assert summary == ""
    assert captured["agent"] is recovery_agent
    assert captured["build_kwargs"] == {
        "checkpointer": False,
        "provider_recovery": True,
    }
    assert captured["invoke_kwargs"]["max_retries"] == 0
    assert captured["invoke_kwargs"]["thread_id"] is None
    assert "working notes是待重新核对" in captured["prompt"]
    assert [event["status"] for event in events] == [
        "poi_generation_recovery_started",
        "poi_generation_recovery_succeeded",
    ]
    assert events[0]["candidate_evidence_count"] == 1
    assert events[0]["working_note_count"] == 1
    assert events[0]["recovery_provider"] == "ooioo"
    assert events[0]["recovery_model"] == "deepseek-v4-flash"


def test_poi_detail_total_failure_does_not_block_other_branches_at_join(isolated_project, monkeypatch):
    """一个分支完全失败（generation_failed）+ 另一个分支正常完成，join节点应该正常执行完，
    不因为一个分支没有文件就崩溃，manifest如实记录两种不同状态，project正常进入"完成"。"""
    _fake_poi_write(isolated_project, 2, "青城山")
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都3日游\n正文内容", meta={})
    store.write_experiment_manifest(
        isolated_project,
        {
            "scenario_id": "join-degradation-test",
            "experiment_version": "join-degradation-test-v1",
            "harness": "langgraph",
            "completion_mode": "in_progress",
        },
    )

    state = {
        "project_id": isolated_project,
        "poi_detail_results": [
            {"day": 2, "poi_name": "青城山", "citation_status": "passed"},
            {
                "day": 3,
                "poi_name": "大熊猫繁育研究基地",
                "citation_status": "generation_failed",
                "failure_kind": "provider_or_tool_failure",
                "failure_summary": "GPT-Load 504 gateway timeout",
            },
        ],
    }
    result = poi_details_join_node(state)

    manifest = store.read_workspace(isolated_project, "outputs/poi_details/manifest.json")
    status_by_name = {e["poi_name"]: e["citation_status"] for e in manifest["generated"]}
    assert status_by_name == {"青城山": "passed", "大熊猫繁育研究基地": "generation_failed"}
    failed_entry = next(
        entry for entry in manifest["generated"] if entry["citation_status"] == "generation_failed"
    )
    assert failed_entry["failure_kind"] == "provider_or_tool_failure"
    assert "504" in failed_entry["failure_summary"]

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "青城山" in guide
    assert "山门" in guide  # 正常完成的分支内容被正确拼接
    assert "大熊猫繁育研究基地" in guide
    assert "未能成功生成" in guide  # 失败分支有诚实的说明，不是指向不存在文件的死链接

    progress = store.read_progress(isolated_project)
    assert progress.state.value == "完成"  # 没有被这个彻底失败的分支阻塞
    experiment = store.read_workspace(isolated_project, "experiment.json")
    assert experiment["completion_mode"] == "completed_with_degradation"
    assert result["last_agent_result"]["agent"] == "poi_detail_agent"


def test_poi_provider_recovery_markers_are_persisted_in_manifest(
    isolated_project,
):
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 青甘攻略\n\n正文",
        meta={},
    )
    _fake_poi_write(isolated_project, 5, "鸣沙山月牙泉")

    poi_details_join_node(
        {
            "project_id": isolated_project,
            "topic": "青甘",
            "poi_detail_results": [
                {
                    "day": 5,
                    "poi_name": "鸣沙山月牙泉",
                    "citation_status": "passed",
                    "generation_mode": "provider_recovery",
                    "recovery_trigger": "provider_retry_exhausted",
                }
            ],
        }
    )

    manifest = store.read_workspace(
        isolated_project, "outputs/poi_details/manifest.json"
    )
    entry = manifest["generated"][0]
    assert entry["generation_mode"] == "provider_recovery"
    assert entry["recovery_trigger"] == "provider_retry_exhausted"
