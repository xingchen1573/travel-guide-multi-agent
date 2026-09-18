---
title: 攻略定向修补与持续优化闭环
type: design
status: implemented
owner: review-system
created: 2026-08-22
last_reviewed: 2026-09-04
authoritative_scope: 生成后定向复核、人工裁决、Patch、批次验收及持续优化的总体职责边界和当前实现入口
source_of_truth: src/review/、src/api/routers/reviews.py、frontend/src/pages/ReviewCenterPage.tsx及对应测试
related_docs:
  - REVIEW_EXECUTOR_OPERATIONS_DESIGN.md
  - CONTINUOUS_QUALITY_EVOLUTION_DESIGN.md
  - PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md
  - TRAVEL_GUIDE_TARGETED_REVIEW_SKILL.md
---

# 攻略定向修补与持续优化闭环

> 状态：总体设计与当前实现入口（截至 2026-09-04）；MVP 后端闭环、Review Center 基础闭环和人工触发的批次验收已形成
>
> 建立日期：2026-08-22
>
> 来源案例：青甘大环线 8 天游程复核

## 0. 文档职责与读取边界

本文档负责生成后维护链路的总体职责、关键安全边界、当前完成度和实现映射，是进入定向复核主题时首先读取的文档。更细事实按以下边界维护：

- Finding、Recommendation、Decision、Patch、Validation和索引的实际字段与行为，以`src/review/`、复核API、前端和测试为准；Workspace文件结构以 [`WORKSPACE.md`](WORKSPACE.md) 为准。
- ReviewRun和标准模型/Codex bundle bridge的运维边界，以 [`REVIEW_EXECUTOR_OPERATIONS_DESIGN.md`](REVIEW_EXECUTOR_OPERATIONS_DESIGN.md) 为准。
- Improvement Signal、Optimization Candidate、Regression和受控演进，以 [`CONTINUOUS_QUALITY_EVOLUTION_DESIGN.md`](CONTINUOUS_QUALITY_EVOLUTION_DESIGN.md) 为准；最小Monitoring机制以 [`PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md`](PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md) 为准。
- Codex执行时直接读取 [`.agents/skills/travel-guide-targeted-review/SKILL.md`](../.agents/skills/travel-guide-targeted-review/SKILL.md)；[`TRAVEL_GUIDE_TARGETED_REVIEW_SKILL.md`](TRAVEL_GUIDE_TARGETED_REVIEW_SKILL.md) 仅保留其开发与影子测试历史。
- [`REVIEW_CENTER_RECONCILIATION_EXECUTION_PLAN.md`](REVIEW_CENTER_RECONCILIATION_EXECUTION_PLAN.md) 是2026-08-27复核中心改造的历史执行记录，不再作为当前接口或状态权威。

## 1. 这份文档要解决什么

当前系统的主要生成路径是：

```text
用户需求 → 路线骨架 → 多 agent 调研 → 整合 → POI 深度详情 → 最终攻略
```

这条路径适合首次生成，也适合路线结构发生变化时的全量重算。但在真实运行中，很多问题并不值得重新跑完整流程：

- 长路线的重新生成成本高；
- 多次调用会带来不稳定的表达变化；
- 旧攻略的问题往往是局部的，不是整条路线失效；
- 有些问题是历史状态、引用索引或展示层没有同步，不是内容本身错误；
- 有些告警是误报，强行重跑反而会引入新的变化；
- 有些问题当前确实无法判断，只能保留为待补充证据或现场核验。

因此，系统需要从“发现问题就重跑”升级为：

```text
发现问题
  → 判断能否解决、是否误报、是否需要重跑
  → 对可局部处理的问题定向修补
  → 对修补结果做人工或模型复核
  → 机械同步派生文件
  → 提取根因并推动代码、规则和测试持续优化
```

这不是对主链路的替代，而是生成完成后的第二条维护链路。

## 2. 青甘案例：为什么需要这条链路

青甘大环线是一个典型的长流程项目。复核时发现的问题主要集中在三类：

1. **内容表达问题**
   - 某些建议过于绝对；
   - 某些数字、时长、店名或开放信息证据不足；
   - 一些内容有参考价值，但应该表达为建议值或条件式建议。

2. **历史状态和引用问题**
   - 代码已经支持更细粒度的 `missing_entities`，旧攻略没有重新回填；
   - `citation_check` 旧逻辑不识别 `platform="amap"` 或不完整的 `web_verified` 证据；
   - POI 详情文件已经通过复核，但 `manifest.json` 仍保留旧的 `force_finalized`。

3. **误报或可避免的重跑**
   - 高德缓存中真实存在的距离，被旧逻辑当成空文本；
   - 历史冲突记录没有在多次校验后正确闭环；
   - 这些问题不需要重新调用 8 天 agent，只需要修正状态、证据识别和派生文件。

