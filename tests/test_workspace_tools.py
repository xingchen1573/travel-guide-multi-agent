"""src/tools/workspace_tools.py 的单元测试，重点覆盖docs/ISSUES.md第116条：
lifestyle_agent在citation_check多轮打回重试压力下，把sources[].chunk_id/doc_id的
topic前缀弄丢，退化成裸格式（如"144-c004"而不是"青甘大环线-144-c004"）。

修复方向B（写入端主动纠正，见_normalize_sources_chunk_id）——write_agent_output等
写入工具在写入这一刻就检测并补全裸格式chunk_id/doc_id的topic前缀，从源头堵住这个
退化，不依赖citation_check那边的防御性归一化兜底。
"""
from __future__ import annotations

import json

import pytest

from src.tools import workspace_tools
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store
from src.workspace.schema import RouteDay, RoutePlan, RoutePoi


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "test-project"
    store.init_project(project_id)
    return project_id


def _get_tool(tools, name: str):
    return next(t for t in tools if t.name == name)


def test_integration_write_final_guide_returns_directly(isolated_project):
    """最终攻略成功落盘后不应再回到模型生成一段收尾文字。"""
    write_final_guide = _get_tool(
        make_workspace_tools(isolated_project, "integration_agent"),
        "write_final_guide",
    )

    assert write_final_guide.return_direct is True


def test_route_read_workspace_can_return_one_complete_day(isolated_project):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(day=1, city="德令哈", pois=[RoutePoi(name="青海湖")]),
                RouteDay(
                    day=2,
                    city="锡铁山镇",
                    notes="保留Day2完整内容",
                    pois=[RoutePoi(name="东台吉乃尔湖")],
                ),
            ],
        ),
    )
    read_workspace = _get_tool(
        make_workspace_tools(
            isolated_project,
            "route_agent",
            topic="青甘大环线",
            route_write_mode="proposal_only",
        ),
        "read_workspace",
    )

    selected = json.loads(
        read_workspace.invoke({"relative_path": "route_plan.json", "day": 2})
    )

    assert selected["day"] == 2
    assert selected["notes"] == "保留Day2完整内容"
    assert selected["pois"][0]["name"] == "东台吉乃尔湖"
    assert "青海湖" not in json.dumps(selected, ensure_ascii=False)


def test_route_proposal_tool_injects_system_work_unit_id(isolated_project):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="大理",
                    pois=[RoutePoi(name="洱海生态廊道")],
                )
            ]
        ),
    )
    write_proposal = _get_tool(
        make_workspace_tools(
            isolated_project,
            "route_agent",
            topic="大理",
            route_write_mode="proposal_only",
            route_proposal_work_unit_id="work-unit-erhai-1",
        ),
        "write_route_change_proposal",
    )

    result = write_proposal.invoke(
        {
            "changed_days": [
                {
                    "day": 1,
                    "city": "大理",
                    "pois": [{"name": "喜洲古镇"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "按结构候选安排喜洲古镇",
                            "support_mode": "user_constraint",
                            "evidence_refs": [],
                        }
                    ],
                }
            ],
            "trigger": "timeliness",
            "reason": "原地点当前不可执行",
            "evidence": "官方状态记录",
        }
    )

    assert "路线结构调整提案已保存" in result
    proposal = store.read_route_change_proposal(isolated_project)
    assert proposal["work_unit_id"] == "work-unit-erhai-1"


def test_integration_write_final_guide_accepts_json_wrapped_containers(isolated_project):
    """Some OpenAI-compatible channels wrap array/object tool args as JSON strings."""
    write_final_guide = _get_tool(
        make_workspace_tools(isolated_project, "integration_agent"),
        "write_final_guide",
    )
    blocks = [
        {
            "section": "行前说明",
            "markdown": "# 成都攻略\n\n按确认路线出行。",
            "mapping_mode": "user_constraint",
            "mapping_note": "用户确认的路线方向",
        }
    ]
    meta = {"merged_fields": ["route_plan"], "checklist_included": False}

    result = write_final_guide.invoke(
        {"blocks": json.dumps(blocks, ensure_ascii=False), "meta": json.dumps(meta)}
    )

    assert "最终攻略已由1个正文块" in result
    assert "成都攻略" in store.read_workspace(isolated_project, "final_guide.md")


