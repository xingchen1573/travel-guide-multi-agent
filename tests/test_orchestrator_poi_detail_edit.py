"""POI详情agent的edit入口（poi_detail_edit_node）单元测试。

对应docs/ARCHITECTURE.md第3.4节第9点此前"这次不做"的局限——2026-07-26接入，2026-08-06
改造成分层增量patch（路线按route_name、途经点按order合并；双工具并存，仿route_draft_node，
见docs/ISSUES.md相关记录），
覆盖：
1. 修改已有路线的具体内容——走write_agent_output_patch，核验范围收窄到这条路线。
2. 切换推荐路线（多骨架备选）——走write_agent_output_patch，不触发citation_check，
   沿用原有citation_status。
3. 新增一条此前不存在的路线——走整份覆盖write_agent_output（结构性变化），核验范围只
   包含新增的路线。
3b. 删除某条已有路线——走整份覆盖write_agent_output，其余路线保留。
4. 越权改动检测——一次改动超过1条路线视为可疑，report_conflict留痕。
5. 首次生成——该POI此前没有详情文件，用户事后单独要求补充生成，全量核验。
6. 反向同步final_guide.md/manifest.json时不会冲掉其它已生成POI的内容。
7. _route_edit_mode路由：last_agent=="poi_detail_agent"一跳直接结束。
8. run_edit()对entry_target="poi_detail_edit_node"的day/poi_name参数校验。

不调用真实LLM——monkeypatch build_poi_detail_agent/invoke_with_retry（跟
tests/test_orchestrator_poi_details.py同款风格）。invoke_side_effect里模拟agent真实写入
时，内容编辑/切换推荐场景改用store.write_poi_detail_patch（对称真实patch工具的行为），
新增/删除路线场景仍用_seed_detail（对称write_agent_output整份覆盖）。
"""
import pytest

from langgraph.graph import END

from src import orchestrator
from src.materials import citation_check
from src.orchestrator import (
    GraphState,
    _diff_poi_routes,
    _route_edit_mode,
    poi_detail_edit_node,
    run_edit,
)
from src.workspace import store
from src.workspace.schema import PoiDetailFile, PoiDetailManifest, PoiDetailManifestEntry, RouteOption, RouteSegment

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "poi-detail-edit-test"
    store.init_project(project_id)
    return project_id


def _seed_detail(project_id: str, day: int, poi_name: str, routes: list[RouteOption], citation_status: str = "passed") -> None:
    detail = PoiDetailFile(poi_name=poi_name, day=day, routes=routes, citation_status=citation_status)
    store.write_poi_detail(project_id, actor="poi_detail_agent", poi_detail=detail)


def _route_a(recommended: bool = True) -> RouteOption:
    return RouteOption(
        route_name="经典线",
        recommended=recommended,
        route_segments=[
            RouteSegment(order=1, point_name="山门", highlight="起点", sources=[]),
            RouteSegment(order=2, point_name="核心景点", highlight="必看", sources=[]),
        ],
    )


def _route_b(recommended: bool = False) -> RouteOption:
    return RouteOption(
        route_name="后山线",
        recommended=recommended,
        route_segments=[
            RouteSegment(order=1, point_name="后门", highlight="人少", sources=[]),
        ],
    )


# ---------------------------------------------------------------------------
# _diff_poi_routes 纯函数行为
# ---------------------------------------------------------------------------


def test_diff_poi_routes_ignores_pure_recommended_switch():
    before = {"routes": [_route_a(True).model_dump(mode="json"), _route_b(False).model_dump(mode="json")]}
    after = {"routes": [_route_a(False).model_dump(mode="json"), _route_b(True).model_dump(mode="json")]}

    needs_recheck, touched = _diff_poi_routes(before, after)

    assert needs_recheck == []  # 纯粹切换推荐，没有任何路线内容变化，不需要重新核验
    assert touched == []  # 也不该被判定为越权改动


