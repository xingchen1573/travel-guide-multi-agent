import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { useGlobalOptimizationCandidates, useGlobalReviewFindings, useProjects, useReviewComparison, useReviewContext, useReviewExecutors, useReviewFindings, useReviewItemDecisions, useReviewQueue, useReviewRecommendation, useReviewRecommendationById, useReviewRecommendationHistory, useReviewRuns, useReviewState, useReviewValidation } from '../api/hooks'
import { Badge } from '../components/Badge'
import { Card } from '../components/Card'
import { Drawer } from '../components/Drawer'
import { Icon } from '../components/Icon'
import { ManifestConsistencyPanel } from '../components/ManifestConsistencyPanel'
import { MissingEntityReviewPanel } from '../components/MissingEntityReviewPanel'
import { ProjectTabs } from '../components/ProjectTabs'
import { QualityImprovementPanel } from '../components/QualityImprovementPanel'
import { Tabs } from '../components/Tabs'
import { reviewProgressPresentation, summarizeReviewRunError } from '../lib/reviewRuns'
import type { ReviewClaimEvidence, ReviewContext, ReviewFinding, ReviewItemDecision, ReviewItemDisposition, ReviewPatch, ReviewProposedOperation, ReviewQueueGroup, ReviewRecommendation, ReviewRun } from '../api/types'

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }

type ReviewItem = {
  key: string
  index: number
  kind: 'claim' | 'legacy_suggestion'
  label: string
  claim: ReviewClaimEvidence | null
}

type ReviewItemContext = ReviewContext['review_item_contexts'][number]
type OriginClaimContext = ReviewContext['origin_claims'][number]

const dispositionLabel: Record<string, string> = {
  resolved: '已解决',
  false_positive: '误报',
  deferred: '待处理',
  needs_rerun: '需重跑',
}

function dispositionVariant(value: string) {
  if (value === 'resolved' || value === 'false_positive') return 'ok' as const
  if (value === 'needs_rerun') return 'danger' as const
  return 'warn' as const
}

function findingModelWasInvolved(finding: ReviewFinding, recommendation: ReviewRecommendation | null | undefined) {
  return Boolean(recommendation) || finding.verification_actor_type === 'model' || finding.actor_id === 'model' || finding.verification_status === 'passed_review'
}

// 模型建议常把改写方案写成一段夹带"- "或"1. "枚举标记的长文本，不含真实换行。
// 这里用零宽分隔符把枚举标记切成真正的列表项，未命中枚举模式时原样返回整段文本。
function splitEnumeratedText(text: string): { intro: string; items: string[] } {
  const normalized = text.replace(/(^|[。：；\n])\s*(?:[-•]\s+|\d+[.．]\s*)/g, (_match, lead: string) => `${lead}\\u0000`)
  const parts = normalized.split('\\u0000').map((part) => part.trim()).filter(Boolean)
  if (parts.length <= 1) return { intro: text.trim(), items: [] }
  const [intro, ...items] = parts
  return { intro, items }
}

function legacySuggestionItems(text: string): string[] {
  const detail = text.includes('具体各点的最小改写建议如下：')
    ? text.split('具体各点的最小改写建议如下：', 2)[1]
    : text
  const lines = detail.split('\n')
    .map((line) => line.match(/^\s*(?:[-•]|\d+[.．])\s*(.+?)\s*$/)?.[1]?.trim())
    .filter((line): line is string => Boolean(line))
  return lines.length > 0 ? lines : text.trim() ? [text.trim()] : []
}

const itemDecisionLabel: Record<ReviewItemDisposition, string> = {
  accepted: '采纳为处理方案',
  keep_original: '保留原文',
  edited: '修改后采纳',
  deferred: '暂缓',
  needs_rerun: '需重跑',
}

const coverageLabel: Record<string, string> = {
  ready: '引用已取回',
  partial: '引用部分缺失',
  insufficient: '未挂可读引用',
  unmapped: '未定位当前字段',
}

function itemCoverageLabel(item: ReviewItem, itemContext?: ReviewItemContext) {
  if (itemContext) return coverageLabel[itemContext.coverage] || itemContext.coverage
  return item.claim?.target_path ? '字段已定位' : coverageLabel.unmapped
}

const supportLabel: Record<string, string> = {
  supported: '模型核验：支持',
  partial: '核验结论：部分支持',
  unsupported: '核验结论：不支持',
  contradicted: '模型核验：存在冲突',
  ambiguous: '模型核验：不确定',
  unknown: '尚未判断支持度',
}

const runStatusLabel: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  waiting_user: '等你处理',
  waiting_external: '等你处理',
  validating: '校验中',
  succeeded: '已完成',
  failed: '执行失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

function runStatusClass(status: string, healthStatus?: string) {
  if (status === 'running' && healthStatus === 'stalled') return 'bg-warn-soft text-[#8a6417]'
  if (status === 'succeeded') return 'bg-ok-soft text-ok'
  if (status === 'failed' || status === 'interrupted') return 'bg-danger-soft text-danger'
  if (status === 'waiting_external' || status === 'waiting_user') return 'bg-warn-soft text-[#8a6417]'
  return 'bg-brand-soft text-brand-dark'
}

function formatReviewTime(value: string | null | undefined) {
  if (!value) return '时间未记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

function supportClass(value: string) {
  if (value === 'supported') return 'bg-ok-soft text-ok'
  if (value === 'unsupported' || value === 'contradicted') return 'bg-danger-soft text-danger'
  if (value === 'partial' || value === 'ambiguous') return 'bg-warn-soft text-[#8a6417]'
  return 'bg-bg-inset text-ink-3'
}

function supportPresentation(itemContext?: ReviewItemContext) {
  if (!itemContext) return { label: supportLabel.unknown, className: supportClass('unknown') }
  if (itemContext.support_basis === 'historical_failure') {
    if (itemContext.alternative_evidence_found) {
      return { label: '历史未通过 · 已发现候选，待模型复核', className: 'bg-warn-soft text-[#8a6417]' }
    }
    return { label: `历史核验：${supportLabel[itemContext.support_status].replace(/^.*：/, '')}`, className: supportClass(itemContext.support_status) }
  }
  if (itemContext.support_basis === 'unassessed') {
    return { label: '尚未进行当前证据复核', className: supportClass('unknown') }
  }
  return { label: supportLabel[itemContext.support_status], className: supportClass(itemContext.support_status) }
}

function humanizeReviewText(text: string) {
  return text
    .replace(/\bhighlight\b/g, '路线描述（highlight 字段）')
    .replace(/\bsuggested_duration\b/g, '建议时长（suggested_duration 字段）')
    .replace(/\bsources\b/g, '引用来源（sources 字段）')
    .replace(/\bsegment\b/g, '途经点条目（segment）')
}

function evidenceActionText(action: string | null | undefined, refs: string[]) {
  if (action === 'add_source') return `补充引用：${refs.join('、') || '模型未列出具体来源'}`
  if (action === 'repair_reference') return `修正引用为：${refs.join('、') || '模型未列出具体来源'}`
  if (action === 'sync_status') return '正文与引用保持不变，仅同步核验状态。'
  return '引用保持不变。'
}

function hasKeepActionConflict(claim: ReviewClaimEvidence | null) {
  if (!claim || claim.content_action !== 'keep') return false
  return /(删除|取消|移除|收窄|改为|不应保留|不再保留)/.test(`${claim.rationale} ${claim.proposed_text}`)
}

function isJsonObject(value: unknown): value is Record<string, JsonValue> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function asJsonValue(value: unknown): JsonValue {
  if (value === undefined) return null
  return JSON.parse(JSON.stringify(value)) as JsonValue
}

function jsonValueKind(value: JsonValue) {
  if (value === null) return 'null'
  if (Array.isArray(value)) return 'array'
  if (typeof value === 'object') return 'object'
  return typeof value
}

function StructuredValueEditor({ value, onChange, depth = 0 }: { value: JsonValue; onChange: (value: JsonValue) => void; depth?: number }) {
  if (typeof value === 'string') {
    const multiline = value.length > 72 || value.includes('\n')
    return multiline
      ? <textarea value={value} onChange={(event) => onChange(event.target.value)} className="w-full min-h-24 rounded-md border border-border bg-surface px-2.5 py-2 text-xs" />
      : <input value={value} onChange={(event) => onChange(event.target.value)} className="w-full rounded-md border border-border bg-surface px-2.5 py-2 text-xs" />
  }
  if (typeof value === 'number') {
    return <input type="number" value={value} onChange={(event) => onChange(Number(event.target.value))} className="w-full rounded-md border border-border bg-surface px-2.5 py-2 text-xs" />
  }
  if (typeof value === 'boolean') {
    return <label className="inline-flex items-center gap-2 text-xs text-ink-2"><input type="checkbox" checked={value} onChange={(event) => onChange(event.target.checked)} />{value ? '是' : '否'}</label>
  }
  if (value === null) {
    return <div className="rounded-md bg-bg-inset px-2.5 py-2 text-xs font-mono text-ink-3">null（保持空值类型）</div>
  }
  if (Array.isArray(value)) {
    const stringItems = value.every((item) => typeof item === 'string')
    return (
      <div className="space-y-2">
        {value.length === 0 && <div className="text-xs text-ink-3">空数组</div>}
        {value.map((item, index) => (
          <div key={index} className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <StructuredValueEditor value={item} onChange={(next) => onChange(value.map((entry, entryIndex) => entryIndex === index ? next : entry))} depth={depth + 1} />
            </div>
            <button type="button" title="移除此项" onClick={() => onChange(value.filter((_, entryIndex) => entryIndex !== index))} className="shrink-0 rounded-md p-2 text-ink-3 hover:bg-danger-soft hover:text-danger"><Icon name="trash" className="h-3.5 w-3.5" /></button>
          </div>
        ))}
        {stringItems && (
          <button type="button" onClick={() => onChange([...value, ''])} className="inline-flex items-center gap-1 rounded-md bg-bg-inset px-2 py-1.5 text-xs font-medium text-ink-2"><Icon name="plus" className="h-3.5 w-3.5" />添加一项</button>
        )}
      </div>
    )
  }
  return (
    <div className={`space-y-2 ${depth > 0 ? 'border-l border-border pl-3' : ''}`}>
      {Object.entries(value).map(([key, fieldValue]) => (
        <div key={key} className="grid grid-cols-[minmax(90px,0.35fr)_minmax(0,1fr)] items-start gap-2">
          <label className="pt-2 text-[11px] font-mono text-ink-3 break-all">{key}</label>
          <StructuredValueEditor value={fieldValue} onChange={(next) => onChange({ ...value, [key]: next })} depth={depth + 1} />
        </div>
      ))}
    </div>
  )
}

function ExpandableEvidenceText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  const mayBeTruncated = text.length > 120
  return (
    <div className="mt-0.5">
      <div className={expanded ? 'whitespace-pre-wrap break-words' : 'line-clamp-4'}>{text}</div>
      {mayBeTruncated && (
        <button type="button" onClick={() => setExpanded((value) => !value)} className="mt-1 text-[11px] font-medium text-brand-dark hover:underline">
          {expanded ? '收起素材片段' : '展开完整素材片段'}
        </button>
      )}
    </div>
  )
}

const originCheckScopeLabel: Record<string, string> = {
  source_availability: '引用可读性',
  entity_identity: '实体身份',
  substantive_claim: '表述完整性',
  route_feasibility: '路线可执行性',
}

function originCheckPresentation(result: string) {
  if (result === 'pass' || result === 'passed') return { label: '通过', className: 'bg-ok-soft text-ok', dot: 'bg-ok' }
  if (['fail', 'failed', 'error', 'unsupported'].includes(result)) return { label: '失败', className: 'bg-danger-soft text-danger', dot: 'bg-danger' }
  return { label: '待判断', className: 'bg-warn-soft text-[#8a6417]', dot: 'bg-warn' }
}