def test_integration_write_final_guide_repairs_unescaped_prose_quotes(isolated_project):
    write_final_guide = _get_tool(
        make_workspace_tools(isolated_project, "integration_agent"),
        "write_final_guide",
    )
    blocks = (
        '[{"section":"提醒","markdown":"路线中"下午4点前入场"可行，夜景点也叫"九眼桥"。",'
        '"mapping_mode":"editorial","mapping_note":"仅整理已有提醒"}]'
    )

    result = write_final_guide.invoke(
        {
            "blocks": blocks,
            "meta": {"merged_fields": ["route_plan"], "checklist_included": False},
        }
    )

    assert "最终攻略已由1个正文块" in result
    guide = store.read_workspace(isolated_project, "final_guide.md")
    assert '"下午4点前入场"' in guide
    assert '"九眼桥"' in guide


# 真实数据构造：docs/ISSUES.md第116条引用的trace_full.jsonl第439行（call idx 438），
# lifestyle_agent第3轮针对citation_check打回的重写，44条items全部chunk_id/doc_id
# 裸格式——这是其中一条真实item（敦煌Day4美食建议），完整保留原始字段值。
_BARE_ITEM = {
    "field": "food",
    "day": 4,
    "recommendation": (
        "Day4抵敦煌后，晚餐去沙洲夜市（敦煌市区最热闹的夜市，素材确认有沙葱牛肉饼、"
        "甜胚子豆花、红柳羊肉串、烤鱼等网红摊位）。"
    ),
    "sources": [
        {"platform": "bilibili", "author": "敦煌攻略", "doc_id": "144", "chunk_id": "144-c004"},
        {"platform": "bilibili", "author": "敦煌攻略", "doc_id": "144", "chunk_id": "144-c002"},
        {"platform": "douyin", "author": "河*", "doc_id": "073", "chunk_id": "073-c006"},
    ],
    "claimed_entities": ["沙洲夜市", "沙葱牛肉饼", "甜胚子豆花", "红柳羊肉串", "烤鱼"],
}


def _with_claims(item: dict, target_path: str = "recommendation") -> dict:
    """Upgrade a legacy object-level source fixture to the claim write contract."""
    item = dict(item)
    sources = [dict(source) for source in item.get("sources", [])]
    item["claims"] = [
        {
            "target_path": target_path,
            "claim_text": str(item.get(target_path) or target_path),
            "support_mode": "user_constraint",
            "claimed_entities": list(item.get("claimed_entities", [])),
            "evidence_refs": sources,
        }
    ]
    return item


def _with_route_segment_claims(routes: list[dict]) -> list[dict]:
    """Attach one claim per non-empty POI segment field while preserving source fixtures."""
    normalized_routes = []
    for raw_route in routes:
        route = dict(raw_route)
        segments = []
        for raw_segment in route.get("route_segments", []):
            segment = dict(raw_segment)
            sources = [dict(source) for source in segment.get("sources", [])]
            segment["claims"] = [
                {
                    "target_path": field,
                    "claim_text": str(segment[field]),
                    "support_mode": "user_constraint",
                    "claimed_entities": [str(segment.get("point_name") or "")],
                    "evidence_refs": sources,
                }
                for field in ("point_name", "highlight", "suggested_duration")
                if str(segment.get(field) or "").strip()
            ]
            segments.append(segment)
        route["route_segments"] = segments
        normalized_routes.append(route)
    return normalized_routes


