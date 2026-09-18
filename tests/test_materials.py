"""素材入库/检索的单元测试。ingest对已入库素材是幂等的，不会触发新的LLM调用，
所以这个测试用已有的"成都"素材跑，不产生额外真实API成本。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import MATERIAL_LIBRARY_DIR
from src.materials import citation_check, retrieve as material_retrieve, vector_store
from src.materials.chunkers import chunk_content
from src.materials.chunkers.chapter_index import strip_chapter_index
from src.materials.chunkers.flat_transcript import chunk_flat_transcript
from src.materials.ingest import ingest_topic, remove_from_index
from src.materials.retrieve import retrieve_materials
from src.workspace import store

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_content_analysis_uses_shared_structured_model(monkeypatch):
    from src.materials import ingest

    expected = ingest._ContentAnalysis(
        is_relevant=True,
        reason="内容相关",
        entities=["金顶"],
    )
    calls = []

    class _Structured:
        def invoke(self, prompt):
            calls.append(("invoke", prompt))
            return expected

    class _Model:
        def with_structured_output(self, schema, method=None):
            calls.append(("structured", schema, method))
            return _Structured()

    monkeypatch.setattr(ingest, "get_structured_chat_model", lambda temperature=0: _Model())

    result = ingest._analyze_content("峨眉山金顶日出与云海游览经验。", "峨眉山")

    assert result == expected
    assert calls[0] == ("structured", ingest._ContentAnalysis, "json_mode")
    assert calls[1][0] == "invoke"


def test_ingest_is_idempotent():
    # 成都素材在开发过程中已经入库过一次，这里重跑应该不产生新条目
    new_entries = ingest_topic("成都")
    assert new_entries == []


# ---------- ingest.py 命令行入口（python -m src.materials.ingest <topic>）----------


def test_ingest_main_entry_actually_runs():
    """回归测试：确保 `python -m src.materials.ingest <topic>` 的 __main__ 分支真的会
    调用 ingest_topic()，不再是静默假成功（见 docs/ISSUES.md 第12条：此前完全没有
    __main__ 入口，这条命令退出码0、无任何输出，但实际什么都没做）。用不存在的topic
    触发 FileNotFoundError，不需要真实调用LLM，跑得快也不产生API费用。
    """
    result = subprocess.run(
        [sys.executable, "-m", "src.materials.ingest", "__不存在的主题_smoke_test__"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode != 0
    assert "FileNotFoundError" in result.stderr


def test_ingest_main_entry_requires_topic_arg():
    # 不传topic参数应该给出明确用法提示并非0退出，而不是无提示的TypeError堆栈
    result = subprocess.run(
        [sys.executable, "-m", "src.materials.ingest"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode != 0


def test_retrieve_finds_relevant_entries():
    results = retrieve_materials("成都", "熊猫基地", top_k=5)
    assert len(results) > 0
    assert any("熊猫" in r["content_snippet"] or "熊猫" in r["title"] for r in results)


def test_retrieve_returns_source_metadata():
    results = retrieve_materials("成都", "太古里", top_k=3)
    assert len(results) > 0
    for r in results:
        assert "platform" in r
        assert "doc_id" in r


def test_retrieve_empty_topic_returns_empty():
    results = retrieve_materials("不存在的目的地xyz", "任意查询", top_k=5)
    assert results == []


def test_unified_ugc_retrieval_keeps_bm25_when_embedding_fails(tmp_path, monkeypatch):
    topic = "青甘大环线"
    global_root = tmp_path / "global"
    overlay_root = tmp_path / "overlay"
    for root, scope in ((global_root, "global"), (overlay_root, "overlay")):
        index = root / "index" / topic
        index.mkdir(parents=True)
        chunks = [
            {
                "chunk_id": f"{scope}-chunk",
                "doc_id": f"{scope}-doc",
                "text": f"东台吉乃尔湖 {scope} 玩法提醒",
            },
            {
                "chunk_id": f"{scope}-noise-1",
                "doc_id": f"{scope}-noise-doc-1",
                "text": "西宁住宿交通",
            },
            {
                "chunk_id": f"{scope}-noise-2",
                "doc_id": f"{scope}-noise-doc-2",
                "text": "祁连山穿衣天气",
            },
        ]
        (index / "chunks.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunks),
            encoding="utf-8",
        )
        entries = [
            {
                "doc_id": f"{scope}-doc",
                "platform": "bilibili",
                "title": f"{scope} video",
                "entities": ["东台吉乃尔湖"],
            },
            {"doc_id": f"{scope}-noise-doc-1", "platform": "bilibili"},
            {"doc_id": f"{scope}-noise-doc-2", "platform": "bilibili"},
        ]
        (index / "entries.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries),
            encoding="utf-8",
        )

    class BrokenEmbeddings:
        def embed_query(self, query):
            raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(material_retrieve, "get_embeddings", lambda: BrokenEmbeddings())
    results, diagnostics = material_retrieve.retrieve_materials_union(
        topic,
        "东台吉乃尔湖",
        5,
        scopes=[("global_core", global_root), ("project_supplement", overlay_root)],
    )

    assert {item["scope"] for item in results} == {
        "global_core",
        "project_supplement",
    }
    assert diagnostics["status"] == "degraded"
    assert diagnostics["available_modalities"] == ["bm25"]
    assert diagnostics["modality_errors"]["embedding"]["type"] == "RuntimeError"


def test_unified_ugc_retrieval_keeps_embedding_when_bm25_fails(tmp_path, monkeypatch):
    topic = "青甘大环线"
    root = tmp_path / "global"
    index = root / "index" / topic
    index.mkdir(parents=True)
    (index / "chunks.jsonl").write_text(
        json.dumps(
            {"chunk_id": "c1", "doc_id": "d1", "text": "东台吉乃尔湖玩法"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (index / "entries.jsonl").write_text(
        json.dumps({"doc_id": "d1", "platform": "bilibili"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    class Embeddings:
        def embed_query(self, query):
            return [1.0, 0.0]

    monkeypatch.setattr(material_retrieve, "get_embeddings", lambda: Embeddings())
    monkeypatch.setattr(
        material_retrieve.vector_store,
        "search",
        lambda *args, **kwargs: [("c1", 0.9)],
    )
    monkeypatch.setattr(
        material_retrieve.bm25_search.BM25SearchIndex,
        "search",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bm25 unavailable")),
    )

    results, diagnostics = material_retrieve.retrieve_materials_union(
        topic,
        "东台吉乃尔湖",
        5,
        scopes=[("global_core", root)],
    )

    assert [item["chunk_id"] for item in results] == ["c1"]
    assert diagnostics["status"] == "degraded"
    assert diagnostics["available_modalities"] == ["embedding"]
    assert diagnostics["modality_errors"]["bm25"]["type"] == "RuntimeError"


def test_material_needs_sync_false_when_up_to_date():
    # 成都的raw和index已经同步过，不应该再触发前置校验
    assert store.material_needs_sync("成都") is False


def test_material_needs_sync_none_when_no_raw():
    assert store.material_needs_sync("不存在的目的地xyz") is None


def test_material_needs_sync_true_when_new_raw_file_added(tmp_path, monkeypatch):
    # 模拟"已有素材的主题下又手动加了一条新素材，但还没ingest"这个场景（之前的bug场景）
    monkeypatch.setattr("src.workspace.store.MATERIAL_LIBRARY_DIR", tmp_path)
    topic_raw = tmp_path / "raw" / "测试目的地"
    topic_raw.mkdir(parents=True)
    (topic_raw / "manifest.json").write_text(
        json.dumps({"items": [{"source_file": "a_原文.md", "title": "a"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (topic_raw / "a_原文.md").write_text("测试内容", encoding="utf-8")

    # 还没建index，应该判定为需要同步
    assert store.material_needs_sync("测试目的地") is True

    # 手动写一条index记录模拟已处理过a
    topic_index = tmp_path / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "entries.jsonl").write_text(
        json.dumps({"source_file": "a_原文.md"}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert store.material_needs_sync("测试目的地") is False

    # 再加一条raw manifest没入库的新文件，应该重新判定为需要同步
    manifest_path = topic_raw / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"].append({"source_file": "b_原文.md", "title": "b"})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert store.material_needs_sync("测试目的地") is True


def test_material_needs_sync_treats_rejected_as_processed(tmp_path, monkeypatch):
    monkeypatch.setattr("src.workspace.store.MATERIAL_LIBRARY_DIR", tmp_path)
    topic = "拒绝素材测试"
    raw_dir = tmp_path / "raw" / topic
    index_dir = tmp_path / "index" / topic
    raw_dir.mkdir(parents=True)
    index_dir.mkdir(parents=True)
    (raw_dir / "manifest.json").write_text(
        json.dumps({"items": [{"source_file": "irrelevant.md"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (index_dir / "rejected.jsonl").write_text(
        json.dumps({"source_file": "irrelevant.md", "reason": "不相关"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    assert store.material_needs_sync(topic) is False

def test_verify_items_literal_match_passes_without_llm_call():
    # 档位A字面核对：claimed_entities都能在引用的chunk原文里逐字找到时，直接判定通过，
    # 不应该触发档位B（不调用LLM），否则这个测试会因为缺少真实API而变慢/报错
    chunk_texts = {"c1": "午餐推荐叶婆婆钵钵鸡，八毛钱一根签，肉菜泡两分钟入味"}
    items = [
        {
            "label": "test-pass",
            "claimed_entities": ["叶婆婆钵钵鸡"],
            "chunk_ids": ["c1"],
            "content": "推荐叶婆婆钵钵鸡",
        }
    ]
    issues = citation_check._verify_items(items, chunk_texts)
    assert issues == []


def test_verify_items_records_successful_checker_explanation():
    chunk_texts = {"c1": "午餐推荐叶婆婆钵钵鸡，红油香但可以选择微辣。"}
    items = [
        {
            "target_type": "lifestyle_item",
            "target_key": "lifestyle:food:day:1:item:0",
            "label": "food.day1",
            "claimed_entities": ["叶婆婆钵钵鸡"],
            "sources": [
                {
                    "platform": "bilibili",
                    "doc_id": "food-doc",
                    "chunk_id": "c1",
                    "source_url": "https://example.com/food",
                }
            ],
            "chunk_ids": ["c1"],
            "content": "推荐叶婆婆钵钵鸡",
            "extra": {"field": "food", "day": 1},
        }
    ]
    checks = []

    issues = citation_check._verify_items(items, chunk_texts, result_sink=checks)

    assert issues == []
    by_scope = {check["scope"]: check for check in checks}
    assert by_scope["source_availability"]["result"] == "pass"
    assert by_scope["entity_identity"]["result"] == "pass"
    assert by_scope["entity_identity"]["method"] == "literal_match"
    assert by_scope["entity_identity"]["evidence"][0]["excerpt"].startswith("午餐推荐")
    assert by_scope["substantive_claim"]["result"] == "not_checked"


def test_verify_items_records_partial_source_availability():
    items = [
        {
            "target_key": "lifestyle:food:day:1:item:0",
            "label": "food.day1",
            "claimed_entities": ["叶婆婆钵钵鸡"],
            "sources": [
                {"platform": "bilibili", "chunk_id": "readable"},
                {"platform": "bilibili", "chunk_id": "missing"},
            ],
            "chunk_ids": ["readable", "missing"],
            "content": "推荐叶婆婆钵钵鸡",
        }
    ]
    checks = []

    issues = citation_check._verify_items(
        items,
        {"readable": "叶婆婆钵钵鸡适合午餐。"},
        result_sink=checks,
    )

    assert issues == []
    availability = next(check for check in checks if check["scope"] == "source_availability")
    assert availability["result"] == "inconclusive"
    assert availability["checked_chunk_ids"] == ["readable"]
    assert "missing" in availability["reason"]


def test_verify_lifestyle_citations_persists_all_checker_results(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    project_id = "evidence-checks-test"
    store.init_project(project_id)
    topic_dir = material_dir / "index" / "测试目的地"
    topic_dir.mkdir(parents=True)
    (topic_dir / "chunks.jsonl").write_text(
        json.dumps(
            {"chunk_id": "food-c1", "text": "叶婆婆钵钵鸡适合作为午餐选择。"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "午餐推荐叶婆婆钵钵鸡",
                "claimed_entities": ["叶婆婆钵钵鸡"],
                "sources": [
                    {
                        "platform": "bilibili",
                        "doc_id": "food-doc",
                        "chunk_id": "food-c1",
                    }
                ],
            }
        ],
    )

    assert citation_check.verify_lifestyle_citations(project_id, "测试目的地") == []

    saved = store.read_evidence_checks(project_id)
    assert {check["scope"] for check in saved["lifestyle"]} == {
        "source_availability",
        "entity_identity",
        "substantive_claim",
    }
    assert all(check["target_key"] == "lifestyle:food:day:1:item:0" for check in saved["lifestyle"])


def test_verify_lifestyle_citations_persists_checker_error(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    project_id = "evidence-check-error-test"
    store.init_project(project_id)
    topic_dir = material_dir / "index" / "测试目的地"
    topic_dir.mkdir(parents=True)
    (topic_dir / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "food-c1", "text": "这是一段无关原文。"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "推荐不存在餐厅",
                "claimed_entities": ["不存在餐厅"],
                "sources": [{"platform": "bilibili", "chunk_id": "food-c1"}],
            }
        ],
    )

    class FailingJudge:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        def invoke(self, _prompt):
            raise RuntimeError("judge unavailable")

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: FailingJudge())

    with pytest.raises(RuntimeError, match="judge unavailable"):
        citation_check.verify_lifestyle_citations(project_id, "测试目的地")

    saved = store.read_evidence_checks(project_id)["lifestyle"]
    error = next(check for check in saved if check["result"] == "error")
    assert error["scope"] == "entity_identity"
    assert error["method"] == "alias_resolution"


def test_verify_items_no_chunk_data_without_disclaimer_flags_issue(monkeypatch):
    # docs/ISSUES.md第74条真实发现：topic索引完全不存在（chunk_texts为空dict）时，
    # 之前会无条件return []整体短路——不是"验证通过"，是"完全没检验"，但调用方
    # 把两者一视同仁当成0冲突，导致诚实声明要求和Bilibili自动补充采集机制对这类
    # 冷启动topic全部失效（真实复现：峨眉山场景住宿价格写低近一半、遗漏最新政策
    # 却全程零打回）。修复后：没有诚实声明必须报成问题，不能再静默放行。
    def _fail_if_called(temperature=0):
        raise AssertionError("chunk_texts整体为空时不应该调用judge——没有原文可供LLM参考")

    monkeypatch.setattr(citation_check, "get_chat_model", _fail_if_called)

    items = [{"label": "x", "claimed_entities": ["某某"], "chunk_ids": ["c1"], "content": "..."}]
    issues = citation_check._verify_items(items, {})
    assert len(issues) == 1
    assert issues[0]["label"] == "x"
    assert issues[0]["missing_entities"] == ["某某"]


def test_verify_items_no_chunk_data_with_disclaimer_passes(monkeypatch):
    # 配对用例：topic索引完全不存在，但content老实声明了是凭经验给出的建议——
    # 视为"老实承认没依据"，跳过，不判定为问题（跟item级留空claimed_entities的
    # 诚实声明判据完全对称）。
    def _fail_if_called(temperature=0):
        raise AssertionError("chunk_texts整体为空时不应该调用judge")

    monkeypatch.setattr(citation_check, "get_chat_model", _fail_if_called)

    items = [
        {
            "label": "x",
            "claimed_entities": ["某某"],
            "chunk_ids": ["c1"],
            "content": "素材库未提供相关信息，凭一般经验推荐。",
        }
    ]
    issues = citation_check._verify_items(items, {})
    assert issues == []


def test_verify_items_with_honest_disclaimer_passes_without_citation():
    # 没有具体专有名词/没标来源，但content里老实声明了"根据一般经验推荐"——
    # 视为诚实、跳过核验，不判定为问题
    chunk_texts = {"c1": "一些内容"}
    items = [
        {"label": "no-entities-honest", "claimed_entities": [], "chunk_ids": ["c1"],
         "content": "检索结果没有提供具体信息，根据一般经验推荐选择市中心住宿。"},
        {"label": "no-chunks-honest", "claimed_entities": ["某某"], "chunk_ids": [],
         "content": "素材未提供相关信息，凭一般经验推荐。"},
    ]
    issues = citation_check._verify_items(items, chunk_texts)
    assert issues == []


def test_verify_items_flags_empty_citation_without_disclaimer():
    # 复现真实案例（见docs/ISSUES.md）：claimed_entities/sources留空、content里也没有
    # 任何诚实声明——之前会被静默放行（核验被短路），现在必须报成问题
    chunk_texts = {"c1": "一些内容"}
    items = [
        {"label": "no-entities-no-disclaimer", "claimed_entities": [], "chunk_ids": ["c1"],
         "content": "推荐去市二医院附近住，交通方便，靠近商圈。"},
    ]
    issues = citation_check._verify_items(items, chunk_texts)
    assert len(issues) == 1
    assert issues[0]["label"] == "no-entities-no-disclaimer"
    assert "没有标注" in issues[0]["reason"] or "诚实声明" in issues[0]["reason"]


def test_verify_items_entity_match_but_fabricated_verifiable_fact_triggers_content_judge(monkeypatch):
    """结构性缺口的回归测试（CLAUDE.md已知问题第4条/docs/ISSUES.md第35条已指出、
    这里正式补上）：claimed_entities字面核对全部通过时，_verify_items此前会直接
    continue，完全跳过对内容里其它"可验证具体事实"（分钟数/价格/线路号等）的核查
    ——哪怕这些事实是编的，只要模型提到的地名/店名本身真实存在，就永远不会被任何
    档位看到。这里用一个claimed_entities能字面对上、但content里"建议停留45分钟"
    是原文完全没提到的场景，验证修复后这类情况会被路由到档位C重新核实，而不是
    像entity检查通过时那样零成本直接放行。
    """
    chunk_texts = {"c1": "武侯祠是三国蜀汉丞相祠堂，很值得一逛"}
    items = [
        {
            "label": "fabricated-duration",
            "claimed_entities": ["武侯祠"],  # 字面核对能通过
            "chunk_ids": ["c1"],
            "content": "武侯祠：建议停留45分钟，够细看完整个祠堂",  # 时长是编的
        }
    ]

    captured_content_prompts = []

    class _FakeContentJudge:
        def invoke(self, prompt):
            captured_content_prompts.append(prompt)
            return citation_check.CitationVerdict(supported=False, reason="原文没有提到任何具体停留时长")

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            # entity都对上了，不应该走档位B（别名消解），应该直接进档位C
            assert schema is citation_check.CitationVerdict, "不应该触发档位B别名消解"
            return _FakeContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda temperature=0, max_tokens=None: _FakeModel())

    issues = citation_check._verify_items(items, chunk_texts)

    assert captured_content_prompts, "entity都对上但content含可验证具体事实时，档位C应该被触发"
    assert "45分钟" in captured_content_prompts[0]
    assert len(issues) == 1
    assert issues[0]["label"] == "fabricated-duration"


def test_verify_items_forced_qualitative_claim_does_not_require_numbers(monkeypatch):
    """定性direct claim仍做实质核验，但不能因为没有数字就自动判成不支持。"""
    captured = []

    class _Judge:
        def invoke(self, prompt):
            captured.append(prompt)
            return citation_check.CitationVerdict(
                supported=True,
                reason="原文明确支持工业遗址氛围这一具体定性描述",
            )

    class _Model:
        def with_structured_output(self, schema, method=None):
            assert schema is citation_check.CitationVerdict
            return _Judge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    issues = citation_check._verify_items(
        [
            {
                "label": "qualitative-highlight",
                "claimed_entities": ["东郊记忆"],
                "chunk_ids": ["c1"],
                "content": "东郊记忆保留了工业遗址氛围，适合慢逛。",
                "_force_substantive_check": True,
            }
        ],
        {"c1": "东郊记忆由老工业厂区改造，保留了工业遗存和厂房风貌。"},
    )

    assert issues == []
    assert captured
    assert "不要求内容或原文必须包含数字" in captured[0]
    assert "如果原文根本没有提到任何相关的具体数字" not in captured[0]


@pytest.mark.parametrize("support_mode", ["direct", "amap"])
def test_evidence_claim_entity_failure_is_not_overridden_by_substantive_pass(
    monkeypatch, support_mode
):
    """V7回归：文字几乎一致也不能把另一POI的证据当成当前实体依据。"""
    item = {
        "target_type": "poi_detail_claim",
        "target_key": f"poi_detail:水上雅丹:claim:{support_mode}",
        "label": "水上雅丹事实",
        "claimed_entities": ["乌素特水上雅丹地质公园"],
        "sources": [{"platform": "amap" if support_mode == "amap" else "bilibili", "chunk_id": "c1"}],
        "chunk_ids": ["c1"],
        "_force_substantive_check": True,
        "_disable_sibling_expansion": True,
        "content": "景区特别大，建议坐接驳车到终点站。",
        "extra": {
            "claim_id": f"claim-{support_mode}",
            "support_mode": support_mode,
            "target_path": "highlight",
            "poi_name": "水上雅丹",
        },
    }

    class _AliasJudge:
        def invoke(self, _prompt):
            return citation_check.AliasResolution(
                resolved=[],
                still_missing=["乌素特水上雅丹地质公园"],
                reason="原文的接驳车和终点站属于翡翠湖，不是水上雅丹。",
            )

    class _ContentJudge:
        def invoke(self, _prompt):
            return citation_check.CitationVerdict(
                supported=True,
                reason="原文字面支持景区很大并建议坐接驳车。",
            )

    class _Model:
        def with_structured_output(self, schema, method=None):
            return _AliasJudge() if schema is citation_check.AliasResolution else _ContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    checks = []

    issues = citation_check._verify_evidence_claim_items(
        [item], {"c1": "翡翠湖景区特别大，建议坐接驳车到终点站。"}, checks, allow_model_calls=True
    )

    assert len(issues) == 1
    assert issues[0]["missing_entities"] == ["乌素特水上雅丹地质公园"]
    assert "正文文字相似" in issues[0]["reason"]
    assert next(check for check in checks if check["scope"] == "entity_identity")["result"] == "fail"
    assert next(check for check in checks if check["scope"] == "substantive_claim")["result"] == "pass"


def test_direct_claim_asr_alias_needs_independent_name_evidence(monkeypatch):
    item = {
        "target_type": "poi_detail_claim",
        "target_key": "poi_detail:茶卡:claim:asr-only",
        "label": "茶卡霞站体验",
        "claimed_entities": ["茶卡霞站"],
        "sources": [{"platform": "douyin_asr", "chunk_id": "asr-c1"}],
        "chunk_ids": ["asr-c1"],
        "_force_substantive_check": True,
        "_disable_sibling_expansion": True,
        "content": "茶卡霞站附近的栈道适合看晚霞。",
        "extra": {
            "claim_id": "claim-asr-only",
            "support_mode": "direct",
            "target_path": "highlight",
        },
    }

    class _AliasJudge:
        def invoke(self, _prompt):
            return citation_check.AliasResolution(
                resolved=["茶卡霞站"],
                still_missing=[],
                reason="狭站可能是霞站的ASR同音转写。",
            )

    class _ContentJudge:
        def invoke(self, _prompt):
            return citation_check.CitationVerdict(
                supported=True,
                reason="原文支持栈道和晚霞体验。",
            )

    class _Model:
        def with_structured_output(self, schema, method=None):
            return _AliasJudge() if schema is citation_check.AliasResolution else _ContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    checks = []

    issues = citation_check._verify_evidence_claim_items(
        [item], {"asr-c1": "茶卡狭站附近有栈道，也是看日落晚霞的好地方。"}, checks, allow_model_calls=True
    )

    assert len(issues) == 1
    identity = next(check for check in checks if check["scope"] == "entity_identity")
    assert identity["result"] == "fail"
    assert identity["missing_entities"] == ["茶卡霞站"]
    assert "缺少非 ASR 独立证据" in identity["reason"]


def test_direct_claim_asr_alias_is_not_unblocked_by_unrelated_non_asr_source(
    monkeypatch,
):
    item = {
        "target_type": "poi_detail_claim",
        "target_key": "poi_detail:茶卡:claim:asr-plus-unrelated",
        "label": "茶卡霞站体验",
        "claimed_entities": ["茶卡霞站"],
        "sources": [
            {"platform": "douyin_asr", "chunk_id": "asr-c1"},
            {"platform": "official", "chunk_id": "official-unrelated-c1"},
        ],
        "chunk_ids": ["asr-c1", "official-unrelated-c1"],
        "_force_substantive_check": True,
        "_disable_sibling_expansion": True,
        "content": "茶卡霞站附近的栈道适合看晚霞。",
        "extra": {
            "claim_id": "claim-asr-plus-unrelated",
            "support_mode": "direct",
            "target_path": "highlight",
        },
    }

    class _AliasJudge:
        def invoke(self, _prompt):
            return citation_check.AliasResolution(
                resolved=["茶卡霞站"],
                still_missing=[],
                reason="狭站可能是霞站的ASR同音转写。",
            )

    class _ContentJudge:
        def invoke(self, _prompt):
            return citation_check.CitationVerdict(
                supported=True,
                reason="ASR原文支持栈道和晚霞体验。",
            )

    class _Model:
        def with_structured_output(self, schema, method=None):
            return _AliasJudge() if schema is citation_check.AliasResolution else _ContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    checks = []

    issues = citation_check._verify_evidence_claim_items(
        [item],
        {
            "asr-c1": "茶卡狭站附近有栈道，也是看日落晚霞的好地方。",
            "official-unrelated-c1": "茶卡盐湖景区今日正常开放，请从东门入园。",
        },
        checks,
        allow_model_calls=True,
    )

    assert len(issues) == 1
    identity = next(check for check in checks if check["scope"] == "entity_identity")
    assert identity["result"] == "fail"
    assert identity["missing_entities"] == ["茶卡霞站"]
    assert "未在任何非 ASR 引用原文中得到独立支持" in identity["reason"]


def test_direct_claim_asr_experience_with_independent_canonical_name_passes(monkeypatch):
    item = {
        "target_type": "poi_detail_claim",
        "target_key": "poi_detail:茶卡:claim:asr-plus-official",
        "label": "茶卡霞站体验",
        "claimed_entities": ["茶卡霞站"],
        "sources": [
            {"platform": "douyin_asr", "chunk_id": "asr-c1"},
            {"platform": "official", "chunk_id": "official-c1"},
        ],
        "chunk_ids": ["asr-c1", "official-c1"],
        "_force_substantive_check": True,
        "_disable_sibling_expansion": True,
        "content": "茶卡霞站附近的栈道适合看晚霞。",
        "extra": {
            "claim_id": "claim-asr-plus-official",
            "support_mode": "direct",
            "target_path": "highlight",
        },
    }

    class _ContentJudge:
        def invoke(self, _prompt):
            return citation_check.CitationVerdict(
                supported=True,
                reason="官方证据确认规范站名，ASR原文支持栈道和晚霞体验。",
            )

    class _Model:
        def with_structured_output(self, schema, method=None):
            assert schema is citation_check.CitationVerdict
            return _ContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    checks = []

    issues = citation_check._verify_evidence_claim_items(
        [item],
        {
            "asr-c1": "茶卡狭站附近有栈道，也是看日落晚霞的好地方。",
            "official-c1": "茶卡盐湖景区官方导览标注的站点名为茶卡霞站。",
        },
        checks,
        allow_model_calls=True,
    )

    assert issues == []
    assert next(check for check in checks if check["scope"] == "source_availability")["result"] == "pass"
    assert next(check for check in checks if check["scope"] == "entity_identity")["result"] == "pass"
    assert next(check for check in checks if check["scope"] == "substantive_claim")["result"] == "pass"


def test_direct_claim_partial_unreadable_evidence_is_not_deliverable(monkeypatch):
    item = {
        "target_type": "lifestyle_claim",
        "target_key": "lifestyle:play:day:1:claim:partial",
        "label": "play.day1事实",
        "claimed_entities": ["东郊记忆"],
        "sources": [
            {"platform": "bilibili", "chunk_id": "readable"},
            {"platform": "bilibili", "chunk_id": "missing"},
        ],
        "chunk_ids": ["readable", "missing"],
        "_force_substantive_check": True,
        "_disable_sibling_expansion": True,
        "content": "东郊记忆保留了工业遗址氛围。",
        "extra": {
            "claim_id": "claim-partial",
            "support_mode": "direct",
            "target_path": "recommendation",
        },
    }

    class _ContentJudge:
        def invoke(self, _prompt):
            return citation_check.CitationVerdict(supported=True, reason="可读原文支持该事实。")

    class _Model:
        def with_structured_output(self, schema, method=None):
            assert schema is citation_check.CitationVerdict
            return _ContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: _Model())
    checks = []

    issues = citation_check._verify_evidence_claim_items(
        [item], {"readable": "东郊记忆保留了老厂房和工业遗存。"}, checks, allow_model_calls=True
    )

    assert len(issues) == 1
    assert "证据可用性未通过" in issues[0]["reason"]
    assert next(check for check in checks if check["scope"] == "source_availability")["result"] == "inconclusive"


def test_verify_items_entity_match_no_verifiable_fact_still_zero_cost(monkeypatch):
    """回归防护：新增的"可验证具体事实"触发路径不应该让原有的零成本场景意外调用LLM——
    content里没有数字化的可验证事实模式时，entity字面核对通过就应该直接放行，
    不触发档位B/C（跟test_verify_items_literal_match_passes_without_llm_call同一诉求，
    这里额外确认get_chat_model真的一次都没被调用，而不是只看返回结果）。
    """
    def _fail_if_called(temperature=0):
        raise AssertionError("不应该调用get_chat_model——entity对上且没有可验证事实模式")

    monkeypatch.setattr(citation_check, "get_chat_model", _fail_if_called)

    chunk_texts = {"c1": "午餐推荐叶婆婆钵钵鸡，好吃不辣"}
    items = [
        {
            "label": "test-pass",
            "claimed_entities": ["叶婆婆钵钵鸡"],
            "chunk_ids": ["c1"],
            "content": "推荐叶婆婆钵钵鸡，味道很好",
        }
    ]
    issues = citation_check._verify_items(items, chunk_texts)
    assert issues == []


def test_verify_items_resolves_bare_chunk_id_missing_topic_prefix():
    """docs/ISSUES.md第116条防御性兜底（方向A）：即使某条历史数据/未覆盖到的写入路径
    仍然带着裸格式chunk_id（丢失topic前缀，如"144-c004"而不是"青甘大环线-144-c004"）
    流入核验，_verify_items也应该能靠后缀反查index命中真实原文，不应该因为chunk_id
    对不上就把这条本来有真实依据的内容误判为"原文片段为空"。

    真实数据来源：trace_full.jsonl第439行（call idx 438）lifestyle_agent第3轮打回重写，
    这条item的claimed_entities/recommendation跟真实撞见的裸格式chunk_id一致。
    """
    chunk_texts = {"青甘大环线-144-c004": "沙洲夜市有沙葱牛肉饼、甜胚子豆花等网红摊位"}
    items = [
        {
            "label": "food.day4",
            "claimed_entities": ["沙洲夜市", "沙葱牛肉饼", "甜胚子豆花"],
            "chunk_ids": ["144-c004"],  # 裸格式，丢失了"青甘大环线-"前缀
            "content": "Day4晚餐去沙洲夜市，推荐沙葱牛肉饼、甜胚子豆花",
        }
    ]
    issues = citation_check._verify_items(items, chunk_texts)
    assert issues == []


def test_verify_items_bare_chunk_id_still_flags_real_mismatch(monkeypatch):
    """兜底不能变成"逢裸格式就放行"——后缀反查命中的是真实chunk，但内容跟claimed_entities
    对不上时，仍然应该走正常的档位B/C核验流程判定为缺乏依据，不能因为新增了归一化就
    放松了真正的核验。"""
    chunk_texts = {"青甘大环线-144-c004": "完全不相关的内容，跟claimed_entities对不上"}
    items = [
        {
            "label": "food.day4",
            "claimed_entities": ["完全不存在的店名"],
            "chunk_ids": ["144-c004"],
            "content": "推荐完全不存在的店名",
        }
    ]

    class _FakeAliasJudge:
        def invoke(self, prompt):
            return citation_check.AliasResolution(
                resolved=[], still_missing=["完全不存在的店名"], reason="原文完全没有提到"
            )

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeAliasJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda temperature=0, max_tokens=None: _FakeModel())

    issues = citation_check._verify_items(items, chunk_texts, entity_identity_only=True)
    assert len(issues) == 1
    assert issues[0]["label"] == "food.day4"


def test_alias_resolution_overlap_uses_resolved_as_positive_verdict(monkeypatch):
    chunk_texts = {"doc-c001": "推荐游览成都国际金融中心"}
    items = [
        {
            "label": "poi.route.1",
            "claimed_entities": ["IFS", "北门"],
            "chunk_ids": ["doc-c001"],
            "content": "从北门进入，前往IFS",
        }
    ]

    class _FakeAliasJudge:
        def invoke(self, prompt):
            return citation_check.AliasResolution(
                resolved=["IFS", "额外实体"],
                still_missing=["IFS", "北门"],
                reason="IFS是成都国际金融中心的简称，原文没有北门",
            )

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeAliasJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **kwargs: _FakeModel())
    results = []
    issues = citation_check._verify_items(
        items,
        chunk_texts,
        entity_identity_only=True,
        result_sink=results,
    )

    assert len(issues) == 1
    assert issues[0]["missing_entities"] == ["北门"]
    alias_result = next(item for item in results if item["method"] == "alias_resolution")
    assert alias_result["missing_entities"] == ["北门"]
    assert "结构矛盾已确定性归并" in alias_result["reason"]


def test_alias_resolution_prompt_uses_same_referent_not_exact_compound_phrase():
    prompt = citation_check._build_alias_resolution_prompt(
        ["七彩丹霞北门观光车顺序", "悬壁长城"],
        "北门观光车按2→1→5→4游览，随后到悬臂长城。",
    )

    assert "只做指代身份消歧" in prompt
    assert "不要求输入的完整字符串以连续字面形式出现" in prompt
    assert "后续实质核验" in prompt
    assert "接驳车/观光车" in prompt
    assert "不同入口、不同景点、不同城市" in prompt


def test_alias_resolution_keeps_true_identity_difference_missing(monkeypatch):
    items = [
        {
            "label": "poi.route.1",
            "claimed_entities": ["七彩丹霞北门"],
            "chunk_ids": ["doc-c001"],
            "content": "从七彩丹霞北门进入",
        }
    ]
    prompts = []

    class _FakeAliasJudge:
        def invoke(self, prompt):
            prompts.append(prompt)
            return citation_check.AliasResolution(
                resolved=[],
                still_missing=["七彩丹霞北门"],
                reason="原文只说西门，属于不同入口。",
            )

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeAliasJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **kwargs: _FakeModel())

    issues = citation_check._verify_items(
        items,
        {"doc-c001": "从七彩丹霞西门进入景区。"},
        entity_identity_only=True,
    )

    assert len(issues) == 1
    assert issues[0]["missing_entities"] == ["七彩丹霞北门"]
    assert prompts and "不同入口" in prompts[0]


def test_verify_items_bare_chunk_id_with_no_matching_suffix_in_index(monkeypatch):
    """裸格式chunk_id在index里完全找不到对应后缀（既不是历史数据问题，也不是真实chunk）
    时，应该跟修复前一样查无原文，不应该抛异常或误报命中。"""
    chunk_texts = {"青甘大环线-144-c004": "沙洲夜市相关内容"}
    items = [
        {
            "label": "food.day5",
            "claimed_entities": ["某某"],
            "chunk_ids": ["999-c999"],  # 裸格式，但index里没有任何chunk_id以这个后缀结尾
            "content": "推荐某某",
        }
    ]

    class _FakeAliasJudge:
        def invoke(self, prompt):
            return citation_check.AliasResolution(resolved=[], still_missing=["某某"], reason="原文没有提到")

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeAliasJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda temperature=0, max_tokens=None: _FakeModel())

    issues = citation_check._verify_items(items, chunk_texts, entity_identity_only=True)
    assert len(issues) == 1


def test_verify_route_citations_flags_issue_without_chunk_data_or_disclaimer(tmp_path, monkeypatch):
    # docs/ISSUES.md第74条：route_plan.json有claimed_entities（POI名）但素材库完全没有
    # chunks.jsonl、notes里也没有诚实声明时，修复后应该报问题，不能再静默放行
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "route-citation-test"
    store.init_project(project_id)

    from src.workspace.schema import RouteDay, RoutePlan, SourceRef

    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                pois=[{"name": "太古里"}],
                sources=[SourceRef(platform="bilibili", doc_id="d1", chunk_id="d1-c001")],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    issues = citation_check.verify_route_citations(project_id, "测试目的地")
    assert len(issues) == 1
    assert issues[0]["day"] == 1
    assert "太古里" in issues[0]["missing_entities"]


def test_verify_route_citations_passes_without_chunk_data_when_disclaimed(tmp_path, monkeypatch):
    # 配对用例：同样没有chunks.jsonl，但notes里老实声明了没有素材依据——应该放行
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "route-citation-test-disclaimed"
    store.init_project(project_id)

    from src.workspace.schema import RouteDay, RoutePlan, SourceRef

    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                pois=[{"name": "太古里"}],
                notes="素材库未提供相关信息，凭一般经验推荐前往。",
                sources=[SourceRef(platform="bilibili", doc_id="d1", chunk_id="d1-c001")],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    issues = citation_check.verify_route_citations(project_id, "测试目的地")
    assert issues == []


def test_verify_route_citations_persists_manual_claim_check_without_null(tmp_path, monkeypatch):
    """Route的estimate/user_constraint Claim必须生成结构化检查，不能向checks塞入None。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "route-manual-claim-check-test"
    store.init_project(project_id)

    from src.workspace.schema import RouteClaim, RouteDay, RoutePlan

    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="峨眉山市",
                notes="素材库未提供相关信息，凭一般经验推荐本日节奏。",
                claims=[
                    RouteClaim(
                        claim_id="route-manual-1",
                        target_path="notes",
                        claim_text="本日按用户要求减少长时间台阶",
                        support_mode="user_constraint",
                    )
                ],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    assert citation_check.verify_route_citations(project_id, "测试目的地") == []
    checks = store.read_evidence_checks(project_id)["route"]
    manual = next(item for item in checks if item.get("claim_id") == "route-manual-1")
    assert manual["target_type"] == "route_claim"
    assert manual["method"] == "user_constraint"
    assert manual["result"] == "not_checked"
    assert all(isinstance(item, dict) for item in checks)


