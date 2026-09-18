from src.orchestrator import _pitfall_research_context_view


def test_pitfall_research_context_keeps_bindings_and_deduplicates_chunk_bodies():
    shared = {
        "scope": "global_core",
        "title": "共享素材",
        "chunk_id": "chunk-shared",
        "content_snippet": "长" * 300,
        "evidence_ref": {"platform": "bilibili", "chunk_id": "chunk-shared"},
    }
    rows = [
        {"field": "food", "day": 1, "results": [shared]},
        {
            "field": "play",
            "day": 1,
            "results": [
                shared,
                {
                    "scope": "project_overlay",
                    "title": "独立素材",
                    "chunk_id": "chunk-unique",
                    "content_snippet": "短摘要",
                    "evidence_ref": {
                        "platform": "project_evidence",
                        "chunk_id": "chunk-unique",
                    },
                },
            ],
        },
    ]

    view = _pitfall_research_context_view(rows)

    assert view["coverage"] == [
        {"field": "food", "day": 1, "chunk_ids": ["chunk-shared"]},
        {
            "field": "play",
            "day": 1,
            "chunk_ids": ["chunk-shared", "chunk-unique"],
        },
    ]
    assert [item["chunk_id"] for item in view["evidence_catalog"]] == [
        "chunk-shared",
        "chunk-unique",
    ]
    assert len(view["evidence_catalog"][0]["content_snippet"]) == 80
