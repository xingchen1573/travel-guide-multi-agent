"""核心素材agent的独立采集图：提案 -> （可选）人工确认 -> 采集入库。

这是独立于 src/orchestrator.py 主图的第二个 StateGraph——素材agent的"单独@调用"模式
本来就是解耦的（见 docs/ARCHITECTURE.md 第3.1节：核心素材agent单独调用不影响任何project
的状态机/依赖级联），不走 AGENT_DEPENDENCY_TABLE 级联，不需要挂进主图。

三个节点：
- propose：构建带采集工具的material_agent，invoke一次（内部可能多轮调用搜索工具换关键词/
  翻页凑够候选数量），取回它调用propose_candidates时提交的数据。第一轮（state里还没有
  target_count时）额外抽取一次目标数量（见_extract_target_count）。
- await_confirm：只做只读+interrupt，不放副作用代码（resume时会重跑，和orchestrator.py
  的await_skeleton_confirm_node同款约束）。默认auto_confirm=True时跳过真正的interrupt，
  直接把候选全部当作approved——还没有真实交互界面展示候选给人工点选前，暂停等于卡住流程
  且人工根本看不到中断内容，显式传auto_confirm=False可恢复暂停等待人工输入。
- collect：纯函数，不调LLM——采集是机械步骤（下载/转写/入库），不需要agent推理，见
  docs/ARCHITECTURE.md"规则优先，agent兜底"原则的延伸。**2026-07-25新增补足循环**：
  用户说"给我5条"，理应是去重/质量门禁后真正入库5条，不是"尝试了5条、能采多少算多少"
  ——实际采集时会因为跟库里已有内容重复、is_relevant判定不相关、ASR失败等原因导致
  实际入库数少于目标数，此时不再是一次性收工，而是清空候选状态、更新request说明还差
  多少条，路由回propose重新搜索补足，直到达标/轮次到上限（MAX_COLLECTION_ROUNDS）/
  某一轮完全没有新增入库（说明该目的地这类素材大概率已经搜尽，再搜也是无意义的重复）
  三者任一满足才停止。

**2026-07-26新增（管理后台采集体验反馈驱动，见CLAUDE.md已知问题第2条）**：
- platform参数：propose阶段按平台硬过滤搜索工具（不是靠request文字软提示），供管理后台
  显式"平台：全部/仅B站/仅抖音"选择器使用。
- log回调：propose/collect两个节点在关键步骤（第几轮开始搜索、agent提交候选数按平台拆分、
  逐条B站下载转写/批量抖音抓取、质量门禁入库结果、是否触发补搜）调用log()，jobs.py把
  这些文字追加进任务的log:list[str]，供前端渲染成一个可展开的进度面板，不再只有一个
  "采集中..."的静态badge。默认no-op，不传时行为和之前完全一致。
"""
from __future__ import annotations

from typing import Callable, Optional, TypedDict
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from src.agents.material_agent.agent import build_material_agent
from src.config import get_structured_chat_model
from src.materials.collectors import bilibili as bilibili_collector
from src.materials.collectors import douyin as douyin_collector
from src.materials.ingest import ingest_topic
from src.structured_output import bind_structured_json
from src.utils import invoke_with_retry

# 补足循环的最大轮次（含第一轮）——不能无限循环下去，万一目的地这类内容存量有限，
# 永远补不满目标数，也要有个诚实收工的上限。跟项目里MAX_RETRY那套"设上限、到点就
# 诚实终止"的一贯做法一致（见src/orchestrator.py）。
MAX_COLLECTION_ROUNDS = 3


class _TargetCount(BaseModel):
    target_count: Optional[int] = Field(
        default=None, description="用户在这句需求描述里明确要求的具体素材/候选数量，没有提到具体数字则为null"
    )


def _extract_target_count(request: str) -> int:
    """从用户的采集需求描述里抽取目标数量，没有明确数字时默认5——用户没说清楚也应该有
    一个明确的补足目标，而不是"不设目标、不补足"（2026-07-25讨论确定，见CLAUDE.md/
    docs/ISSUES.md相关记录）。跟_extract_requirements_fields同款with_structured_output
    (method="json_mode")模式，这里不直接对这个函数写mock LLM的单元测试（准不准由真实
    E2E验证），调用方测试时直接monkeypatch这个函数本身。
    """
    model = get_structured_chat_model(temperature=0)
    structured = bind_structured_json(model, _TargetCount)
    prompt = (
        f"用户的素材采集需求：{request}\n"
        f"请判断用户有没有在这句话里明确要求具体的数量（比如'给我5条''找3个视频'），"
        f"抽取出这个数字；如果完全没有提到具体数量，则为null。"
        f"请以JSON格式回复，包含target_count字段。"
    )
    result: _TargetCount = structured.invoke(prompt)
    return result.target_count or 5


