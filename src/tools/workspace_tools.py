"""公共区读写工具：read_workspace / write_route_plan / write_agent_output /
write_final_guide / report_conflict。

工厂函数 make_workspace_tools(project_id, actor) 只返回对应actor被允许调用的那部分工具，
actor由构建agent的代码固定传入，LLM无法伪造身份去越权调用。

写入类工具的参数用结构化类型（list[dict]/dict），不用"把JSON塞进字符串参数"这种写法——
后者等于让LLM做两层JSON序列化，容易转义出错（真实跑过一次就复现过 JSONDecodeError）。
结构化参数由tool-calling协议自己处理，出错概率低很多；同时每个写入工具都包一层try/except，
校验失败时把具体错误原因返回给agent，让它能看着错误信息重试，而不是直接把整个graph跑崩。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from datetime import datetime, timezone

from langchain_core.tools import tool
from pydantic import BaseModel, ValidationError, field_validator

from src.config import MATERIAL_LIBRARY_DIR
from src.workspace import store
from src.workspace.final_guide_blocks import collect_upstream_claims
from src.workspace.integration_context import integration_delivery_view
from src.workspace.schema import (
    CultureOutput,
    PoiDetailFile,
    RouteEntities,
    RouteExecutionPlan,
    RoutePlan,
)


def _compact_tool_validation_error(error: ValidationError) -> str:
    details = []
    for item in error.errors(include_url=False, include_input=False)[:8]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "参数"
        details.append(f"{location}: {item.get('msg', '格式错误')}")
    suffix = "；错误过多，仅显示前8处" if error.error_count() > 8 else ""
    return "工具参数校验失败：" + "；".join(details) + suffix


def _format_tool_exception(error: Exception) -> str:
    """Keep model-visible write failures actionable without echoing huge payloads."""
    if isinstance(error, ValidationError):
        return _compact_tool_validation_error(error)
    return str(error)


def _install_compact_validation_errors(tools: list) -> list:
    for workspace_tool in tools:
        if workspace_tool.name.startswith("write_"):
            workspace_tool.handle_validation_error = _compact_tool_validation_error
    return tools


def _decode_json_container(value):
    """Recover a structured tool argument accidentally wrapped as JSON text."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        repaired = _repair_unescaped_json_string_quotes(value)
        if repaired == value:
            return value
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return value


def _repair_unescaped_json_string_quotes(value: str) -> str:
    """Escape prose quotes inside an otherwise complete JSON container.

    Some tool-call channels serialize a list as text and forget to escape quotes
    inside long Markdown fields, for example ``路线中"下午4点"可行``. Only a
    quote followed by JSON structural punctuation can close the current string;
    other quotes are treated as prose. The repaired text is still accepted only
    when the standard JSON parser validates the whole container.
    """
    stripped = value.lstrip()
    if not stripped.startswith(("[", "{")):
        return value

    repaired: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(value):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            continue

        lookahead = index + 1
        while lookahead < len(value) and value[lookahead].isspace():
            lookahead += 1
        next_char = value[lookahead] if lookahead < len(value) else ""
        if not next_char or next_char in ",:}]":
            repaired.append(char)
            in_string = False
        else:
            repaired.append('\\"')
    return "".join(repaired)


class _RouteFactsPatchArgs(BaseModel):
    day: int
    notes: str | None = None
    sources: list[dict] | None = None
    claims: list[dict] | None = None

    @field_validator("sources", "claims", mode="before")
    @classmethod
    def decode_json_lists(cls, value):
        return _decode_json_container(value)


class _FinalGuideWriteArgs(BaseModel):
    blocks: list[dict]
    meta: dict

    @field_validator("blocks", "meta", mode="before")
    @classmethod
    def decode_json_containers(cls, value):
        return _decode_json_container(value)


def _looks_like_web_verified_id(value: str) -> bool:
    """判断一个chunk_id/doc_id是不是web_search结果现起的合成id。真实枚举过青甘大环线
    project全部sources后确认的命名变体（模型自己现起字符串，没有代码层强制格式，只能
    枚举真实观察到的模式）：完整关键词"web_verified"、连字符片段"web-XXX"（可能带topic
    前缀，如"web-gansumuseum-2026"/"{topic}-web-gansumuseum-2026"——后者是历史脏数据，
    这条item当初platform就没写对，被旧的"补全topic前缀"逻辑连累又拼了一层前缀，见下方
    说明），或者整个值就是"web"这个词本身（doc_id常见这种写法）。

    不能只用`startswith("web-")`——2026-08-17第133条修复时只测过"青甘大环线-web_verified"
    这一种命名变体（茶卡盐湖POI详情样本），第一版实现（`"web_verified" in combined`）
    覆盖不了`outfit.day8`真实撞到的"web-gansumuseum-2026"这种连字符前缀格式；而这次
    紧接着发现的是更棘手的一种：如果platform字段本来就没写对，_normalize_sources_chunk_id
    的"补topic前缀"那一半逻辑会把它误判成普通素材chunk_id、往前面拼一层topic前缀，
    变成"{topic}-web-gansumuseum-2026"——这时"web-"就不在开头了，startswith检测不到，
    必须用子串匹配"-web-"（前面有连字符分隔）或裸的"web-"开头两种情况都覆盖，才能
    连这种已经被错误处理过一次的历史脏数据也识别出来（详见docs/ISSUES.md第135条）。
    """
    v = (value or "").strip().lower()
    if not v:
        return False
    return (
        v == "web"
        or v == "web_verified"
        or v.startswith("web-")
        or "-web-" in v
        or "web_verified" in v
    )


def _looks_like_amap_id(value: str) -> bool:
    """判断一个chunk_id/doc_id是不是高德实测数据现起的合成id。真实观察到的命名变体
    统一含"amap"关键词（如"{topic}-amap_driving_distance"/"{topic}-amap"），比web的
    情况简单，只有这一种模式。"""
    return "amap" in (value or "").lower()


def _bind_pitfall_targets(project_id: str, items: list[dict]) -> int:
    """按(field, day)把Pitfall目标确定性绑定到当前Lifestyle推荐原文。

    target_recommendation是审计字段，不应依赖模型在长任务末尾重新抄写几十条长文本。
    模型只负责选择正确的field/day并写warning；这里从结构化上游读取权威原文覆盖该字段。
    Lifestyle里不存在的组合直接拒绝，避免生成无法追溯到任何上游推荐的孤立warning。
    """
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
    recommendations = {
        (field, item.get("day")): item.get("recommendation", "")
        for field in ("food", "outfit", "lodging", "play")
        for item in (lifestyle.get(field) or {}).get("items", []) or []
    }
    rebound = 0
    for item in items:
        key = (item.get("field"), item.get("day"))
        if key not in recommendations:
            raise ValueError(
                f"避雷目标{key}在outputs/lifestyle.json中不存在，"
                "只能检查衣食住行已经产出的(field, day)组合"
            )
        canonical = recommendations[key]
        if item.get("target_recommendation") != canonical:
            rebound += 1
        item["target_recommendation"] = canonical
    return rebound


