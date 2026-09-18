# 路线可执行性核验闸门实施设计

> 状态：Feasibility V1已实现并接入主编排；已有真实provider+Web运行证据，restriction适用日窄修已通过确定性回归，检索质量与完整E2E覆盖仍待验收（更新于2026-09-07）
>
> 日期：2026-08-27
>
> 当前生产事实以 `src/materials/route_feasibility_check.py`、`src/orchestrator.py`、`src/workspace/schema.py` 和对应测试为准。已完成POI主动目标、道路provider、可解析时间线、restriction三态适用日、同行人资质、四状态报告、旅行日期口径、事实修复、结构proposal、最终攻略披露和`full -> mimo`核验模型兜底。结构化交通段/时间槽仍属V2。

## 1. 目标

把现有 `route_timeliness_check_node` 从“对 notes 中已出现的道路时效信息做补漏”升级为路线下游调研前的可执行性核验闸门，同时保留已经验证有效的道路检查作为一个子检查。

目标流程：

```text
用户确认路线方向
  -> 路线引用真实性核验
  -> 路线可执行性核验
       -> 核心 POI 当前开放状态
       -> 预约、限流、资质和最晚入园
       -> 当天时间线与开放时间的硬冲突
       -> 道路、交通和入口可用性
       -> 季节性关闭或自然景观物理状态变化
       -> 旅行日期和同行人群触发的特殊限制
  -> passed / warning：进入衣食住行、避雷和 POI 详情
  -> blocked：生成路线结构调整提案，等待用户确认
  -> unknown：重试后仍失败则显式降级，不伪装成 passed
```

本文只定义路线骨架成为下游结构基线之前的核验。它不替代生成完成后的定向复核，也不扩展为天气、价格、优惠和临时活动的全量旅游风控平台。

## 2. 已核实的当前状态

### 2.1 已经存在且应保留

- 节点位置正确：路线引用核验之后、衣食住行之前。
- Route Agent prompt 已要求主动联网核实道路、预约、门票状态、自然景观物理状态和部分时间冲突。
- 普通修复已有 `write_route_facts_patch`，物理上只能修改 `notes/sources`。
- 硬问题已有 `route_change_proposal.json`、`confirm/keep/revise` 和 stale-base 防护。
- 用户确认应用结构提案后，会重新触发路线引用和时效核验。
- 当前plan流程会全路线复核；edit流程已用proposal实际变更天覆盖原始edit范围，避免核验错天。本文后续“只重查受影响天”仍是Feasibility V1对plan流程的目标，不代表plan增量核验已经实现。
- 现有道路检查已有真实青甘 E2E 正面证据，应作为 provider 保留，不应推翻重写。

### 2.2 已确认的问题

1. 核验目标只从 `day.notes` 抽道路编号和少量固定地标；没有道路标识的天直接跳过。
2. 正式结构中的 `day.pois` 和 `needs_detail` 没有参与系统级检查目标构造。
3. Web 未配置、无结果、请求异常和判断模型异常全部静默放行，没有 `unknown`。
4. ~~`requires_structure_change=true` 只有在 `missing_in_notes` 非空时才能进入 issues。~~ 2026-08-27已修复并加回归测试；notes已写明风险但路线仍不可执行时仍会进入proposal。
5. Web query已改用当前UTC年份，但仍没有正式旅行日期字段，跨年旅行可能查询口径不准。
6. Requirements 没有正式旅行日期字段，只能从自由文本软推断月份或季节。
7. `RouteDay.citation_status` 同时承载引用和时效强制终结，无法表达 `warning/blocked/unknown`。
8. `RouteDay` 没有正式交通段和时间槽；严格的长距离交通及开放时间冲突检查仍依赖 notes 自由文本。
9. 已新增结构问题到proposal确认/保留闭环的直接测试；真实provider+真实Web已产生持久化运行证据，但场景覆盖、来源相关性和用户最终产物仍需继续验收。

## 3. 设计原则

1. **正式结构主动派生目标**：不能要求 Route Agent 先想起风险并写进 notes，核验器才看得见。
2. **生成软约束与系统门禁分层**：保留 Agent 主动核实规则，但不把其是否执行当成系统保证。
3. **技术失败不是通过**：搜索失败、来源冲突和日期不足必须落为 `unknown`。
4. **事实与结构分权**：普通问题只改事实层；结构问题只生成候选，不静默改正式路线。
5. **证据可追踪**：记录 URL、摘录、查询、检查时间和适用日期，不只保存模型总结。
6. **最小影响面**：只重查受影响天；不因单个 warning 全量重跑路线。
7. **兼容旧项目**：新增字段使用默认值；旧 `route_plan.json` 不要求迁移即可读取。
8. **诚实命名**：第一期没有结构化交通段前，只宣称覆盖已实现的检查项，不把自由文本推断包装成确定性验证。

