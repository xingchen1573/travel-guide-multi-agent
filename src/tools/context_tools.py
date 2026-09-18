"""持久结论层：record_finding 工具（2026-08-11新增，见docs/ISSUES.md相关条目/src/context_editing.py）。

背景：ReAct循环内的上下文压缩（src/context_editing.py的build_context_editing_hook）会把
retrieve_materials/read_workspace/web_search这类"用过即可放手"的工具原文压成摘要——但摘要
保留不了"适用范围/限定条件"这类细节，如果某条结论以后会被跨天/跨条目引用（比如"某段路仅限
某运营商的共享单车可用"），压缩后可能丢失判断依据，导致后续误判成"同一套服务体系"。

record_finding让agent自己判断"这条信息以后可能跨天/跨条目要用"时主动记一笔——不需要额外
一次LLM调用去总结，就是同一个agent在自己的ReAct循环里顺手多调一次工具，跟append_material
（src/tools/material_tools.py）同款模式：写盘、只回一句确认消息。

持久结论受上下文治理显式保护：这个工具的返回内容很短，也不在可压缩工具集合里；
build_context_editing_hook清理旧AI+Tool交互对时会跳过record_finding，因此模型记录在
tool_calls参数里的结论不会随普通检索轮次一起删除。完整请求计量仍会统计这些参数；若持久结论
自身异常膨胀到超过硬预算，系统会显式停止，而不是假装预算仍然可控。
"""
from __future__ import annotations

import json
import fcntl
from datetime import datetime, timezone

from langchain_core.tools import tool

from src.config import PROJECTS_DIR


def make_context_tools(project_id: str, agent: str) -> list:
    @tool
    def record_finding(fact: str) -> str:
        """记录一条以后可能跨天/跨条目引用的关键结论（不是常规工作内容，是那种"以后再次涉及
        同一话题时必须核对适用范围"的信息，比如某段路的通行限制、某种服务的适用范围、某类人群
        的优惠政策）。一两句话说清楚"是什么+适用范围/限定条件"，不需要每天都调用，只在你判断
        这条信息有跨天/跨条目复用风险时才调用。"""
        findings_dir = PROJECTS_DIR / project_id / "agents" / agent
        findings_dir.mkdir(parents=True, exist_ok=True)
        path = findings_dir / "findings.jsonl"
        lock_path = findings_dir / ".findings.lock"
        record = {
            "note_type": "working_note",
            "actor": agent,
            "fact": fact,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return "已记录关键结论"

    return [record_finding]
