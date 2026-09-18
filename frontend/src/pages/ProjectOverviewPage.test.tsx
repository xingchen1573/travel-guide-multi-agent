import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { ProjectOverviewPage } from './ProjectOverviewPage'
import * as hooks from '../api/hooks'

const baseProject = {
  project_id: 'chengdu-v1',
  topic: '成都',
  destination: '成都',
  days: 3,
  style: '美食+人文',
  budget: '中等',
  notes: '不吃辣',
  must_visit: [],
  style_interpretation: null,
  state: '完成',
  updated_at: '2026-07-24T00:00:00Z',
  progress: {
    state: '完成',
    agents: { route_agent: 'done' },
    skeleton_confirmed: true,
    material_checked: true,
    updated_at: '2026-07-24T00:00:00Z',
  },
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/projects/chengdu-v1']}>
        <Routes>
          <Route path="/projects/:projectId" element={<ProjectOverviewPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProjectOverviewPage rerun confirmation', () => {
  it('does not trigger rerun until the user confirms the dialog', () => {
    const trigger = vi.fn()
    vi.spyOn(hooks, 'useProject').mockReturnValue({
      data: baseProject,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>)
    vi.spyOn(hooks, 'useRerunProject').mockReturnValue({
      trigger,
      triggerError: null,
      isTriggering: false,
      status: undefined,
    } as unknown as ReturnType<typeof hooks.useRerunProject>)
    vi.spyOn(hooks, 'useRunStatus').mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof hooks.useRunStatus>)
    vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useDeleteProject>)

    renderPage()

    fireEvent.click(screen.getByRole('button', { name: '彻底重跑' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(trigger).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '彻底重跑' }))
    fireEvent.click(screen.getByRole('button', { name: '确定重跑' }))
    expect(trigger).toHaveBeenCalledTimes(1)
  })

  it('disables the rerun button while a rerun is in flight', () => {
    vi.spyOn(hooks, 'useProject').mockReturnValue({
      data: baseProject,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>)
    vi.spyOn(hooks, 'useRerunProject').mockReturnValue({
      trigger: vi.fn(),
      triggerError: null,
      isTriggering: false,
      status: { status: 'running', error: null, started_at: 't0', finished_at: null },
    } as unknown as ReturnType<typeof hooks.useRerunProject>)
    vi.spyOn(hooks, 'useRunStatus').mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof hooks.useRunStatus>)
    vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useDeleteProject>)

    renderPage()

    const button = screen.getByRole('button', { name: '重跑中…' })
    expect(button).toBeDisabled()
  })

  it('shows the current running node from run_status polling', () => {
    vi.spyOn(hooks, 'useProject').mockReturnValue({
      data: baseProject,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>)
    vi.spyOn(hooks, 'useRerunProject').mockReturnValue({
      trigger: vi.fn(),
      triggerError: null,
      isTriggering: false,
      status: undefined,
    } as unknown as ReturnType<typeof hooks.useRerunProject>)
    vi.spyOn(hooks, 'useRunStatus').mockReturnValue({
      data: { node: 'route_draft', status: 'running', call_count: 1, started_at: 't0', finished_at: null, error: null },
    } as unknown as ReturnType<typeof hooks.useRunStatus>)
    vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useDeleteProject>)

    renderPage()

    expect(screen.getByText('第 1 次调用')).toBeInTheDocument()
  })

  it('shows a prominent failure banner when the project is stuck on a failed node', () => {
    vi.spyOn(hooks, 'useProject').mockReturnValue({
      data: { ...baseProject, state: '并行调研中', has_run_failure: true, progress: { ...baseProject.progress, state: '并行调研中' } },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>)
    vi.spyOn(hooks, 'useRerunProject').mockReturnValue({
      trigger: vi.fn(),
      triggerError: null,
      isTriggering: false,
      status: undefined,
    } as unknown as ReturnType<typeof hooks.useRerunProject>)
    vi.spyOn(hooks, 'useRunStatus').mockReturnValue({
      data: {
        node: 'lifestyle_agent_node',
        status: 'failed',
        call_count: 1,
        started_at: 't0',
        finished_at: 't1',
        error: '模拟写入失败',
      },
    } as unknown as ReturnType<typeof hooks.useRunStatus>)
    vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useDeleteProject>)

    renderPage()

    expect(screen.getByText(/上次图执行在/)).toBeInTheDocument()
    expect(screen.getAllByText('模拟写入失败')).toHaveLength(2) // banner + 实时Agent运行状态区块各展示一次
    expect(screen.getByText('运行失败 · 并行调研中')).toBeInTheDocument()
  })

  it('deletes the project only after confirming the dialog', () => {
    const mutate = vi.fn()
    vi.spyOn(hooks, 'useProject').mockReturnValue({
      data: baseProject,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>)
    vi.spyOn(hooks, 'useRerunProject').mockReturnValue({
      trigger: vi.fn(),
      triggerError: null,
      isTriggering: false,
      status: undefined,
    } as unknown as ReturnType<typeof hooks.useRerunProject>)
    vi.spyOn(hooks, 'useRunStatus').mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof hooks.useRunStatus>)
    vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof hooks.useDeleteProject>)

    renderPage()

    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确定删除' }))
    expect(mutate).toHaveBeenCalledWith('chengdu-v1', expect.anything())
  })

  it('shows a placeholder when must_visit/style_interpretation are absent', () => {
    vi.spyOn(hooks, 'useProject').mockReturnValue({
      data: baseProject,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>)
    vi.spyOn(hooks, 'useRerunProject').mockReturnValue({
      trigger: vi.fn(),
      triggerError: null,
      isTriggering: false,
      status: undefined,
    } as unknown as ReturnType<typeof hooks.useRerunProject>)
    vi.spyOn(hooks, 'useRunStatus').mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof hooks.useRunStatus>)
    vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useDeleteProject>)

    renderPage()

    expect(screen.getByText('必去地点：').closest('div')).toHaveTextContent('必去地点：无')
    expect(screen.getByText('风格解读：').closest('div')).toHaveTextContent('风格解读：未提供风格偏好')
  })

  it('renders must_visit items with day labels and style_interpretation dimensions', () => {
    vi.spyOn(hooks, 'useProject').mockReturnValue({
      data: {
        ...baseProject,
        must_visit: [
          { name: '熊猫基地', day: 2 },
          { name: '都江堰', day: null },
        ],
        style_interpretation: {
          poi_density: '偏少',
          stay_duration: '偏多',
          needs_detail_bias: null,
          flexible_time: null,
        },
      },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>)
    vi.spyOn(hooks, 'useRerunProject').mockReturnValue({
      trigger: vi.fn(),
      triggerError: null,
      isTriggering: false,
      status: undefined,
    } as unknown as ReturnType<typeof hooks.useRerunProject>)
    vi.spyOn(hooks, 'useRunStatus').mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof hooks.useRunStatus>)
    vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useDeleteProject>)

    renderPage()

    expect(screen.getByText('必去地点：').closest('div')).toHaveTextContent(
      '必去地点：熊猫基地（第2天）、都江堰（未指定天数）',
    )
    expect(screen.getByText('风格解读：').closest('div')).toHaveTextContent(
      '风格解读：每天POI数量密度=偏少、单点停留时长倾向=偏多',
    )
  })
})
