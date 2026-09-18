"""run_generate_poster.py 的单元测试：纯代码筛选/拼prompt逻辑 + 确认/重试流程用mock覆盖，
不发起真实网络请求（真实生图调用已经在2026-07-24 /grilling讨论中手动验证过，见对话记录）。
"""
from __future__ import annotations

import pytest

from scripts.delivery import run_generate_poster as m
from src.workspace import store
from src.workspace.schema import (
    LifestyleCategory,
    LifestyleItem,
    LifestyleOutput,
    PitfallCategory,
    PitfallItem,
    PitfallOutput,
    RouteDay,
    RoutePlan,
    RoutePoi,
)


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "poster-gen-test"
    store.init_project(project_id)
    return project_id


def _sample_route_plan() -> RoutePlan:
    return RoutePlan(
        days=[
            RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠"), RoutePoi(name="锦里古街")]),
            RouteDay(day=2, city="成都", pois=[RoutePoi(name="宽窄巷子")]),
        ]
    )


def _three_day_route_plan(destination: str) -> RoutePlan:
    return RoutePlan(
        days=[
            RouteDay(day=day, city=destination, pois=[RoutePoi(name=f"{destination}景点{day}")])
            for day in range(1, 4)
        ]
    )


def test_build_route_lines():
    lines = m.build_route_lines(_sample_route_plan())
    assert lines == ["Day1路线：武侯祠 → 锦里古街", "Day2路线：宽窄巷子"]


def test_build_route_lines_skips_days_without_pois():
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[])])
    assert m.build_route_lines(plan) == []


def test_shorten_keeps_short_text_unchanged():
    assert m._shorten("很短的一句话") == "很短的一句话"


def test_shorten_cuts_at_first_period():
    text = "第一句话说完了。第二句话很长很长很长很长很长很长很长很长很长很长很长很长很长"
    assert m._shorten(text) == "第一句话说完了"


def test_shorten_hard_truncates_with_ellipsis_when_no_early_separator():
    text = "一" * 60
    result = m._shorten(text, max_len=10)
    assert result == "一" * 10 + "…"


def test_shorten_strips_simple_meta_prefix_before_truncating():
    text = "多个检索素材反馈胡二哥药膳蹄花的药膳味较重，一上桌是很浓的黄芪当归味儿"
    result = m._shorten(text, max_len=20)
    assert not result.startswith("多个检索素材")
    assert result.startswith("胡二哥药膳蹄花")


def test_shorten_strips_list_intro_meta_prefix_and_keeps_real_content():
    text = "检索到有博主对杜甫草堂做了详细分析，指出以下踩坑点：\n\n1. 本质是纪念性博物馆，非杜甫原址"
    result = m._shorten(text, max_len=30)
    assert "指出以下踩坑点" not in result
    assert "本质是纪念性博物馆" in result


def test_shorten_avoids_dangling_quote_when_truncation_lands_mid_quote():
    text = "鹤鸣茶社人流量极大，尤其是节假日和好天气的下午时段基本爆满：'想过人多，但没想到过人这么多'"
    result = m._shorten(text, max_len=40)
    assert result.count("'") % 2 == 0
    assert not result.endswith("'…")


def test_distill_highlights_empty_returns_empty():
    assert m._distill_highlights([]) == []


