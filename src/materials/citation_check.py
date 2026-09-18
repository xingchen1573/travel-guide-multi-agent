"""对agent产出的每条内容做引用真实性核验，不依赖agent自证。

背景（详见 docs/ISSUES.md）：agent在同一次推理里既写内容、又给sources标chunk_id，
chunk_id本身也可能是编的，"标了来源"不代表内容真的来自那个来源。

两档设计（tier A的方案是校准后调整过的，不是最初设想的embedding相似度）：
- 校准发现：推荐文本和chunk原文的embedding余弦相似度，幻觉案例(0.39/0.42)反而比
  真实支持案例(0.33)更高——因为bge-m3捕捉的是"同一个大主题"而不是"具体事实吻合"，
  embedding相似度做不了这个判断，试过了，不可靠，别再用。
- 改用**字面实体核对**：agent写内容时额外报告具体专有名词（店名/菜名/景点名等），
  程序检查这些词是否字面出现在它引用的chunk原文里。这是免费的（不调用任何API），
  在校准案例上表现符合预期（真支持的案例逐字命中，幻觉案例完全不命中）。
- 档位A没有字面命中的，不直接判定为问题（可能只是同义改写），转入档位B——
  单独起一次LLM结构化调用做实质性核查，只对档位A筛出来的可疑项做，控制成本。

route_agent的POI核验和lifestyle_agent的建议核验共用同一套两档逻辑（_verify_items），
只是数据来源不同（route_plan.json的days/pois vs outputs/lifestyle.json的四个字段）。
"""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

from opencc import OpenCC
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.config import (
    MATERIAL_LIBRARY_DIR,
    PROJECTS_DIR,
    get_structured_chat_model as get_chat_model,
)
from src.structured_output import bind_structured_json
from src.materials.project_evidence import load_project_evidence_chunks
from src.materials.web_evidence import render_web_search_log_entry
from src.workspace import store
from src.workspace.final_guide_sections import REFERENCE_SECTION_HEADING


def _canonicalize_sources(sources: list[dict] | None) -> list[dict]:
    """Normalize legacy source records before verification or reference generation.

    Older projects can contain ``platform="web"`` and a descriptive/empty
    chunk id even though the record came from the runtime web-search log. The
    write-time normalizer cannot repair those already-persisted files, so all
    read-time consumers need the same deterministic compatibility step.
    """
    normalized: list[dict] = []
    for raw in sources or []:
        source = dict(raw)
        platform = str(source.get("platform") or "").strip().lower()
        doc_id = str(source.get("doc_id") or "")
        chunk_id = str(source.get("chunk_id") or "")
        source_url = str(source.get("source_url") or "")
        if not source_url and re.match(r"^https?://", doc_id):
            source_url = doc_id
            source["source_url"] = source_url
        synthetic_web = (
            platform in {"web", "web_verified"}
            or chunk_id == "web"
            or chunk_id.startswith("web-")
            or "-web-" in chunk_id
            or "web_verified" in chunk_id
            or doc_id == "web"
            or doc_id.startswith("web-")
            or "-web-" in doc_id
            or "web_verified" in doc_id
        )
        if synthetic_web:
            source["platform"] = "web_verified"
            if chunk_id in {"", "web", "web_verified"}:
                seed = (
                    source_url
                    or doc_id
                    or str(source.get("author") or "")
                    or json.dumps(source, ensure_ascii=False, sort_keys=True)
                )
                source["chunk_id"] = "web-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        normalized.append(source)
    return normalized


class CitationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _normalize_observed_verdict_shape(cls, value):
        if not isinstance(value, dict):
            return value

        fields = set(value)
        if fields == {"answer", "supported", "reason"}:
            answer = value.get("answer")
            supported = value.get("supported")
            reason = value.get("reason")
            if (
                isinstance(answer, str)
                and answer.strip() in {"", "判断"}
                and isinstance(supported, bool)
                and isinstance(reason, str)
                and reason.strip()
            ):
                return {
                    "supported": supported,
                    "reason": reason,
                }
        if fields == {"supported", "answer"}:
            return {
                "supported": value["supported"],
                "reason": value["answer"],
            }
        if fields == {"answer", "reason"}:
            answer = value.get("answer")
            if (
                isinstance(answer, str)
                and answer.strip() == "根据分析，该说法不被原文实质支持。"
            ):
                return {
                    "supported": False,
                    "reason": value["reason"],
                }
        return value

    supported: bool = Field(description="原文有没有真的支持推荐内容里的具体说法（不是主题相关就算支持，要有实质依据，可以考虑同义改写）")
    reason: str = Field(description="判断理由，一句话")


class AliasResolution(BaseModel):
    resolved: list[str] = Field(description="结合当前上下文能确认指向同一现实对象或同一被描述对象的输入标签；不要求完整字符串逐字出现")
    still_missing: list[str] = Field(description="原文没有对应对象，或只主题相关但实际指向不同对象的输入标签")
    reason: str = Field(description="简要说明判断依据，尤其是still_missing部分为什么原文没有支持")


def _effective_alias_missing(
    original_entities: list[str],
    resolution: AliasResolution,
) -> tuple[list[str], list[str]]:
    """Reconcile contradictory structured alias output deterministically.

    The model occasionally places the same canonical input entity in both
    ``resolved`` and ``still_missing``. ``resolved`` is the positive verdict;
    the effective missing set is therefore always the original input minus
    valid resolved entities. The second return value records overlaps for
    audit text without parsing the free-form reason.
    """
    original = list(dict.fromkeys(entity for entity in original_entities if entity))
    original_set = set(original)
    resolved = {
        entity
        for entity in (resolution.resolved or [])
        if entity in original_set
    }
    reported_missing = {
        entity
        for entity in (resolution.still_missing or [])
        if entity in original_set
    }
    overlaps = [entity for entity in original if entity in resolved and entity in reported_missing]
    effective_missing = [entity for entity in original if entity not in resolved]
    return effective_missing, overlaps


def _build_alias_resolution_prompt(
    missing_entities: list[str], combined_text: str
) -> str:
    """Build the identity-only alias prompt without duplicating content review."""
    return (
        f"下面这些输入标签没有在引用原文中逐字匹配：{missing_entities}。"
        "本步骤只做指代身份消歧，不判断价格、时间、顺序、玩法或其它事实是否正确；"
        "这些内容由后续实质核验负责。输入标签可能是规范名、简称、繁简体差异、ASR错字，"
        "也可能误把对象和属性、动作、顺序或时间拼成了复合标签。请结合当前POI和原文上下文，"
        "逐项判断原文是否明确指向同一现实对象、同一服务或同一被描述对象，不要求输入的"
        "完整字符串以连续字面形式出现。例如同一景区语境下的‘接驳车/观光车’、"
        "‘悬壁长城/悬臂长城’，或原文分别出现‘北门’和观光车顺序时的复合标签，"
        "可以判为resolved；但不同入口、不同景点、不同城市，或只有宽泛主题相关时仍须"
        "判为still_missing。不要因为某个时间、价格或顺序缺失而否定已经明确的对象身份。"
        "请以JSON格式回复，包含resolved(字符串数组)、still_missing(字符串数组)、"
        f"reason(字符串)三个字段。\n\n原文片段：{combined_text}"
    )


def load_chunk_texts(topic: str, project_id: Optional[str] = None) -> dict[str, str]:
    """加载某个主题下所有chunk的真实原文，chunk_id -> text。

    公开函数：不只是核验内部用，integration_agent_node也要用它把chunk原文喂给模型
    （见docs/ISSUES.md第8条——之前integration_agent只拿到chunk_id字符串，没见过原文，
    生成"参考来源"章节时只能靠自己编内容描述，这是真实复现过的幻觉根因，不是prompt约束不够）。
    """
    chunks_path = MATERIAL_LIBRARY_DIR / "index" / topic / "chunks.jsonl"
    result = {}
    if chunks_path.exists():
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                c = json.loads(line)
                result[c["chunk_id"]] = c["text"]
    if project_id:
        overlay_chunks = (
            PROJECTS_DIR
            / project_id
            / "materials"
            / "library"
            / "index"
            / topic
            / "chunks.jsonl"
        )
        if overlay_chunks.exists():
            for line in overlay_chunks.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    chunk = json.loads(line)
                    result[chunk["chunk_id"]] = chunk["text"]
        result.update(load_project_evidence_chunks(project_id, PROJECTS_DIR))
    return result


def _load_web_search_evidence(
    project_id: Optional[str], log_scope: Optional[str], claimed_entities: Optional[list[str]] = None
) -> str:
    """读取web_search工具调用时追加写入的agents/<log_scope>/web_search_log.jsonl，拼接成
    一段文本，供sources里platform=="web_verified"的条目核验使用（docs/ISSUES.md第115条
    方向C）。这类来源的chunk_id是agent自己现起的合成字符串，从未写入chunks.jsonl，
    load_chunk_texts()天然查不到——用这次生成过程中真实的web_search结果作为替代证据来源，
    核验强度不降级（复用_verify_items同一套档位A/B/C实体核对），只是证据来源换了。

    project_id/log_scope缺失（调用方没传，比如verify_poi_detail_citations的旧调用方式）或
    日志文件不存在（这次生成没调用过web_search，或log_scope对应的agent还没接入这套记录）时
    返回空字符串——调用方按"没有证据"处理，走正常档位A/B/C核验，大概率判定缺乏依据，
    这是正确行为，不是需要兜底的异常。

    claimed_entities（2026-08-14新增，见docs/ISSUES.md第121条）：可选，传入时只保留
    query命中其中至少一个实体名的日志记录，不是整份日志无差别拼接。真实撞到过一个案例：
    8天长行程场景route_draft单节点累计15次web_search、日志文件233KB，某天的sources里
    只要有1条web_verified来源，_merge_web_verified_evidence此前会把这233KB整份塞进那一天
    的combined_text，喂给alias_judge单次调用（62045 prompt token），撞上商汤relay
    reasoning截断（同第82/83/117条家族问题，但这次根因不是渠道本身而是prompt体积失控）。
    不传（默认None）时保留全部记录（向后兼容旧调用方式，测试用）。
    """
    if not project_id or not log_scope:
        return ""
    log_path = PROJECTS_DIR / project_id / "agents" / log_scope / "web_search_log.jsonl"
    if not log_path.exists():
        return ""
    texts = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if claimed_entities:
            query = entry.get("query", "")
            if not any(e and e in query for e in claimed_entities):
                continue
        rendered = render_web_search_log_entry(entry)
        if rendered:
            texts.append(rendered)
    return "\n".join(texts)


def _load_amap_logged_evidence(
    project_id: Optional[str],
    log_scope: Optional[str],
    evidence_ids: Optional[list[str]] = None,
) -> dict[str, str]:
    """Load project-scoped Amap calls by the evidence IDs returned to the agent."""
    if not project_id or not log_scope:
        return {}
    path = PROJECTS_DIR / project_id / "agents" / log_scope / "amap_evidence_log.jsonl"
    if not path.exists():
        return {}
    wanted = set(evidence_ids or [])
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        evidence_id = str(entry.get("evidence_id") or "")
        if not evidence_id or (wanted and evidence_id not in wanted):
            continue
        result[evidence_id] = (
            f"「{entry.get('poi_a', '')}」→「{entry.get('poi_b', '')}」约"
            f"{entry.get('distance_km')}公里/{entry.get('duration_h')}小时"
            f"（高德驾车实测，观察时间：{entry.get('observed_at', '')}）"
        )
    return result


def _merge_web_verified_evidence(
    items: list[dict],
    chunk_texts: dict[str, str],
    project_id: Optional[str],
    log_scope: Optional[str],
    filter_by_entities: bool = True,
) -> dict[str, str]:
    """给items里sources.platform=="web_verified"的chunk_id，在chunk_texts里补上这次生成
    过程中真实web_search结果作为它们的"原文"，让_verify_items()的combined_text拼接能拿到
    真实证据而不是空字符串。不修改_verify_items本身、不修改load_chunk_texts()返回的原字典
    （返回一份新dict）。items里临时的"_web_verified_ids"字段用完即弹出，不会泄漏进
    _verify_items看到的item结构。

    2026-08-14修复（见docs/ISSUES.md第121条）：filter_by_entities=True（默认）时按
    item的claimed_entities过滤web_search_log，不再是每个item不加区分地拿到同一份
    "全部记录拼接"的证据文本——route_agent/lifestyle_agent/pitfall_agent这3个调用点
    共用同一份跨越整个多天生成过程的日志（可能几十次搜索、几百KB），不过滤会导致
    某天只要有1条web_verified来源，就把全部记录都塞进那一天的combined_text，真实
    撞过233KB日志喂进单次LLM调用（62045 prompt token）触发商汤relay reasoning截断。

    poi_detail_agent这一个调用点传filter_by_entities=False——它的log_scope本身已经
    按"天+POI"精确到单个并行分支（`f"poi_detail_agent/day{day}_{poi_slug}"`），日志
    天然就窄（只有这一个POI的搜索记录），不需要再叠加过滤；而且这个场景的
    claimed_entities是途经点名（如"检票入园"），跟web_search的query用词
    （POI大名+关键词）不是同一个粒度，按子串匹配反而会误伤真实相关的证据。

    2026-08-17补充（docs/ISSUES.md第130条）：不再pop掉item的"_web_verified_ids"——
    改成保留在item上（不影响_verify_items读取其它字段，未知字段会被忽略），供
    _verify_items组装combined_text时识别哪些chunk_id是"合成证据"、需要优先摆放+
    加显著标记（见该函数说明）。同时给合并进来的证据文本包一层醒目前缀，即使这段
    证据在combined_text里被排在很靠前的位置，也能让LLM一眼看出"这是系统认证的
    真实数据，不是普通素材片段"，不容易被前后夹杂的大段其它文本干扰判断。
    """
    merged = dict(chunk_texts)
    for item in items:
        web_verified_ids = item.get("_web_verified_ids", [])
        if not web_verified_ids:
            continue
        entities = item.get("claimed_entities") if filter_by_entities else None
        web_evidence = _load_web_search_evidence(project_id, log_scope, claimed_entities=entities)
        if not web_evidence:
            continue
        for cid in web_verified_ids:
            merged[cid] = f"【系统记录·本次生成过程中web_search真实核实结果】\n{web_evidence}"
    return merged


