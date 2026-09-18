import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useProjectOutputs } from '../api/hooks'
import { Card } from '../components/Card'
import { Badge } from '../components/Badge'
import { Icon } from '../components/Icon'
import { ProjectTabs } from '../components/ProjectTabs'
import type {
  EvidenceCheck,
  EvidenceCheckEvidence,
  EvidenceClaim,
  FinalGuideBlocks,
  LifestyleItem,
  PoiDetailFile,
  PitfallItem,
  ReferenceEntry,
  RouteFeasibilityCheck,
  RouteFeasibilityReport,
  RouteMapEntity,
  RouteMapValidation,
  SourceRef,
} from '../api/types'
import type { BadgeVariant } from '../lib/status'

const CATEGORY_LABEL: Record<string, string> = { food: '美食', outfit: '穿搭', lodging: '住宿', play: '游玩' }
const MAIN_TABS = ['route', 'lifestyle', 'pitfall', 'poi_detail', 'integration'] as const
type MainTab = (typeof MAIN_TABS)[number]
const MAIN_TAB_LABEL: Record<MainTab, string> = {
  route: '路线规划',
  lifestyle: '衣食住行',
  pitfall: '避雷',
  poi_detail: 'POI详情',
  integration: '整合产出',
}

function usageAnchor(usageKey: string): string {
  return `reference-usage-${encodeURIComponent(usageKey)}`
}

function scrollToUsage(usageKey?: string) {
  if (!usageKey) return
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      const target = document.getElementById(usageAnchor(usageKey))
      target?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
    })
  })
}

function usageLabel(usageKey: string): string {
  const parts = usageKey.split(':')
  if (parts[0] === 'route' && parts[1] === 'day') return `Day${parts[2]}路线`
  if ((parts[0] === 'lifestyle' || parts[0] === 'pitfall') && parts[2] === 'day') {
    const field = CATEGORY_LABEL[parts[1]] ?? parts[1]
    return `Day${parts[3]}${field}${parts[0] === 'pitfall' ? '避雷' : ''}`
  }
  if (parts[0] === 'poi_detail' && parts[1] === 'day') {
    return `Day${parts[2]} ${parts[4]} · ${parts[6]}途经点${parts[8]}`
  }
  return usageKey
}

function usageTab(usageKey: string): MainTab | null {
  const section = usageKey.split(':', 1)[0]
  if (section === 'route' || section === 'lifestyle' || section === 'pitfall' || section === 'poi_detail') {
    return section
  }
  return null
}

const SCOPE_LABEL: Record<string, string> = {
  source_availability: '来源可读取',
  entity_identity: '地点/实体对应',
  substantive_claim: '具体说法是否受支持',
  route_feasibility: '路线可行性',
}

const SCOPE_EXPLANATION: Record<string, { question: string; rule: string; boundary: string }> = {
  source_availability: {
    question: '产物声明的 chunk 是否真实存在，并且能读到原文？',
    rule: '按 chunk_id 从素材库或运行时证据中取回文本；全部可读才通过。',
    boundary: '只证明材料可读，不证明材料支持产出中的说法。',
  },
  entity_identity: {
    question: '产出中声明的地点、店名或其他专名，能否在引用材料中找到同一对象？',
    rule: '先做字面匹配和常见后缀归一，再查同文档其他 chunk；实时核验必要时再用模型判断别名、简称或同音转写。',
    boundary: '只确认“这个对象被材料提到”，不自动证明与它相关的票价、时间、交通等说法正确。',
  },
  substantive_claim: {
    question: '产出中的价格、时间、距离、特点、建议等具体说法，是否被原文实质支持？',
    rule: '对触发实质核验的内容，将产出原文与读取到的证据交给模型，判断是否有具体对应支持。',
    boundary: '显示“不适用”表示该类声明不需要此项判断；显示“未检查”才表示当次未覆盖，不能由其他通过项推导为正确。',
  },
}

const RESULT_LABEL: Record<string, string> = {
  pass: '通过',
  fail: '不通过',
  inconclusive: '无法判断',
  not_checked: '未检查',
  error: '检查错误',
}

const METHOD_LABEL: Record<string, string> = {
  chunk_lookup: 'Chunk 读取',
  source_declaration: '来源声明',
  literal_match: '字面匹配',
  sibling_chunk_match: '同文档补充匹配',
  alias_resolution: '别名判断',
  substantive_support: '实质支持判断',
  missing_claimed_entities: '缺少实体声明',
  no_entity_to_verify: '无专名可核对',
  not_triggered: '未触发',
  not_run: '未执行',
  out_of_scope: '不在当前检查范围',
  model_checks_disabled: '历史回填未重跑模型判断',
  amap_measurement: '高德实测',
  web_evidence: '联网证据',
  derived_support: '推导支持判断',
  derived_dependency: '推导依赖检查',
  declared_estimate: '经验估算声明',
  declared_unverified: '未核验声明',
  user_constraint: '用户约束来源',
}

const CHECK_SCOPE_ORDER: Record<string, number> = {
  source_availability: 1,
  entity_identity: 2,
  substantive_claim: 3,
}

function methodExecutionText(check: EvidenceCheck): string {
  const label = METHOD_LABEL[check.method] ?? check.method
  if (check.method === 'out_of_scope') return `不适用：${label}。`
  if (check.method === 'not_run' || check.method === 'not_triggered') return `未执行：${label}。`
  if (check.method === 'model_checks_disabled') return `未执行模型判断：${label}。`
  if (check.method === 'chunk_lookup') return `已执行 ${label}，按 chunk_id 逐个取回材料。`
  if (check.method === 'literal_match') return `已执行 ${label}，对声明实体逐个匹配。`
  if (check.method === 'sibling_chunk_match') return `已执行 ${label}，引用 chunk 未命中后继续读取同文档材料。`
  if (check.method === 'alias_resolution') return `已执行 ${label}，用模型判断别名、简称或同音转写。`
  if (check.method === 'substantive_support') return `已执行 ${label}，比较产出说法与证据原文。`
  if (check.method === 'amap_measurement') return `已执行 ${label}，比较路线事实与生成时保存的高德结果。`
  if (check.method === 'derived_support') return `已执行 ${label}，比较推导结论与它声明依赖的事实。`
  if (check.method === 'derived_dependency') return `已执行 ${label}，依赖事实未全部通过，因此本条不能视为已证实。`
  if (check.method === 'declared_estimate') return `未执行外部核验：${label}。`
  if (check.method === 'declared_unverified') return `未执行外部核验：${label}。`
  if (check.method === 'user_constraint') return `未执行外部核验：${label}。`
  return `本次方法：${label}。`
}

