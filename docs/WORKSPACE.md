# 公共区域与工作空间设计（Workspace Schema）

> 本文档是 `ARCHITECTURE.md` 第5/8/9节的落地版：具体到目录结构、文件字段schema、文件级写入权限。所有对下述文件的读写都必须经过 `travel_guide_kernel/store.py` 提供的接口（旧 `src.workspace.store` 为兼容入口），禁止agent直接操作文件系统。状态流转/打回/级联的具体路由逻辑（依赖表、LangGraph图结构）见 [`docs/ORCHESTRATION.md`](ORCHESTRATION.md)，本文档只定义数据长什么样，不定义谁在什么时候读写它。

---

## 1. 目录结构

### 1.1 全局目录

```
项目：旅游多agent/
├── projects/<project_id>/     # 每个旅游攻略实例（独立项目）
├── data/material_library/     # 全局共享素材库，跨项目复用，核心素材agent维护
    ├── raw/<主题>/              # 原始素材（人工收集 / 未来API或爬虫产出）；主题文件夹名=目的地名称本身（如"成都"），
    │                            # 与project的requirements.destination字段直接映射，检索靠这个字段隔离，不靠project_id
    └── index/<主题>/            # 核心素材agent处理后的结构化索引，与raw/按主题一一配套，程序生成不要手改
├── runtime/review_index/      # 跨project复核聚合索引；可从各project事实重建，不是业务事实源
└── runtime/experiment_index.sqlite3 # 实验与Comparison Pair派生索引；可重建，不是实验登记事实源
```

`data/material_library/` 的详细收录规范（manifest.json字段、raw/index主题配套关系、原文/导读命名约定、raw→index处理流程）见 [`data/material_library/README.md`](../data/material_library/README.md)。

实验登记事实位于`src/experiments/catalog.json`和各Project的
`workspace/experiment.json`。`experiment-catalog-v2`中的`comparison_pairs`是正式双Harness对照的
唯一登记入口：每条必须分别给出一个`codex_project_id`和一个`langgraph_project_id`，可选记录
`source_input_project_id/comparison_scope/declared_grade/evidence_refs`。普通`comparison_peer`只表达
历史谱系，不能替代Pair，也不能单独证明反哺或验证已完成。

Pair API 会在读取时将两侧 Project 已登记的活跃`review_findings.jsonl`和共享阶段状态投影为
`observed_issues`，用于展示“同案中实际观察到了什么”。该投影不写回实验目录，也不等同于
Improvement Signal 或 Optimization Candidate；无登记 Finding 不代表已验证无问题，阶段差异也
不能在模型、Provider或运行条件不同的情况下单独证明 Harness 根因。

### 1.2 单个project内部结构

```
projects/<project_id>/
├── workspace/                 # 公共区域（对应ARCHITECTURE.md图中"路线规划区/各agent进度/各agent产出/最终攻略"）
│   ├── route_plan.json        # 路线规划区
│   ├── route_entities.json    # 与路线结构绑定的地图实体解析结果
│   ├── route_execution.json   # 路段交通方式和跨日运营措施
│   ├── route_map_validation.json # 确定性实体/相邻路段地图核验报告
│   ├── route_feasibility.json # 路线可执行性核验报告（系统checker写）
│   ├── evidence_checks.json  # 关键产物的引用证据和checker解释投影（系统写）
│   ├── route_change_proposal.json # 用户确认后的结构调整候选；pending时不修改正式路线
│   ├── progress.json          # 各agent进度 / 项目状态机当前状态
│   ├── outputs/
│   │   ├── lifestyle.json     # 衣食住行agent产出（food/outfit/lodging/play）
│   │   ├── pitfall.json       # 避雷agent产出（food/outfit/lodging/play，2026-07-23新增，见ARCHITECTURE.md第7节）
│   │   ├── culture.json       # 文化科普agent产出（累加式历史问答记录，2026-07-25新增，见AGENTS.md第8节）
│   │   ├── poi_details/       # POI详情agent产出（树木，见ARCHITECTURE.md第3.4节）
│   │   │   ├── manifest.json  # 登记这次trip生成了哪些POI详情、对应哪个文件，供网站前端不扫目录直接取列表
│   │   │   ├── _stale/        # 路线edit后失效的旧详情归档，不属于当前活动集合
│   │   │   └── day{N}_{poi_slug}.json   # 单个重点景点的深度玩法路线，一个景点一个文件
│   │   └── user_notes.jsonl   # 用户备注/annotation layer，append-only（2026-08-21新增，见ARCHITECTURE.md第21节）
│   ├── final_guide.md         # 最终攻略，仅整合agent可写
│   ├── final_guide.blocks.json # 最终攻略的有序正文块及上游claim映射；程序由此渲染Markdown
│   ├── final_guide.annotated.md  # 溯源版（scripts/delivery/run_annotate.py程序拼接生成，不调用LLM），
│   │                              # final_guide.md原样保留 + 每条建议实际引用的chunk原文附录，供核实内容是否有依据
│   ├── final_guide.meta.json  # 最终攻略的元信息（最后合并时间、涉及字段、版本号）
│   ├── conflicts.jsonl        # 整合agent report_conflict的记录 + 主agent的处理结果
│   ├── changelog.jsonl        # 全量变更留痕（谁改的/改了什么/何时），store.py自动写入
│   ├── review_findings.jsonl  # 定向复核问题当前状态（追加式）
│   ├── review_log.jsonl       # 人工/模型/系统统一审计记录（追加式）
│   ├── review_runs/           # recommendation、逐条人工决定、patch、rollback的版本化记录
│   └── optimization_candidates.jsonl # 重复根因聚合出的系统优化候选
├── materials/                 # 当前Project补充证据；仅evidence-<uuid>.json可检索/引用
└── agents/                    # Agent私有工作笔记与检索日志，不作为引用依据
    ├── route_agent/
    ├── lifestyle_agent/
    └── integration_agent/
```

