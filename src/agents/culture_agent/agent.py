"""文化科普agent的构建。

不绑retrieve_materials/amap_query——素材库是UGC旅游攻略内容（吃什么/怎么玩），不是为
历史文化科普设计的；地理位置校验也不是这个agent的职责。绑web_search（跟realtime_agent
同款联网能力）用于查证历史文化类知识，避免模型凭自身知识杜撰具体史实，见
docs/ARCHITECTURE.md第3.2节。

跟pitfall_agent/lifestyle_agent不同：这个agent的产出（outputs/culture.json）是累加式
历史记录，不是按固定字段整份重新生成——每次只回答一个新问题，把新问答追加到已有历史后面。
"""
from __future__ import annotations

from pathlib import Path

from langgraph.prebuilt import create_react_agent

from src.config import MIMO_CHAT_MODEL
from src.llm_rotation import build_chat_model_with_fallback
from src.tools.web_search_tools import make_web_search_tools
from src.tools.workspace_tools import make_workspace_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _culture_chat_model():
    """文化补充可独立复核，MiMo 主用标准版控制成本。"""
    return build_chat_model_with_fallback(
        mimo_model=MIMO_CHAT_MODEL,
        streaming=True,
    )


def build_culture_agent(project_id: str):
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    tools = make_workspace_tools(project_id, "culture_agent") + make_web_search_tools(project_id, "culture_agent")
    return create_react_agent(_culture_chat_model(), tools, prompt=prompt)
