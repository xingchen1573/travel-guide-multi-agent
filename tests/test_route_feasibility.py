from __future__ import annotations

import json
from datetime import date

import pytest

from src.materials import route_feasibility_check as feasibility
from src.workspace import store
from src.workspace.schema import (
    RouteDay,
    RouteFeasibilityCheck,
    RouteFeasibilityDateContext,
    RouteFeasibilityEvidence,
    RouteFeasibilityReport,
    RouteExecutionPlan,
    RouteOperationalCommitment,
    RoutePlan,
    RoutePoi,
)


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "route-feasibility-test"
    store.init_project(project_id)
    return project_id


def test_targets_include_every_poi_without_using_needs_detail_as_risk_signal():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "敦煌",
                "pois": [
                    {"name": "莫高窟", "needs_detail": True},
                    {"name": "沙洲夜市", "needs_detail": False},
                ],
                "notes": "",
            }
        ]
    }
    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})
    poi_targets = [item for item in targets if item["target_type"] == "poi"]

    assert [item["target"] for item in poi_targets] == ["莫高窟", "沙洲夜市"]
    assert poi_targets[0]["categories"] == ["poi_operational_status"]
    assert poi_targets[1]["categories"] == ["poi_operational_status"]


def test_needs_detail_and_name_suffix_do_not_imply_ticket_or_opening_checks():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [
                    {
                        "name": "东郊记忆",
                        "needs_detail": True,
                        "needs_detail_reason": "大型工业遗产文创园区，适合规划拍照动线",
                    }
                ],
                "notes": "14:00到达",
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})

    assert "poi_kind" not in targets[0]
    assert targets[0]["categories"] == ["poi_operational_status"]
    assert "预约" not in targets[0]["query"]
    assert "最新状态 官方公告" in targets[0]["query"]


def test_query_templates_follow_explicit_claim_categories_not_poi_kind():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [
                    {"name": "武侯祠", "needs_detail": True},
                    {"name": "玉林·芳华街"},
                    {"name": "玉林小酒馆"},
                ],
                "notes": "",
                "claims": [
                    {
                        "subject": "武侯祠",
                        "claimed_entities": ["武侯祠"],
                        "claim_text": "武侯祠需要实名预约，17:00停止入园",
                    },
                    {
                        "subject": "玉林小酒馆",
                        "claimed_entities": ["玉林小酒馆"],
                        "claim_text": "玉林小酒馆营业时间为18:00至次日02:00",
                    },
                ],
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})
    by_name = {target["target"]: target for target in targets}

    assert "poi_kind" not in by_name["武侯祠"]
    assert "预约" in by_name["武侯祠"]["query"]
    assert "停止入园" in by_name["武侯祠"]["query"]
    assert "玉林 芳华街" in by_name["玉林·芳华街"]["query"]
    assert by_name["玉林·芳华街"]["categories"] == ["poi_operational_status"]
    assert "预约" not in by_name["玉林·芳华街"]["query"]
    assert "opening_window" in by_name["玉林小酒馆"]["categories"]
    assert "营业时间" in by_name["玉林小酒馆"]["query"]


def test_resolved_entity_type_changes_only_baseline_route_checks():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "青海",
                "pois": [
                    {"name": "兰州西站"},
                    {"name": "青海湖"},
                    {"name": "德令哈"},
                ],
                "notes": "",
            }
        ]
    }
    route_entities = {
        "entities": [
            {
                "day": 1,
                "poi_index": 0,
                "formal_name": "兰州西站",
                "status": "resolved",
                "type": "交通设施服务;火车站;火车站",
            },
            {
                "day": 1,
                "poi_index": 1,
                "formal_name": "青海湖",
                "status": "resolved",
                "type": "风景名胜;风景名胜;国家级景点",
            },
            {
                "day": 1,
                "poi_index": 2,
                "formal_name": "德令哈",
                "status": "resolved",
                "type": "地名地址信息;普通地名;区县级地名",
            },
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(
        route_plan,
        {},
        route_entities=route_entities,
    )
    by_name = {target["target"]: target for target in targets}

    assert set(by_name) == {"兰州西站", "青海湖"}
    assert by_name["兰州西站"]["entity_kind"] == "transport_hub"
    assert by_name["兰州西站"]["route_role"] == "day_start"
    assert by_name["兰州西站"]["check_profile"] == "transport_service"
    assert "车站运营" in by_name["兰州西站"]["query"]
    assert by_name["青海湖"]["entity_kind"] == "visit_site"
    assert by_name["青海湖"]["check_profile"] == "visit_site"
    assert "最新状态 官方公告" in by_name["青海湖"]["query"]


def test_settlement_keeps_explicit_time_sensitive_claim_check():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "敦煌",
                "pois": [{"name": "敦煌"}],
                "claims": [
                    {
                        "subject": "敦煌",
                        "claimed_entities": ["敦煌"],
                        "claim_text": "敦煌游客中心17:00停止入场",
                    }
                ],
            }
        ]
    }
    route_entities = {
        "entities": [
            {
                "day": 1,
                "poi_index": 0,
                "formal_name": "敦煌",
                "status": "resolved",
                "type": "地名地址信息;普通地名;区县级地名",
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(
        route_plan,
        {},
        route_entities=route_entities,
    )

    assert len(targets) == 1
    assert targets[0]["categories"] == ["last_entry", "schedule_vs_opening_window"]
    assert targets[0]["route_role"] == "single_stop"


def test_settlement_keeps_explicit_operational_status_claim():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "敦煌",
                "pois": [{"name": "敦煌"}],
                "claims": [
                    {
                        "subject": "敦煌",
                        "claimed_entities": ["敦煌"],
                        "claim_text": "敦煌游客中心当前正常开放",
                    }
                ],
            }
        ]
    }
    route_entities = {
        "entities": [
            {
                "day": 1,
                "poi_index": 0,
                "formal_name": "敦煌",
                "status": "resolved",
                "type": "地名地址信息;普通地名;区县级地名",
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(
        route_plan,
        {},
        route_entities=route_entities,
    )

    assert len(targets) == 1
    assert targets[0]["categories"] == ["poi_operational_status"]
    assert targets[0]["check_profile"] == "anchor_claim_only"


def test_unknown_entity_keeps_conservative_baseline_check():
    targets, _ = feasibility.build_route_feasibility_targets(
        {
            "days": [
                {
                    "day": 1,
                    "city": "海西",
                    "pois": [{"name": "东台吉乃尔湖"}],
                    "notes": "",
                }
            ]
        },
        {},
        route_entities={"entities": []},
    )

    assert len(targets) == 1
    assert targets[0]["categories"] == ["poi_operational_status"]
    assert targets[0]["entity_kind"] == "unknown"
    assert targets[0]["check_profile"] == "unknown_conservative"


def test_lodging_anchor_uses_claim_driven_profile_without_scenic_opening_check():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "敦煌",
                "pois": [{"name": "敦煌酒店"}],
                "notes": "晚上入住敦煌酒店",
            }
        ]
    }
    route_entities = {
        "entities": [
            {
                "day": 1,
                "poi_index": 0,
                "formal_name": "敦煌酒店",
                "status": "resolved",
                "type": "住宿服务;宾馆酒店;宾馆酒店",
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(
        route_plan,
        {},
        route_entities=route_entities,
    )

    assert targets == []


def test_cross_day_operational_commitment_is_matched_to_target_poi():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "峨眉山市",
                "pois": [{"name": "报国寺"}],
                "notes": "晚上处理次日票务。",
            },
            {
                "day": 2,
                "city": "峨眉山市",
                "pois": [{"name": "金顶"}],
                "notes": "清晨上山。",
            },
        ]
    }
    route_execution = {
        "operational_commitments": [
            {
                "commitment_id": "reserve-jinding",
                "action": "预约金顶门票",
                "category": "reservation",
                "target_day": 2,
                "target_poi_index": 0,
                "target_name": "金顶",
                "planned_on_day": 1,
                "status": "planned",
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(
        route_plan,
        {},
        route_execution=route_execution,
    )
    target = next(item for item in targets if item["day"] == 2 and item["target"] == "金顶")

    assert "reservation_or_quota" in target["categories"]
    assert target["matched_commitment_ids"] == ["reserve-jinding"]
    assert target["operational_commitments"][0]["planned_on_day"] == 1


def test_reservation_warning_is_reconciled_when_commitment_already_exists(
    isolated_project,
    monkeypatch,
):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="峨眉山市", pois=[RoutePoi(name="金顶")])]),
    )
    store.write_route_execution(
        isolated_project,
        "route_agent",
        RouteExecutionPlan(
            operational_commitments=[
                RouteOperationalCommitment(
                    action="提前预约金顶门票",
                    category="reservation",
                    target_day=1,
                    target_poi_index=0,
                    target_name="金顶",
                    planned_on_day=0,
                )
            ]
        ),
    )

    class SearchTool:
        def invoke(self, payload):
            return "官方提示金顶实行预约限流。"

    monkeypatch.setattr(
        feasibility,
        "_judge_poi_target",
        lambda target, web_result, **kwargs: feasibility.PoiFeasibilityVerdict(
            findings=[
                feasibility.PoiFeasibilityFinding(
                    category=category,
                    status="warning" if category == "reservation_or_quota" else "passed",
                    summary="路线未安排预约" if category == "reservation_or_quota" else "当前正常运营",
                    suggested_action="补充预约措施" if category == "reservation_or_quota" else "",
                )
                for category in target["categories"]
            ]
        ),
    )
    monkeypatch.setattr(
        feasibility,
        "make_web_search_tools",
        lambda *args, **kwargs: [SearchTool()],
    )

    report = feasibility.verify_route_feasibility(isolated_project, "峨眉山")
    reservation = next(
        item for item in report.checks if item.category == "reservation_or_quota"
    )

    assert reservation.status == "passed"
    assert reservation.coverage_status == "covered"
    assert reservation.matched_commitment_ids
    assert reservation.missing_requirement_codes == []
    assert "已登记预约措施" in reservation.summary


