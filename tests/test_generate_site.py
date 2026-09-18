"""run_generate_site.py 的单元测试：纯程序拼接，不调用LLM。用mock的final_guide.md+
poi_details构造一个假project，验证输出HTML里包含关键内容（景点名、途经点、recommended标记）。

森林页真实地图（2026-07-24接入）相关测试mock掉src.tools.geocode.get_cached_or_geocode，
不发起真实网络请求。

树木页视觉升级（2026-07-24接入）后，POI详情不再由Python侧拼成静态HTML，而是打包成JSON
payload嵌入<script>由客户端JS渲染（#view-tree容器+hash路由）。这意味着这批测试大多只能
断言"JSON payload里有没有正确的字段/文本"，无法真正断言"点击路线tab/打卡按钮后DOM是否
切换正确"这类运行时交互行为——那部分靠人工浏览器实测覆盖，见docs/ISSUES.md第40条。

森林页视觉迁移（2026-07-26接入）后，森林页天数长卷（封面/day-num/land地标卡/普通poi
节点）改用route_plan.json结构化数据服务端直接渲染成HTML（不像树木页那样走JSON
payload+客户端JS渲染），所以这部分可以直接断言渲染出的HTML结构本身，见下方新增测试。
"""
from urllib.parse import quote

import pytest

from scripts.delivery import run_generate_site as site_module
from scripts.delivery.run_generate_site import generate_site
from src.review import store as review_store
from src.workspace import store
from src.workspace.final_guide_sections import POI_SECTION_HEADING, REFERENCE_SECTION_HEADING
from src.workspace.schema import PoiDetailFile, RouteDay, RoutePlan, RoutePoi, RouteOption, RouteSegment


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "site-gen-test"
    store.init_project(project_id)
    return project_id


def test_generate_site_includes_forest_and_tree_content(isolated_project, tmp_path):
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md="# 成都3日游\n\nDay1: 宽窄巷子、太古里\nDay2: 青城山",
        meta={},
    )
    detail = PoiDetailFile(
        poi_name="青城山",
        day=2,
        routes=[
            RouteOption(
                route_name="前山经典线",
                recommended=True,
                recommend_reason="休闲游客更适合",
                route_segments=[
                    RouteSegment(order=1, point_name="山门", highlight="起点，检票入园", suggested_duration="10分钟"),
                    RouteSegment(
                        order=2,
                        point_name="天师洞",
                        highlight="道教文化核心景点",
                        image_prompt="青城山天师洞，古朴道观建筑",
                    ),
                ],
            ),
            RouteOption(route_name="后山生态徒步线", recommended=False, route_segments=[]),
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "output" / "site.html"
    result_path = generate_site(isolated_project, output_path)

    assert result_path == output_path
    html_content = output_path.read_text(encoding="utf-8")

    assert "成都3日游" in html_content
    assert "青城山" in html_content
    assert "山门" in html_content
    assert "天师洞" in html_content
    assert "前山经典线" in html_content
    assert "后山生态徒步线" in html_content
    assert "青城山天师洞，古朴道观建筑" in html_content  # image_prompt占位展示
    # 树木视图路由本身依赖JS（hash路由+转场），不再是"纯锚点跳转不含JS"
    assert "<script" in html_content
    assert 'id="view-forest"' in html_content
    assert 'id="view-tree"' in html_content
    # 具体某条路线是否recommended，直接断言JSON payload里的字段值，而不是断言
    # 客户端渲染出来的badge文案（那段文案是脚本源码里的固定字符串，不管数据如何都会出现）
    assert '"recommended": true' in html_content
    assert '"recommended": false' in html_content
    assert f"href='#/tree/{quote('青城山')}'" in html_content


def test_generate_site_uses_empty_manifest_as_authoritative_active_set(isolated_project, tmp_path):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    detail = PoiDetailFile(poi_name="武侯祠", day=1, routes=[])
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)
    store.write_poi_details_manifest(isolated_project, actor="main_agent", manifest=__import__(
        "src.workspace.schema", fromlist=["PoiDetailManifest"]
    ).PoiDetailManifest(generated=[]))

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)

    html_content = output_path.read_text(encoding="utf-8")
    assert "武侯祠" not in html_content


def test_generate_site_handles_missing_final_guide(isolated_project, tmp_path):
    """final_guide.md还没生成（比如中途查看）时不应该报错，如实展示"还没生成"。"""
    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)

    html_content = output_path.read_text(encoding="utf-8")
    assert "还没有生成" in html_content


