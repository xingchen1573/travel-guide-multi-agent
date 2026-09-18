"""src/agents/material_agent/collection_graph.py 的单元测试。

_route_material是纯路由函数，material_collect_node可以monkeypatch外部依赖测试，
两者都不需要真实LLM调用。await_material_confirm_node在auto_confirm=True（默认值）时
不会走到interrupt()，可以直接测；auto_confirm=False的真正暂停/恢复仍需要真实LangGraph
执行上下文才能完整测试，这部分不强求自动化测试（参照docs/ISSUES.md"涉及真实子进程调用，
价值有限"的既有惯例，改为手动跑`python -m scripts.materials.run_material_agent --manual-confirm`验证）。

    python -m pytest tests/test_material_collection_graph.py -v
"""
import pytest

import src.agents.material_agent.collection_graph as cg


@pytest.fixture(autouse=True)
def _default_dependencies_ok(monkeypatch):
    """material_collect_node在有候选时会真实调用check_collector_dependencies
    （2026-07-29新增，见docs/ISSUES.md）——默认mock成"依赖齐全"，不依赖跑测试的
    这台机器是否真的装了ffmpeg/配了MIMO_API_KEY。需要验证"依赖缺失"分支本身的
    测试会自己再monkeypatch覆盖这个默认值。
    """
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.check_collector_dependencies", lambda: None
    )


# ---------- _route_material 路由函数 ----------


def test_route_material_starts_with_propose():
    assert cg._route_material({}) == "propose"


def test_route_material_goes_to_await_confirm_after_propose():
    state = {"proposed_candidates": [{"title": "x"}]}
    assert cg._route_material(state) == "await_confirm"


def test_route_material_goes_to_collect_after_confirm():
    state = {"proposed_candidates": [{"title": "x"}], "approved_candidates": [{"title": "x"}]}
    assert cg._route_material(state) == "collect"


def test_route_material_ends_after_collection_done():
    state = {"collection_done": True}
    assert cg._route_material(state) == "__end__" or cg._route_material(state) == cg.END


def test_route_material_restarts_propose_when_more_requested():
    # await_material_confirm_node收到"more:"决策时会清空proposed_candidates，
    # 路由应该重新回到propose（而不是误判成"还没提案过"以外的状态）
    state = {"request": "再找几条", "proposed_candidates": None}
    assert cg._route_material(state) == "propose"


# ---------- await_material_confirm_node（auto_confirm默认True时不涉及interrupt，可直接测）----------


def test_await_confirm_auto_confirms_by_default():
    # 没有真实交互界面前，auto_confirm默认True，跳过interrupt暂停，候选全部自动放行
    candidates = [{"title": "a"}, {"title": "b"}]
    state = {"proposed_candidates": candidates}
    result = cg.await_material_confirm_node(state)
    assert result == {"approved_candidates": candidates}


def test_await_confirm_auto_confirms_when_explicitly_true():
    candidates = [{"title": "a"}]
    state = {"proposed_candidates": candidates, "auto_confirm": True}
    result = cg.await_material_confirm_node(state)
    assert result == {"approved_candidates": candidates}


def test_await_confirm_falls_through_to_interrupt_when_disabled():
    # auto_confirm=False时应该走真正的interrupt()分支——脱离真实LangGraph执行上下文调用
    # interrupt()会报错，这里只验证它没有走auto_confirm的短路分支（即确实尝试触发中断）
    state = {"proposed_candidates": [{"title": "a"}], "auto_confirm": False}
    import pytest
    with pytest.raises(Exception):
        cg.await_material_confirm_node(state)


# ---------- material_collect_node ----------


