---
title: 持续质量评测与受控自进化设计
type: design
status: implemented
owner: review-system
created: 2026-08-28
last_reviewed: 2026-09-14
authoritative_scope: Improvement Signal、Optimization Candidate、定向回归和受控系统演进的设计与当前实现边界
source_of_truth: src/review/models.py、src/review/index.py、src/review/regression.py、src/review/monitoring.py、复核API、质量中心前端及对应测试
related_docs:
  - TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md
  - PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md
  - REVIEW_EXECUTOR_OPERATIONS_DESIGN.md
---

# 持续质量评测与受控自进化设计

> 状态：核心闭环、受控定向回归和Candidate阶段证据门禁已实施，长期观察指标继续演进  
> 建立日期：2026-08-28  
> 首次实施：2026-08-28  
> 适用范围：生成后状态扫描、定向复核、Codex Skill、人工裁决、系统改进候选、系统性评测  
> 上位设计：[攻略定向修补与持续优化闭环](TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md)  
> 相关实现：[旅游攻略定向复核Skill](../.agents/skills/travel-guide-targeted-review/SKILL.md)、[复核运维执行器](REVIEW_EXECUTOR_OPERATIONS_DESIGN.md)；Skill开发历史见已归档的 [`TRAVEL_GUIDE_TARGETED_REVIEW_SKILL.md`](TRAVEL_GUIDE_TARGETED_REVIEW_SKILL.md)

本文档只负责“已确认的复核信号如何进入系统改进与回归”的跨项目闭环，不重复维护单个Finding的Patch流程。生成后定向修复主链路见 [`TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md`](TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md)；MonitoringWindow、Observation和Candidate进入`verified`的当前细节见 [`PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md`](PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md)。实际状态必须从当前Workspace、可重建索引和代码读取，不能用本文的历史样本数字替代实时查询。

---

## 0. 实施状态

截至 2026-08-28，以下核心链路已经落地：

- `system_observations` 扩展了 `cause_key`、影响层、置信度、代表目标、迁移与回归范围；
- Skill 结果导入会经过系统观察门禁，并追加为独立的 `ImprovementSignal`；
- Signal 支持接受、驳回、暂缓、合并和重分类，且与 Recommendation 决定分离；
- 全局候选只聚合 accepted signal，候选 ID 仅由稳定 `cause_key` 决定；
- 候选统计区分活跃、已解决、误报和暂缓，人工生命周期状态可在索引重建后保留；
- 质量中心已区分“当前项目复核”“跨项目改进”“状态一致性”，并展示待确认信号、候选工程判断和回归计划；
- 候选首次进入`monitoring`必须绑定当前版本的成功可执行Regression Run；`verified`必须留下人工摘要，有Checker时还要求Monitoring Summary=`ready`，无Checker时重新校验当前可执行回归证据；
- 已有 Codex Skill 历史结果可幂等回填为待确认 Signal；首次真实数据回填产生3条pending signal，后续人工裁决已包含accepted记录，实时状态应从当前Workspace与派生索引读取；42条已关闭legacy finding不再形成活跃候选。
- Candidate 会幂等生成历史 Finding 状态复查案例、建议测试草稿和反例草稿；
- 管理后台可显式绑定 `tests/` 下的精确 pytest selector，或经两次成本确认后绑定并运行固定 eval 场景；
- Regression Run 复用统一 JobService，持久化案例结果、代码指纹、Prompt fingerprint 和 eval 产物路径；
- 只有所选案例全部通过，且至少包含一个真正执行的 pytest、反例或固定 eval，`implementing` 候选才自动进入 `monitoring`。
- 2026-09-14 起，正式双 Harness 结论以显式 `ComparisonPair` 为单位，每个 Pair 恰好绑定一个 LangGraph Project 和一个 Codex Skill Project；普通 `comparison_peer` 只保留谱系语义。Signal 可选绑定来源 Pair，Candidate 继承 `source_pair_ids`，并单独记录目标作用域、工程变更引用和验证 Pair，避免把“来自某个实验 Project”误画成“已经反哺 Shared Kernel”。
- Pair 查询会只读投影两侧 Project 已登记且仍活跃的 Finding，并列出共享阶段的确定性状态差异或共同异常状态。这一层回答“同案对照中观察到了什么问题”，不自动生成 Signal，也不把模型、Provider 或运行条件不同造成的结果差异归因给 Harness；只有经人工接受且明确目标作用域的 Signal 才能进入反哺候选。

尚未完成的部分：