def test_lifestyle_write_agent_output_normalizes_bare_chunk_id(isolated_project):
    """核心回归：模型提交裸格式chunk_id/doc_id时，写入工具应该在落盘前自动补全topic前缀，
    不能让裸格式流入outputs/lifestyle.json——否则citation_check.load_chunk_texts()
    按完整chunk_id反查chunks.jsonl会查不到原文，把这条本来有真实依据的内容误判为
    "原文片段为空"而假阳性打回。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    result = write_agent_output.invoke({"items": [_with_claims(_BARE_ITEM)]})

    assert "已自动修正" in result
    saved = store.read_lifestyle_output(isolated_project)
    saved_sources = saved.food.items[0].sources
    assert [s.chunk_id for s in saved_sources] == [
        "青甘大环线-144-c004",
        "青甘大环线-144-c002",
        "青甘大环线-073-c006",
    ]
    assert [s.doc_id for s in saved_sources] == ["青甘大环线-144", "青甘大环线-144", "青甘大环线-073"]


def test_lifestyle_write_agent_output_leaves_correct_chunk_id_untouched(isolated_project):
    """已经带正确前缀的chunk_id（第1/2轮的正常情况）不应该被误改成双重前缀，
    也不应该在返回消息里报告"已自动修正"。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    item = dict(_BARE_ITEM)
    item["sources"] = [
        {"platform": "bilibili", "author": "敦煌攻略", "doc_id": "青甘大环线-144", "chunk_id": "青甘大环线-144-c004"}
    ]
    result = write_agent_output.invoke({"items": [_with_claims(item)]})

    assert "已自动修正" not in result
    saved = store.read_lifestyle_output(isolated_project)
    assert saved.food.items[0].sources[0].chunk_id == "青甘大环线-144-c004"


def test_lifestyle_write_requires_explicit_support_mode(isolated_project):
    """新写入不得因模型漏字段而默认成direct；历史Schema默认仅保留读取兼容。"""
    write_agent_output = _get_tool(
        make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线"),
        "write_agent_output",
    )

    result = write_agent_output.invoke(
        {
            "items": [
                {
                    "field": "play",
                    "day": 2,
                    "recommendation": "适合按固定路线顺路游览",
                    "claims": [
                        {
                            "target_path": "recommendation",
                            "claim_text": "适合按固定路线顺路游览",
                            "evidence_refs": [],
                        }
                    ],
                }
            ]
        }
    )

    assert "必须显式填写support_mode" in result
    assert "不会默认猜成direct" in result
    assert store.read_lifestyle_output(isolated_project) is None


