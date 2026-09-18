"""run_plan/run_edit/run_channel_start/run_channel_resume——四个真正的生产入口函数
本身的单元测试。填补CLAUDE.md已知问题第6条/docs/ISSUES.md第49条已知局限第3点点名的
缺口：这四个函数此前从未被任何测试直接执行过——test_api_jobs.py/test_channels_feishu_service.py
都是把整个`src.orchestrator`模块（或这几个函数本身）换成假模块/假函数，测的是调用方
（jobs.py/service.py）的行为，不是这几个入口函数自己的职责；test_orchestrator_graph_level.py
虽然用了真实build_graph()，但都是手工构造initial_state直接调用graph.invoke()，同样绕开了
这几个入口函数本身。

这里反过来：只mock掉build_graph()返回的"图"本身（用一个记录调用参数、
可编程返回值的假对象代替真实StateGraph），专门验证这几个函数自己的职责——
store.init_project()时机、thread_id拼接规则、requirements.json在图执行前持久化、run_plan的自动confirm
循环+断点续跑检测（2026-08-13新增，见docs/ISSUES.md第120条）、run_channel_start/resume
命中interrupt时不自动confirm——不重复测图内部路由本身（那是test_orchestrator_graph_level.py
等文件的职责）。
"""
from __future__ import annotations

import pytest

from langgraph.types import Command

from src import orchestrator
from src.orchestrator import run_channel_resume, run_channel_start, run_edit, run_plan
from src.workspace import store


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    return "entrypoint-test"


class _FakeGraph:
    """代替真实build_graph()编译出的StateGraph——记录每次invoke的入参，返回值由测试
    用queue()预先排好队列，按调用顺序依次弹出。

    2026-08-13新增get_state()（见docs/ISSUES.md第120条）：run_plan()现在会先调用
    graph.get_state(config)判断是否要走续跑分支，默认返回"没有历史"（values为空dict、
    next为空tuple），让现有测试的"从头开始"路径保持不变；需要测试续跑分支的用例可以
    用set_state()改写返回值。"""

    def __init__(self):
        self.invoke_calls: list[tuple] = []
        self._responses: list[dict] = []
        self._state = _FakeStateSnapshot(values={}, next=())

    def queue(self, *responses: dict) -> "_FakeGraph":
        self._responses.extend(responses)
        return self

    def invoke(self, arg, config):
        self.invoke_calls.append((arg, config))
        return self._responses.pop(0)

    def get_state(self, config):
        return self._state

    def set_state(self, values: dict, next_: tuple = ()) -> "_FakeGraph":
        self._state = _FakeStateSnapshot(values=values, next=next_)
        return self


class _FakeStateSnapshot:
    def __init__(self, values: dict, next: tuple):
        self.values = values
        self.next = next



# ---------------------------------------------------------------------------
# run_plan
# ---------------------------------------------------------------------------


def test_run_plan_initializes_project_builds_graph_and_persists_requirements(isolated_project, monkeypatch):
    project_id = isolated_project
    fake_graph = _FakeGraph().queue({"last_agent_result": {"agent": "route_agent"}})
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    requirements = {"destination": "成都", "days": 3, "style": "休闲"}
    result = run_plan(project_id, requirements)

    # store.init_project()确实先跑过——progress.json已经存在
    assert store.read_progress(project_id) is not None

    assert len(fake_graph.invoke_calls) == 1
    initial_state, config = fake_graph.invoke_calls[0]
    assert initial_state["project_id"] == project_id
    assert initial_state["topic"] == "成都"
    assert initial_state["mode"] == "plan"
    assert initial_state["requirements"] == requirements
    assert initial_state["turn_id"]  # docs/ISSUES.md第142条：新一轮交互要生成非空turn_id
    assert config == {"configurable": {"thread_id": f"{project_id}:plan"}}

    # 没有命中interrupt，requirements.json应该已经持久化，供run_edit/run_companion复用
    saved = store.read_requirements(project_id)
    assert saved["topic"] == "成都"
    assert saved["requirements"] == requirements

    assert result == {"last_agent_result": {"agent": "route_agent"}}


