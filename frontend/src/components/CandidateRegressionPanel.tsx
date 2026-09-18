import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import {
  useOptimizationRegressionCases,
  useOptimizationRegressionRuns,
  useReviewEvalScenarios,
} from '../api/hooks'
import type {
  OptimizationCandidate,
  OptimizationRegressionCase,
  OptimizationRegressionRun,
} from '../api/types'
import { Badge } from './Badge'
import { Icon } from './Icon'

const EMPTY_CASES: OptimizationRegressionCase[] = []
const EMPTY_RUNS: OptimizationRegressionRun[] = []

const caseTypeLabel: Record<OptimizationRegressionCase['case_type'], string> = {
  original_finding: '原始 Finding',
  cross_project_finding: '跨项目 Finding',
  unit_test: '定向 pytest',
  counterexample: '反例保护',
  fixed_eval: '固定场景 eval',
}

const runStatusLabel: Record<OptimizationRegressionRun['status'], string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已通过',
  failed: '未通过',
  cancelled: '已取消',
  interrupted: '已中断',
}

function runVariant(status: OptimizationRegressionRun['status']) {
  if (status === 'succeeded') return 'ok' as const
  if (status === 'failed' || status === 'interrupted') return 'danger' as const
  if (status === 'running') return 'brand' as const
  return 'pending' as const
}

function shortFingerprint(value: unknown) {
  return typeof value === 'string' && value ? value.slice(0, 12) : '未记录'
}

