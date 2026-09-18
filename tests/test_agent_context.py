from src.workspace.agent_context import (
    lifestyle_day_complete,
    lifestyle_for_pitfall,
    route_day_view,
    user_requirement_view,
)


def test_route_day_view_removes_evidence_internals_and_other_days():
    route = {
        "days": [
            {"day": 1, "city": "成都", "pois": [{"name": "武侯祠"}], "claims": [{"claim_text": "大段证据"}]},
            {"day": 2, "city": "成都", "pois": [{"name": "杜甫草堂"}], "sources": [{"chunk_id": "large"}]},
        ]
    }

    projected = route_day_view(route, 2)

    assert projected["day"] == 2
    assert projected["pois"] == [{"name": "杜甫草堂", "brief": "", "needs_detail": False}]
    assert "claims" not in projected
    assert "sources" not in projected
    assert "武侯祠" not in str(projected)


def test_lifestyle_for_pitfall_keeps_targets_but_drops_claim_payloads():
    lifestyle = {
        "food": {
            "items": [
                {
                    "day": 1,
                    "recommendation": "吃担担面",
                    "claimed_entities": ["担担面"],
                    "claims": [{"claim_text": "很长的事实"}],
                    "sources": [{"chunk_id": "c1"}],
                }
            ]
        }
    }

    projected = lifestyle_for_pitfall(lifestyle)

    assert projected["food"]["items"][0]["recommendation"] == "吃担担面"
    assert "claims" not in projected["food"]["items"][0]
    assert "sources" not in projected["food"]["items"][0]


def test_lifestyle_day_complete_requires_all_four_fields():
    partial = {
        field: {"items": [{"day": 1}]} for field in ("food", "outfit", "lodging")
    }
    complete = {
        field: {"items": [{"day": 1}]} for field in ("food", "outfit", "lodging", "play")
    }

    assert lifestyle_day_complete(partial, 1) is False
    assert lifestyle_day_complete(complete, 1) is True


def test_user_requirement_view_removes_internal_keys_only():
    projected = user_requirement_view(
        {"destination": "成都", "days": 3, "notes": "不吃辣", "_optional_asked": True}
    )
    assert projected == {"destination": "成都", "days": 3, "notes": "不吃辣"}