function HighlightedClaim({ text, terms }: { text: string; terms: string[] }) {
  const matchedTerms = [...new Set(terms.filter((term) => term && text.includes(term)))].sort((a, b) => b.length - a.length)
  if (matchedTerms.length === 0) return <>{text}</>
  const pattern = new RegExp(`(${matchedTerms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'g')
  const marked = new Set(matchedTerms)
  return <>{text.split(pattern).map((part, index) => marked.has(part) ? <mark key={`${part}-${index}`} className="rounded-sm bg-danger-soft px-0.5 text-danger">{part}</mark> : part)}</>
}

function OriginClaimCard({ origin, projectId }: { origin: OriginClaimContext; projectId: string }) {
  const failedCount = origin.checks.filter((check) => ['fail', 'failed', 'error', 'unsupported'].includes(check.result)).length
  const passedCount = origin.checks.filter((check) => ['pass', 'passed'].includes(check.result)).length
  const currentValueDiffers = typeof origin.current_value === 'string' && origin.current_value.trim() !== origin.claim_text.trim()
  const trail = [origin.agent_label, origin.day ? `Day ${origin.day}` : '', origin.subject, origin.field_label].filter(Boolean)
  return (
    <section className="overflow-hidden rounded-lg border border-danger/25 bg-surface">
      <div className="border-b border-border bg-danger-soft/20 px-3.5 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-xs font-semibold text-danger">这条问题来自哪里</div>
            <div className="mt-1 text-sm font-medium leading-relaxed text-ink-1">
              这不是一条孤立的天级告警。它来自「{origin.subject || origin.field_label}」这条 Claim；下列校验失败后，被汇总为「{origin.finding_scope_label}」复核。
            </div>
          </div>
          <Link to={`/projects/${projectId}/outputs`} target="_blank" rel="noreferrer" className="shrink-0 text-xs font-semibold text-brand-dark hover:underline">在新标签查看 Agent 产出</Link>
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-[11px] font-medium text-ink-2" aria-label="问题来源链">
          {trail.map((item, index) => (
            <span key={`${item}-${index}`} className="contents">
              {index > 0 && <Icon name="chevronRight" className="h-3 w-3 text-ink-3" />}
              <span className="rounded bg-surface px-2 py-1 shadow-sm ring-1 ring-border">{item}</span>
            </span>
          ))}
        </div>
      </div>

      <div className="space-y-3 px-3.5 py-3">
        <div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-[11px] font-semibold text-ink-3">当时送检的完整 Claim</div>
            <div className="flex gap-1.5 text-[10px]">
              {passedCount > 0 && <span className="rounded bg-ok-soft px-1.5 py-0.5 text-ok">通过 {passedCount}</span>}
              {failedCount > 0 && <span className="rounded bg-danger-soft px-1.5 py-0.5 text-danger">失败 {failedCount}</span>}
            </div>
          </div>
          <blockquote className="mt-1.5 border-l-2 border-danger/50 pl-3 text-sm leading-6 text-ink-1">
            <HighlightedClaim text={origin.claim_text || '未记录 Claim 原文'} terms={origin.missing_entities} />
          </blockquote>
        </div>

        {currentValueDiffers && (
          <div className="rounded-md bg-bg-inset px-3 py-2 text-xs text-ink-2">
            <div className="font-semibold text-ink-1">Agent 当前展示字段</div>
            <div className="mt-1 leading-relaxed">{String(origin.current_value)}</div>
            <div className="mt-1 text-[11px] text-ink-3">当前字段与当时送检 Claim 不同，复核时请分开判断。</div>
          </div>
        )}

        <div>
          <div className="mb-1.5 text-[11px] font-semibold text-ink-3">校验拆解 · {origin.checks.length} 项</div>
          <div className="divide-y divide-border rounded-md border border-border">
            {origin.checks.map((check, index) => {
              const presentation = originCheckPresentation(check.result)
              return (
                <div key={`${check.scope}-${index}`} className="px-3 py-2.5">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className={`h-1.5 w-1.5 rounded-full ${presentation.dot}`} />
                    <span className="font-semibold text-ink-1">{originCheckScopeLabel[check.scope] || check.scope}</span>
                    <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${presentation.className}`}>{presentation.label}</span>
                    {check.cited_source_count > 0 && <span className="text-[10px] text-ink-3">检查 {check.cited_source_count} 条引用</span>}
                  </div>
                  <div className="mt-1.5 pl-3.5 text-xs leading-relaxed text-ink-2">{check.reason || '未记录具体原因。'}</div>
                </div>
              )
            })}
          </div>
        </div>

        <details className="text-[11px] text-ink-3">
          <summary className="cursor-pointer font-medium">展开技术定位</summary>
          <div className="mt-1.5 break-all font-mono">{origin.json_pointer || origin.target_path} · {origin.claim_id}</div>
        </details>
      </div>
    </section>
  )
}

function ReviewItemNavRow({ item, selected, decision, itemContext, onSelect }: {
  item: ReviewItem
  selected: boolean
  decision?: ReviewItemDecision
  itemContext?: ReviewItemContext
  onSelect: () => void
}) {
  const canDecide = Boolean(itemContext?.ready_for_human_review)
  const support = supportPresentation(itemContext)
  return (
    <button type="button" onClick={onSelect} className={`w-full text-left rounded-md border px-2.5 py-2 transition-colors ${selected ? 'border-brand bg-brand-soft/40' : 'border-border bg-surface hover:bg-bg-inset'}`}>
      <div className="text-xs font-medium text-ink-1 line-clamp-2" title={item.label || `建议 ${item.index + 1}`}>{humanizeReviewText(item.label || `建议 ${item.index + 1}`)}</div>
      <div className="mt-0.5 text-[10px] text-ink-3">摘要，完整建议见右侧</div>
      <div className="mt-1 flex flex-wrap gap-1.5 text-[11px]">
        <span className={`rounded px-1.5 py-0.5 ${canDecide || item.claim?.target_path ? 'bg-brand-soft text-brand-dark' : 'bg-warn-soft text-[#8a6417]'}`}>{itemCoverageLabel(item, itemContext)}</span>
        {itemContext && <span className={`rounded px-1.5 py-0.5 ${support.className}`}>{support.label}</span>}
        {decision ? <span className="rounded bg-ok-soft px-1.5 py-0.5 text-ok">{itemDecisionLabel[decision.disposition]}</span> : <span className="rounded bg-bg-inset px-1.5 py-0.5 text-ink-3">未处理</span>}
      </div>
    </button>
  )
}