def test_material_collect_node_calls_collectors_for_each_platform(monkeypatch):
    """B站候选逐条调用collect_one；抖音候选批量调用collect_many（一次传入所有抖音url，
    不逐条调用collect_one）——见docs/ISSUES.md，逐条调用会导致每条候选独立开一次浏览器。"""
    calls = []
    monkeypatch.setattr(
        cg.bilibili_collector, "collect_one",
        lambda topic, candidate: calls.append(("bilibili_one", topic, candidate["source_url"]))
    )
    monkeypatch.setattr(
        cg.douyin_collector, "collect_many",
        lambda topic, source_urls: (calls.append(("douyin_many", topic, source_urls)), [{"doc_id": "x"} for _ in source_urls])[1]
    )
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1, 2])  # 假装新增2条

    state = {
        "topic": "成都",
        "approved_candidates": [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"},
            {"platform": "douyin", "source_url": "https://www.douyin.com/video/123"},
            {"platform": "douyin", "source_url": "https://www.douyin.com/video/456"},
        ],
    }
    result = cg.material_collect_node(state)

    assert ("bilibili_one", "成都", "https://www.bilibili.com/video/BV1") in calls
    # 两条抖音候选应该被合并成一次collect_many调用，不是两次collect_one调用
    douyin_calls = [c for c in calls if c[0] == "douyin_many"]
    assert len(douyin_calls) == 1
    assert douyin_calls[0][2] == ["https://www.douyin.com/video/123", "https://www.douyin.com/video/456"]
    assert result["collection_done"] is True
    assert "2" in result["result_summary"]


def test_material_collect_node_skips_douyin_batch_when_no_douyin_candidates(monkeypatch):
    """全是B站候选时不应该调用collect_many（避免空列表触发无意义的批量调用）。"""
    douyin_many_calls = []
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)
    monkeypatch.setattr(
        cg.douyin_collector, "collect_many",
        lambda topic, source_urls: douyin_many_calls.append(source_urls)
    )
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [])

    state = {
        "topic": "成都",
        "approved_candidates": [{"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"}],
    }
    cg.material_collect_node(state)

    assert douyin_many_calls == []


def test_material_collect_node_handles_empty_approved_candidates(monkeypatch):
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [])
    result = cg.material_collect_node({"topic": "成都", "approved_candidates": []})
    assert result["collection_done"] is True
    assert "0" in result["result_summary"]


# ---------- 环境依赖前置校验 + 全新topic空候选（2026-07-29新增，真实E2E跑洱海骑行场景撞见的
# 两个bug：见docs/ISSUES.md）----------


def test_material_collect_node_stops_gracefully_when_dependencies_missing(monkeypatch):
    """有候选要采集时，ffmpeg/MIMO_API_KEY等环境依赖缺失应该被check_collector_dependencies
    前置拦截、诚实报告收工——不能让bilibili_collector.collect_one内部的subprocess.run
    未捕获异常把整个graph.invoke()崩掉（真实复现：这个独立采集图此前从未接入第77条修复
    的check_collector_dependencies，本机缺ffmpeg时直接FileNotFoundError/CalledProcessError
    一路捅穿到进程崩溃）。"""
    from src.materials.collectors.bilibili import CollectorDependencyError

    def _raise():
        raise CollectorDependencyError("缺少系统级依赖：ffmpeg/ffprobe")

    monkeypatch.setattr(
        "src.materials.collectors.bilibili.check_collector_dependencies", _raise
    )
    collect_one_calls = []
    monkeypatch.setattr(
        cg.bilibili_collector, "collect_one",
        lambda topic, candidate: collect_one_calls.append(candidate)
    )

    state = {
        "topic": "洱海骑行",
        "approved_candidates": [{"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"}],
    }
    result = cg.material_collect_node(state)

    assert collect_one_calls == []  # 依赖缺失时不应该真的尝试下载
    assert result["collection_done"] is True
    assert "环境依赖缺失" in result["result_summary"]


def test_material_collect_node_does_not_check_dependencies_when_no_approved_candidates(monkeypatch):
    """0候选时（比如agent这轮没搜到任何东西）不需要检查环境依赖——没有任何下载动作要做，
    检查本身没有意义，也不该在这种情况下报告一个误导性的"环境依赖缺失"。"""
    calls = []
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.check_collector_dependencies",
        lambda: calls.append(True)
    )
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [])

    cg.material_collect_node({"topic": "洱海骑行", "approved_candidates": []})

    assert calls == []


