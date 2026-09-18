import { Link } from 'react-router-dom'
import type { MissingEntityReviewContext } from '../api/types'

function EvidenceText({ text }: { text: string }) {
  if (text.length <= 220) return <span>{text}</span>
  return (
    <details>
      <summary className="cursor-pointer leading-relaxed">{text.slice(0, 220)}… <span className="font-semibold text-brand-dark">展开原文</span></summary>
      <div className="mt-1.5 leading-relaxed">{text}</div>
    </details>
  )
}

function EntityText({ text, entity }: { text: string; entity: string }) {
  const parts = text.split(entity)
  if (parts.length === 1) return <>{text}</>
  return <>{parts.map((part, index) => (
    <span key={`${part}-${index}`}>
      {index > 0 && <mark className="rounded-sm bg-warn-soft px-0.5 text-ink-1">{entity}</mark>}
      {part}
    </span>
  ))}</>
}

const resolutionPresentation = {
  verified_downstream: { label: '下游有已核验候选', className: 'bg-ok-soft text-ok' },
  candidate_found: { label: '找到素材候选', className: 'bg-brand-soft text-brand-dark' },
  needs_evidence: { label: '仍缺证据', className: 'bg-danger-soft text-danger' },
  no_longer_present: { label: '当前已不存在', className: 'bg-bg-inset text-ink-2' },
} as const

const sourceRelationPresentation = {
  mentions_entity: { label: '原文直接提及', className: 'bg-ok-soft text-ok' },
  no_exact_mention: { label: '原文未直接提及', className: 'bg-warn-soft text-[#8a6417]' },
  unreadable: { label: '未取回原文', className: 'bg-bg-inset text-ink-3' },
} as const