def test_diff_poi_routes_detects_content_change():
    route_a_changed = _route_a(True).model_dump(mode="json")
    route_a_changed["route_segments"][0]["highlight"] = "改过的起点描述"
    before = {"routes": [_route_a(True).model_dump(mode="json"), _route_b(False).model_dump(mode="json")]}
    after = {"routes": [route_a_changed, _route_b(False).model_dump(mode="json")]}

    needs_recheck, touched = _diff_poi_routes(before, after)

    assert needs_recheck == ["经典线"]
    assert touched == ["经典线"]


def test_diff_poi_routes_detects_new_route():
    before = {"routes": [_route_a(True).model_dump(mode="json")]}
    after = {"routes": [_route_a(True).model_dump(mode="json"), _route_b(True).model_dump(mode="json")]}

    needs_recheck, touched = _diff_poi_routes(before, after)

    assert needs_recheck == ["后山线"]
    assert touched == ["后山线"]


# ---------------------------------------------------------------------------
# 场景1：修改已有路线内容
# ---------------------------------------------------------------------------


def test_edit_modifies_existing_route_content_scopes_citation_check(isolated_project, monkeypatch):
    _seed_detail(isolated_project, 2, "青城山", [_route_a(True), _route_b(False)])
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都3日游", meta={})

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        # 只改"经典线"的一个途经点描述，走patch——"后山线"不需要重新提交，store层保留原样
        store.write_poi_detail_patch(
            isolated_project, "poi_detail_agent", 2, "青城山",
            [
                RouteOption(
                    route_name="经典线", recommended=True,
                    route_segments=[
                        RouteSegment(order=1, point_name="山门", highlight="全新描述", sources=[]),
                        RouteSegment(order=2, point_name="核心景点", highlight="必看", sources=[]),
                    ],
                ).model_dump(mode="json"),
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)

    captured_route_names = {}

    def fake_verify(detail, topic, route_names=None, **kwargs):
        captured_route_names["value"] = route_names
        return []

    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", fake_verify)

    state: GraphState = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "day": 2, "poi_name": "青城山", "edit_request": "把山门的描述改一下",
    }
    result = poi_detail_edit_node(state)

    assert captured_route_names["value"] == ["经典线"]  # 核验范围收窄到真正改动的路线
    assert result["last_agent_result"]["agent"] == "poi_detail_agent"

    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert conflicts == []  # 只改了1条路线，不触发越权检测

    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert "青城山" in guide
    assert "山门" in guide


# ---------------------------------------------------------------------------
# 场景2：切换推荐路线（多骨架备选）——不触发citation_check
# ---------------------------------------------------------------------------


def test_edit_switches_recommended_route_without_citation_recheck(isolated_project, monkeypatch):
    _seed_detail(isolated_project, 2, "青城山", [_route_a(True), _route_b(False)], citation_status="passed")

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        # 只切换recommended标记，两条路线内容本身不变——patch语义下要求同时提交旧的
        # 推荐路线（改成false）和新的推荐路线（改成true）两条
        store.write_poi_detail_patch(
            isolated_project, "poi_detail_agent", 2, "青城山",
            [_route_a(False).model_dump(mode="json"), _route_b(True).model_dump(mode="json")],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)

    def fail_if_called(detail, topic, route_names=None, **kwargs):
        raise AssertionError("纯粹切换推荐路线不应该触发citation_check重新核验")

    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", fail_if_called)

    state: GraphState = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "day": 2, "poi_name": "青城山", "edit_request": "推荐路线换成后山线",
    }
    result = poi_detail_edit_node(state)

    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    recommended_names = [r.route_name for r in detail.routes if r.recommended]
    assert recommended_names == ["后山线"]
    assert detail.citation_status == "passed"  # 沿用原状态，没有被重新核验改变
    assert result["last_agent_result"]["agent"] == "poi_detail_agent"

    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert conflicts == []


# ---------------------------------------------------------------------------
# 场景3：新增一条路线
# ---------------------------------------------------------------------------


