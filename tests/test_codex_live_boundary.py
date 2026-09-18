import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.codex_skill import preflight
from src.codex_skill.bridge import append_trace
from src.materials import project_evidence
from src.workspace import store
from src.workspace.schema import RouteDay, RoutePlan, RoutePoi


def _seed_fixed_route_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "兰州", {"destination": "兰州", "days": 1})
    store.write_route_plan(
        "source",
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="兰州", pois=[RoutePoi(name="省博")])]),
    )
    monkeypatch.setattr(
        preflight,
        "_probe_web",
        lambda: {"status": "passed", "attempts": 1},
    )
    monkeypatch.setattr(
        preflight,
        "_probe_amap",
        lambda: {"status": "passed", "attempts": 1},
    )


def test_fixed_route_preflight_keeps_embedding_optional_by_default(tmp_path, monkeypatch):
    _seed_fixed_route_source(tmp_path, monkeypatch)

    def unexpected_probe():
        raise AssertionError("default preflight must not call the Embedding provider")

    monkeypatch.setattr(preflight, "_probe_embedding", unexpected_probe)

    result = preflight.run_fixed_route_preflight(
        "source",
        "target",
        live=True,
        persist=False,
    )

    assert result["ready"] is True
    assert "embedding" not in result["checks"]


def test_fixed_route_preflight_requires_embedding_only_when_explicit(
    tmp_path, monkeypatch
):
    _seed_fixed_route_source(tmp_path, monkeypatch)
    monkeypatch.setattr(
        preflight,
        "_probe_embedding",
        lambda: {
            "status": "failed",
            "reason": "embedding_probe_error",
            "attempts": 1,
            "duration_ms": 3,
            "error_type": "APIConnectionError",
            "error_summary": "Embedding probe failed (APIConnectionError)",
        },
    )

    result = preflight.run_fixed_route_preflight(
        "source",
        "target",
        live=True,
        require_embedding=True,
        persist=False,
    )

    assert result["ready"] is False
    assert result["checks"]["embedding"]["status"] == "failed"


def test_fixed_route_preflight_accepts_required_embedding_when_probe_passes(
    tmp_path, monkeypatch
):
    _seed_fixed_route_source(tmp_path, monkeypatch)
    monkeypatch.setattr(
        preflight,
        "_probe_embedding",
        lambda: {"status": "passed", "attempts": 1, "duration_ms": 3, "dimensions": 3},
    )

    result = preflight.run_fixed_route_preflight(
        "source",
        "target",
        live=True,
        require_embedding=True,
        persist=False,
    )

    assert result["ready"] is True
    assert result["checks"]["embedding"]["dimensions"] == 3
    assert result["embedding_required"] is True
    assert "embedding" in result["required_checks"]


def test_offline_preflight_does_not_call_required_embedding_probe(tmp_path, monkeypatch):
    _seed_fixed_route_source(tmp_path, monkeypatch)

    def unexpected_probe():
        raise AssertionError("offline preflight must not call the Embedding provider")

    monkeypatch.setattr(preflight, "_probe_embedding", unexpected_probe)

    result = preflight.run_fixed_route_preflight(
        "source",
        "target",
        require_embedding=True,
        persist=False,
    )

    assert result["ready"] is True
    assert result["checks"]["embedding"] == {
        "status": "not_run",
        "reason": "offline_preflight",
    }


def test_embedding_probe_accepts_finite_numeric_vector_without_leaking_payload(
    monkeypatch,
):
    class FakeEmbeddings:
        def embed_query(self, text):
            assert text
            return [0.25, -0.5, 1]

    monkeypatch.setattr(preflight, "get_embeddings", lambda: FakeEmbeddings())

    result = preflight._probe_embedding()

    assert result["status"] == "passed"
    assert result["dimensions"] == 3
    assert result["attempts"] == 1
    assert result["duration_ms"] >= 0
    assert set(result) == {"status", "dimensions", "attempts", "duration_ms"}


