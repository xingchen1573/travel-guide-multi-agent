"""把采集+转写好的一条素材规范化写进 data/material_library/raw/<目的地>/：
生成 <标题>_原文.md，并往 manifest.json 追加一条元信息（幂等，按 source_url 去重）。

这是"采集脚本产出"和"正式 ingest"之间的衔接层。写完之后仍需人工审核，再手动跑
    python -m src.materials.ingest <目的地>
把 raw/ 处理进 index/（见 data/material_library/README.md "半自动采集pipeline"）。

不脱敏原始 uid：manifest 里只存 author（博主公开昵称/UP主名，是最终报告"参考来源"要展示的内容），
不存任何原始数字 uid/mid，符合"不含PII"要求。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from src.config import MATERIAL_LIBRARY_DIR

# Windows/跨平台文件名非法字符
_ILLEGAL_FILENAME_CHARS = '<>:"/\\|?*'

# 用于从source_url里提取可读的短标识做文件名去重后缀（BV号/aweme_id），
# 提取不到就退回内容哈希，见 _disambiguate_filename
_BVID_RE = re.compile(r"(BV\w+)")
_AWEME_ID_RE = re.compile(r"/video/(\d{15,20})")


def safe_filename(title: str, max_len: int = 60) -> str:
    cleaned = "".join(c for c in title if c not in _ILLEGAL_FILENAME_CHARS).strip()
    # 折叠连续空白，避免文件名里出现诡异的多空格
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_len] or "untitled"


def _short_id_from_url(source_url: str) -> str:
    """从source_url提取BV号/aweme_id做人类可读的消歧后缀，提取不到就退回短哈希。"""
    m = _BVID_RE.search(source_url)
    if m:
        return m.group(1)
    m = _AWEME_ID_RE.search(source_url)
    if m:
        return m.group(1)[-8:]  # aweme_id很长，取末8位够用于消歧
    return hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:8]


def _disambiguate_filename(topic_dir: Path, base: str, source_url: str, manifest: dict) -> str:
    """标题清洗后可能撞名（B站标题高度模板化，截断60字符后更容易撞），
    直接复用同名文件会静默覆盖前一条素材——这里检测到冲突就用source_url的短标识加后缀，
    绝不覆盖已存在的、属于不同source_url的文件。
    """
    candidate = f"{base}_原文.md"
    existing_files = {item.get("source_file") for item in manifest.get("items", [])}
    if candidate not in existing_files and not (topic_dir / candidate).exists():
        return base
    suffix = _short_id_from_url(source_url) if source_url else hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{base}_{suffix}"


def _to_publish_date(pubdate) -> str:
    """把 Unix 秒时间戳转成 YYYY-MM-DD；已经是字符串或空就原样/空返回。"""
    if not pubdate:
        return ""
    if isinstance(pubdate, str):
        return pubdate
    return datetime.fromtimestamp(pubdate, tz=timezone.utc).strftime("%Y-%m-%d")


def _load_manifest(topic_dir: Path) -> dict:
    manifest_path = topic_dir / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"topic": topic_dir.name, "items": []}


def build_douyin_content(desc: str, chapters: list[dict] | None = None) -> str:
    """组合抖音素材正文：desc（文案+话题标签）是唯一保证有的内容；chapter_list是UP主
    手动加的章节标记（原始aweme_detail字段，不是所有视频都有），有就拼进"章节导览"
    结构化大纲，没有就只用desc——弹性设计，不强求每条视频都有章节。

    背景（实测确认）：抖音的video_text（真实字幕轨）几乎总是空的，caption字段只是
    话题标签的重复，都不提供额外信息，所以不纳入。chapter_list虽然覆盖率不定，但
    有的话是UP主手写的结构化大纲，价值高于逐句字幕，值得单独拼进正文。
    """
    parts = []
    if chapters:
        lines = ["## 章节导览"]
        for ch in chapters:
            title = (ch.get("desc") or "").strip()
            if not title:
                continue
            seconds = (ch.get("timestamp") or 0) / 1000
            mm, ss = divmod(int(seconds), 60)
            lines.append(f"- {mm:02d}:{ss:02d} {title}")
        if len(lines) > 1:  # 除了标题行还有实际章节才拼进去
            parts.append("\n".join(lines))
    if desc.strip():
        parts.append(desc.strip())
    return "\n\n".join(parts)


def ingest_to_raw(
    topic: str,
    title: str,
    transcript: str,
    platform: str,
    author: str = "",
    source_url: str = "",
    publish_date="",
    summary: str = "",
    *,
    library_dir: Path | None = None,
) -> dict | None:
    """把一条转写好的素材写进 raw/<topic>/。

    - platform 必须是 bilibili_asr / douyin / douyin_asr 之一（见 README 对照表），
      绝不能用 bilibili（那是带时间戳标记的存量格式，flat文本用它会退化成一个巨型chunk）。
    - 幂等：manifest 里已存在相同 (source_url, platform) 组合的条目则跳过（返回 None）。
      不是只按source_url去重——同一个视频完全可能既有desc文案素材（platform=douyin）
      又有ASR转写素材（platform=douyin_asr），两者信息量不同、都值得保留，属于两条
      独立的素材条目，不应该被"同url=已采集过"的判断误伤。
    - 文件名冲突消歧：不同source_url的素材标题清洗+截断后如果撞名，不会覆盖已有文件，
      会自动加source_url的短标识（BV号/aweme_id/哈希）区分，见 _disambiguate_filename。
    返回新写入的 manifest 条目（dict），跳过时返回 None。
    """
    if platform not in {"bilibili_asr", "douyin", "douyin_asr"}:
        raise ValueError(
            f"platform={platform!r} 不合法。ASR/自动采集链路只能用 "
            f"bilibili_asr/douyin/douyin_asr，不能用 bilibili（见 README 平台对照表）"
        )

    topic_dir = (library_dir or MATERIAL_LIBRARY_DIR) / "raw" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(topic_dir)

    if source_url:
        for existing in manifest["items"]:
            if existing.get("source_url") == source_url and existing.get("platform") == platform:
                return None  # 同一(source_url, platform)组合已采集过，幂等跳过

    base = _disambiguate_filename(topic_dir, safe_filename(title), source_url, manifest)
    source_file = f"{base}_原文.md"
    (topic_dir / source_file).write_text(transcript.strip() + "\n", encoding="utf-8")

    summary_file = ""
    if summary.strip():
        summary_file = f"{base}_导读.md"
        (topic_dir / summary_file).write_text(summary.strip() + "\n", encoding="utf-8")

    entry = {
        "title": title,
        "source_file": source_file,
        "summary_file": summary_file,
        "platform": platform,
        "author": author,
        "source_url": source_url,
        "publish_date": _to_publish_date(publish_date),
        "collected_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "is_core": True,
    }
    manifest["items"].append(entry)
    (topic_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return entry


def remove_from_manifest(
    topic: str,
    source_url: str,
    platform: str | None = None,
    *,
    library_dir: Path | None = None,
) -> list[dict]:
    """从 manifest.json 中移除匹配 source_url（可选限定 platform）的条目，并删除对应的
    _原文.md / _导读.md 文件本体。结构化操作（load→过滤→dump），不会像手动文本编辑
    manifest.json 那样有语法错误风险——真实踩过这个坑：人工用文本编辑工具删条目时手滑
    打出了多余的花括号，JSON直接损坏（见 docs/ISSUES.md 第12条）。

    platform=None 时删除该 source_url 下所有 platform 的条目；指定 platform 时只删匹配的那条
    （同一视频的 douyin/douyin_asr 两条素材可以只删其中一条，见 (source_url, platform) 联合幂等键）。
    返回被删除的条目列表（找不到匹配项时返回空列表，不报错）。
    """
    topic_dir = (library_dir or MATERIAL_LIBRARY_DIR) / "raw" / topic
    manifest = _load_manifest(topic_dir)

    to_remove = [
        item
        for item in manifest["items"]
        if item.get("source_url") == source_url
        and (platform is None or item.get("platform") == platform)
    ]
    if not to_remove:
        return []

    manifest["items"] = [item for item in manifest["items"] if item not in to_remove]
    for item in to_remove:
        for key in ("source_file", "summary_file"):
            filename = item.get(key)
            if filename and (topic_dir / filename).exists():
                (topic_dir / filename).unlink()

    (topic_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return to_remove


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="从素材库manifest中移除指定条目（结构化操作，不手改JSON）")
    parser.add_argument("topic", help="目的地（=素材库主题文件夹名，如 成都）")
    parser.add_argument("source_url", help="要移除的素材的 source_url")
    parser.add_argument("--platform", default=None, help="只删指定platform的那条；不传则删该source_url下所有platform")
    args = parser.parse_args()

    removed = remove_from_manifest(args.topic, args.source_url, args.platform)
    if removed:
        print(f"移除了 {len(removed)} 条：")
        for item in removed:
            print(f"  - [{item['platform']}] {item['title']}")
    else:
        print("未找到匹配条目，未做任何修改")
