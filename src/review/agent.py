"""Agentic 定向复核。

复核 agent 采用只读调查 + 结构化提交：模型自己按问题单去读取目标内容、精确
chunk、素材检索和历史记录，必要时联网；它不能写攻略文件，也不能创建 patch。
最终结果通过内存中的 submit_review 工具回到现有 recommendation 闭环。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from src.llm_rotation import build_chat_model
from src.materials.bm25_search import BM25SearchIndex
from src.materials.retrieve import retrieve_materials as _retrieve_materials
from src.config import MATERIAL_LIBRARY_DIR, PROJECTS_DIR
from src.review import store
from src.review.advisor import ModelReviewResult, persist_recommendation
from src.review.models import ReviewClaimEvidence
from src.review.runtime import DEFAULT_REVIEW_MODEL_GROUP, REVIEW_MODEL_GROUPS
from src.tools.web_search_tools import make_web_search_tools
from src.workspace import store as workspace_store


MAX_REVIEW_SCOPE_ITEMS = 16
DETERMINISTIC_FINDING_TYPES = {
    "manifest_mismatch",
    "final_guide_contract_violation",
}


def _requirements_topic(project_id: str) -> str:
    requirements = workspace_store.read_requirements(project_id) or {}
    body = requirements.get("requirements", requirements) if isinstance(requirements, dict) else {}
    return str(body.get("destination") or body.get("topic") or "")


def _target_file_and_pointer(target: dict[str, Any]) -> tuple[str, str]:
    file = str(target.get("file") or "")
    raw_path = str(target.get("json_path") or "")
    pointer = ""
    if raw_path.startswith("outputs/"):
        marker = raw_path.find(".json")
        if marker >= 0:
            file = raw_path[: marker + 5]
            rest = raw_path[marker + 5 :].lstrip("/")
            pointer = "/" + rest if rest else ""
    elif raw_path.startswith("/"):
        pointer = raw_path
    return file, pointer


def _pointer_get(value: Any, pointer: str) -> Any:
    if not pointer:
        return value
    current = value
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise KeyError(pointer)
    return current


def _read_target(project_id: str, target: dict[str, Any]) -> str:
    file, pointer = _target_file_and_pointer(target)
    if not file:
        return "问题单没有可读取的目标文件。"
    path = (workspace_store.workspace_dir(project_id) / file).resolve()
    root = workspace_store.workspace_dir(project_id).resolve()
    if root not in path.parents or path.suffix != ".json" or not path.exists():
        return f"目标文件不可读或不存在: {file}"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        value = _pointer_get(value, pointer)
    except (OSError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
        return f"读取目标内容失败: {exc}"
    payload = json.dumps({"file": file, "json_pointer": pointer or None, "content": value}, ensure_ascii=False, indent=2)
    return payload if len(payload) <= 16000 else payload[:16000] + "\n...[目标内容已截断，请用 JSON Pointer 或具体实体继续核对]"


def _read_chunk(topic: str, chunk_id: str) -> str:
    if not topic or not chunk_id:
        return "需要同时提供目的地 topic 和 chunk_id。"
    path = MATERIAL_LIBRARY_DIR / "index" / topic / "chunks.jsonl"
    if not path.exists():
        return f"素材索引不存在: {topic}"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("chunk_id") or "") == chunk_id:
            return json.dumps({
                "chunk_id": chunk_id,
                "doc_id": row.get("doc_id"),
                "platform": row.get("platform"),
                "author": row.get("author"),
                "text": str(row.get("text", ""))[:3000],
            }, ensure_ascii=False, indent=2)
    return f"没有找到 chunk_id={chunk_id} 的原文。"


def _read_history(project_id: str, target: dict[str, Any], limit: int) -> str:
    path = workspace_store.workspace_dir(project_id) / "review_log.jsonl"
    if not path.exists():
        return "当前项目没有历史复核记录。"
    file = str(target.get("file") or "")
    scope = str(target.get("scope") or "")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("review_type") not in {"content_review", "backfill", "presentation_fix", "finding"}:
            continue
        blob = json.dumps(record.get("target") or {}, ensure_ascii=False)
        if (file and file in blob) or (scope and scope in blob):
            records.append({
                "review_id": record.get("review_id"),
                "decision": record.get("decision"),
                "action": record.get("action"),
                "notes": record.get("notes"),
                "evidence_refs": record.get("evidence_refs") or [],
                "llm_called": bool(record.get("llm_called")),
            })
    return json.dumps(records[-max(1, min(limit, 20)):], ensure_ascii=False, indent=2)


def _read_prior_recommendation(project_id: str, recommendation_id: str) -> str:
    """读取旧 recommendation 作为待核验假设，不把历史模型结论当成事实。"""
    if not recommendation_id:
        return "当前 finding 没有历史 recommendation。"
    path = workspace_store.workspace_dir(project_id) / "review_runs" / recommendation_id / "recommendation.json"
    if not path.exists():
        return f"历史 recommendation 不存在: {recommendation_id}"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"读取历史 recommendation 失败: {exc}"
    return json.dumps({"historical_recommendation": value, "warning": "这只是历史模型假设，必须重新用当前原文核验"}, ensure_ascii=False, indent=2)


def _current_target_value(finding: store.ReviewFinding, file: str, pointer: str) -> Any:
    if finding.observed_value is not None:
        return finding.observed_value
    path = workspace_store.workspace_dir(finding.project_id) / file
    if not file or path.suffix != ".json" or not path.exists():
        raise ValueError("问题单目标不是可读取的 workspace JSON 文件，请人工补充定位信息")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return _pointer_get(document, pointer)
    except (OSError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"无法读取问题单目标，请人工修正 file/json_path：{exc}") from exc


def _generic_scope_item(finding: store.ReviewFinding) -> dict[str, Any] | None:
    file, pointer = _target_file_and_pointer(finding.target or {})
    if not file or not pointer:
        return None
    value = _current_target_value(finding, file, pointer)
    sources = value.get("sources", []) if isinstance(value, dict) else []
    label = str((finding.target or {}).get("object_label") or finding.problem_type)
    current_claim = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return {
        "route_name": "",
        "point_name": label,
        "target_path": pointer,
        "claim": current_claim,
        "suggested_duration": "",
        "sources": sources,
    }


def _bounded_scope(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) > MAX_REVIEW_SCOPE_ITEMS:
        raise ValueError(
            f"问题单包含 {len(items)} 个争议项，超过单次复核上限 {MAX_REVIEW_SCOPE_ITEMS}；"
            "请人工拆分 Finding 后再运行，原问题单不会被截断或关闭"
        )
    return items


def _assert_model_reviewable(finding: store.ReviewFinding) -> None:
    if finding.problem_type in DETERMINISTIC_FINDING_TYPES:
        raise ValueError(
            f"{finding.problem_type} 属于确定性一致性问题，应使用对应修复或重跑入口，不调用模型复核"
        )


def _disputed_scope(finding: store.ReviewFinding) -> list[dict[str, Any]]:
    """解析唯一复核范围，避免 agent 自己扫描整份攻略。"""
    summary = finding.issue_summary or ""
    disputed_pairs: list[tuple[str, str]] = []
    disputed_paths: set[str] = set()
    # recommendation_id 是最新投影，而不是完整范围。模型偶发少返回
    # 几条时，不能让下一次刷新继续缩窄问题集；因此从追加历史中选
    # 取覆盖最完整的一版作为基准清单。
    recommendations = store.read_recommendations_for_finding(finding.project_id, finding.finding_id)
    if recommendations:
        def scope_size(recommendation: store.ReviewRecommendation) -> int:
            if recommendation.claim_reviews:
                return len({item.target_path or item.claim for item in recommendation.claim_reviews})
            return len(re.findall(r"^\s*-\s*[^\n「『]+?[「『][^」』]+[」』]：", recommendation.proposed_text or "", re.MULTILINE))

        prior = max(recommendations, key=lambda item: (scope_size(item), item.created_at))
        if prior.claim_reviews:
            disputed_paths.update(item.target_path for item in prior.claim_reviews if item.target_path)
        else:
            disputed_pairs.extend(
                (route.strip(), point.strip())
                for route, point in re.findall(
                    r"^\s*-\s*([^\n「『]+?)[「『]([^」』]+)[」』]：",
                    prior.proposed_text or "",
                    flags=re.MULTILINE,
                )
            )
    if not disputed_pairs:
        for fragment in summary.split("；"):
            match = re.search(r"(?:第\d+轮：)?(.+?)途经点[「『]([^」』]+)[」』]", fragment)
            if match:
                route_label = match.group(1).strip()
                # File-level state findings prefix the latest retry with a
                # human summary such as "...recent failure: round 3:".  That
                # prefix is not part of the route name and previously made an
                # otherwise exact POI segment impossible to export.
                round_prefixes = list(re.finditer(r"第\d+轮[：:]", route_label))
                if round_prefixes:
                    route_label = route_label[round_prefixes[-1].end():].strip()
                disputed_pairs.append((route_label, match.group(2).strip()))
    names = list(dict.fromkeys(point for _route, point in disputed_pairs))
    expected_routes = {route for route, _point in disputed_pairs if route}

    def route_matches(expected_route: str, actual_route: str) -> bool:
        if not expected_route or expected_route == actual_route:
            return True
        # A legacy label may omit a qualifier added to the current route name.
        # Do not use that loose match when the qualified route is itself an
        # explicit disputed route, otherwise sibling routes bleed together.
        return expected_route in actual_route and actual_route not in expected_routes

    if not names:
        names = list(dict.fromkeys(re.findall(r"途经点[「『]([^」』]+)[」』]", summary)))
    observed = finding.observed_value if isinstance(finding.observed_value, dict) else {}
    segments: list[dict[str, Any]] = []
    for route_index, route in enumerate(observed.get("routes", []) or []):
        for segment_index, segment in enumerate(route.get("route_segments", []) or []):
            point_name = str(segment.get("point_name") or "")
            route_name = str(route.get("route_name") or "")
            target_path = f"/routes/{route_index}/route_segments/{segment_index}"
            path_matches = target_path in disputed_paths
            pair_matches = any(
                (point in point_name or point_name in point)
                and route_matches(expected_route, route_name)
                for expected_route, point in disputed_pairs
            )
            name_matches = any(name in point_name or point_name in name for name in names)
            if not point_name:
                continue
            if disputed_paths and not path_matches:
                continue
            if not disputed_paths and disputed_pairs and not pair_matches:
                continue
            if not disputed_paths and not disputed_pairs and names and not name_matches:
                continue
            segments.append({
                "route_name": route_name,
                "point_name": point_name,
                "target_path": target_path,
                "claim": segment.get("highlight", ""),
                "suggested_duration": segment.get("suggested_duration", ""),
                "sources": segment.get("sources", []),
            })
    if segments:
        return _bounded_scope(segments)
    if names:
        raise ValueError("历史争议点无法映射到当前 JSON 路径，请人工补充或刷新 Finding")
    generic = _generic_scope_item(finding)
    if generic is not None:
        return [generic]
    raise ValueError("问题单缺少可复核的 file + json_path 或具体 POI 路线段，请人工补充定位信息")


def _validate_claim_reviews_against_scope(
    scope: list[dict[str, Any]],
    claim_reviews: list[ReviewClaimEvidence],
    *,
    stage: str,
) -> None:
    """确保模型只回答程序圈定的争议项。"""
    if not scope:
        raise RuntimeError(f"{stage}没有可校验的程序圈定范围；未写入任何 recommendation")
    if len(claim_reviews) != len(scope):
        raise RuntimeError(
            f"{stage}返回 {len(claim_reviews)} 条 claim_review，"
            f"预期 {len(scope)} 条；未写入任何 recommendation"
        )
    for index, (expected, actual) in enumerate(zip(scope, claim_reviews)):
        expected_path = str(expected.get("target_path") or "")
        if expected_path and actual.target_path != expected_path:
            raise RuntimeError(
                f"{stage}第 {index + 1} 条 target_path 与程序圈定范围不一致："
                f"预期 {expected_path}，实际 {actual.target_path or '<empty>'}；"
                "未写入任何 recommendation"
            )


def _load_material_rows(topic: str) -> list[dict[str, Any]]:
    if not topic:
        return []
    path = MATERIAL_LIBRARY_DIR / "index" / topic / "chunks.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("chunk_id") and row.get("text"):
            rows.append(row)
    return rows


def _build_disputed_evidence_packet(topic: str, finding: store.ReviewFinding) -> dict[str, Any]:
    """一次性提供直接引用和目的地素材候选，避免逐点工具预算导致漏检。"""
    scope = _disputed_scope(finding)
    rows = _load_material_rows(topic)
    row_map = {str(row["chunk_id"]): row for row in rows}
    index = BM25SearchIndex(rows) if rows else None

    def collect_attached_refs(value: Any) -> set[str]:
        refs: set[str] = set()
        if isinstance(value, dict):
            sources = value.get("sources")
            if isinstance(sources, list):
                refs.update(
                    str(source.get("chunk_id") or "")
                    for source in sources
                    if isinstance(source, dict) and source.get("chunk_id")
                )
            for child in value.values():
                refs.update(collect_attached_refs(child))
        elif isinstance(value, list):
            for child in value:
                refs.update(collect_attached_refs(child))
        return refs

    all_attached_refs = collect_attached_refs(finding.observed_value)
    packet_items: list[dict[str, Any]] = []
    for item in scope:
        direct_refs = [
            str(source.get("chunk_id") or "")
            for source in (item.get("sources") or [])
            if isinstance(source, dict) and source.get("chunk_id")
        ]
        direct_evidence = []
        for ref in direct_refs:
            row = row_map.get(ref)
            direct_evidence.append({
                "ref": ref,
                "found": row is not None,
                "text": str(row.get("text") or "")[:700] if row else "没有在素材库找到该引用原文",
            })

        point_name = str(item.get("point_name") or "")
        claim = str(item.get("claim") or "")
        query = " ".join(part for part in (point_name, claim[:160]) if part).strip()
        ranked = index.search(query, top_k=12) if index and query else []
        ranked_scores = {ref: float(score) for ref, score in ranked}
        exact_terms = [
            term.strip()
            for term in re.split(r"[/（()）·、，,\s]+", point_name)
            if len(term.strip()) >= 2
        ]
        exact_refs = [
            str(row["chunk_id"])
            for row in rows
            if any(term in str(row.get("text") or "") for term in exact_terms)
        ]
        candidate_refs = list(dict.fromkeys([*exact_refs, *(ref for ref, _score in ranked)]))
        candidates: list[dict[str, Any]] = []
        for ref in candidate_refs:
            if ref in direct_refs or ref not in row_map:
                continue
            row = row_map[ref]
            text = str(row.get("text") or "")
            candidates.append({
                "ref": ref,
                "text": text[:700],
                "retrieval_score": round(ranked_scores.get(ref, 0.0), 4),
                "exact_terms": [term for term in exact_terms if term in text],
                "already_attached_elsewhere": ref in all_attached_refs,
            })
        candidates.sort(key=lambda candidate: (
            not candidate["exact_terms"],
            not candidate["already_attached_elsewhere"],
            -candidate["retrieval_score"],
        ))
        candidates = candidates[:3]
        packet_items.append({
            "route_name": item.get("route_name"),
            "point_name": point_name,
            "target_path": item.get("target_path", ""),
            "current_claim": claim,
            "suggested_duration": item.get("suggested_duration"),
            "direct_evidence": direct_evidence,
            "alternative_candidates": candidates,
        })
    return {
        "warning": (
            "直接引用不支持，只能说明当前字段的引用可能挂错；必须继续检查 alternative_candidates。"
            "候选只是召回结果，模型仍需逐字判断支持范围，不能只按检索分数下结论。"
        ),
        "items": packet_items,
    }


def _make_tools(project_id: str, finding: store.ReviewFinding, captured: dict[str, Any]) -> list:
    topic = _requirements_topic(project_id)
    target = finding.target or {}

    def mark_tool(name: str) -> None:
        captured.setdefault("tool_calls", []).append(name)

    seen_chunks: set[str] = set()
    seen_queries: set[str] = set()

    @tool
    def read_current_target() -> str:
        """读取问题单指向的当前 JSON 文件或 JSON Pointer 内容，只读。"""
        mark_tool("read_current_target")
        return _read_target(project_id, target)

    @tool
    def read_material_chunk(chunk_id: str) -> str:
        """按精确 chunk_id 读取素材原文，用来核对当前引用是否真的支持表述。"""
        mark_tool("read_material_chunk")
        if chunk_id in seen_chunks:
            return f"已经读取过 chunk_id={chunk_id}，不要重复调用；请使用已有原文做判断。"
        seen_chunks.add(chunk_id)
        return _read_chunk(topic, chunk_id)

    @tool
    def retrieve_materials(query: str, top_k: int = 5) -> str:
        """按一个具体景点、实体或断言检索素材，不要把多个问题混成一个 query。"""
        mark_tool("retrieve_materials")
        normalized_query = " ".join(query.split()).strip().lower()
        if normalized_query in seen_queries:
            return f"已经检索过“{query}”，不要重复调用；请据已有结果判定证据缺口。"
        seen_queries.add(normalized_query)
        results = _retrieve_materials(topic, query, max(1, min(top_k, 6))) if topic else []
        payload = json.dumps(results, ensure_ascii=False, indent=2) if results else "没有检索到相关素材。"
        return payload if len(payload) <= 9000 else payload[:9000] + "\n...[检索结果已截断，请改用更具体的实体查询]"

    @tool
    def read_review_history(limit: int = 12) -> str:
        """读取当前目标相关的历史复核记录，判断是新问题、旧误报还是已处理未同步。"""
        mark_tool("read_review_history")
        return _read_history(project_id, target, limit)

    @tool
    def read_prior_recommendation() -> str:
        """读取该 finding 之前的模型建议，了解历史上具体指出过哪些点；历史建议不是最终结论。"""
        mark_tool("read_prior_recommendation")
        return _read_prior_recommendation(project_id, finding.recommendation_id or "")

    @tool
    def inspect_disputed_evidence() -> str:
        """批量读取所有争议点的直接引用与目的地素材候选；判断不支持前必须先调用。"""
        mark_tool("inspect_disputed_evidence")
        payload = json.dumps(_build_disputed_evidence_packet(topic, finding), ensure_ascii=False, indent=2)
        return payload if len(payload) <= 42000 else payload[:42000] + "\n...[证据包已截断，可用具体断言继续 retrieve_materials]"

    @tool
    def submit_review(
        passed: bool,
        issues: list[str],
        summary: str,
        proposed_text: str,
        risk_level: str,
        confidence: str,
        recommended_action: str,
        uncertainties: list[str],
        claim_reviews: list[ReviewClaimEvidence],
    ) -> str:
        """提交最终复核建议。只写入本次运行内存，不修改攻略、finding 或 patch。"""
        mark_tool("submit_review")
        validated_claim_reviews = [
            item if isinstance(item, ReviewClaimEvidence) else ReviewClaimEvidence.model_validate(item)
            for item in claim_reviews
        ]
        _validate_claim_reviews_against_scope(
            _disputed_scope(finding),
            validated_claim_reviews,
            stage="Agent 复核",
        )
        captured["result"] = ModelReviewResult(
            passed=passed,
            issues=issues,
            summary=summary,
            proposed_text=proposed_text,
            risk_level=risk_level,
            confidence=confidence,
            recommended_action=recommended_action,
            uncertainties=uncertainties,
            claim_reviews=validated_claim_reviews,
        )
        return "复核建议已提交；不要再调用工具。"

    return [read_current_target, inspect_disputed_evidence, read_material_chunk, retrieve_materials, read_review_history, read_prior_recommendation, submit_review] + make_web_search_tools(
        project_id, f"review_agent/{finding.finding_id}"
    )


def _prompt(finding: store.ReviewFinding) -> str:
    scope = _disputed_scope(finding)
    return (
        "你是旅游攻略的定向复核 agent，只处理当前问题单列出的争议点。你不是重新生成攻略的 agent，"
        "不能修改文件、不能写 patch，也不能扩大到整条路线。\n\n"
        "【必须遵守的调查协议】\n"
        "第1步：只调用一次 read_current_target，读取当前目标 JSON；如果问题单有 recommendation_id，再调用一次 read_prior_recommendation，"
        "把历史建议当作待核验的问题清单，绝不能直接照抄历史结论。\n"
        "第2步：调用一次 inspect_disputed_evidence，批量读取每个争议点的当前直接引用和全目的地素材候选；"
        "然后逐点建立 claim_reviews 证据账本，字段必须与 submit_review 完全一致："
        "claim、target_path、original_text、evidence_refs、evidence_excerpt、verdict、action、content_action、evidence_action、proposed_text、rationale、confidence。\n"
        "第3步：直接引用能读取但不支持当前表述时，不能直接判定素材不存在；必须检查证据包里的 alternative_candidates。"
        "候选原文能支持地点但不能支持全部细节时，应 repair_reference 或 add_source，并只 narrow 无依据的细节/时长。"
        "只有证据包仍不足时，才用该点名称或一个具体断言调用一次 retrieve_materials。"
        "不要用“成都攻略”“熊猫基地路线”这类宽泛 query。\n"
        "第4步：开放时间、交通、预约、临时关闭等时效问题应调用 web_search；"
        "对有保留价值但本地素材不足的静态事实，也可做一次定向联网核实。\n"
        "第5步：所有争议点都完成账本判断后，立即调用 submit_review，不要继续探索。\n"
        "总调查工具调用不超过 14 次（submit_review 计入）；read_current_target/inspect_disputed_evidence/read_prior_recommendation/read_review_history 各最多一次，"
        "同一个 chunk 或 query 不得重复。\n\n"
        "【结论规则】\n"
        "- 直接支持：保留原表述，必要时修正引用；\n"
        "- 部分支持：保留地点和游览价值，只收窄无依据的细节、数字或时长；\n"
        "- 证据缺口：不要删除地点，建议补来源或标注“素材未直接提及，以现场/官方为准”；\n"
        "- 明确矛盾：只修改与证据冲突的那一小段，不能借机重写整条路线；\n"
        "- 历史状态残留：如果当前内容已经合理，结论应是通过复核并同步状态，而不是重跑。\n"
        "没有证据不等于错误；地点删除是最后选项。模型不得凭空补充店名、票价、开放时间或路线细节。\n\n"
        "【claim_reviews 输出协议】\n"
        "- 每个争议点恰好一条，不得遗漏、合并或增加其他问题；\n"
        "- target_path 必须从本次争议点清单原样复制，不得改成子字段、父路径或其他 JSON Pointer；\n"
        "- original_text 原样复制当前攻略的表述，不做改写；\n"
        "- evidence_refs 只写本次工具实际读取过的 chunk_id/URL；未命中时写 search:<实际查询词>；\n"
        "- evidence_excerpt 必须是工具返回中的短原文；未命中时原样写“没有检索到相关素材”；\n"
        "- verdict 只能是 supported/partial/unsupported/contradicted/ambiguous；\n"
        "- action 只能是 keep/narrow/add_source/repair_reference/sync_status/rerun；\n"
        "- content_action 只能是 keep/narrow/rerun，表示正文本身怎么处理；\n"
        "- evidence_action 只能是 keep/add_source/repair_reference/sync_status，表示引用和状态怎么处理；\n"
        "- 一条内容可同时 content_action=narrow、evidence_action=repair_reference；action 填最主要、最需要人工注意的动作；\n"
        "- proposed_text 必须是人工可直接审阅的修改后正文，不得写成操作说明；keep/sync_status 时写“保持原文”；\n"
        "- content_action=keep 时，rationale 不得同时要求删除、取消时长、收窄或改写正文；引用变化只写在 evidence_action/evidence_refs；\n"
        "- rationale 必须解释 evidence_excerpt 如何导出 verdict 和 action；所有字段禁止空字符串或空数组。\n\n"
        "【few-shot 示例】\n"
        "示例A（地点有依据、时长无依据）："
        '{"claim":"小熊猫区建议游览20-30分钟","target_path":"/routes/0/route_segments/5","original_text":"小熊猫区（红熊猫）……约20-30分钟",'
        '"evidence_refs":["成都-035-c000"],"evidence_excerpt":"最南部的大熊猫别墅、小熊猫区域以及月亮产房",'
        '"verdict":"partial","action":"narrow","content_action":"narrow","evidence_action":"keep","proposed_text":"保留小熊猫区域，游览时长改为视现场灵活安排",'
        '"rationale":"原文支持地点存在，但没有提供20-30分钟时长","confidence":"high"}\n'
        "示例B（引用错位）："
        '{"claim":"当前引用支持观景平台","target_path":"/routes/0/route_segments/2","original_text":"sources: 示例-错误-c001",'
        '"evidence_refs":["示例-错误-c001","示例-正确-c004"],"evidence_excerpt":"错误引用未出现观景平台；候选引用明确提到沿木栈道抵达观景平台",'
        '"verdict":"contradicted","action":"repair_reference","content_action":"keep","evidence_action":"repair_reference","proposed_text":"来源改为示例-正确-c004",'
        '"rationale":"当前引用不支持该地点，另一个实际读取的chunk可以支持","confidence":"high"}\n\n'
        f"【本次争议点清单】\n{json.dumps(scope, ensure_ascii=False, indent=2)}\n\n"
        f"【问题单元数据】\n{json.dumps({'finding_id': finding.finding_id, 'target': finding.target, 'problem_type': finding.problem_type, 'recommendation_id': finding.recommendation_id, 'issue_summary': (finding.issue_summary or '')[-6000:], 'review_question': finding.review_question, 'evidence_refs': finding.evidence_refs}, ensure_ascii=False, indent=2)}\n\n"
        "最终 submit_review 的 summary 必须逐点写明“保留/收窄/补来源/明确冲突修改/状态同步”中的哪一种，"
        "proposed_text 只写最小改写，不要输出整篇攻略。claim_reviews 是主要输出，"
        "先完整填写它，再写 summary；不能把证据只写在 summary 而让 claim_reviews 留空。"
    )


def _message_text(message: Any) -> str:
    """把 LangChain 消息的 content 稳定转成文本。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _investigator_report(state: Any) -> str:
    """取 agent 最后一段非空模型回答，作为结构化裁决的输入。"""
    messages = state.get("messages", []) if isinstance(state, dict) else []
    for message in reversed(messages):
        if getattr(message, "type", "") not in {"ai", "assistant"}:
            continue
        text = _message_text(message)
        if text:
            return text[-12000:]
    return ""