function checkExecuted(check: EvidenceCheck): boolean {
  return ![
    'out_of_scope',
    'no_entity_to_verify',
    'not_run',
    'not_triggered',
    'model_checks_disabled',
    'declared_estimate',
    'declared_unverified',
    'user_constraint',
  ].includes(check.method)
}

function checkResultPresentation(check: EvidenceCheck): { label: string; variant: BadgeVariant } {
  if (check.result !== 'not_checked') {
    return { label: RESULT_LABEL[check.result] ?? check.result, variant: checkVariant(check.result) }
  }
  if (check.method === 'out_of_scope' || check.method === 'no_entity_to_verify') {
    return { label: '不适用', variant: 'pending' }
  }
  if (check.method === 'declared_estimate' || check.method === 'user_constraint') {
    return { label: '无需外部核验', variant: 'pending' }
  }
  if (check.method === 'declared_unverified') {
    return { label: '缺少证据', variant: 'warn' }
  }
  if (check.method === 'not_triggered') return { label: '未触发', variant: 'pending' }
  if (check.method === 'not_run' || check.method === 'model_checks_disabled') {
    return { label: '未执行', variant: 'pending' }
  }
  return { label: '未检查', variant: 'pending' }
}

export function AgentOutputsPage() {
  const { projectId = '' } = useParams()
  const { data, isLoading, error } = useProjectOutputs(projectId)
  const [tab, setTab] = useState<MainTab>('route')
  const [focusUsage, setFocusUsage] = useState('')

  if (isLoading) return <p className="text-ink-3">加载中…</p>
  if (error) return <p className="text-danger">加载失败：{(error as Error).message}</p>
  if (!data) return null

  const navigateToUsage = (usageKey: string) => {
    const targetTab = usageTab(usageKey)
    if (!targetTab) return
    setFocusUsage(usageKey)
    setTab(targetTab)
  }

  return (
    <div>
      <ProjectTabs projectId={projectId} />

      <div className="scroll-boundary-x flex gap-5 border-b border-border mb-5 overflow-x-auto">
        {MAIN_TABS.map((t) => (
          <button
            key={t}
            className={`pb-2.5 text-[13.5px] font-semibold relative top-px border-b-2 whitespace-nowrap ${
              tab === t ? 'text-brand-dark border-brand' : 'text-ink-3 border-transparent'
            }`}
            onClick={() => setTab(t)}
          >
            {MAIN_TAB_LABEL[t]}
          </button>
        ))}
      </div>

      {tab === 'route' && (
        <RouteTab
          routePlan={data.route_plan}
          routeMap={data.route_map_validation ?? null}
          checks={data.evidence_checks?.route ?? []}
          feasibility={data.route_feasibility ?? null}
          focusUsage={focusUsage}
        />
      )}
      {tab === 'lifestyle' && (
        <CategoryTab
          categories={data.lifestyle}
          isPitfall={false}
          checks={data.evidence_checks?.lifestyle ?? []}
          focusUsage={focusUsage}
        />
      )}
      {tab === 'pitfall' && (
        <CategoryTab
          categories={data.pitfall}
          isPitfall
          checks={data.evidence_checks?.pitfall ?? []}
          focusUsage={focusUsage}
        />
      )}
      {tab === 'poi_detail' && (
        <PoiDetailTab
          details={data.poi_details ?? []}
          checks={data.evidence_checks?.poi_detail ?? []}
          focusUsage={focusUsage}
        />
      )}
      {tab === 'integration' && (
        <IntegrationTab
          finalGuide={data.final_guide}
          blocks={data.final_guide_blocks ?? null}
          references={data.final_guide_meta?.references ?? []}
          onNavigateUsage={navigateToUsage}
        />
      )}
    </div>
  )
}

function RouteTab({
  routePlan,
  routeMap,
  checks,
  feasibility,
  focusUsage,
}: {
  routePlan: import('../api/types').RoutePlan | null
  routeMap: RouteMapValidation | null
  checks: EvidenceCheck[]
  feasibility: RouteFeasibilityReport | null
  focusUsage?: string
}) {
  useEffect(() => scrollToUsage(focusUsage), [focusUsage])
  if (!routePlan || routePlan.days.length === 0) {
    return <p className="text-ink-3">还没有生成路线规划。</p>
  }
  return (
    <div>
      {routePlan.days.map((day, dayIndex) => {
        const dayKey = `route:day:${day.day}`
        const candidateDayChecks = checks.filter(
          (check) => check.target_key === dayKey || check.target_key.startsWith(`${dayKey}:`),
        )
        const currentClaimIds = new Set((day.claims ?? []).map((claim) => claim.claim_id))
        const dayChecks = currentClaimIds.size > 0
          ? candidateDayChecks.filter(
              (check) => Boolean(check.claim_id && currentClaimIds.has(check.claim_id)),
            )
          : candidateDayChecks.filter((check) => check.target_key === dayKey)
        const dayLevelChecks = dayChecks.filter((check) => check.target_key === dayKey)
        const feasibilityChecks = feasibility?.checks.filter((check) => check.day === day.day) ?? []
        const resolvedEntities = new Map(
          (routeMap?.days.find((item) => item.day === day.day)?.entities ?? [])
            .filter((entity) => entity.status === 'resolved')
            .map((entity) => [entity.original_name, entity]),
        )
        return (
          <div
            id={usageAnchor(dayKey)}
            key={day.day ?? dayIndex}
            className={`scroll-mt-6 ${focusUsage === dayKey ? 'outline outline-2 outline-brand rounded-xl' : ''}`}
          >
          <Card className="mb-3.5 overflow-hidden">
            <div className="flex items-center gap-2.5 px-4 py-3 bg-bg-inset border-b border-border">
              <div className="w-6.5 h-6.5 rounded-lg bg-brand text-white flex items-center justify-center text-xs font-bold shrink-0">
                {day.day}
              </div>
              <b className="text-[13.5px]">
                Day{day.day} · {day.city}
              </b>
              <span className="text-xs text-ink-3">{day.area}</span>
              <div className="ml-auto">
                <CheckSummaryBadge checks={dayChecks} feasibilityChecks={feasibilityChecks} />
              </div>
            </div>
            <div className="px-4 py-3.5">
              {day.pois.map((poi, poiIndex) => (
                <div key={`${poi.name}-${poiIndex}`} className="flex items-start gap-2.5 py-2 border-b border-dashed border-border last:border-none">
                  <div className={`w-1.75 h-1.75 rounded-full mt-1.5 shrink-0 ${poi.needs_detail ? 'bg-brand' : 'bg-ink-3'}`} />
                  <div className="flex-1 min-w-0">
                    <div className="text-[13.5px] font-bold">
                      {poi.name}
                      {poi.needs_detail && (
                        <span className="ml-1.5 inline-block">
                          <Badge variant="brand">需详情</Badge>
                        </span>
                      )}
                    </div>
                    {poi.brief && <div className="text-[12.5px] text-ink-2 mt-0.5 break-words">{poi.brief}</div>}
                  </div>
                </div>
              ))}
              {day.notes && (
                <div className="mt-2.5 text-[12.5px] text-ink-2 break-words">
                  <b className="text-ink">备注：</b>
                  {day.notes}
                </div>
              )}
              {(day.claims?.length ?? 0) > 0 && (
                <ClaimsSection claims={day.claims ?? []} checks={dayChecks} resolvedEntities={resolvedEntities} />
              )}
              <EvidenceAndChecks
                sources={day.sources ?? []}
                checks={dayLevelChecks}
                feasibilityChecks={feasibilityChecks}
                subject={{
                  title: `Day${day.day} 路线产出`,
                  content: `路线地点：${day.pois.map((poi) => poi.name).join('、') || '无'}`,
                  note: day.notes || day.pois.some((poi) => poi.brief)
                    ? (day.claims?.length ?? 0) > 0
                      ? '这里保留天级地点身份检查；备注和简介中的关键事实请查看上方逐条事实证据链。'
                      : '路线备注和 POI 简介已在产出中，但这份旧数据只有天级来源，没有逐条验证其中的票价、时间、交通和建议。'
                    : undefined,
                }}
              />
            </div>
          </Card>
          </div>
        )
      })}
    </div>
  )
}