本次采用的处理方式是：

```text
冻结现有结果
  → 逐条修改 lifestyle / POI 内容
  → 只对改过的条目定向模型复核
  → 回填 citation_status、review_id、review_note
  → 同步 manifest、references、final_guide、site
  → 验证没有残留 force_finalized
```

最终完成了 32 条 lifestyle 和 10 个 POI 详情的状态闭环，没有重跑整条 8 天游程。这证明“定向修补 + 复核”可以成为正式产品能力，而不是一次性人工补丁。

## 3. 设计目标

### 3.1 目标

- 对局部问题进行最小范围修改；
- 把“能解决”“不能解决”“误报”“需要重跑”明确区分；
- 允许人工复核、强模型复核和轻量模型复核协作；
- 修改后必须有可追溯记录；
- 把重复出现的问题沉淀为代码 bug、规则缺陷、数据缺口或回归测试；
- 不让历史攻略因为一次修补而被整篇重写；
- 让最终页面、引用和状态保持一致。

### 3.2 非目标

- 不把所有问题都强行标记为 `passed`；
- 不用定向修补替代路线结构变化时的全量重跑；
- 不把人工修改伪装成模型生成；
- 不把机械回填误认为内容已经被模型验证；
- 不在没有证据时凭空补充票价、开放时间、店名或路线细节。

## 4. 两维问题分类

问题不能只用一个“通过/未通过”字段表达。至少需要两个维度。

### 4.1 处理结果维度：现在怎么处理

```text
resolved        已解决
false_positive  误报
deferred        当前不能安全解决，暂缓
needs_rerun     必须重跑某个 agent 或整条流程
```

说明：`deferred` 不是失败，而是诚实地记录当前证据或决策不足；`needs_rerun` 也不一定意味着全量重跑，可以进一步指定局部 agent 或局部天数。

### 4.2 根因维度：为什么出现

```text
content         内容表达或事实陈述问题
evidence        证据不足、冲突或检索不完整
data            素材、缓存或外部数据缺失/过期
model           模型理解、归纳或输出问题
code            校验、状态机、索引或回填代码问题
presentation    final_guide、references 或 site 展示问题
policy          需要业务或用户偏好决策
```

一个问题可以同时是“已解决”和“代码根因”。例如：

```json
{
  "disposition": "resolved",
  "root_cause": "code",
  "resolution": "backfill_manifest",
  "needs_rerun": false
}
```

### 4.3 推荐的修复动作

```text
mechanical_backfill  只同步状态、索引、references 或页面
targeted_patch       只修改一个字段、一天或一条 POI 路线
human_review         人工判断、人工编辑或确认误报
strong_model_review  复杂内容的定向模型修补/复核
light_model_review   短文本或已修改条目的通过性复核
targeted_rerun       重跑一个 agent、一个字段或一个 POI
full_rerun           重跑完整主链路
defer                保留问题并等待新证据
```

## 5. 核心工作流

### 5.1 冻结基线

每次定向修补开始前，先记录：

- 项目 ID；
- 当前攻略版本；
- route、lifestyle、pitfall、POI 文件版本；
- 当前 references 和最终页面版本；
- 原始问题清单；
- 本次修补的操作者或触发任务。

修补应该基于一个明确版本进行，避免边修边生成导致无法判断差异来源。

### 5.2 生成问题单

问题可以来自：

- `citation_checker`；
- agent 自检；
- 整合冲突；
- 用户反馈；
- 管理后台人工检查；
- 回归测试；
- 历史状态扫描；
- 最终页面展示检查。

问题单不应只保存一句告警，还要保留目标范围、证据和当前状态。

### 5.3 先分类，再决定是否调用模型

建议顺序：

```text
读取目标对象
  → 检查是否为状态/索引问题
  → 检查是否已有足够证据
  → 检查是否只是表达过度
  → 检查是否为误报
  → 最后才决定是否需要模型或 agent 重跑
```

这样可以避免把机械问题、误报问题和内容问题都送进昂贵的生成流程。

### 5.4 最小范围内容介入

修补时优先修改最小单元：

```text
字段 > 条目 > 某一天 > 某个 POI > 某个 agent > 整条路线
```

修改原则：

- 保留有证据支持的内容；
- 对不确定内容改为条件式表达；
- 对建议性内容保留灵活性；
- 不因为某个数字不确定就删除全部建议；
- 不扩散修改无关天数和字段；
- 修改前后保留 diff。

### 5.4.1 Patch-first 原则

人工复核场景默认不直接覆盖源 JSON。所有内容修改先生成一个可审阅 patch：

```text
源文件保持不变
  → 生成 patch
  → 展示模型建议、证据和 diff
  → 人工接受或编辑 patch
  → 原子应用 patch
  → 累计本批影响范围
  → 人工启动一次本批验收
  → 定向 Checker / 必要时单次 Integration
  → 统一重建派生文件
```

