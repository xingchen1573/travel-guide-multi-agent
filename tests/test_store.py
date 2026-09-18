"""workspace/store.py 的单元测试：重点测权限校验和changelog留痕，不涉及LLM调用。"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.workspace import store
from src.workspace.schema import (
    LifestyleOutput,
    PoiDetailFile,
    RouteDay,
    RouteOption,
    RoutePlan,
    RouteSegment,
)


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "test-project"
    store.init_project(project_id)
    return project_id


def test_write_evidence_checks_scoped_replace_preserves_other_targets(isolated_project):
    store.write_evidence_checks(
        isolated_project,
        "route",
        [
            {"target_key": "route:day:1", "scope": "entity_identity", "result": "pass"},
            {"target_key": "route:day:2", "scope": "entity_identity", "result": "pass"},
        ],
        replace_prefixes=["route:"],
    )

    store.write_evidence_checks(
        isolated_project,
        "route",
        [{"target_key": "route:day:2", "scope": "entity_identity", "result": "fail"}],
        replace_prefixes=["route:day:2"],
    )

    saved = store.read_evidence_checks(isolated_project)["route"]
    by_target = {item["target_key"]: item for item in saved}
    assert by_target["route:day:1"]["result"] == "pass"
    assert by_target["route:day:2"]["result"] == "fail"


def test_project_plan_run_lock_rejects_second_process(isolated_project, tmp_path):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys
                from pathlib import Path
                from src.workspace import store

                store.PROJECTS_DIR = Path(sys.argv[1])
                with store.project_plan_run_lock(sys.argv[2]):
                    print("locked", flush=True)
                    sys.stdin.readline()
                """
            ),
            str(tmp_path),
            isolated_project,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(store.ProjectRunAlreadyActiveError):
            with store.project_plan_run_lock(isolated_project):
                pass
    finally:
        if child.poll() is None:
            child.stdin.write("release\n")
            child.stdin.flush()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)

    with store.project_plan_run_lock(isolated_project):
        pass


def test_write_evidence_checks_segment_prefix_does_not_match_order_10(isolated_project):
    """途经点order=1的局部复核不能误删同一路线order=10的证据。"""
    store.write_evidence_checks(
        isolated_project,
        "poi_detail",
        [
            {
                "target_key": "poi_detail:武侯祠:route:经典线:order:1:claim:a",
                "scope": "substantive_claim",
                "result": "fail",
            },
            {
                "target_key": "poi_detail:武侯祠:route:经典线:order:10:claim:b",
                "scope": "substantive_claim",
                "result": "pass",
            },
        ],
        replace_prefixes=["poi_detail:武侯祠:"],
    )

    store.write_evidence_checks(
        isolated_project,
        "poi_detail",
        [
            {
                "target_key": "poi_detail:武侯祠:route:经典线:order:1:claim:c",
                "scope": "substantive_claim",
                "result": "pass",
            }
        ],
        replace_prefixes=["poi_detail:武侯祠:route:经典线:order:1"],
    )

    target_keys = {
        item["target_key"]
        for item in store.read_evidence_checks(isolated_project)["poi_detail"]
    }
    assert "poi_detail:武侯祠:route:经典线:order:1:claim:a" not in target_keys
    assert "poi_detail:武侯祠:route:经典线:order:1:claim:c" in target_keys
    assert "poi_detail:武侯祠:route:经典线:order:10:claim:b" in target_keys


def test_write_evidence_checks_archives_failed_attempt_before_repair(isolated_project):
    store.write_evidence_checks(
        isolated_project,
        "poi_detail",
        [
            {
                "target_key": "poi_detail:东郊记忆:route:拍照线:order:4:claim:old",
                "target_text": "文创小店是路线的固定收尾点。",
                "scope": "substantive_claim",
                "result": "fail",
                "method": "substantive_support",
                "reason": "原文只证明存在文创小店，没有证明固定收尾顺序。",
            },
            {
                "target_key": "poi_detail:东郊记忆:route:拍照线:order:4:claim:old",
                "target_text": "文创小店是路线的固定收尾点。",
                "scope": "source_availability",
                "result": "pass",
                "method": "chunk_lookup",
                "reason": "引用可读取。",
            },
        ],
        replace_prefixes=["poi_detail:东郊记忆:"],
    )

    store.write_evidence_checks(
        isolated_project,
        "poi_detail",
        [
            {
                "target_key": "poi_detail:东郊记忆:route:拍照线:order:4:claim:new",
                "target_text": "将文创小店作为收尾是本攻略的编排建议。",
                "scope": "substantive_claim",
                "result": "not_checked",
                "method": "declared_estimate",
                "reason": "明确标记为编排建议。",
            }
        ],
        replace_prefixes=["poi_detail:东郊记忆:"],
    )

    saved = store.read_evidence_checks(isolated_project)
    assert saved["version"] == 2
    assert saved["poi_detail"][0]["target_text"] == "将文创小店作为收尾是本攻略的编排建议。"
    assert len(saved["history"]) == 1
    archived = saved["history"][0]
    assert archived["section"] == "poi_detail"
    assert {item["scope"] for item in archived["checks"]} == {
        "source_availability",
        "substantive_claim",
    }
    assert any(item["result"] == "fail" for item in archived["checks"])


def test_route_facts_change_archives_all_previous_day_checks(isolated_project):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="峨眉山", notes="旧提醒")]),
    )
    target_key = "route:day:1:claim:old-claim"
    store.write_evidence_checks(
        isolated_project,
        "route",
        [
            {"target_key": target_key, "scope": "source_availability", "result": "pass"},
            {"target_key": target_key, "scope": "entity_identity", "result": "fail"},
            {"target_key": target_key, "scope": "substantive_claim", "result": "pass"},
        ],
        replace_prefixes=["route:day:1"],
    )

    store.write_route_facts_patch(
        isolated_project,
        "route_agent",
        1,
        notes="新提醒",
    )

    saved = store.read_evidence_checks(isolated_project)
    assert saved["route"] == []
    archived = saved["history"][-1]
    assert archived["reason"] == "route_facts_changed"
    assert {item["scope"] for item in archived["checks"]} == {
        "source_availability",
        "entity_identity",
        "substantive_claim",
    }


