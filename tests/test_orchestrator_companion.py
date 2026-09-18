"""陪伴阶段（docs/ORCHESTRATION.md第3.4节/docs/ARCHITECTURE.md第3.5节）单元测试：
向导agent工具边界、意图分类四分支（含避雷agent）、自动串联合并结果、写入审批边界
（高风险重点）、companion_pitfall_query_node（避雷agent陪伴阶段轻量查询节点）、以及
显式@pitfall_agent_node既有edit模式路径不受这次新增路由影响的回归验证。

不调用真实LLM——monkeypatch build_guide_agent/build_realtime_agent/create_react_agent/
invoke_with_retry/_classify_companion_intent，跟tests/test_orchestrator_edit_mode.py、
tests/test_orchestrator_poi_details.py同款风格。

captured-dict边车容器的monkeypatch技巧：guide_agent_node/realtime_agent_node内部会
`captured: dict = {}`后传给build_guide_agent/build_realtime_agent——测试里把这两个
build函数monkeypatch成"直接把captured原样当作agent返回"，配合monkeypatch后的
invoke_with_retry(agent, messages)去修改这个"agent"（其实就是同一个captured字典），
两边操作的是同一个对象，不需要额外的边车传递机制。
"""
import pytest

from langgraph.types import Command

from src import companion_history, orchestrator
from src.orchestrator import (
    _classify_companion_intent,
    _route_after_guide_agent,
    _route_edit_mode,
    await_realtime_approval_node,
    companion_lifestyle_query_node,
    companion_pitfall_query_node,
    companion_culture_query_node,
    guide_agent_node,
    realtime_agent_node,
    run_companion,
    resume_companion,
)
from src.tools.guide_tools import make_guide_tools
from src.tools.material_tools import make_material_tools
from src.tools.realtime_tools import make_realtime_tools
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store
from src.workspace.schema import (
    RoutePlan,
)

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "companion-test"
    store.init_project(project_id)
    store.write_route_plan(project_id, actor="route_agent", route_plan=RoutePlan(days=[]))
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})
    return project_id


def _seed_lifestyle(project_id: str, food_text: str = "原始美食推荐") -> None:
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": food_text, "sources": [], "claimed_entities": []}],
    )


def _seed_pitfall(project_id: str, warning_text: str = "原始避雷内容") -> None:
    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "原始美食推荐", "warning": warning_text}],
    )


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    def __init__(self, content):
        self._content = content

    def invoke(self, messages):
        return _FakeResponse(self._content)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


# ---------------------------------------------------------------------------
# guide_agent工具边界：只读，没有任何write类工具
# ---------------------------------------------------------------------------


def test_guide_agent_tools_have_no_write_tool():
    tools = make_workspace_tools("some-project", "guide_agent") + make_guide_tools({})
    names = {t.name for t in tools}
    assert "read_workspace" in names
    assert "answer" in names
    assert not any(n.startswith("write_") for n in names)


def test_realtime_agent_tools_have_no_direct_write_tool():
    """realtime_agent自己的工具集合里也不应该有能直接写workspace的工具——
    它只能通过propose_patch产出待审批的patch，真正的写入在await_realtime_approval_node
    审批通过后由系统代码调用store.write_agent_output，不是realtime_agent自己的工具权限。"""
    tools = make_workspace_tools("some-project", "realtime_agent") + make_realtime_tools("some-project", {})
    names = {t.name for t in tools}
    assert "read_workspace" in names
    assert {
        "answer_query",
        "propose_patch",
        "get_current_time",
        "create_reminder",
        "list_reminders",
        "cancel_reminder_tool",
        "update_reminder_tool",
    } <= names
    assert not any(n.startswith("write_") for n in names)