export function MissingEntityReviewPanel({ context, projectId }: { context: MissingEntityReviewContext; projectId: string }) {
  const impact = context.project_impact
  return (
    <section className="overflow-hidden rounded-xl border border-warn/35 bg-surface shadow-sm" aria-label="缺失实体证据链">
      <div className="border-b border-border bg-warn-soft/20 px-4 py-3.5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-ink-1">当前待确认实体</div>
            <p className="mt-1 text-xs leading-relaxed text-ink-2">
              按当前业务字段去重和定位，不再把 <span className="font-semibold">missing_entities</span> 里的重复名称当成不同问题。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-md bg-surface px-2.5 py-1.5 text-xs font-semibold text-ink-1 shadow-sm ring-1 ring-border">
              {context.entity_count} 个唯一实体 · {context.occurrence_count} 个字段位置
            </span>
            <Link to={`/projects/${projectId}/outputs`} target="_blank" rel="noreferrer" className="text-xs font-semibold text-brand-dark hover:underline">
              查看 Agent 产出
            </Link>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap gap-2 text-[11px]">
          <span className={`rounded px-2 py-1 font-semibold ${impact.affects_project_state ? 'bg-danger-soft text-danger' : 'bg-ok-soft text-ok'}`}>
            {impact.affects_project_state ? '会保留项目级待处理' : '不再影响项目状态'}
          </span>
          <span className="rounded bg-bg-inset px-2 py-1 text-ink-2">当前引用状态：{impact.citation_status || '未记录'}</span>
          <span className="rounded bg-bg-inset px-2 py-1 text-ink-2">项目阶段：{impact.project_phase || '未记录'}</span>
          {impact.final_guide_affected && (
            <span className="rounded bg-brand-soft px-2 py-1 font-semibold text-brand-dark">最终攻略已传播 · {impact.final_guide_occurrence_count} 处文本命中</span>
          )}
          <span className="rounded bg-bg-inset px-2 py-1 text-ink-3">不阻断已完成的生成产物</span>
        </div>
      </div>

      {context.stale_evidence_state && (
        <div className="border-b border-warn/25 bg-warn-soft/10 px-4 py-3 text-xs leading-relaxed text-ink-2">
          <span className="font-semibold text-[#8a6417]">历史判断已过期：</span>{context.stale_reason}
          <span className="ml-1">下面的旧失败只作审计线索，不代表当前内容仍然失败。</span>
        </div>
      )}

      <div className="divide-y divide-border">
        {context.entities.map((item, entityIndex) => {
          const resolution = resolutionPresentation[item.resolution_state]
          const passedDownstream = item.downstream_evidence.filter((evidence) => evidence.validation_status === 'passed')
          const downstreamFiles = [...new Set(item.downstream_evidence.map((evidence) => evidence.file))]
          return (
            <details key={item.entity} open={entityIndex === 0} className="group px-4 py-3">
              <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3 select-none [&::-webkit-details-marker]:hidden">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="text-xs text-ink-3 transition-transform group-open:rotate-90">▶</span>
                  <span className="truncate text-sm font-semibold text-ink-1">{item.entity}</span>
                  <span className="text-[11px] text-ink-3">出现 {item.occurrence_count} 处</span>
                </div>
                <span className={`rounded px-2 py-1 text-[11px] font-semibold ${resolution.className}`}>{resolution.label}</span>
              </summary>

              <div className="mt-3 space-y-3 pl-5">
                {item.stale_evidence_state && (
                  <div className="rounded-md border border-warn/30 bg-warn-soft/15 px-3 py-2 text-xs text-ink-2">
                    旧核验指向的 Claim 已不在当前内容中。{item.historical_failures.length > 0 && (
                      <details className="mt-1.5">
                        <summary className="cursor-pointer font-semibold text-[#8a6417]">查看 {item.historical_failures.length} 条旧失败线索</summary>
                        <div className="mt-1.5 space-y-1.5">
                          {item.historical_failures.map((failure, index) => (
                            <div key={`${failure.claim_id}-${failure.scope}-${index}`} className="rounded bg-surface px-2.5 py-2">
                              <div className="font-mono text-[10px] text-ink-3">{failure.claim_id} · {failure.scope} · {failure.checked_at || '时间未记录'}</div>
                              <div className="mt-1 text-[10px] font-semibold text-[#8a6417]">
                                {failure.match_status === 'same_field_stale_claim' ? '同一字段，但旧 Claim 已失效' : '旧 Claim 和字段都已无法对齐'}
                                {failure.checked_before_content_update ? ' · 核验早于当前内容' : ''}
                              </div>
                              {failure.checked_claim_text && <div className="mt-1 leading-relaxed text-ink-2">当时送检：{failure.checked_claim_text}</div>}
                              <div className="mt-1 leading-relaxed">{failure.reason}</div>
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
                    {(item.latest_checked_at || item.content_updated_at) && (
                      <div className="mt-1.5 text-[10px] text-ink-3">旧核验：{item.latest_checked_at || '未记录'} · 当前内容更新：{item.content_updated_at || '未记录'}</div>
                    )}
                  </div>
                )}

                <div className="grid gap-3 lg:grid-cols-3">
                  <div className="rounded-lg border border-border bg-bg-inset/50 p-3">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-ink-3">① 当前表述在哪里</div>
                    <div className="mt-2 space-y-2">
                      {item.occurrences.map((occurrence) => (
                        <div key={occurrence.json_pointer} className="rounded-md border border-border bg-surface px-2.5 py-2">
                          <div className="text-[11px] font-semibold text-ink-1">{occurrence.subject ? `${occurrence.subject} · ` : ''}{occurrence.field_label}</div>
                          <blockquote className="mt-1.5 border-l-2 border-warn/50 pl-2 text-xs leading-relaxed text-ink-2">
                            <EntityText text={occurrence.excerpt || String(occurrence.current_value ?? '')} entity={item.entity} />
                          </blockquote>
                          <div className="mt-1.5 break-all font-mono text-[10px] text-ink-3">{occurrence.json_pointer}</div>
                        </div>
                      ))}
                      {item.occurrences.length === 0 && <p className="text-xs text-ink-3">当前业务字段已不再出现该实体。</p>}
                    </div>
                  </div>

                  <div className="rounded-lg border border-border bg-bg-inset/50 p-3">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-ink-3">② 当前对象范围已挂载证据</div>
                    <p className="mt-1 text-[10px] leading-relaxed text-ink-3">可能来自同一 Day 的其他 Claim，不等于已直接挂在这个字段上。</p>
                    <div className="mt-2 space-y-2">
                      {item.current_sources.slice(0, 4).map((source, index) => {
                        const relation = sourceRelationPresentation[source.relation]
                        return (
                          <div key={`${source.ref}-${index}`} className="rounded-md border border-border bg-surface px-2.5 py-2 text-xs text-ink-2">
                            <div className="flex flex-wrap items-center justify-between gap-1.5">
                              <span className="break-all font-medium text-ink-1">{source.ref || '未记录引用 ID'}</span>
                              <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${relation.className}`}>{relation.label}</span>
                            </div>
                            {source.excerpts[0] ? <div className="mt-1.5"><EvidenceText text={source.excerpts[0]} /></div> : <div className="mt-1.5 text-ink-3">只有引用标识，当前无可读原文。</div>}
                          </div>
                        )
                      })}
                      {item.current_sources.length === 0 && <p className="text-xs text-ink-3">当前字段没有挂载可定位的引用。</p>}
                    </div>
                  </div>

                  <div className="rounded-lg border border-brand/25 bg-brand-soft/10 p-3">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-brand-dark">③ 项目内可回填的证据</div>
                    <div className="mt-2 space-y-2">
                      {passedDownstream.slice(0, 2).map((evidence) => (
                        <div key={evidence.claim_id} className="rounded-md border border-ok/25 bg-surface px-2.5 py-2 text-xs text-ink-2">
                          <div className="flex flex-wrap items-center justify-between gap-1.5">
                            <span className="font-semibold text-ink-1">POI Agent · {evidence.poi_name || evidence.file}</span>
                            <span className="rounded bg-ok-soft px-1.5 py-0.5 text-[10px] font-semibold text-ok">下游 Claim 已核验</span>
                          </div>
                          <div className="mt-1.5 leading-relaxed">{evidence.claim_text}</div>
                          {evidence.evidence_excerpts[0] && <div className="mt-1.5 border-l-2 border-ok/40 pl-2"><EvidenceText text={evidence.evidence_excerpts[0].text} /></div>}
                          <div className="mt-1.5 text-[10px] font-medium text-[#8a6417]">只作当前 Route 字段的回填候选，不会自动让 Route 通过。</div>
                        </div>
                      ))}
                      {item.candidate_evidence.slice(0, passedDownstream.length > 0 ? 1 : 2).map((candidate) => (
                        <div key={candidate.ref} className="rounded-md border border-brand/25 bg-surface px-2.5 py-2 text-xs text-ink-2">
                          <div className="flex flex-wrap items-center justify-between gap-1.5">
                            <span className="break-all font-semibold text-ink-1">{candidate.ref}</span>
                            <span className="rounded bg-brand-soft px-1.5 py-0.5 text-[10px] font-semibold text-brand-dark">素材库精确命中</span>
                          </div>
                          <div className="mt-1.5"><EvidenceText text={candidate.text} /></div>
                          <div className="mt-1.5 text-[10px] text-ink-3">命中名称 ≠ 已支持完整表述，仍需核对语义。</div>
                        </div>
                      ))}
                      {passedDownstream.length === 0 && item.candidate_evidence.length === 0 && <p className="text-xs text-ink-3">项目内暂未找到可回填的证据。</p>}
                    </div>
                  </div>
                </div>

                <div className="rounded-lg border border-border px-3 py-2.5">
                  <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-ink-2">
                    <span className="font-semibold text-ink-1">传播范围</span>
                    <span className="rounded bg-bg-inset px-2 py-1">{context.project_impact.affected_files[0] || '当前文件'}</span>
                    {downstreamFiles.length > 0 && <><span className="text-ink-3">→</span><span className="rounded bg-bg-inset px-2 py-1">POI 详情</span></>}
                    {item.final_guide_occurrences.length > 0 && <><span className="text-ink-3">→</span><span className="rounded bg-brand-soft px-2 py-1 font-semibold text-brand-dark">最终攻略：已传播（文本命中）</span></>}
                  </div>
                  {item.final_guide_occurrences.length > 0 && (
                    <details className="mt-2 text-xs text-ink-2">
                      <summary className="cursor-pointer font-semibold">查看最终攻略的 {item.final_guide_occurrences.length} 处命中</summary>
                      <div className="mt-1.5 space-y-1.5">
                        {item.final_guide_occurrences.map((occurrence) => (
                          <div key={`${occurrence.file}-${occurrence.line}`} className="rounded bg-bg-inset px-2.5 py-2">
                            <div className="font-mono text-[10px] text-ink-3">
                              final_guide.md:{occurrence.line} · {occurrence.match_mode} · {occurrence.claim_linked ? `Claim 血缘 ${occurrence.upstream_claim_ids.join(', ')}` : '未建立 Claim 血缘'}
                            </div>
                            <div className="mt-1 leading-relaxed"><EntityText text={occurrence.excerpt} entity={item.entity} /></div>
                          </div>
                        ))}
                      </div>
                    </details>
                  )}
                </div>

                <div className="rounded-lg border border-brand/25 bg-brand-soft/10 px-3 py-2.5 text-xs leading-relaxed text-ink-2">
                  <span className="font-semibold text-brand-dark">建议下一步：</span>{item.recommendation}
                </div>
              </div>
            </details>
          )
        })}
      </div>

      <div className="border-t border-border bg-bg-inset/50 px-4 py-2.5 text-[11px] leading-relaxed text-ink-3">
        判断原则：“素材提到该名称”、“下游 Claim 已通过”和“最终攻略已出现”都是不同事实。只有将适用证据回填到当前字段并重核，才能关闭这条项目级待处理。
      </div>
    </section>
  )
}
