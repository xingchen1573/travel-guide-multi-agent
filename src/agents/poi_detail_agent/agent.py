"""POI详情agent的构建。

跟其他agent不同：POI详情agent是"一个人设、多个并行实例"（见docs/AGENTS.md第0节）——
"树木生成中"阶段会被Send API对每个needs_detail=true的POI各自派发一次，每个实例独立
只关心自己被分配到的那个POI（day+poi_name），因此构建时需要额外绑定这两个上下文参数，
供write_agent_output工具知道该写哪一个day{N}_{poi_slug}.json文件。
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from src.config import MIMO_CHAT_MODEL
from src.context_editing import ConvergenceNudgePolicy, build_context_editing_hook
from src.agent_checkpoint import get_agent_checkpointer
from src.llm_rotation import build_chat_model_with_fallback, build_poi_recovery_chat_model
from src.tools.amap_tools import make_amap_tools
from src.tools.context_tools import make_context_tools
from src.tools.material_tools import make_material_tools
from src.tools.web_search_tools import make_web_search_tools
from src.tools.workspace_tools import make_workspace_tools
from src.workspace.store import poi_slug

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _poi_detail_chat_model():
    """POI 明细是长 ReAct 任务；流式传输避免边缘层等待完整响应时超时。"""
    return build_chat_model_with_fallback(
        mimo_model=MIMO_CHAT_MODEL,
        streaming=True,
    )


def _poi_detail_provider_recovery_model():
    """主GPT-Load退避耗尽后，受限恢复实例使用独立末级渠道。"""
    return build_poi_recovery_chat_model()


def build_poi_detail_agent(
    project_id: str,
    topic: str,
    day: int,
    poi_name: str,
    *,
    checkpointer=None,
    repair_only: bool = False,
    repair_allow_retrieval: bool = False,
    provider_recovery: bool = False,
):
    if repair_only and provider_recovery:
        raise ValueError("repair_only and provider_recovery are mutually exclusive")
    if provider_recovery:
        base_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        prompt_body, separator, _tool_section = base_prompt.partition("\n## 可用工具\n")
        if not separator:
            prompt_body = base_prompt
        prompt = (
            prompt_body.rstrip()
            + "\n\n## Provider超时恢复模式\n"
            "之前的研究过程已因Provider故障在首次结构化写入前中断。"
            "任务消息会提供已持久化的候选证据和未验证工作笔记。你只负责把现有证据"
            "整理成保守、可核验的POI路线，不得重新联网、追加证据、调用高德或修改已有文件。"
            "working notes只是待重新核对的提示，不是可引用事实；必须回到Evidence内容判断。"
            "证据不足时应减少具体断言、改用estimate/unverified或省略非必要细节。"
            "最终必须调用write_agent_output完成首次写入，不要只输出文字总结。\n\n"
            "## 可用工具\n"
            "retrieve_materials、write_agent_output\n"
        )
    elif repair_only:
        available_tools = "write_agent_output_patch"
        retrieval_rule = "不要重新检索；根据核验理由收窄或删除没有充分依据的具体断言。"
        if repair_allow_retrieval:
            available_tools = (
                "retrieve_materials、append_material、web_search、write_agent_output_patch"
            )
            retrieval_rule = (
                "系统已经补充了新素材；仅在替换失败引用时检索这些新证据，仍优先收窄表述。"
            )
        prompt = (
            "你是POI详情引用核验后的局部返修角色。任务消息会直接提供问题清单和受影响"
            f"途经点的完整当前结构。{retrieval_rule}不得调用read_workspace、write_agent_output或高德工具，不得重新生成整个POI，"
            "不得修改任务中未列出的路线或途经点。最终必须调用write_agent_output_patch，"
            "每条claim必须显式填写support_mode。direct只保留原文直接支持的外部事实；"
            "如果一句同时包含外部事实和‘本方案沿用’、‘适合顺路游览’等路线编排，"
            "必须拆分；编排部分按真实来源使用user_constraint、derived或estimate，不得共用direct引用。"
            "claimed_entities只填写claim实际指向的具体对象，不要把时间、顺序、动作、价格、"
            "玩法或景区直通车/观光车/接驳车等普通交通类别拼成待核验实体。"
            "无证的精确开放、发车或票价事实不能用estimate兜底；应删除或标unverified。"
            "routes中只提交route_name和任务指定途经点的完整修正版；未提交的途经点由程序保留。"
            f"不要只输出文字总结。可用工具仅有{available_tools}。"
        )
    else:
        prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    log_scope = f"poi_detail_agent/day{day}_{poi_slug(poi_name)}"
    research_context = {
        "actor": "poi_detail_agent",
        "stage": "poi_detail",
        "target_key": f"poi_detail:day:{day}:poi:{poi_name}",
    }
    material_tools = make_material_tools(
        topic,
        project_id,
        actor="poi_detail_agent",
        log_scope=log_scope,
        allow_append_evidence=True,
        research_target_key=f"poi_detail:day:{day}:poi:{poi_name}",
        target_entity=poi_name,
    )
    workspace_tools = make_workspace_tools(
        project_id, "poi_detail_agent", day=day, poi_name=poi_name, topic=topic
    )
    if provider_recovery:
        material_tools = [tool for tool in material_tools if tool.name == "retrieve_materials"]
        workspace_tools = [
            tool for tool in workspace_tools if tool.name == "write_agent_output"
        ]
        tools = material_tools + workspace_tools
    elif repair_only:
        workspace_tools = [
            tool for tool in workspace_tools if tool.name == "write_agent_output_patch"
        ]
        tools = workspace_tools
        if repair_allow_retrieval:
            tools = (
                material_tools
                + make_web_search_tools(
                    project_id,
                    log_scope,
                    research_context=research_context,
                )
                + tools
            )
    else:
        tools = (
            material_tools
            + make_amap_tools(
                topic,
                project_id=project_id,
                log_scope=log_scope,
                research_context=research_context,
            )
            + make_web_search_tools(
                project_id,
                log_scope,
                research_context=research_context,
            )
            + workspace_tools
            + make_context_tools(project_id, log_scope)
        )
    if checkpointer is None:
        checkpointer = get_agent_checkpointer(project_id)
    convergence_policy = None
    if not repair_only and not provider_recovery:
        convergence_policy = ConvergenceNudgePolicy(
            work_unit=f"poi_detail_day_{day}_{poi_slug(poi_name)}",
            research_tools=(
                "retrieve_materials",
                "retrieve_project_evidence",
                "web_search",
                "amap_query",
                "amap_driving_distance",
            ),
            expected_write_tools=("write_agent_output", "write_agent_output_patch"),
            delivery_description=f"{poi_name}的POI内部路线",
            first_threshold=18,
            second_threshold=30,
        )
    model = (
        _poi_detail_provider_recovery_model()
        if provider_recovery
        else _poi_detail_chat_model()
    )
    return create_react_agent(
        model, tools, prompt=prompt,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=prompt),),
            tools=tuple(tools),
            convergence_policy=convergence_policy,
        ),
        checkpointer=checkpointer,
    )
