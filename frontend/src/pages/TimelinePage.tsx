import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { useProjectTimeline } from '../api/hooks'
import { Badge } from '../components/Badge'
import { Icon } from '../components/Icon'
import { ProjectTabs } from '../components/ProjectTabs'
import { conflictStatusVariant } from '../lib/status'
import type { TimelineEvent } from '../api/types'

const FILTERS = [
  { key: 'all', label: '全部事件' },
  { key: 'conflict', label: '仅冲突/复核' },
  { key: 'write', label: '仅写入操作' },
] as const

const CONFLICT_STATUS_LABEL: Record<string, string> = {
  pending: '待处理',
  resolved: '已收敛',
  force_finalized: '强制终结',
  open: '待复核',
  in_review: '模型复核中',
  closed: '已解决',
}

export function TimelinePage() {
  const { projectId = '' } = useParams()
  const { data: events, isLoading, error } = useProjectTimeline(projectId)
  const [filter, setFilter] = useState<(typeof FILTERS)[number]['key']>('all')

  if (isLoading) return <p className="text-ink-3">加载中…</p>
  if (error) return <p className="text-danger">加载失败：{(error as Error).message}</p>

  const filtered = (events ?? []).filter((e) => filter === 'all' || (filter === 'conflict' && (e.type === 'conflict' || e.type === 'review_finding')) || e.type === filter)

  return (
    <div>
      <ProjectTabs projectId={projectId} />

      <div className="flex gap-2.5 mb-5">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`text-[12.5px] font-semibold px-3 py-1.5 rounded-full border ${
              filter === f.key ? 'bg-ink text-white border-ink' : 'bg-surface text-ink-2 border-border'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <p className="text-ink-3">还没有任何记录。</p>
      ) : (
        <div className="relative pl-7">
          <div className="absolute left-2.25 top-1.5 bottom-1.5 w-0.5 bg-border" />
          {filtered.map((event, i) => (
            <TimelineItem key={i} event={event} />
          ))}
        </div>
      )}
    </div>
  )
}

function TimelineItem({ event }: { event: TimelineEvent }) {
  const isConflict = event.type === 'conflict'
  const isReviewFinding = event.type === 'review_finding'
  const isReviewEvent = isConflict || isReviewFinding
  const isResolved = isReviewEvent && (event.status === 'resolved' || event.status === 'closed' || event.disposition === 'resolved' || event.disposition === 'false_positive')
  const targetLabel = typeof event.target === 'string' ? event.target : String(event.target?.file || event.target?.scope || event.problem_type || '定向复核')

  return (
    <div className="relative pb-5.5">
      <div
        className={`absolute -left-7 top-0.5 w-5 h-5 rounded-full border-2 flex items-center justify-center ${
          isReviewEvent
            ? isResolved
              ? 'border-ok text-ok bg-ok-soft'
              : 'border-danger text-danger bg-danger-soft'
            : 'border-border-strong text-ink-3 bg-surface'
        }`}
      >
        <Icon name={isReviewEvent ? (isResolved ? 'check' : 'alertTriangle') : 'fileText'} className="w-2.75 h-2.75" />
      </div>
      <div
        className={`bg-surface border rounded-xl px-4 py-3 ${
          isReviewEvent ? (isResolved ? 'border-ok-soft' : 'border-danger-soft') : 'border-border'
        }`}
      >
        <div className="flex items-center gap-2 mb-1">
          <span className="text-[13px] font-bold">{event.actor ?? event.reported_by}</span>
          {isReviewEvent ? (
            <Badge variant={isResolved ? 'ok' : 'brand'}>{targetLabel}</Badge>
          ) : (
            <span className="text-[11.5px] text-ink-3">→ {targetLabel}</span>
          )}
          <span className="text-[11.5px] text-ink-3 font-mono ml-auto">{formatTime(event.timestamp)}</span>
        </div>
        <div className="text-[13px] text-ink-2 leading-relaxed">
          {event.issues && event.issues.length > 0 ? (
            <IssueList issues={event.issues} />
          ) : (
            <SummaryText text={event.summary ?? event.reason} />
          )}
        </div>
        {isReviewEvent && (event.status || event.disposition) && (
          <div className="mt-2 flex items-center gap-2">
            <Badge variant={isResolved ? 'ok' : conflictStatusVariant(event.status ?? 'pending')}>
              {CONFLICT_STATUS_LABEL[event.status ?? ''] ?? event.disposition ?? event.status}
            </Badge>
            {event.retry_count != null && (
              <span className="text-[11px] text-ink-3">重试第 {event.retry_count} 次</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function formatTime(iso?: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'medium' })
}

/** issue的字段视来源不同而不同（route: day/city/area；lifestyle/pitfall: field/day/
 * recommendation/warning），这里按已有字段拼一个简短单行，不重新推断跨字段规则。 */
function formatIssueLine(issue: Record<string, unknown>): string {
  const parts: string[] = []
  if (typeof issue.field === 'string' && issue.day != null) {
    parts.push(`${issue.field}.day${issue.day}`)
  } else if (issue.day != null) {
    const place = [issue.city, issue.area].filter(Boolean).join('')
    parts.push(place ? `Day${issue.day}「${place}」` : `Day${issue.day}`)
  }
  const missing = issue.missing_entities
  if (Array.isArray(missing) && missing.length > 0) {
    parts.push(`缺失专有名词${JSON.stringify(missing)}`)
  }
  if (typeof issue.reason === 'string' && issue.reason) {
    parts.push(issue.reason)
  }
  return parts.join('：') || JSON.stringify(issue)
}

// 折叠阈值——超过这个条数才需要"展开/收起"，几条以内直接全展示没必要加交互
const COLLAPSE_THRESHOLD = 5

/** 条数超过阈值时默认只显示前COLLAPSE_THRESHOLD条+展开按钮，避免一次核验打回二三十处
 * 问题时把整个时间线卡片撑得极长（真实案例：citation_checker一次打回26/27处引用核验
 * 问题）。按钮文案复用MaterialInsightsPage"查看原文依据"那套折叠交互的措辞风格。 */
function CollapsibleList({ items }: { items: string[] }) {
  const [expanded, setExpanded] = useState(false)
  const visible = expanded ? items : items.slice(0, COLLAPSE_THRESHOLD)
  const hiddenCount = items.length - COLLAPSE_THRESHOLD
  return (
    <>
      <ul className="list-disc pl-4.5 space-y-1">
        {visible.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
      {hiddenCount > 0 && (
        <button
          className="text-[11px] text-brand mt-1.5 hover:underline"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '收起 ▲' : `展开剩余 ${hiddenCount} 条 ▼`}
        </button>
      )}
    </>
  )
}

function IssueList({ issues }: { issues: Array<Record<string, unknown>> }) {
  return <CollapsibleList items={issues.map(formatIssueLine)} />
}

/** changelog里"共N处：a；b；c"这类核验汇总文案，没有结构化issues字段（那种走上面
 * IssueList），只能靠"；"分隔符拆分成逐条列表，否则几十条问题会挤成一整段无法辨认
 * 的长文字。 */
function SummaryText({ text }: { text?: string }) {
  if (!text) return null
  const match = text.match(/^(.*共\d+处.*?)：([\s\S]+)$/)
  const items = match
    ? match[2]
        .split('；')
        .map((s) => s.trim())
        .filter(Boolean)
    : []
  if (!match || items.length <= 1) return <>{text}</>
  return (
    <>
      <p className="mb-1.5">{match[1]}：</p>
      <CollapsibleList items={items} />
    </>
  )
}
