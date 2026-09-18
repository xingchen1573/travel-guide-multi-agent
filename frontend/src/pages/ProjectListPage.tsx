import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useDeleteProject, useProjects } from '../api/hooks'
import { Card } from '../components/Card'
import { Badge } from '../components/Badge'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Icon } from '../components/Icon'
import { executionStatusLabel, executionStatusVariant } from '../lib/status'
import type { ProjectSummary } from '../api/types'

type StateFilter = 'attention' | 'recent' | 'all' | 'active' | 'done' | 'history' | 'failed'

const ACTIVE_EXECUTION_STATUSES = new Set(['queued', 'running'])
const DEFAULT_CITY_PROJECT_LIMIT = 6

function isActivelyExecuting(project: ProjectSummary): boolean {
  return ACTIVE_EXECUTION_STATUSES.has(project.latest_job_status ?? '')
}

function attentionExecutionStatus(project: ProjectSummary): string | null {
  if (isActivelyExecuting(project)) return project.latest_job_status ?? null
  if (project.latest_job_status === 'interrupted') return 'interrupted'
  if (project.latest_job_status === 'failed') return 'failed'
  if (project.latest_job_status === 'cancelled') return 'cancelled'
  if (['interrupted', 'waiting_user'].includes(project.observed_run_status ?? '')) {
    return project.observed_run_status ?? null
  }
  return null
}

function needsAttention(project: ProjectSummary): boolean {
  return Boolean(attentionExecutionStatus(project) || project.has_run_failure)
}

function isHistoricalIncomplete(project: ProjectSummary): boolean {
  return project.state !== '完成' && !isActivelyExecuting(project) && !needsAttention(project)
}

function projectDisplayStatus(project: ProjectSummary): { label: string; variant: 'ok' | 'danger' | 'warn' | 'pending' | 'brand' } {
  const executionStatus = attentionExecutionStatus(project)
  if (executionStatus) {
    return {
      label: executionStatusLabel(executionStatus, project.latest_job_health_status ?? undefined),
      variant: executionStatusVariant(executionStatus, project.latest_job_health_status ?? undefined),
    }
  }
  if (project.has_run_failure) return { label: '运行失败', variant: 'danger' }
  if (project.state === '完成') return { label: '已完成', variant: 'ok' }
  return { label: '历史未完成', variant: 'pending' }
}

