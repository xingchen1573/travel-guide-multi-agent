"""多API Key轮询与 GPT-Load 同入口短退避。

背景（详见docs/ISSUES.md）：真实E2E跑真实撞到`openai.RateLimitError: rpm exhausted`导致
整个进程崩溃、几十分钟的LLM调用全部作废（InMemorySaver不跨进程保留状态）。排查发现
`citation_check.py`的judge调用、`ingest.py`的`_analyze_content`都是裸`.invoke()`，
完全没经过`src/utils.py`的`invoke_with_retry`包装，429一来直接崩溃——这是这次没能
被"已有重试机制"兜住的真正原因。

设计取舍：不是去逐个梳理/包装orchestrator.py、citation_check.py、ingest.py里散落的
各种invoke调用形态（那样容易漏，前面两处裸调用就是真实的漏网案例），而是子类化
ChatOpenAI，在底层`_generate()`和`_stream()`两条网络路径统一实施轮询。
`create_react_agent(model, ...)`和`.with_structured_output(...)`最终都在同一个底层model实例上
调用`.bind_tools()`/`.bind()`（返回包一层的RunnableBinding，不是深拷贝）。非流式调用落到
`_generate()`；`streaming=True`落到`_stream()`，后者必须完整缓存一次Provider流后才向上层交付，
以便在中途断流时丢弃半截Tool Call并安全换Key/渠道重试。`src/config.py`的factory签名不变。

分组轮询（full/light两组独立key池+独立轮询指针，见docs/ARCHITECTURE.md第13.2节）：
"full"组是核心素材Agent、部分核验和显式兼容调用的key池（LLM_API_KEYS）；长攻略与结构化
核验主链另由TRAVEL_MODEL_PROVIDER选择，不应把full误认为生产主链。"light"组是分类/抽取类
调用（需求字段抽取、陪伴意图分类、飞书意图分类）用的独立key池（LLM_API_KEYS_LIGHT，
未配置时回退到LLM_API_KEYS）。两组各自维护自己的_current_key_index，互不影响——
light组高频调用触发限流轮询，不会打乱full组的轮询进度，反之亦然。

"mimo"组的key/base_url不是一一对应同一个值（2026-08-01新增token plan渠道后）：轮询
列表里前半段是MIMO_TOKEN_PLAN_API_KEYS（配token plan渠道的base_url），后半段是旧
MIMO_API_KEYS（配旧渠道的base_url）——emeishan_3d评测真实撞到旧渠道402余额耗尽，
新渠道优先使用、旧渠道留作兜底，而不是二选一硬切换。402(insufficient_balance)跟429
一样触发换key重试，不再只捕获RateLimitError（旧渠道额度耗尽是最先真实撞到这个case
的地方）。

**2026-08-01更新**：`_keys_for_group`/`_mimo_keys_and_urls`不再直接读`src.config`的
`LLM_API_KEYS`/`LLM_API_KEYS_LIGHT`/`MIMO_TOKEN_PLAN_API_KEYS`/`MIMO_API_KEYS`这几个
模块常量，改为读`src.api.key_store`——管理后台"模型/Key"页面这次支持网页表单直接
增删改key（推翻此前"只能走.env"的政策），`key_store`是运行时可变的key池真正来源，
`src.config`里的这几个常量现在只作为`key_store.ensure_migrated()`首次迁移的种子值，
不再是热路径读取的地方。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import socket
import threading
import time

import httpx
import openai
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.config import (
    CHAT_MODEL_TIMEOUT_SECONDS,
    CUNAI_BASE_URL,
    CUNAI_CHAT_MODEL,
    GPTLOAD_BASE_URL,
    GPTLOAD_CHAT_MODEL,
    JIJI_API_KEYS,
    JIJI_BASE_URL,
    JIJI_CHAT_MODEL,
    LLM_BASE_URL,
    LIGHT_MODEL_TIMEOUT_SECONDS,
    LLM_LIGHT_MODEL,
    LLM_MODEL,
    MIMO_CHAT_BASE_URL,
    MIMO_CHAT_MODEL,
    MIMO_GPTLOAD_BASE_URL,
    MIMO_PRO_CHAT_MODEL,
    MIMO_TOKEN_PLAN_BASE_URL,
    OOIOO_API_KEYS,
    OOIOO_BASE_URL,
    OOIOO_CHAT_MODEL,
    TRAVEL_MODEL_PROVIDER,
)

# 其他渠道保持每把 key 最多尝试一次；GPT-Load 另走有界短退避。
_MAX_CYCLES = 1

# 跨渠道也只走一遍，避免故障时反复消耗长 prompt。
_MAX_CHANNEL_LAPS = 1

# GPT-Load 服务端负载均衡多个上游渠道；应用侧只重发到同一
# HTTP 入口。用短退避吸收瞬时故障，不再由外层把整组请求重放六遍。
_GPTLOAD_PROVIDER_GROUPS = frozenset({"gptload", "deepseek"})
_GPTLOAD_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0, 20.0)
_GPTLOAD_RETRY_WAIT_CAP_SECONDS = 30.0
GPTLOAD_MAX_ATTEMPTS = len(_GPTLOAD_RETRY_DELAYS_SECONDS) + 1
_PROVIDER_RETRY_ATTEMPT_SCOPE: ContextVar[ProviderRetryAttemptScope | None]


@dataclass
class ProviderRetryAttemptScope:
    consumed_attempts: int
    consumed: bool = False


_PROVIDER_RETRY_ATTEMPT_SCOPE = ContextVar(
    "provider_retry_attempt_offset",
    default=None,
)

# 长攻略与窄结构化任务使用同一显式选择的Provider。默认仍是GPT-Load；实验进程可通过
# TRAVEL_MODEL_PROVIDER=jiji切到独立jiji组，不修改其他进程或运行时Key池。
_CHANNEL_FALLBACK_CHAIN = (TRAVEL_MODEL_PROVIDER,)
_COMPANION_FALLBACK_CHAIN = ("mimo", "full")
_COMPANION_ROUTER_CHAIN = ("light", "mimo")
_VERIFICATION_FALLBACK_CHAIN = (TRAVEL_MODEL_PROVIDER,)

_lock = threading.Lock()
_current_key_index: dict[str, int] = {
    "full": 0,
    "light": 0,
    "mimo": 0,
    "gptload": 0,
    "deepseek": 0,
    "jiji": 0,
    "cunai": 0,
    "ooioo": 0,
}

# 独立渠道分组：固定 base_url + 固定默认模型 + 不接入 runtime_config 模型覆盖。
# mimo 除外（token_plan/legacy 双域名联动，见 _mimo_keys_and_urls）。
_FIXED_CHANNEL_GROUPS = ("mimo", "gptload", "deepseek", "jiji", "cunai", "ooioo")

# GPT-Load and jiji are the two supported primary travel Providers. Narrow
# structured calls may attach request-scoped generation controls to either one;
# ``deepseek`` remains an execution-compatible alias for GPT-Load.
_REQUEST_CONTROL_GROUPS = frozenset({"gptload", "deepseek", "jiji"})


class ProviderRetryTerminalError(RuntimeError):
    """Provider retry state that the outer checkpoint helper must not replay."""

    code = "provider_retry_terminal"

    def __init__(
        self,
        *,
        provider_group: str,
        attempts: int,
        max_attempts: int = GPTLOAD_MAX_ATTEMPTS,
        last_error: Exception,
        message: str,
    ) -> None:
        self.provider_group = provider_group
        self.attempts = attempts
        self.max_attempts = max_attempts
        self.last_error = last_error
        super().__init__(message)


class ProviderRetryExhaustedError(ProviderRetryTerminalError):
    """GPT-Load exhausted the initial request plus four bounded retries."""

    code = "provider_retry_exhausted"

    def __init__(
        self,
        *,
        provider_group: str,
        attempts: int,
        max_attempts: int = GPTLOAD_MAX_ATTEMPTS,
        last_error: Exception,
    ) -> None:
        status = getattr(last_error, "status_code", None)
        super().__init__(
            provider_group=provider_group,
            attempts=attempts,
            max_attempts=max_attempts,
            last_error=last_error,
            message=(
                f"{self.code}: {provider_group} same-entry retries exhausted "
                f"after {attempts} attempts (status={status}, "
                f"error={type(last_error).__name__}: {last_error})"
            ),
        )


class ProviderRetryDeferredError(ProviderRetryTerminalError):
    """Provider explicitly requested a delay beyond the in-call wait budget."""

    code = "provider_retry_deferred"

    def __init__(
        self,
        *,
        provider_group: str,
        attempts: int,
        max_attempts: int = GPTLOAD_MAX_ATTEMPTS,
        last_error: Exception,
        retry_after_seconds: float,
        next_retry_at: str,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.next_retry_at = next_retry_at
        super().__init__(
            provider_group=provider_group,
            attempts=attempts,
            max_attempts=max_attempts,
            last_error=last_error,
            message=(
                f"{self.code}: {provider_group} requested retry after "
                f"{retry_after_seconds:g}s, beyond the "
                f"{_GPTLOAD_RETRY_WAIT_CAP_SECONDS:g}s in-call wait budget; "
                f"resume after {next_retry_at}"
            ),
        )

    def remaining_delay_seconds(self, *, now: datetime | None = None) -> float:
        """Return the remaining provider-requested delay from ``next_retry_at``."""
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        try:
            retry_at = datetime.fromisoformat(self.next_retry_at.replace("Z", "+00:00"))
        except ValueError:
            return max(0.0, self.retry_after_seconds)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (
                retry_at.astimezone(timezone.utc)
                - reference.astimezone(timezone.utc)
            ).total_seconds(),
        )


class ProviderConfigurationError(ProviderRetryTerminalError):
    """A deterministic GPT-Load gateway configuration failure."""

    code = "provider_configuration_error"

    def __init__(
        self,
        *,
        provider_group: str,
        attempts: int,
        max_attempts: int = GPTLOAD_MAX_ATTEMPTS,
        last_error: Exception,
    ) -> None:
        super().__init__(
            provider_group=provider_group,
            attempts=attempts,
            max_attempts=max_attempts,
            last_error=last_error,
            message=(
                f"{self.code}: {provider_group} cannot serve this request "
                f"(status={getattr(last_error, 'status_code', None)}, "
                f"error={type(last_error).__name__}: {last_error})"
            ),
        )


class ProviderEmptyStructuredOutputError(ProviderRetryTerminalError):
    """A completed structured response had no usable visible JSON content."""

    code = "provider_empty_structured_output"

    def __init__(self, *, provider_group: str, reasoning_disposition: str) -> None:
        self.reasoning_disposition = reasoning_disposition
        last_error = ValueError(
            "provider returned empty visible content without one exact JSON object"
        )
        super().__init__(
            provider_group=provider_group,
            attempts=1,
            max_attempts=1,
            last_error=last_error,
            message=(
                f"{self.code}: {provider_group} returned empty visible content "
                f"(reasoning={reasoning_disposition}); the completed response "
                "will not be replayed"
            ),
        )


@contextmanager
def provider_retry_attempt_offset(consumed_attempts: int):
    """Apply persisted attempts to the first Provider sequence in one resume.

    Checkpoint supervision wraps one resumed ``agent.invoke`` with this public
    boundary. The offset is consumed once, so later independent model calls in
    the same invocation receive a fresh Provider budget.
    """
    if (
        isinstance(consumed_attempts, bool)
        or not isinstance(consumed_attempts, int)
        or not 0 <= consumed_attempts <= GPTLOAD_MAX_ATTEMPTS
    ):
        raise ValueError(
            "consumed_attempts must be an integer between 0 and "
            f"{GPTLOAD_MAX_ATTEMPTS}"
        )
    scope = ProviderRetryAttemptScope(consumed_attempts=consumed_attempts)
    token = _PROVIDER_RETRY_ATTEMPT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _PROVIDER_RETRY_ATTEMPT_SCOPE.reset(token)


def _consume_provider_retry_attempt_offset(provider_group: str) -> int:
    if provider_group not in _GPTLOAD_PROVIDER_GROUPS:
        return 0
    scope = _PROVIDER_RETRY_ATTEMPT_SCOPE.get()
    if scope is None or scope.consumed:
        return 0
    scope.consumed = True
    return scope.consumed_attempts


def _provider_attempt_window(provider_group: str) -> tuple[int, int, int]:
    """Return cumulative offset, remaining calls and the global ceiling."""
    if provider_group not in _GPTLOAD_PROVIDER_GROUPS:
        return 0, _key_count(provider_group), _key_count(provider_group)
    offset = _consume_provider_retry_attempt_offset(provider_group)
    if offset >= GPTLOAD_MAX_ATTEMPTS:
        raise ProviderRetryExhaustedError(
            provider_group=provider_group,
            attempts=GPTLOAD_MAX_ATTEMPTS,
            max_attempts=GPTLOAD_MAX_ATTEMPTS,
            last_error=RuntimeError(
                "persisted provider retry budget already exhausted"
            ),
        )
    return offset, GPTLOAD_MAX_ATTEMPTS - offset, GPTLOAD_MAX_ATTEMPTS


def _mimo_keys_and_urls() -> list[tuple[str, str]]:
    """mimo组的(key, base_url)配对列表，token plan渠道排在前面优先使用。渠道信息
    从key_store.list_mimo_keys()读取（每个key自带channel字段），不再依赖
    MIMO_TOKEN_PLAN_API_KEYS/MIMO_API_KEYS常量的列表顺序——顺序现在由key_store里
    实际存储的顺序决定（迁移时保留了"token_plan在前"的顺序，之后管理员新增的key
    追加在对应channel分组末尾）。"""
    from src.api import key_store

    entries = key_store.list_mimo_keys()
    return [
        (
            e["key"],
            MIMO_GPTLOAD_BASE_URL
            if e["channel"] == "gptload"
            else MIMO_TOKEN_PLAN_BASE_URL
            if e["channel"] == "token_plan"
            else MIMO_CHAT_BASE_URL,
        )
        for e in entries
    ]


def _gptload_keys() -> list[str]:
    """Return credentials stored in the historical mimo bucket for GPT-Load.

    Runtime storage still uses the historical ``mimo`` bucket with a channel
    field. Filtering only ``gptload`` entries keeps existing credentials and
    CRUD data intact while the execution group and trace identity are truthful.
    """
    from src.api import key_store

    return [
        entry["key"]
        for entry in key_store.list_mimo_keys()
        if entry.get("channel") == "gptload"
    ]


def _keys_for_group(group: str) -> list[str]:
    from src.api import key_store

    if group in {"gptload", "deepseek"}:
        return _gptload_keys()
    if group == "jiji":
        return list(JIJI_API_KEYS)
    if group == "ooioo":
        return list(OOIOO_API_KEYS)
    return key_store.list_keys(group)


def _key_count(group: str = "full") -> int:
    return len(_keys_for_group(group)) or 1  # 避免除零；真正没配置key时get_chat_model本来就会报错


def _current_key(group: str = "full") -> str | None:
    keys = _keys_for_group(group)
    if not keys:
        return None
    with _lock:
        idx = _current_key_index.get(group, 0)
        return keys[idx % len(keys)]


def _current_base_url(group: str) -> str | None:
    """mimo组的base_url随轮询指针跟着key一起换（token plan/旧渠道两个不同的域名）；
    cunai组固定用独立 BASE_URL；其余组固定用一个base_url，不受轮询影响，调用方
    自己用LLM_BASE_URL。返回None表示"调用方自己用所属组的默认base_url"。"""
    if group == "mimo":
        pairs = _mimo_keys_and_urls()
        if not pairs:
            return None
        with _lock:
            idx = _current_key_index.get(group, 0)
            return pairs[idx % len(pairs)][1]
    if group in {"gptload", "deepseek"}:
        return GPTLOAD_BASE_URL
    if group == "jiji":
        return JIJI_BASE_URL
    if group == "cunai":
        return CUNAI_BASE_URL
    if group == "ooioo":
        return OOIOO_BASE_URL
    return None


def _advance_key(group: str = "full") -> None:
    """轮询索引前进一格。加锁是因为react-agent单轮推理内部的多个工具调用可能用
    ThreadPoolExecutor并发跑（见docs/ORCHESTRATION.md），避免竞态导致index跳两格。
    """
    with _lock:
        _current_key_index[group] = _current_key_index.get(group, 0) + 1


def _is_insufficient_quota_error(exc: Exception) -> bool:
    """中转渠道单 key 余额耗尽时返回 403 PermissionDeniedError。

    不同 OpenAI 兼容网关可能返回 ``insufficient_user_quota`` 或
    ``insufficient_user_usd_credit``；两者都按单 key 余额问题处理。

    cunai组5把key各自独立余额（用户确认，不是共享账户），换下一把key很可能还有余额——
    不能套用_is_channel_switch_error对"UA被拉黑"这类403的"立即弃组"判断（那种情况换
    组内其它key也没用，只有切换整个渠道组才可能恢复），这里必须当成可轮询错误（换同组
    下一把key），否则组内剩下几把key白白没轮到就整组放弃了。
    """
    if not isinstance(exc, openai.PermissionDeniedError):
        return False
    msg = str(exc)
    body = str(getattr(exc, "body", None) or "")
    payload = f"{msg} {body}"
    return any(code in payload for code in (
        "insufficient_user_quota",
        "insufficient_user_usd_credit",
    ))


def is_temporary_stream_error(exc: Exception) -> bool:
    """Return whether an SSE stream ended with an observed transient transport error."""
    if type(exc) is openai.APIError:
        message = str(exc)
        return any(
            marker in message
            for marker in (
                "Upstream service temporarily unavailable",
                "Upstream HTTP/2 stream failed",
            )
        )
    if isinstance(exc, openai.InternalServerError):
        return (
            getattr(exc, "status_code", None) == 503
            and "Service temporarily unavailable" in str(exc)
        )
    return isinstance(exc, httpx.RemoteProtocolError) and (
        "incomplete chunked read" in str(exc)
    )


def is_empty_completion_snapshot_assertion(exc: Exception) -> bool:
    """Recognize the OpenAI SDK assertion emitted after a snapshot-less stream."""
    if not isinstance(exc, AssertionError) or str(exc):
        return False
    traceback_cursor = exc.__traceback__
    while traceback_cursor is not None:
        frame = traceback_cursor.tb_frame
        filename = frame.f_code.co_filename.replace("\\", "/")
        if (
            frame.f_code.co_name == "current_completion_snapshot"
            and filename.endswith("/openai/lib/streaming/chat/_completions.py")
        ):
            return True
        traceback_cursor = traceback_cursor.tb_next
    return False


def _is_transient_httpx_error(exc: Exception) -> bool:
    """Recognize transport shapes historically treated as transient."""
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
        ),
    ) or is_temporary_stream_error(exc)


def _has_permanent_dns_error(exc: Exception) -> bool:
    """Detect non-recoverable resolver failures hidden in transport wrappers."""
    permanent_codes = {
        code
        for code in (
            getattr(socket, "EAI_NONAME", None),
            getattr(socket, "EAI_FAIL", None),
        )
        if code is not None
    }
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, socket.gaierror) and current.errno in permanent_codes:
            return True
        cause = current.__cause__
        context = current.__context__
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)
    return False


def _is_safe_transport_replay(exc: Exception) -> bool:
    """Keep transient transport retries while rejecting permanent DNS failures.

    OpenAI/httpx wrap resolver exceptions at different depths across operating
    systems. EAI_NONAME and EAI_FAIL cannot recover by rotating a key or
    replaying the same endpoint; EAI_AGAIN and unknown connection failures keep
    the existing transient behavior.
    """
    if _has_permanent_dns_error(exc):
        return False
    return (
        isinstance(exc, openai.APIConnectionError)
        or _is_transient_httpx_error(exc)
        or is_empty_completion_snapshot_assertion(exc)
    )


def _retry_after_seconds(exc: Exception, *, now: float | None = None) -> float | None:
    """Read Retry-After hints from OpenAI-compatible headers and JSON bodies."""
    candidates: list[float] = []

    def _append_seconds(value, *, milliseconds: bool = False) -> None:
        if value is None or isinstance(value, bool):
            return
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return
        if milliseconds:
            seconds /= 1000.0
        if seconds >= 0:
            candidates.append(seconds)

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        _append_seconds(headers.get("retry-after-ms"), milliseconds=True)
        retry_after = headers.get("retry-after")
        _append_seconds(retry_after)
        if retry_after is not None:
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
            except (TypeError, ValueError, OverflowError):
                retry_at = None
            if retry_at is not None:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                current = time.time() if now is None else now
                candidates.append(max(0.0, retry_at.timestamp() - current))

    body = getattr(exc, "body", None)
    bodies = [body]
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        bodies.append(body["error"])
    for payload in bodies:
        if not isinstance(payload, dict):
            continue
        _append_seconds(payload.get("retry_after"))
        _append_seconds(payload.get("retry_after_seconds"))
        _append_seconds(payload.get("retry_after_ms"), milliseconds=True)

    return max(candidates) if candidates else None


def _retry_timestamp(delay_seconds: float) -> str:
    return datetime.fromtimestamp(
        time.time() + max(delay_seconds, 0.0), tz=timezone.utc
    ).isoformat()


def _is_observed_gptload_transient_model_not_found(
    exc: Exception,
    group: str,
) -> bool:
    """Match the exact intermittent 404 shape observed from GPT-Load.

    The same configured model succeeded immediately before and after this
    response. Keep ordinary 404s deterministic; only the complete Provider
    payload observed on the working endpoint is safe for bounded replay.
    """
    if (
        group not in _GPTLOAD_PROVIDER_GROUPS
        or not isinstance(exc, openai.NotFoundError)
        or exc.status_code != 404
    ):
        return False
    body = getattr(exc, "body", None)
    return (
        isinstance(body, dict)
        and set(body) == {"message", "type", "param", "code"}
        and body.get("message") == "Model not found"
        and body.get("type") == "invalid_request_error"
        and body.get("param") == ""
        and body.get("code") == 404
    )


def _is_gptload_deterministic_status_error(exc: Exception, group: str) -> bool:
    """GPT-Load credentials and request target errors must fail immediately."""
    return (
        group in _GPTLOAD_PROVIDER_GROUPS
        and isinstance(exc, openai.APIStatusError)
        and exc.status_code in {401, 403, 404}
        and not _is_observed_gptload_transient_model_not_found(exc, group)
    )


def _is_rotatable_error(exc: Exception, group: str) -> bool:
    """判断这个异常是否应该触发"换下一个key重试"。429限流所有组都适用；402余额耗尽
    （insufficient_balance）目前只在mimo组真实撞到过（旧sk-渠道欠费），其余组维持
    原有的"只认429"行为不扩大范围——没有证据表明其余渠道也会用402表示可恢复的额度问题。
    403 insufficient_user_quota / insufficient_user_usd_credit（单key余额耗尽）
    也是可轮询错误——见
    _is_insufficient_quota_error说明，跟_is_channel_switch_error里"普通403立即弃组"
    的判断是互斥分支，那边已经优先排除了这类403，不会两边冲突。

    503 SERVICE_BUSY 是2026-08-06真实撞到的可轮请新case（商汤relay服务端过载）：
    第一次baseline qinggan_7d重跑连续两次同款503，排查发现是这里只认429、503直接raise
    不换key——6个full key只试了第1个就放弃，其它5个key没轮到。503是服务端临时过载，
    跟429同属"换一个key/端点可能就好"的可恢复错误，加进可轮询范围。跟429/402一样
    不预先扩大到所有5xx，只加真实撞到的503。

    504 Gateway Timeout 是历史上真实撞到的可轮询单点（中转 relay 对8天
    长行程prompt偶发网关超时）：qinggan_7d评测run_plan在route_draft_node真实复现，
    run_status.error="<html>...504 Gateway Time-out...</html>"，openai client把
    504映射成APIStatusError(status_code=504)，被_generate的except
    (RateLimitError, APIStatusError)catch到，但_is_rotatable_error只认429/402/503、
    不认504，直接raise——整轮run_plan就此失败。504跟503同属"relay侧瞬时、换一次重试
    可能就好"的可恢复错误（隔离采样5次同款route_draft prompt 0次再撞504，证实瞬时
    不是稳定故障），加进可轮询范围。同样不扩大到所有5xx，只加真实撞到的504。注意504
    的body可能是非JSON的html，openai client在这种情况下仍能构造出status_code=504
    的APIStatusError，所以isinstance+status_code判断能命中，不需特殊处理body格式。

    APIConnectionError（含 APITimeoutError）是连接层可轮询错误：2026-08-18
    真实飞书群测试撞到 APITimeoutError，2026-08-24 定向复核又撞到代理 TLS
    `UNEXPECTED_EOF_WHILE_READING`，OpenAI SDK 映射为 APIConnectionError。
    requirements_gather_node一次调用卡满CHAT_MODEL_TIMEOUT_SECONDS后报"Request timed
    out."直接失败，未触发任何换key重试——openai.APITimeoutError不是APIStatusError的
    子类（它继承自APIConnectionError，没有status_code，是纯网络层异常），此前的
    except子句和_is_rotatable_error都没覆盖它，即使key池里还有5个key可用也不会被
    轮到。超时是典型的"换一个key/端点很可能就好"的瞬时故障（不是这个key本身有问题），
    跟429/503/504同属一类，加进可轮询范围；不做5xx那种"只加真实撞到的具体码"的收窄，
    因为APITimeoutError本身就是一个具体、单一的异常类型，不是笼统的一整类。
    """
    if _is_gptload_deterministic_status_error(exc, group):
        return False
    if isinstance(exc, openai.RateLimitError):
        return True
    if _is_safe_transport_replay(exc):
        # Some OpenAI-compatible gateways accept an SSE request, emit chunks,
        # then terminate the stream with this transient provider error. It has
        # no HTTP status code, so it is not covered by APIStatusError or
        # APIConnectionError even though retrying the same checkpoint is safe.
        return True
    if _is_observed_gptload_transient_model_not_found(exc, group):
        return True
    if group == "cunai" and isinstance(exc, openai.BadRequestError):
        # Cunai 的不同 key 可能路由到不同上游：同一个 Pro 模型和同一份
        # tool schema，有的 key 支持强制 tool_choice，有的返回 400001。该错误
        # 已被实测证明换 key 可恢复，只对这条精确文案放行轮询，其余
        # 400/422 仍作为确定性请求错误直接抛出。
        msg = f"{exc} {getattr(exc, 'body', None) or ''}"
        if "Thinking mode does not support this tool_choice" in msg:
            return True
    if group == "mimo" and isinstance(exc, openai.APIStatusError) and exc.status_code == 402:
        return True
    if isinstance(exc, openai.InternalServerError) and exc.status_code == 503:
        return True
    if (
        group in _GPTLOAD_PROVIDER_GROUPS
        and isinstance(exc, openai.APIStatusError)
        and exc.status_code in {502, 524}
    ):
        return True
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 504:
        return True
    if _is_insufficient_quota_error(exc):
        return True
    return False


def _is_channel_switch_error(exc: Exception) -> bool:
    """判断这个异常是否属于"确定性渠道故障"——同 group 其它 key 也救不了，
    立即切 fallback 链里的下一个 group。
    - 401 AuthenticationError（key 失效/认证过期）
    - 403 PermissionDeniedError（被 block/权限）
      —— 但 insufficient_user_quota 这种"单key余额耗尽"的403例外（见_is_insufficient_quota_error），
      不是渠道级故障，同组其它key很可能还有余额，交给_is_rotatable_error当可轮询错误处理，
      不在这里提前弃组（2026-08-13修复，见docs/ISSUES.md第119条：cunai组5把key各自独立余额，
      真实撞到过1把耗尽但另外4把没轮到就整组放弃的情况）。
    - 503 且 body 含 NO_KEYS_AVAILABLE（上游 key 池空）
    503 SERVICE_BUSY 不在此内（它会尝试同组下一把 key）；400/422 不在此内（category A 直接 raise）。
    在 _generate_within_group 里先于 _is_rotatable_error 检查——503-NO_KEYS 同时是 503 又是确定性，
    要让确定性赢，立即切换而非继续尝试同组 key。
    """
    if isinstance(exc, openai.AuthenticationError):
        return True
    if isinstance(exc, openai.PermissionDeniedError):
        return not _is_insufficient_quota_error(exc)
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 503:
        msg = str(exc)
        body = str(getattr(exc, "body", None) or "")
        if "NO_KEYS_AVAILABLE" in msg or "NO_KEYS_AVAILABLE" in body or "No active API keys" in msg:
            return True
    return False


class _ChannelExhausted(Exception):
    """当前 group 用尽（所有 key 失败，或撞到确定性错误），该切 fallback 链里下一个 group。
    仅 RotatingChatOpenAI._generate 内部用，不外泄——_generate 捕获后取 .last_exc 决定 raise 什么。
    last_exc: 该 group 最后一个真实异常（最终 raise 用）；reason: 切渠道原因（日志用）。"""

    def __init__(self, last_exc: Exception | None, reason: str):
        self.last_exc = last_exc
        self.reason = reason
        super().__init__(reason)


def _gptload_retry_decision(
    exc: Exception,
    *,
    provider_group: str,
    attempt: int,
    max_attempts: int,
) -> tuple[float, dict]:
    """Return the next short delay or raise a provider terminal marker."""
    # The hard attempt ceiling wins over Retry-After. Otherwise a long hint on
    # the fifth failure would defer to the checkpoint layer, reset the local
    # counter and allow another complete five-attempt sequence.
    if attempt >= max_attempts:
        error = ProviderRetryExhaustedError(
            provider_group=provider_group,
            attempts=attempt,
            max_attempts=max_attempts,
            last_error=exc,
        )
        raise error from exc

    retry_after = _retry_after_seconds(exc)
    if retry_after is not None and retry_after > _GPTLOAD_RETRY_WAIT_CAP_SECONDS:
        next_retry_at = _retry_timestamp(retry_after)
        error = ProviderRetryDeferredError(
            provider_group=provider_group,
            attempts=attempt,
            max_attempts=max_attempts,
            last_error=exc,
            retry_after_seconds=retry_after,
            next_retry_at=next_retry_at,
        )
        raise error from exc

    scheduled = _GPTLOAD_RETRY_DELAYS_SECONDS[attempt - 1]
    delay = max(scheduled, retry_after or 0.0)
    return delay, {
        "disposition": "retrying",
        "attempt": attempt,
        "max_attempts": max_attempts,
        "delay_seconds": delay,
        "retry_after_seconds": retry_after,
        "next_retry_at": _retry_timestamp(delay),
    }


def _log(msg: str) -> None:
    """跟 orchestrator.py 的 print(f"[{scenario_id}]...") 同款风格，进 eval/run 日志便于排查。
    不打 logging 库避免引入依赖。"""
    print(msg, flush=True)


def _log_channel_switch(chain: tuple[str, ...], from_group: str, reason: str, lap: int) -> None:
    """记录一次渠道切换，便于事后排查实际在跑哪个 group（这次踩渠道故障踩了 5 个就是因为
    在渠道间瞎试、没留痕）。"""
    _log(
        f"[channel-fallback] lap{lap} {from_group} 用尽({reason})→切链下一个；"
        f"链={chain}"
    )


def _strict_json_object_text(value: object) -> str | None:
    """Return exact JSON-object text, never prose, fences, arrays or scalars."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return text if isinstance(payload, dict) else None


