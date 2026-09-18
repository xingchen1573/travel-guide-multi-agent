"""Lifestyle Agent构建级Harness测试，不调用真实模型。"""
from src.agents.lifestyle_agent import agent as lifestyle_agent


def _capture_amap_wiring(monkeypatch, builder):
    amap_calls = []

    monkeypatch.setattr(
        lifestyle_agent,
        "build_chat_model_with_fallback",
        lambda **_kwargs: "model",
    )
    monkeypatch.setattr(lifestyle_agent, "make_material_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(lifestyle_agent, "make_web_search_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(lifestyle_agent, "make_workspace_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(lifestyle_agent, "make_context_tools", lambda *args, **kwargs: [])

    def fake_make_amap_tools(*args, **kwargs):
        amap_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(lifestyle_agent, "make_amap_tools", fake_make_amap_tools)
    monkeypatch.setattr(lifestyle_agent, "build_context_editing_hook", lambda **_kwargs: object())
    monkeypatch.setattr(lifestyle_agent, "create_react_agent", lambda *args, **kwargs: object())

    builder("project-1", "青甘大环线")
    return amap_calls


def test_build_lifestyle_agent_persists_amap_evidence_in_lifestyle_scope(monkeypatch):
    calls = _capture_amap_wiring(monkeypatch, lifestyle_agent.build_lifestyle_agent)

    assert calls == [
        (
            ("青甘大环线",),
            {
                "project_id": "project-1",
                "log_scope": "lifestyle_agent",
                "research_context": {
                    "actor": "lifestyle_agent",
                    "stage": "lifestyle",
                    "target_key": "lifestyle:all",
                },
            },
        )
    ]


def test_build_lifestyle_retry_agent_persists_amap_evidence_in_lifestyle_scope(monkeypatch):
    calls = _capture_amap_wiring(monkeypatch, lifestyle_agent.build_lifestyle_agent_retry_fix)

    assert calls == [
        (
            ("青甘大环线",),
            {
                "project_id": "project-1",
                "log_scope": "lifestyle_agent",
                "research_context": {
                    "actor": "lifestyle_agent",
                    "stage": "lifestyle",
                    "target_key": "lifestyle:all",
                },
            },
        )
    ]