def _normalize_synthetic_platform(sources: list[dict] | None) -> tuple[list[dict], int]:
    """归一化sources里的platform字段：citation_check的_merge_web_verified_evidence/
    _merge_amap_evidence精确匹配platform=="web_verified"/"amap"才会去合并对应的真实
    证据（web_search结果/高德实测数据），但prompt.md里只是用自然语言要求模型"标注
    platform: web_verified"，模型没有格式约束，真实撞到过写成"web"（同一份数据同时
    出现web_verified/web/douyin_asr/bilibili/B站/xiaohongshu等7种platform写法）——
    虽然prompt白纸黑字要求了精确写法，多轮生成压力下模型没有严格遵守（docs/ISSUES.md
    第133条真实案例：茶卡盐湖POI详情有7条sources的chunk_id/doc_id是合成的
    "{topic}-web_verified"格式，但platform字段被写成了"web"，导致这些条目的真实web
    核实内容完全没被citation_check合并进combined_text，被误判为"原文片段为空"）。

    这里用chunk_id/doc_id本身的格式特征做纠正（`_looks_like_web_verified_id`/
    `_looks_like_amap_id`，见这两个函数关于命名变体的完整枚举说明）——比要求模型自己
    把platform这个自由文本字段填对更可靠：只要chunk_id/doc_id命中已知的合成id模式，
    就能确定性地推断这条来源本来就该是哪一类合成证据，不需要猜测模型的意图。只处理
    命中已知模式、且platform还没写对的情况，不改动其它任何platform值（真实的素材
    平台名如douyin/bilibili/B站/xiaohongshu不受影响，即使写法不统一也不是这个函数
    的职责）。
    """
    if not sources:
        return sources or [], 0
    fixed = 0
    normalized = []
    for s in sources:
        s = dict(s)
        chunk_id = s.get("chunk_id") or ""
        doc_id = s.get("doc_id") or ""
        # append_material historically returned only its disk filename
        # ``evidence-{note_id}.json``. Agents copied that stem into chunk_id
        # even though the citable id exposed by retrieve_project_evidence is
        # ``project-evidence-{note_id}``. The combination of project scope and
        # this exact legacy prefix is deterministic, so repair it at the write
        # boundary instead of forcing the agent to resubmit a large POI file
        # once for every affected claim.
        legacy_project_id = ""
        for candidate in (str(chunk_id), str(doc_id)):
            if candidate.startswith("evidence-"):
                legacy_project_id = candidate.removeprefix("evidence-").removesuffix(".json")
                break
        if s.get("scope") == "project" and legacy_project_id:
            canonical_chunk_id = f"project-evidence-{legacy_project_id}"
            if s.get("platform") != "project_evidence":
                s["platform"] = "project_evidence"
                fixed += 1
            if s.get("doc_id") != legacy_project_id:
                s["doc_id"] = legacy_project_id
                fixed += 1
            if s.get("chunk_id") != canonical_chunk_id:
                s["chunk_id"] = canonical_chunk_id
                fixed += 1
            normalized.append(s)
            continue
        # Models have also emitted the semantically correct but non-canonical
        # `platform="web"`, sometimes with only a descriptive doc_id and no
        # chunk_id at all. Treat that shape as runtime web evidence too. Without
        # this fallback, the evidence log cannot be merged during citation checks
        # and the reference table reports a false "原文未找到" warning.
        platform = str(s.get("platform") or "").strip().lower()
        is_web_source = platform in {"web", "web_verified"} or _looks_like_web_verified_id(chunk_id) or _looks_like_web_verified_id(doc_id)
        if is_web_source:
            if s.get("platform") != "web_verified":
                s["platform"] = "web_verified"
                fixed += 1
            if not s.get("chunk_id"):
                seed = str(doc_id or s.get("author") or "web-source")
                digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
                s["chunk_id"] = f"web-{digest}"
                fixed += 1
        elif _looks_like_amap_id(chunk_id) or _looks_like_amap_id(doc_id):
            if s.get("platform") != "amap":
                s["platform"] = "amap"
                fixed += 1
        normalized.append(s)
    return normalized, fixed


def _source_ref_from_chunk_id(
    value: str,
    topic: str,
    project_id: str | None = None,
) -> dict:
    """Recover a SourceRef when a nested claim emits only its chunk_id."""
    chunk_id = str(value or "").strip()
    if not chunk_id:
        raise ValueError("evidence_refs中的chunk_id字符串不能为空")

    if chunk_id.startswith("project-evidence-"):
        note_id = chunk_id.removeprefix("project-evidence-")
        source = {
            "platform": "project_evidence",
            "doc_id": note_id,
            "chunk_id": chunk_id,
            "scope": "project",
        }
        if project_id:
            from src.materials.project_evidence import list_project_evidence

            evidence = next(
                (item for item in list_project_evidence(project_id) if item.note_id == note_id),
                None,
            )
            if evidence is not None:
                source["author"] = evidence.source_platform
                source["source_url"] = evidence.source_url
        return source

    if chunk_id.startswith("amap-evidence-") or _looks_like_amap_id(chunk_id):
        return {"platform": "amap", "chunk_id": chunk_id}
    if _looks_like_web_verified_id(chunk_id):
        return {"platform": "web_verified", "chunk_id": chunk_id}

    prefix = f"{topic}-" if topic else ""
    canonical_chunk_id = (
        chunk_id if not prefix or chunk_id.startswith(prefix) else prefix + chunk_id
    )
    doc_id = canonical_chunk_id.rsplit("-c", 1)[0] if "-c" in canonical_chunk_id else ""
    source = {
        "platform": "material",
        "doc_id": doc_id,
        "chunk_id": canonical_chunk_id,
        "scope": "global_core",
    }
    entries_path = MATERIAL_LIBRARY_DIR / "index" / topic / "entries.jsonl"
    if doc_id and entries_path.exists():
        for line in entries_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if str(entry.get("doc_id") or "") != doc_id:
                continue
            source.update(
                {
                    "platform": str(entry.get("platform") or "material"),
                    "author": str(entry.get("author") or ""),
                    "source_url": str(entry.get("source_url") or ""),
                }
            )
            break
    return source


