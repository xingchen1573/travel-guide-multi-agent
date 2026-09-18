"""回归测试：ingest_topic() 自动触发的洞察生成失败时，不能影响入库结果本身
（见与用户确认的设计：generate_insights失败只打日志，try/except吞掉，不向上抛）。
"""
from __future__ import annotations

import json

from src.materials import ingest, insights


class _FakeEmbeddings:
    model = "fake-embedding-model"

    def embed_documents(self, texts):
        return [[0.0] * 4 for _ in texts]


def _setup_topic(tmp_path, monkeypatch, topic: str):
    monkeypatch.setattr(ingest, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr("src.materials.vector_store.MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(ingest, "get_embeddings", lambda: _FakeEmbeddings())
    monkeypatch.setattr(
        ingest,
        "_analyze_content",
        lambda content, topic: ingest._ContentAnalysis(is_relevant=True, reason="ok", entities=["测试实体"]),
    )

    raw_dir = tmp_path / "raw" / topic
    raw_dir.mkdir(parents=True)
    (raw_dir / "manifest.json").write_text(
        json.dumps({"items": [{"source_file": "a_原文.md", "title": "a", "author": "作者甲"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 21字，需高于ingest.py的20字内容长度门禁阈值，否则会在_analyze_content之前就被跳过
    (raw_dir / "a_原文.md").write_text("这是一条关于测试实体的真实分享内容，非常详实。", encoding="utf-8")


def test_ingest_topic_succeeds_even_when_insight_generation_raises(tmp_path, monkeypatch):
    _setup_topic(tmp_path, monkeypatch, "测试目的地")

    def _raise(topic):
        raise RuntimeError("模拟LLM调用失败")

    monkeypatch.setattr(insights, "generate_insights", _raise)

    new_entries = ingest.ingest_topic("测试目的地")

    assert len(new_entries) == 1
    entries_path = tmp_path / "index" / "测试目的地" / "entries.jsonl"
    assert entries_path.exists()


def test_ingest_topic_calls_generate_insights_when_new_entries_added(tmp_path, monkeypatch):
    _setup_topic(tmp_path, monkeypatch, "测试目的地2")

    called_with = []
    monkeypatch.setattr(insights, "generate_insights", lambda topic: called_with.append(topic))

    ingest.ingest_topic("测试目的地2")

    assert called_with == ["测试目的地2"]


def test_ingest_topic_skips_insight_generation_when_nothing_new(tmp_path, monkeypatch):
    _setup_topic(tmp_path, monkeypatch, "测试目的地3")
    # 先跑一次，把这条素材变成"已入库"
    monkeypatch.setattr(insights, "generate_insights", lambda topic: None)
    ingest.ingest_topic("测试目的地3")

    called_with = []
    monkeypatch.setattr(insights, "generate_insights", lambda topic: called_with.append(topic))

    # 第二次跑（幂等，没有新entry）不应该再触发一次洞察生成
    new_entries = ingest.ingest_topic("测试目的地3")

    assert new_entries == []
    assert called_with == []
