"""Route Agent构建级Harness测试，不调用真实模型。"""

from src.agents.route_agent import agent as route_agent
from src.context_editing import ConvergenceNudgePolicy


def test_facts_only_route_agent_opt_in_wires_convergence_nudge(monkeypatch):
    captured = {}
    hook_kwargs = {}

    monkeypatch.setattr(route_agent, "_route_chat_model", lambda: "model")
    monkeypatch.setattr(route_agent, "make_material_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_amap_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_web_search_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_workspace_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_context_tools", lambda *args, **kwargs: [])

    def fake_build_hook(**kwargs):
        hook_kwargs.update(kwargs)
        return "hook"

    monkeypatch.setattr(route_agent, "build_context_editing_hook", fake_build_hook)

    def fake_create_react_agent(model, tools, **kwargs):
        captured.update(model=model, tools=tools, kwargs=kwargs)
        return "route-agent"

    monkeypatch.setattr(route_agent, "create_react_agent", fake_create_react_agent)

    result = route_agent.build_route_agent(
        "project-1",
        "峨眉山",
        checkpointer=False,
        write_mode="facts_only",
        convergence_work_unit="route_facts_day_1",
    )

    assert result == "route-agent"
    policy = hook_kwargs["convergence_policy"]
    assert isinstance(policy, ConvergenceNudgePolicy)
    assert policy.work_unit == "route_facts_day_1"
    assert policy.expected_write_tools == ("write_route_facts_patch",)
    assert captured["kwargs"]["pre_model_hook"] == "hook"
    prompt = captured["kwargs"]["prompt"]
    assert "事实返修不等于重新调研" in prompt
    assert "不为它们搜索网页背书" in prompt
    assert "不要借返修新增问题清单之外" in prompt
    assert "每个目标天只形成一次阶段Patch" in prompt


def test_route_agent_without_work_unit_does_not_enable_nudge(monkeypatch):
    hook_kwargs = {}

    monkeypatch.setattr(route_agent, "_route_chat_model", lambda: "model")
    monkeypatch.setattr(route_agent, "make_material_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_amap_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_web_search_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_workspace_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_context_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        route_agent,
        "build_context_editing_hook",
        lambda **kwargs: hook_kwargs.update(kwargs) or "hook",
    )
    monkeypatch.setattr(route_agent, "create_react_agent", lambda *args, **kwargs: "route-agent")

    route_agent.build_route_agent("project-1", "峨眉山", write_mode="facts_only")

    assert hook_kwargs["convergence_policy"] is None


def test_proposal_route_agent_forwards_system_work_unit_to_workspace_tool(monkeypatch):
    workspace_kwargs = {}

    monkeypatch.setattr(route_agent, "_route_chat_model", lambda: "model")
    monkeypatch.setattr(route_agent, "make_material_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_amap_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "make_web_search_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        route_agent,
        "make_workspace_tools",
        lambda *args, **kwargs: workspace_kwargs.update(kwargs) or [],
    )
    monkeypatch.setattr(route_agent, "make_context_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(route_agent, "build_context_editing_hook", lambda **kwargs: "hook")
    monkeypatch.setattr(
        route_agent,
        "create_react_agent",
        lambda *args, **kwargs: "route-agent",
    )

    route_agent.build_route_agent(
        "project-1",
        "大理",
        write_mode="proposal_only",
        proposal_work_unit_id="work-unit-erhai-1",
    )

    assert workspace_kwargs["route_proposal_work_unit_id"] == "work-unit-erhai-1"
