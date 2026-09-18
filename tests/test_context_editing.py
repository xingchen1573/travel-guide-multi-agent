"""src/context_editing.py的直接mock单元测试，不调真实LLM。

覆盖：按工具类型分发的摘要函数（_summarize_tool_result）+ pre_model_hook工厂函数
（build_context_editing_hook）。跟tests/test_orchestrator_core_nodes.py里
_compress_retrieval_history的测试同款风格：duck-typed fake类（.content/.tool_calls/
.tool_call_id），不依赖真实langchain消息类。
"""
import json
import pytest

from langchain_core.messages import HumanMessage, SystemMessage

from src.context_editing import (
    ConvergenceNudgePolicy,
    ContextBudgetExceededError,
    _estimate_tokens,
    _message_payload_len,
    _minify_tool_result,
    _summarize_retrieve_materials,
    _summarize_tool_result,
    _summarize_web_search,
    build_context_editing_hook,
)


class _FakeAIWithToolCall:
    def __init__(self, tool_calls):
        self.content = ""
        self.tool_calls = tool_calls


class _FakeToolMessage:
    def __init__(self, content, tool_call_id):
        self.content = content
        self.tool_call_id = tool_call_id


class _FakeBoundTool:
    def __init__(self, name):
        self.name = name


def _rm_result(chunk_id="c001", snippet_repeat=30):
    return json.dumps(
        [
            {
                "chunk_id": chunk_id,
                "platform": "bilibili",
                "author": "A",
                "content_snippet": "武侯祠是三国蜀汉丞相诸葛亮的祠堂" * snippet_repeat,
            }
        ],
        ensure_ascii=False,
    )