这样可以保证：

- 人工在应用前能看到准确的修改范围；
- 拒绝或暂缓时不会污染当前有效版本；
- patch 可以撤销、重放和比较；
- 同一个 patch 可以关联模型建议、人工决定和回归测试；
- 源文件不会因为后台一次表单提交而失去版本边界。

只有纯机械的状态回填、manifest 同步和派生文件重建，才允许直接由确定性程序写入；内容修改必须经过 patch 过程。

### 5.4.2 Patch 冲突与版本校验

每个 patch 必须绑定生成时的源文件版本、目标 JSON 路径和内容 hash。应用前重新校验：

```text
源文件版本未变化
  → 允许应用 patch

源文件版本已变化
  → 标记 patch_conflict
  → 暂停应用
  → 基于最新源文件重新生成 diff 和模型建议
  → 人工重新确认
```

禁止用旧 patch 直接覆盖后来产生的内容。冲突记录至少包含：

- patch 生成版本；
- 当前源文件版本；
- 冲突 JSON 路径；
- 产生冲突的其他修改；
- 重新生成的建议 patch；
- 最终人工决定。

如果冲突只发生在无关字段，系统可以给出自动 rebase 建议，但仍需保留原 patch 和 rebase 后 patch 的关联关系。

### 5.4.3 撤销与回滚

已应用的 patch 可以撤销，但不能通过删除或覆盖历史记录实现。系统应根据原 patch 生成一个反向 `rollback_patch`：

```text
已应用 patch
  → 发现需要撤销
  → 生成反向 patch
  → 展示回滚 diff 和原因
  → 定向复核
  → 人工或系统批准
  → 原子应用 rollback patch
```

回滚也必须经过版本校验。如果源文件在原 patch 应用后又发生了其他修改，回滚先进入 `rollback_conflict`，不能直接恢复旧版本覆盖新内容。

原 patch、rollback patch 和最终状态全部保留，形成可追溯链：

```text
原始版本 → patch-001 → rollback-002 → patch-003
```

这样“撤销”是一次新的、可审计的内容变更，而不是抹掉历史。

### 5.5 定向模型复核

Finding 进入模型前必须先由程序解析唯一复核范围：有 `file + json_path` 时直接使用该字段；
有 Day、POI 或 item key 时定位对应业务对象；POI 历史失败才拆为路线段。Manifest 和最终交付契约
属于确定性一致性问题，不进入模型复核。无法形成精确范围时要求人工补充，不允许模型扫描整份攻略。

单次模型复核最多接受 16 个范围项。超过上限时显式拒绝并保留完整 Finding，由人工决定如何拆批；
不得静默截断。模型输出的条数、顺序和 `target_path` 必须与程序范围完全一致，否则不写入 recommendation。

模型复核的输入只包含：

- 修改后的目标条目；
- 修改前版本；
- 目标条目的来源证据；
- 用户需求中相关的硬约束；
- 当前问题和修补说明；
- 复核规则。

模型输出至少包含：

```json
{
  "passed": true,
  "issues": [],
  "remaining_uncertainty": [],
  "recommended_action": "accept"
}
```

模型复核必须重新阅读修补后的内容，不能只依据人工说“已经改好了”来放行。

### 5.6 回填与派生文件同步

已应用 Patch 不立即触发网站或完整下游链路，而是保留
`pending_targeted_validation` / `pending_integration`，由人工在完成本批修改后启动一次验收。
批次按字段、Day 和 POI 去重；一批调用一次 Integration 节点，并在最终交付契约通过后重建一次网站。

当本批内容通过后，按依赖顺序同步：

```text
源 JSON / POI 文件
  → manifest 和状态索引
  → references
  → final_guide
  → site.html
```

这一步默认是机械过程，不调用完整 agent。同步完成后必须做一致性检查。
验收失败时 Finding 返回复核队列，不自动递归修复或重试。路线结构 Patch 只完成普通批次隔离，需返回既有路线变更流程处理；复核中心不执行结构重跑。

### 5.7 验收

验收至少包括：

- 目标条目状态为 `passed` 或明确的其他终态；
- manifest 与详情文件一致；
- references 使用当前证据；
- final_guide 与结构化数据没有明显漂移；
- site 不再展示已解决的未完成提示；
- 没有残留旧的 `force_finalized`；
- review log 和 changelog 都有记录；
- 如果根因是代码问题，已经创建 bug 或回归测试任务。

### 5.8 Patch 影响范围与级联规则

Patch 应用后不默认全链路重跑，而是根据目标字段和依赖关系决定最小级联范围。