def _normalize_sources_chunk_id(
    sources: list[dict | str] | None,
    topic: str,
    project_id: str | None = None,
) -> tuple[list[dict], int]:
    """归一化sources：先修正platform字段（_normalize_synthetic_platform，见该函数说明），
    再补全chunk_id/doc_id的topic前缀——两步都是"模型输出格式跟核验代码期望的精确格式
    对不上"这一类问题的确定性修复，合并成一个入口，调用方不需要关心内部分了几步。

    chunk_id前缀补全：多轮citation_check打回重试压力下，模型有时会把自己此前写对的、
    带完整topic前缀的chunk_id（如"青甘大环线-144-c004"）在下一轮重新提交时弄丢前缀，
    退化成裸格式（如"144-c004"）——真实trace证据见docs/ISSUES.md第116条，检索工具
    返回给模型的结果100%带前缀，问题只出现在模型自己回填sources的时候。

    裸格式chunk_id会让citation_check.load_chunk_texts()反查chunks.jsonl时查不到原文，
    combined_text变成空字符串，导致这条本来有真实依据的内容被误判为"原文片段为空"而
    假阳性打回，白白消耗MAX_RETRY次数。在写入这一刻就补全前缀，从源头堵住这个退化——
    不依赖citation_check那边的防御性归一化兜底（那是最后一道防线，见_verify_items）。

    只补前缀，不校验补全后chunk_id是否真实存在——真伪判断是citation_check的职责，这里
    只负责"看起来像是丢了前缀的裸格式"这一种确定性的格式退化，不做更激进的猜测。
    platform=="web_verified"/"amap"的条目chunk_id是合成字符串（web_search结果/高德
    实测数据现起的，如"web-XXX-YYY"/"{topic}-amap_driving_distance"），不属于topic
    素材库索引体系，不做这个归一化。
    """
    if not sources:
        return sources or [], 0
    coerced_sources: list[dict] = []
    coerced_count = 0
    for source in sources:
        if isinstance(source, dict):
            coerced_sources.append(source)
        elif isinstance(source, str):
            coerced_sources.append(
                _source_ref_from_chunk_id(source, topic, project_id=project_id)
            )
            coerced_count += 1
        else:
            raise ValueError(
                "evidence_refs每一项必须是SourceRef对象或chunk_id字符串，"
                f"不能是{type(source).__name__}"
            )
    sources, platform_fixed = _normalize_synthetic_platform(coerced_sources)
    if not topic:
        return sources, platform_fixed
    prefix = f"{topic}-"
    fixed = platform_fixed + coerced_count
    normalized = []
    for s in sources:
        s = dict(s)
        is_project_evidence = (
            s.get("platform") == "project_evidence"
            or s.get("scope") == "project"
            or str(s.get("chunk_id") or "").startswith("project-evidence-")
        )
        if s.get("platform") not in ("web_verified", "amap") and not is_project_evidence:
            cid = s.get("chunk_id") or ""
            if cid and not cid.startswith(prefix):
                s["chunk_id"] = prefix + cid
                fixed += 1
            doc_id = s.get("doc_id") or ""
            if doc_id and not doc_id.startswith(prefix):
                s["doc_id"] = prefix + doc_id
        normalized.append(s)
    return normalized, fixed


def _chunk_id_fix_note(fixed: int, topic: str) -> str:
    if not fixed:
        return ""
    return (
        f"（已自动修正{fixed}处来源引用格式或chunk_id前缀——下次请优先提交"
        f"完整SourceRef，并使用完整chunk_id，如'{topic}-144-c004'）"
    )


def _route_claim_id(day: int, claim: dict) -> str:
    seed = "|".join(
        [
            str(day),
            str(claim.get("target_path") or "notes").strip(),
            str(claim.get("claim_type") or "other").strip(),
            str(claim.get("claim_text") or "").strip(),
        ]
    )
    return f"route-claim-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"


def _evidence_claim_id(
    namespace: str,
    owner_parts: list[object],
    claim: dict,
    default_target_path: str,
) -> str:
    seed = "|".join(
        [
            *(str(part) for part in owner_parts),
            str(claim.get("target_path") or default_target_path).strip(),
            str(claim.get("claim_type") or "other").strip(),
            str(claim.get("claim_text") or "").strip(),
        ]
    )
    return f"{namespace}-claim-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"


def _source_key(source: dict) -> tuple[str, str, str, str]:
    return (
        str(source.get("platform") or ""),
        str(source.get("doc_id") or ""),
        str(source.get("chunk_id") or ""),
        str(source.get("source_url") or ""),
    )


def _claim_snapshot(
    project_id: str,
    topic: str,
    claim: dict,
    source: dict,
    chunk_texts: dict[str, str],
    log_scope: str = "route_agent",
) -> dict:
    # Local import avoids coupling citation_check's module initialization to agent tools.
    from src.materials import citation_check

    chunk_id = str(source.get("chunk_id") or "")
    platform = str(source.get("platform") or "")
    text = chunk_texts.get(chunk_id, "")
    source_kind = "material"
    if chunk_id.startswith("project-evidence-") or platform == "project_evidence":
        source_kind = "project_web"
    elif platform == "web_verified":
        source_kind = "runtime_web"
        text = citation_check._load_web_search_evidence(
            project_id,
            log_scope,
            claimed_entities=claim.get("claimed_entities") or ([claim.get("subject")] if claim.get("subject") else None),
        )
    elif platform == "amap":
        source_kind = "amap"
        text = citation_check._load_amap_logged_evidence(
            project_id, log_scope, [chunk_id]
        ).get(chunk_id, "")
        if not text:
            text = citation_check._load_amap_evidence(topic, str(claim.get("claim_text") or ""))

    terms = claim.get("claimed_entities") or ([claim.get("subject")] if claim.get("subject") else [])
    excerpt = citation_check._evidence_excerpt(text, [str(term) for term in terms if term]) if text else ""
    return {
        "source": source,
        "excerpt": excerpt,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_kind": source_kind if excerpt else "unknown",
    }


