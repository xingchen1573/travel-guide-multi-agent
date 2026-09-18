"""生成陪伴阶段之外的静态展示网站：读 final_guide.md + outputs/poi_details/*.json，
拼接出一份HTML（森林页 + 树木页），图片作为同目录site_assets中的完整原图单独提供；
纯前端渲染/hash路由，不请求后端、
不调用LLM——参照 `scripts.delivery.run_annotate` 的"纯程序拼接、不调用LLM"这个实现风格，见
docs/ARCHITECTURE.md 第13节"呈现层"。

image_prompt字段只是预留展示位（原样打印文本占位），不接生图API。

POI真实照片（2026-08-17接入，落地过程见docs/ISSUES.md）：`_attach_poi_overview_photos`
在生成阶段（不进agent工具链、不占LLM token预算）给每个PoiDetailFile（一个树木页/
一个深入详情的景点）调用`src/tools/poi_photo.py`的`get_cached_or_poi_overview_photo`，
用poi_name本身（如"张掖·七彩丹霞景区"）查高德POI搜索接口photos字段，成功时给这个
detail加`overview_photo_url`（相对静态资源路径），前端树木页英雄区渲染真实照片，
查不到/未配key时保留原有英雄区样式（不阻塞展示，只是没有配图）。

用户手动上传照片（管理后台"POI图片"页面，见src/api/routers/poi_photos.py）优先于
高德自动查图：`PoiDetailFile.uploaded_overview_photo_path`/`RouteSegment.uploaded_photo_path`
非空时直接复制这个project workspace下的完整原图到site_assets，不再调用高德查图。途经点
(segment)级别的照片只有用户上传这一条路径——自动查图从未支持途经点粒度（见下方
"配图粒度"说明），这是用户上传新增的展示位置，此前途经点卡片一直是纯CSS占位框。

配图粒度是"整个POI一张图"，不是"每个途经点(segment)一张图"——这是真实E2E跑测
（成都+青甘大环线两轮）验证后收敛出的方案，中途验证过四种途经点级别方案均在真实
数据上暴露图文不符（如把张掖七彩丹霞的途经点错配成保定/东莞/宁夏的无关地点），
根因是长线自驾/多城市行程的city无法可靠限定到"景点内部子设施"级别精度，city范围
越粗、误配风险越大；完整景区名作为搜索词命中率和准确率都远高于碎片化的途经点短语。
详细演变过程和真实案例见src/tools/poi_photo.py模块docstring。真实图片优先于AI生图
（保持项目"素材真实性"气质）；图片全局缓存（跨project复用，同driving_cache.json
模式），不做压缩/缩放（高德photos本身已是约500px/30-140KB的小图，实测确认）。

森林页真实地图（2026-07-24接入，呈现层P2第一项）：读route_plan.json里各天的POI列表，
逐个调用src/tools/geocode.py的get_cached_or_geocode()查坐标（全局缓存，重复调用同名
POI不重复消耗API额度），有坐标数据时嵌入高德JS API渲染地图+marker；未配置
AMAP_GEOCODE_API_KEY/AMAP_JS_API_KEY或坐标查询全部失败时，降级为纯文字POI列表，
不阻塞网站其余部分生成——跟src/tools/amap_tools.py/web_search_tools.py同一种
"没有真实key时优雅降级"模式。地理编码调用的是确定性REST API，不是LLM，符合本文件
"纯静态生成、不调用LLM"的既定原则不变。

树木页视觉升级（2026-07-24接入，呈现层P2第二项）：移植design/poi_detail_page/
preview_v3_真实数据_成都三日行.html的"旅程长卷"设计稿——只移植树木视图部分（POI
深度详情：英雄区/路线tab/吸顶进度条/站点卡片流/打卡），森林视图保持现状（文本+地图），
这是刻意的范围切分，见docs/ISSUES.md第40条。页面结构从"单页平铺锚点跳转"改为
"#view-forest / #view-tree 两个容器 + hash路由(#/tree/<name>) + 幕布转场动画"，
只有一个#view-tree容器，JS按hash动态换填内容，不需要给每个POI各生成一份独立实例。

森林页视觉迁移（2026-07-26接入，补上第40条当时刻意搁置的另一半）：把设计稿的
封面(cover)+天数长卷(day-num/POI节点/深度玩法地标卡)+页脚也迁移过来。天数长卷用
route_plan.json的结构化数据渲染（day/city/area/notes/pois，schema保证存在），不去
正则解析final_guide.md本身——那是模型自由生成的文本，标题格式（是不是"## Day N"、
有没有空格）并没有prompt层面强制约束，靠猜格式解析属于不可靠的假设，历史上这类
"猜LLM输出格式"的坑吃过很多次。因此final_guide.md面向游客的正文保留在天数长卷
下方单独一块（"📖 完整攻略正文"）；森林页和每日详情页不展开技术性附录（参考来源/POI转储），
但用户主动进入"完整攻略原文"视图时会看到final_guide.md文末的参考来源。页面只是多了一层
"天数长卷"作为可视化速览+深度玩法入口。
设计稿里每日还有一个
"checklist"（行前提醒）子模块，但schema/生成流程里没有对应的按天结构化数据（行前
清单只有全局一份，在完整正文里），这里刻意不编造数据去凑、跳过这个子模块。
route_plan.json不存在时（如中途查看/历史遗留数据），天数长卷整体不渲染，退回旧版
"🌳重点景点导航"列表作为进入树木页的唯一入口，见docs/ISSUES.md第70条。

Day详情页（2026-08-16接入，森林页/Day页信息架构重构）：上面第26-29行记录的"不正则解析
final_guide.md"的顾虑依然成立，但天数长卷的碎片摘要和完整正文的彻底割裂本身也是真实的
可用性问题——用户要么看摘要要么翻到底部整块长文，两者不互相指涉。这次改造做的是有安全网
的解析，不是裸解析：`_split_guide_by_day`按`^## `拆区块、用宽松的day-pattern正则定位
连续N个（N=route_plan天数）区块当作"天"，正则找不到规整匹配时降级成"最后N块算天"而不是
抛异常；不管拆得准不准，完整原文永远在一个独立的`#/guide`路由里原样可查——这是解析出错时
的兜底，不是"这次解析器一定不会错"的承诺。避雷内容改读`outputs/pitfall.json`（已有
PitfallOutput模型+store.read_pitfall_output，之前这个文件从未被本脚本读取过），这部分
是结构化数据、不存在"猜格式"的问题。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import math
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import markdown

from src.config import AMAP_JS_API_KEY, AMAP_JS_SECURITY_CODE
from src.tools.geocode import get_cached_or_geocode
from src.tools.poi_photo import get_cached_or_poi_overview_photo
from src.review import store as review_store
from src.workspace import store
from src.workspace.final_guide_sections import contract_body_markdown
from src.workspace.schema import PitfallOutput, RouteDay


def _load_poi_details(project_id: str) -> list[dict]:
    """读取当前有效树木；manifest存在时由store统一限定活动集合。"""
    return [detail.model_dump(mode="json") for detail in store.list_poi_details(project_id)]


def _load_effective_review_projection(project_id: str) -> dict[str, dict]:
    """读取复核覆盖层；原始 citation_status 仍保留在业务 JSON 中。"""
    review_store.sync_state_findings(project_id)
    state = review_store.validate_project_state(project_id)
    return {
        str(item.get("canonical_key") or ""): item
        for item in state.get("effective_objects", [])
        if item.get("canonical_key")
    }


def _load_user_notes(project_id: str) -> list[dict]:
    """读取未删的用户备注（可见标注层，见docs/ARCHITECTURE.md第21节）。不是正文核验
    产物，直接复用store.list_user_notes，不额外做核验/过滤。"""
    return [note.model_dump(mode="json") for note in store.list_user_notes(project_id)]


def _classify_user_notes(
    notes: list[dict], route_plan
) -> tuple[dict[tuple[int, str], list[dict]], dict[int, list[dict]], list[dict]]:
    """按day/poi_name两级可选绑定分类成三桶：POI级((day,poi_name)->notes)/天级(day->notes)/
    行程级(含孤儿，list)。孤儿备注（绑定的POI已经不在当前route_plan里，比如edit模式重写
    后景点被换掉了）退到天级展示，文本不丢；如果连day都没有就退到行程级独立备注区，
    见docs/ARCHITECTURE.md第21.2节"孤儿备注"一节。
    """
    valid_poi_days: set[tuple[int, str]] = set()
    if route_plan:
        for day in route_plan.days:
            for poi in day.pois:
                valid_poi_days.add((day.day, poi.name))

    by_poi: dict[tuple[int, str], list[dict]] = {}
    by_day: dict[int, list[dict]] = {}
    trip_level: list[dict] = []

    for note in notes:
        day = note.get("day")
        poi_name = note.get("poi_name") or ""
        if day is not None and poi_name:
            if (day, poi_name) in valid_poi_days:
                by_poi.setdefault((day, poi_name), []).append(note)
                continue
            poi_name = ""  # 孤儿：退级到天级，不丢文本
        if day is not None:
            by_day.setdefault(day, []).append(note)
        else:
            trip_level.append(note)

    return by_poi, by_day, trip_level


def _render_user_notes_block(notes: list[dict]) -> str:
    """渲染一组用户备注，统一标注"📝 用户备注"——备注是用户自己写的可见标注层，
    不进citation核验、不是正文（见docs/ARCHITECTURE.md第21.1节），不需要再标"未经核验"
    （用户自己记的东西本来就不用核验，标"未经核验"反而让用户以为系统对它做了某种背书前的声明）。
    notes为空时返回空字符串。"""
    if not notes:
        return ""
    items = "".join(
        f'<div class="user-note-item"><p>{html.escape(n.get("note_text", ""))}</p>'
        f'<span class="user-note-meta">{html.escape((n.get("created_at") or "")[:16].replace("T", " "))}</span></div>'
        for n in notes
    )
    return f'<div class="user-notes-block"><div class="user-notes-badge">📝 用户备注</div>{items}</div>'


def _render_trip_level_notes_section(trip_notes: list[dict]) -> str:
    """行程级/孤儿备注的独立区块，放在森林页"行程概览/必备清单/预算"卡片之后、天数长卷之前
    （见docs/ARCHITECTURE.md第21.4节，2026-08-21从"天数长卷之后"上移到更显眼的位置）。
    trip_notes为空时返回空字符串。"""
    if not trip_notes:
        return ""
    block_html = _render_user_notes_block(trip_notes)
    return f'<section class="forest-extra"><div class="card"><h2><span class="ic">📝</span>行程备注</h2>{block_html}</div></section>'


_TABLE_PATTERN = re.compile(r"<table>.*?</table>", re.S)
_LIST_MARKER_PATTERN = re.compile(r"^[ \t]*([-*+][ \t]|\d+\.[ \t])")


def _ensure_blank_line_before_lists(text: str) -> str:
    """LLM生成的final_guide.md里，加粗小标题后紧跟列表却没有空行分隔（如
    "**玩法细节**\\n- xxx"），Python markdown库要求列表前必须有空行才能识别为
    <ul><li>，没空行时整段被解析成一个<p>纯文本，列表项/加粗全混在一起读不出
    层次——这是Day详情页"文字墙"问题的真实根因（5个project的final_guide.md
    共命中24次该模式）。这里在渲染前统一补空行兜底，比改agent prompt可靠
    （能覆盖历史数据和新数据，且prompt层面无法保证LLM一定遵守空行规范）。
    """
    lines = text.split("\n")
    out = []
    for line in lines:
        if _LIST_MARKER_PATTERN.match(line) and out and out[-1].strip() != "" and not _LIST_MARKER_PATTERN.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def _render_md(text: str) -> str:
    """统一的markdown渲染入口，所有卡片/附录正文都走这里，不要各自零散调用
    markdown.markdown()。真实数据里引用清单的doc_id/chunk_id列是无空格标识符
    （web核实的合成id如web-gansumuseum-2026尤其长）。早期用横向滚动容器兜底，
    但用户反馈doc_id长id导致整张表横向溢出要滚动看，可读性差——现改成在cell内
    overflow-wrap:anywhere优先在连字符/CJK边界断行（见.table-scroll CSS注释），
    不再横向溢出。这个<div class="table-scroll">包装层仍保留，min-width:100%
    防止窄内容留难看空白。
    """
    text = _ensure_blank_line_before_lists(text)
    rendered = markdown.markdown(text, extensions=["tables", "sane_lists"])
    return _TABLE_PATTERN.sub(lambda m: f'<div class="table-scroll">{m.group(0)}</div>', rendered)


_DAY_HEADING_PATTERN = re.compile(r"(day\s*\d+|第\s*[一二三四五六七八九十百\d]+\s*天)", re.IGNORECASE)
_H2_SPLIT_PATTERN = re.compile(r"(?m)^## +(.+)$")
_H3_SPLIT_PATTERN = re.compile(r"(?m)^### +(.+)$")
_CLOCK_TIME_PATTERN = re.compile(r"\d{1,2}[:：]\d{2}")


def _split_guide_by_day(final_guide_md: str, num_days: int) -> tuple[str, list[str], str]:
    """把final_guide.md按`^## `二级标题拆区块，定位出哪些区块是"某一天"，返回
    (intro_md, day_blocks, appendix_md)。day_blocks长度恒等于num_days（num_days<=0
    或没有任何区块时全部返回空）。

    区块数量总是>=num_days，天数区块严格连续出现，首尾可能各多出0-N个跨天区块
    （"行程概览"/"必备清单"这类）。优先用宽松的day-pattern正则在标题里找连续命中
    区间；正则命中数对不上num_days时（标题完全不含数字/day/天字样的边界情况），
    降级为"最后num_days个区块当作天"，不抛异常、不丢内容——出错时靠#/guide全文
    兜底，不是靠这个函数100%解析正确。
    """
    if not final_guide_md.strip():
        return "", [], ""
    if num_days <= 0:
        # 没有route_plan/没有天数可分（比如中途查看，route_plan还没生成），但
        # final_guide.md本身可能已经有内容——不能因为分不了天就把内容整个丢掉，
        # 全部归进intro，靠_render_forest_extras在森林首页展示。
        return final_guide_md.strip(), [], ""

    matches = list(_H2_SPLIT_PATTERN.finditer(final_guide_md))
    if not matches:
        # 完全没有`## `标题（理论上不该出现，真实final_guide.md样本里天数区块必有），
        # 仍要保证day_blocks长度恒等于num_days，供_render_all_day_pages的zip()安全配对。
        return final_guide_md, [""] * num_days, ""

    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(final_guide_md)
        blocks.append((m.group(1), final_guide_md[start:end]))

    if len(blocks) <= num_days:
        day_start = 0
    else:
        hit_idxs = [i for i, (heading, _) in enumerate(blocks) if _DAY_HEADING_PATTERN.search(heading)]
        day_start = None
        for start in range(len(blocks) - num_days + 1):
            window = range(start, start + num_days)
            if all(i in hit_idxs for i in window):
                day_start = start
                break
        if day_start is None:
            day_start = len(blocks) - num_days  # 降级：最后num_days个区块当作天

    day_end = day_start + num_days
    intro_md = (final_guide_md[: matches[0].start()] + "".join(b for _, b in blocks[:day_start])).strip()

    day_blocks = [b for _, b in blocks[day_start:day_end]]
    while len(day_blocks) < num_days:  # 区块总数比num_days还少的极端情况，补空天不报错
        day_blocks.append("")
    appendix_md = "".join(b for _, b in blocks[day_end:]).strip()

    return intro_md, day_blocks, appendix_md


def _split_day_block_into_sections(day_block_md: str) -> tuple[str, list[tuple[str, str]]]:
    """输入单天的markdown区块（含`## Day N｜...`标题行），去掉标题行，按`### `
    再拆一层，返回(day_intro_text, [(heading, section_md), ...])——day_intro_text
    是标题行和第一个`###`之间的文字（通常是"行程节奏"这类概述段）。
    """
    body = _H2_SPLIT_PATTERN.sub("", day_block_md, count=1).strip()
    sub_matches = list(_H3_SPLIT_PATTERN.finditer(body))
    if not sub_matches:
        return body, []

    day_intro_text = body[: sub_matches[0].start()].strip()
    sections = []
    for i, m in enumerate(sub_matches):
        end = sub_matches[i + 1].start() if i + 1 < len(sub_matches) else len(body)
        sections.append((m.group(1).strip(), body[m.end():end].strip()))
    return day_intro_text, sections


def _classify_section_heading(heading: str) -> str:
    """子标题关键词子串匹配分类，匹配不到归"其他"（由调用方兜底进"补充提示"卡，
    不丢内容）。"""
    if "美食" in heading:
        return "美食"
    if "穿搭" in heading:
        return "穿搭"
    if "住宿" in heading:
        return "住宿"
    if any(k in heading for k in ("游玩", "具体说明", "时间", "路线", "行程")):
        return "游玩"
    return "其他"


_MARKDOWN_TABLE_ROW_PATTERN = re.compile(r"(?m)^\s*\|[\s:|-]+\|\s*$")


def _extract_timeline(day_intro_text: str) -> Optional[list[dict]]:
    """正则找`HH:MM`模式的时间点，命中>=2处就按命中位置切分文本拼成时间轴条目；
    命中不足2处返回None，调用方据此降级为普通段落展示——不同场景/天数写法差异大，
    这是"尽力而为"的增强，不保证识别所有写法。

    day_intro_text本身是markdown表格时（真实样本里出现过"| 时间 | 地点 | 安排 |"
    这种写法），不做逐字符切分——按纯文本位置切表格行会把"|"分隔符和相邻单元格内容
    切得七零八落，产出的时间轴条目里会混进破碎的表格语法。这种情况直接返回None，
    交给调用方走markdown表格渲染（真正的<table>），而不是伪造一个不成立的时间轴。
    """
    if _MARKDOWN_TABLE_ROW_PATTERN.search(day_intro_text):
        return None
    matches = list(_CLOCK_TIME_PATTERN.finditer(day_intro_text))
    if len(matches) < 2:
        return None

    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(day_intro_text)
        entries.append({"time": m.group(0), "text": day_intro_text[m.end():end].strip(" 　，,、-—→")})
    return entries


def _render_forest_cover(trip_title: str, route_plan) -> str:
    """封面(cover)：行程名+统计数字(N天/M地/X处深度玩法)，移植自design v3森林视图。
    统计数字从route_plan.json算出；route_plan不存在时全部按0展示（不阻塞封面渲染）。
    """
    if route_plan and route_plan.days:
        total_days = len(route_plan.days)
        total_pois = sum(len(day.pois) for day in route_plan.days)
        total_trees = sum(1 for day in route_plan.days for poi in day.pois if poi.needs_detail)
    else:
        total_days = total_pois = total_trees = 0
    title_escaped = html.escape(trip_title)
    seal_char = html.escape(trip_title[0]) if trip_title else "旅"
    return f"""<header class="cover">
  <div class="cover-top">
    <span class="cover-kicker">行 程 总 览</span>
    <span class="cover-seal" aria-hidden="true">{seal_char}</span>
  </div>
  <div class="cover-mid">
    <h1 class="cover-title">{title_escaped}</h1>
    <span class="cover-vertical" aria-hidden="true">{title_escaped}</span>
  </div>
  <div class="cover-bottom">
    <div class="cover-stats">
      <span><b>{total_days}</b>天</span><span><b>{total_pois}</b>地</span><span><b>{total_trees}</b>处深度玩法</span>
    </div>
    <div class="cover-legend">
      <span><i class="lg-dot"></i>顺游点</span>
      <span><i class="lg-dia"></i>深度玩法 · 可进入</span>
    </div>
    <div class="cover-scroll" aria-hidden="true"><span class="cs-line"></span>向下展开长卷</div>
  </div>
