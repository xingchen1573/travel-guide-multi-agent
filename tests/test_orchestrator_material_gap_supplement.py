"""素材缺口检测与限量自动补充采集机制的单元测试（docs/ARCHITECTURE.md第16节）。

覆盖：
1. `_attempt_auto_material_supplement`共享助手函数的各分支（预算耗尽/判定不缺/搜索失败/
   采集成功入库/单条采集失败释放名额/limit参数生效）。
2. `_attempt_route_gap_supplement`/`_attempt_lifestyle_gap_supplement`两个包装函数
   （按实体/字段去重、共享单次触发预算）。
3. 三条路径（poi_detail_agent_node/route_citation_check_node/citation_check_node）
   在MAX_RETRY耗尽分支正确接入这套机制，且预算耗尽/判定不缺时能正确回退到既有行为
   （route/lifestyle/POI详情均没有"呈现候选"渠道，只需确认不影响原有force_finalize流程）。

不调用真实LLM/网络——check_material_gap/search_and_rank/collect_one/ingest_topic
全部monkeypatch为可控假实现。
"""
from __future__ import annotations

import pytest

from src import orchestrator
from src.materials import citation_check
from src.orchestrator import (
    MAX_AUTO_MATERIAL_GAP_SUPPLEMENT,
    MAX_RETRY,
    _MAX_AUTO_COLLECT_PER_TRIGGER,
    _attempt_auto_material_supplement,
    _attempt_lifestyle_gap_supplement,
    _attempt_route_gap_supplement,
    citation_check_node,
    poi_detail_agent_node,
    route_citation_check_node,
)
from src.workspace import store
from src.workspace.schema import PoiDetailFile, RouteDay, RoutePlan, RoutePoi, RouteOption, RouteSegment

from conftest import fake_agent_reply


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "gap-supplement-test"
    store.init_project(project_id)
    return project_id


@pytest.fixture(autouse=True)
def _assume_collector_dependencies_ok(monkeypatch):
    """本文件测的是_attempt_auto_material_supplement的业务分支（预算/判定/采集结果），
    不是check_collector_dependencies本身（那部分见test_materials_collectors_bilibili.py
    的CollectorDependencyError专项测试）——默认mock成"环境依赖齐全"，避免这里的测试结果
    随"跑测试这台机器有没有装ffmpeg"变化（见docs/ISSUES.md第77条：环境依赖缺失应该被
    显式检测和留痕，但不应该让不相关的单测跟着环境状态一起变得不稳定）。"""
    monkeypatch.setattr("src.materials.collectors.bilibili.check_collector_dependencies", lambda: None)


@pytest.fixture(autouse=True)
def _skip_relevance_filter(monkeypatch):
    """_filter_relevant_candidates（相关性粗筛，docs/ISSUES.md第75条修复）内部会调用
    真实LLM——本文件测的是_attempt_auto_material_supplement的预算/判定/采集分支，
    不是相关性过滤本身（那部分见test_orchestrator_gap_relevance_filter.py），默认mock
    成"原样放行"，保持既有测试对候选列表数量/顺序的断言不受影响，也避免真实网络调用。"""
    monkeypatch.setattr(orchestrator, "_filter_relevant_candidates", lambda topic, entity_hint, candidates: candidates)


@pytest.fixture(autouse=True)
def _assume_ingested_files_are_searchable(monkeypatch):
    """Most tests isolate collection control flow; explicit status tests override this."""
    monkeypatch.setattr(
        orchestrator,
        "material_processing_status",
        lambda topic, source_files: {source_file: "indexed" for source_file in source_files},
    )


def _fake_candidate(n: int) -> dict:
    return {"title": f"候选{n}", "author": f"UP主{n}", "source_url": f"https://www.bilibili.com/video/BV{n}"}


# ---------------------------------------------------------------------------
# _attempt_auto_material_supplement
# ---------------------------------------------------------------------------


