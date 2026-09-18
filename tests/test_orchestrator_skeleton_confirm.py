"""await_skeleton_confirm_node的graph级别测试——补齐docs/ARCHITECTURE.md第12.1节P0任务、
docs/CLAUDE.md已知问题第6条提到的缺口。

这个节点"只做只读操作 + interrupt()"（见函数docstring），interrupt()只有在真正的LangGraph
执行上下文（有checkpointer的编译图+graph.invoke()）里调用才有意义——脱离图直接调用
await_skeleton_confirm_node(state)会因为找不到执行上下文而报错（跟test_material_collection_graph.py
里await_material_confirm_node在auto_confirm=False时验证"确实尝试触发中断"是同一个道理）。
所以这里不直接invoke这个函数，而是搭一个只含它自己的最小图，驱动真实的
interrupt()/Command(resume=...)机制，覆盖三种resume决策：confirm、revise:<意见>、以及
"图在正确的位置暂停、且没有确认前不能有副作用"这条最基本的正确性保证。

不引入完整生产图（build_graph()）以避免要mock一整条下游pipeline——只测这一个节点自身的
interrupt/resume契约，跟其余节点的路由联动已经由main_agent_router的路由测试
（tests/test_orchestrator_routing.py等）覆盖。
"""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from src.orchestrator import GraphState, await_skeleton_confirm_node
from src.workspace import store
from src.workspace.schema import ProjectPhase, RouteDay, RoutePlan, RoutePoi


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "skeleton-confirm-test"
    store.init_project(project_id)
    store.write_route_plan(
        project_id,
        actor="route_agent",
        route_plan=RoutePlan(days=[RouteDay(day=1, city="成都", area="武侯区", pois=[RoutePoi(name="武侯祠")])]),
    )
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.SKELETON_CONFIRM)
    return project_id


def _build_minimal_confirm_graph():
    """只含await_skeleton_confirm_node自身的最小图：驱动真实interrupt()/resume机制，
    不需要生产图里的其余节点参与。"""
    graph = StateGraph(GraphState)
    graph.add_node("await_skeleton_confirm", await_skeleton_confirm_node)
    graph.add_edge(START, "await_skeleton_confirm")
    graph.add_edge("await_skeleton_confirm", END)
    return graph.compile(checkpointer=InMemorySaver())


def test_graph_pauses_at_skeleton_confirm_with_route_plan_in_payload(isolated_project):
    """图必须真的在这个节点暂停（结果含"__interrupt__"），且中断payload里要带上当前
    route_plan——用户确认骨架时需要看到具体内容，不能是一个空壳提示。"""
    graph = _build_minimal_confirm_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:skeleton-test"}}

    result = graph.invoke({"project_id": isolated_project}, config)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["question"]
    assert payload["route_plan"]["days"][0]["city"] == "成都"
    # 确认前不应该有任何副作用——progress仍应停留在骨架确认中，不能提前推进状态机
    assert store.read_progress(isolated_project).state == ProjectPhase.SKELETON_CONFIRM
    assert store.read_progress(isolated_project).skeleton_confirmed is False


def test_resume_confirm_advances_progress_and_marks_route_agent_done(isolated_project):
    """resume("confirm")：应该真正推进状态机到并行调研中，并标记skeleton_confirmed=True——
    这是骨架确认完成后下游route_citation_check_node/lifestyle_agent_node能被路由到的前提。"""
    graph = _build_minimal_confirm_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:skeleton-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    result = graph.invoke(Command(resume="confirm"), config)

    assert "__interrupt__" not in result
    assert result["route_agent_done"] is True
    assert result["last_agent_result"]["agent"] == "route_agent"

    progress = store.read_progress(isolated_project)
    assert progress.state == ProjectPhase.PARALLEL_RESEARCH
    assert progress.skeleton_confirmed is True


def test_resume_revise_does_not_advance_progress_and_returns_revision_request(isolated_project):
    """resume("revise:<意见>")：用户要求调整骨架时，不应该推进状态机（还没确认），
    而是把revision_request原样透传出来，供main_agent_router判断该回route_draft_node
    重新生成（见_route_plan_mode里`if state.get("revision_request")`那一段）。"""
    graph = _build_minimal_confirm_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:skeleton-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    result = graph.invoke(Command(resume="revise:武侯祠改成杜甫草堂"), config)

    assert "__interrupt__" not in result
    assert result["revision_request"] == "武侯祠改成杜甫草堂"
    assert "route_agent_done" not in result

    progress = store.read_progress(isolated_project)
    assert progress.state == ProjectPhase.SKELETON_CONFIRM
    assert progress.skeleton_confirmed is False


def test_resume_ambiguous_reply_reprompts_instead_of_confirming(isolated_project):
    """docs/ARCHITECTURE.md第19.1节：回复既不是精确的"confirm"，也不是"revise:"开头
    （如包含"revise"字样但不是指令格式的自然语言），修复前会被静默当成确认通过——
    这是bug。修复后应该重新interrupt()追问，不能静默推进状态机。"""
    graph = _build_minimal_confirm_graph()
    config = {"configurable": {"thread_id": f"{isolated_project}:skeleton-test"}}

    graph.invoke({"project_id": isolated_project}, config)
    result = graph.invoke(Command(resume="我觉得可以revise一下但算了，confirm"), config)

    # 未能识别 -> 重新暂停追问，不推进状态机，也不应该误判成revise分支
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert "没有识别到您的意图" in payload["question"]
    progress = store.read_progress(isolated_project)
    assert progress.state == ProjectPhase.SKELETON_CONFIRM
    assert progress.skeleton_confirmed is False

    # 追问事件写入changelog（不是需要复核的冲突，不走conflicts.jsonl）
    changelog_path = store.workspace_dir(isolated_project) / "changelog.jsonl"
    changelog_lines = changelog_path.read_text(encoding="utf-8").splitlines()
    assert any('"action": "skeleton_confirm_reprompt"' in line for line in changelog_lines)
    conflicts_path = store.workspace_dir(isolated_project) / "conflicts.jsonl"
    assert not conflicts_path.exists()

    # 紧接着真正回复confirm，应该正常推进
    result = graph.invoke(Command(resume="confirm"), config)
    assert "__interrupt__" not in result
    assert result["route_agent_done"] is True
    assert store.read_progress(isolated_project).state == ProjectPhase.PARALLEL_RESEARCH