def test_copy_poster_assets_does_not_reuse_legacy_posters_for_new_qinggan_project(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    fake_module = repo_root / "scripts" / "delivery" / "run_generate_site.py"
    fake_module.parent.mkdir(parents=True)
    (repo_root / "整体.png").write_bytes(b"legacy-overall")
    monkeypatch.setattr(site_module, "__file__", str(fake_module))

    workspace_dir = tmp_path / "workspace"
    assets_dir = tmp_path / "assets"
    workspace_dir.mkdir()

    copied = site_module._copy_poster_assets(
        workspace_dir,
        assets_dir,
        "langgraph-qinggan-8d-fixed-route-20260907-v7-p0-e2e",
    )

    assert copied == set()
    assert not (assets_dir / "posters" / "overall.png").exists()


def test_copy_poster_assets_keeps_exact_legacy_qinggan_fallback(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    fake_module = repo_root / "scripts" / "delivery" / "run_generate_site.py"
    fake_module.parent.mkdir(parents=True)
    (repo_root / "整体.png").write_bytes(b"legacy-overall")
    (repo_root / "day1.png").write_bytes(b"legacy-day-1")
    monkeypatch.setattr(site_module, "__file__", str(fake_module))

    workspace_dir = tmp_path / "workspace"
    assets_dir = tmp_path / "assets"
    workspace_dir.mkdir()

    copied = site_module._copy_poster_assets(
        workspace_dir,
        assets_dir,
        "custom-qinggan-linear-8d-v8-20260813",
    )

    assert copied == {"overall", "day1"}
    assert (assets_dir / "posters" / "overall.png").read_bytes() == b"legacy-overall"
    assert (assets_dir / "posters" / "day1.png").read_bytes() == b"legacy-day-1"


def test_generate_site_uses_actual_day_count_in_overall_poster_copy(isolated_project, tmp_path):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 成都3日游", meta={})
    plan = RoutePlan(days=[
        RouteDay(day=1, city="成都"),
        RouteDay(day=2, city="成都"),
        RouteDay(day=3, city="成都"),
    ])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    (output_path.parent / "poster.png").write_bytes(b"poster")
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "查看3天总览海报" in html_content
    assert "8天总览海报" not in html_content
    assert "const assetTitle = key => key === 'overall' ? '总览海报'" in html_content
    assert "Array.from({length: 8}" not in html_content


def test_generate_site_tree_hero_photo_keeps_desktop_ratio_without_mobile_overflow(
    isolated_project, tmp_path
):
    """桌面保留稳定比例，窄屏取消比例，避免min-height反推宽度造成横向溢出。"""
    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)

    html_content = output_path.read_text(encoding="utf-8")
    assert ".tree-hero.has-photo{background-size:cover;background-position:center;" in html_content
    assert "aspect-ratio:16/10;min-height:260px}" in html_content
    assert "@media (max-width:600px){\n  .tree-hero.has-photo{aspect-ratio:auto}" in html_content


def test_generate_site_renders_forest_markdown_as_html(isolated_project, tmp_path):
    """森林页此前直接把final_guide.md原文转义塞进<pre>，用户会看到#/|这类markdown语法
    符号原样呈现——改为真正渲染成标题/表格等HTML元素。"""
    store.write_final_guide(
        isolated_project,
        actor="integration_agent",
        content_md=(
            "# 成都3日游\n\n"
            "## Day 1\n\n"
            "| 时间 | 地点 |\n"
            "|---|---|\n"
            "| 上午 | 宽窄巷子 |\n\n"
            "> 提示：注意防晒\n"
        ),
        meta={},
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "<h1>成都3日游</h1>" in html_content
    assert "<h2>Day 1</h2>" in html_content
    assert "<table>" in html_content
    assert "<th>时间</th>" in html_content
    assert "<td>宽窄巷子</td>" in html_content
    assert "<blockquote>" in html_content
    # 原始markdown语法符号不应该原样出现在最终HTML里
    assert "|---|---|" not in html_content


def test_generate_site_escapes_script_closing_tag_in_payload(isolated_project, tmp_path):
    """POI详情的highlight等字段是模型生成的自由文本，理论上可能恰好包含'</script>'，
    如果直接json.dumps塞进<script>标签会提前把标签腰斩——必须转义后再嵌入。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    detail = PoiDetailFile(
        poi_name="青城山",
        day=1,
        routes=[
            RouteOption(
                route_name="测试线",
                route_segments=[
                    RouteSegment(order=1, point_name="测试点", highlight="部分内容</script>后续"),
                ],
            ),
        ],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "部分内容</script>后续" not in html_content
    assert "部分内容<\\/script>后续" in html_content


def test_generate_site_includes_citation_status_banner_data(isolated_project, tmp_path):
    """citation_status=force_finalized时，JSON payload要带上这个字段，供客户端JS
    渲染核验提示banner（tree-banner）。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    detail = PoiDetailFile(poi_name="武侯祠", day=1, routes=[], citation_status="force_finalized")
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert '"citation_status": "force_finalized"' in html_content


def test_generate_site_tree_index_uses_route_plan_city_area(isolated_project, tmp_path):
    """树木页英雄区的面包屑（Day N · 区域）需要route_plan.json里的city/area，
    按day+name匹配对应POI，匹配不到不报错。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", area="武侯区", pois=[RoutePoi(name="武侯祠", brief="三国文化")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    detail = PoiDetailFile(poi_name="武侯祠", day=1, routes=[])
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert '"area": "武侯区"' in html_content
    assert '"city": "成都"' in html_content


def test_generate_site_attaches_overview_photo_to_poi_detail(isolated_project, tmp_path, monkeypatch):
    """PoiDetailFile整体查到高德真实照片时，应把overview_photo_url写进这个
    detail（随trees_payload一起打包进JSON），并把完整原图落到site_assets。
    配图粒度是"整个POI一张图"，不是逐途经点——见run_generate_site.py文件头
    "POI真实照片"一节的演变记录。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都3日游", requirements={})
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[RouteOption(route_name="经典线", route_segments=[
            RouteSegment(order=1, point_name="惠陵"),
        ])],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    captured = {}

    def fake_photo(topic, name):
        captured["topic"] = topic
        captured["name"] = name
        return "data:image/jpeg;base64,aGVsbG8="

    monkeypatch.setattr(site_module, "get_cached_or_poi_overview_photo", fake_photo)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert captured == {"topic": "成都3日游", "name": "武侯祠"}
    assert '"overview_photo_url": "site_assets/img-' in html_content
    assert len(list((output_path.parent / "site_assets").glob("*.jpg"))) >= 1


def test_generate_site_no_overview_photo_omits_field(isolated_project, tmp_path, monkeypatch):
    """查不到照片（未配key/不相关/无照片）时不应写入overview_photo_url字段，
    前端据此保留原有英雄区样式（无背景图）。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[RouteOption(route_name="经典线", route_segments=[
            RouteSegment(order=1, point_name="惠陵"),
        ])],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    monkeypatch.setattr(site_module, "get_cached_or_poi_overview_photo", lambda topic, name: None)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "data:image" not in html_content


def test_generate_site_overview_photo_not_scoped_by_city(isolated_project, tmp_path, monkeypatch):
    """多城市/无法确定归属的day.city不应影响overview照片查询——查询词是poi_name
    本身，不依赖route_plan的city字段，这正是从途经点级别方案迁移到POI整体级别
    方案要解决的问题：长线自驾行程（day.city是'敦煌→嘉峪关→张掖'这种多城市接续）
    以前会导致整天所有途经点都拿不到图，现在poi_name本身足够具体，不受影响。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="敦煌→嘉峪关→张掖", pois=[RoutePoi(name="张掖七彩丹霞景区")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    detail = PoiDetailFile(
        poi_name="张掖七彩丹霞景区", day=1,
        routes=[RouteOption(route_name="经典线", route_segments=[
            RouteSegment(order=1, point_name="景区入口"),
        ])],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    captured = {}

    def fake_photo(topic, name):
        captured["name"] = name
        return "data:image/jpeg;base64,fakedata"

    monkeypatch.setattr(site_module, "get_cached_or_poi_overview_photo", fake_photo)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)

    assert captured["name"] == "张掖七彩丹霞景区"


def test_generate_site_uploaded_overview_photo_takes_priority_over_auto_fetch(
    isolated_project, tmp_path, monkeypatch
):
    """用户手动上传的POI整体照片（uploaded_overview_photo_path）应该优先于高德自动
    查图——不应该再调用get_cached_or_poi_overview_photo，且credit要标"用户上传"不是
    "高德地图"，见管理后台"POI图片"页面（src/api/routers/poi_photos.py）。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都3日游", requirements={})
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[RouteOption(route_name="经典线", route_segments=[
            RouteSegment(order=1, point_name="惠陵"),
        ])],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)
    store.write_poi_overview_photo(
        isolated_project, "admin_user", 1, "武侯祠", b"\xff\xd8\xff" + b"upload-bytes", "jpg"
    )

    auto_fetch_called = {"count": 0}

    def fake_photo(topic, name):
        auto_fetch_called["count"] += 1
        return "data:image/jpeg;base64,should-not-be-used"

    monkeypatch.setattr(site_module, "get_cached_or_poi_overview_photo", fake_photo)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert auto_fetch_called["count"] == 0
    assert "should-not-be-used" not in html_content
    assert "用户上传" in html_content


def test_generate_site_segment_uploaded_photo_included_in_html(isolated_project, tmp_path):
    """途经点(segment)照片只有用户上传这一条路径——uploaded_photo_path非空时应该
    在生成的HTML里带上对应的静态资源路径，供前端渲染.t-photo（不是.img-ph占位）。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[RouteOption(route_name="经典线", route_segments=[
            RouteSegment(order=1, point_name="惠陵"),
        ])],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)
    store.write_poi_segment_photo(
        isolated_project, "admin_user", 1, "武侯祠", "经典线", 1, b"\xff\xd8\xff" + b"seg-bytes", "jpg"
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert '"uploaded_photo_url": "site_assets/img-' in html_content
    assert len(list((output_path.parent / "site_assets").glob("*.jpg"))) >= 1


def test_generate_site_segment_without_uploaded_photo_omits_field(isolated_project, tmp_path):
    """没上传途经点照片时不应该出现uploaded_photo_url字段，前端据此保留
    .img-ph占位框渲染分支。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    detail = PoiDetailFile(
        poi_name="武侯祠", day=1,
        routes=[RouteOption(route_name="经典线", route_segments=[
            RouteSegment(order=1, point_name="惠陵"),
        ])],
    )
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    # 注意：不能用"uploaded_photo_url" not in html_content——这个字符串本身
    # 作为JS属性访问（s.uploaded_photo_url）永远会出现在renderTCards源码里，
    # 要检查的是JSON payload里没有这个key（带引号+冒号，是JSON序列化才有的形态）。
    assert '"uploaded_photo_url":' not in html_content


def test_generate_site_map_renders_with_coordinates(isolated_project, tmp_path, monkeypatch):
    """有坐标数据+配置了AMAP_JS_API_KEY时，应渲染真实地图div+高德JS API script标签。
    这里显式把AMAP_JS_SECURITY_CODE设为None（旧key场景），确认没配安全密钥时地图仍能
    正常渲染（只是没有安全校验），跟test_generate_site_map_injects_security_config_when_configured
    互相对照。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(site_module, "AMAP_JS_SECURITY_CODE", None)
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "id=\"map\"" in html_content
    assert "webapi.amap.com/maps" in html_content
    assert "fake-js-key" in html_content
    assert "武侯祠" in html_content
    assert "_AMapSecurityConfig" not in html_content


def test_generate_site_map_injects_security_config_when_configured(isolated_project, tmp_path, monkeypatch):
    """配置了AMAP_JS_SECURITY_CODE时，应在加载高德JS API脚本之前注入
    window._AMapSecurityConfig——这是高德2021年底起JS API的强制要求，漏配会导致
    INVALID_USER_SCODE报错而不是优雅降级，详见docs/ISSUES.md。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(site_module, "AMAP_JS_SECURITY_CODE", "fake-security-code")
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "_AMapSecurityConfig" in html_content
    assert "fake-security-code" in html_content
    # 安全密钥必须在加载maps脚本之前设置，否则地图脚本加载时读不到，顺序错了等于没配。
    assert html_content.index("_AMapSecurityConfig") < html_content.index("webapi.amap.com/maps")


def test_generate_site_map_falls_back_without_js_key(isolated_project, tmp_path, monkeypatch):
    """有坐标数据但AMAP_JS_API_KEY未配置时，应降级为POI列表，不应尝试渲染地图div。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", None)
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "id=\"map\"" not in html_content
    assert "未配置高德地图Key" in html_content
    assert "武侯祠" in html_content


def test_generate_site_map_falls_back_without_any_coordinates(isolated_project, tmp_path, monkeypatch):
    """坐标查询全部失败（如未配置AMAP_GEOCODE_API_KEY）时，应优雅降级、不报错、不影响其余内容。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "get_cached_or_geocode", lambda topic, name, city="": None)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "id=\"map\"" not in html_content
    assert "暂无可展示的POI坐标" in html_content
    assert "攻略正文" in html_content


def test_generate_site_map_handles_no_route_plan(isolated_project, tmp_path):
    """route_plan.json还没生成时（比如中途查看），地图区域应优雅降级，不报错。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "id=\"map\"" not in html_content
    assert "攻略正文" in html_content


def test_generate_site_map_markers_are_numbered_and_colored(isolated_project, tmp_path, monkeypatch):
    """路线地图升级：图钉要按天配色+按访问顺序编号，而不是清一色默认蓝图钉。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[
        RoutePoi(name="武侯祠"), RoutePoi(name="锦里"),
    ])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert '"order": 1' in html_content
    assert '"order": 2' in html_content
    assert "route-pin" in html_content
    assert '"color": "#e1693a"' in html_content  # day1配色


def test_generate_site_map_includes_driving_route_script(isolated_project, tmp_path, monkeypatch):
    """同一天有2个以上POI时，应发起AMap.Driving驾车路线查询并画连线，而不是只放图钉。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[
        RoutePoi(name="武侯祠"), RoutePoi(name="锦里"),
    ])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "AMap.Driving" in html_content
    assert "AMap.Polyline" in html_content


def test_generate_site_map_serializes_driving_requests_and_never_reports_failed_zero(
    isolated_project, tmp_path, monkeypatch
):
    """驾车查询应串行+重试；失败不能静默渲染成0.0km。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="青甘", pois=[
        RoutePoi(name="起点"), RoutePoi(name="终点"),
    ])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="青甘", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (100.0, 36.0)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "var queue = [];" in html_content
    assert "MAX_RETRIES = 2" in html_content
    assert "REQUEST_TIMEOUT_MS = 15000" in html_content
    assert "驾车路线未返回（直线约" in html_content
    assert "景点间，" in html_content


def test_generate_site_map_single_poi_day_skips_driving(isolated_project, tmp_path, monkeypatch):
    """只有1个POI的天没有相邻点可连线，不应该报错，也不该对这天发起驾车查询。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)  # 不应抛错
    html_content = output_path.read_text(encoding="utf-8")

    assert "id=\"map\"" in html_content
    assert "武侯祠" in html_content


def test_generate_site_map_includes_distance_legend(isolated_project, tmp_path, monkeypatch):
    """地图旁应有距离图例：先显示直线距离占位文案，驾车距离查询完成后由JS原地替换。
    降级分支（无坐标/无Key）不应该出现这个图例。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[
        RoutePoi(name="武侯祠"), RoutePoi(name="锦里"),
    ])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "map-legend" in html_content
    assert "直线距离" in html_content


def test_generate_site_map_legend_absent_in_fallback(isolated_project, tmp_path, monkeypatch):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", None)
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    # "map-legend"作为CSS类定义本身始终存在于共享样式表里，这里要断言的是
    # 降级路径不会渲染出对应的<div>元素，而不是整个页面里完全不出现这个词。
    assert '<div class="map-legend">' not in html_content


def test_generate_site_map_uses_set_fit_view(isolated_project, tmp_path, monkeypatch):
    """地图不应再用固定zoom/center，应该setFitView()自动取景包住所有点。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都", requirements={})

    monkeypatch.setattr(site_module, "AMAP_JS_API_KEY", "fake-js-key")
    monkeypatch.setattr(
        site_module, "get_cached_or_geocode", lambda topic, name, city="": (104.05, 30.64)
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "setFitView" in html_content
    assert "zoom: 12" not in html_content


def test_generate_site_forest_cover_shows_stats_from_route_plan(isolated_project, tmp_path):
    """封面(cover)统计数字应该是N天/M地/X处深度玩法，从route_plan.json算出。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                area="武侯区",
                pois=[
                    RoutePoi(name="武侯祠", brief="三国文化", needs_detail=True),
                    RoutePoi(name="锦里", brief="仿古商业街"),
                ],
            ),
            RouteDay(day=2, city="成都", pois=[RoutePoi(name="宽窄巷子", brief="老街")]),
        ]
    )
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_requirements(isolated_project, topic="成都三日行", requirements={})
    detail = PoiDetailFile(poi_name="武侯祠", day=1, routes=[])
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert 'class="cover"' in html_content
    assert '<h1 class="cover-title">成都三日行</h1>' in html_content
    assert "<b>2</b>天" in html_content
    assert "<b>3</b>地" in html_content
    assert "<b>1</b>处深度玩法" in html_content


def test_generate_site_forest_days_render_land_card_and_plain_poi(isolated_project, tmp_path):
    """needs_detail=true且真的生成了详情的POI应渲染成可点击的"land"地标卡（含深入
    理由+指向树木页的链接）；needs_detail=false的POI只渲染成普通.poi节点，不可点击。
    """
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                area="武侯区",
                notes="三国文化主题的一天",
                pois=[
                    RoutePoi(
                        name="武侯祠",
                        brief="三国文化爱好者必去",
                        needs_detail=True,
                        needs_detail_reason="景区内有多个板块，需要规划路线",
                    ),
                    RoutePoi(name="锦里", brief="仿古商业街，适合拍照"),
                ],
            )
        ]
    )
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    detail = PoiDetailFile(poi_name="武侯祠", day=1, routes=[])
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert f'<a class="land" href="#/tree/{quote("武侯祠")}">' in html_content
    assert "景区内有多个板块，需要规划路线" in html_content
    assert '<div class="poi"><h3>锦里</h3>' in html_content
    # 普通poi节点不应该被渲染成可点击的land地标卡
    assert f'href="#/tree/{quote("锦里")}"' not in html_content
    assert "三国文化主题的一天" in html_content
    # 天数长卷已经提供了进入树木页的入口，不应该再重复渲染旧版导航列表
    assert "🌳 重点景点导航" not in html_content


def test_generate_site_forest_needs_detail_without_generated_tree_falls_back_to_plain_poi(
    isolated_project, tmp_path
):
    """needs_detail=true但对应poi_details还没真的生成时（理论上不该出现），
    不应该渲染出一个点进去会404的死链接，降级为普通.poi节点。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(
        days=[
            RouteDay(
                day=1,
                city="成都",
                pois=[RoutePoi(name="待生成景点", brief="详情还没生成", needs_detail=True)],
            )
        ]
    )
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert f'href="#/tree/{quote("待生成景点")}"' not in html_content
    assert '<div class="poi"><h3>待生成景点</h3>' in html_content


def test_generate_site_forest_guide_section_keeps_full_final_guide_content(isolated_project, tmp_path):
    """天数长卷只是新增的可视化速览层，不能替代/裁剪final_guide.md本身——完整正文
    区块应该原样保留全部内容（哪怕route_plan已经存在，天数长卷不需要覆盖到的细节，
    比如美食/穿搭/预算说明，仍然只存在于final_guide.md里）。"""
    content_md = (
        "# 成都三日游\n\n"
        "## Day 1\n\n"
        "### 🍜 美食推荐\n\n"
        "推荐尝试甜水面，非常适合不吃辣的人。\n"
    )
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=content_md, meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert 'class="guide-content"' in html_content
    assert "📖 完整攻略正文" in html_content
    assert "甜水面" in html_content
    assert "非常适合不吃辣的人" in html_content


def test_generate_site_hides_technical_reference_appendix_from_public_html(isolated_project, tmp_path):
    """参考来源和POI转储保留在工作区，但不再出现在游客页面。"""
    content_md = (
        "# 青甘大环线\n\n"
        "## Day 1\n\n"
        "青海湖环湖，注意防晒。\n\n"
        "---\n\n"
        "## 📚 参考来源（程序生成，非模型自行撰写）\n\n"
        "| 平台 | doc_id | chunk_id | 原文片段 |\n"
        "|---|---|---|---|\n"
        "| douyin_asr | 青甘大环线-001 | 青甘大环线-001-c001 | 青海湖原文... |\n\n"
        "## 🌳 重点景点深度玩法\n\n"
        "这段技术转储不应出现在公开页面。\n"
    )
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=content_md, meta={})

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "青海湖环湖，注意防晒" in html_content
    assert "参考来源（程序生成，非模型自行撰写）" not in html_content
    assert "青甘大环线-001-c001" not in html_content
    assert "这段技术转储不应出现在公开页面" not in html_content
    assert "参考来源（程序生成，非模型自行撰写）" in store.read_workspace(isolated_project, "final_guide.md")


def test_public_render_helpers_hide_new_poi_then_reference_order():
    content_md = (
        "# 攻略\n\n## Day 1\n正文\n\n---\n\n"
        f"{POI_SECTION_HEADING}\nPOI转储\n\n---\n\n"
        f"{REFERENCE_SECTION_HEADING}\n引用表"
    )
    appendix_md = (
        "## 📦 行前必备清单\n证件\n\n"
        "## 🌳 重点景点深度玩法\nPOI转储\n\n"
        "## 📚 参考来源\n引用表"
    )

    assert site_module._strip_public_technical_appendix(content_md).endswith("正文")
    front_blocks, technical = site_module._split_appendix_blocks(appendix_md)
    assert [heading for heading, _block in front_blocks] == ["📦 行前必备清单"]
    assert "重点景点深度玩法" in technical
    assert "参考来源" in technical


# ---------------------------------------------------------------------------
# Day详情页信息架构重构（2026-08-16接入）：森林页天数长卷改为轻量入口，衣食住行
# 细节移到独立的Day详情页(#/day/N)，避雷内容改读outputs/pitfall.json，完整原文
# 降级为全局唯一的#/guide安全网。见_split_guide_by_day等纯函数单测（同目录下方）。
# ---------------------------------------------------------------------------


def test_generate_site_renders_day_pages_with_categorized_cards(isolated_project, tmp_path):
    content_md = (
        "# 成都三日游\n\n概览开场白\n\n"
        "## Day 1｜宽窄巷子·太古里\n\n"
        "**行程节奏**：建议9:00出发，12:00午餐。\n\n"
        "### 游玩\n\n宽窄巷子逛老成都街巷。\n\n"
        "### 美食\n\n甜水面非常适合不吃辣的人。\n\n"
        "### 穿搭\n\n舒适步行鞋即可。\n\n"
        "### 住宿\n\n市中心连锁酒店。\n\n"
        "## 🎒 必备清单\n\n带好雨伞。\n"
    )
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=content_md, meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", area="武侯区", pois=[RoutePoi(name="宽窄巷子", brief="老街")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert 'id="day-page-1"' in html_content
    assert 'class="view-day"' in html_content
    assert "甜水面" in html_content
    assert "舒适步行鞋即可" in html_content
    assert "市中心连锁酒店" in html_content
    assert "9:00" in html_content
    # 跨天内容（行程概览/必备清单）不应该出现在Day1详情页区块内部
    day1_start = html_content.index('id="day-page-1"')
    day1_end = html_content.index("</section>", day1_start)
    day1_html = html_content[day1_start:day1_end]
    assert "带好雨伞" not in day1_html
    assert "概览开场白" not in day1_html
    # 但完整原文安全网(#/guide)里应该原样保留全部内容，不因为拆分而丢失
    assert "带好雨伞" in html_content
    assert "概览开场白" in html_content
    assert 'id="view-guide"' in html_content


def test_generate_site_day_page_needs_detail_poi_uses_land_mini(isolated_project, tmp_path):
    content_md = "## Day 1｜青城山\n\n### 游玩\n\n青城山值得细逛。\n"
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=content_md, meta={})
    plan = RoutePlan(days=[RouteDay(
        day=1, city="成都",
        pois=[RoutePoi(name="青城山", brief="道教名山", needs_detail=True)],
    )])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    detail = PoiDetailFile(poi_name="青城山", day=1, routes=[])
    store.write_poi_detail(isolated_project, actor="poi_detail_agent", poi_detail=detail)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert f'class="land-mini" href="#/tree/{quote("青城山")}?from=day-1"' in html_content