def _finalize_investigation(
    model: Any,
    finding: store.ReviewFinding,
    state: Any,
) -> ModelReviewResult:
    """agent 未调用 submit_review 时，用同一模型将调查报告收口为结构化结果。"""
    report = _investigator_report(state)
    if not report:
        raise RuntimeError("调查 agent 未产出可裁决的逐点报告")

    prompt = (
        "你是旅游攻略定向复核的最终裁决器。前置 agent 已经读取当前内容、原始素材和历史记录；"
        "你只能根据它的调查报告形成结构化结论，不得补写报告中没有的事实。\n"
        "逐点使用且仅使用：保留、收窄、补来源、明确冲突修改、状态同步。"
        "没有证据不等于内容错误；不得因 chunk 未命中就删除有游览价值的地点。"
        "proposed_text 必须给出修改后正文；若当前内容无需修改，写“保持原文”，不得在 rationale 中又要求删改正文。"
        "同时返回 claim_reviews，每项必须包含 claim、original_text、evidence_refs、evidence_excerpt、verdict、action、content_action、evidence_action、rationale、confidence。"
        "如果调查报告仍无法对某点判定，将它列入 uncertainties，不得伪造结论。\n\n"
        f"本次争议点：{json.dumps(_disputed_scope(finding), ensure_ascii=False)}\n"
        f"问题单摘要：{(finding.issue_summary or '')[-4000:]}\n\n"
        f"前置 agent 调查报告：\n{report}"
    )
    structured = model.with_structured_output(ModelReviewResult)
    value = structured.invoke([{"role": "user", "content": prompt}])
    result = value if isinstance(value, ModelReviewResult) else ModelReviewResult.model_validate(value)
    _validate_claim_reviews_against_scope(
        _disputed_scope(finding),
        result.claim_reviews,
        stage="调查收口",
    )
    return result