def test_edit_adds_new_route_scopes_citation_check_to_new_route(isolated_project, monkeypatch):
    _seed_detail(isolated_project, 2, "青城山", [_route_a(True)])

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        _seed_detail(isolated_project, 2, "青城山", [_route_a(True), _route_b(False)])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)

    captured_route_names = {}

    def fake_verify(detail, topic, route_names=None, **kwargs):
        captured_route_names["value"] = route_names
        return []

    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", fake_verify)

    state: GraphState = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "day": 2, "poi_name": "青城山", "edit_request": "补充一条后山线",
    }
    poi_detail_edit_node(state)

    assert captured_route_names["value"] == ["后山线"]  # 只核验新增的路线，已有的经典线不重复核验

    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert conflicts == []  # 新增1条路线，不算越权


# ---------------------------------------------------------------------------
# 场景3b：删除某条已有路线——整份覆盖write_agent_output，不算越权
# ---------------------------------------------------------------------------


def test_edit_deletes_existing_route(isolated_project, monkeypatch):
    _seed_detail(isolated_project, 2, "青城山", [_route_a(True), _route_b(False)])

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        # 用户要求删除"后山线"——走整份覆盖write_agent_output，只保留"经典线"
        _seed_detail(isolated_project, 2, "青城山", [_route_a(True)])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda detail, topic, route_names=None, **kwargs: [])

    state: GraphState = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "day": 2, "poi_name": "青城山", "edit_request": "把后山线这条路线删掉",
    }
    poi_detail_edit_node(state)

    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert [r.route_name for r in detail.routes] == ["经典线"]

    # _diff_poi_routes的removed分支会把被删除的路线计入touched，但删除本身只涉及1条
    # 路线，不该触发越权检测（跟"改1条内容"/"新增1条"同一个">1即可疑"阈值）
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert conflicts == []


# ---------------------------------------------------------------------------
# 场景4：越权改动检测——一次改动超过1条路线
# ---------------------------------------------------------------------------


def test_edit_flags_overedit_when_multiple_routes_changed(isolated_project, monkeypatch):
    _seed_detail(isolated_project, 2, "青城山", [_route_a(True), _route_b(False)])

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        # 用户只要求改"经典线"，但agent顺带把"后山线"的内容也改了——越权
        _seed_detail(isolated_project, 2, "青城山", [
            RouteOption(route_name="经典线", recommended=True, route_segments=[
                RouteSegment(order=1, point_name="山门", highlight="改过", sources=[]),
            ]),
            RouteOption(route_name="后山线", recommended=False, route_segments=[
                RouteSegment(order=1, point_name="也被改了", highlight="不该动", sources=[]),
            ]),
        ])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda detail, topic, route_names=None, **kwargs: [])

    state: GraphState = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "day": 2, "poi_name": "青城山", "edit_request": "把经典线的山门描述改一下",
    }
    poi_detail_edit_node(state)

    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl") or []
    assert len(conflicts) == 1
    assert conflicts[0]["target"] == "poi_detail_agent.day2_青城山"
    assert "经典线" in conflicts[0]["reason"] and "后山线" in conflicts[0]["reason"]
    assert conflicts[0]["issue_type"] == "poi_detail_edit_overreach"


# ---------------------------------------------------------------------------
# 场景5：首次生成——该POI此前没有详情文件
# ---------------------------------------------------------------------------


def test_edit_first_generation_when_no_existing_detail(isolated_project, monkeypatch):
    assert store.read_poi_detail(isolated_project, 3, "太古里") is None

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        _seed_detail(isolated_project, 3, "太古里", [_route_a(True)])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)

    captured_route_names = {}

    def fake_verify(detail, topic, route_names=None, **kwargs):
        captured_route_names["value"] = route_names
        return []

    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", fake_verify)

    state: GraphState = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "day": 3, "poi_name": "太古里", "edit_request": "帮我也生成一下太古里的深度玩法",
    }
    result = poi_detail_edit_node(state)

    assert captured_route_names["value"] is None  # 首次生成，全量核验
    assert "edit_original_poi_detail" not in result  # 首次生成没有基线快照
    assert store.read_poi_detail(isolated_project, 3, "太古里") is not None

    manifest = store.read_workspace(isolated_project, "outputs/poi_details/manifest.json")
    assert any(e["poi_name"] == "太古里" for e in manifest["generated"])