export function CandidateRegressionPanel({ candidate }: { candidate: OptimizationCandidate }) {
  const queryClient = useQueryClient()
  const casesQuery = useOptimizationRegressionCases(candidate.candidate_id)
  const runsQuery = useOptimizationRegressionRuns(candidate.candidate_id)
  const scenariosQuery = useReviewEvalScenarios()
  const cases = casesQuery.data ?? EMPTY_CASES
  const runs = runsQuery.data ?? EMPTY_RUNS
  const latestRun = runs[0] ?? null
  const [selectedCaseIds, setSelectedCaseIds] = useState<string[]>([])
  const [newCaseType, setNewCaseType] = useState<'unit_test' | 'counterexample' | 'fixed_eval'>('unit_test')
  const [testSelector, setTestSelector] = useState('')
  const [scenarioId, setScenarioId] = useState('')
  const [creating, setCreating] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const defaultCaseKey = cases
    .filter((item) => item.status === 'ready' && item.case_type !== 'fixed_eval')
    .map((item) => item.case_id)
    .join('|')
  const activeRun = runs.some((item) => ['queued', 'running'].includes(item.status))

  useEffect(() => {
    setSelectedCaseIds(defaultCaseKey ? defaultCaseKey.split('|') : [])
  }, [candidate.candidate_id, defaultCaseKey])

  useEffect(() => {
    if (!scenarioId && scenariosQuery.data?.length) setScenarioId(scenariosQuery.data[0])
  }, [scenarioId, scenariosQuery.data])

  function toggleCase(caseId: string) {
    setSelectedCaseIds((current) => (
      current.includes(caseId) ? current.filter((item) => item !== caseId) : [...current, caseId]
    ))
  }

  async function createCase() {
    setError(null)
    const selector = testSelector.trim()
    if (newCaseType !== 'fixed_eval' && !selector) {
      setError('请填写 tests/ 下的精确 pytest selector。')
      return
    }
    if (newCaseType === 'fixed_eval' && !scenarioId) {
      setError('请选择固定评测场景。')
      return
    }
    let costConfirmed = false
    if (newCaseType === 'fixed_eval') {
      costConfirmed = window.confirm('固定 eval 会调用真实模型、联网搜索与图像链路，可能产生费用并耗时。确认将该场景加入可执行回归集吗？')
      if (!costConfirmed) return
    }
    setCreating(true)
    try {
      await api.createOptimizationRegressionCase(candidate.candidate_id, {
        case_type: newCaseType,
        label: newCaseType === 'fixed_eval' ? `固定评测 · ${scenarioId}` : selector,
        test_selector: newCaseType === 'fixed_eval' ? '' : selector,
        scenario_id: newCaseType === 'fixed_eval' ? scenarioId : '',
        config: newCaseType === 'fixed_eval' ? { cost_confirmed: costConfirmed } : {},
      })
      setTestSelector('')
      await queryClient.invalidateQueries({ queryKey: ['optimization-regression-cases', candidate.candidate_id] })
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setCreating(false)
    }
  }

  async function startRun() {
    setError(null)
    if (!selectedCaseIds.length) {
      setError('至少选择一个已就绪案例。')
      return
    }
    const selectedCases = cases.filter((item) => selectedCaseIds.includes(item.case_id))
    if (selectedCases.some((item) => item.case_type === 'fixed_eval')) {
      const confirmed = window.confirm('本次回归包含固定 eval，将立即调用真实模型与联网链路。确认开始运行吗？')
      if (!confirmed) return
    }
    setRunning(true)
    try {
      await api.startOptimizationRegressionRun(candidate.candidate_id, {
        case_ids: selectedCaseIds,
        advance_to_monitoring: true,
      })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['optimization-regression-runs', candidate.candidate_id] }),
        queryClient.invalidateQueries({ queryKey: ['global-optimization-candidates'] }),
        queryClient.invalidateQueries({ queryKey: ['quality-metrics'] }),
      ])
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="px-5 py-4 border-t border-border">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h4 className="text-xs font-semibold">定向回放与回归</h4>
          <p className="mt-1 text-[11px] text-ink-3">只执行已绑定的历史问题、精确测试节点或显式确认的固定评测场景。</p>
        </div>
        <button type="button" title="运行选中的定向回归案例" disabled={running || activeRun || selectedCaseIds.length === 0} onClick={() => void startRun()} className="inline-flex items-center gap-1.5 rounded-md bg-brand px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">
          <Icon name="refresh" className="w-3.5 h-3.5" />
          {activeRun ? '回归运行中' : `运行已选 ${selectedCaseIds.length} 项`}
        </button>
      </div>

      <div className="mt-4 border-y border-border divide-y divide-border">
        {casesQuery.isLoading ? (
          <p className="py-3 text-xs text-ink-3">正在生成候选的回归集…</p>
        ) : cases.length === 0 ? (
          <p className="py-3 text-xs text-ink-3">还没有可回放案例。</p>
        ) : cases.map((item) => (
          <label key={item.case_id} className={`flex items-start gap-3 py-3 ${item.status !== 'ready' ? 'opacity-65' : ''}`}>
            <input type="checkbox" checked={selectedCaseIds.includes(item.case_id)} disabled={item.status !== 'ready'} onChange={() => toggleCase(item.case_id)} className="mt-0.5 h-4 w-4 accent-brand" />
            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold text-ink-1">{item.label}</span>
                <Badge variant={item.status === 'ready' ? 'ok' : 'pending'}>{item.status === 'ready' ? '已就绪' : '待绑定'}</Badge>
              </span>
              <span className="mt-1 block text-[11px] text-ink-3">
                {caseTypeLabel[item.case_type]}
                {item.test_selector ? ` · ${item.test_selector}` : ''}
                {item.scenario_id ? ` · ${item.scenario_id}` : ''}
              </span>
            </span>
          </label>
        ))}
      </div>

      <div className="mt-4 grid gap-2 min-[760px]:grid-cols-[150px_1fr_auto] min-[760px]:items-end">
        <label className="text-[11px] text-ink-3">新增保护案例
          <select value={newCaseType} onChange={(event) => setNewCaseType(event.target.value as typeof newCaseType)} className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-2 text-xs text-ink-1">
            <option value="unit_test">定向 pytest</option>
            <option value="counterexample">反例保护</option>
            <option value="fixed_eval">固定场景 eval</option>
          </select>
        </label>
        {newCaseType === 'fixed_eval' ? (
          <label className="text-[11px] text-ink-3">评测场景
            <select value={scenarioId} onChange={(event) => setScenarioId(event.target.value)} className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-2 text-xs text-ink-1">
              {(scenariosQuery.data ?? []).map((scenario) => <option key={scenario} value={scenario}>{scenario}</option>)}
            </select>
          </label>
        ) : (
          <label className="text-[11px] text-ink-3">pytest selector
            <input value={testSelector} onChange={(event) => setTestSelector(event.target.value)} placeholder="tests/test_x.py::test_case" className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-2 text-xs text-ink-1 font-mono" />
          </label>
        )}
        <button type="button" title="将该案例加入定向回归集" disabled={creating} onClick={() => void createCase()} className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border bg-surface px-3 py-2 text-xs font-semibold text-ink-1 hover:bg-bg-inset disabled:opacity-50">
          <Icon name="plus" className="w-3.5 h-3.5" />加入
        </button>
      </div>

      {candidate.status === 'implementing' ? (
        <p className="mt-2 text-[11px] text-ink-3">所有已选案例通过且至少包含一个 pytest、反例或固定 eval 后，候选才会自动进入“观察中”。</p>
      ) : (
        <p className="mt-2 text-[11px] text-ink-3">当前状态不是“实施中”；回归结果会留存，但不会自动推进候选状态。</p>
      )}
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}

      {latestRun && (
        <div className="mt-5 border-t border-border pt-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h5 className="text-xs font-semibold">最近一次回归</h5>
              <Badge variant={runVariant(latestRun.status)}>{runStatusLabel[latestRun.status]}</Badge>
            </div>
            <span className="text-[11px] font-mono text-ink-3">{latestRun.run_id}</span>
          </div>
          <p className="mt-2 text-xs text-ink-2">{latestRun.progress_message}</p>
          {latestRun.results.length > 0 && (
            <div className="mt-3 divide-y divide-border border-y border-border">
              {latestRun.results.map((result) => (
                <div key={result.case_id} className="flex items-start justify-between gap-3 py-2.5">
                  <div className="min-w-0">
                    <div className="text-xs font-semibold text-ink-1">{result.label}</div>
                    <div className="mt-0.5 text-[11px] text-ink-3">{result.summary}</div>
                  </div>
                  <Badge variant={result.status === 'passed' ? 'ok' : result.status === 'skipped' ? 'pending' : 'danger'}>{result.status}</Badge>
                </div>
              ))}
            </div>
          )}
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] font-mono text-ink-3">
            <span>code {shortFingerprint(latestRun.code_fingerprint)}</span>
            <span>prompt {shortFingerprint(latestRun.prompt_fingerprint.prompt_hash)}</span>
          </div>
        </div>
      )}
    </section>
  )
}