def _empty_message_content(content: object) -> bool:
    return content is None or content == "" or content == []


def _normalize_nonstandard_structured_reasoning_response(response) -> dict | None:
    """Let exact-JSON recovery inspect the observed ``role=analysis`` shape.

    Normalize only the exact single-choice, tool-free response shape seen in
    production. Other nonstandard roles remain invalid; schema validation still
    happens after recovery.
    """
    response_dict = (
        response
        if isinstance(response, dict)
        else response.model_dump(warnings=False)
    )
    choices = response_dict.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    message = choice.get("message")
    if not isinstance(message, dict):
        return None
    if not (
        message.get("role") == "analysis"
        and "content" in message
        and message["content"] is None
        and choice.get("finish_reason") == "stop"
        and not message.get("tool_calls")
        and not message.get("function_call")
        and _strict_json_object_text(message.get("reasoning_content")) is not None
    ):
        return None
    return {
        **response_dict,
        "choices": [
            {
                **choice,
                "message": {**message, "role": "assistant"},
            }
        ],
    }


def _sanitize_stream_reasoning_and_recover_json(
    chunks: list[ChatGenerationChunk],
) -> tuple[list[ChatGenerationChunk], str | None]:
    """Strip raw reasoning and promote only one exact JSON object when needed."""
    if not chunks:
        return chunks, "missing"
    result = generate_from_stream(iter(chunks))
    message = result.generations[0].message
    visible_empty = _empty_message_content(message.content)
    reasoning_content = message.additional_kwargs.get("reasoning_content")
    recovered = (
        _strict_json_object_text(reasoning_content)
        if visible_empty
        else None
    )
    for chunk in chunks:
        chunk.message.additional_kwargs.pop("reasoning_content", None)
        chunk.message.response_metadata.pop("reasoning_content", None)
    if recovered is not None:
        chunks = [
            *chunks,
            ChatGenerationChunk(
                message=AIMessageChunk(
                    content=recovered,
                    response_metadata={
                        "structured_json_recovered_from_reasoning": True,
                    },
                )
            ),
        ]
        return chunks, None
    if visible_empty:
        disposition = (
            "missing"
            if not isinstance(reasoning_content, str) or not reasoning_content
            else "not_exact_json_object"
        )
        return chunks, disposition
    return chunks, None