class MaterialGraphState(TypedDict, total=False):
    topic: str
    request: str  # 用户的原始需求，如"给我5条成都最新攻略"
    platform: str  # "all"/"bilibili"/"douyin"，限定这次只搜哪个平台（见管理后台显式平台选择器）
    auto_confirm: bool  # 默认True：跳过真正的interrupt暂停，候选自动全部确认采集
    target_count: Optional[int]  # 目标入库数量，第一轮从request抽取一次，后续轮次沿用同一个值
    collection_round: int  # 当前第几轮，从0开始
    total_ingested: int  # 跨轮次累计的实际入库数（不是每轮单独的数字）
    proposed_candidates: Optional[list[dict]]
    proposal_summary: str
    approved_candidates: Optional[list[dict]]
    collection_done: bool
    result_summary: str


_NOOP_LOG: Callable[[str], None] = lambda _msg: None


def material_propose_node(state: MaterialGraphState, log: Callable[[str], None] = _NOOP_LOG) -> dict:
    """构建带采集工具的material_agent，invoke一次(内部可能多轮调用搜索工具)，
    取回它调用propose_candidates时提交的数据。

    发给agent的消息必须显式包含topic（2026-07-25真实E2E复现的bug修复）：之前只传了
    state["request"]原文，topic只在build_material_agent内部用于工具过滤，从没让模型
    真正"看到"过。真实场景下用户的request经常不包含目的地名称（比如陪伴对话里说"帮我
    搜3条最新的探店视频"，用户不会每次都重复说城市名，因为对话本身就在这个project的
    语境里）——模型只能凭"探店"这种泛词自己乱猜关键词，搜出的候选是完全不相关城市的
    内容，会被下游is_relevant门禁正确拒绝，表现成"入库0条"，看起来像"该目的地没有
    更多可采集内容"，实际是模型压根不知道要搜哪个城市。

    log参数（2026-07-26新增，见CLAUDE.md已知问题第2条）：管理后台jobs.py的log回调
    默认是no-op（跟原有直接调用的测试/CLI用法完全兼容），后台API触发时会传入真实回调，
    让前端轮询时能看到"当前第几轮/agent提交了多少候选"这类中间过程，不再只有一个
    "采集中..."的静态badge。
    """
    topic = state["topic"]
    platform = state.get("platform", "all")
    current_round = state.get("collection_round", 0)
    log(f"第{current_round + 1}轮：向核心素材agent发起搜索请求（目的地={topic}，平台={platform}）...")

    captured: dict = {}
    seen: set = set()
    agent = build_material_agent(topic, project_id="_global", captured=captured, seen=seen, platform=platform)
    user_message = f"目的地：{topic}\n用户需求：{state['request']}"
    if platform != "all":
        # 工具层面已经硬过滤（未绑定的平台搜索工具模型根本调不到），这里额外提示一句，
        # 避免模型看到prompt.md里静态列出的"可用工具：...search_douyin..."文字而困惑
        # 为什么调用不到——静态prompt文本不会随platform动态增减。
        user_message += f"\n（本次只能采集{platform}平台，其它平台的搜索工具本次不可用）"
    invoke_with_retry(agent, [{"role": "user", "content": user_message}])
    if "candidates" not in captured:
        # 兜底重试，复用orchestrator.py的_invoke_with_write_verification同款思路：
        # agent可能只输出文字总结没真调用propose_candidates
        invoke_with_retry(agent, [{"role": "user", "content":
            "你还没有调用propose_candidates工具提交候选，请现在调用它提交你搜到的候选列表。"}])
    candidates = captured.get("candidates", [])
    bili_count = sum(1 for c in candidates if c.get("platform") == "bilibili")
    douyin_count = sum(1 for c in candidates if c.get("platform") == "douyin")
    log(f"agent提交候选{len(candidates)}条（B站{bili_count}条/抖音{douyin_count}条）："
        f"{captured.get('summary', '')}")
    result: dict = {
        "proposed_candidates": candidates,
        "proposal_summary": captured.get("summary", ""),
    }
    if state.get("target_count") is None:
        # 只在第一轮（还没抽取过目标数量）时抽取一次——补搜轮次要沿用同一个目标，
        # 不能每轮都重新抽取（可能抽出不同的数字，导致"补足到多少"这个目标本身漂移）
        result["target_count"] = _extract_target_count(state["request"])
    return result


