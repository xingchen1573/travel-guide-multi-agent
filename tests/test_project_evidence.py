from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.materials import citation_check, project_evidence
from src.tools.material_tools import make_material_tools
from src.workspace import store
from src.workspace.schema import RouteDay, RoutePlan, SourceRef


@pytest.fixture
def evidence_project(tmp_path, monkeypatch):
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    project_id = "project-evidence-test"
    log_scope = "route_agent"
    log_dir = tmp_path / project_id / "agents" / log_scope
    log_dir.mkdir(parents=True)
    source_url = "https://example.com/source/1"
    (log_dir / "web_search_log.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "web-search-log-v2",
                "query": "测试景点 门票",
                "created_at": "2026-08-27T00:00:00+00:00",
                "results": [
                    {
                        "title": "测试景点票务公告",
                        "excerpt": "测试景点门票信息以官方页面为准。",
                        "url": source_url,
                    },
                    {
                        "title": "其他景点入口公告",
                        "excerpt": "其他景点目前只开放一处入口。",
                        "url": "https://example.com/source/2",
                    },
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return project_id, log_scope, source_url, tmp_path


def test_project_evidence_requires_observed_source_url(evidence_project):
    project_id, log_scope, _, _ = evidence_project

    with pytest.raises(ValueError, match="web-search log"):
        project_evidence.write_project_evidence(
            project_id,
            actor="route_agent",
            log_scope=log_scope,
            summary="一条有事实含义的内容",
            source_url="https://example.com/not-observed",
        )


def test_project_evidence_rejects_legacy_url_only_log(tmp_path, monkeypatch):
    monkeypatch.setattr(project_evidence, "PROJECTS_DIR", tmp_path)
    project_id = "legacy-url-only"
    log_scope = "route_agent"
    source_url = "https://example.com/source/1"
    log_dir = tmp_path / project_id / "agents" / log_scope
    log_dir.mkdir(parents=True)
    (log_dir / "web_search_log.jsonl").write_text(
        json.dumps({"query": "测试景点", "result": f"来源 {source_url}"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="structured"):
        project_evidence.write_project_evidence(
            project_id,
            actor="route_agent",
            log_scope=log_scope,
            summary="Agent自行补写的事实",
            source_url=source_url,
        )


def test_concurrent_evidence_writes_are_unique_and_valid(evidence_project):
    project_id, log_scope, source_url, root = evidence_project

    def write_one(index: int):
        return project_evidence.write_project_evidence(
            project_id,
            actor="route_agent",
            log_scope=log_scope,
            summary=f"测试景点第{index}条补充证据",
            source_url=source_url,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(write_one, range(40)))

    paths = list((root / project_id / "materials").glob("evidence-*.json"))
    assert len(paths) == 40
    assert len({record.note_id for record in records}) == 40
    assert all(json.loads(path.read_text(encoding="utf-8"))["note_type"] == "evidence" for path in paths)


def test_legacy_and_malformed_notes_never_enter_retrieval(evidence_project):
    project_id, log_scope, source_url, root = evidence_project
    materials_dir = root / project_id / "materials"
    materials_dir.mkdir(parents=True, exist_ok=True)
    (materials_dir / "note-000.json").write_text(
        json.dumps({"content": "旧工作笔记里的禁用内容", "platform": "系统知识"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (materials_dir / "note-001.json").write_text('{"content":"损坏', encoding="utf-8")
    project_evidence.write_project_evidence(
        project_id,
        actor="route_agent",
        log_scope=log_scope,
        summary="测试景点有一处可以核验的入口",
        source_url=source_url,
    )

    results = project_evidence.retrieve_project_evidence(project_id, "测试景点", top_k=5)

    assert len(results) == 1
    assert results[0]["scope"] == "project"
    assert results[0]["platform"] == "project_evidence"
    assert results[0]["summary"] == "测试景点有一处可以核验的入口"
    assert results[0]["content_snippet"] == "测试景点门票信息以官方页面为准。"
    assert "旧工作笔记" not in json.dumps(results, ensure_ascii=False)


def test_project_evidence_is_available_to_citation_check(evidence_project, monkeypatch):
    project_id, log_scope, source_url, root = evidence_project
    record = project_evidence.write_project_evidence(
        project_id,
        actor="route_agent",
        log_scope=log_scope,
        summary="测试景点有一处可以核验的入口",
        source_url=source_url,
    )
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", root / "global-materials")
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", root)

    chunks = citation_check.load_chunk_texts("测试主题", project_id)

    chunk = chunks[f"project-evidence-{record.note_id}"]
    assert chunk == "测试景点票务公告\n测试景点门票信息以官方页面为准。"
    assert "一处可以核验的入口" not in chunk


def test_project_evidence_uses_only_the_selected_urls_excerpt(evidence_project):
    project_id, log_scope, source_url, _ = evidence_project

    record = project_evidence.write_project_evidence(
        project_id,
        actor="route_agent",
        log_scope=log_scope,
        summary="Agent声称测试景点只开放一处入口",
        source_url=source_url,
    )

    assert record.evidence_excerpt == "测试景点门票信息以官方页面为准。"
    assert "只开放一处入口" not in record.evidence_excerpt
    assert record.source_title == "测试景点票务公告"
    assert record.source_query == "测试景点 门票"
    assert record.log_scope == log_scope


def test_project_evidence_scope_reader_uses_exact_scope_and_legacy_url_fallback(
    evidence_project,
):
    project_id, log_scope, source_url, root = evidence_project
    scoped = project_evidence.write_project_evidence(
        project_id,
        actor="route_agent",
        log_scope=log_scope,
        summary="当前工作单元证据",
        source_url=source_url,
    )
    materials_dir = root / project_id / "materials"
    legacy_payload = scoped.model_dump(mode="json")
    legacy_payload["note_id"] = "legacy-note"
    legacy_payload.pop("log_scope")
    (materials_dir / "evidence-legacy-note.json").write_text(
        json.dumps(legacy_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    foreign_payload = scoped.model_dump(mode="json")
    foreign_payload["note_id"] = "foreign-note"
    foreign_payload["log_scope"] = "poi_detail_agent/day9_其他景点"
    (materials_dir / "evidence-foreign-note.json").write_text(
        json.dumps(foreign_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    records = project_evidence.list_project_evidence_for_log_scope(
        project_id, log_scope
    )

    # The exact-scope record wins URL de-duplication. The legacy copy is a
    # compatible fallback, while an explicitly foreign scope is never adopted.
    assert [record.note_id for record in records] == [scoped.note_id]


def test_project_evidence_scope_reader_recovers_legacy_record_by_exact_url(
    evidence_project,
):
    project_id, log_scope, source_url, root = evidence_project
    materials_dir = root / project_id / "materials"
    materials_dir.mkdir(parents=True, exist_ok=True)
    legacy_payload = {
        "schema_version": "project-evidence-v2",
        "note_id": "legacy-only",
        "note_type": "evidence",
        "summary": "旧证据",
        "evidence_excerpt": "测试景点门票信息以官方页面为准。",
        "actor": "poi_detail_agent",
        "source_url": source_url,
        "source_title": "测试景点票务公告",
        "source_query": "测试景点 门票",
        "source_observed_at": "2026-08-27T00:00:00+00:00",
        "source_platform": "example.com",
        "created_at": "2026-08-27T00:00:00+00:00",
    }
    (materials_dir / "evidence-legacy-only.json").write_text(
        json.dumps(legacy_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    records = project_evidence.list_project_evidence_for_log_scope(
        project_id, log_scope
    )

    assert [record.note_id for record in records] == ["legacy-only"]
    assert records[0].log_scope == ""


def test_citation_web_loader_reads_structured_and_legacy_logs(evidence_project, monkeypatch):
    project_id, log_scope, _, root = evidence_project
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", root)
    log_path = root / project_id / "agents" / log_scope / "web_search_log.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"query": "旧查询", "result": "旧格式摘录（来源：https://legacy.example.com）"},
                ensure_ascii=False,
            )
            + "\n"
        )

    evidence = citation_check._load_web_search_evidence(project_id, log_scope)

    assert "测试景点门票信息以官方页面为准" in evidence
    assert "其他景点目前只开放一处入口" in evidence
    assert "旧格式摘录" in evidence


def test_project_evidence_can_support_route_citation(evidence_project, monkeypatch):
    project_id, log_scope, source_url, root = evidence_project
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", root)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", root / "global-materials")
    store.init_project(project_id)
    record = project_evidence.write_project_evidence(
        project_id,
        actor="route_agent",
        log_scope=log_scope,
        summary="测试景点有一处可以核验的入口",
        source_url=source_url,
    )
    store.write_route_plan(
        project_id,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="测试城",
                    pois=[{"name": "测试景点"}],
                    sources=[
                        SourceRef(
                            platform="project_evidence",
                            doc_id=record.note_id,
                            chunk_id=f"project-evidence-{record.note_id}",
                            source_url=source_url,
                            scope="project",
                        )
                    ],
                )
            ]
        ),
    )

    assert citation_check.verify_route_citations(project_id, "测试主题") == []


def test_agent_summary_cannot_self_support_a_route_citation(evidence_project, monkeypatch):
    project_id, log_scope, source_url, root = evidence_project
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", root)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", root / "global-materials")

    class _FakeAliasJudge:
        def invoke(self, _prompt):
            return citation_check.AliasResolution(
                resolved=[], still_missing=["虚构入口"], reason="原始摘录没有提到虚构入口"
            )

    class _FakeModel:
        def with_structured_output(self, _schema, method=None):
            return _FakeAliasJudge()

    monkeypatch.setattr(
        citation_check,
        "get_chat_model",
        lambda temperature=0, max_tokens=None: _FakeModel(),
    )
    store.init_project(project_id)
    record = project_evidence.write_project_evidence(
        project_id,
        actor="route_agent",
        log_scope=log_scope,
        summary="虚构入口是测试景点唯一开放的入口",
        source_url=source_url,
    )
    store.write_route_plan(
        project_id,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="测试城",
                    pois=[{"name": "虚构入口"}],
                    sources=[
                        SourceRef(
                            platform="project_evidence",
                            doc_id=record.note_id,
                            chunk_id=f"project-evidence-{record.note_id}",
                            source_url=source_url,
                            scope="project",
                        )
                    ],
                )
            ]
        ),
    )

    issues = citation_check.verify_route_citations(project_id, "测试主题")

    assert len(issues) == 1
    assert issues[0]["missing_entities"] == ["虚构入口"]


def test_project_evidence_requires_content_check_even_when_entity_matches(monkeypatch):
    captured_prompts = []

    class _FakeContentJudge:
        def invoke(self, prompt):
            captured_prompts.append(prompt)
            return citation_check.CitationVerdict(
                supported=False, reason="原始摘录只讲门票，没有提到入口数量"
            )

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            assert schema is citation_check.CitationVerdict
            return _FakeContentJudge()

    monkeypatch.setattr(
        citation_check,
        "get_chat_model",
        lambda temperature=0, max_tokens=None: _FakeModel(),
    )
    issues = citation_check._verify_items(
        [
            {
                "label": "project-evidence-content-check",
                "claimed_entities": ["测试景点"],
                "chunk_ids": ["project-evidence-test"],
                "content": "测试景点目前只开放一处入口",
            }
        ],
        {"project-evidence-test": "测试景点门票信息以官方页面为准。"},
    )

    assert len(issues) == 1
    assert issues[0]["reason"] == "原始摘录只讲门票，没有提到入口数量"
    assert "原始搜索摘录" in captured_prompts[0]


def test_append_material_tool_writes_evidence_not_working_note(evidence_project, monkeypatch):
    project_id, log_scope, source_url, _ = evidence_project
    monkeypatch.setattr("src.tools.material_tools._retrieve", lambda topic, query, top_k: [])
    tools = make_material_tools(
        "测试主题",
        project_id,
        actor="route_agent",
        log_scope=log_scope,
        allow_append_evidence=True,
    )
    append_tool = next(tool for tool in tools if tool.name == "append_material")

    message = append_tool.invoke(
        {"summary": "测试景点有一处入口", "source_url": source_url}
    )

    assert "已保存Project补充证据" in message
    assert '"platform": "project_evidence"' in message
    assert '"chunk_id": "project-evidence-' in message
    assert "不要把磁盘文件名" in message
    records = project_evidence.list_project_evidence(project_id)
    assert len(records) == 1
    assert records[0].actor == "route_agent"
    assert records[0].note_type == "evidence"
    assert records[0].summary == "测试景点有一处入口"
    assert records[0].evidence_excerpt == "测试景点门票信息以官方页面为准。"
    assert records[0].source_platform == "example.com"