| Patch 范围 | 典型例子 | 必做动作 | 默认级联 |
|---|---|---|---|
| 展示层 | 标题、段落顺序、格式、链接 | 重新渲染并做页面检查 | 不触发 agent |
| 机械状态层 | `citation_status`、manifest、references | 确定性同步和一致性校验 | 不触发 agent |
| lifestyle 单字段 | food、outfit、lodging、play 的局部内容 | 定向 citation review、更新最终攻略 | 不重新跑 route |
| POI 单条路线/途经点 | 某个 POI 的路线描述或建议时长 | 定向 POI review、更新关联正文 | 不重新跑 route/lifestyle |
| route 单日内容 | 某一天的地点顺序或节奏 | route 定向复核 | 按依赖图触发受影响 lifestyle/POI |
| route 结构 | 增删天数、重排路线、替换核心目的地 | route agent 结构性重跑 | 级联所有受影响下游 |

默认规则可以概括为：

```text
展示修补       → 只渲染
内容修补       → 只复核目标条目
路线日级修补   → 复核目标天并按依赖级联
路线结构修补   → 结构性重跑
```

如果 patch 同时触及多个层级，以影响范围最大的层级为准；如果系统无法判断影响范围，进入人工确认，不自动扩大重跑范围。

## 6. 复核记录模型

建议新增统一的 `review_findings.jsonl`，并继续保留已有的 `review_log.jsonl` 作为不可变审计记录。

### 6.1 问题单字段

```json
{
  "finding_id": "finding-qinggan-day7-poi-001",
  "project_id": "custom-qinggan-linear-8d-v8-20260813",
  "baseline_version": "2026-08-22T02:00:00+08:00",
  "target": {
    "resource_type": "poi",
    "file": "outputs/poi_details/day7_山丹军马场.json",
    "json_path": "routes[0].route_segments[2].highlight",
    "day": 7,
    "scope": "single_field"
  },
  "problem_type": "unsupported_specificity",
  "disposition": "resolved",
  "root_cause": "content",
  "severity": "low",
  "rerun_cost": "high",
  "evidence_refs": ["青甘大环线-xxx-c001"],
  "before": "原始文本",
  "after": "修补后的文本",
  "repair_action": "targeted_patch",
  "verification_method": "strong_model_review",
  "verification_status": "passed",
  "review_id": "qinggan-20260822-xxx",
  "needs_rerun": false,
  "created_at": "2026-08-22T03:00:00+08:00",
  "updated_at": "2026-08-22T03:10:00+08:00"
}
```

### 6.2 内容状态与验证状态分离

不建议继续让 `citation_status` 同时承担所有语义。建议逐步拆成：

```json
{
  "content_status": "patched",
  "verification_status": "passed",
  "verification_method": "model_review",
  "needs_targeted_rerun": false,
  "review_id": "..."
}
```

这样可以区分：

- 内容已修改但还没复核；
- 内容没改，但误报已确认；
- 内容已通过，但 manifest 尚未同步；
- 内容已通过，但仍有动态信息需要现场确认。

### 6.3 统一审计主体

用户、运营人员、模型和系统都使用同一套审计记录，不为不同主体建立互相割裂的日志格式，只通过 `actor_type` 和 `actor_id` 区分来源。

```json
{
  "actor_type": "human | model | system",
  "actor_id": "admin_user | gpt-5.5 | citation_checker",
  "action": "propose | edit | approve | reject | backfill",
  "finding_id": "finding-xxx",
  "review_id": "review-xxx",
  "patch_id": "patch-xxx",
  "timestamp": "2026-08-22T03:30:00+08:00"
}
```

统一审计链必须能够回答：

```text
谁发现了问题？
谁提出了修补建议？
谁实际修改了内容？
谁批准了修改？
哪个模型参与过？
最终为什么通过或暂缓？
```

`review_log.jsonl` 继续作为追加式审计记录；`review_findings.jsonl` 作为当前问题状态和检索入口。两者通过 `finding_id`、`review_id` 和 `patch_id` 关联，而不是互相覆盖。

### 6.4 两层存储：项目事实与中央索引

复核记录采用“两层存储”而不是只依赖中央数据库：

```text
项目 workspace
  → 保存完整问题、证据、patch、模型建议和决定

中央运维数据库
  → 保存跨项目索引、队列、聚合统计、根因和优化候选
```

项目 workspace 是事实来源，中央数据库是运营索引。中央库不可用时，项目仍然可以离线解释、复核和重建；中央库恢复后再通过 `finding_id`、`review_id` 和 `patch_id` 增量同步。

中央库不应反向覆盖项目事实。需要修改项目内容时，必须回到项目 workspace 生成 patch，并把结果同步回中央索引。

建议同步字段包括：

- 项目 ID 和版本；
- finding 状态、风险等级和根因；
- 当前负责人和待办动作；
- review/patch 引用；
- 优化候选关联；
- 最后同步时间和同步版本。

## 7. 人工、强模型和轻模型如何协作

### 7.1 人工复核

适合：

