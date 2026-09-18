"""管理后台"POI图片"人工上传功能的文件校验逻辑。

跟src/tools/poi_photo.py（高德自动查图）是互补关系：自动查图覆盖率有天花板（数据库
收录不全、途经点粒度自动匹配已被证明不可行，见poi_photo.py docstring演变过程），
这里给管理后台人类用户提供手动上传真实照片的校验能力，不涉及网络请求/agent工具链。

只做文件类型（magic bytes）+大小+最小分辨率校验，不做内容审核/相关性校验——这是
用户自己挑选的真实照片，不存在"配错"的问题，不需要poi_photo.py那套
_is_relevant_match语义校验。

文件类型校验必须嗅探实际字节内容，不能只看扩展名或HTTP Content-Type头——两者都能被
用户端随意伪造（改文件名后缀、改请求头），不构成可信的类型证据；magic bytes是文件
格式规范本身规定的固定前缀，无法在不破坏文件本身的前提下伪造。

最小分辨率校验复用poi_photo.py的_MIN_PHOTO_WIDTH_PX门槛（480px，低门槛只挡真正
损坏/异常小的图），跟自动查图路径保持同一条质量线，避免管理员手动传了一张比自动
查图更糊的图上去。
"""
from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from src.tools.poi_photo import _MIN_PHOTO_WIDTH_PX

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 单张10MB上限

# 每种支持格式的magic bytes签名，(前缀字节, 对应扩展名)
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_WEBP_RIFF = b"RIFF"
_WEBP_MAGIC = b"WEBP"


class InvalidPhotoUpload(ValueError):
    """上传内容未通过大小/类型校验时抛出，路由层据此返回400。"""


def sniff_image_ext(content: bytes) -> str:
    """按magic bytes判断真实图片格式，返回不带点号的扩展名（"jpg"/"png"/"webp"）。
    都不匹配时抛InvalidPhotoUpload——不做"尽量猜一个"的兜底，类型不明的内容一律拒绝。
    """
    if content.startswith(_JPEG_MAGIC):
        return "jpg"
    if content.startswith(_PNG_MAGIC):
        return "png"
    if content[:4] == _WEBP_RIFF and content[8:12] == _WEBP_MAGIC:
        return "webp"
    raise InvalidPhotoUpload("不是有效的jpg/png/webp图片（文件内容不匹配任何已知图片格式）")


def validate_upload(content: bytes) -> str:
    """校验上传内容的大小上限+真实类型+最小分辨率，返回校验通过的扩展名。
    校验顺序：大小→类型→分辨率——大小先于类型校验，避免为了嗅探一个明显超限的
    大文件浪费判断；分辨率校验放最后，因为需要先确认类型是图片才值得用PIL解码。
    """
    if not content:
        raise InvalidPhotoUpload("上传内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise InvalidPhotoUpload(
            f"文件大小{len(content)}字节超过上限{MAX_UPLOAD_BYTES}字节（10MB）"
        )
    ext = sniff_image_ext(content)

    try:
        width, _ = Image.open(io.BytesIO(content)).size
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidPhotoUpload("图片文件已损坏或无法解析") from exc
    if width < _MIN_PHOTO_WIDTH_PX:
        raise InvalidPhotoUpload(
            f"图片宽度{width}px低于最低要求{_MIN_PHOTO_WIDTH_PX}px，可能已损坏或分辨率过低"
        )

    return ext
