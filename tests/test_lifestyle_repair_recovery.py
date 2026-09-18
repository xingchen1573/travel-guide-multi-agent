from __future__ import annotations

import json

import pytest

from src import orchestrator
from src.orchestrator import (
    _append_lifestyle_repair_run,
    _lifestyle_artifact_sha,
    citation_check_node,
)
from src.workspace import store


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "lifestyle-repair-recovery"
    store.init_project(project_id)
    return project_id


def test_lifestyle_repair_resume_rechecks_only_persisted_claim_scope(
    isolated_project, monkeypatch
):
    conflict = store.report_conflict(
        isolated_project,
        actor="citation_checker",
        target="lifestyle_agent.food",
        reason="Day4 claim引用不支持",
        issues=[{"field": "food", "day": 4, "claim_id": "claim-day4-food"}],
    )
    lifestyle = {
        "food": [
            {
                "day": 4,
                "recommendation": "敦煌晚餐建议",
                "claims": [{"claim_id": "claim-day4-food", "text": "可选当地菜"}],
            }
        ]
    }
    output_path = store.PROJECTS_DIR / isolated_project / "workspace" / "outputs" / "lifestyle.json"
    output_path.write_text(json.dumps(lifestyle, ensure_ascii=False), encoding="utf-8")
    _append_lifestyle_repair_run(
        isolated_project,
        repair_id="repair-day4",
        status="artifact_written",
        artifact_sha256=_lifestyle_artifact_sha(lifestyle),
        claim_ids={"claim-day4-food"},
        item_keys={("food", 4)},
        repair_round=1,
        conflict_id=conflict.id,
    )
    captured = []

    def fake_verify(project_id, topic, **kwargs):
        captured.append(kwargs)
        return []

    monkeypatch.setattr(
        orchestrator.citation_check,
        "verify_lifestyle_citations",
        fake_verify,
    )

    citation_check_node(
        {"project_id": isolated_project, "topic": "青甘", "mode": "plan"}
    )

    assert captured == [
        {
            "fields": None,
            "item_keys": None,
            "reuse_existing_completed_claims": True,
            "claim_ids": {"claim-day4-food"},
        }
    ]
    runs = store.read_validation_runs(isolated_project)
    assert runs[-1]["repair_id"] == "repair-day4"
    assert runs[-1]["status"] == "completed"
    assert store.get_pending_conflict(isolated_project) is None


def test_lifestyle_repair_resume_ignores_stale_artifact_scope(
    isolated_project, monkeypatch
):
    conflict = store.report_conflict(
        isolated_project,
        actor="citation_checker",
        target="lifestyle_agent.food",
        reason="Day4 claim引用不支持",
        issues=[{"field": "food", "day": 4, "claim_id": "claim-day4-food"}],
    )
    _append_lifestyle_repair_run(
        isolated_project,
        repair_id="stale-repair-day4",
        status="artifact_written",
        artifact_sha256="stale-sha",
        claim_ids={"claim-day4-food"},
        item_keys={("food", 4)},
        repair_round=1,
        conflict_id=conflict.id,
    )
    captured = []

    def fake_verify(project_id, topic, **kwargs):
        captured.append(kwargs)
        return []

    monkeypatch.setattr(
        orchestrator.citation_check,
        "verify_lifestyle_citations",
        fake_verify,
    )

    citation_check_node(
        {"project_id": isolated_project, "topic": "青甘", "mode": "plan"}
    )

    assert captured == [{"fields": None, "item_keys": None}]
    assert store.get_pending_conflict(isolated_project) is not None