def await_material_confirm_node(state: MaterialGraphState) -> dict:
    """只做只读+interrupt，不放副作用代码（resume时会重跑），和await_skeleton_confirm_node同款约束。

    auto_confirm默认True：还没有真实交互界面展示候选给人工点选，此时暂停等于卡住流程、
    人工也看不到中断（用户原话"这也看不到你让我确认"）。默认跳过真正的interrupt，把提案
    候选直接全部当作approved自动放行；后续接入真实交互界面时显式传auto_confirm=False即可
    恢复暂停等待人工输入的行为，不需要改这个函数的核心逻辑。
    """
    candidates = state.get("proposed_candidates", [])
    if state.get("auto_confirm", True):
        return {"approved_candidates": candidates}
    decision = interrupt({
        "question": "以下是候选，回复 confirm 全部采集 / select:0,2 只选部分 / more:<补充要求> 重新搜索",
        "candidates": candidates,
        "summary": state.get("proposal_summary", ""),
    })
    if isinstance(decision, str) and decision.startswith("more:"):
        return {"request": decision[len("more:"):], "proposed_candidates": None}
    if isinstance(decision, str) and decision.startswith("select:"):
        idx = {int(i) for i in decision[len("select:"):].split(",") if i.strip().isdigit()}
        approved = [c for i, c in enumerate(candidates) if i in idx]
    else:
        approved = candidates
    return {"approved_candidates": approved}


def material_collect_node(state: MaterialGraphState, log: Callable[[str], None] = _NOOP_LOG) -> dict:
    """纯函数，不调LLM——采集是机械步骤，不需要agent推理，省成本（"规则优先"原则的延伸）。

    B站候选逐条调用collect_one（B站走bilibili_api，不涉及MediaCrawler浏览器自动化，
    没有"重复开浏览器"问题）；抖音候选批量调用collect_many（一次性抓元数据只开一次浏览器）
    ——真实E2E验证时发现逐条调用collect_one会为每条候选独立开一次浏览器窗口（2条候选开2次），
    见docs/ISSUES.md。

    补足循环：如果这次采集设置了target_count（正常情况下propose阶段一定会设置，没设置时
    视为历史/兼容状态，直接维持"一次性、能采多少算多少"的行为），跑完这一轮的采集+入库后，
    判断跨轮次累计的total_ingested是否达标——没达标、轮次还没到MAX_COLLECTION_ROUNDS上限、
    且这一轮确实有新增入库（说明还有可搜的空间，不是已经搜尽），就清空候选状态、更新request
    说明还差多少条，轮次+1，返回的state会被_route_material路由回propose重新搜索；否则
    （达标/到轮次上限/这轮完全没有新增）收工，如实报告最终结果。

    log参数（2026-07-26新增）同material_propose_node，默认no-op，向后兼容。

    **2026-07-29新增环境依赖前置校验**（真实E2E跑洱海骑行场景撞见的bug，见docs/ISSUES.md）：
    这个独立采集图此前从未调用`check_collector_dependencies()`（第77条修复只接进了
    src/orchestrator.py的_attempt_auto_material_supplement那条路径）——本机ffmpeg/ffprobe
    不在PATH时，bilibili_collector.collect_one内部的subprocess.run(check=True)会直接抛
    未捕获异常，整个graph.invoke()崩溃，而不是像orchestrator.py那条路径一样优雅拦截、
    显式留痕。有候选要真正采集时先做一次前置校验，缺失就诚实报告收工，不再让环境问题
    伪装成一次神秘的进程崩溃。
    """
    topic = state["topic"]
    approved = state.get("approved_candidates") or []
    bili_candidates = [c for c in approved if c["platform"] == "bilibili"]
    douyin_urls = [c["source_url"] for c in approved if c["platform"] != "bilibili"]

    if approved:
        from src.materials.collectors.bilibili import CollectorDependencyError, check_collector_dependencies

        try:
            check_collector_dependencies()
        except CollectorDependencyError as e:
            log(f"环境依赖缺失，本轮无法真正采集：{e}")
            return {
                "collection_done": True,
                "total_ingested": state.get("total_ingested", 0),
                "result_summary": f"采集环境依赖缺失，未能真正下载/转写任何候选：{e}",
            }

    bili_success = 0
    for i, c in enumerate(bili_candidates):
        log(f"下载转写B站候选（{i + 1}/{len(bili_candidates)}）：{c.get('title', c['source_url'])}")
        entry = bilibili_collector.collect_one(topic, c)
        if entry is not None:
            bili_success += 1
    douyin_success = 0
    if douyin_urls:
        log(f"批量抓取抖音候选（共{len(douyin_urls)}条）...")
        douyin_entries = douyin_collector.collect_many(topic, douyin_urls)
        douyin_success = sum(1 for e in douyin_entries if e is not None)

    log("正在跑is_relevant质量门禁+入库...")
    # 全新topic（raw/<topic>/manifest.json从未建立过）且这一轮又恰好0候选（agent没搜到/
    # is_relevant门禁全拒）时，ingest_topic按设计会raise FileNotFoundError——这不是bug，
    # 是它本来的契约（material_agent_node等其他调用方依赖这个异常语义判断"素材库完全空白"）。
    # 这里catch掉当作"本轮没有新增"处理，跟src/orchestrator.py的_attempt_auto_material_supplement
    # 对同一个调用已有的try/except Exception: pass是同一个既有模式，不是新发明的处理方式。
    try:
        new_entries = ingest_topic(topic)
    except FileNotFoundError:
        new_entries = []
    total_ingested = state.get("total_ingested", 0) + len(new_entries)
    target_count = state.get("target_count")
    current_round = state.get("collection_round", 0)
    log(f"本轮入库{len(new_entries)}条，累计{total_ingested}/{target_count if target_count is not None else '?'}条")

    should_retry = (
        target_count is not None
        and total_ingested < target_count
        and current_round < MAX_COLLECTION_ROUNDS - 1
        and len(new_entries) > 0
    )

    if should_retry:
        remaining = target_count - total_ingested
        log(f"未达标，还差{remaining}条，进入第{current_round + 2}轮补充搜索...")
        return {
            "total_ingested": total_ingested,
            "collection_round": current_round + 1,
            "proposed_candidates": None,
            "approved_candidates": None,
            "request": f"{state.get('request', '')}（已采集入库{total_ingested}条，"
                       f"还需要再补{remaining}条不重复的候选，换一批关键词或翻页重新搜索）",
        }

    shortfall_note = ""
    if target_count is not None and total_ingested < target_count:
        shortfall_note = (
            f"，未达到目标{target_count}条（已尝试{current_round + 1}轮采集，"
            f"可能该目的地这类内容存量有限）"
        )
    return {
        "collection_done": True,
        "total_ingested": total_ingested,
        "result_summary": f"采集{len(approved)}条候选（B站{len(bili_candidates)}条候选成功写入{bili_success}条/"
                           f"抖音{len(douyin_urls)}条候选成功写入{douyin_success}条），"
                           f"本轮质量门禁后入库{len(new_entries)}条，"
                           f"累计入库{total_ingested}条"
                           f"（其余被is_relevant门禁拒绝，见rejected.jsonl，或ASR失败）{shortfall_note}",
    }


