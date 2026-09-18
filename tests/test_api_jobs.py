"""src/api/jobs.py单元测试：通用任务框架（机制A）——重跑/素材采集/生成网站/生成海报
四种任务类型共用同一套加锁+状态机+日志上报骨架。

orchestrator.run_plan等都是懒加载导入的（避免只读接口场景强依赖langgraph/langchain），
测试里通过monkeypatch sys.modules注入假模块，不需要真实装这些依赖。
"""
from __future__ import annotations

import sys
import threading
import types

import pytest

from src.api import jobs
from src.workspace import store


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    jobs._jobs.clear()
    yield


def _seed_project(project_id: str, requirements: dict) -> None:
    store.init_project(project_id)
    store.write_requirements(project_id, requirements["destination"], requirements)
    (store.workspace_dir(project_id) / "route_plan.json").write_text("{}", encoding="utf-8")


def _wait_until_done(task_type: str, key: str, timeout_steps: int = 50) -> None:
    for _ in range(timeout_steps):
        if not jobs.is_running(task_type, key):
            return
        threading.Event().wait(0.05)


# ---------------------------------------------------------------------------
# 通用任务骨架本身：trigger/get_status/is_running/并发锁
# ---------------------------------------------------------------------------


def test_trigger_runs_work_fn_and_records_success():
    calls = []

    def work(log):
        calls.append("ran")
        log("步骤1完成")
        return {"extra_field": "abc"}

    jobs.trigger("demo", "key-a", work)
    _wait_until_done("demo", "key-a")

    assert calls == ["ran"]
    status = jobs.get_status("demo", "key-a")
    assert status["status"] == "succeeded"
    assert status["extra_field"] == "abc"
    assert status["log"] == ["步骤1完成"]


def test_trigger_captures_exception_as_failed():
    def work(log):
        raise RuntimeError("模拟失败")

    jobs.trigger("demo", "key-b", work)
    _wait_until_done("demo", "key-b")

    status = jobs.get_status("demo", "key-b")
    assert status["status"] == "failed"
    assert "模拟失败" in status["error"]


def test_trigger_rejects_concurrent_request_for_same_key():
    started = threading.Event()
    release = threading.Event()

    def blocking_work(log):
        started.set()
        release.wait(timeout=5)

    jobs.trigger("demo", "key-c", blocking_work)
    assert jobs.is_running("demo", "key-c")

    with pytest.raises(RuntimeError):
        jobs.trigger("demo", "key-c", blocking_work)

    started.wait(timeout=5)
    release.set()
    _wait_until_done("demo", "key-c")
    assert jobs.get_status("demo", "key-c")["status"] == "succeeded"


def test_different_task_types_with_same_key_do_not_conflict():
    # rerun:成都 和 collect_material:成都 是不同的job_key，互不影响并发锁
    started_a = threading.Event()
    release_a = threading.Event()

    def blocking_work(log):
        started_a.set()
        release_a.wait(timeout=5)

    jobs.trigger("rerun", "成都", blocking_work)
    started_a.wait(timeout=5)

    # 另一种task_type用同一个key，不应该被判定为并发冲突
    jobs.trigger("collect_material", "成都", lambda log: None)
    _wait_until_done("collect_material", "成都")
    assert jobs.get_status("collect_material", "成都")["status"] == "succeeded"

    release_a.set()
    _wait_until_done("rerun", "成都")


def test_get_status_unknown_key_returns_none():
    assert jobs.get_status("demo", "never-triggered") is None


# ---------------------------------------------------------------------------
# trigger_rerun：彻底重跑（清空重建，非断点续跑）
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_orchestrator(monkeypatch):
    fake_module = types.ModuleType("src.orchestrator")
    calls = []

    def fake_run_plan(project_id, requirements):
        calls.append((project_id, requirements))

    fake_module.run_plan = fake_run_plan
    monkeypatch.setitem(sys.modules, "src.orchestrator", fake_module)
    return fake_module, calls


def test_backup_workspace_moves_existing_directory():
    _seed_project("proj-a", {"destination": "成都", "days": 2})
    ws = store.workspace_dir("proj-a")
    assert ws.exists()

    dest = jobs._backup_workspace("proj-a")

    assert not ws.exists()
    assert dest.exists()
    assert (dest / "route_plan.json").exists()


def test_backup_workspace_noop_when_no_workspace():
    dest = jobs._backup_workspace("brand-new")
    assert not dest.exists()


def test_trigger_rerun_success(fake_orchestrator):
    fake_module, calls = fake_orchestrator
    requirements = {"destination": "成都", "days": 2}
    _seed_project("proj-b", requirements)

    jobs.trigger_rerun("proj-b")
    _wait_until_done("rerun", "proj-b")

    assert calls == [("proj-b", requirements)]
    status = jobs.get_status("rerun", "proj-b")
    assert status["status"] == "succeeded"
    assert status["error"] is None


