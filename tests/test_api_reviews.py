from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routers import reviews as reviews_router
from src.review import store as review_store
from src.review.models import ReviewRecommendation, ReviewRun, TravelReviewSystemObservation
from src.workspace import store as workspace_store


def test_review_api_create_propose_apply_and_state(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-review-project"
    workspace_store.init_project(project_id)
    path = workspace_store.workspace_dir(project_id) / "outputs" / "lifestyle.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "food": {"items": [{"day": 1, "recommendation": "旧"}]},
                "outfit": {"items": []},
                "lodging": {"items": []},
                "play": {"items": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client = TestClient(app)
    finding = client.post(
        f"/api/projects/{project_id}/review-findings",
        json={"problem_type": "content", "target": {"file": "outputs/lifestyle.json"}},
    )
    assert finding.status_code == 200
    finding_id = finding.json()["finding_id"]

    proposed = client.post(
        f"/api/projects/{project_id}/review-patches",
        json={
            "finding_id": finding_id,
            "file": "outputs/lifestyle.json",
            "operations":[
                {"op": "replace", "path": "/food/items/0/recommendation", "value": "新"}
            ],
        },
    )
    assert proposed.status_code == 200
    patch_id = proposed.json()["patch_id"]

    applied = client.post(f"/api/projects/{project_id}/review-patches/{patch_id}/apply", json={})
    assert applied.status_code == 200
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "新"

    state = client.get(f"/api/projects/{project_id}/review-state")
    assert state.status_code == 200
    assert state.json()["ok"] is True

    cascade = client.post(
        f"/api/projects/{project_id}/review-cascade/preview",
        json={"file": "outputs/lifestyle.json", "target": {"day": 1}},
    )
    assert cascade.status_code == 200
    assert cascade.json()["impact_class"] == "lifestyle"


def test_review_api_starts_one_pending_patch_validation_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-review-validation-project"
    workspace_store.init_project(project_id)
    path = workspace_store.workspace_dir(project_id) / "outputs" / "lifestyle.json"
    path.write_text(
        json.dumps({
            "food": {"items": [{"day": 1, "recommendation": "旧文案"}]},
            "outfit": {"items": []},
            "lodging": {"items": []},
            "play": {"items": []},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[{"op": "replace", "path": "/food/items/0/recommendation", "value": "新文案"}],
    )
    review_store.apply_patch(project_id, patch.patch_id)
    triggered: list[str] = []
    monkeypatch.setattr(reviews_router.jobs, "trigger_review_validation", lambda value: triggered.append(value))
    client = TestClient(app)

    preview = client.get(f"/api/projects/{project_id}/review-validation/preview")
    started = client.post(f"/api/projects/{project_id}/review-validation/start")

    assert preview.status_code == 200
    assert preview.json()["patch_count"] == 1
    assert preview.json()["lifestyle_items"] == [["food", 1]]
    assert started.status_code == 202
    assert triggered == [project_id]


def test_review_run_rejects_deterministic_manifest_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-deterministic-review-project"
    workspace_store.init_project(project_id)
    manifest = workspace_store.workspace_dir(project_id) / "outputs" / "poi_details" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"generated": [{"file": "missing.json"}]}), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="manifest_mismatch",
        target={"file": "outputs/poi_details/manifest.json", "json_path": "/generated/0"},
    )
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/runs",
        json={"mode": "standard", "channel": "cunai"},
    )

    assert response.status_code == 409
    assert "确定性一致性问题" in response.json()["detail"]


def test_review_api_exposes_state_residue_as_actionable_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-state-review-project"
    workspace_store.init_project(project_id)
    poi_dir = workspace_store.workspace_dir(project_id) / "outputs" / "poi_details"
    poi_dir.mkdir(parents=True, exist_ok=True)
    (poi_dir / "manifest.json").write_text(json.dumps({"generated": [{"file": "day3_missing.json", "citation_status": "generation_failed"}]}), encoding="utf-8")
    client = TestClient(app)
    findings = client.get(f"/api/projects/{project_id}/review-findings")
    assert findings.status_code == 200
    body = findings.json()
    assert body[0]["problem_type"] == "manifest_mismatch"
    assert body[0]["repair_action"] == "targeted_poi_detail_regeneration"
    assert body[0]["disposition"] == "needs_rerun"


