"""生成一趟旅程的整体概览海报或单日攻略海报：读route_plan.json/outputs/lifestyle.json/outputs/pitfall.json，
纯代码规则筛选已核验过的内容，拼成一段"如实转录"prompt，调用gpt-image-2（经cun.ai中转）
一次性生成整张信息图海报。

用法：
  python -m scripts.delivery.run_generate_poster <project_id>

方案来自2026-07-24 /grilling讨论，关键决策：
- 不做联网搜图，直接AI一次性生成整张海报（不是程序拼版+嵌入配图），因为参考案例证明
  当前生图模型（gpt-image-2）中文文字渲染已经足够可靠。
- 海报文字内容必须是已核验过的真实数据"如实转录"，不许模型自由编造清单之外的内容——
  这道筛选/拼prompt工序全程用纯代码规则完成（本文件的build_*/select_*函数），不额外调
  LLM摘要，避免引入新的幻觉源；模型只负责排版/配色/插画风格的自由发挥。
- 一次旅程只出一张海报，但内部按天整合所有天数的路线/避坑信息（不逐天/逐POI出图）。
- 手动CLI触发，不接入LangGraph状态机自动化，跟`scripts.delivery.run_generate_site`同款呈现层工程节奏；
  调用前需要用户在命令行确认（这是按次付费的API调用，避免误触发消耗额度）。
  失败自动重试一次，仍失败则报错退出，不做更多次重试。
- 输出直接写到workspace_dir(project_id)/poster.png，不经过store.py写入权限校验
  （呈现层产物，跟site.html同款处理，坐标/图片数据只在呈现层流转不进产出区）；
  同目录存一份实际喂给模型的prompt文本(poster_prompt.txt)方便日后核对内容有没有被
  模型篡改——这个项目一直靠这类"留痕"机制支撑可追溯性。

已知局限：
- 2026-07-24实测两次1024x1536竖版请求都触发cun.ai网关约60秒的隐性504超时，当时改用
  1024x1024方形规避。**2026-07-26复测**：3次全新真实调用（不同project/不同时间）竖版
  请求全部在33-46秒内成功，没有再触发504——判断当时是偶发网关问题，不是稳定复现的
  硬限制，因此改回1024x1536竖版默认输出，更贴近参考案例的长图比例。真实数据（含具体
  店名、用户负面评价原文）测试未观察到被内容/风控拦截，`sandbox/kimi_design_chat/`那条
  "真实数据会被硬拦截"的警告经测试确认不适用于生图接口。
- **2026-07-26第一次修复（不够彻底）**：`select_pitfall_lines`/`_shorten`截断避雷内容时，
  没有过滤避雷agent习惯写的框架性开场白，新增`_strip_meta_preamble`剥离+悬空引号回退
  安全网。用户看真实生成图后指出这版依然"鸡肋"——纯截断只是让句子变短，呈现的还是
  攻略正文的语气和密度，海报需要的是"关键词标签"而不是"被砍断的句子"。
- **2026-07-26第二次修复（关键词提炼，非自由摘要）**：新增`_distill_highlights`，用轻量
  模型（`get_light_chat_model`）把已核验的长文本压缩成不超过12字的短语标签——严格要求
  "只能重组原文已出现的词语，不能引入新事实/数字/地名，不写完整句子"，这跟"如实转录"
  的可信度前提不冲突：真实性由上游agent的引用核验保证，这一步只做语言层面的提炼，不
  新增任何可验证的事实性陈述。内容较短（低于阈值）时不触发LLM调用，直接沿用原有截断
  结果，保持纯代码路径的确定性；调用失败或返回数量对不上时同样回退到`_shorten`，不让
  海报生成因为这一步失败而整体挂掉。
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Optional

import requests

from src.config import POSTER_IMAGE_BASE_URL, POSTER_IMAGE_MODEL, get_light_chat_model
from src.workspace import store
from src.workspace.schema import LifestyleOutput, PitfallOutput, RoutePlan

_IMAGE_SIZE = "1024x1536"
_IMAGE_QUALITY = "low"
_MAX_LINE_LEN = 40

_OVERALL_STYLE_PROMPT = (
    "固定风格：复古旅行手账风（Vintage Travel Journal Style），手绘旅行攻略信息图海报，"
    "水彩插画 + 钢笔线稿 + 手账拼贴风，像一本真实旅行者笔记本页面。"
    "米白色复古纸张纹理背景，轻微做旧质感，手绘地图路线，旅行贴纸、胶带、邮票、印章、便签，"
    "明信片式景点插画卡片；低饱和复古配色，墨黑线稿，棕色复古文字，朱砂红重点标记，"
    "少量湖蓝、沙漠黄、植物绿点缀。"
)

# 避雷agent写警告文案时习惯用的两类框架性开场白（观察自真实产出，见docs/ISSUES.md）：
# ①"多个检索素材反映/提到/反馈/显示XX"——本身不含信息量，紧跟着才是具体事实；
# ②"检索到...指出以下踩坑点："——后面通常接一段带编号的详细列表，开场白同样是纯套话。
# 只剥离这两类已观察到的具体措辞，不做通用摘要（避免引入LLM才能做的语义判断）。
_META_PREFIX_LIST_INTRO = re.compile(r"^.{0,40}?(指出|列出|总结|反馈)以下(踩坑点|问题|几点|情况)[：:]\s*")
_META_PREFIX_SIMPLE = re.compile(r"^多个检索素材(均提到|均反映|反映|提到|反馈|显示)\s*")
_TRAILING_JUNK_CHARS = "：:，,、"


def _strip_meta_preamble(text: str) -> str:
    match = _META_PREFIX_LIST_INTRO.match(text)
    if match:
        return text[match.end():].lstrip()
    match = _META_PREFIX_SIMPLE.match(text)
    if match:
        return text[match.end():].lstrip()
    return text


def _shorten(text: str, max_len: int = _MAX_LINE_LEN) -> str:
    """纯截断，不改写措辞——"如实转录"要求不能靠LLM改写内容，只能靠机械截断控制长度。
    先剥离已知的框架性开场白（见上方两个pattern），再优先在第一个换行/句号处截断，
    保留一句完整的话；仍然超长则硬截断加省略号，且硬截断时如果恰好落在一个未闭合的
    引号中间，回退到引号开始之前，避免留下孤立引号+省略号这种视觉伪影。
    """
    text = _strip_meta_preamble(text.strip())
    for sep in ("\n", "。"):
        idx = text.find(sep)
        if 0 < idx <= max_len:
            return text[:idx].strip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rstrip()
    if truncated.count("'") % 2 == 1:
        truncated = truncated[:truncated.rfind("'")]
    truncated = truncated.rstrip(_TRAILING_JUNK_CHARS + " ")
    return truncated + "…"


# 内容短于这个阈值时不触发LLM提炼——短文本本身已经够短，直接沿用纯代码截断即可，
# 也让现有用短字符串构造的单元测试保持确定性，不需要真的调用/mock轻量模型。
_DISTILL_THRESHOLD = 20

# 剥离模型可能在每行前面加的编号（"1. ""2、"等），即使prompt已经要求不要编号。
_LINE_ENUM_PREFIX = re.compile(r"^\s*\d+[\.、）)]\s*")


def _distill_highlights(texts: list[str]) -> list[str]:
    """把已核验过的完整句子压缩成海报用的短语标签（不超过约12个汉字）。

    这是"提炼关键词"，不是"自由摘要改写"——严格要求模型只能重组原文中已经出现的
    词语/概念，不能引入新的具体数字/地名/店名/判断，跟项目"如实转录不编造"的可信度
    前提不冲突：内容真实性由上游agent的引用核验保证，这一步只做语言层面的浓缩。

    用纯文本按行解析而不是`with_structured_output(json_mode)`——真实调用中发现轻量
    模型对这种开放式生成任务不会稳定遵守json_mode（会直接给纯文本答案，不包一层JSON，
    触发`OutputParserException`），这跟`_extract_requirements_fields`那类固定字段抽取
    任务的行为不同，因此改用更贴合模型实际行为的"每行一个标签"格式。
    调用失败、返回为空、或返回行数跟输入条数对不上时，逐条回退到纯代码截断（`_shorten`），
    不让海报生成因为这一步失败而整体失败。
    """
    if not texts:
        return []
    if not any(len(t) > _DISTILL_THRESHOLD for t in texts):
        return [_shorten(t) for t in texts]
    try:
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        model = get_light_chat_model(temperature=0)
        response = model.invoke(
            f"下面是{len(texts)}条已经过事实核验的旅游攻略内容，请把每一条压缩成一个不超过"
            "12个汉字的短语标签，用于海报小卡片展示（不是完整攻略正文）。\n\n"
            "严格要求：\n"
            "1. 标签只能使用原文中已经出现过的具体词语/概念重新组织，不能引入原文没有的"
            "具体数字、地名、店名，也不能添加原文没有表达过的新判断——这是提炼关键词，"
            "不是自由改写或总结你自己的理解。\n"
            "2. 写成短语/关键词风格（比如\"药膳味偏重\"\"人多易拥堵\"\"非原址纪念馆\"），"
            "不要写成完整句子、不要句号。\n"
            f"3. 严格按输入顺序，每行一个标签，恰好{len(texts)}行，不要编号、不要多余说明文字。\n\n"
            f"内容：\n{numbered}"
        )
        raw_lines = [line.strip() for line in response.content.strip().split("\n")]
        tags = [_LINE_ENUM_PREFIX.sub("", line).strip() for line in raw_lines if line.strip()]
        if len(tags) == len(texts) and all(tags):
            return tags
    except Exception:
        pass
    return [_shorten(t) for t in texts]


def build_route_lines(route_plan: RoutePlan) -> list[str]:
    """每天一行："Day{N}路线：POI1 → POI2 → ..."，跳过没有POI的天数。"""
    lines = []
    for day in route_plan.days:
        poi_names = " → ".join(poi.name for poi in day.pois)
        if poi_names:
            lines.append(f"Day{day.day}路线：{poi_names}")
    return lines


def select_pitfall_lines(pitfall_output: Optional[PitfallOutput], limit: int = 6) -> list[str]:
    """把四个字段（food/outfit/lodging/play）的避坑条目拉平成一份列表，每条提炼成
    短语标签。limit控制总条数上限，避免海报"避坑指南"板块塞不下——按项目里
    已有的字段/条目先后顺序截取前N条，不做重要性排序（排序需要语义判断，交给纯
    代码规则做不合适，见/grilling讨论"筛选粒度"这条决策）。
    """
    if pitfall_output is None:
        return []
    raw: list[tuple[str, str]] = []
    for category in (pitfall_output.food, pitfall_output.outfit, pitfall_output.lodging, pitfall_output.play):
        for item in category.items:
            if not item.warning:
                continue
            label = item.claimed_entities[0] if item.claimed_entities else f"Day{item.day}"
            raw.append((label, item.warning))
    raw = raw[:limit]
    tags = _distill_highlights([warning for _, warning in raw])
    return [f"{label}：{tag}" for (label, _), tag in zip(raw, tags)]


def select_food_highlights(lifestyle_output: Optional[LifestyleOutput], per_day: int = 2) -> list[str]:
    """每天摘取lifestyle.json food字段的前N条推荐，提炼成短语标签。"""
    if lifestyle_output is None:
        return []
    by_day: dict[int, list[str]] = {}
    for item in lifestyle_output.food.items:
        bucket = by_day.setdefault(item.day, [])
        if len(bucket) < per_day:
            bucket.append(item.recommendation)
    entries: list[tuple[int, str]] = []
    for day in sorted(by_day):
        for text in by_day[day]:
            entries.append((day, text))
    tags = _distill_highlights([text for _, text in entries])
    return [f"Day{day}：{tag}" for (day, _), tag in zip(entries, tags)]


def build_poster_title(topic: str, num_days: int) -> str:
    return topic if topic else f"{num_days}天旅游攻略"


def build_poster_prompt(
    route_plan: RoutePlan,
    lifestyle_output: Optional[LifestyleOutput],
    pitfall_output: Optional[PitfallOutput],
    topic: str = "",
) -> str:
    """拼出喂给gpt-image-2的完整prompt文本。所有具体内容（路线/美食/避坑）都来自
    当前Project的结构化攻略结果，明确要求模型原样保留内容及其不确定性，不得新增或改写；
    模型只负责排版和插画风格。
    """
    title = build_poster_title(topic, len(route_plan.days))
    num_days = len(route_plan.days)
    route_lines = build_route_lines(route_plan)
    food_lines = select_food_highlights(lifestyle_output)
    pitfall_lines = select_pitfall_lines(pitfall_output)

    sections = [
        f"请生成一张《{title}》旅行总览信息图海报，内容覆盖{num_days}天行程。",
        _OVERALL_STYLE_PROMPT,
        f"版式固定：顶部大标题；中央手绘路线地图；Day1~Day{num_days}路线节点和方向箭头环绕地图；"
        "周围分布景点明信片卡片；底部设置整体行程、必吃推荐和避坑指南模块。",
        "参考日本旅行手绘地图、旅行杂志信息图和小红书高质量旅行攻略海报，避免商业旅游宣传册感和明显AI插画感。",
        "",
        "以下内容来自当前Project的结构化攻略结果。必须原样保留并绘制到对应板块，"
        "不得新增或改写任何具体地名/数字/建议，尤其不得弱化、隐藏或抹掉原文中的不确定性提示。"
        "只允许自由发挥版式/配色/插画风格。",
        "",
        "【路线】",
        *route_lines,
    ]
    if food_lines:
        sections += ["", "【必吃推荐】", *food_lines]
    if pitfall_lines:
        sections += ["", "【避坑指南】", *pitfall_lines]

    return "\n".join(sections)


def _read_day_source(project_id: str, day: int) -> str:
    """读取final_guide.md中单个Day段落，保留原文作为单日海报的事实边界。"""
    if day < 1 or day > 8:
        raise ValueError("单日海报只支持Day1到Day8")
    guide_path = store.workspace_dir(project_id) / "final_guide.md"
    if not guide_path.exists():
        raise RuntimeError(f"project '{project_id}' 没有final_guide.md，无法生成单日海报")
    guide = guide_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(?ms)^##[ \t]+Day[ \t]+{day}[ \t]*(?:\||｜|·).*?(?=^##[ \t]+|\Z)"
    )
    match = pattern.search(guide)
    if not match:
        raise RuntimeError(f"final_guide.md中没有找到Day{day}正文")
    return match.group(0).strip()


def build_daily_poster_prompt(project_id: str, day: int) -> str:
    """用final_guide.md对应Day原文拼接单日海报prompt，不重新编造路线事实。"""
    source = _read_day_source(project_id, day)
    requirements = store.read_requirements(project_id) or {}
    route_plan = store.read_route_plan(project_id)
    num_days = len(route_plan.days) if route_plan and route_plan.days else day
    topic = str(requirements.get("topic") or "").strip()
    if not topic and isinstance(requirements.get("requirements"), dict):
        topic = str(requirements["requirements"].get("destination") or "").strip()
    title = build_poster_title(topic, num_days)
    return "\n".join(
        [
            f"请生成一张《{title}》单日攻略信息图海报。",
            "",
            "固定视觉风格：复古旅行手账风（Vintage Travel Journal Style），手绘旅行攻略信息图海报，"
            "水彩插画 + 钢笔线稿 + 手账拼贴风，像一本真实旅行者笔记本页面。",
            "米白色复古纸张纹理背景，轻微做旧质感，手绘地图路线，旅行贴纸、胶带、邮票、印章、便签，"
            "明信片式景点插画卡片；低饱和复古配色，墨黑线稿，棕色复古文字，朱砂红重点标记，"
            "少量湖蓝、沙漠黄、植物绿点缀。",
            "",
            "版式固定：顶部大标题；中央时间轴路线；右侧或中下部景点/路线手绘小插画；"
            "下方设置穿搭提示、行前提醒、晚餐/住宿/避雷信息模块。",
            "",
            "必须严格使用下面Day正文中的原文信息，不得新增、删减、修改、编造任何具体地名、数字、"
            "里程、时间、票价、店名或建议。只允许自由发挥版式、配色、地图布局、装饰和插画表现方式。",
            "正文必须清晰可读，中文不要乱码或潦草字。",
            "",
            "【需要绘制的Day正文】",
            source,
            "",
            "画面比例：竖版长图，2:3比例，1024x1536视觉比例。",
            "请严格沿用上一张已生成图片的版式语言、标题样式、配色体系、图标风格、插画笔触和信息模块结构。",
        ]
    )


def _current_poster_key() -> str | None:
    """生图key（管理后台"模型/Key"页面CRUD化后，支持多key配置，2026-08-01新增）。
    不做自动轮询/失败换key——生图是按次收费的真实API调用，`call_image_api`已有的
    "失败重试一次"机制会用同一个第一顺位key重试；如果某个key确实失效，管理员应该
    在网页上手动删除/调整顺序，而不是引入一套完整的轮询机制为这个低频、人工触发的
    场景增加复杂度。始终用列表第一个key（管理员可控的顺序）。"""
    from src.api import key_store

    keys = key_store.list_keys("poster")
    return keys[0] if keys else None


def _request_image_once(prompt: str) -> bytes:
    api_key = _current_poster_key()
    if not api_key:
        raise RuntimeError("未配置生图Key，请在管理后台「模型/Key」页面添加")

    url = POSTER_IMAGE_BASE_URL.rstrip("/") + "/images/generations"
    payload = {
        "model": POSTER_IMAGE_MODEL,
        "prompt": prompt,
        "size": _IMAGE_SIZE,
        "quality": _IMAGE_QUALITY,
        "n": 1,
    }
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=280,
    )
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data") or []
    if not data or not data[0].get("b64_json"):
        raise RuntimeError(f"生图接口返回异常：{json.dumps(body, ensure_ascii=False)[:500]}")
    return base64.b64decode(data[0]["b64_json"])


def call_image_api(prompt: str) -> bytes:
    """失败自动重试一次（呼应cun.ai网关偶发504超时的实测经验），仍失败则报错，
    不做更多次重试——避免付费API的意外重复消耗（2026-07-24 /grilling讨论确定）。
    """
    try:
        return _request_image_once(prompt)
    except Exception as first_exc:
        try:
            return _request_image_once(prompt)
        except Exception as second_exc:
            raise RuntimeError(
                f"生成海报失败（已重试1次）。首次错误：{first_exc}；重试错误：{second_exc}"
            ) from second_exc


def generate_poster(
    project_id: str,
    confirm: bool = True,
    scope: str = "overall",
    day: int | None = None,
) -> Path:
    """主入口：读数据→拼prompt→存prompt存档→（可选）命令行确认→调用生图API→写poster.png。

    confirm=True时会在真正发起付费调用前打印确认提示、等待命令行输入y才继续，避免误触发
    消耗额度；confirm=False供测试/脚本化场景跳过确认（2026-07-24 /grilling讨论确定）。
    """
    route_plan = store.read_route_plan(project_id)
    if route_plan is None or not route_plan.days:
        raise RuntimeError(f"project '{project_id}' 还没有route_plan.json，无法生成海报")

    lifestyle_output = store.read_lifestyle_output(project_id)
    pitfall_output = store.read_pitfall_output(project_id)
    requirements = store.read_requirements(project_id)
    topic = requirements.get("topic", "") if requirements else ""

    if scope == "day":
        if day is None:
            raise ValueError("生成单日海报时必须提供day")
        prompt = build_daily_poster_prompt(project_id, day)
        artifact_stem = f"poster-day{day}"
    elif scope == "overall":
        prompt = build_poster_prompt(route_plan, lifestyle_output, pitfall_output, topic=topic)
        artifact_stem = "poster"
    else:
        raise ValueError(f"不支持的海报范围: {scope}")

    ws_dir = store.workspace_dir(project_id)
    ws_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = ws_dir / f"{artifact_stem}_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    if confirm:
        print("即将调用gpt-image-2生成海报（按次收费），实际prompt已存到:")
        print(f"  {prompt_path}")
        answer = input("是否继续？[y/N] ").strip().lower()
        if answer != "y":
            print("已取消，未发起付费调用。")
            raise SystemExit(0)

    image_bytes = call_image_api(prompt)

    poster_path = ws_dir / f"{artifact_stem}.png"
    poster_path.write_bytes(image_bytes)
    return poster_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python -m scripts.delivery.run_generate_poster <project_id>", file=sys.stderr)
        sys.exit(1)
    result_path = generate_poster(sys.argv[1])
    print(f"海报已生成: {result_path}")