def test_run_plan_persists_requirements_before_first_graph_invoke(isolated_project, monkeypatch):
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3, "notes": "不吃辣"}

    class _RequirementsCheckingGraph(_FakeGraph):
        def invoke(self, arg, config):
            saved = store.read_requirements(project_id)
            assert saved is not None
            assert saved["topic"] == "成都"
            assert saved["requirements"] == requirements
            return super().invoke(arg, config)

    fake_graph = _RequirementsCheckingGraph().queue(
        {"last_agent_result": {"agent": "route_agent"}}
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    run_plan(project_id, requirements)


def test_run_plan_reads_checkpoint_before_persisting_fresh_requirements(
    isolated_project, monkeypatch
):
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3}

    class _OrderingGraph(_FakeGraph):
        def get_state(self, config):
            assert store.read_requirements(project_id) is None
            return super().get_state(config)

    fake_graph = _OrderingGraph().queue({"last_agent_result": {"agent": "route_agent"}})
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    run_plan(project_id, requirements)


def test_run_plan_auto_confirms_interrupt_loop_until_resolved(isolated_project, monkeypatch):
    """没有真人在场时（CLI/测试场景），命中骨架确认的interrupt应该被自动
    Command(resume="confirm")推进，可能需要循环多次（比如骨架被自动确认后又被
    别的机制打断一次），直到result里不再有"__interrupt__"这个key。"""
    project_id = isolated_project
    fake_graph = _FakeGraph().queue(
        {
            "__interrupt__": [
                {"question": "骨架已生成，是否确认？", "route_plan": {}, "can_confirm": True}
            ]
        },
        {
            "__interrupt__": [
                {"question": "还需要再确认一次？", "route_plan": {}, "can_confirm": True}
            ]
        },
        {"last_agent_result": {"agent": "integration_agent"}},
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    requirements = {"destination": "北京", "days": 2}
    result = run_plan(project_id, requirements)

    assert len(fake_graph.invoke_calls) == 3
    # 第一次是真正的initial_state，之后两次都必须是Command(resume="confirm")，
    # 不应该重新传initial_state（那会导致骨架确认节点从头重跑）。turn_id（docs/ISSUES.md
    # 第142条）在函数顶部生成一次，循环内每次resume都要带上同一个值，让整个自动confirm
    # 循环归属同一turn。
    first_arg, first_config = fake_graph.invoke_calls[0]
    assert first_arg["mode"] == "plan"
    turn_id = first_arg["turn_id"]
    assert turn_id
    for arg, config in fake_graph.invoke_calls[1:]:
        assert arg == Command(resume="confirm", update={"turn_id": turn_id})
        assert config == first_config  # 同一个thread_id，不能中途换掉

    assert "__interrupt__" not in result
    assert result == {"last_agent_result": {"agent": "integration_agent"}}
    # requirements在首次图调用前已持久化，不受中间打断了几次影响
    assert store.read_requirements(project_id)["requirements"] == requirements


def test_run_plan_returns_route_proposal_as_action_required_without_auto_decision(
    isolated_project, monkeypatch
):
    proposal_interrupt = {
        "__interrupt__": [
            {
                "question": "请选择是否调整路线",
                "route_change_proposal": {
                    "proposal_id": "proposal-1",
                    "trigger": "timeliness",
                },
            }
        ]
    }
    fake_graph = _FakeGraph().queue(proposal_interrupt)
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    result = run_plan(isolated_project, {"destination": "成都", "days": 3})

    assert result["status"] == "action_required"
    assert result["__interrupt__"] == proposal_interrupt["__interrupt__"]
    assert len(fake_graph.invoke_calls) == 1


def test_run_plan_does_not_auto_confirm_unknown_interrupt(isolated_project, monkeypatch):
    unknown_interrupt = {"__interrupt__": [{"question": "需要外部选择", "custom": True}]}
    fake_graph = _FakeGraph().queue(unknown_interrupt)
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    result = run_plan(isolated_project, {"destination": "成都", "days": 3})

    assert result["status"] == "action_required"
    assert len(fake_graph.invoke_calls) == 1


def test_run_plan_starts_fresh_when_no_prior_checkpoint(isolated_project, monkeypatch):
    """没有历史checkpoint（_FakeGraph默认的get_state()返回values为空dict）时，
    run_plan必须走"从头开始"分支——传完整initial_state，不是None。这是改动前的
    既有行为，加了续跑检测后必须保持不变。"""
    project_id = isolated_project
    fake_graph = _FakeGraph().queue({"last_agent_result": {"agent": "route_agent"}})
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    requirements = {"destination": "成都", "days": 3}
    run_plan(project_id, requirements)

    assert len(fake_graph.invoke_calls) == 1
    initial_state, _ = fake_graph.invoke_calls[0]
    assert initial_state["mode"] == "plan"
    assert initial_state["requirements"] == requirements


def test_run_plan_resumes_with_none_input_when_prior_checkpoint_exists_mid_execution(
    isolated_project, monkeypatch
):
    """2026-08-13新增（见docs/ISSUES.md第120条）：已有checkpoint、且不是停在
    interrupt()节点（state.next不含"await_"开头的节点名，模拟"上次进程崩溃在某个
    普通节点执行中途"）时，必须传input=None续跑——不能重新构造/传fresh initial_state，
    那会被LangGraph当成"从START重新进入图"，把已经完整跑完的节点重新执行一遍。"""
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3}
    fake_graph = _FakeGraph().queue({"last_agent_result": {"agent": "integration_agent"}})
    fake_graph.set_state(
        values={
            "project_id": project_id,
            "topic": "成都",
            "requirements": requirements,
        },
        next_=("lifestyle_agent_node",),
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    result = run_plan(project_id, requirements)

    assert len(fake_graph.invoke_calls) == 1
    arg, config = fake_graph.invoke_calls[0]
    assert arg is None  # 续跑，不是fresh initial_state
    assert config == {"configurable": {"thread_id": f"{project_id}:plan"}}
    assert result == {"last_agent_result": {"agent": "integration_agent"}}


def test_run_plan_resumes_with_confirm_command_when_stopped_at_interrupt(isolated_project, monkeypatch):
    """已有checkpoint、且停在interrupt()节点（state.next含"await_skeleton_confirm"这类
    名字）时，续跑分支要用Command(resume="confirm")，跟改动前"命中interrupt时自动confirm"
    的既有行为保持一致——不能因为加了续跑检测就误判成"直接传None"。"""
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3}
    fake_graph = _FakeGraph().queue({"last_agent_result": {"agent": "route_agent"}})
    fake_graph.set_state(
        values={
            "project_id": project_id,
            "topic": "成都",
            "requirements": requirements,
        },
        next_=("await_skeleton_confirm_node",),
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    result = run_plan(project_id, requirements)

    assert len(fake_graph.invoke_calls) == 1
    arg, config = fake_graph.invoke_calls[0]
    assert isinstance(arg, Command)
    assert arg.resume == "confirm"
    assert arg.update["turn_id"]  # docs/ISSUES.md第142条：续跑分支也要带上本轮turn_id
    assert result == {"last_agent_result": {"agent": "route_agent"}}


def test_run_plan_resume_backfills_missing_requirements_only_from_checkpoint(
    isolated_project, monkeypatch
):
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3, "notes": "不吃辣"}
    fake_graph = _FakeGraph().queue({"last_agent_result": {"agent": "lifestyle_agent"}})
    fake_graph.set_state(
        values={
            "project_id": project_id,
            "topic": "成都",
            "requirements": requirements,
        },
        next_=("lifestyle_agent_node",),
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    run_plan(project_id, requirements)

    saved = store.read_requirements(project_id)
    assert saved["topic"] == "成都"
    assert saved["requirements"] == requirements


def test_run_plan_resume_rejects_invocation_checkpoint_mismatch_before_disk_write(
    isolated_project, monkeypatch
):
    project_id = isolated_project
    checkpoint_requirements = {"destination": "成都", "days": 3}
    fake_graph = _FakeGraph()
    fake_graph.set_state(
        values={
            "project_id": project_id,
            "topic": "成都",
            "requirements": checkpoint_requirements,
        },
        next_=("lifestyle_agent_node",),
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    with pytest.raises(ValueError, match="checkpoint不一致"):
        run_plan(project_id, {"destination": "成都", "days": 4})

    assert store.read_requirements(project_id) is None
    assert fake_graph.invoke_calls == []


def test_run_plan_resume_rejects_disk_checkpoint_mismatch_without_overwrite(
    isolated_project, monkeypatch
):
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3}
    store.init_project(project_id)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 2})
    requirements_path = store.workspace_dir(project_id) / "requirements.json"
    before = requirements_path.read_bytes()
    fake_graph = _FakeGraph()
    fake_graph.set_state(
        values={
            "project_id": project_id,
            "topic": "成都",
            "requirements": requirements,
        },
        next_=("lifestyle_agent_node",),
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    with pytest.raises(ValueError, match="requirements.json与checkpoint不一致"):
        run_plan(project_id, requirements)

    assert requirements_path.read_bytes() == before
    assert fake_graph.invoke_calls == []


def test_run_plan_completed_checkpoint_returns_without_mutating_files_or_invoking(
    isolated_project, monkeypatch
):
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3}
    store.init_project(project_id)
    store.write_requirements(project_id, "成都", requirements)
    requirements_path = store.workspace_dir(project_id) / "requirements.json"
    workspace = store.workspace_dir(project_id)
    before = {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    completed = {
        "project_id": project_id,
        "topic": "成都",
        "requirements": requirements,
        "integration_done": True,
    }
    fake_graph = _FakeGraph().set_state(values=completed, next_=())
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    result = run_plan(project_id, requirements)

    assert result == completed
    assert fake_graph.invoke_calls == []
    after = {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_run_plan_completed_checkpoint_does_not_backfill_missing_requirements(
    isolated_project, monkeypatch
):
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3}
    store.init_project(project_id)
    completed = {
        "project_id": project_id,
        "topic": "成都",
        "requirements": requirements,
        "integration_done": True,
    }
    fake_graph = _FakeGraph().set_state(values=completed, next_=())
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)
    requirements_path = store.workspace_dir(project_id) / "requirements.json"

    result = run_plan(project_id, requirements)

    assert result == completed
    assert fake_graph.invoke_calls == []
    assert not requirements_path.exists()