- 用户偏好和业务取舍；
- 是否保留某个建议性内容；
- 证据冲突但没有客观优先级；
- 是否把一条内容降级为“可选项”。

### 7.2 强模型定向复核

适合：

- 多条素材综合判断；
- POI 深度路线；
- 复杂的事实与表达边界；
- 需要生成修补方案的条目。

### 7.3 轻量模型复核

适合：

- 已人工修改过的短条目；
- 只需要判断通过/不通过；
- 强模型输出超长或成本过高；
- 批量处理低风险问题。

轻量模型不能绕过证据输入，也不能作为所有复杂问题的默认降级方案。

### 7.4 人工确认不是从零审阅

对于需要人工最终确认的问题，模型仍然必须先生成一份“复核建议包”，人工不需要从空白开始重新调查。建议包至少包含：

- 问题摘要；
- 风险等级和风险原因；
- 模型建议的处理结论；
- 建议采用的修补文本；
- 修改前后 diff；
- 支撑建议的证据片段和来源；
- 仍然存在的不确定性；
- 模型建议的下一步动作；
- 是否建议重跑、暂缓或标记误报。

示例：

```json
{
  "recommendation": {
    "decision": "accept_patch",
    "confidence": "medium",
    "risk_level": "high",
    "summary": "现有素材支持该地点作为可选项，但不足以支持固定开放时间。",
    "proposed_text": "若当天开放和交通条件允许，再考虑该地点，具体以现场为准。",
    "evidence_refs": ["青甘大环线-xxx-c001", "amap-cache-xxx"],
    "uncertainties": ["开放安排可能随季节变化"],
    "recommended_action": "human_review"
  }
}
```

人工的职责是对建议包做最终业务判断，而不是重复模型已经完成的检索工作。人工可以：

```text
接受模型建议
人工改写后接受
拒绝模型建议
标记误报
暂缓并保留问题
要求定向重跑
```

产品语义应明确为：

```text
模型负责提出判断和修补建议
人工负责高风险内容的最终放行
系统负责保存证据、差异和决定过程
```

人工确认完成后，系统应同时记录：

```json
{
  "verification_method": "human_approved_model_recommendation",
  "human_decision": "accept_patch",
  "model_recommendation_id": "recommendation-xxx",
  "reviewer": "admin_user",
  "reviewed_at": "2026-08-22T03:30:00+08:00"
}
```

这可以避免两种极端：一是让模型独自放行高风险内容，二是让人工没有任何上下文、重新从头查资料。

### 7.5 风险分级与默认放行策略

定向复核不应所有问题使用同一套放行门槛。默认按内容对用户决策的影响分级：

| 风险级别 | 典型内容 | 默认处理 | 是否必须人工最终确认 |
|---|---|---|---|
| 低风险 | 穿搭、表达语气、建议性时长、文案收窄 | 轻量或标准模型复核后自动通过 | 否 |
| 中风险 | 餐厅、住宿、票价、普通开放时间、POI 细节 | 强模型定向复核，必要时抽样人工 | 默认否，冲突时是 |
| 高风险 | 道路通行、交通安全、能否赶上行程、核心路线顺序、重大时间约束 | 模型先生成复核建议包，人工最终确认 | 是 |

以下情况应自动升级一个风险级别：

- 证据来源互相冲突；
- 内容会导致用户产生不可逆或高成本决策；
- 涉及安全、封路、航班、高铁或长途驾驶；
- 模型置信度低或无法解释判断依据；
- 同一问题历史上曾多次复发；
- patch 同时影响多个 agent 或多个天数。

风险级别是默认策略，不是绕过人工判断的绝对规则。人工可以将问题升级，但不能把高风险问题降级后自动放行而不留下理由。

### 7.6 动态信息的运行时提醒

票价、开放时间、道路通行、天气、预约规则等内容，即使当前证据和模型复核都通过，也可能在出行前发生变化。这类内容不应被标记为“未解决”，而应使用单独的运行时提醒语义：

```json
{
  "verification_status": "passed_with_runtime_check",
  "runtime_check_required": true,
  "runtime_check_note": "出发前以景区官方公告、当天导航和现场安排为准。"
}
```

展示层统一使用短提醒，避免把风险说明写成阻塞性错误：

```text
当前信息已完成复核；票价、开放时间、交通和天气可能变化，请出发前以官方公告、当天导航和现场安排为准。
```

这表示“当前内容可以使用，但执行前需要留意变化”，而不是“当前内容没有通过”。

## 8. 系统优化闭环

每次问题处理完成后，系统要额外判断：

```text
这是一次内容修补，还是暴露了系统缺陷？
```

### 8.1 典型根因到系统动作