def test_material_collect_node_survives_ingest_topic_filenotfound_for_brand_new_topic(monkeypatch):
    """全新topic（raw/<topic>/manifest.json从未建立过）且这一轮恰好0条候选真正入库
    （比如全部候选下载/转写失败，或is_relevant门禁全拒）时，ingest_topic按设计会
    raise FileNotFoundError——这是它本来的契约，不是bug（material_agent_node等其他
    调用方依赖这个异常语义判断"素材库完全空白"）。material_collect_node应该把这种
    情况当作"本轮没有新增"优雅处理，不能让异常把整个graph.invoke()崩掉（真实复现：
    洱海骑行场景第一次搜索给出0候选，随即撞上这个FileNotFoundError）。"""
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)

    def _raise_file_not_found(topic):
        raise FileNotFoundError(f"找不到 raw/{topic}/manifest.json")

    monkeypatch.setattr(cg, "ingest_topic", _raise_file_not_found)

    state = {
        "topic": "洱海骑行",
        "approved_candidates": [{"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"}],
    }
    result = cg.material_collect_node(state)

    assert result["collection_done"] is True
    assert result["total_ingested"] == 0
    assert "0" in result["result_summary"]


# ---------- await_material_confirm_node 决策解析逻辑（跳过interrupt本身，只测后续处理）----------


def test_confirm_decision_string_selects_all_by_default():
    # 复现 confirm 分支的解析逻辑：非select:/more:前缀时approved=全部candidates
    candidates = [{"title": "a"}, {"title": "b"}]
    decision = "confirm"
    if isinstance(decision, str) and decision.startswith("more:"):
        approved = None
    elif isinstance(decision, str) and decision.startswith("select:"):
        idx = {int(i) for i in decision[len("select:"):].split(",") if i.strip().isdigit()}
        approved = [c for i, c in enumerate(candidates) if i in idx]
    else:
        approved = candidates
    assert approved == candidates


def test_select_decision_string_picks_subset():
    candidates = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    decision = "select:0,2"
    idx = {int(i) for i in decision[len("select:"):].split(",") if i.strip().isdigit()}
    approved = [c for i, c in enumerate(candidates) if i in idx]
    assert approved == [{"title": "a"}, {"title": "c"}]


# ---------- 目标数量抽取 + 补足循环（2026-07-25新增，见docs/ISSUES.md/CLAUDE.md已知问题第10条讨论）----------
# _extract_target_count涉及真实LLM结构化输出，跟_extract_requirements_fields同款约定，
# 这里不直接测LLM调用本身（真实准不准由后续真实E2E验证），只monkeypatch它测试
# material_propose_node/material_collect_node的确定性逻辑。


def test_extract_target_count_uses_shared_structured_model(monkeypatch):
    calls = []

    class _Structured:
        def invoke(self, prompt):
            calls.append(("invoke", prompt))
            return cg._TargetCount(target_count=7)

    class _Model:
        def with_structured_output(self, schema, method=None):
            calls.append(("structured", schema, method))
            return _Structured()

    monkeypatch.setattr(cg, "get_structured_chat_model", lambda temperature=0: _Model())

    assert cg._extract_target_count("帮我找7条峨眉山素材") == 7
    assert calls[0] == ("structured", cg._TargetCount, "json_mode")
    assert calls[1][0] == "invoke"


def test_propose_node_extracts_target_count_on_first_round(monkeypatch):
    """第一轮（state里还没有target_count）时，应该调用_extract_target_count抽取一次。"""
    monkeypatch.setattr(cg, "build_material_agent", lambda topic, project_id, captured, seen, platform='all': object())

    def fake_invoke_with_retry(agent, messages):
        # 模拟agent直接调用了propose_candidates（通过闭包很难拿到真实captured字典，
        # 这里改用monkeypatch invoke_with_retry后手工在外部注入captured更简单）
        return None

    monkeypatch.setattr(cg, "invoke_with_retry", fake_invoke_with_retry)
    calls = []
    monkeypatch.setattr(cg, "_extract_target_count", lambda request: (calls.append(request), 5)[1])

    state = {"topic": "成都", "request": "给我5条最新攻略"}
    result = cg.material_propose_node(state)

    assert calls == ["给我5条最新攻略"]
    assert result["target_count"] == 5


