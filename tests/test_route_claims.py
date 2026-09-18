from __future__ import annotations

import json

import pytest

from src.materials import citation_check
from src.tools import amap_tools
from src.tools.workspace_tools import make_workspace_tools
from src.workspace import store


@pytest.fixture
def route_claim_project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    materials_dir = tmp_path / "materials"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", materials_dir)
    monkeypatch.setattr(amap_tools, "PROJECTS_DIR", projects_dir)
    project_id = "route-claim-project"
    topic = "成都"
    store.init_project(project_id)
    topic_dir = materials_dir / "index" / topic
    topic_dir.mkdir(parents=True)
    chunks = [
        {"chunk_id": "成都-doc1-c001", "text": "都江堰景区成人票80元，夏季开放8:00-18:00。"},
        {"chunk_id": "成都-doc2-c001", "text": "都江堰景区游览后，晚上可以到南桥看蓝眼泪夜景。"},
    ]
    (topic_dir / "chunks.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in chunks) + "\n",
        encoding="utf-8",
    )
    return project_id, topic, projects_dir


def _tool(tools, name: str):
    return next(item for item in tools if item.name == name)


def test_route_write_builds_claim_ids_snapshots_and_day_source_union(route_claim_project):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )

    result = write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "notes": "成人票80元；整体行程较满。",
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "subject": "都江堰景区",
                            "claim_text": "Day1安排都江堰景区",
                            "claim_type": "poi_selection",
                            "support_mode": "direct",
                            "claimed_entities": ["都江堰景区"],
                            "evidence_refs": [
                                {
                                    "platform": "bilibili",
                                    "doc_id": "doc1",
                                    "chunk_id": "doc1-c001",
                                }
                            ],
                        },
                        {
                            "target_path": "notes",
                            "subject": "都江堰景区",
                            "claim_text": "都江堰景区成人票80元",
                            "claim_type": "ticket_policy",
                            "support_mode": "direct",
                            "claimed_entities": ["都江堰景区"],
                            "evidence_refs": [
                                {
                                    "platform": "bilibili",
                                    "doc_id": "doc1",
                                    "chunk_id": "doc1-c001",
                                }
                            ],
                        },
                        {
                            "target_path": "notes",
                            "claim_text": "整体行程较满",
                            "claim_type": "schedule_assessment",
                            "support_mode": "derived",
                            "derived_from": ["都江堰景区成人票80元"],
                        },
                    ],
                }
            ]
        }
    )

    assert "路线规划已写入" in result
    day = store.read_route_plan(project_id).days[0]
    assert len(day.claims) == 3
    assert day.claims[0].claim_id.startswith("route-claim-")
    assert "成人票80元" in day.claims[1].evidence_snapshots[0].excerpt
    assert day.claims[2].derived_from == [day.claims[1].claim_id]
    assert [source.chunk_id for source in day.sources] == ["成都-doc1-c001"]


def test_route_write_rejects_bare_poi_selection_claim(route_claim_project):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )

    result = write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "都江堰景区",
                            "claim_type": "poi_selection",
                            "support_mode": "estimate",
                        }
                    ],
                }
            ]
        }
    )

    assert "poi_selection不能只写POI名称" in result
    assert store.read_route_plan(project_id) is None


def test_route_identity_claim_skips_substantive_judge(route_claim_project):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )
    result = write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "都江堰景区",
                            "claim_type": "poi_identity",
                            "support_mode": "direct",
                            "claimed_entities": ["都江堰景区"],
                            "evidence_refs": [
                                {
                                    "platform": "bilibili",
                                    "doc_id": "doc1",
                                    "chunk_id": "doc1-c001",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )
    assert "路线规划已写入" in result

    issues = citation_check.verify_route_citations(
        project_id,
        topic,
        allow_model_calls=False,
    )
    checks = store.read_evidence_checks(project_id)["route"]
    identity_checks = [
        item
        for item in checks
        if item.get("target_type") == "route_claim"
        and item.get("target_text") == "都江堰景区"
    ]

    assert issues == []
    assert next(item for item in identity_checks if item["scope"] == "entity_identity")["result"] == "pass"
    assert next(item for item in identity_checks if item["scope"] == "substantive_claim")["result"] == "not_checked"


def test_route_facts_patch_requires_claims_when_notes_have_live_claims(route_claim_project):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )
    write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "都江堰景区成人票80元",
                            "subject": "都江堰景区",
                            "support_mode": "direct",
                            "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc1", "chunk_id": "doc1-c001"}
                            ],
                        }
                    ],
                }
            ]
        }
    )
    patch_tool = _tool(
        make_workspace_tools(
            project_id,
            "route_agent",
            topic=topic,
            route_write_mode="facts_only",
        ),
        "write_route_facts_patch",
    )

    result = patch_tool.invoke({"day": 1, "notes": "改过的备注"})

    assert "修改notes时必须同步提交claims" in result