def _project_evidence_result(
    chunk_prefix="pe",
    *,
    snippet_chars=1200,
    count=1,
):
    snippet_seed = "青甘路线时效证据"
    snippet = (snippet_seed * (snippet_chars // len(snippet_seed) + 1))[:snippet_chars]
    return json.dumps(
        [
            {
                "content_snippet": f"{index}:{snippet}",
                "summary": "已保存的联网证据摘要",
                "platform": "project_evidence",
                "source_platform": "example.org",
                "author": "",
                "doc_id": f"{chunk_prefix}-{index}",
                "chunk_id": f"project-evidence-{chunk_prefix}-{index}",
                "entities": [],
                "title": f"{chunk_prefix} 官方公告 {index}",
                "score": {"project_keyword": 3.0},
                "scope": "project",
                "note_type": "evidence",
                "source_url": f"https://example.org/{chunk_prefix}/{index}",
                "log_scope": "lifestyle_agent",
            }
            for index in range(count)
        ],
        ensure_ascii=False,
    )


def _msg_len(m):
    content = m.get("content", "") if isinstance(m, dict) else m.content
    return len(content) if isinstance(content, str) else 0


def _append_tool_exchange(messages, tool_name, index, content="查询结果"):
    tool_call_id = f"{tool_name}-{index}"
    messages.append(
        _FakeAIWithToolCall(
            [{"name": tool_name, "id": tool_call_id, "args": {"query": str(index)}}]
        )
    )
    messages.append(_FakeToolMessage(content, tool_call_id=tool_call_id))


def _nudge_policy():
    return ConvergenceNudgePolicy(
        work_unit="route_facts_day_1",
        research_tools=("retrieve_materials", "web_search", "amap_query"),
        expected_write_tools=("write_route_facts_patch",),
        delivery_description="路线事实修复结果",
    )


# ---------------------------------------------------------------------------
# _summarize_tool_result / 按工具类型分发
# ---------------------------------------------------------------------------


def test_summarize_tool_result_compresses_retrieve_materials_when_long():
    raw = _rm_result()
    summarized = _summarize_tool_result("retrieve_materials", raw)
    assert "c001" in summarized
    assert len(summarized) < len(raw)


def test_summarize_project_evidence_keeps_traceability_and_bounded_excerpt():
    raw = _project_evidence_result(count=2)

    summarized = _summarize_tool_result("retrieve_project_evidence", raw)
    payload = json.loads(summarized.split("\n", 1)[1])

    assert len(summarized) < len(raw)
    assert [item["chunk_id"] for item in payload] == [
        "project-evidence-pe-0",
        "project-evidence-pe-1",
    ]
    assert payload[0]["title"] == "pe 官方公告 0"
    assert payload[0]["source_platform"] == "example.org"
    assert payload[0]["source_url"] == "https://example.org/pe/0"
    assert len(payload[0]["excerpt"]) == 240


def test_minify_project_evidence_accepts_raw_and_tier2_content():
    raw = _project_evidence_result(count=2)
    summarized = _summarize_tool_result("retrieve_project_evidence", raw)

    for source in (raw, summarized):
        minified = _minify_tool_result("retrieve_project_evidence", source)
        assert "project-evidence-pe-0" in minified
        assert "pe 官方公告 0" in minified
        assert "青甘路线时效证据" not in minified
        assert len(minified) < len(summarized)


def test_summarize_tool_result_compresses_web_search_when_long():
    raw = (
        "- 武侯祠简介：" + "三国蜀汉丞相诸葛亮的祠堂，历史悠久，是重要的旅游景点" * 20
        + "（来源：http://x.com/a）\n"
        "- 太古里简介：" + "成都著名商圈，聚集了众多品牌门店和网红打卡点" * 20
        + "（来源：http://x.com/b）"
    )
    summarized = _summarize_tool_result("web_search", raw)
    assert "http://x.com/a" in summarized
    assert "http://x.com/b" in summarized
    assert len(summarized) < len(raw)


def test_summarize_tool_result_truncates_read_workspace_when_long():
    raw = json.dumps({"days": [{"day": i, "city": "成都", "pois": []} for i in range(30)]}, ensure_ascii=False)
    summarized = _summarize_tool_result("read_workspace", raw)
    assert len(summarized) < len(raw)
    assert "不要重复读取同一整份文件" in summarized


def test_summarize_read_workspace_is_idempotent():
    raw = json.dumps(
        {"days": [{"day": i, "city": "成都", "pois": []} for i in range(30)]},
        ensure_ascii=False,
    )
    summarized = _summarize_tool_result("read_workspace", raw)

    assert _summarize_tool_result("read_workspace", summarized) == summarized
    assert summarized.count("已读取工作区文件") == 1


def test_summarize_tool_result_leaves_unknown_tool_untouched():
    raw = "x" * 500
    assert _summarize_tool_result("amap_driving_distance", raw) == raw


def test_summarize_tool_result_leaves_short_content_untouched_regardless_of_tool():
    # 覆盖_count_trailing_empty_retrievals依赖的关键词字符串——不管长度门槛以内的内容
    # 来自哪个工具，都不应该被压缩。
    for tool_name in ("retrieve_materials", "read_workspace", "web_search", None):
        assert _summarize_tool_result(tool_name, "没有检索到相关素材。") == "没有检索到相关素材。"


def test_summarize_retrieve_materials_leaves_non_json_content_untouched():
    assert _summarize_retrieve_materials("没有检索到相关素材。") == "没有检索到相关素材。"


def test_summarize_web_search_leaves_no_result_message_untouched():
    msg = "联网搜索没有找到相关结果，请基于你自己的知识回答，并如实告知用户这类信息可能不是最新的，建议现场核实。"
    assert _summarize_web_search(msg) == msg


def test_summarize_read_workspace_leaves_file_not_found_untouched_via_dispatcher():
    # _summarize_read_workspace本身没有长度门槛（无条件截断+提示），保护短内容的门槛在
    # _summarize_tool_result这一层，跟其他工具一致——生产代码永远通过dispatcher调用。
    msg = "文件不存在: outputs/lifestyle.json"
    assert _summarize_tool_result("read_workspace", msg) == msg


# ---------------------------------------------------------------------------
# build_context_editing_hook
# ---------------------------------------------------------------------------


def test_hook_leaves_messages_untouched_below_trigger():
    hook = build_context_editing_hook(trigger_chars=1000)
    messages = [{"role": "system", "content": "short prompt"}, {"role": "user", "content": "hi"}]
    result = hook({"messages": messages})
    assert result == {"messages": messages}


def test_hook_convergence_nudge_is_soft_and_explains_why():
    events = []
    messages = [{"role": "user", "content": "修复Day1"}]
    for index in range(6):
        _append_tool_exchange(messages, "web_search", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=_nudge_policy(),
    )

    from src import context_editing

    original_append = context_editing.append_context_event
    context_editing.append_context_event = events.append
    try:
        edited = hook({"messages": messages})["messages"][1:]
    finally:
        context_editing.append_context_event = original_append

    reminder = edited[-1].content
    assert "不是要求你立即写入" in reminder
    assert "不代表现有证据一定充分" in reminder
    assert "上下文和成本" in reminder
    assert "决策权仍由你" in reminder
    assert events == [
        {
            "status": "convergence_nudge",
            "work_unit": "route_facts_day_1",
            "level": 1,
            "research_calls_since_write": 6,
            "expected_write_tools": ["write_route_facts_patch"],
        }
    ]


def test_hook_convergence_nudge_does_not_trigger_before_threshold():
    messages = [{"role": "user", "content": "修复Day1"}]
    for index in range(5):
        _append_tool_exchange(messages, "web_search", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=_nudge_policy(),
    )

    assert hook({"messages": messages}) == {"messages": messages}


def test_hook_convergence_nudge_escalates_once_without_repeating():
    messages = [{"role": "user", "content": "修复Day1"}]
    for index in range(6):
        _append_tool_exchange(messages, "web_search", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=_nudge_policy(),
    )
    after_first = hook({"messages": messages})["messages"][1:]
    for index in range(6, 10):
        _append_tool_exchange(after_first, "amap_query", index)

    after_second = hook({"messages": after_first})["messages"][1:]
    reminder_texts = [
        message.content
        for message in after_second
        if "[[convergence_nudge:" in getattr(message, "content", "")
    ]
    assert len(reminder_texts) == 2
    assert "level=1" in reminder_texts[0]
    assert "level=2" in reminder_texts[1]

    repeated = hook({"messages": after_second})
    assert repeated == {"messages": after_second}


def test_hook_convergence_nudge_count_survives_compacted_research_history():
    messages = [{"role": "user", "content": "检查全部推荐"}]
    for index in range(24):
        _append_tool_exchange(messages, "retrieve_materials", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=ConvergenceNudgePolicy(
            work_unit="pitfall_full_batch",
            research_tools=("retrieve_materials",),
            expected_write_tools=("write_agent_output",),
            delivery_description="逐项避雷结果",
            first_threshold=24,
            second_threshold=40,
        ),
    )

    after_first = hook({"messages": messages})["messages"][1:]
    first_reminder = after_first[-1]
    assert "level=1:count=24" in first_reminder.content

    # Simulate context pruning: all research calls covered by the first marker
    # have disappeared, while the marker and 16 newer calls remain.
    compacted = [messages[0], first_reminder]
    for index in range(24, 40):
        _append_tool_exchange(compacted, "retrieve_materials", index)

    after_second = hook({"messages": compacted})["messages"][1:]
    second_reminder = after_second[-1]
    assert "level=2:count=40" in second_reminder.content


def test_hook_convergence_nudge_reads_legacy_count_from_checkpoint_text():
    legacy_reminder = HumanMessage(
        content=(
            "[[convergence_nudge:pitfall_full_batch:level=1]]\n"
            "你在当前工作单元中已经进行了24次检索或地图查询。"
        )
    )
    messages = [{"role": "user", "content": "检查全部推荐"}, legacy_reminder]
    for index in range(16):
        _append_tool_exchange(messages, "retrieve_materials", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=ConvergenceNudgePolicy(
            work_unit="pitfall_full_batch",
            research_tools=("retrieve_materials",),
            expected_write_tools=("write_agent_output",),
            delivery_description="逐项避雷结果",
            first_threshold=24,
            second_threshold=40,
        ),
    )

    edited = hook({"messages": messages})["messages"][1:]
    assert "level=2:count=40" in edited[-1].content


def test_hook_convergence_nudge_does_not_repeat_when_prior_marker_is_omitted():
    messages = [{"role": "user", "content": "修复Day1"}]
    for index in range(6):
        _append_tool_exchange(messages, "web_search", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=_nudge_policy(),
    )

    after_first = hook({"messages": messages})["messages"][1:]
    without_marker = [
        message
        for message in after_first
        if "[[convergence_nudge:" not in getattr(message, "content", "")
    ]
    _append_tool_exchange(without_marker, "web_search", 6)

    assert hook({"messages": without_marker}) == {"messages": without_marker}

    for index in range(7, 10):
        _append_tool_exchange(without_marker, "web_search", index)
    after_second = hook({"messages": without_marker})["messages"][1:]
    reminders = [
        message.content
        for message in after_second
        if "[[convergence_nudge:" in getattr(message, "content", "")
    ]
    assert len(reminders) == 1
    assert "level=2" in reminders[0]


def test_hook_convergence_nudge_counts_research_after_latest_write():
    messages = [{"role": "user", "content": "修复Day1"}]
    for index in range(10):
        _append_tool_exchange(messages, "web_search", index)
    _append_tool_exchange(messages, "write_route_facts_patch", 10, content="写入成功")
    for index in range(11, 16):
        _append_tool_exchange(messages, "web_search", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=_nudge_policy(),
    )

    assert hook({"messages": messages}) == {"messages": messages}


def test_hook_successful_write_resets_remembered_nudge_level():
    messages = [{"role": "user", "content": "修复Day1"}]
    for index in range(6):
        _append_tool_exchange(messages, "web_search", index)
    hook = build_context_editing_hook(
        trigger_chars=100000,
        trigger_tokens=100000,
        convergence_policy=_nudge_policy(),
    )
    after_first = hook({"messages": messages})["messages"][1:]
    _append_tool_exchange(after_first, "write_route_facts_patch", 6, content="写入成功")
    for index in range(7, 13):
        _append_tool_exchange(after_first, "web_search", index)

    after_write = hook({"messages": after_first})["messages"][1:]
    reminders = [
        message.content
        for message in after_write
        if "[[convergence_nudge:" in getattr(message, "content", "")
    ]
    assert len(reminders) == 2
    assert "level=1" in reminders[-1]


def test_hook_three_tier_compression():
    """2026-08-11修复：单条raw=564字符/单条tier2摘要=107字符/单条tier3极简=32字符
    （用真实_rm_result()实测得出，不是拍脑袋数字）。固定开销（system消息）=100字符，
    safety_ceiling=1100 → available=1000，keep_fraction=0.6→keep_budget=600（刚好装得下
    1条raw，装不下2条），summary_fraction=0.25→summary_budget=250（刚好装得下2条摘要，
    装不下3条）。5条工具结果，预期：最近1条留原文，接着2条压成富摘要，最早2条压成极简。"""
    hook = build_context_editing_hook(
        trigger_chars=500, safety_ceiling_chars=1400, keep_budget_fraction=0.6, summary_budget_fraction=0.25
    )
    messages = [{"role": "system", "content": "s" * 100}]
    for i in range(5):
        tid = f"call-{i}"
        messages.append(_FakeAIWithToolCall([{"name": "retrieve_materials", "id": tid}]))
        messages.append(_FakeToolMessage(_rm_result(chunk_id=f"c{i:03d}"), tool_call_id=tid))

    result = hook({"messages": messages})
    edited = result["messages"]
    assert edited[0].id == "__remove_all__"  # RemoveMessage标记
    edited = edited[1:]

    tool_msgs = [m for m in edited if isinstance(m, _FakeToolMessage)]
    assert len(tool_msgs) == 5
    # tier1：最近1条（c004）原样保留
    assert tool_msgs[4].content == _rm_result(chunk_id="c004")
    # tier2：接下来2条（c003/c002）压成富摘要，chunk_id仍可见
    assert "c003" in tool_msgs[3].content and len(tool_msgs[3].content) < len(_rm_result(chunk_id="c003"))
    assert "c002" in tool_msgs[2].content and len(tool_msgs[2].content) < len(_rm_result(chunk_id="c002"))
    # tier3：最早2条（c001/c000）压成极简形式，比tier2的富摘要更短，但chunk_id仍可见
    assert "c001" in tool_msgs[1].content and len(tool_msgs[1].content) < len(tool_msgs[2].content)
    assert "c000" in tool_msgs[0].content and len(tool_msgs[0].content) < len(tool_msgs[2].content)


def test_hook_applies_three_tiers_to_project_evidence():
    hook = build_context_editing_hook(
        trigger_chars=500,
        safety_ceiling_chars=13000,
        keep_budget_fraction=0.45,
        summary_budget_fraction=0.08,
        trigger_tokens=100000,
        safety_ceiling_tokens=100000,
    )
    messages = [{"role": "system", "content": "s" * 100}]
    raw_results = []
    for index in range(5):
        raw = _project_evidence_result(
            chunk_prefix=f"tier-{index}",
            snippet_chars=3000,
        )
        raw_results.append(raw)
        _append_tool_exchange(messages, "retrieve_project_evidence", index, raw)

    edited = hook({"messages": messages})["messages"][1:]
    tool_messages = [message for message in edited if isinstance(message, _FakeToolMessage)]

    assert len(tool_messages) == 5
    assert tool_messages[-1].content == raw_results[-1]
    assert "有界摘录" in tool_messages[-2].content
    assert "仅保留可重新定位的信息" in tool_messages[0].content
    assert "project-evidence-tier-0-0" in tool_messages[0].content


def test_hook_keeps_latest_project_evidence_raw_when_newer_research_follows():
    """Latest Project Evidence is semantic recency, not the final tool message."""
    hook = build_context_editing_hook(
        trigger_chars=500,
        safety_ceiling_chars=9000,
        keep_budget_fraction=0.1,
        summary_budget_fraction=0.15,
        trigger_tokens=100000,
        safety_ceiling_tokens=100000,
    )
    messages = [{"role": "system", "content": "s" * 100}]
    older_evidence = _project_evidence_result(
        chunk_prefix="older",
        snippet_chars=3000,
    )
    latest_evidence = _project_evidence_result(
        chunk_prefix="latest",
        snippet_chars=3000,
    )
    _append_tool_exchange(messages, "retrieve_project_evidence", 0, older_evidence)
    _append_tool_exchange(messages, "retrieve_project_evidence", 1, latest_evidence)
    _append_tool_exchange(
        messages,
        "web_search",
        2,
        "- 临时结果：" + "后续联网结果" * 400 + "（来源：https://example.org/web）",
    )
    _append_tool_exchange(messages, "retrieve_materials", 3, _rm_result("later-material"))

    edited = hook({"messages": messages})["messages"][1:]
    tool_messages = {
        message.tool_call_id: message
        for message in edited
        if isinstance(message, _FakeToolMessage)
    }

    assert tool_messages["retrieve_project_evidence-1"].content == latest_evidence
    assert tool_messages["retrieve_project_evidence-0"].content != older_evidence
    assert "project-evidence-older-0" in tool_messages["retrieve_project_evidence-0"].content
    assert sum(_msg_len(message) for message in edited) <= 9000


def test_hook_summarizes_latest_project_evidence_when_raw_exceeds_token_ceiling():
    """Raw evidence is preferred, but the complete request hard limit still wins."""
    hook = build_context_editing_hook(
        trigger_chars=500,
        safety_ceiling_chars=20000,
        keep_budget_fraction=0.2,
        summary_budget_fraction=0.2,
        trigger_tokens=1,
        safety_ceiling_tokens=700,
        max_messages=20,
    )
    messages = [{"role": "system", "content": "s" * 100}]
    latest_evidence = _project_evidence_result(
        chunk_prefix="token-fallback",
        snippet_chars=6000,
    )
    _append_tool_exchange(messages, "retrieve_project_evidence", 0, latest_evidence)

    edited = hook({"messages": messages})["messages"][1:]
    evidence_message = next(
        message
        for message in edited
        if isinstance(message, _FakeToolMessage)
        and message.tool_call_id == "retrieve_project_evidence-0"
    )

    assert evidence_message.content != latest_evidence
    assert "较早原文已压缩为有界摘录" in evidence_message.content
    assert "project-evidence-token-fallback-0" in evidence_message.content
    assert "https://example.org/token-fallback/0" in evidence_message.content
    assert sum(_message_payload_len(message) for message in edited) <= 20000
    assert _estimate_tokens(edited) <= 700
    assert len(edited) <= 20


@pytest.mark.parametrize(
    "evidence_payload_lengths",
    [
        (15878, 15649, 16286),
        (8265, 8563, 6990, 8630),
        (10979, 10979, 11791),
        (15878, 15649, 16286),
        (12324, 10074, 10252),
    ],
)
def test_v7_lifestyle_project_evidence_budget_events_replay_within_limits(
    evidence_payload_lengths,
):
    """Equivalent replay of the five V7 budget events using their observed sizes."""
    hook = build_context_editing_hook()
    messages = [
        {"role": "system", "content": "s" * 8389},
        {"role": "user", "content": "修复Day引用" * 639},
    ]
    raw_results = []
    for index, payload_length in enumerate(evidence_payload_lengths):
        raw = _project_evidence_result(
            chunk_prefix=f"v7-{index}",
            snippet_chars=payload_length,
        )
        raw_results.append(raw)
        _append_tool_exchange(messages, "retrieve_project_evidence", index, raw)

    assert sum(_msg_len(message) for message in messages) > 40000

    edited = hook({"messages": messages})["messages"][1:]
    tool_messages = [message for message in edited if isinstance(message, _FakeToolMessage)]

    assert sum(_msg_len(message) for message in edited) <= 40000
    assert tool_messages[-1].content == raw_results[-1]
    assert all(
        f"project-evidence-v7-{index}-0" in message.content
        for index, message in enumerate(tool_messages)
    )
    assert any("有界摘录" in message.content for message in tool_messages[:-1])


def test_hook_caps_total_size_when_many_calls_accumulate():
    """2026-08-11真实E2E复测踩出的坑的回归测试：真实场景一次做了72次retrieve_materials/
    web_search调用，"tier2富摘要"这个桶本身随调用次数线性增长、重新逼近硬阈值——这里用30次
    调用模拟同款场景，验证压缩后总量不会随调用次数线性增长到危险区间（而是被tier3极简层兜底）。
    """
    hook = build_context_editing_hook()  # 用默认参数（trigger=15000, ceiling=40000）
    messages = [{"role": "system", "content": "s" * 5000}]  # 模拟真实system prompt大小
    for i in range(30):
        tid = f"call-{i}"
        messages.append(_FakeAIWithToolCall([{"name": "retrieve_materials", "id": tid}]))
        messages.append(_FakeToolMessage(_rm_result(chunk_id=f"c{i:03d}"), tool_call_id=tid))

    total_before = sum(_msg_len(m) for m in messages)
    assert total_before > 20000  # 确认这次模拟数据本身会撞上真实场景同款问题（30*564+5000=21920）

    result = hook({"messages": messages})
    edited = result["messages"][1:]
    total_after = sum(_msg_len(m) for m in edited)
    # 30次调用即使全部走tier2富摘要（107字符/条）也才3210字符，不该出现真实bug里"随调用次数
    # 线性涨回危险区间"的情况——这里断言总量明显低于压缩前，且留有安全余量。
    assert total_after < total_before
    assert total_after < 40000 + 5000  # safety_ceiling + 固定开销的宽松上限，不应该被打穿


def test_hook_never_touches_ai_message_tool_call_args():
    """record_finding的调用参数（模型自己记的结论）必须永不被清理——压缩机制只清理
    ToolMessage的返回内容，不touch AIMessage.tool_calls的args。"""
    hook = build_context_editing_hook(trigger_chars=500, safety_ceiling_chars=600)
    messages = [{"role": "system", "content": "s" * 100}]
    for i in range(5):
        tid = f"call-{i}"
        messages.append(_FakeAIWithToolCall([{"name": "retrieve_materials", "id": tid}]))
        messages.append(_FakeToolMessage(_rm_result(chunk_id=f"c{i:03d}"), tool_call_id=tid))
    finding_ai = _FakeAIWithToolCall(
        [{"name": "record_finding", "id": "call-finding", "args": {"fact": "关键结论：仅限内部车辆"}}]
    )
    messages.append(finding_ai)
    messages.append(_FakeToolMessage("已记录关键结论", tool_call_id="call-finding"))

    result = hook({"messages": messages})
    edited = result["messages"][1:]

    ai_finding = [
        m
        for m in edited
        if isinstance(m, _FakeAIWithToolCall) and m.tool_calls and m.tool_calls[0]["name"] == "record_finding"
    ][0]
    assert ai_finding.tool_calls[0]["args"] == {"fact": "关键结论：仅限内部车辆"}


def test_hook_can_remove_completed_old_empty_retrieval_pair():
    """短空结果不改写，但超过硬预算时允许连同已完成的旧AI调用成对淘汰。"""
    hook = build_context_editing_hook(trigger_chars=500, safety_ceiling_chars=600)
    messages = [{"role": "system", "content": "s" * 100}]
    messages.append(_FakeAIWithToolCall([{"name": "retrieve_materials", "id": "call-empty"}]))
    messages.append(_FakeToolMessage("没有检索到相关素材。", tool_call_id="call-empty"))
    for i in range(5):
        tid = f"call-{i}"
        messages.append(_FakeAIWithToolCall([{"name": "retrieve_materials", "id": tid}]))
        messages.append(_FakeToolMessage(_rm_result(chunk_id=f"c{i:03d}"), tool_call_id=tid))

    result = hook({"messages": messages})
    edited = result["messages"][1:]
    assert not any(
        isinstance(m, _FakeToolMessage) and m.tool_call_id == "call-empty" for m in edited
    )


def test_hook_stops_when_latest_failed_write_draft_alone_exceeds_budget():
    hook = build_context_editing_hook(
        trigger_chars=100000,
        safety_ceiling_chars=2000,
        trigger_tokens=500,
        safety_ceiling_tokens=1000,
        recent_messages=4,
    )
    messages = [
        {"role": "user", "content": "生成POI"},
        _FakeAIWithToolCall(
            [{"name": "write_agent_output_patch", "id": "write-1", "args": {"routes": "x" * 8000}}]
        ),
        _FakeToolMessage("工具参数校验失败", tool_call_id="write-1"),
    ]

    with pytest.raises(ContextBudgetExceededError):
        hook({"messages": messages})


def test_hook_prunes_old_completed_pairs_when_message_count_grows():
    hook = build_context_editing_hook(
        trigger_chars=100000,
        safety_ceiling_chars=100000,
        trigger_tokens=100000,
        safety_ceiling_tokens=100000,
        max_messages=20,
        recent_messages=4,
    )
    messages = [{"role": "user", "content": "原始任务"}]
    for i in range(30):
        tid = f"short-{i}"
        messages.append(_FakeAIWithToolCall([{"name": "retrieve_materials", "id": tid}]))
        messages.append(_FakeToolMessage("没有检索到相关素材。", tool_call_id=tid))

    edited = hook({"messages": messages})["messages"][1:]

    assert len(edited) <= 20
    assert edited[0]["content"] == "原始任务"


def test_hook_counts_static_prompt_before_the_pre_model_hook():
    hook = build_context_editing_hook(
        static_messages=(SystemMessage(content="固定系统提示" * 1000),),
        trigger_chars=100,
        safety_ceiling_chars=500,
        trigger_tokens=100,
        safety_ceiling_tokens=300,
    )

    with pytest.raises(ContextBudgetExceededError):
        hook({"messages": [{"role": "user", "content": "短任务"}]})


def test_hook_drops_stale_failed_writes_but_keeps_latest_complete_repair_draft():
    hook = build_context_editing_hook(
        tools=(_FakeBoundTool("write_agent_output_patch"),),
        trigger_chars=1000,
        safety_ceiling_chars=9000,
        trigger_tokens=500,
        safety_ceiling_tokens=6000,
        recent_messages=8,
    )
    messages = [{"role": "user", "content": "修复POI"}]
    for i in range(4):
        tid = f"write-{i}"
        messages.append(
            _FakeAIWithToolCall(
                [{"name": "write_agent_output_patch", "id": tid, "args": {"routes": str(i) * 6000}}]
            )
        )
        messages.append(_FakeToolMessage("工具参数校验失败", tool_call_id=tid))

    edited = hook({"messages": messages})["messages"][1:]
    write_calls = [
        call
        for message in edited
        for call in (getattr(message, "tool_calls", None) or [])
        if call.get("name") == "write_agent_output_patch"
    ]

    assert [call["id"] for call in write_calls] == ["write-3"]
    assert write_calls[0]["args"] == {"routes": "3" * 6000}
    latest_error = next(
        message
        for message in edited
        if isinstance(message, _FakeToolMessage) and message.tool_call_id == "write-3"
    )
    assert latest_error.content == "工具参数校验失败"


def test_hook_drops_old_failed_write_after_later_allowed_write_succeeds():
    hook = build_context_editing_hook(
        tools=(_FakeBoundTool("write_agent_output_patch"),),
        trigger_chars=1,
        safety_ceiling_chars=100000,
        trigger_tokens=1,
        safety_ceiling_tokens=100000,
        max_messages=50,
        recent_messages=20,
    )
    messages = [
        {"role": "user", "content": "修复路线"},
        _FakeAIWithToolCall(
            [
                {
                    "name": "write_agent_output_patch",
                    "id": "write-failed",
                    "args": {"routes": "old"},
                }
            ]
        ),
        _FakeToolMessage("工具参数校验失败：字段缺失", tool_call_id="write-failed"),
        _FakeAIWithToolCall(
            [
                {
                    "name": "write_agent_output_patch",
                    "id": "write-success",
                    "args": {"routes": "fixed"},
                }
            ]
        ),
        _FakeToolMessage("写入成功", tool_call_id="write-success"),
    ]

    edited = hook({"messages": messages})["messages"][1:]

    assert not any(
        isinstance(message, _FakeToolMessage)
        and message.tool_call_id == "write-failed"
        for message in edited
    )
    assert any(
        isinstance(message, _FakeToolMessage)
        and message.tool_call_id == "write-success"
        for message in edited
    )


def test_default_budget_keeps_large_latest_repair_draft_when_token_budget_is_safe():
    """40k字符边界应容纳POI最新底稿，不因旧35k余量误删工作状态。"""
    hook = build_context_editing_hook(
        static_messages=(SystemMessage(content="s" * 5000),),
        trigger_tokens=100000,
    )
    messages = [
        {"role": "user", "content": "修复POI"},
        _FakeAIWithToolCall(
            [{"name": "write_agent_output", "id": "write-latest", "args": {"routes": "x" * 30000}}]
        ),
        _FakeToolMessage("工具参数校验失败：仅列前8条", tool_call_id="write-latest"),
    ]

    edited = hook({"messages": messages})["messages"][1:]
    write_call = next(
        call
        for message in edited
        for call in (getattr(message, "tool_calls", None) or [])
        if call.get("id") == "write-latest"
    )
    assert write_call["args"] == {"routes": "x" * 30000}