def test_trigger_rerun_failure_captures_error(fake_orchestrator):
    fake_module, _ = fake_orchestrator

    def raising_run_plan(project_id, requirements):
        raise RuntimeError("模拟LLM限流")

    fake_module.run_plan = raising_run_plan
    requirements = {"destination": "成都", "days": 2}
    _seed_project("proj-c", requirements)

    jobs.trigger_rerun("proj-c")
    _wait_until_done("rerun", "proj-c")

    status = jobs.get_status("rerun", "proj-c")
    assert status["status"] == "failed"
    assert "模拟LLM限流" in status["error"]


def test_trigger_rerun_without_requirements_marks_failed(fake_orchestrator):
    store.init_project("proj-no-req")

    jobs.trigger_rerun("proj-no-req")
    _wait_until_done("rerun", "proj-no-req")

    status = jobs.get_status("rerun", "proj-no-req")
    assert status["status"] == "failed"
    assert "requirements.json" in status["error"]


def test_trigger_rerun_rejects_concurrent_request(fake_orchestrator):
    fake_module, _ = fake_orchestrator
    started = threading.Event()
    release = threading.Event()

    def blocking_run_plan(project_id, requirements):
        started.set()
        release.wait(timeout=5)

    fake_module.run_plan = blocking_run_plan
    requirements = {"destination": "成都", "days": 2}
    _seed_project("proj-d", requirements)

    jobs.trigger_rerun("proj-d")
    assert jobs.is_running("rerun", "proj-d")

    with pytest.raises(RuntimeError):
        jobs.trigger_rerun("proj-d")

    started.wait(timeout=5)
    release.set()
    _wait_until_done("rerun", "proj-d")
    assert jobs.get_status("rerun", "proj-d")["status"] == "succeeded"


# ---------------------------------------------------------------------------
# trigger_collect_material：核心素材agent自主采集（key是topic，不是project_id）
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_collection_graph(monkeypatch):
    fake_module = types.ModuleType("src.agents.material_agent.collection_graph")
    calls = []

    def fake_run_material_collection(topic, request, auto_confirm=True, platform="all", log=None):
        calls.append((topic, request, auto_confirm, platform))
        if log is not None:
            log("模拟采集中间过程日志")
        return {"result_summary": "采集3条候选，最终入库2条"}

    fake_module.run_material_collection = fake_run_material_collection
    monkeypatch.setitem(sys.modules, "src.agents.material_agent.collection_graph", fake_module)
    return fake_module, calls


def test_trigger_collect_material_success(fake_collection_graph):
    fake_module, calls = fake_collection_graph

    jobs.trigger_collect_material("成都", "给我3条成都最新攻略")
    _wait_until_done("collect_material", "成都")

    assert calls == [("成都", "给我3条成都最新攻略", True, "all")]
    status = jobs.get_status("collect_material", "成都")
    assert status["status"] == "succeeded"
    assert status["result_summary"] == "采集3条候选，最终入库2条"
    assert any("采集3条候选" in line for line in status["log"])


def test_trigger_collect_material_passes_platform_through(fake_collection_graph):
    """platform（2026-07-26新增）必须透传给run_material_collection，不能悄悄丢掉——
    这是管理后台显式平台选择器的唯一落地点。"""
    fake_module, calls = fake_collection_graph

    jobs.trigger_collect_material("成都", "给我3条抖音攻略", platform="douyin")
    _wait_until_done("collect_material", "成都")

    assert calls == [("成都", "给我3条抖音攻略", True, "douyin")]


def test_trigger_collect_material_forwards_log_callback_for_progress(fake_collection_graph):
    """log回调（2026-07-26新增细粒度进度）必须被传给run_material_collection，
    不能只在开始/结束各打一条——中间过程的文字要能实时追加进任务的log列表。"""
    fake_module, calls = fake_collection_graph

    jobs.trigger_collect_material("成都", "给我3条成都最新攻略")
    _wait_until_done("collect_material", "成都")

    status = jobs.get_status("collect_material", "成都")
    assert any("模拟采集中间过程日志" in line for line in status["log"])


def test_trigger_collect_material_failure_captures_error(fake_collection_graph):
    fake_module, _ = fake_collection_graph

    def raising(topic, request, auto_confirm=True, platform="all", log=None):
        raise RuntimeError("MediaCrawler登录态失效")

    fake_module.run_material_collection = raising

    jobs.trigger_collect_material("重庆", "给我几条重庆攻略")
    _wait_until_done("collect_material", "重庆")

    status = jobs.get_status("collect_material", "重庆")
    assert status["status"] == "failed"
    assert "MediaCrawler登录态失效" in status["error"]


# ---------------------------------------------------------------------------
# trigger_generate_site / trigger_generate_poster
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_generate_site(monkeypatch):
    fake_module = types.ModuleType("scripts.delivery.run_generate_site")
    calls = []

    def fake_generate_site_fn(project_id):
        calls.append(project_id)
        from pathlib import Path
        return Path(f"/fake/{project_id}/site.html")

    fake_module.generate_site = fake_generate_site_fn
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_site", fake_module)
    return fake_module, calls


