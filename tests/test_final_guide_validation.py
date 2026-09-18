from src.workspace.final_guide_validation import validate_final_guide_contract
from src.workspace.final_guide_sections import POI_SECTION_HEADING, REFERENCE_SECTION_HEADING


def _guide(*day_headings: str, checklist_heading: str = "## 行前必备清单") -> str:
    sections = ["# 完整旅行攻略", checklist_heading, "证件、药品和充电设备。"]
    sections.extend(f"{heading}\n" + "详细行程内容。" * 30 for heading in day_headings)
    return "\n\n".join(sections)


def _route(*days: int) -> dict:
    return {"days": [{"day": day, "city": f"城市{day}", "pois": [{"name": f"景点{day}"}]} for day in days]}


def _lifestyle() -> dict:
    return {
        field: {"items": [{"day": 1, "recommendation": f"{field}建议"}]}
        for field in ("food", "outfit", "lodging", "play")
    }


def _pitfall() -> dict:
    return {
        "food": {"items": [{"day": 1, "warning": "高峰期需要排队"}]},
        "outfit": {"items": []},
        "lodging": {"items": []},
        "play": {"items": []},
    }


def test_valid_contract_accepts_heading_variants_and_legacy_merged_field_aliases():
    issues = validate_final_guide_contract(
        {"days": 2},
        _route(1, 2),
        {},
        _lifestyle(),
        _pitfall(),
        _guide("## 📅 Day 1：到达", "### 第 2 天｜城市漫步"),
        {
            "merged_fields": [
                "route",
                "food",
                "lifestyle_agent.outfit",
                "lifestyle.lodging",
                "lifestyle.play",
                "避雷agent",
            ],
            "checklist_included": True,
        },
    )

    assert issues == []


def test_contract_reports_missing_day_checklist_and_upstream_modules():
    issues = validate_final_guide_contract(
        {"days": 2},
        _route(1, 2),
        {},
        _lifestyle(),
        _pitfall(),
        _guide("## Day 1", checklist_heading="## 行前提醒"),
        {"merged_fields": ["route_plan", "lifestyle.food"], "checklist_included": False},
    )

    codes = [issue["code"] for issue in issues]
    assert "missing_day" in codes
    assert "checklist_missing" in codes
    missing_fields = {issue["field"] for issue in issues if issue["code"] == "merged_field_missing"}
    assert missing_fields == {
        "lifestyle.outfit",
        "lifestyle.lodging",
        "lifestyle.play",
        "pitfall",
    }


def test_contract_reports_missing_content_and_invalid_meta():
    issues = validate_final_guide_contract({}, _route(1), {}, {}, {}, None, ["not", "an", "object"])

    assert issues == [
        {
            "code": "final_guide_missing",
            "message": "最终攻略不存在或正文为空",
            "evidence_paths": ["final_guide.md"],
        }
    ]


def test_contract_reports_meta_schema_type_errors():
    issues = validate_final_guide_contract(
        {},
        {},
        {},
        {},
        {},
        _guide("## Day 1"),
        {"merged_fields": "route_plan", "checklist_included": True},
    )

    assert any(issue["code"] == "invalid_final_meta" for issue in issues)


def test_contract_rejects_empty_needs_detail_poi_without_terminal_evidence_gap():
    route = {
        "days": [
            {
                "day": 1,
                "city": "西宁",
                "pois": [{"name": "青海湖", "needs_detail": True}],
            }
        ]
    }
    issues = validate_final_guide_contract(
        {},
        route,
        {},
        {},
        {},
        _guide("## Day 1"),
        {"merged_fields": ["route_plan"], "checklist_included": True},
        poi_details=[
            {
                "day": 1,
                "poi_name": "青海湖",
                "routes": [],
                "citation_status": "passed",
            }
        ],
    )

    assert any(issue["code"] == "poi_detail_content_empty" for issue in issues)


def test_contract_rejects_empty_poi_even_with_terminal_evidence_gap():
    route = {
        "days": [
            {
                "day": 1,
                "city": "西宁",
                "pois": [{"name": "青海湖", "needs_detail": True}],
            }
        ]
    }
    issues = validate_final_guide_contract(
        {},
        route,
        {},
        {},
        {},
        _guide("## Day 1"),
        {"merged_fields": ["route_plan"], "checklist_included": True},
        poi_details=[
            {
                "day": 1,
                "poi_name": "青海湖",
                "routes": [],
                "citation_status": "force_finalized",
                "evidence_gap": True,
                "human_review_required": True,
            }
        ],
    )

    assert any(issue["code"] == "poi_detail_content_empty" for issue in issues)


def test_reference_appendix_does_not_satisfy_minimum_body_length():
    issues = validate_final_guide_contract(
        {},
        {},
        {},
        {},
        {},
        "# 攻略\n\n短正文\n\n## 参考来源\n" + "来源说明" * 100,
        {"merged_fields": [], "checklist_included": False},
    )

    too_short = next(issue for issue in issues if issue["code"] == "final_guide_too_short")
    assert too_short["actual_length"] < 50


def test_poi_appendix_day_heading_does_not_satisfy_main_body_day_coverage():
    guide = (
        _guide("## Day 1")
        + f"\n\n---\n\n{POI_SECTION_HEADING}\n\n### Day2 青城山\n路线详情"
        + f"\n\n---\n\n{REFERENCE_SECTION_HEADING}\n来源"
    )

    issues = validate_final_guide_contract(
        {},
        _route(1, 2),
        {},
        {},
        {},
        guide,
        {"merged_fields": ["route_plan"], "checklist_included": True},
    )

    missing_days = [issue["day"] for issue in issues if issue["code"] == "missing_day"]
    assert missing_days == [2]
