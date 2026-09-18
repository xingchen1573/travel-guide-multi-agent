"""衣食住行agent的构建。"""
from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from src.config import MIMO_CHAT_MODEL
from src.context_editing import ConvergenceNudgePolicy, build_context_editing_hook
from src.llm_rotation import build_chat_model_with_fallback
from src.tools.amap_tools import make_amap_tools
from src.tools.context_tools import make_context_tools
from src.tools.material_tools import make_material_tools
from src.tools.web_search_tools import make_web_search_tools
from src.tools.workspace_tools import make_workspace_tools

_PROMPT_PATH = Path(__file__).parent / "prompt.md"

# few-shot轨迹示例（docs/ISSUES.md第80条）：纯文字prompt规则（"检索反复失败后必须停止检索转去
# 调用write_agent_output"）经严格A/B对比证实对"检索空转→只写文字总结→忘记调用写入工具"这个
# 失败模式没有可观测效果（同一project固定其它变量，旧prompt/新prompt均58%成功率）。这是一个
# "在某个条件下必须做什么动作"的过程性/行为性模式，改用真实消息对象构造一段具体的对话轨迹
# （检索两次空结果→不经任何文字过渡直接调用write_agent_output），让模型在"看到长什么样"
# 而不是"被告知规则"的基础上决策，是否更有效待验证（A/B测试见该条修复记录）。
# 刻意用一个明显不同于任何真实目的地的占位地名（"某某镇"/"某某广场"），避免被模型误当成真实素材内容。
#
# 2026-08-05修复（docs/ISSUES.md第106条 haikou v3/v4 真实复现）：原 few-shot 的
# write_agent_output args 里 recommendation 写的是"素材库无相关素材，根据一般经验推荐：选择当地
# 口碑较好的家常菜馆"——用了"当地"这种笼统措辞，没把 route_plan 里的真实 POI 名（few-shot 里
# 是"某某广场"）带进产出。haikou v3/v4 真实跑里 lifestyle 反复检索空手而归触发兜底时，模型
# 复述了 few-shot 的占位名"某某镇"写进 outputs/lifestyle.json，final_guide 出现"某某镇"垃圾
# 内容。修复：few-shot 的 recommendation 改成展示"用 route_plan 里的真实 POI 名（few-shot 里
# 是'某某广场'）+ 素材库无覆盖诚实声明 + 方向性建议"的写法——让模型看到"应该用真实 POI 名
# 写诚实声明"这个模式，而不是笼统说"当地"。配合 _FORCE_WRITE_PROMPT_SUFFIX 里新加的"必须用
# route_plan 真实 POI 名，不要用'某某镇'这种占位名"硬约束，双保险。
_FEWSHOT_TRAJECTORY = [
    HumanMessage(
        content='路线规划已确定：{"days":[{"day":1,"city":"某某镇","area":"某某镇中心","pois":[{"name":"某某广场"}]}]}\n'
        '用户原始需求：目的地=某某镇，天数=1天，风格=不限，预算=不限，备注=无。'
        '请结合素材库检索结果，综合产出美食/穿搭/住宿/游玩四个维度的具体建议（每条建议附上sources来源），写入你的产出区。'
    ),
    AIMessage(content="", tool_calls=[{"name": "retrieve_materials", "args": {"query": "某某镇美食"}, "id": "fewshot_1"}]),
    ToolMessage(content="没有检索到相关素材。", tool_call_id="fewshot_1"),
    AIMessage(content="", tool_calls=[{"name": "retrieve_materials", "args": {"query": "某某镇住宿"}, "id": "fewshot_2"}]),
    ToolMessage(content="没有检索到相关素材。", tool_call_id="fewshot_2"),
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_agent_output",
                "args": {
                    # recommendation 里明确带上 route_plan 的真实 POI 名（few-shot 里是"某某广场"），
                    # 展示"用真实 POI 名 + 素材库无覆盖诚实声明 + 方向性建议"的写法，不要笼统说"当地"。
                    # items是列表格式（新patch写入语义）：首次生成场景items就是全部字段x天数各一条，
                    # 编辑已有产出场景只需要传真正改动的那几条。
                    "items": [
                        {"field": "food", "day": 1, "recommendation": "素材库无针对'某某广场'的具体美食检索结果。根据一般经验建议：到达某某广场后，可在广场周边就近觅食，优先选择当地居民消费较多的餐厅，避开纯网红打卡店；具体店名以现场点评软件实时查为准。", "claims": [{"target_path": "recommendation", "claim_text": "可在某某广场周边就近觅食，具体店名现场复核", "claim_type": "food", "support_mode": "estimate", "claimed_entities": ["某某广场"], "evidence_refs": []}]},
                        {"field": "outfit", "day": 1, "recommendation": "素材库无针对'某某广场'的穿搭检索结果。根据一般经验建议：出行前查询某某镇当日实时天气预报，按气温穿搭，穿舒适步行鞋（某某广场一带步行游览）。", "claims": [{"target_path": "recommendation", "claim_text": "出行前查当日天气并穿舒适步行鞋", "claim_type": "weather", "support_mode": "estimate", "evidence_refs": []}]},
                        {"field": "lodging", "day": 1, "recommendation": "素材库无针对'某某广场'的住宿检索结果。根据一般经验建议：选择靠近某某广场或某某镇中心、交通便利的连锁酒店/民宿，预订前查看近期评价，具体以OTA平台实时价格为准。", "claims": [{"target_path": "recommendation", "claim_text": "可优先选择靠近某某广场或镇中心的住宿", "claim_type": "lodging", "support_mode": "estimate", "claimed_entities": ["某某广场"], "evidence_refs": []}]},
                        {"field": "play", "day": 1, "recommendation": "素材库无针对'某某广场'的具体游玩检索结果。根据一般经验建议：按路线规划区已定的顺序游览某某广场，到达后先在广场周边散步熟悉环境，具体玩法以现场指示牌为准。", "claims": [{"target_path": "recommendation", "claim_text": "按路线规划游览某某广场，具体玩法现场复核", "claim_type": "play", "support_mode": "user_constraint", "claimed_entities": ["某某广场"], "evidence_refs": []}]},
                    ]
                },
                "id": "fewshot_3",
            }
        ],
    ),
    ToolMessage(content="衣食住行产出已写入 outputs/lifestyle.json", tool_call_id="fewshot_3"),
]


