import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { UserNotesPage } from './UserNotesPage'
import * as hooks from '../api/hooks'
import type { UserNote } from '../api/types'

// 这个页面测试跟MaterialInsightsPage.test同款手法：mock掉hooks层，不套真实
// react-query Provider——但CreateNoteCard里调了useCreateUserNote（内部useQueryClient），
// 需要把mutation hook也一并mock，否则会报"No QueryClient set"。

const ROUTE_PLAN_DAYS = [
  {
    day: 1,
    city: '成都',
    area: '市区',
    pois: [{ name: '武侯祠' }, { name: '宽窄巷子' }],
    notes: '',
    sources: [],
  },
  {
    day: 2,
    city: '成都',
    area: '市区',
    pois: [{ name: '太古里' }],
    notes: '',
    sources: [],
  },
]

const NOTES: UserNote[] = [
  {
    note_id: 'note-1',
    day: 2,
    poi_name: '宽窄巷子',
    note_text: '这家火锅鱼要提前订',
    source: 'feishu',
    created_at: '2026-08-21T10:00:00Z',
    superseded_by: null,
    deleted: false,
  },
  {
    note_id: 'note-2',
    day: null,
    poi_name: '',
    note_text: '整体节奏偏紧',
    source: 'web',
    created_at: '2026-08-21T09:00:00Z',
    superseded_by: null,
    deleted: false,
  },
]

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/projects/p1/user_notes']}>
      <Routes>
        <Route path="/projects/:projectId/user_notes" element={<UserNotesPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function mockAll(overrides: { notes?: UserNote[] } = {}) {
  vi.spyOn(hooks, 'useUserNotes').mockReturnValue({
    data: overrides.notes ?? NOTES,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useUserNotes>)
  vi.spyOn(hooks, 'useProjectOutputs').mockReturnValue({
    data: { route_plan: { version: 2, days: ROUTE_PLAN_DAYS, amap_verified: true }, lifestyle: null, pitfall: null, final_guide: null },
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useProjectOutputs>)
  // 这三个mutation hook在CreateNoteCard/NoteCard里被无条件调用，必须mock避免"No QueryClient set"
  vi.spyOn(hooks, 'useCreateUserNote').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useCreateUserNote>)
  vi.spyOn(hooks, 'useUpdateUserNote').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useUpdateUserNote>)
  vi.spyOn(hooks, 'useDeleteUserNote').mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useDeleteUserNote>)
  vi.spyOn(hooks, 'useGenerateSite').mockReturnValue({
    trigger: vi.fn(),
    isTriggering: false,
  } as unknown as ReturnType<typeof hooks.useGenerateSite>)
}

beforeEach(() => {
  mockAll()
})

/** 造一个会真正调用onSuccess的mutation mock——组件里靠onSuccess回调触发重建site，
 * 纯vi.fn()不会执行回调，这两个测试需要确认"改/删成功后→重建site被触发"这条链路。
 * 把组件传入的所有参数原样转发给captured.mutate（供断言），再执行第二参数里的onSuccess。 */
function mutatingMock(capture: { mutate: (...a: unknown[]) => void }) {
  const fn = vi.fn((...args: unknown[]) => {
    capture.mutate(...args)
    const opts = args[1] as { onSuccess?: () => void } | undefined
    opts?.onSuccess?.()
  })
  return { mutate: fn, isPending: false } as never
}

describe('UserNotesPage', () => {
  it('renders source badge and position for each note', () => {
    renderPage()

    // 飞书来源徽章 + Day2·宽窄巷子 位置描述
    expect(screen.getByText('feishu')).toBeInTheDocument()
    expect(screen.getByText('Day2 · 宽窄巷子')).toBeInTheDocument()
    expect(screen.getByText('这家火锅鱼要提前订')).toBeInTheDocument()

    // 行程级备注（无天无POI）展示"行程级"
    expect(screen.getByText('行程级')).toBeInTheDocument()
    expect(screen.getByText('整体节奏偏紧')).toBeInTheDocument()
  })

  it('shows placeholder when there are no notes', () => {
    vi.spyOn(hooks, 'useUserNotes').mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useUserNotes>)
    renderPage()

    expect(screen.getByText('这个project还没有任何备注。')).toBeInTheDocument()
  })

  it('opens inline edit mode on 改 and saves via PUT', async () => {
    const captured: { mutate: (arg: unknown) => void } = { mutate: vi.fn() }
    const trigger = vi.fn()
    vi.spyOn(hooks, 'useUpdateUserNote').mockReturnValue(mutatingMock(captured))
    vi.spyOn(hooks, 'useGenerateSite').mockReturnValue({
      trigger,
      isTriggering: false,
    } as unknown as ReturnType<typeof hooks.useGenerateSite>)

    renderPage()

    fireEvent.click(screen.getAllByText('改')[0])
    expect(screen.getAllByText('保存').length).toBeGreaterThan(0)

    const textarea = screen.getByDisplayValue('这家火锅鱼要提前订')
    fireEvent.change(textarea, { target: { value: '改过的备注内容' } })
    fireEvent.click(screen.getAllByText('保存')[0])

    await waitFor(() => {
      expect(captured.mutate).toHaveBeenCalledWith(
        { noteId: 'note-1', input: { note_text: '改过的备注内容', day: 2, poi_name: '宽窄巷子' } },
        expect.objectContaining({ onSuccess: expect.any(Function) }),
      )
    })
    await waitFor(() => expect(trigger).toHaveBeenCalled())
  })

  it('requires confirm dialog before delete and triggers rebuild on confirm', async () => {
    const captured: { mutate: (arg: unknown) => void } = { mutate: vi.fn() }
    const trigger = vi.fn()
    vi.spyOn(hooks, 'useDeleteUserNote').mockReturnValue(mutatingMock(captured))
    vi.spyOn(hooks, 'useGenerateSite').mockReturnValue({
      trigger,
      isTriggering: false,
    } as unknown as ReturnType<typeof hooks.useGenerateSite>)

    renderPage()

    fireEvent.click(screen.getAllByText('删')[0])
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(captured.mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('确定删除'))
    await waitFor(() => expect(captured.mutate).toHaveBeenCalledWith('note-1', expect.anything()))
    await waitFor(() => expect(trigger).toHaveBeenCalled())
  })
})