# ---------------------------------------------------------------------------
# 意图分类四分支（含避雷agent）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fake_response,expected",
    [
        ("guide_agent", "guide_agent"),
        ("realtime_agent", "realtime_agent"),
        ("lifestyle_agent", "lifestyle_agent"),
        ("pitfall_agent", "pitfall_agent"),
    ],
)
def test_classify_companion_intent_four_branches(monkeypatch, fake_response, expected):
    monkeypatch.setattr(orchestrator, "build_companion_router_model", lambda temperature=0: _FakeModel(fake_response))
    result = _classify_companion_intent(
        "随便什么问题", candidates=["guide_agent", "realtime_agent", "lifestyle_agent", "pitfall_agent"]
    )
    assert result == expected


def test_classify_companion_intent_falls_back_to_first_candidate_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(orchestrator, "build_companion_router_model", lambda temperature=0: _FakeModel("我不知道"))
    result = _classify_companion_intent("随便什么问题", candidates=["guide_agent", "realtime_agent"])
    assert result == "guide_agent"


# ---------------------------------------------------------------------------
# guide_agent_node + _route_after_guide_agent：覆盖度够/不够两条路径
# ---------------------------------------------------------------------------


def test_guide_agent_node_covered_sets_companion_answer_and_ends(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)

    def fake_invoke(agent, messages):
        agent["text"] = "Day1安排是武侯祠+锦里。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "companion_question": "Day1去哪玩？"}
    result = guide_agent_node(state)

    assert result["guide_covered"] is True
    assert result["companion_answer"] == "Day1安排是武侯祠+锦里。"
    assert _route_after_guide_agent(result) == orchestrator.END


def test_route_after_guide_agent_not_covered_routes_to_realtime(monkeypatch):
    monkeypatch.setattr(orchestrator, "_classify_companion_intent", lambda msg, candidates: "realtime_agent")
    state = {"guide_covered": False, "companion_question": "这家店现在还开门吗？"}
    assert _route_after_guide_agent(state) == "realtime_agent_node"


def test_route_after_guide_agent_not_covered_routes_to_lifestyle_query(monkeypatch):
    monkeypatch.setattr(orchestrator, "_classify_companion_intent", lambda msg, candidates: "lifestyle_agent")
    state = {"guide_covered": False, "companion_question": "附近还有什么好吃的？"}
    assert _route_after_guide_agent(state) == "companion_lifestyle_query_node"


def test_route_after_guide_agent_not_covered_routes_to_pitfall_query(monkeypatch):
    monkeypatch.setattr(orchestrator, "_classify_companion_intent", lambda msg, candidates: "pitfall_agent")
    state = {"guide_covered": False, "companion_question": "这家店有没有坑？"}
    assert _route_after_guide_agent(state) == "companion_pitfall_query_node"


# ---------------------------------------------------------------------------
# 自动串联合并结果
# ---------------------------------------------------------------------------


def test_realtime_answer_merges_with_prior_guide_answer(isolated_project, monkeypatch):
    """模拟自动串联：guide_agent先给了一段不完整的回答，realtime_agent补充查询后，
    最终companion_answer应该同时包含两段文本。"""
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)

    def fake_invoke(agent, messages):
        agent["mode"] = "answer"
        agent["text"] = "今天成都多云，气温18-24度。"
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "companion_question": "今天天气怎么样？",
        "companion_guide_answer": "攻略里没有记录天气信息。",
    }
    result = realtime_agent_node(state)

    assert "攻略里没有记录天气信息。" in result["companion_answer"]
    assert "今天成都多云，气温18-24度。" in result["companion_answer"]


def test_realtime_agent_node_create_reminder_path_returns_confirmation_as_answer(isolated_project, monkeypatch):
    """create_reminder工具走的是跟answer_query一样的"直接产出answer"收尾路径（不像
    propose_patch那样需要审批），只是captured["mode"]是"reminder"而不是"answer"——
    验证realtime_agent_node不会因为mode不是"answer"就误判成走patch分支。"""
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)

    def fake_invoke(agent, messages):
        agent["mode"] = "reminder"
        agent["text"] = "提醒已创建，将在 2026-07-25T16:00:00+00:00 通过飞书推送。"
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = realtime_agent_node(
        {"project_id": isolated_project, "companion_question": "一小时后提醒我出发去高铁站"}
    )

    assert "realtime_pending_patch" not in result
    assert "提醒已创建" in result["companion_answer"]


