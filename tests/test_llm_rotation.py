"""LLM key轮询、渠道fallback与MiMo模型档位的确定性单元测试。"""
from __future__ import annotations

import importlib
import json
import socket
from unittest.mock import patch

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from openai import RateLimitError
from pydantic import BaseModel, ValidationError

from src import config, llm_rotation
from src.api import key_store
from src.config import _parse_api_keys
from src.structured_output import bind_structured_json


@pytest.fixture(autouse=True)
def isolated_key_store(tmp_path, monkeypatch):
    monkeypatch.setattr(key_store, "KEY_STORE_PATH", tmp_path / "runtime_config" / "api_keys.json")
    # Unit tests must not inherit provider endpoints from a developer's private
    # .env. Keys and URLs are both synthetic so the suite is reproducible from
    # a clean public snapshot without making network calls.
    provider_urls = {
        "LLM_BASE_URL": "https://llm.example/v1",
        "MIMO_CHAT_BASE_URL": "https://mimo.example/v1",
        "MIMO_GPTLOAD_BASE_URL": "https://gptload.example/v1",
        "GPTLOAD_BASE_URL": "https://gptload.example/v1",
        "MIMO_TOKEN_PLAN_BASE_URL": "https://token-plan.example/v1",
    }
    for name, value in provider_urls.items():
        monkeypatch.setattr(config, name, value)
        monkeypatch.setattr(llm_rotation, name, value)
    provider_models = {
        "LLM_MODEL": "full-test-model",
        "LLM_LIGHT_MODEL": "light-test-model",
    }
    for name, value in provider_models.items():
        monkeypatch.setattr(config, name, value)
        monkeypatch.setattr(llm_rotation, name, value)
    monkeypatch.setattr("src.config.LLM_API_KEYS", [])
    monkeypatch.setattr("src.config.LLM_API_KEYS_LIGHT", [])
    monkeypatch.setattr("src.config.MIMO_TOKEN_PLAN_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_GPTLOAD_API_KEYS", [])
    monkeypatch.setattr("src.config.GPTLOAD_API_KEYS", [])
    monkeypatch.setattr("src.config.JIJI_API_KEYS", [])
    monkeypatch.setattr("src.config.MIMO_API_KEYS", [])
    monkeypatch.setattr("src.config.CUNAI_API_KEYS", [])
    monkeypatch.setattr("src.config.OOIOO_API_KEYS", [])
    monkeypatch.setattr(llm_rotation, "OOIOO_API_KEYS", [])
    monkeypatch.setattr(llm_rotation, "JIJI_API_KEYS", [])
    monkeypatch.setattr(llm_rotation, "_CHANNEL_FALLBACK_CHAIN", ("gptload",))
    monkeypatch.setattr(llm_rotation, "_VERIFICATION_FALLBACK_CHAIN", ("gptload",))
    monkeypatch.setattr("src.config.TAVILY_API_KEYS", [])
    monkeypatch.setattr("src.config.POSTER_IMAGE_API_KEY", None)
    monkeypatch.setattr(
        llm_rotation,
        "_current_key_index",
        {
            "full": 0,
            "light": 0,
            "mimo": 0,
            "gptload": 0,
            "deepseek": 0,
            "jiji": 0,
            "cunai": 0,
            "ooioo": 0,
        },
    )


def _seed_keys(group: str, keys: list[str]) -> None:
    for key in keys:
        key_store.add_key(group, key)


def _seed_mimo_keys(entries: list[tuple[str, str]]) -> None:
    for key, channel in entries:
        key_store.add_key("mimo", key, channel=channel)


@pytest.fixture
def three_keys():
    _seed_keys("full", ["k1", "k2", "k3"])
    return ["k1", "k2", "k3"]


def _response(status: int, *, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", "http://test"),
        headers=headers,
    )


def _make_rate_limit_error(
    *, headers: dict | None = None, body: dict | None = None
) -> RateLimitError:
    return RateLimitError(
        "mocked rpm exhausted",
        response=_response(429, headers=headers),
        body=body,
    )


def _make_401_auth_error():
    from openai import AuthenticationError

    return AuthenticationError("Invalid API Key", response=_response(401), body=None)


def _make_403_permission_error():
    from openai import PermissionDeniedError

    return PermissionDeniedError("Your request was blocked.", response=_response(403), body=None)


def _make_404_not_found_error():
    from openai import NotFoundError

    return NotFoundError("model not found", response=_response(404), body=None)


def _make_transient_gptload_model_not_found_error():
    from openai import NotFoundError

    return NotFoundError(
        "Error code: 404 - Model not found",
        response=_response(404),
        body={
            "message": "Model not found",
            "type": "invalid_request_error",
            "param": "",
            "code": 404,
        },
    )


def _make_503_service_busy_error():
    from openai import InternalServerError

    return InternalServerError("SERVICE_BUSY", response=_response(503), body=None)


def _make_503_no_keys_error():
    from openai import APIStatusError

    return APIStatusError(
        "NO_KEYS_AVAILABLE: No active API keys",
        response=_response(503),
        body={"code": "NO_KEYS_AVAILABLE"},
    )


def _make_504_gateway_timeout_error():
    from openai import APIStatusError

    return APIStatusError("504 Gateway Time-out", response=_response(504), body=None)


def _make_gateway_error(status: int):
    from openai import APIStatusError

    return APIStatusError(
        f"{status} gateway error", response=_response(status), body=None
    )


def _make_api_connection_error():
    from openai import APIConnectionError

    return APIConnectionError(request=httpx.Request("POST", "http://test"))


def _make_read_error():
    return httpx.ReadError(
        "connection reset while reading",
        request=httpx.Request("POST", "http://test"),
    )


def _wrap_dns_error(
    code: int,
    *,
    use_context: bool = False,
    wrap_openai: bool = False,
) -> Exception:
    dns_error = socket.gaierror(code, "mock resolver failure")
    connect_error = httpx.ConnectError(
        "mock connect failure",
        request=httpx.Request("POST", "http://test"),
    )
    if use_context:
        connect_error.__context__ = dns_error
    else:
        connect_error.__cause__ = dns_error
    if not wrap_openai:
        return connect_error
    api_error = openai.APIConnectionError(
        request=httpx.Request("POST", "http://test")
    )
    api_error.__cause__ = connect_error
    return api_error


def _make_temporary_stream_error():
    from openai import APIError

    return APIError(
        "Upstream service temporarily unavailable",
        request=httpx.Request("POST", "http://test"),
        body=None,
    )


def _make_http2_stream_error():
    from openai import APIError

    return APIError(
        "Upstream HTTP/2 stream failed",
        request=httpx.Request("POST", "http://test"),
        body=None,
    )


def _raise_empty_completion_snapshot_assertion():
    namespace = {}
    source = "def current_completion_snapshot():\n    assert False\n"
    exec(
        compile(
            source,
            "/test/site-packages/openai/lib/streaming/chat/_completions.py",
            "exec",
        ),
        namespace,
    )
    namespace["current_completion_snapshot"]()


def _make_generic_api_error():
    from openai import APIError

    return APIError(
        "invalid streamed payload",
        request=httpx.Request("POST", "http://test"),
        body=None,
    )


def _make_insufficient_quota_error(code: str = "insufficient_user_quota"):
    from openai import PermissionDeniedError

    return PermissionDeniedError(
        f"Error code: 403 - {code}",
        response=_response(403),
        body={"error": {"code": code}},
    )


def _make_bad_request_error():
    from openai import BadRequestError

    return BadRequestError("invalid request", response=_response(400), body=None)


def _make_unprocessable_entity_error():
    from openai import UnprocessableEntityError

    return UnprocessableEntityError(
        "invalid schema", response=_response(422), body=None
    )


class _StructuredVerdict(BaseModel):
    ok: bool


def _make_cunai_tool_choice_error():
    from openai import BadRequestError

    return BadRequestError(
        "Thinking mode does not support this tool_choice",
        response=_response(400),
        body={"error": {"code": "400001"}},
    )