def _lifestyle_chat_model():
    """生活信息可局部校验和重跑，MiMo 主用标准版控制成本。"""
    return build_chat_model_with_fallback(
        mimo_model=MIMO_CHAT_MODEL,
        streaming=True,
    )


def build_lifestyle_agent(
    project_id: str,
    topic: str,
    *,
    checkpointer=None,
    convergence_work_unit: str | None = None,
):
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    research_target_key = None
    if convergence_work_unit and convergence_work_unit.startswith("lifestyle_day_"):
        research_target_key = "lifestyle:day:" + convergence_work_unit.removeprefix(
            "lifestyle_day_"
        )
    tools = (
        make_material_tools(
            topic,
            project_id,
            actor="lifestyle_agent",
            log_scope="lifestyle_agent",
            allow_append_evidence=True,
            research_target_key=research_target_key,
        )
        + make_amap_tools(
            topic,
            project_id=project_id,
            log_scope="lifestyle_agent",
            research_context={
                "actor": "lifestyle_agent",
                "stage": "lifestyle",
                "target_key": research_target_key or "lifestyle:all",
            },
        )
        + make_web_search_tools(
            project_id,
            "lifestyle_agent",
            research_context={
                "actor": "lifestyle_agent",
                "stage": "lifestyle",
                "target_key": research_target_key or "lifestyle:all",
            },
        )
        + make_workspace_tools(project_id, "lifestyle_agent", topic=topic)
        + make_context_tools(project_id, "lifestyle_agent")
    )

    def _prompt_with_fewshot(state):
        return [SystemMessage(content=system_prompt)] + _FEWSHOT_TRAJECTORY + state["messages"]

    convergence_policy = None
    if convergence_work_unit:
        convergence_policy = ConvergenceNudgePolicy(
            work_unit=convergence_work_unit,
            research_tools=(
                "retrieve_materials",
                "retrieve_project_evidence",
                "web_search",
                "amap_query",
                "amap_driving_distance",
            ),
            expected_write_tools=("write_agent_output", "write_agent_output_patch"),
            delivery_description="当天food、outfit、lodging、play四项结果",
            first_threshold=12,
            second_threshold=20,
        )

    return create_react_agent(
        _lifestyle_chat_model(), tools, prompt=_prompt_with_fewshot,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=system_prompt), *_FEWSHOT_TRAJECTORY),
            tools=tuple(tools),
            convergence_policy=convergence_policy,
        ),
        checkpointer=checkpointer,
    )


