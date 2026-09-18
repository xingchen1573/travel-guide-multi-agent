import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { TrashPage } from './TrashPage'
import * as hooks from '../api/hooks'
import type { TrashedMaterialSummary, TrashedMaterialTopicSummary, TrashedProjectSummary } from '../api/types'

const TRASHED: TrashedProjectSummary[] = [
  {
    project_id: 'chengdu-v1',
    topic: '成都',
    destination: '成都',
    days: 3,
    state: '完成',
    updated_at: '2026-07-24T00:00:00Z',
  },
]

const TRASHED_MATERIALS: TrashedMaterialSummary[] = [
  {
    resource_type: 'material',
    topic: '成都',
    doc_id: '成都-000',
    title: '武侯祠攻略',
    platform: 'bilibili',
    source_file: '武侯祠_原文.md',
    chunk_count: 3,
    deleted_at: '2026-08-28T01:00:00Z',
  },
]

const TRASHED_MATERIAL_TOPICS: TrashedMaterialTopicSummary[] = [
  {
    resource_type: 'material_topic',
    topic: '测试城市',
    entry_count: 2,
    chunk_count: 5,
    raw_file_count: 2,
    trashed_material_count: 1,
    active_project_ids: [],
    trashed_project_ids: ['deleted-project'],
    deleted_at: '2026-08-28T02:00:00Z',
  },
]

function renderPage(
  trashed: TrashedProjectSummary[] = TRASHED,
  overrides: {
    materialTrash?: TrashedMaterialSummary[]
    restoreMutate?: ReturnType<typeof vi.fn>
    permanentDeleteMutate?: ReturnType<typeof vi.fn>
    restoreMaterialMutate?: ReturnType<typeof vi.fn>
    permanentDeleteMaterialMutate?: ReturnType<typeof vi.fn>
    materialTopicTrash?: TrashedMaterialTopicSummary[]
    restoreMaterialTopicMutate?: ReturnType<typeof vi.fn>
    permanentDeleteMaterialTopicMutate?: ReturnType<typeof vi.fn>
  } = {},
) {
  vi.spyOn(hooks, 'useTrash').mockReturnValue({
    data: trashed,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useTrash>)
  vi.spyOn(hooks, 'useRestoreProject').mockReturnValue({
    mutate: overrides.restoreMutate ?? vi.fn(),
  } as unknown as ReturnType<typeof hooks.useRestoreProject>)
  vi.spyOn(hooks, 'usePermanentlyDeleteProject').mockReturnValue({
    mutate: overrides.permanentDeleteMutate ?? vi.fn(),
  } as unknown as ReturnType<typeof hooks.usePermanentlyDeleteProject>)
  vi.spyOn(hooks, 'useMaterialTrash').mockReturnValue({
    data: overrides.materialTrash ?? [],
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useMaterialTrash>)
  vi.spyOn(hooks, 'useRestoreMaterial').mockReturnValue({
    mutate: overrides.restoreMaterialMutate ?? vi.fn(),
  } as unknown as ReturnType<typeof hooks.useRestoreMaterial>)
  vi.spyOn(hooks, 'usePermanentlyDeleteMaterial').mockReturnValue({
    mutate: overrides.permanentDeleteMaterialMutate ?? vi.fn(),
  } as unknown as ReturnType<typeof hooks.usePermanentlyDeleteMaterial>)
  vi.spyOn(hooks, 'useMaterialTopicTrash').mockReturnValue({
    data: overrides.materialTopicTrash ?? [],
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useMaterialTopicTrash>)
  vi.spyOn(hooks, 'useRestoreMaterialTopic').mockReturnValue({
    mutate: overrides.restoreMaterialTopicMutate ?? vi.fn(),
  } as unknown as ReturnType<typeof hooks.useRestoreMaterialTopic>)
  vi.spyOn(hooks, 'usePermanentlyDeleteMaterialTopic').mockReturnValue({
    mutate: overrides.permanentDeleteMaterialTopicMutate ?? vi.fn(),
  } as unknown as ReturnType<typeof hooks.usePermanentlyDeleteMaterialTopic>)

  return render(<TrashPage />)
}

describe('TrashPage', () => {
  it('shows an empty state when trash is empty', () => {
    renderPage([])
    expect(screen.getByText('Project 回收站是空的。')).toBeInTheDocument()
  })

  it('renders trashed project rows', () => {
    renderPage()
    expect(screen.getByText('chengdu-v1')).toBeInTheDocument()
    expect(screen.getByText('成都')).toBeInTheDocument()
  })

  it('restores a project without needing confirmation', () => {
    const mutate = vi.fn()
    renderPage(TRASHED, { restoreMutate: mutate })

    fireEvent.click(screen.getByRole('button', { name: '恢复' }))
    expect(mutate).toHaveBeenCalledWith('chengdu-v1')
  })

  it('permanently deletes a project only after confirming the dialog', () => {
    const mutate = vi.fn()
    renderPage(TRASHED, { permanentDeleteMutate: mutate })

    fireEvent.click(screen.getByRole('button', { name: '永久删除' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确定永久删除' }))
    expect(mutate).toHaveBeenCalledWith('chengdu-v1')
  })

  it('classifies material trash and supports restore', () => {
    const mutate = vi.fn()
    renderPage([], { materialTrash: TRASHED_MATERIALS, restoreMaterialMutate: mutate })

    fireEvent.click(screen.getByRole('tab', { name: '单条素材 · 1' }))
    expect(screen.getByText('武侯祠攻略')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '恢复' }))
    expect(mutate).toHaveBeenCalledWith({ topic: '成都', docId: '成都-000' })
  })

  it('permanently deletes material only after confirmation', () => {
    const mutate = vi.fn()
    renderPage([], { materialTrash: TRASHED_MATERIALS, permanentDeleteMaterialMutate: mutate })

    fireEvent.click(screen.getByRole('tab', { name: '单条素材 · 1' }))
    fireEvent.click(screen.getByRole('button', { name: '永久删除' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('武侯祠攻略')
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确定永久删除' }))
    expect(mutate).toHaveBeenCalledWith({ topic: '成都', docId: '成都-000' })
  })

  it('classifies material-space trash and supports restore', () => {
    const mutate = vi.fn()
    renderPage([], { materialTopicTrash: TRASHED_MATERIAL_TOPICS, restoreMaterialTopicMutate: mutate })

    fireEvent.click(screen.getByRole('tab', { name: '素材空间 · 1' }))
    expect(screen.getByText('测试城市')).toBeInTheDocument()
    expect(screen.getByText('2 / 5')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '恢复' }))
    expect(mutate).toHaveBeenCalledWith('测试城市')
  })

  it('permanently deletes a material space only after confirmation', () => {
    const mutate = vi.fn()
    renderPage([], {
      materialTopicTrash: TRASHED_MATERIAL_TOPICS,
      permanentDeleteMaterialTopicMutate: mutate,
    })

    fireEvent.click(screen.getByRole('tab', { name: '素材空间 · 1' }))
    fireEvent.click(screen.getByRole('button', { name: '永久删除' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('素材空间“测试城市”')
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确定永久删除' }))
    expect(mutate).toHaveBeenCalledWith('测试城市')
  })
})
