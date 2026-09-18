"""src/tools/web_search_tools.py 的单元测试——不调用真实Tavily API，monkeypatch
requests.post。覆盖：未配置key时的stub降级、正常返回结果的格式化、
无结果、请求异常、多key轮询（429/额度耗尽换下一个key重试）几种场景，跟
src/tools/amap_tools.py的stub模式对称、跟src/llm_rotation.py的多key轮询同一个动机。

**2026-08-01更新**：key的注入方式从直接monkeypatch`TAVILY_API_KEYS`模块常量，
改为通过`src.api.key_store`（管理后台"模型/Key"页面CRUD化后，key池真正来源）。

**2026-08-12更新**：新增project_id/log_scope参数+web_search_log.jsonl证据落盘/
reset_web_search_log()的测试（docs/ISSUES.md第115条方向C）。
"""
import json

import requests
import pytest

import src.tools.web_search_tools as web_search_module
from src.api import key_store
from src.tools.web_search_tools import make_web_search_tools, reset_web_search_log


@pytest.fixture(autouse=True)
def isolated_key_store(tmp_path, monkeypatch):
    monkeypatch.setattr(key_store, "KEY_STORE_PATH", tmp_path / "runtime_config" / "api_keys.json")
    monkeypatch.setattr("src.config.TAVILY_API_KEYS", [])
    monkeypatch.setattr(web_search_module, "PROJECTS_DIR", tmp_path / "projects")


def _get_tool(project_id="test-project", log_scope="test_agent"):
    tools = make_web_search_tools(project_id, log_scope)
    return next(t for t in tools if t.name == "web_search")


def _reset_key_index(monkeypatch):
    monkeypatch.setattr(web_search_module, "_current_key_index", 0)


def _seed_tavily_keys(*keys: str) -> None:
    for k in keys:
        key_store.add_key("tavily", k)


def test_web_search_returns_stub_message_when_no_keys_configured():
    tool = _get_tool()

    result = tool.invoke({"query": "成都今天天气"})

    assert "未配置" in result
    assert "现场核实" in result


def test_web_search_records_research_channel_when_context_is_bound(monkeypatch):
    from src.materials import research_service

    calls = []
    monkeypatch.setattr(
        research_service,
        "record_external_research",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    tool = make_web_search_tools(
        "project-1",
        "poi_detail_agent/day4_东台吉乃尔湖",
        research_context={
            "actor": "poi_detail_agent",
            "stage": "poi_detail",
            "target_key": "poi_detail:day:4:poi:东台吉乃尔湖",
        },
    )[0]

    tool.invoke({"query": "东台吉乃尔湖 2026 开放"})

    assert calls[0][1]["source_kind"] == "web"
    assert calls[0][1]["status"] == "unavailable"
    assert calls[0][1]["target_key"] == "poi_detail:day:4:poi:东台吉乃尔湖"


def test_web_search_formats_results_when_configured(monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {
                "results": [
                    {"title": "成都天气预报", "content": "今天晴，25-32度", "url": "https://example.com/weather"},
                ]
            }

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _FakeResponse())

    result = _get_tool().invoke({"query": "成都今天天气"})

    assert "成都天气预报" in result
    assert "25-32度" in result
    assert "https://example.com/weather" in result


def test_web_search_handles_empty_results(monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {"results": []}

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _FakeResponse())

    result = _get_tool().invoke({"query": "一个查不到任何东西的query"})

    assert "没有找到相关结果" in result
    assert "现场核实" in result


def test_web_search_handles_request_exception(monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    def _raise(*a, **k):
        raise requests.RequestException("connection timed out")

    monkeypatch.setattr(web_search_module.requests, "post", _raise)

    result = _get_tool().invoke({"query": "成都今天天气"})

    assert "所有已配置的key都不可用" in result
    assert "现场核实" in result


def test_web_search_rotates_to_next_key_on_rate_limit(monkeypatch):
    """第一个key返回429（限流/额度耗尽）时，应该自动换第二个key重试并成功，
    不应该把429直接暴露给调用方——跟src/llm_rotation.py的多key轮询同一个动机。"""
    _seed_tavily_keys("key-a", "key-b")
    _reset_key_index(monkeypatch)

    calls = []

    class _RateLimited:
        status_code = 429
        ok = False

    class _Success:
        status_code = 200
        ok = True

        def json(self):
            return {"results": [{"title": "标题", "content": "内容", "url": "https://example.com"}]}

    def fake_post(url, json, timeout):
        calls.append(json["api_key"])
        return _RateLimited() if json["api_key"] == "key-a" else _Success()

    monkeypatch.setattr(web_search_module.requests, "post", fake_post)

    result = _get_tool().invoke({"query": "成都今天天气"})

    assert calls == ["key-a", "key-b"]
    assert "标题" in result


def test_web_search_reports_when_all_keys_rate_limited(monkeypatch):
    _seed_tavily_keys("key-a", "key-b")
    _reset_key_index(monkeypatch)

    class _RateLimited:
        status_code = 429
        ok = False

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _RateLimited())

    result = _get_tool().invoke({"query": "成都今天天气"})

    assert "所有已配置的key都不可用" in result
    assert "限流" in result
    assert "现场核实" in result


def test_web_search_non_rate_limit_error_does_not_retry_other_keys(monkeypatch):
    """非429/432/433的4xx/5xx错误（比如query本身有问题）不该浪费其余key去重试，
    应该直接如实报告失败。"""
    _seed_tavily_keys("key-a", "key-b")
    _reset_key_index(monkeypatch)

    calls = []

    class _BadRequest:
        status_code = 400
        ok = False

    def fake_post(url, json, timeout):
        calls.append(json["api_key"])
        return _BadRequest()

    monkeypatch.setattr(web_search_module.requests, "post", fake_post)

    result = _get_tool().invoke({"query": "成都今天天气"})

    assert calls == ["key-a"]  # 不应该继续尝试key-b
    assert "请求失败" in result


def _log_path(tmp_path, project_id="test-project", log_scope="test_agent"):
    return tmp_path / "projects" / project_id / "agents" / log_scope / "web_search_log.jsonl"


def test_web_search_success_appends_to_log(tmp_path, monkeypatch):
    """真正查到结果时，应该把query+结果落盘到agents/<log_scope>/web_search_log.jsonl
    ——供citation_check核验platform=="web_verified"来源时读取（docs/ISSUES.md第115条方向C）。"""
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {
                "results": [
                    {"title": "翡翠湖门票", "content": "门票70元起", "url": "https://example.com/a"},
                ]
            }

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _FakeResponse())

    _get_tool().invoke({"query": "大柴旦翡翠湖 门票"})

    log_path = _log_path(tmp_path)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["schema_version"] == "web-search-log-v2"
    assert entry["query"] == "大柴旦翡翠湖 门票"
    assert entry["created_at"]
    assert entry["results"] == [
        {
            "title": "翡翠湖门票",
            "excerpt": "门票70元起",
            "url": "https://example.com/a",
        }
    ]


