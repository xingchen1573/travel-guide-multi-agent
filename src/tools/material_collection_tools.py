"""素材agent的采集工具：search_bilibili / search_douyin / propose_candidates。

工厂函数模式：make_collection_tools(topic, captured, seen) 返回绑定好这些容器的工具列表，
和 make_material_tools（检索/追加）拼在一起绑给同一个 material_agent，见
src/agents/material_agent/agent.py。

captured/seen 由调用方（src/agents/material_agent/collection_graph.py 的
material_propose_node）传入的可变容器：
- captured：agent调用propose_candidates时提交的最终候选+摘要，供调用方在agent.invoke()
  结束后取回（react-agent本身没有直接返回结构化数据的机制，只能通过这种边车容器传出）。
- seen：跨多次search_bilibili/search_douyin调用的去重集合——agent可能换关键词/翻页
  搜索多次凑够候选数量（见docs/ISSUES.md第14条"候选不够数不能提前认怂"），不能让同一条
  视频在多轮搜索里重复出现在候选列表里。
"""
from __future__ import annotations

import json

from langchain_core.tools import tool


def make_collection_tools(topic: str, captured: dict, seen: set, platform: str = "all") -> list:
    """platform限定这次采集只允许搜哪个平台（"all"/"bilibili"/"douyin"，见管理后台采集
    对话框的显式平台选择器）——用不绑定对应搜索工具做硬过滤，不是只在prompt里提示，模型
    根本没有机会调用被排除掉的那个平台的搜索工具（真实来源见CLAUDE.md已知问题第2条，
    此前只能靠agent自己从request文字猜平台，前端也不区分展示，2026-07-26新增）。
    """
    @tool
    def search_bilibili(keywords: list[str], sort_by: str = "综合", page: int = 1) -> str:
        """搜索B站候选素材，已自动排除素材库里已有的+本轮已经搜到过的。
        sort_by可选"综合"(默认)/"最新"/"最热"。如果候选数不够用户要求的数量，
        换一批关键词或者把page加1翻页后重新调用这个工具，不要只搜一次就将就。"""
        from src.materials.collectors.bilibili import search_and_rank

        candidates = search_and_rank(keywords, top_n=15, topic=topic, sort_by=sort_by, page=page)
        new_ones = [c for c in candidates if c["bvid"] not in seen]
        seen.update(c["bvid"] for c in new_ones)
        if not new_ones:
            return "没有搜到新候选（可能都已在库中，或和之前搜到的重复）。换个关键词试试。"
        return json.dumps(
            [{**c, "platform": "bilibili"} for c in new_ones], ensure_ascii=False
        )

    @tool
    def search_douyin(keywords: list[str], sort_by: str = "综合") -> str:
        """触发一次抖音关键词搜索（可能弹出登录窗口，需要人工在旁边扫码完成登录），
        排序+去重后返回候选。同样：候选数不够就换一批关键词重新调用，不要只搜一次就将就。"""
        from src.materials.collectors.douyin import list_ranked_candidates, trigger_search

        trigger_search(keywords)
        candidates = list_ranked_candidates(topic, top_n=15, sort_by=sort_by)
        new_ones = [c for c in candidates if c["aweme_id"] not in seen]
        seen.update(c["aweme_id"] for c in new_ones)
        if not new_ones:
            return "没有搜到新候选（可能都已在库中，或和之前搜到的重复）。换个关键词试试。"
        return json.dumps(
            [{**c, "platform": "douyin"} for c in new_ones], ensure_ascii=False
        )

    @tool
    def propose_candidates(candidates: list[dict], summary: str) -> str:
        """搜集+排序去重出最终候选列表后，调用这个工具正式提交，等待人工确认后才会真正采集。
        candidates每一项须含platform("bilibili"或"douyin")/source_url/title/author/score/pubdate，
        直接从search_bilibili/search_douyin的返回结果里挑选组装，不要编造字段，也不要漏掉
        pubdate（B站候选采集时需要这个字段记录发布时间，漏了会导致采集失败）。
        summary简要说明这批候选是怎么来的（用了哪些关键词、排序方式）。
        调用这个工具后本轮任务结束，不要再调用其他工具。"""
        captured["candidates"] = candidates
        captured["summary"] = summary
        return f"已提交{len(candidates)}条候选，等待人工确认后才会真正采集。"

    tools = []
    if platform != "douyin":
        tools.append(search_bilibili)
    if platform != "bilibili":
        tools.append(search_douyin)
    tools.append(propose_candidates)
    return tools
