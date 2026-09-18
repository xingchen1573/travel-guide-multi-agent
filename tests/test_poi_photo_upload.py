"""src/tools/poi_photo_upload.py 的单元测试（纯逻辑，不联网、不起服务）。

    python -m pytest tests/test_poi_photo_upload.py -v
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from src.tools.poi_photo import _MIN_PHOTO_WIDTH_PX
from src.tools.poi_photo_upload import (
    MAX_UPLOAD_BYTES,
    InvalidPhotoUpload,
    sniff_image_ext,
    validate_upload,
)


def _make_jpeg_bytes(width: int, height: int = 300) -> bytes:
    """现场生成一张真实可解码的JPEG，供分辨率/解码相关校验测试使用（跟magic bytes
    伪造字节测试的关注点不同——这里要的是PIL真的能读出宽高）。"""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 120, 60)).save(buf, format="JPEG")
    return buf.getvalue()


# ---------- sniff_image_ext ----------

def test_sniff_image_ext_detects_real_jpeg():
    assert sniff_image_ext(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "jpg"


def test_sniff_image_ext_detects_real_png():
    assert sniff_image_ext(b"\x89PNG\r\n\x1a\n" + b"rest of the file") == "png"


def test_sniff_image_ext_detects_real_webp():
    content = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest"
    assert sniff_image_ext(content) == "webp"


def test_sniff_image_ext_rejects_plain_text_with_fake_jpg_extension():
    """伪造扩展名但内容不是图片的核心边界case——校验必须基于内容，不能信任文件名。"""
    content = b"this is just plain text pretending to be a photo.jpg"
    with pytest.raises(InvalidPhotoUpload):
        sniff_image_ext(content)


def test_sniff_image_ext_rejects_riff_without_webp_marker():
    """RIFF容器格式不止webp一种（比如wav），前缀匹配必须同时核对WEBP标记，不能只看RIFF。"""
    content = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt "
    with pytest.raises(InvalidPhotoUpload):
        sniff_image_ext(content)


def test_sniff_image_ext_rejects_empty_content():
    with pytest.raises(InvalidPhotoUpload):
        sniff_image_ext(b"")


# ---------- validate_upload ----------

def test_validate_upload_accepts_valid_jpeg_within_size_limit():
    content = _make_jpeg_bytes(_MIN_PHOTO_WIDTH_PX)
    assert validate_upload(content) == "jpg"


def test_validate_upload_rejects_empty_content():
    with pytest.raises(InvalidPhotoUpload):
        validate_upload(b"")


def test_validate_upload_rejects_content_over_size_limit():
    oversized = b"\xff\xd8\xff" + b"0" * MAX_UPLOAD_BYTES
    with pytest.raises(InvalidPhotoUpload):
        validate_upload(oversized)


def test_validate_upload_checks_size_before_sniffing_type():
    """超限文件即使内容本身也不是图片，报错信息应该是大小超限（校验顺序：先大小后类型）。"""
    oversized_garbage = b"not an image" * (MAX_UPLOAD_BYTES // 10)
    with pytest.raises(InvalidPhotoUpload, match="大小"):
        validate_upload(oversized_garbage)


def test_validate_upload_rejects_invalid_type_within_size_limit():
    with pytest.raises(InvalidPhotoUpload, match="jpg/png/webp"):
        validate_upload(b"not a real image, but small enough")


def test_validate_upload_rejects_below_min_width():
    content = _make_jpeg_bytes(_MIN_PHOTO_WIDTH_PX - 1)
    with pytest.raises(InvalidPhotoUpload, match="分辨率过低"):
        validate_upload(content)


def test_validate_upload_accepts_at_min_width_threshold():
    content = _make_jpeg_bytes(_MIN_PHOTO_WIDTH_PX)
    assert validate_upload(content) == "jpg"


def test_validate_upload_rejects_corrupted_image_data():
    """magic bytes合法（能通过sniff_image_ext）但后续字节是垃圾，PIL解不出宽高的
    真实损坏文件场景——不能因为类型嗅探通过了就认为图片本身没问题。"""
    content = b"\xff\xd8\xff" + b"\x00" * 50
    with pytest.raises(InvalidPhotoUpload, match="损坏"):
        validate_upload(content)