def test_lifestyle_write_agent_output_does_not_touch_web_verified_chunk_id(isolated_project):
    """platform=="web_verified"的chunk_id是web_search结果现起的合成字符串
    （如"web-XXX-YYY"），不属于topic素材库索引体系，不应该被拼上topic前缀。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    item = dict(_BARE_ITEM)
    item["sources"] = [
        {"platform": "web_verified", "author": "", "doc_id": "web-1", "chunk_id": "web-翡翠湖-门票价格"}
    ]
    result = write_agent_output.invoke({"items": [_with_claims(item)]})

    assert "已自动修正" not in result
    saved = store.read_lifestyle_output(isolated_project)
    assert saved.food.items[0].sources[0].chunk_id == "web-翡翠湖-门票价格"


def test_pitfall_write_agent_output_normalizes_bare_chunk_id(isolated_project):
    """避雷agent的write_agent_output跟lifestyle同一套sources结构，同款风险，同款修复。"""
    store.write_agent_output_patch(
        isolated_project,
        "lifestyle_agent",
        [{"field": "food", "day": 4, "recommendation": "沙洲夜市完整推荐原文", "sources": []}],
    )
    tools = make_workspace_tools(isolated_project, "pitfall_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    item = {
        "field": "food",
        "day": 4,
        "target_recommendation": "沙洲夜市",
        "warning": "部分摊位价格虚高，建议提前砍价",
        "sources": [{"platform": "bilibili", "author": "敦煌攻略", "doc_id": "144", "chunk_id": "144-c004"}],
        "claimed_entities": ["沙洲夜市"],
    }
    result = write_agent_output.invoke({"items": [_with_claims(item, "warning")]})

    assert "已自动修正" in result
    saved = store.read_pitfall_output(isolated_project)
    assert saved.food.items[0].sources[0].chunk_id == "青甘大环线-144-c004"
    assert saved.food.items[0].target_recommendation == "沙洲夜市完整推荐原文"
    assert "纠正1处target_recommendation绑定" in result


def test_pitfall_write_agent_output_rejects_target_missing_from_lifestyle(isolated_project):
    tools = make_workspace_tools(isolated_project, "pitfall_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    result = write_agent_output.invoke(
        {"items": [{"field": "food", "day": 4, "target_recommendation": "沙洲夜市", "warning": ""}]}
    )

    assert "在outputs/lifestyle.json中不存在" in result
    assert store.read_pitfall_output(isolated_project) is None


def test_route_write_route_plan_normalizes_bare_chunk_id(isolated_project):
    """route_agent的write_route_plan同款sources结构，同款风险，同款修复。"""
    tools = make_workspace_tools(isolated_project, "route_agent", topic="青甘大环线")
    write_route_plan = _get_tool(tools, "write_route_plan")

    day = {
        "day": 1,
        "city": "敦煌",
        "pois": [{"name": "鸣沙山月牙泉"}],
        "claims": [{
            "target_path": "pois.0.name",
            "claim_text": "按用户要求安排鸣沙山月牙泉",
            "support_mode": "user_constraint",
            "evidence_refs": [
                {"platform": "bilibili", "author": "敦煌攻略", "doc_id": "144", "chunk_id": "144-c001"}
            ],
        }],
    }
    result = write_route_plan.invoke({"days": [day]})

    assert "已自动修正" in result
    saved = store.read_route_plan(isolated_project)
    assert saved.days[0].sources[0].chunk_id == "青甘大环线-144-c001"


def test_route_write_route_plan_rejects_empty_days_without_overwriting(isolated_project):
    store.write_route_plan(
        isolated_project,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="大理",
                    pois=[RoutePoi(name="洱海生态廊道")],
                )
            ]
        ),
    )
    route_before = store.read_workspace(isolated_project, "route_plan.json")
    changelog_before = store.read_workspace(isolated_project, "changelog.jsonl")
    write_route_plan = _get_tool(
        make_workspace_tools(isolated_project, "route_agent", topic="大理/洱海"),
        "write_route_plan",
    )

    result = write_route_plan.invoke({"days": []})

    assert result == "写入失败：days不能为空"
    assert store.read_workspace(isolated_project, "route_plan.json") == route_before
    assert store.read_workspace(isolated_project, "changelog.jsonl") == changelog_before


def test_route_write_requires_explicit_support_mode(isolated_project):
    write_route_plan = _get_tool(
        make_workspace_tools(isolated_project, "route_agent", topic="青甘大环线"),
        "write_route_plan",
    )

    result = write_route_plan.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "西宁",
                    "pois": [{"name": "青海湖"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "按用户固定路线安排青海湖",
                            "evidence_refs": [],
                        }
                    ],
                }
            ]
        }
    )

    assert "必须显式填写support_mode" in result
    assert store.read_route_plan(isolated_project) is None


def test_route_write_route_plan_recovers_string_evidence_ref(
    isolated_project, monkeypatch
):
    """Nested claim schemas are free-form dicts, so models may copy only chunk_id."""
    monkeypatch.setattr(
        "src.materials.citation_check.load_chunk_texts",
        lambda topic, project_id=None: {
            "青甘大环线-144-c001": "鸣沙山月牙泉是敦煌行程中的游览地点。"
        },
    )
    tools = make_workspace_tools(isolated_project, "route_agent", topic="青甘大环线")
    write_route_plan = _get_tool(tools, "write_route_plan")

    day = {
        "day": 1,
        "city": "敦煌",
        "pois": [{"name": "鸣沙山月牙泉"}],
        "claims": [{
            "target_path": "pois.0",
            "claim_text": "安排鸣沙山月牙泉",
            "support_mode": "direct",
            "claimed_entities": ["鸣沙山月牙泉"],
            "evidence_refs": ["青甘大环线-144-c001"],
        }],
    }
    result = write_route_plan.invoke({"days": [day]})

    assert "路线规划已写入" in result
    assert "已自动修正" in result
    saved = store.read_route_plan(isolated_project)
    source = saved.days[0].claims[0].evidence_refs[0]
    assert saved.days[0].claims[0].target_path == "pois.0.name"
    assert source.chunk_id == "青甘大环线-144-c001"
    assert source.doc_id == "青甘大环线-144"
    assert source.platform != ""


def test_route_write_route_plan_missing_claim_identifies_day_and_poi(isolated_project):
    tools = make_workspace_tools(isolated_project, "route_agent", topic="青甘大环线")
    write_route_plan = _get_tool(tools, "write_route_plan")

    result = write_route_plan.invoke({
        "days": [{
            "day": 2,
            "city": "峨眉山",
            "pois": [
                {"name": "报国寺", "brief": "低山区寺庙"},
                {"name": "伏虎寺", "brief": "从报国寺步行可达"},
            ],
            "claims": [
                {
                    "target_path": "pois.0.name",
                    "claim_text": "安排报国寺",
                    "support_mode": "estimate",
                },
                {
                    "target_path": "pois.0.brief",
                    "claim_text": "报国寺位于低山区",
                    "support_mode": "estimate",
                },
                {
                    "target_path": "pois.1.name",
                    "claim_text": "安排伏虎寺",
                    "support_mode": "estimate",
                },
                {
                    "target_path": "Day2.pois.1.brief",
                    "claim_text": "伏虎寺从报国寺步行可达",
                    "support_mode": "estimate",
                },
            ],
        }]
    })

    assert 'Day2.pois.1.brief（伏虎寺）缺少claim，应填写target_path="pois.1.brief"' in result
    assert "target_path必须使用当前Day内部的局部路径" in result
    assert "不要添加Day1.或days[0].等前缀" in result


def test_route_write_route_plan_patch_normalizes_bare_chunk_id(isolated_project):
    tools = make_workspace_tools(isolated_project, "route_agent", topic="青甘大环线")
    write_route_plan = _get_tool(tools, "write_route_plan")
    write_route_plan_patch = _get_tool(tools, "write_route_plan_patch")

    write_route_plan.invoke({
        "days": [{
            "day": 1,
            "city": "敦煌",
            "pois": [],
            "claims": [{
                "target_path": "day",
                "claim_text": "建立Day1路线骨架",
                "support_mode": "user_constraint",
            }],
        }]
    })

    day = {
        "day": 1,
        "city": "敦煌",
        "pois": [{"name": "鸣沙山月牙泉"}],
        "claims": [{
            "target_path": "pois.0.name",
            "claim_text": "按用户要求安排鸣沙山月牙泉",
            "support_mode": "user_constraint",
            "evidence_refs": [
                {"platform": "bilibili", "author": "敦煌攻略", "doc_id": "144", "chunk_id": "144-c001"}
            ],
        }],
    }
    result = write_route_plan_patch.invoke({"days": [day]})

    assert "已自动修正" in result
    saved = store.read_route_plan(isolated_project)
    assert saved.days[0].sources[0].chunk_id == "青甘大环线-144-c001"


def test_poi_detail_write_agent_output_normalizes_bare_chunk_id_in_nested_sources(isolated_project):
    """POI详情agent的sources嵌在routes -> route_segments里，多一层结构，
    _normalize_route_segments_chunk_id要能钻进这层嵌套里正确归一化。"""
    tools = make_workspace_tools(
        isolated_project, "poi_detail_agent", day=1, poi_name="鸣沙山月牙泉", topic="青甘大环线"
    )
    write_agent_output = _get_tool(tools, "write_agent_output")

    routes = [
        {
            "route_name": "经典线",
            "route_segments": [
                {
                    "order": 1,
                    "point_name": "山门",
                    "sources": [{"platform": "bilibili", "author": "敦煌攻略", "doc_id": "144", "chunk_id": "144-c001"}],
                }
            ],
        }
    ]
    result = write_agent_output.invoke({"routes": _with_route_segment_claims(routes)})

    assert "已自动修正" in result
    saved = store.read_poi_detail(isolated_project, 1, "鸣沙山月牙泉")
    assert saved.routes[0].route_segments[0].sources[0].chunk_id == "青甘大环线-144-c001"


def test_poi_detail_write_agent_output_patch_normalizes_bare_chunk_id(isolated_project):
    tools = make_workspace_tools(
        isolated_project, "poi_detail_agent", day=1, poi_name="鸣沙山月牙泉", topic="青甘大环线"
    )
    write_agent_output = _get_tool(tools, "write_agent_output")
    write_agent_output_patch = _get_tool(tools, "write_agent_output_patch")

    write_agent_output.invoke(
        {
            "routes": _with_route_segment_claims(
                [{"route_name": "经典线", "route_segments": [{"order": 1, "point_name": "山门"}]}]
            )
        }
    )

    routes = [
        {
            "route_name": "经典线",
            "route_segments": [
                {
                    "order": 1,
                    "point_name": "山门",
                    "sources": [{"platform": "bilibili", "author": "敦煌攻略", "doc_id": "144", "chunk_id": "144-c001"}],
                }
            ],
        }
    ]
    result = write_agent_output_patch.invoke({"routes": _with_route_segment_claims(routes)})

    assert "已自动修正" in result
    saved = store.read_poi_detail(isolated_project, 1, "鸣沙山月牙泉")
    assert saved.routes[0].route_segments[0].sources[0].chunk_id == "青甘大环线-144-c001"


def test_poi_detail_write_agent_output_patch_preserves_sibling_segment(isolated_project):
    """工具层只提交一个途经点时，Store应保留同一路线其余途经点。"""
    tools = make_workspace_tools(
        isolated_project,
        "poi_detail_agent",
        day=1,
        poi_name="武侯祠",
        topic="成都",
    )
    write_agent_output = _get_tool(tools, "write_agent_output")
    write_agent_output_patch = _get_tool(tools, "write_agent_output_patch")
    initial_routes = _with_route_segment_claims(
        [
            {
                "route_name": "经典线",
                "suitable_for": "首次到访",
                "route_segments": [
                    {"order": 1, "point_name": "山门", "highlight": "入口"},
                    {"order": 2, "point_name": "刘备殿", "highlight": "旧表述"},
                ],
            }
        ]
    )
    assert "写入失败" not in write_agent_output.invoke({"routes": initial_routes})

    patch_routes = _with_route_segment_claims(
        [
            {
                "route_name": "经典线",
                "route_segments": [
                    {"order": 2, "point_name": "刘备殿", "highlight": "已收窄"}
                ],
            }
        ]
    )
    result = write_agent_output_patch.invoke({"routes": patch_routes})

    assert "写入失败" not in result
    saved = store.read_poi_detail(isolated_project, 1, "武侯祠")
    assert saved.routes[0].suitable_for == "首次到访"
    assert [segment.point_name for segment in saved.routes[0].route_segments] == [
        "山门",
        "刘备殿",
    ]
    assert saved.routes[0].route_segments[0].highlight == "入口"
    assert saved.routes[0].route_segments[1].highlight == "已收窄"


def test_lifestyle_write_agent_output_without_topic_leaves_chunk_id_unchanged(isolated_project):
    """topic未传（None/空字符串）时不应该报错，也不应该做任何归一化——防御性兜底，
    不是这个函数的正常调用路径（所有真实agent构建函数都会传topic），但不能因为
    topic缺失就崩溃或产生错误的拼接结果。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent")
    write_agent_output = _get_tool(tools, "write_agent_output")

    result = write_agent_output.invoke({"items": [_with_claims(_BARE_ITEM)]})

    assert "已自动修正" not in result
    saved = store.read_lifestyle_output(isolated_project)
    assert saved.food.items[0].sources[0].chunk_id == "144-c004"