def _prepare_evidence_claims(
    project_id: str,
    topic: str,
    claims: list[dict] | None,
    *,
    namespace: str,
    owner_parts: list[object],
    required_paths: list[str],
    default_target_path: str,
    log_scope: str,
) -> tuple[list[dict], list[dict], list[str], int]:
    """Normalize a claim set and derive compatibility source/entity unions."""
    raw_claims = claims or []
    if required_paths and not raw_claims:
        raise ValueError(
            "必须提交claims；新写入不能只保留对象级sources"
        )

    from src.materials import citation_check

    chunk_texts = citation_check.load_chunk_texts(topic, project_id)
    fixed_total = 0
    normalized_claims: list[dict] = []
    union_sources: list[dict] = []
    union_entities: list[str] = []
    seen_sources: set[tuple[str, str, str, str]] = set()

    for raw_claim in raw_claims:
        claim = dict(raw_claim)
        claim_text = str(claim.get("claim_text") or "").strip()
        if not claim_text:
            raise ValueError("claims[].claim_text不能为空")
        claim["claim_text"] = claim_text
        claim["target_path"] = str(
            claim.get("target_path") or default_target_path
        ).strip()
        claim["claim_id"] = _evidence_claim_id(
            namespace, owner_parts, claim, default_target_path
        )
        refs, fixed = _normalize_sources_chunk_id(
            claim.get("evidence_refs"), topic, project_id=project_id
        )
        fixed_total += fixed
        claim["evidence_refs"] = refs

        raw_support_mode = claim.get("support_mode")
        if raw_support_mode is None or not str(raw_support_mode).strip():
            raise ValueError(
                f"事实“{claim_text}”必须显式填写support_mode；"
                "请根据内容性质选择direct/amap/derived/estimate/"
                "user_constraint/unverified，写入层不会默认猜成direct"
            )
        support_mode = str(raw_support_mode).strip()
        claim["support_mode"] = support_mode
        if support_mode in {"direct", "amap"} and not refs:
            raise ValueError(
                f"事实“{claim_text}”标记为{support_mode}，但没有evidence_refs；"
                "请补真实引用，或改成estimate/unverified"
            )
        if support_mode == "amap" and any(
            source.get("platform") != "amap"
            or not str(source.get("chunk_id") or "").startswith("amap-evidence-")
            for source in refs
        ):
            raise ValueError(
                f"高德事实“{claim_text}”只能引用amap_driving_distance返回的"
                "amap-evidence-*来源"
            )
        if (
            support_mode == "direct"
            and str(claim.get("provenance_mode") or "live") == "live"
            and any(source.get("platform") == "web_verified" for source in refs)
        ):
            raise ValueError(
                f"Web事实“{claim_text}”必须先用append_material固化成"
                "project-evidence-*再引用；笼统web_verified日志不能作为live事实的精确依据"
            )

        snapshots = [
            _claim_snapshot(
                project_id,
                topic,
                claim,
                source,
                chunk_texts,
                log_scope=log_scope,
            )
            for source in refs
        ]
        if support_mode in {"direct", "amap"} and not any(
            item.get("excerpt") for item in snapshots
        ):
            raise ValueError(
                f"事实“{claim_text}”的引用无法读取原文；"
                "请使用真实chunk、project-evidence或高德来源"
            )
        claim["evidence_snapshots"] = snapshots
        normalized_claims.append(claim)

        for entity in claim.get("claimed_entities") or []:
            entity = str(entity).strip()
            if entity and entity not in union_entities:
                union_entities.append(entity)
        for source in refs:
            key = _source_key(source)
            if key not in seen_sources:
                seen_sources.add(key)
                union_sources.append(source)

    claim_paths = {str(claim.get("target_path") or "") for claim in normalized_claims}
    missing_paths = [path for path in required_paths if path not in claim_paths]
    if missing_paths:
        raise ValueError(
            "以下内容没有事实级依据链："
            + "、".join(missing_paths)
            + "；请为每个位置至少提交一条claim"
        )

    ids = {claim["claim_id"] for claim in normalized_claims}
    text_to_id = {claim["claim_text"]: claim["claim_id"] for claim in normalized_claims}
    for claim in normalized_claims:
        resolved_dependencies: list[str] = []
        for dependency in claim.get("derived_from") or []:
            dependency_id = dependency if dependency in ids else text_to_id.get(str(dependency), "")
            if dependency_id and dependency_id not in resolved_dependencies:
                resolved_dependencies.append(dependency_id)
        claim["derived_from"] = resolved_dependencies
        if claim.get("support_mode") == "derived" and not resolved_dependencies:
            raise ValueError(
                f"推导事实“{claim['claim_text']}”的derived_from必须填写同一个item内"
                "另一条claim的完整claim_text或claim_id；如果只是路线推断、一般建议或"
                "用户偏好，请改用estimate/user_constraint，或补充可直接引用的证据"
            )

    return normalized_claims, union_sources, union_entities, fixed_total


def _prepare_route_day_claims(project_id: str, topic: str, day_item: dict) -> int:
    """Normalize claim refs, assign stable IDs, snapshot evidence, and derive day sources."""
    claims = day_item.get("claims") or []
    if not claims:
        raise ValueError(
            f"Day{day_item.get('day')}必须提交claims；路线新写入不能只保留天级sources"
        )

    from src.materials import citation_check

    chunk_texts = citation_check.load_chunk_texts(topic, project_id)
    fixed_total = 0
    normalized_claims: list[dict] = []
    union_sources: list[dict] = []
    seen_sources: set[tuple[str, str, str, str]] = set()

    for raw_claim in claims:
        claim = dict(raw_claim)
        claim_text = str(claim.get("claim_text") or "").strip()
        if not claim_text:
            raise ValueError("claims[].claim_text不能为空")
        claim["claim_text"] = claim_text
        target_path = str(claim.get("target_path") or "notes").strip()
        target_parts = target_path.split(".")
        if (
            len(target_parts) == 2
            and target_parts[0] == "pois"
            and target_parts[1].isdigit()
        ):
            target_path = f"{target_path}.name"
            fixed_total += 1
        claim["target_path"] = target_path
        poi_name = ""
        poi_name_match = re.fullmatch(r"pois\.(\d+)\.name", target_path)
        if poi_name_match:
            poi_index = int(poi_name_match.group(1))
            pois = day_item.get("pois") or []
            if poi_index < len(pois) and isinstance(pois[poi_index], dict):
                poi_name = str(pois[poi_index].get("name") or "").strip()
            claim_type = str(claim.get("claim_type") or "").strip()
            compact_claim = re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】]+", "", claim_text)
            compact_name = re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】]+", "", poi_name)
            if not claim_type:
                claim_type = "poi_identity" if compact_claim == compact_name else "poi_selection"
                claim["claim_type"] = claim_type
                fixed_total += 1
            if claim_type == "poi_selection" and compact_claim == compact_name:
                raise ValueError(
                    f"Day{day_item.get('day')}.{target_path}的poi_selection不能只写POI名称；"
                    "请写出为什么选择它、它满足什么需求或它在路线中的作用"
                )
            if claim_type == "poi_identity" and poi_name:
                entities = [
                    str(item).strip()
                    for item in claim.get("claimed_entities") or []
                    if str(item).strip()
                ]
                if poi_name not in entities:
                    entities.append(poi_name)
                    claim["claimed_entities"] = entities
                    fixed_total += 1
        claim["claim_id"] = _route_claim_id(int(day_item.get("day") or 0), claim)
        refs, fixed = _normalize_sources_chunk_id(
            claim.get("evidence_refs"), topic, project_id=project_id
        )
        fixed_total += fixed
        claim["evidence_refs"] = refs

        raw_support_mode = claim.get("support_mode")
        if raw_support_mode is None or not str(raw_support_mode).strip():
            raise ValueError(
                f"事实“{claim_text}”必须显式填写support_mode；"
                "请根据内容性质选择direct/amap/derived/estimate/"
                "user_constraint/unverified，写入层不会默认猜成direct"
            )
        support_mode = str(raw_support_mode).strip()
        claim["support_mode"] = support_mode
        if support_mode in {"direct", "amap"} and not refs:
            raise ValueError(
                f"事实“{claim_text}”标记为{support_mode}，但没有evidence_refs；"
                "请补真实引用，或改成estimate/unverified"
            )
        if support_mode == "amap" and any(
            source.get("platform") != "amap"
            or not str(source.get("chunk_id") or "").startswith("amap-evidence-")
            for source in refs
        ):
            raise ValueError(
                f"高德事实“{claim_text}”只能引用amap_driving_distance本次返回的"
                "amap-evidence-*来源"
            )
        if (
            support_mode == "direct"
            and str(claim.get("provenance_mode") or "live") == "live"
            and any(source.get("platform") == "web_verified" for source in refs)
        ):
            raise ValueError(
                f"Web事实“{claim_text}”必须先用append_material固化成project-evidence-*再引用；"
                "笼统web_verified日志不能作为live事实的精确依据"
            )

        snapshots = [
            _claim_snapshot(project_id, topic, claim, source, chunk_texts)
            for source in refs
        ]
        if support_mode in {"direct", "amap"} and not any(item.get("excerpt") for item in snapshots):
            raise ValueError(
                f"事实“{claim_text}”的引用无法读取原文；请使用真实chunk、project-evidence或高德来源"
            )
        claim["evidence_snapshots"] = snapshots
        normalized_claims.append(claim)
        for source in refs:
            key = _source_key(source)
            if key not in seen_sources:
                seen_sources.add(key)
                union_sources.append(source)

    claim_paths = {str(claim.get("target_path") or "") for claim in normalized_claims}
    required_paths: list[str] = []
    if str(day_item.get("notes") or "").strip():
        required_paths.append("notes")
    for index, poi in enumerate(day_item.get("pois") or []):
        if not isinstance(poi, dict):
            continue
        poi_name = str(poi.get("name") or "").strip()
        if poi_name:
            required_paths.append(f"pois.{index}.name")
        if str(poi.get("brief") or "").strip():
            required_paths.append(f"pois.{index}.brief")
        if str(poi.get("needs_detail_reason") or "").strip():
            required_paths.append(f"pois.{index}.needs_detail_reason")
    missing_paths = [path for path in required_paths if path not in claim_paths]
    if missing_paths:
        day_number = day_item.get("day")
        missing_labels = []
        for path in missing_paths:
            location = f"Day{day_number}.{path}"
            parts = path.split(".")
            poi_suffix = ""
            if len(parts) >= 3 and parts[0] == "pois" and parts[1].isdigit():
                poi_index = int(parts[1])
                pois = day_item.get("pois") or []
                if poi_index < len(pois) and isinstance(pois[poi_index], dict):
                    poi_name = str(pois[poi_index].get("name") or "").strip()
                    if poi_name:
                        poi_suffix = f"（{poi_name}）"
            missing_labels.append(
                f'{location}{poi_suffix}缺少claim，应填写target_path="{path}"'
            )
        raise ValueError(
            "以下路线内容没有事实级依据链："
            + "、".join(missing_labels)
            + "；target_path必须使用当前Day内部的局部路径，"
            "不要添加Day1.或days[0].等前缀；每个非空位置都需要独立claim"
        )

    ids = {claim["claim_id"] for claim in normalized_claims}
    text_to_id = {claim["claim_text"]: claim["claim_id"] for claim in normalized_claims}
    for claim in normalized_claims:
        resolved_dependencies = []
        for dependency in claim.get("derived_from") or []:
            dependency_id = dependency if dependency in ids else text_to_id.get(str(dependency), "")
            if dependency_id and dependency_id not in resolved_dependencies:
                resolved_dependencies.append(dependency_id)
        claim["derived_from"] = resolved_dependencies
        if claim.get("support_mode") == "derived" and not resolved_dependencies:
            raise ValueError(f"推导事实“{claim['claim_text']}”必须通过derived_from关联本日其他事实")

    day_item["claims"] = normalized_claims
    day_item["sources"] = union_sources
    return fixed_total