`materials/evidence-<uuid>.json`采用`project-evidence-v2`，字段为`schema_version`、`note_id`、`note_type="evidence"`、`summary`、`evidence_excerpt`、`actor`、`log_scope`、`source_url`、`source_title`、`source_query`、`source_observed_at`、`source_platform`、`created_at`。`log_scope`记录Evidence来自哪个Agent工作单元；旧`project-evidence-v2`缺失该字段时仍可读，仅在恢复历史POI时可用来源URL是否精确出现于目标`web_search_log.jsonl`作为归属回退，不做关键词模糊匹配。其中只有`summary`由Agent提交，且仅用于检索和展示；其余来源字段由系统从URL精确匹配的结构化Web搜索结果生成，引用核验只读取`source_title+evidence_excerpt`。文件采用UUID命名并原子创建；历史`note-*.json`（包括损坏JSON）和`project-evidence-v1`不自动迁移、不参与检索。`agents/<scope>/findings.jsonl`记录`note_type="working_note"`、`actor`、`fact`、`created_at`，只能作为该Agent重试提示。这些磁盘字段未因2026-09-07的Context Editing修复变更；变化只在模型输入投影：最新`retrieve_project_evidence`结果在硬预算内保留完整内容，较早结果逐级压缩为有界摘录和可回查定位符，不修改或覆盖原Evidence文件。

---

## 2. 文件级 Schema

### 2.1 `workspace/route_plan.json`（路线规划agent写）
```json
{
  "version": 2,
  "days": [
    {
      "day": 1,
      "city": "成都",
      "area": "市区",
      "pois": [
        {"name": "宽窄巷子", "brief": "拍照打卡、喝盖碗茶", "needs_detail": false},
        {"name": "太古里", "brief": "潮流购物、IFS爬楼熊猫", "needs_detail": false}
      ],
      "notes": "抵达日，安排较轻松的市区活动",
      "sources": [
        {"platform": "bilibili", "doc_id": "cd-001", "chunk_id": "cd-001-c003"}
      ],
      "claims": [
        {
          "claim_id": "route-claim-...",
          "target_path": "pois.0.name",
          "subject": "宽窄巷子",
          "claim_text": "Day1安排宽窄巷子",
          "claim_type": "poi_selection",
          "support_mode": "direct",
          "claimed_entities": ["宽窄巷子"],
          "evidence_refs": [
            {"platform": "bilibili", "doc_id": "cd-001", "chunk_id": "cd-001-c003"}
          ],
          "evidence_snapshots": [
            {
              "source": {"platform": "bilibili", "doc_id": "cd-001", "chunk_id": "cd-001-c003"},
              "excerpt": "……宽窄巷子……",
              "observed_at": "2026-08-28T10:00:00+00:00",
              "source_kind": "material"
            }
          ],
          "derived_from": [],
          "provenance_mode": "live"
        }
      ]
    },
    {
      "day": 2,
      "city": "成都",
      "area": "郊区",
      "pois": [
        {"name": "青城山", "brief": "道教名山，可徒步登顶", "needs_detail": true, "needs_detail_reason": "大型景区，本身构成完整游玩体验，有明确路线/途经点概念"}
      ],
      "notes": "郊区一日游",
      "sources": [],
      "claims": [
        {"claim_id": "route-claim-...", "target_path": "pois.0.name", "claim_text": "Day2安排青城山", "support_mode": "user_constraint", "evidence_refs": [], "evidence_snapshots": []},
        {"claim_id": "route-claim-...", "target_path": "pois.0.brief", "claim_text": "青城山是道教名山，可徒步游览", "support_mode": "unverified", "evidence_refs": [], "evidence_snapshots": []},
        {"claim_id": "route-claim-...", "target_path": "pois.0.needs_detail_reason", "claim_text": "青城山需要单独生成深度路线", "support_mode": "derived", "derived_from": ["route-claim-..."], "evidence_refs": [], "evidence_snapshots": []},
        {"claim_id": "route-claim-...", "target_path": "notes", "claim_text": "Day2为郊区一日游", "support_mode": "user_constraint", "evidence_refs": [], "evidence_snapshots": []}
      ]
    }
  ],
  "amap_verified": true,
  "created_at": "2026-07-12T10:00:00",
  "updated_at": "2026-07-12T10:00:00"
}
```
> **v2变更（2026-07-20设计讨论，2026-07-21已实现）**：`pois`字段从纯字符串列表升级为对象列表（对应`schema.py`的`RoutePoi`模型，`RoutePlan.version`升到2），新增`brief`（骨架层"稍详细一点"的一句话说明，呼应用户希望骨架比"纯地名列表"多一点信息量但又不到POI详情那种深度）和`needs_detail`+`needs_detail_reason`（`route_agent`在生成骨架的同一次推理里顺带判断这个点是否值得单独生成深度玩法路线，交给LLM按语义判断，不用词典硬编规则匹配"XX山/XX古镇"这类模式，理由见`docs/ARCHITECTURE.md`第3.4节）。`needs_detail=true`的POI会在"树木生成中"阶段自动触发`workspace/outputs/poi_details/`里对应的详情文件生成，详见2.3.1节和`docs/ORCHESTRATION.md`。
>
> **事实级依据链（2026-08-28）**：新写入/改写的每一天必须提交`claims`。程序要求每个`pois.<index>.name`以及非空`brief`、`needs_detail_reason`、`notes`都有对应claim；生成`claim_id`、固化`evidence_snapshots`，并从所有`evidence_refs`自动汇总天级`sources`。`support_mode`分为`direct/amap/derived/estimate/user_constraint/unverified`。Live Web事实必须引用由`append_material`按精确URL固化的`project-evidence-*`；高德事实必须引用本轮调用返回的`amap-evidence-*`。完整语义和页面验收见[`OUTPUT_EVIDENCE_AND_CHECKER_EXPLAINABILITY.md`](OUTPUT_EVIDENCE_AND_CHECKER_EXPLAINABILITY.md)。
> `citation_status`（`RouteDay`级字段）只表示路线引用真实性：`passed` / `force_finalized`。路线可执行性不再复用这个字段，其`warning/blocked/unknown`和强制放行状态单独写入`route_feasibility.json`。

`evidence_checks.json` 是只读解释投影：保存路线、衣食住行、避雷和 POI 详情在引用核验时实际读取的 chunk、原文摘录、检查范围、方法、结果和理由。路线同时保存天级地点身份检查和`route:day:<N>:claim:<claim_id>`事实级检查。它不修改业务产物，也不引入人工处理状态。

