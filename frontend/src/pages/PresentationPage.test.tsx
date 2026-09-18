import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { PresentationPage } from './PresentationPage'
import * as hooks from '../api/hooks'

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/projects/chengdu-v1/presentation']}>
      <Routes>
        <Route path="/projects/:projectId/presentation" element={<PresentationPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function mockSite(overrides: Partial<ReturnType<typeof hooks.useGenerateSite>> = {}) {
  vi.spyOn(hooks, 'useGenerateSite').mockReturnValue({
    trigger: vi.fn(),
    triggerError: null,
    isTriggering: false,
    status: undefined,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useGenerateSite>)
}

function mockPoster(overrides: Partial<ReturnType<typeof hooks.useGeneratePoster>> = {}) {
  vi.spyOn(hooks, 'useGeneratePoster').mockReturnValue({
    trigger: vi.fn(),
    triggerError: null,
    isTriggering: false,
    status: undefined,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useGeneratePoster>)
}

function mockSiteHistory(data: string[] = []) {
  vi.spyOn(hooks, 'useSiteHistory').mockReturnValue({
    data,
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.useSiteHistory>)
}

function mockPosterHistory(data: string[] = []) {
  vi.spyOn(hooks, 'usePosterHistory').mockReturnValue({
    data,
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.usePosterHistory>)
}

function mockPosterPrompt(prompt: string | undefined = undefined) {
  vi.spyOn(hooks, 'usePosterPrompt').mockReturnValue({
    data: prompt ? { prompt } : undefined,
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.usePosterPrompt>)
}

function mockPosterHistoryPrompt(prompt: string | undefined = undefined) {
  vi.spyOn(hooks, 'usePosterHistoryPrompt').mockReturnValue({
    data: prompt ? { prompt } : undefined,
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.usePosterHistoryPrompt>)
}

function mockProject(days: number | null = 3) {
  vi.spyOn(hooks, 'useProject').mockReturnValue({
    data: { days },
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.useProject>)
}

function mockDefaults(days: number | null = 3) {
  mockProject(days)
  mockSiteHistory()
  mockPosterHistory()
  mockPosterPrompt()
  mockPosterHistoryPrompt()
}

describe('PresentationPage', () => {
  it('uses the project trip length for overall and daily poster controls', () => {
    mockSite()
    mockPoster()
    mockDefaults(3)

    renderPage()

    expect(screen.getByRole('tab', { name: '3天整体' })).toBeInTheDocument()
    expect(screen.getByText(/^生成一张包含完整路线/)).toHaveTextContent(
      '生成一张包含完整路线与整体节奏的3天总览海报。',
    )

    fireEvent.click(screen.getByRole('tab', { name: '单日海报' }))
    const selector = screen.getByLabelText('选择天数')
    expect(selector.querySelectorAll('option')).toHaveLength(3)
    expect(screen.queryByRole('option', { name: 'Day4' })).not.toBeInTheDocument()
  })

  it('falls back to a neutral overall label when the project trip length is unavailable', () => {
    mockSite()
    mockPoster()
    mockDefaults(null)

    renderPage()

    expect(screen.getByRole('tab', { name: '整体海报' })).toBeInTheDocument()
    expect(screen.queryByText(/8天/)).not.toBeInTheDocument()
  })

  it('triggers generate_site directly without a confirm dialog', () => {
    const trigger = vi.fn()
    mockSite({ trigger })
    mockPoster()
    mockDefaults()

    renderPage()

    fireEvent.click(screen.getByText('生成网站'))
    expect(trigger).toHaveBeenCalledTimes(1)
  })

  it('shows an embedded preview and open-in-new-tab link once site generation succeeds', () => {
    mockSite({
      status: { task_type: 'generate_site', key: 'chengdu-v1', status: 'succeeded', error: null, started_at: 't0', finished_at: 't1', log: [] },
    })
    mockPoster()
    mockDefaults()

    renderPage()

    const links = screen.getAllByText('在新标签页打开') as HTMLAnchorElement[]
    expect(links[0].getAttribute('href')).toBe('/api/projects/chengdu-v1/site')
    expect(screen.getByTitle('静态展示网站预览')).toBeInTheDocument()
  })

  it('renders generation log lines when present', () => {
    mockSite({
      status: {
        task_type: 'generate_site',
        key: 'chengdu-v1',
        status: 'succeeded',
        error: null,
        started_at: 't0',
        finished_at: 't1',
        log: ['已归档旧版本网站到 history/site/xxx/', '开始生成网站...', '网站已生成: /path/site.html'],
      },
    })
    mockPoster()
    mockDefaults()

    renderPage()

    expect(screen.getByText('开始生成网站...')).toBeInTheDocument()
  })

  it('renders site version history buttons and switches preview src on selection', () => {
    mockSite({
      status: { task_type: 'generate_site', key: 'chengdu-v1', status: 'succeeded', error: null, started_at: 't0', finished_at: 't1', log: [] },
    })
    mockPoster()
    mockSiteHistory(['20260726T120000000000'])
    mockPosterHistory()
    mockPosterPrompt()
    mockPosterHistoryPrompt()

    renderPage()

    expect(screen.getByText('历史版本（共 1 个）')).toBeInTheDocument()
    const historyButton = screen.getByText('2026-07-26 12:00:00')
    fireEvent.click(historyButton)

    expect(screen.getByTitle('静态展示网站预览')).toHaveAttribute(
      'src',
      '/api/projects/chengdu-v1/site/history/20260726T120000000000',
    )
  })

  it('does not trigger generate_poster until the confirm dialog is confirmed (paid API call)', () => {
    const trigger = vi.fn()
    mockSite()
    mockPoster({ trigger })
    mockDefaults()

    renderPage()

    fireEvent.click(screen.getByText('生成海报'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(trigger).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('确定生成'))
    expect(trigger).toHaveBeenCalledTimes(1)
  })

  it('shows error message when poster generation fails', () => {
    mockSite()
    mockPoster({ status: { task_type: 'generate_poster', key: 'chengdu-v1', status: 'failed', error: '生图API 504超时', started_at: 't0', finished_at: 't1', log: [] } })
    mockDefaults()

    renderPage()

    expect(screen.getByText('生图API 504超时')).toBeInTheDocument()
  })

  it('shows an embedded poster image once generation succeeds', () => {
    mockSite()
    mockPoster({
      status: { task_type: 'generate_poster', key: 'chengdu-v1', status: 'succeeded', error: null, started_at: 't0', finished_at: 't1', log: [] },
    })
    mockDefaults()

    renderPage()

    const img = screen.getByAltText('概览海报预览') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('/api/projects/chengdu-v1/poster')
  })

  it('reveals the poster_prompt.txt audit content behind a disclosure toggle', () => {
    mockSite()
    mockPoster({
      status: { task_type: 'generate_poster', key: 'chengdu-v1', status: 'succeeded', error: null, started_at: 't0', finished_at: 't1', log: [] },
    })
    mockSiteHistory()
    mockPosterHistory()
    mockPosterPrompt('这是喂给生图模型的真实prompt内容')
    mockPosterHistoryPrompt()

    renderPage()

    expect(screen.queryByText('这是喂给生图模型的真实prompt内容')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('查看实际喂给生图模型的prompt（审计存档）'))
    expect(screen.getByText('这是喂给生图模型的真实prompt内容')).toBeInTheDocument()
  })
})
