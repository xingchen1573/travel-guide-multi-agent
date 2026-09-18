"""素材库管理页的查询、回收站软删除 + 触发采集（机制A的collect_material任务）。

素材库结构见 data/material_library/README.md：按目的地(topic)分区，
index/<topic>/entries.jsonl 是文档级元数据，chunks.jsonl 是切分后的片段，
raw/<topic>/ 是原始输入层（转写全文/图文正文）。store.py没有现成的素材库读函数
（它只管project workspace），这里直接读文件，跟src/materials/ingest.py读同一批
文件的方式保持一致。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api import jobs
from src.config import MATERIAL_LIBRARY_DIR
from src.materials import recycle_bin, topic_spaces

router = APIRouter(prefix="/api/materials", tags=["materials"])

_AGE_BUCKETS = ["近3个月", "近半年", "近1年", "1年以上", "未知"]
_TOP_AUTHOR_LIMIT = 8


def _read_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _topic_names() -> list[str]:
    return topic_spaces.list_active_topics()


def _raw_word_counts(topic: str, entries: list[dict]) -> dict[str, int]:
    """按raw/<topic>/*_原文.md原文字符数逐条计算——不含_导读.md、不走chunk口径
    （chunker用重叠窗口切分，按chunk求和会重复计入重叠文本导致字数虚高），见
    get_topic_stats的grilling讨论记录。"""
    raw_dir = MATERIAL_LIBRARY_DIR / "raw" / topic
    counts: dict[str, int] = {}
    for entry in entries:
        doc_id = entry.get("doc_id", "")
        source_file = entry.get("source_file")
        text = ""
        if source_file:
            raw_path = raw_dir / source_file
            if raw_path.exists():
                text = raw_path.read_text(encoding="utf-8")
        counts[doc_id] = len(text)
    return counts


@router.get("")
def list_topics() -> list[dict]:
    topics = []
    for topic in _topic_names():
        entries = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl")
        chunks = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "chunks.jsonl")
        references = topic_spaces.topic_project_references(topic)
        topics.append({
            "topic": topic,
            "entry_count": len(entries),
            "core_count": sum(1 for e in entries if e.get("is_core")),
            "chunk_count": len(chunks),
            "total_word_count": sum(_raw_word_counts(topic, entries).values()),
            **references,
        })
    return topics


@router.get("/{topic}")
def get_topic_entries(topic: str) -> list[dict]:
    if topic not in _topic_names():
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 不存在")
    return _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl")


@router.delete("/{topic}")
def delete_topic(topic: str) -> dict:
    running_types = [
        task_type
        for task_type in ("collect_material", "regenerate_insights")
        if jobs.is_running(task_type, topic)
    ]
    if running_types:
        raise HTTPException(
            status_code=409,
            detail={"message": "素材空间仍有后台任务运行，暂时不能删除", "running_tasks": running_types},
        )
    try:
        summary = topic_spaces.soft_delete_topic(topic)
    except topic_spaces.TopicInUseError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "active_project_ids": exc.project_ids,
            },
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**summary, "status": "trashed"}


@router.get("/{topic}/entries/{doc_id}")
def get_entry_detail(topic: str, doc_id: str) -> dict:
    """点进具体一条素材：entry元数据 + 该doc_id下所有chunk文本 + raw/原文（如果找得到）。

    这是本次范围新增的真实缺口——此前完全没暴露chunks.jsonl（真正参与检索的切分文本）
    和raw/目录下的原文，用户只能看到entries.jsonl的文档级元数据，见docs/ARCHITECTURE.md
    第13.2节。
    """
    if topic not in _topic_names():
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 不存在")

    entries = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl")
    entry = next((e for e in entries if e.get("doc_id") == doc_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"doc_id {doc_id!r} 在topic {topic!r} 下不存在")

    all_chunks = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "chunks.jsonl")
    chunks = sorted(
        (c for c in all_chunks if c.get("doc_id") == doc_id),
        key=lambda c: c.get("order", 0),
    )

    raw_text = None
    source_file = entry.get("source_file")
    if source_file:
        raw_path = MATERIAL_LIBRARY_DIR / "raw" / topic / source_file
        if raw_path.exists():
            raw_text = raw_path.read_text(encoding="utf-8")

    return {"entry": entry, "chunks": chunks, "raw_text": raw_text}


@router.delete("/{topic}/entries/{doc_id}")
def delete_entry(topic: str, doc_id: str) -> dict:
    """把素材从当前索引移入回收站，保留原文、chunks和向量以供恢复。"""
    try:
        summary = recycle_bin.soft_delete_material(topic, doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**summary, "status": "trashed"}


@router.get("/{topic}/entity_stats")
def get_entity_stats(topic: str) -> list[dict]:
    """跨全库统计每个实体一共被多少条素材提到过，按count降序返回。

    2026-07-26新增（管理后台反馈驱动）：此前entries.jsonl里的entities只是每条素材各自
    独立的一份列表，从没有跨全库聚合统计过"某个实体一共出现在几条素材里"——热门地标
    理应在更多素材里被反复提到，这个次数本身是有用信号（判断素材覆盖是否扎实、或者反过来
    发现"只出现1次"的冷门实体值得针对性核实/补充采集，见docs/ISSUES.md第42条同类判断）。

    按字符串精确匹配聚合，不做实体规范化/别名合并——"武侯祠"和"武侯祠博物馆"这类同指
    不同写法的实体会被计成两条，是已知的粗粒度限制（跟entities字段本身"每条素材各自独立
    抽取、没有跨条目消歧"的既有设计一致，见prompt.md）。
    """
    if topic not in _topic_names():
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 不存在")

    entries = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl")
    counter: Counter[str] = Counter()
    doc_ids_by_entity: dict[str, list[str]] = {}
    for entry in entries:
        doc_id = entry.get("doc_id", "")
        for entity in entry.get("entities", []):
            counter[entity] += 1
            doc_ids_by_entity.setdefault(entity, []).append(doc_id)

    return [
        {"entity": entity, "count": count, "doc_ids": doc_ids_by_entity[entity]}
        for entity, count in counter.most_common()
    ]


def _age_bucket(publish_date: str, now: datetime) -> str:
    """把发布日期换算成"内容年龄"分桶——这只是弱代理信号（发布越久不代表内容一定过时，
    只是更可能），不是真的时效性判断（那个需要联网核实，见route_timeliness_check_node）。
    空值/格式不认识的一律归"未知"，不能悄悄跳过——见grilling讨论，小红书采集路径完全
    没有这个字段来源，"未知"必须显式可见。
    """
    if not publish_date:
        return "未知"
    try:
        dt = datetime.fromisoformat(publish_date).replace(tzinfo=timezone.utc)
    except ValueError:
        return "未知"
    days = (now - dt).days
    if days <= 90:
        return "近3个月"
    if days <= 180:
        return "近半年"
    if days <= 365:
        return "近1年"
    return "1年以上"


@router.get("/{topic}/stats")
def get_topic_stats(topic: str) -> dict:
    """素材库整体统计画像（2026-08-22 grilling讨论驱动新增）：平台分布+字数、内容年龄
    分布、核心素材占比、作者多样性、拒绝理由。跟entity_stats一样现算不缓存——单topic
    量级最大也就一两百条，没必要为此引入缓存失效逻辑（这个项目已经在别处因缓存没
    正确失效踩过好几次坑，见docs/ISSUES.md）。

    刻意不做的：跨project被引用次数/贡献度——那个依赖final_guide.meta.json.references，
    会被联网核实产生的合成chunk_id污染口径，且跟"素材库本身健康度"是两件事，讨论后
    明确排除在这次范围外。
    """
    if topic not in _topic_names():
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 不存在")

    entries = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl")
    rejected = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "rejected.jsonl")

    # 总字数口径：raw/<topic>/*_原文.md原文字符数，不含_导读.md（那是给人看的精选摘要，
    # 不是素材本体）；chunk口径会因chunker的重叠窗口（flat_transcript._tail_overlap）
    # 重复计入相邻chunk的重叠文本，字数会虚高，见grilling讨论。
    word_counts = _raw_word_counts(topic, entries)

    # 平台分布：条数+字数+占比（按字数）+该平台最长一篇
    platform_stats: dict[str, dict] = {}
    for entry in entries:
        platform = entry.get("platform") or "未知"
        doc_id = entry.get("doc_id", "")
        wc = word_counts.get(doc_id, 0)
        bucket = platform_stats.setdefault(
            platform, {"platform": platform, "count": 0, "word_count": 0, "longest": None}
        )
        bucket["count"] += 1
        bucket["word_count"] += wc
        if bucket["longest"] is None or wc > bucket["longest"]["word_count"]:
            bucket["longest"] = {"doc_id": doc_id, "title": entry.get("title", ""), "word_count": wc}

    total_word_count = sum(b["word_count"] for b in platform_stats.values())
    platform_breakdown = [
        {**b, "percentage": round(b["word_count"] / total_word_count * 100, 1) if total_word_count else 0.0}
        for b in sorted(platform_stats.values(), key=lambda x: x["word_count"], reverse=True)
    ]

    # 内容年龄分布：每个桶再按平台拆分——"未知"这档强制要求按平台展示，其余桶顺带一起给，
    # 前端只在"未知"档用到这个细分，见grilling讨论。
    now = datetime.now(timezone.utc)
    age_by_bucket: dict[str, dict[str, int]] = {b: {} for b in _AGE_BUCKETS}
    for entry in entries:
        bucket = _age_bucket(entry.get("publish_date", ""), now)
        platform = entry.get("platform") or "未知"
        age_by_bucket[bucket][platform] = age_by_bucket[bucket].get(platform, 0) + 1
    age_distribution = [
        {"bucket": b, "count": sum(age_by_bucket[b].values()), "by_platform": age_by_bucket[b]}
        for b in _AGE_BUCKETS
    ]

    core_count = sum(1 for e in entries if e.get("is_core"))
    core_ratio = {"core_count": core_count, "non_core_count": len(entries) - core_count}

    author_counter: Counter[str] = Counter()
    for entry in entries:
        author_counter[entry.get("author") or "未知"] += 1
    ranked_authors = author_counter.most_common()
    top_authors, other_authors = ranked_authors[:_TOP_AUTHOR_LIMIT], ranked_authors[_TOP_AUTHOR_LIMIT:]
    author_ranking = {
        "top": [{"author": a, "count": c} for a, c in top_authors],
        "other_author_count": len(other_authors),
        "other_entry_count": sum(c for _, c in other_authors),
    }

    # 拒绝理由二分类："内容过短"是ingest.py固定模板字符串（结构上可靠），"不相关"是LLM
    # 生成的自由文本，不做进一步细分（关键词猜测容易分错，见grilling讨论）。
    too_short = [r for r in rejected if r.get("reason", "").startswith("内容过短")]
    irrelevant = [r for r in rejected if not r.get("reason", "").startswith("内容过短")]
    rejection_stats = {
        "too_short_count": len(too_short),
        "irrelevant_count": len(irrelevant),
        "irrelevant_reasons": [
            {"source_file": r.get("source_file", ""), "title": r.get("title", ""), "reason": r.get("reason", "")}
            for r in irrelevant
        ],
    }

    return {
        "topic": topic,
        "entry_count": len(entries),
        "total_word_count": total_word_count,
        "platform_breakdown": platform_breakdown,
        "age_distribution": age_distribution,
        "core_ratio": core_ratio,
        "author_ranking": author_ranking,
        "rejection_stats": rejection_stats,
    }


@router.get("/{topic}/insights")
def get_insights(topic: str) -> dict | None:
    """读取index/<topic>/insights.json（LLM从素材库整体提炼的跨来源共识：热门必去/
    饮食/避雷/贴士/其他，见src/materials/insights.py）。还没生成过时返回None，不是404
    ——topic本身是存在的，只是这个派生产物还没跑过。
    """
    if topic not in _topic_names():
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 不存在")
    path = MATERIAL_LIBRARY_DIR / "index" / topic / "insights.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@router.post("/{topic}/insights/regenerate", status_code=202)
def regenerate_insights(topic: str) -> dict:
    """触发洞察重新生成，接入jobs.py通用任务框架（2026-08-01改造，此前是同步阻塞
    请求——用户切走页面再切回来，承载"生成中"状态的页面组件已经卸载，看起来像是
    "中断了"，其实那次fetch请求本身还在后台跑；改成后台任务后状态落在服务端，
    前端轮询GET /insights/regenerate/status即可查到真实进度，不依赖页面组件生命周期）。

    entry数<5这条素材不足的判断在这里前置同步校验、直接返回422快速失败——不进任务
    框架：这条检查本身就是读一下entries.jsonl行数，不涉及LLM调用，没必要为了统一
    形式而绕一圈异步。
    """
    from src.materials.insights import MIN_ENTRIES_FOR_INSIGHTS

    if topic not in _topic_names():
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 不存在")
    entries = _read_jsonl(MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl")
    if len(entries) < MIN_ENTRIES_FOR_INSIGHTS:
        raise HTTPException(status_code=422, detail="素材不足（entry数<5），暂无法生成洞察")
    try:
        jobs.trigger_regenerate_insights(topic)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"topic": topic, "status": "running"}


@router.get("/{topic}/insights/regenerate/status")
def get_regenerate_insights_status(topic: str) -> dict:
    status = jobs.get_status("regenerate_insights", topic)
    if status is None:
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 没有洞察重新生成任务记录")
    return status


class CollectRequest(BaseModel):
    request: str
    platform: Literal["all", "bilibili", "douyin"] = "all"


@router.post("/{topic}/collect", status_code=202)
def trigger_collect(topic: str, body: CollectRequest) -> dict:
    """触发核心素材agent的自主采集（提案->自动确认->采集入库，auto_confirm=True，
    见src/agents/material_agent/collection_graph.py）。此前这个能力只能靠独立CLI
    （scripts/materials/run_material_agent.py）单独调用，这次收进后台，见docs/ARCHITECTURE.md第13.2节。

    platform（2026-07-26新增）：透传给jobs.trigger_collect_material做工具级硬过滤，
    供管理后台采集对话框的显式平台选择器使用（此前只能靠agent自己从request文字猜平台）。
    """
    try:
        jobs.trigger_collect_material(topic, body.request, platform=body.platform)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"topic": topic, "status": "running"}


@router.get("/{topic}/collect/status")
def get_collect_status(topic: str) -> dict | None:
    status = jobs.get_status("collect_material", topic)
    if status is None:
        raise HTTPException(status_code=404, detail=f"topic {topic!r} 没有采集任务记录")
    return status
