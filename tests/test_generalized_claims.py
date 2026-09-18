from __future__ import annotations

import json

import pytest

from src.materials import citation_check
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store


@pytest.fixture
def claim_project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    materials_dir = tmp_path / "materials"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", materials_dir)
    project_id = "generalized-claim-project"
    topic = "成都"
    store.init_project(project_id)
    topic_dir = materials_dir / "index" / topic
    topic_dir.mkdir(parents=True)
    chunks = [
        {"chunk_id": "成都-doc1-c001", "text": "灌县古城内有很多小吃，适合午餐时段就近解决。"},
        {"chunk_id": "成都-doc2-c001", "text": "南桥晚上可以看蓝眼泪夜景。"},
        {"chunk_id": "成都-doc3-c001", "text": "从景区南门进入后先到伏龙观，建议停留20分钟。"},
    ]
    (topic_dir / "chunks.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in chunks) + "\n",
        encoding="utf-8",
    )
    return project_id, topic


def _tool(project_id: str, actor: str, name: str, **kwargs):
    return next(
        item
        for item in make_workspace_tools(project_id, actor, **kwargs)
        if item.name == name
    )


def test_lifestyle_write_builds_claims_snapshots_and_source_union(claim_project):
    project_id, topic = claim_project
    write = _tool(project_id, "lifestyle_agent", "write_agent_output", topic=topic)

    result = write.invoke(
        {
            "items": [
                {
                    "field": "food",
                    "day": 2,
                    "recommendation": "午餐可在灌县古城解决，具体店家现场选择。",
                    "claims": [
                        {
                            "target_path": "recommendation",
                            "claim_text": "灌县古城适合午餐时段就近解决",
                            "claim_type": "food",
                            "support_mode": "direct",
                            "claimed_entities": ["灌县古城"],
                            "evidence_refs": [
                                {"platform": "xiaohongshu", "doc_id": "doc1", "chunk_id": "doc1-c001"}
                            ],
                        },
                        {
                            "target_path": "recommendation",
                            "claim_text": "具体店家可根据现场情况选择",
                            "claim_type": "food",
                            "support_mode": "estimate",
                        },
                    ],
                }
            ]
        }
    )

    assert "衣食住行产出已写入" in result
    item = store.read_lifestyle_output(project_id).food.items[0]
    assert all(claim.claim_id.startswith("lifestyle-claim-") for claim in item.claims)
    assert "灌县古城" in item.claims[0].evidence_snapshots[0].excerpt
    assert [source.chunk_id for source in item.sources] == ["成都-doc1-c001"]
    assert item.claimed_entities == ["灌县古城"]


def test_lifestyle_write_rejects_legacy_sources_without_claims(claim_project):
    project_id, topic = claim_project
    write = _tool(project_id, "lifestyle_agent", "write_agent_output", topic=topic)

    result = write.invoke(
        {
            "items": [
                {
                    "field": "food",
                    "day": 1,
                    "recommendation": "去灌县古城吃午饭",
                    "sources": [
                        {"platform": "xiaohongshu", "doc_id": "doc1", "chunk_id": "doc1-c001"}
                    ],
                }
            ]
        }
    )

    assert "必须提交claims" in result
    assert store.read_lifestyle_output(project_id) is None


def test_lifestyle_write_reports_all_item_validation_errors_at_once(claim_project):
    project_id, topic = claim_project
    write = _tool(project_id, "lifestyle_agent", "write_agent_output", topic=topic)

    result = write.invoke(
        {
            "items": [
                {
                    "field": "food",
                    "day": 1,
                    "recommendation": "根据路线选择附近餐厅。",
                    "claims": [
                        {
                            "target_path": "recommendation",
                            "claim_text": "附近用餐更顺路",
                            "claim_type": "food",
                            "support_mode": "derived",
                            "derived_from": ["路线安排"],
                        }
                    ],
                },
                {
                    "field": "lodging",
                    "day": 2,
                    "recommendation": "继续住原酒店更方便。",
                    "claims": [
                        {
                            "target_path": "recommendation",
                            "claim_text": "不换酒店更方便",
                            "claim_type": "lodging",
                            "support_mode": "derived",
                            "derived_from": ["用户不想搬行李"],
                        }
                    ],
                },
            ]
        }
    )

    assert "共发现2处参数问题" in result
    assert "food.day1" in result
    assert "lodging.day2" in result
    assert "完整claim_text或claim_id" in result
    assert store.read_lifestyle_output(project_id) is None