def _load_amap_evidence(topic: str, content: str) -> str:
    """扫data/material_library/driving_cache.json，把content文本里同时出现的两个地名
    对应的缓存记录拼成"「A」→「B」约X公里/Y小时（高德驾车实测缓存）"证据文本，供sources里
    platform=="amap"的条目核验使用（docs/ISSUES.md第129条）。

    跟_load_web_search_evidence角色一致（都是"合成chunk_id没有真实原文，找一份运行时
    产生的真实数据代替"），但匹配方式不同：amap_driving_distance这个工具本身不写调用日志
    （不像web_search有web_search_log.jsonl），只有跨project共享的driving_cache.json
    这一份全局缓存（src/tools/driving.py），key是f"{topic}|{poi_a}|{poi_b}"（无序对）。
    没有"这次生成过程专属"的调用记录可用，只能反过来用"content文本里是否同时提到这两个
    地名"做匹配——足够用了，因为要核验的正是"content里写的这个距离数字有没有依据"。

    真实调查发现的坑：driving_cache.json同一对概念地点常有"粗/细"两种粒度的key同时存在
    （如"青海湖"vs"青海湖二郎剑景区"，后者是geocode消歧后的精确写法），两者都可能是content
    里出现的子串——如果不做取舍，粗粒度key有时会命中geocode歧义产生的离谱值（真实撞到过
    "茶卡盐湖→青海湖"缓存里存的是1752km/20.6小时，是两个同名异地地点的误配对，
    跟本条内容真正对应的"茶卡盐湖→青海湖二郎剑景区"143.8km完全不是一回事）。这类离谱值
    本身是driving_cache的已知局限（geocode歧义，非本函数职责修复），这里只做"能不选就不选
    粗粒度key"这一层过滤：两个候选pair如果地点名存在包含关系（一个是另一个的子串），
    只保留更长/更具体的那个，避免把明显更粗糙、更可能对应错误地点的候选也当证据塞进去。
    """
    cache_path = MATERIAL_LIBRARY_DIR / "driving_cache.json"
    if not cache_path.exists() or not content:
        return ""
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    prefix = f"{topic}|"
    candidates = []
    for key, val in cache.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].split("|")
        if len(parts) != 2:
            continue
        poi_a, poi_b = parts
        if poi_a and poi_b and poi_a in content and poi_b in content:
            candidates.append((poi_a, poi_b, val))

    # 地名子串匹配本身可能凑巧命中一个跟内容实际数字对不上的候选（上面提到的粗粒度/歧义
    # 问题就是一种表现），额外要求候选的公里数跟content里出现的某个"数字+公里/km"在
    # 容差范围内一致，才真正采信——content完全没写公里数字时（理论上不该发生，但兜底）
    # 不做这层过滤，退回纯地名匹配。这一步比子串包含关系更直接：不管名字匹配得多"精确"，
    # 数字对不上就说明这条缓存记录描述的不是content里在说的这趟路程。
    mentioned_km = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*(?:km|公里)", content)]
    if mentioned_km:
        candidates = [
            c for c in candidates
            if any(abs(c[2].get("distance_m", 0) / 1000 - km) <= max(5, km * 0.15) for km in mentioned_km)
        ]

    def _is_coarser(c1: tuple, c2: tuple) -> bool:
        """c1是否是比c2更粗糙的同一对地点（每个名字都被c2对应名字包含、且至少一处更短）。"""
        a1, b1, _ = c1
        a2, b2, _ = c2
        pairs = [(a1, a2, b1, b2), (a1, b2, b1, a2)]  # 两种配对顺序都要试（无序对）
        for x1, x2, y1, y2 in pairs:
            if x1 in x2 and y1 in y2 and (len(x1) < len(x2) or len(y1) < len(y2)):
                return True
        return False

    kept = [c for c in candidates if not any(_is_coarser(c, other) for other in candidates if other != c)]

    lines = []
    for poi_a, poi_b, val in kept:
        distance_km = round(val.get("distance_m", 0) / 1000)
        duration_h = round(val.get("duration_s", 0) / 3600, 1)
        lines.append(f"「{poi_a}」→「{poi_b}」约{distance_km}公里/{duration_h}小时（高德驾车实测缓存）")
    return "\n".join(lines)


def _merge_amap_evidence(items: list[dict], chunk_texts: dict[str, str], topic: str) -> dict[str, str]:
    """给items里sources.platform=="amap"的chunk_id补上driving_cache.json里匹配到的真实
    距离/时长文本作为"原文"，跟_merge_web_verified_evidence角色对称（docs/ISSUES.md第129条：
    此前amap来源完全没有被合并进combined_text，_resolve_chunk_text对这类合成chunk_id
    永远返回空字符串，导致任何引用了高德实测数据的具体数字必然核验失败——真实复现：
    青甘大环线8天E2E里`play.day1`/`play.day8`等条目的距离/时长数字明明是高德实测的真实
    数据，仍被判定"原文未支持"）。

    同_merge_web_verified_evidence（2026-08-17补充，docs/ISSUES.md第130条）：不再pop掉
    "_amap_ids"，且证据文本加醒目前缀——理由见该函数的同款说明。
    """
    merged = dict(chunk_texts)
    for item in items:
        amap_ids = item.get("_amap_ids", [])
        if not amap_ids:
            continue
        evidence = _load_amap_evidence(topic, item.get("content", ""))
        if not evidence:
            continue
        for cid in amap_ids:
            merged[cid] = f"【系统记录·高德驾车实测数据】\n{evidence}"
    return merged



def collect_cited_chunks(
    route_plan: dict | None,
    lifestyle: dict | None,
    pitfall: dict | None = None,
    poi_details: list[dict] | None = None,
) -> dict[str, dict]:
    """从route_plan.json、outputs/lifestyle.json、outputs/pitfall.json、
    outputs/poi_details/*.json里收集所有被引用的chunk_id及其source信息，按首次出现顺序
    去重。返回 chunk_id -> {platform, author, doc_id, source_url, scope, cited_in}。

    ``cited_in``记录这条来源在哪些产物对象的sources字段中出现。它只是“声明使用位置”
    的反向索引，不代表该来源支持对象中的每一句话；逐事实支持关系由claims/evidence checks
    负责。引用表此前只保留chunk第一次出现的元数据，后续重复引用位置会被去重丢掉，导致
    用户只能看到“有哪些来源”，看不到“这些来源用在哪里”。

    纯函数，不读文件、不调用LLM，方便单测；供build_reference_section和
    integration_agent_node的grounding context构造共用。pitfall/poi_details都是可选参数
    （默认None，向后兼容）。poi_details收集范围此前完全缺失——它的sources嵌在
    routes[].route_segments[].sources，跟route_plan/lifestyle/pitfall的
    items[].sources不是同一层级，之前漏了这一整层，导致"参考来源"表格系统性少列
    POI详情层的引用，见docs/ISSUES.md相关记录。
    """
    def source_metadata(source: dict) -> dict:
        metadata = {
            "platform": source.get("platform", ""),
            "author": source.get("author", ""),
            "doc_id": source.get("doc_id", ""),
            "cited_in": [],
        }
        if source.get("source_url"):
            metadata["source_url"] = source["source_url"]
        if source.get("scope"):
            metadata["scope"] = source["scope"]
        return metadata

    def record_source(source: dict, usage_key: str) -> None:
        cid = source.get("chunk_id")
        if not cid:
            return
        if cid not in seen:
            seen[cid] = source_metadata(source)
        cited_in = seen[cid]["cited_in"]
        if usage_key and usage_key not in cited_in:
            cited_in.append(usage_key)

    seen: dict[str, dict] = {}
    for day in (route_plan or {}).get("days", []) or []:
        for s in _canonicalize_sources(day.get("sources")):
            record_source(s, f"route:day:{day.get('day')}")

    for field in ["food", "outfit", "lodging", "play"]:
        for item_index, item in enumerate((lifestyle or {}).get(field, {}).get("items", []) or []):
            for s in _canonicalize_sources(item.get("sources")):
                record_source(
                    s,
                    f"lifestyle:{field}:day:{item.get('day')}:item:{item_index}",
                )

    for field in ["food", "outfit", "lodging", "play"]:
        for item_index, item in enumerate((pitfall or {}).get(field, {}).get("items", []) or []):
            for s in _canonicalize_sources(item.get("sources")):
                record_source(
                    s,
                    f"pitfall:{field}:day:{item.get('day')}:item:{item_index}",
                )

    for detail in poi_details or []:
        for route in detail.get("routes", []) or []:
            for segment_index, segment in enumerate(route.get("route_segments", []) or []):
                for s in _canonicalize_sources(segment.get("sources")):
                    order = segment.get("order")
                    if order is None:
                        order = segment_index + 1
                    record_source(
                        s,
                        f"poi_detail:day:{detail.get('day')}:poi:{detail.get('poi_name', '')}:"
                        f"route:{route.get('route_name', '')}:order:{order}",
                    )

    return seen


_REFERENCE_FIELD_LABELS = {
    "food": "美食",
    "outfit": "穿搭",
    "lodging": "住宿",
    "play": "游玩",
}


def _citation_usage_label(usage_key: str) -> str:
    """Render a compact human label for a canonical cited_in key."""
    parts = str(usage_key or "").split(":")
    if len(parts) >= 3 and parts[0] == "route" and parts[1] == "day":
        return f"Day{parts[2]}路线"
    if len(parts) >= 6 and parts[0] in {"lifestyle", "pitfall"}:
        field = _REFERENCE_FIELD_LABELS.get(parts[1], parts[1])
        suffix = "避雷" if parts[0] == "pitfall" else ""
        return f"Day{parts[3]}{field}{suffix}"
    if len(parts) >= 9 and parts[0] == "poi_detail":
        return f"Day{parts[2]} {parts[4]}·{parts[6]}途经点{parts[8]}"
    return usage_key


def _reference_usage_summary(cited_in: list[str] | None) -> str:
    usages = list(dict.fromkeys(cited_in or []))
    if not usages:
        return ""
    labels = [_citation_usage_label(item) for item in usages[:3]]
    summary = "、".join(labels)
    if len(usages) > 3:
        summary += f"，另{len(usages) - 3}处"
    return f"{len(usages)}处：{summary}"


def build_reference_section(cited_chunks: dict[str, dict], chunk_texts: dict[str, str]) -> tuple[str, list[dict]]:
    """程序生成"参考来源"章节的markdown文本 + final_guide.meta.json的references列表，
    不经过模型撰写。返回 (markdown_str, references_list)。

    这是确定性兜底防线（见docs/ISSUES.md第8条）：真实chunk原文由上游引用核验节点使用，
    不再注入Integration Agent重复审查。"参考来源"是"把已知结构化数据列出来"的机械任务，
    直接用程序生成，
    不给模型编造的空间——业内对RAG幻觉的建议里，"mechanical verification, not trusting
    model outputs"是明确提到的做法。
    """
    lines = [REFERENCE_SECTION_HEADING, ""]
    references: list[dict] = []

    if not cited_chunks:
        lines.append("本次生成未记录到具体chunk级引用。")
        return "\n".join(lines), references

    lines.append("| 范围 | 平台 | doc_id | chunk_id | 来源链接 | 声明使用位置 | 原文片段 |")
    lines.append("|---|---|---|---|---|---|---|")
    for cid, s in cited_chunks.items():
        text = chunk_texts.get(cid)
        platform = s.get("platform", "")
        if text:
            snippet = text[:60].replace("\n", " ") + "..."
        elif platform == "web_verified":
            snippet = "🌐 系统运行时联网核实所得，非素材库固定chunk，详见生成日志"
        elif platform == "amap":
            snippet = "📍 高德地图API实测所得，非素材库固定chunk"
        else:
            snippet = "⚠️原文未找到，此引用可能有误"
        source_url = s.get("source_url", "")
        source_link = f"[原始来源]({source_url})" if source_url else ""
        cited_in = list(dict.fromkeys(s.get("cited_in") or []))
        usage_summary = _reference_usage_summary(cited_in)
        lines.append(
            f"| {s.get('scope', 'global_core')} | {platform} | {s.get('doc_id', '')} | "
            f"{cid} | {source_link} | {usage_summary} | {snippet} |"
        )
        reference = {
            "platform": s.get("platform", ""),
            "author": s.get("author", ""),
            "doc_id": s.get("doc_id", ""),
            "chunk_id": cid,
            "cited_in": cited_in,
            "excerpt": snippet,
        }
        if source_url:
            reference["source_url"] = source_url
        if s.get("scope"):
            reference["scope"] = s["scope"]
        references.append(reference)

    return "\n".join(lines), references


# 常见的中文地标通用后缀词——route_agent倾向于写景点的规范全称（"武侯祠博物馆"），
# 但UGC素材原文常用口语简称（"武侯祠"），字面核对时把这类后缀当作可选后缀剥离，
# 不算真正的"缺乏依据"。这是一个封闭的小词表，只覆盖确实通用的后缀，不做开放式模糊匹配
# ——遇到词表外的别名（比如缩写"IFS"vs全称"成都国际金融中心"）时，仍然会字面核对失败，
# 转入档位B，但档位B（entity_identity_only=True时）问的是"是不是同一个地点"这个更窄的
# 问题，不会像之前那样把这类别名连带判定为"内容缺乏依据"。见docs/ISSUES.md。
_LANDMARK_SUFFIXES = (
    "博物馆", "纪念馆", "美术馆", "图书馆",
    "步行街", "古街", "老街", "商业街",
    "公园", "广场", "景区", "古镇", "风景区", "风景名胜区",
)

_TRADITIONAL_TO_SIMPLIFIED = OpenCC("t2s")


def _simplified_for_entity_match(value: str) -> str:
    """Build a comparison-only simplified view without rewriting evidence."""
    return _TRADITIONAL_TO_SIMPLIFIED.convert(value)


def _entity_matches(entity: str, text: str) -> bool:
    """字面核对，允许剥离_LANDMARK_SUFFIXES里的常见地标后缀后再匹配一次
    （"武侯祠博物馆"应该能匹配到原文里只写了"武侯祠"的情况）。
    """
    if entity in text:
        return True
    comparison_entity = _simplified_for_entity_match(entity)
    comparison_text = _simplified_for_entity_match(text)
    if comparison_entity in comparison_text:
        return True
    for suffix in _LANDMARK_SUFFIXES:
        if comparison_entity.endswith(suffix):
            core = comparison_entity[: -len(suffix)]
            if len(core) >= 2 and core in comparison_text:
                return True
    return False