class RotatingChatOpenAI(ChatOpenAI):
    """为 ChatOpenAI 提供应用可见的 Key 轮询和有界 Provider 重试。

    非 GPT-Load 渠道保持历史的每把 Key 一次行为；GPT-Load 对瞬时故障
    在同一 HTTP 入口执行首次加最多四次短退避。

    key_group区分用哪一组的key池+轮询指针（"full"或"light"），默认"full"保持向后兼容。

    fallback_chain 非空时启用已有的跨渠道 fallback。
    空 tuple 维持单 group 行为。
    sensetime_max_tokens/full_extra_body 仅属于普通长文本模型的商汤(full)
    渠道配置。request_max_tokens/request_extra_body 属于单次结构化请求，在
    GPT-Load/jiji 主 Provider 之间切组时仍必须保留。
    """

    def _create_chat_result(self, response, generation_info=None):
        """Preserve provider reasoning needed by subsequent tool-call turns.

        Some OpenAI-compatible reasoning models require the assistant's
        ``reasoning_content`` to be sent back verbatim after a tool call. The
        upstream LangChain converter currently drops this provider-specific
        field, which makes the second agent turn fail with HTTP 400.

        MiMo is intentionally excluded. A live gptload smoke test on 2026-08-31
        verified that both mimo-v2.5 and mimo-v2.5-pro continue tool calls with
        only ``tool_calls`` plus the tool result. Keeping the reasoning text for
        MiMo therefore adds checkpoint and request context without a protocol
        benefit. Token usage remains available through ``usage_metadata``.
        """
        normalized_response = (
            _normalize_nonstandard_structured_reasoning_response(response)
            if self.recover_structured_json_from_reasoning
            else None
        )
        result = super()._create_chat_result(
            normalized_response if normalized_response is not None else response,
            generation_info,
        )
        if self.key_group == "mimo":
            return result
        response_dict = response if isinstance(response, dict) else response.model_dump(warnings=False)
        for generation, choice in zip(result.generations, response_dict.get("choices") or []):
            if not isinstance(generation.message, AIMessage):
                continue
            reasoning_content = (choice.get("message") or {}).get("reasoning_content")
            if self.recover_structured_json_from_reasoning:
                recovered = (
                    _strict_json_object_text(reasoning_content)
                    if _empty_message_content(generation.message.content)
                    else None
                )
                if recovered is not None:
                    generation.message.content = recovered
                    generation.message.response_metadata[
                        "structured_json_recovered_from_reasoning"
                    ] = True
                generation.message.additional_kwargs.pop(
                    "reasoning_content", None
                )
                generation.message.response_metadata.pop(
                    "reasoning_content", None
                )
                if (
                    recovered is None
                    and _empty_message_content(generation.message.content)
                ):
                    disposition = (
                        "missing"
                        if not isinstance(reasoning_content, str)
                        or not reasoning_content
                        else "not_exact_json_object"
                    )
                    raise ProviderEmptyStructuredOutputError(
                        provider_group=self.key_group,
                        reasoning_disposition=disposition,
                    )
            elif reasoning_content is not None:
                generation.message.additional_kwargs[
                    "reasoning_content"
                ] = reasoning_content
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """Restore preserved reasoning content to assistant tool-call messages."""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        source_messages = self._convert_input(input_).to_messages()
        for source, target in zip(source_messages, payload.get("messages", [])):
            if not isinstance(source, AIMessage):
                continue
            reasoning_content = source.additional_kwargs.get("reasoning_content")
            if reasoning_content is not None:
                target["reasoning_content"] = reasoning_content
        return payload

    key_group: str = "full"
    fallback_chain: tuple[str, ...] = ()
    sensetime_max_tokens: int | None = None
    mimo_model: str = MIMO_CHAT_MODEL
    full_extra_body: dict | None = None
    request_max_tokens: int | None = None
    request_extra_body: dict | None = None
    recover_structured_json_from_reasoning: bool = False

    def _switch_to_group(self, group: str) -> None:
        """切到 fallback 链里的另一个 group：重设 5 个渠道特异字段（key_group/base_url/model/
        default_headers/max_tokens）+ 该 group 的 key 轮询指针归 0 + 重建底层 openai client
        （null clients + validate_environment，复用 _rotate_to_next_key 已验证的 pydantic reassign 机制）。
        切渠道特异配置走 _channel_* helper 集中维护，避免散落。
        """
        self.key_group = group
        base = _current_base_url(group)
        self.openai_api_base = base if base is not None else LLM_BASE_URL
        self.model_name = self.mimo_model if group == "mimo" else _default_model_for_group(group)
        new_key = _current_key(group)
        if new_key is not None:
            self.openai_api_key = SecretStr(new_key)
        self.default_headers = _channel_default_headers(group, base)
        self.max_tokens = _channel_max_tokens(
            group,
            self.sensetime_max_tokens,
        )
        self.extra_body = _channel_extra_body(
            group,
            self.full_extra_body,
            self.request_extra_body,
            self.request_max_tokens,
        )
        self.http_client = None
        with _lock:
            _current_key_index[group] = 0  # 切过来从该 group 第 0 个 key 开始
        self.client = None
        self.async_client = None
        self.root_client = None
        self.root_async_client = None
        self.validate_environment()

    def _rotate_to_next_key(self) -> None:
        _advance_key(self.key_group)
        new_key = _current_key(self.key_group)
        if new_key is None:
            return
        # api_key在构造时(validate_environment)就固化进了底层openai.OpenAI客户端，
        # 无法直接替换那个客户端对象的api_key属性；清空缓存字段后重新调用
        # validate_environment()，复用langchain自己的构造逻辑重建client，
        # 不用手搓一遍构造参数（不容易因为版本升级跟langchain内部实现失配）。
        self.openai_api_key = SecretStr(new_key)
        new_base_url = _current_base_url(self.key_group)
        if new_base_url is not None:
            self.openai_api_base = new_base_url
        self.client = None
        self.async_client = None
        self.root_client = None
        self.root_async_client = None
        self.validate_environment()

    def _handle_transient_failure(
        self,
        exc: Exception,
        *,
        attempt: int,
        max_attempts: int,
        messages,
        t0: float,
        cycle: int,
        stream: bool,
    ) -> None:
        """Trace and schedule one transient retry within the current group."""
        if self.key_group not in _GPTLOAD_PROVIDER_GROUPS:
            _trace_call(self, messages, None, exc, t0, cycle)
            self._rotate_to_next_key()
            return

        try:
            delay, retry_meta = _gptload_retry_decision(
                exc,
                provider_group=self.key_group,
                attempt=attempt,
                max_attempts=max_attempts,
            )
        except ProviderRetryTerminalError as terminal:
            _trace_call(
                self,
                messages,
                None,
                exc,
                t0,
                cycle,
                provider_retry={
                    "disposition": (
                        "deferred"
                        if isinstance(terminal, ProviderRetryDeferredError)
                        else "exhausted"
                    ),
                    "attempt": terminal.attempts,
                    "max_attempts": max_attempts,
                    "retry_after_seconds": getattr(
                        terminal, "retry_after_seconds", None
                    ),
                    "next_retry_at": getattr(terminal, "next_retry_at", None),
                },
            )
            raise _ChannelExhausted(terminal, terminal.code) from terminal

        _trace_call(
            self,
            messages,
            None,
            exc,
            t0,
            cycle,
            provider_retry=retry_meta,
        )
        stream_label = " stream" if stream else ""
        _log(
            f"[provider-retry] {self.key_group} same-entry{stream_label} "
            f"attempt {attempt}/{max_attempts} failed with "
            f"{type(exc).__name__}; retry in {delay:g}s"
        )
        self._rotate_to_next_key()
        if delay:
            time.sleep(delay)

    def _generate_within_group(self, messages, stop=None, run_manager=None, **kwargs):
        """Run one group under its existing key/provider retry policy.

        GPT-Load gets one initial attempt plus four same-entry retries. Other
        groups retain the historical one-attempt-per-key behavior.
        - 确定性错误（_is_channel_switch_error：401/403/503-NO_KEYS）立即抛 _ChannelExhausted
        - category A（_is_rotatable_error 返回 False，如 400/422）直接 raise（换渠道也没用，透出 _generate）
        - 瞬时错误按当前 group 的策略退避或换下一把 key

        trace hook（2026-08-10）：每次 super()._generate 往返都调 _trace_call 落盘——成功记
        finish_reason/usage/reasoning/tool_calls/invalid_tool_calls，失败记 error_type/status/msg。
        无 trace context（不在 agent run 里）时 _trace_call 内部静默跳过，不影响调用语义。
        """
        last_exc: Exception | None = None
        for cycle in range(_MAX_CYCLES):
            authentication_failures = 0
            attempt_offset, attempts_this_run, max_attempts = _provider_attempt_window(
                self.key_group
            )
            for attempt_index in range(attempts_this_run):
                cumulative_attempt = attempt_offset + attempt_index + 1
                t0 = time.time()
                try:
                    result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
                    _trace_call(self, messages, result, None, t0, cycle)
                    return result
                except (openai.APIError, httpx.TransportError) as e:
                    if _is_gptload_deterministic_status_error(e, self.key_group):
                        _trace_call(self, messages, None, e, t0, cycle)
                        raise
                    # A 401 identifies the current credential, not necessarily the
                    # whole channel. Cunai keys are independent, so try the other
                    # configured keys before declaring the group unavailable.
                    if isinstance(e, openai.AuthenticationError) and _key_count(self.key_group) > 1:
                        _trace_call(self, messages, None, e, t0, cycle)
                        authentication_failures += 1
                        last_exc = e
                        if authentication_failures >= _key_count(self.key_group):
                            raise _ChannelExhausted(
                                last_exc, "all configured keys failed authentication"
                            ) from e
                        self._rotate_to_next_key()
                        continue
                    if _is_channel_switch_error(e):
                        _trace_call(self, messages, None, e, t0, cycle)
                        terminal = (
                            ProviderConfigurationError(
                                provider_group=self.key_group,
                                attempts=cumulative_attempt,
                                max_attempts=max_attempts,
                                last_error=e,
                            )
                            if self.key_group in _GPTLOAD_PROVIDER_GROUPS
                            else e
                        )
                        reason = getattr(terminal, "code", f"deterministic {type(e).__name__}")
                        raise _ChannelExhausted(terminal, reason) from e
                    if not _is_rotatable_error(e, self.key_group):
                        _trace_call(self, messages, None, e, t0, cycle)
                        raise
                    last_exc = e
                    self._handle_transient_failure(
                        e,
                        attempt=cumulative_attempt,
                        max_attempts=max_attempts,
                        messages=messages,
                        t0=t0,
                        cycle=cycle,
                        stream=False,
                    )
            if authentication_failures == _key_count(self.key_group):
                raise _ChannelExhausted(last_exc, "all configured keys failed authentication")
        raise _ChannelExhausted(last_exc, "all configured keys exhausted")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 无 fallback_chain：只轮询当前 group，用尽后抛最后异常。
        if not self.fallback_chain:
            try:
                return self._generate_within_group(messages, stop=stop, run_manager=run_manager, **kwargs)
            except _ChannelExhausted as ce:
                raise ce.last_exc if ce.last_exc is not None else RuntimeError("channel exhausted without exception")
        # 有 fallback_chain：按顺序走完一次可用渠道链。
        last_exc: Exception | None = None
        for lap in range(_MAX_CHANNEL_LAPS):
            for group in self.fallback_chain:
                self._switch_to_group(group)
                try:
                    return self._generate_within_group(messages, stop=stop, run_manager=run_manager, **kwargs)
                except _ChannelExhausted as ce:
                    last_exc = ce.last_exc
                    _log_channel_switch(self.fallback_chain, group, ce.reason, lap)
                    continue  # 切链里下一个 group
        _log(
            f"[channel-fallback] 链 {self.fallback_chain} ×{_MAX_CHANNEL_LAPS}圈全失败，"
            f"raise 最后一个异常: {type(last_exc).__name__ if last_exc else None}"
        )
        raise last_exc if last_exc is not None else RuntimeError("all channels exhausted without exception")

    def _stream_within_group(self, messages, stop=None, **kwargs):
        """Buffer one complete provider stream before exposing any chunks.

        LangChain routes ``streaming=True`` calls directly through ``_stream``;
        without this path the key rotation implemented by ``_generate`` is
        bypassed. Buffering is intentional: if an SSE response breaks after a
        partial tool call, the incomplete chunks must be discarded before a
        clean retry on the next key or provider.
        """
        last_exc: Exception | None = None
        for cycle in range(_MAX_CYCLES):
            authentication_failures = 0
            attempt_offset, attempts_this_run, max_attempts = _provider_attempt_window(
                self.key_group
            )
            for attempt_index in range(attempts_this_run):
                cumulative_attempt = attempt_offset + attempt_index + 1
                t0 = time.time()
                try:
                    chunks = list(
                        super()._stream(
                            messages,
                            stop=stop,
                            run_manager=None,
                            **kwargs,
                        )
                    )
                    if self.recover_structured_json_from_reasoning:
                        chunks, empty_disposition = (
                            _sanitize_stream_reasoning_and_recover_json(chunks)
                        )
                        if empty_disposition is not None:
                            error = ProviderEmptyStructuredOutputError(
                                provider_group=self.key_group,
                                reasoning_disposition=empty_disposition,
                            )
                            _trace_call(
                                self,
                                messages,
                                None,
                                error,
                                t0,
                                cycle,
                            )
                            raise error
                    if chunks:
                        _trace_call(
                            self,
                            messages,
                            generate_from_stream(iter(chunks)),
                            None,
                            t0,
                            cycle,
                        )
                    return chunks
                except (openai.APIError, httpx.TransportError, AssertionError) as e:
                    if _is_gptload_deterministic_status_error(e, self.key_group):
                        _trace_call(self, messages, None, e, t0, cycle)
                        raise
                    if isinstance(e, openai.AuthenticationError) and _key_count(self.key_group) > 1:
                        _trace_call(self, messages, None, e, t0, cycle)
                        authentication_failures += 1
                        last_exc = e
                        if authentication_failures >= _key_count(self.key_group):
                            raise _ChannelExhausted(
                                last_exc, "all configured keys failed authentication"
                            ) from e
                        self._rotate_to_next_key()
                        continue
                    if _is_channel_switch_error(e):
                        _trace_call(self, messages, None, e, t0, cycle)
                        terminal = (
                            ProviderConfigurationError(
                                provider_group=self.key_group,
                                attempts=cumulative_attempt,
                                max_attempts=max_attempts,
                                last_error=e,
                            )
                            if self.key_group in _GPTLOAD_PROVIDER_GROUPS
                            else e
                        )
                        reason = getattr(terminal, "code", f"deterministic {type(e).__name__}")
                        raise _ChannelExhausted(terminal, reason) from e
                    if not _is_rotatable_error(e, self.key_group):
                        _trace_call(self, messages, None, e, t0, cycle)
                        raise
                    last_exc = e
                    self._handle_transient_failure(
                        e,
                        attempt=cumulative_attempt,
                        max_attempts=max_attempts,
                        messages=messages,
                        t0=t0,
                        cycle=cycle,
                        stream=True,
                    )
            if authentication_failures == _key_count(self.key_group):
                raise _ChannelExhausted(last_exc, "all configured keys failed authentication")
        raise _ChannelExhausted(last_exc, "all configured keys exhausted")

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """Stream through the same key/provider fallback policy as ``_generate``."""
        if not self.fallback_chain:
            try:
                chunks = self._stream_within_group(messages, stop=stop, **kwargs)
            except _ChannelExhausted as ce:
                raise ce.last_exc if ce.last_exc is not None else RuntimeError(
                    "channel exhausted without exception"
                )
        else:
            last_exc: Exception | None = None
            chunks = None
            for lap in range(_MAX_CHANNEL_LAPS):
                for group in self.fallback_chain:
                    self._switch_to_group(group)
                    try:
                        chunks = self._stream_within_group(messages, stop=stop, **kwargs)
                        break
                    except _ChannelExhausted as ce:
                        last_exc = ce.last_exc
                        _log_channel_switch(self.fallback_chain, group, ce.reason, lap)
                if chunks is not None:
                    break
            if chunks is None:
                _log(
                    f"[channel-fallback] 链 {self.fallback_chain} ×{_MAX_CHANNEL_LAPS}圈全失败，"
                    f"raise 最后一个异常: {type(last_exc).__name__ if last_exc else None}"
                )
                raise last_exc if last_exc is not None else RuntimeError(
                    "all channels exhausted without exception"
                )

        for chunk in chunks:
            if run_manager:
                logprobs = (chunk.generation_info or {}).get("logprobs")
                run_manager.on_llm_new_token(chunk.text, chunk=chunk, logprobs=logprobs)
            yield chunk