def test_parse_api_keys():
    assert _parse_api_keys(" a , b ,, c ", "single") == ["a", "b", "c"]
    assert _parse_api_keys(None, "single") == ["single"]
    assert _parse_api_keys(",,,", "single") == ["single"]
    assert _parse_api_keys(None, None) == []


def test_current_key_rotates_and_groups_are_independent():
    _seed_keys("full", ["f1", "f2"])
    _seed_keys("light", ["l1", "l2"])

    llm_rotation._advance_key("light")

    assert llm_rotation._current_key("full") == "f1"
    assert llm_rotation._current_key("light") == "l2"
    llm_rotation._advance_key("light")
    assert llm_rotation._current_key("light") == "l1"


def test_mimo_keys_keep_channel_specific_base_urls():
    _seed_mimo_keys([("gateway-k1", "gptload"), ("tp-k1", "token_plan"), ("legacy-k1", "legacy")])
    from src.config import MIMO_CHAT_BASE_URL, MIMO_GPTLOAD_BASE_URL, MIMO_TOKEN_PLAN_BASE_URL

    assert llm_rotation._mimo_keys_and_urls() == [
        ("gateway-k1", MIMO_GPTLOAD_BASE_URL),
        ("tp-k1", MIMO_TOKEN_PLAN_BASE_URL),
        ("legacy-k1", MIMO_CHAT_BASE_URL),
    ]


def test_reasoning_content_survives_tool_call_round_trip():
    model = llm_rotation.RotatingChatOpenAI(base_url="http://test", api_key="k1", model="test-model")
    result = model._create_chat_result(
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "inspect evidence",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    assistant = result.generations[0].message
    payload = model._get_request_payload([
        HumanMessage(content="review"),
        assistant,
        ToolMessage(content="evidence", tool_call_id="call-1"),
    ])

    assert assistant.additional_kwargs["reasoning_content"] == "inspect evidence"
    assert payload["messages"][1]["reasoning_content"] == "inspect evidence"


def test_nonstream_verification_recovers_exact_json_from_empty_reasoning_content():
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        recover_structured_json_from_reasoning=True,
    )
    result = model._create_chat_result(
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": '{"ok": true}',
                },
                "finish_reason": "stop",
            }],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )

    message = result.generations[0].message
    assert message.content == '{"ok": true}'
    assert "reasoning_content" not in message.additional_kwargs
    assert message.response_metadata["structured_json_recovered_from_reasoning"] is True


def test_nonstream_verification_recovers_exact_json_from_nonstandard_null_role_response():
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        recover_structured_json_from_reasoning=True,
    )
    response = {
        "choices": [{
            "message": {
                "role": "analysis",
                "content": None,
                "reasoning_content": '{"ok": true}',
            },
            "finish_reason": "stop",
        }],
        "model": "test-model",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    result = model._create_chat_result(response)

    message = result.generations[0].message
    assert isinstance(message, AIMessage)
    assert message.content == '{"ok": true}'
    assert message.response_metadata["structured_json_recovered_from_reasoning"] is True
    assert response["choices"][0]["message"]["role"] == "analysis"
    assert response["choices"][0]["message"]["content"] is None


def test_nonstandard_null_role_response_without_exact_json_still_fails_closed():
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        recover_structured_json_from_reasoning=True,
    )

    with pytest.raises(ValidationError):
        model._create_chat_result(
            {
                "choices": [{
                    "message": {
                        "role": "analysis",
                        "content": None,
                        "reasoning_content": "not an exact JSON object",
                    },
                    "finish_reason": "stop",
                }],
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"role": "reasoning"}, id="different-nonstandard-role"),
        pytest.param({"content": ""}, id="content-empty-string"),
        pytest.param({"content": []}, id="content-empty-list"),
        pytest.param({"tool_calls": [{"id": "call-1"}]}, id="tool-call-present"),
        pytest.param({"function_call": {"name": "write"}}, id="function-call-present"),
    ],
)
def test_nonstandard_reasoning_normalization_rejects_nearby_message_shapes(mutation):
    message = {
        "role": "analysis",
        "content": None,
        "reasoning_content": '{"ok": true}',
        **mutation,
    }
    response = {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "model": "test-model",
    }

    assert llm_rotation._normalize_nonstandard_structured_reasoning_response(response) is None


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            {
                "choices": [
                    {
                        "message": {
                            "role": "analysis",
                            "content": None,
                            "reasoning_content": '{"ok": true}',
                        },
                        "finish_reason": "length",
                    }
                ]
            },
            id="finish-not-stop",
        ),
        pytest.param(
            {
                "choices": [
                    {
                        "message": {
                            "role": "analysis",
                            "content": None,
                            "reasoning_content": '{"ok": true}',
                        },
                        "finish_reason": "stop",
                    },
                    {
                        "message": {
                            "role": "analysis",
                            "content": None,
                            "reasoning_content": '{"ok": false}',
                        },
                        "finish_reason": "stop",
                    },
                ]
            },
            id="multiple-choices",
        ),
        pytest.param(
            {
                "choices": [
                    {
                        "message": {
                            "role": "analysis",
                            "reasoning_content": '{"ok": true}',
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            id="content-key-missing",
        ),
        pytest.param(
            {
                "choices": [
                    {
                        "message": {
                            "role": "analysis",
                            "content": None,
                            "reasoning_content": "[true]",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            id="reasoning-not-object",
        ),
    ],
)
def test_nonstandard_reasoning_normalization_rejects_nearby_response_shapes(response):
    assert llm_rotation._normalize_nonstandard_structured_reasoning_response(response) is None


def test_structured_reasoning_never_overwrites_visible_content():
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        recover_structured_json_from_reasoning=True,
    )
    result = model._create_chat_result(
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": '{"ok": false}',
                    "reasoning_content": '{"ok": true}',
                },
                "finish_reason": "stop",
            }],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )

    message = result.generations[0].message
    assert message.content == '{"ok": false}'
    assert "reasoning_content" not in message.additional_kwargs
    assert "structured_json_recovered_from_reasoning" not in message.response_metadata


@pytest.mark.parametrize(
    "reasoning_content",
    [
        "analysis then {\"ok\": true}",
        "```json\n{\"ok\": true}\n```",
        '{"ok": true} {"ok": false}',
        "[true]",
        "not json",
    ],
)
def test_empty_reasoning_recovery_rejects_non_object_or_non_exact_json(reasoning_content):
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        recover_structured_json_from_reasoning=True,
    )
    with pytest.raises(
        llm_rotation.ProviderEmptyStructuredOutputError
    ) as caught:
        model._create_chat_result(
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": reasoning_content,
                    },
                    "finish_reason": "stop",
                }],
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )

    assert caught.value.reasoning_disposition == "not_exact_json_object"


def test_nonstream_empty_without_reasoning_is_terminal():
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        recover_structured_json_from_reasoning=True,
    )

    with pytest.raises(
        llm_rotation.ProviderEmptyStructuredOutputError
    ) as caught:
        model._create_chat_result(
            {
                "choices": [{
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }],
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 0,
                    "total_tokens": 1,
                },
            }
        )

    assert caught.value.reasoning_disposition == "missing"


def test_mimo_drops_reasoning_content_but_keeps_reasoning_usage():
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="mimo-v2.5",
        key_group="mimo",
    )
    result = model._create_chat_result(
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "inspect evidence",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "model": "mimo-v2.5",
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }
    )
    assistant = result.generations[0].message
    payload = model._get_request_payload([
        HumanMessage(content="review"),
        assistant,
        ToolMessage(content="evidence", tool_call_id="call-1"),
    ])

    assert "reasoning_content" not in assistant.additional_kwargs
    assert "reasoning_content" not in payload["messages"][1]
    assert assistant.usage_metadata["output_token_details"]["reasoning"] == 5


