import type { ManifestConsistencyContext } from '../api/types'

const actionLabel: Record<string, string> = {
  sync_detail_status: '同步 POI 详情状态',
  sync_manifest_status: '同步 manifest 状态',
  targeted_poi_detail_regeneration: '定向重跑该 POI 详情',
}

function StatusCard({ label, status, detail }: { label: string; status: string; detail: string }) {
  const degraded = status === 'force_finalized' || status === 'generation_failed' || status === 'missing'
  return (
    <div className="rounded-lg border border-border bg-surface px-3 py-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-ink-3">{label}</div>
      <div className={`mt-1.5 font-mono text-sm font-semibold ${degraded ? 'text-[#8a6417]' : 'text-ok'}`}>{status || '未记录'}</div>
      <div className="mt-1 break-all text-[10px] text-ink-3">{detail || '无更新信息'}</div>
    </div>
  )
}

export function ManifestConsistencyPanel({
  context,
  busy,
  onApply,
}: {
  context: ManifestConsistencyContext
  busy: boolean
  onApply: () => void
}) {
  const missing = context.mismatch_kind === 'missing_detail'
  const blockers = context.current_blockers ?? []
  const impact = context.project_impact
  const buttonLabel = context.recommended_action
    ? actionLabel[context.recommended_action] || '执行确定性同步'
    : '暂无可执行动作'

  return (
    <section className="overflow-hidden rounded-xl border border-warn/35 bg-surface shadow-sm" aria-label="POI 状态一致性诊断">
      <div className="border-b border-border bg-warn-soft/20 px-4 py-3.5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-ink-1">{missing ? 'POI 详情文件缺失' : 'POI 终态不一致'}</div>
            <p className="mt-1 text-xs leading-relaxed text-ink-2">
              Day {context.day ?? '—'} · {context.poi_name || context.detail_file || '未定位 POI'}
              {!missing && '：这是系统状态同步问题，不需要模型复核正文。'}
            </p>
          </div>
          <span className={`rounded px-2.5 py-1.5 text-xs font-semibold ${context.executable ? 'bg-ok-soft text-ok' : 'bg-warn-soft text-[#8a6417]'}`}>
            {context.executable ? '可确定性修复' : missing ? '需定向生成' : '缺少权威依据'}
          </span>
        </div>
      </div>

      <div className="space-y-4 px-4 py-4">
        <div className="grid gap-3 md:grid-cols-[1fr_auto_1fr] md:items-center">
          <StatusCard label="manifest 记录" status={context.manifest?.status ?? '未记录'} detail={context.manifest?.json_pointer ?? ''} />
          <div className="text-center text-lg text-warn" aria-hidden="true">≠</div>
          <StatusCard label="POI 详情文件" status={context.detail?.status ?? 'missing'} detail={context.detail?.file ?? context.detail_file ?? ''} />
        </div>

        <div className={`rounded-lg border px-3.5 py-3 ${context.authoritative_status ? 'border-ok/25 bg-ok-soft/20' : 'border-warn/30 bg-warn-soft/15'}`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs font-semibold text-ink-1">权威终态判断</div>
            <span className={`rounded px-2 py-1 font-mono text-[11px] font-semibold ${context.authoritative_status ? 'bg-surface text-ok' : 'bg-surface text-[#8a6417]'}`}>
              {context.authoritative_status || '无法确定'}
            </span>
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-ink-2">
            {context.authority_reason || context.blocking_reason}
          </p>
          <div className="mt-2 flex flex-wrap gap-2 text-[10px] text-ink-3">
            {context.terminal_result && <span className="rounded bg-surface px-2 py-1">Agent 终态：{context.terminal_result.status}</span>}
            <span className="rounded bg-surface px-2 py-1">当前 Claim：{context.current_claim_count ?? 0}</span>
            <span className={`rounded px-2 py-1 ${(context.current_blocker_count ?? 0) > 0 ? 'bg-danger-soft text-danger' : 'bg-surface'}`}>未通过：{context.current_blocker_count ?? 0}</span>
            {context.validation_receipt && <span className="rounded bg-surface px-2 py-1">live 回执：{context.validation_receipt.status}</span>}
          </div>
        </div>

        {blockers.length > 0 && (
          <details className="rounded-lg border border-danger/25 bg-danger-soft/10 px-3.5 py-3" open>
            <summary className="cursor-pointer text-xs font-semibold text-danger">当前仍未通过的 Claim · {blockers.length}</summary>
            <div className="mt-2 space-y-2">
              {blockers.map((item) => (
                <div key={`${item.claim_id}-${item.scope}`} className="rounded-md border border-border bg-surface px-3 py-2.5 text-xs text-ink-2">
                  <div className="font-mono text-[10px] text-ink-3">{item.claim_id} · {item.target_path || item.scope}</div>
                  <div className="mt-1 font-medium text-ink-1">{item.claim_text}</div>
                  <div className="mt-1 leading-relaxed">{item.reason}</div>
                  {item.evidence[0]?.excerpt && (
                    <details className="mt-1.5 rounded bg-bg-inset px-2 py-1.5">
                      <summary className="cursor-pointer text-[11px] font-semibold">查看对应素材片段</summary>
                      <div className="mt-1 leading-relaxed">{item.evidence[0].excerpt}</div>
                    </details>
                  )}
                </div>
              ))}
            </div>
          </details>
        )}

        {impact && (
          <div className="rounded-lg border border-border bg-bg-inset/50 px-3.5 py-3">
            <div className="text-xs font-semibold text-ink-1">执行后会改什么</div>
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-2">
              {impact.will_refresh.map((item, index) => (
                <span key={item} className="contents">
                  {index > 0 && <span className="text-ink-3">→</span>}
                  <span className="rounded bg-surface px-2 py-1 ring-1 ring-border">{item}</span>
                </span>
              ))}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-ink-3">{impact.note}</p>
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
          <div className="text-xs text-ink-3">
            {context.executable
              ? `只会将${context.recommended_action === 'sync_detail_status' ? ' POI 详情' : ' manifest'}同步为 ${context.authoritative_status}，并重扫关闭本问题。`
              : context.blocking_reason}
          </div>
          <button
            type="button"
            onClick={onApply}
            disabled={!context.executable || busy}
            className="shrink-0 rounded-md bg-ok-soft px-3.5 py-2 text-xs font-semibold text-ok disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy ? '同步并重建中…' : buttonLabel}
          </button>
        </div>
      </div>

      <div className="border-t border-border bg-bg-inset/50 px-4 py-2.5 text-[11px] leading-relaxed text-ink-3">
        判断原则：不猜“哪个文件更新”。只有当前 live 核验、未通过 Claim 或 Agent 终态结果能独立确定权威状态时，才允许机械同步。
      </div>
    </section>
  )
}