def _route_material(state: MaterialGraphState) -> str:
    if state.get("collection_done"):
        return END
    if state.get("approved_candidates") is not None:
        return "collect"
    if state.get("proposed_candidates") is not None:
        return "await_confirm"
    return "propose"


def build_material_collection_graph(log: Callable[[str], None] = _NOOP_LOG):
    """log（2026-07-26新增）：绑定进propose/collect两个节点，让调用方（jobs.py）能拿到
    比"提交后干等一个终态"更细颗粒度的中间过程文字。每次run_material_collection都会
    重新build一次图（不是全局单例），用functools.partial把log闭包进节点函数是安全的，
    不涉及跨调用共享状态。
    """
    import functools

    graph = StateGraph(MaterialGraphState)
    graph.add_node("propose", functools.partial(material_propose_node, log=log))
    graph.add_node("await_confirm", await_material_confirm_node)
    graph.add_node("collect", functools.partial(material_collect_node, log=log))
    graph.add_conditional_edges(START, _route_material)
    for n in ["propose", "await_confirm", "collect"]:
        graph.add_conditional_edges(n, _route_material)
    return graph.compile(checkpointer=InMemorySaver())


def run_material_collection(
    topic: str,
    request: str,
    thread_id: str | None = None,
    auto_confirm: bool = True,
    platform: str = "all",
    log: Callable[[str], None] = _NOOP_LOG,
) -> dict:
    """独立入口，不挂进orchestrator.py的主图。

    auto_confirm默认True：没有真实交互界面前，跳过人工确认暂停，候选自动全部采集。
    显式传False可以恢复原有的interrupt暂停行为——返回结果里如果有"__interrupt__"键，
    调用方需要展示candidates给人工，再用 graph.invoke(Command(resume=<决定字符串>), config)
    继续（config的thread_id要保持一致）。

    platform（2026-07-26新增）："all"/"bilibili"/"douyin"，限定这次只搜哪个平台
    （见管理后台采集对话框的显式平台选择器，src/tools/material_collection_tools.py
    做的是工具级硬过滤，不是靠prompt软提示）。
    log（2026-07-26新增）：细粒度进度回调，见build_material_collection_graph。
    """
    graph = build_material_collection_graph(log=log)
    config = {"configurable": {"thread_id": thread_id or f"material:{topic}:{uuid4()}"}}
    return graph.invoke(
        {"topic": topic, "request": request, "auto_confirm": auto_confirm, "platform": platform}, config
    )