def test_returns_empty_when_budget_already_exhausted(isolated_project, monkeypatch):
    for _ in range(MAX_AUTO_MATERIAL_GAP_SUPPLEMENT):
        store.try_consume_material_gap_budget(isolated_project, MAX_AUTO_MATERIAL_GAP_SUPPLEMENT)

    calls = {"gap_checked": False}

    def fail_if_called(*args, **kwargs):
        calls["gap_checked"] = True
        return True

    monkeypatch.setattr(orchestrator, "check_material_gap", fail_if_called)

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["武侯祠 讲解"], ["武侯祠 讲解"], label="poi_detail", limit=2
    )

    assert result == []
    assert calls["gap_checked"] is False  # 预算已耗尽，不需要再花一次检索去判定


def test_returns_empty_when_not_a_material_gap(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: False)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("不应该调用search_and_rank——判定不是真缺就不该搜索")

    monkeypatch.setattr("src.materials.collectors.bilibili.search_and_rank", fail_if_called)

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["灌县古镇"], ["灌县古镇"], label="poi_detail", limit=2
    )

    assert result == []
    assert store.read_material_gap_supplement_count(isolated_project) == 0


def test_returns_empty_when_search_fails(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("网络失败")),
    )

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["虚构地点"], ["虚构地点"], label="poi_detail", limit=2
    )

    assert result == []
    assert store.read_material_gap_supplement_count(isolated_project) == 0


def test_blocks_before_search_when_collector_dependencies_missing(isolated_project, monkeypatch):
    """docs/ISSUES.md第77条：环境依赖（系统级ffmpeg/ffprobe、MIMO_API_KEY）缺失时
    必然会失败，应该在最前面被拦截，不该先花一次check_material_gap的检索成本，
    也不该被静默吞掉误判成"这个topic真的搜不到内容"——要显式留痕到
    material_gap_supplement.json的blocked列表，供事后排查一眼看到根因。"""
    from src.materials.collectors.bilibili import CollectorDependencyError

    monkeypatch.setattr(
        "src.materials.collectors.bilibili.check_collector_dependencies",
        lambda: (_ for _ in ()).throw(CollectorDependencyError("缺少系统级依赖：ffmpeg/ffprobe（PATH里找不到）")),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("环境依赖缺失应该在check_material_gap之前就被拦截")

    monkeypatch.setattr(orchestrator, "check_material_gap", fail_if_called)

    result = _attempt_auto_material_supplement(
        isolated_project, "洱海骑行", ["大理古城"], ["大理古城"], label="route", limit=2
    )

    assert result == []
    data = store._read_json(store._material_gap_path(isolated_project))
    assert len(data["blocked"]) == 1
    assert "ffmpeg" in data["blocked"][0]["reason"]

    changelog_path = store._changelog_path(isolated_project)
    assert "material_gap_supplement" in changelog_path.read_text(encoding="utf-8")



def test_collects_and_ingests_on_success(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda keywords, top_n, topic=None: [_fake_candidate(1), _fake_candidate(2)],
    )
    collect_calls = []

    def fake_collect_one(topic, candidate):
        collect_calls.append(candidate["title"])
        return {"source_file": f"{candidate['title']}.md"}

    monkeypatch.setattr("src.materials.collectors.bilibili.collect_one", fake_collect_one)

    ingest_calls = {"n": 0}

    def fake_ingest(topic):
        ingest_calls["n"] += 1
        return []

    monkeypatch.setattr(orchestrator, "ingest_topic", fake_ingest)

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["武侯祠 讲解"], ["武侯祠 讲解"], label="poi_detail", limit=2
    )

    assert [r["title"] for r in result] == ["候选1", "候选2"]
    assert collect_calls == ["候选1", "候选2"]
    assert ingest_calls["n"] == 1  # 批量采集完只统一入库一次，不是每条各自入库
    assert store.read_material_gap_supplement_count(isolated_project) == 2

    data = store._read_json(store._material_gap_path(isolated_project))
    assert [item["title"] for item in data["items"]] == ["候选1", "候选2"]
    assert data["items"][0]["path"] == "poi_detail"
    assert data["items"][0]["status"] == "indexed"


