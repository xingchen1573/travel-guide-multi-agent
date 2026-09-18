import src.tools.material_tools as material_tools


def test_material_tool_distinguishes_provider_error_from_empty(monkeypatch):
    monkeypatch.setattr(
        material_tools,
        "retrieve_context",
        lambda *args, **kwargs: {
            "results": [],
            "research": {
                "status": "error",
                "scope_errors": {"global_core": {"type": "APIConnectionError"}},
            },
        },
    )
    tool = material_tools.make_material_tools(
        "青甘大环线",
        "project-id",
        actor="pitfall_agent",
        log_scope="pitfall_agent",
    )[0]

    message = tool.invoke(
        {"query": "青海湖 旺季 排队", "field": "play", "day": 2}
    )

    assert "素材检索未完成" in message
    assert "不能把本次技术失败解释为没有相关素材" in message


def test_material_tool_exposes_degraded_ugc_with_usable_results(monkeypatch):
    monkeypatch.setattr(
        material_tools,
        "retrieve_context",
        lambda *args, **kwargs: {
            "results": [
                {
                    "scope": "project_supplement",
                    "chunk_id": "overlay-1",
                    "content_snippet": "当前Project补采的UGC玩法",
                }
            ],
            "research": {
                "status": "success",
                "scope_errors": {"global_core": {"type": "APIConnectionError"}},
            },
        },
    )
    tool = material_tools.make_material_tools(
        "青甘大环线",
        "project-id",
        actor="poi_detail_agent",
        log_scope="poi_detail_agent/day2_青海湖",
    )[0]

    message = tool.invoke({"query": "青海湖内部路线"})

    assert "部分素材scope检索失败" in message
    assert "overlay-1" in message
