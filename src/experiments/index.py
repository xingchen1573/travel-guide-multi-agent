from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import PROJECTS_DIR, RUNTIME_DIR
from src.experiments.metrics import (
    CATALOG_PATH,
    extract_project_metrics,
    load_comparison_pairs,
    load_catalog_with_project_manifests,
    write_metrics,
)
from src.review import index as review_index
from src.review import store as review_store

DEFAULT_DB_PATH = RUNTIME_DIR / "experiment_index.sqlite3"


def database_path() -> Path:
    return Path(os.environ.get("EXPERIMENT_INDEX_DB", DEFAULT_DB_PATH))


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS experiments (
        project_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        scenario_id TEXT NOT NULL,
        experiment_version TEXT,
        harness TEXT NOT NULL,
        destination TEXT,
        trip_days INTEGER,
        route_sha TEXT,
        model TEXT,
        provider TEXT,
        project_status TEXT,
        completion_mode TEXT,
        started_at TEXT,
        completed_at TEXT,
        wall_clock_seconds REAL,
        comparison_grade TEXT,
        metrics_json TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        indexed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS experiment_relations (
        source_project_id TEXT NOT NULL,
        target_project_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        note TEXT,
        PRIMARY KEY (source_project_id, target_project_id, relation_type)
    );
    CREATE TABLE IF NOT EXISTS candidate_experiments (
        candidate_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        relation_role TEXT NOT NULL,
        PRIMARY KEY (candidate_id, project_id, relation_role)
    );
    CREATE TABLE IF NOT EXISTS comparison_pairs (
        pair_id TEXT PRIMARY KEY,
        codex_project_id TEXT NOT NULL,
        langgraph_project_id TEXT NOT NULL,
        source_input_project_id TEXT,
        comparison_scope TEXT NOT NULL,
        declared_grade TEXT NOT NULL,
        effective_grade TEXT NOT NULL,
        status TEXT NOT NULL,
        origin TEXT NOT NULL,
        note TEXT,
        evidence_refs_json TEXT NOT NULL,
        comparability_json TEXT NOT NULL,
        indexed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS candidate_pairs (
        candidate_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        relation_role TEXT NOT NULL,
        PRIMARY KEY (candidate_id, pair_id, relation_role)
    );
    CREATE INDEX IF NOT EXISTS idx_experiments_scenario ON experiments(scenario_id, completed_at);
    CREATE INDEX IF NOT EXISTS idx_experiments_harness ON experiments(harness, completed_at);
    CREATE INDEX IF NOT EXISTS idx_pairs_status ON comparison_pairs(status, effective_grade);
    """)


def _normalize_relation(raw: Any) -> dict[str, Any]:
    """Accept the current relation contract and the two legacy key names."""
    if not isinstance(raw, dict):
        raise ValueError("relation 必须是对象")
    target_project_id = raw.get("target_project_id") or raw.get("project_id")
    relation_type = raw.get("relation_type") or raw.get("type")
    if not target_project_id or not relation_type:
        raise ValueError("relation 缺少 target_project_id/relation_type")
    return {
        "target_project_id": str(target_project_id),
        "relation_type": str(relation_type),
        "note": raw.get("note"),
    }


def _comparison(left: Any, right: Any) -> dict[str, Any]:
    if left is None or right is None:
        state = "unknown"
    else:
        state = "match" if left == right else "different"
    return {"status": state, "codex": left, "langgraph": right}


def _project_complete(metrics: dict[str, Any]) -> bool:
    identity = metrics.get("identity") or {}
    return (
        identity.get("project_status") == "完成"
        and identity.get("artifact_status") == "deliverable"
    )


def _build_pair_projection(
    raw: dict[str, Any], metrics_by_id: dict[str, dict[str, Any]], *, indexed_at: str
) -> dict[str, Any]:
    pair_id = str(raw.get("pair_id") or "").strip()
    codex_project_id = str(raw.get("codex_project_id") or "").strip()
    langgraph_project_id = str(raw.get("langgraph_project_id") or "").strip()
    comparison_scope = str(raw.get("comparison_scope") or "system").strip()
    declared_grade = str(raw.get("declared_grade") or "C").upper()
    reasons: list[str] = []

    codex = metrics_by_id.get(codex_project_id)
    langgraph = metrics_by_id.get(langgraph_project_id)
    codex_identity = (codex or {}).get("identity") or {}
    langgraph_identity = (langgraph or {}).get("identity") or {}
    comparisons = {
        "requirements": _comparison(
            codex_identity.get("requirements_sha"), langgraph_identity.get("requirements_sha")
        ),
        "route": _comparison(codex_identity.get("route_sha"), langgraph_identity.get("route_sha")),
        "materials": _comparison(
            (codex or {}).get("research", {}).get("base_corpus_sha256"),
            (langgraph or {}).get("research", {}).get("base_corpus_sha256"),
        ),
        "model": _comparison(codex_identity.get("model"), langgraph_identity.get("model")),
        "provider": _comparison(codex_identity.get("provider"), langgraph_identity.get("provider")),
    }

    missing_members = [
        project_id
        for project_id, metrics in ((codex_project_id, codex), (langgraph_project_id, langgraph))
        if not project_id or metrics is None
    ]
    if comparison_scope not in {"system", "harness_isolated"}:
        status = "invalid"
        reasons.append("unknown_comparison_scope")
    elif declared_grade not in {"A", "B", "C"}:
        status = "invalid"
        reasons.append("unknown_declared_grade")
    elif missing_members:
        status = "incomplete"
        reasons.append("missing_pair_member")
    elif codex_project_id == langgraph_project_id:
        status = "invalid"
        reasons.append("same_project_on_both_sides")
    elif codex_identity.get("harness") != "codex_skill" or langgraph_identity.get("harness") != "langgraph":
        status = "invalid"
        reasons.append("harness_side_mismatch")
    elif comparisons["requirements"]["status"] != "match" or comparisons["route"]["status"] != "match":
        status = "invalid"
        reasons.append("fixed_input_mismatch")
    elif not _project_complete(codex) or not _project_complete(langgraph):
        status = "incomplete"
        reasons.append("member_not_complete")
    elif comparison_scope == "harness_isolated" and (
        comparisons["model"]["status"] != "match" or comparisons["provider"]["status"] != "match"
    ):
        status = "invalid"
        reasons.append("harness_isolation_contract_broken")
    else:
        status = "complete"

    grade_rank = {"A": 0, "B": 1, "C": 2}
    grade_values = [declared_grade]
    for identity in (codex_identity, langgraph_identity):
        grade = str(identity.get("comparison_grade") or "").upper()
        if grade in grade_rank:
            grade_values.append(grade)
    effective_grade = max(
        (grade for grade in grade_values if grade in grade_rank),
        key=grade_rank.get,
        default="C",
    )
    if status != "complete":
        effective_grade = "C"
    elif comparisons["materials"]["status"] == "unknown" and effective_grade == "A":
        effective_grade = "B"
        reasons.append("material_baseline_unknown")
    if comparison_scope == "system" and (
        comparisons["model"]["status"] == "different"
        or comparisons["provider"]["status"] == "different"
    ):
        reasons.append("system_comparison_not_harness_isolation")

    verdict = (
        "harness_isolated"
        if status == "complete" and comparison_scope == "harness_isolated"
        else "system_comparable"
        if status == "complete"
        else "incomplete"
        if status == "incomplete"
        else "not_comparable"
    )
    return {
        "pair_id": pair_id,
        "codex_project_id": codex_project_id,
        "langgraph_project_id": langgraph_project_id,
        "source_input_project_id": raw.get("source_input_project_id"),
        "comparison_scope": comparison_scope,
        "declared_grade": declared_grade if declared_grade in grade_rank else "C",
        "effective_grade": effective_grade,
        "status": status,
        "origin": str(raw.get("origin") or "catalog"),
        "note": raw.get("note"),
        "evidence_refs": list(raw.get("evidence_refs") or []),
        "comparability": {
            "verdict": verdict,
            "checks": comparisons,
            "reason_codes": reasons,
        },
        "indexed_at": indexed_at,
    }


def rebuild_index(*, projects_dir: Path = PROJECTS_DIR, catalog_path: Path = CATALOG_PATH, db_path: Path | None = None) -> dict[str, Any]:
    catalog = load_catalog_with_project_manifests(
        projects_dir=projects_dir,
        catalog_path=catalog_path,
    )
    existing: dict[str, str] = {}
    target_db = db_path or database_path()
    if target_db.is_file():
        with _connect(target_db) as conn:
            _create_schema(conn)
            existing = {str(row["project_id"]): str(row["source_fingerprint"]) for row in conn.execute("SELECT project_id, source_fingerprint FROM experiments")}
    metrics_rows: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "added": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "relation_failures": [],
    }
    for project_id, entry in catalog.items():
        try:
            metrics = extract_project_metrics(project_id, entry, projects_dir=projects_dir)
            metrics_rows.append(metrics)
            previous_path = projects_dir / project_id / "workspace" / "metrics.json"
            previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else None
            if previous and previous.get("source_fingerprint") == metrics["source_fingerprint"]:
                result["skipped"] += 1
            else:
                write_metrics(metrics, projects_dir=projects_dir)
                result["updated" if project_id in existing else "added"] += 1
        except Exception as exc:
            result["failed"] += 1
            result["failures"].append({"project_id": project_id, "error": str(exc)})
    now = datetime.now(timezone.utc).isoformat()
    candidates = review_index.aggregate_optimization_candidates()
    experiment_ids = {row["identity"]["project_id"] for row in metrics_rows}
    metrics_by_id = {row["identity"]["project_id"]: row for row in metrics_rows}
    pair_rows: list[dict[str, Any]] = []
    seen_pair_ids: set[str] = set()
    for raw_pair in load_comparison_pairs(catalog_path):
        pair_id = str(raw_pair.get("pair_id") or "").strip()
        if not pair_id or pair_id in seen_pair_ids:
            result["failures"].append({
                "project_id": pair_id or "<comparison_pair>",
                "stage": "comparison_pair",
                "error": "pair_id 缺失或重复",
            })
            result["failed"] += 1
            continue
        seen_pair_ids.add(pair_id)
        pair_rows.append(_build_pair_projection(raw_pair, metrics_by_id, indexed_at=now))
    with _connect(target_db) as conn:
        _create_schema(conn)
        with conn:
            conn.execute("DELETE FROM candidate_pairs")
            conn.execute("DELETE FROM comparison_pairs")
            conn.execute("DELETE FROM candidate_experiments")
            conn.execute("DELETE FROM experiment_relations")
            conn.execute("DELETE FROM experiments")
            for metrics in metrics_rows:
                identity = metrics["identity"]
                execution = metrics["execution"]
                conn.execute(
                    """INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        identity["project_id"], identity["schema_version"], identity["scenario_id"], identity.get("experiment_version"),
                        identity["harness"], identity.get("destination"), identity.get("trip_days"), identity.get("route_sha"),
                        identity.get("model"), identity.get("provider"), identity.get("project_status"), identity.get("completion_mode"),
                        execution["started_at"]["value"], execution["completed_at"]["value"], execution["wall_clock_seconds"]["value"],
                        identity.get("comparison_grade"), json.dumps(metrics, ensure_ascii=False, separators=(",", ":")), metrics["source_fingerprint"], now,
                    ),
                )
                for raw_relation in metrics.get("relations", []):
                    try:
                        relation = _normalize_relation(raw_relation)
                    except ValueError as exc:
                        result["relation_failures"].append({
                            "project_id": identity["project_id"],
                            "error": str(exc),
                            "relation": raw_relation,
                        })
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO experiment_relations VALUES (?, ?, ?, ?)",
                        (
                            identity["project_id"],
                            relation["target_project_id"],
                            relation["relation_type"],
                            relation.get("note"),
                        ),
                    )
            for pair in pair_rows:
                conn.execute(
                    """INSERT INTO comparison_pairs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pair["pair_id"],
                        pair["codex_project_id"],
                        pair["langgraph_project_id"],
                        pair.get("source_input_project_id"),
                        pair["comparison_scope"],
                        pair["declared_grade"],
                        pair["effective_grade"],
                        pair["status"],
                        pair["origin"],
                        pair.get("note"),
                        json.dumps(pair["evidence_refs"], ensure_ascii=False, separators=(",", ":")),
                        json.dumps(pair["comparability"], ensure_ascii=False, separators=(",", ":")),
                        pair["indexed_at"],
                    ),
                )
            for candidate in candidates:
                for project_id in candidate.get("project_ids") or []:
                    if project_id in experiment_ids:
                        conn.execute("INSERT OR REPLACE INTO candidate_experiments VALUES (?, ?, 'source')", (candidate["candidate_id"], project_id))
                for pair_id in candidate.get("source_pair_ids") or []:
                    if pair_id in seen_pair_ids:
                        conn.execute(
                            "INSERT OR REPLACE INTO candidate_pairs VALUES (?, ?, 'source')",
                            (candidate["candidate_id"], pair_id),
                        )
                for pair_id in candidate.get("validation_pair_ids") or []:
                    if pair_id in seen_pair_ids:
                        conn.execute(
                            "INSERT OR REPLACE INTO candidate_pairs VALUES (?, ?, 'validation')",
                            (candidate["candidate_id"], pair_id),
                        )
    result["indexed"] = len(metrics_rows)
    result["pairs"] = {
        "registered": len(pair_rows),
        "complete": sum(pair["status"] == "complete" for pair in pair_rows),
        "incomplete": sum(pair["status"] == "incomplete" for pair in pair_rows),
        "invalid": sum(pair["status"] == "invalid" for pair in pair_rows),
    }
    result["database_path"] = str(target_db)
    result["indexed_at"] = now
    return result


def _ensure_index() -> None:
    path = database_path()
    if not path.is_file():
        rebuild_index()


def list_experiments(*, destination: str | None = None, harness: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    _ensure_index()
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (("destination", destination), ("harness", harness), ("project_status", status)):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM experiments {where} ORDER BY COALESCE(completed_at, started_at, indexed_at) DESC, project_id DESC", values).fetchall()
        relations = conn.execute("SELECT * FROM experiment_relations").fetchall()
    rel_by_source: dict[str, list[dict[str, Any]]] = {}
    for row in relations:
        rel_by_source.setdefault(str(row["source_project_id"]), []).append(dict(row))
    results: list[dict[str, Any]] = []
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        results.append(
            {key: row[key] for key in row.keys() if key != "metrics_json"}
            | {
                "metrics_schema_version": row["schema_version"],
                "overall_status": (metrics.get("status") or {}).get("overall"),
                "relations": rel_by_source.get(str(row["project_id"]), []),
            }
        )
    return results


def _pair_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        key: row[key]
        for key in row.keys()
        if key not in {"evidence_refs_json", "comparability_json"}
    } | {
        "evidence_refs": json.loads(row["evidence_refs_json"]),
        "comparability": json.loads(row["comparability_json"]),
    }


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _compact_finding_text(value: Any, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def _pair_finding_snapshot(project_id: str) -> dict[str, Any]:
    """Project Finding 的只读预览；不触发扫描、回填或人工裁决。"""
    try:
        findings = review_store.read_findings(project_id)
    except (OSError, ValueError):
        findings = []
    active = [
        finding
        for finding in findings
        if finding.status != "closed"
        and not finding.superseded_by
        and _enum_text(finding.disposition) not in {"resolved", "false_positive"}
    ]
    severity_rank = {"high": 2, "medium": 1, "low": 0}
    active.sort(
        key=lambda finding: (
            severity_rank.get(str(finding.severity), 0),
            finding.updated_at or finding.created_at,
            finding.finding_id,
        ),
        reverse=True,
    )
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    problem_type_counts: dict[str, int] = {}
    for finding in active:
        severity = str(finding.severity)
        severity_counts[severity if severity in severity_counts else "medium"] += 1
        problem_type_counts[finding.problem_type] = problem_type_counts.get(finding.problem_type, 0) + 1
    return {
        "project_id": project_id,
        "active_finding_count": len(active),
        "severity_counts": severity_counts,
        "problem_type_counts": problem_type_counts,
        "preview_findings": [
            {
                "finding_id": finding.finding_id,
                "problem_type": finding.problem_type,
                "root_cause": _enum_text(finding.root_cause),
                "severity": str(finding.severity),
                "status": str(finding.status),
                "summary": _compact_finding_text(
                    finding.issue_summary or finding.notes or finding.problem_type
                ),
            }
            for finding in active[:3]
        ],
    }


def _stage_needs_attention(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized not in {
        "",
        "unknown",
        "pending",
        "not_started",
        "not_applicable",
        "passed",
        "completed",
        "complete",
        "ok",
        "success",
        "succeeded",
    }


def _pair_stage_observations(
    langgraph_metrics: dict[str, Any], codex_metrics: dict[str, Any]
) -> list[dict[str, Any]]:
    langgraph_stages = {
        str(stage.get("key")): stage
        for stage in langgraph_metrics.get("stages") or []
        if isinstance(stage, dict) and stage.get("key")
    }
    codex_stages = {
        str(stage.get("key")): stage
        for stage in codex_metrics.get("stages") or []
        if isinstance(stage, dict) and stage.get("key")
    }
    ordered_keys = list(langgraph_stages)
    ordered_keys.extend(key for key in codex_stages if key not in langgraph_stages)
    observations: list[dict[str, Any]] = []
    for key in ordered_keys:
        langgraph = langgraph_stages.get(key) or {}
        codex = codex_stages.get(key) or {}
        langgraph_status = str(langgraph.get("status") or "unknown")
        codex_status = str(codex.get("status") or "unknown")
        statuses_differ = langgraph_status != codex_status
        if not statuses_differ and not _stage_needs_attention(langgraph_status):
            continue
        observations.append(
            {
                "key": key,
                "label": str(langgraph.get("label") or codex.get("label") or key),
                "langgraph_status": langgraph_status,
                "codex_status": codex_status,
                "kind": "different" if statuses_differ else "shared_attention",
            }
        )
    return observations


def list_comparison_pairs(*, status: str | None = None) -> list[dict[str, Any]]:
    _ensure_index()
    with _connect() as conn:
        _create_schema(conn)
        if status:
            rows = conn.execute(
                "SELECT * FROM comparison_pairs WHERE status = ? ORDER BY indexed_at DESC, pair_id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM comparison_pairs ORDER BY indexed_at DESC, pair_id"
            ).fetchall()
        experiment_rows = conn.execute("SELECT * FROM experiments").fetchall()

    experiment_by_id: dict[str, dict[str, Any]] = {}
    metrics_by_id: dict[str, dict[str, Any]] = {}
    for experiment_row in experiment_rows:
        metrics = json.loads(experiment_row["metrics_json"])
        metrics_by_id[str(experiment_row["project_id"])] = metrics
        experiment_by_id[str(experiment_row["project_id"])] = {
            key: experiment_row[key]
            for key in experiment_row.keys()
            if key != "metrics_json"
        } | {
            "metrics_schema_version": experiment_row["schema_version"],
            "overall_status": (metrics.get("status") or {}).get("overall"),
        }

    candidates = review_index.aggregate_optimization_candidates()
    candidate_links: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for candidate in candidates:
        for role, field in (
            ("source", "source_pair_ids"),
            ("validation", "validation_pair_ids"),
        ):
            for pair_id in candidate.get(field) or []:
                roles = candidate_links.setdefault(
                    str(pair_id), {"source": [], "validation": []}
                )
                roles[role].append(candidate)

    results: list[dict[str, Any]] = []
    for row in rows:
        pair = _pair_from_row(row)
        pair["codex_experiment"] = experiment_by_id.get(pair["codex_project_id"])
        pair["langgraph_experiment"] = experiment_by_id.get(pair["langgraph_project_id"])
        links = candidate_links.get(pair["pair_id"], {"source": [], "validation": []})
        pair["source_candidates"] = links["source"]
        pair["validation_candidates"] = links["validation"]
        langgraph_findings = _pair_finding_snapshot(pair["langgraph_project_id"])
        codex_findings = _pair_finding_snapshot(pair["codex_project_id"])
        stage_observations = _pair_stage_observations(
            metrics_by_id.get(pair["langgraph_project_id"], {}),
            metrics_by_id.get(pair["codex_project_id"], {}),
        )
        pair["observed_issues"] = {
            "langgraph": langgraph_findings,
            "codex": codex_findings,
            "active_finding_count": (
                langgraph_findings["active_finding_count"]
                + codex_findings["active_finding_count"]
            ),
            "stage_observation_count": len(stage_observations),
            "stage_observations": stage_observations,
        }
        results.append(pair)
    return results


def get_comparison_pair(pair_id: str) -> dict[str, Any] | None:
    return next(
        (pair for pair in list_comparison_pairs() if pair["pair_id"] == pair_id),
        None,
    )


def get_experiment(project_id: str) -> dict[str, Any] | None:
    _ensure_index()
    with _connect() as conn:
        row = conn.execute("SELECT metrics_json, indexed_at FROM experiments WHERE project_id = ?", (project_id,)).fetchone()
        if row is None:
            return None
        relations = [dict(item) for item in conn.execute("SELECT * FROM experiment_relations WHERE source_project_id = ? ORDER BY relation_type, target_project_id", (project_id,))]
        candidate_ids = [str(item["candidate_id"]) for item in conn.execute("SELECT candidate_id FROM candidate_experiments WHERE project_id = ?", (project_id,))]
        pair_ids = [
            str(item["pair_id"])
            for item in conn.execute(
                "SELECT pair_id FROM comparison_pairs WHERE codex_project_id = ? OR langgraph_project_id = ? ORDER BY pair_id",
                (project_id, project_id),
            )
        ]
    metrics = json.loads(row["metrics_json"])
    candidates = [item for item in review_index.aggregate_optimization_candidates() if item["candidate_id"] in candidate_ids]
    return {
        "metrics": metrics,
        "relations": relations,
        "comparison_pair_ids": pair_ids,
        "optimization_candidates": candidates,
        "indexed_at": row["indexed_at"],
    }