- 内容型 Finding 的冻结输入重执行与修复前后结构化 diff；当前 Finding 案例只做最终状态复查，不单独作为自动推进证据；
- 从自由文本 `suggested_test` 自动匹配已有 pytest 节点；当前由工程人员显式绑定；
- 观察窗口、复发率、处理成本和修复收益的长期趋势面板。

当前实现是“显式选择、受控执行、自动留证”：不因 Candidate 出现就自动消耗高成本 eval，也不把 Finding 的人工关闭误当成已完成系统回放。

运行验证边界（2026-09-03）：ImprovementSignal、Candidate、Regression、Monitoring、API和前端面板的代码与聚焦测试均已落地，但当前运行数据中尚未形成可用于运营验收的MonitoringWindow或MonitoringObservation记录。因此本文的`implemented`表示工程能力已经实现，不表示长期观察闭环已经在真实项目中积累足够样本。

---

## 1. 文档目的

本文件定义如何把当前已经存在但彼此尚未完整联动的能力，组成一套持续运行的质量闭环：

```text
项目持续生成和使用
  -> 状态扫描发现具体问题
  -> 标准模型或 Codex Skill 做定向诊断
  -> 人工确认内容结论和系统根因信号
  -> 跨项目聚合系统改进候选
  -> 工程修改代码、Prompt、Schema、规则或数据流程
  -> 定向重放历史失败案例
  -> 固定评测集做独立回归
  -> 观察同类问题复发率
```

目标不是让模型自动修改生产代码，而是把每次真实项目中的问题、调查、人工裁决和修复验证，转化为下一轮生成可以继承的系统能力。

本设计启动时针对以下断点展开；下列内容是实施前快照，不代表2026-09-03当前状态：

1. `TravelReviewResult.system_observations` 已能表达系统问题，但导入后只保留在运行附件中，没有进入持续改进链路；
2. 全局优化候选目前只按 `(root_cause, problem_type)` 和数量聚合，无法表达真实根因、代表证据和建议测试；
3. 复核中心同时展示项目级问题和跨项目候选，但页面作用域没有明确区分；
4. 真实项目产生的在线质量信号与 `eval/` 固定场景评测彼此独立，修复后没有统一的定向重放和效果验证；
5. 当前只能说“某类标签出现了几次”，还不能回答“为什么发生、该改哪里、如何证明修复有效”。

---

## 2. 核心定位

### 2.1 不是单一复核页面

整体产品应定位为“质量中心”，包含两个尺度：

```text
项目级闭环：发现具体问题 -> 定向复核 -> 局部修复 -> 验证当前项目
系统级闭环：聚合已确认原因 -> 工程改进 -> 历史重放 -> 观察复发率
```

项目选择器只控制项目级闭环；跨项目改进默认使用全局作用域，可额外按项目、Agent、模块和时间筛选。

### 2.2 术语定义

#### Finding

某个项目、某个具体业务对象上的问题实例，例如：

```text
成都项目 / 熊猫基地 / “太阳产房”描述缺少完整证据
```

Finding 是问题对象，不是系统根因。

#### Recommendation

标准模型或 Codex Skill 针对一个 Finding 给出的内容和证据处理建议。它可以包含多个 claim 级裁决，但默认只是候选结果，必须经过人工逐条确认。

#### System Observation

复核过程中发现的代码、数据、状态、证据索引、模型行为或展示层问题。当前模型已经存在：

```text
TravelReviewResult.system_observations[]
```

System Observation 是一次模型调查得到的系统问题假设，不因为模型输出就自动成为已确认事实。

#### Improvement Signal

由 System Observation、确定性扫描、人工复核或评测结果产生的、可审计的系统改进信号。它必须绑定来源 Finding、Recommendation、ReviewRun、证据和基线版本，并有独立的人工裁决状态。

#### Optimization Candidate

从一个或多个已经确认的 Improvement Signal 中聚合出的系统性改进假设，用于判断是否应修改代码、Prompt、Schema、校验规则、数据流程或展示逻辑。

候选不要求必须跨多个 Finding。确定性的高影响契约问题可以由单个 Finding 直接形成候选，但仍需工程确认。

#### Regression Case

用于验证某项系统改进是否有效的可重放案例。来源可以是：

- 原始失败 Finding 的冻结输入；
- 已有单元测试 fixture；
- 固定 `eval/scenarios/` 场景；
- 人工补充的反例或边界条件。

### 2.3 “自检”和“自进化”的边界

本系统可以称为持续自检系统：