def build_lifestyle_agent_retry_fix(
    project_id: str,
    topic: str,
    *,
    checkpointer=None,
    convergence_work_unit: str | None = None,
):
    """跟build_lifestyle_agent同一个agent身份+同样的完整工具集，唯一区别：prompt不拼接
    _FEWSHOT_TRAJECTORY。专供citation_check_node打回重试用（orchestrator.py的
    citation_check_node，见docs/ISSUES.md相关条目）。

    背景：citation_check_node打回时只发一条孤立的fix_prompt（不带这次任务的原始
    历史，跟_invoke_with_write_verification那种带累积上下文的重试不是一回事），而
    build_lifestyle_agent每次调用都会重新拼上_FEWSHOT_TRAJECTORY——这段写死的"某某镇/
    某某广场"格式示例排在孤立fix_prompt前面，看起来像是"当前正在处理的任务"，模型会把
    这个假任务当真，判断fix_prompt里提到的真实地名（如"西宁""敦煌"）跟自己的任务
    （"某某镇"）无关，从而拒绝按核验反馈修正，维持few-shot格式的占位内容原样提交
    （真实复现：青甘大环线E2E复测里citation_check打回3轮后，Day1的food/outfit/
    lodging/play四个字段被写成了"素材库无针对'某某广场'的...建议"这种占位文本，
    模型在response里明确说"这条核验反馈并不适用于我当前负责的'某某镇1日'产出"）。

    这条重试路径本身触发的前提是citation_check已经拿到了真实的claimed_entities去核对，
    意味着素材库对这个目的地不是空白的——不会撞上_FEWSHOT_TRAJECTORY原本要解决的
    "素材库空白场景下模型忘记调用write_agent_output"这个问题（那个问题只在真·检索
    反复落空时才会发生，见build_lifestyle_agent_write_only），所以去掉few-shot对
    这条路径没有已知的负面影响。
    """
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    research_target_key = None
    if convergence_work_unit and convergence_work_unit.startswith("lifestyle_day_"):
        research_target_key = "lifestyle:day:" + convergence_work_unit.removeprefix(
            "lifestyle_day_"
        )
    tools = (
        make_material_tools(
            topic,
            project_id,
            actor="lifestyle_agent",
            log_scope="lifestyle_agent",
            allow_append_evidence=True,
            research_target_key=research_target_key,
        )
        + make_amap_tools(
            topic,
            project_id=project_id,
            log_scope="lifestyle_agent",
            research_context={
                "actor": "lifestyle_agent",
                "stage": "lifestyle",
                "target_key": research_target_key or "lifestyle:all",
            },
        )
        + make_web_search_tools(
            project_id,
            "lifestyle_agent",
            research_context={
                "actor": "lifestyle_agent",
                "stage": "lifestyle",
                "target_key": research_target_key or "lifestyle:all",
            },
        )
        + make_workspace_tools(project_id, "lifestyle_agent", topic=topic)
        + make_context_tools(project_id, "lifestyle_agent")
    )
    convergence_policy = None
    if convergence_work_unit:
        convergence_policy = ConvergenceNudgePolicy(
            work_unit=convergence_work_unit,
            research_tools=(
                "retrieve_materials",
                "retrieve_project_evidence",
                "web_search",
                "amap_query",
                "amap_driving_distance",
            ),
            expected_write_tools=("write_agent_output", "write_agent_output_patch"),
            delivery_description="当天引用问题条目的局部修正",
            first_threshold=8,
            second_threshold=12,
        )

    return create_react_agent(
        _lifestyle_chat_model(), tools, prompt=system_prompt,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=system_prompt),),
            tools=tuple(tools),
            convergence_policy=convergence_policy,
        ),
        checkpointer=checkpointer,
    )