# ---------------------------------------------------------------------------
# 场景6：反向同步不会冲掉其它已生成POI的内容
# ---------------------------------------------------------------------------


def test_edit_resync_does_not_wipe_other_pois_from_final_guide(isolated_project, monkeypatch):
    _seed_detail(isolated_project, 1, "武侯祠", [_route_a(True)])
    _seed_detail(isolated_project, 2, "青城山", [_route_b(False)])
    store.write_poi_details_manifest(
        isolated_project, "main_agent",
        PoiDetailManifest(generated=[
            PoiDetailManifestEntry(poi_name="武侯祠", day=1, file="day1_武侯祠.json", citation_status="passed"),
            PoiDetailManifestEntry(poi_name="青城山", day=2, file="day2_青城山.json", citation_status="passed"),
        ]),
    )
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都3日游", meta={})
    # 先手动跑一次反向同步，模拟"树木生成中"阶段join节点已经把两个POI都写进final_guide
    orchestrator._sync_poi_details_into_final_guide(isolated_project, [
        {"day": 1, "poi_name": "武侯祠", "citation_status": "passed"},
        {"day": 2, "poi_name": "青城山", "citation_status": "passed"},
    ])
    guide_before = store.read_workspace(isolated_project, "final_guide.md")
    assert "武侯祠" in guide_before and "青城山" in guide_before

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        _seed_detail(isolated_project, 2, "青城山", [_route_b(True)])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda detail, topic, route_names=None, **kwargs: [])

    state: GraphState = {
        "project_id": isolated_project, "topic": "成都", "mode": "edit",
        "day": 2, "poi_name": "青城山", "edit_request": "把后山线设为推荐",
    }
    poi_detail_edit_node(state)

    guide_after = store.read_workspace(isolated_project, "final_guide.md")
    assert "武侯祠" in guide_after  # 没有被edit这次调用冲掉
    assert "青城山" in guide_after

    manifest = store.read_workspace(isolated_project, "outputs/poi_details/manifest.json")
    names = {e["poi_name"] for e in manifest["generated"]}
    assert names == {"武侯祠", "青城山"}


# ---------------------------------------------------------------------------
# 场景7：_route_edit_mode路由——一跳直接结束
# ---------------------------------------------------------------------------


def test_route_edit_mode_ends_after_poi_detail_agent():
    state = {"project_id": "x", "last_agent_result": {"agent": "poi_detail_agent", "summary": "已更新"}}
    assert _route_edit_mode(state) == END


def test_route_edit_mode_first_hop_returns_entry_target():
    state = {"project_id": "x", "entry_target": "poi_detail_edit_node", "last_agent_result": None}
    assert _route_edit_mode(state) == "poi_detail_edit_node"


# ---------------------------------------------------------------------------
# 场景8：run_edit()对day/poi_name的校验
# ---------------------------------------------------------------------------


def test_run_edit_requires_day_and_poi_name_for_poi_detail_edit_node(isolated_project):
    store.write_requirements(isolated_project, "成都", {"destination": "成都", "days": 3})
    with pytest.raises(ValueError, match="day.*poi_name|poi_name.*day"):
        run_edit(isolated_project, "poi_detail_edit_node", "改一下山门的描述")


def test_run_edit_passes_day_and_poi_name_into_initial_state(isolated_project, monkeypatch):
    store.write_requirements(isolated_project, "成都", {"destination": "成都", "days": 3})
    _seed_detail(isolated_project, 2, "青城山", [_route_a(True)])

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        _seed_detail(isolated_project, 2, "青城山", [_route_a(True)])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)
    monkeypatch.setattr(citation_check, "verify_poi_detail_citations", lambda detail, topic, route_names=None, **kwargs: [])

    result = run_edit(isolated_project, "poi_detail_edit_node", "确认一下山门描述没问题", day=2, poi_name="青城山")

    assert result["last_agent_result"]["agent"] == "poi_detail_agent"