def test_generate_tries_each_key_once_until_success(three_keys):
    model = llm_rotation.RotatingChatOpenAI(base_url="http://test", api_key="k1", model="test-model")
    calls = {"n": 0}

    def fake_generate(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_rate_limit_error()
        return "SUCCESS"

    with patch.object(ChatOpenAI, "_generate", fake_generate):
        assert model._generate([]) == "SUCCESS"

    assert calls["n"] == 3
    assert llm_rotation._current_key() == "k3"


def test_generate_retries_temporary_stream_error(three_keys):
    model = llm_rotation.RotatingChatOpenAI(base_url="http://test", api_key="k1", model="test-model")
    calls = {"n": 0}

    def fail_stream_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_temporary_stream_error()
        return "SUCCESS"

    with patch.object(ChatOpenAI, "_generate", fail_stream_once):
        assert model._generate([]) == "SUCCESS"

    assert calls["n"] == 2
    assert llm_rotation._current_key() == "k2"


def test_gptload_retries_transient_failures_on_same_entry_with_short_backoff():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")
    failures = iter(
        [
            _make_rate_limit_error(),
            _make_gateway_error(502),
            _make_api_connection_error(),
            _make_read_error(),
        ]
    )
    calls: list[tuple[str, str]] = []

    def fail_then_succeed(self, *args, **kwargs):
        calls.append((self.key_group, str(self.openai_api_base)))
        if len(calls) <= 4:
            raise next(failures)
        return "SUCCESS"

    with (
        patch.object(ChatOpenAI, "_generate", fail_then_succeed),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
    ):
        assert model._generate([]) == "SUCCESS"

    assert calls == [("gptload", llm_rotation.GPTLOAD_BASE_URL)] * 5
    assert [call.args[0] for call in mock_sleep.call_args_list] == [
        2.0,
        5.0,
        10.0,
        20.0,
    ]


def test_gptload_exhaustion_raises_terminal_marker_after_exactly_five_attempts():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")
    calls = {"n": 0}

    def always_limited(self, *args, **kwargs):
        calls["n"] += 1
        raise _make_rate_limit_error()

    with (
        patch.object(ChatOpenAI, "_generate", always_limited),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(llm_rotation.ProviderRetryExhaustedError) as caught,
    ):
        model._generate([])

    assert calls["n"] == 5
    assert caught.value.code == "provider_retry_exhausted"
    assert caught.value.provider_group == "gptload"
    assert caught.value.attempts == 5
    assert isinstance(caught.value.last_error, RateLimitError)
    assert [call.args[0] for call in mock_sleep.call_args_list] == [
        2.0,
        5.0,
        10.0,
        20.0,
    ]


def test_gptload_fifth_failure_exhausts_even_with_long_retry_after():
    error = _make_rate_limit_error(body={"retry_after": 90})

    with pytest.raises(llm_rotation.ProviderRetryExhaustedError) as caught:
        llm_rotation._gptload_retry_decision(
            error,
            provider_group="gptload",
            attempt=5,
            max_attempts=5,
        )

    assert caught.value.attempts == 5
    assert caught.value.last_error is error


def test_gptload_fifth_long_retry_after_does_not_restart_from_checkpoint_layer():
    from src.utils import invoke_with_retry

    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")
    calls = {"n": 0}

    def fail_with_long_hint_last(self, *args, **kwargs):
        calls["n"] += 1
        retry_after = 90 if calls["n"] == 5 else None
        raise _make_rate_limit_error(
            body={"retry_after": retry_after} if retry_after else None
        )

    class _Agent:
        def invoke(self, _payload, _config=None):
            return model._generate([])

    with (
        patch.object(ChatOpenAI, "_generate", fail_with_long_hint_last),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(llm_rotation.ProviderRetryExhaustedError),
    ):
        invoke_with_retry(
            _Agent(),
            [{"role": "user", "content": "hi"}],
            thread_id="project:agent:stage",
        )

    assert calls["n"] == 5
    assert [call.args[0] for call in mock_sleep.call_args_list] == [
        2.0,
        5.0,
        10.0,
        20.0,
    ]


def test_gptload_no_keys_is_terminal_across_outer_retry_boundary():
    from src.utils import invoke_with_retry

    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")

    class _Agent:
        def invoke(self, _payload):
            return model._generate([])

    with (
        patch.object(
            ChatOpenAI,
            "_generate",
            side_effect=_make_503_no_keys_error(),
        ) as generate,
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(llm_rotation.ProviderConfigurationError) as caught,
    ):
        invoke_with_retry(_Agent(), [{"role": "user", "content": "hi"}])

    assert generate.call_count == 1
    assert caught.value.code == "provider_configuration_error"
    assert caught.value.attempts == 1
    mock_sleep.assert_not_called()


def test_gptload_no_keys_stream_is_terminal_without_retry():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload", streaming=True)

    with (
        patch.object(
            ChatOpenAI,
            "_stream",
            side_effect=_make_503_no_keys_error(),
        ) as stream,
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(llm_rotation.ProviderConfigurationError),
    ):
        list(model._stream([]))

    assert stream.call_count == 1
    mock_sleep.assert_not_called()


def test_gptload_retry_after_overrides_short_schedule_without_retrying_early():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")
    calls = {"n": 0}

    def limited_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_rate_limit_error(headers={"Retry-After": "12"})
        return "SUCCESS"

    with (
        patch.object(ChatOpenAI, "_generate", limited_once),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
    ):
        assert model._generate([]) == "SUCCESS"

    mock_sleep.assert_called_once_with(12.0)


def test_retry_after_http_date_is_converted_to_relative_delay():
    error = _make_rate_limit_error(
        headers={"Retry-After": "Mon, 07 Sep 2026 12:01:30 GMT"}
    )
    now = llm_rotation.datetime.fromisoformat(
        "2026-09-07T12:00:40+00:00"
    ).timestamp()

    assert llm_rotation._retry_after_seconds(error, now=now) == 50.0


def test_gptload_long_retry_after_defers_to_checkpoint_without_sleeping():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")
    error = _make_rate_limit_error(body={"retry_after": 90})

    with (
        patch.object(ChatOpenAI, "_generate", side_effect=error) as generate,
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(llm_rotation.ProviderRetryDeferredError) as caught,
    ):
        model._generate([])

    assert generate.call_count == 1
    assert caught.value.code == "provider_retry_deferred"
    assert caught.value.attempts == 1
    assert caught.value.max_attempts == llm_rotation.GPTLOAD_MAX_ATTEMPTS == 5
    assert caught.value.retry_after_seconds == 90.0
    assert caught.value.next_retry_at.endswith("+00:00")
    mock_sleep.assert_not_called()


def test_resumed_provider_sequence_honors_remaining_global_attempt_budget():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")
    error = _make_503_service_busy_error()

    with (
        llm_rotation.provider_retry_attempt_offset(4),
        patch.object(ChatOpenAI, "_generate", side_effect=error) as generate,
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(llm_rotation.ProviderRetryExhaustedError) as caught,
    ):
        model._generate([])

    assert generate.call_count == 1
    assert caught.value.attempts == 5
    assert caught.value.max_attempts == 5
    mock_sleep.assert_not_called()


def test_provider_attempt_offset_is_consumed_by_only_first_sequence():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    first_model = llm_rotation.build_chat_model(group="gptload")
    second_model = llm_rotation.build_chat_model(group="gptload")
    calls = {"n": 0}

    def first_succeeds_then_second_exhausts(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "SUCCESS"
        raise _make_503_service_busy_error()

    with (
        llm_rotation.provider_retry_attempt_offset(4) as scope,
        patch.object(ChatOpenAI, "_generate", first_succeeds_then_second_exhausts),
        patch.object(llm_rotation.time, "sleep"),
        pytest.raises(llm_rotation.ProviderRetryExhaustedError) as caught,
    ):
        assert scope.consumed is False
        assert first_model._generate([]) == "SUCCESS"
        assert scope.consumed is True
        second_model._generate([])

    assert calls["n"] == 6
    assert caught.value.attempts == 5


def test_deferred_error_calculates_remaining_wait_from_next_retry_at():
    error = llm_rotation.ProviderRetryDeferredError(
        provider_group="gptload",
        attempts=1,
        last_error=ConnectionError("retry later"),
        retry_after_seconds=90.0,
        next_retry_at="2026-09-07T12:01:30+00:00",
    )

    remaining = error.remaining_delay_seconds(
        now=llm_rotation.datetime.fromisoformat("2026-09-07T12:00:40+00:00")
    )

    assert remaining == 50.0


@pytest.mark.parametrize("status", [502, 524])
def test_gptload_recognizes_gateway_statuses_as_transient(status):
    assert llm_rotation._is_rotatable_error(
        _make_gateway_error(status), "gptload"
    ) is True


def test_gptload_recognizes_read_interruption_as_transient():
    assert llm_rotation._is_rotatable_error(_make_read_error(), "gptload") is True


def test_permanent_dns_errors_are_not_safe_to_replay_across_wrapper_shapes(monkeypatch):
    monkeypatch.setattr(llm_rotation.socket, "EAI_NONAME", -9101, raising=False)
    monkeypatch.setattr(llm_rotation.socket, "EAI_FAIL", -9102, raising=False)

    errors = [
        _wrap_dns_error(-9101),
        _wrap_dns_error(-9102, use_context=True),
        _wrap_dns_error(-9101, wrap_openai=True),
    ]

    for error in errors:
        assert llm_rotation._is_safe_transport_replay(error) is False
        assert llm_rotation._is_rotatable_error(error, "gptload") is False


def test_temporary_dns_error_remains_safe_to_replay(monkeypatch):
    monkeypatch.setattr(llm_rotation.socket, "EAI_AGAIN", -9103, raising=False)
    error = _wrap_dns_error(-9103, wrap_openai=True)

    assert llm_rotation._is_safe_transport_replay(error) is True
    assert llm_rotation._is_rotatable_error(error, "gptload") is True


@pytest.mark.parametrize(
    "error", [_make_bad_request_error(), _make_unprocessable_entity_error()]
)
def test_gptload_does_not_retry_deterministic_request_errors(error):
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_chat_model(group="gptload")

    with (
        patch.object(ChatOpenAI, "_generate", side_effect=error) as generate,
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(type(error)),
    ):
        model._generate([])

    assert generate.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        _make_bad_request_error(),
        _make_401_auth_error(),
        _make_403_permission_error(),
        _make_insufficient_quota_error(),
        _make_404_not_found_error(),
        _make_unprocessable_entity_error(),
    ],
)
def test_gptload_does_not_rotate_credentials_for_deterministic_statuses(error):
    _seed_mimo_keys(
        [("gptload-k1", "gptload"), ("gptload-k2", "gptload")]
    )
    model = llm_rotation.build_chat_model(group="gptload")

    with (
        patch.object(ChatOpenAI, "_generate", side_effect=error) as generate,
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(type(error)),
    ):
        model._generate([])

    assert llm_rotation._is_rotatable_error(error, "gptload") is False
    assert generate.call_count == 1
    mock_sleep.assert_not_called()