def test_generate_site_day_page_reads_pitfall_warning(isolated_project, tmp_path):
    """避雷内容改读outputs/pitfall.json的结构化warning字段，不再从markdown正文抠字。"""
    content_md = "## Day 1｜青城山\n\n### 游玩\n\n青城山值得细逛。\n"
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=content_md, meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="青城山", brief="道教名山")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_pitfall_output_patch(
        isolated_project,
        actor="pitfall_agent",
        items=[{
            "field": "play", "day": 1,
            "target_recommendation": "推荐前山",
            "warning": "旺季排队久，建议早去",
        }],
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "warn-chip" in html_content
    assert "旺季排队久，建议早去" in html_content


def test_generate_site_day_page_misc_section_not_dropped(isolated_project, tmp_path):
    """匹配不到游玩/美食/穿搭/住宿的杂项子标题（带娃提示等）要兜底进"补充提示"卡，
    不能静默丢内容。"""
    content_md = "## Day 1｜海边\n\n### 带娃提示\n\n沙滩需要看护好孩子。\n"
    store.write_final_guide(isolated_project, actor="integration_agent", content_md=content_md, meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="海口", pois=[])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "补充提示" in html_content
    assert "带娃提示" in html_content
    assert "沙滩需要看护好孩子" in html_content


def test_generate_site_forest_day_links_to_day_page(isolated_project, tmp_path):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="## Day 1｜成都\n\n### 游玩\n\n内容\n", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="武侯祠")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert '<a class="day-link" href="#/day/1">查看完整这天攻略 →</a>' in html_content


