from __future__ import annotations

import json

import pytest

from src.review import agent as review_agent
from src.review import skill_bridge
from src.review import store as review_store
from src.workspace import store as workspace_store


@pytest.fixture()
def isolated_skill_project(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    material_root = tmp_path / "material_library"
    monkeypatch.setattr(review_agent, "MATERIAL_LIBRARY_DIR", material_root)
    review_store._LOCKS.clear()
    project_id = "travel-review-skill-project"
    workspace_store.init_project(project_id)
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城", "days": 3})

    chunks_dir = material_root / "index" / "示例城"
    chunks_dir.mkdir(parents=True)
    rows = [
        {"chunk_id": "示例-错误-c001", "doc_id": "示例-错误", "text": "这里只介绍停车场和售票处。"},
        {"chunk_id": "示例-正确-c004", "doc_id": "示例-正确", "text": "沿滨海木栈道步行，可以抵达海边观景平台。"},
    ]
    (chunks_dir / "chunks.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )

    detail_dir = workspace_store.workspace_dir(project_id) / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True)
    content = {
        "poi_name": "示例景区",
        "citation_status": "force_finalized",
        "routes": [{
            "route_name": "路线A",
            "route_segments": [{
                "point_name": "滨海木栈道",
                "highlight": "沿木栈道前往观景平台",
                "suggested_duration": "约20分钟",
                "sources": [{"chunk_id": "示例-错误-c001"}],
            }],
        }],
    }
    target = detail_dir / "day1_demo.json"
    target.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day1_demo.json"},
    )
    review_store.update_finding(
        project_id,
        finding.finding_id,
        issue_summary="第1轮：路线A途经点「滨海木栈道」：当前引用没有对应内容。",
        observed_value=content,
    )
    return project_id, review_store.get_finding(project_id, finding.finding_id), target


def _valid_result(bundle):
    item = bundle["scope"][0]
    return {
        "schema_version": "travel-review-result-v1",
        "bundle_sha256": bundle["bundle_sha256"],
        "recommendation": {
            "recommendation_id": "recommendation-draft",
            "finding_id": bundle["finding"]["finding_id"],
            "summary": "当前引用挂错，改挂实际支持观景平台的素材。",
            "claim_reviews": [{
                "claim": "滨海木栈道可前往观景平台",
                "target_path": item["target_path"],
                "original_text": item["original_text"],
                "evidence_refs": ["示例-错误-c001", "示例-正确-c004"],
                "evidence_excerpt": "沿滨海木栈道步行，可以抵达海边观景平台。",
                "verdict": "contradicted",
                "action": "repair_reference",
                "content_action": "keep",
                "evidence_action": "repair_reference",
                "proposed_text": "保留当前描述，来源改为示例-正确-c004。",
                "rationale": "当前引用只介绍停车场，其他实际读取的素材直接支持观景平台。",
                "confidence": "high",
            }],
            "evidence_refs": ["示例-错误-c001", "示例-正确-c004"],
            "recommended_action": "human_review",
            "model_name": "test-strong-model",
        },
        "system_observations": [],
        "proposed_operations": [],
        "review_trace": {
            "mode": "bundle",
            "status": "completed",
            "model_name": "test-strong-model",
            "evidence_reads": [],
            "errors": [],
        },
    }


def test_export_bundle_recovers_scope_and_destination_candidates(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)

    assert bundle["schema_version"] == "travel-review-bundle-v1"
    assert len(bundle["scope"]) == 1
    item = bundle["scope"][0]
    assert item["target_path"] == "/routes/0/route_segments/0"
    assert item["direct_evidence"][0]["ref"] == "示例-错误-c001"
    assert item["alternative_candidates"][0]["ref"] == "示例-正确-c004"
    assert set(bundle["evidence"]["allowed_refs"]) == {"示例-错误-c001", "示例-正确-c004"}


def test_export_bundle_rejects_deterministic_finding(isolated_skill_project):
    project_id, _finding, _target = isolated_skill_project
    finding = review_store.create_finding(
        project_id,
        problem_type="manifest_mismatch",
        target={"file": "outputs/poi_details/manifest.json", "json_path": "/generated/0"},
    )

    with pytest.raises(ValueError, match="确定性一致性问题"):
        skill_bridge.export_review_bundle(project_id, finding.finding_id)