const SUPPORT_MODE_LABEL: Record<EvidenceClaim['support_mode'], string> = {
  direct: '声明：原文支持',
  amap: '声明：高德实测',
  derived: '声明：综合推导',
  estimate: '类型：经验估算',
  user_constraint: '来源：用户要求',
  unverified: '声明：未核验',
}

const EVIDENCE_SUPPORT_MODES = new Set<EvidenceClaim['support_mode']>(['direct', 'amap', 'derived'])

function resolvedEntityForClaim(
  claim: EvidenceClaim,
  resolvedEntities?: Map<string, RouteMapEntity>,
): RouteMapEntity | undefined {
  if (claim.claim_type !== 'poi_identity' || !resolvedEntities) return undefined
  const names = [claim.claim_text, claim.subject, ...(claim.claimed_entities ?? [])]
  return names.map((name) => name?.trim()).filter(Boolean)
    .map((name) => resolvedEntities.get(name!))
    .find((entity) => entity !== undefined)
}

function claimResult(claim: EvidenceClaim, claimChecks: EvidenceCheck[]): EvidenceCheck['result'] | undefined {
  const latestByScope = new Map<string, EvidenceCheck>()
  claimChecks.forEach((check) => latestByScope.set(check.scope, check))
  const scopedChecks = [...latestByScope.values()]
  const blocking = scopedChecks.find((check) => check.result === 'fail' || check.result === 'error')
  if (blocking) return blocking.result
  if (scopedChecks.some((check) => check.result === 'inconclusive')) return 'inconclusive'

  const substantive = latestByScope.get('substantive_claim')
  const identity = latestByScope.get('entity_identity')
  const source = latestByScope.get('source_availability')
  const identityOnly = claim.claim_type === 'poi_identity' || substantive?.method === 'out_of_scope'
  if (identityOnly) {
    if (identity?.result === 'pass' && (!source || source.result === 'pass')) return 'pass'
    return identity?.result ?? source?.result ?? substantive?.result
  }
  return substantive?.result ?? identity?.result ?? source?.result
}

function ClaimRows({
  claims,
  checks,
  resolvedEntities,
  displayMode = 'evidence',
}: {
  claims: EvidenceClaim[]
  checks: EvidenceCheck[]
  resolvedEntities?: Map<string, RouteMapEntity>
  displayMode?: 'evidence' | 'planning' | 'unverified'
}) {
  return (
    <div className="space-y-2">
      {claims.map((claim) => {
        const claimChecks = checks.filter((check) => check.claim_id === claim.claim_id)
        const resolvedEntity = resolvedEntityForClaim(claim, resolvedEntities)
        const result = resolvedEntity ? 'pass' : claimResult(claim, claimChecks)
        const snapshots = claim.evidence_snapshots ?? []
        const statusLabel = displayMode === 'planning'
          ? '无需外部核验'
          : displayMode === 'unverified'
            ? '缺少证据'
            : result
              ? (RESULT_LABEL[result] ?? result)
              : '尚无结论'
        const statusVariant: BadgeVariant = displayMode === 'planning'
          ? 'pending'
          : displayMode === 'unverified'
            ? 'warn'
            : result
              ? checkVariant(result)
              : 'pending'
        return (
          <details key={claim.claim_id} className="border border-border rounded-md px-3 py-2 group/claim">
            <summary className="list-none cursor-pointer flex items-start gap-2 min-w-0">
              <Icon name="fileText" className="w-3.5 h-3.5 mt-0.5 text-brand-dark shrink-0" />
              <span className="text-xs font-semibold text-ink flex-1 break-words">{claim.claim_text}</span>
              <Badge variant={statusVariant}>{statusLabel}</Badge>
              <Badge variant={resolvedEntity ? 'ok' : claim.support_mode === 'unverified' ? 'warn' : 'brand'}>
                {resolvedEntity ? '高德实体已确认' : SUPPORT_MODE_LABEL[claim.support_mode]}
              </Badge>
              <Icon name="chevronDown" className="w-3.5 h-3.5 mt-0.5 transition-transform group-open/claim:rotate-180" />
            </summary>
            <div className="mt-3 space-y-2">
              <div className="text-[11px] text-ink-3 break-words">
                <b className="text-ink-2">事实类型：</b>{claim.claim_type || 'other'}
                <span className="mx-1.5">·</span>
                <b className="text-ink-2">位置：</b>{claim.target_path}
                {claim.provenance_mode === 'reconstructed' && (
                  <span className="ml-2"><Badge variant="pending">历史重建</Badge></span>
                )}
              </div>
              {resolvedEntity ? (
                <div className="rounded-md border border-border bg-bg-inset px-3 py-2 text-xs text-ink-2">
                  <b className="text-ink">高德实体：</b>
                  {resolvedEntity.selected?.provider_name || resolvedEntity.original_name}
                  {resolvedEntity.selected?.address && <span>·{resolvedEntity.selected.address}</span>}
                  {resolvedEntity.selected?.provider_id && (
                    <span className="ml-1.5 font-mono text-ink-3">{resolvedEntity.selected.provider_id}</span>
                  )}
                </div>
              ) : snapshots.length > 0 ? snapshots.map((snapshot, index) => (
                <EvidenceRow
                  key={`${snapshot.source.chunk_id}-${index}`}
                  evidence={{
                    chunk_id: snapshot.source.chunk_id || '',
                    role: 'cited',
                    platform: snapshot.source.platform,
                    author: snapshot.source.author,
                    doc_id: snapshot.source.doc_id,
                    source_url: snapshot.source.source_url,
                    excerpt: snapshot.excerpt,
                  }}
                />
              )) : (
                <p className="text-xs text-ink-3">
                  {displayMode === 'planning'
                    ? '这是规划依据，不要求生成时证据快照。'
                    : displayMode === 'unverified'
                      ? '这条事实尚无可靠证据快照。'
                      : '这条事实没有生成时证据快照。'}
                </p>
              )}
              {claim.derived_from && claim.derived_from.length > 0 && (
                <p className="text-[11px] text-ink-3 break-all">
                  <b className="text-ink-2">推导依赖：</b>{claim.derived_from.join('、')}
                </p>
              )}
              {claimChecks.map((check, index) => (
                <CheckRow key={`${check.target_key}:${check.scope}`} check={check} step={index + 1} />
              ))}
            </div>
          </details>
        )
      })}
    </div>
  )
}

