import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MaterialInsightsPage } from './MaterialInsightsPage'
import * as hooks from '../api/hooks'
import type { EntityStat, MaterialStats, MaterialTopicSummary, TopicInsights } from '../api/types'

// jsdom不实现ResizeObserver、也没有真实的canvas 2d context（layoutWordCloud已经有
// 文本宽度估算的兜底分支，不强依赖真实canvas测量结果，见MaterialInsightsPage.tsx）。
// 这里用一个不做任何事的stub替代ResizeObserver，并把clientWidth固定成一个够大的值，
// 让词云布局算法在测试环境里也能真正跑起来、把词放进容器（而不是因为width=0直接跳过）。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// 每个测试都要mock这两个新hook，否则组件里的useMaterialInsights/useRegenerateInsights
// 调用会因为没有真实QueryClientProvider而报错（这个页面测试历来都是mock掉hooks层，
// 不套真实的react-query Provider，见下方mockTopics/mockStats的既有模式）。
function mockInsights(data: TopicInsights | null = null, isLoading = false) {
  vi.spyOn(hooks, 'useMaterialInsights').mockReturnValue({
    data,
    isLoading,
  } as unknown as ReturnType<typeof hooks.useMaterialInsights>)
  vi.spyOn(hooks, 'useRegenerateInsights').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useRegenerateInsights>)
}

// 素材库统计画像（2026-08-22新增）同理必须mock，否则useMaterialStats没有真实
// QueryClientProvider会报错。默认给一份0条的空结构，让本文件里此前就存在的、
// 不关心这块内容的测试不需要逐个改动。
const EMPTY_MATERIAL_STATS: MaterialStats = {
  topic: '成都',
  entry_count: 0,
  total_word_count: 0,
  platform_breakdown: [],
  age_distribution: [],
  core_ratio: { core_count: 0, non_core_count: 0 },
  author_ranking: { top: [], other_author_count: 0, other_entry_count: 0 },
  rejection_stats: { too_short_count: 0, irrelevant_count: 0, irrelevant_reasons: [] },
}

function mockMaterialStats(data: MaterialStats | undefined, isLoading = false) {
  vi.spyOn(hooks, 'useMaterialStats').mockReturnValue({
    data,
    isLoading,
  } as unknown as ReturnType<typeof hooks.useMaterialStats>)
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 900 })
  mockInsights()
  mockMaterialStats(EMPTY_MATERIAL_STATS)
})

/** 跳转目标页的桩组件：只回显收到的查询参数，验证"点击词云/榜单条目 -> 导航到
 * /materials并带上正确的topic/search"这条真实的跨页面行为，不去mock useNavigate
 * 本身（mock掉的话测的只是"调用过mock函数"，测不出query string到底拼对没拼对）。*/
function MaterialsDestinationStub() {
  const [params] = useSearchParams()
  return (
    <p>
      跳转目标：topic={params.get('topic')}，search={params.get('search')}
    </p>
  )
}