</header>"""


def _render_forest_overview(route_plan, has_poster: bool = False) -> str:
    """真正的"总览"：每天去哪里/大致路线是什么的一句话清单，直接从route_plan.json
    的city/area/pois拼出来——这些是结构化数据，只要route_plan存在就一定有，不依赖
    final_guide.md自由文本里有没有写出一段像样的行程概览（真实样本里intro_md常常
    只是标题+免责声明，没有逐天路线）。天数长卷本身信息量大（POI brief/地标卡），
    翻到具体某一天之前看不到"整趟大概怎么走"，这张总览就是补这个缺口，位置放在
    天数长卷之前。route_plan不存在时返回空字符串。
    """
    if not route_plan or not route_plan.days:
        return ""
    rows = []
    for day in route_plan.days:
        title = " · ".join(filter(None, [day.city, day.area]))
        poi_chain = " → ".join(poi.name for poi in day.pois)
        poi_html = f'<span class="ov-pois">{html.escape(poi_chain)}</span>' if poi_chain else ""
        rows.append(f"""<a class="overview-row" href="#/day/{day.day}">
  <span class="ov-day">Day {day.day:02d}</span>
  <span class="ov-body"><b>{html.escape(title)}</b>{poi_html}</span>
  <span class="ov-arrow" aria-hidden="true">→</span>
</a>""")
    action = (
        f'<button class="poster-action" data-poster="overall">查看{len(route_plan.days)}天总览海报</button>'
        if has_poster else ""
    )
    return f"""<section class="forest-overview">
  <div class="section-heading-row"><h2 class="guide-heading">🗺️ 行程总览</h2>{action}</div>
  <div class="overview-list">{"".join(rows)}</div>
</section>"""


def _render_forest_days(
    route_plan,
    tree_index: dict,
    flagged_days: Optional[set[int]] = None,
    flagged_detail: Optional[dict[int, list[str]]] = None,
    reviewed_detail: Optional[dict[int, list[str]]] = None,
    notes_by_day: Optional[dict[int, list[dict]]] = None,
) -> str:
    """天数长卷：每天一个巨型日期数字+城市/区域/当日说明+POI节点列表。
    needs_detail=true的POI渲染成可点击的"land"地标卡（链到树木详情页），其余渲染成
    普通.poi节点。是否真的可点击额外用tree_index（真实生成过详情的POI集合）交叉
    核对——needs_detail=true但详情还没真的生成时（理论上不该出现）降级为普通节点，
    不生成死链接。route_plan不存在或没有任何天数时返回空字符串，由调用方决定是否
    退回旧版"🌳重点景点导航"列表。

    flagged_days（见docs/ISSUES.md第125条）：这一天的route/lifestyle/pitfall任一层
    历史数据带有citation_check强制放行(force_finalized)状态时，天数卡片上加一条可见提示——
    明确这是尚未重新核验的历史状态，而不是本次生成新判定出的错误；跟POI树木页横幅
    使用同一个设计语言，但挂在"天"这个粒度上，
    不去final_guide.md正文里精确定位是哪一句话（那是LLM整合后的自由文本，不保留
    这种细粒度追溯关系，见第8/35条已知局限）。

    flagged_detail（entity级粒度下沉）：day -> ["字段标签：entity1、entity2", ...]，
    有值时横幅文案动态列出具体哪个字段/哪些名词未通过核验，取代固定死"行程安排/
    餐饮住宿/避雷提示"这几个词的旧文案——旧文案跟真实触发原因经常对不上（比如
    outfit全部触发时完全没提"穿搭"）。没有detail（旧数据/字段为空）时退回旧文案，
    保持向后兼容。

    notes_by_day（用户备注/annotation layer，docs/ARCHITECTURE.md第21节）：绑天的注渲染
    进天头部——统一标注"📝 用户备注"，见
    _render_user_notes_block。绑POI的注不在这里渲染（那是树页/天详情页该POI处的展示
    位置，见_render_day_poi_item/树木页渲染）。不传时（向后兼容旧调用方式）不渲染
    任何备注区块。
    """
    if not route_plan or not route_plan.days:
        return ""
    flagged_days = flagged_days or set()
    flagged_detail = flagged_detail or {}
    reviewed_detail = reviewed_detail or {}
    notes_by_day = notes_by_day or {}
    sections = []
    for day in route_plan.days:
        theme_idx = ((day.day - 1) % 8) + 1
        poi_html_parts = []
        for poi in day.pois:
            is_land = bool(poi.needs_detail) and poi.name in tree_index
            if is_land:
                reason_html = (
                    f'<p class="land-why"><b>深入理由</b>{html.escape(poi.needs_detail_reason)}</p>'
                    if poi.needs_detail_reason
                    else ""
                )
                poi_html_parts.append(f"""<a class="land" href="#/tree/{quote(poi.name)}">
  <span class="land-node" aria-hidden="true"></span>
  <div class="land-top">
    <h3>{html.escape(poi.name)}</h3>
    <span class="land-glyph" aria-hidden="true">{html.escape(poi.name[0]) if poi.name else ""}</span>
    <span class="stamp">深度玩法</span>
  </div>
  <p class="land-brief">{html.escape(poi.brief)}</p>
  {reason_html}
  <div class="land-go"><span>展开这一站的长卷</span><span class="arrow">→</span></div>
</a>""")
            else:
                poi_html_parts.append(
                    f'<div class="poi"><h3>{html.escape(poi.name)}</h3>'
                    f'<p>{html.escape(poi.brief)}</p></div>'
                )
        area_title = html.escape(" · ".join(filter(None, [day.city, day.area])))
        notes_html = f'<p class="day-notes">{html.escape(day.notes)}</p>' if day.notes else ""
        day_user_notes_html = _render_user_notes_block(notes_by_day.get(day.day))
        detail_items = flagged_detail.get(day.day) or []
        if day.day in flagged_days and detail_items:
            detail_text = "；".join(detail_items)
            unverified_html = (
                '<div class="banner day-banner"><span class="ic">ⓘ</span>'
                f'<span>本日以下内容保留历史核验未完成状态（本次未重新调用模型），'
                f'不代表已确认错误；具体待核验项：{html.escape(detail_text)}。'
                f'建议以景区/商家现场公示信息为准。</span></div>'
            )
        elif day.day in flagged_days:
            unverified_html = (
                '<div class="banner day-banner"><span class="ic">ⓘ</span>'
                '<span>本日部分内容（行程安排/餐饮住宿/避雷提示）保留历史核验未完成状态，'
                '本次未重新调用模型，不代表已确认错误；建议以景区/商家现场公示信息为准。</span></div>'
            )
        else:
            unverified_html = ""
        reviewed_items = reviewed_detail.get(day.day) or []
        reviewed_html = (
            '<div class="banner day-banner review-passed"><span class="ic">✓</span>'
            f'<span>人工复核通过：{html.escape("；".join(reviewed_items))}。原始生成状态保留用于审计。</span></div>'
            if reviewed_items else ""
        )
        sections.append(f"""<section class="day" style="--dc:var(--d{theme_idx});--dc-soft:var(--d{theme_idx}-soft)">
  <div class="day-head">
    <div class="day-num">{day.day:02d}</div>
    <div>
      <h2>{area_title}</h2>
      {notes_html}
      {unverified_html}
      {reviewed_html}
      {day_user_notes_html}
      <a class="day-link" href="#/day/{day.day}">查看完整这天攻略 →</a>
    </div>
  </div>
  <div class="day-body">{''.join(poi_html_parts)}</div>
</section>""")
    return f'<main class="forest-body">{"".join(sections)}</main>'


_TECHNICAL_HEADING_PATTERN = re.compile(r"参考来源|重点景点深度玩法")


def _split_appendix_blocks(appendix_md: str) -> tuple[list[tuple[str, str]], str]:
    """appendix_md（_split_guide_by_day切出来的跨天尾部内容）按`## `再拆一层。
    真实样本里这段尾部其实混了两类完全不同性质的内容：必备清单/避雷提示汇总/
    待确认事项这类"用户行前该看"的提示，和"参考来源"（程序生成的引用清单表格）
    +"重点景点深度玩法"（程序拼接自POI详情文件的纯文字转储，跟已有的交互式
    树木页内容重复）这类技术性附录——后者体量往往比前者大得多（青甘真实样本
    里参考来源+POI转储合计1万3千字，占了appendix的90%），不应该跟必备清单混在
    一起展示在页面靠前的位置。

    从标题首次命中"参考来源"或"重点景点深度玩法"的区块开始（含该区块本身）
    到appendix结尾，全部归为
    技术性附录(back_md)；之前的区块(front_blocks)保留原标题(通常已经带emoji前缀，
    如"📦 行前必备清单")分别渲染成独立卡片。没有区块能匹配到"参考来源"字样时
    （比如这份final_guide.md压根没有程序追加引用清单的老版本），不强行拆分，
    全部归进front_blocks，不丢内容也不误伤。
    """
    if not appendix_md.strip():
        return [], ""
    matches = list(_H2_SPLIT_PATTERN.finditer(appendix_md))
    if not matches:
        return [("🎒 实用提示", appendix_md)], ""

    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(appendix_md)
        blocks.append((m.group(1).strip(), appendix_md[start:end]))

    ref_idx = next(
        (i for i, (heading, _) in enumerate(blocks) if _TECHNICAL_HEADING_PATTERN.search(heading)),
        None,
    )
    if ref_idx is None:
        return blocks, ""

    front_blocks = blocks[:ref_idx]
    back_md = "".join(b for _, b in blocks[ref_idx:])
    return front_blocks, back_md


def _render_forest_extras(intro_md: str, front_blocks: list[tuple[str, str]]) -> str:
    """跨天、不属于任何单一天、但对用户"行前准备"有用的内容："行程概览"(intro)+
    必备清单/避雷提示汇总/待确认事项这类front_blocks（来自_split_appendix_blocks）。
    每块渲染成独立卡片（复用Day详情页同款.card/.card-body视觉语言），不再是
    整块markdown原样倾倒。技术性附录（参考来源/POI转储）不在这里，见
    _render_forest_technical_appendix（页面最下方）。
    """
    parts = []
    if intro_md.strip():
        rendered = _render_md(intro_md)
        parts.append(f'<div class="card"><h2><span class="ic">📋</span>行程概览</h2>'
                      f'<div class="card-body">{rendered}</div></div>')
    for heading, block_md in front_blocks:
        body = _H2_SPLIT_PATTERN.sub("", block_md, count=1).strip()
        if not body:
            continue
        rendered = _render_md(body)
        parts.append(f'<div class="card"><h2>{html.escape(heading)}</h2>'
                      f'<div class="card-body">{rendered}</div></div>')
    if not parts:
        return ""
    return f'<section class="forest-extra">{"".join(parts)}</section>'


def _render_forest_technical_appendix(back_md: str) -> str:
    """Render the technical appendix for a trusted operations/audit view.

    The public site intentionally does not call this helper; the raw appendix
    remains available from the workspace for backend tooling.
    """
    if not back_md.strip():
        return ""
    rendered = _render_md(back_md)
    return (
        '<section class="forest-appendix">'
        '<h2 class="guide-heading">📚 技术附录</h2>'
        f'<div class="guide-content">{rendered}</div></section>'
    )


def _strip_public_technical_appendix(final_guide_md: str) -> str:
    """Remove the generated reference/POI appendix from forest/day-card HTML.

    The original markdown and ``final_guide.meta.json`` remain untouched for
    the full-guide view; forest/day-card rendering omits the appendix because
    it is redundant with the interactive POI pages.
    """
    return contract_body_markdown(final_guide_md)


def _render_guide_view(final_guide_md: str) -> str:
    """渲染公开的完整攻略正文，不暴露程序生成的技术附录。"""
    public_guide_md = _strip_public_technical_appendix(final_guide_md)
    if not public_guide_md:
        rendered_html = "<p>（本次还没有生成最终攻略正文）</p>"
    else:
        rendered_html = _render_md(public_guide_md)
    return f"""<div id="view-guide" class="view" hidden>
  <header class="tree-hero" id="guide-hero">
    <div class="hero-top">
      <button class="back-btn" id="back-forest-guide">← 返回森林</button>
      <span class="hero-crumb">完整攻略原文</span>
    </div>
    <h1>📖 完整攻略正文</h1>
  </header>
  <div class="tree-body">
    <div class="guide-content">{rendered_html}</div>
  </div>