function ClaimsSection({
  claims,
  checks,
  resolvedEntities,
  title = '关键事实与证据',
}: {
  claims: EvidenceClaim[]
  checks: EvidenceCheck[]
  resolvedEntities?: Map<string, RouteMapEntity>
  title?: string
}) {
  if (claims.length === 0) return null

  const evidenceClaims = claims.filter(
    (claim) => EVIDENCE_SUPPORT_MODES.has(claim.support_mode) || resolvedEntityForClaim(claim, resolvedEntities),
  )
  const unverifiedClaims = claims.filter(
    (claim) => claim.support_mode === 'unverified' && !resolvedEntityForClaim(claim, resolvedEntities),
  )
  const planningClaims = claims.filter((claim) => claim.support_mode === 'estimate' || claim.support_mode === 'user_constraint')
  const counts = { pass: 0, fail: 0, other: 0 }
  evidenceClaims.forEach((claim) => {
    const result = resolvedEntityForClaim(claim, resolvedEntities)
      ? 'pass'
      : claimResult(claim, checks.filter((check) => check.claim_id === claim.claim_id))
    if (result === 'pass') counts.pass += 1
    else if (result === 'fail' || result === 'error') counts.fail += 1
    else counts.other += 1
  })

  return (
    <section className="mt-4 border-t border-border pt-3">
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <h4 className="text-xs font-bold text-ink">{title}</h4>
        <Badge variant={counts.fail > 0 ? 'danger' : counts.other > 0 ? 'pending' : 'ok'}>
          {evidenceClaims.length} 条 · 通过 {counts.pass} · 异常 {counts.fail} · 待判断 {counts.other}
        </Badge>
      </div>
      {evidenceClaims.length > 0 ? (
        <ClaimRows claims={evidenceClaims} checks={checks} resolvedEntities={resolvedEntities} />
      ) : (
        <p className="text-xs text-ink-3">当前没有带 chunk、高德实测或可追溯推导的关键事实。</p>
      )}
      {unverifiedClaims.length > 0 && (
        <details className="mt-2 border border-border rounded-md px-3 py-2 group/unverified">
          <summary className="list-none cursor-pointer flex items-center gap-2 text-xs font-semibold text-ink">
            <Icon name="chevronDown" className="w-3.5 h-3.5 transition-transform group-open/unverified:rotate-180" />
            <span>待补证事实</span>
            <Badge variant="warn">{unverifiedClaims.length} 条</Badge>
          </summary>
          <div className="mt-2">
            <ClaimRows claims={unverifiedClaims} checks={checks} displayMode="unverified" />
          </div>
        </details>
      )}
      {planningClaims.length > 0 && (
        <details className="mt-2 border border-border rounded-md px-3 py-2 group/planning">
          <summary className="list-none cursor-pointer flex items-center gap-2 text-xs font-semibold text-ink">
            <Icon name="chevronDown" className="w-3.5 h-3.5 transition-transform group-open/planning:rotate-180" />
            <span>规划依据与用户要求</span>
            <Badge variant="pending">{planningClaims.length} 条·无需外部核验</Badge>
          </summary>
          <div className="mt-2">
            <ClaimRows claims={planningClaims} checks={checks} displayMode="planning" />
          </div>
        </details>
      )}
    </section>
  )
}

function SourceChip({ source }: { source: SourceRef }) {
  const label = [source.platform, source.doc_id, source.chunk_id].filter(Boolean).join('/')
  const content = (
    <>
      <Icon name="link" className="w-2.75 h-2.75" />
      <span className="break-all">{label || '未知来源'}</span>
    </>
  )
  const className = "inline-flex items-center gap-1 text-[11px] text-ink-3 bg-bg-inset rounded-full px-2.5 py-1 max-w-full"
  return source.source_url ? (
    <a className={className} href={source.source_url} target="_blank" rel="noreferrer">
      {content}
    </a>
  ) : (
    <span className={className}>{content}</span>
  )
}

