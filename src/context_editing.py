"""ReAct循环内的上下文压缩（2026-08-11新增，见docs/ISSUES.md相关条目）。

背景：route_agent/lifestyle_agent这类绑了retrieve_materials的agent，在单次agent.invoke()内部
的ReAct循环里（不是外层重试之间）会反复调用retrieve_materials、
retrieve_project_evidence、read_workspace和web_search，每轮几千到上万字符原文滚雪球式
累加。真实trace（LLM_TRACE_FULL=1落盘，见src/llm_trace.py）证实：累计上下文超过约4万字符
后，历史中转渠道504网关超时概率骤增（同一份134872字符的历史被原样重试18次，前17次
全504）。

这个模块提供两样东西：
1. 按工具类型分发的摘要函数（_summarize_tool_result/_minify_tool_result，两级），给"用过
   一次就可以放手"的工具原文（retrieve_materials/retrieve_project_evidence/
   read_workspace/web_search）生成保留关键定位信息的摘要，不是空占位符。同时给
   orchestrator.py里_compress_retrieval_history复用，避免维护两份摘要逻辑。
2. build_context_editing_hook：构造一个langgraph.prebuilt.create_react_agent的
   pre_model_hook——这个hook在ReAct循环内部每次调模型之前都会执行（不是invoke()返回后才跑
   一次），按完整请求预算压缩工具结果并清理已完成的旧交互轮次。

设计要点（详见grilling讨论/plan文件）：
- 预算统计覆盖system prompt、few-shot、工具schema、消息正文和AIMessage.tool_calls参数；
  同时设置字符、近似token和消息条数阈值，避免只看ToolMessage正文时出现漏算。
- 近期窗口是软保护：优先保留原始任务、record_finding和最新交互；如果三层内容压缩后仍超限，
  删除更早的完整AI+Tool调用对及旧重试说明，而不是让短消息轮次继续线性增长。
- 只压缩内容长度超过_MIN_COMPRESS_LEN的ToolMessage：这条规则顺带保护了orchestrator.py里
  _count_trailing_empty_retrievals依赖的"没有检索到相关素材"这类关键词字符串——不需要为这个
  关键词单独写例外逻辑，因为空结果本来就很短，天然不会被这条规则压缩。
- AIMessage.tool_calls参数计入预算。普通已完成调用可随其ToolMessage一起从旧历史移除；
  record_finding调用及其参数单独保护，作为跨工作单元仍需保留的持久结论。
- 失败的write_*调用只删除更早失败轮；最新一轮的完整工具参数和精简错误是
  当前局部修复底稿，不能为压缩而替换为空占位符。若该底稿本身仍使完整请求超过
  硬预算，则显式停止。
- **三层预算，不是两层**（2026-08-11真实E2E复测踩出的坑，见docs/ISSUES.md）：最初只设计了
  "最近N条留原文，更早的压成富摘要"两层，真实跑青甘大环线时lifestyle_agent一次做了72次
  retrieve_materials/web_search调用（每次retrieve_materials默认top_k=5，压缩后单条约
  400+字符），"压缩摘要"这个桶本身随调用次数线性增长，72次×400+字符=近3万字符，重新逼近
  4万字符硬阈值——压缩了"每一条的体积"，没有压缩"条目数量本身"的增长。修复：压缩摘要
  （tier2）也设一个预算上限，超出这个预算的更老条目进一步压成tier3极简形式（只留
  chunk_id/URL列表，不留platform/author/snippet正文）。若极简条目数量本身仍导致超限，
  再进入轮次清理；仍无法降到硬预算内则显式停止，不自动续窗。
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from src.llm_trace import append_context_event

_MIN_COMPRESS_LEN = 200
_READ_WORKSPACE_TRUNCATE_LEN = 200
_READ_WORKSPACE_MINIFY_LEN = 40
_PROJECT_EVIDENCE_EXCERPT_LEN = 240

_DEFAULT_COMPRESSIBLE_TOOLS = (
    "retrieve_materials",
    "retrieve_project_evidence",
    "read_workspace",
    "web_search",
)
_DEFAULT_TRIGGER_CHARS = 15000
# The historical relay started degrading around 40k message characters. Keep
# that boundary as a hard secondary guard while the 32k approximate-token cap
# remains the primary request limit. A lower 35k ceiling can reject an otherwise
# valid latest repair draft even when the complete request is below 32k tokens.
_DEFAULT_SAFETY_CEILING_CHARS = 40000
_DEFAULT_KEEP_BUDGET_FRACTION = 0.5
_DEFAULT_SUMMARY_BUDGET_FRACTION = 0.3
_DEFAULT_TRIGGER_TOKENS = 12000
_DEFAULT_SAFETY_CEILING_TOKENS = 32000
_DEFAULT_MAX_MESSAGES = 96
_DEFAULT_RECENT_MESSAGES = 8
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 2.0

_WEB_SEARCH_LINE_RE = re.compile(r"^- (?P<title>.*?)：.*?（来源：(?P<url>.*?)）$")
_WEB_SEARCH_URL_RE = re.compile(r"（来源：(?P<url>.*?)）")
_CHUNK_ID_RE = re.compile(r"\[([^\]]+)\]")
_CONVERGENCE_NUDGE_MARKER = "[[convergence_nudge:"
_READ_WORKSPACE_SUMMARY_PREFIX = "已读取工作区文件（内容较长，已压缩前"
_READ_WORKSPACE_MINIFIED_PREFIX = "（更早读取过的工作区文件，节选："
_PROJECT_EVIDENCE_SUMMARY_PREFIX = (
    "已检索到以下Project Evidence（较早原文已压缩为有界摘录）：\n"
)
_PROJECT_EVIDENCE_MINIFIED_PREFIX = (
    "（更早的Project Evidence，仅保留可重新定位的信息）：\n"
)


@dataclass(frozen=True)
class ConvergenceNudgePolicy:
    """Optional soft reminder for research-heavy work units."""

    work_unit: str
    research_tools: tuple[str, ...]
    expected_write_tools: tuple[str, ...]
    delivery_description: str
    first_threshold: int = 6
    second_threshold: int = 10


def _summarize_retrieve_materials(content: Any) -> Any:
    """把retrieve_materials返回的JSON全文（含每条chunk完整content_snippet）压缩成
    chunk_id+平台/作者+一句摘要。JSON解析失败（如"没有检索到相关素材。"这类短句）原样返回，
    不能破坏_count_trailing_empty_retrievals依赖的关键词匹配。"""
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    try:
        results = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(results, list):
        return content
    lines = [
        f"[{r.get('chunk_id', '')}] {r.get('platform', '')}/{r.get('author', '')}："
        f"{(r.get('content_snippet') or '')[:40]}..."
        for r in results
        if isinstance(r, dict)
    ]
    if not lines:
        return content
    return (
        "已检索到以下素材（原文已压缩为摘要，无需重看全文；"
        "如需原文可用chunk_id重新检索）：\n" + "\n".join(lines)
    )


def _project_evidence_records(content: Any) -> list[dict[str, Any]] | None:
    """Parse raw or tier2 Project Evidence without guessing other tool schemas."""
    if isinstance(content, list):
        if any(
            isinstance(item, dict)
            and (item.get("chunk_id") or item.get("source_url"))
            for item in content
        ):
            results = content
        else:
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
            results = None
    else:
        results = None
    if results is None:
        if not isinstance(content, str):
            return None
        if content.startswith(_PROJECT_EVIDENCE_MINIFIED_PREFIX):
            return None
        if content.startswith(_PROJECT_EVIDENCE_SUMMARY_PREFIX):
            content = content[len(_PROJECT_EVIDENCE_SUMMARY_PREFIX) :]
        try:
            results = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(results, list):
        return None
    records = [item for item in results if isinstance(item, dict)]
    return records or None


def _bounded_project_evidence_excerpt(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= _PROJECT_EVIDENCE_EXCERPT_LEN:
        return text
    return text[: _PROJECT_EVIDENCE_EXCERPT_LEN - 3] + "..."


def _summarize_project_evidence(content: Any) -> Any:
    """Tier2 keeps citable locators plus a bounded excerpt from each result."""
    if isinstance(content, str) and content.startswith(
        (_PROJECT_EVIDENCE_SUMMARY_PREFIX, _PROJECT_EVIDENCE_MINIFIED_PREFIX)
    ):
        return content
    records = _project_evidence_records(content)
    if records is None:
        return content
    compact = []
    for record in records:
        item = {
            "chunk_id": str(record.get("chunk_id") or ""),
            "title": str(record.get("title") or ""),
            "source_platform": str(
                record.get("source_platform") or record.get("platform") or ""
            ),
            "source_url": str(record.get("source_url") or ""),
            "excerpt": _bounded_project_evidence_excerpt(
                record.get("content_snippet") or record.get("excerpt")
            ),
        }
        if item["chunk_id"] or item["title"] or item["source_url"]:
            compact.append(item)
    if not compact:
        return content
    return _PROJECT_EVIDENCE_SUMMARY_PREFIX + json.dumps(
        compact,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _summarize_web_search(content: Any) -> Any:
    """把web_search返回的"- 标题：内容摘要（来源：URL）"逐行文本压缩成"- 标题（来源：URL）"，
    砍掉占体积大头的内容摘要正文。格式不匹配（如"联网搜索没有找到相关结果..."这类提示句）
    原样返回。"""
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    if not isinstance(content, str):
        return content
    lines = content.splitlines()
    summarized_lines = []
    matched_any = False
    for line in lines:
        m = _WEB_SEARCH_LINE_RE.match(line)
        if m:
            matched_any = True
            summarized_lines.append(f"- {m.group('title')}（来源：{m.group('url')}）")
        else:
            summarized_lines.append(line)
    if not matched_any:
        return content
    return (
        "已联网搜索以下结果（原文已压缩为摘要，无需重看全文；如需原文可重新调用web_search）：\n"
        + "\n".join(summarized_lines)
    )


def _summarize_read_workspace(content: Any) -> Any:
    """把read_workspace返回的任意JSON截断成前_READ_WORKSPACE_TRUNCATE_LEN字符+提示，
    不做结构解析（不同文件schema不固定，不值得为每种文件写解析规则）。"文件不存在: xxx"
    这类短提示原样返回（走不到这里，长度门槛已经拦住）。"""
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    if not isinstance(content, str):
        return content
    if content.startswith(
        (_READ_WORKSPACE_SUMMARY_PREFIX, _READ_WORKSPACE_MINIFIED_PREFIX)
    ):
        return content
    truncated = content[:_READ_WORKSPACE_TRUNCATE_LEN]
    return (
        f"已读取工作区文件（内容较长，已压缩前{_READ_WORKSPACE_TRUNCATE_LEN}字）：{truncated}...\n"
        "若工具提供筛选参数，请缩小范围读取；不要重复读取同一整份文件。"
    )


_SUMMARIZERS: dict[str, Callable[[Any], Any]] = {
    "retrieve_materials": _summarize_retrieve_materials,
    "retrieve_project_evidence": _summarize_project_evidence,
    "web_search": _summarize_web_search,
    "read_workspace": _summarize_read_workspace,
}


def _minify_retrieve_materials(content: Any) -> Any:
    """tier3极简化：只留chunk_id列表，不留platform/author/snippet正文。原文可能是尚未压缩
    过的原始JSON（json.loads能解析），也可能是已经被_summarize_retrieve_materials压缩过
    一次的富摘要（这时候原文已经不是JSON了，改用正则从"[chunk_id]"这种格式里提取）——
    两种输入都要处理，因为hook触发时不保证一条消息此前有没有被压缩过。"""
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    if not isinstance(content, str):
        return content
    try:
        results = json.loads(content)
        if isinstance(results, list):
            chunk_ids = [r.get("chunk_id", "") for r in results if isinstance(r, dict)]
        else:
            chunk_ids = None
    except (json.JSONDecodeError, TypeError):
        chunk_ids = None
    if chunk_ids is None:
        chunk_ids = _CHUNK_ID_RE.findall(content)
    if not chunk_ids:
        return content
    return f"[{','.join(chunk_ids)}]（更早的检索结果，如需可用chunk_id重新检索）"


def _minify_project_evidence(content: Any) -> Any:
    """Tier3 keeps only enough identity to find or cite older evidence again."""
    if isinstance(content, str) and content.startswith(_PROJECT_EVIDENCE_MINIFIED_PREFIX):
        return content
    records = _project_evidence_records(content)
    if records is None:
        return content
    locators: list[str] = []
    for record in records:
        chunk_id = str(record.get("chunk_id") or "").strip()
        title = str(record.get("title") or "").strip()
        source_url = str(record.get("source_url") or "").strip()
        if chunk_id and title:
            locator = f"[{chunk_id}] {title}"
        elif chunk_id and source_url:
            locator = f"[{chunk_id}] {source_url}"
        elif chunk_id:
            locator = f"[{chunk_id}]"
        else:
            locator = title or source_url
        if locator and locator not in locators:
            locators.append(locator)
    if not locators:
        return content
    return _PROJECT_EVIDENCE_MINIFIED_PREFIX + "\n".join(
        f"- {locator}" for locator in locators
    )


def _minify_web_search(content: Any) -> Any:
    """tier3极简化：只留来源URL列表，不留标题/正文。同样要兼容"尚未压缩的原始文本"和
    "已经被_summarize_web_search压缩过一次的富摘要"两种输入。"""
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    if not isinstance(content, str):
        return content
    urls = _WEB_SEARCH_URL_RE.findall(content)
    if not urls:
        return content
    return f"（更早的联网搜索结果，来源：{','.join(urls)}，如需可重新调用web_search）"


def _minify_read_workspace(content: Any) -> Any:
    """tier3极简化：进一步截断到比tier2更短，不重复"如需重新调用"这类提示语（已经在tier2
    出现过，tier3只需要留一个更短的锚点，不需要重复完整提示）。"""
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    if not isinstance(content, str):
        return content
    if content.startswith(_READ_WORKSPACE_MINIFIED_PREFIX):
        return content
    truncated = content[:_READ_WORKSPACE_MINIFY_LEN]
    return f"（更早读取过的工作区文件，节选：{truncated}...）"


_MINIFIERS: dict[str, Callable[[Any], Any]] = {
    "retrieve_materials": _minify_retrieve_materials,
    "retrieve_project_evidence": _minify_project_evidence,
    "web_search": _minify_web_search,
    "read_workspace": _minify_read_workspace,
}


def _minify_tool_result(tool_name: str | None, content: Any) -> Any:
    """tier3极简化的分发函数，跟_summarize_tool_result同款长度门槛保护。"""
    text = "".join(str(c) for c in content) if isinstance(content, list) else content
    if not isinstance(text, str) or len(text) <= _MIN_COMPRESS_LEN:
        return content
    minifier = _MINIFIERS.get(tool_name or "")
    if minifier is None:
        return content
    return minifier(content)


def _summarize_tool_result(tool_name: str | None, content: Any) -> Any:
    """按tool_name分发到对应摘要函数。未知工具名、或内容长度不超过_MIN_COMPRESS_LEN，
    原样返回不处理——短内容压缩没有收益，且天然保护"没有检索到相关素材"这类关键词字符串。"""
    text = "".join(str(c) for c in content) if isinstance(content, list) else content
    if not isinstance(text, str) or len(text) <= _MIN_COMPRESS_LEN:
        return content
    summarizer = _SUMMARIZERS.get(tool_name or "")
    if summarizer is None:
        return content
    return summarizer(content)


def _tool_call_names_by_id(messages: list) -> dict[str, str]:
    """扫描历史里所有AIMessage的tool_calls，建立tool_call_id -> 工具名的映射。

    ToolMessage本身不一定可靠地带工具名（不同react-agent实现/测试fake不一定填这个字段），
    但tool_call_id关联回发起调用的那条AIMessage.tool_calls是LangChain消息协议里唯一
    保证存在的关联方式，比依赖ToolMessage.name更稳。"""
    mapping: dict[str, str] = {}
    for m in messages:
        tool_calls = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
        for call in (tool_calls or []):
            if not isinstance(call, dict):
                continue
            call_id, name = call.get("id"), call.get("name")
            if call_id and name:
                mapping[call_id] = name
    return mapping


def _convergence_nudge_metadata(
    message: Any,
    work_unit: str,
) -> tuple[int, int | None] | None:
    content = (
        message.get("content", "")
        if isinstance(message, dict)
        else getattr(message, "content", "")
    )
    if not isinstance(content, str):
        return None
    marker = f"{_CONVERGENCE_NUDGE_MARKER}{work_unit}:level="
    if marker not in content:
        return None
    match = re.search(
        re.escape(marker) + r"(?P<level>\d+)(?::count=(?P<count>\d+))?\]\]",
        content,
    )
    if not match:
        return None
    count = int(match.group("count")) if match.group("count") else None
    if count is None:
        # Backward compatibility for checkpoints written before the count was
        # embedded in the marker. The human-readable reminder already carried
        # the exact count, so reuse it instead of resetting after compaction.
        legacy_count = re.search(r"已经进行了(?P<count>\d+)次检索或地图查询", content)
        if legacy_count:
            count = int(legacy_count.group("count"))
    return int(match.group("level")), count


def _convergence_nudge_level(message: Any, work_unit: str) -> int | None:
    metadata = _convergence_nudge_metadata(message, work_unit)
    return metadata[0] if metadata is not None else None


def _build_convergence_nudge(
    policy: ConvergenceNudgePolicy,
    *,
    level: int,
    research_count: int,
) -> HumanMessage:
    if level == 1:
        follow_up = (
            "请结合当前任务判断下一步：如果仍缺少影响结论的关键证据，可以继续检索，但应聚焦"
            "明确的未解决问题；如果现有证据已经能够支持收窄、改写或降级，可以考虑先形成阶段"
            "结果；如果事实暂时无法可靠核实，可以保留不确定性，不必为了得到确定答案持续搜索。"
        )
    else:
        follow_up = (
            "请再次判断新增搜索是否仍在解决明确的未完成问题，还是已经进入重复查找或边际收益"
            "较低的状态。如果选择继续，请聚焦尚缺少的具体证据；如果当前结论已经可以通过收窄、"
            "标注推断或保留未核实状态表达，可以考虑形成阶段结果。"
        )
    write_tools = " / ".join(policy.expected_write_tools)
    return HumanMessage(
        content=(
            f"{_CONVERGENCE_NUDGE_MARKER}{policy.work_unit}:level={level}:count={research_count}]]\n"
            "【阶段性行动提醒】\n"
            f"你在当前工作单元中已经进行了{research_count}次检索或地图查询，但尚未产生本阶段"
            f"预期的{policy.delivery_description}（对应工具：{write_tools}）。\n"
            "这条提醒不是要求你立即写入，也不代表现有证据一定充分。它的目的是避免研究范围在"
            "不知不觉中持续扩大，因为继续检索会增加上下文和成本，也可能引入与当前问题无关的"
            "信息。\n"
            f"{follow_up}\n"
            "决策权仍由你根据当前证据和交付要求判断。"
        )
    )


def _maybe_add_convergence_nudge(
    messages: list,
    policy: ConvergenceNudgePolicy | None,
    remembered_levels: dict[str, int] | None = None,
) -> tuple[list, dict[str, Any] | None]:
    if policy is None:
        return list(messages), None

    tool_names = _tool_call_names_by_id(messages)
    completed_tools: list[tuple[int, str, Any]] = []
    for message_index, message in enumerate(messages):
        if not _is_tool_message(message):
            continue
        call_id = (
            message.get("tool_call_id")
            if isinstance(message, dict)
            else getattr(message, "tool_call_id", None)
        )
        name = tool_names.get(str(call_id)) if call_id else None
        if name:
            completed_tools.append((message_index, name, message))

    write_results = [
        (message_index, name, message)
        for message_index, name, message in completed_tools
        if name in policy.expected_write_tools
    ]
    if write_results and remembered_levels is not None:
        remembered_levels.pop(policy.work_unit, None)
    last_write_message_index = max((item[0] for item in write_results), default=-1)
    visible_research_count = sum(
        name in policy.research_tools
        for message_index, name, _message in completed_tools
        if message_index > last_write_message_index
    )
    existing_nudges = [
        (message_index, metadata[0], metadata[1])
        for message_index, message in enumerate(messages)
        if message_index > last_write_message_index
        if (metadata := _convergence_nudge_metadata(message, policy.work_unit)) is not None
    ]
    research_count = visible_research_count
    if existing_nudges:
        latest_index, _latest_level, previous_count = existing_nudges[-1]
        if previous_count is not None:
            research_after_latest_nudge = sum(
                name in policy.research_tools
                for message_index, name, _message in completed_tools
                if message_index > latest_index
            )
            research_count = max(
                visible_research_count,
                previous_count + research_after_latest_nudge,
            )
    existing_levels = {level for _index, level, _count in existing_nudges}
    highest_existing_level = max(
        max(existing_levels, default=0),
        (remembered_levels or {}).get(policy.work_unit, 0),
    )
    if research_count >= policy.second_threshold and highest_existing_level < 2:
        level = 2
    elif research_count >= policy.first_threshold and highest_existing_level < 1:
        level = 1
    else:
        return list(messages), None

    edited = [
        *messages,
        _build_convergence_nudge(policy, level=level, research_count=research_count),
    ]
    if remembered_levels is not None:
        remembered_levels[policy.work_unit] = level
    return edited, {
        "status": "convergence_nudge",
        "work_unit": policy.work_unit,
        "level": level,
        "research_calls_since_write": research_count,
        "expected_write_tools": list(policy.expected_write_tools),
    }


class ContextBudgetExceededError(RuntimeError):
    """The request cannot be reduced below the configured context budget."""


def _message_content_len(m: Any) -> int:
    content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
    if isinstance(content, list):
        content = "".join(str(c) for c in content)
    return len(content) if isinstance(content, str) else 0


def _message_payload_len(m: Any) -> int:
    """Approximate the serialized payload, including tool-call arguments."""
    size = _message_content_len(m)
    tool_calls = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
    if tool_calls:
        size += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    tool_call_id = (
        m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
    )
    if tool_call_id:
        size += len(str(tool_call_id))
    return size


def _estimate_tokens(messages: list, *, tools: tuple[Any, ...] = ()) -> int:
    try:
        return count_tokens_approximately(
            messages,
            tools=list(tools),
            chars_per_token=_TOKEN_ESTIMATE_CHARS_PER_TOKEN,
        )
    except (TypeError, ValueError, NotImplementedError):
        # Unit-test fakes and provider-specific message objects may not be directly
        # convertible by LangChain. Keep the same conservative accounting fallback.
        return int(
            sum(_message_payload_len(message) for message in messages)
            / _TOKEN_ESTIMATE_CHARS_PER_TOKEN
        ) + len(messages) * 3


def _is_tool_message(m: Any) -> bool:
    mtype = (m.get("role", "") or m.get("type", "")) if isinstance(m, dict) else type(m).__name__
    return "Tool" in mtype or mtype == "tool"


def _set_content(m: Any, content: Any) -> Any:
    if isinstance(m, dict):
        return {**m, "content": content}
    new_m = copy.copy(m)
    new_m.content = content
    return new_m


def _set_tool_calls(m: Any, tool_calls: list[dict]) -> Any:
    if isinstance(m, dict):
        return {**m, "tool_calls": tool_calls}
    if hasattr(m, "model_copy"):
        return m.model_copy(update={"tool_calls": tool_calls})
    new_m = copy.copy(m)
    new_m.tool_calls = tool_calls
    return new_m


def _is_ai_message(m: Any) -> bool:
    mtype = (m.get("role", "") or m.get("type", "")) if isinstance(m, dict) else type(m).__name__
    return "AI" in mtype or mtype in {"ai", "assistant"}


def _is_human_message(m: Any) -> bool:
    mtype = (m.get("role", "") or m.get("type", "")) if isinstance(m, dict) else type(m).__name__
    return "Human" in mtype or mtype in {"human", "user"}


def _tool_call_ids(m: Any) -> list[str]:
    calls = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
    return [str(call.get("id")) for call in (calls or []) if isinstance(call, dict) and call.get("id")]


def _drop_stale_failed_write_pairs(
    messages: list,
    expected_write_tools: tuple[str, ...],
) -> list:
    """Drop old failed writes unless the newest allowed write itself failed."""
    tool_names = _tool_call_names_by_id(messages)
    failed_ids: list[str] = []
    latest_allowed_result_id: str | None = None
    latest_allowed_result_failed = False
    for message in messages:
        if not _is_tool_message(message):
            continue
        content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
        text = "".join(str(item) for item in content) if isinstance(content, list) else str(content or "")
        call_id = message.get("tool_call_id") if isinstance(message, dict) else getattr(message, "tool_call_id", None)
        if not call_id or tool_names.get(str(call_id)) not in expected_write_tools:
            continue
        latest_allowed_result_id = str(call_id)
        latest_allowed_result_failed = text.startswith(
            ("写入失败", "提案写入失败", "工具参数校验失败")
        )
        if latest_allowed_result_failed:
            failed_ids.append(str(call_id))

    preserve_id = latest_allowed_result_id if latest_allowed_result_failed else None
    stale_ids = {call_id for call_id in failed_ids if call_id != preserve_id}
    if not stale_ids:
        return list(messages)

    edited = []
    for message in messages:
        if _is_tool_message(message):
            call_id = message.get("tool_call_id") if isinstance(message, dict) else getattr(message, "tool_call_id", None)
            if call_id and str(call_id) in stale_ids:
                continue
        if _is_ai_message(message):
            calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
            if calls:
                kept_calls = [
                    call
                    for call in calls
                    if not (isinstance(call, dict) and str(call.get("id")) in stale_ids)
                ]
                if not kept_calls and _message_content_len(message) == 0:
                    continue
                if len(kept_calls) != len(calls):
                    message = _set_tool_calls(message, kept_calls)
        edited.append(message)
    return edited


def _over_budget(
    messages: list,
    *,
    static_messages: tuple[Any, ...],
    tools: tuple[Any, ...],
    safety_ceiling_chars: int,
    safety_ceiling_tokens: int,
    max_messages: int,
) -> bool:
    all_messages = [*static_messages, *messages]
    return (
        sum(_message_payload_len(message) for message in all_messages) > safety_ceiling_chars
        or _estimate_tokens(all_messages, tools=tools) > safety_ceiling_tokens
        or len(all_messages) > max_messages
    )


def _prune_completed_history(
    messages: list,
    *,
    static_messages: tuple[Any, ...],
    tools: tuple[Any, ...],
    safety_ceiling_chars: int,
    safety_ceiling_tokens: int,
    max_messages: int,
    recent_messages: int,
    protected_tool_call_ids: frozenset[str] = frozenset(),
) -> list:
    """Drop old completed exchanges while preserving task, findings, and recency."""
    edited = list(messages)
    cutoff = max(1, len(edited) - recent_messages)

    while _over_budget(
        edited,
        static_messages=static_messages,
        tools=tools,
        safety_ceiling_chars=safety_ceiling_chars,
        safety_ceiling_tokens=safety_ceiling_tokens,
        max_messages=max_messages,
    ):
        tool_indices_by_id: dict[str, list[int]] = {}
        for index, message in enumerate(edited):
            if not _is_tool_message(message):
                continue
            call_id = (
                message.get("tool_call_id")
                if isinstance(message, dict)
                else getattr(message, "tool_call_id", None)
            )
            if call_id:
                tool_indices_by_id.setdefault(str(call_id), []).append(index)

        removed = False
        for index, message in enumerate(edited[:cutoff]):
            if not _is_ai_message(message):
                continue
            call_ids = _tool_call_ids(message)
            if not call_ids:
                continue
            if protected_tool_call_ids.intersection(call_ids):
                continue
            calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
            if any(call.get("name") == "record_finding" for call in (calls or []) if isinstance(call, dict)):
                continue
            paired_indices = [
                tool_index
                for call_id in call_ids
                for tool_index in tool_indices_by_id.get(call_id, [])
            ]
            if not paired_indices or max(paired_indices) >= cutoff:
                continue
            remove_indices = {index, *paired_indices}
            edited = [item for item_index, item in enumerate(edited) if item_index not in remove_indices]
            cutoff = max(1, len(edited) - recent_messages)
            removed = True
            break
        if removed:
            continue

        # Completed tool exchanges are gone. Remove stale narration/retry notes,
        # keeping the original task and the recent working window.
        removable = next(
            (
                index
                for index, message in enumerate(edited[:cutoff])
                if index > 0
                and _CONVERGENCE_NUDGE_MARKER
                not in str(
                    message.get("content", "")
                    if isinstance(message, dict)
                    else getattr(message, "content", "")
                )
                and ((_is_ai_message(message) and not _tool_call_ids(message)) or _is_human_message(message))
            ),
            None,
        )
        if removable is None:
            # The recent window is a preference, not permission to exceed the hard
            # budget. Gradually expose older recent pairs while always protecting
            # the final exchange that the model must react to next.
            if cutoff < max(1, len(edited) - 2):
                cutoff = min(len(edited) - 2, cutoff + 2)
                continue
            break
        edited.pop(removable)
        cutoff = max(1, len(edited) - recent_messages)

    return edited


def build_context_editing_hook(
    compressible_tools: tuple[str, ...] = _DEFAULT_COMPRESSIBLE_TOOLS,
    trigger_chars: int = _DEFAULT_TRIGGER_CHARS,
    safety_ceiling_chars: int = _DEFAULT_SAFETY_CEILING_CHARS,
    keep_budget_fraction: float = _DEFAULT_KEEP_BUDGET_FRACTION,
    summary_budget_fraction: float = _DEFAULT_SUMMARY_BUDGET_FRACTION,
    *,
    static_messages: tuple[Any, ...] = (),
    tools: tuple[Any, ...] = (),
    trigger_tokens: int = _DEFAULT_TRIGGER_TOKENS,
    safety_ceiling_tokens: int = _DEFAULT_SAFETY_CEILING_TOKENS,
    max_messages: int = _DEFAULT_MAX_MESSAGES,
    recent_messages: int = _DEFAULT_RECENT_MESSAGES,
    convergence_policy: ConvergenceNudgePolicy | None = None,
) -> Callable[[dict], dict]:
    """构造一个create_react_agent的pre_model_hook：ReAct循环内部每次调模型之前执行。

    低于字符、近似token和消息条数三个触发阈值：原样返回。
    任一阈值触发后：把属于compressible_tools的ToolMessage按时间顺序分成三层（从最近
    往前累加字符数）：
    - tier1（最近、原文）：累加原文字符数直到即将超过keep_budget_chars。
    - tier2（次新、富摘要）：紧接着tier1往前，累加**摘要后**的字符数直到即将超过
      summary_budget_chars，摘要内容用_summarize_tool_result生成（chunk_id/URL+关键片段）。
    - tier3（更早、极简）：tier2边界之前的全部条目，不管数量多少，都用_minify_tool_result
      压成只留chunk_id/URL列表的极简形式——这一层存在的原因是tier2虽然把"单条"体积压下来了，
      但调用次数一多（真实场景一次跑72次retrieve_materials/web_search），"tier2摘要"这个桶
      本身还是会随调用次数线性增长、重新逼近硬阈值，必须再加一层不随调用次数强增长的极简形式
      做第一步体积控制（2026-08-11真实E2E复测发现，见docs/ISSUES.md）。

    keep_budget_chars/summary_budget_chars不是固定常量，是**按safety_ceiling_chars减去
    "固定开销"（system+human+ai+不可压缩工具，比如lifestyle_agent的任务prompt里直接内嵌了
    完整route_plan.json，8天行程本身就有一两万字符）之后剩下的空间**动态算出来的——同一次
    2026-08-11真实E2E复测发现，固定预算数字（keep=12000+summary=6000）没考虑到不同agent/
    不同任务的固定开销差异极大（这次lifestyle_agent的固定开销就有23420字符），两个固定预算
    加上固定开销直接破了4万字符线。固定开销越大，留给retrieve_materials/web_search这些
    可压缩内容的空间就越小，是自适应而不是拍脑袋定死的两个数字。

    三层压缩之后，若完整请求仍超过字符/token/消息数硬预算，则清理已经完成的旧AI+Tool
    交互对和旧重试说明；原始任务、record_finding和最近交互优先保留。失败的write_*
    调用只删旧轮，最新完整参数+精简错误作为当前修复底稿保留。仍无法达标时抛出
    ContextBudgetExceededError并记录context_events.jsonl，不自动换窗继续消耗。

    整份messages用RemoveMessage(REMOVE_ALL_MESSAGES)覆盖式更新，让裁剪后的历史直接成为
    react-agent后续循环和checkpoint里的当前状态，不另外保留一份未压缩副本；压缩效果也会
    延续到_invoke_with_write_verification的外层重试，减少双重累积。
    """

    # LangGraph may rebuild the outgoing message update from checkpoint state in
    # a way that temporarily omits the previously injected HumanMessage. Keep a
    # small hook-local high-water mark so the same work unit cannot receive the
    # same reminder repeatedly within one agent instance.
    remembered_nudge_levels: dict[str, int] = {}
    bound_write_tools = tuple(
        dict.fromkeys(
            name
            for tool in tools
            if (name := getattr(tool, "name", None)) and name.startswith("write_")
        )
    )
    expected_write_tools = bound_write_tools
    if convergence_policy is not None:
        expected_write_tools = tuple(
            name
            for name in convergence_policy.expected_write_tools
            if name in bound_write_tools
        )

    def hook(state: dict) -> dict:
        messages, nudge_event = _maybe_add_convergence_nudge(
            state["messages"],
            convergence_policy,
            remembered_nudge_levels,
        )
        if nudge_event is not None:
            append_context_event(nudge_event)
        all_messages = [*static_messages, *messages]
        total_chars = sum(_message_payload_len(m) for m in all_messages)
        total_tokens = _estimate_tokens(all_messages, tools=tools)
        original_message_count = len(all_messages)
        if (
            total_chars <= trigger_chars
            and total_tokens <= trigger_tokens
            and len(all_messages) <= max_messages
        ):
            if nudge_event is None:
                return {"messages": messages}
            return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]}

        tool_names = _tool_call_names_by_id(messages)
        compressible_indices = [
            i
            for i, m in enumerate(messages)
            if _is_tool_message(m)
            and tool_names.get(
                m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
            )
            in compressible_tools
        ]

        # "Latest Project Evidence" means the latest result from that tool, not
        # simply the final compressible ToolMessage. Web/material follow-ups may
        # occur after evidence retrieval, but they must not evict the evidence
        # raw text while the complete hard budget can still accommodate it.
        latest_project_evidence_index = next(
            (
                index
                for index in reversed(compressible_indices)
                if tool_names.get(
                    messages[index].get("tool_call_id")
                    if isinstance(messages[index], dict)
                    else getattr(messages[index], "tool_call_id", None)
                )
                == "retrieve_project_evidence"
            ),
            None,
        )
        tier_priority_indices = list(compressible_indices)
        if latest_project_evidence_index is not None:
            tier_priority_indices.remove(latest_project_evidence_index)
            tier_priority_indices.append(latest_project_evidence_index)

        compressible_chars = sum(_message_content_len(messages[i]) for i in compressible_indices)
        fixed_chars = total_chars - compressible_chars
        available_chars = max(0, safety_ceiling_chars - fixed_chars)
        keep_budget_chars = available_chars * keep_budget_fraction
        configured_summary_budget_chars = available_chars * summary_budget_fraction

        # tier1：从最近往前累加原文字符数，直到即将超过keep_budget_chars。
        kept_budget_used = 0
        keep_from_pos = len(tier_priority_indices)  # 默认：一条都不留（compressible为空时）
        for pos in range(len(tier_priority_indices) - 1, -1, -1):
            idx = tier_priority_indices[pos]
            size = _message_content_len(messages[idx])
            if kept_budget_used + size > keep_budget_chars:
                name = tool_names.get(
                    messages[idx].get("tool_call_id")
                    if isinstance(messages[idx], dict)
                    else getattr(messages[idx], "tool_call_id", None)
                )
                # V7 Project Evidence can exceed its fractional tier1 share even
                # when the latest result still fits the complete hard budget.
                # Preserve that newest evidence; older results use tier2/tier3.
                if (
                    kept_budget_used == 0
                    and name == "retrieve_project_evidence"
                    and size <= available_chars
                ):
                    kept_budget_used = size
                    keep_from_pos = pos
                break
            kept_budget_used += size
            keep_from_pos = pos

        summary_budget_chars = min(
            configured_summary_budget_chars,
            max(0, available_chars - kept_budget_used),
        )

        # tier2：紧接着tier1往前，累加**摘要后**的字符数直到即将超过summary_budget_chars。
        summary_budget_used = 0
        summary_from_pos = keep_from_pos
        tier2_summaries: dict[int, Any] = {}
        for pos in range(keep_from_pos - 1, -1, -1):
            idx = tier_priority_indices[pos]
            m = messages[idx]
            name = tool_names.get(
                m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
            )
            content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
            summarized = _summarize_tool_result(name, content)
            size = len(summarized) if isinstance(summarized, str) else _message_content_len(m)
            if summary_budget_used + size > summary_budget_chars:
                break
            summary_budget_used += size
            summary_from_pos = pos
            tier2_summaries[idx] = summarized

        tier2_indices = set(tier_priority_indices[summary_from_pos:keep_from_pos])
        tier3_indices = set(tier_priority_indices[:summary_from_pos])
        tier1_indices = set(tier_priority_indices[keep_from_pos:])
        protected_project_evidence_call_ids = frozenset(
            str(call_id)
            for index in tier1_indices
            if index == latest_project_evidence_index
            if (
                call_id := (
                    messages[index].get("tool_call_id")
                    if isinstance(messages[index], dict)
                    else getattr(messages[index], "tool_call_id", None)
                )
            )
        )

        edited = []
        for i, m in enumerate(messages):
            if i in tier2_indices:
                summarized = tier2_summaries[i]
                content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
                edited.append(m if summarized == content else _set_content(m, summarized))
            elif i in tier3_indices:
                name = tool_names.get(
                    m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
                )
                content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
                minified = _minify_tool_result(name, content)
                edited.append(m if minified == content else _set_content(m, minified))
            else:
                edited.append(m)

        # Older rejected drafts are historical noise. The newest rejected
        # write is active repair state: keep its complete arguments together
        # with the compact validation error so the next turn can patch it
        # instead of regenerating the whole structured object from scratch.
        edited = _drop_stale_failed_write_pairs(edited, expected_write_tools)
        edited = _prune_completed_history(
            edited,
            static_messages=static_messages,
            tools=tools,
            safety_ceiling_chars=safety_ceiling_chars,
            safety_ceiling_tokens=safety_ceiling_tokens,
            max_messages=max_messages,
            recent_messages=recent_messages,
            protected_tool_call_ids=protected_project_evidence_call_ids,
        )
        if protected_project_evidence_call_ids and _over_budget(
            edited,
            static_messages=static_messages,
            tools=tools,
            safety_ceiling_chars=safety_ceiling_chars,
            safety_ceiling_tokens=safety_ceiling_tokens,
            max_messages=max_messages,
        ):
            # The raw evidence was preferred over the fractional tier1 budget,
            # but the complete request still cannot fit after normal pruning.
            # Fall back to the existing bounded summary instead of failing an
            # otherwise recoverable turn solely to preserve the raw payload.
            compacted = []
            for message in edited:
                call_id = (
                    message.get("tool_call_id")
                    if isinstance(message, dict)
                    else getattr(message, "tool_call_id", None)
                )
                if _is_tool_message(message) and str(call_id) in protected_project_evidence_call_ids:
                    content = (
                        message.get("content", "")
                        if isinstance(message, dict)
                        else getattr(message, "content", "")
                    )
                    summarized = _summarize_tool_result("retrieve_project_evidence", content)
                    compacted.append(
                        message if summarized == content else _set_content(message, summarized)
                    )
                else:
                    compacted.append(message)
            edited = _prune_completed_history(
                compacted,
                static_messages=static_messages,
                tools=tools,
                safety_ceiling_chars=safety_ceiling_chars,
                safety_ceiling_tokens=safety_ceiling_tokens,
                max_messages=max_messages,
                recent_messages=recent_messages,
            )
        if _over_budget(
            edited,
            static_messages=static_messages,
            tools=tools,
            safety_ceiling_chars=safety_ceiling_chars,
            safety_ceiling_tokens=safety_ceiling_tokens,
            max_messages=max_messages,
        ):
            append_context_event(
                {
                    "status": "budget_exceeded",
                    "before_tokens": total_tokens,
                    "before_messages": original_message_count,
                    "after_tokens": _estimate_tokens([*static_messages, *edited], tools=tools),
                    "after_messages": len(static_messages) + len(edited),
                }
            )
            raise ContextBudgetExceededError(
                "上下文在压缩和历史清理后仍超过安全预算；已停止当前Agent，未自动续窗"
            )

        append_context_event(
            {
                "status": "compacted",
                "before_tokens": total_tokens,
                "before_messages": original_message_count,
                "after_tokens": _estimate_tokens([*static_messages, *edited], tools=tools),
                "after_messages": len(static_messages) + len(edited),
            }
        )

        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *edited]}

    return hook
