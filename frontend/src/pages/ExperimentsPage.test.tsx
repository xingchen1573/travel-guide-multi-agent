import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as hooks from '../api/hooks'
import { ExperimentsPage } from './ExperimentsPage'

vi.mock('../api/hooks')

const summaries = [
  { project_id: 'langgraph-exp', schema_version: '1', metrics_schema_version: '1', scenario_id: 'same-case', experiment_version: 'lg-v1', harness: 'langgraph', destination: '峨眉山', trip_days: 3, route_sha: 'abc', model: 'deepseek', provider: 'MiMo', project_status: '完成', completion_mode: 'completed', started_at: null, completed_at: null, wall_clock_seconds: null, comparison_grade: 'A', source_fingerprint: 'f1', indexed_at: '2026-09-02T00:00:00Z', relations: [{ target_project_id: 'codex-exp', relation_type: 'comparison_peer', note: 'same route' }] },
  { project_id: 'codex-exp', schema_version: '1', metrics_schema_version: '1', scenario_id: 'same-case', experiment_version: 'codex-v1', harness: 'codex_skill', destination: '峨眉山', trip_days: 3, route_sha: 'abc', model: 'gpt', provider: 'Codex', project_status: '完成', completion_mode: 'completed', started_at: null, completed_at: null, wall_clock_seconds: 60, comparison_grade: 'A', source_fingerprint: 'f2', indexed_at: '2026-09-02T00:00:00Z', relations: [{ target_project_id: 'langgraph-exp', relation_type: 'comparison_peer', note: 'same route' }] },
] as const

const pairs = [{
  pair_id: 'pair-same-case-v1', codex_project_id: 'codex-exp', langgraph_project_id: 'langgraph-exp', source_input_project_id: 'source-input', comparison_scope: 'system', declared_grade: 'A', effective_grade: 'B', status: 'complete', origin: 'test', note: 'same route', evidence_refs: [], indexed_at: '2026-09-02T00:00:00Z',
  comparability: { verdict: 'system_comparable', checks: {
    requirements: { status: 'match', codex: 'req', langgraph: 'req' },
    route: { status: 'match', codex: 'abc', langgraph: 'abc' },
    materials: { status: 'unknown', codex: null, langgraph: null },
    model: { status: 'different', codex: 'gpt', langgraph: 'deepseek' },
    provider: { status: 'different', codex: 'Codex', langgraph: 'MiMo' },
  }, reason_codes: ['material_baseline_unknown'] },
  codex_experiment: { ...summaries[1] }, langgraph_experiment: { ...summaries[0] },
  observed_issues: {
    active_finding_count: 3,
    stage_observation_count: 1,
    stage_observations: [{ key: 'lifestyle', label: 'Lifestyle', langgraph_status: 'force_finalized', codex_status: 'passed', kind: 'different' }],
    langgraph: {
      project_id: 'langgraph-exp', active_finding_count: 2,
      severity_counts: { high: 0, medium: 2, low: 0 }, problem_type_counts: { force_finalized_residue: 2 },
      preview_findings: [{ finding_id: 'finding-lg-1', problem_type: 'force_finalized_residue', root_cause: 'evidence', severity: 'medium', status: 'open', summary: '引用没有覆盖马旺子川小馆。' }],
    },
    codex: {
      project_id: 'codex-exp', active_finding_count: 1,
      severity_counts: { high: 0, medium: 1, low: 0 }, problem_type_counts: { manifest_mismatch: 1 },
      preview_findings: [{ finding_id: 'finding-codex-1', problem_type: 'manifest_mismatch', root_cause: 'code', severity: 'medium', status: 'open', summary: 'manifest 与详情状态不一致。' }],
    },
  },
  source_candidates: [], validation_candidates: [],
}] as const

function detail(projectId: 'langgraph-exp' | 'codex-exp') {
  const harness = projectId === 'langgraph-exp' ? 'langgraph' : 'codex_skill'
  return {
    indexed_at: '2026-09-02T00:00:00Z', relations: [], optimization_candidates: [],
    metrics: {
      identity: { schema_version: '1', is_experiment: true, project_id: projectId, scenario_id: 'same-case', experiment_version: projectId, harness, destination: '峨眉山', trip_days: 3, route_sha: 'abc', requirements_sha: 'req', project_status: '完成', completion_mode: 'completed', comparison_grade: 'A', model: harness === 'langgraph' ? 'deepseek' : 'gpt', provider: harness === 'langgraph' ? 'MiMo' : 'Codex', inherited_downstream_outputs: false, extraction_time: '2026-09-02T00:00:00Z' },
      outputs: { route_day_count: 3, lifestyle_item_count: 0 },
      quality_contract: { withheld_claim_leak_count: 0 }, route_gate: { warning_count: 0 },
      execution: { business_retry_count: { value: 0, value_kind: 'derived', source_paths: [], calculation_method: 'count', missing_reason: null } },
      tool_usage: { model_call_count: { value: null, value_kind: 'unknown', source_paths: [], calculation_method: null, missing_reason: '埋点不完整' } },
      context: {},
      stages: [{ key: 'route', label: 'Route Gate', status: 'passed', evidence_paths: [] }],
      status: { overall: 'deliverable', delivery: 'passed', route_risk: 'passed', poi_completeness: 'passed', recovery: 'completed', protocol: 'conformant_or_unknown', attention: [] },
      poi_details: [{ day: 2, poi_name: '金顶', generation_status: 'passed', citation_status: 'passed', content_depth: 'route_outline', route_count: 1, recommended_route_count: 0, segment_count: 0, claim_count: 0 }],
      evidence_paths: [], missing_metrics: [{ group: 'tool_usage', field: 'model_call_count', reason: '埋点不完整' }], relations: [], source_fingerprint: 'f',
    },
  }
}

