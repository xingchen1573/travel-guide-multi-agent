---
title: 公开实验证据摘要
type: canonical
status: current
owner: project-maintainers
created: 2026-09-17
last_reviewed: 2026-09-17
authoritative_scope: 对外作品快照中四项代表性实验指标的口径、证据等级和不可外推边界
source_of_truth: Private 冻结 Project、Workspace、Trace、审计记录与对应自动化回归；本文仅为脱敏公开投影
related_docs:
  - docs/CONTINUOUS_QUALITY_EVOLUTION_DESIGN.md
  - docs/PHASE5_MONITORING_AND_FROZEN_REPLAY_DESIGN.md
---

# 公开实验证据摘要

本文解释根 README 中四项代表性指标的样本、测量口径和限制。它是 Private 实验现场的脱敏公开投影，不替代原始 Trace、Project 产物或回归记录，也不把单次实验外推为生产指标。

## 1. 上下文投影

**问题。** Integration 若直接消费完整上游 JSON、审计历史和失败过程，输入会被与最终写作无关的信息占满。系统因此为角色建立输入投影，只交付当前角色需要的字段，同时把完整事实留在 Workspace 中供追溯。

**样本与结果。** 在离线序列化同一角色输入的回放中：

- 成都 3 日 LangGraph 历史样本由 `238,953` 字符降至 `38,993` 字符，下降 `83.7%`；
- 成都 3 日 Codex 历史样本由 `267,365` 字符降至 `40,851` 字符，下降 `84.7%`；
- 青甘 8 日样本投影后为 `26,390` 字符，但缺少可直接比较的投影前基线，因此不计算降幅。

对应 Context、Workspace 工具、核心节点、Pitfall 和最终正文块的聚焦回归为 `123 passed`，另有 1 条既有依赖弃用警告。

**可以证明。** 投影机制在两个成都历史样本上显著缩小了离线序列化输入体积，并能处理更长的青甘样本。

**不能外推。** 字符数不是实际 Prompt token、整条任务 token、费用、延迟或攻略质量；这些数字也不是所有 Agent、所有目的地的平均降幅。

公开快照中的对应实现与回归入口：`src/workspace/integration_context.py`、`tests/test_integration_context.py`。

## 2. 受监督恢复

**问题。** 长流程运行可能遇到 Provider 波动、人工误中断或进程重启。系统以 Project Checkpoint 和持久化 Workspace 保存阶段事实，使恢复从最近可证状态继续，而不是整条攻略重跑。

**样本与结果。** 青甘 8 日 LangGraph V7 在两次人工误中断及后续自然渠道失败后，从同一 Checkpoint 恢复；路线结构 Hash 保持不变，最终完成：

- 11 个正文块，映射 259 条上游 Claim；
- 13 个 POI 详情和最终 Join；
- 9 个 POI 为 `passed`，4 个为 `force_finalized`；
- 最终交付契约为 `passed`，结构问题数为 0；
- 相关编排与交付聚焦回归为 `102 passed`，另有 1 条既有依赖弃用警告。

**可以证明。** Checkpoint、Workspace 和恢复入口在这个真实长流程样本中保住了已完成阶段，并支持继续完成后续交付。

**不能外推。** 这是有人工观察且运行中包含窄范围修正的 C 级受监督实验，不证明无人值守稳定性；`force_finalized` 也不等于内容检查全部通过。历史成品还暴露过路线 `keep` 语义漂移，当前代码虽已修复，但尚无修复后的全新 8 日端到端样本。

公开快照中的对应实现入口：`src/orchestrator.py`、`src/workspace/`。

## 3. Claim 交付隔离

**问题。** 审计层需要保留失败、冲突和暂缓事实，但这些信息不能因为仍在 Workspace 中就自动进入用户成品。系统因此区分原始 Claim、允许交付的 Claim 与最终正文映射。

**样本与结果。** 青甘 8 日 LangGraph V6 共记录 772 条原始 Claim，其中 733 条可交付、39 条暂缓。最终正文映射的 Claim ID 与 39 条暂缓 Claim ID 的交集为 0，最终交付契约的结构问题数为 0。

**可以证明。** 该样本中，结构化标记为暂缓的 Claim ID 没有进入最终正文映射，审计事实与交付事实实现了可检查的隔离。

**不能外推。** `交集为 0` 不是全文语义级泄漏检测，也不证明剩余 733 条 Claim 全部准确；39 条暂缓包含不同原因，不能统一解释为模型误判或业务错误。

公开快照中的对应实现与回归入口：`src/experiments/metrics.py`、`src/workspace/integration_context.py`、`tests/test_experiment_metrics.py`、`tests/test_integration_context.py`。

## 4. 路线门禁定向复核

**问题。** 首版路线门禁在少量检查未解决时仍整批重跑所有 POI，重复消耗 Web 检索。后续将复核范围收窄为未解决的 `check_id`，只补查缺口。

**样本与结果。** 两次成都 3 日固定路线实验使用相同需求、确认路线和路线结构 Hash，首轮均包含 13 个 POI：

- V1 连续三轮整批查询，持久化 39 条 Route Gate Web 日志，即 13 个 POI 乘以 3 轮；
- V2 首轮查询 13 次，随后只对同一个未解决 `check_id` 局部复核两次，共 15 条日志；
- V2 最终为 37 个 `passed`、0 个 `warning`、1 个证据 `unknown`、0 个技术错误，路线结构未变化；
- 相关确定性聚焦回归为 `160 passed`，另有 1 条既有依赖弃用警告。

**可以证明。** 在这个同路线样本中，按未解决 ID 局部复核避免了其余 POI 的重复查询，并保留了每轮证据。

**不能外推。** 39 与 15 是持久化 Route Gate Web 日志，不是全部 Web 尝试、模型调用、费用或严格 Harness A/B；两次运行间还包含日期语义和模型配置等其他调整，因此不能把日志降幅直接写成总体成本或质量提升。

公开快照中的对应实现与回归入口：`src/materials/route_feasibility_check.py`、`src/codex_skill/bridge.py`、`tests/test_route_feasibility.py`、`tests/test_codex_skill_bridge.py`。