def test_rejected_collection_is_not_reported_as_searchable(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda keywords, top_n, topic=None: [_fake_candidate(1)],
    )
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.collect_one",
        lambda topic, candidate: {"source_file": "候选1.md"},
    )
    monkeypatch.setattr(orchestrator, "ingest_topic", lambda topic: [])
    monkeypatch.setattr(
        orchestrator,
        "material_processing_status",
        lambda topic, source_files: {"候选1.md": "rejected"},
    )

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["某地"], ["某地"], label="route", limit=1
    )

    assert result == []
    assert [item["title"] for item in result.rejected] == ["候选1"]
    data = store._read_json(store._material_gap_path(isolated_project))
    assert data["items"][0]["status"] == "rejected"
    assert store.read_material_gap_supplement_count(isolated_project) == 0


def test_pending_collection_releases_budget_until_it_is_searchable(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda keywords, top_n, topic=None: [_fake_candidate(1)],
    )
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.collect_one",
        lambda topic, candidate: {"source_file": "候选1.md"},
    )
    monkeypatch.setattr(orchestrator, "ingest_topic", lambda topic: [])
    monkeypatch.setattr(
        orchestrator,
        "material_processing_status",
        lambda topic, source_files: {"候选1.md": "pending"},
    )

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["某地"], ["某地"], label="route", limit=1
    )

    assert result == []
    assert [item["title"] for item in result.pending] == ["候选1"]
    assert store.read_material_gap_supplement_count(isolated_project) == 0


def test_release_budget_when_single_candidate_collection_fails(isolated_project, monkeypatch):
    """一条候选采集失败（下载/ASR异常/幂等跳过返回None）不应该永久占用预算名额，
    也不应该影响其他候选继续尝试。"""
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda keywords, top_n, topic=None: [_fake_candidate(1), _fake_candidate(2)],
    )

    def fake_collect_one(topic, candidate):
        if candidate["title"] == "候选1":
            return None  # 幂等跳过/下载失败
        return {"source_file": "候选2.md"}

    monkeypatch.setattr("src.materials.collectors.bilibili.collect_one", fake_collect_one)
    monkeypatch.setattr(orchestrator, "ingest_topic", lambda topic: [])

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["武侯祠 讲解"], ["武侯祠 讲解"], label="poi_detail", limit=2
    )

    assert [r["title"] for r in result] == ["候选2"]
    # 候选1失败但没有白白占用名额：最终计数只反映真正成功的1条
    assert store.read_material_gap_supplement_count(isolated_project) == 1


def test_collect_one_exception_also_releases_budget(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda keywords, top_n, topic=None: [_fake_candidate(1)],
    )
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.collect_one",
        lambda topic, candidate: (_ for _ in ()).throw(RuntimeError("下载超时")),
    )

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["武侯祠 讲解"], ["武侯祠 讲解"], label="poi_detail", limit=2
    )

    assert result == []
    assert store.read_material_gap_supplement_count(isolated_project) == 0


def test_limit_parameter_caps_number_collected(isolated_project, monkeypatch):
    """limit控制的是最终真正采集入库的条数上限，不是search_and_rank的候选池大小
    （候选池固定用_GAP_SUPPLEMENT_CANDIDATE_POOL，见该常量的注释——相关性过滤需要
    一个比limit大得多的候选池才能筛出真正相关的候选，见docs/ISSUES.md第75条）。"""
    from src.orchestrator import _GAP_SUPPLEMENT_CANDIDATE_POOL

    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    seen_top_n = {}

    def fake_search_and_rank(keywords, top_n, topic=None):
        seen_top_n["value"] = top_n
        return [_fake_candidate(i) for i in range(top_n)]

    monkeypatch.setattr("src.materials.collectors.bilibili.search_and_rank", fake_search_and_rank)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.collect_one",
        lambda topic, candidate: {"source_file": f"{candidate['title']}.md"},
    )
    monkeypatch.setattr(orchestrator, "ingest_topic", lambda topic: [])

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["武侯祠 讲解"], ["武侯祠 讲解"], label="poi_detail", limit=1
    )

    assert len(result) == 1
    assert seen_top_n["value"] == _GAP_SUPPLEMENT_CANDIDATE_POOL