def test_generate_site_no_route_plan_skips_day_pages(isolated_project, tmp_path):
    """route_plan缺失时，天数长卷本身不渲染，Day详情页也不应该生成（没有对应入口链过来）。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="## Day 1｜成都\n\n内容\n", meta={})

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert 'class="view-day"' not in html_content


# ---------------------------------------------------------------------------
# 拆分/分类纯函数单测：_split_guide_by_day / _split_day_block_into_sections /
# _classify_section_heading / _extract_timeline
# ---------------------------------------------------------------------------


def test_split_guide_by_day_with_intro_and_appendix():
    md = (
        "# 标题\n\n一些开场白\n\n"
        "## Day 1｜西宁 → 青海湖\n\nday1内容\n\n"
        "## Day 2｜青海湖 → 茶卡\n\nday2内容\n\n"
        "## Day 3｜茶卡 → 敦煌\n\nday3内容\n\n"
        "## 📦 行前必备清单\n\n清单内容\n"
    )
    intro, days, appendix = site_module._split_guide_by_day(md, 3)
    assert "开场白" in intro
    assert len(days) == 3
    assert "day1内容" in days[0] and "Day 1" in days[0]
    assert "day2内容" in days[1]
    assert "day3内容" in days[2]
    assert "清单内容" in appendix


def test_split_guide_by_day_no_intro_no_appendix():
    md = "## 第一天：抵达\n\n内容1\n\n## 第二天：深度游\n\n内容2\n"
    intro, days, appendix = site_module._split_guide_by_day(md, 2)
    assert intro == ""
    assert appendix == ""
    assert len(days) == 2
    assert "内容1" in days[0]
    assert "内容2" in days[1]


def test_split_guide_by_day_emoji_and_mixed_heading_styles():
    """真实样本里标题文案格式差异很大（"Day N"/"第N天"/带emoji），day-pattern
    正则要能兼容识别，不只认最简单的格式。"""
    md = (
        "## 📋 行程概览\n\n概览\n\n"
        "## 🗓️ Day 1：低山区慢行\n\n内容1\n\n"
        "## 📅 第二天：核心朝圣日\n\n内容2\n\n"
        "## ⚠️ 重要提示汇总\n\n提示\n"
    )
    intro, days, appendix = site_module._split_guide_by_day(md, 2)
    assert "概览" in intro
    assert len(days) == 2
    assert "内容1" in days[0]
    assert "内容2" in days[1]
    assert "提示" in appendix


def test_split_guide_by_day_fallback_when_pattern_not_found():
    """标题完全不含数字/day/天字样时，day-pattern正则找不到连续命中区间，必须
    降级成"最后num_days个区块当作天"，不能抛异常、也不能返回长度不对的day_blocks。
    """
    md = "## 引子\n\n引子内容\n\n## 甲\n\n内容甲\n\n## 乙\n\n内容乙\n"
    intro, days, appendix = site_module._split_guide_by_day(md, 2)
    assert len(days) == 2
    assert "内容甲" in days[0]
    assert "内容乙" in days[1]
    assert "引子内容" in intro


def test_split_guide_by_day_fewer_blocks_than_num_days_pads_empty():
    """区块总数比num_days还少的极端情况：不报错，用空字符串补齐，长度恒等于
    num_days（调用方zip(route_plan.days, day_blocks)依赖这个长度保证）。"""
    md = "## Day 1｜出发\n\n内容1\n"
    intro, days, appendix = site_module._split_guide_by_day(md, 3)
    assert len(days) == 3
    assert days[0] != ""
    assert days[1] == ""
    assert days[2] == ""


def test_split_guide_by_day_empty_input():
    intro, days, appendix = site_module._split_guide_by_day("", 3)
    assert intro == "" and days == [] and appendix == ""


def test_split_guide_by_day_no_headings_at_all():
    """final_guide.md完全没有`## `标题（理论不该出现，但要有兜底）：全部当intro，
    day_blocks仍padding到num_days长度，不崩溃。"""
    md = "只有一段纯文本，没有任何二级标题。"
    intro, days, appendix = site_module._split_guide_by_day(md, 3)
    assert intro == md
    assert days == ["", "", ""]
    assert appendix == ""


def test_split_guide_by_day_zero_days():
    intro, days, appendix = site_module._split_guide_by_day("## Day 1\n\n内容\n", 0)
    assert days == []


def test_split_day_block_into_sections_with_intro():
    block = (
        "## Day 1｜西宁 → 青海湖\n\n"
        "**行程节奏**：建议7:30出发，10:00抵达。\n\n"
        "### 游玩\n\n游玩内容\n\n"
        "### 美食\n\n美食内容\n"
    )
    intro, sections = site_module._split_day_block_into_sections(block)
    assert "行程节奏" in intro
    assert len(sections) == 2
    assert sections[0] == ("游玩", "游玩内容")
    assert sections[1] == ("美食", "美食内容")
    # 子标题本身不该残留在section正文里（避免和卡片自己的<h2>标题重复）
    assert "### 游玩" not in sections[0][1]


def test_split_day_block_into_sections_no_intro_paragraph():
    """真实样本里有的场景（haikou/emeishan）Day标题后直接就是###子标题，没有
    概述段落——day_intro_text应该是空字符串，不是异常。"""
    block = "## 📅 第一天：骑楼老街\n\n### 行程时间线\n\n时间线内容\n"
    intro, sections = site_module._split_day_block_into_sections(block)
    assert intro == ""
    assert sections == [("行程时间线", "时间线内容")]


def test_split_day_block_into_sections_no_subheadings():
    block = "## Day 1｜出发\n\n只有一段话，没有子标题。\n"
    intro, sections = site_module._split_day_block_into_sections(block)
    assert "只有一段话" in intro
    assert sections == []


def test_classify_section_heading_standard_categories():
    assert site_module._classify_section_heading("游玩") == "游玩"
    assert site_module._classify_section_heading("🍽️ 美食建议（Day 1）") == "美食"
    assert site_module._classify_section_heading("🍜 美食建议") == "美食"
    assert site_module._classify_section_heading("👗 穿搭建议") == "穿搭"
    assert site_module._classify_section_heading("🏨 住宿建议") == "住宿"


def test_classify_section_heading_route_time_variants_map_to_play():
    assert site_module._classify_section_heading("行程时间线") == "游玩"
    assert site_module._classify_section_heading("路线与时间安排") == "游玩"
    assert site_module._classify_section_heading("具体说明") == "游玩"
    assert site_module._classify_section_heading("🎯 游玩细节") == "游玩"


def test_classify_section_heading_misc_falls_to_other():
    assert site_module._classify_section_heading("👶 带娃提示") == "其他"
    assert site_module._classify_section_heading("⛈️ 天气备选") == "其他"


def test_extract_timeline_two_or_more_clock_times():
    text = "建议7:30出发，10:00抵达景区，14:00返程。"
    timeline = site_module._extract_timeline(text)
    assert timeline is not None
    assert len(timeline) == 3
    assert timeline[0]["time"] == "7:30"
    assert "出发" in timeline[0]["text"]
    assert timeline[1]["time"] == "10:00"


def test_extract_timeline_single_clock_time_falls_back_to_none():
    text = "建议7:30前上G6/G109，全程约500km。"
    assert site_module._extract_timeline(text) is None


def test_extract_timeline_no_clock_time_returns_none():
    text = "全程约500km，是最赶的一天之一。"
    assert site_module._extract_timeline(text) is None


def test_extract_timeline_fullwidth_colon():
    text = "早上7：30出发，晚上18：00到达。"
    timeline = site_module._extract_timeline(text)
    assert timeline is not None
    assert len(timeline) == 2


def test_generate_site_day_banner_shows_when_route_day_force_finalized(isolated_project, tmp_path):
    """docs/ISSUES.md第125条：某天的RouteDay.citation_status为force_finalized时，
    天数长卷的这一天卡片应出现可见提示；未被标记的天不应出现，避免视觉噪音。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(
        days=[
            RouteDay(day=1, city="西宁", citation_status="force_finalized"),
            RouteDay(day=2, city="敦煌"),
        ]
    )
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert html_content.count('class="banner day-banner"') == 1
    assert "保留历史核验未完成状态" in html_content
    assert "不代表已确认错误" in html_content