def test_pitfall_empty_warning_needs_no_claim_but_nonempty_warning_does(claim_project):
    project_id, topic = claim_project
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": "灌县古城午餐", "sources": []}],
    )
    write = _tool(project_id, "pitfall_agent", "write_agent_output", topic=topic)

    empty_result = write.invoke(
        {"items": [{"field": "food", "day": 1, "target_recommendation": "", "warning": ""}]}
    )
    assert "避雷产出已写入" in empty_result
    empty_item = store.read_pitfall_output(project_id).food.items[0]
    assert empty_item.claims == []
    assert empty_item.sources == []

    failed_result = write.invoke(
        {
            "items": [
                {
                    "field": "food",
                    "day": 1,
                    "target_recommendation": "灌县古城午餐",
                    "warning": "节假日部分店家排队较长",
                }
            ]
        }
    )
    assert "必须提交claims" in failed_result


def test_poi_write_requires_claim_for_each_nonempty_segment_field(claim_project):
    project_id, topic = claim_project
    write = _tool(
        project_id,
        "poi_detail_agent",
        "write_agent_output",
        topic=topic,
        day=1,
        poi_name="都江堰景区",
    )

    failed = write.invoke(
        {
            "routes": [
                {
                    "route_name": "经典线",
                    "route_segments": [
                        {
                            "order": 1,
                            "point_name": "伏龙观",
                            "highlight": "从南门进入后先到伏龙观",
                            "suggested_duration": "20分钟",
                            "claims": [
                                {
                                    "target_path": "point_name",
                                    "claim_text": "第一个途经点是伏龙观",
                                    "support_mode": "direct",
                                    "evidence_refs": [
                                        {"platform": "bilibili", "doc_id": "doc3", "chunk_id": "doc3-c001"}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )
    assert "highlight" in failed and "suggested_duration" in failed

    passed = write.invoke(
        {
            "routes": [
                {
                    "route_name": "经典线",
                    "route_segments": [
                        {
                            "order": 1,
                            "point_name": "伏龙观",
                            "highlight": "从南门进入后先到伏龙观",
                            "suggested_duration": "20分钟",
                            "claims": [
                                {
                                    "target_path": field,
                                    "claim_text": text,
                                    "support_mode": "direct",
                                    "claimed_entities": ["伏龙观"],
                                    "evidence_refs": [
                                        {"platform": "bilibili", "doc_id": "doc3", "chunk_id": "doc3-c001"}
                                    ],
                                }
                                for field, text in [
                                    ("point_name", "第一个途经点是伏龙观"),
                                    ("highlight", "从南门进入后先到伏龙观"),
                                    ("suggested_duration", "伏龙观建议停留20分钟"),
                                ]
                            ],
                        }
                    ],
                }
            ]
        }
    )
    assert "POI详情已写入" in passed
    segment = store.read_poi_detail(project_id, 1, "都江堰景区").routes[0].route_segments[0]
    assert all(claim.claim_id.startswith("poi-claim-") for claim in segment.claims)
    assert [source.chunk_id for source in segment.sources] == ["成都-doc3-c001"]


def test_poi_write_expands_pipe_separated_target_paths(claim_project):
    project_id, topic = claim_project
    write = _tool(
        project_id,
        "poi_detail_agent",
        "write_agent_output",
        topic=topic,
        day=1,
        poi_name="都江堰景区",
    )

    result = write.invoke(
        {
            "routes": [
                {
                    "route_name": "经典线",
                    "route_segments": [
                        {
                            "order": 1,
                            "point_name": "伏龙观",
                            "highlight": "从南门进入后先到伏龙观",
                            "claims": [
                                {
                                    "target_path": "point_name|highlight",
                                    "claim_text": "从南门进入后的第一个途经点是伏龙观",
                                    "support_mode": "direct",
                                    "claimed_entities": ["伏龙观"],
                                    "evidence_refs": [
                                        {
                                            "platform": "bilibili",
                                            "doc_id": "doc3",
                                            "chunk_id": "doc3-c001",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )

    assert "POI详情已写入" in result
    segment = store.read_poi_detail(project_id, 1, "都江堰景区").routes[0].route_segments[0]
    assert {claim.target_path for claim in segment.claims} == {"point_name", "highlight"}
    assert len({claim.claim_id for claim in segment.claims}) == 2


def test_poi_write_reports_all_segment_validation_errors_at_once(claim_project):
    project_id, topic = claim_project
    write = _tool(
        project_id,
        "poi_detail_agent",
        "write_agent_output",
        topic=topic,
        day=1,
        poi_name="都江堰景区",
    )

    result = write.invoke(
        {
            "routes": [
                {
                    "route_name": "经典线",
                    "route_segments": [
                        {
                            "order": 1,
                            "point_name": "伏龙观",
                            "highlight": "南门后的第一个途经点",
                            "claims": [
                                {
                                    "target_path": "point_name",
                                    "claim_text": "第一个途经点是伏龙观",
                                    "support_mode": "direct",
                                    "evidence_refs": [
                                        {"platform": "bilibili", "doc_id": "doc3", "chunk_id": "doc3-c001"}
                                    ],
                                }
                            ],
                        },
                        {
                            "order": 2,
                            "point_name": "南桥",
                            "suggested_duration": "20分钟",
                            "claims": [
                                {
                                    "target_path": "point_name",
                                    "claim_text": "途经点是南桥",
                                    "support_mode": "direct",
                                    "evidence_refs": [
                                        {"platform": "bilibili", "doc_id": "doc2", "chunk_id": "doc2-c001"}
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ]
        }
    )

    assert "POI路线共发现2处参数问题" in result
    assert "segment1(伏龙观)" in result and "highlight" in result
    assert "segment2(南桥)" in result and "suggested_duration" in result
    assert store.read_poi_detail(project_id, 1, "都江堰景区") is None


def test_lifestyle_claim_checker_does_not_borrow_sibling_claim_source(claim_project, monkeypatch):
    project_id, topic = claim_project
    write = _tool(project_id, "lifestyle_agent", "write_agent_output", topic=topic)
    result = write.invoke(
        {
            "items": [
                {
                    "field": "play",
                    "day": 2,
                    "recommendation": "灌县古城适合吃午饭，南桥晚上可看蓝眼泪。",
                    "claims": [
                            {
                                "target_path": "recommendation",
                                "claim_text": "灌县古城适合吃午饭",
                                "support_mode": "direct",
                                "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc2", "chunk_id": "doc2-c001"}
                            ],
                        },
                        {
                                "target_path": "recommendation",
                                "claim_text": "南桥晚上可看蓝眼泪",
                                "support_mode": "direct",
                                "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc2", "chunk_id": "doc2-c001"}
                            ],
                        },
                    ],
                }
            ]
        }
    )
    assert "衣食住行产出已写入" in result

    class _Judge:
        def invoke(self, prompt):
            content = prompt.split("内容：", 1)[-1].split("原文片段：", 1)[0]
            source = prompt.rsplit("原文片段：", 1)[-1]
            return citation_check.CitationVerdict(
                supported=("灌县古城" not in content and "南桥" in source),
                reason="只根据当前claim的来源判断",
            )

    class _Model:
        def with_structured_output(self, _schema, method=None):
            return _Judge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    issues = citation_check.verify_lifestyle_citations(project_id, topic)
    checks = store.read_evidence_checks(project_id)["lifestyle"]
    substantive = [check for check in checks if check.get("scope") == "substantive_claim"]

    assert any("灌县古城" in issue.get("claim_text", "") for issue in issues)
    assert next(check for check in substantive if "灌县古城" in check["claim_text"])["result"] == "fail"
    assert next(check for check in substantive if "南桥" in check["claim_text"])["result"] == "pass"


def test_derived_claim_dependency_issue_keeps_parent_item_coordinates(monkeypatch):
    claim_item = {
        "target_key": "lifestyle:food:day:2:item:0:claim:derived-claim",
        "extra": {
            "field": "food",
            "day": 2,
            "recommendation": "继续住原酒店附近并就近吃饭。",
            "claim_id": "derived-claim",
            "claim_text": "就近吃饭更顺路",
            "claim_type": "food",
            "support_mode": "derived",
            "target_path": "recommendation",
            "derived_from": ["missing-source-claim"],
        },
    }
    checks = [
        {
            "target_key": claim_item["target_key"],
            "target_text": "就近吃饭更顺路",
            "scope": "substantive_claim",
            "method": "substantive_support",
            "result": "pass",
            "claim_id": "derived-claim",
            "claim_text": "就近吃饭更顺路",
            "claim_type": "food",
            "target_path": "recommendation",
            "cited_chunk_ids": [],
        }
    ]
    monkeypatch.setattr(citation_check, "_verify_items", lambda *args, **kwargs: [])

    issues = citation_check._verify_evidence_claim_items(
        [claim_item], {}, checks, allow_model_calls=False
    )

    assert issues == [
        {
            "label": "就近吃饭更顺路",
            "chunk_ids": [],
            "missing_entities": [],
            "reason": "推导所依赖的事实未全部核验通过，不能把本条推导视为已证实：missing-source-claim",
            "claim_id": "derived-claim",
            "claim_text": "就近吃饭更顺路",
            "claim_type": "food",
            "support_mode": "derived",
            "target_path": "recommendation",
            "field": "food",
            "day": 2,
            "recommendation": "继续住原酒店附近并就近吃饭。",
            "derived_from": ["missing-source-claim"],
        }
    ]
