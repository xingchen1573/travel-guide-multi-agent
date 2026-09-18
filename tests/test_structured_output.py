from __future__ import annotations

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from src.materials import citation_check
from src.structured_output import bind_structured_json


class _Result(BaseModel):
    ok: bool
    reason: str


class _Runnable:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    def invoke(self, input_, *args, **kwargs):
        self.calls += 1
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class _Model:
    def __init__(self, results):
        self.runnable = _Runnable(results)

    def with_structured_output(self, schema, *, method, include_raw):
        assert schema is _Result
        assert method == "json_mode"
        assert include_raw is True
        return self.runnable


def _parse_failure(content: str):
    return {
        "raw": AIMessage(content=content),
        "parsed": None,
        "parsing_error": ValueError("invalid JSON response"),
    }


def test_recovers_one_schema_valid_json_object_wrapped_in_chinese_text():
    model = _Model(
        [_parse_failure('核验结果如下：\n```json\n{"ok": true, "reason": "通过"}\n```')]
    )

    result = bind_structured_json(model, _Result).invoke("prompt")

    assert result == _Result(ok=True, reason="通过")
    assert model.runnable.calls == 1


def test_recovers_one_schema_valid_json_object_wrapped_in_answer_string():
    model = _Model(
        [
            _parse_failure(
                '{"answer": "{\\"ok\\": true, '
                '\\"reason\\": \\"provider wrapper\\"}"}'
            )
        ]
    )

    result = bind_structured_json(model, _Result).invoke("prompt")

    assert result == _Result(ok=True, reason="provider wrapper")
    assert model.runnable.calls == 1


def test_citation_verdict_accepts_observed_answer_field_as_reason():
    class _CitationModel:
        def __init__(self):
            self.runnable = _Runnable(
                [
                    _parse_failure(
                        '{"answer": "原文明确支持该说法。", "supported": true}'
                    )
                ]
            )

        def with_structured_output(self, schema, *, method, include_raw):
            assert schema is citation_check.CitationVerdict
            assert method == "json_mode"
            assert include_raw is True
            return self.runnable

    model = _CitationModel()

    result = bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert result == citation_check.CitationVerdict(
        supported=True,
        reason="原文明确支持该说法。",
    )
    assert model.runnable.calls == 1


def test_citation_verdict_accepts_canonical_fields():
    model = _Model(
        [_parse_failure('{"supported": true, "reason": "原文明确支持。"}')]
    )
    model.with_structured_output = lambda schema, *, method, include_raw: model.runnable

    result = bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert result == citation_check.CitationVerdict(
        supported=True,
        reason="原文明确支持。",
    )
    assert model.runnable.calls == 1


def test_citation_verdict_accepts_observed_judgment_label_with_canonical_fields():
    model = _Model(
        [
            _parse_failure(
                '{"answer": "判断", "supported": true, '
                '"reason": "原文明确支持该说法。"}'
            )
        ]
    )
    model.with_structured_output = lambda schema, *, method, include_raw: model.runnable

    result = bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert result == citation_check.CitationVerdict(
        supported=True,
        reason="原文明确支持该说法。",
    )
    assert model.runnable.calls == 1


def test_citation_verdict_accepts_observed_empty_answer_with_canonical_fields():
    model = _Model(
        [
            _parse_failure(
                '{"answer": "", "supported": true, '
                '"reason": "原文明确支持该说法。"}'
            )
        ]
    )
    model.with_structured_output = lambda schema, *, method, include_raw: model.runnable

    result = bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert result == citation_check.CitationVerdict(
        supported=True,
        reason="原文明确支持该说法。",
    )
    assert model.runnable.calls == 1


def test_citation_verdict_accepts_observed_negative_answer_as_supported_flag():
    model = _Model(
        [
            _parse_failure(
                '{"answer": "根据分析，该说法不被原文实质支持。", '
                '"reason": "原文没有支持路线编排判断。"}'
            )
        ]
    )
    model.with_structured_output = lambda schema, *, method, include_raw: model.runnable

    result = bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert result == citation_check.CitationVerdict(
        supported=False,
        reason="原文没有支持路线编排判断。",
    )
    assert model.runnable.calls == 1


def test_citation_verdict_does_not_guess_unknown_answer_without_supported():
    model = _Model(
        [
            _parse_failure(
                '{"answer": "需要进一步判断。", "reason": "结论不明确。"}'
            )
        ]
    )
    model.with_structured_output = lambda schema, *, method, include_raw: model.runnable

    with pytest.raises(ValueError, match="invalid JSON response"):
        bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert model.runnable.calls == 1