def _merge_batched_results(results: list[ModelReviewResult]) -> ModelReviewResult:
    claim_reviews = [claim for result in results for claim in result.claim_reviews]
    action_names = {
        "keep": "保留",
        "narrow": "收窄",
        "add_source": "补来源",
        "repair_reference": "修正引用",
        "sync_status": "状态同步",
        "rerun": "需重跑",
    }
    counts: dict[str, int] = {}
    for claim in claim_reviews:
        counts[claim.action] = counts.get(claim.action, 0) + 1
    summary = "分批复核完成：" + "，".join(
        f"{action_names[action]} {count} 条" for action, count in counts.items()
    )
    issues = [
        f"{claim.claim}：{claim.rationale}"
        for claim in claim_reviews
        if claim.action not in {"keep", "sync_status"}
    ]
    proposed_text = "\n".join(
        f"{claim.claim}：{claim.proposed_text}"
        for claim in claim_reviews
        if claim.action not in {"keep", "sync_status"} and claim.proposed_text
    )
    passed = all(
        claim.action == "sync_status" or (claim.action == "keep" and claim.verdict == "supported")
        for claim in claim_reviews
    )
    risk_level = "high" if any(claim.action == "rerun" for claim in claim_reviews) else "medium" if issues else "low"
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    confidence = min(
        (claim.confidence for claim in claim_reviews),
        key=lambda value: confidence_rank[value],
        default="medium",
    )
    uncertainties = list(dict.fromkeys(item for result in results for item in result.uncertainties))
    return ModelReviewResult(
        passed=passed,
        issues=issues,
        summary=summary,
        proposed_text=proposed_text,
        claim_reviews=claim_reviews,
        risk_level=risk_level,
        confidence=confidence,
        recommended_action="sync_status" if passed else "human_review",
        uncertainties=uncertainties,
    )