- 自动扫描确定性状态；
- 自动发现未闭环对象；
- 自动生成冻结复核包；
- 自动调用标准模型，或生成 Codex Skill 任务包；
- 自动校验结果是否绑定当前基线和真实证据；
- 自动聚合已确认信号并建议回归范围；
- 修复后自动执行允许范围内的定向回归。

本系统只能称为“受控自进化”，不能称为无人监管的自主进化：

- 不自动修改或发布生产代码；
- 不自动提升未经人工确认的系统根因；
- 不允许评测模型仅凭自己的复核结果宣布修复成功；
- 内容修改继续经过 Patch 和人工批准；
- 代码、Prompt、Schema 和规则修改继续经过工程实现、测试和发布；
- 修复效果必须同时经过历史案例重放和独立固定评测验证。

---

## 3. 实施前能力与断点

### 3.1 设计启动时已有能力

| 能力 | 当前实现 | 可复用结论 |
|---|---|---|
| 确定性状态扫描 | `src/review/store.py::validate_project_state()` | 可以发现 `force_finalized`、交付契约和 manifest 一致性问题 |
| Finding 当前投影 | `review_findings.jsonl`、`build_review_queue()` | 已有项目级稳定问题对象和人工状态 |
| 冻结复核包 | `src/review/skill_bridge.py::export_review_bundle()` | 已绑定文件 Hash、Finding revision、scope 和允许证据 |
| Skill 结果门禁 | `validate_review_result()` | 已检查基线、原文、证据、scope、动作一致性和执行状态 |
| 双执行器 | `standard_model`、`codex_skill` | 可以生成独立候选 Recommendation 并人工选择 |
| 人工逐条裁决 | `ReviewItemDecision`、`finalize_item_decisions()` | 已支持保留、接受、编辑、暂缓和定向重跑 |
| Patch-first | `propose_patch/apply/rollback` | 内容修改不会由 Skill 直接落盘 |
| System Observation 协议 | `TravelReviewResult.system_observations` | 已有系统问题的结构化表达入口 |
| 全局候选索引 | `src/review/index.py` | 已能跨项目聚合，但当前语义和信息密度不足 |
| 固定系统性评测 | `eval/scenarios/`、`eval/runs/` | 已有完整场景、Judge 分数和 Prompt fingerprint |

### 3.2 设计启动时主要断点

#### 断点 A：System Observation 没有进入业务投影

`import_review_result()` 当前只追加 `ReviewRecommendation`。完整 Skill 结果虽然写入 `review_runs/**/travel_review_result.json`，但 `system_observations` 没有独立状态、人工裁决、全局索引或候选关联。

#### 断点 B：候选按表面标签聚合

当前全局候选仅按：

```python
(root_cause, problem_type)
```

分组。这会把以下不同问题揉进同一个 `evidence / force_finalized_residue`：

- 完全没有 `sources`；
- 引用只支持复合实体中的一部分；
- 固定分钟数或价格无依据；
- 路线拓扑或顺序来自常识推断；
- 真实素材库覆盖不足；
- verifier 规则误报。

#### 断点 C：历史迁移记录污染当前候选

`legacy_content_review` 是历史日志迁移类型，不是真实归一化根因。已关闭、已解决的迁移记录不应仅因数量大而成为活跃系统优化候选。

#### 断点 D：在线信号和离线评测没有统一回放

真实项目 Finding 能发现固定评测集没有覆盖的新问题；固定评测集能防止系统只在当前案例上自证成功。第一层联动已通过统一 Regression Case / Run 落地；剩余断点是内容型 Finding 的冻结输入重执行和长期复发率观察。

#### 断点 E：页面混淆项目和全局作用域

当前项目选择器仍显示在全局优化候选页，但候选数据来自跨项目索引。用户容易误以为全局候选只属于当前项目。

---

## 4. 目标闭环

### 4.1 分层职责

```text
确定性状态扫描
  负责：发现结构、状态和交付契约问题
  不负责：判断正文是否真的错误

标准模型 / Codex Skill
  负责：读取当前内容、真实证据和历史，做单个 Finding 深度诊断
  不负责：直接修改业务文件或确认系统根因

人工复核
  负责：确认内容动作、证据动作和系统问题信号
  不负责：绕过工程验证直接发布系统修改

持续改进聚合器
  负责：聚合已确认信号、计算影响范围和建议回归案例
  不负责：自动编写或发布生产代码

工程与评测流程
  负责：实施修改、运行测试、定向重放和固定评测、观察复发率
```

### 4.2 端到端数据流