> **Route核验当前性（2026-09-17）**：`route_plan.json`整份覆盖、按Day Patch或`write_route_facts_patch`只要真实改变当前路线事实，Store就立即从active区移除对应`route:day:<N>`检查，并将被替换的整组记录连同原结果归入`history`；不把旧`fail`改写成`pass`。Route的`direct/amap/derived` Claim检查同时保存覆盖文本、实体、来源顺序、证据原文、支持类型、目标路径和依赖关系的`verification_fingerprint`。API只投影当前Route Claim ID对应的active检查；旧项目中已被替换的Claim不得因共享Day前缀而影响当前红黄状态。`estimate/user_constraint/unverified`仍是人工边界记录，不冒充外部核验通过。

> **确认后写入边界（2026-09-07更新）**：引用/普通时效修复只能调用`write_route_facts_patch`修改既有天的`notes/sources/claims`；不能改`day/city/area/pois/needs_detail`。已启用claims的天修改notes时必须同步提交完整claims，防止产物文本与证据链错位。确需结构变化时，Route Agent把候选写入`route_change_proposal.json`（含`proposal_id/status/trigger/reason/evidence/base_structure/before_days/proposed_days/candidate_route_plan`），正式`route_plan.json`保持不变；只有主agent收到专门确认并通过基线未过期检查后才能应用。用户选择`keep`时，不向正式路线复制候选中任何`notes/sources/claims`，决策前后的完整`route_plan.json`必须相等；风险事实继续保留在`route_feasibility.json`，不借“事实Patch”偷渡未确认的移除、替换或调序动作。

### 2.1.A `workspace/route_entities.json`（路线规划agent写，系统checker消费）

该文件使用`route-entities-v1`，以`(day, poi_index, formal_name)`绑定当前路线结构。每个实体记录`provider_query/provider/provider_id/provider_name`、省市区、坐标、候选及拒绝原因、选择理由、置信度和`resolved/ambiguous/unresolved/requires_human_confirmation/stale`状态。Store写入时校验POI索引、正式名称和`route_plan_structure_hash`；结构变化后旧实体不能静默复用。

Route Agent可用`amap_resolve_route_entity`取得候选和行政区拒绝原因，再通过`write_route_entities`固化选择。Map Checker优先消费当前结构下的`resolved`实体；缺失、过期或未解决时才重新检索。provider规范名不得覆盖用户可见的`route_plan.json`名称。

### 2.1.B `workspace/route_execution.json`（路线规划agent写，Map/Feasibility消费）

该文件使用`route-execution-v1`，包含`route_legs`和`operational_commitments`。路段以Day及起止POI索引绑定，`transport_mode`支持`driving/taxi/public_transit/scenic_shuttle/cable_car/walking/rail/mixed/unknown`；`mixed`用`segments`拆分。只有`driving/taxi`默认进入高德驾车核验，其他方式保留声明状态或使用对应专用证据，不得被错误包装成驾车通过。

`operational_commitments`记录预约、购票、证件和接驳等动作，区分`planned_on_day`与`applies_to_day`，使前一天安排的措施可被后一天Feasibility目标读取。Store校验路段端点、正式名称和路线结构哈希；这些sidecar不进入用户路线结构SHA，但会进入Route验证输入指纹。

### 2.1.C `workspace/route_map_validation.json`（系统Map Checker写）

该文件保存路线实体解析、相邻路段测量、异常和日程汇总。驾车测量失败的路段可带`provider_error.code/detail`；当前错误码包括`missing_api_key`、`request_error`、`invalid_json`、`api_error`、`no_route`、`invalid_metrics`和`non_positive_metrics`。Provider调用日志同步保存相同的`error_code/error_detail`。旧报告只有笼统`provider_error`时，不得事后推断为高德API、额度或网络中的某一种原因。

2026-09-07起，它也是Lifestyle和Integration使用“正式路线精确地图事实”的唯一下游来源。`route_map_day_view()`先校验`route_plan_structure_hash`，再只投影目标Day的canonical实体、相邻路段、交通方式和汇总；候选实体、原始Provider响应和诊断不进入Agent Prompt。相邻路段的`distance_km/duration_h`可被`amap_driving_distance(..., day=...)`直接复用，这一快路径不新增高德Provider调用记录；非相邻正式路线点复用文件中坐标再测距，一端为新推荐时只解析新端点。Hash过期、Day错位、多重匹配或未解析实体不得静默回退到按名称取首个候选；纯非路线地点查询仍保持原有通用行为。

### 2.1.1 `workspace/route_feasibility.json`（系统可执行性checker写）

该文件保存`route_plan_structure_hash`、旅行日期精度、`overall_status`、`force_finalized`及逐项检查。每项包含`day/target/target_type/category/status/summary/evidence/checked_at/applicable_date/requires_structure_change/suggested_action/technical_error`，以及`coverage_status/matched_route_claim_ids/matched_commitment_ids/missing_requirement_codes`。状态为`passed/warning/blocked/unknown`；只有系统checker拥有写权限。

`blocked`进入`route_change_proposal.json`确认流；`warning`只允许修改`notes/sources/claims`；`unknown`重试耗尽后可显式降级继续，但报告保持不确定状态，最终攻略必须提醒出发前复核。

### 2.2 `workspace/progress.json`（主agent写，状态机当前状态）
```json
{
  "state": "并行调研中",
  "agents": {
    "route_agent": "done",
    "lifestyle_agent": "in_progress",
    "integration_agent": "pending"
  },
  "skeleton_confirmed": true,
  "updated_at": "2026-07-12T10:05:00"
}
```

`state` 取值枚举（对应 ARCHITECTURE.md 第4节状态机）：
`需求收集中` → `骨架确认中` → `路线规划中` → `并行调研中` → `整合中` → `树木生成中` → `完成`

