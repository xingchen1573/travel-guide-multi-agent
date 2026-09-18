from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.delivery import run_generate_site
from src.api.app import app
from src.review import store as review_store
from src.review.manifest_consistency import (
    build_manifest_consistency_context,
    execute_manifest_consistency_repair,
)
from src.workspace import delivery
from src.workspace import store as workspace_store


@pytest.fixture()
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "manifest-consistency-project"
    workspace_store.init_project(project_id)
    workspace_store.write_requirements(project_id, "测试目的地", {"destination": "测试目的地"})
    return project_id, workspace_store.workspace_dir(project_id)


def _detail(*, status: str = "passed") -> dict:
    return {
        "poi_name": "金顶",
        "day": 2,
        "citation_status": status,
        "human_review_required": status != "passed",
        "evidence_gap": False,
        "updated_at": "2026-09-02T01:27:56+00:00",
        "routes": [{
            "route_name": "核心线",
            "recommended": True,
            "route_segments": [{
                "order": 1,
                "point_name": "华藏寺",
                "highlight": "适合纳入礼佛主线。",
                "claims": [{
                    "claim_id": "poi-claim-current",
                    "target_path": "highlight",
                    "claim_text": "华藏寺适合纳入礼佛主线。",
                    "claim_type": "poi_detail",
                    "support_mode": "direct",
                }],
            }],
        }],
    }


