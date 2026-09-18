import pytest

from src.codex_skill import preflight
from src.codex_skill.bridge import prepare_route_only
from src.materials import baseline, project_evidence, route_map_check
from src.workspace import store
from src.workspace.schema import (
    RouteDay,
    RouteEntities,
    RouteEntityResolution,
    RouteExecutionPlan,
    RouteGeoPoint,
    RouteLeg,
    RouteLegSegment,
    RoutePlan,
    RoutePoi,
)


def test_fixed_route_preflight_persists_hashes(tmp_path, monkeypatch):
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
    monkeypatch.setattr(preflight, "_probe_web", lambda: {"status": "passed", "attempts": 1})
    monkeypatch.setattr(preflight, "_probe_amap", lambda: {"status": "passed", "attempts": 1})

    result = preflight.run_fixed_route_preflight("source", "target", live=True)

    assert result["ready"] is True
    assert result["requirements_sha256"]
    assert result["route_structure_sha256"]
    assert result["report_path"] == "runtime/preflight/target.json"
    assert (tmp_path / "runtime" / "preflight" / "target.json").is_file()


def test_fixed_route_preflight_requires_comparison_sidecars_when_requested(
    tmp_path, monkeypatch
):
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

    result = preflight.run_fixed_route_preflight(
        "source",
        "target",
        require_comparison_sidecars=True,
        persist=False,
    )

    assert result["ready"] is False
    assert result["comparison_sidecars_required"] is True
    assert result["checks"]["fixed_route_input_snapshot"]["status"] == "missing"
    assert result["checks"]["route_entities"]["status"] == "missing"
    assert result["checks"]["route_execution"]["status"] == "missing"


def test_fixed_route_preflight_accepts_current_comparison_sidecars(tmp_path, monkeypatch):
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
    store.write_fixed_route_input_snapshot(
        "source",
        {"source": "user_confirmed_table", "days": [{"day": 1, "route": "省博"}]},
    )
    store.write_route_entities(
        "source",
        "route_agent",
        RouteEntities(
            entities=[
                RouteEntityResolution(
                    day=1,
                    poi_index=0,
                    formal_name="省博",
                    provider_id="gansu-museum",
                    provider_name="甘肃省博物馆",
                    location=RouteGeoPoint(lng=103.75, lat=36.07),
                    status="resolved",
                )
            ]
        ),
    )
    store.write_route_execution(
        "source",
        "route_agent",
        RouteExecutionPlan(route_legs=[]),
    )
    material_root = tmp_path / "material_library"
    topic_index = material_root / "index" / "兰州"
    topic_raw = material_root / "raw" / "兰州"
    topic_index.mkdir(parents=True)
    topic_raw.mkdir(parents=True)
    (topic_index / "entries.jsonl").write_text(
        '{"doc_id":"doc-1","source_file":"one.md"}\n', encoding="utf-8"
    )
    (topic_index / "chunks.jsonl").write_text(
        '{"chunk_id":"chunk-1","doc_id":"doc-1","text":"兰州"}\n', encoding="utf-8"
    )
    (topic_raw / "manifest.json").write_text(
        '{"items":[{"source_file":"one.md"}]}', encoding="utf-8"
    )
    monkeypatch.setattr(baseline, "MATERIAL_LIBRARY_DIR", material_root)
    monkeypatch.setattr(baseline, "PROJECT_ROOT", tmp_path)
    store.write_material_baseline(
        "source", baseline.build_material_baseline("兰州", "兰州")
    )

    result = preflight.run_fixed_route_preflight(
        "source",
        "target",
        require_comparison_sidecars=True,
        persist=False,
    )

    assert result["ready"] is True
    assert result["checks"]["fixed_route_input_snapshot"]["status"] == "passed"
    assert result["checks"]["route_entities"]["status"] == "passed"
    assert result["checks"]["route_execution"]["status"] == "passed"
    assert result["checks"]["material_baseline"]["status"] == "passed"


def test_fixed_route_preflight_rejects_material_topic_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements(
        "source",
        "青甘大环线8天固定路线",
        {"destination": "青甘大环线", "days": 8},
    )
    store.write_route_plan(
        "source",
        "route_agent",
        RoutePlan(days=[RouteDay(day=1, city="西宁", pois=[RoutePoi(name="青海湖")])]),
    )

    result = preflight.run_fixed_route_preflight(
        "source",
        "target",
        require_comparison_sidecars=True,
        persist=False,
    )

    assert result["ready"] is False
    assert any(
        issue["code"] == "material_topic_mismatch"
        for issue in result["checks"]["material_baseline"]["issues"]
    )


