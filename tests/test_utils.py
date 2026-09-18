"""src/utils.py 的单测：invoke_with_retry的重试次数/退避行为。

见docs/ISSUES.md第118条——2026-08-13把max_retries从3提到6、退避从线性(2s/4s)
改成指数(2s/4s/8s/16s/32s)，这里锁定新行为，避免以后无意改回旧参数。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import patch

import httpx
import openai
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from src import llm_rotation
from src.context_editing import ContextBudgetExceededError
from src.llm_rotation import (
    ProviderRetryDeferredError,
    ProviderRetryExhaustedError,
)
from src.utils import invoke_with_retry


class _FailingAgent:
    """前N次invoke都抛异常，之后成功返回的假agent，用于验证重试次数。"""

    def __init__(self, fail_times: int, exc: Exception):
        self.fail_times = fail_times
        self.exc = exc
        self.call_count = 0

    def invoke(self, payload):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise self.exc
        return {"messages": [], "call_count": self.call_count}


class _AlwaysFailingAgent:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.call_count = 0

    def invoke(self, payload):
        self.call_count += 1
        raise self.exc


def test_invoke_with_retry_succeeds_after_transient_failures():
    agent = _FailingAgent(fail_times=2, exc=ConnectionError("Connection error."))
    with patch("time.sleep") as mock_sleep:
        result = invoke_with_retry(agent, [{"role": "user", "content": "hi"}])
    assert result["call_count"] == 3
    assert agent.call_count == 3
    assert mock_sleep.call_count == 2


def test_invoke_with_retry_default_max_retries_is_six():
    """docs/ISSUES.md第118条：真实4次跑测3次撞上APIConnectionError，
    3次重试+线性退避(总容错约6秒)不够，加强到6次+指数退避(总容错约62秒)。"""
    agent = _AlwaysFailingAgent(ConnectionError("Connection error."))
    with patch("time.sleep") as mock_sleep:
        with pytest.raises(ConnectionError):
            invoke_with_retry(agent, [{"role": "user", "content": "hi"}])
    assert agent.call_count == 6
    assert mock_sleep.call_count == 5


def test_invoke_with_retry_uses_exponential_backoff():
    agent = _AlwaysFailingAgent(ConnectionError("Connection error."))
    with patch("time.sleep") as mock_sleep:
        with pytest.raises(ConnectionError):
            invoke_with_retry(agent, [{"role": "user", "content": "hi"}], max_retries=6, backoff_seconds=2.0)
    sleep_calls = [call.args[0] for call in mock_sleep.call_args_list]
    assert sleep_calls == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_invoke_with_retry_raises_last_exception_after_exhausting_retries():
    exc = RuntimeError("persistent failure")
    agent = _AlwaysFailingAgent(exc)
    with patch("time.sleep"):
        with pytest.raises(RuntimeError, match="persistent failure"):
            invoke_with_retry(agent, [{"role": "user", "content": "hi"}], max_retries=2)
    assert agent.call_count == 2


def test_invoke_with_retry_no_sleep_when_first_attempt_succeeds():
    agent = _FailingAgent(fail_times=0, exc=ConnectionError("unused"))
    with patch("time.sleep") as mock_sleep:
        result = invoke_with_retry(agent, [{"role": "user", "content": "hi"}])
    assert result["call_count"] == 1
    mock_sleep.assert_not_called()


def test_retry_deadline_cleanup_failure_does_not_replay_completed_agent():
    agent = _FailingAgent(fail_times=0, exc=ConnectionError("unused"))

    with patch(
        "src.utils._clear_provider_retry_deadline",
        side_effect=RuntimeError("deadline cleanup failed"),
    ), pytest.raises(RuntimeError, match="deadline cleanup failed"):
        invoke_with_retry(
            agent,
            [{"role": "user", "content": "hi"}],
        )

    assert agent.call_count == 1


def test_invoke_with_retry_does_not_retry_context_budget_stop():
    agent = _AlwaysFailingAgent(ContextBudgetExceededError("budget exceeded"))
    with patch("time.sleep") as mock_sleep:
        with pytest.raises(ContextBudgetExceededError, match="budget exceeded"):
            invoke_with_retry(agent, [{"role": "user", "content": "hi"}])
    assert agent.call_count == 1
    mock_sleep.assert_not_called()


def test_invoke_with_retry_does_not_multiply_exhausted_provider_attempts():
    provider_error = ProviderRetryExhaustedError(
        provider_group="gptload",
        attempts=5,
        last_error=ConnectionError("same-entry attempts exhausted"),
    )
    agent = _AlwaysFailingAgent(provider_error)

    with patch("time.sleep") as mock_sleep, pytest.raises(
        ProviderRetryExhaustedError
    ):
        invoke_with_retry(agent, [{"role": "user", "content": "hi"}])

    assert agent.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        (openai.BadRequestError, 400),
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
        (openai.NotFoundError, 404),
        (openai.UnprocessableEntityError, 422),
    ],
)
def test_invoke_with_retry_does_not_replay_deterministic_request_errors(
    error_type, status
):
    response = httpx.Response(
        status, request=httpx.Request("POST", "http://test")
    )
    agent = _AlwaysFailingAgent(
        error_type("invalid request", response=response, body=None)
    )

    with patch("time.sleep") as mock_sleep, pytest.raises(error_type):
        invoke_with_retry(agent, [{"role": "user", "content": "hi"}])

    assert agent.call_count == 1
    mock_sleep.assert_not_called()


# ---------- thread_id续跑（2026-08-13新增，见docs/ISSUES.md第120条）----------


class _RecordingAgent:
    """记录每次invoke收到的(input, config)，用于验证第一次attempt传完整messages，
    异常后的重试改传input=None（续跑语义，不是重新开始）。"""

    def __init__(self, fail_times: int, exc: Exception):
        self.fail_times = fail_times
        self.exc = exc
        self.calls: list[tuple] = []

    def invoke(self, input_, config=None):
        self.calls.append((input_, config))
        if len(self.calls) <= self.fail_times:
            raise self.exc
        return {"messages": [], "call_count": len(self.calls)}


def test_invoke_with_retry_without_thread_id_never_passes_config():
    """不传thread_id时（默认None）行为完全不变——不带config参数调用invoke。"""
    agent = _RecordingAgent(fail_times=1, exc=ConnectionError("transient"))
    with patch("time.sleep"):
        invoke_with_retry(agent, [{"role": "user", "content": "hi"}])
    assert agent.calls[0] == ({"messages": [{"role": "user", "content": "hi"}]}, None)
    assert agent.calls[1] == ({"messages": [{"role": "user", "content": "hi"}]}, None)


def test_invoke_with_retry_with_thread_id_first_attempt_passes_full_messages():
    agent = _RecordingAgent(fail_times=0, exc=ConnectionError("unused"))
    with patch("time.sleep"):
        invoke_with_retry(agent, [{"role": "user", "content": "hi"}], thread_id="proj:agent:route_draft")
    input_, config = agent.calls[0]
    assert input_ == {"messages": [{"role": "user", "content": "hi"}]}
    assert config == {"configurable": {"thread_id": "proj:agent:route_draft"}}


def test_invoke_with_retry_with_thread_id_retry_passes_none_to_resume():
    """核心场景：撞到异常后的重试不重发messages（那样会因为add_messages reducer
    追加重复历史），而是传input=None让LangGraph从上次checkpoint续跑。"""
    agent = _RecordingAgent(fail_times=2, exc=ConnectionError("transient"))
    with patch("time.sleep"):
        result = invoke_with_retry(
            agent, [{"role": "user", "content": "hi"}], thread_id="proj:agent:route_draft"
        )
    assert result["call_count"] == 3
    # 第一次attempt传完整messages
    assert agent.calls[0][0] == {"messages": [{"role": "user", "content": "hi"}]}
    # 第二、三次attempt（重试）传None，不是重新发送messages
    assert agent.calls[1][0] is None
    assert agent.calls[2][0] is None
    # 三次调用用的是同一个thread_id（续跑同一个对话，不是切换到别的thread）
    expected_config = {"configurable": {"thread_id": "proj:agent:route_draft"}}
    assert agent.calls[0][1] == expected_config
    assert agent.calls[1][1] == expected_config
    assert agent.calls[2][1] == expected_config


def test_invoke_with_retry_resumes_deferred_provider_from_checkpoint():
    provider_error = ProviderRetryDeferredError(
        provider_group="gptload",
        attempts=1,
        last_error=ConnectionError("retry later"),
        retry_after_seconds=90.0,
        next_retry_at="2026-09-07T12:01:30+00:00",
    )
    agent = _RecordingAgent(fail_times=1, exc=provider_error)

    with patch.object(
        provider_error, "remaining_delay_seconds", return_value=90.0
    ) as remaining_delay, patch("time.sleep") as mock_sleep:
        result = invoke_with_retry(
            agent,
            [{"role": "user", "content": "hi"}],
            thread_id="proj:agent:lifestyle",
        )

    assert result["call_count"] == 2
    assert agent.calls[0][0] == {
        "messages": [{"role": "user", "content": "hi"}]
    }
    assert agent.calls[1][0] is None
    assert agent.calls[0][1] == agent.calls[1][1] == {
        "configurable": {"thread_id": "proj:agent:lifestyle"}
    }
    remaining_delay.assert_called_once_with()
    mock_sleep.assert_called_once_with(90.0)


def test_deferred_provider_attempts_share_one_hard_ceiling_across_resumes():
    provider_error = ProviderRetryDeferredError(
        provider_group="gptload",
        attempts=1,
        last_error=ConnectionError("retry later"),
        retry_after_seconds=90.0,
        next_retry_at="2026-09-07T12:01:30+00:00",
    )
    agent = _RecordingAgent(fail_times=99, exc=provider_error)

    with patch.object(
        provider_error, "remaining_delay_seconds", return_value=0.0
    ), pytest.raises(ProviderRetryExhaustedError) as caught:
        invoke_with_retry(
            agent,
            [{"role": "user", "content": "hi"}],
            thread_id="proj:agent:provider-ceiling",
        )

    assert len(agent.calls) == provider_error.max_attempts
    assert caught.value.attempts == provider_error.max_attempts


def test_invoke_with_retry_resumes_real_state_graph_after_provider_deferral():
    provider_error = ProviderRetryDeferredError(
        provider_group="gptload",
        attempts=1,
        last_error=ConnectionError("retry later"),
        retry_after_seconds=90.0,
        next_retry_at="2026-09-07T12:01:30+00:00",
    )
    executions = {"n": 0}

    def call_provider(state):
        executions["n"] += 1
        if executions["n"] == 1:
            raise provider_error
        return {"completed": True}

    graph = StateGraph(dict)
    graph.add_node("call_provider", call_provider)
    graph.add_edge(START, "call_provider")
    graph.add_edge("call_provider", END)
    agent = graph.compile(checkpointer=InMemorySaver())

    with patch.object(
        provider_error, "remaining_delay_seconds", return_value=0.0
    ):
        result = invoke_with_retry(
            agent,
            [{"role": "user", "content": "hi"}],
            thread_id="proj:agent:real-checkpoint",
        )

    assert executions["n"] == 2
    assert result["completed"] is True


def test_deferred_provider_deadline_survives_sqlite_checkpoint_reopen(tmp_path):
    next_retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    provider_error = ProviderRetryDeferredError(
        provider_group="gptload",
        attempts=1,
        last_error=ConnectionError("retry later"),
        retry_after_seconds=3600.0,
        next_retry_at=next_retry_at,
    )
    executions = {"n": 0}
    events: list[str] = []

    def build_agent():
        saver = SqliteSaver(
            sqlite3.connect(
                str(tmp_path / "checkpoint.sqlite"),
                check_same_thread=False,
            )
        )
        saver.setup()

        def call_provider(_state):
            offset = llm_rotation._consume_provider_retry_attempt_offset("gptload")
            events.append(f"provider:{offset}")
            executions["n"] += 1
            if executions["n"] == 1:
                raise provider_error
            return {"completed": True}

        graph = StateGraph(dict)
        graph.add_node("call_provider", call_provider)
        graph.add_edge(START, "call_provider")
        graph.add_edge("call_provider", END)
        return graph.compile(checkpointer=saver), saver

    agent, saver = build_agent()
    with patch("time.sleep", side_effect=RuntimeError("process stopped while waiting")):
        with pytest.raises(RuntimeError, match="process stopped while waiting"):
            invoke_with_retry(
                agent,
                [{"role": "user", "content": "hi"}],
                thread_id="proj:agent:sqlite-deadline",
            )
    with saver.cursor() as cur:
        persisted = cur.execute(
            "SELECT attempts, next_retry_at FROM provider_retry_deadlines WHERE thread_id = ?",
            ("proj:agent:sqlite-deadline",),
        ).fetchone()
    assert persisted == (1, next_retry_at)
    saver.conn.close()

    events.clear()
    resumed_agent, resumed_saver = build_agent()

    def interrupt_resumed_wait(_seconds):
        events.append("sleep")
        raise RuntimeError("second process stopped while waiting")

    with patch("time.sleep", side_effect=interrupt_resumed_wait):
        with pytest.raises(RuntimeError, match="second process stopped while waiting"):
            invoke_with_retry(
                resumed_agent,
                [{"role": "user", "content": "fresh prompt must not replay"}],
                thread_id="proj:agent:sqlite-deadline",
                resume_from_checkpoint=True,
            )

    assert events == ["sleep"]
    assert executions["n"] == 1
    with resumed_saver.cursor() as cur:
        cur.execute(
            "UPDATE provider_retry_deadlines SET next_retry_at = ? WHERE thread_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "proj:agent:sqlite-deadline",
            ),
        )
    resumed_saver.conn.close()

    events.clear()
    resumed_agent, resumed_saver = build_agent()
    with patch("time.sleep") as resumed_sleep:
        result = invoke_with_retry(
            resumed_agent,
            [{"role": "user", "content": "fresh prompt must not replay"}],
            thread_id="proj:agent:sqlite-deadline",
            resume_from_checkpoint=True,
        )

    resumed_sleep.assert_not_called()
    assert events == ["provider:1"]
    assert executions["n"] == 2
    assert result["completed"] is True
    with resumed_saver.cursor() as cur:
        remaining = cur.execute(
            "SELECT COUNT(*) FROM provider_retry_deadlines WHERE thread_id = ?",
            ("proj:agent:sqlite-deadline",),
        ).fetchone()
    assert remaining == (0,)
    resumed_saver.conn.close()


def test_deferred_provider_attempt_ceiling_survives_repeated_sqlite_reopens(
    tmp_path,
):
    database_path = tmp_path / "checkpoint.sqlite"
    thread_id = "proj:agent:sqlite-attempt-ceiling"
    provider_calls: list[int] = []

    def build_agent():
        saver = SqliteSaver(
            sqlite3.connect(str(database_path), check_same_thread=False)
        )
        saver.setup()

        def call_provider(_state):
            offset = llm_rotation._consume_provider_retry_attempt_offset("gptload")
            provider_calls.append(offset)
            cumulative_attempt = offset + 1
            raise ProviderRetryDeferredError(
                provider_group="gptload",
                attempts=cumulative_attempt,
                last_error=ConnectionError("retry later"),
                retry_after_seconds=0.0,
                next_retry_at=(
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat(),
            )

        graph = StateGraph(dict)
        graph.add_node("call_provider", call_provider)
        graph.add_edge(START, "call_provider")
        graph.add_edge("call_provider", END)
        return graph.compile(checkpointer=saver), saver

    for process_index in range(llm_rotation.GPTLOAD_MAX_ATTEMPTS):
        agent, saver = build_agent()
        expected_error = (
            ProviderRetryExhaustedError
            if process_index == llm_rotation.GPTLOAD_MAX_ATTEMPTS - 1
            else ProviderRetryDeferredError
        )
        with pytest.raises(expected_error) as caught:
            invoke_with_retry(
                agent,
                [{"role": "user", "content": "initial prompt"}],
                thread_id=thread_id,
                resume_from_checkpoint=process_index > 0,
                max_retries=1,
            )

        assert caught.value.attempts == process_index + 1
        with saver.cursor() as cur:
            persisted = cur.execute(
                "SELECT attempts FROM provider_retry_deadlines WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if expected_error is ProviderRetryExhaustedError:
            assert persisted is None
        else:
            assert persisted == (process_index + 1,)
        saver.conn.close()

    assert provider_calls == list(range(llm_rotation.GPTLOAD_MAX_ATTEMPTS))


def test_deleting_checkpoint_thread_also_clears_provider_retry_deadline(tmp_path):
    thread_id = "proj:agent:deleted-retry-thread"
    saver = SqliteSaver(
        sqlite3.connect(
            str(tmp_path / "checkpoint.sqlite"),
            check_same_thread=False,
        )
    )
    saver.setup()

    def call_provider(_state):
        llm_rotation._consume_provider_retry_attempt_offset("gptload")
        raise ProviderRetryDeferredError(
            provider_group="gptload",
            attempts=1,
            last_error=ConnectionError("retry later"),
            retry_after_seconds=3600.0,
            next_retry_at=(
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
        )

    graph = StateGraph(dict)
    graph.add_node("call_provider", call_provider)
    graph.add_edge(START, "call_provider")
    graph.add_edge("call_provider", END)
    agent = graph.compile(checkpointer=saver)

    with pytest.raises(ProviderRetryDeferredError):
        invoke_with_retry(
            agent,
            [{"role": "user", "content": "initial prompt"}],
            thread_id=thread_id,
            max_retries=1,
        )

    with saver.cursor() as cur:
        before_delete = cur.execute(
            "SELECT attempts FROM provider_retry_deadlines WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    assert before_delete == (1,)

    saver.delete_thread(thread_id)

    with saver.cursor() as cur:
        after_delete = cur.execute(
            "SELECT attempts FROM provider_retry_deadlines WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    assert after_delete is None
    saver.conn.close()


def test_invoke_with_retry_does_not_resume_deferred_provider_without_checkpoint():
    provider_error = ProviderRetryDeferredError(
        provider_group="gptload",
        attempts=1,
        last_error=ConnectionError("retry later"),
        retry_after_seconds=90.0,
        next_retry_at="2026-09-07T12:01:30+00:00",
    )
    agent = _AlwaysFailingAgent(provider_error)

    with patch("time.sleep") as mock_sleep, pytest.raises(
        ProviderRetryDeferredError
    ):
        invoke_with_retry(agent, [{"role": "user", "content": "hi"}])

    assert agent.call_count == 1
    mock_sleep.assert_not_called()


def test_invoke_with_retry_can_resume_existing_checkpoint_on_first_attempt():
    """进程重启后首次重新进入POI节点时，直接从已有thread续跑，不重发初始messages。"""
    agent = _RecordingAgent(fail_times=0, exc=ConnectionError("unused"))
    with patch("time.sleep"):
        result = invoke_with_retry(
            agent,
            [{"role": "user", "content": "fresh prompt should not be sent"}],
            thread_id="proj:agent:poi_detail_draft:abc:day2_wuhouci",
            resume_from_checkpoint=True,
        )
    assert result["call_count"] == 1
    assert agent.calls[0][0] is None
    assert agent.calls[0][1] == {
        "configurable": {"thread_id": "proj:agent:poi_detail_draft:abc:day2_wuhouci"}
    }


def test_invoke_with_retry_passes_recursion_limit_without_thread_id():
    agent = _RecordingAgent(fail_times=0, exc=ConnectionError("unused"))

    result = invoke_with_retry(
        agent,
        [{"role": "user", "content": "repair"}],
        max_retries=1,
        recursion_limit=5,
    )

    assert result["call_count"] == 1
    assert agent.calls == [
        ({"messages": [{"role": "user", "content": "repair"}]}, {"recursion_limit": 5})
    ]


def test_invoke_with_retry_does_not_retry_graph_recursion_error():
    agent = _AlwaysFailingAgent(GraphRecursionError("research budget exhausted"))

    with patch("time.sleep") as mock_sleep, pytest.raises(GraphRecursionError):
        invoke_with_retry(agent, [{"role": "user", "content": "hi"}])

    assert agent.call_count == 1
    mock_sleep.assert_not_called()
