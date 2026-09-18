"""素材库相关工具：全局核心素材检索 + Project 补充证据。

工厂函数模式：make_material_tools(topic, project_id) 返回绑定好 topic/project_id 的工具列表，
调用方(actor)由构建agent时决定给哪个agent绑定哪些工具，不依赖LLM自己声明身份。

captured/seen 不为None时，额外拼上采集工具（search_bilibili/search_douyin/propose_candidates，
见 src/tools/material_collection_tools.py），供"素材agent"的采集流程使用——检索和采集是
同一个agent身份的两类能力，不是两个agent。platform（"all"/"bilibili"/"douyin"）透传给
make_collection_tools做工具级硬过滤，见该模块docstring。
"""
from __future__ import annotations

import hashlib
import json

from langchain_core.tools import tool

from src.materials.project_evidence import write_project_evidence
from src.materials.research_service import (
    retrieve_context,
    retrieve_project_evidence_context,
)
from src.workspace import store

# Compatibility alias for tests and older extensions that patched the former
# direct retrieval adapter. Production calls go through research_service.
from src.materials.retrieve import retrieve_materials as _retrieve


def make_material_tools(
    topic: str,
    project_id: str,
    captured: dict | None = None,
    seen: set | None = None,
    platform: str = "all",
    *,
    actor: str = "unknown_agent",
    log_scope: str = "",
    allow_append_evidence: bool = False,
    research_target_key: str | None = None,
    target_entity: str | None = None,
) -> list:
    stage = {
        "route_agent": "route",
        "lifestyle_agent": "lifestyle",
        "pitfall_agent": "pitfall",
        "poi_detail_agent": "poi_detail",
    }.get(actor, "material")

    @tool
    def retrieve_materials(
        query: str,
        top_k: int = 8,
        field: str = "",
        day: int = 0,
    ) -> str:
        """检索素材库，返回和query相关的素材片段（含平台/作者/doc_id/chunk_id）。
        写建议时，sources字段要带上具体的chunk_id（不只是doc_id），这样才能追溯到
        是原文里哪一段话支撑了这条建议，不要只写文档级来源。
        如果结果scope为project_supplement，还必须把scope复制到sources；
        有source_url时也必须一并复制，最终参考来源表才能展示原始链接。

        query措辞建议：如果你要为某个具体的专有名词（店名/菜名/景点名）找依据，
        请只用这个名词本身单独检索一次，不要和当天其他多个话题混在一起写成一句
        笼统的query——混合检索容易把真正含答案的片段挤出结果之外（真实排查过一个
        案例：某个具体点心名称真实存在于素材库，但用"某天美食+多个景点"这种笼统
        query一次性检索时完全查不到，改成只用该点心名称单独检索后稳定排第一）。"""
        resolved_target_key = research_target_key or log_scope or f"{stage}:unscoped"
        target_sha256 = None
        if actor == "pitfall_agent" and field in {"food", "outfit", "lodging", "play"} and day > 0:
            lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
            recommendation = next(
                (
                    str(item.get("recommendation") or "")
                    for item in (lifestyle.get(field) or {}).get("items", []) or []
                    if item.get("day") == day
                ),
                "",
            )
            if recommendation:
                resolved_target_key = f"pitfall:{field}:day:{day}"
                target_sha256 = hashlib.sha256(
                    recommendation.encode("utf-8")
                ).hexdigest()
        response = retrieve_context(
            project_id,
            actor=actor,
            stage=stage,
            target_key=resolved_target_key,
            query=query,
            top_k=top_k,
            target_sha256=target_sha256,
            target_entity=target_entity,
        )
        results = response["results"]
        research = response["research"]
        if not results:
            if research.get("status") == "error":
                return (
                    "素材检索未完成，不能把本次技术失败解释为没有相关素材。"
                    "请保留未知状态并向编排器报告："
                    + json.dumps(research.get("scope_errors") or {}, ensure_ascii=False)
                )
            return "没有检索到相关素材。"
        rendered = json.dumps(results, ensure_ascii=False, indent=2)
        if research.get("scope_errors"):
            return (
                "部分素材scope检索失败；以下结果仍可使用，但失败scope不能被解释为无证据："
                + json.dumps(research["scope_errors"], ensure_ascii=False)
                + "\n"
                + rendered
            )
        return rendered

    @tool
    def retrieve_project_evidence(query: str, top_k: int = 5) -> str:
        """检索当前工作单元已经保存的Web证据。

        该通道不占用UGC素材的top_k，只用于开放、预约、票价、临时管制等时效事实。
        返回记录已按当前log_scope隔离；不要把其他工作单元的Evidence直接当作当前POI证据。
        """
        resolved_target_key = research_target_key or log_scope or f"{stage}:unscoped"
        response = retrieve_project_evidence_context(
            project_id,
            actor=actor,
            stage=stage,
            target_key=resolved_target_key,
            log_scope=log_scope,
            query=query,
            top_k=top_k,
        )
        results = response["results"]
        research = response["research"]
        if not results:
            if research.get("status") == "error":
                return (
                    "Project Evidence检索未完成，不能解释为没有Web证据："
                    + json.dumps(research.get("error") or {}, ensure_ascii=False)
                )
            return "当前工作单元没有检索到相关Project Evidence。"
        return json.dumps(results, ensure_ascii=False, indent=2)

    @tool
    def append_material(
        summary: str,
        source_url: str,
    ) -> str:
        """保存一条仅当前Project可检索、可引用的补充证据。

        summary是Agent对来源的检索摘要，只用于召回和展示，不作为引用原文。source_url
        必填，而且必须精确匹配本Agent本项目已执行web_search留下的结构化结果；系统会从
        对应结果提取原始excerpt作为唯一引用依据。没有可靠来源的推断、提醒和中间结论不能
        调用本工具，应使用record_finding保存为working_note。
        """
        try:
            evidence = write_project_evidence(
                project_id,
                actor=actor,
                log_scope=log_scope,
                summary=summary,
                source_url=source_url,
            )
        except ValueError as exc:
            return f"保存Project补充证据失败：{exc}。无来源内容请改用record_finding。"
        evidence_ref = {
            "platform": "project_evidence",
            "doc_id": evidence.note_id,
            "chunk_id": f"project-evidence-{evidence.note_id}",
            "scope": "project",
            "source_url": evidence.source_url,
        }
        return (
            "已保存Project补充证据。后续claims[].evidence_refs请直接复制以下规范引用，"
            "不要把磁盘文件名evidence-*.json当作chunk_id：\n"
            + json.dumps(evidence_ref, ensure_ascii=False)
        )

    tools = [retrieve_materials]
    if allow_append_evidence:
        tools.extend([retrieve_project_evidence, append_material])
    if captured is not None and seen is not None:
        from src.tools.material_collection_tools import make_collection_tools

        tools += make_collection_tools(topic, captured, seen, platform=platform)
    return tools