def test_unchanged_route_facts_do_not_invalidate_current_checks(isolated_project):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都", notes="保持不变")]),
    )
    check = {
        "target_key": "route:day:1",
        "scope": "entity_identity",
        "result": "pass",
    }
    store.write_evidence_checks(
        isolated_project,
        "route",
        [check],
        replace_prefixes=["route:day:1"],
    )

    store.write_route_facts_patch(
        isolated_project,
        "route_agent",
        1,
        notes="保持不变",
    )

    saved = store.read_evidence_checks(isolated_project)
    assert saved["route"] == [check]
    assert saved["history"] == []


def test_write_evidence_checks_does_not_archive_pass_only_revalidation(isolated_project):
    check = {
        "target_key": "route:day:1",
        "scope": "entity_identity",
        "result": "pass",
    }
    store.write_evidence_checks(
        isolated_project, "route", [check], replace_prefixes=["route:day:1"]
    )
    store.write_evidence_checks(
        isolated_project, "route", [check], replace_prefixes=["route:day:1"]
    )

    assert store.read_evidence_checks(isolated_project)["history"] == []


def test_write_evidence_checks_uses_atomic_replace(isolated_project, monkeypatch):
    calls = []
    real_replace = os.replace

    def tracked_replace(source, target):
        calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(store.os, "replace", tracked_replace)

    store.write_evidence_checks(
        isolated_project,
        "route",
        [{"target_key": "route:day:1", "scope": "entity_identity", "result": "pass"}],
        replace_prefixes=["route:day:1"],
    )

    workspace = store.workspace_dir(isolated_project)
    assert calls and calls[-1][1] == workspace / "evidence_checks.json"
    assert not list(workspace.glob(".evidence_checks.json.*.tmp"))
    assert store.read_evidence_checks(isolated_project)["route"][0]["result"] == "pass"


