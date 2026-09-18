from __future__ import annotations

import json

import pytest

from src.review import agent as review_agent
from src.review.advisor import ModelReviewResult
from src.review import store as review_store
from src.workspace import store as workspace_store


def _claim_review(*, verdict: str, action: str) -> dict:
    return {
        "claim": "示例断言",
        "original_text": "示例原文",
        "evidence_refs": ["chunk-1"],
        "evidence_excerpt": "示例证据",
        "verdict": verdict,
        "action": action,
        "proposed_text": "保持原文" if action in {"keep", "sync_status"} else "最小改写",
        "rationale": "根据证据判定",
        "confidence": "high",
    }


def test_model_review_result_forces_passed_false_when_claim_needs_change():
    result = ModelReviewResult(
        passed=True,
        claim_reviews=[_claim_review(verdict="partial", action="narrow")],
    )

    assert result.passed is False


def test_model_review_result_keeps_passed_true_for_status_sync():
    result = ModelReviewResult(
        passed=True,
        claim_reviews=[_claim_review(verdict="supported", action="sync_status")],
    )

    assert result.passed is True


def test_claim_review_scope_rejects_wrong_target_path():
    claim = review_agent.ReviewClaimEvidence.model_validate({
        **_claim_review(verdict="supported", action="keep"),
        "target_path": "/routes/0/route_segments/2",
    })

    with pytest.raises(RuntimeError, match="target_path.*不一致"):
        review_agent._validate_claim_reviews_against_scope(
            [{"target_path": "/routes/0/route_segments/1"}],
            [claim],
            stage="测试复核",
        )


def test_claim_review_scope_rejects_unstructured_finding_scope():
    claim = review_agent.ReviewClaimEvidence.model_validate(
        _claim_review(verdict="supported", action="keep")
    )

    with pytest.raises(RuntimeError, match="没有可校验"):
        review_agent._validate_claim_reviews_against_scope([], [claim], stage="测试复核")


