"""src/materials/collectors/douyin.py 的单元测试（纯逻辑部分，用tmp_path隔离，不联网）。

    python -m pytest tests/test_collectors_douyin.py -v
"""
import json
import time
from pathlib import Path

import src.materials.collectors.douyin as cd
import src.materials.collectors.ingest_to_raw as m

_RECENT_CREATE_TIME = int(time.time()) - 30 * 24 * 3600  # 30天前，确保通过硬过滤(不超过2年)


def _write_search_jsonl(mediacrawler_dir: Path, items: list[dict], filename: str = "search_contents_2026-07-17.jsonl") -> None:
    jsonl_dir = mediacrawler_dir / "data" / "douyin" / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(item, ensure_ascii=False) for item in items)
    (jsonl_dir / filename).write_text(lines + "\n", encoding="utf-8")


def _write_detail_jsonl(mediacrawler_dir: Path, items: list[dict], filename: str = "detail_contents_2026-07-17.jsonl") -> None:
    jsonl_dir = mediacrawler_dir / "data" / "douyin" / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(item, ensure_ascii=False) for item in items)
    (jsonl_dir / filename).write_text(lines + "\n", encoding="utf-8")


def test_list_ranked_candidates_excludes_already_ingested(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")
    m.ingest_to_raw("成都", "已入库", "正文", "douyin_asr", source_url="https://www.douyin.com/video/111")

    _write_search_jsonl(tmp_path / "MediaCrawler", [
        {"aweme_id": "111", "desc": "已入库的内容", "aweme_url": "https://www.douyin.com/video/111", "create_time": _RECENT_CREATE_TIME, "liked_count": "100000"},
        {"aweme_id": "222", "desc": "新内容", "aweme_url": "https://www.douyin.com/video/222", "create_time": _RECENT_CREATE_TIME, "liked_count": "50000"},
    ])

    candidates = cd.list_ranked_candidates("成都", top_n=10)

    aweme_ids = [c["aweme_id"] for c in candidates]
    assert "111" not in aweme_ids
    assert "222" in aweme_ids


def test_list_ranked_candidates_dedupes_across_multiple_jsonl_files(tmp_path, monkeypatch):
    # 同一个aweme_id可能出现在跨天多个jsonl文件里（多次search可能抓到同一条），应该只算一条
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")

    _write_search_jsonl(tmp_path / "MediaCrawler", [
        {"aweme_id": "333", "desc": "内容A", "aweme_url": "https://www.douyin.com/video/333", "create_time": _RECENT_CREATE_TIME, "liked_count": "1000"},
    ], filename="search_contents_2026-07-15.jsonl")
    _write_search_jsonl(tmp_path / "MediaCrawler", [
        {"aweme_id": "333", "desc": "内容A", "aweme_url": "https://www.douyin.com/video/333", "create_time": _RECENT_CREATE_TIME, "liked_count": "1000"},
    ], filename="search_contents_2026-07-16.jsonl")

    candidates = cd.list_ranked_candidates("成都", top_n=10)

    assert len(candidates) == 1


def test_list_ranked_candidates_filters_by_keyword(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")

    _write_search_jsonl(tmp_path / "MediaCrawler", [
        {"aweme_id": "444", "desc": "成都内容", "aweme_url": "https://www.douyin.com/video/444", "create_time": _RECENT_CREATE_TIME, "liked_count": "1000", "source_keyword": "成都旅游攻略"},
        {"aweme_id": "555", "desc": "无关内容", "aweme_url": "https://www.douyin.com/video/555", "create_time": _RECENT_CREATE_TIME, "liked_count": "1000", "source_keyword": "其他关键词"},
    ])

    candidates = cd.list_ranked_candidates("成都", top_n=10, keyword="成都旅游攻略")

    assert [c["aweme_id"] for c in candidates] == ["444"]


def test_list_ranked_candidates_empty_when_no_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")
    (tmp_path / "MediaCrawler" / "data" / "douyin" / "jsonl").mkdir(parents=True)

    candidates = cd.list_ranked_candidates("成都", top_n=10)

    assert candidates == []


# ---------- collect_one（从main()拆出的单条采集函数，供素材agent的采集图节点直接调用）----------


def test_collect_one_desc_only_when_content_long_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")
    monkeypatch.setattr(cd, "fetch_metadata", lambda aweme_ids: None)  # 跳过真实MediaCrawler调用
    _write_detail_jsonl(tmp_path / "MediaCrawler", [{
        "aweme_id": "666666666666666",
        "desc": "这是一段足够长的desc文案内容，包含具体的店名和路线信息，超过六十个字符，"
                "应该被认为内容充分不需要ASR，这是补充填充的文字让长度达标测试用例专用文本",
        "aweme_url": "https://www.douyin.com/video/666666666666666",
        "create_time": _RECENT_CREATE_TIME,
        "nickname": "测试UP",
    }])

    entry = cd.collect_one("成都", "https://www.douyin.com/video/666666666666666")

    assert entry is not None
    assert entry["platform"] == "douyin"  # 内容够长，不需要自动补ASR


def test_collect_one_returns_none_when_no_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")
    monkeypatch.setattr(cd, "fetch_metadata", lambda aweme_ids: None)
    (tmp_path / "MediaCrawler" / "data" / "douyin" / "jsonl").mkdir(parents=True)

    entry = cd.collect_one("成都", "https://www.douyin.com/video/777777777777777")

    assert entry is None


def test_collect_one_triggers_asr_when_content_too_thin(tmp_path, monkeypatch):
    """desc过短时collect_one应该自动调run_asr_and_ingest补ASR，返回ASR版本的条目。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")
    monkeypatch.setattr(cd, "fetch_metadata", lambda aweme_ids: None)
    _write_detail_jsonl(tmp_path / "MediaCrawler", [{
        "aweme_id": "888888888888888",
        "desc": "短文案",
        "aweme_url": "https://www.douyin.com/video/888888888888888",
        "create_time": _RECENT_CREATE_TIME,
        "nickname": "测试UP",
    }])

    asr_called_with = []

    def fake_run_asr_and_ingest(topic, urls):
        asr_called_with.append((topic, urls))
        m.ingest_to_raw(
            topic, "短文案", "ASR转写出来的完整内容，信息量比desc丰富得多", "douyin_asr",
            author="测试UP", source_url="https://www.douyin.com/video/888888888888888",
        )

    monkeypatch.setattr(cd, "run_asr_and_ingest", fake_run_asr_and_ingest)

    entry = cd.collect_one("成都", "https://www.douyin.com/video/888888888888888")

    assert asr_called_with == [("成都", ["https://www.douyin.com/video/888888888888888"])]
    assert entry is not None
    assert entry["platform"] == "douyin_asr"


# ---------- collect_many（批量版本，供material_collect_node一次性处理多条候选用）----------


def test_collect_many_fetches_all_metadata_in_one_call(tmp_path, monkeypatch):
    """核心回归测试：多条候选应该只调用一次fetch_metadata（=一次浏览器启动），不是每条一次。
    见docs/ISSUES.md：真实E2E验证时发现material_collect_node逐条调用collect_one导致
    N条候选开N次浏览器，改用collect_many批量处理修复。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")

    fetch_calls = []

    def fake_fetch_metadata(aweme_ids):
        fetch_calls.append(list(aweme_ids))

    monkeypatch.setattr(cd, "fetch_metadata", fake_fetch_metadata)
    _write_detail_jsonl(tmp_path / "MediaCrawler", [
        {
            "aweme_id": "111111111111111",
            "desc": "这是第一条足够长的desc文案内容，包含具体的店名和路线信息，超过六十个字符达标测试专用",
            "aweme_url": "https://www.douyin.com/video/111111111111111",
            "create_time": _RECENT_CREATE_TIME,
            "nickname": "UP1",
        },
        {
            "aweme_id": "222222222222222",
            "desc": "这是第二条足够长的desc文案内容，包含具体的店名和路线信息，超过六十个字符达标测试专用",
            "aweme_url": "https://www.douyin.com/video/222222222222222",
            "create_time": _RECENT_CREATE_TIME,
            "nickname": "UP2",
        },
    ])

    entries = cd.collect_many("成都", [
        "https://www.douyin.com/video/111111111111111",
        "https://www.douyin.com/video/222222222222222",
    ])

    assert len(fetch_calls) == 1  # 只调用了一次，不是两次
    assert set(fetch_calls[0]) == {"111111111111111", "222222222222222"}
    assert len(entries) == 2
    assert all(e is not None for e in entries)
    assert all(e["platform"] == "douyin" for e in entries)


def test_collect_many_returns_empty_list_for_empty_input():
    assert cd.collect_many("成都", []) == []


def test_collect_many_returns_none_for_all_when_no_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")
    monkeypatch.setattr(cd, "fetch_metadata", lambda aweme_ids: None)
    (tmp_path / "MediaCrawler" / "data" / "douyin" / "jsonl").mkdir(parents=True)

    entries = cd.collect_many("成都", [
        "https://www.douyin.com/video/333333333333333",
        "https://www.douyin.com/video/444444444444444",
    ])

    assert entries == [None, None]


def test_collect_many_batches_asr_for_thin_candidates(tmp_path, monkeypatch):
    """多条候选里desc过短的应该被合并成一次run_asr_and_ingest调用（批量ASR），不是逐条调用。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cd, "_MEDIACRAWLER_DIR", tmp_path / "MediaCrawler")
    monkeypatch.setattr(cd, "fetch_metadata", lambda aweme_ids: None)
    _write_detail_jsonl(tmp_path / "MediaCrawler", [
        {
            "aweme_id": "555555555555555",
            "desc": "短文案A",
            "aweme_url": "https://www.douyin.com/video/555555555555555",
            "create_time": _RECENT_CREATE_TIME,
            "nickname": "UP1",
        },
        {
            "aweme_id": "666666666666667",
            "desc": "短文案B",
            "aweme_url": "https://www.douyin.com/video/666666666666667",
            "create_time": _RECENT_CREATE_TIME,
            "nickname": "UP2",
        },
    ])

    asr_calls = []

    def fake_run_asr_and_ingest(topic, urls):
        asr_calls.append(list(urls))
        for url in urls:
            aweme_id = cd._extract_aweme_id(url)
            m.ingest_to_raw(
                topic, f"标题{aweme_id}", "ASR转写出来的完整内容", "douyin_asr",
                author="UP", source_url=url,
            )

    monkeypatch.setattr(cd, "run_asr_and_ingest", fake_run_asr_and_ingest)

    entries = cd.collect_many("成都", [
        "https://www.douyin.com/video/555555555555555",
        "https://www.douyin.com/video/666666666666667",
    ])

    assert len(asr_calls) == 1  # 批量补ASR，只调用一次，不是两次
    assert set(asr_calls[0]) == {
        "https://www.douyin.com/video/555555555555555",
        "https://www.douyin.com/video/666666666666667",
    }
    assert all(e["platform"] == "douyin_asr" for e in entries)
