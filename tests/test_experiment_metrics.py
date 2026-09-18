from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.experiments import index as experiment_index
from src.experiments import metrics as experiment_metrics
from src.experiments.metrics import extract_project_metrics
from src.review.models import ReviewFinding


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _seed_project(projects_dir: Path, project_id: str) -> None:
    root = projects_dir / project_id
    workspace = root / "workspace"
    claim = {"claim_id": "route-claim-1", "claim_text": "测试事实", "claim_type": "route_fact", "support_mode": "direct", "evidence_refs": []}
    _write_json(workspace / "requirements.json", {"topic": "成都", "requirements": {"destination": "成都", "days": 1}})
    _write_json(workspace / "route_plan.json", {"version": 1, "days": [{"day": 1, "city": "成都", "area": "中心", "pois": [], "claims": [claim]}]})
    _write_json(
        workspace / "route_entities.json",
        {"entities": [{"formal_name": "武侯祠", "status": "resolved"}]},
    )
    _write_json(
        workspace / "route_execution.json",
        {"route_legs": [{"transport_mode": "public_transit"}]},
    )
    _write_json(workspace / "route_feasibility.json", {"route_plan_structure_hash": "route-sha", "overall_status": "warning", "checks": [{"status": "passed", "coverage_status": "covered"}, {"status": "warning", "coverage_status": "missing"}]})
    _write_json(workspace / "progress.json", {"state": "完成"})
    _write_json(workspace / "evidence_checks.json", {"route": [{"claim_id": "route-claim-1", "result": "fail"}]})
    for owner in ("lifestyle", "pitfall"):
        _write_json(workspace / "outputs" / f"{owner}.json", {field: {"items": []} for field in ("food", "outfit", "lodging", "play")})
    _write_json(workspace / "outputs" / "poi_details" / "manifest.json", {"generated": []})
    _write_json(workspace / "final_guide.blocks.json", {"blocks": [{"upstream_claim_ids": ["route-claim-1"]}]})
    _write_json(workspace / "final_guide.meta.json", {"contract_validation": {"status": "passed", "issues": []}})
    (workspace / "final_guide.md").write_text("# 成都", encoding="utf-8")
    trace = root / "agents" / "codex_skill" / "trace.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(
        '\n'.join([
            json.dumps({"event": "run_start", "timestamp": "2026-09-02T00:00:00Z"}),
            json.dumps({"event": "retry", "status": "content_repair", "timestamp": "2026-09-02T00:00:10Z"}),
            json.dumps({"event": "retry", "status": "provider_network_failure", "timestamp": "2026-09-02T00:00:20Z"}),
            json.dumps({"event": "retry", "status": "structured_parse_failure", "timestamp": "2026-09-02T00:00:30Z"}),
            json.dumps({"event": "run_completed", "human_interventions": 0, "timestamp": "2026-09-02T00:01:00Z"}),
        ]) + '\n',
        encoding="utf-8",
    )
    web_log = root / "agents" / "route_feasibility_check" / "web_search_log.jsonl"
    web_log.parent.mkdir(parents=True, exist_ok=True)
    web_log.write_text(
        json.dumps({"query": "成都", "content": "第一段\u2028第二段"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"query": "武侯祠", "content": "正常结果"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (root / "audit.md").write_text("模型调用 999，费用 123 元", encoding="utf-8")


def _entry(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "scenario_id": "chengdu-one-day",
        "experiment_version": "codex-test-v1",
        "harness": "codex_skill",
        "model": "test-model",
        "provider": "test-provider",
        "comparison_grade": "B",
        "completion_mode": "completed",
        "inherited_downstream_outputs": False,
        "evidence_docs": [],
        "relations": [],
    }


def test_extractor_distinguishes_confirmed_zero_from_unknown_and_does_not_parse_markdown(tmp_path):
    _seed_project(tmp_path, "exp-one")
    metrics = extract_project_metrics("exp-one", _entry("exp-one"), projects_dir=tmp_path)

    assert metrics["outputs"]["lifestyle_item_count"] == 0
    assert metrics["quality_contract"]["withheld_claim_count"] == 1
    assert metrics["quality_contract"]["withheld_claim_leak_count"] == 1
    assert metrics["execution"]["human_intervention_count"]["value"] == 0
    assert metrics["execution"]["human_intervention_count"]["value_kind"] == "derived"
    assert metrics["execution"]["business_retry_count"]["value"] == 1
    assert metrics["execution"]["locally_repaired_claim_count"]["value"] == 0
    assert metrics["execution"]["transport_retry_count"]["value"] == 1
    assert metrics["execution"]["recovery_count"]["value"] == 3
    assert metrics["tool_usage"]["web_persisted_query_count"]["value"] == 2
    assert metrics["tool_usage"]["model_call_count"]["value"] is None
    assert metrics["tool_usage"]["model_call_count"]["value_kind"] == "unknown"
    for key in (
        "model_call_count",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "monetary_cost",
    ):
        assert (
            metrics["tool_usage"][key]["missing_reason"]
            == "codex_host_usage_not_exposed"
        )
    assert metrics["route_gate"]["coverage_counts"]["covered"] == 1
    assert metrics["route_gate"]["coverage_counts"]["missing"] == 1
    assert metrics["route_gate"]["entity_status_counts"] == {"resolved": 1}
    assert metrics["route_gate"]["transport_mode_counts"] == {"public_transit": 1}
    assert metrics["research"]["call_count"] == 0
    assert "workspace/route_entities.json" in metrics["evidence_paths"]
    assert "workspace/route_execution.json" in metrics["evidence_paths"]
    assert "999" not in json.dumps(metrics, ensure_ascii=False)


def test_metrics_apply_latest_experiment_validity_correction_to_effective_identity(
    tmp_path,
):
    project_id = "exp-contaminated-continuation"
    _seed_project(tmp_path, project_id)
    workspace = tmp_path / project_id / "workspace"
    _write_json(
        workspace / "experiment.json",
        {
            "schema_version": "experiment-manifest-v1",
            "project_id": project_id,
            "scenario_id": "chengdu-one-day",
            "experiment_version": "codex-test-v1",
            "harness": "codex_skill",
            "comparison_grade": "A",
            "completion_mode": "completed_with_documented_degradation",
        },
    )
    corrections = [
        {
            "correction_type": "experiment_validity",
            "declared_comparison_grade": "A",
            "effective_comparison_grade": "B",
            "formal_run_status": "superseded-old-value",
            "continuation_status": "completed",
            "continuation_mode": "earlier_continuation",
        },
        {
            "correction_type": "experiment_validity",
            "declared_comparison_grade": "C",
            "effective_comparison_grade": "A",
            "formal_run_status": "completed",
            "continuation_status": "completed_with_documented_degradation",
            "continuation_mode": "post_fix_contaminated_continuation",
            "reason_codes": ["production_code_changed"],
            "capture_mode": "audited_backfill",
        },
    ]
    (workspace / "post_run_corrections.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in corrections) + "\n",
        encoding="utf-8",
    )
    trace = tmp_path / project_id / "agents" / "codex_skill" / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in (
                {
                    "event": "run_start",
                    "status": "started",
                    "timestamp": "2026-09-07T00:00:00Z",
                },
                {
                    "event": "run_completed",
                    "status": "validation_failed_preserved",
                    "timestamp": "2026-09-07T00:10:00Z",
                },
                {
                    "event": "run_start",
                    "status": "diagnostic",
                    "timestamp": "2026-09-07T02:00:00Z",
                },
                {
                    "event": "run_completed",
                    "status": "completed_with_documented_degradation",
                    "continuation_mode": "post_fix_contaminated_continuation",
                    "timestamp": "2026-09-07T02:20:00Z",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    metrics = extract_project_metrics(
        project_id,
        {
            **_entry(project_id),
            "comparison_grade": "A",
            "completion_mode": "completed_with_documented_degradation",
        },
        projects_dir=tmp_path,
    )

    identity = metrics["identity"]
    assert identity["declared_comparison_grade"] == "A"
    assert identity["comparison_grade"] == "C"
    assert identity["completion_mode"] == "validation_failed_preserved"
    assert identity["formal_run_status"] == "validation_failed_preserved"
    assert identity["continuation_status"] == "completed_with_documented_degradation"
    assert identity["continuation_completion_mode"] == "completed_with_documented_degradation"
    assert identity["continuation_mode"] == "post_fix_contaminated_continuation"
    assert identity["artifact_status"] == "deliverable"
    assert identity["experiment_validity"]["correction_applied"] is True
    assert identity["experiment_validity"]["reason_codes"] == ["production_code_changed"]


def test_metrics_keep_legacy_identity_when_no_validity_correction_exists(tmp_path):
    project_id = "exp-legacy-identity"
    _seed_project(tmp_path, project_id)

    metrics = extract_project_metrics(project_id, _entry(project_id), projects_dir=tmp_path)

    identity = metrics["identity"]
    assert identity["declared_comparison_grade"] == "B"
    assert identity["comparison_grade"] == "B"
    assert identity["completion_mode"] == "completed"
    assert identity["formal_run_status"] == "completed"
    assert identity["continuation_status"] is None
    assert identity["artifact_status"] == "deliverable"


def test_metrics_split_amap_evidence_and_route_map_measurements(tmp_path):
    project_id = "exp-amap-counts"
    _seed_project(tmp_path, project_id)
    workspace = tmp_path / project_id / "workspace"
    _write_json(
        workspace / "route_map_validation.json",
        {
            "days": [
                {
                    "day": 1,
                    "legs": [
                        {"status": "passed", "distance_km": 10.0, "duration_h": 0.3},
                        {"status": "anomaly", "distance_km": 900.0, "duration_h": 12.0},
                        {"status": "provider_error", "distance_km": 1.0, "duration_h": 0.1},
                        {"status": "passed", "distance_km": 2.0},
                        {
                            "status": "anomaly",
                            "transport_mode": "public_transit",
                            "distance_km": 850.0,
                            "duration_h": 10.0,
                        },
                        {
                            "status": "mode_specific_not_driving",
                            "transport_mode": "mixed",
                            "distance_km": 30.0,
                            "duration_h": 1.0,
                            "segments": [
                                {
                                    "mode": "driving",
                                    "status": "passed",
                                    "distance_km": 3.0,
                                    "duration_h": 0.2,
                                },
                                {
                                    "mode": "taxi",
                                    "status": "anomaly",
                                    "distance_km": 20.0,
                                    "duration_h": 0.4,
                                },
                                {
                                    "mode": "public_transit",
                                    "status": "anomaly",
                                    "distance_km": 7.0,
                                    "duration_h": 0.4,
                                },
                                {
                                    "mode": "driving",
                                    "status": "provider_error",
                                    "distance_km": 1.0,
                                    "duration_h": 0.1,
                                },
                            ],
                        },
                    ],
                }
            ]
        },
    )
    amap_log = tmp_path / project_id / "agents" / "route_agent" / "amap_evidence_log.jsonl"
    amap_log.parent.mkdir(parents=True, exist_ok=True)
    amap_log.write_text(
        json.dumps({"query": "one", "source": "route_map_validation"})
        + "\n"
        + json.dumps({"query": "two", "source": "live_query"})
        + "\ninvalid\n",
        encoding="utf-8",
    )

    metrics = extract_project_metrics(project_id, _entry(project_id), projects_dir=tmp_path)

    usage = metrics["tool_usage"]
    assert usage["amap_persisted_record_count"]["value"] == 2
    assert usage["amap_evidence_log_record_count"]["value"] == 2
    assert usage["amap_route_map_reuse_log_record_count"]["value"] == 1
    assert usage["amap_route_map_measurement_count"]["value"] == 4
    assert usage["amap_persisted_total_count"]["value"] == 5


def test_metrics_segment_formal_and_continuation_runs_and_count_local_claim_repairs(
    tmp_path,
):
    project_id = "exp-run-segments"
    _seed_project(tmp_path, project_id)
    trace = tmp_path / project_id / "agents" / "codex_skill" / "trace.jsonl"
    rows = [
        {"event": "run_start", "status": "started", "timestamp": "2026-09-07T00:00:00Z"},
        {"event": "agent_completed", "local_claim_repairs": 2, "timestamp": "2026-09-07T00:02:00Z"},
        {"event": "run_completed", "status": "validation_failed_preserved", "timestamp": "2026-09-07T00:10:00Z"},
        {"event": "run_start", "status": "diagnostic", "timestamp": "2026-09-07T02:00:00Z"},
        {"event": "agent_completed", "local_claim_repairs": 1, "timestamp": "2026-09-07T02:05:00Z"},
        {"event": "agent_completed", "local_claim_repairs": 1, "timestamp": "2026-09-07T02:10:00Z"},
        {
            "event": "run_completed",
            "status": "completed_with_documented_degradation",
            "continuation_mode": "post_fix_contaminated_continuation",
            "timestamp": "2026-09-07T02:20:00Z",
        },
    ]
    trace.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    metrics = extract_project_metrics(project_id, _entry(project_id), projects_dir=tmp_path)

    execution = metrics["execution"]
    assert execution["wall_clock_seconds"]["value"] == 600
    assert execution["project_trace_span_seconds"]["value"] == 8400
    assert execution["formal_run"]["status"] == "validation_failed_preserved"
    assert execution["formal_run"]["wall_clock_seconds"] == 600
    assert execution["formal_run"]["capture_mode"] == "reconstructed"
    assert len(execution["continuation_runs"]) == 1
    assert execution["continuation_runs"][0]["continuation_mode"] == "post_fix_contaminated_continuation"
    assert execution["continuation_runs"][0]["wall_clock_seconds"] == 1200
    assert execution["business_retry_count"]["value"] == 3
    assert execution["locally_repaired_claim_count"]["value"] == 4
    assert metrics["identity"]["declared_comparison_grade"] == "B"
    assert metrics["identity"]["comparison_grade"] == "C"


def test_route_metrics_keep_live_status_when_later_offline_run_skips_provider(tmp_path):
    _seed_project(tmp_path, "exp-route-modes")
    workspace = tmp_path / "exp-route-modes" / "workspace"
    route_artifact = {
        "route_plan": json.loads((workspace / "route_plan.json").read_text(encoding="utf-8")),
        "route_entities": json.loads((workspace / "route_entities.json").read_text(encoding="utf-8")),
        "route_execution": json.loads((workspace / "route_execution.json").read_text(encoding="utf-8")),
    }
    import hashlib

    artifact_sha = hashlib.sha256(
        json.dumps(
            route_artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    rows = [
        {
            "stage": "route",
            "observation_mode": "live",
            "artifact_sha256": artifact_sha,
            "route_gate": {
                "component_statuses": {
                    "route_map": "warning",
                    "route_feasibility": "needs_confirmation",
                },
                "unresolved_check_ids": ["route-check-1"],
            },
        },
        {
            "stage": "route",
            "observation_mode": "offline",
            "artifact_sha256": artifact_sha,
            "route_gate": {
                "component_statuses": {
                    "route_map": "not_run_offline",
                    "route_feasibility": "not_run_offline",
                    "route_structure": "passed",
                },
                "unresolved_check_ids": [],
            },
        },
    ]
    (workspace / "validation_runs.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    metrics = extract_project_metrics(
        "exp-route-modes", _entry("exp-route-modes"), projects_dir=tmp_path
    )

    assert metrics["route_gate"]["latest_live_component_statuses"]["route_map"] == "warning"
    assert metrics["route_gate"]["latest_offline_component_statuses"]["route_map"] == "not_run_offline"
    assert metrics["route_gate"]["effective_component_statuses"]["route_map"] == "warning"
    assert metrics["route_gate"]["live_unresolved_check_ids"] == ["route-check-1"]


def test_metrics_include_research_scope_hits_and_provider_errors(tmp_path):
    _seed_project(tmp_path, "exp-research")
    workspace = tmp_path / "exp-research" / "workspace"
    rows = [
        {
            "source_kind": "material",
            "channel": "ugc",
            "stage": "lifestyle",
            "status": "degraded",
            "result_count": 2,
            "result_scopes": {"global_core": 1, "project_supplement": 1},
            "scope_errors": {"ugc": {"embedding": {"type": "APIError"}}},
            "retrieval_diagnostics": {
                "modality_errors": {"embedding": {"type": "APIError"}}
            },
        },
        {
            "source_kind": "web",
            "channel": "web",
            "stage": "poi_detail",
            "status": "empty",
            "result_count": 0,
            "result_scopes": {},
        },
    ]
    (workspace / "research_runs.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    _write_json(
        workspace / "material_baseline.json",
        {
            "corpus_sha256": "base-corpus",
            "prompt_fingerprint": {"aggregate_sha256": "prompt-sha", "per_role": {}},
        },
    )

    metrics = extract_project_metrics(
        "exp-research", _entry("exp-research"), projects_dir=tmp_path
    )

    assert metrics["research"]["call_count"] == 2
    assert metrics["research"]["result_count"] == 2
    assert metrics["research"]["empty_result_count"] == 1
    assert metrics["research"]["source_kind_counts"] == {"material": 1, "web": 1}
    assert metrics["research"]["channel_counts"] == {"ugc": 1, "web": 1}
    assert metrics["research"]["status_counts"] == {"degraded": 1, "empty": 1}
    assert metrics["research"]["scope_hit_counts"] == {
        "global_core": 1,
        "project_supplement": 1,
    }
    assert metrics["research"]["scope_error_counts"] == {"ugc": 1}
    assert metrics["research"]["modality_error_counts"] == {"embedding": 1}
    assert metrics["research"]["base_corpus_sha256"] == "base-corpus"


def test_completed_langgraph_metrics_do_not_report_recovery_as_unfinished(
    tmp_path, monkeypatch
):
    _seed_project(tmp_path, "exp-langgraph")
    workspace = tmp_path / "exp-langgraph" / "workspace"
    route = json.loads((workspace / "route_plan.json").read_text(encoding="utf-8"))
    route["days"][0]["pois"] = [{"name": "武侯祠", "needs_detail": True}]
    _write_json(workspace / "route_plan.json", route)
    _write_json(
        workspace / "outputs" / "poi_details" / "manifest.json",
        {
            "generated": [
                {
                    "day": 1,
                    "poi_name": "武侯祠",
                    "file": "day1_武侯祠.json",
                    "citation_status": "generation_failed",
                    "failure_kind": "provider_or_tool_failure",
                }
            ]
        },
    )
    monkeypatch.setattr(
        experiment_metrics,
        "build_resume_plan",
        lambda project_id: {"status": "action_required"},
    )
    entry = {**_entry("exp-langgraph"), "harness": "langgraph"}

    metrics = extract_project_metrics("exp-langgraph", entry, projects_dir=tmp_path)

    assert metrics["identity"]["project_status"] == "完成"
    assert metrics["status"]["delivery"] == "passed"
    assert metrics["status"]["route_risk"] == "warning"
    assert metrics["status"]["poi_completeness"] == "attention_required"
    assert metrics["status"]["recovery"] == "completed"
    assert metrics["status"]["attention"] == [
        "route_risk",
        "poi_completeness",
    ]
    assert metrics["outputs"]["poi_detail_success_count"] == 0
    assert next(stage for stage in metrics["stages"] if stage["key"] == "poi")["status"] == "generation_failed"


def test_incomplete_langgraph_metrics_still_use_resume_plan(tmp_path, monkeypatch):
    _seed_project(tmp_path, "exp-langgraph-running")
    workspace = tmp_path / "exp-langgraph-running" / "workspace"
    _write_json(workspace / "progress.json", {"state": "并行调研"})
    monkeypatch.setattr(
        experiment_metrics,
        "build_resume_plan",
        lambda project_id: {"status": "action_required"},
    )

    metrics = extract_project_metrics(
        "exp-langgraph-running",
        {**_entry("exp-langgraph-running"), "harness": "langgraph"},
        projects_dir=tmp_path,
    )

    assert metrics["status"]["recovery"] == "action_required"
    assert "recovery" in metrics["status"]["attention"]


def test_metrics_separate_poi_citation_status_from_structural_content_depth(
    tmp_path, monkeypatch
):
    _seed_project(tmp_path, "exp-depth")
    workspace = tmp_path / "exp-depth" / "workspace"
    route = json.loads((workspace / "route_plan.json").read_text(encoding="utf-8"))
    route["days"][0]["pois"] = [
        {"name": "空详情", "needs_detail": True},
        {"name": "路线框架", "needs_detail": True},
        {"name": "可执行路线", "needs_detail": True},
    ]
    _write_json(workspace / "route_plan.json", route)
    details = [
        {"day": 1, "poi_name": "空详情", "routes": [], "citation_status": "passed"},
        {
            "day": 1,
            "poi_name": "路线框架",
            "routes": [{"route_name": "框架", "recommended": False, "route_segments": []}],
            "citation_status": "passed",
        },
        {
            "day": 1,
            "poi_name": "可执行路线",
            "routes": [
                {
                    "route_name": "经典线",
                    "recommended": True,
                    "route_segments": [
                        {
                            "order": 1,
                            "point_name": "入口",
                            "claims": [{"claim_id": "poi-claim-1", "claim_text": "从入口出发"}],
                        }
                    ],
                }
            ],
            "citation_status": "passed",
        },
    ]
    generated = []
    for index, detail in enumerate(details, start=1):
        filename = f"day1_depth_{index}.json"
        _write_json(workspace / "outputs" / "poi_details" / filename, detail)
        generated.append(
            {
                "day": 1,
                "poi_name": detail["poi_name"],
                "file": filename,
                "citation_status": "passed",
            }
        )
    _write_json(
        workspace / "outputs" / "poi_details" / "manifest.json",
        {"generated": generated},
    )
    monkeypatch.setattr(
        experiment_metrics,
        "build_resume_plan",
        lambda project_id: {"status": "completed"},
    )

    metrics = extract_project_metrics("exp-depth", _entry("exp-depth"), projects_dir=tmp_path)

    by_name = {item["poi_name"]: item for item in metrics["poi_details"]}
    assert by_name["空详情"]["citation_status"] == "passed"
    assert by_name["空详情"]["content_depth"] == "empty"
    assert by_name["路线框架"]["content_depth"] == "route_outline"
    assert by_name["可执行路线"]["content_depth"] == "recommended_route"
    assert metrics["outputs"]["poi_depth_empty_count"] == 1
    assert metrics["outputs"]["poi_depth_outline_count"] == 1
    assert metrics["outputs"]["poi_depth_recommended_count"] == 1


def test_rebuildable_index_is_separate_from_execution_and_checkpoint_databases(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    _seed_project(projects_dir, "exp-one")
    workspace = projects_dir / "exp-one" / "workspace"
    _write_json(
        workspace / "experiment.json",
        {
            "schema_version": "experiment-manifest-v1",
            "project_id": "exp-one",
            "scenario_id": "chengdu-one-day",
            "experiment_version": "codex-test-v1",
            "harness": "codex_skill",
            "comparison_grade": "A",
            "completion_mode": "completed_with_documented_degradation",
        },
    )
    (workspace / "post_run_corrections.jsonl").write_text(
        json.dumps(
            {
                "correction_type": "experiment_validity",
                "declared_comparison_grade": "A",
                "effective_comparison_grade": "C",
                "formal_run_status": "validation_failed_preserved",
                "continuation_status": "completed_with_documented_degradation",
                "continuation_mode": "post_fix_contaminated_continuation",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    trace = projects_dir / "exp-one" / "agents" / "codex_skill" / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "event": "run_start",
                    "timestamp": "2026-09-02T00:00:00Z",
                },
                {
                    "event": "run_completed",
                    "status": "validation_failed_preserved",
                    "timestamp": "2026-09-02T00:01:00Z",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    catalog_path = tmp_path / "catalog.json"
    _write_json(catalog_path, {"experiments": [_entry("exp-one")]})
    db_path = tmp_path / "runtime" / "experiment_index.sqlite3"
    monkeypatch.setattr(experiment_index.review_index, "aggregate_optimization_candidates", lambda: [])

    first = experiment_index.rebuild_index(projects_dir=projects_dir, catalog_path=catalog_path, db_path=db_path)
    second = experiment_index.rebuild_index(projects_dir=projects_dir, catalog_path=catalog_path, db_path=db_path)

    assert first["added"] == 1
    assert second["skipped"] == 1
    assert (projects_dir / "exp-one" / "workspace" / "metrics.json").is_file()
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {
            "experiments",
            "experiment_relations",
            "candidate_experiments",
            "comparison_pairs",
            "candidate_pairs",
        }
        assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
        comparison_grade, completion_mode = conn.execute(
            "SELECT comparison_grade, completion_mode FROM experiments WHERE project_id = ?",
            ("exp-one",),
        ).fetchone()
        assert comparison_grade == "C"
        assert completion_mode == "validation_failed_preserved"


def test_rebuild_indexes_explicit_one_to_one_pair_and_normalizes_legacy_relation(
    tmp_path, monkeypatch
):
    projects_dir = tmp_path / "projects"
    _seed_project(projects_dir, "codex-exp")
    _seed_project(projects_dir, "langgraph-exp")
    codex_entry = {
        **_entry("codex-exp"),
        "relations": [{"project_id": "langgraph-exp", "type": "comparison_peer"}],
    }
    langgraph_entry = {
        **_entry("langgraph-exp"),
        "harness": "langgraph",
        "experiment_version": "langgraph-test-v1",
        "relations": [],
    }
    catalog_path = tmp_path / "catalog.json"
    _write_json(
        catalog_path,
        {
            "experiments": [codex_entry, langgraph_entry],
            "comparison_pairs": [
                {
                    "pair_id": "pair-test-v1",
                    "codex_project_id": "codex-exp",
                    "langgraph_project_id": "langgraph-exp",
                    "source_input_project_id": "input-v1",
                    "comparison_scope": "system",
                    "declared_grade": "A",
                }
            ],
        },
    )
    db_path = tmp_path / "runtime" / "experiment_index.sqlite3"
    candidate = {
        "candidate_id": "candidate-one",
        "project_ids": ["codex-exp"],
        "source_pair_ids": ["pair-test-v1"],
        "validation_pair_ids": [],
        "target_scope": "shared_kernel",
    }
    monkeypatch.setattr(
        experiment_index.review_index,
        "aggregate_optimization_candidates",
        lambda: [candidate],
    )
    monkeypatch.setattr(
        experiment_index.review_store,
        "read_findings",
        lambda project_id: [
            ReviewFinding(
                finding_id="finding-one",
                project_id=project_id,
                problem_type="manifest_mismatch",
                root_cause="code",
                severity="medium",
                status="open",
                notes="manifest 与详情状态不一致。",
            )
        ] if project_id == "codex-exp" else [],
    )

    result = experiment_index.rebuild_index(
        projects_dir=projects_dir,
        catalog_path=catalog_path,
        db_path=db_path,
    )

    assert result["relation_failures"] == []
    assert result["pairs"] == {
        "registered": 1,
        "complete": 1,
        "incomplete": 0,
        "invalid": 0,
    }
    monkeypatch.setenv("EXPERIMENT_INDEX_DB", str(db_path))
    pairs = experiment_index.list_comparison_pairs()
    assert len(pairs) == 1
    assert pairs[0]["pair_id"] == "pair-test-v1"
    assert pairs[0]["effective_grade"] == "B"
    assert pairs[0]["comparability"]["verdict"] == "system_comparable"
    assert [item["candidate_id"] for item in pairs[0]["source_candidates"]] == [
        "candidate-one"
    ]
    assert pairs[0]["observed_issues"]["active_finding_count"] == 1
    assert pairs[0]["observed_issues"]["codex"]["preview_findings"] == [
        {
            "finding_id": "finding-one",
            "problem_type": "manifest_mismatch",
            "root_cause": "code",
            "severity": "medium",
            "status": "open",
            "summary": "manifest 与详情状态不一致。",
        }
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT target_project_id, relation_type FROM experiment_relations"
        ).fetchone() == ("langgraph-exp", "comparison_peer")


def test_pair_projection_rejects_mismatched_fixed_input_and_marks_missing_side_incomplete():
    def metrics(project_id: str, harness: str, requirements_sha: str) -> dict:
        return {
            "identity": {
                "project_id": project_id,
                "harness": harness,
                "requirements_sha": requirements_sha,
                "route_sha": "route-one",
                "project_status": "完成",
                "artifact_status": "deliverable",
                "comparison_grade": "A",
                "model": "same-model",
                "provider": "same-provider",
            },
            "research": {"base_corpus_sha256": "materials-one"},
        }

    raw = {
        "pair_id": "pair-invalid",
        "codex_project_id": "codex-exp",
        "langgraph_project_id": "langgraph-exp",
        "comparison_scope": "harness_isolated",
        "declared_grade": "A",
    }
    invalid = experiment_index._build_pair_projection(
        raw,
        {
            "codex-exp": metrics("codex-exp", "codex_skill", "requirements-a"),
            "langgraph-exp": metrics("langgraph-exp", "langgraph", "requirements-b"),
        },
        indexed_at="2026-09-14T00:00:00Z",
    )
    incomplete = experiment_index._build_pair_projection(
        raw,
        {"codex-exp": metrics("codex-exp", "codex_skill", "requirements-a")},
        indexed_at="2026-09-14T00:00:00Z",
    )

    assert invalid["status"] == "invalid"
    assert invalid["effective_grade"] == "C"
    assert invalid["comparability"]["verdict"] == "not_comparable"
    assert incomplete["status"] == "incomplete"
    assert incomplete["comparability"]["verdict"] == "incomplete"


def test_pair_stage_observations_surface_differences_without_claiming_root_cause():
    observations = experiment_index._pair_stage_observations(
        {
            "stages": [
                {"key": "route", "label": "Route Gate", "status": "passed"},
                {"key": "lifestyle", "label": "Lifestyle", "status": "force_finalized"},
                {"key": "pitfall", "label": "Pitfall", "status": "warning"},
            ]
        },
        {
            "stages": [
                {"key": "route", "label": "Route Gate", "status": "passed"},
                {"key": "lifestyle", "label": "Lifestyle", "status": "passed"},
                {"key": "pitfall", "label": "Pitfall", "status": "warning"},
            ]
        },
    )

    assert observations == [
        {
            "key": "lifestyle",
            "label": "Lifestyle",
            "langgraph_status": "force_finalized",
            "codex_status": "passed",
            "kind": "different",
        },
        {
            "key": "pitfall",
            "label": "Pitfall",
            "langgraph_status": "warning",
            "codex_status": "warning",
            "kind": "shared_attention",
        },
    ]
