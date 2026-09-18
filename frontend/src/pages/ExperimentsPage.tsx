import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { useComparisonPairs, useExperiment, useExperiments, useReindexExperiments } from '../api/hooks'
import type { ComparisonPair, ComparisonPairFindingSnapshot, ComparisonPairStageObservation, ExperimentDetail, ExperimentEvidenceValue, ExperimentMetrics, ExperimentSummary } from '../api/types'
import { Badge } from '../components/Badge'
import { Drawer } from '../components/Drawer'
import { Icon } from '../components/Icon'
import { Tabs } from '../components/Tabs'
import { Tooltip } from '../components/Tooltip'

type ViewKey = 'pairs' | 'compare' | 'archive'

const harnessLabel = { langgraph: 'LangGraph', codex_skill: 'Codex Skill' }
const metricLabels: Record<string, string> = {
  route_day_count: '路线天数', must_visit_count: '路线 POI', needs_detail_target_count: '详情目标',
  lifestyle_item_count: 'Lifestyle 条目', pitfall_item_count: 'Pitfall 条目', integration_block_count: '正文块',
  poi_detail_count: 'POI 详情', poi_route_count: 'POI 内部路线', poi_segment_count: 'POI 路线段',
  poi_depth_missing_count: '详情缺失', poi_depth_empty_count: '空详情', poi_depth_outline_count: '只有路线框架',
  poi_depth_executable_count: '有可执行路线', poi_depth_recommended_count: '有推荐路线',
  final_guide_exists: '最终攻略', raw_claim_count: '原始 Claim', deliverable_claim_count: '可交付 Claim',
  withheld_claim_count: '暂缓 Claim', mapped_unique_claim_count: 'Integration Blocks 映射 Claim', poi_appendix_projected_claim_count: 'POI 附录投影 Claim', withheld_claim_leak_count: '暂缓 Claim 泄漏',
  force_finalized_count: 'force_finalized', final_contract_issue_count: '最终契约问题', passed_count: '路线通过',
  warning_count: '路线警告', blocked_count: '路线阻断', unknown_count: '路线未知', technical_error_count: '路线技术错误',
  coverage_counts: '路线措施覆盖', component_statuses: 'Checker 状态', unresolved_check_ids: '未完成 Check ID',
  entity_status_counts: '实体解析状态', transport_mode_counts: '交通方式',
  wall_clock_seconds: '墙钟时间', agent_task_count: 'Agent 任务', business_retry_count: '业务返修',
  transport_retry_count: '传输重试', recovery_count: '恢复事件', takeover_count: '接管', human_intervention_count: '人工介入',
  web_persisted_query_count: 'Web 持久化查询', web_attempt_count: 'Web 尝试', web_success_count: 'Web 成功',
  amap_persisted_record_count: '高德持久化记录', amap_attempt_count: '高德尝试', model_call_count: '模型调用',
  input_tokens: '输入 Token', output_tokens: '输出 Token', reasoning_tokens: '推理 Token', monetary_cost: '费用',
  compaction_count: '上下文压缩', context_budget_exceeded_count: '上下文超预算', history_message_count: '历史消息',
  failed_draft_preserved: '失败底稿保留',
}

const depthLabel: Record<string, string> = {
  missing: '未生成',
  empty: '无内部路线',
  route_outline: '只有路线框架',
  executable_route: '有可执行路线',
  recommended_route: '有推荐路线',
}

const comparabilityGradeDescriptions: Record<ComparisonPair['effective_grade'], string> = {
  A: '证据条件完整，可形成高可信对照。只有模型、Provider 等条件也一致且标记为 Harness 隔离对照时，才能将差异主要归因于 Harness。',
  B: '核心输入和路线可比，但部分条件不同或证据不足。可比较系统整体结果，不能把差异单独归因于 Harness。',
  C: '存在运行污染、成员不完整或对照契约不成立。结果只能作为观察线索，不能作为严格 A/B 结论。',
}

const comparabilityReasonLabels: Record<string, string> = {
  unknown_comparison_scope: '对照范围未识别',
  unknown_declared_grade: '声明的等级无效',
  missing_pair_member: '对照成员缺失',
  same_project_on_both_sides: '对照两侧指向同一项目',
  harness_side_mismatch: 'Harness 侧别登记不匹配',
  fixed_input_mismatch: '需求或确认路线不一致',
  member_not_complete: '至少一侧尚未形成完整可交付结果',
  harness_isolation_contract_broken: '模型或 Provider 不一致，不满足 Harness 隔离条件',
  material_baseline_unknown: '素材基线指纹未知，从 A 级降为 B 级',
  system_comparison_not_harness_isolation: '模型或 Provider 不同，只支持系统级对照',
}

function ComparabilityHeader() {
  return <Tooltip
    label="了解可比性等级"
    content={<><span className="block font-semibold text-ink">这是对照证据强度，不是系统可用率。</span><span className="mt-1 block">A 级：条件完整。B 级：核心输入可比，但有条件差异或证据缺口。C 级：仅供观察或尚待补齐。</span></>}
  >
    <span className="inline-flex items-center gap-1 underline decoration-dotted underline-offset-4">可比性等级 <span aria-hidden="true" className="text-[10px] text-ink-3">ⓘ</span></span>
  </Tooltip>
}