def test_write_poi_detail_clamps_multiple_recommended_routes(isolated_project):
    """docs/ARCHITECTURE.md第3.4节第6点"多骨架备选"：一个POI可能有多条routes，但只应该
    有一条recommended=True——如果调用方（LLM/edit入口）误标了多条为true，写入时应该
    确定性地兜底只保留第一条，不依赖prompt自觉遵守。"""
    detail = PoiDetailFile(
        poi_name="武侯祠",
        day=1,
        routes=[
            RouteOption(route_name="经典线", recommended=True),
            RouteOption(route_name="后山线", recommended=True),
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    saved = store.read_poi_detail(isolated_project, 1, "武侯祠")
    recommended_names = [r.route_name for r in saved.routes if r.recommended]
    assert recommended_names == ["经典线"]


def test_write_poi_detail_allows_zero_recommended_routes(isolated_project):
    """零条recommended是合法状态（模型判断"两条路线各有侧重，不强行只推荐一条"），
    不应该被clamp逻辑误伤。"""
    detail = PoiDetailFile(
        poi_name="武侯祠",
        day=1,
        routes=[
            RouteOption(route_name="经典线", recommended=False),
            RouteOption(route_name="后山线", recommended=False),
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    saved = store.read_poi_detail(isolated_project, 1, "武侯祠")
    assert all(not r.recommended for r in saved.routes)


def test_write_poi_detail_patch_requires_existing_file(isolated_project):
    """patch只能用于修改已有文件——首次生成必须走整份覆盖的write_poi_detail。"""
    with pytest.raises(ValueError, match="不存在"):
        store.write_poi_detail_patch(
            isolated_project, "poi_detail_agent", 1, "武侯祠",
            [{"route_name": "经典线", "recommended": True}],
        )


def test_write_poi_detail_patch_preserves_untouched_routes(isolated_project):
    """核心行为：patch某条路线时，其余路线必须原样保留，不需要调用方重新提交一遍。"""
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[
            RouteOption(route_name="经典线", recommended=True, suitable_for="大众"),
            RouteOption(route_name="后山线", recommended=False, suitable_for="资深玩家"),
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    updated = store.write_poi_detail_patch(
        isolated_project, "poi_detail_agent", 1, "武侯祠",
        [{"route_name": "经典线", "recommended": True, "suitable_for": "改过的描述"}],
    )

    by_name = {r.route_name: r for r in updated.routes}
    assert by_name["经典线"].suitable_for == "改过的描述"
    assert by_name["后山线"].suitable_for == "资深玩家"  # 未被patch的路线原样保留

    saved = store.read_poi_detail(isolated_project, 1, "武侯祠")
    assert {r.route_name for r in saved.routes} == {"经典线", "后山线"}


def test_write_poi_detail_patch_can_add_new_route_name(isolated_project):
    """提交一个此前不存在的route_name，会被当成新增这一条路线（by_name字典的自然行为，
    跟write_route_plan_patch可以增天同一个道理）——prompt引导优先用整份覆盖，但store层
    本身不拦截。"""
    detail = PoiDetailFile(poi_name="武侯祠", day=1, routes=[RouteOption(route_name="经典线")])
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    updated = store.write_poi_detail_patch(
        isolated_project, "poi_detail_agent", 1, "武侯祠",
        [{"route_name": "后山线", "recommended": False}],
    )

    assert {r.route_name for r in updated.routes} == {"经典线", "后山线"}


def test_write_poi_detail_patch_clamps_after_merge_not_just_submitted_subset(isolated_project):
    """分歧点3的核心约束：clamp必须在merge之后跑，针对合并后的完整routes数组，不能只看
    这次提交的子集——否则模型切换推荐时只提交新recommended=true的一条、忘了把旧的改成
    false，merge后会出现两条recommended=true，必须靠这里的clamp兜底纠正。"""
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[
            RouteOption(route_name="经典线", recommended=True),
            RouteOption(route_name="后山线", recommended=False),
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    # 只提交新推荐的"后山线"，没有把"经典线"的recommended改成false
    updated = store.write_poi_detail_patch(
        isolated_project, "poi_detail_agent", 1, "武侯祠",
        [{"route_name": "后山线", "recommended": True}],
    )

    recommended_names = [r.route_name for r in updated.routes if r.recommended]
    assert recommended_names == ["经典线"]  # clamp保留第一条（既有顺序里最先出现的）


def test_write_poi_detail_patch_preserves_needs_detail_reason_by_default(isolated_project):
    """needs_detail_reason是POI文件级标量字段，不属于任何一条route——patch某条路线内容
    不应该意外清空它（不传或传None时保留原值）。"""
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1, needs_detail_reason="山岳景区，值得深挖",
        routes=[RouteOption(route_name="经典线")],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    updated = store.write_poi_detail_patch(
        isolated_project, "poi_detail_agent", 1, "武侯祠",
        [{"route_name": "经典线", "suitable_for": "改过"}],
    )

    assert updated.needs_detail_reason == "山岳景区，值得深挖"


def test_write_poi_detail_patch_can_explicitly_update_needs_detail_reason(isolated_project):
    detail = PoiDetailFile(poi_name="武侯祠", day=1, needs_detail_reason="旧理由", routes=[RouteOption(route_name="经典线")])
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    updated = store.write_poi_detail_patch(
        isolated_project, "poi_detail_agent", 1, "武侯祠",
        [{"route_name": "经典线"}], needs_detail_reason="新理由",
    )

    assert updated.needs_detail_reason == "新理由"


def test_write_poi_detail_patch_preserves_citation_status_and_history(isolated_project):
    """citation_status/auto_collected/retry_history由核验流程（mark_poi_force_finalize）
    独立维护，patch不应该把它们重置回pydantic默认值。"""
    store.write_poi_detail(
        isolated_project, actor="poi_detail_agent",
        poi_detail=PoiDetailFile(
            poi_name="武侯祠",
            day=1,
            routes=[
                RouteOption(
                    route_name="经典线",
                    route_segments=[
                        RouteSegment(order=1, point_name="武侯祠", highlight="参观主体")
                    ],
                )
            ],
        ),
    )
    store.mark_poi_force_finalize(
        isolated_project, 1, "武侯祠", auto_collected=[{"title": "补充素材"}], retry_history=["第1轮：xxx"],
    )

    updated = store.write_poi_detail_patch(
        isolated_project, "poi_detail_agent", 1, "武侯祠",
        [{"route_name": "经典线", "suitable_for": "改过"}],
    )

    assert updated.citation_status == "force_finalized"
    assert updated.auto_collected == [{"title": "补充素材"}]
    assert updated.retry_history == ["第1轮：xxx"]


def test_write_poi_detail_patch_replaces_only_submitted_segment(isolated_project):
    """途经点返修只替换(route_name, order)，路线元数据、相邻途经点和人工照片均保留。"""
    detail = PoiDetailFile(
        poi_name="武侯祠",
        day=1,
        needs_detail_reason="大型景区",
        uploaded_overview_photo_path="outputs/poi_photos/day1_武侯祠/overview.jpg",
        routes=[
            RouteOption(
                route_name="经典线",
                suitable_for="首次到访",
                recommended=True,
                recommend_reason="覆盖核心区域",
                route_segments=[
                    RouteSegment(
                        order=1,
                        point_name="山门",
                        highlight="从山门进入",
                        uploaded_photo_path="outputs/poi_photos/day1_武侯祠/seg_经典线_1.jpg",
                    ),
                    RouteSegment(
                        order=2,
                        point_name="刘备殿",
                        highlight="旧的具体表述",
                        uploaded_photo_path="outputs/poi_photos/day1_武侯祠/seg_经典线_2.jpg",
                    ),
                ],
            ),
            RouteOption(route_name="快速线", suitable_for="时间有限"),
        ],
    )
    store.write_poi_detail(isolated_project, "poi_detail_agent", detail)

    updated = store.write_poi_detail_patch(
        isolated_project,
        "poi_detail_agent",
        1,
        "武侯祠",
        [
            {
                "route_name": "经典线",
                "route_segments": [
                    {
                        "order": 2,
                        "point_name": "刘备殿",
                        "highlight": "已收窄为素材支持的介绍",
                        "uploaded_photo_path": "模型无权覆盖.jpg",
                    }
                ],
            }
        ],
    )

    by_name = {route.route_name: route for route in updated.routes}
    classic = by_name["经典线"]
    assert classic.suitable_for == "首次到访"
    assert classic.recommended is True
    assert classic.recommend_reason == "覆盖核心区域"
    assert [segment.order for segment in classic.route_segments] == [1, 2]
    assert classic.route_segments[0].highlight == "从山门进入"
    assert classic.route_segments[0].uploaded_photo_path.endswith("seg_经典线_1.jpg")
    assert classic.route_segments[1].highlight == "已收窄为素材支持的介绍"
    assert classic.route_segments[1].uploaded_photo_path.endswith("seg_经典线_2.jpg")
    assert by_name["快速线"].suitable_for == "时间有限"
    assert updated.uploaded_overview_photo_path.endswith("overview.jpg")
    assert updated.needs_detail_reason == "大型景区"


def test_write_poi_detail_patch_empty_segments_preserves_existing_segments(isolated_project):
    detail = PoiDetailFile(
        poi_name="武侯祠",
        day=1,
        routes=[
            RouteOption(
                route_name="经典线",
                route_segments=[RouteSegment(order=1, point_name="山门")],
            )
        ],
    )
    store.write_poi_detail(isolated_project, "poi_detail_agent", detail)

    updated = store.write_poi_detail_patch(
        isolated_project,
        "poi_detail_agent",
        1,
        "武侯祠",
        [{"route_name": "经典线", "suitable_for": "亲子", "route_segments": []}],
    )

    assert updated.routes[0].suitable_for == "亲子"
    assert [segment.point_name for segment in updated.routes[0].route_segments] == ["山门"]


def test_write_poi_detail_patch_rejects_duplicate_submitted_segment_order(isolated_project):
    store.write_poi_detail(
        isolated_project,
        "poi_detail_agent",
        PoiDetailFile(
            poi_name="武侯祠",
            day=1,
            routes=[
                RouteOption(
                    route_name="经典线",
                    route_segments=[RouteSegment(order=1, point_name="山门")],
                )
            ],
        ),
    )

    with pytest.raises(ValueError, match="重复提交order=1"):
        store.write_poi_detail_patch(
            isolated_project,
            "poi_detail_agent",
            1,
            "武侯祠",
            [
                {
                    "route_name": "经典线",
                    "route_segments": [
                        {"order": 1, "point_name": "山门"},
                        {"order": 1, "point_name": "另一个山门"},
                    ],
                }
            ],
        )


def test_mark_route_days_force_finalized_marks_only_specified_days(isolated_project):
    """docs/ISSUES.md第125条：route层此前完全没有记录"这一天的核验最终是否通过"，
    只能靠人工翻conflicts.jsonl。跟mark_poi_force_finalize同款设计：只改route_plan.json
    自己的字段，只标记传入的天数，不误伤其他天。"""
    store.write_route_plan(
        isolated_project, "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都"), RouteDay(day=2, city="都江堰")]),
    )

    store.mark_route_days_force_finalized(isolated_project, [1])

    plan = store.read_route_plan(isolated_project)
    by_day = {d.day: d for d in plan.days}
    assert by_day[1].citation_status == "force_finalized"
    assert by_day[2].citation_status == "passed"


def test_mark_lifestyle_items_force_finalized_marks_only_specified_keys(isolated_project):
    """跟mark_route_days_force_finalized同一个设计意图，粒度是(field, day)。"""
    store.write_agent_output_patch(
        isolated_project, "lifestyle_agent",
        [
            {"field": "food", "day": 1, "recommendation": "尕面片"},
            {"field": "food", "day": 2, "recommendation": "火锅"},
        ],
    )

    store.mark_lifestyle_items_force_finalized(isolated_project, [("food", 1)])

    output = store.read_lifestyle_output(isolated_project)
    by_day = {item.day: item for item in output.food.items}
    assert by_day[1].citation_status == "force_finalized"
    assert by_day[2].citation_status == "passed"


def test_mark_pitfall_items_force_finalized_marks_only_specified_keys(isolated_project):
    """跟lifestyle同构，标记outputs/pitfall.json。"""
    store.write_pitfall_output_patch(
        isolated_project, "pitfall_agent",
        [
            {"field": "food", "day": 1, "target_recommendation": "尕面片", "warning": "偶有排队"},
            {"field": "food", "day": 2, "target_recommendation": "火锅", "warning": ""},
        ],
    )

    store.mark_pitfall_items_force_finalized(isolated_project, [("food", 1)])

    output = store.read_pitfall_output(isolated_project)
    by_day = {item.day: item for item in output.food.items}
    assert by_day[1].citation_status == "force_finalized"
    assert by_day[2].citation_status == "passed"


def test_mark_route_days_force_finalized_writes_missing_entities(isolated_project):
    """entity级粒度下沉：missing_entities_by_day传入时应写入对应RouteDay.missing_entities，
    不传时（向后兼容旧调用方式）保持原有行为，字段留空。"""
    store.write_route_plan(
        isolated_project, "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="成都"), RouteDay(day=2, city="都江堰")]),
    )

    store.mark_route_days_force_finalized(
        isolated_project, [1], missing_entities_by_day={1: ["德令哈"]}
    )

    plan = store.read_route_plan(isolated_project)
    by_day = {d.day: d for d in plan.days}
    assert by_day[1].missing_entities == ["德令哈"]
    assert by_day[2].missing_entities == []


def test_mark_lifestyle_items_force_finalized_writes_missing_entities(isolated_project):
    """entity级粒度下沉：missing_entities_by_key传入时应写入对应LifestyleItem.missing_entities。"""
    store.write_agent_output_patch(
        isolated_project, "lifestyle_agent",
        [
            {"field": "food", "day": 1, "recommendation": "尕面片"},
            {"field": "food", "day": 2, "recommendation": "火锅"},
        ],
    )

    store.mark_lifestyle_items_force_finalized(
        isolated_project, [("food", 1)], missing_entities_by_key={("food", 1): ["火星营地"]}
    )

    output = store.read_lifestyle_output(isolated_project)
    by_day = {item.day: item for item in output.food.items}
    assert by_day[1].missing_entities == ["火星营地"]
    assert by_day[2].missing_entities == []


def test_mark_pitfall_items_force_finalized_writes_missing_entities(isolated_project):
    """entity级粒度下沉：missing_entities_by_key传入时应写入对应PitfallItem.missing_entities。"""
    store.write_pitfall_output_patch(
        isolated_project, "pitfall_agent",
        [
            {"field": "food", "day": 1, "target_recommendation": "尕面片", "warning": "偶有排队"},
            {"field": "food", "day": 2, "target_recommendation": "火锅", "warning": ""},
        ],
    )

    store.mark_pitfall_items_force_finalized(
        isolated_project, [("food", 1)], missing_entities_by_key={("food", 1): ["马发银西北菜"]}
    )

    output = store.read_pitfall_output(isolated_project)
    by_day = {item.day: item for item in output.food.items}
    assert by_day[1].missing_entities == ["马发银西北菜"]
    assert by_day[2].missing_entities == []


def test_write_poi_detail_patch_requires_poi_detail_agent_permission(isolated_project):
    store.write_poi_detail(
        isolated_project, actor="poi_detail_agent",
        poi_detail=PoiDetailFile(poi_name="武侯祠", day=1, routes=[RouteOption(route_name="经典线")]),
    )
    with pytest.raises(store.PermissionError):
        store.write_poi_detail_patch(
            isolated_project, "lifestyle_agent", 1, "武侯祠",
            [{"route_name": "经典线", "suitable_for": "改过"}],
        )


def test_init_project_creates_progress(isolated_project):
    progress = store.read_progress(isolated_project)
    assert progress.state.value == "需求收集中"


def test_route_agent_can_write_route_plan(isolated_project):
    plan = RoutePlan(days=[])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    assert store.read_route_plan(isolated_project) is not None


def test_lifestyle_agent_cannot_write_route_plan(isolated_project):
    plan = RoutePlan(days=[])
    with pytest.raises(store.PermissionError):
        store.write_route_plan(isolated_project, actor="lifestyle_agent", route_plan=plan)


def test_write_route_plan_patch_first_write_with_no_history(isolated_project):
    """首次写入（没有历史route_plan.json）：只提交一天，结果应该只有这一天。"""
    plan = store.write_route_plan_patch(
        isolated_project, "route_agent", [{"day": 1, "city": "成都", "area": "市区"}]
    )
    assert len(plan.days) == 1
    assert plan.days[0].area == "市区"

    saved = store.read_route_plan(isolated_project)
    assert saved.days[0].area == "市区"


def test_write_route_plan_patch_preserves_untouched_days(isolated_project):
    """核心行为：patch某一天时，其余天必须原样保留，不需要调用方重新提交一遍。"""
    store.write_route_plan_patch(
        isolated_project,
        "route_agent",
        [
            {"day": 1, "city": "成都", "area": "市区"},
            {"day": 2, "city": "成都", "area": "都江堰"},
            {"day": 3, "city": "成都", "area": "郊区"},
        ],
    )

    plan = store.write_route_plan_patch(
        isolated_project, "route_agent", [{"day": 2, "city": "成都", "area": "改成青城山"}]
    )

    assert [d.day for d in plan.days] == [1, 2, 3]
    assert plan.days[0].area == "市区"
    assert plan.days[1].area == "改成青城山"
    assert plan.days[2].area == "郊区"


def test_write_route_plan_patch_can_add_new_day(isolated_project):
    """提交一个此前不存在的day编号，会被当成新增这一天（by_day字典的自然行为）。"""
    store.write_route_plan_patch(isolated_project, "route_agent", [{"day": 1, "city": "成都", "area": "市区"}])

    plan = store.write_route_plan_patch(
        isolated_project, "route_agent", [{"day": 2, "city": "成都", "area": "新加的一天"}]
    )

    assert [d.day for d in plan.days] == [1, 2]
    assert plan.days[1].area == "新加的一天"


def test_write_route_plan_patch_preserves_amap_verified_by_default(isolated_project):
    """amap_verified不传时保留原值，不会被单天patch意外改动。"""
    store.write_route_plan(
        isolated_project, actor="route_agent", route_plan=RoutePlan(days=[RouteDay(day=1, city="成都")], amap_verified=True)
    )

    plan = store.write_route_plan_patch(
        isolated_project, "route_agent", [{"day": 1, "city": "成都", "area": "改动"}]
    )

    assert plan.amap_verified is True


def test_write_route_plan_patch_can_explicitly_update_amap_verified(isolated_project):
    store.write_route_plan_patch(isolated_project, "route_agent", [{"day": 1, "city": "成都"}])

    plan = store.write_route_plan_patch(
        isolated_project, "route_agent", [{"day": 1, "city": "成都"}], amap_verified=True
    )

    assert plan.amap_verified is True


def test_write_route_plan_patch_requires_route_agent_permission(isolated_project):
    with pytest.raises(store.PermissionError):
        store.write_route_plan_patch(isolated_project, "lifestyle_agent", [{"day": 1, "city": "成都"}])


def test_lifestyle_agent_cannot_write_final_guide(isolated_project):
    with pytest.raises(store.PermissionError):
        store.write_final_guide(isolated_project, actor="lifestyle_agent", content_md="# x", meta={})


def test_write_agent_output_patch_first_write_with_no_history(isolated_project):
    """首次写入（没有历史lifestyle.json）：只提交一个字段一天，其余字段应该是空的。"""
    output = store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": "钵钵鸡", "sources": [], "claimed_entities": ["钵钵鸡"]}],
    )
    assert output.food.items[0].recommendation == "钵钵鸡"
    assert output.outfit.items == []
    assert output.lodging.items == []
    assert output.play.items == []
    assert output.last_updated_field == "food"

    saved = store.read_lifestyle_output(isolated_project)
    assert saved.food.items[0].recommendation == "钵钵鸡"


def test_write_agent_output_patch_preserves_untouched_fields_and_days(isolated_project):
    """核心行为：对已有产出做单字段单天patch，其余字段/天必须原样保留，
    不需要调用方重新提交一遍——这是本次改动要解决的问题本身。"""
    store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [
            {"field": "food", "day": 1, "recommendation": "原始美食推荐day1", "sources": [], "claimed_entities": []},
            {"field": "food", "day": 2, "recommendation": "原始美食推荐day2", "sources": [], "claimed_entities": []},
            {"field": "outfit", "day": 1, "recommendation": "原始穿搭推荐", "sources": [], "claimed_entities": []},
        ],
    )

    output = store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": "换成了甜品店", "sources": [], "claimed_entities": []}],
    )

    assert output.food.items[0].day == 1
    assert output.food.items[0].recommendation == "换成了甜品店"
    # 未被这次patch提及的food.day2必须原样保留
    assert output.food.items[1].day == 2
    assert output.food.items[1].recommendation == "原始美食推荐day2"
    # 未被这次patch提及的outfit字段必须原样保留
    assert output.outfit.items[0].recommendation == "原始穿搭推荐"
    assert output.last_updated_field == "food"