def test_gptload_stream_does_not_rotate_credentials_for_auth_error():
    _seed_mimo_keys(
        [("gptload-k1", "gptload"), ("gptload-k2", "gptload")]
    )
    model = llm_rotation.build_chat_model(group="gptload")

    with (
        patch.object(
            ChatOpenAI, "_stream", side_effect=_make_401_auth_error()
        ) as stream,
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(openai.AuthenticationError),
    ):
        list(model._stream([]))

    assert stream.call_count == 1
    mock_sleep.assert_not_called()


def test_gptload_structured_stream_retries_observed_transient_model_not_found():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def unavailable_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_transient_gptload_model_not_found_error()
        yield ChatGenerationChunk(
            message=AIMessageChunk(content='{"ok": true}')
        )

    with (
        patch.object(ChatOpenAI, "_stream", unavailable_once),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
    ):
        result = model.with_structured_output(
            _StructuredVerdict, method="json_mode"
        ).invoke("check")

    assert result == _StructuredVerdict(ok=True)
    assert calls["n"] == 2
    mock_sleep.assert_called_once_with(2.0)


def test_bare_structured_invoke_uses_gptload_provider_retry_boundary():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def unavailable_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_503_service_busy_error()
        yield ChatGenerationChunk(
            message=AIMessageChunk(content='{"ok": true}')
        )

    with (
        patch.object(ChatOpenAI, "_stream", unavailable_once),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
    ):
        result = model.with_structured_output(
            _StructuredVerdict, method="json_mode"
        ).invoke("check")

    assert result == _StructuredVerdict(ok=True)
    assert calls["n"] == 2
    mock_sleep.assert_called_once_with(2.0)


def test_bare_structured_invoke_retries_observed_http2_stream_failure():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def http2_failure_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_http2_stream_error()
        yield ChatGenerationChunk(
            message=AIMessageChunk(content='{"ok": true}')
        )

    with (
        patch.object(ChatOpenAI, "_stream", http2_failure_once),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
    ):
        result = model.with_structured_output(
            _StructuredVerdict, method="json_mode"
        ).invoke("check")

    assert result == _StructuredVerdict(ok=True)
    assert calls["n"] == 2
    mock_sleep.assert_called_once_with(2.0)


def test_bare_structured_invoke_retries_snapshotless_sdk_stream():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def snapshotless_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            _raise_empty_completion_snapshot_assertion()
        yield ChatGenerationChunk(
            message=AIMessageChunk(content='{"ok": true}')
        )

    with (
        patch.object(ChatOpenAI, "_stream", snapshotless_once),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
    ):
        result = model.with_structured_output(
            _StructuredVerdict, method="json_mode"
        ).invoke("check")

    assert result == _StructuredVerdict(ok=True)
    assert calls["n"] == 2
    mock_sleep.assert_called_once_with(2.0)


def test_stream_verification_recovers_exact_json_from_empty_reasoning_content():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()

    def empty_content_with_json_reasoning(self, *args, **kwargs):
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": '{"ok": true}'},
            ),
            generation_info={"finish_reason": "stop"},
        )

    with patch.object(ChatOpenAI, "_stream", empty_content_with_json_reasoning):
        result = model.with_structured_output(
            _StructuredVerdict,
            method="json_mode",
            include_raw=True,
        ).invoke("check")

    assert result["parsed"] == _StructuredVerdict(ok=True)
    assert "reasoning_content" not in result["raw"].additional_kwargs
    assert result["raw"].response_metadata[
        "structured_json_recovered_from_reasoning"
    ] is True


def test_stream_reasoning_recovery_still_requires_target_schema_without_replay():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def empty_content_with_wrong_json(self, *args, **kwargs):
        calls["n"] += 1
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": '{"wrong": true}'},
            ),
            generation_info={"finish_reason": "stop"},
        )

    with (
        patch.object(ChatOpenAI, "_stream", empty_content_with_wrong_json),
        pytest.raises(Exception),
    ):
        bind_structured_json(model, _StructuredVerdict).invoke("check")

    assert calls["n"] == 1


def test_stream_empty_without_exact_json_reasoning_is_terminal_without_replay():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def empty_response(self, *args, **kwargs):
        calls["n"] += 1
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=""),
            generation_info={"finish_reason": "stop"},
        )

    with (
        patch.object(ChatOpenAI, "_stream", empty_response),
        pytest.raises(
            llm_rotation.ProviderEmptyStructuredOutputError
        ) as caught,
    ):
        bind_structured_json(model, _StructuredVerdict).invoke("check")

    assert calls["n"] == 1
    assert caught.value.reasoning_disposition == "missing"


