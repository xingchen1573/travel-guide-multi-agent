"""定向复核闭环的追加式存储、patch 应用和回滚。

设计约束：

* 项目 workspace 是事实来源；
* finding/review/patch 记录追加写，不覆盖历史；
* 内容 patch 使用源文件 sha256 做乐观版本校验；
* 应用和回滚都通过临时文件 + replace 原子写入；
* 该模块不调用 LLM，只负责确定性存储和变更。
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.review.models import (
    ImprovementSignal,
    PatchOperation,
    PatchRecord,
    ReviewFinding,
    ReviewDecisionFinalization,
    ReviewItemDecision,
    ReviewQueueGroup,
    ReviewRecommendation,
    ReviewRecommendationHistoryEntry,
    ReviewRun,
    TravelReviewSystemObservation,
)
from src.review import policy as review_policy
from src.materials import bm25_search, citation_check
from src.workspace import store as workspace_store
from src.workspace.final_guide_sections import (
    contract_body_markdown,
    normalize_final_guide_section_order,
)
from src.workspace.final_guide_validation import validate_final_guide_contract

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(project_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


@contextmanager
def _finding_write_lock(project_id: str):
    """Serialize state-finding creation across threads and agent processes."""
    lock_path = _workspace(project_id) / ".review_findings.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(project_id):
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace(project_id: str) -> Path:
    return workspace_store.workspace_dir(project_id)


def _review_dir(project_id: str) -> Path:
    path = _workspace(project_id) / "review_runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _findings_path(project_id: str) -> Path:
    return _workspace(project_id) / "review_findings.jsonl"


def _audit_path(project_id: str) -> Path:
    return _workspace(project_id) / "review_log.jsonl"


def _optimization_path(project_id: str) -> Path:
    return _workspace(project_id) / "optimization_candidates.jsonl"


def _improvement_signals_path(project_id: str) -> Path:
    return _workspace(project_id) / "improvement_signals.jsonl"


def _item_decisions_path(project_id: str, recommendation_id: str) -> Path:
    return _review_dir(project_id) / recommendation_id / "item_decisions.jsonl"


def _execution_dir(project_id: str) -> Path:
    path = _review_dir(project_id) / "_executions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _review_run_path(project_id: str, run_id: str) -> Path:
    return _execution_dir(project_id) / run_id / "run.json"


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            result.append(json.loads(line))
    return result


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _safe_workspace_file(project_id: str, relative_file: str) -> Path:
    if not relative_file or Path(relative_file).is_absolute():
        raise ValueError("patch file 必须是 workspace 内的相对路径")
    root = _workspace(project_id).resolve()
    path = (root / relative_file).resolve()
    if path != root and root not in path.parents:
        raise ValueError("patch file 不得离开 project workspace")
    if path.suffix != ".json":
        raise ValueError("MVP patch 只支持 JSON 文件")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _value_fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise ValueError(f"JSON Pointer 必须以 / 开头: {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


_MISSING = object()


def _pointer_get(document: Any, pointer: str) -> Any:
    current = document
    for token in _decode_pointer(pointer):
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return _MISSING
        elif isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        else:
            return _MISSING
    return current


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _find_text_pointer(value: Any, needle: str, pointer: str = "") -> str:
    """在 JSON 中定位模型给出的原文；只返回首个精确或包含命中。"""
    if not needle:
        return ""
    if isinstance(value, str) and (value == needle or needle in value):
        return pointer
    if isinstance(value, dict):
        for key, child in value.items():
            found = _find_text_pointer(child, needle, f"{pointer}/{_pointer_token(str(key))}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_text_pointer(child, needle, f"{pointer}/{index}")
            if found:
                return found
    return ""


def _route_segment_target(document: Any, label: str) -> tuple[str, dict[str, Any] | None, str, str]:
    """根据“路线「途经点」”标签定位旧版建议对应的 route segment。"""
    match = re.search(r"^(.*?)「([^」]+)」", label.strip())
    if not match or not isinstance(document, dict):
        return "", None, "", ""
    route_label = match.group(1).strip().rstrip("：:")
    point_label = match.group(2).strip()
    route_base = route_label.split("（", 1)[0]
    routes = document.get("routes") or []
    for route_index, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        route_name = str(route.get("route_name") or "")
        if route_label not in route_name and route_name not in route_label and route_base not in route_name:
            continue
        for segment_index, segment in enumerate(route.get("route_segments") or []):
            if not isinstance(segment, dict):
                continue
            point_name = str(segment.get("point_name") or "")
            if point_label == point_name or point_label in point_name or point_name in point_label:
                return f"/routes/{route_index}/route_segments/{segment_index}", segment, route_name, point_name
    return "", None, route_label, point_label


def _direct_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    return [item for item in (value.get("sources") or []) if isinstance(item, dict)]


def _matching_failure_points(retry_history: list[Any], point_name: str, claim: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    needles = [item for item in {point_name, claim} if item]
    for round_index, retry in enumerate(retry_history, start=1):
        detail = str(retry)
        for part in [item.strip() for item in detail.split("；") if item.strip()]:
            if any(needle in part for needle in needles):
                matches.append({"round": round_index, "detail": part})
    return matches


def _historical_support_status(failure_points: list[dict[str, Any]], model_verdict: str = "") -> str:
    """区分“引用存在”和“引用是否支持当前主张”。"""
    if model_verdict in {"supported", "partial", "unsupported", "contradicted", "ambiguous"}:
        return model_verdict
    if not failure_points:
        return "unknown"
    detail = "\n".join(str(item.get("detail") or "") for item in failure_points)
    negative_markers = (
        "不支持", "无法实质", "未实质", "没有实质", "缺乏实质", "无对应",
        "没有对应", "没有支持", "未提供", "完全没有", "并未出现", "没有出现", "缺少依据",
    )
    if not any(marker in detail for marker in negative_markers):
        return "unknown"
    partial_markers = (
        "部分有具体对应", "部分元素相关", "部分支持", "并非全部", "虽可支持",
        "虽然提到", "虽然出现", "确实提到", "确实出现", "原文可支持", "原文确实",
    )
    return "partial" if any(marker in detail for marker in partial_markers) else "unsupported"


def _candidate_search_terms(point_name: str, failure_points: list[dict[str, Any]]) -> list[str]:
    def relates_to_point(term: str) -> bool:
        longest = ""
        for start in range(len(term)):
            for end in range(start + 2, len(term) + 1):
                fragment = term[start:end]
                if fragment in point_name and len(fragment) > len(longest):
                    longest = fragment
        if len(longest) >= 3:
            return True
        return len(longest) == 2 and longest not in {"熊猫", "景区", "园区", "区域", "建议", "游览"}

    terms: set[str] = set()
    for part in re.split(r"[/（()）·、，,\s]+", point_name):
        part = part.strip()
        if 2 <= len(part) <= 36:
            terms.add(part)
    for failure in failure_points:
        detail = str(failure.get("detail") or "")
        for term in re.findall(r"[「“‘'\"]([^」”’'\"]{2,36})[」”’'\"]", detail):
            term = term.strip()
            if term and "途经点" not in term and "原文" not in term and relates_to_point(term):
                terms.add(term)
    return sorted(terms, key=lambda item: (-len(item), item))


def _related_source_candidates(
    chunk_map: dict[str, str],
    file_source_ids: list[str],
    evidence_refs: list[str],
    point_name: str,
    failure_points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从同一文件已经挂载的其他引用中找弱相关候选，不冒充支持证据。"""
    terms = _candidate_search_terms(point_name, failure_points)
    direct_refs = set(evidence_refs)
    candidates: list[dict[str, Any]] = []
    for ref in file_source_ids:
        if ref in direct_refs or ref not in chunk_map:
            continue
        text = chunk_map[ref]
        matched_terms = [term for term in terms if term in text]
        if not matched_terms:
            continue
        candidates.append({
            "ref": ref,
            "text": text[:1200],
            "matched_terms": matched_terms[:8],
        })
    candidates.sort(key=lambda item: (-len(item["matched_terms"]), file_source_ids.index(item["ref"])))
    return candidates[:6]