def test_explicit_natural_state_claim_gets_state_change_categories():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "海西",
                "pois": [{"name": "东台吉乃尔湖"}],
                "notes": "",
                "claims": [
                    {
                        "subject": "东台吉乃尔湖",
                        "claimed_entities": ["东台吉乃尔湖"],
                        "claim_text": "东台吉乃尔湖枯水期可能出现水位下降或干涸",
                    }
                ],
            }
        ]
    }
    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})
    categories = targets[0]["categories"]
    assert "seasonal_closure" in categories
    assert "physical_state_change" in categories


def test_descriptive_open_or_renovated_words_do_not_create_operational_claims():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [
                    {
                        "name": "东郊记忆",
                        "brief": "老厂改造形成的开放式工业遗产文创园区",
                    }
                ],
                "notes": "",
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})

    assert targets[0]["categories"] == ["poi_operational_status"]


def test_structured_region_entity_does_not_attach_to_longer_merchant_name():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [{"name": "玉林·芳华街"}, {"name": "玉林小酒馆"}],
                "notes": "",
                "claims": [
                    {
                        "subject": "玉林",
                        "claimed_entities": ["玉林"],
                        "claim_text": "玉林片区全天开放",
                    }
                ],
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})
    by_name = {target["target"]: target for target in targets}

    assert "opening_window" in by_name["玉林·芳华街"]["categories"]
    assert by_name["玉林小酒馆"]["categories"] == ["poi_operational_status"]


def test_targets_add_schedule_and_traveler_qualification_checks():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "敦煌",
                "notes": "",
                "pois": [{"name": "莫高窟", "needs_detail": False}],
                "claims": [
                    {
                        "subject": "莫高窟",
                        "claimed_entities": ["莫高窟"],
                        "claim_text": "莫高窟需要实名预约，15:30到达并在16:00入园",
                    },
                    {
                        "subject": "莫高窟",
                        "claimed_entities": ["莫高窟"],
                        "claim_text": "老人需要携带身份证核验入场资格",
                    },
                ],
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(
        route_plan, {"notes": "带老人同行"}
    )

    assert "schedule_vs_opening_window" in targets[0]["categories"]
    assert "entry_qualification" in targets[0]["categories"]
    assert "带老人同行" in targets[0]["query"]


def test_traveler_constraints_exclude_unrelated_preferences():
    requirements = {"notes": "有老人同行，膝盖不好；对寺庙素斋感兴趣；不吃辣"}

    constraints = feasibility._traveler_constraints(requirements)

    assert constraints == "有老人同行，膝盖不好"


def test_named_poi_experience_goal_adds_alignment_check_and_search_terms():
    route_plan = {
        "days": [
            {
                "day": 3,
                "city": "峨眉山市",
                "pois": [{"name": "金顶"}, {"name": "洗象池"}],
                "notes": "上午10:00到达金顶游览",
            }
        ]
    }
    requirements = {"style": "登山朝圣，想体验金顶日出和云海，同时轻度徒步不想太累"}

    targets, _ = feasibility.build_route_feasibility_targets(route_plan, requirements)
    by_name = {target["target"]: target for target in targets}

    assert by_name["金顶"]["user_goal_text"] == "想体验金顶日出和云海"
    assert "user_goal_alignment" in by_name["金顶"]["categories"]
    assert "日出和云海" in by_name["金顶"]["query"]
    assert "user_goal_alignment" not in by_name["洗象池"]["categories"]