def test_review_context_api_exposes_deduplicated_missing_entity_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-missing-entity-context-project"
    workspace_store.init_project(project_id)
    route_path = workspace_store.workspace_dir(project_id) / "route_plan.json"
    route_path.write_text(
        json.dumps({
            "updated_at": "2026-09-01T13:07:00+00:00",
            "days": [{
                "day": 2,
                "citation_status": "force_finalized",
                "missing_entities": ["十方普贤圣像", "华藏寺", "十方普贤圣像"],
                "pois": [{
                    "name": "金顶",
                    "brief": "可观看十方普贤圣像。",
                    "needs_detail_reason": "含华藏寺、十方普贤圣像等景观。",
                }],
                "claims": [],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    client = TestClient(app)

    findings_response = client.get(f"/api/projects/{project_id}/review-findings")
    assert findings_response.status_code == 200
    finding = next(
        item for item in findings_response.json()
        if item.get("canonical_key") == "route_day:2"
    )
    context_response = client.get(
        f"/api/projects/{project_id}/review-findings/{finding['finding_id']}/context"
    )

    assert context_response.status_code == 200
    missing = context_response.json()["missing_entity_context"]
    assert missing["schema_version"] == "missing-entity-review-v1"
    assert missing["entity_count"] == 2
    assert missing["occurrence_count"] == 3
    assert missing["project_impact"]["review_required"] is True
    assert missing["project_impact"]["affects_project_state"] is True
    assert missing["project_impact"]["generation_blocked"] is False


def test_improvement_signal_rejects_pair_that_does_not_contain_project(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-unbound-pair-project"
    workspace_store.init_project(project_id)
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        root_cause="evidence",
        target={"file": "outputs/poi_details/day1.json"},
    )
    observation = TravelReviewSystemObservation(
        root_cause="evidence",
        problem_type="reference_attachment_mismatch",
        cause_key="citation:reference_attachment_mismatch",
        affected_layer="evidence_pipeline",
        summary="引用证据与正文不匹配。",
        severity="medium",
        confidence="high",
        is_systemic=True,
    )
    signal = review_store.append_improvement_signals(
        project_id,
        finding_id=finding.finding_id,
        recommendation_id="recommendation-unbound-pair",
        observations=[observation],
    )[0]
    monkeypatch.setattr(
        reviews_router.experiment_index,
        "get_comparison_pair",
        lambda pair_id: {
            "pair_id": pair_id,
            "status": "complete",
            "langgraph_project_id": "different-langgraph-project",
            "codex_project_id": "different-codex-project",
        },
    )

    response = TestClient(app).post(
        f"/api/projects/{project_id}/improvement-signals/{signal.signal_id}/decision",
        json={"decision": "accepted", "comparison_pair_id": "pair-other-v1"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Signal Project 不属于所选 Comparison Pair"


def test_improvement_signal_rejects_incomplete_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-incomplete-pair-project"
    workspace_store.init_project(project_id)
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        root_cause="evidence",
        target={"file": "outputs/poi_details/day1.json"},
    )
    observation = TravelReviewSystemObservation(
        root_cause="evidence",
        problem_type="reference_attachment_mismatch",
        cause_key="citation:reference_attachment_mismatch",
        affected_layer="evidence_pipeline",
        summary="引用证据与正文不匹配。",
        severity="medium",
        confidence="high",
        is_systemic=True,
    )
    signal = review_store.append_improvement_signals(
        project_id,
        finding_id=finding.finding_id,
        recommendation_id="recommendation-incomplete-pair",
        observations=[observation],
    )[0]
    monkeypatch.setattr(
        reviews_router.experiment_index,
        "get_comparison_pair",
        lambda pair_id: {
            "pair_id": pair_id,
            "status": "incomplete",
            "langgraph_project_id": project_id,
            "codex_project_id": "missing-codex-project",
        },
    )

    response = TestClient(app).post(
        f"/api/projects/{project_id}/improvement-signals/{signal.signal_id}/decision",
        json={"decision": "accepted", "comparison_pair_id": "pair-incomplete-v1"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Comparison Pair 尚未完整，不能作为反哺来源"


def test_improvement_signal_triage_builds_stable_global_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-improvement-project"
    workspace_store.init_project(project_id)
    observation = TravelReviewSystemObservation(
        root_cause="evidence",
        problem_type="reference_attachment_mismatch",
        cause_key="citation:reference_attachment_mismatch",
        affected_layer="evidence_pipeline",
        summary="引用失败后没有继续检索候选素材。",
        evidence_refs=["chunk-1"],
        suggested_fix="补充目的地范围的候选素材检索。",
        suggested_test="引用失配但候选命中时应修复引用。",
        severity="high",
        confidence="high",
        is_systemic=True,
    )
    signal_ids = []
    for index in range(2):
        finding = review_store.create_finding(
            project_id,
            problem_type="force_finalized_residue",
            root_cause="evidence",
            target={"file": f"outputs/poi_details/day{index + 1}.json"},
        )
        finding = review_store.update_finding(
            project_id,
            finding.finding_id,
            disposition="needs_rerun",
            status="open",
        )
        signal = review_store.append_improvement_signals(
            project_id,
            finding_id=finding.finding_id,
            recommendation_id=f"recommendation-signal-{index}",
            observations=[observation],
        )[0]
        signal_ids.append(signal.signal_id)

    client = TestClient(app)
    pending = client.get(f"/api/projects/{project_id}/improvement-signals", params={"decision": "pending"})
    assert pending.status_code == 200
    assert len(pending.json()) == 2

    for signal_id in signal_ids:
        accepted = client.post(
            f"/api/projects/{project_id}/improvement-signals/{signal_id}/decision",
            json={"decision": "accepted", "reason": "人工确认是同一证据链路问题", "actor_id": "tester"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["decision"] == "accepted"

    candidates = client.get("/api/review-index/optimization-candidates")
    assert candidates.status_code == 200
    assert len(candidates.json()) == 1
    candidate = candidates.json()[0]
    assert candidate["cause_key"] == "citation:reference_attachment_mismatch"
    assert candidate["active_count"] == 2
    candidate_id = candidate["candidate_id"]

    triaged = client.patch(
        f"/api/review-index/optimization-candidates/{candidate_id}",
        json={"status": "triaged", "owner": "quality-team", "notes": "确认进入工程计划"},
    )
    assert triaged.status_code == 200
    updated = client.patch(
        f"/api/review-index/optimization-candidates/{candidate_id}",
        json={"status": "planned", "owner": "quality-team", "notes": "先补回归测试"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "planned"
    rebuilt = client.get("/api/review-index/optimization-candidates").json()[0]
    assert rebuilt["candidate_id"] == candidate_id
    assert rebuilt["status"] == "planned"
    assert rebuilt["owner"] == "quality-team"

    implementing = client.patch(
        f"/api/review-index/optimization-candidates/{candidate_id}",
        json={"status": "implementing", "owner": "quality-team"},
    )
    assert implementing.status_code == 200
    missing_regression = client.patch(
        f"/api/review-index/optimization-candidates/{candidate_id}",
        json={"status": "monitoring", "owner": "quality-team"},
    )
    assert missing_regression.status_code == 400
    assert "成功 Regression Run" in missing_regression.json()["detail"]

    regression_cases = client.get(
        f"/api/review-index/optimization-candidates/{candidate_id}/regression-cases"
    )
    assert regression_cases.status_code == 200
    assert sum(item["case_type"] == "original_finding" for item in regression_cases.json()) == 2
    assert any(item["case_type"] == "unit_test" and item["status"] == "draft" for item in regression_cases.json())

    bound_test = client.post(
        f"/api/review-index/optimization-candidates/{candidate_id}/regression-cases",
        json={
            "case_type": "unit_test",
            "label": "候选聚合回归",
            "test_selector": "tests/test_review_loop.py::test_repeated_root_cause_creates_optimization_candidate",
        },
    )
    assert bound_test.status_code == 200
    assert bound_test.json()["status"] == "ready"
    unsafe_test = client.post(
        f"/api/review-index/optimization-candidates/{candidate_id}/regression-cases",
        json={
            "case_type": "unit_test",
            "label": "unsafe",
            "test_selector": "../tests/test_review_loop.py",
        },
    )
    assert unsafe_test.status_code == 400

    invalid_verified = client.patch(
        f"/api/review-index/optimization-candidates/{candidate_id}",
        json={"status": "verified"},
    )
    assert invalid_verified.status_code == 400
    assert "不允许" in invalid_verified.json()["detail"]

    metrics = client.get("/api/review-index/quality-metrics")
    assert metrics.status_code == 200
    assert metrics.json()["accepted_signal_count"] == 2
    assert metrics.json()["candidate_count"] == 1


def test_project_timeline_and_summary_include_current_review_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-review-projection-project"
    workspace_store.init_project(project_id)
    client = TestClient(app)
    created = client.post(
        f"/api/projects/{project_id}/review-findings",
        json={"problem_type": "content", "target": {"file": "outputs/route_plan.json"}},
    )
    assert created.status_code == 200
    finding_id = created.json()["finding_id"]
    summary = client.get(f"/api/projects/{project_id}").json()
    assert summary["review_findings_total"] == 1
    assert summary["review_findings_open"] == 1
    timeline = client.get(f"/api/projects/{project_id}/timeline").json()
    projected = [event for event in timeline if event["type"] == "review_finding" and event["finding_id"] == finding_id]
    assert projected and projected[0]["status"] == "open"
    verified = client.post(
        f"/api/projects/{project_id}/review-findings/{finding_id}/verification",
        json={
            "verification_status": "passed_with_review",
            "verification_method": "test",
            "disposition": "resolved",
            "actor_type": "human",
            "actor_id": "tester",
        },
    )
    assert verified.status_code == 200
    summary_after = client.get(f"/api/projects/{project_id}").json()
    assert summary_after["review_findings_open"] == 0
    timeline_after = client.get(f"/api/projects/{project_id}/timeline").json()
    current = [event for event in timeline_after if event["type"] == "review_finding" and event["finding_id"] == finding_id]
    assert current and current[0]["status"] == "closed"
    assert any(event.get("action") == "review_verification" for event in timeline_after)


def test_review_api_records_item_decision_without_closing_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-item-review-project"
    workspace_store.init_project(project_id)
    finding = review_store.create_finding(project_id, problem_type="content", target={"file": "outputs/demo.json"})
    recommendation = ReviewRecommendation(
        recommendation_id="recommendation-api-item",
        finding_id=finding.finding_id,
        claim_reviews=[{
            "claim": "名称核验",
            "original_text": "旧名称",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "素材中的正式名称",
            "verdict": "contradicted",
            "action": "narrow",
            "rationale": "名称不一致",
        }],
    )
    review_store.append_recommendation(project_id, recommendation)
    client = TestClient(app)
    response = client.post(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/item-decisions",
        json={
            "recommendation_id": recommendation.recommendation_id,
            "item_key": "claim:0",
            "item_index": 0,
            "item_kind": "claim",
            "item_label": "名称核验",
            "disposition": "keep_original",
        },
    )
    assert response.status_code == 200
    decisions = client.get(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/item-decisions",
        params={"recommendation_id": recommendation.recommendation_id},
    )
    assert decisions.status_code == 200
    assert decisions.json()[0]["disposition"] == "keep_original"
    assert review_store.get_finding(project_id, finding.finding_id).status == "in_review"


def test_review_run_codex_bridge_keeps_candidate_until_promoted(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-review-run-project"
    workspace_store.init_project(project_id)
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城", "days": 1})
    output = workspace_store.workspace_dir(project_id) / "outputs" / "demo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"name": "旧名称"}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="content",
        target={"file": "outputs/demo.json", "json_path": "/name"},
    )
    client = TestClient(app)

    executors = client.get("/api/review-executors")
    assert executors.status_code == 200
    assert {item["executor_id"] for item in executors.json()} == {"standard_model", "codex_skill"}

    created = client.post(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/runs",
        json={"mode": "codex"},
    )
    assert created.status_code == 200
    run = created.json()[0]
    assert run["status"] == "waiting_user"
    bundle = client.get(f"/api/projects/{project_id}/review-runs/{run['run_id']}/bundle").json()
    scope = bundle["scope"][0]
    result = {
        "schema_version": "travel-review-result-v1",
        "bundle_sha256": bundle["bundle_sha256"],
        "recommendation": {
            "recommendation_id": "recommendation-draft",
            "finding_id": finding.finding_id,
            "summary": "当前信息不足，保留原文并交人工判断。",
            "claim_reviews": [{
                "claim": "名称核验",
                "target_path": scope["target_path"],
                "original_text": scope["original_text"],
                "evidence_refs": ["material_search:示例城"],
                "evidence_excerpt": "未找到可读的直接证据。",
                "verdict": "partial",
                "action": "keep",
                "content_action": "keep",
                "evidence_action": "keep",
                "proposed_text": "保持原文，等待补充证据。",
                "rationale": "当前冻结包没有支持或反驳名称的可读来源。",
                "confidence": "low",
            }],
            "evidence_refs": ["material_search:示例城"],
            "recommended_action": "human_review",
            "model_name": "codex-shadow-test",
        },
        "system_observations": [],
        "proposed_operations": [],
        "review_trace": {
            "mode": "bundle",
            "status": "completed",
            "model_name": "codex-shadow-test",
            "evidence_reads": [{
                "ref": "material_search:示例城",
                "excerpt": "未找到可读的直接证据。",
                "source_type": "material_search",
            }],
            "errors": [],
        },
    }
    imported = client.post(
        f"/api/projects/{project_id}/review-runs/{run['run_id']}/result",
        json={"result": result},
    )
    assert imported.status_code == 200
    completed = imported.json()
    assert completed["status"] == "succeeded"
    recommendation_id = completed["recommendation_id"]
    assert recommendation_id
    assert review_store.get_finding(project_id, finding.finding_id).recommendation_id is None

    promoted = client.post(
        f"/api/projects/{project_id}/review-runs/{run['run_id']}/promote",
        json={"actor_id": "tester"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["recommendation_id"] == recommendation_id


def test_review_compare_runs_share_frozen_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-review-compare-project"
    workspace_store.init_project(project_id)
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城", "days": 1})
    output = workspace_store.workspace_dir(project_id) / "outputs" / "demo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"name": "示例"}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="content",
        target={"file": "outputs/demo.json", "json_path": "/name"},
    )

    monkeypatch.setattr("src.review.executors._run_standard", lambda project_id, run_id: None)
    client = TestClient(app)
    response = client.post(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/runs",
        json={"mode": "compare"},
    )
    assert response.status_code == 200
    runs = response.json()
    assert len(runs) == 2
    assert len({run["bundle_sha256"] for run in runs}) == 1
    assert len({run["comparison_group_id"] for run in runs}) == 1
    standard_run = next(run for run in runs if run["executor_id"] == "standard_model")
    assert standard_run["channel"] == "gptload"


def test_promote_requires_confirmation_when_current_recommendation_has_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-review-promotion-confirmation"
    workspace_store.init_project(project_id)
    output = workspace_store.workspace_dir(project_id) / "outputs" / "demo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"name": "当前名称"}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(project_id, problem_type="content", target={"file": "outputs/demo.json"})
    current = ReviewRecommendation(
        recommendation_id="recommendation-current-with-decision",
        finding_id=finding.finding_id,
        claim_reviews=[{
            "claim": "名称核验",
            "original_text": "当前名称",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "当前名称",
            "verdict": "supported",
            "action": "keep",
            "rationale": "已有证据",
        }],
    )
    review_store.append_recommendation(project_id, current)
    review_store.record_item_decision(
        project_id,
        finding.finding_id,
        current.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="名称核验",
        disposition="keep_original",
    )
    candidate = ReviewRecommendation(
        recommendation_id="recommendation-new-candidate",
        finding_id=finding.finding_id,
        summary="新的候选建议",
    )
    review_store.append_recommendation(project_id, candidate, promote=False)
    baseline = review_store.get_finding(project_id, finding.finding_id)
    bundle_path = workspace_store.workspace_dir(project_id) / "review_runs" / "_executions" / "review-run-confirm" / "review_bundle.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps({"baseline": {"file": "outputs/demo.json"}}), encoding="utf-8")
    review_store.save_review_run(project_id, ReviewRun(
        run_id="review-run-confirm",
        project_id=project_id,
        finding_id=finding.finding_id,
        executor_id="codex_skill",
        status="succeeded",
        recommendation_id=candidate.recommendation_id,
        baseline_file_sha256=review_store.file_sha256(output),
        baseline_finding_revision=baseline.revision,
        artifact_paths={"bundle": str(bundle_path.relative_to(workspace_store.workspace_dir(project_id)))},
    ))

    client = TestClient(app)
    blocked = client.post(
        f"/api/projects/{project_id}/review-runs/review-run-confirm/promote",
        json={"actor_id": "tester"},
    )
    assert blocked.status_code == 409
    assert "replace_confirmation_required" in blocked.json()["detail"]
    promoted = client.post(
        f"/api/projects/{project_id}/review-runs/review-run-confirm/promote",
        json={"actor_id": "tester", "confirm_replace_decisions": True},
    )
    assert promoted.status_code == 200
    assert promoted.json()["recommendation_id"] == candidate.recommendation_id
    assert review_store.read_item_decisions(project_id, current.recommendation_id)[0].disposition == "keep_original"

    history = client.get(
        f"/api/projects/{project_id}/review-findings/{finding.finding_id}/recommendations"
    )
    assert history.status_code == 200
    entries = {item["recommendation"]["recommendation_id"]: item for item in history.json()}
    assert entries[candidate.recommendation_id]["is_current"] is True
    assert entries[current.recommendation_id]["decision_count"] == 1
    assert entries[current.recommendation_id]["lifecycle_status"] == "superseded"


def test_promote_rejects_stale_file_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "api-review-stale-promotion"
    workspace_store.init_project(project_id)
    output = workspace_store.workspace_dir(project_id) / "outputs" / "demo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"name": "旧名称"}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(project_id, problem_type="content", target={"file": "outputs/demo.json"})
    candidate = ReviewRecommendation(recommendation_id="recommendation-stale", finding_id=finding.finding_id)
    review_store.append_recommendation(project_id, candidate, promote=False)
    bundle_path = workspace_store.workspace_dir(project_id) / "review_runs" / "_executions" / "review-run-stale" / "review_bundle.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps({"baseline": {"file": "outputs/demo.json"}}), encoding="utf-8")
    review_store.save_review_run(project_id, ReviewRun(
        run_id="review-run-stale",
        project_id=project_id,
        finding_id=finding.finding_id,
        executor_id="codex_skill",
        status="succeeded",
        recommendation_id=candidate.recommendation_id,
        baseline_file_sha256=review_store.file_sha256(output),
        baseline_finding_revision=finding.revision,
        artifact_paths={"bundle": str(bundle_path.relative_to(workspace_store.workspace_dir(project_id)))},
    ))
    output.write_text(json.dumps({"name": "其他人已修改"}, ensure_ascii=False), encoding="utf-8")

    response = TestClient(app).post(
        f"/api/projects/{project_id}/review-runs/review-run-stale/promote",
        json={"actor_id": "tester"},
    )
    assert response.status_code == 409
    assert "stale_candidate" in response.json()["detail"]
