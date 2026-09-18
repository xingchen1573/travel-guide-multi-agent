import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { useComparisonPairs, useGlobalOptimizationCandidates, useImprovementSignals, useQualityMetrics } from '../api/hooks'
import type { ImprovementSignal, OptimizationCandidate } from '../api/types'
import { Badge } from './Badge'
import { CandidateRegressionPanel } from './CandidateRegressionPanel'
import { Icon } from './Icon'
import { Tooltip } from './Tooltip'

const statusLabel: Record<OptimizationCandidate['status'], string> = {
  new: '待分诊',
  triaged: '已确认方向',
  planned: '已制定计划',
  implementing: '实施中',
  monitoring: '观察中',
  verified: '已验证',
  dismissed: '已驳回',
  deferred: '已暂缓',
}

const statusTransitions: Record<OptimizationCandidate['status'], OptimizationCandidate['status'][]> = {
  new: ['new', 'triaged', 'deferred', 'dismissed'],
  triaged: ['triaged', 'planned', 'deferred', 'dismissed'],
  planned: ['planned', 'implementing', 'deferred'],
  implementing: ['implementing', 'monitoring', 'deferred'],
  monitoring: ['monitoring', 'verified', 'implementing', 'deferred'],
  verified: ['verified', 'monitoring'],
  dismissed: ['dismissed', 'triaged'],
  deferred: ['deferred', 'triaged'],
}

const targetScopeLabel: Record<OptimizationCandidate['target_scope'], string> = {
  undecided: '尚未判定',
  shared_kernel: 'Shared Kernel',
  langgraph_harness: '仅 LangGraph Harness',
  codex_skill_harness: '仅 Codex Skill Harness',
  multiple: '多个作用域',
}

function statusVariant(status: OptimizationCandidate['status']) {
  if (status === 'verified') return 'ok' as const
  if (status === 'implementing' || status === 'monitoring') return 'brand' as const
  if (status === 'dismissed') return 'danger' as const
  if (status === 'deferred') return 'pending' as const
  return 'warn' as const
}

function severityVariant(severity: OptimizationCandidate['severity'] | ImprovementSignal['severity']) {
  if (severity === 'high') return 'danger' as const
  if (severity === 'medium') return 'warn' as const
  return 'pending' as const
}

const signalDecisionLabel: Record<ImprovementSignal['decision'], string> = {
  pending: '待确认',
  accepted: '已确认',
  dismissed: '已驳回',
  deferred: '已暂缓',
  merged: '已合并',
  reclassified: '已重分类',
}

function signalSourceLabel(sourceType: string) {
  if (sourceType === 'codex_skill') return 'Codex Skill'
  if (sourceType === 'external_skill') return '外部复核 Skill'
  return sourceType || '未知来源'
}