```text
Project event
  -> State Observation / User Feedback / Eval Issue
  -> ReviewFinding
  -> ReviewRun
  -> ReviewRecommendation
  -> ReviewItemDecision
  -> SystemObservation
  -> ImprovementSignal(pending)
  -> Human triage
  -> ImprovementSignal(accepted / dismissed / deferred)
  -> OptimizationCandidate
  -> EngineeringChange
  -> RegressionRun
  -> MonitoringWindow
  -> MonitoringObservation
```

### 4.3 触发时机

#### 项目事件触发

以下事件完成后运行确定性扫描，并按 canonical key 幂等同步 Finding：

- 完整攻略生成结束；
- POI 详情 join 完成；
- 定向编辑完成；
- Patch 应用或回滚完成；
- 历史迁移完成；
- site/final guide 派生文件重建完成。

#### 深度复核触发

- 人工点击标准模型或 Codex Skill 复核；
- 高严重度确定性 Finding 自动建议复核，但不自动形成业务结论；
- 修复后需要重放原始 Finding；
- 候选进入工程验证阶段，需要补齐代表案例诊断。

#### 聚合触发

- Improvement Signal 被人工确认；
- Finding 最终关闭、标记误报或进入重跑；
- 新项目完成扫描；
- 工程修复发布后进入观察期；
- 管理后台显式重建全局索引。

---

## 5. Improvement Signal 设计

### 5.1 为什么需要中间层

`system_observations` 是模型输出；`OptimizationCandidate` 是全局工程决策对象。两者之间必须有一个可验证、可驳回、可合并的投影，避免模型一次误判直接污染全局改进统计。

### 5.2 建议模型

```json
{
  "signal_id": "improvement-signal-...",
  "project_id": "...",
  "finding_id": "...",
  "recommendation_id": "...",
  "review_run_id": "...",
  "source_type": "codex_skill",
  "comparison_pair_id": "pair-qinggan-fixed-route-20260906-v4",
  "cause_key": "citation:unsupported_specificity",
  "root_cause": "model",
  "affected_layer": "poi_detail_prompt",
  "problem_type": "unsupported_specificity",
  "hypothesis": "素材不足时仍生成了可验证的具体数字",
  "evidence_refs": ["..."],
  "representative_targets": ["outputs/poi_details/...#/routes/0/route_segments/2"],
  "suggested_fix": "增加无证据时禁止固定时长的生成约束和写入校验",
  "suggested_test": "无来源途经点不得保留固定分钟数",
  "severity": "medium",
  "confidence": "high",
  "decision": "pending",
  "decision_reason": "",
  "baseline": {
    "bundle_sha256": "...",
    "finding_revision": 2,
    "prompt_fingerprint": "..."
  },
  "created_at": "...",
  "updated_at": "..."
}
```

### 5.3 `system_observations` 建议扩展

当前字段已有：

- `root_cause`
- `problem_type`
- `summary`
- `evidence_refs`
- `suggested_fix`
- `suggested_test`
- `severity`

建议新增：

- `cause_key`：稳定归一化身份；
- `affected_layer`：代码、Prompt、Schema、数据、状态、展示或评测；
- `confidence`：模型对根因假设的置信度；
- `representative_targets`：问题对应的当前精确路径；
- `migration_required`：是否可能影响历史文件；
- `regression_scope`：建议重放 Finding、单测或固定场景；
- `is_systemic`：复核 Skill 是否明确认为值得进入系统问题审查；缺省为 `false`，只有显式 `true` 才生成待人工确认的 Improvement Signal。

这些字段仍是模型建议，不直接作为已确认事实。

### 5.4 确定性导入门禁

Skill 结果导入时，对 System Observation 至少校验：

1. `problem_type`、`summary` 非空；
2. `cause_key` 符合受控格式；
3. `evidence_refs` 必须来自 bundle 允许证据或 `review_trace.evidence_reads`；
4. `representative_targets` 必须位于当前 Finding scope，或明确标注为代码/系统目标；
5. `suggested_fix` 不得包含直接发布、绕过人工或自动修改生产文件的指令；
6. `suggested_test` 在高严重度问题中不得为空；
7. 结果绑定当前 bundle hash 和 Finding revision；
8. 执行失败、超时、无效 JSON 不产生 Improvement Signal。

### 5.5 人工裁决

每条 Improvement Signal 单独支持：

- `accepted`：确认是可聚合的系统问题信号；
- `dismissed`：不是系统问题或属于误诊；
- `deferred`：证据不足，暂不进入聚合；
- `merged`：并入已有 cause key；
- `reclassified`：人工修正 root cause、affected layer 或 cause key。

