#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT_ENV = "TRAVEL_GUIDE_REPO_ROOT"
PYTHON_ENV = "TRAVEL_GUIDE_PYTHON"
RUNTIME_ROOT_ENV = "TRAVEL_GUIDE_RUNTIME_ROOT"
_BOOTSTRAP_ENV = "TRAVEL_GUIDE_BOOTSTRAPPED"
_INSTALL_CONFIG_NAME = ".travel-guide-runtime.json"
_INSTALL_CONFIG_VERSION = "travel-guide-skill-runtime-v1"

_RUNTIME_MARKERS = (
    Path("src/codex_skill/bridge.py"),
    Path("src/codex_skill/preflight.py"),
    Path("src/config.py"),
    Path("travel_guide_kernel/__init__.py"),
)
_SKILL_BUNDLE_PATHS = tuple(
    Path(path)
    for path in (
        "SKILL.md",
        "agents/openai.yaml",
        "references/workflow.md",
        "references/shared-contract.md",
        "references/evidence-policy.md",
        "references/roles/route.md",
        "references/roles/lifestyle.md",
        "references/roles/pitfall.md",
        "references/roles/integration.md",
        "references/roles/poi-detail.md",
        "scripts/install_skill.py",
        "scripts/travel_guide_bridge.py",
    )
)
_ROLE_CONTRACTS = tuple(
    Path(path)
    for path in (
        "src/agents/route_agent/prompt.md",
        "src/agents/lifestyle_agent/prompt.md",
        "src/agents/pitfall_agent/prompt.md",
        "src/agents/integration_agent/prompt.md",
        "src/agents/poi_detail_agent/prompt.md",
        ".codex/agents/travel-route.toml",
        ".codex/agents/travel-lifestyle.toml",
        ".codex/agents/travel-pitfall.toml",
        ".codex/agents/travel-integration.toml",
        ".codex/agents/travel-poi-detail.toml",
    )
)
_BRIDGE_API_SYMBOLS = (
    "accept_route_risks",
    "amap_driving_distance",
    "amap_resolve_route_entity",
    "append_project_evidence",
    "append_trace",
    "build_resume_plan",
    "freeze_material_baseline",
    "finalize_integration",
    "finalize_poi_details",
    "finalize_route_only",
    "integration_delivery_inputs",
    "material_context",
    "prepare_fixed_route",
    "prepare_route_only",
    "project_evidence_context",
    "reconcile_zero_claim_validation_from_trace",
    "research_status",
    "supplement_material_gap",
    "validate_stage",
    "web_search",
    "write_route_entities_payload",
    "write_route_execution_payload",
    "write_route_facts_payload",
    "write_route_plan_payload",
    "write_stage_payload",
)
_PREFLIGHT_API_SYMBOLS = ("run_fixed_route_preflight", "run_route_only_preflight")


class BootstrapError(RuntimeError):
    pass


def _normalise_path(value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve()


def _missing_paths(root: Path, paths: Sequence[Path]) -> list[str]:
    return [str(path) for path in paths if not (root / path).is_file()]


def _load_install_config(skill_root: Path) -> dict:
    path = skill_root / _INSTALL_CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Skill运行配置不可读: {path}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"Skill运行配置必须是JSON对象: {path}")
    if value.get("schema_version") != _INSTALL_CONFIG_VERSION:
        raise BootstrapError(f"Skill运行配置版本不受支持: {path}")
    return value


