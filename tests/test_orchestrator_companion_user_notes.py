"""用户备注/annotation layer 被动带出（docs/ARCHITECTURE.md第21.5节阶段3）的单元测试：

覆盖范围：
1. `_extract_day_and_poi`：窄版正则抽day（"Day N"/"第N天"）、route_plan真实POI名字面匹配；
   用户没说具体天/POI时返回(None, None)；POI不在route_plan里时匹配不上（保守设计）。
2. `_relevant_user_notes`：按day筛天级备注、按poi筛景点级备注、都None返回空、
   过滤deleted（软删的条目不在list_user_notes返回里，间接验证）、过滤superseded旧条
   只留最新（update_user_note把旧条标deleted=true，list不返回旧条）。
3. 四个companion节点的mock测试：构造带"用户备注"上下文的prompt，断言注入逻辑在
   "用户话提到Day N+有该天备注"时注入、"用户话没提具体天"时不注入、"有备注但全deleted"
   时不注入。重点测注入条件，不测LLM是否真转述（那是真实E2E的事）。

跟test_orchestrator_companion.py同款isolated_project fixture+monkeypatch风格。
"""
import pytest

from src import orchestrator
from src.orchestrator import (
    _extract_day_and_poi,
    _format_user_notes_block,
    _relevant_user_notes,
    companion_lifestyle_query_node,
    companion_pitfall_query_node,
    guide_agent_node,
    realtime_agent_node,
)
from src.tools.guide_tools import make_guide_tools
from src.tools.material_tools import make_material_tools
from src.tools.realtime_tools import make_realtime_tools
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store
from src.workspace.schema import (
    RouteDay,
    RoutePlan,
    RoutePoi,
)

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "companion-notes-test"
    store.init_project(project_id)
    route_plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                pois=[RoutePoi(name="宽窄巷子"), RoutePoi(name="人民公园")],
            ),
            RouteDay(
                day=2,
                city="成都",
                pois=[RoutePoi(name="武侯祠"), RoutePoi(name="锦里古街")],
            ),
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=route_plan)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})
    return project_id


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    def __init__(self, content):
        self._content = content

    def invoke(self, messages):
        return _FakeResponse(self._content)


# ---------------------------------------------------------------------------
# _extract_day_and_poi：窄版day正则 + route_plan真实POI字面匹配
# ---------------------------------------------------------------------------


def test_extract_day_matches_day_n_pattern():
    rp = store.read_route_plan  # 不会用到，传None即可
    day, poi = _extract_day_and_poi("Day2有什么好玩的", None)
    assert day == 2
    assert poi is None
    del rp


def test_extract_day_matches_chinese_day_pattern():
    day, _ = _extract_day_and_poi("第3天去哪", None)
    assert day == 3


def test_extract_day_returns_none_when_no_explicit_day():
    day, _ = _extract_day_and_poi("今天去哪玩", None)
    assert day is None  # 相对日期不解析，是窄版已知局限（docs/ARCHITECTURE.md第21.5节）


def test_extract_poi_matches_route_plan_real_poi_name(isolated_project):
    rp = store.read_route_plan(isolated_project)
    day, poi = _extract_day_and_poi("武侯祠有什么好玩的", rp)
    assert poi == "武侯祠"
    assert day is None


def test_extract_poi_does_not_match_nonexistent_poi(isolated_project):
    """保守设计：POI不在route_plan真实景点集合里时匹配不上→poi返回None，
    不做模糊匹配（跟_extract_user_note_fields/孤儿备注判定同款，见§21.7阶段2已知局限②）。"""
    rp = store.read_route_plan(isolated_project)
    _, poi = _extract_day_and_poi("三星堆有什么好玩的", rp)
    assert poi is None


def test_extract_both_day_and_poi(isolated_project):
    rp = store.read_route_plan(isolated_project)
    day, poi = _extract_day_and_poi("Day2宽窄巷子怎么走", rp)
    assert day == 2
    assert poi == "宽窄巷子"


def test_extract_returns_none_none_when_nothing_matchable(isolated_project):
    rp = store.read_route_plan(isolated_project)
    day, poi = _extract_day_and_poi("成都有什么好吃的", rp)
    assert day is None
    assert poi is None