function CategoryTab({
  categories,
  isPitfall,
  checks,
  focusUsage,
}: {
  categories: import('../api/types').LifestyleOutput | import('../api/types').PitfallOutput | null
  isPitfall: boolean
  checks: EvidenceCheck[]
  focusUsage?: string
}) {
  const keys = Object.keys(CATEGORY_LABEL) as (keyof typeof CATEGORY_LABEL)[]
  const [active, setActive] = useState(keys[0])
  const section = isPitfall ? 'pitfall' : 'lifestyle'

  useEffect(() => {
    if (!focusUsage || !focusUsage.startsWith(`${section}:`)) return
    const field = focusUsage.split(':')[1] as keyof typeof CATEGORY_LABEL
    if (keys.includes(field)) setActive(field)
  }, [focusUsage, keys, section])

  useEffect(() => {
    if (!focusUsage || !focusUsage.startsWith(`${section}:${active}:`)) return
    scrollToUsage(focusUsage)
  }, [active, focusUsage, section])

  if (!categories) return <p className="text-ink-3">还没有生成这部分内容。</p>
  const items =
    (categories as unknown as Record<string, { items: (LifestyleItem | PitfallItem)[] }>)[active]
      ?.items ?? []

  return (
    <div>
      <div className="scroll-boundary-x flex gap-1.5 mb-4 overflow-x-auto">
        {keys.map((k) => (
          <button
            key={k}
            onClick={() => setActive(k)}
            className={`text-xs font-semibold px-3.5 py-1.5 rounded-full whitespace-nowrap ${
              active === k ? 'bg-brand-soft text-brand-dark' : 'bg-bg-inset text-ink-2'
            }`}
          >
            {CATEGORY_LABEL[k]}{' '}
            <span className="text-ink-3 font-normal">
              · {(categories as unknown as Record<string, { items: unknown[] }>)[k]?.items.length ?? 0}
            </span>
          </button>
        ))}
      </div>
      {items.length === 0 ? (
        <p className="text-ink-3 text-sm">
          {isPitfall ? '这一项没有查到有效的避雷信息，没有强行凑内容。' : '本次没有生成这一项内容。'}
        </p>
      ) : (
        items.map((item, itemIndex) => {
          const targetKey = `${section}:${active}:day:${item.day}:item:${itemIndex}`
          const itemChecks = checks.filter(
            (check) => check.target_key === targetKey || check.target_key.startsWith(`${targetKey}:claim:`),
          )
          return (
            <div
              id={usageAnchor(targetKey)}
              key={targetKey}
              className={`scroll-mt-6 ${focusUsage === targetKey ? 'outline outline-2 outline-brand rounded-xl' : ''}`}
            >
            <Card className="p-4 mb-2.5">
              <div className="flex items-center gap-2 mb-1.5">
                <span className="text-[11px] font-bold text-brand-dark bg-brand-soft rounded px-1.75 py-0.5">
                  Day{item.day}
                </span>
                <span className="ml-auto">
                  <CheckSummaryBadge checks={itemChecks} feasibilityChecks={[]} />
                </span>
              </div>
              {isPitfall ? (
                <>
                  <div className="text-xs text-ink-3 mb-1.5 break-words">
                    针对推荐：{(item as PitfallItem).target_recommendation}
                  </div>
                  <div className="text-[13.5px] leading-relaxed text-danger bg-danger-soft rounded-lg px-3 py-2.5 break-words">
                    {(item as PitfallItem).warning || '（未查到具体避雷内容）'}
                  </div>
                </>
              ) : (
                <div className="text-[13.5px] leading-relaxed break-words">{(item as LifestyleItem).recommendation}</div>
              )}
              {(item as LifestyleItem).claimed_entities && (item as LifestyleItem).claimed_entities!.length > 0 && (
                <div className="mt-2 flex gap-1.5 flex-wrap">
                  {(item as LifestyleItem).claimed_entities!.map((entity) => (
                    <span key={entity} className="text-xs text-ink-2 bg-bg-inset border border-border rounded-full px-2.5 py-0.5">
                      {entity}
                    </span>
                  ))}
                </div>
              )}
              <ClaimsSection
                claims={item.claims ?? []}
                checks={itemChecks}
                title={isPitfall ? '避雷事实与证据' : `${CATEGORY_LABEL[active]}事实与证据`}
              />
              <EvidenceAndChecks
                sources={item.sources ?? []}
                checks={itemChecks}
                feasibilityChecks={[]}
                subject={{
                  title: isPitfall ? '避雷说法' : `${CATEGORY_LABEL[active]} 建议`,
                  content: isPitfall
                    ? (item as PitfallItem).warning || '（未查到具体避雷内容）'
                    : (item as LifestyleItem).recommendation,
                }}
              />
            </Card>
            </div>
          )
        })
      )}
    </div>
  )
}

function PoiDetailTab({
  details,
  checks,
  focusUsage,
}: {
  details: PoiDetailFile[]
  checks: EvidenceCheck[]
  focusUsage?: string
}) {
  useEffect(() => scrollToUsage(focusUsage), [focusUsage])

  if (details.length === 0) return <p className="text-ink-3">还没有生成POI详情。</p>

  return (
    <div className="space-y-4">
      {details
        .slice()
        .sort((left, right) => left.day - right.day)
        .map((detail) => (
          <Card key={`${detail.day}-${detail.poi_name}`} className="overflow-hidden">
            <div className="flex items-center gap-2.5 px-4 py-3 bg-bg-inset border-b border-border">
              <Badge variant="brand">Day{detail.day}</Badge>
              <b className="text-[13.5px]">{detail.poi_name}</b>
              <Badge variant={detail.citation_status === 'passed' ? 'ok' : 'warn'}>
                {detail.citation_status === 'passed' ? '核验通过' : '部分未通过'}
              </Badge>
            </div>
            <div className="px-4 py-3.5 space-y-4">
              {detail.routes.map((route) => (
                <section key={route.route_name}>
                  <div className="flex items-center gap-2 mb-2">
                    <h4 className="text-xs font-bold text-ink">{route.route_name}</h4>
                    {route.recommended && <Badge variant="ok">推荐路线</Badge>}
                  </div>
                  <div className="space-y-2">
                    {route.route_segments
                      .slice()
                      .sort((left, right) => left.order - right.order)
                      .map((segment) => {
                        const usageKey = `poi_detail:day:${detail.day}:poi:${detail.poi_name}:route:${route.route_name}:order:${segment.order}`
                        const checkKey = `poi_detail:${detail.poi_name}:route:${route.route_name}:order:${segment.order}`
                        const segmentChecks = checks.filter(
                          (check) => check.target_key === checkKey || check.target_key.startsWith(`${checkKey}:claim:`),
                        )
                        const content = [segment.point_name, segment.highlight, segment.suggested_duration]
                          .filter(Boolean)
                          .join(' · ')
                        return (
                          <div
                            id={usageAnchor(usageKey)}
                            key={usageKey}
                            className={`border-t border-dashed border-border pt-2 scroll-mt-6 ${
                              focusUsage === usageKey ? 'outline outline-2 outline-brand rounded-md px-2' : ''
                            }`}
                          >
                            <div className="flex items-start gap-2">
                              <span className="w-5 h-5 rounded-full bg-bg-inset flex items-center justify-center text-[11px] font-bold shrink-0">
                                {segment.order}
                              </span>
                              <div className="min-w-0 flex-1">
                                <div className="text-[13px] font-semibold">{segment.point_name}</div>
                                {segment.highlight && <p className="text-xs text-ink-2 mt-1">{segment.highlight}</p>}
                                {segment.suggested_duration && (
                                  <p className="text-[11px] text-ink-3 mt-1">建议停留：{segment.suggested_duration}</p>
                                )}
                              </div>
                              <CheckSummaryBadge checks={segmentChecks} feasibilityChecks={[]} />
                            </div>
                            <ClaimsSection
                              claims={segment.claims ?? []}
                              checks={segmentChecks}
                              title="途经点事实与证据"
                            />
                            <EvidenceAndChecks
                              sources={segment.sources ?? []}
                              checks={segmentChecks}
                              feasibilityChecks={[]}
                              subject={{ title: 'POI途经点', content }}
                            />
                          </div>
                        )
                      })}
                  </div>
                </section>
              ))}
            </div>
          </Card>
        ))}
    </div>
  )
}

