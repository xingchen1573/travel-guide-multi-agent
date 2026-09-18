# Phase 5 最小持续观察设计

> 状态：Phase 5.0-5.1最小版及Candidate证据门禁已实现（更新于2026-09-03）
> 范围：只观察确定性状态扫描器已经能识别的问题。
> 暂缓：通用 detector 平台、冻结生成重放、自动验证与策略编辑。

## 1. 为什么收缩

此前设计同时引入了：

- 通用 `DetectionContract`；
- 多种 detector 类型；
- 多种 exposure unit；
- 可编辑 Monitoring Policy；
- Observation supersession；
- 持久化派生 snapshot；
- 基线、迁移、成本与失败率门槛；
- Frozen Replay。

在本轮收缩设计启动前，这些扩展能力没有实际调用方，运行目录中也没有已创建的窗口或观察记录。继续保留会带来三个问题：

1. 页面展示了大量不能指导行动的抽象字段；
2. 同一状态既存事实又存快照，容易不一致；
3. 后续开发者难以判断哪些是现有能力，哪些只是预留接口。

因此本阶段只实现一条能端到端验证的链路：

```text
成功的定向回归
  -> MonitoringWindow
  -> 已完成项目发生新 revision
  -> 内置状态扫描器运行
  -> MonitoringObservation(clear / hit / failed)
  -> GET 时派生 collecting / ready / breached / stale
```

截至2026-09-03，上述最小链路的模型、聚合、Candidate门禁、只读API和聚焦测试已经实现；当前运行数据中仍未找到可用于真实运营验收的MonitoringWindow或MonitoringObservation记录。因此“已实现”只表示代码级能力成立，样本门槛、复发率和长期稳定性仍未由真实观察窗口证明。

## 2. 当前系统回答什么

它只回答一个问题：

> 某个已经修复并通过定向回归的工程问题，在后续完成项目中是否再次出现同类可确定识别的症状？

它不回答：

- 所有内容质量是否提高；
- 新命中是否一定和原候选具有同一个根因；
- 模型或 Skill 是否应该自动修改自身；
- 没有 checker 的候选是否已经被验证；
- 历史项目重跑后的生成质量是否等价。

`hit` 只表示“同类症状再次被 scanner 命中”，不是“根因已经自动确认”。

## 3. 唯一观察单位

观察单位固定为：

```text
project_id + project_revision
```

`project_revision` 由状态扫描器覆盖的业务事实文件确定性计算。相同 revision 重复扫描只产生一条相同 outcome 的观察记录。

只有同时满足以下条件的项目才适用：

- 项目状态为 `完成`；
- 当前 checker 所需的业务产物均存在；
- revision 生成时间不早于窗口开始时间；
- 窗口记录的代码、Prompt 和 checker 版本仍是当前版本。

不适用的项目直接跳过，不写 `not_applicable` 事件。

## 4. 内置 checker

当前只支持状态扫描器已有的三个问题类型：

| problem_type | checker_id | 必需产物 |
|---|---|---|
| `manifest_mismatch` | `state_scanner:manifest_mismatch` | POI detail manifest |
| `force_finalized_residue` | `state_scanner:force_finalized_residue` | 路线、生活、避坑、POI manifest、final meta |
| `final_guide_contract_violation` | `state_scanner:final_guide_contract_violation` | final guide meta |

checker 配置和状态扫描核心函数共同生成 `checker_version`。实现变化后，旧窗口变为 `stale`，不会继续混入新观察。

增加第四个 checker 时，应直接增加一条静态映射和对应测试。只有出现两个真正不同的扫描引擎后，才重新评估是否需要通用 detector 接口。

## 5. 两个持久化模型

### 5.1 MonitoringWindow

```text
window_id
candidate_id
source_regression_run_id
checker_id
checker_version
code_fingerprint
prompt_fingerprint
started_at
minimum_elapsed_days
minimum_samples
created_by
created_at
```

创建窗口前必须验证：

- Regression Run 属于该候选；
- Regression Run 状态为 `succeeded`；
- 至少包含一个 `unit_test`、`counterexample` 或 `fixed_eval`；
- 对应可执行Case结果实际通过；
- code 和 Prompt fingerprint 完整且对应当前发布版本。