def test_stream_without_any_chunks_is_terminal_without_replay():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def no_chunks(self, *args, **kwargs):
        calls["n"] += 1
        if False:
            yield None

    with (
        patch.object(ChatOpenAI, "_stream", no_chunks),
        pytest.raises(
            llm_rotation.ProviderEmptyStructuredOutputError
        ) as caught,
    ):
        bind_structured_json(model, _StructuredVerdict).invoke("check")

    assert calls["n"] == 1
    assert caught.value.reasoning_disposition == "missing"


def test_structured_json_wrapper_does_not_replay_exhausted_provider_sequence():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model()
    calls = {"n": 0}

    def always_unavailable(self, *args, **kwargs):
        calls["n"] += 1
        raise _make_503_service_busy_error()
        yield  # pragma: no cover

    with (
        patch.object(ChatOpenAI, "_stream", always_unavailable),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
        pytest.raises(llm_rotation.ProviderRetryExhaustedError),
    ):
        bind_structured_json(model, _StructuredVerdict).invoke("check")

    assert calls["n"] == 5
    assert [call.args[0] for call in mock_sleep.call_args_list] == [
        2.0,
        5.0,
        10.0,
        20.0,
    ]


def test_stream_discards_partial_chunks_before_rotating_key(three_keys):
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        streaming=True,
    )
    calls = {"n": 0}

    def partial_then_success(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body (incomplete chunked read)")
        yield ChatGenerationChunk(message=AIMessageChunk(content="complete"))

    with patch.object(ChatOpenAI, "_stream", partial_then_success):
        chunks = list(model._stream([]))

    assert [chunk.text for chunk in chunks] == ["complete"]
    assert calls["n"] == 2
    assert llm_rotation._current_key() == "k2"


def test_stream_discards_partial_tool_call_before_rotating_key(three_keys):
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        streaming=True,
    )
    calls = {"n": 0}

    def partial_tool_then_success(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "write_agent_output",
                            "args": '{"day":',
                            "id": "partial-call",
                            "index": 0,
                        }
                    ],
                )
            )
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)"
            )
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": "write_agent_output",
                        "args": '{"day": 1}',
                        "id": "complete-call",
                        "index": 0,
                    }
                ],
            )
        )

    with patch.object(ChatOpenAI, "_stream", partial_tool_then_success):
        chunks = list(model._stream([]))

    assert len(chunks) == 1
    assert chunks[0].message.tool_call_chunks == [
        {
            "name": "write_agent_output",
            "args": '{"day": 1}',
            "id": "complete-call",
            "index": 0,
            "type": "tool_call_chunk",
        }
    ]
    assert calls["n"] == 2
    assert llm_rotation._current_key() == "k2"


def test_stream_uses_configured_provider_fallback():
    _seed_mimo_keys([("gptload-k1", "gptload")])
    _seed_keys("cunai", ["cunai-k1"])
    model = llm_rotation.build_chat_model_with_fallback(
        chain=("gptload", "cunai"),
        streaming=True,
    )
    calls: list[str] = []

    def first_provider_fails(self, *args, **kwargs):
        calls.append(self.key_group)
        if self.key_group == "gptload":
            raise _make_504_gateway_timeout_error()
        yield ChatGenerationChunk(message=AIMessageChunk(content="fallback-ok"))

    with (
        patch.object(ChatOpenAI, "_stream", first_provider_fails),
        patch.object(llm_rotation.time, "sleep") as mock_sleep,
    ):
        chunks = list(model._stream([]))

    assert [chunk.text for chunk in chunks] == ["fallback-ok"]
    assert calls == ["gptload"] * 5 + ["cunai"]
    assert [call.args[0] for call in mock_sleep.call_args_list] == [
        2.0,
        5.0,
        10.0,
        20.0,
    ]
    assert model.key_group == "cunai"


def test_stream_does_not_rotate_deterministic_request_error(three_keys):
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="k1",
        model="test-model",
        streaming=True,
    )

    def invalid_request(self, *args, **kwargs):
        raise _make_bad_request_error()
        yield  # pragma: no cover

    with patch.object(ChatOpenAI, "_stream", invalid_request), pytest.raises(openai.BadRequestError):
        list(model._stream([]))

    assert llm_rotation._current_key() == "k1"


def test_generate_exhaustion_does_not_replay_key_pool(three_keys):
    model = llm_rotation.RotatingChatOpenAI(base_url="http://test", api_key="k1", model="test-model")
    calls = {"n": 0}

    def always_limited(self, *args, **kwargs):
        calls["n"] += 1
        raise _make_rate_limit_error()

    with patch.object(ChatOpenAI, "_generate", always_limited), pytest.raises(RateLimitError):
        model._generate([])

    assert calls["n"] == 3


def test_single_key_is_not_retried_in_place():
    _seed_keys("full", ["only-key"])
    model = llm_rotation.RotatingChatOpenAI(base_url="http://test", api_key="only-key", model="test-model")
    calls = {"n": 0}

    def always_limited(self, *args, **kwargs):
        calls["n"] += 1
        raise _make_rate_limit_error()

    with patch.object(ChatOpenAI, "_generate", always_limited), pytest.raises(RateLimitError):
        model._generate([])

    assert calls["n"] == 1


def test_authentication_failure_tries_next_configured_key(three_keys):
    model = llm_rotation.RotatingChatOpenAI(base_url="http://test", api_key="k1", model="test-model")
    calls = {"n": 0}

    def first_invalid(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_401_auth_error()
        return "SUCCESS"

    with patch.object(ChatOpenAI, "_generate", first_invalid):
        assert model._generate([]) == "SUCCESS"

    assert calls["n"] == 2
    assert llm_rotation._current_key() == "k2"


def test_non_openai_error_is_raised_without_rotation(three_keys):
    model = llm_rotation.RotatingChatOpenAI(base_url="http://test", api_key="k1", model="test-model")

    with patch.object(ChatOpenAI, "_generate", side_effect=ValueError("bad local input")):
        with pytest.raises(ValueError, match="bad local input"):
            model._generate([])

    assert llm_rotation._current_key() == "k1"


def test_build_chat_model_runtime_override_and_sdk_retry_policy(monkeypatch):
    from src.api import runtime_config

    _seed_keys("full", ["k1"])
    monkeypatch.setattr(llm_rotation, "LLM_BASE_URL", "http://test")
    monkeypatch.setattr(llm_rotation, "LLM_MODEL", "default-model")
    monkeypatch.setattr(llm_rotation, "CHAT_MODEL_TIMEOUT_SECONDS", 42.0)
    monkeypatch.setattr("src.config.LLM_MODEL", "default-model")
    runtime_config.set_model_override("runtime-model")
    try:
        model = llm_rotation.build_chat_model()
    finally:
        runtime_config.set_model_override(None)

    assert model.model_name == "runtime-model"
    assert model.request_timeout == 42.0
    assert model.max_retries == 0


def test_build_chat_model_light_group_uses_independent_model(monkeypatch):
    _seed_keys("light", ["light-k1"])
    monkeypatch.setattr(llm_rotation, "LLM_BASE_URL", "http://test")
    monkeypatch.setattr(llm_rotation, "LLM_LIGHT_MODEL", "light-model")
    monkeypatch.setattr(llm_rotation, "LIGHT_MODEL_TIMEOUT_SECONDS", 17.0)
    monkeypatch.setattr("src.config.LLM_LIGHT_MODEL", "light-model")

    model = llm_rotation.build_chat_model(group="light")

    assert model.key_group == "light"
    assert model.model_name == "light-model"
    assert model.request_timeout == 17.0


@pytest.mark.parametrize("model_name", ["mimo-v2.5", "mimo-v2.5-pro"])
def test_build_chat_model_mimo_supports_standard_and_pro(model_name):
    _seed_mimo_keys([("mimo-k1", "legacy")])

    model = llm_rotation.build_chat_model(group="mimo", mimo_model=model_name)

    assert model.key_group == "mimo"
    assert model.model_name == model_name
    assert model.mimo_model == model_name


def test_build_chat_model_cunai_uses_direct_channel_headers():
    _seed_keys("cunai", ["cunai-k1"])

    model = llm_rotation.build_chat_model(group="cunai")

    assert model.key_group == "cunai"
    assert model.default_headers == {"User-Agent": "curl/8.0"}


@pytest.mark.parametrize(
    ("group", "base_url", "expected"),
    [
        (
            "gptload",
            "http://gateway.test/proxy/glm-flash/v1",
            {"User-Agent": "curl/8.0"},
        ),
        (
            "deepseek",
            "http://gateway.test/proxy/glm-flash/v1",
            {"User-Agent": "curl/8.0"},
        ),
        (
            "mimo",
            "http://gateway.test/proxy/cun_copy/v1",
            {"User-Agent": "curl/8.0"},
        ),
        ("mimo", "https://api.xiaomimimo.com/v1", None),
        ("jiji", "https://jiji.example/v1", None),
    ],
)
def test_channel_default_headers_follow_group_and_proxy_contract(
    group,
    base_url,
    expected,
):
    assert llm_rotation._channel_default_headers(group, base_url) == expected


def test_build_chat_model_cun_gptload_uses_cunai_headers(monkeypatch):
    _seed_mimo_keys([("gateway-k1", "gptload")])
    monkeypatch.setattr(
        llm_rotation,
        "MIMO_GPTLOAD_BASE_URL",
        "http://gateway.test/proxy/cun_copy/v1",
    )

    model = llm_rotation.build_chat_model(group="mimo")

    assert model.default_headers == {"User-Agent": "curl/8.0"}


def test_build_chat_model_regular_mimo_keeps_default_headers():
    _seed_mimo_keys([("mimo-k1", "legacy")])

    model = llm_rotation.build_chat_model(group="mimo")

    assert model.default_headers is None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_make_rate_limit_error(), id="429"),
        pytest.param(_make_503_service_busy_error(), id="503"),
        pytest.param(_make_504_gateway_timeout_error(), id="504"),
        pytest.param(_make_api_connection_error(), id="connection"),
        pytest.param(_make_temporary_stream_error(), id="temporary-stream"),
        pytest.param(_make_http2_stream_error(), id="http2-stream"),
    ],
)
def test_transient_errors_are_rotatable(error):
    for group in ("full", "light", "mimo", "gptload", "deepseek", "cunai"):
        assert llm_rotation._is_rotatable_error(error, group) is True