def test_write_agent_output_patch_rejects_unknown_field(isolated_project):
    with pytest.raises(ValueError):
        store.write_agent_output_patch(
            isolated_project,
            "lifestyle_agent",
            [{"field": "not_a_real_field", "day": 1, "recommendation": "x", "sources": [], "claimed_entities": []}],
        )


def test_write_agent_output_patch_requires_lifestyle_agent_permission(isolated_project):
    with pytest.raises(store.PermissionError):
        store.write_agent_output_patch(
            isolated_project,
            "route_agent",
            [{"field": "food", "day": 1, "recommendation": "x", "sources": [], "claimed_entities": []}],
        )


def test_write_pitfall_output_patch_first_write_with_no_history(isolated_project):
    """首次写入（没有历史pitfall.json）：只提交一个字段一天，其余字段应该是空的。"""
    output = store.write_pitfall_output_patch(
        isolated_project,
        "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "钵钵鸡", "warning": "有卫生问题投诉"}],
    )
    assert output.food.items[0].warning == "有卫生问题投诉"
    assert output.outfit.items == []
    assert output.lodging.items == []
    assert output.play.items == []

    saved = store.read_pitfall_output(isolated_project)
    assert saved.food.items[0].warning == "有卫生问题投诉"


def test_write_pitfall_output_patch_preserves_untouched_fields_and_days(isolated_project):
    """核心行为：对已有产出做单字段单天patch，其余字段/天必须原样保留，
    不需要调用方重新提交一遍。"""
    store.write_pitfall_output_patch(
        isolated_project,
        "pitfall_agent",
        [
            {"field": "food", "day": 1, "target_recommendation": "钵钵鸡", "warning": "原始food.day1避雷内容"},
            {"field": "food", "day": 2, "target_recommendation": "某某火锅", "warning": "原始food.day2避雷内容"},
            {"field": "outfit", "day": 1, "target_recommendation": "某某穿搭建议", "warning": "原始outfit避雷内容"},
        ],
    )

    output = store.write_pitfall_output_patch(
        isolated_project,
        "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "钵钵鸡", "warning": "更新后的food.day1避雷内容"}],
    )

    assert output.food.items[0].day == 1
    assert output.food.items[0].warning == "更新后的food.day1避雷内容"
    # 未被这次patch提及的food.day2必须原样保留
    assert output.food.items[1].day == 2
    assert output.food.items[1].warning == "原始food.day2避雷内容"
    # 未被这次patch提及的outfit字段必须原样保留
    assert output.outfit.items[0].warning == "原始outfit避雷内容"