</div>"""


def _render_forest_section(
    intro_md: str,
    appendix_md: str,
    route_plan,
    trip_title: str,
    poi_details: list[dict],
    tree_index: dict,
    map_html: str,
    lifestyle=None,
    pitfall=None,
    user_notes: Optional[list[dict]] = None,
    poster_keys: Optional[set[str]] = None,
    review_projection: Optional[dict[str, dict]] = None,
) -> str:
    """组装面向游客的森林页：封面 + 行程总览(route_plan逐天路线) + 行程概览/必备清单等
    卡片(intro/appendix前段) + 天数长卷 + 行程级用户备注 + 路线地图 + (无route_plan时的
    兜底导航) + 页脚。森林页不展开参考来源和POI文字转储；完整攻略视图单独保留参考来源，
    供用户核对素材依据。
    用户"行前该看"的内容放在天数长卷之前，见_split_appendix_blocks。
    intro_md/appendix_md/route_plan都缺失时展示"还没有生成"提示。

    lifestyle/pitfall（可选，见docs/ISSUES.md第125条）：用于汇总"哪些天有内容被
    citation_check强制放行(force_finalized)"，传给_render_forest_days在天数卡片上
    加可见提示。不传时（向后兼容旧调用方式）天数长卷只看route_plan自己的
    citation_status，不检查lifestyle/pitfall。

    flagged_detail（entity级粒度下沉，见docs/ISSUES.md第137条前后）：day -> 该天
    具体未通过核验的"标签: entity列表"文本，供_render_forest_days渲染出精确到
    具体字段/具体名词的横幅文案，而不是固定死"行程安排/餐饮住宿/避雷提示"这几个词——
    真实案例：outfit（穿搭）8天全部触发，但固定文案完全没提到"穿搭"这个词，用户
    看到的警告文案跟实际触发原因对不上。

    user_notes（用户备注/annotation layer，docs/ARCHITECTURE.md第21节）：分POI级/天级/
    行程级(含孤儿)渲染，见_classify_user_notes。不传时不渲染任何备注区块。
    """
    if not intro_md and not appendix_md and not (route_plan and route_plan.days):
        return "<p>（本次还没有生成最终攻略正文）</p>"

    front_blocks, _technical_appendix_md = _split_appendix_blocks(appendix_md)

    _LIFESTYLE_FIELD_LABELS = {"food": "美食", "outfit": "穿搭", "lodging": "住宿", "play": "游玩"}

    flagged_days: set[int] = set()
    flagged_detail: dict[int, list[str]] = {}
    reviewed_detail: dict[int, list[str]] = {}
    review_projection = review_projection or {}
    if route_plan:
        for day in route_plan.days:
            if day.citation_status == "force_finalized":
                projection = review_projection.get(f"route_day:{day.day}") or {}
                label = f"行程安排：{'、'.join(day.missing_entities)}" if day.missing_entities else "行程安排"
                if projection.get("effective_status") == "review_passed":
                    reviewed_detail.setdefault(day.day, []).append(label)
                else:
                    flagged_days.add(day.day)
                    if day.missing_entities:
                        flagged_detail.setdefault(day.day, []).append(label)
    for output, source_label in ((lifestyle, ""), (pitfall, "避雷提示·")):
        if not output:
            continue
        for field in ("food", "outfit", "lodging", "play"):
            category = getattr(output, field, None)
            if not category:
                continue
            field_label = source_label + _LIFESTYLE_FIELD_LABELS.get(field, field)
            for item in category.items:
                if item.citation_status == "force_finalized":
                    category_name = "lifestyle" if output is lifestyle else "pitfall"
                    projection = review_projection.get(f"{category_name}:{field}:day:{item.day}") or {}
                    label = f"{field_label}：{'、'.join(item.missing_entities)}" if item.missing_entities else field_label
                    if projection.get("effective_status") == "review_passed":
                        reviewed_detail.setdefault(item.day, []).append(label)
                    else:
                        flagged_days.add(item.day)
                        if item.missing_entities:
                            flagged_detail.setdefault(item.day, []).append(label)

    notes_by_poi, notes_by_day, trip_notes = _classify_user_notes(user_notes or [], route_plan)

    cover_html = _render_forest_cover(trip_title, route_plan)
    poster_keys = poster_keys or set()
    overview_html = _render_forest_overview(route_plan, "overall" in poster_keys)
    public_intro_md = _strip_public_technical_appendix(intro_md)
    extras_html = _render_forest_extras(public_intro_md, front_blocks)
    trip_notes_html = _render_trip_level_notes_section(trip_notes)
    # 行程级用户备注放在"行程概览/必备清单/预算"卡片(extras_html)之后、天数长卷(days_html)
    # 之前——它是整体性的旁注，放在天数列表上方更显眼，用户进森林页就能看到，而不是原来
    # 排在天数长卷之后要往下翻才看到。见docs/ARCHITECTURE.md第21.4节（2026-08-21调整位置）。
    days_html = _render_forest_days(
        route_plan, tree_index, flagged_days, flagged_detail, reviewed_detail, notes_by_day,
    )
    # 天数长卷已经用land地标卡覆盖了进入树木页的入口；只有route_plan缺失、
    # 长卷整体没渲染出来时，才退回旧版导航列表，避免用户完全无法进入树木页。
    nav_html = "" if (route_plan and route_plan.days) else _render_tree_nav(poi_details)
    return f"""{cover_html}
{overview_html}
{extras_html}
{trip_notes_html}
{days_html}
<section class="forest-map"><h2 class="map-heading">🗺️ 路线地图</h2>{map_html}</section>
{nav_html}
<p class="guide-fulltext-link"><a href="#/guide">📖 查看完整攻略原文 →</a></p>
<footer class="forest-foot"><span class="foot-seal">卷 终</span></footer>"""


_PITFALL_CATEGORY_BY_SECTION = {"游玩": "play", "美食": "food", "穿搭": "outfit", "住宿": "lodging"}


def _pitfall_warning(pitfall: Optional[PitfallOutput], category_key: str, day: int) -> str:
    """从PitfallOutput里按类别+天数取warning文案，取不到/为空都返回""，调用方据此
    决定要不要渲染⚠️提示条——不使用target_recommendation字段，那部分内容已经在
    final_guide.md对应小节的叙述文字里覆盖过，没必要重复展示。
    """
    if pitfall is None:
        return ""
    category = getattr(pitfall, category_key, None)
    if category is None:
        return ""
    for item in category.items:
        if item.day == day:
            return item.warning or ""
    return ""


def _render_warn_chip(warning: str) -> str:
    if not warning:
        return ""
    return f'<div class="warn-chip"><span class="ic">⚠️</span><span>{html.escape(warning)}</span></div>'


def _render_day_intro_card(day_intro_text: str, timeline: Optional[list[dict]]) -> str:
    """日开头"行程节奏"文字：能提取出>=2个时间点就渲染成时间轴样式，否则降级成
    普通段落卡片；day_intro_text本身为空（比如这天markdown直接从###小标题开始，
    没有概述段）时不渲染这张卡。"""
    if not day_intro_text.strip():
        return ""
    if timeline:
        rows = "".join(
            f'<div class="t-row clock"><span class="clock-time">{html.escape(e["time"])}</span>'
            f'{html.escape(e["text"])}</div>'
            for e in timeline
            if e["text"]
        )
        body = f'<div class="timeline">{rows}</div>'
    else:
        rendered = _render_md(day_intro_text)
        body = f'<div class="card-body">{rendered}</div>'
    return f'<div class="card"><h2><span class="ic">🕐</span>行程速览</h2>{body}</div>'


def _render_day_poi_item(poi, tree_index: dict, day_num: int, notes_by_poi: Optional[dict] = None) -> str:
    """Day详情页里的单个POI条目：needs_detail=true且真的生成过详情(在tree_index里)
    渲染成紧凑版"land-mini"地标卡（可点击跳树木详情页，带来源标记from=day-N，
    让树木页的返回按钮能回到这个Day页而不是森林首页）；其余渲染成普通条目。

    notes_by_poi（用户备注/annotation layer，docs/ARCHITECTURE.md第21节）：绑该POI的
    未删备注，渲染在条目下方，标注"📝 用户备注"。
    """
    notes_html = _render_user_notes_block((notes_by_poi or {}).get((day_num, poi.name)))
    is_land = bool(poi.needs_detail) and poi.name in tree_index
    if is_land:
        return f"""<a class="land-mini" href="#/tree/{quote(poi.name)}?from=day-{day_num}">
  <div class="top"><b>{html.escape(poi.name)}</b><span class="stamp-mini">深度玩法 →</span></div>
  <p>{html.escape(poi.brief)}</p>
</a>{notes_html}"""
    return f'<div class="poi-item"><b>{html.escape(poi.name)}</b><p>{html.escape(poi.brief)}</p>{notes_html}</div>'


def _render_day_play_card(
    day: RouteDay, section_md: str, tree_index: dict, warning: str, notes_by_poi: Optional[dict] = None
) -> str:
    """"游玩"卡片：结构化POI列表(来自route_plan，可靠、含land-mini区分) + 该天
    `### 游玩`小节的原始叙述文字(渲染成HTML，包含避雷提示等markdown里已经写好的
    细节) + pitfall.json里play类目的warning(结构化数据来源，和markdown叙述可能有
    语义重复，可接受)。
    """
    poi_html = "".join(_render_day_poi_item(poi, tree_index, day.day, notes_by_poi) for poi in day.pois)
    body_html = _render_md(section_md) if section_md.strip() else ""
    warn_html = _render_warn_chip(warning)
    return f"""<div class="card"><h2><span class="ic">🎯</span>游玩详情</h2>
{warn_html}
{poi_html}
<div class="card-body">{body_html}</div>
</div>"""


def _render_day_category_card(icon: str, title: str, section_md: str, warning: str) -> str:
    """美食/穿搭/住宿通用卡片：该分类下所有匹配到的`###`小节markdown渲染 + 对应
    pitfall.json类目的warning提示条。section_md为空时返回""（这天没这个分类内容，
    不渲染空卡片）。
    """
    if not section_md.strip():
        return ""
    rendered = _render_md(section_md)
    warn_html = _render_warn_chip(warning)
    return f'<div class="card"><h2><span class="ic">{icon}</span>{title}</h2>{warn_html}<div class="card-body">{rendered}</div></div>'


def _render_day_misc_card(misc_sections: list[tuple[str, str]]) -> str:
    """分类不到游玩/美食/穿搭/住宿任何一类的杂项子标题(带娃提示/天气备选等每个
    场景不一样的内容)兜底进这一张"补充提示"卡，保留各自原始小标题，不丢内容。
    """
    if not misc_sections:
        return ""
    blocks = []
    for heading, section_md in misc_sections:
        rendered = _render_md(section_md)
        blocks.append(f'<h3>{html.escape(heading)}</h3><div class="card-body">{rendered}</div>')
    return f'<div class="card"><h2><span class="ic">📌</span>补充提示</h2>{"".join(blocks)}</div>'


def _render_day_page(
    day: RouteDay,
    day_intro_text: str,
    timeline: Optional[list[dict]],
    sections_by_category: dict[str, str],
    misc_sections: list[tuple[str, str]],
    pitfall: Optional[PitfallOutput],
    tree_index: dict,
    total_days: int,
    notes_by_poi: Optional[dict] = None,
    notes_by_day: Optional[dict] = None,
    has_poster: bool = False,
) -> str:
    """组装单天的完整Day详情页：头部(日期数字+city/area/notes) + 上一天/下一天
    导航 + 行程速览卡 + 游玩/美食/穿搭/住宿卡 + 补充提示兜底卡 + 完整原文兜底
    链接。theme_idx跟随_render_forest_days同款八色轮换，保证同一天在首页和Day页
    颜色一致。

    notes_by_poi/notes_by_day（用户备注/annotation layer，docs/ARCHITECTURE.md第21节）：
    绑该天的注渲染在头部；绑POI的注透传给_render_day_play_card渲染在对应POI条目下方。
    """
    theme_idx = ((day.day - 1) % 8) + 1
    area_title = html.escape(" · ".join(filter(None, [day.city, day.area]))) or f"Day {day.day}"
    notes_html = f'<div class="sub">{html.escape(day.notes)}</div>' if day.notes else ""
    day_user_notes_html = _render_user_notes_block((notes_by_day or {}).get(day.day))

    prev_link = f'<a href="#/day/{day.day - 1}">‹ Day {day.day - 1}</a>' if day.day > 1 else ""
    next_link = f'<a href="#/day/{day.day + 1}">Day {day.day + 1} ›</a>' if day.day < total_days else ""

    cards = [
        _render_day_intro_card(day_intro_text, timeline),
        _render_day_play_card(day, sections_by_category.get("游玩", ""), tree_index,
                               _pitfall_warning(pitfall, "play", day.day), notes_by_poi),
        _render_day_category_card("🍽️", "美食", sections_by_category.get("美食", ""),
                                   _pitfall_warning(pitfall, "food", day.day)),
        _render_day_category_card("👗", "穿搭", sections_by_category.get("穿搭", ""),
                                   _pitfall_warning(pitfall, "outfit", day.day)),
        _render_day_category_card("🏨", "住宿", sections_by_category.get("住宿", ""),
                                   _pitfall_warning(pitfall, "lodging", day.day)),
        _render_day_misc_card(misc_sections),
    ]
    cards_html = "".join(c for c in cards if c)

    poster_action = f'<button class="poster-action poster-action-light" data-poster="day{day.day}">查看 Day{day.day} 海报</button>' if has_poster else ""
    return f"""<section id="day-page-{day.day}" class="view-day" data-day="{day.day}" hidden
  style="--dc:var(--d{theme_idx});--dc-soft:var(--d{theme_idx}-soft)">
  <div class="topnav">
    <button class="back-btn-flat" data-back-forest>← 返回森林</button>
    <div class="daynav">{prev_link}{next_link}</div>
  </div>
  <div class="head-card">
    <div class="day-chip">DAY {day.day}</div>
    <h1>{area_title}</h1>
    {notes_html}
    {poster_action}
  </div>
  {day_user_notes_html}
  {cards_html}
  <p class="guide-fulltext-link"><a href="#/guide">📖 查看这一天的完整原文 →</a></p>
