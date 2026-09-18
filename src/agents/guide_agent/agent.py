"""随身向导agent的构建。

跟其他agent不同：guide_agent是陪伴阶段权限最小的agent（见docs/AGENTS.md第7节），
只读已生成的final_guide.md/outputs/poi_details/，不联网、不检索素材库、不写公共区。
make_workspace_tools对没有匹配任何写分支的actor只会返回read_workspace这一个工具
（见src/tools/workspace_tools.py），天然满足"只读"约束，不需要额外的权限判断代码。

结构化产出（covered标记）靠captured-dict边车容器（见src/tools/guide_tools.py），
不解析自由文本——由调用方（src/orchestrator.py的guide_agent_node）传入并在
invoke完成后读回。
"""
from __future__ import annotations

from pathlib import Path

from langgraph.prebuilt import create_react_agent

from src.llm_rotation import build_companion_chat_model
from src.tools.guide_tools import make_guide_tools
from src.tools.workspace_tools import make_workspace_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def build_guide_agent(project_id: str, captured: dict):
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    tools = make_workspace_tools(project_id, "guide_agent") + make_guide_tools(captured)
    return create_react_agent(build_companion_chat_model(), tools, prompt=prompt)
