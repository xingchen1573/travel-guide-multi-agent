import json

from src.review import agent as review_agent
from src.review import executors, skill_bridge
from src.review import store as review_store
from src.workspace import store as workspace_store


def test_cloudflare_challenge_is_summarized_and_retryable():
    error = RuntimeError(
        '<!DOCTYPE html><html><head><title>Just a moment...</title></head>'
        '<script>window._cf_chl_opt={cRay: "ray-test-123"}</script>'
        '<div>Enable JavaScript and cookies to continue</div>'
        '<a href="https://challenges.cloudflare.com">challenge</a></html>'
    )

    message, retryable = executors._summarize_run_error(error)

    assert retryable is True
    assert "Cloudflare 人机验证" in message
    assert "ray-test-123" in message
    assert "<!DOCTYPE html>" not in message


def test_generic_run_error_is_bounded_for_display():
    message, retryable = executors._summarize_run_error(RuntimeError("x" * 1000))

    assert retryable is False
    assert len(message) == 601
    assert message.endswith("…")


def test_failed_standard_run_persists_summary_and_keeps_raw_error_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "PROJECTS_DIR", tmp_path / "projects")
    review_store._LOCKS.clear()
    project_id = "review-executor-error-project"
    workspace_store.init_project(project_id)
    workspace_store.write_requirements(
        project_id,
        "示例城",
        {"destination": "示例城", "days": 1},
    )
    output = workspace_store.workspace_dir(project_id) / "outputs" / "demo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"name": "示例"}, ensure_ascii=False), encoding="utf-8")
    finding = review_store.create_finding(
        project_id,
        problem_type="content",
        target={"file": "outputs/demo.json", "json_path": "/name"},
    )

    class FakeService:
        def create_job(self, *args, **kwargs):
            return None

        def start_job(self, *args, **kwargs):
            return True

        def update_progress(self, *args, **kwargs):
            return None

        def fail_job(self, _run_id, payload):
            assert payload["retryable"] is True
            assert "Cloudflare 人机验证" in payload["message"]
            return True

    service = FakeService()
    monkeypatch.setattr(executors, "get_job_service", lambda: service)
    monkeypatch.setattr(executors, "get_current_instance_id", lambda: "test-instance")
    bundle = skill_bridge.export_review_bundle(project_id, finding.finding_id)
    run = executors._create_run(
        project_id,
        finding.finding_id,
        executor_id="standard_model",
        bundle=bundle,
        comparison_group_id=None,
        channel="gptload",
    )
    raw_error = (
        '<!DOCTYPE html><title>Just a moment...</title>'
        '<script>window._cf_chl_opt={cRay: "ray-artifact"}</script>'
        '<div>Enable JavaScript</div><a href="https://challenges.cloudflare.com">challenge</a>'
    )
    monkeypatch.setattr(
        review_agent,
        "generate_agentic_recommendation",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(raw_error)),
    )

    executors._run_standard(project_id, run.run_id)

    saved = review_store.read_review_run(project_id, run.run_id)
    assert saved is not None
    assert saved.status == "failed"
    assert "Cloudflare 人机验证" in (saved.error or "")
    assert "<!DOCTYPE html>" not in (saved.error or "")
    artifact = workspace_store.workspace_dir(project_id) / saved.artifact_paths["error"]
    assert raw_error in json.loads(artifact.read_text(encoding="utf-8"))["message"]
