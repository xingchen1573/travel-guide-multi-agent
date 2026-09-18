"""系统性评测集运行脚本（docs/ARCHITECTURE.md第17节"系统性评测集设计v1"）。

以真实用户视角跑通完整链路（run_plan生成→html网站→海报→陪伴阶段典型交互），检验最终
产出能否真正指导实际旅游——不是改动前后diff式回归工具，也不是及格线门禁，是用来发现
问题、驱动调优的可用性测试集。

纯手动CLI触发，不接CI/定时（决策8）。不额外加"评测模式"开关屏蔽真实副作用，按真实生产
逻辑跑，会产生真实的LLM/联网搜索/生图调用成本（决策9）——这是有意的取舍：人为关闭副作用
会让"素材空白目的地究竟表现如何"这类核心观测目标本身失真。

场景定义文件在 eval/scenarios/*.json（决策6），每次运行的结果写入
eval/runs/<时间戳>/<场景id>/{checklist.json, judge_scores.json, human_review.json}
+ eval/runs/<时间戳>/summary.json（决策7），human_review.json是留给人工抽查填写的空壳
（决策5），本脚本本身不做人工判断，也不自动写docs/ISSUES.md（决策10）——评测结果只是
线索，是否值得沉淀成文档记录、是否需要动手修复，都是人工看完结果之后再决定的事。

用法：
    PYTHONIOENCODING=utf-8 python run_scenario_eval.py chengdu_3d
    PYTHONIOENCODING=utf-8 python run_scenario_eval.py --all
    PYTHONIOENCODING=utf-8 python run_scenario_eval.py --list
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from src.config import get_chat_model
from src.orchestrator import run_companion, run_edit, run_plan, resume_companion
from src.review import store as review_store
from src.tools.web_search_tools import make_web_search_tools
from src.utils import invoke_with_retry
from src.workspace import store

EVAL_DIR = Path(__file__).parent / "eval"
SCENARIOS_DIR = EVAL_DIR / "scenarios"
RUNS_DIR = EVAL_DIR / "runs"
AGENTS_DIR = Path(__file__).parent / "src" / "agents"


def compute_prompt_fingerprint() -> dict:
    """计算当前所有 agent prompt.md 的内容指纹（SHA-256）。

    每次 eval run 跑完后写入 summary.json，事后能程序化对比 baseline vs 新版 run
    分别是哪份 prompt 产出的（决策11）。按 agent 目录名排序保证跨 run 确定性；
    Python 内建 hash() 对字符串 per-process 加盐，跨 run 不可比，必须用 hashlib。
    """
    per_agent: dict = {}
    chunks: list = []
    for prompt_path in sorted(AGENTS_DIR.glob("*/prompt.md")):
        agent_name = prompt_path.parent.name
        content = prompt_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        per_agent[agent_name] = digest
        # 用 agent 名作前缀分界，避免拼接歧义
        chunks.append(f"{agent_name}\n{content}")
    aggregate = hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()
    return {"prompt_hash": aggregate, "per_agent": per_agent}

# 陪伴阶段各角色在GraphState.last_agent_result里落的agent标记（跟src/orchestrator.py里
# 实际写入的字符串保持同步，见run_companion/companion_lifestyle_query_node/
# companion_pitfall_query_node/realtime_agent_node/guide_agent_node），决策4据此核对
# "是否路由到了预期的agent"。
_COMPANION_RESULT_AGENT = {
    "guide_agent": "guide_agent",
    "realtime_agent": "realtime_agent",
    "lifestyle_agent": "companion_query",
    "pitfall_agent": "companion_pitfall_query",
}


# ---------- 决策6：场景加载 ----------


def load_scenario(scenario_id: str) -> dict:
    path = SCENARIOS_DIR / f"{scenario_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"场景定义文件不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_scenario_ids() -> list[str]:
    if not SCENARIOS_DIR.exists():
        return []
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.json"))


# ---------- 决策3第二类：确定性checklist（产出物是否生成、核验链路是否收敛、陪伴阶段路由是否正确） ----------


def run_checklist(project_id: str, companion_interactions: list[dict]) -> dict:
    """程序能查的不用LLM主观判断。产出物生成走真实生产脚本（懒加载导入，跟
    src/api/jobs.py、src/channels/feishu/service.py同款局部import习惯），失败不阻塞
    其余检查项——生成海报是真实付费调用，失败只降级成一条issue，不中断整个场景评测。
    """
    issues: list[dict] = []
    checks: dict = {}

    final_guide = store.read_workspace(project_id, "final_guide.md")
    checks["final_guide_generated"] = bool(final_guide and str(final_guide).strip())
    if not checks["final_guide_generated"]:
        issues.append({"severity": "blocker", "item": "final_guide", "detail": "final_guide.md为空或不存在"})

    site_path = store.workspace_dir(project_id) / "site.html"
    try:
        from scripts.delivery.run_generate_site import generate_site

        generate_site(project_id)
        checks["site_generated"] = site_path.exists() and site_path.stat().st_size > 0
    except Exception as e:
        checks["site_generated"] = False
        issues.append({"severity": "major", "item": "site", "detail": f"生成html网站失败：{type(e).__name__}: {e}"})

    poster_path = store.workspace_dir(project_id) / "poster.png"
    try:
        from scripts.delivery.run_generate_poster import generate_poster

        generate_poster(project_id, confirm=False)
        checks["poster_generated"] = poster_path.exists() and poster_path.stat().st_size > 0
    except Exception as e:
        checks["poster_generated"] = False
        issues.append({"severity": "minor", "item": "poster", "detail": f"生成海报失败：{type(e).__name__}: {e}"})

    conflicts = store.read_workspace(project_id, "conflicts.jsonl") or []
    unresolved = [c for c in conflicts if c.get("status") == "pending"]
    checks["unresolved_conflicts"] = len(unresolved)
    if unresolved:
        issues.append({"severity": "major", "item": "conflicts", "detail": f"仍有{len(unresolved)}条未解决冲突"})

    state_validation = review_store.validate_project_state(project_id)
    force_finalized_paths = state_validation.get("raw_force_finalized_paths") or []
    checks["force_finalized_count"] = len(force_finalized_paths)
    if force_finalized_paths:
        issues.append(
            {
                "severity": "major",
                "item": "force_finalized",
                "detail": (
                    f"{len(force_finalized_paths)}个规范对象因核验打回耗尽被强制终结"
                    "（可能是真实素材缺口，不一定是bug，需要人工核对）"
                ),
            }
        )

    companion_results = run_companion_interactions(project_id, companion_interactions)
    for result in companion_results:
        if result.get("error"):
            issues.append(
                {
                    "severity": "major",
                    "item": f"companion:{result.get('id')}",
                    "detail": f"陪伴阶段交互抛出异常：{result['error']}",
                }
            )
            continue
        if not result.get("has_answer"):
            issues.append(
                {
                    "severity": "major",
                    "item": f"companion:{result.get('id')}",
                    "detail": "陪伴阶段交互没有产出任何回答",
                }
            )
        if result.get("expected_agent") and not result.get("routed_as_expected"):
            issues.append(
                {
                    "severity": "minor",
                    "item": f"companion:{result.get('id')}",
                    "detail": (
                        f"预期路由到{result.get('expected_agent')}，实际路由结果是"
                        f"{result.get('actual_agent')}（可能是_classify_companion_intent边界case，"
                        f"不一定是bug，需要人工核对回答内容本身是否依然合理）"
                    ),
                }
            )

    return {"checks": checks, "issues": issues, "companion_interactions": companion_results}


def run_companion_interactions(project_id: str, interactions: list[dict]) -> list[dict]:
    """决策4：陪伴阶段每项能力各跑一个典型交互，检查路由是否正确、是否给出回答——这里只做
    结构性检查（有没有触达预期agent、有没有产出回答），内容层面的幻觉/质量判断留给
    run_judge通读最终产出和决策10的人工抽查，单条陪伴问答不重新单独跑一次judge，避免过度设计。
    """
    results: list[dict] = []
    for interaction in interactions:
        entry: dict = {"id": interaction.get("id"), "query": interaction["query"]}
        try:
            via = interaction.get("via", "companion")
            if via == "edit":
                result = run_edit(
                    project_id,
                    entry_target=interaction["entry_target"],
                    edit_request=interaction["query"],
                )
                answer = None
                if interaction.get("entry_target") == "culture_agent_node":
                    culture = store.read_workspace(project_id, "outputs/culture.json") or {}
                    items = culture.get("items") or []
                    answer = items[-1]["answer"] if items else None
                actual_agent = (result.get("last_agent_result") or {}).get("agent")
            else:
                result = run_companion(project_id, interaction["query"])
                if "__interrupt__" in result:
                    # realtime_agent发起了待审批的修改——这是无人值守的评测运行，跟run_plan
                    # 对骨架确认"自动confirm模拟没有真人在场"同款取舍：自动approve让流程走完、
                    # 结果能真正落盘，但如实标注这不是真实用户的决策，供人工回看时留意。
                    entry["auto_approved_interrupt"] = True
                    result = resume_companion(project_id, "approve")
                answer = result.get("companion_answer") or result.get("companion_fallback_answer")
                actual_agent = (result.get("last_agent_result") or {}).get("agent")

            entry["answer"] = answer
            entry["actual_agent"] = actual_agent
            expected_agent = interaction.get("expected_agent")
            if expected_agent:
                entry["expected_agent"] = expected_agent
                expected_marker = _COMPANION_RESULT_AGENT.get(expected_agent, expected_agent)
                entry["routed_as_expected"] = actual_agent == expected_marker
            entry["has_answer"] = bool(answer and str(answer).strip())
            entry["error"] = None
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["has_answer"] = False
        results.append(entry)
    return results


# ---------- 决策3第一类 + 决策5：LLM-as-judge打分（配web_search工具核实可验证事实） ----------


def _build_judge_tools(captured: dict) -> list:
    @tool
    def submit_score(
        completeness_score: int,
        completeness_reason: str,
        consistency_score: int,
        consistency_reason: str,
        faithfulness_score: int,
        faithfulness_reason: str,
        requirement_adherence_score: int,
        requirement_adherence_reason: str,
        readability_score: int,
        readability_reason: str,
        usability_score: int,
        usability_reason: str,
        overall_comment: str,
    ) -> str:
        """提交最终打分结果，六个维度各1-5分+理由。涉及具体时间/价格/开放状态/路况这类
        可验证事实的判断，必须先调用web_search核实，不能凭自己的知识直接打分——否则容易
        出现"两个都不知道真相的模型互相配合"的假象。调用后本轮任务结束。"""
        captured["score"] = {
            "completeness_score": completeness_score,
            "completeness_reason": completeness_reason,
            "consistency_score": consistency_score,
            "consistency_reason": consistency_reason,
            "faithfulness_score": faithfulness_score,
            "faithfulness_reason": faithfulness_reason,
            "requirement_adherence_score": requirement_adherence_score,
            "requirement_adherence_reason": requirement_adherence_reason,
            "readability_score": readability_score,
            "readability_reason": readability_reason,
            "usability_score": usability_score,
            "usability_reason": usability_reason,
            "overall_comment": overall_comment,
        }
        return "打分已提交。"

    return [submit_score]


def _build_judge_prompt(requirements: dict, project_id: str) -> str:
    final_guide = store.read_workspace(project_id, "final_guide.md") or ""
    meta = store.read_workspace(project_id, "final_guide.meta.json")
    route_plan = store.read_workspace(project_id, "route_plan.json")
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")

    return f"""你是一个严格的旅游攻略评审员，代表真实用户视角评估下面这份AI生成的旅游攻略是否