@pytest.mark.parametrize(
    ("vector", "reason"),
    [
        ([], "embedding_probe_empty_vector"),
        ([0.25, "not-a-number"], "embedding_probe_non_numeric_vector"),
        ([0.25, float("nan")], "embedding_probe_non_finite_vector"),
        ([0.25, float("inf")], "embedding_probe_non_finite_vector"),
    ],
)
def test_embedding_probe_rejects_invalid_vectors(monkeypatch, vector, reason):
    class FakeEmbeddings:
        def embed_query(self, text):
            return vector

    monkeypatch.setattr(preflight, "get_embeddings", lambda: FakeEmbeddings())

    result = preflight._probe_embedding()

    assert result["status"] == "failed"
    assert result["reason"] == reason
    assert "vector" not in result


def test_embedding_probe_redacts_provider_exception_details(monkeypatch):
    secret = "sk-do-not-log-this"

    class FakeEmbeddings:
        def embed_query(self, text):
            raise RuntimeError(f"request failed with api_key={secret}")

    monkeypatch.setattr(preflight, "get_embeddings", lambda: FakeEmbeddings())

    result = preflight._probe_embedding()

    assert result["status"] == "failed"
    assert result["error_type"] == "RuntimeError"
    assert result["reason"] == "embedding_probe_error"
    assert secret not in json.dumps(result)


def test_embedding_probe_reports_missing_configuration_without_exposing_values(
    monkeypatch,
):
    def missing_config():
        raise RuntimeError(
            "缺少embedding配置，请检查 .env 中的 "
            "EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL"
        )

    monkeypatch.setattr(preflight, "get_embeddings", missing_config)

    result = preflight._probe_embedding()

    assert result["status"] == "failed"
    assert result["reason"] == "embedding_config_missing"
    assert result["attempts"] == 0
    assert result["error_type"] == "RuntimeError"
    assert "API_KEY" not in json.dumps(result)


def test_fixed_route_preflight_cli_forwards_require_embedding(monkeypatch, capsys):
    script_path = (
        Path(__file__).parents[1]
        / ".agents"
        / "skills"
        / "travel-guide-codex-orchestrator"
        / "scripts"
        / "travel_guide_bridge.py"
    )
    spec = importlib.util.spec_from_file_location(
        "travel_guide_bridge_embedding_preflight_test", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    observed = {}

    def fake_preflight(source_project_id, target_project_id, **kwargs):
        observed.update(
            source_project_id=source_project_id,
            target_project_id=target_project_id,
            **kwargs,
        )
        return {"ready": True}

    monkeypatch.setattr(preflight, "run_fixed_route_preflight", fake_preflight)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script_path),
            "preflight-fixed-route",
            "source",
            "target",
            "--live",
            "--require-embedding",
        ],
    )

    assert module.main() == 0

    assert json.loads(capsys.readouterr().out) == {"ready": True}
    assert observed["source_project_id"] == "source"
    assert observed["target_project_id"] == "target"
    assert observed["live"] is True
    assert observed["require_embedding"] is True


def test_trace_accepts_confirmed_sandbox_boundary_recovery_as_retry(
    tmp_path, monkeypatch
):
    _seed_fixed_route_source(tmp_path, monkeypatch)

    result = append_trace(
        "source",
        {
            "event": "retry",
            "status": "sandbox_network_boundary_recovered",
            "summary": "Embedding probe recovered through the authorized Host boundary",
            "original_outcome": {
                "execution_mode": "restricted_sandbox",
                "status": "failed",
                "error_type": "APIConnectionError",
            },
            "external_outcome": {
                "execution_mode": "host_approved_external",
                "status": "passed",
            },
        },
    )

    event = result["event"]
    assert event["event"] == "retry"
    assert event["status"] == "sandbox_network_boundary_recovered"
    assert event["external_outcome"]["status"] == "passed"


def test_skill_documents_require_host_boundary_and_retry_trace_contract():
    root = Path(__file__).parents[1]
    orchestrator_skill = (
        root / ".agents" / "skills" / "travel-guide-codex-orchestrator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    supervisor_protocol = (
        root
        / ".agents"
        / "skills"
        / "long-running-task-supervisor"
        / "references"
        / "failure-and-recovery-protocol.md"
    ).read_text(encoding="utf-8")

    for text in (orchestrator_skill, supervisor_protocol):
        assert "Host" in text
        assert "sandbox_network_boundary_recovered" in text
        assert "Provider fallback" in text
        assert "elevat" in text
    assert "network_execution_mode=host_approved_external" in orchestrator_skill
