"""路线规划agent的构建。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from src.config import MIMO_PRO_CHAT_MODEL
from src.context_editing import ConvergenceNudgePolicy, build_context_editing_hook
from src.llm_rotation import build_chat_model_with_fallback
from src.tools.amap_tools import make_amap_tools
from src.tools.context_tools import make_context_tools
from src.tools.material_tools import make_material_tools
from src.tools.web_search_tools import make_web_search_tools
from src.tools.workspace_tools import make_workspace_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _route_chat_model():
    """路线结构影响全局，MiMo 主用 Pro；长上下文使用流式传输。"""
    return build_chat_model_with_fallback(
        mimo_model=MIMO_PRO_CHAT_MODEL,
        streaming=True,
    )


def build_route_agent(
    project_id: str,
    topic: str,
    *,
    checkpointer=None,
    write_mode: Literal["full", "facts_only", "proposal_only"] = "full",
    convergence_work_unit: str | None = None,
    proposal_work_unit_id: str = "",
):
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    if write_mode == "facts_only":
        prompt += (
            "\n\n## 当前运行模式：事实修复\n"
            "你这次不是重新规划路线。只能调用write_route_facts_patch修改既有天的notes、sources和claims；"
            "如果该天已经有claims，修改notes时必须同步提交修正后的完整claims；sources由claims自动汇总。"
            "事实返修不等于重新调研。先逐条判断问题属于证据错绑、复合说法、路线编排判断，还是"
            "确实缺少且有必要保留的具体事实：证据错绑优先改用已有正确证据或收窄；复合说法拆开或"
            "只保留已支持部分；路线顺序、适合人群、是否值得串联等编排判断用derived或estimate表达，"
            "不为它们搜索网页背书；只有保留具体硬事实会影响攻略价值、且现有证据确实不足时才继续检索。"
            "不要借返修新增问题清单之外的精确数字、政策、班次或景点事实；未失败的内容尽量保持不变。"
            "处理完当前工作单元的全部问题后，每个目标天只形成一次阶段Patch。"
            "不得改day、city、area、POI名称/顺序/所属天、needs_detail或天数。即使你认为路线结构"
            "应该调整，也不要绕过权限；如实说明需要结构调整，由系统另走用户确认。"
        )
    elif write_mode == "proposal_only":
        prompt += (
            "\n\n## 当前运行模式：结构调整提案\n"
            "正式route_plan已经由用户确认。你只能调用write_route_change_proposal提交候选，不能直接"
            "覆盖正式路线。changed_days只包含确实需要调整的既有天；reason和evidence必须说明为什么"
            "原路线不可执行。提案提交后由系统向用户展示差异并等待确认。"
        )
    tools = (
        make_material_tools(
            topic,
            project_id,
            actor="route_agent",
            log_scope="route_agent",
            allow_append_evidence=True,
        )
        + make_amap_tools(
            topic,
            project_id=project_id,
            log_scope="route_agent",
            include_entity_resolution=True,
            research_context={
                "actor": "route_agent",
                "stage": "route",
                "target_key": "route:all",
            },
        )
        + make_web_search_tools(
            project_id,
            "route_agent",
            research_context={
                "actor": "route_agent",
                "stage": "route",
                "target_key": "route:all",
            },
        )
        + make_workspace_tools(
            project_id,
            "route_agent",
            topic=topic,
            route_write_mode=write_mode,
            route_proposal_work_unit_id=proposal_work_unit_id,
        )
        + make_context_tools(project_id, "route_agent")
    )
    convergence_policy = None
    if write_mode == "facts_only" and convergence_work_unit:
        convergence_policy = ConvergenceNudgePolicy(
            work_unit=convergence_work_unit,
            research_tools=(
                "retrieve_materials",
                "retrieve_project_evidence",
                "web_search",
                "amap_query",
                "amap_driving_distance",
            ),
            expected_write_tools=("write_route_facts_patch",),
            delivery_description="路线事实修复结果",
        )
    return create_react_agent(
        _route_chat_model(), tools, prompt=prompt,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=prompt),),
            tools=tuple(tools),
            convergence_policy=convergence_policy,
        ),
        checkpointer=checkpointer,
    )