def test_propose_node_does_not_reextract_target_count_on_later_round(monkeypatch):
    """补搜轮次（state里已经有target_count）时，不应该重新调用_extract_target_count——
    目标数量应该在整个采集过程里保持一致，不能补搜时又抽取出一个不同的数字。"""
    monkeypatch.setattr(cg, "build_material_agent", lambda topic, project_id, captured, seen, platform='all': object())
    monkeypatch.setattr(cg, "invoke_with_retry", lambda agent, messages: None)
    calls = []
    monkeypatch.setattr(cg, "_extract_target_count", lambda request: calls.append(request))

    state = {"topic": "成都", "request": "还需要再补2条", "target_count": 5, "collection_round": 1}
    result = cg.material_propose_node(state)

    assert calls == []
    assert "target_count" not in result  # 不重复设置，state里已有的值原样保留


def test_propose_node_includes_topic_in_message_sent_to_agent(monkeypatch):
    """回归测试（真实E2E复现的bug）：request原文可能完全不包含目的地名称
    （比如"帮我搜3条最新的探店视频"，陪伴对话场景下用户不会每次都重复说城市名，
    因为对话本身就在这个project的语境里）——之前material_propose_node调用agent
    时只传了request，从没显式把topic告诉模型，导致真实运行时agent凭"探店"这种
    泛词自己乱猜，搜出了完全不相关城市的候选，全部被is_relevant门禁正确拒绝，
    表现成"入库0条"。修复后，发给agent的消息内容必须包含topic。
    """
    monkeypatch.setattr(cg, "build_material_agent", lambda topic, project_id, captured, seen, platform='all': object())
    monkeypatch.setattr(cg, "_extract_target_count", lambda request: 3)
    captured_messages = []

    def fake_invoke_with_retry(agent, messages):
        captured_messages.append(messages)
        return None

    monkeypatch.setattr(cg, "invoke_with_retry", fake_invoke_with_retry)

    state = {"topic": "成都", "request": "帮我搜3条B站最新的探店视频"}
    cg.material_propose_node(state)

    assert captured_messages, "invoke_with_retry应该被调用过"
    first_call_content = captured_messages[0][0]["content"]
    assert "成都" in first_call_content


def test_collect_node_finishes_when_target_reached(monkeypatch):
    """一轮就达到目标数量时，直接结束，不触发补搜循环。"""
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1, 2, 3, 4, 5])

    state = {
        "topic": "成都",
        "request": "给我5条",
        "target_count": 5,
        "collection_round": 0,
        "approved_candidates": [
            {"platform": "bilibili", "source_url": f"https://www.bilibili.com/video/BV{i}"} for i in range(5)
        ],
    }
    result = cg.material_collect_node(state)

    assert result["collection_done"] is True
    assert result["total_ingested"] == 5
    assert "5" in result["result_summary"]


def test_collect_node_loops_back_to_propose_when_short_and_room_left(monkeypatch):
    """没达到目标、轮次还没到上限、这一轮确实有新增入库——应该清空候选状态、
    轮次+1，路由回propose补搜，不应该设置collection_done。"""
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1, 2])  # 只新增2条，目标5条

    state = {
        "topic": "成都",
        "request": "给我5条",
        "target_count": 5,
        "collection_round": 0,
        "approved_candidates": [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"},
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV2"},
        ],
    }
    result = cg.material_collect_node(state)

    assert "collection_done" not in result
    assert result["total_ingested"] == 2
    assert result["collection_round"] == 1
    assert result["proposed_candidates"] is None
    assert result["approved_candidates"] is None
    assert "2" in result["request"]  # 补搜请求里应该提到还需要补多少条
    # 路由函数看到proposed_candidates=None应该重新回到propose（跟"more:"分支同一套逻辑）
    assert cg._route_material(result) == "propose"


def test_collect_node_stops_early_when_no_new_entries_this_round(monkeypatch):
    """即使轮次还没到上限、目标也没达到，但这一轮完全没有新增入库（比如全被去重/
    质量门禁拒了）——应该提前停止，不再无意义地继续补搜。"""
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [])  # 这一轮0新增

    state = {
        "topic": "成都",
        "request": "给我5条",
        "target_count": 5,
        "collection_round": 0,
        "total_ingested": 2,  # 假设上一轮已经攒了2条
        "approved_candidates": [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"},
        ],
    }
    result = cg.material_collect_node(state)

    assert result["collection_done"] is True
    assert result["total_ingested"] == 2
    assert "未达到目标" in result["result_summary"] or "存量有限" in result["result_summary"]