def test_write_pitfall_output_patch_allows_legitimate_empty_warning(isolated_project):
    """warning留空是合法结果（这条推荐确实没查到避雷内容），patch必须能正常保存，
    不应该被误判成"没有内容不用写"。"""
    output = store.write_pitfall_output_patch(
        isolated_project,
        "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "钵钵鸡", "warning": ""}],
    )
    assert output.food.items[0].warning == ""
    assert output.updated_at


def test_write_pitfall_output_patch_rejects_unknown_field(isolated_project):
    with pytest.raises(ValueError):
        store.write_pitfall_output_patch(
            isolated_project,
            "pitfall_agent",
            [{"field": "not_a_real_field", "day": 1, "target_recommendation": "x", "warning": ""}],
        )


def test_write_pitfall_output_patch_requires_pitfall_agent_permission(isolated_project):
    with pytest.raises(store.PermissionError):
        store.write_pitfall_output_patch(
            isolated_project,
            "route_agent",
            [{"field": "food", "day": 1, "target_recommendation": "x", "warning": ""}],
        )


def test_only_integration_agent_can_write_final_guide(isolated_project):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略", meta={})
    content = store.read_workspace(isolated_project, "final_guide.md")
    assert "攻略" in content


def test_write_final_guide_validates_meta_without_dropping_extra_fields(isolated_project):
    with pytest.raises(Exception):
        store.write_final_guide(
            isolated_project,
            actor="integration_agent",
            content_md="# 攻略",
            meta={"merged_fields": "route_plan"},
        )

    meta = {"merged_fields": [], "custom_note": "保留模型扩展字段"}
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略", meta=meta)
    assert store.read_workspace(isolated_project, "final_guide.meta.json")["custom_note"] == "保留模型扩展字段"


