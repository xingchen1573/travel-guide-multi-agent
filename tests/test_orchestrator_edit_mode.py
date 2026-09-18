"""edit模式单元测试：edit_request是否真正拼进prompt、误改检测是否触发report_conflict、
级联场景是否正确退化为plan模式prompt、_route_edit_mode的citation_check分支。

不调用真实LLM——monkeypatch build_lifestyle_agent/build_integration_agent（返回哑对象，
不会被真正调用到网络）+ invoke_with_retry（用一个可控的副作用函数模拟"模型这次做了什么"，
直接操作store模拟write_agent_output/write_final_guide的效果）。见docs/ISSUES.md第28/29条。

误改检测（_is_edit_overreach/_infer_edit_scope）默认用_fail_light_model autouse fixture
让轻量LLM调用失败，强制走静态阈值1的降级路径——绝大多数测试验证的正是这条降级路径本身
（跟改动前"len(changed) > 1"完全等价），只有test_infer_edit_scope_*/test_*_dynamic_scope_*
这几个专门测动态语义比对的用例会显式monkeypatch get_light_chat_model提供受控返回值。
见docs/ARCHITECTURE.md第20.2节。
"""
import pytest
from types import SimpleNamespace

from src import orchestrator
from src.orchestrator import (
    _infer_edit_scope,
    _route_edit_mode,
    citation_check_node,
    integration_agent_node,
    lifestyle_agent_node,
    resume_plan_route_decision,
    route_draft_node,
)
from src.workspace import store
from src.workspace.schema import LIFESTYLE_FIELDS, RouteDay, RoutePlan, RoutePoi

from conftest import fake_agent_reply


@pytest.fixture(autouse=True)
def _fail_light_model(monkeypatch):
    """默认让get_light_chat_model调用失败，_infer_edit_scope据此返回None，_is_edit_overreach
    降级回退到静态阈值1判断——保证本文件里没有显式覆盖这个fixture的既有测试行为不变。"""

    def _raise(temperature=0):
        raise RuntimeError("测试环境不调用真实LLM")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", _raise)


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "edit-mode-test"
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


def _seed_lifestyle_multi_day(project_id: str) -> None:
    """跟_seed_lifestyle同款，但food/outfit/lodging/play各自有day1/2/3三条item——
    用于验证item级diff只标出真正变化的那一天，其余天数不该被牵连（docs/ISSUES.md第37补条）。"""
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {"field": f, "day": d, "recommendation": f"day{d}原始内容", "sources": [], "claimed_entities": []}
            for f in LIFESTYLE_FIELDS
            for d in (1, 2, 3)
        ],
    )


def _base_state(project_id: str, **overrides) -> dict:
    state = {
        "project_id": project_id,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": 3},
        "entry_target": "lifestyle_agent_node",
        "edit_request": "Day1午餐换一家甜品店",
        "last_agent_result": None,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# lifestyle_agent_node
# ---------------------------------------------------------------------------


def test_lifestyle_edit_prompt_contains_edit_request(isolated_project, monkeypatch):
    """核心断链修复验证：edit_request原文必须真正出现在拼给模型的prompt里。"""
    _seed_lifestyle(isolated_project)
    captured_prompts = []

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        _seed_lifestyle(isolated_project)
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _base_state(isolated_project)
    lifestyle_agent_node(state)

    assert captured_prompts, "invoke_with_retry应该被调用过"
    prompt = captured_prompts[0]
    assert "Day1午餐换一家甜品店" in prompt
    assert "综合产出美食/穿搭/住宿/游玩四个维度的具体建议" not in prompt


def test_lifestyle_plan_mode_prompt_has_no_edit_request(isolated_project, monkeypatch):
    """对照组：plan模式下不应该出现edit_request相关措辞（本来也没有edit_request字段）。"""
    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: (
            captured_prompts.append(messages[0]["content"]),
            _seed_lifestyle(isolated_project),
            fake_agent_reply(messages),
        )[2],
    )

    state = _base_state(isolated_project, mode="plan", entry_target=None, edit_request=None)
    lifestyle_agent_node(state)

    prompt = captured_prompts[0]
    assert "本工作单元只处理Day1" in prompt


