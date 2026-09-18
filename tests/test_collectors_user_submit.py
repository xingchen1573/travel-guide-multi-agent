"""src/materials/collectors/user_submit.py 的单元测试（纯逻辑，tmp_path隔离，mock多模态模型调用，不联网）。

    python -m pytest tests/test_collectors_user_submit.py -v
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import src.materials.collectors.user_submit as m


class _FakeVisionResult:
    def __init__(self, content: str):
        self.content = content


class _FakeVisionModel:
    """按调用顺序返回预设结果；条目为异常类时模拟调用失败。

    真实图片描述走ThreadPoolExecutor并发调用，call_count自增需要加锁，
    避免多线程测试里出现竞态（跟src/llm_rotation.py的_advance_key同款考虑）。
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.call_count = 0
        self._lock = threading.Lock()

    def invoke(self, messages):
        with self._lock:
            resp = self._responses[self.call_count % len(self._responses)]
            self.call_count += 1
        if isinstance(resp, Exception):
            raise resp
        return _FakeVisionResult(resp)


def _make_image(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(b"\xff\xd8\xff\xe0fakejpegbytes")
    return path


def test_folder_not_exist_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    with pytest.raises(m.SubmissionError):
        m.submit_material_folder("成都", tmp_path / "不存在", "标题")


def test_empty_folder_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    with pytest.raises(m.SubmissionError, match="没有可提交的内容"):
        m.submit_material_folder("成都", folder, "标题")


def test_successful_submission_with_images_and_note(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    _make_image(folder, "1.jpg")
    _make_image(folder, "2.jpg")
    (folder / "说明.md").write_text("人均120，工作日下午不用排队", encoding="utf-8")

    fake_model = _FakeVisionModel(["装修复古的火锅店门头，排了约10人的队", "一份麻辣牛油锅底"])
    monkeypatch.setattr(m, "get_vision_model", lambda temperature=0: fake_model)

    entry = m.submit_material_folder("成都", folder, "国宾路老火锅探店", author="某博主")

    assert entry["platform"] == "xiaohongshu"
    assert entry["author"] == "某博主"
    content = (tmp_path / "raw" / "成都" / entry["source_file"]).read_text(encoding="utf-8")
    assert "## 图片描述" in content
    assert "火锅店门头" in content
    assert "麻辣牛油锅底" in content
    assert "## 博主原文" in content
    assert "人均120" in content

    # 图片应拷贝进 raw/<topic>/images/
    images = list((tmp_path / "raw" / "成都" / "images").glob("*"))
    assert len(images) == 2


def test_note_only_no_images(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "说明.md").write_text("自己写的游记文字", encoding="utf-8")

    entry = m.submit_material_folder("成都", folder, "纯文字游记")

    content = (tmp_path / "raw" / "成都" / entry["source_file"]).read_text(encoding="utf-8")
    assert "## 图片描述" not in content
    assert "## 博主原文" in content
    assert "自己写的游记文字" in content
    assert not (tmp_path / "raw" / "成都" / "images").exists()


def test_content_txt_is_preferred_note_filename(tmp_path, monkeypatch):
    """content.txt 是小红书收藏工具导出约定的文本说明文件名，优先级高于说明.md等历史写法。
    同时存在多个文本文件时只取 content.txt，不把多份内容拼在一起。
    """
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "content.txt").write_text("博主从小红书复制的正文", encoding="utf-8")
    (folder / "说明.md").write_text("用户手写的备注", encoding="utf-8")

    entry = m.submit_material_folder("成都", folder, "content.txt优先级测试")

    content = (tmp_path / "raw" / "成都" / entry["source_file"]).read_text(encoding="utf-8")
    assert "## 博主原文" in content
    assert "博主从小红书复制的正文" in content
    assert "用户手写的备注" not in content  # 没拼上 说明.md 的内容


def test_content_md_and_other_note_filenames_recognized(tmp_path, monkeypatch):
    """content.md/说明.txt/note.txt 等历史/手写约定也能被识别（content.txt 不存在时回退）。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    _make_image(folder, "1.jpg")
    (folder / "note.txt").write_text("note.txt兜底内容", encoding="utf-8")

    fake_model = _FakeVisionModel(["一张图"])
    monkeypatch.setattr(m, "get_vision_model", lambda temperature=0: fake_model)

    entry = m.submit_material_folder("成都", folder, "note.txt识别测试")
    content = (tmp_path / "raw" / "成都" / entry["source_file"]).read_text(encoding="utf-8")
    assert "## 博主原文" in content
    assert "note.txt兜底内容" in content


def test_partial_image_failure_still_succeeds(tmp_path, monkeypatch):
    """一张图识别失败（重试1次后仍失败），另一张成功，整体应仍成功且带警告。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    _make_image(folder, "1.jpg")
    _make_image(folder, "2.jpg")

    def fake_describe(image_path):
        if image_path.name == "1.jpg":
            return None  # 模拟重试后仍失败
        return "成功识别的描述"

    monkeypatch.setattr(m, "_describe_one_image", fake_describe)

    entry = m.submit_material_folder("成都", folder, "标题")

    content = (tmp_path / "raw" / "成都" / entry["source_file"]).read_text(encoding="utf-8")
    assert "成功识别的描述" in content
    assert entry.get("_warnings")
    assert "1张图片识别失败" in entry["_warnings"][0]


def test_all_images_fail_without_note_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    _make_image(folder, "1.jpg")

    monkeypatch.setattr(m, "_describe_one_image", lambda image_path: None)

    with pytest.raises(m.SubmissionError, match="识别失败"):
        m.submit_material_folder("成都", folder, "标题")


def test_all_images_fail_but_note_present_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    _make_image(folder, "1.jpg")
    (folder / "说明.md").write_text("文字说明兜底", encoding="utf-8")

    monkeypatch.setattr(m, "_describe_one_image", lambda image_path: None)

    entry = m.submit_material_folder("成都", folder, "标题")
    content = (tmp_path / "raw" / "成都" / entry["source_file"]).read_text(encoding="utf-8")
    assert "## 图片描述" not in content
    assert "文字说明兜底" in content


def test_duplicate_title_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "说明.md").write_text("说明", encoding="utf-8")

    m.submit_material_folder("成都", folder, "重复标题")
    with pytest.raises(m.SubmissionError, match="已存在"):
        m.submit_material_folder("成都", folder, "重复标题")