def test_write_operations_append_changelog(isolated_project):
    plan = RoutePlan(days=[])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    changelog = store.read_workspace(isolated_project, "changelog.jsonl")
    assert len(changelog) >= 1
    assert changelog[-1]["actor"] == "route_agent"


def test_conflict_retry_flow(isolated_project):
    record = store.report_conflict(isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason="太远了")
    assert record.status == "pending"
    assert record.retry_count == 0

    store.increment_retry_count(isolated_project, record.id)
    pending = store.get_pending_conflict(isolated_project)
    assert pending.retry_count == 1

    store.resolve_conflict(isolated_project, record.id)
    assert store.get_pending_conflict(isolated_project) is None


def test_conflict_issue_type_defaults_empty_and_can_be_set(isolated_project):
    """docs/ARCHITECTURE.md第19.2节：issue_type是新增的结构化字段，默认空字符串
    （兼容历史记录没有这个字段），显式传入时应该原样落盘。"""
    untyped = store.report_conflict(isolated_project, actor="citation_checker", target="route_agent", reason="缺失依据")
    assert untyped.issue_type == ""

    typed = store.report_conflict(
        isolated_project,
        actor="citation_checker",
        target="route_agent.day1",
        reason="越权改动",
        issue_type="route_edit_overreach",
    )
    assert typed.issue_type == "route_edit_overreach"

    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["issue_type"] == "route_edit_overreach"