## 4. 实施范围

### 4.1 V1 必做

1. 修复现有结构问题被吞 bug。
2. 去掉年份硬编码。
3. 增加正式 POI 目标构造：每个 `day.pois` 都进入开放状态检查。
4. `needs_detail=true` 的 POI 增加预约、限流、资质、最晚入园检查优先级。
5. 对自然景观覆盖长期关闭、季节性关闭和重大物理状态变化。
6. 保留现有道路标识检查。
7. 增加 `passed/warning/blocked/unknown` 四状态。
8. 把完整检查报告保存为独立 workspace 文件。
9. 接入已有事实修复和结构 proposal 流程。
10. 增加旅行日期口径及无日期降级规则。
11. 补齐单元、节点和确认闭环测试。

### 4.2 V1 暂不做

- 所有餐厅和酒店的营业状态核验；它们属于 lifestyle 层后续能力。
- 票价、优惠、天气、临时活动作为阻断条件；第一期最多记为 warning。
- POI 内部每条步道、索道和途经点的完整核验；它们属于 poi_details 层。
- 自动改变用户确认过的 POI、顺序、所属天或天数。
- 用一次搜索无结果证明某地不存在或关闭。

### 4.3 V2 才能完整解决的部分

要稳定覆盖“每个长距离交通段”和“到达晚于最晚入园”，需要给路线增加结构化交通段和时间槽。V1 可从现有 `city/area/pois/notes` 做保守判断，但解析不出来时必须为 `unknown`，不得视为通过。

建议的向后兼容结构：

```yaml
route_legs:
  - from_name
    to_name
    mode
    road_identifiers
    planned_departure
    planned_arrival
    duration_minutes

schedule:
  - target
    planned_start
    planned_end
```

V2 实施时需要同步 Route Agent 写入工具、prompt、route schema、前端展示和旧项目兼容测试，不混入 V1 第一轮改动。

## 5. 旅行日期口径

Requirements 新增可选字段：

```json
{
  "travel_start_date": "2026-10-02",
  "travel_end_date": "2026-10-09",
  "travel_date_text": "国庆前后",
  "travel_date_precision": "exact|month|season|unknown"
}
```

处理规则：

- `exact`：按每一天的真实日期和星期核验。
- `month`：核验月份、季节性政策和长期运营状态；星期类限制为 `unknown/warning`。
- `season`：只核验季节性关闭、长期政策和重大物理状态。
- `unknown`：以生成日期作为“本次查询基准”，结果不得表述为用户出发时必然有效，并增加出发前复查提示。
- 日期不是需求收集硬必填项。用户已明确给出日期时结构化提取；路线存在关键日期依赖且完全无日期时，才低打扰补问一次。

查询年份优先使用适用旅行日期年份；否则动态使用当前年份。

## 6. 检查目标构造

新增纯函数 `build_route_feasibility_targets(route_plan, requirements, days=None)`，输出去重后的目标列表。

### 6.1 POI 目标

`RouteDay.pois` 中每个 POI 都是路线骨架层的核心 POI，不能再额外依赖“core”布尔字段。

每个 POI 最少生成一个 `poi_operational_status` 目标。`needs_detail=true` 时，同一次 Web 搜索结果还要判断：

- `reservation_or_quota`
- `entry_qualification`
- `opening_window`
- `last_entry`

自然景观类 POI 还判断：

- `seasonal_closure`
- `physical_state_change`

类别判断可由窄范围结构化模型完成，但目标来源必须始终是正式 POI 列表；模型不能决定跳过整个 POI。

### 6.2 道路目标

复用现有 `_extract_road_identifiers` 和道路查询逻辑，作为 `road_operational_status` provider。

V1 仍允许从 notes 补充道路编号、垭口、入口和特殊活动，但 notes 只作为附加目标来源，不能再是全部目标来源。

### 6.3 时间线目标

当 notes 包含可解析的到达、入园或游览时段时，生成 `schedule_vs_opening_window` 目标。无法可靠解析时返回 `unknown`，并在报告中写明缺少结构化时间槽。

