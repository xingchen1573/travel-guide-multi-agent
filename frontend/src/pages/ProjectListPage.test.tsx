import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectListPage } from './ProjectListPage'
import * as hooks from '../api/hooks'
import type { ProjectSummary } from '../api/types'

const PROJECTS: ProjectSummary[] = [
  {
    project_id: 'chengdu-v1',
    topic: '成都',
    destination: '成都',
    days: 3,
    style: '美食',
    budget: '中等',
    notes: null,
    must_visit: [],
    style_interpretation: null,
    state: '完成',
    updated_at: '2026-07-24T00:00:00Z',
    has_pending_conflicts: false,
    has_run_failure: false,
  },
  {
    project_id: 'chengdu-v2',
    topic: '成都',
    destination: '成都',
    days: 2,
    style: '人文',
    budget: '经济',
    notes: null,
    must_visit: [],
    style_interpretation: null,
    state: '并行调研中',
    updated_at: '2026-07-24T01:00:00Z',
    has_pending_conflicts: true,
    has_run_failure: false,
    review_findings_open: 2,
  },
]

function renderPage(projects: ProjectSummary[] = PROJECTS, mutate = vi.fn()) {
  vi.spyOn(hooks, 'useProjects').mockReturnValue({
    data: projects,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useProjects>)
  vi.spyOn(hooks, 'useDeleteProject').mockReturnValue({
    mutate,
  } as unknown as ReturnType<typeof hooks.useDeleteProject>)

  return render(
    <MemoryRouter>
      <ProjectListPage />
    </MemoryRouter>,
  )
}

describe('ProjectListPage', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('defaults to completed projects', () => {
    renderPage()
    const activeLabel = screen.getByText('正在执行')
    expect(activeLabel.parentElement!.textContent).toContain('0')
    expect(screen.getByDisplayValue('状态：已完成')).toBeInTheDocument()
    expect(screen.getByText('chengdu-v1')).toBeInTheDocument()
    expect(screen.queryByText('chengdu-v2')).not.toBeInTheDocument()

    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'history' } })
    expect(screen.getByText('chengdu-v2')).toBeInTheDocument()
    expect(screen.queryByText('chengdu-v1')).not.toBeInTheDocument()
    expect(screen.getAllByText('历史未完成')).toHaveLength(2)
    expect(screen.getByText('流程停在：并行调研中')).toBeInTheDocument()
  })

  it('explains the empty default view when no project is completed', () => {
    renderPage([PROJECTS[1]])

    expect(screen.getByDisplayValue('状态：已完成')).toBeInTheDocument()
    expect(screen.getByText('当前还没有已完成的项目')).toBeInTheDocument()
  })

  it('shows a real active job and an interrupted observation distinctly', () => {
    renderPage([
      { ...PROJECTS[0], project_id: 'active-project', latest_job_status: 'running' },
      { ...PROJECTS[1], project_id: 'stale-project', observed_run_status: 'interrupted' },
    ])

    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'attention' } })
    expect(screen.getByText('运行中')).toBeInTheDocument()
    expect(screen.getByText('已中断')).toBeInTheDocument()
    expect(screen.getByText('正在执行').parentElement!.textContent).toContain('1')
  })

  it('does not mix quality findings into the project run summary', () => {
    renderPage()
    expect(screen.queryByText('存在待处理冲突')).not.toBeInTheDocument()
    expect(screen.queryByText('定点修改误改检测触发统计')).not.toBeInTheDocument()
  })

  it('filters projects by search keyword matching destination or project_id', () => {
    renderPage()
    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'all' } })
    fireEvent.change(screen.getByPlaceholderText(/按目的地或 project_id 搜索/), {
      target: { value: 'chengdu-v2' },
    })
    expect(screen.getByText('chengdu-v2')).toBeInTheDocument()
    expect(screen.queryByText('chengdu-v1')).not.toBeInTheDocument()
  })

  it('filters projects by state selector', () => {
    renderPage()
    expect(screen.getByText('chengdu-v1')).toBeInTheDocument()
    expect(screen.queryByText('chengdu-v2')).not.toBeInTheDocument()
  })

  it('keeps older projects collapsed per city until requested', () => {
    const many = Array.from({ length: 14 }, (_, i) => ({
      project_id: `p-${i}`,
      topic: '成都',
      destination: '成都',
      days: 3,
      style: null,
      budget: null,
      notes: null,
      must_visit: [],
      style_interpretation: null,
      state: '完成',
      updated_at: '2026-07-24T00:00:00Z',
      has_pending_conflicts: false,
      has_run_failure: false,
    }))
    renderPage(many)
    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'all' } })

    expect(screen.getByText('p-0')).toBeInTheDocument()
    expect(screen.queryByText('p-6')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '显示其余 8 个较早项目' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '显示其余 8 个较早项目' }))

    expect(screen.getByText('p-0')).toBeInTheDocument()
    expect(screen.getByText('p-12')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '收起较早项目' })).toBeInTheDocument()
  })

  it('surfaces a stuck-on-failure project distinctly from its stale in-progress state', () => {
    const projects: ProjectSummary[] = [
      ...PROJECTS,
      {
        project_id: 'emeishan-v1',
        topic: '峨眉山',
        destination: '峨眉山',
        days: 3,
        style: null,
        budget: null,
        notes: null,
        must_visit: [],
        style_interpretation: null,
        state: '并行调研中',
        updated_at: '2026-07-29T02:42:00Z',
        has_pending_conflicts: false,
        has_run_failure: true,
      },
    ]
    renderPage(projects)

    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'attention' } })
    expect(screen.getByText('运行失败')).toBeInTheDocument()
    expect(screen.getByText('流程停在：并行调研中')).toBeInTheDocument()

    fireEvent.change(screen.getByDisplayValue('查看：需要处理'), { target: { value: 'failed' } })
    expect(screen.getByText('emeishan-v1')).toBeInTheDocument()
    expect(screen.queryByText('chengdu-v1')).not.toBeInTheDocument()
    expect(screen.queryByText('chengdu-v2')).not.toBeInTheDocument()
  })

  it('deletes a project only after confirming the dialog', () => {
    const mutate = vi.fn()
    renderPage([{ ...PROJECTS[1], has_run_failure: true }], mutate)

    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'attention' } })
    fireEvent.click(screen.getByTitle('删除'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确定删除' }))
    expect(mutate).toHaveBeenCalledWith('chengdu-v2')
  })

  it('cancelling the delete dialog does not delete the project', () => {
    const mutate = vi.fn()
    renderPage([{ ...PROJECTS[1], has_run_failure: true }], mutate)

    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'attention' } })
    fireEvent.click(screen.getByTitle('删除'))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()
  })

  it('limits the recent-completed view to six newest projects', () => {
    const many = Array.from({ length: 8 }, (_, i) => ({
      ...PROJECTS[0],
      project_id: `done-${i}`,
      updated_at: `2026-07-${String(10 + i).padStart(2, '0')}T00:00:00Z`,
    }))
    renderPage(many)
    fireEvent.change(screen.getByDisplayValue('状态：已完成'), { target: { value: 'recent' } })
    expect(screen.getByText('done-7')).toBeInTheDocument()
    expect(screen.queryByText('done-0')).not.toBeInTheDocument()
    expect(screen.getByText(/当前显示 6 \/ 8 个项目/)).toBeInTheDocument()
  })
})