@pytest.mark.parametrize("content", ['{"topic":"成都"}', "null", "[]", "42", "{"])
def test_run_plan_resume_rejects_malformed_existing_requirements_file(
    isolated_project, monkeypatch, content
):
    project_id = isolated_project
    requirements = {"destination": "成都", "days": 3}
    store.init_project(project_id)
    requirements_path = store.workspace_dir(project_id) / "requirements.json"
    requirements_path.write_text(content, encoding="utf-8")
    before = requirements_path.read_bytes()
    fake_graph = _FakeGraph().set_state(
        values={
            "project_id": project_id,
            "topic": "成都",
            "requirements": requirements,
        },
        next_=("lifestyle_agent_node",),
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    with pytest.raises(ValueError, match="requirements.json"):
        run_plan(project_id, requirements)

    assert requirements_path.read_bytes() == before
    assert fake_graph.invoke_calls == []


def test_run_plan_rejects_duplicate_execution_before_building_graph(isolated_project, monkeypatch):
    """A second plan/resume process must not enter the shared checkpoint at all."""
    build_calls = []
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: build_calls.append(project_id))

    with store.project_plan_run_lock(isolated_project):
        with pytest.raises(store.ProjectRunAlreadyActiveError, match="active plan execution"):
            run_plan(isolated_project, {"destination": "青甘", "days": 8})

    assert build_calls == []


