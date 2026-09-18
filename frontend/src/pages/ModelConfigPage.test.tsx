import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ModelConfigPage } from './ModelConfigPage'
import * as hooks from '../api/hooks'
import type {
  FixedModelGroupStatus,
  KeyPoolStatus,
  ModelGroupStatus,
  PrimaryTravelModelStatus,
} from '../api/types'

function makeGroup(overrides: Partial<ModelGroupStatus> = {}): ModelGroupStatus {
  return {
    group: 'full',
    label: '通用 full 渠道',
    usage: '核心素材 Agent、部分核验与显式 full 兼容调用',
    default_model: 'sensenova-v1',
    effective_model: 'sensenova-v1',
    override_active: false,
    key_count: 2,
    current_key_index: 0,
    keys: [
      { index: 0, masked: 'sk-a...bcd', is_current: true },
      { index: 1, masked: 'sk-e...fgh', is_current: false },
    ],
    ...overrides,
  }
}

function makeMimoGroup(overrides: Partial<FixedModelGroupStatus> = {}): FixedModelGroupStatus {
  return {
    group: 'mimo',
    label: '陪伴问答兼容渠道（历史 mimo 分组）',
    usage: '陪伴问答主用，非当前长攻略主链',
    default_model: 'mimo-v2.5',
    available_models: ['mimo-v2.5', 'mimo-v2.5-pro'],
    key_count: 0,
    current_key_index: null,
    keys: [],
    ...overrides,
  }
}

function makeCunaiGroup(overrides: Partial<FixedModelGroupStatus> = {}): FixedModelGroupStatus {
  return {
    group: 'cunai',
    label: 'CunAI直连渠道',
    usage: '定向复核可选兼容渠道，非当前长攻略主链',
    default_model: 'deepseek-v4-flash-0731',
    available_models: ['deepseek-v4-flash-0731'],
    key_count: 0,
    current_key_index: null,
    keys: [],
    ...overrides,
  }
}

function makePrimaryTravel(overrides: Partial<PrimaryTravelModelStatus> = {}): PrimaryTravelModelStatus {
  return {
    label: '当前长攻略与结构化核验主链',
    provider_group: 'gptload',
    provider: 'GPT-Load',
    base_url: 'https://gateway.example/gptload/v1',
    model: 'glm-current',
    usage: '路线、衣食住行、避雷、POI详情、文化、最终整合及结构化核验',
    configured: true,
    key_count: 1,
    ...overrides,
  }
}

function makeKeyPool(overrides: Partial<KeyPoolStatus> = {}): KeyPoolStatus {
  return {
    group: 'tavily',
    label: 'Tavily 搜索',
    usage: 'realtime_agent联网查询',
    key_count: 0,
    current_key_index: null,
    keys: [],
    ...overrides,
  }
}

function mockModelConfig(overrides: {
  primary_travel?: PrimaryTravelModelStatus
  full?: ModelGroupStatus
  light?: ModelGroupStatus
  mimo?: FixedModelGroupStatus
  cunai?: FixedModelGroupStatus
  tavily?: KeyPoolStatus
  poster?: KeyPoolStatus
  other_services?: unknown[]
} = {}) {
  vi.spyOn(hooks, 'useModelConfig').mockReturnValue({
    data: {
      primary_travel: overrides.primary_travel ?? makePrimaryTravel(),
      groups: {
        full: overrides.full ?? makeGroup(),
        light: overrides.light ?? makeGroup({ group: 'light' }),
        cunai: overrides.cunai ?? makeCunaiGroup(),
        mimo: overrides.mimo ?? makeMimoGroup(),
      },
      key_pools: {
        tavily: overrides.tavily ?? makeKeyPool({ group: 'tavily' }),
        poster: overrides.poster ?? makeKeyPool({ group: 'poster', label: '生图', usage: '按次收费' }),
      },
      other_services: overrides.other_services ?? [],
    },
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useModelConfig>)
}

function mockTestModelKeys(overrides: Partial<ReturnType<typeof hooks.useTestModelKeys>> = {}) {
  vi.spyOn(hooks, 'useTestModelKeys').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useTestModelKeys>)
}

function mockSetModel(overrides: Partial<ReturnType<typeof hooks.useSetModel>> = {}) {
  vi.spyOn(hooks, 'useSetModel').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useSetModel>)
}

function mockAddModelKey(overrides: Partial<ReturnType<typeof hooks.useAddModelKey>> = {}) {
  vi.spyOn(hooks, 'useAddModelKey').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useAddModelKey>)
}

function mockDeleteModelKey(overrides: Partial<ReturnType<typeof hooks.useDeleteModelKey>> = {}) {
  vi.spyOn(hooks, 'useDeleteModelKey').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useDeleteModelKey>)
}

function setupDefaultMocks() {
  mockSetModel()
  mockTestModelKeys()
  mockAddModelKey()
  mockDeleteModelKey()
}