### 2.3 `workspace/outputs/lifestyle.json`（衣食住行agent写）
```json
{
  "version": 1,
  "food": {
    "items": [
      {
        "day": 1,
        "recommendation": "...",
        "sources": [
          {"platform": "bilibili", "author": "XX博主", "doc_id": "cd-001", "chunk_id": "cd-001-c003"},
          {"platform": "xiaohongshu", "author": "YY", "doc_id": "cd-014", "chunk_id": "cd-014-c001"}
        ],
        "claims": [
          {
            "claim_id": "lifestyle-claim-...",
            "target_path": "recommendation",
            "claim_text": "一条可独立核验的美食建议",
            "claim_type": "food",
            "support_mode": "direct",
            "claimed_entities": ["具体店名"],
            "evidence_refs": [
              {"platform": "bilibili", "author": "XX博主", "doc_id": "cd-001", "chunk_id": "cd-001-c003"}
            ],
            "evidence_snapshots": [{"source": {"platform": "bilibili", "doc_id": "cd-001", "chunk_id": "cd-001-c003"}, "excerpt": "原文快照…", "observed_at": "...", "source_kind": "material"}],
            "derived_from": [],
            "provenance_mode": "live"
          }
        ],
        "claimed_entities": ["具体店名"]
      }
    ]
  },
  "outfit": {"items": [{"day": 1, "recommendation": "...", "sources": [{"platform": "bilibili", "author": "XX博主", "doc_id": "cd-001", "chunk_id": "cd-001-c007"}]}]},
  "lodging": {"items": [{"day": 1, "recommendation": "...", "sources": []}]},
  "play": {"items": [{"day": 1, "recommendation": "...", "sources": [{"platform": "bilibili", "author": "XX博主", "doc_id": "cd-002", "chunk_id": "cd-002-c001"}, {"platform": "bilibili", "author": "ZZ", "doc_id": "cd-009", "chunk_id": "cd-009-c005"}]}]},
  "last_updated_field": "food",
  "updated_at": "2026-07-12T10:20:00"
}
```
> **事实级依据链（2026-08-28）**：新写入/改写的每个LifestyleItem必须提交`claims`并覆盖`recommendation`。程序生成`claim_id/evidence_snapshots`，从`claims[].evidence_refs`自动汇总兼容字段`sources`，从`claims[].claimed_entities`汇总顶层实体。旧Project没有claims时仍可读取，并继续走整条item级核验。
> `sources.length`仍可作为对象级来源数信号，但新数据的精确支持关系应读取`claims/evidence_checks.json`，不能将顶层来源理解为对整段文字的无条件支持。
> `citation_status`（`LifestyleItem`级字段，2026-08-16新增，见`docs/ISSUES.md`第125条）：`passed` / `force_finalized`，跟`route_plan.json`的`RouteDay.citation_status`同一批修复——`citation_check_node`打回`MAX_RETRY`次仍不过时，用`store.mark_lifestyle_items_force_finalized`把当轮`issues`里涉及的`(field, day)`标记为`force_finalized`。

### 2.3.2 `workspace/outputs/pitfall.json`（避雷agent写，2026-07-23已实现）
```json
{
  "version": 1,
  "food": {
    "items": [
      {
        "field": "food",
        "day": 1,
        "target_recommendation": "叶婆婆钵钵鸡",
        "warning": "有博主反馈味道偏重、排队时间长",
        "sources": [{"platform": "bilibili", "author": "XX博主", "doc_id": "cd-005", "chunk_id": "cd-005-c002"}],
        "claims": [{"claim_id": "pitfall-claim-...", "target_path": "warning", "claim_text": "有博主反馈排队时间长", "claim_type": "pitfall", "support_mode": "direct", "claimed_entities": ["叶婆婆钵钵鸡"], "evidence_refs": [{"platform": "bilibili", "author": "XX博主", "doc_id": "cd-005", "chunk_id": "cd-005-c002"}], "evidence_snapshots": [{"source": {"platform": "bilibili", "doc_id": "cd-005", "chunk_id": "cd-005-c002"}, "excerpt": "原文快照…", "observed_at": "...", "source_kind": "material"}], "derived_from": [], "provenance_mode": "live"}],
        "claimed_entities": ["叶婆婆钵钵鸡"]
      },
      {"field": "food", "day": 1, "target_recommendation": "另一条推荐", "warning": "", "sources": [], "claims": [], "claimed_entities": []}
    ]
  },
  "outfit": {"items": []},
  "lodging": {"items": []},
  "play": {"items": []},
  "updated_at": "..."
}
```
> **字段说明**：
> - 结构跟`lifestyle.json`完全对称（同样按food/outfit/lodging/play四个字段分类），不是独立的新维度——避雷内容天然依附于衣食住行agent的具体推荐，见`ARCHITECTURE.md`第7节。
> - `target_recommendation`：这条避雷内容针对的是衣食住行agent的哪条具体推荐原文，留痕供人工核对匹配是否合理，不是核验字段本身。
> - `warning`：避雷内容本身，允许留空——"这条推荐检索不到负面反馈"是合法结果，不强行凑内容（上面示例里第二条就是这种情况）。留空的item不会被`citation_check`核验，也不会计入"来源数量对比"信号。
> - warning非空时必须提交`claims`并覆盖`warning`；空warning是合法的“已检索但未发现有效避雷信息”，`claims/sources/claimed_entities`均为空。新数据的`sources/claimed_entities`由claims自动汇总；旧数据无claims时继续走原有item级核验。
> - **来源数量对比**：整合agent（`_compute_pitfall_source_counts`纯函数）会拿这份文件跟`lifestyle.json`按`field.day`配对，比较`sources`去重后的chunk_id数量，作为"避雷内容是否明显超过原建议可信度"的判断信号——默认以衣食住行agent内容为主，只有避雷来源数明显更多时才打回衣食住行agent重新考虑，不由避雷agent或程序直接下"要不要替换"的结论，最终判断交给整合agent。
> - `citation_status`（`PitfallItem`级字段，2026-08-16新增，见`docs/ISSUES.md`第125条）：`passed` / `force_finalized`，跟`lifestyle.json`同款字段和写入方式（`store.mark_pitfall_items_force_finalized`），`pitfall_citation_check_node`打回耗尽时标记对应`(field, day)`。