| 发现模式 | 根因 | 后续动作 |
|---|---|---|
| `amap` 证据被当成空文本 | citation checker 代码缺平台分支 | 修复解析器 + 增加回归测试 |
| `web_verified` 未合并 | 证据归一化规则不完整 | 扩展 evidence normalizer |
| 详情文件通过但 manifest 未更新 | 状态同步缺少统一入口 | 增加生成后 manifest 校验 |
| 多次出现旧 `force_finalized` | 历史状态迁移不完整 | 增加 status migration 命令 |
| 模型把建议时长当成硬事实 | prompt/schema 没区分建议值与事实值 | 增加字段语义和提示规则 |
| references 与正文漂移 | 派生文件没有从单一来源重建 | references 改为确定性重建 |
| 同类表达反复被人工收窄 | 缺少领域修补模板 | 沉淀 rewrite policy / rule |
| 模型输出过长导致复核失败 | 复核任务粒度过大 | 增加短提示重试和模型降级策略 |

### 8.2 代码 bug 必须进入回归体系

如果问题根因是代码，处理完成后不能只写一条日志，还应至少做一项：

- 新增单元测试；
- 新增 fixture；
- 增加状态一致性扫描；
- 增加生成后断言；
- 更新 `docs/ISSUES.md`；
- 更新对应 agent prompt 或 schema；
- 建立 bug 与复核记录的关联。

目标不是让某个项目变干净，而是让下一个项目少出现同类问题。

### 8.3 重复问题自动升级为优化候选

定向复核系统应持续聚合同类问题，而不是把每次修补都当作孤立事件。默认升级规则：

```text
同一项目内同类问题重复 2 次
或多个项目累计出现 3 次
→ 自动创建系统优化候选
```

以下根因第一次出现就可以创建候选，因为它们通常会造成批量误报或状态污染：

- 证据平台解析错误；
- citation checker 规则错误；
- manifest / references / final_guide 状态不同步；
- 状态机迁移错误；
- 生成后校验缺失；
- 涉及安全信息的错误判断。

优化候选至少包含：

- 相关 finding 列表；
- 归一化后的根因；
- 影响项目和条目数量；
- 当前 workaround；
- 建议修改的代码、规则、prompt 或 schema；
- 建议新增的回归测试；
- 是否需要迁移历史文件。

自动升级只负责创建候选和整理证据，不自动修改生产代码。代码变更仍需工程确认、测试和发布；发布后还要重新运行历史 finding，确认问题确实减少。

## 9. 管理运维系统形态

建议新增“定向复核中心”，而不是把所有问题塞进“重新生成”按钮。

### 9.1 问题列表

支持按以下维度筛选：

- 项目；
- 天数；
- agent；
- 内容类型；
- 严重程度；
- 能否解决；
- 是否误报；
- 是否需要重跑；
- 根因类型；
- 当前复核人或模型。

### 9.2 问题详情

详情页至少包含：

- 原文；
- 修补后文本；
- diff；
- 证据片段；
- 引用来源；
- 问题分类；
- 推荐修复动作；
- 模型判断理由；
- 模型复核建议包；
- 风险等级和建议动作；
- 历史复核记录；
- 相关代码 bug 和回归测试。

### 9.3 操作入口

```text
接受定向修改
人工确认模型建议
人工编辑后复核
让强模型重判
让轻量模型检查
标记误报
暂缓处理
重跑当前字段
重跑当前 agent
重跑整条路线
创建系统 bug
```

所有操作都应产生审计记录，不允许后台直接覆盖文件而没有 provenance。

## 10. 与现有架构的关系

这套能力应该复用已有结构，而不是新建一套平行系统。

### 10.1 与 agent 的关系

- 生成链中的 `route_agent`、`lifestyle_agent`、`poi_detail_agent`、`citation_checker` 和 `integration_agent` 仍负责首次生成及原有 edit 流程；
- 生成后的定向复核由独立的 `src/review/` 服务承担，不进入 `build_graph()`，也不直接复用生成 agent 的写工具；
- `src/review/agent.py` 只读调查目标 JSON、素材原文、全目的地候选、历史 recommendation / review log，必要时联网补证，最终只提交结构化 `ReviewRecommendation`；
- JSON patch 的提出、审批、版本校验、应用、拒绝和回滚由确定性 store 完成，模型不能直接修改 workspace；
- `src/review/policy.py` 生成确定性的影响分类和级联计划；`src/review/validation.py` 在人工启动后合并待验收 Patch，定向运行 Checker、调用一次 Integration 节点，并仅在最终交付契约通过后统一重建网站。路线结构 Patch 只做隔离，需回到既有路线变更流程。

### 10.2 与现有工作区的关系

当前实际分层：

```text
workspace/route_plan.json
workspace/outputs/lifestyle.json
workspace/outputs/pitfall.json
workspace/outputs/poi_details/*.json
workspace/review_log.jsonl
workspace/changelog.jsonl
```