def test_route_only_preflight_requires_requirements_but_not_route(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "峨眉山", {"destination": "峨眉山", "days": 3})
    monkeypatch.setattr(preflight, "_probe_web", lambda: {"status": "passed", "attempts": 1})
    monkeypatch.setattr(preflight, "_probe_amap", lambda: {"status": "passed", "attempts": 1})

    result = preflight.run_route_only_preflight("source", "target", live=True)

    assert result["ready"] is True
    assert result["mode"] == "full_plan_route_only"
    assert result["requirements_sha256"]
    assert result["route_structure_sha256"] is None
    assert result["checks"]["material_topic"] == {
        "status": "passed",
        "topic": "峨眉山",
        "destination": "峨眉山",
    }
    assert "material_topic" in result["required_checks"]
    assert "route_plan" not in result["checks"]


def test_route_only_preflight_rejects_material_topic_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements(
        "source",
        "成都3天美食人文慢节奏摄影",
        {"destination": "成都", "days": 3},
    )

    result = preflight.run_route_only_preflight(
        "source",
        "target",
        persist=False,
    )

    assert result["ready"] is False
    assert result["checks"]["material_topic"] == {
        "status": "failed",
        "reason": "material_topic_mismatch",
        "topic": "成都3天美食人文慢节奏摄影",
        "destination": "成都",
    }
    assert "material_topic" in result["required_checks"]


def test_route_only_preflight_allows_matching_existing_prepared_target(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "杭州", {"destination": "杭州", "days": 2})
    prepare_route_only("source", "target")

    result = preflight.run_route_only_preflight(
        "source",
        "target",
        allow_existing_target=True,
        persist=False,
    )

    assert result["ready"] is True
    assert result["checks"]["target_project"]["status"] == "passed"
    assert result["checks"]["target_project"]["source_project_id"] == "source"


@pytest.mark.parametrize(
    "relative_path",
    [
        "outputs/lifestyle.json",
        "outputs/pitfall.json",
        "final_guide.md",
        "final_guide.blocks.json",
        "final_guide.meta.json",
        "outputs/poi_details/manifest.json",
        "outputs/poi_details/day1_test.json",
    ],
)
def test_route_only_preflight_rejects_existing_target_with_downstream_artifact(
    tmp_path,
    monkeypatch,
    relative_path,
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "杭州", {"destination": "杭州", "days": 2})
    prepare_route_only("source", "target")
    artifact_path = store.workspace_dir("target") / relative_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{}" if artifact_path.suffix == ".json" else "污染产物", encoding="utf-8")

    result = preflight.run_route_only_preflight(
        "source",
        "target",
        allow_existing_target=True,
        persist=False,
    )

    assert result["ready"] is False
    assert result["checks"]["target_project"]["reason"] == (
        "route_only_downstream_artifacts_present"
    )
    assert relative_path in result["checks"]["target_project"][
        "downstream_artifacts"
    ]


def test_route_only_preflight_rejects_existing_target_with_tampered_mode(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "杭州", {"destination": "杭州", "days": 2})
    prepare_route_only("source", "target")
    manifest = store.read_workspace("target", "experiment.json")
    manifest["mode"] = "fixed_route"
    store.write_experiment_manifest("target", manifest)

    result = preflight.run_route_only_preflight(
        "source",
        "target",
        allow_existing_target=True,
        persist=False,
    )

    assert result["ready"] is False
    assert result["checks"]["target_project"]["reason"] == (
        "route_only_manifest_invalid"
    )
    assert "route_only_mode_invalid" in result["checks"]["target_project"][
        "issue_codes"
    ]


def test_route_only_preflight_rejects_existing_target_with_wrong_source_relation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "杭州", {"destination": "杭州", "days": 2})
    prepare_route_only("source", "target")
    manifest = store.read_workspace("target", "experiment.json")
    manifest["relations"] = [
        {"type": "requirements_only_source", "project_id": "other-source"}
    ]
    store.write_experiment_manifest("target", manifest)

    result = preflight.run_route_only_preflight(
        "source",
        "target",
        allow_existing_target=True,
        persist=False,
    )

    assert result["ready"] is False
    assert result["checks"]["target_project"]["reason"] == "source_relation_mismatch"