人工确认内容 Recommendation，不等于自动确认 System Observation；两个决定必须分开。

若来源 Project 恰好属于一个正式 `ComparisonPair`，人工接受 Signal 时可同时写入
`comparison_pair_id`。无法唯一定位时保持空值，不从相似 project ID、目的地或松散
`comparison_peer` 关系猜测来源。

实验页的 Pair 问题投影与本节的 Signal 裁决是两个层级：前者只展示两侧当前活跃 Finding、
严重度摘要和共享阶段状态观察，允许用户先看到具体问题；后者才负责确认问题能否聚合、根因
落在哪一层。某侧没有已登记 Finding 只能表述为“当前无登记记录”，不能表述为该侧已证明无问题。

---

## 6. Optimization Candidate 设计

### 6.1 稳定身份

候选 ID 应基于归一化 `cause_key`，不应包含完整 Finding ID 集合。新的 Finding 加入时更新候选统计，而不是生成一个新候选并丢失历史状态。

```text
optimization:citation:unsupported_specificity
optimization:manifest:generation_failed_contract
```

### 6.2 候选至少包含

- 中文标题和症状摘要；
- `cause_key`、root cause、affected layer；
- 根因假设和置信度；
- 活跃、已解决、误报、暂缓数量；
- 影响项目、业务对象和 Agent 数量；
- 首次、最近发生时间和趋势；
- 代表 Improvement Signal 和 Finding；
- 当前 workaround；
- 建议修改的代码、Prompt、Schema、规则或数据流程；
- 建议新增的回归测试；
- 是否需要迁移历史文件；
- 修复后验证计划；
- 候选负责人、状态和人工备注。
- `source_pair_ids`：由已接受 Signal 的显式 `comparison_pair_id` 聚合而来；
- `target_scope`：`undecided/shared_kernel/langgraph_harness/codex_skill_harness/multiple`；
- `change_ref`：实现该候选的 commit、PR 或 patch 引用；
- `validation_pair_ids`：修复后实际用于验证的正式 Pair，可为空，此时只能陈述已有单测或固定评测证据。

`source_pair_ids`、`target_scope` 与 `validation_pair_ids` 是三个不同问题：候选从哪里发现、
修改落在哪一层、修复后由什么 Pair 验证。页面只有在 `target_scope=shared_kernel` 时才把候选
绘制在 Shared Kernel 列；`undecided` 或 `multiple` 不做这种归因。

### 6.3 生命周期

```text
new
  -> triaged
  -> planned
  -> implementing
  -> monitoring
  -> verified

new/triaged
  -> dismissed

任意未完成状态
  -> deferred
```

含义：

- `new`：自动聚合，尚未人工确认优先级；
- `triaged`：已确认是系统改进方向；
- `planned`：已确定修复和回归方案；
- `implementing`：工程改动进行中；
- `monitoring`：修复已通过当前代码和Prompt版本的定向回归；有内置Checker时等待后续完成项目的确定性状态观察，无Checker时保留人工观察边界；
- `verified`：有Checker时目标回归通过且Monitoring Summary为`ready`；无Checker时当前可执行回归通过并由人工记录验证摘要；
- `dismissed`：误聚合、非系统问题或不值得实施；
- `deferred`：信息不足或暂不投入。

### 6.4 聚合规则

默认建议：

```text
同一 cause_key 在同一项目确认 >= 2 个信号
或跨项目确认 >= 3 个信号
  -> 自动创建 new 候选
```

以下问题可以单个已确认信号直接创建候选：

- 确定性状态契约冲突；
- 会造成批量误报或状态污染的扫描器问题；
- 安全、权限或数据损坏风险；
- 生成后校验完全失效；
- 已有回归测试明确证明的代码缺陷。

不应计入活跃升级阈值：

- `false_positive`；
- `dismissed` Improvement Signal；
- derived mirror；
- superseded Finding；
- 未重新归类的 `legacy_content_review`；
- 纯执行失败且尚无业务结论的 ReviewRun。

已解决信号保留在历史统计中，但不作为当前活跃数量。

---

## 7. 持续评测设计

### 7.1 在线持续评测

在线评测来自真实项目事件，主要观察：

- `force_finalized` 比例；
- `generation_failed` 比例；
- 状态一致性异常数量；
- 最终攻略交付契约失败率；
- Finding 每百项目发生率；
- 同 cause key 复发率；
- 标准模型与 Codex Skill 建议一致率；
- Recommendation 被人工接受、编辑、保留或重跑的比例；
- 状态扫描误报率；
- 每类问题平均处理时间和人工成本；
- 修复后新项目是否仍出现同类信号。