当前不提供页面或公共 API 修改窗口。成功 Regression Run 在包含可执行证明、允许推进且候选处于 `implementing` 时，会为受支持的问题类型幂等创建窗口，并将候选推进到 `monitoring`。窗口启动失败不会覆盖回归成功事实，候选保留原状态，失败原因写入 Regression Run 摘要。

### 5.2 MonitoringObservation

```text
event_id
window_id
candidate_id
project_id
project_revision
outcome: clear | hit | failed
finding_ids
reason
observed_at
```

语义：

- `clear`：checker 成功运行，未命中同类症状；
- `hit`：checker 成功运行，命中同类症状；
- `failed`：scanner 对适用项目运行失败。

`failed` 不进入样本分母；最近一次事件为 `failed` 时，窗口不能进入 `ready`。

## 6. 派生状态

不持久化 snapshot。每次读取时从当前窗口和 Observation 计算：

| 状态 | 条件 |
|---|---|
| `not_started` | 候选没有窗口 |
| `stale` | code、Prompt 或 checker 版本已变化 |
| `breached` | 窗口内至少有一个 `hit` |
| `ready` | 时间和样本双门槛满足、无 hit、最近扫描未失败、候选仍在 monitoring/verified |
| `collecting` | 其余情况 |

默认门槛：

```text
elapsed_days >= 14
AND distinct project revisions >= 5
```

高严重度候选默认要求 10 个 revision。当前不提供快速通道或任意策略编辑。

### 6.1 Candidate进入verified

- 有内置Checker：必须存在Window、人工摘要非空，且实时派生状态严格为`ready`；
- 无内置Checker：不创建不存在的Window，必须有当前版本的成功可执行Regression Run和人工摘要；
- `not_started`、`collecting`、`stale`、`breached`均禁止有Checker候选进入`verified`；
- `hit`只让Summary显示`breached`，不会自动把Candidate退回`implementing`；
- Candidate已处于`monitoring`或`verified`时，同状态owner、notes或摘要更新不重复执行阶段进入门禁。

## 7. API

只保留两个只读接口：

```text
GET /api/review-index/optimization-candidates/{candidate_id}/monitoring
GET /api/review-index/optimization-candidates/{candidate_id}/monitoring-observations
```

已移除：

- Monitoring Policy PATCH；
- snapshot rebuild POST；
- 通用 Quality Observation API。

页面只需要展示派生状态、样本数、命中数、失败数、观察时长和未满足门槛。不要暴露内部配置表单。

## 8. 核心不变量

测试必须覆盖：

1. 无窗口时为 `not_started`；
2. 同 revision 重复扫描幂等；
3. `clear` 可在双门槛满足后进入 `ready`；
4. 任一 `hit` 立即进入 `breached`；
5. 不完整项目和缺少必需产物的项目不写事件；
6. `failed` 不进入样本数，并阻止 `ready`；
7. 窗口开始前的 revision 不计入；
8. release 或 checker 变化后窗口为 `stale` 且停止收集；
9. 有Checker时`collecting/stale/breached`不能verified，`ready`和非空摘要可以verified；
10. 无Checker时由当前成功可执行回归和人工摘要支持verified；
11. API 只有Monitoring读取能力，Candidate状态写入仍走共享领域门禁。

## 9. 暂不实现

以下能力有明确需求和真实数据后再设计：

- 自动把 `ready` 候选推进为 `verified`；
- 内容模型、Skill 或人工复核 detector；
- POI、route、claim 等其他 exposure unit；
- 统计基线、成本收益和跨 checker 统一评分；
- Frozen Generation Replay；
- 自动修改 Prompt、Skill 或代码的“自进化”执行器。

自检可以持续运行，但自进化必须继续保持“提出候选 -> 定向回归 -> 观察 -> 人工确认”的边界。

## 10. 重新扩展的门槛

只有满足以下任一条件时才扩展模型：

- 第二种 detector 已经有生产调用方，现有 Observation 无法表达；
- 第二种 exposure unit 已经有明确分母和去重规则；
- 页面确实需要修改窗口配置，且角色权限和审计要求已定义；
- 历史输入能够完整冻结，并已有一个 Frozen Replay 验收案例；
- 派生 summary 的读取成本被实测证明不可接受。

在这些条件出现前，优先增加具体 checker 和回归测试，不增加通用平台层。
