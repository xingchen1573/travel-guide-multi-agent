from __future__ import annotations

from scripts.e2e.run_route_feasibility_smoke import build_smoke_plan, main
from src.materials import route_feasibility_check as feasibility
from src.workspace import store
from src.workspace.schema import RouteDay, RouteFeasibilityReport, RoutePlan, RoutePoi


def _seed(tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "route-feasibility-smoke"
    store.init_project(project_id)
    store.write_requirements(
        project_id,
        "敦煌",
        {"destination": "敦煌", "days": 1, "travel_start_date": "2026-10-02"},
    )
    store.write_route_plan(
        project_id,
        "route_agent",
        RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="敦煌",
                    pois=[
                        RoutePoi(name="莫高窟", needs_detail=True),
                        RoutePoi(name="鸣沙山月牙泉"),
                    ],
                )
            ]
        ),
    )
    return project_id


def test_build_smoke_plan_caps_targets_and_estimates_calls(tmp_path, monkeypatch):
    project_id = _seed(tmp_path, monkeypatch)

    plan = build_smoke_plan(project_id, max_targets=1)

    assert plan["selected_target_count"] == 1
    assert plan["estimated_web_searches"] == 1
    assert plan["targets"][0]["target"] == "莫高窟"
    assert len(plan["check_ids"]) == 1


def test_smoke_preview_makes_no_real_calls(tmp_path, monkeypatch, capsys):
    project_id = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        feasibility,
        "verify_route_feasibility",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview must not execute")),
    )

    assert main([project_id, "--max-targets", "1"]) == 0
    assert '"mode": "preview"' in capsys.readouterr().out


def test_smoke_execute_does_not_persist_production_report(tmp_path, monkeypatch):
    project_id = _seed(tmp_path, monkeypatch)
    captured = {}

    def fake_verify(*args, **kwargs):
        captured.update(kwargs)
        return RouteFeasibilityReport(overall_status="passed")

    monkeypatch.setattr(feasibility, "verify_route_feasibility", fake_verify)

    assert main([project_id, "--max-targets", "1", "--execute"]) == 0
    assert captured["persist"] is False
    assert len(captured["check_ids"]) == 1
    assert store.read_route_feasibility(project_id) is None
