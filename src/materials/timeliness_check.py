"""route_plan 时效性核验：检查每天 notes 里的道路通行/限流/资质变更等信息是否完整、是否过时。

跟 citation_check.py 并列，职责不同：
- citation_check 核"引用真实性"——POI名/店名有没有chunk依据，是幻觉/编造问题。
- timeliness_check 核"时效性信息完整性"——notes 里提到的道路封闭/景区限流这类信息，
  UGC素材（某时间点拍摄的B站/抖音转写文本）可能过时或不全，靠素材库本身判断不可靠，
  改用 web_search 查权威路况公告，比对 notes 有没有完整反映当前真实状态。

背景（docs/ISSUES.md 第99/95条）：
1. qinggan_7d Day8 路况不完整复用——route_agent 把 038 chunk 的"达坂山封闭"挪进了 Day8
   notes，但漏了同一chunk里紧挨着的另外两段（景阳岭/峨堡-扁都口），且没把 038 标进 Day8
   sources。诊断脚本实测确认根因不是 chunk 截断（038 完整到达模型），是 notes 自由文本对
   封闭信息不保证完整性。route_agent 即使是自驾类（prompt 第11条理应触发 web_search），
   实测 0 次调用，"触发了也不查"。
2. emeishan_3d 金顶限流连续 3 次漏掉——prompt 第11条触发框架写死"自驾/长线穿越类"，
   没覆盖"登山朝圣"这类同样有限流风险的目的地。

核验方式（用户决策，方向2起步——只做 route，lifestyle/poi_details 留作已知缺口）：
- 查证对象：从 day.notes 抽"道路标识"专名（G/国道+数字 + 已知垭口/地标专名词表）。
  不从 sources chunk 抽——chunk 标注本身是已知不可靠环节（问题1根因正是 route_agent 漏标
  sources），用漏标的产物做抽取输入会原样继承漏标问题。
- 核验手段：用 web_search 程序化查证（不靠 chunk，chunk 时效性本身可能过时）。
  实测坐实（sandbox/diag_web_search_b.py）：Tavily 查"达坂山 G227 封闭施工 2026"返回
  青海省交通运输部门官方公告，3/3 段封闭区间全命中——即使 notes 只提"达坂山"，web 查
  整条 G227 能自动带出另外两段，正好补 notes 的漏。
- 比对判定：用 get_light_chat_model（便宜模型）做 web结果 vs notes 的完整性比对，
  json_mode 结构化输出（项目里 deepseek-v4-flash 不支持 json_schema/function_calling，
  json_mode 是唯一稳定方式，见 citation_check.py:320 注释）。
- 失败兜底：web_search key 耗尽/LLM判定异常时按"放行"处理（参考 orchestrator.py:1414
  _filter_relevant_candidates 的 try/except 模式），时效核验是增强不是阻塞。

关键约束（实测得出，写进 docstring 强制 check）：web_search query 必须含具体道路编号/垭口名，
不能用"张掖回西宁"这种笼统措辞——实测 query4 笼统措辞 0/3 命中，返回全是旅游团营销内容。
从 notes 抽取时只抽道路编号+垭口/地标专名，不抽城市名。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from src.config import get_structured_chat_model as get_chat_model
from src.structured_output import bind_structured_json
from src.tools.web_search_tools import make_web_search_tools
from src.workspace import store


class TimelinessVerdict(BaseModel):
    has_timeliness_info: bool = Field(
        description="web查证结果里有没有封闭/施工/限流/限行/歇业这类时效性运营状态信息"
    )
    missing_in_notes: list[str] = Field(
        description="web提到但notes里没有完整反映的具体封闭区间/路段/限流措施（带原文措辞，"
        "方便route_agent打回重写时直接引用）。notes完整反映了web所有时效信息时留空数组。"
    )
    reason: str = Field(description="判断理由，一句话")
    requires_structure_change: bool = Field(
        default=False,
        description="只有权威结果明确表明原路线当前不可执行，且仅补notes警告不足以解决时才为true",
    )
    structure_change_reason: str = Field(
        default="",
        description="需要换POI、调整顺序或改所属天时的具体原因；不需要结构调整时留空",
    )


# 已知垭口/地标专名词表——封闭的封闭区间关键词。封闭小词表，不做开放式模糊匹配
# （参考 citation_check.py:130 的 _LANDMARK_SUFFIXES 思路：封闭词表只覆盖确实通用的
# 时效性地标，遇到词表外的（比如某次新增的小地名）转由 light model 兜底判定）。
# 这些词是从真实素材（青甘大环线-038 原文）和实测 web 公告里提炼的垭口/地标名，
# 后续遇到新目的地的时效性地标，扩充这个表即可。
_ROAD_LANDMARK_KEYWORDS = (
    # G227沿线垭口（青甘大环线真实案例）
    "达坂山", "景阳岭", "峨堡", "扁都口",
    # 通用道路/景区时效性地标后缀（限定"路/岭/口/垭口"这类，避免误抽城市名）
)

# 道路编号正则：G227 / 国道227 / 国道二二七 / G0611 / S302 / 省道302 / 227国道
# 中文数字版（国道二二七）单独匹配——038 原文用的是"国道二二七"这种写法
# 注意"国道227"和"227国道"都要匹配，所以分开几个分支，避免顺序约束。
_ROAD_NUMBER_PATTERN = re.compile(
    r"(?:国道|省道)[一二三四五六七八九十百千万零〇Oo0-9]+"
    r"|(?:G|S)\s*\d{1,5}"
    r"|(?:[0-9]{2,4})\s*国道"
    r"|(?:[0-9]{2,4})\s*省道",
    re.IGNORECASE,
)


def _extract_road_identifiers(notes: str) -> list[str]:
    """从 day.notes 抽"道路标识"专名——道路编号（G227/国道227）+ 已知垭口/地标专名。

    只抽这些，不抽城市名——实测（sandbox/diag_web_search_b.py query4）证明笼统的
    "张掖回西宁"措辞 web 查 0/3 命中，返回全是旅游团广告；必须用具体道路编号/垭口名
    才能 web 查到权威路况公告。

    返回去重后的标识列表，每项是一个可直接拼进 web_search query 的短语
    （如 "G227"、"达坂山"）。
    """
    if not notes:
        return []
    found: list[str] = []

    # 1. 道路编号：G227/国道227/国道二二七/G0611/省道302 等
    for m in _ROAD_NUMBER_PATTERN.finditer(notes):
        token = m.group(0).strip()
        # 归一化：把"国道二二七"这种中文数字写法保留原样（web 查"国道227"和"国道二二七"
        # 都能命中，实测 query1 用的就是"G227"），同时把 G 后面常见的空格去掉
        token = re.sub(r"\s+", "", token)
        if token and token not in found:
            found.append(token)

    # 2. 已知垭口/地标专名
    for kw in _ROAD_LANDMARK_KEYWORDS:
        if kw in notes and kw not in found:
            found.append(kw)

    return found


def _normalize_road_id(token: str) -> str:
    """归一化抽出的道路标识，方便 web 查证。

    实测（sandbox/diag_web_search_b.py + 调试期实测）：
    - "G227" → web 命中权威公告（3/3 段，最优）
    - "国道227" / "227国道" / "国道二二七" → web 0/3 命中（Tavily 对中文数字+国道写法召回差，
      返回短且无官方公告）
    所以把"国道227/227国道/国道二二七"都归一成"G227"再拼 query。
    "G0611"/"省道302" 这类本身是字母数字写法的保留原样。
    """
    token = re.sub(r"\s+", "", token)
    # 提取数字部分（中文数字 or 阿拉伯数字）——匹配"国道二二七""国道227""227国道""G227"等
    m = re.search(r"([一二三四五六七八九十百千万零〇Oo0-9]+)", token)
    if not m:
        return token
    num_str = m.group(1)
    # 中文数字转阿拉伯（简单覆盖常见的道路编号位数）
    cn_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
              "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
              "百": "", "千": "", "万": "", "零": "0", "〇": "0", "O": "0", "o": "0"}
    if any(c in cn_map for c in num_str):
        num = "".join(cn_map.get(c, c) for c in num_str)
    else:
        num = num_str
    prefix = "S" if "省道" in token else "G"
    return f"{prefix}{num}"


def _build_web_query(road_ids: list[str], year: Optional[int] = None) -> str:
    """把抽到的道路标识拼成 web_search query。

    实测（sandbox/diag_web_search_b.py + 调试期实测）：
    - "达坂山 G227 封闭施工 2026" → 3/3 命中（最佳）
    - "G227 封闭施工 2026"（只有道路编号）→ 2/3 命中（漏达坂山，但抓到峨堡-扁都口+景阳岭）
    - "国道227 封闭施工 2026" → 0/3 命中（中文写法 Tavily 召回差）
    - "张掖到西宁 道路封闭 2026"（无道路编号）→ 0/3 命中

    所以 query 要：归一化后的道路编号 + 已知垭口/地标专名 + 时效关键词 + 当前年份。
    """
    if not road_ids:
        return ""
    # 标识归一化后用空格连接（G227 达坂山），加时效关键词和年份。路线可执行性
    # 核验会传入旅行日期年份；旧调用方不传时动态使用当前年份。
    normalized = [_normalize_road_id(t) for t in road_ids]
    query_year = year if year is not None else datetime.now(timezone.utc).year
    return f"{' '.join(normalized)} 封闭施工限行 {query_year}"


def _judge_timeliness(notes: str, web_result: str) -> Optional[TimelinessVerdict]:
    """用 full model（不是 light model）判定 web 结果里的时效信息 notes 是否完整反映。

    **模型选型**（docs/ISSUES.md第100条 A/B测试验证）：最初用 get_light_chat_model
    （sensenova-6.7-flash-lite），真实E2E里把全省路况公告各路段都算进missing_in_notes
    （88%误报率），把route_agent拖进死循环（Day1连续3轮force_finalize）。A/B测试证明
    这是模型选型问题不是prompt措辞问题——换full model（deepseek-v4-flash）后误报率
    降到50%，且真相关段（湟中区国寺营桥/湟源县日月山垭口）2/2全召回不漏。

    light model做不了"web路段地名 vs 当天行程地名"的地理相关性推理——CLAUDE.md在
    get_light_chat_model注释里早就划过"明确不用于核心内容生成或引用真实性核验"，
    这里做的是引用真实性核验，本就该用full model，最初选light是越界。

    成本权衡：每个有道路标识的天调一次full model，8天行程最多8次核验调用+重试循环
    里每轮再调，量级可控（不是几十次）。full model的token截断历史（第82/83条reasoning
    卡8193）是在"生成端写days数组"的长输出场景，这里返回小JSON，输出很短不会撞截断。

    失败时返回 None，调用方按"放行"处理（不武断打回——时效核验是增强不是阻塞，
    参考 orchestrator.py:1414 _filter_relevant_candidates 的 try/except 兜底模式）。
    """
    model = bind_structured_json(get_chat_model(temperature=0), TimelinessVerdict)
    prompt = (
        f"下面是一段旅游行程的notes（route_agent写的当天路线说明）和web查证结果"
        f"（搜索引擎返回的当前路况/限流信息）。请判断：web结果里提到的封闭/施工/"
        f"限流/限行/歇业这类时效性信息，notes有没有完整反映？\n\n"
        f"判断要点（务必严格遵守，否则会把无关路段也算成缺失）：\n"
        f"1. web结果是一条省级路况公告/多篇网页，可能包含全省各路段的施工信息——"
        f"你只需要关心跟notes当天行程【直接相关】的路段（notes提到的道路编号/垭口/"
        f"地标所在的那条路，以及这条路上前后相邻的同名路段），不要把全省其它无关路段"
        f"（比如跟notes提到的道路根本不是同一条线的）算进missing_in_notes。\n"
        f"2. web结果里没有跟notes相关道路的时效性信息（只是景点介绍/旅游团广告/无关路段）"
        f"时，has_timeliness_info=false，missing_in_notes留空。\n"
        f"3. web结果有跟notes相关道路的时效性信息，且notes也完整提到了所有相关封闭/限流"
        f"区间，missing_in_notes留空数组（不是问题）。\n"
        f"4. web结果有跟notes相关道路的时效性信息，但notes只提了一部分（比如web说三段封闭，"
        f"notes只提了一段），把notes漏掉的【相关】封闭区间/路段列进missing_in_notes，"
        f"带web原文措辞，方便打回重写时直接引用。\n"
        f"5. 不要把notes里已经提到的内容算进missing_in_notes。\n\n"
        f"6. 如果权威结果明确表明原路线当前不可执行（例如景点长期闭园、道路封闭导致当天线路"
        f"无法按原计划完成），且仅在notes里补警告不足以解决，requires_structure_change=true，"
        f"并在structure_change_reason说明必须调整什么。只是补充开放时间、预约、限流或绕行提醒"
        f"即可解决时必须为false。拿不准时也为false。\n\n"
        f"请以JSON格式回复，包含has_timeliness_info(布尔)、missing_in_notes(字符串数组)、"
        f"reason(字符串)、requires_structure_change(布尔)、structure_change_reason(字符串)五个字段。\n\n"
        f"notes：{notes}\n\nweb查证结果：{web_result}"
    )
    return model.invoke(prompt)


def verify_route_timeliness(
    project_id: str,
    topic: str,
    days: Optional[list[int]] = None,
) -> list[dict]:
    """返回未通过核验的问题列表：[{day, city, area, road_identifiers, web_query,
    web_evidence, missing_in_notes, reason, notes}]

    对 route_plan.json 的每个 day（或 days 指定的天）：
    1. 从 day.notes 抽道路标识专名（_extract_road_identifiers）。
    2. 抽不到任何道路标识 → 跳过这天。这是合法的"无显性时效断言"——按用户决策
       （方向2），时效核验只对"已经明确声明时效断言"的项做，notes 没提路况的天
       留给陪伴阶段 realtime_agent 兜底，不在生成阶段核验，控制成本。
    3. 抽到了 → web_search 查证（_build_web_query + make_web_search_tools 程序化调用）。
    4. web 结果 + notes 喂给 full model 判定（_judge_timeliness）。
    5. has_timeliness_info=False → 不报问题（这条路当前可能确实没封闭，web 查不到不等于错）。
       missing_in_notes 非空 → 报问题。

    days 为 None（默认）核验全部天数（plan模式/整体重新生成）；传入具体天数列表时只核验
    这些天——edit 模式用于把核验范围收窄到这次编辑实际改动的天数，跟
    verify_route_citations 的 days 参数同一个 scoping 思路（docs/ISSUES.md第37补条）。

    失败兜底（web key耗尽/LLM异常）：当天按"放行"处理，不报问题也不阻塞流程——
    时效核验是增强不是阻塞。这一层失败由调用方（route_timeliness_check_node）的
    force_finalize 机制兜底，不在核验函数里 raise。
    """
    # web_search 工具程序化调用：make_web_search_tools 返回 BaseTool 列表，
    # 用 .invoke({"query": ...}) 调用（langchain tool 标准方式，不走 agent react 循环）。
    # 复用 make_web_search_tools 而不是自己 POST Tavily——保住 key 轮询逻辑
    # （web_search_tools.py:39-89 的 429/432/433 换 key 重试）。
    web_search_tool = make_web_search_tools(project_id, "route_timeliness_check")[0]

    route_plan = store.read_workspace(project_id, "route_plan.json")
    if not route_plan:
        return []

    target_days = set(days) if days is not None else None

    issues: list[dict] = []
    for day in route_plan.get("days", []):
        if target_days is not None and day.get("day") not in target_days:
            continue
        notes = day.get("notes", "") or ""
        road_ids = _extract_road_identifiers(notes)
        if not road_ids:
            # notes 没提任何道路标识——不在生成阶段核验，留给陪伴阶段 realtime_agent
            continue

        web_query = _build_web_query(road_ids)
        try:
            web_evidence = web_search_tool.invoke({"query": web_query})
        except Exception:
            # web_search 工具本身失败（key全耗尽/网络）——按放行处理，不武断打回。
            # 工具内部已经做了 key 轮询和降级提示（web_search_tools.py:60-112），
            # 这里再 catch 是兜 tool 抛异常的极端情况。
            continue

        # web 返回的是降级提示字符串（"未配置"/"没有找到相关结果"/"所有key都不可用"）时，
        # 没有真实时效信息，跳过——不把"查不到"当成"有问题"。
        if not web_evidence or "未配置" in web_evidence or "没有找到相关结果" in web_evidence:
            continue

        try:
            verdict = _judge_timeliness(notes, web_evidence)
        except Exception:
            # full model 判定异常——按放行处理（参考 _filter_relevant_candidates:1414）
            continue

        if verdict is None or not verdict.has_timeliness_info:
            continue
        if not verdict.missing_in_notes and not verdict.requires_structure_change:
            # web 有时效信息，notes 也完整反映，且原路线仍可执行——通过。结构问题不能因
            # notes已经写明风险而被吞掉："已写闭园"不等于"仍安排该POI"变得可执行。
            continue

        issues.append(
            {
                "day": day.get("day"),
                "city": day.get("city"),
                "area": day.get("area"),
                "road_identifiers": road_ids,
                "web_query": web_query,
                "web_evidence": web_evidence,
                "missing_in_notes": verdict.missing_in_notes,
                "reason": verdict.reason,
                "notes": notes,
                "requires_structure_change": verdict.requires_structure_change,
                "structure_change_reason": verdict.structure_change_reason,
            }
        )

    return issues
