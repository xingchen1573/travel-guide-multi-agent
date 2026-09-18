import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  useCreateUserNote,
  useDeleteUserNote,
  useProjectOutputs,
  useUpdateUserNote,
  useUserNotes,
} from '../api/hooks'
import { useGenerateSite } from '../api/hooks'
import type { RouteDay, UserNote } from '../api/types'
import { Badge } from '../components/Badge'
import { Card } from '../components/Card'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { ProjectTabs } from '../components/ProjectTabs'

/** 后台"用户备注"子tab（docs/ARCHITECTURE.md第21节阶段2）：列出project的所有未删备注，
 * 支持新增/改/删，改删后触发重建site让备注在网页可见。跟PoiPhotosPage同款"手写Tailwind
 * +原生表单元素"风格，不引新依赖。
 *
 * 备注=可见标注层，不进citation核验、不是正文重写——前端展示时统一带"📝 用户备注·未经核验"
 * 标识跟核验过的攻略内容视觉分离（见run_generate_site.py的_render_user_notes_block）。 */
export function UserNotesPage() {
  const { projectId = '' } = useParams()
  const { data: notes, isLoading, error } = useUserNotes(projectId)
  const { data: outputs } = useProjectOutputs(projectId)

  // route_plan的days摘要，供"新增"表单的天/POI下拉选项用（跟网页内嵌表单
  // _render_user_note_form同款：day下拉带城市、poi下拉随day联动）。
  const days: RouteDay[] = useMemo(
    () => outputs?.route_plan?.days ?? [],
    [outputs?.route_plan?.days],
  )

  if (isLoading) return <div><ProjectTabs projectId={projectId} /><p className="text-ink-3">加载中…</p></div>
  if (error)
    return (
      <div>
        <ProjectTabs projectId={projectId} />
        <p className="text-danger">加载失败：{(error as Error).message}</p>
      </div>
    )

  const sorted = [...(notes ?? [])].sort((a, b) => {
    // 按天升序、无天排最后；同天按创建时间倒序（新备注在前）
    if (a.day === null && b.day !== null) return 1
    if (a.day !== null && b.day === null) return -1
    if (a.day !== b.day) return (a.day ?? 0) - (b.day ?? 0)
    return (b.created_at || '').localeCompare(a.created_at || '')
  })

  return (
    <div>
      <ProjectTabs projectId={projectId} />
      <p className="text-[12.5px] text-ink-3 mb-4">
        备注是你自己的旁注，不经过系统核验，不会改写攻略正文。改/删后会自动重建展示网站让备注在网页可见。
      </p>
      <CreateNoteCard projectId={projectId} days={days} />
      <div className="flex flex-col gap-3 mt-4">
        {sorted.length === 0 ? (
          <p className="text-ink-3">这个project还没有任何备注。</p>
        ) : (
          sorted.map((note) => (
            <NoteCard key={note.note_id} projectId={projectId} note={note} days={days} />
          ))
        )}
      </div>
    </div>
  )
}

