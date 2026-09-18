"""Pitfall Agent构建级Harness测试，不调用真实模型。"""
from src.agents.pitfall_agent import agent as pitfall_agent


def test_build_pitfall_agent_wires_context_hook_checkpoint_and_working_notes(monkeypatch):
    captured = {}
    sentinel_hook = object()
    sentinel_checkpointer = object()

    model_kwargs = {}

    def fake_build_model(**kwargs):
        model_kwargs.update(kwargs)
        return "model"

    monkeypatch.setattr(pitfall_agent, "build_chat_model_with_fallback", fake_build_model)
    monkeypatch.setattr(pitfall_agent, "make_material_tools", lambda *args, **kwargs: ["retrieve_materials"])
    monkeypatch.setattr(pitfall_agent, "make_workspace_tools", lambda *args, **kwargs: ["write_agent_output"])
    monkeypatch.setattr(pitfall_agent, "make_context_tools", lambda *args, **kwargs: ["record_finding"])
    hook_kwargs = {}

    def fake_build_hook(**kwargs):
        hook_kwargs.update(kwargs)
        return sentinel_hook

    monkeypatch.setattr(pitfall_agent, "build_context_editing_hook", fake_build_hook)

    def fake_create_react_agent(model, tools, **kwargs):
        captured.update(model=model, tools=tools, kwargs=kwargs)
        return "pitfall-agent"

    monkeypatch.setattr(pitfall_agent, "create_react_agent", fake_create_react_agent)

    result = pitfall_agent.build_pitfall_agent(
        "project-1",
        "成都",
        checkpointer=sentinel_checkpointer,
    )

    assert result == "pitfall-agent"
    assert captured["model"] == "model"
    assert captured["tools"] == ["retrieve_materials", "write_agent_output", "record_finding"]
    assert captured["kwargs"]["pre_model_hook"] is sentinel_hook
    assert captured["kwargs"]["checkpointer"] is sentinel_checkpointer
    assert hook_kwargs["tools"] == tuple(captured["tools"])
    assert model_kwargs == {
        "mimo_model": pitfall_agent.MIMO_CHAT_MODEL,
        "streaming": True,
    }
