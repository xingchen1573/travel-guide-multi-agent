import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  useCollectMaterial,
  useDeleteMaterial,
  useDeleteMaterialTopic,
  useMaterialEntries,
  useMaterialEntityStats,
  useMaterialEntryDetail,
  useMaterialTopics,
} from '../api/hooks'
import { Card } from '../components/Card'
import { Badge } from '../components/Badge'
import { Icon } from '../components/Icon'
import { ConfirmDialog } from '../components/ConfirmDialog'
import type { IconName } from '../components/Icon'
import type { CollectPlatform, MaterialEntry } from '../api/types'

const PLATFORM_ICON: Record<string, IconName> = {
  bilibili: 'video',
  douyin: 'music',
  xiaohongshu: 'mapPin',
}

const PAGE_SIZE = 10

type CoreFilter = 'all' | 'core' | 'noncore'

export function MaterialsPage() {
  const { data: topics, isLoading: topicsLoading } = useMaterialTopics()
  const [searchParams] = useSearchParams()
  const [activeTopic, setActiveTopic] = useState<string>('')
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)
  const [collectOpen, setCollectOpen] = useState(false)
  const [entityModalEntry, setEntityModalEntry] = useState<MaterialEntry | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<MaterialEntry | null>(null)
  const [topicDeleteOpen, setTopicDeleteOpen] = useState(false)

  const [search, setSearch] = useState('')
  const [coreFilter, setCoreFilter] = useState<CoreFilter>('all')
  const [platformFilter, setPlatformFilter] = useState('all')
  const [page, setPage] = useState(1)

  // 从"素材洞察"页词云/榜单点进来时，URL带着?topic=xxx&search=yyy——首次拿到topics后
  // 应用一次这些参数（优先于"默认选第一个topic"）。这里只调用setActiveTopic本身，不走
  // selectTopic（见下方）——否则会把刚从URL带进来的search关键词立刻清空。之后按用户
  // 操作走正常状态管理，不持续跟URL同步（避免用户手动改search后又被回退成URL里的旧值）。
  const appliedUrlParamsRef = useRef(false)
  useEffect(() => {
    if (appliedUrlParamsRef.current || !topics || topics.length === 0) return
    appliedUrlParamsRef.current = true
    const topicParam = searchParams.get('topic')
    const searchParam = searchParams.get('search')
    const validTopic = topicParam && topics.some((t) => t.topic === topicParam) ? topicParam : topics[0].topic
    setActiveTopic(validTopic)
    if (searchParam) setSearch(searchParam)
  }, [topics, searchParams])

  // 用户点击topic标签页切换目的地时才需要重置筛选条件——显式函数调用而不是
  // "监听activeTopic变化就重置"的副作用写法：后者会连带误伤上面URL参数带来的
  // 那次activeTopic赋值（effect依赖触发的二次渲染会让"reset副作用"再跑一次，
  // 把刚应用的search关键词清空，这是真实踩过的坑，不是理论假设）。
  function selectTopic(topic: string) {
    setActiveTopic(topic)
    setSearch('')
    setCoreFilter('all')
    setPlatformFilter('all')
    setPage(1)
  }

  const { data: entries, isLoading: entriesLoading } = useMaterialEntries(activeTopic)
  const { data: entityStats } = useMaterialEntityStats(activeTopic)
  const active = topics?.find((t) => t.topic === activeTopic)
  const collect = useCollectMaterial(activeTopic)
  const deleteMaterial = useDeleteMaterial(activeTopic)
  const deleteTopic = useDeleteMaterialTopic()
  const collectRunning = ['queued', 'running'].includes(collect.status?.status ?? '') || collect.isTriggering
  const activeProjectIds = active?.active_project_ids ?? []
  const trashedProjectIds = active?.trashed_project_ids ?? []
  const topicDeleteBlocked = activeProjectIds.length > 0 || collectRunning

  // 实体->跨全库出现次数的查表，供表格标签/弹窗decorate用，不参与筛选逻辑本身。
  const entityCountMap = useMemo(
    () => new Map((entityStats ?? []).map((s) => [s.entity, s.count])),
    [entityStats],
  )

  const platforms = useMemo(
    () => Array.from(new Set((entries ?? []).map((e) => e.platform))).sort(),
    [entries],
  )

  const filteredEntries = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return (entries ?? []).filter((e) => {
      if (coreFilter === 'core' && !e.is_core) return false
      if (coreFilter === 'noncore' && e.is_core) return false
      if (platformFilter !== 'all' && e.platform !== platformFilter) return false
      if (keyword) {
        const inTitle = e.title.toLowerCase().includes(keyword)
        const inEntities = e.entities.some((x) => x.toLowerCase().includes(keyword))
        if (!inTitle && !inEntities) return false
      }
      return true
    })
  }, [entries, search, coreFilter, platformFilter])

  useEffect(() => {
    setPage(1)
  }, [search, coreFilter, platformFilter])

  const totalPages = Math.max(1, Math.ceil(filteredEntries.length / PAGE_SIZE))
  const pagedEntries = filteredEntries.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  if (topicsLoading) return <p className="text-ink-3">加载中…</p>

  return (
    <div>
      <div className="mb-4.5">
        <div className="flex items-center justify-between gap-3 mb-2">
          <div className="text-xs font-bold text-ink-3">素材空间</div>
          {activeTopic && (
            <div className="flex gap-2 shrink-0">
              <Link
                to={`/materials/insights?topic=${encodeURIComponent(activeTopic)}`}
                className="text-[13px] font-semibold px-3.5 py-2 rounded-lg border border-border-strong text-ink-2 hover:bg-bg-inset"
              >
                素材洞察 →
              </Link>
              <button
                className="inline-flex w-9 h-9 items-center justify-center rounded-lg border border-border-strong text-ink-3 hover:text-danger hover:bg-danger-soft disabled:opacity-40 disabled:hover:text-ink-3 disabled:hover:bg-transparent"
                aria-label={`将素材空间移入回收站：${activeTopic}`}
                title={
                  activeProjectIds.length > 0
                    ? `仍有 ${activeProjectIds.length} 个正常 Project 使用，不能删除`
                    : collectRunning
                      ? '采集运行中，不能删除'
                      : '将素材空间移入回收站'
                }
                disabled={topicDeleteBlocked || deleteTopic.isPending}
                onClick={() => setTopicDeleteOpen(true)}
              >
                <Icon name="trash" className="w-4 h-4" />
              </button>
              <button
                className="text-[13px] font-semibold px-3.5 py-2 rounded-lg bg-brand text-white disabled:opacity-50"
                disabled={collectRunning}
                onClick={() => setCollectOpen(true)}
              >
                {collectRunning ? '采集中…' : '触发采集'}
              </button>
            </div>
          )}
        </div>
        <div className="scroll-boundary-x flex gap-2 overflow-x-auto pb-1">
          {(topics ?? []).map((t) => (
            <button
              key={t.topic}
              onClick={() => selectTopic(t.topic)}
              title={t.topic}
              className={`flex w-[154px] shrink-0 items-center justify-between gap-2 px-3.5 py-2 rounded-lg border text-[13px] font-semibold ${
                activeTopic === t.topic ? 'border-brand bg-brand-soft text-brand-dark' : 'border-border bg-surface'
              }`}
            >
              <span className="truncate">{t.topic}</span>
              <span className="text-ink-3 font-normal shrink-0">· {t.entry_count}</span>
            </button>
          ))}
        </div>
        {active && (
          <div className="flex items-center gap-2 mt-2 text-xs text-ink-3">
            <Icon name="link" className="w-3.5 h-3.5 shrink-0" />
            <span title={activeProjectIds.join('、')}>
              关联 Project：正常 {activeProjectIds.length}
            </span>
            <span>·</span>
            <span title={trashedProjectIds.join('、')}>回收站 {trashedProjectIds.length}</span>
            {activeProjectIds.length > 0 && <span className="text-warn">需先删除或改绑正常 Project，才能回收此空间</span>}
          </div>
        )}
      </div>

      {(collectRunning || collect.status) && collect.status?.log && collect.status.log.length > 0 && (
        <div className="rounded-xl border border-border bg-bg-inset p-3.5 mb-4">
          <p className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2">
            采集进度{collectRunning && '（进行中…）'}
          </p>
          <div className="scroll-boundary-y flex flex-col gap-1 max-h-40 overflow-y-auto">
            {collect.status.log.map((line, i) => (
              <p key={i} className="text-xs text-ink-2 font-mono">
                {line}
              </p>
            ))}
          </div>
        </div>
      )}

      {collect.status?.status === 'succeeded' && (
        <div className="rounded-xl border border-ok-soft bg-ok-soft p-3.5 mb-4">
          <p className="text-sm text-ok font-semibold">
            采集完成：{collect.status.result_summary ?? '(无结果摘要)'}
          </p>
        </div>
      )}
      {collect.status?.status === 'failed' && (
        <div className="rounded-xl border border-danger-soft bg-danger-soft p-3.5 mb-4">
          <p className="text-sm text-danger font-semibold">采集失败</p>
          <pre className="text-xs text-danger mt-1 whitespace-pre-wrap">{collect.status.error}</pre>
        </div>
      )}
      {collect.status?.status === 'interrupted' && (
        <div className="rounded-xl border border-danger-soft bg-danger-soft p-3.5 mb-4">
          <p className="text-sm text-danger font-semibold">采集已中断，可以重新触发。</p>
          {collect.status.status_reason && <p className="text-xs text-danger mt-1">{collect.status.status_reason}</p>}
        </div>
      )}
      {collect.status?.status === 'running' && collect.status.health_status === 'stalled' && (
        <div className="rounded-xl border border-warn bg-warn-soft p-3.5 mb-4 text-sm text-[#8a6417] font-semibold">
          采集进程仍存活，但业务进度已超过 15 分钟没有变化。
        </div>
      )}

      {active && (
        <div className="grid grid-cols-3 gap-3.5 mb-5">
          <Card className="p-4 flex items-center gap-3">
            <div className="w-9.5 h-9.5 rounded-[10px] bg-teal-soft text-teal flex items-center justify-center shrink-0">
              <Icon name="database" className="w-4.5 h-4.5" />
            </div>
            <div>
              <b className="text-xl">{active.entry_count}</b>
              <div className="text-xs text-ink-3">已入库素材</div>
            </div>
          </Card>
          <Card className="p-4 flex items-center gap-3">
            <div className="w-9.5 h-9.5 rounded-[10px] bg-ok-soft text-ok flex items-center justify-center shrink-0">
              <Icon name="check" className="w-4.5 h-4.5" />
            </div>
            <div>
              <b className="text-xl">{active.core_count}</b>
              <div className="text-xs text-ink-3">核心素材（is_core）</div>
            </div>
          </Card>
          <Card className="p-4 flex items-center gap-3">
            <div className="w-9.5 h-9.5 rounded-[10px] bg-gold-soft text-gold flex items-center justify-center shrink-0">
              <Icon name="layers" className="w-4.5 h-4.5" />
            </div>
            <div>
              <b className="text-xl">{active.chunk_count}</b>
              <div className="text-xs text-ink-3">切分后 chunk 总数</div>
            </div>
          </Card>
        </div>
      )}

      <div className="flex items-center gap-2.5 mb-3.5 flex-wrap">
        <div className="relative flex-1 min-w-[220px]">
          <Icon name="search" className="w-3.75 h-3.75 absolute left-3 top-1/2 -translate-y-1/2 text-ink-3" />
          <input
            className="w-full border border-border rounded-lg pl-9 pr-3 py-2 text-sm"
            placeholder="按标题或提及实体搜索…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <select
          className="border border-border rounded-lg px-3 py-2 text-sm bg-surface"
          value={coreFilter}
          onChange={(e) => setCoreFilter(e.target.value as CoreFilter)}
        >
          <option value="all">核心素材：全部</option>
          <option value="core">核心素材：是</option>
          <option value="noncore">核心素材：否</option>
        </select>
        <select
          className="border border-border rounded-lg px-3 py-2 text-sm bg-surface"
          value={platformFilter}
          onChange={(e) => setPlatformFilter(e.target.value)}
        >
          <option value="all">平台：全部</option>
          {platforms.map((p) => (
            <option key={p} value={p}>
              平台：{p}
            </option>
          ))}
        </select>
      </div>

      {deleteMaterial.error && (
        <p className="text-sm text-danger mb-3">删除失败：{(deleteMaterial.error as Error).message}</p>
      )}
      {deleteTopic.error && (
        <p className="text-sm text-danger mb-3">素材空间删除失败：{(deleteTopic.error as Error).message}</p>
      )}

      <Card>
        <div className="scroll-boundary-x overflow-x-auto">
          <table className="w-full min-w-[820px] border-collapse">
            <thead>
              <tr>
                <th className="w-8.5" />
                <th className="text-left text-[11.5px] font-semibold text-ink-3 uppercase tracking-wide px-3.5 py-2.5 border-b border-border">
                  素材标题
                </th>
                <th className="text-left text-[11.5px] font-semibold text-ink-3 uppercase tracking-wide px-3.5 py-2.5 border-b border-border">
                  提及实体
                </th>
                <th className="text-left text-[11.5px] font-semibold text-ink-3 uppercase tracking-wide px-3.5 py-2.5 border-b border-border">
                  核心素材
                </th>
                <th className="text-left text-[11.5px] font-semibold text-ink-3 uppercase tracking-wide px-3.5 py-2.5 border-b border-border">
                  doc_id
                </th>
                <th className="w-14 text-right text-[11.5px] font-semibold text-ink-3 uppercase tracking-wide px-3.5 py-2.5 border-b border-border">
                  操作
                </th>
              </tr>
            </thead>
            <tbody>
            {entriesLoading && (
              <tr>
                <td colSpan={6} className="px-3.5 py-4 text-ink-3">
                  加载中…
                </td>
              </tr>
            )}
            {!entriesLoading && filteredEntries.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3.5 py-4 text-ink-3">
                  {(entries ?? []).length === 0 ? '没有素材条目。' : '没有匹配搜索/筛选条件的素材。'}
                </td>
              </tr>
            )}
            {pagedEntries.map((e) => {
              const shown = e.entities.slice(0, 4)
              const more = e.entities.length - shown.length
              return (
                <tr
                  key={e.doc_id}
                  className="hover:bg-surface-hover cursor-pointer"
                  onClick={() => setSelectedDocId(e.doc_id)}
                >
                  <td className="px-3.5 py-3 border-b border-border">
                    <div className="w-7.5 h-7.5 rounded-lg bg-bg-inset flex items-center justify-center">
                      <Icon name={PLATFORM_ICON[e.platform] ?? 'fileText'} className="w-3.75 h-3.75" />
                    </div>
                  </td>
                  <td className="px-3.5 py-3 border-b border-border">
                    <div className="flex items-center gap-1.5 max-w-[360px]">
                      <div className="text-[13.5px] font-semibold">{e.title}</div>
                      {e.source_url && (
                        <a
                          href={e.source_url}
                          target="_blank"
                          rel="noreferrer"
                          onClick={(ev) => ev.stopPropagation()}
                          className="shrink-0 text-ink-3 hover:text-brand-dark"
                          aria-label="打开原始链接"
                          title="打开原始链接"
                        >
                          <Icon name="externalLink" className="w-3.25 h-3.25" />
                        </a>
                      )}
                    </div>
                    <div className="text-[11.5px] text-ink-3 mt-0.5">{e.platform}</div>
                  </td>
                  <td className="px-3.5 py-3 border-b border-border">
                    <div className="flex gap-1.5 flex-wrap max-w-[320px] items-center">
                      {shown.map((x) => (
                        <span key={x} className="text-[11px] px-2 py-0.5 rounded-full bg-bg-inset border border-border text-ink-2">
                          {x}
                          {entityCountMap.has(x) && (
                            <span className="text-ink-3"> ×{entityCountMap.get(x)}</span>
                          )}
                        </span>
                      ))}
                      {more > 0 && (
                        <button
                          className="text-[11px] text-brand-dark font-semibold hover:underline"
                          onClick={(ev) => {
                            ev.stopPropagation()
                            setEntityModalEntry(e)
                          }}
                        >
                          +{more}
                        </button>
                      )}
                      {e.entities.length === 0 && (
                        <span className="text-xs text-ink-3">未标注（is_core=false，检索时不参与排序）</span>
                      )}
                    </div>
                  </td>
                  <td className="px-3.5 py-3 border-b border-border">
                    {e.is_core ? <Badge variant="ok">是</Badge> : <Badge variant="pending">否</Badge>}
                  </td>
                  <td className="px-3.5 py-3 border-b border-border">
                    <span className="text-xs text-ink-3 font-mono">{e.doc_id}</span>
                  </td>
                  <td className="px-3.5 py-3 border-b border-border text-right">
                    <button
                      className="inline-flex w-8 h-8 items-center justify-center rounded-md text-ink-3 hover:text-danger hover:bg-danger-soft disabled:opacity-40"
                      aria-label={`将素材移入回收站：${e.title}`}
                      title="移入回收站"
                      disabled={deleteMaterial.isPending}
                      onClick={(ev) => {
                        ev.stopPropagation()
                        setDeleteTarget(e)
                      }}
                    >
                      <Icon name="trash" className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              )
            })}
            </tbody>
          </table>
        </div>
        {filteredEntries.length > 0 && (
          <div className="flex items-center justify-between px-3.5 py-2.5 border-t border-border">
            <span className="text-xs text-ink-3">
              共 {filteredEntries.length} 条，第 {page}/{totalPages} 页
            </span>
            <div className="flex gap-1.5">
              <button
                className="px-2.5 py-1 rounded-md border border-border text-xs font-semibold disabled:opacity-40"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                上一页
              </button>
              <button
                className="px-2.5 py-1 rounded-md border border-border text-xs font-semibold disabled:opacity-40"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                下一页
              </button>
            </div>
          </div>
        )}
      </Card>

      {selectedDocId && (
        <MaterialEntryDetailModal
          topic={activeTopic}
          docId={selectedDocId}
          onClose={() => setSelectedDocId(null)}
        />
      )}

      {entityModalEntry && (
        <EntityListModal
          entry={entityModalEntry}
          entityCountMap={entityCountMap}
          onClose={() => setEntityModalEntry(null)}
        />
      )}

      {collectOpen && (
        <CollectDialog
          topic={activeTopic}
          onCancel={() => setCollectOpen(false)}
          onConfirm={(requestText, platform) => {
            setCollectOpen(false)
            collect.trigger(requestText, platform)
          }}
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          title="将素材移入回收站？"
          message="素材会立即停止参与检索；原文、切片和向量会保留在回收站中，可以恢复。"
          confirmLabel="移入回收站"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            const target = deleteTarget
            setDeleteTarget(null)
            if (selectedDocId === target.doc_id) setSelectedDocId(null)
            deleteMaterial.mutate(target.doc_id)
          }}
        />
      )}

      {topicDeleteOpen && active && (
        <ConfirmDialog
          title="将素材空间移入回收站？"
          message={`“${active.topic}”中的 ${active.entry_count} 条素材、${active.chunk_count} 个切片以及原文和向量索引会一起移入回收站。该空间关联的 ${trashedProjectIds.length} 个回收站 Project 不会被删除，空间之后可以整体恢复。`}
          confirmLabel="回收素材空间"
          onCancel={() => setTopicDeleteOpen(false)}
          onConfirm={() => {
            const topic = active.topic
            setTopicDeleteOpen(false)
            deleteTopic.mutate(topic, {
              onSuccess: () => {
                const nextTopic = (topics ?? []).find((item) => item.topic !== topic)?.topic ?? ''
                selectTopic(nextTopic)
              },
            })
          }}
        />
      )}
    </div>
  )
}

