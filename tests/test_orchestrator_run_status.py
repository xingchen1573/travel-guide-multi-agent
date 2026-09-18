"""_track_run_status装饰器的单元测试（机制B：docs/ARCHITECTURE.md第13.2节）。

不调真实LLM/不建真实图，直接对装饰器包一个假node函数测试：正常完成写"done"、
异常写"failed"并原样抛出、GraphBubbleUp（interrupt()真正的实现机制）必须透传
不能被误记成failed、call_count按(project_id, turn_id, node_name)累计——turn_id是
docs/ISSUES.md第142条新增的作用域（"本轮turn内第几跳"，不是跨turn/跨进程累积）。
"""
import pytest
from langgraph.errors import GraphBubbleUp

from src.orchestrator import _node_call_counts, _track_run_status
from src.workspace import store


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "run-status-test"
    store.init_project(project_id)
    _node_call_counts.clear()
    return project_id


def test_track_run_status_records_running_then_done(isolated_project):
    def fake_node(state):
        # node执行期间应该已经能读到"running"状态
        assert store.read_run_status(isolated_project)["status"] == "running"
        return {"ok": True}

    wrapped = _track_run_status("fake_node")(fake_node)
    result = wrapped({"project_id": isolated_project})

    assert result == {"ok": True}
    status = store.read_run_status(isolated_project)
    assert status["status"] == "done"
    assert status["node"] == "fake_node"
    assert status["call_count"] == 1
    assert status["error"] is None
    assert status["finished_at"] is not None


def test_track_run_status_records_failed_and_reraises(isolated_project):
    def raising_node(state):
        raise RuntimeError("模拟node执行失败")

    wrapped = _track_run_status("raising_node")(raising_node)
    with pytest.raises(RuntimeError, match="模拟node执行失败"):
        wrapped({"project_id": isolated_project})

    status = store.read_run_status(isolated_project)
    assert status["status"] == "failed"
    assert "模拟node执行失败" in status["error"]


def test_track_run_status_lets_graph_bubble_up_pass_through(isolated_project):
    """interrupt()真正的实现机制是抛GraphBubbleUp，节点正常暂停时也会抛这个异常，
    不能被这层装饰器误判成"node执行失败"并写成failed状态。"""
    def interrupting_node(state):
        raise GraphBubbleUp()

    wrapped = _track_run_status("interrupting_node")(interrupting_node)
    with pytest.raises(GraphBubbleUp):
        wrapped({"project_id": isolated_project})

    # 状态应该还停在调用前写的"running"，不应该被标记成failed
    status = store.read_run_status(isolated_project)
    assert status["status"] == "running"


def test_track_run_status_increments_call_count_across_calls_without_turn_id(isolated_project):
    """state里没有turn_id字段（理论上不该发生，防御一下）时应该照常工作，不抛错——
    退化成key里turn_id为空字符串，行为等价于所有无turn_id的调用共享一个计数桶。"""
    def fake_node(state):
        return {}

    wrapped = _track_run_status("fake_node")(fake_node)
    wrapped({"project_id": isolated_project})
    wrapped({"project_id": isolated_project})

    assert store.read_run_status(isolated_project)["call_count"] == 2


def test_track_run_status_call_count_scoped_to_turn_id(isolated_project):
    """docs/ISSUES.md第142条：call_count应该是"本轮turn内第几跳"，不是跨turn/跨进程
    累积的。同一turn_id下调用2次显示第2次；换一个新turn_id后同一node的call_count
    重新从1开始，不受之前turn的历史影响。"""
    def fake_node(state):
        return {}

    wrapped = _track_run_status("fake_node")(fake_node)
    wrapped({"project_id": isolated_project, "turn_id": "turn-a"})
    wrapped({"project_id": isolated_project, "turn_id": "turn-a"})
    assert store.read_run_status(isolated_project)["call_count"] == 2

    wrapped({"project_id": isolated_project, "turn_id": "turn-b"})
    assert store.read_run_status(isolated_project)["call_count"] == 1

    wrapped({"project_id": isolated_project, "turn_id": "turn-a"})
    assert store.read_run_status(isolated_project)["call_count"] == 3


def test_track_run_status_noop_without_project_id():
    def fake_node(state):
        return {"ran": True}

    wrapped = _track_run_status("fake_node")(fake_node)
    # 没有project_id时（理论上不该发生，但防御一下）应该直接跑node，不报错
    assert wrapped({}) == {"ran": True}