def _normalize_route_segments_chunk_id(routes: list[dict], topic: str) -> int:
    """跟_normalize_sources_chunk_id同一个问题，只是数据结构多套了一层
    routes -> route_segments -> sources（POI详情agent的产出结构）。"""
    fixed_total = 0
    for route in routes:
        for seg in route.get("route_segments", []) or []:
            seg["sources"], fixed = _normalize_sources_chunk_id(seg.get("sources"), topic)
            fixed_total += fixed
    return fixed_total


def _prepare_lifestyle_item_claims(
    project_id: str, topic: str, item: dict
) -> int:
    normalized, sources, entities, fixed = _prepare_evidence_claims(
        project_id,
        topic,
        item.get("claims"),
        namespace="lifestyle",
        owner_parts=[item.get("field"), item.get("day")],
        required_paths=["recommendation"] if str(item.get("recommendation") or "").strip() else [],
        default_target_path="recommendation",
        log_scope="lifestyle_agent",
    )
    item["claims"] = normalized
    item["sources"] = sources
    item["claimed_entities"] = entities
    return fixed


def _prepare_pitfall_item_claims(
    project_id: str, topic: str, item: dict
) -> int:
    if not str(item.get("warning") or "").strip():
        item["claims"] = []
        item["sources"] = []
        item["claimed_entities"] = []
        return 0
    normalized, sources, entities, fixed = _prepare_evidence_claims(
        project_id,
        topic,
        item.get("claims"),
        namespace="pitfall",
        owner_parts=[item.get("field"), item.get("day")],
        required_paths=["warning"],
        default_target_path="warning",
        log_scope="pitfall_agent",
    )
    item["claims"] = normalized
    item["sources"] = sources
    item["claimed_entities"] = entities
    return fixed


def _prepare_route_segment_claims(
    project_id: str,
    topic: str,
    routes: list[dict],
    *,
    day: int,
    poi_name: str,
) -> int:
    prepared_routes = deepcopy(routes)
    fixed_total = 0
    validation_errors: list[str] = []
    log_scope = f"poi_detail_agent/day{day}_{store.poi_slug(poi_name)}"
    for route in prepared_routes:
        route_name = str(route.get("route_name") or "")
        for segment in route.get("route_segments", []) or []:
            # Tool schemas often describe string enums with ``a|b|c``. Some
            # models copy that notation literally when one fact supports more
            # than one segment field. Normalize the shorthand into independent
            # claims so the stored target_path remains an exact, queryable path.
            expanded_claims: list[dict] = []
            for raw_claim in segment.get("claims") or []:
                claim = dict(raw_claim)
                raw_path = str(claim.get("target_path") or "").strip()
                paths = [path.strip() for path in raw_path.split("|") if path.strip()]
                if len(paths) > 1 and all(
                    path in {"point_name", "highlight", "suggested_duration"}
                    for path in paths
                ):
                    for path in paths:
                        expanded = deepcopy(claim)
                        expanded["target_path"] = path
                        expanded_claims.append(expanded)
                else:
                    expanded_claims.append(claim)
            segment["claims"] = expanded_claims
            required_paths = [
                field
                for field in ("point_name", "highlight", "suggested_duration")
                if str(segment.get(field) or "").strip()
            ]
            try:
                normalized, sources, entities, fixed = _prepare_evidence_claims(
                    project_id,
                    topic,
                    segment.get("claims"),
                    namespace="poi",
                    owner_parts=[day, poi_name, route_name, segment.get("order")],
                    required_paths=required_paths,
                    default_target_path="highlight",
                    log_scope=log_scope,
                )
                segment["claims"] = normalized
                segment["sources"] = sources
                # RouteSegment does not persist claimed_entities; the union remains
                # available on individual claims and is consumed by the checker.
                fixed_total += fixed
            except Exception as exc:  # noqa: BLE001 - collect every segment error in one response
                point_name = str(segment.get("point_name") or "未命名途经点")
                validation_errors.append(
                    f"{route_name or '未命名路线'}.segment{segment.get('order')}({point_name}): {exc}"
                )
    if validation_errors:
        raise ValueError(
            f"POI路线共发现{len(validation_errors)}处参数问题："
            + "；".join(validation_errors)
        )
    routes[:] = prepared_routes
    return fixed_total