# prompt第10/11条建议模型在检索结果不够时用的诚实措辞——不追求穷举所有可能的
# 诚实表述方式，只堵住"完全没有citation、也没有任何诚实信号"这种最坏情况
# （见docs/ISSUES.md：真实复现过agent整批产出留空claimed_entities/sources，
# 核验因此被完全短路却报0冲突）。
_DISCLAIMER_MARKERS = (
    "根据一般经验推荐", "根据一般经验", "凭一般经验", "凭经验推荐",
    "未提供具体信息", "未提供具体", "素材未提供", "检索结果没有提供",
    "检索结果未提供", "没有具体依据", "无直接素材依据",
    # POI详情agent真实E2E里观察到的诚实措辞变体（见docs/ISSUES.md）——POI途经点常写
    # "素材库未提供该建筑的具体细节描述，此处为...一般介绍"这类声明，语义上和上面几条等价，
    # 都是"老实承认这段是凭常规认知补的、没有逐字素材依据"，补进白名单避免把诚实声明误报成问题。
    "素材库未提供", "素材库未直接", "常规游览路线的一般介绍", "常规参观点",
)

# 2026-07-28真实复现（洱海骑行评测，素材库完全空白场景）：同一次评测里route_agent/
# lifestyle_agent连续三次被打回，每次写的诚实声明措辞都不一样（"素材库中未检索到相关
# 支撑素材"/"未检索到任何支持性素材"/"素材库中（含精确检索N个xx相关名词）均无相关
# 可用素材"）——逐个把新措辞加进上面的精确短语列表，跟"追新词条"打地鼠一样，模型每次
# 换个说法就又漏一次，已经在这一次评测里连续踩了3次。改用一个宽松正则兜底，匹配"素材/
# 检索(结果/库)+没有/未/无/均无+可用/相关/支撑/支持性+素材/依据"这一整类语义组合，
# 以及"凭/根据+自身/一般/个人+常识/经验"这类表述，一次性覆盖这一大类诚实声明的表达
# 变体，不再依赖精确字符串匹配。跟_DISCLAIMER_MARKERS任一命中即可，两者不互斥。
# 2026-07-29真实复现（峨眉山评测，同样是素材库完全空白场景）：route_agent写的是
# "素材库中无直接支撑原文，故sources留空"——"支撑"后面紧跟的是"原文"而不是词表里的
# "素材/依据/信息/chunk"，命中失败，连续3次被误判打回耗尽force_finalize。这是同一个
# "打地鼠追不上模型措辞变化"问题的第4个变体，补"原文"进最后一个分支的词表。
_DISCLAIMER_PATTERN = re.compile(
    r"(检索结果|检索|素材库|素材)[^。\n]{0,20}(没有|未|无|均无)[^。\n]{0,15}"
    r"(相关|可用|支撑|支持性|直接)?[^。\n]{0,5}(素材|依据|信息|chunk|原文)"
    r"|(检索结果|检索|素材库|素材)[^。\n]{0,10}(没有|未|无)(给出|提供|涉及|提到|明确|说明)"
    r"|凭(自身|一般|个人)?(常识|经验)"
    r"|根据(一般|自身)?(常识|经验)"
)


def _has_honesty_disclaimer(content: str) -> bool:
    return any(marker in content for marker in _DISCLAIMER_MARKERS) or bool(
        _DISCLAIMER_PATTERN.search(content)
    )


# entity触发式核验架构的已知结构性缺口的部分缓解（CLAUDE.md已知问题第4条/
# docs/ISSUES.md第20/35条）：claimed_entities全部字面核对通过时，_verify_items
# 此前会直接continue，完全不检查内容里挂靠在真实地名/店名上的其它具体事实
# （建议停留时长、地铁线路号、换乘站名、价格等）是不是编的——这类"真地名+编造
# 事实细节"是目前观察到危害最大的一类幻觉模式。这里不追求穷尽识别所有可验证
# 事实的表达方式（同_DISCLAIMER_MARKERS的取舍一样），只用一个粗粒度的"数字+
# 单位"正则识别最常见、最容易被验证对错的几类：时长、价格、距离、地铁线路号、
# 具体时刻。命中时会多触发一次档位C的LLM核查（见下方_verify_items），不是
# 免费的，但只对含这类模式的内容触发，不是对所有条目全量核查。
_VERIFIABLE_FACT_PATTERN = re.compile(
    r"\d+\s*(分钟|小时|元|块钱|米|公里|号线|站)|\d{1,2}:\d{2}"
)

# chunk_id真实格式是f"{topic}-{i:03d}-c{order:03d}"（见src/materials/ingest.py），
# 裸格式（丢失topic前缀）就是这个末尾的"NNN-cNNN"部分。这是防御性兜底（docs/ISSUES.md
# 第116条）——write_agent_output等写入工具已经在写入这一刻主动补全前缀了（治本），这里
# 是万一某条历史数据/未覆盖到的写入路径仍然带着裸格式流入核验时的最后一道防线（治标）。
_BARE_CHUNK_ID_SUFFIX = re.compile(r"^\d{3}-c\d{3}$")
_CHUNK_ID_SUFFIX = re.compile(r"(\d{3}-c\d{3})$")


def _build_bare_chunk_index(chunk_texts: dict[str, str]) -> dict[str, str]:
    """chunk_texts的key都是完整chunk_id（带topic前缀）。构建"裸格式后缀 -> 完整chunk_id"
    的反查索引，供_resolve_chunk_text在原样查找失败时兜底。同一topic下"NNN-cNNN"后缀
    在不同文档间不会重复（NNN是文档序号，各不相同），不存在歧义。"""
    index: dict[str, str] = {}
    for full_id in chunk_texts:
        m = _CHUNK_ID_SUFFIX.search(full_id)
        if m:
            index.setdefault(m.group(1), full_id)
    return index


def _resolve_chunk_text(cid: str, chunk_texts: dict[str, str], bare_index: dict[str, str]) -> str:
    """先按原样查找；查不到、且cid本身长得像丢了topic前缀的裸格式时，用后缀反查索引兜底。"""
    text = chunk_texts.get(cid)
    if text is not None:
        return text
    if _BARE_CHUNK_ID_SUFFIX.match(cid):
        full_id = bare_index.get(cid)
        if full_id:
            return chunk_texts.get(full_id, "")
    return ""


_CHUNK_DOC_SUFFIX = re.compile(r"-c\d{3}$")


def _doc_id_of(chunk_id: str) -> str:
    """从完整chunk_id剥掉末尾"-cNNN"部分得到doc_id（见ingest.py的chunk_id命名格式）。
    web_verified/amap这类合成chunk_id没有这个后缀，原样返回（各自独立，不会跟真实
    文档的doc_id混同）。"""
    return _CHUNK_DOC_SUFFIX.sub("", chunk_id)


def _build_doc_chunk_index(chunk_texts: dict[str, str]) -> dict[str, list[str]]:
    """doc_id -> 属于同一篇文档的全部chunk_id列表（见docs/ISSUES.md第127条）。

    根因：部分素材（"多图截图合集"类博主笔记）被切分时，一篇文章的内容会分散在
    相邻的多个chunk里（真实案例：同一篇笔记的Day3/4/5三天行程被压缩进同一个chunk，
    紧邻的下一个chunk又是Day6）。模型标注sources时容易在这组相邻chunk里"选错了
    具体是哪一个"——不是编造，是引用配对精度问题。真实核实过44个"缺失实体"，
    75%属于这种情况：实体真实存在，只是引用的chunk_id指向了同文档的相邻段落。"""
    index: dict[str, list[str]] = {}
    for cid in chunk_texts:
        index.setdefault(_doc_id_of(cid), []).append(cid)
    return index


def _ordered_chunk_ids(item: dict, chunk_ids: list[str]) -> list[str]:
    """把item的chunk_ids重排序：系统合成证据（经_merge_web_verified_evidence/
    _merge_amap_evidence注入了醒目前缀）排在最前面，其余真实素材chunk_id保持原有
    相对顺序排在后面（docs/ISSUES.md第130条）。

    背景：combined_text此前按chunk_ids原始顺序简单拼接，真实撞到过合成证据被排在
    后面、且前面跟着一段远超它长度的其它证据（15KB的web_verified博物馆核实内容 vs
    32字符的amap距离数据）——档位C的LLM判断没能从这段长文本里注意到那32个字符，
    误判"缺乏支持"。合成证据本身已经是系统核实过的高置信度数据，排在最前面能让
    LLM优先看到，配合前缀标记双重提升可见性，不改变证据内容本身、只调整摆放顺序。

    amap_ids排在web_verified_ids之前（不是两者不分先后）：amap证据固定是"「A」→「B」
    约X公里/Y小时"这种几十字符的精炼陈述，web_verified证据是整份web_search_log过滤后
    拼接的结果，可能仍有数千到上万字符（真实撞到过15KB）——两者都排最前面的话，
    amap排在web_verified后面依然会被这堆长文本挤到"很靠前但不是最前面"的位置，
    仍有被忽略的风险，索性让更短小精确的amap证据排在最开头。
    """
    amap_ids = list(item.get("_amap_ids") or [])
    web_verified_ids = [cid for cid in (item.get("_web_verified_ids") or []) if cid not in amap_ids]
    priority_ids = set(amap_ids) | set(web_verified_ids)
    if not priority_ids:
        return chunk_ids
    ordered_priority = [cid for cid in amap_ids if cid in chunk_ids] + [
        cid for cid in web_verified_ids if cid in chunk_ids
    ]
    return ordered_priority + [cid for cid in chunk_ids if cid not in priority_ids]


