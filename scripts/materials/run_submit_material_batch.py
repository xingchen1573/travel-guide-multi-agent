"""批量提交图文素材（小红书/自定义素材池）CLI脚本：一次处理 inbox/<目的地>/ 下的多个zip压缩包/文件夹。

单条素材提交见 `scripts.materials.run_submit_material`（用法一致，标题需要手动指定）。这个脚本是它的批量版：
遍历一个目录下的所有zip压缩包/文件夹，**标题自动取文件名（去掉.zip后缀）**，逐条调用
src.materials.collectors.user_submit.submit_material_folder写入raw/，
最后统一跑一次 ingest_topic(topic) 入库（而不是每条都单独ingest一次，避免重复扫描全量raw/）。

**目录约定**：跟 raw/<目的地>/、index/<目的地>/ 同一套"文件夹名=目的地名称"的规则，
inbox/ 下按目的地建子文件夹（如 inbox/成都/、inbox/北京/），不再是不分目的地的扁平目录——
不加 --dir 参数时，默认扫描 inbox/<topic位置参数>/，这样多个目的地的素材可以分开攒着，
不用每次都手动指定目录。

用法：
    # 把zip/文件夹放进 data/material_library/inbox/成都/ 后：
    PYTHONIOENCODING=utf-8 python -m scripts.materials.run_submit_material_batch 成都
    # 也可以显式指定别的目录（比如不想用inbox/这套约定）：
    PYTHONIOENCODING=utf-8 python -m scripts.materials.run_submit_material_batch 成都 --dir 某个自定义目录 --author "某博主"

**避免重复处理的规则**：如果目录下同时存在 `标题.zip` 和 `标题/`（后者通常是上次提交时
_extract_zip_to_inbox自动解压出来的中间产物），只会把 `标题.zip` 当作一条待处理素材，
`标题/` 文件夹会被跳过——不然同一份内容会被当成两条不同素材重复提交（第二条会因为标题
重复报错，虽然不会破坏数据，但会在输出里造成误导性的"失败"记录）。如果 `标题/` 文件夹
是手动整理的、跟同名zip无关的独立素材，请重命名其中一个避免撞名。

单条提交标题重复（同一批次里跑第二次，或者素材本来就已经提交过）会被跳过并在汇总里报告，
不会中断整批处理；每条都会打印" 成功/跳过/失败"的即时进度。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from src.config import MATERIAL_LIBRARY_DIR
from src.materials.collectors.user_submit import SubmissionError, submit_material_folder
from src.materials.ingest import ingest_topic


def _collect_targets(directory: Path) -> list[Path]:
    """遍历目录，返回待提交的zip/文件夹列表，跳过"和某个zip同名"的文件夹（视为其解压产物）。"""
    children = sorted(directory.iterdir())
    zip_stems = {p.stem for p in children if p.is_file() and p.suffix.lower() == ".zip"}
    targets = []
    for p in children:
        if p.is_file() and p.suffix.lower() == ".zip":
            targets.append(p)
        elif p.is_dir() and p.name not in zip_stems:
            targets.append(p)
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description="批量提交一个目录下的多条图文素材（小红书/自定义素材池）")
    parser.add_argument("topic", help="目的地（=素材库主题文件夹名，如 成都）")
    parser.add_argument(
        "--dir", default=None,
        help="待扫描的目录，不指定时默认 data/material_library/inbox/<topic>/"
        "（跟raw/、index/同一套按目的地分文件夹的约定），不递归扫描子目录",
    )
    parser.add_argument("--author", default="", help="作者/博主昵称，本批次所有素材统一填这个（可选）")
    args = parser.parse_args()

    default_directory = MATERIAL_LIBRARY_DIR / "inbox" / args.topic
    directory = Path(args.dir) if args.dir else default_directory
    if not directory.is_dir():
        print(f"目录不存在：{directory}"
              f"（默认按「{default_directory}/」这个约定查找，"
              f"如果你的zip放在别处，用 --dir 指定）")
        return

    targets = _collect_targets(directory)
    if not targets:
        print(f"「{directory}」下没有找到待提交的zip/文件夹。")
        return

    print(f"=== 批量提交: {args.topic} — 共{len(targets)}条待处理 ===\n")

    succeeded: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for target in targets:
        title = target.stem if target.is_file() else target.name
        print(f"[{title}] 处理中...")
        try:
            entry = submit_material_folder(args.topic, target, title, author=args.author)
        except SubmissionError as e:
            msg = str(e)
            if "已存在" in msg:
                skipped.append(title)
                print(f"  跳过（{msg}）")
            else:
                failed.append((title, msg))
                print(f"  失败：{msg}")
            continue
        succeeded.append(title)
        print(f"  已写入 raw/{args.topic}/{entry['source_file']}")
        for warning in entry.get("_warnings", []):
            print(f"  警告：{warning}")

    print(f"\n=== 正式入库（is_relevant质量门禁，一次性处理本批次新增内容）===")
    new_entries = ingest_topic(args.topic)
    new_titles = {e.title for e in new_entries}

    print(f"\n=== 汇总 ===")
    print(f"成功写入raw/: {len(succeeded)}条")
    for title in succeeded:
        status = "已入库" if title in new_titles else "未过is_relevant质量门禁（详见rejected.jsonl）"
        print(f"  - {title}：{status}")
    if skipped:
        print(f"跳过（标题/来源已存在，可能是重复提交）: {len(skipped)}条")
        for title in skipped:
            print(f"  - {title}")
    if failed:
        print(f"处理失败: {len(failed)}条")
        for title, msg in failed:
            print(f"  - {title}：{msg}")


if __name__ == "__main__":
    main()