export function ProjectListPage() {
  const { data: projects, isLoading, error } = useProjects()
  const [search, setSearch] = useState('')
  const [stateFilter, setStateFilter] = useState<StateFilter>('done')
  const [expandedCities, setExpandedCities] = useState<Set<string>>(new Set())
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  const deleteProject = useDeleteProject()

  const filtered = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    const matching = (projects ?? []).filter((p) => {
      if (stateFilter === 'attention' && !needsAttention(p)) return false
      if (stateFilter === 'recent' && p.state !== '完成') return false
      if (stateFilter === 'done' && p.state !== '完成') return false
      if (stateFilter === 'history' && !isHistoricalIncomplete(p)) return false
      if (stateFilter === 'active' && !isActivelyExecuting(p)) return false
      if (stateFilter === 'failed' && !p.has_run_failure) return false
      if (keyword) {
        const inDestination = (p.destination ?? '').toLowerCase().includes(keyword)
        const inId = p.project_id.toLowerCase().includes(keyword)
        if (!inDestination && !inId) return false
      }
      return true
    })
    if (stateFilter !== 'recent') return matching
    return matching
      .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
      .slice(0, 6)
  }, [projects, search, stateFilter])

  // 按城市分组
  const groupedByCity = useMemo(() => {
    const groups = new Map<string, typeof filtered>()
    filtered.forEach((p) => {
      const city = p.destination || '未知目的地'
      if (!groups.has(city)) {
        groups.set(city, [])
      }
      groups.get(city)!.push(p)
    })
    // 按每个城市的最新更新时间排序
    return Array.from(groups.entries())
      .map(([city, items]) => ({
        city,
        items: items.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()),
        latestUpdate: Math.max(...items.map(p => new Date(p.updated_at).getTime()))
      }))
      .sort((a, b) => b.latestUpdate - a.latestUpdate)
  }, [filtered])

  if (isLoading) return <p className="text-ink-3">加载中…</p>
  if (error) return <p className="text-danger">加载失败：{(error as Error).message}</p>

  const total = projects?.length ?? 0
  const done = projects?.filter((p) => p.state === '完成').length ?? 0
  const activelyExecuting = projects?.filter(isActivelyExecuting).length ?? 0
  const attention = projects?.filter(needsAttention).length ?? 0
  const historicalIncomplete = projects?.filter(isHistoricalIncomplete).length ?? 0

  const totalCities = groupedByCity.length

  return (
    <div>
      <header className="mb-5">
        <h1 className="text-xl font-bold">项目</h1>
        <p className="mt-1 text-xs text-ink-3">默认显示已完成的项目；需要处理和历史记录仍可随时查找。</p>
      </header>
      <div className="grid grid-cols-2 gap-3.5 mb-6 min-[760px]:grid-cols-4">
        <Card className="p-4">
          <div className={`text-2xl font-bold ${attention ? 'text-danger' : 'text-ok'}`}>{attention}</div>
          <div className="text-xs text-ink-3 mt-0.5">需要处理</div>
        </Card>
        <Card className="p-4">
          <div className="text-2xl font-bold text-brand-dark">{activelyExecuting}</div>
          <div className="text-xs text-ink-3 mt-0.5">正在执行</div>
        </Card>
        <Card className="p-4">
          <div className="text-2xl font-bold text-ok">{done}</div>
          <div className="text-xs text-ink-3 mt-0.5">已完成</div>
        </Card>
        <Card className="p-4">
          <div className="text-2xl font-bold text-ink-2">{historicalIncomplete}</div>
          <div className="text-xs text-ink-3 mt-0.5">历史未完成</div>
        </Card>
      </div>

      {total > 0 && (
        <div className="flex items-center gap-2.5 mb-4 flex-wrap">
          <div className="relative flex-1 min-w-[220px]">
            <Icon name="search" className="w-3.75 h-3.75 absolute left-3 top-1/2 -translate-y-1/2 text-ink-3" />
            <input
              className="w-full border border-border rounded-lg pl-9 pr-3 py-2 text-sm"
              placeholder="按目的地或 project_id 搜索…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="relative">
            <select
              className="appearance-none border border-border rounded-lg pl-3 pr-8 py-2 text-sm bg-surface cursor-pointer hover:border-border-strong transition-colors"
              value={stateFilter}
              onChange={(e) => setStateFilter(e.target.value as StateFilter)}
            >
              <option value="attention">查看：需要处理</option>
              <option value="recent">查看：最近完成</option>
              <option value="all">查看：全部记录</option>
              <option value="active">状态：正在执行</option>
              <option value="done">状态：已完成</option>
              <option value="history">状态：历史未完成</option>
              <option value="failed">状态：上次运行失败</option>
            </select>
            <Icon name="chevronDown" className="w-3.5 h-3.5 absolute right-2.5 top-1/2 -translate-y-1/2 text-ink-3 pointer-events-none" />
          </div>
        </div>
      )}

      {total === 0 ? (
        <p className="text-ink-3">还没有任何项目。</p>
      ) : filtered.length === 0 ? (
        <div className="border-y border-ok/25 bg-ok-soft px-5 py-6 text-center">
          <p className="text-sm font-semibold text-ok">
            {stateFilter === 'done' ? '当前还没有已完成的项目' : '当前筛选条件下没有项目'}
          </p>
          <p className="mt-1 text-xs text-ink-3">可切换筛选条件继续查看。</p>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap gap-5 items-start">
            {groupedByCity.map(({ city, items }) => {
              const canCollapse = !search.trim() && stateFilter === 'all' && items.length > DEFAULT_CITY_PROJECT_LIMIT
              const isExpanded = expandedCities.has(city)
              const visibleItems = canCollapse && !isExpanded
                ? items.slice(0, DEFAULT_CITY_PROJECT_LIMIT)
                : items
              return <div key={city} className="bg-bg-inset/60 border border-border rounded-2xl p-4 min-w-0 max-w-full">
                <div className="flex items-center gap-2 mb-3">
                  <h2 className="text-base font-bold text-ink-1">{city}</h2>
                  <span className="text-xs text-ink-3">
                    {items.length} 个项目
                  </span>
                </div>
                <div className="flex flex-wrap gap-4">
                  {visibleItems.map((p) => {
                    const displayStatus = projectDisplayStatus(p)
                    return (
                    <Link
                      key={p.project_id}
                      to={`/projects/${p.project_id}`}
                      className="bg-surface border border-border rounded-2xl p-4.5 shadow-sm flex flex-col gap-3 hover:shadow-md hover:border-border-strong transition-all w-[280px]"
                    >
                      <div className="flex items-start justify-between gap-2.5">
                        <div className="flex items-center gap-2 min-w-0">
                          <div className="w-9.5 h-9.5 rounded-[10px] shrink-0 flex items-center justify-center text-base font-bold text-white bg-brand">
                            {p.days ?? '?'}
                          </div>
                          <div className="min-w-0">
                            <h3 className="text-sm font-bold truncate">
                              {p.days ?? '?'}天 · {p.destination ?? '未知目的地'}
                            </h3>
                            <div className="text-[11.5px] text-ink-3 font-mono truncate">{p.project_id}</div>
                          </div>
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          <Badge
                            variant={displayStatus.variant}
                          >
                            {displayStatus.label}
                          </Badge>
                          <button
                            className="text-ink-3 hover:text-danger p-1 -m-1 rounded transition-colors"
                            title="删除"
                            onClick={(e) => {
                              e.preventDefault()
                              e.stopPropagation()
                              setDeleteTarget(p.project_id)
                            }}
                          >
                            <Icon name="trash" className="w-3.75 h-3.75" />
                          </button>
                        </div>
                      </div>
                      <div className="flex gap-3.5 text-xs text-ink-2 flex-wrap">
                        {p.style && (
                          <span className="flex items-center gap-1">
                            <Icon name="compass" className="w-3.5 h-3.5 opacity-65" />
                            {p.style}
                          </span>
                        )}
                        {p.budget && (
                          <span className="flex items-center gap-1">
                            <Icon name="mapPin" className="w-3.5 h-3.5 opacity-65" />
                            预算·{p.budget}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center justify-between pt-2.5 border-t border-border">
                        <span className="text-[11.5px] text-ink-3">
                          {p.state === '完成' ? `更新于 ${formatRelative(p.updated_at)}` : `流程停在：${p.state}`}
                        </span>
                        <div className="flex items-center gap-2.5">
                          {p.has_run_failure && (
                            <Icon name="alertTriangle" className="w-3.25 h-3.25 text-danger" />
                          )}
                          {(p.review_findings_open ?? 0) > 0 && (
                            <span className="text-[10px] font-semibold text-warn">{p.review_findings_open} 条待复核</span>
                          )}
                        </div>
                      </div>
                    </Link>
                    )
                  })}
                </div>
                {canCollapse && (
                  <button
                    type="button"
                    className="mt-3 w-full border-t border-border pt-3 text-xs font-semibold text-brand-dark hover:text-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                    onClick={() => setExpandedCities((current) => {
                      const next = new Set(current)
                      if (next.has(city)) next.delete(city)
                      else next.add(city)
                      return next
                    })}
                  >
                    {isExpanded ? '收起较早项目' : `显示其余 ${items.length - DEFAULT_CITY_PROJECT_LIMIT} 个较早项目`}
                  </button>
                )}
              </div>
            })}
          </div>

          <div className="flex items-center justify-between mt-5">
            <span className="text-xs text-ink-3">
              当前显示 {filtered.length} / {total} 个项目，分布在 {totalCities} 个城市
            </span>
          </div>
        </>
      )}
      {deleteTarget && (
        <ConfirmDialog
          title="确定要删除这个project吗？"
          message="删除后会移入回收站，可以在「回收站」页面恢复或永久删除。"
          confirmLabel="确定删除"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            const target = deleteTarget
            setDeleteTarget(null)
            deleteProject.mutate(target)
          }}
        />
      )}
    </div>
  )
}

function formatRelative(iso: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
}
