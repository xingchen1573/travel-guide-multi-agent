from __future__ import annotations

import pytest

from src.review import store as review_store
from src.workspace import store
from src.workspace.final_guide_blocks import collect_upstream_claims
from src.workspace.final_guide_sections import (
    REFERENCE_SECTION_HEADING,
    compose_final_guide_sections,
)
from src.workspace.final_guide_validation import validate_final_guide_contract
from src.workspace.schema import RouteClaim, RouteDay, RoutePlan


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "guide-blocks-test"
    store.init_project(project_id)
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    notes="上午游览武侯祠。",
                    claims=[
                        RouteClaim(
                            claim_id="route-claim-wuhou",
                            target_path="notes",
                            subject="武侯祠",
                            claim_text="上午游览武侯祠。",
                            claim_type="schedule",
                            support_mode="user_constraint",
                        )
                    ],
                )
            ]
        ),
    )
    return project_id


def _blocks() -> list[dict]:
    return [
        {
            "section": "标题",
            "markdown": "# 成都一日攻略",
            "mapping_mode": "editorial",
            "mapping_note": "章节结构",
        },
        {
            "section": "Day1路线",
            "markdown": "## Day 1\n\n上午游览武侯祠。" + "行程说明。" * 45,
            "mapping_mode": "upstream",
            "upstream_claim_ids": ["route-claim-wuhou"],
        },
        {
            "section": "行前清单",
            "markdown": "## 行前清单\n\n证件、药品和充电设备。",
            "mapping_mode": "editorial",
            "mapping_note": "整合Agent根据行程结构生成的清单",
        },
    ]


def test_write_blocks_renders_body_and_freezes_current_claim_snapshot(isolated_project):
    output = store.write_final_guide_blocks(
        isolated_project,
        "integration_agent",
        _blocks(),
        {"merged_fields": ["route_plan"], "checklist_included": True},
    )

    guide = store.read_workspace(isolated_project, "final_guide.md")
    meta = store.read_workspace(isolated_project, "final_guide.meta.json")
    persisted = store.read_final_guide_blocks(isolated_project)

    assert "## Day 1" in guide
    assert output.blocks[1].block_id.startswith("guide-block-")
    assert output.blocks[1].claim_snapshots[0].claim_text == "上午游览武侯祠。"
    assert output.blocks[1].claim_snapshots[0].usage_key == "route:day:1"
    assert output.blocks[1].claim_snapshots[0].source_path.endswith("/claims/0")
    assert meta["body_source"] == "blocks_v1"
    assert meta["blocks_count"] == 3
    assert meta["mapped_claim_count"] == 1
    assert persisted is not None


def test_write_blocks_rejects_dangling_and_ambiguous_mappings(isolated_project):
    dangling = _blocks()
    dangling[1]["upstream_claim_ids"] = ["route-claim-does-not-exist"]
    with pytest.raises(ValueError, match="不存在的上游claim"):
        store.write_final_guide_blocks(isolated_project, "integration_agent", dangling, {})

    missing_ids = _blocks()
    missing_ids[1]["upstream_claim_ids"] = []
    with pytest.raises(ValueError, match="必须填upstream_claim_ids"):
        store.write_final_guide_blocks(isolated_project, "integration_agent", missing_ids, {})

    unverified = _blocks()
    unverified[1] = {
        "section": "待复核提示",
        "markdown": "具体营业时间请出发前复核。",
        "mapping_mode": "unverified",
    }
    with pytest.raises(ValueError, match="必须说明mapping_note"):
        store.write_final_guide_blocks(isolated_project, "integration_agent", unverified, {})


def test_program_owned_appendix_preserves_blocks_but_body_rewrite_invalidates_them(isolated_project):
    store.write_final_guide_blocks(
        isolated_project,
        "integration_agent",
        _blocks(),
        {"merged_fields": ["route_plan"], "checklist_included": True},
    )
    guide = store.read_workspace(isolated_project, "final_guide.md")
    body = guide.strip()
    with_references = compose_final_guide_sections(
        body,
        reference_appendix=f"{REFERENCE_SECTION_HEADING}\n\n来源表",
    )
    meta = store.read_workspace(isolated_project, "final_guide.meta.json")
    store.write_final_guide(isolated_project, "integration_agent", with_references, meta)
    assert store.read_final_guide_blocks(isolated_project) is not None
    assert store.read_workspace(isolated_project, "final_guide.meta.json")["body_source"] == "blocks_v1"

    store.write_final_guide(isolated_project, "integration_agent", "# 人工改写正文", meta)
    assert store.read_final_guide_blocks(isolated_project) is None
    assert store.read_workspace(isolated_project, "final_guide.meta.json")["body_source"] == "legacy_markdown"


