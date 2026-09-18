"""核心素材agent的构建。MVP阶段主链路的素材入库走 src/materials/ingest.py 的确定性脚本，
这个react-agent对象用于"用户单独@核心素材agent"做维护性调用/自主采集，见
src/agents/material_agent/collection_graph.py。
"""
from __future__ import annotations

from pathlib import Path

from langgraph.prebuilt import create_react_agent

from src.config import get_chat_model
from src.tools.material_tools import make_material_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def build_material_agent(
    topic: str,
    project_id: str = "_global",
    captured: dict | None = None,
    seen: set | None = None,
    platform: str = "all",
):
    """captured/seen 传入时，agent额外拥有search_bilibili/search_douyin/propose_candidates
    采集工具（见 src/tools/material_collection_tools.py），供 collection_graph.py 的
    material_propose_node 使用；不传则只有检索/追加能力（用户单纯查询素材库时的用法）。
    platform（"all"/"bilibili"/"douyin"）限定这次只绑定对应平台的搜索工具，见
    src/tools/material_collection_tools.py::make_collection_tools。
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    tools = make_material_tools(
        topic,
        project_id,
        captured=captured,
        seen=seen,
        platform=platform,
        actor="material_agent",
        allow_append_evidence=False,
    )
    return create_react_agent(get_chat_model(streaming=True), tools, prompt=prompt)