# ---------------------------------------------------------------------------
# _normalize_synthetic_platform（docs/ISSUES.md第133条真实案例修复）


def test_normalize_legacy_append_material_reference_to_project_evidence():
    sources, fixed = workspace_tools._normalize_synthetic_platform(
        [
            {
                "platform": "web_search",
                "chunk_id": "evidence-abc123",
                "scope": "project",
                "source_url": "https://example.com/evidence",
            }
        ]
    )

    assert fixed == 3
    assert sources == [
        {
            "platform": "project_evidence",
            "doc_id": "abc123",
            "chunk_id": "project-evidence-abc123",
            "scope": "project",
            "source_url": "https://example.com/evidence",
        }
    ]
# ---------------------------------------------------------------------------


def test_poi_detail_write_agent_output_corrects_web_platform_to_web_verified(isolated_project):
    """真实复现场景：POI详情agent的prompt.md明确要求"查到更新数字就...在对应sources里
    标注platform: web_verified"，但真实产出里模型写成了"web"（茶卡盐湖POI详情7条
    sources全部如此）——chunk_id/doc_id本身已经是合成的"{topic}-web_verified"格式，
    只是platform字段没写对，导致citation_check._merge_web_verified_evidence精确匹配
    platform=="web_verified"时完全找不到这些条目，真实web核实内容被误判为空。"""
    tools = make_workspace_tools(
        isolated_project, "poi_detail_agent", day=1, poi_name="茶卡盐湖", topic="青甘大环线"
    )
    write_agent_output = _get_tool(tools, "write_agent_output")

    routes = [
        {
            "route_name": "经典线",
            "route_segments": [
                {
                    "order": 1,
                    "point_name": "茶卡壹号·盐湖景区大门",
                    "sources": [
                        {
                            "platform": "web",
                            "author": "茶卡壹號·鹽湖景區（官方介绍）",
                            "doc_id": "青甘大环线-web_verified",
                            "chunk_id": "青甘大环线-web_verified",
                        }
                    ],
                }
            ],
        }
    ]
    result = write_agent_output.invoke({"routes": _with_route_segment_claims(routes)})

    assert "已自动修正" in result
    saved = store.read_poi_detail(isolated_project, 1, "茶卡盐湖")
    assert saved.routes[0].route_segments[0].sources[0].platform == "web_verified"