function ReviewItemDetailPanel({ item, itemContext, decision, proposedOperation, allowDecision, editingItemKey, editedItemValue, busyItemKey, onSetEditedItemValue, onStartEdit, onCancelEdit, onDecide }: {
  item: ReviewItem
  itemContext?: ReviewItemContext
  decision?: ReviewItemDecision
  proposedOperation?: ReviewProposedOperation
  allowDecision: boolean
  editingItemKey: string | null
  editedItemValue: JsonValue
  busyItemKey: string | null
  onSetEditedItemValue: (value: JsonValue) => void
  onStartEdit: (value: JsonValue) => void
  onCancelEdit: () => void
  onDecide: (disposition: ReviewItemDisposition, editedValue?: JsonValue) => void
}) {
  const claim = item.claim
  const currentValue = itemContext?.current_value
  const canDecide = allowDecision && Boolean(itemContext?.ready_for_human_review)
  const isEditing = editingItemKey === item.key
  const isBusy = busyItemKey === item.key
  const support = supportPresentation(itemContext)
  const keepActionConflict = hasKeepActionConflict(claim)
  const canAcceptDirectly = canDecide && !keepActionConflict
  const suggestedEditValue = decision?.has_edited_value
    ? decision.edited_value
    : proposedOperation && proposedOperation.operation !== 'remove'
      ? proposedOperation.proposed_value
      : typeof currentValue === 'string' && claim?.proposed_text
        ? claim.proposed_text
        : currentValue
  const canEditValue = canDecide && proposedOperation?.operation !== 'remove' && suggestedEditValue !== undefined
  const editPath = proposedOperation?.path || itemContext?.target.json_pointer || claim?.target_path || ''
  const afterText = decision?.disposition === 'edited' && decision.edited_text
    ? decision.edited_text
    : decision?.disposition === 'keep_original' || claim?.content_action === 'keep'
      ? ''
      : claim?.proposed_text || ''
  return (
    <div className="space-y-2">
      <div className="space-y-2">
        <div className="min-w-0">
          <div className="text-[11px] text-ink-3">模型针对当前内容提出的处理建议</div>
          <div className="mt-0.5 text-sm font-medium text-ink-1 whitespace-pre-wrap break-words">{humanizeReviewText(item.label || `建议 ${item.index + 1}`)}</div>
        </div>
        <div className="flex flex-wrap gap-1.5 text-[11px]">
          {claim && <span className="rounded bg-bg-inset px-1.5 py-0.5 text-ink-2">模型判断：{claim.verdict || '未确定'}</span>}
          <span className={`rounded px-1.5 py-0.5 ${canDecide || claim?.target_path ? 'bg-brand-soft text-brand-dark' : 'bg-warn-soft text-[#8a6417]'}`}>{itemCoverageLabel(item, itemContext)}</span>
          {itemContext && <span className={`rounded px-1.5 py-0.5 ${support.className}`}>{support.label}</span>}
          {decision && <span className="rounded bg-ok-soft px-1.5 py-0.5 text-ok">人工：{itemDecisionLabel[decision.disposition]}</span>}
        </div>
      </div>
      {(itemContext?.target.json_pointer || claim?.target_path) && <div className="text-[11px] text-ink-3">当前字段：{itemContext?.target.json_pointer || claim?.target_path}</div>}
      {currentValue !== null && currentValue !== undefined && (
        <div className="rounded-md bg-bg-inset px-2.5 py-2 text-xs text-ink-2 space-y-1">
          <div className="font-semibold text-ink-1">当前 JSON 内容（尚未修改）</div>
          {isJsonObject(currentValue) && currentValue.point_name != null && <div><span className="text-ink-3">途经点：</span>{String(currentValue.point_name)}</div>}
          {isJsonObject(currentValue) && currentValue.highlight != null && <div><span className="text-ink-3">描述：</span>{String(currentValue.highlight)}</div>}
          {isJsonObject(currentValue) && currentValue.suggested_duration != null && <div><span className="text-ink-3">建议时长：</span>{String(currentValue.suggested_duration)}</div>}
          {(!isJsonObject(currentValue) || (!('point_name' in currentValue) && !('highlight' in currentValue))) && <div className="whitespace-pre-wrap break-words font-mono">{JSON.stringify(currentValue, null, 2)}</div>}
        </div>
      )}
      {itemContext && !currentValue && <div className="rounded-md bg-warn-soft/30 px-2.5 py-2 text-xs text-[#8a6417]">没有定位到该建议对应的当前字段，不能直接据此确认修改。</div>}
      {itemContext && itemContext.failure_points.length > 0 && (
        <details className="rounded-md border border-warn/30 px-2.5 py-2 text-xs text-ink-2">
          <summary className="cursor-pointer font-semibold">为什么被标记 · 命中 {itemContext.failure_points.length} 条历史失败点</summary>
          <div className="mt-1.5 space-y-1.5">
            {itemContext.failure_points.map((failure, failureIndex) => <div key={failureIndex}><span className="text-ink-3">第 {failure.round} 轮：</span>{failure.detail}</div>)}
          </div>
        </details>
      )}
      {itemContext && (
        <div className="rounded-md border border-brand/20 px-2.5 py-2 text-xs text-ink-2 space-y-1.5">
          <div className="font-semibold text-ink-1">当前引用 · 已取回 {itemContext.evidence_excerpts.length}/{itemContext.evidence_refs.length}</div>
          <div className="text-[11px] text-ink-3">
            来源：{itemContext.evidence_origin === 'model_review' ? '本次模型复核实际读取的 evidence_refs' : '当前 JSON 字段的 sources'}。引用被挂载或成功读取，不等于原文支持当前全部表述。
            这里展示的是原始素材中检索到的 chunk 片段，不是整条视频或整份文档。
          </div>
          {itemContext.support_basis === 'historical_failure' && itemContext.alternative_evidence_found && (
            <div className="rounded-md bg-warn-soft/40 px-2 py-1.5 text-[11px] text-[#8a6417]">
              当前“不通过”只针对原字段挂载的旧引用。系统已经找到其他候选素材，但尚未由当前模型判断能否补挂或替换，不能据此删除途经点。
            </div>
          )}
          {itemContext.evidence_excerpts.slice(0, 1).map((evidence) => <div key={evidence.ref} className="border-l-2 border-brand/40 pl-2"><span className="font-medium">{evidence.ref}：</span><ExpandableEvidenceText text={evidence.text} /></div>)}
          {itemContext.evidence_excerpts.length > 1 && (
            <details>
              <summary className="cursor-pointer font-medium text-brand-dark">查看其余 {itemContext.evidence_excerpts.length - 1} 条原文</summary>
              <div className="mt-1.5 space-y-2">{itemContext.evidence_excerpts.slice(1).map((evidence) => <div key={evidence.ref} className="border-l-2 border-brand/40 pl-2"><span className="font-medium">{evidence.ref}：</span>{evidence.text}</div>)}</div>
            </details>
          )}
          {itemContext.model_evidence_excerpt && itemContext.evidence_excerpts.length === 0 && <div className="border-l-2 border-brand/40 pl-2">模型引用摘录：{itemContext.model_evidence_excerpt}</div>}
          {itemContext.unresolved_evidence_refs.length > 0 && <div className="text-warn">尚未取回原文：{itemContext.unresolved_evidence_refs.join('、')}</div>}
          {itemContext.evidence_refs.length === 0 && <div className="text-warn">当前字段没有挂载引用，不能仅凭旧建议作确定性修改。</div>}
          {itemContext.related_evidence_candidates.length > 0 && (
            <details className="border-t border-border pt-1.5">
              <summary className="cursor-pointer font-medium text-brand-dark">同文件其他引用候选 · {itemContext.related_evidence_candidates.length} 条</summary>
              <div className="mt-1.5 text-[11px] text-ink-3">来自当前 JSON 文件的其他 sources，只是命中了相关词，不代表能够支持完整主张。</div>
              <div className="mt-2 space-y-2">
                {itemContext.related_evidence_candidates.map((evidence) => (
                  <div key={evidence.ref} className="border-l-2 border-border pl-2">
                    <div className="font-medium text-ink-2">{evidence.ref} · 命中：{evidence.matched_terms.join('、')}</div>
                    <ExpandableEvidenceText text={evidence.text} />
                  </div>
                ))}
              </div>
            </details>
          )}
          {itemContext.library_evidence_candidates.length > 0 && (
            <details className="border-t border-border pt-1.5">
              <summary className="cursor-pointer font-medium text-brand-dark">整个目的地素材库候选 · {itemContext.library_evidence_candidates.length} 条</summary>
              <div className="mt-1.5 text-[11px] text-ink-3">系统使用本地 BM25 检索全部目的地 chunks。它们尚未被当前字段引用，只是待模型或人工复核的召回结果。</div>
              <div className="mt-2 space-y-2">
                {itemContext.library_evidence_candidates.map((evidence) => (
                  <div key={evidence.ref} className="border-l-2 border-border pl-2">
                    <div className="flex flex-wrap items-center gap-1.5 font-medium text-ink-2">
                      <span>{evidence.ref}</span>
                      {evidence.same_document && <span className="rounded bg-brand-soft px-1 py-0.5 text-[10px] text-brand-dark">同一原始文档</span>}
                      {evidence.matched_terms.length > 0 && <span className="text-ink-3">命中：{evidence.matched_terms.join('、')}</span>}
                    </div>
                    <ExpandableEvidenceText text={evidence.text} />
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}
      {claim && (
        <div className="rounded-md border border-border px-2.5 py-2 text-xs text-ink-2">
          <div className="font-semibold text-ink-1">模型本次实际使用的证据</div>
          <div className="mt-1 text-[11px] text-ink-3 break-words">引用：{claim.evidence_refs.join('、') || '未记录'}</div>
          <div className="mt-1.5 border-l-2 border-brand/40 pl-2 whitespace-pre-wrap break-words">{claim.evidence_excerpt || '模型未返回可定位的证据摘录。'}</div>
        </div>
      )}
      {claim?.original_text && <div className="text-xs text-ink-2"><span className="text-ink-3">模型核验的当前表述：</span>{humanizeReviewText(claim.original_text)}</div>}
      {claim?.rationale && <div className="text-xs text-ink-2"><span className="text-ink-3">模型理由：</span>{claim.rationale}</div>}
      {claim && <div className="flex flex-wrap gap-1.5 text-[11px] text-ink-3"><span>正文动作：{claim.content_action || claim.action || '人工确认'}</span><span>引用动作：{claim.evidence_action || (['add_source', 'repair_reference', 'sync_status'].includes(claim.action) ? claim.action : 'keep')}</span><span>置信度：{claim.confidence || 'medium'}</span></div>}
      {claim && (
        <div className="rounded-md border border-ok/30 bg-ok-soft/20 px-2.5 py-2 text-xs text-ink-2 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="font-semibold text-ink-1">修改后预览</div>
            <span className="text-[11px] text-ink-3">尚未应用到 JSON</span>
          </div>
          <div>
            <div className="text-[11px] font-medium text-ink-3">正文</div>
            {afterText ? (
              <div className="mt-0.5 whitespace-pre-wrap break-words">{humanizeReviewText(afterText)}</div>
            ) : (
              <div className="mt-0.5">
                {decision?.disposition === 'keep_original' ? '人工选择保留当前正文，不做文字修改。' : '模型建议正文保持当前内容，不做文字修改。'}
              </div>
            )}
          </div>
          <div>
            <div className="text-[11px] font-medium text-ink-3">引用与状态</div>
            <div className="mt-0.5 break-words">{evidenceActionText(claim.evidence_action, claim.evidence_refs)}</div>
          </div>
          {keepActionConflict && (
            <div className="rounded bg-danger-soft/50 px-2 py-1.5 text-danger">
              模型输出存在冲突：正文动作要求保持不变，但理由中又要求删除、取消或收窄内容。该条不能直接采纳，请编辑修改后正文或重新复核。
            </div>
          )}
        </div>
      )}
      {!allowDecision && <div className="rounded-md border border-warn/30 bg-warn-soft/20 px-2.5 py-2 text-xs text-[#8a6417]">当前查看的是候选建议。先设为当前推荐，再进行逐条人工确认。</div>}
      {decision?.has_edited_value && <div className="rounded-md bg-ok-soft/40 px-2.5 py-2 text-xs text-ink-2"><div className="text-ink-3">人工结构化修改值：</div><pre className="mt-1 whitespace-pre-wrap break-words font-mono">{JSON.stringify(decision.edited_value, null, 2)}</pre></div>}
      {!decision?.has_edited_value && decision?.edited_text && <div className="rounded-md bg-ok-soft/40 px-2.5 py-2 text-xs text-ink-2"><span className="text-ink-3">人工修改稿：</span>{decision.edited_text}</div>}
      {isEditing && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs font-semibold text-ink-1">编辑结构化修改值</div>
            <span className="text-[11px] text-ink-3">{jsonValueKind(editedItemValue)} · {editPath}</span>
          </div>
          <StructuredValueEditor value={editedItemValue} onChange={onSetEditedItemValue} />
          <div className="flex gap-2">
            <button disabled={isBusy} onClick={() => onDecide('edited', editedItemValue)} className="rounded-md bg-ok-soft px-2.5 py-1.5 text-xs font-semibold text-ok disabled:opacity-50">保存修改值</button>
            <button onClick={onCancelEdit} className="rounded-md bg-bg-inset px-2.5 py-1.5 text-xs text-ink-2">取消</button>
          </div>
        </div>
      )}
      {!isEditing && (
        <div className="flex flex-wrap gap-2 pt-1">
          <button disabled={isBusy || !canAcceptDirectly} onClick={() => onDecide('accepted')} className="rounded-md bg-ok-soft px-2.5 py-1.5 text-xs font-semibold text-ok disabled:opacity-50">采纳方案</button>
          <button disabled={isBusy || !canDecide} onClick={() => onDecide('keep_original')} className="rounded-md bg-brand-soft px-2.5 py-1.5 text-xs font-semibold text-brand-dark disabled:opacity-50">保留原文</button>
          <button disabled={isBusy || !canEditValue} onClick={() => onStartEdit(asJsonValue(suggestedEditValue))} className="rounded-md bg-bg-inset px-2.5 py-1.5 text-xs font-semibold text-ink-2 disabled:opacity-50">编辑修改值</button>
          <button disabled={isBusy} onClick={() => onDecide('deferred')} className="rounded-md bg-warn-soft px-2.5 py-1.5 text-xs font-semibold text-[#8a6417] disabled:opacity-50">暂缓</button>
          <button disabled={isBusy} onClick={() => onDecide('needs_rerun')} className="rounded-md bg-danger-soft px-2.5 py-1.5 text-xs font-semibold text-danger disabled:opacity-50">需重跑</button>
        </div>
      )}
    </div>
  )
}

export function ReviewCenterPage() {
  const { data: projects, isLoading: projectsLoading } = useProjects()
  const { projectId: routeProjectId } = useParams()
  const [searchParams] = useSearchParams()
  const { data: globalFindings } = useGlobalReviewFindings(true)
  const [projectId, setProjectId] = useState(searchParams.get('project') ?? '')
  const queryClient = useQueryClient()
  const selectedId = routeProjectId || projectId || globalFindings?.[0]?.project_id || projects?.[0]?.project_id || ''
  const { data: findings, isLoading, error } = useReviewFindings(selectedId)
  const queueQuery = useReviewQueue(selectedId)
  const stateQuery = useReviewState(selectedId)
  const validation = useReviewValidation(selectedId)
  const candidatesQuery = useGlobalOptimizationCandidates()
  const [filter, setFilter] = useState<'all' | 'open' | 'needs_rerun'>('open')
  const [selectedFinding, setSelectedFinding] = useState<ReviewFinding | null>(null)
  const [busy, setBusy] = useState(false)
  const [modelError, setModelError] = useState<string | null>(null)
  const [runMode, setRunMode] = useState<'standard' | 'codex' | 'compare'>('standard')
  const [runBusy, setRunBusy] = useState(false)
  const [deterministicRepairBusy, setDeterministicRepairBusy] = useState(false)
  const [runStatusFilter, setRunStatusFilter] = useState<'all' | 'active' | 'attention'>('all')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedRecommendationId, setSelectedRecommendationId] = useState<string | null>(null)
  const [busyItemKey, setBusyItemKey] = useState<string | null>(null)
  const [editingItemKey, setEditingItemKey] = useState<string | null>(null)
  const [editedItemValue, setEditedItemValue] = useState<JsonValue>(null)
  const [selectedItemKey, setSelectedItemKey] = useState<string | null>(null)
  const [pendingPatch, setPendingPatch] = useState<ReviewPatch | null>(null)
  const [pageTab, setPageTab] = useState<'review' | 'optimization' | 'scan'>('review')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [drawerTab, setDrawerTab] = useState<'decide' | 'runs' | 'evidence'>('decide')
  const recommendationQuery = useReviewRecommendation(selectedId, selectedFinding?.finding_id ?? '', selectedFinding?.recommendation_id ?? null)
  const recommendationHistoryQuery = useReviewRecommendationHistory(selectedId, selectedFinding?.finding_id ?? '')
  const contextQuery = useReviewContext(selectedId, selectedFinding?.finding_id ?? '')
  const manifestConsistency = contextQuery.data?.manifest_consistency_context ?? null
  const isManifestConsistencyFinding = selectedFinding?.problem_type === 'manifest_mismatch'
  const executorsQuery = useReviewExecutors()
  const runsQuery = useReviewRuns(selectedId, selectedFinding?.finding_id ?? '')
  const visibleRuns = useMemo(() => {
    const runs = runsQuery.data ?? []
    if (runStatusFilter === 'active') return runs.filter((run) => ['queued', 'running', 'validating'].includes(run.status))
    if (runStatusFilter === 'attention') return runs.filter((run) => ['waiting_user', 'waiting_external', 'failed', 'interrupted'].includes(run.status) || run.health_status === 'stalled')
    return runs
  }, [runsQuery.data, runStatusFilter])
  const selectedRun = (runsQuery.data ?? []).find((run) => run.run_id === selectedRunId) ?? null
  const runRecommendationSignature = (runsQuery.data ?? []).map((run) => `${run.run_id}:${run.recommendation_id ?? ''}:${run.status}`).join('|')
  const candidateRecommendationQuery = useReviewRecommendationById(
    selectedId,
    selectedRecommendationId && selectedRecommendationId !== selectedFinding?.recommendation_id
      ? selectedRecommendationId
      : null,
  )
  const recommendation = candidateRecommendationQuery.data ?? recommendationQuery.data
  const isCurrentRecommendation = Boolean(recommendation && recommendation.recommendation_id === selectedFinding?.recommendation_id)
  const comparisonQuery = useReviewComparison(selectedId, selectedRun?.comparison_group_id ?? null)
  const itemDecisionsQuery = useReviewItemDecisions(selectedId, selectedFinding?.finding_id ?? '', recommendation?.recommendation_id ?? null)
  const itemDecisionMap = useMemo(
    () => new Map((itemDecisionsQuery.data ?? []).map((item) => [item.item_key, item])),
    [itemDecisionsQuery.data],
  )
  const modelReviewWasCompleted = selectedFinding ? findingModelWasInvolved(selectedFinding, recommendation) : false
  const modelWasAttempted = Boolean(runsQuery.data?.length)
  const modelReviewProgress = reviewProgressPresentation({
    selectedStatus: selectedRun?.status ?? null,
    hasRecommendation: Boolean(recommendation),
    hasHistoricalCompletion: modelReviewWasCompleted,
    hasSuccessfulRun: Boolean(runsQuery.data?.some((run) => run.status === 'succeeded' && run.recommendation_id)),
    hasFailedRun: Boolean(runsQuery.data?.some((run) => ['failed', 'interrupted'].includes(run.status))),
  })
  const modelReviewProgressClass = modelReviewProgress.tone === 'complete'
    ? 'text-ok font-medium'
    : modelReviewProgress.tone === 'failed'
      ? 'text-danger font-medium'
      : modelReviewProgress.tone === 'idle'
        ? 'text-ink-3'
        : 'text-brand-dark font-medium'
  const selectedRunExecutorLabel = selectedRun?.executor_id === 'codex_skill' ? 'Codex 任务包' : '标准模型'
  const runActionLabel = runMode === 'codex' ? '创建任务包' : runMode === 'compare' ? '创建对照运行' : '开始复核'
  const reviewItems = useMemo(() => {
    const claims = recommendation?.claim_reviews ?? []
    if (claims.length > 0) {
      return claims.map((claim, index) => ({
        key: `claim:${index}`,
        index,
        kind: 'claim' as const,
        label: claim.claim,
        claim,
      }))
    }
    return legacySuggestionItems(recommendation?.proposed_text ?? '').map((label, index) => ({
      key: `legacy:${index}`,
      index,
      kind: 'legacy_suggestion' as const,
      label,
      claim: null,
    }))
  }, [recommendation])
  const decidedItemCount = reviewItems.filter((item) => itemDecisionMap.has(item.key)).length
  const prevDecidedCountRef = useRef(decidedItemCount)
  const queueListRef = useRef<HTMLDivElement>(null)
  const itemContextMap = useMemo(
    () => new Map((isCurrentRecommendation ? contextQuery.data?.review_item_contexts ?? [] : []).map((item) => [item.item_key, item])),
    [contextQuery.data?.review_item_contexts, isCurrentRecommendation],
  )
  const reviewReadyCount = reviewItems.filter((item) => itemContextMap.get(item.key)?.ready_for_human_review).length

  useEffect(() => {
    if (selectedFinding && runRecommendationSignature) void recommendationHistoryQuery.refetch()
    // recommendation history is append-only; refresh when a run gains a result or changes terminal state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runRecommendationSignature, selectedFinding?.finding_id])

  useEffect(() => {
    if (reviewItems.length === 0) {
      if (selectedItemKey !== null) setSelectedItemKey(null)
      return
    }
    const stillValid = reviewItems.some((item) => item.key === selectedItemKey)
    if (stillValid) return
    const firstUndecided = reviewItems.find((item) => !itemDecisionMap.has(item.key))
    setSelectedItemKey((firstUndecided ?? reviewItems[0]).key)
  }, [reviewItems, itemDecisionMap, selectedItemKey])

  useEffect(() => {
    if (decidedItemCount <= prevDecidedCountRef.current) {
      prevDecidedCountRef.current = decidedItemCount
      return
    }
    prevDecidedCountRef.current = decidedItemCount
    const next = reviewItems.find((item) => !itemDecisionMap.has(item.key) && item.key !== selectedItemKey)
    if (next) setSelectedItemKey(next.key)
  }, [decidedItemCount, reviewItems, itemDecisionMap, selectedItemKey])

  const findingById = useMemo(() => new Map((findings ?? []).map((item) => [item.finding_id, item])), [findings])
  const visible = useMemo(() => {
    const rows = queueQuery.data ?? []
    if (filter === 'open') return rows.filter((item) => item.effective_status !== 'review_passed')
    if (filter === 'needs_rerun') return rows.filter((item) => findingById.get(item.primary_finding_id)?.disposition === 'needs_rerun')
    return rows
  }, [queueQuery.data, findingById, filter])

  useEffect(() => {
    queueListRef.current?.scrollTo({ top: 0 })
  }, [selectedId, filter])

  async function selectQueueGroup(group: ReviewQueueGroup) {
    const finding = findingById.get(group.primary_finding_id)
    if (!finding) return
    setSelectedFinding(finding)
    setDrawerOpen(true)
    setDrawerTab('decide')
    setSelectedRunId(null)
    setSelectedRecommendationId(null)
    setEditingItemKey(null)
    setEditedItemValue(null)
    setModelError(null)
    setPendingPatch(null)
    if (finding.patch_id) {
      try {
        setPendingPatch(await api.getReviewPatch(selectedId, finding.patch_id))
      } catch {
        setPendingPatch(null)
      }
    }
  }

  useEffect(() => {
    const runs = runsQuery.data ?? []
    if (runs.length === 0) {
      setSelectedRunId(null)
      return
    }
    if (!runs.some((run) => run.run_id === selectedRunId)) setSelectedRunId(runs[0].run_id)
  }, [runsQuery.data, selectedRunId])

  async function startReviewRun() {
    if (!selectedFinding) return
    setRunBusy(true)
    setModelError(null)
    try {
      const runs = await api.createReviewRuns(selectedId, selectedFinding.finding_id, runMode)
      setSelectedRunId(runs[0]?.run_id ?? null)
      setSelectedRecommendationId(null)
      await queryClient.invalidateQueries({ queryKey: ['review-runs', selectedId, selectedFinding.finding_id] })
    } catch (error) {
      setModelError((error as Error).message)
    } finally {
      setRunBusy(false)
    }
  }

  async function applyManifestConsistencyRepair() {
    if (!selectedFinding || !manifestConsistency) return
    setDeterministicRepairBusy(true)
    setModelError(null)
    try {
      const result = await api.applyManifestConsistencyRepair(
        selectedId,
        selectedFinding.finding_id,
        manifestConsistency.observed_fingerprint,
      )
      setSelectedFinding(result.finding)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['review-findings', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['review-queue', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['review-state', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['review-context', selectedId, selectedFinding.finding_id] }),
      ])
    } catch (error) {
      setModelError((error as Error).message)
    } finally {
      setDeterministicRepairBusy(false)
    }
  }

  async function downloadRunBundle(run: ReviewRun) {
    const bundle = await api.getReviewRunBundle(selectedId, run.run_id)
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${run.run_id}-review-bundle.json`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  async function importCodexResult(run: ReviewRun, file: File) {
    setRunBusy(true)
    setModelError(null)
    try {
      const result = JSON.parse(await file.text()) as Record<string, unknown>
      await api.importReviewRunResult(selectedId, run.run_id, result)
      await queryClient.invalidateQueries({ queryKey: ['review-runs', selectedId, run.finding_id] })
    } catch (error) {
      setModelError((error as Error).message)
    } finally {
      setRunBusy(false)
    }
  }

  async function promoteRun(run: ReviewRun) {
    setRunBusy(true)
    setModelError(null)
    try {
      let promoted: ReviewFinding
      try {
        promoted = await api.promoteReviewRun(selectedId, run.run_id)
      } catch (error) {
        const message = (error as Error).message
        if (!message.includes('replace_confirmation_required')) throw error
        const confirmed = window.confirm('当前推荐已有人工逐条决定。替换后旧决定仍保留在历史记录中，但当前处理进度会切换到新建议。确认替换吗？')
        if (!confirmed) return
        promoted = await api.promoteReviewRun(selectedId, run.run_id, true)
      }
      setSelectedFinding(promoted)
      setSelectedRecommendationId(null)
      await queryClient.invalidateQueries({ queryKey: ['review-findings', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-queue', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-recommendation', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-recommendation-history', selectedId, run.finding_id] })
    } catch (error) {
      setModelError((error as Error).message)
    } finally {
      setRunBusy(false)
    }
  }

  async function decideItem(item: { key: string; index: number; kind: 'claim' | 'legacy_suggestion'; label: string }, disposition: ReviewItemDisposition, editedValue?: JsonValue) {
    if (!selectedFinding || !recommendation) return
    setBusyItemKey(item.key)
    try {
      await api.recordReviewItemDecision(selectedId, selectedFinding.finding_id, {
        recommendation_id: recommendation.recommendation_id,
        item_key: item.key,
        item_index: item.index,
        item_kind: item.kind,
        item_label: item.label,
        disposition,
        edited_text: typeof editedValue === 'string' ? editedValue : '',
        edited_value: editedValue,
        has_edited_value: disposition === 'edited',
        actor_type: 'human',
        actor_id: 'admin_user',
      })
      setEditingItemKey(null)
      setEditedItemValue(null)
      await itemDecisionsQuery.refetch()
      await queryClient.invalidateQueries({ queryKey: ['review-findings', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-recommendation-history', selectedId, selectedFinding.finding_id] })
    } finally {
      setBusyItemKey(null)
    }
  }

  async function summarizeItemDecisions(items: Array<{ key: string; kind: 'claim' | 'legacy_suggestion'; index: number }>) {
    if (!selectedFinding || !recommendation) return
    const decisions = items.map((item) => itemDecisionMap.get(item.key)).filter(Boolean)
    if (decisions.length !== items.length) return
    try {
      const result = await api.finalizeReviewItemDecisions(selectedId, selectedFinding.finding_id, recommendation.recommendation_id)
      setSelectedFinding(result.outcome === 'closed' ? null : result.finding)
      setPendingPatch(result.patch_id ? await api.getReviewPatch(selectedId, result.patch_id) : null)
      setModelError(null)
    } catch (error) {
      setModelError((error as Error).message)
      return
    }
    await queryClient.invalidateQueries({ queryKey: ['review-findings', selectedId] })
    await queryClient.invalidateQueries({ queryKey: ['review-queue', selectedId] })
    await queryClient.invalidateQueries({ queryKey: ['review-state', selectedId] })
  }

  async function applyPendingPatch() {
    if (!pendingPatch) return
    setBusy(true)
    try {
      const patch = await api.applyReviewPatch(selectedId, pendingPatch.patch_id)
      setPendingPatch(patch)
      setModelError(null)
      await queryClient.invalidateQueries({ queryKey: ['review-findings', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-queue', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-state', selectedId] })
    } catch (error) {
      setModelError((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function rejectPendingPatch() {
    if (!pendingPatch) return
    setBusy(true)
    try {
      const patch = await api.rejectReviewPatch(selectedId, pendingPatch.patch_id, '人工暂不应用该修改。')
      setPendingPatch(patch)
      setModelError(null)
      await queryClient.invalidateQueries({ queryKey: ['review-findings', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-queue', selectedId] })
    } catch (error) {
      setModelError((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function preparePatchRollback() {
    if (!pendingPatch) return
    setBusy(true)
    try {
      const rollback = await api.rollbackReviewPatch(selectedId, pendingPatch.patch_id)
      setPendingPatch(rollback)
      setSelectedFinding((current) => current ? { ...current, patch_id: rollback.patch_id, status: 'awaiting_apply' } : current)
      setModelError(null)
      await queryClient.invalidateQueries({ queryKey: ['review-findings', selectedId] })
      await queryClient.invalidateQueries({ queryKey: ['review-queue', selectedId] })
    } catch (error) {
      setModelError((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (projectsLoading) return <p className="text-ink-3">加载复核中心…</p>
  if (!selectedId) return <p className="text-ink-3">还没有可复核的项目。</p>

  const selectedGroup = selectedFinding ? (queueQuery.data ?? []).find((group) => group.primary_finding_id === selectedFinding.finding_id) : null
  const currentQueueIndex = selectedFinding ? visible.findIndex((group) => group.primary_finding_id === selectedFinding.finding_id) : -1
  const prevGroup = currentQueueIndex > 0 ? visible[currentQueueIndex - 1] : null
  const nextGroup = currentQueueIndex >= 0 && currentQueueIndex < visible.length - 1 ? visible[currentQueueIndex + 1] : null
  const selectedItem = reviewItems.find((item) => item.key === selectedItemKey) ?? null

  return (
    <div>
      {routeProjectId && <ProjectTabs projectId={routeProjectId} />}
      <div className="flex flex-col items-stretch gap-4 mb-5 min-[900px]:flex-row min-[900px]:items-end min-[900px]:justify-between">
        <div>
          <div className="flex items-center gap-2 text-ink-3 text-xs mb-1"><Icon name="check" className="w-3.5 h-3.5" /> 项目复核与系统演进</div>
          <h1 className="text-2xl font-bold tracking-tight">问题与改进</h1>
          <p className="text-sm text-ink-3 mt-1">先确认具体问题，再决定是否升级为系统改进。</p>
        </div>
        {routeProjectId ? (
          <div className="max-w-full rounded-md border border-border bg-bg-inset px-3 py-2 text-sm text-ink-2">
            {(projects ?? []).find((project) => project.project_id === selectedId)?.destination || '未命名'} · {selectedId.slice(0, 28)}
          </div>
        ) : (
          <select className="w-full border border-border rounded-lg px-3 py-2 text-sm bg-surface min-[900px]:w-auto" value={selectedId} onChange={(event) => { setProjectId(event.target.value); setSelectedFinding(null) }}>
            {(projects ?? []).map((project) => <option key={project.project_id} value={project.project_id}>{project.destination || '未命名'} · {project.project_id.slice(0, 28)}</option>)}
          </select>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-y border-border py-2.5 mb-5 text-xs">
        <span><strong className="text-sm text-ink-1">{(queueQuery.data ?? []).length}</strong> <span className="text-ink-3">真实问题对象</span></span>
        <span><strong className="text-sm text-warn">{(queueQuery.data ?? []).filter((item) => item.effective_status !== 'review_passed').length}</strong> <span className="text-ink-3">待复核</span></span>
        <span><strong className="text-sm text-danger">{(queueQuery.data ?? []).filter((item) => findingById.get(item.primary_finding_id)?.disposition === 'needs_rerun').length}</strong> <span className="text-ink-3">需重跑</span></span>
        <span className={stateQuery.data?.ok ? 'text-ok' : 'text-danger'}>{stateQuery.data?.ok ? '状态扫描正常' : '状态扫描需检查'}</span>
      </div>

      {(validation.preview?.patch_count || validation.status) && (
        <div className="mb-5 border border-border bg-bg-inset px-4 py-3">
          <div className="flex flex-col gap-3 min-[760px]:flex-row min-[760px]:items-center min-[760px]:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-sm font-semibold text-ink-1">
                <Icon name="check" className="h-4 w-4" />
                本批修改验收
              </div>
              <div className="mt-1 text-xs text-ink-3">
                {validation.preview?.patch_count
                  ? `${validation.preview.patch_count} 个已应用 Patch 待验收 · Lifestyle ${validation.preview.lifestyle_items.length} · Pitfall ${validation.preview.pitfall_items.length} · POI ${validation.preview.poi_files.length} · Route Day ${validation.preview.route_days.length}${validation.preview.requires_integration ? ' · 需一次 Integration' : ''}`
                  : validation.status?.validation_status === 'passed'
                    ? '上一批验收已完成，网站已统一重建。'
                    : validation.status?.validation_status === 'failed'
                      ? `上一批验收发现 ${validation.status.issue_count ?? 0} 个问题，已返回复核队列。`
                      : '正在读取批次状态。'}
              </div>
              {validation.preview?.route_structure_patch_ids.length ? <div className="mt-1 text-xs text-danger">包含路线结构修改，已从普通批次隔离；请返回既有路线变更流程处理。</div> : null}
              {validation.preview?.unresolved_patch_ids.length ? <div className="mt-1 text-xs text-danger">有 {validation.preview.unresolved_patch_ids.length} 个 Patch 缺少可验收的精确 Day/字段定位，请先补充问题单。</div> : null}
              {validation.status?.log?.length ? <div className="mt-1 text-[11px] text-ink-3">{validation.status.log.at(-1)}</div> : null}
              {validation.error ? <div className="mt-1 text-xs text-danger">{validation.error.message}</div> : null}
            </div>
            {validation.preview?.patch_count ? (
              <button
                type="button"
                disabled={validation.isStarting || ['queued', 'running'].includes(validation.status?.status ?? '') || !validation.preview.can_start}
                onClick={() => void validation.start()}
                className="shrink-0 rounded-md bg-ok-soft px-3 py-2 text-xs font-semibold text-ok disabled:opacity-50"
              >
                {validation.isStarting || ['queued', 'running'].includes(validation.status?.status ?? '') ? '验收进行中…' : '开始本批验收'}
              </button>
            ) : null}
          </div>
        </div>
      )}

      <Tabs
        className="mb-5"
        active={pageTab}
        onChange={(key) => setPageTab(key as typeof pageTab)}
        tabs={[
          { key: 'review', label: '待复核问题', badge: (queueQuery.data ?? []).filter((item) => item.effective_status !== 'review_passed').length || undefined },
          { key: 'optimization', label: '系统改进', badge: (candidatesQuery.data ?? []).length || undefined },
          { key: 'scan', label: '状态一致性', badge: stateQuery.data && !stateQuery.data.ok ? '需处理' : undefined },
        ]}
      />

      {pageTab === 'review' && (
        <Card className="overflow-hidden">
          <div className="px-5 py-3.5 border-b border-border flex items-center justify-between">
            <div className="font-semibold text-sm">问题队列</div>
            <select className="border border-border rounded-md px-2 py-1 text-xs bg-surface" value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}>
              <option value="open">待处理</option><option value="needs_rerun">需重跑</option><option value="all">全部</option>
            </select>
          </div>
          {isLoading || queueQuery.isLoading ? <p className="p-4 text-sm text-ink-3">加载中…</p> : error || queueQuery.error ? <p className="p-4 text-sm text-danger">加载失败：{((error || queueQuery.error) as Error).message}</p> : visible.length === 0 ? <p className="p-4 text-sm text-ink-3">当前没有匹配的问题。</p> : (
            <div ref={queueListRef} className="review-queue-list divide-y divide-border">
              {visible.map((group) => {
                const finding = findingById.get(group.primary_finding_id)
                if (!finding) return null
                return (
                  <div key={group.group_id} className="review-queue-row">
                    <button type="button" onClick={() => void selectQueueGroup(group)} className={`review-queue-item w-full text-left px-5 py-4 hover:bg-bg-inset transition-colors ${selectedFinding?.finding_id === finding.finding_id ? 'bg-brand-soft/40' : ''}`}>
                      <div className="flex items-start justify-between gap-3 mb-1.5"><span className="min-w-0 font-semibold text-sm leading-snug">{group.title}</span><Badge variant={dispositionVariant(finding.disposition)}>{dispositionLabel[finding.disposition] ?? finding.disposition}</Badge></div>
                      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-3 mb-1.5">
                        <span>{finding.problem_type === 'manifest_mismatch' ? '确定性状态诊断' : group.model_status === 'ready' ? '模型建议已就绪' : group.model_status === 'running' ? '模型复核中' : '模型未运行'}</span>
                        {group.decision_progress.total > 0 && <span>人工 {group.decision_progress.decided}/{group.decision_progress.total}</span>}
                        {group.mirror_count > 0 && <span>含 {group.mirror_count} 个派生记录</span>}
                      </div>
                      <div className="text-xs text-ink-2 line-clamp-3">{group.summary || '等待复核结论'}</div>
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </Card>
      )}

      {pageTab === 'optimization' && (
        <QualityImprovementPanel
          projectId={selectedId}
          currentPendingFindingCount={(queueQuery.data ?? []).filter((item) => item.effective_status !== 'review_passed').length}
          onOpenReview={() => setPageTab('review')}
        />
      )}

      {pageTab === 'scan' && (
        stateQuery.data && !stateQuery.data.ok ? (
          <Card className="p-5 bg-danger-soft/30">
            <div className="text-sm font-semibold text-danger">状态扫描发现残留</div>
            <div className="text-xs text-ink-2 mt-2">{stateQuery.data.force_finalized_paths.length} 个 force_finalized，{stateQuery.data.contract_violation_paths.length} 个最终攻略交付问题，{stateQuery.data.manifest_mismatches.length} 个 manifest 不一致。</div>
          </Card>
        ) : (
          <Card className="p-5">
            <div className="text-sm font-semibold text-ok">状态扫描正常</div>
            <div className="text-xs text-ink-3 mt-2">未发现 force_finalized、最终攻略交付问题或 manifest 不一致。</div>
          </Card>
        )
      )}

      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        title={selectedGroup?.title || selectedFinding?.finding_id || '问题详情'}
        subtitle={selectedFinding && (
          <span className="inline-flex items-center gap-2">
            <Badge variant={dispositionVariant(selectedFinding.disposition)}>{dispositionLabel[selectedFinding.disposition] ?? selectedFinding.disposition}</Badge>
            <span>{selectedFinding.finding_id}</span>
          </span>
        )}
        headerExtra={(
          <div className="flex items-center gap-1">
            <button type="button" disabled={!prevGroup} onClick={() => prevGroup && void selectQueueGroup(prevGroup)} className="rounded-md p-1.5 text-ink-3 hover:bg-bg-inset hover:text-ink-1 disabled:opacity-30 disabled:hover:bg-transparent" aria-label="上一条">
              <Icon name="chevronRight" className="w-4 h-4 rotate-180" />
            </button>
            {currentQueueIndex >= 0 && <span className="text-[11px] text-ink-3 tabular-nums">{currentQueueIndex + 1}/{visible.length}</span>}
            <button type="button" disabled={!nextGroup} onClick={() => nextGroup && void selectQueueGroup(nextGroup)} className="rounded-md p-1.5 text-ink-3 hover:bg-bg-inset hover:text-ink-1 disabled:opacity-30 disabled:hover:bg-transparent" aria-label="下一条">
              <Icon name="chevronRight" className="w-4 h-4" />
            </button>
          </div>
        )}
      >
        {!selectedFinding ? null : (
          <div className="px-5 py-4 space-y-4">
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {isManifestConsistencyFinding ? <>
                <span className="text-ok font-medium">① 发现冲突 ✓</span>
                <span className={contextQuery.isLoading ? 'text-brand-dark font-medium' : 'text-ok font-medium'}>② 读取两侧事实 {contextQuery.isLoading ? '进行中' : '✓'}</span>
                <span className={manifestConsistency?.authoritative_status ? 'text-ok font-medium' : 'text-warn font-medium'}>③ 确定权威终态 {manifestConsistency?.authoritative_status ? '✓' : '待依据'}</span>
                <span className={selectedFinding.status === 'closed' ? 'text-ok font-medium' : 'text-ink-3'}>④ 同步并重扫 {selectedFinding.status === 'closed' ? '✓' : '待执行'}</span>
              </> : <>
                <span className="text-ok font-medium">① 系统发现 ✓</span>
                <span className={contextQuery.isLoading ? 'text-brand-dark font-medium' : 'text-ok font-medium'}>② 读取上下文 {contextQuery.isLoading ? '进行中' : '✓'}</span>
                <span className={modelReviewProgressClass}>③ 模型复核 {modelReviewProgress.label}</span>
                <span className={selectedFinding.verification_actor_type === 'human' ? 'text-ok font-medium' : 'text-ink-3'}>④ 人工确认 {selectedFinding.verification_actor_type === 'human' ? '✓' : '待确认'}</span>
              </>}
            </div>

            <div className="border-l-4 border-danger pl-3">
              <div className="text-xs font-semibold text-danger mb-1">问题是什么</div>
              <div className="text-sm text-ink-1 line-clamp-4">{selectedFinding.issue_summary || selectedFinding.notes || '当前 finding 尚未生成可读的问题说明。'}</div>
              {(selectedFinding.issue_summary || '').length > 260 && (
                <details className="mt-1.5 text-xs text-ink-2">
                  <summary className="cursor-pointer font-semibold text-brand-dark">展开完整问题记录</summary>
                  <div className="mt-1.5 whitespace-pre-wrap leading-relaxed">{selectedFinding.issue_summary}</div>
                </details>
              )}
              <div className="text-xs font-semibold text-ink-2 mt-2 mb-0.5">{isManifestConsistencyFinding ? '处理原则' : '人工要回答什么'}</div>
              <div className="text-sm text-ink-2">{selectedFinding.review_question || '请先查看当前内容和证据，再决定是否通过、暂缓或需要重跑。'}</div>
            </div>

            {!isManifestConsistencyFinding && (contextQuery.isLoading ? (
              <div className="rounded-lg border border-border bg-bg-inset px-3.5 py-3 text-xs text-ink-3">正在追溯触发这条复核的 Agent Claim 和校验记录…</div>
            ) : (
              (() => {
                const origins = contextQuery.data?.origin_claims ?? []
                const currentCount = contextQuery.data?.verification_timeline.current_blocker_count
                const currentClaimCount = contextQuery.data?.verification_timeline.current_claim_count
                if (origins.length === 0) return null
                return (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between gap-3 rounded-lg border border-danger/25 bg-danger-soft/10 px-3.5 py-2.5">
                      <div>
                        <div className="text-sm font-semibold text-ink-1">当前待确认问题</div>
                        <div className="mt-0.5 text-[11px] text-ink-3">仅统计当前文件仍存在、且最新结构化校验未通过的 Claim。</div>
                      </div>
                      <span className="shrink-0 rounded bg-danger-soft px-2 py-1 text-xs font-semibold text-danger">
                        {currentCount ?? origins.length} 个问题{currentClaimCount && currentClaimCount !== currentCount ? ` · 关联 ${currentClaimCount} 条 Claim` : ''}
                      </span>
                    </div>
                    {origins.map((origin) => <OriginClaimCard key={origin.claim_id} origin={origin} projectId={selectedId} />)}
                  </div>
                )
              })()
            ))}

            {manifestConsistency && (
              <ManifestConsistencyPanel
                context={manifestConsistency}
                busy={deterministicRepairBusy}
                onApply={() => void applyManifestConsistencyRepair()}
              />
            )}

            {isManifestConsistencyFinding && contextQuery.isLoading && (
              <div className="rounded-lg border border-border bg-bg-inset px-3.5 py-3 text-xs text-ink-3">正在对齐 manifest、POI 详情、当前核验与 Agent 终态记录…</div>
            )}

            {isManifestConsistencyFinding && contextQuery.error && (
              <div className="rounded-lg border border-danger/25 bg-danger-soft/20 px-3.5 py-3 text-xs text-danger">状态诊断加载失败：{(contextQuery.error as Error).message}</div>
            )}

            {!isManifestConsistencyFinding && contextQuery.data?.missing_entity_context && (
              <MissingEntityReviewPanel context={contextQuery.data.missing_entity_context} projectId={selectedId} />
            )}

            {!isManifestConsistencyFinding && <Tabs
              active={drawerTab}
              onChange={(key) => setDrawerTab(key as typeof drawerTab)}
              tabs={[
                { key: 'decide', label: '逐条确认', badge: reviewItems.length > 0 ? `${decidedItemCount}/${reviewItems.length}` : undefined },
                { key: 'runs', label: '运行与建议历史', badge: (runsQuery.data ?? []).length || undefined },
                { key: 'evidence', label: '证据与审计资料' },
              ]}
            />}

            {!isManifestConsistencyFinding && drawerTab === 'decide' && (
              <div className="space-y-4">
                <details className="rounded-lg border border-border">
                  <summary className="cursor-pointer px-3 py-2.5 text-xs font-semibold text-ink-1 select-none">问题元数据 · 根因 / 严重度 / 处理动作</summary>
                  <div className="px-3 pb-3 pt-1 space-y-3">
                    <div className="grid grid-cols-2 gap-3">
                      <div className="rounded-lg bg-bg-inset px-3 py-2"><div className="text-xs text-ink-3">根因</div><div className="text-sm font-medium mt-0.5">{selectedFinding.root_cause}</div></div>
                      <div className="rounded-lg bg-bg-inset px-3 py-2"><div className="text-xs text-ink-3">严重度</div><div className="text-sm font-medium mt-0.5">{selectedFinding.severity}</div></div>
                      <div className="rounded-lg bg-bg-inset px-3 py-2"><div className="text-xs text-ink-3">重跑成本</div><div className="text-sm font-medium mt-0.5">{selectedFinding.rerun_cost}</div></div>
                      <div className="rounded-lg bg-bg-inset px-3 py-2"><div className="text-xs text-ink-3">验证状态</div><div className="text-sm font-medium mt-0.5">{selectedFinding.verification_status}</div></div>
                    </div>
                    <dl className="text-sm space-y-2">
                      <div><dt className="text-xs text-ink-3">处理动作</dt><dd className="mt-0.5">{selectedFinding.repair_action || '等待定向处理'}</dd></div>
                      <div><dt className="text-xs text-ink-3">发现来源</dt><dd className="mt-0.5">{selectedFinding.actor_id === 'state_scanner' ? '确定性状态扫描' : selectedFinding.source_review_id ? '历史复核记录' : selectedFinding.actor_id}</dd></div>
                      <div><dt className="text-xs text-ink-3">模型介入</dt><dd className="mt-0.5">{selectedRun ? `${selectedRunExecutorLabel} · ${runStatusLabel[selectedRun.status] || selectedRun.status}` : recommendation ? `已有当前建议 · ${recommendation.model_name || '模型'}` : modelReviewWasCompleted ? '存在历史模型记录' : modelWasAttempted ? '存在运行记录，但未完成复核' : '尚未调用'}</dd></div>
                      <div><dt className="text-xs text-ink-3">人工确认</dt><dd className="mt-0.5">{selectedFinding.verification_actor_type === 'human' ? `已确认 · ${selectedFinding.verification_actor_id || 'admin_user'}` : selectedFinding.verification_status === 'pending' ? '尚未确认' : `未见人工确认（${selectedFinding.verification_actor_type || '系统/模型'}）`}</dd></div>
                    </dl>
                    {selectedFinding.target && <div className="rounded-lg border border-border px-3 py-2 text-xs text-ink-2 break-words">目标：{String(selectedFinding.target.mismatch || selectedFinding.target.residue_path || selectedFinding.target.file || selectedFinding.target.scope || '局部内容')}</div>}
                    {selectedFinding.repair_action === 'targeted_poi_detail_regeneration' && <p className="text-xs text-warn">建议：只处理对应 POI 详情文件，不重跑整条路线。</p>}
                    {selectedFinding.notes && <p className="text-sm text-ink-2 border-t border-border pt-3">{selectedFinding.notes}</p>}
                  </div>
                </details>

                {recommendation ? (() => {
                  const { intro, items } = splitEnumeratedText(recommendation.proposed_text || '')
                  const claimReviews = recommendation.claim_reviews ?? []
                  const isLegacyRecommendation = claimReviews.length === 0
                  return (
                    <div className="rounded-lg border border-border overflow-hidden">
                      <div className="px-3 py-2.5 bg-bg-inset">
                        <div className="flex items-center justify-between gap-3 mb-1">
                          <div className="text-xs font-semibold text-ink-3">{isCurrentRecommendation ? '当前模型建议包' : '候选模型建议包'}</div>
                          <div className="text-xs text-ink-2">{isCurrentRecommendation ? `可人工判断 ${reviewReadyCount}/${reviewItems.length} · 已确认 ${decidedItemCount}/${reviewItems.length}` : `待设为当前推荐 · ${reviewItems.length} 条候选`}</div>
                        </div>
                        <div className="text-sm font-medium text-ink-1 leading-relaxed">{recommendation.summary || '模型未给出摘要'}</div>
                        <div className="text-[11px] text-ink-3 mt-1">模型：{recommendation.model_name || '未记录'}{recommendation.model_group ? ` · ${recommendation.model_group}` : ''}{recommendation.context_version ? ` · ${recommendation.context_version}` : ''}</div>
                      </div>
                      {isLegacyRecommendation && (
                        <div className="px-3 py-2.5 border-t border-danger/20 bg-danger-soft/20 text-xs text-ink-2">
                          这是旧版整体建议。系统已根据路线名和途经点，把建议重新关联到当前字段、历史失败点和对应素材；没有成功关联或证据不足的条目不会允许直接确认，需要补证据或重新结构化复核。
                        </div>
                      )}
                      {reviewItems.length > 0 && (
                        <div className="border-t border-border grid grid-cols-1 md:grid-cols-[300px_minmax(0,1fr)]">
                          <div className="scroll-boundary-y border-b md:border-b-0 md:border-r border-border px-3 py-3 space-y-1.5 md:max-h-[560px] md:overflow-y-auto">
                            <div className="text-xs font-semibold text-ink-1">逐条处理 · 点击查看详情</div>
                            {reviewItems.map((item) => (
                              <ReviewItemNavRow
                                key={item.key}
                                item={item}
                                selected={selectedItemKey === item.key}
                                decision={itemDecisionMap.get(item.key)}
                                itemContext={itemContextMap.get(item.key)}
                                onSelect={() => setSelectedItemKey(item.key)}
                              />
                            ))}
                          </div>
                          <div className="scroll-boundary-y px-4 py-4 md:max-h-[560px] md:overflow-y-auto">
                            {!selectedItem ? <p className="text-sm text-ink-3">该建议没有可逐条处理的条目。</p> : (
                              <ReviewItemDetailPanel
                                item={selectedItem}
                                itemContext={itemContextMap.get(selectedItem.key)}
                                decision={itemDecisionMap.get(selectedItem.key)}
                                proposedOperation={(recommendation.proposed_operations ?? []).find((operation) => operation.item_key === selectedItem.key)}
                                allowDecision={isCurrentRecommendation}
                                editingItemKey={editingItemKey}
                                editedItemValue={editedItemValue}
                                busyItemKey={busyItemKey}
                                onSetEditedItemValue={setEditedItemValue}
                                onStartEdit={(value) => {
                                  setEditingItemKey(selectedItem.key)
                                  setEditedItemValue(value)
                                }}
                                onCancelEdit={() => { setEditingItemKey(null); setEditedItemValue(null) }}
                                onDecide={(disposition, value) => decideItem(selectedItem, disposition, value)}
                              />
                            )}
                          </div>
                        </div>
                      )}
                      {recommendation.proposed_text && (
                        <details className="group px-3 py-2.5 text-sm text-ink-2 border-t border-border">
                          <summary className="cursor-pointer text-xs font-semibold text-brand-dark select-none">
                            <span className="group-open:hidden">展开完整修改建议</span>
                            <span className="hidden group-open:inline">收起修改建议</span>
                          </summary>
                          <div className="scroll-boundary-y mt-2 max-h-64 overflow-y-auto leading-relaxed pr-1">
                            {intro && <p className="whitespace-pre-wrap break-words">{intro}</p>}
                            {items.length > 0 && (
                              <ul className="mt-2 space-y-1.5 list-disc pl-4">
                                {items.map((item, index) => <li key={index} className="whitespace-pre-wrap break-words">{item}</li>)}
                              </ul>
                            )}
                          </div>
                        </details>
                      )}
                      {recommendation.uncertainties.length > 0 && <div className="px-3 py-2 border-t border-border text-xs text-warn">不确定性：{recommendation.uncertainties.join('；')}</div>}
                    </div>
                  )
                })() : (
                  <p className="text-sm text-ink-3">尚未生成模型建议。可以到"运行与建议历史"标签发起复核。</p>
                )}

                {pendingPatch && (
                  <div className="border border-border overflow-hidden rounded-md">
                    <div className="px-3 py-2.5 bg-bg-inset flex items-center justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold">待应用修改</div>
                        <div className="text-[11px] text-ink-3 mt-0.5">{pendingPatch.file} · {pendingPatch.operations.length} 项 · 影响级别 {pendingPatch.impact_class}</div>
                      </div>
                      <Badge variant={pendingPatch.status === 'applied' ? 'ok' : pendingPatch.status === 'patch_conflict' ? 'danger' : 'warn'}>{pendingPatch.status}</Badge>
                    </div>
                    <div className="divide-y divide-border">
                      {pendingPatch.operations.map((operation, index) => (
                        <div key={`${operation.path}-${index}`} className="px-3 py-2.5 text-xs">
                          <div className="font-mono text-ink-2">{operation.op} {operation.path}</div>
                          <div className="grid grid-cols-2 gap-2 mt-2">
                            <div><div className="text-[11px] text-ink-3 mb-1">修改前</div><pre className="scroll-boundary-y whitespace-pre-wrap break-words bg-danger-soft/20 p-2 rounded-sm max-h-32 overflow-y-auto">{JSON.stringify(operation.before, null, 2)}</pre></div>
                            <div><div className="text-[11px] text-ink-3 mb-1">修改后</div><pre className="scroll-boundary-y whitespace-pre-wrap break-words bg-ok-soft/30 p-2 rounded-sm max-h-32 overflow-y-auto">{JSON.stringify(operation.value, null, 2)}</pre></div>
                          </div>
                        </div>
                      ))}
                    </div>
                    {pendingPatch.status === 'proposed' && (
                      <div className="px-3 py-2.5 border-t border-border flex gap-2">
                        <button disabled={busy} onClick={() => void applyPendingPatch()} className="px-3 py-1.5 rounded-md bg-ok-soft text-ok text-xs font-semibold disabled:opacity-50">应用修改</button>
                        <button disabled={busy} onClick={() => void rejectPendingPatch()} className="px-3 py-1.5 rounded-md bg-bg-inset text-ink-2 text-xs font-semibold disabled:opacity-50">暂不应用</button>
                      </div>
                    )}
                    {pendingPatch.status === 'applied' && (
                      <div className="px-3 py-2.5 border-t border-border">
                        <button disabled={busy} onClick={() => void preparePatchRollback()} className="px-3 py-1.5 rounded-md bg-bg-inset text-ink-2 text-xs font-semibold disabled:opacity-50">生成回滚修改</button>
                      </div>
                    )}
                  </div>
                )}

                <div className="sticky bottom-0 -mx-5 px-5 py-3 bg-surface border-t border-border flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="text-xs font-medium text-ink-1">{reviewItems.length > 0 ? `逐条确认 ${decidedItemCount}/${reviewItems.length}` : '处理建议尚未生成'}</div>
                    <div className="text-[11px] text-ink-3 mt-0.5">{reviewItems.length > 0 ? '采纳或编辑只记录处理方案，不会直接修改正文；正文变更必须另行生成并应用 patch。' : '上方已可查看原始 Claim 和失败原因；生成模型建议后才会进入逐条确认。'}</div>
                  </div>
                  <button disabled={busy || !isCurrentRecommendation || !recommendation || reviewItems.length === 0 || decidedItemCount !== reviewItems.length} onClick={async () => { setBusy(true); try { await summarizeItemDecisions(reviewItems) } finally { setBusy(false) } }} className="px-3 py-1.5 rounded-md bg-ok-soft text-ok text-xs font-semibold disabled:opacity-50">汇总逐条结论</button>
                </div>
              </div>
            )}

            {!isManifestConsistencyFinding && drawerTab === 'runs' && (
              <div className="space-y-4">
                <div className="rounded-lg border border-border overflow-hidden">
                  <div className="px-3 py-2.5 bg-bg-inset flex items-center justify-between gap-3">
                    <div>
                      <div className="text-xs font-semibold text-ink-1">复核运行</div>
                      <div className="text-[11px] text-ink-3 mt-0.5">每次先生成候选；人工设为当前推荐后，才进入逐条确认。</div>
                    </div>
                    <span className="text-[11px] text-ink-3">{(runsQuery.data ?? []).length} 次历史运行</span>
                  </div>
                  <div className="px-3 py-3 space-y-3">
                    <div className="grid grid-cols-3 gap-1 rounded-md bg-bg-inset p-1">
                      {([
                        ['standard', '标准模型'],
                        ['codex', '创建任务包'],
                        ['compare', '对照运行'],
                      ] as const).map(([value, label]) => (
                        <button key={value} type="button" onClick={() => setRunMode(value)} className={`min-h-8 rounded px-2 text-xs font-semibold ${runMode === value ? 'bg-surface text-ink-1 shadow-sm' : 'text-ink-3 hover:text-ink-1'}`}>{label}</button>
                      ))}
                    </div>
                    <div className="flex items-center justify-between gap-3">
                      <div className="text-[11px] text-ink-3">
                        {runMode === 'standard' && '服务端运行现有强模型工作流。'}
                        {runMode === 'codex' && '只创建冻结任务包，不会在网页内直接运行 Codex；外部复核后导入结果。'}
                        {runMode === 'compare' && '启动标准模型，同时创建同一冻结基线的 Codex 任务包。'}
                      </div>
                      <button disabled={runBusy} onClick={startReviewRun} className="shrink-0 rounded-md bg-brand-soft px-3 py-1.5 text-xs font-semibold text-brand-dark disabled:opacity-50">{runBusy ? '处理中…' : runActionLabel}</button>
                    </div>
                    {(executorsQuery.data ?? []).length > 0 && <div className="text-[10px] text-ink-3">已启用：{(executorsQuery.data ?? []).map((item) => `${item.name} · ${item.execution_mode === 'server' ? '服务端' : '任务包桥接'}`).join('；')}</div>}

                    {(runsQuery.data ?? []).length > 0 && (
                      <div className="flex gap-1 rounded-md bg-bg-inset p-1 w-fit">
                        {([['all', '全部'], ['active', '活动中'], ['attention', '需处理']] as const).map(([value, label]) => (
                          <button key={value} type="button" onClick={() => setRunStatusFilter(value)} className={`rounded px-2 py-1 text-[11px] font-semibold ${runStatusFilter === value ? 'bg-surface text-ink-1 shadow-sm' : 'text-ink-3'}`}>{label}</button>
                        ))}
                      </div>
                    )}
                    {(runsQuery.data ?? []).length === 0 ? <div className="rounded-md border border-dashed border-border px-3 py-3 text-xs text-ink-3">还没有执行记录。选择一种方式开始复核。</div> : visibleRuns.length === 0 ? <div className="rounded-md border border-dashed border-border px-3 py-3 text-xs text-ink-3">当前筛选下没有执行记录。</div> : (
                      <div className="space-y-2">
                        {visibleRuns.map((run) => {
                          const isCurrent = Boolean(run.recommendation_id && run.recommendation_id === selectedFinding.recommendation_id)
                          return (
                            <div key={run.run_id} className={`rounded-md border px-2.5 py-2 ${selectedRunId === run.run_id ? 'border-brand bg-brand-soft/20' : 'border-border'}`}>
                              <button type="button" onClick={() => { setSelectedRunId(run.run_id); if (run.recommendation_id) setSelectedRecommendationId(run.recommendation_id) }} className="w-full text-left">
                                <div className="flex items-center justify-between gap-2">
                                  <div className="text-xs font-semibold text-ink-1">{run.executor_id === 'codex_skill' ? 'Codex 任务包' : '标准模型'}</div>
                                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${runStatusClass(run.status, run.health_status)}`}>{run.status === 'running' && run.health_status === 'stalled' ? '进展停滞' : runStatusLabel[run.status] || run.status}</span>
                                </div>
                                <div className="mt-1 text-[11px] text-ink-2">{run.progress_message}</div>
                                <div className="mt-1 text-[10px] text-ink-3">阶段：{run.stage} · 基线 {run.bundle_sha256.slice(0, 8)}</div>
                              </button>
                              {run.error && <div className="mt-1.5 rounded bg-danger-soft/30 px-2 py-1 text-[11px] text-danger">技术失败：{summarizeReviewRunError(run.error)}</div>}
                              {run.status_reason && <div className="mt-1.5 text-[11px] text-ink-3">{run.status_reason}</div>}
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {run.executor_id === 'codex_skill' && <button type="button" onClick={() => downloadRunBundle(run)} className="rounded bg-bg-inset px-2 py-1 text-[11px] font-medium text-ink-2">下载任务包</button>}
                                {run.executor_id === 'codex_skill' && ['waiting_user', 'waiting_external', 'failed'].includes(run.status) && (
                                  <label className="cursor-pointer rounded bg-brand-soft px-2 py-1 text-[11px] font-medium text-brand-dark">
                                    导入 Codex 结果
                                    <input type="file" accept="application/json,.json" className="hidden" onChange={(event) => { const file = event.target.files?.[0]; if (file) void importCodexResult(run, file); event.currentTarget.value = '' }} />
                                  </label>
                                )}
                                {run.status === 'succeeded' && run.recommendation_id && !isCurrent && <button type="button" disabled={runBusy} onClick={() => promoteRun(run)} className="rounded bg-ok-soft px-2 py-1 text-[11px] font-semibold text-ok disabled:opacity-50">设为当前推荐</button>}
                                {isCurrent && <span className="rounded bg-ok-soft px-2 py-1 text-[11px] font-semibold text-ok">当前推荐</span>}
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    )}

                    {comparisonQuery.data && (
                      <div className="rounded-md border border-border px-2.5 py-2 text-xs">
                        <div className="flex items-center justify-between gap-2"><span className="font-semibold">对照摘要</span><span className={comparisonQuery.data.same_baseline ? 'text-ok' : 'text-danger'}>{comparisonQuery.data.same_baseline ? '同一冻结基线' : '基线不一致'}</span></div>
                        <div className="mt-1 text-ink-2">一致 {comparisonQuery.data.items.filter((item) => item.aligned).length} 条，存在差异 {comparisonQuery.data.items.filter((item) => !item.aligned).length} 条。系统不会自动合并或投票。</div>
                      </div>
                    )}
                  </div>
                </div>

                <div className="rounded-lg border border-border overflow-hidden">
                  <div className="flex items-center justify-between gap-3 bg-bg-inset px-3 py-2.5">
                    <div>
                      <div className="text-xs font-semibold text-ink-1">建议历史</div>
                      <div className="mt-0.5 text-[11px] text-ink-3">独立保留每版模型建议和对应人工决定，只读查看不会改变当前推荐。</div>
                    </div>
                    <span className="text-[11px] text-ink-3">{(recommendationHistoryQuery.data ?? []).length} 个版本</span>
                  </div>
                  {recommendationHistoryQuery.isLoading ? (
                    <div className="px-3 py-3 text-xs text-ink-3">正在读取建议历史…</div>
                  ) : (recommendationHistoryQuery.data ?? []).length === 0 ? (
                    <div className="px-3 py-3 text-xs text-ink-3">尚无 recommendation 历史。</div>
                  ) : (
                    <div className="scroll-boundary-y divide-y divide-border max-h-[420px] overflow-y-auto">
                      {(recommendationHistoryQuery.data ?? []).map((entry) => {
                        const item = entry.recommendation
                        const selected = recommendation?.recommendation_id === item.recommendation_id
                        return (
                          <button
                            key={item.recommendation_id}
                            type="button"
                            onClick={() => {
                              setSelectedRecommendationId(entry.is_current ? null : item.recommendation_id)
                              setSelectedRunId(entry.run_id)
                              setEditingItemKey(null)
                              setEditedItemValue(null)
                            }}
                            className={`w-full px-3 py-2.5 text-left transition-colors ${selected ? 'bg-brand-soft/30' : 'hover:bg-bg-inset'}`}
                          >
                            <div className="flex items-start justify-between gap-2">
                              <div className="min-w-0 text-xs font-semibold text-ink-1 line-clamp-1">{item.summary || item.recommendation_id}</div>
                              <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${entry.lifecycle_status === 'current' ? 'bg-ok-soft text-ok' : entry.lifecycle_status === 'superseded' ? 'bg-bg-inset text-ink-3' : 'bg-warn-soft text-[#8a6417]'}`}>{entry.lifecycle_status === 'current' ? '当前' : entry.lifecycle_status === 'superseded' ? '已替代' : '候选'}</span>
                            </div>
                            <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-[10px] text-ink-3">
                              <span>{item.model_name || '模型未记录'}</span>
                              <span>人工 {entry.decision_count}/{entry.total_items}</span>
                              <span>{formatReviewTime(item.created_at)}</span>
                              {entry.executor_id && <span>{entry.executor_id === 'codex_skill' ? 'Codex 任务包' : '标准模型'}</span>}
                            </div>
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}

            {!isManifestConsistencyFinding && drawerTab === 'evidence' && (
              <div className="space-y-4">
                {!contextQuery.data ? <p className="text-sm text-ink-3">正在加载复核对象与审计资料…</p> : <>
                  <div className="rounded-lg border border-border px-3 py-3">
                    <div className="font-semibold text-sm text-ink-1 mb-2">复核对象</div>
                    <div className="text-xs text-ink-2 space-y-0.5">
                      <div>文件：{String(contextQuery.data.summary.file || '未定位到文件')}</div>
                      <div>范围：{String(contextQuery.data.summary.scope_label || contextQuery.data.summary.json_pointer || '文件级扫描')}</div>
                    </div>
                    <p className="mt-2 text-xs text-ink-3">这里说明 finding 的文件范围。触发问题的原始 Claim、当前字段和校验失败点已放在抽屉顶部；模型复核后的处理建议会进入"逐条确认"。</p>
                  </div>

                  <details className="rounded-lg border border-border px-3 py-2.5">
                    <summary className="cursor-pointer text-xs font-semibold text-ink-2">当前内容摘要（模型上下文压缩版）</summary>
                    <pre className="scroll-boundary-y mt-2 font-mono text-xs whitespace-pre-wrap break-words max-h-64 overflow-y-auto">{JSON.stringify(contextQuery.data.current_content, null, 2)}</pre>
                  </details>

                  {(() => {
                    const groups = contextQuery.data.evidence_groups
                    const attachedSources = groups?.attached_sources ?? []
                    const sourceExcerpts = groups?.source_excerpts ?? []
                    const failures = groups?.verification_failures ?? []
                    const missingEntityFailures = failures.filter((item) => item.type === 'missing_entity')
                    const timeline = contextQuery.data.verification_timeline
                    const history = groups?.historical_reviews ?? []
                    const findingRefs = groups?.finding_refs ?? []
                    return (
                      <>
                        <div className="rounded-lg border border-border px-3 py-2.5">
                          <div className="text-xs font-semibold text-ink-1 mb-1.5">全文件引用索引 · {attachedSources.length + findingRefs.length} 条</div>
                          <div className="space-y-1.5">
                            {findingRefs.map((ref) => <div key={ref} className="rounded-md border border-border px-2.5 py-1.5 text-xs text-ink-2">finding 引用：{ref}</div>)}
                            {attachedSources.slice(0, 8).map((item, index) => {
                              const source = item.source as Record<string, unknown> | undefined
                              const chunkId = String(source?.chunk_id || '')
                              return <div key={`${chunkId}-${index}`} className="rounded-md border border-border px-2.5 py-1.5 text-xs text-ink-2"><span className="font-medium">{chunkId || '当前引用'}</span> · {String(source?.platform || '未知来源')} · {String(source?.author || '未记录作者')}</div>
                            })}
                            {attachedSources.length + findingRefs.length === 0 && <p className="text-xs text-ink-3">当前对象没有挂载引用。</p>}
                          </div>
                        </div>
                        <details className="rounded-lg border border-border px-3 py-2.5">
                          <summary className="cursor-pointer text-xs font-semibold text-ink-1">全文件已取回的素材原文 · {sourceExcerpts.length} 条</summary>
                          <div className="mt-1.5 space-y-1.5">
                            {sourceExcerpts.slice(0, 8).map((item, index) => {
                              const source = item.source as Record<string, unknown> | undefined
                              return <div key={index} className="rounded-md border border-brand/20 bg-brand-soft/10 px-2.5 py-1.5 text-xs text-ink-2"><span className="font-medium">{String(source?.chunk_id || '素材原文')}</span>：{String(item.text || '')}</div>
                            })}
                            {sourceExcerpts.length === 0 && <p className="text-xs text-ink-3">引用存在，但没有从素材索引读到对应原文。</p>}
                          </div>
                        </details>
                        <div className="rounded-lg border border-border px-3 py-2.5">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="text-xs font-semibold text-ink-1">自动核验收敛过程 · {timeline.round_count} 轮</div>
                            {timeline.progression.length > 0 && (
                              <span className="rounded bg-bg-inset px-2 py-1 text-[11px] font-semibold tabular-nums text-ink-2">
                                {timeline.progression.map((count) => count ?? '数量未知').join(' → ')}
                              </span>
                            )}
                          </div>
                          <p className="mt-1 text-[11px] leading-relaxed text-ink-3">这里保留生成阶段的历史打回记录；前面轮次已被后续局部返修替代，不代表当前仍有这些问题。</p>
                          <div className="mt-2 space-y-1.5">
                            {timeline.rounds.map((item) => (
                              <details key={`${item.round}-${item.history_index}`} open={item.is_latest} className="rounded-md border border-warn/30 bg-warn-soft/20 px-2.5 py-1.5 text-xs text-ink-2">
                                <summary className="cursor-pointer font-medium">
                                  第{item.round}轮 · {item.issue_count == null ? '旧格式，数量未知' : `${item.issue_count} 个失败 Claim`} · {item.superseded ? '已被后续返修替代' : '最后一轮历史记录'}
                                </summary>
                                <div className="mt-1.5 whitespace-pre-wrap leading-relaxed">{item.raw_detail}</div>
                              </details>
                            ))}
                            {timeline.other_events.map((item) => (
                              <details key={`event-${item.history_index}`} className="rounded-md border border-border bg-bg-inset px-2.5 py-1.5 text-xs text-ink-2">
                                <summary className="cursor-pointer font-medium">其他核验事件 · {item.label}</summary>
                                <div className="mt-1.5 whitespace-pre-wrap leading-relaxed">{item.raw_detail}</div>
                              </details>
                            ))}
                            {timeline.round_count === 0 && timeline.other_events.length === 0 && <p className="text-xs text-ink-3">没有读取到历史自动核验记录。</p>}
                          </div>
                        </div>
                        {missingEntityFailures.length > 0 && !contextQuery.data.missing_entity_context && (
                          <details className="rounded-lg border border-border px-3 py-2.5">
                            <summary className="cursor-pointer text-xs font-semibold text-ink-1">当前文件的旧式缺失实体 · {missingEntityFailures.length} 条</summary>
                            <div className="mt-1.5 space-y-1.5">
                              {missingEntityFailures.map((item, index) => <div key={index} className="rounded-md border border-warn/30 bg-warn-soft/20 px-2.5 py-1.5 text-xs text-ink-2">缺失实体：{String(item.entity || '')}</div>)}
                            </div>
                          </details>
                        )}
                        <details className="rounded-lg border border-border px-3 py-2.5">
                          <summary className="cursor-pointer text-xs font-semibold text-ink-1">历史复核记录 · {history.length} 条</summary>
                          <div className="mt-1.5 space-y-1.5">
                            {history.map((item, index) => <div key={index} className="rounded-md border border-border px-2.5 py-1.5 text-xs text-ink-2">{String(item.notes || item.action || item.decision || '无补充说明')}</div>)}
                            {history.length === 0 && <p className="text-xs text-ink-3">没有匹配到历史复核事件。</p>}
                          </div>
                        </details>
                      </>
                    )
                  })()}
                </>}
              </div>
            )}

            {modelError && <p className="text-xs text-danger">操作失败：{modelError}</p>}
          </div>
        )}
      </Drawer>
    </div>
  )
}
