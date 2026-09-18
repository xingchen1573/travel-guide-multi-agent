"""自然语言→confirm/revise/approve/reject的意图分类+resume值拼装。

照抄src/orchestrator.py的_classify_companion_intent同款"一次LLM调用，从候选里选一个，
解析失败兜底第一个"模式（不新增第二套判断逻辑）——这也是飞书接入经验文档记录的架构
决策：HITL走纯对话式判断，不做卡片按钮，交互复杂度让位给语言理解。
"""
from __future__ import annotations

from src.config import get_light_chat_model

_SKELETON_CONFIRM_HINTS = {
    "confirm": "用户表示同意/确认这份行程骨架，没有提出任何修改要求",
    "revise": "用户提出了具体的修改意见，想调整骨架里的某些内容",
}

# 已确认骨架后的结构变更比首次骨架更敏感。这里不允许使用_classify的默认首项兜底：
# 模糊回复或分类失败必须让渠道重新询问，不能把它代理成keep/confirm。
_ROUTE_STRUCTURE_CONFIRM_HINTS = {
    "keep": "用户要求保留原路线、不同意应用这次结构调整候选",
    "confirm": "用户明确同意应用这次路线结构调整候选",
    "revise": "用户不直接接受当前候选，并提出了具体调整意见",
    "unclear": "用户没有明确选择，或只说好、嗯、就这样等无法判断保留还是采纳的话",
}

_REALTIME_APPROVAL_HINTS = {
    "approve": "用户同意/批准这次修改",
    "reject": "用户不同意/拒绝这次修改",
}

# "no"放在第一位——_classify解析失败时兜底返回candidates[0]，这里希望解析失败时
# 保守地不触发采集（误触发的代价是真实的B站/抖音网络请求+topic锁占用，不是免费的），
# 跟_SKELETON_CONFIRM_HINTS/_REALTIME_APPROVAL_HINTS"解析失败兜底成最常见/最安全的
# 选项"不是同一个安全方向，这里明确选"不触发"作为更安全的默认值。
_MATERIAL_COLLECTION_HINTS = {
    "no": "用户只是在正常对话、提问，或者聊行程/景点相关的其它话题，"
          "没有明确要求系统去补充/采集新的素材内容",
    "yes": "用户明确要求系统去搜索/采集/补充新的素材内容"
           "（比如'帮我再搜点素材''去B站找几条最新攻略''补充几条抖音的内容'）",
}

# 完成消息主动问"要不要生成海报"之后的回复分类。"no"同样放第一位——生成海报是
# gpt-image-2真实收费的API调用（见run_generate_poster.py），误触发的代价是真实扣费，
# 解析失败时应该保守地不生成，跟_MATERIAL_COLLECTION_HINTS同一个安全方向。
_POSTER_OFFER_HINTS = {
    "no": "用户暂时不需要生成海报，或者在问别的问题、没有明确同意生成",
    "yes": "用户明确同意/想要生成一张海报（比如'要''好的''发一张吧'）",
}

# 真实事故（2026-08-20）：用户在awaiting="poster_offer"期间问了句完全无关的
# "第二天的行程是怎么安排的？"，被送进classify_poster_offer_decision的LLM调用，
# 恰好撞上商汤relay慢/超时，整轮turn卡了十几分钟没有响应——这类问题本该在这里
# 零延迟判"no"，不需要为一个明显不含"要不要海报"语义的问句去等一次LLM调用。
# 关键词只做"明显不是在回答海报问题"的粗筛，不判定"是"，避免快速通道本身
# 引入新的误判——命中就直接判"no"（跟_POSTER_OFFER_HINTS的默认保守方向一致），
# 不命中的落回原有LLM分类，行为不变。
_POSTER_OFFER_NO_FASTPATH_KEYWORDS = (
    "行程", "安排", "怎么走", "怎么去", "路线", "第", "天", "景点", "美食",
    "住宿", "天气", "提醒", "门票", "怎么玩", "推荐", "介绍",
)