def test_citation_verdict_does_not_treat_answer_as_supported():
    class _CitationModel:
        def __init__(self):
            self.runnable = _Runnable(
                [
                    _parse_failure(
                        '{"answer": true, "reason": "原文明确支持该说法。"}'
                    )
                ]
            )

        def with_structured_output(self, schema, *, method, include_raw):
            assert schema is citation_check.CitationVerdict
            assert method == "json_mode"
            assert include_raw is True
            return self.runnable

    model = _CitationModel()

    with pytest.raises(ValueError, match="invalid JSON response"):
        bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert model.runnable.calls == 1


def test_citation_verdict_rejects_reason_and_answer_together():
    class _CitationModel:
        def __init__(self):
            self.runnable = _Runnable(
                [
                    _parse_failure(
                        '{"supported": true, "reason": "正式字段", '
                        '"answer": "兼容字段"}'
                    )
                ]
            )

        def with_structured_output(self, schema, *, method, include_raw):
            assert schema is citation_check.CitationVerdict
            assert method == "json_mode"
            assert include_raw is True
            return self.runnable

    model = _CitationModel()

    with pytest.raises(ValueError, match="invalid JSON response"):
        bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert model.runnable.calls == 1


def test_citation_verdict_rejects_extra_fields():
    model = _Model(
        [
            _parse_failure(
                '{"supported": true, "reason": "原文支持。", '
                '"confidence": 0.9}'
            )
        ]
    )
    model.with_structured_output = lambda schema, *, method, include_raw: model.runnable

    with pytest.raises(ValueError, match="invalid JSON response"):
        bind_structured_json(model, citation_check.CitationVerdict).invoke("prompt")

    assert model.runnable.calls == 1


def test_schema_failure_does_not_resend_same_request():
    model = _Model(
        [
            _parse_failure("我还需要思考，暂无JSON"),
            {"raw": AIMessage(content=""), "parsed": _Result(ok=True, reason="retry"), "parsing_error": None},
        ]
    )

    with pytest.raises(ValueError, match="invalid JSON response"):
        bind_structured_json(model, _Result).invoke("same prompt")

    assert model.runnable.calls == 1


def test_does_not_guess_between_multiple_valid_json_objects():
    model = _Model(
        [
            _parse_failure('{"ok": true, "reason": "a"}\n{"ok": false, "reason": "b"}'),
        ]
    )

    with pytest.raises(ValueError, match="invalid JSON response"):
        bind_structured_json(model, _Result).invoke("prompt")

    assert model.runnable.calls == 1


def test_transport_errors_are_not_retried_by_parser_recovery():
    model = _Model([RuntimeError("provider unavailable")])

    with pytest.raises(RuntimeError, match="provider unavailable"):
        bind_structured_json(model, _Result).invoke("prompt")

    assert model.runnable.calls == 1


def test_temporary_stream_error_is_not_retried_by_parser_recovery():
    error = openai.APIError(
        "Upstream service temporarily unavailable",
        request=httpx.Request("POST", "http://test"),
        body=None,
    )
    model = _Model(
        [
            error,
            {"raw": AIMessage(content=""), "parsed": _Result(ok=True, reason="retry"), "parsing_error": None},
        ]
    )

    with pytest.raises(openai.APIError, match="temporarily unavailable"):
        bind_structured_json(model, _Result).invoke("same prompt")

    assert model.runnable.calls == 1


def test_incomplete_chunked_stream_error_is_not_retried_by_parser_recovery():
    error = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    model = _Model(
        [
            error,
            {"raw": AIMessage(content=""), "parsed": _Result(ok=True, reason="retry"), "parsing_error": None},
        ]
    )

    with pytest.raises(httpx.RemoteProtocolError, match="incomplete chunked read"):
        bind_structured_json(model, _Result).invoke("same prompt")

    assert model.runnable.calls == 1


def test_temporary_503_stream_error_is_not_retried_by_parser_recovery():
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
    model = _Model(
        [
            error,
            {"raw": AIMessage(content=""), "parsed": _Result(ok=True, reason="retry"), "parsing_error": None},
        ]
    )

    with pytest.raises(openai.InternalServerError, match="temporarily unavailable"):
        bind_structured_json(model, _Result).invoke("same prompt")

    assert model.runnable.calls == 1