def test_route_facts_patch_accepts_json_wrapped_lists(route_claim_project):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )
    write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "notes": "原备注",
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "用户要求游览都江堰景区",
                            "support_mode": "user_constraint",
                        },
                        {
                            "target_path": "notes",
                            "claim_text": "原备注",
                            "support_mode": "user_constraint",
                        },
                    ],
                }
            ]
        }
    )
    patch_tool = _tool(
        make_workspace_tools(
            project_id,
            "route_agent",
            topic=topic,
            route_write_mode="facts_only",
        ),
        "write_route_facts_patch",
    )
    current_day = store.read_route_plan(project_id).days[0]
    current_claims = [claim.model_dump(mode="json") for claim in current_day.claims]
    current_sources = [source.model_dump(mode="json") for source in current_day.sources]

    result = patch_tool.invoke(
        {
            "day": 1,
            "notes": "新备注",
            "sources": json.dumps(current_sources, ensure_ascii=False),
            "claims": json.dumps(current_claims, ensure_ascii=False),
        }
    )

    assert "Day1事实层已更新" in result
    assert store.read_route_plan(project_id).days[0].notes == "新备注"


def test_route_claim_checker_does_not_borrow_another_claims_source(route_claim_project, monkeypatch):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )
    write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}, {"name": "南桥"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "都江堰景区成人票40元",
                            "subject": "都江堰景区",
                            "support_mode": "direct",
                            "claimed_entities": ["都江堰景区"],
                            "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc2", "chunk_id": "doc2-c001"}
                            ],
                        },
                        {
                            "target_path": "pois.1.name",
                            "claim_text": "南桥可以看蓝眼泪夜景",
                            "subject": "南桥",
                            "support_mode": "direct",
                            "claimed_entities": ["南桥"],
                            "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc2", "chunk_id": "doc2-c001"}
                            ],
                        },
                    ],
                }
            ]
        }
    )

    prompts = []

    class _Judge:
        def invoke(self, prompt):
            prompts.append(prompt)
            content = prompt.split("内容：", 1)[-1].split("原文片段：", 1)[0]
            source = prompt.rsplit("原文片段：", 1)[-1]
            return citation_check.CitationVerdict(
                supported=("成人票40元" not in content and "南桥" in source),
                reason="只按本事实声明的证据判断",
            )

    class _Model:
        def with_structured_output(self, _schema, method=None):
            return _Judge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())

    issues = citation_check.verify_route_citations(project_id, topic)
    checks = store.read_evidence_checks(project_id)["route"]
    claim_checks = [item for item in checks if item.get("target_type") == "route_claim"]

    assert any("成人票40元" in item.get("claim_text", "") for item in issues)
    bad_check = next(item for item in claim_checks if item["target_text"] == "都江堰景区成人票40元" and item["scope"] == "substantive_claim")
    good_check = next(item for item in claim_checks if item["target_text"] == "南桥可以看蓝眼泪夜景" and item["scope"] == "substantive_claim")
    assert bad_check["result"] == "fail"
    assert good_check["result"] == "pass"
    assert all("成人票80元" not in prompt for prompt in prompts)


def test_amap_tool_persists_evidence_id(route_claim_project, monkeypatch):
    project_id, topic, projects_dir = route_claim_project
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "test-key")
    monkeypatch.setattr(amap_tools.driving, "get_cached_or_driving", lambda *_args, **_kwargs: (65000, 5400))
    tool = _tool(
        amap_tools.make_amap_tools(topic, project_id=project_id, log_scope="route_agent"),
        "amap_driving_distance",
    )

    result = tool.invoke({"poi_a": "成都", "poi_b": "都江堰", "city": "成都"})

    assert "amap-evidence-" in result
    log_path = projects_dir / project_id / "agents" / "route_agent" / "amap_evidence_log.jsonl"
    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["distance_km"] == 65
    assert entry["duration_h"] == 1.5
    assert entry["evidence_id"] in result