真的可以拿去指导实际旅游。遇到具体时间/价格/开放状态/路况这类可验证事实，必须先调用
web_search核实，不要凭自己的知识直接判断。完成评估后必须调用submit_score提交结果。

用户原始需求：{json.dumps(requirements, ensure_ascii=False)}

结构化数据：
- route_plan: {json.dumps(route_plan, ensure_ascii=False)}
- lifestyle output: {json.dumps(lifestyle, ensure_ascii=False)}
- pitfall output: {json.dumps(pitfall, ensure_ascii=False)}
- final_guide.meta.json: {json.dumps(meta, ensure_ascii=False)}

最终攻略正文：
{final_guide}

请特别注意检查：①用户的饮食禁忌/预算/风格等要求有没有被真正遵守（不要只看有没有提到，
要看推荐内容是否真的符合要求）；②内容有没有被过度压缩简略、按这份攻略是否真的能走下来
（这是新增的"实用可执行度"维度，专门盯这类问题）。请按1-5分制对每个维度打分并给出具体理由。"""


def run_judge(project_id: str, requirements: dict) -> dict:
    captured: dict = {}
    tools = make_web_search_tools(project_id, "eval_judge") + _build_judge_tools(captured)
    agent = create_react_agent(get_chat_model(temperature=0), tools)

    prompt = _build_judge_prompt(requirements, project_id)
    messages: list = [{"role": "user", "content": prompt}]
    result = invoke_with_retry(agent, messages)
    if "score" not in captured:
        retry_note = (
            "你刚才的回复没有调用submit_score工具提交打分结果，只有文字总结是不够的。"
            "请现在立刻调用submit_score提交。"
        )
        messages = result["messages"] + [{"role": "user", "content": retry_note}]
        invoke_with_retry(agent, messages)

    return captured.get("score") or {"error": "judge未能提交打分结果"}


# ---------- 决策7：结果落盘 + 决策11：单场景/批量运行入口 ----------


def run_scenario(scenario_id: str, run_timestamp: str) -> dict:
    """跑单个场景的完整流程，返回结果summary。

    审计计时（2026-08-11新增）：分别记录run_plan/checklist+陪伴阶段/judge三个阶段各自的
    真实耗时（秒），以及整体started_at/finished_at（ISO时间戳，UTC）——之前只能靠人工
    对比project目录时间戳跟summary.json的mtime来估算总耗时（如2026-08-11那次cunai渠道
    复测靠这种方式估出约2小时51分钟），既麻烦又不精确（mtime只反映summary.json最后
    一次写入时刻，不是这次运行本身的起止点）。分阶段计时能定位"这次跑得慢主要慢在
    哪个阶段"（生成 vs 陪伴阶段 vs judge打分），不是只有一个笼统的总耗时数字。
    """
    scenario = load_scenario(scenario_id)
    project_id = f"eval-{scenario_id}-{run_timestamp}"
    run_dir = RUNS_DIR / run_timestamp / scenario_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    scenario_t0 = time.monotonic()

    print(f"[{scenario_id}] 开始跑run_plan（project_id={project_id}）...")
    plan_t0 = time.monotonic()
    try:
        plan_result = run_plan(project_id, scenario["requirements"])
    except Exception as e:
        plan_elapsed = time.monotonic() - plan_t0
        error_summary = {
            "scenario_id": scenario_id,
            "project_id": project_id,
            "fatal_error": f"run_plan失败：{type(e).__name__}: {e}",
            "timing": {
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "run_plan_seconds": round(plan_elapsed, 1),
                "total_seconds": round(time.monotonic() - scenario_t0, 1),
            },
        }
        (run_dir / "checklist.json").write_text(
            json.dumps(error_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[{scenario_id}] run_plan失败，跳过后续步骤：{e}")
        return error_summary
    plan_elapsed = time.monotonic() - plan_t0
    if isinstance(plan_result, dict) and "__interrupt__" in plan_result:
        payload = getattr(plan_result["__interrupt__"][0], "value", plan_result["__interrupt__"][0])
        proposal = payload.get("route_change_proposal") if isinstance(payload, dict) else None
        action_summary = {
            "scenario_id": scenario_id,
            "project_id": project_id,
            "status": "action_required",
            "proposal_id": (proposal or {}).get("proposal_id"),
            "proposal_reason": (proposal or {}).get("reason"),
            "proposal_evidence": (proposal or {}).get("evidence"),
            "decision_options": ["keep", "confirm", "revise:<意见>"] if proposal else [],
            "timing": {
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "run_plan_seconds": round(plan_elapsed, 1),
                "total_seconds": round(time.monotonic() - scenario_t0, 1),
            },
        }
        (run_dir / "checklist.json").write_text(
            json.dumps(action_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[{scenario_id}] 需要用户路线决策，已停止后续checklist/陪伴/judge。")
        return action_summary

    print(f"[{scenario_id}] 跑确定性checklist+陪伴阶段典型交互...")
    checklist_t0 = time.monotonic()
    checklist = run_checklist(project_id, scenario.get("companion_interactions", []))
    checklist_elapsed = time.monotonic() - checklist_t0
    (run_dir / "checklist.json").write_text(json.dumps(checklist, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[{scenario_id}] 跑LLM-as-judge打分...")
    judge_t0 = time.monotonic()
    try:
        judge_score = run_judge(project_id, scenario["requirements"])
    except Exception as e:
        judge_score = {"error": f"{type(e).__name__}: {e}"}
    judge_elapsed = time.monotonic() - judge_t0
    (run_dir / "judge_scores.json").write_text(json.dumps(judge_score, ensure_ascii=False, indent=2), encoding="utf-8")

    human_review_path = run_dir / "human_review.json"
    if not human_review_path.exists():
        human_review_path.write_text(
            json.dumps({"reviewed": False, "notes": ""}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    finished_at = datetime.now(timezone.utc).isoformat()
    total_elapsed = time.monotonic() - scenario_t0

    return {
        "scenario_id": scenario_id,
        "destination_type": scenario.get("destination_type"),
        "project_id": project_id,
        "checks": checklist["checks"],
        "issues": checklist["issues"],
        "judge_scores": judge_score,
        "timing": {
            "started_at": started_at,
            "finished_at": finished_at,
            "run_plan_seconds": round(plan_elapsed, 1),
            "checklist_and_companion_seconds": round(checklist_elapsed, 1),
            "judge_seconds": round(judge_elapsed, 1),
            "total_seconds": round(total_elapsed, 1),
        },
    }


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--list":
        for sid in list_scenario_ids():
            print(sid)
        return
    scenario_ids = list_scenario_ids() if args[0] == "--all" else args

    run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prompt_fingerprint = compute_prompt_fingerprint()
    summaries = []
    for scenario_id in scenario_ids:
        try:
            summaries.append(run_scenario(scenario_id, run_timestamp))
        except Exception as e:
            traceback.print_exc()
            summaries.append({"scenario_id": scenario_id, "fatal_error": f"{type(e).__name__}: {e}"})

    summary_path = RUNS_DIR / run_timestamp / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"prompt_fingerprint": prompt_fingerprint, "runs": summaries}
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n本次批量运行汇总已写入 {summary_path}")
    print(f"prompt 指纹：{prompt_fingerprint['prompt_hash'][:12]}…（10 个 agent）")


if __name__ == "__main__":
    main()