def test_distill_highlights_skips_llm_call_when_all_texts_short(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("短文本不应该触发轻量模型调用")

    monkeypatch.setattr(m, "get_light_chat_model", fail_if_called)
    result = m._distill_highlights(["药膳味较重", "人流量极大"])
    assert result == ["药膳味较重", "人流量极大"]


def test_distill_highlights_uses_light_model_for_long_text(monkeypatch):
    long_text = "多个检索素材反馈胡二哥药膳蹄花的药膳味较重，一上桌是很浓的黄芪当归味儿，并非清淡药香"

    class FakeResponse:
        content = "药膳味偏重"

    class FakeModel:
        def invoke(self, prompt):
            assert "药膳蹄花" in prompt
            return FakeResponse()

    monkeypatch.setattr(m, "get_light_chat_model", lambda temperature=0: FakeModel())
    result = m._distill_highlights([long_text])
    assert result == ["药膳味偏重"]


def test_distill_highlights_strips_leading_enumeration_if_model_adds_it(monkeypatch):
    long_text_a = "多个检索素材反馈胡二哥药膳蹄花的药膳味较重，一上桌是很浓的黄芪当归味儿，并非清淡药香"
    long_text_b = "多个检索素材均提到鹤鸣茶社人流量极大，尤其是节假日和好天气的下午时段基本爆满"

    class FakeResponse:
        content = "1. 药膳味偏重\n2、人多易拥堵"

    class FakeModel:
        def invoke(self, prompt):
            return FakeResponse()

    monkeypatch.setattr(m, "get_light_chat_model", lambda temperature=0: FakeModel())
    result = m._distill_highlights([long_text_a, long_text_b])
    assert result == ["药膳味偏重", "人多易拥堵"]


def test_distill_highlights_falls_back_when_light_model_raises(monkeypatch):
    long_text = "多个检索素材反馈胡二哥药膳蹄花的药膳味较重，一上桌是很浓的黄芪当归味儿，并非清淡药香"

    def raise_error(temperature=0):
        raise RuntimeError("模拟调用失败")

    monkeypatch.setattr(m, "get_light_chat_model", raise_error)
    result = m._distill_highlights([long_text])
    assert result == [m._shorten(long_text)]


def test_distill_highlights_falls_back_when_tag_count_mismatches(monkeypatch):
    long_text_a = "多个检索素材反馈胡二哥药膳蹄花的药膳味较重，一上桌是很浓的黄芪当归味儿，并非清淡药香"
    long_text_b = "多个检索素材均提到鹤鸣茶社人流量极大，尤其是节假日和好天气的下午时段基本爆满"

    class FakeResponse:
        content = "只有一条"  # 故意只返回1行，跟输入的2条对不上

    class FakeModel:
        def invoke(self, prompt):
            return FakeResponse()

    monkeypatch.setattr(m, "get_light_chat_model", lambda temperature=0: FakeModel())
    result = m._distill_highlights([long_text_a, long_text_b])
    assert result == [m._shorten(long_text_a), m._shorten(long_text_b)]


def test_select_pitfall_lines_none_returns_empty():
    assert m.select_pitfall_lines(None) == []


def test_select_pitfall_lines_flattens_categories_and_uses_entity_label():
    pitfall = PitfallOutput(
        food=PitfallCategory(
            items=[
                PitfallItem(
                    field="food", day=1, target_recommendation="x", warning="药膳味较重",
                    claimed_entities=["胡二哥药膳蹄花"],
                )
            ]
        ),
        play=PitfallCategory(
            items=[
                PitfallItem(field="play", day=2, target_recommendation="y", warning="人流量极大", claimed_entities=[])
            ]
        ),
    )
    lines = m.select_pitfall_lines(pitfall)
    assert lines == ["胡二哥药膳蹄花：药膳味较重", "Day2：人流量极大"]


def test_select_pitfall_lines_skips_empty_warning():
    pitfall = PitfallOutput(
        food=PitfallCategory(items=[PitfallItem(field="food", day=1, target_recommendation="x", warning="")])
    )
    assert m.select_pitfall_lines(pitfall) == []


def test_select_pitfall_lines_respects_limit():
    items = [
        PitfallItem(field="food", day=1, target_recommendation="x", warning=f"警告{i}") for i in range(10)
    ]
    pitfall = PitfallOutput(food=PitfallCategory(items=items))
    assert len(m.select_pitfall_lines(pitfall, limit=3)) == 3


def test_select_pitfall_lines_uses_distilled_tags(monkeypatch):
    monkeypatch.setattr(m, "_distill_highlights", lambda texts: [f"[{t}]" for t in texts])
    pitfall = PitfallOutput(
        food=PitfallCategory(
            items=[
                PitfallItem(
                    field="food", day=1, target_recommendation="x", warning="药膳味较重",
                    claimed_entities=["胡二哥药膳蹄花"],
                )
            ]
        )
    )
    assert m.select_pitfall_lines(pitfall) == ["胡二哥药膳蹄花：[药膳味较重]"]


def test_select_food_highlights_none_returns_empty():
    assert m.select_food_highlights(None) == []


def test_select_food_highlights_limits_per_day_and_sorts_by_day():
    lifestyle = LifestyleOutput(
        food=LifestyleCategory(
            items=[
                LifestyleItem(day=2, recommendation="推荐B1"),
                LifestyleItem(day=1, recommendation="推荐A1"),
                LifestyleItem(day=1, recommendation="推荐A2"),
                LifestyleItem(day=1, recommendation="推荐A3（超出per_day应被丢弃）"),
            ]
        )
    )
    lines = m.select_food_highlights(lifestyle, per_day=2)
    assert lines == ["Day1：推荐A1", "Day1：推荐A2", "Day2：推荐B1"]


def test_select_food_highlights_uses_distilled_tags(monkeypatch):
    monkeypatch.setattr(m, "_distill_highlights", lambda texts: [f"[{t}]" for t in texts])
    lifestyle = LifestyleOutput(food=LifestyleCategory(items=[LifestyleItem(day=1, recommendation="必吃蹄花")]))
    assert m.select_food_highlights(lifestyle) == ["Day1：[必吃蹄花]"]


def test_build_poster_title_uses_topic_when_present():
    assert m.build_poster_title("成都3天亲子游", 3) == "成都3天亲子游"


def test_build_poster_title_falls_back_to_day_count():
    assert m.build_poster_title("", 2) == "2天旅游攻略"


def test_build_poster_prompt_includes_all_sections():
    route_plan = _sample_route_plan()
    lifestyle = LifestyleOutput(food=LifestyleCategory(items=[LifestyleItem(day=1, recommendation="必吃蹄花")]))
    pitfall = PitfallOutput(
        food=PitfallCategory(
            items=[PitfallItem(field="food", day=1, target_recommendation="x", warning="味道偏重", claimed_entities=["蹄花"])]
        )
    )
    prompt = m.build_poster_prompt(route_plan, lifestyle, pitfall, topic="成都2天旅游攻略")
    assert "成都2天旅游攻略" in prompt
    assert "来自当前Project的结构化攻略结果" in prompt
    assert "不得弱化、隐藏或抹掉原文中的不确定性提示" in prompt
    assert "都已经过事实核验" not in prompt
    assert "Day1路线：武侯祠 → 锦里古街" in prompt
    assert "Day1：必吃蹄花" in prompt
    assert "蹄花：味道偏重" in prompt


def test_build_poster_prompt_omits_empty_sections():
    prompt = m.build_poster_prompt(_sample_route_plan(), None, None, topic="")
    assert "【必吃推荐】" not in prompt
    assert "【避坑指南】" not in prompt
    assert "【路线】" in prompt


@pytest.mark.parametrize(
    ("destination", "topic"),
    [
        ("海口", "海口3日休闲游"),
        ("洱海", "洱海3日骑行游"),
    ],
)
def test_three_day_destination_prompts_use_project_topic_and_route_days(
    isolated_project,
    destination,
    topic,
):
    route_plan = _three_day_route_plan(destination)
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=route_plan)
    store.write_requirements(
        isolated_project,
        topic=topic,
        requirements={"destination": destination, "days": 3},
    )
    (store.workspace_dir(isolated_project) / "final_guide.md").write_text(
        f"## Day 1｜{destination}行程\n\n{destination}景点1",
        encoding="utf-8",
    )

    overall_prompt = m.build_poster_prompt(route_plan, None, None, topic=topic)
    daily_prompt = m.build_daily_poster_prompt(isolated_project, day=1)

    assert f"《{topic}》" in overall_prompt
    assert "内容覆盖3天行程" in overall_prompt
    assert "Day1~Day3路线节点" in overall_prompt
    assert "8天自驾" not in overall_prompt
    assert f"《{topic}》单日攻略" in daily_prompt
    assert "青甘大环线" not in daily_prompt