def test_route_only_preflight_rejects_existing_target_with_requirements_drift(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "杭州", {"destination": "杭州", "days": 2})
    prepare_route_only("source", "target")
    store.write_requirements("target", "杭州", {"destination": "杭州", "days": 3})

    result = preflight.run_route_only_preflight(
        "source",
        "target",
        allow_existing_target=True,
        persist=False,
    )

    assert result["ready"] is False
    assert result["checks"]["target_project"]["reason"] == "requirements_mismatch"


def test_route_only_preflight_rejects_existing_non_route_only_manifest(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(preflight, "RUNTIME_DIR", tmp_path / "runtime")
    store.init_project("source")
    store.write_requirements("source", "杭州", {"destination": "杭州", "days": 2})
    store.init_project("target")
    store.write_requirements("target", "杭州", {"destination": "杭州", "days": 2})
    store.write_experiment_manifest(
        "target",
        {
            "scenario_id": "fixed",
            "experiment_version": "target",
            "harness": "langgraph",
            "inherited_downstream_outputs": False,
            "relations": [
                {"type": "requirements_only_source", "project_id": "source"}
            ],
        },
    )

    result = preflight.run_route_only_preflight(
        "source",
        "target",
        allow_existing_target=True,
        persist=False,
    )

    assert result["ready"] is False
    assert result["checks"]["target_project"]["reason"] == "route_only_manifest_invalid"


def test_composite_route_label_requires_human_confirmation():
    result = route_map_check.resolve_entity("黑独山/胭脂山", "冷湖镇")

    assert result["status"] == "requires_human_confirmation"
    assert result["selected"] is None


def test_entity_resolution_keeps_original_and_provider_names_separate(monkeypatch):
    monkeypatch.setattr(
        route_map_check.geocode,
        "search_poi_candidates",
        lambda *args, **kwargs: [
            {
                "provider_id": "poi-1",
                "provider_name": "甘肃省博物馆",
                "type": "科教文化服务",
                "address": "七里河区西津西路",
                "province": "甘肃省",
                "city": "兰州市",
                "district": "七里河区",
                "lng": 103.7,
                "lat": 36.0,
            }
        ],
    )

    result = route_map_check.resolve_entity("省博", "山丹 → 省博 → 兰州")

    assert result["original_name"] == "省博"
    assert result["query_name"] == "甘肃省博物馆"
    assert result["selected"]["provider_name"] == "甘肃省博物馆"


def test_entity_resolution_rejects_exact_name_outside_admin_scope(monkeypatch):
    monkeypatch.setattr(
        route_map_check.geocode,
        "search_poi_candidates",
        lambda *args, **kwargs: [
            {
                "provider_id": "wrong-city",
                "provider_name": "金顶",
                "type": "风景名胜",
                "address": "北京市海淀区",
                "province": "北京市",
                "city": "北京市",
                "district": "海淀区",
                "lng": 116.3,
                "lat": 39.9,
            }
        ],
    )

    result = route_map_check.resolve_entity("金顶", "峨眉山市")

    assert result["status"] == "unresolved"
    assert result["selected"] is None
    assert result["selection_reason"] == ["all_candidates_outside_admin_scope"]
    assert result["candidates"][0]["rejected_reason"] == "admin_scope_mismatch"


def test_route_map_consumes_pinned_entities_and_non_driving_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    store.init_project("route-project")
    route_plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="峨眉山市",
                pois=[RoutePoi(name="雷洞坪"), RoutePoi(name="金顶")],
            )
        ]
    )
    store.write_route_plan("route-project", "route_agent", route_plan)
    store.write_route_entities(
        "route-project",
        "route_agent",
        RouteEntities(
            entities=[
                RouteEntityResolution(
                    day=1,
                    poi_index=0,
                    formal_name="雷洞坪",
                    provider_query="雷洞坪停车场",
                    provider_id="leidongping",
                    provider_name="雷洞坪停车场",
                    province="四川省",
                    city="乐山市",
                    district="峨眉山市",
                    location=RouteGeoPoint(lng=103.33, lat=29.54),
                    selection_reason=["route_agent_locked"],
                    confidence=0.95,
                    status="resolved",
                ),
                RouteEntityResolution(
                    day=1,
                    poi_index=1,
                    formal_name="金顶",
                    provider_query="峨眉山金顶",
                    provider_id="jinding",
                    provider_name="峨眉山金顶",
                    province="四川省",
                    city="乐山市",
                    district="峨眉山市",
                    location=RouteGeoPoint(lng=103.34, lat=29.52),
                    selection_reason=["route_agent_locked"],
                    confidence=0.98,
                    status="resolved",
                ),
            ]
        ),
    )
    store.write_route_execution(
        "route-project",
        "route_agent",
        RouteExecutionPlan(
            route_legs=[
                RouteLeg(
                    day=1,
                    from_poi_index=0,
                    to_poi_index=1,
                    from_name="雷洞坪",
                    to_name="金顶",
                    transport_mode="cable_car",
                    duration_minutes=20,
                    distance_km=5.0,
                )
            ]
        ),
    )
    monkeypatch.setattr(
        route_map_check.geocode,
        "search_poi_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not search")),
    )
    monkeypatch.setattr(
        route_map_check.driving,
        "driving_distance",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not drive")),
    )

    report = route_map_check.verify_route_map(
        "route-project",
        "峨眉山",
        route_plan.model_dump(mode="json"),
        persist=False,
    )

    assert [item["resolution_source"] for item in report["days"][0]["entities"]] == [
        "route_entities",
        "route_entities",
    ]
    leg = report["days"][0]["legs"][0]
    assert leg["transport_mode"] == "cable_car"
    assert leg["mode_source"] == "route_execution"
    assert leg["status"] == "mode_specific_not_driving"
    assert leg["distance_km"] == 5.0
    assert leg["duration_h"] == 0.33

    store.write_route_execution(
        "route-project",
        "route_agent",
        RouteExecutionPlan(
            route_legs=[
                RouteLeg(
                    day=1,
                    from_poi_index=0,
                    to_poi_index=1,
                    transport_mode="mixed",
                    segments=[
                        RouteLegSegment(
                            from_name="雷洞坪",
                            to_name="金顶",
                            mode="cable_car",
                        )
                    ],
                )
            ]
        ),
    )

    mixed_report = route_map_check.verify_route_map(
        "route-project",
        "峨眉山",
        route_plan.model_dump(mode="json"),
        persist=False,
    )
    mixed_leg = mixed_report["days"][0]["legs"][0]
    assert mixed_leg["status"] == "unknown"
    assert mixed_leg["segments"][0]["status"] == "unknown"
    assert mixed_report["days"][0]["schedule_check"]["status"] == "unknown"
    assert any(
        issue["code"] == "route_map_mixed_segment_unknown"
        for issue in mixed_report["issues"]
    )