def test_lifestyle_cascade_scenario_uses_plan_style_prompt(isolated_project, monkeypatch):
    """级联场景（entry_target不是lifestyle_agent_node，比如是route_draft）：
    即使mode=edit且带着edit_request，也不该套用定点修改prompt——这是route_agent改动
    级联触发lifestyle_agent重新审视的情况，语义上应该整体重新生成。"""
    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: (
            captured_prompts.append(messages[0]["content"]),
            _seed_lifestyle(isolated_project),
            fake_agent_reply(messages),
        )[2],
    )

    state = _base_state(
        isolated_project,
        entry_target="route_draft",  # 用户实际@的是route_agent，不是lifestyle_agent
        last_agent_result={"agent": "route_agent", "summary": "路线已调整"},
    )
    result = lifestyle_agent_node(state)

    prompt = captured_prompts[0]
    assert "本工作单元只处理Day1" in prompt
    assert "Day1午餐换一家甜品店" not in prompt
    # 级联场景不是"用户定点编辑"，不应该产生edit_changed_lifestyle_fields——citation_check_node
    # 据此回退到全量核验，这才是"整体重新生成就该整体重新核验"的正确语义（docs/ISSUES.md第37补条）。
    assert "edit_changed_lifestyle_fields" not in result


def test_lifestyle_diff_detection_no_conflict_when_single_field_changed(isolated_project, monkeypatch):
    """只改了1个字段（符合预期）：不应该触发report_conflict。"""
    _seed_lifestyle(isolated_project)

    def fake_invoke(agent, messages, **kwargs):
        # 模拟模型只改了food字段，其余三个保持不变
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [{"field": "food", "day": 1, "recommendation": "换成了甜品店", "sources": [], "claimed_entities": []}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _base_state(isolated_project)
    result = lifestyle_agent_node(state)

    assert store.get_pending_conflict(isolated_project) is None
    # docs/ISSUES.md第37补条：供citation_check_node收窄核验范围，只标出这次真的改了的字段
    assert result["edit_changed_lifestyle_fields"] == ["food"]
    # item级diff（比字段更细一档）：day1的食物内容变了，应该被算作("food", 1)一个改动项
    assert result["edit_changed_lifestyle_items"] == [["food", 1]]


def test_lifestyle_diff_detection_item_level_only_flags_changed_day(isolated_project, monkeypatch):
    """字段内有多天时，只有真正变化的那一天应该出现在item级diff结果里——
    这是第37补条"已知局限"要修的点：同字段其它天不该被无谓地牵连进核验scoping。"""
    _seed_lifestyle_multi_day(isolated_project)

    def fake_invoke(agent, messages, **kwargs):
        # 只改day1，day2/day3原样保留（对称原始快照里的三天），不需要重新提交
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [{"field": "food", "day": 1, "recommendation": "换成了甜品店", "sources": [], "claimed_entities": []}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _base_state(isolated_project)
    result = lifestyle_agent_node(state)

    assert result["edit_changed_lifestyle_fields"] == ["food"]
    # 只有day1变了，day2/day3不应该出现在item级diff结果里
    assert result["edit_changed_lifestyle_items"] == [["food", 1]]


def test_lifestyle_diff_detection_reports_conflict_when_multiple_fields_changed(isolated_project, monkeypatch):
    """真实复现过的bug场景：旧prompt下模型会把四个字段全部重新生成一遍。
    这里模拟同样的行为，验证diff检测确实会自动report_conflict打回。"""
    _seed_lifestyle(isolated_project)

    def fake_invoke(agent, messages, **kwargs):
        # 模拟模型把food/outfit/lodging/play全部改了（越权改动）
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": f, "day": 1, "recommendation": "全部重新生成", "sources": [], "claimed_entities": []}
                for f in LIFESTYLE_FIELDS
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _base_state(isolated_project)
    result = lifestyle_agent_node(state)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.target.split(".")[0] == "lifestyle_agent"
    assert "4" in conflict.reason or "['food'" in conflict.reason
    assert conflict.issue_type == "lifestyle_edit_overreach"
    # 越权检测（report_conflict）和核验scoping（edit_changed_lifestyle_fields）是两个独立的
    # 防护机制——即使触发了越权冲突，这次真实改动的4个字段依然应该被如实上报，供下游
    # citation_check_node核验（docs/ISSUES.md第37补条）。
    assert set(result["edit_changed_lifestyle_fields"]) == {"food", "outfit", "lodging", "play"}


def test_lifestyle_diff_baseline_persists_across_retries(isolated_project, monkeypatch):
    """基线固定性验证：第一轮已经把edit_original_lifestyle写进结果后，第二轮（模拟被打回
    重试）如果继续用state里的原始基线而不是"上一轮"的（已经错误的）快照对比，
    仍然应该能发现"相对最初版本，多个字段被改了"这件事。"""
    _seed_lifestyle(isolated_project, text="最初版本")

    def fake_invoke_round1(agent, messages, **kwargs):
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": f, "day": 1, "recommendation": "第1轮全改了", "sources": [], "claimed_entities": []}
                for f in LIFESTYLE_FIELDS
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_lifestyle_agent", lambda project_id, topic, **kwargs: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke_round1)

    state = _base_state(isolated_project)
    result1 = lifestyle_agent_node(state)
    assert "edit_original_lifestyle" in result1
    original = result1["edit_original_lifestyle"]
    assert original["food"]["items"][0]["recommendation"] == "最初版本"

    # 第2轮：状态里带着第1轮固定下来的基线，模拟模型在第1轮结果基础上"只保持不变"
    # （没有真正修复越权改动），diff检测理应仍然发现相对最初版本changed了多个字段。
    def fake_invoke_round2(agent, messages, **kwargs):
        # 重新提交同一份错误结果，证明本轮确实发生了写入，但没有修复越权内容。
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [
                {"field": f, "day": 1, "recommendation": "第1轮全改了", "sources": [], "claimed_entities": []}
                for f in LIFESTYLE_FIELDS
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke_round2)
    state2 = _base_state(isolated_project, edit_original_lifestyle=original)
    lifestyle_agent_node(state2)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None  # 第2轮依然能识别出相对最初基线的越权改动


# ---------------------------------------------------------------------------
# route_draft_node（定点编辑，粒度为"天"，见docs/ISSUES.md第46条）
# ---------------------------------------------------------------------------


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


def _route_edit_base_state(project_id: str, **overrides) -> dict:
    state = {
        "project_id": project_id,
        "topic": "成都",
        "mode": "edit",
        "requirements": {"destination": "成都", "days": 3},
        "entry_target": "route_draft",
        "edit_request": "把第二天改成去都江堰",
        "last_agent_result": None,
    }
    state.update(overrides)
    return state


def test_route_edit_prompt_contains_edit_request(isolated_project, monkeypatch):
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}, {"day": 2, "area": "郊区"}, {"day": 3, "area": "市区"}])
    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _route_edit_base_state(isolated_project)
    route_draft_node(state)

    prompt = captured_prompts[0]
    assert "把第二天改成去都江堰" in prompt
    assert "write_route_plan_patch" in prompt