</section>"""


def _render_all_day_pages(
    route_plan,
    final_guide_md: str,
    pitfall: Optional[PitfallOutput],
    tree_index: dict,
    notes_by_poi: Optional[dict] = None,
    notes_by_day: Optional[dict] = None,
    poster_keys: Optional[set[str]] = None,
) -> str:
    """拆分final_guide.md + 循环渲染每一天的Day详情页，拼接成一整段HTML（每天一个
    `<section id="day-page-N">`，JS路由按hash切换显示哪个）。route_plan缺失或没有
    天数时返回空字符串——没有route_plan就没有POI/城市这些Day页头部必需的结构化
    数据，此时天数长卷本身也不会渲染(_render_forest_days同款判断)，Day页自然也
    没有入口链过来，不需要单独处理。
    """
    if not route_plan or not route_plan.days:
        return ""
    poster_keys = poster_keys or set()
    total_days = len(route_plan.days)
    _, day_blocks, _ = _split_guide_by_day(final_guide_md, total_days)

    pages = []
    for day, block_md in zip(route_plan.days, day_blocks):
        day_intro_text, sections = _split_day_block_into_sections(block_md) if block_md else ("", [])
        timeline = _extract_timeline(day_intro_text) if day_intro_text else None
        sections_by_category: dict[str, str] = {}
        misc_sections: list[tuple[str, str]] = []
        for heading, section_md in sections:
            category = _classify_section_heading(heading)
            if category == "其他":
                misc_sections.append((heading, section_md))
            else:
                sections_by_category[category] = (
                    sections_by_category.get(category, "") + "\n\n" + section_md
                ).strip()
        pages.append(_render_day_page(
            day, day_intro_text, timeline, sections_by_category, misc_sections,
            pitfall, tree_index, total_days, notes_by_poi, notes_by_day,
            f"day{day.day}" in poster_keys,
        ))
    return "".join(pages)



def _collect_poi_coordinates(project_id: str) -> list[dict]:
    """读route_plan.json的POI列表，逐个查坐标（全局缓存）。跳过查询失败的POI，
    不影响其余渲染；route_plan不存在或AMAP_GEOCODE_API_KEY未配置时返回空列表。
    order是POI在当天列表里的1-based顺序（route_plan.json本身就是访问顺序），
    供地图给图钉编号、串联驾车路线用，不需要为此改schema。
    """
    route_plan = store.read_route_plan(project_id)
    if not route_plan:
        return []

    requirements = store.read_requirements(project_id)
    topic = (requirements or {}).get("topic", "")

    points = []
    for day in route_plan.days:
        city = day.city or ""
        order = 0
        for poi in day.pois:
            coord = get_cached_or_geocode(topic, poi.name, city=city)
            if coord is None:
                continue
            order += 1
            lng, lat = coord
            points.append({"name": poi.name, "day": day.day, "order": order, "lng": lng, "lat": lat})
    return points


def _haversine_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """球面距离公式，算两点间直线距离（公里）。用作驾车路线异步查询返回前的
    即时占位显示，网络请求慢/失败时也能有个兜底数字，不让图例长时间空白。
    """
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# 和_TREE_STYLE里--d1~--d8的hex值保持一致（见_render_forest_days的theme_idx
# 循环逻辑），这样地图上某天的颜色和"天数长卷"里那天卡片的颜色一致。这里刻意存成
# 具体hex而不是"var(--d1)"字符串——AMap.Polyline的strokeColor是直接喂给canvas
# 画布的颜色值，不经过浏览器CSS级联解析，CSS变量引用在这个位置不会生效。
# 8色（而不是3色）是为了覆盖常见的3~8天行程不重复撞色——3色循环时7/8天长线
# 行程会出现同色不同天的圆圈套同一个天内序号，视觉上容易被误认成同一天/同一个点
# （见docs/ISSUES.md相关记录）；9天以上仍会循环复用，但目前场景库里没有更长的行程。
_DAY_ROUTE_COLORS = [
    "#e1693a", "#d99a2e", "#4b9e56", "#2e9c8f",
    "#2e86c1", "#6e62c9", "#a857a8", "#d15c7c",
]


def _render_map_section(points: list[dict], *, offline: bool = False) -> str:
    """有坐标数据+配置了AMAP_JS_API_KEY时渲染真实地图；否则降级为POI列表提示。
    真实地图会按天分色+编号图钉、串联当天已标注POI之间的驾车路线并标注距离。
    路线查询采用全局串行队列，避免同时请求多天导致高德接口限流；失败时有限重试，
    最终明确显示失败/部分成功状态，不把0误报成有效驾车里程。
    """
    if not points or not AMAP_JS_API_KEY:
        if not points:
            note = (
                "📍 当前静态演示未启用在线地图，路线顺序请查看下方行程。"
                if offline
                else "📍 暂无可展示的POI坐标（未配置高德地图Key，或本次没有查到任何坐标）。"
            )
        else:
            note = "📍 未配置高德地图Key（AMAP_JS_API_KEY），暂以列表展示POI。"
        items = "\n".join(
            f"<li>Day{p['day']} · {html.escape(p['name'])}</li>" for p in points
        )
        return f"<div class='map-fallback'><p>{note}</p><ul>{items}</ul></div>"

    days: dict[int, list[dict]] = {}
    for p in points:
        days.setdefault(p["day"], []).append(p)

    days_payload = []
    for day_no in sorted(days):
        day_points = sorted(days[day_no], key=lambda p: p["order"])
        color = _DAY_ROUTE_COLORS[(day_no - 1) % len(_DAY_ROUTE_COLORS)]
        straight_km = sum(
            _haversine_km(
                day_points[i]["lng"], day_points[i]["lat"],
                day_points[i + 1]["lng"], day_points[i + 1]["lat"],
            )
            for i in range(len(day_points) - 1)
        )
        days_payload.append({
            "day": day_no,
            "color": color,
            "straight_km": round(straight_km, 1),
            "points": [
                {"name": p["name"], "order": p["order"], "lng": p["lng"], "lat": p["lat"]}
                for p in day_points
            ],
        })

    days_json = _dumps_for_script(days_payload)
    legend_rows = "\n".join(
        f'<div class="leg-row"><span class="leg-dot" style="background:{d["color"]}"></span>'
        f'<span>Day{d["day"]}</span>'
        f'<span class="leg-dist" id="day-dist-{d["day"]}">约{d["straight_km"]}km 直线距离'
        + ("，景点间驾车路线计算中…" if len(d["points"]) > 1 else "")
        + "</span></div>"
        for d in days_payload
    )
    legend_html = f'<div class="map-legend">{legend_rows}</div>'

    # 高德2021年底起JS API强制要求先设置安全密钥，否则加载报INVALID_USER_SCODE
    # （即使key本身有效）——AMAP_JS_SECURITY_CODE未配置时（如旧key不受此限制）跳过这段，
    # 不影响其余渲染，详见docs/ISSUES.md。这段必须在加载maps脚本的<script>之前执行。
    security_config = ""
    if AMAP_JS_SECURITY_CODE:
        security_config = (
            "<script>window._AMapSecurityConfig = "
            f'{{ securityJsCode: {json.dumps(AMAP_JS_SECURITY_CODE)} }};</script>\n'
        )
    return f"""<div id="map" style="width:100%;height:420px;border-radius:8px;"></div>
{legend_html}
{security_config}<script src="https://webapi.amap.com/maps?v=2.0&key={AMAP_JS_API_KEY}"></script>
<script>
  var _days = {days_json};
  var _map = new AMap.Map('map', {{}});
  _days.forEach(function(d) {{
    d.points.forEach(function(p) {{
      var marker = new AMap.Marker({{
        position: [p.lng, p.lat],
        map: _map,
        offset: new AMap.Pixel(-13, -13),
        content: '<div class="route-pin" style="background:' + d.color + '">' + p.order + '</div>',
        title: p.name
      }});
      var infoWindow = new AMap.InfoWindow({{ content: 'Day' + d.day + ' · #' + p.order + ' ' + p.name }});
      marker.on('click', function() {{ infoWindow.open(_map, marker.getPosition()); }});
    }});
  }});
  _map.setFitView();

  AMap.plugin(['AMap.Driving'], function() {{
    // 所有天数共用一个串行队列，避免并发请求触发高德限流/随机失败。
    var driving = new AMap.Driving({{ map: _map, hideMarkers: true, showTraffic: false }});
    var queue = [];
    var MAX_RETRIES = 2;
    var REQUEST_TIMEOUT_MS = 15000;

    function updateDistance(d) {{
      var state = d._routeState;
      var span = document.getElementById('day-dist-' + d.day);
      if (!span) {{ return; }}
      if (state.failedLegs === 0 && state.successLegs === state.totalLegs) {{
        span.textContent = (state.totalMeters / 1000).toFixed(1) + 'km 驾车（景点间）';
      }} else if (state.successLegs > 0) {{
        if (state.failedLegs > 0) {{
          span.textContent = (state.totalMeters / 1000).toFixed(1) + 'km 驾车（景点间，'
            + state.failedLegs + '段未返回）';
        }} else {{
          span.textContent = (state.totalMeters / 1000).toFixed(1) + 'km 驾车（景点间，仍在计算）';
        }}
      }} else if (state.failedLegs >= state.totalLegs) {{
        span.textContent = '驾车路线未返回（直线约' + d.straight_km + 'km）';
      }}
    }}

    _days.forEach(function(d) {{
      if (d.points.length < 2) {{ return; }}
      d._routeState = {{
        totalMeters: 0,
        totalLegs: d.points.length - 1,
        successLegs: 0,
        failedLegs: 0,
      }};
      for (var i = 0; i < d.points.length - 1; i++) {{
        queue.push({{ day: d, index: i, attempts: 0 }});
      }}
    }});

    function runNext() {{
      if (!queue.length) {{ return; }}
      var item = queue.shift();
      var d = item.day;
      var origin = [d.points[item.index].lng, d.points[item.index].lat];
      var dest = [d.points[item.index + 1].lng, d.points[item.index + 1].lat];
      var settled = false;
      var timer = setTimeout(function() {{ finish(false); }}, REQUEST_TIMEOUT_MS);

      function finish(ok, result) {{
        if (settled) {{ return; }}
        settled = true;
        clearTimeout(timer);
        if (ok) {{
          var route = result.routes[0];
          d._routeState.totalMeters += route.distance || 0;
          d._routeState.successLegs += 1;
          var path = [];
          (route.steps || []).forEach(function(step) {{
            if (step.path) {{ path = path.concat(step.path); }}
          }});
          if (path.length) {{
            new AMap.Polyline({{ map: _map, path: path, strokeColor: d.color, strokeWeight: 4, strokeOpacity: 0.85 }});
          }}
          updateDistance(d);
          runNext();
          return;
        }}
        if (item.attempts < MAX_RETRIES) {{
          item.attempts += 1;
          queue.push(item);
          setTimeout(runNext, 500 * item.attempts);
          return;
        }}
        d._routeState.failedLegs += 1;
        updateDistance(d);
        runNext();
      }}

      driving.search(origin, dest, function(status, result) {{
        finish(status === 'complete' && result && result.routes && result.routes.length > 0, result);
      }});
    }}

    runNext();
  }});