def _looks_like_poster_offer_no(user_message: str) -> bool:
    text = user_message.strip()
    if not text:
        return False
    if any(k in text for k in ("海报", "图片", "生成图")):
        return False  # 明确提到海报/图片，交给LLM正常判断，不走快速通道
    return any(k in text for k in _POSTER_OFFER_NO_FASTPATH_KEYWORDS)

# 陪伴模式下判断这句话是不是想让系统"记一条备注/备忘"（docs/ARCHITECTURE.md第21节
# 飞书写入入口）。"no"放第一位——跟_MATERIAL_COLLECTION_HINTS同一个安全方向：命中的
# 代价是一次带route_plan上下文的结构化抽取LLM调用+写入+重建site，不是免费的，
# 解析失败/语义模糊时应该保守地不触发，误判会打断正常的陪伴问答体验。命中后走单轮
# fire-and-forget写进user_notes.jsonl（source="feishu"），不进反问状态机，跟
# classify_wants_material_collection同款性质，见service._handle_user_note。
_USER_NOTE_HINTS = {
    "no": "用户只是在正常对话、提问、查信息、要求改攻略正文……"
          "没有明确要求'记一条备注/备忘/笔记'",
    "yes": "用户明确要求记一条备注/备忘/笔记，"
           "比如'记一下…''帮我记着…''备忘：…''提醒我这次记一下…'",
}


def classify_wants_user_note(user_message: str) -> bool:
    """判断陪伴模式下这句话是不是想让系统记一条用户备注（不是查信息、不是改正文）。

    命中走单轮fire-and-forget：light model分类命中→再一次带route_plan上下文的结构化
    抽取{day,poi,note}→store.write_user_note(source="feishu")→重建site，全程不进
    awaiting状态机（见docs/ARCHITECTURE.md第21.3节"飞书走单轮fire-and-forget"）。
    误判代价是一次LLM调用+重建，且会打断正常陪伴问答，所以用LLM分类而不是关键词，
    也是_USER_NOTE_HINTS把"no"放第一位、解析失败保守返回False的原因，跟
    classify_wants_material_collection同一个安全方向。
    """
    return _classify(user_message, _USER_NOTE_HINTS) == "yes"


# 陪伴模式下判断这句话是不是想发起一次全新的旅程（docs/ARCHITECTURE.md第14.4节，
# `/grilling`访谈定案）。"no"放第一位——误判的代价是打断正常的陪伴问答体验（比如
# 用户只是随口问"大理好玩吗"这类已经能被realtime_agent/culture_agent正常回答的问题，
# 不该被拽进"是否要开始新旅程"这层反问），解析模糊/不确定时应该保守不触发，
# 跟_MATERIAL_COLLECTION_HINTS/_POSTER_OFFER_HINTS同一个安全方向。命中只会触发一次
# 反问确认（见_NEW_JOURNEY_OFFER_HINTS），不直接切换project，代价可控。
_NEW_JOURNEY_HINTS = {
    "no": "用户在正常提问、聊已有行程相关的内容，或者只是随口问起某个地方的信息"
          "（比如'大理好玩吗''这个季节热不热''附近有什么好吃的'），"
          "并没有明确表达要重新规划一次新的旅游行程",
    "yes": "用户明确表达想要规划一次新的、不同的旅游行程"
           "（比如提到具体天数、说'帮我规划''这次/下次想去...''重新安排一次旅行'等措辞）",
}

# "要不要开始新旅程"这次反问确认的回复分类，跟_POSTER_OFFER_HINTS同款"no放第一位、
# 解析失败保守不触发"模式——误触发会让用户被拽进多轮需求收集，代价不小。
_NEW_JOURNEY_OFFER_HINTS = {
    "no": "用户暂时不想开始新旅程，或者在问别的问题、没有明确同意",
    "yes": "用户明确同意/想要开始一次新的旅程（比如'要''对''开始吧''是的'）",
}


