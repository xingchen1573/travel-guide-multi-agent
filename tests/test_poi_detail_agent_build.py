"""POI Detail Agent构建级Harness测试，不调用真实模型。"""

from src.agents.poi_detail_agent import agent as poi_agent
from src.context_editing import ConvergenceNudgePolicy


def _stub_dependencies(monkeypatch, hook_kwargs):
    monkeypatch.setattr(poi_agent, "_poi_detail_chat_model", lambda: "model")
    monkeypatch.setattr(
        poi_agent, "_poi_detail_provider_recovery_model", lambda: "recovery-model"
    )
    monkeypatch.setattr(poi_agent, "make_material_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(poi_agent, "make_amap_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(poi_agent, "make_web_search_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(poi_agent, "make_workspace_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(poi_agent, "make_context_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        poi_agent,
        "build_context_editing_hook",
        lambda **kwargs: hook_kwargs.update(kwargs) or "hook",
    )
    monkeypatch.setattr(
        poi_agent,
        "create_react_agent",
        lambda *args, **kwargs: "poi-agent",
    )


def test_poi_detail_agent_wires_conservative_convergence_nudge(monkeypatch):
    hook_kwargs = {}
    _stub_dependencies(monkeypatch, hook_kwargs)

    result = poi_agent.build_poi_detail_agent(
        "project-1", "青甘", 5, "鸣沙山月牙泉", checkpointer=False
    )

    assert result == "poi-agent"
    policy = hook_kwargs["convergence_policy"]
    assert isinstance(policy, ConvergenceNudgePolicy)
    assert policy.work_unit == "poi_detail_day_5_鸣沙山月牙泉"
    assert policy.first_threshold == 18
    assert policy.second_threshold == 30
    assert policy.expected_write_tools == (
        "write_agent_output",
        "write_agent_output_patch",
    )


def test_poi_detail_repair_agent_does_not_add_research_nudge(monkeypatch):
    hook_kwargs = {}
    _stub_dependencies(monkeypatch, hook_kwargs)

    poi_agent.build_poi_detail_agent(
        "project-1",
        "青甘",
        5,
        "鸣沙山月牙泉",
        checkpointer=False,
        repair_only=True,
    )

    assert hook_kwargs["convergence_policy"] is None
    repair_prompt = hook_kwargs["static_messages"][0].content
    assert "每条claim必须显式填写support_mode" in repair_prompt
    assert "本方案沿用" in repair_prompt
    assert "外部事实和" in repair_prompt
    assert "必须拆分" in repair_prompt


def test_poi_prompt_defines_atomic_claim_and_entity_boundaries():
    prompt = poi_agent._PROMPT_PATH.read_text(encoding="utf-8")

    assert "一条claim只表达一个可独立核验的事实" in prompt
    assert "claimed_entities`只填写这条claim实际指向的具体对象名称" in prompt
    assert "七彩丹霞北门观光车顺序" in prompt
    assert "万马饮水时间" in prompt
    assert "景区直通车/观光车/接驳车" in prompt
    assert "景区”前缀本身不构成专名" in prompt
    assert "至少拆成寄存、价格、时刻三条claim" in prompt
    assert "不能改成`estimate`伪装为路线估算" in prompt


def test_poi_detail_provider_recovery_has_only_local_retrieval_and_initial_write(
    monkeypatch,
):
    hook_kwargs = {}
    captured = {}

    class FakeTool:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(poi_agent, "_poi_detail_chat_model", lambda: "model")
    monkeypatch.setattr(
        poi_agent, "_poi_detail_provider_recovery_model", lambda: "recovery-model"
    )
    monkeypatch.setattr(
        poi_agent,
        "make_material_tools",
        lambda *args, **kwargs: [FakeTool("retrieve_materials"), FakeTool("append_material")],
    )
    monkeypatch.setattr(
        poi_agent,
        "make_workspace_tools",
        lambda *args, **kwargs: [
            FakeTool("read_workspace"),
            FakeTool("write_agent_output"),
            FakeTool("write_agent_output_patch"),
        ],
    )
    monkeypatch.setattr(poi_agent, "make_amap_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(poi_agent, "make_web_search_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(poi_agent, "make_context_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        poi_agent,
        "build_context_editing_hook",
        lambda **kwargs: hook_kwargs.update(kwargs) or "hook",
    )

    def fake_create(_model, tools, **kwargs):
        captured["model"] = _model
        captured["tools"] = [tool.name for tool in tools]
        captured.update(kwargs)
        return "poi-agent"

    monkeypatch.setattr(poi_agent, "create_react_agent", fake_create)

    result = poi_agent.build_poi_detail_agent(
        "project-1",
        "青甘",
        5,
        "鸣沙山月牙泉",
        checkpointer=False,
        provider_recovery=True,
    )

    assert result == "poi-agent"
    assert captured["model"] == "recovery-model"
    assert captured["tools"] == ["retrieve_materials", "write_agent_output"]
    assert captured["checkpointer"] is False
    assert hook_kwargs["convergence_policy"] is None
    assert "Provider超时恢复模式" in captured["prompt"]
    assert "retrieve_materials、write_agent_output" in captured["prompt"]