def test_realtime_agent_node_handles_query_then_cancel_then_answer_sequence(isolated_project, monkeypatch):
    """list_reminders/cancel_reminder_tool这类"不结束本轮"的工具在react-agent内部
    可以被调用任意次，只要最终落到answer_query/propose_patch/create_reminder三者之一，
    realtime_agent_node就应该能正确产出companion_answer——这里用fake_invoke模拟
    "模型先查询再取消再回答"这个多步骤序列的*最终结果*（react-agent内部的多轮工具调用
    循环本身已经被monkeypatch掉，这里只验证节点对"最终mode是answer"这一结果的处理，
    不是验证真实的react-agent工具调用循环）。"""
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)

    def fake_invoke(agent, messages):
        # 模拟模型内部经过list_reminders查询+cancel_reminder_tool取消后，最终调用
        # answer_query收尾——这两个中间工具不会写captured["mode"]，只有最后一步会。
        agent["mode"] = "answer"
        agent["text"] = "已帮你取消高铁出发提醒。"
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    result = realtime_agent_node(
        {"project_id": isolated_project, "companion_question": "把高铁那个提醒取消掉"}
    )

    assert "realtime_pending_patch" not in result
    assert "已帮你取消高铁出发提醒。" in result["companion_answer"]


def test_companion_lifestyle_query_merges_with_prior_guide_answer(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages: {"messages": [_FakeMessage("附近推荐一家老字号钟水饺。")]},
    )

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "附近还有什么好吃的？",
        "companion_guide_answer": "树木详情里没有覆盖到这个问题。",
    }
    result = companion_lifestyle_query_node(state)

    assert "树木详情里没有覆盖到这个问题。" in result["companion_answer"]
    assert "附近推荐一家老字号钟水饺。" in result["companion_answer"]
    assert result["last_agent_result"]["agent"] == "companion_query"


# ---------------------------------------------------------------------------
# companion_pitfall_query_node（避雷agent陪伴阶段轻量查询）：跟companion_lifestyle_query_node
# 同款结构，区别是①工具不绑amap②读取已有pitfall.json塞进上下文。
# ---------------------------------------------------------------------------


def test_companion_pitfall_query_tools_have_no_write_and_no_amap():
    """companion_pitfall_query_node实际用的工具集（跟真实pitfall_agent对称，见
    src/agents/pitfall_agent/agent.py）：只有素材检索+只读workspace，没有写权限，
    也没有amap_query（避雷是内容检索不是地理位置校验）。"""
    tools = make_material_tools("成都", "some-project") + make_workspace_tools("some-project", "companion_query")
    names = {t.name for t in tools}
    assert "read_workspace" in names
    assert not any(n.startswith("write_") for n in names)
    assert "amap_query" not in names


def test_companion_pitfall_query_merges_with_prior_guide_answer_and_reads_existing_pitfall(
    isolated_project, monkeypatch
):
    """验证两件事：①自动串联时正确拼接guide_agent的既有回答；②prompt里带上了这个project
    已有的outputs/pitfall.json内容，避免临时查询的结论跟正式避雷产出互相矛盾。"""
    _seed_pitfall(isolated_project, warning_text="鹤鸣茶社人流量极大")
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")

    captured_messages = []

    def fake_invoke(agent, messages):
        captured_messages.append(messages[0]["content"])
        return {"messages": [_FakeMessage("这家店确实有人反馈味道偏咸，属实避雷。")]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "这家店有没有坑？",
        "companion_guide_answer": "树木详情里没有覆盖到这个问题。",
    }
    result = companion_pitfall_query_node(state)

    assert "树木详情里没有覆盖到这个问题。" in result["companion_answer"]
    assert "这家店确实有人反馈味道偏咸，属实避雷。" in result["companion_answer"]
    assert result["last_agent_result"]["agent"] == "companion_pitfall_query"
    # 已有避雷内容确实被塞进了prompt上下文
    assert "鹤鸣茶社人流量极大" in captured_messages[0]