def test_disputed_scope_uses_generic_file_pointer(isolated_review_project):
    project_id, workspace = isolated_review_project
    path = workspace / "outputs" / "lifestyle.json"
    path.write_text(
        json.dumps({"food": {"items": [{"day": 1, "recommendation": "旧文案"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
    )

    scope = review_agent._disputed_scope(finding)

    assert len(scope) == 1
    assert scope[0]["target_path"] == "/food/items/0/recommendation"
    assert scope[0]["claim"] == "旧文案"


def test_disputed_scope_strips_state_summary_prefix_before_matching_route(isolated_review_project):
    project_id, _workspace = isolated_review_project
    route_name = "长城三联票深度线（关城+悬壁长城+长城第一墩）"
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day6_demo.json"},
    ).model_copy(update={
        "issue_summary": (
            "历史引用核验未通过，最近一次失败记录：第3轮："
            f"{route_name}途经点「悬壁长城」的失败claim：claim_id=claim-1"
        ),
        "observed_value": {
            "routes": [{
                "route_name": route_name,
                "route_segments": [{
                    "point_name": "悬壁长城",
                    "highlight": "联票包含的景点之一",
                    "suggested_duration": "灵活安排",
                    "sources": [],
                }],
            }],
        },
    })

    scope = review_agent._disputed_scope(finding)

    assert len(scope) == 1
    assert scope[0]["route_name"] == route_name
    assert scope[0]["point_name"] == "悬壁长城"
    assert scope[0]["target_path"] == "/routes/0/route_segments/0"


def test_disputed_scope_rejects_silent_overflow(isolated_review_project):
    project_id, _workspace = isolated_review_project
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day1_demo.json"},
    ).model_copy(update={
        "observed_value": {
            "routes": [{
                "route_name": "完整路线",
                "route_segments": [
                    {"point_name": f"点位{index}", "highlight": "说明", "sources": []}
                    for index in range(review_agent.MAX_REVIEW_SCOPE_ITEMS + 1)
                ],
            }],
        },
    })

    with pytest.raises(ValueError, match="超过单次复核上限"):
        review_agent._disputed_scope(finding)


def test_evidence_packet_finds_alternative_when_direct_reference_is_wrong(
    isolated_review_project, tmp_path, monkeypatch
):
    project_id, _workspace = isolated_review_project
    workspace_store.write_requirements(project_id, "示例城", {"destination": "示例城"})
    material_root = tmp_path / "material_library"
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
    monkeypatch.setattr(review_agent, "MATERIAL_LIBRARY_DIR", material_root)
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day1_demo.json"},
    ).model_copy(update={
        "issue_summary": "第1轮：路线A途经点「滨海木栈道」：当前引用没有对应内容。",
        "observed_value": {
            "routes": [{
                "route_name": "路线A",
                "route_segments": [{
                    "point_name": "滨海木栈道",
                    "highlight": "沿木栈道前往观景平台",
                    "suggested_duration": "灵活安排",
                    "sources": [{"chunk_id": "示例-错误-c001"}],
                }],
            }],
        },
    })

    packet = review_agent._build_disputed_evidence_packet("示例城", finding)

    item = packet["items"][0]
    assert item["direct_evidence"][0]["ref"] == "示例-错误-c001"
    assert item["alternative_candidates"][0]["ref"] == "示例-正确-c004"
    assert "观景平台" in item["alternative_candidates"][0]["text"]


@pytest.fixture()
def isolated_review_project(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "review-agent-project"
    workspace_store.init_project(project_id)
    return project_id, workspace_store.workspace_dir(project_id)


def test_agentic_review_submits_structured_result_without_writing_content(isolated_review_project, monkeypatch):
    project_id, workspace = isolated_review_project
    path = workspace / "outputs" / "lifestyle.json"
    path.write_text(json.dumps({"food": {"items": [{"recommendation": "旧文案"}]}}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="unsupported_specificity",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
        evidence_refs=["chunk-1"],
    )

    class FakeModel:
        model_name = "glm-5.3-flash"
        key_group = "gptload"

    class FakeGraph:
        def __init__(self, tools):
            self.tools = tools

        def invoke(self, state, **kwargs):
            submit = next(tool for tool in self.tools if tool.name == "submit_review")
            submit.invoke({
                "passed": False,
                "issues": ["具体时长缺少依据"],
                "summary": "保留地点，收窄无依据的数字。",
                "proposed_text": "按现场人流弹性安排。",
                "risk_level": "low",
                "confidence": "high",
                "recommended_action": "human_review",
                "uncertainties": [],
                "claim_reviews": [{
                    "claim": "旧文案",
                    "target_path": "/food/items/0/recommendation",
                    "original_text": "旧文案",
                    "evidence_refs": ["chunk-1"],
                    "evidence_excerpt": "素材原文片段",
                    "verdict": "unsupported",
                    "action": "narrow",
                    "proposed_text": "按现场人流弹性安排。",
                    "rationale": "原有时长没有证据。",
                    "confidence": "high",
                }],
            })
            return {"messages": state["messages"]}

    model_groups = []

    def fake_build_chat_model(group, temperature):
        model_groups.append(group)
        return FakeModel()

    monkeypatch.setattr(review_agent, "build_chat_model", fake_build_chat_model)
    monkeypatch.setattr(review_agent, "create_react_agent", lambda model, tools, prompt: FakeGraph(tools))

    recommendation, result = review_agent.generate_agentic_recommendation(project_id, finding.finding_id)

    assert result.summary.startswith("保留地点")
    assert recommendation.context_version == "review-agent-v2"
    assert model_groups == ["gptload"]
    assert recommendation.model_group == "gptload"
    assert recommendation.evidence_refs == ["chunk-1"]
    assert recommendation.claim_reviews[0].verdict == "unsupported"
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "旧文案"
    assert review_store.get_finding(project_id, finding.finding_id).recommendation_id == recommendation.recommendation_id


def test_agentic_review_structures_final_report_when_submit_tool_is_skipped(isolated_review_project, monkeypatch):
    project_id, workspace = isolated_review_project
    path = workspace / "outputs" / "lifestyle.json"
    path.write_text(json.dumps({"food": {"items": [{"recommendation": "保留地点"}]}}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/lifestyle.json", "json_path": "/food/items/0/recommendation"},
    )

    expected = review_agent.ModelReviewResult(
        passed=True,
        issues=[],
        summary="当前内容可保留，只需同步历史状态。",
        proposed_text="",
        claim_reviews=[{
            "claim": "历史 force_finalized 状态",
            "target_path": "/food/items/0/recommendation",
            "original_text": "保留地点",
            "evidence_refs": ["review_history"],
            "evidence_excerpt": "素材支持当前地点",
            "verdict": "supported",
            "action": "sync_status",
            "proposed_text": "保持原文",
            "rationale": "当前内容合理，仅历史状态未同步。",
            "confidence": "high",
        }],
        risk_level="low",
        confidence="high",
        recommended_action="sync_status",
        uncertainties=[],
    )

    class FakeStructured:
        def invoke(self, messages):
            assert "前置 agent 调查报告" in messages[0]["content"]
            return expected

    class FakeModel:
        model_name = "fake-cunai"
        key_group = "cunai"

        def with_structured_output(self, schema):
            assert schema is review_agent.ModelReviewResult
            return FakeStructured()

    class FakeMessage:
        type = "ai"
        content = "素材支持当前地点，历史 force_finalized 只是未同步状态。"

    class FakeGraph:
        def invoke(self, state, **kwargs):
            return {"messages": [*state["messages"], FakeMessage()]}

    monkeypatch.setattr(review_agent, "build_chat_model", lambda group, temperature: FakeModel())
    monkeypatch.setattr(review_agent, "create_react_agent", lambda model, tools, prompt: FakeGraph())

    recommendation, result = review_agent.generate_agentic_recommendation(project_id, finding.finding_id)

    assert result.passed is True
    assert recommendation.context_version == "review-agent-v2"
    assert recommendation.recommended_action == "sync_status"
    assert json.loads(path.read_text(encoding="utf-8"))["food"]["items"][0]["recommendation"] == "保留地点"


def test_merge_batched_results_preserves_each_claim_and_requires_human_review():
    first = ModelReviewResult(
        passed=True,
        summary="batch one",
        claim_reviews=[_claim_review(verdict="supported", action="keep")],
    )
    second_claim = _claim_review(verdict="partial", action="narrow")
    second_claim["claim"] = "时长建议"
    second = ModelReviewResult(
        passed=False,
        summary="batch two",
        claim_reviews=[second_claim],
        uncertainties=["现场人流可能变化"],
    )

    merged = review_agent._merge_batched_results([first, second])

    assert len(merged.claim_reviews) == 2
    assert merged.passed is False
    assert merged.recommended_action == "human_review"
    assert "保留 1 条" in merged.summary
    assert "收窄 1 条" in merged.summary
    assert merged.uncertainties == ["现场人流可能变化"]


def test_disputed_scope_prefers_legacy_recommendation_items(isolated_review_project):
    project_id, _workspace = isolated_review_project
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day1_demo.json"},
    )
    review_store.update_finding(
        project_id,
        finding.finding_id,
        issue_summary="最后只剩途经点「小熊猫区」的截断摘要。",
        observed_value={
                "routes": [{
                    "route_name": "南门精华线（含追花花）",
                    "route_segments": [
                    {"point_name": "小熊猫区", "highlight": "小熊猫", "sources": []},
                    {"point_name": "天鹅湖/湖区", "highlight": "湖区", "sources": []},
                    ],
                }],
            },
    )
    recommendation = review_store.ReviewRecommendation(
        recommendation_id="recommendation-legacy-scope",
        finding_id=finding.finding_id,
        proposed_text=(
            "- 南门精华线「小熊猫区」：最小修改。\n"
            "- 南门精华线「天鹅湖/湖区」：最小修改。"
        ),
    )
    review_store.append_recommendation(project_id, recommendation)

    scope = review_agent._disputed_scope(review_store.get_finding(project_id, finding.finding_id))

    assert [item["point_name"] for item in scope] == ["小熊猫区", "天鹅湖/湖区"]


def test_disputed_scope_uses_most_complete_append_only_recommendation(isolated_review_project):
    project_id, _workspace = isolated_review_project
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day1_demo.json"},
    ).model_copy(update={
        "observed_value": {
            "routes": [{
                "route_name": "南门精华线",
                "route_segments": [
                    {"point_name": "小熊猫区", "highlight": "小熊猫", "sources": []},
                    {"point_name": "天鹅湖/湖区", "highlight": "天鹅湖", "sources": []},
                ],
            }],
        },
    })
    complete_claims = []
    for index, name in enumerate(("小熊猫区", "天鹅湖")):
        claim = _claim_review(verdict="partial", action="narrow")
        claim.update({"claim": name, "target_path": f"/routes/0/route_segments/{index}"})
        complete_claims.append(claim)
    review_store.append_recommendation(project_id, review_store.ReviewRecommendation(
        recommendation_id="recommendation-complete",
        finding_id=finding.finding_id,
        claim_reviews=complete_claims,
        created_at="2026-08-26T01:00:00+00:00",
    ))
    review_store.append_recommendation(project_id, review_store.ReviewRecommendation(
        recommendation_id="recommendation-accidentally-short",
        finding_id=finding.finding_id,
        claim_reviews=[complete_claims[0]],
        created_at="2026-08-26T02:00:00+00:00",
    ))

    scope = review_agent._disputed_scope(finding)

    assert [item["point_name"] for item in scope] == ["小熊猫区", "天鹅湖/湖区"]


def test_latest_complete_claim_projection_overlays_new_partial_review(isolated_review_project):
    project_id, _workspace = isolated_review_project
    finding = review_store.create_finding(
        project_id,
        problem_type="force_finalized_residue",
        target={"file": "outputs/poi_details/day1_demo.json"},
    )
    base_claims = []
    for index, name in enumerate(("小熊猫区", "天鹅湖")):
        claim = _claim_review(verdict="partial", action="narrow")
        claim.update({
            "claim": name,
            "target_path": f"/routes/0/route_segments/{index}",
            "proposed_text": f"base-{name}",
        })
        base_claims.append(claim)
    review_store.append_recommendation(project_id, review_store.ReviewRecommendation(
        recommendation_id="recommendation-base",
        finding_id=finding.finding_id,
        claim_reviews=base_claims,
        created_at="2026-08-26T01:00:00+00:00",
    ))
    replacement = dict(base_claims[0])
    replacement["proposed_text"] = "latest-小熊猫区"
    review_store.append_recommendation(project_id, review_store.ReviewRecommendation(
        recommendation_id="recommendation-partial-refresh",
        finding_id=finding.finding_id,
        claim_reviews=[replacement],
        created_at="2026-08-26T02:00:00+00:00",
    ))

    projected = review_agent._project_latest_complete_claims(project_id, finding.finding_id)

    assert len(projected) == 2
    assert projected[0].proposed_text == "latest-小熊猫区"
    assert projected[1].proposed_text == "base-天鹅湖"


def test_invoke_review_json_parses_plain_model_content():
    expected = ModelReviewResult(
        passed=True,
        summary="支持原文",
        claim_reviews=[_claim_review(verdict="supported", action="keep")],
    )

    class FakeResponse:
        content = expected.model_dump_json()

    class FakeModel:
        def invoke(self, messages):
            assert "JSON Schema" in messages[0]["content"]
            return FakeResponse()

    parsed = review_agent._invoke_review_json(FakeModel(), "review")

    assert parsed.summary == "支持原文"
    assert parsed.claim_reviews[0].action == "keep"


def test_consistency_review_catches_duration_retained_after_being_called_unsupported():
    claim = review_agent.ReviewClaimEvidence.model_validate({
        **_claim_review(verdict="partial", action="repair_reference"),
        "original_text": "小熊猫区。约20-30分钟",
        "proposed_text": "保留小熊猫区，约20-30分钟",
        "rationale": "地点有依据，但时长无依据。",
    })

    assert review_agent._claim_needs_consistency_review(claim) is True
    assert review_agent._claim_needs_web_evidence(claim) is False


def test_refine_batched_claims_uses_web_fallback_and_replaces_only_selected_claim(monkeypatch):
    finding = review_store.ReviewFinding(
        finding_id="finding-demo",
        project_id="project-demo",
        problem_type="force_finalized_residue",
    )
    stable = review_agent.ReviewClaimEvidence.model_validate(
        _claim_review(verdict="supported", action="keep")
    )
    unresolved_data = _claim_review(verdict="unsupported", action="rerun")
    unresolved_data.update({
        "claim": "天鹅湖",
        "target_path": "/routes/0/route_segments/6",
        "original_text": "天鹅湖。约20分钟",
        "evidence_refs": ["material_search:天鹅湖"],
        "evidence_excerpt": "本地素材未命中",
        "proposed_text": "保持原文",
        "rationale": "需要补证。",
    })
    unresolved = review_agent.ReviewClaimEvidence.model_validate(unresolved_data)
    merged = ModelReviewResult(
        passed=False,
        claim_reviews=[stable, unresolved],
    )
    items = [
        {"point_name": "稳定条目", "target_path": ""},
        {"point_name": "天鹅湖/湖区", "target_path": "/routes/0/route_segments/6"},
    ]
    monkeypatch.setattr(
        review_agent,
        "_web_evidence_for_item",
        lambda *args, **kwargs: [{"query": "成都 天鹅湖 官方", "result": "官方观光车设天鹅湖站"}],
    )
    refined_data = _claim_review(verdict="partial", action="add_source")
    refined_data.update({
        "claim": "天鹅湖",
        "target_path": "/routes/0/route_segments/6",
        "original_text": "天鹅湖。约20分钟",
        "evidence_refs": ["search:成都 天鹅湖 官方"],
        "evidence_excerpt": "官方观光车设天鹅湖站",
        "proposed_text": "保留天鹅湖，停留时长视现场安排。",
        "rationale": "官方证据支持点位，但不支持固定时长。",
    })
    refined_result = ModelReviewResult(
        passed=False,
        claim_reviews=[refined_data],
    )
    monkeypatch.setattr(review_agent, "_invoke_review_json", lambda model, prompt: refined_result)

    result = review_agent._refine_batched_claims(
        "project-demo",
        finding,
        "成都",
        object(),
        items,
        merged,
        batch_size=3,
    )

    assert result.claim_reviews[0].action == "keep"
    assert result.claim_reviews[1].action == "add_source"
    assert result.claim_reviews[1].proposed_text.endswith("视现场安排。")
    assert "需重跑" not in result.summary