def _default_model_for_group(group: str) -> str:
    """各 group 的默认模型名（抽取自 build_chat_model 的 if-elif 链，给 _switch_to_group 复用）。
    light/full 走 LLM_LIGHT_MODEL/LLM_MODEL，固定渠道组走各自 *_CHAT_MODEL 常量。"""
    if group == "mimo":
        return MIMO_CHAT_MODEL
    if group in {"gptload", "deepseek"}:
        return GPTLOAD_CHAT_MODEL
    if group == "jiji":
        return JIJI_CHAT_MODEL
    if group == "cunai":
        return CUNAI_CHAT_MODEL
    if group == "ooioo":
        return OOIOO_CHAT_MODEL
    if group == "light":
        return LLM_LIGHT_MODEL
    return LLM_MODEL


def _channel_default_headers(group: str, base_url: str | None = None) -> dict[str, str] | None:
    """Known gateway groups override the SDK UA to avoid upstream blocking.

    CunAI 直连、ooioo、GPT-Load 两个执行组及 MiMo 经 cun_copy 代理
    都已真实观察到默认 OpenAI SDK UA 可返回 403，而 curl 兼容 UA 可用。
    该渠道真实撞到：
    openai-python 默认 UA 被上游拦截返回 403 PermissionDeniedError "Your request was
    blocked"，curl 自定义 UA 不受影响。
    jiji 等其余 group 返回 None 保持 ChatOpenAI 默认行为。"""
    is_cun_gptload = group in {"mimo", "gptload", "deepseek"} and "/proxy/cun_copy/" in f"{(base_url or '').rstrip('/')}/"
    return (
        {"User-Agent": "curl/8.0"}
        if group in {"cunai", "ooioo", "gptload", "deepseek"}
        or is_cun_gptload
        else None
    )


