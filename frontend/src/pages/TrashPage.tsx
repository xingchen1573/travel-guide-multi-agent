import { useState } from 'react'
import {
  useMaterialTopicTrash,
  useMaterialTrash,
  usePermanentlyDeleteMaterial,
  usePermanentlyDeleteMaterialTopic,
  usePermanentlyDeleteProject,
  useRestoreMaterial,
  useRestoreMaterialTopic,
  useRestoreProject,
  useTrash,
} from '../api/hooks'
import type { TrashedMaterialSummary, TrashedMaterialTopicSummary, TrashedProjectSummary } from '../api/types'
import { ConfirmDialog } from '../components/ConfirmDialog'

const PAGE_SIZE = 12

type TrashCategory = 'projects' | 'materials' | 'material-topics'
type PermanentDeleteTarget =
  | { kind: 'project'; projectId: string }
  | { kind: 'material'; topic: string; docId: string; title: string }
  | { kind: 'material-topic'; topic: string }

export function TrashPage() {
  const projectTrash = useTrash()
  const materialTrash = useMaterialTrash()
  const materialTopicTrash = useMaterialTopicTrash()
  const [category, setCategory] = useState<TrashCategory>('projects')
  const [page, setPage] = useState(1)
  const [permanentDeleteTarget, setPermanentDeleteTarget] = useState<PermanentDeleteTarget | null>(null)
  const restoreProject = useRestoreProject()
  const permanentlyDeleteProject = usePermanentlyDeleteProject()
  const restoreMaterial = useRestoreMaterial()
  const permanentlyDeleteMaterial = usePermanentlyDeleteMaterial()
  const restoreMaterialTopic = useRestoreMaterialTopic()
  const permanentlyDeleteMaterialTopic = usePermanentlyDeleteMaterialTopic()

  if (projectTrash.isLoading || materialTrash.isLoading || materialTopicTrash.isLoading) {
    return <p className="text-ink-3">加载中…</p>
  }
  const loadError = projectTrash.error ?? materialTrash.error ?? materialTopicTrash.error
  if (loadError) return <p className="text-danger">加载失败：{(loadError as Error).message}</p>

  const projects = projectTrash.data ?? []
  const materials = materialTrash.data ?? []
  const materialTopics = materialTopicTrash.data ?? []
  const itemCount =
    category === 'projects' ? projects.length : category === 'materials' ? materials.length : materialTopics.length
  const totalPages = Math.max(1, Math.ceil(itemCount / PAGE_SIZE))
  const offset = (page - 1) * PAGE_SIZE

  function selectCategory(next: TrashCategory) {
    setCategory(next)
    setPage(1)
  }

  function confirmPermanentDelete() {
    const target = permanentDeleteTarget
    setPermanentDeleteTarget(null)
    if (!target) return
    if (target.kind === 'project') {
      permanentlyDeleteProject.mutate(target.projectId)
    } else if (target.kind === 'material') {
      permanentlyDeleteMaterial.mutate({ topic: target.topic, docId: target.docId })
    } else {
      permanentlyDeleteMaterialTopic.mutate(target.topic)
    }
  }

  return (
    <div>
      <h2 className="text-lg font-bold mb-1">回收站</h2>
      <p className="text-xs text-ink-3 mb-4">
        已删除的 Project、单条素材和素材空间会分类保留在这里。可以恢复，也可以永久删除；回收站不会自动清理。
      </p>

      <div
        className="inline-flex p-1 rounded-lg bg-bg-inset border border-border mb-5"
        role="tablist"
        aria-label="回收站分类"
      >
        <button
          role="tab"
          aria-selected={category === 'projects'}
          className={`px-4 py-1.5 rounded-md text-xs font-semibold ${
            category === 'projects' ? 'bg-surface text-ink shadow-sm' : 'text-ink-3 hover:text-ink'
          }`}
          onClick={() => selectCategory('projects')}
        >
          Project · {projects.length}
        </button>
        <button
          role="tab"
          aria-selected={category === 'materials'}
          className={`px-4 py-1.5 rounded-md text-xs font-semibold ${
            category === 'materials' ? 'bg-surface text-ink shadow-sm' : 'text-ink-3 hover:text-ink'
          }`}
          onClick={() => selectCategory('materials')}
        >
          单条素材 · {materials.length}
        </button>
        <button
          role="tab"
          aria-selected={category === 'material-topics'}
          className={`px-4 py-1.5 rounded-md text-xs font-semibold ${
            category === 'material-topics' ? 'bg-surface text-ink shadow-sm' : 'text-ink-3 hover:text-ink'
          }`}
          onClick={() => selectCategory('material-topics')}
        >
          素材空间 · {materialTopics.length}
        </button>
      </div>

      {itemCount === 0 ? (
        <p className="text-ink-3">
          {category === 'projects'
            ? 'Project 回收站是空的。'
            : category === 'materials'
              ? '单条素材回收站是空的。'
              : '素材空间回收站是空的。'}
        </p>
      ) : (
        <>
          <div className="scroll-boundary-x border border-border rounded-lg overflow-x-auto">
            {category === 'projects' ? (
              <ProjectTrashTable
                items={projects.slice(offset, offset + PAGE_SIZE)}
                onRestore={(projectId) => restoreProject.mutate(projectId)}
                onDelete={(projectId) => setPermanentDeleteTarget({ kind: 'project', projectId })}
              />
            ) : category === 'materials' ? (
              <MaterialTrashTable
                items={materials.slice(offset, offset + PAGE_SIZE)}
                onRestore={(topic, docId) => restoreMaterial.mutate({ topic, docId })}
                onDelete={(item) => setPermanentDeleteTarget({
                  kind: 'material',
                  topic: item.topic,
                  docId: item.doc_id,
                  title: item.title,
                })}
              />
            ) : (
              <MaterialTopicTrashTable
                items={materialTopics.slice(offset, offset + PAGE_SIZE)}
                onRestore={(topic) => restoreMaterialTopic.mutate(topic)}
                onDelete={(topic) => setPermanentDeleteTarget({ kind: 'material-topic', topic })}
              />
            )}
          </div>

          <div className="flex items-center justify-between mt-5">
            <span className="text-xs text-ink-3">
              共 {itemCount} 条，第 {page}/{totalPages} 页
            </span>
            <div className="flex gap-1.5">
              <button
                className="px-3 py-1.5 rounded-md border border-border text-xs font-semibold disabled:opacity-40"
                disabled={page <= 1}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
              >
                上一页
              </button>
              <button
                className="px-3 py-1.5 rounded-md border border-border text-xs font-semibold disabled:opacity-40"
                disabled={page >= totalPages}
                onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
              >
                下一页
              </button>
            </div>
          </div>
        </>
      )}

      {permanentDeleteTarget && (
        <ConfirmDialog
          title="确定要永久删除吗？"
          message={
            permanentDeleteTarget.kind === 'project'
              ? '此操作不可恢复，Project 的全部数据（workspace/materials/agents）将被彻底清除。'
              : permanentDeleteTarget.kind === 'material'
                ? `此操作不可恢复，素材“${permanentDeleteTarget.title}”的原文、切片和向量快照将被彻底清除。`
                : `此操作不可恢复，素材空间“${permanentDeleteTarget.topic}”的原文、切片、向量索引以及随空间回收的单条素材记录将被彻底清除。`
          }
          confirmLabel="确定永久删除"
          onCancel={() => setPermanentDeleteTarget(null)}
          onConfirm={confirmPermanentDelete}
        />
      )}
    </div>
  )
}