def test_verify_route_citations_uses_claim_contract_instead_of_legacy_day_union(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "fixed-route-user-constraint-citation-test"
    store.init_project(project_id)

    from src.workspace.schema import RouteClaim, RouteDay, RoutePlan, RoutePoi

    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="德令哈",
                area="兰州西站 → 青海湖 → 德令哈",
                pois=[RoutePoi(name="青海湖")],
                claims=[
                    RouteClaim(
                        claim_id="route-fixed-1",
                        target_path="pois.0.name",
                        subject="青海湖",
                        claim_text="青海湖",
                        claim_type="poi_identity",
                        support_mode="user_constraint",
                        claimed_entities=["青海湖"],
                    )
                ],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    assert citation_check.verify_route_citations(project_id, "青甘大环线") == []
    checks = store.read_evidence_checks(project_id)["route"]
    assert not any(item.get("target_type") == "route_day" for item in checks)
    assert checks[0]["method"] == "user_constraint"


def test_verify_route_citations_persists_exact_input_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "route-fingerprint-test"
    store.init_project(project_id)

    from src.workspace.schema import (
        EvidenceClaimEvidenceSnapshot,
        RouteClaim,
        RouteDay,
        RoutePlan,
        SourceRef,
    )

    source = SourceRef(
        platform="project_evidence",
        doc_id="official-poi",
        chunk_id="project-evidence-official-poi",
        source_url="https://example.com/poi",
        scope="project",
    )
    store.write_route_plan(
        project_id,
        actor="route_agent",
        route_plan=RoutePlan(
            days=[
                RouteDay(
                    day=1,
                    city="成都",
                    claims=[
                        RouteClaim(
                            claim_id="route-current-poi",
                            target_path="pois.0.name",
                            subject="文殊院",
                            claim_text="文殊院",
                            claim_type="poi_identity",
                            support_mode="direct",
                            claimed_entities=["文殊院"],
                            evidence_refs=[source],
                            evidence_snapshots=[
                                EvidenceClaimEvidenceSnapshot(
                                    source=source,
                                    excerpt="文殊院位于成都市青羊区。",
                                    observed_at="2026-09-17T00:00:00Z",
                                    source_kind="project_web",
                                )
                            ],
                        )
                    ],
                )
            ]
        ),
    )

    assert citation_check.verify_route_citations(
        project_id,
        "成都",
        allow_model_calls=False,
    ) == []
    checks = [
        item
        for item in store.read_evidence_checks(project_id)["route"]
        if item.get("claim_id") == "route-current-poi"
    ]
    assert {item["scope"] for item in checks} == {
        "source_availability",
        "entity_identity",
        "substantive_claim",
    }
    fingerprints = {item.get("verification_fingerprint") for item in checks}
    assert len(fingerprints) == 1
    assert next(iter(fingerprints))


def test_has_honesty_disclaimer_matches_real_variants_seen_in_erhai_cycling_eval():
    """docs/ISSUES.md第76条：模型每次被打回后重新生成的诚实声明措辞都不完全一样，
    精确字符串白名单（_DISCLAIMER_MARKERS）追不上，连续在同一次评测里误伤3次。
    这三句都是洱海骑行评测里真实出现过的原文，_has_honesty_disclaimer必须都命中。"""
    real_variants = [
        "【诚实声明】以下美食建议基于大理旅游常识和经验给出，素材库中未检索到任何支持性素材，无具体餐厅名/店名可引用。",
        "【诚实声明】本日所有POI均为凭自身常识经验安排，素材库中未检索到相关支撑素材，行程合理性基于公开信息和大理旅游常识。",
        "【诚实声明】素材库中（含精确检索11个大理相关名词）均无相关可用素材，本条路线安排完全基于自身常识撰写，sources字段为空。",
    ]
    for content in real_variants:
        assert citation_check._has_honesty_disclaimer(content), f"未命中: {content}"


def test_has_honesty_disclaimer_matches_gansu_museum_eval_variant():
    """docs/ISSUES.md第125条真实复现（青甘大环线8天E2E，山丹军马场）：poi_detail_agent写的是
    "素材未给出固定机位"——"未"后面接的是具体名词"固定机位"而不是词表里的"素材/依据/信息/
    chunk/原文"这类抽象词，此前的正则漏检，导致一条已经诚实声明的内容被误判成"完全没有
    标注sources/claimed_entities，也没有诚实声明"。"""
    content = (
        "花海带具体点位与季节情况属于经验性建议，素材未给出固定机位，建议顺路看到合适草甸再停。"
    )
    assert citation_check._has_honesty_disclaimer(content)


def test_has_honesty_disclaimer_matches_emeishan_eval_variant():
    """docs/ISSUES.md第76条同类问题的第4个变体（峨眉山评测真实复现）：route_agent写的是
    "素材库中无直接支撑原文"——"支撑"后紧跟"原文"而不是词表里的"素材/依据/信息/chunk"，
    此前的正则漏检，导致route_agent连续3次被误判打回耗尽force_finalize。"""
    content = (
        "本日POI安排基于个人经验与通用地理常识（峨眉山为著名景区，报国寺/伏虎寺为其山脚标准景点），"
        "素材库中无直接支撑原文，故sources留空。"
    )
    assert citation_check._has_honesty_disclaimer(content)


def test_has_honesty_disclaimer_does_not_match_normal_content_without_disclaimer():
    """正则兜底不能把正常的、包含具体地名/店名的推荐内容误判成诚实声明——
    否则会反过来削弱核验机制本身（本该被打回的编造内容被误放行）。"""
    normal_content = [
        "这条路线经过大理古城和喜洲古镇，沿途风景优美，适合拍照打卡。",
        "推荐尝试洱海酸辣鱼和喜洲破酥粑粑，风味独特，本地人也常吃。",
        "武侯祠是三国文化的重要遗迹，红墙夹道适合拍照，值得游览。",
    ]
    for content in normal_content:
        assert not citation_check._has_honesty_disclaimer(content), f"误判为诚实声明: {content}"


def test_extract_poi_names_strips_descriptor_and_splits_slash():
    # 核心行为：地名后面括号里的修饰性描述要剥离掉，不参与核验；
    # 用"/"、"、"并列的多个地名要拆开，分别作为独立的claimed_entity
    assert citation_check._extract_poi_names("武侯祠博物馆（三国文化、红墙夹道拍照）") == ["武侯祠博物馆"]
    assert citation_check._extract_poi_names("安顺廊桥/九眼桥（夜景拍照）") == ["安顺廊桥", "九眼桥"]
    assert citation_check._extract_poi_names("太古里") == ["太古里"]  # 没有括号也要正常处理
    assert citation_check._extract_poi_names("宽窄巷子(拍照打卡)") == ["宽窄巷子"]  # 半角括号也要支持


def test_extract_poi_names_splits_hyphen_separated_pois():
    # 复现真实案例（chengdu-3d-e2e-v13）：route_agent用"-"而不是"/"、"、"做并列分隔符
    # （"望平街-安顺廊桥（夜景）"），修复前整串被当成一个地名，两个词都在原文里
    # 却因为写法不同永远匹配不上，被误判为缺乏依据
    assert citation_check._extract_poi_names("望平街-安顺廊桥（夜景）") == ["望平街", "安顺廊桥"]


def test_extract_poi_names_splits_middle_dot_separated_pois():
    # 复现都江堰真实数据："灌县古城·南桥"是两个地点的组合写法，引用原文分别
    # 提到"灌县古城"和"南桥"时应当能确定性通过，不应进入模型别名判断。
    assert citation_check._extract_poi_names("灌县古城·南桥") == ["灌县古城", "南桥"]


def test_entity_matches_strips_common_landmark_suffix():
    # 复现并验证修复：素材原文常用口语简称（"武侯祠"），route_agent写规范全称（"武侯祠博物馆"），
    # 剥离常见地标后缀后应该能匹配上，不应该被判定为"缺乏依据"
    assert citation_check._entity_matches("武侯祠博物馆", "武侯祠是三国文化的代表") is True
    assert citation_check._entity_matches("锦里古街", "旁边就是锦里，晚上亮灯拍照出片") is True
    assert citation_check._entity_matches("太古里", "太古里步行街的核心区") is True  # 本来就能直接匹配
    # 词表外的、真正缺乏依据的地名不应该被误放行
    assert citation_check._entity_matches("完全不存在的公园", "武侯祠是三国文化的代表") is False
    # 后缀剥离后核心词太短（比如只剩1个字）不应该放宽匹配，避免false positive
    assert citation_check._entity_matches("寺公园", "这是一个寺") is False


def test_entity_matches_normalizes_traditional_chinese_for_comparison_only():
    evidence = "察爾汗鹽湖的萬丈鹽橋橫跨湖面32公里"

    assert citation_check._entity_matches("察尔汗盐湖", evidence) is True
    assert citation_check._entity_matches("万丈盐桥", evidence) is True
    assert citation_check._entity_matches("七彩丹霞", evidence) is False


def test_verify_route_citations_ignores_unsupported_descriptor_but_requires_real_poi_name(tmp_path, monkeypatch):
    # 复现并验证修复：真实运行中"武侯祠博物馆（三国文化、红墙夹道拍照）"这种POI，
    # 素材原文只提到了地名本身、没有逐字提到"红墙夹道拍照"这个修饰语，之前会被误判为
    # 缺乏依据打回。修复后只核验地名，这条不应该再被判定为问题（且不需要调用LLM，
    # 因为地名能在档位A字面匹配上，不会进入档位B）。
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", material_dir)
    project_id = "route-citation-descriptor-test"
    store.init_project(project_id)

    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps(
            {"chunk_id": "d1-c001", "text": "武侯祠是如果你非常喜欢三国文化，可以去逛一下，安顺廊桥晚上比较好看，九眼桥有酒吧一条街"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    from src.workspace.schema import RouteDay, RoutePlan, SourceRef

    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                pois=[{"name": "武侯祠（三国文化、红墙夹道拍照）"}, {"name": "安顺廊桥/九眼桥（夜景拍照）"}],
                sources=[SourceRef(platform="bilibili", doc_id="d1", chunk_id="d1-c001")],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    issues = citation_check.verify_route_citations(project_id, "测试目的地")
    assert issues == []


def test_verify_route_citations_days_param_scopes_verification(tmp_path, monkeypatch):
    """edit模式定点编辑（route_draft_node）用的核验范围收窄——days=[2]时只核验day2，
    day1即使有真实问题也应该被跳过，见docs/ISSUES.md第46条。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", material_dir)
    project_id = "route-citation-days-scope-test"
    store.init_project(project_id)

    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "武侯祠是三国文化的代表景点"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    from src.workspace.schema import RouteDay, RoutePlan, SourceRef

    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                pois=[{"name": "完全不存在于原文的地名"}],
                sources=[SourceRef(platform="bilibili", doc_id="d1", chunk_id="d1-c001")],
            ),
            RouteDay(
                day=2,
                city="成都",
                pois=[{"name": "武侯祠"}],
                sources=[SourceRef(platform="bilibili", doc_id="d1", chunk_id="d1-c001")],
            ),
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    # 不传days（全量核验）：day1的问题应该被发现
    all_issues = citation_check.verify_route_citations(project_id, "测试目的地")
    assert any(i["day"] == 1 for i in all_issues)

    # 只核验day2：day1的问题应该被跳过，day2本身没有问题
    scoped_issues = citation_check.verify_route_citations(project_id, "测试目的地", days=[2])
    assert scoped_issues == []


def test_verify_route_citations_web_verified_source_passes_with_matching_log(tmp_path, monkeypatch):
    """docs/ISSUES.md第115条方向C的核心回归：sources标platform=="web_verified"的POI，
    chunk_id是合成字符串，chunks.jsonl里查不到——但这次生成过程中真实调用过web_search
    且日志里有支持这个地名的内容时，应该通过核验，而不是像修复前那样因为
    combined_text为空被判定"未提供原文片段"。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "route-web-verified-test"
    store.init_project(project_id)

    from src.workspace.schema import RouteDay, RoutePlan, SourceRef

    plan = RoutePlan(
        days=[
            RouteDay(
                day=2,
                city="海西州",
                area="大柴旦",
                pois=[{"name": "大柴旦翡翠湖"}],
                sources=[SourceRef(platform="web_verified", doc_id="web-1", chunk_id="web-大柴旦翡翠湖-门票开放时间")],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    log_dir = tmp_path / project_id / "agents" / "route_agent"
    log_dir.mkdir(parents=True)
    (log_dir / "web_search_log.jsonl").write_text(
        json.dumps({"query": "大柴旦翡翠湖 门票", "result": "- 大柴旦翡翠湖门票信息：门票70元起（来源：https://example.com）"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    issues = citation_check.verify_route_citations(project_id, "测试目的地")
    assert issues == []


def test_verify_route_citations_web_verified_source_fails_without_log(tmp_path, monkeypatch):
    """配对用例：同样是web_verified来源，但这次生成没有留下web_search_log（比如没配
    Tavily key、或者根本没调用过），应该维持修复前的行为——判定缺乏依据。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "route-web-verified-no-log-test"
    store.init_project(project_id)

    from src.workspace.schema import RouteDay, RoutePlan, SourceRef

    plan = RoutePlan(
        days=[
            RouteDay(
                day=2,
                city="海西州",
                area="大柴旦",
                pois=[{"name": "大柴旦翡翠湖"}],
                sources=[SourceRef(platform="web_verified", doc_id="web-1", chunk_id="web-大柴旦翡翠湖-门票开放时间")],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    issues = citation_check.verify_route_citations(project_id, "测试目的地")
    assert len(issues) == 1
    assert "大柴旦翡翠湖" in issues[0]["missing_entities"]


def test_verify_route_citations_web_verified_only_uses_relevant_log_entries(tmp_path, monkeypatch):
    """docs/ISSUES.md第121条回归：真实撞过的场景——route_draft单节点8天生成过程累计
    十几次web_search，日志文件几百KB，但某一天的sources只要标了1条web_verified来源，
    修复前会把全部记录不加区分地拼成combined_text喂给alias_judge，prompt体积失控
    （真实案例62045 token，撞上商汤relay reasoning截断）。这里验证：只有query命中
    这一天claimed_entities（POI名）的记录才会被拼进combined_text，其余无关天数的
    搜索记录不会被带进来。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "route-web-verified-filter-test"
    store.init_project(project_id)

    from src.workspace.schema import RouteDay, RoutePlan, SourceRef

    plan = RoutePlan(
        days=[
            RouteDay(
                day=2,
                city="海西州",
                area="大柴旦",
                pois=[{"name": "大柴旦翡翠湖"}],
                sources=[SourceRef(platform="web_verified", doc_id="web-1", chunk_id="web-大柴旦翡翠湖-门票")],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    log_dir = tmp_path / project_id / "agents" / "route_agent"
    log_dir.mkdir(parents=True)
    unrelated_entry = json.dumps(
        {"query": "茶卡盐湖 天空之镜 开放时间", "result": "无关内容" * 2000},
        ensure_ascii=False,
    )
    relevant_entry = json.dumps(
        {"query": "大柴旦翡翠湖 门票 开放时间", "result": "大柴旦翡翠湖：门票70元起（来源：https://example.com）"},
        ensure_ascii=False,
    )
    log_dir_path = log_dir / "web_search_log.jsonl"
    # 混入大量无关天数的搜索记录（模拟真实233KB日志），验证不会被无差别拼进来。
    log_dir_path.write_text("\n".join([unrelated_entry] * 20 + [relevant_entry]) + "\n", encoding="utf-8")

    evidence = citation_check._load_web_search_evidence(
        project_id, "route_agent", claimed_entities=["大柴旦翡翠湖"]
    )
    assert "大柴旦翡翠湖" in evidence
    assert "无关内容" not in evidence

    issues = citation_check.verify_route_citations(project_id, "测试目的地")
    assert issues == []


def test_load_web_search_evidence_without_entity_filter_returns_everything(tmp_path, monkeypatch):
    """向后兼容：claimed_entities不传（默认None）时保留全部记录——poi_detail_agent这个
    调用点故意传filter_by_entities=False，因为它的log_scope本身已经按天+POI精确隔离，
    日志天然就窄，不需要再叠加实体过滤（且claimed_entities是途经点名，跟query用词粒度
    不同，按子串匹配会误伤真实相关证据，见_merge_web_verified_evidence的说明）。"""
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    project_id = "no-filter-test"
    log_dir = tmp_path / project_id / "agents" / "poi_detail_agent/day2_daizhaidan"
    log_dir.mkdir(parents=True)
    (log_dir / "web_search_log.jsonl").write_text(
        json.dumps({"query": "大柴旦翡翠湖 门票", "result": "门票信息A"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"query": "检票入园 流程", "result": "门票信息B"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    evidence = citation_check._load_web_search_evidence(project_id, "poi_detail_agent/day2_daizhaidan")
    assert "门票信息A" in evidence
    assert "门票信息B" in evidence


def test_collect_cited_chunks_dedupes_across_route_and_lifestyle():
    # 复现docs/ISSUES.md第8条的场景：route_plan和lifestyle都引用了同一个chunk_id，
    # collect_cited_chunks应该只出现一次（按首次出现顺序去重），不重复
    route_plan = {
        "days": [
            {"day": 1, "sources": [{"platform": "bilibili", "author": "", "doc_id": "d1", "chunk_id": "d1-c001"}]},
        ]
    }
    lifestyle = {
        "food": {
            "items": [
                {"day": 1, "sources": [{"platform": "bilibili", "author": "", "doc_id": "d1", "chunk_id": "d1-c001"}]},
                {"day": 2, "sources": [{"platform": "bilibili", "author": "X", "doc_id": "d2", "chunk_id": "d2-c003"}]},
            ]
        },
        "outfit": {"items": []},
        "lodging": {"items": []},
        "play": {"items": []},
    }
    cited = citation_check.collect_cited_chunks(route_plan, lifestyle)
    assert list(cited.keys()) == ["d1-c001", "d2-c003"]
    assert cited["d2-c003"]["author"] == "X"
    assert cited["d1-c001"]["cited_in"] == [
        "route:day:1",
        "lifestyle:food:day:1:item:0",
    ]
    assert cited["d2-c003"]["cited_in"] == ["lifestyle:food:day:2:item:1"]


def test_collect_cited_chunks_includes_pitfall_sources():
    # pitfall是可选参数，向后兼容——不传时行为不变（上面那个测试已验证）；
    # 传了之后应该把避雷内容的sources也收进来，跟route/lifestyle同样去重
    pitfall = {
        "food": {"items": [{"day": 1, "sources": [{"platform": "bilibili", "author": "Y", "doc_id": "d3", "chunk_id": "d3-c001"}]}]},
        "outfit": {"items": []},
        "lodging": {"items": []},
        "play": {"items": []},
    }
    cited = citation_check.collect_cited_chunks(None, None, pitfall)
    assert cited == {
        "d3-c001": {
            "platform": "bilibili",
            "author": "Y",
            "doc_id": "d3",
            "cited_in": ["pitfall:food:day:1:item:0"],
        }
    }


def test_collect_cited_chunks_includes_poi_details_sources():
    # 复现用户反馈的场景（final_guide.md的"参考来源"表格看着比实际引用少）：poi_details的
    # sources嵌在routes[].route_segments[].sources这一层，跟route_plan/lifestyle/pitfall
    # 的items[].sources不是同一层级，此前collect_cited_chunks完全没有扫描这个路径。
    poi_details = [
        {
            "poi_name": "青城山",
            "day": 2,
            "routes": [
                {
                    "route_name": "经典线",
                    "recommended": True,
                    "route_segments": [
                        {
                            "point_name": "山门",
                            "sources": [
                                {"platform": "bilibili", "author": "Z", "doc_id": "d4", "chunk_id": "d4-c001"},
                            ],
                        },
                        {
                            "point_name": "天师洞",
                            # 同一个chunk_id被同一个POI内多处引用，应该只出现一次
                            "sources": [
                                {"platform": "bilibili", "author": "Z", "doc_id": "d4", "chunk_id": "d4-c001"},
                            ],
                        },
                    ],
                },
                {
                    "route_name": "小众线",
                    "recommended": False,
                    "route_segments": [
                        {
                            "point_name": "后山",
                            "sources": [
                                {"platform": "xiaohongshu", "author": "", "doc_id": "d5", "chunk_id": "d5-c002"},
                            ],
                        },
                    ],
                },
            ],
        }
    ]
    cited = citation_check.collect_cited_chunks(None, None, None, poi_details)
    assert cited == {
        "d4-c001": {
            "platform": "bilibili",
            "author": "Z",
            "doc_id": "d4",
            "cited_in": [
                "poi_detail:day:2:poi:青城山:route:经典线:order:1",
                "poi_detail:day:2:poi:青城山:route:经典线:order:2",
            ],
        },
        "d5-c002": {
            "platform": "xiaohongshu",
            "author": "",
            "doc_id": "d5",
            "cited_in": [
                "poi_detail:day:2:poi:青城山:route:小众线:order:1",
            ],
        },
    }


def test_collect_cited_chunks_canonicalizes_legacy_web_sources_without_chunk_id():
    """旧项目里platform=web且chunk_id为空时，重建references也不能再生成假警告。"""
    cited = citation_check.collect_cited_chunks(
        None,
        None,
        None,
        [
            {
                "routes": [
                    {
                        "route_segments": [
                            {
                                "sources": [
                                    {
                                        "platform": "web",
                                        "doc_id": "成都-dujiang-dam-都江堰",
                                        "chunk_id": "",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ],
    )
    assert len(cited) == 1
    chunk_id, source = next(iter(cited.items()))
    assert source["platform"] == "web_verified"
    assert chunk_id.startswith("web-")


def test_collect_cited_chunks_separates_generic_web_ids_and_recovers_url_from_doc_id():
    cited = citation_check.collect_cited_chunks(
        {
            "days": [
                {
                    "day": 1,
                    "sources": [
                        {
                            "platform": "web_verified",
                            "doc_id": "https://example.com/a",
                            "chunk_id": "web_verified",
                        },
                        {
                            "platform": "web_verified",
                            "doc_id": "https://example.com/b",
                            "chunk_id": "web_verified",
                        },
                    ],
                }
            ]
        },
        None,
    )

    assert len(cited) == 2
    assert len(set(cited)) == 2
    assert {source["source_url"] for source in cited.values()} == {
        "https://example.com/a",
        "https://example.com/b",
    }


def test_verify_pitfall_citations_returns_empty_without_pitfall_output(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "pitfall-citation-test"
    store.init_project(project_id)

    assert citation_check.verify_pitfall_citations(project_id, "测试目的地") == []


def test_verify_pitfall_citations_skips_legitimate_empty_warning(tmp_path, monkeypatch):
    """核心行为：warning留空是"这条推荐确实没查到避雷内容"的合法结果，不应该被误报成
    "完全没有引用依据"的问题——跟_verify_items原有的honesty_disclaimer判断逻辑要分开处理，
    因为空字符串本身不会命中_DISCLAIMER_MARKERS里的任何一个措辞。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "pitfall-citation-test"
    store.init_project(project_id)

    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [{"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": ""}],
    )

    issues = citation_check.verify_pitfall_citations(project_id, "测试目的地")
    assert issues == []


def test_verify_pitfall_citations_flags_missing_entity(tmp_path, monkeypatch):
    # "卫生问题"字面不在chunk原文里，会触发档位B/C走真实LLM调用（跟test_verify_items_literal_match_passes_without_llm_call
    # 的对照组不同，那个测试特意用完全字面匹配避免LLM调用；这里就是要验证缺失entity真的会被抓出来）
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "pitfall-citation-test"
    store.init_project(project_id)

    topic_index = tmp_path / "material_library" / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "这家店味道一般，人均消费五十元左右"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [
            {
                "field": "food",
                "day": 1,
                "target_recommendation": "叶婆婆钵钵鸡",
                "warning": "有卫生问题投诉",
                "claimed_entities": ["卫生问题"],
                "sources": [{"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}],
            }
        ],
    )

    issues = citation_check.verify_pitfall_citations(project_id, "测试目的地")
    assert len(issues) == 1
    assert issues[0]["field"] == "food"
    assert "卫生问题" in issues[0]["missing_entities"]


def test_verify_pitfall_citations_scopes_to_given_fields(tmp_path, monkeypatch):
    """docs/ISSUES.md第37补条修复：edit模式下citation_check系节点应该只核验这次改动的
    字段，不应该把此前已经核验过、这次没碰过的字段也重新拉出来核验（避免非确定性判断
    误伤未改动内容）。这里用零LLM成本的"完全没标注citation"路径验证scoping本身生效——
    不用故意构造真的缺失引用的实体去触发档位B/C（那需要真实LLM调用，"卫生问题"那个
    测试已经覆盖过那条路径），只需要证明fields筛选前置生效即可。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "pitfall-citation-scope-test"
    store.init_project(project_id)

    topic_index = tmp_path / "material_library" / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "熊猫基地是成都著名景点，可以看熊猫"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [
            # food：完全没有claimed_entities/sources，也没有诚实声明——一定会报问题（零LLM成本路径）
            {"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": "有卫生问题投诉"},
            # play：字面完全匹配，一定通过，不产生问题
            {
                "field": "play",
                "day": 1,
                "target_recommendation": "熊猫基地",
                "warning": "旺季排队时间长",
                "claimed_entities": ["熊猫基地"],
                "sources": [{"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}],
            },
        ],
    )

    # 不传fields（默认全量）：food的问题应该被发现
    assert len(citation_check.verify_pitfall_citations(project_id, "测试目的地")) == 1

    # fields=["food"]：food在范围内，问题依然应该被发现
    issues_food_scope = citation_check.verify_pitfall_citations(project_id, "测试目的地", fields=["food"])
    assert len(issues_food_scope) == 1
    assert issues_food_scope[0]["field"] == "food"

    # fields=["play"]：food不在范围内，即使food确实有问题也不应该被核验出来
    assert citation_check.verify_pitfall_citations(project_id, "测试目的地", fields=["play"]) == []

    # fields=[]：显式不核验任何字段
    assert citation_check.verify_pitfall_citations(project_id, "测试目的地", fields=[]) == []


def test_verify_lifestyle_citations_scopes_to_given_fields(tmp_path, monkeypatch):
    """跟test_verify_pitfall_citations_scopes_to_given_fields对称，验证verify_lifestyle_citations
    同样的fields筛选行为（docs/ISSUES.md第37补条）——lifestyle_agent的edit流程是这个问题
    最初被发现的场景，同一套_verify_items实现，理应同样受益。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-citation-scope-test"
    store.init_project(project_id)

    topic_index = tmp_path / "material_library" / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "熊猫基地是成都著名景点，可以看熊猫"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            # food：完全没有claimed_entities/sources，也没有诚实声明——一定会报问题（零LLM成本路径）
            {"field": "food", "day": 1, "recommendation": "推荐去市二医院附近吃点东西", "sources": [], "claimed_entities": []},
            # play：字面完全匹配，一定通过，不产生问题
            {
                "field": "play",
                "day": 1,
                "recommendation": "去熊猫基地看熊猫",
                "claimed_entities": ["熊猫基地"],
                "sources": [{"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}],
            },
        ],
    )

    # 不传fields（默认全量）：food的问题应该被发现
    assert len(citation_check.verify_lifestyle_citations(project_id, "测试目的地")) == 1

    # fields=["food"]：food在范围内，问题依然应该被发现
    issues_food_scope = citation_check.verify_lifestyle_citations(project_id, "测试目的地", fields=["food"])
    assert len(issues_food_scope) == 1
    assert issues_food_scope[0]["field"] == "food"

    # fields=["play"]：food不在范围内，即使food确实有问题也不应该被核验出来
    assert citation_check.verify_lifestyle_citations(project_id, "测试目的地", fields=["play"]) == []

    # fields=[]：显式不核验任何字段
    assert citation_check.verify_lifestyle_citations(project_id, "测试目的地", fields=[]) == []


def test_verify_lifestyle_citations_scopes_to_given_item_keys(tmp_path, monkeypatch):
    """item级scoping（比fields更细的粒度）：edit模式下用户只改了某个字段里的某一天，
    同字段里其它天完全没被触碰的item不应该被重新核验——即使那些item本身字面对不上、
    会触发档位B/C的非确定性LLM判断。见docs/ISSUES.md第37补条"已知局限"（字段级scoping
    不够细，同字段内其它天的item仍会被无谓地重新送入核验循环）。

    跟test_verify_lifestyle_citations_scopes_to_given_fields同样用零LLM成本路径验证——
    day1完全没有claimed_entities/sources也没有诚实声明（零成本必报问题的路径），
    day2同理构造一条会报问题的内容；只要item_keys筛选生效，指定只核验day1时day2的
    问题就不应该出现在结果里，反之亦然。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-citation-item-scope-test"
    store.init_project(project_id)

    topic_index = tmp_path / "material_library" / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "熊猫基地是成都著名景点，可以看熊猫"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            # day1：完全没有claimed_entities/sources，也没有诚实声明——零成本必报问题
            {"field": "food", "day": 1, "recommendation": "推荐去市二医院附近吃点东西", "sources": [], "claimed_entities": []},
            # day2：同样零成本必报问题，用来验证"只核验day1"时day2不应该被牵连
            {"field": "food", "day": 2, "recommendation": "推荐去玉林路附近吃点东西", "sources": [], "claimed_entities": []},
        ],
    )

    # 不传item_keys（默认None）：food两天都应该被核验出来，行为跟修复前一致
    issues_all = citation_check.verify_lifestyle_citations(project_id, "测试目的地")
    assert {i["day"] for i in issues_all} == {1, 2}

    # item_keys只包含("food", 1)：只有day1应该被核验，day2完全不应该出现在结果里
    issues_day1_only = citation_check.verify_lifestyle_citations(
        project_id, "测试目的地", item_keys={("food", 1)}
    )
    assert {i["day"] for i in issues_day1_only} == {1}

    # item_keys只包含("food", 2)：反过来，只有day2
    issues_day2_only = citation_check.verify_lifestyle_citations(
        project_id, "测试目的地", item_keys={("food", 2)}
    )
    assert {i["day"] for i in issues_day2_only} == {2}

    # item_keys为空集合：显式一条都不核验
    assert citation_check.verify_lifestyle_citations(project_id, "测试目的地", item_keys=set()) == []


def test_verify_lifestyle_citations_scopes_to_claim_ids_and_preserves_other_checks(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-citation-claim-scope-test"
    store.init_project(project_id)
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "play",
                "day": 1,
                "recommendation": "先去甲景点，再去乙景点。",
                "claims": [
                    {
                        "claim_id": "claim-a",
                        "target_path": "recommendation",
                        "claim_text": "甲景点可作为第一站",
                        "support_mode": "estimate",
                    },
                    {
                        "claim_id": "claim-b",
                        "target_path": "recommendation",
                        "claim_text": "乙景点可作为第二站",
                        "support_mode": "estimate",
                    },
                ],
            }
        ],
    )

    assert citation_check.verify_lifestyle_citations(project_id, "测试目的地") == []
    before = {
        item["claim_id"]: item
        for item in store.read_evidence_checks(project_id)["lifestyle"]
    }
    assert set(before) == {"claim-a", "claim-b"}
    assert before["claim-b"]["observation_mode"] == "live"

    assert citation_check.verify_lifestyle_citations(
        project_id,
        "测试目的地",
        claim_ids={"claim-a"},
        observation_mode="replay",
    ) == []
    after = {
        item["claim_id"]: item
        for item in store.read_evidence_checks(project_id)["lifestyle"]
    }
    assert set(after) == {"claim-a", "claim-b"}
    assert after["claim-a"]["observation_mode"] == "replay"
    assert after["claim-b"]["observation_mode"] == "live"


def test_verify_lifestyle_citations_resume_reuses_only_completed_exact_claims(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-citation-resume-test"
    topic = "测试目的地"
    store.init_project(project_id)
    topic_dir = tmp_path / "material_library" / "index" / topic
    topic_dir.mkdir(parents=True)
    (topic_dir / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "food-c1", "text": "甲事实和乙事实均有依据。"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "甲事实；乙事实。",
                "claims": [
                    {
                        "claim_id": "claim-a",
                        "target_path": "recommendation",
                        "claim_text": "甲事实",
                        "claim_type": "food",
                        "support_mode": "direct",
                        "evidence_refs": [{"platform": "bilibili", "chunk_id": "food-c1"}],
                    },
                    {
                        "claim_id": "claim-b",
                        "target_path": "recommendation",
                        "claim_text": "乙事实",
                        "claim_type": "food",
                        "support_mode": "direct",
                        "evidence_refs": [{"platform": "bilibili", "chunk_id": "food-c1"}],
                    },
                ],
            }
        ],
    )

    class InterruptedJudge:
        calls = 0

        def invoke(self, _prompt):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("provider interrupted")
            return citation_check.CitationVerdict(supported=True, reason="有依据")

    interrupted = InterruptedJudge()

    class InterruptedModel:
        def with_structured_output(self, _schema, method=None):
            return interrupted

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: InterruptedModel())
    with pytest.raises(RuntimeError, match="provider interrupted"):
        citation_check.verify_lifestyle_citations(project_id, topic)

    partial = store.read_evidence_checks(project_id)["lifestyle"]
    complete_targets = {
        check["target_key"]
        for check in partial
        if check.get("scope") == "substantive_claim" and check.get("result") == "pass"
    }
    assert len(complete_targets) == 1
    assert all(
        check.get("verification_fingerprint")
        for check in partial
        if check["target_key"] in complete_targets
    )

    class ResumeJudge:
        calls = 0

        def invoke(self, _prompt):
            self.calls += 1
            return citation_check.CitationVerdict(supported=True, reason="有依据")

    resumed = ResumeJudge()

    class ResumeModel:
        def with_structured_output(self, _schema, method=None):
            return resumed

    monkeypatch.setattr(citation_check, "get_chat_model", lambda **_kwargs: ResumeModel())
    assert citation_check.verify_lifestyle_citations(
        project_id, topic, reuse_existing_completed_claims=True
    ) == []
    assert resumed.calls == 1


def test_reusable_completed_claim_checks_rejects_changed_or_incomplete_fingerprint():
    item = {"target_key": "lifestyle:food:day:1:item:0:claim:claim-a", "_verification_fingerprint": "new"}
    complete_old = [
        {"target_key": item["target_key"], "scope": "source_availability", "result": "pass", "verification_fingerprint": "old"},
        {"target_key": item["target_key"], "scope": "entity_identity", "result": "pass", "verification_fingerprint": "old"},
        {"target_key": item["target_key"], "scope": "substantive_claim", "result": "pass", "verification_fingerprint": "old"},
    ]
    assert citation_check._reusable_completed_claim_checks(complete_old, [item]) == ([], set(), [])

    item["_verification_fingerprint"] = "old"
    incomplete = complete_old[:-1]
    assert citation_check._reusable_completed_claim_checks(incomplete, [item]) == ([], set(), [])

    errored = [dict(check) for check in complete_old]
    errored[-1]["result"] = "error"
    assert citation_check._reusable_completed_claim_checks(errored, [item]) == ([], set(), [])


def test_reusable_completed_claim_checks_preserves_exact_business_failure():
    item = {
        "target_key": "lifestyle:food:day:1:item:0:claim:claim-a",
        "label": "food.day1事实",
        "chunk_ids": ["chunk-a"],
        "extra": {"field": "food", "day": 1, "claim_id": "claim-a"},
        "_verification_fingerprint": "same",
    }
    complete_failure = [
        {"target_key": item["target_key"], "scope": "source_availability", "result": "pass", "verification_fingerprint": "same"},
        {"target_key": item["target_key"], "scope": "entity_identity", "result": "pass", "verification_fingerprint": "same"},
        {
            "target_key": item["target_key"],
            "scope": "substantive_claim",
            "result": "fail",
            "reason": "原文不支持该具体事实。",
            "missing_entities": [],
            "verification_fingerprint": "same",
        },
    ]

    checks, target_keys, issues = citation_check._reusable_completed_claim_checks(
        complete_failure, [item]
    )

    assert len(checks) == 3
    assert target_keys == {item["target_key"]}
    assert issues == [
        {
            "label": "food.day1事实",
            "chunk_ids": ["chunk-a"],
            "missing_entities": [],
            "reason": "原文不支持该具体事实。",
            "field": "food",
            "day": 1,
            "claim_id": "claim-a",
        }
    ]


def test_reusable_completed_direct_claim_preserves_entity_failure_without_duplicate_on_resume():
    item = {
        "target_key": "poi_detail:水上雅丹:claim:cross-poi",
        "label": "水上雅丹事实",
        "chunk_ids": ["chunk-a"],
        "extra": {
            "claim_id": "claim-cross-poi",
            "support_mode": "direct",
            "poi_name": "水上雅丹",
        },
        "_verification_fingerprint": "same",
    }
    completed = [
        {
            "target_key": item["target_key"],
            "scope": "source_availability",
            "result": "pass",
            "verification_fingerprint": "same",
        },
        {
            "target_key": item["target_key"],
            "scope": "entity_identity",
            "result": "fail",
            "reason": "证据属于翡翠湖。",
            "missing_entities": ["水上雅丹"],
            "verification_fingerprint": "same",
        },
        {
            "target_key": item["target_key"],
            "scope": "substantive_claim",
            "result": "pass",
            "reason": "文字相似。",
            "verification_fingerprint": "same",
        },
    ]

    checks, target_keys, issues = citation_check._reusable_completed_claim_checks(
        completed, [item]
    )

    assert len(checks) == 3
    assert target_keys == {item["target_key"]}
    assert len(issues) == 1
    assert issues[0]["missing_entities"] == ["水上雅丹"]
    assert "不能覆盖实体归属失败" in issues[0]["reason"]

    resumed_issues = citation_check._verify_evidence_claim_items(
        [item],
        {},
        checks,
        allow_model_calls=False,
        preverified_target_keys=target_keys,
    )

    assert resumed_issues == []
    assert len(issues + resumed_issues) == 1


def test_verify_lifestyle_citations_claim_scope_clears_removed_failed_claim(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-citation-removed-claim-test"
    store.init_project(project_id)
    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "play",
                "day": 1,
                "recommendation": "仅保留乙景点。",
                "claims": [
                    {
                        "claim_id": "claim-b",
                        "target_path": "recommendation",
                        "claim_text": "乙景点可作为第二站",
                        "support_mode": "estimate",
                    }
                ],
            }
        ],
    )
    store.write_evidence_checks(
        project_id,
        "lifestyle",
        [
            {
                "target_key": "lifestyle:play:day:1:item:0:claim:claim-a",
                "claim_id": "claim-a",
                "scope": "substantive_claim",
                "result": "fail",
                "reason": "原引用不支持",
            },
            {
                "target_key": "lifestyle:play:day:1:item:0:claim:claim-b",
                "claim_id": "claim-b",
                "scope": "substantive_claim",
                "result": "pass",
                "reason": "已通过",
            },
        ],
        replace_prefixes=["lifestyle:"],
    )

    assert citation_check.verify_lifestyle_citations(
        project_id, "测试目的地", claim_ids={"claim-a"}
    ) == []
    evidence = store.read_evidence_checks(project_id)
    assert [item["claim_id"] for item in evidence["lifestyle"]] == ["claim-b"]
    assert any(
        item.get("claim_id") == "claim-a"
        for history in evidence["history"]
        for item in history.get("checks", [])
    )


def test_verify_lifestyle_citations_web_verified_source_passes_with_matching_log(tmp_path, monkeypatch):
    """跟test_verify_route_citations_web_verified_source_passes_with_matching_log对称
    （docs/ISSUES.md第115条方向C）：lifestyle_agent同样接了web_search（规则16价格时效
    核实），web_verified来源在有匹配日志时应该通过核验。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-web-verified-test"
    store.init_project(project_id)

    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "food",
                "day": 1,
                "recommendation": "翡翠湖附近的餐厅",
                "claimed_entities": ["翡翠湖"],
                "sources": [{"platform": "web_verified", "doc_id": "web-1", "chunk_id": "web-翡翠湖-餐厅"}],
            },
        ],
    )

    log_dir = tmp_path / project_id / "agents" / "lifestyle_agent"
    log_dir.mkdir(parents=True)
    (log_dir / "web_search_log.jsonl").write_text(
        json.dumps({"query": "翡翠湖 餐厅", "result": "- 翡翠湖附近餐厅信息（来源：https://example.com）"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    issues = citation_check.verify_lifestyle_citations(project_id, "测试目的地")
    assert issues == []


def test_load_amap_evidence_matches_content_and_formats_distance(tmp_path, monkeypatch):
    """docs/ISSUES.md第129条核心回归：driving_cache.json里有匹配content的地名对时，
    应该正确格式化成"「A」→「B」约X公里/Y小时（高德驾车实测缓存）"的证据文本。"""
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    cache_path = tmp_path / "material_library" / "driving_cache.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps({"测试目的地|西宁|茶卡盐湖": {"distance_m": 144000, "duration_s": 9720}}, ensure_ascii=False),
        encoding="utf-8",
    )

    evidence = citation_check._load_amap_evidence("测试目的地", "【西宁→茶卡盐湖】约144km/2.7小时（高德实测）")
    assert "「西宁」→「茶卡盐湖」" in evidence
    assert "144公里" in evidence
    assert "2.7小时" in evidence


def test_load_amap_evidence_prefers_specific_over_coarser_duplicate(tmp_path, monkeypatch):
    """真实撞到的坑：同一对概念地点常同时有粗/细两种粒度的key，粗粒度那条可能是geocode
    歧义产生的离谱值——两者都命中content时，应该只采信更具体（更长）的那一个。"""
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    cache_path = tmp_path / "material_library" / "driving_cache.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "测试目的地|茶卡盐湖|青海湖": {"distance_m": 1752000, "duration_s": 74000},  # 粗粒度，离谱值
                "测试目的地|茶卡盐湖|青海湖二郎剑景区": {"distance_m": 143800, "duration_s": 9600},  # 精确值
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    evidence = citation_check._load_amap_evidence(
        "测试目的地", "【茶卡盐湖→青海湖二郎剑景区】约144km/2.7小时（高德实测）"
    )
    assert "1752公里" not in evidence
    assert "144公里" in evidence


def test_load_amap_evidence_rejects_name_match_with_wrong_distance(tmp_path, monkeypatch):
    """地名子串凑巧都命中，但缓存记录的公里数跟content里写的数字对不上时（说明缓存记录
    描述的不是这趟路程，比如geocode歧义把同名异地地点配对到了一起），不应该采信。"""
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    cache_path = tmp_path / "material_library" / "driving_cache.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps({"测试目的地|茶卡盐湖|青海湖": {"distance_m": 1752000, "duration_s": 74000}}, ensure_ascii=False),
        encoding="utf-8",
    )

    evidence = citation_check._load_amap_evidence(
        "测试目的地", "【德令哈→翡翠湖】约218km/2.7小时，途经茶卡盐湖和青海湖方向"
    )
    assert evidence == ""


def test_verify_lifestyle_citations_amap_source_passes_with_matching_cache(tmp_path, monkeypatch):
    """docs/ISSUES.md第129条端到端回归：sources标platform=="amap"的条目，chunk_id是
    合成字符串（如"topic-amap_driving_distance"），chunks.jsonl里永远查不到——但
    driving_cache.json里有匹配content的真实距离数据时，应该通过核验，而不是像修复前
    那样因为combined_text里amap那部分永远是空字符串被判定"未提供原文片段"。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-amap-test"
    store.init_project(project_id)

    topic_index = tmp_path / "material_library" / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "测试目的地-001-c000", "text": "茶卡盐湖门票60元起，小火车50元"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "material_library" / "driving_cache.json").write_text(
        json.dumps({"测试目的地|西宁|茶卡盐湖": {"distance_m": 144000, "duration_s": 9720}}, ensure_ascii=False),
        encoding="utf-8",
    )

    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "play",
                "day": 1,
                "recommendation": "【西宁→茶卡盐湖】约144km/2.7小时（高德实测），门票60元起",
                "claimed_entities": ["茶卡盐湖"],
                "sources": [
                    {"platform": "xiaohongshu", "doc_id": "测试目的地-001", "chunk_id": "测试目的地-001-c000"},
                    {"platform": "amap", "doc_id": "测试目的地-amap", "chunk_id": "测试目的地-amap_driving_distance"},
                ],
            },
        ],
    )

    # "茶卡盐湖"字面核对能通过，但content命中_VERIFIABLE_FACT_PATTERN（144km/2.7小时），
    # 仍会触发一次档位C——这里不是要证明"不调用LLM"，是要证明传给档位C的combined_text
    # 真的包含了amap证据（修复前这里永远是空字符串，档位C看不到任何支持144km的文本）。
    captured_prompts = []

    class _FakeContentJudge:
        def invoke(self, prompt):
            captured_prompts.append(prompt)
            return citation_check.CitationVerdict(supported=True, reason="原文和高德实测数据支持这一说法")

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda temperature=0, max_tokens=None: _FakeModel())

    issues = citation_check.verify_lifestyle_citations(project_id, "测试目的地")
    assert issues == []
    assert captured_prompts, "144km应该触发_VERIFIABLE_FACT_PATTERN，走到档位C"
    assert "高德驾车实测缓存" in captured_prompts[0]


def test_ordered_chunk_ids_puts_amap_before_web_verified_before_regular():
    """docs/ISSUES.md第130条核心回归：_amap_ids排最前，_web_verified_ids次之，
    普通chunk_id保留原有相对顺序排最后——不是简单的"合成证据整体优先"，因为amap
    证据（几十字符）比web_verified证据（真实撞到过15KB）更精炼，两者都排最前面的话
    amap仍可能被更长的web_verified证据挤到不显眼的位置。"""
    item = {
        "_amap_ids": ["amap-1"],
        "_web_verified_ids": ["web-1"],
    }
    chunk_ids = ["real-1", "web-1", "real-2", "amap-1"]
    ordered = citation_check._ordered_chunk_ids(item, chunk_ids)
    assert ordered == ["amap-1", "web-1", "real-1", "real-2"]


def test_ordered_chunk_ids_no_synthetic_evidence_keeps_original_order():
    """没有amap/web_verified来源时，不做任何重排——普通场景（占绝大多数）行为完全不变。"""
    item = {"_amap_ids": [], "_web_verified_ids": []}
    chunk_ids = ["real-1", "real-2", "real-3"]
    assert citation_check._ordered_chunk_ids(item, chunk_ids) == chunk_ids


def test_verify_lifestyle_citations_amap_evidence_not_drowned_by_large_web_verified_evidence(
    tmp_path, monkeypatch
):
    """真实复现场景（docs/ISSUES.md第130条）：青甘大环线`play.day8`同时引用了amap
    （441km/4.8小时，32字符）和web_verified（甘肃省博物馆核实内容，15463字符）两种
    合成证据——修复前combined_text按sources原始顺序简单拼接，amap那32个字符被
    web_verified的长文本挤没了信噪比，档位C判断"未提及441km"，误判为缺乏依据。
    这里用等比缩小的数据复现同一种"短证据被长证据挤没"结构，验证修复后档位C传入的
    combined_text里amap证据在最前面、且完整可见。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "lifestyle-amap-noise-test"
    store.init_project(project_id)

    topic_index = tmp_path / "material_library" / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "material_library" / "driving_cache.json").write_text(
        json.dumps({"测试目的地|兰州|山丹县": {"distance_m": 441000, "duration_s": 17280}}, ensure_ascii=False),
        encoding="utf-8",
    )

    log_dir = tmp_path / project_id / "agents" / "lifestyle_agent"
    log_dir.mkdir(parents=True)
    # 模拟真实案例里那种远超amap证据长度的web_verified日志（用重复文本等比放大，不需要
    # 真的15KB，只要显著长于amap的那几十个字符、验证"排在前面"这个结构性修复即可）。
    long_museum_text = "甘肃省博物馆开放时间与预约信息" * 200
    (log_dir / "web_search_log.jsonl").write_text(
        json.dumps({"query": "甘肃省博物馆", "result": long_museum_text}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    store.write_agent_output_patch(
        project_id,
        "lifestyle_agent",
        [
            {
                "field": "play",
                "day": 8,
                "recommendation": "【山丹县→兰州】约441km/4.8小时（高德实测）。甘肃省博物馆建议留2小时参观。",
                "claimed_entities": ["甘肃省博物馆"],
                "sources": [
                    {"platform": "web_verified", "doc_id": "web", "chunk_id": "web-gansumuseum"},
                    {"platform": "amap", "doc_id": "测试目的地-amap", "chunk_id": "测试目的地-amap_driving_distance"},
                ],
            },
        ],
    )

    captured_prompts = []

    class _FakeContentJudge:
        def invoke(self, prompt):
            captured_prompts.append(prompt)
            return citation_check.CitationVerdict(supported=True, reason="441km有高德实测数据支持")

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda temperature=0, max_tokens=None: _FakeModel())

    issues = citation_check.verify_lifestyle_citations(project_id, "测试目的地")
    assert issues == []
    assert captured_prompts
    prompt = captured_prompts[0]
    amap_pos = prompt.find("441公里")
    museum_pos = prompt.find("甘肃省博物馆开放时间")
    assert amap_pos != -1, "amap证据应该出现在传给档位C的prompt里"
    assert amap_pos < museum_pos, "amap证据应该排在冗长的web_verified证据之前，不被挤没"


def test_verify_pitfall_citations_scopes_to_given_item_keys(tmp_path, monkeypatch):
    """跟test_verify_lifestyle_citations_scopes_to_given_item_keys对称，验证
    verify_pitfall_citations同样的item_keys筛选行为——避雷agent的edit流程跟lifestyle
    共用同一套_verify_items实现，理应同样受益。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", tmp_path / "material_library")
    project_id = "pitfall-citation-item-scope-test"
    store.init_project(project_id)

    topic_index = tmp_path / "material_library" / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "熊猫基地是成都著名景点，可以看熊猫"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    store.write_pitfall_output_patch(
        project_id,
        "pitfall_agent",
        [
            # day1：非空warning但完全没有claimed_entities/sources、无诚实声明——零成本必报问题
            {"field": "food", "day": 1, "target_recommendation": "叶婆婆钵钵鸡", "warning": "有卫生问题投诉"},
            # day2：同样构造零成本必报问题的场景
            {"field": "food", "day": 2, "target_recommendation": "某某火锅", "warning": "有过期食材投诉"},
        ],
    )

    issues_all = citation_check.verify_pitfall_citations(project_id, "测试目的地")
    assert {i["day"] for i in issues_all} == {1, 2}

    issues_day1_only = citation_check.verify_pitfall_citations(
        project_id, "测试目的地", item_keys={("food", 1)}
    )
    assert {i["day"] for i in issues_day1_only} == {1}

    issues_day2_only = citation_check.verify_pitfall_citations(
        project_id, "测试目的地", item_keys={("food", 2)}
    )
    assert {i["day"] for i in issues_day2_only} == {2}

    assert citation_check.verify_pitfall_citations(project_id, "测试目的地", item_keys=set()) == []


def test_verify_lifestyle_citations_resolves_entity_via_sibling_chunk_in_same_doc(tmp_path, monkeypatch):
    """docs/ISSUES.md第127条真实复现（青甘大环线8天E2E，"尚客优酒店"案例）：素材切分时
    一篇"多图截图合集"笔记的多天内容被压缩进相邻的多个chunk（c001讲Day5抵达敦煌住宿，
    c002讲Day6行程），item引用了c002（不含"尚客优酒店"），但这个名字真实存在于同一篇
    文档的c001里——模型标注sources时选错了相邻chunk，不是编造。零成本检查同文档兄弟
    chunk应该能在不调用LLM的前提下解决这条，不再需要走档位B/C。"""
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        "\n".join([
            json.dumps({"chunk_id": "d1-c001", "text": "抵达敦煌后住宿酒店（尚客优酒店）"}, ensure_ascii=False),
            json.dumps({"chunk_id": "d1-c002", "text": "第二天从敦煌出发前往下一站"}, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )
    project_id = "test-project"
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    store.init_project(project_id)
    store.write_agent_output_patch(
        project_id, "lifestyle_agent",
        [{
            "field": "lodging", "day": 4,
            "recommendation": "当晚住敦煌尚客优酒店",
            "sources": [{"platform": "xiaohongshu", "doc_id": "d1", "chunk_id": "d1-c002"}],  # 引错了相邻chunk
            "claimed_entities": ["尚客优酒店"],
        }],
    )

    issues = citation_check.verify_lifestyle_citations(project_id, "测试目的地")
    assert issues == []


def test_verify_lifestyle_citations_sibling_chunk_fallback_does_not_search_other_docs(tmp_path, monkeypatch):
    """跟上一个测试对称：同文档兜底只在"同一篇文档"范围内找，不搜索全库其它文档——
    否则会削弱核验强度（比如通用地名在毫不相关的文档里凑巧出现也会被误判为有依据）。
    这里让真正含依据的chunk属于另一篇文档（d2），确认不会被跨文档误判为通过。"""
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        "\n".join([
            json.dumps({"chunk_id": "d1-c001", "text": "第二天从敦煌出发前往下一站"}, ensure_ascii=False),
            json.dumps({"chunk_id": "d2-c001", "text": "抵达敦煌后住宿酒店（尚客优酒店）"}, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )
    project_id = "test-project"
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    store.init_project(project_id)
    store.write_agent_output_patch(
        project_id, "lifestyle_agent",
        [{
            "field": "lodging", "day": 4,
            "recommendation": "当晚住敦煌尚客优酒店",
            "sources": [{"platform": "xiaohongshu", "doc_id": "d1", "chunk_id": "d1-c001"}],
            "claimed_entities": ["尚客优酒店"],
        }],
    )

    class _FakeAliasJudge:
        def invoke(self, prompt):
            return citation_check.AliasResolution(resolved=[], still_missing=["尚客优酒店"], reason="原文没有提到")

    class _FakeContentJudge:
        def invoke(self, prompt):
            return citation_check.CitationVerdict(supported=False, reason="原文没有提到")

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeAliasJudge() if schema is citation_check.AliasResolution else _FakeContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda temperature=0, max_tokens=None: _FakeModel())

    issues = citation_check.verify_lifestyle_citations(project_id, "测试目的地")
    assert len(issues) == 1
    assert "尚客优酒店" in issues[0]["missing_entities"]


def test_verify_poi_detail_strips_composite_point_name(tmp_path, monkeypatch):
    # 复现真实E2E（poi-detail-e2e-v1）里的问题：POI详情agent写的point_name常是
    # "刘备殿（汉昭烈庙）"、"武侯祠博物馆大门/文物区入口"这种"名称（描述）/别名"复合串，
    # 修复前整串当一个claimed_entity做字面核对，会因为括号描述/斜杠别名永远匹配不上而
    # 误判缺乏依据。修复后剥离括号、按/拆开，只核验纯名称，地名在原文里就不该被报问题。
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "武侯祠里的刘备殿很值得看，红墙夹道拍照出片"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    poi_detail = {
        "poi_name": "武侯祠",
        "day": 1,
        "routes": [
            {
                "route_name": "经典线",
                "route_segments": [
                    {
                        "order": 1,
                        "point_name": "刘备殿（汉昭烈庙）",  # 括号描述要剥离，只核验"刘备殿"
                        "highlight": "祭祀刘备",
                        "sources": [{"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}],
                    },
                    {
                        "order": 2,
                        "point_name": "红墙夹道",
                        "highlight": "拍照出片",
                        "sources": [{"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}],
                    },
                ],
            }
        ],
    }
    issues = citation_check.verify_poi_detail_citations(poi_detail, "测试目的地")
    # 两个途经点的纯名称（刘备殿/红墙夹道）都在原文里，档位A就能字面命中，不该报问题
    assert issues == []


def test_verify_poi_point_name_claim_checks_identity_not_route_wording(tmp_path, monkeypatch):
    """“被排为途经点”是路线结构决定；point_name claim只需证明地点身份。"""
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "刘备殿是武侯祠的重要建筑。"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        citation_check,
        "get_chat_model",
        lambda **_kwargs: pytest.fail("实体字面命中后不应调用实质核验模型"),
    )
    poi_detail = {
        "poi_name": "武侯祠",
        "day": 1,
        "routes": [
            {
                "route_name": "经典线",
                "route_segments": [
                    {
                        "order": 1,
                        "point_name": "刘备殿",
                        "claims": [
                            {
                                "claim_id": "point-1",
                                "target_path": "point_name",
                                "claim_text": "路线第一个途经点安排为刘备殿。",
                                "support_mode": "direct",
                                "claimed_entities": ["刘备殿"],
                                "evidence_refs": [
                                    {"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    assert citation_check.verify_poi_detail_citations(poi_detail, "测试目的地") == []


def test_verify_poi_detail_procedural_point_name_skips_literal_entity_check(tmp_path, monkeypatch):
    # 复现docs/ISSUES.md第125条真实E2E案例：point_name是"动作步骤：具体操作"这种格式
    # （如"入馆准备：提前预约+抵达安检"），不是地名，原文永远不可能逐字包含这句完整操作
    # 说明——修复前会被当claimed_entity逐字核对，3轮重试issue描述几乎逐字重复（模型
    # 不是不肯改，是这个point_name格式本身没法通过核验）。修复后含"："的point_name不再
    # 被当claimed_entity，只要sources非空、highlight本身没有编造的具体数字，就不该报问题。
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "提前3天预约，周一闭馆"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    poi_detail = {
        "poi_name": "测试博物馆",
        "day": 1,
        "routes": [
            {
                "route_name": "经典线",
                "route_segments": [
                    {
                        "order": 1,
                        "point_name": "入馆准备：提前预约+抵达安检",
                        "highlight": "提前3天预约，周一闭馆记得避开",
                        "sources": [{"platform": "xiaohongshu", "doc_id": "d1", "chunk_id": "d1-c001"}],
                    },
                ],
            }
        ],
    }
    issues = citation_check.verify_poi_detail_citations(poi_detail, "测试目的地")
    assert issues == []


def test_verify_poi_detail_procedural_point_name_with_empty_sources_still_flagged(tmp_path, monkeypatch):
    # 跟上一个测试对称：point_name是动作步骤格式时claimed_entities刻意留空，
    # 但如果sources也确实是空的（真的没有引用依据），不能因为claimed_entities的豁免
    # 就连带放过chunk_ids缺失这件事——_no_entity_to_verify只豁免"没有实体可核对"，
    # 不豁免"完全没有sources"。
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr("src.materials.citation_check.MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "无关内容"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    poi_detail = {
        "poi_name": "测试博物馆",
        "day": 1,
        "routes": [
            {
                "route_name": "经典线",
                "route_segments": [
                    {
                        "order": 1,
                        "point_name": "入馆准备：提前预约+抵达安检",
                        "highlight": "提前3天预约，周一闭馆记得避开",
                        "sources": [],
                    },
                ],
            }
        ],
    }
    issues = citation_check.verify_poi_detail_citations(poi_detail, "测试目的地")
    assert len(issues) == 1
    assert "完全没有标注" in issues[0]["reason"]


def test_verify_poi_detail_citations_route_names_scoping(tmp_path, monkeypatch):
    """route_names参数（poi_detail_edit_node的edit入口用来把核验范围收窄到这次真的
    改动/新增的路线，见docs/ISSUES.md第37补条同一个思路）：只核验指定的路线，
    未被指定的路线即使原文里完全没有依据，也不该被报成问题。"""
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "武侯祠里的刘备殿很值得看"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    poi_detail = {
        "poi_name": "武侯祠",
        "day": 1,
        "routes": [
            {
                "route_name": "经典线",
                "route_segments": [
                    {"order": 1, "point_name": "刘备殿", "highlight": "祭祀刘备", "sources": [{"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}]},
                ],
            },
            {
                "route_name": "后山线",
                "route_segments": [
                    # 原文完全没提到"编造的观景台"，但这条路线不在route_names范围内，不该被核验
                    {"order": 1, "point_name": "编造的观景台", "highlight": "编造的介绍", "sources": []},
                ],
            },
        ],
    }

    issues_all = citation_check.verify_poi_detail_citations(poi_detail, "测试目的地")
    assert any(i["route_name"] == "后山线" for i in issues_all)

    issues_scoped = citation_check.verify_poi_detail_citations(poi_detail, "测试目的地", route_names=["经典线"])
    assert issues_scoped == []  # 只核验"经典线"，"后山线"的编造内容不在核验范围内


def test_verify_poi_detail_citations_segment_keys_scoping(tmp_path, monkeypatch):
    """局部返修后只复核指定(route_name, order)，不重新判断同路线其它途经点。"""
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    (material_dir / "index" / "测试目的地").mkdir(parents=True)
    (material_dir / "index" / "测试目的地" / "chunks.jsonl").write_text(
        "",
        encoding="utf-8",
    )
    poi_detail = {
        "poi_name": "武侯祠",
        "day": 1,
        "routes": [
            {
                "route_name": "经典线",
                "route_segments": [
                    {"order": 1, "point_name": "山门", "highlight": "入口", "sources": []},
                    {"order": 2, "point_name": "刘备殿", "highlight": "核心区域", "sources": []},
                ],
            }
        ],
    }

    issues = citation_check.verify_poi_detail_citations(
        poi_detail,
        "测试目的地",
        segment_keys=[("经典线", 2)],
        allow_model_calls=False,
    )

    assert [(issue["route_name"], issue["order"]) for issue in issues] == [("经典线", 2)]


def test_verify_poi_detail_citations_web_verified_passes_with_matching_log(tmp_path, monkeypatch):
    """真实案例回归（docs/ISSUES.md第115条方向C）：projects/custom-qinggan-linear-8d-
    20260812/workspace/outputs/poi_details/day2_大柴旦翡翠湖.json的"检票入园（游客中心）"
    途经点，sources标platform=="web_verified"、chunk_id是合成字符串"web-大柴旦翡翠湖-
    门票开放时间"，此前不管这次是否真的调用过web_search都会被判定"未提供原文片段"。
    传入project_id/log_scope、且日志里有匹配证据时应该通过。"""
    monkeypatch.setattr(citation_check, "PROJECTS_DIR", tmp_path)
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    project_id = "poi-detail-web-verified-test"
    day, poi_name = 2, "大柴旦翡翠湖"

    poi_detail = {
        "poi_name": poi_name,
        "day": day,
        "routes": [
            {
                "route_name": "经典小火车主线",
                "route_segments": [
                    {
                        "order": 1,
                        "point_name": "检票入园（游客中心）",
                        "highlight": "景区大门购票入园，门票网络来源报价约50元/人",
                        "sources": [
                            {"platform": "web_verified", "doc_id": "web-1", "chunk_id": "web-大柴旦翡翠湖-门票开放时间"}
                        ],
                    },
                ],
            }
        ],
    }

    log_scope = f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}"
    log_dir = tmp_path / project_id / "agents" / log_scope
    log_dir.mkdir(parents=True)
    (log_dir / "web_search_log.jsonl").write_text(
        json.dumps(
            {
                "query": "大柴旦翡翠湖 门票 开放时间",
                "result": (
                    "- 大柴旦翡翠湖：游客中心位于景区大门，"
                    "游客在景区大门购票后从游客中心检票入园，"
                    "门票约50元/人（来源：https://example.com）"
                ),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    issues = citation_check.verify_poi_detail_citations(
        poi_detail, "测试目的地", project_id=project_id, log_scope=log_scope
    )
    assert issues == []


def test_verify_poi_detail_citations_web_verified_without_project_id_falls_back_to_old_behavior(tmp_path, monkeypatch):
    """向后兼容：不传project_id/log_scope（旧调用方式）时，web_verified来源应该维持
    修复前的行为——combined_text为空，判定缺乏依据。"""
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)

    poi_detail = {
        "poi_name": "大柴旦翡翠湖",
        "day": 2,
        "routes": [
            {
                "route_name": "经典小火车主线",
                "route_segments": [
                    {
                        "order": 1,
                        "point_name": "检票入园（游客中心）",
                        "highlight": "景区大门购票入园，门票网络来源报价约50元/人",
                        "sources": [
                            {"platform": "web_verified", "doc_id": "web-1", "chunk_id": "web-大柴旦翡翠湖-门票开放时间"}
                        ],
                    },
                ],
            }
        ],
    }

    issues = citation_check.verify_poi_detail_citations(poi_detail, "测试目的地")
    assert len(issues) == 1
    assert "检票入园" in issues[0]["point_name"]


def test_verify_poi_detail_includes_suggested_duration_in_content_judge_prompt(tmp_path, monkeypatch):
    """2026-07-22审计发现的缺口：suggested_duration（建议停留时间）此前完全没被纳入核验
    文本，模型编一个"建议停留45分钟"不会被任何环节看到。这里验证档位C触发时，
    content_judge收到的内容里确实包含suggested_duration文本，不只是point_name+highlight。

    需要mock LLM判断（get_chat_model），因为要触发到档位C必须先让档位A/B都判定"缺失"——
    这是项目里第一次给_verify_items的档位B/C路径写mock测试，此前的测试只覆盖档位A
    （字面匹配直接通过，不触发LLM）。
    """
    material_dir = tmp_path / "material_library"
    monkeypatch.setattr(citation_check, "MATERIAL_LIBRARY_DIR", material_dir)
    topic_index = material_dir / "index" / "测试目的地"
    topic_index.mkdir(parents=True)
    (topic_index / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "d1-c001", "text": "武侯祠是三国蜀汉丞相祠堂，可以逛逛"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    poi_detail = {
        "poi_name": "武侯祠",
        "day": 1,
        "routes": [
            {
                "route_name": "经典线",
                "route_segments": [
                    {
                        "order": 1,
                        "point_name": "刘备殿",  # 原文没提到，档位A/B都会判定缺失，触发档位C
                        "highlight": "祭祀刘备",
                        "suggested_duration": "45分钟",
                        "sources": [{"platform": "bilibili", "doc_id": "d1", "chunk_id": "d1-c001"}],
                    }
                ],
            }
        ],
    }

    captured_content_prompts = []

    class _FakeAliasJudge:
        def invoke(self, prompt):
            return citation_check.AliasResolution(resolved=[], still_missing=["刘备殿"], reason="原文没提到刘备殿")

    class _FakeContentJudge:
        def invoke(self, prompt):
            captured_content_prompts.append(prompt)
            return citation_check.CitationVerdict(supported=False, reason="原文没有实质支撑")

    class _FakeModel:
        def with_structured_output(self, schema, method=None):
            return _FakeAliasJudge() if schema is citation_check.AliasResolution else _FakeContentJudge()

    monkeypatch.setattr(citation_check, "get_chat_model", lambda temperature=0, max_tokens=None: _FakeModel())

    issues = citation_check.verify_poi_detail_citations(poi_detail, "测试目的地")

    assert len(issues) == 1
    assert captured_content_prompts, "档位C应该被触发过一次"
    assert "45分钟" in captured_content_prompts[0]


def test_collect_cited_chunks_handles_none_inputs():
    # route_plan/lifestyle是None（比如还没写入）时不应该报错
    assert citation_check.collect_cited_chunks(None, None) == {}


def test_build_reference_section_shows_real_text_not_model_written_description():
    # 核心回归测试：这就是docs/ISSUES.md第8条的具体复现场景——一个chunk的真实内容
    # 跟被引用处声称的主题完全不相关（地铁打卡路线 vs "东郊记忆"标签），
    # build_reference_section必须原样输出真实原文，不能让任何描述性文字冒充chunk内容
    cited_chunks = {
        "成都-000-c007": {"platform": "bilibili", "author": "", "doc_id": "成都-000"},
    }
    chunk_texts = {
        "成都-000-c007": "八号线结束，我们去李家桥换乘一号线继续下半场...",
    }
    md, references = citation_check.build_reference_section(cited_chunks, chunk_texts)

    assert "李家桥" in md  # 真实原文片段出现在表格里
    assert "东郊记忆" not in md  # 不应该出现任何编造的主题描述
    assert references == [
        {
            "platform": "bilibili",
            "author": "",
            "doc_id": "成都-000",
            "chunk_id": "成都-000-c007",
            "cited_in": [],
            "excerpt": "八号线结束，我们去李家桥换乘一号线继续下半场......",
        }
    ]


def test_reference_section_shows_declared_usage_summary():
    cited_chunks = {
        "成都-020-c001": {
            "platform": "douyin_asr",
            "author": "",
            "doc_id": "成都-020",
            "cited_in": [
                "route:day:2",
                "lifestyle:food:day:2:item:0",
                "poi_detail:day:2:poi:都江堰景区:route:经典线:order:9",
                "pitfall:food:day:2:item:0",
            ],
        }
    }
    md, references = citation_check.build_reference_section(
        cited_chunks,
        {"成都-020-c001": "晚上可以到南桥看蓝眼泪夜景。"},
    )

    assert "声明使用位置" in md
    assert "4处：Day2路线、Day2美食、Day2 都江堰景区·经典线途经点9，另1处" in md
    assert references[0]["cited_in"] == cited_chunks["成都-020-c001"]["cited_in"]


def test_build_reference_section_flags_missing_chunk_text():
    # chunk_id在cited_chunks里但chunk_texts里查不到原文（比如素材库有变动）时，
    # 要明确标注"原文未找到"，不能留空白让人误以为有依据
    cited_chunks = {"missing-c001": {"platform": "bilibili", "author": "", "doc_id": "missing"}}
    md, references = citation_check.build_reference_section(cited_chunks, {})
    assert "原文未找到" in md
    assert len(references) == 1


def test_build_reference_section_empty_when_no_citations():
    md, references = citation_check.build_reference_section({}, {})
    assert references == []
    assert "未记录到具体chunk级引用" in md


# ---------- flat_transcript chunker（ASR产出的纯flat无时间戳口播文本）----------

# 模拟MiMo ASR产出：连续口播、没有任何"发言人 00:00"时间戳标记（真实转写通常上千字，
# 这里用一段足够长、能自然切出多个chunk的文本）
_FLAT_ASR_SAMPLE = (
    "第一次来成都怕被坑怕踩雷，我整理了这份保姆级攻略。先说交通，坐飞机尽量选双流机场，"
    "天府机场离市区很远，坐地铁要一个多小时。动车就选成都南站或者东站，出站就是地铁。"
    "然后是住宿，别再住太古里和春熙路，又贵人多还很吵，住市二医院或者文殊院地铁站附近，"
    "去哪儿都方便。第一天主要逛市中心，熊猫屁股、裸眼大屏，对面的太古里和大慈寺，半天刷完。"
    "饿了的话志丽面馆推荐刀削面和甜水面，姐弟豆花面也不错。晚上一定要去望平街，"
    "书店、小摊、复古小酒馆，这才是成都的松弛感。走到安顺廊桥，晚上亮灯后简直美到不行。"
    "第二天早上八点要到熊猫基地，记得提前买票，看完熊猫直接坐地铁到东郊记忆，"
    "这边有很多特色小店和工业遗址。下午的玉林路才是胃的主场，超级适合city walk。"
    "第三天主打休闲，人民公园喝盖碗茶，然后去祠堂街和柿子巷，宽窄巷子记住三不买原则。"
    "如果还有时间，可以去杜甫草堂和武侯祠，感受一下成都的历史文化底蕴。"
    "杜甫草堂适合阴雨天去，武侯祠出来就是锦里古街，晚上灯笼亮起特别有氛围。"
    "最后一天推荐去成都博物馆，里面的汉代陶俑个个面带微笑，非常治愈。"
    "博物馆对面就是天府广场，地铁四通八达，去机场也方便。"
    "总之成都是个来了就不想走的城市，慢节奏、美食多、人也热情，值得多待几天。"
)


def test_flat_transcript_splits_into_multiple_chunks():
    # 核心：flat文本必须被切成多个chunk，绝不能像误用bilibili时间戳chunker那样退化成一个巨型chunk
    chunks = chunk_flat_transcript(_FLAT_ASR_SAMPLE)
    assert len(chunks) >= 2
    # 没有任何chunk长到接近整篇（否则说明根本没切开）
    assert all(len(c) < len(_FLAT_ASR_SAMPLE) for c in chunks)


def test_flat_transcript_has_overlap_between_adjacent_chunks():
    # 相邻chunk之间应有重叠：后一个chunk开头的内容能在前一个chunk里找到（尾部整句被回捞作为上下文）
    chunks = chunk_flat_transcript(_FLAT_ASR_SAMPLE)
    assert len(chunks) >= 2
    found_overlap = False
    for prev, nxt in zip(chunks, chunks[1:]):
        # 后一个chunk的开头第一句应当出现在前一个chunk里（说明它被作为重叠上下文回捞了）
        first_sentence = nxt.split("。")[0]
        if first_sentence and first_sentence in prev:
            found_overlap = True
    assert found_overlap


def test_flat_transcript_empty_input():
    assert chunk_flat_transcript("") == []
    assert chunk_flat_transcript("   \n  ") == []


def test_flat_transcript_short_input_single_chunk():
    # 短于目标长度的内容不该被强行切碎，也不该丢内容
    short = "住文殊院地铁站附近很方便。"
    chunks = chunk_flat_transcript(short)
    assert chunks == [short]


def test_chunk_content_routes_asr_platform_to_flat_chunker():
    # bilibili_asr / douyin_asr 必须走flat策略，不能走带时间戳的bilibili chunker
    for platform in ("bilibili_asr", "douyin_asr"):
        chunks = chunk_content(_FLAT_ASR_SAMPLE, platform)
        assert len(chunks) >= 2, f"{platform} 应被切成多个chunk"


def test_chunk_content_bilibili_still_uses_timestamp_chunker():
    # 存量bilibili（带"发言人 00:00"标记）仍走时间戳切分，不受本次改动影响
    timestamped = "标识_原文\n2026年07月12日 15:55\n发言人   00:00\n第一段内容。\n\n发言人   00:36\n第二段内容。"
    chunks = chunk_content(timestamped, "bilibili")
    assert "第一段内容。" in chunks
    assert "第二段内容。" in chunks


# ---------- strip_chapter_index：剥离"章节导览"结构化目录块（docs/ISSUES.md第21条第3点）----------


def test_strip_chapter_index_removes_real_case():
    # 真实复现案例：成都-002原始文件，全文就是一段章节导览+标题，没有任何实质描述
    raw_path = PROJECT_ROOT / "data" / "material_library" / "raw" / "成都" / (
        "丢掉你脑子里所有成都攻略！ 直接看我这篇就行了！ "
        "#青年创作者成长计划 #交换世界计划 #旅游攻略 #超会玩派对_原文.md"
    )
    content = raw_path.read_text(encoding="utf-8")
    stripped = strip_chapter_index(content)
    assert "章节导览" not in stripped
    assert "00:30 交通" not in stripped
    assert "东郊记忆" not in stripped  # 只在章节目录里出现的关键词，不应该残留
    assert "丢掉你脑子里所有成都攻略" in stripped  # 标题本身不是章节导览的一部分，应保留


def test_strip_chapter_index_no_match_preserves_content():
    # 没有"章节导览"格式的正常内容不应该被误伤，原样返回
    normal = "武侯祠是三国文化的代表，红墙夹道非常出片。\n- 这是普通的列表项，不是章节导览。"
    assert strip_chapter_index(normal) == normal


def test_strip_chapter_index_empty_input():
    assert strip_chapter_index("") == ""


# ---------- remove_from_index：entries/chunks/FAISS索引三处一并清理（docs/ISSUES.md第21条）----------


def _seed_index(topic_dir, monkeypatch, material_root):
    """在临时目录合成一份最小的entries.jsonl+chunks.jsonl+FAISS索引，供remove_from_index测试用。"""
    monkeypatch.setattr("src.materials.ingest.MATERIAL_LIBRARY_DIR", material_root)
    monkeypatch.setattr("src.materials.vector_store.MATERIAL_LIBRARY_DIR", material_root)

    index_dir = material_root / "index" / topic_dir
    index_dir.mkdir(parents=True)
    entries = [
        {"doc_id": "t-000", "topic": topic_dir, "source_file": "a.md", "platform": "douyin",
         "author": "", "publish_date": "", "title": "a", "is_core": True, "entities": []},
        {"doc_id": "t-001", "topic": topic_dir, "source_file": "b.md", "platform": "douyin",
         "author": "", "publish_date": "", "title": "b", "is_core": True, "entities": []},
    ]
    chunks = [
        {"chunk_id": "t-000-c000", "doc_id": "t-000", "topic": topic_dir, "order": 0,
         "text": "章节导览垃圾内容", "embedding_model": "test", "embedded_at": "now"},
        {"chunk_id": "t-001-c000", "doc_id": "t-001", "topic": topic_dir, "order": 0,
         "text": "真实有用的内容", "embedding_model": "test", "embedded_at": "now"},
    ]
    (index_dir / "entries.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8"
    )
    (index_dir / "chunks.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks) + "\n", encoding="utf-8"
    )
    vector_store.add_chunks(topic_dir, ["t-000-c000", "t-001-c000"], [[1.0, 0.0], [0.0, 1.0]])
    return index_dir


def test_remove_from_index_removes_matching_doc_only(tmp_path, monkeypatch):
    topic = "测试主题"
    index_dir = _seed_index(topic, monkeypatch, tmp_path)

    result = remove_from_index(topic, ["t-000"])

    assert [e["doc_id"] for e in result["removed_entries"]] == ["t-000"]
    assert result["removed_chunk_ids"] == ["t-000-c000"]

    remaining_entries = [json.loads(l) for l in (index_dir / "entries.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["doc_id"] for e in remaining_entries] == ["t-001"]

    remaining_chunks = [json.loads(l) for l in (index_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c["chunk_id"] for c in remaining_chunks] == ["t-001-c000"]

    # FAISS索引也要同步清理，不是只清entries/chunks.jsonl
    assert vector_store.load_id_map(topic) == ["t-001-c000"]


def test_remove_from_index_nonexistent_doc_id_is_noop(tmp_path, monkeypatch):
    topic = "测试主题"
    _seed_index(topic, monkeypatch, tmp_path)

    result = remove_from_index(topic, ["不存在的doc"])

    assert result == {"removed_entries": [], "removed_chunk_ids": []}
    assert len(vector_store.load_id_map(topic)) == 2


def test_remove_from_index_empty_doc_ids_is_noop(tmp_path, monkeypatch):
    topic = "测试主题"
    _seed_index(topic, monkeypatch, tmp_path)

    result = remove_from_index(topic, [])

    assert result == {"removed_entries": [], "removed_chunk_ids": []}
    assert len(vector_store.load_id_map(topic)) == 2


# ---------- ingest_topic 内容长度门禁（docs/ARCHITECTURE.md第8.1节）----------


def test_ingest_topic_skips_short_content_without_llm_call(tmp_path, monkeypatch):
    """原文短于20字的素材应该在_analyze_content之前就被跳过，不调用LLM，
    直接写入rejected.jsonl（不新开文件）。"""
    from src.materials import ingest

    topic = "测试主题"
    material_root = tmp_path
    monkeypatch.setattr(ingest, "MATERIAL_LIBRARY_DIR", material_root)

    raw_dir = material_root / "raw" / topic
    raw_dir.mkdir(parents=True)
    (raw_dir / "short.md").write_text("太短了", encoding="utf-8")  # 3字，低于20字阈值
    (raw_dir / "manifest.json").write_text(
        json.dumps({"items": [
            {"source_file": "short.md", "title": "短内容", "platform": "douyin"},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )

    def _fail_if_called(content, topic):
        raise AssertionError("内容过短应该在_analyze_content之前就被跳过，不应该调用LLM")

    class _FakeEmbeddings:
        model = "test-embedding-model"

        def embed_documents(self, texts):
            raise AssertionError("内容过短不应该走到embedding这一步")

    monkeypatch.setattr(ingest, "_analyze_content", _fail_if_called)
    monkeypatch.setattr(ingest, "get_embeddings", lambda: _FakeEmbeddings())

    new_entries = ingest.ingest_topic(topic)

    assert new_entries == []
    rejected_path = material_root / "index" / topic / "rejected.jsonl"
    rejected = [json.loads(l) for l in rejected_path.read_text(encoding="utf-8").splitlines()]
    assert len(rejected) == 1
    assert rejected[0]["source_file"] == "short.md"
    assert "内容过短" in rejected[0]["reason"]
    assert "3字" in rejected[0]["reason"]
    assert "20字阈值" in rejected[0]["reason"]

    # entries.jsonl不应该有新条目——短内容不算入库
    entries_path = material_root / "index" / topic / "entries.jsonl"
    assert not entries_path.exists() or entries_path.read_text(encoding="utf-8").strip() == ""


def test_next_doc_number_does_not_reuse_manifest_position_after_incremental_append(tmp_path, monkeypatch):
    """增量补采不能因为manifest插入/重排而复用历史doc_id。"""
    from src.materials import ingest as ingest_module

    monkeypatch.setattr(ingest_module, "MATERIAL_LIBRARY_DIR", tmp_path)
    index_dir = tmp_path / "index" / "成都"
    index_dir.mkdir(parents=True)
    (index_dir / "entries.jsonl").write_text(
        json.dumps({"doc_id": "成都-058", "source_file": "old-a.md"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"doc_id": "成都-103", "source_file": "old-b.md"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    assert ingest_module._next_doc_number("成都") == 104


def test_ingest_topic_content_at_threshold_boundary_not_skipped(tmp_path, monkeypatch):
    """长度检查用的是 < 阈值（严格小于），正好20字或以上不应该被长度门禁挡住，
    应该正常走到_analyze_content这一步（会被mock掉的relevance判断挡住，但这里只
    验证LLM确实被调用了，即没有被长度门禁误伤）。"""
    from src.materials import ingest

    topic = "测试主题"
    material_root = tmp_path
    monkeypatch.setattr(ingest, "MATERIAL_LIBRARY_DIR", material_root)

    raw_dir = material_root / "raw" / topic
    raw_dir.mkdir(parents=True)
    content_20_chars = "一二三四五六七八九十一二三四五六七八九十"  # 20字，正好等于阈值
    assert len(content_20_chars) == 20
    (raw_dir / "boundary.md").write_text(content_20_chars, encoding="utf-8")
    (raw_dir / "manifest.json").write_text(
        json.dumps({"items": [
            {"source_file": "boundary.md", "title": "边界内容", "platform": "douyin"},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )

    called = {"count": 0}

    class _FakeAnalysis:
        is_relevant = False
        reason = "mock拒绝，仅验证是否被调用"
        entities: list = []

    def _fake_analyze(content, topic):
        called["count"] += 1
        return _FakeAnalysis()

    monkeypatch.setattr(ingest, "_analyze_content", _fake_analyze)

    ingest.ingest_topic(topic)

    assert called["count"] == 1  # 正好20字应该走到LLM判断这一步，没被长度门禁挡住


# ---------- ingest_topic entities 程序化过滤（docs/ARCHITECTURE.md第8.1节"entities质量改进"）----------


def test_ingest_topic_filters_entities_not_in_original_content(tmp_path, monkeypatch):
    """模型编造了原文里没有出现的实体词时，应该被程序化过滤剔除，只保留能在原文里
    逐字找到的实体（允许_LANDMARK_SUFFIXES地标后缀剥离后再匹配），不整条拒绝这份素材。
    """
    from src.materials import ingest

    topic = "测试主题"
    material_root = tmp_path
    monkeypatch.setattr(ingest, "MATERIAL_LIBRARY_DIR", material_root)

    content = "今天去了武侯祠，还路过了太古里，感觉都很不错。"
    raw_dir = material_root / "raw" / topic
    raw_dir.mkdir(parents=True)
    (raw_dir / "doc.md").write_text(content, encoding="utf-8")
    (raw_dir / "manifest.json").write_text(
        json.dumps({"items": [
            {"source_file": "doc.md", "title": "游记", "platform": "douyin"},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )

    class _FakeAnalysis:
        is_relevant = True
        reason = "相关"
        entities = [
            "武侯祠博物馆",  # 原文只写"武侯祠"，剥离地标后缀"博物馆"后应该能匹配上，保留
            "太古里",  # 原文逐字出现，保留
            "宽窄巷子",  # 原文完全没提到，模型编造/引申，应该被过滤掉
        ]

    monkeypatch.setattr(ingest, "_analyze_content", lambda content, topic: _FakeAnalysis())

    class _FakeEmbeddings:
        model = "test-embedding-model"

        def embed_documents(self, texts):
            return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(ingest, "get_embeddings", lambda: _FakeEmbeddings())
    monkeypatch.setattr(vector_store, "add_chunks", lambda *a, **kw: None)

    new_entries = ingest.ingest_topic(topic)

    assert len(new_entries) == 1
    assert set(new_entries[0].entities) == {"武侯祠博物馆", "太古里"}
    assert "宽窄巷子" not in new_entries[0].entities


# ===================== timeliness_check（时效性核验）测试 =====================
# docs/ISSUES.md第99/95/100条：route_plan 的 notes 里提到的道路通行状态/限流信息
# 是否完整、是否过时。核验用 web_search 查证 + light model 比对，失败按放行兜底。
# 这些测试全部 mock web_search 和 light model，不产生真实 API 调用。


from src.materials import timeliness_check


class _FakeTimelinessModel:
    """模拟 get_chat_model().with_structured_output() 的返回。

    timeliness_check._judge_timeliness 里调用方式是
    model.with_structured_output(TimelinessVerdict, method="json_mode").invoke(prompt)
    ——返回的对象需要有 .with_structured_output() 方法，返回的对象再有 .invoke() 方法，
    最终返回 TimelinessVerdict 实例。
    """

    def __init__(self, verdict):
        self._verdict = verdict

    def with_structured_output(self, schema, method=None):
        outer = self

        class _Structured:
            def invoke(self_inner, prompt):
                return outer._verdict

        return _Structured()


def _make_web_search_tool_mock(return_value: str, calls: list | None = None):
    """构造一个假的 web_search tool 对象，模拟 make_web_search_tools()[0]。

    timeliness_check 里调用方式是 web_search_tool.invoke({"query": ...})，
    返回 str。这里记录每次 query 到 calls（供断言用）。
    """

    class _FakeTool:
        def invoke(self, payload):
            if calls is not None:
                calls.append(payload.get("query", ""))
            return return_value

    return _FakeTool()


def _write_route_with_notes(project_id, day, notes, monkeypatch, tmp_path):
    """辅助：往临时project写一条带指定notes的route_plan。"""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    store.init_project(project_id)
    from src.workspace.schema import RouteDay, RoutePlan

    plan = RoutePlan(
        days=[
            RouteDay(
                day=day,
                city="测试城市",
                area="测试区域",
                notes=notes,
                pois=[],
                sources=[],
            )
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)


def test_timeliness_flags_missing_segments_when_notes_incomplete(tmp_path, monkeypatch):
    # docs/ISSUES.md第99条 Day8 真实案例的复刻：
    # notes 只提了"达坂山封闭"，web 查到 G227 三段封闭（达坂山+景阳岭+峨堡-扁都口），
    # light model 判定 missing_in_notes 非空 → 应该报问题。
    project_id = "timeliness-day8-incomplete"
    notes = (
        "返程日张掖翻越祁连山回西宁。⚠️达坂山上下山路段（铁迈煤矿至黑泉水库）"
        "2026年5月1日至2027年7月31日全封闭施工，绕行G0611张汶高速。"
    )
    _write_route_with_notes(project_id, 8, notes, monkeypatch, tmp_path)

    # mock web_search 返回含三段封闭的公告
    web_result = (
        "- 青海省交通运输厅公告：G227达坂山上下山路段全封闭至2027年7月31日；"
        "景阳岭至达坂山半幅通行；峨堡至扁都口至察汗河段全封闭至2027年7月31日。"
    )
    calls = []
    monkeypatch.setattr(
        timeliness_check,
        "make_web_search_tools",
        lambda *a, **k: [_make_web_search_tool_mock(web_result, calls)],
    )

    # mock light model 返回：web有时效信息，notes漏了景阳岭段+峨堡-扁都口段
    from src.materials.timeliness_check import TimelinessVerdict

    verdict = TimelinessVerdict(
        has_timeliness_info=True,
        missing_in_notes=["景阳岭至达坂山半幅通行", "峨堡至扁都口至察汗河段全封闭"],
        reason="notes只提了达坂山，漏了景阳岭段和峨堡-扁都口段",
    )
    monkeypatch.setattr(
        timeliness_check,
        "get_chat_model",
        lambda temperature=0: _FakeTimelinessModel(verdict),
    )

    issues = timeliness_check.verify_route_timeliness(project_id, "青甘大环线")
    assert len(issues) == 1
    assert issues[0]["day"] == 8
    assert "景阳岭至达坂山半幅通行" in issues[0]["missing_in_notes"]
    assert "峨堡至扁都口至察汗河段全封闭" in issues[0]["missing_in_notes"]
    # 确认 web_search 被调用了，且 query 含道路标识（不是笼统措辞）
    assert len(calls) == 1
    assert "达坂山" in calls[0]


def test_timeliness_passes_when_notes_complete(tmp_path, monkeypatch):
    # 配对：notes 完整反映了 web 查到的三段封闭 → light model 判定 missing_in_notes 为空 → 不报问题
    project_id = "timeliness-day8-complete"
    notes = (
        "返程日张掖翻越祁连山回西宁。⚠️G227三段封闭：①达坂山全封闭绕行G0611；"
        "②景阳岭至达坂山半幅通行；③峨堡至扁都口至察汗河段全封闭。"
    )
    _write_route_with_notes(project_id, 8, notes, monkeypatch, tmp_path)

    web_result = "- 青海省交通运输厅公告：G227达坂山+景阳岭+峨堡-扁都口三段封闭。"
    monkeypatch.setattr(
        timeliness_check,
        "make_web_search_tools",
        lambda *a, **k: [_make_web_search_tool_mock(web_result)],
    )

    from src.materials.timeliness_check import TimelinessVerdict

    verdict = TimelinessVerdict(
        has_timeliness_info=True,
        missing_in_notes=[],  # notes完整反映，没有缺失
        reason="notes已完整反映三段封闭",
    )
    monkeypatch.setattr(
        timeliness_check,
        "get_chat_model",
        lambda temperature=0: _FakeTimelinessModel(verdict),
    )

    issues = timeliness_check.verify_route_timeliness(project_id, "青甘大环线")
    assert issues == []


def test_timeliness_reports_structure_change_even_when_notes_complete(tmp_path, monkeypatch):
    """notes写全风险不等于路线可执行；结构问题不能被空missing_in_notes吞掉。"""
    project_id = "timeliness-structure-change-with-complete-notes"
    notes = "沿G227行驶。官方已公告全线封闭，当前路线无法执行。"
    _write_route_with_notes(project_id, 1, notes, monkeypatch, tmp_path)

    web_result = "- 交通主管部门公告：G227全线封闭，原路线无法通行。"
    monkeypatch.setattr(
        timeliness_check,
        "make_web_search_tools",
        lambda *a, **k: [_make_web_search_tool_mock(web_result)],
    )

    from src.materials.timeliness_check import TimelinessVerdict

    verdict = TimelinessVerdict(
        has_timeliness_info=True,
        missing_in_notes=[],
        reason="notes已经写明封闭，但原路线仍不可执行",
        requires_structure_change=True,
        structure_change_reason="必须更换道路或调整当天路线",
    )
    monkeypatch.setattr(
        timeliness_check,
        "get_chat_model",
        lambda temperature=0: _FakeTimelinessModel(verdict),
    )

    issues = timeliness_check.verify_route_timeliness(project_id, "测试路线")
    assert len(issues) == 1
    assert issues[0]["missing_in_notes"] == []
    assert issues[0]["requires_structure_change"] is True
    assert issues[0]["structure_change_reason"] == "必须更换道路或调整当天路线"


def test_timeliness_skips_day_without_road_identifiers(tmp_path, monkeypatch):
    # notes 没提任何道路标识（G227/达坂山等）→ 不该核验这天，跳过
    # （留给陪伴阶段 realtime_agent 兜底，不在生成阶段核验，控制成本）
    project_id = "timeliness-no-road-id"
    notes = "上午游览武侯祠，下午逛宽窄巷子，全程无长途驾驶。"  # 无道路标识
    _write_route_with_notes(project_id, 1, notes, monkeypatch, tmp_path)

    # mock 一个会报错的 web_search，确认根本没被调用
    def _boom_tool(payload):
        raise AssertionError("web_search不该被调用——notes没有道路标识就该跳过")

    class _BoomTool:
        invoke = staticmethod(_boom_tool)

    monkeypatch.setattr(timeliness_check, "make_web_search_tools", lambda *a, **k: [_BoomTool()])
    # light model 也mock成会报错，确认没被调用
    def _boom_lm(temperature=0):
        raise AssertionError("light model不该被调用")

    monkeypatch.setattr(timeliness_check, "get_chat_model", _boom_lm)

    issues = timeliness_check.verify_route_timeliness(project_id, "成都")
    assert issues == []


def test_timeliness_passes_when_web_has_no_timeliness_info(tmp_path, monkeypatch):
    # web 查到的是旅游团广告（无封闭/施工信息）→ light model 判定
    # has_timeliness_info=False → 不报问题（这条路当前可能确实没封闭）
    project_id = "timeliness-web-no-info"
    notes = "返程日沿G227返回西宁。"  # 有道路标识G227，会触发web查
    _write_route_with_notes(project_id, 8, notes, monkeypatch, tmp_path)

    web_result = "- 某旅游团广告：青甘大环线8天7晚人均3000，西宁进出，必打卡青海湖。"  # 无时效信息
    monkeypatch.setattr(
        timeliness_check,
        "make_web_search_tools",
        lambda *a, **k: [_make_web_search_tool_mock(web_result)],
    )

    from src.materials.timeliness_check import TimelinessVerdict

    verdict = TimelinessVerdict(
        has_timeliness_info=False,  # web没查到时效信息
        missing_in_notes=[],
        reason="web结果只是旅游团广告，无封闭/施工信息",
    )
    monkeypatch.setattr(
        timeliness_check,
        "get_chat_model",
        lambda temperature=0: _FakeTimelinessModel(verdict),
    )

    issues = timeliness_check.verify_route_timeliness(project_id, "青甘大环线")
    assert issues == []


def test_timeliness_passes_when_web_search_fails(tmp_path, monkeypatch):
    # 失败兜底：web_search 工具抛异常 → 按放行处理，不报问题也不阻塞
    # （时效核验是增强不是阻塞，参考 _filter_relevant_candidates 的 try/except 模式）
    project_id = "timeliness-web-fail"
    notes = "返程日沿G227返回西宁。"
    _write_route_with_notes(project_id, 8, notes, monkeypatch, tmp_path)

    class _FailTool:
        def invoke(self, payload):
            raise RuntimeError("模拟web_search key全耗尽")

    monkeypatch.setattr(timeliness_check, "make_web_search_tools", lambda *a, **k: [_FailTool()])
    # light model mock成会报错，确认没被调用（web失败就该跳过，不调light model）
    def _boom_lm(temperature=0):
        raise AssertionError("web失败时light model不该被调用")

    monkeypatch.setattr(timeliness_check, "get_chat_model", _boom_lm)

    issues = timeliness_check.verify_route_timeliness(project_id, "青甘大环线")
    assert issues == []


def test_timeliness_passes_when_light_model_fails(tmp_path, monkeypatch):
    # 失败兜底：light model 判定异常 → 按放行处理，不报问题也不阻塞
    project_id = "timeliness-llm-fail"
    notes = "返程日沿G227返回西宁，达坂山封闭施工。"
    _write_route_with_notes(project_id, 8, notes, monkeypatch, tmp_path)

    web_result = "- 青海省交通运输厅公告：G227达坂山封闭。"
    monkeypatch.setattr(
        timeliness_check,
        "make_web_search_tools",
        lambda *a, **k: [_make_web_search_tool_mock(web_result)],
    )
    # light model mock成抛异常
    def _fail_lm(temperature=0):
        raise RuntimeError("模拟LLM判定异常")

    monkeypatch.setattr(timeliness_check, "get_chat_model", _fail_lm)

    issues = timeliness_check.verify_route_timeliness(project_id, "青甘大环线")
    assert issues == []


def test_timeliness_days_filter_narrows_scope(tmp_path, monkeypatch):
    # edit模式 scoping：days=[8] 时只核验day8，day1即使有道路标识也不核验
    project_id = "timeliness-days-filter"
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    store.init_project(project_id)
    from src.workspace.schema import RouteDay, RoutePlan

    plan = RoutePlan(
        days=[
            RouteDay(day=1, city="西宁", area="市区", notes="沿G227去祁连，达坂山封闭", pois=[], sources=[]),
            RouteDay(day=8, city="张掖", area="返程", notes="沿G227回西宁", pois=[], sources=[]),
        ]
    )
    store.write_route_plan(project_id, actor="route_agent", route_plan=plan)

    calls = []
    web_result = "- 公告：G227封闭。"
    monkeypatch.setattr(
        timeliness_check,
        "make_web_search_tools",
        lambda *a, **k: [_make_web_search_tool_mock(web_result, calls)],
    )

    from src.materials.timeliness_check import TimelinessVerdict

    verdict = TimelinessVerdict(
        has_timeliness_info=True,
        missing_in_notes=["峨堡至扁都口段"],
        reason="day8 notes漏了峨堡-扁都口段",
    )
    monkeypatch.setattr(
        timeliness_check,
        "get_chat_model",
        lambda temperature=0: _FakeTimelinessModel(verdict),
    )

    # 只核验day8
    issues = timeliness_check.verify_route_timeliness(project_id, "青甘大环线", days=[8])
    assert len(issues) == 1
    assert issues[0]["day"] == 8
    # web_search 只该被调用1次（day8），不该查day1
    assert len(calls) == 1


def test_extract_road_identifiers_finds_g_and_landmark():
    # 道路标识抽取：G227 + 达坂山 都该抽到，城市名（张掖/西宁）不该抽到
    notes = "从张掖沿G227翻越达坂山回西宁，绕行G0611。"
    ids = timeliness_check._extract_road_identifiers(notes)
    assert "G227" in ids
    assert "G0611" in ids
    assert "达坂山" in ids
    # 城市名不该被当成道路标识
    assert "张掖" not in ids
    assert "西宁" not in ids


def test_extract_road_identifiers_normalizes_chinese_number():
    # "国道二二七" / "国道227" / "227国道" 都该归一成 G227（实测中文写法 web 召回差）
    for raw in ["国道二二七", "国道227", "227国道"]:
        ids = timeliness_check._extract_road_identifiers(f"沿{raw}行驶")
        # 抽出来后归一化拼进 query 应该是 G227
        query = timeliness_check._build_web_query(ids)
        assert "G227" in query, f"{raw} 应归一成 G227，实际 query={query}"


def test_build_web_query_empty_when_no_identifiers():
    # 没有道路标识时 query 该是空字符串（调用方据此跳过这天）
    assert timeliness_check._build_web_query([]) == ""


def test_build_web_query_uses_supplied_year():
    assert timeliness_check._build_web_query(["G227"], year=2028).endswith("2028")