```text
workspace/review_findings.jsonl
workspace/review_log.jsonl
workspace/review_runs/<recommendation_id>/recommendation.json
workspace/review_runs/<recommendation_id>/item_decisions.jsonl
workspace/review_runs/<patch_id>/patch.json
workspace/optimization_candidates.jsonl
```

项目 workspace 是完整事实来源。项目目录同级的 `runtime/review_index/` 只保存可重建的跨项目队列和聚合索引，不是中央数据库，也不反向覆盖 workspace。

派生的 `final_guide.md`、references 和 `site.html` 仍由确定性程序重建，避免多个来源互相漂移。

## 11. 版本路线与当前完成度

### MVP：后端闭环（已实现）

当前 CLI、API 和确定性 store 共用同一套 finding、recommendation、item decision、patch 与审计模型，已覆盖：

1. 追加式 finding、review log、recommendation、item decision 和优化候选；
2. 单个 JSON 目标的 patch-first 修改，带源文件 SHA256、原子写入和冲突检测；
3. patch 的 propose / apply / reject / rollback 及冲突状态；
4. 人工 resolved / false_positive / deferred / needs_rerun 等结论；
5. 只读 agentic 定向复核；程序统一解析精确目标，最多 16 项，超过上限显式拒绝拆批；模型输出必须逐项按路径对齐；
6. 本地素材不足时的联网候选补证，但联网结果仍只作为待裁决证据；
7. recommendation 的 claim 级证据账本，以及逐条采纳、修改后采纳、拒绝、暂缓；
8. 生成后状态扫描、影响分类、级联计划和统一审计链；
9. 人工触发的项目级 Patch 批次验收，定向 Checker、必要时一次 Integration、通过后一次网站重建；
10. 可重建的跨项目 review index。

后端已能组合完成以下闭环，但仍需按具体项目运行验证，不应把“能力存在”写成“所有历史项目已处理”：

```text
创建 finding
  → 生成模型建议
  → 生成 patch
  → 人工批准 patch
  → 原子应用
  → 记录逐条人工决定
  → 人工启动本批定向验收或生成反向 patch
  → 验证状态和审计记录
```

管理后台直接消费同一套 finding、patch 和 review 数据，不另造一套业务逻辑。

### V1：管理运维（基础闭环已实现）

- 已实现：跨项目问题队列、详情、当前内容、证据面板、recommendation 展示、逐条人工决定和全目的地候选查看；
- 已实现：patch propose / apply / reject / rollback、影响预览、级联计划，以及复核中心的 Patch 应用和“开始本批验收”入口；
- 尚未实现：通用可视化 diff 编辑器、模型渠道选择、跨项目批量处理同类问题、路线结构重跑入口。

### V2：系统优化闭环

1. 优化候选和跨项目索引已有基础实现，但还不是完整中央数据库；
2. 自动生成代码 bug 草稿；
3. 自动建议回归测试；
4. 统计每类问题的复发率；
5. 根据历史数据优化“重跑还是修补”的策略；
6. 将稳定的修补模式沉淀为 Skill、规则或 prompt 片段。

## 12. 需要继续讨论的关键问题

这部分是下一轮 grill 的重点，不在设计稿中假设答案：

1. **已形成原则、尚未自动执行：** 模型输出风险和建议动作，但当前仍以人工决定为安全边界；低风险自动通过的阈值和启用条件待定义。
2. **模型渠道已可选择、策略未自动化：** 后端支持 `cunai`、`full`、`light` 等已有渠道，尚未按风险自动选择强弱模型，也未在前端提供渠道选择器。
3. **已形成原则：** 内容修改先形成 patch，人工审批后原子应用；patch 绑定源版本，发现版本变化就暂停并重新生成建议，不允许旧 patch 盲目覆盖新内容。
4. **已形成原则：** 用户、运营、模型、系统统一使用同一套审计记录，通过 `actor_type` 和 `actor_id` 区分，并通过 `finding_id`、`review_id`、`patch_id` 建立关联。
5. **已形成原则：** 动态信息使用 `passed_with_runtime_check`，内容仍视为已复核，同时在最终攻略和站点显示“以实际为准”的运行时提醒，不把它当成未解决问题。
6. **系统 bug 是自动创建，还是由运营确认后创建？**
7. **已实现基础升级：** 同一项目重复2次或跨项目累计3次可创建优化候选，高影响根因可首次触发；根因归一化、跨表达去重和长期统计仍需增强。
8. **是否允许批量把同一类表达统一收窄？** 批量操作的安全边界是什么？
9. **已形成默认规则：** 展示层和机械状态层不级联，lifestyle/POI 单字段只做目标复核，route 日级内容按依赖图级联，路线结构变化才升级为结构性重跑；还需要补齐字段级影响图和异常兜底。
10. **已形成存储原则：** 项目 workspace 保存完整事实，中央数据库保存跨项目索引和聚合；中央库不反向覆盖项目事实；撤销通过反向 patch 实现，不删除历史；还需要确定保留期限、脱敏规则和同步失败重试策略。