def test_force_finalize_marks_all_pending(isolated_project):
    store.report_conflict(isolated_project, actor="integration_agent", target="route_agent", reason="路线有问题")
    store.mark_force_finalize(isolated_project)
    assert store.get_pending_conflict(isolated_project) is None


def test_get_latest_unresolved_conflict_finds_stale_force_finalized_record(isolated_project):
    """docs/ISSUES.md第138条：跨node调用的记账修复——上一次调用耗尽MAX_RETRY留下的
    force_finalized记录，这次调用应该能找到，而不是从None开始完全失联。"""
    record = store.report_conflict(
        isolated_project, actor="citation_checker", target="route_agent", reason="路线引用核验未通过（共1处）：Day1缺失"
    )
    store.mark_force_finalize(isolated_project)

    found = store.get_latest_unresolved_conflict(isolated_project, "route_agent")
    assert found is not None
    assert found.id == record.id
    assert found.status == "force_finalized"


def test_get_latest_unresolved_conflict_returns_none_when_already_resolved(isolated_project):
    record = store.report_conflict(isolated_project, actor="citation_checker", target="route_agent", reason="路线引用核验未通过")
    store.resolve_conflict(isolated_project, record.id)

    assert store.get_latest_unresolved_conflict(isolated_project, "route_agent") is None


def test_get_latest_unresolved_conflict_reason_prefix_disambiguates_same_target(isolated_project):
    """route_citation_check_node和route_timeliness_check_node共用target="route_agent"，
    但reason前缀不同——不传reason_prefix会把两者混在一起，传了应该只匹配对应那一类。"""
    citation_record = store.report_conflict(
        isolated_project, actor="citation_checker", target="route_agent", reason="路线引用核验未通过（共1处）：Day1缺失"
    )
    store.mark_force_finalize(isolated_project)
    store.report_conflict(
        isolated_project, actor="citation_checker", target="route_agent", reason="路线时效核验未通过（共1处）：Day2施工"
    )

    citation_found = store.get_latest_unresolved_conflict(
        isolated_project, "route_agent", reason_prefix="路线引用核验未通过"
    )
    assert citation_found is not None
    assert citation_found.id == citation_record.id

    timeliness_found = store.get_latest_unresolved_conflict(
        isolated_project, "route_agent", reason_prefix="路线时效核验未通过"
    )
    assert timeliness_found is not None
    assert timeliness_found.id != citation_record.id


def test_read_workspace_on_directory_does_not_crash(isolated_project):
    # agent有时会传目录路径（比如"outputs"而不是"outputs/lifestyle.json"）
    # 之前在Windows上会因为对目录调read_text()报PermissionError，这里验证已经修好
    result = store.read_workspace(isolated_project, "outputs")
    assert isinstance(result, list)


def test_read_run_status_none_when_never_written(isolated_project):
    assert store.read_run_status(isolated_project) is None


def test_write_run_status_roundtrip(isolated_project):
    store.write_run_status(isolated_project, {"node": "route_draft", "status": "running", "call_count": 1})
    assert store.read_run_status(isolated_project) == {
        "node": "route_draft", "status": "running", "call_count": 1
    }

    store.write_run_status(isolated_project, {"node": "route_draft", "status": "done", "call_count": 1})
    assert store.read_run_status(isolated_project)["status"] == "done"


# ---------------------------------------------------------------------------
# 素材缺口自动补充计数器（docs/ARCHITECTURE.md第16节）
# ---------------------------------------------------------------------------


def test_material_gap_count_starts_at_zero(isolated_project):
    assert store.read_material_gap_supplement_count(isolated_project) == 0


def test_try_consume_material_gap_budget_increments_count(isolated_project):
    assert store.try_consume_material_gap_budget(isolated_project, max_total=10) is True
    assert store.read_material_gap_supplement_count(isolated_project) == 1
    assert store.try_consume_material_gap_budget(isolated_project, max_total=10) is True
    assert store.read_material_gap_supplement_count(isolated_project) == 2


def test_try_consume_material_gap_budget_refuses_when_at_cap(isolated_project):
    for _ in range(3):
        store.try_consume_material_gap_budget(isolated_project, max_total=3)

    assert store.read_material_gap_supplement_count(isolated_project) == 3
    assert store.try_consume_material_gap_budget(isolated_project, max_total=3) is False
    # 拒绝时不应该有任何改动
    assert store.read_material_gap_supplement_count(isolated_project) == 3


def test_release_material_gap_budget_gives_back_a_slot(isolated_project):
    store.try_consume_material_gap_budget(isolated_project, max_total=10)
    store.try_consume_material_gap_budget(isolated_project, max_total=10)
    assert store.read_material_gap_supplement_count(isolated_project) == 2

    store.release_material_gap_budget(isolated_project)  # 采集失败，还回一个名额

    assert store.read_material_gap_supplement_count(isolated_project) == 1


def test_release_material_gap_budget_does_not_go_below_zero(isolated_project):
    store.release_material_gap_budget(isolated_project)
    assert store.read_material_gap_supplement_count(isolated_project) == 0


def test_material_gap_reservation_is_released_only_by_its_owner(isolated_project):
    first = store.reserve_material_gap_budget(isolated_project, max_total=2)
    second = store.reserve_material_gap_budget(isolated_project, max_total=2)

    assert first and second and first != second
    assert store.reserve_material_gap_budget(isolated_project, max_total=2) is None

    store.release_material_gap_budget(isolated_project, first)

    data = store._read_json(store._material_gap_path(isolated_project))
    assert [item["reservation_id"] for item in data["reservations"]] == [second]
    assert store.finalize_material_gap_budget(isolated_project, first) is False
    assert store.finalize_material_gap_budget(isolated_project, second) is True
    assert store.read_material_gap_supplement_count(isolated_project) == 1


