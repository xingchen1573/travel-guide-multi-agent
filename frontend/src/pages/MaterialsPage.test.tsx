import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { MaterialsPage } from './MaterialsPage'
import * as hooks from '../api/hooks'

// MaterialsPage现在用useSearchParams/Link（"素材洞察"入口链接+接收词云点击带来的
// ?topic=&search=参数），render时需要Router上下文。initialEntries默认不带查询参数，
// 跟改动前"打开页面时URL干净"的场景保持一致。
function renderPage(
  initialEntries: string[] = ['/materials'],
  deleteOverrides: Partial<ReturnType<typeof hooks.useDeleteMaterial>> = {},
  deleteTopicOverrides: Partial<ReturnType<typeof hooks.useDeleteMaterialTopic>> = {},
) {
  vi.spyOn(hooks, 'useDeleteMaterial').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    error: null,
    ...deleteOverrides,
  } as unknown as ReturnType<typeof hooks.useDeleteMaterial>)
  vi.spyOn(hooks, 'useDeleteMaterialTopic').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    error: null,
    ...deleteTopicOverrides,
  } as unknown as ReturnType<typeof hooks.useDeleteMaterialTopic>)
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <MaterialsPage />
    </MemoryRouter>,
  )
}

function mockCollectMaterial(overrides: Partial<ReturnType<typeof hooks.useCollectMaterial>> = {}) {
  vi.spyOn(hooks, 'useCollectMaterial').mockReturnValue({
    trigger: vi.fn(),
    triggerError: null,
    isTriggering: false,
    status: undefined,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useCollectMaterial>)
}

function mockEntityStats(data: { entity: string; count: number; doc_ids: string[] }[] = []) {
  vi.spyOn(hooks, 'useMaterialEntityStats').mockReturnValue({
    data,
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.useMaterialEntityStats>)
}

describe('MaterialsPage', () => {
  it('auto-selects the first topic and renders its entries', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [
        { topic: '成都', entry_count: 25, core_count: 25, chunk_count: 148, total_word_count: 0 },
        { topic: '测试城市', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [
        { doc_id: '成都-000', platform: 'bilibili', title: '示例标题', is_core: true, entities: ['武侯祠'] },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage()

    expect(screen.getByText('示例标题')).toBeInTheDocument()
    // entry_count和core_count都是25，用getAllByText确认两个统计卡都渲染了这个数字
    expect(screen.getAllByText('25')).toHaveLength(2)
    expect(screen.getByText('148')).toBeInTheDocument()
  })

  it('shows an empty-state row when the selected topic has no entries', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '测试城市', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage()

    expect(screen.getByText('没有素材条目。')).toBeInTheDocument()
  })

  it('opens the entry detail modal with chunk text and raw text when a row is clicked', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 1, core_count: 1, chunk_count: 2, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [{ doc_id: '成都-000', platform: 'bilibili', title: '示例标题', is_core: true, entities: [] }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()
    vi.spyOn(hooks, 'useMaterialEntryDetail').mockReturnValue({
      data: {
        entry: { doc_id: '成都-000', title: '示例标题', platform: 'bilibili', is_core: true, entities: [] },
        chunks: [{ chunk_id: '成都-000-c000', doc_id: '成都-000', order: 0, text: '第一段chunk文本' }],
        raw_text: '完整原文内容',
      },
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntryDetail>)

    renderPage()

    fireEvent.click(screen.getByText('示例标题'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('第一段chunk文本')).toBeInTheDocument()
    expect(screen.getByText('完整原文内容')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('关闭'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('moves a material to trash only after confirmation', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 1, core_count: 1, chunk_count: 1, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [{ doc_id: '成都-000', platform: 'bilibili', title: '示例标题', is_core: true, entities: [] }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()
    const mutate = vi.fn()

    renderPage(['/materials'], { mutate })

    fireEvent.click(screen.getByRole('button', { name: '将素材移入回收站：示例标题' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '移入回收站' }))
    expect(mutate).toHaveBeenCalledWith('成都-000')
  })

  it('moves an unreferenced material space to trash only after confirmation', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{
        topic: '测试城市',
        entry_count: 2,
        core_count: 1,
        chunk_count: 4,
        total_word_count: 0,
        active_project_ids: [],
        trashed_project_ids: ['deleted-project'],
      }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()
    const mutate = vi.fn()

    renderPage(['/materials'], {}, { mutate })

    fireEvent.click(screen.getByRole('button', { name: '将素材空间移入回收站：测试城市' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('2 条素材、4 个切片')
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '回收素材空间' }))
    expect(mutate).toHaveBeenCalledWith('测试城市', expect.objectContaining({ onSuccess: expect.any(Function) }))
  })

  it('blocks material-space deletion while an active Project references it', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{
        topic: '成都',
        entry_count: 2,
        core_count: 1,
        chunk_count: 4,
        total_word_count: 0,
        active_project_ids: ['chengdu-active'],
        trashed_project_ids: [],
      }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({ data: [], isLoading: false } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage()

    const deleteButton = screen.getByRole('button', { name: '将素材空间移入回收站：成都' })
    expect(deleteButton).toBeDisabled()
    expect(deleteButton).toHaveAttribute('title', '仍有 1 个正常 Project 使用，不能删除')
    expect(screen.getByText('需先删除或改绑正常 Project，才能回收此空间')).toBeInTheDocument()
  })

  it('does not trigger collection until the confirm dialog is confirmed', async () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    const trigger = vi.fn()
    mockCollectMaterial({ trigger })
    mockEntityStats()

    renderPage()

    await waitFor(() => expect(screen.getByText('触发采集')).toBeInTheDocument())
    fireEvent.click(screen.getByText('触发采集'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(trigger).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('开始采集'))
    expect(trigger).toHaveBeenCalledTimes(1)
  })

  it('disables the collect trigger button while a collection is running', async () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial({ status: { task_type: 'collect_material', key: '成都', status: 'running', error: null, started_at: 't0', finished_at: null, log: [] } })
    mockEntityStats()

    renderPage()

    await waitFor(() => expect(screen.getByText('采集中…')).toBeInTheDocument())
    expect(screen.getByText('采集中…')).toBeDisabled()
  })

  it('opens the full entity list modal when clicking the "+N more" button', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 1, core_count: 1, chunk_count: 1, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [
        {
          doc_id: '成都-000',
          platform: 'bilibili',
          title: '示例标题',
          is_core: true,
          entities: ['太古里', 'IFS', '百盛购物中心', '寅嘉广场', '春熙路', '锦里'],
        },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage()

    fireEvent.click(screen.getByText('+2'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('锦里')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('关闭'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('filters entries by search keyword matching title or entities', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 2, core_count: 2, chunk_count: 2, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [
        { doc_id: 'a', platform: 'bilibili', title: '武侯祠攻略', is_core: true, entities: ['武侯祠'] },
        { doc_id: 'b', platform: 'douyin', title: '春熙路探店', is_core: false, entities: ['春熙路'] },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage()

    expect(screen.getByText('武侯祠攻略')).toBeInTheDocument()
    expect(screen.getByText('春熙路探店')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('按标题或提及实体搜索…'), { target: { value: '武侯祠' } })

    expect(screen.getByText('武侯祠攻略')).toBeInTheDocument()
    expect(screen.queryByText('春熙路探店')).not.toBeInTheDocument()
  })

  it('filters entries by is_core selector', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 2, core_count: 1, chunk_count: 2, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [
        { doc_id: 'a', platform: 'bilibili', title: '核心素材条目', is_core: true, entities: [] },
        { doc_id: 'b', platform: 'douyin', title: '非核心素材条目', is_core: false, entities: [] },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage()

    fireEvent.change(screen.getByDisplayValue('核心素材：全部'), { target: { value: 'core' } })

    expect(screen.getByText('核心素材条目')).toBeInTheDocument()
    expect(screen.queryByText('非核心素材条目')).not.toBeInTheDocument()
  })

  it('paginates entries beyond the first page', () => {
    const entries = Array.from({ length: 15 }, (_, i) => ({
      doc_id: `doc-${i}`,
      platform: 'bilibili',
      title: `素材条目${i}`,
      is_core: true,
      entities: [],
    }))
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 15, core_count: 15, chunk_count: 15, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: entries,
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage()

    expect(screen.getByText('素材条目0')).toBeInTheDocument()
    expect(screen.queryByText('素材条目10')).not.toBeInTheDocument()
    expect(screen.getByText('共 15 条，第 1/2 页')).toBeInTheDocument()

    fireEvent.click(screen.getByText('下一页'))

    expect(screen.queryByText('素材条目0')).not.toBeInTheDocument()
    expect(screen.getByText('素材条目10')).toBeInTheDocument()
  })

  it('decorates entity tags in the table with the全库mention count', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 1, core_count: 1, chunk_count: 1, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [{ doc_id: '成都-000', platform: 'bilibili', title: '示例标题', is_core: true, entities: ['武侯祠'] }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats([
      { entity: '武侯祠', count: 12, doc_ids: ['成都-000'] },
      { entity: '锦里', count: 3, doc_ids: ['成都-001'] },
    ])

    renderPage()

    // 表格里的实体标签应该带上跨全库次数（"×12"）——真正的频次榜/词云可视化
    // 已经移到独立的"素材洞察"页面（见MaterialInsightsPage.test.tsx），这里
    // 只保留表格内联的快速参考。
    expect(screen.getByText('×12')).toBeInTheDocument()
  })

  it('links to the "素材洞察" page with the current topic in the query string', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 1, core_count: 1, chunk_count: 1, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats([{ entity: '武侯祠', count: 12, doc_ids: ['成都-000'] }])

    renderPage()

    const link = screen.getByText('素材洞察 →')
    expect(link.closest('a')).toHaveAttribute('href', '/materials/insights?topic=%E6%88%90%E9%83%BD')
  })

  it('applies ?topic=&search= query params from an incoming word-cloud click on first load', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [
        { topic: '成都', entry_count: 2, core_count: 2, chunk_count: 2, total_word_count: 0 },
        { topic: '测试城市', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [
        { doc_id: 'a', platform: 'bilibili', title: '武侯祠攻略', is_core: true, entities: ['武侯祠'] },
        { doc_id: 'b', platform: 'douyin', title: '春熙路探店', is_core: false, entities: ['春熙路'] },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockCollectMaterial()
    mockEntityStats()

    renderPage(['/materials?topic=成都&search=武侯祠'])

    expect(screen.getByPlaceholderText('按标题或提及实体搜索…')).toHaveValue('武侯祠')
    expect(screen.getByText('武侯祠攻略')).toBeInTheDocument()
    expect(screen.queryByText('春熙路探店')).not.toBeInTheDocument()
  })

  it('lets the user pick a platform in the collect dialog and passes it to trigger', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    const trigger = vi.fn()
    mockCollectMaterial({ trigger })
    mockEntityStats()

    renderPage()

    fireEvent.click(screen.getByText('触发采集'))
    fireEvent.change(screen.getByDisplayValue('全部（B站+抖音）'), { target: { value: 'douyin' } })
    fireEvent.click(screen.getByText('开始采集'))

    expect(trigger).toHaveBeenCalledTimes(1)
    expect(trigger.mock.calls[0][1]).toBe('douyin')
  })

  it('renders the granular collection progress log while a collection is running', () => {
    vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
      data: [{ topic: '成都', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 }],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
    vi.spyOn(hooks, 'useMaterialEntries').mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useMaterialEntries>)
    mockEntityStats()
    mockCollectMaterial({
      status: {
        task_type: 'collect_material',
        key: '成都',
        status: 'running',
        error: null,
        started_at: 't0',
        finished_at: null,
        log: ['第1轮：向核心素材agent发起搜索请求（目的地=成都，平台=all）...', 'agent提交候选3条（B站2条/抖音1条）：xxx'],
      },
    })

    renderPage()

    expect(screen.getByText(/第1轮：向核心素材agent发起搜索请求/)).toBeInTheDocument()
    expect(screen.getByText(/agent提交候选3条/)).toBeInTheDocument()
  })
})