def test_companion_pitfall_query_works_when_no_pitfall_content_yet(isolated_project, monkeypatch):
    """project还没生成过避雷内容时（outputs/pitfall.json不存在），不应该报错，
    读到None直接一起序列化进prompt即可。"""
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages: {"messages": [_FakeMessage("暂时没查到相关避雷内容。")]},
    )

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "这家店有没有坑？",
    }
    result = companion_pitfall_query_node(state)

    assert result["companion_answer"] == "暂时没查到相关避雷内容。"
    assert result["last_agent_result"]["agent"] == "companion_pitfall_query"


# ---------------------------------------------------------------------------
# companion_culture_query_node（文化科普agent陪伴阶段轻量查询）：跟
# companion_lifestyle_query_node/companion_pitfall_query_node同款结构，区别是
# ①工具只绑web_search+只读workspace（不绑material/amap）②不读outputs/culture.json
# 塞上下文（文化科普是纯知识问答，没有"正式产出区已有结论"要对齐的必要）。
# ---------------------------------------------------------------------------


def test_companion_culture_query_tools_have_no_material_amap_or_write():
    """companion_culture_query_node实际用的工具集（跟真实culture_agent对称，见
    src/agents/culture_agent/agent.py）：只有web_search+只读workspace，没有写权限，
    没有retrieve_materials（素材库是UGC攻略内容不是历史文化科普），也没有amap_query
    （文化科普不是地理位置校验）。"""
    from src.tools.web_search_tools import make_web_search_tools

    tools = make_web_search_tools("some-project", "companion_culture") + make_workspace_tools(
        "some-project", "companion_query"
    )
    names = {t.name for t in tools}
    assert "read_workspace" in names
    assert "web_search" in names
    assert not any(n.startswith("write_") for n in names)
    assert "retrieve_materials" not in names
    assert "amap_query" not in names


def test_companion_culture_query_merges_with_prior_guide_answer(isolated_project, monkeypatch):
    """自动串联时正确拼接guide_agent的既有回答（同companion_lifestyle_query_node），
    且last_agent_result.agent是"companion_culture_query"。"""
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages: {"messages": [_FakeMessage("武侯祠是纪念诸葛亮的祠堂，建于公元233年。")]},
    )

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "武侯祠名字的由来？",
        "companion_guide_answer": "攻略里没有覆盖到这个问题的文化背景。",
    }
    result = companion_culture_query_node(state)

    assert "攻略里没有覆盖到这个问题的文化背景。" in result["companion_answer"]
    assert "武侯祠是纪念诸葛亮的祠堂，建于公元233年。" in result["companion_answer"]
    assert result["last_agent_result"]["agent"] == "companion_culture_query"


def test_companion_culture_query_does_not_read_existing_culture_json(isolated_project, monkeypatch):
    """跟pitfall节点读outputs/pitfall.json避免结论矛盾不同，文化科普节点不读
    outputs/culture.json塞上下文——文化科普是纯知识问答，没有"正式产出区已有结论"
    要对齐的必要。验证prompt上下文里不包含已有culture.json内容。"""
    from src.workspace.schema import CultureOutput, CultureItem

    store.write_culture_output(
        isolated_project,
        "culture_agent",
        CultureOutput(items=[CultureItem(question="旧问题", answer="旧答案", references=[])]),
    )
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")

    captured_messages = []

    def fake_invoke(agent, messages):
        captured_messages.append(messages[0]["content"])
        return {"messages": [_FakeMessage("武侯祠因诸葛亮谥号忠武得名。")]}

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {
        "project_id": isolated_project,
        "topic": "成都",
        "companion_question": "武侯祠名字的由来？",
    }
    result = companion_culture_query_node(state)

    # 旧问答历史不应被塞进陪伴节点的prompt上下文
    assert "旧答案" not in captured_messages[0]
    assert result["last_agent_result"]["agent"] == "companion_culture_query"