def _configured_path(value: object, *, base: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _normalise_path(text, base=base)


def _find_runtime_root(start: Path) -> Path | None:
    candidate = start.resolve()
    for root in (candidate, *candidate.parents):
        if not _missing_paths(root, _RUNTIME_MARKERS):
            return root
    return None


def _resolve_repo_root(
    explicit: str | Path | None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    script_path: Path | None = None,
    install_config: Mapping[str, object] | None = None,
) -> tuple[Path | None, str]:
    if environ is None:
        environ = os.environ
    if explicit:
        return _normalise_path(explicit), "cli"
    if environ.get(REPO_ROOT_ENV):
        return _normalise_path(environ[REPO_ROOT_ENV]), "environment"

    skill_root = (script_path or Path(__file__)).resolve().parents[1]
    installed_root = _configured_path(
        (install_config or {}).get("code_root"),
        base=skill_root,
    )
    if installed_root is not None:
        return installed_root, "installed_bundle"

    current_root = _find_runtime_root(cwd or Path.cwd())
    if current_root is not None:
        return current_root, "working_directory"

    skill_root = _find_runtime_root((script_path or Path(__file__)).resolve().parent)
    if skill_root is not None:
        return skill_root, "embedded_skill"
    return None, "missing"


def _resolve_python_executable(
    explicit: str | Path | None,
    *,
    repo_root: Path | None,
    environ: Mapping[str, str] | None = None,
    current_executable: str | Path | None = None,
    installed_python: str | Path | None = None,
    skill_root: Path | None = None,
) -> tuple[Path, str]:
    if environ is None:
        environ = os.environ
    configured = explicit or environ.get(PYTHON_ENV) or installed_python
    if configured:
        source = (
            "cli"
            if explicit
            else "environment"
            if environ.get(PYTHON_ENV)
            else "installed_bundle"
        )
        raw = str(configured)
        configured_path = Path(raw).expanduser()
        command = (
            shutil.which(raw)
            if not configured_path.is_absolute() and configured_path.parent == Path(".")
            else None
        )
        if command:
            return Path(command).absolute(), source
        path = configured_path
        if not path.is_absolute():
            base = skill_root if source == "installed_bundle" else repo_root
            if base is not None:
                path = base / path
        return path.absolute(), source
    return Path(current_executable or sys.executable).absolute(), "current_process"


def _same_executable(left: Path, right: Path) -> bool:
    # Keep virtual-environment launchers distinct even when both are symlinks to
    # the same base Python binary; invoking through the venv path sets sys.prefix.
    return os.path.normcase(str(left.expanduser().absolute())) == os.path.normcase(
        str(right.expanduser().absolute())
    )


def _reexec_with_configured_python(
    python_executable: Path,
    *,
    repo_root: Path,
    argv: Sequence[str],
    environ: Mapping[str, str] | None = None,
    script_path: Path | None = None,
    export_repo_root: bool = True,
) -> None:
    current = Path(sys.executable).expanduser().absolute()
    if _same_executable(python_executable, current):
        return
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise BootstrapError(f"配置的Python不可执行: {python_executable}")

    source_env = dict(os.environ if environ is None else environ)
    bootstrapped_target = source_env.get(_BOOTSTRAP_ENV)
    if bootstrapped_target and _same_executable(
        Path(bootstrapped_target), python_executable
    ):
        return
    if bootstrapped_target:
        raise BootstrapError(
            "切换到配置的宿主Python后解释器仍不匹配；请检查TRAVEL_GUIDE_PYTHON"
        )
    if export_repo_root:
        source_env[REPO_ROOT_ENV] = str(repo_root)
    source_env[_BOOTSTRAP_ENV] = str(python_executable)
    script = (script_path or Path(__file__)).resolve()
    os.execve(
        str(python_executable),
        [str(python_executable), str(script), *argv],
        source_env,
    )


def _check(name: str, passed: bool, **details) -> dict:
    return {"name": name, "status": "passed" if passed else "failed", **details}


def _doctor_report(
    *,
    skill_root: Path,
    repo_root: Path | None,
    repo_root_source: str,
    python_executable: Path,
    python_source: str,
) -> dict:
    checks: list[dict] = []
    checks.append(
        _check(
            "platform",
            os.name == "posix",
            detail="V1支持macOS/Linux" if os.name == "posix" else "V1暂不支持Windows",
        )
    )

    skill_missing = _missing_paths(skill_root, _SKILL_BUNDLE_PATHS)
    checks.append(
        _check(
            "skill_bundle",
            not skill_missing,
            path=str(skill_root),
            missing=skill_missing,
        )
    )

    runtime_missing = (
        _missing_paths(repo_root, _RUNTIME_MARKERS) if repo_root is not None else []
    )
    checks.append(
        _check(
            "host_repository",
            repo_root is not None and not runtime_missing,
            path=str(repo_root) if repo_root is not None else None,
            source=repo_root_source,
            missing=runtime_missing,
        )
    )

    python_ready = python_executable.is_file() and os.access(python_executable, os.X_OK)
    checks.append(
        _check(
            "python_executable",
            python_ready,
            path=str(python_executable),
            source=python_source,
        )
    )

    role_missing = (
        _missing_paths(repo_root, _ROLE_CONTRACTS) if repo_root is not None else []
    )
    checks.append(
        _check(
            "role_contracts",
            repo_root is not None and not role_missing,
            missing=role_missing,
            detail=(
                "安装包已包含角色契约；自主路线可由当前Codex按Route参考执行。"
                if repo_root_source == "installed_bundle"
                else "文件存在不代表当前Codex任务已加载自定义Agent；请从宿主仓库打开任务。"
            ),
        )
    )

    import_check = _check(
        "runtime_import",
        False,
        detail="宿主仓库或Python未就绪，未执行导入检查。",
    )
    if repo_root is not None and not runtime_missing and python_ready:
        probe = (
            "import json, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import travel_guide_kernel; "
            "from src.codex_skill import bridge, preflight; "
            f"bridge_required={_BRIDGE_API_SYMBOLS!r}; "
            f"preflight_required={_PREFLIGHT_API_SYMBOLS!r}; "
            "missing=[name for name in bridge_required if not hasattr(bridge, name)]; "
            "missing += [name for name in preflight_required if not hasattr(preflight, name)]; "
            "assert not missing, 'missing runtime API: ' + ', '.join(missing); "
            "print(json.dumps({'bridge': bridge.__file__, 'preflight': preflight.__file__, "
            "'kernel': travel_guide_kernel.__file__}))"
        )
        try:
            completed = subprocess.run(
                [str(python_executable), "-I", "-c", probe, str(repo_root)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            detail = (completed.stderr or completed.stdout).strip()
            detail_line = detail.splitlines()[-1] if detail else "宿主运行时可导入。"
            module_paths = {}
            if completed.returncode == 0 and completed.stdout.strip():
                try:
                    module_paths = json.loads(completed.stdout.splitlines()[-1])
                except json.JSONDecodeError:
                    module_paths = {}
            import_check = _check(
                "runtime_import",
                completed.returncode == 0,
                detail=detail_line[-1000:],
                modules=module_paths,
            )
        except subprocess.TimeoutExpired:
            import_check = _check(
                "runtime_import",
                False,
                detail="宿主运行时导入检查30秒超时。",
            )
    checks.append(import_check)

    ready = all(item["status"] == "passed" for item in checks)
    return {
        "schema_version": "travel-guide-doctor-v1",
        "status": "ready" if ready else "blocked",
        "repo_root": str(repo_root) if repo_root is not None else None,
        "python_executable": str(python_executable),
        "checks": checks,
        "next_step": (
            "运行项目级preflight；doctor不检查具体Project、素材或在线凭证。"
            if ready
            else "修复failed检查后重新运行doctor。"
        ),
    }


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    skill_root = Path(__file__).resolve().parents[1]
    try:
        install_config = _load_install_config(skill_root)
    except BootstrapError as exc:
        print(f"travel-guide启动失败: {exc}", file=sys.stderr)
        return 2

    configured_state_root = _configured_path(
        install_config.get("state_root"),
        base=skill_root,
    )
    if configured_state_root is not None and not os.environ.get(RUNTIME_ROOT_ENV):
        os.environ[RUNTIME_ROOT_ENV] = str(configured_state_root)

    parser = argparse.ArgumentParser(
        description="Bridge Codex travel-guide roles to the existing Workspace kernel."
    )
    parser.add_argument(
        "--repo-root",
        help=f"宿主旅游仓库根目录；也可设置{REPO_ROOT_ENV}",
    )
    parser.add_argument(
        "--python",
        dest="python_executable",
        help=f"宿主Python解释器；也可设置{PYTHON_ENV}，相对路径按宿主仓库解析",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")

    prepare = subparsers.add_parser("prepare-fixed-route")
    prepare.add_argument("source_project_id")
    prepare.add_argument("target_project_id")
    prepare.add_argument("--model")
    prepare.add_argument("--comparison-grade", choices=["A", "B", "C"], default="C")
    prepare.add_argument("--require-comparison-sidecars", action="store_true")

    freeze_baseline = subparsers.add_parser("freeze-material-baseline")
    freeze_baseline.add_argument("project_id")

    prepare_route = subparsers.add_parser("prepare-route-only")
    prepare_route.add_argument("source_project_id")
    prepare_route.add_argument("target_project_id")

    preflight = subparsers.add_parser("preflight-fixed-route")
    preflight.add_argument("source_project_id")
    preflight.add_argument("target_project_id")
    preflight.add_argument("--live", action="store_true")
    preflight.add_argument("--allow-existing-target", action="store_true")
    preflight.add_argument("--require-comparison-sidecars", action="store_true")
    preflight.add_argument("--require-embedding", action="store_true")

    route_preflight = subparsers.add_parser("preflight-route-only")
    route_preflight.add_argument("source_project_id")
    route_preflight.add_argument("target_project_id")
    route_preflight.add_argument("--live", action="store_true")
    route_preflight.add_argument("--allow-existing-target", action="store_true")

    material = subparsers.add_parser("material-context")
    material.add_argument("project_id")
    material.add_argument("--stage", required=True, choices=["route", "lifestyle", "pitfall", "poi_detail"])
    material.add_argument("--query", required=True)
    material.add_argument("--day", type=int)
    material.add_argument("--poi-name")
    material.add_argument("--field")
    material.add_argument("--top-k", type=int, default=8)

    amap_entity = subparsers.add_parser("amap-resolve-route-entity")
    amap_entity.add_argument("project_id")
    amap_entity.add_argument("--poi-name", required=True)
    amap_entity.add_argument("--city-hint", required=True)

    amap_distance = subparsers.add_parser("amap-driving-distance")
    amap_distance.add_argument("project_id")
    amap_distance.add_argument("--poi-a", required=True)
    amap_distance.add_argument("--poi-b", required=True)
    amap_distance.add_argument("--city", default="")
    amap_distance.add_argument("--day", type=int)

    project_evidence = subparsers.add_parser("project-evidence-context")
    project_evidence.add_argument("project_id")
    project_evidence.add_argument(
        "--stage", required=True, choices=["route", "lifestyle", "poi_detail"]
    )
    project_evidence.add_argument("--query", required=True)
    project_evidence.add_argument("--day", type=int)
    project_evidence.add_argument("--poi-name")
    project_evidence.add_argument("--field")
    project_evidence.add_argument("--top-k", type=int, default=5)

    web = subparsers.add_parser("web-search")
    web.add_argument("project_id")
    web.add_argument("--stage", required=True, choices=["route", "lifestyle", "poi_detail"])
    web.add_argument("--query", required=True)
    web.add_argument("--day", type=int)
    web.add_argument("--poi-name")
    web.add_argument("--field")

    evidence = subparsers.add_parser("append-project-evidence")
    evidence.add_argument("project_id")
    evidence.add_argument("--stage", required=True, choices=["route", "lifestyle", "poi_detail"])
    evidence.add_argument("--summary", required=True)
    evidence.add_argument("--source-url", required=True)
    evidence.add_argument("--day", type=int)
    evidence.add_argument("--poi-name")
    evidence.add_argument("--field")

    research = subparsers.add_parser("research-status")
    research.add_argument("project_id")

    supplement = subparsers.add_parser("supplement-material-gap")
    supplement.add_argument("project_id")
    supplement.add_argument("--stage", required=True, choices=["route", "lifestyle", "poi_detail"])
    supplement.add_argument("--day", type=int)
    supplement.add_argument("--poi-name")
    supplement.add_argument("--field")
    supplement.add_argument("--expected-route-sha", required=True)
    supplement.add_argument("--expected-artifact-sha", required=True)

    resume = subparsers.add_parser("resume-plan")
    resume.add_argument("project_id")
    resume.add_argument("--expected-route-sha")

    write = subparsers.add_parser("write")
    write.add_argument("project_id")
    write.add_argument("stage", choices=["lifestyle", "pitfall", "integration", "poi_detail"])
    write.add_argument("--input", required=True, type=Path)
    write.add_argument("--day", type=int)
    write.add_argument("--poi-name")

    route_facts = subparsers.add_parser("write-route-facts")
    route_facts.add_argument("project_id")
    route_facts.add_argument("--input", required=True, type=Path)
    route_facts.add_argument("--day", required=True, type=int)
    route_facts.add_argument("--expected-route-sha", required=True)

    route_plan = subparsers.add_parser("write-route-plan")
    route_plan.add_argument("project_id")
    route_plan.add_argument("--input", required=True, type=Path)

    route_entities = subparsers.add_parser("write-route-entities")
    route_entities.add_argument("project_id")
    route_entities.add_argument("--input", required=True, type=Path)

    route_execution = subparsers.add_parser("write-route-execution")
    route_execution.add_argument("project_id")
    route_execution.add_argument("--input", required=True, type=Path)

    route_finish = subparsers.add_parser("finalize-route-only")
    route_finish.add_argument("project_id")

    accept_risks = subparsers.add_parser("accept-route-risks")
    accept_risks.add_argument("project_id")
    accept_risks.add_argument("--expected-route-sha", required=True)
    accept_risks.add_argument("--note", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("project_id")
    validate.add_argument(
        "stage",
        choices=["route", "lifestyle", "pitfall", "integration", "poi_detail", "final"],
    )
    validate.add_argument("--live", action="store_true")
    validate.add_argument("--day", type=int)
    validate.add_argument("--poi-name")
    validate.add_argument(
        "--route-check",
        dest="route_checks",
        choices=["structure", "citation", "map", "feasibility"],
        action="append",
        help="Route only: run selected checker(s); repeat to compose a subset.",
    )
    validate.add_argument(
        "--check-id",
        dest="check_ids",
        action="append",
        help="Route live retry only: recheck one unresolved route feasibility check ID; repeat as needed.",
    )
    validate.add_argument("--expected-route-sha")

    integrate = subparsers.add_parser("finalize-integration")
    integrate.add_argument("project_id")

    delivery_inputs = subparsers.add_parser("integration-inputs")
    delivery_inputs.add_argument("project_id")

    poi = subparsers.add_parser("finalize-poi")
    poi.add_argument("project_id")
    poi.add_argument("--results", required=True, type=Path)

    trace = subparsers.add_parser("trace")
    trace.add_argument("project_id")
    trace.add_argument("--event", required=True, type=Path)

    reconcile = subparsers.add_parser("reconcile-zero-claim-validation")
    reconcile.add_argument("project_id")
    reconcile.add_argument("stage", choices=["pitfall"])

    args = parser.parse_args(raw_argv)
    repo_root, repo_root_source = _resolve_repo_root(
        args.repo_root,
        script_path=Path(__file__),
        install_config=install_config,
    )
    python_executable, python_source = _resolve_python_executable(
        args.python_executable,
        repo_root=repo_root,
        installed_python=install_config.get("python_executable"),
        skill_root=skill_root,
    )

    if args.command == "doctor":
        report = _doctor_report(
            skill_root=skill_root,
            repo_root=repo_root,
            repo_root_source=repo_root_source,
            python_executable=python_executable,
            python_source=python_source,
        )
        report["runtime_root"] = os.environ.get(RUNTIME_ROOT_ENV)
        _print(report)
        return 0 if report["status"] == "ready" else 1

    if repo_root is None:
        print(
            f"travel-guide启动失败: 未找到宿主仓库；请设置{REPO_ROOT_ENV}或--repo-root",
            file=sys.stderr,
        )
        return 2
    runtime_missing = _missing_paths(repo_root, _RUNTIME_MARKERS)
    if runtime_missing:
        print(
            "travel-guide启动失败: 宿主仓库缺少运行时文件: "
            + ", ".join(runtime_missing),
            file=sys.stderr,
        )
        return 2
    try:
        _reexec_with_configured_python(
            python_executable,
            repo_root=repo_root,
            argv=raw_argv,
            export_repo_root=repo_root_source != "installed_bundle",
        )
    except BootstrapError as exc:
        print(f"travel-guide启动失败: {exc}", file=sys.stderr)
        return 2

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from src.codex_skill.bridge import (
            accept_route_risks,
            amap_driving_distance,
            amap_resolve_route_entity,
            append_project_evidence,
            append_trace,
            build_resume_plan,
            freeze_material_baseline,
            finalize_integration,
            finalize_poi_details,
            finalize_route_only,
            integration_delivery_inputs,
            material_context,
            prepare_fixed_route,
            prepare_route_only,
            project_evidence_context,
            reconcile_zero_claim_validation_from_trace,
            research_status,
            supplement_material_gap,
            validate_stage,
            web_search,
            write_route_entities_payload,
            write_route_execution_payload,
            write_route_facts_payload,
            write_route_plan_payload,
            write_stage_payload,
        )
        from src.codex_skill.preflight import (
            run_fixed_route_preflight,
            run_route_only_preflight,
        )
    except Exception as exc:
        print(
            "travel-guide启动失败: 宿主运行时导入失败；请先运行doctor。"
            f" ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 2

    if args.command == "prepare-fixed-route":
        result = prepare_fixed_route(
            args.source_project_id,
            args.target_project_id,
            model=args.model,
            comparison_grade=args.comparison_grade,
            require_comparison_sidecars=args.require_comparison_sidecars,
        )
    elif args.command == "freeze-material-baseline":
        result = freeze_material_baseline(args.project_id)
    elif args.command == "prepare-route-only":
        result = prepare_route_only(args.source_project_id, args.target_project_id)
    elif args.command == "preflight-fixed-route":
        result = run_fixed_route_preflight(
            args.source_project_id,
            args.target_project_id,
            live=args.live,
            allow_existing_target=args.allow_existing_target,
            require_comparison_sidecars=args.require_comparison_sidecars,
            require_embedding=args.require_embedding,
        )
    elif args.command == "preflight-route-only":
        result = run_route_only_preflight(
            args.source_project_id,
            args.target_project_id,
            live=args.live,
            allow_existing_target=args.allow_existing_target,
        )
    elif args.command == "material-context":
        result = material_context(
            args.project_id,
            stage=args.stage,
            query=args.query,
            day=args.day,
            poi_name=args.poi_name,
            field=args.field,
            top_k=args.top_k,
        )
    elif args.command == "amap-resolve-route-entity":
        result = amap_resolve_route_entity(
            args.project_id,
            poi_name=args.poi_name,
            city_hint=args.city_hint,
        )
    elif args.command == "amap-driving-distance":
        result = amap_driving_distance(
            args.project_id,
            poi_a=args.poi_a,
            poi_b=args.poi_b,
            city=args.city,
            day=args.day,
        )
    elif args.command == "web-search":
        result = web_search(
            args.project_id,
            stage=args.stage,
            query=args.query,
            day=args.day,
            poi_name=args.poi_name,
            field=args.field,
        )
    elif args.command == "project-evidence-context":
        result = project_evidence_context(
            args.project_id,
            stage=args.stage,
            query=args.query,
            day=args.day,
            poi_name=args.poi_name,
            field=args.field,
            top_k=args.top_k,
        )
    elif args.command == "append-project-evidence":
        result = append_project_evidence(
            args.project_id,
            stage=args.stage,
            summary=args.summary,
            source_url=args.source_url,
            day=args.day,
            poi_name=args.poi_name,
            field=args.field,
        )
    elif args.command == "research-status":
        result = research_status(args.project_id)
    elif args.command == "supplement-material-gap":
        result = supplement_material_gap(
            args.project_id,
            stage=args.stage,
            day=args.day,
            poi_name=args.poi_name,
            field=args.field,
            expected_route_structure_sha256=args.expected_route_sha,
            expected_artifact_sha256=args.expected_artifact_sha,
        )
    elif args.command == "resume-plan":
        result = build_resume_plan(args.project_id, expected_route_sha=args.expected_route_sha)
    elif args.command == "write":
        result = write_stage_payload(
            args.project_id,
            args.stage,
            _read_json(args.input),
            day=args.day,
            poi_name=args.poi_name,
        )
    elif args.command == "write-route-facts":
        result = write_route_facts_payload(
            args.project_id,
            _read_json(args.input),
            day=args.day,
            expected_route_structure_sha256=args.expected_route_sha,
        )
    elif args.command == "write-route-plan":
        result = write_route_plan_payload(args.project_id, _read_json(args.input))
    elif args.command == "write-route-entities":
        result = write_route_entities_payload(args.project_id, _read_json(args.input))
    elif args.command == "write-route-execution":
        result = write_route_execution_payload(args.project_id, _read_json(args.input))
    elif args.command == "finalize-route-only":
        result = finalize_route_only(args.project_id)
    elif args.command == "accept-route-risks":
        result = accept_route_risks(
            args.project_id,
            expected_route_structure_sha256=args.expected_route_sha,
            note=args.note,
        )
    elif args.command == "validate":
        result = validate_stage(
            args.project_id,
            args.stage,
            live=args.live,
            day=args.day,
            poi_name=args.poi_name,
            route_check_ids=args.check_ids,
            route_checks=args.route_checks,
            expected_route_structure_sha256=args.expected_route_sha,
        )
    elif args.command == "finalize-integration":
        result = finalize_integration(args.project_id)
    elif args.command == "integration-inputs":
        result = integration_delivery_inputs(args.project_id)
    elif args.command == "finalize-poi":
        raw = _read_json(args.results)
        results = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            raise ValueError("POI results必须是数组或包含results数组的JSON对象")
        result = finalize_poi_details(args.project_id, results)
    elif args.command == "trace":
        result = append_trace(args.project_id, _read_json(args.event))
    elif args.command == "reconcile-zero-claim-validation":
        result = reconcile_zero_claim_validation_from_trace(args.project_id, args.stage)
    else:
        parser.error(f"unknown command: {args.command}")
        return 2

    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