def test_generate_site_day_banner_shows_when_lifestyle_item_force_finalized(isolated_project, tmp_path):
    """route层本身passed，但当天有lifestyle item被force_finalized时，同一天的卡片
    也应出现提示——core信息来自route_plan/lifestyle/pitfall任一层，不只看route自己。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="西宁"), RouteDay(day=2, city="敦煌")])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_agent_output_patch(
        isolated_project, "lifestyle_agent",
        [{"field": "food", "day": 1, "recommendation": "尕面片"}],
    )
    store.mark_lifestyle_items_force_finalized(isolated_project, [("food", 1)])

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert html_content.count('class="banner day-banner"') == 1


def test_generate_site_no_day_banner_when_all_passed(isolated_project, tmp_path):
    """没有任何force_finalized内容时，天数长卷不应渲染任何未核验提示。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="西宁"), RouteDay(day=2, city="敦煌")])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert 'class="banner day-banner"' not in html_content


# ---------------------------------------------------------------------------
# 用户备注/annotation layer（docs/ARCHITECTURE.md第21节）
# ---------------------------------------------------------------------------


def test_generate_site_renders_trip_level_user_note_in_independent_section(isolated_project, tmp_path):
    """不绑day/poi的行程级备注渲染进独立的"行程备注"卡片，标注"📝 用户备注"。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    store.write_user_note(isolated_project, "admin_user", "整体节奏偏紧凑")

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "📝 用户备注" in html_content
    assert "未经核验" not in html_content  # 不再标"未经核验"，用户自己记的备注本来就不用核验
    assert "整体节奏偏紧凑" in html_content
    assert "行程备注" in html_content


def test_generate_site_renders_day_level_user_note_in_forest_day_card(isolated_project, tmp_path):
    """绑day的备注渲染在天数长卷对应天的卡片区块内。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都"), RouteDay(day=2, city="乐山")])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_user_note(isolated_project, "admin_user", "这天记得带伞", day=1)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "这天记得带伞" in html_content
    # 不应该出现在独立的"行程备注"区块——绑了day就该出现在天数长卷内
    assert html_content.index("这天记得带伞") < html_content.index("🗺️ 路线地图")