def test_route_after_guide_agent_not_covered_routes_to_culture_query(monkeypatch):
    """guide_agent覆盖度不够、自动串联分类落到culture_agent时，路由到
    companion_culture_query_node（五选一的culture分支）。"""
    monkeypatch.setattr(orchestrator, "_classify_companion_intent", lambda msg, candidates: "culture_agent")
    state = {"guide_covered": False, "companion_question": "武侯祠名字的由来？"}
    assert _route_after_guide_agent(state) == "companion_culture_query_node"


def test_route_edit_mode_companion_culture_query_ends_without_cascading(isolated_project):
    """companion_culture_query_node是陪伴阶段轻量只读节点，跑完直接END，
    不落进AGENT_DEPENDENCY_TABLE驱动的级联/整合逻辑——跟companion_query/
    companion_pitfall_query同款分支。"""
    state = {"project_id": isolated_project, "last_agent_result": {"agent": "companion_culture_query"}}
    assert _route_edit_mode(state) == orchestrator.END


# ---------------------------------------------------------------------------
# 写入审批边界（高风险重点）：realtime_agent判断需要写入时，无论是直接@触发还是
# 自动串联触发，都必须停在await_realtime_approval_node，不能跳过审批直接写。
# ---------------------------------------------------------------------------


def _patch_realtime_agent_to_propose_patch(monkeypatch, field="food", day=1, new_text="换成了XX甜品店"):
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)

    def fake_invoke(agent, messages):
        agent["mode"] = "patch"
        agent["patch"] = {
            "field": field,
            "day": day,
            "new_recommendation": new_text,
            "reason": "原推荐已确认长期停业",
        }
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)


def test_realtime_agent_node_produces_pending_patch_not_direct_write(isolated_project, monkeypatch):
    _seed_lifestyle(isolated_project)
    _patch_realtime_agent_to_propose_patch(monkeypatch)

    result = realtime_agent_node({"project_id": isolated_project, "companion_question": "这家店关门了"})

    assert result["realtime_pending_patch"]["field"] == "food"
    assert "companion_answer" not in result  # 没有直接产出答案，等审批节点收尾
    # 关键：这一步绝不能已经把内容写进outputs/lifestyle.json
    assert store.read_lifestyle_output(isolated_project).food.items[0].recommendation == "原始美食推荐"


def test_route_after_realtime_agent_with_pending_patch_goes_to_approval():
    state = {"realtime_pending_patch": {"field": "food"}, "realtime_approval_done": False}
    assert orchestrator._route_after_realtime_agent(state) == "await_realtime_approval_node"


def test_direct_realtime_edit_stops_at_interrupt_without_writing(isolated_project, monkeypatch):
    """直接@realtime_agent发起修改：完整graph.invoke()必须在await_realtime_approval_node
    暂停（结果含"__interrupt__"），且此时store里的内容必须完全没有变化——真正的写入
    还没有发生，不能被"agent已经产出patch"这件事误认为已经写完了。"""
    _seed_lifestyle(isolated_project)
    _patch_realtime_agent_to_propose_patch(monkeypatch)

    result = run_companion(isolated_project, "这家店关门了，帮我更新一下", entry_target="realtime_agent_node")

    assert "__interrupt__" in result
    assert store.read_lifestyle_output(isolated_project).food.items[0].recommendation == "原始美食推荐"
    assert companion_history.read_recent_turns(isolated_project) == []