def test_key_quota_rotation_is_preserved_except_for_gptload():
    error = _make_insufficient_quota_error()

    for group in ("full", "light", "mimo", "cunai"):
        assert llm_rotation._is_rotatable_error(error, group) is True
    for group in ("gptload", "deepseek"):
        assert llm_rotation._is_rotatable_error(error, group) is False


def test_cunai_tool_choice_compatibility_error_is_scoped_to_cunai():
    error = _make_cunai_tool_choice_error()
    assert llm_rotation._is_rotatable_error(error, "cunai") is True
    assert llm_rotation._is_rotatable_error(error, "full") is False


def test_generic_stream_api_error_is_not_rotatable():
    assert llm_rotation._is_rotatable_error(_make_generic_api_error(), "gptload") is False
    assert llm_rotation.is_temporary_stream_error(_make_generic_api_error()) is False


def test_temporary_stream_error_is_recognized():
    assert llm_rotation.is_temporary_stream_error(_make_temporary_stream_error()) is True


def test_http2_stream_error_is_recognized():
    assert llm_rotation.is_temporary_stream_error(_make_http2_stream_error()) is True


def test_snapshotless_openai_sdk_assertion_is_recognized():
    with pytest.raises(AssertionError) as caught:
        _raise_empty_completion_snapshot_assertion()

    assert llm_rotation.is_empty_completion_snapshot_assertion(caught.value) is True
    assert llm_rotation._is_rotatable_error(caught.value, "gptload") is True


def test_generic_assertion_is_not_a_transport_retry():
    try:
        raise AssertionError()
    except AssertionError as error:
        assert llm_rotation.is_empty_completion_snapshot_assertion(error) is False
        assert llm_rotation._is_rotatable_error(error, "gptload") is False


def test_incomplete_chunked_stream_error_is_recognized():
    error = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )

    assert llm_rotation.is_temporary_stream_error(error) is True


def test_temporary_503_stream_error_is_recognized():
    response = httpx.Response(
        503,
        request=httpx.Request("POST", "http://test"),
        json={"error": {"message": "Service temporarily unavailable", "type": "api_error"}},
    )
    error = openai.InternalServerError(
        "Service temporarily unavailable",
        response=response,
        body=response.json(),
    )

    assert llm_rotation.is_temporary_stream_error(error) is True


def test_channel_switch_error_classification():
    assert llm_rotation._is_channel_switch_error(_make_401_auth_error()) is True
    assert llm_rotation._is_channel_switch_error(_make_403_permission_error()) is True
    assert llm_rotation._is_channel_switch_error(_make_503_no_keys_error()) is True
    assert llm_rotation._is_channel_switch_error(_make_503_service_busy_error()) is False
    assert llm_rotation._is_channel_switch_error(_make_rate_limit_error()) is False
    assert llm_rotation._is_channel_switch_error(_make_insufficient_quota_error()) is False


def test_explicit_fallback_switches_from_mimo_to_cunai_and_uses_pro_model_first():
    _seed_keys("cunai", ["cunai-k1"])
    _seed_mimo_keys([("mimo-k1", "legacy")])
    model = llm_rotation.build_chat_model_with_fallback(
        chain=("mimo", "cunai"),
        mimo_model="mimo-v2.5-pro",
    )
    calls: list[tuple[str, str]] = []

    def fake_generate(self, *args, **kwargs):
        calls.append((self.key_group, self.model_name))
        if self.key_group == "mimo":
            raise _make_rate_limit_error()
        return "SUCCESS"

    with patch.object(ChatOpenAI, "_generate", fake_generate):
        assert model._generate([]) == "SUCCESS"

    assert calls == [("mimo", "mimo-v2.5-pro"), ("cunai", llm_rotation.CUNAI_CHAT_MODEL)]
    assert model.key_group == "cunai"


def test_build_chat_model_with_fallback_can_enable_streaming():
    _seed_mimo_keys([("deepseek-k1", "gptload")])

    model = llm_rotation.build_chat_model_with_fallback(streaming=True)

    assert model.streaming is True