def test_generate_site_renders_poi_level_user_note_in_day_page_and_tree_notes_payload(
    isolated_project, tmp_path
):
    """绑POI的备注：渲染进Day详情页对应POI条目下方 + 打包进树木页的TREE_NOTES JSON
    payload（客户端渲染进树页），见_render_day_poi_item/renderTreeUserNotes。"""
    store.write_final_guide(
        isolated_project, actor="integration_agent", content_md="# 攻略正文\n\n## Day 1\n\n### 游玩\n\n内容", meta={},
    )
    plan = RoutePlan(
        days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="宽窄巷子", brief="老街")])]
    )
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_user_note(isolated_project, "admin_user", "这家火锅鱼要提前订", day=1, poi_name="宽窄巷子")

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "这家火锅鱼要提前订" in html_content
    assert '"宽窄巷子": [{"note_id"' in html_content or "宽窄巷子" in html_content
    assert "TREE_NOTES" in html_content


def test_generate_site_orphan_poi_note_falls_back_to_day_level(isolated_project, tmp_path):
    """绑定的POI在当前route_plan里已经找不到了（比如edit模式重写后景点被换掉），
    退到天级展示，文本不丢，见docs/ARCHITECTURE.md第21.2节"孤儿备注"一节。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    plan = RoutePlan(days=[RouteDay(day=1, city="成都", pois=[RoutePoi(name="新景点", brief="")])])
    store.write_route_plan(isolated_project, actor="route_agent", route_plan=plan)
    store.write_user_note(isolated_project, "admin_user", "旧景点的备注还在", day=1, poi_name="已被替换的景点")

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "旧景点的备注还在" in html_content


def test_generate_site_deleted_user_note_not_rendered(isolated_project, tmp_path):
    """软删的备注不应该出现在生成的site.html里。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    note = store.write_user_note(isolated_project, "admin_user", "应该被删掉的备注")
    store.delete_user_note(isolated_project, "admin_user", note.note_id)

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert "应该被删掉的备注" not in html_content


