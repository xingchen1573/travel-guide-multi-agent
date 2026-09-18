import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OptimizationCandidate } from '../api/types'
import { CandidateRegressionPanel } from './CandidateRegressionPanel'

const { createCase, startRun } = vi.hoisted(() => ({
  createCase: vi.fn(),
  startRun: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    createOptimizationRegressionCase: createCase,
    startOptimizationRegressionRun: startRun,
  },
}))

vi.mock('../api/hooks', () => ({
  useOptimizationRegressionCases: () => ({
    data: [
      {
        case_id: 'case-finding',
        candidate_id: 'candidate-1',
        case_type: 'original_finding',
        label: '原始失败 Finding · finding-1',
        project_id: 'project-1',
        finding_id: 'finding-1',
        test_selector: '',
        scenario_id: '',
        expected_outcome: 'not_active',
        config: {},
        status: 'ready',
        created_by: 'system',
        created_at: '2026-08-28T00:00:00Z',
        updated_at: '2026-08-28T00:00:00Z',
      },
      {
        case_id: 'case-draft',
        candidate_id: 'candidate-1',
        case_type: 'counterexample',
        label: '待绑定反例',
        project_id: '',
        finding_id: '',
        test_selector: '',
        scenario_id: '',
        expected_outcome: 'resolved',
        config: {},
        status: 'draft',
        created_by: 'system',
        created_at: '2026-08-28T00:00:00Z',
        updated_at: '2026-08-28T00:00:00Z',
      },
    ],
    isLoading: false,
  }),
  useOptimizationRegressionRuns: () => ({ data: [] }),
  useReviewEvalScenarios: () => ({ data: ['chengdu_3d'] }),
}))

const candidate = {
  candidate_id: 'candidate-1',
  cause_key: 'code:test',
  title: '回归执行器契约',
  symptom_summary: '回写语义错误',
  root_cause: 'code',
  affected_layer: 'runtime_contract',
  affected_layers: ['runtime_contract'],
  problem_type: 'manifest_mismatch',
  finding_ids: ['project-1:finding-1'],
  signal_ids: ['signal-1'],
  project_ids: ['project-1'],
  source_pair_ids: [],
  occurrence_count: 2,
  active_count: 2,
  resolved_count: 0,
  false_positive_count: 0,
  deferred_count: 0,
  confidence: 'high',
  severity: 'high',
  status: 'implementing',
  owner: '',
  notes: '',
  verification_summary: '',
  target_scope: 'undecided',
  change_ref: '',
  validation_pair_ids: [],
  first_seen_at: '2026-08-28T00:00:00Z',
  last_seen_at: '2026-08-28T00:00:00Z',
  created_at: '2026-08-28T00:00:00Z',
  migration_required: false,
  suggested_fix: '修正契约',
  suggested_test: '运行定向回归',
  suggested_actions: [],
  regression_plan: {
    original_findings: ['project-1:finding-1'],
    cross_project_case_required: false,
    counterexample_required: true,
    suggested_tests: [],
    fixed_eval_scenario_required: false,
  },
  representative_signals: [],
} satisfies OptimizationCandidate

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CandidateRegressionPanel candidate={candidate} />
    </QueryClientProvider>,
  )
}

describe('CandidateRegressionPanel', () => {
  beforeEach(() => {
    createCase.mockReset().mockResolvedValue({})
    startRun.mockReset().mockResolvedValue({})
  })

  it('selects ready cases and starts a bounded regression run', async () => {
    renderPanel()

    expect(screen.getByText('待绑定反例')).toBeInTheDocument()
    const runButton = await screen.findByRole('button', { name: '运行已选 1 项' })
    fireEvent.click(runButton)

    await waitFor(() => expect(startRun).toHaveBeenCalledWith('candidate-1', {
      case_ids: ['case-finding'],
      advance_to_monitoring: true,
    }))
  })

  it('binds an exact pytest selector as a ready case', async () => {
    renderPanel()
    const selector = 'tests/test_review_loop.py::test_repeated_root_cause_creates_optimization_candidate'

    fireEvent.change(screen.getByPlaceholderText('tests/test_x.py::test_case'), { target: { value: selector } })
    fireEvent.click(screen.getByRole('button', { name: '加入' }))

    await waitFor(() => expect(createCase).toHaveBeenCalledWith('candidate-1', {
      case_type: 'unit_test',
      label: selector,
      test_selector: selector,
      scenario_id: '',
      config: {},
    }))
  })
})