def test_validate_result_rejects_missing_scope_claim(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    result["recommendation"]["claim_reviews"] = []

    with pytest.raises(ValueError, match="schema 校验失败|claim 数量"):
        skill_bridge.validate_review_result(bundle, result)


def test_validate_result_rejects_unread_evidence(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    result["recommendation"]["claim_reviews"][0]["evidence_refs"] = ["不存在-c999"]

    with pytest.raises(ValueError, match="未读取证据"):
        skill_bridge.validate_review_result(bundle, result)


def test_validate_result_rejects_failed_model_run(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    result["review_trace"]["status"] = "failed"
    result["review_trace"]["errors"] = ["504 Gateway Timeout"]

    with pytest.raises(ValueError, match="模型运行未成功完成"):
        skill_bridge.validate_review_result(bundle, result)


def test_validate_result_rejects_duration_kept_after_missing_evidence(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    claim = result["recommendation"]["claim_reviews"][0]
    claim.update({
        "verdict": "partial",
        "action": "narrow",
        "proposed_text": "建议停留约20分钟。",
        "rationale": "地点有依据，但没有提供具体时长。",
    })

    with pytest.raises(ValueError, match="仍保留相同固定时长"):
        skill_bridge.validate_review_result(bundle, result)


def test_validate_result_accepts_compound_content_and_evidence_actions(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    claim = result["recommendation"]["claim_reviews"][0]
    claim.update({
        "verdict": "partial",
        "action": "narrow",
        "content_action": "narrow",
        "evidence_action": "repair_reference",
        "proposed_text": "沿滨海木栈道前往观景平台，停留时间视现场安排；来源改为示例-正确-c004。",
        "rationale": "地点由候选素材支持，当前引用挂错，原固定时长没有提供具体证据。",
    })

    validated = skill_bridge.validate_review_result(bundle, result)

    assert validated.recommendation.claim_reviews[0].content_action == "narrow"
    assert validated.recommendation.claim_reviews[0].evidence_action == "repair_reference"


def test_validate_result_rejects_conflicting_content_action(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    result["recommendation"]["claim_reviews"][0].update({
        "action": "narrow",
        "content_action": "keep",
    })

    with pytest.raises(ValueError, match="content_action=keep"):
        skill_bridge.validate_review_result(bundle, result)


def test_validate_result_rejects_keep_action_that_cancels_duration(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    claim = result["recommendation"]["claim_reviews"][0]
    claim.update({
        "action": "repair_reference",
        "content_action": "keep",
        "evidence_action": "repair_reference",
        "proposed_text": "保持原文，仅修正引用。",
        "rationale": "地点有依据，但需要取消固定时长并修复来源。",
    })

    with pytest.raises(ValueError, match="要求修改正文"):
        skill_bridge.validate_review_result(bundle, result)


def test_import_result_appends_recommendation_without_touching_content(isolated_skill_project):
    project_id, finding, target = isolated_skill_project
    before = target.read_bytes()
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)

    recommendation = skill_bridge.import_review_result(project_id, bundle, _valid_result(bundle))

    assert recommendation.recommendation_id != "recommendation-draft"
    assert recommendation.context_version == "travel-review-skill-v1"
    assert target.read_bytes() == before
    artifact = (
        workspace_store.workspace_dir(project_id)
        / "review_runs"
        / recommendation.recommendation_id
        / "travel_review_result.json"
    )
    assert artifact.exists()


def test_import_result_creates_pending_improvement_signal(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    result["system_observations"] = [{
        "root_cause": "evidence",
        "problem_type": "reference_attachment_mismatch",
        "cause_key": "citation:reference_attachment_mismatch",
        "affected_layer": "evidence_pipeline",
        "summary": "当前引用失败后没有继续检索同目的地候选素材。",
        "evidence_refs": ["示例-错误-c001", "示例-正确-c004"],
        "representative_targets": [bundle["scope"][0]["target_path"]],
        "suggested_fix": "在判定不支持前继续检索同目的地候选素材。",
        "suggested_test": "当前引用失配但候选素材命中时应建议修复引用。",
        "severity": "high",
        "confidence": "high",
        "regression_scope": [finding.finding_id],
        "is_systemic": True,
    }]

    recommendation = skill_bridge.import_review_result(
        project_id,
        bundle,
        result,
        review_run_id="review-run-test",
        source_type="codex_skill",
    )

    signals = review_store.read_improvement_signals(project_id)
    assert len(signals) == 1
    assert signals[0].recommendation_id == recommendation.recommendation_id
    assert signals[0].review_run_id == "review-run-test"
    assert signals[0].cause_key == "citation:reference_attachment_mismatch"
    assert signals[0].decision == "pending"


def test_validate_result_rejects_system_observation_with_unread_evidence(isolated_skill_project):
    project_id, finding, _target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    result = _valid_result(bundle)
    result["system_observations"] = [{
        "root_cause": "evidence",
        "problem_type": "reference_attachment_mismatch",
        "cause_key": "citation:reference_attachment_mismatch",
        "summary": "引用失配。",
        "evidence_refs": ["没有读取过的引用"],
        "suggested_test": "引用必须可追溯。",
        "severity": "high",
    }]

    with pytest.raises(ValueError, match="system observation.*未读取证据"):
        skill_bridge.validate_review_result(bundle, result)


def test_import_result_rejects_changed_baseline(isolated_skill_project):
    project_id, finding, target = isolated_skill_project
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    data = json.loads(target.read_text(encoding="utf-8"))
    data["poi_name"] = "其他人刚刚修改"
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="baseline 已变化"):
        skill_bridge.import_review_result(project_id, bundle, _valid_result(bundle))