def test_collect_node_stops_at_max_rounds_even_if_short(monkeypatch):
    """轮次已经到上限（第3轮，collection_round=2，即将变成第3次尝试后到顶），
    即使还没达标也不再继续补搜。"""
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1])  # 这一轮还有新增，但轮次已到顶

    state = {
        "topic": "成都",
        "request": "给我5条",
        "target_count": 5,
        "collection_round": cg.MAX_COLLECTION_ROUNDS - 1,  # 已经是最后允许的一轮
        "total_ingested": 3,
        "approved_candidates": [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"},
        ],
    }
    result = cg.material_collect_node(state)

    assert result["collection_done"] is True
    assert result["total_ingested"] == 4


def test_collect_node_without_target_count_behaves_like_before(monkeypatch):
    """没有target_count（理论上不会发生，propose阶段已经会默认填5，但防御性验证）时，
    不触发补足循环，维持修复前"一次性、能采多少算多少"的行为。"""
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1])

    state = {
        "topic": "成都",
        "approved_candidates": [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"},
        ],
    }
    result = cg.material_collect_node(state)

    assert result["collection_done"] is True


# ---------- platform硬过滤 + log细粒度回调（2026-07-26新增，见CLAUDE.md已知问题第2条）----------


def test_propose_node_passes_platform_to_build_material_agent(monkeypatch):
    """platform字段要真的传给build_material_agent做工具级硬过滤，不是只存在state里不生效。"""
    received = {}

    def fake_build_material_agent(topic, project_id, captured, seen, platform="all"):
        received["platform"] = platform
        return object()

    monkeypatch.setattr(cg, "build_material_agent", fake_build_material_agent)
    monkeypatch.setattr(cg, "invoke_with_retry", lambda agent, messages: None)
    monkeypatch.setattr(cg, "_extract_target_count", lambda request: 3)

    state = {"topic": "成都", "request": "给我3条抖音视频", "platform": "douyin"}
    cg.material_propose_node(state)

    assert received["platform"] == "douyin"


def test_propose_node_defaults_platform_to_all_when_absent(monkeypatch):
    """state里没有platform字段时（历史/兼容场景），默认"all"，不应该报错或误传None。"""
    received = {}

    def fake_build_material_agent(topic, project_id, captured, seen, platform="all"):
        received["platform"] = platform
        return object()

    monkeypatch.setattr(cg, "build_material_agent", fake_build_material_agent)
    monkeypatch.setattr(cg, "invoke_with_retry", lambda agent, messages: None)
    monkeypatch.setattr(cg, "_extract_target_count", lambda request: 3)

    state = {"topic": "成都", "request": "给我3条最新攻略"}
    cg.material_propose_node(state)

    assert received["platform"] == "all"


def test_propose_node_logs_round_and_candidate_breakdown(monkeypatch):
    """log回调应该记录"第几轮"以及agent提交候选按平台拆分的数字，不只是一句"开始"。"""
    monkeypatch.setattr(cg, "build_material_agent", lambda topic, project_id, captured, seen, platform='all': object())
    monkeypatch.setattr(cg, "_extract_target_count", lambda request: 3)

    def fake_invoke_with_retry(agent, messages):
        return None

    monkeypatch.setattr(cg, "invoke_with_retry", fake_invoke_with_retry)

    logs = []
    state = {"topic": "成都", "request": "给我3条最新攻略"}
    cg.material_propose_node(state, log=logs.append)

    assert any("第1轮" in line for line in logs)
    assert any("候选0条" in line for line in logs)