### 2.3.3 `workspace/outputs/culture.json`（文化科普agent写，2026-07-25已实现）
```json
{
  "version": 1,
  "items": [
    {
      "question": "武侯祠这个名字的来历是什么？",
      "answer": "武侯祠是纪念三国时期蜀汉丞相诸葛亮（谥号'忠武侯'）的祠堂……",
      "references": ["https://example.com/some-real-search-result"],
      "answered_at": "2026-07-25T10:00:00"
    }
  ],
  "updated_at": "2026-07-25T10:00:00"
}
```
> **字段说明**：
> - 跟`lifestyle.json`/`pitfall.json`那种"按固定字段整份重新生成"不同，这份文件是**累加式历史问答记录**——`items`只增不减，每次edit调用只追加一条新问答，不重新生成已有内容，见`ARCHITECTURE.md`第3.2节/`AGENTS.md`第8节。
> - `references`：`web_search`查证时用到的网页URL，不是素材库`chunk_id`——文化科普agent不绑`retrieve_materials`，不走`citation_check`那套chunk级核验体系，只靠prompt软约束（如实说明依据、查不到不要编）。
> - 写入方式是"整份覆盖写"（`store.write_culture_output`），由agent自己负责先读出已有`items`、追加新的一条后整份传回，不是`store.py`层做追加逻辑。

### 2.3.1 `workspace/outputs/poi_details/day{N}_{poi_slug}.json`（POI详情agent写，2026-07-20设计讨论新增，2026-07-21已实现）
```json
{
  "poi_name": "青城山",
  "day": 2,
  "routes": [
    {
      "route_name": "前山经典线",
      "suitable_for": "休闲游客，路程较短，有缆车可选",
      "recommended": true,
      "recommend_reason": "根据需求登记的偏好为休闲游、非徒步爱好者，推荐前山经典线",
      "route_segments": [
        {
          "order": 1,
          "point_name": "山门",
          "highlight": "起点，检票入园",
          "suggested_duration": "10分钟",
          "image_prompt": "青城山山门，古朴石牌坊，绿荫环绕",
          "sources": [{"platform": "bilibili_asr", "author": "XX博主", "doc_id": "cd-020", "chunk_id": "cd-020-c002"}],
          "claims": [
            {"claim_id": "poi-claim-...", "target_path": "point_name", "claim_text": "路线从山门开始", "claim_type": "waypoint", "support_mode": "direct", "claimed_entities": ["山门"], "evidence_refs": [{"platform": "bilibili_asr", "doc_id": "cd-020", "chunk_id": "cd-020-c002"}]},
            {"claim_id": "poi-claim-...", "target_path": "highlight", "claim_text": "山门是检票入园起点", "claim_type": "highlight", "support_mode": "direct", "claimed_entities": ["山门"], "evidence_refs": [{"platform": "bilibili_asr", "doc_id": "cd-020", "chunk_id": "cd-020-c002"}]},
            {"claim_id": "poi-claim-...", "target_path": "suggested_duration", "claim_text": "建议在山门停留10分钟", "claim_type": "duration", "support_mode": "direct", "claimed_entities": ["山门"], "evidence_refs": [{"platform": "bilibili_asr", "doc_id": "cd-020", "chunk_id": "cd-020-c002"}]}
          ],
          "uploaded_photo_path": ""
        },
        {
          "order": 2,
          "point_name": "天师洞",
          "highlight": "道教文化核心景点，可参观壁画",
          "suggested_duration": "30分钟",
          "image_prompt": "...",
          "sources": [...],
          "uploaded_photo_path": ""
        }
      ]
    },
    {
      "route_name": "后山生态徒步线",
      "suitable_for": "徒步爱好者，路程更长，人少景美",
      "recommended": false,
      "recommend_reason": null,
      "route_segments": [...]
    }
  ],
  "needs_detail_reason": "大型景区，本身构成完整游玩体验，有明确路线概念",
  "citation_status": "passed",
  "auto_collected": [],
  "retry_history": [],
  "uploaded_overview_photo_path": "",
  "created_at": "...",
  "updated_at": "..."
}
```
> **字段说明**：
> - `routes`是数组不是单一路线——同一景点素材里可能真实存在多条不同路线体系（如青城山前山/后山），不强行糅合成一条，避免途经点顺序拼凑错乱的幻觉风险。只有一条路线时数组也只有一项，不强求凑多条。
> - `recommended`+`recommend_reason`：由POI详情agent读取该project已持久化的`requirements.json`（用户在"需求收集中"阶段的偏好，见`docs/ISSUES.md`第29条提到的持久化机制）判断哪条路线更贴合用户，不是随便选第一条，也不新增"问用户需求"的步骤。
> - `route_segments[].claims`：新写入时，非空`point_name/highlight/suggested_duration`必须各有对应`target_path`的claim。程序生成`claim_id/evidence_snapshots`并汇总segment `sources`；核验器按claim分别核对，不允许一条说法借用另一条的来源。`image_prompt`是基于已核验内容的衍生创作，不逐字核验。历史segment无claims时仍使用原有segment级核验。
> - **局部Patch语义（2026-09-02）**：`write_agent_output_patch`不是用部分数组覆盖整条路线，而是按`route_name`找到路线、再按`route_segments[].order`合并途经点。省略路线或途经点表示保留，不能表示删除；路线级`recommended/suitable_for/recommend_reason`只在显式提交时更新。已有途经点的`uploaded_photo_path`以及文件级`uploaded_overview_photo_path/citation_status/retry_history`由程序保留。新增/删除路线、删除途经点或整体重排仍走`write_agent_output`整份覆盖。
> - `citation_status`：`passed` / `force_finalized`（打回`MAX_RETRY`次仍不过，走`mark_force_finalize`，如实标注但不阻塞流程，见`docs/ARCHITECTURE.md`第3.4节第8点）。
> - `needs_detail_reason`：从`route_plan.json`对应POI的同名字段带过来，留痕方便复核`route_agent`的判断标准是否合理。
> - **文件名`poi_slug`（实现说明）**：`store.poi_slug()`直接保留中文（如`day1_武侯祠.json`），不做拼音转写——中文文件系统支持，且比拼音更直观易读，本节schema示例里的`day2_qingchengshan.json`只是占位写法，实际落盘是中文名。`poi_slug`只把文件名不安全的字符替换成`_`，不引入拼音依赖（不在本次范围）。
> - `citation_status="force_finalized"`（实现补充）：走的是**按POI粒度**的`store.mark_poi_force_finalize`，只改这一个POI详情文件自己的`citation_status`，**不碰`conflicts.jsonl`、不设置project级别的全局force_finalize粘性标志**——不能复用project粒度的`mark_force_finalize`，否则一个POI核验失败会误伤"整个project是否完成"的判断（见`docs/ORCHESTRATION.md`第3.3节"实现时要注意"第1点、`docs/ISSUES.md`第30条）。
> - `auto_collected`（2026-07-24新增，2026-08-02字段说明同步更新）：`[{"title": ..., "author": ..., "source_url": ...}]`，只在`citation_status="force_finalized"`时可能非空——判定为真缺且项目级自动补充采集预算未耗尽时，已经真实下载/转写/入库成功的条目（见`docs/ARCHITECTURE.md`第16节）。此前这里还有一个`collection_suggestions`字段（未经过滤原样呈现B站搜索候选，供"人工审核"），因为从未真正有人工审核入口消费、唯一出口是直接写进`final_guide.md`，真实撞见过不适宜内容混入其中，已于`docs/ISSUES.md`第98条移除。
> - `retry_history`（2026-08-02新增）：`["第1轮：...", "第2轮：...", ...]`，是`_run_poi_detail_citation_loop`每一轮核验打回理由的不可变审计历史；定向复核通过并将`citation_status`回填为`passed`后也不删除。Review Center将其按轮次投影为收敛时间线，用`evidence_checks`中最新的Claim级结果单独表示“当前待确认问题”；历史轮次数与当前问题数不能相加。旧项目通过读时投影获得该结构，不批量改写业务文件，见`docs/ISSUES.md`第98条。
> - `missing_entity_context`（2026-09-17新增）：Review Context会将旧式`missing_entities`在读取时去重，定位到当前业务字段，并分开返回“当前对象范围已挂载证据”、“素材库精确命中候选”、“下游POI Claim结构化核验”和“最终攻略文本传播位置”。当历史失败的Claim ID已不在当前内容中时，返回`stale_failure`和新旧时间/字段对照，不把旧结论当成当前失败。下游通过和素材命中都只是当前字段的回填候选；必须回填并重核当前对象后才能关闭Finding。最终攻略仅按行级文本命中标记`text_match`，现有块级映射不足以声明行级Claim血缘。该上下文会保留项目级待处理状态，但不阻断已完成的生成产物。
> - `uploaded_overview_photo_path`/`route_segments[].uploaded_photo_path`（2026-08-17新增）：管理后台"POI图片"页面人工上传真实照片的相对`workspace/`路径（如`"outputs/poi_photos/day2_青城山/overview.jpg"`），空字符串=未上传。这两个字段不是agent写的，是人类通过`POST /api/projects/{id}/poi_photos/...`（`src/api/routers/poi_photos.py`，actor=`admin_user`）上传时由`travel_guide_kernel/store.py`的`write_poi_overview_photo`/`write_poi_segment_photo`写入。优先级：`run_generate_site.py`生成阶段，`uploaded_overview_photo_path`非空时优先于高德自动查图（`src/tools/poi_photo.py`）；`uploaded_photo_path`是途经点粒度唯一的照片来源（自动查图从未支持这个粒度）。文件本体存在`workspace/outputs/poi_photos/day{N}_{poi_slug}/`目录下，不进`data/material_library/`全局素材库。

