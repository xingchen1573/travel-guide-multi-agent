"""src/materials/collectors/ingest_to_raw.py 的单元测试（纯逻辑，用tmp_path隔离，不碰真实素材库，不联网）。

    python -m pytest tests/test_collectors_ingest_to_raw.py -v
"""
from pathlib import Path

import src.materials.collectors.ingest_to_raw as m


def test_rejects_invalid_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    try:
        m.ingest_to_raw("成都", "标题", "正文", "bilibili")  # 存量格式，ASR/自动化链路不能用
        assert False, "应该拒绝platform=bilibili"
    except ValueError:
        pass


def test_basic_write_and_manifest_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    entry = m.ingest_to_raw(
        "成都", "测试<视频>标题", "这是一段转写正文。第二句。", "bilibili_asr",
        author="某UP主", source_url="https://www.bilibili.com/video/BV1",
        publish_date=1712196000, summary="导读内容",
    )
    assert entry is not None
    assert ":" not in entry["source_file"] and "<" not in entry["source_file"]
    assert entry["publish_date"] == "2024-04-04"
    assert (tmp_path / "raw" / "成都" / entry["source_file"]).exists()
    assert (tmp_path / "raw" / "成都" / entry["summary_file"]).exists()


def test_same_url_same_platform_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    url = "https://www.bilibili.com/video/BV1"
    e1 = m.ingest_to_raw("成都", "标题", "正文1", "bilibili_asr", source_url=url)
    e2 = m.ingest_to_raw("成都", "标题2", "正文2", "bilibili_asr", source_url=url)
    assert e1 is not None
    assert e2 is None  # 同(url, platform)重复，幂等跳过


def test_same_url_different_platform_both_kept():
    """核心回归测试：docs/ISSUES.md第11e条——同一视频的desc(douyin)和ASR(douyin_asr)
    是两条信息量不同的独立素材，不能被"同url=重复"误判掉，必须都保留且互不覆盖。"""
    import tempfile
    tmp_path = Path(tempfile.mkdtemp())
    m.MATERIAL_LIBRARY_DIR = tmp_path
    url = "https://www.douyin.com/video/7600763471677133166"

    e_desc = m.ingest_to_raw("成都", "流浪中国的第104站", "desc文案正文", "douyin", source_url=url)
    e_asr = m.ingest_to_raw("成都", "流浪中国的第104站", "ASR转写正文，信息量更丰富", "douyin_asr", source_url=url)

    assert e_desc is not None and e_asr is not None
    assert e_desc["source_file"] != e_asr["source_file"]  # 不能撞名覆盖

    desc_content = (tmp_path / "raw" / "成都" / e_desc["source_file"]).read_text(encoding="utf-8")
    asr_content = (tmp_path / "raw" / "成都" / e_asr["source_file"]).read_text(encoding="utf-8")
    assert "desc文案" in desc_content
    assert "ASR转写" in asr_content

    # 各自重复调用仍然正确幂等跳过（确认联合键两边都生效，不是只判断了一边）
    assert m.ingest_to_raw("成都", "x", "y", "douyin", source_url=url) is None
    assert m.ingest_to_raw("成都", "x", "y", "douyin_asr", source_url=url) is None


def test_disambiguate_filename_on_title_collision(tmp_path, monkeypatch):
    """docs/ISSUES.md第11c条：不同source_url的素材标题清洗后撞名，不应互相覆盖。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    same_title = "送你一份超详细的成都三日旅游攻略"
    e1 = m.ingest_to_raw("成都", same_title, "正文A", "bilibili_asr", source_url="https://www.bilibili.com/video/BV1AAA111")
    e2 = m.ingest_to_raw("成都", same_title, "正文B", "bilibili_asr", source_url="https://www.bilibili.com/video/BV1BBB222")

    assert e1["source_file"] != e2["source_file"]
    content1 = (tmp_path / "raw" / "成都" / e1["source_file"]).read_text(encoding="utf-8")
    content2 = (tmp_path / "raw" / "成都" / e2["source_file"]).read_text(encoding="utf-8")
    assert "正文A" in content1
    assert "正文B" in content2


def test_build_douyin_content_with_and_without_chapters():
    with_chapters = m.build_douyin_content(
        "desc正文", [{"desc": "交通", "timestamp": 30000}, {"desc": "住哪", "timestamp": 48000}]
    )
    assert "## 章节导览" in with_chapters
    assert "00:30 交通" in with_chapters
    assert "desc正文" in with_chapters

    without_chapters = m.build_douyin_content("desc正文", None)
    assert "章节导览" not in without_chapters
    assert without_chapters == "desc正文"

    empty_chapters = m.build_douyin_content("desc正文", [])
    assert empty_chapters == "desc正文"


# ---------- remove_from_manifest（安全删除，避免手改manifest.json，见docs/ISSUES.md第12条）----------


def test_remove_from_manifest_deletes_entry_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    url = "https://www.bilibili.com/video/BV1"
    entry = m.ingest_to_raw("成都", "标题", "正文", "bilibili_asr", source_url=url, summary="导读")

    removed = m.remove_from_manifest("成都", url)

    assert len(removed) == 1
    assert removed[0]["source_file"] == entry["source_file"]
    manifest = m._load_manifest(tmp_path / "raw" / "成都")
    assert manifest["items"] == []
    assert not (tmp_path / "raw" / "成都" / entry["source_file"]).exists()
    assert not (tmp_path / "raw" / "成都" / entry["summary_file"]).exists()


def test_remove_from_manifest_platform_filter_keeps_other_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    url = "https://www.douyin.com/video/123"
    e_desc = m.ingest_to_raw("成都", "标题", "desc正文", "douyin", source_url=url)
    e_asr = m.ingest_to_raw("成都", "标题", "ASR正文", "douyin_asr", source_url=url)

    removed = m.remove_from_manifest("成都", url, platform="douyin")

    assert len(removed) == 1
    assert removed[0]["platform"] == "douyin"
    manifest = m._load_manifest(tmp_path / "raw" / "成都")
    remaining_platforms = {item["platform"] for item in manifest["items"]}
    assert remaining_platforms == {"douyin_asr"}
    assert not (tmp_path / "raw" / "成都" / e_desc["source_file"]).exists()
    assert (tmp_path / "raw" / "成都" / e_asr["source_file"]).exists()


def test_remove_from_manifest_no_match_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    m.ingest_to_raw("成都", "标题", "正文", "bilibili_asr", source_url="https://www.bilibili.com/video/BV1")

    removed = m.remove_from_manifest("成都", "https://www.bilibili.com/video/不存在")

    assert removed == []
    manifest = m._load_manifest(tmp_path / "raw" / "成都")
    assert len(manifest["items"]) == 1  # 原有条目未受影响