def test_verification_uses_gptload_provider_only():
    _seed_keys("full", ["full-k1", "full-k2"])
    _seed_mimo_keys([("deepseek-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model(request_max_tokens=32000)
    calls: list[tuple[str, str, int | None]] = []

    def fake_generate(self, *args, **kwargs):
        calls.append(
            (
                self.key_group,
                self.model_name,
                (self.extra_body or {}).get("max_tokens"),
            )
        )
        if self.key_group == "full":
            raise _make_rate_limit_error()
        return "SUCCESS"

    with patch.object(ChatOpenAI, "_generate", fake_generate):
        assert model._generate([]) == "SUCCESS"

    assert calls == [("gptload", llm_rotation.GPTLOAD_CHAT_MODEL, 32000)]
    assert model.fallback_chain == ("gptload",)
    assert model.key_group == "gptload"
    assert model.streaming is True


def test_gptload_group_ignores_non_gptload_mimo_credentials():
    _seed_mimo_keys(
        [
            ("token-plan-k1", "token_plan"),
            ("deepseek-k1", "gptload"),
            ("legacy-mimo-k1", "legacy"),
        ]
    )

    model = llm_rotation.build_verification_chat_model()

    assert model.key_group == "gptload"
    assert model.openai_api_key.get_secret_value() == "deepseek-k1"
    assert model.model_name == llm_rotation.GPTLOAD_CHAT_MODEL


def test_jiji_group_uses_environment_credentials_without_runtime_key_store(monkeypatch):
    monkeypatch.setattr(llm_rotation, "JIJI_API_KEYS", ["jiji-k1"])
    monkeypatch.setattr(llm_rotation, "JIJI_BASE_URL", "https://jiji.example/v1")
    monkeypatch.setattr(llm_rotation, "JIJI_CHAT_MODEL", "glm-5.3-flash")

    model = llm_rotation.build_chat_model(group="jiji", streaming=True)

    assert model.key_group == "jiji"
    assert model.openai_api_key.get_secret_value() == "jiji-k1"
    assert model.openai_api_base == "https://jiji.example/v1"
    assert model.model_name == "glm-5.3-flash"
    assert model.streaming is True


def test_primary_long_and_verification_chains_can_select_jiji(monkeypatch):
    monkeypatch.setattr(llm_rotation, "JIJI_API_KEYS", ["jiji-k1"])
    monkeypatch.setattr(llm_rotation, "JIJI_BASE_URL", "https://jiji.example/v1")
    monkeypatch.setattr(llm_rotation, "JIJI_CHAT_MODEL", "glm-5.3-flash")
    monkeypatch.setattr(llm_rotation, "_CHANNEL_FALLBACK_CHAIN", ("jiji",))
    monkeypatch.setattr(llm_rotation, "_VERIFICATION_FALLBACK_CHAIN", ("jiji",))

    long_model = llm_rotation.build_chat_model_with_fallback(streaming=True)
    verification_model = llm_rotation.build_verification_chat_model(
        request_max_tokens=16000,
        disable_reasoning=True,
    )

    assert long_model.key_group == "jiji"
    assert long_model.fallback_chain == ("jiji",)
    assert verification_model.key_group == "jiji"
    assert verification_model.fallback_chain == ("jiji",)
    assert verification_model.streaming is True
    assert verification_model.max_tokens is None
    assert verification_model.extra_body == {
        "thinking": {"type": "disabled"},
        "max_tokens": 16000,
    }
    assert llm_rotation.primary_travel_model_identity() == {
        "provider_group": "jiji",
        "provider": "jiji.cc",
        "model": "glm-5.3-flash",
    }


def test_poi_recovery_does_not_mix_ooioo_into_jiji_run(monkeypatch):
    monkeypatch.setattr(llm_rotation, "JIJI_API_KEYS", ["jiji-k1"])
    monkeypatch.setattr(llm_rotation, "JIJI_BASE_URL", "https://jiji.example/v1")
    monkeypatch.setattr(llm_rotation, "JIJI_CHAT_MODEL", "glm-5.3-flash")
    monkeypatch.setattr(llm_rotation, "OOIOO_API_KEYS", ["ooioo-k1"])
    monkeypatch.setattr(llm_rotation, "_CHANNEL_FALLBACK_CHAIN", ("jiji",))

    model = llm_rotation.build_poi_recovery_chat_model()

    assert model.key_group == "jiji"
    assert llm_rotation.poi_recovery_model_identity() == {
        "provider": "jiji",
        "model": "glm-5.3-flash",
    }


def test_narrow_structured_factories_share_one_verification_boundary(monkeypatch):
    sentinel = object()
    calls = []

    def fake_builder(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(llm_rotation, "build_verification_chat_model", fake_builder)

    assert config.get_structured_chat_model(temperature=0, max_tokens=16000) is sentinel
    assert config.get_verification_chat_model(temperature=0, max_tokens=32000) is sentinel
    assert config.get_mimo_verification_chat_model(temperature=0) is sentinel
    assert calls == [
        {
            "temperature": 0,
            "request_max_tokens": 16000,
            "disable_reasoning": False,
        },
        {
            "temperature": 0,
            "request_max_tokens": 32000,
            "disable_reasoning": False,
        },
        {
            "temperature": 0,
            "request_max_tokens": None,
            "disable_reasoning": False,
        },
    ]


def test_verification_applies_request_controls_to_gptload():
    _seed_keys("full", ["full-k1"])
    _seed_mimo_keys([("deepseek-k1", "gptload")])
    model = llm_rotation.build_verification_chat_model(
        request_max_tokens=16000,
        disable_reasoning=True,
    )

    assert model.max_tokens is None
    assert model.extra_body == {
        "thinking": {"type": "disabled"},
        "max_tokens": 16000,
    }
    assert model.request_max_tokens == 16000
    assert model.request_extra_body == {"thinking": {"type": "disabled"}}
    assert model.full_extra_body is None
    payload = model._get_request_payload([HumanMessage(content="return json")])
    assert "max_completion_tokens" not in payload
    assert payload["extra_body"] == {
        "thinking": {"type": "disabled"},
        "max_tokens": 16000,
    }


def test_openai_sdk_sends_provider_native_max_tokens_in_final_http_payload():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "glm-5.3-flash",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"ok": true}',
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        model = llm_rotation.RotatingChatOpenAI(
            base_url="http://test/v1",
            api_key="k1",
            model="glm-5.3-flash",
            max_retries=0,
            http_client=http_client,
            extra_body={
                "thinking": {"type": "disabled"},
                "max_tokens": 16000,
            },
            request_max_tokens=16000,
            request_extra_body={"thinking": {"type": "disabled"}},
        )
        model.invoke("return json")

    assert requests[0]["max_tokens"] == 16000
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "max_completion_tokens" not in requests[0]


def test_request_controls_survive_switch_from_gptload_to_jiji(monkeypatch):
    _seed_mimo_keys([("gptload-k1", "gptload")])
    monkeypatch.setattr(llm_rotation, "JIJI_API_KEYS", ["jiji-k1"])
    monkeypatch.setattr(llm_rotation, "JIJI_BASE_URL", "https://jiji.example/v1")
    monkeypatch.setattr(llm_rotation, "JIJI_CHAT_MODEL", "glm-5.3-flash")
    controls = {"thinking": {"type": "disabled"}}
    resolved_controls = {**controls, "max_tokens": 16000}
    model = llm_rotation.build_chat_model(
        group="gptload",
        fallback_chain=("gptload", "jiji"),
        request_max_tokens=16000,
        request_extra_body=controls,
    )
    assert (model.key_group, model.max_tokens, model.extra_body) == (
        "gptload",
        None,
        resolved_controls,
    )

    model._switch_to_group("jiji")

    assert (model.key_group, model.max_tokens, model.extra_body) == (
        "jiji",
        None,
        resolved_controls,
    )


def test_full_only_controls_keep_existing_long_text_scope():
    _seed_keys("full", ["full-k1"])
    _seed_mimo_keys([("gptload-k1", "gptload")])
    full_options = {"thinking": {"type": "disabled"}}
    request_options = {"thinking": {"type": "enabled"}}

    full_model = llm_rotation.build_chat_model(
        group="full",
        sensetime_max_tokens=32000,
        full_extra_body=full_options,
    )
    gptload_model = llm_rotation.build_chat_model(
        group="gptload",
        sensetime_max_tokens=32000,
        full_extra_body=full_options,
        request_max_tokens=16000,
        request_extra_body=request_options,
    )

    assert full_model.max_tokens == 32000
    assert full_model.extra_body == full_options
    assert gptload_model.max_tokens is None
    assert gptload_model.extra_body == {**request_options, "max_tokens": 16000}

    gptload_model._switch_to_group("full")

    assert gptload_model.max_tokens == 32000
    assert gptload_model.extra_body == full_options


def test_deterministic_channel_error_switches_immediately():
    _seed_keys("cunai", ["cunai-k1", "cunai-k2"])
    _seed_mimo_keys([("mimo-k1", "legacy")])
    model = llm_rotation.build_chat_model_with_fallback(chain=("mimo", "cunai"))
    calls: list[str] = []

    def fake_generate(self, *args, **kwargs):
        calls.append(self.key_group)
        if self.key_group == "mimo":
            raise _make_403_permission_error()
        return "SUCCESS"

    with patch.object(ChatOpenAI, "_generate", fake_generate):
        assert model._generate([]) == "SUCCESS"

    assert calls == ["mimo", "cunai"]


def test_category_a_request_error_does_not_switch_channel():
    _seed_keys("cunai", ["cunai-k1"])
    _seed_mimo_keys([("mimo-k1", "legacy")])
    model = llm_rotation.build_chat_model_with_fallback(chain=("mimo", "cunai"))
    calls: list[str] = []

    def bad_request(self, *args, **kwargs):
        calls.append(self.key_group)
        raise _make_bad_request_error()

    with patch.object(ChatOpenAI, "_generate", bad_request), pytest.raises(Exception):
        model._generate([])

    assert calls == ["mimo"]


def test_all_channels_are_attempted_only_once():
    _seed_keys("cunai", ["cunai-k1"])
    _seed_mimo_keys([("mimo-k1", "legacy")])
    model = llm_rotation.build_chat_model_with_fallback(chain=("mimo", "cunai"))
    calls: list[str] = []

    def always_limited(self, *args, **kwargs):
        calls.append(self.key_group)
        raise _make_rate_limit_error()

    with patch.object(ChatOpenAI, "_generate", always_limited), pytest.raises(RateLimitError):
        model._generate([])

    assert calls == ["mimo", "cunai"]


def test_build_fallback_filters_unconfigured_channel():
    _seed_mimo_keys([("deepseek-k1", "gptload")])

    model = llm_rotation.build_chat_model_with_fallback(mimo_model="mimo-v2.5-pro")

    assert model.key_group == "gptload"
    assert model.fallback_chain == ("gptload",)
    assert model.model_name == llm_rotation.GPTLOAD_CHAT_MODEL


def test_build_fallback_never_falls_back_to_sensetime_when_chain_is_empty():
    _seed_keys("full", ["sensetime-k1"])

    with pytest.raises(RuntimeError, match="不会静默切换到其他Provider"):
        llm_rotation.build_chat_model_with_fallback()


def test_poi_recovery_uses_ooioo_when_configured(monkeypatch):
    monkeypatch.setattr(llm_rotation, "OOIOO_API_KEYS", ["ooioo-k1"])
    monkeypatch.setattr(llm_rotation, "OOIOO_BASE_URL", "https://ooioo.example/v1")
    monkeypatch.setattr(llm_rotation, "OOIOO_CHAT_MODEL", "deepseek-v4-flash")

    model = llm_rotation.build_poi_recovery_chat_model()

    assert model.key_group == "ooioo"
    assert model.openai_api_key.get_secret_value() == "ooioo-k1"
    assert model.openai_api_base == "https://ooioo.example/v1"
    assert model.model_name == "deepseek-v4-flash"
    assert model.default_headers == {"User-Agent": "curl/8.0"}
    assert llm_rotation.poi_recovery_model_identity() == {
        "provider": "ooioo",
        "model": "deepseek-v4-flash",
    }


def test_poi_recovery_keeps_old_gptload_behavior_when_ooioo_is_missing(monkeypatch):
    monkeypatch.setattr(llm_rotation, "OOIOO_API_KEYS", [])
    _seed_mimo_keys([("gptload-k1", "gptload")])

    model = llm_rotation.build_poi_recovery_chat_model()

    assert model.key_group == "gptload"
    assert model.model_name == llm_rotation.GPTLOAD_CHAT_MODEL
    assert llm_rotation.poi_recovery_model_identity() == {
        "provider": "gptload",
        "model": llm_rotation.GPTLOAD_CHAT_MODEL,
    }


def test_usd_credit_quota_code_remains_gateway_compatible():
    error = _make_insufficient_quota_error("insufficient_user_usd_credit")

    assert llm_rotation._is_insufficient_quota_error(error) is True
    assert llm_rotation._is_rotatable_error(error, "mimo") is True
    assert llm_rotation._is_channel_switch_error(error) is False


def test_cunai_insufficient_quota_rotates_to_next_key_before_fallback():
    _seed_keys("cunai", ["cunai-k1", "cunai-k2", "cunai-k3"])
    model = llm_rotation.RotatingChatOpenAI(
        base_url="http://test",
        api_key="cunai-k1",
        model="test-model",
        key_group="cunai",
    )
    calls = {"n": 0}

    def quota_then_success(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_insufficient_quota_error()
        return "SUCCESS"

    with patch.object(ChatOpenAI, "_generate", quota_then_success):
        assert model._generate([]) == "SUCCESS"

    assert calls["n"] == 3
    assert llm_rotation._current_key("cunai") == "cunai-k3"


def test_companion_answer_chain_has_three_levels():
    _seed_keys("cunai", ["cunai-k1"])
    _seed_mimo_keys([("mimo-k1", "legacy")])
    _seed_keys("full", ["full-k1"])

    model = llm_rotation.build_companion_chat_model()

    assert model.fallback_chain == ("mimo", "full")
    assert model.mimo_model == llm_rotation.MIMO_CHAT_MODEL


def test_companion_router_chain_prefers_light_model():
    _seed_keys("light", ["light-k1"])
    _seed_keys("cunai", ["cunai-k1"])
    _seed_mimo_keys([("mimo-k1", "legacy")])

    model = llm_rotation.build_companion_router_model()

    assert model.key_group == "light"
    assert model.fallback_chain == ("light", "mimo")


@pytest.mark.parametrize(
    ("module_name", "factory_name", "model_constant"),
    [
        ("src.agents.route_agent.agent", "_route_chat_model", "MIMO_PRO_CHAT_MODEL"),
        ("src.agents.integration_agent.agent", "_integration_chat_model", "MIMO_PRO_CHAT_MODEL"),
        ("src.agents.lifestyle_agent.agent", "_lifestyle_chat_model", "MIMO_CHAT_MODEL"),
        ("src.agents.culture_agent.agent", "_culture_chat_model", "MIMO_CHAT_MODEL"),
        ("src.agents.poi_detail_agent.agent", "_poi_detail_chat_model", "MIMO_CHAT_MODEL"),
    ],
)
def test_long_context_agents_use_expected_mimo_tier(module_name, factory_name, model_constant):
    module = importlib.import_module(module_name)
    expected_model = object()

    with (
        patch.object(module, model_constant, expected_model),
        patch.object(module, "build_chat_model_with_fallback", return_value="model") as build,
    ):
        assert getattr(module, factory_name)() == "model"

    expected_kwargs = {"mimo_model": expected_model}
    if module_name in {
        "src.agents.route_agent.agent",
        "src.agents.integration_agent.agent",
        "src.agents.lifestyle_agent.agent",
        "src.agents.poi_detail_agent.agent",
        "src.agents.culture_agent.agent",
    }:
        expected_kwargs["streaming"] = True
    build.assert_called_once_with(**expected_kwargs)


def test_material_react_agent_uses_streaming_transport():
    module = importlib.import_module("src.agents.material_agent.agent")

    with (
        patch.object(module, "get_chat_model", return_value="model") as build,
        patch.object(module, "make_material_tools", return_value=[]),
        patch.object(module, "create_react_agent", return_value="agent"),
    ):
        assert module.build_material_agent("成都") == "agent"

    build.assert_called_once_with(streaming=True)


@pytest.mark.parametrize(
    ("module_name", "builder_name", "builder_args"),
    [
        ("src.agents.guide_agent.agent", "build_guide_agent", ("project", {})),
        ("src.agents.realtime_agent.agent", "build_realtime_agent", ("project", {})),
    ],
)
def test_companion_agent_builders_use_companion_fallback(module_name, builder_name, builder_args):
    module = importlib.import_module(module_name)

    with (
        patch.object(module, "build_companion_chat_model", return_value="companion-model") as build,
        patch.object(module, "create_react_agent", return_value="agent") as create,
    ):
        assert getattr(module, builder_name)(*builder_args) == "agent"

    build.assert_called_once_with()
    assert create.call_args.args[0] == "companion-model"