在线评测是事件驱动的近实时质量观察，不等于每次都重新运行完整系统性评测集。

### 7.2 离线固定评测

继续保留 `eval/scenarios/` 的固定场景和独立 Judge：

- 成都城市综合游；
- 青甘长线自驾；
- 峨眉山空素材/山岳场景；
- 海口城市与自然景区；
- 洱海骑行与交通执行性。

固定评测承担两项在线信号无法替代的职责：

1. 防止系统只修当前案例并在同一数据上自证成功；
2. 检查一处修复是否让其他目的地、Agent 或生成阶段退化。

### 7.3 历史定向重放

每个进入 `planned` 的候选必须选择一组历史 Regression Case：

- 至少一个原始失败 Finding；
- 至少一个相同 cause key 的其他项目案例（存在时）；
- 至少一个反例，证明新规则不会误伤合法内容；
- 影响面较大时追加一个固定 eval 场景。

定向重放优先于完整评测：先快速验证根因和修复方向，再决定是否运行高成本全链路场景。

### 7.4 修复效果判定

候选不能因为“代码已经合并”就标记 `verified`。当前程序门禁最少满足：

- `verification_summary`非空；
- Regression Run属于当前Candidate、状态为`succeeded`、至少有一个实际通过的`unit_test`、`counterexample`或`fixed_eval`；
- code fingerprint和`prompt_fingerprint.prompt_hash`完整且对应当前版本；
- 有内置Checker时必须存在Monitoring Window且实时Summary为`ready`；
- 无内置Checker时不要求虚构Window，依靠当前成功可执行回归和人工摘要验证。

原始Finding、反例和固定场景仍按候选回归计划选择；当前门禁不强制所有Candidate运行`fixed_eval`，也不自动生成反例。Monitoring为`breached`时禁止进入`verified`，但Candidate不会自动退回`implementing`。

默认观察窗口采用时间与有效检测暴露的双门槛：

```text
elapsed_days >= 14
  AND distinct_project_revisions >= 5
```

高严重度候选至少需要 10 个项目 revision。当前只接入内置确定性状态扫描器，不提供快速通道、多 detector、多 exposure unit 或可编辑策略。scanner 成功完成的 `clear` / `hit` 才进入样本数，失败和不适用项目不进入。完整判定规则见 [Phase 5 最小持续观察设计](PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md)。

---

## 8. 质量中心页面语义

### 8.1 顶层定位

建议侧栏从“定向复核”调整为：

```text
质量中心
```

页面标题：

```text
复核与持续改进
```

说明文字：

```text
定位修复当前项目问题，并从跨项目复核结果中沉淀系统改进。
```

### 8.2 三个主视图

#### 当前项目复核

- 受项目选择器控制；
- 展示真实业务对象；
- 运行标准模型或 Codex Skill；
- 完成人工逐条决定、Patch 和验证。

#### 跨项目改进

- 默认全局作用域；
- 项目选择器改为筛选器或隐藏；
- 展示 Optimization Candidate；
- 可查看代表信号、Finding、建议修改和回归计划；
- 支持候选生命周期操作。

#### 状态一致性

- 受项目选择器控制；
- 展示确定性状态扫描；
- 区分预期失败状态、真实不一致和已由人工复核覆盖的历史状态。

### 8.3 候选卡片首屏信息

- 中文标题；
- 当前状态和优先级；
- 活跃问题数、历史问题数、项目数；
- 根因假设和 affected layer；
- 最近发生时间；
- 建议第一步动作。

展开抽屉展示：

- 代表 Improvement Signal；
- 真实失败摘录；
- 影响项目和目标路径；
- 当前 workaround；
- 建议修改位置；
- 建议测试和迁移；
- 历史回归与观察指标；
- 人工 triage 记录。

---

## 9. 安全与防自证边界

### 9.1 不把模型观察当事实

System Observation 是假设。只有通过确定性门禁并经人工确认后，才成为可聚合 Improvement Signal。

### 9.2 不自动修改系统

候选可以自动生成修复建议、测试草稿和影响范围，但生产代码、Prompt、Schema 和规则修改必须由工程流程实施。

### 9.3 评测集隔离

- 原始失败案例可用于回归，但不能作为唯一验收；
- 固定 eval 场景保留不参与候选根因生成的独立 Judge；
- 重要改动保留至少一个未参与诊断的 holdout 场景；
- 评测 Prompt、生成 Prompt 和代码版本必须留 fingerprint。