def test_lifestyle_write_accepts_project_logged_amap_evidence_with_short_claim_name(
    route_claim_project, monkeypatch
):
    project_id, topic, projects_dir = route_claim_project
    monkeypatch.setattr(amap_tools, "AMAP_GEOCODE_API_KEY", "test-key")
    monkeypatch.setattr(
        amap_tools.driving,
        "get_cached_or_driving",
        lambda *_args, **_kwargs: (7903, 1552),
    )
    driving_tool = _tool(
        amap_tools.make_amap_tools(
            topic,
            project_id=project_id,
            log_scope="lifestyle_agent",
        ),
        "amap_driving_distance",
    )
    driving_tool.invoke(
        {"poi_a": "鸣沙山月牙泉", "poi_b": "沙洲夜市", "city": "敦煌"}
    )
    log_path = (
        projects_dir
        / project_id
        / "agents"
        / "lifestyle_agent"
        / "amap_evidence_log.jsonl"
    )
    evidence_id = json.loads(log_path.read_text(encoding="utf-8").strip())[
        "evidence_id"
    ]
    write_lifestyle = _tool(
        make_workspace_tools(project_id, "lifestyle_agent", topic=topic),
        "write_agent_output",
    )

    result = write_lifestyle.invoke(
        {
            "items": [
                {
                    "field": "play",
                    "day": 5,
                    "recommendation": "鸣沙山到沙洲夜市约8km/0.4h。",
                    "claims": [
                        {
                            "target_path": "recommendation",
                            "claim_text": "鸣沙山到沙洲夜市约8km/0.4h",
                            "claim_type": "play",
                            "support_mode": "amap",
                            "claimed_entities": ["鸣沙山月牙泉", "沙洲夜市"],
                            "evidence_refs": [
                                {
                                    "platform": "amap",
                                    "doc_id": "鸣沙山月牙泉→沙洲夜市",
                                    "chunk_id": evidence_id,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )

    assert "衣食住行产出已写入" in result


def test_route_write_rejects_missing_claim_coverage(route_claim_project):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )

    result = write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "notes": "成人票80元。",
                    "claims": [
                        {
                            "target_path": "notes",
                            "claim_text": "都江堰景区成人票80元",
                            "support_mode": "direct",
                            "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc1", "chunk_id": "doc1-c001"}
                            ],
                        }
                    ],
                }
            ]
        }
    )

    assert "pois.0.name" in result
    assert store.read_route_plan(project_id) is None


def test_route_write_rejects_imprecise_live_web_reference(route_claim_project):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )

    result = write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "Day1安排都江堰景区",
                            "support_mode": "direct",
                            "evidence_refs": [
                                {
                                    "platform": "web_verified",
                                    "doc_id": "web",
                                    "chunk_id": "web-dujiangyan",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )

    assert "必须先用append_material固化" in result
    assert store.read_route_plan(project_id) is None


def test_derived_claim_is_not_trusted_when_dependency_fails(route_claim_project, monkeypatch):
    project_id, topic, _ = route_claim_project
    write_route = _tool(
        make_workspace_tools(project_id, "route_agent", topic=topic),
        "write_route_plan",
    )
    result = write_route.invoke(
        {
            "days": [
                {
                    "day": 1,
                    "city": "都江堰",
                    "pois": [{"name": "都江堰景区"}],
                    "notes": "成人票40元，两人预算80元。",
                    "claims": [
                        {
                            "target_path": "pois.0.name",
                            "claim_text": "Day1安排都江堰景区",
                            "support_mode": "direct",
                            "claimed_entities": ["都江堰景区"],
                            "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc1", "chunk_id": "doc1-c001"}
                            ],
                        },
                        {
                            "target_path": "notes",
                            "claim_text": "都江堰景区成人票40元",
                            "support_mode": "direct",
                            "claimed_entities": ["都江堰景区"],
                            "evidence_refs": [
                                {"platform": "bilibili", "doc_id": "doc1", "chunk_id": "doc1-c001"}
                            ],
                        },
                        {
                            "target_path": "notes",
                            "claim_text": "两人门票预算80元",
                            "support_mode": "derived",
                            "derived_from": ["都江堰景区成人票40元"],
                        },
                    ],
                }
            ]
        }
    )
    assert "路线规划已写入" in result

    class _Judge:
        def invoke(self, prompt):
            content = prompt.split("内容：", 1)[-1].split("原文片段：", 1)[0]
            return citation_check.CitationVerdict(
                supported="成人票40元" not in content,
                reason="测试事实支持关系",
            )

    class _Model:
        def with_structured_output(self, _schema, method=None):
            return _Judge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    issues = citation_check.verify_route_citations(project_id, topic)
    checks = store.read_evidence_checks(project_id)["route"]
    derived = next(
        item
        for item in checks
        if item.get("target_text") == "两人门票预算80元"
        and item.get("scope") == "substantive_claim"
    )

    assert derived["result"] == "inconclusive"
    assert derived["method"] == "derived_dependency"
    assert any(item.get("claim_text") == "两人门票预算80元" for item in issues)