def test_auto_chain_realtime_write_also_stops_at_interrupt_without_writing(isolated_project, monkeypatch):
    """自动串联场景：guide_agent先跑（entry_target显式指定guide_agent_node，模拟run_companion
    的初次三选一分类已经落在guide_agent——这一步的分类逻辑本身在
    test_classify_companion_intent_three_branches里已经独立验证过，这里只关心guide跑完
    covered=False之后的后续行为），判断没覆盖到、_route_after_guide_agent的追加分类
    （mock）落到realtime_agent，realtime_agent判断需要写入——这条路径必须和直接
    @realtime_agent一样停在审批节点，不能因为是"自动追加触发的"就绕过审批直接写。
    这是本次实现里唯一的高风险点。"""
    _seed_lifestyle(isolated_project)
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)
    monkeypatch.setattr(orchestrator, "_classify_companion_intent", lambda msg, candidates: "realtime_agent")

    # guide_agent_node先跑（第1次invoke_with_retry调用），之后路由到realtime_agent_node
    # 再跑一次（第2次调用）——用调用次数区分两段不同agent的行为，因为两个节点各自的
    # captured字典是不同对象，fake函数只能靠调用顺序判断当前在给哪个节点提供数据。
    calls = {"n": 0}

    def fake_invoke_sequenced(agent, messages):
        calls["n"] += 1
        if calls["n"] == 1:
            agent["text"] = "攻略里没有提到这家店的最新状态。"
            agent["covered"] = False
        else:
            agent["mode"] = "patch"
            agent["patch"] = {
                "field": "food",
                "day": 1,
                "new_recommendation": "换成了XX甜品店",
                "reason": "原推荐已确认长期停业",
            }
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke_sequenced)

    result = run_companion(
        isolated_project, "这家店现在还开着吗，要是关了帮我换一个", entry_target="guide_agent_node"
    )

    assert calls["n"] == 2  # 确认guide_agent和realtime_agent都真的各跑了一次，是真实的两段串联
    assert "__interrupt__" in result
    assert store.read_lifestyle_output(isolated_project).food.items[0].recommendation == "原始美食推荐"


def test_resume_companion_approve_actually_writes(isolated_project, monkeypatch):
    _seed_lifestyle(isolated_project)
    _patch_realtime_agent_to_propose_patch(monkeypatch, new_text="换成了XX甜品店")
    # 批准后会真正落盘，路由会按_route_after_realtime_agent级联到整合阶段重新合并
    # （文档行为：只有真写入了才级联整合，见docs/ORCHESTRATION.md第3.2节）。整合环节本身
    # 的正确性已经有专门的测试覆盖，这里stub掉它，只关心"patch真的被写进了outputs/lifestyle.json"
    # 这一个高风险点，避免被无关的integration_agent真实LLM调用干扰。
    monkeypatch.setattr(
        orchestrator,
        "integration_agent_node",
        lambda state: {"integration_done": True, "last_agent_result": {"agent": "integration_agent", "summary": "stub"}},
    )

    interrupted = run_companion(isolated_project, "这家店关门了", entry_target="realtime_agent_node")
    assert "__interrupt__" in interrupted

    result = resume_companion(isolated_project, "approve")

    assert "__interrupt__" not in result
    updated = store.read_lifestyle_output(isolated_project)
    assert updated.food.items[0].recommendation == "换成了XX甜品店"


def test_resume_companion_reject_does_not_write(isolated_project, monkeypatch):
    _seed_lifestyle(isolated_project)
    _patch_realtime_agent_to_propose_patch(monkeypatch, new_text="换成了XX甜品店")

    interrupted = run_companion(isolated_project, "这家店关门了", entry_target="realtime_agent_node")
    assert "__interrupt__" in interrupted

    result = resume_companion(isolated_project, "reject")

    assert "__interrupt__" not in result
    unchanged = store.read_lifestyle_output(isolated_project)
    assert unchanged.food.items[0].recommendation == "原始美食推荐"


# ---------------------------------------------------------------------------
# pitfall_agent（避雷agent）：路由函数本身的entry_target透传机制对任意字符串都成立
# （这条泛化断言不因为pitfall_agent后来有了真实实现而失效）。真正的
# companion_pitfall_query_node路由+显式@pitfall_agent_node回归验证见下方两个小节。
# ---------------------------------------------------------------------------


def test_route_edit_mode_passes_through_arbitrary_entry_target_for_future_pitfall_agent():
    state = {"project_id": "any-project", "entry_target": "pitfall_agent", "last_agent_result": None}
    assert _route_edit_mode(state) == "pitfall_agent"


