"""src/channels/feishu/service.py 的单元测试：核心路由逻辑，注入fake的ReplyChannel和
fake的orchestrator调用，不连真实飞书API/真实LLM，跟tests/test_api_jobs.py同款
"jobs.trigger起真实后台线程，轮询is_running等它跑完"手法。

2026-08-18新增：完成消息改进、帮助命令、新手引导、Agent标识的测试。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from src.api import jobs
from src.channels.feishu import bindings, push_log, service
from src.workspace import store
from src.workspace.schema import ProjectPhase, RouteDay, RoutePlan, RoutePoi


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    from src import llm_trace

    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(llm_trace, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(bindings, "BINDINGS_PATH", tmp_path / "feishu" / "bindings.json")
    monkeypatch.setattr(push_log, "PUSH_LOG_PATH", tmp_path / "feishu" / "push_log.jsonl")
    jobs._jobs.clear()
    # 🆕 默认开启agent标识（测试需要）
    monkeypatch.setattr(service, "FEISHU_SHOW_AGENT_INFO", True)
    yield


@pytest.fixture
def fake_generate_site(monkeypatch):
    """跟tests/test_api_jobs.py同款手法：用假模块替换sys.modules，不让
    `from scripts.delivery.run_generate_site import generate_site`这类局部import真的加载
    真实模块（避免真实模块的其它顶层依赖/副作用）。"""
    fake_module = types.ModuleType("scripts.delivery.run_generate_site")
    calls = []

    def fake_generate_site_fn(project_id):
        calls.append(project_id)
        return None

    fake_module.generate_site = fake_generate_site_fn
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_site", fake_module)
    return fake_module, calls


@pytest.fixture
def fake_generate_poster(monkeypatch):
    fake_module = types.ModuleType("scripts.delivery.run_generate_poster")
    calls = []

    def fake_generate_poster_fn(project_id, confirm=True):
        calls.append((project_id, confirm))
        return "/tmp/poster.png"

    fake_module.generate_poster = fake_generate_poster_fn
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_poster", fake_module)
    return fake_module, calls


class _FakeChannel(service.ReplyChannel):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str]] = []
        self.images: list[str] = []
        self._next_id = 0

    def send(self, text: str) -> str:
        self._next_id += 1
        message_id = f"msg-{self._next_id}"
        self.sent.append((message_id, text))
        return message_id

    def edit(self, message_id: str, text: str) -> None:
        self.edits.append((message_id, text))

    def send_image(self, image_path: str) -> str:
        self.images.append(image_path)
        return "fake-image-msg"


class _FakeStructuredResult:
    """模拟with_structured_output(method="json_mode")的invoke返回值——构造时传一个dict，
    .model_dump()原样返回它（模拟pydantic结构化输出对象）。"""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self):
        return dict(self._data)


class _FakeStructured:
    """模拟get_light_chat_model().with_structured_output(...)的链式调用——invoke返回
    _FakeStructuredResult，供_extract_user_note_fields的poi_name降级逻辑测试用。"""

    def __init__(self, data: dict):
        self._data = data

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, prompt):
        return _FakeStructuredResult(self._data)


def _wait_until_idle(project_id: str, timeout_steps: int = 100) -> None:
    for _ in range(timeout_steps):
        if not jobs.is_running(service.TASK_TYPE, project_id):
            return
        threading.Event().wait(0.02)


# ---------------------------------------------------------------------------
# split_for_reply
# ---------------------------------------------------------------------------


def test_split_for_reply_returns_single_chunk_when_under_limit():
    assert service.split_for_reply("短消息", limit=100) == ["短消息"]


def test_split_for_reply_splits_long_text_by_limit():
    text = "a" * 250
    chunks = service.split_for_reply(text, limit=100)
    assert chunks == ["a" * 100, "a" * 100, "a" * 50]


# ---------------------------------------------------------------------------
# 中断payload格式化/分类
# ---------------------------------------------------------------------------


def test_classify_interrupt_kind_branches():
    assert service._classify_interrupt_kind({"question": "q", "draft": {}}) == "requirements"
    assert service._classify_interrupt_kind({"question": "q", "route_change_proposal": {}}) == "route_structure_confirm"
    assert service._classify_interrupt_kind({"question": "q", "route_plan": {}}) == "skeleton_confirm"
    assert service._classify_interrupt_kind({"question": "q", "patch": {}}) == "realtime_approval"
    assert service._classify_interrupt_kind({"question": "q"}) == "unknown"


def test_format_route_plan_summary_deduplicates_city_prefix_from_area():
    text = service._format_route_plan_summary(
        {
            "days": [
                {
                    "day": 1,
                    "city": " 大理 ",
                    "area": " 大理古城—洱海西岸 ",
                    "pois": [{"name": "大理古城"}],
                }
            ]
        }
    )

    assert text == "Day1 大理古城—洱海西岸：大理古城"


def test_format_route_plan_summary_separates_distinct_city_and_area():
    text = service._format_route_plan_summary(
        {"days": [{"day": 2, "city": " 成都 ", "area": " 武侯区 ", "pois": [{"name": "武侯祠"}]}]}
    )

    assert text == "Day2 成都 · 武侯区：武侯祠"


def test_format_route_plan_summary_omits_empty_location_separator():
    text = service._format_route_plan_summary(
        {"days": [{"day": 3, "city": " ", "area": None, "pois": [{"name": "自由活动"}]}]}
    )

    assert text == "Day3：自由活动"


def test_format_interrupt_message_skeleton_confirm_includes_route_plan_summary():
    payload = {
        "question": "骨架已生成，是否确认？",
        "route_plan": {"days": [{"day": 1, "city": "成都", "area": "武侯区", "pois": [{"name": "武侯祠"}]}]},
    }
    text = service._format_interrupt_message("skeleton_confirm", payload)
    assert "骨架已生成" in text
    assert "Day1 成都 · 武侯区：武侯祠" in text


def test_format_interrupt_message_route_structure_shows_before_and_after():
    payload = {
        "question": "是否应用候选？",
        "route_change_proposal": {
            "reason": "原景点闭园",
            "before_days": [{"day": 2, "city": "成都", "pois": [{"name": "熊猫基地"}]}],
            "proposed_days": [{"day": 2, "city": "成都", "pois": [{"name": "人民公园"}]}],
        },
    }
    text = service._format_interrupt_message("route_structure_confirm", payload)
    assert "原景点闭园" in text
    assert "原安排" in text and "熊猫基地" in text
    assert "候选安排" in text and "人民公园" in text


def test_invoke_channel_route_structure_classifies_then_resumes(monkeypatch):
    monkeypatch.setattr("src.channels.feishu.intent.classify_route_structure_decision", lambda msg: "keep")
    captured = {}

    def fake_resume(project_id, decision):
        captured["decision"] = decision
        return {"ok": True}

    monkeypatch.setattr("src.orchestrator.run_channel_resume", fake_resume)
    assert service._invoke_channel("p1", "保留原路线", "route_structure_confirm") == {"ok": True}
    assert captured["decision"] == "keep"


def test_invoke_channel_route_structure_unclear_reasks_without_resuming(monkeypatch):
    project_id = "route-structure-unclear"
    store.init_project(project_id)
    monkeypatch.setattr(
        "src.channels.feishu.intent.classify_route_structure_decision",
        lambda msg: None,
    )
    monkeypatch.setattr(
        "src.orchestrator.run_channel_resume",
        lambda *args, **kwargs: pytest.fail("模糊决策不得恢复checkpoint"),
    )

    result = service._invoke_channel(
        project_id,
        "嗯，就这样吧",
        "route_structure_confirm",
    )
    text, awaiting = service._result_to_message(project_id, "channel", result)

    assert result == {"_route_structure_decision_unclear": True}
    assert "保留原路线" in text and "采纳候选" in text
    assert awaiting == "route_structure_confirm"
    events = store.read_workspace(project_id, "changelog.jsonl")
    assert events[-1]["action"] == "route_structure_decision_reprompt"


# ---------------------------------------------------------------------------
# handle_message：全新一轮（awaiting为None）
# ---------------------------------------------------------------------------


def test_handle_message_new_binding_starts_channel_conversation(monkeypatch):
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"__interrupt__": [_Interrupt({"question": "想去哪里旅游、打算玩几天？", "draft": {}})]},
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划一次旅游", channel)
    _wait_until_idle(bindings.get_binding("chat-1")["active_project_id"])

    # 首次绑定会先发欢迎消息（2026-08-18新增），再发"收到"确认消息
    assert "欢迎使用旅游攻略助手" in channel.sent[0][1]
    assert channel.sent[1][1] == "收到，正在处理，请稍候…"
    assert "想去哪里旅游、打算玩几天？" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] == "requirements"


def test_handle_message_reuses_existing_binding_on_second_message(monkeypatch):
    bindings.get_or_create_binding("chat-1")
    monkeypatch.setattr(
        "src.orchestrator.run_channel_resume",
        lambda project_id, resume_value: {
            "__interrupt__": [_Interrupt({"question": "还有偏好吗？", "draft": {"destination": "成都", "days": 3}})]
        },
    )
    bindings.set_awaiting("chat-1", "requirements")
    channel = _FakeChannel()

    service.handle_message("chat-1", "成都玩3天", channel)
    _wait_until_idle(bindings.get_binding("chat-1")["active_project_id"])

    assert "还有偏好吗？" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] == "requirements"


def test_handle_message_skeleton_confirm_classifies_natural_language_then_resumes(monkeypatch):
    bindings.get_or_create_binding("chat-1")
    bindings.set_awaiting("chat-1", "skeleton_confirm")
    monkeypatch.setattr("src.channels.feishu.intent.classify_skeleton_decision", lambda msg: "confirm")
    captured = {}

    def fake_resume(project_id, resume_value):
        captured["resume_value"] = resume_value
        return {"requirements": {"destination": "成都"}}

    monkeypatch.setattr("src.orchestrator.run_channel_resume", fake_resume)
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.PARALLEL_RESEARCH)
    channel = _FakeChannel()

    service.handle_message("chat-1", "挺好的，就这样吧", channel)
    _wait_until_idle(project_id)

    assert captured["resume_value"] == "confirm"
    assert bindings.get_binding("chat-1")["awaiting"] is None
    assert "当前进度" in channel.edits[-1][1]


def test_handle_message_done_phase_routes_to_companion(monkeypatch):
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    # 陪伴模式现在会先判断一次"是不是想触发素材采集"/"是不是想发起新旅程"，都显式mock成
    # False，避免这个既有测试意外触发真实LLM调用（见下方各自的专属测试）。
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_new_journey", lambda msg: False)
    monkeypatch.setattr(
        "src.orchestrator.run_companion", lambda project_id, msg: {"companion_answer": "今天建议去武侯祠"}
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "今天去哪", channel)
    _wait_until_idle(project_id)

    assert "今天建议去武侯祠" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] is None


def test_handle_message_done_phase_realtime_approval_classifies_then_resumes(monkeypatch):
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "realtime_approval")

    monkeypatch.setattr("src.channels.feishu.intent.classify_realtime_approval_decision", lambda msg: "approve")
    captured = {}

    def fake_resume_companion(project_id, decision):
        captured["decision"] = decision
        return {"companion_answer": "已更新"}

    monkeypatch.setattr("src.orchestrator.resume_companion", fake_resume_companion)
    channel = _FakeChannel()

    service.handle_message("chat-1", "可以，批准", channel)
    _wait_until_idle(project_id)

    assert captured["decision"] == "approve"
    assert "已更新" in channel.edits[-1][1]


# ---------------------------------------------------------------------------
# 陪伴模式接入素材采集意图（2026-07-25新增，见/grilling讨论）：不碰主图，
# 只在companion模式、_invoke_companion内部插入一次独立判断
# ---------------------------------------------------------------------------


def test_companion_material_collection_intent_triggers_background_job_not_run_companion(monkeypatch):
    """识别到"想采集素材"意图时，应该直接调jobs.trigger_collect_material，
    不应该再调orchestrator.run_companion（两者互斥，不能都触发）。"""
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})

    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: True)
    captured = {}

    def fake_trigger(topic, request):
        captured["topic"] = topic
        captured["request"] = request

    monkeypatch.setattr("src.api.jobs.trigger_collect_material", fake_trigger)

    def fail_if_called(project_id, msg):
        raise AssertionError("识别到采集意图时不应该再调run_companion")

    monkeypatch.setattr("src.orchestrator.run_companion", fail_if_called)
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我再搜几条成都最新的攻略", channel)
    _wait_until_idle(project_id)

    assert captured["topic"] == "成都"
    assert captured["request"] == "帮我再搜几条成都最新的攻略"
    assert "成都" in channel.edits[-1][1]
    assert "后台" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] is None


def test_companion_no_material_collection_intent_still_routes_to_run_companion(monkeypatch):
    """没识别到采集意图时，照常走run_companion，新增的判断不应该影响正常陪伴对话
    （回归防护，对称test_handle_message_done_phase_routes_to_companion）。"""
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_new_journey", lambda msg: False)

    def fail_if_called(topic, request):
        raise AssertionError("没有采集意图时不应该触发jobs.trigger_collect_material")

    monkeypatch.setattr("src.api.jobs.trigger_collect_material", fail_if_called)
    monkeypatch.setattr(
        "src.orchestrator.run_companion", lambda project_id, msg: {"companion_answer": "今天建议去武侯祠"}
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "今天去哪", channel)
    _wait_until_idle(project_id)

    assert "今天建议去武侯祠" in channel.edits[-1][1]


def test_companion_material_collection_already_running_shows_friendly_message(monkeypatch):
    """topic已经有一次采集在跑（jobs.trigger_collect_material抛RuntimeError）时，
    应该转成友好提示，不能被_run_turn外层通用的"网络或系统异常"兜底文案盖掉——
    这是"正常的忙碌状态"，不是故障。"""
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})

    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: True)

    def raise_already_running(topic, request):
        raise RuntimeError(f"任务 collect_material:{topic!r} 已经在执行中")

    monkeypatch.setattr("src.api.jobs.trigger_collect_material", raise_already_running)
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我搜点素材", channel)
    _wait_until_idle(project_id)

    assert "已经有一次" in channel.edits[-1][1] or "正在进行中" in channel.edits[-1][1]
    assert "网络或系统异常" not in channel.edits[-1][1]


def test_channel_mode_never_checks_material_collection_intent(monkeypatch):
    """channel模式（project还没"完成"）下，这条新分支完全不应该生效——只在companion
    模式接入，是这次讨论明确定的边界。"""
    calls = []
    monkeypatch.setattr(
        "src.channels.feishu.intent.classify_wants_material_collection",
        lambda msg: calls.append(msg) or False,
    )
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"requirements": {"destination": "成都", "days": 3}},
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划一次旅游", channel)
    _wait_until_idle(bindings.get_binding("chat-1")["active_project_id"])

    assert calls == []


# ---------------------------------------------------------------------------
# 异常处理：区分业务性拒绝 vs 网络/系统异常
# ---------------------------------------------------------------------------


def test_handle_message_value_error_becomes_business_rejection_message(monkeypatch):
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)

    def raise_value_error(project_id, msg):
        raise ValueError("目的地信息不完整")

    monkeypatch.setattr("src.orchestrator.run_channel_start", raise_value_error)
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划", channel)
    _wait_until_idle(project_id)

    assert "这次请求不能处理" in channel.edits[-1][1]
    assert "目的地信息不完整" in channel.edits[-1][1]


def test_handle_message_generic_exception_becomes_network_error_message(monkeypatch):
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)

    def raise_timeout(project_id, msg):
        raise TimeoutError("模拟请求超时")

    monkeypatch.setattr("src.orchestrator.run_channel_start", raise_timeout)
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划", channel)
    _wait_until_idle(project_id)

    assert "网络或系统异常" in channel.edits[-1][1]
    assert "这次请求不能处理" not in channel.edits[-1][1]


# ---------------------------------------------------------------------------
# 并发保护：上一条还在处理时不重复触发
# ---------------------------------------------------------------------------


def test_handle_message_rejects_concurrent_turn_for_same_project(monkeypatch):
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)

    started = threading.Event()
    release = threading.Event()

    def blocking_start(project_id, msg):
        started.set()
        release.wait(timeout=5)
        return {"requirements": {"destination": "成都", "days": 3}}

    monkeypatch.setattr("src.orchestrator.run_channel_start", blocking_start)
    channel_a = _FakeChannel()
    channel_b = _FakeChannel()

    service.handle_message("chat-1", "第一条消息", channel_a)
    started.wait(timeout=5)
    service.handle_message("chat-1", "第二条消息", channel_b)

    assert channel_b.sent[-1][1] == "上一条消息还在处理中，请稍等它完成后再发新消息。"
    release.set()
    _wait_until_idle(project_id)


# ---------------------------------------------------------------------------
# 进度轮询：跑图期间会原地编辑同一条消息
# ---------------------------------------------------------------------------


def test_progress_polling_edits_message_while_turn_is_running(monkeypatch):
    monkeypatch.setattr(service, "FEISHU_PROGRESS_POLL_SECONDS", 0.02)
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.write_run_status(project_id, {"node": "route_draft", "call_count": 1})

    release = threading.Event()

    def slow_start(project_id, msg):
        release.wait(timeout=5)
        return {"requirements": {"destination": "成都", "days": 3}}

    monkeypatch.setattr("src.orchestrator.run_channel_start", slow_start)
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划", channel)
    time.sleep(0.15)
    release.set()
    _wait_until_idle(project_id)

    assert any("正在规划路线" in text for _, text in channel.edits)


def test_completion_message_includes_route_summary_not_just_empty_notice(fake_generate_site):
    """真实联调用户反馈（2026-07-25）：完成时只回一句"生成好了"，用户在群里看不到任何
    实质内容。修复后完成消息应该直接带出天级行程摘要，而不是空话——顺带验证"进度消息
    被原地编辑覆盖"不再等于"信息丢失"：只要最终编辑的内容够实在，历史记录本身就是可读的。
    """
    project_id = "chat-completion-test"
    store.init_project(project_id)
    route_plan = RoutePlan(
        days=[
            RouteDay(day=1, city="成都", area="市区核心", pois=[RoutePoi(name="太古里"), RoutePoi(name="宽窄巷子")]),
        ]
    )
    store.write_route_plan(project_id, "route_agent", route_plan)

    text = service._format_completion_summary(project_id)

    assert "太古里" in text
    assert "宽窄巷子" in text
    assert "Day1" in text
    assert text != "攻略已经生成好了！有什么问题都可以直接问我。"


def test_completion_message_falls_back_when_no_route_plan(fake_generate_site):
    """没有route_plan.json时（理论上不该出现，但兜底）不能报错，退回简单文案。"""
    project_id = "chat-completion-no-route"
    store.init_project(project_id)

    text = service._format_completion_summary(project_id)

    assert "攻略已经生成好了" in text
    assert "这个景点怎么走" in text
    assert "这个景点要注意什么" in text
    assert "介绍一下这个景点" in text
    assert "武侯祠" not in text


def test_completion_message_uses_current_route_poi_in_examples(fake_generate_site):
    project_id = "chat-completion-erhai"
    store.init_project(project_id)
    route_plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="大理",
                area="洱海西岸",
                pois=[RoutePoi(name="洱海生态廊道"), RoutePoi(name="喜洲古镇")],
            ),
        ]
    )
    store.write_route_plan(project_id, "route_agent", route_plan)

    text = service._format_completion_summary(project_id)

    assert "洱海生态廊道怎么走" in text
    assert "洱海生态廊道要注意什么" in text
    assert "介绍一下洱海生态廊道" in text
    assert "武侯祠" not in text


def test_completion_message_reports_force_finalized_degradation(fake_generate_site):
    project_id = "chat-completion-degraded"
    store.init_project(project_id)
    output_path = store.workspace_dir(project_id) / "outputs" / "lifestyle.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "food": {
                    "items": [
                        {
                            "day": 1,
                            "recommendation": "待复核",
                            "citation_status": "force_finalized",
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    text = service._format_completion_summary(project_id)

    assert "completed_with_degradation" in text
    assert "1 个 force_finalized" in text


# ---------------------------------------------------------------------------
# 2026-07-26新增：完成消息带网站链接 + 主动询问海报（真实用户反馈：进度覆盖型看不到
# 过程 + html/海报没有推送）
# ---------------------------------------------------------------------------


def test_completion_message_includes_site_link_when_generate_site_succeeds(fake_generate_site, monkeypatch):
    monkeypatch.setattr(service, "FEISHU_SITE_BASE_URL", "http://127.0.0.1:9000")
    project_id = "chat-completion-site"
    store.init_project(project_id)

    text = service._format_completion_summary(project_id)

    assert f"http://127.0.0.1:9000/api/projects/{project_id}/site" in text
    assert "海报" in text  # 主动询问要不要生成海报也应该出现


def test_completion_message_reports_existing_poster_ready_to_send(fake_generate_site):
    project_id = "chat-completion-existing-poster"
    store.init_project(project_id)
    (store.workspace_dir(project_id) / "poster.png").write_bytes(b"existing-poster")

    text = service._format_completion_summary(project_id)

    assert "海报已准备好，回复「要」即可发送" in text
    assert "我也可以生成攻略海报" not in text


def test_completion_message_skips_site_link_when_generate_site_fails(monkeypatch):
    fake_module = types.ModuleType("scripts.delivery.run_generate_site")

    def raise_error(project_id):
        raise RuntimeError("生成失败")

    fake_module.generate_site = raise_error
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_site", fake_module)
    project_id = "chat-completion-site-fail"
    store.init_project(project_id)

    text = service._format_completion_summary(project_id)

    assert "/site" not in text
    assert "攻略已经生成好了" in text


def test_result_to_message_sets_poster_offer_awaiting_on_completion(monkeypatch):
    monkeypatch.setattr(service, "_format_completion_summary", lambda project_id: "完成摘要")
    project_id = "chat-poster-offer-awaiting"
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    text, new_awaiting = service._result_to_message(project_id, "channel", {})

    assert text == "完成摘要"
    assert new_awaiting == "poster_offer"


def test_handle_message_poster_offer_yes_generates_and_sends_image(fake_generate_poster, monkeypatch):
    fake_module, calls = fake_generate_poster
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "poster_offer")

    monkeypatch.setattr("src.channels.feishu.intent.classify_poster_offer_decision", lambda msg: "yes")
    channel = _FakeChannel()

    service.handle_message("chat-1", "要一张", channel)
    _wait_until_idle(project_id)

    assert calls == [(project_id, False)]
    assert channel.images == ["/tmp/poster.png"]
    assert "海报生成好了" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] is None


def test_handle_message_poster_offer_yes_reuses_existing_image(fake_generate_poster, monkeypatch):
    _fake_module, calls = fake_generate_poster
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    poster_path = store.workspace_dir(project_id) / "poster.png"
    poster_path.write_bytes(b"existing-poster")
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "poster_offer")

    monkeypatch.setattr("src.channels.feishu.intent.classify_poster_offer_decision", lambda msg: "yes")
    channel = _FakeChannel()

    service.handle_message("chat-1", "要一张", channel)
    _wait_until_idle(project_id)

    assert calls == []
    assert channel.images == [str(poster_path)]
    assert "海报已发送" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] is None


def test_handle_message_poster_offer_no_skips_generation(fake_generate_poster, monkeypatch):
    fake_module, calls = fake_generate_poster
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "poster_offer")

    monkeypatch.setattr("src.channels.feishu.intent.classify_poster_offer_decision", lambda msg: "no")
    channel = _FakeChannel()

    service.handle_message("chat-1", "不用了", channel)
    _wait_until_idle(project_id)

    assert calls == []  # 用户拒绝时不应该调用generate_poster
    assert channel.images == []
    assert "不生成海报" in channel.edits[-1][1]


def test_handle_message_poster_offer_generation_failure_becomes_readable_message(fake_generate_poster, monkeypatch):
    fake_module, _calls = fake_generate_poster

    def raise_error(project_id, confirm=True):
        raise RuntimeError("生图API超时")

    fake_module.generate_poster = raise_error

    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "poster_offer")

    monkeypatch.setattr("src.channels.feishu.intent.classify_poster_offer_decision", lambda msg: "yes")
    channel = _FakeChannel()

    service.handle_message("chat-1", "要", channel)
    _wait_until_idle(project_id)

    assert "生图API超时" in channel.edits[-1][1]
    assert channel.images == []


def test_handle_message_poster_offer_classify_exception_clears_awaiting(fake_generate_poster, monkeypatch):
    """2026-08-20真实事故回归测试：classify_poster_offer_decision抛异常（真实场景是
    商汤relay超时/网络异常）不能让awaiting="poster_offer"卡死——否则后续所有消息
    （包括跟海报完全无关的提问）都会被继续送进这个handler重蹈覆辙。修复后异常按
    保守方向当作"no"处理，这一轮turn正常走完并清掉awaiting。"""
    fake_module, calls = fake_generate_poster

    def raise_timeout(msg):
        raise TimeoutError("模拟商汤relay超时")

    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "poster_offer")

    monkeypatch.setattr("src.channels.feishu.intent.classify_poster_offer_decision", raise_timeout)
    channel = _FakeChannel()

    service.handle_message("chat-1", "第二天的行程是怎么安排的？", channel)
    _wait_until_idle(project_id)

    assert calls == []  # 分类失败不应该触发真实收费的生图调用
    assert "不生成海报" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] is None


# ---------------------------------------------------------------------------
# 2026-07-26新增：_ProgressTracker——累积阶段清单，不是"覆盖型"单句进度
# ---------------------------------------------------------------------------


def test_progress_tracker_accumulates_history_across_multiple_nodes():
    project_id = "chat-tracker-history"
    store.init_project(project_id)
    tracker = service._ProgressTracker(project_id)

    tracker.observe({"node": "route_draft", "call_count": 1})
    tracker.observe({"node": "route_draft", "call_count": 1})  # 同一个node重复观察不应该重复记录
    tracker.observe({"node": "lifestyle_agent_node", "call_count": 1})

    assert len(tracker.history) == 2
    text = tracker.render()
    assert "✅ 正在规划路线" in text  # 已经切换到下一个node，上一个应该标记为完成
    assert "▶ 正在整理美食住宿穿搭建议" in text


def test_progress_tracker_marks_retry_when_call_count_increases():
    project_id = "chat-tracker-retry"
    store.init_project(project_id)
    tracker = service._ProgressTracker(project_id)

    tracker.observe({"node": "lifestyle_agent_node", "call_count": 1})
    tracker.observe({"node": "citation_check_node", "call_count": 1})
    tracker.observe({"node": "lifestyle_agent_node", "call_count": 2})

    assert len(tracker.history) == 3
    text = tracker.render()
    assert "第2次" in text


def test_progress_tracker_render_waiting_shows_pause_icon_instead_of_running():
    project_id = "chat-tracker-waiting"
    store.init_project(project_id)
    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "await_skeleton_confirm", "call_count": 1})

    text = tracker.render(waiting=True, trailing="骨架已生成，是否确认？")

    assert "⏸ 等待你确认" in text
    assert "骨架已生成，是否确认？" in text


def test_progress_tracker_render_includes_destination_header_when_requirements_saved():
    project_id = "chat-tracker-header"
    store.init_project(project_id)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})
    tracker = service._ProgressTracker(project_id)

    text = tracker.render()

    assert "📍 成都 · 3天" in text


def test_progress_tracker_ignores_stale_run_status_left_over_from_earlier_turn():
    """真实bug复现（2026-07-26，飞书真实联调）：run_status.json是per-project的单一快照，
    不是per-turn的。project昨天已经完成整趟生成（最后一个节点是poi_details_join_node），
    今天用户在陪伴阶段问了一句完全无关的话，_run_turn给这次新的turn创建了一个全新的
    _ProgressTracker——它的第一次observe()读到的是run_status.json里那份昨天遗留的旧快照
    （status="done"，started_at是昨天），如果不做任何时间校验，会被误当成"这个节点现在
    正在跑"记进history，用户会在完全不相关的一次陪伴阶段问答里看到"正在做最后整理（进行中，
    1m16s）"这种驴唇不对马嘴的进度提示（这条elapsed还是当场用错误的"重新起算"时间算出来的，
    跟昨天真实的耗时毫无关系）。"""
    project_id = "chat-tracker-stale-status"
    store.init_project(project_id)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    tracker = service._ProgressTracker(project_id)  # tracker创建时间晚于stale status

    tracker.observe({
        "node": "poi_details_join_node",
        "status": "done",
        "call_count": 1,
        "started_at": yesterday.isoformat(),
        "finished_at": yesterday.isoformat(),
    })

    assert tracker.history == []  # 昨天遗留的快照不应该被当成本轮新条目

    # 真正属于本轮、started_at晚于tracker创建时间的观察，应该正常记录
    tracker.observe({
        "node": "companion_lifestyle_query_node",
        "status": "running",
        "call_count": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    })
    assert len(tracker.history) == 1
    assert tracker.history[0]["node"] == "companion_lifestyle_query_node"


# ---------------------------------------------------------------------------
# _detect_retry_trouble / render()健康检测（2026-08-18新增，飞书进度可见性改进）：
# 真实撞过route_draft卡在cun渠道504网关超时循环40分钟、进度提示始终是同一行没有
# 任何变化的情况，见docs/ISSUES.md。trace.jsonl与Workspace现在共用可配置的
# PROJECTS_DIR；各测试仍切换cwd，以证明读取不再依赖真实仓库目录。
# ---------------------------------------------------------------------------


def test_detect_retry_trouble_returns_none_when_trace_file_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert service._detect_retry_trouble("p1", "route_draft") is None


def test_detect_retry_trouble_returns_none_for_unmapped_node(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert service._detect_retry_trouble("p1", "await_skeleton_confirm") is None


def test_detect_retry_trouble_returns_none_when_recent_calls_succeed(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    llm_trace.set_trace_context("p1", "route_agent")
    for _ in range(3):
        llm_trace.append_trace({"ok": True, "group": "cunai"})

    assert service._detect_retry_trouble("p1", "route_draft") is None


def test_detect_retry_trouble_returns_none_with_fewer_than_threshold_failures(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai"})
    llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError", "status_code": 504})
    llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError", "status_code": 504})

    assert service._detect_retry_trouble("p1", "route_draft") is None


def test_detect_retry_trouble_flags_consecutive_504_failures(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    llm_trace.set_trace_context("p1", "route_agent")
    for _ in range(3):
        llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError", "status_code": 504})

    result = service._detect_retry_trouble("p1", "route_draft")
    assert result is not None
    assert "3次" in result
    assert "网关超时" in result


def test_detect_retry_trouble_flags_consecutive_rate_limit_failures():
    from src import llm_trace

    llm_trace.set_trace_context("p1", "lifestyle_agent")
    for _ in range(3):
        llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "RateLimitError", "status_code": 429})

    result = service._detect_retry_trouble("p1", "lifestyle_agent_node")
    assert result is not None
    assert "限流" in result


def test_progress_tracker_render_includes_retry_trouble_for_in_progress_node(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-retry-trouble"
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "route_agent")
    for _ in range(3):
        llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError", "status_code": 504})

    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "route_draft", "call_count": 1})

    text = tracker.render()
    assert "▶ 正在规划路线" in text
    assert "网关超时" in text


def test_progress_tracker_render_does_not_show_trouble_for_completed_node(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-retry-trouble-completed"
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "route_agent")
    for _ in range(3):
        llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError", "status_code": 504})

    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "route_draft", "call_count": 1})
    tracker.observe({"node": "lifestyle_agent_node", "call_count": 1})  # route_draft已完成，切到下一个node

    text = tracker.render()
    assert "✅ 正在规划路线" in text
    assert "网关超时" not in text  # 已完成的历史条目不做健康检测


# ---------------------------------------------------------------------------
# _detect_current_step / render()当前步骤展示（2026-08-18新增，第二期进度可见性）：
# 真实反馈"正常运行时也应该看到步骤，不然是透明的"——第一期只解决了故障提示，这次
# 补上正常运行时的步骤级可见性。
# ---------------------------------------------------------------------------


def test_detect_current_step_returns_none_when_trace_file_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert service._detect_current_step("p1", "route_draft") is None


def test_detect_current_step_returns_none_for_unmapped_node(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert service._detect_current_step("p1", "await_skeleton_confirm") is None


def test_detect_current_step_returns_none_when_last_call_failed(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError"})

    assert service._detect_current_step("p1", "route_draft") is None


def test_detect_current_step_returns_none_when_no_tool_calls(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai", "tool_calls": None})

    assert service._detect_current_step("p1", "route_draft") is None


def test_detect_current_step_returns_friendly_labels_deduplicated(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({
        "ok": True,
        "group": "cunai",
        "tool_calls": ["retrieve_materials", "retrieve_materials", "amap_driving_distance"],
    })

    step = service._detect_current_step("p1", "route_draft")
    assert step == "检索素材库、核实驾车距离"


def test_detect_current_step_returns_unmapped_tool_name_as_is(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai", "tool_calls": ["some_new_tool"]})

    step = service._detect_current_step("p1", "route_draft")
    assert step == "some_new_tool"


def test_detect_current_step_renders_read_workspace_with_friendly_label(monkeypatch, tmp_path):
    """2026-08-21新增：read_workspace 此前没收录进 _TOOL_FRIENDLY_LABELS，guide_agent
    读 final_guide.md 时后缀会兜底显示技术名“正在read_workspace”。收录后应该显示
    “读取已生成内容”。锁定这个映射防止以后悄悄回退（陪伴阶段进度可见性的核心诉求：
    用户要能看出“在读已生成内容”而不是“在read_workspace”）。"""
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-read-workspace"
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "guide_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai", "tool_calls": ["read_workspace"]})

    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "guide_agent_node", "call_count": 1})

    text = tracker.render()
    assert "读取已生成内容" in text
    assert "read_workspace" not in text  # 技术名不能泄漏给最终用户


def test_detect_current_step_renders_companion_lifestyle_tools(monkeypatch, tmp_path):
    """2026-08-21新增，2026-08-21补充拆分（docs/ISSUES.md第142条）：
    companion_lifestyle_query_node 调 retrieve_materials（检索素材库）时，配合
    node 友好名"正在补充查询美食住宿"，用户能同时看到"是哪个 agent 在跑"
    +"在用什么数据源"。锁定这条映射。"""
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-companion-lifestyle-tools"
    store.init_project(project_id)
    # companion_lifestyle_query_node set_trace_context("companion_lifestyle")，
    # 跟companion_pitfall_query_node的trace目录互相独立（第142条修复前两者共用
    # "companion_query"，无法区分是哪个在查）。
    llm_trace.set_trace_context(project_id, "companion_lifestyle")
    llm_trace.append_trace({"ok": True, "group": "cunai", "tool_calls": ["retrieve_materials"]})

    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "companion_lifestyle_query_node", "call_count": 1})

    text = tracker.render()
    assert "正在补充查询美食住宿" in text  # 带身份的 node 名
    assert "检索素材库" in text  # 数据源后缀


def test_detect_current_step_renders_companion_pitfall_tools(monkeypatch, tmp_path):
    """2026-08-21新增（docs/ISSUES.md第142条）：companion_pitfall_query_node调
    retrieve_materials时同样能看到带身份的node名+数据源后缀，且它的trace落在独立的
    "companion_pitfall"目录里——不会被companion_lifestyle_query_node的trace记录污染
    （拆分前两者共用"companion_query"目录，无法区分是哪个agent在查）。"""
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-companion-pitfall-tools"
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "companion_pitfall")
    llm_trace.append_trace({"ok": True, "group": "cunai", "tool_calls": ["retrieve_materials"]})

    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "companion_pitfall_query_node", "call_count": 1})

    text = tracker.render()
    assert "正在补充查询避雷信息" in text  # 带身份的 node 名
    assert "检索素材库" in text  # 数据源后缀


def test_companion_lifestyle_and_pitfall_trace_do_not_cross_contaminate(monkeypatch, tmp_path):
    """docs/ISSUES.md第142条核心诉求：拆分trace目录后，lifestyle节点执行期间读不到
    pitfall那次工具调用的trace记录（反之亦然）——两者是独立的agent身份，不再共用
    "companion_query"这一个目录。"""
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-companion-no-cross-contam"
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "companion_pitfall")
    llm_trace.append_trace({"ok": True, "group": "cunai", "tool_calls": ["retrieve_materials"]})

    # 只观察lifestyle节点——它的trace目录("companion_lifestyle")里没有任何记录，
    # 不应该读到pitfall那次工具调用留下的痕迹。
    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "companion_lifestyle_query_node", "call_count": 1})

    text = tracker.render()
    assert "正在补充查询美食住宿" in text
    assert "检索素材库" not in text  # 不应该串到pitfall那次的trace记录


def test_progress_tracker_render_includes_current_step_for_in_progress_node(monkeypatch, tmp_path):
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-current-step"
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "route_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai", "tool_calls": ["retrieve_materials"]})

    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "route_draft", "call_count": 1})

    text = tracker.render()
    assert "▶ 正在规划路线" in text
    assert "正在检索素材库" in text


def test_progress_tracker_render_prefers_trouble_over_current_step(monkeypatch, tmp_path):
    """故障提示优先级更高——最近一条如果是失败记录，_detect_retry_trouble需要看最近
    N条是否连续失败；这里构造"最近3条全部失败"，_detect_current_step自身也会因为
    最后一条ok!=True而返回None，两者天然互斥，不会同时出现在同一行。"""
    from src import llm_trace

    monkeypatch.chdir(tmp_path)
    project_id = "chat-tracker-trouble-priority"
    store.init_project(project_id)
    llm_trace.set_trace_context(project_id, "route_agent")
    for _ in range(3):
        llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError", "status_code": 504})

    tracker = service._ProgressTracker(project_id)
    tracker.observe({"node": "route_draft", "call_count": 1})

    text = tracker.render()
    assert "网关超时" in text
    assert "正在检索" not in text
    assert " · 正在" not in text  # current_step的内联后缀格式不应该出现


# ---------------------------------------------------------------------------
# 一个群多次旅程（2026-07-26新增，见docs/ARCHITECTURE.md第14.4节）：意图检测→
# 二段式确认→切换project→复用触发消息
# ---------------------------------------------------------------------------


def test_companion_new_journey_intent_triggers_offer_confirmation(monkeypatch):
    """识别到"想发起新旅程"意图时，不直接切换，只是反问一句确认；不应该调用
    run_companion（这条分支跟正常陪伴问答互斥）。"""
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_new_journey", lambda msg: True)

    def fail_if_called(project_id, msg):
        raise AssertionError("识别到新旅程意图时不应该再调run_companion")

    monkeypatch.setattr("src.orchestrator.run_companion", fail_if_called)
    channel = _FakeChannel()

    service.handle_message("chat-1", "我想去大理玩3天", channel)
    _wait_until_idle(project_id)

    assert "新的旅程" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] == "new_journey_offer"
    assert bindings.get_binding("chat-1")["pending_new_journey_message"] == "我想去大理玩3天"
    # 还没真正切换——active_project_id保持原样
    assert bindings.get_binding("chat-1")["active_project_id"] == project_id


def test_companion_no_new_journey_intent_still_routes_to_run_companion(monkeypatch):
    """没识别到新旅程意图时，照常走run_companion，新增的判断不应该影响正常陪伴对话
    （回归防护，对称素材采集分支的同款测试）。"""
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_new_journey", lambda msg: False)
    monkeypatch.setattr(
        "src.orchestrator.run_companion", lambda project_id, msg: {"companion_answer": "今天建议去武侯祠"}
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "大理好玩吗", channel)
    _wait_until_idle(project_id)

    assert "今天建议去武侯祠" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] is None


def test_new_journey_offer_confirmed_switches_project_and_reuses_pending_message(monkeypatch):
    """用户确认"要"之后，应该真正切换active_project_id，并把之前暂存的触发消息
    直接喂给新project的run_channel_start（不逼用户复述）。"""
    binding = bindings.get_or_create_binding("chat-1")
    old_project_id = binding["active_project_id"]
    store.init_project(old_project_id)
    store.update_progress(old_project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "new_journey_offer")
    bindings.set_pending_new_journey_message("chat-1", "我想去大理玩3天")

    monkeypatch.setattr("src.channels.feishu.intent.classify_new_journey_offer_decision", lambda msg: "yes")
    captured = {}

    def fake_run_channel_start(project_id, msg):
        captured["project_id"] = project_id
        captured["msg"] = msg
        return {"__interrupt__": [_Interrupt({"question": "还有其它偏好吗？", "draft": {}})]}

    monkeypatch.setattr("src.orchestrator.run_channel_start", fake_run_channel_start)
    channel = _FakeChannel()

    service.handle_message("chat-1", "要", channel)
    _wait_until_idle(old_project_id)

    new_project_id = bindings.get_binding("chat-1")["active_project_id"]
    assert new_project_id != old_project_id
    assert captured["project_id"] == new_project_id
    assert captured["msg"] == "我想去大理玩3天"
    assert "还有其它偏好吗？" in channel.edits[-1][1]
    assert bindings.get_binding("chat-1")["awaiting"] == "requirements"
    assert bindings.get_binding("chat-1")["history"] == [old_project_id, new_project_id]


def test_new_journey_offer_rejected_keeps_current_project(monkeypatch):
    """用户拒绝时不应该切换project，暂存的消息也应该被清空（不能残留到下一轮）。"""
    binding = bindings.get_or_create_binding("chat-1")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    bindings.set_awaiting("chat-1", "new_journey_offer")
    bindings.set_pending_new_journey_message("chat-1", "我想去大理玩3天")

    monkeypatch.setattr("src.channels.feishu.intent.classify_new_journey_offer_decision", lambda msg: "no")

    def fail_if_called(project_id, msg):
        raise AssertionError("用户拒绝时不应该调用run_channel_start")

    monkeypatch.setattr("src.orchestrator.run_channel_start", fail_if_called)
    channel = _FakeChannel()

    service.handle_message("chat-1", "不用了", channel)
    _wait_until_idle(project_id)

    assert "继续聊现在这趟旅程" in channel.edits[-1][1]
    binding = bindings.get_binding("chat-1")
    assert binding["active_project_id"] == project_id
    assert binding["awaiting"] is None
    assert binding["pending_new_journey_message"] is None


# ---------------------------------------------------------------------------
# 飞书写入用户备注（2026-08-21新增，docs/ARCHITECTURE.md第21节阶段2）：意图命中→
# 单轮fire-and-forget抽{day,poi,note}→write_user_note(source=feishu)+重建site，
# 不进反问状态机、不调run_companion（跟material_collection分支同款互斥语义）
# ---------------------------------------------------------------------------


def _done_project(chat_id="chat-1"):
    """helper：建一个DONE状态的project供陪伴模式分支测试。"""
    binding = bindings.get_or_create_binding(chat_id)
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)
    store.write_requirements(project_id, "成都", {"destination": "成都", "days": 3})
    return project_id


def _write_sample_route_plan(project_id, monkeypatch):
    """helper：给project写一个Day1/Day2的真实route_plan，供_extract_user_note_fields
    的route_plan上下文摘要读取（不是每个user_note测试都需要，但抽取路径需要真实POI名
    才能验证poi_name校验逻辑）。"""
    route_plan = RoutePlan(
        days=[
            RouteDay(day=1, city="成都", area="市区", pois=[RoutePoi(name="武侯祠"), RoutePoi(name="宽窄巷子")]),
            RouteDay(day=2, city="成都", area="市区", pois=[RoutePoi(name="太古里")]),
        ]
    )
    store.write_route_plan(project_id, "route_agent", route_plan)
    return route_plan


def test_companion_user_note_intent_short_circuits_run_companion(fake_generate_site, monkeypatch):
    """识别到"想记一条备注"意图时，应该直接走_handle_user_note，不应该再调
    orchestrator.run_companion（两者互斥，跟material_collection分支同款语义）。"""
    project_id = _done_project()
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: True)
    # 抽取用一个固定fake：喂回真实抽取的等效结构，避免跑真实LLM
    monkeypatch.setattr(
        "src.channels.feishu.service._extract_user_note_fields",
        lambda pid, msg: {"day": 2, "poi_name": "宽窄巷子", "note": "这家火锅鱼要提前订"},
    )

    def fail_if_called(project_id, msg):
        raise AssertionError("识别到记备注意图时不应该再调run_companion")

    monkeypatch.setattr("src.orchestrator.run_companion", fail_if_called)
    channel = _FakeChannel()

    service.handle_message("chat-1", "记一下Day2宽窄巷子这家火锅鱼要提前订", channel)
    _wait_until_idle(project_id)

    notes = store.list_user_notes(project_id)
    assert len(notes) == 1
    assert notes[0].source == "feishu"
    assert notes[0].day == 2
    assert notes[0].poi_name == "宽窄巷子"
    assert notes[0].note_text == "这家火锅鱼要提前订"
    reply = channel.edits[-1][1]
    assert "已记下" in reply
    assert "宽窄巷子" in reply
    # 不进反问状态机
    assert bindings.get_binding("chat-1")["awaiting"] is None


def test_companion_no_user_note_intent_still_routes_to_run_companion(monkeypatch):
    """没识别到记备注意图时，照常走run_companion，新增的判断不应该影响正常陪伴对话
    （回归防护，对称素材采集/新旅程分支的同款测试）。"""
    project_id = _done_project()
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_new_journey", lambda msg: False)

    def fail_if_extract(pid, msg):
        raise AssertionError("没识别到记备注意图时不应该走抽取")

    monkeypatch.setattr("src.channels.feishu.service._extract_user_note_fields", fail_if_extract)
    monkeypatch.setattr(
        "src.orchestrator.run_companion", lambda project_id, msg: {"companion_answer": "今天建议去武侯祠"}
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "武侯祠几点开门", channel)
    _wait_until_idle(project_id)

    assert "今天建议去武侯祠" in channel.edits[-1][1]


def test_handle_user_note_extraction_returns_none_prompts_reformat(monkeypatch):
    """抽取彻底失败（JSON都解析不出，_extract_user_note_fields返回None）时，不写、
    不报错，返回"按格式重说"——单轮fire-and-forget，不进状态机。"""
    project_id = _done_project()
    monkeypatch.setattr("src.channels.feishu.service._extract_user_note_fields", lambda pid, msg: None)
    result = service._handle_user_note(project_id, "记一下")
    assert result["companion_answer"].startswith("没听明白")
    assert "格式" in result["companion_answer"]
    # 不应该写任何备注
    assert store.list_user_notes(project_id) == []


def test_handle_user_note_empty_note_text_prompts_reformat(monkeypatch):
    """抽取回来了但note字段为空（用户只说了"记一下"没说记什么）时，同样不写、
    返回"按格式重说"。"""
    project_id = _done_project()
    monkeypatch.setattr(
        "src.channels.feishu.service._extract_user_note_fields",
        lambda pid, msg: {"day": None, "poi_name": None, "note": ""},
    )
    result = service._handle_user_note(project_id, "记一下")
    assert "没听明白" in result["companion_answer"]
    assert store.list_user_notes(project_id) == []


def test_handle_user_note_trip_level_when_day_and_poi_both_null(monkeypatch):
    """day和poi都抽不出时，应该写一条行程级备注（day=None, poi=""），不报错、
    不拒绝——这是设计允许的（docs/ARCHITECTURE.md第21.2节"都不填=行程级"）。"""
    project_id = _done_project(fake_generate_site=None) if False else _done_project()
    monkeypatch.setattr(
        "src.channels.feishu.service._extract_user_note_fields",
        lambda pid, msg: {"day": None, "poi_name": None, "note": "整体节奏偏紧，每天留弹性时间"},
    )
    result = service._handle_user_note(project_id, "记一下整体节奏偏紧")
    assert "已记下" in result["companion_answer"]
    notes = store.list_user_notes(project_id)
    assert len(notes) == 1
    assert notes[0].day is None
    assert notes[0].poi_name == ""
    assert notes[0].source == "feishu"


def test_handle_user_note_invalid_poi_name_degraded_to_null(fake_generate_site, monkeypatch):
    """模型抽出的poi_name不在route_plan真实景点里时，_extract_user_note_fields
    应该把它降级成None（防编造景点名→写成孤儿备注），写成天级备注。"""
    project_id = _done_project()
    _write_sample_route_plan(project_id, monkeypatch)
    monkeypatch.setattr(
        service,
        "get_light_chat_model",
        lambda temperature=0: _FakeStructured({"day": 2, "poi_name": "编造的景点", "note": "要提前订"}),
    )
    fields = service._extract_user_note_fields(project_id, "记一下编造的景点要提前订")
    assert fields is not None
    assert fields["poi_name"] is None  # 被降级
    assert fields["day"] == 2
    assert fields["note"] == "要提前订"


def test_handle_user_note_valid_poi_name_kept(fake_generate_site, monkeypatch):
    """poi_name在route_plan真实景点里时，原样保留——验证降级逻辑不会误伤真实命中。"""
    project_id = _done_project()
    _write_sample_route_plan(project_id, monkeypatch)
    monkeypatch.setattr(
        service,
        "get_light_chat_model",
        lambda temperature=0: _FakeStructured({"day": 1, "poi_name": "武侯祠", "note": "留够2小时"}),
    )
    fields = service._extract_user_note_fields(project_id, "记一下武侯祠留够2小时")
    assert fields["poi_name"] == "武侯祠"
    assert fields["day"] == 1


def test_extract_user_note_fields_returns_none_on_invoke_exception(monkeypatch):
    """with_structured_output.invoke()抛异常时（真实场景：relay返回非JSON/超时），
    _extract_user_note_fields应该吞掉异常返回None，不冒泡打断陪伴对话。"""
    project_id = _done_project()

    class _ExplodingStructured:
        def invoke(self, prompt):
            raise RuntimeError("relay返回了非JSON内容")

        def with_structured_output(self, *a, **kw):
            return self

    monkeypatch.setattr(service, "get_light_chat_model", lambda temperature=0: _ExplodingStructured())
    fields = service._extract_user_note_fields(project_id, "记一下Day1要穿舒适鞋")
    assert fields is None


def test_handle_user_note_missing_project_returns_friendly_message(monkeypatch):
    """project不存在（read_requirements返回None）时，不写、不调抽取，返回友好提示。"""
    monkeypatch.setattr(store, "read_requirements", lambda pid: None)
    result = service._handle_user_note("nonexistent-project", "记一下啥")
    assert "找不到" in result["companion_answer"]
    assert store.list_user_notes("nonexistent-project") == []


def test_handle_user_note_rebuilds_site_after_write(fake_generate_site, monkeypatch):
    """写入备注后应该触发重建site（让备注在网页可见）——用fake_generate_site
    fixture捕获调用，跟完成消息重建site同款fake模块手法。"""
    fake_module, calls = fake_generate_site
    project_id = _done_project()
    monkeypatch.setattr(
        "src.channels.feishu.service._extract_user_note_fields",
        lambda pid, msg: {"day": 1, "poi_name": "武侯祠", "note": "留够2小时"},
    )
    service._handle_user_note(project_id, "记一下武侯祠留够2小时")
    assert project_id in calls  # generate_site被调用了


def test_handle_user_note_rebuild_failure_does_not_block_reply(monkeypatch):
    """重建site失败时，"已记下"这条回复仍然要正常发出来（跟完成消息里重建失败
    不阻塞同款态度），只打日志。"""
    fake_module = types.ModuleType("scripts.delivery.run_generate_site")

    def raise_error(project_id):
        raise RuntimeError("生成失败")

    fake_module.generate_site = raise_error
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_site", fake_module)
    project_id = _done_project()
    monkeypatch.setattr(
        "src.channels.feishu.service._extract_user_note_fields",
        lambda pid, msg: {"day": 1, "poi_name": None, "note": "要穿舒适鞋"},
    )
    result = service._handle_user_note(project_id, "记一下Day1要穿舒适鞋")
    # 备注写成功了，回复也正常发出，重建失败被吞掉
    assert "已记下" in result["companion_answer"]
    assert len(store.list_user_notes(project_id)) == 1


def test_channel_mode_never_checks_user_note_intent(monkeypatch):
    """channel模式（project还没"完成"）下，user_note这条新分支完全不应该生效——
    只在companion模式接入，跟material_collection/new_journey同款边界。"""
    calls = []
    monkeypatch.setattr(
        "src.channels.feishu.intent.classify_wants_material_collection",
        lambda msg: calls.append(("material", msg)) or False,
    )
    monkeypatch.setattr(
        "src.channels.feishu.intent.classify_wants_user_note",
        lambda msg: calls.append(("user_note", msg)) or False,
    )
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"requirements": {"destination": "成都", "days": 3}},
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划一次旅游", channel)
    _wait_until_idle(bindings.get_binding("chat-1")["active_project_id"])

    assert calls == []


# ---------------------------------------------------------------------------
# 推送成功/失败日志（2026-08-01新增，补上此前"仍是占位未做"的缺口）
# ---------------------------------------------------------------------------


def test_run_turn_logs_successful_send_to_push_log(monkeypatch):
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"__interrupt__": [_Interrupt({"question": "想去哪里？", "draft": {}})]},
    )
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划一次旅游", channel)
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    _wait_until_idle(project_id)

    entries = push_log.read_push_log()
    assert len(entries) >= 2  # 至少"收到，正在处理"的ack + 最终结果两条
    assert all(e["success"] is True for e in entries)
    assert all(e["chat_id"] == "chat-1" for e in entries)
    assert any(e["project_id"] == project_id for e in entries)


def test_run_turn_business_error_still_logs_a_successful_push(monkeypatch):
    """业务异常（网络/系统错误）被_run_turn转成一句中文提示后，那句提示本身通过
    channel.edit()正常发送成功——这是一次成功的推送（推送的内容是错误提示），
    跟"推送这个动作本身失败"是两个不同的概念，不应该混为一谈。核心断言：日志记录是
    纯粹的旁路副作用，不能改变现有的"网络异常转中文提示"这条用户可见的错误处理流程。"""

    def raise_timeout(project_id, msg):
        raise TimeoutError("模拟请求超时")

    monkeypatch.setattr("src.orchestrator.run_channel_start", raise_timeout)
    channel = _FakeChannel()

    service.handle_message("chat-1", "帮我规划", channel)
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    _wait_until_idle(project_id)

    assert "网络或系统异常" in channel.edits[-1][1]  # 既有行为不受影响

    entries = push_log.read_push_log()
    assert len(entries) >= 1
    # 推送本身（把这句错误提示发出去）是成功的，即便提示内容讲的是一次业务失败
    assert any(e["push_type"] == "error" and e["success"] is True for e in entries)


def test_run_turn_logs_failure_when_channel_edit_itself_raises(monkeypatch):
    """真正的推送失败——channel.edit()本身抛异常（比如飞书API报错/网络中断），
    这才是本条日志机制真正要捕捉的场景。"""
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"__interrupt__": [_Interrupt({"question": "想去哪里？", "draft": {}})]},
    )

    class _FailingEditChannel(_FakeChannel):
        def edit(self, message_id, text):
            raise RuntimeError("模拟飞书API报错")

    channel = _FailingEditChannel()

    service.handle_message("chat-1", "帮我规划一次旅游", channel)
    project_id = bindings.get_binding("chat-1")["active_project_id"]
    _wait_until_idle(project_id)

    entries = push_log.read_push_log()
    failed_entries = [e for e in entries if not e["success"]]
    assert len(failed_entries) >= 1
    assert "模拟飞书API报错" in failed_entries[0]["error"]


def test_handle_message_binding_failure_logs_with_null_project_id(monkeypatch):
    """绑定失败发生在project_id被解析出来之前，这条日志的project_id应该是null，
    而不是被跳过不记——这里的send本身是成功的（发出了一句"绑定失败"的提示），
    只是project_id还没解析出来。"""
    monkeypatch.setattr(
        bindings, "get_or_create_binding", lambda chat_id: (_ for _ in ()).throw(RuntimeError("绑定损坏"))
    )
    channel = _FakeChannel()

    service.handle_message("chat-broken", "你好", channel)

    entries = push_log.read_push_log()
    assert len(entries) == 1
    assert entries[0]["project_id"] is None
    assert entries[0]["success"] is True
    assert "绑定损坏" in channel.sent[0][1]


def test_handle_message_auto_starts_new_journey_when_active_project_id_is_none(monkeypatch):
    """真实飞书群测试中发现的bug（2026-08-18）：管理后台软删除project后
    （bindings.unbind_project），active_project_id变成None，此前handle_message
    没有做任何保护，直接把None传进store.read_progress，最终报出
    "unsupported operand type(s) for /: 'PosixPath' and 'NoneType'"这类对用户
    完全不可读的技术性错误。修复后应该自动调用start_new_journey开启一次新旅程，
    而不是让用户卡在一个无法恢复的错误提示里。"""
    binding = bindings.get_or_create_binding("chat-orphaned")
    bindings.clear_new_user_flag("chat-orphaned")
    unbound = bindings.unbind_project(binding["active_project_id"])
    assert unbound == ["chat-orphaned"]
    assert bindings.get_binding("chat-orphaned")["active_project_id"] is None

    captured_project_ids = []
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: (
            captured_project_ids.append(project_id),
            {"__interrupt__": [_Interrupt({"question": "想去哪里旅游、打算玩几天？", "draft": {}})]},
        )[1],
    )
    channel = _FakeChannel()

    service.handle_message("chat-orphaned", "三天成都旅游", channel)

    new_project_id = bindings.get_binding("chat-orphaned")["active_project_id"]
    _wait_until_idle(new_project_id)

    assert new_project_id is not None
    assert new_project_id != binding["active_project_id"]
    assert captured_project_ids == [new_project_id]
    assert not any("unsupported operand type" in text for _, text in channel.sent + channel.edits)
    assert "想去哪里旅游、打算玩几天？" in channel.edits[-1][1]


class _Interrupt:
    """最小化模拟LangGraph真实interrupt对象——真实类型是langgraph.types.Interrupt，
    这里只需要一个有.value属性的对象，不引入真实图执行。"""

    def __init__(self, value: dict):
        self.value = value


# ---------------------------------------------------------------------------
# 2026-08-18新增：新手引导、帮助命令、Agent标识的测试
# ---------------------------------------------------------------------------


def test_new_user_welcome_message_sent_on_first_binding(monkeypatch):
    """方案A：首次创建绑定时发送欢迎消息"""
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"__interrupt__": [_Interrupt({"question": "想去哪里旅游？", "draft": {}})]},
    )
    channel = _FakeChannel()

    service.handle_message("chat-new-user", "你好", channel)
    project_id = bindings.get_binding("chat-new-user")["active_project_id"]
    _wait_until_idle(project_id)

    # 应该发送了两条消息：欢迎消息 + 正常回复
    assert len(channel.sent) >= 2
    welcome_msg = channel.sent[0][1]
    assert "欢迎使用旅游攻略助手" in welcome_msg
    assert "规划多天旅游行程" in welcome_msg
    assert "三天成都旅游" in welcome_msg
    assert "帮助" in welcome_msg


def test_new_user_flag_removed_after_welcome(monkeypatch):
    """欢迎消息发送后，_is_new_user标志应该被移除"""
    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"__interrupt__": [_Interrupt({"question": "想去哪里？", "draft": {}})]},
    )
    channel = _FakeChannel()

    service.handle_message("chat-flag-test", "你好", channel)
    project_id = bindings.get_binding("chat-flag-test")["active_project_id"]
    _wait_until_idle(project_id)

    # 再次获取binding，_is_new_user应该不存在了
    binding = bindings.get_binding("chat-flag-test")
    assert "_is_new_user" not in binding


def test_existing_user_no_welcome_message(monkeypatch):
    """已有绑定的用户不应该收到欢迎消息"""
    # 先创建绑定，然后清除新用户标志（模拟已经发过欢迎消息的老用户）
    bindings.get_or_create_binding("chat-existing")
    bindings.clear_new_user_flag("chat-existing")

    monkeypatch.setattr(
        "src.orchestrator.run_channel_start",
        lambda project_id, msg: {"__interrupt__": [_Interrupt({"question": "想去哪里？", "draft": {}})]},
    )
    channel = _FakeChannel()

    service.handle_message("chat-existing", "你好", channel)
    project_id = bindings.get_binding("chat-existing")["active_project_id"]
    _wait_until_idle(project_id)

    # 不应该有欢迎消息
    for msg_id, text in channel.sent:
        assert "欢迎使用旅游攻略助手" not in text


def test_help_command_triggers_with_multiple_keywords(monkeypatch):
    """帮助命令应该响应多种触发词"""
    binding = bindings.get_or_create_binding("chat-help")
    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    monkeypatch.setattr(
        "src.orchestrator.run_companion",
        lambda project_id, msg: {"companion_answer": "不应该出现的答案"},
    )

    # 测试多种触发词
    for keyword in ["帮助", "help", "菜单", "?", "？", "/help"]:
        channel_test = _FakeChannel()
        service.handle_message("chat-help", keyword, channel_test)
        _wait_until_idle(project_id)

        # 应该返回帮助消息（可能在sent或edits中）
        all_messages = [msg[1] for msg in channel_test.sent] + [msg[1] for msg in channel_test.edits]
        help_msg = "\n".join(all_messages)

        assert "完整功能列表" in help_msg
        assert "查询攻略" in help_msg
        assert "避坑建议" in help_msg
        assert "提醒管理" in help_msg


def test_newbie_question_triggers_guidance(monkeypatch):
    """方案C：检测新手问题时主动给出引导"""
    binding = bindings.get_or_create_binding("chat-newbie")
    # 清除新用户标志，避免触发欢迎消息
    bindings.clear_new_user_flag("chat-newbie")

    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    monkeypatch.setattr(
        "src.orchestrator.run_companion",
        lambda project_id, msg: {"companion_answer": "不应该出现的答案"},
    )

    # 测试新手问题触发
    for question in ["你能做什么", "有什么功能", "能帮我什么", "会啥", "功能"]:
        channel_test = _FakeChannel()
        service.handle_message("chat-newbie", question, channel_test)
        _wait_until_idle(project_id)

        # 检查所有消息（sent + edits）
        all_messages = [msg[1] for msg in channel_test.sent] + [msg[1] for msg in channel_test.edits]
        guidance_msg = "\n".join(all_messages)

        assert "很高兴为你服务" in guidance_msg
        assert "规划行程" in guidance_msg
        assert "避坑建议" in guidance_msg
        assert "帮助" in guidance_msg


def test_agent_info_displayed_when_enabled(monkeypatch):
    """Agent标识应该在回答开头显示（FEISHU_SHOW_AGENT_INFO=true）"""
    binding = bindings.get_or_create_binding("chat-agent-info")
    # 清除新用户标志，避免触发欢迎消息
    bindings.clear_new_user_flag("chat-agent-info")

    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    # mock掉素材采集/新旅程意图检测，避免触发真实LLM调用
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_new_journey", lambda msg: False)
    monkeypatch.setattr(
        "src.orchestrator.run_companion",
        lambda project_id, msg: {
            "companion_answer": "根据攻略，Day1安排...",
            "last_agent_result": {"agent": "guide_agent", "summary": "向导已回答"},
        },
    )

    channel = _FakeChannel()

    service.handle_message("chat-agent-info", "Day1怎么安排", channel)
    _wait_until_idle(project_id)

    # 检查所有消息（sent + edits）
    all_messages = [msg[1] for msg in channel.sent] + [msg[1] for msg in channel.edits]
    reply = "\n".join(all_messages)

    assert "📖 随身向导" in reply
    assert "根据攻略，Day1安排" in reply


def test_agent_info_not_displayed_when_disabled(monkeypatch):
    """Agent标识可以通过环境变量关闭"""
    monkeypatch.setattr(service, "FEISHU_SHOW_AGENT_INFO", False)

    binding = bindings.get_or_create_binding("chat-no-agent-info")
    # 清除新用户标志，避免触发欢迎消息
    bindings.clear_new_user_flag("chat-no-agent-info")

    project_id = binding["active_project_id"]
    store.init_project(project_id)
    store.update_progress(project_id, actor="main_agent", state=ProjectPhase.DONE)

    # mock掉素材采集/新旅程意图检测，避免触发真实LLM调用
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_material_collection", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_user_note", lambda msg: False)
    monkeypatch.setattr("src.channels.feishu.intent.classify_wants_new_journey", lambda msg: False)
    monkeypatch.setattr(
        "src.orchestrator.run_companion",
        lambda project_id, msg: {
            "companion_answer": "根据攻略，Day1安排...",
            "last_agent_result": {"agent": "guide_agent", "summary": "向导已回答"},
        },
    )

    channel = _FakeChannel()

    service.handle_message("chat-no-agent-info", "Day1怎么安排", channel)
    _wait_until_idle(project_id)

    # 检查所有消息（sent + edits）
    all_messages = [msg[1] for msg in channel.sent] + [msg[1] for msg in channel.edits]
    reply = "\n".join(all_messages)

    # 不应该包含agent标识
    assert "📖 随身向导" not in reply
    # 但应该包含正常回答
    assert "根据攻略，Day1安排" in reply