def _evidence_excerpt(text: str, terms: list[str], limit: int = 900) -> str:
    """Return a deterministic, target-centered excerpt for the admin explanation UI."""
    normalized = (text or "").strip()
    if len(normalized) <= limit:
        return normalized

    positions = [normalized.find(term) for term in terms if term and normalized.find(term) >= 0]
    if positions:
        center = min(positions)
        start = max(0, center - limit // 3)
    else:
        start = 0
    end = min(len(normalized), start + limit)
    excerpt = normalized[start:end]
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(normalized):
        excerpt += "…"
    return excerpt


def _append_check_result(
    result_sink: Optional[list[dict]],
    item: dict,
    *,
    scope: str,
    result: str,
    method: str,
    reason: str,
    checked_chunk_ids: list[str],
    chunk_texts: dict[str, str],
    bare_chunk_index: dict[str, str],
    supplemental_chunk_ids: Optional[list[str]] = None,
    missing_entities: Optional[list[str]] = None,
) -> None:
    if result_sink is None:
        return

    supplemental = set(supplemental_chunk_ids or [])
    source_by_id = {
        str(source.get("chunk_id") or ""): source
        for source in item.get("sources") or []
        if source.get("chunk_id")
    }
    evidence = []
    seen: set[str] = set()
    terms = list(item.get("claimed_entities") or [])
    for chunk_id in checked_chunk_ids:
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        source = source_by_id.get(chunk_id, {})
        text = _resolve_chunk_text(chunk_id, chunk_texts, bare_chunk_index)
        evidence.append(
            {
                "chunk_id": chunk_id,
                "role": "checker_supplement" if chunk_id in supplemental else "cited",
                "platform": source.get("platform", ""),
                "author": source.get("author", ""),
                "doc_id": source.get("doc_id", _doc_id_of(chunk_id)),
                "source_url": source.get("source_url", ""),
                "excerpt": _evidence_excerpt(text, terms),
            }
        )

    check = {
        "target_type": item.get("target_type", "content_item"),
        "target_key": item.get("target_key", item.get("label", "")),
        "target_text": item.get("content", ""),
        "scope": scope,
        "result": result,
        "method": method,
        "reason": reason,
        "cited_sources": item.get("sources") or [],
        "cited_chunk_ids": item.get("chunk_ids") or [],
        "checked_chunk_ids": list(dict.fromkeys(checked_chunk_ids)),
        "evidence": evidence,
        "missing_entities": missing_entities or [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "observation_mode": item.get("observation_mode", "live"),
        **item.get("extra", {}),
    }
    verification_fingerprint = str(item.get("_verification_fingerprint") or "")
    if verification_fingerprint:
        check["verification_fingerprint"] = verification_fingerprint
    result_sink.append(check)


def _verification_fingerprint(item: dict, chunk_texts: dict[str, str]) -> str:
    """Hash every input that can change a claim verification decision."""
    bare_chunk_index = _build_bare_chunk_index(chunk_texts)
    ordered_chunk_ids = _ordered_chunk_ids(item, list(item.get("chunk_ids") or []))
    payload = {
        "target_type": item.get("target_type", "content_item"),
        "target_key": item.get("target_key", item.get("label", "")),
        "content": item.get("content", ""),
        "claimed_entities": list(item.get("claimed_entities") or []),
        "sources": list(item.get("sources") or []),
        "chunk_ids": ordered_chunk_ids,
        "chunk_sha256": [
            {
                "chunk_id": chunk_id,
                "sha256": hashlib.sha256(
                    _resolve_chunk_text(chunk_id, chunk_texts, bare_chunk_index).encode("utf-8")
                ).hexdigest(),
            }
            for chunk_id in ordered_chunk_ids
        ],
        "web_verified_ids": list(item.get("_web_verified_ids") or []),
        "amap_ids": list(item.get("_amap_ids") or []),
        "entity_identity_only": bool(item.get("_entity_identity_only")),
        "force_substantive_check": bool(item.get("_force_substantive_check")),
        "disable_sibling_expansion": bool(item.get("_disable_sibling_expansion")),
        "support_mode": item.get("extra", {}).get("support_mode"),
        "target_path": item.get("extra", {}).get("target_path"),
        "derived_from": list(item.get("extra", {}).get("derived_from") or []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_STRICT_EVIDENCE_SUPPORT_MODES = {"direct", "amap"}
_BLOCKING_CLAIM_CHECK_RESULTS = {"fail", "error", "inconclusive"}


def _uses_strict_evidence_conjunction(item: dict) -> bool:
    return str(item.get("extra", {}).get("support_mode") or "") in _STRICT_EVIDENCE_SUPPORT_MODES


def _uses_asr_source(item: dict) -> bool:
    return any(
        str(source.get("platform") or "").strip().lower().endswith("_asr")
        for source in item.get("sources") or []
        if isinstance(source, dict)
    )


def _has_non_asr_entity_name_evidence(
    item: dict,
    entity: str,
    chunk_texts: dict[str, str],
    bare_chunk_index: dict[str, str],
) -> bool:
    """Require the canonical name in an actually cited non-ASR evidence body."""
    for source in item.get("sources") or []:
        if not isinstance(source, dict):
            continue
        platform = str(source.get("platform") or "").strip().lower()
        if not platform or platform.endswith("_asr"):
            continue
        chunk_id = str(source.get("chunk_id") or "").strip()
        if not chunk_id:
            continue
        evidence_text = _resolve_chunk_text(chunk_id, chunk_texts, bare_chunk_index)
        if evidence_text and _entity_matches(entity, evidence_text):
            return True
    return False


def _strict_claim_issue(item: dict, checks: list[dict]) -> Optional[dict]:
    """Build one aggregate issue when a direct/Amap claim fails any evidence gate."""
    if not _uses_strict_evidence_conjunction(item):
        return None

    target_key = str(item.get("target_key") or "")
    latest_by_scope: dict[str, dict] = {}
    for check in checks:
        if str(check.get("target_key") or "") == target_key:
            latest_by_scope[str(check.get("scope") or "")] = check

    failures: list[str] = []
    source_check = latest_by_scope.get("source_availability")
    entity_check = latest_by_scope.get("entity_identity")
    substantive_check = latest_by_scope.get("substantive_claim")
    if source_check and source_check.get("result") in _BLOCKING_CLAIM_CHECK_RESULTS:
        failures.append(f"证据可用性未通过：{source_check.get('reason', '')}")
    if entity_check and entity_check.get("result") in _BLOCKING_CLAIM_CHECK_RESULTS:
        failures.append(f"实体归属未通过：{entity_check.get('reason', '')}")
    if (
        not item.get("_entity_identity_only")
        and substantive_check
        and substantive_check.get("result") in _BLOCKING_CLAIM_CHECK_RESULTS
    ):
        failures.append(f"正文实质支持未通过：{substantive_check.get('reason', '')}")
    if not failures:
        return None

    if (
        entity_check
        and entity_check.get("result") in _BLOCKING_CLAIM_CHECK_RESULTS
        and substantive_check
        and substantive_check.get("result") == "pass"
    ):
        failures.append("正文文字相似或内容得到支持，不能覆盖实体归属失败。")

    missing_entities = (
        (entity_check or {}).get("missing_entities")
        or (substantive_check or {}).get("missing_entities")
        or []
    )
    return {
        "label": item.get("label", ""),
        "chunk_ids": item.get("chunk_ids") or [],
        "missing_entities": missing_entities,
        "reason": "direct/amap Claim未同时满足交付条件：" + "；".join(failures),
        **item.get("extra", {}),
    }


def _merge_strict_claim_issues(
    claim_items: list[dict],
    checks: list[dict],
    issues: list[dict],
) -> None:
    """Replace per-scope issues with one traceable conjunction failure per claim."""
    for item in claim_items:
        aggregate = _strict_claim_issue(item, checks)
        if aggregate is None:
            continue
        claim_id = str(item.get("extra", {}).get("claim_id") or "")
        matching_indexes = [
            index
            for index, issue in enumerate(issues)
            if (claim_id and str(issue.get("claim_id") or "") == claim_id)
            or (not claim_id and issue.get("label") == item.get("label"))
        ]
        if matching_indexes:
            issues[matching_indexes[0]] = aggregate
            for index in reversed(matching_indexes[1:]):
                issues.pop(index)
        else:
            issues.append(aggregate)


def _reusable_completed_claim_checks(
    existing_checks: list[dict],
    claim_items: list[dict],
) -> tuple[list[dict], set[str], list[dict]]:
    """Return complete exact-input claim verdicts from an interrupted run.

    A completed business failure is as final as a pass. Re-running it after a
    transport interruption wastes a model call and can introduce verdict
    drift. Technical errors, inconclusive results and incomplete scope groups
    are deliberately excluded.
    """
    by_target: dict[str, list[dict]] = {}
    for check in existing_checks:
        target_key = str(check.get("target_key") or "")
        if target_key:
            by_target.setdefault(target_key, []).append(check)

    reused: list[dict] = []
    reused_target_keys: set[str] = set()
    reused_issues: list[dict] = []
    for item in claim_items:
        target_key = str(item.get("target_key") or "")
        fingerprint = str(item.get("_verification_fingerprint") or "")
        candidates = by_target.get(target_key, [])
        if not fingerprint or not candidates:
            continue
        if any(check.get("verification_fingerprint") != fingerprint for check in candidates):
            continue

        latest_by_scope: dict[str, dict] = {}
        for check in sorted(candidates, key=lambda value: str(value.get("checked_at") or "")):
            latest_by_scope[str(check.get("scope") or "")] = check
        source_check = latest_by_scope.get("source_availability")
        entity_check = latest_by_scope.get("entity_identity")
        substantive_check = latest_by_scope.get("substantive_claim")
        if not source_check or not entity_check or not substantive_check:
            continue
        if source_check.get("result") != "pass":
            continue
        if entity_check.get("result") not in {"pass", "fail", "not_checked"}:
            continue
        if substantive_check.get("result") not in {"pass", "fail"}:
            continue

        reused.extend(
            dict(check)
            for check in (source_check, entity_check, substantive_check)
        )
        reused_target_keys.add(target_key)
        strict_issue = _strict_claim_issue(
            item,
            [source_check, entity_check, substantive_check],
        )
        if strict_issue is not None:
            reused_issues.append(strict_issue)
        elif substantive_check.get("result") == "fail":
            reused_issues.append(
                {
                    "label": item.get("label", ""),
                    "chunk_ids": item.get("chunk_ids") or [],
                    "missing_entities": substantive_check.get("missing_entities") or [],
                    "reason": substantive_check.get("reason", ""),
                    **item.get("extra", {}),
                }
            )
    return reused, reused_target_keys, reused_issues


def _source_availability(
    chunk_ids: list[str],
    chunk_texts: dict[str, str],
    bare_chunk_index: dict[str, str],
) -> tuple[str, str, list[str]]:
    """Summarize cited chunk readability without hiding partial failures."""
    readable = [
        chunk_id
        for chunk_id in chunk_ids
        if _resolve_chunk_text(chunk_id, chunk_texts, bare_chunk_index).strip()
    ]
    unreadable = [chunk_id for chunk_id in chunk_ids if chunk_id not in readable]
    if not unreadable:
        return "pass", "所有引用的chunk都能读取。", readable
    if readable:
        return (
            "inconclusive",
            f"部分引用chunk无法读取：{unreadable}。校验只能基于其余可读证据。",
            readable,
        )
    return "fail", f"引用的chunk均无可读原文：{unreadable}。", []


def _verify_items(
    items: list[dict],
    chunk_texts: dict[str, str],
    entity_identity_only: bool = False,
    result_sink: Optional[list[dict]] = None,
    allow_model_calls: bool = True,
) -> list[dict]:
    """三档核验。items里每条要有：label(用于问题定位)、claimed_entities(专有名词列表)、
    chunk_ids(引用的chunk id列表)、content(被核验的原始内容文本，供档位C的prompt使用)、
    extra(附加字段，原样透传进issue，比如field/day，方便调用方后续处理)。

    - 档位A：字面实体核对（含常见地标后缀剥离），零成本。
    - 档位B（别名/同音字消解）：档位A没对上的实体，先问一个窄问题——"是不是原文里
      指代同一事物/地点的另一种写法"（比如"太古里"被素材ASR转写成"泰国里"，"望平街"
      转写成"万平街"——这是真实复现过的问题，见docs/ISSUES.md）。这一步不判断"内容
      有没有实质支撑"，只判断"这个名字本身找不找得到对应"，避免同音字这种小瑕疵拖累
      整条内容被判定为缺乏依据。
      但 `direct/amap` Claim 若只能依赖 ASR 转写和别名模型猜测，不能单独证明
      规范名称；即使同时引用其他来源，也必须由非 ASR 引用原文实际支持该名称。
    - 档位C：对档位B仍然确认"原文真的没提到"的实体，才需要进一步判断：
      entity_identity_only=True时（route_agent用）：档位B的结论本身已经是最终结论，
      跳过档位C，直接把still_missing报告为问题——route只关心地名本身有没有依据，
      不关心地名后面的修饰语，档位B已经回答了这个问题。
      entity_identity_only=False时（lifestyle_agent用）：还需要额外判断整段content
      有没有实质支撑（原有逻辑），但判断范围收窄到档位B筛出来的still_missing，不再
      被已经消解掉的别名/同音字问题拖累。

    独立于上述entity缺失路径，还有第二个触发档位C的条件（entity_identity_only=False
    时才生效，route场景按设计只关心地名本身）：即使claimed_entities全部字面核对通过，
    只要content命中_VERIFIABLE_FACT_PATTERN（数字化的时长/价格/线路号等）、引用了
    `project-evidence-*`，或调用方明确标记direct claim需要实质核验，也会单独触发档位C。
    数字事实和定性事实使用不同提示，避免把“没有数字”本身误判为不支持。Project evidence之所以强制全内容核查，
    是因为它同时带有Agent summary和系统捕获excerpt，不能让“实体名称对得上”再次把Agent
    自己补写的定性事实放行；档位C只读取citation chunk里的原始excerpt。全局核心素材仍只
    对数字化具体事实触发这一额外核查，不扩成全量LLM判断。

    claimed_entities/chunk_ids为空时：区分"老实承认没依据"和"完全没标注"（见
    docs/ISSUES.md）——schema里这两个字段都是可选的（default_factory=list），
    write_agent_output工具不会因为留空就报错，真实复现过agent整批产出12条全部
    留空、核验因此被完全短路却报0冲突的情况。所以空claimed_entities/chunk_ids时，
    先看content里有没有prompt第10/11条要求的"根据一般经验推荐"这类诚实声明——
    有就当作"老实说了没依据"，跳过（这不是"通过"，是"承认没有依据但诚实"）；
    没有就必须报成问题，不能再静默放行，否则核验机制形同虚设。

    chunk_texts整体为空dict（topic在data/material_library/index/下完全没有chunk
    索引，不是"检索不到匹配"，是索引目录本身不存在——真实的极端冷启动场景，见
    docs/ISSUES.md第74条）时，**不再无条件return []**：之前的写法会让"完全没检验过"
    和"检验后确认没问题"在调用方看来毫无区别，第23条修复的诚实声明强制要求、以及
    唯一会触发Bilibili自动补充采集的"打回耗尽"分支（两者都要求issues先非空）会因此
    对这类topic整体失效——真实复现过峨眉山场景住宿价格被写低近一半、遗漏2026年
    最新限流政策，这些本该被诚实声明拦下的内容，因为核验从未真正运行而原样流出。
    改成对每条item套用跟"单条item留空claimed_entities/chunk_ids"完全相同的诚实声明
    判据：有声明放行，没有就报issue。**不调用档位B/C（alias/content judge）**——没有
    任何chunk原文可供LLM参考，combined_text必为空字符串，judge的结论是可预见的
    空转成本，直接用规则判断更省成本也更准确。

    `direct/amap` Claim 的调用方会在三档检查完成后合取证据可用性、
    实体归属和正文实质支持结果；任一明确失败都生成 issue，正文相似不能
    覆盖实体归属失败。路线旧结构项、`point_name` 和非核验型 support mode 保持原边界。
    """
    if not chunk_texts:
        issues = []
        for item in items:
            content = item.get("content", "")
            has_disclaimer = _has_honesty_disclaimer(content)
            reason = (
                "该目的地素材库没有可读取的chunk；内容已经明确声明为一般经验，无法进行来源核验。"
                if has_disclaimer
                else "该目的地素材库完全没有可用chunk，内容缺乏素材依据且没有一般经验声明。"
            )
            _append_check_result(
                result_sink,
                item,
                scope="source_availability",
                result="inconclusive" if has_disclaimer else "fail",
                method="chunk_lookup",
                reason=reason,
                checked_chunk_ids=item.get("chunk_ids") or [],
                chunk_texts={},
                bare_chunk_index={},
            )
            _append_check_result(
                result_sink,
                item,
                scope="entity_identity",
                result="not_checked",
                method="not_run",
                reason="没有可读取的证据原文，未执行实体核对。",
                checked_chunk_ids=[],
                chunk_texts={},
                bare_chunk_index={},
            )
            item_identity_only = entity_identity_only or bool(item.get("_entity_identity_only"))
            if item_identity_only:
                _append_check_result(
                    result_sink,
                    item,
                    scope="substantive_claim",
                    result="not_checked",
                    method="out_of_scope",
                    reason="路线引用核验当前只核对 POI 地点名称；没有核验路线备注和 POI 简介中"
                    "的票价、时间、距离、交通、开放规则及建议等具体事实。",
                    checked_chunk_ids=[],
                    chunk_texts={},
                    bare_chunk_index={},
                )
            if not has_disclaimer:
                issues.append(
                    {
                        "label": item.get("label", ""),
                        "chunk_ids": item.get("chunk_ids") or [],
                        "missing_entities": item.get("claimed_entities") or [],
                        "reason": "该目的地素材库完全没有可用chunk（topic索引不存在），这条内容"
                        "缺乏任何素材依据，也没有诚实声明是凭经验给出的——必须明确注明，"
                        "或者等素材补充后重新生成。",
                        **item.get("extra", {}),
                    }
                )
        return issues

    issues = []
    alias_judge = None  # 惰性创建，档位A都通过的话完全不需要调用模型
    content_judge = None
    bare_chunk_index = _build_bare_chunk_index(chunk_texts)
    doc_chunk_index = _build_doc_chunk_index(chunk_texts)

    for item in items:
        claimed_entities = item.get("claimed_entities") or []
        chunk_ids = item.get("chunk_ids") or []
        # _no_entity_to_verify（2026-08-16新增，见docs/ISSUES.md第125条）：调用方明确知道
        # 这条item没有可核验的具体专有名词（比如point_name是"入馆准备：提前预约+抵达安检"
        # 这种动作步骤标题，不是地名），claimed_entities=[]是刻意的、不代表"模型没写引用"。
        # 只有在这个标志为True时才允许claimed_entities为空还继续往下走（不触发"完全没有
        # 标注"这条判断）——chunk_ids仍然必须非空，真正完全没有sources的item不受影响，
        # 依然会被下面这条判断抓到。默认False，其余调用方（route/lifestyle/pitfall）不传
        # 这个字段时行为跟修复前完全一致。
        no_entity_to_verify = item.get("_no_entity_to_verify", False)
        if not chunk_ids or (not claimed_entities and not no_entity_to_verify):
            content = item.get("content", "")
            has_disclaimer = _has_honesty_disclaimer(content)
            if not chunk_ids:
                _append_check_result(
                    result_sink,
                    item,
                    scope="source_availability",
                    result="inconclusive" if has_disclaimer else "fail",
                    method="source_declaration",
                    reason=(
                        "内容声明为一般经验，没有提供可核对的引用来源。"
                        if has_disclaimer
                        else "内容没有提供可核对的引用来源。"
                    ),
                    checked_chunk_ids=[],
                    chunk_texts=chunk_texts,
                    bare_chunk_index=bare_chunk_index,
                )
            else:
                availability_result, availability_reason, readable_chunk_ids = _source_availability(
                    chunk_ids, chunk_texts, bare_chunk_index
                )
                _append_check_result(
                    result_sink,
                    item,
                    scope="source_availability",
                    result=availability_result,
                    method="chunk_lookup",
                    reason=availability_reason,
                    checked_chunk_ids=readable_chunk_ids,
                    chunk_texts=chunk_texts,
                    bare_chunk_index=bare_chunk_index,
                )
            _append_check_result(
                result_sink,
                item,
                scope="entity_identity",
                result="not_checked" if has_disclaimer else "inconclusive",
                method="missing_claimed_entities" if chunk_ids else "not_run",
                reason=(
                    "内容声明为一般经验，不执行实体来源核对。"
                    if has_disclaimer
                    else "缺少sources或claimed_entities，无法判断实体是否有真实来源。"
                ),
                checked_chunk_ids=chunk_ids,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )
            _append_check_result(
                result_sink,
                item,
                scope="substantive_claim",
                result="not_checked",
                method="not_run",
                reason="引用信息不足，未执行具体事实支持判断。",
                checked_chunk_ids=chunk_ids,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )
            if not has_disclaimer:
                issues.append(
                    {
                        "label": item.get("label", ""),
                        "chunk_ids": chunk_ids,
                        "missing_entities": [],
                        "reason": "这条内容完全没有标注sources/claimed_entities（引用依据），"
                        "也没有诚实声明是凭一般经验推荐的——无法判断内容是否有真实素材支撑，"
                        "必须要么补充真实引用，要么明确注明这是凭经验给出的建议。",
                        **item.get("extra", {}),
                    }
                )
            continue

        ordered_chunk_ids = _ordered_chunk_ids(item, chunk_ids)
        combined_text = "".join(
            _resolve_chunk_text(cid, chunk_texts, bare_chunk_index) for cid in ordered_chunk_ids
        )
        availability_result, availability_reason, readable_chunk_ids = _source_availability(
            chunk_ids, chunk_texts, bare_chunk_index
        )
        _append_check_result(
            result_sink,
            item,
            scope="source_availability",
            result=availability_result,
            method="chunk_lookup",
            reason=availability_reason,
            checked_chunk_ids=readable_chunk_ids,
            chunk_texts=chunk_texts,
            bare_chunk_index=bare_chunk_index,
        )

        # ---- 档位A：字面实体核对（含常见地标后缀剥离），零成本 ----
        missing_entities = [e for e in claimed_entities if e and not _entity_matches(e, combined_text)]
        literal_missing = list(missing_entities)
        checked_sibling_ids: list[str] = []
        resolved_by_sibling: list[str] = []

        # ---- 档位A'：同文档兄弟chunk兜底（零成本，在花费LLM调用之前，见_build_doc_
        # chunk_index的说明/docs/ISSUES.md第127条）----
        if missing_entities and not item.get("_disable_sibling_expansion", False):
            doc_ids = {_doc_id_of(cid) for cid in chunk_ids}
            sibling_ids = [
                sid for doc in doc_ids for sid in doc_chunk_index.get(doc, []) if sid not in chunk_ids
            ]
            if sibling_ids:
                sibling_text = "".join(_resolve_chunk_text(sid, chunk_texts, bare_chunk_index) for sid in sibling_ids)
                resolved_by_sibling = [e for e in missing_entities if _entity_matches(e, sibling_text)]
                if resolved_by_sibling:
                    checked_sibling_ids = sibling_ids
                    combined_text += sibling_text  # 证据并入，供后续档位C共用
                    missing_entities = [e for e in missing_entities if e not in resolved_by_sibling]

        # route全局核验和POI point_name claim都只核对实体身份；后者的“被安排为途经点”
        # 是路线结构决定，不要求素材原文逐字声明“途经”。其余claim按数字事实、Project
        # evidence或调用方强制要求进入实质支持判断。
        content = item.get("content", "")
        item_identity_only = entity_identity_only or bool(item.get("_entity_identity_only"))
        uses_project_evidence = any(cid.startswith("project-evidence-") for cid in chunk_ids)
        has_numeric_fact = bool(_VERIFIABLE_FACT_PATTERN.search(content))
        has_verifiable_fact = not item_identity_only and (
            has_numeric_fact
            or uses_project_evidence
            or bool(item.get("_force_substantive_check"))
        )

        if not claimed_entities and no_entity_to_verify:
            _append_check_result(
                result_sink,
                item,
                scope="entity_identity",
                result="not_checked",
                method="no_entity_to_verify",
                reason="该条目是动作或步骤标题，没有需要核对的专有实体。",
                checked_chunk_ids=chunk_ids,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )
        elif not missing_entities:
            identity_method = "sibling_chunk_match" if literal_missing else "literal_match"
            identity_reason = (
                f"引用chunk未直接命中，但同文档补充chunk支持实体：{resolved_by_sibling}。"
                if literal_missing
                else "引用原文直接出现全部声明实体。"
            )
            _append_check_result(
                result_sink,
                item,
                scope="entity_identity",
                result="pass",
                method=identity_method,
                reason=identity_reason,
                checked_chunk_ids=chunk_ids + checked_sibling_ids,
                supplemental_chunk_ids=checked_sibling_ids,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )

        if not missing_entities and not has_verifiable_fact:
            _append_check_result(
                result_sink,
                item,
                scope="substantive_claim",
                result="not_checked",
                method="out_of_scope" if item_identity_only else "not_triggered",
                reason=(
                    "当前条目只核对地点身份是否有依据；地点被安排为途经点及其先后顺序属于"
                    "路线编排决定，不要求引用原文逐字声明。"
                    if item_identity_only
                    else "当前规则只完成实体核对；内容未触发具体事实支持判断。"
                ),
                checked_chunk_ids=chunk_ids + checked_sibling_ids,
                supplemental_chunk_ids=checked_sibling_ids,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )
            continue  # 都能字面对上，也没有需要额外核实的具体事实模式，通过，不需要档位B/C

        if not allow_model_calls:
            if missing_entities:
                _append_check_result(
                    result_sink,
                    item,
                    scope="entity_identity",
                    result="not_checked",
                    method="model_checks_disabled",
                    reason=(
                        f"当前回填只执行确定性检查；字面和同文档chunk未找到实体"
                        f"{missing_entities}，未调用模型做别名或同音字判断。"
                    ),
                    checked_chunk_ids=chunk_ids + checked_sibling_ids,
                    supplemental_chunk_ids=checked_sibling_ids,
                    missing_entities=missing_entities,
                    chunk_texts=chunk_texts,
                    bare_chunk_index=bare_chunk_index,
                )
            _append_check_result(
                result_sink,
                item,
                scope="substantive_claim",
                result="not_checked",
                method="model_checks_disabled",
                reason=(
                    "当前回填未启用模型，未重新判断具体事实是否得到原文实质支持。"
                    if not item_identity_only
                    else "当前条目只核对实体身份，且本次回填未启用模型别名判断。"
                ),
                checked_chunk_ids=chunk_ids + checked_sibling_ids,
                supplemental_chunk_ids=checked_sibling_ids,
                missing_entities=missing_entities,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )
            continue

        # ---- 档位B：别名/同音字消解，只对档位A没对上的实体做 ----
        still_missing: list[str] = []
        if missing_entities:
            if alias_judge is None:
                # method="json_mode"：deepseek-v4-flash既不支持json_schema响应格式，
                # 也不支持function_calling方式强制的tool_choice="required"（"思考模式下不支持"），
                # json_mode不强制tool_choice，是唯一稳定跑通的方式，见docs/ISSUES.md
                alias_judge = bind_structured_json(
                    get_chat_model(temperature=0, max_tokens=32000), AliasResolution
                )

            alias_prompt = _build_alias_resolution_prompt(
                missing_entities, combined_text
            )
            try:
                resolution: AliasResolution = alias_judge.invoke(alias_prompt)
            except Exception as exc:
                _append_check_result(
                    result_sink,
                    item,
                    scope="entity_identity",
                    result="error",
                    method="alias_resolution",
                    reason=f"别名判断调用失败：{exc}",
                    checked_chunk_ids=chunk_ids + checked_sibling_ids,
                    supplemental_chunk_ids=checked_sibling_ids,
                    missing_entities=missing_entities,
                    chunk_texts=chunk_texts,
                    bare_chunk_index=bare_chunk_index,
                )
                raise
            still_missing, alias_output_conflicts = _effective_alias_missing(
                missing_entities,
                resolution,
            )
            alias_reason = resolution.reason
            alias_resolved = [
                entity for entity in missing_entities if entity not in set(still_missing)
            ]
            if (
                alias_resolved
                and _uses_strict_evidence_conjunction(item)
                and _uses_asr_source(item)
            ):
                unsupported_aliases = [
                    entity
                    for entity in alias_resolved
                    if not _has_non_asr_entity_name_evidence(
                        item,
                        entity,
                        chunk_texts,
                        bare_chunk_index,
                    )
                ]
                if unsupported_aliases:
                    unresolved = set(still_missing) | set(unsupported_aliases)
                    still_missing = [
                        entity for entity in missing_entities if entity in unresolved
                    ]
                    alias_reason = (
                        f"{alias_reason}（当前 direct/amap Claim 的 ASR 转写只可支持体验内容；"
                        f"{'、'.join(unsupported_aliases)}的规范名称缺少非 ASR 独立证据："
                        "未在任何非 ASR 引用原文中得到独立支持，别名判断不能单独将其标记为已确认。）"
                    )
            if alias_output_conflicts:
                alias_reason = (
                    f"{alias_reason}（结构矛盾已确定性归并："
                    f"{'、'.join(alias_output_conflicts)}同时出现在resolved和still_missing，"
                    "按resolved的正向判定从最终缺失集合移除。）"
                )
            _append_check_result(
                result_sink,
                item,
                scope="entity_identity",
                result="fail" if still_missing else "pass",
                method="alias_resolution",
                reason=alias_reason,
                checked_chunk_ids=chunk_ids + checked_sibling_ids,
                supplemental_chunk_ids=checked_sibling_ids,
                missing_entities=still_missing,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )

            if item_identity_only:
                # route场景和POI point_name claim只关心地点身份，档位B结论就是最终结论。
                if still_missing:
                    issues.append(
                        {
                            "label": item.get("label", ""),
                            "chunk_ids": chunk_ids,
                            "missing_entities": still_missing,
                            "reason": alias_reason,
                            **item.get("extra", {}),
                        }
                    )
                _append_check_result(
                    result_sink,
                    item,
                    scope="substantive_claim",
                    result="not_checked",
                    method="out_of_scope",
                    reason="当前条目只核对地点身份是否有依据；地点被安排为途经点及其先后顺序属于"
                    "路线编排决定，不要求引用原文逐字声明。",
                    checked_chunk_ids=chunk_ids + checked_sibling_ids,
                    supplemental_chunk_ids=checked_sibling_ids,
                    chunk_texts=chunk_texts,
                    bare_chunk_index=bare_chunk_index,
                )
                continue

            if not still_missing and not has_verifiable_fact:
                _append_check_result(
                    result_sink,
                    item,
                    scope="substantive_claim",
                    result="not_checked",
                    method="not_triggered",
                    reason="别名核对已经确认实体对应；内容未触发具体事实支持判断。",
                    checked_chunk_ids=chunk_ids + checked_sibling_ids,
                    supplemental_chunk_ids=checked_sibling_ids,
                    chunk_texts=chunk_texts,
                    bare_chunk_index=bare_chunk_index,
                )
                continue  # 全部只是别名/同音字问题，也没有需要额外核实的具体事实，通过

        # ---- 档位C：LLM实质性核查 ----
        # 档位B仍确认缺失实体，或entity都对上但命中数字/Project evidence/direct claim时，
        # 进入实质支持判断。
        if content_judge is None:
            content_judge = bind_structured_json(
                get_chat_model(temperature=0, max_tokens=32000), CitationVerdict
            )

        if still_missing:
            content_prompt = (
                f"下面这段内容声称提到了这些专有名词：{still_missing}，"
                f"但在它引用的原文片段里字面上没有直接找到（可能是同义改写，也可能是编造的）。"
                f"请判断原文片段有没有实质性支持这条内容——不是主题相关就算数，要有具体对应。"
                f"请以JSON格式回复，包含supported(布尔值)和reason(字符串)两个字段。\n\n"
                f"内容：{content}\n\n原文片段：{combined_text}"
            )
        else:
            if uses_project_evidence:
                content_prompt = (
                    f"下面这段内容引用了当前Project的补充证据。Agent提供的检索摘要不属于"
                    f"引用原文，只有这里给出的系统捕获搜索摘录可以作为依据。即使专有名词"
                    f"能对上，也要判断摘录是否实质支持内容里的具体说法；只是主题相关不算支持。"
                    f"请以JSON格式回复，包含supported(布尔值)和reason(字符串)两个字段。\n\n"
                    f"内容：{content}\n\n原始搜索摘录：{combined_text}"
                )
            elif has_numeric_fact:
                # entity都对上了，但content里有数字化的可验证具体事实（时长/价格/线路号等）。
                content_prompt = (
                    f"下面这段内容提到的专有名词都能在引用的原文片段里找到对应，但内容里还包含"
                    f"具体的可验证事实（比如时长、价格、距离、地铁线路号等数字化细节）——这类细节"
                    f"即使地名/店名本身真实存在，也可能是凭经验编出来的，需要单独核实。"
                    f"请判断原文片段有没有实质性支持这条内容里的具体数字/事实细节——不是主题相关"
                    f"就算数，要有具体对应；如果原文根本没有提到任何相关的具体数字/时长/价格/"
                    f"线路信息，应判定为不支持。"
                    f"请以JSON格式回复，包含supported(布尔值)和reason(字符串)两个字段。\n\n"
                    f"内容：{content}\n\n原文片段：{combined_text}"
                )
            else:
                # 调用方要求核验一条定性direct claim。不能沿用“必须有数字”的提示词，否则
                # 会把“适合收尾/保留工业记忆”等定性描述仅因没有数字而误判为不支持。
                content_prompt = (
                    f"下面这段内容提到的专有名词都能在引用原文中找到。请判断原文是否实质支持"
                    f"这条具体的定性说法；不要求内容或原文必须包含数字，但不能只因主题相近就"
                    f"判定支持，也不要把路线编排建议误当成来源事实。"
                    f"请以JSON格式回复，包含supported(布尔值)和reason(字符串)两个字段。\n\n"
                    f"内容：{content}\n\n原文片段：{combined_text}"
                )

        try:
            verdict: CitationVerdict = content_judge.invoke(content_prompt)
        except Exception as exc:
            _append_check_result(
                result_sink,
                item,
                scope="substantive_claim",
                result="error",
                method="substantive_support",
                reason=f"具体事实支持判断调用失败：{exc}",
                checked_chunk_ids=chunk_ids + checked_sibling_ids,
                supplemental_chunk_ids=checked_sibling_ids,
                missing_entities=still_missing,
                chunk_texts=chunk_texts,
                bare_chunk_index=bare_chunk_index,
            )
            raise
        _append_check_result(
            result_sink,
            item,
            scope="substantive_claim",
            result="pass" if verdict.supported else "fail",
            method="substantive_support",
            reason=verdict.reason,
            checked_chunk_ids=chunk_ids + checked_sibling_ids,
            supplemental_chunk_ids=checked_sibling_ids,
            missing_entities=still_missing,
            chunk_texts=chunk_texts,
            bare_chunk_index=bare_chunk_index,
        )
        if not verdict.supported:
            issues.append(
                {
                    "label": item.get("label", ""),
                    "chunk_ids": chunk_ids,
                    "missing_entities": still_missing,
                    "reason": verdict.reason,
                    **item.get("extra", {}),
                }
            )

    return issues


def verify_lifestyle_citations(
    project_id: str,
    topic: str,
    fields: Optional[list[str]] = None,
    item_keys: Optional[set[tuple[str, int]]] = None,
    claim_ids: Optional[set[str]] = None,
    *,
    allow_model_calls: bool = True,
    observation_mode: str = "live",
    persist: bool = True,
    reuse_existing_completed_claims: bool = False,
) -> list[dict]:
    """返回未通过核验的问题列表：[{field, day, recommendation, label, chunk_ids, missing_entities, reason}]

    fields为None（默认）时核验全部四个字段（plan模式/整体重新生成场景）；传入具体字段列表时
    只核验这些字段——edit模式下用于把核验范围收窄到这次编辑实际改动的字段，避免未改动的字段
    被非确定性判断误伤而清空（见docs/ISSUES.md第37补条）。传空列表时不核验任何字段。

    item_keys是比fields更细一档的过滤（{(field, day), ...}），None时不做item级过滤
    （维持fields决定的范围不变）——用于把核验范围进一步收窄到这次编辑实际改动的具体天，
    而不是整个字段。背景：fields级scoping解决了"跨字段"误伤，但同一个字段内通常有多天
    的item，用户edit只改了其中一天时，fields级scoping仍会把同字段里其它天完全没被
    触碰的item一起送进_verify_items——如果那些item本身字面对不上（需要走有非确定性的
    档位B/C判断），有实测概率被无端判定为"缺乏依据"而清空，即使它们此前已经通过核验、
    这次编辑压根没有涉及。fields和item_keys可以同时生效，互不冲突（fields先筛字段范围，
    item_keys在字段内部再筛具体天）。两者都是None时行为完全不变。

    claim_ids是返修阶段的最细粒度过滤。传入后只重新核验指定claim，
    未改动的已通过claim保留原证据记录；被返修删除的旧claim会从当前视图移除，
    但失败历史仍由write_evidence_checks归档。空集合表示明确不核验任何claim。

    reuse_existing_completed_claims只用于中断恢复：仅当Claim内容、实体、来源顺序、
    证据原文和核验模式的指纹全部一致，且此前三层检查已形成完整业务结论时，复用
    `pass`或`fail`。技术错误、不确定和半截记录不复用。默认关闭，避免普通显式复核
    被历史结果短路。
    """
    if claim_ids == set():
        return []

    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
    if not lifestyle:
        if not persist:
            return []
        store.write_evidence_checks(
            project_id, "lifestyle", [], replace_prefixes=["lifestyle:"]
        )
        return []

    chunk_texts = load_chunk_texts(topic, project_id)
    target_fields = fields if fields is not None else ["food", "outfit", "lodging", "play"]

    items = []
    claim_items = []
    manual_checks: list[dict] = []
    for field in target_fields:
        for item_index, lf_item in enumerate(lifestyle.get(field, {}).get("items", [])):
            if item_keys is not None and (field, lf_item.get("day")) not in item_keys:
                continue
            item_key = f"lifestyle:{field}:day:{lf_item.get('day')}:item:{item_index}"
            claims = lf_item.get("claims") or []
            if claims:
                claim_by_id = {str(claim.get("claim_id") or ""): claim for claim in claims}
                claim_extra = {
                    "field": field,
                    "day": lf_item.get("day"),
                    "recommendation": lf_item.get("recommendation", ""),
                }
                for claim in claims:
                    claim_id = str(claim.get("claim_id") or "")
                    if claim_ids is not None and claim_id not in claim_ids:
                        continue
                    claim_key = f"{item_key}:claim:{claim.get('claim_id')}"
                    support_mode = str(claim.get("support_mode") or "unverified")
                    if support_mode in {"estimate", "unverified", "user_constraint"}:
                        manual_checks.append(
                            _evidence_claim_manual_check(
                                target_type="lifestyle_claim",
                                target_key=claim_key,
                                claim=claim,
                                observation_mode=observation_mode,
                                extra=claim_extra,
                            )
                        )
                    else:
                        claim_items.append(
                            _evidence_claim_item(
                                target_type="lifestyle_claim",
                                target_key=claim_key,
                                label=f"{field}.day{lf_item.get('day')}事实「{claim.get('claim_text', '')}」",
                                claim=claim,
                                claim_by_id=claim_by_id,
                                chunk_texts=chunk_texts,
                                observation_mode=observation_mode,
                                extra=claim_extra,
                            )
                        )
                continue
            if claim_ids is not None:
                # 旧产物没有claim粒度，不能被claim_ids误选中后整条重验。
                continue
            sources = _canonicalize_sources(lf_item.get("sources"))
            items.append(
                {
                    "target_type": "lifestyle_item",
                    "target_key": item_key,
                    "observation_mode": observation_mode,
                    "label": f"{field}.day{lf_item.get('day')}「{lf_item.get('recommendation', '')[:20]}...」",
                    "claimed_entities": lf_item.get("claimed_entities") or [],
                    "sources": sources,
                    "chunk_ids": [s["chunk_id"] for s in sources if s.get("chunk_id")],
                    "_web_verified_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "web_verified"
                    ],
                    "_amap_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "amap"
                    ],
                    "content": lf_item.get("recommendation", ""),
                    "extra": {
                        "field": field,
                        "day": lf_item.get("day"),
                        "recommendation": lf_item.get("recommendation", ""),
                    },
                }
            )

    all_items = items + claim_items
    chunk_texts = _merge_web_verified_evidence(all_items, chunk_texts, project_id, "lifestyle_agent")
    chunk_texts = _merge_amap_evidence(all_items, chunk_texts, topic)
    for item in claim_items:
        item["_verification_fingerprint"] = _verification_fingerprint(item, chunk_texts)
    checks: list[dict] = list(manual_checks)
    reused_target_keys: set[str] = set()
    reused_issues: list[dict] = []
    if reuse_existing_completed_claims:
        reused_checks, reused_target_keys, reused_issues = _reusable_completed_claim_checks(
            store.read_evidence_checks(project_id).get("lifestyle", []),
            claim_items,
        )
        checks.extend(reused_checks)
    if claim_ids is not None:
        selected_target_keys = {
            str(item.get("target_key") or "")
            for item in [*manual_checks, *claim_items]
            if item.get("target_key")
        }
        existing_target_keys = {
            str(item.get("target_key") or "")
            for item in store.read_evidence_checks(project_id).get("lifestyle", [])
            if str(item.get("claim_id") or "") in claim_ids and item.get("target_key")
        }
        replace_prefixes = sorted(selected_target_keys | existing_target_keys)
    elif item_keys is not None:
        replace_prefixes = [f"lifestyle:{field}:day:{day}:" for field, day in sorted(item_keys)]
    elif fields is not None:
        replace_prefixes = [f"lifestyle:{field}:" for field in fields]
    else:
        replace_prefixes = ["lifestyle:"]
    try:
        issues = list(reused_issues)
        issues.extend(_verify_items(
            items,
            chunk_texts,
            result_sink=checks,
            allow_model_calls=allow_model_calls,
        ))
        issues.extend(
            _verify_evidence_claim_items(
                claim_items,
                chunk_texts,
                checks,
                allow_model_calls=allow_model_calls,
                preverified_target_keys=reused_target_keys,
            )
        )
        return issues
    finally:
        if persist:
            store.write_evidence_checks(
                project_id,
                "lifestyle",
                checks,
                replace_prefixes=replace_prefixes,
            )


def verify_pitfall_citations(
    project_id: str,
    topic: str,
    fields: Optional[list[str]] = None,
    item_keys: Optional[set[tuple[str, int]]] = None,
    *,
    allow_model_calls: bool = True,
    observation_mode: str = "live",
    persist: bool = True,
) -> list[dict]:
    """避雷agent产出的核验，跟verify_lifestyle_citations完全对称——核验对象从
    outputs/lifestyle.json换成outputs/pitfall.json，content换成warning字段。
    entity_identity_only=False（默认），因为避雷内容本身是具体事实性描述（跟lifestyle
    同一类风险），不是route_agent那种"只核验地名本身"的场景。见docs/ARCHITECTURE.md第7节。

    fields/item_keys参数语义跟verify_lifestyle_citations完全一致
    （None=全部字段/不做item级过滤，见docs/ISSUES.md第37补条）。

    返回未通过核验的问题列表：[{field, day, warning, label, chunk_ids, missing_entities, reason}]
    """
    pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
    if not pitfall:
        if not persist:
            return []
        store.write_evidence_checks(
            project_id, "pitfall", [], replace_prefixes=["pitfall:"]
        )
        return []

    chunk_texts = load_chunk_texts(topic, project_id)
    target_fields = fields if fields is not None else ["food", "outfit", "lodging", "play"]

    items = []
    claim_items = []
    manual_checks: list[dict] = []
    for field in target_fields:
        for item_index, pf_item in enumerate(pitfall.get(field, {}).get("items", [])):
            if item_keys is not None and (field, pf_item.get("day")) not in item_keys:
                continue
            warning = pf_item.get("warning", "")
            if not warning:
                # 合法的空结果——这条推荐确实没查到避雷内容，warning本身留空就是诚实声明，
                # 不需要（也不应该）走_has_honesty_disclaimer那套"没有claimed_entities/chunk_ids
                # 时必须有诚实措辞"的判断，否则会把这个合法空结果误报成"完全没有引用依据"的问题。
                continue
            item_key = f"pitfall:{field}:day:{pf_item.get('day')}:item:{item_index}"
            claims = pf_item.get("claims") or []
            if claims:
                claim_by_id = {str(claim.get("claim_id") or ""): claim for claim in claims}
                claim_extra = {
                    "field": field,
                    "day": pf_item.get("day"),
                    "warning": warning,
                }
                for claim in claims:
                    claim_key = f"{item_key}:claim:{claim.get('claim_id')}"
                    support_mode = str(claim.get("support_mode") or "unverified")
                    if support_mode in {"estimate", "unverified", "user_constraint"}:
                        manual_checks.append(
                            _evidence_claim_manual_check(
                                target_type="pitfall_claim",
                                target_key=claim_key,
                                claim=claim,
                                observation_mode=observation_mode,
                                extra=claim_extra,
                            )
                        )
                    else:
                        claim_items.append(
                            _evidence_claim_item(
                                target_type="pitfall_claim",
                                target_key=claim_key,
                                label=f"{field}.day{pf_item.get('day')}避雷事实「{claim.get('claim_text', '')}」",
                                claim=claim,
                                claim_by_id=claim_by_id,
                                chunk_texts=chunk_texts,
                                observation_mode=observation_mode,
                                extra=claim_extra,
                            )
                        )
                continue
            sources = _canonicalize_sources(pf_item.get("sources"))
            items.append(
                {
                    "target_type": "pitfall_item",
                    "target_key": item_key,
                    "observation_mode": observation_mode,
                    "label": f"{field}.day{pf_item.get('day')}「{warning[:20]}...」",
                    "claimed_entities": pf_item.get("claimed_entities") or [],
                    "sources": sources,
                    "chunk_ids": [s["chunk_id"] for s in sources if s.get("chunk_id")],
                    "_web_verified_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "web_verified"
                    ],
                    "_amap_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "amap"
                    ],
                    "content": warning,
                    "extra": {
                        "field": field,
                        "day": pf_item.get("day"),
                        "warning": warning,
                    },
                }
            )

    all_items = items + claim_items
    chunk_texts = _merge_web_verified_evidence(all_items, chunk_texts, project_id, "pitfall_agent")
    chunk_texts = _merge_amap_evidence(all_items, chunk_texts, topic)
    checks: list[dict] = list(manual_checks)
    if item_keys is not None:
        replace_prefixes = [f"pitfall:{field}:day:{day}:" for field, day in sorted(item_keys)]
    elif fields is not None:
        replace_prefixes = [f"pitfall:{field}:" for field in fields]
    else:
        replace_prefixes = ["pitfall:"]
    try:
        issues = _verify_items(
            items,
            chunk_texts,
            result_sink=checks,
            allow_model_calls=allow_model_calls,
        )
        issues.extend(
            _verify_evidence_claim_items(
                claim_items,
                chunk_texts,
                checks,
                allow_model_calls=allow_model_calls,
            )
        )
        return issues
    finally:
        if persist:
            store.write_evidence_checks(
                project_id,
                "pitfall",
                checks,
                replace_prefixes=replace_prefixes,
            )