### 9.4 避免 Goodhart 效应

不能只优化 `force_finalized_count` 或 Judge 总分。降低告警数量可能来自校验短路，而不是质量改善。必须同时观察：

- 真实内容错误率；
- 状态扫描误报率；
- 人工接受率；
- 可执行性和忠实度；
- 失败是否被如实暴露，而不是被静默吞掉。

### 9.5 执行失败不是业务结论

401、403、429、503、504、超时、无效 JSON、基线变化和任务中断只记录为 ReviewRun 执行失败，不产生业务 Finding 结论或 Improvement Signal。

---

## 10. 分阶段实施

### Phase 0：冻结基线与契约

目标：先确保讨论对象和现状数据固定。

- 为当前三个全局候选建立真实 fixture；
- 固定 `legacy_content_review x 42`、`force_finalized_residue x 7`、`manifest_mismatch x 1` 的当前投影；
- 明确 `generation_failed` 与 manifest/扫描器的业务契约；
- 固定在线指标和离线评测的基线；
- 完成本设计文档评审。

验收：没有代码行为变化，但所有后续实现都有确定输入、预期分类和反例。

### Phase 1：接通 System Observation

目标：把 Skill 已有的系统观察转成可审计信号。

- 扩展 `TravelReviewSystemObservation`；
- `validate_review_result()` 增加系统观察门禁；
- `import_review_result()` 写入 `ImprovementSignal`；
- 增加 signal 的人工接受、驳回、暂缓和重分类 API；
- Recommendation 人工决定和 Signal 人工决定保持独立。

验收：Codex Skill 发现系统问题后，管理后台能看到来源、证据和建议，但不会自动进入全局候选。

### Phase 2：重做候选聚合

目标：从标签计数升级为稳定系统问题对象。

- 使用 `cause_key` 聚合 accepted signal；
- 建立稳定候选 ID 和生命周期；
- 区分活跃、已解决、误报和历史数量；
- 排除 legacy、mirror、superseded 和执行失败噪声；
- 支持单个高影响信号升级；
- 重建全局索引并保留旧文件审计备份。

验收：42 条已解决 legacy 记录不再显示为活跃候选；7 条证据问题按真实原因拆分。

### Phase 3：质量中心页面

目标：明确项目级和全局级两个作用域。

- 调整页面命名和 Tab；
- 当前项目复核与状态扫描继续使用项目选择器；
- 跨项目改进使用全局作用域和筛选器；
- 候选卡片展示可行动摘要；
- 增加候选详情抽屉和生命周期操作；
- Signal 可从 Finding/ReviewRun 双向追溯。

验收：用户不再误解全局候选属于当前项目，并能从候选直接判断影响、根因假设、修改方向和回归计划。

### Phase 4：定向回放与评测联动

目标：系统改进实施后产生可追溯的定向验证证据。

- Candidate 绑定 Regression Case；
- 修复后运行历史 Finding 定向重放；
- 运行受影响单元测试和必要 eval 场景；
- 保存前后对比、Prompt fingerprint 和结果摘要；
- 自动进入 `monitoring`，人工确认是否 `verified`。

验收：候选不能只凭代码合并关闭，必须有可追溯的修复前后结果。

2026-08-28 第一版实施结果：

- 已建立 `OptimizationRegressionCase` / `OptimizationRegressionRun` 持久化协议；
- 已接入历史 Finding 状态复查、白名单 pytest selector 和现有 `run_scenario_eval.py` 固定场景；
- 已记录代码与 Prompt 指纹、分案例结果和 eval 产物路径；
- 已复用 JobService 的排队、运行、失败和中断状态；
- 已在候选详情提供案例选择、新增、运行、进度和最近结果视图；
- 固定 eval 不自动触发，加入案例和开始运行时均需明确成本确认；
- 仅 Finding 状态复查通过不会推进候选，至少一个可执行测试或 eval 通过且Run指纹对应当前版本后才可进入 `monitoring`；
- API人工修改与Regression Runner自动推进复用同一领域门禁，旧Run不能跨代码或Prompt版本复用。

### Phase 5：持续指标与策略沉淀

目标：先用最小、可验证的观察闭环确认已修工程问题是否再次出现。

详细实施口径见 [Phase 5 最小持续观察设计](PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md)。当前只保留一个内置状态扫描器、一个 `project revision` 观察单位、两个事实模型和两个只读 API；通用 detector 平台、冻结生成重放和自动自进化均暂缓。

