"""抖音素材半自动采集编排：给定视频链接列表 -> MediaCrawler抓详情(desc+章节) -> 写入 raw/ + manifest。

和 bilibili.py 的定位一致。

**关于抖音关键词搜索**：`MediaCrawler`的`--type search`模式（`--keywords`参数）实测可用（见
`docs/ISSUES.md`第11d条），一次能抓到多条不重复的真实候选，且没有触发account blocked。
`trigger_search()`封装了这次调用；抓到候选后写进`search_contents_*.jsonl`，用
`list_ranked_candidates()`读取+排序+去重，见`docs/ISSUES.md`第14条。

**抖音默认不做ASR，但desc内容过短(<60字)时会自动补**（实测确认：video_text真字幕轨几乎总是空，
desc+chapter_list通常已经是可靠信息来源，见 data/material_library/README.md；但也实测遇到过desc
完全空白、只等价于标题本身的情况，这种不需要人工判断，脚本会自动追加ASR，见docs/ISSUES.md第12条）。
`--asr`参数语义是"强制对全部链接补ASR"（覆盖自动判断），不是"要不要补"的开关。

用法（作为CLI脚本手动跑）：
    # 先触发一次抖音关键词搜索（也可以用trigger_search()函数调用，见素材agent的采集工具）：
    #   python -m src.materials.collectors.douyin 成都 --trigger-search --keyword "成都旅游攻略"
    # 然后列出排序+去重后的候选（不联网，纯读本地jsonl）：
    python -m src.materials.collectors.douyin 成都 --list-candidates --top-n 10 --sort-by 最热

    # 只抓元数据（desc+章节导览），desc内容过短的会自动追加ASR，不需要人工判断
    python -m src.materials.collectors.douyin 成都 <抖音链接1> <抖音链接2> ...

    # 强制对全部指定链接补ASR（忽略自动判断，会调用 douyin_to_text/pipeline.py 下载+转写，较慢）
    python -m src.materials.collectors.douyin 成都 <抖音链接1> --asr

也可以被素材agent的采集工具（src/tools/material_collection_tools.py）以Python函数方式调用，
trigger_search/list_ranked_candidates/collect_one 是agent工具直接复用的核心函数。

前提：sandbox/MediaCrawler 已跑通过一次（装好依赖+playwright chromium）。首次运行/登录态
过期时会弹出浏览器要求扫码，扫完自动继续（见 sandbox/MediaCrawler/config/base_config.py
的 ENABLE_CDP_MODE：零星测试建议 False 用标准模式（过期自动弹码更省心）；
真要批量跑几十条建议 True 用CDP模式（更强反检测），但要提前手动登录一次刷新对应Edge
profile的cookie，见 docs/ISSUES.md 抖音登录态相关记录）。**素材agent触发抖音相关工具时
可能弹出登录扫码窗口，需要有人在旁边处理——这是有意不做的设计简化，见docs/ISSUES.md
"完整素材agent"相关记录：subprocess本身会同步阻塞等真人扫码，不需要额外的检测/暂停机制。**

【转正记录】本文件原是 sandbox/pipeline/collect_douyin.py，经过多轮真实验证+单元测试后
正式迁入 src/materials/collectors/（见 docs/ISSUES.md 相关记录）。迁移时把 _PROJECT_VENV_PYTHON
改成了sys.executable；_MEDIACRAWLER_PYTHON保留（MediaCrawler是真正独立的venv，不受此次迁移影响）。
新增 collect_one() 和 trigger_search()，供素材agent的采集图节点/工具直接调用。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")

_SANDBOX_DIR = Path(__file__).resolve().parents[3] / "sandbox"
_MEDIACRAWLER_DIR = _SANDBOX_DIR / "MediaCrawler"
_MEDIACRAWLER_PYTHON = _MEDIACRAWLER_DIR / ".venv" / "Scripts" / "python.exe"

_AWEME_ID_RE = re.compile(r"(\d{15,20})")

# desc内容长度低于此值视为"过短，没有实质信息量"，自动追加ASR转写而不需要人工传--asr判断。
# 依据：实测遇到过desc完全为空、content等价于title本身的情况（约40-60字符），真正有实质
# 信息量的desc普遍在100+字符（含话题标签），60是留了安全余量的折中值，见docs/ISSUES.md第12条。
_THIN_CONTENT_THRESHOLD = 60


def _extract_aweme_id(url_or_id: str) -> str:
    """从抖音链接或纯ID里提取 aweme_id（数字串）。"""
    m = _AWEME_ID_RE.search(url_or_id)
    if not m:
        raise ValueError(f"无法从 {url_or_id!r} 提取 aweme_id，请传标准视频链接或纯数字ID")
    return m.group(1)


def _load_existing_source_urls(topic: str) -> set[str]:
    """读取 raw/<topic>/manifest.json 里已有的 source_url 集合，去重预过滤用（见bilibili.py同名函数）。"""
    from src.materials.collectors.ingest_to_raw import MATERIAL_LIBRARY_DIR, _load_manifest

    manifest = _load_manifest(MATERIAL_LIBRARY_DIR / "raw" / topic)
    return {item.get("source_url", "") for item in manifest.get("items", []) if item.get("source_url")}


def _find_manifest_entry(topic: str, source_url: str, platform: str) -> dict | None:
    """采集完成后从manifest里查回刚写入(或早已存在)的那条条目，供collect_one返回给调用方。"""
    from src.materials.collectors.ingest_to_raw import MATERIAL_LIBRARY_DIR, _load_manifest

    manifest = _load_manifest(MATERIAL_LIBRARY_DIR / "raw" / topic)
    for item in manifest.get("items", []):
        if item.get("source_url") == source_url and item.get("platform") == platform:
            return item
    return None


def trigger_search(keywords: list[str]) -> None:
    """触发一次MediaCrawler抖音关键词搜索，产出写进search_contents_*.jsonl（供list_ranked_candidates读取）。
    可能弹出登录窗口要求扫码（标准模式下的正常行为，subprocess会同步阻塞直到完成/超时）。

    ⚠️ 关键：显式传 --enable_get_meidas false，只抓元数据(标题/点赞数/视频url等用于排序展示)，
    不下载视频本体。search阶段的目的是"列出候选给人工挑"，不是真正采集——真正下载视频是
    collect_one()阶段人工确认后才做的事。如果不关掉这个开关，MediaCrawler默认会在search时
    就完整下载每条候选的视频，而且它本身不检查文件是否已存在就会重新下载/覆盖写入（见
    sandbox/MediaCrawler/store/douyin/douyin_store_media.py的save_video，没有exists()判断）。
    真实验证过：素材agent自主换关键词重搜时命中大量重复热门视频，同一个100MB+的视频被
    完整重新下载了一次，单条耗时11分钟，整个流程30多分钟卡在重复下载上——这是当场发现并
    修复的性能bug，见docs/ISSUES.md。
    """
    subprocess.run(
        [
            str(_MEDIACRAWLER_PYTHON), "main.py",
            "--platform", "dy", "--type", "search",
            "--keywords", ",".join(keywords),
            "--enable_get_meidas", "false",
        ],
        cwd=str(_MEDIACRAWLER_DIR),
        check=True,
    )


def list_ranked_candidates(topic: str, top_n: int = 10, sort_by: str = "综合", keyword: str | None = None) -> list[dict]:
    """读取本地已有的 search_contents_*.jsonl（trigger_search 或 MediaCrawler --type search 的产出），
    排除已入库的source_url，排序后返回候选列表。不联网，不下载，只是"列出+排序"这一步。

    keyword不为None时只保留source_keyword匹配的候选（一次search可能用多个关键词）。
    """
    from src.materials.collectors.douyin_ranking import rank_candidates

    jsonl_dir = _MEDIACRAWLER_DIR / "data" / "douyin" / "jsonl"
    existing = _load_existing_source_urls(topic)
    merged: dict[str, dict] = {}
    for f in jsonl_dir.glob("search_contents_*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            aweme_id = item.get("aweme_id")
            if not aweme_id or aweme_id in merged:
                continue
            if keyword and item.get("source_keyword") != keyword:
                continue
            source_url = item.get("aweme_url", f"https://www.douyin.com/video/{aweme_id}")
            if source_url in existing:
                continue
            merged[aweme_id] = item

    return rank_candidates(list(merged.values()), top_n=top_n, sort_by=sort_by)


def _print_ranked_candidates(candidates: list[dict]) -> None:
    print(f"\n=== 排序后候选（共{len(candidates)}个，已排除库中已有素材）===")
    for i, c in enumerate(candidates):
        print(
            f"[{i}] score={c['score']:.3f} | 赞{c['liked_count']} 评{c['comment_count']} "
            f"分享{c['share_count']} 收藏{c['collected_count']} | {c['nickname']}\n"
            f"     {c['desc'][:60]}\n"
            f"     {c['source_url']}"
        )


def fetch_metadata(aweme_ids: list[str]) -> None:
    """子进程调用 MediaCrawler detail 模式抓取指定视频的元数据（含chapter_list）。

    显式传 --enable_get_meidas false：这里只用得到desc+chapter_list（ingest_metadata_only
    只读这两个字段），不需要下载视频本体——默认ENABLE_GET_MEIDAS=True会把完整video.mp4也
    下载到本地占空间，镜像trigger_search()同样的修复（同一个bug，detail模式这里之前漏改）。
    """
    subprocess.run(
        [
            str(_MEDIACRAWLER_PYTHON), "main.py",
            "--platform", "dy", "--type", "detail",
            "--specified_id", ",".join(aweme_ids),
            "--enable_get_meidas", "false",
        ],
        cwd=str(_MEDIACRAWLER_DIR),
        check=True,
    )


def load_metadata(aweme_ids: list[str]) -> dict[str, dict]:
    """读取MediaCrawler产出的jsonl，只取本次关心的aweme_id。

    detail模式（--type detail）和search模式（--type search）产出的文件名前缀不同
    （分别是 detail_contents_*.jsonl / search_contents_*.jsonl，实测确认），两种都要
    扫描——否则用search模式（关键词搜索，见README"抖音搜索"）抓到的视频会读不到元数据。
    """
    jsonl_dir = _MEDIACRAWLER_DIR / "data" / "douyin" / "jsonl"
    wanted = set(aweme_ids)
    result: dict[str, dict] = {}
    for pattern in ("detail_contents_*.jsonl", "search_contents_*.jsonl"):
        for f in jsonl_dir.glob(pattern):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("aweme_id") in wanted:
                    result[item["aweme_id"]] = item
    return result


def ingest_metadata_only(topic: str, metadata: dict[str, dict]) -> list[str]:
    """把抓到的desc+章节导览规范化落库，platform=douyin，不涉及ASR。

    返回：desc内容过短（低于_THIN_CONTENT_THRESHOLD）、建议自动补ASR的source_url列表。
    """
    from src.materials.collectors.ingest_to_raw import build_douyin_content, ingest_to_raw

    thin_urls: list[str] = []
    for aweme_id, item in metadata.items():
        desc = item.get("desc", "")
        chapters = item.get("chapter_list") or []
        content = build_douyin_content(desc, chapters)
        if not content.strip():
            print(f"  [跳过] {aweme_id} 没有可用文本内容")
            continue

        title = desc[:60] if desc else aweme_id
        source_url = item.get("aweme_url", f"https://www.douyin.com/video/{aweme_id}")
        entry = ingest_to_raw(
            topic=topic,
            title=title,
            transcript=content,
            platform="douyin",
            author=item.get("nickname", ""),  # MediaCrawler已脱敏（如"小***）"），不是真实昵称，见README已知局限
            source_url=source_url,
            publish_date=item.get("create_time", ""),
        )
        if entry is None:
            print(f"  [跳过] {aweme_id} 已在manifest中（幂等）")
            continue

        has_chapters = "含章节导览" if chapters else "仅desc"
        print(f"  [完成] {aweme_id}（{has_chapters}）-> raw/{topic}/{entry['source_file']}")
        content_len = len(content.strip())
        if content_len < _THIN_CONTENT_THRESHOLD:
            print(f"  [提示] {aweme_id} desc内容过短({content_len}字)，自动追加ASR转写")
            thin_urls.append(source_url)
    return thin_urls


def run_asr_and_ingest(topic: str, urls: list[str]) -> None:
    """对指定链接补跑ASR（复用 douyin_to_text/pipeline.py），转写结果以 platform=douyin_asr 落库。"""
    from src.materials.collectors.ingest_to_raw import ingest_to_raw

    douyin_to_text_dir = _SANDBOX_DIR / "douyin_to_text"
    subprocess.run(
        [sys.executable, str(douyin_to_text_dir / "pipeline.py"), *urls],
        check=True,
    )

    aweme_ids = [_extract_aweme_id(u) for u in urls]
    metadata = load_metadata(aweme_ids)
    output_dir = douyin_to_text_dir / "output"

    for aweme_id in aweme_ids:
        item = metadata.get(aweme_id)
        if not item:
            print(f"  [ASR跳过] {aweme_id} 找不到元数据")
            continue
        title = item.get("desc", aweme_id)
        safe_title = "".join(c for c in title if c not in '<>:"/\\|?*')[:60]
        txt_path = output_dir / f"{safe_title}.txt"
        if not txt_path.exists():
            print(f"  [ASR跳过] {aweme_id} 未找到转写产出 {txt_path.name}")
            continue

        entry = ingest_to_raw(
            topic=topic,
            title=title,
            transcript=txt_path.read_text(encoding="utf-8"),
            platform="douyin_asr",
            author=item.get("nickname", ""),
            source_url=item.get("aweme_url", f"https://www.douyin.com/video/{aweme_id}"),
            publish_date=item.get("create_time", ""),
        )
        if entry is None:
            print(f"  [ASR跳过] {aweme_id} 已在manifest中（幂等）")
        else:
            print(f"  [ASR完成] {aweme_id} -> raw/{topic}/{entry['source_file']}")


def collect_one(topic: str, source_url: str) -> dict | None:
    """采集单个抖音候选：抓详情(desc+章节) -> 写入raw+manifest；desc过短自动追加ASR。

    镜像 bilibili.py 的 collect_one，供素材agent的采集图节点直接调用（把此前耦合在
    main()里的"单条采集"逻辑拆出来）。返回写入的manifest条目——如果补了ASR，返回
    ASR版本(platform=douyin_asr)的条目；只有desc版本就返回desc版本的条目；彻底失败返回None。

    ⚠️ 一次只处理一条链接，会独立触发一次fetch_metadata（=一次MediaCrawler子进程+一次
    浏览器启动）。多条候选批量采集时应该用collect_many，不要循环调用这个函数——见
    docs/ISSUES.md：真实E2E验证时发现material_collect_node逐条调用collect_one导致
    N条候选开N次浏览器，已改成批量调用collect_many修复。
    """
    aweme_id = _extract_aweme_id(source_url)
    print(f"\n>>> 采集: {source_url}")
    fetch_metadata([aweme_id])
    metadata = load_metadata([aweme_id])
    if not metadata:
        print("  [失败] 未抓到元数据，可能登录态失效或视频不可访问")
        return None

    thin_urls = ingest_metadata_only(topic, metadata)
    entry = _find_manifest_entry(topic, source_url, "douyin")

    if source_url in thin_urls:
        print("  [提示] desc过短，自动补ASR")
        run_asr_and_ingest(topic, [source_url])
        asr_entry = _find_manifest_entry(topic, source_url, "douyin_asr")
        if asr_entry:
            entry = asr_entry
    return entry


def collect_many(topic: str, source_urls: list[str]) -> list[dict | None]:
    """批量采集多条抖音候选：一次性抓所有元数据(只启动一次MediaCrawler子进程+一次浏览器)，
    批量落库，desc过短的统一批量补ASR。返回每个source_url对应写入的manifest条目列表
    （和source_urls顺序一一对应；采集失败/找不到元数据的位置为None）。

    这是collect_one的批量版本，专门给material_collect_node这类"一次确认多条候选"的场景用——
    逐条调用collect_one会为每条候选独立开一次浏览器，真实E2E验证时发现2条候选开了2次浏览器
    窗口，见docs/ISSUES.md。fetch_metadata/ingest_metadata_only/run_asr_and_ingest本身
    都已经是批量设计的函数（main()里就是这么用的），这里只是把它们组合成一个可复用的批量入口。
    """
    if not source_urls:
        return []

    aweme_ids = [_extract_aweme_id(u) for u in source_urls]
    print(f"\n>>> 批量采集 {len(source_urls)} 条抖音候选...")
    fetch_metadata(aweme_ids)
    metadata = load_metadata(aweme_ids)
    if not metadata:
        print("  [失败] 未抓到任何元数据，可能登录态失效或视频不可访问")
        return [None] * len(source_urls)

    thin_urls = ingest_metadata_only(topic, metadata)
    entries: list[dict | None] = [_find_manifest_entry(topic, url, "douyin") for url in source_urls]

    asr_urls = [url for url in source_urls if url in thin_urls]
    if asr_urls:
        print(f"  [提示] {len(asr_urls)}条desc过短，批量补ASR")
        run_asr_and_ingest(topic, asr_urls)
        for i, url in enumerate(source_urls):
            if url in asr_urls:
                asr_entry = _find_manifest_entry(topic, url, "douyin_asr")
                if asr_entry:
                    entries[i] = asr_entry
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic", help="目的地（=素材库主题文件夹名，如 成都）")
    parser.add_argument("urls", nargs="*", help="一个或多个抖音视频链接（或纯aweme_id）；--list-candidates/--trigger-search模式下不需要")
    parser.add_argument(
        "--asr", action="store_true",
        help="强制对所有链接补ASR转写（默认只对desc内容过短(<60字)的自动补，不需要手动判断）",
    )
    parser.add_argument(
        "--trigger-search", action="store_true",
        help="触发一次MediaCrawler关键词搜索(需配合--keyword)，产出写入本地jsonl供--list-candidates读取",
    )
    parser.add_argument(
        "--list-candidates", action="store_true",
        help="不采集，只读取本地已有的search_contents_*.jsonl，排序+去重后列出候选",
    )
    parser.add_argument("--top-n", type=int, default=10, help="--list-candidates模式：排序后保留的候选数量")
    parser.add_argument(
        "--sort-by", choices=["综合", "最新", "最热"], default="综合",
        help="--list-candidates模式：排序维度",
    )
    parser.add_argument("--keyword", action="append", default=None, help="搜索/过滤关键词，可多次传入")
    args = parser.parse_args()

    if args.trigger_search:
        if not args.keyword:
            parser.error("--trigger-search 需要配合至少一个 --keyword")
        trigger_search(args.keyword)
        print("搜索完成，用 --list-candidates 查看结果。")
        return

    if args.list_candidates:
        keyword = args.keyword[0] if args.keyword else None
        candidates = list_ranked_candidates(args.topic, top_n=args.top_n, sort_by=args.sort_by, keyword=keyword)
        if not candidates:
            print("没有找到候选（可能本地没有search_contents_*.jsonl，或候选全部已在库中）。")
            return
        _print_ranked_candidates(candidates)
        print("\n从上面挑选链接，传给本脚本正式采集：")
        print(f"  python -m src.materials.collectors.douyin {args.topic} <source_url1> <source_url2> ...")
        return

    if not args.urls:
        parser.error("urls 不能为空（非--list-candidates/--trigger-search模式下必须至少传一个链接）")

    aweme_ids = [_extract_aweme_id(u) for u in args.urls]

    print(f"[1/2] 抓取 {len(aweme_ids)} 条视频元数据...")
    fetch_metadata(aweme_ids)
    metadata = load_metadata(aweme_ids)
    if not metadata:
        print("未抓到任何元数据，可能登录态失效或视频不可访问，请检查上方MediaCrawler日志。")
        return

    print(f"[2/2] 写入 raw/{args.topic}/ ...")
    thin_urls = ingest_metadata_only(args.topic, metadata)

    asr_urls = list(args.urls) if args.asr else thin_urls
    if asr_urls:
        label = "全部" if args.asr else f"{len(asr_urls)}条desc内容过短的"
        print(f"\n[补充] 对{label}视频自动补跑ASR转写...")
        run_asr_and_ingest(args.topic, asr_urls)

    print(
        f"\n采集完成。请人工审核 raw/{args.topic}/ 下的内容，确认后运行：\n"
        f"  PYTHONIOENCODING=utf-8 python -m src.materials.ingest {args.topic}"
    )


if __name__ == "__main__":
    main()