def _extract_poi_names(poi: str) -> list[str]:
    """从"地名（修饰性描述）"格式的POI字符串里，剥离括号里的修饰语，只留纯地名用于核验。

    背景（详见docs/ISSUES.md）：真实运行中发现route_agent写的POI形如
    "武侯祠博物馆（三国文化、红墙夹道拍照）"——地名本身在素材里有依据，但括号里
    "红墙夹道拍照"这类修饰性描述是模型自己综合归纳的措辞，永远不会逐字出现在原文
    （不是编造，是描述颗粒度和素材的口语转写颗粒度不匹配）。之前把整串"地名（描述）"
    当claimed_entity做字面核对，档位A必然100%失败，从而让"地名真实存在、只是形容词
    没逐字对上"的POI被误判为"缺乏依据"打回。用户明确的取舍是：地名必须查得到依据（这
    是防止真的编造一个不存在的地点/内容），修饰语允许模型自由发挥，不需要逐字有出处。

    并列地名（如"安顺廊桥/九眼桥"、"望平街-安顺廊桥"、"灌县古城·南桥"）按
    "/"、"、"、"-"、"·"拆开分别核验，
    只要素材原文分别提到过这些地名即可，不要求它们以并列的复合形式整体出现在同一处。
    """
    stripped = re.sub(r"[（(][^（）()]*[）)]\s*$", "", poi).strip()
    return [name.strip() for name in re.split(r"[/、\-－·]", stripped) if name.strip()]