def test_named_poi_negative_preference_does_not_become_experience_goal():
    route_plan = {
        "days": [{"day": 1, "city": "成都", "pois": [{"name": "宽窄巷子"}], "notes": ""}]
    }

    targets, _ = feasibility.build_route_feasibility_targets(
        route_plan, {"style": "不想去宽窄巷子，希望节奏轻松"}
    )

    assert targets[0]["user_goal_text"] == ""
    assert "user_goal_alignment" not in targets[0]["categories"]


def test_goal_alignment_prompt_requires_an_operational_plan_not_a_guarantee():
    prompt = feasibility._poi_judge_prompt(
        {
            "target": "金顶",
            "city": "峨眉山市",
            "day": 3,
            "date_precision": "unknown",
            "applicable_date": "2026-09-01",
            "categories": ["user_goal_alignment"],
            "notes": "上午10:00到达金顶",
            "traveler_constraints": "有老人同行",
            "user_goal_text": "想体验金顶日出和云海",
        },
        "官方信息显示景区正常开放",
    )

    assert "只写了景点游览却没有真正安排该体验时判warning" in prompt
    assert "不得要求保证日出、云海等自然现象必然出现" in prompt


def test_goal_alignment_cannot_pass_when_named_experience_is_absent_from_notes(
    isolated_project, monkeypatch
):
    target = {
        "day": 3,
        "target": "金顶",
        "target_type": "poi",
        "city": "峨眉山市",
        "categories": ["user_goal_alignment"],
        "applicable_date": "2026-09-01",
        "query": "峨眉山市 金顶 日出 云海",
        "notes": "建议早上尽早出发，索道约6:00开放",
        "user_goal_text": "想体验金顶日出和云海",
    }
    monkeypatch.setattr(
        feasibility,
        "_judge_poi_target",
        lambda target, web_result, **kwargs: feasibility.PoiFeasibilityVerdict(
            findings=[
                feasibility.PoiFeasibilityFinding(
                    category="user_goal_alignment",
                    status="passed",
                    summary="尽早出发即可",
                )
            ]
        ),
    )

    checks = feasibility._run_poi_target(
        isolated_project,
        target,
        type("Search", (), {"invoke": lambda self, args: "景区正常开放"})(),
    )

    assert checks[0].status == "warning"
    assert "日出、云海" in checks[0].summary


def test_targets_use_exact_trip_date_and_edit_scope():
    route_plan = {
        "days": [
            {"day": 1, "city": "成都", "pois": [{"name": "武侯祠"}], "notes": ""},
            {"day": 2, "city": "成都", "pois": [{"name": "熊猫基地"}], "notes": ""},
        ]
    }
    requirements = {"travel_start_date": "2027-01-31", "travel_date_precision": "exact"}
    targets, context = feasibility.build_route_feasibility_targets(route_plan, requirements, days=[2])
    assert context.precision == "exact"
    assert len(targets) == 1
    assert targets[0]["day"] == 2
    assert targets[0]["applicable_date"] == "2027-02-01"


def test_unknown_trip_date_does_not_force_warning_in_judge_contract():
    prompt = feasibility._poi_judge_prompt(
        {
            "target": "宽窄巷子",
            "city": "成都",
            "day": 1,
            "date_precision": "unknown",
            "applicable_date": "2026-08-29",
            "categories": ["poi_operational_status"],
            "notes": "开放式街区顺路游览",
            "traveler_constraints": "",
        },
        "官方信息显示当前正常开放",
    )

    assert "不能仅因此把已核实的当前事实降为warning" in prompt
    assert "查询基准日不是旅行日期" in prompt
    assert "不得据此推断用户出行日的星期、季节、节假日、余票或名额" in prompt
    assert "只有依赖延时开放、特定季节或其他日期条件才能按原计划执行时判warning" in prompt
    assert "可以把出发前再次确认写入suggested_action" in prompt
    assert "若notes没有出发前复核提醒，返回warning" not in prompt


def test_stop_entry_claim_creates_last_entry_and_schedule_checks():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [{"name": "文殊院"}],
                "notes": "文殊院8:00-17:00开放，16:30停止入场，计划9:30到达。",
            }
        ]
    }

    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})

    assert "last_entry" in targets[0]["categories"]
    assert "schedule_vs_opening_window" in targets[0]["categories"]
    assert "停止入园" in targets[0]["query"]


def test_unknown_has_higher_priority_than_warning():
    checks = [
        RouteFeasibilityCheck(
            check_id="1", day=1, target="A", target_type="poi", category="x", status="warning"
        ),
        RouteFeasibilityCheck(
            check_id="2", day=1, target="B", target_type="poi", category="x", status="unknown"
        ),
    ]
    assert feasibility._overall_status(checks) == "unknown"


def test_poi_feasibility_finding_accepts_reason_as_summary_alias():
    finding = feasibility.PoiFeasibilityFinding.model_validate(
        {
            "category": "poi_operational_status",
            "status": "warning",
            "reason": "实际出行日前需要再次核实开放状态",
            "requires_structure_change": False,
        }
    )

    assert finding.summary == "实际出行日前需要再次核实开放状态"
    assert finding.model_dump() == {
        "category": "poi_operational_status",
        "status": "warning",
        "summary": "实际出行日前需要再次核实开放状态",
        "requires_structure_change": False,
        "suggested_action": "",
    }


@pytest.mark.parametrize(
    ("payload", "expected_summary", "expected_action"),
    [
        ({"rationale": "开放式街区可正常访问"}, "开放式街区可正常访问", ""),
        (
            {"evidence": "官方页面显示正常开放", "recommendation": "出发前再次确认"},
            "官方页面显示正常开放",
            "出发前再次确认",
        ),
    ],
)
def test_poi_feasibility_finding_normalizes_additional_model_variants(
    payload, expected_summary, expected_action
):
    finding = feasibility.PoiFeasibilityFinding.model_validate(
        {
            "category": "poi_operational_status",
            "status": "warning",
            "requires_structure_change": False,
            **payload,
        }
    )

    assert finding.summary == expected_summary
    assert finding.suggested_action == expected_action


def test_operational_timeline_later_reopening_overrides_older_closure():
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="blocked",
        summary="6月17日关闭，未确认恢复",
        requires_structure_change=True,
    )
    events = [
        feasibility.PoiOperationalEvent(
            entity="乌素特水上雅丹",
            event_type="closure",
            event_date="2026-06-17",
            source_type="government",
        ),
        feasibility.PoiOperationalEvent(
            entity="水上雅丹",
            event_type="reopening",
            event_date="2026年6月22日",
            publication_date="2026-06-25",
            source_type="official",
            summary="景区已恢复开放",
        ),
    ]

    updated = feasibility._apply_operational_timeline(
        {"target": "乌素特（水上）雅丹"},
        finding,
        events,
    )

    assert updated.status == "passed"
    assert updated.requires_structure_change is False
    assert "关闭事件：2026-06-17" in updated.summary
    assert "恢复事件：2026-06-22" in updated.summary
    assert "未来出行前" in updated.suggested_action