function CheckSummaryBadge({
  checks,
  feasibilityChecks,
}: {
  checks: EvidenceCheck[]
  feasibilityChecks: RouteFeasibilityCheck[]
}) {
  const results = [
    ...checks.map((check) => check.result),
    ...feasibilityChecks.map((check) => feasibilityResult(check.status)),
  ]
  let variant: BadgeVariant = 'pending'
  let label = '尚无校验记录'
  if (results.some((result) => result === 'fail' || result === 'error')) {
    variant = 'danger'
    label = '校验有异常'
  } else if (results.some((result) => result === 'inconclusive')) {
    variant = 'warn'
    label = '存在无法判断'
  } else if (results.some((result) => result === 'not_checked')) {
    variant = 'pending'
    label = '部分未核验'
  } else if (results.some((result) => result === 'pass')) {
    variant = 'ok'
    label = '有校验记录'
  } else if (results.length > 0) {
    label = '检查范围有限'
  }
  return <Badge variant={variant}>{label}</Badge>
}

function EvidenceAndChecks({
  sources,
  checks,
  feasibilityChecks,
  subject,
}: {
  sources: SourceRef[]
  checks: EvidenceCheck[]
  feasibilityChecks: RouteFeasibilityCheck[]
  subject: { title: string; content: string; note?: string }
}) {
  const hasContent = sources.length > 0 || checks.length > 0 || feasibilityChecks.length > 0
  const isBackfill = checks.some((check) => check.observation_mode === 'backfill_current_state')
  const orderedChecks = [...checks].sort(
    (left, right) => (CHECK_SCOPE_ORDER[left.scope] ?? 99) - (CHECK_SCOPE_ORDER[right.scope] ?? 99),
  )

  return (
    <details className="mt-3 border-t border-border pt-3 group">
      <summary className="list-none cursor-pointer flex items-center gap-2 text-[12.5px] font-semibold text-ink-2 select-none">
        <Icon name="fileText" className="w-3.5 h-3.5 text-brand-dark" />
        天级核验详情（供人工查看）
        <Icon name="chevronDown" className="w-3.5 h-3.5 ml-auto transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-3 space-y-3">
        <section className="border border-border bg-bg-inset px-3 py-2.5 rounded-md">
          <div className="text-[11px] font-bold text-ink-3 mb-1">核验对象</div>
          <div className="text-xs font-semibold text-ink">{subject.title}</div>
          <p className="mt-1 text-xs leading-relaxed text-ink-2 break-words">{subject.content}</p>
          {subject.note && <p className="mt-2 text-xs leading-relaxed text-[#8a6417] break-words">{subject.note}</p>}
        </section>

        {isBackfill && (
          <div className="border border-warn/40 bg-warn-soft px-3 py-2 rounded-md text-xs leading-relaxed text-ink-2">
            <b className="text-ink">记录性质：</b>这是用当前产物和当前可读材料重建的历史回填，不是生成当时完整的模型调用轨迹。
          </div>
        )}

        <section className="min-w-0">
          <h4 className="text-xs font-bold text-ink mb-1">天级汇总来源（兼容字段）</h4>
          <p className="text-[11px] leading-relaxed text-ink-3 mb-2">
            这是由本天各条 Claim 引用汇总得到的 sources 兼容字段，不代表每条说法都被所有材料支持。逐条对应关系以上方关键事实的证据快照为准。
          </p>
          {sources.length > 0 ? (
            <div className="flex gap-1.5 flex-wrap">
              {sources.map((source, index) => <SourceChip key={`${source.chunk_id}-${index}`} source={source} />)}
            </div>
          ) : (
            <p className="text-xs text-ink-3">产物没有声明引用来源。</p>
          )}
        </section>

        <section className="min-w-0">
          <h4 className="text-xs font-bold text-ink mb-2">天级检查与路线可行性</h4>
          {!hasContent ? (
            <p className="text-xs text-ink-3">尚无来源和核验记录。</p>
          ) : checks.length === 0 && feasibilityChecks.length === 0 ? (
            <p className="text-xs text-ink-3">尚未保存核验器的解释结果。</p>
          ) : (
            <div className="space-y-2">
              {orderedChecks.map((check, index) => (
                <CheckRow key={`${check.target_key}:${check.scope}`} check={check} step={index + 1} />
              ))}
              {feasibilityChecks.map((check, index) => (
                <FeasibilityRow key={check.check_id} check={check} step={orderedChecks.length + index + 1} />
              ))}
            </div>
          )}
        </section>
      </div>
    </details>
  )
}

function EvidenceRow({ evidence }: { evidence: EvidenceCheckEvidence }) {
  const source: SourceRef = {
    platform: evidence.platform || (evidence.role === 'checker_supplement' ? '校验器补充' : '来源'),
    author: evidence.author,
    doc_id: evidence.doc_id,
    chunk_id: evidence.chunk_id,
    source_url: evidence.source_url,
  }
  return (
    <div className="border border-border bg-bg-inset px-3 py-2 rounded-md min-w-0">
      <div className="flex items-start gap-2 flex-wrap">
        <SourceChip source={source} />
        <Badge variant={evidence.role === 'checker_supplement' ? 'warn' : 'brand'}>
          {evidence.role === 'checker_supplement' ? '校验器补充读取' : '产物引用'}
        </Badge>
      </div>
      {evidence.excerpt ? (
        <p className="mt-2 text-xs leading-relaxed text-ink-2 whitespace-pre-wrap break-words">{evidence.excerpt}</p>
      ) : (
        <p className="mt-2 text-xs text-ink-3">该来源没有可展示的原文摘录。</p>
      )}
    </div>
  )
}