def _poi_display_name(poi: dict | str) -> str:
    """route_plan.json v2的pois是对象列表（{name,brief,needs_detail,...}），
    v1是纯字符串列表——统一取出用于核验/展示的名称字符串，两种格式都兼容
    （兼容性只是防御旧数据，不是要长期维护两套schema）。"""
    return poi.get("name", "") if isinstance(poi, dict) else poi


def _route_claim_manual_check(day: dict, claim: dict, observation_mode: str) -> dict:
    return _evidence_claim_manual_check(
        target_type="route_claim",
        target_key=f"route:day:{day.get('day')}:claim:{claim.get('claim_id')}",
        claim=claim,
        observation_mode=observation_mode,
        extra={
            "day": day.get("day"),
            "city": day.get("city"),
            "area": day.get("area"),
        },
    )


def _evidence_claim_manual_check(
    *,
    target_type: str,
    target_key: str,
    claim: dict,
    observation_mode: str,
    extra: dict,
) -> dict:
    support_mode = str(claim.get("support_mode") or "unverified")
    messages = {
        "estimate": ("declared_estimate", "该事实已明确标记为一般经验估算，不冒充外部证据核验通过。"),
        "unverified": ("declared_unverified", "该事实已明确标记为未核验，目前没有可靠证据支持。"),
        "user_constraint": ("user_constraint", "该内容声明来自用户约束，本步骤不判断外部事实真伪。"),
    }
    method, reason = messages.get(
        support_mode,
        ("not_run", f"support_mode={support_mode}当前没有可执行的事实核验方法。"),
    )
    evidence = []
    for snapshot in claim.get("evidence_snapshots") or []:
        source = snapshot.get("source") or {}
        evidence.append(
            {
                "chunk_id": source.get("chunk_id", ""),
                "role": "cited",
                "platform": source.get("platform", ""),
                "author": source.get("author", ""),
                "doc_id": source.get("doc_id", ""),
                "source_url": source.get("source_url", ""),
                "excerpt": snapshot.get("excerpt", ""),
            }
        )
    return {
        "target_type": target_type,
        "target_key": target_key,
        "target_text": claim.get("claim_text", ""),
        "scope": "substantive_claim",
        "result": "not_checked",
        "method": method,
        "reason": reason,
        "cited_sources": claim.get("evidence_refs") or [],
        "cited_chunk_ids": [
            source.get("chunk_id")
            for source in claim.get("evidence_refs") or []
            if source.get("chunk_id")
        ],
        "checked_chunk_ids": [],
        "evidence": evidence,
        "missing_entities": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "observation_mode": observation_mode,
        "claim_id": claim.get("claim_id"),
        "claim_text": claim.get("claim_text"),
        "claim_type": claim.get("claim_type"),
        "support_mode": support_mode,
        "target_path": claim.get("target_path"),
        **extra,
    }