V1 不为了解析成功率增加复杂正则集合；时间线结构化在 V2 解决。

### 6.4 查询预算

- 同一 POI 一次查询尽量同时覆盖开放、预约、限流、资质和最晚入园，不按类别重复搜索。
- 相同规范化 POI、适用日期和查询类型在单次 run 内去重。
- edit 模式只生成 `edit_changed_route_days` 范围内目标。
- 首轮后的自动复核只重跑上轮`warning/unknown`的`check_id`；同一POI的多个未解决类别仍合并为一次Web搜索。
- scoped/edit复核将新结果合并回完整`route_feasibility.json`，不得用局部报告覆盖其他天的已有检查。
- 搜索结果继续写现有结构化 `web_search_log.jsonl`，报告引用其中的 URL、excerpt 和 created_at。

## 7. 输出数据结构

新增 `workspace/route_feasibility.json`：

```json
{
  "schema_version": "route-feasibility-v1",
  "route_plan_updated_at": "",
  "route_plan_structure_hash": "",
  "requirements_date_context": {
    "precision": "exact|month|season|unknown",
    "start_date": null,
    "end_date": null,
    "source_text": "",
    "query_baseline_date": "2026-08-27"
  },
  "overall_status": "passed|warning|blocked|unknown",
  "checks": [
    {
      "check_id": "",
      "day": 1,
      "target": "莫高窟",
      "target_type": "poi|road|schedule",
      "category": "opening_window",
      "status": "passed|warning|blocked|unknown",
      "summary": "",
      "evidence": [
        {
          "title": "",
          "url": "",
          "excerpt": "",
          "source_type": "official|government|map|verified_web|ugc|unknown",
          "observed_at": ""
        }
      ],
      "checked_at": "",
      "applicable_date": "",
      "requires_structure_change": false,
      "suggested_action": "",
      "technical_error": null
    }
  ],
  "created_at": ""
}
```

### 7.1 状态定义

- `passed`：有当前、相关且足够可信的证据支持该目标可执行。
- `warning`：路线可保留，但需要补充预约、时间、价格免责声明或风险提示。
- `blocked`：权威或高可信证据表明原路线不能按当前结构完成。
- `unknown`：联网失败、来源冲突、缺少适用日期或缺少足够信息，不能做可靠结论。

### 7.2 汇总规则

```text
存在 blocked -> overall_status=blocked
否则存在 unknown -> overall_status=unknown
否则存在 warning -> overall_status=warning
否则全部 passed -> overall_status=passed
```

`unknown` 的排序高于 `warning`，因为它表示系统不知道，而不是已经确认可保留。

### 7.3 运营事件适用日

`PoiOperationalEvent` 的 `applies_on_applicable_date` 是 Route Feasibility 内部三态字段，不进入 Workspace Schema。仅 restriction 使用该字段：明确不适用于实际旅行日时为 `false` 并保持 Judge 的 `passed`；明确适用时为 `true` 并提升为 warning；证据不足或旧 Provider 输出为 `None` 时继续保守 warning。对精确旅行日，发生在旅行日之后的 closure、reopening 和 restriction 不参与当日状态时间线。不得仅按公告年龄猜测 restriction 已失效。

## 8. 闸门策略

### 8.1 passed

直接进入下游。

### 8.2 warning

调用 `write_route_facts_patch` 更新必要的 `notes/sources`，只重新核验上轮未解决的`check_id`。通过或有界重试耗尽后按显式降级规则进入下游。

### 8.3 blocked

1. 写 `conflicts.jsonl`，`issue_type=route_feasibility_blocked`。
2. 调用现有 proposal-only Route Agent。
3. 保存 `route_change_proposal.json`，正式路线保持不变。
4. 用户选择 `confirm/keep/revise`。
5. `confirm` 后重新核验受影响天；`keep` 时吸收安全的事实提示，并把该检查保留为未解决/强制保留，不能标为 passed。

### 8.4 unknown

1. 对技术失败做有限重试。
2. 仍失败则写入报告和 conflict，不进入事实修复 Agent 循环，因为 Agent 无法修复网络或日期缺失。
3. 沿用项目现有“可强制终结但必须可见”的原则：允许下游继续，但 route feasibility 状态保持 `unknown/force_finalized`，不得改写为 passed。
4. 核心 POI 或关键交通目标为 unknown 时，在最终攻略中增加出发前复查提示。

这里选择“显式降级后继续”，而不是无限阻塞整个生成流程，兼顾可靠性和现有系统可用性。