### `workspace/outputs/poi_details/manifest.json`（POI详情agent写，登记本次trip生成了哪些POI详情）
```json
{
  "generated": [
    {
      "poi_name": "青城山",
      "day": 2,
      "file": "day2_qingchengshan.json",
      "citation_status": "passed",
      "generation_mode": "normal",
      "recovery_trigger": null
    }
  ],
  "updated_at": "..."
}
```
> 这是当前有效树木的权威列表。网站、API和合同校验在manifest存在时只读取`generated[].file`；
> 只有历史项目完全没有manifest时才回退扫描活动目录。`orchestrator.py`在"树木生成中"阶段
> 的join节点里统一写入，不是每个并行分支各自写（避免并发写同一个文件的竞态问题）。完成后
> 修改路线时，编排层会比较edit前后的路线：被删除、移动日期，或从`needs_detail=true`改为
> `false`的旧详情会移入`_stale/`并从manifest移除；人工通过POI edit入口补充、且对应POI仍在
> 路线中的详情不会仅因`needs_detail=false`被误归档。
> `generation_mode="provider_recovery"`表示POI首次生成的Provider技术重试耗尽后，系统曾使用已持久化证据发起一次受限恢复；此时`recovery_trigger="provider_retry_exhausted"`。它只说明生成路径，不代表质量通过；最终质量仍看`citation_status`。旧Manifest缺失两字段时默认为`normal/null`。

### 2.4 `workspace/final_guide.blocks.json`（整合agent提交，程序校验和固化）