_RETRY_WRITE_ONLY_SYSTEM_PROMPT = """你是Lifestyle Agent的引用返修提交阶段。
研究已经结束，用户消息会提供当天的问题清单和受影响条目的完整精简JSON。你只能做局部修正：
1. 原样保留未被问题清单指出的recommendation内容和claims。
2. direct/amap事实必须保留真实evidence_refs；证据不支持时删除或收窄具体事实，或改为estimate/unverified。
3. 不新增店名、数字、营业时间、开放状态或地点事实，不修改其他日期。
4. 只使用write_agent_output提交实际改动的完整条目；不要输出解释，也不要只给修改建议。
write_agent_output会按field/day局部覆盖，未提交条目自动保留。"""


def build_lifestyle_agent_retry_write_only(project_id: str, topic: str):
    """引用返修达到研究边界后的同角色受限写入器。"""
    system_prompt = _RETRY_WRITE_ONLY_SYSTEM_PROMPT
    tools = [
        workspace_tool
        for workspace_tool in make_workspace_tools(
            project_id, "lifestyle_agent", topic=topic
        )
        if workspace_tool.name == "write_agent_output"
    ]
    return create_react_agent(
        _lifestyle_chat_model(),
        tools,
        prompt=system_prompt,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=system_prompt),),
            tools=tuple(tools),
        ),
    )


_FORCE_WRITE_PROMPT_SUFFIX = (
    "\n\n【重要·写最小诚实产出】检索工具已经被禁用——当前工作单元的研究阶段已经结束，"
    "对话中已有的素材、finding和路线信息就是本次提交可用的证据。现在请**停止任何检索**，"
    "直接调用write_agent_output，对food/outfit/lodging/play四个字段的每一天各提交一条item。"
    "\n\n**产出要求**：有证据的事实使用已有证据；证据不足的细节要收窄，不能假装已核实。"
    "每个字段每个day写一条最小可交付产出：①没有具体素材覆盖时明确说「素材库无针对X的具体检索结果」；"
    "②用路线规划区route_plan.json里route_agent已经写好的**真实POI名/目的地名**写方向性建议"
    "（如'骑楼老街''白沙门公园''假日海滩''火山口公园'这些真实地点名，**不要用'某某镇'"
    "'某某广场''当地'这种占位名或笼统措辞**——你是在给真实目的地写攻略，用户会照着念、"
    "照着去找店，占位名会让用户完全摸不着头脑）；③给方向性、原则性的建议（住X附近/选连锁"
    "酒店/按天气穿搭/按POI顺序游览），不编造具体店名/价格/营业时段等可验证事实。"
    "\n\n**few-shot示例里的'某某镇/某某广场'只是格式示例**，真实产出必须替换成route_plan里"
    "的真实POI名——这是格式模板，不是要你复述占位名。"
)


def build_lifestyle_agent_write_only(project_id: str, topic: str):
    """跟build_lifestyle_agent同一个agent身份，但只绑write_agent_output/read_workspace，
    不提供retrieve_materials/amap_query/append_material——检索类工具在物理上不可用。

    背景（docs/ISSUES.md第80条）：素材库空白场景下，模型会反复检索无果，最终只写文字总结、
    忘记调用write_agent_output。prompt层规则（"检索N次无果就该停"）和few-shot轨迹示例
    都试过，few-shot方向上有改善但未达统计显著，都属于"讲道理"，依赖模型自己判断和遵守。
    这个函数走另一条路：由_invoke_with_write_verification检测到"连续N次retrieve_materials
    都返回空"这个客观条件后，改用这个受限agent做最后一轮重试——模型这一轮物理上没有检索类
    工具可选，只能在write_agent_output和空手而归之间选，从根上避免"继续换词检索"这个选项，
    不依赖模型自己"意识到该停手"。跟给模型讲道理相比，这是一个不依赖模型判断力的结构性收窄。
    """
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8") + _FORCE_WRITE_PROMPT_SUFFIX
    tools = make_workspace_tools(project_id, "lifestyle_agent", topic=topic)  # 只有read_workspace+write_agent_output

    def _prompt_with_fewshot(state):
        return [SystemMessage(content=system_prompt)] + _FEWSHOT_TRAJECTORY + state["messages"]

    return create_react_agent(
        _lifestyle_chat_model(),
        tools,
        prompt=_prompt_with_fewshot,
        pre_model_hook=build_context_editing_hook(
            static_messages=(SystemMessage(content=system_prompt), *_FEWSHOT_TRAJECTORY),
            tools=tuple(tools),
        ),
    )