def test_route_edit_cascade_scenario_uses_plan_style_prompt(isolated_project, monkeypatch):
    """级联场景（entry_target不是route_draft，比如是lifestyle_agent_node，被冲突打回route_agent）：
    不该套用定点编辑prompt，语义上应该整体重新生成，跟lifestyle_agent_node的同款测试对称。"""
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}])
    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _route_edit_base_state(
        isolated_project,
        entry_target="lifestyle_agent_node",  # 用户实际@的是lifestyle_agent，不是route_agent
    )
    result = route_draft_node(state)

    prompt = captured_prompts[0]
    assert "产出几天几夜的行程骨架" in prompt
    assert "把第二天改成去都江堰" not in prompt
    assert "edit_changed_route_days" not in result


def test_route_edit_no_conflict_when_single_day_changed(isolated_project, monkeypatch):
    """只改了1天（符合预期）：不应该触发report_conflict。"""
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}, {"day": 2, "area": "郊区"}, {"day": 3, "area": "市区"}])
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        # 模拟模型只改了day2，day1/day3保持不变
        _seed_route(
            isolated_project,
            [{"day": 1, "area": "市区"}, {"day": 2, "area": "都江堰"}, {"day": 3, "area": "市区"}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _route_edit_base_state(isolated_project)
    result = route_draft_node(state)

    assert store.get_pending_conflict(isolated_project) is None
    assert result["edit_changed_route_days"] == [2]


def test_route_edit_reports_conflict_when_multiple_days_changed(isolated_project, monkeypatch):
    """改了2天以上（模拟模型越权多改）：应该report_conflict留痕。"""
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}, {"day": 2, "area": "郊区"}, {"day": 3, "area": "市区"}])
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        # 模拟模型越权改了day1和day2
        _seed_route(
            isolated_project,
            [{"day": 1, "area": "改动过的市区"}, {"day": 2, "area": "都江堰"}, {"day": 3, "area": "市区"}],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _route_edit_base_state(isolated_project)
    route_draft_node(state)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.target.startswith("route_agent.day")
    assert conflict.issue_type == "route_edit_overreach"


def test_route_edit_no_false_positive_when_day_count_changes(isolated_project, monkeypatch):
    """天数变化（模拟"加一天"场景）：days数量从3变4，不应该误报冲突——这是已知局限的正面验证，
    不强行对不同长度的days列表做逐天比对。"""
    _seed_route(isolated_project, [{"day": 1, "area": "市区"}, {"day": 2, "area": "郊区"}, {"day": 3, "area": "市区"}])
    monkeypatch.setattr(orchestrator, "build_route_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        _seed_route(
            isolated_project,
            [
                {"day": 1, "area": "市区"},
                {"day": 2, "area": "郊区"},
                {"day": 3, "area": "市区"},
                {"day": 4, "area": "新加的一天"},
            ],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _route_edit_base_state(isolated_project, edit_request="帮我加一天")
    route_draft_node(state)

    assert store.get_pending_conflict(isolated_project) is None


# ---------------------------------------------------------------------------
# integration_agent_node
# ---------------------------------------------------------------------------


def test_integration_edit_prompt_contains_edit_request(isolated_project, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="#原始攻略\n" + "内容" * 100, meta={})
    captured_prompts = []
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        captured_prompts.append(messages[0]["content"])
        store.write_final_guide(isolated_project, actor="integration_agent", content_md="#原始攻略\n" + "内容" * 100, meta={})
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _base_state(
        isolated_project,
        entry_target="integration_agent_node",
        edit_request="必备清单里加一把雨伞",
        last_agent_result=None,
    )
    integration_agent_node(state)

    assert "必备清单里加一把雨伞" in captured_prompts[0]


def test_integration_diff_detection_reports_conflict_on_length_drop(isolated_project, monkeypatch):
    original_content = "#原始攻略\n" + "详细内容" * 100  # 400+字符
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=original_content, meta={})
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        # 模拟模型把正文压缩了——长度要求：> 200（避开_integration_done的"没写完"判断），
        # 但 < 原文70%（触发骤降检测本身）。原文约406字符，70%约284，这里控制在228左右。
        store.write_final_guide(
            isolated_project, actor="integration_agent", content_md="#压缩后的攻略\n" + "少量内容" * 55, meta={}
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _base_state(
        isolated_project,
        entry_target="integration_agent_node",
        edit_request="必备清单里加一把雨伞",
        last_agent_result=None,
    )
    integration_agent_node(state)

    conflict = store.get_pending_conflict(isolated_project)
    assert conflict is not None
    assert conflict.target == "integration_agent"


def test_integration_no_conflict_when_length_stable(isolated_project, monkeypatch):
    original_content = "#原始攻略\n\n## 行前必备清单\n" + "详细内容" * 100
    valid_meta = {"merged_fields": [], "checklist_included": True}
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=original_content, meta=valid_meta)
    monkeypatch.setattr(orchestrator, "build_integration_agent", lambda project_id, topic, **kwargs: object())

    def fake_invoke(agent, messages, **kwargs):
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md=original_content + "\n补充：带把雨伞",
            meta=valid_meta,
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = _base_state(
        isolated_project,
        entry_target="integration_agent_node",
        edit_request="必备清单里加一把雨伞",
        last_agent_result=None,
    )
    integration_agent_node(state)

    assert store.get_pending_conflict(isolated_project) is None


# ---------------------------------------------------------------------------
# _route_edit_mode: citation_check接入
# ---------------------------------------------------------------------------


def test_route_edit_mode_first_hop_returns_entry_target(isolated_project):
    state = _base_state(isolated_project, last_agent_result=None)
    assert _route_edit_mode(state) == "lifestyle_agent_node"


def test_route_edit_mode_routes_to_citation_check_after_lifestyle(isolated_project):
    state = _base_state(
        isolated_project,
        last_agent_result={"agent": "lifestyle_agent", "summary": "衣食住行产出完成"},
        edit_citation_checked=False,
    )
    assert _route_edit_mode(state) == "citation_check_node"


def test_route_edit_mode_does_not_repeat_citation_check(isolated_project):
    """核验做完一次后（edit_citation_checked=True），不应该无限循环回citation_check_node。"""
    state = _base_state(
        isolated_project,
        last_agent_result={"agent": "citation_checker", "summary": "引用核验完成"},
        edit_citation_checked=True,
        lifestyle_agent_done=True,
        integration_done=False,
    )
    # lifestyle_agent的feeds_integration=True，核验完且没有下游，应该走向整合环节
    next_node = _route_edit_mode(state)
    assert next_node != "citation_check_node"


def test_route_edit_mode_route_agent_cascades_to_lifestyle(isolated_project):
    """route_agent作为edit入口完成后，先过route_citation_check_node+route_timeliness_check_node
    （两个checked=True代表已经跑过），再级联到lifestyle_agent（依赖表决定）。"""
    state = _base_state(
        isolated_project,
        entry_target="route_draft",
        last_agent_result={"agent": "route_agent", "summary": "路线骨架已生成"},
        route_citation_checked=True,
        route_map_checked=True,
        route_timeliness_checked=True,
        lifestyle_agent_done=False,
    )
    assert _route_edit_mode(state) == "lifestyle_agent_node"


def test_route_edit_mode_route_agent_goes_to_citation_check_first(isolated_project):
    """route_agent完成后，还没过citation check时（route_citation_checked未设置），
    必须先去route_citation_check_node，不能直接跳到lifestyle_agent级联。"""
    state = _base_state(
        isolated_project,
        entry_target="route_draft",
        last_agent_result={"agent": "route_agent", "summary": "路线骨架已生成"},
    )
    assert _route_edit_mode(state) == "route_citation_check_node"


def test_route_edit_mode_route_agent_goes_to_map_after_citation(isolated_project):
    """route_agent完成、route_citation_check跑完（route_citation_checked=True）但
    route_timeliness_check还没跑时，下一步该去route_timeliness_check_node，
    不能直接级联到lifestyle_agent。"""
    state = _base_state(
        isolated_project,
        entry_target="route_draft",
        last_agent_result={"agent": "route_agent", "summary": "路线骨架已生成"},
        route_citation_checked=True,
        # route_timeliness_checked 未设置
        lifestyle_agent_done=False,
    )
    assert _route_edit_mode(state) == "route_map_check_node"


def test_route_edit_mode_route_citation_checker_source_resolves_to_route_agent(isolated_project):
    """route_citation_check_node跑完后，last_agent_result带着source="route"标记，
    _route_edit_mode应该正确解析成route_agent（依赖表downstream=["lifestyle_agent"]），
    不能被旧的flag heuristic误判成lifestyle_agent（那样会跳过pitfall_agent，见docs/ISSUES.md第46条）。

    注意：citation_check跑完后还要过route_timeliness_check_node（timeliness还没跑），
    所以这里期望返回 timeliness 节点，不是 lifestyle_agent。"""
    state = _base_state(
        isolated_project,
        entry_target="route_draft",
        last_agent_result={"agent": "citation_checker", "summary": "路线引用核验完成", "source": "route"},
        route_citation_checked=True,
        # route_timeliness_checked 未设置
        lifestyle_agent_done=False,
    )
    assert _route_edit_mode(state) == "route_map_check_node"


def test_route_edit_mode_route_map_checker_goes_to_timeliness(isolated_project):
    state = _base_state(
        isolated_project,
        entry_target="route_draft",
        last_agent_result={"agent": "citation_checker", "summary": "路线地图核验完成", "source": "route_map"},
        route_citation_checked=True,
        route_map_checked=True,
        lifestyle_agent_done=False,
    )
    assert _route_edit_mode(state) == "route_timeliness_check_node"


def test_route_edit_mode_route_timeliness_checker_cascades_to_lifestyle(isolated_project):
    """route_timeliness_check_node跑完后，last_agent_result带着source="route_timeliness"标记，
    且两个核验都已完成（route_citation_checked + route_timeliness_checked），应该级联到
    lifestyle_agent（source="route_timeliness" 映射到route_agent，依赖表downstream=["lifestyle_agent"]）。"""
    state = _base_state(
        isolated_project,
        entry_target="route_draft",
        last_agent_result={"agent": "citation_checker", "summary": "路线时效核验完成", "source": "route_timeliness"},
        route_citation_checked=True,
        route_map_checked=True,
        route_timeliness_checked=True,
        lifestyle_agent_done=False,
    )
    assert _route_edit_mode(state) == "lifestyle_agent_node"


def test_route_edit_mode_rechecks_citations_after_timeliness_fact_repair(isolated_project):
    state = _base_state(
        isolated_project,
        entry_target="route_draft",
        last_agent_result={
            "agent": "citation_checker",
            "summary": "路线可执行性修复了Day1事实",
            "source": "route_timeliness",
        },
        route_citation_checked=False,
        route_map_checked=True,
        route_timeliness_checked=True,
        route_recheck_days=[1],
        lifestyle_agent_done=False,
    )

    assert _route_edit_mode(state) == "route_citation_check_node"


def test_route_edit_mode_citation_checker_without_source_falls_back_to_legacy_heuristic(isolated_project):
    """回归测试：lifestyle/pitfall两条既有路径的citation_checker完成时不带source字段，
    行为必须跟改动前完全一致（走pitfall_citation_checked的flag heuristic），不受route分支
    影响——这里复用tests/test_orchestrator_pitfall.py里已有的同类断言逻辑，只额外确认
    effective_agent没有被误判成route_agent（不会查AGENT_DEPENDENCY_TABLE["route_agent"]）。"""
    state = _base_state(
        isolated_project,
        entry_target="pitfall_agent_node",
        last_agent_result={"agent": "citation_checker", "summary": "避雷内容引用核验完成"},
        pitfall_citation_checked=True,
        integration_done=False,
    )
    # pitfall_agent的downstream为空、feeds_integration=True，没有source字段时应该走原有
    # heuristic正确解析成pitfall_agent，直接进整合阶段（没有pending conflict时是
    # integration_agent_node），不会被误判成route_agent（那样会去查
    # AGENT_DEPENDENCY_TABLE["route_agent"]的downstream=["lifestyle_agent"]）。
    assert _route_edit_mode(state) == "integration_agent_node"


def test_route_integration_phase_for_integration_agent_target(isolated_project):
    """target=integration_agent的conflict必须路由回integration_agent_node，不能落进
    默认的lifestyle_agent_node分支（本次修复过程中发现的一个真实bug）。"""
    from src.orchestrator import _route_integration_phase

    store.report_conflict(isolated_project, actor="citation_checker", target="integration_agent", reason="正文长度骤降")
    state = {"project_id": isolated_project, "integration_retry_count": 0}
    assert _route_integration_phase(state) == "integration_agent_node"


def test_resume_plan_route_decision_only_resumes_route_confirmation(monkeypatch):
    invoked = []

    class FakeGraph:
        def get_state(self, config):
            assert config == {"configurable": {"thread_id": "route-decision:plan"}}
            return SimpleNamespace(
                values={"project_id": "route-decision"},
                next=("await_route_structure_confirm",),
            )

        def invoke(self, command, config):
            invoked.append((command, config))
            return {"status": "resumed"}

    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: FakeGraph())

    result = resume_plan_route_decision("route-decision", "keep")

    assert result == {"status": "resumed"}
    command, config = invoked[0]
    assert command.resume == "keep"
    assert command.update["turn_id"]
    assert config == {"configurable": {"thread_id": "route-decision:plan"}}


def test_resume_plan_route_decision_rejects_unrelated_checkpoint(monkeypatch):
    class FakeGraph:
        def get_state(self, config):
            return SimpleNamespace(
                values={"project_id": "route-decision"},
                next=("await_initial_route_confirm",),
            )

    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: FakeGraph())

    with pytest.raises(ValueError, match="不在路线结构确认节点"):
        resume_plan_route_decision("route-decision", "keep")
