"""src/materials/insights.py 的单元测试：全部 mock MiMo factory，不调真实 LLM。"""
from __future__ import annotations

import json

import pytest

from src.materials import insights


def _write_entries(index_dir, entries: list[dict]) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    with (index_dir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_chunks(index_dir, chunks: list[dict]) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    with (index_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")


class _FakeStructuredModel:
    """按调用顺序返回队列里的结果。队列为空时复用最后一个，第 first_n_to_raise 个调用
    抛 raise_exc（用于"某批失败不影响其余批"测试）。"""

    def __init__(self, results: list[insights._RawTopicInsights], raise_on_call=None, raise_exc=None):
        self._results = list(results)
        self._raise_on_call = raise_on_call
        self._raise_exc = raise_exc
        self._call_count = 0

    def invoke(self, prompt):
        self._call_count += 1
        if self._raise_on_call is not None and self._call_count == self._raise_on_call:
            raise self._raise_exc or RuntimeError("模拟某批 LLM 调用失败")
        if self._results:
            # 队列式：第 N 次调用取第 N 个结果，超出时复用最后一个（单批场景兼容）
            return self._results[min(self._call_count - 1, len(self._results) - 1)]
        return insights._RawTopicInsights()


class _FakeChatModel:
    """构造时传 results（list，每个元素是一次 invoke 的返回值）。单批场景传 1 个即可。"""

    def __init__(self, results: list[insights._RawTopicInsights], raise_on_call=None, raise_exc=None):
        self._results = results
        self._raise_on_call = raise_on_call
        self._raise_exc = raise_exc

    def with_structured_output(self, schema, method=None):
        assert schema is insights._RawTopicInsights
        return _FakeStructuredModel(self._results, raise_on_call=self._raise_on_call, raise_exc=self._raise_exc)


def _five_entries(topic: str) -> list[dict]:
    return [
        {"doc_id": f"{topic}-00{i}", "topic": topic, "source_file": f"f{i}.md", "platform": "bilibili_asr",
         "author": f"作者{i}", "entities": [f"实体{i}"]}
        for i in range(5)
    ]


def _make_fake_model(raw: insights._RawTopicInsights):
    """单结果 fake（向后兼容现有单批测试）。"""
    return _FakeChatModel(results=[raw])


def test_generate_insights_skips_when_below_min_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    index_dir = tmp_path / "index" / "峨眉山"
    _write_entries(index_dir, [{"doc_id": "峨眉山-000", "topic": "峨眉山", "author": "a", "entities": []}])

    def _fail_if_called(temperature=0):
        raise AssertionError("entry数<5时不应该调用LLM")

    monkeypatch.setattr(insights, "get_structured_chat_model", _fail_if_called)

    result = insights.generate_insights("峨眉山")

    assert result is None
    assert not (index_dir / "insights.json").exists()


def test_confidence_consensus_requires_two_distinct_authors(tmp_path, monkeypatch):
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic = "成都"
    index_dir = tmp_path / "index" / topic
    entries = _five_entries(topic)
    _write_entries(index_dir, entries)
    _write_chunks(index_dir, [
        {"chunk_id": f"{e['doc_id']}-c000", "doc_id": e["doc_id"], "topic": topic, "order": 0,
         "text": f"这里提到了实体{i}，非常好吃"}
        for i, e in enumerate(entries)
    ])

    raw = insights._RawTopicInsights(
        must_visit=[],
        food=[
            insights._RawInsightItem(summary="实体0很受欢迎", doc_ids=["成都-000", "成都-001"]),
            insights._RawInsightItem(summary="实体2值得一试", doc_ids=["成都-002"]),
        ],
        pitfalls=[], tips=[], other=[],
    )
    monkeypatch.setattr(insights, "get_structured_chat_model", lambda temperature=0: _make_fake_model(raw))

    result = insights.generate_insights(topic)

    assert result is not None
    assert len(result.food) == 2
    consensus_item = next(i for i in result.food if "实体0" in i.summary)
    single_source_item = next(i for i in result.food if "实体2" in i.summary)
    assert consensus_item.confidence == "consensus"
    assert single_source_item.confidence == "single_source"


def test_author_missing_does_not_count_toward_consensus(tmp_path, monkeypatch):
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic = "成都"
    index_dir = tmp_path / "index" / topic
    entries = _five_entries(topic)
    entries[0]["author"] = ""  # 缺失author，不该被当成一个额外来源
    entries[1]["author"] = ""
    _write_entries(index_dir, entries)
    _write_chunks(index_dir, [
        {"chunk_id": f"{e['doc_id']}-c000", "doc_id": e["doc_id"], "topic": topic, "order": 0,
         "text": f"这里提到了实体{i}"}
        for i, e in enumerate(entries)
    ])

    raw = insights._RawTopicInsights(
        must_visit=[insights._RawInsightItem(summary="实体0是热门景点", doc_ids=["成都-000", "成都-001"])],
        food=[], pitfalls=[], tips=[], other=[],
    )
    monkeypatch.setattr(insights, "get_structured_chat_model", lambda temperature=0: _make_fake_model(raw))

    result = insights.generate_insights(topic)

    assert result is not None
    assert len(result.must_visit) == 1
    assert result.must_visit[0].confidence == "single_source"


def test_ungrounded_conclusion_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic = "成都"
    index_dir = tmp_path / "index" / topic
    entries = _five_entries(topic)
    _write_entries(index_dir, entries)
    _write_chunks(index_dir, [
        {"chunk_id": f"{e['doc_id']}-c000", "doc_id": e["doc_id"], "topic": topic, "order": 0,
         "text": f"这里提到了实体{i}"}
        for i, e in enumerate(entries)
    ])

    raw = insights._RawTopicInsights(
        must_visit=[],
        food=[],
        pitfalls=[
            # summary完全没提到该doc_id下ingest时抽取的entity（"实体0"），核验应判定不成立
            insights._RawInsightItem(summary="这家店服务态度很差，排队两小时", doc_ids=["成都-000"]),
        ],
        tips=[], other=[],
    )
    monkeypatch.setattr(insights, "get_structured_chat_model", lambda temperature=0: _make_fake_model(raw))

    result = insights.generate_insights(topic)

    assert result is not None
    assert result.pitfalls == []


def test_insights_json_is_overwritten_not_appended(tmp_path, monkeypatch):
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic = "成都"
    index_dir = tmp_path / "index" / topic
    entries = _five_entries(topic)
    _write_entries(index_dir, entries)
    _write_chunks(index_dir, [
        {"chunk_id": f"{e['doc_id']}-c000", "doc_id": e["doc_id"], "topic": topic, "order": 0,
         "text": f"这里提到了实体{i}"}
        for i, e in enumerate(entries)
    ])

    raw1 = insights._RawTopicInsights(
        must_visit=[insights._RawInsightItem(summary="实体0值得一去", doc_ids=["成都-000", "成都-001"])],
        food=[], pitfalls=[], tips=[], other=[],
    )
    monkeypatch.setattr(insights, "get_structured_chat_model", lambda temperature=0: _make_fake_model(raw1))
    insights.generate_insights(topic)

    raw2 = insights._RawTopicInsights(
        must_visit=[],
        food=[insights._RawInsightItem(summary="实体2好吃", doc_ids=["成都-002", "成都-003"])],
        pitfalls=[], tips=[], other=[],
    )
    monkeypatch.setattr(insights, "get_structured_chat_model", lambda temperature=0: _make_fake_model(raw2))
    insights.generate_insights(topic)

    on_disk = json.loads((index_dir / "insights.json").read_text(encoding="utf-8"))
    assert on_disk["must_visit"] == []  # 第一次写入的内容应该已经被整份覆盖，不是累加
    assert len(on_disk["food"]) == 1
    assert "实体2" in on_disk["food"][0]["summary"]


# ---------- 分批喂入新增测试（2026-08-09） ----------

def _huge_entries(topic: str, n: int, chars_per_entry: int) -> list[dict]:
    """构造 n 条每条 chars_per_entry 字符的 entries，强制分批（单批超过 _BATCH_CHAR_BUDGET）。"""
    return [
        {"doc_id": f"{topic}-{i:03d}", "topic": topic, "source_file": f"f{i}.md",
         "platform": "bilibili_asr", "author": f"作者{i}", "entities": [f"实体{i}"]}
        for i in range(n)
    ]


def _huge_chunks(topic: str, entries: list[dict], chars_per_entry: int) -> list[dict]:
    text = "素材" * (chars_per_entry // 2)
    return [
        {"chunk_id": f"{e['doc_id']}-c000", "doc_id": e["doc_id"], "topic": topic, "order": 0, "text": text}
        for e in entries
    ]


def test_split_entries_into_batches_splits_by_char_budget():
    """贪心按字符预算切批：3 条每条 8000 字符（_BATCH_CHAR_BUDGET=20000）→ 一批（24000 > 20000，
    但前 2 条 16000 不超，加第 3 条 24000 超 → 第 1 批 2 条 + 第 2 批 1 条）。"""
    entries = [{"doc_id": f"d-{i}"} for i in range(3)]
    chunks_by_doc = {f"d-{i}": [{"text": "x" * 8000}] for i in range(3)}
    batches = insights._split_entries_into_batches(entries, chunks_by_doc, budget=20000)
    assert len(batches) == 2
    assert [len(b) for b in batches] == [2, 1]


def test_split_single_oversized_entry_is_own_batch():
    """单条 entry 超过 budget 时单独成批，不拆 entry（保 doc_id→文本原子性）。"""
    entries = [{"doc_id": "d-0"}, {"doc_id": "d-1"}]
    chunks_by_doc = {"d-0": [{"text": "x" * 30000}], "d-1": [{"text": "y" * 100}]}
    batches = insights._split_entries_into_batches(entries, chunks_by_doc, budget=20000)
    # d-0 单独一批（30000 > 20000），d-1 单独一批
    assert len(batches) == 2
    assert batches[0] == [{"doc_id": "d-0"}]
    assert batches[1] == [{"doc_id": "d-1"}]


def test_batches_are_merged_across_invokes(tmp_path, monkeypatch):
    """分批跑出多批 _RawTopicInsights，合并后写盘的 insights.json 同时包含两批的结论。"""
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic = "青甘大环线"
    index_dir = tmp_path / "index" / topic
    # 6 条每条 12000 字符 → 总 72000 > 20000，分批：1/1/1/1/1/1（12000*2=24000>20000 每批1条）共6批
    entries = _huge_entries(topic, 6, 12000)
    _write_entries(index_dir, entries)
    _write_chunks(index_dir, _huge_chunks(topic, entries, 12000))

    # 两批 fake 结果：批1 给 must_visit，批2 给 food（后续批用 must_visit 复用最后一个）。
    # 注意 entities=["实体0"]/["实体1"]，summary 必须含实体才过 grounding。
    raw_results = [
        insights._RawTopicInsights(
            must_visit=[insights._RawInsightItem(summary="实体0是必去景点", doc_ids=["青甘大环线-000"])],
            food=[], pitfalls=[], tips=[], other=[],
        ),
        insights._RawTopicInsights(
            must_visit=[],
            food=[insights._RawInsightItem(summary="实体1很好吃", doc_ids=["青甘大环线-001"])],
            pitfalls=[], tips=[], other=[],
        ),
    ]
    fake = _FakeChatModel(results=raw_results)
    monkeypatch.setattr(insights, "get_structured_chat_model", lambda temperature=0: fake)

    result = insights.generate_insights(topic)

    assert result is not None
    assert result.model == insights.DEEPSEEK_CHAT_MODEL
    # 合并后 must_visit 含实体0、food 含实体1（两批结论都在）
    assert any("实体0" in i.summary for i in result.must_visit)
    assert any("实体1" in i.summary for i in result.food)
    on_disk = json.loads((index_dir / "insights.json").read_text(encoding="utf-8"))
    assert on_disk["based_on_entry_count"] == 6
    assert any("实体0" in i["summary"] for i in on_disk["must_visit"])
    assert any("实体1" in i["summary"] for i in on_disk["food"])


def test_one_batch_failure_does_not_drop_others(tmp_path, monkeypatch, capsys):
    """第 2 批 invoke 抛异常，断言第 1 批的结论仍写盘、不抛、打日志（print 到 stdout）。"""
    monkeypatch.setattr(insights, "MATERIAL_LIBRARY_DIR", tmp_path)
    topic = "青甘大环线"
    index_dir = tmp_path / "index" / topic
    entries = _huge_entries(topic, 6, 12000)
    _write_entries(index_dir, entries)
    _write_chunks(index_dir, _huge_chunks(topic, entries, 12000))

    raw_results = [
        insights._RawTopicInsights(
            must_visit=[insights._RawInsightItem(summary="实体0是必去景点", doc_ids=["青甘大环线-000"])],
            food=[], pitfalls=[], tips=[], other=[],
        ),
    ]
    # 第 1 次调用返回 raw_results[0]，第 2 次调用抛异常（raise_on_call=2）
    fake = _FakeChatModel(results=raw_results, raise_on_call=2, raise_exc=RuntimeError("模拟第2批失败"))
    monkeypatch.setattr(insights, "get_structured_chat_model", lambda temperature=0: fake)

    import logging
    result = insights.generate_insights(topic)

    assert result is not None  # 不抛
    # 第 1 批的结论仍在
    assert any("实体0" in i.summary for i in result.must_visit)
    # 源码用 print 输出"第 2 批失败，跳过"，capsys 捕 stdout 校验
    out = capsys.readouterr().out
    assert "第 2/6 批失败" in out
    assert "模拟第2批失败" in out
    # 后续批仍执行（第3批完成的 print 也要在）
    assert "第 3/6 批完成" in out