describe('ExperimentsPage', () => {
  beforeEach(() => {
    vi.mocked(hooks.useExperiments).mockReturnValue({ data: [...summaries], isLoading: false, error: null } as unknown as ReturnType<typeof hooks.useExperiments>)
    vi.mocked(hooks.useExperiment).mockImplementation((projectId) => ({ data: projectId === 'langgraph-exp' || projectId === 'codex-exp' ? detail(projectId) : undefined, isLoading: false, error: null } as ReturnType<typeof hooks.useExperiment>))
    vi.mocked(hooks.useComparisonPairs).mockReturnValue({ data: [...pairs], isLoading: false, error: null } as unknown as ReturnType<typeof hooks.useComparisonPairs>)
    vi.mocked(hooks.useReindexExperiments).mockReturnValue({ mutate: vi.fn(), isPending: false, data: undefined } as unknown as ReturnType<typeof hooks.useReindexExperiments>)
  })

  it('renders lineage and preserves zero versus unknown in comparison', () => {
    render(<MemoryRouter><ExperimentsPage /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /实验档案/ }))
    expect(screen.getByText('历史版本谱系')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '对照详情' }))
    expect(screen.getByText('可比性契约')).toBeInTheDocument()
    expect(screen.getAllByText('0').length).toBeGreaterThan(0)
    expect(screen.getAllByText('未知').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/不能把差异单独归因于 Harness/).length).toBeGreaterThan(0)
    expect(screen.getByLabelText('正式 1:1 Pair')).toHaveValue('pair-same-case-v1')
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
    expect(screen.queryByText(/自由诊断/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('Pair 绑定成员')).toHaveTextContent('LANGGRAPH · 已绑定')
    expect(screen.getByLabelText('Pair 绑定成员')).toHaveTextContent('CODEX SKILL · 已绑定')
  })

  it('renders formal pair without inventing a Shared Kernel candidate', () => {
    render(<MemoryRouter><ExperimentsPage /></MemoryRouter>)
    expect(screen.getByText('pair-same-case-v1')).toBeInTheDocument()
    expect(screen.getByText('可比性等级')).toBeInTheDocument()
    fireEvent.mouseEnter(screen.getByRole('button', { name: '了解 B 级可比性' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('素材基线指纹未知，从 A 级降为 B 级')
    expect(screen.getByRole('tooltip')).toHaveTextContent('不能把差异单独归因于 Harness')
    fireEvent.mouseLeave(screen.getByRole('button', { name: '了解 B 级可比性' }))
    fireEvent.focus(screen.getByRole('button', { name: '了解可比性等级' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('不是系统可用率')
    expect(screen.getByText('两侧开放记录')).toBeInTheDocument()
    expect(screen.getByText(/这些是两侧项目已有记录/)).toBeInTheDocument()
    expect(screen.getByText(/当前没有来自 Pair 的改进候选/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看对照' }))
    expect(screen.getByText('引用没有覆盖马旺子川小馆。')).toBeInTheDocument()
    expect(screen.getByText('manifest 与详情状态不一致。')).toBeInTheDocument()
    expect(screen.getByText('force_finalized')).toBeInTheDocument()
    expect(screen.getByText(/不等于已确认系统根因/)).toBeInTheDocument()
  })

  it('shows citation status and POI content depth independently', () => {
    render(<MemoryRouter><ExperimentsPage /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /实验档案/ }))
    fireEvent.click(screen.getAllByRole('button', { name: '详情' })[0])
    expect(screen.getByText('POI 内容深度')).toBeInTheDocument()
    expect(screen.getByText('Day 2 · 金顶')).toBeInTheDocument()
    expect(screen.getByText('只有路线框架')).toBeInTheDocument()
    expect(screen.getByText(/引用通过只表示/)).toBeInTheDocument()
  })

  it('keeps archive columns separated when experiment labels are long', () => {
    render(<MemoryRouter><ExperimentsPage /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /实验档案/ }))
    const table = screen.getByRole('table', { name: '实验清单' })
    expect(table).toHaveClass('table-fixed', 'min-w-[1380px]')
    expect(table.querySelectorAll('col')).toHaveLength(7)
    expect(screen.getByRole('columnheader', { name: 'Harness' })).toHaveClass('px-3')
    expect(screen.getByRole('columnheader', { name: '模型' })).toHaveClass('px-3')
  })
})