@pytest.mark.parametrize("separator", [" | ", " · ", "｜"])
def test_read_day_source_accepts_supported_heading_separators(isolated_project, separator):
    (store.workspace_dir(isolated_project) / "final_guide.md").write_text(
        f"# 测试攻略\n\n## Day 1{separator}成都\n\n当天正文\n\n## Day 2 | 乐山\n\n次日正文\n",
        encoding="utf-8",
    )

    source = m._read_day_source(isolated_project, day=1)

    assert f"## Day 1{separator}成都" in source
    assert "当天正文" in source
    assert "Day 2" not in source
    assert "次日正文" not in source


def test_read_day_source_stops_at_next_non_day_h2(isolated_project):
    (store.workspace_dir(isolated_project) / "final_guide.md").write_text(
        "## Day 1 | 成都\n\n当天正文\n\n## 行前清单\n\n不应进入单日海报\n",
        encoding="utf-8",
    )

    source = m._read_day_source(isolated_project, day=1)

    assert "当天正文" in source
    assert "行前清单" not in source
    assert "不应进入单日海报" not in source


def test_read_day_source_missing_requested_day_raises(isolated_project):
    (store.workspace_dir(isolated_project) / "final_guide.md").write_text(
        "## Day 1 | 成都\n\n当天正文\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="没有找到Day2正文"):
        m._read_day_source(isolated_project, day=2)


def test_call_image_api_retries_once_then_succeeds(monkeypatch):
    calls = {"count": 0}

    def fake_request(prompt):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("模拟网关超时")
        return b"fake-image-bytes"

    monkeypatch.setattr(m, "_request_image_once", fake_request)
    result = m.call_image_api("prompt")
    assert result == b"fake-image-bytes"
    assert calls["count"] == 2


def test_call_image_api_raises_after_retry_exhausted(monkeypatch):
    def always_fail(prompt):
        raise RuntimeError("模拟持续失败")

    monkeypatch.setattr(m, "_request_image_once", always_fail)
    with pytest.raises(RuntimeError, match="已重试1次"):
        m.call_image_api("prompt")


def test_request_image_once_no_key_raises(monkeypatch, tmp_path):
    from src.api import key_store

    monkeypatch.setattr(key_store, "KEY_STORE_PATH", tmp_path / "runtime_config" / "api_keys.json")
    monkeypatch.setattr("src.config.POSTER_IMAGE_API_KEY", None)
    with pytest.raises(RuntimeError, match="生图Key"):
        m._request_image_once("prompt")


def test_generate_poster_no_route_plan_raises(isolated_project):
    with pytest.raises(RuntimeError, match="还没有route_plan"):
        m.generate_poster(isolated_project, confirm=False)


def test_generate_poster_writes_prompt_and_poster_without_confirm(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=_sample_route_plan())
    monkeypatch.setattr(m, "call_image_api", lambda prompt: b"fake-png-bytes")

    poster_path = m.generate_poster(isolated_project, confirm=False)

    assert poster_path.name == "poster.png"
    assert poster_path.read_bytes() == b"fake-png-bytes"
    prompt_path = poster_path.parent / "poster_prompt.txt"
    assert prompt_path.exists()
    assert "Day1路线：武侯祠 → 锦里古街" in prompt_path.read_text(encoding="utf-8")


def test_generate_poster_confirm_declined_cancels_without_calling_api(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=_sample_route_plan())

    def fail_if_called(prompt):
        raise AssertionError("确认被拒绝时不应该调用call_image_api")

    monkeypatch.setattr(m, "call_image_api", fail_if_called)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    with pytest.raises(SystemExit):
        m.generate_poster(isolated_project, confirm=True)


def test_generate_poster_confirm_accepted_calls_api(isolated_project, monkeypatch):
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=_sample_route_plan())
    monkeypatch.setattr(m, "call_image_api", lambda prompt: b"fake-png-bytes")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    poster_path = m.generate_poster(isolated_project, confirm=True)
    assert poster_path.read_bytes() == b"fake-png-bytes"
