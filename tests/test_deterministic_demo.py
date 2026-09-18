from __future__ import annotations

import json
from pathlib import Path

from scripts.demo.run_deterministic_demo import DEFAULT_INPUT, run_demo
from scripts.delivery import run_generate_site as site_generator


def _unexpected_call(*_args, **_kwargs):
    raise AssertionError("standalone demo must not use external or mutable project services")


def test_deterministic_demo_is_offline_standalone_and_reproducible(tmp_path, monkeypatch):
    monkeypatch.setattr(site_generator, "get_cached_or_geocode", _unexpected_call)
    monkeypatch.setattr(site_generator, "get_cached_or_poi_overview_photo", _unexpected_call)
    monkeypatch.setattr(site_generator, "_load_effective_review_projection", _unexpected_call)
    monkeypatch.setattr(site_generator, "_load_user_notes", _unexpected_call)

    first = run_demo(output_root=tmp_path / "first")
    second = run_demo(output_root=tmp_path / "second")

    assert first["status"] == "passed"
    assert first["contract_issue_count"] == 0
    assert first["external_calls"] == {"llm": 0, "web": 0, "amap": 0}
    assert first["delivery_sha256"] == second["delivery_sha256"]
    assert first["delivery_files"] == second["delivery_files"]

    workspace = (
        tmp_path
        / "first"
        / "projects"
        / "public-deterministic-demo"
        / "workspace"
    )
    assert (workspace / "final_guide.blocks.json").is_file()
    assert (workspace / "outputs" / "poi_details" / "manifest.json").is_file()
    site = (workspace / "site.html").read_text(encoding="utf-8")
    assert "海岚市两日慢游" in site
    assert "灯塔步道" in site
    assert '<div class="site-toolbar">' not in site
    assert "/api/projects/public-deterministic-demo/site/download" not in site
    assert "/user_notes" not in site
    assert "fetch(" not in site
    assert "http://" not in site
    assert "https://" not in site
    assert "当前静态演示未启用在线地图" in site
    assert not any(path.is_file() for path in (workspace / "site_assets").rglob("*"))

    frozen_input = json.loads(DEFAULT_INPUT.read_text(encoding="utf-8"))
    serialized_input = json.dumps(frozen_input, ensure_ascii=False)
    assert "source_url" not in serialized_input
    assert "uploaded_overview_photo_path" not in serialized_input
    assert "uploaded_photo_path" not in serialized_input


def test_deterministic_demo_requires_force_before_rebuild(tmp_path):
    output = tmp_path / "demo"
    first = run_demo(output_root=output)

    try:
        run_demo(output_root=output)
    except FileExistsError as exc:
        assert "--force" in str(exc)
    else:
        raise AssertionError("existing demo output must not be overwritten implicitly")

    rebuilt = run_demo(output_root=output, force=True)
    assert rebuilt["delivery_sha256"] == first["delivery_sha256"]
