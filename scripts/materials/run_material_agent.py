"""素材agent CLI手动驱动脚本：给定目的地+需求，走完整的
提案(propose)->采集(collect)流程。

默认auto_confirm=True，自动全部采集提案候选，不暂停等待人工确认（还没有真实交互
界面前，暂停也没人能看到候选去点选，见docs/ISSUES.md相关记录）。想手动测试
interrupt暂停/恢复这条路径，加 --manual-confirm。

用法：
    PYTHONIOENCODING=utf-8 python -m scripts.materials.run_material_agent 成都 "给我3条成都最新的抖音攻略"
    PYTHONIOENCODING=utf-8 python -m scripts.materials.run_material_agent 成都 "给我3条成都最新的抖音攻略" --manual-confirm

见 src/agents/material_agent/collection_graph.py 的设计说明。
"""
from __future__ import annotations

import sys

from langgraph.types import Command

from src.agents.material_agent.collection_graph import build_material_collection_graph


def _print_candidates(candidates: list[dict]) -> None:
    print(f"\n=== 候选（共{len(candidates)}个）===")
    for i, c in enumerate(candidates):
        print(
            f"[{i}] platform={c.get('platform')} score={c.get('score', '?')} | "
            f"{c.get('author') or c.get('nickname', '')}\n"
            f"     {c.get('title') or c.get('desc', '')[:60]}\n"
            f"     {c.get('source_url')}"
        )


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--manual-confirm"]
    manual_confirm = "--manual-confirm" in sys.argv[1:]
    if len(args) != 2:
        print("用法: python -m scripts.materials.run_material_agent <目的地> <需求描述> [--manual-confirm]")
        sys.exit(1)

    topic, request = args
    print(f"=== 素材采集: {topic} — {request} ===\n")

    graph = build_material_collection_graph()
    thread_id = f"material:{topic}:cli"
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke(
        {"topic": topic, "request": request, "auto_confirm": not manual_confirm}, config
    )

    while "__interrupt__" in result:
        interrupt_data = result["__interrupt__"][0].value
        print(f"\n{interrupt_data['question']}")
        if interrupt_data.get("summary"):
            print(f"（摘要：{interrupt_data['summary']}）")
        _print_candidates(interrupt_data.get("candidates", []))

        decision = input("\n你的决定: ").strip()
        result = graph.invoke(Command(resume=decision), config)

    print("\n=== 完成 ===")
    print(result.get("result_summary", "(无结果摘要)"))


if __name__ == "__main__":
    main()
