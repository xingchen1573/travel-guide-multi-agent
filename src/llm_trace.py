"""LLM 调用 trace 落盘（2026-08-10新增，docs/ISSUES.md 第108条后追加）。

青甘评测曾连续遇到认证、网关、权限和输出截断故障，每次失败想查
"撞错时 prompt 长什么样、累积到第几轮、reasoning
token 多少、tool_call 是不是被截断"——都查不到。changelog.jsonl 只记动作不记 LLM I/O。

本模块用 contextvars 把"当前在跑哪个 project 的哪个 agent"线程到 RotatingChatOpenAI._generate
（model 实例本身拿不到 project/agent，见 docs/ISSUES.md 第108条调研§3/§4）。node 函数入口
set_trace_context，_generate 里 append_trace 落盘到 projects/<id>/agents/<agent>/trace.jsonl。
不在 agent run 里（无 context）的调用静默跳过——admin key-test 用裸 ChatOpenAI 根本不经过
RotatingChatOpenAI，B 类 structured_output judge 调用点没 set 也会静默跳过。

不记 messages 全文（只记 role/len/tool_call 名），避免 trace 爆炸；够定位"第几步、prompt 多大、
哪个工具、reasoning 多少、撞什么错"。

**LLM_TRACE_FULL 开关**（2026-08-10 隔离测试前追加）：上面的摘要版trace只存了messages的
role/len，事后想拿"故障发生那一刻的真实完整历史"去做隔离重放测试（而不是靠人工拼素材模拟历史，
后者验证过跟真实故障现场有系统性差异——人工拼的静态材料块测不出真实react多轮循环里AI决策/
工具调用交替的结构，也诱导不出真实的工具调用行为）时，摘要版完全不够用。设置环境变量
`LLM_TRACE_FULL=1` 后，`append_trace` 额外把完整messages（含每条tool返回的原文，不截断）
落盘到 `projects/<id>/agents/<agent>/trace_full.jsonl`（跟摘要版分文件，不影响现有逻辑/测试，
默认关闭）；失败时（ok=False）额外单独存一份 `failure_snapshot_seq<N>.json`，装当次请求的
完整messages，方便之后不用扫全量trace_full直接定位"死之前那一刻"。
"""
from __future__ import annotations

import contextvars
import json
import os
import time
from pathlib import Path
from typing import Any

from src.config import PROJECTS_DIR

LLM_TRACE_FULL = os.environ.get("LLM_TRACE_FULL") == "1"

# 运行时上下文：当前在跑哪个 project 的哪个 agent。
# node 函数入口处 set，_generate 里 get（同线程内自动继承；langgraph 用 Send() 并行会自动
# 复制 contextvar 到子任务，POI 并行也能正确归属——这是 contextvars 的语义保证，不用手搓）。
_current_project_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_trace_project_id", default=None
)
_current_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_trace_agent", default=None
)
# 同一 agent run 内的调用序号，跨 _generate 自增（一次 agent.invoke 会产生 N 条，react 多步）。
_call_seq: contextvars.ContextVar[int] = contextvars.ContextVar("llm_trace_seq", default=0)


def set_trace_context(project_id: str | None, agent: str | None) -> None:
    """node 函数入口调，set 当前 project + agent。后续同线程/_generate 里都能 get 到。

    不返回 token 做 reset——langgraph 的 node 是独立调用栈，进 node 前 contextvar 默认值是
    None（不在 agent run 里），set 之后整个 node 执行期间都对，node 返回后context自然回退到上层
    （上层通常是 main loop 没有 agent context，正好静默跳过非 agent 的 LLM 调用）。
    POI 并行：每个 Send("poi_detail_agent_node", {...}) 子任务继承 set 时的 contextvar 副本，
    各分支独立、不串。
    """
    _current_project_id.set(project_id)
    _current_agent.set(agent)


def get_trace_context() -> tuple[str | None, str | None]:
    """_generate hook 里读当前归属。返回 (project_id, agent)，无 context 时 (None, None)。"""
    return _current_project_id.get(), _current_agent.get()


def _trace_path() -> Path | None:
    """trace 落盘路径：projects/<project_id>/agents/<agent>/trace.jsonl。
    无 project/agent context 时返回 None（调用方应静默跳过不写）。"""
    pid = _current_project_id.get()
    agent = _current_agent.get()
    if not pid or not agent:
        return None
    return PROJECTS_DIR / pid / "agents" / agent / "trace.jsonl"


