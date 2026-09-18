"""每个project一份持久化的LangGraph checkpointer，供图级/agent内层两层断点续跑复用。

背景（docs/ISSUES.md第120条）：build_graph()此前用InMemorySaver（进程内存态checkpointer），
进程崩溃后几小时的执行状态全部作废，只能改project_id从头重跑。这里改用SqliteSaver，
per-project一个文件（不是全局共享），两层用途共用同一个文件、靠thread_id命名空间区分：

- 图层（Layer 1）：build_graph()自己的checkpointer，thread_id沿用既有约定
  （"{project_id}:plan"/"{project_id}:edit"/"{project_id}:companion"/"{project_id}:channel"），
  让"已经完整跑完的节点"在进程重启后重新invoke时不用重跑。
- agent内层（Layer 2）：route_draft/lifestyle_draft以及按POI隔离的poi_detail_draft这类
  耗时的create_react_agent内部对话循环自己拥有checkpointer。thread_id用本模块的
  agent_thread_id()生成；POI详情额外把turn_id+day+poi_slug编码进thread，避免Send并行
  分支互撞，也让同一轮进程重启后可以续上被打断的工具调用循环。
  route/lifestyle仍用固定逻辑thread并在新一轮开始时clear；POI详情用turn_id生成新thread，
  因而不需要在节点入口清空旧thread。

一个project一个sqlite文件，避免不同project并发跑测时的文件锁竞争；SqliteSaver.setup()
默认开WAL journal_mode，同一文件内多个thread_id/多个连接的读写不会互相阻塞。每次获取
checkpointer都开一个新的sqlite3连接、用完不缓存——简单，代价（毫秒级）相比LLM调用可忽略。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from src.workspace import store


def checkpoint_path(project_id: str) -> Path:
    """per-project的sqlite checkpoint文件路径，天然继承store.PROJECTS_DIR的测试隔离
    （测试monkeypatch store.PROJECTS_DIR到tmp_path时，这里自动跟着变）。"""
    return store.workspace_dir(project_id) / "checkpoint.sqlite"


def _open_saver(project_id: str) -> SqliteSaver:
    path = checkpoint_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def get_graph_checkpointer(project_id: str) -> SqliteSaver:
    """图层（Layer 1）checkpointer，供build_graph()使用。"""
    return _open_saver(project_id)


def get_agent_checkpointer(project_id: str) -> SqliteSaver:
    """agent内层（Layer 2）checkpointer，供build_route_agent等构建函数使用。
    跟get_graph_checkpointer指向同一个文件，独立连接。"""
    return _open_saver(project_id)


def agent_thread_id(project_id: str, node_name: str, suffix: str = "") -> str:
    """Layer 2的thread_id命名。node_name是逻辑上的"这个节点这一类对话"标识
    （如"route_draft"/"lifestyle_draft"），suffix给并行fan-out场景（如POI详情按天+POI）
    加区分，避免并行分支撞thread_id。"""
    base = f"{project_id}:agent:{node_name}"
    return f"{base}:{suffix}" if suffix else base


def poi_detail_agent_thread_id(
    project_id: str,
    day: int,
    poi_name: str,
    turn_id: str,
) -> str:
    """为一次POI详情生成构造稳定且按分支隔离的agent thread。

    ``turn_id``标识一次逻辑上的生成/编辑；同一轮进程重启后会复用它，
    ``day+poi_slug``再把Send fan-out的各个POI分支隔开。
    """
    suffix = f"{turn_id}:day{day}_{store.poi_slug(poi_name)}"
    return agent_thread_id(project_id, "poi_detail_draft", suffix=suffix)


def clear_agent_thread(project_id: str, node_name: str, suffix: str = "") -> None:
    """清空某个agent thread的checkpoint历史，用在每个"逻辑上一次性对话"开始之前
    （node函数开头，跟reset_web_search_log同一个惯例位置）。

    原因：create_react_agent内部messages字段用add_messages reducer，同一个thread_id
    第二次invoke传完整消息列表是**追加**不是覆盖——如果不清空，同一个node_name在不同时刻
    的两次逻辑上无关的对话（比如这次route_draft和几天后一次edit触发的新route_draft）会
    互相污染，新prompt追加到旧对话几小时的完整历史后面，上下文爆炸+逻辑混乱。

    幂等：清空一个不存在的thread是no-op，可无条件调用。"""
    thread_id = agent_thread_id(project_id, node_name, suffix)
    saver = _open_saver(project_id)
    try:
        saver.delete_thread(thread_id)
    finally:
        saver.conn.close()


def clear_agent_thread_family(project_id: str, thread_id: str) -> None:
    """Delete one agent parent thread and its isolated write-verification children.

    The prefix is already a complete, stable work-unit thread id. Only its
    ``:write-verification:`` descendants are included, so unrelated historical
    work units for the same logical Agent remain available for audit/recovery.
    """
    child_prefix = f"{thread_id}:write-verification:"
    saver = _open_saver(project_id)
    try:
        thread_ids = {thread_id}
        for checkpoint in saver.list(None):
            config = getattr(checkpoint, "config", None) or {}
            candidate = (config.get("configurable") or {}).get("thread_id")
            if isinstance(candidate, str) and candidate.startswith(child_prefix):
                thread_ids.add(candidate)
        for candidate in thread_ids:
            saver.delete_thread(candidate)
    finally:
        saver.conn.close()
