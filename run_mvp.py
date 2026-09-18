"""MVP端到端运行脚本：跑一次"三天成都旅游"的完整生成流程。"""
from __future__ import annotations

import json

from src.orchestrator import run_plan
from src.workspace import store


def main():
    project_id = "chengdu-3d-e2e-v9"
    requirements = {
        "destination": "成都",
        "days": 3,
        "style": "美食+人文，不想太赶",
        "budget": "中等，人均每天300-500元",
        "notes": "不吃辣，喜欢拍照打卡",
    }

    print(f"=== 开始生成: {requirements['destination']} {requirements['days']}天 (project_id={project_id}) ===\n")

    result = run_plan(project_id, requirements)

    progress = store.read_progress(project_id)
    print(f"\n=== 运行结束，最终状态: {progress.state.value} ===")
    print("最近一步:", result.get("last_agent_result"))

    route_plan = store.read_workspace(project_id, "route_plan.json")
    print("\n--- route_plan.json 概要 ---")
    if route_plan:
        for day in route_plan.get("days", []):
            print(f"Day{day.get('day')} {day.get('city')}/{day.get('area')}: {day.get('pois')}")
            print(f"  sources: {day.get('sources')}")
    else:
        print("(空)")

    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    print("\n--- outputs/lifestyle.json 概要 ---")
    if lifestyle:
        for field in ["food", "outfit", "lodging", "play"]:
            items = lifestyle.get(field, {}).get("items", [])
            print(f"{field}: {len(items)} 条")
    else:
        print("(空)")

    conflicts = store.read_workspace(project_id, "conflicts.jsonl")
    print("\n--- conflicts.jsonl ---")
    print(conflicts if conflicts else "(无冲突记录)")

    final_guide_path = store.workspace_dir(project_id) / "final_guide.md"
    print(f"\n--- 最终攻略路径 ---\n{final_guide_path}")
    if final_guide_path.exists():
        content = final_guide_path.read_text(encoding="utf-8")
        print(f"(长度: {len(content)} 字符)")
    else:
        print("(未生成!)")


if __name__ == "__main__":
    main()
