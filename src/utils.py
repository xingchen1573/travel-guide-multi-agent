"""通用工具函数。"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Optional

import openai
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError

from src.context_editing import (
    ContextBudgetExceededError,
)
from src.llm_rotation import (
    GPTLOAD_MAX_ATTEMPTS,
    ProviderRetryDeferredError,
    ProviderRetryExhaustedError,
    ProviderRetryTerminalError,
    provider_retry_attempt_offset,
)


_PROVIDER_RETRY_DEADLINE_TABLE = "provider_retry_deadlines"


def _sqlite_checkpointer(agent) -> SqliteSaver | None:
    checkpointer = getattr(agent, "checkpointer", None)
    return checkpointer if isinstance(checkpointer, SqliteSaver) else None


def _ensure_provider_retry_deadline_table(cur) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_PROVIDER_RETRY_DEADLINE_TABLE} (
            thread_id TEXT PRIMARY KEY,
            provider_group TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            retry_after_seconds REAL NOT NULL,
            next_retry_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # Agent lifecycle cleanup already deletes its LangGraph thread. Keep the
    # adjacent deadline record under the same lifecycle without requiring every
    # call site of clear_agent_thread() to know about this table.
    cur.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS clear_provider_retry_deadline_after_thread_delete
        AFTER DELETE ON checkpoints
        WHEN NOT EXISTS (
            SELECT 1 FROM checkpoints WHERE thread_id = OLD.thread_id
        )
        BEGIN
            DELETE FROM {_PROVIDER_RETRY_DEADLINE_TABLE}
            WHERE thread_id = OLD.thread_id;
        END
        """
    )