describe('ModelConfigPage', () => {
  it('shows the actual primary travel provider, model, and URL above legacy key groups', () => {
    mockModelConfig()
    setupDefaultMocks()

    render(<ModelConfigPage />)

    expect(screen.getByText('当前长攻略与结构化核验主链')).toBeInTheDocument()
    expect(screen.getByText('GPT-Load')).toBeInTheDocument()
    expect(screen.getByText('glm-current')).toBeInTheDocument()
    expect(screen.getByText('https://gateway.example/gptload/v1')).toBeInTheDocument()
    expect(screen.queryByText('长攻略主链；不可用后才切换到MiMo。')).not.toBeInTheDocument()
  })

  it('renders full/light/mimo groups, key pools, and other services keys', () => {
    mockModelConfig({
      full: makeGroup(),
      light: makeGroup({
        group: 'light',
        label: '分类/抽取（轻量模型）',
        default_model: 'sensenova-lite',
        effective_model: 'sensenova-lite',
        key_count: 1,
        keys: [{ index: 0, masked: 'sk-l...ite', is_current: true }],
      }),
      mimo: makeMimoGroup({ key_count: 1, keys: [{ index: 0, masked: 'tp-a...bcd', is_current: true }] }),
      tavily: makeKeyPool({ group: 'tavily', key_count: 1, keys: [{ index: 0, masked: 'tv-a...bcd', is_current: true }] }),
      other_services: [
        {
          name: '高德MCP',
          usage: 'route_agent路线核验',
          configured: true,
          key_count: 1,
          masked_keys: ['am-a...bcd'],
        },
      ],
    })
    setupDefaultMocks()

    render(<ModelConfigPage />)

    expect(screen.getAllByText('sensenova-v1').length).toBeGreaterThan(0)
    expect(screen.getByText('sensenova-lite')).toBeInTheDocument()
    expect(screen.getByText('sk-a...bcd')).toBeInTheDocument()
    expect(screen.getByText('sk-l...ite')).toBeInTheDocument()
    expect(screen.getByText('tp-a...bcd')).toBeInTheDocument()
    expect(screen.getByText('tv-a...bcd')).toBeInTheDocument()
    expect(screen.getByText('高德MCP')).toBeInTheDocument()
    expect(screen.getByText('am-a...bcd')).toBeInTheDocument()
    expect(screen.queryByText('运行时已切换')).not.toBeInTheDocument()
  })

  it('shows override badge when a runtime override is active for full group', () => {
    mockModelConfig({ full: makeGroup({ effective_model: 'deepseek-v4', override_active: true }) })
    setupDefaultMocks()

    render(<ModelConfigPage />)

    expect(screen.getByText('deepseek-v4')).toBeInTheDocument()
    expect(screen.getByText('运行时已切换')).toBeInTheDocument()
  })

  it('calls setModel with the group of the card the user edited', () => {
    const mutate = vi.fn()
    mockModelConfig({
      light: makeGroup({ group: 'light', key_count: 1, keys: [{ index: 0, masked: 'sk-l', is_current: true }] }),
    })
    mockSetModel({ mutate })
    mockTestModelKeys()
    mockAddModelKey()
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    const inputs = screen.getAllByPlaceholderText(/输入新模型名/)
    fireEvent.change(inputs[0], { target: { value: 'deepseek-v4' } })
    fireEvent.click(screen.getAllByText('切换')[0])

    expect(mutate).toHaveBeenCalledWith({ model: 'deepseek-v4', group: 'full' })
  })

  it('restore-default button is disabled when there is no active override', () => {
    mockModelConfig({
      full: makeGroup({ key_count: 0, keys: [] }),
      light: makeGroup({ group: 'light', key_count: 0, keys: [] }),
    })
    setupDefaultMocks()

    render(<ModelConfigPage />)

    expect(screen.getAllByText('恢复默认')[0]).toBeDisabled()
  })

  // ---------------------------------------------------------------------------
  // 手动可用性检测（2026-07-26新增，见CLAUDE.md已知问题第5条）：此前页面只展示
  // "配置了什么"，从没真正发起过一次调用验证key/模型现在是否真的可用。
  // ---------------------------------------------------------------------------

  it('calls testModelKeys with the group and no keyIndex when "测试全部Key" is clicked', () => {
    const mutate = vi.fn()
    mockModelConfig()
    mockSetModel()
    mockTestModelKeys({ mutate })
    mockAddModelKey()
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    fireEvent.click(screen.getAllByText('测试全部Key')[0])

    expect(mutate).toHaveBeenCalledWith(
      { group: 'full', keyIndex: undefined },
      expect.anything(),
    )
  })

  it('calls testModelKeys with a specific keyIndex when a single key\'s "测试" button is clicked', () => {
    const mutate = vi.fn()
    mockModelConfig()
    mockSetModel()
    mockTestModelKeys({ mutate })
    mockAddModelKey()
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    // 每个分组的每个key旁边各有一个"测试"按钮，第一个对应full组的#0
    fireEvent.click(screen.getAllByText('测试')[0])

    expect(mutate).toHaveBeenCalledWith(
      { group: 'full', keyIndex: 0 },
      expect.anything(),
    )
  })

  it('renders success result inline after a key test succeeds', () => {
    mockModelConfig()
    mockSetModel()
    mockTestModelKeys({
      mutate: vi.fn((_vars, options) => {
        options?.onSuccess?.({
          group: 'full',
          model: 'sensenova-v1',
          results: [
            { index: 0, masked: 'sk-a...bcd', ok: true, latency_ms: 123, error: null, response_snippet: '可用' },
          ],
        })
      }),
    })
    mockAddModelKey()
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    fireEvent.click(screen.getAllByText('测试')[0])

    expect(screen.getByText('可用 · 123ms')).toBeInTheDocument()
  })

  it('renders failure result inline after a key test fails', () => {
    mockModelConfig()
    mockSetModel()
    mockTestModelKeys({
      mutate: vi.fn((_vars, options) => {
        options?.onSuccess?.({
          group: 'full',
          model: 'sensenova-v1',
          results: [
            { index: 0, masked: 'sk-a...bcd', ok: false, latency_ms: 50, error: '401 Unauthorized', response_snippet: null },
          ],
        })
      }),
    })
    mockAddModelKey()
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    fireEvent.click(screen.getAllByText('测试')[0])

    expect(screen.getByText('不可用：401 Unauthorized')).toBeInTheDocument()
  })

  it('disables the "测试全部Key" button for a group with zero configured keys', () => {
    mockModelConfig({
      full: makeGroup({ key_count: 0, keys: [] }),
      light: makeGroup({ group: 'light', key_count: 0, keys: [] }),
    })
    setupDefaultMocks()

    render(<ModelConfigPage />)

    for (const btn of screen.getAllByText('测试全部Key')) {
      expect(btn).toBeDisabled()
    }
  })

  // ---------------------------------------------------------------------------
  // Key CRUD（2026-08-01新增，推翻旧"不可编辑"政策）
  // ---------------------------------------------------------------------------

  it('adds a key to the full group when the add form is submitted', () => {
    const mutate = vi.fn()
    mockModelConfig()
    mockSetModel()
    mockTestModelKeys()
    mockAddModelKey({ mutate })
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    const inputs = screen.getAllByPlaceholderText('输入新Key')
    fireEvent.change(inputs[0], { target: { value: 'sk-newkey' } })
    fireEvent.click(screen.getAllByText('添加')[0])

    expect(mutate).toHaveBeenCalledWith(
      { group: 'full', key: 'sk-newkey', channel: undefined },
      expect.anything(),
    )
  })

  it('shows a channel selector when adding a mimo key and includes it in the mutation', () => {
    const mutate = vi.fn()
    mockModelConfig()
    mockSetModel()
    mockTestModelKeys()
    mockAddModelKey({ mutate })
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    expect(screen.getByText('gptload（聚合优先）')).toBeInTheDocument()

    const inputs = screen.getAllByPlaceholderText('输入新Key')
    // full/light/cunai/mimo/tavily/poster各一个添加行，mimo是第4个（index 3）
    fireEvent.change(inputs[3], { target: { value: 'gateway-newkey' } })
    fireEvent.click(screen.getAllByText('添加')[3])

    expect(mutate).toHaveBeenCalledWith(
      { group: 'mimo', key: 'gateway-newkey', channel: 'gptload' },
      expect.anything(),
    )
  })

  it('deletes a key only after confirming the dialog', () => {
    const mutate = vi.fn()
    mockModelConfig()
    mockSetModel()
    mockTestModelKeys()
    mockAddModelKey()
    mockDeleteModelKey({ mutate })

    render(<ModelConfigPage />)

    fireEvent.click(screen.getAllByText('删除')[0])
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确定删除' }))
    expect(mutate).toHaveBeenCalledWith({ group: 'full', index: 0 })
  })

  it('poster test result shows the untested-due-to-cost message instead of ok/fail', () => {
    mockModelConfig({
      full: makeGroup({ key_count: 0, keys: [] }),
      light: makeGroup({ group: 'light', key_count: 0, keys: [] }),
      poster: makeKeyPool({
        group: 'poster',
        label: '生图',
        usage: '按次收费',
        key_count: 1,
        keys: [{ index: 0, masked: 'sk-poster', is_current: true }],
      }),
    })
    mockSetModel()
    mockTestModelKeys({
      mutate: vi.fn((_vars, options) => {
        options?.onSuccess?.({
          group: 'poster',
          model: null,
          results: [
            {
              index: 0,
              masked: 'sk-poster',
              ok: null,
              latency_ms: null,
              error: '生图为按次收费真实调用，出于成本考虑不做自动测试，请直接尝试生成一次海报来验证',
              response_snippet: null,
            },
          ],
        })
      }),
    })
    mockAddModelKey()
    mockDeleteModelKey()

    render(<ModelConfigPage />)

    // full/light都是0 key，唯一有key的行是poster的这一个，其"测试"按钮就是页面上第一个
    fireEvent.click(screen.getAllByText('测试')[0])

    expect(screen.getByText(/出于成本考虑不做自动测试/)).toBeInTheDocument()
  })
})