def _invoke_review_json(model: Any, prompt: str) -> ModelReviewResult:
    """Use plain chat JSON when a provider lacks response_format support."""
    schema = json.dumps(ModelReviewResult.model_json_schema(), ensure_ascii=False)
    response = model.invoke([{
        "role": "user",
        "content": (
            f"{prompt}\n\n严格只输出一个 JSON 对象，不要 Markdown 代码块或额外说明。"
            f"输出必须通过以下 JSON Schema：{schema}"
        ),
    }])
    text = _message_text(response)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("分批复核模型未返回可解析的 JSON")
    try:
        return ModelReviewResult.model_validate_json(text[start : end + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"分批复核 JSON 校验失败: {exc}") from exc


_WEB_FAILURE_MARKERS = (
    "联网搜索未配置",
    "联网搜索没有找到",
    "联网搜索请求失败",
    "联网搜索所有已配置的key都不可用",
)


def _claim_needs_consistency_review(claim: ReviewClaimEvidence) -> bool:
    """识别需要二次收口的建议。

    首轮的任务是全量粗排；只有未命中证据、引用不可读，或结论与
    拟议文案明显矛盾的条目才进入小范围二次复核。
    """
    if claim.action == "rerun" or claim.verdict in {"unsupported", "ambiguous"}:
        return True
    if any(ref.startswith("material_search:") for ref in claim.evidence_refs):
        return True
    if "没有在素材库找到" in claim.evidence_excerpt:
        return True
    if claim.action == "add_source" and claim.confidence != "high":
        return True
    if any(ref.startswith("search:") for ref in claim.evidence_refs) and any(
        marker in f"{claim.evidence_excerpt} {claim.rationale}"
        for marker in ("缺乏官方来源", "未找到官方网站", "旅游平台")
    ):
        return True
    rationale_says_duration_missing = any(
        marker in claim.rationale
        for marker in ("时长无依据", "时长没有证据", "停留分钟无对应", "无原文对应")
    )
    retains_numeric_duration = bool(
        re.search(r"\d+\s*(?:[-–~至]\s*\d+)?\s*分钟", claim.proposed_text)
    )
    return rationale_says_duration_missing and retains_numeric_duration


def _claim_needs_web_evidence(claim: ReviewClaimEvidence) -> bool:
    """只对“本地素材未命中”的实体性争议联网，不为普通文案改写增加延迟。"""
    return (
        claim.action == "rerun"
        or claim.action == "add_source"
        or claim.verdict in {"unsupported", "ambiguous"}
        or any(ref.startswith("material_search:") for ref in claim.evidence_refs)
        or any(ref.startswith("search:") for ref in claim.evidence_refs)
        or "没有在素材库找到" in claim.evidence_excerpt
    )


def _web_evidence_for_item(
    project_id: str,
    finding_id: str,
    subject: str,
    item: dict[str, Any],
) -> list[dict[str, str]]:
    """为本地素材缺口召回公开证据；返回结果仍是候选，由模型逐字判断。"""
    point_name = str(item.get("point_name") or "").strip()
    if not point_name:
        return []
    query = " ".join(part for part in (subject, point_name, "官方") if part)
    web_search = make_web_search_tools(project_id, f"review_agent/{finding_id}")[0]
    try:
        result = str(web_search.invoke({"query": query}) or "").strip()
    except Exception as exc:  # 联网失败不应让本地复核整体失败
        return [{"query": query, "result": f"联网补证失败：{exc}"}]
    if not result or any(marker in result for marker in _WEB_FAILURE_MARKERS):
        return [{"query": query, "result": result or "联网补证无返回内容"}]
    return [{"query": query, "result": result[:7000]}]


def _refine_batched_claims(
    project_id: str,
    finding: store.ReviewFinding,
    topic: str,
    model: Any,
    items: list[dict[str, Any]],
    merged: ModelReviewResult,
    *,
    batch_size: int,
) -> ModelReviewResult:
    """对首轮未收敛或自相矛盾的少量条目做一次有界二次复核。"""
    selected: list[tuple[int, dict[str, Any], ReviewClaimEvidence]] = []
    for index, (item, claim) in enumerate(zip(items, merged.claim_reviews)):
        if _claim_needs_consistency_review(claim):
            selected.append((index, item, claim))
    if not selected:
        return merged

    refined_claims = list(merged.claim_reviews)
    observed = finding.observed_value if isinstance(finding.observed_value, dict) else {}
    evidence_subject = str(observed.get("poi_name") or topic or "").strip()
    for start in range(0, len(selected), max(1, batch_size)):
        selection_batch = selected[start : start + max(1, batch_size)]
        review_batch = []
        for _index, item, prior_claim in selection_batch:
            review_item = dict(item)
            review_item["first_pass_review"] = prior_claim.model_dump(mode="json")
            review_item["review_feedback"] = (
                "重新检查证据与拟议文案是否自洽；若时长被判定无依据，拟议文案不得继续保留该固定数字。"
            )
            review_item["web_candidates"] = (
                _web_evidence_for_item(project_id, finding.finding_id, evidence_subject, item)
                if _claim_needs_web_evidence(prior_claim)
                else []
            )
            review_batch.append(review_item)
        prompt = (
            "你是旅游攻略定向复核的二次裁决员。只修正本批首轮未收敛或自相矛盾的条目，"
            "不重生路线，不扩大问题范围。\n"
            "每个 items 必须恰好输出一条 claim_review，顺序与输入一致，target_path 原样复制。"
            "web_candidates 只是联网召回，必须优先使用官方网站、政府网站或景区官方信息进行裁决。"
            "Trip.com、本地宝、永安旅游、博客和建筑媒体都是第三方，不得称为官方来源。"
            "如果官方只能确认点位/站点存在，拟议文案也只能保留该层级的事实，不得自行推导休息功能、游览时长或开放规则。"
            "本地引用挂错但公开证据支持时，应使用 add_source 或 repair_reference，不得标为 rerun。"
            "时长或过度具体描述没有依据时，保留景点价值并改为灵活/现场安排；"
            "不得在 rationale 说数字无依据却在 proposed_text 中保留同一数字。"
            "evidence_refs 只能写实际展示的 chunk_id、搜索 query 或 URL；evidence_excerpt 使用实际判断的短原文。\n\n"
            f"本批二次复核包：{json.dumps(review_batch, ensure_ascii=False, indent=2)}"
        )
        parsed = _invoke_review_json(model, prompt)
        _validate_claim_reviews_against_scope(
            [item for _index, item, _prior in selection_batch],
            parsed.claim_reviews,
            stage="二次复核",
        )
        for (index, _item, _prior), claim in zip(selection_batch, parsed.claim_reviews):
            refined_claims[index] = claim

    return _merge_batched_results([
        ModelReviewResult(
            passed=False,
            summary="二次小范围复核已完成",
            claim_reviews=refined_claims,
            uncertainties=merged.uncertainties,
        )
    ])


def _project_latest_complete_claims(project_id: str, finding_id: str) -> list[ReviewClaimEvidence]:
    """把追加式 recommendation 投影成“最完整范围 + 每条最新结论”。

    较新的模型调用可能只复核了少数条目。它应更新这些 target_path，
    但不能让其他条目从当前投影中消失。
    """
    recommendations = store.read_recommendations_for_finding(project_id, finding_id)
    with_claims = [item for item in recommendations if item.claim_reviews]
    if not with_claims:
        return []
    base = max(
        with_claims,
        key=lambda item: (len({claim.target_path or claim.claim for claim in item.claim_reviews}), item.created_at),
    )
    ordered_keys = [claim.target_path or claim.claim for claim in base.claim_reviews]
    latest = {claim.target_path or claim.claim: claim for claim in base.claim_reviews}
    base_keys = set(ordered_keys)
    for recommendation in with_claims:
        for claim in recommendation.claim_reviews:
            key = claim.target_path or claim.claim
            if key in base_keys:
                latest[key] = claim
    return [latest[key] for key in ordered_keys]


def refine_existing_recommendation(
    project_id: str,
    finding_id: str,
    *,
    channel: str = "mimo",
    batch_size: int = 3,
) -> tuple[store.ReviewRecommendation, ModelReviewResult]:
    """基于已有模型结果只复核未收敛条目，不重跑已经通过的条目。"""
    finding = store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    claims = _project_latest_complete_claims(project_id, finding_id)
    if not claims:
        raise RuntimeError("没有可用于定向收口的历史模型建议")
    topic = _requirements_topic(project_id)
    items = _build_disputed_evidence_packet(topic, finding)["items"]
    claim_map = {claim.target_path: claim for claim in claims if claim.target_path}
    ordered_claims = [claim_map.get(str(item.get("target_path") or "")) for item in items]
    if any(claim is None for claim in ordered_claims):
        raise RuntimeError("历史建议与当前完整复核范围无法按 target_path 对齐")
    merged = _merge_batched_results([
        ModelReviewResult(
            passed=False,
            summary="历史建议统一投影",
            claim_reviews=[claim for claim in ordered_claims if claim is not None],
        )
    ])
    model = build_chat_model(group=channel, temperature=0)
    refined = _refine_batched_claims(
        project_id,
        finding,
        topic,
        model,
        items,
        merged,
        batch_size=batch_size,
    )
    recommendation = persist_recommendation(
        project_id,
        finding,
        refined,
        model=model,
        context_version="review-batched-v2-refine",
    )
    return recommendation, refined


def generate_batched_recommendation(
    project_id: str,
    finding_id: str,
    *,
    channel: str = DEFAULT_REVIEW_MODEL_GROUP,
    batch_size: int = 3,
    promote: bool = True,
) -> tuple[store.ReviewRecommendation, ModelReviewResult]:
    """Review a long disputed scope in bounded model calls, then persist once."""
    finding = store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    _assert_model_reviewable(finding)
    topic = _requirements_topic(project_id)
    packet = _build_disputed_evidence_packet(topic, finding)
    items = packet["items"]
    if not items:
        raise RuntimeError("问题单没有可分批复核的具体争议点")
    model = build_chat_model(group=channel, temperature=0)
    results: list[ModelReviewResult] = []
    for start in range(0, len(items), max(1, batch_size)):
        batch = items[start : start + max(1, batch_size)]
        prompt = (
            "你是旅游攻略定向复核员。下面是一个长问题单的局部批次，只处理本批条目，"
            "不重新生成路线，不扩大到其他内容。\n"
            "每个 items 必须恰好输出一条 claim_review，顺序与输入一致，target_path 原样复制。"
            "original_text 必须同时反映 current_claim 和 suggested_duration。\n"
            "direct_evidence 是当前字段挂载的引用；alternative_candidates 是从整个目的地素材库召回的其他候选。"
            "当直接引用不支持但候选支持时，应 repair_reference 或 add_source，不能删除地点。"
            "地点有依据、只有时长或过度具体描述无依据时，判为 partial+narrow，保留游览价值。"
            "只有证据明确相反才用 contradicted；没有证据不等于内容错误。\n"
            "evidence_refs 只能写本批展示的 ref；若一条都没有，写 material_search:<point_name>。"
            "evidence_excerpt 引用用于实际判断的短原文。proposed_text 只给最小修改，无需修改时写“保持原文”。\n"
            f"历史问题摘要（只是待核验假设）：{(finding.issue_summary or '')[-1800:]}\n\n"
            f"本批证据包：{json.dumps(batch, ensure_ascii=False, indent=2)}"
        )
        parsed = _invoke_review_json(model, prompt)
        _validate_claim_reviews_against_scope(
            batch,
            parsed.claim_reviews,
            stage="分批复核",
        )
        results.append(parsed)
    merged = _merge_batched_results(results)
    merged = _refine_batched_claims(
        project_id,
        finding,
        topic,
        model,
        items,
        merged,
        batch_size=batch_size,
    )
    recommendation = persist_recommendation(
        project_id,
        finding,
        merged,
        model=model,
        context_version="review-batched-v2",
        promote=promote,
    )
    return recommendation, merged


def generate_agentic_recommendation(
    project_id: str,
    finding_id: str,
    *,
    channel: str = DEFAULT_REVIEW_MODEL_GROUP,
    promote: bool = True,
) -> tuple[store.ReviewRecommendation, ModelReviewResult]:
    """运行只读 agentic 复核并追加 recommendation。"""
    finding = store.get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    _assert_model_reviewable(finding)
    if channel not in REVIEW_MODEL_GROUPS:
        raise ValueError(f"不支持的复核模型渠道: {channel}")
    if len(_disputed_scope(finding)) > 6:
        return generate_batched_recommendation(project_id, finding_id, channel=channel, promote=promote)
    captured: dict[str, Any] = {}
    model = build_chat_model(group=channel, temperature=0)
    agent = create_react_agent(
        model,
        _make_tools(project_id, finding, captured),
        prompt=_prompt(finding),
    )
    state = agent.invoke(
        {"messages": [{"role": "user", "content": "开始调查并完成这条定向复核。"}]},
        config={"recursion_limit": 14},
    )
    result = captured.get("result")
    if not isinstance(result, ModelReviewResult):
        result = _finalize_investigation(model, finding, state)
    return persist_recommendation(
        project_id,
        finding,
        result,
        model=model,
        context_version="review-agent-v2",
        promote=promote,
    ), result