def test_run_companion_auto_classifies_to_pitfall_query_node_when_no_entry_target(
    isolated_project, monkeypatch
):
    """完整的run_companion()端到端路由：没有显式@任何角色，问一句明显避雷类的话，
    意图分类（mock LLM）返回pitfall_agent，应该真正执行到companion_pitfall_query_node
    （而不是guide/realtime/lifestyle），last_agent_result落在companion_pitfall_query。"""
    monkeypatch.setattr(
        orchestrator,
        "build_companion_router_model",
        lambda temperature=0: _FakeModel("pitfall_agent"),
    )
    monkeypatch.setattr(orchestrator, "create_react_agent", lambda model, tools, prompt: "dummy-agent")
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages: {"messages": [_FakeMessage("确实有人反馈这家店排队久、味道一般。")]},
    )

    result = run_companion(isolated_project, "这家店有没有坑？")

    assert result["companion_answer"] == "确实有人反馈这家店排队久、味道一般。"
    assert result["last_agent_result"] == {
        "agent": "companion_pitfall_query",
        "summary": "避雷agent补充检索完成",
    }


def test_explicit_pitfall_agent_node_entry_target_unaffected_by_companion_routing(
    isolated_project, monkeypatch
):
    """回归验证：显式@避雷agent（entry_target="pitfall_agent_node"）走的是已经实现验证过的
    edit模式全量写入路径（真实pitfall_agent_node），不能被这次新增的
    companion_pitfall_query_node自动分类路由分支误伤——两条路径的入口判断完全独立
    （entry_target非空时_route_edit_mode第一跳直接透传，压根不会触发意图分类）。"""
    _seed_lifestyle(isolated_project)
    monkeypatch.setattr(
        orchestrator,
        "build_pitfall_agent",
        lambda project_id, topic, **kwargs: object(),
    )

    def fake_invoke(agent, messages):
        _seed_pitfall(isolated_project, warning_text="杜甫草堂是后世纪念馆非真实居住原址")
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(
        orchestrator,
        "pitfall_citation_check_node",
        lambda state: {
            "pitfall_citation_checked": True,
            "last_agent_result": {"agent": "citation_checker", "summary": "stub"},
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "integration_agent_node",
        lambda state: {
            "integration_done": True,
            "last_agent_result": {"agent": "integration_agent", "summary": "stub"},
        },
    )

    result = run_companion(isolated_project, "再帮我查查有什么坑", entry_target="pitfall_agent_node")

    assert "__interrupt__" not in result
    assert result["last_agent_result"]["agent"] == "integration_agent"
    written = store.read_pitfall_output(isolated_project)
    assert written.food.items[0].warning == "杜甫草堂是后世纪念馆非真实居住原址"


# ---------------------------------------------------------------------------
# 多次run_companion()调用之间的状态隔离（真实E2E发现的问题，见docs/ISSUES.md）：
# ":companion"这个thread_id是按project固定复用的（同一个project的所有陪伴阶段问答共用
# 一个thread），LangGraph对没有显式reducer的普通dict字段的默认语义是"没在这次输入里
# 出现的key，保留上一次checkpoint的值"——如果run_companion()的initial_state不显式
# 重置last_agent_result等路由用的记账字段，第二次调用会把第一次调用遗留的
# last_agent_result误认为"已经不是第一跳了"，导致完全忽略这次的entry_target/
# companion_question，直接复用上一轮的路由结果。
# ---------------------------------------------------------------------------


def test_run_companion_second_call_is_not_ignored_due_to_stale_state(isolated_project, monkeypatch):
    """真实E2E复现过的bug：连续两次run_companion()调用（同一个project，不同问题/
    不同entry_target），第二次调用必须真正执行到它自己指定的目标节点，不能被第一次
    调用遗留在thread checkpoint里的last_agent_result等字段误判成"已经跑过第一跳"
    而直接短路返回。"""
    _seed_lifestyle(isolated_project)
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)
    monkeypatch.setattr(orchestrator, "build_realtime_agent", lambda project_id, captured: captured)

    def fake_guide_invoke(agent, messages):
        agent["text"] = "Day1安排是武侯祠+锦里。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_guide_invoke)

    # 第一次调用：@guide_agent，覆盖度够，直接结束
    result1 = run_companion(isolated_project, "Day1去哪玩？", entry_target="guide_agent_node")
    assert result1.get("companion_answer") == "Day1安排是武侯祠+锦里。"
    assert result1.get("last_agent_result") == {"agent": "guide_agent", "summary": "向导已回答"}

    # 第二次调用：换成@realtime_agent发起修改——如果bug没修复，这次调用会因为
    # thread里残留的last_agent_result（guide_agent）而被误判成"非第一跳"，
    # 完全不会真正调用realtime_agent，直接复用第一次的companion_answer返回。
    _patch_realtime_agent_to_propose_patch(monkeypatch)
    result2 = run_companion(isolated_project, "这家店关门了，帮我更新一下", entry_target="realtime_agent_node")

    assert "__interrupt__" in result2, (
        "第二次调用应该真正执行到realtime_agent并产出待审批patch，"
        "结果里应该出现__interrupt__——如果没有，说明第二次调用被静默短路了"
    )