function CheckRow({ check, step }: { check: EvidenceCheck; step: number }) {
  const explanation = SCOPE_EXPLANATION[check.scope]
  const evidence = uniqueEvidence(check.evidence ?? [])
  const executed = checkExecuted(check)
  const resultPresentation = checkResultPresentation(check)
  return (
    <div className="border border-border px-3 py-2 rounded-md min-w-0">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-bold text-ink-3">步骤 {step}</span>
        <span className="text-xs font-semibold text-ink">{SCOPE_LABEL[check.scope] ?? check.scope}</span>
        <Badge variant={resultPresentation.variant}>{resultPresentation.label}</Badge>
        {check.observation_mode === 'backfill_current_state' && <Badge variant="pending">历史记录重建</Badge>}
      </div>
      {explanation && (
        <div className="mt-2 grid gap-1.5 text-xs leading-relaxed">
          <AuditLine label="核验问题" text={explanation.question} />
          <AuditLine label="判定规则" text={explanation.rule} />
          <AuditLine label="本次执行" text={methodExecutionText(check)} />
          <AuditLine label="本次结果" text={check.reason} />
          <AuditLine label="判断边界" text={explanation.boundary} muted />
        </div>
      )}
      {!explanation && <p className="mt-1.5 text-xs leading-relaxed text-ink-2 break-words">{check.reason}</p>}
      {check.checked_chunk_ids && check.checked_chunk_ids.length > 0 && (
        <p className="mt-2 text-[11px] text-ink-3 break-all">
          <b>{executed ? '本步骤实际读取：' : '关联材料（本次未执行判断）：'}</b>
          {check.checked_chunk_ids.join('、')}
        </p>
      )}
      {evidence.length > 0 && (
        <details className="mt-2 group/evidence">
          <summary className="list-none cursor-pointer text-[11px] font-semibold text-brand-dark">
            {executed ? '查看本步骤读取的原文' : '查看本步骤关联的材料'}（{evidence.length}）
          </summary>
          <div className="mt-2 space-y-2">
            {evidence.map((item) => <EvidenceRow key={`${item.role}:${item.chunk_id}`} evidence={item} />)}
          </div>
        </details>
      )}
    </div>
  )
}

function AuditLine({ label, text, muted = false }: { label: string; text: string; muted?: boolean }) {
  return (
    <div className="grid grid-cols-[68px_minmax(0,1fr)] gap-2">
      <b className="text-ink">{label}</b>
      <span className={`${muted ? 'text-ink-3' : 'text-ink-2'} break-words`}>{text}</span>
    </div>
  )
}

