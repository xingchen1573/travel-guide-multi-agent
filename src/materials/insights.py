"""素材库LLM洞察挖掘：从某个topic的全部素材里提炼跨来源共识（热门必去/饮食/避雷/贴士/其他）。

独立于主生成链路（route_agent/lifestyle_agent等不读取这里的产物），只服务管理后台
"素材洞察"页面，见docs/ARCHITECTURE.md（改动时补充对应小节）。可靠性靠结构性约束而
不是二次LLM核验：①只让LLM给出summary+它依据的doc_ids，author/chunk_id/excerpt全部
由程序侧从entries.jsonl/chunks.jsonl真实数据回填，LLM没有编造这些字段的机会；
②回填后用entries.jsonl里ingest时就已经抽取好的entities（已经是被信任的真实数据）
反查summary是否至少提到了其中一个，提不到就整条丢弃——不是重新调一次LLM去核验。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.config import DEEPSEEK_CHAT_MODEL, MATERIAL_LIBRARY_DIR, get_structured_chat_model
from src.structured_output import bind_structured_json
from src.materials.citation_check import _entity_matches
from src.workspace.schema import InsightItem, InsightSourceRef, TopicInsights

MIN_ENTRIES_FOR_INSIGHTS = 5

# 分批喂入时单批素材总字符预算。insights 是"跨来源共识提炼"，单批素材太少提不出共识、
# 太多则输出端 JSON 撑爆 provider 默认 max_tokens（150 条全量拼曾撞 openai.LengthFinishReasonError，
# completion_tokens 撞上限被截断，不是 prompt 超 context）。按字符预算贪心累加 entry 全文到预算成批，
# 末尾不满一批单独成批。沿用项目"字符数"惯例（chunkers/ingest 长度门禁都按字符数，见
# chunkers/generic.py、ingest._MIN_CONTENT_LENGTH），不引入 tiktoken 之类的 token 估算。
_BATCH_CHAR_BUDGET = 20000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_CATEGORY_KEYS = ("must_visit", "food", "pitfalls", "tips", "other")

_CATEGORY_PROMPT_HINT = """
- must_visit：被反复提及、值得专程去的景点/体验
- food：被反复提及的美食/店铺
- pitfalls：多条素材独立提到的同一个坑（排队/被坑/体验差/收费不透明等）
- tips：反复出现的非景点类实用经验（交通/穿衣/预约技巧等）
- other：以上四类都不太合适、但对旅游确实有参考价值的其它发现
"""


class _RawInsightItem(BaseModel):
    summary: str = Field(description="一句话结论，说明素材里普遍反映的内容")
    doc_ids: list[str] = Field(description="支撑这条结论的素材doc_id列表，必须是真实存在于给定素材里的doc_id")


class _RawTopicInsights(BaseModel):
    must_visit: list[_RawInsightItem] = Field(default_factory=list)
    food: list[_RawInsightItem] = Field(default_factory=list)
    pitfalls: list[_RawInsightItem] = Field(default_factory=list)
    tips: list[_RawInsightItem] = Field(default_factory=list)
    other: list[_RawInsightItem] = Field(default_factory=list)


def _topic_index_dir(topic: str) -> Path:
    return MATERIAL_LIBRARY_DIR / "index" / topic


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _entry_full_text(entry: dict, chunks_by_doc: dict[str, list[dict]]) -> str:
    """单条 entry 的全部 chunk 文本拼接（与 _build_prompt 里拼素材用的同一段拼接逻辑，
    抽成函数复用：分批切分要按"单条 entry 全文长度"算预算，不能只算 chunk 数量）。"""
    return "".join(c["text"] for c in chunks_by_doc.get(entry["doc_id"], []))


def _split_entries_into_batches(
    entries: list[dict],
    chunks_by_doc: dict[str, list[dict]],
    budget: int = _BATCH_CHAR_BUDGET,
) -> list[list[dict]]:
    """按字符预算贪心把 entries 切成若干批，供分批 invoke 用。

    关键细节：单条 entry 全文超过 budget 时，该条**单独成批不强行拆 entry**——拆 entry 会破坏
    doc_id→文本的原子性，insights 的 doc_ids 回指会乱（一条结论引用的 doc 必须完整喂给模型），
    允许单批超预算。返回 list[list[entry]]，至少一批（entries 非空时）。
    """
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for entry in entries:
        text_len = len(_entry_full_text(entry, chunks_by_doc))
        # 当前列已有内容且加入这条会超预算 → 先把当前列成一批，再另起新列
        if current and current_chars + text_len > budget:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(entry)
        current_chars += text_len
    if current:
        batches.append(current)
    return batches


def _build_prompt(
    topic: str,
    entries: list[dict],
    chunks_by_doc: dict[str, list[dict]],
    batch_index: int | None = None,
    batch_total: int | None = None,
) -> str:
    doc_blocks = []
    for entry in entries:
        doc_id = entry["doc_id"]
        author = entry.get("author", "") or "未知作者"
        texts = [c["text"] for c in chunks_by_doc.get(doc_id, [])]
        doc_blocks.append(f"[{doc_id}] 作者：{author}\n{''.join(texts)}")
    joined = "\n\n".join(doc_blocks)

    # 分批语义提示：诚实告诉模型"你看到的不全"，避免它把"这批没提到 X"误判成"X 不值得去"。
    # batch_index/batch_total 都给定时才加这句，默认 None 时文案跟改造前完全一致（不破坏
    # 单批调用场景，虽然本模块改造后唯一调用点都是分批循环）。
    batch_note = ""
    if batch_index is not None and batch_total is not None and batch_total > 1:
        batch_note = (
            f"注意：以下素材是「{topic}」的**部分**素材（第 {batch_index}/{batch_total} 批），"
            f"不是全部。请仅基于这批素材提炼共识，不要因为某内容在这批没出现就判定它不存在。\n\n"
        )

    return (
        f"{batch_note}"
        f"下面是关于「{topic}」的一批真实旅游/生活分享素材，每条都标注了doc_id和作者。\n"
        f"请从中提炼跨多条素材反复出现的共识性结论，分成以下5类：\n{_CATEGORY_PROMPT_HINT}\n"
        f"要求：\n"
        f"1. 只输出真实存在于给定素材内容里的结论，不要编造素材里没有提到的内容。\n"
        f"2. 每条结论的doc_ids必须是上面给出的真实doc_id，且这些素材确实支撑这条结论。\n"
        f"3. 没有明显发现的类别可以留空列表。\n"
        f"4. 请以JSON格式回复，包含must_visit/food/pitfalls/tips/other五个字段，"
        f"每个字段是对象数组，每个对象包含summary(字符串)、doc_ids(字符串数组)。\n\n"
        f"素材内容：\n{joined}"
    )


def _pick_supporting_chunk(
    doc_id: str, summary: str, chunks_by_doc: dict[str, list[dict]]
) -> dict | None:
    """在doc的多个chunk里挑出实际支撑summary的那个，而不是无脑取chunk[0]。

    video/长文转写常见"开场白"chunk[0]跟结论毫无关系（真实案例：青甘大环线-197的
    c000是视频开场自我介绍，真正支撑"敦煌夜市美食"这条结论的是c004）。用summary跟
    每个chunk文本的字符bigram重叠数打分选最相关的chunk；重叠全为0（找不到更好的）时
    回退到chunk[0]，保持原有行为不倒退。"""
    doc_chunks = chunks_by_doc.get(doc_id, [])
    if not doc_chunks:
        return None
    if len(doc_chunks) == 1:
        return doc_chunks[0]
    summary_bigrams = {summary[i : i + 2] for i in range(len(summary) - 1)}
    if not summary_bigrams:
        return doc_chunks[0]
    best_chunk = doc_chunks[0]
    best_score = -1
    for chunk in doc_chunks:
        text = chunk["text"]
        score = sum(1 for bg in summary_bigrams if bg in text)
        if score > best_score:
            best_score = score
            best_chunk = chunk
    return best_chunk


def _pick_excerpt(chunk: dict | None) -> str:
    if not chunk:
        return ""
    return chunk["text"][:200]


def _confidence_and_sources(
    raw_item: _RawInsightItem,
    entries_by_id: dict[str, dict],
    chunks_by_doc: dict[str, list[dict]],
) -> tuple[Literal["consensus", "single_source"], list[InsightSourceRef]]:
    sources: list[InsightSourceRef] = []
    authors: set[str] = set()
    for doc_id in raw_item.doc_ids:
        entry = entries_by_id.get(doc_id)
        if entry is None:
            continue  # LLM引用了不存在的doc_id，直接忽略这条来源
        author = entry.get("author", "")
        if author:
            authors.add(author)
        chunk = _pick_supporting_chunk(doc_id, raw_item.summary, chunks_by_doc)
        sources.append(
            InsightSourceRef(
                doc_id=doc_id,
                chunk_id=chunk["chunk_id"] if chunk else "",
                author=author,
                excerpt=_pick_excerpt(chunk),
            )
        )
    confidence: Literal["consensus", "single_source"] = "consensus" if len(authors) >= 2 else "single_source"
    return confidence, sources


def _passes_grounding_check(raw_item: _RawInsightItem, entries_by_id: dict[str, dict]) -> bool:
    """结论必须至少提到它引用的某个doc在ingest时就已经抽取好的一个真实entity，
    否则视为跟它自称的依据对不上，丢弃（不展示，只留日志）。"""
    for doc_id in raw_item.doc_ids:
        entry = entries_by_id.get(doc_id)
        if entry is None:
            continue
        for entity in entry.get("entities", []):
            if _entity_matches(entity, raw_item.summary):
                return True
    return False


def _merge_raw_results(batch_results: list[_RawTopicInsights]) -> _RawTopicInsights:
    """把分批跑出的多个 _RawTopicInsights 按 5 个类别 list 字段拼接合并成一个。
    不做跨批 summary 去重——schema/前端/路由三层都不去重，重复 summary 当前会被原样透传到 UI
    （见探索结论），且现有 _passes_grounding_check 已过滤掉没提到真实 entity 的结论，重复结论
    只要 grounding 通过仍是合法结论。若后续 UI 出现重复卡片再单独处理。"""
    merged = _RawTopicInsights()
    for category in _CATEGORY_KEYS:
        combined: list[_RawInsightItem] = []
        for result in batch_results:
            combined.extend(getattr(result, category))
        setattr(merged, category, combined)
    return merged


def generate_insights(topic: str) -> TopicInsights | None:
    """读取index/<topic>/entries.jsonl+chunks.jsonl，按字符预算分批跑轻量模型提炼共识，
    把多批结果合并后整份覆盖写入index/<topic>/insights.json。entry数<MIN_ENTRIES_FOR_INSIGHTS
    时跳过，返回None。

    2026-08-09改成分批喂入：此前单次全量拼 prompt（每条 entry 全部 chunk 文本拼接塞一个 prompt），
    素材量上来后（青甘 150 条）单次回复要输出 5 类共几十条 JSON 结论，输出 token 撑爆 provider
    默认 max_tokens 撞 openai.LengthFinishReasonError。改成分批：按 _BATCH_CHAR_BUDGET 贪心
    切批 → 顺序（非并发，避免 light key 池 429 雪崩）逐批 invoke → 合并 5 个 list 字段 →
    复用既有的 grounding 校验/sources 回填/写盘逻辑。产物 JSON 形状与改造前字段对字段一致。
    """
    index_dir = _topic_index_dir(topic)
    entries = _read_jsonl(index_dir / "entries.jsonl")
    if len(entries) < MIN_ENTRIES_FOR_INSIGHTS:
        print(f"[insights] {topic} 仅有 {len(entries)} 条素材，低于下限 {MIN_ENTRIES_FOR_INSIGHTS}，跳过生成")
        return None

    chunks = _read_jsonl(index_dir / "chunks.jsonl")
    chunks_by_doc: dict[str, list[dict]] = {}
    for chunk in chunks:
        chunks_by_doc.setdefault(chunk["doc_id"], []).append(chunk)
    entries_by_id = {e["doc_id"]: e for e in entries}

    # 按字符预算分批；单批总长不超过 _BATCH_CHAR_BUDGET 的会落在一批，单条 entry 超过预算的
    # 单独成批（不拆 entry，保 doc_id→文本原子性，见 _split_entries_into_batches 注释）。
    batches = _split_entries_into_batches(entries, chunks_by_doc)
    total_batches = len(batches)

    # 跨素材共识批次接近 20k 字符，不再属于短分类任务。真实 E2E 中 light 池的 6 个入口
    # 对同一批次全部 180 秒超时，因此这里走共享的 DeepSeek 结构化任务边界；
    # 短标题粗筛等小任务仍保留 light。
    # 惰性建一次 model（沿用 citation_check.py 惰性初始化写法，避免每批重建）。
    structured = bind_structured_json(
        get_structured_chat_model(temperature=0), _RawTopicInsights
    )

    # 顺序（非并行）逐批 invoke：insights 是 fire-and-forget/后台任务，不时延敏感；
    # 并行会让多个长批次同时占用 MiMo entry，放大限流和超时风险，串行更稳。
    batch_results: list[_RawTopicInsights] = []
    for i, batch in enumerate(batches, start=1):
        try:
            result = structured.invoke(
                _build_prompt(topic, batch, chunks_by_doc, batch_index=i, batch_total=total_batches)
            )
            batch_results.append(result)
            print(f"[insights] {topic} 第 {i}/{total_batches} 批完成（{len(batch)} 条素材）")
        except Exception as exc:  # noqa: BLE001 - 单批失败不能中断其余批，也不能让整份 insights 丢
            # 保持与 ingest hook 契约一致（generate_insights 失败只打日志、不向上抛、不影响入库）。
            print(f"[insights] {topic} 第 {i}/{total_batches} 批失败，跳过（不影响其余批）：{exc}")

    if not batch_results:
        # 所有批都失败：不抛——insights 缺失是正常情况（见 retrieve.py._load_consensus_summaries
        # 注释"软信号，insights.json 缺失是正常情况"）。写一份带空 list 的 TopicInsights 落盘，
        # 标记 based_on_entry_count，至少不让前端还展示旧的过期 insights.json。
        print(f"[insights] {topic} 所有 {total_batches} 批均失败，写空 insights.json 落盘")
        result = TopicInsights(
            topic=topic,
            generated_at=_now(),
            model=DEEPSEEK_CHAT_MODEL,
            based_on_entry_count=len(entries),
        )
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "insights.json").write_text(
            json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    merged_raw = _merge_raw_results(batch_results)

    result = TopicInsights(
        topic=topic,
        generated_at=_now(),
        model=DEEPSEEK_CHAT_MODEL,
        based_on_entry_count=len(entries),
    )

    dropped_count = 0
    for category in _CATEGORY_KEYS:
        kept_items: list[InsightItem] = []
        for raw_item in getattr(merged_raw, category):
            if not raw_item.doc_ids or not _passes_grounding_check(raw_item, entries_by_id):
                dropped_count += 1
                continue
            confidence, sources = _confidence_and_sources(raw_item, entries_by_id, chunks_by_doc)
            if not sources:
                dropped_count += 1
                continue
            kept_items.append(InsightItem(summary=raw_item.summary, confidence=confidence, sources=sources))
        setattr(result, category, kept_items)

    if dropped_count:
        print(f"[insights] {topic} 有 {dropped_count} 条候选未通过核验，已丢弃")

    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "insights.json").write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