def make_workspace_tools(
    project_id: str,
    actor: str,
    day: int | None = None,
    poi_name: str | None = None,
    topic: str | None = None,
    route_write_mode: str = "full",
    route_proposal_work_unit_id: str = "",
) -> list:
    tools = []

    @tool
    def read_workspace(relative_path: str, day: int | None = None) -> str:
        """读取当前项目公共区的某个文件，如 'route_plan.json'、'outputs/lifestyle.json'。

        读取route_plan.json时可传day只返回该天的完整Day对象；需要修改已知受影响天时优先使用窄读取，
        不要反复读取整份长路线。
        """
        data = store.read_workspace(project_id, relative_path)
        if data is None:
            return f"文件不存在: {relative_path}"
        if day is not None:
            if relative_path != "route_plan.json" or not isinstance(data, dict):
                return "day筛选仅支持route_plan.json"
            selected = next(
                (
                    item
                    for item in (data.get("days") or [])
                    if isinstance(item, dict) and item.get("day") == day
                ),
                None,
            )
            if selected is None:
                return f"route_plan.json中不存在Day{day}"
            data = selected
        if actor == "integration_agent":
            route_plan = store.read_workspace(project_id, "route_plan.json")
            lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json")
            pitfall = store.read_workspace(project_id, "outputs/pitfall.json")
            poi_details = [
                detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)
            ]
            deliverable_claim_ids = set(
                collect_upstream_claims(
                    route_plan,
                    lifestyle,
                    pitfall,
                    poi_details,
                    evidence_checks=store.read_evidence_checks(project_id),
                    review_findings=store.read_workspace(project_id, "review_findings.jsonl") or [],
                )
            )
            data = integration_delivery_view(
                relative_path,
                data,
                deliverable_claim_ids=deliverable_claim_ids,
                route_plan=route_plan,
            )
        return json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)

    tools.append(read_workspace)

    if actor == "route_agent" and route_write_mode == "full":

        @tool
        def write_route_plan(days: list[dict], amap_verified: bool = False) -> str:
            """整份覆盖写入路线规划区，用于首次生成骨架，或用户明确要求「加一天」/「减少一天」这类
            涉及增删/重排天数的结构性调整——这类调整需要看到整体结构才能判断插入位置/重新排序，
            必须传完整的days数组（未涉及的天数内容也要传，保持逐字一致）。
            如果只是修改某一天已有的内容、不涉及增删天数，请改用write_route_plan_patch，
            不需要重新传一遍其它天数。
            days是一个数组，每项是一个对象，包含 day(整数)/city/area/notes 字段，
            以及pois(数组，每项是一个对象 {"name":"景点名","brief":"一句话说明，比纯地名多一点信息量但不到深度玩法",
            "needs_detail":布尔值,"needs_detail_reason":"判断理由"})——needs_detail标注这个点是否本身构成
            一次独立、有明确路线/途经点概念的游玩体验（如山岳景区/古镇/大型园林），区别于奶茶店这类普通打卡点，
            true的点后续会自动生成深度玩法路线，请按语义判断，不要什么都标true或都标false；
            以及claims(数组)：把notes/brief中的关键事实逐条写成claim_text，并提供target_path、subject、
            claim_type、support_mode、claimed_entities、evidence_refs、derived_from。direct/amap事实必须提供
            真实evidence_refs；程序会生成claim_id、读取原文快照，并由全部claim引用自动汇总当天sources，
            不需要重复手写day.sources。target_path必须是当前Day内部的局部路径，例如notes、pois.0.name、
            pois.0.brief；不能写Day1.notes或days[0].notes。没有依据的事实必须标estimate或unverified，
            不能伪装成direct。"""
            try:
                if not days:
                    return "写入失败：days不能为空"
                fixed_total = 0
                for day_item in days:
                    fixed = _prepare_route_day_claims(project_id, topic or "", day_item)
                    fixed_total += fixed
                route_plan = RoutePlan(days=days, amap_verified=amap_verified)
                store.write_route_plan(project_id, actor, route_plan)
                return f"路线规划已写入 route_plan.json{_chunk_id_fix_note(fixed_total, topic)}"
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_route_plan)

        @tool
        def write_route_plan_patch(days: list[dict], amap_verified: bool | None = None) -> str:
            """新增或修改路线规划区里某一天/某几天的内容，未涉及的天数保持原样不受影响、
            不需要重新传一遍。只适用于"改内容"——如果用户要求「加一天」/「减少一天」这类
            增删天数的结构性调整，请改用write_route_plan（整份覆盖，需要传完整days数组）。
            days是一个数组，每项是一天的完整内容，格式跟write_route_plan的days参数完全一样
            （day/city/area/notes/pois/claims），只是这里只需要传你实际新增/修改的那几天。
            amap_verified不传时保留原值不变，只有确实用amap_driving_distance重新核实过
            距离/时长才需要显式传true。"""
            try:
                if not days:
                    return "写入失败：days不能为空"
                fixed_total = 0
                for day_item in days:
                    fixed = _prepare_route_day_claims(project_id, topic or "", day_item)
                    fixed_total += fixed
                store.write_route_plan_patch(project_id, actor, days, amap_verified=amap_verified)
                return f"路线规划已更新 route_plan.json{_chunk_id_fix_note(fixed_total, topic)}"
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_route_plan_patch)

        @tool
        def write_route_entities(entities: list[dict]) -> str:
            """写入当前正式路线中每个POI已经消歧的provider实体。

            formal_name必须保持route_plan里的原始名称；provider_query/provider_name可使用地图规范名。
            resolved实体必须提供provider_id和location。ambiguous、unresolved或复合名称应原样保留，
            不得为了继续测距而擅自选取低置信度候选。
            """
            try:
                payload = RouteEntities(entities=entities)
                saved = store.write_route_entities(project_id, actor, payload)
                return f"路线实体已写入 route_entities.json（{len(saved.entities)}项）"
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_route_entities)

        @tool
        def write_route_execution(
            route_legs: list[dict],
            operational_commitments: list[dict] | None = None,
        ) -> str:
            """写入路段交通方式和跨日预约/购票/接驳等执行措施。

            每个相邻POI路段应声明transport_mode；mixed必须拆分segments。预约等措施通过
            target_day+target_poi_index绑定目标，允许planned_on_day早于applies_to_day。
            """
            try:
                payload = RouteExecutionPlan(
                    route_legs=route_legs,
                    operational_commitments=operational_commitments or [],
                )
                saved = store.write_route_execution(project_id, actor, payload)
                return (
                    "路线执行计划已写入 route_execution.json"
                    f"（{len(saved.route_legs)}路段，{len(saved.operational_commitments)}项措施）"
                )
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_route_execution)

    if actor == "route_agent" and route_write_mode == "facts_only":

        @tool(args_schema=_RouteFactsPatchArgs)
        def write_route_facts_patch(
            day: int,
            notes: str | None = None,
            sources: list[dict] | None = None,
            claims: list[dict] | None = None,
        ) -> str:
            """只修复既有某一天的事实说明或引用。

            只能修改notes、sources和claims；不能修改day/city/area/pois/needs_detail，也不能新增、
            删除或重排天数。已经启用claims的天修改notes时必须同步提交完整claims。提交claims后
            sources会由claims里的evidence_refs自动汇总。引用核验、普通时效核验的自动修复必须使用此工具。
            """
            try:
                fixed_total = 0
                normalized_sources = sources
                normalized_claims = claims
                if claims is not None:
                    claim_day = {"day": day, "claims": claims}
                    fixed_total = _prepare_route_day_claims(project_id, topic or "", claim_day)
                    normalized_claims = claim_day["claims"]
                    normalized_sources = claim_day["sources"]
                elif sources is not None:
                    normalized_sources, fixed_total = _normalize_sources_chunk_id(
                        sources, topic, project_id=project_id
                    )
                store.write_route_facts_patch(
                    project_id,
                    actor,
                    day,
                    notes=notes,
                    sources=normalized_sources,
                    claims=normalized_claims,
                )
                return f"Day{day}事实层已更新{_chunk_id_fix_note(fixed_total, topic)}"
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_route_facts_patch)

    if actor == "route_agent" and route_write_mode == "proposal_only":

        @tool
        def write_route_change_proposal(
            changed_days: list[dict],
            trigger: str,
            reason: str,
            evidence: str = "",
        ) -> str:
            """提交路线结构调整候选，不直接修改正式route_plan.json。

            changed_days只传确实需要替换的既有天，每项是该天完整内容。系统会把候选与当前
            路线合并并等待用户专门确认；不能借此新增/删除天数。
            """
            try:
                fixed_total = 0
                for day_item in changed_days:
                    fixed = _prepare_route_day_claims(project_id, topic or "", day_item)
                    fixed_total += fixed
                proposal = store.write_route_change_proposal(
                    project_id,
                    actor,
                    changed_days=changed_days,
                    trigger=trigger,
                    reason=reason,
                    evidence=evidence,
                    work_unit_id=route_proposal_work_unit_id,
                )
                return (
                    f"路线结构调整提案已保存，proposal_id={proposal['proposal_id']}，"
                    f"正式路线尚未修改{_chunk_id_fix_note(fixed_total, topic)}"
                )
            except Exception as e:
                return f"提案写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_route_change_proposal)

    if actor == "lifestyle_agent":

        @tool
        def write_agent_output(items: list[dict]) -> str:
            """新增或修改衣食住行产出的具体条目。items是列表，每一项形如：
            {"field": "food"|"outfit"|"lodging"|"play", "day": 2, "recommendation": "...",
             "claims": [{"target_path":"recommendation","claim_text":"一条可独立核验的说法",
             "claim_type":"food|weather|lodging|play|other","support_mode":"direct|amap|derived|estimate|user_constraint|unverified",
             "claimed_entities":["具体店名/菜名"],"evidence_refs":[{"platform":"...","doc_id":"...","chunk_id":"..."}],
             "derived_from":[]}]}。derived只用于同一item内事实之间的推导，derived_from必须填写
            另一条claim的完整claim_text或claim_id；仅根据路线、一般经验或用户要求形成的建议，
            应分别使用estimate或user_constraint。程序会生成claim_id和证据快照，并由claims自动汇总sources/
            claimed_entities，不需要重复手写对象级sources。direct/amap必须有可读真实依据；
            一般经验要标estimate，没有依据的具体说法标unverified。不要把“景点当前开放”
            和“适合作为当日收尾”等外部事实与路线编排写在同一条direct claim中。
            只需要传本次新增或修改的条目——同一(field, day)组合会覆盖旧内容，
            未提及的(field, day)保持原样不受影响，不需要重新传一遍。
            首次生成由编排层按天分配工作单元：每个工作单元只提交目标天的四个字段；
            编辑已有产出时只传被要求修改的那几条。"""
            try:
                if not items:
                    return "写入失败：items不能为空"
                prepared_items: list[dict] = []
                validation_errors: list[str] = []
                fixed_total = 0
                for item in items:
                    prepared = deepcopy(item)
                    try:
                        fixed_total += _prepare_lifestyle_item_claims(
                            project_id, topic or "", prepared
                        )
                    except Exception as e:
                        validation_errors.append(
                            f"{prepared.get('field', '?')}.day{prepared.get('day', '?')}：{e}"
                        )
                    prepared_items.append(prepared)
                if validation_errors:
                    return (
                        f"写入失败：共发现{len(validation_errors)}处参数问题，请一次性修正后重试：\n- "
                        + "\n- ".join(validation_errors)
                    )
                output = store.write_agent_output_patch(project_id, actor, prepared_items)
                return (
                    f"衣食住行产出已写入 outputs/lifestyle.json（本次更新：{output.last_updated_field}）"
                    f"{_chunk_id_fix_note(fixed_total, topic)}"
                )
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_agent_output)

    if actor == "pitfall_agent":

        @tool
        def write_agent_output(items: list[dict]) -> str:
            """新增或修改避雷产出的具体条目。items是列表，每一项形如：
            {"field": "food"|"outfit"|"lodging"|"play", "day": 1,
             "target_recommendation": "针对的具体推荐原文", "warning": "具体踩坑反馈，没查到就留空",
             "claims": [{"target_path":"warning","claim_text":"一条可独立核验的负面事实",
             "claim_type":"pitfall","support_mode":"direct","claimed_entities":["具体店名/景点名"],
             "evidence_refs":[{"platform":"...","doc_id":"...","chunk_id":"..."}]}]}。
            warning非空时必须提交claims；程序由claims自动汇总sources/
            claimed_entities。warning留空的合法“未查到”结果不需要claims。
            某个字段确实没查到任何避雷内容时，也要针对涉及的每个(field, day)提交一条
            warning留空的item——不提交就代表"这次调用不涉及这个组合"，跟"确实检索过、
            没查到"是两种不同的语义，不要混淆。
            只需要传本次新增或修改的条目——同一(field, day)组合会覆盖旧内容，
            未提及的(field, day)保持原样不受影响，不需要重新传一遍。
            target_recommendation会由系统按(field, day)自动绑定到Lifestyle推荐原文，模型提交的
            截断/概括文本不会作为最终绑定依据；Lifestyle里不存在的组合会被拒绝。
            首次生成时必须覆盖Lifestyle实际存在的全部(field, day)；编辑已有产出时只传被要求修改的那几条。"""
            try:
                if not items:
                    return "写入失败：items不能为空"
                rebound = _bind_pitfall_targets(project_id, items)
                fixed_total = 0
                for item in items:
                    fixed_total += _prepare_pitfall_item_claims(
                        project_id, topic or "", item
                    )
                store.write_pitfall_output_patch(project_id, actor, items)
                bind_note = f"；已按field/day纠正{rebound}处target_recommendation绑定" if rebound else ""
                return f"避雷产出已写入 outputs/pitfall.json{bind_note}{_chunk_id_fix_note(fixed_total, topic)}"
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_agent_output)

    if actor == "culture_agent":

        @tool
        def write_agent_output(culture: dict) -> str:
            """写入文化科普产出区。culture是一个对象，包含items数组，每项形如
            {"question":"用户的问题","answer":"你的回答","references":["查证用到的网页URL"]}。
            这是累加式历史记录——写入前先用read_workspace读取outputs/culture.json里已有的
            items，把它们原样保留在数组最前面，再把这次新的问答追加到最后面一并传入，
            不要丢失历史记录，也不要重复写入同一条。"""
            try:
                output = CultureOutput.model_validate(culture)
                store.write_culture_output(project_id, actor, output)
                return "文化科普产出已写入 outputs/culture.json"
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_agent_output)

    if actor == "integration_agent":

        @tool(args_schema=_FinalGuideWriteArgs, return_direct=True)
        def write_final_guide(blocks: list[dict], meta: dict) -> str:
            """写入最终攻略的完整有序正文块。每块形如
            {"section":"Day1路线","markdown":"## Day 1...",
            "mapping_mode":"upstream","upstream_claim_ids":["route-claim-..."]}。
            mapping_mode可选upstream/editorial/user_constraint/unverified：
            upstream块必须填当前上游JSON里真实存在的claim_id；纯标题、排版、
            清单结构等用editorial并可写mapping_note；直接来自用户要求的表述用
            user_constraint；无上游支持但必须保留的提示用unverified，且必须说明
            mapping_note。block_id和claim_snapshots由程序生成，你不要填。
            meta包含merged_fields/checklist_included等字段；merged_fields使用route_plan、
            lifestyle.food/lifestyle.outfit/lifestyle.lodging/lifestyle.play和pitfall这些规范名。
            references由程序生成，不需要你填写。写入完成后该工具直接结束本轮Agent执行，
            避免已经落盘后再发起一次无业务价值的模型请求。"""
            try:
                output = store.write_final_guide_blocks(project_id, actor, blocks, meta)
                mapped = sum(len(block.upstream_claim_ids) for block in output.blocks)
                return (
                    f"最终攻略已由{len(output.blocks)}个正文块渲染写入 final_guide.md，"
                    f"关联{mapped}条上游claim"
                )
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_final_guide)

        @tool
        def report_conflict(target: str, reason: str) -> str:
            """报告一个冲突给主agent，target形如 'route_agent' 或 'lifestyle_agent.food'。"""
            record = store.report_conflict(project_id, actor, target, reason)
            return f"冲突已记录，id={record.id}"

        tools.append(report_conflict)

    if actor == "poi_detail_agent":

        @tool
        def write_agent_output(routes: list[dict], needs_detail_reason: str = "") -> str:
            """整份覆盖写入你负责的这一个POI详情文件，用于首次生成，或者用户明确要求
            「新增一条路线」/「删除某条路线」这类涉及路线数量变化的结构性调整——这类调整
            需要你对"这个POI到底该有几条路线"做整体判断，必须传完整的routes数组（未涉及
            的路线内容也要传，保持逐字一致）。如果只是修改某条已有路线的内容、或者切换
            推荐哪条路线，不涉及路线数量变化，请改用write_agent_output_patch，不需要
            重新传一遍其它路线。
            routes是一个数组，每项是一条路线，形如
            {"route_name":"前山经典线","suitable_for":"...","recommended":布尔值,
            "recommend_reason":"依据用户偏好判断的理由，不推荐的路线这里可以留空",
            "route_segments":[{"order":1,"point_name":"山门","highlight":"起点，检票入园",
            "suggested_duration":"10分钟","image_prompt":"视觉描述",
            "claims":[
              {"target_path":"point_name","claim_text":"途经点是山门",...},
              {"target_path":"highlight","claim_text":"山门是检票入园的起点",...},
              {"target_path":"suggested_duration","claim_text":"建议停留10分钟",...}
            ]}]}]。每条claim的target_path只填一个精确值，不要把多个值用`|`连在一起。
            point_name/highlight/suggested_duration非空时都要有对应target_path的claim；
            程序会由claims自动汇总segment.sources。每条claim都必须显式填写support_mode；
            direct只能表达原文直接支持的外部事实。“本方案沿用”、“适合顺路游览”等
            路线编排必须拆成独立claim，按真实来源标为user_constraint、derived或estimate。
            只有一条路线时routes数组也只放一项，不要为了凑数硬拆出不存在的第二条路线。
            needs_detail_reason原样带上route_agent当初判断这个点值得深挖的理由（留痕）。
            你只需要传routes，不需要传poi_name/day——这两个字段由系统根据你被分配到的
            具体任务自动填写，不受你的输入影响。"""
            try:
                fixed_total = _prepare_route_segment_claims(
                    project_id,
                    topic or "",
                    routes,
                    day=int(day or 0),
                    poi_name=str(poi_name or ""),
                )
                detail = PoiDetailFile(
                    poi_name=poi_name,
                    day=day,
                    routes=routes,
                    needs_detail_reason=needs_detail_reason,
                )
                store.write_poi_detail(project_id, actor, detail)
                return (
                    f"POI详情已写入 outputs/poi_details/day{day}_{store.poi_slug(poi_name)}.json"
                    f"{_chunk_id_fix_note(fixed_total, topic)}"
                )
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_agent_output)

        @tool
        def write_agent_output_patch(routes: list[dict], needs_detail_reason: str | None = None) -> str:
            """增量修改这个POI详情文件。未提交的路线保持原样；已有路线只更新显式提交的
            suitable_for/recommended/recommend_reason字段，route_segments按order局部合并，
            未提交的途经点同样保持原样。修改一个途经点时，只提交route_name和该途经点的
            完整修正版，不要重新提交整条路线。只适用于"改内容"/"切换推荐路线"——如果用户
            要求「新增一条路线」/「删除某条路线」这类涉及路线数量变化的结构性调整，请改用
            write_agent_output（整份覆盖，需要传完整routes数组）。
            这个POI详情文件必须已经存在（首次生成请用write_agent_output）。
            routes是一个数组，每项必须有route_name；路线级字段只传实际要修改的字段，
            route_segments只传实际要修改的途经点。每个提交的途经点仍应包含完整的
            order/point_name/highlight/suggested_duration/image_prompt/claims，保证内容与事实声明
            保持一致。每条claim必须显式填写support_mode；外部事实和本次路线编排不得
            共用一条direct claim。切换推荐路线时要把旧的推荐路线（改成
            recommended:false）和新的推荐路线（改成recommended:true）两条都提交，只提交
            新的一条会导致旧路线的recommended字段没被清掉。
            省略路线或途经点表示保留，不表示删除；删除或整体重排必须使用write_agent_output。
            needs_detail_reason不传（或传null）时保留原值不变，只有确实需要更新这个理由
            才显式传字符串。"""
            try:
                if not routes:
                    return "写入失败：routes不能为空"
                fixed_total = _prepare_route_segment_claims(
                    project_id,
                    topic or "",
                    routes,
                    day=int(day or 0),
                    poi_name=str(poi_name or ""),
                )
                store.write_poi_detail_patch(
                    project_id, actor, day, poi_name, routes, needs_detail_reason=needs_detail_reason
                )
                return (
                    f"POI详情已更新 outputs/poi_details/day{day}_{store.poi_slug(poi_name)}.json"
                    f"{_chunk_id_fix_note(fixed_total, topic)}"
                )
            except Exception as e:
                return f"写入失败，请检查参数格式后重试：{_format_tool_exception(e)}"

        tools.append(write_agent_output_patch)

    return _install_compact_validation_errors(tools)
