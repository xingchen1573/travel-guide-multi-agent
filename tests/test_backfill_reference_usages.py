import json

from scripts.maintenance import backfill_reference_usages
from src.materials import citation_check
from src.workspace import store
from src.workspace.schema import PoiDetailFile, RouteDay, RouteOption, RoutePlan, RouteSegment


def test_backfill_reference_usages_preserves_body_and_rebuilds_reverse_index(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)

    project_id = "historical-project"
    topic = "测试目的地"
    source = {
        "platform": "bilibili",
        "doc_id": "doc-1",
        "chunk_id": "doc-1-c001",
    }
    store.init_project(project_id)
    store.write_requirements(project_id, topic, {"destination": topic})
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="测试城市", sources=[source])]),
    )
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "测试美食建议",
                "sources": [source],
            }
        ],
    )
    store.write_poi_detail(
        project_id,
        "poi_detail_agent",
        PoiDetailFile(
            poi_name="测试景点",
            day=1,
            routes=[
                RouteOption(
                    route_name="经典线",
                    recommended=True,
                    route_segments=[
                        RouteSegment(
                            order=1,
                            point_name="正门",
                            highlight="从正门进入",
                            sources=[source],
                        )
                    ],
                )
            ],
        ),
    )
    topic_dir = material_dir / "index" / topic
    topic_dir.mkdir(parents=True)
    (topic_dir / "chunks.jsonl").write_text(
        json.dumps(
            {"chunk_id": "doc-1-c001", "text": "测试景点的正门是经典线路的入口。"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    original_body = "# 最终攻略\n\n这是不应被回填改写的整合正文。"
    poi_appendix = "## 🌳 重点景点深度玩法（程序拼接自POI详情文件，非模型自行撰写）\n\nPOI原有附录。"
    old_references = "## 📚 参考来源（程序生成，非模型自行撰写）\n\n旧表格"
    store.write_final_guide(
        project_id,
        "integration_agent",
        f"{original_body}\n\n---\n\n{poi_appendix}\n\n---\n\n{old_references}\n",
        {"version": 3, "references": []},
    )

    result = backfill_reference_usages.backfill_project(project_id)

    assert result == {
        "project_id": project_id,
        "topic": topic,
        "changed": True,
        "reference_count": 1,
        "usage_count": 3,
        "runtime_reference_count": 0,
        "missing_text_reference_count": 0,
    }
    content = store.read_workspace(project_id, "final_guide.md")
    assert original_body in content
    assert poi_appendix in content
    assert "旧表格" not in content
    assert "Day1路线" in content
    assert "Day1美食" in content
    assert "Day1 测试景点·经典线途经点1" in content

    meta = store.read_workspace(project_id, "final_guide.meta.json")
    assert meta["references"][0]["cited_in"] == [
        "route:day:1",
        "lifestyle:food:day:1:item:0",
        "poi_detail:day:1:poi:测试景点:route:经典线:order:1",
    ]
    assert meta["references"][0]["excerpt"].startswith("测试景点的正门")

    assert backfill_reference_usages.backfill_project(project_id)["changed"] is False