def test_stops_collecting_when_project_budget_exhausted_mid_loop(isolated_project, monkeypatch):
    """预算在循环过程中被耗尽（比如剩余预算只够1条，尽管candidates/limit允许2条），
    应该停止而不是报错。"""
    for _ in range(MAX_AUTO_MATERIAL_GAP_SUPPLEMENT - 1):
        store.try_consume_material_gap_budget(isolated_project, MAX_AUTO_MATERIAL_GAP_SUPPLEMENT)
    assert store.read_material_gap_supplement_count(isolated_project) == MAX_AUTO_MATERIAL_GAP_SUPPLEMENT - 1

    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda keywords, top_n, topic=None: [_fake_candidate(1), _fake_candidate(2)],
    )
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.collect_one",
        lambda topic, candidate: {"source_file": f"{candidate['title']}.md"},
    )
    monkeypatch.setattr(orchestrator, "ingest_topic", lambda topic: [])

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["武侯祠 讲解"], ["武侯祠 讲解"], label="poi_detail", limit=2
    )

    assert len(result) == 1
    assert store.read_material_gap_supplement_count(isolated_project) == MAX_AUTO_MATERIAL_GAP_SUPPLEMENT


def test_limit_zero_returns_empty_without_any_call(isolated_project, monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("limit<=0时不应该做任何检索/采集尝试")

    monkeypatch.setattr(orchestrator, "check_material_gap", fail_if_called)

    result = _attempt_auto_material_supplement(
        isolated_project, "成都", ["武侯祠 讲解"], ["武侯祠 讲解"], label="poi_detail", limit=0
    )

    assert result == []


# ---------------------------------------------------------------------------
# _attempt_route_gap_supplement / _attempt_lifestyle_gap_supplement
# ---------------------------------------------------------------------------


def test_route_gap_supplement_dedups_entities_and_shares_trigger_budget(isolated_project, monkeypatch):
    seeds_seen = []

    def fake_attempt(project_id, topic, query_seeds, search_keywords, label, limit):
        seeds_seen.append((query_seeds[0], label, limit))
        return [_fake_candidate(1)]  # 每次都假装采集成功1条

    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", fake_attempt)

    issues = [
        {"day": 1, "missing_entities": ["武侯祠", "杜甫草堂"]},
        {"day": 2, "missing_entities": ["杜甫草堂"]},  # 重复实体，应该去重只处理一次
    ]
    result = _attempt_route_gap_supplement(isolated_project, "成都", issues)

    entities_called = sorted(s[0] for s in seeds_seen)
    assert entities_called == ["杜甫草堂", "武侯祠"]  # 去重后只有2个不同实体
    assert all(label == "route" for _, label, _ in seeds_seen)
    # 单次触发预算_MAX_AUTO_COLLECT_PER_TRIGGER在多个实体之间共享递减：
    # 第一个实体拿到满额，第二个实体拿到剩余额度
    limits_called = [limit for _, _, limit in seeds_seen]
    assert limits_called[0] == _MAX_AUTO_COLLECT_PER_TRIGGER
    assert sum(limits_called) <= _MAX_AUTO_COLLECT_PER_TRIGGER + 1  # 每次假装成功1条后递减
    assert len(result) == 2  # 两个实体各自"成功"1条


def test_route_gap_supplement_stops_when_trigger_budget_used_up(isolated_project, monkeypatch):
    call_count = {"n": 0}

    def fake_attempt(project_id, topic, query_seeds, search_keywords, label, limit):
        call_count["n"] += 1
        return [_fake_candidate(i) for i in range(limit)]  # 每次都用满剩余预算

    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", fake_attempt)

    issues = [{"day": 1, "missing_entities": ["A", "B", "C"]}]
    result = _attempt_route_gap_supplement(isolated_project, "成都", issues)

    # 第一个实体就用满了_MAX_AUTO_COLLECT_PER_TRIGGER的全部预算，后面的实体不应该再被尝试
    assert call_count["n"] == 1
    assert len(result) == _MAX_AUTO_COLLECT_PER_TRIGGER


def test_lifestyle_gap_supplement_dedups_by_field_and_entity_uses_angle_word(isolated_project, monkeypatch):
    seeds_seen = []

    def fake_attempt(project_id, topic, query_seeds, search_keywords, label, limit):
        seeds_seen.append((tuple(query_seeds), label))
        return []

    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", fake_attempt)

    issues = [
        {"field": "food", "day": 1, "missing_entities": ["胡二哥药膳蹄花"]},
        {"field": "food", "day": 2, "missing_entities": ["胡二哥药膳蹄花"]},  # 同field+同entity，去重
        {"field": "lodging", "day": 1, "missing_entities": ["某民宿"]},
    ]
    _attempt_lifestyle_gap_supplement(isolated_project, "成都", issues)

    assert len(seeds_seen) == 2  # (food, 胡二哥药膳蹄花) 和 (lodging, 某民宿)，各只处理一次
    food_seeds = next(s for s, label in seeds_seen if "胡二哥药膳蹄花 美食" in s)
    assert food_seeds[0] == "胡二哥药膳蹄花"
    lodging_seeds = next(s for s, label in seeds_seen if s[0] == "某民宿")
    assert "某民宿 住宿" in lodging_seeds
    assert all(label == "lifestyle" for _, label in seeds_seen)


def test_route_gap_supplement_falls_back_to_pois_when_missing_entities_empty(isolated_project, monkeypatch):
    """docs/ISSUES.md第58条：agent完全没写引用时（"完全没有标注sources/claimed_entities"
    分支），missing_entities是空列表，此前会被直接跳过、从不触发自动补充——真实E2E
    （黄龙溪古镇案例）复现的bug。修复后应该退化用issue自带的pois反推候选实体。"""
    seeds_seen = []

    def fake_attempt(project_id, topic, query_seeds, search_keywords, label, limit):
        seeds_seen.append(query_seeds[0])
        return []

    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", fake_attempt)

    issues = [
        {
            "day": 1,
            "missing_entities": [],
            "pois": ["黄龙溪古镇古街古巷", "河边茶馆品茶（体验盖碗茶文化）"],
        }
    ]
    _attempt_route_gap_supplement(isolated_project, "成都", issues)

    assert sorted(seeds_seen) == ["河边茶馆品茶", "黄龙溪古镇古街古巷"]


def test_route_gap_supplement_still_skips_when_no_entities_and_no_pois(isolated_project, monkeypatch):
    """missing_entities和pois都为空（理论上不应该发生，但兜底不应该报错）时，
    什么都不触发，也不应该抛异常。"""
    calls = []
    monkeypatch.setattr(
        orchestrator, "_attempt_auto_material_supplement", lambda *a, **k: calls.append(1) or []
    )

    result = _attempt_route_gap_supplement(isolated_project, "成都", [{"day": 1, "missing_entities": []}])

    assert result == []
    assert calls == []


def test_lifestyle_gap_supplement_falls_back_to_route_pois_when_missing_entities_empty(
    isolated_project, monkeypatch
):
    """docs/ISSUES.md第58条同款修复：lifestyle场景agent完全没写引用时，退化用同一天
    route_plan.json里的POI地名反推候选实体（而不是直接跳过）。"""
    store.write_route_plan(
        isolated_project,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    pois=[
                        RoutePoi(name="黄龙溪古镇古街古巷"),
                        RoutePoi(name="河边茶馆品茶（体验盖碗茶文化）"),
                    ],
                )
            ]
        ),
    )

    seeds_seen = []

    def fake_attempt(project_id, topic, query_seeds, search_keywords, label, limit):
        seeds_seen.append(query_seeds[0])
        return []

    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", fake_attempt)

    issues = [{"field": "food", "day": 1, "missing_entities": []}]
    _attempt_lifestyle_gap_supplement(isolated_project, "成都", issues)

    assert sorted(seeds_seen) == ["河边茶馆品茶", "黄龙溪古镇古街古巷"]


