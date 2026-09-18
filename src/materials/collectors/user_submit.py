"""用户提交图文素材（小红书/自定义素材池）：图片文件夹/zip压缩包 -> 多模态描述 -> 合成正文 -> 写入 raw/。

对应 docs/ARCHITECTURE.md 第12.1节P1最后一项、data/material_library/README.md
"方式三：用户提交的图文素材"。一个文件夹（或一个zip压缩包）= 一条素材，不做批量拆分：
用户手动把截图/照片存进一个文件夹（可选配一个文本说明文件），或者直接打包成zip，这里负责：
0.（如果传的是zip）解压到 inbox/ 下同名文件夹，再按文件夹流程处理——这样用户不需要
   自己先手动解压；
1. 逐张图片调用多模态模型生成描述（图片间并发，单张失败重试1次后跳过）；
2. 合成 <标题>_原文.md（"图片描述"在前、"博主原文"在后，两段分明，避免模型看图
   猜测的内容被误当成博主亲口陈述的事实）；
3. 图片拷贝进 raw/<topic>/images/（raw/要自包含，不依赖用户之后是否还留着inbox）；
4. 写入 manifest.json（platform=xiaohongshu），去重键 (title, platform) 为主。

写完 manifest 后仍需调用 src.materials.ingest.ingest_topic(topic) 才会真正过
is_relevant 质量门禁入库（跟 ingest_to_raw.py 的 ingest_to_raw() 是同一层次的
"产出->raw/"衔接函数，CLI入口见 `scripts/materials/run_submit_material.py`）。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage

from src.config import MATERIAL_LIBRARY_DIR, get_vision_model
from src.materials.collectors.ingest_to_raw import _disambiguate_filename, _load_manifest, safe_filename

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_WORKERS = 6

_DESCRIBE_PROMPT = (
    "这是一张旅游相关的图文素材配图（可能来自小红书笔记或用户自己拍摄）。"
    "请用一两句话客观描述图片内容：画面里的地点/店铺/菜品/景观是什么样的，"
    "不要编造图片里看不出来的具体信息（如价格、店名，除非画面文字清晰可见）。"
)


class SubmissionError(ValueError):
    """提交失败（如文件夹为空、标题重复、无图无说明文本）时抛出，携带用户可读的原因。"""


# 文本说明文件的可选文件名，按优先级排序：先认 content.txt（小红书收藏工具导出约定），
# 再认 content.md/说明.md/说明.txt/note.txt 等历史/手写约定。命中第一个就不再找后面的，
# 避免同时存在多个文本文件时把多份内容拼在一起（语义上它们都是"博主/用户提供的原文文字"，
# 取一份即可）。
_NOTE_FILENAMES = (
    "content.txt",
    "content.md",
    "说明.md",
    "说明.txt",
    "note.txt",
    "note.md",
)

# 文本说明段的标题。历史上叫"## 用户说明"，但 content.txt 装的其实是从小红书复制下来的
# 博主正文（博主亲口陈述），不是用户自己写的备注——为了不让下游 agent 误判来源，改成"## 博主原文"。
# 纯文字游记（只放说明文本、没图）的场景也用这个段标题。
_NOTE_SECTION_HEADER = "## 博主原文"


def _list_images(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    )


def _locate_note_file(folder: Path) -> Path | None:
    """按 _NOTE_FILENAMES 优先级查找文本说明文件，命中第一个即返回；都不存在返回None。
    命中第一个就不再继续找——同时存在多个文本文件时只取优先级最高的那一份，不拼接多份。
    """
    for name in _NOTE_FILENAMES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _decode_zip_member_name(raw_name: str) -> str:
    """zip标准要求文件名用UTF-8，但Windows上的压缩工具（360压缩/系统自带压缩等）
    常年不遵守，默认按本地编码（简体中文系统=GBK）写入，导致标准库按cp437/utf-8解码
    时中文文件名变成乱码。这里做个尽力而为的修复：先按cp437反编码回原始字节，
    再按GBK重新解码；任何一步失败就放弃修复，原样返回zipfile给出的名字。
    """
    try:
        return raw_name.encode("cp437").decode("gbk")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw_name


def _extract_zip_to_inbox(zip_path: Path) -> Path:
    """把zip解压到 inbox/<zip文件名（去掉.zip）>/，返回可直接当素材文件夹使用的路径。

    - 解压目标已存在时报错而不是静默覆盖/合并，避免冲掉用户手动加过的说明.md或已有内容。
    - 部分压缩包本身会带一层同名外壳文件夹（解压出来是"文件夹里只有一个文件夹"），
      这里自动剥一层壳，让调用方拿到的始终是"直接装着图片"的那一层。
    """
    inbox_dir = MATERIAL_LIBRARY_DIR / "inbox"
    target = inbox_dir / zip_path.stem
    if target.exists():
        raise SubmissionError(
            f"解压目标「{target}」已存在，为避免覆盖已有内容不会自动重新解压——"
            f"请先手动删除这个文件夹，或换一个zip文件名后重试"
        )
    target.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            # flag_bits第11位为1表示文件名按UTF-8编码写入，不需要修复；否则尝试GBK修复
            if not info.flag_bits & 0x800:
                name = _decode_zip_member_name(name)
            dest = target / name
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))

    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return target


def _describe_one_image(image_path: Path) -> str | None:
    """调用多模态模型描述单张图片。重试1次后仍失败返回None（不抛异常，由调用方汇总处理）。"""
    mime, _ = mimetypes.guess_type(image_path.name)
    mime = mime or "image/jpeg"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    message = HumanMessage(content=[
        {"type": "text", "text": _DESCRIBE_PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ])
    model = get_vision_model(temperature=0)
    for _ in range(2):  # 首次尝试 + 重试1次
        try:
            result = model.invoke([message])
            text = (result.content or "").strip()
            if text:
                return text
        except Exception:
            continue
    return None


def _describe_images(images: list[Path]) -> tuple[list[str], list[str]]:
    """并发描述所有图片。返回 (按原顺序排列的描述列表, 失败的文件名列表)。
    描述列表跳过失败的图片（不用None占位），保持"图片描述"正文里的编号连续。
    """
    results: list[str | None] = [None] * len(images)
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(images) or 1)) as executor:
        futures = {executor.submit(_describe_one_image, img): i for i, img in enumerate(images)}
        for future in futures:
            i = futures[future]
            results[i] = future.result()

    descriptions = [r for r in results if r]
    failed = [img.name for img, r in zip(images, results) if r is None]
    return descriptions, failed


def _build_content(descriptions: list[str], note_text: str) -> str:
    parts = []
    if descriptions:
        lines = ["## 图片描述"]
        lines += [f"{i + 1}. {desc}" for i, desc in enumerate(descriptions)]
        parts.append("\n".join(lines))
    if note_text.strip():
        parts.append(f"{_NOTE_SECTION_HEADER}\n{note_text.strip()}")
    return "\n\n".join(parts)


def submit_material_folder(
    topic: str,
    folder_path: str | Path,
    title: str,
    author: str = "",
    source_url: str = "",
) -> dict:
    """处理一个"图片+可选文本说明文件"文件夹（或对应的zip压缩包），写入 raw/<topic>/。
    返回写入的 manifest 条目。

    folder_path 可以是文件夹路径，也可以是.zip压缩包路径——传zip时会先解压到
    inbox/<zip文件名>/（见 _extract_zip_to_inbox），再按文件夹流程处理，用户不需要
    自己手动解压。

    文本说明文件按 _NOTE_FILENAMES 里的顺序查找（content.txt 优先，兼容历史"说明.md"
    等写法），命中第一个即用，不再继续找——避免同时存在多个文本文件时拼多份内容。
    合成正文时文本说明统一落到"## 博主原文"段（见 _NOTE_SECTION_HEADER）。

    失败时抛出 SubmissionError（路径不存在/无图无说明/标题或source_url已存在）。
    """
    topic_dir = MATERIAL_LIBRARY_DIR / "raw" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(topic_dir)

    # 去重检查提到最前面：标题已存在时直接报错跳过，不做任何解压/图片识别——
    # 用户可能会重复上传同一份素材（尤其批量场景下），标题一样就不该浪费时间解压+跑多模态模型。
    for item in manifest["items"]:
        if item.get("title") == title and item.get("platform") == "xiaohongshu":
            raise SubmissionError(f"标题「{title}」在「{topic}」下已存在，如需更新请先手动从raw/删除旧记录，或换个标题")
    if source_url:
        for item in manifest["items"]:
            if item.get("source_url") == source_url and item.get("platform") == "xiaohongshu":
                raise SubmissionError(f"source_url「{source_url}」在「{topic}」下已存在")

    path = Path(folder_path)
    if path.is_file() and path.suffix.lower() == ".zip":
        folder = _extract_zip_to_inbox(path)
    elif path.is_dir():
        folder = path
    else:
        raise SubmissionError(f"路径不存在（既不是文件夹也不是zip文件）：{path}")

    images = _list_images(folder)
    note_path = _locate_note_file(folder)
    note_text = note_path.read_text(encoding="utf-8") if note_path else ""

    if not images and not note_text.strip():
        raise SubmissionError(f"文件夹「{folder}」里既没有图片也没有文本说明文件（{_NOTE_FILENAMES[0]}等），没有可提交的内容")

    descriptions: list[str] = []
    failed: list[str] = []
    if images:
        descriptions, failed = _describe_images(images)
        if not descriptions and not note_text.strip():
            raise SubmissionError(
                f"全部{len(images)}张图片识别失败（{', '.join(failed)}），且没有文本说明文件兜底，提交失败"
            )

    content = _build_content(descriptions, note_text)

    base = _disambiguate_filename(topic_dir, safe_filename(title), source_url, manifest)
    source_file = f"{base}_原文.md"
    (topic_dir / source_file).write_text(content.strip() + "\n", encoding="utf-8")

    # 图片拷贝进 raw/<topic>/images/，raw/要自包含，不依赖用户之后是否还留着inbox临时文件夹
    images_dir = topic_dir / "images"
    if images:
        images_dir.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(images):
            dest = images_dir / f"{base}_{i + 1:02d}{img.suffix.lower()}"
            dest.write_bytes(img.read_bytes())

    entry = {
        "title": title,
        "source_file": source_file,
        "summary_file": "",
        "platform": "xiaohongshu",
        "author": author,
        "source_url": source_url,
        "publish_date": "",
        "collected_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "is_core": True,
    }
    manifest["items"].append(entry)
    (topic_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if failed:
        entry = {**entry, "_warnings": [f"{len(failed)}张图片识别失败：{', '.join(failed)}"]}
    return entry
