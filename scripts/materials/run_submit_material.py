"""用户提交图文素材（小红书/自定义素材池）CLI脚本：图片文件夹/zip压缩包 -> raw/ -> 正式入库。

见 src/materials/collectors/user_submit.py 的设计说明、data/material_library/README.md
"方式三：用户提交的图文素材"。一个文件夹（或一个zip压缩包）= 一条素材。

用法（文件夹）：
    PYTHONIOENCODING=utf-8 python -m scripts.materials.run_submit_material 成都 data/material_library/inbox/成都_火锅探店 \
        --title "国宾路老火锅探店" --author "某博主" --source_url "https://..."

用法（zip压缩包，直接传zip路径，会自动解压到inbox/后按上面同样流程处理）：
    PYTHONIOENCODING=utf-8 python -m scripts.materials.run_submit_material 成都 "~/Downloads/成都保姆级攻略.zip" \
        --title "成都保姆级旅游攻略6-8月都这样玩" --author "某博主"
"""
from __future__ import annotations

import argparse
import sys

from src.config import MATERIAL_LIBRARY_DIR
from src.materials.collectors.user_submit import SubmissionError, submit_material_folder
from src.materials.ingest import ingest_topic


def main() -> None:
    parser = argparse.ArgumentParser(description="提交一个图文素材文件夹（小红书/自定义素材池）")
    parser.add_argument("topic", help="目的地（=素材库主题文件夹名，如 成都）")
    parser.add_argument("folder", help="包含图片+可选文本说明文件（content.txt/说明.md等）的文件夹路径，或对应的zip压缩包路径")
    parser.add_argument("--title", required=True, help="素材标题（必填，也是去重主键的一部分）")
    parser.add_argument("--author", default="", help="作者/博主昵称（可选）")
    parser.add_argument("--source_url", default="", help="来源链接，如真实小红书笔记链接（可选）")
    args = parser.parse_args()

    print(f"=== 提交素材: {args.topic} — {args.title} ===\n")

    try:
        entry = submit_material_folder(
            args.topic, args.folder, args.title, author=args.author, source_url=args.source_url
        )
    except SubmissionError as e:
        print(f"提交失败：{e}", file=sys.stderr)
        sys.exit(1)

    print(f"已写入 raw/{args.topic}/{entry['source_file']}")
    for warning in entry.get("_warnings", []):
        print(f"  警告：{warning}")

    print("\n=== 正式入库（is_relevant质量门禁）===")
    new_entries = ingest_topic(args.topic)
    if any(e.title == args.title for e in new_entries):
        print(f"「{args.title}」已成功入库。")
    else:
        rejected_path = MATERIAL_LIBRARY_DIR / "index" / args.topic / "rejected.jsonl"
        print(f"「{args.title}」未进入index（可能被is_relevant质量门禁拒绝，详见 {rejected_path}）。")


if __name__ == "__main__":
    main()
