"""飞书消息路由核心逻辑：收到一条消息后，判断绑定/awaiting状态，调用对应的
orchestrator入口，格式化回复、驱动进度轮询。

不直接依赖lark_oapi（那是ws_client.py的职责）——所有对外的"发消息"/"编辑消息"副作用
都通过调用方传入的ReplyChannel完成，真实实现在ws_client.py里包一层lark_oapi的
消息API，测试里注入fake实现，跟飞书接入经验文档"接入层协议差异在最外层抹平，业务
代码不感知渠道细节"的教训一致。

耗时的graph.invoke()调用（route_agent/lifestyle_agent等真实LLM调用是分钟级的）必须
丢进后台线程——WS事件回调线程必须立刻返回，不能被一次完整的攻略生成阻塞住整条长连接。
复用src/api/jobs.py的通用任务框架（trigger+并发锁），跟管理后台"彻底重跑"共用同一套
"任务key正在跑就拒绝重复触发"语义。

**2026-07-26进度展示改版**（真实用户反馈：进度是"覆盖型"，中间发生了什么用户完全看不到，
体验单薄）：不再是"每次poll把消息替换成一句新的当前状态"，改成`_ProgressTracker`维护
一份从头到尾的累积阶段清单——同一条消息仍然是原地编辑（不刷屏），但每次编辑都是在完整
历史清单基础上追加/刷新，而不是替换掉整句话。这样最终这条消息本身就是一份完整的执行
记录（含每个阶段的耗时、有没有被打回重试），不是转瞬即逝的"正在处理"。

同时新增：完成后顺带生成静态展示网站（纯程序拼接，不调用LLM，够快，直接同步调用），
把链接带进完成消息——网站部署复用本项目已有的管理后台服务（`GET /api/projects/{id}/site`），
不另外接一套独立静态托管，个人使用场景下只要保持管理后台常驻运行即可访问。以及完成后
主动询问要不要获取海报（按需触发，不自动生成——没有现成图片时，生成海报是gpt-image-2
真实收费调用，见run_generate_poster.py），用户回复"要"后优先发送已有图片，否则才生成。

**2026-08-18用户体验改进**：①完成消息明确列出所有可用能力（不再只是笼统的"想看哪部分
可以直接问我"）；②新增帮助命令（帮助/help/菜单/?/？//help）展示详细功能列表；③陪伴阶段
回答开头显示agent标识（📖随身向导/⚠️避雷助手/🌐实时助手/📚文化向导），环境变量可关闭；
④新用户首次使用时发送欢迎消息（方案A）；⑤检测"你能做什么""有什么功能"等新手问题时
主动给出引导（方案C）。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from src import orchestrator
from src.api import jobs
from src.channels.feishu import bindings, intent, push_log
from src.config import get_light_chat_model
from src.review import store as review_store
from src.workspace import store
from src.workspace.schema import ProjectPhase

from pydantic import BaseModel, Field

# 单条消息的最大字符数，超过按此长度切分成多条发送——跟飞书接入经验文档记录的
# 默认值（3200）保持一致，避免单条消息过长被飞书拒收。
FEISHU_REPLY_CHAR_LIMIT = int(os.environ.get("FEISHU_REPLY_CHAR_LIMIT", "3200"))
# 进度轮询间隔（秒）——跑route_agent/lifestyle_agent这类真实LLM调用期间，多久检查一次
# workspace/run_status.json（机制B，src/orchestrator.py的_track_run_status）并更新
# 进度消息，而不是一直发新消息刷屏。
FEISHU_PROGRESS_POLL_SECONDS = float(os.environ.get("FEISHU_PROGRESS_POLL_SECONDS", "2.5"))
# 呈现层（生成的静态网站）实际访问地址的前缀——个人使用场景，复用已有管理后台
# （uvicorn src.api.app:app）而不是另起一套nginx静态托管，完成消息里的链接直接拼这个
# 前缀+GET /api/projects/{id}/site。只要管理后台进程保持运行，这个链接就能访问。
FEISHU_SITE_BASE_URL = os.environ.get("FEISHU_SITE_BASE_URL", "http://127.0.0.1:8000")
# 🆕 陪伴阶段是否在回答开头显示agent标识（2026-08-18）
FEISHU_SHOW_AGENT_INFO = os.environ.get("FEISHU_SHOW_AGENT_INFO", "true").lower() == "true"

# 🆕 Agent显示名称映射（2026-08-18）
_AGENT_DISPLAY_NAMES = {
    "guide_agent": "📖 随身向导",
    "realtime_agent": "🌐 实时信息",
    "companion_lifestyle_query": "🍜 美食住宿",
    "lifestyle_agent": "🍜 美食住宿",
    "companion_pitfall_query": "⚠️ 避雷助手",
    "pitfall_agent": "⚠️ 避雷助手",
    "companion_culture_query": "📚 文化科普",
    "culture_agent": "📚 文化科普",
}

TASK_TYPE = "feishu_turn"


class ReplyChannel:
    """service.py只依赖这几个方法的最小抽象，不直接依赖lark_oapi。真实实现见
    ws_client.py（包一层lark_oapi的im/v1/messages发送+编辑+图片接口），测试里注入
    只记录调用参数的fake实现。"""

    def send(self, text: str) -> str:
        """发一条新消息，返回message_id（供后续edit使用）。"""
        raise NotImplementedError

    def edit(self, message_id: str, text: str) -> None:
        """原地编辑已发送的消息（进度轮询用，不是每次都发新消息刷屏）。"""
        raise NotImplementedError

    def send_image(self, image_path: str) -> str:
        """发一张图片消息（海报推送用），返回message_id。真实实现见ws_client.py。"""
        raise NotImplementedError


def split_for_reply(text: str, limit: int = FEISHU_REPLY_CHAR_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _classify_interrupt_kind(payload: dict) -> str:
    """interrupt() payload靠各自独有的key区分。"""
    if "draft" in payload:
        return "requirements"
    if "route_change_proposal" in payload:
        return "route_structure_confirm"
    if "route_plan" in payload:
        return "skeleton_confirm"
    if "patch" in payload:
        return "realtime_approval"
    return "unknown"


def _format_route_plan_summary(route_plan: Optional[dict]) -> str:
    if not route_plan:
        return ""
    lines = []
    for day in route_plan.get("days", []):
        city = str(day.get("city") or "").strip()
        area = str(day.get("area") or "").strip()
        if city and area.startswith(city):
            location = area
        elif city and area:
            location = f"{city} · {area}"
        else:
            location = city or area
        pois = "、".join(p.get("name", "") for p in day.get("pois", []))
        location_text = f" {location}" if location else ""
        lines.append(f"Day{day.get('day')}{location_text}：{pois}")
    return "\n".join(lines)


# 真实联调用户反馈（2026-07-25）：进度提示用orchestrator内部节点名（如"lifestyle_agent_node"）
# 对普通用户毫无意义，只对排查问题的开发者有用。这里做一层人话翻译，没收录的节点名兜底显示
# 原名（不阻塞新节点上线——加节点忘记同步这张表不会导致报错，只是暂时看到技术名）。
_NODE_FRIENDLY_LABELS: dict[str, str] = {
    "material_agent_node": "正在检查素材库",
    "requirements_gather_node": "正在了解你的需求",
    "collect_requirements": "需求已确认",
    "route_draft": "正在规划路线",
    "await_skeleton_confirm": "路线骨架待确认",
    "await_route_structure_confirm": "路线结构变化待确认",
    "route_citation_check_node": "正在核实路线真实性",
    "lifestyle_agent_node": "正在整理美食住宿穿搭建议",
    "citation_check_node": "正在核实内容真实性",
    "pitfall_agent_node": "正在搜集避坑提醒",
    "pitfall_citation_check_node": "正在核实避坑内容真实性",
    "integration_agent_node": "正在整合成完整攻略",
    "poi_detail_agent_node": "正在生成重点景区深度玩法",
    "poi_details_join_node": "正在做最后整理",
    "guide_agent_node": "正在查询已生成内容",
    "realtime_agent_node": "正在联网查询最新信息",
    "await_realtime_approval_node": "等待确认更新内容",
    # 此前两个 companion 节点分别写成“正在补充查询”/“正在查询避坑信息”，前者太笼统——
    # 用户看不出是在查美食住宿还是在查避雷。改成带 agent 身份的措辞，跟最终答案开头的
    # _AGENT_DISPLAY_NAMES（🍜 美食住宿 / ⚠️ 避雷助手）对齐，进度条和答案身份一致。
    "companion_lifestyle_query_node": "正在补充查询美食住宿",
    "companion_pitfall_query_node": "正在补充查询避雷信息",
    "companion_culture_query_node": "正在查询文化背景知识",
    "culture_agent_node": "正在查询文化背景知识",
}


# node名 -> trace agent名（2026-08-18新增，见docs/ISSUES.md）：真实飞书场景下route_draft
# 卡在cun渠道504网关超时循环里40分钟，进度提示始终是同一行"进行中"没有任何变化，用户
# 无法区分"正常在跑"和"卡在故障重试"。node函数体内调用set_trace_context(project_id,
# agent)（见src/orchestrator.py），LLM调用的成功/失败记录落到
# projects/<id>/agents/<agent>/trace.jsonl（src/llm_trace.py）——这里维护跟
# set_trace_context调用点一致的node->agent映射，供_detect_retry_trouble读取。
# 没有调用set_trace_context的node（如material_agent_node不经过react-agent LLM调用、
# await_skeleton_confirm是纯等待节点）不在此列，_detect_retry_trouble对未收录的node
# 直接返回None，不报错。同一个agent被多个node复用trace目录是预期行为（比如
# route_citation_check_node/route_timeliness_check_node打回重试时也用route_agent这个
# trace目录）——只看"最近发生了什么"，不需要精确区分是哪个node触发的。
#
# companion_lifestyle_query_node/companion_pitfall_query_node此前都映射到同一个
# "companion_query"（2026-08-21修复，见docs/ISSUES.md第142条）——trace落同一个目录，
# _detect_current_step读tool_calls拼"正在检索素材库"这类后缀时无法区分是lifestyle
# 还是pitfall在查（node级友好标签_NODE_FRIENDLY_LABELS已经能区分，trace级证据没有）。
# 拆分成"companion_lifestyle"/"companion_pitfall"两个独立trace目录后解决。
_NODE_TO_AGENT: dict[str, str] = {
    "route_draft": "route_agent",
    "route_citation_check_node": "route_agent",
    "route_timeliness_check_node": "route_agent",
    "lifestyle_agent_node": "lifestyle_agent",
    "citation_check_node": "lifestyle_agent",
    "pitfall_agent_node": "pitfall_agent",
    "pitfall_citation_check_node": "pitfall_agent",
    "culture_agent_node": "culture_agent",
    "integration_agent_node": "integration_agent",
    "poi_detail_agent_node": "poi_detail_agent",
    "guide_agent_node": "guide_agent",
    "realtime_agent_node": "realtime_agent",
    "companion_lifestyle_query_node": "companion_lifestyle",
    "companion_pitfall_query_node": "companion_pitfall",
    "companion_culture_query_node": "companion_culture",
}

# 连续失败判定阈值：最近N条trace记录全部ok=False才判定为"疑似故障重试"（保守，
# 避免偶发1-2次瞬时网络抖动就误报——src/llm_rotation.py本身已经有渠道内3轮×渠道外
# 3圈的自动重试，个别失败是正常现象，只有"连续一长串都失败"才值得主动提示用户）。
_RETRY_TROUBLE_THRESHOLD = 3

_ERROR_TYPE_LABELS: dict[str, str] = {
    "InternalServerError": "网关超时/服务端错误",
    "RateLimitError": "触发限流",
    "APITimeoutError": "请求超时",
    "APIConnectionError": "网络连接异常",
}


def _detect_retry_trouble(project_id: str, node: str) -> Optional[str]:
    """检查该node对应agent最近的LLM调用trace，连续失败达到阈值时返回一句简短中文
    提示；未收录该node、trace文件不存在、或最近记录不构成连续失败时返回None（不
    展示额外信息，保持现有渲染不变）。纯只读展示判定，不影响任何重试/超时逻辑本身。"""
    agent = _NODE_TO_AGENT.get(node)
    if not agent:
        return None
    from src.llm_trace import read_recent_trace

    recent = read_recent_trace(project_id, agent, limit=_RETRY_TROUBLE_THRESHOLD)
    if len(recent) < _RETRY_TROUBLE_THRESHOLD:
        return None
    if not all(r.get("ok") is False for r in recent):
        return None
    last = recent[-1]
    error_type = last.get("error_type") or ""
    label = _ERROR_TYPE_LABELS.get(error_type, "网络异常")
    return f"⚠️ 检测到连续{len(recent)}次{label}重试，可能需要更长时间或稍后重试"


# 工具名 -> 友好中文标签（2026-08-18新增，第二期进度可见性）：真实反馈"正常运行时也应该
# 看到步骤，不然是透明的"——第一期只解决了"检测到故障时给提示"，节点内部正常执行时进度
# 提示始终是同一句"进行中"不变。trace.jsonl成功记录（ok=True分支）带tool_calls字段，是
# react-agent多轮循环里"AI最近一次决定做什么"的真实信号，用它推断当前步骤。跟
# _NODE_FRIENDLY_LABELS同款容错：未收录的工具名兜底显示原名，不阻塞新工具上线时忘记
# 同步这张表。收录范围见各src/tools/*.py里@tool装饰的函数名。
_TOOL_FRIENDLY_LABELS: dict[str, str] = {
    "retrieve_materials": "检索素材库",
    "web_search": "联网搜索最新信息",
    "amap_driving_distance": "核实驾车距离",
    "amap_query": "查询地图信息",
    # 陪伴阶段只读工具（2026-08-21补）：此前 read_workspace 没收录，guide_agent 读
    # final_guide.md 时后缀会兜底显示技术名“正在read_workspace”，用户看不出在读什么。
    # “已生成内容”是笼统但准确的措辞——read_workspace 在 guide 读 final_guide.md、
    # 在 companion_query 读 route_plan.json，都是读“已经落盘的产出”，统一这么标。
    "read_workspace": "读取已生成内容",
    # 收尾性工具：AI 调 answer/answer_query 时已经在组织最终文本了，不收录会兜底显示
    # 技术名，收录成“组织回答”让后缀自然（“· 正在组织回答”）。
    "answer": "组织回答",
    "answer_query": "组织回答",
    # realtime_agent 的提醒类工具：陪伴阶段常见动作，此前全部兜底显示原名。
    "propose_patch": "准备修改建议",
    "get_current_time": "确认当前时间",
    "create_reminder": "设置提醒",
    "list_reminders": "查看提醒列表",
    "cancel_reminder_tool": "取消提醒",
    "update_reminder_tool": "修改提醒",
    # 写入工具：主要在生成阶段出现，陪伴阶段 companion_lifestyle/pitfall 明确不调写入
    # 工具（prompt 禁止），但收录了让生成阶段进度也清楚。
    "write_route_plan": "写入路线方案",
    "write_route_plan_patch": "写入路线方案",
    "write_agent_output": "写入产出内容",
    "write_agent_output_patch": "写入产出内容",
    "write_final_guide": "写入最终攻略",
    "report_conflict": "标记需要修正的问题",
    "record_finding": "记录检索发现",
}


def _detect_current_step(project_id: str, node: str) -> Optional[str]:
    """读该node对应agent最近一条trace记录，是成功记录且带tool_calls时，返回形如
    "检索素材库、联网搜索最新信息"的当前步骤描述；最近一条是失败记录（该由
    _detect_retry_trouble覆盖，避免"正在检索"和"连续失败"同时出现造成矛盾观感）、
    没有tool_calls（AI这轮只输出文字没调用工具）、或trace文件还没有任何记录时返回
    None。近似展示，不追求毫秒级实时——最后一条trace记录反映"AI最近决定做什么"，
    不是"工具真的还在执行中的哪一秒"，下一次轮询（2.5秒间隔）如果已产生新记录会
    自动刷新。"""
    agent = _NODE_TO_AGENT.get(node)
    if not agent:
        return None
    from src.llm_trace import read_recent_trace

    recent = read_recent_trace(project_id, agent, limit=1)
    if not recent:
        return None
    last = recent[-1]
    if last.get("ok") is not True:
        return None
    tool_calls = last.get("tool_calls") or []
    if not tool_calls:
        return None
    seen: list[str] = []
    for name in tool_calls:
        label = _TOOL_FRIENDLY_LABELS.get(name, name)
        if label not in seen:
            seen.append(label)
    return "、".join(seen)


def _format_elapsed(total_seconds: float) -> str:
    total_seconds = max(0, int(total_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def _logged_send(channel: ReplyChannel, chat_id: str, project_id: Optional[str], push_type: str, text: str) -> str:
    """包一层channel.send()，成功/失败都追加一条推送日志（2026-08-01新增，补上
    此前"推送成功/失败日志仍是占位未做"的缺口）。不改变异常语义——失败时原样
    re-raise，调用方现有的错误处理逻辑完全不受影响，只是多一个记日志的副作用。"""
    try:
        result = channel.send(text)
    except Exception as exc:
        push_log.append_push_log(chat_id, project_id, push_type, False, str(exc))
        raise
    push_log.append_push_log(chat_id, project_id, push_type, True, None)
    return result


def _logged_edit(channel: ReplyChannel, chat_id: str, project_id: Optional[str], push_type: str, message_id: str, text: str) -> None:
    try:
        channel.edit(message_id, text)
    except Exception as exc:
        push_log.append_push_log(chat_id, project_id, push_type, False, str(exc))
        raise
    push_log.append_push_log(chat_id, project_id, push_type, True, None)


def _logged_send_image(channel: ReplyChannel, chat_id: str, project_id: Optional[str], push_type: str, image_path: str) -> str:
    try:
        result = channel.send_image(image_path)
    except Exception as exc:
        push_log.append_push_log(chat_id, project_id, push_type, False, str(exc))
        raise
    push_log.append_push_log(chat_id, project_id, push_type, True, None)
    return result


def _progress_header(project_id: str) -> str:
    """完成消息/进度卡片顶部的目的地摘要，读requirements.json——没有的话（比如陪伴阶段
    单条查询这类还没走到需要它的场景）直接返回空字符串，调用方按空串跳过这一行。"""
    saved = store.read_requirements(project_id)
    if not saved:
        return ""
    req = saved.get("requirements") or {}
    destination = req.get("destination") or saved.get("topic") or ""
    if not destination:
        return ""
    days = req.get("days")
    return f"📍 {destination} · {days}天" if days else f"📍 {destination}"


class _ProgressTracker:
    """维护一次turn从头到尾的累积阶段清单，不依赖run_status.json本身的数据结构变化——
    run_status.json只存"当前正在跑哪个node"这一条快照（机制B），这里在轮询间隔
    （FEISHU_PROGRESS_POLL_SECONDS）内用wall clock近似算出每个阶段的耗时，精度足够
    一份进度展示消息使用，不追求精确到毫秒（真实节点通常是秒级到分钟级，2.5秒的轮询
    间隔误差可以忽略）。

    call_count变化（同一个node被重新调用，比如打回重试）会被当成一条新的清单条目，
    而不是原地更新旧条目——这样"被打回重新生成"这件事本身也会体现在清单历史里，
    不需要额外的"重试次数"字段。
    """

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.history: list[dict] = []
        self._started_at = datetime.now(timezone.utc)

    def observe(self, status: Optional[dict]) -> None:
        if not status:
            return
        node = status.get("node", "")
        if not node:
            return
        # 真实bug（2026-07-26飞书真实联调发现）：run_status.json是per-project的单一快照，
        # 不是per-turn的——project完成很久之后才发起的新一轮turn（比如陪伴阶段的一句无关
        # 提问），第一次poll读到的可能是上一轮（甚至上一天）遗留的旧快照，此时那个node早已
        # 跑完，不该被当成"本轮正在进行中"的新条目记进history（会显示成"正在做最后整理
        # （进行中，Xs）"这种驴唇不对马嘴的进度提示，且elapsed还是当场重新起算的，跟真实
        # 耗时毫无关系）。用"这条快照的started_at是否早于本次tracker的创建时间"判断是不是
        # 真的属于这一轮，不是的话直接忽略——没有started_at字段时（比如既有测试构造的最小
        # dict）跳过这层校验，保持向后兼容。
        started_at_raw = status.get("started_at")
        if started_at_raw:
            try:
                started_at = datetime.fromisoformat(started_at_raw)
            except ValueError:
                started_at = None
            if started_at is not None and started_at < self._started_at:
                return
        call_count = status.get("call_count", 1)
        now = datetime.now(timezone.utc)
        last = self.history[-1] if self.history else None
        if last is None or last["node"] != node or last["call_count"] != call_count:
            if last is not None and last["end"] is None:
                last["end"] = now
            self.history.append({"node": node, "call_count": call_count, "start": now, "end": None})

    def _label(self, node: str, call_count: int) -> str:
        label = _NODE_FRIENDLY_LABELS.get(node, node)
        return f"{label}（第{call_count}次）" if call_count > 1 else label

    def render(self, waiting: bool = False, trailing: str = "") -> str:
        now = datetime.now(timezone.utc)
        lines: list[str] = []
        for i, entry in enumerate(self.history):
            is_last = i == len(self.history) - 1
            label = self._label(entry["node"], entry["call_count"])
            if entry["end"] is not None:
                elapsed = _format_elapsed((entry["end"] - entry["start"]).total_seconds())
                lines.append(f"✅ {label} ({elapsed})")
            elif is_last and waiting:
                lines.append("⏸ 等待你确认")
            else:
                elapsed = _format_elapsed((now - entry["start"]).total_seconds())
                # 只对当前仍在进行中的最后一条做健康检测/当前步骤展示（已完成的历史
                # 条目不需要）——真实撞过route_draft卡在cun渠道504网关超时循环40分钟、
                # 进度提示始终是同一行没有任何变化的情况；也有真实反馈"正常运行时也
                # 应该看到步骤，不然是透明的"。两者互斥：故障提示优先级更高，命中时
                # 不再展示当前步骤（避免"正在检索"和"连续失败"同时出现造成矛盾观感）。
                trouble = _detect_retry_trouble(self.project_id, entry["node"])
                if trouble:
                    lines.append(f"▶ {label}（进行中，{elapsed}）")
                    lines.append(trouble)
                else:
                    step = _detect_current_step(self.project_id, entry["node"])
                    suffix = f" · 正在{step}" if step else ""
                    lines.append(f"▶ {label}（进行中，{elapsed}{suffix}）")

        header = _progress_header(self.project_id)
        total_elapsed = _format_elapsed((now - self._started_at).total_seconds())
        parts = []
        if header:
            parts.append(header)
        parts.append(f"⏱ 已耗时：{total_elapsed}")
        parts.append("")
        parts.append("\n".join(lines) if lines else "正在准备…")
        if trailing:
            parts.append("")
            parts.append(trailing)
        return "\n".join(parts)


def _format_interrupt_message(kind: str, payload: dict) -> str:
    question = payload.get("question", "")
    if kind == "requirements":
        return question
    if kind == "skeleton_confirm":
        summary = _format_route_plan_summary(payload.get("route_plan"))
        return f"{question}\n\n{summary}" if summary else question
    if kind == "route_structure_confirm":
        proposal = payload.get("route_change_proposal") or {}
        before = _format_route_plan_summary({"days": proposal.get("before_days", [])})
        after = _format_route_plan_summary({"days": proposal.get("proposed_days", [])})
        parts = [question, f"原因：{proposal.get('reason', '')}"]
        if before:
            parts.append(f"原安排：\n{before}")
        if after:
            parts.append(f"候选安排：\n{after}")
        return "\n\n".join(part for part in parts if part)
    if kind == "realtime_approval":
        return f"{question}\n\n建议改动：{payload.get('patch')}"
    return question or "需要你确认一下才能继续。"


def _generate_site_link(project_id: str) -> str:
    """完成后顺带生成静态展示网站——纯程序拼接、不调用LLM（见run_generate_site.py），
    够快，直接同步调用而不是丢进jobs后台任务队列（那套是给耗时/收费的任务用的）。

    网站部署直接复用本项目已有的管理后台服务（`GET /api/projects/{id}/site`，见
    src/api/routers/presentation.py），不另外接一套独立nginx静态托管——个人使用场景下，
    只要保持管理后台（uvicorn src.api.app:app --port 8000）常驻运行，这个链接就能访问到。

    生成失败不阻塞完成消息本身，只在服务端打日志方便排查（跟项目里其余"没有条件就
    优雅降级"的既有模式一致），返回空字符串，调用方据此跳过这一行。
    """
    try:
        from scripts.delivery.run_generate_site import generate_site

        generate_site(project_id)
    except Exception as exc:  # noqa: BLE001 — 生成失败不能拖累整条完成消息
        print(f"[飞书] 生成展示网站失败：{exc}")
        return ""
    return f"完整攻略网站：{FEISHU_SITE_BASE_URL}/api/projects/{project_id}/site"


def _format_completion_summary(project_id: str) -> str:
    """完成时不能只回一句"生成好了"——用户在群里看不到任何实质内容，只能一句句追问才能
    拼凑出攻略长什么样（真实联调用户反馈，2026-07-25）。这里直接把route_plan的天级摘要
    带出来，既是"结果预览"，也顺带解决了"进度消息被原地编辑覆盖、回看聊天记录看不到
    过程"的问题——这条完成消息本身就是这轮对话最终留下的历史记录（2026-07-26起，这条
    消息本身还会带着`_ProgressTracker`累积的完整阶段清单一起呈现，见_run_turn）。

    2026-07-26新增：顺带生成静态展示网站并把链接带出来（见_generate_site_link），以及
    主动询问要不要获取一张海报——没有现成图片时，海报生成是真实收费的API调用，不自动
    触发；回复由_handle_poster_offer_response处理（awaiting="poster_offer"）。

    2026-08-18新增：明确列出所有可用能力，不再只是笼统的"想看哪部分可以直接问我"。
    """
    route_plan = store.read_route_plan(project_id)
    summary = _format_route_plan_summary(route_plan.model_dump() if route_plan else None)
    example_poi = next(
        (
            poi.name.strip()
            for day in (route_plan.days if route_plan else [])
            for poi in day.pois
            if poi.name.strip()
        ),
        "这个景点",
    )
    state_validation = review_store.validate_project_state(project_id)
    force_finalized_count = len(state_validation.get("raw_force_finalized_paths") or [])

    if force_finalized_count:
        text = (
            "⚠ 攻略已生成，完成状态：completed_with_degradation。"
            f"共有 {force_finalized_count} 个 force_finalized 对象需要复核。"
        )
        if summary:
            text += f"行程大概是这样——\n\n{summary}\n"
    elif not summary:
        text = "✅ 攻略已经生成好了！完成状态：completed。"
    else:
        text = f"✅ 攻略生成好了！完成状态：completed。行程大概是这样——\n\n{summary}\n"

    # 🆕 明确列出可用能力（按使用频率排序）
    text += (
        "\n📍 **现在你可以问我：**\n"
        f"• 查看行程：「第二天怎么安排」「{example_poi}怎么走」\n"
        f"• 避坑建议：「{example_poi}有没有坑」「{example_poi}要注意什么」\n"
        "• 美食住宿：「附近有什么好吃的」\n"
        "• 实时信息：「明天天气」「这个点还开门吗」\n"
        f"• 文化历史：「介绍一下{example_poi}」\n"
        "• 设置提醒：「一小时后提醒我」「每天8点提醒看行程」\n"
        "• 修改攻略：「改一下第X天的美食」\n\n"
        "💬 随时输入「帮助」查看完整功能"
    )

    site_link = _generate_site_link(project_id)
    if site_link:
        text += f"\n\n🌐 {site_link}"

    poster_path = store.workspace_dir(project_id) / "poster.png"
    if poster_path.exists():
        text += "\n\n📷 海报已准备好，回复「要」即可发送～"
    else:
        text += "\n\n📷 需要的话我也可以生成攻略海报，回复「要」就行～"
    return text


def _result_to_message(project_id: str, mode: str, result: dict) -> tuple[str, Optional[str]]:
    """把graph.invoke()的返回值转成(要发给用户的文本, 新的awaiting值)。"""
    if result.get("_route_structure_decision_unclear"):
        return (
            "我还不能确定你的选择。请明确回复「保留原路线」「采纳候选」，"
            "或直接说明希望怎样调整候选路线。",
            "route_structure_confirm",
        )
    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        kind = _classify_interrupt_kind(payload)
        return _format_interrupt_message(kind, payload), kind

    if mode == "companion":
        answer = result.get("companion_answer") or "（没有生成回答）"

        # 🆕 添加agent标识（2026-08-18）
        if FEISHU_SHOW_AGENT_INFO:
            last_agent = result.get("last_agent_result", {})
            agent_name = last_agent.get("agent", "")
            display_name = _AGENT_DISPLAY_NAMES.get(agent_name, "")

            if display_name:
                # 检查是否有自动串联（guide + 其他agent）
                guide_answer = result.get("companion_guide_answer")
                fallback_answer = result.get("companion_fallback_answer")

                # 如果guide_answer和fallback_answer都存在，且最终answer包含两者
                # 说明是自动串联场景
                if guide_answer and fallback_answer and guide_answer in answer and fallback_answer in answer:
                    # 自动串联：在guide内容前加"随身向导"，在fallback内容前加对应agent名
                    answer = answer.replace(guide_answer, f"📖 随身向导\n\n{guide_answer}", 1)
                    answer = answer.replace(fallback_answer, f"{display_name}\n\n{fallback_answer}", 1)
                else:
                    # 单agent场景：直接在开头加标识
                    answer = f"{display_name}\n\n{answer}"

        # "_awaiting"是内部sentinel key（不是orchestrator图返回的真实字段）——
        # 陪伴模式默认下一条消息不需要等任何回复（awaiting=None），但"发起新旅程"
        # 检测/确认这两步需要在companion分支里临时设置awaiting（"new_journey_offer"
        # 或切换后新project真正interrupt出的requirements/skeleton_confirm），
        # 不能像其余companion回复一样一律回落到None，见_invoke_companion/
        # _handle_new_journey_offer_response。
        return answer, result.get("_awaiting")

    progress = store.read_progress(project_id)
    if progress.state == ProjectPhase.DONE:
        # "poster_offer"：完成消息主动问了要不要获取海报，下一条消息按这个awaiting路由
        # 到_handle_poster_offer_response，不是普通陪伴对话。
        return _format_completion_summary(project_id), "poster_offer"
    # 理论上不该出现——中间阶段（并行调研中/整合中/树木生成中）之间没有interrupt，
    # 一路跑到底要么进interrupt要么到DONE。兜底展示当前状态，避免awaiting搞不清楚。
    return f"当前进度：{progress.state.value}，请稍候。", None


def _invoke_channel(project_id: str, user_message: str, awaiting: Optional[str]) -> dict:
    if awaiting is None:
        return orchestrator.run_channel_start(project_id, user_message)
    if awaiting == "requirements":
        return orchestrator.run_channel_resume(project_id, user_message)
    if awaiting == "skeleton_confirm":
        decision = intent.classify_skeleton_decision(user_message)
        return orchestrator.run_channel_resume(project_id, decision)
    if awaiting == "route_structure_confirm":
        decision = intent.classify_route_structure_decision(user_message)
        if decision is None:
            store.log_event(
                project_id,
                actor="feishu_channel",
                action="route_structure_decision_reprompt",
                target="route_change_proposal.json",
                summary="收到新的用户消息，但无法明确分类为keep/confirm/revise，未恢复checkpoint",
            )
            return {"_route_structure_decision_unclear": True}
        store.log_event(
            project_id,
            actor="feishu_channel",
            action="route_structure_decision_classified",
            target="route_change_proposal.json",
            summary=f"新的用户消息被分类为{decision!r}，来源=feishu",
        )
        return orchestrator.run_channel_resume(project_id, decision)
    raise ValueError(f"未知的awaiting状态：{awaiting!r}，无法处理这条消息")


def _invoke_companion(project_id: str, user_message: str, awaiting: Optional[str], channel: ReplyChannel, chat_id: str) -> dict:
    """陪伴阶段（docs/ORCHESTRATION.md第3.4节）的自由对话入口，对照run_edit。

    ... 原有docstring ...
    """

    # 🆕 检测新手问题（优先级最高，比帮助命令更自然）
    newbie_triggers = [
        "你能做什么", "有什么功能", "怎么用", "能帮我什么",
        "你是谁", "你会什么", "可以做什么", "能做啥",
        "功能", "会啥", "干什么的", "能做什么"
    ]
    if any(trigger in user_message for trigger in newbie_triggers):
        return {
            "companion_answer": (
                "👋 很高兴为你服务！我可以帮你：\n\n"
                "📍 **规划行程** → 「三天成都旅游」\n"
                "🔍 **查询攻略** → 「第二天怎么安排」\n"
                "⚠️ **避坑建议** → 「这家店有没有坑」\n"
                "🍜 **美食推荐** → 「附近有什么好吃的」\n"
                "🌤️ **实时信息** → 「明天天气」\n"
                "📚 **文化背景** → 「介绍一下武侯祠」\n"
                "⏰ **设置提醒** → 「一小时后提醒我」\n"
                "✏️ **修改攻略** → 「改一下Day2美食」\n\n"
                "💬 想看详细说明，输入「**帮助**」"
            )
        }

    # 🆕 帮助命令（扩展触发词）
    if user_message.strip() in ["帮助", "help", "菜单", "?", "？", "/help"]:
        return {
            "companion_answer": (
                "📖 **完整功能列表**\n\n"
                "🔍 **查询攻略** → 「Day2怎么安排」\n"
                "查看已生成的行程、景点详情\n\n"

                "⚠️ **避坑建议** → 「XX有没有坑」\n"
                "查询踩雷信息、差评、注意事项\n\n"

                "🍜 **美食住宿** → 「附近吃什么」\n"
                "推荐餐厅、小吃、酒店\n\n"

                "🌤️ **实时信息** → 「明天天气」\n"
                "查询天气、营业状态、临时闭馆\n\n"

                "📚 **文化背景** → 「介绍一下XX」\n"
                "了解景点历史、文化故事\n\n"

                "⏰ **提醒管理** → 「1小时后提醒我」\n"
                "• 创建提醒：「X点/X分钟后提醒我做XX」\n"
                "• 重复提醒：「每天8点提醒我看行程」\n"
                "• 查看提醒：「查看我的提醒」\n"
                "• 取消提醒：「取消刚才的提醒」\n\n"

                "✏️ **修改攻略** → 「改Day2美食」\n"
                "修改行程、补充内容\n\n"

                "💡 直接问就行，我会自动识别！"
            )
        }

    # 完成消息主动问过"要不要生成海报"之后的回复，优先处理——跟其余awaiting分支同级，
    # 只是这条不需要走orchestrator图，直接同步调用生图脚本+推送图片。
    if awaiting == "poster_offer":
        return _handle_poster_offer_response(project_id, chat_id, user_message, channel)

    if awaiting == "realtime_approval":
        decision = intent.classify_realtime_approval_decision(user_message)
        return orchestrator.resume_companion(project_id, decision)

    # 发起新旅程的反问确认（2026-07-26新增，见docs/ARCHITECTURE.md第14.4节）——
    # 跟poster_offer同级，同样不直接走orchestrator图，处理逻辑见_handle_new_journey_offer_response。
    if awaiting == "new_journey_offer":
        return _handle_new_journey_offer_response(chat_id, user_message)

    # 素材采集入口（2026-07-25新增，见/grilling讨论）：只在companion模式生效，
    # 不碰主图/orchestrator.py——独立的意图判断，命中就直接fire-and-forget触发
    # jobs.trigger_collect_material（复用管理后台已有的topic级并发锁+采集入库流程），
    # 不进run_companion。返回值直接构造成companion_answer，_result_to_message的
    # companion分支不用改一行。
    if intent.classify_wants_material_collection(user_message):
        return _trigger_material_collection(project_id, user_message)

    # 记一条用户备注（docs/ARCHITECTURE.md第21节飞书写入入口，2026-08-21阶段2新增）——
    # 跟素材采集分支同款fire-and-forget性质：只在awaiting为None时跑到（前面的awaiting
    # 分支已return），命中就短路掉run_companion，不进反问状态机。用户在飞书说
    # "记一下Day2宽窄巷子这家火锅鱼要提前订"→单轮写进同一份user_notes.jsonl
    #（source="feishu"）。飞书这半只能加新不能改删（定位"哪条"难，见§21.3）。
    if intent.classify_wants_user_note(user_message):
        return _handle_user_note(project_id, user_message)

    # 发起新旅程意图检测（2026-07-26新增）——只在awaiting为None（既没有等海报/审批
    # /新旅程确认，也没有识别到素材采集意图）时才做，命中不直接切换project，只是
    # 反问一句确认（二段式：先反问再切换，误判代价只是多问一句，不会真的打断
    # 用户的正常陪伴问答）。触发消息本身暂存起来，确认后直接复用，不逼用户复述。
    if intent.classify_wants_new_journey(user_message):
        bindings.set_pending_new_journey_message(chat_id, user_message)
        return {
            "companion_answer": (
                "看起来你想规划一个新的旅程？当前这趟行程我会完整保留，随时可以问我。"
                "要开始新的旅程吗？"
            ),
            "_awaiting": "new_journey_offer",
        }

    return orchestrator.run_companion(project_id, user_message)


def _handle_new_journey_offer_response(chat_id: str, user_message: str) -> dict:
    """完成消息陪伴阶段检测到"可能想发起新旅程"后主动反问，这里处理用户的回复。

    "不确认"是保守的默认路径（跟_handle_poster_offer_response同款态度）——用户
    拒绝或回复内容无法判断为"要"时，不切换project，继续陪伴当前旅程；只有明确
    确认才真正调用`bindings.start_new_journey`切换活跃project，并直接复用触发
    这次检测时暂存的原始消息（`pending_new_journey_message`）发起新project的
    `run_channel_start`，不逼用户重新说一遍目的地/天数。

    返回值里带`_switched_project_id`时（只在真正切换成功时才有），调用方
    （_run_turn）需要据此改用新project_id去渲染最终回复/记录进度，而不是继续用
    这一轮turn最初绑定的project_id——见_run_turn里"effective_project_id"的处理。
    """
    if intent.classify_new_journey_offer_decision(user_message) != "yes":
        bindings.pop_pending_new_journey_message(chat_id)
        return {"companion_answer": "好的，那我们继续聊现在这趟旅程。有其他问题随时问我。"}

    pending_message = bindings.pop_pending_new_journey_message(chat_id) or user_message
    new_project_id = bindings.start_new_journey(chat_id)
    result = dict(orchestrator.run_channel_start(new_project_id, pending_message))
    result["_switched_project_id"] = new_project_id
    return result


def _handle_poster_offer_response(project_id: str, chat_id: str, user_message: str, channel: ReplyChannel) -> dict:
    """完成消息末尾主动问"要不要获取海报"，这里处理用户的回复。

    优先复用workspace里已有的poster.png；仅在文件不存在时才调用真实收费的生图API
    （gpt-image-2，见run_generate_poster.py），所以走"按需触发"，不在完成时自动生成——
    跟管理后台"按次收费需二次确认"的态度一致，这里"用户明确回复要"就是等效的确认
    动作。`classify_poster_offer_decision`把
    "no"放第一位、解析失败时保守返回不触发，理由同classify_wants_material_collection。

    同步调用（不丢进jobs后台任务队列）：这个函数本身已经跑在_run_turn的后台线程里
    （由handle_message的jobs.trigger丢进后台，WS事件回调线程不会被阻塞），阻塞
    40-60秒的生图请求跟graph.invoke()本身耗时分钟级是同一个道理，不需要再叠一层
    后台任务+轮询。

    真实事故（2026-08-20）：classify_poster_offer_decision内部调用轻量模型，一次
    商汤relay超时/网络异常就会让异常从这里冒泡到_run_turn的通用except分支——那个
    分支只负责把异常转成用户可读提示，不会清除awaiting（清除逻辑在_run_turn正常
    返回路径的bindings.set_awaiting那一步，异常路径根本走不到），导致这个chat的
    awaiting="poster_offer"永久卡住、后续所有消息（包括跟海报完全无关的提问）都
    会被继续送进这里重蹈覆辙。修复：分类调用失败时按跟"解析失败"同一个保守方向
    处理——当作"no"，不让异常冒出这个函数，这样无论如何这一轮turn都会正常走完
    并清掉awaiting。
    """
    try:
        decision = intent.classify_poster_offer_decision(user_message)
    except Exception:  # noqa: BLE001 — 分类调用失败不能让awaiting卡死，保守当作"no"
        decision = "no"
    if decision != "yes":
        return {"companion_answer": "好的，不生成海报。有其他问题随时问我。"}

    poster_path = store.workspace_dir(project_id) / "poster.png"
    reused_existing = poster_path.exists()
    if not reused_existing:
        from scripts.delivery.run_generate_poster import generate_poster

        try:
            poster_path = generate_poster(project_id, confirm=False)
        except Exception as exc:  # noqa: BLE001 — 收费API调用失败必须转成用户可读提示
            return {"companion_answer": f"抱歉，生成海报时出现问题：{exc}"}

    try:
        _logged_send_image(channel, chat_id, project_id, "poster_image", str(poster_path))
    except Exception as exc:  # noqa: BLE001 — 发送失败不能让用户以为海报没生成
        ready_message = "海报已准备好" if reused_existing else "海报生成好了"
        return {
            "companion_answer": (
                f"{ready_message}，但发送图片时出了点问题（{exc}），"
                f"可以去管理后台查看：{FEISHU_SITE_BASE_URL}/api/projects/{project_id}/poster"
            )
        }
    return {"companion_answer": "海报已发送！" if reused_existing else "海报生成好了！"}


def _trigger_material_collection(project_id: str, user_message: str) -> dict:
    """fire-and-forget触发一次素材采集，不等它跑完——`jobs.trigger_collect_material`
    本身跑在独立的后台线程里（复用管理后台"彻底重跑"同款jobs框架），这里立刻返回
    一句"已经在后台处理"。topic锁重复触发时给出友好提示，不能让RuntimeError冒泡到
    `_run_turn`外层通用的"网络或系统异常"兜底话术——那是"正常的忙碌状态"，不是故障。
    """
    saved = store.read_requirements(project_id)
    topic = saved["topic"] if saved else None
    if not topic:
        return {"companion_answer": "抱歉，找不到这个项目对应的目的地信息，没法帮你采集素材。"}
    try:
        jobs.trigger_collect_material(topic, user_message)
    except RuntimeError:
        return {"companion_answer": f"「{topic}」已经有一次素材采集在进行中，稍后再试试看吧。"}
    return {
        "companion_answer": f"好的，已经在后台帮你搜集『{topic}』相关的新素材了，"
                             f"通常需要几分钟，完成后可以再问我。"
    }


class _UserNoteFields(BaseModel):
    """从用户自由文本里抽取的备注定位字段（见_extract_user_note_fields）。day/poi_name
    抽不出就给null——允许天级/行程级备注（docs/ARCHITECTURE.md第21.2节两级可选绑定）。"""

    day: Optional[int] = None
    poi_name: Optional[str] = None
    note: str = ""


def _extract_user_note_fields(project_id: str, user_message: str) -> Optional[dict]:
    """飞书自由文本→{day, poi_name, note}的一次性结构化抽取（docs/ARCHITECTURE.md第21.3节）。

    项目里此前没有"自由文本→(day,poi_name)定位"的先例——现有intent.py全是N-of-1分类，
    不做实体抽取。这里用get_light_chat_model(temperature=0)+with_structured_output(method=
    "json_mode")（项目约定，见ARCHITECTURE变更记录v1.7：deepseek-v4-flash用json_mode不用
    json_schema/function_calling），把store.read_route_plan的天级+POI名摘要喂进上下文，
    让模型从用户话里抽出{day:int|null, poi_name:str|null, note:str}。day/poi_name抽不出
    给null（允许"记一下第一天要穿舒适鞋"这类天级备注、"整体节奏偏紧"这类行程级备注）。

    返回None表示抽取彻底失败（结构化调用本身就抛异常或返回内容无法解析）——调用方
    _handle_user_note据此返回"按格式重说"，不写、不报错。

    参考realtime_agent_node（orchestrator.py约2905）做结构化抽取的先例形态，但这里是一次性
    light model调用不是react-agent：realtime是react-agent多轮调用工具，这里是一次结构化invoke。
    """
    route_plan = store.read_route_plan(project_id)
    days_summary = ""
    if route_plan:
        lines = []
        for day in route_plan.days:
            pois = "、".join(p.name for p in day.pois)
            lines.append(f"Day{day.day} {day.city}：{pois}")
        days_summary = "\n".join(lines)
    context = f"当前行程的天数和景点：\n{days_summary}" if days_summary else "（行程尚未生成）"

    model = get_light_chat_model(temperature=0).with_structured_output(
        _UserNoteFields, method="json_mode"
    )
    prompt = (
        f"{context}\n\n"
        f"用户消息：{user_message}\n\n"
        "用户想记一条旅游备注。请从消息里抽取：\n"
        "- day：这条备注绑第几天（整数）；消息没提具体哪天就给null（表示整次行程级或天级备注）\n"
        "- poi_name：绑哪个景点（必须从上面行程列表里真实存在的景点名里选，原样选取不要改写）；"
        "消息没绑具体景点就给null\n"
        "- note：备注内容本身（用户真正想记的那句话，去掉'记一下''帮我记着'这类触发词）\n"
        "以JSON格式回复，包含day、poi_name、note三个字段。"
    )
    try:
        result: _UserNoteFields = model.invoke(prompt)
    except Exception as exc:  # noqa: BLE001 — 抽取失败转成"请按格式重说"，不阻断陪伴对话
        print(f"[飞书] 备注字段抽取失败：{exc}")
        return None

    data = result.model_dump()
    # poi_name不在route_plan真实景点里时降级为null（防模型编造景点名），跟设计意图一致：
    # 绑POI的注要真的绑在当前行程存在的POI上，否则渲染时就是孤儿备注退到天级。
    if data.get("poi_name"):
        valid_names = set()
        if route_plan:
            for day in route_plan.days:
                for poi in day.pois:
                    valid_names.add(poi.name)
        if data["poi_name"] not in valid_names:
            data["poi_name"] = None
    return data


def _handle_user_note(project_id: str, user_message: str) -> dict:
    """飞书"记一条备注"意图命中后的单轮fire-and-forget处理（docs/ARCHITECTURE.md第21.3节）。

    仿_trigger_material_collection：guard(确认project存在)→抽取{day,poi,note}→写
    store.write_user_note(source="feishu")→触发重建site。不进反问状态机（不set awaiting），
    抽取彻底失败或note_text为空时返回"按「Day X·景点名·备注内容」格式再说一次"，不写不报错。

    飞书这半只能"加新"不能改/删——定位"哪条"难，见§21.3"飞书自由文本…不能"一栏。改/删走
    网页表单或后台API（阶段2后台前端）。
    """
    saved = store.read_requirements(project_id)
    if not saved:
        return {"companion_answer": "抱歉，找不到这个项目的信息，没法帮你记备注。"}

    fields = _extract_user_note_fields(project_id, user_message)
    note_text = (fields or {}).get("note", "").strip() if fields else ""
    if fields is None or not note_text:
        return {
            "companion_answer": (
                "没听明白要记什么，请按「Day X·景点名·备注内容」格式再说一次，"
                "比如：记一下 Day2 宽窄巷子这家火锅鱼要提前订"
            )
        }

    day = fields.get("day")
    poi_name = fields.get("poi_name") or ""
    store.write_user_note(
        project_id, "admin_user", note_text, day=day, poi_name=poi_name, source="feishu",
    )

    # 重建site让备注在网页可见（复用_generate_site_link同款同步调用run_generate_site，
    # 纯程序拼接不调LLM，够快；失败只打日志不阻塞回复，跟完成消息里重建site同一态度）。
    try:
        from scripts.delivery.run_generate_site import generate_site

        generate_site(project_id)
    except Exception as exc:  # noqa: BLE001 — 重建失败不阻塞"已记下"这条回复
        print(f"[飞书] 记备注后重建展示网站失败：{exc}")

    position = []
    if day:
        position.append(f"Day{day}")
    if poi_name:
        position.append(poi_name)
    position_desc = " · ".join(position) if position else "这次行程"
    return {
        "companion_answer": f"已记下{position_desc}：{note_text}。网页重建后会显示在那儿。"
    }


def _start_progress_poller(
    project_id: str, chat_id: str, channel: ReplyChannel, message_id: str, tracker: _ProgressTracker, stop_event: threading.Event
) -> threading.Thread:
    def _poll() -> None:
        last_rendered: Optional[str] = None
        while not stop_event.wait(FEISHU_PROGRESS_POLL_SECONDS):
            tracker.observe(store.read_run_status(project_id))
            text = tracker.render()
            if text != last_rendered:
                try:
                    _logged_edit(channel, chat_id, project_id, "progress", message_id, text)
                except Exception:
                    pass  # 进度展示失败不应该影响主流程，静默跳过这一次更新（_logged_edit已经记过日志了）
                last_rendered = text

    thread = threading.Thread(target=_poll, daemon=True)
    thread.start()
    return thread


def _run_turn(project_id: str, chat_id: str, user_message: str, awaiting: Optional[str], channel: ReplyChannel) -> None:
    """真正执行这一轮对话——耗时的graph.invoke()调用+进度轮询都在这里，由
    handle_message丢进后台线程执行。每一跳都try/except转成中文可读提示，区分
    "业务性拒绝"（ValueError：项目状态不对/awaiting值非法）和"网络异常/系统异常"
    两种话术，不能用同一句兜底文案盖掉两种完全不同的原因（飞书接入经验文档教训②）。

    真实联调发现：轮询线程和"发最终结果"之间存在时序竞争——轮询线程的最后一次
    channel.edit()如果正好卡在网络请求里，`poller.join(timeout=1)`等不到它真正
    结束就返回，主线程随后发出的最终结果反而可能被轮询线程那次迟到的请求盖掉
    （用户会看到消息停留在"正在处理：xxx"而不是真正的最终结果，且没有任何异常
    可供排查——两次真实调用都复现过这个现象）。修复：**必须先彻底停止轮询线程
    （无超时地join，等它当前在跑的这次edit真正完成），再发最终结果**，保证
    最终结果一定是这条消息收到的最后一次编辑。

    2026-07-26：最终发出的消息不再是单独一句话，而是`tracker.render()`——累积的
    完整阶段清单 + 这一跳的实际结果（打回/确认问题、陪伴回答、完成摘要等）拼在
    一起，一条消息里同时呈现"整个过程"和"最终结果"。
    """
    message_id = _logged_send(channel, chat_id, project_id, "ack", "收到，正在处理，请稍候…")
    tracker = _ProgressTracker(project_id)
    stop_event = threading.Event()
    poller = _start_progress_poller(project_id, chat_id, channel, message_id, tracker, stop_event)

    def _stop_poller() -> None:
        stop_event.set()
        poller.join()  # 不设超时——宁可多等一次网络请求，也不能让它跟最终结果赛跑

    try:
        progress = store.read_progress(project_id)
    except Exception as exc:
        _stop_poller()
        _logged_edit(channel, chat_id, project_id, "error", message_id, f"抱歉，读取项目状态时出现问题：{exc}")
        return

    mode = "companion" if progress.state == ProjectPhase.DONE else "channel"
    try:
        if mode == "companion":
            result = _invoke_companion(project_id, user_message, awaiting, channel, chat_id)
        else:
            result = _invoke_channel(project_id, user_message, awaiting)
    except ValueError as exc:
        _stop_poller()
        _logged_edit(channel, chat_id, project_id, "error", message_id, f"抱歉，这次请求不能处理：{exc}")
        return
    except Exception as exc:  # noqa: BLE001 — 后台线程，异常必须被捕获转成用户可读提示
        _stop_poller()
        _logged_edit(channel, chat_id, project_id, "error", message_id, f"抱歉，处理时遇到网络或系统异常，请稍后重试：{exc}")
        return

    # 发起新旅程确认后（_handle_new_journey_offer_response），这一轮turn实际操作的
    # project已经从最初绑定的project_id切换成了新project——后续的进度观察/结果渲染
    # 都要用新的，不能继续沿用turn开始时的project_id。**已知局限**：进度轮询线程
    # （_start_progress_poller）在切换发生前就已经绑定了旧project_id启动，这次切换
    # 期间它读到的仍是旧project（已是DONE状态、不再变化）的run_status，不会显示新
    # project刚起步的进度——这个窗口通常很短（run_channel_start的第一跳很快就会
    # interrupt出多轮需求收集的第一个问题），影响仅限"切换那几秒钟看不到中间过程"，
    # 不影响最终结果的正确性（下面用effective_project_id渲染，保证是对的）。
    effective_project_id = result.pop("_switched_project_id", project_id)
    if effective_project_id != project_id:
        tracker.project_id = effective_project_id

    # 收尾前再观察一次最新的run_status——companion模式这类单节点、可能比一个轮询间隔
    # 还快跑完的调用，不做这一步的话tracker.history可能是空的（一次poll都没赶上）。
    tracker.observe(store.read_run_status(effective_project_id))

    text, new_awaiting = _result_to_message(effective_project_id, mode, result)
    bindings.set_awaiting(chat_id, new_awaiting)
    _stop_poller()
    waiting = "__interrupt__" in result
    _logged_edit(channel, chat_id, effective_project_id, "final_result", message_id, tracker.render(waiting=waiting, trailing=text))


def handle_message(chat_id: str, user_message: str, channel: ReplyChannel) -> None:
    """飞书渠道收到一条消息时的唯一入口（ws_client.py收到事件后调用这个函数）。
    立刻返回，真正的处理在后台线程里跑，不阻塞WS事件回调。"""
    try:
        binding = bindings.get_or_create_binding(chat_id)
    except Exception as exc:
        _logged_send(channel, chat_id, None, "error", f"抱歉，处理这条消息时出现问题（绑定项目失败）：{exc}")
        return

    # 🆕 新用户欢迎消息（2026-08-18，方案A）
    if binding.get("_is_new_user", False):
        welcome = (
            "👋 **欢迎使用旅游攻略助手！**\n\n"
            "我可以帮你：\n"
            "📍 规划多天旅游行程\n"
            "🍜 推荐美食、住宿\n"
            "⚠️ 提醒避坑事项\n"
            "⏰ 设置旅行提醒\n\n"
            "💬 试试说：**「三天成都旅游」**\n"
            "随时输入「**帮助**」查看完整功能～"
        )
        _logged_send(channel, chat_id, None, "welcome", welcome)
        # 移除标志位并保存
        bindings.clear_new_user_flag(chat_id)

    project_id = binding["active_project_id"]
    awaiting = binding.get("awaiting")

    # 真实测试中发现的bug（2026-08-18）：管理后台软删除project后（unbind_project），
    # active_project_id会被置为None，但此前这里没有做任何保护——直接把None传进
    # _run_turn/store.read_progress，最终在路径拼接（_workspace_dir）那一步才报出
    # 一句对用户完全不可读的"unsupported operand type(s) for /: 'PosixPath' and
    # 'NoneType'"。修复：复用start_new_journey自动开启一次新旅程（跟"一个群多次
    # 旅程"机制第14.4节同一套序号式命名），不能只提示不解决——否则用户照着提示重新
    # 描述目的地后，仍然会命中同一个None分支，陷入死循环。
    if project_id is None:
        project_id = bindings.start_new_journey(chat_id)
        awaiting = None

    def work_fn(log):
        _run_turn(project_id, chat_id, user_message, awaiting, channel)
        return None

    try:
        jobs.trigger(TASK_TYPE, project_id, work_fn)
    except RuntimeError:
        _logged_send(channel, chat_id, project_id, "busy_notice", "上一条消息还在处理中，请稍等它完成后再发新消息。")