def test_lifestyle_gap_supplement_skips_when_no_entities_and_no_route_plan(isolated_project, monkeypatch):
    """route_plan.json不存在（理论上不该发生，MAX_RETRY耗尽分支在route核验之后才会走到）时，
    兜底返回空列表，不报错。"""
    calls = []
    monkeypatch.setattr(
        orchestrator, "_attempt_auto_material_supplement", lambda *a, **k: calls.append(1) or []
    )

    result = _attempt_lifestyle_gap_supplement(
        isolated_project, "成都", [{"field": "food", "day": 1, "missing_entities": []}]
    )

    assert result == []
    assert calls == []


# ---------------------------------------------------------------------------
# 三条路径的接入点
# ---------------------------------------------------------------------------


def _fake_poi_write(project_id: str, day: int, poi_name: str) -> None:
    detail = PoiDetailFile(
        poi_name=poi_name, day=day,
        routes=[RouteOption(route_name="经典线", recommended=True, route_segments=[
            RouteSegment(order=1, point_name="山门", highlight="起点", sources=[]),
        ])],
    )
    store.write_poi_detail(project_id, actor="poi_detail_agent", poi_detail=detail)


def test_poi_detail_agent_auto_collects_when_budget_available(isolated_project, monkeypatch):
    """预算充足+判定真缺时，poi_detail_agent_node应该自动采集成功，写入auto_collected。"""
    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        _fake_poi_write(isolated_project, 2, "青城山")
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)
    monkeypatch.setattr(
        citation_check, "verify_poi_detail_citations",
        lambda detail, topic, route_names=None, **kwargs: [{"route_name": "经典线", "order": 1, "point_name": "山门",
                                 "label": "x", "chunk_ids": [], "missing_entities": ["山门"], "reason": "无依据"}],
    )
    monkeypatch.setattr(orchestrator, "check_material_gap", lambda topic, seeds: True)
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.search_and_rank",
        lambda keywords, top_n, topic=None: [_fake_candidate(1)],
    )
    monkeypatch.setattr(
        "src.materials.collectors.bilibili.collect_one",
        lambda topic, candidate: {"source_file": "候选1.md"},
    )
    monkeypatch.setattr(orchestrator, "ingest_topic", lambda topic: [])

    state = {"project_id": isolated_project, "topic": "成都", "day": 2, "poi_name": "青城山", "needs_detail_reason": "大型景区"}
    result = poi_detail_agent_node(state)

    assert result["poi_detail_results"] == [{"day": 2, "poi_name": "青城山", "citation_status": "force_finalized"}]
    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert detail.auto_collected == [{"title": "候选1", "author": "UP主1", "source_url": "https://www.bilibili.com/video/BV1"}]