def test_operational_timeline_does_not_mix_different_entities_or_undated_events():
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="warning",
        summary="当前状态待确认",
    )
    events = [
        feasibility.PoiOperationalEvent(
            entity="翡翠湖",
            event_type="reopening",
            event_date="2026-06-27",
            source_type="official",
        ),
        feasibility.PoiOperationalEvent(
            entity="水上雅丹",
            event_type="reopening",
            event_date="",
            source_type="official",
        ),
    ]

    assert feasibility._apply_operational_timeline(
        {"target": "水上雅丹"}, finding, events
    ) == finding


def test_operational_timeline_latest_closure_blocks_current_status():
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="passed",
        summary="常规页面显示开放",
    )
    events = [
        feasibility.PoiOperationalEvent(
            entity="莫高窟",
            event_type="reopening",
            event_date="2026-06-01",
            source_type="official",
        ),
        feasibility.PoiOperationalEvent(
            entity="莫高窟",
            event_type="closure",
            event_date="2026-09-02",
            source_type="official",
            summary="临时闭园",
            applies_on_applicable_date=True,
        ),
    ]

    updated = feasibility._apply_operational_timeline(
        {
            "target": "莫高窟",
            "date_precision": "exact",
            "applicable_date": "2026-09-08",
        },
        finding,
        events,
    )

    assert updated.status == "blocked"
    assert updated.requires_structure_change is True
    assert "2026-09-02" in updated.summary


@pytest.mark.parametrize(
    ("target_name", "event_date", "summary"),
    [
        (
            "茶卡盐湖",
            "2025-11-01",
            "冬季优惠和运营安排已于2026-02-28结束",
        ),
        (
            "兰州",
            "2022-03-01",
            "历史防疫期间曾限制跨区域流动",
        ),
    ],
)
def test_operational_timeline_keeps_passed_when_restriction_does_not_apply_on_trip_date(
    target_name, event_date, summary
):
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="passed",
        summary="当前规则支持按计划执行",
    )
    events = [
        feasibility.PoiOperationalEvent(
            entity=target_name,
            event_type="restriction",
            event_date=event_date,
            source_type="official",
            summary=summary,
            applies_on_applicable_date=False,
        )
    ]

    updated = feasibility._apply_operational_timeline(
        {
            "target": target_name,
            "date_precision": "exact",
            "applicable_date": "2026-09-26",
        },
        finding,
        events,
    )

    assert updated == finding


@pytest.mark.parametrize(
    ("target_name", "summary"),
    [
        ("黑独山", "当前实行分区管理，部分区域禁止进入"),
        ("鸣沙山月牙泉", "当前季节执行限定开放时段"),
    ],
)
def test_operational_timeline_warns_when_restriction_applies_on_trip_date(
    target_name, summary
):
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="passed",
        summary="常规页面显示开放",
    )
    events = [
        feasibility.PoiOperationalEvent(
            entity=target_name,
            event_type="restriction",
            event_date="2026-09-01",
            source_type="government",
            summary=summary,
            applies_on_applicable_date=True,
        )
    ]

    updated = feasibility._apply_operational_timeline(
        {
            "target": target_name,
            "date_precision": "exact",
            "applicable_date": "2026-09-26",
        },
        finding,
        events,
    )

    assert updated.status == "warning"
    assert summary in updated.summary
    assert "限制条件" in updated.suggested_action


def test_operational_timeline_keeps_conservative_warning_for_legacy_restriction():
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="passed",
        summary="常规页面显示开放",
    )
    events = [
        feasibility.PoiOperationalEvent(
            entity="敦煌博物馆",
            event_type="restriction",
            event_date="2026-08-01",
            source_type="official",
            summary="部分展厅存在访问限制",
        )
    ]

    updated = feasibility._apply_operational_timeline(
        {
            "target": "敦煌博物馆",
            "date_precision": "exact",
            "applicable_date": "2026-09-26",
        },
        finding,
        events,
    )

    assert events[0].applies_on_applicable_date is None
    assert updated.status == "warning"
    assert updated.requires_structure_change is False
    assert "出发前复核" in updated.suggested_action


def test_operational_timeline_current_open_status_overrides_old_closure():
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="passed",
        summary="当前官方票务页面可正常预约",
        requires_structure_change=False,
    )
    event = feasibility.PoiOperationalEvent(
        entity="武侯祠",
        event_type="closure",
        event_date="2022-09-01",
        source_type="official",
        summary="历史临时闭馆公告",
        applies_on_applicable_date=None,
    )

    updated = feasibility._apply_operational_timeline(
        {"target": "武侯祠", "date_precision": "unknown", "applicable_date": ""},
        finding,
        [event],
    )

    assert updated.status == "passed"
    assert updated.requires_structure_change is False
    assert "当前权威开放/票务证据" in updated.summary
    assert "出发前复核" in updated.suggested_action


def test_operational_timeline_uncertain_old_closure_does_not_block_or_change_route():
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status="blocked",
        summary="只找到历史闭馆公告",
        requires_structure_change=True,
    )
    event = feasibility.PoiOperationalEvent(
        entity="武侯祠",
        event_type="closure",
        event_date="2022-09-01",
        source_type="official",
        summary="历史临时闭馆公告",
        applies_on_applicable_date=None,
    )

    updated = feasibility._apply_operational_timeline(
        {"target": "武侯祠", "date_precision": "unknown", "applicable_date": ""},
        finding,
        [event],
    )

    assert updated.status == "unknown"
    assert updated.requires_structure_change is False
    assert "出发前复核" in updated.suggested_action


@pytest.mark.parametrize(
    ("event_type", "initial_status", "expected_status"),
    [
        ("closure", "passed", "passed"),
        ("reopening", "blocked", "blocked"),
        ("restriction", "passed", "passed"),
    ],
)
def test_operational_timeline_ignores_events_after_exact_trip_date(
    event_type, initial_status, expected_status
):
    finding = feasibility.PoiFeasibilityFinding(
        category="poi_operational_status",
        status=initial_status,
        summary="旅行日当时的状态结论",
        requires_structure_change=initial_status == "blocked",
    )
    event = feasibility.PoiOperationalEvent(
        entity="测试景区",
        event_type=event_type,
        event_date="2026-10-01",
        source_type="official",
        summary="旅行日之后才发生的事件",
        applies_on_applicable_date=True if event_type == "restriction" else None,
    )

    updated = feasibility._apply_operational_timeline(
        {
            "target": "测试景区",
            "date_precision": "exact",
            "applicable_date": "2026-09-26",
        },
        finding,
        [event],
    )

    assert updated.status == expected_status
    assert updated.summary == finding.summary
    assert updated.requires_structure_change == finding.requires_structure_change