def test_companion_graph_singleton_fully_removed(isolated_project):
    """2026-08-13回归测试：_companion_graph模块级单例+_get_companion_graph()应该已经
    被完全移除（见docs/ISSUES.md第120条）——持久化checkpointer后不再需要它，留着反而
    是误导性的死代码。"""
    assert not hasattr(orchestrator, "_companion_graph")
    assert not hasattr(orchestrator, "_get_companion_graph")


# ---------------------------------------------------------------------------
# run_edit
# ---------------------------------------------------------------------------


def test_run_edit_raises_when_requirements_not_saved(isolated_project):
    """项目从没跑过run_plan（或者是补丁前生成的旧project），requirements.json不存在时，
    run_edit必须诚实报错，不能假装能继续、拿一个空requirements瞎跑。"""
    store.init_project(isolated_project)  # 只init，不写requirements.json

    with pytest.raises(ValueError, match="requirements.json"):
        run_edit(isolated_project, "lifestyle_agent_node", "Day1午餐换一家甜品店")


def test_run_edit_uses_saved_requirements_and_independent_edit_thread_id(isolated_project, monkeypatch):
    project_id = isolated_project
    store.init_project(project_id)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})

    fake_graph = _FakeGraph().queue({"last_agent_result": {"agent": "integration_agent"}})
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    result = run_edit(project_id, "lifestyle_agent_node", "Day1午餐换一家甜品店")

    assert len(fake_graph.invoke_calls) == 1
    initial_state, config = fake_graph.invoke_calls[0]
    # 独立于":plan"的thread_id——不能跟run_plan复用同一个checkpointer thread
    assert config == {"configurable": {"thread_id": f"{project_id}:edit"}}
    assert initial_state["mode"] == "edit"
    assert initial_state["topic"] == "成都"
    assert initial_state["requirements"] == {"destination": "成都", "days": 3}
    assert initial_state["entry_target"] == "lifestyle_agent_node"
    assert initial_state["edit_request"] == "Day1午餐换一家甜品店"
    assert initial_state["turn_id"]  # docs/ISSUES.md第142条：新一轮交互要生成非空turn_id
    # 普通edit入口不需要day/poi_name
    assert "day" not in initial_state
    assert "poi_name" not in initial_state

    assert result == {"last_agent_result": {"agent": "integration_agent"}}


