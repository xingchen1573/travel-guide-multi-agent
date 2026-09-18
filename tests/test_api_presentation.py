"""src/api/routers/presentation.py的FastAPI TestClient测试：呈现层产出触发+预览。

跟test_api_jobs.py的fake_generate_site/fake_generate_poster fixture同款风格，
不发起真实生图/写站点调用。
"""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import jobs
from src.api.app import app
from src.workspace import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    jobs._jobs.clear()
    yield


def _seed_project(project_id: str) -> None:
    store.init_project(project_id)


def _wait_until_done(task_type: str, key: str, timeout_steps: int = 50) -> None:
    for _ in range(timeout_steps):
        if not jobs.is_running(task_type, key):
            return
        threading.Event().wait(0.05)


# ---------------------------------------------------------------------------
# generate_site
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_generate_site(monkeypatch):
    fake_module = types.ModuleType("scripts.delivery.run_generate_site")
    calls = []

    def fake_fn(project_id):
        calls.append(project_id)
        path = store.workspace_dir(project_id) / "site.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html>fake site</html>", encoding="utf-8")
        return path

    fake_module.generate_site = fake_fn
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_site", fake_module)
    return fake_module, calls


def test_trigger_generate_site_not_found():
    resp = client.post("/api/projects/does-not-exist/generate_site")
    assert resp.status_code == 404


def test_trigger_generate_site_success_and_preview(fake_generate_site):
    _seed_project("proj-site")
    resp = client.post("/api/projects/proj-site/generate_site")
    assert resp.status_code == 202

    _wait_until_done("generate_site", "proj-site")
    status = client.get("/api/projects/proj-site/generate_site/status")
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"

    preview = client.get("/api/projects/proj-site/site")
    assert preview.status_code == 200
    assert "fake site" in preview.text


def test_generate_site_status_not_found_when_never_triggered():
    _seed_project("proj-site-2")
    resp = client.get("/api/projects/proj-site-2/generate_site/status")
    assert resp.status_code == 404


def test_get_site_not_found_before_generation():
    _seed_project("proj-site-3")
    resp = client.get("/api/projects/proj-site-3/site")
    assert resp.status_code == 404


def test_site_asset_serves_full_resolution_file_and_blocks_traversal():
    _seed_project("proj-site-assets")
    assets_dir = store.workspace_dir("proj-site-assets") / "site_assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "img-test.jpg").write_bytes(b"full-resolution-bytes")

    asset = client.get("/api/projects/proj-site-assets/site_assets/img-test.jpg")
    assert asset.status_code == 200
    assert asset.content == b"full-resolution-bytes"

    traversal = client.get("/api/projects/proj-site-assets/site_assets/../site.html")
    assert traversal.status_code == 404


def test_download_site_sets_attachment_header(fake_generate_site):
    """下载副本路由（docs/ARCHITECTURE.md第21.4节）：跟GET .../site同一份内容，多一个
    Content-Disposition:attachment头触发浏览器下载而不是内联展示。"""
    _seed_project("proj-site-download")
    client.post("/api/projects/proj-site-download/generate_site")
    _wait_until_done("generate_site", "proj-site-download")

    resp = client.get("/api/projects/proj-site-download/site/download")
    assert resp.status_code == 200
    assert "fake site" in resp.text
    assert "attachment" in resp.headers["content-disposition"]
    assert "site.html" in resp.headers["content-disposition"]


def test_download_site_not_found_before_generation():
    _seed_project("proj-site-download-2")
    resp = client.get("/api/projects/proj-site-download-2/site/download")
    assert resp.status_code == 404


def test_site_history_empty_before_any_generation():
    _seed_project("proj-site-history-empty")
    resp = client.get("/api/projects/proj-site-history-empty/site/history")
    assert resp.status_code == 200
    assert resp.json() == []


def test_site_history_not_found_for_missing_project():
    resp = client.get("/api/projects/does-not-exist/site/history")
    assert resp.status_code == 404


def test_site_history_lists_and_serves_archived_version(fake_generate_site):
    """生成两次后，第一次的版本应该出现在历史列表里，且能通过历史接口取回原内容。"""
    _seed_project("proj-site-history")
    ws = store.workspace_dir("proj-site-history")

    resp1 = client.post("/api/projects/proj-site-history/generate_site")
    assert resp1.status_code == 202
    _wait_until_done("generate_site", "proj-site-history")

    # 手动改动当前site.html内容，验证第二次生成前它被归档而不是丢失
    (ws / "site.html").write_text("<html>first version</html>", encoding="utf-8")

    resp2 = client.post("/api/projects/proj-site-history/generate_site")
    assert resp2.status_code == 202
    _wait_until_done("generate_site", "proj-site-history")

    history = client.get("/api/projects/proj-site-history/site/history")
    assert history.status_code == 200
    timestamps = history.json()
    assert len(timestamps) == 1

    archived = client.get(f"/api/projects/proj-site-history/site/history/{timestamps[0]}")
    assert archived.status_code == 200
    assert "first version" in archived.text