function ComparabilityGradeBadge({ pair }: { pair: ComparisonPair }) {
  const reasons = pair.comparability.reason_codes.map((reason) => comparabilityReasonLabels[reason] || reason)
  return <Tooltip
    label={`了解 ${pair.effective_grade} 级可比性`}
    content={<><span className="block font-semibold text-ink">{pair.effective_grade} 级 · {pair.comparison_scope === 'harness_isolated' ? 'Harness 隔离对照' : '系统级对照'}</span><span className="mt-1 block">{comparabilityGradeDescriptions[pair.effective_grade]}</span>{reasons.length > 0 && <span className="mt-2 block border-t border-border pt-2"><span className="font-semibold text-ink">本组判定：</span>{reasons.join('；')}。</span>}{pair.note && <span className="mt-1 block text-ink-3">登记说明：{pair.note}</span>}</>}
  >
    <Badge variant={pair.status === 'complete' ? 'ok' : 'warn'}>{pair.effective_grade} 级</Badge>
  </Tooltip>
}

function statusVariant(status: string) {
  if (['完成', 'completed', 'passed', 'ok'].includes(status)) return 'ok' as const
  if (status.includes('fail') || status.includes('error') || status === 'blocked') return 'danger' as const
  if (status.includes('warning') || status.includes('degrad') || status === 'force_finalized') return 'warn' as const
  return 'pending' as const
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return '未知'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.round((seconds % 3600) / 60)
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分`
}

function shortHash(value: string | null | undefined) {
  return value ? `${value.slice(0, 8)}…${value.slice(-5)}` : '未知'
}

function ValueCell({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-ink-3">未知</span>
  if (typeof value === 'boolean') return <span>{value ? '是' : '否'}</span>
  if (typeof value === 'number') return <span className={value === 0 ? 'font-mono font-semibold text-ok' : 'font-mono tabular-nums'}>{value}</span>
  if (Array.isArray(value)) return <span className="break-all font-mono text-[11px]">{value.length ? value.join(', ') : '无'}</span>
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    return <span className="block space-y-1 font-mono text-[11px]">{entries.length ? entries.map(([key, item]) => <span className="block break-all" key={key}>{key}: {typeof item === 'object' ? JSON.stringify(item) : String(item)}</span>) : '无'}</span>
  }
  return <span>{String(value)}</span>
}

function EvidenceValueCell({ metric }: { metric: ExperimentEvidenceValue | undefined }) {
  if (!metric || metric.value_kind === 'unknown') {
    return <span className="group relative inline-flex cursor-help text-ink-3 underline decoration-dotted underline-offset-4">未知<span className="pointer-events-none absolute right-0 top-full z-20 mt-2 hidden w-64 border border-border bg-surface p-2.5 text-left text-[11px] leading-relaxed text-ink-2 shadow-lg group-hover:block">{metric?.missing_reason || '当前没有可靠记录'}</span></span>
  }
  if (typeof metric.value === 'number' && metric.value === 0) return <span className="font-mono font-semibold text-ok">0</span>
  return <ValueCell value={metric.value} />
}

function ComparabilityContract({ left, right }: { left: ExperimentDetail; right: ExperimentDetail }) {
  const a = left.metrics.identity
  const b = right.metrics.identity
  const checks = [
    ['requirements', a.requirements_sha && b.requirements_sha ? a.requirements_sha === b.requirements_sha : null, shortHash(a.requirements_sha), shortHash(b.requirements_sha)],
    ['route SHA', a.route_sha && b.route_sha ? a.route_sha === b.route_sha : null, shortHash(a.route_sha), shortHash(b.route_sha)],
    ['模型', a.model && b.model ? a.model === b.model : null, a.model || '未知', b.model || '未知'],
    ['Provider', a.provider && b.provider ? a.provider === b.provider : null, a.provider || '未知', b.provider || '未知'],
    ['下游继承', a.inherited_downstream_outputs !== null && b.inherited_downstream_outputs !== null ? a.inherited_downstream_outputs === b.inherited_downstream_outputs : null, a.inherited_downstream_outputs ? '是' : '否', b.inherited_downstream_outputs ? '是' : '否'],
  ] as const
  const systemComparable = checks[0][1] === true && checks[1][1] === true
  const isolatedHarness = systemComparable && checks[2][1] === true && checks[3][1] === true
  return <section className="border-y border-border bg-surface">
    <div className="grid gap-0 min-[760px]:grid-cols-[180px_1fr_1fr]">
      <div className="border-b border-border bg-bg-inset px-4 py-3 text-xs font-semibold min-[760px]:border-b-0 min-[760px]:border-r">可比性契约</div>
      <div className="border-b border-border px-4 py-3 text-xs font-semibold min-[760px]:border-b-0 min-[760px]:border-r">{harnessLabel[a.harness]}<div className="mt-1 truncate font-mono text-[10px] font-normal text-ink-3">{a.project_id}</div></div>
      <div className="px-4 py-3 text-xs font-semibold">{harnessLabel[b.harness]}<div className="mt-1 truncate font-mono text-[10px] font-normal text-ink-3">{b.project_id}</div></div>
    </div>
    {checks.map(([label, match, leftValue, rightValue]) => <div key={label} className="grid min-[760px]:grid-cols-[180px_1fr_1fr] border-t border-border text-xs"><div className="flex items-center gap-2 bg-bg-inset/60 px-4 py-2.5 font-semibold"><span className={`h-2 w-2 rounded-full ${match === true ? 'bg-ok' : match === false ? 'bg-warn' : 'bg-pending'}`} />{label}</div><div className="break-all px-4 py-2.5 font-mono min-[760px]:border-l min-[760px]:border-border">{leftValue}</div><div className="break-all px-4 py-2.5 font-mono min-[760px]:border-l min-[760px]:border-border">{rightValue}</div></div>)}
    <div className={`border-t px-4 py-3 text-xs leading-relaxed ${systemComparable ? 'border-ok/25 bg-ok-soft text-ok' : 'border-warn/25 bg-warn-soft text-ink-2'}`}>{isolatedHarness ? '输入、路线、模型和 Provider 一致，可做更强的 Harness 隔离对照。' : systemComparable ? '同案例、同路线，可做系统级结果对照；模型、Provider 或工具条件不同，不能把差异单独归因于 Harness。' : '输入或路线不一致，只适合观察事实和版本演进，不构成直接 A/B。'}</div>
  </section>
}

function StageRail({ left, right }: { left: ExperimentMetrics; right: ExperimentMetrics }) {
  return <div className="overflow-x-auto scroll-boundary-x border-y border-border bg-surface"><div className="min-w-[760px] grid grid-cols-[1fr_90px_1fr]">
    {left.stages.map((stage, index) => { const peer = right.stages[index]; return <div className="contents" key={stage.key}><div className="flex items-center justify-between border-b border-border px-4 py-3"><span className="text-xs font-semibold">{stage.label}</span><Badge variant={statusVariant(stage.status)}>{stage.status}</Badge></div><div className="relative flex items-center justify-center border-x border-b border-border bg-bg-inset"><span className="absolute h-px w-full bg-border-strong" /><span className="relative h-3 w-3 rounded-full border-2 border-surface bg-brand shadow-sm" /></div><div className="flex items-center justify-between border-b border-border px-4 py-3"><Badge variant={statusVariant(peer?.status || 'unknown')}>{peer?.status || 'unknown'}</Badge><span className="text-xs font-semibold">{peer?.label || stage.label}</span></div></div> })}
  </div></div>
}

function MetricMatrix({ left, right }: { left: ExperimentMetrics; right: ExperimentMetrics }) {
  const groups: Array<[string, Record<string, unknown>, Record<string, unknown>, boolean]> = [
    ['产物', left.outputs, right.outputs, false], ['交付契约', left.quality_contract, right.quality_contract, false], ['路线闸门', left.route_gate, right.route_gate, false],
    ['执行', left.execution, right.execution, true], ['工具与模型', left.tool_usage, right.tool_usage, true], ['上下文', left.context, right.context, true],
  ]
  const rows = groups.flatMap(([group, a, b, wrapped]) => {
    const keys = Array.from(new Set([...Object.keys(a), ...Object.keys(b)]))
      .filter((key) => !['site_exists', 'poster_exists', 'started_at', 'completed_at', 'degradation_reasons'].includes(key))
    return [
      <tr key={`${group}-header`}>
        <td colSpan={3} className="border-b border-border bg-bg-inset/55 px-4 py-2 text-[11px] font-bold text-ink-3">{group}</td>
      </tr>,
      ...keys.map((key) => (
        <tr key={`${group}-${key}`}>
          <td className="border-b border-border px-4 py-2.5 font-medium">{metricLabels[key] || key}</td>
          <td className="border-b border-l border-border px-4 py-2.5">
            {wrapped ? <EvidenceValueCell metric={a[key] as ExperimentEvidenceValue | undefined} /> : <ValueCell value={a[key]} />}
          </td>
          <td className="border-b border-l border-border px-4 py-2.5">
            {wrapped ? <EvidenceValueCell metric={b[key] as ExperimentEvidenceValue | undefined} /> : <ValueCell value={b[key]} />}
          </td>
        </tr>
      )),
    ]
  })
  return <div className="overflow-x-auto scroll-boundary-x border-y border-border bg-surface"><table className="w-full min-w-[760px] border-collapse text-xs"><thead><tr className="bg-bg-inset text-left"><th className="w-48 border-b border-border px-4 py-2.5">统一指标</th><th className="border-b border-l border-border px-4 py-2.5">{harnessLabel[left.identity.harness]}</th><th className="border-b border-l border-border px-4 py-2.5">{harnessLabel[right.identity.harness]}</th></tr></thead><tbody>{rows}</tbody></table></div>
}

function Overview({ experiments, pairCount, onOpen }: { experiments: ExperimentSummary[]; pairCount: number; onOpen: (id: string) => void }) {
  const [destination, setDestination] = useState('all')
  const [harness, setHarness] = useState('all')
  const filtered = experiments.filter((item) => (destination === 'all' || item.destination === destination) && (harness === 'all' || item.harness === harness))
  const destinations = Array.from(new Set(experiments.map((item) => item.destination).filter(Boolean))) as string[]
  const grouped = useMemo(() => { const map = new Map<string, ExperimentSummary[]>(); filtered.forEach((item) => { const key = item.destination || '未知目的地'; map.set(key, [...(map.get(key) || []), item]) }); return Array.from(map.entries()) }, [filtered])
  return <div className="space-y-5">
    <div className="flex flex-wrap items-end gap-3"><label className="text-[11px] font-semibold text-ink-3">目的地<select className="mt-1 block rounded-md border border-border bg-surface px-3 py-2 text-xs text-ink" value={destination} onChange={(e) => setDestination(e.target.value)}><option value="all">全部目的地</option>{destinations.map((item) => <option key={item}>{item}</option>)}</select></label><label className="text-[11px] font-semibold text-ink-3">Harness<select className="mt-1 block rounded-md border border-border bg-surface px-3 py-2 text-xs text-ink" value={harness} onChange={(e) => setHarness(e.target.value)}><option value="all">全部 Harness</option><option value="langgraph">LangGraph</option><option value="codex_skill">Codex Skill</option></select></label><div className="ml-auto text-xs text-ink-3">显示 {filtered.length} / {experiments.length} 个显式登记实验</div></div>
    <section className="border-y border-border bg-surface px-4 py-4"><div className="grid grid-cols-2 gap-4 sm:grid-cols-4">{[['正式 1:1 Pair', pairCount], ['已完成', experiments.filter((item) => item.project_status === '完成').length], ['有降级', experiments.filter((item) => item.completion_mode?.includes('degradation')).length], ['未知墙钟', experiments.filter((item) => item.wall_clock_seconds === null).length]].map(([label, value]) => <div key={String(label)}><div className="text-xl font-semibold tabular-nums">{value}</div><div className="text-[11px] text-ink-3">{label}</div></div>)}</div></section>
    <section><div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold">历史版本谱系</h2><span className="text-[11px] text-ink-3">连线只来自显式关系登记</span></div><div className="space-y-3">{grouped.map(([city, items]) => <div key={city} className="grid gap-2 border-y border-border bg-surface px-4 py-3 min-[760px]:grid-cols-[90px_1fr]"><div className="text-xs font-bold">{city}</div><div className="flex gap-2 overflow-x-auto pb-1 scroll-boundary-x">{items.slice().reverse().map((item, index) => <button type="button" onClick={() => onOpen(item.project_id)} key={item.project_id} className="relative min-w-48 border border-border bg-surface px-3 py-2.5 text-left hover:border-brand"><div className="flex items-center justify-between gap-2"><span className="text-[11px] font-semibold">{harnessLabel[item.harness]}</span><span className="text-[10px] text-ink-3">{item.comparison_grade}级</span></div><div className="mt-1 text-xs font-semibold">{item.experiment_version || item.project_id}</div><div className="mt-1 font-mono text-[10px] text-ink-3">{shortHash(item.route_sha)}</div>{index < items.length - 1 && <span className="absolute -right-2 top-1/2 z-10 h-px w-2 bg-border-strong" />}</button>)}</div></div>)}</div></section>
    <section><h2 className="mb-3 text-sm font-semibold">实验清单</h2><div className="overflow-x-auto border-y border-border bg-surface scroll-boundary-x"><table aria-label="实验清单" className="w-full min-w-[1380px] table-fixed border-collapse text-xs"><colgroup><col className="w-[500px]" /><col className="w-[110px]" /><col className="w-[230px]" /><col className="w-[140px]" /><col className="w-[310px]" /><col className="w-[90px]" /><col className="w-[100px]" /></colgroup><thead><tr className="bg-bg-inset text-left"><th className="px-4 py-2.5">案例 / 版本</th><th className="px-3 py-2.5">Harness</th><th className="px-3 py-2.5">模型</th><th className="px-3 py-2.5">路线 Hash</th><th className="px-3 py-2.5">状态</th><th className="px-3 py-2.5">墙钟</th><th className="px-4 py-2.5 text-right">入口</th></tr></thead><tbody>{filtered.map((item) => { const displayedStatus = item.overall_status || item.project_status || '未知'; return <tr key={item.project_id} className="border-t border-border"><td className="px-4 py-3 align-top"><div className="break-words font-semibold">{item.destination} · {item.experiment_version}</div><div className="mt-1 break-all font-mono text-[10px] text-ink-3">{item.project_id}</div></td><td className="px-3 py-3 align-top font-medium">{harnessLabel[item.harness]}</td><td className="break-words px-3 py-3 align-top leading-relaxed">{item.model || '未知'}</td><td className="whitespace-nowrap px-3 py-3 align-top font-mono">{shortHash(item.route_sha)}</td><td className="px-3 py-3 align-top"><Badge variant={statusVariant(displayedStatus)}>{displayedStatus}</Badge></td><td className="whitespace-nowrap px-3 py-3 align-top">{formatDuration(item.wall_clock_seconds)}</td><td className="px-4 py-3 align-top"><div className="flex flex-col items-end gap-0.5"><button type="button" onClick={() => onOpen(item.project_id)} className="text-brand-dark hover:underline">详情</button><Link to={`/projects/${item.project_id}`} className="text-brand-dark hover:underline">Project</Link></div></td></tr> })}</tbody></table></div></section>
  </div>
}

function Compare({ pairs, preferredPairId }: { pairs: ComparisonPair[]; preferredPairId?: string }) {
  const completePairs = pairs.filter((item) => item.status === 'complete')
  const defaultPair = completePairs.find((item) => item.pair_id === preferredPairId) ?? completePairs[0]
  const [pairId, setPairId] = useState(defaultPair?.pair_id || '')
  const selectedPair = completePairs.find((item) => item.pair_id === pairId) ?? defaultPair
  const leftId = selectedPair?.langgraph_project_id || ''
  const rightId = selectedPair?.codex_project_id || ''
  const left = useExperiment(leftId)
  const right = useExperiment(rightId)
  if (!selectedPair) return <section className="border-y border-warn/30 bg-warn-soft px-4 py-5"><h2 className="text-sm font-semibold">尚无正式 1:1 Pair</h2><p className="mt-1 text-xs leading-relaxed text-ink-3">没有成对登记的 LangGraph 与 Codex 实验，因此不能进入对照或生成反哺候选。</p></section>
  return <div className="space-y-5"><section className="border-y border-border bg-surface px-4 py-3"><div className="flex flex-wrap items-end gap-3"><label className="min-w-[280px] flex-1"><span className="mb-1 block text-[11px] font-semibold text-ink-3">正式 1:1 Pair</span><select aria-label="正式 1:1 Pair" className="w-full rounded-md border border-border bg-surface px-3 py-2 text-xs" value={selectedPair.pair_id} onChange={(event) => setPairId(event.target.value)}>{completePairs.map((pair) => <option key={pair.pair_id} value={pair.pair_id}>{pair.effective_grade}级 · {pair.pair_id}</option>)}</select></label><div className="flex items-center gap-2 pb-2"><Badge variant="ok">完整 Pair</Badge><span className="text-xs text-ink-3">{selectedPair.comparison_scope === 'harness_isolated' ? 'Harness 隔离对照' : '系统级对照'}</span></div></div><p className="mt-2 text-[11px] leading-relaxed text-ink-3">一个 Pair 固定绑定一条 LangGraph 实验和一条 Codex 实验。页面不能单独更换任一侧；对照结论与反哺候选都以这个 Pair 为来源边界。</p></section><section aria-label="Pair 绑定成员" className="grid border-y border-border bg-surface min-[700px]:grid-cols-2"><div className="border-b border-border px-4 py-3 min-[700px]:border-b-0 min-[700px]:border-r"><div className="text-[10px] font-bold text-ink-3">LANGGRAPH · 已绑定</div><div className="mt-1 break-all text-xs font-semibold">{selectedPair.langgraph_experiment?.experiment_version || selectedPair.langgraph_project_id}</div><div className="mt-1 break-all font-mono text-[10px] text-ink-3">{selectedPair.langgraph_project_id}</div></div><div className="px-4 py-3"><div className="text-[10px] font-bold text-ink-3">CODEX SKILL · 已绑定</div><div className="mt-1 break-all text-xs font-semibold">{selectedPair.codex_experiment?.experiment_version || selectedPair.codex_project_id}</div><div className="mt-1 break-all font-mono text-[10px] text-ink-3">{selectedPair.codex_project_id}</div></div></section>{left.isLoading || right.isLoading ? <p className="text-sm text-ink-3">正在读取 Pair 的两个指标快照…</p> : left.data && right.data ? <><ComparabilityContract left={left.data} right={right.data} /><PairIssuePanel pair={selectedPair} /><div><h2 className="mb-3 text-sm font-semibold">共享阶段双轨</h2><StageRail left={left.data.metrics} right={right.data.metrics} /></div><div><div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold">统一指标矩阵</h2><span className="text-[11px] text-ink-3">绿色 0 = 已确认未发生；未知 = 当前无法可靠统计</span></div><MetricMatrix left={left.data.metrics} right={right.data.metrics} /></div></> : <p className="text-danger">无法读取这个 Pair 的对照实验。</p>}</div>
}

const findingTypeLabels: Record<string, string> = {
  force_finalized_residue: '强制收口残留',
  manifest_mismatch: '清单不一致',
  contract_violation: '契约异常',
}

function findingVariant(severity: string) {
  return severity === 'high' ? 'danger' as const : severity === 'medium' ? 'warn' as const : 'pending' as const
}

function FindingLane({ label, snapshot, align = 'left' }: { label: string; snapshot: ComparisonPairFindingSnapshot; align?: 'left' | 'right' }) {
  const hiddenCount = snapshot.active_finding_count - snapshot.preview_findings.length
  return <div className={`min-w-0 p-4 ${align === 'right' ? 'min-[780px]:text-right' : ''}`}><div className={`flex items-center gap-2 ${align === 'right' ? 'min-[780px]:justify-end' : ''}`}><span className="text-[10px] font-bold text-ink-3">{label}</span><span className={`font-mono text-lg font-semibold tabular-nums ${snapshot.active_finding_count ? 'text-warn' : 'text-ok'}`}>{snapshot.active_finding_count}</span><span className="text-[10px] text-ink-3">条开放 Finding</span></div>{snapshot.preview_findings.length ? <div className="mt-2 space-y-2">{snapshot.preview_findings.map((finding) => <div key={finding.finding_id} className={`border-l-2 border-warn/45 bg-warn-soft/45 px-2.5 py-2 text-left ${align === 'right' ? 'min-[780px]:border-l-0 min-[780px]:border-r-2' : ''}`}><div className="flex flex-wrap items-center gap-1.5"><Badge variant={findingVariant(finding.severity)}>{finding.severity}</Badge><span className="text-[10px] font-semibold text-ink-3">{findingTypeLabels[finding.problem_type] || finding.problem_type}</span></div><p className="mt-1 text-[11px] leading-relaxed text-ink-2">{finding.summary}</p></div>)}{hiddenCount > 0 && <p className="text-[10px] text-ink-3">另有 {hiddenCount} 条，请到质量中心查看。</p>}</div> : <p className="mt-2 text-[11px] text-ink-3">未登记开放 Finding；这不等于已经证明没有问题。</p>}<Link to={`/projects/${snapshot.project_id}/review`} className="mt-3 inline-flex items-center gap-1 text-[10px] font-semibold text-brand-dark">查看这一侧的问题 <Icon name="externalLink" className="h-3 w-3" /></Link></div>
}

function StageObservationLane({ observations }: { observations: ComparisonPairStageObservation[] }) {
  return <div className="feedback-kernel min-w-0 p-4"><div className="flex items-center justify-between gap-2"><span className="text-[10px] font-bold text-teal">阶段差异</span><span className={`font-mono text-lg font-semibold tabular-nums ${observations.length ? 'text-warn' : 'text-ok'}`}>{observations.length}</span></div>{observations.length ? <div className="mt-2 space-y-2">{observations.slice(0, 4).map((item) => <div key={item.key} className="border-y border-border/80 bg-surface/70 px-2.5 py-2"><p className="text-[11px] font-semibold">{item.label}</p><div className="mt-1 grid grid-cols-[1fr_auto_1fr] items-center gap-1 text-[10px]"><Badge variant={statusVariant(item.langgraph_status)}>{item.langgraph_status}</Badge><span className="text-ink-3">→</span><span className="text-right"><Badge variant={statusVariant(item.codex_status)}>{item.codex_status}</Badge></span></div></div>)}</div> : <p className="mt-2 text-[11px] leading-relaxed text-ink-3">共享阶段没有登记到状态差异或异常收口。</p>}</div>
}

function PairIssuePanel({ pair }: { pair: ComparisonPair }) {
  const issues = pair.observed_issues
  return <section className="border-y border-warn/30 bg-surface"><div className="flex flex-wrap items-center gap-2 border-b border-warn/25 bg-warn-soft px-4 py-2.5"><span className="text-xs font-semibold">观察到的问题</span><Badge variant={issues.active_finding_count ? 'warn' : 'ok'}>{issues.active_finding_count} 条待复核</Badge><span className="text-[10px] leading-relaxed text-ink-3">Finding 和阶段差异是观察信号，不等于已确认系统根因。</span></div><div className="grid min-[780px]:grid-cols-[1fr_minmax(240px,0.9fr)_1fr]"><FindingLane label="LANGGRAPH" snapshot={issues.langgraph} /><StageObservationLane observations={issues.stage_observations} /><FindingLane label="CODEX SKILL" snapshot={issues.codex} align="right" /></div>{pair.comparison_scope !== 'harness_isolated' && <p className="border-t border-warn/25 px-4 py-2 text-[10px] leading-relaxed text-ink-3">当前为系统级对照：可以描述哪一侧问题更多，但不能把差异单独归因于 Harness。</p>}</section>
}

function PairOverview({ pairs, onCompare }: { pairs: ComparisonPair[]; onCompare: (pairId: string) => void }) {
  const completePairs = pairs.filter((pair) => pair.status === 'complete')
  const incompletePairs = pairs.filter((pair) => pair.status !== 'complete')
  const linkedCandidateCount = new Set(pairs.flatMap((pair) => pair.source_candidates.map((candidate) => candidate.candidate_id))).size
  const activeFindingCount = pairs.reduce((total, pair) => total + pair.observed_issues.active_finding_count, 0)
  const problemTypes = pairs.reduce<Record<string, number>>((totals, pair) => {
    for (const snapshot of [pair.observed_issues.langgraph, pair.observed_issues.codex]) {
      for (const [type, count] of Object.entries(snapshot.problem_type_counts)) totals[type] = (totals[type] ?? 0) + count
    }
    return totals
  }, {})
  const pairTable = (items: ComparisonPair[], title: string, note: string, actionable = true) => <section><div className="mb-2 flex items-baseline justify-between gap-3"><h2 className="text-sm font-semibold">{title}</h2><span className="text-[11px] text-ink-3">{note}</span></div><div className="overflow-x-auto border-y border-border bg-surface"><table className="w-full min-w-[820px] border-collapse text-xs"><thead><tr className="bg-bg-inset text-left"><th className="px-4 py-2.5">案例</th><th>LangGraph</th><th>Codex Skill</th><th><ComparabilityHeader /></th><th>开放记录</th><th className="pr-4 text-right">操作</th></tr></thead><tbody>{items.map((pair) => <tr key={pair.pair_id} className="border-t border-border"><td className="px-4 py-3"><div className="font-semibold">{pair.pair_id}</div><div className="mt-1 text-[10px] text-ink-3">{pair.comparison_scope === 'harness_isolated' ? 'Harness 隔离对照' : '系统级对照'}</div></td><td className="max-w-52 break-all font-mono text-[10px]">{pair.langgraph_experiment?.experiment_version || pair.langgraph_project_id}</td><td className="max-w-52 break-all font-mono text-[10px]">{pair.codex_experiment?.experiment_version || pair.codex_project_id}</td><td><ComparabilityGradeBadge pair={pair} /></td><td><span className={pair.observed_issues.active_finding_count ? 'font-semibold text-warn' : 'text-ok'}>{pair.observed_issues.active_finding_count}</span><span className="ml-1 text-ink-3">条</span></td><td className="pr-4 text-right">{actionable ? <button type="button" onClick={() => onCompare(pair.pair_id)} className="font-semibold text-brand-dark hover:underline">查看对照</button> : <span className="text-[11px] text-ink-3">补齐后可用</span>}</td></tr>)}</tbody></table></div></section>
  return <div className="space-y-6"><section className="border-y border-border bg-surface px-4 py-4"><div className="grid grid-cols-2 gap-4 sm:grid-cols-4"><div><div className="text-xl font-semibold tabular-nums">{completePairs.length}</div><div className="text-[11px] text-ink-3">可用 Pair</div></div><div><div className="text-xl font-semibold tabular-nums text-warn">{activeFindingCount}</div><div className="text-[11px] text-ink-3">两侧开放记录</div></div><div><div className="text-xl font-semibold tabular-nums">{linkedCandidateCount}</div><div className="text-[11px] text-ink-3">Pair 来源改进候选</div></div><div><div className="text-xl font-semibold tabular-nums">{incompletePairs.length}</div><div className="text-[11px] text-ink-3">待补齐 Pair</div></div></div>{Object.keys(problemTypes).length > 0 && <p className="mt-3 text-[11px] text-ink-3">开放记录构成：{Object.entries(problemTypes).map(([type, count]) => `${findingTypeLabels[type] || type} ${count}`).join('，')}。这些是两侧项目已有记录，不是对照自动确认的根因。</p>}{linkedCandidateCount === 0 && <div className="mt-3 border-l-2 border-warn bg-warn-soft/60 px-3 py-2 text-[11px] text-ink-2">当前没有来自 Pair 的改进候选。请先到“问题与改进”完成人工确认和作用域判定。</div>}</section>{completePairs.length > 0 && pairTable(completePairs, '正式 Pair', '可进入对照详情')}{incompletePairs.length > 0 && pairTable(incompletePairs, '待补齐', '不进入正式结论统计', false)}<div className="text-right"><Link to="/review-center" className="inline-flex items-center gap-1 text-xs font-semibold text-brand-dark">前往问题与改进 <Icon name="externalLink" className="h-3.5 w-3.5" /></Link></div></div>
}

function DetailDrawer({ projectId, onClose }: { projectId: string | null; onClose: () => void }) {
  const detail = useExperiment(projectId || '')
  const metrics = detail.data?.metrics
  const statusRows = metrics?.status ? [
    ['总体', metrics.status.overall],
    ['交付', metrics.status.delivery],
    ['路线风险', metrics.status.route_risk],
    ['POI 完整性', metrics.status.poi_completeness || '未知'],
    ['恢复', metrics.status.recovery],
    ['协议', metrics.status.protocol],
  ] : []
  const poiDetails = metrics?.poi_details ?? []
  return <Drawer open={!!projectId} onClose={onClose} title={metrics?.identity.experiment_version || projectId || ''} subtitle={projectId || ''} widthClassName="sm:max-w-[720px]"><div className="space-y-5 p-5">{detail.isLoading ? <p className="text-sm text-ink-3">正在读取实验详情…</p> : metrics ? <><div className="grid grid-cols-2 gap-x-5 gap-y-3 text-xs sm:grid-cols-3">{[['Harness', harnessLabel[metrics.identity.harness]], ['目的地', metrics.identity.destination], ['状态', metrics.identity.project_status], ['模型', metrics.identity.model], ['Provider', metrics.identity.provider], ['路线 Hash', shortHash(metrics.identity.route_sha)]].map(([label, value]) => <div key={String(label)}><div className="text-[10px] font-semibold text-ink-3">{label}</div><div className="mt-1 break-all font-medium">{value || '未知'}</div></div>)}</div>{statusRows.length > 0 && <div><h3 className="mb-2 text-xs font-semibold">系统状态</h3><div className="flex flex-wrap gap-2">{statusRows.map(([label, value]) => <Badge key={label} variant={statusVariant(value)}>{label}: {value}</Badge>)}</div></div>}{poiDetails.length > 0 && <div><h3 className="mb-2 text-xs font-semibold">POI 内容深度</h3><div className="overflow-x-auto border-y border-border"><table className="w-full min-w-[620px] border-collapse text-[11px]"><thead><tr className="bg-bg-inset text-left"><th className="px-3 py-2">目标</th><th>引用状态</th><th>内容深度</th><th>路线</th><th>途经点</th><th>Claim</th></tr></thead><tbody>{poiDetails.map((item) => <tr key={`${item.day}-${item.poi_name}`} className="border-t border-border"><td className="px-3 py-2 font-semibold">Day {item.day} · {item.poi_name}</td><td><Badge variant={statusVariant(item.citation_status)}>{item.citation_status}</Badge></td><td>{depthLabel[item.content_depth] || item.content_depth}</td><td className="font-mono">{item.route_count} / 推荐 {item.recommended_route_count}</td><td className="font-mono">{item.segment_count}</td><td className="font-mono">{item.claim_count}</td></tr>)}</tbody></table></div><p className="mt-2 text-[11px] text-ink-3">引用通过只表示当前内容符合证据契约，不等于已生成完整内部路线。</p></div>}<div><h3 className="mb-2 text-xs font-semibold">原始证据</h3><div className="divide-y divide-border border-y border-border">{metrics.evidence_paths.map((path) => <a key={path} href={api.experimentEvidenceUrl(metrics.identity.project_id, path)} target="_blank" rel="noreferrer" className="flex items-center justify-between gap-3 px-3 py-2 text-[11px] hover:bg-bg-inset"><span className="break-all font-mono">{path}</span><Icon name="externalLink" className="h-3.5 w-3.5 shrink-0 text-brand-dark" /></a>)}</div></div><div><h3 className="mb-2 text-xs font-semibold">仍无法可靠提取</h3><div className="divide-y divide-border border-y border-border">{metrics.missing_metrics.map((item) => <div key={`${item.group}-${item.field}`} className="px-3 py-2 text-[11px]"><span className="font-semibold">{metricLabels[item.field] || item.field}</span><p className="mt-0.5 text-ink-3">{item.reason}</p></div>)}</div></div><div className="flex flex-wrap gap-2"><Link to={`/projects/${metrics.identity.project_id}`} className="inline-flex items-center gap-1 rounded-md bg-brand px-3 py-2 text-xs font-semibold text-white">打开 Project <Icon name="externalLink" className="h-3.5 w-3.5" /></Link><Link to={`/projects/${metrics.identity.project_id}/review`} className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-2 text-xs font-semibold">打开质量中心</Link></div></> : <p className="text-danger">实验详情加载失败。</p>}</div></Drawer>
}

export function ExperimentsPage() {
  const experimentsQuery = useExperiments()
  const pairsQuery = useComparisonPairs()
  const reindex = useReindexExperiments()
  const [view, setView] = useState<ViewKey>('pairs')
  const [selectedPairId, setSelectedPairId] = useState<string | undefined>()
  const [detailId, setDetailId] = useState<string | null>(null)
  const experiments = experimentsQuery.data ?? []
  const pairs = pairsQuery.data ?? []
  return <div><header className="mb-5 flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-xl font-bold">对照实验</h1><p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-3">先选择正式 Pair，再查看两套系统在同一案例上的差异；问题处理统一进入“问题与改进”。</p></div><details className="relative text-xs"><summary className="cursor-pointer rounded-md border border-border bg-surface px-3 py-2 font-semibold">索引维护</summary><div className="absolute right-0 z-20 mt-1 w-52 border border-border bg-surface p-2 shadow-lg"><p className="mb-2 text-[10px] leading-relaxed text-ink-3">仅在项目实验登记变化后使用。</p><button type="button" title="从 Project 快照重建实验索引" disabled={reindex.isPending} onClick={() => reindex.mutate()} className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-bg-inset px-3 py-2 text-xs font-semibold disabled:opacity-50"><Icon name="refresh" className={`h-4 w-4 ${reindex.isPending ? 'animate-spin' : ''}`} />{reindex.isPending ? '重建中' : '重建索引'}</button></div></details></header>{reindex.data && <div className="mb-4 border-y border-ok/25 bg-ok-soft px-4 py-2.5 text-xs text-ok">索引已重建：新增 {reindex.data.added}，更新 {reindex.data.updated}，跳过 {reindex.data.skipped}，失败 {reindex.data.failed}。{reindex.data.pairs ? ` Pair：完成 ${reindex.data.pairs.complete}，待补齐 ${reindex.data.pairs.incomplete}，无效 ${reindex.data.pairs.invalid}。` : ''}</div>}<Tabs tabs={[{ key: 'pairs', label: 'Pair 总览', badge: pairs.filter((pair) => pair.status === 'complete').length }, { key: 'compare', label: '对照详情' }, { key: 'archive', label: '实验档案', badge: experiments.length }]} active={view} onChange={(key) => setView(key as ViewKey)} className="mb-5 overflow-x-auto scroll-boundary-x" />{experimentsQuery.isLoading || pairsQuery.isLoading ? <p className="text-sm text-ink-3">正在读取实验索引…</p> : experimentsQuery.error || pairsQuery.error ? <p className="text-danger">读取失败：{((experimentsQuery.error || pairsQuery.error) as Error).message}</p> : view === 'pairs' ? <PairOverview pairs={pairs} onCompare={(pairId) => { setSelectedPairId(pairId); setView('compare') }} /> : view === 'compare' ? <Compare key={selectedPairId || 'default'} pairs={pairs} preferredPairId={selectedPairId} /> : <Overview experiments={experiments} pairCount={pairs.filter((pair) => pair.status === 'complete').length} onOpen={setDetailId} />}<DetailDrawer projectId={detailId} onClose={() => setDetailId(null)} /></div>
}
