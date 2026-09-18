"""run_scenario_eval.py（docs/ARCHITECTURE.md第17节"系统性评测集设计v1"）的单元测试。

不发起真实run_plan/真实LLM调用/真实联网搜索——那部分是决策9明确要接受真实成本才能验证的
部分，留给人工手动跑一次真实场景验证（见docs/ISSUES.md）。这里只测试纯逻辑/mock掉agent
调用之后的行为：场景加载、确定性checklist（决策3第二类）、陪伴阶段交互驱动（决策4）、
judge打分的captured-dict流程（决策3第一类+决策5）。
"""
from __future__ import annotations

import json

import pytest

import run_scenario_eval as m
from src.workspace import store


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "scenario-eval-test"
    store.init_project(project_id)
    return project_id


# ---------- 决策6：场景加载 ----------


def test_load_scenario_reads_json(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SCENARIOS_DIR", tmp_path)
    (tmp_path / "demo.json").write_text(
        json.dumps({"scenario_id": "demo", "requirements": {"destination": "成都"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    scenario = m.load_scenario("demo")
    assert scenario["requirements"]["destination"] == "成都"


def test_load_scenario_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SCENARIOS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        m.load_scenario("not_exist")


def test_list_scenario_ids_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SCENARIOS_DIR", tmp_path)
    (tmp_path / "b_scenario.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a_scenario.json").write_text("{}", encoding="utf-8")
    assert m.list_scenario_ids() == ["a_scenario", "b_scenario"]


def test_list_scenario_ids_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SCENARIOS_DIR", tmp_path / "not_exist")
    assert m.list_scenario_ids() == []


def test_real_chengdu_scenario_file_is_valid():
    """决策11：这一轮先打通的成都场景定义文件本身要能被正常加载、字段齐全。"""
    scenario = m.load_scenario("chengdu_3d")
    assert scenario["requirements"]["destination"] == "成都"
    assert scenario["destination_type"] == "城市人文"
    assert len(scenario["companion_interactions"]) >= 1
    for interaction in scenario["companion_interactions"]:
        assert "id" in interaction and "query" in interaction


# ---------- 决策3第二类：确定性checklist ----------


def test_checklist_flags_missing_final_guide(isolated_project, monkeypatch):
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    result = m.run_checklist(isolated_project, [])
    assert result["checks"]["final_guide_generated"] is False
    assert any(i["item"] == "final_guide" and i["severity"] == "blocker" for i in result["issues"])


def test_checklist_passes_when_guide_present_and_no_conflicts(isolated_project, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    # 跳过真实生成site/poster（决策9只在真实评测时接受成本，单元测试不应该发真实网络请求）。
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: (store.workspace_dir(project_id) / "site.html").write_text("<html></html>", encoding="utf-8"))
    monkeypatch.setattr(run_generate_poster, "generate_poster", lambda project_id, confirm=True: (store.workspace_dir(project_id) / "poster.png").write_bytes(b"fake"))

    result = m.run_checklist(isolated_project, [])
    assert result["checks"]["final_guide_generated"] is True
    assert result["checks"]["site_generated"] is True
    assert result["checks"]["poster_generated"] is True
    assert result["checks"]["unresolved_conflicts"] == 0
    assert result["checks"]["force_finalized_count"] == 0
    assert result["issues"] == []


def test_checklist_site_generation_failure_is_non_blocking_issue(isolated_project, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])

    from scripts.delivery import run_generate_poster, run_generate_site

    def _boom(project_id):
        raise RuntimeError("网络超时")

    monkeypatch.setattr(run_generate_site, "generate_site", _boom)
    monkeypatch.setattr(run_generate_poster, "generate_poster", lambda project_id, confirm=True: (store.workspace_dir(project_id) / "poster.png").write_bytes(b"fake"))

    result = m.run_checklist(isolated_project, [])
    assert result["checks"]["site_generated"] is False
    assert any(i["item"] == "site" and i["severity"] == "major" for i in result["issues"])
    # site失败不应该阻断poster检查继续跑。
    assert result["checks"]["poster_generated"] is True


def test_checklist_flags_unresolved_conflicts(isolated_project, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: None)
    monkeypatch.setattr(run_generate_poster, "generate_poster", lambda project_id, confirm=True: None)

    store.report_conflict(isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason="内容冲突")

    result = m.run_checklist(isolated_project, [])
    assert result["checks"]["unresolved_conflicts"] == 1
    assert any(i["item"] == "conflicts" for i in result["issues"])


def test_checklist_flags_force_finalized_conflict(isolated_project, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: None)
    monkeypatch.setattr(run_generate_poster, "generate_poster", lambda project_id, confirm=True: None)

    store.report_conflict(isolated_project, actor="citation_checker", target="lifestyle_agent.food", reason="核验失败")
    store.mark_force_finalize(isolated_project)

    result = m.run_checklist(isolated_project, [])
    assert result["checks"]["unresolved_conflicts"] == 0  # force_finalized不算pending
    assert result["checks"]["force_finalized_count"] == 1
    assert any(i["item"] == "force_finalized" for i in result["issues"])


def test_checklist_flags_generation_failed_manifest_entry_once(
    isolated_project, monkeypatch
):
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 攻略正文",
        meta={},
    )
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: None)
    monkeypatch.setattr(
        run_generate_poster,
        "generate_poster",
        lambda project_id, confirm=True: None,
    )
    manifest_path = (
        store.workspace_dir(isolated_project)
        / "outputs"
        / "poi_details"
        / "manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "generated": [
                    {
                        "poi_name": "洱海生态廊道",
                        "day": 1,
                        "file": "day1_erhai.json",
                        "citation_status": "generation_failed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = m.run_checklist(isolated_project, [])

    assert result["checks"]["force_finalized_count"] == 1
    force_issues = [item for item in result["issues"] if item["item"] == "force_finalized"]
    assert len(force_issues) == 1


def test_checklist_flags_force_finalized_poi_detail(isolated_project, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: None)
    monkeypatch.setattr(run_generate_poster, "generate_poster", lambda project_id, confirm=True: None)

    manifest_path = store.workspace_dir(isolated_project) / "outputs" / "poi_details" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {"generated": [{"poi_name": "武侯祠", "day": 1, "file": "day1_武侯祠.json", "citation_status": "force_finalized"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    detail_path = manifest_path.parent / "day1_武侯祠.json"
    detail_path.write_text(
        json.dumps(
            {
                "poi_name": "武侯祠",
                "day": 1,
                "citation_status": "force_finalized",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = m.run_checklist(isolated_project, [])
    assert result["checks"]["force_finalized_count"] == 1


def test_checklist_rejects_clean_when_only_route_state_is_force_finalized(
    isolated_project, monkeypatch
):
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 攻略正文",
        meta={},
    )
    route_path = store.workspace_dir(isolated_project) / "route_plan.json"
    route_path.write_text(
        json.dumps(
            {
                "days": [
                    {
                        "day": 1,
                        "city": "成都",
                        "pois": [],
                        "citation_status": "force_finalized",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: None)
    monkeypatch.setattr(run_generate_poster, "generate_poster", lambda project_id, confirm=True: None)

    result = m.run_checklist(isolated_project, [])

    assert result["checks"]["force_finalized_count"] == 1
    assert any(
        issue["item"] == "force_finalized" and issue["severity"] == "major"
        for issue in result["issues"]
    )


def test_checklist_rejects_clean_when_only_feasibility_is_force_finalized(
    isolated_project, monkeypatch
):
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 攻略正文",
        meta={},
    )
    (store.workspace_dir(isolated_project) / "route_feasibility.json").write_text(
        json.dumps(
            {"overall_status": "warning", "force_finalized": True, "checks": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "run_companion_interactions", lambda *a, **k: [])
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: None)
    monkeypatch.setattr(
        run_generate_poster,
        "generate_poster",
        lambda project_id, confirm=True: None,
    )

    result = m.run_checklist(isolated_project, [])

    assert result["checks"]["force_finalized_count"] == 1
    assert any(
        issue["item"] == "force_finalized" and issue["severity"] == "major"
        for issue in result["issues"]
    )


def test_checklist_folds_in_companion_interaction_issues(isolated_project, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    from scripts.delivery import run_generate_poster, run_generate_site

    monkeypatch.setattr(run_generate_site, "generate_site", lambda project_id: None)
    monkeypatch.setattr(run_generate_poster, "generate_poster", lambda project_id, confirm=True: None)

    monkeypatch.setattr(
        m,
        "run_companion_interactions",
        lambda *a, **k: [
            {"id": "no_answer", "query": "q1", "answer": None, "actual_agent": "guide_agent", "has_answer": False, "error": None},
            {
                "id": "wrong_route",
                "query": "q2",
                "answer": "答案",
                "actual_agent": "guide_agent",
                "expected_agent": "realtime_agent",
                "routed_as_expected": False,
                "has_answer": True,
                "error": None,
            },
            {"id": "crashed", "query": "q3", "has_answer": False, "error": "ValueError: boom"},
        ],
    )

    result = m.run_checklist(isolated_project, [])
    items = {i["item"] for i in result["issues"]}
    assert "companion:no_answer" in items
    assert "companion:wrong_route" in items
    assert "companion:crashed" in items
    severities = {i["item"]: i["severity"] for i in result["issues"]}
    assert severities["companion:crashed"] == "major"
    assert severities["companion:wrong_route"] == "minor"  # 路由不符预期不阻断，只是需要人工核对


# ---------- 决策4：陪伴阶段交互驱动 ----------


def test_companion_interaction_reads_answer_and_matches_expected_agent(monkeypatch):
    monkeypatch.setattr(
        m,
        "run_companion",
        lambda project_id, query: {
            "companion_answer": "武侯祠今晚6点关门",
            "last_agent_result": {"agent": "realtime_agent"},
        },
    )
    results = m.run_companion_interactions(
        "proj",
        [{"id": "hours", "query": "武侯祠几点关门", "expected_agent": "realtime_agent"}],
    )
    assert results[0]["has_answer"] is True
    assert results[0]["routed_as_expected"] is True
    assert results[0]["actual_agent"] == "realtime_agent"


def test_companion_interaction_lifestyle_expected_agent_maps_to_companion_query(monkeypatch):
    monkeypatch.setattr(
        m,
        "run_companion",
        lambda project_id, query: {
            "companion_answer": "附近有家甜品店不错",
            "last_agent_result": {"agent": "companion_query"},
        },
    )
    results = m.run_companion_interactions(
        "proj",
        [{"id": "sweets", "query": "附近有什么甜品店", "expected_agent": "lifestyle_agent"}],
    )
    assert results[0]["routed_as_expected"] is True


def test_companion_interaction_auto_approves_realtime_patch_interrupt(monkeypatch):
    monkeypatch.setattr(m, "run_companion", lambda project_id, query: {"__interrupt__": [{"patch": "x"}]})
    monkeypatch.setattr(
        m,
        "resume_companion",
        lambda project_id, decision: {
            "companion_answer": None,
            "companion_fallback_answer": "已批准修改",
            "last_agent_result": {"agent": "realtime_agent"},
        },
    )
    results = m.run_companion_interactions("proj", [{"id": "patch_case", "query": "这家店关门了帮我改一下"}])
    assert results[0]["auto_approved_interrupt"] is True
    assert results[0]["has_answer"] is True


def test_companion_interaction_culture_via_edit_reads_outputs_culture_json(isolated_project, monkeypatch):
    store.write_culture_output(
        isolated_project,
        actor="culture_agent",
        culture_output=__import__("src.workspace.schema", fromlist=["CultureOutput", "CultureItem"]).CultureOutput(
            items=[
                __import__("src.workspace.schema", fromlist=["CultureItem"]).CultureItem(
                    question="杜甫草堂名字由来", answer="因杜甫曾在此居住得名", references=["https://example.com"]
                )
            ]
        ),
    )
    monkeypatch.setattr(
        m,
        "run_edit",
        lambda project_id, entry_target, edit_request: {"last_agent_result": {"agent": "culture_agent"}},
    )
    results = m.run_companion_interactions(
        isolated_project,
        [
            {
                "id": "culture_query",
                "query": "杜甫草堂名字由来",
                "expected_agent": "culture_agent",
                "entry_target": "culture_agent_node",
                "via": "edit",
            }
        ],
    )
    assert results[0]["answer"] == "因杜甫曾在此居住得名"
    assert results[0]["routed_as_expected"] is True


def test_companion_interaction_captures_exception(monkeypatch):
    def _boom(project_id, query):
        raise RuntimeError("图执行失败")

    monkeypatch.setattr(m, "run_companion", _boom)
    results = m.run_companion_interactions("proj", [{"id": "broken", "query": "问题"}])
    assert results[0]["has_answer"] is False
    assert "RuntimeError" in results[0]["error"]


# ---------- 决策3第一类 + 决策5：judge打分 ----------


def test_run_judge_submits_score_directly(isolated_project, monkeypatch):
    captured_tools = {}

    def fake_create_react_agent(model, tools):
        captured_tools["tools"] = tools
        return tools

    def fake_invoke_with_retry(agent, messages):
        submit_score = agent[-1]
        submit_score.invoke(
            {
                "completeness_score": 4,
                "completeness_reason": "齐全",
                "consistency_score": 5,
                "consistency_reason": "无矛盾",
                "faithfulness_score": 5,
                "faithfulness_reason": "均有依据",
                "requirement_adherence_score": 4,
                "requirement_adherence_reason": "基本满足",
                "readability_score": 4,
                "readability_reason": "清晰",
                "usability_score": 3,
                "usability_reason": "部分内容略简略",
                "overall_comment": "整体不错",
            }
        )
        return {"messages": messages}

    monkeypatch.setattr(m, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(m, "invoke_with_retry", fake_invoke_with_retry)

    score = m.run_judge(isolated_project, {"destination": "成都", "days": 3})
    assert score["completeness_score"] == 4
    assert score["usability_score"] == 3
    assert "error" not in score


def test_run_judge_retries_when_score_not_submitted(isolated_project, monkeypatch):
    call_count = {"n": 0}

    def fake_create_react_agent(model, tools):
        return tools

    def fake_invoke_with_retry(agent, messages):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"messages": messages + [{"role": "assistant", "content": "我觉得还不错"}]}
        submit_score = agent[-1]
        submit_score.invoke(
            {
                "completeness_score": 3,
                "completeness_reason": "r",
                "consistency_score": 3,
                "consistency_reason": "r",
                "faithfulness_score": 3,
                "faithfulness_reason": "r",
                "requirement_adherence_score": 3,
                "requirement_adherence_reason": "r",
                "readability_score": 3,
                "readability_reason": "r",
                "usability_score": 3,
                "usability_reason": "r",
                "overall_comment": "r",
            }
        )
        return {"messages": messages}

    monkeypatch.setattr(m, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(m, "invoke_with_retry", fake_invoke_with_retry)

    score = m.run_judge(isolated_project, {"destination": "成都", "days": 3})
    assert call_count["n"] == 2
    assert score["completeness_score"] == 3


def test_run_judge_reports_error_when_never_submitted(isolated_project, monkeypatch):
    monkeypatch.setattr(m, "create_react_agent", lambda model, tools: tools)
    monkeypatch.setattr(m, "invoke_with_retry", lambda agent, messages: {"messages": messages})

    score = m.run_judge(isolated_project, {"destination": "成都", "days": 3})
    assert "error" in score


# ---------- 决策11：单场景运行入口（run_plan失败时如实记录、不让整个批量运行崩溃） ----------


def test_run_scenario_records_fatal_error_when_run_plan_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(m, "SCENARIOS_DIR", tmp_path / "scenarios")
    monkeypatch.setattr(m, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "demo.json").write_text(
        json.dumps({"scenario_id": "demo", "requirements": {"destination": "成都", "days": 1}}, ensure_ascii=False),
        encoding="utf-8",
    )

    def _boom(project_id, requirements):
        raise RuntimeError("LLM限流")

    monkeypatch.setattr(m, "run_plan", _boom)

    summary = m.run_scenario("demo", "20260101T000000Z")
    assert "run_plan失败" in summary["fatal_error"]
    checklist_path = tmp_path / "runs" / "20260101T000000Z" / "demo" / "checklist.json"
    assert checklist_path.exists()
    # 审计计时：run_plan失败也要记录耗时，不能只在成功路径才有timing字段
    assert "timing" in summary
    assert summary["timing"]["started_at"]
    assert summary["timing"]["finished_at"]
    assert summary["timing"]["run_plan_seconds"] >= 0
    assert summary["timing"]["total_seconds"] >= 0


def test_run_scenario_stops_at_action_required_without_checklist_or_judge(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(m, "SCENARIOS_DIR", tmp_path / "scenarios")
    monkeypatch.setattr(m, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "demo.json").write_text(
        json.dumps(
            {
                "scenario_id": "demo",
                "requirements": {"destination": "成都", "days": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        m,
        "run_plan",
        lambda project_id, requirements: {
            "__interrupt__": [
                {
                    "route_change_proposal": {
                        "proposal_id": "proposal-1",
                        "reason": "历史公告状态待确认",
                        "evidence": "官方信息",
                    }
                }
            ]
        },
    )
    monkeypatch.setattr(
        m,
        "run_checklist",
        lambda *args, **kwargs: pytest.fail("action-required后不得运行checklist"),
    )
    monkeypatch.setattr(
        m,
        "run_judge",
        lambda *args, **kwargs: pytest.fail("action-required后不得运行judge"),
    )

    summary = m.run_scenario("demo", "20260101T000000Z")

    assert summary["status"] == "action_required"
    assert summary["proposal_id"] == "proposal-1"
    assert summary["decision_options"] == ["keep", "confirm", "revise:<意见>"]


def test_run_scenario_records_per_stage_timing_on_success(tmp_path, monkeypatch):
    """2026-08-11新增：审计用的分阶段耗时——之前只能靠人工对比project目录时间戳
    跟summary.json的mtime估算总耗时，既麻烦又不精确。这里验证成功路径下
    run_plan/checklist_and_companion/judge三个阶段+总耗时都被记录下来。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(m, "SCENARIOS_DIR", tmp_path / "scenarios")
    monkeypatch.setattr(m, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "demo.json").write_text(
        json.dumps({"scenario_id": "demo", "requirements": {"destination": "成都", "days": 1}}, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(m, "run_plan", lambda project_id, requirements: None)
    monkeypatch.setattr(
        m,
        "run_checklist",
        lambda project_id, interactions: {"checks": {"final_guide_generated": False}, "issues": []},
    )
    monkeypatch.setattr(m, "run_judge", lambda project_id, requirements: {"completeness_score": 3})

    summary = m.run_scenario("demo", "20260101T000000Z")

    timing = summary["timing"]
    assert timing["started_at"] < timing["finished_at"]
    for key in ("run_plan_seconds", "checklist_and_companion_seconds", "judge_seconds", "total_seconds"):
        assert isinstance(timing[key], (int, float))
        assert timing[key] >= 0
    # 总耗时应该约等于三个阶段之和（允许极小的调用开销误差）
    assert timing["total_seconds"] >= (
        timing["run_plan_seconds"] + timing["checklist_and_companion_seconds"] + timing["judge_seconds"]
    ) - 0.1