function formatTime(value: string) {
  if (!value) return '未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function QualityImprovementPanel({ projectId, currentPendingFindingCount = 0, onOpenReview }: { projectId: string; currentPendingFindingCount?: number; onOpenReview?: () => void }) {
  const queryClient = useQueryClient()
  const candidatesQuery = useGlobalOptimizationCandidates()
  const signalsQuery = useImprovementSignals(projectId)
  const metricsQuery = useQualityMetrics()
  const pairsQuery = useComparisonPairs()
  const candidates = candidatesQuery.data ?? []
  const pairObservationCount = (pairsQuery.data ?? []).reduce(
    (total, pair) => total + (pair.observed_issues?.active_finding_count ?? 0),
    0,
  )
  const completePairObservationCount = (pairsQuery.data ?? []).reduce(
    (total, pair) => total + (pair.status === 'complete' ? pair.observed_issues?.active_finding_count ?? 0 : 0),
    0,
  )
  const incompletePairObservationCount = pairObservationCount - completePairObservationCount
  const currentPair = (pairsQuery.data ?? []).find(
    (pair) => pair.codex_project_id === projectId || pair.langgraph_project_id === projectId,
  )
  const currentPairProjectCount = currentPair
    ? (currentPair.codex_project_id === projectId
      ? currentPair.observed_issues?.codex.active_finding_count
      : currentPair.observed_issues?.langgraph.active_finding_count) ?? 0
    : 0
  const currentPairPeerCount = currentPair
    ? (currentPair.codex_project_id === projectId
      ? currentPair.observed_issues?.langgraph.active_finding_count
      : currentPair.observed_issues?.codex.active_finding_count) ?? 0
    : 0
  const pendingSignals = useMemo(
    () => (signalsQuery.data ?? []).filter((item) => item.decision === 'pending'),
    [signalsQuery.data],
  )
  const handledSignals = useMemo(
    () => (signalsQuery.data ?? []).filter((item) => item.decision !== 'pending'),
    [signalsQuery.data],
  )
  const globalPendingSignalCount = metricsQuery.data?.pending_signal_count ?? pendingSignals.length
  const globalAcceptedSignalCount = metricsQuery.data?.accepted_signal_count ?? 0
  const candidateSignalIds = new Set(candidates.flatMap((candidate) => candidate.signal_ids ?? []))
  const [signalView, setSignalView] = useState<'pending' | 'handled'>('pending')
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null)
  const [busySignalId, setBusySignalId] = useState<string | null>(null)
  const [candidateBusy, setCandidateBusy] = useState(false)
  const [candidateError, setCandidateError] = useState<string | null>(null)
  const selectedCandidate = candidates.find((item) => item.candidate_id === selectedCandidateId) ?? candidates[0] ?? null
  const [candidateStatus, setCandidateStatus] = useState<OptimizationCandidate['status']>('new')
  const [candidateOwner, setCandidateOwner] = useState('')
  const [candidateNotes, setCandidateNotes] = useState('')
  const [verificationSummary, setVerificationSummary] = useState('')
  const [candidateTargetScope, setCandidateTargetScope] = useState<OptimizationCandidate['target_scope']>('undecided')
  const [candidateChangeRef, setCandidateChangeRef] = useState('')
  const [validationPairIds, setValidationPairIds] = useState('')

  useEffect(() => {
    if (!selectedCandidate) return
    setSelectedCandidateId(selectedCandidate.candidate_id)
    setCandidateStatus(selectedCandidate.status)
    setCandidateOwner(selectedCandidate.owner)
    setCandidateNotes(selectedCandidate.notes)
    setVerificationSummary(selectedCandidate.verification_summary)
    setCandidateTargetScope(selectedCandidate.target_scope)
    setCandidateChangeRef(selectedCandidate.change_ref)
    setValidationPairIds(selectedCandidate.validation_pair_ids.join(', '))
  }, [selectedCandidate])

  async function decideSignal(signal: ImprovementSignal, decision: 'accepted' | 'dismissed' | 'deferred') {
    setBusySignalId(signal.signal_id)
    try {
      const matchingPairs = (pairsQuery.data ?? []).filter(
        (pair) => pair.codex_project_id === signal.project_id || pair.langgraph_project_id === signal.project_id,
      )
      await api.decideImprovementSignal(projectId, signal.signal_id, {
        decision,
        comparison_pair_id: decision === 'accepted' && matchingPairs.length === 1 ? matchingPairs[0].pair_id : undefined,
      })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['improvement-signals', projectId] }),
        queryClient.invalidateQueries({ queryKey: ['global-optimization-candidates'] }),
        queryClient.invalidateQueries({ queryKey: ['quality-metrics'] }),
      ])
      setSignalView('handled')
    } finally {
      setBusySignalId(null)
    }
  }

  async function saveCandidateState() {
    if (!selectedCandidate) return
    setCandidateBusy(true)
    setCandidateError(null)
    try {
      await api.updateOptimizationCandidate(selectedCandidate.candidate_id, {
        status: candidateStatus,
        owner: candidateOwner,
        notes: candidateNotes,
        verification_summary: verificationSummary,
        target_scope: candidateTargetScope,
        change_ref: candidateChangeRef,
        validation_pair_ids: validationPairIds.split(',').map((item) => item.trim()).filter(Boolean),
      })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['global-optimization-candidates'] }),
        queryClient.invalidateQueries({ queryKey: ['quality-metrics'] }),
      ])
    } catch (error) {
      setCandidateError((error as Error).message)
    } finally {
      setCandidateBusy(false)
    }
  }

  return (
    <div className="border border-border rounded-lg bg-surface overflow-hidden">
      <div className="px-5 py-4 border-b border-border">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2"><h2 className="font-semibold text-sm">全局系统改进漏斗</h2><Badge variant="pending">全局</Badge></div>
            <p className="text-xs text-ink-3 mt-1">汇总全部 Pair 和项目；开放记录须经复核和人工确认后才可能进入系统候选。当前项目状态在下方单独展示。</p>
          </div>
          <div className="text-[11px] text-ink-3">最近重建 {formatTime(metricsQuery.data?.generated_at ?? '')}</div>
        </div>
        <div className="grid grid-cols-2 gap-x-5 gap-y-3 mt-4 min-[760px]:grid-cols-5">
          <div className="min-w-0">
            <div className="text-lg font-semibold tabular-nums text-ink-1">{pairObservationCount}</div>
            <Tooltip
              label="了解全部 Pair 开放记录"
              content={<><span className="block font-semibold text-ink">全部已登记 Pair 两侧尚未关闭的 Finding 合计。</span><span className="mt-1 block">完整 Pair {completePairObservationCount} 条；待补齐 Pair {incompletePairObservationCount} 条。这不是当前项目的数量。</span>{currentPair && <span className="mt-2 block border-t border-border pt-2">当前项目所在 Pair 共 {currentPairProjectCount + currentPairPeerCount} 条：本项目 {currentPairProjectCount} 条，对照侧 {currentPairPeerCount} 条。</span>}</>}
            >
              <span className="inline-flex items-center gap-1 text-[11px] text-ink-3 underline decoration-dotted underline-offset-4">全部 Pair 开放记录 <span aria-hidden="true" className="text-[10px]">ⓘ</span></span>
            </Tooltip>
          </div>
          {[
            ['全局待确认信号', globalPendingSignalCount],
            ['全局已确认信号', globalAcceptedSignalCount],
            ['全局改进候选', metricsQuery.data?.candidate_count ?? candidates.length],
            ['全局已验证', metricsQuery.data?.verified_candidate_count ?? 0],
          ].map(([label, value]) => (
            <div key={String(label)} className="min-w-0">
              <div className="text-lg font-semibold tabular-nums text-ink-1">{value}</div>
              <div className="text-[11px] text-ink-3">{label}</div>
            </div>
          ))}
        </div>
      </div>

      <section className="px-5 py-4 border-b border-border">
        <div className="flex items-center justify-between gap-3 mb-3">
          <div>
            <h3 className="text-sm font-semibold">当前项目系统信号</h3>
            <p className="text-[11px] text-ink-3 mt-0.5">复核 Skill 提出系统观察，人工确认后才参与全局聚合。</p>
          </div>
          <div className="grid grid-cols-2 gap-1 rounded-md bg-bg-inset p-1">
            <button type="button" onClick={() => setSignalView('pending')} className={`rounded px-2.5 py-1.5 text-xs font-semibold ${signalView === 'pending' ? 'bg-surface text-ink-1 shadow-sm' : 'text-ink-3'}`}>待确认 {pendingSignals.length}</button>
            <button type="button" onClick={() => setSignalView('handled')} className={`rounded px-2.5 py-1.5 text-xs font-semibold ${signalView === 'handled' ? 'bg-surface text-ink-1 shadow-sm' : 'text-ink-3'}`}>已处理 {handledSignals.length}</button>
          </div>
        </div>
        {signalsQuery.isLoading ? (
          <p className="text-xs text-ink-3">正在读取系统信号…</p>
        ) : (signalView === 'pending' ? pendingSignals : handledSignals).length === 0 ? (
          signalView === 'pending' && currentPendingFindingCount > 0 ? (
            <div className="border-l-2 border-warn bg-warn-soft/45 px-3 py-2.5">
              <p className="text-xs font-semibold text-ink-2">本项目有 {currentPendingFindingCount} 条待复核问题，但尚未形成待确认的系统信号。</p>
              <p className="mt-1 text-[11px] leading-relaxed text-ink-3">只有完成复核，且复核结果明确提出可跨项目复用的系统观察时，才会生成信号。</p>
              {onOpenReview && <button type="button" onClick={onOpenReview} className="mt-2 inline-flex items-center gap-1 text-xs font-semibold text-brand-dark hover:underline">前往待复核问题 <Icon name="arrowRight" className="h-3.5 w-3.5" /></button>}
            </div>
          ) : (
            <p className="text-xs text-ink-3 py-2">{signalView === 'pending' ? '这个项目目前没有待确认的系统信号。' : '这个项目目前没有已处理的系统信号。'}</p>
          )
        ) : (
          <div className="divide-y divide-border border-y border-border">
            {(signalView === 'pending' ? pendingSignals : handledSignals).map((signal) => (
              <div key={signal.signal_id} className="py-3.5 grid gap-3 min-[900px]:grid-cols-[1fr_auto] min-[900px]:items-start">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold text-ink-1">{signal.hypothesis}</span>
                    <Badge variant={severityVariant(signal.severity)}>{signal.severity}</Badge>
                    {signal.decision !== 'pending' && <Badge variant={signal.decision === 'accepted' ? 'ok' : signal.decision === 'dismissed' ? 'danger' : 'pending'}>{signalDecisionLabel[signal.decision]}</Badge>}
                  </div>
                  <div className="mt-1 text-[11px] font-mono text-ink-3 break-all">{signal.cause_key} · {signal.affected_layer}</div>
                  {signal.suggested_fix && <p className="mt-2 text-xs text-ink-2 leading-relaxed"><span className="font-semibold">建议修改：</span>{signal.suggested_fix}</p>}
                  {signal.suggested_test && <p className="mt-1 text-xs text-ink-2 leading-relaxed"><span className="font-semibold">建议回归：</span>{signal.suggested_test}</p>}
                  <div className="mt-2 text-[11px] text-ink-3">来源：{signalSourceLabel(signal.source_type)} · Finding：{signal.finding_id}</div>
                  {signal.comparison_pair_id && <div className="mt-1 break-all font-mono text-[11px] text-teal">来源 Pair：{signal.comparison_pair_id}</div>}
                  {signal.decision !== 'pending' && (
                    <div className="mt-1 text-[11px] text-ink-3">
                      {signal.decision === 'accepted'
                        ? candidateSignalIds.has(signal.signal_id)
                          ? '已进入系统候选。'
                          : '已确认，等待同根因信号达到候选升级门槛。'
                        : `处理结果：${signalDecisionLabel[signal.decision]}。`}
                      {signal.decided_by ? ` 操作人：${signal.decided_by}。` : ''}
                      {signal.updated_at ? ` 处理时间：${formatTime(signal.updated_at)}。` : ''}
                    </div>
                  )}
                </div>
                {signal.decision === 'pending' && <div className="flex items-center gap-1.5">
                  <button type="button" title="确认进入系统改进聚合" disabled={busySignalId === signal.signal_id} onClick={() => void decideSignal(signal, 'accepted')} className="inline-flex items-center gap-1.5 rounded-md bg-ok-soft px-2.5 py-1.5 text-xs font-semibold text-ok disabled:opacity-50"><Icon name="check" className="w-3.5 h-3.5" />确认</button>
                  <button type="button" title="证据不足，暂不聚合" disabled={busySignalId === signal.signal_id} onClick={() => void decideSignal(signal, 'deferred')} className="inline-flex items-center gap-1.5 rounded-md bg-bg-inset px-2.5 py-1.5 text-xs font-semibold text-ink-2 disabled:opacity-50"><Icon name="clock" className="w-3.5 h-3.5" />暂缓</button>
                  <button type="button" title="驳回该系统问题判断" disabled={busySignalId === signal.signal_id} onClick={() => void decideSignal(signal, 'dismissed')} className="w-8 h-8 inline-flex items-center justify-center rounded-md text-danger hover:bg-danger-soft disabled:opacity-50" aria-label="驳回系统信号"><Icon name="x" className="w-4 h-4" /></button>
                </div>}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="grid min-[980px]:grid-cols-[minmax(300px,0.85fr)_minmax(0,1.4fr)]">
        <div className="border-b border-border min-[980px]:border-b-0 min-[980px]:border-r">
          <div className="px-5 py-3 border-b border-border text-xs font-semibold">全局系统候选</div>
          {candidatesQuery.isLoading ? (
            <p className="p-5 text-xs text-ink-3">正在聚合已确认信号…</p>
          ) : candidates.length === 0 ? (
            <div className="p-5">
              <p className="text-sm font-semibold text-ink-2">当前没有可执行的改进候选</p>
              <p className="text-xs text-ink-3 mt-1 leading-relaxed">{globalAcceptedSignalCount > 0 ? `全局已有 ${globalAcceptedSignalCount} 条已确认信号，但同根因尚未达到候选门槛。` : '全局尚无已确认信号。'}{globalPendingSignalCount > 0 ? ` 另有 ${globalPendingSignalCount} 条待人工确认，暂不参与聚合。` : ''}</p>
              <p className="mt-2 border-t border-border pt-2 text-[11px] leading-relaxed text-ink-3">同根因满足任一条件才生成候选：同一项目至少 2 条，或至少 3 个项目，或属于高严重度代码、manifest、安全、数据丢失类问题。</p>
            </div>
          ) : (
            <div className="divide-y divide-border max-h-[620px] overflow-y-auto">
              {candidates.map((candidate) => (
                <button key={candidate.candidate_id} type="button" onClick={() => setSelectedCandidateId(candidate.candidate_id)} className={`w-full text-left px-5 py-4 transition-colors ${selectedCandidate?.candidate_id === candidate.candidate_id ? 'bg-brand-soft/45' : 'hover:bg-bg-inset'}`}>
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-xs font-semibold leading-snug line-clamp-2">{candidate.title}</span>
                    <Badge variant={statusVariant(candidate.status)}>{statusLabel[candidate.status]}</Badge>
                  </div>
                  <div className="mt-1.5 text-[11px] font-mono text-ink-3 break-all">{candidate.cause_key}</div>
                  <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-3">
                    <span>{candidate.active_count} 活跃</span>
                    <span>{candidate.occurrence_count} 条信号</span>
                    <span>{candidate.project_ids?.length ?? 0} 个项目</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="min-w-0">
          {!selectedCandidate ? (
            <p className="p-5 text-xs leading-relaxed text-ink-3">左侧尚无达到门槛的全局候选。候选生成后，这里会展示工程判断、回归计划和候选生命周期。</p>
          ) : (
            <div>
              <div className="px-5 py-4 border-b border-border">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap gap-2 mb-2"><Badge variant={severityVariant(selectedCandidate.severity)}>{selectedCandidate.severity} 优先级</Badge><Badge variant={statusVariant(selectedCandidate.status)}>{statusLabel[selectedCandidate.status]}</Badge></div>
                    <h3 className="text-base font-semibold leading-snug">{selectedCandidate.title}</h3>
                    <p className="mt-1 text-[11px] font-mono text-ink-3 break-all">{selectedCandidate.cause_key}</p>
                  </div>
                  <div className="text-right text-[11px] text-ink-3"><div>首次 {formatTime(selectedCandidate.first_seen_at)}</div><div>最近 {formatTime(selectedCandidate.last_seen_at)}</div></div>
                </div>
                <div className="grid grid-cols-2 gap-3 mt-4 min-[700px]:grid-cols-4">
                  {[['活跃', selectedCandidate.active_count], ['已解决', selectedCandidate.resolved_count], ['误报', selectedCandidate.false_positive_count], ['暂缓', selectedCandidate.deferred_count]].map(([label, value]) => <div key={String(label)}><div className="text-base font-semibold tabular-nums">{value}</div><div className="text-[11px] text-ink-3">{label}</div></div>)}
                </div>
              </div>

              <div className="px-5 py-4 grid gap-5 min-[760px]:grid-cols-2">
                <div>
                  <h4 className="text-xs font-semibold mb-2">工程判断</h4>
                  <dl className="space-y-2 text-xs">
                    <div><dt className="text-ink-3">影响层</dt><dd className="mt-0.5 text-ink-1">{selectedCandidate.affected_layers.join('、') || selectedCandidate.affected_layer}</dd></div>
                    <div><dt className="text-ink-3">根因假设</dt><dd className="mt-0.5 text-ink-1 leading-relaxed">{selectedCandidate.symptom_summary}</dd></div>
                    <div><dt className="text-ink-3">建议修改</dt><dd className="mt-0.5 text-ink-1 leading-relaxed">{selectedCandidate.suggested_fix || '等待工程分诊补充。'}</dd></div>
                    <div><dt className="text-ink-3">历史迁移</dt><dd className="mt-0.5 text-ink-1">{selectedCandidate.migration_required ? '需要评估历史文件迁移' : '当前未标记为需要迁移'}</dd></div>
                  </dl>
                </div>
                <div>
                  <h4 className="text-xs font-semibold mb-2">回归计划</h4>
                  <p className="text-xs text-ink-1 leading-relaxed">{selectedCandidate.suggested_test || '等待补充确定性回归断言。'}</p>
                  <div className="mt-3 space-y-1 text-[11px] text-ink-3">
                    <div>原始失败案例：{selectedCandidate.regression_plan.original_findings.length} 个</div>
                    <div>跨项目案例：{selectedCandidate.regression_plan.cross_project_case_required ? '需要' : '按影响范围决定'}</div>
                    <div>反例保护：{selectedCandidate.regression_plan.counterexample_required ? '必须' : '未要求'}</div>
                    <div>固定评测场景：{selectedCandidate.regression_plan.fixed_eval_scenario_required ? '需要' : '按风险决定'}</div>
                  </div>
                </div>
              </div>

              <CandidateRegressionPanel candidate={selectedCandidate} />

              <div className="px-5 py-4 border-t border-border bg-bg-inset/45">
                <h4 className="text-xs font-semibold mb-3">候选生命周期</h4>
                <div className="grid gap-3 min-[760px]:grid-cols-[160px_180px_1fr_auto] min-[760px]:items-end">
                  <label className="text-[11px] text-ink-3">状态<select value={candidateStatus} onChange={(event) => setCandidateStatus(event.target.value as OptimizationCandidate['status'])} className="mt-1 w-full border border-border rounded-md bg-surface px-2 py-2 text-xs text-ink-1">{statusTransitions[selectedCandidate.status].map((status) => <option key={status} value={status}>{statusLabel[status]}</option>)}</select></label>
                  <label className="text-[11px] text-ink-3">负责人<input value={candidateOwner} onChange={(event) => setCandidateOwner(event.target.value)} placeholder="未分配" className="mt-1 w-full border border-border rounded-md bg-surface px-2 py-2 text-xs text-ink-1" /></label>
                  <label className="text-[11px] text-ink-3">工作备注<input value={candidateNotes} onChange={(event) => setCandidateNotes(event.target.value)} placeholder="记录修复范围或下一步" className="mt-1 w-full border border-border rounded-md bg-surface px-2 py-2 text-xs text-ink-1" /></label>
                  <button type="button" disabled={candidateBusy} onClick={() => void saveCandidateState()} className="inline-flex items-center justify-center gap-1.5 rounded-md bg-brand px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"><Icon name="check" className="w-3.5 h-3.5" />保存</button>
                </div>
                <div className="mt-3 grid gap-3 min-[760px]:grid-cols-[210px_1fr_1fr]">
                  <label className="text-[11px] text-ink-3">目标作用域<select value={candidateTargetScope} onChange={(event) => setCandidateTargetScope(event.target.value as OptimizationCandidate['target_scope'])} className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-2 text-xs text-ink-1">{Object.entries(targetScopeLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                  <label className="text-[11px] text-ink-3">变更引用<input value={candidateChangeRef} onChange={(event) => setCandidateChangeRef(event.target.value)} placeholder="commit / PR / patch id" className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-2 text-xs text-ink-1" /></label>
                  <label className="text-[11px] text-ink-3">验证 Pair<input value={validationPairIds} onChange={(event) => setValidationPairIds(event.target.value)} placeholder="pair-id，多个用逗号分隔" className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-2 text-xs text-ink-1" /></label>
                </div>
                {(candidateStatus === 'monitoring' || candidateStatus === 'verified') && (
                  <label className="block mt-3 text-[11px] text-ink-3">回归与观察证据摘要<textarea value={verificationSummary} onChange={(event) => setVerificationSummary(event.target.value)} placeholder="记录已运行的原始失败案例、反例、固定评测场景和观察结果；标记已验证时必填。" className="mt-1 w-full min-h-20 resize-y border border-border rounded-md bg-surface px-2.5 py-2 text-xs text-ink-1 leading-relaxed" /></label>
                )}
                {candidateError && <p className="mt-2 text-xs text-danger">{candidateError}</p>}
              </div>

              <div className="px-5 py-4 border-t border-border">
                <h4 className="text-xs font-semibold mb-3">代表信号</h4>
                <div className="space-y-3">
                  {selectedCandidate.representative_signals.map((signal) => (
                    <div key={signal.signal_id} className="border-l-2 border-brand/45 pl-3">
                      <p className="text-xs text-ink-1 leading-relaxed">{signal.hypothesis}</p>
                      <p className="mt-1 text-[11px] text-ink-3">{signal.project_id} · {signal.finding_id}</p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  )
}