def _channel_max_tokens(
    group: str,
    sensetime_max_tokens: int | None,
) -> int | None:
    """Resolve the generation cap without mixing two configuration scopes.

    ``sensetime_max_tokens`` preserves the historical full-channel-only policy
    for long ReAct output. Structured Provider limits use the native
    ``max_tokens`` request body field in ``_channel_extra_body`` instead.
    """
    return sensetime_max_tokens if group == "full" else None


def _channel_extra_body(
    group: str,
    full_extra_body: dict | None,
    request_extra_body: dict | None = None,
    request_max_tokens: int | None = None,
) -> dict | None:
    """Resolve Provider body options using the same scope boundary as tokens."""
    if group in _REQUEST_CONTROL_GROUPS:
        resolved = dict(request_extra_body or {})
        if request_max_tokens is not None:
            # ChatOpenAI normalizes its max_tokens field to
            # max_completion_tokens. GLM's OpenAI-compatible contract uses
            # max_tokens, so keep this narrow request control in extra_body;
            # openai-python merges it into the top-level Provider payload.
            resolved["max_tokens"] = request_max_tokens
        return resolved or None
    return full_extra_body if group == "full" else None


def build_chat_model(
    temperature: float = 0.3,
    model: str | None = None,
    group: str = "full",
    fallback_chain: tuple[str, ...] = (),
    sensetime_max_tokens: int | None = None,
    mimo_model: str | None = None,
    full_extra_body: dict | None = None,
    streaming: bool = False,
    request_max_tokens: int | None = None,
    request_extra_body: dict | None = None,
    recover_structured_json_from_reasoning: bool = False,
) -> RotatingChatOpenAI:
    """src.config.get_chat_model()/get_vision_model()/get_light_chat_model()的实际实现，
    抽到这个模块避免config.py本身依赖langchain_openai（config.py目前保持"只做常量和
    轻量factory"的定位）。

    group 支持 full、light、mimo、gptload、deepseek（兼容名）、jiji、cunai、ooioo。mimo_model 只决定 MiMo 使用标准版还是 Pro，
    不另建 key 池；两档模型共享同一组聚合 key，便于后续接 gpt-load 配额池。

    model参数默认None时优先用管理后台运行时切换的模型名（机制C，见src/api/runtime_config.py，
    按group分别维护覆盖值，只支持full/light），没有运行时覆盖值则回退到该group的默认模型
    （LLM_MODEL/LLM_LIGHT_MODEL/MIMO_CHAT_MODEL）；
    get_vision_model()显式传入LLM_VISION_MODEL覆盖，不受运行时切换影响（那是多模态模型，
    跟对话模型是两回事）。
    """
    keys = _keys_for_group(group)
    if group in _FIXED_CHANNEL_GROUPS:
        base_url = _current_base_url(group)
    else:
        base_url = LLM_BASE_URL
    selected_mimo_model = mimo_model or MIMO_CHAT_MODEL
    default_model = selected_mimo_model if group == "mimo" else _default_model_for_group(group)
    if not (keys and base_url and default_model):
        raise RuntimeError(
            "缺少模型配置，请检查 .env 中的 LLM_API_KEY（或 LLM_API_KEYS） / LLM_BASE_URL / LLM_MODEL"
            if group == "full"
            else "缺少轻量模型配置，请检查 .env 中的 LLM_API_KEYS_LIGHT（可选） / LLM_BASE_URL / LLM_LIGHT_MODEL"
            if group == "light"
            else "缺少MiMo模型配置，请检查 .env 中的 MIMO_TOKEN_PLAN_API_KEYS/MIMO_API_KEYS（至少一个）"
            " / MIMO_TOKEN_PLAN_BASE_URL/MIMO_CHAT_BASE_URL / MIMO_CHAT_MODEL"
            if group == "mimo"
            else "缺少GPT-Load配置，请检查GPTLOAD_API_KEYS（或旧MIMO_GPTLOAD_API_KEYS）"
            " / GPTLOAD_BASE_URL / GPTLOAD_CHAT_MODEL"
            if group in {"gptload", "deepseek"}
            else "缺少jiji配置，请检查JIJI_API_KEY（或JIJI_API_KEYS）"
            " / JIJI_BASE_URL / JIJI_CHAT_MODEL"
            if group == "jiji"
            else "缺少ooioo配置，请检查OOIOO_API_KEY（或OOIOO_API_KEYS）"
            " / OOIOO_BASE_URL / OOIOO_CHAT_MODEL"
            if group == "ooioo"
            else "缺少CUN.AI直连渠道配置，请检查 .env 中的 CUNAI_API_KEY（或 CUNAI_API_KEYS）"
            " / CUNAI_BASE_URL / CUNAI_CHAT_MODEL"
            if group == "cunai"
            else "缺少LLM模型配置，请检查 .env 中的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL"
        )

    if model is None and group not in _FIXED_CHANNEL_GROUPS:
        from src.api.runtime_config import get_effective_model

        model = get_effective_model(group)

    # 渠道特异配置走 helper 集中维护，_switch_to_group 也复用，
    # 避免 fallback 时遗漏或泄漏上一组的请求参数。
    default_headers = _channel_default_headers(group, base_url)
    initial_max_tokens = _channel_max_tokens(
        group,
        sensetime_max_tokens,
    )
    initial_extra_body = _channel_extra_body(
        group,
        full_extra_body,
        request_extra_body,
        request_max_tokens,
    )
    return RotatingChatOpenAI(
        base_url=base_url,
        api_key=_current_key(group),
        model=model or default_model,
        temperature=temperature,
        timeout=(LIGHT_MODEL_TIMEOUT_SECONDS if group == "light" else CHAT_MODEL_TIMEOUT_SECONDS),
        max_retries=0,  # 重试由本模块显式记录，不让SDK隐藏重放。
        key_group=group,
        default_headers=default_headers,
        max_tokens=initial_max_tokens,
        streaming=streaming,
        extra_body=initial_extra_body,
        fallback_chain=fallback_chain,
        sensetime_max_tokens=sensetime_max_tokens,
        mimo_model=selected_mimo_model,
        full_extra_body=full_extra_body,
        request_max_tokens=request_max_tokens,
        request_extra_body=request_extra_body,
        recover_structured_json_from_reasoning=recover_structured_json_from_reasoning,
    )