def test_route_map_persists_driving_provider_failure_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    store.init_project("route-provider-error")
    route_plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="敦煌",
                pois=[RoutePoi(name="鸣沙山月牙泉"), RoutePoi(name="敦煌")],
            )
        ]
    )
    store.write_route_plan("route-provider-error", "route_agent", route_plan)
    store.write_route_entities(
        "route-provider-error",
        "route_agent",
        RouteEntities(
            entities=[
                RouteEntityResolution(
                    day=1,
                    poi_index=0,
                    formal_name="鸣沙山月牙泉",
                    provider_id="mingsha",
                    location=RouteGeoPoint(lng=94.67, lat=40.09),
                    status="resolved",
                ),
                RouteEntityResolution(
                    day=1,
                    poi_index=1,
                    formal_name="敦煌",
                    provider_id="dunhuang",
                    location=RouteGeoPoint(lng=94.66, lat=40.14),
                    status="resolved",
                ),
            ]
        ),
    )

    def fail_driving(*args, diagnostic=None, **kwargs):
        diagnostic.update({"code": "api_error", "detail": "10003: DAILY_QUERY_OVER_LIMIT"})
        return None

    monkeypatch.setattr(route_map_check.driving, "driving_distance", fail_driving)

    report = route_map_check.verify_route_map(
        "route-provider-error",
        "敦煌",
        route_plan.model_dump(mode="json"),
        persist=False,
    )

    leg = report["days"][0]["legs"][0]
    assert leg["status"] == "provider_error"
    assert leg["provider_error"] == {
        "code": "api_error",
        "detail": "10003: DAILY_QUERY_OVER_LIMIT",
    }
    assert leg["from_entity"] == {
        "original_name": "鸣沙山月牙泉",
        "query_name": "鸣沙山月牙泉",
        "resolution_source": "route_entities",
        "provider_id": "mingsha",
        "provider_name": "鸣沙山月牙泉",
        "province": "",
        "city": "",
        "district": "",
        "lng": 94.67,
        "lat": 40.09,
    }
    assert leg["to_entity"]["provider_id"] == "dunhuang"
    assert leg["to_entity"]["lng"] == 94.66
