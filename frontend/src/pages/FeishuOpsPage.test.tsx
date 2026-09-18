import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { FeishuOpsPage } from './FeishuOpsPage'
import * as hooks from '../api/hooks'
import type { ActivityStat, FeishuBinding, FeishuHealth, HeartbeatHistoryResponse, PushLogEntry } from '../api/types'

function mockHealth(data: FeishuHealth | undefined, overrides: Partial<ReturnType<typeof hooks.useFeishuHealth>> = {}) {
  vi.spyOn(hooks, 'useFeishuHealth').mockReturnValue({
    data,
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useFeishuHealth>)
}

function mockBindings(
  data: FeishuBinding[] | undefined,
  overrides: Partial<ReturnType<typeof hooks.useFeishuBindings>> = {},
) {
  vi.spyOn(hooks, 'useFeishuBindings').mockReturnValue({
    data,
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as ReturnType<typeof hooks.useFeishuBindings>)
}

function mockPushLog(data: PushLogEntry[] = []) {
  vi.spyOn(hooks, 'useFeishuPushLog').mockReturnValue({
    data,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useFeishuPushLog>)
}

function mockHeartbeatHistory(data: HeartbeatHistoryResponse = { history: [], anomalies: [] }) {
  vi.spyOn(hooks, 'useFeishuHeartbeatHistory').mockReturnValue({
    data,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useFeishuHeartbeatHistory>)
}

function mockActivityStats(data: ActivityStat[] = []) {
  vi.spyOn(hooks, 'useFeishuActivityStats').mockReturnValue({
    data,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useFeishuActivityStats>)
}

function renderPage() {
  return render(
    <MemoryRouter>
      <FeishuOpsPage />
    </MemoryRouter>,
  )
}

function setupDefaultNewCardMocks() {
  mockPushLog()
  mockHeartbeatHistory()
  mockActivityStats()
}

describe('FeishuOpsPage', () => {
  it('renders healthy status with pid/started_at/heartbeat details', () => {
    mockHealth({
      status: 'healthy',
      pid: 1234,
      started_at: '2026-07-26T00:00:00+00:00',
      last_heartbeat: '2026-07-26T00:05:00+00:00',
      seconds_since_heartbeat: 5,
    })
    mockBindings([])
    setupDefaultNewCardMocks()

    renderPage()

    expect(screen.getByText('运行中')).toBeInTheDocument()
    expect(screen.getByText('PID：1234')).toBeInTheDocument()
    expect(screen.getByText('最近心跳：5秒前')).toBeInTheDocument()
  })

  it('renders not_running status without pid/heartbeat details', () => {
    mockHealth({
      status: 'not_running',
      pid: null,
      started_at: null,
      last_heartbeat: null,
      seconds_since_heartbeat: null,
    })
    mockBindings([])
    setupDefaultNewCardMocks()

    renderPage()

    expect(screen.getByText('未运行')).toBeInTheDocument()
    expect(screen.getByText(/从未收到过心跳/)).toBeInTheDocument()
  })

  it('renders stale status', () => {
    mockHealth({
      status: 'stale',
      pid: 999,
      started_at: '2026-07-26T00:00:00+00:00',
      last_heartbeat: '2026-07-26T00:00:00+00:00',
      seconds_since_heartbeat: 200,
    })
    mockBindings([])
    setupDefaultNewCardMocks()

    renderPage()

    expect(screen.getByText('心跳超时')).toBeInTheDocument()
    expect(screen.getByText('最近心跳：3分钟前')).toBeInTheDocument()
  })

  it('renders empty state when no bindings exist', () => {
    mockHealth({
      status: 'not_running',
      pid: null,
      started_at: null,
      last_heartbeat: null,
      seconds_since_heartbeat: null,
    })
    mockBindings([])
    setupDefaultNewCardMocks()

    renderPage()

    expect(screen.getByText('还没有任何飞书群绑定过project。')).toBeInTheDocument()
  })

  it('renders a binding with project summary when project exists, as a clickable link', () => {
    mockHealth({
      status: 'healthy',
      pid: 1,
      started_at: '2026-07-26T00:00:00+00:00',
      last_heartbeat: '2026-07-26T00:00:00+00:00',
      seconds_since_heartbeat: 1,
    })
    mockBindings([
      {
        chat_id: 'chat-1',
        project_id: 'feishu-chat-1',
        history: ['feishu-chat-1'],
        awaiting: 'skeleton_confirm',
        project_exists: true,
        topic: '成都',
        destination: '成都',
        state: '骨架确认中',
        updated_at: '2026-07-26T00:00:00+00:00',
      },
    ])
    setupDefaultNewCardMocks()

    renderPage()

    expect(screen.getByText('chat-1')).toBeInTheDocument()
    expect(screen.getByText('project_id：feishu-chat-1')).toBeInTheDocument()
    expect(screen.getByText('目的地：成都')).toBeInTheDocument()
    expect(screen.getByText('等待回复：skeleton_confirm')).toBeInTheDocument()
    expect(screen.getByText('骨架确认中')).toBeInTheDocument()
    const link = screen.getByText('chat-1').closest('a')
    expect(link).toHaveAttribute('href', '/projects/feishu-chat-1')
  })

  it('renders a danger badge when the binding is orphaned (project no longer exists), not clickable', () => {
    mockHealth({
      status: 'healthy',
      pid: 1,
      started_at: '2026-07-26T00:00:00+00:00',
      last_heartbeat: '2026-07-26T00:00:00+00:00',
      seconds_since_heartbeat: 1,
    })
    mockBindings([
      {
        chat_id: 'chat-orphan',
        project_id: 'feishu-chat-orphan',
        history: ['feishu-chat-orphan'],
        awaiting: null,
        project_exists: false,
        topic: null,
        destination: null,
        state: null,
        updated_at: null,
      },
    ])
    setupDefaultNewCardMocks()

    renderPage()

    expect(screen.getByText('project不存在')).toBeInTheDocument()
    expect(screen.getByText('chat-orphan').closest('a')).toBeNull()
  })

  // ---------------------------------------------------------------------------
  // 推送日志/心跳历史/活跃度统计（2026-08-01新增）
  // ---------------------------------------------------------------------------

  it('renders push log entries with success/failure badges', () => {
    mockHealth({ status: 'not_running', pid: null, started_at: null, last_heartbeat: null, seconds_since_heartbeat: null })
    mockBindings([])
    mockHeartbeatHistory()
    mockActivityStats()
    mockPushLog([
      {
        timestamp: '2026-08-01T00:00:00+00:00',
        chat_id: 'chat-1',
        project_id: 'project-1',
        push_type: 'reminder',
        success: true,
        error: null,
      },
      {
        timestamp: '2026-08-01T00:01:00+00:00',
        chat_id: 'chat-2',
        project_id: null,
        push_type: 'message',
        success: false,
        error: '模拟推送失败',
      },
    ])

    renderPage()

    expect(screen.getByText('reminder')).toBeInTheDocument()
    expect(screen.getByText('模拟推送失败')).toBeInTheDocument()
  })

  it('renders empty state for push log when there are no entries', () => {
    mockHealth({ status: 'not_running', pid: null, started_at: null, last_heartbeat: null, seconds_since_heartbeat: null })
    mockBindings([])
    setupDefaultNewCardMocks()

    renderPage()

    expect(screen.getByText('还没有任何推送记录。')).toBeInTheDocument()
  })

  it('renders activity stats table with aggregated counts', () => {
    mockHealth({ status: 'not_running', pid: null, started_at: null, last_heartbeat: null, seconds_since_heartbeat: null })
    mockBindings([])
    mockPushLog()
    mockHeartbeatHistory()
    mockActivityStats([
      {
        project_id: 'project-1',
        chat_id: 'chat-1',
        push_count: 5,
        success_count: 4,
        failure_count: 1,
        last_push_at: '2026-08-01T00:00:00+00:00',
      },
    ])

    renderPage()

    expect(screen.getByText('project-1')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
  })

  it('renders heartbeat history summary and anomaly periods', () => {
    mockHealth({ status: 'not_running', pid: null, started_at: null, last_heartbeat: null, seconds_since_heartbeat: null })
    mockBindings([])
    mockPushLog()
    mockActivityStats()
    mockHeartbeatHistory({
      history: [
        { timestamp: '2026-08-01T00:00:00+00:00', pid: 1 },
        { timestamp: '2026-08-01T00:05:00+00:00', pid: 1 },
      ],
      anomalies: [
        { started_at: '2026-08-01T00:00:00+00:00', ended_at: '2026-08-01T00:05:00+00:00', gap_seconds: 300 },
      ],
    })

    renderPage()

    expect(screen.getByText(/最近 2 次心跳，共检测到 1 次疑似掉线/)).toBeInTheDocument()
  })
})
