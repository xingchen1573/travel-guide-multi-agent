"""避雷agent的构建。

不绑amap_query——避雷是内容检索（有没有踩坑反馈），不是地理位置校验，
见docs/ARCHITECTURE.md第7节。
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from src.config import MIMO_CHAT_MODEL
from src.context_editing import ConvergenceNudgePolicy, build_context_editing_hook
from src.llm_rotation import build_chat_model_with_fallback
from src.tools.context_tools import make_context_tools
from src.tools.material_tools import make_material_tools
from src.tools.workspace_tools import make_workspace_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _pitfall_chat_model():
    """避雷调研属于长上下文任务，使用与其他攻略 Agent 一致的 MiMo 链。"""
    return build_chat_model_with_fallback(
        mimo_model=MIMO_CHAT_MODEL,
        streaming=True,
    )


def build_pitfall_agent(
    project_id: str,
    topic: str,
    *,
    checkpointer=None,
    convergence_work_unit: str = "pitfall_full_batch",
):
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    tools = (
        make_material_tools(topic, project_id, actor="pitfall_agent", allow_append_evidence=False)
        + make_workspace_tools(project_id, "pitfall_agent", topic=topic)
        + make_context_tools(project_id, "pitfall_agent")
    )
    return create_react_agent(
        _pitfall_chat_model(),
        tools,
        prompt=prompt,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=prompt),),
            tools=tuple(tools),
            convergence_policy=ConvergenceNudgePolicy(
                work_unit=convergence_work_unit,
                research_tools=("retrieve_materials",),
                expected_write_tools=("write_agent_output", "write_agent_output_patch"),
                delivery_description="与Lifestyle逐项绑定的避雷结果",
                first_threshold=24,
                second_threshold=40,
            ),
        ),
        checkpointer=checkpointer,
    )


def build_pitfall_agent_write_only(project_id: str, topic: str):
    prompt = _PROMPT_PATH.read_text(encoding="utf-8") + (
        "\n\n【最终提交阶段】研究阶段已经结束。请消费当前对话中的Lifestyle输入和已检索证据，"
        "立即调用write_agent_output提交全部实际(field, day)组合。查不到可靠负面证据时，"
        "warning留空并保持target_recommendation与Lifestyle原文完全一致；不要继续检索，"
        "也不要只输出文字总结。"
    )
    tools = make_workspace_tools(project_id, "pitfall_agent", topic=topic)
    return create_react_agent(
        _pitfall_chat_model(),
        tools,
        prompt=prompt,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=prompt),), tools=tuple(tools)
        ),
    )