def test_poi_judge_prompt_defines_closure_and_restriction_applicability_contract():
    prompt = feasibility._poi_judge_prompt(
        {
            "target": "茶卡盐湖",
            "city": "海西",
            "day": 2,
            "date_precision": "exact",
            "applicable_date": "2026-09-26",
            "categories": ["poi_operational_status"],
            "notes": "按计划游览",
        },
        "官方信息同时包含历史冬季安排和当前开放规则",
    )

    assert "applies_on_applicable_date" in prompt
    assert "closure和restriction事件" in prompt
    assert "当前权威开放/票务状态" in prompt
    assert "不得仅根据公告年龄猜测事件已经失效" in prompt


def test_length_limited_poi_verdict_retries_once_on_structured_channel(monkeypatch):
    calls = []

    class FakeStructured:
        def __init__(self, result):
            self.result = result

        def invoke(self, prompt):
            calls.append((self.result, prompt))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class FakeModel:
        def __init__(self, group, result):
            self.key_group = group
            self.model_name = "glm-5.3-flash"
            self.result = result

        def with_structured_output(self, schema, method=None):
            assert schema is feasibility.PoiFeasibilityVerdict
            assert method == "json_mode"
            return FakeStructured(self.result)

    expected = feasibility.PoiFeasibilityVerdict(
        findings=[
            feasibility.PoiFeasibilityFinding(
                category="poi_operational_status",
                status="passed",
                summary="当前正常开放",
            )
        ]
    )
    primary = FakeModel("deepseek", RuntimeError("Could not parse response content as the length limit was reached"))
    retry = FakeModel("deepseek", expected)
    factory_args = []
    models = iter([primary, retry])

    def fake_factory(**kwargs):
        factory_args.append(kwargs)
        return next(models)

    monkeypatch.setattr(feasibility, "get_chat_model", fake_factory)

    diagnostics = {}
    verdict = feasibility._judge_poi_target(
        {
            "target": "武侯祠",
            "city": "成都",
            "day": 2,
            "date_precision": "unknown",
            "applicable_date": "2026-08-29",
            "categories": ["poi_operational_status"],
            "notes": "",
            "traveler_constraints": "",
        },
        "官方结果",
        diagnostics=diagnostics,
    )

    assert verdict == expected
    assert factory_args == [
        {"temperature": 0, "max_tokens": 16000, "disable_reasoning": True},
        {"temperature": 0},
    ]
    assert len(calls) == 2
    assert diagnostics == {
        "judge_provider": "gptload",
        "judge_model": "glm-5.3-flash",
        "requested_categories": ["poi_operational_status"],
        "returned_categories": ["poi_operational_status"],
        "stream_completed": True,
        "technical_error_class": "",
    }


def test_poi_verdict_schema_rejects_empty_findings():
    with pytest.raises(ValueError):
        feasibility.PoiFeasibilityVerdict(findings=[])


@pytest.mark.parametrize(
    ("raw_source_type", "expected"),
    [
        ("web", "verified_web"),
        ("news", "verified_web"),
        ("新闻媒体", "verified_web"),
        ("local_bureau", "government"),
        ("government_website", "government"),
        ("official_website", "official"),
        ("official_announcement", "official"),
        ("announcement", "official"),
        ("官方公告", "official"),
    ],
)
def test_poi_operational_event_normalizes_model_source_labels(raw_source_type, expected):
    event = feasibility.PoiOperationalEvent(
        entity="测试景区",
        event_type="reopening",
        source_type=raw_source_type,
    )

    assert event.source_type == expected


def test_missing_category_records_attempt_and_judge_diagnostics(
    isolated_project, monkeypatch
):
    target = {
        "day": 2,
        "target": "武侯祠",
        "target_type": "poi",
        "city": "成都",
        "categories": ["poi_operational_status", "reservation_or_quota"],
        "applicable_date": "2026-09-05",
        "query": "成都 武侯祠 开放 预约 官方",
        "notes": "",
        "validation_attempt": 2,
        "entity_kind": "visit_site",
        "route_role": "visit_stop",
        "check_profile": "visit_site",
    }

    def fake_judge(target, web_result, *, diagnostics=None):
        diagnostics.update(
            {
                "judge_provider": "gptload",
                "judge_model": "glm-5.3-flash",
                "requested_categories": list(target["categories"]),
                "returned_categories": ["poi_operational_status"],
                "stream_completed": True,
                "technical_error_class": "",
            }
        )
        return feasibility.PoiFeasibilityVerdict(
            findings=[
                feasibility.PoiFeasibilityFinding(
                    category="poi_operational_status",
                    status="passed",
                    summary="当前正常开放",
                )
            ]
        )

    monkeypatch.setattr(feasibility, "_judge_poi_target", fake_judge)
    monkeypatch.setattr(
        feasibility,
        "_evidence_for_query",
        lambda *args: [
            RouteFeasibilityEvidence(
                title="武侯祠官方公告",
                url="https://example.test/official",
                excerpt="开放和预约信息",
                source_type="official",
            )
        ],
    )
    checks = feasibility._run_poi_target(
        isolated_project,
        target,
        type("Search", (), {"invoke": lambda self, args: "官方开放和预约信息"})(),
    )

    by_category = {check.category: check for check in checks}
    passed = by_category["poi_operational_status"]
    missing = by_category["reservation_or_quota"]
    assert passed.validation_attempt == 2
    assert passed.judge_provider == "gptload"
    assert passed.judge_model == "glm-5.3-flash"
    assert passed.returned_categories == ["poi_operational_status"]
    assert passed.stream_completed is True
    assert missing.status == "unknown"
    assert missing.technical_error_class == "missing_category"
    assert missing.requested_categories == [
        "poi_operational_status",
        "reservation_or_quota",
    ]
    assert missing.returned_categories == ["poi_operational_status"]
    assert missing.check_profile == "visit_site"
    assert missing.evidence[0].title == "武侯祠官方公告"


