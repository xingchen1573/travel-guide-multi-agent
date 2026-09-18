"""公共区域数据结构定义，对应 docs/WORKSPACE.md 第2节的JSON schema。

这些模型只做数据校验/序列化，不包含任何读写逻辑（读写逻辑在 store.py）。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from travel_guide_kernel.models import (
    EvidenceClaim,
    EvidenceClaimEvidenceSnapshot,
    RouteClaim,
    RouteClaimEvidenceSnapshot,
    RouteDay,
    RouteEntities,
    RouteEntityCandidate,
    RouteEntityResolution,
    RouteExecutionPlan,
    RouteFeasibilityCheck,
    RouteFeasibilityDateContext,
    RouteFeasibilityEvidence,
    RouteFeasibilityReport,
    RouteGeoPoint,
    RouteLeg,
    RouteLegSegment,
    RouteOperationalCommitment,
    RoutePlan,
    RoutePoi,
    SourceRef,
)


class ProjectPhase(str, Enum):
    """对应 docs/WORKSPACE.md 第4节状态机的 state 枚举。"""

    REQUIREMENTS = "需求收集中"
    SKELETON_CONFIRM = "骨架确认中"
    ROUTE_PLANNING = "路线规划中"
    PARALLEL_RESEARCH = "并行调研中"
    INTEGRATING = "整合中"
    POI_DETAIL = "树木生成中"
    DONE = "完成"


# ---------- shared route contracts ----------
# Route contracts are defined by travel_guide_kernel.models and re-exported here.

# ---------- progress.json ----------


class Progress(BaseModel):
    state: ProjectPhase = ProjectPhase.REQUIREMENTS
    agents: dict[str, str] = Field(default_factory=dict)  # agent_name -> pending/in_progress/done
    skeleton_confirmed: bool = False
    material_checked: bool = False
    updated_at: str = ""


# ---------- outputs/lifestyle.json ----------

LIFESTYLE_FIELDS = ["food", "outfit", "lodging", "play"]


class LifestyleItem(BaseModel):
    day: int
    recommendation: str
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="兼容性来源并集；新写入由claims[].evidence_refs自动汇总",
    )
    claims: list[EvidenceClaim] = Field(
        default_factory=list,
        description="recommendation中具体说法到证据的事实级映射",
    )
    claimed_entities: list[str] = Field(
        default_factory=list,
        description="这条推荐里提到的具体专有名词（店名/菜名/景点名），用于核验sources里的chunk是否真的支持这个说法",
    )
    citation_status: str = "passed"  # passed / force_finalized，见docs/ISSUES.md第125条
    # 只在citation_status=force_finalized时非空：最后一轮核验未通过的具体entity列表，
    # 供前端精确到entity级展示，而不是整条recommendation笼统标注"未通过核验"。
    missing_entities: list[str] = Field(default_factory=list)


class LifestyleCategory(BaseModel):
    items: list[LifestyleItem] = Field(default_factory=list)


class LifestyleOutput(BaseModel):
    version: int = 1
    food: LifestyleCategory = Field(default_factory=LifestyleCategory)
    outfit: LifestyleCategory = Field(default_factory=LifestyleCategory)
    lodging: LifestyleCategory = Field(default_factory=LifestyleCategory)
    play: LifestyleCategory = Field(default_factory=LifestyleCategory)
    last_updated_field: Optional[str] = None
    updated_at: str = ""


# ---------- outputs/pitfall.json（避雷agent写，见docs/WORKSPACE.md第2.3.2节）----------
# 结构跟LifestyleOutput对称——避雷agent针对衣食住行agent已产出的具体推荐（某餐厅/某住宿/
# 某景点）做针对性检索，跟四个lifestyle字段一一对应，不是独立的新维度。见ARCHITECTURE.md第7节。


class PitfallItem(BaseModel):
    field: str  # 对应lifestyle的哪个字段：food/outfit/lodging/play
    day: int
    target_recommendation: str  # 针对衣食住行agent的哪条具体推荐做的避雷检索，留痕供人工核对匹配是否合理
    warning: str = ""  # 避雷内容本身：具体踩坑反馈。没查到就留空，不强行凑内容
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="兼容性来源并集；新写入由claims[].evidence_refs自动汇总",
    )
    claims: list[EvidenceClaim] = Field(
        default_factory=list,
        description="warning中具体说法到证据的事实级映射；空warning可以为空",
    )
    claimed_entities: list[str] = Field(
        default_factory=list,
        description="避雷内容里提到的具体专有名词，用于核验sources里的chunk是否真的支持这个说法",
    )
    citation_status: str = "passed"  # passed / force_finalized，见docs/ISSUES.md第125条
    # 只在citation_status=force_finalized时非空：最后一轮核验未通过的具体entity列表，
    # 供前端精确到entity级展示，而不是整条warning笼统标注"未通过核验"。
    missing_entities: list[str] = Field(default_factory=list)


class PitfallCategory(BaseModel):
    items: list[PitfallItem] = Field(default_factory=list)


class PitfallOutput(BaseModel):
    version: int = 1
    food: PitfallCategory = Field(default_factory=PitfallCategory)
    outfit: PitfallCategory = Field(default_factory=PitfallCategory)
    lodging: PitfallCategory = Field(default_factory=PitfallCategory)
    play: PitfallCategory = Field(default_factory=PitfallCategory)
    updated_at: str = ""


# ---------- outputs/culture.json（文化科普agent写，见docs/WORKSPACE.md第2.3.3节）----------
# 累加式历史记录：跟lifestyle/pitfall那种"按固定字段整份覆盖写"不同，culture_agent每次
# 回答一个新问题，都要把已有items读出来+追加新的一条再整份传回，不是store层做追加逻辑，
# 见src/agents/culture_agent/prompt.md。


class CultureItem(BaseModel):
    question: str
    answer: str
    references: list[str] = Field(
        default_factory=list, description="web_search返回结果里的来源网页URL，不是素材库chunk_id"
    )
    answered_at: str = ""


class CultureOutput(BaseModel):
    version: int = 1
    items: list[CultureItem] = Field(default_factory=list)
    updated_at: str = ""


# ---------- outputs/poi_details/day{N}_{poi_slug}.json ----------
# 见 docs/WORKSPACE.md 第2.3.1节


class RouteSegment(BaseModel):
    order: int
    point_name: str
    highlight: str = ""
    suggested_duration: str = ""
    image_prompt: str = ""
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="兼容性来源并集；新写入由claims[].evidence_refs自动汇总",
    )
    claims: list[EvidenceClaim] = Field(
        default_factory=list,
        description="point_name/highlight/suggested_duration到证据的事实级映射",
    )
    # 管理后台人工上传的真实途经点照片，相对workspace/的路径（如
    # "outputs/poi_photos/day2_青城山/seg_前山经典线_1.jpg"）。空字符串=未上传，
    # 渲染时保留占位框。见travel_guide_kernel/store.py的write_poi_segment_photo。
    uploaded_photo_path: str = ""


class RouteOption(BaseModel):
    route_name: str
    suitable_for: str = ""
    recommended: bool = False
    recommend_reason: Optional[str] = None
    route_segments: list[RouteSegment] = Field(default_factory=list)


class PoiDetailFile(BaseModel):
    poi_name: str
    day: int
    routes: list[RouteOption] = Field(default_factory=list)
    needs_detail_reason: str = ""
    citation_status: str = "passed"  # passed / force_finalized
    evidence_gap: bool = False
    human_review_required: bool = False
    # 只在citation_status=force_finalized时可能非空：docs/ARCHITECTURE.md第16节新增——
    # 判定为真缺且项目级自动补充采集预算未耗尽时，已经真实下载/转写/入库成功的条目（不是
    # 候选，是已经真的进了素材库的内容）。见store.mark_poi_force_finalize的说明。
    # 每条：{title, author, source_url}。
    auto_collected: list[dict] = Field(default_factory=list)
    # 只在citation_status=force_finalized时非空：_run_poi_detail_citation_loop每一轮
    # 核验打回的具体理由（此前只用于生成给模型看的重试prompt，用完即焚、不落盘，事后
    # 完全无法查证"每一轮到底因为什么被打回"，只能靠重新跑一遍验证去反推，见docs/ISSUES.md
    # 第98条）。每条是一轮的完整打回原因文本（跟fix_prompt里的summary同源）。
    retry_history: list[str] = Field(default_factory=list)
    # 管理后台人工上传的POI整体真实照片，相对workspace/的路径（如
    # "outputs/poi_photos/day2_青城山/overview.jpg"）。空字符串=未上传，生成站点时
    # 回退到高德自动查图（src/tools/poi_photo.py）。优先级见run_generate_site.py的
    # _attach_poi_overview_photos。见travel_guide_kernel/store.py的write_poi_overview_photo。
    uploaded_overview_photo_path: str = ""
    created_at: str = ""
    updated_at: str = ""


class PoiDetailManifestEntry(BaseModel):
    poi_name: str
    day: int
    file: str
    citation_status: str = "passed"
    # generation_failed时区分渠道/工具失败与未履行写入契约；正常条目保持为空。
    failure_kind: Optional[
        Literal["provider_or_tool_failure", "write_contract_failure"]
    ] = None
    failure_summary: str = ""
    generation_mode: Literal["normal", "provider_recovery"] = "normal"
    recovery_trigger: Optional[Literal["provider_retry_exhausted"]] = None


class PoiDetailManifest(BaseModel):
    generated: list[PoiDetailManifestEntry] = Field(default_factory=list)
    updated_at: str = ""


# ---------- outputs/user_notes.jsonl（用户备注/annotation layer，docs/ARCHITECTURE.md第21节） ----------


class UserNote(BaseModel):
    """可见标注层，不是正文重写——不进citation核验、不变成攻略正文，见ARCHITECTURE.md第21.1节。
    day/poi_name两级可选绑定：只填day=天级；都填=景点级；都不填=行程级。"""

    note_id: str
    day: Optional[int] = None
    poi_name: str = ""
    note_text: str
    source: Literal["web", "feishu", "admin"] = "web"
    created_at: str = ""
    superseded_by: Optional[str] = None
    deleted: bool = False


# ---------- final_guide.meta.json ----------


class ReferenceEntry(BaseModel):
    platform: str
    author: str = ""
    doc_id: str = ""
    chunk_id: str = ""  # 具体片段，不只是doc_id；见docs/ISSUES.md第8条——references由程序生成，不是模型撰写
    source_url: str = ""
    scope: str = "global_core"
    cited_in: list[str] = Field(default_factory=list)
    excerpt: str = ""


class FinalGuideClaimSnapshot(BaseModel):
    """最终正文块写入时固化的上游事实快照。"""

    claim_id: str
    source_path: str
    usage_key: str = ""
    claim_text: str
    claim_type: str = "other"
    support_mode: Literal[
        "direct", "amap", "derived", "estimate", "user_constraint", "unverified"
    ] = "unverified"
    evidence_refs: list[SourceRef] = Field(default_factory=list)


class FinalGuideBlock(BaseModel):
    """整合Agent提交的最小可追溯正文块。"""

    block_id: str = ""  # 由程序按正文和映射内容确定性生成
    section: str
    markdown: str
    mapping_mode: Literal["upstream", "editorial", "user_constraint", "unverified"] = "upstream"
    mapping_note: str = ""
    upstream_claim_ids: list[str] = Field(default_factory=list)
    claim_snapshots: list[FinalGuideClaimSnapshot] = Field(default_factory=list)


class FinalGuideBlocks(BaseModel):
    schema_version: str = "final-guide-blocks-v1"
    blocks: list[FinalGuideBlock] = Field(default_factory=list)
    rendered_body_sha256: str = ""
    created_at: str = ""
    updated_at: str = ""


class FinalGuideWarning(BaseModel):
    """由最终 Join 从已核验运营检查确定性投影的最小提醒。"""

    day: int
    poi_name: str
    warning_type: Literal[
        "operational_uncertainty",
        "operational_restriction",
        "verification_incomplete",
    ]
    message: str
    evidence_refs: list[str] = Field(default_factory=list)


class FinalGuideMeta(BaseModel):
    version: int = 1
    merged_fields: list[str] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)
    checklist_included: bool = False
    references: list[ReferenceEntry] = Field(default_factory=list)
    warnings: list[FinalGuideWarning] = Field(default_factory=list)
    contract_validation: dict = Field(default_factory=dict)
    body_source: Literal["legacy_markdown", "blocks_v1"] = "legacy_markdown"
    blocks_schema_version: str = ""
    blocks_count: int = 0
    mapped_claim_count: int = 0
    updated_at: str = ""


# ---------- conflicts.jsonl ----------


class ConflictRecord(BaseModel):
    id: str
    reported_by: str
    target: str  # 形如 "route_agent" 或 "lifestyle_agent.food"
    reason: str
    # 核验循环里join成reason整句之前的结构化issues列表（可选，向后兼容旧记录）：
    # 管理后台时间线用它分行展示，避免reason那句"；".join的长句子挤在一起看不清。
    issues: list[dict] = Field(default_factory=list)
    status: str = "pending"  # pending / resolved / force_finalized
    retry_count: int = 0
    created_at: str = ""
    # 结构化的问题类型标签（如"route_edit_overreach"），供管理后台按类型聚合统计触发频率，
    # 不用再靠解析reason文本判断类型（docs/ARCHITECTURE.md第19.2节）。可选，默认空字符串，
    # 兼容历史记录没有这个字段的情况。
    issue_type: str = ""


# ---------- changelog.jsonl ----------


class ChangelogEntry(BaseModel):
    timestamp: str
    actor: str
    action: str
    target: str
    summary: str = ""


# ---------- data/material_library/index/<主题>/entries.jsonl ----------


class MaterialEntry(BaseModel):
    doc_id: str
    topic: str
    source_file: str
    platform: str
    author: str = ""
    source_url: str = ""
    publish_date: str = ""
    title: str = ""
    is_core: bool = True
    entities: list[str] = Field(default_factory=list)


# ---------- data/material_library/index/<主题>/chunks.jsonl ----------


class MaterialChunk(BaseModel):
    chunk_id: str
    doc_id: str
    topic: str
    order: int
    text: str
    embedding_model: str = ""
    embedded_at: str = ""


# ---------- data/material_library/index/<主题>/insights.json ----------


class InsightSourceRef(BaseModel):
    doc_id: str
    chunk_id: str
    author: str = ""
    excerpt: str = ""


class InsightItem(BaseModel):
    summary: str
    confidence: Literal["consensus", "single_source"]
    sources: list[InsightSourceRef] = Field(default_factory=list)


class TopicInsights(BaseModel):
    topic: str
    generated_at: str = ""
    model: str = ""
    based_on_entry_count: int = 0
    must_visit: list[InsightItem] = Field(default_factory=list)
    food: list[InsightItem] = Field(default_factory=list)
    pitfalls: list[InsightItem] = Field(default_factory=list)
    tips: list[InsightItem] = Field(default_factory=list)
    other: list[InsightItem] = Field(default_factory=list)
