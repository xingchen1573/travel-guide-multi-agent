"""src/materials/collectors/bilibili.py 的单元测试（纯逻辑部分，用tmp_path隔离，不联网）。

    python -m pytest tests/test_collectors_bilibili.py -v
"""
import time

import pytest

import src.materials.collectors.bilibili as cb
import src.materials.collectors.ingest_to_raw as m

_RECENT_PUBDATE = int(time.time()) - 30 * 24 * 3600  # 30天前，确保通过硬过滤(不超过2年)


def test_load_existing_source_urls_empty_when_no_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    assert cb._load_existing_source_urls("成都") == set()


def test_load_existing_source_urls_reads_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    m.ingest_to_raw("成都", "标题A", "正文", "bilibili_asr", source_url="https://www.bilibili.com/video/BV1AAA")
    m.ingest_to_raw("成都", "标题B", "正文", "bilibili_asr", source_url="https://www.bilibili.com/video/BV2BBB")

    urls = cb._load_existing_source_urls("成都")

    assert urls == {
        "https://www.bilibili.com/video/BV1AAA",
        "https://www.bilibili.com/video/BV2BBB",
    }


def test_search_and_rank_excludes_already_ingested(tmp_path, monkeypatch):
    """核心去重预过滤测试：docs/ISSUES.md第14条——已入库的source_url不应该出现在
    排序结果里，避免对着库里已有的视频重新下载+ASR白白浪费成本。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    m.ingest_to_raw("成都", "已入库的视频", "正文", "bilibili_asr", source_url="https://www.bilibili.com/video/BV1OLD")

    fake_results = [
        {"type": "video", "bvid": "BV1OLD", "duration": "12:00", "play": 1_000_000, "like": 50_000, "pubdate": _RECENT_PUBDATE},
        {"type": "video", "bvid": "BV2NEW", "duration": "12:00", "play": 500_000, "like": 20_000, "pubdate": _RECENT_PUBDATE},
    ]
    monkeypatch.setattr(cb, "_search", lambda keyword, page=1: _async_return(fake_results))

    ranked = cb.search_and_rank(["成都旅游攻略"], top_n=10, topic="成都")

    bvids = [c["bvid"] for c in ranked]
    assert "BV1OLD" not in bvids
    assert "BV2NEW" in bvids


def test_search_and_rank_without_topic_skips_dedup_filter(tmp_path, monkeypatch):
    # 不传topic时跳过"已入库"过滤，只做同批次内部bvid去重，行为和改动前一致
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    fake_results = [
        {"type": "video", "bvid": "BV1", "duration": "12:00", "play": 1_000_000, "like": 50_000, "pubdate": _RECENT_PUBDATE},
    ]
    monkeypatch.setattr(cb, "_search", lambda keyword, page=1: _async_return(fake_results))

    ranked = cb.search_and_rank(["成都旅游攻略"], top_n=10)

    assert [c["bvid"] for c in ranked] == ["BV1"]


def test_search_and_rank_passes_page_through(monkeypatch):
    # 新增分页支持（Phase B需要agent能换页重搜），验证page参数确实透传给_search
    captured_pages = []

    async def fake_search(keyword, page=1):
        captured_pages.append(page)
        return []

    monkeypatch.setattr(cb, "_search", fake_search)
    cb.search_and_rank(["成都旅游攻略"], top_n=10, page=2)

    assert captured_pages == [2]


async def _async_return(value):
    return value


# ---------- collect_one 对pubdate缺失的兜底（2026-07-25真实E2E复现的bug）----------
# 真实场景复现：propose_candidates工具的docstring只要求candidates含
# platform/source_url/title/author/score，没提pubdate；agent严格按这份规格
# 重新组装候选提交时，如实地把没被要求的pubdate字段丢掉了——不是模型编造/疏漏，
# 是工具文档本身没要求。collect_one此前硬编码candidate["pubdate"]直接崩溃
# （KeyError），不应该让"人工确认后的候选字典缺一个非核心字段"整个采集流程直接崩掉。


def test_collect_one_defaults_pubdate_to_zero_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cb, "download_mp3", lambda url: tmp_path / "fake.mp3")
    (tmp_path / "fake.mp3").write_text("fake audio", encoding="utf-8")
    monkeypatch.setattr(cb, "transcribe", lambda mp3_path: tmp_path / "fake.txt")
    (tmp_path / "fake.txt").write_text("转写内容", encoding="utf-8")

    candidate = {
        "title": "测试视频",
        "author": "测试UP主",
        "source_url": "https://www.bilibili.com/video/BV1TEST",
        # 故意不含pubdate，模拟真实E2E里propose_candidates规格缺失导致的场景
    }

    entry = cb.collect_one("成都", candidate)

    assert entry is not None
    assert entry["publish_date"] == ""  # ingest_to_raw._to_publish_date对falsy值返回空字符串


def test_collect_one_uses_pubdate_when_present(tmp_path, monkeypatch):
    """回归防护：正常有pubdate字段时，行为不应该受这次兜底修改影响。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(cb, "download_mp3", lambda url: tmp_path / "fake2.mp3")
    (tmp_path / "fake2.mp3").write_text("fake audio", encoding="utf-8")
    monkeypatch.setattr(cb, "transcribe", lambda mp3_path: tmp_path / "fake2.txt")
    (tmp_path / "fake2.txt").write_text("转写内容", encoding="utf-8")

    candidate = {
        "title": "测试视频2",
        "author": "测试UP主2",
        "source_url": "https://www.bilibili.com/video/BV2TEST",
        "pubdate": _RECENT_PUBDATE,
    }

    entry = cb.collect_one("成都", candidate)

    assert entry is not None
    assert entry["publish_date"] != ""  # 有pubdate时应该被转成非空的日期字符串