def build_chat_model_with_fallback(
    chain: tuple[str, ...] | None = None,
    sensetime_max_tokens: int | None = None,
    temperature: float = 0.3,
    mimo_model: str = MIMO_CHAT_MODEL,
    streaming: bool = False,
) -> RotatingChatOpenAI:
    """长攻略模型入口：使用当前进程显式选择的Provider。

    mimo_model 可按 Agent 选择标准版或 Pro；两者共享 MiMo key 池。
    整条渠道链只走一遍；GPT-Load 组内可按有界短退避原请求。
    """
    requested_chain = chain if chain is not None else _CHANNEL_FALLBACK_CHAIN
    resolved = tuple(g for g in requested_chain if _keys_for_group(g))
    if not resolved:
        raise RuntimeError(
            f"长攻略模型链缺少{requested_chain!r}配置；不会静默切换到其他Provider"
        )
    head = resolved[0]
    return build_chat_model(
        temperature=temperature,
        group=head,
        fallback_chain=resolved,
        sensetime_max_tokens=sensetime_max_tokens,
        mimo_model=mimo_model,
        streaming=streaming,
    )


def build_poi_recovery_chat_model(temperature: float = 0.3) -> RotatingChatOpenAI:
    """Build the restricted POI recovery model after primary-provider retries exhaust.

    The optional ooioo channel remains a GPT-Load-only recovery path. A run
    explicitly pinned to another provider stays on that provider so an
    experiment cannot silently mix channels.
    """
    if _CHANNEL_FALLBACK_CHAIN == ("gptload",) and _keys_for_group("ooioo"):
        return build_chat_model(temperature=temperature, group="ooioo")
    return build_chat_model_with_fallback(temperature=temperature)