# ---------------------------------------------------------------------------
# _relevant_user_notes：按day/poi筛未删备注
# ---------------------------------------------------------------------------


def test_relevant_user_notes_empty_when_day_and_poi_both_none(isolated_project):
    """行程级备注不靠这条companion路径带出（避免每次塞一堆无关行程级备注），
    见docs/ARCHITECTURE.md第21.5节。"""
    store.write_user_note(isolated_project, "admin_user", "整体节奏偏紧")  # 行程级
    assert _relevant_user_notes(isolated_project, day=None, poi_name=None) == []


def test_relevant_user_notes_filters_by_day_picks_day_level_notes(isolated_project):
    store.write_user_note(isolated_project, "admin_user", "第一天步行多", day=1)
    store.write_user_note(isolated_project, "admin_user", "第二天要早起", day=2)
    notes = _relevant_user_notes(isolated_project, day=1, poi_name=None)
    assert len(notes) == 1
    assert notes[0]["note_text"] == "第一天步行多"
    assert notes[0]["day"] == 1


def test_relevant_user_notes_filters_by_poi_picks_poi_level_notes(isolated_project):
    store.write_user_note(isolated_project, "admin_user", "武侯祠要请讲解", day=2, poi_name="武侯祠")
    store.write_user_note(isolated_project, "admin_user", "锦里商业化重", day=2, poi_name="锦里古街")
    notes = _relevant_user_notes(isolated_project, day=None, poi_name="武侯祠")
    assert len(notes) == 1
    assert notes[0]["note_text"] == "武侯祠要请讲解"
    assert notes[0]["poi_name"] == "武侯祠"


def test_relevant_user_notes_excludes_poi_level_note_of_other_poi_on_same_day(isolated_project):
    """给day带天级+景点级备注，但不能把"绑别的POI的景点级备注因为同一天就带出来"
    ——那是关于另一个POI的话，跟当前问题无关（见_relevant_user_notes注释）。"""
    store.write_user_note(isolated_project, "admin_user", "天级备注", day=2)
    store.write_user_note(isolated_project, "admin_user", "武侯祠的备注", day=2, poi_name="武侯祠")
    notes = _relevant_user_notes(isolated_project, day=2, poi_name=None)
    # 只带天级那条，不带武侯祠的景点级那条（poi_name=None时，景点级备注不该被day误命中）
    assert len(notes) == 1
    assert notes[0]["note_text"] == "天级备注"


def test_relevant_user_notes_excludes_deleted(isolated_project):
    """软删的条目不在list_user_notes返回里（store层已过滤），_relevant_user_notes
    间接继承——这里验证软删后确实带不出。"""
    note = store.write_user_note(isolated_project, "admin_user", "会被删的备注", day=1)
    store.delete_user_note(isolated_project, "admin_user", note.note_id)
    assert _relevant_user_notes(isolated_project, day=1, poi_name=None) == []


def test_relevant_user_notes_filters_superseded_keeps_only_latest(isolated_project):
    """update_user_note把旧条标deleted=true+append新条（superseded链），list只返回新条
    （store层已过滤deleted），_relevant_user_notes间接只带最新那条。"""
    old = store.write_user_note(isolated_project, "admin_user", "旧内容", day=1)
    new = store.update_user_note(isolated_project, "admin_user", old.note_id, "新内容", day=1)
    notes = _relevant_user_notes(isolated_project, day=1, poi_name=None)
    assert len(notes) == 1
    assert notes[0]["note_text"] == "新内容"
    assert notes[0]["note_text"] != "旧内容"


def test_relevant_user_notes_returns_both_day_and_poi_when_both_given(isolated_project):
    """同时给day和poi_name：绑该POI的景点级备注 + 该天的天级备注都该带上。"""
    store.write_user_note(isolated_project, "admin_user", "天级备注", day=2)
    store.write_user_note(isolated_project, "admin_user", "武侯祠景点级", day=2, poi_name="武侯祠")
    notes = _relevant_user_notes(isolated_project, day=2, poi_name="武侯祠")
    texts = {n["note_text"] for n in notes}
    assert texts == {"天级备注", "武侯祠景点级"}


# ---------------------------------------------------------------------------
# _format_user_notes_block：空列表返回空串、非空拼成上下文段
# ---------------------------------------------------------------------------