def test_trigger_generate_site_success(fake_generate_site):
    fake_module, calls = fake_generate_site
    _seed_project("proj-site", {"destination": "成都", "days": 2})

    jobs.trigger_generate_site("proj-site")
    _wait_until_done("generate_site", "proj-site")

    assert calls == ["proj-site"]
    status = jobs.get_status("generate_site", "proj-site")
    assert status["status"] == "succeeded"
    assert "site.html" in status["output_path"]


@pytest.fixture
def fake_generate_poster(monkeypatch):
    fake_module = types.ModuleType("scripts.delivery.run_generate_poster")
    calls = []

    def fake_generate_poster_fn(project_id, confirm=True):
        calls.append((project_id, confirm))
        from pathlib import Path
        return Path(f"/fake/{project_id}/poster.png")

    fake_module.generate_poster = fake_generate_poster_fn
    monkeypatch.setitem(sys.modules, "scripts.delivery.run_generate_poster", fake_module)
    return fake_module, calls


def test_trigger_generate_poster_success_skips_cli_confirm(fake_generate_poster):
    """必须传confirm=False——run_generate_poster.py默认的命令行input()确认在后台线程里
    会卡死，等效的二次确认改由前端在发起请求前弹窗完成。"""
    fake_module, calls = fake_generate_poster
    _seed_project("proj-poster", {"destination": "成都", "days": 2})

    jobs.trigger_generate_poster("proj-poster")
    _wait_until_done("generate_poster", "proj-poster")

    assert calls == [("proj-poster", False)]
    status = jobs.get_status("generate_poster", "proj-poster")
    assert status["status"] == "succeeded"
    assert "poster.png" in status["output_path"]


def test_trigger_generate_poster_failure_captures_error(fake_generate_poster):
    fake_module, _ = fake_generate_poster

    def raising(project_id, confirm=True):
        raise RuntimeError("生图API 504超时")

    fake_module.generate_poster = raising
    _seed_project("proj-poster-fail", {"destination": "成都", "days": 2})

    jobs.trigger_generate_poster("proj-poster-fail")
    _wait_until_done("generate_poster", "proj-poster-fail")

    status = jobs.get_status("generate_poster", "proj-poster-fail")
    assert status["status"] == "failed"
    assert "504" in status["error"]


# ---------------------------------------------------------------------------
# _archive_previous_output / 版本历史归档（2026-07-26新增，见docs/ARCHITECTURE.md第13.2节）
# ---------------------------------------------------------------------------


def test_generate_site_archives_previous_version_before_regenerating(fake_generate_site):
    """已存在旧site.html时，生成前应该把它移到history/site/<timestamp>/site.html，
    不是直接覆盖丢失。"""
    _seed_project("proj-site-archive", {"destination": "成都", "days": 2})
    ws = store.workspace_dir("proj-site-archive")
    (ws / "site.html").write_text("<html>old version</html>", encoding="utf-8")

    jobs.trigger_generate_site("proj-site-archive")
    _wait_until_done("generate_site", "proj-site-archive")

    history_dir = ws / "history" / "site"
    assert history_dir.exists()
    versions = list(history_dir.iterdir())
    assert len(versions) == 1
    archived_file = versions[0] / "site.html"
    assert archived_file.read_text(encoding="utf-8") == "<html>old version</html>"

    status = jobs.get_status("generate_site", "proj-site-archive")
    assert any("已归档旧版本网站" in line for line in status["log"])


def test_generate_site_no_archive_when_no_previous_version(fake_generate_site):
    """第一次生成时（还没有旧site.html），不应该创建history目录，也不应该记一条归档日志。"""
    _seed_project("proj-site-first", {"destination": "成都", "days": 2})

    jobs.trigger_generate_site("proj-site-first")
    _wait_until_done("generate_site", "proj-site-first")

    ws = store.workspace_dir("proj-site-first")
    assert not (ws / "history").exists()
    status = jobs.get_status("generate_site", "proj-site-first")
    assert not any("已归档" in line for line in status["log"])


def test_generate_poster_archives_previous_image_and_prompt_together(fake_generate_poster):
    """海报归档时poster.png和poster_prompt.txt要配套一起挪走，不能图归档了prompt还留在原地。"""
    _seed_project("proj-poster-archive", {"destination": "成都", "days": 2})
    ws = store.workspace_dir("proj-poster-archive")
    (ws / "poster.png").write_bytes(b"old-fake-png-bytes")
    (ws / "poster_prompt.txt").write_text("旧版本的prompt内容", encoding="utf-8")

    jobs.trigger_generate_poster("proj-poster-archive")
    _wait_until_done("generate_poster", "proj-poster-archive")

    history_dir = ws / "history" / "poster"
    versions = list(history_dir.iterdir())
    assert len(versions) == 1
    assert (versions[0] / "poster.png").read_bytes() == b"old-fake-png-bytes"
    assert (versions[0] / "poster_prompt.txt").read_text(encoding="utf-8") == "旧版本的prompt内容"

    status = jobs.get_status("generate_poster", "proj-poster-archive")
    assert any("已归档旧版本海报" in line for line in status["log"])
