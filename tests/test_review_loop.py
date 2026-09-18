from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.review import store as review_store
from src.review import advisor as review_advisor
from src.review import policy as review_policy
from src.review import validation as review_validation
from src.review.models import ReviewFinding, ReviewRecommendation
from src.workspace import store as workspace_store
from src.workspace.final_guide_sections import POI_SECTION_HEADING, REFERENCE_SECTION_HEADING


@pytest.fixture()
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "review-project"
    workspace_store.init_project(project_id)
    return project_id, workspace_store.workspace_dir(project_id)


def _write_lifestyle(workspace, text="旧文案"):
    path = workspace / "outputs" / "lifestyle.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "food": {"items": [{"day": 1, "recommendation": text}]},
                "outfit": {"items": []},
                "lodging": {"items": []},
                "play": {"items": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_patch_first_apply_and_audit(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
        root_cause="content",
        severity="low",
        before="旧文案",
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[
            {
                "op": "replace",
                "path": "/food/items/0/recommendation",
                "value": "新文案",
            }
        ],
        actor_type="model",
        actor_id="test-model",
    )
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "旧文案"

    review_store.apply_patch(project_id, patch.patch_id, actor_type="human", actor_id="admin_user")
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "新文案"
    assert review_store.read_patch(project_id, patch.patch_id).status == "applied"
    assert review_store.read_patch(project_id, patch.patch_id).impact_class == "lifestyle"
    assert review_store.read_patch(project_id, patch.patch_id).cascade_plan["needs_human_approval"] is True
    assert review_store.read_patch(project_id, patch.patch_id).cascade_plan["execution_status"] == "pending_integration"
    updated_finding = review_store.get_finding(project_id, finding.finding_id)
    assert updated_finding.disposition == "deferred"
    assert updated_finding.status == "verifying"
    assert updated_finding.verification_status == "pending_integration_review"
    audit = workspace_store.read_workspace(project_id, "review_log.jsonl")
    assert any(item.get("actor_type") == "human" and item.get("action") == "apply_patch" for item in audit)


def test_patch_conflict_does_not_overwrite_new_source(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="content",
        target={"file": "outputs/lifestyle.json"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[
            {"op": "replace", "path": "/food/items/0/recommendation", "value": "模型建议"}
        ],
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["recommendation"] = "其他人刚刚修改"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="patch_conflict"):
        review_store.apply_patch(project_id, patch.patch_id)
    assert review_store.read_patch(project_id, patch.patch_id).status == "patch_conflict"
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "其他人刚刚修改"


def test_evidence_only_patch_waits_for_targeted_validation_without_integration(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["sources"] = ["old-ref"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="reference_mismatch",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/sources"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[
            {"op": "replace", "path": "/food/items/0/sources", "value": ["new-ref"]}
        ],
    )

    review_store.apply_patch(project_id, patch.patch_id)

    applied = review_store.read_patch(project_id, patch.patch_id)
    resolved = review_store.get_finding(project_id, finding.finding_id)
    assert applied.cascade_plan["requires_integration_execution"] is False
    assert applied.cascade_plan["requires_targeted_validation"] is True
    assert applied.cascade_plan["execution_status"] == "pending_targeted_validation"
    assert resolved.status == "verifying"
    assert resolved.disposition == "deferred"
    assert resolved.verification_status == "pending_targeted_review"


def test_batched_patch_validation_runs_targeted_checker_integration_and_site_once(isolated_project, monkeypatch):
    project_id, workspace = isolated_project
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城"})
    path = _write_lifestyle(workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["citation_status"] = "force_finalized"
    data["food"]["items"][0]["missing_entities"] = ["旧问题"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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

    calls = {"checker": 0, "integration": 0, "site": 0}

    def fake_checker(project, topic, *, fields, item_keys, allow_model_calls):
        calls["checker"] += 1
        assert project == project_id
        assert topic == "示例城"
        assert fields == ["food"]
        assert item_keys == {("food", 1)}
        assert allow_model_calls is False
        return []

    def fake_integration(project, entry_target, request):
        calls["integration"] += 1
        assert project == project_id
        assert entry_target == "integration_agent_node"
        assert "lifestyle_items" in request
        workspace_store.write_final_guide_contract_validation(
            project, status="passed", issues=[]
        )

    def fake_site(project):
        calls["site"] += 1
        assert project == project_id
        return workspace / "site.html"

    monkeypatch.setattr(review_validation.citation_check, "verify_lifestyle_citations", fake_checker)

    result = review_validation.run_pending_patch_validation(
        project_id,
        allow_model_calls=False,
        integration_runner=fake_integration,
        site_builder=fake_site,
    )

    assert result["status"] == "passed"
    assert calls == {"checker": 1, "integration": 1, "site": 1}
    assert review_store.read_patch(project_id, patch.patch_id).cascade_plan["execution_status"] == "completed"
    assert review_store.get_finding(project_id, finding.finding_id).status == "closed"
    validated_item = json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]
    assert validated_item["citation_status"] == "passed"
    assert "missing_entities" not in validated_item


@pytest.mark.parametrize("contract_status", ["retrying", "force_finalized"])
def test_batched_patch_validation_keeps_finding_open_when_contract_fails(
    isolated_project, monkeypatch, contract_status
):
    project_id, workspace = isolated_project
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城"})
    path = _write_lifestyle(workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["citation_status"] = "force_finalized"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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
    monkeypatch.setattr(
        review_validation.citation_check,
        "verify_lifestyle_citations",
        lambda *_args, **_kwargs: [],
    )

    def fake_integration(project, _entry_target, _request):
        workspace_store.write_final_guide_contract_validation(
            project,
            status=contract_status,
            issues=[{"code": "checklist_missing", "message": "最终攻略缺少清单"}],
        )

    result = review_validation.run_pending_patch_validation(
        project_id,
        allow_model_calls=False,
        integration_runner=fake_integration,
        site_builder=lambda *_args: pytest.fail("契约未通过时不应生成网站"),
    )

    assert result["status"] == "failed"
    assert result["issues"]["final_guide_contract"][0]["code"] == "checklist_missing"
    assert review_store.read_patch(project_id, patch.patch_id).cascade_plan["execution_status"] == "validation_failed"
    assert review_store.get_finding(project_id, finding.finding_id).status == "open"
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["citation_status"] == "force_finalized"


def test_batched_patch_validation_rejects_new_integration_conflict_despite_stale_passed_contract(
    isolated_project, monkeypatch
):
    project_id, workspace = isolated_project
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城"})
    _write_lifestyle(workspace)
    workspace_store.write_final_guide_contract_validation(project_id, status="passed", issues=[])
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
    monkeypatch.setattr(
        review_validation.citation_check,
        "verify_lifestyle_citations",
        lambda *_args, **_kwargs: [],
    )

    def fake_integration(project, _entry_target, _request):
        workspace_store.report_conflict(
            project,
            actor="integration_agent",
            target="lifestyle_agent",
            reason="整合发现新冲突",
        )

    result = review_validation.run_pending_patch_validation(
        project_id,
        allow_model_calls=False,
        integration_runner=fake_integration,
        site_builder=lambda *_args: pytest.fail("新 Integration conflict 存在时不应生成网站"),
    )

    assert result["status"] == "failed"
    assert result["issues"]["final_guide_contract"][0]["code"] == "integration_conflict"
    assert review_store.get_finding(project_id, finding.finding_id).status == "open"


def test_batched_patch_validation_failure_returns_finding_to_queue(isolated_project, monkeypatch):
    project_id, workspace = isolated_project
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城"})
    _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="reference_mismatch",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[{"op": "replace", "path": "/food/items/0/recommendation", "value": "仍缺证据"}],
    )
    review_store.apply_patch(project_id, patch.patch_id)
    monkeypatch.setattr(
        review_validation.citation_check,
        "verify_lifestyle_citations",
        lambda *_args, **_kwargs: [{"field": "food", "day": 1, "reason": "证据不足"}],
    )

    result = review_validation.run_pending_patch_validation(
        project_id,
        allow_model_calls=False,
        integration_runner=lambda *_args: pytest.fail("验收失败时不应运行 Integration"),
        site_builder=lambda *_args: pytest.fail("验收失败时不应生成网站"),
    )

    assert result["status"] == "failed"
    assert review_store.read_patch(project_id, patch.patch_id).cascade_plan["execution_status"] == "validation_failed"
    reopened = review_store.get_finding(project_id, finding.finding_id)
    assert reopened.status == "open"
    assert reopened.verification_status == "failed_targeted_validation"


def test_poi_pass_status_updates_detail_and_manifest_together(isolated_project):
    project_id, workspace = isolated_project
    poi_name = "示例景区"
    filename = f"day1_{workspace_store.poi_slug(poi_name)}.json"
    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / filename).write_text(
        json.dumps({
            "day": 1,
            "poi_name": poi_name,
            "citation_status": "force_finalized",
            "missing_entities": ["旧问题"],
            "routes": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (detail_dir / "manifest.json").write_text(
        json.dumps({
            "generated": [{
                "day": 1,
                "poi_name": poi_name,
                "file": filename,
                "citation_status": "force_finalized",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    workspace_store.mark_poi_citation_passed(project_id, 1, poi_name, actor="review_validation")

    detail = json.loads((detail_dir / filename).read_text(encoding="utf-8"))
    manifest = json.loads((detail_dir / "manifest.json").read_text(encoding="utf-8"))
    assert detail["citation_status"] == "passed"
    assert "missing_entities" not in detail
    assert manifest["generated"][0]["citation_status"] == "passed"


def test_route_structure_patch_is_not_mixed_into_normal_validation_batch(isolated_project):
    project_id, workspace = isolated_project
    route_path = workspace / "route_plan.json"
    route_path.write_text(json.dumps({"days": [{"day": 1, "pois": []}]}), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="route_structure_change",
        target={"file": "route_plan.json"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="route_plan.json",
        operations=[{"op": "replace", "path": "/days", "value": [{"day": 1, "pois": []}, {"day": 2, "pois": []}]}],
    )
    review_store.apply_patch(project_id, patch.patch_id)

    preview = review_validation.build_pending_validation_scope(project_id)

    assert preview["can_start"] is False
    assert preview["route_structure_patch_ids"] == [patch.patch_id]
    with pytest.raises(ValueError, match="路线结构 Patch"):
        review_validation.run_pending_patch_validation(project_id)


def test_unresolved_content_patch_does_not_silently_skip_validation(isolated_project):
    project_id, workspace = isolated_project
    path = workspace / "outputs" / "lifestyle.json"
    path.write_text(
        json.dumps({"food": {"items": [{"recommendation": "旧文案"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[{"op": "replace", "path": "/food/items/0/recommendation", "value": "新文案"}],
    )
    review_store.apply_patch(project_id, patch.patch_id)

    preview = review_validation.build_pending_validation_scope(project_id)

    assert preview["can_start"] is False
    assert preview["unresolved_patch_ids"] == [patch.patch_id]
    with pytest.raises(ValueError, match="无法解析出定向验收范围"):
        review_validation.run_pending_patch_validation(project_id)


def test_rollback_is_a_new_patch_and_restores_previous_value(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="content",
        target={"file": "outputs/lifestyle.json"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[
            {"op": "replace", "path": "/food/items/0/recommendation", "value": "新文案"}
        ],
    )
    review_store.apply_patch(project_id, patch.patch_id)
    rollback = review_store.rollback_patch(project_id, patch.patch_id)
    assert rollback.patch_id != patch.patch_id
    assert rollback.parent_patch_id == patch.patch_id
    review_store.apply_patch(project_id, rollback.patch_id)
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "旧文案"


def test_reject_patch_keeps_source_and_defers_finding(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="content",
        target={"file": "outputs/lifestyle.json"},
    )
    patch = review_store.propose_patch(
        project_id,
        finding_id=finding.finding_id,
        file="outputs/lifestyle.json",
        operations=[
            {"op": "replace", "path": "/food/items/0/recommendation", "value": "不采用"}
        ],
    )
    review_store.reject_patch(project_id, patch.patch_id, reason="需要补充证据")
    assert review_store.read_patch(project_id, patch.patch_id).status == "rejected"
    assert review_store.get_finding(project_id, finding.finding_id).disposition == "deferred"
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "旧文案"


def test_path_traversal_is_rejected(isolated_project):
    project_id, _ = isolated_project
    finding = review_store.create_finding(project_id, problem_type="content", target={})
    with pytest.raises(ValueError, match="workspace"):
        review_store.propose_patch(
            project_id,
            finding_id=finding.finding_id,
            file="../outside.json",
            operations=[{"op": "replace", "path": "/x", "value": 1}],
        )


def test_repeated_root_cause_creates_optimization_candidate(isolated_project):
    project_id, _ = isolated_project
    for _ in range(2):
        review_store.create_finding(
            project_id,
            problem_type="manifest_stale",
            root_cause="code",
            target={"file": "outputs/poi_details/manifest.json"},
        )
    candidates = review_store.collect_optimization_candidates(project_id)
    assert len(candidates) == 1
    assert candidates[0]["problem_type"] == "manifest_stale"
    assert review_store.read_optimization_candidates(project_id)[0]["occurrence_count"] == 2


def test_model_recommendation_is_persisted_without_mutating_content(isolated_project, monkeypatch):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json"},
        evidence_refs=["chunk-1"],
    )

    class FakeStructured:
        def invoke(self, messages):
            assert "chunk-1" in messages[0]["content"]
            return review_advisor.ModelReviewResult(
                passed=True,
                summary="建议保留，但改成条件式表达。",
                proposed_text="以当天现场安排为准。",
                claim_reviews=[{
                    "claim": "具体安排",
                    "original_text": "原文",
                    "evidence_refs": ["chunk-1"],
                    "evidence_excerpt": "素材原文",
                    "verdict": "partial",
                    "action": "narrow",
                    "proposed_text": "以当天现场安排为准。",
                    "rationale": "素材只支持条件式表达。",
                    "confidence": "high",
                }],
                risk_level="low",
                confidence="high",
                recommended_action="accept_patch",
            )

    class FakeModel:
        def with_structured_output(self, schema):
            assert schema is review_advisor.ModelReviewResult
            return FakeStructured()

    monkeypatch.setattr(review_advisor, "get_chat_model", lambda temperature=0: FakeModel())
    recommendation, result = review_advisor.generate_recommendation(
        project_id,
        finding.finding_id,
        current_content="原文",
        evidence=["chunk-1"],
        model_name="fake-strong",
    )
    assert result.passed is False
    assert recommendation.decision == "human_review"
    assert review_store.get_finding(project_id, finding.finding_id).recommendation_id == recommendation.recommendation_id
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "旧文案"


def test_generic_finding_explanation_is_available_without_project_specific_copy(isolated_project):
    project_id, _ = isolated_project
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/route.json", "json_path": "/days/0/notes"},
        before="安排 40 分钟",
        notes="",
    )
    loaded = review_store.get_finding(project_id, finding.finding_id)
    assert loaded is not None
    assert "unsupported_specificity" in loaded.issue_summary
    assert "outputs/route.json/days/0/notes" in loaded.issue_summary
    assert "局部修改" in loaded.review_question


def test_manifest_context_includes_linked_poi_detail(isolated_project):
    project_id, workspace = isolated_project
    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / "day2_demo.json").write_text(
        json.dumps({"poi_name": "示例景区", "day": 2, "citation_status": "force_finalized", "retry_history": ["未找到证据"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (detail_dir / "manifest.json").write_text(
        json.dumps({"generated": [{"poi_name": "示例景区", "day": 2, "file": "day2_demo.json", "citation_status": "force_finalized"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    finding = review_store.create_finding(
        project_id,
        problem_type="manifest_mismatch",
        target={"file": "outputs/poi_details/manifest.json", "json_path": "/generated", "missing_file": "day2_demo.json"},
    )
    context = review_store.build_review_context(project_id, finding.finding_id)
    assert context["summary"]["linked_detail"]["poi_name"] == "示例景区"
    assert any(item.get("type") == "retry_history" for item in context["evidence"] if isinstance(item, dict))
    assert context["summary"]["scope_kind"] == "field"
    assert context["evidence_groups"]["verification_failures"]
    assert context["evidence_groups"]["historical_reviews"] == []
    assert context["verification_timeline"]["round_count"] == 0
    assert context["verification_timeline"]["other_events"][0]["raw_detail"] == "未找到证据"


def test_context_backfills_verification_timeline_without_counting_reason_punctuation(isolated_project):
    project_id, workspace = isolated_project
    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    detail_file = detail_dir / "day6_demo.json"
    detail_file.write_text(
        json.dumps({
            "poi_name": "示例景区",
            "day": 6,
            "citation_status": "force_finalized",
            "retry_history": [
                "第1轮：路线A途经点「甲」的失败claim：claim_id=old-a，target_path=point_name，失败原因=未提及；理由内部还有；分号；路线A途经点「乙」的失败claim：claim_id=old-b，target_path=highlight，失败原因=不支持",
                "第2轮：路线A途经点「乙」的失败claim：claim_id=current-a，target_path=point_name，失败原因=名称未对应",
            ],
            "routes": [{
                "route_name": "路线A",
                "route_segments": [{
                    "order": 2,
                    "point_name": "乙",
                    "highlight": "乙的介绍",
                    "claims": [
                        {
                            "claim_id": "current-a",
                            "target_path": "point_name",
                            "claim_text": "乙是途经点",
                            "support_mode": "direct",
                            "claimed_entities": ["乙"],
                        },
                        {
                            "claim_id": "current-b",
                            "target_path": "highlight",
                            "claim_text": "乙的介绍",
                            "support_mode": "direct",
                            "claimed_entities": ["乙"],
                        },
                    ],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace_store.write_evidence_checks(project_id, "poi_detail", [
        {
            "target_key": "poi_detail:示例景区:route:路线A:order:2:claim:current-a",
            "claim_id": "current-a",
            "scope": "entity_identity",
            "result": "fail",
            "method": "alias_resolution",
            "reason": "名称未对应。",
            "missing_entities": ["乙"],
            "route_name": "路线A",
            "order": 2,
            "point_name": "乙",
            "checked_at": "2026-09-08T10:18:00+00:00",
        },
        {
            "target_key": "poi_detail:示例景区:route:路线A:order:2:claim:current-b",
            "claim_id": "current-b",
            "scope": "substantive_claim",
            "result": "fail",
            "method": "substantive_support",
            "reason": "介绍未获得支持。",
            "route_name": "路线A",
            "order": 2,
            "point_name": "乙",
            "checked_at": "2026-09-08T10:19:00+00:00",
        },
    ])
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day6_demo.json", "object_label": "示例景区"},
    )

    context = review_store.build_review_context(project_id, finding.finding_id)

    timeline = context["verification_timeline"]
    assert timeline["source"] == "retry_history_read_projection"
    assert timeline["round_count"] == 2
    assert timeline["progression"] == [2, 1]
    assert timeline["rounds"][0]["superseded"] is True
    assert timeline["rounds"][1]["is_latest"] is True
    assert timeline["current_blocker_count"] == 1
    assert timeline["current_claim_count"] == 2
    assert len(context["origin_claims"]) == 2
    assert context["origin_claims"][0]["current_value"] == "乙"
    assert context["origin_claims"][0]["json_pointer"] == "/routes/0/route_segments/0/point_name"


def test_context_traces_day_finding_to_exact_failed_claim_before_model_review(isolated_project):
    project_id, workspace = isolated_project
    (workspace / "route_plan.json").write_text(
        json.dumps({
            "days": [{
                "day": 1,
                "citation_status": "force_finalized",
                "missing_entities": ["文殊坊文化街区"],
                "pois": [{
                    "name": "文殊院",
                    "brief": "免费开放的千年古刹，院外有小吃",
                }],
                "claims": [{
                    "claim_id": "route-claim-wenshu",
                    "target_path": "pois.0.brief",
                    "subject": "文殊院",
                    "claim_text": "文殊院免费开放；寺院门外即为文殊坊文化街区，有小吃。",
                    "claim_type": "fact",
                    "support_mode": "direct",
                    "claimed_entities": ["文殊院", "文殊坊文化街区"],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace_store.write_evidence_checks(project_id, "route", [
        {
            "target_key": "route:day:1:claim:route-claim-wenshu",
            "claim_id": "route-claim-wenshu",
            "scope": "source_availability",
            "result": "pass",
            "method": "chunk_lookup",
            "reason": "引用可读。",
            "cited_chunk_ids": ["chunk-1"],
            "checked_at": "2026-09-08T10:17:00+00:00",
        },
        {
            "target_key": "route:day:1:claim:route-claim-wenshu",
            "claim_id": "route-claim-wenshu",
            "scope": "substantive_claim",
            "result": "fail",
            "method": "substantive_support",
            "reason": "证据未明确支持寺院门外即为该街区。",
            "missing_entities": ["文殊坊文化街区"],
            "cited_chunk_ids": ["chunk-1"],
            "checked_at": "2026-09-08T10:18:00+00:00",
        },
    ])
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={
            "file": "route_plan.json",
            "json_path": "/days/0",
            "object_label": "第 1 天路线",
        },
    )

    context = review_store.build_review_context(project_id, finding.finding_id)

    assert context["review_item_contexts"] == []
    assert len(context["origin_claims"]) == 1
    origin = context["origin_claims"][0]
    assert origin["agent_label"] == "路线规划 Agent"
    assert origin["day"] == 1
    assert origin["subject"] == "文殊院"
    assert origin["field_label"] == "简介"
    assert origin["claim_id"] == "route-claim-wenshu"
    assert origin["claim_text"].endswith("有小吃。")
    assert origin["current_value"] == "免费开放的千年古刹，院外有小吃"
    assert origin["json_pointer"] == "/days/0/pois/0/brief"
    assert [item["result"] for item in origin["checks"]] == ["pass", "fail"]
    assert origin["checks"][1]["reason"] == "证据未明确支持寺院门外即为该街区。"


def test_context_without_claim_checks_keeps_legacy_fallback(isolated_project):
    project_id, workspace = isolated_project
    (workspace / "route_plan.json").write_text(
        json.dumps({"days": [{"day": 1, "citation_status": "force_finalized"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "route_plan.json", "json_path": "/days/0"},
    )

    context = review_store.build_review_context(project_id, finding.finding_id)

    assert context["origin_claims"] == []
    assert context["summary"]["file"] == "route_plan.json"


def test_context_explains_stale_missing_entities_with_current_and_downstream_evidence(
    isolated_project, monkeypatch
):
    project_id, workspace = isolated_project
    material_root = workspace.parent / "materials"
    chunks_dir = material_root / "index" / "示例山"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    (chunks_dir / "chunks.jsonl").write_text(
        json.dumps({
            "chunk_id": "示例山-004-c001",
            "text": "华藏寺、十方普贤圣像与金顶景观齐聚于此。",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(review_store.citation_check, "MATERIAL_LIBRARY_DIR", material_root)
    monkeypatch.setattr(review_store.citation_check, "PROJECTS_DIR", workspace.parent.parent)
    workspace_store.write_requirements(project_id, "示例山", {"destination": "示例山"})

    (workspace / "route_plan.json").write_text(
        json.dumps({
            "updated_at": "2026-09-01T13:07:00+00:00",
            "days": [{
                "day": 2,
                "citation_status": "force_finalized",
                "missing_entities": ["十方普贤圣像", "华藏寺", "十方普贤圣像"],
                "sources": [{
                    "platform": "project_evidence",
                    "chunk_id": "project-evidence-irrelevant",
                }],
                "pois": [{
                    "name": "金顶",
                    "brief": "可观看十方普贤圣像和金顶云海。",
                    "needs_detail_reason": "含华藏寺、十方普贤圣像等多处景观。",
                }],
                "claims": [{
                    "claim_id": "route-claim-current-notes",
                    "target_path": "notes",
                    "claim_text": "当日需关注观光车预约。",
                    "support_mode": "direct",
                    "evidence_refs": [{
                        "platform": "project_evidence",
                        "chunk_id": "project-evidence-irrelevant",
                    }],
                    "evidence_snapshots": [{
                        "source": {
                            "platform": "project_evidence",
                            "chunk_id": "project-evidence-irrelevant",
                        },
                        "excerpt": "高山区观光车实行分时预约。",
                    }],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace_store.write_evidence_checks(project_id, "route", [{
        "target_key": "route:day:2:claim:route-claim-old-brief",
        "claim_id": "route-claim-old-brief",
        "claim_text": "金顶有十方普贤圣像和云海。",
        "target_path": "pois.0.brief",
        "scope": "entity_identity",
        "result": "fail",
        "reason": "旧原文未提到十方普贤圣像。",
        "missing_entities": ["十方普贤圣像"],
        "checked_at": "2026-09-01T12:59:00+00:00",
    }, {
        "target_key": "route:day:2:claim:route-claim-old-reason",
        "claim_id": "route-claim-old-reason",
        "claim_text": "金顶含华藏寺等多处景观。",
        "target_path": "pois.0.needs_detail_reason",
        "scope": "substantive_claim",
        "result": "fail",
        "reason": "旧原文未提到华藏寺。",
        "missing_entities": ["华藏寺"],
        "checked_at": "2026-09-01T12:59:30+00:00",
    }])

    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / "day2_金顶.json").write_text(
        json.dumps({
            "poi_name": "金顶",
            "day": 2,
            "citation_status": "passed",
            "routes": [{
                "route_name": "核心线",
                "route_segments": [{
                    "order": 1,
                    "point_name": "十方普贤圣像广场",
                    "highlight": "华藏寺与十方普贤圣像是金顶重要景观。",
                    "claims": [{
                        "claim_id": "poi-claim-current",
                        "target_path": "highlight",
                        "claim_text": "华藏寺与十方普贤圣像是金顶重要景观。",
                        "claimed_entities": ["华藏寺", "十方普贤圣像"],
                        "support_mode": "direct",
                        "evidence_refs": [{"chunk_id": "示例山-004-c001"}],
                        "evidence_snapshots": [{
                            "source": {"chunk_id": "示例山-004-c001"},
                            "excerpt": "华藏寺、十方普贤圣像与金顶景观齐聚于此。",
                        }],
                    }],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace_store.write_evidence_checks(project_id, "poi_detail", [{
        "target_key": "poi_detail:day:2:claim:poi-claim-current",
        "claim_id": "poi-claim-current",
        "scope": "substantive_claim",
        "result": "pass",
        "reason": "原文支持。",
        "checked_at": "2026-09-01T16:00:00+00:00",
    }])
    (workspace / "final_guide.md").write_text(
        "# 攻略\n金顶可观看华藏寺和十方普贤圣像。\n",
        encoding="utf-8",
    )
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={
            "file": "route_plan.json",
            "json_path": "/days/0",
            "object_label": "第 2 天路线",
        },
    )

    context = review_store.build_review_context(project_id, finding.finding_id)

    assert context["origin_claims"] == []
    missing = context["missing_entity_context"]
    assert missing["entity_count"] == 2
    assert missing["occurrence_count"] == 3
    assert missing["raw_missing_entity_count"] == 3
    assert missing["verification_state"] == "stale_failure"
    assert missing["project_impact"]["review_queue_counted"] is True
    assert missing["project_impact"]["generation_blocked"] is False
    assert missing["project_impact"]["final_guide_affected"] is True

    statue = missing["entities"][0]
    assert statue["entity"] == "十方普贤圣像"
    assert statue["occurrence_count"] == 2
    assert [item["json_pointer"] for item in statue["occurrences"]] == [
        "/days/0/pois/0/brief",
        "/days/0/pois/0/needs_detail_reason",
    ]
    assert statue["stale_evidence_state"] is True
    assert statue["historical_failures"][0]["claim_id"] == "route-claim-old-brief"
    assert statue["historical_failures"][0]["match_status"] == "same_field_stale_claim"
    assert statue["historical_failures"][0]["checked_before_content_update"] is True
    assert statue["historical_failures"][0]["checked_claim_text"] == "金顶有十方普贤圣像和云海。"
    assert statue["current_sources"][0]["relation"] == "no_exact_mention"
    assert statue["current_sources"][0]["attachment_scope"] == "review_scope"
    assert statue["candidate_evidence"][0]["ref"] == "示例山-004-c001"
    assert statue["downstream_evidence"][0]["validation_status"] == "passed"
    assert statue["downstream_evidence"][0]["evidence_excerpts"][0]["origin"] == "embedded_snapshot"
    assert statue["final_guide_occurrences"][0]["match_mode"] == "text_match"
    assert statue["final_guide_occurrences"][0]["claim_linked"] is False
    assert any(
        item.get("origin") == "embedded_snapshot" and "分时预约" in str(item.get("text"))
        for item in context["evidence_groups"]["source_excerpts"]
    )
    temple = missing["entities"][1]
    assert temple["entity"] == "华藏寺"
    assert temple["occurrence_count"] == 1
    assert temple["occurrences"][0]["json_pointer"] == "/days/0/pois/0/needs_detail_reason"


def test_item_decisions_are_append_only_and_do_not_modify_content(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
    )
    recommendation = ReviewRecommendation(
        recommendation_id="recommendation-item-test",
        finding_id=finding.finding_id,
        claim_reviews=[{
            "claim": "停留时长缺少依据",
            "original_text": "建议停留 40 分钟",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "原文没有给出具体分钟数",
            "verdict": "unsupported",
            "action": "narrow",
            "proposed_text": "建议根据现场情况灵活安排",
            "rationale": "没有数字依据",
            "confidence": "high",
        }],
    )
    review_store.append_recommendation(project_id, recommendation)

    first = review_store.record_item_decision(
        project_id,
        finding.finding_id,
        recommendation.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="停留时长缺少依据",
        disposition="accepted",
    )
    assert first.disposition == "accepted"
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "旧文案"
    assert review_store.get_finding(project_id, finding.finding_id).status == "in_review"

    review_store.record_item_decision(
        project_id,
        finding.finding_id,
        recommendation.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="停留时长缺少依据",
        disposition="keep_original",
        notes="人工复查后保留",
    )
    latest = review_store.read_item_decisions(project_id, recommendation.recommendation_id)
    assert len(latest) == 1
    assert latest[0].disposition == "keep_original"
    decision_lines = (workspace / "review_runs" / recommendation.recommendation_id / "item_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(decision_lines) == 2


def test_legacy_recommendation_is_projected_to_route_segment_context(isolated_project):
    project_id, workspace = isolated_project
    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / "day1_demo.json").write_text(
        json.dumps({
            "poi_name": "示例景区",
            "citation_status": "force_finalized",
            "retry_history": ["第1轮：路线A途经点「湖区」：原文没有支持20分钟。"],
            "routes": [{
                "route_name": "路线A（推荐）",
                "route_segments": [{
                    "point_name": "湖区",
                    "highlight": "湖边休息",
                    "suggested_duration": "20分钟",
                    "sources": [],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day1_demo.json"},
    )
    recommendation = ReviewRecommendation(
        recommendation_id="recommendation-legacy-projection",
        finding_id=finding.finding_id,
        proposed_text="具体各点的最小改写建议如下：\n- 路线A「湖区」：删除20分钟，改为灵活安排。",
    )
    review_store.append_recommendation(project_id, recommendation)

    context = review_store.build_review_context(project_id, finding.finding_id)
    item = context["review_item_contexts"][0]
    assert item["item_key"] == "legacy:0"
    assert item["target"]["json_pointer"] == "/routes/0/route_segments/0"
    assert item["current_value"]["suggested_duration"] == "20分钟"
    assert item["failure_points"][0]["round"] == 1
    assert item["coverage"] == "insufficient"
    assert item["evidence_origin"] == "current_json_sources"
    assert item["support_status"] == "unsupported"
    assert item["support_basis"] == "historical_failure"
    assert item["alternative_evidence_found"] is False
    assert item["related_evidence_candidates"] == []
    assert item["library_evidence_candidates"] == []
    assert item["ready_for_human_review"] is False


def test_related_evidence_candidates_are_weak_matches_not_direct_evidence():
    failure_points = [{
        "round": 2,
        "detail": "原文仅提及“大熊猫别墅”“六号别墅”“小熊猫区域”和“月亮产房”，并未出现“成年/亚成年大熊猫别墅区”。",
    }]
    candidates = review_store._related_source_candidates(
        {
            "direct": "第一部分是最南部的大熊猫别墅。",
            "villa-route": "从西门进入，离六号别墅比较远。",
            "unrelated": "小熊猫区域很大，随后前往月亮产房。",
        },
        ["direct", "villa-route", "unrelated"],
        ["direct"],
        "成年/亚成年大熊猫别墅区",
        failure_points,
    )

    assert [item["ref"] for item in candidates] == ["villa-route"]
    assert candidates[0]["matched_terms"] == ["六号别墅"]
    assert review_store._historical_support_status(failure_points) == "unsupported"


def test_library_candidates_search_beyond_current_file_without_domain_rules():
    failure_points = [{"round": 1, "detail": "原文没有出现“滨海木栈道”和“观景平台”。"}]
    candidates = review_store._library_source_candidates(
        {
            "island-a-c000": "海岛入口和停车场说明。",
            "island-a-c001": "沿滨海木栈道步行可到观景平台。",
            "other-c000": "市中心商业街和餐饮推荐。",
        },
        [("island-a-c001", 8.2), ("other-c000", 3.1)],
        {"island-a-c000"},
        "滨海木栈道",
        failure_points,
        {"island-a"},
    )

    assert [item["ref"] for item in candidates] == ["island-a-c001", "other-c000"]
    assert candidates[0]["same_document"] is True
    assert candidates[0]["matched_terms"] == ["滨海木栈道"]
    assert candidates[1]["matched_terms"] == []


def test_cascade_policy_distinguishes_targeted_day_from_structure():
    day_plan = review_policy.build_cascade_plan(
        "p1",
        file="route_plan.json",
        target={"day": 3},
        operations=[{"op": "replace", "path": "/days/2/notes", "value": "调整"}],
    )
    assert day_plan["impact_class"] == "route_day"
    assert day_plan["requires_integration_execution"] is True
    assert "full_rerun" not in [item["action"] for item in day_plan["actions"]]
    structure_plan = review_policy.build_cascade_plan(
        "p1",
        file="route_plan.json",
        target={},
        operations=[{"op": "replace", "path": "/days", "value": []}],
    )
    assert structure_plan["impact_class"] == "route_structure"
    assert structure_plan["needs_human_approval"] is True

    manifest_plan = review_policy.build_cascade_plan(
        "p1",
        file="outputs/poi_details/manifest.json",
        target={"json_path": "/generated/0/citation_status"},
        operations=[{"op": "replace", "path": "/generated/0/citation_status", "value": "passed"}],
    )
    assert manifest_plan["impact_class"] == "mechanical"
    assert manifest_plan["requires_targeted_validation"] is False
    assert manifest_plan["requires_integration_execution"] is False


def test_state_scanner_excludes_review_artifacts_and_groups_manifest_mirror(isolated_project):
    project_id, workspace = isolated_project
    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    detail = {
        "poi_name": "示例景区",
        "day": 2,
        "citation_status": "force_finalized",
        "retry_history": ["缺少开放时间证据"],
    }
    (detail_dir / "day2_demo.json").write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
    (detail_dir / "manifest.json").write_text(
        json.dumps({"generated": [{"poi_name": "示例景区", "day": 2, "file": "day2_demo.json", "citation_status": "force_finalized"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    frozen = workspace / "review_runs" / "recommendation-copy" / "workspace" / "outputs" / "poi_details"
    frozen.mkdir(parents=True, exist_ok=True)
    (frozen / "day2_demo.json").write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

    review_store.sync_state_findings(project_id)
    findings = review_store.read_findings(project_id)
    queue = review_store.build_review_queue(project_id)
    state = review_store.validate_project_state(project_id)

    assert len(findings) == 1
    assert findings[0].canonical_key == "poi_detail:2:示例景区"
    assert len(queue) == 1
    assert queue[0].title == "引用核验待确认 · 示例景区"
    assert queue[0].mirror_count == 0
    assert state["force_finalized_paths"] == ["outputs/poi_details/day2_demo.json"]
    assert len(state["derived_mirrors"]) == 1


def test_concurrent_state_scans_create_one_canonical_finding(isolated_project):
    project_id, workspace = isolated_project
    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / "day2_demo.json").write_text(
        json.dumps(
            {
                "poi_name": "示例景区",
                "day": 2,
                "citation_status": "force_finalized",
                "retry_history": ["缺少开放时间证据"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(review_store.sync_state_findings, [project_id, project_id]))

    findings = review_store.read_findings(project_id)
    assert len(findings) == 1
    assert findings[0].canonical_key == "poi_detail:2:示例景区"


def test_state_scanner_supersedes_existing_duplicate_canonical_findings(isolated_project):
    project_id, workspace = isolated_project
    detail_dir = workspace / "outputs" / "poi_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    (detail_dir / "day2_demo.json").write_text(
        json.dumps(
            {
                "poi_name": "示例景区",
                "day": 2,
                "citation_status": "force_finalized",
                "retry_history": ["缺少开放时间证据"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    canonical_key = "poi_detail:2:示例景区"
    for index in range(2):
        finding = ReviewFinding(
            finding_id=f"finding-state-duplicate-{index}",
            project_id=project_id,
            baseline_version=f"2026-09-05T00:00:0{index}+00:00",
            target={"file": "outputs/poi_details/day2_demo.json"},
            problem_type="force_finalized_residue",
            source_review_id=f"state:v2:{canonical_key}",
            canonical_key=canonical_key,
            finding_kind="state_residue",
            created_at=f"2026-09-05T00:00:0{index}+00:00",
            updated_at=f"2026-09-05T00:00:0{index}+00:00",
        )
        review_store._append_jsonl(
            review_store._findings_path(project_id),
            finding.model_dump(mode="json"),
        )

    review_store.sync_state_findings(project_id)

    findings = sorted(review_store.read_findings(project_id), key=lambda item: item.finding_id)
    assert len(findings) == 2
    primary = next(item for item in findings if not item.superseded_by)
    duplicate = next(item for item in findings if item.superseded_by)
    assert duplicate.status == "closed"
    assert duplicate.finding_kind == "derived_mirror"
    assert duplicate.superseded_by == primary.finding_id
    assert duplicate.verification_status == "duplicate_canonical_superseded"


def test_final_guide_contract_force_finalize_uses_canonical_review_finding(isolated_project):
    project_id, _workspace = isolated_project
    workspace_store.write_final_guide(
        project_id,
        actor="integration_agent",
        content_md="# 攻略\n\n## Day 1\n" + "内容" * 120,
        meta={"merged_fields": ["route_plan"], "checklist_included": False},
    )
    issues = [{
        "code": "checklist_missing",
        "message": "最终攻略缺少必备清单",
        "evidence_paths": ["final_guide.md"],
    }]
    workspace_store.write_final_guide_contract_validation(
        project_id,
        status="force_finalized",
        issues=issues,
    )

    review_store.sync_state_findings(project_id)
    findings = review_store.read_findings(project_id)
    queue = review_store.build_review_queue(project_id)
    state = review_store.validate_project_state(project_id)

    assert len(findings) == 1
    assert findings[0].canonical_key == "final_guide:contract"
    assert findings[0].problem_type == "final_guide_contract_violation"
    assert findings[0].repair_action == "targeted_integration_rerun"
    assert queue[0].title == "最终攻略交付不完整"
    assert state["contract_violation_paths"] == ["final_guide.md"]
    assert state["raw_force_finalized_paths"] == [
        "final_guide.meta.json/contract_validation"
    ]

    workspace_store.write_final_guide_contract_validation(project_id, status="passed", issues=[])
    review_store.sync_state_findings(project_id)

    resolved = review_store.get_finding(project_id, findings[0].finding_id)
    assert resolved.status == "closed"
    assert resolved.verification_status == "passed_after_regeneration"
    assert review_store.validate_project_state(project_id)["contract_violation_paths"] == []
    assert review_store.validate_project_state(project_id)["raw_force_finalized_paths"] == []


def test_passed_poi_recheck_closes_state_finding_and_keeps_history(isolated_project):
    project_id, workspace = isolated_project
    details_dir = workspace / "outputs" / "poi_details"
    details_dir.mkdir(parents=True, exist_ok=True)
    detail_path = details_dir / "day6_demo.json"
    detail_path.write_text(
        json.dumps({
            "day": 6,
            "poi_name": "示例景区",
            "routes": [],
            "citation_status": "force_finalized",
            "human_review_required": True,
            "retry_history": ["第1轮：路线途经点「甲」的失败claim：claim_id=claim-a"],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    review_store.sync_state_findings(project_id)
    finding = next(
        item for item in review_store.read_findings(project_id)
        if item.canonical_key == "poi_detail:6:示例景区"
    )

    data = json.loads(detail_path.read_text(encoding="utf-8"))
    data["citation_status"] = "passed"
    data["human_review_required"] = False
    detail_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    review_store.sync_state_findings(project_id)

    resolved = review_store.get_finding(project_id, finding.finding_id)
    assert resolved.status == "closed"
    assert resolved.disposition == "resolved"
    assert resolved.verification_status == "passed_after_targeted_recheck"
    assert "历史核验记录继续保留作审计" in resolved.notes
    assert json.loads(detail_path.read_text(encoding="utf-8"))["retry_history"] == [
        "第1轮：路线途经点「甲」的失败claim：claim_id=claim-a"
    ]

    context = review_store.build_review_context(project_id, finding.finding_id)
    assert context["verification_timeline"]["current_blocker_count"] == 0
    assert context["verification_timeline"]["current_claim_count"] == 0


def test_route_feasibility_force_finalize_is_in_raw_degradation_scan(isolated_project):
    project_id, workspace = isolated_project
    (workspace / "route_feasibility.json").write_text(
        json.dumps(
            {
                "overall_status": "warning",
                "force_finalized": True,
                "checks": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = review_store.validate_project_state(project_id)

    assert state["raw_force_finalized_paths"] == ["route_feasibility.json"]


def test_generation_failed_manifest_entry_is_one_raw_degradation_when_detail_missing(
    isolated_project,
):
    project_id, workspace = isolated_project
    manifest_dir = workspace / "outputs" / "poi_details"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated": [
                    {
                        "poi_name": "洱海生态廊道",
                        "day": 1,
                        "file": "day1_erhai.json",
                        "citation_status": "generation_failed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = review_store.validate_project_state(project_id)

    assert state["raw_force_finalized_paths"] == [
        "outputs/poi_details/day1_erhai.json"
    ]
    assert state["manifest_mismatches"] == ["missing:day1_erhai.json"]


def test_force_finalized_conflict_without_canonical_mirror_is_raw_degradation(
    isolated_project,
):
    project_id, _workspace = isolated_project
    record = workspace_store.report_conflict(
        project_id,
        actor="integration_agent",
        target="route_agent",
        reason="独立路线冲突",
        issue_type="route_edit_overreach",
    )
    workspace_store.mark_force_finalize(project_id)

    state = review_store.validate_project_state(project_id)

    assert state["raw_force_finalized_paths"] == [
        f"conflicts.jsonl/{record.id}"
    ]


def test_force_finalized_conflicts_do_not_duplicate_matching_canonical_objects(
    isolated_project,
):
    project_id, workspace = isolated_project
    lifestyle_path = workspace / "outputs" / "lifestyle.json"
    lifestyle_path.parent.mkdir(parents=True, exist_ok=True)
    lifestyle_path.write_text(
        json.dumps(
            {
                "food": {
                    "items": [
                        {
                            "day": 1,
                            "recommendation": "待复核",
                            "citation_status": "force_finalized",
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    workspace_store.report_conflict(
        project_id,
        actor="citation_checker",
        target="lifestyle_agent.food",
        reason="引用核验未通过",
        issues=[{"field": "food", "day": 1}],
    )
    workspace_store.mark_force_finalize(project_id)

    state = review_store.validate_project_state(project_id)

    assert state["raw_force_finalized_paths"] == [
        "outputs/lifestyle.json/food/items/0"
    ]


def test_route_feasibility_conflict_does_not_duplicate_force_finalized_report(
    isolated_project,
):
    project_id, workspace = isolated_project
    (workspace / "route_feasibility.json").write_text(
        json.dumps(
            {"overall_status": "unknown", "force_finalized": True, "checks": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    workspace_store.report_conflict(
        project_id,
        actor="citation_checker",
        target="route_agent",
        reason="路线可执行性核验尚有未解决项",
        issue_type="route_feasibility_unresolved",
    )
    workspace_store.mark_force_finalize(project_id)

    state = review_store.validate_project_state(project_id)

    assert state["raw_force_finalized_paths"] == ["route_feasibility.json"]


def test_unrelated_force_conflict_is_not_hidden_by_other_canonical_degradation(
    isolated_project,
):
    project_id, workspace = isolated_project
    feasibility_path = workspace / "route_feasibility.json"
    feasibility_path.write_text(
        json.dumps(
            {"overall_status": "warning", "force_finalized": True, "checks": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record = workspace_store.report_conflict(
        project_id,
        actor="integration_agent",
        target="route_agent",
        reason="编辑越权",
        issue_type="route_edit_overreach",
    )
    workspace_store.mark_force_finalize(project_id)

    state = review_store.validate_project_state(project_id)

    assert state["raw_force_finalized_paths"] == [
        "route_feasibility.json",
        f"conflicts.jsonl/{record.id}",
    ]


def test_pending_and_resolved_conflicts_are_not_raw_degradation(isolated_project):
    project_id, _workspace = isolated_project
    record = workspace_store.report_conflict(
        project_id,
        actor="integration_agent",
        target="route_agent",
        reason="尚待处理",
    )
    assert review_store.validate_project_state(project_id)["raw_force_finalized_paths"] == []

    workspace_store.resolve_conflict(project_id, record.id)

    assert review_store.validate_project_state(project_id)["raw_force_finalized_paths"] == []


def test_final_guide_contract_backfill_is_dry_run_then_idempotent_apply(isolated_project):
    project_id, workspace = isolated_project
    workspace_store.write_requirements(project_id, "成都", {"destination": "成都", "days": 1})
    (workspace / "route_plan.json").write_text(
        json.dumps({"days": [{"day": 1, "city": "成都", "pois": []}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace_store.write_final_guide(
        project_id,
        actor="integration_agent",
        content_md="# 攻略\n\n## 行前必备清单\n证件\n\n## Day 1\n" + "内容" * 120,
        meta={"merged_fields": [], "checklist_included": True},
    )

    plan = review_store.plan_final_guide_contract_backfill([project_id])
    item = plan["plans"][0]
    assert item["action"] == "write_contract_validation"
    assert item["status"] == "force_finalized"
    assert {issue["code"] for issue in item["issues"]} == {"merged_field_missing"}
    assert "contract_validation" not in workspace_store.read_workspace(project_id, "final_guide.meta.json")

    applied = review_store.apply_final_guide_contract_backfill(plan)
    assert applied == [{
        "project_id": project_id,
        "status": "applied",
        "contract_status": "force_finalized",
        "issue_count": 1,
        "meta_merged_fields_added": [],
        "sections_normalized": False,
    }]
    contract = workspace_store.read_workspace(project_id, "final_guide.meta.json")["contract_validation"]
    assert contract["source"] == "historical_backfill"
    finding = next(item for item in review_store.read_findings(project_id) if item.canonical_key == "final_guide:contract")
    assert finding.issue_summary.startswith("历史攻略补扫未通过最终交付契约")

    second_plan = review_store.plan_final_guide_contract_backfill([project_id])
    assert second_plan["plans"][0]["reason"] == "contract_already_current"
    assert review_store.apply_final_guide_contract_backfill(second_plan)[0]["status"] == "skipped"


def test_final_guide_contract_backfill_normalizes_sections_and_upgrades_hashes(isolated_project):
    project_id, _workspace = isolated_project
    legacy_guide = (
        "# 攻略\n\n## Day 1\n" + "正文" * 120
        + f"\n\n---\n\n{REFERENCE_SECTION_HEADING}\n来源"
        + f"\n\n---\n\n{POI_SECTION_HEADING}\n### Day1 武侯祠\n路线"
    )
    workspace_store.write_final_guide(
        project_id,
        actor="integration_agent",
        content_md=legacy_guide,
        meta={"merged_fields": [], "checklist_included": False},
    )
    workspace_store.write_final_guide_contract_validation(
        project_id,
        status="passed",
        issues=[],
    )
    meta_path = workspace_store.workspace_dir(project_id) / "final_guide.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["contract_validation"]["body_sha256"]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    plan = review_store.plan_final_guide_contract_backfill([project_id])
    assert plan["schema_version"] == "final-guide-contract-backfill-v2"
    assert plan["plans"][0]["action"] == "refresh_contract_delivery"
    assert plan["plans"][0]["normalize_program_sections"] is True

    result = review_store.apply_final_guide_contract_backfill(plan)[0]
    assert result["sections_normalized"] is True
    assert result["delivery_hash_refreshed"] is True
    guide = workspace_store.read_workspace(project_id, "final_guide.md")
    assert guide.index("重点景点深度玩法") < guide.index("参考来源")
    contract = workspace_store.read_workspace(project_id, "final_guide.meta.json")["contract_validation"]
    assert contract["body_sha256"]
    assert contract["guide_sha256"] == hashlib.sha256(guide.encode("utf-8")).hexdigest()
    assert review_store.plan_final_guide_contract_backfill([project_id])["plans"][0]["reason"] == "contract_already_current"


def test_final_guide_contract_backfill_revalidates_unprovable_legacy_body_hash(isolated_project):
    project_id, workspace = isolated_project
    workspace_store.write_final_guide(
        project_id,
        actor="integration_agent",
        content_md="# 攻略\n\n## Day 1\n" + "正文" * 120,
        meta={"merged_fields": [], "checklist_included": False},
    )
    workspace_store.write_final_guide_contract_validation(
        project_id,
        status="passed",
        issues=[],
    )
    meta_path = workspace / "final_guide.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["contract_validation"]["body_sha256"]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    guide_path = workspace / "final_guide.md"
    guide_path.write_text(
        guide_path.read_text(encoding="utf-8")
        + f"\n\n---\n\n{POI_SECTION_HEADING}\n### Day1 武侯祠\n路线\n",
        encoding="utf-8",
    )

    plan = review_store.plan_final_guide_contract_backfill([project_id])
    item = plan["plans"][0]
    assert item["action"] == "revalidate_contract_delivery"
    assert item["status"] == "force_finalized"

    review_store.apply_final_guide_contract_backfill(plan)
    guide = workspace_store.read_workspace(project_id, "final_guide.md")
    contract = workspace_store.read_workspace(project_id, "final_guide.meta.json")["contract_validation"]
    assert contract["source"] == "historical_backfill"
    assert contract["body_sha256"]
    assert contract["guide_sha256"] == hashlib.sha256(guide.encode("utf-8")).hexdigest()


def test_final_guide_contract_backfill_rejects_stale_plan(isolated_project):
    project_id, workspace = isolated_project
    workspace_store.write_final_guide(
        project_id,
        actor="integration_agent",
        content_md="# 攻略\n" + "内容" * 120,
        meta={"merged_fields": [], "checklist_included": False},
    )
    plan = review_store.plan_final_guide_contract_backfill([project_id])
    (workspace / "final_guide.md").write_text("# 已变化\n" + "新内容" * 120, encoding="utf-8")

    with pytest.raises(RuntimeError, match="已变化"):
        review_store.apply_final_guide_contract_backfill(plan)


def test_final_guide_contract_backfill_normalizes_legacy_pitfall_declaration(isolated_project):
    project_id, workspace = isolated_project
    (workspace / "route_plan.json").write_text(
        json.dumps({"days": [{"day": 1, "city": "成都", "pois": []}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (workspace / "outputs" / "pitfall.json").write_text(
        json.dumps({"food": {"items": [{"day": 1, "warning": "高峰期排队"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace_store.write_final_guide(
        project_id,
        actor="integration_agent",
        content_md="# 攻略\n\n## Day 1\n" + "内容" * 120 + "\n\n## 避雷提示\n高峰期排队",
        meta={"merged_fields": ["route_plan"], "checklist_included": False},
    )

    plan = review_store.plan_final_guide_contract_backfill([project_id])
    item = plan["plans"][0]
    assert item["meta_merged_fields_to_add"] == ["pitfall"]
    assert item["status"] == "force_finalized"
    assert {issue["code"] for issue in item["issues"]} == {"checklist_missing"}

    review_store.apply_final_guide_contract_backfill(plan)
    meta = workspace_store.read_workspace(project_id, "final_guide.meta.json")
    assert "pitfall" in meta["merged_fields"]


def test_resolved_fingerprint_stays_closed_until_content_changes(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["citation_status"] = "force_finalized"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    review_store.sync_state_findings(project_id)
    finding = review_store.read_findings(project_id)[0]
    review_store.record_verification(
        project_id,
        finding.finding_id,
        verification_status="passed_with_review",
        verification_method="test",
        disposition="resolved",
        actor_type="human",
        actor_id="tester",
    )

    review_store.sync_state_findings(project_id)
    assert review_store.get_finding(project_id, finding.finding_id).status == "closed"
    assert review_store.validate_project_state(project_id)["force_finalized_paths"] == []

    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["recommendation"] = "内容已经变化"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    review_store.sync_state_findings(project_id)
    reopened = review_store.get_finding(project_id, finding.finding_id)
    assert reopened.status == "open"
    assert reopened.verification_status == "pending"


def test_finalize_sync_status_closes_without_mutating_raw_generation_status(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["citation_status"] = "force_finalized"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    review_store.sync_state_findings(project_id)
    finding = review_store.read_findings(project_id)[0]
    recommendation = ReviewRecommendation(
        recommendation_id="recommendation-sync-status",
        finding_id=finding.finding_id,
        claim_reviews=[{
            "claim": "当前内容与证据一致",
            "target_path": "/food/items/0/recommendation",
            "original_text": "旧文案",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "素材支持当前表述",
            "verdict": "supported",
            "action": "sync_status",
            "content_action": "keep",
            "evidence_action": "sync_status",
            "rationale": "正文无需改动",
        }],
    )
    review_store.append_recommendation(project_id, recommendation)
    review_store.record_item_decision(
        project_id,
        finding.finding_id,
        recommendation.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="当前内容与证据一致",
        disposition="accepted",
    )

    result = review_store.finalize_item_decisions(project_id, finding.finding_id, recommendation.recommendation_id)
    assert result.outcome == "closed"
    assert result.finding.resolved_fingerprint
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["citation_status"] == "force_finalized"
    assert review_store.validate_project_state(project_id)["force_finalized_paths"] == []


def test_finalize_content_change_creates_patch_before_mutation(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
    )
    recommendation = ReviewRecommendation(
        recommendation_id="recommendation-structured-patch",
        finding_id=finding.finding_id,
        claim_reviews=[{
            "claim": "表述过于具体",
            "target_path": "/food/items/0/recommendation",
            "original_text": "旧文案",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "素材只支持概括表述",
            "verdict": "partial",
            "action": "narrow",
            "content_action": "narrow",
            "evidence_action": "keep",
            "proposed_text": "收窄后的文案",
            "rationale": "避免超出证据",
        }],
    )
    review_store.append_recommendation(project_id, recommendation)
    review_store.record_item_decision(
        project_id,
        finding.finding_id,
        recommendation.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="表述过于具体",
        disposition="accepted",
    )

    result = review_store.finalize_item_decisions(project_id, finding.finding_id, recommendation.recommendation_id)
    assert result.outcome == "awaiting_apply"
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "旧文案"
    patch = review_store.read_patch(project_id, result.patch_id)
    assert patch.operations[0].path == "/food/items/0/recommendation"
    assert patch.operations[0].value == "收窄后的文案"


def test_finalize_rejects_plain_text_replacement_for_object_target(isolated_project):
    project_id, workspace = isolated_project
    path = workspace / "outputs" / "demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"segment": {"point_name": "湖区", "highlight": "旧描述"}}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/demo.json", "json_path": "/segment"},
    )
    recommendation = ReviewRecommendation(
        recommendation_id="recommendation-object-text-guard",
        finding_id=finding.finding_id,
        claim_reviews=[{
            "claim": "路线段描述需要收窄",
            "target_path": "/segment",
            "original_text": "旧描述",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "素材只支持部分表述",
            "verdict": "partial",
            "action": "narrow",
            "content_action": "narrow",
            "evidence_action": "keep",
            "rationale": "需要人工编辑结构化对象",
        }],
    )
    review_store.append_recommendation(project_id, recommendation)
    review_store.record_item_decision(
        project_id,
        finding.finding_id,
        recommendation.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="路线段描述需要收窄",
        disposition="edited",
        edited_text="不能把整个对象替换成这段文本",
    )

    with pytest.raises(ValueError, match="replace 类型不匹配"):
        review_store.finalize_item_decisions(project_id, finding.finding_id, recommendation.recommendation_id)
    assert json.loads(path.read_text(encoding="utf-8"))["segment"]["point_name"] == "湖区"


def test_finalize_accepts_typed_object_edit_and_creates_patch(isolated_project):
    project_id, workspace = isolated_project
    path = workspace / "outputs" / "demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {"point_name": "湖区", "highlight": "旧描述", "sources": ["chunk-1"]}
    updated = {"point_name": "湖区", "highlight": "收窄后的描述", "sources": ["chunk-1", "chunk-2"]}
    path.write_text(json.dumps({"segment": original}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/demo.json", "json_path": "/segment"},
    )
    recommendation = ReviewRecommendation(
        recommendation_id="recommendation-object-structured-edit",
        finding_id=finding.finding_id,
        claim_reviews=[{
            "claim": "路线段描述需要收窄",
            "target_path": "/segment",
            "original_text": "旧描述",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "素材只支持部分表述",
            "verdict": "partial",
            "action": "narrow",
            "content_action": "narrow",
            "evidence_action": "add_source",
            "rationale": "保留对象结构并调整字段",
        }],
    )
    review_store.append_recommendation(project_id, recommendation)
    decision = review_store.record_item_decision(
        project_id,
        finding.finding_id,
        recommendation.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="路线段描述需要收窄",
        disposition="edited",
        edited_value=updated,
        has_edited_value=True,
    )

    assert decision.has_edited_value is True
    assert decision.edited_value == updated
    result = review_store.finalize_item_decisions(project_id, finding.finding_id, recommendation.recommendation_id)
    patch = review_store.read_patch(project_id, result.patch_id)
    assert result.outcome == "awaiting_apply"
    assert patch.operations[0].before == original
    assert patch.operations[0].value == updated
    assert json.loads(path.read_text(encoding="utf-8"))["segment"] == original


def test_recommendation_history_includes_candidates_and_old_decisions(isolated_project):
    project_id, _workspace = isolated_project
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
    )
    current = ReviewRecommendation(
        recommendation_id="recommendation-history-current",
        finding_id=finding.finding_id,
        summary="当前建议",
        claim_reviews=[{
            "claim": "当前条目",
            "target_path": "/food/items/0/recommendation",
            "original_text": "旧文案",
            "evidence_refs": ["chunk-1"],
            "evidence_excerpt": "支持保留",
            "verdict": "supported",
            "action": "keep",
            "rationale": "证据充分",
        }],
    )
    candidate = ReviewRecommendation(
        recommendation_id="recommendation-history-candidate",
        finding_id=finding.finding_id,
        summary="候选建议",
    )
    review_store.append_recommendation(project_id, current)
    review_store.record_item_decision(
        project_id,
        finding.finding_id,
        current.recommendation_id,
        item_key="claim:0",
        item_index=0,
        item_kind="claim",
        item_label="当前条目",
        disposition="keep_original",
    )
    review_store.append_recommendation(project_id, candidate, promote=False)

    history = review_store.build_recommendation_history(project_id, finding.finding_id)
    by_id = {item.recommendation.recommendation_id: item for item in history}
    assert set(by_id) == {current.recommendation_id, candidate.recommendation_id}
    assert by_id[current.recommendation_id].is_current is True
    assert by_id[current.recommendation_id].decision_count == 1
    assert by_id[current.recommendation_id].total_items == 1
    assert by_id[candidate.recommendation_id].is_current is False
    assert by_id[candidate.recommendation_id].decision_count == 0


def test_review_maintenance_is_idempotent_and_preserves_history(isolated_project):
    project_id, workspace = isolated_project
    path = _write_lifestyle(workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["food"]["items"][0]["citation_status"] = "force_finalized"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    review_store.sync_state_findings(project_id)
    canonical = review_store.read_findings(project_id)[0]
    review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "review_runs/copy/workspace/outputs/lifestyle.json", "json_path": "/food/items/0"},
        before=data["food"]["items"][0],
        notes="冻结任务包副本",
    )
    mirror = review_store.read_findings(project_id)[-1]
    review_store.update_finding(project_id, mirror.finding_id, observed_value=data["food"]["items"][0])

    plan = review_store.plan_review_maintenance(project_id)
    assert len(plan["close_review_run_copies"]) == 1
    first = review_store.apply_review_maintenance_plan(project_id, plan)
    second = review_store.apply_review_maintenance_plan(project_id, plan)
    migrated = review_store.get_finding(project_id, mirror.finding_id)
    assert migrated.status == "closed"
    assert migrated.superseded_by == canonical.finding_id
    assert first["changed_finding_ids"]
    assert second["changed_finding_ids"] == []
    assert review_store.validate_project_state(project_id)["force_finalized_paths"] == [
        "outputs/lifestyle.json/food/items/0"
    ]