function ProjectTrashTable({
  items,
  onRestore,
  onDelete,
}: {
  items: TrashedProjectSummary[]
  onRestore: (projectId: string) => void
  onDelete: (projectId: string) => void
}) {
  return (
    <table className="w-full min-w-[780px] text-sm">
      <thead className="bg-bg-inset text-ink-3 text-xs">
        <tr>
          <th className="text-left px-4 py-2.5 font-semibold">project_id</th>
          <th className="text-left px-4 py-2.5 font-semibold">目的地</th>
          <th className="text-left px-4 py-2.5 font-semibold">天数</th>
          <th className="text-left px-4 py-2.5 font-semibold">状态</th>
          <th className="text-left px-4 py-2.5 font-semibold">更新于</th>
          <th className="text-right px-4 py-2.5 font-semibold">操作</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.project_id} className="border-t border-border">
            <td className="px-4 py-2.5 font-mono text-[12.5px]">{item.project_id}</td>
            <td className="px-4 py-2.5">{item.destination ?? '—'}</td>
            <td className="px-4 py-2.5">{item.days ?? '—'}</td>
            <td className="px-4 py-2.5">{item.state ?? '—'}</td>
            <td className="px-4 py-2.5 text-ink-3">{formatDate(item.updated_at)}</td>
            <ActionCell onRestore={() => onRestore(item.project_id)} onDelete={() => onDelete(item.project_id)} />
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function MaterialTrashTable({
  items,
  onRestore,
  onDelete,
}: {
  items: TrashedMaterialSummary[]
  onRestore: (topic: string, docId: string) => void
  onDelete: (item: TrashedMaterialSummary) => void
}) {
  return (
    <table className="w-full min-w-[900px] text-sm">
      <thead className="bg-bg-inset text-ink-3 text-xs">
        <tr>
          <th className="text-left px-4 py-2.5 font-semibold">素材</th>
          <th className="text-left px-4 py-2.5 font-semibold">主题</th>
          <th className="text-left px-4 py-2.5 font-semibold">平台</th>
          <th className="text-left px-4 py-2.5 font-semibold">chunks</th>
          <th className="text-left px-4 py-2.5 font-semibold">删除于</th>
          <th className="text-right px-4 py-2.5 font-semibold">操作</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={`${item.topic}/${item.doc_id}`} className="border-t border-border">
            <td className="px-4 py-2.5">
              <div className="font-semibold max-w-[340px] truncate" title={item.title}>
                {item.title}
              </div>
              <div className="font-mono text-[11.5px] text-ink-3 mt-0.5">{item.doc_id}</div>
            </td>
            <td className="px-4 py-2.5">{item.topic}</td>
            <td className="px-4 py-2.5">{item.platform || '—'}</td>
            <td className="px-4 py-2.5">{item.chunk_count}</td>
            <td className="px-4 py-2.5 text-ink-3">{formatDate(item.deleted_at)}</td>
            <ActionCell
              onRestore={() => onRestore(item.topic, item.doc_id)}
              onDelete={() => onDelete(item)}
            />
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function MaterialTopicTrashTable({
  items,
  onRestore,
  onDelete,
}: {
  items: TrashedMaterialTopicSummary[]
  onRestore: (topic: string) => void
  onDelete: (topic: string) => void
}) {
  return (
    <table className="w-full min-w-[900px] text-sm">
      <thead className="bg-bg-inset text-ink-3 text-xs">
        <tr>
          <th className="text-left px-4 py-2.5 font-semibold">素材空间</th>
          <th className="text-left px-4 py-2.5 font-semibold">素材 / chunks</th>
          <th className="text-left px-4 py-2.5 font-semibold">原文文件</th>
          <th className="text-left px-4 py-2.5 font-semibold">随空间回收的单条素材</th>
          <th className="text-left px-4 py-2.5 font-semibold">关联回收站 Project</th>
          <th className="text-left px-4 py-2.5 font-semibold">删除于</th>
          <th className="text-right px-4 py-2.5 font-semibold">操作</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.topic} className="border-t border-border">
            <td className="px-4 py-2.5 font-semibold max-w-[260px] truncate" title={item.topic}>
              {item.topic}
            </td>
            <td className="px-4 py-2.5">{item.entry_count} / {item.chunk_count}</td>
            <td className="px-4 py-2.5">{item.raw_file_count}</td>
            <td className="px-4 py-2.5">{item.trashed_material_count}</td>
            <td className="px-4 py-2.5" title={item.trashed_project_ids.join('、')}>
              {item.trashed_project_ids.length}
            </td>
            <td className="px-4 py-2.5 text-ink-3">{formatDate(item.deleted_at)}</td>
            <ActionCell onRestore={() => onRestore(item.topic)} onDelete={() => onDelete(item.topic)} />
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ActionCell({ onRestore, onDelete }: { onRestore: () => void; onDelete: () => void }) {
  return (
    <td className="w-[176px] px-4 py-2.5 text-right">
      <div className="flex justify-end gap-2">
        <button
          className="whitespace-nowrap px-3 py-1.5 rounded-md border border-border-strong text-xs font-semibold hover:bg-bg-inset"
          onClick={onRestore}
        >
          恢复
        </button>
        <button
          className="whitespace-nowrap px-3 py-1.5 rounded-md border border-danger text-danger text-xs font-semibold hover:bg-danger-soft"
          onClick={onDelete}
        >
          永久删除
        </button>
      </div>
    </td>
  )
}

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
}
