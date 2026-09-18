"""src/llm_trace.py + RotatingChatOpenAI._generate trace hook 的单元测试（2026-08-10新增）。

验证：contextvar set/get、append_trace 落盘格式、无上下文静默跳过、seq 递增、
失败 trace 字段、成功 trace 字段（含 reasoning + invalid_tool_calls，§82/94 截断签名）、
POI 并行 contextvar 传播、无 fallback_chain 旧行为不回归。
不调真实 API，mock ChatOpenAI._generate。
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from src import llm_rotation, llm_trace
from src.api import key_store


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Keep trace and key-store writes inside this test's temporary root."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm_trace, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(key_store, "KEY_STORE_PATH", tmp_path / "runtime_config" / "api_keys.json")
    monkeypatch.setattr("src.config.LLM_API_KEYS", [])
    monkeypatch.setattr("src.config.LLM_API_KEYS_LIGHT", [])
    monkeypatch.setattr("src.config.MIMO_TOKEN_PLAN_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_GPTLOAD_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_API_KEYS", [])
    monkeypatch.setattr("src.config.CUNAI_API_KEYS", [])
    monkeypatch.setattr("src.config.TAVILY_API_KEYS", [])
    monkeypatch.setattr("src.config.POSTER_IMAGE_API_KEY", None)


@pytest.fixture(autouse=True)
def clear_trace_context():
    """每个测试前清掉 contextvar（避免上个测试残留）。set_trace_context 在 node 退出后
    context 自然回退，但同进程内连续跑测试可能残留——显式 reset 保险。"""
    llm_trace._current_project_id.set(None)
    llm_trace._current_agent.set(None)
    llm_trace._call_seq.set(0)


# ---------- contextvar 基础 ----------


def test_set_and_get_trace_context():
    llm_trace.set_trace_context("p1", "route_agent")
    assert llm_trace.get_trace_context() == ("p1", "route_agent")


def test_trace_path_none_without_context():
    """无 context 时 _trace_path 返回 None（调用方应静默跳过）。"""
    assert llm_trace._trace_path() is None


def test_trace_path_with_context():
    llm_trace.set_trace_context("p1", "route_agent")
    p = llm_trace._trace_path()
    assert p is not None
    assert p.name == "trace.jsonl"
    assert "p1" in str(p) and "route_agent" in str(p)


# ---------- append_trace 落盘 ----------


def test_append_trace_writes_jsonl_with_seq_ts_project_agent():
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"group": "mimo", "model_name": "mimo-v2.5"})
    p = llm_trace._trace_path()
    assert p.exists()
    line = p.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["seq"] == 0
    assert rec["project_id"] == "p1"
    assert rec["agent"] == "route_agent"
    assert rec["group"] == "mimo"
    assert "ts" in rec and isinstance(rec["ts"], float)


def test_append_trace_seq_increments():
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"group": "g"})
    llm_trace.append_trace({"group": "g"})
    llm_trace.append_trace({"group": "g"})
    lines = llm_trace._trace_path().read_text(encoding="utf-8").strip().split("\n")
    seqs = [json.loads(l)["seq"] for l in lines]
    assert seqs == [0, 1, 2]


def test_append_trace_silent_skip_without_context(tmp_path):
    """无 context 不抛错、不写文件。覆盖 admin key-test / B 类 structured_output judge 场景。"""
    # 没有 set_trace_context
    llm_trace.append_trace({"group": "full"})
    # 不应该写任何文件
    assert not (tmp_path / "projects").exists()


# ---------- _trace_call 成功路径（含 §82 reasoning + §94 invalid_tool_calls 签名）----------


def _make_success_result(reasoning=8192, finish_reason="tool_calls", invalid_tool_call_error=None):
    """构造一个 ChatResult，含 usage_metadata(reasoning) + 可选 invalid_tool_calls。
    模拟 §82 截断（reasoning=8192）和 §94 tool_call 参数 JSON 被截断（invalid_tool_calls）。"""
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "write_route_plan", "args": {"days": []}, "id": "tc1"}],
        invalid_tool_calls=(
            [
                {
                    "name": "write_route_plan",
                    "args": '{"days": [...',  # 截断的原始 args 串
                    "error": invalid_tool_call_error or "Function write_route_plan arguments are not valid JSON. Received JSONDecodeError ...",
                    "id": "itc1",
                    "type": "invalid_tool_call",
                }
            ]
            if invalid_tool_call_error is not None
            else []
        ),
        usage_metadata={
            "input_tokens": 7348,
            "output_tokens": 1933,
            "total_tokens": 9281,
            "input_token_details": {},
            "output_token_details": {"reasoning": reasoning},
        },
    )
    gen = ChatGeneration(message=ai, generation_info={"finish_reason": finish_reason})
    cr = ChatResult(generations=[gen])
    return cr


def test_trace_call_success_records_finish_reason_usage_tool_calls_invalid(monkeypatch):
    """成功 trace 记 finish_reason/usage.reasoning/tool_calls/invalid_tool_calls。
    §82 reasoning=8192 + §94 invalid_tool_calls.error 都要进 trace。"""
    monkeypatch.setattr(llm_rotation.time, "sleep", lambda *a, **k: None)
    llm_trace.set_trace_context("p1", "route_agent")
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="deepseek-v4-flash",
        key_group="gptload",
    )
    result = _make_success_result(reasoning=8192, invalid_tool_call_error="JSONDecodeError at end")
    messages = [HumanMessage(content="hi")]
    llm_rotation._trace_call(model, messages, result, None, t0=0.0, cycle=0)
    rec = json.loads(llm_trace._trace_path().read_text(encoding="utf-8").strip())
    assert rec["ok"] is True
    assert rec["provider_group"] == "gptload"
    assert rec["model_name"] == "deepseek-v4-flash"
    assert rec["finish_reason"] == "tool_calls"
    assert rec["usage"]["reasoning"] == 8192  # §82 截断签名
    assert rec["usage"]["input"] == 7348
    assert rec["tool_calls"] == ["write_route_plan"]
    assert rec["invalid_tool_calls"] is not None
    assert rec["invalid_tool_calls"][0]["name"] == "write_route_plan"
    assert "JSONDecodeError" in rec["invalid_tool_calls"][0]["error"]
    assert rec["messages"][0]["role"] == "human"
    assert rec["messages"][0]["len"] == 2  # "hi"


def test_trace_call_success_no_invalid_tool_calls(monkeypatch):
    """成功但无 invalid_tool_calls 时，invalid_tool_calls 字段为 None。"""
    monkeypatch.setattr(llm_rotation.time, "sleep", lambda *a, **k: None)
    llm_trace.set_trace_context("p1", "route_agent")
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test", api_key="k1", model="deepseek-v4-flash"
    )
    # invalid_tool_call_error=None → 不构造 invalid_tool_calls
    result = _make_success_result(reasoning=290, invalid_tool_call_error=None)
    llm_rotation._trace_call(model, [HumanMessage(content="hi")], result, None, 0.0, 0)
    rec = json.loads(llm_trace._trace_path().read_text(encoding="utf-8").strip())
    assert rec["ok"] is True
    assert rec["invalid_tool_calls"] is None
    assert rec["usage"]["reasoning"] == 290


# ---------- _trace_call 失败路径 ----------


def _make_503_no_keys_error():
    from openai import APIStatusError

    resp = httpx.Response(503, request=httpx.Request("POST", "http://test"))
    return APIStatusError(
        "Error code: 503 - {'code': 'NO_KEYS_AVAILABLE', 'message': 'No active API keys'}",
        response=resp,
        body={"code": "NO_KEYS_AVAILABLE", "message": "No active API keys"},
    )


def test_trace_call_failure_records_error_type_status_msg(monkeypatch):
    """失败 trace 记 ok:false + error_type + status_code + error_msg(含NO_KEYS)。"""
    monkeypatch.setattr(llm_rotation.time, "sleep", lambda *a, **k: None)
    llm_trace.set_trace_context("p1", "cunai")
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test", api_key="k1", model="deepseek-v4-flash-0731"
    )
    exc = _make_503_no_keys_error()
    llm_rotation._trace_call(model, [HumanMessage(content="hi")], None, exc, 0.0, 0)
    rec = json.loads(llm_trace._trace_path().read_text(encoding="utf-8").strip())
    assert rec["ok"] is False
    assert rec["error_type"] == "APIStatusError"
    assert rec["status_code"] == 503
    assert "NO_KEYS_AVAILABLE" in rec["error_msg"]


# ---------- _generate hook 集成（成功+失败都落 trace）----------


def test_generate_within_group_success_writes_trace(monkeypatch):
    """_generate_within_group 成功返回时落一条 ok:true trace。"""
    monkeypatch.setattr(llm_rotation.time, "sleep", lambda *a, **k: None)
    llm_trace.set_trace_context("p1", "route_agent")
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test", api_key="k1", model="test-model"
    )

    def fake_generate(self, *args, **kwargs):
        return _make_success_result(reasoning=100)

    with patch.object(ChatOpenAI, "_generate", fake_generate):
        result = model._generate_within_group([])
    assert result is not None
    rec = json.loads(llm_trace._trace_path().read_text(encoding="utf-8").strip())
    assert rec["ok"] is True
    assert rec["usage"]["reasoning"] == 100


def test_generate_within_group_failure_writes_trace(monkeypatch):
    """_generate_within_group 撞 503 时落一条 ok:false trace，然后按 _is_channel_switch_error 立即切。"""
    from openai import APIStatusError

    monkeypatch.setattr(llm_rotation.time, "sleep", lambda *a, **k: None)
    llm_trace.set_trace_context("p1", "cunai")
    _seed_keys_for_trace()
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test", api_key="cunai1", model="test-model", key_group="cunai"
    )

    def fake_generate(self, *args, **kwargs):
        raise _make_503_no_keys_error()

    with patch.object(ChatOpenAI, "_generate", fake_generate):
        with pytest.raises(llm_rotation._ChannelExhausted):
            model._generate_within_group([])
    rec = json.loads(llm_trace._trace_path().read_text(encoding="utf-8").strip())
    assert rec["ok"] is False
    assert rec["status_code"] == 503


# ---------- 无 trace context 时 _generate 旧行为不回归 ----------


def test_generate_without_trace_context_still_works_and_no_file(monkeypatch):
    """无 trace context（不在 agent run 里）时 _generate 正常工作，且不写任何 trace 文件。
    覆盖 B 类 structured_output judge / admin key-test 场景，验证 trace 不影响调用语义。"""
    monkeypatch.setattr(llm_rotation.time, "sleep", lambda *a, **k: None)
    # 不 set_trace_context
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test", api_key="k1", model="test-model"
    )

    def fake_generate(self, *args, **kwargs):
        return _make_success_result(reasoning=50)

    with patch.object(ChatOpenAI, "_generate", fake_generate):
        result = model._generate_within_group([])
    assert result is not None
    # 不写 trace 文件：无 context 时 append_trace 静默跳过，projects/ 下不应有 trace.jsonl
    projects_dir = os.getcwd() / "projects" if hasattr(os.getcwd(), "__truediv__") else None
    # tmp_path 是 Path，os.getcwd() 在 monkeypatch.chdir 后也是 Path
    from pathlib import Path

    projects = Path(os.getcwd()) / "projects"
    if projects.exists():
        assert not list(projects.rglob("trace.jsonl"))


def _seed_keys_for_trace():
    """给 trace 测试用的 key_store 注入一把 CunAI key。"""
    try:
        key_store.add_key("cunai", "cunai1")
    except Exception:
        pass  # 可能已存在


# ---------- POI 并行 contextvar 传播（Send 语义模拟）----------


def test_contextvar_propagates_to_parallel_branch():
    """模拟 langgraph Send() 并行：每个分支继承父 contextvar 副本，set 后各分支独立。
    langgraph 的并发模型是 asyncio.Task（不是 ThreadPoolExecutor）——asyncio.create_task
    会复制当前 contextvars.Context 到子 Task，所以父 node set 的 (project, agent) 在并行
    分支里能 get 到。这是 contextvars 的语义保证，本项目靠它让 POI 并行各分支的 trace
    都正确归属 poi_detail_agent。

    注：ThreadPoolExecutor 不会自动复制 contextvar（Python 语义如此），但 langgraph 的 Send
    走 asyncio 不是线程池，所以这里用 asyncio 验证才贴合真实运行模型。"""
    import asyncio

    llm_trace.set_trace_context("p1", "poi_detail_agent")

    async def branch_work(poi_name):
        await asyncio.sleep(0)  # 让出控制权，模拟真实异步分支
        pid, agent = llm_trace.get_trace_context()
        return (poi_name, pid, agent)

    async def main():
        # 模拟 Send fan-out：每个 create_task 复制当前 context
        tasks = [asyncio.create_task(branch_work(f"poi{i}")) for i in range(3)]
        return await asyncio.gather(*tasks)

    results = asyncio.run(main())
    for poi_name, pid, agent in results:
        assert pid == "p1"
        assert agent == "poi_detail_agent"


# ---------- read_recent_trace（2026-08-18新增，飞书进度可见性改进）----------


def test_read_recent_trace_returns_empty_list_when_file_missing():
    assert llm_trace.read_recent_trace("p1", "route_agent") == []


def test_read_recent_trace_returns_records_in_order():
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai"})
    llm_trace.append_trace({"ok": False, "group": "cunai", "error_type": "InternalServerError"})
    llm_trace.append_trace({"ok": True, "group": "cunai"})

    recent = llm_trace.read_recent_trace("p1", "route_agent")
    assert len(recent) == 3
    assert [r["seq"] for r in recent] == [0, 1, 2]
    assert recent[1]["error_type"] == "InternalServerError"


def test_read_recent_trace_respects_limit():
    llm_trace.set_trace_context("p1", "route_agent")
    for _ in range(5):
        llm_trace.append_trace({"ok": True, "group": "cunai"})

    recent = llm_trace.read_recent_trace("p1", "route_agent", limit=2)
    assert len(recent) == 2
    assert [r["seq"] for r in recent] == [3, 4]


def test_read_recent_trace_skips_corrupted_lines():
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai"})
    p = llm_trace._trace_path()
    with p.open("a", encoding="utf-8") as f:
        f.write("not valid json\n")
    llm_trace.append_trace({"ok": False, "group": "cunai"})

    recent = llm_trace.read_recent_trace("p1", "route_agent")
    assert len(recent) == 2


def test_read_recent_trace_scoped_by_project_and_agent():
    llm_trace.set_trace_context("p1", "route_agent")
    llm_trace.append_trace({"ok": True, "group": "cunai"})
    llm_trace.set_trace_context("p1", "lifestyle_agent")
    llm_trace.append_trace({"ok": False, "group": "cunai"})

    assert len(llm_trace.read_recent_trace("p1", "route_agent")) == 1
    assert len(llm_trace.read_recent_trace("p1", "lifestyle_agent")) == 1
    assert len(llm_trace.read_recent_trace("p2", "route_agent")) == 0