def _evidence_claim_item(
    *,
    target_type: str,
    target_key: str,
    label: str,
    claim: dict,
    claim_by_id: dict[str, dict],
    chunk_texts: dict[str, str],
    observation_mode: str,
    extra: dict,
) -> dict:
    support_mode = str(claim.get("support_mode") or "unverified")
    sources = _canonicalize_sources(claim.get("evidence_refs"))
    claim_chunk_ids = [source["chunk_id"] for source in sources if source.get("chunk_id")]
    if support_mode == "derived":
        sources = []
        claim_chunk_ids = []
        for dependency_id in claim.get("derived_from") or []:
            dependency = claim_by_id.get(str(dependency_id))
            if not dependency:
                continue
            synthetic_id = f"derived-claim-{dependency_id}"
            chunk_texts[synthetic_id] = str(dependency.get("claim_text") or "")
            sources.append(
                {
                    "platform": "derived_claim",
                    "doc_id": "同一产物对象的事实",
                    "chunk_id": synthetic_id,
                    "source_url": "",
                    "scope": "project",
                }
            )
            claim_chunk_ids.append(synthetic_id)

    for snapshot in claim.get("evidence_snapshots") or []:
        source = snapshot.get("source") or {}
        chunk_id = str(source.get("chunk_id") or "")
        excerpt = str(snapshot.get("excerpt") or "")
        if chunk_id and excerpt:
            chunk_texts[chunk_id] = excerpt

    entities = [str(item) for item in claim.get("claimed_entities") or [] if item]
    if not entities and claim.get("subject"):
        entities = [str(claim.get("subject"))]
    if not entities and claim.get("target_path") == "point_name":
        # point_name即使漏填claimed_entities，也不能退化成“没有实体可核验”；使用所属
        # segment的地点名补回身份检查，避免任意非空引用都被静默放行。
        entities = _extract_poi_names(str(extra.get("point_name") or ""))
    return {
        "target_type": target_type,
        "target_key": target_key,
        "observation_mode": observation_mode,
        "label": label,
        "claimed_entities": entities,
        "_no_entity_to_verify": not entities,
        "sources": sources,
        "chunk_ids": claim_chunk_ids,
        # point_name只证明地点身份；是否把它排进当前路线由结构本身表达。highlight和
        # suggested_duration仍必须核验原文是否实质支持。
        "_entity_identity_only": claim.get("target_path") == "point_name",
        "_force_substantive_check": claim.get("target_path") != "point_name",
        "_disable_sibling_expansion": True,
        "content": str(claim.get("claim_text") or ""),
        "extra": {
            **extra,
            "claim_id": claim.get("claim_id"),
            "claim_text": claim.get("claim_text"),
            "claim_type": claim.get("claim_type"),
            "support_mode": support_mode,
            "target_path": claim.get("target_path"),
            "derived_from": claim.get("derived_from") or [],
        },
    }


def _verify_evidence_claim_items(
    claim_items: list[dict],
    chunk_texts: dict[str, str],
    checks: list[dict],
    *,
    allow_model_calls: bool,
    preverified_target_keys: Optional[set[str]] = None,
) -> list[dict]:
    targets_to_verify = [
        item
        for item in claim_items
        if item.get("target_key") not in (preverified_target_keys or set())
    ]
    issues = _verify_items(
        targets_to_verify,
        chunk_texts,
        entity_identity_only=False,
        result_sink=checks,
        allow_model_calls=allow_model_calls,
    )
    # Interrupted-run reuse already returned the aggregate issue for every
    # preverified target. Re-aggregating those skipped targets here would append
    # the same strict conjunction failure a second time.
    _merge_strict_claim_issues(targets_to_verify, checks, issues)
    support_mode_by_key = {
        item["target_key"]: item.get("extra", {}).get("support_mode") for item in claim_items
    }
    dependencies_by_key = {
        item["target_key"]: item.get("extra", {}).get("derived_from") or [] for item in claim_items
    }
    extra_by_key = {
        item["target_key"]: item.get("extra", {}) for item in claim_items
    }
    substantive_by_claim = {
        str(check.get("claim_id") or ""): check
        for check in checks
        if check.get("scope") == "substantive_claim" and check.get("claim_id")
    }
    for check in checks:
        mode = support_mode_by_key.get(check.get("target_key"))
        if check.get("scope") == "substantive_claim" and check.get("method") == "substantive_support":
            if mode == "amap":
                check["method"] = "amap_measurement"
            elif mode == "derived":
                check["method"] = "derived_support"
        if check.get("scope") != "substantive_claim" or mode != "derived":
            continue
        dependencies = dependencies_by_key.get(check.get("target_key"), [])
        unavailable = [
            dependency_id
            for dependency_id in dependencies
            if substantive_by_claim.get(str(dependency_id), {}).get("result") != "pass"
        ]
        if not unavailable:
            continue
        check["result"] = "inconclusive"
        check["method"] = "derived_dependency"
        check["reason"] = (
            "推导所依赖的事实未全部核验通过，不能把本条推导视为已证实："
            + "、".join(str(item) for item in unavailable)
        )
        issues.append(
            {
                "label": check.get("target_text", ""),
                "chunk_ids": check.get("cited_chunk_ids") or [],
                "missing_entities": [],
                "reason": check["reason"],
                "claim_id": check.get("claim_id"),
                "claim_text": check.get("claim_text"),
                "claim_type": check.get("claim_type"),
                "support_mode": mode,
                "target_path": check.get("target_path"),
                **extra_by_key.get(check.get("target_key"), {}),
            }
        )
    return issues
    method, reason = messages.get(
        support_mode,
        ("not_run", f"support_mode={support_mode}当前没有可执行的事实核验方法。"),
    )
    evidence = []
    for snapshot in claim.get("evidence_snapshots") or []:
        source = snapshot.get("source") or {}
        evidence.append(
            {
                "chunk_id": source.get("chunk_id", ""),
                "role": "cited",
                "platform": source.get("platform", ""),
                "author": source.get("author", ""),
                "doc_id": source.get("doc_id", ""),
                "source_url": source.get("source_url", ""),
                "excerpt": snapshot.get("excerpt", ""),
            }
        )
    return {
        "target_type": "route_claim",
        "target_key": f"route:day:{day.get('day')}:claim:{claim.get('claim_id')}",
        "target_text": claim.get("claim_text", ""),
        "scope": "substantive_claim",
        "result": "not_checked",
        "method": method,
        "reason": reason,
        "cited_sources": claim.get("evidence_refs") or [],
        "cited_chunk_ids": [
            source.get("chunk_id")
            for source in claim.get("evidence_refs") or []
            if source.get("chunk_id")
        ],
        "checked_chunk_ids": [],
        "evidence": evidence,
        "missing_entities": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "observation_mode": observation_mode,
        "day": day.get("day"),
        "city": day.get("city"),
        "area": day.get("area"),
        "claim_id": claim.get("claim_id"),
        "claim_text": claim.get("claim_text"),
        "claim_type": claim.get("claim_type"),
        "support_mode": support_mode,
        "target_path": claim.get("target_path"),
    }