## 9. 事实修复与结构提案

### 9.1 事实修复

以下问题只允许更新 `notes/sources`：

- 需要预约或限流；
- 开放时间、最晚入园和入口提醒；
- 票价或政策可能变化的免责声明；
- 可绕行但需要提醒的道路施工；
- 出发前需要再次核实的 unknown。

继续使用 `write_route_facts_patch`，不扩大其参数。

### 9.2 结构提案

以下问题进入 proposal：

- 核心 POI 长期闭园或适用日期明确闭园；
- 道路/交通中断且没有可在原结构内完成的绕行；
- 到达时间晚于最晚入园，且仅改 notes 无法解决；
- 季节性景观在适用日期无法游览；
- 用户日期或同行资质使核心安排不可执行。

复用现有 `write_route_change_proposal`、stale-base 防护和确认节点。V1 不新增第二套 proposal schema。

## 10. 编排调整

第一阶段保留图节点名称 `route_timeliness_check_node`，避免一次性修改 checkpoint/state 路由兼容面；节点内部改为调用新的 feasibility service。用户可见文案和文档改称“路线可执行性核验”。完成稳定验证后，再单独评估是否重命名内部节点。

建议内部拆分：

```text
route_timeliness_check_node
  -> build_route_feasibility_targets
  -> run_poi_feasibility_checks
  -> run_road_timeliness_checks
  -> run_schedule_checks
  -> persist_route_feasibility_report
  -> apply_gate_policy
```

旧 `verify_route_timeliness` 可暂时保留为道路 provider，待调用点全部迁移后再决定是否改名，避免大爆炸式重构。

## 11. 文件级实施清单

### 11.1 第一批：现有正确性热修（2026-08-27已完成）

- `src/materials/timeliness_check.py`
  - 结构问题不再依赖 `missing_in_notes` 非空。
  - 查询年份动态生成。
  - 修正文档中 light/full model 的陈旧描述。
- `tests/test_materials.py`
  - 增加“notes 已完整但必须结构调整”测试。
  - 增加动态年份测试。
- `tests/test_orchestrator_core_nodes.py`
  - 增加 blocked -> proposal 的节点测试。

### 11.2 第二批：Feasibility V1 数据和服务

- 新增 `src/materials/route_feasibility_check.py`
  - 目标构造、provider 编排、状态汇总和报告生成。
- `src/workspace/schema.py`
  - 增加 feasibility report/check/evidence 模型。
- `src/workspace/store.py`
  - 增加报告原子写入和读取；写入 actor 限定为 `citation_checker`。
- `src/materials/web_evidence.py`
  - 复用现有结构化 Web 日志读取，必要时增加按 query/时间读取最新 entry 的帮助函数。
- `tests/test_route_feasibility.py`
  - 新增目标构造、四状态、汇总和旧路线兼容测试。

### 11.3 第三批：编排与日期

- `src/orchestrator.py`
  - 接入 feasibility report 和 gate policy。
  - `unknown` 与业务问题分流，避免把技术失败打回 Route Agent。
  - proposal 应用后只核验受影响天。
- `src/agents/route_agent/prompt.md`
  - 保留主动核实规则，更新系统闸门说明，不把 Agent 自检描述成最终保证。
- Requirements 抽取结构
  - 增加旅行日期可选字段和低打扰补问条件。
- 编排测试
  - 覆盖 passed/warning/blocked/unknown 四条主路径。

### 11.4 第四批：文档同步

代码和测试完成后再同步：

- `docs/ARCHITECTURE.md`：职责边界和事实/结构分权。
- `docs/ORCHESTRATION.md`：节点行为、路由和确认后重查。
- `docs/WORKSPACE.md`：`route_feasibility.json` schema 和写权限。
- `docs/AGENTS.md`：Route Agent 与系统 checker 的职责边界。
- `docs/ISSUES.md`：真实问题、根因、修复和验证证据。
- `docs/VISION.md`：对应能力状态从设计未实现更新为已实现时再改标签。

## 12. 测试矩阵

### 12.1 纯函数和 store

- 所有正式 POI 都产生检查目标。
- `needs_detail` 只增加优先检查类别，不决定 POI 是否被检查。
- edit days 正确收窄目标。
- 相同 POI/日期/query 去重。
- 旧 RoutePlan 无新增字段仍可读取。
- 报告包含 route plan hash，路线变化后旧报告判 stale。
- 四状态汇总优先级正确。

### 12.2 Provider

