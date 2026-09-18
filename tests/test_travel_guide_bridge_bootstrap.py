from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SKILL_ROOT = (
    ROOT
    / ".agents"
    / "skills"
    / "travel-guide-codex-orchestrator"
)
SCRIPT = SKILL_ROOT / "scripts" / "travel_guide_bridge.py"
INSTALLER = SKILL_ROOT / "scripts" / "install_skill.py"


def _load_cli(path: Path = SCRIPT):
    spec = importlib.util.spec_from_file_location("travel_guide_bridge_bootstrap_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "travel_guide_skill_installer_test", INSTALLER
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _create_minimal_install_source(root: Path) -> None:
    for relative in (
        ".agents/skills/travel-guide-codex-orchestrator/SKILL.md",
        ".agents/skills/travel-guide-codex-orchestrator/scripts/travel_guide_bridge.py",
        "src/codex_skill/bridge.py",
        "src/codex_skill/preflight.py",
        "src/config.py",
        "travel_guide_kernel/store.py",
        ".codex/agents/travel-route.toml",
        "pyproject.toml",
        "requirements.txt",
        ".env.example",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture: {relative}\n", encoding="utf-8")


def test_installed_script_import_does_not_require_runtime_beside_it(tmp_path):
    installed = tmp_path / "skills" / "travel-guide-codex-orchestrator" / "scripts"
    installed.mkdir(parents=True)
    copied_script = installed / "travel_guide_bridge.py"
    shutil.copy2(SCRIPT, copied_script)

    module = _load_cli(copied_script)

    assert module.REPO_ROOT_ENV == "TRAVEL_GUIDE_REPO_ROOT"


def test_runtime_copy_excludes_sensitive_files(tmp_path):
    module = _load_installer()
    source = tmp_path / "source"
    _create_minimal_install_source(source)
    (source / "src/.env").write_text("SECRET=one\n", encoding="utf-8")
    (source / "src/.env.local").write_text("SECRET=two\n", encoding="utf-8")
    (source / "src/credentials.json").write_text("{}\n", encoding="utf-8")
    (source / "travel_guide_kernel/private.pem").write_text(
        "private\n", encoding="utf-8"
    )

    destination = tmp_path / "runtime"
    module._copy_runtime(source, destination)

    assert (destination / "src/codex_skill/bridge.py").is_file()
    assert (destination / ".env.example").is_file()
    assert not (destination / "src/.env").exists()
    assert not (destination / "src/.env.local").exists()
    assert not (destination / "src/credentials.json").exists()
    assert not (destination / "travel_guide_kernel/private.pem").exists()


def test_installer_rejects_source_and_state_path_overlap(tmp_path):
    module = _load_installer()
    source = tmp_path / "source"
    _create_minimal_install_source(source)
    python = Path(sys.executable)

    with pytest.raises(module.InstallError, match="canonical Skill source"):
        module.install(
            source_root=source,
            codex_home=source / ".agents",
            runtime_python=python,
            state_root=tmp_path / "state",
            replace=True,
            install_dependencies=False,
        )
    assert (source / module.SKILL_RELATIVE_PATH / "SKILL.md").is_file()

    codex_home = tmp_path / "codex-home"
    destination = codex_home / "skills" / module.SKILL_NAME
    with pytest.raises(module.InstallError, match="state root overlaps"):
        module.install(
            source_root=source,
            codex_home=codex_home,
            runtime_python=python,
            state_root=destination / "state",
            replace=False,
            install_dependencies=False,
        )
    assert not destination.exists()

    codex_home = tmp_path / "runtime-codex-home"
    destination = codex_home / "skills" / module.SKILL_NAME
    installed_python = destination / "venv/bin/python"
    installed_python.parent.mkdir(parents=True)
    installed_python.write_text("#!/bin/sh\n", encoding="utf-8")
    installed_python.chmod(0o755)
    with pytest.raises(module.InstallError, match="runtime Python overlaps"):
        module.install(
            source_root=source,
            codex_home=codex_home,
            runtime_python=installed_python,
            state_root=tmp_path / "other-state",
            replace=True,
            install_dependencies=False,
        )
    assert installed_python.is_file()


def test_cli_repo_root_overrides_environment(tmp_path):
    module = _load_cli()
    environment_root = tmp_path / "environment"

    resolved, source = module._resolve_repo_root(
        ROOT,
        environ={module.REPO_ROOT_ENV: str(environment_root)},
        cwd=tmp_path,
        script_path=tmp_path / "travel_guide_bridge.py",
    )

    assert resolved == ROOT.resolve()
    assert source == "cli"


def test_relative_python_is_resolved_from_host_repository(tmp_path):
    module = _load_cli()

    resolved, source = module._resolve_python_executable(
        ".venv/bin/python",
        repo_root=tmp_path,
        environ={},
        current_executable=sys.executable,
    )

    assert resolved == (tmp_path / ".venv/bin/python").resolve()
    assert source == "cli"


def test_relative_python_path_does_not_resolve_from_caller_path(tmp_path, monkeypatch):
    module = _load_cli()
    caller_bin = tmp_path / "caller" / ".venv" / "bin"
    caller_bin.mkdir(parents=True)
    caller_python = caller_bin / "python"
    caller_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(caller_bin))
    host_root = tmp_path / "host"

    resolved, source = module._resolve_python_executable(
        ".venv/bin/python",
        repo_root=host_root,
        environ={},
        current_executable=sys.executable,
    )

    assert resolved == (host_root / ".venv/bin/python").resolve()
    assert resolved != caller_python.resolve()
    assert source == "cli"


def test_virtualenv_launchers_are_not_equivalent_by_shared_base_binary(tmp_path):
    module = _load_cli()
    first = tmp_path / "first" / "bin" / "python"
    second = tmp_path / "second" / "bin" / "python"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.symlink_to(sys.executable)
    second.symlink_to(sys.executable)

    assert os.path.samefile(first, second)
    assert module._same_executable(first, second) is False


def test_reexec_preserves_arguments_and_exports_resolved_root(tmp_path, monkeypatch):
    module = _load_cli()
    target_python = tmp_path / "host-python"
    target_python.write_text("#!/bin/sh\n", encoding="utf-8")
    target_python.chmod(0o755)
    script_path = tmp_path / "installed" / "travel_guide_bridge.py"
    script_path.parent.mkdir()
    script_path.write_text("", encoding="utf-8")
    captured = {}

    def fake_execve(executable, argv, environ):
        captured.update(executable=executable, argv=argv, environ=environ)

    monkeypatch.setattr(module.os, "execve", fake_execve)

    module._reexec_with_configured_python(
        target_python,
        repo_root=ROOT,
        argv=["resume-plan", "project-1"],
        environ={"PRESERVE_ME": "yes"},
        script_path=script_path,
    )

    assert captured["executable"] == str(target_python)
    assert captured["argv"] == [
        str(target_python),
        str(script_path.resolve()),
        "resume-plan",
        "project-1",
    ]
    assert captured["environ"]["PRESERVE_ME"] == "yes"
    assert captured["environ"][module.REPO_ROOT_ENV] == str(ROOT)
    assert captured["environ"][module._BOOTSTRAP_ENV] == str(target_python)


def test_reexec_can_reload_installed_root_from_config(tmp_path, monkeypatch):
    module = _load_cli()
    target_python = tmp_path / "host-python"
    target_python.write_text("#!/bin/sh\n", encoding="utf-8")
    target_python.chmod(0o755)
    captured = {}

    monkeypatch.setattr(
        module.os,
        "execve",
        lambda executable, argv, environ: captured.update(
            executable=executable, argv=argv, environ=environ
        ),
    )

    module._reexec_with_configured_python(
        target_python,
        repo_root=tmp_path / "installed/runtime",
        argv=["prepare-route-only", "source", "target"],
        environ={},
        export_repo_root=False,
    )

    assert module.REPO_ROOT_ENV not in captured["environ"]
    assert captured["environ"][module._BOOTSTRAP_ENV] == str(target_python)


def test_reexec_keeps_matching_virtualenv_launcher(tmp_path, monkeypatch):
    module = _load_cli()
    target_python = tmp_path / "host-venv" / "bin" / "python"
    target_python.parent.mkdir(parents=True)
    target_python.symlink_to(sys.executable)
    monkeypatch.setattr(module.sys, "executable", str(target_python))
    monkeypatch.setattr(
        module.os,
        "execve",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not re-exec")),
    )

    module._reexec_with_configured_python(
        target_python,
        repo_root=ROOT,
        argv=["resume-plan", "project-1"],
        environ={},
    )


def test_reexec_accepts_matching_bootstrap_marker(tmp_path, monkeypatch):
    module = _load_cli()
    target_python = tmp_path / "host-python"
    target_python.write_text("#!/bin/sh\n", encoding="utf-8")
    target_python.chmod(0o755)
    monkeypatch.setattr(module.sys, "executable", str(tmp_path / "resolved-python"))
    monkeypatch.setattr(
        module.os,
        "execve",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not re-exec")),
    )

    module._reexec_with_configured_python(
        target_python,
        repo_root=ROOT,
        argv=["resume-plan", "project-1"],
        environ={module._BOOTSTRAP_ENV: str(target_python)},
    )


def test_installed_copy_doctor_validates_configured_host_repository(tmp_path):
    installed = tmp_path / "skills" / "travel-guide-codex-orchestrator"
    shutil.copytree(SKILL_ROOT, installed)
    copied_script = installed / "scripts" / "travel_guide_bridge.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["TRAVEL_GUIDE_REPO_ROOT"] = str(ROOT)
    env["TRAVEL_GUIDE_PYTHON"] = sys.executable

    completed = subprocess.run(
        [sys.executable, str(copied_script), "doctor"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "ready"
    assert {check["name"] for check in report["checks"]} == {
        "platform",
        "skill_bundle",
        "host_repository",
        "python_executable",
        "role_contracts",
        "runtime_import",
    }


def test_generated_install_runs_route_draft_outside_repository(tmp_path):
    runtime_python = os.environ.get(
        "TRAVEL_GUIDE_TEST_RUNTIME_PYTHON", sys.executable
    )
    codex_home = tmp_path / "codex-home"
    state_root = tmp_path / "travel-state"
    launch_dir = tmp_path / "unrelated-working-directory"
    launch_dir.mkdir()
    installed = codex_home / "skills" / "travel-guide-codex-orchestrator"

    base_install_command = [
        sys.executable,
        str(INSTALLER),
        "--source-root",
        str(ROOT),
        "--codex-home",
        str(codex_home),
    ]
    install_command = [
        *base_install_command,
        "--runtime-python",
        runtime_python,
        "--state-root",
        str(state_root),
    ]
    install = subprocess.run(
        install_command,
        cwd=launch_dir,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    install_report = json.loads(install.stdout)
    assert install_report["status"] == "installed"
    assert install_report["dependencies_installed"] is False
    runtime = installed / "runtime"
    assert not (runtime / ".env").exists()
    assert not (runtime / "projects").exists()
    assert not (runtime / "data").exists()

    obsolete = installed / "obsolete-install-file"
    obsolete.write_text("old", encoding="utf-8")
    updated = subprocess.run(
        [*base_install_command, "--replace"],
        cwd=launch_dir,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert updated.returncode == 0, updated.stderr
    assert not obsolete.exists()
    updated_config = json.loads(
        (installed / ".travel-guide-runtime.json").read_text(encoding="utf-8")
    )
    assert updated_config["python_executable"] == str(Path(runtime_python).absolute())
    assert updated_config["state_root"] == str(state_root.resolve())

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["CODEX_HOME"] = str(codex_home)
    env.pop("PYTHONPATH", None)
    for name in list(env):
        if name.startswith("TRAVEL_GUIDE_"):
            env.pop(name)

    bridge = installed / "scripts" / "travel_guide_bridge.py"
    doctor = subprocess.run(
        [runtime_python, "-I", str(bridge), "doctor"],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert doctor.returncode == 0, doctor.stderr
    doctor_report = json.loads(doctor.stdout)
    assert doctor_report["status"] == "ready"
    assert doctor_report["repo_root"] == str(runtime)
    assert doctor_report["runtime_root"] == str(state_root)
    host_check = next(
        check for check in doctor_report["checks"] if check["name"] == "host_repository"
    )
    assert host_check["source"] == "installed_bundle"
    import_check = next(
        check for check in doctor_report["checks"] if check["name"] == "runtime_import"
    )
    assert import_check["status"] == "passed"
    assert Path(import_check["modules"]["bridge"]).is_relative_to(runtime)
    assert Path(import_check["modules"]["preflight"]).is_relative_to(runtime)
    assert Path(import_check["modules"]["kernel"]).is_relative_to(runtime)

    source_workspace = state_root / "projects" / "route-source" / "workspace"
    source_workspace.mkdir(parents=True)
    (source_workspace / "requirements.json").write_text(
        json.dumps(
            {
                "topic": "成都",
                "requirements": {"destination": "成都", "days": 1},
                "updated_at": "2026-09-10T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prepared = subprocess.run(
        [
            runtime_python,
            "-I",
            str(bridge),
            "prepare-route-only",
            "route-source",
            "standalone-route",
        ],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    assert json.loads(prepared.stdout)["status"] == "ready_for_route_generation"

    route_payload = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "area": "市中心",
                "pois": [
                    {
                        "name": "人民公园",
                        "brief": "安排一段城市休闲体验",
                        "needs_detail": False,
                        "needs_detail_reason": "",
                    }
                ],
                "notes": "按一日慢节奏需求安排。",
                "claims": [
                    {
                        "target_path": "pois.0.name",
                        "subject": "人民公园",
                        "claim_text": "人民公园",
                        "claim_type": "poi_identity",
                        "support_mode": "user_constraint",
                        "claimed_entities": ["人民公园"],
                        "evidence_refs": [],
                    },
                    {
                        "target_path": "pois.0.name",
                        "subject": "人民公园",
                        "claim_text": "人民公园适合作为本次城市休闲路线的一站。",
                        "claim_type": "poi_selection",
                        "support_mode": "estimate",
                        "claimed_entities": ["人民公园"],
                        "evidence_refs": [],
                    },
                    {
                        "target_path": "pois.0.brief",
                        "subject": "人民公园",
                        "claim_text": "这里安排一段城市休闲体验。",
                        "claim_type": "planning_judgment",
                        "support_mode": "estimate",
                        "claimed_entities": ["人民公园"],
                        "evidence_refs": [],
                    },
                    {
                        "target_path": "notes",
                        "subject": "Day 1",
                        "claim_text": "本日按一日慢节奏需求安排。",
                        "claim_type": "planning_judgment",
                        "support_mode": "user_constraint",
                        "claimed_entities": [],
                        "evidence_refs": [],
                    },
                ],
            }
        ]
    }
    route_input = tmp_path / "route.json"
    route_input.write_text(
        json.dumps(route_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    written = subprocess.run(
        [
            runtime_python,
            "-I",
            str(bridge),
            "write-route-plan",
            "standalone-route",
            "--input",
            str(route_input),
        ],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    written_report = json.loads(written.stdout)
    assert written_report["status"] == "awaiting_user_confirmation"
    route_sha = written_report["route_structure_sha256"]

    validated = subprocess.run(
        [
            runtime_python,
            "-I",
            str(bridge),
            "validate",
            "standalone-route",
            "route",
            "--route-check",
            "structure",
            "--expected-route-sha",
            route_sha,
        ],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert validated.returncode == 0, validated.stderr
    validation_report = json.loads(validated.stdout)
    assert validation_report["issues"] == []
    assert any(
        check["name"] == "route_structure" and check["status"] == "passed"
        for check in validation_report["checks"]
    )
    assert (
        state_root
        / "projects"
        / "standalone-route"
        / "workspace"
        / "route_plan.json"
    ).is_file()


def test_installed_copy_doctor_blocks_without_host_repository(tmp_path):
    copied_script = tmp_path / "travel_guide_bridge.py"
    shutil.copy2(SCRIPT, copied_script)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("TRAVEL_GUIDE_REPO_ROOT", None)
    env.pop("TRAVEL_GUIDE_PYTHON", None)

    completed = subprocess.run(
        [sys.executable, str(copied_script), "doctor"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["status"] == "blocked"
    repository_check = next(
        check for check in report["checks"] if check["name"] == "host_repository"
    )
    assert repository_check["status"] == "failed"


def test_doctor_blocks_incompatible_host_runtime_api(tmp_path):
    module = _load_cli()
    for relative in (*module._RUNTIME_MARKERS, *module._ROLE_CONTRACTS):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    report = module._doctor_report(
        skill_root=SKILL_ROOT,
        repo_root=tmp_path,
        repo_root_source="cli",
        python_executable=Path(sys.executable),
        python_source="current_process",
    )

    assert report["status"] == "blocked"
    import_check = next(
        check for check in report["checks"] if check["name"] == "runtime_import"
    )
    assert import_check["status"] == "failed"
    assert "missing runtime API" in import_check["detail"]
