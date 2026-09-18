from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routers import experiments

client = TestClient(app)


def test_list_and_detail_experiment_api(monkeypatch):
    monkeypatch.setattr(experiments.experiment_index, "list_experiments", lambda **kwargs: [{"project_id": "exp-one"}])
    monkeypatch.setattr(experiments.experiment_index, "get_experiment", lambda project_id: {"metrics": {"identity": {"project_id": project_id}}})

    assert client.get("/api/experiments").json() == [{"project_id": "exp-one"}]
    response = client.get("/api/experiments/exp-one")
    assert response.status_code == 200
    assert response.json()["metrics"]["identity"]["project_id"] == "exp-one"


def test_reindex_api(monkeypatch):
    monkeypatch.setattr(experiments.experiment_index, "rebuild_index", lambda: {"indexed": 3, "failed": 0})
    response = client.post("/api/experiments/reindex")
    assert response.status_code == 200
    assert response.json() == {"indexed": 3, "failed": 0}


def test_comparison_pair_routes_are_not_captured_as_project_ids(monkeypatch):
    monkeypatch.setattr(
        experiments.experiment_index,
        "list_comparison_pairs",
        lambda **kwargs: [{"pair_id": "pair-one"}],
    )
    monkeypatch.setattr(
        experiments.experiment_index,
        "get_comparison_pair",
        lambda pair_id: {"pair_id": pair_id},
    )

    assert client.get("/api/experiments/pairs").json() == [{"pair_id": "pair-one"}]
    assert client.get("/api/experiments/pairs/pair-one").json() == {
        "pair_id": "pair-one"
    }


def test_missing_experiment_returns_404(monkeypatch):
    monkeypatch.setattr(experiments.experiment_index, "get_experiment", lambda project_id: None)
    assert client.get("/api/experiments/missing").status_code == 404
