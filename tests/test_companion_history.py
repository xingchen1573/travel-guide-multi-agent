import json

from src import companion_history
from src.workspace import store


def _project(tmp_path, monkeypatch, project_id="history-test"):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    store.init_project(project_id)
    return project_id


def test_recent_context_keeps_only_latest_two_turns(tmp_path, monkeypatch):
    project_id = _project(tmp_path, monkeypatch)
    companion_history.append_turn(project_id, "第一问", "第一答", agent="guide_agent")
    companion_history.append_turn(project_id, "第二问", "第二答", agent="realtime_agent")
    companion_history.append_turn(project_id, "第三问", "第三答", agent="companion_query")

    turns = companion_history.read_recent_turns(project_id)
    context = companion_history.build_recent_context(project_id)

    assert [turn["user_message"] for turn in turns] == ["第二问", "第三问"]
    assert "第一问" not in context
    assert context.index("第二问") < context.index("第三问")
    assert "只用于理解当前问题中的指代" in context
    assert "不自动视为已经核验的事实" in context


def test_recent_context_is_project_scoped(tmp_path, monkeypatch):
    first = _project(tmp_path, monkeypatch, "history-first")
    second = _project(tmp_path, monkeypatch, "history-second")
    companion_history.append_turn(first, "武侯祠几点关门", "下午停止入场")

    assert "武侯祠" in companion_history.build_recent_context(first)
    assert companion_history.build_recent_context(second) == ""


def test_recent_context_skips_malformed_lines_and_obeys_length_limit(tmp_path, monkeypatch):
    project_id = _project(tmp_path, monkeypatch)
    companion_history.append_turn(project_id, "问题", "回答" * 2000)
    path = store.project_dir(project_id) / "companion_history.jsonl"
    with path.open("a", encoding="utf-8") as file:
        file.write("not-json\n")
        file.write(json.dumps({"user_message": 123, "assistant_answer": "bad"}) + "\n")

    context = companion_history.build_recent_context(project_id, max_chars=300)

    assert "用户：问题" in context
    assert len(context) < 550


def test_empty_or_incomplete_turn_is_not_recorded(tmp_path, monkeypatch):
    project_id = _project(tmp_path, monkeypatch)
    companion_history.append_turn(project_id, "", "回答")
    companion_history.append_turn(project_id, "问题", "")

    assert companion_history.read_recent_turns(project_id) == []