def test_contract_requires_blocks_only_when_upstream_claims_exist(isolated_project):
    route = store.read_workspace(isolated_project, "route_plan.json")
    guide = "# 成都攻略\n\n## Day 1\n\n" + "详细行程。" * 45 + "\n\n## 行前清单\n\n证件和药品。"
    meta = {"merged_fields": ["route_plan"], "checklist_included": True}

    issues = validate_final_guide_contract({}, route, {}, {}, {}, guide, meta)
    assert "final_guide_blocks_missing" in {issue["code"] for issue in issues}

    legacy_route = {
        "days": [{"day": 1, "city": "成都", "notes": "上午游览武侯祠。", "claims": []}]
    }
    legacy_issues = validate_final_guide_contract({}, legacy_route, {}, {}, {}, guide, meta)
    assert "final_guide_blocks_missing" not in {issue["code"] for issue in legacy_issues}


def test_contract_accepts_current_program_generated_blocks(isolated_project):
    store.write_final_guide_blocks(
        isolated_project,
        "integration_agent",
        _blocks(),
        {"merged_fields": ["route_plan"], "checklist_included": True},
    )
    issues = validate_final_guide_contract(
        {},
        store.read_workspace(isolated_project, "route_plan.json"),
        {},
        {},
        {},
        store.read_workspace(isolated_project, "final_guide.md"),
        store.read_workspace(isolated_project, "final_guide.meta.json"),
        store.read_workspace(isolated_project, "final_guide.blocks.json"),
    )
    assert issues == []


def test_deliverable_projection_keeps_valid_claims_inside_force_finalized_item():
    route = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "citation_status": "force_finalized",
                "claims": [
                    {
                        "claim_id": "claim-pass",
                        "claim_text": "用户确认第一天去武侯祠。",
                        "claim_type": "schedule",
                        "support_mode": "user_constraint",
                    },
                    {
                        "claim_id": "claim-fail",
                        "claim_text": "武侯祠无需预约。",
                        "claim_type": "reservation",
                        "support_mode": "direct",
                    },
                    {
                        "claim_id": "claim-estimate",
                        "claim_text": "建议预留2小时游览。",
                        "claim_type": "duration",
                        "support_mode": "estimate",
                    },
                ],
            }
        ]
    }
    evidence = {
        "route": [
            {"claim_id": "claim-pass", "result": "pass"},
            {"claim_id": "claim-fail", "result": "fail"},
            {"claim_id": "claim-estimate", "result": "not_checked"},
        ]
    }

    projected = collect_upstream_claims(route, None, None, evidence_checks=evidence)

    assert set(projected) == {"claim-pass", "claim-estimate"}
    assert projected["claim-estimate"].support_mode == "estimate"


def test_closed_false_positive_review_restores_blocked_claim():
    route = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "claims": [
                    {
                        "claim_id": "claim-reviewed",
                        "claim_text": "上午游览武侯祠。",
                        "claim_type": "schedule",
                        "support_mode": "direct",
                    }
                ],
            }
        ]
    }
    evidence = {"route": [{"claim_id": "claim-reviewed", "result": "fail"}]}
    reviews = [
        {
            "finding_id": "finding-route-day-1",
            "canonical_key": "route_day:1",
            "status": "closed",
            "disposition": "false_positive",
            "verification_status": "false_positive",
        }
    ]

    projected = collect_upstream_claims(
        route,
        None,
        None,
        evidence_checks=evidence,
        review_findings=reviews,
    )

    assert set(projected) == {"claim-reviewed"}


def test_final_blocks_reject_machine_failed_claim_until_review_passes(isolated_project):
    store.write_evidence_checks(
        isolated_project,
        "route",
        [
            {
                "check_id": "check-route-claim-wuhou",
                "target_key": "route:day:1",
                "claim_id": "route-claim-wuhou",
                "result": "fail",
            }
        ],
    )
    with pytest.raises(ValueError, match="不存在的上游claim"):
        store.write_final_guide_blocks(
            isolated_project,
            "integration_agent",
            _blocks(),
            {"merged_fields": ["route_plan"], "checklist_included": True},
        )

    finding = review_store.create_finding(
        isolated_project,
        problem_type="evaluator_false_positive",
        target={"file": "route_plan.json", "json_path": "/days/0"},
    )
    review_store.update_finding(
        isolated_project,
        finding.finding_id,
        canonical_key="route_day:1",
        status="closed",
        disposition="false_positive",
        verification_status="false_positive",
    )

    output = store.write_final_guide_blocks(
        isolated_project,
        "integration_agent",
        _blocks(),
        {"merged_fields": ["route_plan"], "checklist_included": True},
    )
    assert output.blocks[1].upstream_claim_ids == ["route-claim-wuhou"]