def _classify(
    user_message: str,
    hints: dict[str, str],
    *,
    exact: bool = False,
    fallback_to_first: bool = True,
) -> str | None:
    candidates = list(hints.keys())
    # N选1分类，不涉及内容生成/核验，用轻量模型（见CLAUDE.md模型分层讨论）。
    model = get_light_chat_model(temperature=0)
    options = "\n".join(f"- {c}：{hints[c]}" for c in candidates)
    prompt = (
        f"用户消息：{user_message}\n"
        f"请判断这句话最应该归入下面哪一类，只回答类别名本身（如「{candidates[0]}」），"
        f"不要输出多余文字：\n{options}"
    )
    response = model.invoke([{"role": "user", "content": prompt}])
    text = (response.content or "").strip()
    if exact:
        if text in candidates:
            return text
        return candidates[0] if fallback_to_first else None
    for c in candidates:
        if c in text:
            return c
    return candidates[0] if fallback_to_first else None


def classify_skeleton_decision(user_message: str) -> str:
    """把用户对骨架的自然语言回复，转成await_skeleton_confirm_node认识的resume值：
    "confirm" 或 "revise:<意见>"（revise分支把用户原话整段带上，不只是意图标签，
    因为route_draft_node需要具体的调整意见才能重新生成，见docs/orchestrator.py）。"""
    intent = _classify(user_message, _SKELETON_CONFIRM_HINTS)
    if intent == "revise":
        return f"revise:{user_message}"
    return "confirm"


def classify_route_structure_decision(user_message: str) -> str | None:
    """把确认后结构候选的明确回复转成confirm/keep/revise:<意见>。

    返回None表示模型调用失败或输出不是唯一、精确的类别名；调用方必须重新询问，
    不得把这种不确定性代理成keep或confirm。
    """
    try:
        decision = _classify(
            user_message,
            _ROUTE_STRUCTURE_CONFIRM_HINTS,
            exact=True,
            fallback_to_first=False,
        )
    except Exception:  # noqa: BLE001 - 路线决策分类失败必须回到用户边界
        return None
    if decision is None:
        return None
    if decision == "unclear":
        return None
    if decision == "revise":
        return f"revise:{user_message}"
    return decision


def classify_realtime_approval_decision(user_message: str) -> str:
    """把用户对实时agent写入patch的自然语言回复，转成await_realtime_approval_node
    认识的resume值："approve" 或 "reject"。"""
    return _classify(user_message, _REALTIME_APPROVAL_HINTS)


def classify_wants_material_collection(user_message: str) -> bool:
    """判断陪伴模式下这句话是不是想让系统去补充/采集新素材（不是查询已有内容）。

    命中会真的触发一次B站/抖音搜索+下载+入库（`jobs.trigger_collect_material`），
    有真实网络开销，且按topic加了并发锁——误判代价不小，所以用LLM分类而不是简单
    关键词匹配，也是_MATERIAL_COLLECTION_HINTS把"no"放第一位、解析失败时保守
    返回False的原因。见docs/ISSUES.md、CLAUDE.md已知问题第10条相关讨论。
    """
    return _classify(user_message, _MATERIAL_COLLECTION_HINTS) == "yes"


def classify_poster_offer_decision(user_message: str) -> str:
    """把用户对"要不要生成海报"这次主动询问的自然语言回复，转成"yes"/"no"。
    见service._handle_poster_offer_response。命中_looks_like_poster_offer_no的
    消息直接判"no"，不调用LLM（见该函数注释，2026-08-20真实事故修复）。"""
    if _looks_like_poster_offer_no(user_message):
        return "no"
    return _classify(user_message, _POSTER_OFFER_HINTS)


def classify_wants_new_journey(user_message: str) -> bool:
    """判断陪伴模式下这句话是不是想发起一次全新的旅程（不是问已有行程/某地信息）。

    命中只会触发一次反问确认（不直接切换project，见service._invoke_companion），
    但边界仍然要偏保守——误判的代价是打断正常的陪伴问答体验，跟
    classify_wants_material_collection/classify_poster_offer_decision同一个
    "no放第一位、解析失败兜底不触发"的方向。见docs/ARCHITECTURE.md第14.4节。
    """
    return _classify(user_message, _NEW_JOURNEY_HINTS) == "yes"


def classify_new_journey_offer_decision(user_message: str) -> str:
    """把用户对"要不要开始新旅程"这次反问确认的自然语言回复，转成"yes"/"no"。
    见service._handle_new_journey_offer_response。"""
    return _classify(user_message, _NEW_JOURNEY_OFFER_HINTS)