def test_generate_site_includes_note_form_and_download_link(isolated_project, tmp_path):
    """网页内嵌表单+下载副本链接应该始终出现在生成的site.html里，不依赖有没有备注。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert 'id="note-form"' in html_content
    assert "/user_notes" in html_content
    assert f"/api/projects/{quote(isolated_project)}/site/download" in html_content
    assert "⬇ 下载本页副本" in html_content


def test_generate_site_no_user_notes_omits_notes_badge(isolated_project, tmp_path):
    """没有任何用户备注时，服务端渲染出的森林页/Day页里不应该出现"用户备注"标签div——
    只应该剩下_TREE_SCRIPT里那一份JS模板字符串（客户端按数据动态渲染树页用，见
    renderTreeUserNotes），不应该额外出现在服务端拼好的静态HTML部分。"""
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert html_content.count('<div class="user-notes-badge">') == 1


def test_generate_site_uses_human_review_effective_status(isolated_project, tmp_path):
    store.write_final_guide(isolated_project, actor="integration_agent", content_md="# 攻略正文", meta={})
    store.write_route_plan(
        isolated_project,
        actor="route_agent",
        route_plan=RoutePlan(days=[RouteDay(day=1, city="成都", citation_status="force_finalized")]),
    )
    review_store.sync_state_findings(isolated_project)
    finding = next(item for item in review_store.read_findings(isolated_project) if item.canonical_key == "route_day:1")
    review_store.record_verification(
        isolated_project,
        finding.finding_id,
        verification_status="passed_with_review",
        verification_method="test",
        disposition="resolved",
        actor_type="human",
        actor_id="tester",
    )

    output_path = tmp_path / "site.html"
    generate_site(isolated_project, output_path)
    html_content = output_path.read_text(encoding="utf-8")

    assert 'class="banner day-banner review-passed"' in html_content
    assert "人工复核通过：行程安排" in html_content
    assert 'class="banner day-banner"' not in html_content