def append_context_event(record: dict[str, Any]) -> None:
    """Persist compact context-budget events for the active agent, if any."""
    pid = _current_project_id.get()
    agent = _current_agent.get()
    if not pid or not agent:
        return
    path = PROJECTS_DIR / pid / "agents" / agent / "context_events.jsonl"
    payload = {
        **record,
        "ts": time.time(),
        "project_id": pid,
        "agent": agent,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def read_recent_trace(project_id: str, agent: str, limit: int = 10) -> list[dict[str, Any]]:
    """读取某个project的某个agent最近limit条trace记录，按时间正序（旧到新）——跟
    health.py的read_heartbeat_history(limit)同款读取风格。trace.jsonl不存在（这个
    agent这次run还没真正发起过LLM调用）时返回空列表，不抛错。

    这里显式接受project_id/agent参数（不依赖contextvar），因为调用方（飞书进度展示）
    通常在轮询线程里读取，跟真正产生trace记录的agent执行线程是不同线程/不同调用时机，
    不能依赖当时的_current_project_id/_current_agent——那两个contextvar只在agent
    实际执行期间、同一调用栈内有效。"""
    path = PROJECTS_DIR / project_id / "agents" / agent / "trace.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-limit:]


def append_trace(record: dict[str, Any]) -> int | None:
    """_generate hook 调。无 context（不在 agent run 里）静默跳过不写、不抛错，返回 None。
    有 context 时 stamp 上 seq/ts/project_id/agent，追加写到 trace.jsonl，返回本次用的 seq——
    调用方（_trace_call）需要这个 seq 去关联 append_trace_full 写的全文记录/失败快照，
    两者必须是同一个 seq 才能事后对上号。
    seq 在同一 agent run 内自增（跨 _generate），便于事后按 seq 还原"第几次调用"。"""
    p = _trace_path()
    if p is None:
        return None
    seq = _call_seq.get()
    _call_seq.set(seq + 1)
    record["seq"] = seq
    record["ts"] = time.time()
    record["project_id"] = _current_project_id.get()
    record["agent"] = _current_agent.get()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return seq


def _full_trace_path() -> Path | None:
    """trace_full 落盘路径：projects/<project_id>/agents/<agent>/trace_full.jsonl。
    跟摘要版 _trace_path 同款判断，无 context 时返回 None。"""
    pid = _current_project_id.get()
    agent = _current_agent.get()
    if not pid or not agent:
        return None
    return PROJECTS_DIR / pid / "agents" / agent / "trace_full.jsonl"


def _failure_snapshot_path(seq: int) -> Path | None:
    """单次失败快照落盘路径：projects/<project_id>/agents/<agent>/failure_snapshot_seq<N>.json。
    跟 trace_full.jsonl 分开存，方便直接定位"死之前那一刻"而不用扫全量 trace_full。"""
    pid = _current_project_id.get()
    agent = _current_agent.get()
    if not pid or not agent:
        return None
    return PROJECTS_DIR / pid / "agents" / agent / f"failure_snapshot_seq{seq}.json"


def append_trace_full(seq: int, full_record: dict[str, Any]) -> None:
    """LLM_TRACE_FULL=1 时才写（调用方应先检查这个开关，避免禁用时白白构造大对象）。
    seq 必须跟同一次调用的 append_trace 返回值一致，才能在 trace.jsonl（摘要）和
    trace_full.jsonl（全文）之间按 seq 对号。full_record 里 messages 应是完整文本
    （不截断/不只记长度），失败时（full_record.get("ok") is False）额外单独存一份
    failure_snapshot_seq<N>.json，供隔离重放测试直接读取"故障发生那一刻的真实完整历史"，
    不用再靠人工拼素材模拟历史（后者验证过跟真实故障现场有系统性差异，见本模块开头说明）。"""
    p = _full_trace_path()
    if p is None:
        return
    full_record["seq"] = seq
    full_record["ts"] = time.time()
    full_record["project_id"] = _current_project_id.get()
    full_record["agent"] = _current_agent.get()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(full_record, ensure_ascii=False, default=str) + "\n")
    if full_record.get("ok") is False:
        snap_path = _failure_snapshot_path(seq)
        if snap_path is not None:
            snap_path.write_text(
                json.dumps(full_record, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )


def reset_seq() -> None:
    """重置 _call_seq（测试用；或一个 agent run 结束后想清零）。生产环境不调——seq 跨 agent
    继续递增也无妨，每条都带 project_id/agent 能区分归属。"""
    _call_seq.set(0)