def poi_recovery_model_identity() -> dict[str, str]:
    """Return the provider/model selected for POI recovery audit events."""
    if _CHANNEL_FALLBACK_CHAIN == ("gptload",) and _keys_for_group("ooioo"):
        return {"provider": "ooioo", "model": OOIOO_CHAT_MODEL}
    group = _CHANNEL_FALLBACK_CHAIN[0]
    return {"provider": group, "model": _default_model_for_group(group)}


def primary_travel_model_identity() -> dict[str, str]:
    """Return the configured primary Provider identity for experiment metadata."""
    group = _CHANNEL_FALLBACK_CHAIN[0]
    return {
        "provider_group": group,
        "provider": "jiji.cc" if group == "jiji" else "GPT-Load",
        "model": _default_model_for_group(group),
    }


def build_verification_chat_model(
    temperature: float = 0,
    request_max_tokens: int | None = None,
    disable_reasoning: bool = False,
) -> RotatingChatOpenAI:
    """Narrow structured-task chain: use the configured primary travel Provider.

    This boundary covers material extraction and semantic quality gates without
    sending their short JSON prompts through the full SenseNova channel. The
    GPT-Load credentials remain in the runtime mimo bucket. jiji credentials
    remain environment-only and use the explicit ``jiji`` trace group. Request
    controls follow either supported Provider rather than reusing full-only
    SenseNova configuration fields.
    """
    resolved = tuple(group for group in _VERIFICATION_FALLBACK_CHAIN if _keys_for_group(group))
    if not resolved:
        raise RuntimeError(
            f"结构化模型链缺少{_VERIFICATION_FALLBACK_CHAIN!r}配置"
        )
    return build_chat_model(
        temperature=temperature,
        group=resolved[0],
        fallback_chain=resolved,
        request_max_tokens=request_max_tokens,
        request_extra_body={"thinking": {"type": "disabled"}} if disable_reasoning else None,
        recover_structured_json_from_reasoning=True,
        streaming=True,
    )