def test_run_companion_second_call_gets_fresh_companion_question(isolated_project, monkeypatch):
    """第二轮保留最近对话用于指代，但当前问题仍必须作为独立字段清楚出现。"""
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)
    captured_questions = []

    def fake_invoke(agent, messages):
        # prompt里会拼入companion_question，从这里能看出这次guide_agent_node
        # 实际处理的是哪个问题
        captured_questions.append(messages[0]["content"])
        agent["text"] = f"回答：{messages[0]['content'][:10]}"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    run_companion(isolated_project, "第一个问题：Day1去哪玩？", entry_target="guide_agent_node")
    run_companion(isolated_project, "第二个完全不同的问题：必备清单里有什么？", entry_target="guide_agent_node")

    assert len(captured_questions) == 2
    assert "第一个问题" in captured_questions[0]
    assert "第二个完全不同的问题" in captured_questions[1]
    assert "【最近对话上下文】" in captured_questions[1]
    assert "第一个问题" in captured_questions[1]
    assert "用户问题：第二个完全不同的问题" in captured_questions[1]


def test_run_companion_second_turn_injects_history_into_router_and_agent(isolated_project, monkeypatch):
    router_prompts = []
    agent_prompts = []

    class CapturingRouter:
        def invoke(self, messages):
            router_prompts.append(messages[0]["content"])
            return _FakeResponse("guide_agent")

    monkeypatch.setattr(
        orchestrator,
        "build_companion_router_model",
        lambda temperature=0: CapturingRouter(),
    )
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)

    def fake_invoke(agent, messages):
        agent_prompts.append(messages[0]["content"])
        agent["text"] = "这是本轮回答。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    run_companion(isolated_project, "武侯祠几点关门？")
    run_companion(isolated_project, "那门票呢？")

    assert "最近对话上下文" in router_prompts[1]
    assert "武侯祠几点关门" in router_prompts[1]
    assert "【当前用户问题】\n那门票呢？" in router_prompts[1]
    assert "最近对话上下文" in agent_prompts[1]
    assert "武侯祠几点关门" in agent_prompts[1]
    assert "用户问题：那门票呢？" in agent_prompts[1]
    turns = companion_history.read_recent_turns(isolated_project)
    assert [turn["user_message"] for turn in turns] == ["武侯祠几点关门？", "那门票呢？"]


def test_run_companion_call_count_resets_across_turns(isolated_project, monkeypatch):
    """docs/ISSUES.md第142条：连续两次run_companion()调用（都路由到guide_agent_node）
    是两个独立的turn，不是同一轮的第2跳——run_status.json的call_count应该都显示1，
    不能像修复前那样跨turn累积成1、2。"""
    monkeypatch.setattr(orchestrator, "build_guide_agent", lambda project_id, captured: captured)

    def fake_invoke(agent, messages):
        agent["text"] = "Day1安排是武侯祠+锦里。"
        agent["covered"] = True
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    run_companion(isolated_project, "Day1去哪玩？", entry_target="guide_agent_node")
    assert store.read_run_status(isolated_project)["call_count"] == 1

    run_companion(isolated_project, "Day2去哪玩？", entry_target="guide_agent_node")
    assert store.read_run_status(isolated_project)["call_count"] == 1