def test_site_history_version_not_found():
    _seed_project("proj-site-history-404")
    resp = client.get("/api/projects/proj-site-history-404/site/history/nonexistent")
    assert resp.status_code == 404


def test_trigger_generate_site_concurrent_returns_409(fake_generate_site):
    started = threading.Event()
    release = threading.Event()

    def blocking(project_id):
        started.set()
        release.wait(timeout=5)
        return store.workspace_dir(project_id) / "site.html"

    fake_generate_site[0].generate_site = blocking
    _seed_project("proj-site-4")

    resp1 = client.post("/api/projects/proj-site-4/generate_site")
    assert resp1.status_code == 202
    started.wait(timeout=5)

    resp2 = client.post("/api/projects/proj-site-4/generate_site")
    assert resp2.status_code == 409

    release.set()
    _wait_until_done("generate_site", "proj-site-4")


# ---------------------------------------------------------------------------
# generate_poster
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_generate_poster(monkeypatch):
    fake_module = types.ModuleType("scripts.delivery.run_generate_poster")
    calls = []

    def fake_fn(project_id, confirm=True):
        calls.append((project_id, confirm))
        ws = store.workspace_dir(project_id)
        ws.mkdir(parents=True, exist_ok=True)
        path = ws / "poster.png"
        path.write_bytes(b"fake-png-bytes")
        (ws / "poster_prompt.txt").write_text("fake prompt content", encoding="utf-8")
        return path

    fake_module.generate_poster = fake_fn
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_poster", fake_module)
    return fake_module, calls


def test_trigger_generate_poster_not_found():
    resp = client.post("/api/projects/does-not-exist/generate_poster")
    assert resp.status_code == 404


def test_trigger_generate_poster_success_skips_cli_confirm_and_previews(fake_generate_poster):
    fake_module, calls = fake_generate_poster
    _seed_project("proj-poster")

    resp = client.post("/api/projects/proj-poster/generate_poster")
    assert resp.status_code == 202
    _wait_until_done("generate_poster", "proj-poster")

    assert calls == [("proj-poster", False)]
    status = client.get("/api/projects/proj-poster/generate_poster/status")
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"

    preview = client.get("/api/projects/proj-poster/poster")
    assert preview.status_code == 200
    assert preview.content == b"fake-png-bytes"


def test_get_poster_not_found_before_generation():
    _seed_project("proj-poster-2")
    resp = client.get("/api/projects/proj-poster-2/poster")
    assert resp.status_code == 404


def test_get_poster_prompt_returns_audit_archive(fake_generate_poster):
    _seed_project("proj-poster-prompt")
    resp = client.post("/api/projects/proj-poster-prompt/generate_poster")
    assert resp.status_code == 202
    _wait_until_done("generate_poster", "proj-poster-prompt")

    prompt_resp = client.get("/api/projects/proj-poster-prompt/poster/prompt")
    assert prompt_resp.status_code == 200
    assert prompt_resp.json()["prompt"] == "fake prompt content"


def test_get_poster_prompt_not_found_before_generation():
    _seed_project("proj-poster-prompt-2")
    resp = client.get("/api/projects/proj-poster-prompt-2/poster/prompt")
    assert resp.status_code == 404


def test_poster_history_empty_before_any_generation():
    _seed_project("proj-poster-history-empty")
    resp = client.get("/api/projects/proj-poster-history-empty/poster/history")
    assert resp.status_code == 200
    assert resp.json() == []


def test_poster_history_lists_and_serves_archived_version_and_prompt(fake_generate_poster):
    """生成两次后，第一次的图和prompt应该配套出现在历史里，且能通过历史接口取回。"""
    _seed_project("proj-poster-history")
    ws = store.workspace_dir("proj-poster-history")

    resp1 = client.post("/api/projects/proj-poster-history/generate_poster")
    assert resp1.status_code == 202
    _wait_until_done("generate_poster", "proj-poster-history")

    # 手动改动当前内容，验证第二次生成前它被归档而不是丢失
    (ws / "poster.png").write_bytes(b"first-version-bytes")
    (ws / "poster_prompt.txt").write_text("first version prompt", encoding="utf-8")

    resp2 = client.post("/api/projects/proj-poster-history/generate_poster")
    assert resp2.status_code == 202
    _wait_until_done("generate_poster", "proj-poster-history")

    history = client.get("/api/projects/proj-poster-history/poster/history")
    assert history.status_code == 200
    timestamps = history.json()
    assert len(timestamps) == 1

    archived_image = client.get(f"/api/projects/proj-poster-history/poster/history/{timestamps[0]}")
    assert archived_image.status_code == 200
    assert archived_image.content == b"first-version-bytes"

    archived_prompt = client.get(
        f"/api/projects/proj-poster-history/poster/history/{timestamps[0]}/prompt"
    )
    assert archived_prompt.status_code == 200
    assert archived_prompt.json()["prompt"] == "first version prompt"


def test_poster_history_version_not_found():
    _seed_project("proj-poster-history-404")
    resp = client.get("/api/projects/proj-poster-history-404/poster/history/nonexistent")
    assert resp.status_code == 404