def test_poi_detail_write_agent_output_repairs_legacy_web_source_without_chunk_id(isolated_project):
    """兼容真实成都项目形态：模型写platform=web、只给描述性doc_id、chunk_id留空。
    这类记录也必须被归一化成可供web_search日志核验的web_verified合成来源。"""
    tools = make_workspace_tools(
        isolated_project, "poi_detail_agent", day=1, poi_name="都江堰景区", topic="成都"
    )
    write_agent_output = _get_tool(tools, "write_agent_output")

    result = write_agent_output.invoke(
        {
            "routes": _with_route_segment_claims([
                {
                    "route_name": "经典线",
                    "route_segments": [
                        {
                            "order": 1,
                            "point_name": "入口",
                            "highlight": "景区入口",
                            "sources": [
                                {
                                    "platform": "web",
                                    "doc_id": "成都-dujiang-dam-都江堰",
                                    "chunk_id": "",
                                }
                            ],
                        }
                    ],
                }
            ])
        }
    )

    assert "已自动修正" in result
    saved = store.read_poi_detail(isolated_project, 1, "都江堰景区")
    source = saved.routes[0].route_segments[0].sources[0]
    assert source.platform == "web_verified"
    assert source.chunk_id.startswith("web-")


def test_poi_detail_write_agent_output_corrects_platform_for_amap_chunk_id(isolated_project):
    """跟web_verified对称：amap合成chunk_id但platform字段没精确写"amap"时，同样应该
    被纠正——citation_check._merge_amap_evidence同样精确匹配platform=="amap"。"""
    tools = make_workspace_tools(
        isolated_project, "poi_detail_agent", day=1, poi_name="鸣沙山月牙泉", topic="青甘大环线"
    )
    write_agent_output = _get_tool(tools, "write_agent_output")

    routes = [
        {
            "route_name": "经典线",
            "route_segments": [
                {
                    "order": 1,
                    "point_name": "景区入口",
                    "sources": [
                        {
                            "platform": "高德地图",
                            "author": "高德驾车实测",
                            "doc_id": "青甘大环线-amap",
                            "chunk_id": "青甘大环线-amap_driving_distance",
                        }
                    ],
                }
            ],
        }
    ]
    result = write_agent_output.invoke({"routes": _with_route_segment_claims(routes)})

    assert "已自动修正" in result
    saved = store.read_poi_detail(isolated_project, 1, "鸣沙山月牙泉")
    assert saved.routes[0].route_segments[0].sources[0].platform == "amap"


