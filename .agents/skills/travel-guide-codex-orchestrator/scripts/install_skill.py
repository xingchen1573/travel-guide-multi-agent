#!/usr/bin/env python3
"""Install a self-contained travel Skill runtime from the canonical repository."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Sequence


SKILL_NAME = "travel-guide-codex-orchestrator"
CONFIG_NAME = ".travel-guide-runtime.json"
CONFIG_VERSION = "travel-guide-skill-runtime-v1"
SKILL_RELATIVE_PATH = Path(".agents/skills") / SKILL_NAME
SOURCE_MARKERS = (
    SKILL_RELATIVE_PATH / "SKILL.md",
    Path("src/codex_skill/bridge.py"),
    Path("travel_guide_kernel/store.py"),
    Path("requirements.txt"),
)
RUNTIME_DIRECTORIES = (Path("src"), Path("travel_guide_kernel"))
RUNTIME_FILES = (Path("pyproject.toml"), Path("requirements.txt"), Path(".env.example"))
SENSITIVE_NAMES = {
    ".env",
    "api-keys.json",
    "api_keys.json",
    "id_ed25519",
    "id_rsa",
}
SENSITIVE_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


class InstallError(RuntimeError):
    pass


def _missing_markers(root: Path) -> list[str]:
    return [str(path) for path in SOURCE_MARKERS if not (root / path).is_file()]


def _find_source_root(explicit: str | Path | None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        missing = _missing_markers(root)
        if missing:
            raise InstallError("source root is missing: " + ", ".join(missing))
        return root

    starts = (Path.cwd(), Path(__file__).resolve().parent)
    for start in starts:
        for root in (start, *start.parents):
            if not _missing_markers(root):
                return root
    raise InstallError("cannot find the canonical travel repository")


def _resolve_python(value: str | Path) -> Path:
    raw = str(value)
    path = Path(raw).expanduser()
    if not path.is_absolute() and path.parent == Path("."):
        command = shutil.which(raw)
        if command:
            path = Path(command)
    # Preserve a virtual-environment launcher instead of resolving its symlink
    # to the base interpreter, which would lose that environment's packages.
    path = path.absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise InstallError(f"runtime Python is not executable: {path}")
    return path


def _paths_overlap(left: Path, right: Path) -> bool:
    def variants(path: Path) -> set[Path]:
        absolute = Path(os.path.abspath(path.expanduser()))
        return {absolute, absolute.resolve()}

    return any(
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
        for left_path in variants(left)
        for right_path in variants(right)
    )


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in SENSITIVE_NAMES
        or (lowered.startswith(".env.") and lowered != ".env.example")
        or lowered.startswith(("credentials.", "secrets."))
        or lowered.endswith(SENSITIVE_SUFFIXES)
    )


def _ignore_generated(_directory: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__", ".pytest_cache", ".DS_Store"}
    return {
        name
        for name in names
        if name in ignored
        or name.endswith((".pyc", ".pyo"))
        or _is_sensitive_name(name)
    }


def _load_existing_config(destination: Path) -> dict:
    path = destination / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"existing install config is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != CONFIG_VERSION:
        raise InstallError(f"existing install config is unsupported: {path}")
    return value


def _copy_runtime(source_root: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for relative in RUNTIME_DIRECTORIES:
        shutil.copytree(
            source_root / relative,
            destination / relative,
            ignore=_ignore_generated,
        )
    for relative in RUNTIME_FILES:
        source = source_root / relative
        if source.is_file():
            shutil.copy2(source, destination / relative.name)

    agents_source = source_root / ".codex/agents"
    agents_destination = destination / ".codex/agents"
    agents_destination.mkdir(parents=True)
    for source in sorted(agents_source.glob("travel-*.toml")):
        shutil.copy2(source, agents_destination / source.name)


def _install_dependencies(python_executable: Path, source_root: Path) -> None:
    subprocess.run(
        [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "-r",
            str(source_root / "requirements.txt"),
        ],
        check=True,
    )
    subprocess.run(
        [str(python_executable), "-m", "pip", "install", str(source_root)],
        check=True,
    )


def install(
    *,
    source_root: Path,
    codex_home: Path,
    runtime_python: Path,
    state_root: Path,
    replace: bool,
    install_dependencies: bool,
) -> dict:
    source_root = source_root.expanduser().resolve()
    codex_home = codex_home.expanduser().resolve()
    runtime_python = runtime_python.expanduser().absolute()
    state_root = state_root.expanduser().resolve()
    destination = codex_home / "skills" / SKILL_NAME
    source_skill = source_root / SKILL_RELATIVE_PATH

    if _paths_overlap(destination, source_skill):
        raise InstallError(
            "destination overlaps the canonical Skill source; choose another CODEX_HOME"
        )
    if _paths_overlap(destination, state_root):
        raise InstallError(
            "state root overlaps the installed Skill; choose a separate state directory"
        )
    if _paths_overlap(destination, runtime_python):
        raise InstallError(
            "runtime Python overlaps the installed Skill; choose a separate environment"
        )
    if destination.exists() and not replace:
        raise InstallError(
            f"destination already exists: {destination}; rerun with --replace to update it"
        )

    if install_dependencies:
        _install_dependencies(runtime_python, source_root)

    destination.parent.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{SKILL_NAME}.install-{uuid.uuid4().hex}"
    try:
        shutil.copytree(
            source_root / SKILL_RELATIVE_PATH,
            staging,
            ignore=_ignore_generated,
        )
        _copy_runtime(source_root, staging / "runtime")
        config = {
            "schema_version": CONFIG_VERSION,
            "code_root": "runtime",
            "python_executable": str(runtime_python),
            "state_root": str(state_root),
        }
        (staging / CONFIG_NAME).write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            expected_skill = destination / "SKILL.md"
            expected_bridge = destination / "scripts/travel_guide_bridge.py"
            if not expected_skill.is_file() or not expected_bridge.is_file():
                raise InstallError(
                    f"refusing to replace an unrecognized directory: {destination}"
                )
            shutil.rmtree(destination)
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    bridge = destination / "scripts/travel_guide_bridge.py"
    return {
        "schema_version": CONFIG_VERSION,
        "status": "installed",
        "skill_root": str(destination),
        "code_root": str(destination / "runtime"),
        "state_root": str(state_root),
        "python_executable": str(runtime_python),
        "dependencies_installed": install_dependencies,
        "next_command": [str(runtime_python), str(bridge), "doctor"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the travel-guide Codex Skill and its runtime snapshot."
    )
    parser.add_argument("--source-root")
    parser.add_argument("--codex-home")
    parser.add_argument("--runtime-python")
    parser.add_argument("--state-root")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--install-dependencies", action="store_true")
    args = parser.parse_args(argv)

    try:
        source_root = _find_source_root(args.source_root)
        codex_home = Path(
            args.codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser().resolve()
        destination = codex_home / "skills" / SKILL_NAME
        existing = _load_existing_config(destination) if args.replace else {}
        runtime_python = _resolve_python(
            args.runtime_python or existing.get("python_executable") or sys.executable
        )
        state_root = Path(
            args.state_root
            or existing.get("state_root")
            or codex_home / "travel-guide-runtime"
        ).expanduser().resolve()
        result = install(
            source_root=source_root,
            codex_home=codex_home,
            runtime_python=runtime_python,
            state_root=state_root,
            replace=args.replace,
            install_dependencies=args.install_dependencies,
        )
    except (InstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"travel-guide install failed: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