def test_judge_failure_preserves_successful_web_evidence(
    isolated_project, monkeypatch
):
    target = {
        "day": 1,
        "target": "茶卡盐湖",
        "target_type": "poi",
        "city": "海西",
        "categories": ["poi_operational_status"],
        "applicable_date": "2026-09-05",
        "query": "海西 茶卡盐湖 最新状态 官方公告",
        "notes": "",
        "entity_kind": "visit_site",
        "route_role": "visit_stop",
        "check_profile": "visit_site",
    }
    monkeypatch.setattr(
        feasibility,
        "_evidence_for_query",
        lambda *args: [
            RouteFeasibilityEvidence(
                title="茶卡盐湖官网",
                url="https://example.test/chaka",
                excerpt="景区开放信息",
                source_type="official",
            )
        ],
    )

    def fail_judge(target, web_result, *, diagnostics=None):
        diagnostics.update(
            {
                "judge_provider": "jiji",
                "judge_model": "glm-5.3-flash",
                "requested_categories": list(target["categories"]),
                "returned_categories": [],
                "stream_completed": False,
                "technical_error_class": "stream_interrupted",
            }
        )
        raise RuntimeError("incomplete chunked read")

    monkeypatch.setattr(feasibility, "_judge_poi_target", fail_judge)
    checks = feasibility._run_poi_target(
        isolated_project,
        target,
        type("Search", (), {"invoke": lambda self, args: "已取得官方搜索结果"})(),
    )

    assert len(checks) == 1
    assert checks[0].technical_error_class == "stream_interrupted"
    assert checks[0].stream_completed is False
    assert checks[0].evidence[0].title == "茶卡盐湖官网"


def test_scoped_retry_increments_persisted_validation_attempt(
    isolated_project, monkeypatch
):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    pois=[RoutePoi(name="武侯祠")],
                )
            ]
        ),
    )
    monkeypatch.setattr(
        feasibility,
        "make_web_search_tools",
        lambda *args, **kwargs: [
            type("Search", (), {"invoke": lambda self, args: "官方开放信息"})()
        ],
    )
    state = {"passed": False}

    def fake_judge(target, web_result, *, diagnostics=None):
        returned = ["poi_operational_status"] if state["passed"] else []
        diagnostics.update(
            {
                "judge_provider": "gptload",
                "judge_model": "glm-5.3-flash",
                "requested_categories": list(target["categories"]),
                "returned_categories": returned,
                "stream_completed": True,
                "technical_error_class": "",
            }
        )
        if not state["passed"]:
            # Simulate a legacy provider result that passed the old permissive
            # schema but omitted the only requested category.
            return feasibility.PoiFeasibilityVerdict.model_construct(
                findings=[], operational_events=[]
            )
        return feasibility.PoiFeasibilityVerdict(
            findings=[
                feasibility.PoiFeasibilityFinding(
                    category="poi_operational_status",
                    status="passed",
                    summary="当前正常开放",
                )
            ]
        )

    monkeypatch.setattr(feasibility, "_judge_poi_target", fake_judge)
    first = feasibility.verify_route_feasibility(isolated_project, "成都")
    assert first.checks[0].validation_attempt == 1
    assert first.checks[0].technical_error_class == "missing_category"

    state["passed"] = True
    second = feasibility.verify_route_feasibility(
        isolated_project,
        "成都",
        check_ids=[first.checks[0].check_id],
    )

    assert second.checks[0].status == "passed"
    assert second.checks[0].validation_attempt == 2


def test_scoped_technical_retry_reuses_persisted_web_evidence(
    isolated_project, monkeypatch
):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    pois=[RoutePoi(name="武侯祠")],
                )
            ]
        ),
    )
    target = feasibility.build_route_feasibility_targets(
        store.read_workspace(isolated_project, "route_plan.json"),
        {"destination": "成都", "days": 1},
    )[0][0]
    check_id = feasibility._check_id(target, "poi_operational_status")
    previous = RouteFeasibilityReport(
        route_plan_structure_hash=feasibility._structure_hash(
            store.read_workspace(isolated_project, "route_plan.json")
        ),
        checks=[
            RouteFeasibilityCheck(
                check_id=check_id,
                day=1,
                target="武侯祠",
                target_type="poi",
                category="poi_operational_status",
                status="unknown",
                summary="结构化解析失败",
                technical_error="解析失败",
                technical_error_class="judge_error",
                validation_attempt=1,
            )
        ],
    )
    store.write_route_feasibility(isolated_project, "citation_checker", previous)
    log_path = feasibility._web_log_path(isolated_project)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "schema_version": "web-search-log-v2",
                "query": target["query"],
                "created_at": "2026-09-05T00:00:00+00:00",
                "results": [
                    {
                        "title": "武侯祠公告",
                        "excerpt": "武侯祠正常开放",
                        "url": "https://example.test/wuhou",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        feasibility,
        "_suggest_retry_query",
        lambda *args, **kwargs: pytest.fail("技术复核不应重新生成Query"),
    )
    monkeypatch.setattr(
        feasibility,
        "make_web_search_tools",
        lambda *args, **kwargs: [
            type(
                "Search",
                (),
                {"invoke": lambda self, args: pytest.fail("技术复核不应重新搜索")},
            )()
        ],
    )

    def pass_judge(target, web_result, *, diagnostics=None):
        assert "武侯祠正常开放" in web_result
        diagnostics.update(
            {
                "judge_provider": "gptload",
                "judge_model": "glm-5.2",
                "requested_categories": list(target["categories"]),
                "returned_categories": list(target["categories"]),
                "stream_completed": True,
                "technical_error_class": "",
            }
        )
        return feasibility.PoiFeasibilityVerdict(
            findings=[
                feasibility.PoiFeasibilityFinding(
                    category="poi_operational_status",
                    status="passed",
                    summary="当前正常开放",
                )
            ]
        )

    monkeypatch.setattr(feasibility, "_judge_poi_target", pass_judge)
    report = feasibility.verify_route_feasibility(
        isolated_project,
        "成都",
        requirements={"destination": "成都", "days": 1},
        check_ids=[check_id],
    )

    assert report.checks[0].status == "passed"
    assert report.checks[0].validation_attempt == 2
    evidence_checks = store.read_evidence_checks(isolated_project)["route"]
    assert evidence_checks[0]["query_source"] == "technical_retry_reuse"
    evidence_checks = store.read_evidence_checks(isolated_project)
    current = next(
        item
        for item in evidence_checks["route"]
        if item.get("target_type") == "route_feasibility"
    )
    assert current["validation_attempt"] == 2
    assert current["judge_provider"] == "gptload"


def test_non_length_poi_parse_error_does_not_fallback(monkeypatch):
    class FakeModel:
        key_group = "full"

        def with_structured_output(self, schema, method=None):
            class Broken:
                def invoke(self, prompt):
                    raise ValueError("schema mismatch")

            return Broken()

    fallback_called = {"value": False}

    def fake_factory(**kwargs):
        if fallback_called["value"]:
            raise AssertionError("non-length parse errors must not retry")
        fallback_called["value"] = True
        return FakeModel()

    monkeypatch.setattr(feasibility, "get_chat_model", fake_factory)

    with pytest.raises(ValueError, match="schema mismatch"):
        feasibility._judge_poi_target(
            {
                "target": "武侯祠",
                "city": "成都",
                "day": 2,
                "date_precision": "unknown",
                "applicable_date": "2026-08-29",
                "categories": ["poi_operational_status"],
                "notes": "",
                "traveler_constraints": "",
            },
            "官方结果",
        )

    assert fallback_called["value"] is True


def test_select_target_checks_reuses_one_poi_search_for_requested_categories():
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "敦煌",
                "pois": [{"name": "莫高窟", "needs_detail": True}],
                "notes": "",
                "claims": [
                    {
                        "subject": "莫高窟",
                        "claimed_entities": ["莫高窟"],
                        "claim_text": "莫高窟需要提前预约，17:30停止入园",
                    }
                ],
            }
        ]
    }
    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})
    target = targets[0]
    requested = [
        feasibility._check_id(target, "reservation_or_quota"),
        feasibility._check_id(target, "last_entry"),
    ]

    selected = feasibility._select_target_checks(targets, requested)

    assert len(selected) == 1
    assert selected[0]["categories"] == ["reservation_or_quota", "last_entry"]