def test_duplicate_source_url_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "说明.md").write_text("说明", encoding="utf-8")
    url = "https://www.xiaohongshu.com/explore/abc123"

    m.submit_material_folder("成都", folder, "标题A", source_url=url)
    with pytest.raises(m.SubmissionError, match="source_url"):
        m.submit_material_folder("成都", folder, "标题B", source_url=url)


def test_describe_one_image_retries_once_then_succeeds(tmp_path, monkeypatch):
    """真正验证_describe_one_image本身的重试逻辑（不mock掉它自己）。"""
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    image = _make_image(tmp_path, "1.jpg")

    fake_model = _FakeVisionModel([RuntimeError("网络错误"), "重试后成功的描述"])
    monkeypatch.setattr(m, "get_vision_model", lambda temperature=0: fake_model)

    result = m._describe_one_image(image)

    assert result == "重试后成功的描述"
    assert fake_model.call_count == 2


def test_describe_one_image_fails_after_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MATERIAL_LIBRARY_DIR", tmp_path)
    image = _make_image(tmp_path, "1.jpg")

    fake_model = _FakeVisionModel([RuntimeError("网络错误"), RuntimeError("再次失败")])
    monkeypatch.setattr(m, "get_vision_model", lambda temperature=0: fake_model)

    result = m._describe_one_image(image)

    assert result is None
    assert fake_model.call_count == 2