def test_lifestyle_write_agent_output_leaves_correct_platform_untouched(isolated_project):
    """platform已经精确写"web_verified"/"amap"时不应该被误报"已自动修正"——只有
    真的发生纠正才提示，避免噪音掩盖真正的问题。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    item = dict(_BARE_ITEM)
    item["sources"] = [
        {"platform": "web_verified", "author": "", "doc_id": "web-1", "chunk_id": "web-翡翠湖-门票价格"}
    ]
    result = write_agent_output.invoke({"items": [_with_claims(item)]})

    assert "已自动修正" not in result


def test_lifestyle_write_agent_output_does_not_touch_unrelated_platform_values(isolated_project):
    """真实素材平台名（douyin/bilibili/B站/xiaohongshu等）即使写法不统一，也不是
    _normalize_synthetic_platform的职责——它只处理chunk_id/doc_id命中已知合成证据
    关键词的情况，不应该误伤这些正常的平台标注。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    item = dict(_BARE_ITEM)
    item["sources"] = [
        {"platform": "B站", "author": "某UP主", "doc_id": "青甘大环线-152", "chunk_id": "青甘大环线-152-c006"}
    ]
    result = write_agent_output.invoke({"items": [_with_claims(item)]})

    assert "已自动修正" not in result
    saved = store.read_lifestyle_output(isolated_project)
    assert saved.food.items[0].sources[0].platform == "B站"