def test_format_user_notes_block_empty_returns_empty_string():
    assert _format_user_notes_block([]) == ""


def test_format_user_notes_block_non_empty_includes_marker_and_text():
    block = _format_user_notes_block(
        [{"note_text": "要提前订", "created_at": "2026-08-21T10:00:00Z", "day": 2, "poi_name": "武侯祠"}]
    )
    assert "用户备注" in block  # 标注是用户备注（不是已核验的攻略事实），见§21.1
    assert "未经核验" not in block  # 不再标"未经核验"——用户自己记的本来不用核验
    assert "要提前订" in block
    assert "2026-08-21T10:00:00Z" in block


# ---------------------------------------------------------------------------
# 四个companion节点：mock测试注入条件
# ---------------------------------------------------------------------------


def test_guide_agent_node_injects_user_notes_when_day_matches(isolated_project, monkeypatch):
    """用户话提到Day1 + 有该天未删备注 → 备注段真的被拼进发给模型的prompt。"""
    store.write_user_note(isolated_project, "admin_user", "第一天步行多穿舒适鞋", day=1)
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)

    captured_prompts = []

    def fake_invoke(agent, messages):
        captured_prompts.append(messages[0]["content"])
        agent["text"] = "Day1安排是宽窄巷子。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "companion_question": "Day1去哪玩？"}
    guide_agent_node(state)

    assert "第一天步行多穿舒适鞋" in captured_prompts[0]
    assert "用户备注" in captured_prompts[0]


def test_guide_agent_node_does_not_inject_when_no_explicit_day(isolated_project, monkeypatch):
    """用户话没提具体天（"成都有什么好吃的"）→ 不注入备注段，prompt里不应该出现备注标记。"""
    store.write_user_note(isolated_project, "admin_user", "整体节奏偏紧")  # 行程级
    store.write_user_note(isolated_project, "admin_user", "第一天备注", day=1)
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)

    captured_prompts = []

    def fake_invoke(agent, messages):
        captured_prompts.append(messages[0]["content"])
        agent["text"] = "回答。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "companion_question": "成都有什么好吃的"}
    guide_agent_node(state)

    assert "未经核验" not in captured_prompts[0]
    assert "整体节奏偏紧" not in captured_prompts[0]
    assert "第一天备注" not in captured_prompts[0]
    assert "用户备注" not in captured_prompts[0]  # 完全没注入备注段


def test_guide_agent_node_does_not_inject_when_all_notes_deleted(isolated_project, monkeypatch):
    """有绑该天的备注但全deleted → list_user_notes已过滤，_relevant_user_notes返回空，
    prompt里不出现备注段（"没给就不提"的注入侧保障，见guide_agent prompt规则6）。"""
    note = store.write_user_note(isolated_project, "admin_user", "已删的备注", day=1)
    store.delete_user_note(isolated_project, "admin_user", note.note_id)
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)

    captured_prompts = []

    def fake_invoke(agent, messages):
        captured_prompts.append(messages[0]["content"])
        agent["text"] = "回答。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "companion_question": "Day1去哪玩？"}
    guide_agent_node(state)

    assert "已删的备注" not in captured_prompts[0]
    assert "用户备注" not in captured_prompts[0]  # 全deleted→不注入备注段


def test_guide_agent_node_injects_when_poi_matches(isolated_project, monkeypatch):
    """用户话提到route_plan真实POI名 + 有绑该POI的未删备注 → 注入。"""
    store.write_user_note(isolated_project, "admin_user", "武侯祠要请讲解", day=2, poi_name="武侯祠")
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)

    captured_prompts = []

    def fake_invoke(agent, messages):
        captured_prompts.append(messages[0]["content"])
        agent["text"] = "武侯祠玩法。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "companion_question": "武侯祠怎么玩"}
    guide_agent_node(state)

    assert "武侯祠要请讲解" in captured_prompts[0]


def test_realtime_agent_node_injects_user_notes_when_day_matches(isolated_project, monkeypatch):
    store.write_user_note(isolated_project, "admin_user", "第二天提醒带伞", day=2)
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)

    captured_prompts = []

    def fake_invoke(agent, messages):
        captured_prompts.append(messages[0]["content"])
        agent["mode"] = "answer"
        agent["text"] = "第二天多云。"
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "companion_question": "Day2天气怎么样"}
    realtime_agent_node(state)

    assert "第二天提醒带伞" in captured_prompts[0]


