import json

from src.materials import baseline


def _seed_material_library(root, topic="兰州"):
    index_dir = root / "index" / topic
    raw_dir = root / "raw" / topic
    index_dir.mkdir(parents=True)
    raw_dir.mkdir(parents=True)
    (index_dir / "entries.jsonl").write_text(
        json.dumps({"doc_id": "doc-1", "source_file": "one.md"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (index_dir / "chunks.jsonl").write_text(
        json.dumps(
            {"chunk_id": "chunk-1", "doc_id": "doc-1", "text": "兰州牛肉面"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (raw_dir / "manifest.json").write_text(
        json.dumps({"items": [{"source_file": "one.md"}]}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_material_baseline_detects_corpus_drift(tmp_path):
    material_root = tmp_path / "material_library"
    _seed_material_library(material_root)
    frozen = baseline.build_material_baseline(
        "兰州",
        "兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )

    chunks = material_root / "index" / "兰州" / "chunks.jsonl"
    chunks.write_text(chunks.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    result = baseline.validate_material_baseline(
        frozen,
        topic="兰州",
        destination="兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )

    assert result["status"] == "failed"
    assert any(issue["code"] == "material_corpus_changed" for issue in result["issues"])


def test_material_baseline_detects_prompt_drift(tmp_path):
    material_root = tmp_path / "material_library"
    _seed_material_library(material_root)
    prompt_path = tmp_path / "src" / "agents" / "route_agent" / "prompt.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("before", encoding="utf-8")
    frozen = baseline.build_material_baseline(
        "兰州",
        "兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )

    prompt_path.write_text("after", encoding="utf-8")
    result = baseline.validate_material_baseline(
        frozen,
        topic="兰州",
        destination="兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )

    assert result["status"] == "failed"
    assert any(
        issue["code"] == "prompt_fingerprint_changed" for issue in result["issues"]
    )


def test_material_baseline_records_actual_structured_provider_model(
    tmp_path, monkeypatch
):
    material_root = tmp_path / "material_library"
    _seed_material_library(material_root)
    monkeypatch.setattr(baseline, "GPTLOAD_CHAT_MODEL", "glm-5.2")
    monkeypatch.setattr(baseline, "TRAVEL_MODEL_PROVIDER", "gptload")

    frozen = baseline.build_material_baseline(
        "兰州",
        "兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )

    assert frozen["model_boundary"]["structured_judge_model"] == "glm-5.2"
    assert frozen["model_boundary"]["structured_judge_provider"] == "gptload"


def test_material_baseline_records_and_validates_jiji_provider(
    tmp_path, monkeypatch
):
    material_root = tmp_path / "material_library"
    _seed_material_library(material_root)
    monkeypatch.setattr(baseline, "TRAVEL_MODEL_PROVIDER", "jiji")
    monkeypatch.setattr(baseline, "JIJI_CHAT_MODEL", "glm-5.3-flash")

    frozen = baseline.build_material_baseline(
        "兰州",
        "兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )

    assert frozen["model_boundary"]["structured_judge_provider"] == "jiji"
    assert frozen["model_boundary"]["structured_judge_model"] == "glm-5.3-flash"
    assert baseline.validate_material_baseline(
        frozen,
        topic="兰州",
        destination="兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )["status"] == "passed"

    monkeypatch.setattr(baseline, "TRAVEL_MODEL_PROVIDER", "gptload")
    result = baseline.validate_material_baseline(
        frozen,
        topic="兰州",
        destination="兰州",
        material_library_dir=material_root,
        project_root=tmp_path,
    )

    assert result["status"] == "failed"
    assert any(
        issue.get("field") == "structured_judge_provider"
        for issue in result["issues"]
    )