# ---------------------------------------------------------------------------
# check_collector_dependencies（docs/ISSUES.md第77条：此前ffmpeg缺失会被静默吞掉，
# 误诊断成"素材库该目的地真的搜不到内容"，改成显式前置检查，缺失时立刻报错）
# ---------------------------------------------------------------------------


def test_check_collector_dependencies_passes_when_ffmpeg_and_key_present(monkeypatch):
    monkeypatch.setattr(cb.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("src.config.MIMO_API_KEYS", ["sk-fake"])
    monkeypatch.setattr("src.config.SILICONFLOW_ASR_API_KEYS", [])

    cb.check_collector_dependencies()  # 不应该抛异常


def test_check_collector_dependencies_passes_when_only_silicon_key_present(monkeypatch):
    """2026-08-04起硅基 TeleSpeechASR 是ASR主渠道、MiMo降为兜底。只配硅基 key、
    不配 MiMo key 也应该放行——旧的"必须有MIMO_API_KEYS"检查会把这种合法配置误拦。"""
    monkeypatch.setattr(cb.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("src.config.MIMO_API_KEYS", [])
    monkeypatch.setattr("src.config.SILICONFLOW_ASR_API_KEYS", ["sk-sf-fake"])

    cb.check_collector_dependencies()  # 不应该抛异常


def test_check_collector_dependencies_raises_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(cb.shutil, "which", lambda name: None)
    monkeypatch.setattr("src.config.MIMO_API_KEYS", ["sk-fake"])
    monkeypatch.setattr("src.config.SILICONFLOW_ASR_API_KEYS", [])

    with pytest.raises(cb.CollectorDependencyError, match="ffmpeg"):
        cb.check_collector_dependencies()


def test_check_collector_dependencies_raises_when_only_ffprobe_missing(monkeypatch):
    monkeypatch.setattr(cb.shutil, "which", lambda name: None if name == "ffprobe" else f"/usr/bin/{name}")
    monkeypatch.setattr("src.config.MIMO_API_KEYS", ["sk-fake"])
    monkeypatch.setattr("src.config.SILICONFLOW_ASR_API_KEYS", [])

    with pytest.raises(cb.CollectorDependencyError, match="ffprobe"):
        cb.check_collector_dependencies()


def test_check_collector_dependencies_raises_when_both_asr_keys_missing(monkeypatch):
    """两个ASR渠道的key都没配才报错。2026-08-04起报错文案改成"ASR key"通用措辞，
    不再只提MIMO_API_KEY——因为硅基已经是主渠道。"""
    monkeypatch.setattr(cb.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("src.config.MIMO_API_KEYS", [])
    monkeypatch.setattr("src.config.SILICONFLOW_ASR_API_KEYS", [])

    with pytest.raises(cb.CollectorDependencyError, match="ASR"):
        cb.check_collector_dependencies()
