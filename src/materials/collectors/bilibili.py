"""B站素材半自动采集编排：搜索 -> 筛选排序 -> 人工勾选 -> 下音轨 -> ASR转写 -> 写入 raw/ + manifest。

这是"半自动"：脚本负责把 sandbox 下已有的独立环节（bilibili_search / bilibili_to_mp3 /
mimo_asr）串起来并规范化落库，但**中间有一道人工勾选关口**（选哪些视频），落库后仍需
人工审核转写质量、剔恰饭段，再手动跑 `python -m src.materials.ingest <目的地>` 入库。
见 data/material_library/README.md "半自动采集pipeline"。

用法（作为CLI脚本手动跑）：
    # 交互式：搜索并列出候选，手动输入要采集的序号
    python -m src.materials.collectors.bilibili 成都 --keyword "成都旅游攻略" --keyword "成都三日游"

    # 非交互：直接取排序后的前 N 个（适合批量，但更要人工审核落库结果）
    python -m src.materials.collectors.bilibili 成都 --keyword "成都旅游攻略" --auto-top 3

也可以被素材agent的采集工具（src/tools/material_collection_tools.py）以Python函数方式调用，
search_and_rank/collect_one 是agent工具直接复用的两个核心函数。

前提：项目 .venv 里已装 bilibili-api-python / yt-dlp，系统有 ffmpeg，.env 配好 MIMO_API_KEY。
大会员/登录才能看的视频，download_mp3 支持 --cookies，这里暂不透传（默认公开视频）。

【转正记录】本文件原是 sandbox/pipeline/collect_bilibili.py，经过多轮真实验证+单元测试后
正式迁入 src/materials/collectors/（见 docs/ISSUES.md 相关记录）。迁移时把 _VENV_PYTHON 改成了
sys.executable——现在是被 src/ 正常 import 调用，天然跑在项目venv下，不用再猜路径；
bilibili_to_mp3/mimo_asr 这些真正独立单一用途的工具脚本仍留在 sandbox/，被 subprocess 调用。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from src.materials.collectors.ranking import rank_candidates

sys.stdout.reconfigure(errors="replace")

_SANDBOX_DIR = Path(__file__).resolve().parents[3] / "sandbox"
_MP3_OUTPUT_DIR = _SANDBOX_DIR / "bilibili_to_mp3" / "output"


class CollectorDependencyError(RuntimeError):
    """采集环境依赖缺失（系统级ffmpeg/ffprobe、.env里的MIMO_API_KEY）。

    见docs/ISSUES.md第77条：此前`collect_one`对`download_mp3`失败的处理是静默
    `return None`，导致这台机器缺ffmpeg时，"入库0条"被误诊断成"素材库该目的地
    真的搜不到相关内容/候选质量差"，排查了很久才发现是环境依赖问题——跟内容质量
    完全无关。现在改成在真正开始下载前就显式检查一次，缺失时立刻抛出这个异常，
    不再让环境问题伪装成内容判断的结果。
    """


def check_collector_dependencies() -> None:
    """调用真正的下载/转写前，先检查系统级ffmpeg/ffprobe和ASR key是否就位，
    严格拦截——环境依赖缺失时必然会失败，绝不能让流程继续跑下去把结果误判成
    "这个topic真的搜不到内容"（见docs/ISSUES.md第77条真实教训）。

    先尝试`static-ffmpeg`这个pip包自带的静态二进制自愈（如果已安装），失败/未安装
    时才真正报错——这不是"悄悄放过检查"，是在硬拦截之前先尝试一个已知可行、
    不需要用户手动装Homebrew的修复路径，仍然只检查"存不存在"，不检查"能不能真正
    调通"（网络问题、key是否有效额度等仍然要等到真实调用时才知道）。

    ASR key检查：2026-08-04起ASR主渠道切到硅基 TeleSpeechASR，MiMo降为兜底。
    硅基 key（SILICONFLOW_ASR_API_KEYS）和 MiMo key（MIMO_API_KEYS）只要配了任意
    一个就放行——sandbox/mimo_asr/test_asr.py 内部按"硅基优先、MiMo兜底"分发。
    两个都没配才报错。这跟旧逻辑（只检查MIMO_API_KEYS）的区别是：现在硅基是主渠道，
    只配硅基不配MiMo也完全够用，不应该被旧的"必须有MiMo key"检查误拦。
    """
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        try:
            import static_ffmpeg

            static_ffmpeg.add_paths()
        except Exception:
            pass  # 没装这个可选包，或自愈本身失败——下面的严格检查会照实报错

    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    if shutil.which("ffprobe") is None:
        missing.append("ffprobe")
    if missing:
        raise CollectorDependencyError(
            f"缺少系统级依赖：{'/'.join(missing)}（PATH里找不到）。"
            "B站采集pipeline下载音轨这一步（yt-dlp的FFmpegExtractAudio postprocessor）"
            "硬依赖这两个二进制，缺失时下载会先成功拿到.m4a、再在转码阶段静默失败，"
            "容易被误判成\"素材库该目的地搜不到内容\"（见docs/ISSUES.md第77条）。"
            "安装：macOS用Homebrew（brew install ffmpeg）；没有Homebrew时可用pip装"
            "static-ffmpeg（pip install static-ffmpeg，本函数会自动调用它的add_paths()），"
            "或从ffmpeg.org下载静态构建后加入系统PATH。"
        )

    from src.config import MIMO_API_KEYS, SILICONFLOW_ASR_API_KEYS

    if not MIMO_API_KEYS and not SILICONFLOW_ASR_API_KEYS:
        raise CollectorDependencyError(
            "缺少ASR key：SILICONFLOW_ASR_API_KEY(S) 和 MIMO_API_KEY(S) 都未配置。"
            "B站采集pipeline的ASR转写步骤（sandbox/mimo_asr）至少需要配一个——"
            "2026-08-04起硅基 TeleSpeechASR 是主渠道（SILICONFLOW_ASR_API_KEYS，"
            "跟EMBEDDING_BASE_URL同平台但独立申请的ASR key），MiMo（MIMO_API_KEYS）"
            "作兜底。只配硅基不配MiMo也够用，反之亦然，两个都没配才会被这里拦截。"
        )


async def _search(keyword: str, page: int = 1) -> list[dict]:
    from bilibili_api import search

    result = await search.search_by_type(
        keyword, search_type=search.SearchObjectType.VIDEO, page=page
    )
    return result.get("result", [])


def _load_existing_source_urls(
    topic: str, *, library_dir: Path | None = None
) -> set[str]:
    """读取 raw/<topic>/manifest.json 里已有的 source_url 集合，用于采集前排除已入库的视频。

    只在 source_url 级别去重（防"同一视频被重复抓取"），不做内容语义级别的去重——
    多个不同视频覆盖同一话题是"多来源印证"信号的一部分（见docs/ARCHITECTURE.md第8.2节），
    不应该被过滤掉，见docs/ISSUES.md第14条。
    """
    from src.materials.collectors.ingest_to_raw import MATERIAL_LIBRARY_DIR, _load_manifest

    manifest = _load_manifest((library_dir or MATERIAL_LIBRARY_DIR) / "raw" / topic)
    return {item.get("source_url", "") for item in manifest.get("items", []) if item.get("source_url")}


def search_and_rank(
    keywords: list[str],
    top_n: int,
    topic: str | None = None,
    sort_by: str = "综合",
    page: int = 1,
    *,
    library_dir: Path | None = None,
) -> list[dict]:
    """多关键词搜索，合并去重（按 bvid），排除已入库的候选，统一排序，返回前 top_n 个候选。

    topic 传入时才会做"已入库"过滤（避免对着库里已有的视频重新下载+ASR，白白浪费成本，
    见docs/ISSUES.md第14条）；不传 topic 则跳过这一步，仅做同批次内部的bvid去重。

    page：B站搜索翻页参数，默认第1页。候选不够数时素材agent会换关键词或翻页重新调用
    （见docs/ISSUES.md第14条"候选不够数不能提前认怂"）。
    """
    merged: dict[str, dict] = {}
    for kw in keywords:
        for item in asyncio.run(_search(kw, page=page)):
            bvid = item.get("bvid")
            if bvid and bvid not in merged:
                merged[bvid] = item

    if topic:
        existing = _load_existing_source_urls(topic, library_dir=library_dir)
        merged = {
            bvid: item for bvid, item in merged.items()
            if f"https://www.bilibili.com/video/{bvid}" not in existing
        }

    return rank_candidates(list(merged.values()), top_n=top_n, sort_by=sort_by)


def _print_candidates(candidates: list[dict]) -> None:
    print(f"\n=== 排序后候选（共{len(candidates)}个）===")
    for i, c in enumerate(candidates):
        print(
            f"[{i}] score={c['score']:.3f} | {c['duration']} | "
            f"播放{c['play']} 赞{c['like']} | {c['author']}\n"
            f"     {c['title']}\n"
            f"     {c['source_url']}"
        )


def _select_interactive(candidates: list[dict]) -> list[dict]:
    _print_candidates(candidates)
    raw = input("\n输入要采集的序号（逗号分隔，直接回车取消）: ").strip()
    if not raw:
        return []
    picked = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit() and 0 <= int(token) < len(candidates):
            picked.append(candidates[int(token)])
    return picked


def download_mp3(source_url: str) -> Path | None:
    """子进程调用现有的 bilibili_to_mp3/download_mp3.py 下音轨，返回产出的 mp3 路径。

    直接解析脚本stdout里"已保存到: <path>"这一行拿到确切路径，不用目录前后差集去猜——
    差集在"同一标题的视频被重复采集/output目录里已有同名旧文件"时会误判：新文件其实
    下载成功了，但因为下载前该文件名就已存在，前后集合相同，diff算出空集，被错误报告
    为"下载失败"（真实复现过：批量采集时命中了之前预览测试用过的同一条候选）。
    """
    try:
        result = subprocess.run(
            [sys.executable, str(_SANDBOX_DIR / "bilibili_to_mp3" / "download_mp3.py"), source_url],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as e:
        # 见docs/ISSUES.md第88条：这里以前不捕获，子进程非0退出（如ffmpeg转码postprocessor
        # 失败）会让异常原样往上炸穿collect_one，material_collect_node/CLI main()的批量
        # for循环没有try/except，一条视频下载失败就会中断整批采集。改成返回None，让
        # collect_one已有的"未能下载到音轨"分支正常处理、继续采集下一条。
        print(f"  [失败] 下音轨子进程出错：{(e.stderr or str(e)).strip()}")
        return None
    print(result.stdout, end="")
    for line in result.stdout.splitlines():
        if line.startswith("已保存到: "):
            path = Path(line[len("已保存到: "):].strip())
            return path if path.exists() else None
    return None


def transcribe(mp3_path: Path) -> Path:
    """子进程调用现有的 mimo_asr/transcribe_long.py，产出同名 .txt。"""
    subprocess.run(
        [sys.executable, str(_SANDBOX_DIR / "mimo_asr" / "transcribe_long.py"), str(mp3_path)],
        check=True,
    )
    return mp3_path.with_suffix(".txt")


def collect_one(
    topic: str,
    candidate: dict,
    *,
    library_dir: Path | None = None,
) -> dict | None:
    """采集单个候选：下音轨 -> 转写 -> 写入 raw/ + manifest。返回写入的manifest条目，失败/幂等跳过返回None。

    见docs/ISSUES.md第88条：`mp3_path.unlink()`此前放在transcribe()成功之后，只覆盖了
    "转写顺利完成"这一条路径——ASR转写失败时（MiMo限流/欠费/网络错误，subprocess.run(check=True)
    直接抛CalledProcessError）异常会原样传播出collect_one，unlink这行代码根本执行不到，
    音轨永久留在sandbox/bilibili_to_mp3/output/。真实复现：3.3GB/364个文件的遗留音频，
    其中多个是当天刚下载转写失败、码率完全正常的.mp3（不是历史debris）。现在用try/finally
    把"删除音轨"和"转写是否成功"彻底解耦——不管转写成功、产出空文本、还是直接抛异常，
    音轨都在finally里保证被删掉。同时不再让转写异常向上传播炸穿调用方的批量for循环。
    """
    from src.materials.collectors.ingest_to_raw import ingest_to_raw

    print(f"\n>>> 采集: {candidate['title']}")
    mp3_path = download_mp3(candidate["source_url"])
    if not mp3_path or not mp3_path.exists():
        print("  [失败] 未能下载到音轨，跳过")
        return None

    try:
        txt_path = transcribe(mp3_path)
        if not txt_path.exists():
            print("  [失败] 转写未产出文本，跳过")
            return None
        transcript = txt_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  [失败] 转写过程出错，跳过：{e}")
        return None
    finally:
        mp3_path.unlink(missing_ok=True)  # 无论转写成功/失败，音轨都是冗余产物，必须清理

    entry = ingest_to_raw(
        topic=topic,
        title=candidate["title"],
        transcript=transcript,
        platform="bilibili_asr",  # ASR flat文本，绝不能用 bilibili（见 README/ISSUES第10条）
        author=candidate["author"],
        source_url=candidate["source_url"],
        # candidate["pubdate"]直接索引在真实E2E里崩溃过（KeyError）——propose_candidates
        # 工具的docstring只要求candidates含platform/source_url/title/author/score，
        # 没提pubdate，agent经过工具间转手重新组装候选时会如实丢掉没被要求的字段，
        # 不是agent编造/疏漏。改用.get兜底为0（ingest_to_raw._to_publish_date对falsy值
        # 会正确处理成空字符串），避免因为一个非核心字段缺失让整个采集流程崩溃。
        publish_date=candidate.get("pubdate", 0),
        summary=candidate.get("description", ""),
        library_dir=library_dir,
    )
    if entry is None:
        print("  [跳过] 该 source_url 已在 manifest 中（幂等）")
    else:
        print(f"  [完成] 已写入 raw/{topic}/{entry['source_file']}")
    return entry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic", help="目的地（=素材库主题文件夹名，如 成都）")
    parser.add_argument("--keyword", action="append", required=True, help="搜索关键词，可多次传入")
    parser.add_argument("--top-n", type=int, default=10, help="排序后保留的候选数量")
    parser.add_argument("--auto-top", type=int, default=0, help="非交互模式：直接采集排序后前N个")
    parser.add_argument(
        "--sort-by", choices=["综合", "最新", "最热"], default="综合",
        help="排序维度：综合(默认，播放/点赞/时长/时新加权)、最新(纯按发布时间)、最热(纯按播放+点赞)",
    )
    args = parser.parse_args()

    try:
        check_collector_dependencies()
    except CollectorDependencyError as e:
        # 见docs/ISSUES.md第88条：CLI直接手动调用这条路径此前从未做过这个前置校验
        # （只有orchestrator.py/collection_graph.py两条生产路径接了），本机ffmpeg不在
        # 系统PATH时，静默依赖static_ffmpeg自愈——但如果自愈也失败，会在下载阶段才
        # 崩溃且留下不完整的.m4a垃圾文件，不如在这里提前显式拦截。
        print(f"[环境依赖缺失] {e}")
        return

    candidates = search_and_rank(args.keyword, top_n=args.top_n, topic=args.topic, sort_by=args.sort_by)
    if not candidates:
        print("没有通过筛选的候选视频（可能全部已在库中，或没有满足硬过滤条件的结果）。")
        return

    if args.auto_top > 0:
        picked = candidates[: args.auto_top]
        _print_candidates(picked)
    else:
        picked = _select_interactive(candidates)

    if not picked:
        print("未选择任何视频，退出。")
        return

    for c in picked:
        collect_one(args.topic, c)

    print(
        f"\n全部采集完成。请人工审核 raw/{args.topic}/ 下的转写质量（剔恰饭段/复读），"
        f"确认后运行：\n  PYTHONIOENCODING=utf-8 python -m src.materials.ingest {args.topic}"
    )


if __name__ == "__main__":
    main()