def _persist_provider_retry_deadline(
    agent,
    thread_id: str,
    error: ProviderRetryDeferredError,
    *,
    attempts: int | None = None,
) -> bool:
    """Persist a deferred retry beside the agent's durable checkpoints."""
    saver = _sqlite_checkpointer(agent)
    if saver is None:
        return False
    with saver.cursor() as cur:
        _ensure_provider_retry_deadline_table(cur)
        cur.execute(
            f"""
            INSERT INTO {_PROVIDER_RETRY_DEADLINE_TABLE} (
                thread_id,
                provider_group,
                attempts,
                retry_after_seconds,
                next_retry_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                provider_group = excluded.provider_group,
                attempts = excluded.attempts,
                retry_after_seconds = excluded.retry_after_seconds,
                next_retry_at = excluded.next_retry_at,
                updated_at = excluded.updated_at
            """,
            (
                thread_id,
                error.provider_group,
                error.attempts if attempts is None else attempts,
                error.retry_after_seconds,
                error.next_retry_at,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return True


def _load_provider_retry_deadline(agent, thread_id: str) -> dict | None:
    saver = _sqlite_checkpointer(agent)
    if saver is None:
        return None
    with saver.cursor() as cur:
        _ensure_provider_retry_deadline_table(cur)
        row = cur.execute(
            f"""
            SELECT provider_group, attempts, retry_after_seconds, next_retry_at
            FROM {_PROVIDER_RETRY_DEADLINE_TABLE}
            WHERE thread_id = ?
            """,
            (thread_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "provider_group": str(row[0]),
        "attempts": int(row[1]),
        "retry_after_seconds": float(row[2]),
        "next_retry_at": str(row[3]),
    }


def _clear_provider_retry_deadline(agent, thread_id: str | None) -> None:
    if not thread_id:
        return
    saver = _sqlite_checkpointer(agent)
    if saver is None:
        return
    with saver.cursor() as cur:
        _ensure_provider_retry_deadline_table(cur)
        cur.execute(
            f"DELETE FROM {_PROVIDER_RETRY_DEADLINE_TABLE} WHERE thread_id = ?",
            (thread_id,),
        )


def _remaining_persisted_retry_delay(deadline: dict) -> float:
    try:
        retry_at = datetime.fromisoformat(
            str(deadline["next_retry_at"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError):
        # A malformed legacy row must not cause an early replay. Waiting the original
        # duration is conservative; a successful or terminal call removes the row.
        return max(0.0, float(deadline.get("retry_after_seconds") or 0.0))
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(
        0.0,
        (
            retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds(),
    )


def invoke_with_retry(
    agent,
    messages: list[dict],
    max_retries: int = 6,
    backoff_seconds: float = 2.0,
    *,
    thread_id: Optional[str] = None,
    resume_from_checkpoint: bool = False,
    recursion_limit: Optional[int] = None,
):
    """包一层重试，应对调用LLM API时的瞬时网络故障（连接中断/SSL EOF等）。

    这类错误是网络层面的瞬时问题，不是业务逻辑错误，重试通常就能过去——
    真实跑过一次，多agent串联的长流程里，任何一次网络抖动都会让整个流程从头崩溃，
    之前已经完成的所有步骤（可能是好几分钟的LLM调用）全部作废，成本很高，值得包一层重试。

    2026-08-13加强（见docs/ISSUES.md第118条）：原先max_retries=3+线性退避(2s/4s，
    总容错约6秒)，在route_agent这类单节点要发起200+次网络调用（web_search+高德+
    素材检索，第114/115条规则触发后单节点常规跑4-5小时）的场景下明显不够——真实
    连续4次跑同一个8天青甘大环线场景，3次都在这类长节点里撞上APIConnectionError，
    每次InMemorySaver（进程内存态checkpointer）都让当次几小时的进度全部作废，代价
    很高。改成max_retries=6+指数退避(2s/4s/8s/16s/32s，总容错约62秒)，覆盖更长的
    瞬时网络中断窗口，不改变函数签名/调用方式，纯内部参数调整。

    2026-08-13新增thread_id可选参数（见docs/ISSUES.md第120条）：agent必须是绑了
    checkpointer构建的react-agent（见src/agent_checkpoint.py）。传thread_id时，
    第一次尝试正常传完整messages+config(thread_id)（会被checkpointer记录）；异常后
    的重试改传input=None+同一个config（LangGraph续跑语义：从上次checkpoint继续，
    不是重新开始）——这样route_draft这类已经做了几十次工具调用的对话，撞到网络抖动
    重试时不会把已经做的工作全部重来一遍，只续上被打断的那一步。传thread_id时，
    ``resume_from_checkpoint=True``用于进程重启后第一次重新进入一个尚未完成的agent
    节点：第一次调用也传``input=None``，直接从已有thread checkpoint继续；默认False时
    保持原有的“第一次传完整messages”语义。未传thread_id时（默认None）行为完全不变，
    向后兼容——大多数调用点（短平快的单轮修复对话）不需要
    这层机制，强行接反而增加thread_id复用导致历史污染的风险面，见调用方注释。

    2026-09-07新增Provider延后恢复：GPT-Load返回超过进程内等待预算的Retry-After时，
    Provider层抛出带next_retry_at的ProviderRetryDeferredError。只有传入thread_id、能够
    安全复用LangGraph checkpoint时，本层才等待剩余时间并在下一轮以input=None从同一
    恢复入口继续。SQLite checkpointer会在同一数据库持久化绝对deadline；新进程恢复时
    先等待deadline，并把已消耗attempt作为下一次Provider序列的一次性offset，确保重启
    不能重置“首次+4次”的总上限；成功或确定性终态后清除。没有checkpoint的裸结构化
    调用直接保留延后异常，由其所属阶段处理。Provider重试耗尽仍立即上抛，避免与本层
    六次通用重试相乘。
    """
    last_exc: Exception | None = None
    config = {}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    invoke_config = config or None
    consumed_provider_attempts = 0
    if thread_id:
        persisted_deadline = _load_provider_retry_deadline(agent, thread_id)
        if persisted_deadline:
            consumed_provider_attempts = int(persisted_deadline.get("attempts") or 0)
            if consumed_provider_attempts >= GPTLOAD_MAX_ATTEMPTS:
                _clear_provider_retry_deadline(agent, thread_id)
                raise ProviderRetryExhaustedError(
                    provider_group=str(
                        persisted_deadline.get("provider_group") or "gptload"
                    ),
                    attempts=consumed_provider_attempts,
                    max_attempts=GPTLOAD_MAX_ATTEMPTS,
                    last_error=RuntimeError(
                        "persisted provider retry budget already exhausted"
                    ),
                )
            remaining_delay = _remaining_persisted_retry_delay(persisted_deadline)
            if remaining_delay > 0:
                time.sleep(remaining_delay)
    for attempt in range(max_retries):
        retry_scope = None
        try:
            with provider_retry_attempt_offset(consumed_provider_attempts) as retry_scope:
                if thread_id and (attempt > 0 or (attempt == 0 and resume_from_checkpoint)):
                    result = agent.invoke(None, invoke_config)
                elif thread_id:
                    result = agent.invoke({"messages": messages}, invoke_config)
                elif invoke_config:
                    result = agent.invoke({"messages": messages}, invoke_config)
                else:
                    result = agent.invoke({"messages": messages})
        except ProviderRetryDeferredError as e:
            # 长Retry-After只允许走已有checkpoint恢复入口。下一轮attempt>0会
            # 调用agent.invoke(None, same_config)，不会重发完整messages。SQLite
            # checkpointer同时保存deadline，因此进程在等待期间退出后，新进程会先
            # 等到同一绝对时间再恢复，不能把重启当成绕过Retry-After的机会。
            last_exc = e
            if not thread_id:
                raise
            # llm_rotation reports cumulative attempts after consuming the
            # persisted offset. A non-Rotating test double or adapter does not
            # consume that scope, so add its local attempts explicitly.
            cumulative_attempts = (
                int(e.attempts)
                if retry_scope is not None and retry_scope.consumed
                else consumed_provider_attempts + int(e.attempts)
            )
            if cumulative_attempts >= int(e.max_attempts):
                _clear_provider_retry_deadline(agent, thread_id)
                raise ProviderRetryExhaustedError(
                    provider_group=e.provider_group,
                    attempts=cumulative_attempts,
                    max_attempts=e.max_attempts,
                    last_error=e.last_error,
                ) from e
            consumed_provider_attempts = cumulative_attempts
            _persist_provider_retry_deadline(
                agent,
                thread_id,
                e,
                attempts=consumed_provider_attempts,
            )
            if attempt >= max_retries - 1:
                raise
            remaining_delay = e.remaining_delay_seconds()
            if remaining_delay > 0:
                time.sleep(remaining_delay)
        except (
            ContextBudgetExceededError,
            GraphRecursionError,
            ProviderRetryTerminalError,
            openai.AuthenticationError,
            openai.BadRequestError,
            openai.NotFoundError,
            openai.PermissionDeniedError,
            openai.UnprocessableEntityError,
        ):
            # 本地Harness确定性停止与Provider内层已耗尽信号
            # 都不能在这里再重放：前者会重复命中同一边界，后者会
            # 将Provider的5次短退避放大成30次请求。
            _clear_provider_retry_deadline(agent, thread_id)
            raise
        except Exception as e:  # noqa: BLE001 - 故意捕获所有异常，网络层错误的具体类型不好穷举
            last_exc = e
            if retry_scope is not None and retry_scope.consumed:
                # The deferred Provider sequence reached a non-deferred outcome;
                # any later generic failure belongs to a subsequent graph step.
                _clear_provider_retry_deadline(agent, thread_id)
                consumed_provider_attempts = 0
            if attempt < max_retries - 1:
                time.sleep(backoff_seconds * (2 ** attempt))
        else:
            # Cleanup failure must never replay an Agent invocation that already
            # completed and may have persisted user-visible side effects.
            _clear_provider_retry_deadline(agent, thread_id)
            return result
    _clear_provider_retry_deadline(agent, thread_id)
    raise last_exc