- 为通过定向回归的受支持候选创建观察窗口；
- 对后续完成项目的新增 revision 记录 `clear`、`hit` 或 `failed`；
- 用时间与样本双门槛派生 `collecting`、`ready`、`breached` 或 `stale`；
- 保留人工确认 `verified` 的边界，并在有Checker时强制Summary=`ready`；
- 没有Checker的候选不创建Window，以当前成功可执行回归和人工摘要完成验证。

验收：对受支持候选，质量中心能回答“观察是否有效、收集了多少新 revision、同类症状是否复发、还差哪个门槛”。

---

## 11. 当前真实案例验收

### 11.1 历史迁移候选

当前：

```text
content / legacy_content_review / 42 次 / 1 个项目
```

目标：

- 不进入活跃系统候选；
- 作为历史审计统计保留；
- 只有重新归类为真实 cause key 的记录才能进入聚合。

### 11.2 证据残留候选

当前：

```text
evidence / force_finalized_residue / 7 次 / 4 个项目
```

目标：至少区分：

- `citation:missing_sources`；
- `citation:partial_entity_support`；
- `citation:unsupported_specificity`；
- `route:unsupported_topology_inference`；
- `materials:true_coverage_gap`；
- `verifier:false_positive`。

不要求一次规则自动完美分类；Codex Skill 和人工裁决可以逐步建立高质量 cause key。

### 11.3 Manifest 候选

当前：

```text
code / manifest_mismatch / 1 次 / 1 个项目
```

已观察到的契约：

- `generation_failed` 分支明确不产生 POI 详情文件；
- join 仍在 manifest 中记录该 POI 和 `generation_failed`；
- 状态扫描把 manifest 指向不存在文件判断为 `manifest_mismatch`。

目标候选：

```text
manifest:generation_failed_contract
```

工程评审需要在以下方案中定案：

1. 扫描器将 `generation_failed + missing detail file` 识别为预期失败状态，并创建独立可重跑 Finding；
2. manifest 对 generation failed 使用可空 `file` 或显式 outcome 字段；
3. 生成失败时写最小占位详情文件。

无论选择哪种方案，都必须增加生成器、manifest 和扫描器之间的契约测试。

---

## 12. 建议代码落点

| 范围 | 建议落点 |
|---|---|
| System Observation 模型 | `src/review/models.py` |
| Skill 结果门禁与导入 | `src/review/skill_bridge.py` |
| Improvement Signal 存储与人工状态 | `src/review/store.py` |
| 全局聚合与候选生命周期 | `src/review/index.py`，必要时新增独立 service |
| API | `src/api/routers/reviews.py` |
| 质量中心页面 | `frontend/src/pages/ReviewCenterPage.tsx` |
| Skill 协议 | `.agents/skills/travel-guide-targeted-review/` |
| 定向回放 | `src/review/regression.py` |
| 固定评测 | `eval/`、现有 `run_scenario_eval.py` 入口 |
| 回归测试 | `tests/test_review_regression.py`、`tests/test_api_reviews.py`、前端组件测试 |

存储继续优先沿用当前 JSON/JSONL 和可重建索引模式；在候选生命周期、跨项目查询和指标规模确实超过文件存储边界前，不为本设计提前引入新的数据库。

---

## 13. 非目标

本轮设计不包含：

- 自动生成并提交生产代码；
- 自动修改 Prompt 后直接发布；
- 让 Codex Skill 绕过人工应用 Patch；
- 每个项目结束后自动运行全部高成本 eval 场景；
- 把所有用户反馈都直接认定为系统问题；
- 用系统自己的 Recommendation 作为唯一成功标准；
- 建立通用机器学习训练平台；
- 替代现有项目级 ReviewFinding、Recommendation、Patch 和审计链路。

---

## 14. 完成定义

这套闭环完成时，应能够回答以下问题：

1. 当前项目具体哪里有问题，证据是什么？
2. 标准模型、Codex Skill 和人工分别怎么判断？
3. 这个问题只需要修当前内容，还是暴露了系统机制缺陷？
4. 相同根因影响了多少项目和对象，当前仍有多少未解决？
5. 建议修改代码、Prompt、Schema、数据、状态还是展示层？
6. 应增加什么回归测试，是否需要迁移历史文件？
7. 修复后原始失败案例是否通过，合法反例是否没有被误伤？
8. 固定评测场景是否退化？
9. 新项目中的同类问题复发率是否下降？
10. 这项改进是否可以沉淀为长期规则、测试、Prompt 片段或 Skill？

只有当这些问题都有可追溯答案时，系统才从“会发现问题”升级为“能持续验证和改进自身质量”的受控自进化系统。