def test_scoped_retry_prefers_valid_model_query_and_falls_back_for_duplicate(
    isolated_project, monkeypatch
):
    route_plan = {
        "days": [
            {
                "day": 1,
                "city": "成都",
                "pois": [{"name": "玉林·芳华街"}],
                "notes": "",
            }
        ]
    }
    targets, _ = feasibility.build_route_feasibility_targets(route_plan, {})
    target = targets[0]
    log_path = feasibility._web_log_path(isolated_project)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"schema_version":"web-search-log-v2","query":"成都 玉林 芳华街 最新状态 官方公告 2026-08-29","results":[]}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        feasibility,
        "_suggest_retry_query",
        lambda *args, **kwargs: feasibility.PoiRetryQueryPlan(
            query="成都 芳华街 当前是否正常通行 官方消息 2026-08-29",
            reason="上轮结果与目标不相关",
        ),
    )

    first_retry = feasibility._rewrite_scoped_retry_queries(isolated_project, [target])[0]
    assert first_retry["query_variant"] == 1
    assert first_retry["query_source"] == "model_retry"
    assert first_retry["query"] == "成都 芳华街 当前是否正常通行 官方消息 2026-08-29"

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"schema_version":"web-search-log-v2","query":"成都 芳华街 当前是否正常通行 官方消息 2026-08-29","results":[]}\n'
        )
    second_retry = feasibility._rewrite_scoped_retry_queries(isolated_project, [target])[0]
    assert second_retry["query_variant"] == 2
    assert second_retry["query_source"] == "program_fallback"
    assert "政府 文旅 本地生活 最新消息" in second_retry["query"]


@pytest.mark.parametrize(
    "query",
    [
        "芳华街 当前状态",
        "成都 熊猫基地 当前状态",
        "成都 玉林小酒馆 当前状态",
        "成都 玉林 芳华街 最新状态 官方公告 2026-08-29",
    ],
)
def test_retry_query_validator_rejects_missing_city_wrong_target_and_duplicate(query):
    target = {"city": "成都", "target": "玉林·芳华街"}
    previous = ["成都 玉林 芳华街 最新状态 官方公告 2026-08-29"]

    assert feasibility._valid_retry_query(query, target, previous) is False


@pytest.mark.parametrize(
    "query",
    [
        "成都 武侯祠 2026年8月29日 余票 官方渠道",
        "成都 武侯祠 2026年8月29日 参观日预约规则",
        "成都 武侯祠 当天预约名额 官方公告",
    ],
)
def test_unknown_trip_date_rejects_date_specific_retry_query(query):
    target = {
        "city": "成都",
        "target": "武侯祠",
        "date_precision": "unknown",
    }

    assert feasibility._valid_retry_query(query, target, []) is False


def test_unknown_trip_date_retry_prompt_distinguishes_query_baseline(monkeypatch):
    target = {
        "city": "成都",
        "target": "武侯祠",
        "categories": ["reservation_or_quota"],
        "applicable_date": "2026-08-29",
        "query_baseline_date": "2026-08-29",
        "date_precision": "unknown",
        "claim_texts": ["武侯祠实行实名预约"],
    }

    prompt = feasibility._retry_query_prompt(
        target,
        [],
        [{"query": "成都 武侯祠 最新预约规则", "results": []}],
    )

    assert "查询最新规则的基准日：2026-08-29" in prompt
    assert "实际旅行日期：未提供" in prompt
    assert "不得把基准日写成参观日、路线日期或出行当日" in prompt


def test_retry_query_suggestion_uses_small_structured_model(monkeypatch):
    captured = {}

    class FakeStructured:
        def invoke(self, prompt):
            captured["prompt"] = prompt
            return feasibility.PoiRetryQueryPlan(
                query="成都 芳华街 当前通行状态 官方消息",
                reason="原结果错配",
            )

    class FakeModel:
        def with_structured_output(self, schema, method=None):
            assert schema is feasibility.PoiRetryQueryPlan
            assert method == "json_mode"
            return FakeStructured()

    def fake_model(**kwargs):
        captured["kwargs"] = kwargs
        return FakeModel()

    monkeypatch.setattr(feasibility, "get_chat_model", fake_model)
    target = {
        "city": "成都",
        "target": "玉林·芳华街",
        "categories": ["poi_operational_status"],
        "applicable_date": "2026-08-29",
        "claim_texts": ["芳华街可以进行citywalk"],
    }
    previous = [
        RouteFeasibilityCheck(
            check_id="x",
            day=3,
            target="玉林·芳华街",
            target_type="poi",
            category="poi_operational_status",
            status="unknown",
            summary="搜索结果指向了其他地点",
        )
    ]
    history = [{"query": "成都 玉林 芳华街 最新状态 官方公告", "results": []}]

    result = feasibility._suggest_retry_query(target, previous, history)

    assert result is not None
    assert result.query == "成都 芳华街 当前通行状态 官方消息"
    assert captured["kwargs"] == {
        "temperature": 0,
        "max_tokens": 800,
        "disable_reasoning": True,
    }
    assert "不回答事实结论" in captured["prompt"]
    assert "已经使用的Query" in captured["prompt"]