def _library_source_candidates(
    chunk_map: dict[str, str],
    ranked_hits: list[tuple[str, float]],
    excluded_refs: set[str],
    anchor: str,
    failure_points: list[dict[str, Any]],
    preferred_doc_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """保留素材库检索候选及其排序信号，但不把相关性分数当成支持结论。"""
    terms = _candidate_search_terms(anchor, failure_points)
    preferred_doc_ids = preferred_doc_ids or set()
    candidates: list[dict[str, Any]] = []
    for ref, score in ranked_hits:
        if ref in excluded_refs or ref not in chunk_map:
            continue
        text = chunk_map[ref]
        matched_terms = [term for term in terms if term in text][:8]
        doc_id = ref.rsplit("-c", 1)[0] if "-c" in ref else ref
        candidates.append({
            "ref": ref,
            "text": text[:1200],
            "matched_terms": matched_terms,
            "retrieval_method": "bm25",
            "retrieval_score": round(float(score), 4),
            "same_document": doc_id in preferred_doc_ids,
        })
    candidates.sort(key=lambda item: (
        not item["same_document"],
        -max((len(term) for term in item["matched_terms"]), default=0),
        -len(item["matched_terms"]),
        -item["retrieval_score"],
    ))
    return candidates[:6]


def _pointer_parent(document: Any, pointer: str) -> tuple[Any, str]:
    tokens = _decode_pointer(pointer)
    if not tokens:
        raise ValueError("MVP 不允许直接替换整个 JSON 根对象")
    current = document
    for token in tokens[:-1]:
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            if token not in current:
                raise KeyError(pointer)
            current = current[token]
        else:
            raise KeyError(pointer)
    return current, tokens[-1]


def _set_pointer(document: Any, pointer: str, value: Any, *, allow_new: bool) -> None:
    parent, token = _pointer_parent(document, pointer)
    if isinstance(parent, list):
        if token == "-" and allow_new:
            parent.append(value)
            return
        index = int(token)
        if index < 0 or index >= len(parent):
            raise IndexError(pointer)
        parent[index] = value
        return
    if not isinstance(parent, dict):
        raise KeyError(pointer)
    if not allow_new and token not in parent:
        raise KeyError(pointer)
    parent[token] = value


def _remove_pointer(document: Any, pointer: str) -> Any:
    parent, token = _pointer_parent(document, pointer)
    if isinstance(parent, list):
        index = int(token)
        if index < 0 or index >= len(parent):
            raise IndexError(pointer)
        return parent.pop(index)
    if not isinstance(parent, dict) or token not in parent:
        raise KeyError(pointer)
    return parent.pop(token)


def _apply_operations(document: Any, operations: Iterable[PatchOperation]) -> Any:
    result = copy.deepcopy(document)
    for operation in operations:
        current = _pointer_get(result, operation.path)
        exists = current is not _MISSING
        if exists != operation.before_exists:
            raise ValueError(f"patch 前置状态不匹配: {operation.path}")
        if operation.before_exists and current != operation.before:
            raise ValueError(f"patch 前置值不匹配: {operation.path}")
        if operation.op == "replace":
            if not exists:
                raise ValueError(f"replace 目标不存在: {operation.path}")
            _set_pointer(result, operation.path, copy.deepcopy(operation.value), allow_new=False)
        elif operation.op == "add":
            _set_pointer(result, operation.path, copy.deepcopy(operation.value), allow_new=True)
        elif operation.op == "remove":
            if not exists:
                raise ValueError(f"remove 目标不存在: {operation.path}")
            _remove_pointer(result, operation.path)
    return result


def _json_value_kind(value: Any) -> str:
    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _audit(
    project_id: str,
    *,
    review_id: str,
    review_type: str,
    target: dict[str, Any],
    decision: str,
    action: str,
    actor_type: str,
    actor_id: str,
    modified: bool,
    notes: str = "",
    evidence_refs: list[str] | None = None,
) -> None:
    record = {
        "review_id": review_id,
        "timestamp": _now(),
        "review_type": review_type,
        "target": target,
        "decision": decision,
        "action": action,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "modified": modified,
        "llm_called": actor_type == "model",
        "needs_targeted_rerun": decision == "needs_rerun",
        "evidence_refs": evidence_refs or [],
        "confidence": "high" if decision in {"passed", "accepted", "false_positive"} else "medium",
        "notes": notes,
    }
    _append_jsonl(_audit_path(project_id), record)


def _latest_records(path: Path, key: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in _read_jsonl(path):
        identifier = record.get(key)
        if identifier:
            latest[identifier] = record
    return latest


_CAUSE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._:-]*$")


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def normalize_cause_key(root_cause: Any, problem_type: str, cause_key: str = "") -> str:
    """生成可跨项目复用的稳定根因键，并拒绝含展示文案的自由文本键。"""
    raw = cause_key.strip().lower()
    if not raw:
        root = re.sub(r"[^a-z0-9._-]+", "_", _enum_text(root_cause).strip().lower()).strip("_")
        problem = re.sub(r"[^a-z0-9._:-]+", "_", problem_type.strip().lower()).strip("_")
        raw = f"{root or 'system'}:{problem or 'unspecified'}"
    if not _CAUSE_KEY_RE.fullmatch(raw):
        raise ValueError(f"cause_key 格式无效: {cause_key!r}")
    return raw


def _default_affected_layer(root_cause: Any) -> str:
    return {
        "code": "runtime_contract",
        "evidence": "evidence_pipeline",
        "data": "workspace_data",
        "model": "generation_model",
        "presentation": "admin_ui",
        "policy": "review_policy",
    }.get(_enum_text(root_cause), "content_pipeline")


def _default_representative_targets(finding: ReviewFinding | None) -> list[str]:
    if finding is None:
        return []
    target = finding.target or {}
    file = str(target.get("file") or "")
    pointer = str(target.get("json_path") or "")
    if file:
        return [f"{file}#{pointer}" if pointer else file]
    return [finding.finding_id]


def append_improvement_signals(
    project_id: str,
    *,
    finding_id: str,
    recommendation_id: str,
    observations: Iterable[TravelReviewSystemObservation],
    review_run_id: str = "",
    source_type: str = "codex_skill",
    baseline: dict[str, Any] | None = None,
) -> list[ImprovementSignal]:
    """把已通过结果门禁的系统观察投影为待人工确认信号。"""
    finding = get_finding(project_id, finding_id)
    existing = _latest_records(_improvement_signals_path(project_id), "signal_id")
    created: list[ImprovementSignal] = []
    now = _now()
    for index, observation in enumerate(observations):
        if not observation.is_systemic:
            continue
        cause_key = normalize_cause_key(
            observation.root_cause,
            observation.problem_type,
            observation.cause_key,
        )
        signature = json.dumps(
            [project_id, finding_id, recommendation_id, cause_key, index],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        signal_id = f"improvement-signal-{hashlib.sha256(signature.encode('utf-8')).hexdigest()[:16]}"
        if signal_id in existing:
            created.append(ImprovementSignal.model_validate(existing[signal_id]))
            continue
        signal = ImprovementSignal(
            signal_id=signal_id,
            project_id=project_id,
            finding_id=finding_id,
            recommendation_id=recommendation_id,
            review_run_id=review_run_id,
            source_type=source_type,
            cause_key=cause_key,
            root_cause=observation.root_cause,
            affected_layer=observation.affected_layer or _default_affected_layer(observation.root_cause),
            problem_type=observation.problem_type,
            hypothesis=observation.summary,
            evidence_refs=observation.evidence_refs,
            representative_targets=(
                observation.representative_targets or _default_representative_targets(finding)
            ),
            suggested_fix=observation.suggested_fix,
            suggested_test=observation.suggested_test,
            severity=observation.severity,
            confidence=observation.confidence,
            migration_required=observation.migration_required,
            regression_scope=observation.regression_scope,
            baseline=baseline or {},
            created_at=now,
            updated_at=now,
        )
        with _lock(project_id):
            _append_jsonl(_improvement_signals_path(project_id), signal.model_dump(mode="json"))
            _audit(
                project_id,
                review_id=signal.signal_id,
                review_type="improvement_signal",
                target={"finding_id": finding_id, "cause_key": cause_key},
                decision="pending",
                action="create_improvement_signal",
                actor_type="model",
                actor_id=source_type,
                modified=False,
                notes=signal.hypothesis,
                evidence_refs=signal.evidence_refs,
            )
        created.append(signal)
    return created


def read_improvement_signals(project_id: str) -> list[ImprovementSignal]:
    latest = _latest_records(_improvement_signals_path(project_id), "signal_id")
    signals: list[ImprovementSignal] = []
    for raw in latest.values():
        try:
            signals.append(ImprovementSignal.model_validate(raw))
        except ValueError:
            continue
    return sorted(signals, key=lambda item: (item.updated_at, item.signal_id), reverse=True)


def decide_improvement_signal(
    project_id: str,
    signal_id: str,
    *,
    decision: str,
    reason: str = "",
    actor_id: str = "admin_user",
    cause_key: str = "",
    root_cause: str = "",
    affected_layer: str = "",
    comparison_pair_id: str | None = None,
) -> ImprovementSignal:
    allowed = {"accepted", "dismissed", "deferred", "merged", "reclassified"}
    if decision not in allowed:
        raise ValueError(f"不支持的 Improvement Signal 决定: {decision}")
    current = next((item for item in read_improvement_signals(project_id) if item.signal_id == signal_id), None)
    if current is None:
        raise KeyError(f"Improvement Signal 不存在: {signal_id}")
    updates: dict[str, Any] = {
        "decision": decision,
        "decision_reason": reason,
        "decided_by": actor_id,
        "updated_at": _now(),
    }
    if comparison_pair_id is not None:
        updates["comparison_pair_id"] = comparison_pair_id or None
    if decision in {"merged", "reclassified"}:
        updates["cause_key"] = normalize_cause_key(
            root_cause or current.root_cause,
            current.problem_type,
            cause_key or current.cause_key,
        )
        if root_cause:
            updates["root_cause"] = root_cause
        if affected_layer:
            updates["affected_layer"] = affected_layer
    updated = current.model_copy(update=updates)
    with _lock(project_id):
        _append_jsonl(_improvement_signals_path(project_id), updated.model_dump(mode="json"))
        _audit(
            project_id,
            review_id=signal_id,
            review_type="improvement_signal_decision",
            target={
                "finding_id": updated.finding_id,
                "cause_key": updated.cause_key,
                "comparison_pair_id": updated.comparison_pair_id,
            },
            decision=decision,
            action="decide_improvement_signal",
            actor_type="human",
            actor_id=actor_id,
            modified=False,
            notes=reason,
            evidence_refs=updated.evidence_refs,
        )
    return updated


def sync_improvement_signals_from_artifacts(project_id: str) -> list[ImprovementSignal]:
    """为升级前已导入的 Skill 结果补建信号；重复调用保持幂等。"""
    from src.review.models import TravelReviewResult

    runs_by_recommendation = {
        run.recommendation_id: run.run_id
        for run in read_review_runs(project_id)
        if run.recommendation_id
    }
    synced: list[ImprovementSignal] = []
    for path in _review_dir(project_id).glob("recommendation-*/travel_review_result.json"):
        try:
            result = TravelReviewResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if result.review_trace.status != "completed" or result.review_trace.errors:
            continue
        recommendation = result.recommendation
        synced.extend(append_improvement_signals(
            project_id,
            finding_id=recommendation.finding_id,
            recommendation_id=recommendation.recommendation_id,
            observations=result.system_observations,
            review_run_id=runs_by_recommendation.get(recommendation.recommendation_id, ""),
            source_type="codex_skill" if result.review_trace.model_group == "codex_skill" else "external_skill",
            baseline={"bundle_sha256": result.bundle_sha256},
        ))
    return synced


def _generic_finding_explanation(finding: ReviewFinding) -> tuple[str, str]:
    """为没有专用说明的 finding 生成可读的通用解释。"""
    target = finding.target or {}
    file = str(target.get("file") or "局部内容")
    pointer = str(target.get("json_path") or "")
    location = f"{file}{pointer}" if pointer else file
    observed = finding.observed_value
    if finding.problem_type == "legacy_content_review":
        summary = f"历史复核记录指向 {location}，当前需要重新确认内容与证据是否一致。"
        question = "查看当前值和证据后，判断是已解决、误报、暂缓，还是需要定向重跑。"
    elif finding.problem_type == "manifest_mismatch":
        summary = f"生成清单与文件状态不一致：{location}。"
        question = "确认缺失或不一致是否只影响对应局部文件，并决定同步状态还是定向重跑。"
    elif finding.problem_type == "force_finalized_residue":
        summary = f"历史核验状态仍未闭环：{location}。"
        question = "检查当前值、失败记录和证据，判断内容是否正确，还是只需要补核验并同步状态。"
    else:
        summary = f"检测到 {finding.problem_type}：目标为 {location}。"
        question = "根据当前值、证据和历史记录，判断是否需要局部修改、标记误报、暂缓或定向重跑。"
    if observed is not None and finding.problem_type not in {"force_finalized_residue", "manifest_mismatch"}:
        summary += f" 当前观测值：{json.dumps(observed, ensure_ascii=False)[:240]}。"
    return summary, question


def _with_generic_explanation(finding: ReviewFinding) -> ReviewFinding:
    if finding.issue_summary and finding.review_question:
        return finding
    summary, question = _generic_finding_explanation(finding)
    return finding.model_copy(update={
        "issue_summary": finding.issue_summary or summary,
        "review_question": finding.review_question or question,
        "notes": finding.notes or finding.issue_summary or summary,
    })


def create_finding(
    project_id: str,
    *,
    problem_type: str,
    target: dict[str, Any],
    root_cause: str = "content",
    severity: str = "medium",
    rerun_cost: str = "medium",
    evidence_refs: list[str] | None = None,
    before: Any = None,
    notes: str = "",
    actor_type: str = "system",
    actor_id: str = "review_engine",
) -> ReviewFinding:
    """创建一个 finding，并以追加记录方式写入项目 workspace。"""
    now = _now()
    finding = ReviewFinding(
        finding_id=f"finding-{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        baseline_version=now,
        target=target,
        problem_type=problem_type,
        root_cause=root_cause,
        severity=severity,
        rerun_cost=rerun_cost,
        evidence_refs=evidence_refs or [],
        before=before,
        notes=notes,
        created_at=now,
        updated_at=now,
    )
    with _lock(project_id):
        _append_jsonl(_findings_path(project_id), finding.model_dump(mode="json"))
        _audit(
            project_id,
            review_id=finding.finding_id,
            review_type="finding",
            target=target,
            decision="detected",
            action="create_finding",
            actor_type=actor_type,
            actor_id=actor_id,
            modified=False,
            notes=notes,
            evidence_refs=finding.evidence_refs,
        )
        workspace_store.log_event(
            project_id,
            actor_id or actor_type,
            "review_finding_detected",
            f"review_findings/{finding.finding_id}",
            notes or f"发现定向复核问题：{problem_type}",
        )
    return finding


def read_findings(project_id: str) -> list[ReviewFinding]:
    latest = _latest_records(_findings_path(project_id), "finding_id")
    return [_with_generic_explanation(ReviewFinding.model_validate(item)) for item in latest.values()]


def _finding_object_label(finding: ReviewFinding) -> str:
    target = finding.target or {}
    if target.get("object_label"):
        return str(target["object_label"])
    observed = finding.observed_value if isinstance(finding.observed_value, dict) else {}
    if observed.get("poi_name") or observed.get("name"):
        return str(observed.get("poi_name") or observed.get("name"))
    if observed.get("recommendation"):
        return str(observed["recommendation"])[:48]
    if observed.get("warning"):
        return str(observed["warning"])[:48]
    file = str(target.get("missing_file") or target.get("file") or "局部内容")
    return Path(file).stem or "局部内容"


def _mirror_canonical_key(finding: ReviewFinding) -> str:
    target = finding.target or {}
    file = str(target.get("file") or "")
    if file == "review_runs":
        residue_path = str(target.get("residue_path") or "")
        marker = residue_path.find(".json")
        bundle_file = residue_path[: marker + 5] if marker >= 0 else ""
        bundle = _read_workspace_json(finding.project_id, bundle_file) if bundle_file else None
        frozen_finding = bundle.get("finding") if isinstance(bundle, dict) else None
        if isinstance(frozen_finding, dict):
            original_id = str(frozen_finding.get("finding_id") or "")
            original = get_finding(finding.project_id, original_id) if original_id else None
            if original is not None:
                return _legacy_canonical_key(original)
            try:
                return _legacy_canonical_key(ReviewFinding.model_validate(frozen_finding))
            except ValueError:
                return ""
    if "/workspace/" in file:
        mirrored_file = file.split("/workspace/", 1)[1]
        mirrored = finding.model_copy(update={"target": {**target, "file": mirrored_file}})
        return _legacy_canonical_key(mirrored)
    if file == "outputs/poi_details/manifest.json" and finding.problem_type == "force_finalized_residue":
        observed = finding.observed_value if isinstance(finding.observed_value, dict) else {}
        detail_file = str(observed.get("file") or "")
        if detail_file:
            detail = _read_workspace_json(finding.project_id, f"outputs/poi_details/{detail_file}")
            if isinstance(detail, dict):
                day_number = detail.get("day", observed.get("day", "unknown"))
                poi_name = str(detail.get("poi_name") or detail.get("name") or Path(detail_file).stem)
                return f"poi_detail:{day_number}:{poi_name}"
    return ""


def build_review_queue(project_id: str, *, include_closed: bool = True) -> list[ReviewQueueGroup]:
    """构造面向人工处理的队列投影，不改变底层 finding 审计记录。"""
    findings = read_findings(project_id)
    grouped: dict[str, list[ReviewFinding]] = {}
    for finding in findings:
        key = _legacy_canonical_key(finding) or _mirror_canonical_key(finding) or f"finding:{finding.finding_id}"
        grouped.setdefault(key, []).append(finding)

    result: list[ReviewQueueGroup] = []
    for key, members in grouped.items():
        primary = next(
            (
                item for item in members
                if not item.superseded_by
                and item.finding_kind != "derived_mirror"
                and not str((item.target or {}).get("file") or "").startswith("review_runs/")
                and not (
                    str((item.target or {}).get("file") or "") == "outputs/poi_details/manifest.json"
                    and item.problem_type == "force_finalized_residue"
                )
            ),
            members[0],
        )
        if not include_closed and primary.status == "closed":
            continue
        recommendation = read_recommendation(project_id, primary.recommendation_id) if primary.recommendation_id else None
        decisions = read_item_decisions(project_id, primary.recommendation_id) if primary.recommendation_id else []
        if recommendation and recommendation.claim_reviews:
            total = len(recommendation.claim_reviews)
        elif recommendation:
            total = len(_legacy_recommendation_items(recommendation.proposed_text))
        else:
            total = 0
        runs = read_review_runs(project_id, primary.finding_id)
        if any(run.status in {"queued", "running", "validating"} for run in runs):
            model_status = "running"
        elif recommendation:
            model_status = "ready"
        elif runs and all(run.status in {"failed", "cancelled", "interrupted"} for run in runs):
            model_status = "failed"
        else:
            model_status = "not_started"
        disposition = getattr(primary.disposition, "value", str(primary.disposition))
        effective_status = "review_passed" if primary.status == "closed" and disposition in {"resolved", "false_positive"} else primary.status
        object_label = _finding_object_label(primary)
        title = object_label
        if primary.problem_type == "manifest_mismatch":
            title = f"POI 文件清单不一致 · {object_label}"
        elif primary.problem_type == "force_finalized_residue":
            title = f"引用核验待确认 · {object_label}"
        elif primary.problem_type == "final_guide_contract_violation":
            title = "最终攻略交付不完整"
        result.append(ReviewQueueGroup(
            group_id=f"queue:{key}",
            primary_finding_id=primary.finding_id,
            related_finding_ids=[item.finding_id for item in members if item.finding_id != primary.finding_id],
            title=title,
            summary=primary.issue_summary or primary.notes,
            object_label=object_label,
            finding_kind=primary.finding_kind,
            root_cause=str(getattr(primary.root_cause, "value", primary.root_cause)),
            effective_status=effective_status,
            severity=primary.severity,
            model_status=model_status,
            decision_progress={"decided": len(decisions), "total": total},
            mirror_count=max(0, len(members) - 1),
            technical={
                "canonical_key": key if not key.startswith("finding:") else "",
                "problem_type": primary.problem_type,
                "target": primary.target,
            },
        ))
    return sorted(result, key=lambda item: (item.effective_status == "review_passed", item.title, item.group_id))


def backfill_findings_from_review_log(project_id: str) -> list[ReviewFinding]:
    """将旧 review_log 的内容复核记录迁移为当前 finding，幂等且不调用模型。"""
    existing = read_findings(project_id)
    source_ids = {item.source_review_id for item in existing if item.source_review_id}
    created: list[ReviewFinding] = []
    for record in _read_jsonl(_audit_path(project_id)):
        if record.get("review_type") != "content_review":
            continue
        source_id = record.get("review_id")
        if not source_id or source_id in source_ids:
            continue
        decision = str(record.get("decision") or "")
        if decision not in {"passed_review", "false_positive", "deferred", "needs_rerun", "inconclusive"}:
            continue
        target = record.get("target") or {}
        if not isinstance(target, dict):
            target = {"scope": str(target)}
        needs_rerun = bool(record.get("needs_targeted_rerun")) or decision in {"needs_rerun", "inconclusive"}
        disposition = "needs_rerun" if needs_rerun else {"passed_review": "resolved", "false_positive": "false_positive", "deferred": "deferred"}.get(decision, "deferred")
        now = record.get("timestamp") or _now()
        finding = ReviewFinding(
            finding_id=f"finding-legacy-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            baseline_version=now,
            target=target,
            problem_type="legacy_content_review",
            disposition=disposition,
            root_cause="content",
            severity="medium",
            rerun_cost="high" if needs_rerun else "low",
            evidence_refs=record.get("evidence_refs") or [],
            verification_method="legacy_review_log_backfill",
            verification_status=decision,
            verification_actor_type="model" if record.get("llm_called") else "system",
            verification_actor_id=record.get("actor_id") or ("strong_model" if record.get("llm_called") else "state_scanner"),
            needs_rerun=needs_rerun,
            status="closed" if disposition in {"resolved", "false_positive"} else "open",
            notes=record.get("notes") or "历史复核记录迁移",
            created_at=now,
            updated_at=now,
            actor_id=record.get("actor_id") or ("model" if record.get("llm_called") else "system"),
            source_review_id=source_id,
        )
        with _lock(project_id):
            _append_jsonl(_findings_path(project_id), finding.model_dump(mode="json"))
            _audit(project_id, review_id=finding.finding_id, review_type="finding_backfill", target=target, decision="backfilled", action="backfill_review_log", actor_type="system", actor_id="review_migration", modified=False, notes=f"source_review_id={source_id}", evidence_refs=finding.evidence_refs)
            workspace_store.log_event(project_id, "review_migration", "review_finding_backfilled", f"review_findings/{finding.finding_id}", finding.notes)
        source_ids.add(source_id)
        created.append(finding)
    return created


def _read_workspace_json(project_id: str, relative_file: str) -> Any:
    path = _workspace(project_id) / relative_file
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _state_issue_explanation(observed: Any) -> tuple[str, str]:
    missing_entities = observed.get("missing_entities", []) if isinstance(observed, dict) else []
    retry_history = observed.get("retry_history", []) if isinstance(observed, dict) else []
    if missing_entities:
        summary = f"引用核验未覆盖实体：{'、'.join(str(item) for item in missing_entities[:8])}。"
        question = "逐个检查当前 sources 是否支持这些实体；有证据就补引用并回填通过，没有证据就收窄或删除对应具体表述。"
    elif retry_history:
        summary = f"历史引用核验未通过，最近一次失败记录：{str(retry_history[-1])[:360]}"
        question = "检查失败记录对应的具体字段和证据，判断是补证据、收窄措辞，还是只需重新核验该对象。"
    else:
        summary = "历史引用核验尚未闭环；原始 citation_status 为 force_finalized，但这不等于正文一定错误。"
        question = "检查当前内容与证据；内容正确时完成复核即可，证据不足时再补证据、收窄措辞或定向重跑。"
    return summary, question


_ORIGIN_AGENT_LABELS = {
    "route_plan.json": "路线规划 Agent",
    "outputs/lifestyle.json": "衣食住行 Agent",
    "outputs/pitfall.json": "避雷 Agent",
}

_CLAIM_FIELD_LABELS = {
    "name": "名称",
    "brief": "简介",
    "notes": "行程备注",
    "needs_detail": "是否需要深度玩法",
    "needs_detail_reason": "深度玩法判断",
    "recommendation": "推荐内容",
    "highlight": "亮点描述",
    "suggested_duration": "建议时长",
}

_FAILED_CHECK_RESULTS = {"fail", "failed", "error", "inconclusive", "unsupported", "partial"}

_ENTITY_SCAN_METADATA_KEYS = {
    "auto_collected",
    "claimed_entities",
    "claims",
    "evidence_refs",
    "evidence_snapshots",
    "missing_entities",
    "retry_history",
    "sources",
}

_VERIFICATION_ROUND_PREFIX = re.compile(r"^第(?P<round>\d+)轮[：:]")
_VERIFICATION_FAILED_CLAIM_MARKER = re.compile(r"的失败claim：claim_id=")


def _verification_history_projection(
    value: Any,
    *,
    current_blocker_count: int | None,
    current_claim_count: int | None,
) -> dict[str, Any]:
    """Project legacy retry strings into an honest, typed verification timeline.

    ``retry_history`` remains the immutable audit source.  This projection is a
    read-time backfill for old Projects, so opening Review Center immediately
    gains the same semantics as newly generated content without rewriting
    historical business artifacts.
    """
    retry_history = value.get("retry_history", []) if isinstance(value, dict) else []
    if not isinstance(retry_history, list):
        retry_history = []

    rounds: list[dict[str, Any]] = []
    other_events: list[dict[str, Any]] = []
    normalized_history = [str(item) for item in retry_history if str(item).strip()]
    for history_index, detail in enumerate(normalized_history):
        match = _VERIFICATION_ROUND_PREFIX.match(detail)
        if match is None:
            other_events.append({
                "history_index": history_index,
                "label": detail.split("：", 1)[0][:80] or f"历史记录 {history_index + 1}",
                "raw_detail": detail,
            })
            continue
        issue_count = len(_VERIFICATION_FAILED_CLAIM_MARKER.findall(detail))
        rounds.append({
            "history_index": history_index,
            "round": int(match.group("round")),
            # Some very old records predate claim-level formatting.  Returning
            # null is safer than inventing a count by splitting punctuation in
            # a model-written failure reason.
            "issue_count": issue_count or None,
            "raw_detail": detail,
            "is_latest": False,
            "superseded": False,
        })

    if rounds:
        rounds[-1]["is_latest"] = True
        for round_item in rounds[:-1]:
            round_item["superseded"] = True

    source_hash = hashlib.sha256(
        json.dumps(normalized_history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    progression = [item["issue_count"] for item in rounds]
    return {
        "source": "retry_history_read_projection",
        "source_hash": source_hash,
        "round_count": len(rounds),
        "progression": progression,
        "current_blocker_count": current_blocker_count,
        "current_claim_count": current_claim_count,
        "current_source": "latest_evidence_checks" if current_blocker_count is not None else "unassessed",
        "rounds": rounds,
        "other_events": other_events,
    }


def _origin_agent_label(file: str) -> str:
    if file.startswith("outputs/poi_details/"):
        return "POI 详情 Agent"
    return _ORIGIN_AGENT_LABELS.get(file, "生成 Agent")


def _collect_claim_records(value: Any) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            raw_claims = node.get("claims")
            if isinstance(raw_claims, list):
                claims.extend(item for item in raw_claims if isinstance(item, dict) and item.get("claim_id"))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return claims


def _dotted_path_get(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _claim_owner(value: Any, claim_id: str, pointer: str = "") -> tuple[Any, str]:
    """Locate the object whose ``claims`` array contains ``claim_id``."""
    if isinstance(value, dict):
        raw_claims = value.get("claims")
        if isinstance(raw_claims, list) and any(
            isinstance(item, dict) and str(item.get("claim_id") or "") == claim_id
            for item in raw_claims
        ):
            return value, pointer
        for key, child in value.items():
            owner, owner_pointer = _claim_owner(child, claim_id, f"{pointer}/{key}")
            if owner is not None:
                return owner, owner_pointer
    elif isinstance(value, list):
        for index, child in enumerate(value):
            owner, owner_pointer = _claim_owner(child, claim_id, f"{pointer}/{index}")
            if owner is not None:
                return owner, owner_pointer
    return None, ""


def _claim_origin_contexts(
    project_id: str,
    *,
    file: str,
    pointer: str,
    target: dict[str, Any],
    current_content: Any,
) -> list[dict[str, Any]]:
    """Join a coarse state finding back to the exact claims and checks that caused it."""
    section = (
        "route" if file == "route_plan.json"
        else "lifestyle" if file == "outputs/lifestyle.json"
        else "pitfall" if file == "outputs/pitfall.json"
        else "poi_detail" if file.startswith("outputs/poi_details/")
        else ""
    )
    if not section:
        return []
    evidence_checks = workspace_store.read_evidence_checks(project_id) or {}
    section_checks = evidence_checks.get(section, []) if isinstance(evidence_checks, dict) else []
    if not isinstance(section_checks, list):
        return []

    latest_by_claim_scope: dict[tuple[str, str], dict[str, Any]] = {}
    for check in section_checks:
        if not isinstance(check, dict) or not check.get("claim_id"):
            continue
        claim_id = str(check["claim_id"])
        scope = str(check.get("scope") or "unknown")
        key = (claim_id, scope)
        previous = latest_by_claim_scope.get(key)
        if previous is None or str(check.get("checked_at") or "") >= str(previous.get("checked_at") or ""):
            latest_by_claim_scope[key] = check

    claims = _collect_claim_records(current_content)
    if not claims:
        return []
    missing_entities = {
        str(item) for item in (
            current_content.get("missing_entities", [])
            if isinstance(current_content, dict) and isinstance(current_content.get("missing_entities"), list)
            else []
        ) if item
    }
    checks_by_claim: dict[str, list[dict[str, Any]]] = {}
    for (claim_id, _scope), check in latest_by_claim_scope.items():
        checks_by_claim.setdefault(claim_id, []).append(check)

    candidates: list[dict[str, Any]] = []
    fallback_candidates: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        checks = checks_by_claim.get(claim_id, [])
        failures = [check for check in checks if str(check.get("result") or "").lower() in _FAILED_CHECK_RESULTS]
        if not failures:
            continue
        fallback_candidates.append(claim)
        if not missing_entities:
            candidates.append(claim)
            continue
        claim_blob = " ".join([
            str(claim.get("claim_text") or ""),
            *(str(item) for item in claim.get("claimed_entities", []) if item),
            *(str(check.get("reason") or "") for check in failures),
            *(str(item) for check in failures for item in (check.get("missing_entities") or []) if item),
        ])
        if any(entity in claim_blob for entity in missing_entities):
            candidates.append(claim)
    if not candidates:
        candidates = fallback_candidates

    day = current_content.get("day") if isinstance(current_content, dict) else None
    contexts: list[dict[str, Any]] = []
    for claim in candidates[:8]:
        claim_id = str(claim.get("claim_id") or "")
        checks = sorted(checks_by_claim.get(claim_id, []), key=lambda item: str(item.get("checked_at") or ""))
        target_path = str(claim.get("target_path") or "")
        field_key = target_path.rsplit(".", 1)[-1] if target_path else ""
        subject = str(claim.get("subject") or "").strip()
        owner, owner_pointer = _claim_owner(current_content, claim_id)
        current_value = _dotted_path_get(owner, target_path)
        json_pointer = pointer.rstrip("/") + owner_pointer
        if target_path:
            json_pointer += "/" + "/".join(target_path.split("."))
        location_check = next(
            (
                check for check in checks
                if check.get("route_name") or check.get("point_name") or check.get("field") or check.get("day")
            ),
            {},
        )
        route_name = str(location_check.get("route_name") or "")
        point_name = str(location_check.get("point_name") or subject)
        order = location_check.get("order")
        group_location = [
            route_name,
            str(order) if order is not None else "",
            point_name,
            str(location_check.get("field") or ""),
            str(location_check.get("day") or day or ""),
        ]
        issue_group_key = "|".join(group_location).strip("|") or claim_id
        summarized_checks = []
        for check in checks:
            cited_ids = check.get("cited_chunk_ids") or [
                source.get("chunk_id")
                for source in (check.get("cited_sources") or [])
                if isinstance(source, dict) and source.get("chunk_id")
            ]
            summarized_checks.append({
                "scope": str(check.get("scope") or "unknown"),
                "result": str(check.get("result") or "unknown"),
                "method": str(check.get("method") or ""),
                "reason": str(check.get("reason") or ""),
                "missing_entities": [str(item) for item in (check.get("missing_entities") or []) if item],
                "cited_source_count": len(set(str(item) for item in cited_ids if item)),
                "checked_at": str(check.get("checked_at") or ""),
            })
        contexts.append({
            "agent_label": _origin_agent_label(file),
            "day": day,
            "subject": subject,
            "field_label": _CLAIM_FIELD_LABELS.get(field_key, field_key or "内容字段"),
            "claim_id": claim_id,
            "claim_text": str(claim.get("claim_text") or ""),
            "claim_type": str(claim.get("claim_type") or ""),
            "support_mode": str(claim.get("support_mode") or ""),
            "target_path": target_path,
            "json_pointer": json_pointer or None,
            "current_value": current_value,
            "route_name": route_name,
            "point_name": point_name,
            "order": order,
            "issue_group_key": issue_group_key,
            "claimed_entities": [str(item) for item in (claim.get("claimed_entities") or []) if item],
            "missing_entities": sorted({
                str(item)
                for check in checks
                for item in (check.get("missing_entities") or [])
                if item
            } | missing_entities),
            "checks": summarized_checks,
            "finding_scope_label": str(target.get("object_label") or target.get("scope") or "当前复核"),
        })
    return contexts


def _entity_text_excerpt(text: str, entity: str, *, radius: int = 90) -> str:
    """Return a short, stable excerpt around an exact entity mention."""
    normalized = " ".join(str(text).split())
    index = normalized.find(entity)
    if index < 0 or len(normalized) <= radius * 2:
        return normalized[: radius * 2]
    start = max(0, index - radius)
    end = min(len(normalized), index + len(entity) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(normalized) else ""
    return f"{prefix}{normalized[start:end]}{suffix}"


def _timestamp_precedes(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        left_time = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_time = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except ValueError:
        return False
    return left_time < right_time


def _entity_occurrences(
    value: Any,
    entity: str,
    *,
    pointer: str = "",
    subject: str = "",
) -> list[dict[str, Any]]:
    """Locate business-field mentions while excluding evidence/audit metadata.

    ``missing_entities`` historically contains duplicate labels but no field
    location.  Counting leaf fields instead of that list gives reviewers the
    meaningful number: how many current statements actually need a decision.
    """
    occurrences: list[dict[str, Any]] = []
    if isinstance(value, dict):
        current_subject = str(
            value.get("name")
            or value.get("poi_name")
            or value.get("point_name")
            or value.get("subject")
            or subject
            or ""
        )
        for key, child in value.items():
            if key in _ENTITY_SCAN_METADATA_KEYS:
                continue
            child_pointer = f"{pointer}/{_pointer_token(str(key))}"
            if isinstance(child, str) and entity in child:
                occurrences.append({
                    "json_pointer": child_pointer,
                    "field_label": _CLAIM_FIELD_LABELS.get(str(key), str(key)),
                    "subject": current_subject,
                    "current_value": child,
                    "excerpt": _entity_text_excerpt(child, entity),
                })
            elif isinstance(child, (dict, list)):
                occurrences.extend(
                    _entity_occurrences(
                        child,
                        entity,
                        pointer=child_pointer,
                        subject=current_subject,
                    )
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_pointer = f"{pointer}/{index}"
            if isinstance(child, str) and entity in child:
                occurrences.append({
                    "json_pointer": child_pointer,
                    "field_label": "文本内容",
                    "subject": subject,
                    "current_value": child,
                    "excerpt": _entity_text_excerpt(child, entity),
                })
            elif isinstance(child, (dict, list)):
                occurrences.extend(
                    _entity_occurrences(
                        child,
                        entity,
                        pointer=child_pointer,
                        subject=subject,
                    )
                )
    return occurrences


def _latest_claim_checks(checks: list[Any]) -> dict[str, list[dict[str, Any]]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_check in checks:
        if not isinstance(raw_check, dict) or not raw_check.get("claim_id"):
            continue
        claim_id = str(raw_check["claim_id"])
        scope = str(raw_check.get("scope") or "unknown")
        key = (claim_id, scope)
        previous = latest.get(key)
        if previous is None or str(raw_check.get("checked_at") or "") >= str(previous.get("checked_at") or ""):
            latest[key] = raw_check
    grouped: dict[str, list[dict[str, Any]]] = {}
    for (claim_id, _scope), check in latest.items():
        grouped.setdefault(claim_id, []).append(check)
    for claim_checks in grouped.values():
        claim_checks.sort(key=lambda item: (str(item.get("checked_at") or ""), str(item.get("scope") or "")))
    return grouped


def _claim_evidence_excerpts(
    claim: dict[str, Any],
    chunk_map: dict[str, str],
    *,
    entity: str,
) -> list[dict[str, Any]]:
    excerpts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in claim.get("evidence_snapshots", []) or []:
        if not isinstance(snapshot, dict):
            continue
        source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
        ref = str(source.get("chunk_id") or snapshot.get("chunk_id") or "")
        text = str(snapshot.get("excerpt") or snapshot.get("text") or "")
        key = ref or text
        if not text or key in seen:
            continue
        seen.add(key)
        excerpts.append({
            "ref": ref,
            "text": _entity_text_excerpt(text, entity),
            "mentions_entity": entity in text,
            "origin": "embedded_snapshot",
        })
    for source in claim.get("evidence_refs", []) or []:
        if isinstance(source, dict):
            ref = str(source.get("chunk_id") or "")
        else:
            ref = str(source or "")
        text = chunk_map.get(ref, "")
        if not ref or not text or ref in seen:
            continue
        seen.add(ref)
        excerpts.append({
            "ref": ref,
            "text": _entity_text_excerpt(text, entity),
            "mentions_entity": entity in text,
            "origin": "material_library",
        })
    return excerpts[:3]


def _entity_downstream_evidence(
    project_id: str,
    entity: str,
    *,
    chunk_map: dict[str, str],
    checks_by_claim: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Find exact, claim-level POI evidence produced after route planning."""
    details_dir = _workspace(project_id) / "outputs" / "poi_details"
    if not details_dir.exists():
        return []
    matches: list[dict[str, Any]] = []
    for path in sorted(details_dir.glob("day*.json")):
        detail = _read_workspace_json(project_id, f"outputs/poi_details/{path.name}")
        if not isinstance(detail, dict):
            continue
        for claim in _collect_claim_records(detail):
            claim_id = str(claim.get("claim_id") or "")
            claim_blob = " ".join([
                str(claim.get("claim_text") or ""),
                *(str(item) for item in (claim.get("claimed_entities") or []) if item),
            ])
            if entity not in claim_blob:
                continue
            owner, owner_pointer = _claim_owner(detail, claim_id)
            target_path = str(claim.get("target_path") or "")
            current_value = _dotted_path_get(owner, target_path)
            json_pointer = owner_pointer
            if target_path:
                json_pointer += "/" + "/".join(_pointer_token(part) for part in target_path.split("."))
            checks = checks_by_claim.get(claim_id, [])
            failed = [
                check for check in checks
                if str(check.get("result") or "").lower() in _FAILED_CHECK_RESULTS
            ]
            passed = [
                check for check in checks
                if str(check.get("result") or "").lower() in {"pass", "passed"}
            ]
            entity_support_passed = [
                check for check in passed
                if str(check.get("scope") or "") in {"entity_identity", "substantive_claim"}
            ]
            if failed:
                status = "failed"
            elif entity_support_passed:
                status = "passed"
            else:
                status = "unassessed"
            matches.append({
                "file": f"outputs/poi_details/{path.name}",
                "poi_name": str(detail.get("poi_name") or ""),
                "day": detail.get("day"),
                "claim_id": claim_id,
                "claim_text": str(claim.get("claim_text") or ""),
                "json_pointer": json_pointer or None,
                "current_value": current_value,
                "validation_status": status,
                "passed_scope_count": len(passed),
                "failed_scope_count": len(failed),
                "checked_at": max((str(check.get("checked_at") or "") for check in checks), default=""),
                "evidence_excerpts": _claim_evidence_excerpts(claim, chunk_map, entity=entity),
            })
    matches.sort(
        key=lambda item: (
            item["validation_status"] != "passed",
            str(item.get("checked_at") or ""),
            str(item.get("file") or ""),
        )
    )
    return matches[:6]


def _final_guide_entity_occurrences(project_id: str, entity: str) -> list[dict[str, Any]]:
    path = _workspace(project_id) / "final_guide.md"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    blocks = workspace_store.read_final_guide_blocks(project_id)
    matching_blocks: list[Any] = []
    if blocks is not None:
        matching_blocks = [block for block in blocks.blocks if entity in block.markdown]
    occurrences: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if entity not in line:
            continue
        normalized_line = line.strip()
        block = next(
            (
                candidate for candidate in matching_blocks
                if normalized_line and normalized_line in candidate.markdown
            ),
            None,
        )
        occurrences.append({
            "file": "final_guide.md",
            "line": line_number,
            "excerpt": _entity_text_excerpt(line, entity),
            # final_guide.blocks.json only maps an entire Markdown block to a
            # set of upstream claims.  It cannot prove that one exact line was
            # produced from one exact claim, so keep this an honest text match.
            "match_mode": "text_match",
            "claim_linked": False,
            "block_id": block.block_id if block is not None else None,
            "section": block.section if block is not None else "",
            "upstream_claim_ids": [],
        })
    return occurrences[:6]


def _build_missing_entity_review(
    project_id: str,
    *,
    finding: ReviewFinding,
    file: str,
    pointer: str,
    current_content: Any,
    content_updated_at: str,
    origin_claims: list[dict[str, Any]],
    attached_sources: list[dict[str, Any]],
    source_excerpts: list[dict[str, Any]],
    chunk_map: dict[str, str],
    material_bm25_index: bm25_search.BM25SearchIndex | None,
) -> dict[str, Any] | None:
    raw_entities = (
        current_content.get("missing_entities", [])
        if isinstance(current_content, dict) and isinstance(current_content.get("missing_entities"), list)
        else []
    )
    entities = list(dict.fromkeys(str(item).strip() for item in raw_entities if str(item).strip()))
    if not entities:
        return None

    section = (
        "route" if file == "route_plan.json"
        else "lifestyle" if file == "outputs/lifestyle.json"
        else "pitfall" if file == "outputs/pitfall.json"
        else "poi_detail" if file.startswith("outputs/poi_details/")
        else ""
    )
    evidence_checks = workspace_store.read_evidence_checks(project_id) or {}
    section_checks = evidence_checks.get(section, []) if isinstance(evidence_checks, dict) else []
    poi_checks = evidence_checks.get("poi_detail", []) if isinstance(evidence_checks, dict) else []
    current_claim_ids = {
        str(claim.get("claim_id") or "")
        for claim in _collect_claim_records(current_content)
        if claim.get("claim_id")
    }
    poi_checks_by_claim = _latest_claim_checks(poi_checks if isinstance(poi_checks, list) else [])
    retry_blob = " ".join(
        str(item) for item in (
            current_content.get("retry_history", [])
            if isinstance(current_content, dict) and isinstance(current_content.get("retry_history"), list)
            else []
        )
    )

    excerpts_by_ref: dict[str, list[str]] = {}
    for item in source_excerpts:
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        ref = str(source.get("chunk_id") or item.get("ref") or "")
        text = str(item.get("text") or "")
        if ref and text and text not in excerpts_by_ref.setdefault(ref, []):
            excerpts_by_ref[ref].append(text)

    current_source_rows: list[dict[str, Any]] = []
    for item in attached_sources:
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        ref = str(source.get("chunk_id") or "")
        texts = excerpts_by_ref.get(ref, [])
        current_source_rows.append({
            "ref": ref,
            "platform": str(source.get("platform") or ""),
            "author": str(source.get("author") or ""),
            "source_url": str(source.get("source_url") or ""),
            "excerpts": texts[:2],
            "origin": str(item.get("origin") or "sources"),
            "attachment_scope": "review_scope",
        })

    entity_rows: list[dict[str, Any]] = []
    affected_files: set[str] = {file} if file else set()
    total_occurrences = 0
    total_final_entity_mentions = 0
    final_guide_locations: set[tuple[str, int]] = set()
    stale_entities = 0
    for entity in entities:
        occurrences = _entity_occurrences(current_content, entity, pointer=pointer)
        total_occurrences += len(occurrences)

        sources = []
        for source in current_source_rows:
            excerpts = list(dict.fromkeys(
                _entity_text_excerpt(str(text), entity)
                for text in source["excerpts"]
            ))
            if not excerpts:
                relation = "unreadable"
            elif any(entity in text for text in excerpts):
                relation = "mentions_entity"
            else:
                relation = "no_exact_mention"
            sources.append({
                **source,
                "relation": relation,
                "excerpts": excerpts,
            })

        score_by_ref = dict(material_bm25_index.search(entity, top_k=30)) if material_bm25_index else {}
        candidate_refs = [ref for ref, text in chunk_map.items() if entity in text]
        candidate_refs.sort(key=lambda ref: (-float(score_by_ref.get(ref, 0.0)), ref))
        candidates = [
            {
                "ref": ref,
                "text": _entity_text_excerpt(chunk_map[ref], entity),
                "retrieval_method": "exact_entity+bm25" if ref in score_by_ref else "exact_entity",
                "retrieval_score": round(float(score_by_ref.get(ref, 0.0)), 6),
                "already_attached": any(source["ref"] == ref for source in sources),
            }
            for ref in candidate_refs[:4]
        ]

        downstream = _entity_downstream_evidence(
            project_id,
            entity,
            chunk_map=chunk_map,
            checks_by_claim=poi_checks_by_claim,
        )
        affected_files.update(str(item.get("file") or "") for item in downstream if item.get("file"))
        final_occurrences = _final_guide_entity_occurrences(project_id, entity)
        if final_occurrences:
            affected_files.add("final_guide.md")
        total_final_entity_mentions += len(final_occurrences)
        final_guide_locations.update(
            (str(item.get("file") or ""), int(item.get("line") or 0))
            for item in final_occurrences
        )

        historical_failures = []
        for check in section_checks if isinstance(section_checks, list) else []:
            if not isinstance(check, dict):
                continue
            result = str(check.get("result") or "").lower()
            blob = " ".join([
                str(check.get("reason") or ""),
                *(str(item) for item in (check.get("missing_entities") or []) if item),
            ])
            claim_id = str(check.get("claim_id") or "")
            if result in _FAILED_CHECK_RESULTS and entity in blob and claim_id not in current_claim_ids:
                target_path = str(check.get("target_path") or check.get("field") or "")
                check_pointer = pointer.rstrip("/")
                if target_path:
                    check_pointer += "/" + "/".join(
                        _pointer_token(part) for part in target_path.split(".")
                    )
                same_current_field = bool(
                    check_pointer
                    and any(item.get("json_pointer") == check_pointer for item in occurrences)
                )
                checked_at = str(check.get("checked_at") or "")
                historical_failures.append({
                    "claim_id": claim_id,
                    "checked_claim_text": str(check.get("claim_text") or check.get("target_text") or ""),
                    "target_path": target_path,
                    "json_pointer": check_pointer or None,
                    "scope": str(check.get("scope") or ""),
                    "reason": str(check.get("reason") or ""),
                    "checked_at": checked_at,
                    "checked_before_content_update": _timestamp_precedes(checked_at, content_updated_at),
                    "current_claim_present": False,
                    "match_status": "same_field_stale_claim" if same_current_field else "orphaned_claim",
                })
        has_current_origin = any(
            entity in " ".join([
                str(origin.get("claim_text") or ""),
                *(str(item) for item in (origin.get("missing_entities") or []) if item),
            ])
            for origin in origin_claims
        )
        stale = not has_current_origin and bool(historical_failures or entity in retry_blob)
        stale_entities += int(stale)

        downstream_passed = any(item.get("validation_status") == "passed" for item in downstream)
        if not occurrences:
            resolution_state = "no_longer_present"
            recommendation = "当前业务字段已不再出现该实体；可重核当前对象并清理旧状态。"
        elif downstream_passed:
            resolution_state = "verified_downstream"
            recommendation = "下游 POI Claim 已有通过的结构化核验；建议将证据精确回填到当前字段后，仅重核该对象。"
        elif candidates:
            resolution_state = "candidate_found"
            recommendation = "素材库已找到精确提及该实体的候选原文；需确认它支持完整表述后再回填，不可因“出现名称”直接判通过。"
        else:
            resolution_state = "needs_evidence"
            recommendation = "当前项目内未找到可读的支持证据；需补充证据或收窄当前表述。"

        entity_rows.append({
            "entity": entity,
            "occurrence_count": len(occurrences),
            "occurrences": occurrences,
            "current_sources": sources,
            "candidate_evidence": candidates,
            "downstream_evidence": downstream,
            "final_guide_occurrences": final_occurrences,
            "stale_evidence_state": stale,
            "verification_state": "stale_failure" if stale else ("current_failure" if has_current_origin else "unassessed"),
            "content_updated_at": content_updated_at,
            "latest_checked_at": max(
                (str(item.get("checked_at") or "") for item in historical_failures),
                default="",
            ),
            "historical_failures": historical_failures[:4],
            "resolution_state": resolution_state,
            "recommendation": recommendation,
        })

    citation_status = str(current_content.get("citation_status") or "") if isinstance(current_content, dict) else ""
    finding_status = getattr(finding.status, "value", str(finding.status))
    progress = workspace_store.read_workspace(project_id, "progress.json") or {}
    project_phase = str(progress.get("state") or "") if isinstance(progress, dict) else ""
    return {
        "schema_version": "missing-entity-review-v1",
        "entity_count": len(entity_rows),
        "occurrence_count": total_occurrences,
        "raw_missing_entity_count": len(raw_entities),
        "stale_evidence_state": stale_entities > 0,
        "stale_entity_count": stale_entities,
        "verification_state": "stale_failure" if stale_entities > 0 else ("current_failure" if origin_claims else "unassessed"),
        "stale_reason": (
            "历史失败检查指向已不在当前内容中的 Claim ID；需以当前字段和后续证据重新判断。"
            if stale_entities > 0 else ""
        ),
        "project_impact": {
            "finding_status": finding_status,
            "project_phase": project_phase,
            "citation_status": citation_status,
            "review_required": finding_status != "closed",
            "review_queue_counted": finding_status != "closed",
            "generation_blocked": False,
            "affects_project_state": finding_status != "closed" and citation_status == "force_finalized",
            "final_guide_affected": bool(final_guide_locations),
            "final_guide_occurrence_count": len(final_guide_locations),
            "final_guide_entity_mention_count": total_final_entity_mentions,
            "affected_files": sorted(path for path in affected_files if path),
            "note": "当前异常会保留项目级待处理状态；最终攻略出现该实体只说明已传播，不等于当前引用已通过。",
        },
        "entities": entity_rows,
    }


def _force_finalized_conflict_has_canonical_mirror(
    conflict: dict[str, Any],
    *,
    route_force_days: set[int],
    lifestyle_force_keys: set[tuple[str, int]],
    pitfall_force_keys: set[tuple[str, int]],
    route_feasibility_force: bool,
    final_contract_force: bool,
) -> bool:
    """Avoid counting a conflict already represented by a canonical object."""
    target = str(conflict.get("target") or "")
    issue_type = str(conflict.get("issue_type") or "")
    reason = str(conflict.get("reason") or "")
    issues = [item for item in (conflict.get("issues") or []) if isinstance(item, dict)]

    if issue_type == "final_guide_contract":
        return final_contract_force
    if issue_type.startswith("route_feasibility_") or reason.startswith(
        ("路线可执行性核验", "路线时效核验")
    ):
        return route_feasibility_force
    if issue_type:
        return False
    if target == "route_agent":
        days = {int(item["day"]) for item in issues if item.get("day") is not None}
        return bool(days) and days.issubset(route_force_days)

    for agent_prefix, forced_keys in (
        ("lifestyle_agent.", lifestyle_force_keys),
        ("pitfall_agent.", pitfall_force_keys),
    ):
        if not target.startswith(agent_prefix):
            continue
        target_field = target.removeprefix(agent_prefix)
        keys = {
            (str(item.get("field") or target_field), int(item["day"]))
            for item in issues
            if item.get("day") is not None
        }
        return bool(keys) and keys.issubset(forced_keys)
    return False


def _canonical_state_observations(project_id: str) -> dict[str, Any]:
    """只扫描业务事实文件，返回可操作对象和只读派生镜像。"""
    workspace = _workspace(project_id)
    observations: list[dict[str, Any]] = []
    resolved_observations: list[dict[str, str]] = []
    derived_mirrors: list[dict[str, Any]] = []
    manifest_mismatches: list[str] = []
    raw_force_finalized_paths: list[str] = []
    route_force_days: set[int] = set()
    lifestyle_force_keys: set[tuple[str, int]] = set()
    pitfall_force_keys: set[tuple[str, int]] = set()

    def add_force(
        *,
        canonical_key: str,
        file: str,
        pointer: str,
        observed: Any,
        object_label: str,
    ) -> None:
        if not isinstance(observed, dict):
            return
        if observed.get("citation_status") == "passed":
            resolved_observations.append({
                "canonical_key": canonical_key,
                "resolved_fingerprint": _value_fingerprint(observed),
                "verification_status": "passed_after_targeted_recheck",
                "resolution_note": f"{object_label}已通过定向引用复核；历史核验记录继续保留作审计。",
            })
            return
        if observed.get("citation_status") != "force_finalized":
            return
        residue_path = f"{file}{pointer}"
        summary, question = _state_issue_explanation(observed)
        observations.append({
            "canonical_key": canonical_key,
            "finding_kind": "state_residue",
            "problem_type": "force_finalized_residue",
            "root_cause": "evidence",
            "repair_action": "targeted_content_review",
            "target": {
                "file": file,
                "json_path": pointer,
                "residue_path": residue_path,
                "scope": "historical_status_residue",
                "field": "citation_status",
                "object_label": object_label,
            },
            "issue_summary": summary,
            "review_question": question,
            "observed_value": observed,
            "observed_fingerprint": _value_fingerprint(observed),
        })

    final_meta = _read_workspace_json(project_id, "final_guide.meta.json")
    contract = final_meta.get("contract_validation", {}) if isinstance(final_meta, dict) else {}
    if isinstance(contract, dict) and contract.get("status") == "force_finalized":
        raw_force_finalized_paths.append(
            "final_guide.meta.json/contract_validation"
        )
        issues = contract.get("issues", []) if isinstance(contract.get("issues"), list) else []
        issue_labels = [str(issue.get("message") or issue.get("code")) for issue in issues if isinstance(issue, dict)]
        summary = (
            "历史攻略补扫未通过最终交付契约"
            if contract.get("source") == "historical_backfill"
            else "最终攻略交付契约在重试耗尽后仍未通过"
        )
        if issue_labels:
            summary += f"：{'；'.join(issue_labels[:8])}"
        observations.append({
            "canonical_key": "final_guide:contract",
            "finding_kind": "consistency",
            "problem_type": "final_guide_contract_violation",
            "root_cause": "code",
            "repair_action": "targeted_integration_rerun",
            "target": {
                "file": "final_guide.md",
                "meta_file": "final_guide.meta.json",
                "json_path": "/contract_validation",
                "object_label": "最终攻略交付契约",
            },
            "issue_summary": summary,
            "review_question": "检查缺失的Day、清单或上游模块，决定保留、暂缓或定向重跑Integration Agent。",
            "observed_value": contract,
            "observed_fingerprint": _value_fingerprint({
                "body_sha256": contract.get("body_sha256", ""),
                "guide_sha256": contract.get("guide_sha256", ""),
                "issues": issues,
            }),
        })
    elif isinstance(contract, dict) and contract.get("status") == "passed":
        resolved_observations.append({
            "canonical_key": "final_guide:contract",
            "resolved_fingerprint": _value_fingerprint({
                "body_sha256": contract.get("body_sha256", ""),
                "guide_sha256": contract.get("guide_sha256", ""),
                "issues": [],
            }),
            "verification_status": "passed_after_regeneration",
            "resolution_note": "最终攻略已重新生成并通过交付契约。",
        })

    route = _read_workspace_json(project_id, "route_plan.json")
    for index, day in enumerate(route.get("days", []) if isinstance(route, dict) else []):
        if not isinstance(day, dict):
            continue
        day_number = day.get("day", index + 1)
        add_force(
            canonical_key=f"route_day:{day_number}",
            file="route_plan.json",
            pointer=f"/days/{index}",
            observed=day,
            object_label=f"第 {day_number} 天路线",
        )
        if day.get("citation_status") == "force_finalized":
            route_force_days.add(int(day_number))

    feasibility = _read_workspace_json(project_id, "route_feasibility.json")
    route_feasibility_force = bool(
        isinstance(feasibility, dict) and feasibility.get("force_finalized") is True
    )
    if route_feasibility_force:
        raw_force_finalized_paths.append("route_feasibility.json")

    for category in ("lifestyle", "pitfall"):
        relative_file = f"outputs/{category}.json"
        document = _read_workspace_json(project_id, relative_file)
        if not isinstance(document, dict):
            continue
        for field, section in document.items():
            items = section.get("items", []) if isinstance(section, dict) else []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                day_number = item.get("day", index + 1)
                field_label = {
                    "food": "美食建议",
                    "outfit": "穿搭建议",
                    "lodging": "住宿建议",
                    "play": "游玩建议",
                }.get(str(field), str(field))
                object_label = (
                    f"第 {day_number} 天{field_label}"
                    if category == "lifestyle"
                    else f"第 {day_number} 天避雷提示 · {field_label}"
                )
                add_force(
                    canonical_key=f"{category}:{field}:day:{day_number}",
                    file=relative_file,
                    pointer=f"/{_pointer_token(str(field))}/items/{index}",
                    observed=item,
                    object_label=object_label,
                )
                if item.get("citation_status") == "force_finalized":
                    key = (str(field), int(day_number))
                    if category == "lifestyle":
                        lifestyle_force_keys.add(key)
                    else:
                        pitfall_force_keys.add(key)

    details_dir = workspace / "outputs" / "poi_details"
    if details_dir.exists():
        for path in sorted(details_dir.glob("*.json")):
            if path.name == "manifest.json":
                continue
            detail = _read_workspace_json(project_id, f"outputs/poi_details/{path.name}")
            if not isinstance(detail, dict):
                continue
            day_number = detail.get("day", "unknown")
            poi_name = str(detail.get("poi_name") or detail.get("name") or path.stem)
            add_force(
                canonical_key=f"poi_detail:{day_number}:{poi_name}",
                file=f"outputs/poi_details/{path.name}",
                pointer="",
                observed=detail,
                object_label=poi_name,
            )

    manifest_file = "outputs/poi_details/manifest.json"
    manifest = _read_workspace_json(project_id, manifest_file)
    for index, entry in enumerate(manifest.get("generated", []) if isinstance(manifest, dict) else []):
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("file") or "")
        detail_file = f"outputs/poi_details/{filename}" if filename else ""
        detail = _read_workspace_json(project_id, detail_file) if detail_file else None
        if entry.get("citation_status") == "generation_failed":
            raw_force_finalized_paths.append(
                detail_file or f"{manifest_file}/generated/{index}"
            )
        if entry.get("citation_status") == "force_finalized":
            derived_mirrors.append({
                "file": manifest_file,
                "json_path": f"/generated/{index}",
                "residue_path": f"{manifest_file}/generated/{index}",
                "detail_file": detail_file,
            })
        missing_detail = not filename or not (workspace / detail_file).exists()
        status_mismatch = bool(
            not missing_detail
            and isinstance(detail, dict)
            and entry.get("citation_status") != detail.get("citation_status")
        )
        pair_fingerprint = _value_fingerprint({"entry": entry, "detail": detail})
        if not missing_detail:
            resolved_observations.append({
                "canonical_key": f"manifest:missing:{filename}",
                "resolved_fingerprint": pair_fingerprint,
                "verification_status": "passed_after_targeted_recheck",
                "resolution_note": f"{filename} 已存在；历史缺文件记录已解决。",
            })
        if missing_detail:
            mismatch = f"missing:{filename}"
            manifest_mismatches.append(mismatch)
            summary = f"manifest 记录了 {filename or '空文件名'}，但对应 POI 详情文件不存在。"
        elif status_mismatch:
            mismatch = f"status:{filename}"
            manifest_mismatches.append(mismatch)
            summary = f"manifest 与 {filename} 的 citation_status 不一致。"
        else:
            resolved_observations.append({
                "canonical_key": f"manifest:status:{filename}",
                "resolved_fingerprint": pair_fingerprint,
                "verification_status": "passed_after_state_sync",
                "resolution_note": f"{filename} 与 manifest 的引用终态已一致。",
            })
            continue
        needs_rerun = mismatch.startswith("missing:")
        observations.append({
            "canonical_key": f"manifest:{mismatch}",
            "finding_kind": "consistency",
            "problem_type": "manifest_mismatch",
            "root_cause": "code",
            "repair_action": "targeted_poi_detail_regeneration" if needs_rerun else "reconcile_poi_status",
            "disposition": "needs_rerun" if needs_rerun else "deferred",
            "needs_rerun": needs_rerun,
            "rerun_cost": "low" if needs_rerun else "none",
            "target": {
                "file": manifest_file,
                "json_path": f"/generated/{index}",
                "mismatch": mismatch,
                "missing_file": filename if needs_rerun else "",
                "detail_file": filename,
                "manifest_index": index,
                "day": entry.get("day"),
                "poi_name": entry.get("poi_name"),
                "object_label": filename or "POI manifest",
            },
            "issue_summary": summary,
            "review_question": (
                "确认该 POI 仍需详情后定向重跑，不重跑整条路线。"
                if needs_rerun
                else "根据终态结果和当前核验记录确定权威状态，再做确定性同步。"
            ),
            "observed_value": entry,
            "observed_fingerprint": _value_fingerprint({"entry": entry, "detail": detail}),
        })

    conflicts = workspace_store.read_workspace(project_id, "conflicts.jsonl") or []
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict) or conflict.get("status") != "force_finalized":
            continue
        if _force_finalized_conflict_has_canonical_mirror(
            conflict,
            route_force_days=route_force_days,
            lifestyle_force_keys=lifestyle_force_keys,
            pitfall_force_keys=pitfall_force_keys,
            route_feasibility_force=route_feasibility_force,
            final_contract_force=bool(
                isinstance(contract, dict) and contract.get("status") == "force_finalized"
            ),
        ):
            continue
        conflict_id = str(conflict.get("id") or index)
        raw_force_finalized_paths.append(f"conflicts.jsonl/{conflict_id}")

    return {
        "observations": observations,
        "resolved_observations": resolved_observations,
        "derived_mirrors": derived_mirrors,
        "manifest_mismatches": manifest_mismatches,
        "raw_force_finalized_paths": raw_force_finalized_paths,
    }


def scan_project_state(project_id: str) -> dict[str, Any]:
    """运行确定性状态扫描，并为同一覆盖输入生成稳定 project revision。"""
    workspace = _workspace(project_id)
    covered_inputs: dict[str, Any] = {}
    covered_mtimes: list[float] = []
    fixed_files = [
        "final_guide.meta.json",
        "route_plan.json",
        "route_feasibility.json",
        "outputs/lifestyle.json",
        "outputs/pitfall.json",
    ]
    for relative_file in fixed_files:
        covered_inputs[relative_file] = _read_workspace_json(project_id, relative_file)
        path = workspace / relative_file
        if path.is_file():
            covered_mtimes.append(path.stat().st_mtime)
    conflicts_path = workspace / "conflicts.jsonl"
    covered_inputs["conflicts.jsonl"] = (
        workspace_store.read_workspace(project_id, "conflicts.jsonl") or []
    )
    if conflicts_path.is_file():
        covered_mtimes.append(conflicts_path.stat().st_mtime)
    details_dir = workspace / "outputs" / "poi_details"
    if details_dir.exists():
        for path in sorted(details_dir.glob("*.json")):
            relative_file = str(path.relative_to(workspace))
            covered_inputs[relative_file] = _read_workspace_json(project_id, relative_file)
            covered_mtimes.append(path.stat().st_mtime)
    scan = _canonical_state_observations(project_id)
    scan["project_revision"] = _value_fingerprint(covered_inputs)
    scan["revision_created_at"] = (
        datetime.fromtimestamp(max(covered_mtimes), tz=timezone.utc).isoformat()
        if covered_mtimes
        else ""
    )
    scan["observed_at"] = _now()
    return scan


def _evaluate_final_guide_contract_backfill(
    project_id: str,
    guide: str,
    meta: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Evaluate current legacy content and infer only deterministic meta aliases."""
    requirements_doc = workspace_store.read_workspace(project_id, "requirements.json") or {}
    requirements = (
        requirements_doc.get("requirements", requirements_doc)
        if isinstance(requirements_doc, dict)
        else {}
    )
    route_plan = workspace_store.read_workspace(project_id, "route_plan.json")
    feasibility = workspace_store.read_workspace(project_id, "route_feasibility.json")
    lifestyle = workspace_store.read_workspace(project_id, "outputs/lifestyle.json")
    pitfall = workspace_store.read_workspace(project_id, "outputs/pitfall.json")
    final_blocks = workspace_store.read_workspace(project_id, "final_guide.blocks.json")
    poi_details = [
        detail.model_dump(mode="json") for detail in workspace_store.list_poi_details(project_id)
    ]
    issues = validate_final_guide_contract(
        requirements,
        route_plan,
        feasibility,
        lifestyle,
        pitfall,
        guide,
        meta,
        final_blocks,
        poi_details,
        workspace_store.read_evidence_checks(project_id),
        workspace_store.read_workspace(project_id, "review_findings.jsonl") or [],
    )
    inferred_fields: list[str] = []
    if any(
        issue.get("code") == "merged_field_missing" and issue.get("field") == "pitfall"
        for issue in issues
    ) and re.search(r"避雷|踩坑", guide):
        inferred_fields.append("pitfall")
        migrated_meta = copy.deepcopy(meta)
        migrated_meta["merged_fields"] = list(migrated_meta.get("merged_fields") or []) + inferred_fields
        issues = validate_final_guide_contract(
            requirements,
            route_plan,
            feasibility,
            lifestyle,
            pitfall,
            guide,
            migrated_meta,
            final_blocks,
            poi_details,
            workspace_store.read_evidence_checks(project_id),
            workspace_store.read_workspace(project_id, "review_findings.jsonl") or [],
        )
    return issues, inferred_fields


def plan_final_guide_contract_backfill(project_ids: Iterable[str]) -> dict[str, Any]:
    """Build a read-only, content-addressed plan for legacy final-guide metadata."""
    plans: list[dict[str, Any]] = []
    for project_id in project_ids:
        guide = workspace_store.read_workspace(project_id, "final_guide.md")
        meta = workspace_store.read_workspace(project_id, "final_guide.meta.json")
        if not isinstance(guide, str) or not isinstance(meta, dict):
            plans.append({"project_id": project_id, "action": "skip", "reason": "missing_final_guide"})
            continue
        normalized_guide = normalize_final_guide_section_order(guide)
        guide_sha256 = hashlib.sha256(guide.encode("utf-8")).hexdigest()
        normalized_guide_sha256 = hashlib.sha256(normalized_guide.encode("utf-8")).hexdigest()
        body_sha256 = hashlib.sha256(
            contract_body_markdown(normalized_guide).encode("utf-8")
        ).hexdigest()
        contract = meta.get("contract_validation")
        if isinstance(contract, dict) and contract.get("status"):
            recorded_body_sha256 = str(contract.get("body_sha256") or "")
            if recorded_body_sha256 and recorded_body_sha256 != body_sha256:
                plans.append({
                    "project_id": project_id,
                    "action": "skip",
                    "reason": "validated_body_changed_manual_review_required",
                })
                continue
            needs_normalization = normalized_guide != guide
            if not recorded_body_sha256 and contract.get("guide_sha256") != guide_sha256:
                issues, inferred_fields = _evaluate_final_guide_contract_backfill(
                    project_id,
                    normalized_guide,
                    meta,
                )
                plans.append({
                    "project_id": project_id,
                    "action": "revalidate_contract_delivery",
                    "guide_sha256": guide_sha256,
                    "meta_fingerprint": _value_fingerprint(meta),
                    "normalize_program_sections": needs_normalization,
                    "meta_merged_fields_to_add": inferred_fields,
                    "status": "force_finalized" if issues else "passed",
                    "issues": issues,
                })
                continue
            needs_hash_refresh = (
                not recorded_body_sha256
                or contract.get("guide_sha256") != normalized_guide_sha256
            )
            if not needs_normalization and not needs_hash_refresh:
                plans.append({
                    "project_id": project_id,
                    "action": "skip",
                    "reason": "contract_already_current",
                })
                continue
            plans.append({
                "project_id": project_id,
                "action": "refresh_contract_delivery",
                "guide_sha256": guide_sha256,
                "meta_fingerprint": _value_fingerprint(meta),
                "normalize_program_sections": needs_normalization,
            })
            continue
        issues, inferred_fields = _evaluate_final_guide_contract_backfill(
            project_id,
            normalized_guide,
            meta,
        )
        plans.append({
            "project_id": project_id,
            "action": "write_contract_validation",
            "guide_sha256": guide_sha256,
            "meta_fingerprint": _value_fingerprint(meta),
            "normalize_program_sections": normalized_guide != guide,
            "meta_merged_fields_to_add": inferred_fields,
            "status": "force_finalized" if issues else "passed",
            "issues": issues,
        })
    return {
        "schema_version": "final-guide-contract-backfill-v2",
        "created_at": _now(),
        "plans": plans,
    }


def apply_final_guide_contract_backfill(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply an reviewed backfill plan without rewriting final-guide prose."""
    if plan.get("schema_version") != "final-guide-contract-backfill-v2":
        raise ValueError("不支持的最终攻略合同迁移计划版本")
    results: list[dict[str, Any]] = []
    for item in plan.get("plans", []):
        project_id = str(item.get("project_id") or "")
        action = item.get("action")
        if action not in {
            "write_contract_validation",
            "refresh_contract_delivery",
            "revalidate_contract_delivery",
        }:
            results.append({"project_id": project_id, "status": "skipped", "reason": item.get("reason", "plan_skip")})
            continue
        guide = workspace_store.read_workspace(project_id, "final_guide.md")
        meta = workspace_store.read_workspace(project_id, "final_guide.meta.json")
        if not isinstance(guide, str) or not isinstance(meta, dict):
            raise RuntimeError(f"项目{project_id!r}的最终攻略在计划生成后已不存在")
        actual_hash = hashlib.sha256(guide.encode("utf-8")).hexdigest()
        if actual_hash != item.get("guide_sha256"):
            raise RuntimeError(f"项目{project_id!r}的最终攻略在计划生成后已变化，请重新生成迁移计划")
        if _value_fingerprint(meta) != item.get("meta_fingerprint"):
            raise RuntimeError(f"项目{project_id!r}的最终攻略Meta在计划生成后已变化，请重新生成迁移计划")
        normalized = False
        if item.get("normalize_program_sections"):
            normalized = workspace_store.normalize_final_guide_program_sections(project_id)
        if action == "refresh_contract_delivery":
            existing = meta.get("contract_validation")
            if not isinstance(existing, dict) or not existing.get("status"):
                raise RuntimeError(f"项目{project_id!r}的契约记录在计划生成后已消失")
            refreshed = workspace_store.refresh_final_guide_delivery_hash(
                project_id,
                actor="review_migration",
            )
            sync_state_findings(project_id)
            results.append({
                "project_id": project_id,
                "status": "applied",
                "contract_status": existing.get("status"),
                "sections_normalized": normalized,
                "delivery_hash_refreshed": refreshed,
            })
            continue
        inferred_fields = list(item.get("meta_merged_fields_to_add") or [])
        if inferred_fields:
            workspace_store.backfill_final_guide_merged_fields(project_id, inferred_fields)
        workspace_store.write_final_guide_contract_validation(
            project_id,
            status=str(item.get("status")),
            issues=list(item.get("issues") or []),
            actor="review_migration",
            source="historical_backfill",
        )
        sync_state_findings(project_id)
        results.append({
            "project_id": project_id,
            "status": "applied",
            "contract_status": item.get("status"),
            "issue_count": len(item.get("issues") or []),
            "meta_merged_fields_added": inferred_fields,
            "sections_normalized": normalized,
        })
    return results


def _legacy_canonical_key(finding: ReviewFinding) -> str:
    if finding.canonical_key:
        return finding.canonical_key
    target = finding.target or {}
    file = str(target.get("file") or "")
    pointer = str(target.get("json_path") or "")
    observed = finding.observed_value if isinstance(finding.observed_value, dict) else {}
    if file.startswith("review_runs/"):
        return ""
    if finding.problem_type == "manifest_mismatch":
        mismatch = str(target.get("mismatch") or "")
        return f"manifest:{mismatch}" if mismatch else ""
    if file == "outputs/poi_details/manifest.json":
        return ""
    if file.startswith("outputs/poi_details/"):
        day_number = observed.get("day", "unknown")
        poi_name = str(observed.get("poi_name") or observed.get("name") or Path(file).stem)
        return f"poi_detail:{day_number}:{poi_name}"
    match = re.match(r"^/(.+)/items/\d+$", pointer)
    if file in {"outputs/lifestyle.json", "outputs/pitfall.json"} and match:
        category = "lifestyle" if "lifestyle" in file else "pitfall"
        day_number = observed.get("day")
        return f"{category}:{match.group(1)}:day:{day_number}" if day_number is not None else ""
    match = re.match(r"^/days/\d+$", pointer)
    if file == "route_plan.json" and match:
        day_number = observed.get("day")
        return f"route_day:{day_number}" if day_number is not None else ""
    return ""


def _supersede_duplicate_state_findings(
    project_id: str,
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Close duplicate scanner records while preserving their append-only history."""
    grouped: dict[str, list[ReviewFinding]] = {}
    for finding in findings:
        if finding.superseded_by or finding.finding_kind == "derived_mirror":
            continue
        key = _legacy_canonical_key(finding)
        if key and finding.source_review_id == f"state:v2:{key}":
            grouped.setdefault(key, []).append(finding)

    changed = False
    for items in grouped.values():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda item: (item.created_at, item.finding_id))
        primary = ordered[0]
        for duplicate in ordered[1:]:
            update_finding(
                project_id,
                duplicate.finding_id,
                finding_kind="derived_mirror",
                superseded_by=primary.finding_id,
                disposition="false_positive",
                status="closed",
                verification_method="deterministic_state_scan",
                verification_status="duplicate_canonical_superseded",
                verification_actor_type="system",
                verification_actor_id="state_scanner",
                needs_rerun=False,
                notes="并发状态扫描生成了重复记录；已保留历史并归并到同一规范问题。",
            )
            changed = True
    return read_findings(project_id) if changed else findings


def sync_state_findings(project_id: str) -> list[ReviewFinding]:
    """将规范业务对象的状态残留转成 finding，并维护复核状态指纹。"""
    checked_at = _now()
    try:
        scan = scan_project_state(project_id)
    except Exception as exc:
        try:
            from src.review import monitoring as review_monitoring

            review_monitoring.observe_state_scan_failure(project_id, str(exc))
        except Exception:
            pass
        raise
    with _finding_write_lock(project_id):
        existing = _supersede_duplicate_state_findings(
            project_id,
            read_findings(project_id),
        )
    by_key: dict[str, ReviewFinding] = {}
    for item in existing:
        if item.superseded_by or item.finding_kind == "derived_mirror":
            continue
        key = _legacy_canonical_key(item)
        if key and key not in by_key:
            by_key[key] = item

    created: list[ReviewFinding] = []
    for observation in scan["observations"]:
        key = observation["canonical_key"]
        current = by_key.get(key)
        if current is not None:
            updates = {
                "canonical_key": key,
                "finding_kind": observation["finding_kind"],
                "target": observation["target"],
                "issue_summary": observation["issue_summary"],
                "review_question": observation["review_question"],
                "observed_value": observation["observed_value"],
                "observed_fingerprint": observation["observed_fingerprint"],
                "notes": observation["issue_summary"],
            }
            if observation["problem_type"] == "manifest_mismatch":
                updates.update({
                    "repair_action": observation["repair_action"],
                    "disposition": observation["disposition"],
                    "needs_rerun": observation["needs_rerun"],
                    "rerun_cost": observation["rerun_cost"],
                })
            fingerprint_changed = bool(
                current.observed_fingerprint
                and current.observed_fingerprint != observation["observed_fingerprint"]
            )
            if current.status == "closed":
                resolved_fingerprint = current.resolved_fingerprint or current.observed_fingerprint
                updates["resolved_fingerprint"] = resolved_fingerprint
                if resolved_fingerprint and resolved_fingerprint != observation["observed_fingerprint"]:
                    updates.update({
                        "status": "open",
                        "disposition": "deferred",
                        "verification_status": "pending",
                        "verification_actor_type": "",
                        "verification_actor_id": "",
                        "recommendation_id": None,
                        "patch_id": None,
                        "resolved_fingerprint": "",
                    })
            elif fingerprint_changed and current.recommendation_id:
                updates.update({
                    "status": "open",
                    "verification_status": "pending",
                    "recommendation_id": None,
                })
            changed = {name: value for name, value in updates.items() if getattr(current, name) != value}
            if changed:
                update_finding(project_id, current.finding_id, **changed)
            continue

        problem_type = observation["problem_type"]
        finding = ReviewFinding(
            finding_id=f"finding-state-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            baseline_version=checked_at,
            target=observation["target"],
            problem_type=problem_type,
            disposition=observation.get("disposition", "deferred"),
            root_cause=observation["root_cause"],
            severity="medium",
            rerun_cost=observation.get("rerun_cost", "medium"),
            repair_action=observation["repair_action"],
            verification_method="deterministic_state_scan",
            verification_status="pending",
            needs_rerun=bool(observation.get("needs_rerun", False)),
            status="open",
            notes=observation["issue_summary"],
            created_at=checked_at,
            updated_at=checked_at,
            actor_id="state_scanner",
            source_review_id=f"state:v2:{key}",
            issue_summary=observation["issue_summary"],
            review_question=observation["review_question"],
            observed_value=observation["observed_value"],
            canonical_key=key,
            finding_kind=observation["finding_kind"],
            observed_fingerprint=observation["observed_fingerprint"],
        )
        with _finding_write_lock(project_id):
            # ``by_key`` was built before taking the cross-process lock. A
            # concurrent scanner may have created this canonical finding in
            # the meantime, so re-read under the lock before appending.
            concurrent = next(
                (
                    item
                    for item in read_findings(project_id)
                    if not item.superseded_by
                    and item.finding_kind != "derived_mirror"
                    and _legacy_canonical_key(item) == key
                ),
                None,
            )
            if concurrent is not None:
                by_key[key] = concurrent
                continue
            _append_jsonl(_findings_path(project_id), finding.model_dump(mode="json"))
            _audit(project_id, review_id=finding.finding_id, review_type="state_finding", target=finding.target, decision="detected", action="sync_state_finding", actor_type="system", actor_id="state_scanner", modified=False, notes=finding.notes)
            workspace_store.log_event(project_id, "state_scanner", "review_finding_detected", f"review_findings/{finding.finding_id}", finding.issue_summary)
        by_key[key] = finding
        created.append(finding)

    for resolved in scan.get("resolved_observations", []):
        current = by_key.get(resolved["canonical_key"])
        if current is None or current.status == "closed":
            continue
        update_finding(
            project_id,
            current.finding_id,
            status="closed",
            disposition="resolved",
            verification_status=str(
                resolved.get("verification_status") or "passed_after_state_sync"
            ),
            verification_method="deterministic_state_scan",
            verification_actor_type="system",
            verification_actor_id="state_scanner",
            resolved_fingerprint=resolved["resolved_fingerprint"],
            notes=str(resolved.get("resolution_note") or "当前对象已通过状态复核。"),
        )
    from src.review import monitoring as review_monitoring

    review_monitoring.observe_state_scan(project_id, scan)
    return created


def plan_review_maintenance(project_id: str) -> dict[str, Any]:
    """生成只读迁移计划；不写 finding、recommendation 或业务文件。"""
    findings = read_findings(project_id)
    scan = _canonical_state_observations(project_id)
    observations = {item["canonical_key"]: item for item in scan["observations"]}
    canonical_by_key: dict[str, ReviewFinding] = {}
    for finding in findings:
        key = _legacy_canonical_key(finding)
        if key and key in observations and key not in canonical_by_key:
            canonical_by_key[key] = finding

    canonical_updates = []
    for key, observation in observations.items():
        finding = canonical_by_key.get(key)
        if finding is None:
            continue
        canonical_updates.append({
            "finding_id": finding.finding_id,
            "expected_revision": finding.revision,
            "canonical_key": key,
            "finding_kind": observation["finding_kind"],
            "observed_fingerprint": observation["observed_fingerprint"],
        })

    close_review_run_copies = []
    supersede_manifest_mirrors = []
    for finding in findings:
        target_file = str((finding.target or {}).get("file") or "")
        mirror_key = _mirror_canonical_key(finding)
        primary = canonical_by_key.get(mirror_key)
        action = {
            "finding_id": finding.finding_id,
            "expected_revision": finding.revision,
            "canonical_key": mirror_key,
            "superseded_by": primary.finding_id if primary else None,
        }
        if target_file == "review_runs" or target_file.startswith("review_runs/"):
            close_review_run_copies.append(action)
        elif target_file == "outputs/poi_details/manifest.json" and finding.problem_type == "force_finalized_residue":
            supersede_manifest_mirrors.append(action)

    open_before = sum(item.status != "closed" for item in findings)
    duplicate_open = sum(
        1
        for action in [*close_review_run_copies, *supersede_manifest_mirrors]
        if (current := next((item for item in findings if item.finding_id == action["finding_id"]), None))
        and current.status != "closed"
    )
    recommendation_count = sum(len(read_recommendations_for_finding(project_id, item.finding_id)) for item in findings)
    decision_file_count = len(list((_workspace(project_id) / "review_runs").glob("recommendation-*/item_decisions.jsonl"))) if (_workspace(project_id) / "review_runs").exists() else 0
    return {
        "schema_version": "review-maintenance-plan-v1",
        "project_id": project_id,
        "generated_at": _now(),
        "canonical_updates": canonical_updates,
        "create_canonical_keys": sorted(set(observations) - set(canonical_by_key)),
        "close_review_run_copies": close_review_run_copies,
        "supersede_manifest_mirrors": supersede_manifest_mirrors,
        "preserve_manifest_mismatches": [
            item.finding_id for item in findings if item.problem_type == "manifest_mismatch"
        ],
        "expected_open_before": open_before,
        "expected_open_after": max(0, open_before - duplicate_open + len(set(observations) - set(canonical_by_key))),
        "recommendation_count_before": recommendation_count,
        "decision_file_count_before": decision_file_count,
    }


def apply_review_maintenance_plan(project_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """幂等应用迁移计划；只追加 finding 投影，不删除历史。"""
    if plan.get("schema_version") != "review-maintenance-plan-v1" or plan.get("project_id") != project_id:
        raise ValueError("迁移计划版本或 project_id 不匹配")
    sync_state_findings(project_id)
    changed: list[str] = []

    for action in plan.get("canonical_updates", []):
        finding = get_finding(project_id, str(action.get("finding_id") or ""))
        if finding is None:
            raise KeyError(f"迁移目标 finding 不存在: {action.get('finding_id')}")
        updates = {
            "canonical_key": str(action.get("canonical_key") or finding.canonical_key),
            "finding_kind": str(action.get("finding_kind") or finding.finding_kind),
            "observed_fingerprint": str(action.get("observed_fingerprint") or finding.observed_fingerprint),
        }
        delta = {name: value for name, value in updates.items() if getattr(finding, name) != value}
        if delta:
            update_finding(project_id, finding.finding_id, **delta)
            changed.append(finding.finding_id)

    def close_mirror(action: dict[str, Any], verification_status: str) -> None:
        finding_id = str(action.get("finding_id") or "")
        finding = get_finding(project_id, finding_id)
        if finding is None:
            raise KeyError(f"迁移目标 finding 不存在: {finding_id}")
        if finding.status == "closed" and finding.finding_kind == "derived_mirror":
            return
        update_finding(
            project_id,
            finding_id,
            finding_kind="derived_mirror",
            canonical_key=str(action.get("canonical_key") or finding.canonical_key),
            superseded_by=action.get("superseded_by"),
            disposition="false_positive",
            status="closed",
            verification_method="deterministic_migration_v2",
            verification_status=verification_status,
            verification_actor_type="system",
            verification_actor_id="review_migration",
            needs_rerun=False,
            notes="该记录来自派生镜像或冻结复核任务包，不再作为独立问题展示。",
        )
        changed.append(finding_id)

    for action in plan.get("close_review_run_copies", []):
        close_mirror(action, "scanner_artifact_excluded")
    for action in plan.get("supersede_manifest_mirrors", []):
        close_mirror(action, "derived_mirror_superseded")

    current = read_findings(project_id)
    return {
        "project_id": project_id,
        "changed_finding_ids": changed,
        "open_after": sum(item.status != "closed" and not item.superseded_by for item in current),
        "queue_count_after": len(build_review_queue(project_id, include_closed=True)),
        "recommendation_count_after": sum(len(read_recommendations_for_finding(project_id, item.finding_id)) for item in current),
        "decision_file_count_after": len(list((_workspace(project_id) / "review_runs").glob("recommendation-*/item_decisions.jsonl"))) if (_workspace(project_id) / "review_runs").exists() else 0,
        "applied_at": _now(),
    }


def get_finding(project_id: str, finding_id: str) -> ReviewFinding | None:
    latest = _latest_records(_findings_path(project_id), "finding_id")
    data = latest.get(finding_id)
    return _with_generic_explanation(ReviewFinding.model_validate(data)) if data else None


def build_review_context(project_id: str, finding_id: str) -> dict[str, Any]:
    """为模型复核拼装实际内容、精确字段和历史证据。"""
    finding = get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    target = dict(finding.target or {})
    from src.review.manifest_consistency import build_manifest_consistency_context

    manifest_consistency_context = build_manifest_consistency_context(project_id, finding)
    raw_path = str(target.get("json_path") or "")
    file = str(target.get("file") or "")
    pointer = ""
    # 兼容旧状态 finding：json_path 里保存的是完整 workspace 路径。
    if raw_path.startswith("outputs/"):
        marker = raw_path.find(".json")
        if marker >= 0:
            file = raw_path[: marker + 5]
            rest = raw_path[marker + 5 :].lstrip("/")
            pointer = "/" + rest if rest else ""
    elif raw_path.startswith("/"):
        pointer = raw_path
    if file == "outputs":
        file = ""
    current_content: Any = None
    document: Any = None
    file_path: Path | None = None
    if file:
        try:
            file_path = _safe_workspace_file(project_id, file)
        except ValueError:
            file_path = None
    if file_path and file_path.exists():
        try:
            document = json.loads(file_path.read_text(encoding="utf-8"))
            current_content = _pointer_get(document, pointer) if pointer else document
            if current_content is _MISSING:
                current_content = None
        except (OSError, json.JSONDecodeError):
            current_content = None

    origin_claims = _claim_origin_contexts(
        project_id,
        file=file,
        pointer=pointer,
        target=target,
        current_content=current_content,
    )

    related_history: list[dict[str, Any]] = []
    scope = str(target.get("scope") or "")
    for record in _read_jsonl(_audit_path(project_id)):
        if record.get("review_type") not in {"content_review", "backfill", "presentation_fix"}:
            continue
        record_target = record.get("target") or {}
        blob = json.dumps(record_target, ensure_ascii=False)
        if (file and file in blob) or (scope and scope in blob) or (finding.source_review_id and record.get("review_id") == finding.source_review_id):
            related_history.append({
                "review_id": record.get("review_id"),
                "decision": record.get("decision"),
                "action": record.get("action"),
                "notes": record.get("notes"),
                "evidence_refs": record.get("evidence_refs") or [],
                "llm_called": bool(record.get("llm_called")),
            })
    related_history = related_history[-12:]
    content_evidence: list[dict[str, Any]] = []
    def collect_content_evidence(value: Any) -> None:
        if isinstance(value, dict):
            for source_field in ("sources", "evidence_refs"):
                for source in value.get(source_field, []) or []:
                    if isinstance(source, dict):
                        content_evidence.append({
                            "type": "current_source",
                            "source": source,
                            "origin": source_field,
                        })
            for snapshot in value.get("evidence_snapshots", []) or []:
                if not isinstance(snapshot, dict):
                    continue
                source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
                excerpt = str(snapshot.get("excerpt") or snapshot.get("text") or "")
                if source:
                    content_evidence.append({
                        "type": "current_source",
                        "source": source,
                        "origin": "embedded_snapshot",
                    })
                if excerpt:
                    content_evidence.append({
                        "type": "source_excerpt",
                        "source": source,
                        "text": excerpt,
                        "origin": "embedded_snapshot",
                    })
            for entity in value.get("missing_entities", []) or []:
                content_evidence.append({"type": "missing_entity", "entity": entity})
            for retry in (value.get("retry_history", []) or [])[-5:]:
                content_evidence.append({"type": "retry_history", "detail": retry})
            for child in value.values():
                collect_content_evidence(child)
        elif isinstance(value, list):
            for child in value:
                collect_content_evidence(child)
    collect_content_evidence(current_content)

    # finding 中通常只有 chunk_id；复核模型必须看到对应原文，才能区分“内容错误”
    # 和“内容真实但引用标错/证据未挂上”。按项目目的地读取素材索引并回填短摘录。
    source_refs: list[dict[str, Any]] = []
    def collect_source_refs(value: Any) -> None:
        if isinstance(value, dict):
            for source_field in ("sources", "evidence_refs"):
                for source in value.get(source_field, []) or []:
                    if isinstance(source, dict) and source.get("chunk_id"):
                        source_refs.append(source)
            for snapshot in value.get("evidence_snapshots", []) or []:
                if not isinstance(snapshot, dict):
                    continue
                source = snapshot.get("source")
                if isinstance(source, dict) and source.get("chunk_id"):
                    source_refs.append(source)
            for child in value.values():
                collect_source_refs(child)
        elif isinstance(value, list):
            for child in value:
                collect_source_refs(child)
    collect_source_refs(current_content)
    file_source_ids = list(dict.fromkeys(
        str(source.get("chunk_id") or "") for source in source_refs if source.get("chunk_id")
    ))
    requirements = workspace_store.read_requirements(project_id) or {}
    requirements_body = requirements.get("requirements", requirements) if isinstance(requirements, dict) else {}
    topic = str(requirements_body.get("destination") or requirements.get("topic") or "")
    chunk_map: dict[str, str] = {}
    has_missing_entities = bool(
        isinstance(current_content, dict)
        and isinstance(current_content.get("missing_entities"), list)
        and current_content.get("missing_entities")
    )
    if topic and (source_refs or has_missing_entities):
        chunk_map = citation_check.load_chunk_texts(topic, project_id)
        seen_chunks: set[str] = set()
        for source in source_refs:
            chunk_id = str(source.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen_chunks or chunk_id not in chunk_map:
                continue
            seen_chunks.add(chunk_id)
            content_evidence.append({
                "type": "source_excerpt",
                "source": source,
                "topic": topic,
                "text": chunk_map[chunk_id][:1600],
                "origin": "material_library",
            })
            if len(seen_chunks) >= 30:
                break
    material_bm25_index = (
        bm25_search.BM25SearchIndex([
            {"chunk_id": chunk_id, "text": text}
            for chunk_id, text in chunk_map.items()
        ])
        if topic and chunk_map else None
    )
    def compact(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "routes" in value and "poi_name" in value:
            return {
                "poi_name": value.get("poi_name"),
                "day": value.get("day"),
                "citation_status": value.get("citation_status"),
                "needs_detail_reason": value.get("needs_detail_reason"),
                "retry_history_tail": (value.get("retry_history") or [])[-3:],
                "routes": [
                    {
                        "route_name": route.get("route_name"),
                        "recommended": route.get("recommended"),
                        "segments": [
                            {"point_name": segment.get("point_name"), "highlight": segment.get("highlight"), "suggested_duration": segment.get("suggested_duration"), "sources": segment.get("sources", [])}
                            for segment in (route.get("route_segments") or [])
                        ],
                    }
                    for route in (value.get("routes") or [])
                ],
            }
        if "recommendation" in value:
            return {
                "day": value.get("day"),
                "recommendation": value.get("recommendation"),
                "citation_status": value.get("citation_status"),
                "missing_entities": value.get("missing_entities", []),
                "sources": value.get("sources", []),
                "claimed_entities": value.get("claimed_entities", []),
            }
        return value

    review_item_contexts: list[dict[str, Any]] = []
    recommendation = read_recommendation(project_id, finding.recommendation_id) if finding.recommendation_id else None
    retry_history = (document.get("retry_history") or []) if isinstance(document, dict) else []
    if recommendation is not None:
        if recommendation.claim_reviews:
            raw_items = [
                {
                    "item_key": f"claim:{index}",
                    "item_kind": "claim",
                    "label": claim.claim,
                    "target_path": claim.target_path,
                    "original_text": claim.original_text,
                    "evidence_refs": list(claim.evidence_refs),
                    "model_evidence_excerpt": claim.evidence_excerpt,
                    "evidence_origin": "model_review",
                    "model_verdict": claim.verdict,
                }
                for index, claim in enumerate(recommendation.claim_reviews)
            ]
        else:
            raw_items = [
                {
                    "item_key": f"legacy:{index}",
                    "item_kind": "legacy_suggestion",
                    "label": label,
                    "target_path": "",
                    "original_text": "",
                    "evidence_refs": [],
                    "model_evidence_excerpt": "",
                    "evidence_origin": "current_json_sources",
                    "model_verdict": "",
                }
                for index, label in enumerate(_legacy_recommendation_items(recommendation.proposed_text))
            ]

        for raw_item in raw_items:
            item_pointer = str(raw_item["target_path"] or "")
            item_value: Any = None
            route_name = ""
            point_name = ""
            if item_pointer and document is not None:
                located = _pointer_get(document, item_pointer)
                item_value = None if located is _MISSING else located
            if item_value is None and document is not None:
                route_pointer, segment, route_name, point_name = _route_segment_target(document, str(raw_item["label"]))
                if segment is not None:
                    item_pointer = route_pointer
                    item_value = segment
            if item_value is None and document is not None and raw_item["original_text"]:
                item_pointer = _find_text_pointer(document, str(raw_item["original_text"]))
                if item_pointer:
                    located = _pointer_get(document, item_pointer)
                    item_value = None if located is _MISSING else located

            item_sources = _direct_sources(item_value)
            evidence_refs = list(raw_item["evidence_refs"])
            if not evidence_refs:
                evidence_refs = [str(source.get("chunk_id") or "") for source in item_sources if source.get("chunk_id")]
            excerpts = []
            unresolved_refs = []
            for evidence_ref in evidence_refs:
                ref = str(evidence_ref)
                if ref in chunk_map:
                    excerpts.append({"ref": ref, "text": chunk_map[ref][:1600]})
                else:
                    unresolved_refs.append(ref)
            failure_points = _matching_failure_points(
                retry_history,
                point_name,
                str(raw_item["label"]),
            )
            support_status = _historical_support_status(failure_points, str(raw_item["model_verdict"] or ""))
            if raw_item["model_verdict"]:
                support_basis = "current_model_review"
            elif failure_points:
                support_basis = "historical_failure"
            else:
                support_basis = "unassessed"
            candidate_anchor = point_name or str(raw_item["label"])
            related_candidates = _related_source_candidates(
                chunk_map,
                file_source_ids,
                evidence_refs,
                candidate_anchor,
                failure_points,
            )
            search_query = " ".join(_candidate_search_terms(candidate_anchor, failure_points)[:12])
            ranked_library_hits = material_bm25_index.search(search_query, top_k=24) if material_bm25_index else []
            library_candidates = _library_source_candidates(
                chunk_map,
                ranked_library_hits,
                set(evidence_refs) | set(file_source_ids) | {item["ref"] for item in related_candidates},
                candidate_anchor,
                failure_points,
                {ref.rsplit("-c", 1)[0] for ref in evidence_refs if "-c" in ref},
            )
            has_model_excerpt = bool(raw_item["model_evidence_excerpt"])
            if item_value is None:
                coverage = "unmapped"
            elif excerpts or has_model_excerpt:
                coverage = "partial" if unresolved_refs else "ready"
            else:
                coverage = "insufficient"
            review_item_contexts.append({
                "item_key": raw_item["item_key"],
                "item_kind": raw_item["item_kind"],
                "label": raw_item["label"],
                "target": {
                    "file": file or None,
                    "json_pointer": item_pointer or None,
                    "route_name": route_name or None,
                    "point_name": point_name or None,
                },
                "current_value": compact(item_value),
                "evidence_refs": evidence_refs,
                "evidence_excerpts": excerpts,
                "unresolved_evidence_refs": unresolved_refs,
                "evidence_origin": raw_item["evidence_origin"],
                "support_status": support_status,
                "support_basis": support_basis,
                "related_evidence_candidates": related_candidates,
                "library_evidence_candidates": library_candidates,
                "alternative_evidence_found": bool(related_candidates or library_candidates),
                "model_evidence_excerpt": raw_item["model_evidence_excerpt"],
                "failure_points": failure_points,
                "coverage": coverage,
                "ready_for_human_review": coverage in {"ready", "partial"},
            })

    linked_detail: Any = None
    linked_document: Any = None
    if file == "outputs/poi_details/manifest.json":
        try:
            manifest_document = json.loads(file_path.read_text(encoding="utf-8")) if file_path and file_path.exists() else None
        except (OSError, json.JSONDecodeError):
            manifest_document = None
        entries = manifest_document.get("generated", []) if isinstance(manifest_document, dict) else []
        mismatch_file = str(target.get("detail_file") or target.get("missing_file") or "")
        manifest_index = target.get("manifest_index")
        if not isinstance(manifest_index, int):
            match = re.fullmatch(r"/generated/(\d+)", pointer)
            manifest_index = int(match.group(1)) if match else None
        for entry_index, entry in enumerate(entries):
            detail_file = str(entry.get("file") or "") if isinstance(entry, dict) else ""
            if (
                not detail_file
                or (mismatch_file and detail_file != mismatch_file)
                or (not mismatch_file and manifest_index is not None and entry_index != manifest_index)
            ):
                continue
            try:
                detail_path = _safe_workspace_file(project_id, f"outputs/poi_details/{detail_file}")
                linked_document = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.exists() else None
            except (OSError, json.JSONDecodeError, ValueError):
                linked_document = None
            linked_detail = compact(linked_document) if linked_document is not None else {"file": detail_file, "missing": True}
            collect_content_evidence(linked_document)
            break

    content_evidence = content_evidence[:80]
    attached_sources: list[dict[str, Any]] = []
    seen_attached_sources: set[str] = set()
    for item in content_evidence:
        if item.get("type") != "current_source":
            continue
        source = item.get("source") or {}
        source_key = str(source.get("chunk_id") or json.dumps(source, ensure_ascii=False, sort_keys=True))
        if source_key in seen_attached_sources:
            continue
        seen_attached_sources.add(source_key)
        attached_sources.append(item)
    source_excerpts = [item for item in content_evidence if item.get("type") == "source_excerpt"]
    missing_entity_context = _build_missing_entity_review(
        project_id,
        finding=finding,
        file=file,
        pointer=pointer,
        current_content=current_content,
        content_updated_at=str(
            (
                current_content.get("updated_at")
                if isinstance(current_content, dict) and current_content.get("updated_at")
                else document.get("updated_at")
                if isinstance(document, dict)
                else ""
            )
            or ""
        ),
        origin_claims=origin_claims,
        attached_sources=attached_sources,
        source_excerpts=source_excerpts,
        chunk_map=chunk_map,
        material_bm25_index=material_bm25_index,
    )
    current_references = [*attached_sources, *source_excerpts]
    verification_failures = [
        item for item in content_evidence
        if item.get("type") in {"retry_history", "missing_entity"}
    ]
    current_issue_keys = {
        str(item.get("issue_group_key") or item.get("claim_id") or "")
        for item in origin_claims
        if item.get("issue_group_key") or item.get("claim_id")
    }
    verification_source = linked_document if isinstance(linked_document, dict) else current_content
    is_currently_passed = bool(
        isinstance(verification_source, dict)
        and verification_source.get("citation_status") == "passed"
    )
    verification_timeline = _verification_history_projection(
        verification_source,
        current_blocker_count=(
            len(current_issue_keys) if origin_claims else (0 if is_currently_passed else None)
        ),
        current_claim_count=(
            len(origin_claims) if origin_claims else (0 if is_currently_passed else None)
        ),
    )
    evidence: list[dict[str, Any] | str] = []
    if finding.evidence_refs:
        evidence.extend(finding.evidence_refs)
    evidence.extend(content_evidence)
    evidence.extend(related_history)
    current_content = compact(current_content)
    if linked_detail is not None:
        if isinstance(current_content, dict):
            current_content = {**current_content, "linked_detail": linked_detail}
        else:
            current_content = {"manifest_entry": current_content, "linked_detail": linked_detail}
    summary = {
        "file": file or None,
        "json_pointer": pointer or None,
        "current_content": current_content,
        "historical_reviews": related_history,
        "finding_notes": finding.notes,
        "problem_type": finding.problem_type,
        "issue_summary": finding.issue_summary,
        "review_question": finding.review_question,
        "linked_detail": linked_detail,
        "scope_kind": "field" if pointer else "file",
        "scope_label": pointer or "文件级扫描结果（未定位到单一字段）",
        "evidence_counts": {
            "finding_refs": len(finding.evidence_refs),
            "attached_sources": len(attached_sources),
            "source_excerpts": len(source_excerpts),
            "verification_failures": len(verification_failures),
            "historical_reviews": len(related_history),
        },
    }
    return {
        "current_content": current_content if current_content is not None else summary,
        "origin_claims": origin_claims,
        "missing_entity_context": missing_entity_context,
        "manifest_consistency_context": manifest_consistency_context,
        "verification_timeline": verification_timeline,
        "review_item_contexts": review_item_contexts,
        "evidence": evidence,
        "evidence_groups": {
            "finding_refs": list(finding.evidence_refs),
            "current_references": current_references,
            "attached_sources": attached_sources,
            "source_excerpts": source_excerpts,
            "verification_failures": verification_failures,
            "historical_reviews": related_history,
        },
        "summary": summary,
    }


def update_finding(project_id: str, finding_id: str, **updates: Any) -> ReviewFinding:
    current = get_finding(project_id, finding_id)
    if current is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    data = current.model_dump(mode="json")
    data.update(updates)
    data["revision"] = current.revision + 1
    data["updated_at"] = _now()
    updated = ReviewFinding.model_validate(data)
    with _lock(project_id):
        _append_jsonl(_findings_path(project_id), updated.model_dump(mode="json"))
        _audit(
            project_id,
            review_id=updated.finding_id,
            review_type="finding",
            target=updated.target,
            decision=getattr(updated.disposition, "value", str(updated.disposition)),
            action="update_finding",
            actor_type="system",
            actor_id="review_engine",
            modified=True,
            notes=updated.notes,
            evidence_refs=updated.evidence_refs,
        )
    return updated


def propose_patch(
    project_id: str,
    *,
    finding_id: str,
    file: str,
    operations: list[dict[str, Any] | PatchOperation],
    actor_type: str = "model",
    actor_id: str = "",
) -> PatchRecord:
    """读取当前文件并生成 patch 建议，绝不修改源文件。"""
    path = _safe_workspace_file(project_id, file)
    if not path.exists():
        raise FileNotFoundError(file)
    with _lock(project_id):
        raw_document = json.loads(path.read_text(encoding="utf-8"))
        normalized: list[PatchOperation] = []
        for raw in operations:
            operation = raw if isinstance(raw, PatchOperation) else PatchOperation.model_validate(raw)
            current = _pointer_get(raw_document, operation.path)
            operation.before_exists = current is not _MISSING
            operation.before = None if current is _MISSING else copy.deepcopy(current)
            if operation.op == "replace":
                if current is _MISSING:
                    raise ValueError(f"replace 目标不存在: {operation.path}")
                before_kind = _json_value_kind(current)
                after_kind = _json_value_kind(operation.value)
                if before_kind != after_kind:
                    raise ValueError(
                        f"replace 类型不匹配: {operation.path} 当前为 {before_kind}，修改值为 {after_kind}"
                    )
            normalized.append(operation)
        _apply_operations(raw_document, normalized)
        patch_id = f"patch-{uuid.uuid4().hex[:12]}"
        now = _now()
        patch = PatchRecord(
            patch_id=patch_id,
            project_id=project_id,
            finding_id=finding_id,
            file=file,
            base_sha256=file_sha256(path),
            operations=normalized,
            actor_type=actor_type,
            actor_id=actor_id,
            created_at=now,
            updated_at=now,
        )
        finding = get_finding(project_id, finding_id)
        patch.cascade_plan = review_policy.build_cascade_plan(
            project_id,
            file=file,
            target=finding.target if finding else {},
            operations=[item.model_dump(mode="json") for item in normalized],
        )
        patch.impact_class = patch.cascade_plan["impact_class"]
        patch_path = _review_dir(project_id) / patch_id / "patch.json"
        _write_json_atomic(patch_path, patch.model_dump(mode="json"))
        update_finding(project_id, finding_id, patch_id=patch_id, repair_action="targeted_patch", status="in_review")
        _audit(
            project_id,
            review_id=patch_id,
            review_type="patch",
            target={"file": file, "finding_id": finding_id},
            decision="proposed",
            action="propose_patch",
            actor_type=actor_type,
            actor_id=actor_id,
            modified=False,
        )
        return patch


def read_patch(project_id: str, patch_id: str) -> PatchRecord:
    path = _review_dir(project_id) / patch_id / "patch.json"
    if not path.exists():
        raise FileNotFoundError(patch_id)
    return PatchRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))


def read_patches(project_id: str) -> list[PatchRecord]:
    review_dir = _review_dir(project_id)
    patches: list[PatchRecord] = []
    if not review_dir.exists():
        return patches
    for path in review_dir.glob("patch-*/patch.json"):
        try:
            patches.append(PatchRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return sorted(patches, key=lambda item: (item.created_at, item.patch_id))


def read_pending_validation_patches(project_id: str) -> list[PatchRecord]:
    pending_statuses = {
        "pending_integration",
        "pending_targeted_validation",
    }
    return [
        patch
        for patch in read_patches(project_id)
        if patch.status == "applied"
        and str(patch.cascade_plan.get("execution_status") or "") in pending_statuses
    ]


def record_patch_validation_status(
    project_id: str,
    patch_id: str,
    *,
    execution_status: str,
    details: dict[str, Any] | None = None,
    actor_id: str = "review_validation",
) -> PatchRecord:
    with _lock(project_id):
        patch = read_patch(project_id, patch_id)
        if patch.status != "applied":
            raise ValueError(f"只有已应用 Patch 可以记录验收状态: {patch.status}")
        patch.cascade_plan["execution_status"] = execution_status
        if details is not None:
            patch.cascade_plan["validation_result"] = copy.deepcopy(details)
        _save_patch(project_id, patch)
        _audit(
            project_id,
            review_id=patch.patch_id,
            review_type="patch_validation",
            target={"file": patch.file, "finding_id": patch.finding_id},
            decision=execution_status,
            action="record_patch_validation",
            actor_type="system",
            actor_id=actor_id,
            modified=False,
            notes=json.dumps(details or {}, ensure_ascii=False)[:1200],
        )
        return patch


def _save_patch(project_id: str, patch: PatchRecord) -> None:
    patch.updated_at = _now()
    _write_json_atomic(_review_dir(project_id) / patch.patch_id / "patch.json", patch.model_dump(mode="json"))


def apply_patch(project_id: str, patch_id: str, *, actor_type: str = "human", actor_id: str = "admin_user") -> PatchRecord:
    with _lock(project_id):
        patch = read_patch(project_id, patch_id)
        if patch.status != "proposed":
            raise ValueError(f"patch 当前状态不允许应用: {patch.status}")
        path = _safe_workspace_file(project_id, patch.file)
        if not path.exists() or file_sha256(path) != patch.base_sha256:
            patch.status = "patch_conflict"
            _save_patch(project_id, patch)
            _audit(
                project_id,
                review_id=patch.patch_id,
                review_type="patch",
                target={"file": patch.file, "finding_id": patch.finding_id},
                decision="patch_conflict",
                action="apply_patch",
                actor_type=actor_type,
                actor_id=actor_id,
                modified=False,
                notes="源文件版本已变化，暂停应用旧patch。",
            )
            raise RuntimeError("patch_conflict: 源文件版本已变化")
        document = json.loads(path.read_text(encoding="utf-8"))
        try:
            updated_document = _apply_operations(document, patch.operations)
        except (KeyError, IndexError, ValueError) as exc:
            patch.status = "patch_conflict"
            _save_patch(project_id, patch)
            raise RuntimeError(f"patch_conflict: {exc}") from exc
        _write_json_atomic(path, updated_document)
        patch.status = "applied"
        patch.actor_type = actor_type
        patch.actor_id = actor_id
        patch.applied_at = _now()
        requires_integration = bool(patch.cascade_plan.get("requires_integration_execution"))
        requires_targeted_validation = bool(
            patch.cascade_plan.get("requires_targeted_validation")
        )
        if requires_integration:
            patch.cascade_plan["execution_status"] = "pending_integration"
        elif requires_targeted_validation:
            patch.cascade_plan["execution_status"] = "pending_targeted_validation"
        else:
            patch.cascade_plan["execution_status"] = "completed_mechanical"
        _save_patch(project_id, patch)
        finding = get_finding(project_id, patch.finding_id)
        resolved_fingerprint = finding.observed_fingerprint if finding else ""
        try:
            target_pointer = str((finding.target or {}).get("json_path") or "") if finding else ""
            current_value = _pointer_get(updated_document, target_pointer) if target_pointer else updated_document
            if current_value is not _MISSING:
                resolved_fingerprint = _value_fingerprint(current_value)
        except (TypeError, ValueError):
            pass
        if requires_integration or requires_targeted_validation:
            verification_status = (
                "pending_integration_review"
                if requires_integration
                else "pending_targeted_review"
            )
            notes = (
                "人工已应用批准的内容 Patch，等待 Integration 局部重组与交付契约验证。"
                if requires_integration
                else "人工已应用批准的证据或状态 Patch，等待原核验环节定向复核，无需重组 Integration 正文。"
            )
            update_finding(
                project_id,
                patch.finding_id,
                disposition="deferred",
                verification_status=verification_status,
                verification_method="approved_patch_apply",
                verification_actor_type=actor_type,
                verification_actor_id=actor_id,
                status="verifying",
                observed_fingerprint=resolved_fingerprint,
                notes=notes,
            )
        else:
            update_finding(
                project_id,
                patch.finding_id,
                disposition="resolved",
                verification_status="passed_after_patch",
                verification_method="approved_patch_apply",
                verification_actor_type=actor_type,
                verification_actor_id=actor_id,
                status="closed",
                observed_fingerprint=resolved_fingerprint,
                resolved_fingerprint=resolved_fingerprint,
                notes="人工已应用批准的证据或状态 Patch，确定性收口完成。",
            )
        _audit(
            project_id,
            review_id=patch.patch_id,
            review_type="patch",
            target={"file": patch.file, "finding_id": patch.finding_id},
            decision="accepted",
            action="apply_patch",
            actor_type=actor_type,
            actor_id=actor_id,
            modified=True,
        )
        return patch


def reject_patch(project_id: str, patch_id: str, *, actor_type: str = "human", actor_id: str = "admin_user", reason: str = "") -> PatchRecord:
    with _lock(project_id):
        patch = read_patch(project_id, patch_id)
        if patch.status != "proposed":
            raise ValueError(f"patch 当前状态不允许拒绝: {patch.status}")
        patch.status = "rejected"
        _save_patch(project_id, patch)
        update_finding(project_id, patch.finding_id, disposition="deferred", status="open", notes=reason)
        _audit(
            project_id,
            review_id=patch.patch_id,
            review_type="patch",
            target={"file": patch.file, "finding_id": patch.finding_id},
            decision="rejected",
            action="reject_patch",
            actor_type=actor_type,
            actor_id=actor_id,
            modified=False,
            notes=reason,
        )
        return patch


def rollback_patch(project_id: str, patch_id: str, *, actor_type: str = "human", actor_id: str = "admin_user") -> PatchRecord:
    """为已应用 patch 生成反向 patch；不删除原 patch。"""
    with _lock(project_id):
        patch = read_patch(project_id, patch_id)
        if patch.status != "applied":
            raise ValueError(f"只有已应用patch可以回滚: {patch.status}")
        reverse_ops: list[PatchOperation] = []
        for operation in reversed(patch.operations):
            if operation.op == "replace":
                reverse_ops.append(
                    PatchOperation(op="replace", path=operation.path, value=operation.before)
                )
            elif operation.op == "add":
                if operation.before_exists:
                    reverse_ops.append(
                        PatchOperation(op="replace", path=operation.path, value=operation.before)
                    )
                else:
                    reverse_ops.append(PatchOperation(op="remove", path=operation.path))
            elif operation.op == "remove":
                reverse_ops.append(PatchOperation(op="add", path=operation.path, value=operation.before))
        rollback = propose_patch(
            project_id,
            finding_id=patch.finding_id,
            file=patch.file,
            operations=reverse_ops,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        rollback.parent_patch_id = patch.patch_id
        _save_patch(project_id, rollback)
        _audit(
            project_id,
            review_id=rollback.patch_id,
            review_type="rollback",
            target={"file": patch.file, "finding_id": patch.finding_id, "parent_patch_id": patch.patch_id},
            decision="proposed",
            action="rollback_patch",
            actor_type=actor_type,
            actor_id=actor_id,
            modified=False,
            notes="为已应用patch生成反向patch。",
        )
        return rollback


def append_recommendation(
    project_id: str,
    recommendation: ReviewRecommendation,
    *,
    promote: bool = True,
) -> None:
    path = _review_dir(project_id) / recommendation.recommendation_id / "recommendation.json"
    recommendation.created_at = recommendation.created_at or _now()
    _write_json_atomic(path, recommendation.model_dump(mode="json"))
    if promote:
        update_finding(project_id, recommendation.finding_id, recommendation_id=recommendation.recommendation_id, status="in_review")
    _audit(
        project_id,
        review_id=recommendation.recommendation_id,
        review_type="recommendation",
        target={"finding_id": recommendation.finding_id},
        decision=recommendation.decision,
        action="append_recommendation" if promote else "append_candidate_recommendation",
        actor_type="model",
        actor_id=recommendation.model_name,
        modified=False,
        notes=recommendation.summary,
        evidence_refs=recommendation.evidence_refs,
    )
    workspace_store.log_event(
        project_id,
        recommendation.model_name or "model",
        "review_recommendation",
        f"review_findings/{recommendation.finding_id}",
        recommendation.summary or ("模型已生成当前定向复核建议" if promote else "模型已生成候选定向复核建议"),
    )


def promote_recommendation(
    project_id: str,
    finding_id: str,
    recommendation_id: str,
    *,
    actor_id: str = "admin_user",
) -> ReviewFinding:
    """显式把一个候选 recommendation 设为 finding 的当前推荐。"""
    finding = get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    recommendation = read_recommendation(project_id, recommendation_id)
    if recommendation is None or recommendation.finding_id != finding_id:
        raise KeyError(f"recommendation 不存在或不属于该 finding: {recommendation_id}")
    updated = update_finding(
        project_id,
        finding_id,
        recommendation_id=recommendation_id,
        status="in_review",
    )
    _audit(
        project_id,
        review_id=f"review-promote-{uuid.uuid4().hex[:12]}",
        review_type="recommendation_promotion",
        target={"finding_id": finding_id, "recommendation_id": recommendation_id},
        decision="promoted",
        action="promote_recommendation",
        actor_type="human",
        actor_id=actor_id,
        modified=False,
        notes="人工将候选建议设为当前推荐。",
        evidence_refs=recommendation.evidence_refs,
    )
    workspace_store.log_event(
        project_id,
        actor_id,
        "review_recommendation_promoted",
        f"review_findings/{finding_id}",
        f"已将 {recommendation_id} 设为当前推荐",
    )
    return updated


def assert_review_run_baseline_current(project_id: str, run: ReviewRun) -> None:
    """候选建议晋升前再次校验冻结 finding 和业务文件。"""
    finding = get_finding(project_id, run.finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {run.finding_id}")
    if finding.revision != run.baseline_finding_revision:
        raise RuntimeError("stale_candidate: finding 在复核运行后已变化，请基于当前版本重新运行")
    bundle_path = _workspace(project_id) / str(run.artifact_paths.get("bundle") or "")
    if not bundle_path.exists():
        raise RuntimeError("stale_candidate: 冻结任务包不存在")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("stale_candidate: 冻结任务包不可读") from exc
    baseline_file = str((bundle.get("baseline") or {}).get("file") or "")
    if baseline_file:
        path = _safe_workspace_file(project_id, baseline_file)
        current_sha = file_sha256(path) if path.exists() else ""
        if current_sha != run.baseline_file_sha256:
            raise RuntimeError("stale_candidate: 复核目标文件已变化，请重新运行")


def save_review_run(project_id: str, run: ReviewRun) -> ReviewRun:
    if run.project_id != project_id:
        raise ValueError("ReviewRun.project_id 与项目不一致")
    _write_json_atomic(_review_run_path(project_id, run.run_id), run.model_dump(mode="json"))
    return run


def read_review_run(project_id: str, run_id: str) -> ReviewRun | None:
    path = _review_run_path(project_id, run_id)
    if not path.exists():
        return None
    try:
        run = ReviewRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return _project_review_run_state(run)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def update_review_run(project_id: str, run_id: str, **changes: Any) -> ReviewRun:
    with _lock(project_id):
        current = read_review_run(project_id, run_id)
        if current is None:
            raise KeyError(f"review run 不存在: {run_id}")
        requested_status = changes.get("status")
        if current.status in {"succeeded", "failed", "cancelled", "interrupted"}:
            if requested_status is not None and requested_status != current.status:
                return current
        updated = current.model_copy(update=changes)
        return save_review_run(project_id, updated)


def read_review_runs(project_id: str, finding_id: str | None = None) -> list[ReviewRun]:
    runs: list[ReviewRun] = []
    for path in _execution_dir(project_id).glob("review-run-*/run.json"):
        try:
            run = _project_review_run_state(
                ReviewRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
            )
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if finding_id is None or run.finding_id == finding_id:
            runs.append(run)
    return sorted(runs, key=lambda item: (item.created_at, item.run_id), reverse=True)


def _project_review_run_state(run: ReviewRun) -> ReviewRun:
    """Overlay SQLite lifecycle state on the JSON review artifact."""
    from src.execution_state import get_job_service

    job = get_job_service().get_job(run.run_id)
    if job is None:
        return run
    return run.model_copy(update={
        "status": job["status"],
        "status_reason": job.get("status_reason"),
        "health_status": job.get("health_status") or "healthy",
        "owner_instance_id": job.get("owner_instance_id"),
        "started_at": job.get("started_at"),
        "last_heartbeat_at": job.get("last_heartbeat_at"),
        "last_progress_at": job.get("last_progress_at"),
        "completed_at": job.get("finished_at"),
        "error": job.get("error") or run.error,
    })


def read_recommendation(project_id: str, recommendation_id: str) -> ReviewRecommendation | None:
    path = _review_dir(project_id) / recommendation_id / "recommendation.json"
    if not path.exists():
        return None
    return ReviewRecommendation.model_validate(json.loads(path.read_text(encoding="utf-8")))


def read_recommendations_for_finding(project_id: str, finding_id: str) -> list[ReviewRecommendation]:
    """读取一个 finding 的全部追加式建议，按创建时间排序。

    finding 上的 ``recommendation_id`` 只是最新投影，不是完整历史。
    复核范围恢复、审计和前端时间线都不应只依赖这一个指针。
    """
    recommendations: list[ReviewRecommendation] = []
    for path in _review_dir(project_id).glob("recommendation-*/recommendation.json"):
        try:
            recommendation = ReviewRecommendation.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if recommendation.finding_id == finding_id:
            recommendations.append(recommendation)
    return sorted(recommendations, key=lambda item: (item.created_at, item.recommendation_id))


def build_recommendation_history(
    project_id: str,
    finding_id: str,
) -> list[ReviewRecommendationHistoryEntry]:
    """聚合 recommendation、人工决定和运行元数据，不修改任何历史记录。"""
    finding = get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    runs_by_recommendation = {
        run.recommendation_id: run
        for run in read_review_runs(project_id, finding_id)
        if run.recommendation_id
    }
    current_recommendation = (
        read_recommendation(project_id, finding.recommendation_id)
        if finding.recommendation_id
        else None
    )
    entries: list[ReviewRecommendationHistoryEntry] = []
    for recommendation in reversed(read_recommendations_for_finding(project_id, finding_id)):
        decisions = read_item_decisions(project_id, recommendation.recommendation_id)
        total_items = (
            len(recommendation.claim_reviews)
            if recommendation.claim_reviews
            else len(_legacy_recommendation_items(recommendation.proposed_text))
        )
        run = runs_by_recommendation.get(recommendation.recommendation_id)
        is_current = recommendation.recommendation_id == finding.recommendation_id
        is_superseded = bool(
            not is_current
            and (
                (run and run.superseded_at)
                or decisions
                or (
                    current_recommendation
                    and recommendation.created_at
                    and current_recommendation.created_at
                    and recommendation.created_at < current_recommendation.created_at
                )
            )
        )
        entries.append(ReviewRecommendationHistoryEntry(
            recommendation=recommendation,
            is_current=is_current,
            lifecycle_status="current" if is_current else "superseded" if is_superseded else "candidate",
            decision_count=len(decisions),
            total_items=total_items,
            run_id=run.run_id if run else recommendation.source_run_id,
            run_status=run.status if run else None,
            executor_id=run.executor_id if run else None,
            promoted_at=run.promoted_at if run else None,
            superseded_at=run.superseded_at if run else None,
        ))
    return entries


def _legacy_recommendation_items(text: str) -> list[str]:
    """把旧版整体建议拆成可追踪条目；只用于人工决策，不补造证据关系。"""
    if not text.strip():
        return []
    detail = text.split("具体各点的最小改写建议如下：", 1)[-1]
    items = []
    for line in detail.splitlines():
        match = re.match(r"^\s*(?:[-•]|\d+[.．])\s*(.+?)\s*$", line)
        if match:
            items.append(match.group(1))
    return items or [text.strip()]


def read_item_decisions(project_id: str, recommendation_id: str) -> list[ReviewItemDecision]:
    latest = _latest_records(_item_decisions_path(project_id, recommendation_id), "item_key")
    return [
        ReviewItemDecision.model_validate(record)
        for _, record in sorted(latest.items(), key=lambda pair: int(pair[1].get("item_index", 0)))
    ]


def record_item_decision(
    project_id: str,
    finding_id: str,
    recommendation_id: str,
    *,
    item_key: str,
    item_index: int,
    item_kind: str,
    item_label: str,
    disposition: str,
    edited_text: str = "",
    edited_value: Any = None,
    has_edited_value: bool = False,
    notes: str = "",
    actor_type: str = "human",
    actor_id: str = "admin_user",
) -> ReviewItemDecision:
    """记录单条建议的处理意见；不关闭 finding，也不写入业务正文。"""
    finding = get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    recommendation = read_recommendation(project_id, recommendation_id)
    if recommendation is None or recommendation.finding_id != finding_id:
        raise KeyError(f"recommendation 不存在或不属于该 finding: {recommendation_id}")
    if disposition == "edited" and not has_edited_value and not edited_text.strip():
        raise ValueError("修改后采纳必须填写修改值")

    if item_kind == "claim":
        if item_index >= len(recommendation.claim_reviews):
            raise ValueError("建议条目索引不存在")
        expected_label = recommendation.claim_reviews[item_index].claim
        expected_key = f"claim:{item_index}"
    elif item_kind == "legacy_suggestion":
        if recommendation.claim_reviews:
            raise ValueError("结构化建议不能按旧版条目记录")
        legacy_items = _legacy_recommendation_items(recommendation.proposed_text)
        if item_index >= len(legacy_items):
            raise ValueError("旧版建议条目索引不存在")
        expected_label = legacy_items[item_index]
        expected_key = f"legacy:{item_index}"
    else:
        raise ValueError(f"不支持的建议条目类型: {item_kind}")
    if item_key != expected_key or item_label.strip() != expected_label.strip():
        raise ValueError("建议条目已变化，请刷新后重试")

    decision = ReviewItemDecision(
        decision_id=f"item-decision-{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        finding_id=finding_id,
        recommendation_id=recommendation_id,
        item_key=item_key,
        item_index=item_index,
        item_kind=item_kind,
        item_label=expected_label,
        disposition=disposition,
        edited_text=edited_text,
        edited_value=copy.deepcopy(edited_value),
        has_edited_value=has_edited_value,
        notes=notes,
        actor_type=actor_type,
        actor_id=actor_id,
        created_at=_now(),
    )
    evidence_refs = (
        recommendation.claim_reviews[item_index].evidence_refs
        if item_kind == "claim"
        else recommendation.evidence_refs
    )
    with _lock(project_id):
        _append_jsonl(_item_decisions_path(project_id, recommendation_id), decision.model_dump(mode="json"))
        update_finding(project_id, finding_id, status="in_review")
        _audit(
            project_id,
            review_id=decision.decision_id,
            review_type="item_decision",
            target={
                "finding_id": finding_id,
                "recommendation_id": recommendation_id,
                "item_key": item_key,
            },
            decision=disposition,
            action="record_item_decision",
            actor_type=actor_type,
            actor_id=actor_id,
            modified=False,
            notes=notes or expected_label,
            evidence_refs=evidence_refs,
        )
    return decision


def finalize_item_decisions(
    project_id: str,
    finding_id: str,
    recommendation_id: str,
    *,
    actor_id: str = "admin_user",
) -> ReviewDecisionFinalization:
    """汇总逐条人工决定；需要改正文时只生成 patch，不直接关闭 finding。"""
    finding = get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    if finding.recommendation_id != recommendation_id:
        raise ValueError("只能汇总当前推荐；请刷新后重试")
    recommendation = read_recommendation(project_id, recommendation_id)
    if recommendation is None or recommendation.finding_id != finding_id:
        raise KeyError(f"recommendation 不存在或不属于该 finding: {recommendation_id}")

    if recommendation.claim_reviews:
        item_keys = [f"claim:{index}" for index in range(len(recommendation.claim_reviews))]
    else:
        item_keys = [f"legacy:{index}" for index, _ in enumerate(_legacy_recommendation_items(recommendation.proposed_text))]
    decisions = {item.item_key: item for item in read_item_decisions(project_id, recommendation_id)}
    missing = [item_key for item_key in item_keys if item_key not in decisions]
    if missing:
        raise ValueError(f"还有 {len(missing)} 条建议未确认")

    selected = [decisions[item_key] for item_key in item_keys]
    if any(item.disposition == "needs_rerun" for item in selected):
        updated = update_finding(
            project_id,
            finding_id,
            disposition="needs_rerun",
            needs_rerun=True,
            status="open",
            verification_status="pending_followup",
            verification_actor_type="human",
            verification_actor_id=actor_id,
            notes="逐条复核后确认需要定向重跑。",
        )
        return ReviewDecisionFinalization(finding=updated, outcome="needs_rerun", message="已标记为需定向重跑。")
    if any(item.disposition == "deferred" for item in selected):
        updated = update_finding(
            project_id,
            finding_id,
            disposition="deferred",
            status="open",
            verification_status="pending_followup",
            verification_actor_type="human",
            verification_actor_id=actor_id,
            notes="逐条复核中仍有暂缓项。",
        )
        return ReviewDecisionFinalization(finding=updated, outcome="deferred", message="仍有暂缓项，finding 保持开启。")

    proposed_by_item = {item.item_key: item for item in recommendation.proposed_operations}
    operations: list[dict[str, Any]] = []
    unresolved_changes: list[str] = []
    for decision in selected:
        if decision.disposition == "keep_original":
            continue
        proposed = proposed_by_item.get(decision.item_key)
        if proposed is not None:
            if decision.disposition == "edited":
                value = decision.edited_value if decision.has_edited_value else decision.edited_text
            else:
                value = proposed.proposed_value
            operations.append({"op": proposed.operation, "path": proposed.path, "value": value})
            continue
        if decision.item_kind == "claim":
            claim = recommendation.claim_reviews[decision.item_index]
            content_action = claim.content_action or ("keep" if claim.action in {"keep", "sync_status", "add_source", "repair_reference"} else "narrow")
            evidence_only = content_action == "keep" and claim.action in {"keep", "sync_status"}
            if evidence_only and decision.disposition == "accepted":
                continue
            if claim.target_path and decision.disposition == "edited":
                value = decision.edited_value if decision.has_edited_value else decision.edited_text
                operations.append({"op": "replace", "path": claim.target_path, "value": value})
                continue
            if claim.target_path and claim.proposed_text and decision.disposition == "accepted":
                operations.append({"op": "replace", "path": claim.target_path, "value": claim.proposed_text})
                continue
        unresolved_changes.append(decision.item_label)

    if unresolved_changes:
        labels = "、".join(unresolved_changes[:4])
        raise ValueError(f"以下采纳项缺少可执行的字段路径或结构化操作：{labels}。请编辑后重试或重新生成结构化建议。")

    if operations:
        file = str((finding.target or {}).get("file") or "")
        if not file or file == "outputs/poi_details/manifest.json":
            raise ValueError("当前复核对象没有可直接修改的规范业务文件")
        patch = propose_patch(
            project_id,
            finding_id=finding_id,
            file=file,
            operations=operations,
            actor_type="human",
            actor_id=actor_id,
        )
        updated = update_finding(
            project_id,
            finding_id,
            disposition="deferred",
            status="awaiting_apply",
            verification_status="pending_patch_apply",
            verification_actor_type="human",
            verification_actor_id=actor_id,
            notes="逐条决定已汇总并生成 Patch，等待人工应用。",
        )
        return ReviewDecisionFinalization(
            finding=updated,
            outcome="awaiting_apply",
            patch_id=patch.patch_id,
            effective_status="awaiting_apply",
            message="已生成 Patch，应用并验证后才会关闭问题。",
        )

    all_keep_original = all(item.disposition == "keep_original" for item in selected)
    disposition = "false_positive" if all_keep_original else "resolved"
    updated = record_verification(
        project_id,
        finding_id,
        verification_status="false_positive" if all_keep_original else "passed_with_review",
        verification_method="itemized_admin_review",
        disposition=disposition,
        actor_type="human",
        actor_id=actor_id,
        notes="已完成全部建议条目的逐条判断；正文无需修改，人工复核结论已生效。",
        recommendation_id=recommendation_id,
    )
    return ReviewDecisionFinalization(
        finding=updated,
        outcome="closed",
        effective_status="review_passed",
        message="人工复核已完成，原始生成状态保留为审计信息。",
    )


def record_verification(
    project_id: str,
    finding_id: str,
    *,
    verification_status: str,
    verification_method: str,
    disposition: str = "resolved",
    actor_type: str = "model",
    actor_id: str = "",
    notes: str = "",
    recommendation_id: str | None = None,
) -> ReviewFinding:
    """记录模型或人工复核结论，不修改业务正文。"""
    finding = get_finding(project_id, finding_id)
    if finding is None:
        raise KeyError(f"finding 不存在: {finding_id}")
    is_closed = disposition in {"resolved", "false_positive"}
    updated = update_finding(
        project_id,
        finding_id,
        disposition=disposition,
        verification_status=verification_status,
        verification_actor_type=actor_type,
        verification_actor_id=actor_id,
        verification_method=verification_method,
        recommendation_id=recommendation_id or finding.recommendation_id,
        status="closed" if is_closed else "open",
        resolved_fingerprint=finding.observed_fingerprint if is_closed else finding.resolved_fingerprint,
        notes=notes or finding.notes,
    )
    _audit(
        project_id,
        review_id=f"review-{uuid.uuid4().hex[:12]}",
        review_type="content_review",
        target=updated.target,
        decision=verification_status,
        action="record_verification",
        actor_type=actor_type,
        actor_id=actor_id,
        modified=False,
        notes=notes,
        evidence_refs=updated.evidence_refs,
    )
    workspace_store.log_event(
        project_id,
        actor_id or actor_type,
        "review_verification",
        f"review_findings/{finding_id}",
        notes or f"定向复核结论：{disposition}",
    )
    return updated


def validate_project_state(project_id: str) -> dict[str, Any]:
    """扫描规范业务对象，并叠加人工复核形成的有效状态。"""
    scan = _canonical_state_observations(project_id)
    findings = read_findings(project_id)
    findings_by_key: dict[str, ReviewFinding] = {}
    for finding in findings:
        if finding.superseded_by or finding.finding_kind == "derived_mirror":
            continue
        key = _legacy_canonical_key(finding)
        if key and key not in findings_by_key:
            findings_by_key[key] = finding

    unresolved_paths: list[str] = []
    raw_paths: list[str] = list(scan.get("raw_force_finalized_paths") or [])
    resolved_paths: list[str] = []
    effective_objects: list[dict[str, Any]] = []
    for observation in scan["observations"]:
        key = observation["canonical_key"]
        path = str(observation["target"].get("residue_path") or observation["target"].get("file") or key)
        if observation["problem_type"] == "force_finalized_residue":
            raw_paths.append(path)
        finding = findings_by_key.get(key)
        resolved_fingerprint = finding.resolved_fingerprint if finding else ""
        if finding and not resolved_fingerprint and finding.status == "closed":
            resolved_fingerprint = finding.observed_fingerprint
        is_resolved = bool(
            finding
            and finding.status == "closed"
            and str(finding.disposition) in {"resolved", "false_positive", "FindingDisposition.RESOLVED", "FindingDisposition.FALSE_POSITIVE"}
            and resolved_fingerprint
            and resolved_fingerprint == observation["observed_fingerprint"]
        )
        if is_resolved:
            resolved_paths.append(path)
            effective_status = "review_passed"
        else:
            unresolved_paths.append(path)
            effective_status = "needs_review"
        effective_objects.append({
            "canonical_key": key,
            "finding_id": finding.finding_id if finding else None,
            "raw_status": (
                "force_finalized"
                if observation["problem_type"] == "force_finalized_residue"
                else "contract_failed"
                if observation["problem_type"] == "final_guide_contract_violation"
                else "inconsistent"
            ),
            "effective_status": effective_status,
            "path": path,
        })

    return {
        "project_id": project_id,
        "ok": not unresolved_paths,
        "force_finalized_paths": [
            item["path"]
            for item in effective_objects
            if item["raw_status"] == "force_finalized" and item["effective_status"] == "needs_review"
        ],
        "contract_violation_paths": [
            item["path"]
            for item in effective_objects
            if item["raw_status"] == "contract_failed" and item["effective_status"] == "needs_review"
        ],
        "raw_force_finalized_paths": list(dict.fromkeys(raw_paths)),
        "resolved_by_review_paths": resolved_paths,
        "derived_mirrors": scan["derived_mirrors"],
        "manifest_mismatches": scan["manifest_mismatches"],
        "effective_objects": effective_objects,
        "checked_at": _now(),
    }


def collect_optimization_candidates(project_id: str, threshold: int = 2) -> list[dict[str, Any]]:
    """按项目聚合同类 finding，生成可供工程确认的优化候选。"""
    findings = read_findings(project_id)
    grouped: dict[tuple[str, str], list[ReviewFinding]] = {}
    high_impact = {"code", "evidence"}
    for finding in findings:
        key = (str(finding.root_cause), finding.problem_type)
        grouped.setdefault(key, []).append(finding)
    candidates: list[dict[str, Any]] = []
    existing = {
        (item.get("root_cause"), item.get("problem_type"), tuple(sorted(item.get("finding_ids", []))))
        for item in read_optimization_candidates(project_id)
    }
    for (root_cause, problem_type), matches in grouped.items():
        if len(matches) < threshold and root_cause not in high_impact:
            continue
        candidate = {
            "candidate_id": f"optimization-{uuid.uuid4().hex[:12]}",
            "project_id": project_id,
            "root_cause": root_cause,
            "problem_type": problem_type,
            "finding_ids": [item.finding_id for item in matches],
            "occurrence_count": len(matches),
            "status": "candidate",
            "created_at": _now(),
            "suggested_actions": ["工程确认根因", "补充回归测试", "评估历史文件迁移"],
        }
        signature = (root_cause, problem_type, tuple(sorted(candidate["finding_ids"])))
        if signature in existing:
            continue
        existing.add(signature)
        candidates.append(candidate)
    if candidates:
        with _lock(project_id):
            for candidate in candidates:
                _append_jsonl(_optimization_path(project_id), candidate)
                _audit(
                    project_id,
                    review_id=candidate["candidate_id"],
                    review_type="optimization",
                    target={"problem_type": candidate["problem_type"]},
                    decision="candidate",
                    action="create_optimization_candidate",
                    actor_type="system",
                    actor_id="review_engine",
                    modified=False,
                    notes="同类finding达到升级阈值。",
                )
    return candidates


def read_optimization_candidates(project_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(_optimization_path(project_id))