def _seed_status_drift(workspace: Path, *, with_failure: bool, terminal_status: str | None = "force_finalized") -> None:
    poi_dir = workspace / "outputs" / "poi_details"
    poi_dir.mkdir(parents=True, exist_ok=True)
    (poi_dir / "day2_金顶.json").write_text(json.dumps(_detail(), ensure_ascii=False), encoding="utf-8")
    (poi_dir / "manifest.json").write_text(
        json.dumps({
            "generated": [
                {"day": 1, "poi_name": "峨眉山", "file": "day1_峨眉山.json", "citation_status": "passed"},
                {"day": 2, "poi_name": "金顶", "file": "day2_金顶.json", "citation_status": "force_finalized"},
            ],
            "updated_at": "2026-09-02T01:32:10+00:00",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    checks = []
    if with_failure:
        checks.append({
            "claim_id": "poi-claim-current",
            "claim_text": "华藏寺适合纳入礼佛主线。",
            "poi_name": "金顶",
            "target_path": "highlight",
            "scope": "substantive_claim",
            "result": "fail",
            "reason": "素材只支持地点身份，不支持礼佛主线的定性。",
            "checked_at": "2026-09-02T01:29:12+00:00",
            "observation_mode": "live",
            "evidence": [{"chunk_id": "source-1", "excerpt": "华藏寺是金顶的核心看点之一。"}],
        })
    (workspace / "evidence_checks.json").write_text(
        json.dumps({"poi_detail": checks, "route": [], "lifestyle": [], "pitfall": [], "history": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    if terminal_status:
        result_path = workspace.parent / "agents" / "codex_skill" / "poi_results.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"results": [{"day": 2, "poi_name": "金顶", "citation_status": terminal_status}]}, ensure_ascii=False),
            encoding="utf-8",
        )
    (workspace / "final_guide.md").write_text("# 攻略\n\n" + "金顶行程说明。" * 40, encoding="utf-8")
    (workspace / "final_guide.meta.json").write_text("{}", encoding="utf-8")


def _status_finding(project_id: str):
    review_store.sync_state_findings(project_id)
    return next(
        item
        for item in review_store.read_findings(project_id)
        if item.canonical_key == "manifest:status:day2_金顶.json" and item.status != "closed"
    )


def test_finalize_persists_terminal_status_to_detail_and_manifest(isolated_project, monkeypatch):
    project_id, workspace = isolated_project
    poi_dir = workspace / "outputs" / "poi_details"
    poi_dir.mkdir(parents=True, exist_ok=True)
    (poi_dir / "day2_金顶.json").write_text(json.dumps(_detail(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(delivery, "refresh_reference_section", lambda *_args: None)
    monkeypatch.setattr(delivery, "sync_poi_details_into_final_guide", lambda *_args: None)

    delivery.finalize_poi_details(
        project_id,
        "测试目的地",
        [{"day": 2, "poi_name": "金顶", "citation_status": "force_finalized"}],
    )

    detail = json.loads((poi_dir / "day2_金顶.json").read_text(encoding="utf-8"))
    manifest = json.loads((poi_dir / "manifest.json").read_text(encoding="utf-8"))
    assert detail["citation_status"] == "force_finalized"
    assert detail["human_review_required"] is True
    assert manifest["generated"][0]["citation_status"] == "force_finalized"


def test_finalize_validates_full_batch_before_persisting_any_status(isolated_project):
    project_id, workspace = isolated_project
    poi_dir = workspace / "outputs" / "poi_details"
    poi_dir.mkdir(parents=True, exist_ok=True)
    detail_path = poi_dir / "day2_金顶.json"
    detail_path.write_text(json.dumps(_detail(), ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown POI citation_status"):
        delivery.finalize_poi_details(
            project_id,
            "测试目的地",
            [
                {"day": 2, "poi_name": "金顶", "citation_status": "force_finalized"},
                {"day": 3, "poi_name": "万年寺", "citation_status": "unexpected"},
            ],
        )

    assert json.loads(detail_path.read_text(encoding="utf-8"))["citation_status"] == "passed"
    assert not (poi_dir / "manifest.json").exists()


def test_context_uses_exact_manifest_row_and_current_failure(isolated_project):
    project_id, workspace = isolated_project
    _seed_status_drift(workspace, with_failure=True)
    finding = _status_finding(project_id)

    context = build_manifest_consistency_context(project_id, finding)

    assert context is not None
    assert context["manifest_index"] == 1
    assert context["detail_file"] == "day2_金顶.json"
    assert context["manifest"]["status"] == "force_finalized"
    assert context["detail"]["status"] == "passed"
    assert context["authoritative_status"] == "force_finalized"
    assert context["authority_basis"] == "current_failed_checks"
    assert context["recommended_action"] == "sync_detail_status"
    assert context["current_blocker_count"] == 1
    assert context["executable"] is True
    assert finding.needs_rerun is False
    assert finding.repair_action == "reconcile_poi_status"


def test_context_refuses_sync_without_authoritative_evidence(isolated_project):
    project_id, workspace = isolated_project
    _seed_status_drift(workspace, with_failure=False, terminal_status=None)
    finding = _status_finding(project_id)

    context = build_manifest_consistency_context(project_id, finding)

    assert context is not None
    assert context["authoritative_status"] is None
    assert context["recommended_action"] == ""
    assert context["executable"] is False
    assert "核验证据" in context["blocking_reason"]


def test_legacy_terminal_result_is_audit_context_not_repair_authority(isolated_project):
    project_id, workspace = isolated_project
    _seed_status_drift(workspace, with_failure=False, terminal_status="force_finalized")
    finding = _status_finding(project_id)

    context = build_manifest_consistency_context(project_id, finding)

    assert context is not None
    assert context["terminal_result"]["status"] == "force_finalized"
    assert context["authoritative_status"] is None
    assert context["recommended_action"] == ""
    assert context["executable"] is False
    assert "审计线索" in context["blocking_reason"]


def test_current_live_pass_overrides_older_force_terminal(isolated_project):
    project_id, workspace = isolated_project
    _seed_status_drift(workspace, with_failure=False, terminal_status="force_finalized")
    (workspace / "evidence_checks.json").write_text(
        json.dumps({
            "poi_detail": [{
                "claim_id": "poi-claim-current",
                "claim_text": "华藏寺适合纳入礼佛主线。",
                "poi_name": "金顶",
                "target_path": "highlight",
                "scope": "substantive_claim",
                "result": "pass",
                "reason": "当前素材已实质支持。",
                "checked_at": "2026-09-02T02:00:00+00:00",
                "observation_mode": "live",
            }],
            "route": [], "lifestyle": [], "pitfall": [], "history": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    finding = _status_finding(project_id)

    context = build_manifest_consistency_context(project_id, finding)

    assert context is not None
    assert context["authoritative_status"] == "passed"
    assert context["authority_basis"] == "current_live_validation"
    assert context["recommended_action"] == "sync_manifest_status"
    assert context["executable"] is True


def test_execute_repairs_pair_and_scanner_closes_original_finding(isolated_project):
    project_id, workspace = isolated_project
    _seed_status_drift(workspace, with_failure=True)
    finding = _status_finding(project_id)
    context = build_manifest_consistency_context(project_id, finding)
    assert context is not None

    result = execute_manifest_consistency_repair(
        project_id,
        finding.finding_id,
        expected_fingerprint=context["observed_fingerprint"],
        site_builder=lambda _project_id: workspace / "site.html",
    )

    detail = json.loads((workspace / "outputs" / "poi_details" / "day2_金顶.json").read_text(encoding="utf-8"))
    manifest = json.loads((workspace / "outputs" / "poi_details" / "manifest.json").read_text(encoding="utf-8"))
    assert detail["citation_status"] == manifest["generated"][1]["citation_status"] == "force_finalized"
    assert result["finding"]["status"] == "closed"
    assert result["finding"]["verification_status"] == "passed_after_state_sync"
    review_store.sync_state_findings(project_id)
    matches = [item for item in review_store.read_findings(project_id) if item.canonical_key == finding.canonical_key]
    assert {item.finding_id for item in matches} == {finding.finding_id}
    assert matches[-1].status == "closed"


def test_execute_rejects_stale_fingerprint_without_overwriting(isolated_project):
    project_id, workspace = isolated_project
    _seed_status_drift(workspace, with_failure=True)
    finding = _status_finding(project_id)
    context = build_manifest_consistency_context(project_id, finding)
    assert context is not None
    detail_path = workspace / "outputs" / "poi_details" / "day2_金顶.json"
    changed = json.loads(detail_path.read_text(encoding="utf-8"))
    changed["routes"][0]["route_segments"][0]["highlight"] = "人工刚刚修改"
    detail_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="状态已变化"):
        execute_manifest_consistency_repair(
            project_id,
            finding.finding_id,
            expected_fingerprint=context["observed_fingerprint"],
            site_builder=lambda _project_id: workspace / "site.html",
        )

    assert json.loads(detail_path.read_text(encoding="utf-8"))["citation_status"] == "passed"


def test_api_exposes_preview_and_executes_deterministic_repair(isolated_project, monkeypatch):
    project_id, workspace = isolated_project
    _seed_status_drift(workspace, with_failure=True)
    finding = _status_finding(project_id)
    monkeypatch.setattr(run_generate_site, "generate_site", lambda _project_id: workspace / "site.html")
    client = TestClient(app)

    preview = client.get(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/context"
    )
    assert preview.status_code == 200
    context = preview.json()["manifest_consistency_context"]
    assert context["recommended_action"] == "sync_detail_status"
    assert context["authoritative_status"] == "force_finalized"

    repaired = client.post(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/deterministic-repair",
        json={"observed_fingerprint": context["observed_fingerprint"]},
    )
    assert repaired.status_code == 200
    assert repaired.json()["finding"]["status"] == "closed"
