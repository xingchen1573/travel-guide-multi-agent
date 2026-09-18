import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ManifestConsistencyContext } from '../api/types'
import { ManifestConsistencyPanel } from './ManifestConsistencyPanel'

const context: ManifestConsistencyContext = {
  schema_version: 'manifest-consistency-v1',
  mismatch_kind: 'status',
  manifest_index: 1,
  day: 2,
  poi_name: '金顶',
  detail_file: 'day2_金顶.json',
  manifest: { status: 'force_finalized', updated_at: '2026-09-02T01:32:10Z', json_pointer: '/generated/1' },
  detail: { status: 'passed', updated_at: '2026-09-02T01:27:56Z', file: 'outputs/poi_details/day2_金顶.json' },
  terminal_result: { status: 'force_finalized', source: 'agents/codex_skill/poi_results.json', observed_at: '2026-09-02T01:32:10Z' },
  validation_receipt: null,
  current_claim_count: 12,
  current_check_count: 31,
  current_blocker_count: 1,
  current_blockers: [{
    claim_id: 'poi-claim-current',
    claim_text: '华藏寺适合纳入礼佛主线。',
    target_path: 'highlight',
    scope: 'substantive_claim',
    result: 'fail',
    reason: '素材只支持地点身份，不支持礼佛主线的定性。',
    checked_at: '2026-09-02T01:29:12Z',
    evidence: [{ ref: 'source-1', excerpt: '华藏寺是金顶的核心看点之一。' }],
  }],
  authoritative_status: 'force_finalized',
  authority_basis: 'current_failed_checks',
  authority_reason: '当前 Claim 仍有 1 个未通过，不能把该 POI 标成 passed。',
  confidence: 'high',
  recommended_action: 'sync_detail_status',
  action_target: 'outputs/poi_details/day2_金顶.json',
  executable: true,
  blocking_reason: '',
  observed_fingerprint: 'fingerprint',
  project_impact: {
    needs_rerun: false,
    generation_blocked: false,
    final_guide_mention_count: 2,
    will_refresh: ['manifest/detail 状态对', '最终攻略 POI 附录', '引用附录', '静态网站'],
    note: '只修复系统事实，不改写正文。',
  },
}

describe('ManifestConsistencyPanel', () => {
  it('展示两侧状态、权威依据和当前阻断 Claim', () => {
    render(<ManifestConsistencyPanel context={context} busy={false} onApply={() => undefined} />)

    expect(screen.getByText('POI 终态不一致')).toBeInTheDocument()
    expect(screen.getAllByText('force_finalized').length).toBeGreaterThan(0)
    expect(screen.getByText('passed')).toBeInTheDocument()
    expect(screen.getByText(/当前 Claim 仍有 1 个未通过/)).toBeInTheDocument()
    expect(screen.getByText('华藏寺适合纳入礼佛主线。')).toBeInTheDocument()
    expect(screen.queryByText('模型复核')).not.toBeInTheDocument()
  })

  it('只通过确定性按钮触发同步', () => {
    const onApply = vi.fn()
    render(<ManifestConsistencyPanel context={context} busy={false} onApply={onApply} />)

    fireEvent.click(screen.getByRole('button', { name: '同步 POI 详情状态' }))
    expect(onApply).toHaveBeenCalledTimes(1)
  })

  it('缺少权威依据时禁止机械执行', () => {
    render(<ManifestConsistencyPanel context={{ ...context, authoritative_status: null, recommended_action: '', executable: false, blocking_reason: '无法确定权威终态。' }} busy={false} onApply={() => undefined} />)

    expect(screen.getByText('无法确定')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '暂无可执行动作' })).toBeDisabled()
  })
})
