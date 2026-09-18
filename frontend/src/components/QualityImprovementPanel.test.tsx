import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { QualityImprovementPanel } from './QualityImprovementPanel'

const { decideImprovementSignal, signalState } = vi.hoisted(() => ({
  decideImprovementSignal: vi.fn(),
  signalState: { current: [] as Array<Record<string, unknown>> },
}))

vi.mock('../api/client', () => ({
  api: {
    decideImprovementSignal,
    updateOptimizationCandidate: vi.fn(),
  },
}))

vi.mock('../api/hooks', () => ({
  useComparisonPairs: () => ({
    data: [{
      pair_id: 'pair-project-1', codex_project_id: 'project-1', langgraph_project_id: 'peer-1', status: 'complete',
      observed_issues: {
        active_finding_count: 12,
        codex: { active_finding_count: 2 },
        langgraph: { active_finding_count: 10 },
      },
    }],
    isLoading: false,
  }),
  useGlobalOptimizationCandidates: () => ({ data: [], isLoading: false }),
  useImprovementSignals: () => ({
    data: signalState.current,
    isLoading: false,
  }),
  useQualityMetrics: () => ({
    data: {
      pending_signal_count: 2,
      accepted_signal_count: 1,
      candidate_count: 0,
      monitoring_candidate_count: 0,
      verified_candidate_count: 0,
      generated_at: '2026-08-28T00:00:00Z',
    },
  }),
}))

const signals = [{
      signal_id: 'signal-1',
      project_id: 'project-1',
      finding_id: 'finding-1',
      recommendation_id: 'recommendation-1',
      review_run_id: 'run-1',
      source_type: 'codex_skill',
      cause_key: 'citation:reference_attachment_mismatch',
      root_cause: 'evidence',
      affected_layer: 'evidence_pipeline',
      problem_type: 'reference_attachment_mismatch',
      hypothesis: '当前引用失配后没有继续检索候选素材。',
      evidence_refs: ['chunk-1'],
      representative_targets: ['/routes/0/route_segments/0'],
      suggested_fix: '补充目的地候选素材检索。',
      suggested_test: '候选命中时建议修复引用。',
      severity: 'high',
      confidence: 'high',
      migration_required: false,
      regression_scope: [],
      decision: 'pending',
      decision_reason: '',
      decided_by: '',
      baseline: {},
      created_at: '2026-08-28T00:00:00Z',
      updated_at: '2026-08-28T00:00:00Z',
    }, {
      signal_id: 'signal-accepted',
      project_id: 'project-1',
      finding_id: 'finding-1',
      recommendation_id: 'recommendation-1',
      review_run_id: 'run-1',
      source_type: 'codex_skill',
      cause_key: 'content:compound_review_action',
      root_cause: 'content',
      affected_layer: 'content_pipeline',
      problem_type: 'compound_review_action',
      hypothesis: '已确认的复合处理动作问题。',
      evidence_refs: [],
      representative_targets: ['final_guide.md'],
      suggested_fix: '拆分内容与证据动作。',
      suggested_test: '分别展示两种动作。',
      severity: 'medium',
      confidence: 'medium',
      migration_required: false,
      regression_scope: [],
      decision: 'accepted',
      decision_reason: '',
      decided_by: 'admin_user',
      baseline: {},
      created_at: '2026-08-28T00:00:00Z',
      updated_at: '2026-08-28T01:00:00Z',
    }]

describe('QualityImprovementPanel', () => {
  beforeEach(() => {
    decideImprovementSignal.mockReset().mockResolvedValue({})
    signalState.current = signals
  })

  it('keeps project signals separate from global candidates and supports explicit acceptance', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <QualityImprovementPanel projectId="project-1" />
      </QueryClientProvider>,
    )

    expect(screen.getByText(/开放记录须经复核和人工确认/)).toBeInTheDocument()
    expect(screen.getByText('全局系统改进漏斗')).toBeInTheDocument()
    expect(screen.getByText('全局待确认信号')).toBeInTheDocument()
    fireEvent.mouseEnter(screen.getByRole('button', { name: '了解全部 Pair 开放记录' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('这不是当前项目的数量')
    expect(screen.getByRole('tooltip')).toHaveTextContent('本项目 2 条，对照侧 10 条')
    expect(screen.getByText('当前引用失配后没有继续检索候选素材。')).toBeInTheDocument()
    expect(screen.getByText('当前没有可执行的改进候选')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() => expect(decideImprovementSignal).toHaveBeenCalledWith(
      'project-1',
      'signal-1',
      { decision: 'accepted', comparison_pair_id: 'pair-project-1' },
    ))
    expect(screen.getByText('已确认的复合处理动作问题。')).toBeInTheDocument()
    expect(screen.getByText(/已确认，等待同根因信号达到候选升级门槛/)).toBeInTheDocument()
    expect(screen.getByText(/来源：Codex Skill/)).toBeInTheDocument()
  })

  it('explains why a project with open findings has no system signals', () => {
    signalState.current = []
    const onOpenReview = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <QualityImprovementPanel projectId="project-1" currentPendingFindingCount={2} onOpenReview={onOpenReview} />
      </QueryClientProvider>,
    )

    expect(screen.getByText('本项目有 2 条待复核问题，但尚未形成待确认的系统信号。')).toBeInTheDocument()
    expect(screen.getByText(/全局已有 1 条已确认信号/)).toBeInTheDocument()
    expect(screen.getByText(/同一项目至少 2 条/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /前往待复核问题/ }))
    expect(onOpenReview).toHaveBeenCalledOnce()
  })
})