def test_poi_detail_agent_skips_auto_collect_when_budget_exhausted(isolated_project, monkeypatch):
    """项目级预算已耗尽时，应该跳过自动采集——不会因为这次扩展而破坏docs/ISSUES.md第42条
    已经验证过的既有行为。此前这里还有一条"回退到只呈现候选"的路径（`collection_suggestions`），
    已于docs/ISSUES.md第98条移除（未经相关性/内容安全过滤，真实撞见过不适宜内容混入
    最终产出），现在预算耗尽时直接force_finalize，不再有候选呈现。"""
    for _ in range(MAX_AUTO_MATERIAL_GAP_SUPPLEMENT):
        store.try_consume_material_gap_budget(isolated_project, MAX_AUTO_MATERIAL_GAP_SUPPLEMENT)

    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda project_id, topic, day, poi_name: object())

    def invoke_side_effect(agent, messages):
        _fake_poi_write(isolated_project, 2, "青城山")
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)
    monkeypatch.setattr(
        citation_check, "verify_poi_detail_citations",
        lambda detail, topic, route_names=None, **kwargs: [{"route_name": "经典线", "order": 1, "point_name": "山门",
                                 "label": "x", "chunk_ids": [], "missing_entities": ["山门"], "reason": "无依据"}],
    )

    def fail_if_called(topic, seeds):
        raise AssertionError("预算已耗尽，不应该再花一次检索去判定是否真缺")

    monkeypatch.setattr(orchestrator, "check_material_gap", fail_if_called)

    state = {"project_id": isolated_project, "topic": "成都", "day": 2, "poi_name": "青城山", "needs_detail_reason": "大型景区"}
    result = poi_detail_agent_node(state)

    assert result["poi_detail_results"] == [{"day": 2, "poi_name": "青城山", "citation_status": "force_finalized"}]
    detail = store.read_poi_detail(isolated_project, 2, "青城山")
    assert detail.auto_collected == []