def test_run_edit_poi_detail_entry_requires_day_and_poi_name(isolated_project):
    store.init_project(isolated_project)
    store.write_requirements(isolated_project, "成都", {"destination": "成都", "days": 3})

    with pytest.raises(ValueError, match="day.*poi_name|poi_name.*day"):
        run_edit(isolated_project, "poi_detail_edit_node", "改一下山门的描述")


def test_run_edit_poi_detail_entry_passes_day_and_poi_name_into_initial_state(isolated_project, monkeypatch):
    project_id = isolated_project
    store.init_project(project_id)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})

    fake_graph = _FakeGraph().queue({"last_agent_result": {"agent": "poi_detail_agent"}})
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    run_edit(project_id, "poi_detail_edit_node", "确认一下山门描述没问题", day=2, poi_name="青城山")

    initial_state, _ = fake_graph.invoke_calls[0]
    assert initial_state["day"] == 2
    assert initial_state["poi_name"] == "青城山"


# ---------------------------------------------------------------------------
# run_channel_start / run_channel_resume
# ---------------------------------------------------------------------------


def test_run_channel_start_extracts_requirements_and_does_not_auto_confirm(isolated_project, monkeypatch):
    """飞书等长连接渠道场景：跟run_plan最核心的区别——命中interrupt时必须原样返回，
    不能像run_plan那样自动confirm（真实场景没有"没有真人在场"这回事，必须真等真人回复）。"""
    project_id = isolated_project
    fake_graph = _FakeGraph().queue({"__interrupt__": [{"question": "还需要补充预算吗？"}]})
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)
    monkeypatch.setattr(
        orchestrator, "_extract_requirements_fields", lambda msg: {"destination": "成都", "days": 3}
    )

    result = run_channel_start(project_id, "帮我规划成都三天旅游")

    # store.init_project()确实先跑过
    assert store.read_progress(project_id) is not None

    assert len(fake_graph.invoke_calls) == 1
    initial_state, config = fake_graph.invoke_calls[0]
    assert config == {"configurable": {"thread_id": f"{project_id}:channel"}}
    assert initial_state["mode"] == "plan"
    # 开场白里已经提取到的destination应该预填进topic，不用逼用户在第一句问题里重复
    assert initial_state["topic"] == "成都"
    assert initial_state["requirements"] == {}
    assert initial_state["requirements_draft"] == {"destination": "成都", "days": 3}
    assert initial_state["turn_id"]  # docs/ISSUES.md第142条：新一轮交互要生成非空turn_id

    # 命中interrupt时原样返回，不自动confirm（这是跟run_plan最核心的行为区别）
    assert "__interrupt__" in result

    # 需求还没收集完整（result里没有requirements/topic），不应该误写requirements.json
    assert store.read_requirements(project_id) is None