function EntityListModal({
  entry,
  entityCountMap,
  onClose,
}: {
  entry: MaterialEntry
  entityCountMap: Map<string, number>
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" role="dialog" aria-modal="true">
      <Card className="scroll-boundary-y p-6 max-w-md w-full mx-4 max-h-[70vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-bold text-base">{entry.title} · 提及实体（共 {entry.entities.length} 个）</h3>
          <button className="text-ink-3 hover:text-ink" onClick={onClose} aria-label="关闭">
            <Icon name="x" className="w-4.5 h-4.5" />
          </button>
        </div>
        <div className="flex gap-1.5 flex-wrap">
          {entry.entities.map((x) => (
            <span key={x} className="text-[12px] px-2.5 py-1 rounded-full bg-bg-inset border border-border text-ink-2">
              {x}
              {entityCountMap.has(x) && <span className="text-ink-3"> ×{entityCountMap.get(x)}</span>}
            </span>
          ))}
        </div>
      </Card>
    </div>
  )
}

function MaterialEntryDetailModal({
  topic,
  docId,
  onClose,
}: {
  topic: string
  docId: string
  onClose: () => void
}) {
  const { data, isLoading } = useMaterialEntryDetail(topic, docId)

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" role="dialog" aria-modal="true">
      <Card className="scroll-boundary-y p-6 max-w-2xl w-full mx-4 max-h-[80vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-bold text-base flex items-center gap-2">
            {data?.entry.title ?? docId}
            {data?.entry.source_url && (
              <a
                href={data.entry.source_url}
                target="_blank"
                rel="noreferrer"
                className="text-ink-3 hover:text-brand-dark"
                aria-label="打开原始链接"
                title="打开原始链接"
              >
                <Icon name="externalLink" className="w-3.75 h-3.75" />
              </a>
            )}
          </h3>
          <button className="text-ink-3 hover:text-ink" onClick={onClose} aria-label="关闭">
            <Icon name="x" className="w-4.5 h-4.5" />
          </button>
        </div>
        {isLoading ? (
          <p className="text-ink-3">加载中…</p>
        ) : data ? (
          <div className="flex flex-col gap-4">
            <div>
              <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2">
                Chunk 文本（共 {data.chunks.length} 条）
              </div>
              <div className="flex flex-col gap-2">
                {data.chunks.map((c) => (
                  <div key={c.chunk_id} className="border border-border rounded-lg p-3">
                    <div className="text-[11px] text-ink-3 font-mono mb-1">{c.chunk_id}</div>
                    <div className="text-[13px] leading-relaxed whitespace-pre-wrap">{c.text}</div>
                  </div>
                ))}
                {data.chunks.length === 0 && <p className="text-sm text-ink-3">没有chunk数据。</p>}
              </div>
            </div>
            <div>
              <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2">Raw 原文</div>
              {data.raw_text ? (
                <pre className="text-[13px] leading-relaxed whitespace-pre-wrap font-sans border border-border rounded-lg p-3">
                  {data.raw_text}
                </pre>
              ) : (
                <p className="text-sm text-ink-3">找不到对应的raw原文文件。</p>
              )}
            </div>
          </div>
        ) : (
          <p className="text-ink-3">未找到该条素材。</p>
        )}
      </Card>
    </div>
  )
}