## 13. 当前设计结论

青甘案例说明，长路线攻略不应该只有“重新生成”一种维护方式。

更合理的产品闭环是：

```text
全量生成负责建立初稿
定向修补负责低成本纠错
人工/模型复核负责确认结果
机械同步负责保持派生文件一致
根因分析负责推动代码和规则改进
回归测试负责防止问题再次出现
```

这套闭环的亮点不只是节省模型调用成本，更重要的是把每次人工或模型介入都转化为系统学习：

```text
一次修补 → 一条复核记录
多次同类修补 → 一个优化候选
优化候选 → 代码/规则/prompt/测试改进
改进后 → 下一次生成更少误报、更少人工介入
```

因此，“定向修补与持续优化”应被视为攻略系统的正式产品能力，也可以进一步封装为一个 Skill，供主 agent、管理后台和运维任务共同调用。

## 14. MVP 实现映射

本设计已经有第一版可运行实现：

| 设计能力 | 当前实现 |
|---|---|
| finding / patch / review 数据模型 | `src/review/models.py` |
| 追加式 finding、审计和优化候选存储 | `src/review/store.py` |
| Patch-first、版本校验、原子应用 | `src/review/store.py` |
| patch 冲突、拒绝和 rollback | `src/review/store.py` |
| 定向模型建议包 | `src/review/advisor.py` |
| 只读 agentic 复核、长范围分批与二次收口 | `src/review/agent.py` |
| 运维 CLI | `run_review_mvp.py` |
| 管理后台基础 API、影响预览、跨项目索引 | `src/api/routers/reviews.py`、`src/review/index.py` |
| patch 影响分类与级联计划 | `src/review/policy.py`、`PatchRecord.cascade_plan` |
| 人工触发的 Patch 批次验收 | `src/review/validation.py`、`src/api/jobs.py`、`src/api/routers/reviews.py` |
| 管理后台基础复核中心 | `frontend/src/pages/ReviewCenterPage.tsx` |
| claim 级证据与逐条人工决定 | `src/review/models.py`、`src/review/store.py`、`frontend/src/pages/ReviewCenterPage.tsx` |
| 回归测试 | `tests/test_review_loop.py`、`tests/test_api_reviews.py`、`tests/test_review_agent.py` |
| 生成后状态扫描 | `validate_project_state()` |

当前 MVP 有意保留的边界：

- 尚未接入中央数据库；当前跨项目索引落在项目目录同级的 `runtime/review_index/`，项目 workspace 仍是唯一事实来源；
- Patch 应用后不会自动逐次触发下游；系统累计精确影响范围，由运营在复核中心一次启动本批验收；路线结构修改继续单独处理；
- 复核中心已提供队列、状态扫描、证据面板、模型建议、逐条决定、Patch 应用和批次验收；通用 diff 编辑与跨项目批量操作未完成；
- 模型建议由 `advisor.py` 持久化，`agent.py` 负责只读调查与结构化裁决；范围超过 16 项时显式要求拆批，不静默漏项；
- agentic 复核的默认渠道是 `cunai`，真实调用依赖对应环境变量；自动化测试主要验证确定性逻辑和 mock 模型行为，不等于所有渠道都完成真实 E2E；
- references、final_guide 和 site 的重建仍由现有确定性生成脚本负责。

青甘项目已有一次历史状态扫描记录：当时未发现残留 `force_finalized`，POI manifest 与详情文件一致。该记录只能证明当次扫描结果，不能替代后续代码变化后的重新验证。

## 15. 项目级旅游攻略复核Skill

当前Codex执行协议和命令入口见：

- [`.agents/skills/travel-guide-targeted-review/SKILL.md`](../.agents/skills/travel-guide-targeted-review/SKILL.md)
- [`src/review/skill_bridge.py`](../src/review/skill_bridge.py)

Skill与bridge定义标准证据包、结构化结果、只读调查和确定性导入门禁；它们不会直接调用生成Agent、应用Patch或提升当前Recommendation。项目级Skill、适配层、隔离测试、成都真实finding的Codex影子测试和复核中心bundle bridge接入已经完成。当前仍未实现服务端直接启动Codex，也没有在真实业务正文上完成Patch应用与回滚E2E。

开发取舍、协议演变和首次影子测试记录保留在已归档的 [`TRAVEL_GUIDE_TARGETED_REVIEW_SKILL.md`](TRAVEL_GUIDE_TARGETED_REVIEW_SKILL.md)；系统观察进入持续改进后的当前设计见 [`CONTINUOUS_QUALITY_EVOLUTION_DESIGN.md`](CONTINUOUS_QUALITY_EVOLUTION_DESIGN.md)。