def test_web_search_log_keeps_each_result_bound_to_its_url(tmp_path, monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {
                "results": [
                    {"title": "来源A", "content": "A只讲门票", "url": "https://example.com/a"},
                    {"title": "来源B", "content": "B讲一处入口", "url": "https://example.com/b"},
                ]
            }

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _FakeResponse())

    _get_tool().invoke({"query": "测试景点"})

    entry = json.loads(_log_path(tmp_path).read_text(encoding="utf-8"))
    assert entry["results"][0]["url"] == "https://example.com/a"
    assert entry["results"][0]["excerpt"] == "A只讲门票"
    assert entry["results"][1]["url"] == "https://example.com/b"
    assert entry["results"][1]["excerpt"] == "B讲一处入口"


def test_web_search_success_appends_multiple_calls_same_log(tmp_path, monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {"results": [{"title": "标题", "content": "内容", "url": "https://example.com"}]}

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _FakeResponse())

    tool = _get_tool()
    tool.invoke({"query": "query1"})
    tool.invoke({"query": "query2"})

    lines = _log_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_web_search_no_keys_does_not_write_log(tmp_path):
    _get_tool().invoke({"query": "成都今天天气"})
    assert not _log_path(tmp_path).exists()


def test_web_search_empty_results_does_not_write_log(tmp_path, monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {"results": []}

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _FakeResponse())

    _get_tool().invoke({"query": "一个查不到任何东西的query"})

    assert not _log_path(tmp_path).exists()


def test_web_search_request_exception_does_not_write_log(tmp_path, monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    def _raise(*a, **k):
        raise requests.RequestException("connection timed out")

    monkeypatch.setattr(web_search_module.requests, "post", _raise)

    _get_tool().invoke({"query": "成都今天天气"})

    assert not _log_path(tmp_path).exists()


def test_reset_web_search_log_clears_file(tmp_path, monkeypatch):
    _seed_tavily_keys("fake-key")
    _reset_key_index(monkeypatch)

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {"results": [{"title": "标题", "content": "内容", "url": "https://example.com"}]}

    monkeypatch.setattr(web_search_module.requests, "post", lambda *a, **k: _FakeResponse())

    _get_tool().invoke({"query": "query1"})
    assert _log_path(tmp_path).exists()

    reset_web_search_log("test-project", "test_agent")

    assert not _log_path(tmp_path).exists()


def test_reset_web_search_log_missing_file_is_noop(tmp_path):
    reset_web_search_log("test-project", "never-written-scope")