def test_run_channel_start_persists_requirements_once_gathering_completes(isolated_project, monkeypatch):
    project_id = isolated_project
    fake_graph = _FakeGraph().queue(
        {"requirements": {"destination": "成都", "days": 3}, "topic": "成都", "last_agent_result": {}}
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)
    monkeypatch.setattr(orchestrator, "_extract_requirements_fields", lambda msg: {"destination": "成都"})

    run_channel_start(project_id, "帮我规划成都旅游")

    saved = store.read_requirements(project_id)
    assert saved["topic"] == "成都"
    assert saved["requirements"] == {"destination": "成都", "days": 3}


def test_run_channel_resume_finds_same_thread_via_persistent_checkpointer(isolated_project, monkeypatch):
    """2026-08-13起build_graph()改用持久化checkpointer（见docs/ISSUES.md第120条），
    run_channel_start/run_channel_resume不再需要复用同一个Python图对象才能找到之前暂停的
    thread——只要thread_id一致，指向同一个sqlite文件就行，所以每次调用各自新建
    build_graph(project_id)是预期行为（不再是"只调用一次"）。这里验证：build_graph每次
    都被调用（各自传入project_id），且两次invoke用的是同一个thread_id（模拟真实场景里
    两次build_graph(project_id)会打开指向同一个sqlite文件的连接，找到同一份历史）。"""
    project_id = isolated_project
    build_calls = {"n": 0}

    fake_graph = _FakeGraph().queue(
        {"__interrupt__": [{"question": "第一次追问"}]},
        {"last_agent_result": {"agent": "main_agent"}},
    )

    def fake_build_graph(project_id):
        build_calls["n"] += 1
        return fake_graph

    monkeypatch.setattr(orchestrator, "build_graph", fake_build_graph)
    monkeypatch.setattr(orchestrator, "_extract_requirements_fields", lambda msg: {"destination": "成都"})

    run_channel_start(project_id, "帮我规划成都旅游")
    run_channel_resume(project_id, "三天")

    # 每次调用各自build_graph(project_id)——不再依赖模块级单例缓存。
    assert build_calls["n"] == 2
    assert len(fake_graph.invoke_calls) == 2

    resume_arg, resume_config = fake_graph.invoke_calls[1]
    assert isinstance(resume_arg, Command)
    assert resume_arg.resume == "三天"
    assert resume_arg.update["turn_id"]  # docs/ISSUES.md第142条：每次独立消息生成新turn_id
    assert resume_config == {"configurable": {"thread_id": f"{project_id}:channel"}}


def test_run_channel_resume_persists_requirements_when_result_completes_gathering(isolated_project, monkeypatch):
    project_id = isolated_project
    fake_graph = _FakeGraph().queue(
        {"requirements": {"destination": "上海", "days": 5}, "topic": "上海", "last_agent_result": {}}
    )
    monkeypatch.setattr(orchestrator, "build_graph", lambda project_id: fake_graph)

    run_channel_resume(project_id, "确认")

    saved = store.read_requirements(project_id)
    assert saved["topic"] == "上海"
    assert saved["requirements"] == {"destination": "上海", "days": 5}