function renderPage(initialEntries: string[] = ['/materials/insights']) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route path="/materials/insights" element={<MaterialInsightsPage />} />
        <Route path="/materials" element={<MaterialsDestinationStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

function mockTopics(data: MaterialTopicSummary[]) {
  vi.spyOn(hooks, 'useMaterialTopics').mockReturnValue({
    data,
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.useMaterialTopics>)
}

function mockStats(data: EntityStat[], isLoading = false) {
  vi.spyOn(hooks, 'useMaterialEntityStats').mockReturnValue({
    data,
    isLoading,
  } as unknown as ReturnType<typeof hooks.useMaterialEntityStats>)
}

describe('MaterialInsightsPage', () => {
  it('renders the page header and topic tabs', () => {
    mockTopics([
      { topic: '成都', entry_count: 5, core_count: 5, chunk_count: 10, total_word_count: 0 },
      { topic: '测试城市', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 },
    ])
    mockStats([])

    renderPage()

    expect(screen.getByText('素材洞察')).toBeInTheDocument()
    expect(screen.getByText('热门实体词云')).toBeInTheDocument()
  })

  it('shows a loading state while entity stats are loading', () => {
    mockTopics([{ topic: '成都', entry_count: 5, core_count: 5, chunk_count: 10, total_word_count: 0 }])
    mockStats([], true)

    renderPage()

    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('shows an empty state when the topic has no entity stats yet', () => {
    mockTopics([{ topic: '测试城市', entry_count: 0, core_count: 0, chunk_count: 0, total_word_count: 0 }])
    mockStats([])

    renderPage()

    expect(screen.getByText('暂无实体数据。')).toBeInTheDocument()
    expect(screen.getByText('没有匹配的实体。')).toBeInTheDocument()
  })

  it('restores the topic from the URL and keeps it on the return link', () => {
    mockTopics([
      { topic: '大理', entry_count: 3, core_count: 2, chunk_count: 6, total_word_count: 0 },
      { topic: '成都', entry_count: 5, core_count: 5, chunk_count: 10, total_word_count: 0 },
    ])
    mockStats([])

    renderPage(['/materials/insights?topic=%E6%88%90%E9%83%BD'])

    expect(hooks.useMaterialEntityStats).toHaveBeenLastCalledWith('成都')
    expect(screen.getByRole('link', { name: '返回素材库' })).toHaveAttribute('href', '/materials?topic=%E6%88%90%E9%83%BD')
  })

  it('places top entities as clickable word-cloud buttons and navigates to /materials on click', () => {
    mockTopics([{ topic: '成都', entry_count: 5, core_count: 5, chunk_count: 10, total_word_count: 0 }])
    mockStats([
      { entity: '武侯祠', count: 12, doc_ids: ['成都-000'] },
      { entity: '锦里', count: 3, doc_ids: ['成都-001'] },
    ])

    renderPage()

    const wordButton = screen.getByTitle('出现在 12 条素材中，点击查看相关素材')
    expect(wordButton).toHaveTextContent('武侯祠')

    fireEvent.click(wordButton)

    expect(screen.getByText('跳转目标：topic=成都，search=武侯祠')).toBeInTheDocument()
  })

  it('renders the full ranked list with rank badges and lets clicking a row navigate too', () => {
    mockTopics([{ topic: '成都', entry_count: 5, core_count: 5, chunk_count: 10, total_word_count: 0 }])
    mockStats([
      { entity: '武侯祠', count: 12, doc_ids: ['成都-000'] },
      { entity: '锦里', count: 3, doc_ids: ['成都-001'] },
    ])

    renderPage()

    expect(screen.getByText('完整频次榜（共 2 个实体）')).toBeInTheDocument()
    expect(screen.getByText('出现在 12 条素材中')).toBeInTheDocument()
    expect(screen.getByText('出现在 3 条素材中')).toBeInTheDocument()

    fireEvent.click(screen.getByText('出现在 3 条素材中'))

    expect(screen.getByText('跳转目标：topic=成都，search=锦里')).toBeInTheDocument()
  })

  it('filters the ranked list by keyword without affecting the word cloud', () => {
    mockTopics([{ topic: '成都', entry_count: 5, core_count: 5, chunk_count: 10, total_word_count: 0 }])
    mockStats([
      { entity: '武侯祠', count: 12, doc_ids: ['成都-000'] },
      { entity: '锦里', count: 3, doc_ids: ['成都-001'] },
    ])

    renderPage()

    fireEvent.change(screen.getByPlaceholderText('筛选实体…'), { target: { value: '锦里' } })

    expect(screen.queryByText('出现在 12 条素材中')).not.toBeInTheDocument()
    expect(screen.getByText('出现在 3 条素材中')).toBeInTheDocument()
    // 词云本身不受这个筛选框影响——它只筛下方的榜单列表
    expect(screen.getByTitle('出现在 12 条素材中，点击查看相关素材')).toBeInTheDocument()
  })

  it('switches topic and reloads entity stats for the newly selected topic', () => {
    mockTopics([
      { topic: '成都', entry_count: 5, core_count: 5, chunk_count: 10, total_word_count: 0 },
      { topic: '重庆', entry_count: 3, core_count: 3, chunk_count: 6, total_word_count: 0 },
    ])
    const spy = vi.spyOn(hooks, 'useMaterialEntityStats')
    spy.mockImplementation((topic: string) =>
      ({
        data: topic === '成都' ? [{ entity: '武侯祠', count: 12, doc_ids: ['成都-000'] }] : [{ entity: '洪崖洞', count: 8, doc_ids: ['重庆-000'] }],
        isLoading: false,
      }) as unknown as ReturnType<typeof hooks.useMaterialEntityStats>,
    )

    renderPage()

    expect(screen.getByText('出现在 12 条素材中')).toBeInTheDocument()

    fireEvent.click(screen.getByText('重庆', { exact: false }))

    expect(screen.getByText('出现在 8 条素材中')).toBeInTheDocument()
    expect(screen.queryByText('出现在 12 条素材中')).not.toBeInTheDocument()
  })

  it('shows insufficient-material guidance when entry_count is below the insights threshold', () => {
    mockTopics([{ topic: '峨眉山', entry_count: 1, core_count: 1, chunk_count: 1, total_word_count: 0 }])
    mockStats([])
    mockInsights(null)

    renderPage()

    expect(screen.getByText(/低于生成洞察所需的最少 5 条/)).toBeInTheDocument()
  })

  it('prompts to generate insights when entries are enough but insights.json does not exist yet', () => {
    mockTopics([{ topic: '成都', entry_count: 53, core_count: 53, chunk_count: 274, total_word_count: 0 }])
    mockStats([])
    mockInsights(null)

    renderPage()

    expect(screen.getByText('还没有生成过洞察，点击右上角"重新生成洞察"试试。')).toBeInTheDocument()
  })

  it('renders the 5 category cards with confidence badges and expandable sources', () => {
    mockTopics([{ topic: '成都', entry_count: 53, core_count: 53, chunk_count: 274, total_word_count: 0 }])
    mockStats([])
    mockInsights({
      topic: '成都',
      generated_at: '2026-08-01T00:00:00+00:00',
      model: 'sensenova-6.7-flash-lite',
      based_on_entry_count: 53,
      must_visit: [
        {
          summary: '武侯祠是多条素材反复提到的必去景点',
          confidence: 'consensus',
          sources: [
            { doc_id: '成都-000', chunk_id: '成都-000-c000', author: '了in林', excerpt: '武侯祠真的值得一去' },
            { doc_id: '成都-001', chunk_id: '成都-001-c000', author: '丹牛食走起', excerpt: '推荐武侯祠' },
          ],
        },
      ],
      food: [
        {
          summary: '某小店的冒菜只有一条素材提到',
          confidence: 'single_source',
          sources: [{ doc_id: '成都-002', chunk_id: '成都-002-c000', author: '猴儿甜猴儿甜', excerpt: '这家冒菜绝了' }],
        },
      ],
      pitfalls: [],
      tips: [],
      other: [],
    })

    renderPage()

    expect(screen.getByText('大模型素材洞察')).toBeInTheDocument()
    expect(screen.getByText('热门必去')).toBeInTheDocument()
    expect(screen.getByText('武侯祠是多条素材反复提到的必去景点')).toBeInTheDocument()
    expect(screen.getByText('多方印证')).toBeInTheDocument()
    expect(screen.getByText('仅单一来源，谨慎参考')).toBeInTheDocument()
    // 避雷/贴士/其他都没有内容时应展示"暂无发现"骨架，而不是隐藏卡片
    expect(screen.getAllByText('暂无发现。').length).toBe(3)

    fireEvent.click(screen.getByText('查看原文依据（2条）▼'))
    expect(screen.getByText('武侯祠真的值得一去')).toBeInTheDocument()
    expect(screen.getByText('了in林')).toBeInTheDocument()
  })
})

describe('material stats section', () => {
  const FULL_STATS: MaterialStats = {
    topic: '成都',
    entry_count: 3,
    total_word_count: 35,
    platform_breakdown: [
      {
        platform: 'bilibili', count: 2, word_count: 30, percentage: 85.7,
        longest: { doc_id: '成都-001', title: '长文', word_count: 20 },
      },
      {
        platform: 'douyin', count: 1, word_count: 5, percentage: 14.3,
        longest: { doc_id: '成都-002', title: '短文', word_count: 5 },
      },
    ],
    age_distribution: [
      { bucket: '近3个月', count: 1, by_platform: { bilibili: 1 } },
      { bucket: '近半年', count: 0, by_platform: {} },
      { bucket: '近1年', count: 0, by_platform: {} },
      { bucket: '1年以上', count: 0, by_platform: {} },
      { bucket: '未知', count: 2, by_platform: { xiaohongshu: 1, bilibili: 1 } },
    ],
    core_ratio: { core_count: 2, non_core_count: 1 },
    author_ranking: {
      top: [
        { author: '牛油果の日常', count: 2 },
        { author: '青林知青', count: 1 },
      ],
      other_author_count: 2,
      other_entry_count: 2,
    },
    rejection_stats: {
      too_short_count: 1,
      irrelevant_count: 2,
      irrelevant_reasons: [
        { source_file: 'y.md', title: '跑题了', reason: '文本主要讲松潘而非成都' },
        { source_file: 'z.md', title: '也跑题', reason: '未提及成都，可能是其他城市景点' },
      ],
    },
  }

  function setup(stats: MaterialStats | undefined, isLoading = false) {
    mockTopics([{ topic: '成都', entry_count: 3, core_count: 2, chunk_count: 3, total_word_count: 35 }])
    mockStats([])
    mockMaterialStats(stats, isLoading)
    renderPage()
  }

  it('renders platform breakdown with total word count and the longest article per platform', () => {
    setup(FULL_STATS)

    expect(screen.getByText('平台分布 · 共35字')).toBeInTheDocument()
    expect(screen.getByText(/最长一篇《长文》20字/)).toBeInTheDocument()
    expect(screen.getByText(/最长一篇《短文》5字/)).toBeInTheDocument()
    // platform是ingest管线内部值，不应该直接把英文原样露给用户，见与用户的反馈
    expect(screen.getAllByText('B站').length).toBeGreaterThan(0)
    expect(screen.getAllByText('抖音').length).toBeGreaterThan(0)
    expect(screen.queryByText('bilibili')).not.toBeInTheDocument()
    expect(screen.queryByText('douyin')).not.toBeInTheDocument()
  })

  it('breaks the "unknown" age bucket down by platform instead of showing one bare number', () => {
    setup(FULL_STATS)

    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName.toLowerCase() === 'p' &&
          !!element.textContent?.includes('按平台拆分') &&
          element.textContent.includes('小红书 1条') &&
          element.textContent.includes('B站 1条'),
      ),
    ).toBeInTheDocument()
  })

  it('shows the overflow note for authors beyond the top ranking', () => {
    setup(FULL_STATS)

    expect(screen.getByText(/其余 2 位作者共贡献 2 条/)).toBeInTheDocument()
  })

  it('shows the too-short/irrelevant rejection split and expands to the raw reasons', () => {
    setup(FULL_STATS)

    expect(screen.getByText('内容过短')).toBeInTheDocument()
    expect(screen.getByText('内容不相关')).toBeInTheDocument()
    expect(screen.queryByText('文本主要讲松潘而非成都')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('查看不相关理由（2条）▼'))

    expect(screen.getByText('文本主要讲松潘而非成都')).toBeInTheDocument()
    expect(screen.getByText('未提及成都，可能是其他城市景点')).toBeInTheDocument()
  })

  it('shows a loading state for the stats section independently from entity stats', () => {
    setup(undefined, true)

    // 此时entity stats/insights都不是loading（mockStats/mockInsights默认isLoading=false），
    // 唯一的"加载中…"只能来自统计画像区块本身，验证它没被其它loading分支挡住。
    expect(screen.getByText('加载中…')).toBeInTheDocument()
    expect(screen.queryByText('暂无素材，无法生成统计画像。')).not.toBeInTheDocument()
  })

  it('shows an empty-state message when the topic has no materials for stats', () => {
    setup(EMPTY_MATERIAL_STATS)

    expect(screen.getByText('暂无素材，无法生成统计画像。')).toBeInTheDocument()
  })
})
