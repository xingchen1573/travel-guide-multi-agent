"""src/api/routers/projects.py的FastAPI TestClient测试。"""
from __future__ import annotations

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.api import jobs
from src.channels.feishu import bindings
from src.review import store as review_store
from src.workspace import store
from src.workspace.schema import (
    PoiDetailManifest,
    PoiDetailManifestEntry,
    ProjectPhase,
    RouteDay,
    RoutePlan,
    RoutePoi,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(bindings, "BINDINGS_PATH", tmp_path / "feishu-bindings.json")
    jobs._jobs.clear()
    yield


def _seed_project(project_id: str, requirements: dict, state=None) -> None:
    store.init_project(project_id)
    store.write_requirements(project_id, requirements["destination"], requirements)
    if state is not None:
        store.update_progress(project_id, "main_agent", state=state)


def test_list_projects_empty():
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_projects_returns_summary():
    _seed_project("chengdu-v1", {"destination": "成都", "days": 3, "style": "美食", "budget": "中等"})

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["project_id"] == "chengdu-v1"
    assert body[0]["destination"] == "成都"
    assert body[0]["days"] == 3
    assert body[0]["state"] == "需求收集中"
    assert body[0]["has_pending_conflicts"] is False
    assert body[0]["has_run_failure"] is False
    assert body[0]["latest_job_status"] is None
    assert body[0]["observed_run_status"] is None


def test_project_summary_separates_process_completion_from_quality_dimensions():
    _seed_project(
        "qinggan-quality",
        {"destination": "青甘", "days": 1},
        state=ProjectPhase.DONE,
    )
    store.write_route_plan(
        "qinggan-quality",
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="敦煌",
                    pois=[RoutePoi(name="鸣沙山月牙泉", needs_detail=True)],
                )
            ]
        ),
    )
    store.write_final_guide(
        "qinggan-quality",
        "integration_agent",
        "# 青甘攻略",
        {"contract_validation": {"status": "passed", "issues": []}},
    )
    store.write_poi_details_manifest(
        "qinggan-quality",
        "main_agent",
        PoiDetailManifest(
            generated=[
                PoiDetailManifestEntry(
                    poi_name="鸣沙山月牙泉",
                    day=1,
                    file="day1_鸣沙山月牙泉.json",
                    citation_status="generation_failed",
                    failure_kind="provider_or_tool_failure",
                )
            ]
        ),
    )
    (store.workspace_dir("qinggan-quality") / "route_feasibility.json").write_text(
        '{"overall_status":"blocked"}', encoding="utf-8"
    )

    summary = client.get("/api/projects/qinggan-quality").json()

    assert summary["state"] == "完成"
    assert summary["delivery_status"] == "passed"
    assert summary["route_risk_status"] == "blocked"
    assert summary["poi_detail_completed"] == 0
    assert summary["poi_detail_expected"] == 1
    assert summary["completion_mode"] == "completed_with_degradation"
    assert summary["force_finalized_count"] == 1


def test_project_summary_reports_conflict_only_force_finalized_degradation():
    project_id = "conflict-only-degraded"
    _seed_project(
        project_id,
        {"destination": "大理", "days": 1},
        state=ProjectPhase.DONE,
    )
    record = store.report_conflict(
        project_id,
        actor="integration_agent",
        target="route_agent",
        reason="独立路线冲突",
        issue_type="route_edit_overreach",
    )
    store.mark_force_finalize(project_id)

    summary = client.get(f"/api/projects/{project_id}").json()

    assert record.id
    assert summary["completion_mode"] == "completed_with_degradation"
    assert summary["force_finalized_count"] == 1


