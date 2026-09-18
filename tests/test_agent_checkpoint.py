"""src/agent_checkpoint.py 的单元测试：per-project持久化checkpointer的路径解析、
thread_id命名、clear_agent_thread清空行为。见docs/ISSUES.md第120条。

用真实的langgraph.checkpoint.sqlite.SqliteSaver（不mock），配合真实的最小StateGraph
验证"持久化"这个核心claim——同一个project_id、不同时刻新建的checkpointer实例（模拟
进程重启/不同Python对象）确实能看到同一份磁盘上的历史，这是整套断点续跑机制成立的前提。
"""
from __future__ import annotations

import pytest
from langgraph.graph import END, START, StateGraph
from typing import TypedDict

from src import agent_checkpoint as ac
from src.workspace import store


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "checkpoint-test"
    store.init_project(project_id)
    return project_id


def test_checkpoint_path_respects_projects_dir_override(isolated_project, tmp_path):
    path = ac.checkpoint_path(isolated_project)
    assert path == tmp_path / isolated_project / "workspace" / "checkpoint.sqlite"


def test_get_graph_checkpointer_creates_file_and_is_usable(isolated_project):
    saver = ac.get_graph_checkpointer(isolated_project)
    try:
        assert ac.checkpoint_path(isolated_project).exists()
        assert saver.get_tuple({"configurable": {"thread_id": "nonexistent"}}) is None
    finally:
        saver.conn.close()


def test_graph_and_agent_checkpointer_point_to_same_file(isolated_project):
    graph_saver = ac.get_graph_checkpointer(isolated_project)
    agent_saver = ac.get_agent_checkpointer(isolated_project)
    try:
        assert ac.checkpoint_path(isolated_project).exists()
        # 两个独立连接，但物理上是同一个文件——用sqlite自带的database_list核实。
        import sqlite3

        def _db_path(conn: sqlite3.Connection) -> str:
            rows = conn.execute("PRAGMA database_list").fetchall()
            return rows[0][2]

        assert _db_path(graph_saver.conn) == _db_path(agent_saver.conn)
    finally:
        graph_saver.conn.close()
        agent_saver.conn.close()


def test_agent_thread_id_naming():
    assert ac.agent_thread_id("proj1", "route_draft") == "proj1:agent:route_draft"


def test_agent_thread_id_with_suffix_for_parallel_fanout():
    """POI详情这类Send并行fan-out场景需要按天+POI再加一层区分，避免并行分支撞thread_id。"""
    tid = ac.agent_thread_id("proj1", "poi_detail_draft", suffix="day2_wuhouci")
    assert tid == "proj1:agent:poi_detail_draft:day2_wuhouci"


def test_poi_detail_agent_thread_id_isolated_by_turn_day_and_poi():
    tid = ac.poi_detail_agent_thread_id("proj1", 2, "武侯祠", "abc123")
    assert tid == "proj1:agent:poi_detail_draft:abc123:day2_武侯祠"


def test_clear_agent_thread_on_empty_thread_is_noop(isolated_project):
    """幂等：清空一个从没写过历史的thread不应该报错。"""
    ac.clear_agent_thread(isolated_project, "route_draft")


class _TinyState(TypedDict):
    counter: int
    log: list


def _build_tiny_graph(project_id: str):
    """跟真实build_graph()同款用法：checkpointer=get_graph_checkpointer(project_id)，
    模拟真实的两节点顺序图，用来验证checkpoint确实落盘、能跨对象续跑。"""

    def node_a(state):
        return {"counter": state["counter"] + 1, "log": state["log"] + ["a"]}

    def node_b(state):
        return {"counter": state["counter"] + 1, "log": state["log"] + ["b"]}

    graph = StateGraph(_TinyState)
    graph.add_node("node_a", node_a)
    graph.add_node("node_b", node_b)
    graph.add_edge(START, "node_a")
    graph.add_edge("node_a", "node_b")
    graph.add_edge("node_b", END)
    return graph.compile(checkpointer=ac.get_graph_checkpointer(project_id))


def test_persistent_checkpointer_survives_fresh_graph_object(isolated_project):
    """核心claim：checkpoint历史不绑在具体的Python图对象上——用同一个project_id、
    不同时刻新建的图对象（模拟进程重启后重新build_graph(project_id)）也能看到
    同一份磁盘上的历史。这是_companion_graph单例被移除、"随时新建图也能续跑"这套
    设计成立的前提，必须有真实验证不能只靠代码审查。"""
    config = {"configurable": {"thread_id": "run1"}}

    graph1 = _build_tiny_graph(isolated_project)
    result1 = graph1.invoke({"counter": 0, "log": []}, config)
    assert result1 == {"counter": 2, "log": ["a", "b"]}

    # 全新的图对象（不同的Python实例，模拟另一次build_graph(project_id)调用），
    # 但指向同一个project_id -> 同一个sqlite文件。
    graph2 = _build_tiny_graph(isolated_project)
    state = graph2.get_state(config)
    assert state.values == result1
    assert state.next == ()  # 图已经完整跑完


def test_clear_agent_thread_removes_history_for_reuse(isolated_project):
    """clear_agent_thread清空后，同一个thread_id的下一次invoke不会带着旧历史——
    验证真实场景：route_draft_node被再次调用（比如后续一次edit触发的新route_draft）
    时不会继承几小时前的对话历史。"""
    node_name = "tiny_thread"
    thread_id = ac.agent_thread_id(isolated_project, node_name)
    config = {"configurable": {"thread_id": thread_id}}

    graph1 = _build_tiny_graph(isolated_project)
    graph1.invoke({"counter": 0, "log": []}, config)
    assert graph1.get_state(config).values == {"counter": 2, "log": ["a", "b"]}

    ac.clear_agent_thread(isolated_project, node_name)

    graph2 = _build_tiny_graph(isolated_project)
    state_after_clear = graph2.get_state(config)
    assert state_after_clear.values == {}  # 历史已清空，不是带着旧counter/log残留

    # 清空后重新invoke，counter应该从0开始（不是接着旧的2继续），证明没有历史残留。
    result2 = graph2.invoke({"counter": 0, "log": []}, config)
    assert result2 == {"counter": 2, "log": ["a", "b"]}


def test_clear_agent_thread_family_removes_only_parent_and_write_children(isolated_project):
    parent = ac.agent_thread_id(isolated_project, "route_structure_proposal", suffix="work-a")
    child = f"{parent}:write-verification:1:abc"
    unrelated = ac.agent_thread_id(
        isolated_project,
        "route_structure_proposal",
        suffix="work-b",
    )
    graph = _build_tiny_graph(isolated_project)
    for thread_id in (parent, child, unrelated):
        graph.invoke(
            {"counter": 0, "log": []},
            {"configurable": {"thread_id": thread_id}},
        )

    ac.clear_agent_thread_family(isolated_project, parent)

    assert graph.get_state({"configurable": {"thread_id": parent}}).values == {}
    assert graph.get_state({"configurable": {"thread_id": child}}).values == {}
    assert graph.get_state({"configurable": {"thread_id": unrelated}}).values == {
        "counter": 2,
        "log": ["a", "b"],
    }