</script>"""


def _build_tree_index(poi_details: list[dict], route_plan) -> dict:
    """给每个有深度详情的POI算出{day, city, area, brief}，city/area/brief从route_plan.json
    按day+name匹配对应的RoutePoi；匹配不到就留空，不报错（route_plan不存在时同样留空）。
    """
    poi_meta_by_day_name: dict[tuple[int, str], dict] = {}
    if route_plan:
        for day in route_plan.days:
            for poi in day.pois:
                poi_meta_by_day_name[(day.day, poi.name)] = {
                    "city": day.city or "",
                    "area": day.area or "",
                    "brief": poi.brief or "",
                }

    index = {}
    for detail in poi_details:
        name = detail.get("poi_name", "")
        day = detail.get("day")
        meta = poi_meta_by_day_name.get((day, name), {"city": "", "area": "", "brief": ""})
        index[name] = {"day": day, **meta}
    return index


_UPLOADED_PHOTO_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
_PHOTO_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _write_photo_asset(content: bytes, mime: str, assets_dir: Path) -> str:
    """把完整原图写入内容哈希命名的静态资源文件，并返回相对HTML的URL。"""
    assets_dir.mkdir(parents=True, exist_ok=True)
    extension = _PHOTO_EXT_BY_MIME.get(mime, "jpg")
    digest = hashlib.sha256(content).hexdigest()[:24]
    filename = f"img-{digest}.{extension}"
    path = assets_dir / filename
    if not path.exists():
        path.write_bytes(content)
    return f"{assets_dir.name}/{filename}"


def _photo_file_asset(project_id: str, relative_path: str, assets_dir: Path) -> Optional[str]:
    """把用户上传的完整原图复制到site_assets，文件缺失时返回None。"""
    path = store.workspace_dir(project_id) / relative_path
    if not path.exists():
        return None
    ext = path.suffix.lstrip(".").lower()
    mime = _UPLOADED_PHOTO_MIME.get(ext, "image/jpeg")
    return _write_photo_asset(path.read_bytes(), mime, assets_dir)


def _data_url_asset(data_url: str, assets_dir: Path) -> Optional[str]:
    """将高德缓存返回的data URL落成完整原图文件，避免继续内嵌进HTML。"""
    match = re.fullmatch(r"data:(image/[^;]+);base64,(.+)", data_url, flags=re.DOTALL)
    if not match:
        return None
    mime, encoded = match.groups()
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    return _write_photo_asset(content, mime, assets_dir)


def _attach_poi_overview_photos(
    poi_details: list[dict],
    topic: str,
    project_id: str,
    assets_dir: Path,
    *,
    allow_remote: bool = True,
) -> None:
    """给每个PoiDetailFile字段追加overview_photo_url，有真实照片时前端在树木页
    英雄区渲染真实照片，查不到/未配key时该字段缺失，前端保留原有英雄区样式（就地修改
    传入的dict，不返回新对象）。

    优先级：用户手动上传（uploaded_overview_photo_path非空）> 高德自动查图 > 无图占位。
    用户上传是管理后台"POI图片"页面人工指定的，不存在自动匹配图文不符的风险，直接信任
    不做相关性校验；只有用户没有上传时才回退到下面这套高德自动查图逻辑。

    查询词直接用detail的poi_name本身（如"张掖·七彩丹霞景区"），不依赖route_plan
    的city字段——这是真实E2E跑测（成都+青甘大环线）验证后从"途经点(segment)级别+
    city限定"收敛出的最终方案，途经点级别在长线自驾/多城市行程里几乎不可行（city
    无法可靠限定到景点内部子设施级别精度），完整景区名作为搜索词命中率和准确率
    都远高于碎片化的途经点短语。演变过程和真实误配案例见src/tools/poi_photo.py
    模块docstring。
    """
    for detail in poi_details:
        poi_name = detail.get("poi_name", "")
        if not poi_name:
            continue
        uploaded_path = detail.get("uploaded_overview_photo_path")
        if uploaded_path:
            photo = _photo_file_asset(project_id, uploaded_path, assets_dir)
            if photo:
                detail["overview_photo_url"] = photo
                detail["overview_photo_credit"] = detail.get("overview_photo_credit") or "图片来源：用户上传"
                continue
        if not allow_remote:
            continue
        photo = get_cached_or_poi_overview_photo(topic, poi_name)
        if photo:
            asset = _data_url_asset(photo, assets_dir)
            if asset:
                detail["overview_photo_url"] = asset
                detail["overview_photo_credit"] = "图片来源：高德地图"


def _attach_segment_uploaded_photos(
    poi_details: list[dict], project_id: str, assets_dir: Path
) -> None:
    """给每个途经点(route_segment)追加uploaded_photo_url，仅来自用户手动上传——
    途经点粒度没有自动查图路径（见上方_attach_poi_overview_photos的说明），为空时前端
    保留.img-ph占位框。就地修改传入的dict，不返回新对象。"""
    for detail in poi_details:
        for route in detail.get("routes", []):
            for segment in route.get("route_segments", []):
                uploaded_path = segment.get("uploaded_photo_path")
                if not uploaded_path:
                    continue
                photo = _photo_file_asset(project_id, uploaded_path, assets_dir)
                if photo:
                    segment["uploaded_photo_url"] = photo


def _build_trees_payload(poi_details: list[dict]) -> dict:
    return {detail.get("poi_name", ""): detail for detail in poi_details}


def _render_tree_nav(poi_details: list[dict]) -> str:
    if not poi_details:
        return ""
    links = "\n".join(
        f"<li><a href='#/tree/{quote(d.get('poi_name', ''))}'>"
        f"Day{d.get('day')} · {html.escape(d.get('poi_name', ''))}</a></li>"
        for d in poi_details
    )
    return f"<nav><h2>🌳 重点景点导航</h2><ul>{links}</ul></nav>"


def _dumps_for_script(payload) -> str:
    """json.dumps后转义字面量'</'，防止payload里的自由文本（agent生成的highlight/
    recommend_reason，或用户输入的topic）恰好包含'</script>'提前把<script>标签腰斩。
    对三个嵌入的JSON payload统一处理，不假设只有TREES这一处会出现这个子串。"""
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


_STYLE = """
* { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html { scroll-behavior: smooth; }
body { background: var(--paper); color: var(--ink); font-family: var(--sans); font-size: 16px; line-height: 1.65; overflow-x: hidden; }
button { font-family: inherit; }
:focus-visible { outline: 2px solid var(--cinnabar); outline-offset: 2px; }

/* 窄屏渲染表格用：容器+min-width，防止列被压窄留难看空白。
   min-width:100%而不是width:100%——内容天然比容器窄时仍占满宽度，
   内容天然更宽时不强行压缩列宽（现cell内overflow-wrap兜底断行，见下条）。*/
.table-scroll{margin:8px 0}
.table-scroll table{min-width:100%}
/* 技术附录引用清单表格+#/guide全文dump共用这个class。那张表的doc_id/chunk_id列是
   无空格标识符（web核实的合成id如web-gansumuseum-2026尤其长）。早期用nowrap逼
   table按内容自然宽度撑开+overflow-x:auto横向滚动，但用户反馈doc_id列长id导致
   整张表横向溢出要滚动着看，可读性差。改用overflow-wrap:anywhere：优先在连字符/
   CJK字符边界处断行（这类id都带"-"，断在连字符处不丑），只有窄到连字符都塞不下
   才在拉丁字母间断——避免早期"逐字硬断"的丑样，又不再横向溢出。card-body（Day
   详情页/概览卡片里的表格，通常是结构化行程信息）不套这条规则，保留正常换行。*/
.guide-content .table-scroll th, .guide-content .table-scroll td{white-space:normal;overflow-wrap:anywhere}

/* ==================== 森林视图 · 封面 ==================== */
.cover{min-height:100svh;background:linear-gradient(180deg,#253049 0%,#3e4c66 26%,#6f5560 50%,#a6533c 74%,#b4552d 100%);color:#fdf6ec;position:relative;overflow:hidden;
  display:flex;flex-direction:column;justify-content:space-between;
  padding:calc(22px + env(safe-area-inset-top)) 24px 26px}
.cover::before{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(85% 50% at 80% 100%,rgba(255,208,146,.32),transparent 62%)}
.cover-top{display:flex;justify-content:space-between;align-items:flex-start;position:relative}
.cover-kicker{font-size:12px;letter-spacing:.5em;color:#c9c0ae;padding-top:8px}
.cover-seal{flex:none;width:46px;height:46px;border:2px solid #f2ddc4;border-radius:6px;
  outline:1px solid #f2ddc4;outline-offset:2px;color:#ffe9cf;
  font-family:var(--serif);font-size:24px;font-weight:700;
  display:flex;align-items:center;justify-content:center;transform:rotate(4deg)}
.cover-mid{position:relative;margin:8vh 0 6vh}
.cover-title{font-family:var(--serif);font-weight:700;letter-spacing:.04em;
  font-size:clamp(46px,13.5vw,88px);line-height:1.14;text-wrap:balance}
.cover-vertical{position:absolute;right:0;top:50%;transform:translateY(-50%);
  writing-mode:vertical-rl;font-family:var(--serif);font-size:14px;letter-spacing:.55em;
  color:rgba(244,239,230,.32)}
@media (max-width:640px){.cover-vertical{display:none}}
.cover-bottom{position:relative}
.cover-stats{display:flex;gap:22px;flex-wrap:wrap;font-size:13px;color:#e6d9c6;
  border-top:1px solid rgba(253,246,236,.28);padding-top:16px}
.cover-stats b{color:var(--paper);font-family:var(--serif);font-size:22px;font-weight:700;margin-right:5px}
.cover-legend{display:flex;gap:18px;margin-top:14px;font-size:12px;color:#d8cbb8}
.cover-legend i{display:inline-block;vertical-align:-1px;margin-right:6px}
.lg-dot{width:9px;height:9px;border-radius:50%;border:2px solid #d8cbb8}
.lg-dia{width:10px;height:10px;background:var(--cinnabar);transform:rotate(45deg);border-radius:2px}
.cover-scroll{display:flex;align-items:center;gap:10px;margin-top:22px;
  font-size:11px;letter-spacing:.35em;color:#e0d3bf}
.cs-line{width:1px;height:34px;background:linear-gradient(var(--paper),transparent);
  animation:csline 1.8s ease-in-out infinite;transform-origin:top}
@keyframes csline{0%,100%{transform:scaleY(.35);opacity:.4}50%{transform:scaleY(1);opacity:1}}

/* ==================== 森林视图 · 天数长卷 ==================== */
.forest-body{max-width:680px;margin:0 auto;padding:0 18px}
.day{position:relative;padding:40px 0 10px}
.day-head{display:flex;align-items:flex-start;gap:16px;padding:0 2px 10px}
.day-num{font-family:var(--serif);font-weight:700;line-height:.86;
  font-size:clamp(60px,17vw,96px);color:var(--dc,var(--cinnabar))}
.day-head h2{font-family:var(--serif);font-size:22px;margin:8px 0 5px;letter-spacing:.02em}
.day-notes{font-size:13.5px;color:var(--ink2)}
.day-link{display:inline-block;margin-top:6px;font-size:12.5px;font-weight:600;color:var(--dc,var(--cinnabar))}
.guide-fulltext-link{max-width:680px;margin:20px auto 0;padding:0 18px;text-align:center;font-size:13px}
.guide-fulltext-link a{color:var(--ink2)}
.day-body{position:relative;margin-left:8px;padding-left:30px}
.day-body::before{content:"";position:absolute;left:5px;top:6px;bottom:6px;width:2px;background:var(--line2)}

.poi{position:relative;padding:11px 0 11px 4px}
.poi::before{content:"";position:absolute;left:-29px;top:20px;width:9px;height:9px;border-radius:50%;
  background:var(--paper);border:2px solid var(--line2)}
.poi h3{font-size:16px;font-weight:600}
.poi p{font-size:13.5px;color:var(--ink2);margin-top:2px}

.land{display:block;position:relative;color:inherit;text-decoration:none;cursor:pointer;
  background:var(--card);border:1.5px solid var(--ink);border-radius:var(--radius);
  margin:18px 8px 18px 0;box-shadow:0 2px 0 rgba(29,26,22,.06)}
.land::before{content:"";position:absolute;inset:0;z-index:-1;
  border:1.5px solid var(--dc,var(--cinnabar));border-radius:inherit;opacity:.55;
  transform:translate(6px,6px);transition:transform .25s}
.land:active::before{transform:translate(2px,2px)}
.land::after{content:"";position:absolute;left:-25px;top:38px;width:20px;height:2px;
  background:var(--dc,var(--cinnabar))}
.land-node{position:absolute;left:-30px;top:32px;width:12px;height:12px;border-radius:2px;
  background:var(--dc,var(--cinnabar));transform:rotate(45deg);border:2px solid var(--paper)}
.land-top{position:relative;background:var(--dc-soft,var(--cinnabar-soft));
  border-bottom:1.5px solid var(--ink);border-radius:12px 12px 0 0;
  padding:16px 16px 13px;overflow:hidden}
.land-glyph{position:absolute;right:12px;bottom:-14px;font-family:var(--serif);font-weight:700;
  font-size:74px;line-height:1;color:var(--dc,var(--cinnabar));opacity:.16;pointer-events:none}
.land-top h3{font-family:var(--serif);font-size:23px;letter-spacing:.02em;padding-right:86px}
.stamp{position:absolute;top:14px;right:14px;transform:rotate(-6deg);
  color:var(--cinnabar);border:2px solid currentColor;border-radius:4px;
  outline:1px solid currentColor;outline-offset:2px;
  font-family:var(--serif);font-size:11px;font-weight:700;letter-spacing:.15em;
  padding:3px 7px;background:rgba(255,253,248,.85)}
.land-brief{font-size:14px;color:#3a372f;padding:12px 16px 0}
.land-why{font-size:12.5px;color:var(--ink2);padding:8px 16px 0}
.land-why b{color:var(--dc,var(--cinnabar));font-weight:600;margin-right:6px}
.land-go{display:flex;align-items:center;justify-content:space-between;
  padding:12px 16px 15px;font-size:13px;font-weight:600;color:var(--cinnabar)}
.land-go .arrow{font-family:var(--serif);font-size:18px;transition:transform .2s}
.land:active .land-go .arrow{transform:translateX(4px)}

/* ==================== 森林视图 · 地图/完整正文/页脚 ==================== */
.forest-map, .forest-extra, .forest-appendix{max-width:680px;margin:26px auto 0;padding:0 18px}
.forest-appendix{opacity:.72;font-size:12.5px}
.forest-appendix .guide-heading{font-size:15px}
.forest-appendix .guide-content{padding:14px 16px}
.forest-overview{max-width:680px;margin:22px auto 0;padding:0 18px}
.overview-list{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
.overview-row{display:flex;align-items:center;gap:12px;padding:12px 14px;text-decoration:none;color:inherit;
  border-bottom:1px solid var(--line)}
.overview-row:last-child{border-bottom:0}
.overview-row .ov-day{flex:none;font-family:var(--serif);font-weight:700;font-size:13px;color:var(--cinnabar);
  min-width:52px}
.overview-row .ov-body{flex:1;display:flex;flex-direction:column;gap:2px;min-width:0}
.overview-row .ov-body b{font-size:14px;font-weight:600}
.overview-row .ov-pois{font-size:12px;color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.overview-row .ov-arrow{flex:none;color:var(--ink3);font-family:var(--serif)}
.map-heading, .guide-heading{font-family:var(--serif);font-size:20px;margin-bottom:12px;color:var(--ink)}
.map-fallback { background: var(--card); border: 1px solid var(--line); padding: 16px; border-radius: var(--radius); color: var(--ink2); }
.route-pin{width:26px;height:26px;border-radius:50%;color:#fffdf8;font-family:var(--sans);
  font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center;
  border:2px solid var(--paper);box-shadow:0 1px 4px rgba(29,26,22,.35)}
.map-legend{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 16px;margin-top:12px;display:flex;flex-direction:column;gap:6px}
.map-legend .leg-row{display:flex;align-items:center;gap:8px;font-size:13.5px;color:var(--ink2)}
.map-legend .leg-dot{width:12px;height:12px;border-radius:50%;flex:none}
.map-legend .leg-dist{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--ink)}
.guide-content { background: var(--card); border: 1px solid var(--line); padding: 16px 24px; border-radius: var(--radius); word-break: break-word; }
.guide-content h1, .guide-content h2, .guide-content h3 { font-family: var(--serif); line-height: 1.4; margin: 1.2em 0 0.5em; color: var(--ink); }
.guide-content h1:first-child, .guide-content h2:first-child, .guide-content h3:first-child { margin-top: 0; }
.guide-content table { border-collapse: collapse; margin: 12px 0; }
.guide-content th, .guide-content td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
.guide-content th { background: var(--paper2); }
.guide-content blockquote { border-left: 3px solid var(--dc, var(--cinnabar)); margin: 8px 0; padding: 2px 12px; color: var(--ink2); }
.guide-content hr { border: none; border-top: 1px solid var(--line); margin: 16px 0; }
.guide-content code { background: var(--paper2); padding: 1px 4px; border-radius: 3px; }
nav ul { list-style: none; padding: 0 18px; max-width: 680px; margin: 0 auto; }
nav li { margin: 4px 0; }
.forest-foot{max-width:680px;margin:26px auto 0;padding:26px 18px calc(40px + env(safe-area-inset-bottom));
  text-align:center;color:var(--ink3);font-size:12px;letter-spacing:.3em}
.foot-seal{display:inline-block;border-top:1px solid var(--line2);border-bottom:1px solid var(--line2);
  padding:8px 18px;font-family:var(--serif)}

/* ==================== 用户备注/annotation layer（docs/ARCHITECTURE.md第21节） ====================
   跨森林页(天/行程级)/Day详情页(POI级)/树木页共用同一套视觉语言："📝 用户备注"标签+
   浅色卡片，跟攻略正文明确区分开——这是用户自己写的旁注，不是agent产出，见21.1节。 */
.user-notes-block{margin:10px 0}
.user-notes-badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;
  color:#8a6d3b;background:#fdf3d8;border:1px solid #eddca3;border-radius:999px;
  padding:2px 9px;margin-bottom:6px}
.user-note-item{background:#fffdf5;border:1px dashed #e3d5a8;border-radius:8px;
  padding:8px 10px;margin-bottom:6px;font-size:13px;color:var(--ink2);line-height:1.6}
.user-note-item p{margin:0 0 4px;color:var(--ink)}
.user-note-item .user-note-meta{font-size:11px;color:var(--ink3)}

.section-heading-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.section-heading-row .guide-heading{margin-bottom:0}
.poster-action{border:1px solid var(--cinnabar);background:var(--cinnabar-soft);color:var(--cinnabar);
  border-radius:8px;padding:7px 11px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.poster-action:hover{background:#f1d8d1}
.poster-action-light{display:inline-block;margin-top:10px;border-color:rgba(255,253,248,.7);
  background:rgba(255,253,248,.16);color:#fffdf8}
.poster-action-light:hover{background:rgba(255,253,248,.28)}
.poster-lightbox{position:fixed;inset:0;z-index:100;background:rgba(20,17,14,.78);display:flex;
  align-items:center;justify-content:center;padding:18px}
.poster-lightbox[hidden]{display:none}
.poster-lightbox-backdrop{position:absolute;inset:0}
.poster-lightbox-panel{position:relative;z-index:1;display:flex;flex-direction:column;align-items:center;
  width:min(94vw,760px);max-height:96vh;background:var(--paper);border:1px solid var(--line);
  border-radius:12px;padding:12px;box-shadow:0 12px 44px rgba(0,0,0,.35)}
.poster-lightbox-head,.poster-lightbox-actions{width:100%;display:flex;align-items:center;gap:8px}
.poster-lightbox-head{justify-content:space-between;padding:0 2px 8px;font-size:14px;color:var(--ink)}
.poster-lightbox-head button,.poster-lightbox-actions button,.poster-lightbox-actions a{border:1px solid var(--line2);
  background:var(--card);color:var(--ink2);border-radius:7px;padding:6px 10px;font-size:12px;cursor:pointer;text-decoration:none}
.poster-lightbox-image{display:block;max-width:100%;max-height:calc(96vh - 108px);width:auto;height:auto;object-fit:contain;border-radius:6px;background:#eee}
.poster-lightbox-actions{justify-content:center;padding-top:9px}
.poster-lightbox-actions > span{display:flex;gap:8px}
.poster-lightbox-actions[hidden]{display:none}
@media (max-width:520px){.section-heading-row{align-items:flex-start}.poster-action{font-size:11px;padding:6px 8px}.poster-lightbox{padding:8px}.poster-lightbox-panel{padding:8px}}
"""

# 移植自design/poi_detail_page/preview_v3_真实数据_成都三日行.html——只移植树木视图
# 相关部分（英雄区/banner/路线tab/吸顶进度条/卡片流/转场），森林视图的天数长卷/地标卡/
# 巨型日期数字CSS不移植，见本文件模块docstring"树木页视觉升级"一节。
_TREE_STYLE = """
:root{
  --paper:#f4efe6; --paper2:#ece5d6; --card:#fffdf8;
  --ink:#1d1a16; --ink2:#6b6459; --ink3:#a89e8d;
  --cinnabar:#b4432e; --cinnabar-soft:#f7e9e4;
  --line:#ded5c4; --line2:#cfc5b0;
  --done:#2e7d4e; --done-soft:#e6f1ea; --done-line:#c9ddcf;
  --amber-bg:#fff6df; --amber-line:#ecd9a2; --amber-ink:#7a5b13;
  --d1:#e1693a; --d1-soft:#fbe6db;
  --d2:#d99a2e; --d2-soft:#faedd3;
  --d3:#4b9e56; --d3-soft:#e2f0e3;
  --d4:#2e9c8f; --d4-soft:#dcf0ed;
  --d5:#2e86c1; --d5-soft:#dceaf5;
  --d6:#6e62c9; --d6-soft:#e6e3f7;
  --d7:#a857a8; --d7-soft:#f2e1f2;
  --d8:#d15c7c; --d8-soft:#f9e1e7;
  --serif:"Songti SC","STSong","Noto Serif SC","Source Han Serif SC",serif;
  --sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC",sans-serif;
  --radius:14px;
  --sticky-h:104px;
}
[hidden]{display:none!important}
.view-in{animation:viewIn .5s cubic-bezier(.22,.8,.3,1) both}
@keyframes viewIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
.wipe{position:fixed;inset:0;z-index:200;background:var(--wc,#b4432e);color:#fffdf8;
  display:flex;align-items:center;justify-content:center;
  transform:translateY(103%);pointer-events:none}
.wipe.cover-on {transition:transform .48s cubic-bezier(.76,0,.24,1);transform:translateY(0)}
.wipe.cover-off{transition:transform .48s cubic-bezier(.76,0,.24,1);transform:translateY(-103%)}
.wipe-word{font-family:var(--serif);font-size:clamp(26px,7vw,42px);letter-spacing:.12em;
  opacity:0;transform:translateY(12px);transition:opacity .3s .18s,transform .3s .18s}
.wipe.cover-on .wipe-word{opacity:1;transform:none}

.tree-hero{position:relative;overflow:hidden;color:#fffdf8;
  background:var(--tc,var(--cinnabar));
  padding:calc(14px + env(safe-area-inset-top)) 18px 30px;
  box-shadow:inset 0 -3px 0 rgba(29,26,22,.18)}
.tree-hero::before{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(110% 90% at 85% -20%,rgba(255,255,255,.16),transparent 55%)}
.tree-hero.has-photo{background-size:cover;background-position:center;
  aspect-ratio:16/10;min-height:260px}
.tree-hero.has-photo::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(75% 60% at 6% 0%,rgba(20,17,14,.58) 0%,rgba(20,17,14,.24) 42%,transparent 70%)}
.hero-photo-credit{position:relative;font-size:10px;color:rgba(255,253,248,.56);margin-top:2px;opacity:.82}
.hero-top{display:flex;align-items:center;gap:12px;position:relative}
.back-btn{border:1px solid rgba(255,253,248,.55);background:rgba(29,26,22,.14);color:#fffdf8;
  font-size:13px;font-weight:600;padding:8px 14px;border-radius:999px;cursor:pointer;min-height:38px}
.back-btn:active{background:rgba(29,26,22,.28)}
.hero-crumb{font-size:12px;letter-spacing:.08em;color:rgba(255,253,248,.82);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tree-hero h1{position:relative;font-family:var(--serif);font-weight:700;
  font-size:clamp(34px,10vw,52px);line-height:1.2;letter-spacing:.03em;margin:18px 0 10px}
.hero-meta{position:relative;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  font-size:13px;color:rgba(255,253,248,.9)}
.tree-hero.has-photo h1{text-shadow:0 2px 16px rgba(20,17,14,.7),0 1px 4px rgba(20,17,14,.65)}
.tree-hero.has-photo .hero-crumb{text-shadow:0 1px 6px rgba(20,17,14,.65)}
.tree-hero.has-photo .hero-meta{text-shadow:0 1px 6px rgba(20,17,14,.6)}
.hero-glyph{position:absolute;right:2px;bottom:-34px;font-family:var(--serif);font-weight:700;
  font-size:150px;line-height:1;color:rgba(255,253,248,.13);pointer-events:none}
.reopen{border:0;background:none;color:rgba(255,253,248,.75);font-size:12px;cursor:pointer;
  text-decoration:underline;text-underline-offset:3px}

.tree-body{max-width:680px;margin:0 auto;padding:18px 18px calc(44px + env(safe-area-inset-bottom))}

.banner{display:flex;gap:10px;align-items:flex-start;background:var(--amber-bg);
  border:1px solid var(--amber-line);border-radius:12px;padding:12px;margin-bottom:16px;
  color:var(--amber-ink);font-size:13.5px;line-height:1.6}
.banner .ic{flex:none}
.banner .close{flex:none;border:0;background:none;color:var(--amber-ink);font-size:15px;
  cursor:pointer;padding:1px 4px;opacity:.65}
.day-banner{margin:8px 0;padding:8px 10px;font-size:12.5px}
.banner.review-passed{background:#eef6ee;border-color:#bfd7bf;color:#315d3b}
.banner.review-passed .close{color:#315d3b}

.route-tabs{display:flex;background:#e8e1d2;border-radius:12px;padding:3px;gap:3px;margin-bottom:12px}
.route-tabs button{flex:1;border:0;background:none;padding:9px 8px;border-radius:9px;
  font-size:14px;font-weight:600;color:var(--ink2);cursor:pointer;min-height:40px}
.route-tabs button.on{background:var(--card);color:var(--ink);box-shadow:0 1px 3px rgba(29,26,22,.14)}
.route-card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--tc,var(--cinnabar));
  border-radius:12px;padding:14px 16px;margin-bottom:6px}
.route-name{font-family:var(--serif);font-size:18px;font-weight:700;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.rec-badge{font-family:var(--sans);font-size:11px;font-weight:700;color:#fffdf8;background:var(--cinnabar);
  border-radius:6px;padding:2px 7px;letter-spacing:.08em}
.route-meta{font-size:12.5px;color:var(--ink3);margin:4px 0 6px}
.route-desc{font-size:13.5px;color:var(--ink2);line-height:1.7;margin-top:6px}
.route-desc b{color:var(--ink);font-weight:600;margin-right:8px}

.tree-sticky{position:sticky;top:0;z-index:50;margin:14px -18px 0;padding:10px 18px 6px;
  background:rgba(244,239,230,.94);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line)}
.progress-row{display:flex;justify-content:space-between;align-items:baseline;gap:10px;min-height:20px}
.progress-row .now{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.progress-row .count{flex:none;font-size:12.5px;color:var(--ink2);font-variant-numeric:tabular-nums}
.strip{display:flex;overflow-x:auto;padding:10px 2px 6px;scrollbar-width:none}
.strip::-webkit-scrollbar{display:none}
.chip{position:relative;flex:none;width:44px;height:40px;display:flex;align-items:center;justify-content:center;
  background:none;border:0;cursor:pointer}
.chip .n{width:26px;height:26px;border-radius:50%;background:var(--card);border:1.5px solid var(--line2);
  color:var(--ink2);font-size:13px;font-weight:600;display:flex;align-items:center;justify-content:center;transition:all .2s}
.chip::after{content:"";position:absolute;right:-9px;top:50%;transform:translateY(-50%);
  width:18px;height:1.5px;background:#d8d0bd}
.chip:last-child::after{display:none}
.chip.done .n{background:var(--done);border-color:var(--done);color:#fff}
.chip.done::after{background:var(--done-line)}
.chip.current .n{border-color:var(--tc,var(--cinnabar));color:var(--tc,var(--cinnabar));
  box-shadow:0 0 0 3px rgba(180,67,46,.15)}
.chip.current.done .n{color:#fff;border-color:var(--done);box-shadow:0 0 0 3px rgba(46,125,78,.16)}

.t-cards{padding-top:22px}
.t-card{position:relative;padding:0 0 22px 46px;scroll-margin-top:var(--sticky-h)}
.t-card::before{content:"";position:absolute;left:15px;top:42px;bottom:0;width:2px;background:var(--line)}
.t-card:last-child::before{display:none}
.t-dot{position:absolute;left:0;top:2px;width:32px;height:32px;border-radius:50%;
  background:var(--tc,var(--cinnabar));color:#fffdf8;font-weight:700;font-size:15px;
  display:flex;align-items:center;justify-content:center;box-shadow:0 2px 6px rgba(29,26,22,.25)}
.t-dot .ck{display:none}
.t-card.done .t-dot{background:var(--done)}
.t-card.done .t-dot .num{display:none}
.t-card.done .t-dot .ck{display:inline}
.t-inner{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px;box-shadow:0 1px 3px rgba(29,26,22,.05)}
.t-top{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.t-title{font-size:19px;font-weight:700;line-height:1.4;min-width:0;flex:1 1 220px;overflow-wrap:anywhere}
.t-dur{flex:none;font-size:12px;font-weight:600;color:var(--tc,var(--cinnabar));
  background:var(--paper2);padding:4px 9px;border-radius:999px;white-space:normal;max-width:100%;line-height:1.45;margin-top:3px}
.t-card.done .t-dur{color:var(--done);background:var(--done-soft)}
.img-ph{aspect-ratio:16/9;border:1.5px dashed #d3c9b4;border-radius:10px;
  background:linear-gradient(135deg,#f2ede2,#e9e2d2);
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;
  color:var(--ink3);padding:14px;text-align:center}
.img-ph svg{opacity:.75}
.img-ph .tag{font-size:12px;font-weight:600;letter-spacing:.08em}
.img-ph .prompt{font-size:11px;line-height:1.55;color:#b3a991;max-width:92%;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.t-photo{position:relative;aspect-ratio:16/9;border-radius:10px;overflow:hidden}
.t-photo img{width:100%;height:100%;object-fit:cover;display:block}
.t-photo-credit{position:absolute;right:8px;bottom:6px;font-size:9px;line-height:1.3;color:rgba(255,253,248,.68);
  background:rgba(0,0,0,.16);padding:1px 4px;border-radius:4px;opacity:.62;backdrop-filter:blur(2px);pointer-events:none}
.t-hl{font-size:15.5px;line-height:1.78;color:#3a372f;margin:14px 2px}
.arrive{width:100%;min-height:48px;border:0;border-radius:12px;cursor:pointer;
  background:var(--tc,var(--cinnabar));color:#fffdf8;font-size:16px;font-weight:600;
  transition:transform .1s,background .2s}
.arrive:active{transform:scale(.985)}
.t-card.done .arrive{background:var(--done-soft);color:var(--done);border:1px solid var(--done-line)}

.tree-foot{margin-top:28px;padding-top:18px;border-top:1px solid var(--line);
  display:flex;flex-direction:column;gap:10px;align-items:center}
.link-btn{border:0;background:none;color:var(--ink3);font-size:12px;cursor:pointer;
  text-decoration:underline;text-underline-offset:3px}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));z-index:400;
  transform:translateX(-50%) translateY(16px);background:var(--ink);color:var(--paper);
  padding:10px 18px;border-radius:999px;font-size:14px;opacity:0;pointer-events:none;
  transition:all .3s;white-space:nowrap}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

@media (max-width:600px){
  .tree-hero.has-photo{aspect-ratio:auto}
  .tree-body{padding-left:14px;padding-right:14px}
  .tree-sticky{margin-left:-14px;margin-right:-14px;padding-left:14px;padding-right:14px}
  .t-card{padding-left:40px}
  .t-inner{padding:14px}
  .t-top{display:block}
  .t-title{display:block;font-size:18px;line-height:1.45;margin-bottom:8px}
  .t-dur{display:inline-block;font-size:11.5px}
  .note-form-fab{right:12px;bottom:calc(12px + env(safe-area-inset-bottom));padding:10px 14px;font-size:12.5px}
}

@media (prefers-reduced-motion: reduce){
  html{scroll-behavior:auto}
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
}
"""

# Day详情页CSS（2026-08-16接入，见本文件模块docstring"Day详情页"一节）。App行程单
# 视觉风格，复用_TREE_STYLE已经定义好的:root变量（--paper/--ink/--d1~d8/--serif/
# --sans/--radius/--amber-*等），不重复定义。.land-mini是.land（森林页天数长卷用的
# 完整地标卡）的紧凑版，同一套"深度玩法"视觉语言（印章+可点击跳树木页），只是
# Day详情页空间更紧凑不需要那么大的卡片。
_DAY_STYLE = """
.view-day{max-width:640px;margin:0 auto;padding:22px 18px calc(40px + env(safe-area-inset-bottom))}

.topnav{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;font-size:13px}
.back-btn-flat{border:0;background:none;color:var(--ink3);font-size:13px;cursor:pointer;padding:0;min-height:32px}
.daynav{display:flex;align-items:center;gap:14px;color:var(--ink2);font-size:13px}
.daynav a{color:var(--dc,var(--cinnabar));font-weight:600}
.daynav .disabled{color:var(--ink3)}

.head-card{background:var(--dc,var(--d1));color:#fffdf8;border-radius:var(--radius);
  padding:16px 18px;margin-bottom:14px;box-shadow:0 2px 8px rgba(29,26,22,.18)}
.head-card .day-chip{display:inline-block;font-size:11px;letter-spacing:.15em;
  background:rgba(255,253,248,.18);padding:3px 9px;border-radius:999px;margin-bottom:8px}
.head-card h1{font-family:var(--serif);font-size:20px;margin:0 0 4px;font-weight:700}
.head-card .sub{font-size:12.5px;opacity:.85;line-height:1.6}

.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 16px;margin-bottom:12px}
.card h2{font-size:14px;font-weight:700;margin:0 0 10px;display:flex;
  align-items:center;gap:6px;color:var(--ink)}
.card h2 .ic{font-size:15px}
.card-body{font-size:13.5px;color:var(--ink2);line-height:1.75}
.card-body p{margin:0 0 8px}
.card-body p:last-child{margin-bottom:0}
.card-body ul{margin:6px 0 10px;padding-left:22px;list-style:none}
.card-body ul li{position:relative;padding-left:6px;margin-bottom:7px}
.card-body ul li::before{content:"";position:absolute;left:-12px;top:10px;width:5px;height:5px;
  border-radius:50%;background:var(--dc,var(--cinnabar));opacity:.7}
.card-body ol{margin:0;padding:0;list-style:none}
.card-body ol li{position:relative;padding-left:18px}
.card-body ol li::before{content:"";position:absolute;left:2px;top:8px;width:6px;height:6px;
  border-radius:50%;background:var(--cinnabar)}
.card-body li{margin-bottom:8px}
.card-body li:last-child{margin-bottom:0}
.card-body strong{color:var(--ink)}
.card-body table{border-collapse:collapse;font-size:12.5px;margin:4px 0}
.card-body th, .card-body td{border:1px solid var(--line);padding:6px 8px;text-align:left}
.card-body th{background:var(--paper2)}
.card h3{font-family:var(--serif);font-size:13px;margin:14px 0 6px;color:var(--dc,var(--cinnabar))}
.card h3:first-child{margin-top:0}

.timeline{position:relative;padding-left:18px}
.timeline::before{content:"";position:absolute;left:4px;top:4px;bottom:4px;width:1.5px;background:var(--line2)}
.t-row{position:relative;padding-bottom:12px;font-size:13.5px;color:var(--ink2)}
.t-row:last-child{padding-bottom:0}
.t-row::before{content:"";position:absolute;left:-18px;top:4px;width:7px;height:7px;
  border-radius:50%;background:var(--cinnabar);border:2px solid var(--card)}
.t-row .clock-time{font-weight:700;font-variant-numeric:tabular-nums;margin-right:8px;color:var(--cinnabar)}

.land-mini{display:block;position:relative;text-decoration:none;color:inherit;cursor:pointer;
  background:var(--cinnabar-soft);border:1.5px solid var(--ink);border-radius:11px;
  padding:12px 14px;margin-bottom:10px}
.land-mini .top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px}
.land-mini b{font-family:var(--serif);font-size:15.5px}
.land-mini .stamp-mini{flex:none;font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--cinnabar);
  border:1.5px solid currentColor;border-radius:4px;padding:1px 6px;white-space:nowrap}
.land-mini p{margin:0;font-size:12.5px;color:var(--ink2);line-height:1.6}

.poi-item{font-size:13.5px;margin-bottom:10px}
.poi-item:last-child{margin-bottom:0}
.poi-item b{display:block;margin-bottom:2px}
.poi-item p{margin:0;color:var(--ink2);line-height:1.6}

.warn-chip{display:flex;gap:6px;background:var(--amber-bg);border:1px solid var(--amber-line);
  color:var(--amber-ink);border-radius:9px;padding:8px 10px;font-size:12.5px;line-height:1.6;margin-bottom:10px}
"""

_NOTE_FORM_STYLE = """
.site-toolbar{position:sticky;top:0;z-index:50;background:var(--paper2,#f4efe6);
  border-bottom:1px solid var(--line);padding:6px 14px;text-align:right;font-size:12.5px}
.site-toolbar a{color:var(--ink2)}
.note-form-fab{position:fixed;right:18px;bottom:calc(18px + env(safe-area-inset-bottom));
  z-index:60;background:var(--cinnabar);color:#fffdf8;border:none;border-radius:999px;
  padding:12px 18px;font-size:13px;font-weight:600;box-shadow:0 4px 14px rgba(29,26,22,.28);cursor:pointer}
.note-form-overlay{position:fixed;inset:0;z-index:70;background:rgba(20,17,14,.5);
  display:flex;align-items:flex-end;justify-content:center}
.note-form-overlay[hidden]{display:none}
.note-form-panel{background:var(--paper,#fdf9f2);width:100%;max-width:480px;
  border-radius:16px 16px 0 0;padding:18px 20px calc(20px + env(safe-area-inset-bottom));
  max-height:80vh;overflow-y:auto}
.note-form-panel h2{font-family:var(--serif);font-size:17px;margin-bottom:12px}
.note-form-panel label{display:block;font-size:12.5px;color:var(--ink2);margin:10px 0 4px}
.note-form-panel select, .note-form-panel textarea{width:100%;border:1px solid var(--line);
  border-radius:8px;padding:8px 10px;font-size:13.5px;font-family:inherit;background:var(--card)}
.note-form-panel textarea{min-height:80px;resize:vertical}
.note-form-actions{display:flex;gap:10px;margin-top:14px}
.note-form-actions button{flex:1;border-radius:8px;padding:10px;font-size:13.5px;cursor:pointer}
.note-form-submit{background:var(--cinnabar);color:#fffdf8;border:none;font-weight:600}
.note-form-cancel{background:none;border:1px solid var(--line);color:var(--ink2)}
"""


def _render_user_note_form(route_plan, project_id: str) -> str:
    """网页内嵌的用户备注表单（阶段1唯一写入入口，见docs/ARCHITECTURE.md第21.3/21.7节）：
    day下拉(从route_plan的days填)+poi_name下拉(从route_plan的pois填，随day联动)+
    note_text文本框+提交按钮。提交fetch POST到/api/projects/{id}/user_notes，成功后
    location.reload()刷新——site.html本身是纯静态副本，表单提交依赖src/api后端在运行，
    见ARCHITECTURE.md第21.4节"网页/后台是结构化字段"一节。
    """
    days_payload = []
    if route_plan:
        for day in route_plan.days:
            days_payload.append({
                "day": day.day,
                "label": f"Day {day.day}" + (f" · {day.city}" if day.city else ""),
                "pois": [poi.name for poi in day.pois],
            })
    days_json = _dumps_for_script(days_payload)
    project_id_json = _dumps_for_script(project_id)
    return f"""<button class="note-form-fab" id="note-form-open">📝 加备注</button>
<div class="note-form-overlay" id="note-form-overlay" hidden>
  <div class="note-form-panel">
    <h2>添加用户备注</h2>
    <p style="font-size:12px;color:var(--ink3)">备注是你自己的旁注，不经过系统核验，不会改写攻略正文。</p>
    <form id="note-form">
      <label>绑定天数（可选）</label>
      <select id="note-form-day"><option value="">不绑定（行程级备注）</option></select>
      <label>绑定景点（可选，先选天数）</label>
      <select id="note-form-poi"><option value="">不绑定</option></select>
      <label>备注内容</label>
      <textarea id="note-form-text" required placeholder="比如：这家火锅鱼要提前订"></textarea>
      <div class="note-form-actions">
        <button type="button" class="note-form-cancel" id="note-form-cancel">取消</button>
        <button type="submit" class="note-form-submit">提交</button>
      </div>
    </form>
  </div>
</div>
<script>
(function(){{
  var DAYS = {days_json};
  var PROJECT_ID = {project_id_json};
  var daySelect = document.getElementById('note-form-day');
  var poiSelect = document.getElementById('note-form-poi');
  DAYS.forEach(function(d){{
    var opt = document.createElement('option');
    opt.value = d.day; opt.textContent = d.label;
    daySelect.appendChild(opt);
  }});
  daySelect.addEventListener('change', function(){{
    poiSelect.innerHTML = '<option value="">不绑定</option>';
    var d = DAYS.find(function(x){{ return String(x.day) === daySelect.value; }});
    if (!d) return;
    d.pois.forEach(function(name){{
      var opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      poiSelect.appendChild(opt);
    }});
  }});
  var overlay = document.getElementById('note-form-overlay');
  document.getElementById('note-form-open').onclick = function(){{ overlay.hidden = false; }};
  document.getElementById('note-form-cancel').onclick = function(){{ overlay.hidden = true; }};
  document.getElementById('note-form').addEventListener('submit', function(e){{
    e.preventDefault();
    var body = {{
      note_text: document.getElementById('note-form-text').value,
      day: daySelect.value ? parseInt(daySelect.value, 10) : null,
      poi_name: poiSelect.value || '',
    }};
    var apiBase = '/api/projects/' + encodeURIComponent(PROJECT_ID);
    function pollUntilDone(){{
      return fetch(apiBase + '/generate_site/status').then(function(r){{ return r.json(); }}).then(function(s){{
        if (s && s.status === 'running') {{
          return new Promise(function(resolve){{ setTimeout(resolve, 800); }}).then(pollUntilDone);
        }}
        return s;
      }});
    }}
    fetch(apiBase + '/user_notes', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }}).then(function(r){{
      if (!r.ok) throw new Error('提交失败');
      return fetch(apiBase + '/generate_site', {{ method: 'POST' }});
    }}).then(function(){{
      return pollUntilDone();
    }}).then(function(){{
      location.reload();
    }}).catch(function(err){{
      alert('备注提交失败：' + err.message);
    }});
  }});
}})();
</script>"""


_POSTER_LIGHTBOX = """<div class="poster-lightbox" id="poster-lightbox" hidden>
  <div class="poster-lightbox-backdrop" data-poster-close></div>
  <div class="poster-lightbox-panel" role="dialog" aria-modal="true" aria-labelledby="poster-lightbox-title">
    <div class="poster-lightbox-head">
      <strong id="poster-lightbox-title">海报预览</strong>
      <button type="button" data-poster-close>关闭</button>
    </div>
    <img class="poster-lightbox-image" id="poster-lightbox-image" alt="海报预览">
    <div class="poster-lightbox-actions" id="poster-lightbox-actions">
      <span id="poster-navigation">
        <button type="button" id="poster-prev">上一张</button>
        <button type="button" id="poster-next">下一张</button>
      </span>
      <a id="poster-download" download>下载原图</a>
    </div>
  </div>
</div>"""


_POSTER_SCRIPT = """<script>
(function(){
  const box = document.getElementById('poster-lightbox');
  const image = document.getElementById('poster-lightbox-image');
  const title = document.getElementById('poster-lightbox-title');
  const navigation = document.getElementById('poster-navigation');
  const prev = document.getElementById('poster-prev');
  const next = document.getElementById('poster-next');
  const download = document.getElementById('poster-download');
  if (!box || !image) return;
  const dayKeys = Array.from(new Set(
    Array.from(document.querySelectorAll('[data-poster^="day"]'), btn => btn.dataset.poster)
  ));
  let current = 'overall';
  const assetUrl = key => key === 'overall'
    ? 'site_assets/posters/overall.png'
    : `site_assets/posters/${key}.png`;
  const assetTitle = key => key === 'overall' ? '总览海报' : `Day${key.slice(3)} 海报`;
  function render(key){
    current = key;
    const url = assetUrl(key);
    title.textContent = assetTitle(key);
    image.src = url;
    image.alt = assetTitle(key);
    download.href = url;
    download.download = `${key}.png`;
    navigation.hidden = key === 'overall';
    if (key !== 'overall'){
      const idx = dayKeys.indexOf(key);
      prev.disabled = idx <= 0;
      next.disabled = idx >= dayKeys.length - 1;
    }
    box.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function close(){ box.hidden = true; image.removeAttribute('src'); document.body.style.overflow = ''; }
  document.querySelectorAll('[data-poster]').forEach(btn => btn.addEventListener('click', () => render(btn.dataset.poster)));
  document.querySelectorAll('[data-poster-close]').forEach(btn => btn.addEventListener('click', close));
  prev.addEventListener('click', () => { const i = dayKeys.indexOf(current); if (i > 0) render(dayKeys[i - 1]); });
  next.addEventListener('click', () => { const i = dayKeys.indexOf(current); if (i < dayKeys.length - 1) render(dayKeys[i + 1]); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !box.hidden) close(); });
})();
</script>"""


_TREE_SHELL = """<div id="wipe" class="wipe" aria-hidden="true"><span class="wipe-word" id="wipe-word"></span></div>

<div id="view-tree" class="view" hidden>
  <header class="tree-hero" id="tree-hero">
    <div class="hero-top">
      <button class="back-btn" id="back-forest">← 返回森林</button>
      <span class="hero-crumb" id="tree-crumb"></span>
    </div>
    <h1 id="tree-name"></h1>
    <div class="hero-meta">
      <span id="tree-meta"></span>
      <button class="reopen" id="tree-banner-reopen" hidden>ⓘ 内容说明</button>
    </div>
    <span class="hero-glyph" id="tree-glyph" aria-hidden="true"></span>
  </header>

  <div class="tree-body">
    <div class="banner" id="tree-banner" hidden>
      <span class="ic">ⓘ</span>
      <span id="tree-banner-text">本页部分内容保留历史核验未完成状态，本次未重新调用模型，不代表已确认错误；仅供参考，建议以景区现场公示信息为准。</span>
      <button class="close" id="tree-banner-close" aria-label="收起提示">✕</button>
    </div>

    <section id="tree-route-slot"></section>
    <section id="tree-user-notes-slot"></section>

    <div class="tree-sticky">
      <div class="progress-row">
        <span class="now" id="tree-now"></span>
        <span class="count" id="tree-count"></span>
      </div>
      <div class="strip" id="tree-strip"></div>
    </div>

    <main class="t-cards" id="tree-cards"></main>

    <footer class="tree-foot">
      <button class="link-btn" id="tree-reset">重置打卡记录</button>
      <button class="link-btn" id="back-forest-2">← 返回森林总览</button>
    </footer>
  </div>
</div>

<div class="toast" id="toast"></div>
"""

# 移植自design v3的Module A（路由/转场）+ Module B（树木详情渲染），去掉了森林页
# 天数长卷渲染(renderForest)，findPoiCtx改查__TREE_INDEX_JSON__（Python侧算好，
# 不再依赖完整FOREST.days结构）。用占位符字符串替换插值，不用f-string整体格式化，
# 避免大段JS里几百个花括号要手动转义成{{}}的隐蔽出错风险。
_TREE_SCRIPT = """<script>
const $ = (s, el=document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let toastTimer;
function toast(msg){
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
}

const TRIP_TITLE = __TRIP_TITLE_JSON__;
const TREE_INDEX = __TREE_INDEX_JSON__;
window.TREES = __TREES_JSON__;
const TREE_NOTES = __TREE_NOTES_JSON__;

function renderTreeUserNotes(name){
  const slot = $('#tree-user-notes-slot');
  const notes = TREE_NOTES[name] || [];
  if (!notes.length){ slot.innerHTML = ''; return; }
  const items = notes.map(n => `<div class="user-note-item"><p>${esc(n.note_text)}</p>
    <span class="user-note-meta">${esc((n.created_at || '').slice(0, 16).replace('T', ' '))}</span></div>`).join('');
  slot.innerHTML = `<div class="user-notes-block"><div class="user-notes-badge">📝 用户备注</div>${items}</div>`;
}

function themeIdxOf(day){ return (((day || 1) - 1) % 8) + 1; }
function themeColorOf(day){ return `var(--d${themeIdxOf(day)})`; }

function treeUrl(name){ return '#/tree/' + encodeURIComponent(name); }
function findPoiCtx(name){
  const meta = TREE_INDEX[name];
  if (!meta) return null;
  return { name, day: meta.day, city: meta.city, area: meta.area, brief: meta.brief || '' };
}

function renderTreeShell(ctx){
  const view = $('#view-tree');
  const hero = $('#tree-hero');
  view.style.setProperty('--tc', themeColorOf(ctx.day));
  $('#tree-crumb').textContent = `${TRIP_TITLE} · Day ${ctx.day}${ctx.area ? ' · ' + ctx.area : ''}`;
  $('#tree-name').textContent  = ctx.name;
  $('#tree-glyph').textContent = ctx.name.charAt(0);
  document.title = `${ctx.name} · ${TRIP_TITLE}`;

  const detail = window.TREES[ctx.name];
  const photo = detail && detail.overview_photo_url;
  hero.classList.toggle('has-photo', !!photo);
  hero.style.backgroundImage = photo ? `url("${photo}")` : '';
  let credit = $('#tree-hero-photo-credit');
  if (photo){
    if (!credit){
      credit = document.createElement('div');
      credit.id = 'tree-hero-photo-credit';
      credit.className = 'hero-photo-credit';
      $('.hero-meta', hero).after(credit);
    }
    credit.textContent = detail.overview_photo_credit || '图片来源：高德地图';
  } else if (credit){
    credit.remove();
  }

  $('#tree-banner').hidden = true;
  $('#tree-banner-reopen').hidden = true;
  renderTreeUserNotes(ctx.name);

  if (typeof window.renderTreeDetail === 'function'){
    window.renderTreeDetail(ctx);
  }
}

const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
let currentKey = null;
let forestScroll = 0;
let navSeq = 0;
let treeReturnTarget = '#/';

function parseHash(){
  const dayMatch = location.hash.match(/^#\\/day\\/(\\d+)$/);
  if (dayMatch) return { view:'day', day: parseInt(dayMatch[1], 10) };
  if (location.hash === '#/guide') return { view:'guide' };
  const m = location.hash.match(/^#\\/tree\\/([^?]+)(?:\\?from=([^&]+))?$/);
  if (m){
    try {
      const name = decodeURIComponent(m[1]);
      const returnTarget = (m[2] && m[2].indexOf('day-') === 0) ? '#/day/' + m[2].slice(4) : '#/';
      return { view:'tree', name, returnTarget };
    } catch(e){}
  }
  return { view:'forest' };
}

function routeKey(route){
  if (route.view === 'tree') return 'tree:' + route.name;
  if (route.view === 'day') return 'day:' + route.day;
  if (route.view === 'guide') return 'guide';
  return 'forest';
}

function replayIn(el){ el.classList.remove('view-in'); void el.offsetWidth; el.classList.add('view-in'); }

function applyRoute(route){
  if (route.view === 'tree' && !findPoiCtx(route.name)){
    history.replaceState(null, '', '#/');
    toast('未找到该景点的深度玩法');
    route = { view:'forest' };
  }
  if (route.view === 'day' && !$('#day-page-' + route.day)){
    history.replaceState(null, '', '#/');
    toast('未找到这一天的详情');
    route = { view:'forest' };
  }

  const forestEl = $('#view-forest'), treeEl = $('#view-tree'), guideEl = $('#view-guide');
  forestEl.hidden = true; treeEl.hidden = true;
  if (guideEl) guideEl.hidden = true;
  document.querySelectorAll('.view-day').forEach(el => { el.hidden = true; });

  if (route.view === 'tree'){
    treeReturnTarget = route.returnTarget || '#/';
    renderTreeShell(findPoiCtx(route.name));
    treeEl.hidden = false;
    window.scrollTo(0, 0);
    replayIn(treeEl);
  } else if (route.view === 'day'){
    const dayEl = $('#day-page-' + route.day);
    dayEl.hidden = false;
    document.title = `Day ${route.day} · ${TRIP_TITLE}`;
    window.scrollTo(0, 0);
    replayIn(dayEl);
  } else if (route.view === 'guide'){
    if (guideEl){ guideEl.hidden = false; replayIn(guideEl); }
    document.title = `完整攻略正文 · ${TRIP_TITLE}`;
    window.scrollTo(0, 0);
  } else {
    document.title = `${TRIP_TITLE} · 行程总览`;
    forestEl.hidden = false;
    window.scrollTo(0, forestScroll);
    replayIn(forestEl);
  }
  currentKey = routeKey(route);
}

function transitionTo(route){
  const key = routeKey(route);
  if (key === currentKey) return;
  if (currentKey === 'forest') forestScroll = window.scrollY;
  const invalid = (route.view === 'tree' && !findPoiCtx(route.name)) ||
    (route.view === 'day' && !$('#day-page-' + route.day));
  if (REDUCED || invalid){ applyRoute(route); return; }

  const seq = ++navSeq;
  const wipe = $('#wipe'), word = $('#wipe-word');
  let wc = '#253049', label = TRIP_TITLE;
  if (route.view === 'tree'){ wc = themeColorOf(findPoiCtx(route.name).day); label = route.name; }
  else if (route.view === 'day'){ wc = themeColorOf(route.day); label = `Day ${route.day}`; }
  else if (route.view === 'guide'){ label = '完整攻略正文'; }
  wipe.style.setProperty('--wc', wc);
  word.textContent = label;

  wipe.classList.remove('cover-off'); void wipe.offsetWidth;
  wipe.classList.add('cover-on');
  setTimeout(() => {
    if (seq !== navSeq) return;
    applyRoute(route);
    wipe.classList.remove('cover-on');
    wipe.classList.add('cover-off');
    setTimeout(() => { if (seq === navSeq) wipe.classList.remove('cover-off'); }, 520);
  }, 520);
}

function bindStatic(){
  const backToForest = () => { if (location.hash && location.hash !== '#/') location.hash = '#/'; };
  const backFromTree = () => { location.hash = treeReturnTarget || '#/'; };
  $('#back-forest').onclick = backFromTree;
  $('#back-forest-2').onclick = backFromTree;
  const guideBack = $('#back-forest-guide');
  if (guideBack) guideBack.onclick = backToForest;
  document.querySelectorAll('[data-back-forest]').forEach(btn => { btn.onclick = backToForest; });
  $('#tree-reset').onclick = () => { if (tCtx && tData) { tVisited.clear(); lsSet(lsVisit(), []); syncTreeUI(); toast('打卡记录已重置'); } };
}

window.addEventListener('hashchange', () => transitionTo(parseHash()));
bindStatic();
document.title = `${TRIP_TITLE} · 行程总览`;

/* ---------- 树木详情渲染（打卡状态用localStorage持久化，纯前端行为） ---------- */
function lsGet(k, d){ try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch(e){ return d; } }
function lsSet(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){} }
function fmtDur(segs){
  let m = 0, ok = false;
  for (const s of segs){ const v = parseInt(s.suggested_duration, 10); if(!isNaN(v)){ m += v; ok = true; } }
  if (!ok) return '';
  if (m < 60) return `约 ${m} 分钟`;
  const h = Math.floor(m/60), r = m%60;
  return r ? `约 ${h} 小时 ${r} 分钟` : `约 ${h} 小时`;
}

let tCtx = null, tData = null, tRoutes = [], tRouteIdx = 0;
let tVisited = new Set(), bannerOff = false, tCurrent = null, tIO = null;
const keyOfT = s => `${tRouteIdx}:${s.order}`;
const lsVisit  = () => `poi:visited:${tCtx.name}`;
const lsBanner = () => `poi:banner:${tCtx.name}`;

function renderTRouteArea(){
  const slot = $('#tree-route-slot');
  const r = tRoutes[tRouteIdx];
  const tabsHtml = tRoutes.length >= 2
    ? `<div class="route-tabs">${tRoutes.map((x,i) =>
        `<button data-i="${i}" class="${i === tRouteIdx ? 'on' : ''}">${esc(x.route_name || `路线 ${i+1}`)}</button>`).join('')}</div>`
    : '';
  slot.innerHTML = tabsHtml + `
    <div class="route-card">
      <div class="route-name">${esc(r.route_name || '游览路线')}
        ${r.recommended ? '<span class="rec-badge">⭐ 推荐路线</span>' : ''}</div>
      <div class="route-meta">${r.segments.length} 站 · 全程 ${fmtDur(r.segments)}</div>
      ${r.suitable_for     ? `<p class="route-desc"><b>适合人群</b>${esc(r.suitable_for)}</p>` : ''}
      ${r.recommend_reason ? `<p class="route-desc"><b>推荐理由</b>${esc(r.recommend_reason)}</p>` : ''}
    </div>`;
  slot.querySelectorAll('.route-tabs button').forEach(b => {
    b.onclick = () => {
      const i = +b.dataset.i; if (i === tRouteIdx) return;
      tRouteIdx = i; tCurrent = null;
      renderTRouteArea(); renderTStrip(); renderTCards(); syncTreeUI();
      $('#tree-route-slot').scrollIntoView({behavior: REDUCED ? 'auto' : 'smooth'});
    };
  });
  $('#tree-meta').textContent = `${tRoutes.length} 条路线 · 本线 ${r.segments.length} 站 · 全程 ${fmtDur(r.segments)}`;
}

const IMG_ICON = `<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.5"/><path d="M3.5 17.5 8.5 13l3.5 3 3.5-3.5 4.5 4.5"/></svg>`;

function renderTCards(){
  const wrap = $('#tree-cards'); wrap.innerHTML = '';
  tRoutes[tRouteIdx].segments.forEach((s, i) => {
    const key = keyOfT(s);
    const art = document.createElement('article');
    art.className = 't-card';
    art.id = `t-card-${tRouteIdx}-${s.order}`;
    art.dataset.key = key;
    art.innerHTML = `
      <div class="t-dot"><span class="num">${i+1}</span><span class="ck">✓</span></div>
      <div class="t-inner">
        <div class="t-top">
          <h2 class="t-title">${esc(s.point_name)}</h2>
          ${s.suggested_duration ? `<span class="t-dur">⏱ ${esc(s.suggested_duration)}</span>` : ''}
        </div>
        ${s.uploaded_photo_url ? `
        <div class="t-photo">
          <img src="${s.uploaded_photo_url}" alt="${esc(s.point_name)}" loading="lazy" decoding="async">
          <div class="t-photo-credit">${esc(s.photo_credit || '图片来源：用户上传')}</div>
        </div>` : `
        <div class="img-ph">
          ${IMG_ICON}
          <div class="tag">配图占位 · 16:9</div>
          ${s.image_prompt ? `<div class="prompt">${esc(s.image_prompt)}</div>` : ''}
        </div>`}
        <p class="t-hl">${esc(s.highlight)}</p>
        <button class="arrive"></button>
      </div>`;
    art.querySelector('.arrive').onclick = () => onArriveT(key);
    wrap.appendChild(art);
  });
  observeTCards();
}

function renderTStrip(){
  const strip = $('#tree-strip'); strip.innerHTML = '';
  tRoutes[tRouteIdx].segments.forEach((s, i) => {
    const key = keyOfT(s);
    const b = document.createElement('button');
    b.className = 'chip';
    b.dataset.key = key;
    b.title = s.point_name;
    b.innerHTML = `<span class="n">${i+1}</span>`;
    b.onclick = () => document.getElementById(`t-card-${tRouteIdx}-${s.order}`)
      ?.scrollIntoView({behavior: REDUCED ? 'auto' : 'smooth', block: 'start'});
    strip.appendChild(b);
  });
}

function observeTCards(){
  if (tIO) tIO.disconnect();
  tIO = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting){ tCurrent = e.target.dataset.key; syncTreeUI(); } });
  }, { rootMargin: '-30% 0px -55% 0px', threshold: 0 });
  document.querySelectorAll('.t-card').forEach(c => tIO.observe(c));
}

function syncTreeUI(){
  const segs = tRoutes[tRouteIdx].segments;
  const keys = segs.map(keyOfT);
  document.querySelectorAll('.t-card').forEach(c => {
    const done = tVisited.has(c.dataset.key);
    c.classList.toggle('done', done);
    c.querySelector('.arrive').textContent = done ? '已到达 ✓ · 点击撤销' : '✓ 我到了';
  });
  document.querySelectorAll('.chip').forEach(ch => {
    const k = ch.dataset.key;
    ch.classList.toggle('done', tVisited.has(k));
    const cur = k === tCurrent, was = ch.classList.contains('current');
    ch.classList.toggle('current', cur);
    if (cur && !was) ch.scrollIntoView({inline: 'center', block: 'nearest', behavior: REDUCED ? 'auto' : 'smooth'});
  });
  const doneN = keys.filter(k => tVisited.has(k)).length;
  const idx = tCurrent ? keys.indexOf(tCurrent) : 0;
  const seg = idx >= 0 ? segs[idx] : null;
  $('#tree-now').textContent = (keys.length && doneN === keys.length)
    ? '✓ 已完成本条路线全部游览'
    : (seg ? `第 ${idx+1} / ${keys.length} 站 · ${seg.point_name}` : '');
  $('#tree-count').textContent = `已打卡 ${doneN}/${keys.length}`;
}

function onArriveT(key){
  const marking = !tVisited.has(key);
  marking ? tVisited.add(key) : tVisited.delete(key);
  lsSet(lsVisit(), [...tVisited]);
  syncTreeUI();
  if (marking){
    const next = tRoutes[tRouteIdx].segments.map(keyOfT).find(k => !tVisited.has(k));
    if (next){
      const [ri, ord] = next.split(':');
      setTimeout(() => document.getElementById(`t-card-${ri}-${ord}`)
        ?.scrollIntoView({behavior: REDUCED ? 'auto' : 'smooth', block: 'start'}), 220);
    } else {
      toast('🎉 已完成本条路线的全部打卡');
    }
  }
}

function syncTreeBanner(){
  const reviewed = !!tData && tData.effective_review_status === 'review_passed';
  const need = !!tData && (tData.citation_status === 'force_finalized' || reviewed);
  $('#tree-banner').classList.toggle('review-passed', reviewed);
  $('#tree-banner-text').textContent = reviewed
    ? '本页原始生成状态保留为 force_finalized，但当前内容已经人工复核通过。'
    : '本页部分内容保留历史核验未完成状态，本次未重新调用模型，不代表已确认错误；仅供参考，建议以景区现场公示信息为准。';
  $('#tree-banner').hidden = !(need && !bannerOff);
  $('#tree-banner-reopen').hidden = !(need && bannerOff);
}

window.renderTreeDetail = function(ctx){
  tCtx = ctx;
  tData = window.TREES[ctx.name] || null;
  if (tIO){ tIO.disconnect(); tIO = null; }
  tCurrent = null;
  if (!tData || !Array.isArray(tData.routes) || !tData.routes.length){
    $('#tree-meta').textContent = '暂无路线数据';
    $('#tree-route-slot').innerHTML = `<div class="route-card"><p class="route-desc">该景点的深度路线尚未生成。</p></div>`;
    $('#tree-strip').innerHTML = ''; $('#tree-cards').innerHTML = '';
    $('#tree-now').textContent = ''; $('#tree-count').textContent = '';
    $('#tree-banner').hidden = true; $('#tree-banner-reopen').hidden = true;
    return;
  }
  tRoutes = tData.routes.map(r => ({
    ...r,
    segments: [...(r.route_segments || [])].sort((a, b) => a.order - b.order)
  }));
  tRouteIdx = Math.max(0, tRoutes.findIndex(r => r.recommended));
  tVisited  = new Set(lsGet(lsVisit(), []));
  bannerOff = lsGet(lsBanner(), false);
  renderTRouteArea(); renderTStrip(); renderTCards(); syncTreeBanner(); syncTreeUI();
};

$('#tree-banner-close').onclick  = () => { if (!tCtx) return; bannerOff = true;  lsSet(lsBanner(), true);  syncTreeBanner(); };
$('#tree-banner-reopen').onclick = () => { if (!tCtx) return; bannerOff = false; lsSet(lsBanner(), false); syncTreeBanner(); };

applyRoute(parseHash());
</script>
"""


def _copy_poster_assets(workspace_dir: Path, assets_dir: Path, project_id: str) -> set[str]:
    """Copy current generated posters, falling back to the project's imported root PNGs."""
    repo_root = Path(__file__).resolve().parents[2]
    poster_dir = assets_dir / "posters"
    poster_dir.mkdir(parents=True, exist_ok=True)
    copied: set[str] = set()
    sources = {"overall": workspace_dir / "poster.png"}
    for generated in workspace_dir.glob("poster-day*.png"):
        day = generated.stem.removeprefix("poster-day")
        if day.isdigit() and int(day) > 0:
            sources[f"day{int(day)}"] = generated
    if project_id == "custom-qinggan-linear-8d-v8-20260813":
        for day in range(1, 9):
            sources.setdefault(f"day{day}", workspace_dir / f"poster-day{day}.png")
    for key, generated in sources.items():
        source = generated
        if not source.is_file() and project_id == "custom-qinggan-linear-8d-v8-20260813":
            source = repo_root / ("整体.png" if key == "overall" else f"{key}.png")
        if source.is_file():
            shutil.copy2(source, poster_dir / f"{key}.png")
            copied.add(key)
    return copied


def generate_site(
    project_id: str,
    output_path: Optional[Path] = None,
    *,
    offline: bool = False,
    standalone: bool = False,
) -> Path:
    """Render one Project as HTML.

    ``offline=True`` keeps the deterministic rendering path but disables automatic
    geocoding and remote POI-photo lookup. User-uploaded local images remain usable.
    ``standalone=True`` also skips review/user-note synchronization and removes
    controls that require the FastAPI backend; it implies the offline fetch policy.
    """
    output_path = output_path or (store.workspace_dir(project_id) / "site.html")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    assets_dir = output_path.parent / "site_assets"
    poster_keys = _copy_poster_assets(output_path.parent, assets_dir, project_id)

    final_guide_md = store.read_workspace(project_id, "final_guide.md") or ""
    poi_details = _load_poi_details(project_id)
    review_projection = {} if standalone else _load_effective_review_projection(project_id)
    for detail in poi_details:
        key = f"poi_detail:{detail.get('day', 'unknown')}:{detail.get('poi_name') or detail.get('name') or ''}"
        projection = review_projection.get(key) or {}
        detail["effective_review_status"] = projection.get("effective_status") or ""
        detail["review_finding_id"] = projection.get("finding_id")
    external_fetches_enabled = not (offline or standalone)
    map_points = _collect_poi_coordinates(project_id) if external_fetches_enabled else []
    route_plan = store.read_route_plan(project_id)
    requirements = store.read_requirements(project_id)
    pitfall = store.read_pitfall_output(project_id)
    lifestyle = store.read_lifestyle_output(project_id)
    user_notes = [] if standalone else _load_user_notes(project_id)
    trip_title = (requirements or {}).get("topic") or "旅游攻略"

    _attach_poi_overview_photos(
        poi_details,
        trip_title,
        project_id,
        assets_dir,
        allow_remote=external_fetches_enabled,
    )
    _attach_segment_uploaded_photos(poi_details, project_id, assets_dir)

    tree_index = _build_tree_index(poi_details, route_plan)
    trees_payload = _build_trees_payload(poi_details)
    map_html = _render_map_section(map_points, offline=not external_fetches_enabled)

    notes_by_poi, notes_by_day, _trip_notes = _classify_user_notes(user_notes, route_plan)
    tree_notes_payload: dict[str, list[dict]] = {}
    for (_day, poi_name), notes in notes_by_poi.items():
        tree_notes_payload.setdefault(poi_name, []).extend(notes)

    num_days = len(route_plan.days) if route_plan else 0
    intro_md, _, appendix_md = _split_guide_by_day(final_guide_md, num_days)
    forest_html = _render_forest_section(
        intro_md, appendix_md, route_plan, trip_title, poi_details, tree_index, map_html,
        lifestyle=lifestyle, pitfall=pitfall, user_notes=user_notes, poster_keys=poster_keys,
        review_projection=review_projection,
    )
    day_pages_html = _render_all_day_pages(
        route_plan, final_guide_md, pitfall, tree_index, notes_by_poi, notes_by_day, poster_keys,
    )
    guide_view_html = _render_guide_view(final_guide_md)
    note_form_html = "" if standalone else _render_user_note_form(route_plan, project_id)
    toolbar_html = (
        ""
        if standalone
        else f'<div class="site-toolbar"><a href="/api/projects/{quote(project_id)}/site/download">⬇ 下载本页副本</a></div>'
    )

    tree_script = (
        _TREE_SCRIPT
        .replace("__TRIP_TITLE_JSON__", _dumps_for_script(trip_title))
        .replace("__TREE_INDEX_JSON__", _dumps_for_script(tree_index))
        .replace("__TREES_JSON__", _dumps_for_script(trees_payload))
        .replace("__TREE_NOTES_JSON__", _dumps_for_script(tree_notes_payload))
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(trip_title)}</title>
<style>{_STYLE}{_TREE_STYLE}{_DAY_STYLE}{_NOTE_FORM_STYLE}</style>
</head>
<body>
{toolbar_html}
<div id="view-forest" class="view">
{forest_html}
</div>
{day_pages_html}
{guide_view_html}
{_POSTER_LIGHTBOX}
{_TREE_SHELL}
{note_form_html}
{_POSTER_SCRIPT}
{tree_script}
</body>
</html>
"""

    output_path.write_text(html_doc, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="从现有Project生成静态攻略网站")
    parser.add_argument("project_id", nargs="?", default="chengdu-3d-e2e-v9")
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="禁用自动地理编码和远程POI图片查询",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="生成不读写复核/备注状态且不依赖后端控件的静态页面（同时禁用外部查询）",
    )
    args = parser.parse_args()
    path = generate_site(
        args.project_id,
        args.output,
        offline=args.offline,
        standalone=args.standalone,
    )
    print(f"已生成静态网站: {path}")
