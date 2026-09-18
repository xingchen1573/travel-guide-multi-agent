"""定向修补 MVP CLI。

示例：

    .venv-mac/bin/python run_review_mvp.py finding-create \
      --project-id <id> --problem-type unsupported_specificity \
      --target '{"file":"outputs/lifestyle.json"}'

    .venv-mac/bin/python run_review_mvp.py patch-propose \
      --project-id <id> --finding-id <finding> \
      --file outputs/lifestyle.json \
      --operations '[{"op":"replace","path":"/food/items/0/recommendation","value":"..."}]'
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.review import store
from src.review import index as review_index
from src.workspace import store as workspace_store
from src.review.models import ReviewRecommendation


def _json_arg(value: str) -> Any:
    try:
        if value.startswith("@"):
            with open(value[1:], encoding="utf-8") as handle:
                return json.load(handle)
        return json.loads(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise argparse.ArgumentTypeError(f"不是合法JSON或文件: {value}") from exc


def _print(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="攻略定向修补与复核 MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    finding = sub.add_parser("finding-create", help="创建问题单")
    finding.add_argument("--project-id", required=True)
    finding.add_argument("--problem-type", required=True)
    finding.add_argument("--target", required=True, type=_json_arg)
    finding.add_argument("--root-cause", default="content")
    finding.add_argument("--severity", default="medium")
    finding.add_argument("--rerun-cost", default="medium")
    finding.add_argument("--evidence-refs", default="[]", type=_json_arg)
    finding.add_argument("--before", default=None, type=_json_arg)
    finding.add_argument("--notes", default="")
    finding.add_argument("--actor-type", default="system")
    finding.add_argument("--actor-id", default="review_engine")

    propose = sub.add_parser("patch-propose", help="生成patch，不修改源文件")
    propose.add_argument("--project-id", required=True)
    propose.add_argument("--finding-id", required=True)
    propose.add_argument("--file", required=True)
    propose.add_argument("--operations", required=True, type=_json_arg)
    propose.add_argument("--actor-type", default="model")
    propose.add_argument("--actor-id", default="")

    apply = sub.add_parser("patch-apply", help="人工接受并应用patch")
    apply.add_argument("--project-id", required=True)
    apply.add_argument("--patch-id", required=True)
    apply.add_argument("--actor-type", default="human")
    apply.add_argument("--actor-id", default="admin_user")

    reject = sub.add_parser("patch-reject", help="拒绝patch并暂缓问题")
    reject.add_argument("--project-id", required=True)
    reject.add_argument("--patch-id", required=True)
    reject.add_argument("--reason", default="")
    reject.add_argument("--actor-type", default="human")
    reject.add_argument("--actor-id", default="admin_user")

    rollback = sub.add_parser("patch-rollback", help="为已应用patch生成反向patch")
    rollback.add_argument("--project-id", required=True)
    rollback.add_argument("--patch-id", required=True)
    rollback.add_argument("--actor-type", default="human")
    rollback.add_argument("--actor-id", default="admin_user")

    recommendation = sub.add_parser("recommendation-record", help="保存模型复核建议包")
    recommendation.add_argument("--project-id", required=True)
    recommendation.add_argument("--recommendation", required=True, type=_json_arg)

    findings = sub.add_parser("findings", help="列出当前问题单")
    findings.add_argument("--project-id", required=True)

    backfill = sub.add_parser("backfill-findings", help="将旧review_log迁移为当前finding")
    backfill.add_argument("--project-id", required=True)

    candidates = sub.add_parser("optimization-candidates", help="生成并列出系统优化候选")
    candidates.add_argument("--project-id", required=True)
    candidates.add_argument("--threshold", type=int, default=2)

    maintenance_plan = sub.add_parser("maintenance-plan", help="生成复核历史治理 dry-run 计划")
    maintenance_scope = maintenance_plan.add_mutually_exclusive_group(required=True)
    maintenance_scope.add_argument("--project-id")
    maintenance_scope.add_argument("--all-projects", action="store_true")
    maintenance_plan.add_argument("--output", help="可选：将计划写入 JSON 文件")

    maintenance_apply = sub.add_parser("maintenance-apply", help="应用已审核的复核历史治理计划")
    maintenance_apply.add_argument("--plan", required=True, help="maintenance-plan 生成的 JSON 文件")

    rebuild_index = sub.add_parser("rebuild-global-index", help="从 canonical findings 原子重建全局优化索引")
    rebuild_index.add_argument("--threshold", type=int, default=2)

    contract_plan = sub.add_parser("final-guide-contract-plan", help="生成历史最终攻略合同补扫 dry-run 计划")
    contract_scope = contract_plan.add_mutually_exclusive_group(required=True)
    contract_scope.add_argument("--project-id")
    contract_scope.add_argument("--all-projects", action="store_true")
    contract_plan.add_argument("--output", help="可选：将计划写入 JSON 文件")

    contract_apply = sub.add_parser("final-guide-contract-apply", help="应用已审核的历史最终攻略合同补扫计划")
    contract_apply.add_argument("--plan", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "finding-create":
            result = store.create_finding(
                args.project_id,
                problem_type=args.problem_type,
                target=args.target,
                root_cause=args.root_cause,
                severity=args.severity,
                rerun_cost=args.rerun_cost,
                evidence_refs=args.evidence_refs,
                before=args.before,
                notes=args.notes,
                actor_type=args.actor_type,
                actor_id=args.actor_id,
            )
            _print(result)
        elif args.command == "patch-propose":
            _print(
                store.propose_patch(
                    args.project_id,
                    finding_id=args.finding_id,
                    file=args.file,
                    operations=args.operations,
                    actor_type=args.actor_type,
                    actor_id=args.actor_id,
                )
            )
        elif args.command == "patch-apply":
            _print(store.apply_patch(args.project_id, args.patch_id, actor_type=args.actor_type, actor_id=args.actor_id))
        elif args.command == "patch-reject":
            _print(store.reject_patch(args.project_id, args.patch_id, actor_type=args.actor_type, actor_id=args.actor_id, reason=args.reason))
        elif args.command == "patch-rollback":
            _print(store.rollback_patch(args.project_id, args.patch_id, actor_type=args.actor_type, actor_id=args.actor_id))
        elif args.command == "recommendation-record":
            recommendation = ReviewRecommendation.model_validate(args.recommendation)
            store.append_recommendation(args.project_id, recommendation)
            _print(recommendation)
        elif args.command == "findings":
            _print([item.model_dump(mode="json") for item in store.read_findings(args.project_id)])
        elif args.command == "backfill-findings":
            _print([item.model_dump(mode="json") for item in store.backfill_findings_from_review_log(args.project_id)])
        elif args.command == "optimization-candidates":
            _print(store.collect_optimization_candidates(args.project_id, threshold=args.threshold))
        elif args.command == "maintenance-plan":
            project_ids = workspace_store.list_project_ids() if args.all_projects else [args.project_id]
            result = {
                "schema_version": "review-maintenance-batch-v1",
                "plans": [store.plan_review_maintenance(project_id) for project_id in project_ids],
            }
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                store._write_json_atomic(output, result)
            _print(result)
        elif args.command == "maintenance-apply":
            payload = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            plans = payload.get("plans") if payload.get("schema_version") == "review-maintenance-batch-v1" else [payload]
            _print([
                store.apply_review_maintenance_plan(str(plan["project_id"]), plan)
                for plan in plans
            ])
        elif args.command == "rebuild-global-index":
            _print(review_index.rebuild_global_index(threshold=args.threshold))
        elif args.command == "final-guide-contract-plan":
            project_ids = workspace_store.list_project_ids() if args.all_projects else [args.project_id]
            result = store.plan_final_guide_contract_backfill(project_ids)
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                store._write_json_atomic(output, result)
            _print(result)
        elif args.command == "final-guide-contract-apply":
            payload = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            _print(store.apply_final_guide_contract_backfill(payload))
        else:  # pragma: no cover - argparse已保证command合法
            raise ValueError(args.command)
    except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