def test_scoped_check_merge_preserves_other_days_and_missing_requested_check():
    context = RouteFeasibilityDateContext(precision="exact", start_date="2026-10-02")
    day1 = RouteFeasibilityCheck(
        check_id="day1", day=1, target="A", target_type="poi", category="status", status="passed"
    )
    unresolved = RouteFeasibilityCheck(
        check_id="day2", day=2, target="B", target_type="road", category="road", status="warning"
    )
    previous = RouteFeasibilityReport(
        route_plan_structure_hash="hash",
        requirements_date_context=context,
        overall_status="warning",
        checks=[day1, unresolved],
    )

    merged, force_finalized = feasibility._merge_scoped_checks(
        previous,
        new_checks=[],
        structure_hash="hash",
        context=context,
        days=None,
        check_ids=["day2"],
    )

    assert [check.check_id for check in merged] == ["day1", "day2"]
    assert merged[1].status == "warning"
    assert force_finalized is False


def test_verify_scoped_check_updates_only_requested_id_and_keeps_full_report(
    isolated_project, monkeypatch
):
    route = RoutePlan(
        days=[
            RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")]),
            RouteDay(day=2, city="成都", pois=[RoutePoi(name="熊猫基地")]),
        ]
    )
    store.write_route_plan(isolated_project, "route_agent", route)
    route_data = store.read_workspace(isolated_project, "route_plan.json")
    targets, context = feasibility.build_route_feasibility_targets(route_data, {})
    by_day = {target["day"]: target for target in targets}
    day1_id = feasibility._check_id(by_day[1], "poi_operational_status")
    day2_id = feasibility._check_id(by_day[2], "poi_operational_status")
    store.write_route_feasibility(
        isolated_project,
        "citation_checker",
        RouteFeasibilityReport(
            route_plan_structure_hash=feasibility._structure_hash(route_data),
            requirements_date_context=context,
            overall_status="warning",
            checks=[
                RouteFeasibilityCheck(
                    check_id=day1_id,
                    day=1,
                    target="武侯祠",
                    target_type="poi",
                    category="poi_operational_status",
                    status="passed",
                ),
                RouteFeasibilityCheck(
                    check_id=day2_id,
                    day=2,
                    target="熊猫基地",
                    target_type="poi",
                    category="poi_operational_status",
                    status="warning",
                ),
            ],
        ),
    )
    checked_targets = []

    def fake_run(project_id, target, web_search_tool):
        checked_targets.append(target)
        return [
            RouteFeasibilityCheck(
                check_id=day2_id,
                day=2,
                target="熊猫基地",
                target_type="poi",
                category="poi_operational_status",
                status="passed",
            )
        ]

    monkeypatch.setattr(feasibility, "make_web_search_tools", lambda *args, **kwargs: [object()])
    monkeypatch.setattr(feasibility, "_run_poi_target", fake_run)

    report = feasibility.verify_route_feasibility(
        isolated_project,
        "成都",
        days=[2],
        check_ids=[day2_id],
    )

    assert [(target["day"], target["categories"]) for target in checked_targets] == [
        (2, ["poi_operational_status"])
    ]
    assert [(check.day, check.status) for check in report.checks] == [(1, "passed"), (2, "passed")]
    assert report.overall_status == "passed"


def test_scoped_feasibility_repair_archives_previous_target_checks(
    isolated_project, monkeypatch
):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="芳华街")])]),
    )
    monkeypatch.setattr(feasibility, "make_web_search_tools", lambda *args, **kwargs: [object()])
    current_status = {"value": "unknown"}

    def fake_run(project_id, target, web_search_tool):
        return [
            RouteFeasibilityCheck(
                check_id=feasibility._check_id(target, "poi_operational_status"),
                day=1,
                target="芳华街",
                target_type="poi",
                category="poi_operational_status",
                status=current_status["value"],
                summary="首次证据错配" if current_status["value"] == "unknown" else "当前状态已确认",
            )
        ]

    monkeypatch.setattr(feasibility, "_run_poi_target", fake_run)
    first = feasibility.verify_route_feasibility(isolated_project, "成都")
    check_id = first.checks[0].check_id
    current_status["value"] = "passed"

    repaired = feasibility.verify_route_feasibility(
        isolated_project,
        "成都",
        check_ids=[check_id],
    )

    evidence_checks = store.read_evidence_checks(isolated_project)
    assert repaired.overall_status == "passed"
    feasibility_checks = [
        item
        for item in evidence_checks["route"]
        if item["target_key"].startswith("route_feasibility:")
    ]
    assert len(feasibility_checks) == 1
    assert feasibility_checks[0]["result"] == "pass"
    assert len(evidence_checks["history"]) == 1
    archived = evidence_checks["history"][0]
    assert archived["section"] == "route"
    assert archived["checks"][0]["result"] == "inconclusive"
    assert archived["checks"][0]["reason"] == "首次证据错配"


def test_store_round_trip_route_feasibility(isolated_project):
    report = RouteFeasibilityReport(overall_status="passed", created_at="now")
    store.write_route_feasibility(isolated_project, "citation_checker", report)
    restored = store.read_route_feasibility(isolated_project)
    assert restored is not None
    assert restored.overall_status == "passed"
    with pytest.raises(store.PermissionError):
        store.write_route_feasibility(isolated_project, "route_agent", report)


def test_verify_persists_unknown_when_web_search_fails(isolated_project, monkeypatch):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])]
        ),
    )

    class FailingTool:
        def invoke(self, payload):
            raise RuntimeError("network down")

    monkeypatch.setattr(
        feasibility,
        "make_web_search_tools",
        lambda *args, **kwargs: [FailingTool()],
    )
    report = feasibility.verify_route_feasibility(isolated_project, "成都")
    assert report.overall_status == "unknown"
    assert report.checks[0].status == "unknown"
    assert "network down" in (report.checks[0].technical_error or "")
    assert store.read_route_feasibility(isolated_project) is not None


def test_web_no_result_is_evidence_unknown_not_technical_error(isolated_project, monkeypatch):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="宽窄巷子")])]),
    )

    class EmptyResultTool:
        def invoke(self, payload):
            return "联网搜索没有找到相关结果"

    monkeypatch.setattr(
        feasibility,
        "make_web_search_tools",
        lambda *args, **kwargs: [EmptyResultTool()],
    )

    report = feasibility.verify_route_feasibility(isolated_project, "成都")

    assert report.overall_status == "unknown"
    assert report.checks[0].technical_error is None
    assert report.checks[0].suggested_action == "出发前通过官方渠道重新核实"


def test_structure_hash_ignores_notes_but_changes_with_pois():
    first = {"days": [{"day": 1, "city": "成都", "pois": [{"name": "武侯祠"}], "notes": "A"}]}
    second = {"days": [{"day": 1, "city": "成都", "pois": [{"name": "武侯祠"}], "notes": "B"}]}
    changed = {"days": [{"day": 1, "city": "成都", "pois": [{"name": "杜甫草堂"}], "notes": "A"}]}
    assert feasibility._structure_hash(first) == feasibility._structure_hash(second)
    assert feasibility._structure_hash(first) != feasibility._structure_hash(changed)
