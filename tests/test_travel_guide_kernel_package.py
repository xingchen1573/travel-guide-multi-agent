from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_kernel_wheel_installs_without_host_source(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source_dir / "pyproject.toml")
    shutil.copytree(
        ROOT / "travel_guide_kernel",
        source_dir / "travel_guide_kernel",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    wheel_dir = tmp_path / "wheelhouse"
    wheel_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "TRAVEL_GUIDE_RUNTIME_ROOT": str(tmp_path / "runtime"),
        }
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-cache-dir",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(source_dir),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_dir.glob("travel_guide_kernel-*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        members = set(archive.namelist())
    assert {
        "travel_guide_kernel/__init__.py",
        "travel_guide_kernel/models.py",
        "travel_guide_kernel/paths.py",
        "travel_guide_kernel/projects.py",
        "travel_guide_kernel/py.typed",
        "travel_guide_kernel/route.py",
        "travel_guide_kernel/schema.py",
        "travel_guide_kernel/store.py",
        "travel_guide_kernel/final_guide_blocks.py",
        "travel_guide_kernel/final_guide_sections.py",
        "travel_guide_kernel/workspace.py",
    } <= members
    assert not any(
        name.startswith(("src/", "tests/", "projects/", "data/", ".agents/"))
        for name in members
    )

    install_dir = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-cache-dir",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheels[0]),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    smoke = r'''
import importlib.abc
import importlib.util
import sys
from pathlib import Path

install_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(install_dir))
assert importlib.util.find_spec("src") is None

class BlockHostImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "src" or fullname.startswith("src."):
            raise AssertionError(f"unexpected host import: {fullname}")
        return None

sys.meta_path.insert(0, BlockHostImports())

import travel_guide_kernel
from travel_guide_kernel import models, paths, projects, route, workspace

package_root = Path(travel_guide_kernel.__file__).resolve().parent
assert package_root.parent == install_dir
assert workspace.store.__name__ == "travel_guide_kernel.store"
assert not any(name == "src" or name.startswith("src.") for name in sys.modules)

plan = models.RoutePlan.model_validate({
    "days": [{"day": 1, "city": "成都", "pois": [{"name": "武侯祠"}]}]
})
payload = plan.model_dump(mode="json")
assert payload["days"][0]["pois"][0]["name"] == "武侯祠"
assert route.validate_route_structure(payload, expected_days=1) == []
assert len(route.route_structure_hash(payload)) == 64
assert paths.resolve_runtime_root("/tmp/code", {}) == Path("/tmp/code").resolve()
assert paths.resolve_runtime_root(
    "/tmp/code", {paths.RUNTIME_ROOT_ENV: "/tmp/travel-runtime"}
) == Path("/tmp/travel-runtime").resolve()

project_id = "package-smoke"
workspace.store.init_project(project_id)
workspace.store.write_requirements(project_id, "成都", {"days": 1})
workspace.store.write_route_plan(project_id, "route_agent", plan)
saved_requirements = workspace.store.read_requirements(project_id)
assert saved_requirements["topic"] == "成都"
assert saved_requirements["requirements"] == {"days": 1}
assert workspace.store.read_route_plan(project_id).model_dump(mode="json") == plan.model_dump(mode="json")
assert (Path(paths.resolve_runtime_root(".")) / "projects" / project_id / "workspace").is_dir()

assert projects.__name__ == "travel_guide_kernel.projects"
assert not any(name == "src" or name.startswith("src.") for name in sys.modules)
'''
    subprocess.run(
        [sys.executable, "-I", "-c", smoke, str(install_dir)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