def test_realtime_agent_node_does_not_inject_when_no_explicit_day(isolated_project, monkeypatch):
    store.write_user_note(isolated_project, "admin_user", "第一天备注", day=1)
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)

    captured_prompts = []

    def fake_invoke(agent, messages):
        captured_prompts.append(messages[0]["content"])
        agent["mode"] = "answer"
        agent["text"] = "回答。"
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "companion_question": "现在天气怎么样"}
    realtime_agent_node(state)

    assert "第一天备注" not in captured_prompts[0]
    assert "用户备注" not in captured_prompts[0]


def test_companion_lifestyle_query_injects_user_notes_when_poi_matches(isolated_project, monkeypatch):
    """companion_lifestyle_query_node：用户话提到POI + 有绑该POI的备注 → 备注段拼进user message。"""
    store.write_user_note(isolated_project, "admin_user", "宽窄巷子要早去", day=1, poi_name="宽窄巷子")
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")

    captured_messages = []

    def fake_invoke(agent, messages):
        captured_messages.append(messages[0]["content"])
        return {"messages": [_FakeMessage("附近推荐老字号钟水饺。")]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "宽窄巷子附近吃什么",
    }
    companion_lifestyle_query_node(state)

    assert "宽窄巷子要早去" in captured_messages[0]
    assert "用户备注" in captured_messages[0]


def test_companion_lifestyle_query_does_not_inject_when_no_explicit_anchor(isolated_project, monkeypatch):
    """用户话没提具体天/POI（"附近有什么好吃的"）→ 不注入备注段。"""
    store.write_user_note(isolated_project, "admin_user", "整体节奏偏紧")  # 行程级
    store.write_user_note(isolated_project, "admin_user", "第一天备注", day=1)
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")

    captured_messages = []

    def fake_invoke(agent, messages):
        captured_messages.append(messages[0]["content"])
        return {"messages": [_FakeMessage("推荐钟水饺。")]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "附近有什么好吃的",
    }
    companion_lifestyle_query_node(state)

    assert "未经核验" not in captured_messages[0]
    assert "整体节奏偏紧" not in captured_messages[0]
    assert "第一天备注" not in captured_messages[0]
    assert "用户备注" not in captured_messages[0]


def test_companion_pitfall_query_injects_user_notes_when_poi_matches(isolated_project, monkeypatch):
    """companion_pitfall_query_node：用户话提到POI + 有绑该POI的备注 → 注入。
    也要验证它原有的"已有pitfall.json塞进上下文"行为不被这次改动破坏。"""
    store.write_user_note(isolated_project, "admin_user", "武侯祠要小心讲解推销", day=2, poi_name="武侯祠")
    # seed一点pitfall内容，验证原行为 + 备注注入并存
    store.write_pitfall_output_patch(
        isolated_project,
        "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "原始美食推荐", "warning": "鹤鸣茶社人流量极大"}],
    )
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")

    captured_messages = []

    def fake_invoke(agent, messages):
        captured_messages.append(messages[0]["content"])
        return {"messages": [_FakeMessage("这家店确实有人反馈味道偏咸。")]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "武侯祠有没有坑",
    }
    companion_pitfall_query_node(state)

    # 备注段注入
    assert "武侯祠要小心讲解推销" in captured_messages[0]
    assert "用户备注" in captured_messages[0]
    # 原有pitfall上下文仍在（不被这次改动破坏）
    assert "鹤鸣茶社人流量极大" in captured_messages[0]


def test_companion_pitfall_query_does_not_inject_when_no_explicit_anchor(isolated_project, monkeypatch):
    store.write_user_note(isolated_project, "admin_user", "整体节奏偏紧")  # 行程级
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")

    captured_messages = []

    def fake_invoke(agent, messages):
        captured_messages.append(messages[0]["content"])
        return {"messages": [_FakeMessage("暂时没查到相关避雷内容。")]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "这家店有没有坑",
    }
    companion_pitfall_query_node(state)

    assert "未经核验" not in captured_messages[0]
    assert "整体节奏偏紧" not in captured_messages[0]