def build_companion_chat_model(temperature: float = 0.3) -> RotatingChatOpenAI:
    """飞书陪伴问答链：MiMo主用、商汤承担短问答最终兜底。"""
    resolved = tuple(group for group in _COMPANION_FALLBACK_CHAIN if _keys_for_group(group))
    if not resolved:
        raise RuntimeError("陪伴问答模型链缺少 MiMo/商汤 key")
    return build_chat_model(
        temperature=temperature,
        group=resolved[0],
        fallback_chain=resolved,
        mimo_model=MIMO_CHAT_MODEL,
    )


def build_companion_router_model(temperature: float = 0) -> RotatingChatOpenAI:
    """陪伴意图分类链：轻量模型优先，失败后用MiMo保证可路由。"""
    resolved = tuple(group for group in _COMPANION_ROUTER_CHAIN if _keys_for_group(group))
    if not resolved:
        raise RuntimeError("陪伴意图分类模型链缺少 light/MiMo key")
    return build_chat_model(
        temperature=temperature,
        group=resolved[0],
        fallback_chain=resolved,
        mimo_model=MIMO_CHAT_MODEL,
    )


def _serialize_message_full(m) -> dict:
    """LLM_TRACE_FULL 全文序列化：跟 _trace_call 里的摘要序列化平行存在，区别是 content
    存全文（不只是 len）、tool_calls 存完整 name+args（不只是 name）、invalid_tool_calls
    存完整 args_head 不截断到 80 字。用于隔离重放测试还原"故障发生那一刻的真实完整历史"。"""
    content = getattr(m, "content", None)
    if content is None and isinstance(m, dict):
        content = m.get("content", "")
    tool_calls = getattr(m, "tool_calls", None) or (m.get("tool_calls") if isinstance(m, dict) else None) or []
    invalid_tool_calls = (
        getattr(m, "invalid_tool_calls", None)
        or (m.get("invalid_tool_calls") if isinstance(m, dict) else None)
        or []
    )
    tool_call_id = getattr(m, "tool_call_id", None) or (m.get("tool_call_id") if isinstance(m, dict) else None)
    return {
        "role": getattr(m, "type", None) or (m.get("role") if isinstance(m, dict) else None) or type(m).__name__,
        "content": content,
        "tool_call_id": tool_call_id,
        "tool_calls": [
            {"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")} for tc in tool_calls
        ]
        or None,
        "invalid_tool_calls": [
            {"name": itc.get("name"), "args": itc.get("args"), "error": itc.get("error")}
            for itc in invalid_tool_calls
        ]
        or None,
    }


def _trace_call(model, messages, result, exc, t0, cycle, *, provider_retry=None):
    """每次 _generate 往返记一条 trace（2026-08-10，docs/ISSUES.md 第108条后追加）。
    成功记 finish_reason/usage/reasoning/tool_calls/invalid_tool_calls；失败记 error_type/status/msg。
    走 src.llm_trace.append_trace 落盘到 projects/<id>/agents/<agent>/trace.jsonl——无 trace context
    （不在 agent run 里，如 admin key-test 或 B 类 structured_output judge）时 append_trace 静默跳过。

    messages 只记概要（role/len/tool_call 名），不记全文，避免 trace 爆炸——够定位"第几步、prompt
    多大、哪个工具、reasoning 多少、撞什么错"。

    LLM_TRACE_FULL=1 时额外调 append_trace_full 落盘完整 messages（含每条 tool 返回的原文），
    用 append_trace 返回的 seq 关联起来，供隔离重放测试直接用（见 src.llm_trace 模块开头说明）。

    result 是 langchain ChatResult（成功时），exc 是 openai 异常（失败时）。两者其一为 None。
    """
    from src.llm_trace import LLM_TRACE_FULL, append_trace, append_trace_full

    rec: dict = {
        "group": model.key_group,
        "provider_group": "gptload" if model.key_group in {"gptload", "deepseek"} else model.key_group,
        "model_name": model.model_name,
        "elapsed_s": round(time.time() - t0, 2),
        "cycle": cycle,
        "fallback_chain": list(model.fallback_chain) if model.fallback_chain else [],
    }
    if provider_retry:
        rec["provider_retry"] = provider_retry
    # messages 概要：角色/长度/工具调用名/invalid_tool_calls 报错头。不记全文。
    rec["messages"] = [
        {
            "role": getattr(m, "type", type(m).__name__),
            "len": len(m.content) if hasattr(m, "content") and isinstance(m.content, str) else 0,
            "tool_calls": [tc.get("name") for tc in getattr(m, "tool_calls", [])] or None,
            "invalid_tool_calls": [
                {"name": itc.get("name"), "error": (itc.get("error") or "")[:120]}
                for itc in getattr(m, "invalid_tool_calls", [])
            ]
            or None,
        }
        for m in (messages or [])
    ]
    if exc is not None:
        rec["ok"] = False
        rec["error_type"] = type(exc).__name__
        rec["status_code"] = getattr(exc, "status_code", None)
        rec["error_msg"] = str(exc)[:200]
    else:
        rec["ok"] = True
        # 成功：从 ChatResult 取 finish_reason/usage，从返回 AIMessage 取 tool_calls/invalid_tool_calls。
        # finish_reason 在 ChatGeneration.generation_info（调研§6），不在 AIMessage.response_metadata。
        # usage_metadata（含 output_token_details.reasoning，§82 截断就看这个）在 AIMessage 上。
        try:
            gen = result.generations[0]  # ChatGeneration
            ai = gen.message
            rec["finish_reason"] = gen.generation_info.get("finish_reason")
            rec["usage"] = None
            um = getattr(ai, "usage_metadata", None)
            if um:
                rec["usage"] = {
                    "input": um.get("input_tokens"),
                    "output": um.get("output_tokens"),
                    "reasoning": (um.get("output_token_details") or {}).get("reasoning"),
                }
            rec["tool_calls"] = [tc.get("name") for tc in (ai.tool_calls or [])] or None
            rec["invalid_tool_calls"] = [
                {
                    "name": itc.get("name"),
                    "args_head": (itc.get("args") or "")[:80],  # 截断的原始 args 串前80字
                    "error": (itc.get("error") or "")[:120],  # JSONDecodeError 报错
                }
                for itc in (ai.invalid_tool_calls or [])
            ] or None
        except Exception:
            rec["parse_warning"] = "无法从 ChatResult 提取 finish_reason/usage"
    seq = append_trace(rec)
    if LLM_TRACE_FULL and seq is not None:
        full_rec: dict = {
            "group": rec["group"],
            "model_name": rec["model_name"],
            "elapsed_s": rec["elapsed_s"],
            "cycle": rec["cycle"],
            "fallback_chain": rec["fallback_chain"],
            "ok": rec["ok"],
            # 请求方向：这次_generate实际发出去的完整messages原文（不截断），
            # 是隔离重放测试要用的"故障发生那一刻的真实完整历史"。
            "request_messages": [_serialize_message_full(m) for m in (messages or [])],
        }
        if exc is not None:
            full_rec["error_type"] = rec["error_type"]
            full_rec["status_code"] = rec["status_code"]
            full_rec["error_msg"] = rec["error_msg"]
        else:
            full_rec["finish_reason"] = rec.get("finish_reason")
            full_rec["usage"] = rec.get("usage")
            # 响应方向：这次_generate实际收到的AIMessage原文（tool_calls的完整args，不截断）。
            try:
                full_rec["response_message"] = _serialize_message_full(result.generations[0].message)
            except Exception:
                full_rec["response_parse_warning"] = "无法从 ChatResult 提取 response message"
        append_trace_full(seq, full_rec)