def test_project_summary_reports_canonical_force_finalized_degradation_once():
    project_id = "chengdu-degraded"
    _seed_project(
        project_id,
        {"destination": "成都", "days": 1},
        state=ProjectPhase.DONE,
    )
    details_dir = store.workspace_dir(project_id) / "outputs" / "poi_details"
    details_dir.mkdir(parents=True, exist_ok=True)
    detail = {
        "poi_name": "武侯祠",
        "day": 1,
        "citation_status": "force_finalized",
        "retry_history": ["证据不足"],
    }
    (details_dir / "day1_武侯祠.json").write_text(
        json.dumps(detail, ensure_ascii=False),
        encoding="utf-8",
    )
    (details_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated": [
                    {
                        "poi_name": "武侯祠",
                        "day": 1,
                        "file": "day1_武侯祠.json",
                        "citation_status": "force_finalized",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = client.get(f"/api/projects/{project_id}").json()

    assert summary["completion_mode"] == "completed_with_degradation"
    assert summary["force_finalized_count"] == 1


def test_project_summary_counts_contract_and_feasibility_force_finalization():
    project_id = "chengdu-system-degraded"
    _seed_project(
        project_id,
        {"destination": "成都", "days": 1},
        state=ProjectPhase.DONE,
    )
    store.write_final_guide(
        project_id,
        "integration_agent",
        "# 成都攻略",
        {},
    )
    store.write_final_guide_contract_validation(
        project_id,
        status="force_finalized",
        issues=[{"code": "checklist_missing", "message": "缺少清单"}],
    )
    (store.workspace_dir(project_id) / "route_feasibility.json").write_text(
        json.dumps(
            {"overall_status": "warning", "force_finalized": True, "checks": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = client.get(f"/api/projects/{project_id}").json()

    assert summary["completion_mode"] == "completed_with_degradation"
    assert summary["force_finalized_count"] == 2


def test_list_projects_excludes_stray_directories_without_progress():
    (store.PROJECTS_DIR / "core-node-test" / "agents").mkdir(parents=True)
    _seed_project("chengdu-v1", {"destination": "成都", "days": 3})

    resp = client.get("/api/projects")

    assert resp.status_code == 200
    assert [project["project_id"] for project in resp.json()] == ["chengdu-v1"]


def test_list_projects_reports_sqlite_active_job_separately_from_project_phase(isolated_execution_database):
    _seed_project("chengdu-active", {"destination": "成都", "days": 3})
    instance_id = isolated_execution_database.register_instance(role="backend", environment="test")
    job = isolated_execution_database.create_job("rerun", "chengdu-active", environment="test")
    isolated_execution_database.start_job(job["job_id"], instance_id)

    resp = client.get("/api/projects")

    assert resp.status_code == 200
    summary = resp.json()[0]
    assert summary["state"] == "需求收集中"
    assert summary["latest_job_status"] == "running"
    assert summary["latest_job_health_status"] == "healthy"


def test_list_projects_counts_failed_sqlite_job_as_run_failure(isolated_execution_database):
    _seed_project("chengdu-failed", {"destination": "成都", "days": 3})
    instance_id = isolated_execution_database.register_instance(role="backend", environment="test")
    job = isolated_execution_database.create_job("generate_site", "chengdu-failed", environment="test")
    isolated_execution_database.start_job(job["job_id"], instance_id)
    isolated_execution_database.fail_job(job["job_id"], "生成失败")

    resp = client.get("/api/projects")

    assert resp.status_code == 200
    summary = resp.json()[0]
    assert summary["latest_job_status"] == "failed"
    assert summary["has_run_failure"] is True


def test_list_projects_projects_stale_observation_as_interrupted():
    _seed_project("chengdu-stale", {"destination": "成都", "days": 3})
    store.write_run_status(
        "chengdu-stale",
        {"node": "route_draft", "status": "running", "started_at": "2026-08-01T00:00:00+00:00"},
    )

    resp = client.get("/api/projects")

    assert resp.status_code == 200
    assert resp.json()[0]["observed_run_status"] == "interrupted"


def test_list_projects_flags_run_failure_when_last_run_status_is_failed():
    """真实bug（用户在管理后台发现）：progress.state只在阶段跳转成功时更新，节点内部
    失败（如lifestyle_agent多次重试仍未完成写入）会让run_plan()直接抛异常退出，state
    永远停留在崩溃前最后一次成功跳转的阶段——列表页此前对此完全没有可见信号，看起来
    像是"还在正常调研中"。这里验证run_status.json的failed信号能正确冒泡到列表接口。"""
    _seed_project("chengdu-v13", {"destination": "成都", "days": 2}, state=ProjectPhase.PARALLEL_RESEARCH)
    store.write_run_status(
        "chengdu-v13",
        {"node": "lifestyle_agent_node", "status": "failed", "call_count": 1, "error": "写入失败"},
    )

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["has_run_failure"] is True
    assert body[0]["state"] == "并行调研中"  # progress.state本身依然卡在旧阶段不变


def test_list_projects_run_failure_false_after_a_later_node_succeeds():
    """run_status.json是单文件覆写的：只要失败之后有新的node成功执行过（覆盖成done），
    就不该再判定为"卡死"——failed只是最后一次写入时的信号，不是历史累加的标记。"""
    _seed_project("chengdu-v14", {"destination": "成都", "days": 2})
    store.write_run_status("chengdu-v14", {"node": "lifestyle_agent_node", "status": "failed", "call_count": 1})
    store.write_run_status("chengdu-v14", {"node": "integration_agent_node", "status": "done", "call_count": 1})

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json()[0]["has_run_failure"] is False


def test_list_projects_flags_pending_conflicts():
    _seed_project("chengdu-v9", {"destination": "成都", "days": 2})
    store.report_conflict("chengdu-v9", "citation_checker", "route_agent", "缺失依据")

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json()[0]["has_pending_conflicts"] is True


def test_list_projects_resolved_conflict_is_not_pending():
    _seed_project("chengdu-v10", {"destination": "成都", "days": 2})
    record = store.report_conflict("chengdu-v10", "citation_checker", "route_agent", "缺失依据")
    store.resolve_conflict("chengdu-v10", record.id)

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json()[0]["has_pending_conflicts"] is False


def test_list_projects_keeps_review_findings_separate_from_pending_conflicts():
    _seed_project("chengdu-review-only", {"destination": "成都", "days": 2})
    review_store.create_finding(
        "chengdu-review-only",
        problem_type="manifest_mismatch",
        target={"file": "workspace/outputs/poi_details/manifest.json"},
    )

    summary = client.get("/api/projects").json()[0]

    assert summary["has_pending_conflicts"] is False
    assert summary["pending_conflict_count"] == 0
    assert summary["has_open_findings"] is True
    assert summary["review_findings_open"] == 1


def test_issue_type_stats_empty_when_no_conflicts():
    resp = client.get("/api/projects/issue_type_stats")
    assert resp.status_code == 200
    assert resp.json() == []


def test_issue_type_stats_aggregates_across_projects():
    """跨project聚合：两个project各触发一次route_edit_overreach，应该合计成一条count=2的记录；
    未打标签的历史记录落进unspecified分组，不丢弃。"""
    _seed_project("chengdu-v11", {"destination": "成都", "days": 2})
    _seed_project("chengdu-v12", {"destination": "成都", "days": 3})
    store.report_conflict(
        "chengdu-v11", "citation_checker", "route_agent.day1", "越权改动", issue_type="route_edit_overreach"
    )
    store.report_conflict(
        "chengdu-v12", "citation_checker", "route_agent.day2", "越权改动", issue_type="route_edit_overreach"
    )
    store.report_conflict("chengdu-v11", "citation_checker", "route_agent", "缺失依据")

    resp = client.get("/api/projects/issue_type_stats")
    assert resp.status_code == 200
    body = resp.json()
    assert {"issue_type": "route_edit_overreach", "count": 2} in body
    assert {"issue_type": "unspecified", "count": 1} in body


def test_issue_type_stats_excludes_resolved_history():
    _seed_project("chengdu-resolved-history", {"destination": "成都", "days": 2})
    record = store.report_conflict(
        "chengdu-resolved-history",
        "citation_checker",
        "route_agent",
        "已经解决",
        issue_type="route_edit_overreach",
    )
    store.resolve_conflict("chengdu-resolved-history", record.id)

    assert client.get("/api/projects/issue_type_stats").json() == []


def test_issue_type_stats_route_not_shadowed_by_project_id_route():
    """回归测试：`/issue_type_stats`必须在`/{project_id}`路由之前注册，否则会被误当成
    project_id去查找，返回404而不是聚合结果。"""
    resp = client.get("/api/projects/issue_type_stats")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_project_not_found():
    resp = client.get("/api/projects/does-not-exist")
    assert resp.status_code == 404


def test_get_project_detail_includes_progress():
    _seed_project("chengdu-v2", {"destination": "成都", "days": 2, "style": "人文", "budget": "经济"})

    resp = client.get("/api/projects/chengdu-v2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["destination"] == "成都"
    assert body["progress"]["state"] == "需求收集中"
    assert body["progress"]["skeleton_confirmed"] is False


def test_get_project_detail_includes_must_visit_and_style_interpretation():
    """docs/ARCHITECTURE.md第18节：管理后台只读展示，不需要编辑入口。"""
    _seed_project(
        "chengdu-v20",
        {
            "destination": "成都",
            "days": 3,
            "must_visit": [{"name": "熊猫基地", "day": 2}, {"name": "都江堰", "day": None}],
            "style_interpretation": {
                "poi_density": "偏少",
                "stay_duration": "偏多",
                "needs_detail_bias": None,
                "flexible_time": None,
            },
        },
    )

    resp = client.get("/api/projects/chengdu-v20")
    assert resp.status_code == 200
    body = resp.json()
    assert body["must_visit"] == [{"name": "熊猫基地", "day": 2}, {"name": "都江堰", "day": None}]
    assert body["style_interpretation"]["poi_density"] == "偏少"
    assert body["style_interpretation"]["needs_detail_bias"] is None


def test_get_project_detail_defaults_must_visit_and_style_interpretation_when_absent():
    """没有提供这两个字段时（草稿场景/CLI既有调用方），must_visit落空列表、
    style_interpretation落null，不应该报错或抛KeyError。"""
    _seed_project("chengdu-v21", {"destination": "成都", "days": 3})

    resp = client.get("/api/projects/chengdu-v21")
    assert resp.status_code == 200
    body = resp.json()
    assert body["must_visit"] == []
    assert body["style_interpretation"] is None


def test_get_project_outputs():
    _seed_project("chengdu-v3", {"destination": "成都", "days": 2})
    ws = store.workspace_dir("chengdu-v3")
    (ws / "route_plan.json").write_text('{"days": []}', encoding="utf-8")
    (ws / "route_map_validation.json").write_text(
        '{"status":"passed","days":[]}', encoding="utf-8"
    )
    (ws / "final_guide.md").write_text("# 攻略正文", encoding="utf-8")

    resp = client.get("/api/projects/chengdu-v3/outputs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["route_plan"] == {"days": []}
    assert body["route_map_validation"] == {"status": "passed", "days": []}
    assert body["route_feasibility"] is None
    assert body["final_guide"] == "# 攻略正文"
    assert body["final_guide_meta"] is None
    assert body["final_guide_blocks"] is None
    assert body["lifestyle"] is None
    assert body["pitfall"] is None
    assert body["poi_details"] == []
    assert body["evidence_checks"]["route"] == []
    assert body["evidence_checks"]["lifestyle"] == []


def test_get_project_outputs_excludes_checks_for_replaced_route_claims():
    project_id = "emeishan-stale-route-check"
    _seed_project(project_id, {"destination": "峨眉山", "days": 1})
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="峨眉山",
                    claims=[
                        {
                            "claim_id": "route-current",
                            "target_path": "notes",
                            "claim_text": "雷洞坪运营情况需出发前确认。",
                            "support_mode": "direct",
                        }
                    ],
                )
            ]
        ),
    )
    store.write_evidence_checks(
        project_id,
        "route",
        [
            {
                "target_key": "route:day:1:claim:route-old",
                "claim_id": "route-old",
                "scope": "entity_identity",
                "result": "fail",
            },
            {
                "target_key": "route:day:1:claim:route-current",
                "claim_id": "route-current",
                "scope": "substantive_claim",
                "result": "inconclusive",
            },
        ],
        replace_prefixes=["route:day:1"],
    )

    resp = client.get(f"/api/projects/{project_id}/outputs")

    assert resp.status_code == 200
    route_checks = resp.json()["evidence_checks"]["route"]
    assert [item["claim_id"] for item in route_checks] == ["route-current"]


def test_get_project_outputs_not_found():
    resp = client.get("/api/projects/does-not-exist/outputs")
    assert resp.status_code == 404


def test_get_project_outputs_returns_current_final_guide_blocks():
    _seed_project("chengdu-blocks", {"destination": "成都", "days": 1})
    store.write_final_guide_blocks(
        "chengdu-blocks",
        "integration_agent",
        [
            {
                "section": "攻略标题",
                "markdown": "# 成都一日攻略",
                "mapping_mode": "editorial",
                "mapping_note": "章节结构",
            }
        ],
        {"merged_fields": [], "checklist_included": False},
    )

    resp = client.get("/api/projects/chengdu-blocks/outputs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["final_guide_blocks"]["schema_version"] == "final-guide-blocks-v1"
    assert body["final_guide_blocks"]["blocks"][0]["section"] == "攻略标题"


def test_get_project_timeline_merges_and_sorts():
    _seed_project("chengdu-v4", {"destination": "成都", "days": 2})
    store.update_progress("chengdu-v4", "main_agent", state=ProjectPhase.SKELETON_CONFIRM)
    store.report_conflict("chengdu-v4", "citation_checker", "route_agent", "缺失依据")
    store.update_progress("chengdu-v4", "main_agent", state=ProjectPhase.PARALLEL_RESEARCH)

    resp = client.get("/api/projects/chengdu-v4/timeline")
    assert resp.status_code == 200
    events = resp.json()
    types_seen = {e["type"] for e in events}
    assert "write" in types_seen
    assert "conflict" in types_seen
    timestamps = [e["timestamp"] for e in events]
    assert timestamps == sorted(timestamps)


def test_rerun_not_found():
    resp = client.post("/api/projects/does-not-exist/rerun")
    assert resp.status_code == 404


def test_rerun_without_requirements_returns_400():
    store.init_project("no-req-project")
    resp = client.post("/api/projects/no-req-project/rerun")
    assert resp.status_code == 400


def test_rerun_success_returns_202(monkeypatch):
    _seed_project("chengdu-v5", {"destination": "成都", "days": 2})
    monkeypatch.setattr(jobs, "trigger_rerun", lambda pid: None)

    resp = client.post("/api/projects/chengdu-v5/rerun")
    assert resp.status_code == 202
    assert resp.json() == {"project_id": "chengdu-v5", "status": "running"}


def test_rerun_concurrent_returns_409(monkeypatch):
    _seed_project("chengdu-v6", {"destination": "成都", "days": 2})

    def already_running(pid):
        raise RuntimeError(f"project {pid!r} 已经在重跑中")

    monkeypatch.setattr(jobs, "trigger_rerun", already_running)

    resp = client.post("/api/projects/chengdu-v6/rerun")
    assert resp.status_code == 409


def test_rerun_status_not_found():
    resp = client.get("/api/projects/chengdu-v7/rerun/status")
    assert resp.status_code == 404


def test_rerun_status_returns_job_state():
    jobs._jobs["rerun:chengdu-v8"] = {
        "status": "succeeded",
        "error": None,
        "started_at": "t0",
        "finished_at": "t1",
        "log": [],
    }
    resp = client.get("/api/projects/chengdu-v8/rerun/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"


def test_run_status_not_found_for_unknown_project():
    resp = client.get("/api/projects/does-not-exist/run_status")
    assert resp.status_code == 404


def test_run_status_none_when_never_run():
    _seed_project("chengdu-v11", {"destination": "成都", "days": 2})
    resp = client.get("/api/projects/chengdu-v11/run_status")
    assert resp.status_code == 200
    assert resp.json() is None


def test_run_status_returns_written_status():
    _seed_project("chengdu-v12", {"destination": "成都", "days": 2})
    store.write_run_status("chengdu-v12", {"node": "route_draft", "status": "running", "call_count": 1})

    resp = client.get("/api/projects/chengdu-v12/run_status")
    assert resp.status_code == 200
    assert resp.json()["node"] == "route_draft"


# ---------------------------------------------------------------------------
# DELETE /api/projects/{project_id}：软删除（2026-08-01新增）
# ---------------------------------------------------------------------------


def test_delete_project_not_found_returns_404():
    resp = client.delete("/api/projects/does-not-exist")
    assert resp.status_code == 404


def test_delete_project_success_moves_to_trash_and_returns_200():
    _seed_project("chengdu-v15", {"destination": "成都", "days": 2})

    resp = client.delete("/api/projects/chengdu-v15")
    assert resp.status_code == 200
    assert resp.json() == {"project_id": "chengdu-v15", "status": "trashed"}
    assert "chengdu-v15" not in store.list_project_ids()
    assert store.is_trashed("chengdu-v15")


def test_delete_project_running_rerun_returns_409():
    _seed_project("chengdu-v16", {"destination": "成都", "days": 2})
    jobs._jobs["rerun:chengdu-v16"] = {
        "status": "running", "error": None, "started_at": "t0", "finished_at": None, "log": [],
    }

    resp = client.delete("/api/projects/chengdu-v16")
    assert resp.status_code == 409
    assert "chengdu-v16" in store.list_project_ids()


def test_delete_project_running_generate_site_returns_409():
    _seed_project("chengdu-v17", {"destination": "成都", "days": 2})
    jobs._jobs["generate_site:chengdu-v17"] = {
        "status": "running", "error": None, "started_at": "t0", "finished_at": None, "log": [],
    }

    resp = client.delete("/api/projects/chengdu-v17")
    assert resp.status_code == 409


def test_delete_project_unbinds_feishu_binding():
    _seed_project("feishu-chat-99", {"destination": "成都", "days": 2})
    bindings.get_or_create_binding("chat-99")
    assert bindings.get_binding("chat-99")["active_project_id"] == "feishu-chat-99"

    resp = client.delete("/api/projects/feishu-chat-99")
    assert resp.status_code == 200

    binding = bindings.get_binding("chat-99")
    assert binding["active_project_id"] is None
    assert binding["history"] == ["feishu-chat-99"]