- POI 正常开放 -> passed。
- 需预约但可执行 -> warning。
- 长期闭园 -> blocked。
- 自然景观重大状态变化 -> blocked 或 warning，按是否仍具备核心游览价值判定。
- 搜索失败、无结果、来源冲突、模型异常 -> unknown。
- 道路 notes 完整但路线仍不可执行 -> blocked，不得被吞。
- 查询年份来自适用日期或当前时间。

### 12.3 编排

- passed 直接进入 lifestyle。
- warning 事实修复后复核并进入 lifestyle。
- blocked 生成 proposal，正式路线不变。
- confirm 应用候选并重查受影响天。
- keep 保留原结构、吸收安全事实、状态不伪装为 passed。
- revise 废弃旧 proposal 并生成新候选。
- unknown 重试耗尽后显式强制终结，不调用 Route Agent 解决技术失败。
- edit 模式只检查改动天。

### 12.4 真实验证

截至2026-09-03，多个真实Project已经生成`route_feasibility.json`和Web证据记录。例如`projects/codex-emeishan-3d-full-plan-route-only-20260903-v1-current-code/`中的结果包含`verified_web`和政府来源，并记录本次观察时间。这证明provider与Web链路已经真实执行并落盘，因此不再标记为“尚未执行”。

现有结果同时暴露了检索质量边界：部分召回内容与检查目标相关性较弱，系统会在证据不足时保留`unknown`或风险提示。已有运行证据不等于以下完整场景矩阵均已验收：

1. 青甘长线：道路封闭、自然景观物理状态、长距离日。
2. 峨眉山：预约限流、资质/人群政策、季节或施工限制。
3. 城市短线：普通 POI 开放状态，验证不会因过度检查制造大量误报和成本。

后续完整 E2E 仍必须同时核对：

- `route_feasibility.json`；
- Web evidence log；
- conflict/proposal/changelog；
- 最终攻略是否正确呈现 warning/unknown；
- 下游是否只在允许状态启动；
- 搜索次数和整体运行时间是否可接受。

完整E2E前可先用成本受控的smoke runner。默认只预览目标和预计调用数，不发起Web/模型请求：

```bash
python -m scripts.e2e.run_route_feasibility_smoke <project_id> --days 1 --max-targets 3
```

确认成本后显式执行：

```bash
python -m scripts.e2e.run_route_feasibility_smoke <project_id> --days 1 --max-targets 3 --execute
```

`--execute`只检查预览中选中的目标，返回本次smoke检查结果，不覆盖生产`route_feasibility.json`；Web搜索审计日志仍正常追加。可用重复`--target <POI名>`进一步限定目标。

## 13. 完成定义

V1 只有同时满足以下条件才算完成：

- 正式 POI 不依赖 notes 即可进入系统级检查。
- 道路检查继续通过既有回归和真实青甘案例。
- `passed/warning/blocked/unknown` 均有持久化、测试和编排行为。
- 技术失败不再返回空 issues 冒充通过。
- notes 已完整但结构不可执行的问题能触发 proposal。
- 旅行日期有明确精度和无日期降级口径。
- 用户确认前正式路线不会因 blocked 自动变化。
- proposal 应用后重新核验受影响天。
- 旧项目无需迁移即可读取和继续运行。
- 核心设计文档已按真实实现同步，`docs/ISSUES.md` 记录真实验证结果和剩余局限。

## 14. 默认实施决策

为避免编码阶段反复悬而未决，本文先固定以下默认值：

1. 第一阶段不立即重命名 LangGraph 节点，只升级内部能力。
2. 新增独立 `route_feasibility.json`，不继续扩张 `citation_status` 语义。
3. 所有 RoutePlan POI 都检查；`needs_detail` 影响类别和优先级，不决定是否检查。
4. `unknown` 重试后允许显式降级继续，但绝不记为 passed。
5. blocked 必须走现有 proposal 确认，不自动改路线。
6. V1 不增加结构化 `route_legs/schedule`；相关检查无法可靠完成时返回 unknown。
7. 票价、天气、优惠和临时活动第一期不作为 blocked 条件。
8. 先提交正确性热修，再提交 Feasibility V1，避免一个巨大且难回归的改动。
9. 结构化核验使用`full -> mimo-v2.5`专用链；先轮完full组Key，只对可恢复渠道错误跨到MiMo，不对确定性请求错误兜底。

后续实施若要改变以上任一默认值，应先更新本文对应章节，再开始改代码。
