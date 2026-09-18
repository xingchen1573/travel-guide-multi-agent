"""文化科普agent（culture_agent）的mock单元测试——仿照tests/test_orchestrator_pitfall.py结构。

不调用真实LLM：monkeypatch build_culture_agent/invoke_with_retry，用可控的副作用函数
模拟"模型这次做了什么"。文化科普agent跟避雷/衣食住行agent的关键差异：①不绑retrieve_materials/
amap_query，绑web_search；②不做引用级核验，一跳结束；③产出是累加式历史记录，不是按固定
字段整份重新生成。
"""
import pytest

from src import orchestrator
from src.orchestrator import _route_edit_mode, culture_agent_node
from src.tools.web_search_tools import make_web_search_tools
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store
from src.workspace.schema import CultureItem, CultureOutput, RoutePlan

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "culture-test"
    store.init_project(project_id)
    store.write_route_plan(project_id, actor="route_agent", route_plan=RoutePlan(days=[]))
    return project_id


def _write_culture(project_id: str, items: list | None = None) -> None:
    output = CultureOutput(items=items or [])
    store.write_culture_output(project_id, actor="culture_agent", culture_output=output)


# ---------------------------------------------------------------------------
# 工具边界：只有read_workspace/write_agent_output(自己产出区)/web_search，
# 没有retrieve_materials/amap_query/其他write_工具
# ---------------------------------------------------------------------------


def test_culture_agent_tools_have_no_material_or_amap_tools():
    tools = make_workspace_tools("some-project", "culture_agent") + make_web_search_tools("some-project", "culture_agent")
    names = {t.name for t in tools}
    assert "read_workspace" in names
    assert "write_agent_output" in names
    assert "web_search" in names
    assert "retrieve_materials" not in names
    assert "amap_query" not in names
    # 除了自己产出区的write_agent_output外，不应该有别的write_类工具
    write_tools = {n for n in names if n.startswith("write_")}
    assert write_tools == {"write_agent_output"}


# ---------------------------------------------------------------------------
# culture_agent_node：写入校验/重试逻辑
# ---------------------------------------------------------------------------


def test_culture_agent_node_writes_and_marks_done(isolated_project, monkeypatch):
    captured_prompts = []

    def fake_invoke(agent, messages):
        captured_prompts.append(messages[0]["content"])
        _write_culture(
            isolated_project,
            items=[CultureItem(question="武侯祠的名字怎么来的？", answer="纪念诸葛亮", references=["https://example.com"])],
        )
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_culture_agent", lambda project_id: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "edit_request": "武侯祠的名字怎么来的？"}
    result = culture_agent_node(state)

    assert "武侯祠的名字怎么来的？" in captured_prompts[0]
    assert result["culture_agent_done"] is True
    assert result["last_agent_result"]["agent"] == "culture_agent"

    saved = store.read_culture_output(isolated_project)
    assert saved.items[0].question == "武侯祠的名字怎么来的？"


def test_culture_agent_node_retries_when_no_real_write(isolated_project, monkeypatch):
    """模型只输出文字总结、没有真正调用write_agent_output时，必须重试一次并携带完整对话历史
    （跟pitfall_agent_node同款_invoke_with_write_verification行为）。"""
    call_count = {"n": 0}

    def fake_invoke(agent, messages):
        call_count["n"] += 1
        if call_count["n"] == 2:
            _write_culture(isolated_project, items=[CultureItem(question="q", answer="a")])
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "build_culture_agent", lambda project_id: object())
    monkeypatch.setattr(orchestrator, "invoke_with_retry", fake_invoke)

    state = {"project_id": isolated_project, "edit_request": "随便问点什么"}
    result = culture_agent_node(state)

    assert call_count["n"] == 2
    assert result["culture_agent_done"] is True


# ---------------------------------------------------------------------------
# _route_edit_mode：第一跳直达 + 一跳结束不级联
# ---------------------------------------------------------------------------


def test_route_edit_mode_entry_target_culture_agent(isolated_project):
    state = {"project_id": isolated_project, "entry_target": "culture_agent_node", "last_agent_result": None}
    assert _route_edit_mode(state) == "culture_agent_node"


def test_route_edit_mode_culture_agent_ends_without_cascading(isolated_project):
    state = {
        "project_id": isolated_project,
        "last_agent_result": {"agent": "culture_agent", "summary": "文化科普回答完成"},
        "integration_done": False,
    }
    assert _route_edit_mode(state) == orchestrator.END


# ---------------------------------------------------------------------------
# store.py权限校验
# ---------------------------------------------------------------------------


def test_write_culture_output_rejects_wrong_actor(isolated_project):
    with pytest.raises(store.PermissionError):
        store.write_culture_output(isolated_project, actor="lifestyle_agent", culture_output=CultureOutput())


def test_write_culture_output_accepts_culture_agent(isolated_project):
    store.write_culture_output(
        isolated_project,
        actor="culture_agent",
        culture_output=CultureOutput(items=[CultureItem(question="q", answer="a")]),
    )
    saved = store.read_culture_output(isolated_project)
    assert saved is not None
    assert saved.items[0].question == "q"
    assert saved.updated_at