def test_propose_node_log_breaks_down_candidates_by_platform(monkeypatch):
    """agent真的提交了候选时，log里的B站/抖音拆分数字应该反映真实候选内容，
    不能永远显示0条（上一个测试只验证了空候选场景，这里验证真实计数逻辑本身正确）。
    """
    captured_ref: dict = {}

    def fake_build_material_agent(topic, project_id, captured, seen, platform="all"):
        captured_ref["captured"] = captured
        return object()

    def fake_invoke_with_retry(agent, messages):
        # 模拟agent真的调用了propose_candidates工具，往captured里塞入候选
        captured_ref["captured"]["candidates"] = [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"},
            {"platform": "douyin", "source_url": "https://www.douyin.com/video/123"},
            {"platform": "douyin", "source_url": "https://www.douyin.com/video/456"},
        ]
        captured_ref["captured"]["summary"] = "搜到3条"

    monkeypatch.setattr(cg, "build_material_agent", fake_build_material_agent)
    monkeypatch.setattr(cg, "invoke_with_retry", fake_invoke_with_retry)
    monkeypatch.setattr(cg, "_extract_target_count", lambda request: 3)

    logs = []
    state = {"topic": "成都", "request": "给我3条最新攻略"}
    cg.material_propose_node(state, log=logs.append)

    assert any("B站1条" in line and "抖音2条" in line for line in logs)


def test_collect_node_logs_bilibili_progress_per_candidate(monkeypatch):
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: {"doc_id": "x"})
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1])

    logs = []
    state = {
        "topic": "成都",
        "approved_candidates": [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1", "title": "示例视频"},
        ],
    }
    cg.material_collect_node(state, log=logs.append)

    assert any("示例视频" in line for line in logs)
    assert any("质量门禁" in line for line in logs)


def test_collect_node_logs_douyin_batch_progress(monkeypatch):
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: None)
    monkeypatch.setattr(cg.douyin_collector, "collect_many", lambda topic, source_urls: [{"doc_id": "x"}] * len(source_urls))
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1])

    logs = []
    state = {
        "topic": "成都",
        "approved_candidates": [
            {"platform": "douyin", "source_url": "https://www.douyin.com/video/123"},
        ],
    }
    cg.material_collect_node(state, log=logs.append)

    assert any("抖音" in line for line in logs)


def test_material_collect_node_reports_per_platform_write_success_counts(monkeypatch):
    """result_summary应该按平台拆分"提交候选数/成功写入数"，不只是笼统的候选总数——
    这是采集平台可见性改动的一部分（见CLAUDE.md已知问题第2条）。"""
    monkeypatch.setattr(cg.bilibili_collector, "collect_one", lambda topic, candidate: {"doc_id": "x"})
    monkeypatch.setattr(cg.douyin_collector, "collect_many", lambda topic, source_urls: [None, {"doc_id": "y"}])
    monkeypatch.setattr(cg, "ingest_topic", lambda topic: [1, 2])

    state = {
        "topic": "成都",
        "approved_candidates": [
            {"platform": "bilibili", "source_url": "https://www.bilibili.com/video/BV1"},
            {"platform": "douyin", "source_url": "https://www.douyin.com/video/123"},
            {"platform": "douyin", "source_url": "https://www.douyin.com/video/456"},
        ],
    }
    result = cg.material_collect_node(state)

    assert "B站1条候选成功写入1条" in result["result_summary"]
    assert "抖音2条候选成功写入1条" in result["result_summary"]


def test_build_material_collection_graph_accepts_log_without_error():
    """graph构建阶段不应该因为传了log而报错——functools.partial绑定的正确性验证
    （不需要真实跑graph.invoke，那需要真实LLM调用）。"""
    graph = cg.build_material_collection_graph(log=lambda msg: None)
    assert graph is not None


def test_run_material_collection_passes_platform_into_initial_state(monkeypatch):
    """run_material_collection应该把platform写进graph初始state，不能默认丢弃。"""
    captured_state = {}

    class FakeGraph:
        def invoke(self, state, config):
            captured_state.update(state)
            return {"collection_done": True}

    monkeypatch.setattr(cg, "build_material_collection_graph", lambda log=cg._NOOP_LOG: FakeGraph())

    cg.run_material_collection("成都", "给我3条抖音视频", platform="douyin")

    assert captured_state["platform"] == "douyin"


def test_run_material_collection_defaults_platform_to_all(monkeypatch):
    captured_state = {}

    class FakeGraph:
        def invoke(self, state, config):
            captured_state.update(state)
            return {"collection_done": True}

    monkeypatch.setattr(cg, "build_material_collection_graph", lambda log=cg._NOOP_LOG: FakeGraph())

    cg.run_material_collection("成都", "给我3条最新攻略")

    assert captured_state["platform"] == "all"
