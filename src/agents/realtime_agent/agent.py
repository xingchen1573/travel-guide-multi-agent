"""实时agent的构建。

定位收窄为"真正需要联网的时效性信息"（见docs/ARCHITECTURE.md第3.5节），不绑
retrieve_materials（不是它的职责范围，避免和衣食住行agent工作重叠）；绑amap_query
用于POI/营业状态相关的结构化校验（目前.env未配置时是stub，见src/tools/amap_tools.py）；
绑web_search（Tavily）用于天气/临时闭馆/最新事件这类需要真实联网搜索才能回答的问题
（同样未配置TAVILY_API_KEY时走stub降级，见src/tools/web_search_tools.py）；绑
get_current_time/create_reminder用于主动提醒机制v1（docs/ARCHITECTURE.md第14.1节）。

结构化产出（纯问答 vs 发起修改）靠captured-dict边车容器（见src/tools/realtime_tools.py），
由调用方（src/orchestrator.py的realtime_agent_node）传入并在invoke完成后读回。
"""
from __future__ import annotations

from pathlib import Path

from langgraph.prebuilt import create_react_agent

from src.llm_rotation import build_companion_chat_model
from src.tools.amap_tools import make_amap_tools
from src.tools.realtime_tools import make_realtime_tools
from src.tools.web_search_tools import make_web_search_tools
from src.tools.workspace_tools import make_workspace_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def build_realtime_agent(project_id: str, captured: dict):
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    tools = (
        make_workspace_tools(project_id, "realtime_agent")
        + make_amap_tools()
        + make_web_search_tools(project_id, "realtime_agent")
        + make_realtime_tools(project_id, captured)
    )
    return create_react_agent(build_companion_chat_model(), tools, prompt=prompt)