function FeasibilityRow({ check, step }: { check: RouteFeasibilityCheck; step: number }) {
  const statusMap: Record<RouteFeasibilityCheck['status'], { label: string; variant: BadgeVariant }> = {
    passed: { label: '通过', variant: 'ok' },
    warning: { label: '警告', variant: 'warn' },
    blocked: { label: '阻断', variant: 'danger' },
    unknown: { label: '未知', variant: 'pending' },
  }
  const status = statusMap[check.status]
  return (
    <div className="border border-border px-3 py-2 rounded-md min-w-0">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-bold text-ink-3">步骤 {step}</span>
        <span className="text-xs font-semibold text-ink">路线可行性 · {check.target}</span>
        <Badge variant={status.variant}>{status.label}</Badge>
        <span className="text-[11px] text-ink-3">{check.category}</span>
      </div>
      <div className="mt-2 grid gap-1.5 text-xs leading-relaxed">
        <AuditLine label="核验问题" text="这个地点、道路或日程安排在当前条件下是否可执行？" />
        <AuditLine label="核验方法" text={`按 ${check.category} 类别读取地图或联网证据并判定。`} />
        <AuditLine label="本次结果" text={check.summary || check.technical_error || '核验器没有提供说明。'} />
        <AuditLine label="判断边界" text="只覆盖本条记录指明的对象和类别，不代表整天所有安排均已验证。" muted />
      </div>
      {check.evidence && check.evidence.length > 0 && (
        <div className="mt-2 space-y-1.5">
          {check.evidence.map((item, index) => (
            <div key={`${item.url}-${index}`} className="text-[11px] text-ink-3 break-words">
              {item.title && <b className="text-ink-2">{item.title}：</b>}
              {item.excerpt}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function uniqueEvidence(items: EvidenceCheckEvidence[]): EvidenceCheckEvidence[] {
  const seen = new Set<string>()
  return items.filter((item) => {
    const key = `${item.role}:${item.chunk_id}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function checkVariant(result: EvidenceCheck['result']): BadgeVariant {
  if (result === 'pass') return 'ok'
  if (result === 'fail' || result === 'error') return 'danger'
  if (result === 'inconclusive') return 'warn'
  return 'pending'
}

function feasibilityResult(status: RouteFeasibilityCheck['status']): EvidenceCheck['result'] {
  if (status === 'passed') return 'pass'
  if (status === 'blocked') return 'fail'
  return 'inconclusive'
}

function guideWithoutReferenceSection(finalGuide: string): string {
  const heading = /^#{1,6}\s+(?:📚\s*)?参考来源[^\n]*$/m
  const match = heading.exec(finalGuide)
  if (!match || match.index === undefined) return finalGuide
  const before = finalGuide.slice(0, match.index).trimEnd()
  const afterReference = finalGuide.slice(match.index + match[0].length)
  const poiHeading = /^##\s+🌳\s+重点景点深度玩法[^\n]*$/m
  const poiMatch = poiHeading.exec(afterReference)
  if (!poiMatch || poiMatch.index === undefined) return before
  return `${before}\n\n---\n\n${afterReference.slice(poiMatch.index).trimStart()}`
}

function ReferenceUsageList({
  references,
  onNavigateUsage,
}: {
  references: ReferenceEntry[]
  onNavigateUsage: (usageKey: string) => void
}) {
  if (references.length === 0) return null
  const totalUsages = references.reduce((sum, reference) => sum + (reference.cited_in?.length ?? 0), 0)
  return (
    <details className="mt-5 border-t border-border pt-4 group">
      <summary className="list-none cursor-pointer flex items-center gap-2 select-none">
        <Icon name="fileText" className="w-4 h-4 text-brand-dark" />
        <span className="text-sm font-bold text-ink">引用来源总表</span>
        <Badge variant="brand">{references.length}条来源 · {totalUsages}处声明使用</Badge>
        <Icon name="chevronDown" className="w-4 h-4 ml-auto transition-transform group-open:rotate-180" />
      </summary>
      <p className="mt-2 text-xs leading-relaxed text-ink-3">
        按chunk去重展示。“声明使用位置”只表示该来源写入了对应对象的sources，不代表它支持对象里的每一句话。
      </p>
      <div className="mt-3 border-y border-border divide-y divide-border">
        {references.map((reference) => {
          const usages = reference.cited_in ?? []
          const sourceLabel = [reference.platform, reference.doc_id, reference.chunk_id].filter(Boolean).join(' / ')
          return (
            <div key={`${reference.platform}:${reference.chunk_id}`} className="py-3">
              <div className="flex items-start gap-2 flex-wrap">
                {reference.source_url ? (
                  <a
                    href={reference.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs font-semibold text-brand-dark break-all"
                  >
                    {sourceLabel || '未知来源'}
                  </a>
                ) : (
                  <span className="text-xs font-semibold text-ink break-all">{sourceLabel || '未知来源'}</span>
                )}
                <Badge variant={usages.length > 0 ? 'pending' : 'warn'}>{usages.length}处</Badge>
              </div>
              {reference.excerpt && (
                <p className="mt-1.5 text-xs leading-relaxed text-ink-2 break-words">{reference.excerpt}</p>
              )}
              {usages.length > 0 ? (
                <div className="mt-2 flex gap-1.5 flex-wrap">
                  {usages.map((usageKey) => (
                    <button
                      key={usageKey}
                      type="button"
                      onClick={() => onNavigateUsage(usageKey)}
                      className="inline-flex items-center gap-1 text-[11px] text-brand-dark bg-brand-soft rounded-full px-2.5 py-1"
                    >
                      <Icon name="link" className="w-2.75 h-2.75" />
                      {usageLabel(usageKey)}
                    </button>
                  ))}
                </div>
              ) : (
                <p className="mt-2 text-[11px] text-ink-3">这份历史记录没有保存声明使用位置。</p>
              )}
            </div>
          )
        })}
      </div>
    </details>
  )
}

function IntegrationTab({
  finalGuide,
  blocks,
  references,
  onNavigateUsage,
}: {
  finalGuide: string | null
  blocks: FinalGuideBlocks | null
  references: ReferenceEntry[]
  onNavigateUsage: (usageKey: string) => void
}) {
  const renderedGuide = finalGuide && references.length > 0
    ? guideWithoutReferenceSection(finalGuide)
    : finalGuide
  return (
    <Card className="p-5">
      <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2.5">
        最终攻略正文预览（final_guide.md）
      </div>
      {renderedGuide ? (
        <div className="markdown-body text-[13.5px]">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{renderedGuide}</ReactMarkdown>
        </div>
      ) : (
        <p className="text-ink-3">还没有生成最终攻略。</p>
      )}
      <FinalGuideMappingList blocks={blocks} hasGuide={Boolean(renderedGuide)} onNavigateUsage={onNavigateUsage} />
      <ReferenceUsageList references={references} onNavigateUsage={onNavigateUsage} />
    </Card>
  )
}

const GUIDE_MAPPING_LABEL: Record<string, string> = {
  upstream: '上游事实',
  editorial: '编辑性内容',
  user_constraint: '用户要求',
  unverified: '未核验提示',
}

function guideMappingVariant(mode: string): BadgeVariant {
  if (mode === 'upstream') return 'ok'
  if (mode === 'unverified') return 'warn'
  if (mode === 'user_constraint') return 'brand'
  return 'pending'
}

function FinalGuideMappingList({
  blocks,
  hasGuide,
  onNavigateUsage,
}: {
  blocks: FinalGuideBlocks | null
  hasGuide: boolean
  onNavigateUsage: (usageKey: string) => void
}) {
  if (!hasGuide) return null
  if (!blocks) {
    return (
      <div className="mt-5 border-t border-border pt-4 text-xs text-ink-3">
        这份历史正文没有保存结构化的段落与claim映射。
      </div>
    )
  }

  const mappedClaims = blocks.blocks.reduce(
    (sum, block) => sum + (block.upstream_claim_ids?.length ?? 0),
    0,
  )
  return (
    <details className="mt-5 border-t border-border pt-4 group">
      <summary className="list-none cursor-pointer flex items-center gap-2 select-none">
        <Icon name="link" className="w-4 h-4 text-brand-dark" />
        <span className="text-sm font-bold text-ink">正文事实映射</span>
        <Badge variant="brand">{blocks.blocks.length}个正文块 · {mappedClaims}条claim关联</Badge>
        <Icon name="chevronDown" className="w-4 h-4 ml-auto transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-3 border-y border-border divide-y divide-border">
        {blocks.blocks.map((block) => (
          <div key={block.block_id} className="py-3">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-bold text-ink">{block.section}</span>
              <Badge variant={guideMappingVariant(block.mapping_mode)}>
                {GUIDE_MAPPING_LABEL[block.mapping_mode] ?? block.mapping_mode}
              </Badge>
              {block.upstream_claim_ids && block.upstream_claim_ids.length > 0 && (
                <span className="text-[11px] text-ink-3">{block.upstream_claim_ids.length}条claim</span>
              )}
            </div>
            <div className="mt-2 pl-3 border-l-2 border-border text-xs text-ink-2 markdown-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.markdown}</ReactMarkdown>
            </div>
            {block.mapping_note && (
              <p className="mt-2 text-[11px] leading-relaxed text-ink-3">{block.mapping_note}</p>
            )}
            {(block.claim_snapshots?.length ?? 0) > 0 && (
              <div className="mt-2 space-y-2">
                {block.claim_snapshots?.map((claim) => (
                  <div key={claim.claim_id} className="text-xs leading-relaxed">
                    <div className="flex items-start gap-2 flex-wrap">
                      <span className="text-ink-2 break-words">{claim.claim_text}</span>
                      <Badge variant={claim.support_mode === 'unverified' ? 'warn' : 'pending'}>
                        {claim.support_mode}
                      </Badge>
                    </div>
                    <div className="mt-1 flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-[10px] text-ink-3 break-all">{claim.source_path}</span>
                      {claim.usage_key && (
                        <button
                          type="button"
                          onClick={() => onNavigateUsage(claim.usage_key!)}
                          className="inline-flex items-center gap-1 text-[11px] text-brand-dark bg-brand-soft rounded-full px-2.5 py-1"
                        >
                          <Icon name="link" className="w-2.75 h-2.75" />
                          {usageLabel(claim.usage_key)}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </details>
  )
}