function CollectDialog({
  topic,
  onCancel,
  onConfirm,
}: {
  topic: string
  onCancel: () => void
  onConfirm: (requestText: string, platform: CollectPlatform) => void
}) {
  const [requestText, setRequestText] = useState(`给我几条${topic}最新的攻略`)
  const [platform, setPlatform] = useState<CollectPlatform>('all')

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" role="dialog" aria-modal="true">
      <Card className="p-6 max-w-sm mx-4">
        <h3 className="font-bold text-base mb-2">触发采集：{topic}</h3>
        <p className="text-sm text-ink-2 mb-3">
          核心素材agent会自主搜索候选并自动确认采集入库（auto_confirm）。
        </p>
        <label className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1.5 block">采集平台</label>
        <select
          className="w-full border border-border rounded-lg px-3 py-2 text-sm mb-3 bg-surface"
          value={platform}
          onChange={(e) => setPlatform(e.target.value as CollectPlatform)}
        >
          <option value="all">全部（B站+抖音）</option>
          <option value="bilibili">仅B站</option>
          <option value="douyin">仅抖音</option>
        </select>
        <textarea
          className="w-full border border-border rounded-lg px-3 py-2 text-sm mb-4"
          rows={3}
          value={requestText}
          onChange={(e) => setRequestText(e.target.value)}
        />
        <div className="flex justify-end gap-2.5">
          <button
            className="px-4 py-2 rounded-lg text-[13px] font-semibold border border-border-strong text-ink-2 hover:bg-bg-inset"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-brand text-white hover:opacity-90"
            onClick={() => onConfirm(requestText, platform)}
          >
            开始采集
          </button>
        </div>
      </Card>
    </div>
  )
}