def test_legacy_budget_api_counts_active_reservations_toward_cap(isolated_project):
    reservation = store.reserve_material_gap_budget(isolated_project, max_total=2)

    assert reservation
    assert store.try_consume_material_gap_budget(isolated_project, max_total=2) is True
    assert store.try_consume_material_gap_budget(isolated_project, max_total=2) is False


def test_material_gap_reservation_does_not_overbook_across_processes(isolated_project):
    worker = (
        "from pathlib import Path\n"
        "import sys\n"
        "from src.workspace import store\n"
        "store.PROJECTS_DIR = Path(sys.argv[1])\n"
        "print(store.reserve_material_gap_budget(sys.argv[2], int(sys.argv[3])) or '')\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(store.PROJECTS_DIR), isolated_project, "5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(12)
    ]
    tokens = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        if stdout.strip():
            tokens.append(stdout.strip())

    assert len(tokens) == 5
    assert len(set(tokens)) == 5
    assert store.reserve_material_gap_budget(isolated_project, max_total=5) is None


def test_record_material_gap_supplement_item_does_not_change_count(isolated_project):
    store.try_consume_material_gap_budget(isolated_project, max_total=10)
    store.record_material_gap_supplement_item(
        isolated_project, {"path": "poi_detail", "topic": "成都", "title": "武侯祠讲解", "source_url": "https://x"}
    )

    assert store.read_material_gap_supplement_count(isolated_project) == 1
    data = store._read_json(store._material_gap_path(isolated_project))
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "武侯祠讲解"


def test_try_consume_material_gap_budget_is_thread_safe_under_concurrency(isolated_project):
    """真实起多个线程并发调用try_consume_material_gap_budget，验证锁确实生效——
    这是docs/ARCHITECTURE.md第16节提到的真实并发风险（POI详情agent的Send API并行分支
    可能同时触发这个函数），不是理论假设。如果锁失效，最终count会超过max_total
    （多个线程同时读到同一个旧值、各自+1写回，互相覆盖导致总数被低估或错误递增）。"""
    import threading

    max_total = 20
    successes = []
    lock = threading.Lock()

    def worker():
        result = store.try_consume_material_gap_budget(isolated_project, max_total)
        with lock:
            successes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 50个线程抢20个名额：恰好20个成功，30个失败，且最终计数恰好等于max_total，
    # 不多不少（不多是锁保证了不超发，不少是每次成功的+1都被真实持久化，没有丢更新）。
    assert sum(1 for s in successes if s) == max_total
    assert sum(1 for s in successes if not s) == 50 - max_total
    assert store.read_material_gap_supplement_count(isolated_project) == max_total


# ---------------------------------------------------------------------------
# 回收站：软删除/恢复/永久删除（管理后台"项目软删除+回收站"功能，2026-08-01新增）
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    return tmp_path


def test_soft_delete_project_moves_to_trash_dir(isolated_projects_dir):
    project_id = "test-project"
    store.init_project(project_id)

    store.soft_delete_project(project_id)

    assert not (isolated_projects_dir / project_id).exists()
    assert (isolated_projects_dir / "_trash" / project_id).is_dir()


def test_soft_delete_nonexistent_project_raises(isolated_projects_dir):
    with pytest.raises(FileNotFoundError):
        store.soft_delete_project("does-not-exist")


def test_soft_delete_already_trashed_project_raises(isolated_projects_dir):
    project_id = "test-project"
    store.init_project(project_id)
    store.soft_delete_project(project_id)

    # 恢复后再删一次是正常流程，但直接对同一个已在回收站里的id再删一次应该报错
    # （不应该发生，防御性检查）
    with pytest.raises(FileNotFoundError):
        store.soft_delete_project(project_id)


def test_is_trashed_true_after_soft_delete(isolated_projects_dir):
    project_id = "test-project"
    store.init_project(project_id)
    assert store.is_trashed(project_id) is False

    store.soft_delete_project(project_id)
    assert store.is_trashed(project_id) is True


def test_list_trashed_project_ids_returns_sorted_names(isolated_projects_dir):
    for pid in ["c-project", "a-project", "b-project"]:
        store.init_project(pid)
        store.soft_delete_project(pid)

    assert store.list_trashed_project_ids() == ["a-project", "b-project", "c-project"]


def test_list_trashed_project_ids_empty_when_no_trash_dir(isolated_projects_dir):
    assert store.list_trashed_project_ids() == []


def test_restore_project_moves_back(isolated_projects_dir):
    project_id = "test-project"
    store.init_project(project_id)
    store.soft_delete_project(project_id)

    store.restore_project(project_id)

    assert (isolated_projects_dir / project_id).is_dir()
    assert not store.is_trashed(project_id)
    assert project_id in store.list_project_ids()


def test_restore_nonexistent_trashed_project_raises(isolated_projects_dir):
    with pytest.raises(FileNotFoundError):
        store.restore_project("never-trashed")


def test_restore_project_name_collision_raises(isolated_projects_dir):
    project_id = "test-project"
    store.init_project(project_id)
    store.soft_delete_project(project_id)
    # 一个新project恰好用了同一个id（正常流程里几乎不会发生，但要防御）
    store.init_project(project_id)

    with pytest.raises(ValueError):
        store.restore_project(project_id)


def test_permanently_delete_project_removes_directory(isolated_projects_dir):
    project_id = "test-project"
    store.init_project(project_id)
    store.soft_delete_project(project_id)

    store.permanently_delete_project(project_id)

    assert not (isolated_projects_dir / "_trash" / project_id).exists()
    assert store.list_trashed_project_ids() == []


def test_permanently_delete_nonexistent_raises(isolated_projects_dir):
    with pytest.raises(FileNotFoundError):
        store.permanently_delete_project("never-trashed")


def test_list_project_ids_excludes_trash_dir_itself(isolated_projects_dir):
    store.init_project("normal-project")
    store.init_project("trashed-project")
    store.soft_delete_project("trashed-project")

    ids = store.list_project_ids()
    assert ids == ["normal-project"]
    assert "_trash" not in ids
