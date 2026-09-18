from __future__ import annotations

from src.agents.integration_agent import agent as integration_agent


def test_integration_chat_model_enables_streaming(monkeypatch):
    captured = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return "model"

    monkeypatch.setattr(integration_agent, "build_chat_model_with_fallback", fake_build)

    assert integration_agent._integration_chat_model() == "model"
    assert captured["mimo_model"] == integration_agent.MIMO_PRO_CHAT_MODEL
    assert captured["streaming"] is True


def test_integration_agent_disables_parent_checkpoint_inheritance(monkeypatch):
    captured = {}

    def fake_create_react_agent(model, tools, **kwargs):
        captured.update(kwargs)
        return "integration-agent"

    monkeypatch.setattr(integration_agent, "_integration_chat_model", lambda: "model")
    monkeypatch.setattr(integration_agent, "make_workspace_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(integration_agent, "create_react_agent", fake_create_react_agent)

    result = integration_agent.build_integration_agent("project", "Chengdu")

    assert result == "integration-agent"
    assert captured["checkpointer"] is False
