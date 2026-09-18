"""Narrow recovery for JSON-mode structured model responses.

The provider may occasionally return a correct JSON object wrapped in a short
natural-language preface or Markdown fence. LangChain's normal JSON parser
rejects that response even though the payload itself is usable. This module
recovers only one unambiguous object that validates against the requested
Pydantic schema; it does not repair malformed JSON or guess missing fields.
"""
from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

from pydantic import BaseModel


SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _raw_message_text(raw: Any) -> str:
    content = getattr(raw, "content", raw)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _recover_unique_json_object(text: str, schema: type[SchemaT]) -> SchemaT | None:
    """Return the only schema-valid JSON object embedded in ``text``.

    Scanning starts only at ``{`` characters. Invalid JSON, arrays, multiple
    schema-valid objects and schema-invalid objects are deliberately rejected.
    """
    decoder = json.JSONDecoder()
    recovered: list[SchemaT] = []
    seen_payloads: set[str] = set()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            parsed = schema.model_validate(payload)
        except Exception:
            parsed = None
        if parsed is None and set(payload) == {"answer"} and isinstance(payload["answer"], str):
            # Some OpenAI-compatible providers wrap an otherwise valid JSON
            # object in one string-valued ``answer`` field. Unwrap exactly one
            # such layer; do not search arbitrary fields or repair its schema.
            parsed = _recover_unique_json_object(payload["answer"], schema)
        if parsed is None:
            continue
        fingerprint = parsed.model_dump_json()
        if fingerprint not in seen_payloads:
            seen_payloads.add(fingerprint)
            recovered.append(parsed)
    return recovered[0] if len(recovered) == 1 else None


class StructuredJsonInvoker(Generic[SchemaT]):
    """Runnable-like wrapper with one narrow, same-response parser recovery."""

    def __init__(self, model: Any, schema: type[SchemaT], *, method: str = "json_mode"):
        self.schema = schema
        self._include_raw = True
        try:
            self._runnable = model.with_structured_output(
                schema, method=method, include_raw=True
            )
        except TypeError:
            # Compatibility for local fake models and older custom wrappers.
            # Production ChatOpenAI supports include_raw and uses the guarded path.
            self._include_raw = False
            self._runnable = model.with_structured_output(schema, method=method)

    def invoke(self, input_: Any, *args: Any, **kwargs: Any) -> SchemaT:
        if not self._include_raw:
            return self._runnable.invoke(input_, *args, **kwargs)

        # Provider/transport retries belong to RotatingChatOpenAI. This layer
        # may recover one unambiguous schema-valid object from the response it
        # already received, but must not resend a deterministic parse failure.
        result = self._runnable.invoke(input_, *args, **kwargs)
        if not isinstance(result, dict) or "parsed" not in result:
            return result
        parsed = result.get("parsed")
        if parsed is not None:
            return parsed
        recovered = _recover_unique_json_object(
            _raw_message_text(result.get("raw")), self.schema
        )
        if recovered is not None:
            return recovered
        parsing_error = result.get("parsing_error")
        if isinstance(parsing_error, Exception):
            raise parsing_error
        raise ValueError(
            f"{self.schema.__name__}结构化输出无法解析："
            "未找到唯一且符合Schema的JSON对象"
        )


def bind_structured_json(
    model: Any, schema: type[SchemaT], *, method: str = "json_mode"
) -> StructuredJsonInvoker[SchemaT]:
    return StructuredJsonInvoker(model, schema, method=method)