def test_route_citation_check_node_calls_gap_supplement_before_force_finalize(isolated_project, monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda project_id, topic, *, write_mode="full", **kwargs: (
            object() if write_mode == "facts_only" else pytest.fail(f"unexpected write_mode: {write_mode}")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda agent, prompt, verify_fn=None, retry_note=None, **kwargs: None,
    )
    monkeypatch.setattr(
        citation_check, "verify_route_citations",
        lambda project_id, topic, days=None: [{"day": 1, "city": "成都", "area": "", "missing_entities": ["某地"], "reason": "无依据"}],
    )

    calls = {"args": None}

    def fake_gap_supplement(project_id, topic, issues):
        calls["args"] = (project_id, topic, issues)
        return []

    monkeypatch.setattr(orchestrator, "_attempt_route_gap_supplement", fake_gap_supplement)

    state = {"project_id": isolated_project, "topic": "成都"}
    result = route_citation_check_node(state)

    assert result["force_finalize"] is True
    assert calls["args"][0] == isolated_project
    assert calls["args"][1] == "成都"
    assert calls["args"][2][0]["missing_entities"] == ["某地"]
    # 既有行为不受影响：仍然真的force_finalize了
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "force_finalized"


def test_citation_check_node_calls_gap_supplement_before_force_finalize(isolated_project, monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent_retry_fix",
        lambda project_id, topic, **kwargs: object(),
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: None,
    )
    monkeypatch.setattr(
        citation_check, "verify_lifestyle_citations",
        lambda project_id, topic, fields=None, item_keys=None: [
            {"field": "food", "day": 1, "recommendation": "某推荐", "missing_entities": ["某店"], "reason": "无依据"}
        ],
    )

    calls = {"args": None}

    def fake_gap_supplement(project_id, topic, issues):
        calls["args"] = (project_id, topic, issues)
        return []

    monkeypatch.setattr(orchestrator, "_attempt_lifestyle_gap_supplement", fake_gap_supplement)

    state = {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    result = citation_check_node(state)

    assert result["force_finalize"] is True
    assert calls["args"][0] == isolated_project
    assert calls["args"][1] == "成都"
    assert calls["args"][2][0]["missing_entities"] == ["某店"]


def test_route_indexed_supplement_gets_one_scoped_repair(isolated_project, monkeypatch):
    issue = {"day": 1, "city": "成都", "area": "", "missing_entities": ["某地"], "reason": "无依据"}
    verdicts = iter([[issue], [issue], [issue], []])
    monkeypatch.setattr(
        citation_check, "verify_route_citations", lambda project_id, topic, days=None: next(verdicts)
    )
    monkeypatch.setattr(orchestrator, "_attempt_route_gap_supplement", lambda *a, **k: [_fake_candidate(1)])
    build_kwargs = []

    def fake_build(project_id, topic, *, write_mode="full", **kwargs):
        if write_mode != "facts_only":
            pytest.fail(f"unexpected write_mode: {write_mode}")
        build_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(orchestrator, "build_route_agent", fake_build)
    prompts = []
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda agent, prompt, verify_fn=None, retry_note=None, **kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: prompts.append(messages[0]["content"]),
    )

    result = route_citation_check_node({"project_id": isolated_project, "topic": "成都"})

    assert result["force_finalize"] is False
    assert len(prompts) == 1
    assert "只回炉修正Day[1]" in prompts[0]
    assert build_kwargs[-1]["convergence_work_unit"] == "route_gap_supplement_days_1"
    assert store.read_workspace(isolated_project, "conflicts.jsonl")[-1]["status"] == "resolved"


def test_route_supplement_repair_error_falls_back_to_force_finalize(isolated_project, monkeypatch):
    issue = {"day": 1, "city": "成都", "area": "", "missing_entities": ["某地"], "reason": "无依据"}
    monkeypatch.setattr(
        citation_check, "verify_route_citations", lambda project_id, topic, days=None: [issue]
    )
    monkeypatch.setattr(orchestrator, "_attempt_route_gap_supplement", lambda *a, **k: [_fake_candidate(1)])
    monkeypatch.setattr(
        orchestrator,
        "build_route_agent",
        lambda project_id, topic, *, write_mode="full", **kwargs: (
            object() if write_mode == "facts_only" else pytest.fail(f"unexpected write_mode: {write_mode}")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_write_verification",
        lambda agent, prompt, verify_fn=None, retry_note=None, **kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages: (_ for _ in ()).throw(RuntimeError("局部回炉模型不可用")),
    )

    result = route_citation_check_node({"project_id": isolated_project, "topic": "成都"})

    assert result["force_finalize"] is True
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "force_finalized"
    assert "局部回炉执行失败" in conflicts[-1]["reason"]


def test_lifestyle_indexed_supplement_gets_one_item_scoped_repair(isolated_project, monkeypatch):
    issue = {
        "field": "food", "day": 2, "recommendation": "某推荐",
        "missing_entities": ["某店"], "reason": "无依据",
    }
    verdicts = iter([[issue], [issue], [issue], []])
    monkeypatch.setattr(
        citation_check,
        "verify_lifestyle_citations",
        lambda project_id, topic, fields=None, item_keys=None: next(verdicts),
    )
    monkeypatch.setattr(orchestrator, "_attempt_lifestyle_gap_supplement", lambda *a, **k: [_fake_candidate(1)])
    monkeypatch.setattr(
        orchestrator,
        "build_lifestyle_agent_retry_fix",
        lambda project_id, topic, **kwargs: object(),
    )
    prompts = []
    monkeypatch.setattr(
        orchestrator,
        "invoke_with_retry",
        lambda agent, messages, **kwargs: prompts.append(messages[0]["content"]),
    )

    result = citation_check_node(
        {"project_id": isolated_project, "topic": "成都", "mode": "plan"}
    )

    assert result["force_finalize"] is False
    assert len(prompts) == MAX_RETRY + 1
    assert "('food', 2)" in prompts[-1]


def test_poi_indexed_supplement_gets_one_route_scoped_repair(isolated_project, monkeypatch):
    monkeypatch.setattr(orchestrator, "build_poi_detail_agent", lambda *a, **k: object())

    def invoke_side_effect(agent, messages, **kwargs):
        _fake_poi_write(isolated_project, 2, "青城山")
        return fake_agent_reply(messages)

    monkeypatch.setattr(orchestrator, "invoke_with_retry", invoke_side_effect)
    issue = {
        "route_name": "经典线", "order": 1, "point_name": "山门", "label": "x",
        "chunk_ids": [], "missing_entities": ["山门"], "reason": "无依据",
    }
    # POI补采后局部patch首次通过还要再确认一次；连续两次通过才真正收口。
    verdicts = iter([[issue], [issue], [issue], [], []])
    monkeypatch.setattr(
        citation_check,
        "verify_poi_detail_citations",
        lambda detail, topic, route_names=None, **kwargs: next(verdicts),
    )
    monkeypatch.setattr(orchestrator, "_attempt_auto_material_supplement", lambda *a, **k: [_fake_candidate(1)])

    result = poi_detail_agent_node(
        {"project_id": isolated_project, "topic": "成都", "day": 2, "poi_name": "青城山", "needs_detail_reason": "大型景区"}
    )

    assert result["poi_detail_results"] == [
        {"day": 2, "poi_name": "青城山", "citation_status": "passed"}
    ]