function CreateNoteCard({
  projectId,
  days,
}: {
  projectId: string
  days: RouteDay[]
}) {
  const createNote = useCreateUserNote(projectId)
  const generateSite = useGenerateSite(projectId)
  const [day, setDay] = useState<number | null>(null)
  const [poiName, setPoiName] = useState('')
  const [text, setText] = useState('')

  const submit = () => {
    if (!text.trim()) return
    createNote.mutate(
      { note_text: text.trim(), day, poi_name: poiName },
      {
        onSuccess: () => {
          setText('')
          setDay(null)
          setPoiName('')
          generateSite.trigger()
        },
      },
    )
  }

  return (
    <Card className="p-4">
      <div className="text-[13px] font-semibold mb-3">📝 新增用户备注</div>
      <div className="flex flex-col gap-3">
        <div className="flex gap-3">
          <label className="flex flex-col gap-1 flex-1">
            <span className="text-[12px] text-ink-2">绑定天数（可选）</span>
            <select
              className="rounded-md border border-border-strong px-2.5 py-1.5 text-[13px]"
              value={day ?? ''}
              onChange={(e) => {
                setDay(e.target.value === '' ? null : Number(e.target.value))
                setPoiName('')
              }}
            >
              <option value="">不绑定（行程级）</option>
              {days.map((d) => (
                <option key={d.day} value={d.day}>
                  Day {d.day}
                  {d.city ? ` · ${d.city}` : ''}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 flex-1">
            <span className="text-[12px] text-ink-2">绑定景点（可选，先选天数）</span>
            <select
              className="rounded-md border border-border-strong px-2.5 py-1.5 text-[13px]"
              value={poiName}
              disabled={day === null}
              onChange={(e) => setPoiName(e.target.value)}
            >
              <option value="">不绑定</option>
              {(days.find((d) => d.day === day)?.pois ?? []).map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <textarea
          className="rounded-md border border-border-strong px-2.5 py-1.5 text-[13px] resize-y"
          rows={2}
          placeholder="比如：这家火锅鱼要提前订"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <div className="flex items-center gap-3">
          <button
            className="px-3 py-1.5 rounded-md text-[12.5px] font-semibold text-white bg-brand hover:opacity-90 disabled:opacity-50"
            disabled={!text.trim() || createNote.isPending}
            onClick={submit}
          >
            {createNote.isPending ? '提交中…' : '提交'}
          </button>
          {createNote.error && (
            <span className="text-xs text-danger">
              {(createNote.error as Error).message}
            </span>
          )}
          {generateSite.isTriggering && (
            <span className="text-xs text-ink-3">正在重建网页…</span>
          )}
        </div>
      </div>
    </Card>
  )
}

function NoteCard({
  projectId,
  note,
  days,
}: {
  projectId: string
  note: UserNote
  days: RouteDay[]
}) {
  const updateNote = useUpdateUserNote(projectId)
  const deleteNote = useDeleteUserNote(projectId)
  const generateSite = useGenerateSite(projectId)
  const [editing, setEditing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  // 行内编辑的本地state
  const [day, setDay] = useState<number | null>(note.day)
  const [poiName, setPoiName] = useState(note.poi_name)
  const [text, setText] = useState(note.note_text)

  const positionDesc = useMemo(() => {
    if (note.day !== null && note.poi_name) return `Day${note.day} · ${note.poi_name}`
    if (note.day !== null) return `Day${note.day}`
    if (note.poi_name) return note.poi_name
    return '行程级'
  }, [note])

  const sourceVariant =
    note.source === 'feishu' ? 'brand' : note.source === 'admin' ? 'warn' : 'pending'

  const saveEdit = () => {
    if (!text.trim()) return
    updateNote.mutate(
      { noteId: note.note_id, input: { note_text: text.trim(), day, poi_name: poiName } },
      {
        onSuccess: () => {
          setEditing(false)
          generateSite.trigger()
        },
      },
    )
  }

  const confirmDeleteAction = () => {
    setConfirmDelete(false)
    deleteNote.mutate(note.note_id, {
      onSuccess: () => {
        generateSite.trigger()
      },
    })
  }

  return (
    <Card className="p-4">
      <div className="flex items-center gap-2 mb-2">
        <Badge variant={sourceVariant}>{note.source}</Badge>
        <span className="text-[12px] text-ink-3">{positionDesc}</span>
        <span className="ml-auto text-[11px] text-ink-3">{note.created_at || ''}</span>
      </div>
      {!editing ? (
        <p className="text-[14px] text-ink-1 whitespace-pre-wrap break-words">{note.note_text}</p>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex gap-2">
            <select
              className="rounded-md border border-border-strong px-2 py-1.5 text-[12.5px]"
              value={day ?? ''}
              onChange={(e) => {
                setDay(e.target.value === '' ? null : Number(e.target.value))
                setPoiName('')
              }}
            >
              <option value="">行程级</option>
              {days.map((d) => (
                <option key={d.day} value={d.day}>
                  Day {d.day}
                </option>
              ))}
            </select>
            <select
              className="rounded-md border border-border-strong px-2 py-1.5 text-[12.5px] flex-1"
              value={poiName}
              disabled={day === null}
              onChange={(e) => setPoiName(e.target.value)}
            >
              <option value="">不绑景点</option>
              {(days.find((d) => d.day === day)?.pois ?? []).map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          <textarea
            className="rounded-md border border-border-strong px-2.5 py-1.5 text-[13px] resize-y"
            rows={2}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
      )}
      <div className="flex gap-2 mt-3">
        {!editing ? (
          <>
            <button
              className="text-[12px] font-semibold px-2.5 py-1.5 rounded-md border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50"
              disabled={updateNote.isPending || deleteNote.isPending}
              onClick={() => {
                setDay(note.day)
                setPoiName(note.poi_name)
                setText(note.note_text)
                setEditing(true)
              }}
            >
              改
            </button>
            <button
              className="text-[12px] font-semibold px-2.5 py-1.5 rounded-md border border-danger text-danger hover:bg-danger-soft disabled:opacity-50"
              disabled={updateNote.isPending || deleteNote.isPending}
              onClick={() => setConfirmDelete(true)}
            >
              删
            </button>
            {(updateNote.isPending || deleteNote.isPending) && (
              <span className="text-xs text-ink-3 self-center">处理中…</span>
            )}
            {generateSite.isTriggering && (
              <span className="text-xs text-ink-3 self-center">正在重建网页…</span>
            )}
          </>
        ) : (
          <>
            <button
              className="text-[12px] font-semibold px-2.5 py-1.5 rounded-md bg-brand text-white hover:opacity-90 disabled:opacity-50"
              disabled={updateNote.isPending || !text.trim()}
              onClick={saveEdit}
            >
              保存
            </button>
            <button
              className="text-[12px] font-semibold px-2.5 py-1.5 rounded-md border border-border-strong text-ink-2 hover:bg-bg-inset"
              disabled={updateNote.isPending}
              onClick={() => setEditing(false)}
            >
              取消
            </button>
          </>
        )}
      </div>
      {confirmDelete && (
        <ConfirmDialog
          title="确定要删除这条备注吗？"
          message="删除后网页会在重建后同步移除该备注。此操作不可撤销。"
          confirmLabel="确定删除"
          onCancel={() => setConfirmDelete(false)}
          onConfirm={confirmDeleteAction}
        />
      )}
    </Card>
  )
}