def test_lifestyle_write_agent_output_corrects_hyphenated_web_id_platform(isolated_project):
    """docs/ISSUES.md第135条真实复现：第一版_normalize_synthetic_platform只测过完整
    关键词"web_verified"这一种命名变体，青甘大环线真实数据里`outfit.day8`这条item的
    chunk_id是"web-gansumuseum-2026"（连字符前缀，不含"web_verified"这个连续字符串），
    完全没被覆盖到，platform被错误标注为"xiaohongshu"，最终导致final_guide.md的参考
    来源表显示"⚠️原文未找到，此引用可能有误"这种误导性文案（第125条本该修复的问题
    因为这个bug重新复现了）。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    item = dict(_BARE_ITEM)
    item["sources"] = [
        {"platform": "xiaohongshu", "author": "兰州本地宝", "doc_id": "web", "chunk_id": "web-gansumuseum-2026"}
    ]
    result = write_agent_output.invoke({"items": [_with_claims(item)]})

    assert "已自动修正" in result
    saved = store.read_lifestyle_output(isolated_project)
    assert saved.food.items[0].sources[0].platform == "web_verified"
    # web_verified的合成id不应该被误加topic前缀（跟_normalize_sources_chunk_id
    # "只补真实素材chunk_id前缀"的既有约定一致，见test_..._does_not_touch_web_verified）。
    assert saved.food.items[0].sources[0].chunk_id == "web-gansumuseum-2026"


def test_lifestyle_write_agent_output_corrects_platform_even_when_chunk_id_already_corrupted_with_topic_prefix(
    isolated_project,
):
    """更棘手的真实场景：如果platform在更早一轮就没写对，_normalize_sources_chunk_id
    的"补topic前缀"逻辑会把这条合成id误判成普通素材chunk_id、往前拼一层topic前缀，
    变成"{topic}-web-gansumuseum-2026"——这时"web-"已经不在字符串开头，必须用子串
    匹配"-web-"才能识别出这是已经被错误处理过一次的历史脏数据（docs/ISSUES.md第135条，
    这是在青甘project真实历史数据里实际发生的情况，不是构造的极端case）。"""
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="青甘大环线")
    write_agent_output = _get_tool(tools, "write_agent_output")

    item = dict(_BARE_ITEM)
    item["sources"] = [
        {
            "platform": "xiaohongshu",
            "author": "兰州本地宝",
            "doc_id": "青甘大环线-web",
            "chunk_id": "青甘大环线-web-gansumuseum-2026",
        }
    ]
    result = write_agent_output.invoke({"items": [_with_claims(item)]})

    assert "已自动修正" in result
    saved = store.read_lifestyle_output(isolated_project)
    assert saved.food.items[0].sources[0].platform == "web_verified"


def test_workspace_write_tool_validation_error_does_not_echo_large_input(isolated_project):
    tools = make_workspace_tools(isolated_project, "lifestyle_agent", topic="成都")
    write_agent_output = _get_tool(tools, "write_agent_output")

    result = write_agent_output.invoke({"items": "x" * 10000})

    assert result.startswith("工具参数校验失败：")
    assert len(result) < 1000
    assert "x" * 100 not in result
