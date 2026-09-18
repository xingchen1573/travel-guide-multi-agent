"""整合agent的构建。"""
from __future__ import annotations

from pathlib import Path

from langgraph.prebuilt import create_react_agent

from src.config import MIMO_PRO_CHAT_MODEL
from src.llm_rotation import build_chat_model_with_fallback
from src.tools.workspace_tools import make_workspace_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _integration_chat_model():
    """最终整合决定整份攻略质量，MiMo 主用 Pro；长响应使用流式传输。

    gptload 对较长非流式响应真实出现过连续超时和不完整JSON返回；流式传输让代理持续
    收到数据，避免整份攻略生成完后才一次性返回。渠道和模型不变，也不回退到商汤。
    """
    return build_chat_model_with_fallback(
        mimo_model=MIMO_PRO_CHAT_MODEL,
        streaming=True,
    )


def build_integration_agent(project_id: str, topic: str, *, checkpointer=None):
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    tools = [
        workspace_tool
        for workspace_tool in make_workspace_tools(project_id, "integration_agent")
        if workspace_tool.name in {"write_final_guide", "report_conflict"}
    ]
    # 默认仍不继承父图checkpointer；调用方只有在用当前完整Prompt哈希隔离thread时，
    # 才显式传入per-project checkpointer，从而兼顾超时续跑与输入变化后不复用旧上下文。
    return create_react_agent(
        _integration_chat_model(),
        tools,
        prompt=prompt,
        checkpointer=checkpointer if checkpointer is not None else False,
    )
