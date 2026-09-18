"""主agent的编排层：按 docs/ORCHESTRATION.md 实现的 LangGraph StateGraph。

核心设计（详见 docs/ORCHESTRATION.md）：
- Supervisor模式：所有节点做完都回到 main_agent_router 决定下一跳，节点之间不直连。
- 依赖表驱动路由（AGENT_DEPENDENCY_TABLE）+ 状态机phase映射（PHASE_TO_NODE）。
- 骨架确认用 interrupt()，checkpointer 用per-project持久化SqliteSaver（见
  src/agent_checkpoint.py，2026-08-13起支持run_plan()断点续跑，docs/ISSUES.md第120条）。

MVP简化（相对文档里的完整设计）：
- "路线规划中"这个phase在plan模式的happy path里被跳过——路线草稿在"骨架确认中"阶段
  由 route_draft 节点生成并确认，确认后直接进入"并行调研中"，不再单独重跑一次route_agent。
  这是为了避免LangGraph "resume时节点从头重跑"的机制导致route_agent被重复调用两次。
- edit模式（见docs/ISSUES.md第28/29/46条）：`edit_request`断链已修复（真正拼进route_agent/
  lifestyle_agent/integration_agent的prompt，仅在用户直接@的入口agent生效，级联下游沿用
  plan模式全量重新生成）。写入后加了误改检测：route_agent一次改动了2天以上、lifestyle_agent
  一次改动了2个以上字段、或integration_agent正文长度骤降，都会自动report_conflict打回复核，
  不信任模型自觉遵守"只改被要求的部分"。route_agent的edit粒度是"天"（跟lifestyle/pitfall的
  "字段"粒度对称），完成后会先过一次route_citation_check_node再级联下游。
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import operator
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, TypedDict

import openai
from langgraph.errors import GraphBubbleUp, GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel, Field

from src.agent_checkpoint import (
    agent_thread_id,
    clear_agent_thread,
    clear_agent_thread_family,
    get_agent_checkpointer,
    get_graph_checkpointer,
    poi_detail_agent_thread_id,
)
from src.context_editing import (
    ContextBudgetExceededError,
    _summarize_tool_result,
    _tool_call_names_by_id,
)
from src import companion_history

from src.agents.culture_agent.agent import build_culture_agent
from src.agents.guide_agent.agent import build_guide_agent
from src.agents.integration_agent.agent import build_integration_agent
from src.agents.lifestyle_agent.agent import (
    build_lifestyle_agent,
    build_lifestyle_agent_retry_fix,
    build_lifestyle_agent_retry_write_only,
    build_lifestyle_agent_write_only,
)
from src.agents.pitfall_agent.agent import build_pitfall_agent, build_pitfall_agent_write_only
from src.agents.poi_detail_agent.agent import build_poi_detail_agent
from src.agents.realtime_agent.agent import build_realtime_agent
from src.agents.route_agent.agent import build_route_agent
from src.config import PROJECTS_DIR, get_light_chat_model
from src.llm_rotation import build_companion_chat_model, build_companion_router_model
from src.materials import citation_check, route_feasibility_check, route_map_check
from src.materials import supplement as material_supplement
from src.materials import research_service
from src.materials.baseline import validate_material_baseline
from src.materials.citation_check import _entity_matches
from src.materials.gap_check import check_material_gap
from src.materials.ingest import ingest_topic, material_processing_status
from src.materials.project_evidence import list_project_evidence_for_log_scope
from src.tools.amap_tools import make_amap_tools, reset_amap_evidence_log
from src.tools.material_tools import make_material_tools
from src.tools.web_search_tools import make_web_search_tools, reset_web_search_log
from src.tools.workspace_tools import make_workspace_tools
from src.utils import invoke_with_retry
from src.llm_trace import append_context_event, set_trace_context
from src.workspace import delivery
from src.workspace.schema import (
    LIFESTYLE_FIELDS,
    PitfallOutput,
    PoiDetailManifest,
    PoiDetailManifestEntry,
    ProjectPhase,
)
from travel_guide_kernel.models import RoutePlan
from travel_guide_kernel.projects import prepare_fixed_route_project
from travel_guide_kernel.route import (
    route_structure_diff,
    route_structure_hash,
    route_structure_snapshot,
    validate_route_structure,
)
from travel_guide_kernel.workspace import store
from src.workspace.final_guide_validation import validate_final_guide_contract
from src.workspace.final_guide_blocks import collect_upstream_claims
from src.workspace.integration_context import integration_delivery_view
from src.workspace.agent_context import (
    lifestyle_day_complete,
    lifestyle_for_pitfall,
    route_day_view,
    route_map_day_view,
    user_requirement_view,
)

MAX_RETRY = 2

# 素材缺口检测与限量自动补充采集（docs/ARCHITECTURE.md第16节）：三条路径
# （poi_detail_agent_node/route_citation_check_node/citation_check_node）在各自
# MAX_RETRY耗尽即将force_finalize前共用的预算常量。
# MAX_AUTO_MATERIAL_GAP_SUPPLEMENT：全项目累计自动采集上限，超过后统一回退到"只呈现
# 候选、人工确认"的既有路径，不会无限制自动扩张素材库（决策3）。
MAX_AUTO_MATERIAL_GAP_SUPPLEMENT = material_supplement.MAX_AUTO_MATERIAL_GAP_SUPPLEMENT
# _MAX_AUTO_COLLECT_PER_TRIGGER：单次触发（一次force_finalize事件，可能涉及多个缺失
# 实体/字段）最多自动采集几条——这是任务讨论里明确留白、由实现时自行决定的一个细粒度上限
# （见docs/ARCHITECTURE.md第16节"未决/待实现时确定"）。选择2的理由：项目级预算只有10条，
# 如果不设单次上限，一次force_finalize事件（比如lifestyle一次核验涉及3个缺失实体）就可能
# 单独消耗掉大半预算，导致后续其他POI/字段几乎分不到预算；2是一个折中——既能让单次事件
# 有实质性的补充效果（不是采1条聊胜于无），又能让10条预算覆盖到大约5次不同的触发事件，
# 分布更均匀。
_MAX_AUTO_COLLECT_PER_TRIGGER = material_supplement.MAX_AUTO_COLLECT_PER_TRIGGER

# _attempt_auto_material_supplement里search_and_rank的候选池大小（不是最终采集数量，
# 那个由limit/_MAX_AUTO_COLLECT_PER_TRIGGER控制）。真实复现过（docs/ISSUES.md第75条）
# 真正相关的候选可能排到第20名开外（排序权重不含语义相关性，被字面命中关键词的高热度
# 无关视频挤占靠前位置），候选池太小会导致_filter_relevant_candidates根本看不到它们。
_GAP_SUPPLEMENT_CANDIDATE_POOL = material_supplement.GAP_SUPPLEMENT_CANDIDATE_POOL


def _is_formal_comparison(project_id: str) -> bool:
    manifest = store.read_workspace(project_id, "experiment.json") or {}
    return str(manifest.get("comparison_grade") or "").upper() == "A"


def _assert_formal_comparison_baseline(project_id: str, topic: str) -> None:
    if not _is_formal_comparison(project_id):
        return
    saved = store.read_requirements(project_id) or {}
    destination = str((saved.get("requirements") or {}).get("destination") or "")
    check = validate_material_baseline(
        store.read_workspace(project_id, "material_baseline.json"),
        topic=topic,
        destination=destination,
    )
    if check["status"] != "passed":
        raise RuntimeError(
            "正式对照素材基线已变化: "
            + json.dumps(check["issues"], ensure_ascii=False)
        )


def _compact_research_results(results: list[dict], *, max_chars: int = 600) -> list[dict]:
    return [
        {
            "scope": item.get("scope"),
            "title": item.get("title"),
            "chunk_id": item.get("chunk_id"),
            "content_snippet": str(item.get("content_snippet") or "")[:max_chars],
            "evidence_ref": item.get("evidence_ref"),
        }
        for item in results
    ]


def _pitfall_research_context_view(rows: list[dict], *, max_chars: int = 80) -> dict:
    """Keep per-item evidence bindings without repeating the same chunk body."""
    coverage = []
    catalog: dict[str, dict] = {}
    catalog_order: list[str] = []
    for row in rows:
        chunk_ids = []
        for item in row.get("results") or []:
            chunk_id = str(item.get("chunk_id") or "")
            if not chunk_id:
                continue
            chunk_ids.append(chunk_id)
            if chunk_id in catalog:
                continue
            compact = dict(item)
            compact["content_snippet"] = str(
                compact.get("content_snippet") or ""
            )[:max_chars]
            catalog[chunk_id] = compact
            catalog_order.append(chunk_id)
        coverage.append(
            {
                "field": row.get("field"),
                "day": row.get("day"),
                "chunk_ids": chunk_ids,
            }
        )
    return {
        "coverage": coverage,
        "evidence_catalog": [catalog[chunk_id] for chunk_id in catalog_order],
    }


class MaterialSupplementResult(list):
    """List-compatible collection containing only newly searchable items."""

    def __init__(
        self,
        indexed: list[dict] | None = None,
        *,
        rejected: list[dict] | None = None,
        pending: list[dict] | None = None,
    ):
        super().__init__(indexed or [])
        self.indexed = self
        self.rejected = rejected or []
        self.pending = pending or []


def _missing_entities_retry_hint(issues: list[dict]) -> str:
    """打回修正prompt的共享辅助函数（docs/ISSUES.md第66条真实案例修复）。

    背景：真实E2E里发现一个反复验证过"不是模型编造、是检索没捞出真实存在的素材"的案例——
    "葱油酥/椒盐酥"这两个具体点心名词真实存在于素材库某条chunk里，但lifestyle_agent写
    day3.food这条建议时用的是覆盖全天多个话题的笼统query一次性检索，这条chunk被挤出
    默认top_k=5之外，模型转而引用了另一条只说"糕点，有两家"的含糊chunk。核验反馈只是
    笼统地说"重新检索"，两轮打回修正模型都没有针对这两个孤立专有名词单独精确检索，
    只能在"保留编造细节"和"整体改笼统"之间二选一，两次都选了前者，编造内容最终被迫
    force_finalize后原样进入产出。

    真实排查（不是猜测）：直接调用retrieve_materials对比不同措辞的排名——用具体专有名词
    单独检索（如"葱油酥 椒盐酥"）时正确chunk稳定排第1位，用覆盖全天话题的笼统query
    检索时完全查不到，验证了"给出具体缺失词、要求单独精确检索"这个定向提示能真正命中
    检索侧已经具备的能力，不是治标不治本的软约束。

    这个函数把issues里已经算出来的missing_entities拼成一句具体建议，供四处citation_check
    打回逻辑（route/lifestyle/pitfall/poi_detail）共用，取代此前"请重新检索"这句笼统措辞。
    """
    all_entities: list[str] = []
    for issue in issues:
        for e in issue.get("missing_entities", []):
            if e and e not in all_entities:
                all_entities.append(e)
    if not all_entities:
        return ""
    quoted = "、".join(f"「{e}」" for e in all_entities)
    return (
        f"这些缺失的具体名词（{quoted}）可能真实存在于素材库里，只是笼统的检索query没有把"
        f"对应chunk排进结果——请针对每一个缺失名词单独发起一次精确检索（比如只用这个名词"
        f"本身作为query，不要和其他话题混在一起查），确认素材库里到底有没有支持这个说法的"
        f"原文，而不是直接假设检索不到。"
    )


def _findings_retry_hint(project_id: str, agent: str) -> str:
    """打回修正prompt的共享辅助函数（docs/ISSUES.md第131条真实案例修复）。

    背景：真实E2E里发现一个比第66条更具体的失效模式——lifestyle_agent用`record_finding`
    工具（`src/tools/context_tools.py`）正确记录过"火星营地=青甘大环线-095-c000"这类
    专名到chunk_id的对照表，但最终调用write_agent_output提交时，用的却是完全不同、
    不支持这个专名的chunk_id。真实核实过11处这类"模型自己记过正确答案"的案例，10处
    最终sources跟findings记录**完全零重叠**——不是记错了几个，是这次对话（493条消息、
    反复检索几十次）拖得太长，写入那一刻没有回头核对自己早前记下的笔记。

    这不是"检索没捞出来"（第66条那类，属于_missing_entities_retry_hint的场景），
    是"检索捞出来了、记下来了，但写入时没用上"——两种问题的对症提示不同：前者要求
    "单独精确检索"，这里要求"先看自己记过的笔记，不要重新检索"。

    找不到findings.jsonl（这个agent这次run没调用过record_finding，或当前agent没有
    绑定该工具）时返回空字符串——调用方按"没有这层提示"处理，行为退化成打回前的
    样子。
    """
    path = PROJECTS_DIR / project_id / "agents" / agent / "findings.jsonl"
    if not path.exists():
        return ""
    facts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            facts.append(json.loads(line)["fact"])
        except (json.JSONDecodeError, KeyError):
            continue
    if not facts:
        return ""
    joined = "\n".join(f"- {f}" for f in facts)
    return (
        f"\n\n【重要·先查自己记过的笔记】你在这次生成过程中用record_finding记录过以下关键"
        f"结论，里面可能已经包含上面缺失名词对应的正确chunk_id——写入前请先核对这份记录，"
        f"不要跳过它直接重新检索或凭当前上下文里更近的检索结果随便配一个chunk_id：\n{joined}"
    )

# 依赖表：docs/ORCHESTRATION.md 第3节。避雷agent（2026-07-23实现）跟着衣食住行agent
# 自动级联；文化科普/实时agent仍是v2未接入的部分（实时agent陪伴阶段查询/审批链路已单独
# 实现，但不走这张plan模式依赖表，见run_companion）。
AGENT_DEPENDENCY_TABLE = {
    "route_agent": {"downstream": ["lifestyle_agent"], "feeds_integration": True},
    "lifestyle_agent": {"downstream": ["pitfall_agent"], "feeds_integration": True},
    "pitfall_agent": {"downstream": [], "feeds_integration": True},
    "integration_agent": {"downstream": [], "feeds_integration": False},
    "material_agent": {"downstream": [], "feeds_integration": False},
    # 文化科普agent：游离于主流程，仅edit入口@触发，一跳结束，不级联citation_check/整合
    # （见docs/ORCHESTRATION.md场景9）。补全在依赖表里是为了跟其他agent保持一致的兜底行为，
    # 实际路由走_route_edit_mode专门的"last_agent == culture_agent"分支直接END，
    # 不会真正查这张表。
    "culture_agent": {"downstream": [], "feeds_integration": False},
}


class GraphState(TypedDict, total=False):
    project_id: str
    topic: str
    mode: Literal["plan", "edit"]
    # 本轮用户交互（一次run_plan/run_edit/run_companion/run_channel_start调用，含内部
    # 自动confirm循环/resume_companion续interrupt这类"同一次交互的延续"）的标识，供
    # _track_run_status装饰器区分"这个node在本轮内是第几次被调用"（docs/ISSUES.md第142条）。
    # 只在"发起新一轮用户交互"的入口函数里生成新值；resume类调用（resume_companion/
    # run_channel_resume/run_plan内部confirm循环）复用同一个turn_id，不生成新的。
    turn_id: Optional[str]
    requirements: dict
    # 多轮需求收集（飞书接入用，见requirements_gather_node）：累积中的需求草稿，
    # plain字段（无reducer，节点每次整体返回覆盖）。字段齐全（含内部"_optional_asked"
    # 标记）后节点才会把完整字段写进上面的requirements，路由函数据此判断是否继续追问。
    # CLI/API等已经准备好完整requirements字典的调用方完全不会用到这个字段（见
    # _route_plan_mode里"requirements已完整则跳过多轮收集"的判断）。
    requirements_draft: dict
    entry_target: Optional[str]
    edit_request: Optional[str]
    last_agent_result: Optional[dict]
    material_checked: bool
    route_plan_drafted: bool
    revision_request: Optional[str]
    route_agent_done: bool
    route_citation_checked: bool
    route_map_checked: bool
    route_timeliness_checked: bool  # 时效性核验（route_timeliness_check_node），见该节点注释
    route_hard_constraints_blocked: bool
    pending_route_structure_confirmation: bool
    route_structure_resume_check: Optional[str]
    route_structure_revision_request: Optional[str]
    route_structure_revision_context: Optional[dict]
    # edit流程应用结构proposal后，引用/时效核验必须检查proposal实际改动的天，
    # 不能继续沿用原始edit范围。plan流程仍保持全路线核验。
    route_recheck_days: Optional[list[int]]
    route_citation_after_timeliness: bool
    auto_confirmed_structure_change: bool
    lifestyle_agent_done: bool
    edit_citation_checked: bool
    pitfall_agent_done: bool
    pitfall_citation_checked: bool
    # 文化科普agent：游离主流程的edit入口，一跳结束，不需要citation_checked这类核验字段
    # （不做引用级核验，见docs/ARCHITECTURE.md第3.2节）。
    culture_agent_done: bool
    integration_done: bool
    force_finalize: bool
    awaiting_integration_recheck: bool
    integration_retry_count: int
    edit_original_lifestyle: Optional[dict]
    edit_original_guide_len: Optional[int]
    edit_original_pitfall: Optional[dict]
    edit_original_route_plan: Optional[dict]
    # edit流程确认应用结构proposal时保存应用前的完整路线，供Integration成功后失效旧POI树木。
    poi_reconciliation_original_route_plan: Optional[dict]
    # edit模式下这次编辑实际改动了哪些字段（_changed_lifestyle_fields/_changed_pitfall_fields
    # 的计算结果）——citation_check_node/pitfall_citation_check_node据此把核验范围收窄到
    # 这次改动涉及的字段，未改动字段不重新核验，避免非确定性判断误伤（见docs/ISSUES.md第37补条）。
    edit_changed_lifestyle_fields: Optional[list[str]]
    edit_changed_pitfall_fields: Optional[list[str]]
    # 比上面两个更细一档（_changed_lifestyle_items/_changed_pitfall_items的计算结果）：
    # [[field, day], ...]——字段级scoping解决了跨字段误伤，但同字段内其它天完全没被
    # 触碰的item仍会被无谓地送进核验循环，有实测概率被非确定性判断误伤（见docs/ISSUES.md
    # 第37补条的补充验证）。citation_check_node/pitfall_citation_check_node据此把核验范围
    # 进一步收窄到真正改动的具体天，未设置时（级联整体重新生成场景）不做item级过滤，
    # 只受上面fields级scoping控制，行为不变。
    edit_changed_lifestyle_items: Optional[list[list]]
    edit_changed_pitfall_items: Optional[list[list]]
    # 同上，route_draft_node定点编辑的粒度是"天"不是"字段"，_changed_route_days的计算结果，
    # 供route_citation_check_node收窄核验范围。
    edit_changed_route_days: Optional[list[int]]
    # "树木生成中"阶段（docs/ORCHESTRATION.md第3.3节）：poi_detail_targets是从route_plan.json
    # 读出的needs_detail=true POI列表，驱动Send fan-out，是普通字段（每次由路由函数整体重新
    # 计算，不需要累加）。poi_detail_results是各并行分支各自的返回值，必须用operator.add
    # 声明reducer——Send/map-reduce模式下多个并行分支的返回值要累加成列表而不是互相覆盖，
    # 这是LangGraph官方文档里的标准写法（也是本次实现前就预判到的坑，见ORCHESTRATION.md）。
    poi_detail_targets: list[dict]
    poi_detail_results: Annotated[list[dict], operator.add]
    # 下面三个字段在两种场景下存在：①poi_detail_agent_node的单次并行分支执行期间，由Send
    # 负载直接携带（不是路由函数写进主state的），见_route_poi_detail_phase；②poi_detail_edit_node
    # 的edit入口场景，由run_edit()的day/poi_name参数直接传入initial_state（同样表示"这次
    # 要处理的POI是哪个"，语义一致，只是驱动方式不同——一个是Send fan-out，一个是edit_target直连）。
    day: int
    poi_name: str
    needs_detail_reason: str
    # poi_detail_edit_node的edit入口场景（docs/ARCHITECTURE.md第3.4节第9点）：这次编辑开始前
    # 的原始快照，供同一edit线程内的重试沿用同一条基线（原理跟edit_original_lifestyle等完全
    # 一致）。首次生成场景（该POI此前没有详情文件）不设置，值为None。
    edit_original_poi_detail: Optional[dict]
    # 陪伴阶段（docs/ORCHESTRATION.md第3.4节）：companion_question是run_companion()
    # 传入的用户自由提问，guide_agent_node/realtime_agent_node/companion_lifestyle_query_node/
    # companion_pitfall_query_node都从这个字段读要回答的问题。companion_guide_answer/
    # guide_covered是guide_agent_node的产出；companion_fallback_answer是自动串联时兜底agent
    # （realtime/companion_lifestyle_query/companion_pitfall_query）的产出；companion_answer
    # 是最终要返回给用户的（可能是guide单独覆盖，也可能是guide+fallback拼接后）的文本——
    # 由"可能成为链路终点"的节点各自负责组装写入，不额外加一个汇总节点（见实现计划）。
    companion_question: Optional[str]
    # 最近两轮已完成的用户可见问答。它由run_companion在进入图前从project级运行元数据
    # 读取，只用于解析“那里/第二个/刚才那个”等跨轮指代，不属于Workspace事实，也不依赖
    # LangGraph checkpoint残留。每次新turn整体覆盖，避免再次引入路由scratch状态污染。
    companion_recent_context: Optional[str]
    companion_guide_answer: Optional[str]
    guide_covered: Optional[bool]
    companion_fallback_answer: Optional[str]
    companion_answer: Optional[str]
    # 实时agent写入审批（docs/ORCHESTRATION.md第3.2节）：realtime_pending_patch是
    # realtime_agent_node判断需要修改时产出的待审批patch；realtime_approval_done标注
    # await_realtime_approval_node是否已经处理过一次（避免_route_after_realtime_agent
    # 无限绕回审批节点）；realtime_patch_applied标注patch是否真的被批准并写入
    # （只有这个为True才级联整合agent重新合并，被拒绝/纯问答场景都不设置）。
    realtime_pending_patch: Optional[dict]
    realtime_approval_done: bool
    realtime_patch_applied: bool


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------


def _said_would_call_tool_but_didnt(messages: list) -> bool:
    """判断最后一条AI消息是否是"嘴上说要调用工具，但这条回复本身没有真正发起调用"这种模式——
    即tool_calls为空，但文字内容里出现了"现在调用/立即调用/现在写入/马上写入"这类意图表述。

    2026-07-29真实复现（峨眉山评测lifestyle_agent，见docs/ISSUES.md第80条）：连续多轮里
    AI消息反复说"好的，我立即调用write_agent_output写入完整产出"，但这条消息的tool_calls
    始终是空的——不是"完全没理解任务"，是"意图已经生成、但没有在同一条回复里落地成真正的
    function call"，跟retry_note要解决的原始问题（模型只写文字总结、完全没打算调用）不是
    同一严重程度，值得用更直接的措辞单独处理，而不是重复发送同一条通用retry_note。
    """
    if not messages:
        return False
    last = messages[-1]
    if isinstance(last, dict):
        tool_calls = last.get("tool_calls")
        content = last.get("content", "") or ""
    else:
        tool_calls = getattr(last, "tool_calls", None)
        content = getattr(last, "content", "") or ""
    if tool_calls:
        return False
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    return bool(re.search(r"(现在|立即|马上|这就)(调用|写入)", content))


def _has_failed_write_draft(messages: list, tool_name: str) -> bool:
    """Return whether a completed agent thread still contains a repairable write draft."""
    failed_call_ids: set[str] = set()
    for message in messages or []:
        if isinstance(message, dict):
            content = message.get("content", "") or ""
            message_type = message.get("role", "") or message.get("type", "")
            call_id = message.get("tool_call_id")
        else:
            content = getattr(message, "content", "") or ""
            message_type = type(message).__name__
            call_id = getattr(message, "tool_call_id", None)
        if "Tool" not in message_type and message_type != "tool":
            continue
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        if str(content).startswith(("写入失败", "提案写入失败", "工具参数校验失败")) and call_id:
            failed_call_ids.add(str(call_id))

    if not failed_call_ids:
        return False
    for message in messages or []:
        tool_calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            else getattr(message, "tool_calls", None)
        ) or []
        if any(
            isinstance(call, dict)
            and call.get("name") == tool_name
            and str(call.get("id")) in failed_call_ids
            for call in tool_calls
        ):
            return True
    return False


def _latest_failed_write_error(messages: list) -> str:
    """Return the newest concrete write-tool validation error, if any."""
    prefixes = ("写入失败", "提案写入失败", "工具参数校验失败")
    for message in reversed(messages or []):
        if isinstance(message, dict):
            content = message.get("content", "") or ""
            message_type = message.get("role", "") or message.get("type", "")
        else:
            content = getattr(message, "content", "") or ""
            message_type = type(message).__name__
        if "Tool" not in message_type and message_type != "tool":
            continue
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        text = str(content).strip()
        if text.startswith(prefixes):
            return text
    return ""


def _latest_expected_write_error(
    messages: list,
    expected_write_tools: tuple[str, ...],
) -> str:
    """Return an error only when the newest expected write itself failed."""
    prefixes = ("写入失败", "提案写入失败", "工具参数校验失败")
    call_names: dict[str, str] = {}
    latest_outcome = ""
    for message in messages or []:
        tool_calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            else getattr(message, "tool_calls", None)
        ) or []
        for call in tool_calls:
            if isinstance(call, dict) and call.get("id") and call.get("name"):
                call_names[str(call["id"])] = str(call["name"])

        if isinstance(message, dict):
            content = message.get("content", "") or ""
            message_type = message.get("role", "") or message.get("type", "")
            call_id = message.get("tool_call_id")
        else:
            content = getattr(message, "content", "") or ""
            message_type = type(message).__name__
            call_id = getattr(message, "tool_call_id", None)
        if "Tool" not in message_type and message_type != "tool":
            continue
        if call_names.get(str(call_id)) not in expected_write_tools:
            continue
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        latest_outcome = str(content).strip()
    return latest_outcome if latest_outcome.startswith(prefixes) else ""


def _latest_failed_write_exchange(
    messages: list,
    expected_write_tools: tuple[str, ...],
) -> list:
    """Extract the newest complete failed write call and its exact tool error.

    Context editing deliberately preserves this pair as the smallest trustworthy
    repair draft. Returning only the matched call avoids carrying unrelated old
    research or unresolved sibling tool calls into the isolated repair attempt.
    """
    prefixes = ("写入失败", "提案写入失败", "工具参数校验失败")
    calls_by_id: dict[str, dict] = {}
    for message in messages or []:
        tool_calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            else getattr(message, "tool_calls", None)
        ) or []
        for call in tool_calls:
            if isinstance(call, dict) and call.get("id") and call.get("name"):
                calls_by_id[str(call["id"])] = call

    for tool_index in range(len(messages or []) - 1, -1, -1):
        tool_message = messages[tool_index]
        if isinstance(tool_message, dict):
            content = tool_message.get("content", "") or ""
            message_type = tool_message.get("role", "") or tool_message.get("type", "")
            call_id = tool_message.get("tool_call_id")
        else:
            content = getattr(tool_message, "content", "") or ""
            message_type = type(tool_message).__name__
            call_id = getattr(tool_message, "tool_call_id", None)
        if "Tool" not in message_type and message_type != "tool":
            continue
        call = calls_by_id.get(str(call_id))
        if not call or call.get("name") not in expected_write_tools:
            continue
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        outcome = str(content).strip()
        if not outcome.startswith(prefixes):
            return []
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [copy.deepcopy(call)],
            },
            {
                "role": "tool",
                "content": outcome,
                "tool_call_id": str(call_id),
            },
        ]
    return []


_CHECKPOINT_WRITE_REPAIR_PROMPT = (
    "下面保留了本线程最近一次完整写入工具调用及其真实校验错误。"
    "请只修正错误指出的参数，保留其余参数和正文，然后重新调用同一个写入工具；"
    "不要重新调研或从头生成。"
)


def _checkpoint_failed_write_recovery_messages(
    agent,
    thread_id: Optional[str],
    expected_write_tools: tuple[str, ...],
) -> list:
    """Read a repairable failed write draft from a stopped agent checkpoint."""
    if not thread_id or getattr(agent, "checkpointer", None) is None:
        return []
    try:
        snapshot = agent.get_state({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 - unreadable checkpoints keep the original failure visible
        return []
    values = getattr(snapshot, "values", None) or {}
    exchange = _latest_failed_write_exchange(
        list(values.get("messages") or []),
        expected_write_tools,
    )
    if not exchange:
        return []
    return [{"role": "user", "content": _CHECKPOINT_WRITE_REPAIR_PROMPT}, *exchange]


_FORCEFUL_RETRY_SUFFIX = (
    "\n\n注意：你上一条回复只是用文字说要调用/要写入，但这条回复本身没有发起任何工具调用——"
    "这不算完成任务，光说'现在调用'不会真的把内容写进去。这次的回复请不要输出任何文字说明，"
    "直接且仅发起一次你能用的写入类工具调用（本轮任务要求你调用的那个工具，不是随便一个工具），"
    "把你已经想好的完整内容作为参数传入。"
)

# 连续空检索达到阈值时注入的诚实强措辞（docs/ISSUES.md第80条/第107条）：
# 不撒谎说"工具已禁用"（切兜底前工具还没禁用），诚实告诉模型"你试了很多次都空手而归"，
# 让模型最后一次机会自己写诚实产出（素材库无覆盖+真实POI名+方向性建议），仍失败才切兜底。
# 跟_FORCEFUL_RETRY_SUFFIX区别：那个管"嘴上说要调用却没调用"（措辞硬约束"别只说不做"），
# 这个管"反复检索空手而归"（措辞硬约束"停止检索，直接写诚实声明"）。
_EMPTY_RETRIEVAL_STOP_SUFFIX = (
    "\n\n【重要·停止检索】你刚才已经反复检索多次，每次都返回'没有检索到相关素材'——"
    "素材库对这次需要的内容没有更多可用信息了，继续换词检索没有意义，只会耗尽你的机会。"
    "现在请**停止检索**，直接基于你已经掌握的信息（已检索到的素材+route_plan里route_agent"
    "已经写好的真实POI名/目的地名+你自己的常识），调用写入工具写入完整内容。"
    "没有素材依据的部分，按规则写诚实声明——「素材库无针对'X'的具体检索结果，根据一般经验"
    "建议…」+真实POI名+方向性建议即可，**不要凭空编造具体店名/价格等可验证事实**，也"
    "**不要用'某某镇''某某广场''当地'这种占位名或笼统措辞**，必须用route_plan里的真实"
    "POI名/目的地名写建议。这次任务必须以调用写入工具结束。"
)


_EMPTY_RETRIEVAL_MARKERS = ("没有检索到相关素材", "未检索到")


def _count_trailing_empty_retrievals(messages: list) -> int:
    """统计消息列表末尾连续多少次"检索工具返回空结果"——真实的react-agent消息序列是
    AIMessage(发起tool_call)→ToolMessage(结果)交替出现的，所以扫描时完全跳过非ToolMessage
    （AIMessage的文字/tool_calls本身不影响计数），只用ToolMessage的内容判断：连续遇到
    "没有检索到相关素材"这类空结果就累加，遇到第一条非空结果（或非检索类工具的结果）
    就停止（哪怕之前累积了不少空结果，只要中间有一次成功检索或不同类型的工具调用，
    就不再算"连续"）。

    用于_invoke_with_write_verification判断是否该切换到fallback_agent_factory：
    模型已经反复检索无果，继续用原agent重试大概率还是在同一个"换词检索"的循环里打转
    （见docs/ISSUES.md第80条：观测到单次任务里最多23次检索调用仍然全部落空）。
    """
    count = 0
    for m in reversed(messages):
        if isinstance(m, dict):
            content = m.get("content", "") or ""
            mtype = m.get("role", "") or m.get("type", "")
        else:
            content = getattr(m, "content", "") or ""
            mtype = type(m).__name__
        is_tool_message = "Tool" in mtype or mtype == "tool"
        if not is_tool_message:
            continue  # 跳过AIMessage/HumanMessage，只用ToolMessage本身判断连续性
        if isinstance(content, list):
            content = "".join(str(c) for c in content)
        if any(marker in content for marker in _EMPTY_RETRIEVAL_MARKERS):
            count += 1
        else:
            break
    return count


_COMPRESSIBLE_RETRIEVAL_TOOLS = (
    "retrieve_materials",
    "retrieve_project_evidence",
    "read_workspace",
    "web_search",
)


def _summarize_retrieval_result(content):
    """把retrieve_materials返回的JSON全文（含每条chunk完整content_snippet）压缩成
    chunk_id+平台/作者+一句摘要。见_compress_retrieval_history的完整说明。

    实际实现搬到了src/context_editing.py（跟ReAct循环内的pre_model_hook共用同一套按
    工具类型分发的摘要函数，见该模块docstring），这里保留一个薄封装是因为已有测试
    （tests/test_orchestrator_core_nodes.py）直接调用orchestrator._summarize_retrieval_result。
    """
    return _summarize_tool_result("retrieve_materials", content)


def _compress_retrieval_history(messages: list) -> list:
    """重试累加历史（_invoke_with_write_verification）的选择性压缩。

    docs/ISSUES.md第33条教训：孤立发送重试消息会让模型看不到原始任务上下文，等于凭空编内容，
    所以外层重试必须先保留可恢复的任务上下文，而不是孤立发送一句重试说明。这里先按消息类型
    压缩体积最大的检索返回；真正发送模型前，agent的pre_model_hook还会按完整请求预算清理
    已完成的旧交互轮次，避免"保留上下文"退化成无限线性增长：
    - 本函数不单独改AIMessage.tool_calls；它们会在pre_model_hook中计入预算，并可随已完成的
      普通AI+Tool调用对一起清理。record_finding调用受到显式保护。
    - ToolMessage里retrieve_materials/read_workspace/web_search类的返回，把体积大头的原文
      压成保留关键信息的摘要（按工具类型分发，见src/context_editing.py）——这是重试累加里
      真正体积失控的部分：每轮检索4-16KB chunk原文，多轮重试会滚雪球式膨胀（第2次重试
      实测≈70-90k token）。原文在这条ToolMessage第一次出现时模型已经完整看过、据此做过
      判断，重试阶段真正需要保留的是"查过什么、结果大致是什么"这个记忆，不是逐字重看。
    - write_xxx/report_conflict等写入类工具的短错误、"没有检索到相关素材"这类空结果和
      长度不超过_MIN_COMPRESS_LEN的内容在这一层原样保留；过旧的完整交互仍可能在最终预算
      治理阶段被整体删除，最新失败轮保持可见。
    """
    tool_names = _tool_call_names_by_id(messages)
    compressed = []
    for m in messages:
        mtype = (m.get("role", "") or m.get("type", "")) if isinstance(m, dict) else type(m).__name__
        if not ("Tool" in mtype or mtype == "tool"):
            compressed.append(m)
            continue
        call_id = m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
        tool_name = tool_names.get(call_id)
        if tool_name not in _COMPRESSIBLE_RETRIEVAL_TOOLS:
            compressed.append(m)
            continue
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        summarized = _summarize_tool_result(tool_name, content)
        if summarized == content:
            compressed.append(m)
            continue
        if isinstance(m, dict):
            compressed.append({**m, "content": summarized})
        else:
            new_m = copy.copy(m)
            new_m.content = summarized
            compressed.append(new_m)
    return compressed


def _invoke_with_write_verification(
    agent,
    prompt: str,
    verify_fn,
    retry_note: str,
    max_retries: int = 2,
    fallback_agent_factory=None,
    empty_retrieval_threshold: int = 3,
    network_max_retries: int = 6,
    *,
    thread_id: Optional[str] = None,
    resume_from_checkpoint: bool = False,
    agent_recursion_limit: Optional[int] = None,
    checkpoint_repair_write_tools: tuple[str, ...] = (),
) -> None:
    """调用react-agent执行一个必须产生副作用（写某个文件）的任务，并验证副作用真的发生了。

    背景（见docs/ISSUES.md）：react-agent在经过一长串retrieve_materials之类的探索性工具调用后，
    有时会只输出一段文字总结，却没有真正调用最后那个写入工具——invoke()本身不报错、正常返回，
    但期望的副作用根本没发生。之前的代码是"invoke()跑完就假设任务完成了"，这次真实跑出过
    lifestyle_agent没写任何东西、但整合agent还是"顺利"编出一份攻略的情况，必须堵上。

    2026-07-21真实E2E（chengdu-3d-e2e-v19/v20）连续两次触发"agent多次尝试后仍未完成必需
    的写入操作"这个RuntimeError兜底——排查发现比"模型偶尔抽风"严重得多的根因：build_route_agent
    等agent构建函数都没有绑定checkpointer，`create_react_agent`编译出的图在没有checkpointer时
    两次独立`.invoke()`调用之间完全无状态共享（用一个记录messages的假agent直接验证过，见
    docs/ISSUES.md）。这里此前的实现每次重试只传`[{"role": "user", "content": retry_note}]`
    这一条孤立消息，模型完全看不到原始任务（目的地/天数/需求/已检索的素材/已经做的推理），
    等于让它凭空编一个"days数组"——这不是"模型没能力"，是重试请求本身没给它任何可用信息。
    修复：在本函数内部维护一个累积的messages列表，把每次invoke()返回的完整对话（含AI的回复/
    工具调用记录）作为下一次调用的输入基础，重试消息追加在这个不断增长的历史后面，而不是
    孤立发送——这样模型在重试时依然能看到完整的原始任务上下文。

    2026-07-29补充（docs/ISSUES.md第80条）：真实复现过"模型嘴上反复说'现在调用/立即写入'，
    但连续几轮回复的tool_calls始终为空"这种子模式——跟"完全不打算调用"不是同一严重程度，
    却在此前的实现里被套用同一条通用retry_note，导致重试预算被无意义地消耗掉。这里改为：
    检测到这种"说了却没做"模式时，追加_FORCEFUL_RETRY_SUFFIX强化措辞（不输出文字、直接发起
    调用），而不是原样重复retry_note——试图打断"用文字确认意图"这个循环本身。这不是尝试消灭
    这类非确定性行为（做不到，见该条已知局限），只是针对性提高单次重试的命中率。也没有强制
    tool_choice（本项目使用的relay端点/模型不稳定支持"required"tool_choice，见
    src/materials/citation_check.py的alias_judge注释），只能在消息内容层面做文章。

    2026-07-30补充（docs/ISSUES.md第80条）：prompt层规则（"检索N次无果就该停"）和few-shot
    轨迹示例都经过严格A/B对比验证过——前者完全无效（58% vs 58%），后者方向上有改善但样本量
    不足以达到统计显著（p=0.20）。两者都是"给模型讲道理"，依赖模型自己判断和遵守。这里新增
    一条不依赖模型判断力的结构性兜底：fallback_agent_factory是一个可选的agent构建函数，
    检测到messages末尾连续empty_retrieval_threshold次（默认3）以上的"检索工具返回空结果"时，
    最后一次重试改用fallback_agent_factory()构建的agent——这个agent应该只绑write类工具，
    物理上没有retrieve_materials可用，从根上避免"继续换词检索"这个选项，而不是指望模型
    自己意识到该停手。fallback_agent_factory为None时行为跟之前完全一致（向后兼容，其余
    调用点不受影响）。这个机制本身尚未做真实E2E/A・B验证，见该条已知局限。

    2026-08-06补充（token优化）：当时累积的messages历史没有做轮次裁剪，8天长线场景
    第2次重试实测输入≈70-90k token，主要是每轮retrieve_materials返回的chunk全文
    （4-16KB/轮）滚雪球式累加。这里在每次拿到result["messages"]之后接一道
    _compress_retrieval_history：只把ToolMessage里retrieve_materials的返回从chunk全文
    压成chunk_id+摘要，作为外层重试的第一道减重。2026-08-30进一步由agent的pre_model_hook
    统计system/few-shot/tool schema/tool args和全部消息，并清理已完成旧轮次；最新失败信息
    保留，仍无法满足硬预算则显式停止。

    2026-08-13新增可选thread_id参数（见docs/ISSUES.md第120条）：只透传给**第一次**
    invoke_with_retry调用（下面的第一行），不透传给循环里重试用的那次调用——那次调用
    本来就在用这里手动维护的messages累积列表做状态传递（每轮把AI回复/重试note拼接进去），
    如果也传同一个thread_id，LangGraph的add_messages reducer会把这些内容再重复追加进
    checkpoint历史一遍，历史里会有两份。第一次调用传thread_id是为了让route_draft/
    lifestyle_draft这类耗时对话在遇到网络抖动等瞬时故障重试时，能从上次checkpoint续跑
    （invoke_with_retry内部处理），不用把已经做的几十次工具调用全部重来。thread_id为
    None（默认，大多数调用点）时不传这个kwarg——保持跟改动前完全一致的调用形态，不影响
    既有测试里那些签名是`def fake_invoke(agent, messages)`的invoke_with_retry mock。
    """
    messages: list = [{"role": "user", "content": prompt}]

    # 只有真正绑定了checkpointer的CompiledStateGraph才传thread参数；保留
    # 测试/轻量替身agent的旧调用形态，也避免把thread_id误传给无状态fallback agent。
    kwargs = {}
    if thread_id and getattr(agent, "checkpointer", None) is not None:
        kwargs = {
            "thread_id": thread_id,
            "resume_from_checkpoint": resume_from_checkpoint,
        }
    if network_max_retries != 6:
        kwargs["max_retries"] = network_max_retries
    if agent_recursion_limit is not None:
        kwargs["recursion_limit"] = agent_recursion_limit
    checkpoint_repair_active = False
    try:
        result = invoke_with_retry(agent, messages, **kwargs)
    except ContextBudgetExceededError:
        if verify_fn():
            return
        messages = _checkpoint_failed_write_recovery_messages(
            agent,
            thread_id,
            checkpoint_repair_write_tools,
        )
        if not messages:
            raise
        checkpoint_repair_active = True
    except GraphRecursionError:
        if verify_fn():
            return
        raise
    else:
        messages = _compress_retrieval_history(result["messages"])
    for attempt in range(max_retries):
        if verify_fn():
            return
        is_last_attempt = attempt == max_retries - 1
        empty_retrievals = _count_trailing_empty_retrievals(messages)
        # The restricted writer is the final missing-write fallback, not merely an
        # empty-search fallback. A research agent can have plenty of useful evidence
        # and still answer in prose without calling the required write tool (observed
        # repeatedly on Qinggan Day4). In that case the final attempt should also
        # remove research tools and make the already-collected evidence deliverable.
        use_fallback = fallback_agent_factory is not None and (
            is_last_attempt
        )
        # 分层注入（docs/ISSUES.md第107条）：
        # ① 反复检索空手而归（客观条件，连续空检索≥阈值）：注入_EMPTY_RETRIEVAL_STOP_SUFFIX
        #    "停止检索+诚实声明"——给模型最后一次机会自己写诚实产出，仍失败才切兜底。
        # ② 嘴上说要调用却没调用（_said_would_call_tool_but_didnt）：注入_FORCEFUL_RETRY_SUFFIX
        #    "别只说不做"——跟①是两个独立子模式，可叠加（一条note同时含两者也行）。
        # 兜底（use_fallback）在最后一次重试切受限agent；连续空检索时仍额外注入①的诚实声明，
        # 非空检索但始终未写入时则直接消费已有证据完成交付。
        note = retry_note
        latest_write_error = (
            _latest_expected_write_error(messages, checkpoint_repair_write_tools)
            if checkpoint_repair_active
            else _latest_failed_write_error(messages)
        )
        if latest_write_error:
            note = (
                f"{note}\n最近一次写入工具的真实返回是：{latest_write_error}\n"
                "请基于最近一次完整工具参数只修正这个校验错误，保留其他字段和正文；"
                "不要从头重写，也不要把工具已调用误判成未调用。"
            )
        if empty_retrievals >= empty_retrieval_threshold:
            note = note + _EMPTY_RETRIEVAL_STOP_SUFFIX
        if _said_would_call_tool_but_didnt(messages):
            note = note + _FORCEFUL_RETRY_SUFFIX
        messages = messages + [{"role": "user", "content": note}]
        active_agent = fallback_agent_factory() if use_fallback else agent
        # 不传thread_id，见上面docstring的说明。
        retry_kwargs = (
            {"max_retries": network_max_retries}
            if network_max_retries != 6
            else {}
        )
        if agent_recursion_limit is not None:
            retry_kwargs["recursion_limit"] = agent_recursion_limit
        if use_fallback:
            retry_kwargs["recursion_limit"] = 10
        if thread_id and getattr(active_agent, "checkpointer", None) is not None:
            # A compiled agent with a checkpointer rejects invocations that omit
            # configurable.thread_id. Reusing the research thread here would append
            # the accumulated messages to its existing history again, so each
            # write-verification attempt gets an isolated child thread. Network
            # retries within invoke_with_retry still resume that child checkpoint.
            retry_kwargs["thread_id"] = (
                f"{thread_id}:write-verification:{attempt + 1}:{uuid.uuid4().hex}"
            )
        try:
            result = invoke_with_retry(active_agent, messages, **retry_kwargs)
        except (ContextBudgetExceededError, GraphRecursionError):
            if verify_fn():
                return
            raise
        messages = _compress_retrieval_history(result["messages"])
    if not verify_fn():
        latest_write_error = (
            _latest_expected_write_error(messages, checkpoint_repair_write_tools)
            if checkpoint_repair_active
            else _latest_failed_write_error(messages)
        )
        detail = latest_write_error or retry_note
        raise RuntimeError(f"agent多次尝试后仍未完成必需的写入操作：{detail}")


def material_agent_node(state: GraphState) -> dict:
    topic = state["topic"]
    try:
        entries = ingest_topic(topic)
        summary = f"素材已就绪，新增{len(entries)}条"
    except FileNotFoundError:
        # v1 MVP阶段没有真实抓取API，raw/<topic>/下完全没有素材时先如实告知，不中断流程
        # （route_agent等下游agent会缺少素材参考，只能更多依赖自身知识+高德MCP）
        summary = f"目的地「{topic}」暂无素材库内容（v1阶段需要人工放入 data/material_library/raw/{topic}/），已跳过"
    return {
        "material_checked": True,
        "last_agent_result": {"agent": "material_agent", "summary": summary},
    }


_REQUIRED_REQUIREMENTS_FIELDS = ("destination", "days")
# origin/transport 不是硬性必需（用户没说从哪出发时不要瞎编出发地）——走可选追问，
# 默认值"未告知"/"不限"。route_agent 第13条用"origin != '未告知'"做分支判断要不要给
# 城际大交通方案；用户明确表示"交通自己安排"时 transport 会是这类值，route_agent 据此跳过。
_OPTIONAL_REQUIREMENTS_DEFAULTS = {
    "style": "不限",
    "budget": "不限",
    "notes": "无",
    "origin": "未告知",
    "transport": "不限",
}


def _missing_required_fields(requirements: dict) -> list[str]:
    return [f for f in _REQUIRED_REQUIREMENTS_FIELDS if not requirements.get(f)]


def _visible_requirements_draft(draft: dict) -> dict:
    """去掉draft里"_"开头的内部记账字段（如_optional_asked），只暴露真正的需求字段——
    既用于展示给用户（interrupt payload），也用于最终写入state["requirements"]。"""
    return {k: v for k, v in draft.items() if not k.startswith("_")}


class _MustVisitItem(BaseModel):
    name: str = Field(description="必去地点名称")
    day: Optional[int] = Field(default=None, description="用户明确指定的具体第几天，没有指定具体哪天则为null")


class _StyleInterpretation(BaseModel):
    """style自由文本的结构化解读，四个维度各取三档枚举（docs/ARCHITECTURE.md第18.5节）。
    只有当style本身有值时才会被解读，四个维度独立判断，某个维度style原文没有体现倾向
    就填null（不要为了填满而瞎猜档位）。"""

    poi_density: Optional[Literal["偏少", "适中", "偏多"]] = Field(
        default=None, description="每天POI数量密度倾向，style没体现相关倾向则为null"
    )
    stay_duration: Optional[Literal["偏少", "适中", "偏多"]] = Field(
        default=None, description="单点停留时长倾向，style没体现相关倾向则为null"
    )
    needs_detail_bias: Optional[Literal["偏少", "适中", "偏多"]] = Field(
        default=None,
        description="needs_detail=true判断倾向（深度游更倾向标true触发深度玩法），style没体现相关倾向则为null",
    )
    flexible_time: Optional[Literal["偏少", "适中", "偏多"]] = Field(
        default=None, description="预留弹性/未定时间的倾向，style没体现相关倾向则为null"
    )


class _RequirementsFields(BaseModel):
    """LLM结构化抽取用的schema，跟src/materials/ingest.py的_ContentAnalysis同款
    with_structured_output(method="json_mode")模式——deepseek-v4-flash不支持
    json_schema/function_calling响应格式，json_mode是目前验证下来最稳的方式（见docs/ISSUES.md）。"""

    destination: Optional[str] = Field(default=None, description="目的地城市/地区名称，消息里没提到则为null，不要编造")
    days: Optional[int] = Field(default=None, description="旅游天数，整数，消息里没提到则为null")
    style: Optional[str] = Field(default=None, description="旅行风格/偏好（如'美食+人文'），没提到则为null")
    budget: Optional[str] = Field(default=None, description="预算范围，没提到则为null")
    notes: Optional[str] = Field(default=None, description="忌口或其他注意事项，用户明确表示没有则填'无'，完全没提到则为null")
    origin: Optional[str] = Field(default=None, description="出发城市/地区，消息里没提到则为null，不要编造")
    transport: Optional[str] = Field(default=None, description="交通方式偏好（高铁/飞机/自驾/大巴/不限/自己安排），没提到则为null")
    travel_start_date: Optional[str] = Field(
        default=None, description="明确出发日期，格式YYYY-MM-DD；用户没给出精确日期则为null"
    )
    travel_end_date: Optional[str] = Field(
        default=None, description="明确结束日期，格式YYYY-MM-DD；用户没给出精确日期则为null"
    )
    travel_date_text: Optional[str] = Field(
        default=None, description="用户对出行时间的原始表达，如'10月'、'秋季'，没提到则为null"
    )
    travel_date_precision: Optional[Literal["exact", "month", "season", "unknown"]] = Field(
        default=None, description="出行时间精度：精确日期/month/season/unknown"
    )
    must_visit: list[_MustVisitItem] = Field(
        default_factory=list,
        description="用户明确提到一定要去/想去的地点，或用户提交的一份完整行程草稿（一段包含多天多地点的长文本，"
        "如'Day1去A，Day2去B和C'）解析出的每个地点+对应天数。草稿场景每一条都应该带day；"
        "零散提及场景没说哪天就day=null。这句话里完全没有提到必去地点/草稿时留空列表",
    )
    style_interpretation: Optional[_StyleInterpretation] = Field(
        default=None,
        description="仅当style字段本身有值时才需要解读产出（四个维度：poi_density/stay_duration/"
        "needs_detail_bias/flexible_time，各自取'偏少'/'适中'/'偏多'，style里没体现的维度为null）；"
        "style为null时这个字段整体也应该是null，不要强行解读",
    )


def _extract_requirements_fields(user_message: str) -> dict:
    # 简单固定字段抽取，不涉及内容生成/核验，用轻量模型（见CLAUDE.md模型分层讨论）。
    model = get_light_chat_model(temperature=0)
    structured = model.with_structured_output(_RequirementsFields, method="json_mode")
    result: _RequirementsFields = structured.invoke(
        f"用户消息：{user_message}\n"
        "请从这句话里抽取旅游需求相关字段：destination(目的地)/days(天数,整数)/"
        "style(风格偏好)/budget(预算)/notes(忌口或注意事项)/origin(出发城市)/"
        "transport(交通方式偏好，如高铁/飞机/自驾/不限，用户说自己安排就填'自己安排')/"
        "travel_start_date/travel_end_date(明确日期用YYYY-MM-DD)/"
        "travel_date_text(用户的月份、季节或日期原文)/"
        "travel_date_precision(exact/month/season/unknown)/"
        "must_visit(必去地点列表，每项含name和day；如果用户提交的是一段包含多天多地点的完整"
        "行程草稿，把解析出的每个地点连同它所在的天数都放进must_visit，草稿本身隐含的总天数"
        "也应该体现在days字段里；如果只是零散提到某个地点一定要去、没说哪天，day填null；"
        "完全没提到必去地点/草稿则留空列表)/"
        "style_interpretation(仅当style有值时才需要解读，四个维度poi_density/stay_duration/"
        "needs_detail_bias/flexible_time各取'偏少'/'适中'/'偏多'，style原文没体现某个维度倾向"
        "就填null；style本身为null时这个字段整体也不用给)。"
        "这句话里没有明确提到的字段填null，不要编造用户没说过的信息；如果用户明确表示"
        "'没有'/'不限'/'随便'，对应字段可以填'不限'或'无'而不是null。请以JSON格式回复。"
    )
    extracted = {k: v for k, v in result.model_dump().items() if v is not None}
    # must_visit是list字段，空列表([])在上面的"v is not None"过滤里会被保留下来（合法值，
    # 不是"没提取到"），这里显式去掉空列表，避免每次抽取都在draft里挂一个没有信息量的空key。
    if not extracted.get("must_visit"):
        extracted.pop("must_visit", None)
    if not extracted.get("style_interpretation"):
        extracted.pop("style_interpretation", None)
    return extracted


def _merge_must_visit(existing: list, new: list) -> list:
    """按name合并must_visit列表——同名地点用最新解析结果覆盖day（修正场景：用户说"不对，
    是第2天不是第3天"，新解析结果应该覆盖旧的），不同名地点追加，不丢弃此前已确认/已提到的条目。"""
    merged = list(existing)
    index_by_name = {item.get("name"): idx for idx, item in enumerate(merged)}
    for item in new:
        name = item.get("name")
        if name in index_by_name:
            merged[index_by_name[name]] = item
        else:
            merged.append(item)
            index_by_name[name] = len(merged) - 1
    return merged


def _apply_requirements_answer(draft: dict, answer) -> None:
    """把一次用户回复的抽取结果合并进draft，must_visit单独做增量合并（而不是被
    dict.update整体覆盖丢掉之前轮次已经收集到的条目）——见docs/ARCHITECTURE.md第18.3节
    "不主动问，识别到才捕获"，必去地点可能分散在对话的不同轮次里提到。

    合并后must_visit若真的发生变化，重置_must_visit_confirmed=False，需要重新走一轮确认
    （见_must_visit_confirm_question所在的确认循环）。"""
    if not (isinstance(answer, str) and answer.strip()):
        return
    extracted = _extract_requirements_fields(answer)
    new_must_visit = extracted.pop("must_visit", None)
    draft.update(extracted)
    if new_must_visit:
        merged = _merge_must_visit(draft.get("must_visit") or [], new_must_visit)
        if merged != draft.get("must_visit"):
            draft["must_visit"] = merged
            draft["_must_visit_confirmed"] = False


class _ConfirmationVerdict(BaseModel):
    confirmed: bool = Field(
        description="用户是否明确表示确认/同意/没有问题，没有提出需要更正或补充的新内容"
    )


def _classify_must_visit_confirmation(answer: str) -> bool:
    """轻量模型二选一判断：这句回复是"确认无误"还是"还有要修正/补充的内容"（见
    docs/ARCHITECTURE.md第18.4节确认循环）。不涉及内容生成/核验，用轻量模型。"""
    model = get_light_chat_model(temperature=0).with_structured_output(
        _ConfirmationVerdict, method="json_mode"
    )
    verdict: _ConfirmationVerdict = model.invoke(
        f"用户回复：{answer}\n"
        "请判断用户是否明确表示确认/同意/没有问题（比如「对」「确认」「没错」「可以」），"
        "还是提出了需要更正或补充的新内容。请以JSON格式回复，包含confirmed（布尔值）字段。"
    )
    return verdict.confirmed


def _must_visit_confirm_question(must_visit: list) -> str:
    lines = []
    for item in must_visit:
        day = item.get("day")
        day_desc = f"第{day}天" if day else "未指定具体哪天"
        lines.append(f"{item.get('name')}（{day_desc}）")
    listed = "、".join(lines)
    return (
        f"我理解您的意思是必去以下地点：{listed}，对吗？"
        "如果有需要补充或修正的地方请直接说明，确认无误请回复「确认」。"
    )


def _next_requirements_question(draft: dict) -> str:
    missing = _missing_required_fields(draft)
    if missing:
        asks = []
        if "destination" in missing:
            asks.append("想去哪里旅游")
        if "days" in missing:
            asks.append("打算玩几天")
        return "、".join(asks) + "？"
    # 可选追问阶段：origin/transport 跟 style/budget/notes 一起问一轮，用户答"没有"则落默认值，
    # 不会无限追问。origin 问的是"从哪出发"——route_agent 第13条据此判断要不要给城际大交通方案；
    # transport 问的是"想坐什么交通工具"——用户说"自己安排"时 route_agent 跳过大交通方案。
    return (
        "了解！这次旅行从哪里出发、主要想坐什么交通工具（高铁/飞机/自驾等）？"
        "大概什么时候出发？另外在风格、预算、忌口或其他注意事项上有什么偏好吗？"
        "没有的话直接说「没有」就行。"
    )


def requirements_gather_node(state: GraphState) -> dict:
    """真正的多轮需求收集（飞书接入第一期核心新增）：不断interrupt()问缺失信息，
    直到destination/days（硬性必需）齐全、且风格/预算/忌口这一轮可选追问也问过一次
    （用户答"没有"则落默认值，不会无限追问）。

    只在state["requirements"]本身不完整时才会被路由到这个节点——CLI/API等调用方
    一次性传入完整requirements dict的既有用法完全不受影响，见_route_plan_mode。

    docs/ARCHITECTURE.md第18.4节：must_visit（必去地点/完整行程草稿解析结果）在
    必需+可选字段都收集完之后，还需要额外一轮（或多轮）确认——展示解析结果给用户，
    用户明确表示确认才真正定稿；提出修正则合并修正内容、重新展示确认，循环直到用户
    明确满意为止（跟await_skeleton_confirm_node同款"同一节点内部处理确认通过/提出
    修正循环"模式，见该节点docstring）。style_interpretation是从style自由文本派生的
    软信号，不需要用户单独确认。

    只做"读状态 -> interrupt() -> 处理回复"，不在interrupt()之前做任何有副作用的操作
    （resume时节点会从头重跑，跟await_skeleton_confirm_node同款约束）。"""
    draft = dict(state.get("requirements_draft") or {})
    missing_before = _missing_required_fields(draft)
    asking_optional_now = not missing_before and not draft.get("_optional_asked")
    awaiting_must_visit_confirm = (
        not missing_before
        and draft.get("_optional_asked")
        and bool(draft.get("must_visit"))
        and not draft.get("_must_visit_confirmed")
    )

    if awaiting_must_visit_confirm:
        question = _must_visit_confirm_question(draft["must_visit"])
    else:
        question = _next_requirements_question(draft)
    answer = interrupt({"question": question, "draft": _visible_requirements_draft(draft)})

    if awaiting_must_visit_confirm:
        if isinstance(answer, str) and answer.strip():
            if _classify_must_visit_confirmation(answer):
                draft["_must_visit_confirmed"] = True
            else:
                # 提出了修正/补充：合并进must_visit（可能改day、加新地点），保持未确认，
                # 下一轮重新展示确认——不会因为一次不满意的回复就无限卡死在同一句话上。
                _apply_requirements_answer(draft, answer)
    else:
        _apply_requirements_answer(draft, answer)
        if asking_optional_now:
            draft["_optional_asked"] = True
            for field, default in _OPTIONAL_REQUIREMENTS_DEFAULTS.items():
                draft.setdefault(field, default)

    must_visit_pending = bool(draft.get("must_visit")) and not draft.get("_must_visit_confirmed")
    if _missing_required_fields(draft) or not draft.get("_optional_asked") or must_visit_pending:
        return {"requirements_draft": draft}

    requirements = _visible_requirements_draft(draft)
    return {
        "requirements_draft": draft,
        "requirements": requirements,
        # topic必须跟着一起更新——_route_plan_mode顶部的素材前置校验、route_draft_node
        # 都直接读state["topic"]，这个字段在多轮收集开始时是空的（尚未知道目的地），
        # 不在这里显式写回的话，即使requirements已经完整，下游也拿不到正确的目的地。
        "topic": requirements["destination"],
        "last_agent_result": {"agent": "main_agent", "summary": "需求已收集完整"},
    }


def collect_requirements_node(state: GraphState) -> dict:
    store.update_progress(state["project_id"], actor="main_agent", state=ProjectPhase.SKELETON_CONFIRM)
    return {"last_agent_result": {"agent": "main_agent", "summary": "需求已收集"}}


def _changed_route_days(before: Optional[dict], after: Optional[dict]) -> list[int]:
    """route_plan.json的天级diff，跟_changed_lifestyle_fields同一个思路（见docs/ISSUES.md第37条），
    粒度从"字段"换成"天"。只比较两边都存在的day编号——days数量不同（用户可能真的要求
    "加一天"/"减少一天"）时不强行比对，这类合法场景不做误改检测，是已知局限，不是bug。"""
    before_days = {d.get("day"): d for d in (before or {}).get("days", [])}
    after_days = {d.get("day"): d for d in (after or {}).get("days", [])}
    if set(before_days) != set(after_days):
        return []
    common_days = sorted(before_days)
    return [d for d in common_days if before_days[d] != after_days[d]]


class _EditScopeInference(BaseModel):
    covered: list[str] = Field(
        description="用户这次编辑请求实际涵盖的候选项，必须从给定候选项集合里原样选取，不要编造候选集合之外的值"
    )


def _infer_edit_scope(
    edit_request: str,
    candidates: set,
    conflict_target: Optional[str] = None,
) -> Optional[set]:
    """独立于被检查agent之外，用轻量模型（get_light_chat_model）读edit_request原文，判断
    这次编辑请求实际涵盖了candidates（字段名集合如{"food","outfit","lodging","play"}，或
    route_plan里实际存在的day编号集合）里的哪些项。见docs/ARCHITECTURE.md第20.2节。

    独立判断而不是让被检查的agent自己汇报涵盖范围——如果agent真的越权，它也可能同步"粉饰"
    自己汇报的范围，让检测形同虚设。

    返回具体涉及范围的**集合**，不是数量——只比数量无法发现"数量凑巧对上、但改动的具体
    内容跟用户请求完全不沾边"这类问题（例：用户要求改food+play，agent却改了outfit+lodging，
    数量都是2，纯数字比对会放过）。

    conflict_target非空时（这次是系统核验打回重试，不是用户首次发起的edit，形如
    "route_agent.day2"/"lifestyle_agent.food"），把它指向的具体候选项也并入返回的范围——
    覆盖"因核验发现问题被指定回来修，顺带做了语义上合理的连带修正"这种场景，不会被误判成越权。

    LLM调用失败（返回格式不对/API异常）时返回None，调用方据此降级回退到现有的静态阈值1
    判断——保证有一道兜底，新机制不工作时不会比现状更差。"""
    if not candidates:
        return set()

    label_to_candidate = {str(c): c for c in candidates}
    scope: set = set()
    try:
        model = get_light_chat_model(temperature=0).with_structured_output(
            _EditScopeInference, method="json_mode"
        )
        verdict: _EditScopeInference = model.invoke(
            f"用户提出的编辑请求原文：{edit_request}\n"
            f"候选项集合：{sorted(label_to_candidate)}\n"
            f"请判断这次编辑请求实际涵盖了候选项集合里的哪些项——只能从候选项集合里原样选择，"
            f"不要编造候选集合之外的值，也不要遗漏请求里明确涉及的候选项。"
            f"请以JSON格式回复，包含covered（字符串数组）字段。"
        )
        scope = {label_to_candidate[label] for label in verdict.covered if label in label_to_candidate}
    except Exception:
        return None

    if conflict_target:
        parts = conflict_target.split(".", 1)
        if len(parts) == 2:
            suffix = parts[1]
            if suffix.startswith("day"):
                suffix = suffix[len("day"):]
            if suffix in label_to_candidate:
                scope.add(label_to_candidate[suffix])

    return scope


def _is_edit_overreach(
    edit_request: str,
    changed: list,
    candidates: set,
    conflict_target: Optional[str],
) -> bool:
    """三处edit定点修改误改检测（route_draft_node/lifestyle_agent_node/pitfall_agent_node）
    共用的越权判断：允许范围 = _infer_edit_scope算出的用户请求涵盖范围 ∪ conflict_target
    指向范围，实际改动集合超出这个允许范围才判定越权。_infer_edit_scope返回None（LLM判断
    本身失败）时降级回退到现有的静态阈值1判断（docs/ARCHITECTURE.md第20.2节）。"""
    scope = _infer_edit_scope(edit_request, candidates, conflict_target)
    if scope is None:
        return len(changed) > 1
    return not set(changed).issubset(scope)


def _missing_must_visit_items(must_visit: list, route_plan: Optional[dict]) -> list:
    """docs/ARCHITECTURE.md第18.7节确定性校验：must_visit每一项检查是否真的出现在
    route_plan里——完全没出现，或指定了day但排到了别的天（day错位视同遗漏）。
    排上了但内容是否合理不在这里判断（交给route_agent自己在notes里处理，见规则8）。

    复用_entity_matches做地点名字面匹配（允许剥离常见地标后缀，跟citation_check同款
    容错），不要求POI名称逐字相同。"""
    days = (route_plan or {}).get("days", [])
    pois_by_day: dict[int, list[str]] = {}
    all_poi_names: list[str] = []
    for d in days:
        day_num = d.get("day")
        names = [p.get("name", "") for p in d.get("pois", [])]
        pois_by_day[day_num] = names
        all_poi_names.extend(names)

    missing = []
    for item in must_visit:
        name = item.get("name")
        day = item.get("day")
        if not name:
            continue
        if day is not None:
            day_names = pois_by_day.get(day, [])
            if not any(_entity_matches(name, n) or _entity_matches(n, name) for n in day_names):
                missing.append(item)
        else:
            if not any(_entity_matches(name, n) or _entity_matches(n, name) for n in all_poi_names):
                missing.append(item)
    return missing


def _apply_skeleton_revision_constraint_delta(requirements: dict, revision: str) -> tuple[dict, list[str]]:
    """Apply only explicit hard-constraint changes stated in a skeleton revision.

    The parser is intentionally conservative and deterministic. It supports a
    new total day count plus removing/moving already-known must-visit places;
    ordinary wording changes leave requirements untouched.
    """
    updated = copy.deepcopy(requirements)
    changes: list[str] = []

    day_match = re.search(r"(?:改成|调整为|变成|增加到|延长到|缩短为)\s*([1-9]\d*)\s*天", revision)
    if day_match:
        new_days = int(day_match.group(1))
        if new_days != updated.get("days"):
            updated["days"] = new_days
            changes.append(f"天数改为{new_days}天")

    must_visit = copy.deepcopy(updated.get("must_visit") or [])
    move_words = ("改到", "放到", "挪到", "移到")
    retained = []
    for item in must_visit:
        name = str(item.get("name", "")).strip()
        if not name or name not in revision:
            retained.append(item)
            continue
        removal_pattern = (
            rf"(?:不去|取消|删除|去掉)[^，。；]{{0,12}}{re.escape(name)}"
            rf"|{re.escape(name)}[^，。；]{{0,8}}(?:不去|取消|删除|去掉|不去了)"
        )
        if re.search(removal_pattern, revision):
            changes.append(f"取消必去地点{name}")
            continue
        day_pattern = rf"{re.escape(name)}.*?(?:{'|'.join(move_words)}).*?第?\s*([1-9]\d*)\s*天"
        move_match = re.search(day_pattern, revision)
        if move_match:
            new_day = int(move_match.group(1))
            if item.get("day") != new_day:
                item["day"] = new_day
                changes.append(f"{name}改到第{new_day}天")
        retained.append(item)
    if retained != must_visit:
        if retained:
            updated["must_visit"] = retained
        else:
            updated.pop("must_visit", None)
    elif must_visit:
        updated["must_visit"] = retained
    return updated, changes


def _route_hard_constraint_issues(requirements: dict, route_plan: Optional[dict]) -> list[dict]:
    """Combine day topology/basic-content checks with must-visit checks."""
    issues = validate_route_structure(route_plan, expected_days=requirements.get("days"))
    missing = _missing_must_visit_items(requirements.get("must_visit") or [], route_plan)
    if missing:
        issues.append(
            {
                "code": "must_visit_missing",
                "message": f"必去地点未按要求出现：{_format_must_visit_for_prompt(missing)}",
                "missing": missing,
            }
        )
    return issues


def _format_route_constraint_issues(issues: list[dict]) -> str:
    return "；".join(str(issue.get("message") or issue.get("code") or issue) for issue in issues)


def _format_must_visit_for_prompt(must_visit: list) -> str:
    if not must_visit:
        return "无"
    parts = []
    for item in must_visit:
        day = item.get("day")
        parts.append(f"{item.get('name')}（第{day}天）" if day else f"{item.get('name')}（未指定具体哪天）")
    return "、".join(parts)


def _format_style_interpretation_for_prompt(style_interpretation: Optional[dict]) -> str:
    if not style_interpretation:
        return "无"
    labels = {
        "poi_density": "每天POI数量密度",
        "stay_duration": "单点停留时长倾向",
        "needs_detail_bias": "needs_detail判断倾向",
        "flexible_time": "预留弹性时间倾向",
    }
    parts = [
        f"{labels[dim]}={value}"
        for dim, value in style_interpretation.items()
        if value and dim in labels
    ]
    return "、".join(parts) if parts else "无"


def _route_structure_proposal_thread_id(project_id: str, work_unit_id: str) -> str:
    return agent_thread_id(
        project_id,
        "route_structure_proposal",
        suffix=f"work-{work_unit_id}",
    )


def _route_structure_proposal_matches_work_unit(
    proposal: dict,
    *,
    work_unit_id: str,
    trigger: str,
    current_route: dict,
    affected_days: list[int],
) -> bool:
    proposed_days = {
        int(item["day"])
        for item in proposal.get("proposed_days", [])
        if isinstance(item, dict) and item.get("day") is not None
    }
    affected_day_set = set(affected_days)
    return bool(
        proposal.get("work_unit_id") == work_unit_id
        and proposal.get("status") == "pending"
        and proposal.get("trigger") == trigger
        and proposal.get("base_structure") == route_structure_snapshot(current_route)
        and route_structure_diff(
            proposal.get("base_structure"), proposal.get("candidate_route_plan")
        )
        and (
            not affected_day_set
            or (proposed_days and proposed_days.issubset(affected_day_set))
        )
    )


def _request_route_structure_proposal(
    project_id: str,
    topic: str,
    *,
    trigger: str,
    reason: str,
    evidence: str = "",
    revision_feedback: str = "",
    affected_days: Optional[list[int]] = None,
    replaces_proposal_id: Optional[str] = None,
    source_identity: str,
) -> dict:
    """Ask a proposal-only Route Agent to persist a new structural candidate."""
    source_identity = source_identity.strip()
    if not source_identity:
        raise ValueError("路线结构候选缺少稳定来源身份")
    normalized_days = sorted({int(day) for day in (affected_days or []) if day is not None})
    current_route = store.read_workspace(project_id, "route_plan.json") or {}
    route_structure_sha = route_structure_hash(current_route)
    work_unit_payload = json.dumps(
        {
            "route_structure_sha256": route_structure_sha,
            "trigger": trigger,
            "affected_days": normalized_days,
            "replaces_proposal_id": replaces_proposal_id,
            "source_identity": source_identity,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    work_unit_id = hashlib.sha256(work_unit_payload.encode("utf-8")).hexdigest()[:20]
    proposal_thread_id = _route_structure_proposal_thread_id(project_id, work_unit_id)

    previous = store.read_route_change_proposal(project_id) or {}
    previous_id = previous.get("proposal_id")
    previous_matches_work_unit = _route_structure_proposal_matches_work_unit(
        previous,
        work_unit_id=work_unit_id,
        trigger=trigger,
        current_route=current_route,
        affected_days=normalized_days,
    )
    if previous_matches_work_unit:
        return previous

    agent = build_route_agent(
        project_id,
        topic,
        checkpointer=get_agent_checkpointer(project_id),
        write_mode="proposal_only",
        proposal_work_unit_id=work_unit_id,
    )
    try:
        proposal_snapshot = agent.get_state(
            {"configurable": {"thread_id": proposal_thread_id}}
        )
        proposal_checkpoint_pending = bool(
            proposal_snapshot.values and proposal_snapshot.next
        )
    except Exception:  # noqa: BLE001 - unreadable checkpoint falls back to a fresh proposal
        proposal_checkpoint_pending = False

    if not proposal_checkpoint_pending:
        clear_agent_thread_family(project_id, proposal_thread_id)
        reset_web_search_log(project_id, "route_agent")
        reset_amap_evidence_log(project_id, "route_agent")
    set_trace_context(project_id, "route_agent")
    feedback_note = f"\n用户对上一版候选的调整意见：{revision_feedback}" if revision_feedback else ""
    if normalized_days:
        affected_day_set = set(normalized_days)
        proposal_inputs = []
        for day_item in current_route.get("days", []):
            if not isinstance(day_item, dict) or day_item.get("day") not in affected_day_set:
                continue
            proposal_day = copy.deepcopy(day_item)
            for claim in proposal_day.get("claims", []) or []:
                if isinstance(claim, dict):
                    claim.pop("evidence_snapshots", None)
            proposal_inputs.append(proposal_day)
        read_instruction = (
            f"受影响天已定位为{normalized_days}。以下是这些天从当前route_plan.json提取的"
            "完整提交底稿（仅移除可由程序重建的evidence_snapshots）：\n"
            f"{json.dumps(proposal_inputs, ensure_ascii=False, indent=2)}\n"
            "这些底稿已足够生成候选，无需再次调用read_workspace读取同一路线。"
        )
    else:
        read_instruction = "请先读取当前route_plan.json，定位确实受影响的既有天。"
    prompt = (
        f"正式route_plan已经由用户确认，但系统发现可能必须调整路线结构。\n"
        f"触发来源：{trigger}\n原因：{reason}\n证据：{evidence or '见原因说明'}"
        f"{feedback_note}\n"
        f"{read_instruction}只调整确实受影响的既有天，并调用"
        f"write_route_change_proposal提交候选。候选应同时修正相关notes/sources；"
        f"不要直接覆盖正式路线，也不要改动无关天数。"
    )

    def _proposal_written() -> bool:
        proposal = store.read_route_change_proposal(project_id) or {}
        return bool(
            proposal.get("proposal_id")
            and proposal.get("proposal_id") != previous_id
            and _route_structure_proposal_matches_work_unit(
                proposal,
                work_unit_id=work_unit_id,
                trigger=trigger,
                current_route=current_route,
                affected_days=normalized_days,
            )
        )

    _invoke_with_write_verification(
        agent,
        prompt,
        verify_fn=_proposal_written,
        retry_note="你刚才没有真正调用write_route_change_proposal提交结构候选，"
        "或候选没有产生任何结构变化。请读取正式路线后提交一个有明确差异的候选。",
        thread_id=proposal_thread_id,
        resume_from_checkpoint=proposal_checkpoint_pending,
        checkpoint_repair_write_tools=("write_route_change_proposal",),
    )
    proposal = store.read_route_change_proposal(project_id)
    if not proposal or not _route_structure_proposal_matches_work_unit(
        proposal,
        work_unit_id=work_unit_id,
        trigger=trigger,
        current_route=current_route,
        affected_days=normalized_days,
    ):
        raise RuntimeError("Route Agent未生成可确认的路线结构调整提案")
    return proposal


def route_draft_node(state: GraphState) -> dict:
    """真正调用route_agent（有LLM调用+写文件的副作用），必须和await_skeleton_confirm分开，
    避免interrupt resume时因为"节点从头重跑"而被意外重复调用。

    可能因四种原因被调用：①首次生成骨架；②用户在骨架确认阶段要求调整（revision_request）；
    ③被route_citation_check_node或integration_agent的report_conflict打回重新生成
    （target以"route_agent"开头的pending conflict）；④edit模式下用户完成后单独@route_agent
    定点修改某一天（is_direct_edit_target，跟lifestyle_agent_node/pitfall_agent_node同款
    定点编辑模式，粒度是天不是字段）。四种情况互斥，判断顺序：revision > is_direct_edit_target > 默认。"""
    project_id = state["project_id"]
    structure_revision = state.get("route_structure_revision_request")
    structure_revision_context = state.get("route_structure_revision_context")
    if structure_revision:
        if not isinstance(structure_revision_context, dict):
            raise RuntimeError("路线结构修订缺少可恢复的来源候选上下文")
        source_proposal_id = str(
            structure_revision_context.get("source_proposal_id") or ""
        )
        if not source_proposal_id:
            raise RuntimeError("路线结构修订缺少来源proposal_id")
        current_proposal = store.read_route_change_proposal(project_id) or {}
        if current_proposal.get("proposal_id") == source_proposal_id:
            if current_proposal.get("status") == "pending":
                store.decide_route_change_proposal(
                    project_id,
                    actor="main_agent",
                    decision="supersede",
                )
            elif current_proposal.get("status") != "superseded":
                raise RuntimeError(
                    "路线结构修订来源候选已被其他决定处理，不能继续自动生成新候选"
                )

        proposal = _request_route_structure_proposal(
            project_id,
            state["topic"],
            trigger=str(
                structure_revision_context.get("trigger")
                or "post_confirmation_check"
            ),
            reason=str(
                structure_revision_context.get("reason")
                or "路线结构需要调整"
            ),
            evidence=str(structure_revision_context.get("evidence") or ""),
            revision_feedback=structure_revision,
            affected_days=[
                int(day)
                for day in (structure_revision_context.get("affected_days") or [])
            ],
            replaces_proposal_id=source_proposal_id,
            source_identity=(
                f"proposal:{source_proposal_id}:feedback:"
                + hashlib.sha256(
                    " ".join(structure_revision.split()).encode("utf-8")
                ).hexdigest()[:20]
            ),
        )
        return {
            "pending_route_structure_confirmation": True,
            "route_structure_revision_request": None,
            "route_structure_revision_context": None,
            "last_agent_result": {
                "agent": "route_agent",
                "summary": f"已按意见更新结构候选 {proposal['proposal_id']}",
            },
        }

    conflict = store.get_pending_conflict(project_id)
    revision = state.get("revision_request")
    is_direct_edit_target = (
        state.get("mode") == "edit"
        and state.get("edit_request")
        and state.get("entry_target") == "route_draft"
    )

    # 用户确认骨架后的整合冲突不能再让完整写工具静默覆盖正式路线。此时Route Agent
    # 只生成候选，真正route_plan要等await_route_structure_confirm_node收到用户决定。
    progress = store.read_progress(project_id)
    if (
        conflict
        and conflict.target.split(".")[0] == "route_agent"
        and (
            progress.state in {ProjectPhase.INTEGRATING, ProjectPhase.DONE}
            or progress.skeleton_confirmed
            or state.get("mode") == "edit"
        )
        and not revision
        and not is_direct_edit_target
    ):
        proposal = _request_route_structure_proposal(
            project_id,
            state["topic"],
            trigger="integration_conflict",
            reason=conflict.reason,
            evidence=json.dumps(conflict.issues, ensure_ascii=False),
            affected_days=sorted(
                {
                    int(issue["day"])
                    for issue in (conflict.issues or [])
                    if isinstance(issue, dict) and issue.get("day") is not None
                }
            ),
            source_identity=f"conflict:{conflict.id}",
        )
        return {
            "pending_route_structure_confirmation": True,
            "route_structure_resume_check": "integration",
            "last_agent_result": {
                "agent": "route_agent",
                "summary": f"已生成待确认的路线结构候选 {proposal['proposal_id']}",
            },
        }
    agent = build_route_agent(project_id, state["topic"], checkpointer=get_agent_checkpointer(project_id))
    route_thread_id = agent_thread_id(project_id, "route_draft")
    try:
        route_snapshot = agent.get_state(
            {"configurable": {"thread_id": route_thread_id}}
        )
        route_checkpoint_pending = bool(route_snapshot.values) and bool(route_snapshot.next)
    except Exception:  # noqa: BLE001 - checkpoint读取失败时退化为一次全新生成
        route_checkpoint_pending = False

    # 只有逻辑上全新的路线生成才清理历史。进程中断后根图会重新进入route_draft；
    # 此时内层Agent若仍有next，必须保留它的工具历史和失败写入底稿并从input=None续跑。
    # 否则所谓per-project持久化checkpoint只保住了根节点位置，实际调研仍会从头重来。
    if not route_checkpoint_pending:
        reset_web_search_log(project_id, "route_agent")
        reset_amap_evidence_log(project_id, "route_agent")
        clear_agent_thread(project_id, "route_draft")
    set_trace_context(project_id, "route_agent")
    req = state["requirements"]
    revision_constraint_changes: list[str] = []
    if revision:
        req, revision_constraint_changes = _apply_skeleton_revision_constraint_delta(req, revision)

    conflict_note = ""
    if conflict and conflict.target.split(".")[0] == "route_agent":
        conflict_note = f"\n注意：核验发现了问题需要你重新调整——{conflict.reason}"

    # 用entry_target而不是last_agent_result is None判断：原理跟lifestyle_agent_node/
    # pitfall_agent_node完全一致（见docs/ISSUES.md第28/29条）——entry_target在整个edit
    # 线程内不会变，才是"用户是不是直接点名route_agent"的稳定信号，"route_draft"是
    # graph节点名（跟_NODE_FUNCS注册名一致），不是"route_draft_node"。
    # 基线固定性原理跟lifestyle_agent_node完全一致：必须是"这次edit开始前"的原始快照，
    # 不能是"上一轮重试后"的快照，否则第1轮误改后被打回，第2轮只要保持第1轮的（已经错误的）
    # 结果不变，diff检测会认为"没有变化"而放行。
    if is_direct_edit_target:
        original_snapshot = state.get("edit_original_route_plan")
        if original_snapshot is None:
            original_snapshot = store.read_workspace(project_id, "route_plan.json")
    else:
        original_snapshot = None

    if revision:
        constraint_note = (
            f"本次明确更新的硬约束：{'、'.join(revision_constraint_changes)}。"
            if revision_constraint_changes
            else "原有天数和必去地点硬约束继续有效。"
        )
        prompt = (
            f"用户要求调整骨架：{revision}。{constraint_note}"
            f"请基于当前route_plan.json做调整后重新写入。"
        )
    elif is_direct_edit_target:
        prompt = (
            f"用户提出的具体修改要求：{state['edit_request']}\n"
            f"请判断这个要求具体涉及哪一天（day）。如果只是修改某一天已有的内容，"
            f"调用write_route_plan_patch时只需要提交你新生成/修改的那几天——"
            f"未提交的天数不会被这次调用影响，会自动保留原样，不要重新排序。"
            f"如果用户明确要求「加一天」/「减少一天」这类涉及增删/重排天数的结构性变化，"
            f"改用write_route_plan（整份覆盖，需要传完整days数组）。"
            f"如果需要参考现有路线规划的具体内容，可以用read_workspace工具查询route_plan.json。"
            f"{conflict_note}"
        )
    else:
        prompt = (
            f"用户需求：目的地={req['destination']}，天数={req['days']}天，"
            f"风格={req.get('style', '不限')}，预算={req.get('budget', '不限')}，"
            f"备注={req.get('notes', '无')}，出发地={req.get('origin', '未告知')}，"
            f"交通方式偏好={req.get('transport', '不限')}，"
            f"必去地点={_format_must_visit_for_prompt(req.get('must_visit'))}，"
            f"风格解读={_format_style_interpretation_for_prompt(req.get('style_interpretation'))}。"
            f"请检索素材库、结合高德MCP做校验，"
            f"产出几天几夜的行程骨架（每天的city/area/pois/notes/claims；sources由claims自动汇总），"
            f"写入路线规划区。{conflict_note}"
        )
    _invoke_with_write_verification(
        agent,
        prompt,
        verify_fn=lambda: bool((store.read_workspace(project_id, "route_plan.json") or {}).get("days")),
        retry_note="你刚才的回复没有真正调用write_route_plan/write_route_plan_patch工具写入路线规划区，"
        "只有文字总结是不够的。请现在立刻调用其中一个工具完成写入。",
        thread_id=route_thread_id,
        resume_from_checkpoint=route_checkpoint_pending,
        checkpoint_repair_write_tools=("write_route_plan", "write_route_plan_patch"),
    )

    # 骨架确认前的硬约束确定性校验：首次生成和每次revision都必须满足用户要求的天数结构、
    # 每天基本内容和must_visit。完成后的主动edit可能明确改变旧要求，不套用旧N/must_visit，
    # 但下方仍会做不依赖旧要求的通用结构合法性检查。
    hard_constraints_blocked = False
    if not is_direct_edit_target:
        last_record_id = None
        for attempt in range(MAX_RETRY + 1):
            current_plan = store.read_workspace(project_id, "route_plan.json")
            constraint_issues = _route_hard_constraint_issues(req, current_plan)
            if not constraint_issues:
                if last_record_id:
                    store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
                break

            issue_desc = _format_route_constraint_issues(constraint_issues)
            if last_record_id:
                store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
            record = store.report_conflict(
                project_id,
                actor="citation_checker",
                target="route_agent",
                reason=f"路线骨架硬约束未通过（共{len(constraint_issues)}处）：{issue_desc}",
                issues=constraint_issues,
                issue_type="route_hard_constraint",
            )
            last_record_id = record.id

            if attempt == MAX_RETRY:
                hard_constraints_blocked = True
                break

            fix_prompt = (
                f"你刚才生成的路线骨架没有满足确认前必须通过的硬约束：{issue_desc}。"
                f"用户要求总天数={req.get('days')}，必去地点="
                f"{_format_must_visit_for_prompt(req.get('must_visit') or [])}。"
                f"请修正后重新写入。如果涉及天数增删/重排，调用write_route_plan提交完整days；"
                f"如果只是补齐某一天内容或必去地点，调用write_route_plan_patch提交受影响的天。"
            )
            invoke_with_retry(agent, [{"role": "user", "content": fix_prompt}])

    if is_direct_edit_target:
        after_snapshot = store.read_workspace(project_id, "route_plan.json")
        generic_structure_issues = validate_route_structure(after_snapshot)
        if generic_structure_issues and original_snapshot:
            store.write_route_plan(
                project_id,
                actor="route_agent",
                route_plan=RoutePlan.model_validate(original_snapshot),
            )
            after_snapshot = original_snapshot
            store.report_conflict(
                project_id,
                actor="citation_checker",
                target="route_agent",
                reason=f"edit模式写出了非法路线结构，已回滚："
                f"{_format_route_constraint_issues(generic_structure_issues)}",
                issues=generic_structure_issues,
                issue_type="route_structure_invalid",
            )
        changed = _changed_route_days(original_snapshot, after_snapshot)
        day_candidates = {d.get("day") for d in (after_snapshot or {}).get("days", []) if d.get("day") is not None}
        relevant_conflict_target = conflict.target if conflict and conflict.target.split(".")[0] == "route_agent" else None
        if changed and _is_edit_overreach(state["edit_request"], changed, day_candidates, relevant_conflict_target):
            # 用户的修改要求可能天然涉及多天，静态阈值1不参考请求内容会误判——改用
            # _infer_edit_scope动态判断这次请求实际涵盖的天数范围，超出这个范围才算可疑越权
            # （docs/ARCHITECTURE.md第20.2节）。LLM判断失败时_is_edit_overreach内部降级为
            # 原有的">1即可疑"静态阈值判断。不阻塞流程，走既有的report_conflict留痕机制，
            # 交给下游整合环节的复核循环处理。
            store.report_conflict(
                project_id,
                actor="citation_checker",
                target=f"route_agent.day{changed[0]}",
                reason=f"edit模式下用户只要求「{state['edit_request']}」，但实际改动了day{changed}这"
                f"{len(changed)}天，可能误改了未涉及的部分，需要复核是否应该只保留其中一天的改动。",
                issue_type="route_edit_overreach",
            )

    result: dict = {
        "route_plan_drafted": True,
        "revision_request": None,
        "force_finalize": state.get("force_finalize", False),
        "route_hard_constraints_blocked": hard_constraints_blocked,
        "last_agent_result": {"agent": "route_agent", "summary": "路线骨架已生成"},
    }
    if req != state.get("requirements"):
        result["requirements"] = req
    if is_direct_edit_target:
        # 固定在state里，供同一edit线程内的后续重试沿用同一条基线（原理同lifestyle_agent_node）。
        result["edit_original_route_plan"] = original_snapshot
        # 供route_citation_check_node收窄核验范围，只核验这次真的改动的天数。
        result["edit_changed_route_days"] = changed
    if conflict and conflict.target.split(".")[0] == "route_agent":
        # 因整合agent的冲突被打回修复的情况，需要先回integration复核，见_route_integration_phase。
        # 注意：这条路径不会重新触发route_citation_check_node——这时progress.state已经是
        # INTEGRATING，_route_plan_mode不会再走PARALLEL_RESEARCH分支去检查route_citation_checked。
        # 这和lifestyle_agent_node被integration打回时同样不会重新触发citation_check_node是一致的
        # 设计：自动核验只在初次PARALLEL_RESEARCH阶段跑一次，integration自己的冲突走独立的
        # 打回-复核循环，不会绕回自动核验节点。
        result["awaiting_integration_recheck"] = True
        result["integration_retry_count"] = state.get("integration_retry_count", 0) + 1
        # docs/ARCHITECTURE.md第5.1节场景13"打回时要重置下游done标志"这条原则此前只在
        # lifestyle_agent_node被打回时实现了（重置pitfall_agent_done），route_agent这条
        # 路径一直遗漏——route骨架重新生成后（比如某天的city/area变了），lifestyle_agent
        # 的既有推荐可能已经跟新路线对不上，理应重新检索/调整一遍，而不是让
        # lifestyle_agent_done停留在True、被路由函数直接跳过（见docs/ISSUES.md第32条
        # 已知局限②，这次真实排查后确认并补上，不是本条修复顺带臆测的新问题）。
        # pitfall_agent_done一并重置是因为lifestyle_agent_node自己被重新触发时也会做
        # 同样的重置，这里只是让route这条路径提前生效，避免过期的避雷内容留在原地。
        result["lifestyle_agent_done"] = False
        result["pitfall_agent_done"] = False
        result["pitfall_citation_checked"] = False
    return result


def await_skeleton_confirm_node(state: GraphState) -> dict:
    """只做只读操作 + interrupt()，不能在这里放有副作用的代码（resume时会重跑）。

    无法识别的回复（既不是"confirm"也不是"revise:"开头，如手滑打错字、说了句无关的话）
    不能被静默当成确认通过，会重新interrupt()追问，直到收到能识别的回复——见
    docs/ARCHITECTURE.md第19.1节。追问事件写入changelog（不是需要复核的冲突，是用户
    交互层面的重试，不走report_conflict/conflicts.jsonl）。"""
    project_id = state["project_id"]
    route_plan = store.read_workspace(project_id, "route_plan.json")
    hard_issues = _route_hard_constraint_issues(state.get("requirements") or {}, route_plan)
    can_confirm = not hard_issues
    if can_confirm:
        question = "骨架已生成，是否确认？回复 confirm 确认，或 revise:<调整意见> 要求调整"
    else:
        question = (
            f"骨架仍有不能确认的硬约束问题：{_format_route_constraint_issues(hard_issues)}。"
            f"请回复 revise:<调整意见> 继续修正；当前不能 confirm。"
        )

    while True:
        decision = interrupt(
            {
                "question": question,
                "route_plan": route_plan,
                "can_confirm": can_confirm,
                "hard_constraint_issues": hard_issues,
            }
        )

        if decision == "confirm" and can_confirm:
            break
        if isinstance(decision, str) and decision.startswith("revise:"):
            return {
                "revision_request": decision[len("revise:") :],
                "route_hard_constraints_blocked": False,
            }

        store.log_event(
            project_id,
            actor="main_agent",
            action="skeleton_confirm_reprompt",
            target="progress.json",
            summary=f"未识别的骨架确认回复，重新追问：{decision!r}",
        )
        question = (
            "当前骨架仍不满足硬约束，请回复revise:您的调整意见"
            if not can_confirm
            else "没有识别到您的意图，请回复confirm确认，或revise:您的调整意见"
        )

    store.update_progress(
        project_id, actor="main_agent", state=ProjectPhase.PARALLEL_RESEARCH, skeleton_confirmed=True
    )
    return {
        "route_agent_done": True,
        "route_hard_constraints_blocked": False,
        "last_agent_result": {"agent": "route_agent", "summary": "骨架已确认"},
    }


def await_route_structure_confirm_node(state: GraphState) -> dict:
    """Confirm a post-skeleton structural candidate before it can replace the route."""
    project_id = state["project_id"]
    proposal = store.read_route_change_proposal(project_id)
    if not proposal or proposal.get("status") != "pending":
        return {
            "pending_route_structure_confirmation": False,
            "route_recheck_days": None,
        }
    proposal_work_unit_id = str(proposal.get("work_unit_id") or "")
    if proposal_work_unit_id:
        clear_agent_thread_family(
            project_id,
            _route_structure_proposal_thread_id(project_id, proposal_work_unit_id),
        )

    structure_diff = route_structure_diff(
        proposal.get("base_structure"), proposal.get("candidate_route_plan")
    )
    question = (
        "核验发现原路线可能需要调整POI、顺序或所属天。请回复confirm应用候选，"
        "keep保留原路线，或revise:<意见>调整候选。"
    )
    while True:
        decision = interrupt(
            {
                "question": question,
                "route_change_proposal": proposal,
                "structure_diff": structure_diff,
            }
        )
        if decision in {"confirm", "keep"}:
            break
        if isinstance(decision, str) and decision.startswith("revise:"):
            feedback = decision[len("revise:") :].strip()
            if not feedback:
                store.log_event(
                    project_id,
                    actor="main_agent",
                    action="route_structure_confirm_reprompt",
                    target="route_change_proposal.json",
                    summary="收到空的revise请求，未改变候选并重新询问",
                )
                question = "请在revise:后写明希望怎样调整，或回复confirm、keep"
                continue
            return {
                # The interrupt node records the explicit user choice and exits.
                # route_draft performs the fallible proposal work as a separate,
                # checkpointed node so a crash cannot turn this into a false terminal.
                "pending_route_structure_confirmation": False,
                "route_structure_revision_request": feedback,
                "route_structure_revision_context": {
                    "source_proposal_id": proposal.get("proposal_id"),
                    "trigger": proposal.get("trigger") or "post_confirmation_check",
                    "reason": proposal.get("reason") or "路线结构需要调整",
                    "evidence": proposal.get("evidence") or "",
                    "affected_days": [
                        int(item["day"])
                        for item in (proposal.get("proposed_days") or [])
                        if isinstance(item, dict) and item.get("day") is not None
                    ],
                },
                "last_agent_result": {
                    "agent": "route_agent",
                    "summary": "已记录用户对路线结构候选的调整意见，准备生成新版候选",
                },
            }
        store.log_event(
            project_id,
            actor="main_agent",
            action="route_structure_confirm_reprompt",
            target="route_change_proposal.json",
            summary=f"未识别的路线结构确认回复：{decision!r}",
        )
        question = "没有识别到您的决定，请回复confirm、keep或revise:您的调整意见"

    resume_check = state.get("route_structure_resume_check") or proposal.get("trigger")
    proposal_trigger = proposal.get("trigger")
    conflict = store.get_pending_conflict(project_id)
    affected_days = sorted(
        {
            item.get("day")
            for item in proposal.get("proposed_days", [])
            if item.get("day") is not None
        }
    )

    if decision == "confirm":
        route_before_apply = store.read_workspace(project_id, "route_plan.json")
        store.decide_route_change_proposal(project_id, actor="main_agent", decision="apply")
        if conflict and conflict.target.split(".")[0] == "route_agent":
            store.resolve_conflict(project_id, conflict.id, actor="main_agent")
        result = {
            "pending_route_structure_confirmation": False,
            "route_structure_revision_request": None,
            "route_recheck_days": affected_days,
            "route_citation_checked": False,
            "route_map_checked": False,
            "route_timeliness_checked": False,
            "lifestyle_agent_done": False,
            "pitfall_agent_done": False,
            "pitfall_citation_checked": False,
            "poi_reconciliation_original_route_plan": route_before_apply,
            "last_agent_result": {"agent": "route_agent", "summary": "用户已确认路线结构调整"},
        }
        if resume_check == "integration":
            result["awaiting_integration_recheck"] = True
        return result

    # keep表示用户拒绝整个候选，而不仅是候选里的POI数组。风险事实继续留在
    # route_feasibility并由确定性Join投影；候选notes/claims也可能包含“已移除/已替换”
    # 等结构动作，不能绕过确认写回正式route_plan。
    store.decide_route_change_proposal(project_id, actor="main_agent", decision="keep")
    if proposal_trigger == "timeliness":
        feasibility = store.read_route_feasibility(project_id)
        if feasibility:
            feasibility.force_finalized = True
            store.write_route_feasibility(project_id, "citation_checker", feasibility)
    if conflict and conflict.target.split(".")[0] == "route_agent":
        store.mark_force_finalize(project_id)
    return {
        "pending_route_structure_confirmation": False,
        "route_structure_resume_check": None,
        "route_structure_revision_request": None,
        "route_recheck_days": None,
        "route_timeliness_checked": proposal_trigger == "timeliness" or state.get("route_timeliness_checked", False),
        "awaiting_integration_recheck": (
            resume_check == "integration" and state.get("awaiting_integration_recheck", False)
        ),
        "force_finalize": True,
        "last_agent_result": {
            "agent": "citation_checker",
            "summary": "用户保留原路线，结构候选未应用",
            "source": "route_timeliness" if proposal_trigger == "timeliness" else "route_structure",
        },
    }


def route_citation_check_node(state: GraphState) -> dict:
    """系统级校验，核实route_plan.json每天POI标的chunk_id是不是真的支持内容。
    结构和citation_check_node（衣食住行的核验）完全对称，见src/materials/citation_check.py，
    区别只是核验的是route_plan.json而不是outputs/lifestyle.json。

    放在骨架确认之后、lifestyle_agent执行之前——route核验完了lifestyle才有可靠的路线做参考。

    edit模式下只核验这次编辑实际改动的天数（edit_changed_route_days），未改动天数不重新核验；
    如果用户随后确认了结构proposal，则优先使用proposal实际改动的route_recheck_days，避免
    沿用旧edit范围而漏查新改动天。
    避免非确定性判断把此前已通过核验的内容误判掉（跟citation_check_node/pitfall_citation_check_node
    的fields参数同一个思路，见docs/ISSUES.md第37补条）。plan模式或edit模式下该字段未设置
    （如级联整体重新生成）时，days为None，即全量核验，行为跟修复前一致。

    days为None（全量核验）时，用store.get_latest_unresolved_conflict兜底初始化
    last_record_id——不这样做的话，如果route_agent在这次node调用之前已经因为MAX_RETRY
    耗尽被force_finalize过一次、后来又被重新触发重写（比如下游冲突级联），这次调用的
    last_record_id会从None开始，即使这次核验全部通过，也不知道要去resolve上一次调用
    遗留的那条force_finalized记录，导致conflicts.jsonl永久留着一条过时的记录（见
    docs/ISSUES.md第138条）。edit模式（days非None）不做这个兜底——上一次遗留的记录
    可能覆盖跟这次收窄范围不同的天数，贸然继承有误伤风险，见get_latest_unresolved_conflict
    的说明。
    """
    project_id, topic = state["project_id"], state["topic"]
    _assert_formal_comparison_baseline(project_id, topic)
    force_flag = state.get("force_finalize", False)  # 粘性标志，见citation_check_node同款注释
    days = state.get("route_recheck_days")
    if days is None and state.get("mode") == "edit":
        days = state.get("edit_changed_route_days")
    last_record_id = None
    if days is None:
        stale = store.get_latest_unresolved_conflict(project_id, "route_agent", reason_prefix="路线引用核验未通过")
        if stale:
            last_record_id = stale.id

    for attempt in range(MAX_RETRY + 1):
        issues = citation_check.verify_route_citations(project_id, topic, days=days)

        if not issues:
            if last_record_id:
                store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
            break

        summary = "；".join(
            (
                f"Day{i['day']}事实「{i.get('claim_text', '')}」：{i['reason']}"
                if i.get("claim_id")
                else f"Day{i['day']}「{i.get('city','')}{i.get('area','')}」缺失专有名词"
                f"{i['missing_entities']}：{i['reason']}"
            )
            for i in issues
        )
        if last_record_id:
            # 上一轮的record已经被这次重新核验取代——不论上一轮指出的问题这次是否已修复，
            # 都不该让它继续以pending状态存在（见下面mark_force_finalize分支的说明）。
            store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
        record = store.report_conflict(
            project_id,
            actor="citation_checker",
            target="route_agent",
            reason=f"路线引用核验未通过（共{len(issues)}处）：{summary}",
            issues=issues,
        )
        last_record_id = record.id

        if attempt == MAX_RETRY:
            indexed = _attempt_route_gap_supplement(project_id, topic, issues)
            if indexed:
                repair_days = sorted({i["day"] for i in issues})
                agent = build_route_agent(
                    project_id,
                    topic,
                    checkpointer=False,
                    write_mode="facts_only",
                    convergence_work_unit=(
                        "route_gap_supplement_days_"
                        + "_".join(str(day) for day in repair_days)
                    ),
                )
                set_trace_context(project_id, "route_agent")
                repair_prompt = (
                    f"系统刚为当前素材库新增了{len(indexed)}条已经完成入库、现在可被"
                    f"retrieve_materials检索到的素材。请只回炉修正Day{repair_days}，针对"
                    f"核验缺失项重新检索新素材并更新sources；其他天不要改。修正后逐天调用"
                    f"write_route_facts_patch，只提交notes/sources/claims。已经启用claims的天修改notes时"
                    f"必须同步提交完整claims。问题清单：{summary}"
                )
                try:
                    invoke_with_retry(agent, [{"role": "user", "content": repair_prompt}])
                    store.increment_retry_count(project_id, record.id)
                    repaired_issues = citation_check.verify_route_citations(
                        project_id, topic, days=repair_days
                    )
                except Exception as exc:
                    repaired_issues = [
                        {**issue, "reason": f"补采后局部回炉执行失败：{exc}"} for issue in issues
                    ]
                if not repaired_issues:
                    store.resolve_conflict(project_id, record.id, actor="citation_checker")
                    break
                store.resolve_conflict(project_id, record.id, actor="citation_checker")
                repaired_summary = "；".join(
                    (
                        f"Day{i['day']}事实「{i.get('claim_text', '')}」：{i['reason']}"
                        if i.get("claim_id")
                        else f"Day{i['day']}「{i.get('city','')}{i.get('area','')}」缺失专有名词"
                        f"{i['missing_entities']}：{i['reason']}"
                    )
                    for i in repaired_issues
                )
                record = store.report_conflict(
                    project_id,
                    actor="citation_checker",
                    target="route_agent",
                    reason=f"补采后局部回炉仍未通过（共{len(repaired_issues)}处）：{repaired_summary}",
                    issues=repaired_issues,
                )
                last_record_id = record.id
                issues = repaired_issues
            missing_by_day: dict[int, list[str]] = {}
            for i in issues:
                missing_by_day.setdefault(i["day"], []).extend(i.get("missing_entities") or [])
            store.mark_route_days_force_finalized(
                project_id, sorted({i["day"] for i in issues}), missing_entities_by_day=missing_by_day
            )
            store.mark_force_finalize(project_id)
            force_flag = True
            break

        issues_by_day: dict[int, list[dict]] = {}
        for issue in issues:
            issues_by_day.setdefault(int(issue["day"]), []).append(issue)

        # 一次混入多天问题会迫使弱模型反复read_workspace，再在长上下文里同时维护多份
        # 完整claims。真实峨眉山3天运行中，18个问题一次下发后产生30余次调用和连续读取，
        # 始终没有调用write_route_facts_patch。这里沿用既有Day业务边界顺序执行同一角色，
        # 每个工作单元直接携带当前Day完整事实底稿；不增加Agent、不限制检索，也不改变
        # retry/force-finalized语义，只让输入范围与单次patch的输出契约一致。
        for repair_day in sorted(issues_by_day):
            day_issues = issues_by_day[repair_day]
            day_summary = "；".join(
                (
                    f"Day{i['day']}事实「{i.get('claim_text', '')}」：{i['reason']}"
                    if i.get("claim_id")
                    else f"Day{i['day']}「{i.get('city','')}{i.get('area','')}」缺失专有名词"
                    f"{i['missing_entities']}：{i['reason']}"
                )
                for i in day_issues
            )
            route_plan = store.read_workspace(project_id, "route_plan.json") or {}
            current_day = next(
                (
                    item
                    for item in route_plan.get("days", [])
                    if int(item.get("day") or 0) == repair_day
                ),
                {},
            )
            claim_fields = (
                "target_path",
                "subject",
                "claim_text",
                "claim_type",
                "support_mode",
                "claimed_entities",
                "evidence_refs",
                "derived_from",
                "provenance_mode",
            )
            day_facts = {
                "day": current_day.get("day"),
                "notes": current_day.get("notes", ""),
                "pois": current_day.get("pois", []),
                # claim_id和evidence_snapshots是写入后的自动派生字段，模型既不能
                # 重用它们，也不需要在完整claims重提时回传。返修快照只保留
                # 模型拥有的字段，避免证据摘录在每一轮重复占用上下文。
                "claims": [
                    {field: claim[field] for field in claim_fields if field in claim}
                    for claim in current_day.get("claims", [])
                ],
            }
            # 这是一次只依赖当前Day事实底稿和问题清单的窄返修。显式False避免嵌套
            # LangGraph把外层route_draft的长研究checkpoint继承进来；None在这里不是
            # 无状态语义。持久事实仍由Workspace承接，旧工具对话不属于本工作单元输入。
            agent = build_route_agent(
                project_id,
                topic,
                checkpointer=False,
                write_mode="facts_only",
                convergence_work_unit=f"route_facts_day_{repair_day}",
            )
            set_trace_context(project_id, "route_agent")
            fix_prompt = (
                f"自动核验发现Day{repair_day}的部分路线事实引用不支持当前说法。"
                f"本工作单元只处理Day{repair_day}，不要修改其他天。问题清单：{day_summary}\n"
                f"以下是Day{repair_day}当前完整事实底稿，已经直接提供，不需要反复read_workspace：\n"
                f"{json.dumps(day_facts, ensure_ascii=False)}\n"
                f"请先逐条判断失败原因，再决定是否需要搜索，返修不等于搜索："
                f"(1)证据对象或片段绑定错误，优先换用现有正确证据；没有正确证据就收窄或降级；"
                f"(2)一个claim混合了事实与建议，拆开或只保留证据支持的部分；"
                f"(3)路线顺序、适合人群、是否串联、是否需要深度规划等编排判断，改用derived或estimate，"
                f"不需要联网证明；(4)只有某项具体硬事实确实影响攻略价值、必须保留且现有证据不足时，"
                f"才针对该事实继续检索。不要借返修增加问题清单之外的新数字、政策、班次或景点事实，"
                f"未失败的内容尽量原样保留。处理完本日全部问题后统一写入一次。"
                f"{_missing_entities_retry_hint(day_issues)}"
                f"{_findings_retry_hint(project_id, 'route_agent')}"
                f"完成后调用一次write_route_facts_patch提交Day{repair_day}的notes和完整claims。"
                f"只能更新notes/sources/claims，不得修改POI、顺序或天数结构。"
            )
            before_facts = _route_facts_snapshot(project_id, [repair_day])
            try:
                _invoke_with_write_verification(
                    agent,
                    fix_prompt,
                    verify_fn=lambda repair_day=repair_day, before_facts=before_facts: (
                        _route_facts_snapshot(project_id, [repair_day]) != before_facts
                    ),
                    retry_note=(
                        f"你刚才没有真正写入Day{repair_day}的路线事实修复；文字说明或准备继续处理"
                        "都不算完成。请根据已经给出的本日底稿和问题清单，完成必要的换证据、拆分、"
                        "收窄或降级，然后调用write_route_facts_patch写入本日完整notes和claims。"
                    ),
                )
            except (
                ContextBudgetExceededError,
                GraphRecursionError,
            ):
                # ReAct在工具已成功写入后还会再请求一次最终回复。如果只是
                # 这个收尾请求超出上下文或递归边界，Workspace中的有效修复不应
                # 被误判为整个工作单元失败。只在事实快照确实发生变化时接受；
                # 未写入的真实异常仍原样抛出。
                if _route_facts_snapshot(project_id, [repair_day]) == before_facts:
                    raise
        store.increment_retry_count(project_id, record.id)

    result = {
        "route_citation_checked": True,
        "force_finalize": force_flag,
        # source字段是给_route_edit_mode的"citation_checker"消歧义用的显式标记（见该函数注释）——
        # 加入route后pitfall_citation_checked/edit_citation_checked这两个flag的执行顺序假设
        # 不再成立（route→lifestyle级联场景下route_citation_checked会先于lifestyle自己的
        # citation_check_node跑完之前就是True），必须靠显式来源标记而不是flag顺序推断。
        "last_agent_result": {"agent": "citation_checker", "summary": "路线引用核验完成", "source": "route"},
    }
    if state.get("route_citation_after_timeliness"):
        result["route_recheck_days"] = None
        result["route_citation_after_timeliness"] = False
    return result


def _build_route_feasibility_fix_prompt(checks: list[dict]) -> str:
    """把可执行性 warning 转成仅允许修改 notes/sources/claims 的事实层任务。"""
    blocks = []
    for check in checks:
        evidence = "\n".join(
            f"- {item.get('title') or item.get('url') or '查证证据'}：{item.get('excerpt', '')}"
            for item in check.get("evidence", [])
        ) or "- 暂无可直接引用的证据摘要，请再检索核实"
        blocks.append(
            f"Day{check.get('day')} / {check.get('target')} / {check.get('category')}："
            f"{check.get('summary')}\n建议动作：{check.get('suggested_action') or '补充必要提醒'}\n"
            f"证据：\n{evidence}"
        )
    return (
        "路线可执行性核验发现以下不需要改动POI结构、但必须补入路线事实层的提醒：\n\n"
        + "\n\n".join(blocks)
        + "\n\n请只修改涉及天的notes/sources/claims：写清预约、限流、资质、开放时间、"
        "最晚入园、道路或季节风险及对应操作。不得替换、移动或删除POI，不得改变天数结构。"
        "不得通过删除原有时间线、道路编号或预约描述来让检查目标消失；要补充或纠正事实。"
        "checker证据摘要可供你判断和措辞，但sources只能标注你本轮用web_search或素材检索实际获得的来源；"
        "不得把checker摘要伪装成你自己检索到的chunk。"
        "若该天已有claims，必须同步更新完整claims，让新增或改正的事实绑定你本轮实际获得的证据。"
        "修正后逐天调用write_route_facts_patch完成写入。"
    )


def _route_feasibility_unknown_is_technical(check: dict) -> bool:
    return bool(
        str(check.get("technical_error") or "").strip()
        or str(check.get("technical_error_class") or "").strip()
        or check.get("stream_completed") is False
    )


def _route_facts_snapshot(project_id: str, days: list[int]) -> dict[int, dict]:
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    targets = set(days)
    return {
        item.get("day"): {
            "notes": item.get("notes", ""),
            "sources": item.get("sources", []),
            "claims": item.get("claims", []),
        }
        for item in route_plan.get("days", [])
        if item.get("day") in targets
    }


def _route_map_artifact_sha(project_id: str, route_plan: dict) -> str:
    artifact = {
        "route_plan": route_plan,
        "route_entities": store.read_workspace(project_id, "route_entities.json") or {},
        "route_execution": store.read_workspace(project_id, "route_execution.json") or {},
    }
    payload = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def route_map_check_node(state: GraphState) -> dict:
    """Run the deterministic entity and adjacent-leg map gate before Lifestyle.

    Map findings never mutate the confirmed route and do not ask a model to
    rewrite it. A current report is reused when the route structure has not
    changed, so facts-only repairs do not repeat Amap calls.
    """
    project_id, topic = state["project_id"], state["topic"]
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    current_route_sha = route_structure_hash(route_plan)
    existing = store.read_workspace(project_id, "route_map_validation.json") or {}
    if existing.get("route_plan_structure_hash") == current_route_sha:
        report = existing
        reused = True
    else:
        report = route_map_check.verify_route_map(project_id, topic, route_plan)
        reused = False

    map_status = str(report.get("status") or "unknown")
    gate_status = "passed" if map_status == "passed" else "warning"
    store.append_validation_run(
        project_id,
        {
            "stage": "route",
            "scope": "checker:map",
            "observation_mode": "live",
            "status": "passed" if map_status == "passed" else "needs_repair",
            "artifact_sha256": _route_map_artifact_sha(project_id, route_plan),
            "route_structure_sha256": current_route_sha,
            "issue_codes": [
                str(issue.get("code") or "")
                for issue in report.get("issues") or []
                if isinstance(issue, dict)
            ],
            "route_checks": ["map"],
            "route_gate": {
                "status": gate_status,
                "requested_checks": ["map"],
                "component_statuses": {"route_map": map_status},
                "unresolved_check_ids": [],
            },
            "source": "langgraph",
            "reused_current_report": reused,
        },
    )
    return {
        "route_map_checked": True,
        "force_finalize": state.get("force_finalize", False),
        "last_agent_result": {
            "agent": "citation_checker",
            "summary": (
                "路线地图核验通过"
                if map_status == "passed"
                else f"路线地图核验保留{len(report.get('issues') or [])}项风险"
            ),
            "source": "route_map",
        },
    }


def route_timeliness_check_node(state: GraphState) -> dict:
    """路线可执行性闸门；保留旧节点名以兼容已持久化的图状态。"""
    project_id, topic = state["project_id"], state["topic"]
    force_flag = state.get("force_finalize", False)  # 粘性标志，见 citation_check_node 同款注释
    days = None
    if state.get("mode") == "edit":
        days = state.get("route_recheck_days")
        if days is None:
            days = state.get("edit_changed_route_days")
    last_record_id = None
    if days is None:
        stale = store.get_latest_unresolved_conflict(project_id, "route_agent", reason_prefix="路线可执行性核验")
        if stale is None:
            stale = store.get_latest_unresolved_conflict(
                project_id, "route_agent", reason_prefix="路线时效核验未通过"
            )
        if stale:
            last_record_id = stale.id

    requirements = state.get("requirements") or {}
    if not requirements:
        saved_requirements = store.read_requirements(project_id) or {}
        requirements = saved_requirements.get("requirements") or saved_requirements

    active_check_ids: Optional[list[str]] = None
    scoped_days = set(days or []) if days is not None else None
    business_unknowns: list[dict] = []
    repaired_route_days: set[int] = set()
    for attempt in range(MAX_RETRY + 1):
        verify_kwargs = {"requirements": requirements, "days": days}
        if active_check_ids is not None:
            verify_kwargs["check_ids"] = active_check_ids
        report = route_feasibility_check.verify_route_feasibility(project_id, topic, **verify_kwargs)
        all_checks = [check.model_dump(mode="json") for check in report.checks]
        business_unknowns = [
            check
            for check in all_checks
            if check["status"] == "unknown"
            and not str(check.get("technical_error") or "").strip()
            and not str(check.get("technical_error_class") or "").strip()
        ]
        checks = all_checks
        if active_check_ids is not None:
            active_ids = set(active_check_ids)
            checks = [check for check in checks if check["check_id"] in active_ids]
        elif scoped_days is not None:
            checks = [check for check in checks if check["day"] in scoped_days]
        blocked = [check for check in checks if check["status"] == "blocked"]
        warnings = [check for check in checks if check["status"] == "warning"]
        unknowns = [check for check in checks if check["status"] == "unknown"]
        technical_unknowns = [
            check
            for check in unknowns
            if _route_feasibility_unknown_is_technical(check)
        ]

        if not blocked and not warnings and not technical_unknowns:
            if last_record_id:
                store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
            break

        if blocked:
            summary = "；".join(
                f"Day{i['day']}「{i['target']}」：{i.get('summary')}" for i in blocked
            )
            if last_record_id:
                store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
            record = store.report_conflict(
                project_id,
                actor="citation_checker",
                target="route_agent",
                reason=f"路线可执行性核验要求结构调整（共{len(blocked)}处）：{summary}",
                issues=blocked,
                issue_type="route_feasibility_blocked",
            )
            evidence_lines = []
            for check in blocked:
                evidence_lines.extend(
                    item.get("excerpt") or item.get("url") or "" for item in check.get("evidence", [])
                )
            proposal = _request_route_structure_proposal(
                project_id,
                topic,
                trigger="timeliness",
                reason=record.reason,
                evidence="\n\n".join(line for line in evidence_lines if line),
                affected_days=sorted({int(check["day"]) for check in blocked}),
                source_identity=(
                    "route_feasibility:"
                    + hashlib.sha256(
                        json.dumps(
                            sorted({str(check["check_id"]) for check in blocked}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()[:20]
                ),
            )
            return {
                "pending_route_structure_confirmation": True,
                "route_structure_resume_check": (
                    "integration"
                    if state.get("route_structure_resume_check") == "integration"
                    else "timeliness"
                ),
                "route_timeliness_checked": False,
                "last_agent_result": {
                    "agent": "route_agent",
                    "summary": f"可执行性核验生成待确认结构候选 {proposal['proposal_id']}",
                },
            }

        unresolved = warnings + technical_unknowns
        active_check_ids = [check["check_id"] for check in unresolved]
        summary = "；".join(
            f"Day{i['day']}「{i['target']}」[{i['status']}]：{i.get('summary')}" for i in unresolved
        )
        if last_record_id:
            store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
        record = store.report_conflict(
            project_id,
            actor="citation_checker",
            target="route_agent",
            reason=f"路线可执行性核验尚有未解决项（共{len(unresolved)}处）：{summary}",
            issues=unresolved,
            issue_type="route_feasibility_unresolved",
        )
        last_record_id = record.id

        if attempt == MAX_RETRY:
            report.force_finalized = True
            store.write_route_feasibility(project_id, "citation_checker", report)
            store.mark_force_finalize(project_id)
            force_flag = True
            break

        if not warnings:
            store.increment_retry_count(project_id, record.id)
            continue

        agent = build_route_agent(project_id, topic, write_mode="facts_only")
        set_trace_context(project_id, "route_agent")
        fix_prompt = _build_route_feasibility_fix_prompt(warnings)
        repair_days = sorted({i["day"] for i in warnings})
        before_facts = _route_facts_snapshot(project_id, repair_days)
        _invoke_with_write_verification(
            agent,
            fix_prompt,
            verify_fn=lambda: _route_facts_snapshot(project_id, repair_days) != before_facts,
            retry_note="你刚才的回复没有真正调用write_route_facts_patch更新路线事实层，"
            "只有文字总结是不够的。请现在立刻调用该工具完成写入。",
        )
        if _route_facts_snapshot(project_id, repair_days) != before_facts:
            repaired_route_days.update(int(day) for day in repair_days)
        store.increment_retry_count(project_id, record.id)

    result = {
        "route_timeliness_checked": True,
        "route_recheck_days": sorted(repaired_route_days) if repaired_route_days else None,
        "route_citation_after_timeliness": bool(repaired_route_days),
        "force_finalize": force_flag,
        "last_agent_result": {
            "agent": "citation_checker",
            "summary": (
                f"路线可执行性核验完成，保留{len(business_unknowns)}项出发前复核提醒"
                if business_unknowns
                else "路线可执行性核验完成"
            ),
            "source": "route_timeliness",
        },
    }
    if repaired_route_days:
        # Feasibility runs after route citation.  Once it writes new notes/claims,
        # the previous citation verdicts are no longer current; route the touched
        # days back through the normal citation repair loop before downstream work.
        result["route_citation_checked"] = False
    if state.get("route_structure_resume_check") == "integration":
        result["route_structure_resume_check"] = None
    return result


def _changed_lifestyle_fields(before: Optional[dict], after: Optional[dict]) -> list[str]:
    before = before or {}
    after = after or {}
    return [f for f in LIFESTYLE_FIELDS if before.get(f) != after.get(f)]


def _changed_lifestyle_items(before: Optional[dict], after: Optional[dict]) -> list[list]:
    """比_changed_lifestyle_fields更细一档的diff：不是"哪个字段变了"，而是"字段内具体
    哪一天的item变了"。返回[[field, day], ...]（不用tuple——GraphState要能整体JSON序列化，
    tuple在这条路径上会被存进state/持久化，保险起见统一用list）。

    背景（docs/ISSUES.md第37补条已知局限的修复）：字段级diff只能做到"整字段一起核验"，
    用户edit只改了字段里某一天时，同字段其它天完全没被触碰的item仍会被送进
    _verify_items——如果那些item本身字面对不上（走有非确定性的档位B/C判断），有实测
    概率被无端判定为"缺乏依据"而清空，即使这次编辑压根没有涉及它们（真实复现过的机制，
    不是理论假设，见docs/ISSUES.md第37补条这次的补充验证）。item级diff把核验scoping从
    "整字段"收窄到"真正改动的具体天"，缩小这个误伤的风险敞口。

    按day分组比对（不是逐item位置比对）——同一字段内同一天只会有一条item（已核对全部
    真实project数据，没有反例），(field, day)是稳定的天然标识，不需要给schema加id字段。
    day数量本身变化（新增/删除某天）也算作该(field, day)变化，纳入结果。"""
    before = before or {}
    after = after or {}
    changed: list[list] = []
    for f in LIFESTYLE_FIELDS:
        before_by_day = {it.get("day"): it for it in (before.get(f) or {}).get("items", []) or []}
        after_by_day = {it.get("day"): it for it in (after.get(f) or {}).get("items", []) or []}
        for day in sorted((set(before_by_day) | set(after_by_day)), key=lambda d: (d is None, d)):
            if before_by_day.get(day) != after_by_day.get(day):
                changed.append([f, day])
    return changed


def _changed_pitfall_fields(before: Optional[dict], after: Optional[dict]) -> list[str]:
    """字段级diff（food/outfit/lodging/play四个字段），供越权检测使用——一次edit请求
    通常只涉及一个维度，改动超过1个字段视为可疑的跨字段误改，用report_conflict打回复核。
    真实E2E复现过的具体bug：用户只要求补充"杜甫草堂"（play字段）的避雷内容，agent却
    顺带清空了"food"字段里另一条不相关的avoid内容（胡二哥药膳蹄花）——这是跨字段误改，
    字段级diff就能抓到。（跟_changed_pitfall_items是两个独立用途：这个函数管"是否越权
    改动了不该碰的字段"，_changed_pitfall_items管"核验scoping要收窄到哪些具体item"，
    见docs/ISSUES.md第37补条。）"""
    before = before or {}
    after = after or {}
    return [f for f in LIFESTYLE_FIELDS if before.get(f) != after.get(f)]


def _changed_pitfall_items(before: Optional[dict], after: Optional[dict]) -> list[list]:
    """跟_changed_lifestyle_items完全对称，只是数据结构换成PitfallOutput（同样按
    field/day分组，schema一致）。见_changed_lifestyle_items的完整说明。"""
    before = before or {}
    after = after or {}
    changed: list[list] = []
    for f in LIFESTYLE_FIELDS:
        before_by_day = {it.get("day"): it for it in (before.get(f) or {}).get("items", []) or []}
        after_by_day = {it.get("day"): it for it in (after.get(f) or {}).get("items", []) or []}
        for day in sorted((set(before_by_day) | set(after_by_day)), key=lambda d: (d is None, d)):
            if before_by_day.get(day) != after_by_day.get(day):
                changed.append([f, day])
    return changed


def lifestyle_agent_node(state: GraphState) -> dict:
    project_id = state["project_id"]
    _assert_formal_comparison_baseline(project_id, state["topic"])
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    route_map_validation = store.read_workspace(project_id, "route_map_validation.json") or {}
    route_map_context = {
        "days": [
            route_map_day_view(route_plan, route_map_validation, int(item.get("day") or 0))
            for item in route_plan.get("days", [])
            if isinstance(item, dict)
        ]
    }

    conflict_note = ""
    conflict = store.get_pending_conflict(project_id)
    if conflict and conflict.target.split(".")[0] == "lifestyle_agent":
        conflict_note = f"\n注意：整合agent发现了问题需要你重新调整——{conflict.reason}"

    # edit模式下，只有用户直接@的入口agent才按"定点修改"处理——被依赖表级联触发的下游
    # （比如route_agent改了、级联lifestyle_agent重新审视）语义上是"上游变了，你也该整体
    # 重新看一遍"，跟plan模式首次生成是同一诉求，不套edit_request。
    # 用entry_target而不是last_agent_result is None判断：诊断出的一个真实bug——
    # 自我修正重试（本函数下方的字段级diff检测把自己打回给自己）时last_agent_result已经
    # 不是None了，如果用它判断会导致重试那一轮丢失edit_request语境，退化成plan模式全量prompt。
    # entry_target在整个edit线程里不会变，才是"我是不是用户直接点名的对象"的稳定信号。
    # 见docs/ISSUES.md第28/29条。
    is_direct_edit_target = (
        state.get("mode") == "edit"
        and state.get("edit_request")
        and state.get("entry_target") == "lifestyle_agent_node"
    )
    # 基线必须是"这次edit开始前"的原始快照，不能是"上一轮重试后"的快照——否则如果第1轮
    # 就误改了2个字段，report_conflict打回后，第2轮只要保持第1轮的（已经错误的）结果不变，
    # diff检测会认为"没有变化"而放行，永远发现不了第1轮那次误改。用state里持久化的
    # edit_original_lifestyle固定基线，同一个edit线程内的所有重试都对比同一个原始版本。
    if is_direct_edit_target:
        original_snapshot = state.get("edit_original_lifestyle")
        if original_snapshot is None:
            original_snapshot = store.read_workspace(project_id, "outputs/lifestyle.json")
    else:
        original_snapshot = None

    req = state["requirements"]
    if is_direct_edit_target:
        agent = build_lifestyle_agent(
            project_id, state["topic"], checkpointer=get_agent_checkpointer(project_id)
        )
        thread_id = agent_thread_id(project_id, "lifestyle_draft")
        try:
            snapshot = agent.get_state({"configurable": {"thread_id": thread_id}})
            checkpoint_pending = bool(snapshot.values) and bool(snapshot.next)
        except Exception:  # noqa: BLE001 - checkpoint读取失败时退化为一次全新生成
            checkpoint_pending = False
        if not checkpoint_pending:
            reset_web_search_log(project_id, "lifestyle_agent")
            clear_agent_thread(project_id, "lifestyle_draft")
        set_trace_context(project_id, "lifestyle_agent")
        prompt = (
            f"路线规划：{json.dumps(route_plan, ensure_ascii=False)}\n"
            f"路线地图事实（只复用当前路线Hash对应的数据；只有status=passed的相邻路段可作为"
            f"精确距离，anomaly仅用于识别风险且不得引用其数值；已测相邻路段不要重新按名称查询）："
            f"{json.dumps(route_map_context, ensure_ascii=False)}\n"
            f"用户提出的具体修改要求：{state['edit_request']}\n"
            f"请判断这个要求具体涉及food/outfit/lodging/play中的哪个字段、哪一天，"
            f"调用write_agent_output时只需要提交你新生成/修改的那几条——"
            f"未提交的天数或字段不会被这次调用影响，会自动保留原样。"
            f"如果需要参考现有产出的具体措辞，可以用read_workspace工具查询outputs/lifestyle.json。"
            f"{conflict_note}"
        )
        before_updated_at = (
            store.read_workspace(project_id, "outputs/lifestyle.json") or {}
        ).get("updated_at")

        def _lifestyle_written() -> bool:
            data = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
            return bool(data.get("updated_at") and data.get("updated_at") != before_updated_at)

        _invoke_with_write_verification(
            agent,
            prompt,
            verify_fn=_lifestyle_written,
            retry_note="你刚才的回复没有真正调用write_agent_output工具写入产出区，只有文字总结是不够的。"
            "请现在立刻调用write_agent_output工具，只提交本次修改涉及的item。",
            fallback_agent_factory=lambda: build_lifestyle_agent_write_only(project_id, state["topic"]),
            thread_id=thread_id,
            resume_from_checkpoint=checkpoint_pending,
        )
    else:
        route_days = sorted(
            route_plan.get("days") or [{"day": 1, "city": state["topic"], "pois": []}],
            key=lambda item: item.get("day", 0),
        )
        day_runs = []
        turn_id = state.get("turn_id") or ""
        for route_day in route_days:
            day = int(route_day.get("day") or 0)
            suffix = (
                f"{turn_id}:day{day}"
                if turn_id
                else ("" if len(route_days) == 1 else f"day{day}")
            )
            agent = build_lifestyle_agent(
                project_id,
                state["topic"],
                checkpointer=get_agent_checkpointer(project_id),
                convergence_work_unit=f"lifestyle_day_{day}",
            )
            thread_id = agent_thread_id(project_id, "lifestyle_draft", suffix=suffix)
            try:
                snapshot = agent.get_state({"configurable": {"thread_id": thread_id}})
                has_checkpoint = bool(snapshot.values)
                checkpoint_pending = has_checkpoint and bool(snapshot.next)
            except Exception:  # noqa: BLE001 - checkpoint读取失败时退化为一次全新生成
                has_checkpoint = False
                checkpoint_pending = False
            day_runs.append((day, suffix, agent, thread_id, has_checkpoint, checkpoint_pending))

        if not any(has_checkpoint for _, _, _, _, has_checkpoint, _ in day_runs):
            reset_web_search_log(project_id, "lifestyle_agent")

        for day, suffix, agent, thread_id, has_checkpoint, checkpoint_pending in day_runs:
            current = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
            if lifestyle_day_complete(current, day):
                continue
            if not checkpoint_pending:
                clear_agent_thread(project_id, "lifestyle_draft", suffix=suffix)
            set_trace_context(project_id, "lifestyle_agent")
            formal_material_context = ""
            if _is_formal_comparison(project_id):
                day_view = route_day_view(route_plan, day)
                poi_names = [
                    str(poi.get("name") or "")
                    for poi in day_view.get("pois") or []
                    if isinstance(poi, dict)
                ]
                response = research_service.retrieve_context(
                    project_id,
                    actor="lifestyle_agent",
                    stage="lifestyle",
                    target_key=f"lifestyle:day:{day}",
                    query=" ".join([*poi_names, "美食 穿搭 住宿 游玩"]),
                    top_k=8,
                )
                formal_material_context = (
                    "\nA级正式对照的共享素材预检结果："
                    + json.dumps(
                        _compact_research_results(response["results"]),
                        ensure_ascii=False,
                    )
                )
            prompt = (
                f"本工作单元只处理Day{day}，不要生成或修改其他天。\n"
                f"当天路线：{json.dumps(route_day_view(route_plan, day), ensure_ascii=False)}\n"
                f"当天路线地图事实（这是Route Map已核验的唯一精确距离来源；只有status=passed"
                f"的相邻路段可引用精确数值，anomaly仅用于识别风险；已有相邻路段不要再调用高德"
                f"重新按名称查询）："
                f"{json.dumps(route_map_day_view(route_plan, route_map_validation, day), ensure_ascii=False)}\n"
                f"用户原始需求（硬性要求必须真正满足）："
                f"{json.dumps(user_requirement_view(req), ensure_ascii=False)}\n"
                f"请结合素材库检索结果，为Day{day}分别生成food、outfit、lodging、play四条建议，"
                f"最终一次调用write_agent_output提交恰好这四个(field, day={day}) item。"
                f"不得提交其他day。{formal_material_context}"
                f"{_findings_retry_hint(project_id, 'lifestyle_agent')}{conflict_note}"
            )
            before_updated_at = current.get("updated_at")

            def _day_written(day_number=day, previous_updated_at=before_updated_at) -> bool:
                data = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
                return (
                    bool(data.get("updated_at"))
                    and data.get("updated_at") != previous_updated_at
                    and lifestyle_day_complete(data, day_number)
                )

            _invoke_with_write_verification(
                agent,
                prompt,
                verify_fn=_day_written,
                retry_note=(
                    f"你还没有完整写入Day{day}。请立即调用write_agent_output，"
                    f"只提交Day{day}的food/outfit/lodging/play四条item，不要提交其他天。"
                ),
                fallback_agent_factory=lambda: build_lifestyle_agent_write_only(project_id, state["topic"]),
                thread_id=thread_id,
                resume_from_checkpoint=checkpoint_pending,
            )

        final_lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
        missing_days = [
            int(route_day.get("day") or 0)
            for route_day in route_days
            if not lifestyle_day_complete(final_lifestyle, int(route_day.get("day") or 0))
        ]
        if missing_days:
            raise RuntimeError(f"Lifestyle日级工作单元未完整落盘：{missing_days}")

    if is_direct_edit_target:
        after_snapshot = store.read_workspace(project_id, "outputs/lifestyle.json")
        changed = _changed_lifestyle_fields(original_snapshot, after_snapshot)
        relevant_conflict_target = conflict.target if conflict and conflict.target.split(".")[0] == "lifestyle_agent" else None
        if changed and _is_edit_overreach(state["edit_request"], changed, set(LIFESTYLE_FIELDS), relevant_conflict_target):
            # 用户的修改要求可能天然涉及多个维度（如"把Day2的美食和游玩都换一下"），静态阈值1
            # 不参考请求内容会误判——改用_infer_edit_scope动态判断这次请求实际涵盖的字段范围，
            # 超出这个范围才算可疑越权（docs/ARCHITECTURE.md第20.2节）。LLM判断失败时
            # _is_edit_overreach内部降级为原有的">1即可疑"静态阈值判断（真实测试复现过：
            # 旧prompt下模型会把四个字段全部重新生成一遍）。不阻塞流程，走既有的
            # report_conflict留痕机制，交给下游整合环节的复核循环处理。
            store.report_conflict(
                project_id,
                actor="citation_checker",
                target=f"lifestyle_agent.{changed[0]}",
                reason=f"edit模式下用户只要求「{state['edit_request']}」，但实际改动了{changed}这{len(changed)}个字段，"
                f"可能误改了未涉及的部分，需要复核是否应该只保留其中一项改动。",
                issue_type="lifestyle_edit_overreach",
            )

    result: dict = {
        "lifestyle_agent_done": True,
        "last_agent_result": {"agent": "lifestyle_agent", "summary": "衣食住行产出完成"},
    }
    if is_direct_edit_target:
        # 固定在state里，供同一edit线程内的后续重试沿用同一条基线（见上方原理说明）。
        result["edit_original_lifestyle"] = original_snapshot
        # 供citation_check_node收窄核验范围，只核验这次真的改动的字段（见docs/ISSUES.md第37补条）。
        result["edit_changed_lifestyle_fields"] = changed
        # 更细一档：只核验真正改动的具体天（见docs/ISSUES.md第37补条已知局限的修复）。
        result["edit_changed_lifestyle_items"] = _changed_lifestyle_items(original_snapshot, after_snapshot)
    if conflict and conflict.target.split(".")[0] == "lifestyle_agent":
        # 因整合agent的冲突被打回修复的情况，需要先回integration复核，见_route_integration_phase
        result["awaiting_integration_recheck"] = True
        result["integration_retry_count"] = state.get("integration_retry_count", 0) + 1
        # 场景13（docs/ORCHESTRATION.md）：lifestyle的推荐内容变了，避雷agent必须针对新内容
        # 重新检索一遍，不能沿用上一轮（针对旧推荐）的避雷结果——否则避雷内容和最新推荐对不上。
        # 这是依赖表新增lifestyle_agent.downstream=["pitfall_agent"]后第一次真正需要的
        # 下游done标志重置（此前v1阶段lifestyle_agent没有下游，不需要这一步）。
        result["pitfall_agent_done"] = False
        result["pitfall_citation_checked"] = False
    return result


def pitfall_agent_node(state: GraphState) -> dict:
    """避雷agent：跟着衣食住行agent自动级联（见AGENT_DEPENDENCY_TABLE），针对
    lifestyle_agent已产出的具体推荐检索有没有踩坑反馈。跟lifestyle_agent_node同款结构
    （edit定点修改判断、_invoke_with_write_verification写入校验），主要区别是
    verify_fn不要求"至少一个字段有warning"——避雷agent产出全空（所有实际推荐都没
    查到负面证据）是完全合法的结果；但全量生成仍要求本轮发生新写入，并为Lifestyle中
    每个实际(field, day)提交记录、绑定完整target_recommendation。"""
    project_id = state["project_id"]
    _assert_formal_comparison_baseline(project_id, state["topic"])
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")

    def _pitfall_output_covers_lifestyle(data: dict, *, day: int | None = None) -> bool:
        expected = {
            (field, item.get("day")): item.get("recommendation", "")
            for field in LIFESTYLE_FIELDS
            for item in (lifestyle or {}).get(field, {}).get("items", []) or []
            if day is None or item.get("day") == day
        }
        actual = {
            (field, item.get("day")): item.get("target_recommendation", "")
            for field in LIFESTYLE_FIELDS
            for item in data.get(field, {}).get("items", []) or []
            if day is None or item.get("day") == day
        }
        return bool(expected) and all(
            actual.get(key) == recommendation
            for key, recommendation in expected.items()
        )

    conflict_note = ""
    conflict = store.get_pending_conflict(project_id)
    if conflict and conflict.target.split(".")[0] == "pitfall_agent":
        conflict_note = f"\n注意：整合agent发现了问题需要你重新调整——{conflict.reason}"

    is_direct_edit_target = (
        state.get("mode") == "edit"
        and state.get("edit_request")
        and state.get("entry_target") == "pitfall_agent_node"
    )
    if is_direct_edit_target:
        original_snapshot = state.get("edit_original_pitfall")
        if original_snapshot is None:
            original_snapshot = store.read_workspace(project_id, "outputs/pitfall.json")
    else:
        original_snapshot = None

    if is_direct_edit_target:
        thread_id = agent_thread_id(project_id, "pitfall_draft")
        agent = build_pitfall_agent(
            project_id,
            state["topic"],
            checkpointer=get_agent_checkpointer(project_id),
        )
        try:
            snapshot = agent.get_state({"configurable": {"thread_id": thread_id}})
            checkpoint_pending = bool(snapshot.values) and bool(snapshot.next)
        except Exception:  # noqa: BLE001 - checkpoint读取失败时退化为一次全新生成
            checkpoint_pending = False
        if not checkpoint_pending:
            clear_agent_thread(project_id, "pitfall_draft")
        set_trace_context(project_id, "pitfall_agent")
        before_updated_at = (
            store.read_workspace(project_id, "outputs/pitfall.json") or {}
        ).get("updated_at")
        prompt = (
            f"衣食住行产出（针对下面这些具体推荐检索有没有踩坑反馈）："
            f"{json.dumps(lifestyle_for_pitfall(lifestyle), ensure_ascii=False)}\n"
            f"用户提出的具体要求：{state['edit_request']}\n"
            f"请判断这个要求具体涉及food/outfit/lodging/play中的哪一个字段、哪一天，"
            f"只针对相关的具体推荐检索有没有踩坑反馈，调用write_agent_output时只需要提交你新生成/修改的那几条——"
            f"未提交的天数或字段不会被这次调用影响，会自动保留原样。"
            f"如果需要参考现有避雷产出的具体措辞，可以用read_workspace工具查询outputs/pitfall.json。"
            f"{conflict_note}"
        )

        def _pitfall_written() -> bool:
            data = store.read_workspace(project_id, "outputs/pitfall.json") or {}
            updated_at = data.get("updated_at")
            return bool(updated_at and updated_at != before_updated_at)

        _invoke_with_write_verification(
            agent,
            prompt,
            verify_fn=_pitfall_written,
            retry_note="你刚才的回复没有真正调用write_agent_output工具写入避雷产出区，只有文字总结是不够的。"
            "请现在立刻调用write_agent_output工具，只提交本次修改涉及的item。",
            fallback_agent_factory=lambda: build_pitfall_agent_write_only(project_id, state["topic"]),
            thread_id=thread_id,
            resume_from_checkpoint=checkpoint_pending,
        )
    else:
        days = sorted(
            {
                int(item.get("day") or 0)
                for field in LIFESTYLE_FIELDS
                for item in (lifestyle or {}).get(field, {}).get("items", []) or []
                if int(item.get("day") or 0) > 0
            }
        )
        turn_id = state.get("turn_id") or ""
        day_runs = []
        for day in days:
            suffix = (
                f"{turn_id}:day{day}"
                if turn_id
                else ("" if len(days) == 1 else f"day{day}")
            )
            agent = build_pitfall_agent(
                project_id,
                state["topic"],
                checkpointer=get_agent_checkpointer(project_id),
                convergence_work_unit=f"pitfall_day_{day}",
            )
            thread_id = agent_thread_id(project_id, "pitfall_draft", suffix=suffix)
            try:
                snapshot = agent.get_state({"configurable": {"thread_id": thread_id}})
                has_checkpoint = bool(snapshot.values)
                checkpoint_pending = has_checkpoint and bool(snapshot.next)
            except Exception:  # noqa: BLE001 - checkpoint读取失败时退化为一次全新生成
                has_checkpoint = False
                checkpoint_pending = False
            day_runs.append((day, suffix, agent, thread_id, has_checkpoint, checkpoint_pending))

        for day, suffix, agent, thread_id, has_checkpoint, checkpoint_pending in day_runs:
            current = store.read_workspace(project_id, "outputs/pitfall.json") or {}
            if has_checkpoint and _pitfall_output_covers_lifestyle(current, day=day):
                continue
            if not checkpoint_pending:
                clear_agent_thread(project_id, "pitfall_draft", suffix=suffix)
            set_trace_context(project_id, "pitfall_agent")

            lifestyle_input = lifestyle_for_pitfall(lifestyle, day=day)
            formal_material_context = []
            if _is_formal_comparison(project_id):
                for field in LIFESTYLE_FIELDS:
                    for item in (lifestyle or {}).get(field, {}).get("items", []) or []:
                        if item.get("day") != day:
                            continue
                        recommendation = str(item.get("recommendation") or "")
                        response = research_service.retrieve_context(
                            project_id,
                            actor="pitfall_agent",
                            stage="pitfall",
                            target_key=f"pitfall:{field}:day:{day}",
                            target_sha256=hashlib.sha256(
                                recommendation.encode("utf-8")
                            ).hexdigest(),
                            query=recommendation,
                            top_k=3,
                        )
                        formal_material_context.append(
                            {
                                "field": field,
                                "day": day,
                                "results": _compact_research_results(
                                    response["results"], max_chars=300
                                ),
                            }
                        )

            prompt = (
                f"本工作单元只处理Day{day}，不要检索、生成或修改其他天。\n"
                f"当天衣食住行推荐：{json.dumps(lifestyle_input, ensure_ascii=False)}\n"
                f"请针对当天food/outfit/lodging/play四个字段里的每一条具体推荐分别检索，"
                f"最终一次调用write_agent_output提交Day{day}实际存在的全部(field, day={day}) item。"
                f"没查到可靠负面证据时warning留空，不要为了有产出编造内容，也不得提交其他day。"
                f"\nA级正式对照的当天逐项共享素材预检结果："
                f"{json.dumps(_pitfall_research_context_view(formal_material_context), ensure_ascii=False)}"
                f"{_findings_retry_hint(project_id, 'pitfall_agent')}{conflict_note}"
            )
            before_updated_at = current.get("updated_at")

            def _day_written(day_number=day, previous_updated_at=before_updated_at) -> bool:
                data = store.read_workspace(project_id, "outputs/pitfall.json") or {}
                return (
                    bool(data.get("updated_at"))
                    and data.get("updated_at") != previous_updated_at
                    and _pitfall_output_covers_lifestyle(data, day=day_number)
                )

            _invoke_with_write_verification(
                agent,
                prompt,
                verify_fn=_day_written,
                retry_note=(
                    f"你还没有完整写入Day{day}的避雷检索结果。请立即调用write_agent_output，"
                    f"只提交Day{day}实际存在的food/outfit/lodging/play item；"
                    f"没查到可靠负面证据时warning留空，不要提交其他天。"
                ),
                fallback_agent_factory=lambda: build_pitfall_agent_write_only(project_id, state["topic"]),
                thread_id=thread_id,
                resume_from_checkpoint=checkpoint_pending,
            )

        final_pitfall = store.read_workspace(project_id, "outputs/pitfall.json") or {}
        if not _pitfall_output_covers_lifestyle(final_pitfall):
            missing_days = [
                day
                for day in days
                if not _pitfall_output_covers_lifestyle(final_pitfall, day=day)
            ]
            raise RuntimeError(f"Pitfall日级工作单元未完整落盘：{missing_days}")

    if is_direct_edit_target:
        after_snapshot = store.read_workspace(project_id, "outputs/pitfall.json")
        changed = _changed_pitfall_fields(original_snapshot, after_snapshot)
        relevant_conflict_target = conflict.target if conflict and conflict.target.split(".")[0] == "pitfall_agent" else None
        if changed and _is_edit_overreach(state["edit_request"], changed, set(LIFESTYLE_FIELDS), relevant_conflict_target):
            # 跟lifestyle_agent_node同款动态语义比对（docs/ARCHITECTURE.md第20.2节）：
            # 静态阈值1不参考请求内容会误判天然涉及多个维度的合法请求。真实E2E复现过越权
            # 案例——用户只要求补充"杜甫草堂"（play字段）的避雷内容，agent却顺带清空了
            # "food"字段里另一条不相关的避雷内容（胡二哥药膳蹄花）。LLM判断失败时
            # _is_edit_overreach内部降级为原有的">1即可疑"静态阈值判断。不阻塞流程，
            # 走既有的report_conflict留痕机制，交给下游整合环节的复核循环处理。
            store.report_conflict(
                project_id,
                actor="citation_checker",
                target=f"pitfall_agent.{changed[0]}",
                reason=f"edit模式下用户只要求「{state['edit_request']}」，但实际改动了{changed}这{len(changed)}个字段，"
                f"可能误改了未涉及的部分，需要复核是否应该只保留其中一项改动。",
                issue_type="pitfall_edit_overreach",
            )

    result: dict = {
        "pitfall_agent_done": True,
        "last_agent_result": {"agent": "pitfall_agent", "summary": "避雷检索完成"},
    }
    if is_direct_edit_target:
        # 固定在state里，供同一edit线程内的后续重试沿用同一条基线（原理同lifestyle_agent_node）。
        result["edit_original_pitfall"] = original_snapshot
        # 供pitfall_citation_check_node收窄核验范围，只核验这次真的改动的字段（见docs/ISSUES.md第37补条）。
        result["edit_changed_pitfall_fields"] = changed
        # 更细一档：只核验真正改动的具体天（见docs/ISSUES.md第37补条已知局限的修复）。
        result["edit_changed_pitfall_items"] = _changed_pitfall_items(original_snapshot, after_snapshot)
    if conflict and conflict.target.split(".")[0] == "pitfall_agent":
        result["awaiting_integration_recheck"] = True
        result["integration_retry_count"] = state.get("integration_retry_count", 0) + 1
    return result


def pitfall_citation_check_node(state: GraphState) -> dict:
    """系统级校验，跟citation_check_node完全对称，核验对象换成outputs/pitfall.json。
    见src/materials/citation_check.py的verify_pitfall_citations。

    edit模式下只核验这次编辑实际改动的字段（edit_changed_pitfall_fields），未改动字段
    不重新核验，避免非确定性判断把此前已通过核验的内容误判掉（docs/ISSUES.md第37补条）。
    plan模式或edit模式下该字段未设置（如级联整体重新生成）时，fields为None，即全量核验，
    行为跟修复前一致。

    edit_changed_pitfall_items（更细一档，[field, day]列表）设置时进一步收窄到具体天——
    同字段内其它天完全没被触碰的item不该被无谓地重新送入有非确定性的核验判断（docs/ISSUES.md
    第37补条已知局限的修复）。未设置时item_keys为None，不做item级过滤，只受fields控制。"""
    project_id, topic = state["project_id"], state["topic"]
    force_flag = state.get("force_finalize", False)
    last_record_id = None
    is_edit = state.get("mode") == "edit"
    fields = state.get("edit_changed_pitfall_fields") if is_edit else None
    raw_item_keys = state.get("edit_changed_pitfall_items") if is_edit else None
    item_keys = {(f, d) for f, d in raw_item_keys} if raw_item_keys is not None else None

    for attempt in range(MAX_RETRY + 1):
        issues = citation_check.verify_pitfall_citations(project_id, topic, fields=fields, item_keys=item_keys)

        if not issues:
            if last_record_id:
                store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
            break

        summary = "；".join(
            f"{i['field']}.day{i['day']}「{i['warning'][:20]}...」缺失专有名词{i['missing_entities']}：{i['reason']}"
            for i in issues
        )
        affected_field = issues[0]["field"]
        if last_record_id:
            # 上一轮的record已经被这次重新核验取代——不论上一轮指出的问题这次是否已修复，
            # 都不该让它继续以pending状态存在（见下面mark_force_finalize分支的说明）。
            store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
        record = store.report_conflict(
            project_id,
            actor="citation_checker",
            target=f"pitfall_agent.{affected_field}",
            reason=f"避雷内容引用核验未通过（共{len(issues)}处）：{summary}",
            issues=issues,
        )
        last_record_id = record.id

        if attempt == MAX_RETRY:
            missing_by_key: dict[tuple[str, int], list[str]] = {}
            for i in issues:
                key = (i["field"], i["day"])
                missing_by_key.setdefault(key, []).extend(i.get("missing_entities") or [])
            store.mark_pitfall_items_force_finalized(
                project_id, sorted({(i["field"], i["day"]) for i in issues}), missing_entities_by_key=missing_by_key
            )
            store.mark_force_finalize(project_id)
            force_flag = True
            break

        agent = build_pitfall_agent(project_id, topic)
        set_trace_context(project_id, "pitfall_agent")
        fix_prompt = (
            f"自动核验发现你之前写的部分避雷内容，标注的chunk_id引用实际不支持内容里的具体说法。"
            f"问题清单：{summary}\n"
            f"请针对这些具体条目重新检查已有素材，必要时使用检索工具补证；"
            f"如果找到正确原文就更新引用，找不到则删除或收窄不被证据支持的具体说法，"
            f"无法支持warning时将其清空。不要保留无依据的具体断言。"
            f"{_missing_entities_retry_hint(issues)}"
            f"{_findings_retry_hint(project_id, 'pitfall_agent')}"
            f"其他没被指出问题的条目不用改。修正后重新调用write_agent_output提交你改动的那几条即可，其他条目不用重新提交。"
        )
        invoke_with_retry(agent, [{"role": "user", "content": fix_prompt}])
        store.increment_retry_count(project_id, record.id)

    result = {
        "force_finalize": force_flag,
        "pitfall_citation_checked": True,
        "last_agent_result": {"agent": "citation_checker", "summary": "避雷内容引用核验完成"},
    }
    return result


def culture_agent_node(state: GraphState) -> dict:
    """文化科普agent：游离主流程，仅edit入口@触发（docs/ORCHESTRATION.md场景9），
    不做引用级核验（web_search返回网页摘要，没有chunk_id体系，用prompt软约束代替），
    一跳结束——见_route_edit_mode里"last_agent == culture_agent"直接END的分支。

    跟pitfall_agent_node不同：没有edit误改检测，因为这个agent每次只是追加一条新问答到
    累加式历史记录里，不存在"已有字段被误改"的概念。"""
    project_id = state["project_id"]
    question = state.get("edit_request") or ""
    agent = build_culture_agent(project_id)
    set_trace_context(project_id, "culture_agent")

    prompt = (
        f"用户的文化科普问题：{question}\n"
        f"请先用read_workspace读取outputs/culture.json里已有的问答历史，"
        f"用web_search查证后回答，把这次新的问答追加到已有items后面，"
        f"调用write_agent_output传入完整的culture对象（含历史记录+这次新增的一条）。"
    )

    def _culture_written() -> bool:
        data = store.read_workspace(project_id, "outputs/culture.json")
        return bool(data and data.get("updated_at"))

    _invoke_with_write_verification(
        agent,
        prompt,
        verify_fn=_culture_written,
        retry_note="你刚才的回复没有真正调用write_agent_output工具写入，只有文字总结是不够的。"
        "请现在立刻调用write_agent_output，传入包含历史记录+这次新问答的完整对象。",
    )

    return {
        "culture_agent_done": True,
        "last_agent_result": {"agent": "culture_agent", "summary": "文化科普回答完成"},
    }


_LIFESTYLE_CLAIM_RETRY_FIELDS = (
    "target_path",
    "subject",
    "claim_text",
    "claim_type",
    "support_mode",
    "claimed_entities",
    "evidence_refs",
    "derived_from",
    "provenance_mode",
)


def _lifestyle_claims_by_id(lifestyle: dict | None) -> dict[str, dict]:
    """Return stable claim payloads without volatile evidence snapshot timestamps."""
    result: dict[str, dict] = {}
    for field in LIFESTYLE_FIELDS:
        for item in ((lifestyle or {}).get(field) or {}).get("items", []) or []:
            for claim in item.get("claims") or []:
                claim_id = str(claim.get("claim_id") or "")
                if not claim_id:
                    continue
                result[claim_id] = {
                    "field": field,
                    "day": item.get("day"),
                    **{key: copy.deepcopy(claim.get(key)) for key in _LIFESTYLE_CLAIM_RETRY_FIELDS},
                }
    return result


def _lifestyle_retry_claim_ids(
    before: dict | None,
    after: dict | None,
    issues: list[dict],
) -> set[str]:
    """Scope a retry to failed, actually changed, and dependent derived claims."""
    before_by_id = _lifestyle_claims_by_id(before)
    after_by_id = _lifestyle_claims_by_id(after)
    retry_ids = {
        str(issue.get("claim_id") or "")
        for issue in issues
        if issue.get("claim_id")
    }
    retry_ids.update(
        claim_id
        for claim_id in before_by_id.keys() | after_by_id.keys()
        if before_by_id.get(claim_id) != after_by_id.get(claim_id)
    )

    # A derived claim whose dependency changed must be re-evaluated even when its
    # own text and deterministic claim_id stayed unchanged.
    changed = True
    while changed:
        changed = False
        for claim_id, claim in after_by_id.items():
            if claim_id in retry_ids:
                continue
            if set(claim.get("derived_from") or []) & retry_ids:
                retry_ids.add(claim_id)
                changed = True
    return retry_ids


def _lifestyle_repair_items_snapshot(
    lifestyle: dict | None,
    day: int,
    fields: set[str],
) -> list[dict]:
    """Build the smallest complete item payload a restricted repair writer needs."""
    claim_keys = (
        "target_path",
        "subject",
        "claim_text",
        "claim_type",
        "support_mode",
        "claimed_entities",
        "evidence_refs",
        "derived_from",
    )
    snapshot: list[dict] = []
    for field in LIFESTYLE_FIELDS:
        if field not in fields:
            continue
        for item in ((lifestyle or {}).get(field) or {}).get("items", []) or []:
            if item.get("day") != day:
                continue
            snapshot.append(
                {
                    "field": field,
                    "day": day,
                    "recommendation": item.get("recommendation", ""),
                    "claims": [
                        {key: claim.get(key) for key in claim_keys if key in claim}
                        for claim in item.get("claims", []) or []
                    ],
                }
            )
    return snapshot


_LIFESTYLE_REPAIR_OPEN_STATUSES = {"started", "artifact_written"}


def _lifestyle_artifact_sha(value: dict | None) -> str:
    payload = json.dumps(
        value or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest_open_lifestyle_repair(project_id: str) -> dict | None:
    latest_by_id: dict[str, dict] = {}
    for item in store.read_validation_runs(project_id):
        if item.get("stage") != "lifestyle_citation_repair":
            continue
        repair_id = str(item.get("repair_id") or "")
        if repair_id:
            latest_by_id[repair_id] = item
    open_runs = [
        item
        for item in latest_by_id.values()
        if item.get("status") in _LIFESTYLE_REPAIR_OPEN_STATUSES
    ]
    if not open_runs:
        return None
    latest = max(open_runs, key=lambda item: str(item.get("checked_at") or ""))
    pending_conflict = store.get_pending_conflict(project_id)
    conflict_id = str(latest.get("conflict_id") or "")
    if conflict_id and (
        pending_conflict is None or pending_conflict.id != conflict_id
    ):
        return None
    if latest.get("status") == "artifact_written":
        current_sha = _lifestyle_artifact_sha(
            store.read_workspace(project_id, "outputs/lifestyle.json")
        )
        if latest.get("artifact_sha256") != current_sha:
            return None
    return latest


def _append_lifestyle_repair_run(
    project_id: str,
    *,
    repair_id: str,
    status: str,
    artifact_sha256: str,
    claim_ids: set[str] | list[str],
    item_keys: set[tuple[str, int]] | list[list],
    repair_round: int,
    conflict_id: str = "",
) -> dict:
    normalized_keys = sorted(
        [str(field), int(day)] for field, day in item_keys
    )
    return store.append_validation_run(
        project_id,
        {
            "stage": "lifestyle_citation_repair",
            "scope": "claim_or_item",
            "observation_mode": "live",
            "status": status,
            "artifact_sha256": artifact_sha256,
            "repair_id": repair_id,
            "repair_round": repair_round,
            "conflict_id": conflict_id,
            "claim_ids": sorted(str(claim_id) for claim_id in claim_ids if claim_id),
            "item_keys": normalized_keys,
            "source": "langgraph",
        },
    )


def citation_check_node(state: GraphState) -> dict:
    """系统级校验（不是agent自证）：核实lifestyle_agent产出里每条建议标的chunk_id
    是不是真的支持内容，见 src/materials/citation_check.py 和 docs/ISSUES.md。
    最多打回lifestyle_agent重新修正 MAX_RETRY 次，仍未通过就强制终结，交给整合agent
    如实标注（复用已有的force_finalize机制，不用另造一套）。

    edit模式下只核验这次编辑实际改动的字段（edit_changed_lifestyle_fields），未改动字段
    不重新核验，避免非确定性判断把此前已通过核验的内容误判掉（docs/ISSUES.md第37补条）。
    plan模式或edit模式下该字段未设置（如级联整体重新生成）时，fields为None，即全量核验，
    行为跟修复前一致。

    edit_changed_lifestyle_items（更细一档，[field, day]列表）设置时进一步收窄到具体天——
    同字段内其它天完全没被触碰的item不该被无谓地重新送入有非确定性的核验判断（docs/ISSUES.md
    第37补条已知局限的修复）。未设置时item_keys为None，不做item级过滤，只受fields控制。"""
    project_id, topic = state["project_id"], state["topic"]
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    route_map_validation = store.read_workspace(project_id, "route_map_validation.json") or {}
    # force_finalize是"一旦True就不能被后面的节点覆盖回False"的粘性标志——route_citation_check_node
    # 和这个节点都会返回force_finalize这个key，LangGraph对普通dict字段是直接覆盖不是合并，
    # 如果这里无脑返回局部变量的初始值False，会把route核验那边"已放弃修复"的信号冲掉，
    # 导致integration_agent收不到"如实标注未解决问题"的提示。所以起始值要读state里已有的。
    force_flag = state.get("force_finalize", False)
    last_record_id = None
    is_edit = state.get("mode") == "edit"
    fields = state.get("edit_changed_lifestyle_fields") if is_edit else None
    raw_item_keys = state.get("edit_changed_lifestyle_items") if is_edit else None
    item_keys = {(f, d) for f, d in raw_item_keys} if raw_item_keys is not None else None
    verification_fields = fields
    verification_item_keys = item_keys
    verification_claim_ids: set[str] | None = None
    recovery_run = _latest_open_lifestyle_repair(project_id)
    reuse_interrupted_passes = bool(recovery_run)
    if recovery_run:
        recovered_conflict_id = str(recovery_run.get("conflict_id") or "")
        if recovered_conflict_id:
            last_record_id = recovered_conflict_id
        recovered_claim_ids = {
            str(claim_id)
            for claim_id in recovery_run.get("claim_ids") or []
            if claim_id
        }
        recovered_item_keys = {
            (str(item[0]), int(item[1]))
            for item in recovery_run.get("item_keys") or []
            if isinstance(item, list) and len(item) == 2
        }
        if recovered_claim_ids:
            verification_claim_ids = recovered_claim_ids
        elif recovered_item_keys:
            verification_fields = sorted({field for field, _ in recovered_item_keys})
            verification_item_keys = recovered_item_keys

    for attempt in range(MAX_RETRY + 1):
        verify_kwargs = {
            "fields": None if verification_claim_ids is not None else verification_fields,
            "item_keys": None if verification_claim_ids is not None else verification_item_keys,
        }
        if reuse_interrupted_passes:
            verify_kwargs["reuse_existing_completed_claims"] = True
        if verification_claim_ids is not None:
            verify_kwargs["claim_ids"] = verification_claim_ids
        issues = citation_check.verify_lifestyle_citations(project_id, topic, **verify_kwargs)
        reuse_interrupted_passes = False

        if recovery_run:
            _append_lifestyle_repair_run(
                project_id,
                repair_id=str(recovery_run["repair_id"]),
                status="completed" if not issues else "validation_failed",
                artifact_sha256=_lifestyle_artifact_sha(
                    store.read_workspace(project_id, "outputs/lifestyle.json")
                ),
                claim_ids=set(recovery_run.get("claim_ids") or []),
                item_keys=list(recovery_run.get("item_keys") or []),
                repair_round=int(recovery_run.get("repair_round") or 0),
                conflict_id=str(recovery_run.get("conflict_id") or ""),
            )
            recovery_run = None

        if not issues:
            if last_record_id:
                store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
            break

        summary = "；".join(
            f"{i['field']}.day{i['day']}「{i['recommendation'][:20]}...」缺失专有名词{i['missing_entities']}：{i['reason']}"
            for i in issues
        )
        affected_field = issues[0]["field"]
        if last_record_id:
            # 上一轮的record已经被这次重新核验取代——不论上一轮指出的问题这次是否已修复，
            # 都不该让它继续以pending状态存在（见下面mark_force_finalize分支的说明）。
            store.resolve_conflict(project_id, last_record_id, actor="citation_checker")
        record = store.report_conflict(
            project_id,
            actor="citation_checker",
            target=f"lifestyle_agent.{affected_field}",
            reason=f"引用核验未通过（共{len(issues)}处）：{summary}",
            issues=issues,
        )
        last_record_id = record.id

        if attempt == MAX_RETRY:
            indexed = _attempt_lifestyle_gap_supplement(project_id, topic, issues)
            if indexed:
                repair_keys = sorted({(i["field"], i["day"]) for i in issues})
                repair_fields = sorted({field for field, _ in repair_keys})
                before_repair = store.read_workspace(project_id, "outputs/lifestyle.json")
                agent = build_lifestyle_agent_retry_fix(project_id, topic)
                set_trace_context(project_id, "lifestyle_agent")
                repair_prompt = (
                    f"系统刚为当前素材库新增了{len(indexed)}条已经完成入库、现在可被"
                    f"retrieve_materials检索到的素材。请只回炉修正{repair_keys}这些条目，"
                    f"针对核验缺失项重新检索新素材并更新sources；其他条目不要改。修正后"
                    f"调用write_agent_output，只提交这些条目。问题清单：{summary}"
                )
                try:
                    invoke_with_retry(agent, [{"role": "user", "content": repair_prompt}])
                    store.increment_retry_count(project_id, record.id)
                    after_repair = store.read_workspace(project_id, "outputs/lifestyle.json")
                    repair_claim_ids = _lifestyle_retry_claim_ids(
                        before_repair, after_repair, issues
                    )
                    repair_verify_kwargs = {
                        "fields": repair_fields,
                        "item_keys": set(repair_keys),
                    }
                    if all(issue.get("claim_id") for issue in issues):
                        repair_verify_kwargs = {
                            "fields": None,
                            "item_keys": None,
                            "claim_ids": repair_claim_ids,
                        }
                    repaired_issues = citation_check.verify_lifestyle_citations(
                        project_id, topic, **repair_verify_kwargs
                    )
                except Exception as exc:
                    repaired_issues = [
                        {**issue, "reason": f"补采后局部回炉执行失败：{exc}"} for issue in issues
                    ]
                if not repaired_issues:
                    store.resolve_conflict(project_id, record.id, actor="citation_checker")
                    break
                store.resolve_conflict(project_id, record.id, actor="citation_checker")
                repaired_summary = "；".join(
                    f"{i['field']}.day{i['day']}「{i['recommendation'][:20]}...」缺失"
                    f"专有名词{i['missing_entities']}：{i['reason']}" for i in repaired_issues
                )
                record = store.report_conflict(
                    project_id,
                    actor="citation_checker",
                    target=f"lifestyle_agent.{repaired_issues[0]['field']}",
                    reason=f"补采后局部回炉仍未通过（共{len(repaired_issues)}处）：{repaired_summary}",
                    issues=repaired_issues,
                )
                last_record_id = record.id
                issues = repaired_issues
            missing_by_key: dict[tuple[str, int], list[str]] = {}
            for i in issues:
                key = (i["field"], i["day"])
                missing_by_key.setdefault(key, []).extend(i.get("missing_entities") or [])
            store.mark_lifestyle_items_force_finalized(
                project_id, sorted({(i["field"], i["day"]) for i in issues}), missing_entities_by_key=missing_by_key
            )
            store.mark_force_finalize(project_id)
            force_flag = True
            break

        issues_by_day: dict[int, list[dict]] = {}
        for issue in issues:
            issues_by_day.setdefault(issue["day"], []).append(issue)

        changed_claim_ids: set[str] = set()
        before_attempt = store.read_workspace(project_id, "outputs/lifestyle.json")
        retry_item_keys = {(issue["field"], issue["day"]) for issue in issues}
        repair_id = uuid.uuid4().hex[:12]
        issue_claim_ids = {
            str(issue.get("claim_id") or "")
            for issue in issues
            if issue.get("claim_id")
        }
        _append_lifestyle_repair_run(
            project_id,
            repair_id=repair_id,
            status="started",
            artifact_sha256=_lifestyle_artifact_sha(before_attempt),
            claim_ids=issue_claim_ids,
            item_keys=retry_item_keys,
            repair_round=attempt + 1,
            conflict_id=record.id,
        )
        for repair_day, day_issues in sorted(issues_by_day.items()):
            day_summary = "；".join(
                f"{i['field']}.day{i['day']}「{i['recommendation'][:20]}...」缺失"
                f"专有名词{i['missing_entities']}：{i['reason']}"
                for i in day_issues
            )
            agent = build_lifestyle_agent_retry_fix(
                project_id,
                topic,
                convergence_work_unit=f"lifestyle_citation_day_{repair_day}_attempt_{attempt + 1}",
            )
            set_trace_context(project_id, "lifestyle_agent")
            fix_prompt = (
                f"自动核验发现Day{repair_day}的部分建议所标chunk_id，实际不支持具体说法。"
                f"本工作单元只处理Day{repair_day}，问题清单：{day_summary}\n"
                f"当天路线地图事实（只有status=passed的相邻路段可引用精确数值；anomaly仅用于"
                f"识别风险，已核验路段不得重新按名称生成另一套距离）："
                f"{json.dumps(route_map_day_view(route_plan, route_map_validation, repair_day), ensure_ascii=False)}\n"
                f"请针对这些具体条目重新检查已有素材，必要时使用检索工具补证；"
                f"如果找到正确原文就更新引用，找不到则删除或收窄证据不支持的具体说法，"
                f"现有证据不足时改成条件化或方向性建议。"
                f"{_findings_retry_hint(project_id, 'lifestyle_agent')}"
                f"其他没被指出问题的条目不用改，不要修改其他日期，也不要重新研究未被指出的内容。"
                f"修正后调用"
                f"write_agent_output，只提交Day{repair_day}中实际改动的条目。"
            )
            before_day = store.read_workspace(project_id, "outputs/lifestyle.json")
            try:
                invoke_with_retry(
                    agent,
                    [{"role": "user", "content": fix_prompt}],
                    recursion_limit=40,
                )
            except (
                ContextBudgetExceededError,
                GraphRecursionError,
            ):
                # The write tool can succeed on the final permitted step.  The next
                # verifier pass is the quality contract. Both the recursion bound and
                # context budget are deterministic research-stop signals; if no write
                # happened, hand the same scoped task to the restricted writer.
                if store.read_workspace(project_id, "outputs/lifestyle.json") == before_day:
                    write_only_agent = build_lifestyle_agent_retry_write_only(project_id, topic)
                    affected_fields = {issue["field"] for issue in day_issues}
                    repair_items = _lifestyle_repair_items_snapshot(
                        before_day,
                        repair_day,
                        affected_fields,
                    )
                    write_prompt = (
                        f"这是Lifestyle引用返修的受限提交阶段。本工作单元只处理Day{repair_day}，"
                        f"研究已经结束，不能联网或继续检索。问题清单：{day_summary}\n"
                        f"以下是Day{repair_day}受影响条目的完整精简JSON：\n"
                        f"{json.dumps(repair_items, ensure_ascii=False)}\n"
                        "只修改上述失败Claim所在的条目，原样保留未被指出的recommendation内容"
                        "和claims。已有证据不能支持的复合事实应拆分、收窄，或改成"
                        "estimate/unverified；不要修改其他日期，不要补造新事实。必须调用"
                        f"write_agent_output，只提交Day{repair_day}实际改动的完整条目。"
                    )
                    try:
                        _invoke_with_write_verification(
                            write_only_agent,
                            write_prompt,
                            verify_fn=lambda: (
                                store.read_workspace(project_id, "outputs/lifestyle.json")
                                != before_day
                            ),
                            retry_note=(
                                f"你尚未完成Day{repair_day}的局部写入。不要继续解释或只给修改建议；"
                                f"请读取上一轮工具校验反馈并修正参数，然后直接调用write_agent_output，"
                                f"只提交Day{repair_day}中问题清单涉及的条目。"
                            ),
                            max_retries=2,
                            agent_recursion_limit=30,
                        )
                    except GraphRecursionError:
                        # The restricted writer may use its final permitted step for
                        # write_agent_output and miss only the closing prose response.
                        # The persisted patch, not that prose, is the contract.
                        if store.read_workspace(project_id, "outputs/lifestyle.json") == before_day:
                            raise
                    if store.read_workspace(project_id, "outputs/lifestyle.json") == before_day:
                        raise RuntimeError(
                            f"Lifestyle Day{repair_day}引用返修达到研究边界后仍未完成局部写入"
                        )
            after_day = store.read_workspace(project_id, "outputs/lifestyle.json")
            changed_claim_ids.update(
                _lifestyle_retry_claim_ids(before_day, after_day, day_issues)
            )
        store.increment_retry_count(project_id, record.id)
        after_repair = store.read_workspace(project_id, "outputs/lifestyle.json")
        verification_fields = sorted({field for field, _ in retry_item_keys})
        verification_item_keys = retry_item_keys
        verification_claim_ids = (
            changed_claim_ids
            or _lifestyle_retry_claim_ids(before_attempt, after_repair, issues)
            if all(issue.get("claim_id") for issue in issues)
            else None
        )
        recovery_run = _append_lifestyle_repair_run(
            project_id,
            repair_id=repair_id,
            status="artifact_written",
            artifact_sha256=_lifestyle_artifact_sha(after_repair),
            claim_ids=verification_claim_ids or issue_claim_ids,
            item_keys=retry_item_keys,
            repair_round=attempt + 1,
            conflict_id=record.id,
        )

    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.INTEGRATING)
    result = {
        "force_finalize": force_flag,
        "last_agent_result": {"agent": "citation_checker", "summary": "引用核验完成"},
    }
    if state.get("mode") == "edit":
        # 供_route_edit_mode判断"lifestyle_agent的核验这一步已经走过"，避免死循环
        # 重复路由回citation_check_node。plan模式走的是progress.state硬编码阶段顺序，
        # 不需要这个标志。
        result["edit_citation_checked"] = True
    return result


def _compute_pitfall_source_counts(lifestyle: dict | None, pitfall: dict | None) -> dict[str, list[int]]:
    """纯函数：按field.day为key，统计避雷来源数vs原建议来源数（sources去重后的chunk_id条数），
    供integration_agent_node喂给模型做"避雷来源数是否明显超过原建议"的判断信号——原始数字
    必须是程序算出来的真实数字，"明显超过"这个判断本身仍交给模型，不用规则写死具体倍数
    （见docs/ARCHITECTURE.md第7节，只定了"来源数量"这个信号来源，没有定死阈值）。
    """
    def _unique_chunk_count(sources: list[dict] | None) -> int:
        return len({s["chunk_id"] for s in (sources or []) if s.get("chunk_id")})

    lifestyle_counts: dict[str, int] = {}
    for field in LIFESTYLE_FIELDS:
        for item in (lifestyle or {}).get(field, {}).get("items", []) or []:
            key = f"{field}.day{item.get('day')}"
            lifestyle_counts[key] = _unique_chunk_count(item.get("sources"))

    result: dict[str, list[int]] = {}
    for field in LIFESTYLE_FIELDS:
        for item in (pitfall or {}).get(field, {}).get("items", []) or []:
            if not item.get("warning"):
                continue  # 合法空结果，没有避雷内容可比较，不纳入信号
            key = f"{field}.day{item.get('day')}"
            result[key] = [_unique_chunk_count(item.get("sources")), lifestyle_counts.get(key, 0)]

    return result


def integration_agent_node(state: GraphState) -> dict:
    project_id, topic = state["project_id"], state["topic"]
    _assert_formal_comparison_baseline(project_id, topic)
    route_plan = store.read_workspace(project_id, "route_plan.json")
    route_map_validation = store.read_workspace(project_id, "route_map_validation.json") or {}
    feasibility = store.read_workspace(project_id, "route_feasibility.json") or {}
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    poi_details = [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]
    existing_guide_blocks = (
        store.read_workspace(project_id, "final_guide.blocks.json")
        if state.get("mode") == "edit"
        else None
    )
    deliverable_claim_ids = set(
        collect_upstream_claims(
            route_plan,
            lifestyle,
            pitfall,
            poi_details,
            evidence_checks=store.read_evidence_checks(project_id),
            review_findings=store.read_workspace(project_id, "review_findings.jsonl") or [],
        )
    )

    route_context = integration_delivery_view(
        "route_plan.json", route_plan, deliverable_claim_ids=deliverable_claim_ids
    )
    route_map_context = integration_delivery_view(
        "route_map_validation.json",
        route_map_validation,
        route_plan=route_plan,
    )
    feasibility_context = integration_delivery_view("route_feasibility.json", feasibility)
    lifestyle_context = integration_delivery_view(
        "outputs/lifestyle.json", lifestyle, deliverable_claim_ids=deliverable_claim_ids
    )
    pitfall_context = integration_delivery_view(
        "outputs/pitfall.json", pitfall, deliverable_claim_ids=deliverable_claim_ids
    )
    existing_blocks_context = integration_delivery_view(
        "final_guide.blocks.json",
        existing_guide_blocks,
        deliverable_claim_ids=deliverable_claim_ids,
    )

    # ---- 参考来源表机械生成用的数据（不再喂给模型） ----
    # 第8条最初把chunk原文inline进prompt，是为了让integration_agent自己核对"参考来源"
    # 表有没有编造引用；但第8条第2层修复（build_reference_section纯函数机械生成参考来源表，
    # 不经过LLM）早已让这个用途过时——真正的逐条引用真实性核验现在由citation_check系节点
    # （route_citation_check_node/citation_check_node/pitfall_citation_check_node）承担，
    # 它们是逐条比对chunk原文，不受这里的改动影响。这里inline全部cited chunk原文只是
    # 冗余的"二次核查"，在8天长线场景下是单次prompt里体积最大的一块，且chunk话题混杂
    # （见docs/ISSUES.md第9条已知局限），压缩成摘要反而可能因为截断位置不对而让二次核查
    # 失效，不如直接不做——上游核验关卡已经兜底，直接删除更干净。cited_chunks/chunk_texts
    # 仍需要保留，_replace_reference_section机械生成参考来源表要用。
    cited_chunks = citation_check.collect_cited_chunks(route_plan, lifestyle, pitfall, poi_details)
    chunk_texts = citation_check.load_chunk_texts(topic, project_id)

    # 避雷内容vs原建议的来源数量对比（docs/ARCHITECTURE.md第7节）：程序算出信号喂给模型，
    # 不要求模型自己去数sources长度——"明显超过"这个判断本身仍交给模型（不用规则写死倍数），
    # 但原始数字必须是程序算出来的真实数字，不能让模型凭空估算。
    pitfall_signal = _compute_pitfall_source_counts(lifestyle, pitfall)

    prior_conflict = store.get_pending_conflict(project_id)
    prior_contract_conflict = store.get_latest_unresolved_conflict(
        project_id,
        target="integration_agent",
        reason_prefix="最终攻略交付契约未通过",
    )
    # force的判断要同时兜底"整合阶段自己的重试次数已达上限"这个信号，不能只看
    # route/lifestyle核验阶段设置的force_finalize粘性标志——这是2026-07-21真实E2E
    # 首次触发`_route_integration_phase`的MAX_RETRY分支时发现的死循环bug：那个函数是
    # LangGraph条件边路由函数，只能返回下一跳节点名，没有能力把"该强制终结了"这个事实
    # 写回GraphState，它对上限的处理方式是直接调用store.mark_force_finalize()写磁盘
    # （标记conflicts.jsonl里的pending记录），但这个磁盘副作用从未被这里读取过。
    # 结果：一旦触发到上限，会不断把不受限制的prompt喂给模型，模型每次都诚实地
    # 发现同一个真实冲突并调用report_conflict，形成死循环，直到进程被手动杀掉——
    # 真实E2E里5分钟内产生了24条冲突记录。用state["integration_retry_count"]（跟
    # _route_integration_phase判断上限用的是同一个权威计数器）兜底，不需要新增
    # 跨节点通信机制。见docs/ISSUES.md。
    force = state.get("force_finalize", False) or state.get("integration_retry_count", 0) >= MAX_RETRY
    force_note = (
        "打回次数已达上限，这次不允许再调用report_conflict，必须直接写出最终攻略，"
        "并在文中如实标注仍未解决的问题。" if force else ""
    )

    # 同lifestyle_agent_node：只有用户直接@integration_agent时才走"定点修改"提示，
    # 被级联触发（比如lifestyle_agent改完需要重新汇总）时走plan模式风格的全量整合prompt——
    # 因为整合本身就是"基于最新上下游数据重新汇总"，级联场景下这本来就是期望行为，不需要
    # 额外的"只改一部分"提示。用entry_target而不是last_agent_result is None判断，原因同
    # lifestyle_agent_node：自我修正重试时last_agent_result已经非None，用它会丢失edit语境。
    # 见docs/ISSUES.md第28/29条。
    is_direct_edit_target = (
        state.get("mode") == "edit"
        and state.get("edit_request")
        and state.get("entry_target") == "integration_agent_node"
    )
    edit_note = (
        f"\n这是一次局部修改请求：{state['edit_request']}。请只重新整合受影响的部分，"
        f"其余正文保持不变，不要把整篇攻略重新改写一遍。" if is_direct_edit_target else ""
    )
    prompt_conflict = prior_conflict or (prior_contract_conflict if force else None)
    if prompt_conflict and prompt_conflict.target.split(".")[0] == "integration_agent":
        # 自检长度骤降触发的自我打回（见本函数下方），提示里要点明具体原因，
        # 不能只沉默地让它重跑一遍——否则大概率重复同样的过度压缩。
        edit_note += f"\n注意：上一轮自动检查发现问题——{prompt_conflict.reason}"
    # 同lifestyle_agent_node：基线固定为"这次edit开始前"的原始长度，不能是上一轮重试后的
    # 长度——否则第1轮从1000字符压缩到600字符（已经骤降）被打回后，第2轮只要不再继续大幅
    # 缩水（比如维持在600字符左右），会因为"相对上一轮没有骤降"而被放行，实际上相对最初的
    # 1000字符仍然是一次不该发生的压缩。
    if is_direct_edit_target:
        original_guide_len = state.get("edit_original_guide_len")
        if original_guide_len is None:
            original_guide_len = len(store.read_workspace(project_id, "final_guide.md") or "")
    else:
        original_guide_len = None

    prompt = (
        f"用户原始需求：{json.dumps(state['requirements'], ensure_ascii=False)}\n"
        f"路线规划（交付视图）：{json.dumps(route_context, ensure_ascii=False)}\n"
        f"路线地图事实（交付视图；只有status=passed的路段可作为精确距离与时长的唯一来源，"
        f"anomaly只表示风险，不得引用其数值）："
        f"{json.dumps(route_map_context, ensure_ascii=False)}\n"
        f"路线可执行性核验（交付视图）："
        f"{json.dumps(feasibility_context, ensure_ascii=False)}\n"
        f"衣食住行产出（交付视图）：{json.dumps(lifestyle_context, ensure_ascii=False)}\n"
        f"避雷agent针对上述具体推荐检索到的踩坑反馈（交付视图）："
        f"{json.dumps(pitfall_context, ensure_ascii=False)}\n"
        f"已有最终攻略正文块（局部修改时保留未受影响块）："
        f"{json.dumps(existing_blocks_context, ensure_ascii=False)}\n"
        f"以上交付视图保留了正文、核验结论和当前可交付的claim_id；"
        f"未解决的失败claim_id已被程序过滤，不得把没有对应可交付claim的具体表述写成已核实事实。"
        f"原始Web结果、"
        f"证据快照和引用详情已由上游核验并完整保留在Workspace，"
        f"不需要你重新读取或复核。\n"
        f"来源数量对比（程序统计，chunk_id去重后的sources条数，field.day为key，"
        f"[避雷来源数, 原建议来源数]为value）：{json.dumps(pitfall_signal, ensure_ascii=False)}\n"
        f"请检查两者是否存在有明确上游依据的跨模块冲突（如Day归属、城市/区域字段或已有高德核验结果互相矛盾），"
        f"不要凭自己的地理知识重新判断地点距离；并且**重点核查衣食住行产出是否真正遵守了用户的硬性要求**"
        f"（尤其是饮食禁忌等——如果用户不吃辣但美食建议里有辣味菜品，这就是必须打回的冲突，不能忽略）。"
        f"如果有严重冲突且不是强制终结状态，调用report_conflict说明问题，target填'lifestyle_agent.<字段名>'。"
        f"**处理避雷内容时**：默认以衣食住行agent自己的内容为主，避雷内容作为补充预警标注融入最终攻略，"
        f"不要直接替换或删除原建议；只有当来源数量对比显示避雷来源数明显超过原建议来源数时，"
        f"才在正文里明确标注'需要重新考虑该推荐'，并调用report_conflict（target填'lifestyle_agent.<字段名>'，"
        f"reason里说明是因为避雷来源数明显更多）。"
        f"路线可执行性报告只用于避免把风险误写成已通过；运营提醒由程序Join统一投影，"
        f"不要在正文重复技术状态，也不得据此删除、缩短、替换或重排POI。"
        f"如果没有冲突，请整合成一份完整可读的最终攻略，并以完整有序blocks写入；"
        f"正文中每个使用上游事实的块都必须填真实upstream_claim_ids，"
        f"纯编辑结构、用户要求和未核验提示分别标为editorial/user_constraint/unverified。"
        f"（含每日路线、美食/穿搭/住宿/游玩建议、必备清单、避雷提示）并写入。**不要自己撰写"
        f"'参考来源'章节——这部分由程序根据真实chunk数据自动生成并追加，你写了也会被覆盖，"
        f"不用花精力编。**write_final_guide的meta必须使用规范模块名："
        f"route_plan、lifestyle.food、lifestyle.outfit、lifestyle.lodging、lifestyle.play；"
        f"实际融入了非空避雷内容时再加pitfall，并设置checklist_included=true。**"
        f"{force_note}{edit_note}"
    )
    integration_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    integration_thread_id = agent_thread_id(
        project_id, "integration_draft", suffix=integration_fingerprint
    )
    agent = build_integration_agent(
        project_id,
        topic,
        checkpointer=get_agent_checkpointer(project_id),
    )
    try:
        integration_snapshot = agent.get_state(
            {"configurable": {"thread_id": integration_thread_id}}
        )
        integration_checkpoint_messages = (
            integration_snapshot.values.get("messages", [])
            if isinstance(integration_snapshot.values, dict)
            else []
        )
        integration_checkpoint_pending = bool(integration_snapshot.values) and bool(
            integration_snapshot.next
        )
        integration_failed_write_draft = _has_failed_write_draft(
            integration_checkpoint_messages, "write_final_guide"
        )
    except Exception:  # noqa: BLE001 - checkpoint读取失败时退化为全新整合
        integration_checkpoint_pending = False
        integration_failed_write_draft = False
    if not integration_checkpoint_pending and not integration_failed_write_draft:
        clear_agent_thread(
            project_id, "integration_draft", suffix=integration_fingerprint
        )
    integration_invoke_prompt = prompt
    if integration_failed_write_draft:
        latest_write_error = _latest_failed_write_error(
            integration_checkpoint_messages
        )
        integration_invoke_prompt = (
            "上一轮write_final_guide已经生成了完整blocks，但工具参数校验失败。"
            "请直接基于上一轮保留的完整草稿修正参数并重新调用write_final_guide；"
            "不要重新读取上游、不要重新生成整份攻略，也不要只用文字说明。"
            f"最近一次工具校验错误：{latest_write_error or '请读取最近的工具返回'}。"
        )
    set_trace_context(project_id, "integration_agent")
    meta_before = store.read_workspace(project_id, "final_guide.meta.json") or {}
    meta_updated_at_before = meta_before.get("updated_at") if isinstance(meta_before, dict) else None

    def _integration_done() -> bool:
        guide = store.read_workspace(project_id, "final_guide.md")
        meta_now = store.read_workspace(project_id, "final_guide.meta.json") or {}
        conflict_now = store.get_pending_conflict(project_id)
        new_conflict_raised = conflict_now is not None and (prior_conflict is None or conflict_now.id != prior_conflict.id)
        guide_written = bool(
            guide
            and isinstance(meta_now, dict)
            and meta_now.get("updated_at")
            and meta_now.get("updated_at") != meta_updated_at_before
        )
        return guide_written or new_conflict_raised

    _invoke_with_write_verification(
        agent,
        integration_invoke_prompt,
        verify_fn=_integration_done,
        retry_note="你刚才的回复既没有调用write_final_guide写出最终攻略，也没有调用report_conflict报告问题。"
        "请现在必须二选一：如果内容没问题，调用write_final_guide写出完整攻略；如果有问题，调用report_conflict说明。",
        # Integration的单次Prompt即使经过交付视图仍属长输入。超时时
        # 不在同一节点内隐式重发多次；由根图保留失败状态，排查后
        # 再显式resume，避免重复消耗大量输入token。
        network_max_retries=1,
        thread_id=integration_thread_id,
        resume_from_checkpoint=integration_checkpoint_pending,
    )

    new_conflict = store.get_pending_conflict(project_id)
    new_model_conflict = new_conflict is not None and (prior_conflict is None or new_conflict.id != prior_conflict.id)
    contract_issues: list[dict] = []
    if not new_model_conflict:
        original_route_plan = state.get("poi_reconciliation_original_route_plan") or state.get(
            "edit_original_route_plan"
        )
        if state.get("mode") == "edit" and isinstance(original_route_plan, dict):
            active_poi_results = _reconcile_poi_details_after_route_edit(
                project_id,
                original_route_plan,
                route_plan,
            )
            _sync_poi_details_into_final_guide(project_id, active_poi_results)
            poi_details = [
                detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)
            ]
            cited_chunks = citation_check.collect_cited_chunks(
                route_plan, lifestyle, pitfall, poi_details
            )
        # 参考来源是程序所有的最终落盘内容，必须先规范化，再对最终文件做合同验证。
        # 旧顺序先验证、后替换参考来源，会让contract_validation.guide_sha256指向旧版本；
        # 如果模型把参考来源标题写在正文中部，替换还可能在验证通过后截掉后续章节。
        _replace_reference_section(project_id, topic, cited_chunks, chunk_texts)
        after_guide = store.read_workspace(project_id, "final_guide.md") or ""
        final_meta = store.read_workspace(project_id, "final_guide.meta.json")
        final_blocks = store.read_workspace(project_id, "final_guide.blocks.json")
        contract_issues = validate_final_guide_contract(
            state.get("requirements") or {},
            route_plan,
            feasibility,
            lifestyle,
            pitfall,
            after_guide,
            final_meta,
            final_blocks,
            poi_details,
            store.read_evidence_checks(project_id),
            store.read_workspace(project_id, "review_findings.jsonl") or [],
        )

    if is_direct_edit_target and not new_model_conflict:
        before_len = original_guide_len or 0
        after_len = len(after_guide)
        # 局部修改不应该让正文大幅缩水——真实测试里"信息压缩"是本项目反复出现的问题
        # （见docs/ISSUES.md第26/27条），edit模式下如果模型把定点修改做成了大范围重写/删减，
        # 用长度骤降作为粗粒度信号自动打回复核，而不是信任模型对"只改受影响部分"的自觉遵守。
        if before_len > 200 and after_len < before_len * 0.7:
            contract_issues.append(
                {
                    "code": "edit_length_drop",
                    "message": f"edit模式下用户只要求「{state['edit_request']}」，但整合后的最终攻略长度从"
                    f"{before_len}字符骤降到{after_len}字符，疑似误删正文",
                    "evidence_paths": ["final_guide.md"],
                    "before_length": before_len,
                    "after_length": after_len,
                }
            )

    if not new_model_conflict:
        if prior_conflict is not None:
            store.resolve_conflict(project_id, prior_conflict.id)
        if contract_issues:
            contract_retry_exhausted = state.get("integration_retry_count", 0) >= MAX_RETRY
            status = "force_finalized" if contract_retry_exhausted else "retrying"
            store.write_final_guide_contract_validation(project_id, status=status, issues=contract_issues)
            if contract_retry_exhausted:
                new_conflict = None
            else:
                summary = "；".join(issue["message"] for issue in contract_issues)
                store.report_conflict(
                    project_id,
                    actor="citation_checker",
                    target="integration_agent",
                    reason=f"最终攻略交付契约未通过：{summary}",
                    issues=contract_issues,
                    issue_type="final_guide_contract",
                )
                new_conflict = store.get_pending_conflict(project_id)
        else:
            store.write_final_guide_contract_validation(project_id, status="passed", issues=[])
            latest_contract_conflict = store.get_latest_unresolved_conflict(
                project_id,
                target="integration_agent",
                reason_prefix="最终攻略交付契约未通过",
            )
            if latest_contract_conflict is not None:
                store.resolve_conflict(project_id, latest_contract_conflict.id)
            new_conflict = None

    if new_conflict is None:
        # 只有plan模式首次整合完成才进入"树木生成中"——edit模式下integration_agent_node
        # 是对一个已经"完成"的project做定点修改（见docs/ORCHESTRATION.md第4.2节），项目
        # 应该维持在"完成"，不应该被重新推进状态机、更不该触发POI详情agent（POI详情agent
        # 明确不接edit入口，见docs/ARCHITECTURE.md第3.4节第9点已知局限）。
        next_phase = ProjectPhase.POI_DETAIL if state.get("mode") == "plan" else ProjectPhase.DONE
        store.update_progress(project_id, actor="main_agent", state=next_phase)

    return {
        "integration_done": True,
        "last_agent_result": {"agent": "integration_agent", "summary": "整合完成"},
        # 不管这轮复核结果如何（解决了/又发现新问题），复核这一步本身已经完成，
        # 清掉标志，避免下次路由无脑跳过pending conflict检查而直接又回integration_agent_node
        # （虽然直接回integration_agent_node本身无害，但会让重试计数判断逻辑失去意义）。
        "awaiting_integration_recheck": False,
        # 自己给自己report_conflict（长度骤降自检）时，必须自己递增重试计数——
        # 这条路径不像lifestyle_agent/route_agent那样有一个"別的agent把冲突打回给我"的
        # 上游环节代为递增，如果不在这里主动加，_route_integration_phase的MAX_RETRY判断
        # 永远不会触发，可能无限循环重跑自己。
        **(
            {"integration_retry_count": state.get("integration_retry_count", 0) + 1}
            if new_conflict is not None and new_conflict.target.split(".")[0] == "integration_agent"
            else {}
        ),
        # 固定基线，供同一edit线程内的后续重试沿用（原理同lifestyle_agent_node）。
        **({"edit_original_guide_len": original_guide_len} if is_direct_edit_target else {}),
    }


def _replace_reference_section(project_id: str, topic: str, cited_chunks: dict, chunk_texts: dict) -> None:
    """替换final_guide.md里模型自己可能写的"参考来源"章节（如果有）；
    保留正文和POI附录，并统一组装为“正文 -> POI -> 参考来源”；
    final_guide.meta.json的references字段同步用程序算出的结果覆盖，不保留模型写的版本。
    见docs/ISSUES.md第8条。
    """
    delivery.replace_reference_section(project_id, topic, cited_chunks, chunk_texts)


# ---------------------------------------------------------------------------
# 树木生成中：POI详情agent（docs/ORCHESTRATION.md 第3.3节）
# ---------------------------------------------------------------------------


def _needs_detail_targets(route_plan: Optional[dict]) -> list[dict]:
    """从route_plan.json里提取所有needs_detail=true的POI，返回派发给poi_detail_agent_node
    的payload列表：[{day, poi_name, needs_detail_reason}]。纯函数，方便单测。"""
    targets = []
    for day in (route_plan or {}).get("days", []) or []:
        for poi in day.get("pois", []) or []:
            if isinstance(poi, dict) and poi.get("needs_detail"):
                targets.append(
                    {
                        "day": day.get("day"),
                        "poi_name": poi.get("name", ""),
                        "needs_detail_reason": poi.get("needs_detail_reason", ""),
                    }
                )
    return targets


def _route_poi_detail_flags(route_plan: Optional[dict]) -> dict[tuple[int, str], bool]:
    """返回路线中每个``(day, poi_name)``对应的needs_detail状态。"""
    flags: dict[tuple[int, str], bool] = {}
    for day in (route_plan or {}).get("days", []) or []:
        day_number = day.get("day")
        if not isinstance(day_number, int):
            continue
        for poi in day.get("pois", []) or []:
            if not isinstance(poi, dict):
                continue
            poi_name = str(poi.get("name") or "").strip()
            if poi_name:
                flags[(day_number, poi_name)] = bool(poi.get("needs_detail"))
    return flags


def _reconcile_poi_details_after_route_edit(
    project_id: str,
    original_route_plan: Optional[dict],
    current_route_plan: Optional[dict],
) -> list[dict]:
    """归档路线edit后失效的树木，重建manifest并返回用于附录同步的活动结果。

    只失效原路线中被删除/换日的POI，或明确从needs_detail=true改成false的POI。这样不会
    误伤用户通过POI edit入口手动补充、但本来就不依赖needs_detail标记的额外树木。
    """
    old_flags = _route_poi_detail_flags(original_route_plan)
    current_flags = _route_poi_detail_flags(current_route_plan)
    stale_keys = (set(old_flags) - set(current_flags)) | {
        key for key, needed in old_flags.items() if needed and not current_flags.get(key, False)
    }

    manifest_data = store.read_workspace(project_id, "outputs/poi_details/manifest.json")
    all_details = store.list_poi_details(project_id, include_unmanifested=True)
    if isinstance(manifest_data, dict):
        entries = [
            PoiDetailManifestEntry.model_validate(entry)
            for entry in (manifest_data.get("generated") or [])
            if isinstance(entry, dict)
        ]
    else:
        # 兼容早期项目：没有manifest时先把活动目录里的详情登记为当前集合。
        entries = [
            PoiDetailManifestEntry(
                poi_name=detail.poi_name,
                day=detail.day,
                file=f"day{detail.day}_{store.poi_slug(detail.poi_name)}.json",
                citation_status=detail.citation_status,
            )
            for detail in all_details
        ]

    for detail in all_details:
        if (detail.day, detail.poi_name) in stale_keys:
            store.archive_poi_detail(project_id, "main_agent", detail.day, detail.poi_name)

    kept_entries = [entry for entry in entries if (entry.day, entry.poi_name) not in stale_keys]
    store.write_poi_details_manifest(
        project_id,
        "main_agent",
        PoiDetailManifest(generated=kept_entries),
    )
    results = []
    for entry in kept_entries:
        result = {
            "day": entry.day,
            "poi_name": entry.poi_name,
            "citation_status": entry.citation_status,
        }
        if entry.failure_kind:
            result["failure_kind"] = entry.failure_kind
        if entry.failure_summary:
            result["failure_summary"] = entry.failure_summary
        if entry.generation_mode == "provider_recovery":
            result["generation_mode"] = entry.generation_mode
            result["recovery_trigger"] = entry.recovery_trigger
        results.append(result)
    return results


class _TitleRelevanceBatch(BaseModel):
    relevant_indices: list[int] = Field(
        description="真的可能包含指定目的地/主题相关内容的候选编号，从0开始"
    )


_TITLE_TARGET_SUFFIXES = ("市", "区", "县", "自治州", "景区", "风景区", "风景名胜区")


def _title_mentions_supplement_target(topic: str, entity_hint: str, title: str) -> bool:
    """自动下载前的最低确定性条件：标题至少字面命中目的地或缺失实体。

    这不是最终相关性判定，只是防止轻量模型把"冬天去哪玩"这类泛旅游标题
    送进下载+转写。仅剥离封闭的行政/景区后缀，不维护开放式地点类型表；通过后
    仍由LLM标题粗筛和全文ingest门禁两层判定。
    """
    normalized_title = re.sub(r"\s+", "", title or "").casefold()
    terms: set[str] = set()
    for value in (topic, entity_hint):
        for raw_term in str(value or "").split():
            term = re.sub(r"\s+", "", raw_term).casefold()
            if len(term) >= 2:
                terms.add(term)
            for suffix in _TITLE_TARGET_SUFFIXES:
                if term.endswith(suffix):
                    core = term[: -len(suffix)]
                    if len(core) >= 2:
                        terms.add(core)
    return any(term in normalized_title for term in terms)


def _filter_relevant_candidates(topic: str, entity_hint: str, candidates: list[dict]) -> list[dict]:
    """search_and_rank返回的候选按热度/时长/时新度排序，完全不考虑标题内容是否真的
    跟目的地/缺失实体相关——真实复现过（docs/ISSUES.md第75条）候选池被字面命中关键词但
    内容毫不相关的高热度视频（游戏解说、沙雕动画）挤占，排序规则改不了这个问题（那些
    视频的播放/点赞/时长/时新度指标反而更好看）。在真正调用collect_one（下载+转写，
    真金白银的成本）之前，先用轻量模型对候选标题做一次批量相关性粗筛——只看标题，不看正文，
    且明确带上topic目的地上下文，模型才能判断"标题字面提到的东西是不是真的
    发生在这个地方"，而不是只看关键词是否出现。候选池最多25条，逐条调模型会把一次
    粗筛放大成长链路，并让任意一条超时拖住整个补采流程，所以必须批量判断。

    过滤失败（模型调用异常等）时保留该候选，不因为粗筛本身出错而误伤——真正的质量门禁
    仍然是下载/转写后ingest_topic的is_relevant判断（那个基于全文，更权威），这里只是
    在它之前省掉明显不相关候选的下载/转写成本，判断不确定时应该让候选进入下一关而不是
    在这里就武断丢弃。
    """
    if not candidates:
        return []
    candidates = [
        candidate
        for candidate in candidates
        if _title_mentions_supplement_target(topic, entity_hint, candidate.get("title", ""))
    ]
    if not candidates:
        return []
    try:
        model = get_light_chat_model(temperature=0).with_structured_output(
            _TitleRelevanceBatch, method="json_mode"
        )
    except Exception:
        return candidates  # 模型初始化本身失败，同样按"判断不确定，放行"处理，不武断丢弃
    titles = "\n".join(f"{index}. {candidate.get('title', '')}" for index, candidate in enumerate(candidates))
    try:
        verdict: _TitleRelevanceBatch = model.invoke(
            f"目的地/主题：{topic}\n"
            f"我们正在为「{topic}」这个目的地补充关于「{entity_hint}」的真实旅游/生活类素材。\n"
            "请从下列候选视频标题中，选出真的可能包含该目的地当地相关真实内容的编号；"
            "排除只是字面命中关键词、实际为游戏、搞笑段子、其它地区或无关话题的标题。\n"
            f"候选标题：\n{titles}\n"
            "请以JSON格式回复，包含relevant_indices（从0开始的整数数组）。"
        )
    except Exception:
        return candidates
    relevant_indices = {
        index for index in verdict.relevant_indices if isinstance(index, int) and 0 <= index < len(candidates)
    }
    return [candidate for index, candidate in enumerate(candidates) if index in relevant_indices]


def _attempt_auto_material_supplement(
    project_id: str,
    topic: str,
    query_seeds: list[str],
    search_keywords: list[str],
    label: str,
    limit: int,
) -> MaterialSupplementResult:
    """核验打回耗尽、即将force_finalize前的共享入口：判定是否真的缺素材，在项目级预算
    内自动下载/转写/入库补充。docs/ARCHITECTURE.md第16节。

    返回本次真正生成可检索entry/chunk的条目列表；rejected/pending另记在结果属性中，
    不得被调用方当成"补充成功"来获得额外修复机会。
    预算已耗尽、check_material_gap判定"不是真缺"、搜索本身失败、或候选全部采集失败，
    都返回空列表，调用方按各自的既有逻辑处理"没有真正补充成功"这个结果（POI详情agent
    此前有一条"只呈现候选、不下载"的兜底路径，因为没有相关性/内容安全过滤、且从未真正
    有人工审核入口消费这个字段，已于docs/ISSUES.md第98条移除；route/lifestyle本来就
    没有对应的呈现渠道，见docs/ISSUES.md第51条的已知局限）。

    limit：这次调用最多允许真正采集几条（不是search_and_rank的候选池大小，是"最终成功
    入库"的上限）——调用方（route/lifestyle）在同一次force_finalize事件里可能对多个
    缺失实体分别调用本函数，需要在多次调用之间共享一个递减的预算，所以由调用方显式传入，
    不在这里写死_MAX_AUTO_COLLECT_PER_TRIGGER（POI详情agent一次只对应一个POI，直接传
    _MAX_AUTO_COLLECT_PER_TRIGGER本身）。

    本函数只负责确认入库事实；是否执行一次有上限的局部回炉由三个调用方各自决定。
    """
    shared = material_supplement.attempt_auto_material_supplement(
        project_id,
        topic,
        query_seeds,
        search_keywords,
        stage=label,
        target_key=f"{label}:material_gap",
        repair_scope={"label": label},
        limit=limit,
        gap_checker=check_material_gap,
        candidate_filter=_filter_relevant_candidates,
        ingest_fn=ingest_topic,
        processing_status_fn=material_processing_status,
    )
    return MaterialSupplementResult(
        list(shared.indexed), rejected=shared.rejected, pending=shared.pending
    )


_ROUTE_LIFESTYLE_QUERY_SUFFIXES = ("介绍", "攻略")


def _entity_gap_query_seeds(entity: str, angle_word: str | None = None) -> list[str]:
    """route/lifestyle场景下，从核验报出的具体缺失实体反推检索问法——跟POI详情agent
    "{名称} 讲解"/"{名称} 攻略"的固定后缀模式同一个思路，只是多了一个可选的场景相关
    角度词（比如lifestyle的food字段传"美食"），生成多个不同角度的问法交给check_material_gap
    逐个验证。query_seed用空格分隔实体和角度词——这是src/materials/gap_check.py
    _query_core_entities按空格拆词剥离泛化词的约定，见该模块docstring。
    """
    seeds = [entity]
    if angle_word:
        seeds.append(f"{entity} {angle_word}")
    seeds.extend(f"{entity} {suffix}" for suffix in _ROUTE_LIFESTYLE_QUERY_SUFFIXES)
    return seeds


def _attempt_route_gap_supplement(project_id: str, topic: str, issues: list[dict]) -> list[dict]:
    """route_citation_check_node的MAX_RETRY耗尽分支专用：issues是verify_route_citations
    报出的问题列表（每条含missing_entities），对全部涉及的缺失实体去重后逐个尝试自动补充，
    单次事件共享_MAX_AUTO_COLLECT_PER_TRIGGER这一个预算上限（不是每个实体各自2条）。

    missing_entities为空的issue（通常是_verify_items"完全没有标注sources/claimed_entities"
    这一分支——agent压根没写引用，而不是引用了错误实体）此前会被直接跳过，完全不会触发
    自动补充——但这恰恰是最该触发补充采集的场景（agent放弃写引用往往正是因为真的检索不到
    东西），真实E2E（黄龙溪古镇案例）发现这个bug后已修复：退化用issue自带的这一天POI列表
    （extra.pois，见verify_route_citations）反推候选实体，不再让这类issue空转跳过。
    详见docs/ISSUES.md第58条。
    """
    collected_all: list[dict] = []
    remaining = _MAX_AUTO_COLLECT_PER_TRIGGER
    entities: set[str] = set()
    for i in issues:
        missing = i.get("missing_entities") or []
        if missing:
            entities.update(missing)
        else:
            for poi in i.get("pois") or []:
                entities.update(citation_check._extract_poi_names(poi))
    for entity in sorted(entities):
        if remaining <= 0:
            break
        seeds = _entity_gap_query_seeds(entity)
        collected = _attempt_auto_material_supplement(
            project_id, topic, seeds, seeds, label="route", limit=remaining
        )
        collected_all.extend(collected)
        remaining -= len(collected)
    return collected_all


# lifestyle四个字段各自对应的检索角度词，帮check_material_gap/search_and_rank生成更贴合
# 该字段场景的问法——不是必需（entity本身+"介绍"/"攻略"已经能覆盖大部分情况），只是让
# 生成的问法更贴近这条推荐实际讨论的话题。
_LIFESTYLE_FIELD_ANGLE_WORDS = {"food": "美食", "outfit": "穿搭", "lodging": "住宿", "play": "游玩"}


def _route_poi_entities_by_day(project_id: str) -> dict[int, list[str]]:
    """从route_plan.json按天提取POI纯地名列表，供_attempt_lifestyle_gap_supplement
    在missing_entities为空时兜底反推候选实体（见该函数说明）。找不到route_plan或
    某天不存在时返回空列表，不报错。"""
    route_plan = store.read_workspace(project_id, "route_plan.json")
    if not route_plan:
        return {}
    result: dict[int, list[str]] = {}
    for day in route_plan.get("days", []):
        pois = [citation_check._poi_display_name(p) for p in (day.get("pois") or [])]
        result[day.get("day")] = [name for poi in pois for name in citation_check._extract_poi_names(poi)]
    return result


def _attempt_lifestyle_gap_supplement(project_id: str, topic: str, issues: list[dict]) -> list[dict]:
    """citation_check_node的MAX_RETRY耗尽分支专用：issues是verify_lifestyle_citations
    报出的问题列表（每条含field/missing_entities），按(field, entity)去重后逐个尝试自动
    补充，单次事件共享_MAX_AUTO_COLLECT_PER_TRIGGER这一个预算上限。

    missing_entities为空的issue（agent完全没写引用，而不是引用了错误实体）此前会被直接
    跳过、从不触发自动补充——真实E2E（黄龙溪古镇案例）发现这个bug后已修复：退化用同一天
    route_plan里的POI地名反推候选实体（该建议本来就是围绕这一天行程展开的，用当天POI
    作为检索线索是合理近似，不要求精确对应到具体推荐的实体）。详见docs/ISSUES.md第58条。
    """
    collected_all: list[dict] = []
    remaining = _MAX_AUTO_COLLECT_PER_TRIGGER
    seen: set[tuple[str, str]] = set()
    route_pois_by_day: Optional[dict[int, list[str]]] = None
    for issue in issues:
        field = issue.get("field", "")
        entities = issue.get("missing_entities") or []
        if not entities:
            if route_pois_by_day is None:
                route_pois_by_day = _route_poi_entities_by_day(project_id)
            entities = route_pois_by_day.get(issue.get("day"), [])
        for entity in entities:
            key = (field, entity)
            if key in seen or remaining <= 0:
                continue
            seen.add(key)
            seeds = _entity_gap_query_seeds(entity, _LIFESTYLE_FIELD_ANGLE_WORDS.get(field))
            collected = _attempt_auto_material_supplement(
                project_id, topic, seeds, seeds, label="lifestyle", limit=remaining
            )
            collected_all.extend(collected)
            remaining -= len(collected)
    return collected_all


def _run_poi_detail_citation_loop(
    project_id: str,
    topic: str,
    day: int,
    poi_name: str,
    agent,
    route_names: Optional[list[str]] = None,
    thread_id: Optional[str] = None,
) -> str:
    """POI详情文件的核验+修复重试循环，poi_detail_agent_node（首次生成/"树木生成中"阶段
    并行分支）和poi_detail_edit_node（edit入口定点编辑）共用，避免重复实现同一段重试逻辑。

    route_names为None（默认）时核验全部路线（跟原来poi_detail_agent_node的行为完全一致）；
    传入具体路线名列表时把核验范围收窄到这些路线——poi_detail_edit_node用它把核验范围收窄到
    这次编辑真的改动/新增的路线，未涉及路线不重新核验（跟citation_check_node的fields/
    item_keys参数同一个思路，见docs/ISSUES.md第37补条）。

    返回最终的citation_status（"passed"/"force_finalized"）。
    """
    citation_status = "passed"
    initial_detail = store.read_poi_detail(project_id, day, poi_name)
    retry_history: list[str] = list(initial_detail.retry_history or []) if initial_detail else []
    patched_segments: set[tuple[str, int]] = set()

    def _has_nonempty_content() -> bool:
        current = store.read_poi_detail(project_id, day, poi_name)
        return bool(current and any(route.route_segments for route in current.routes))

    if not _has_nonempty_content():
        retry_history.append(
            "content_empty：首次产物没有形成任何含途经点的路线，返回同一POI角色局部返修。"
        )
        content_prompt = (
            f"自动核验发现Day{day}「{poi_name}」的POI详情为空。你的职责是写攻略和提醒风险，"
            "不是决定固定路线中的景点去留。当前关闭、开放不确定或没有官方发布的完整内部游线，"
            "都不能作为routes=[]的理由。请使用已经检索到的UGC和可核验事实重新完成这个exact target："
            "地点身份和外部亮点必须有依据；路线顺序属于本次规划，可标为estimate或derived；"
            "没有内部子点时可把POI本身作为单个途经点，时长写成非数字的灵活安排；"
            "不得虚构入口、岔路、观景台或内部路径。不要调用素材补采，只调用write_agent_output"
            "覆盖写入当前POI的非空完整routes。"
        )
        try:
            _invoke_with_write_verification(
                agent,
                content_prompt,
                verify_fn=_has_nonempty_content,
                retry_note=(
                    "当前POI仍然没有形成含途经点的非空routes。请停止解释为什么不能游览，"
                    "立即按职责写出有依据的最小玩法并调用write_agent_output；不能虚构内部点位。"
                ),
                # The helper performs one call before its retry loop. One
                # retry therefore equals the two bounded local repair rounds.
                max_retries=1,
                thread_id=thread_id,
            )
        except Exception as exc:  # noqa: BLE001 - explicit branch failure, siblings continue
            failure_summary = (
                f"poi_detail_content_empty after bounded repair: {type(exc).__name__}: {exc}"
            )[:480]
            retry_history.append(f"content_empty返修耗尽：{failure_summary}")
            store.mark_poi_generation_failed(
                project_id,
                day,
                poi_name,
                failure_summary=failure_summary,
                retry_history=retry_history,
            )
            return "generation_failed"
        initial_detail = store.read_poi_detail(project_id, day, poi_name)

    def _history_applies_to_scope() -> bool:
        if initial_detail is None:
            return False
        if initial_detail.citation_status == "force_finalized":
            return True
        if not retry_history:
            return False
        if route_names is None:
            return True
        return any(
            route_name in history
            for route_name in route_names
            for history in retry_history
        )

    existing_failure_requires_confirmation = _history_applies_to_scope()
    repair_agent = agent
    if getattr(agent, "checkpointer", None) is not None:
        # Citation repair only needs the persisted POI file plus the current
        # issue list. Reusing the research checkpoint drags the entire material
        # exploration/tool history into every patch request (a real Chengdu
        # resume reached 91,924 input tokens). Keep the same role and tools but
        # start a stateless repair conversation for this narrow task.
        repair_agent = build_poi_detail_agent(
            project_id,
            topic,
            day,
            poi_name,
            checkpointer=False,
            repair_only=True,
        )
    repair_retry_kwargs = (
        {"max_retries": 1, "recursion_limit": 10} if repair_agent is not agent else {}
    )

    transport_retry_backoff_s = (30.0, 60.0, 90.0, 120.0, 180.0, 240.0)

    def _transport_retry_delay(
        exc: Exception,
        scheduled_delay: float,
    ) -> float | None:
        if isinstance(exc, openai.APITimeoutError):
            return scheduled_delay
        if type(exc) is openai.APIError and (
            "Model output became abnormal while generating a JSON response"
            in str(exc)
        ):
            # Some OpenAI-compatible providers abort a response_format request
            # after accepting it and explicitly ask the caller to retry. This
            # is a transient generation failure, not a citation verdict.
            return scheduled_delay
        if not isinstance(exc, openai.APIStatusError):
            return None
        if getattr(exc, "status_code", None) not in {502, 503, 504}:
            return None
        body = getattr(exc, "body", None)
        retry_after = body.get("retry_after") if isinstance(body, dict) else None
        try:
            provider_delay = min(max(float(retry_after), 0.0), 240.0)
            return max(scheduled_delay, provider_delay)
        except (TypeError, ValueError):
            return scheduled_delay

    def _call_with_transport_backoff(call, *, label: str):
        """同一无副作用/幂等调用按固定阶梯退避，始终留在当前业务轮次。"""
        for attempt in range(len(transport_retry_backoff_s) + 1):
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - 只有明确的技术错误会进入退避
                scheduled_delay = (
                    transport_retry_backoff_s[attempt]
                    if attempt < len(transport_retry_backoff_s)
                    else 0.0
                )
                delay = _transport_retry_delay(exc, scheduled_delay)
                if delay is None or attempt == len(transport_retry_backoff_s):
                    raise
                retry_history.append(
                    f"技术重试：{label}遇到{type(exc).__name__}"
                    f"（status={getattr(exc, 'status_code', None)}），"
                    f"第{attempt + 1}/{len(transport_retry_backoff_s)}次退避{delay:g}秒后原样重试。"
                )
                if delay:
                    time.sleep(delay)

    def _invoke_patch_with_transport_retry(
        target_agent,
        messages: list[dict],
        *,
        verify_fn=None,
        **kwargs,
    ):
        """瞬时超时/502/503/504对同一窄patch阶梯退避，不消耗业务返修轮次。"""
        def _invoke_once():
            try:
                return invoke_with_retry(target_agent, messages, **kwargs)
            except GraphRecursionError:
                # 与主写入Harness保持同一原则：工具副作用已经真实落盘时，Agent写完后
                # 继续行动触发的图步数边界不能覆盖成功事实；后续仍会重新跑引用核验。
                if verify_fn is not None and verify_fn():
                    return None
                raise

        # 其它调用若保留invoke_with_retry默认值，内部已经做过多次技术重试；只给
        # 明确收窄为单次调用的stateless patch补这组退避，避免嵌套放大请求数。
        if kwargs.get("max_retries") != 1:
            return _invoke_once()
        return _call_with_transport_backoff(
            _invoke_once,
            label="POI局部patch",
        )

    def _repair_segments_view(
        detail_dict: dict,
        segment_keys: list[tuple[str, int]],
    ) -> list[dict]:
        """只暴露本轮需要返修的途经点；route级字段和其它途经点由store保留。"""
        selected = []
        wanted = {(str(route_name), int(order)) for route_name, order in segment_keys}
        for route in detail_dict.get("routes") or []:
            route_name = str(route.get("route_name") or "")
            route_orders = {
                order for wanted_route, order in wanted if wanted_route == route_name
            }
            if not route_orders:
                continue
            route_view = {"route_name": route_name}
            route_view["route_segments"] = []
            for segment in route.get("route_segments") or []:
                if int(segment.get("order")) not in route_orders:
                    continue
                segment_view = {
                    key: copy.deepcopy(segment.get(key))
                    for key in (
                        "order",
                        "point_name",
                        "highlight",
                        "suggested_duration",
                        "image_prompt",
                    )
                }
                segment_view["claims"] = [
                    {
                        key: copy.deepcopy(claim.get(key))
                        for key in (
                            "target_path",
                            "claim_text",
                            "claim_type",
                            "support_mode",
                            "claimed_entities",
                            "evidence_refs",
                            "derived_from",
                        )
                    }
                    for claim in segment.get("claims") or []
                ]
                route_view["route_segments"].append(segment_view)
            selected.append(route_view)
        return selected

    def _segment_snapshot(route_name: str, order: int) -> str:
        current = store.read_poi_detail(project_id, day, poi_name)
        current_dict = current.model_dump(mode="json") if current else {}
        return json.dumps(
            _repair_segments_view(current_dict, [(route_name, order)]),
            ensure_ascii=False,
            sort_keys=True,
        )

    def _verify_detail_with_transport_retry(
        detail_dict: dict,
        names: Optional[list[str]],
        segment_keys: Optional[list[tuple[str, int]]] = None,
    ) -> list[dict]:
        """POI核验是无副作用读操作；瞬时传输失败时按同一阶梯退避。"""
        return _call_with_transport_backoff(
            lambda: citation_check.verify_poi_detail_citations(
                detail_dict,
                topic,
                route_names=names,
                project_id=project_id,
                log_scope=f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}",
                segment_keys=segment_keys,
            ),
            label="POI核验",
        )

    def _verify_current(
        names: Optional[list[str]],
        segment_keys: Optional[list[tuple[str, int]]] = None,
    ) -> tuple[dict, list[dict]]:
        current = store.read_poi_detail(project_id, day, poi_name)
        current_dict = current.model_dump(mode="json") if current else {}
        current_issues = _verify_detail_with_transport_retry(
            current_dict,
            names,
            segment_keys,
        )
        return current_dict, current_issues

    def _issue_summary(issues: list[dict]) -> str:
        return "；".join(
            _format_poi_claim_issue(i) for i in issues
        )

    def _format_poi_claim_issue(issue: dict) -> str:
        location = f"{issue['route_name']}途经点「{issue['point_name']}」"
        claim_id = str(issue.get("claim_id") or "").strip()
        if not claim_id:
            return f"{location}：{issue['reason']}"
        return (
            f"{location}的失败claim：claim_id={claim_id}，"
            f"target_path={issue.get('target_path') or ''}，"
            f"当前support_mode={issue.get('support_mode') or ''}，"
            f"原句=「{issue.get('claim_text') or issue.get('label') or ''}」，"
            f"失败原因={issue['reason']}"
        )

    def _confirm_pass(
        names: Optional[list[str]],
        segment_keys: Optional[list[tuple[str, int]]] = None,
    ) -> bool:
        """已有失败历史或刚patch的目标必须连续两次通过。

        第一次通过、确认却失败属于核验结论冲突。此时不再让生成Agent追着非确定性判断
        继续改内容，而是保留force_finalized交给现有人工复核中心。
        """
        _, confirmation_issues = _verify_current(names, segment_keys)
        if confirmation_issues:
            retry_history.append(f"通过确认冲突：{_issue_summary(confirmation_issues)}")
            store.mark_poi_force_finalize(
                project_id,
                day,
                poi_name,
                retry_history=retry_history,
            )
            return False
        store.mark_poi_citation_passed(
            project_id,
            day,
            poi_name,
            retry_history=retry_history,
        )
        return True

    for attempt in range(MAX_RETRY + 1):
        segment_scope = sorted(patched_segments) if patched_segments else None
        detail_dict, issues = _verify_current(route_names, segment_scope)

        if not issues:
            if patched_segments or existing_failure_requires_confirmation:
                if not _confirm_pass(
                    None if patched_segments else route_names,
                    segment_scope,
                ):
                    citation_status = "force_finalized"
            break

        summary = _issue_summary(issues)
        retry_history.append(f"第{attempt + 1}轮：{summary}")

        if attempt == MAX_RETRY:
            # docs/ARCHITECTURE.md第16节：先尝试判定真缺+在项目级预算内自动补充采集；
            # 预算耗尽/判定不是真缺/采集本身失败时不再有"只呈现候选"的兜底路径（原本的
            # _suggest_collection_candidates没有相关性/内容安全过滤、也从未真正被人工
            # 审核环节消费，已于docs/ISSUES.md第98条移除——真实撞见过一条不相关的
            # 成人内容视频标题被原样写进final_guide.md）。
            query_seeds = [f"{poi_name} 讲解", f"{poi_name} 攻略"]
            auto_collected = _attempt_auto_material_supplement(
                project_id, topic, query_seeds, query_seeds,
                label="poi_detail", limit=_MAX_AUTO_COLLECT_PER_TRIGGER,
            )
            if auto_collected:
                repair_segments = sorted(
                    {(str(i["route_name"]), int(i["order"])) for i in issues}
                )
                evidence_repair_agent = repair_agent
                evidence_repair_kwargs = repair_retry_kwargs
                if repair_agent is not agent:
                    evidence_repair_agent = build_poi_detail_agent(
                        project_id,
                        topic,
                        day,
                        poi_name,
                        checkpointer=False,
                        repair_only=True,
                        repair_allow_retrieval=True,
                    )
                    evidence_repair_kwargs = {"max_retries": 1}
                try:
                    for route_name, order in repair_segments:
                        before_segment = _segment_snapshot(route_name, order)
                        segment_issues = [
                            i
                            for i in issues
                            if str(i["route_name"]) == route_name
                            and int(i["order"]) == order
                        ]
                        segment_summary = "；".join(
                            _format_poi_claim_issue(i) for i in segment_issues
                        )
                        repair_prompt = (
                            f"系统刚为当前素材库新增了{len(auto_collected)}条已经完成入库、现在可被"
                            f"retrieve_materials检索到的素材。请只回炉修正路线「{route_name}」中"
                            f"order={order}的核验失败途经点，重新检索新素材并更新sources；"
                            f"其他路线和途经点不要改。修正后调用write_agent_output_patch，"
                            f"routes中只提交route_name和这个途经点的完整修正版。"
                            f"问题清单：{segment_summary}\n当前途经点（已去掉证据快照）："
                            f"{json.dumps(_repair_segments_view(detail_dict, [(route_name, order)]), ensure_ascii=False)}"
                        )
                        _invoke_patch_with_transport_retry(
                            evidence_repair_agent,
                            [{"role": "user", "content": repair_prompt}],
                            verify_fn=lambda route_name=route_name, order=order, before_segment=before_segment: (
                                _segment_snapshot(route_name, order) != before_segment
                            ),
                            **evidence_repair_kwargs,
                        )
                        patched_segments.add((route_name, order))
                    repaired = store.read_poi_detail(project_id, day, poi_name)
                    repaired_dict = repaired.model_dump(mode="json") if repaired else {}
                    repaired_issues = _verify_detail_with_transport_retry(
                        repaired_dict,
                        None,
                        repair_segments,
                    )
                except Exception as exc:
                    repaired_issues = [
                        {**issue, "reason": f"补采后局部回炉执行失败：{exc}"} for issue in issues
                    ]
                if not repaired_issues:
                    if not _confirm_pass(None, repair_segments):
                        citation_status = "force_finalized"
                    break
                repaired_summary = "；".join(
                    f"{i['route_name']}途经点「{i['point_name']}」：{i['reason']}"
                    for i in repaired_issues
                )
                retry_history.append(f"补采后局部回炉：{repaired_summary}")
            store.mark_poi_force_finalize(
                project_id, day, poi_name,
                auto_collected=auto_collected,
                retry_history=retry_history,
            )
            citation_status = "force_finalized"
            break

        issue_segments = sorted(
            {(str(i["route_name"]), int(i["order"])) for i in issues}
        )
        for route_name, order in issue_segments:
            before_segment = _segment_snapshot(route_name, order)
            segment_issues = [
                i
                for i in issues
                if str(i["route_name"]) == route_name and int(i["order"]) == order
            ]
            segment_summary = "；".join(
                _format_poi_claim_issue(i) for i in segment_issues
            )
            fix_prompt = (
                f"自动核验发现路线「{route_name}」中order={order}的途经点引用不支持描述内容。"
                f"问题清单：{segment_summary}\n"
                f"请只针对这个途经点收窄描述、去掉无依据的具体断言；不要重新检索，"
                f"不要修改其他途经点。{_missing_entities_retry_hint(segment_issues)}"
                f"若失败claim把外部事实和本次路线编排揉在一起，必须拆成独立claim："
                f"只有原文直接支持的部分可保留为direct；“本方案沿用”、“适合顺路游览”"
                f"等编排判断应根据来源改为user_constraint、derived或estimate，"
                f"不得继续共用外部素材的direct引用。"
                f"如果问题来自没有来源支持的建议时长，不要继续把经验分钟数标成direct；"
                f"改成不含具体数字的灵活安排，并把对应claim标成estimate。"
                f"{_findings_retry_hint(project_id, f'poi_detail_agent/day{day}_{store.poi_slug(poi_name)}')}"
                f"修正后调用write_agent_output_patch，routes中只提交route_name和这个途经点的"
                f"完整修正版；未提交的途经点由程序保留。\n"
                f"当前途经点（已去掉证据快照）："
                f"{json.dumps(_repair_segments_view(detail_dict, [(route_name, order)]), ensure_ascii=False)}"
            )
            try:
                _invoke_patch_with_transport_retry(
                    repair_agent,
                    [{"role": "user", "content": fix_prompt}],
                    verify_fn=lambda route_name=route_name, order=order, before_segment=before_segment: (
                        _segment_snapshot(route_name, order) != before_segment
                    ),
                    **repair_retry_kwargs,
                )
            except Exception as exc:  # noqa: BLE001 - bounded repair failure goes to human review
                failure_kind = (
                    "技术退避耗尽"
                    if _transport_retry_delay(exc, 0.0) is not None
                    else "未落盘"
                )
                retry_history.append(
                    f"路线「{route_name}」order={order}途经点局部返修{failure_kind}：{exc}"
                )
                store.mark_poi_force_finalize(
                    project_id,
                    day,
                    poi_name,
                    retry_history=retry_history,
                )
                return "force_finalized"
            patched_segments.add((route_name, order))

    return citation_status


def _poi_detail_thread_id(state: GraphState) -> Optional[str]:
    """返回一次POI详情生成/编辑的稳定agent thread；兼容旧的无turn_id state。"""
    turn_id = state.get("turn_id")
    if not turn_id:
        return None
    return poi_detail_agent_thread_id(
        state["project_id"], state["day"], state["poi_name"], turn_id,
    )


def _poi_detail_checkpoint_status(agent, thread_id: Optional[str]) -> tuple[bool, bool]:
    """返回(thread是否存在, 是否仍停在未完成节点)。"""
    if not thread_id or getattr(agent, "checkpointer", None) is None:
        return False, False
    try:
        snapshot = agent.get_state({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 - checkpoint读取失败时退化为一次全新生成
        return False, False
    return bool(snapshot.values), bool(snapshot.next)


def _classify_poi_generation_failure(exc: Exception) -> tuple[str | None, str]:
    """Classify recoverable POI branch failures without hiding code defects."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    full_summary = " | ".join(
        f"{type(item).__name__}: {str(item)}" for item in chain
    ).replace("\n", " ")
    summary = full_summary[:240]
    lowered = full_summary.casefold()
    provider_markers = (
        "timeout",
        "timed out",
        "connection",
        "rate limit",
        "429",
        "502",
        "503",
        "504",
        "gateway",
        "all configured keys exhausted",
        "渠道",
    )
    if any(marker in lowered for marker in provider_markers):
        return "provider_or_tool_failure", summary
    if isinstance(exc, RuntimeError) and any(
        marker in lowered
        for marker in ("仍未完成必需的写入", "没有真正调用", "write_agent_output")
    ):
        return "write_contract_failure", summary
    return None, summary


def _poi_detail_log_scope(day: int, poi_name: str) -> str:
    return f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}"


def _load_poi_recovery_working_notes(
    project_id: str,
    log_scope: str,
) -> list[dict]:
    """Load only this POI work unit's durable, non-citable working notes."""
    path = store.project_dir(project_id) / "agents" / log_scope / "findings.jsonl"
    if not path.exists():
        return []
    notes: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("note_type") != "working_note":
            continue
        notes.append(
            {
                "fact": str(record.get("fact", "")).strip(),
                "created_at": str(record.get("created_at", "")),
            }
        )
    return [note for note in notes if note["fact"]]


def _poi_recovery_evidence_view(project_id: str, log_scope: str) -> list[dict]:
    """Build compact candidate metadata without asserting that it is true."""
    records = list_project_evidence_for_log_scope(
        project_id,
        log_scope,
        projects_dir=store.project_dir(project_id).parent,
    )
    return [
        {
            "chunk_id": f"project-evidence-{record.note_id}",
            "title": record.source_title,
            "summary": record.summary,
            "source_url": record.source_url,
            "source_query": record.source_query,
            "source_observed_at": record.source_observed_at,
        }
        for record in records
    ]


def _build_poi_provider_recovery_prompt(
    *,
    poi_name: str,
    day: int,
    needs_detail_reason: str,
    route_plan: dict,
    requirements: dict,
    evidence: list[dict],
    working_notes: list[dict],
) -> str:
    return (
        "POI首次生成的研究阶段已经完成，但原请求在结构化写入时遇到Provider故障。"
        "这是一次受限恢复：消费已持久化素材并提交，不重新调研。\n"
        f"景点名称：{poi_name}\n"
        f"所在天数：Day{day}\n"
        f"当天路线背景：{json.dumps(route_day_view(route_plan, day), ensure_ascii=False)}\n"
        f"值得深挖的理由：{needs_detail_reason}\n"
        f"用户需求：{json.dumps(requirements.get('requirements', {}), ensure_ascii=False)}\n"
        "下列只是归属于当前POI工作单元的候选Evidence，并不代表每条结论都正确。"
        "需要原文时可用retrieve_materials按POI或具体专名检索，但不得扩展到新的联网研究：\n"
        f"{json.dumps(evidence, ensure_ascii=False)}\n"
        "下列working notes是待重新核对的研究提示，不是可引用事实；"
        "必须回到Evidence内容确认后才能写成direct Claim：\n"
        f"{json.dumps(working_notes, ensure_ascii=False)}\n"
        "请生成保守、可执行的POI内部路线。不足以支持的具体价格、时间、固定入口或途经点应省略、"
        "收窄或标成estimate/unverified。最终必须调用write_agent_output写入完整routes，不要只输出文字。"
    )


def _attempt_poi_provider_recovery(
    *,
    project_id: str,
    topic: str,
    day: int,
    poi_name: str,
    needs_detail_reason: str,
    route_plan: dict,
    requirements: dict,
) -> tuple[bool, str]:
    """Try one stateless, write-focused recovery after GPT-Load retries exhaust."""
    from src.llm_rotation import poi_recovery_model_identity

    log_scope = _poi_detail_log_scope(day, poi_name)
    evidence = _poi_recovery_evidence_view(project_id, log_scope)
    working_notes = _load_poi_recovery_working_notes(project_id, log_scope)
    recovery_identity = poi_recovery_model_identity()
    event = {
        "day": day,
        "poi_name": poi_name,
        "trigger": "provider_retry_exhausted",
        "source_log_scope": log_scope,
        "candidate_evidence_count": len(evidence),
        "working_note_count": len(working_notes),
        "recovery_provider": recovery_identity["provider"],
        "recovery_model": recovery_identity["model"],
    }
    append_context_event({"status": "poi_generation_recovery_started", **event})

    recovery_agent = build_poi_detail_agent(
        project_id,
        topic,
        day,
        poi_name,
        checkpointer=False,
        provider_recovery=True,
    )
    prompt = _build_poi_provider_recovery_prompt(
        poi_name=poi_name,
        day=day,
        needs_detail_reason=needs_detail_reason,
        route_plan=route_plan,
        requirements=requirements,
        evidence=evidence,
        working_notes=working_notes,
    )

    def _poi_written() -> bool:
        detail = store.read_poi_detail(project_id, day, poi_name)
        return bool(detail and detail.routes)

    try:
        # One logical recovery attempt. invoke_with_retry may transparently
        # repeat transport failures, but no second research/write conversation
        # is created after a completed response that failed to write.
        _invoke_with_write_verification(
            recovery_agent,
            prompt,
            verify_fn=_poi_written,
            retry_note="Provider恢复调用未写入POI文件。",
            max_retries=0,
            thread_id=None,
            agent_recursion_limit=10,
        )
    except Exception as exc:  # noqa: BLE001 - unknown code defects must remain visible
        failure_kind, failure_summary = _classify_poi_generation_failure(exc)
        append_context_event(
            {
                "status": "poi_generation_recovery_failed",
                **event,
                "failure_kind": failure_kind or "unknown",
                "failure_summary": failure_summary,
            }
        )
        if failure_kind is None:
            raise
        return False, failure_summary

    append_context_event({"status": "poi_generation_recovery_succeeded", **event})
    return True, ""


def poi_detail_agent_node(state: GraphState) -> dict:
    """"树木生成中"阶段的并行分支，被Send API为每个needs_detail=true的POI各自派发一次。
    每个实例独立完成：检索+生成routes → 内嵌citation_check核验循环（复用_verify_items，
    跟route_citation_check_node/citation_check_node同款实现，见docs/ORCHESTRATION.md第4.1节）
    → 写入自己的outputs/poi_details/day{N}_{poi_slug}.json。

    一个分支的核验打回不影响其他分支——这里没有任何跨分支共享的可变状态，每个分支只操作
    自己的那个文件；打回耗尽时调用按POI粒度的store.mark_poi_force_finalize，不是project
    粒度的store.mark_force_finalize，避免误伤"整体是否完成"的判断（其他POI、以及route/
    lifestyle阶段已经核验通过的状态都不该被这一个POI的核验失败连累）。
    """
    project_id, topic = state["project_id"], state["topic"]
    _assert_formal_comparison_baseline(project_id, topic)
    day, poi_name = state["day"], state["poi_name"]
    needs_detail_reason = state.get("needs_detail_reason", "")

    agent = build_poi_detail_agent(project_id, topic, day, poi_name)
    thread_id = _poi_detail_thread_id(state)
    has_checkpoint, checkpoint_pending = _poi_detail_checkpoint_status(agent, thread_id)

    # 每个POI是Send API独立并行分支，scope按day+poi_slug隔离，不会跟其他POI分支串证据。
    # 新一轮生成才清空日志；恢复已有agent checkpoint时必须保留之前查到的web证据。
    if not has_checkpoint:
        reset_web_search_log(project_id, f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}")

    set_trace_context(project_id, "poi_detail_agent")
    requirements = store.read_requirements(project_id) or {}
    route_plan = store.read_workspace(project_id, "route_plan.json") or {}
    formal_material_context = ""
    if _is_formal_comparison(project_id):
        response = research_service.retrieve_context(
            project_id,
            actor="poi_detail_agent",
            stage="poi_detail",
            target_key=f"poi_detail:day:{day}:poi:{poi_name}",
            query=f"{poi_name} 内部路线 攻略",
            top_k=8,
        )
        formal_material_context = (
            "\nA级正式对照的共享素材预检结果："
            + json.dumps(
                _compact_research_results(response["results"]), ensure_ascii=False
            )
        )

    prompt = (
        f"请为这个重点景点生成深度玩法路线：\n"
        f"景点名称：{poi_name}\n"
        f"所在天数：Day{day}\n"
        f"当天路线背景：{json.dumps(route_day_view(route_plan, day), ensure_ascii=False)}\n"
        f"当初被判断值得深挖的理由：{needs_detail_reason}\n"
        f"用户需求（判断recommended路线时参考）：{json.dumps(requirements.get('requirements', {}), ensure_ascii=False)}\n"
        f"请检索素材库、梳理这个景点内部的具体路线（可能不止一条），拆解出有序的途经点列表，"
        f"每个途经点附上sources，调用write_agent_output写入。{formal_material_context}"
    )

    def _poi_written() -> bool:
        detail = store.read_poi_detail(project_id, day, poi_name)
        return bool(detail and detail.routes)

    # These fields are added to the branch result only if the provider recovery
    # path actually runs; normal callers keep the historical result shape.
    generation_mode = "normal"
    recovery_trigger = None
    try:
        # 如果上一次已经完成react-agent调用且文件也已落盘，说明崩溃发生在
        # 节点收尾/核验阶段，直接进入核验，不重复生成；否则按checkpoint状态决定
        # 是从中断的工具循环resume，还是在同一thread开启一次新的尝试。
        if not (has_checkpoint and _poi_written()):
            _invoke_with_write_verification(
                agent,
                prompt,
                verify_fn=_poi_written,
                retry_note="你刚才的回复没有真正调用write_agent_output工具写入POI详情文件，只有文字总结是不够的。"
                "请现在立刻调用write_agent_output工具，把routes数组的完整内容写进去。",
                thread_id=thread_id,
                resume_from_checkpoint=checkpoint_pending,
            )
    except Exception as exc:  # noqa: BLE001 - only known recoverable classes are absorbed
        # 2026-07-21真实E2E（chengdu-3d-e2e-v19）首次触发：4个并行POI分支里，"大熊猫繁育
        # 研究基地"这一个分支连续3次尝试（初次+2次重试）都没有真正调用write_agent_output，
        # _invoke_with_write_verification按设计抛出的写入失败，或最终耗尽的渠道/工具异常——但这是LangGraph Send
        # 并行分支之一，异常会直接冲垮整个graph.invoke()，让另外3个已经成功完成的POI
        # （武侯祠/杜甫草堂/三星堆当时都已正常写入）也白白陪葬：join节点永远不会执行，
        # manifest.json不会生成，final_guide.md不会反向同步，project永久卡在"树木生成中"
        # 无法自然恢复。这跟"一个POI核验不通过不该阻塞其他POI"（mark_poi_force_finalize）
        # 是同一类原则，只是之前只覆盖了"核验没通过"，没覆盖"agent完全没有产出"这种更彻底
        # 的失败。这里捕获后不再往上抛，直接标记为generation_failed、跳过citation_check循环
        # （没有文件可核验），让这个分支体面结束，其他分支和join流程完全不受影响。见docs/ISSUES.md。
        failure_kind, failure_summary = _classify_poi_generation_failure(exc)
        if failure_kind is None:
            raise
        # A provider can fail after the write tool has already committed the
        # file. In that case the durable side effect wins and citation checking
        # continues; recovery is only for a genuinely missing first output.
        if not _poi_written():
            recovered = False
            recovery_summary = ""
            if failure_kind == "provider_or_tool_failure":
                recovered, recovery_summary = _attempt_poi_provider_recovery(
                    project_id=project_id,
                    topic=topic,
                    day=day,
                    poi_name=poi_name,
                    needs_detail_reason=needs_detail_reason,
                    route_plan=route_plan,
                    requirements=requirements,
                )
                generation_mode = "provider_recovery"
                recovery_trigger = "provider_retry_exhausted"
            if not recovered:
                combined_summary = failure_summary
                if recovery_summary:
                    combined_summary = (
                        f"{failure_summary} | provider_recovery_failed: {recovery_summary}"
                    )[:480]
                failure_result = {
                    "day": day,
                    "poi_name": poi_name,
                    "citation_status": "generation_failed",
                    "failure_kind": failure_kind,
                    "failure_summary": combined_summary,
                }
                if generation_mode == "provider_recovery":
                    failure_result.update(
                        generation_mode=generation_mode,
                        recovery_trigger=recovery_trigger,
                    )
                return {"poi_detail_results": [failure_result]}

    citation_status = _run_poi_detail_citation_loop(
        project_id, topic, day, poi_name, agent, thread_id=thread_id,
    )

    result = {"day": day, "poi_name": poi_name, "citation_status": citation_status}
    if citation_status == "generation_failed":
        result.update(
            failure_kind="write_contract_failure",
            failure_summary="poi_detail_content_empty after bounded local repair",
        )
    if generation_mode == "provider_recovery":
        result.update(
            generation_mode=generation_mode,
            recovery_trigger=recovery_trigger,
        )
    return {"poi_detail_results": [result]}


def _diff_poi_routes(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """POI详情文件的路线级diff，跟_changed_route_days同一个思路（见docs/ISSUES.md第37条），
    粒度是"route_name"（对应"一个POI可能有多条路线"这个已有的多骨架设计，见
    docs/ARCHITECTURE.md第3.4节第6点）。只在非首次生成场景调用（before一定不为None）。

    返回(needs_recheck, touched)两个route_name列表：
    - needs_recheck：需要重新核验的路线——新增的路线 + 内容（除recommended/recommend_reason
      外）确实发生变化的既有路线。比较时刻意排除recommended/recommend_reason这两个字段：
      多骨架备选场景下"切换推荐路线"天然会同时翻转两条路线的这两个字段，但这不涉及任何
      新的事实性陈述，不该触发citation_check（也不该被误判成"改动了2条路线的内容"）。
    - touched：用于越权改动检测的完整改动集合（needs_recheck + 被删除的路线名）——删除
      整条路线虽然不需要核验（内容已经不存在），但仍然是一次实质改动，要计入"这次edit
      到底碰了几条路线"的越权判断（跟lifestyle_agent_node同款">1即可疑"阈值，见
      poi_detail_edit_node）。
    """
    before_routes = {r.get("route_name"): r for r in (before or {}).get("routes", []) or []}
    after_routes = {r.get("route_name"): r for r in (after or {}).get("routes", []) or []}

    def _content_only(route: dict) -> dict:
        return {k: v for k, v in route.items() if k not in ("recommended", "recommend_reason")}

    needs_recheck = [
        name
        for name, a in after_routes.items()
        if name not in before_routes or _content_only(before_routes[name]) != _content_only(a)
    ]
    removed = [name for name in before_routes if name not in after_routes]
    return needs_recheck, needs_recheck + removed


def poi_detail_edit_node(state: GraphState) -> dict:
    """POI详情agent的edit入口（docs/ARCHITECTURE.md第3.4节第9点此前"这次不做"的局限之一，
    2026-07-26接入）。跟route_draft_node/lifestyle_agent_node/pitfall_agent_node同款
    "定点编辑"模式，粒度是"路线"（route_name）——对称route_agent的"天"、lifestyle_agent的
    "字段"。entry_target="poi_detail_edit_node"，需要额外通过run_edit()的day/poi_name
    参数指定具体是哪一个POI（跟Send fan-out分支复用GraphState同名字段，语义一致——都表示
    "这次要处理的POI是哪个"，见GraphState字段注释）。

    支持四种意图（由模型从edit_request自然语言判断，不做硬编码字符串匹配）：
    ①修改某条已有路线的具体内容；②切换推荐哪条路线（多骨架备选——schema早就支持
    routes数组+recommended标记，此前只有生成时模型自己判断一次，没有编辑入口让用户事后
    调整，见docs/ARCHITECTURE.md第3.4节第6点）；③新增一条此前不存在的路线；④删除某条
    已有路线。①②使用write_agent_output_patch：路线级字段按route_name更新，途经点按
    (route_name, order)分层合并，未涉及的路线和途经点不需要模型原样抄写；③④涉及路线
    数量变化，需要模型对"这个POI到底该有几条路线"做整体判断，
    走整份覆盖（write_agent_output，跟write_route_plan"增删天数走整份覆盖"同一个理由，
    见docs/ISSUES.md相关记录，双工具并存模式仿route_draft_node）。

    还支持"首次生成"——如果这个POI当初没有被route_agent标记needs_detail=true（或者
    "树木生成中"阶段整体被跳过、从未生成过这个POI的详情文件），用户事后单独要求补充生成，
    这里同样可以处理，是对"只在骨架阶段一次性判断"这条设计的自然补充，不是重新设计整个
    自动触发机制（自动fan-out的触发条件本身不变）。

    核验/打回复用_run_poi_detail_citation_loop（跟poi_detail_agent_node共用同一份实现），
    核验范围收窄到这次真的改动/新增的路线（route_names参数），未涉及路线不重新核验
    （跟citation_check_node的fields/item_keys参数同一个思路，见docs/ISSUES.md第37补条）。

    这是一跳结束的edit入口（不级联citation_check_node/pitfall_agent/integration_agent——
    POI详情本来就不在AGENT_DEPENDENCY_TABLE的级联链路里，plan模式"整合完成后自动触发"
    和这里的"用户事后定点编辑"是两条独立的路径），见_route_edit_mode的
    "last_agent == poi_detail_agent"分支。
    """
    project_id, topic = state["project_id"], state["topic"]
    day, poi_name = state["day"], state["poi_name"]
    edit_request = state["edit_request"]

    existing_detail = store.read_poi_detail(project_id, day, poi_name)
    is_first_generation = existing_detail is None

    agent = build_poi_detail_agent(project_id, topic, day, poi_name)
    thread_id = _poi_detail_thread_id(state)
    has_checkpoint, checkpoint_pending = _poi_detail_checkpoint_status(agent, thread_id)

    # 这是一次全新的POI详情编辑生成尝试时清空证据日志；同一turn的进程恢复不能清空。
    if not has_checkpoint:
        reset_web_search_log(project_id, f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}")

    set_trace_context(project_id, "poi_detail_agent")
    requirements = store.read_requirements(project_id) or {}

    if is_first_generation:
        original_snapshot = None
        prompt = (
            f"这个景点此前没有生成过深度玩法路线，用户现在单独要求补充生成：\n"
            f"景点名称：{poi_name}\n"
            f"所在天数：Day{day}\n"
            f"用户的具体要求：{edit_request}\n"
            f"用户需求（判断recommended路线时参考）：{json.dumps(requirements.get('requirements', {}), ensure_ascii=False)}\n"
            f"请检索素材库、梳理这个景点内部的具体路线（可能不止一条），拆解出有序的途经点列表，"
            f"每个途经点附上sources，调用write_agent_output写入。"
        )
    else:
        original_snapshot = state.get("edit_original_poi_detail")
        if original_snapshot is None:
            original_snapshot = existing_detail.model_dump(mode="json")
        route_names = [r.get("route_name") for r in original_snapshot.get("routes", [])]
        prompt = (
            f"这个景点当前已有的路线名称：{route_names}\n"
            f"用户提出的具体修改要求：{edit_request}\n"
            f"请判断这个要求属于以下哪一种，只做被要求的那一种改动：\n"
            f"①修改某条已有路线的具体内容（途经点/亮点/建议停留时间等）——调用"
            f"write_agent_output_patch，只提交这一条改动后的路线，不要动其它路线；\n"
            f"②切换推荐哪条路线（多骨架备选）——调用write_agent_output_patch，把旧的推荐"
            f"路线（recommended改成false）和新的推荐路线（recommended改成true）两条都提交"
            f"（不要只提交新的那一条，否则旧路线的recommended字段不会被清掉），只改"
            f"recommended/recommend_reason字段，不要重写route_segments内容；\n"
            f"③新增一条此前不存在的路线——调用write_agent_output（整份覆盖），传完整"
            f"routes数组（现有路线+新路线），现有路线内容必须和当前产出逐字一致；\n"
            f"④删除某条已有路线——调用write_agent_output（整份覆盖），传不包含被删除路线"
            f"的完整routes数组，其余保留路线内容必须逐字一致。\n"
            f"如果需要参考现有某条路线的具体内容，可以用read_workspace工具查询"
            f"outputs/poi_details/day{day}_{store.poi_slug(poi_name)}.json。"
            f"同一时刻只应该有一条路线的recommended为true。"
        )

    def _poi_written() -> bool:
        detail = store.read_poi_detail(project_id, day, poi_name)
        return bool(detail and detail.routes)

    # edit场景下文件在生成前就已经存在，不能仅凭文件存在判断本轮agent已完成；
    # 只有checkpoint已走到完成态、且文件存在时，才跳过重复生成。
    if not (has_checkpoint and not checkpoint_pending and store.read_poi_detail(project_id, day, poi_name)):
        _invoke_with_write_verification(
            agent,
            prompt,
            verify_fn=_poi_written,
            retry_note="你刚才的回复没有真正调用write_agent_output/write_agent_output_patch工具写入"
            "POI详情文件，只有文字总结是不够的。请现在立刻调用其中一个工具完成写入。",
            thread_id=thread_id,
            resume_from_checkpoint=checkpoint_pending,
        )

    needs_recheck: list[str] = []
    if not is_first_generation:
        after_snapshot = store.read_poi_detail(project_id, day, poi_name).model_dump(mode="json")
        needs_recheck, touched = _diff_poi_routes(original_snapshot, after_snapshot)
        if len(touched) > 1:
            # 用户的修改要求通常只涉及一条路线，一次改动2条以上视为可疑的越权改动（跟
            # lifestyle_agent_node/pitfall_agent_node同款">1即可疑"阈值）。不阻塞流程，走
            # 既有的report_conflict留痕机制——POI详情不在AGENT_DEPENDENCY_TABLE级联链路里，
            # 没有下游整合环节会自动复核这条冲突，留作人工在conflicts.jsonl里事后审查。
            store.report_conflict(
                project_id,
                actor="citation_checker",
                target=f"poi_detail_agent.day{day}_{store.poi_slug(poi_name)}",
                reason=f"edit模式下用户只要求「{edit_request}」，但实际改动了{touched}这"
                f"{len(touched)}条路线，可能误改了未涉及的部分，需要人工复核。",
                issue_type="poi_detail_edit_overreach",
            )

    if is_first_generation or needs_recheck:
        # 首次生成全量核验（route_names=None）；否则只核验真正需要重新核验的路线
        # （needs_recheck非空——新增/内容有实质变化的路线）。
        citation_status = _run_poi_detail_citation_loop(
            project_id, topic, day, poi_name, agent,
            route_names=(needs_recheck or None) if not is_first_generation else None,
            thread_id=thread_id,
        )
    else:
        # 纯粹切换推荐路线（或者压根没有任何路线内容变化）——没有任何新的事实性陈述需要
        # 核验，沿用原有citation_status，不做不必要的重新核验（避免_verify_items的
        # 非确定性判断误伤已经通过核验的内容，见docs/ISSUES.md第37补条同一个思路）。
        citation_status = existing_detail.citation_status

    # 反向同步manifest+final_guide：必须重建"全部已生成POI"的完整列表（读manifest获取），
    # 不能只传这一个被编辑的POI——_sync_poi_details_into_final_guide会整体重建"🌳"章节，
    # 只传一个会把其它POI的内容从final_guide.md里冲掉。
    manifest = store.read_workspace(project_id, "outputs/poi_details/manifest.json") or {}
    all_results = [
        {
            "day": e["day"],
            "poi_name": e["poi_name"],
            "citation_status": e["citation_status"],
            "failure_kind": e.get("failure_kind"),
            "failure_summary": e.get("failure_summary", ""),
            "generation_mode": e.get("generation_mode", "normal"),
            "recovery_trigger": e.get("recovery_trigger"),
        }
        for e in (manifest.get("generated") or [])
        if not (e["day"] == day and e["poi_name"] == poi_name)
    ]
    updated_result = {"day": day, "poi_name": poi_name, "citation_status": citation_status}
    if citation_status == "generation_failed":
        updated_result.update(
            failure_kind="write_contract_failure",
            failure_summary="poi_detail_content_empty after bounded local repair",
        )
    all_results.append(updated_result)

    entries = [
        PoiDetailManifestEntry(
            poi_name=r["poi_name"],
            day=r["day"],
            file=f"day{r['day']}_{store.poi_slug(r['poi_name'])}.json",
            citation_status=r["citation_status"],
            failure_kind=r.get("failure_kind"),
            failure_summary=r.get("failure_summary", ""),
            generation_mode=r.get("generation_mode", "normal"),
            recovery_trigger=r.get("recovery_trigger"),
        )
        for r in all_results
    ]
    store.write_poi_details_manifest(project_id, "main_agent", PoiDetailManifest(generated=entries))
    _sync_poi_details_into_final_guide(project_id, all_results)

    result: dict = {
        "last_agent_result": {
            "agent": "poi_detail_agent",
            "summary": f"POI详情已{'生成' if is_first_generation else '更新'}：{poi_name}",
        },
    }
    if not is_first_generation:
        result["edit_original_poi_detail"] = original_snapshot
    return result


def poi_details_join_node(state: GraphState) -> dict:
    """所有并行分支完成后自动join（LangGraph Send/map-reduce模式的标准行为，见ORCHESTRATION.md
    第3.3节）。纯函数，不调用LLM：
    1. 写outputs/poi_details/manifest.json（由join节点统一写，不是各并行分支各自写，避免
       并发写同一文件的竞态）。
    2. 反向同步final_guide.md：优先从每个已生成的POI详情文件里抽取recommended=true
       路线的途经点摘要；零推荐时仅把第一条作为中性路线预览。纯程序拼接追加到正文对应
       POI处+跳转链接——不重新调用LLM，不给已核验通过的森林正文重新引入幻觉风险，也不
       需要重新走一遍citation_check。
    3. 设置progress.state = "完成"。
    """
    project_id = state["project_id"]
    topic = state.get("topic", "")
    results = state.get("poi_detail_results", [])

    delivery.finalize_poi_details(project_id, topic, results)
    return {
        "last_agent_result": {"agent": "poi_detail_agent", "summary": f"树木生成完成，共{len(results)}个POI"},
    }


def _sync_poi_details_into_final_guide(project_id: str, results: list[dict]) -> None:
    """Compatibility wrapper for callers and tests that import the old helper."""
    delivery.sync_poi_details_into_final_guide(project_id, results)


# ---------------------------------------------------------------------------
# 陪伴阶段（docs/ORCHESTRATION.md 第3.4节 / docs/ARCHITECTURE.md 第3.5节）
# ---------------------------------------------------------------------------


# 用户备注/annotation layer 被动带出（docs/ARCHITECTURE.md第21.5节，阶段3）：
# companion四个agent回答时先查有没有绑"本次问题相关天/POI"的未删备注，有就在答案末尾
# 转述一句「你之前记过：…」。这是"被动带出"——只读list_user_notes、拼进prompt上下文，
# 不新增写入路径、不进citation核验、不改权限矩阵（四个agent都不加新工具）。

# 窄版"从用户话里抽day/POI"——只认明确写出来的"Day N/第N天"+route_plan真实POI名字面匹配。
# 不做NLP解析（"今天/明天"这类相对日期需要知道"今天是第几天"，本项目没有先例，留作已知
# 局限，见docs/ARCHITECTURE.md第21.5节）。抽不到day/POI就不带备注——避免每次都把一堆
# 无关行程级备注塞进上下文（行程级备注不靠这条路径带出，只在site渲染层可见）。
_DAY_PATTERN = re.compile(r"(?:Day\s*|第\s*)(\d{1,2})(?:\s*[天日]?)")


def _extract_day_and_poi(question: str, route_plan) -> tuple[Optional[int], Optional[str]]:
    """从用户话里抽day和poi_name（窄版正则+route_plan真实POI字面匹配）。

    返回(day, poi_name)，抽不到的维度返回None——调用方据此决定要不要带备注。
    跟飞书service.py的_extract_user_note_fields不是同一个东西：那个是"写入备注"时定位
    该绑哪天/哪个POI（用light model结构化抽取，要容错"整体节奏偏紧"这类行程级备注）；
    这个是"回答问题时"判断"用户这次问的是哪天/哪个POI"（只认明确写出来的，抽不到就不带），
    是纯函数不调LLM，避免陪伴阶段每问一句都多一次light model调用。
    """
    day: Optional[int] = None
    m = _DAY_PATTERN.search(question)
    if m:
        try:
            day = int(m.group(1))
        except ValueError:
            day = None

    poi_name: Optional[str] = None
    if route_plan:
        # 跟_extract_user_note_fields/孤儿备注判定同款保守设计：只做精确字面匹配，不做
        # 模糊匹配（用户说"武侯祠"但route_plan里写的是"武候祠"错别字会匹配不上→不带，
        # 是刻意保守，见docs/ARCHITECTURE.md第21.7节阶段2已知局限②）。
        for d in route_plan.days:
            for poi in d.pois:
                if poi.name and poi.name in question:
                    poi_name = poi.name
                    break
            if poi_name:
                break
    return day, poi_name


def _companion_context_prefix(state: GraphState) -> str:
    context = (state.get("companion_recent_context") or "").strip()
    return f"{context}\n\n" if context else ""


def _companion_route_text(question: str, recent_context: str = "") -> str:
    recent_context = (recent_context or "").strip()
    if not recent_context:
        return question
    return f"{recent_context}\n\n【当前用户问题】\n{question}"


def _extract_companion_day_and_poi(question: str, recent_context: str, route_plan):
    """Prefer explicit entities in the current turn, then fill gaps from recent context."""
    day, poi_name = _extract_day_and_poi(question, route_plan)
    if (day is None or not poi_name) and recent_context:
        history_day, history_poi = _extract_day_and_poi(recent_context, route_plan)
        if day is None:
            day = history_day
        if not poi_name:
            poi_name = history_poi
    return day, poi_name


def _relevant_user_notes(project_id: str, day: Optional[int] = None, poi_name: Optional[str] = None) -> list[dict]:
    """读list_user_notes按day/poi_name筛"跟本次问题相关的未删备注"（纯函数不调LLM）。

    docs/ARCHITECTURE.md第21.5节阶段3：day和poi_name都为None时返回空——行程级备注
    不靠这条companion答案路径带出（避免每次都塞一堆无关行程级备注），它只在site渲染层
    的"独立备注区"可见。给day带天级+景点级备注（绑该天的）；给poi_name只带绑该POI的
    景点级备注（不限天，POI已经唯一锚定了位置）。list_user_notes本身已过滤deleted=true
    和未删条目（见src/workspace/store.py），superseded链上的旧条因为标了deleted=true也
    不在里面，这里不需要再处理。
    """
    if day is None and not poi_name:
        return []
    notes = store.list_user_notes(project_id)
    matched: list[dict] = []
    for note in notes:
        if poi_name and note.poi_name == poi_name:
            matched.append({"note_text": note.note_text, "created_at": note.created_at, "day": note.day, "poi_name": note.poi_name})
            continue
        # POI没命中（或没给POI）时，再看天级：只匹配poi_name为空的天级备注，避免把绑别的POI
        # 的景点级备注因为同一天就带出来（那是关于另一个POI的话，跟当前问题无关）。
        if day is not None and note.day == day and not note.poi_name:
            matched.append({"note_text": note.note_text, "created_at": note.created_at, "day": note.day, "poi_name": note.poi_name})
    return matched


def _format_user_notes_block(notes: list[dict]) -> str:
    """把_relevant_user_notes筛出的备注拼成prompt里塞的上下文段。空列表返回空串。

    标注"用户备注"——这是用户自己写的东西，不是系统核验过的攻略事实，agent转述时要
    当成"用户自己记的"而不是"已核验结论"（docs/ARCHITECTURE.md第21.1节）。不再标
    "未经核验"——用户自己记的东西本来就不用核验，标"未经核验"反而让模型以为系统对
    它做了某种背书前的声明，措辞改成中性的"用户备注"更合适。agent规则要求"没给这段
    就不提"，所以这里返回空串时调用方完全不加这一段，agent自然不会带出。
    """
    if not notes:
        return ""
    lines = ["【用户备注（关于本天/本景点）：】"]
    for n in notes:
        lines.append(f"- {n['note_text']}（记于{n['created_at']}）")
    return "\n".join(lines) + "\n"


def _invoke_with_capture_verification(agent, prompt: str, captured: dict, required_key: str, retry_note: str) -> None:
    """跟_invoke_with_write_verification同一个思路（见docs/ISSUES.md），但验证目标不是
    "某个文件被写入"，而是"captured这个边车容器里出现了必需的key"——guide_agent/realtime_agent
    没有写权限，产出结构化数据只能靠调用专用工具往captured里塞（见src/tools/guide_tools.py、
    src/tools/realtime_tools.py），同样可能出现"只输出一段文字总结、没有真的调用工具"的情况。

    同_invoke_with_write_verification的修复（docs/ISSUES.md）：重试消息必须带上累积的完整
    对话历史，不能只发一条孤立的retry_note——没有checkpointer的react-agent两次独立invoke()
    之间完全无状态共享，孤立的retry_note会让模型看不到原始问题是什么。
    """
    messages: list = [{"role": "user", "content": prompt}]
    result = invoke_with_retry(agent, messages)
    messages = result["messages"]
    if required_key not in captured:
        messages = messages + [{"role": "user", "content": retry_note}]
        invoke_with_retry(agent, messages)


def guide_agent_node(state: GraphState) -> dict:
    project_id = state["project_id"]
    question = state.get("companion_question") or state.get("edit_request") or ""
    captured: dict = {}
    agent = build_guide_agent(project_id, captured)
    set_trace_context(project_id, "guide_agent")

    # 用户备注被动带出（docs/ARCHITECTURE.md第21.5节阶段3）：从用户话里抽day/POI，
    # 把绑该天/该POI的未删备注作为上下文段塞进prompt。抽不到day/POI（用户没说具体天/
    # POI）就不加这段——agent规则要求"没给就不提"，不会编造备注。
    recent_context = state.get("companion_recent_context") or ""
    day, poi_name = _extract_companion_day_and_poi(
        question, recent_context, store.read_route_plan(project_id)
    )
    notes_block = _format_user_notes_block(_relevant_user_notes(project_id, day, poi_name))

    prompt = _companion_context_prefix(state) + (
        f"用户问题：{question}\n"
        f"请只依据final_guide.md和outputs/poi_details/下已生成的内容回答，"
        f"回答不到就如实说明「当前攻略没有这部分信息」，不要编造。"
        f"完成后必须调用answer工具提交你的回答和covered标记。"
    )
    if notes_block:
        prompt = f"{notes_block}\n{prompt}"
    _invoke_with_capture_verification(
        agent,
        prompt,
        captured,
        required_key="text",
        retry_note="你刚才的回复没有调用answer工具提交回答，只有文字总结是不够的。"
        "请现在立刻调用answer工具，把回答内容和covered标记传进去。",
    )

    answer_text = captured.get("text", "")
    covered = bool(captured.get("covered", True))
    result: dict = {
        "companion_guide_answer": answer_text,
        "guide_covered": covered,
        "last_agent_result": {"agent": "guide_agent", "summary": "向导已回答"},
    }
    if covered:
        result["companion_answer"] = answer_text
    return result


_COMPANION_INTENT_HINTS = {
    "guide_agent": "问的是已经生成好的攻略/景点详情内容（读取已有产出就能回答）",
    # 真实bug（2026-07-26飞书真实联调发现）：realtime_agent在2026-07-25新绑了
    # create_reminder/get_current_time/list_reminders/cancel_reminder/update_reminder
    # 这几个提醒管理工具（见ARCHITECTURE.md第14.1节），但这条分类hint当时没有同步更新，
    # 只字未提"提醒"能力——真实用户说"5分钟后提醒我喝水"被轻量分类模型误判成
    # lifestyle_agent（大概率是"喝水"这个词面上更像生活方式类推荐），提醒因此从未被创建。
    # 已修复：hint里显式补上提醒相关表述。
    "realtime_agent": "问的是需要联网才知道的时效性信息（比如天气、是否还在营业、临时关闭），"
    "或者要设置/查询/取消/修改一个提醒（比如'5分钟后提醒我喝水''帮我看看还有哪些提醒''取消刚才那个提醒'）",
    "lifestyle_agent": "问的是树木详情没覆盖到的更细节推荐（不带时效性，比如附近还有什么吃的/买的）",
    "pitfall_agent": "问的是要不要避坑/有没有踩雷/差评/不推荐的理由，比如'这家店有没有坑''这个真的值得去吗''有什么要小心的'",
    # 2026-08-21新增（见docs/ARCHITECTURE.md 13.1.1节第7条从"留给后续决定"更新为已接入）：
    # culture_agent此前只走edit入口显式@触发，陪伴阶段一句随口的"武侯祠名字的由来"
    # 这类科普提问会被误分到guide/realtime/lifestyle/pitfall之一、从来真正问到文化agent。
    # 照lifestyle/pitfall"陪伴阶段建轻量只读节点不落盘"的范式接入五选一。
    "culture_agent": "问的是目的地历史文化/民俗风俗/名称由来这类科普性问题，不是行程推荐，"
    "比如'这个景点名字的来历''这里为什么有这个习俗''这道菜的历史渊源'",
}


def _classify_companion_intent(user_message: str, candidates: list[str]) -> str:
    """一次LLM调用，从candidates里选一个最匹配的意图（docs/ORCHESTRATION.md第3.4节
    _classify_companion_intent伪代码的落地）。返回值必然落在candidates内——解析失败时
    兜底返回candidates[0]，不抛异常打断路由。"""
    # N选1分类，不涉及内容生成/核验，用轻量模型（见CLAUDE.md模型分层讨论）。
    model = build_companion_router_model(temperature=0)
    options = "\n".join(f"- {c}：{_COMPANION_INTENT_HINTS.get(c, '')}" for c in candidates)
    prompt = (
        f"用户消息：{user_message}\n"
        f"请判断这句话最应该路由给下面哪一个角色处理，只回答角色名本身（如「{candidates[0]}」），"
        f"不要输出多余文字：\n{options}"
    )
    response = model.invoke([{"role": "user", "content": prompt}])
    text = (response.content or "").strip()
    for c in candidates:
        if c in text:
            return c
    return candidates[0]


def realtime_agent_node(state: GraphState) -> dict:
    project_id = state["project_id"]
    question = state.get("companion_question") or state.get("edit_request") or ""
    captured: dict = {}
    agent = build_realtime_agent(project_id, captured)
    set_trace_context(project_id, "realtime_agent")

    # 用户备注被动带出（同guide_agent_node，见docs/ARCHITECTURE.md第21.5节阶段3）。
    recent_context = state.get("companion_recent_context") or ""
    day, poi_name = _extract_companion_day_and_poi(
        question, recent_context, store.read_route_plan(project_id)
    )
    notes_block = _format_user_notes_block(_relevant_user_notes(project_id, day, poi_name))

    prompt = _companion_context_prefix(state) + (
        f"用户问题：{question}\n"
        f"如果只是查询/回答，调用answer_query；如果你判断需要更新公共区内容，"
        f"调用propose_patch说明具体改动，不要直接下结论说已经改好了；"
        f"如果用户是在交代一件有明确时间点的事要求到时候提醒，先调用get_current_time"
        f"推算出绝对时间戳，再调用create_reminder创建提醒（重复提醒额外传"
        f"repeat_interval_hours）；如果用户是想查看/取消/修改已有的提醒，先调用"
        f"list_reminders确认id，再调用cancel_reminder_tool/update_reminder_tool，"
        f"最后仍然要调用answer_query把结果转述给用户。"
    )
    if notes_block:
        prompt = f"{notes_block}\n{prompt}"
    _invoke_with_capture_verification(
        agent,
        prompt,
        captured,
        required_key="mode",
        retry_note="你刚才的回复没有调用answer_query/propose_patch/create_reminder中的任何一个工具，"
        "只有文字总结是不够的。请现在立刻调用其中一个工具提交结果。",
    )

    result: dict = {"last_agent_result": {"agent": "realtime_agent", "summary": "实时agent处理完成"}}
    if captured.get("mode") == "patch":
        result["realtime_pending_patch"] = captured.get("patch")
        return result

    answer_text = captured.get("text", "")
    guide_answer = state.get("companion_guide_answer")
    result["companion_fallback_answer"] = answer_text
    result["companion_answer"] = f"{guide_answer}\n\n{answer_text}" if guide_answer else answer_text
    return result


def await_realtime_approval_node(state: GraphState) -> dict:
    """只做只读操作+interrupt()，不能在这里放有副作用的代码（resume时会重跑），
    同await_skeleton_confirm_node同款约束。resume决策：「approve」批准写入 /
    其他任意字符串（约定用「reject」）视为拒绝，patch作废不写入。"""
    project_id = state["project_id"]
    patch = state.get("realtime_pending_patch")
    decision = interrupt(
        {
            "question": "实时agent建议更新以下内容，是否批准写入？回复 approve 批准，或 reject 拒绝",
            "patch": patch,
        }
    )
    approved = decision == "approve"

    result: dict = {"realtime_approval_done": True, "realtime_patch_applied": False}
    if approved and patch:
        field = patch.get("field")
        day = patch.get("day")
        if field in LIFESTYLE_FIELDS:
            store.write_agent_output_patch(
                project_id,
                "realtime_agent",
                [
                    {
                        "field": field,
                        "day": day,
                        "recommendation": patch.get("new_recommendation", ""),
                        "sources": [],
                        "claimed_entities": [],
                    }
                ],
            )
            result["realtime_patch_applied"] = True

    fallback_text = (
        "已根据你的批准更新内容。" if result["realtime_patch_applied"] else "该更新未获批准，未做任何修改。"
    )
    guide_answer = state.get("companion_guide_answer")
    result["companion_fallback_answer"] = fallback_text
    result["companion_answer"] = f"{guide_answer}\n\n{fallback_text}" if guide_answer else fallback_text
    result["last_agent_result"] = {
        "agent": "realtime_agent",
        "summary": "写入已批准并应用" if result["realtime_patch_applied"] else "写入被拒绝，未修改任何内容",
    }
    return result


def companion_lifestyle_query_node(state: GraphState) -> dict:
    """陪伴阶段"衣食住行agent重新检索"——docs/ORCHESTRATION.md第3.4节"这是对现有agent
    能力的复用，不是新设计"。但不复用重量级的lifestyle_agent_node（会写入outputs/lifestyle.json
    并级联citation_check/整合agent重新合并一整份攻略）——陪伴阶段一句随口的"附近还有什么好吃的"
    不应该静默重写正式产出。这里现场构建一个只有检索能力、没有写权限的轻量agent，只回答不落盘，
    见实现计划"实现决策"第2条。
    """
    project_id, topic = state["project_id"], state["topic"]
    question = state.get("companion_question") or state.get("edit_request") or ""
    route_plan = store.read_workspace(project_id, "route_plan.json")

    tools = (
        make_material_tools(topic, project_id, actor="companion_lifestyle", allow_append_evidence=False)
        + make_amap_tools(topic)
        + make_workspace_tools(project_id, "companion_query")  # 未匹配任何写分支，只返回read_workspace
    )
    prompt = (
        "你是衣食住行agent，这次是陪伴阶段的一次自由提问，不是要求你正式修改产出区——"
        "只需要检索素材库、结合已有路线规划回答用户的问题，用自然语言直接回答，"
        "不需要也不要调用任何写入工具。回答使用markdown格式排版（分点、**加粗**关键信息等），"
        "可以适度用与内容贴合的emoji（如🍜🏨🎒）帮助用户快速扫读重点，但不要堆砌。"
    )
    agent = create_react_agent(build_companion_chat_model(), tools, prompt=prompt)
    # docs/ISSUES.md第142条：trace agent名从"companion_query"拆分成"companion_lifestyle"，
    # 跟companion_pitfall_query_node区分开——此前两者共用"companion_query"，飞书进度条
    # 读trace.jsonl的tool_calls拼"正在检索素材库"这类后缀时无法区分是lifestyle还是
    # pitfall在查（node级友好标签已经能区分，trace级证据没有），见service.py的_NODE_TO_AGENT。
    set_trace_context(project_id, "companion_lifestyle")
    # 用户备注被动带出（同guide_agent_node，见docs/ARCHITECTURE.md第21.5节阶段3）。
    # route_plan.json这份dict上面已经读了用于拼prompt；这里另读一次read_route_plan
    # 拿RoutePlan结构化对象供_extract_day_and_poi遍历pois——两份读的是同一个文件，
    # companion轻量查询本来就是一次问答，多一次本地文件读没有副作用，保持跟其它三个
    # companion节点统一"用RoutePlan对象抽day/poi"的写法。
    rp = store.read_route_plan(project_id)
    recent_context = state.get("companion_recent_context") or ""
    day, poi_name = _extract_companion_day_and_poi(question, recent_context, rp)
    notes_block = _format_user_notes_block(_relevant_user_notes(project_id, day, poi_name))
    user_content = (
        f"{_companion_context_prefix(state)}"
        f"路线规划：{json.dumps(route_plan, ensure_ascii=False)}\n用户问题：{question}"
    )
    if notes_block:
        user_content = f"{notes_block}{user_content}"
    invoke_result = invoke_with_retry(
        agent,
        [
            {
                "role": "user",
                "content": user_content,
            }
        ],
    )
    answer_text = invoke_result["messages"][-1].content

    guide_answer = state.get("companion_guide_answer")
    merged = f"{guide_answer}\n\n{answer_text}" if guide_answer else answer_text
    return {
        "companion_fallback_answer": answer_text,
        "companion_answer": merged,
        "last_agent_result": {"agent": "companion_query", "summary": "衣食住行agent补充检索完成"},
    }


def companion_pitfall_query_node(state: GraphState) -> dict:
    """陪伴阶段"避雷agent重新检索"——跟companion_lifestyle_query_node同一个思路（陪伴阶段
    一句随口的"这家店有没有坑"不应该静默重写正式产出区），完全照抄companion_lifestyle_query_node
    的实现模式，只有两处具体差异：①工具不绑amap_tools（真实pitfall_agent本身也不绑，避雷是
    内容检索不是地理位置校验，见src/agents/pitfall_agent/agent.py）；②额外把已有的
    outputs/pitfall.json塞进prompt上下文，避免这次临时查询的结论和已经生成的正式避雷内容
    互相矛盾或重复（project还没生成过避雷内容时该文件不存在，读到None直接一起序列化进去
    即可，不需要额外判断报错）。

    这次任务范围内明确不做：不给这个查询接联网检索能力（不加web_search工具）——是否要让
    避雷查询联网获取最新差评/资讯，讨论结论是"值得做但范围更大，需要先决定真实pitfall_agent
    本身要不要联网"，这次先搁置，见docs/ISSUES.md。
    """
    project_id, topic = state["project_id"], state["topic"]
    question = state.get("companion_question") or state.get("edit_request") or ""
    route_plan = store.read_workspace(project_id, "route_plan.json")
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")

    tools = (
        make_material_tools(topic, project_id, actor="companion_pitfall", allow_append_evidence=False)
        + make_workspace_tools(project_id, "companion_query")  # 未匹配任何写分支，只返回read_workspace
    )
    prompt = (
        "你是避雷agent，这次是陪伴阶段的一次自由提问，不是要求你正式修改产出区——"
        "只需要检索素材库、结合已有路线规划和已有避雷内容回答用户的问题，用自然语言直接回答，"
        "不需要也不要调用任何写入工具。回答使用markdown格式排版（分点、**加粗**关键信息等），"
        "可以适度用与内容贴合的emoji（如⚠️🚫）帮助用户快速扫读重点，但不要堆砌。"
    )
    agent = create_react_agent(build_companion_chat_model(), tools, prompt=prompt)
    # docs/ISSUES.md第142条：trace agent名从"companion_query"拆分成"companion_pitfall"，
    # 理由同companion_lifestyle_query_node那处注释。
    set_trace_context(project_id, "companion_pitfall")
    # 用户备注被动带出（同companion_lifestyle_query_node，见docs/ARCHITECTURE.md第21.5节阶段3）。
    rp = store.read_route_plan(project_id)
    recent_context = state.get("companion_recent_context") or ""
    day, poi_name = _extract_companion_day_and_poi(question, recent_context, rp)
    notes_block = _format_user_notes_block(_relevant_user_notes(project_id, day, poi_name))
    user_content = _companion_context_prefix(state) + (
        f"路线规划：{json.dumps(route_plan, ensure_ascii=False)}\n"
        f"已有避雷内容：{json.dumps(pitfall, ensure_ascii=False)}\n"
        f"用户问题：{question}"
    )
    if notes_block:
        user_content = f"{notes_block}{user_content}"
    invoke_result = invoke_with_retry(
        agent,
        [
            {
                "role": "user",
                "content": user_content,
            }
        ],
    )
    answer_text = invoke_result["messages"][-1].content

    guide_answer = state.get("companion_guide_answer")
    merged = f"{guide_answer}\n\n{answer_text}" if guide_answer else answer_text
    return {
        "companion_fallback_answer": answer_text,
        "companion_answer": merged,
        "last_agent_result": {"agent": "companion_pitfall_query", "summary": "避雷agent补充检索完成"},
    }


def companion_culture_query_node(state: GraphState) -> dict:
    """陪伴阶段"文化科普agent"——跟companion_lifestyle_query_node/companion_pitfall_query_node
    同一个思路（陪伴阶段一句随口的"武侯祠名字的由来"这类科普提问不应该静默重写正式产出区
    `outputs/culture.json`），完全照抄companion_lifestyle_query_node的实现模式。差异点对齐
    真实culture_agent的定位（见src/agents/culture_agent/agent.py）：①工具只绑
    web_search+只读workspace（不绑make_material_tools，素材库是UGC攻略内容不是历史文化
    科普；不绑make_amap_tools，文化科普不是地理位置校验）；②不读outputs/culture.json塞
    上下文——跟pitfall节点读outputs/pitfall.json避免结论矛盾不同，文化科普是纯知识问答，
    没有"正式产出区已有结论"需要对齐的必要，陪伴阶段这次问答的结论也不会落盘，自然不
    存在"和正式记录矛盾"这层顾虑。正式落盘的文化问答仍走原culture_agent_node（edit入口
    @culture_agent显式触发，见_route_edit_mode的last_agent=="culture_agent"分支），本节点
    只回答不落盘。"""
    project_id, topic = state["project_id"], state["topic"]
    question = state.get("companion_question") or state.get("edit_request") or ""
    route_plan = store.read_workspace(project_id, "route_plan.json")

    tools = make_web_search_tools(project_id, "companion_culture") + make_workspace_tools(
        project_id, "companion_query"
    )  # 未匹配任何写分支，只返回read_workspace
    prompt = (
        "你是文化科普agent，负责回答目的地历史文化/民俗风俗/名称由来这类科普性问题——"
        "不是给出行程/推荐建议，是纯粹的知识科普。这次是陪伴阶段的一次自由提问，"
        "不是要求你正式修改产出区，所以只需要用web_search查证后回答用户的问题，"
        "用自然语言直接回答，不需要也不要调用任何写入工具。"
        "请先用web_search查证再回答，网上能查到的公开知识性内容查证一遍比凭记忆回答更可靠；"
        "如果web_search没有找到相关结果，如实告知用户'这个问题目前无法查证确切依据'，"
        "不要编造具体的年代、人名、典故细节去填补空白。"
        "回答使用markdown格式排版（分点、**加粗**关键信息等），可以适度用与内容贴合的emoji"
        "（如📜🏛️🎎）帮助用户快速扫读重点，但不要堆砌。"
    )
    agent = create_react_agent(build_companion_chat_model(), tools, prompt=prompt)
    # trace agent名独立成"companion_culture"——跟companion_lifestyle/companion_pitfall区分开
    # （同docs/ISSUES.md第142条那次拆分的理由：此前companion_lifestyle/pitfall共用过
    # "companion_query"，飞书进度条读trace.jsonl的tool_calls拼"正在检索素材库"这类后缀时
    # 无法区分是哪个节点在查），见service.py的_NODE_TO_AGENT。
    set_trace_context(project_id, "companion_culture")
    # 用户备注被动带出（同companion_lifestyle_query_node，见docs/ARCHITECTURE.md第21.5节阶段3）。
    rp = store.read_route_plan(project_id)
    recent_context = state.get("companion_recent_context") or ""
    day, poi_name = _extract_companion_day_and_poi(question, recent_context, rp)
    notes_block = _format_user_notes_block(_relevant_user_notes(project_id, day, poi_name))
    user_content = (
        f"{_companion_context_prefix(state)}"
        f"路线规划：{json.dumps(route_plan, ensure_ascii=False)}\n用户问题：{question}"
    )
    if notes_block:
        user_content = f"{notes_block}{user_content}"
    invoke_result = invoke_with_retry(
        agent,
        [
            {
                "role": "user",
                "content": user_content,
            }
        ],
    )
    answer_text = invoke_result["messages"][-1].content

    guide_answer = state.get("companion_guide_answer")
    merged = f"{guide_answer}\n\n{answer_text}" if guide_answer else answer_text
    return {
        "companion_fallback_answer": answer_text,
        "companion_answer": merged,
        "last_agent_result": {"agent": "companion_culture_query", "summary": "文化科普agent补充检索完成"},
    }


# ---------------------------------------------------------------------------
# 路由函数（docs/ORCHESTRATION.md 第4节）
# ---------------------------------------------------------------------------


def main_agent_router(state: GraphState) -> str:
    if state.get("pending_route_structure_confirmation"):
        return "await_route_structure_confirm"
    if state.get("route_structure_revision_request"):
        return "route_draft"
    # 接受了确认后结构候选的路线必须重新过引用/时效核验。这个优先级独立于项目phase，
    # 因而整合阶段接受候选后也不会绕过两道检查。
    if state.get("route_structure_resume_check") == "integration":
        if not state.get("route_citation_checked"):
            return "route_citation_check_node"
        if not state.get("route_map_checked"):
            return "route_map_check_node"
        if not state.get("route_timeliness_checked"):
            return "route_timeliness_check_node"
    if state.get("mode", "plan") == "plan":
        return _route_plan_mode(state)
    return _route_edit_mode(state)


def _route_plan_mode(state: GraphState) -> str:
    project_id = state["project_id"]
    topic = state["topic"]

    # topic在"需求收集中"阶段可能还不知道（飞书等渠道驱动的多轮收集场景，见
    # requirements_gather_node）——只有topic已经确定时才做素材前置校验，避免拿一个
    # 空字符串去查素材库；CLI/API等一次性传入完整requirements的既有调用方topic从一开始
    # 就是非空的，这条guard不改变它们的行为。
    if not state.get("material_checked") and topic:
        needs_sync = store.material_needs_sync(topic)
        if needs_sync is not False:  # True(有未处理的新素材) 或 None(raw完全没有素材) 都需要先跑一趟
            return "material_agent_node"

    progress = store.read_progress(project_id)

    if progress.state == ProjectPhase.REQUIREMENTS:
        if _missing_required_fields(state.get("requirements") or {}):
            return "requirements_gather_node"
        return "collect_requirements"
    if progress.state == ProjectPhase.SKELETON_CONFIRM:
        if state.get("revision_request") or not state.get("route_plan_drafted"):
            return "route_draft"
        return "await_skeleton_confirm"
    if progress.state == ProjectPhase.PARALLEL_RESEARCH:
        if not state.get("route_citation_checked"):
            return "route_citation_check_node"
        if not state.get("route_map_checked"):
            return "route_map_check_node"
        if not state.get("route_timeliness_checked"):  # 时效核验（见route_timeliness_check_node）
            return "route_timeliness_check_node"
        if not state.get("lifestyle_agent_done"):
            return "lifestyle_agent_node"
        if not state.get("pitfall_agent_done"):
            return "pitfall_agent_node"
        if not state.get("pitfall_citation_checked"):
            return "pitfall_citation_check_node"
        return "citation_check_node"
    if progress.state == ProjectPhase.INTEGRATING:
        return _route_integration_phase(state)
    if progress.state == ProjectPhase.POI_DETAIL:
        return _route_poi_detail_phase(state)
    if progress.state == ProjectPhase.DONE:
        return END
    return END


def _route_edit_mode(state: GraphState) -> str:
    """按 docs/ORCHESTRATION.md 第4节实现。edit入口覆盖route_agent（粒度为"天"）/lifestyle_agent
    （粒度为"字段"）/pitfall_agent/integration_agent，见docs/ISSUES.md第29/46条。"""
    project_id = state["project_id"]

    if state.get("last_agent_result") is None:
        return state["entry_target"]

    last_agent = state["last_agent_result"]["agent"]

    if last_agent == "guide_agent":
        return _route_after_guide_agent(state)

    if last_agent == "realtime_agent":
        return _route_after_realtime_agent(state)

    # 文化科普agent：游离主流程，仅edit入口@触发，不做引用级核验、不级联citation_check/整合，
    # 一跳直接结束（docs/ORCHESTRATION.md场景9）。
    if last_agent == "culture_agent":
        return END

    # POI详情agent的edit入口（poi_detail_edit_node）：核验/打回已经内嵌在节点自身
    # （_run_poi_detail_citation_loop），跟plan模式"树木生成中"阶段的核验方式一致，不需要
    # 独立的citation_check节点；POI详情本来就不在AGENT_DEPENDENCY_TABLE的级联链路里，一跳
    # 直接结束，不级联lifestyle_agent/pitfall_agent/integration_agent。
    if last_agent == "poi_detail_agent":
        return END

    # companion_lifestyle_query_node是陪伴阶段的轻量查询节点（docs/ORCHESTRATION.md第3.4节），
    # 不写任何workspace文件，跑完直接结束，不落进下面AGENT_DEPENDENCY_TABLE驱动的级联/整合逻辑
    # （那套逻辑是为"正式修改产出区"设计的，companion_lifestyle_query_node不是那类操作）。
    if last_agent == "companion_query":
        return END

    # companion_pitfall_query_node是陪伴阶段的轻量查询节点（避雷agent版本，同上），同样不写
    # 任何workspace文件，跑完直接结束，不落进下面的级联/整合逻辑。
    if last_agent == "companion_pitfall_query":
        return END

    # companion_culture_query_node是陪伴阶段的轻量查询节点（文化科普agent版本，同上），同样
    # 不写任何workspace文件，跑完直接结束——正式落盘的culture_agent_node走edit入口显式@
    # 触发，有自己的last_agent=="culture_agent"分支处理，这里这个是只读陪伴节点不是那个。
    if last_agent == "companion_culture_query":
        return END

    # Feasibility may repair notes/sources/claims after the normal route citation
    # gate has already completed.  Those writes explicitly invalidate the old
    # checks, so edit mode must send the touched days back through citation before
    # cascading to downstream agents.
    if (
        last_agent == "citation_checker"
        and state["last_agent_result"].get("source") == "route_timeliness"
        and not state.get("route_citation_checked")
    ):
        return "route_citation_check_node"

    # route_agent写完后，先过一次系统级引用核验（跟plan模式的route_citation_check_node对称）。
    # 不区分是直接edit入口还是被整合agent冲突打回重新生成——两种情况都该在回整合复核/
    # 级联lifestyle_agent之前先核验一遍这次改动的内容，跟lifestyle_agent的判断逻辑对称。
    if last_agent == "route_agent" and not state.get("route_citation_checked"):
        return "route_citation_check_node"

    if not state.get("route_map_checked") and (
        last_agent == "route_agent"
        or (
            last_agent == "citation_checker"
            and state["last_agent_result"].get("source") == "route"
        )
    ):
        return "route_map_check_node"

    # route_agent 的时效核验（route_timeliness_check_node）：在引用核验之后、级联
    # lifestyle_agent 之前跑，跟 plan 模式的 PARALLEL_RESEARCH 顺序对称（见该节点注释）。
    # 注意这里 last_agent 可能是 "route_agent"（直接edit）或 "citation_checker"（引用核验刚跑完，
    # source="route"）——两种情况只要 route_timeliness_checked 还没设，都该去 timeliness 节点。
    if not state.get("route_timeliness_checked") and (
        last_agent == "route_agent"
        or (
            last_agent == "citation_checker"
            and state["last_agent_result"].get("source") == "route_map"
        )
    ):
        return "route_timeliness_check_node"

    # lifestyle_agent写完后，先过一次系统级引用核验（跟plan模式的
    # route_citation_check_node→lifestyle_agent_node→citation_check_node对称），
    # 核验节点跑完后last_agent会变成"citation_checker"，不会再次进入这个分支。
    if last_agent == "lifestyle_agent" and not state.get("edit_citation_checked"):
        return "citation_check_node"

    # pitfall_agent写完后同样先过一次系统级引用核验，跟lifestyle_agent那段对称。
    if last_agent == "pitfall_agent" and not state.get("pitfall_citation_checked"):
        return "pitfall_citation_check_node"

    # citation_check_node/pitfall_citation_check_node/route_citation_check_node都不是
    # AGENT_DEPENDENCY_TABLE里的独立"agent"，只是各自agent和整合环节之间的系统检查点，
    # 路由决策上把它们分别当成route_agent/lifestyle_agent/pitfall_agent的延续。但三个核验
    # 节点共用同一个"citation_checker"actor名，无法仅从last_agent单独区分是哪一个核验刚跑完。
    #
    # 判断依据：优先读route_citation_check_node/route_timeliness_check_node显式写入的
    # last_agent_result["source"]字段（值为"route"或"route_timeliness"）——这是显式标记，
    # 不依赖执行顺序推断。没有这个字段时（lifestyle/pitfall两条既有路径，暂不加source字段，
    # 兼容不动）回退到原有的 flag顺序推断：pitfall_citation_checked为True说明
    # pitfall_citation_check_node刚执行完（它是唯一会把这个标志设成True的节点），这次的
    # citation_checker必然是pitfall那次；否则说明是lifestyle那次citation_check_node刚跑完。
    #
    # 为什么不能像route那样也直接沿用"看某个flag是不是True"的旧heuristic：route→lifestyle
    # 级联场景下，route_citation_checked会在lifestyle自己的citation_check_node跑完之前就已经
    # 是True，如果照搬"哪个flag是True就归哪个agent"的heuristic，会把lifestyle那次的
    # citation_checker完成误判成route的，导致后续错误地查AGENT_DEPENDENCY_TABLE["route_agent"]
    # （downstream=["lifestyle_agent"]，此时已完成会被跳过）而不是
    # AGENT_DEPENDENCY_TABLE["lifestyle_agent"]（downstream=["pitfall_agent"]），
    # 造成pitfall_agent在route编辑级联场景下被静默跳过——这是设计阶段就要避免的真实bug。
    if last_agent == "citation_checker":
        source = state["last_agent_result"].get("source")
        if source:
            effective_agent = {"route": "route_agent", "route_map": "route_agent", "route_timeliness": "route_agent", "lifestyle": "lifestyle_agent", "pitfall": "pitfall_agent"}[
                source
            ]
        elif state.get("pitfall_citation_checked"):
            effective_agent = "pitfall_agent"
        else:
            effective_agent = "lifestyle_agent"
    else:
        effective_agent = last_agent

    info = AGENT_DEPENDENCY_TABLE.get(effective_agent, {"downstream": [], "feeds_integration": False})

    for downstream in info["downstream"]:
        if not state.get(f"{downstream}_done"):
            node_name = {"lifestyle_agent": "lifestyle_agent_node", "pitfall_agent": "pitfall_agent_node"}.get(
                downstream, downstream
            )
            return node_name

    if info["feeds_integration"] and not state.get("integration_done"):
        return _route_integration_phase(state)

    return END


def _route_after_guide_agent(state: GraphState) -> str:
    """guide_agent跑完后的路由（docs/ORCHESTRATION.md第3.4节"自动串联"）：覆盖度足够
    直接结束；不够则用同一套意图分类逻辑（候选收窄为实时agent/衣食住行agent/避雷agent/
    文化科普agent四个查询型角色，guide_agent自己已经问过一轮了不会再选它）决定追加查谁。"""
    if state.get("guide_covered", True):
        return END
    question = state.get("companion_question") or state.get("edit_request") or ""
    route_text = _companion_route_text(question, state.get("companion_recent_context") or "")
    fallback = _classify_companion_intent(
        route_text,
        candidates=["realtime_agent", "lifestyle_agent", "pitfall_agent", "culture_agent"],
    )
    if fallback == "realtime_agent":
        return "realtime_agent_node"
    if fallback == "lifestyle_agent":
        return "companion_lifestyle_query_node"
    if fallback == "culture_agent":
        return "companion_culture_query_node"
    return "companion_pitfall_query_node"


def _route_after_realtime_agent(state: GraphState) -> str:
    """实时agent写入是审批后才动态级联，见 docs/ORCHESTRATION.md 第3.2节。

    三种情况：①patch已经批准并真正写入 → 走正常的整合级联（含冲突判断）；②有待审批的
    patch但还没走过审批节点 → 去await_realtime_approval_node；③纯问答，或patch已经走完
    审批（不管批准与否，realtime_approval_done都会被置True）→ 结束，不再重复绕回审批节点。
    """
    if state.get("realtime_patch_applied"):
        return _route_integration_phase(state)
    if state.get("realtime_pending_patch") is not None and not state.get("realtime_approval_done"):
        return "await_realtime_approval_node"
    return END


def _route_integration_phase(state: GraphState) -> str:
    """决定整合阶段的下一跳：先integration_agent产出/检查 → 有冲突就打回对应子agent修复 →
    子agent修复完必须先回integration_agent复核，不能直接再打回一次（否则第二次重试机会被浪费，
    见docs/ISSUES.md「整合冲突打回缺一步复核」）。

    重试计数的权威来源是state["integration_retry_count"]，不是conflict.retry_count——
    因为report_conflict每次都创建一条新记录（新id），如果拿conflict.retry_count判断上限，
    复核后若问题依旧、重新report出的新conflict会从0开始计数，MAX_RETRY形同虚设，
    可能无限打回（同样见docs/ISSUES.md）。conflict.retry_count仍然会被increment_retry_count
    更新，但只作为changelog审计用途，不再是这里的判断依据。
    """
    project_id = state["project_id"]

    if state.get("awaiting_integration_recheck"):
        # 子agent刚修复完，强制先复核，不看有没有pending conflict——但"复核"不等于
        # "无条件立刻回integration_agent_node"：如果这次打回顺带重置了下游done标志
        # （route_draft_node/lifestyle_agent_node被target=route_agent/lifestyle_agent
        # 的冲突打回时会这样做，见docs/ORCHESTRATION.md场景13），必须先让被重置的下游
        # 重新跑一遍，再回integration_agent_node复核，不能绕过。
        #
        # 这是真实排查docs/ISSUES.md第32条已知局限②时发现的：_route_plan_mode一旦进入
        # INTEGRATING阶段就无条件委托给这个函数，不会像PARALLEL_RESEARCH阶段那样再检查
        # lifestyle_agent_done/pitfall_agent_done——这个函数是plan模式下唯一还有机会
        # 消费这些重置标志的地方，此前这里直接跳过了，导致route_agent骨架重新生成后，
        # lifestyle_agent即使被标记"需要重新看一遍"（lifestyle_agent_done=False）也永远
        # 不会被真正调用；同样的问题此前也一直存在于"lifestyle_agent被打回、连带重置
        # pitfall_agent_done"这条已有路径上，只是从未被graph级别测试触发过。
        if not state.get("lifestyle_agent_done"):
            return "lifestyle_agent_node"
        if not state.get("pitfall_agent_done"):
            return "pitfall_agent_node"
        return "integration_agent_node"

    conflict = store.get_pending_conflict(project_id)

    if conflict is None:
        return "integration_agent_node"

    if state.get("integration_retry_count", 0) >= MAX_RETRY:
        store.mark_force_finalize(project_id)
        return "integration_agent_node"

    target_agent = conflict.target.split(".")[0]
    store.increment_retry_count(project_id, conflict.id)  # 仅用于审计，不是判断依据
    # "integration_agent"作为target：edit模式下integration_agent_node自己的长度骤降检测
    # 会report_conflict(target="integration_agent")，要求自己重新做一次（见docs/ISSUES.md第29条），
    # 不能落进默认的"lifestyle_agent_node"分支，否则会错误地打回衣食住行agent。
    node_map = {
        "route_agent": "route_draft",
        "lifestyle_agent": "lifestyle_agent_node",
        "pitfall_agent": "pitfall_agent_node",
        "integration_agent": "integration_agent_node",
    }
    return node_map.get(target_agent, "lifestyle_agent_node")


def _route_poi_detail_phase(state: GraphState) -> str | list[Send]:
    """"树木生成中"阶段的路由（docs/ORCHESTRATION.md第3.3节）：读route_plan.json的
    needs_detail=true POI列表——为空则直接跳过本阶段，进入"完成"（不强行fan-out零个任务，
    也不产出空的poi_details/目录）；非空则返回Send列表并行派发poi_detail_agent_node，
    每个Send负载显式携带project_id/topic（Send targeted节点只看得到负载里的key，看不到
    外层state的其他字段，这是LangGraph本身的行为，不是本项目特有设计，见docs/ORCHESTRATION.md
    第3.3节"实现时要注意"）。

    这个函数只在首次进入POI_DETAIL阶段时被main_agent_router调用一次——一旦Send派发出去，
    各poi_detail_agent_node分支跑完后走静态边到poi_details_join_node（不是回到这个路由
    函数，见build_graph），join节点里会把progress.state改成"完成"，下次main_agent_router
    路由到DONE分支直接结束，不会重复计算targets或重复fan-out。
    """
    project_id, topic = state["project_id"], state["topic"]
    route_plan = store.read_workspace(project_id, "route_plan.json")
    targets = _needs_detail_targets(route_plan)

    if not targets:
        store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
        return END

    return [
        Send(
            "poi_detail_agent_node",
            {
                "project_id": project_id,
                "topic": topic,
                "day": t["day"],
                "poi_name": t["poi_name"],
                "needs_detail_reason": t["needs_detail_reason"],
                "turn_id": state.get("turn_id"),
            },
        )
        for t in targets
    ]


# ---------------------------------------------------------------------------
# 机制B：Agent运行状态的跨进程持久化通道（docs/ARCHITECTURE.md第13.2节）
# ---------------------------------------------------------------------------

_run_status_lock = threading.Lock()
# key: (project_id, turn_id, node_name)——docs/ISSUES.md第142条修复：早期版本key只有
# (project_id, node_name)，是跨进程生命周期累积的，不是"本轮turn内第几跳"，见_track_run_status
# 的说明。
_node_call_counts: dict[tuple[str, str, str], int] = {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _track_run_status(node_name: str):
    """包一层而不改node函数体本身：每次调用前后写workspace/run_status.json，记录当前在跑
    哪个node、第几次调用、开始/结束时间——project可能来自CLI（run_mvp.py）也可能来自后台API
    触发的重跑，落盘依据state里的project_id，两种触发来源天然兼容。

    GraphBubbleUp（`interrupt()`真正的实现机制，见langgraph.errors）必须原样透传、不能被当成
    普通异常标记成"failed"——`await_skeleton_confirm_node`/`await_realtime_approval_node`
    这类节点正常暂停时也会抛这个异常，跟真正的node执行失败是两回事。

    call_count按(project_id, turn_id, node_name)累计（docs/ISSUES.md第142条修复）——早期版本
    只按(project_id, node_name)累计，是跨整个worker进程生命周期的累积值，不是"这一轮用户交互
    内第几跳"，真实表现是连续3条无关的飞书消息只要都路由到同一个node就会显示"第3次"，用户会
    误解成"这一轮被打回了3次"。turn_id由发起新一轮交互的入口函数生成（run_plan/run_edit/
    run_companion/run_channel_start），resume类调用（resume_companion/run_channel_resume/
    run_plan内部confirm循环）复用同一turn_id。state里没有turn_id时（理论上不该发生）退化成
    空字符串，保持向后兼容不报错。
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(state: GraphState) -> dict:
            project_id = state.get("project_id")
            if not project_id:
                return fn(state)
            turn_id = state.get("turn_id") or ""
            key = (project_id, turn_id, node_name)
            with _run_status_lock:
                _node_call_counts[key] = _node_call_counts.get(key, 0) + 1
                call_count = _node_call_counts[key]
            started_at = _iso_now()
            store.write_run_status(project_id, {
                "node": node_name,
                "status": "running",
                "call_count": call_count,
                "started_at": started_at,
                "finished_at": None,
                "error": None,
            })
            try:
                result = fn(state)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                store.write_run_status(project_id, {
                    "node": node_name,
                    "status": "failed",
                    "call_count": call_count,
                    "started_at": started_at,
                    "finished_at": _iso_now(),
                    "error": str(exc),
                })
                raise
            store.write_run_status(project_id, {
                "node": node_name,
                "status": "done",
                "call_count": call_count,
                "started_at": started_at,
                "finished_at": _iso_now(),
                "error": None,
            })
            return result
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# 图组装
# ---------------------------------------------------------------------------

_NODES = [
    "material_agent_node",
    "requirements_gather_node",
    "collect_requirements",
    "route_draft",
    "await_skeleton_confirm",
    "await_route_structure_confirm",
    "route_citation_check_node",
    "route_map_check_node",
    "route_timeliness_check_node",
    "lifestyle_agent_node",
    "citation_check_node",
    "pitfall_agent_node",
    "pitfall_citation_check_node",
    "integration_agent_node",
    "guide_agent_node",
    "realtime_agent_node",
    "await_realtime_approval_node",
    "companion_lifestyle_query_node",
    "companion_pitfall_query_node",
    "companion_culture_query_node",
    "culture_agent_node",
    "poi_detail_edit_node",
]

# node名 -> 节点函数名（字符串，不是函数对象本身）：build_graph()每次调用时通过globals()
# 现查现取，而不是在模块导入时就把函数对象固化进字典——测试里大量用
# monkeypatch.setattr(orchestrator, "integration_agent_node", fake_fn)替换模块级函数
# 再调用run_plan/run_companion（内部会重新build_graph()），如果这里存的是函数对象本身，
# monkeypatch换的是模块属性、不会影响已经存进字典里的旧引用，图里注册的还是没打桩前的
# 原函数，测试会静默失效（真实碰到过，见docs/ISSUES.md）。
_NODE_FUNCS: dict[str, str] = {
    "material_agent_node": "material_agent_node",
    "requirements_gather_node": "requirements_gather_node",
    "collect_requirements": "collect_requirements_node",
    "route_draft": "route_draft_node",
    "await_skeleton_confirm": "await_skeleton_confirm_node",
    "await_route_structure_confirm": "await_route_structure_confirm_node",
    "route_citation_check_node": "route_citation_check_node",
    "route_map_check_node": "route_map_check_node",
    "route_timeliness_check_node": "route_timeliness_check_node",
    "lifestyle_agent_node": "lifestyle_agent_node",
    "citation_check_node": "citation_check_node",
    "pitfall_agent_node": "pitfall_agent_node",
    "pitfall_citation_check_node": "pitfall_citation_check_node",
    "integration_agent_node": "integration_agent_node",
    "poi_detail_agent_node": "poi_detail_agent_node",
    "poi_details_join_node": "poi_details_join_node",
    "guide_agent_node": "guide_agent_node",
    "realtime_agent_node": "realtime_agent_node",
    "await_realtime_approval_node": "await_realtime_approval_node",
    "companion_lifestyle_query_node": "companion_lifestyle_query_node",
    "companion_pitfall_query_node": "companion_pitfall_query_node",
    "companion_culture_query_node": "companion_culture_query_node",
    "culture_agent_node": "culture_agent_node",
    "poi_detail_edit_node": "poi_detail_edit_node",
}


def build_graph(project_id: str):
    graph = StateGraph(GraphState)
    for node_name, func_name in _NODE_FUNCS.items():
        graph.add_node(node_name, _track_run_status(node_name)(globals()[func_name]))

    graph.add_conditional_edges(START, main_agent_router)
    for node_name in _NODES:
        graph.add_conditional_edges(node_name, main_agent_router)
    # poi_detail_agent_node是Send fan-out的目标：跑完统一走静态边到join节点（不经过
    # main_agent_router），LangGraph会在所有并行分支都到达poi_details_join_node后才
    # 真正执行它一次（标准的Send/map-reduce join行为，见docs/ORCHESTRATION.md第3.3节）。
    # join节点本身完成后再回到main_agent_router（这时progress.state已经是"完成"）。
    graph.add_edge("poi_detail_agent_node", "poi_details_join_node")
    graph.add_conditional_edges("poi_details_join_node", main_agent_router)

    # 2026-08-13改用持久化checkpointer（见docs/ISSUES.md第120条）：per-project一个
    # sqlite文件，进程崩溃后重新build_graph(project_id)+同一个thread_id还能看到磁盘上
    # 的checkpoint历史，支持run_plan()断点续跑。之前用InMemorySaver时，检查点历史绑在
    # 具体的CompiledStateGraph对象上，跨进程/甚至同进程内新建一次graph对象都会丢，这也
    # 是_companion_graph模块级单例存在过的原因——持久化后不再需要那个单例，见其移除说明。
    return graph.compile(checkpointer=get_graph_checkpointer(project_id))


def prepare_langgraph_fixed_route(
    source_project_id: str,
    target_project_id: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    comparison_grade: str = "C",
    require_comparison_sidecars: bool = False,
) -> dict:
    """Create one isolated LangGraph target from a canonical fixed-route input."""
    return prepare_fixed_route_project(
        source_project_id,
        target_project_id,
        harness="langgraph",
        model=model,
        provider=provider,
        comparison_grade=comparison_grade,
        require_comparison_sidecars=require_comparison_sidecars,
    )


def _with_project_plan_run_lock(fn):
    @functools.wraps(fn)
    def wrapper(project_id: str, *args, **kwargs):
        with store.project_plan_run_lock(project_id):
            return fn(project_id, *args, **kwargs)

    return wrapper


@_with_project_plan_run_lock
def run_plan(project_id: str, requirements: dict) -> dict:
    topic = requirements["destination"]
    graph = build_graph(project_id)
    config = {"configurable": {"thread_id": f"{project_id}:plan"}}
    # 本轮turn的标识（docs/ISSUES.md第142条）：只在这里生成一次，下面自动confirm循环内的
    # 每次Command(resume=...)都要带上同一个turn_id，让循环内多次node调用仍归属同一turn。
    turn_id = uuid.uuid4().hex[:8]

    # 2026-08-13新增断点续跑检测（见docs/ISSUES.md第120条）：graph.get_state(config)读
    # 磁盘上这个thread_id已有的checkpoint历史（用SqliteSaver后跨进程也能看到）。有历史
    # 就续跑（input=None，从上次checkpoint继续，不能传fresh initial_state——那会被当成
    # "从START重新进入图"），没有历史才是真正的首次调用。之前进程崩溃后只能改project_id
    # 从头重跑，几小时的进度全部作废；现在用同一个project_id重新调用run_plan()就能接着跑，
    # 已经完整执行完的节点不会重复执行。
    state = graph.get_state(config)
    if state.values:
        checkpoint_project_id = state.values.get("project_id")
        checkpoint_topic = state.values.get("topic")
        checkpoint_requirements = state.values.get("requirements")
        if (
            checkpoint_project_id != project_id
            or checkpoint_topic != topic
            or checkpoint_requirements != requirements
        ):
            raise ValueError(
                "run_plan恢复参数与checkpoint不一致；已拒绝续跑且未改写requirements.json"
            )

        requirements_path = store.workspace_dir(project_id) / "requirements.json"
        requirements_missing = not requirements_path.exists()
        try:
            saved = store.read_requirements(project_id)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "requirements.json已存在但无法读取；已拒绝续跑且未覆盖磁盘文件"
            ) from exc
        if not requirements_missing:
            if not isinstance(saved, dict) or (
                saved.get("topic") != checkpoint_topic
                or saved.get("requirements") != checkpoint_requirements
            ):
                raise ValueError(
                    "requirements.json与checkpoint不一致；已拒绝续跑且未覆盖磁盘文件"
                )
        if not state.next:
            # 已完成checkpoint是终态，再次调用只返回现有状态，不能重新invoke图、
            # 刷新requirements.json，或为历史缺文件做非必要回填。
            return dict(state.values)
        if requirements_missing:
            # 兼容requirements.json尚未由旧版本持久化的历史checkpoint。checkpoint是
            # 唯一允许的回填来源；上面的比较先证明了本次调用参数与它完全一致。
            store.write_requirements(
                project_id,
                checkpoint_topic,
                checkpoint_requirements,
            )
        if state.next and any("await_route_structure_confirm" in n for n in state.next):
            # 结构变化必须专门确认，run_plan没有真人输入时也不能代替用户静默接受。
            result = graph.invoke(None, config)
        elif state.next and any("await_skeleton_confirm" in n for n in state.next):
            # 只有首次骨架确认在run_plan的自治契约内。其他interrupt即使节点名以
            # await_开头，也必须先返回给调用方，不能由Codex/E2E猜测决定。
            result = graph.invoke(Command(resume="confirm", update={"turn_id": turn_id}), config)
        else:
            # 上次执行到一半（某个节点抛异常导致进程崩溃），从上次checkpoint继续——这是跨
            # 进程重启的续跑场景，不生成新turn_id（沿用checkpoint里的旧值），graph.invoke(None, ...)
            # 本身也不能带update，属于第142条明确不处理的场景，见ISSUES.md。
            result = graph.invoke(None, config)
    else:
        # Material research resolves the destination, harness and audit context from
        # requirements.json, so only a genuinely fresh plan persists invocation input
        # before the first graph node can call retrieve_materials.
        store.init_project(project_id)
        store.write_requirements(project_id, topic, requirements)
        initial_state: GraphState = {
            "project_id": project_id,
            "topic": topic,
            "mode": "plan",
            "requirements": requirements,
            "turn_id": turn_id,
        }
        result = graph.invoke(initial_state, config)

    # 没有真人在场时只自动确认合法的首次骨架。确认后的结构候选，以及仍未通过硬约束的
    # 骨架都必须把interrupt原样返回，避免静默接受或陷入confirm重试死循环。
    while "__interrupt__" in result:
        raw_interrupt = result["__interrupt__"][0]
        payload = getattr(raw_interrupt, "value", raw_interrupt)
        if not (
            "route_plan" in payload
            and payload.get("can_confirm") is True
            and "route_change_proposal" not in payload
        ):
            break
        result = graph.invoke(Command(resume="confirm", update={"turn_id": turn_id}), config)
    if "__interrupt__" in result:
        result = dict(result)
        result["status"] = "action_required"
    return result


@_with_project_plan_run_lock
def resume_plan_route_decision(project_id: str, decision: str) -> dict:
    """Resume an explicit post-confirmation route proposal decision.

    This is the non-channel counterpart of ``run_channel_resume`` for the
    ``:plan`` checkpoint.  It never invents a decision and only runs when the
    graph is currently waiting at the route-structure confirmation node.
    """
    if decision not in {"confirm", "keep"} and not decision.startswith("revise:"):
        raise ValueError("decision必须是confirm、keep或revise:<意见>")

    graph = build_graph(project_id)
    config = {"configurable": {"thread_id": f"{project_id}:plan"}}
    state = graph.get_state(config)
    if not state.values or not state.next:
        raise ValueError("当前plan没有等待确认的路线结构候选")
    if not any("await_route_structure_confirm" in node for node in state.next):
        raise ValueError("当前plan不在路线结构确认节点")

    result = graph.invoke(
        Command(resume=decision, update={"turn_id": uuid.uuid4().hex[:8]}),
        config,
    )
    if "__interrupt__" in result:
        result = dict(result)
        result["status"] = "action_required"
    return result


def run_edit(
    project_id: str,
    entry_target: str,
    edit_request: str,
    day: Optional[int] = None,
    poi_name: Optional[str] = None,
) -> dict:
    """已完成项目的定点修改入口，对照run_plan。见docs/ISSUES.md第28/29条。

    entry_target是graph节点名（如"lifestyle_agent_node"、"integration_agent_node"），
    不是agent的逻辑名——跟_route_edit_mode第一跳`return state["entry_target"]`直接对应。

    day/poi_name仅在entry_target="poi_detail_edit_node"（POI详情agent的edit入口，
    docs/ARCHITECTURE.md第3.4节第9点）时需要提供，用于定位具体是哪一个POI详情文件——
    跟lifestyle/pitfall/route不同，POI详情agent同一个project里有多个并行实例，
    单靠entry_target这一个字符串无法定位到具体是哪一个。
    """
    if entry_target == "poi_detail_edit_node" and (day is None or not poi_name):
        raise ValueError(
            "entry_target='poi_detail_edit_node'时必须同时提供day和poi_name，"
            "用于定位要编辑的具体是哪一个POI详情文件。"
        )

    saved = store.read_requirements(project_id)
    if saved is None:
        raise ValueError(
            f"项目{project_id!r}没有找到已保存的requirements.json，无法进入edit模式——"
            f"这个项目可能是run_plan补丁前生成的旧数据，或者project_id写错了。"
        )
    topic, requirements = saved["topic"], saved["requirements"]

    graph = build_graph(project_id)
    # 用独立的thread_id（跟"{project_id}:plan"区分），避免复用同一个checkpointer thread
    # 导致interrupt/state历史串联到一起。
    config = {"configurable": {"thread_id": f"{project_id}:edit"}}

    initial_state: GraphState = {
        "project_id": project_id,
        "topic": topic,
        "mode": "edit",
        "requirements": requirements,
        "entry_target": entry_target,
        "edit_request": edit_request,
        # 每次run_edit()调用都是独立的一次用户操作（不会resume之前的interrupt），
        # 生成新turn_id（docs/ISSUES.md第142条）。
        "turn_id": uuid.uuid4().hex[:8],
    }
    if day is not None:
        initial_state["day"] = day
    if poi_name is not None:
        initial_state["poi_name"] = poi_name
    return graph.invoke(initial_state, config)



_COMPANION_ENTRY_NODE_MAP = {
    "guide_agent": "guide_agent_node",
    "realtime_agent": "realtime_agent_node",
    "lifestyle_agent": "companion_lifestyle_query_node",
    "pitfall_agent": "companion_pitfall_query_node",
    "culture_agent": "companion_culture_query_node",
}

# 2026-08-13起不再需要陪伴阶段专用的编译图缓存单例（见docs/ISSUES.md第120条）：
# build_graph()改用持久化SqliteSaver后，任意时刻新建的build_graph(project_id)只要指向
# 同一个per-project sqlite文件+同一个thread_id，都能看到磁盘上同一份checkpoint历史——
# resume_companion()不再需要复用同一个Python对象才能找到await_realtime_approval_node
# 暂停的thread，直接每次build_graph(project_id)即可。之前用InMemorySaver时必须靠模块级
# 单例（_companion_graph）绕开"检查点历史绑在具体CompiledStateGraph对象上、不跨对象共享"
# 这条限制，现在这层限制已经不存在了。


def run_companion(project_id: str, user_message: str, entry_target: Optional[str] = None) -> dict:
    """陪伴阶段（docs/ORCHESTRATION.md第3.4节）的自由对话入口，对照run_edit。

    entry_target为空时（用户没有显式@任何角色）：先跑一次_classify_companion_intent
    五选一（guide_agent/realtime_agent/lifestyle_agent/pitfall_agent/culture_agent），映射到对应的graph
    节点名再进入图；entry_target非空时（用户显式@，如"guide_agent_node"/"realtime_agent_node"/
    "pitfall_agent_node"）直接透传，跟run_edit的entry_target语义完全一致。

    跟run_plan不同：命中"__interrupt__"（realtime_agent发起修改，等待审批）时**直接原样
    返回**，不自动确认——docs/ORCHESTRATION.md明确"这条线不因为陪伴阶段的交互更频繁就
    放松"，真实审批不能被静默跳过。调用方需要展示patch给人工，再用resume_companion继续。
    """
    saved = store.read_requirements(project_id)
    if saved is None:
        raise ValueError(
            f"项目{project_id!r}没有找到已保存的requirements.json，无法进入陪伴阶段——"
            f"这个项目可能是run_plan补丁前生成的旧数据，或者project_id写错了。"
        )
    topic, requirements = saved["topic"], saved["requirements"]

    recent_context = companion_history.build_recent_context(project_id)
    resolved_target = entry_target
    if not resolved_target:
        intent = _classify_companion_intent(
            _companion_route_text(user_message, recent_context),
            candidates=["guide_agent", "realtime_agent", "lifestyle_agent", "pitfall_agent", "culture_agent"],
        )
        resolved_target = _COMPANION_ENTRY_NODE_MAP[intent]

    graph = build_graph(project_id)
    # 独立于":plan"/":edit"的thread_id，避免陪伴阶段的自由问答和正式edit修改的历史串联到一起。
    config = {"configurable": {"thread_id": f"{project_id}:companion"}}

    # 2026-07-21真实E2E发现的问题（见docs/ISSUES.md）：":companion"这个thread_id按project
    # 固定复用（同一个project的所有陪伴阶段问答共用一个thread，这是为了让
    # await_realtime_approval_node的interrupt/resume能在同一个thread内找到彼此，见
    # resume_companion的说明）。但LangGraph对没有显式reducer的普通dict字段的默认语义是
    # "这次输入没提供的key，保留上一次checkpoint的值"——如果initial_state只传
    # entry_target/companion_question这几个"看起来是这次要用的"字段，last_agent_result
    # 等路由记账字段会从上一轮问答的checkpoint里原样"渗透"过来。真实复现过：连续问两个
    # 不同问题（第一次@guide_agent，第二次@realtime_agent），第二次调用因为
    # last_agent_result不是None（上一轮遗留的），被_route_edit_mode误判成"已经不是第一跳"，
    # entry_target被完全无视，直接复用上一轮的路由结果，图里没有任何节点被真正执行。
    # 修复：显式把所有陪伴阶段路由/执行依赖的记账字段重置为"全新一轮"的默认值——只重置
    # 这些记账字段，不动workspace文件里的真实持久化数据（那些数据的最新状态本来就该
    # 保留，只是这次一轮问答的"routing scratch"不能带着上一轮的痕迹）。
    initial_state: GraphState = {
        "project_id": project_id,
        "topic": topic,
        "mode": "edit",
        "requirements": requirements,
        "entry_target": resolved_target,
        "edit_request": None,
        "companion_question": user_message,
        "companion_recent_context": recent_context,
        "last_agent_result": None,
        # 每次run_companion()调用都是一次新的用户消息（不是resume之前的interrupt），
        # 生成新turn_id（docs/ISSUES.md第142条）——resume_companion()续审批时复用这里
        # 生成的turn_id（从checkpoint里读回），不会覆盖成新值。
        "turn_id": uuid.uuid4().hex[:8],
        "route_agent_done": False,
        "lifestyle_agent_done": False,
        "edit_citation_checked": False,
        # pitfall_agent_done/pitfall_citation_checked是2026-07-23新增的字段——这里正是
        # docs/ISSUES.md第34条警告过的那个"容易被遗漏的模式"（GraphState新增陪伴阶段
        # 相关字段时容易忘记同步进这份重置清单），补上避免同一类bug在避雷agent上重演。
        "pitfall_agent_done": False,
        "pitfall_citation_checked": False,
        "integration_done": False,
        "force_finalize": False,
        "awaiting_integration_recheck": False,
        "integration_retry_count": 0,
        "edit_original_lifestyle": None,
        "edit_original_guide_len": None,
        "edit_original_pitfall": None,
        "companion_guide_answer": None,
        "guide_covered": None,
        "companion_fallback_answer": None,
        "companion_answer": None,
        "realtime_pending_patch": None,
        "realtime_approval_done": False,
        "realtime_patch_applied": False,
    }
    result = graph.invoke(initial_state, config)
    answer = result.get("companion_answer") if isinstance(result, dict) else None
    if isinstance(answer, str) and answer.strip() and "__interrupt__" not in result:
        agent = ((result.get("last_agent_result") or {}).get("agent") or "")
        companion_history.append_turn(project_id, user_message, answer, agent=agent)
    return result


def resume_companion(project_id: str, decision: str) -> dict:
    """resume陪伴阶段被await_realtime_approval_node打断的审批。持久化checkpointer后
    build_graph(project_id)不需要跟run_companion复用同一个Python对象——只要thread_id
    一致（"{project_id}:companion"），指向同一个sqlite文件就能找到之前暂停的thread。
    decision约定"approve"批准/"reject"拒绝，见await_realtime_approval_node。

    turn_id（docs/ISSUES.md第142条）：审批回复是用户发来的另一条独立飞书消息，触发的是
    独立的一次_run_turn（service.py），跟触发run_companion()的那条消息不是同一个turn——
    生成新turn_id，不复用被打断前的旧值。await_realtime_approval_node被resume时会
    从函数开头重新执行一遍（LangGraph interrupt恢复的标准行为），如果复用旧turn_id，
    call_count会从打断前累积的值继续+1，对用户显示成"（第2次）"，但这次审批回复从用户
    视角是这次交互唯一的一次执行，不该带着"第几次"的误导性后缀。
    """
    graph = build_graph(project_id)
    config = {"configurable": {"thread_id": f"{project_id}:companion"}}
    return graph.invoke(Command(resume=decision, update={"turn_id": uuid.uuid4().hex[:8]}), config)


def _persist_channel_requirements(project_id: str, result: dict) -> None:
    """真实联调发现的缺口：run_plan()末尾会持久化requirements.json（供run_edit/
    run_companion后续复用），但run_channel_start/run_channel_resume（飞书渠道的多轮
    需求收集入口）从来没有调用过——飞书生成完的project进不了陪伴阶段、也进不了edit
    模式，报"没有找到已保存的requirements.json"。requirements_gather_node收集完整后
    会把requirements/topic写进state，图继续往下跑，之后每次调用的返回结果里这两个key
    都会一直非空（LangGraph状态累积语义）——每次都重写一遍是幂等的，不需要判断"是不是
    第一次出现"，代价是多几次廉价的本地写文件。requirements_draft阶段（多轮收集还没问完）
    这两个key仍是初始的空值，天然不会误触发。"""
    requirements = result.get("requirements")
    topic = result.get("topic")
    if requirements and topic:
        store.write_requirements(project_id, topic, requirements)


def run_channel_start(project_id: str, first_message: str) -> dict:
    """长连接渠道（目前是飞书，见src/channels/feishu/、docs/ARCHITECTURE.md第14节）
    "开启一次全新对话"的入口，跟run_plan不同——不要求调用方提前准备好完整的
    requirements dict，只给一句自由文本，交给requirements_gather_node做真正的多轮
    追问收集（见该节点的docstring）。

    用户的开场白（如"三天成都旅游"）不应该被无视、逼用户重新答一遍requirements_gather_node
    的固定第一问——这里预先做一次结构化抽取，把能提取到的字段填进requirements_draft的
    初始值。这样如果开场白已经把destination/days都说了，节点第一次interrupt()问的就是
    风格/预算这类可选偏好，而不是重复问目的地/天数。

    持久化checkpointer后不再需要跟companion复用同一个Python图对象——build_graph(project_id)
    只要thread_id一致（"{project_id}:channel"）就能在同一个sqlite文件里找到这个thread，
    确保后续run_channel_resume()续得上。命中interrupt时直接原样返回中断payload，**不自动
    confirm**——这是飞书场景区别于run_plan（CLI/测试场景自动confirm模拟没有真人在场）最
    核心的一点，必须真等真人回复，跟run_companion的同款设计取舍一致。
    """
    store.init_project(project_id)
    initial_draft = _extract_requirements_fields(first_message)
    graph = build_graph(project_id)
    config = {"configurable": {"thread_id": f"{project_id}:channel"}}
    initial_state: GraphState = {
        "project_id": project_id,
        "topic": initial_draft.get("destination", ""),
        "mode": "plan",
        "requirements": {},
        "requirements_draft": initial_draft,
        # 开场白是这次对话的第一条消息，新turn（docs/ISSUES.md第142条）。
        "turn_id": uuid.uuid4().hex[:8],
    }
    result = graph.invoke(initial_state, config)
    _persist_channel_requirements(project_id, result)
    return result


def run_channel_resume(project_id: str, resume_value: str) -> dict:
    """resume长连接渠道":channel"这个thread上任意一个interrupt()——可能是
    requirements_gather_node的追问，也可能是骨架确认/实时审批（一旦requirements收集
    完成，图会在同一个thread里自然流转到await_skeleton_confirm_node等既有节点）。
    跟run_channel_start共用同一份磁盘checkpoint历史（build_graph(project_id)+同一个
    thread_id），不需要复用同一个Python图对象。

    resume_value的语义由调用方决定：多轮需求收集阶段直接透传用户原话（喂给
    requirements_gather_node内部的结构化抽取）；骨架确认/审批阶段需要调用方先用
    src/channels/feishu/intent.py把自然语言分类成节点期望的固定值
    （"confirm"/"revise:<意见>"/"approve"/"reject"）。

    每次调用都是用户发来的独立一条飞书消息，生成新turn_id（docs/ISSUES.md第142条），
    理由同resume_companion的说明——被resume的node会从头重新执行一次，不应该带着
    被打断前的call_count继续累加。
    """
    graph = build_graph(project_id)
    config = {"configurable": {"thread_id": f"{project_id}:channel"}}
    result = graph.invoke(Command(resume=resume_value, update={"turn_id": uuid.uuid4().hex[:8]}), config)
    _persist_channel_requirements(project_id, result)
    return result