```json
{
  "schema_version": "final-guide-blocks-v1",
  "blocks": [
    {
      "block_id": "guide-block-...",
      "section": "Day1路线",
      "markdown": "## Day 1\n\n上午游览武侯祠……",
      "mapping_mode": "upstream",
      "mapping_note": "",
      "upstream_claim_ids": ["route-claim-..."],
      "claim_snapshots": [
        {
          "claim_id": "route-claim-...",
          "source_path": "route_plan.json:/days/0/claims/0",
          "usage_key": "route:day:1",
          "claim_text": "上午游览武侯祠。",
          "claim_type": "schedule",
          "support_mode": "user_constraint",
          "evidence_refs": []
        }
      ]
    }
  ],
  "rendered_body_sha256": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

> `mapping_mode`分为`upstream/editorial/user_constraint/unverified`。`upstream`块必须引用当前上游JSON里真实存在的`claim_id`；程序忽略模型自填的`block_id/claim_snapshots`，从`route_plan.json`、`outputs/lifestyle.json`、`outputs/pitfall.json`和POI详情文件重新取值并固化快照。`unverified`块必须填`mapping_note`说明为何保留。`final_guide.md`正文由blocks按顺序渲染；POI附录和参考来源仍由程序独立追加。

> 历史兼容：没有`final_guide.blocks.json`的旧Project继续读取`final_guide.md`，Meta标记`body_source="legacy_markdown"`。如果人工或旧代码直接改写正文，导致正文SHA256与blocks不一致，API不再返回过期映射，也不将其展示成当前追溯链。

### 2.5 `workspace/final_guide.meta.json`（整合agent写正文相关字段，`references`由程序生成覆盖，见下方说明）
```json
{
  "version": 3,
  "merged_fields": ["route_plan", "lifestyle.food"],
  "unresolved_conflicts": [],
  "checklist_included": true,
  "body_source": "blocks_v1",
  "blocks_schema_version": "final-guide-blocks-v1",
  "blocks_count": 12,
  "mapped_claim_count": 18,
  "references": [
    {
      "platform": "bilibili",
      "author": "XX博主",
      "doc_id": "cd-001",
      "chunk_id": "cd-001-c003",
      "cited_in": ["route:day:1", "lifestyle:food:day:1:item:0"],
      "excerpt": "该chunk的程序截取原文片段…"
    }
  ],
  "updated_at": "2026-07-12T11:00:00"
}
```
> `references`**不是**整合agent自己写的（历史上曾经是，见`docs/ISSUES.md`第8条：整合agent只拿到`chunk_id`字符串、没见过原文时，会把上游推荐文本误当成chunk原文编进引用表）。现在由`src/materials/citation_check.py`的`collect_cited_chunks`+`build_reference_section`纯程序生成，`orchestrator.py`的`_replace_reference_section`统一覆盖这个字段（以及`final_guide.md`末尾的"参考来源"章节），不依赖模型自觉保证真实性。`chunk_id`字段定位到具体原文片段；`excerpt`是程序从该片段截取的预览；`cited_in`是程序反向扫描各个`sources`字段得到的“声明使用位置”，例如`route:day:1`、`lifestyle:food:day:1:item:0`。它只表示这个产物对象声明使用了该来源，**不表示来源支持对象里的每一句话**；逐事实支持关系由`claims`/`evidence_checks.json`负责。生成分两次：`integration_agent_node`写完正文时先遍历`route_plan.json`/`outputs/lifestyle.json`/`outputs/pitfall.json`的`sources`生成一版；这时POI详情（"树木"阶段）还没生成，`poi_details_join_node`在所有POI详情文件写完后会额外遍历`outputs/poi_details/*.json`的`routes[].route_segments[].sources`，重新收集全量引用并再次覆盖这个字段——POI详情层的sources跟前三者不是同一嵌套层级，此前完全没被收集过，导致表格系统性少列约1/4真实引用（见`docs/ISSUES.md`第136条）。

### 2.6 `workspace/conflicts.jsonl`（整合agent追加，主agent更新处理结果）
```json
{"id": "c1", "reported_by": "integration_agent", "target": "lifestyle_agent.food", "reason": "Day1路线城市字段为成都，但美食建议明确标注为重庆，与上游结构化信息矛盾", "status": "pending", "retry_count": 0, "created_at": "...", "issue_type": ""}
```
> `issue_type`（实现补充，`docs/ARCHITECTURE.md`第19.2节）：结构化的问题类型标签，供管理后台跨project按类型聚合统计触发频率，不用再靠解析`reason`文本判断类型。默认空字符串（历史记录/未打标签的冲突走这个值）；`route_draft_node`/`lifestyle_agent_node`/`pitfall_agent_node`/`poi_detail_edit_node`四处"定点修改误改检测"分别标注`route_edit_overreach`/`lifestyle_edit_overreach`/`pitfall_edit_overreach`/`poi_detail_edit_overreach`。

### 2.7 `workspace/changelog.jsonl`（store.py自动写，任何agent都不直接写）
```json
{"timestamp": "2026-07-12T10:20:00", "actor": "lifestyle_agent", "action": "write", "target": "outputs/lifestyle.json#food", "summary": "更新Day1美食推荐"}
```

### 2.6.1 定向复核文件（2026-08-22新增）

定向修补与持续优化的设计和流程见 [`docs/TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md`](TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md)。

- `review_findings.jsonl`：问题单当前状态；每次更新追加一行，通过 `finding_id` 取最新版本。
- `review_log.jsonl`：统一审计链，记录 `actor_type`（`human` / `model` / `system`）、`actor_id`、动作、证据和决定。
- `review_runs/<review_id>/patch.json`：Patch-first 的源版本、JSON Pointer 操作、应用状态和 rollback 关系。
- `review_runs/<review_id>/recommendation.json`：模型给人工的复核建议包，不直接修改正文。
- `review_runs/<recommendation_id>/item_decisions.jsonl`：人工对建议包中每个 claim/旧版建议的追加式决定；同一 `item_key` 读取最新记录，不直接修改正文。
- `optimization_candidates.jsonl`：重复根因聚合后的工程优化候选，不自动修改生产代码。

跨项目聚合索引写入项目目录同级的 `runtime/review_index/`，只保存候选统计和项目/条目引用，
不覆盖任何 workspace 文件；索引损坏或删除后可从各项目的 `review_findings.jsonl` 重建。

这些文件是项目 workspace 的事实记录；中央运维系统（未来接入）只能索引和聚合，不能反向覆盖 workspace。

### 2.7 `workspace/outputs/user_notes.jsonl`（用户备注/annotation layer，2026-08-21新增，见ARCHITECTURE.md第21节）

Append-only单行JSONL，每行一条备注：
```json
{"note_id": "75386b06815b", "day": 2, "poi_name": "武侯祠", "note_text": "这家火锅鱼要提前订", "source": "web", "created_at": "2026-08-21T04:47:34.918096+00:00", "superseded_by": null, "deleted": false}
```

> - `note_id`：hex串，`write_user_note`/`update_user_note`用`uuid.uuid4().hex[:12]`生成。
> - `day`/`poi_name`：两级可选绑定。都为空=行程级；只填`day`=天级；都填=景点级。
> - `note_text`：备注正文，用户自由输入的原文，不经过citation核验、不改写。
> - `source`：`web`（网页表单）/ `feishu`（阶段2）/ `admin`（后台API改）。
> - `created_at`：这一条记录本身的创建时间（不是原始备注首次创建的时间——改会生成新note_id+新created_at）。
> - `superseded_by`：非空时表示这条已被后续修改取代，指向新note_id；仍保留在文件里不删除。
> - `deleted`：软删标记。改=旧条`deleted=true`+`superseded_by`指向新条+append新条；删=旧条`deleted=true`。
>
> 生命周期：append-only底本不动，`store.list_user_notes`只返回`deleted=false`的条目。跨`run_edit`/彻底重跑不清——备注是用户资产，不属于agent产出。渲染时按`day`/`poi_name`分三桶：POI级（绑定的POI仍在当前`route_plan.json`里）渲染进Day详情页对应POI处+树木页；天级渲染进天数长卷对应天的卡片；行程级、以及"绑定的POI在route_plan里已经找不到"的孤儿备注，一起退到独立备注区（文本不丢），见`run_generate_site.py`的`_classify_user_notes`。

---

## 3. 文件级写入权限对照表

| 文件 | 可写方 | 触发方式 | 留痕 |
|---|---|---|---|
| `route_plan.json` | 路线规划agent | 主agent触发 / 依赖图联动 | 是 |
| `route_entities.json` | 路线规划agent | full规划或Codex Route交付时固化地图实体 | 是 |
| `route_execution.json` | 路线规划agent | full规划或Codex Route交付时声明路段和跨日措施 | 是 |
| `route_map_validation.json` | 系统Map Checker | Route引用核验后、Feasibility前；当前输入可复用 | 是（validation run同步留痕） |
| `route_feasibility.json` | 系统Feasibility Checker | Map Checker后、Lifestyle前 | 是（validation run同步留痕） |
| `route_change_proposal.json` | 路线规划agent写候选；主agent决定应用/保留/替换候选 | 用户确认后核验或整合发现结构问题 | 是 |
| `progress.json` | 主agent | 状态流转时自动更新 | 是 |
| `outputs/lifestyle.json` | 衣食住行agent（本人） | 主agent触发 / 用户直接@ | 是 |
| `outputs/pitfall.json` | 避雷agent（本人） | 衣食住行agent完成后自动级联 / 用户直接@ | 是 |
| `outputs/poi_details/day{N}_{poi_slug}.json` | POI详情agent（本人，并行多实例各写各的文件） | "树木生成中"阶段自动触发（针对`needs_detail=true`的POI） | 是 |
| `outputs/poi_details/manifest.json` | POI详情agent（由join节点统一写，不是各并行分支各自写，避免并发写竞态） | 同上，所有并行分支完成后 | 是 |
| `outputs/poi_details/day{N}_{poi_slug}.json`的`uploaded_*_photo_path`字段 + `outputs/poi_photos/day{N}_{poi_slug}/**`（2026-08-17新增） | 人类（管理后台，actor=`admin_user`，非agent） | 管理后台"POI图片"页面手动上传/替换/删除真实照片 | 是 |
| `outputs/user_notes.jsonl`（2026-08-21新增） | 人类（网页表单/后台API，actor=`admin_user`，非agent） | 网页表单提交 / 后台API增改删 | 是 |
| `final_guide.blocks.json` / `final_guide.md` / `final_guide.meta.json` | 仅整合agent（POI/参考附录由主agent程序拼接） | 主agent触发（含"树木生成中"阶段join后的二次合并，不重新调用LLM） | 是 |
| `conflicts.jsonl` | 整合agent（追加）/ 主agent（更新status） | report_conflict / trigger_agent | 是（jsonl本身即留痕） |
| `changelog.jsonl` | 系统（store.py内部） | 任意写操作自动触发 | — |
| `data/material_library/raw/<主题>/**` | 人工（MVP阶段）/ 未来API或爬虫工具 | 手动放入文件+登记manifest.json / 后续自动化抓取 | manifest.json本身即记录 |
| `data/material_library/index/<主题>/*` | 核心素材agent | 读取同名raw/<主题>/下内容处理生成，append-only | append-only天然可追溯 |
| `data/material_library/index/<主题>/insights.json` | `generate_insights()`（`src/materials/insights.py`） | 入库后自动触发 / 管理后台手动"重新生成"按钮，**整份覆盖写**，非append-only | generated_at字段记录生成时间 |
| 全局用户记忆（预留：`data/user_memory.json`） | 主agent | 任意agent发现偏好→通知主agent写入 | 是 |

---

## 4. 状态机流转条件

```
需求收集中
  → 骨架确认中     [主agent判断需求信息已足够]
骨架确认中
  → 路线规划中     [用户确认骨架 / 用户要求调整则原地打回路线规划agent]
路线规划中
  → 并行调研中     [route_plan.json 写入完成]
并行调研中
  → 整合中         [outputs/lifestyle.json 写入完成 且 route/lifestyle引用核验通过 且
                     outputs/pitfall.json 写入完成（衣食住行agent完成后自动级联，2026-07-23新增）
                     且避雷内容引用核验通过]
整合中
  → 树木生成中     [final_guide.md 写入完成 且 conflicts.jsonl 中无 pending 状态的冲突（复用"完成"原来的判断条件）]
  → 打回(条件路由)  [conflicts.jsonl 新增 pending 冲突，且 retry_count < 2 —— 打回目标不固定为衣食住行agent，
                     由 conflict.target 字段动态决定跳回 route_agent 还是 lifestyle_agent，见 ORCHESTRATION.md 第4节]
  → 强制终结        [retry_count 达到上限(2)，不再打回，整合agent被强制要求如实写出未解决的冲突，避免死循环]
树木生成中
  → 完成           [route_plan.json 中所有 needs_detail=true 的 POI 都已生成对应详情文件（含"打回耗尽后强制标注"这种非完美但已终结的情况）
                     且 outputs/poi_details/manifest.json 写入完成 且 final_guide.md 二次合并完成；
                     若 route_plan.json 中没有任何 needs_detail=true 的 POI，直接跳过本阶段进入完成]
完成（维护态，非终态）
  → （主agent转为响应模式，不主动做任何事；仅在用户@某agent修改 / 整合agent report_conflict 时被唤醒，
      按依赖表判断影响范围，触发局部重跑，不整体回退状态机——具体路由规则见 ORCHESTRATION.md）
```

---

## 5. Agent私有工作空间（`agents/<agent_name>/`）

- 不对外暴露、不受权限矩阵约束，agent可以在自己的私有目录里自由读写调研草稿、中间检索结果等。
- 私有空间的内容不计入 changelog，也不参与依赖图判断——只有写入 `workspace/` 下的内容才是"正式产出"。
