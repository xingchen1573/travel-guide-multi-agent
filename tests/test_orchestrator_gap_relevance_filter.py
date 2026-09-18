"""_filter_relevant_candidates（素材缺口自动补充采集的相关性粗筛，docs/ISSUES.md第75条）
单元测试：mock LLM返回，不调真实模型/网络。

背景：search_and_rank按热度/时长/时新度排序，完全不考虑标题内容是否真的跟目的地/缺失
实体相关——真实复现过（第75条）候选池被字面命中关键词但内容毫不相关的高热度视频（游戏
解说、沙雕动画）挤占。_filter_relevant_candidates在真正下载/转写前，用轻量模型对候选
标题做一次相关性粗筛，明确带上topic目的地上下文。
"""
from __future__ import annotations

from src import orchestrator


class _FakeVerdict:
    def __init__(self, relevant_indices: list[int]):
        self.relevant_indices = relevant_indices


class _FakeStructuredModel:
    """按标题决定relevant的假模型，模拟get_light_chat_model(...).with_structured_output(...)。"""

    def __init__(self, relevant_titles: set[str]):
        self._relevant_titles = relevant_titles

    def invoke(self, prompt: str):
        relevant_indices = []
        for line in prompt.splitlines():
            prefix, separator, title = line.partition(". ")
            if separator and prefix.isdigit() and title in self._relevant_titles:
                relevant_indices.append(int(prefix))
        return _FakeVerdict(relevant_indices)


class _FakeLightModel:
    def __init__(self, relevant_titles: set[str]):
        self._relevant_titles = relevant_titles

    def with_structured_output(self, schema, method=None):
        return _FakeStructuredModel(self._relevant_titles)


def _candidate(title: str) -> dict:
    return {"title": title, "author": "up主", "source_url": f"https://www.bilibili.com/video/{title}"}


def test_filters_out_irrelevant_titles(monkeypatch):
    monkeypatch.setattr(
        orchestrator, "get_light_chat_model", lambda temperature=0: _FakeLightModel({"喜洲粑粑真实测评"})
    )
    candidates = [
        _candidate("【原神】映夏活动攻略"),
        _candidate("喜洲粑粑真实测评"),
        _candidate("三角洲行动无敌粑粑枪"),
    ]
    result = orchestrator._filter_relevant_candidates("洱海骑行", "喜洲粑粑", candidates)
    assert [c["title"] for c in result] == ["喜洲粑粑真实测评"]


def test_empty_candidates_returns_empty_without_calling_model(monkeypatch):
    def fail_if_called(temperature=0):
        raise AssertionError("候选为空时不该调用模型")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", fail_if_called)
    assert orchestrator._filter_relevant_candidates("洱海骑行", "喜洲粑粑", []) == []


def test_model_exception_keeps_candidate_instead_of_dropping(monkeypatch):
    """粗筛本身出错（模型调用异常）不应该武断丢弃候选——真正的质量门禁是下载/转写后
    ingest_topic的is_relevant判断（更权威、看的是全文），这里不确定时应该放行而不是拦截。"""

    class _RaisingModel:
        def with_structured_output(self, schema, method=None):
            raise RuntimeError("模型调用失败")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _RaisingModel())
    candidates = [_candidate("喜洲粑粑某个候选")]
    result = orchestrator._filter_relevant_candidates("洱海骑行", "喜洲粑粑", candidates)
    assert result == candidates


def test_batch_invoke_exception_keeps_all_candidates(monkeypatch):
    class _RaisingStructuredModel:
        def invoke(self, prompt):
            raise RuntimeError("批量判定超时")

    class _LightModel:
        def with_structured_output(self, schema, method=None):
            return _RaisingStructuredModel()

    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _LightModel())
    candidates = [_candidate("金顶候选A"), _candidate("金顶候选B")]

    assert orchestrator._filter_relevant_candidates("峨眉山", "金顶", candidates) == candidates


def test_prompt_includes_topic_and_entity_hint_context(monkeypatch):
    """模型只看标题短文本判断不出"是否讲这个地方"，prompt必须明确带上topic目的地
    和缺失实体上下文，不能只喂标题本身。"""
    captured_prompts = []

    class _CapturingModel:
        def invoke(self, prompt):
            captured_prompts.append(prompt)
            return _FakeVerdict([0])

    class _CapturingLightModel:
        def with_structured_output(self, schema, method=None):
            return _CapturingModel()

    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _CapturingLightModel())
    orchestrator._filter_relevant_candidates("洱海骑行", "周城", [_candidate("周城某视频标题")])

    assert len(captured_prompts) == 1
    assert "洱海骑行" in captured_prompts[0]
    assert "周城" in captured_prompts[0]
    assert "周城某视频标题" in captured_prompts[0]


def test_batches_all_candidates_into_one_model_call(monkeypatch):
    captured_prompts = []

    class _CapturingModel:
        def invoke(self, prompt):
            captured_prompts.append(prompt)
            return _FakeVerdict([0, 2])

    class _CapturingLightModel:
        def with_structured_output(self, schema, method=None):
            return _CapturingModel()

    monkeypatch.setattr(orchestrator, "get_light_chat_model", lambda temperature=0: _CapturingLightModel())
    candidates = [_candidate("金顶候选A"), _candidate("金顶候选B"), _candidate("金顶候选C")]

    result = orchestrator._filter_relevant_candidates("峨眉山", "金顶", candidates)

    assert len(captured_prompts) == 1
    assert [candidate["title"] for candidate in result] == ["金顶候选A", "金顶候选C"]


def test_deterministic_prefilter_rejects_generic_travel_titles_without_model_call(monkeypatch):
    def fail_if_called(temperature=0):
        raise AssertionError("未命中目的地或实体的泛旅游标题不应调用模型")

    monkeypatch.setattr(orchestrator, "get_light_chat_model", fail_if_called)
    candidates = [
        _candidate("冬天去哪玩？一南一北都挑了一些强特色的"),
        _candidate("9月国庆去哪玩？基本都适合蹦跶"),
    ]

    assert orchestrator._filter_relevant_candidates("峨眉山", "圣积寺", candidates) == []