def verify_route_citations(
    project_id: str,
    topic: str,
    days: Optional[list[int]] = None,
    *,
    allow_model_calls: bool = True,
    observation_mode: str = "live",
    persist: bool = True,
) -> list[dict]:
    """返回未通过核验的问题列表：[{day, city, area, pois, label, chunk_ids, missing_entities, reason}]

    claimed_entities用_extract_poi_names从RouteDay.pois里剥离出的纯地名列表，不直接用
    pois原始字符串——见_extract_poi_names的说明，只核验地名本身，不核验地名后面的修饰语。

    days为None（默认）时核验全部天数（plan模式/整体重新生成场景）；传入具体天数列表时
    只核验这些天——edit模式下用于把核验范围收窄到这次编辑实际改动的天数，跟
    verify_lifestyle_citations/verify_pitfall_citations的fields参数同一个思路
    （见docs/ISSUES.md第37补条）。
    """
    route_plan = store.read_workspace(project_id, "route_plan.json")
    if not route_plan:
        if not persist:
            return []
        store.write_evidence_checks(project_id, "route", [], replace_prefixes=["route:"])
        return []

    chunk_texts = load_chunk_texts(topic, project_id)
    target_days = set(days) if days is not None else None

    day_items = []
    claim_items = []
    manual_checks: list[dict] = []
    for day in route_plan.get("days", []):
        if target_days is not None and day.get("day") not in target_days:
            continue
        claims = day.get("claims") or []
        sources = _canonicalize_sources(day.get("sources"))
        pois = day.get("pois") or []
        poi_display = [_poi_display_name(p) for p in pois]
        poi_names = [name for poi in poi_display for name in _extract_poi_names(poi)]
        # Claims are the current fact-level contract.  The day-level check is
        # retained only for legacy plans that have not migrated to claims;
        # running both would require a second, redundant day source union and
        # incorrectly reject user-confirmed fixed routes.
        if not claims:
            day_items.append(
                {
                    "target_type": "route_day",
                    "target_key": f"route:day:{day.get('day')}",
                    "observation_mode": observation_mode,
                    "label": f"Day{day.get('day')}「{day.get('area', day.get('city', ''))}」pois={poi_display}",
                    "claimed_entities": poi_names,
                    "sources": sources,
                    "chunk_ids": [s["chunk_id"] for s in sources if s.get("chunk_id")],
                    "_web_verified_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "web_verified"
                    ],
                    "_amap_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "amap"
                    ],
                    "content": f"{day.get('city', '')} {day.get('area', '')}：{day.get('notes', '')} POIs: {poi_display}",
                    "extra": {
                        "day": day.get("day"),
                        "city": day.get("city"),
                        "area": day.get("area"),
                        "pois": poi_display,
                    },
                }
            )

        claim_by_id = {str(claim.get("claim_id") or ""): claim for claim in claims}
        for claim in claims:
            support_mode = str(claim.get("support_mode") or "unverified")
            if support_mode in {"estimate", "unverified", "user_constraint"}:
                manual_checks.append(_route_claim_manual_check(day, claim, observation_mode))
                continue

            sources = _canonicalize_sources(claim.get("evidence_refs"))
            claim_chunk_ids = [source["chunk_id"] for source in sources if source.get("chunk_id")]
            if support_mode == "derived":
                sources = []
                claim_chunk_ids = []
                for dependency_id in claim.get("derived_from") or []:
                    dependency = claim_by_id.get(str(dependency_id))
                    if not dependency:
                        continue
                    synthetic_id = f"derived-claim-{dependency_id}"
                    chunk_texts[synthetic_id] = str(dependency.get("claim_text") or "")
                    sources.append(
                        {
                            "platform": "derived_claim",
                            "doc_id": f"Day{day.get('day')}事实",
                            "chunk_id": synthetic_id,
                            "source_url": "",
                            "scope": "project",
                        }
                    )
                    claim_chunk_ids.append(synthetic_id)

            for snapshot in claim.get("evidence_snapshots") or []:
                source = snapshot.get("source") or {}
                chunk_id = str(source.get("chunk_id") or "")
                excerpt = str(snapshot.get("excerpt") or "")
                if chunk_id and excerpt:
                    chunk_texts[chunk_id] = excerpt

            entities = [str(item) for item in claim.get("claimed_entities") or [] if item]
            if not entities and claim.get("subject"):
                entities = [str(claim.get("subject"))]
            identity_only = str(claim.get("claim_type") or "") == "poi_identity"
            claim_items.append(
                {
                    "target_type": "route_claim",
                    "target_key": f"route:day:{day.get('day')}:claim:{claim.get('claim_id')}",
                    "observation_mode": observation_mode,
                    "label": f"Day{day.get('day')}事实「{claim.get('claim_text', '')}」",
                    "claimed_entities": entities,
                    "_no_entity_to_verify": not entities,
                    "sources": sources,
                    "chunk_ids": claim_chunk_ids,
                    "_entity_identity_only": identity_only,
                    "_force_substantive_check": not identity_only,
                    "_disable_sibling_expansion": True,
                    "content": str(claim.get("claim_text") or ""),
                    "extra": {
                        "day": day.get("day"),
                        "city": day.get("city"),
                        "area": day.get("area"),
                        "claim_id": claim.get("claim_id"),
                        "claim_text": claim.get("claim_text"),
                        "claim_type": claim.get("claim_type"),
                        "support_mode": support_mode,
                        "target_path": claim.get("target_path"),
                        "derived_from": claim.get("derived_from") or [],
                    },
                }
            )

    chunk_texts = _merge_web_verified_evidence(day_items, chunk_texts, project_id, "route_agent")
    chunk_texts = _merge_amap_evidence(day_items, chunk_texts, topic)
    for item in claim_items:
        item["_verification_fingerprint"] = _verification_fingerprint(item, chunk_texts)
    checks: list[dict] = list(manual_checks)
    replace_prefixes = (
        [f"route:day:{day}" for day in days]
        if days is not None
        else ["route:"]
    )
    try:
        issues = _verify_items(
            day_items,
            chunk_texts,
            entity_identity_only=True,
            result_sink=checks,
            allow_model_calls=allow_model_calls,
        )
        claim_issues = _verify_items(
            claim_items,
            chunk_texts,
            entity_identity_only=False,
            result_sink=checks,
            allow_model_calls=allow_model_calls,
        )
        issues.extend(claim_issues)
        _merge_strict_claim_issues(claim_items, checks, issues)
        support_mode_by_key = {
            item["target_key"]: item.get("extra", {}).get("support_mode") for item in claim_items
        }
        dependencies_by_key = {
            item["target_key"]: item.get("extra", {}).get("derived_from") or [] for item in claim_items
        }
        substantive_by_claim = {
            str(check.get("claim_id") or ""): check
            for check in checks
            if check.get("target_type") == "route_claim"
            and check.get("scope") == "substantive_claim"
            and check.get("claim_id")
        }
        for check in checks:
            mode = support_mode_by_key.get(check.get("target_key"))
            if check.get("scope") == "substantive_claim" and check.get("method") == "substantive_support":
                if mode == "amap":
                    check["method"] = "amap_measurement"
                elif mode == "derived":
                    check["method"] = "derived_support"
            if check.get("scope") != "substantive_claim" or mode != "derived":
                continue
            dependencies = dependencies_by_key.get(check.get("target_key"), [])
            unavailable = [
                dependency_id
                for dependency_id in dependencies
                if substantive_by_claim.get(str(dependency_id), {}).get("result") != "pass"
            ]
            if not unavailable:
                continue
            check["result"] = "inconclusive"
            check["method"] = "derived_dependency"
            check["reason"] = (
                "推导所依赖的事实未全部核验通过，不能把本条推导视为已证实："
                + "、".join(str(item) for item in unavailable)
            )
            issues.append(
                {
                    "label": f"Day{check.get('day')}事实「{check.get('target_text', '')}」",
                    "chunk_ids": check.get("cited_chunk_ids") or [],
                    "missing_entities": [],
                    "reason": check["reason"],
                    "day": check.get("day"),
                    "city": check.get("city"),
                    "area": check.get("area"),
                    "claim_id": check.get("claim_id"),
                    "claim_text": check.get("claim_text"),
                    "claim_type": check.get("claim_type"),
                    "support_mode": mode,
                    "target_path": check.get("target_path"),
                }
            )
        return issues
    finally:
        if persist:
            store.write_evidence_checks(
                project_id,
                "route",
                checks,
                replace_prefixes=replace_prefixes,
            )


def verify_poi_detail_citations(
    poi_detail: dict,
    topic: str,
    route_names: Optional[list[str]] = None,
    project_id: Optional[str] = None,
    log_scope: Optional[str] = None,
    *,
    segment_keys: Optional[list[tuple[str, int]]] = None,
    allow_model_calls: bool = True,
    observation_mode: str = "live",
    persist: bool = True,
) -> list[dict]:
    """POI详情agent的核验：核对每条路线每个途经点的point_name/highlight是否有真实chunk依据，
    跟lifestyle_agent的核验对称（entity_identity_only=False，要求highlight的具体描述本身
    有实质支撑，不只是point_name这个名字存在——见docs/AGENTS.md第6节第5点"不要编造具体细节，
    比如虚构的开放时间、不存在的观景台"，这跟route_agent"只核验地名本身"的要求不同，更接近
    lifestyle_agent对内容实质性的要求）。

    直接接收poi_detail这个dict（不是project_id读盘），因为这是"树木生成中"阶段单个并行分支
    内部的核验循环，此时文件刚由这一次write_agent_output写入，调用方（poi_detail_agent_node）
    已经有现成的写入后数据，不需要走store.read_poi_detail重新读一遍。

    route_names为None（默认）时核验全部路线（plan模式首次生成/树木生成中阶段场景，行为不变）；
    传入具体route_name列表时只核验这些路线——poi_detail_edit_node（POI详情agent的edit入口）
    据此把核验范围收窄到这次编辑实际改动/新增的路线，未涉及路线不重新核验，跟
    verify_route_citations的days/verify_lifestyle_citations的fields同一个思路
    （见docs/ISSUES.md第37补条）。

    segment_keys进一步按(route_name, order)收窄到具体途经点，供引用核验后的局部返修使用。
    它和route_names可以同时提供；此时必须同时满足两个范围。证据台账也只替换这些途经点
    的target_key，不覆盖同一路线中未重新核验的其它途经点。

    project_id/log_scope：可选，传入时用于核验sources.platform=="web_verified"的条目
    （见docs/ISSUES.md第115条方向C）——不传（默认None）时行为跟之前完全一致，
    这类来源会因为查不到chunk原文被判定缺乏依据。调用方（_run_poi_detail_citation_loop）
    传入`f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}"`。

    返回问题列表：[{route_name, order, point_name, label, chunk_ids, missing_entities, reason}]
    """
    chunk_texts = load_chunk_texts(topic, project_id)
    target_routes = set(route_names) if route_names is not None else None
    target_segments = (
        {(str(route_name), int(order)) for route_name, order in segment_keys}
        if segment_keys is not None
        else None
    )

    items = []
    claim_items = []
    manual_checks: list[dict] = []
    poi_name = str(poi_detail.get("poi_name") or "unknown")
    for route in poi_detail.get("routes", []) or []:
        route_name = str(route.get("route_name") or "")
        if target_routes is not None and route_name not in target_routes:
            continue
        for seg in route.get("route_segments", []) or []:
            segment_order = int(seg.get("order"))
            if (
                target_segments is not None
                and (route_name, segment_order) not in target_segments
            ):
                continue
            item_key = (
                f"poi_detail:{poi_name}:route:{route_name}:"
                f"order:{segment_order}"
            )
            claims = seg.get("claims") or []
            if claims:
                claim_by_id = {str(claim.get("claim_id") or ""): claim for claim in claims}
                claim_extra = {
                    "route_name": route_name,
                    "order": segment_order,
                    "point_name": seg.get("point_name", ""),
                    "poi_name": poi_name,
                }
                for claim in claims:
                    claim_key = f"{item_key}:claim:{claim.get('claim_id')}"
                    support_mode = str(claim.get("support_mode") or "unverified")
                    if support_mode in {"estimate", "unverified", "user_constraint"}:
                        manual_checks.append(
                            _evidence_claim_manual_check(
                                target_type="poi_detail_claim",
                                target_key=claim_key,
                                claim=claim,
                                observation_mode=observation_mode,
                                extra=claim_extra,
                            )
                        )
                    else:
                        claim_items.append(
                            _evidence_claim_item(
                                target_type="poi_detail_claim",
                                target_key=claim_key,
                                label=(
                                    f"{route_name}.途经点{segment_order}"
                                    f"事实「{claim.get('claim_text', '')}」"
                                ),
                                claim=claim,
                                claim_by_id=claim_by_id,
                                chunk_texts=chunk_texts,
                                observation_mode=observation_mode,
                                extra=claim_extra,
                            )
                        )
                continue
            sources = _canonicalize_sources(seg.get("sources"))
            point_name = seg.get("point_name", "")
            # point_name和route_plan的pois一样，模型常写成"武侯祠博物馆大门/文物区入口"、
            # "刘备殿（汉昭烈庙）"这种"名称（描述）/别名"的复合串——真实E2E里复现过，如果整串
            # 当一个claimed_entity做字面核对，会因为括号描述/斜杠别名永远匹配不上而误判缺乏依据
            # （跟route_agent的_extract_poi_names是同一个问题，见docs/ISSUES.md第19条）。剥离
            # 括号修饰、按/、-拆开别名，只核验纯名称部分。
            #
            # POI详情场景比route_plan复杂一层：point_name经常不是地名，是"动作步骤"标题
            # （如"入馆准备：提前预约+抵达安检"），含"："冒号是强信号——route_plan的pois从
            # 未出现这个格式，只在poi_detail场景真实复现过。这类标题拿去做逐字/别名核对，
            # 原文永远不可能包含这句完整的操作说明，核验从架构上就注定失败，不管内容怎么
            # 改写都通不过——真实证据：`docs/ISSUES.md`第125条记录的甘肃省博物馆
            # "入馆准备：提前预约+抵达安检"这条，3轮重试issue描述几乎逐字重复，不是模型
            # 不肯改，是这个point_name格式本身没法通过核验。含"："时不再把point_name本身
            # 当claimed_entity，只核验highlight/suggested_duration里的实际内容（数字化
            # 可验证事实仍会被下面的_VERIFIABLE_FACT_PATTERN触发档位C，不降低核验强度，
            # 只是不再拿一句操作说明去找"原文有没有逐字提到这句话"这个不可能成立的对应）。
            is_procedural_title = bool(point_name) and ("：" in point_name or ":" in point_name)
            claimed = [] if is_procedural_title else (_extract_poi_names(point_name) if point_name else [])
            items.append(
                {
                    "target_type": "poi_detail_segment",
                    "target_key": item_key,
                    "observation_mode": observation_mode,
                    "label": f"{route_name}.途经点{segment_order}「{point_name}」",
                    "claimed_entities": claimed,
                    "sources": sources,
                    "chunk_ids": [s["chunk_id"] for s in sources if s.get("chunk_id")],
                    # is_procedural_title时claimed_entities刻意留空（point_name不是地名，
                    # 没有可核验的专名），不代表这条item"完全没有引用"——chunk_ids是否非空
                    # 仍然如实反映sources字段，_no_entity_to_verify只放行"没有实体可核对"
                    # 这一种情况，真正没写sources的item依然会被_verify_items判定有问题。
                    "_no_entity_to_verify": is_procedural_title,
                    "_web_verified_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "web_verified"
                    ],
                    "_amap_ids": [
                        s["chunk_id"] for s in sources if s.get("chunk_id") and s.get("platform") == "amap"
                    ],
                    # 含suggested_duration（建议停留时间）——2026-07-22审计发现这个字段此前完全
                    # 没被纳入核验文本，是真实性风险最高的一类字段（可验证的具体数字，跟
                    # docs/ISSUES.md第20条lodging案例的地铁线路号/分钟数同一类别）。
                    # 已知局限：_verify_items是entity触发式设计（claimed_entities字面核对通过
                    # 就直接跳过档位C，见函数第225-226行"都能字面对上，通过，不需要档位B/C"），
                    # suggested_duration不是专有名词、不会被claimed_entities捕获，只有在
                    # point_name本身核验失败触发档位C时才会顺带被content_judge看到——
                    # 这不是结构性修复，只是content_judge真的被触发时能多判断一层，
                    # 真正的防线在prompt.md新增的"可验证具体事实"规则。
                    "content": f"{point_name}：{seg.get('highlight', '')}"
                    + (f"（建议停留{seg.get('suggested_duration')}）" if seg.get("suggested_duration") else ""),
                    "extra": {
                        "route_name": route_name,
                        "order": segment_order,
                        "point_name": point_name,
                    },
                }
            )

    all_items = items + claim_items
    chunk_texts = _merge_web_verified_evidence(
        all_items, chunk_texts, project_id, log_scope, filter_by_entities=False
    )
    chunk_texts = _merge_amap_evidence(all_items, chunk_texts, topic)
    checks: list[dict] = list(manual_checks)
    if project_id:
        if segment_keys is not None:
            replace_prefixes = [
                f"poi_detail:{poi_name}:route:{route_name}:order:{int(order)}"
                for route_name, order in segment_keys
            ]
        elif route_names is not None:
            replace_prefixes = [
                f"poi_detail:{poi_name}:route:{route_name}:" for route_name in route_names
            ]
        else:
            replace_prefixes = [f"poi_detail:{poi_name}:"]
    else:
        replace_prefixes = []
    try:
        issues = _verify_items(
            items,
            chunk_texts,
            result_sink=checks,
            allow_model_calls=allow_model_calls,
        )
        issues.extend(
            _verify_evidence_claim_items(
                claim_items,
                chunk_texts,
                checks,
                allow_model_calls=allow_model_calls,
            )
        )
        return issues
    finally:
        if project_id and persist:
            store.write_evidence_checks(
                project_id,
                "poi_detail",
                checks,
                replace_prefixes=replace_prefixes,
            )
