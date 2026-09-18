import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { MissingEntityReviewContext } from '../api/types'
import { MissingEntityReviewPanel } from './MissingEntityReviewPanel'

const context: MissingEntityReviewContext = {
  schema_version: 'missing-entity-review-v1',
  entity_count: 2,
  occurrence_count: 3,
  raw_missing_entity_count: 3,
  stale_evidence_state: true,
  stale_entity_count: 2,
  verification_state: 'stale_failure',
  stale_reason: '旧 Claim ID 已失配。',
  project_impact: {
    finding_status: 'open',
    project_phase: '完成',
    citation_status: 'force_finalized',
    review_required: true,
    review_queue_counted: true,
    generation_blocked: false,
    affects_project_state: true,
    final_guide_affected: true,
    final_guide_occurrence_count: 2,
    final_guide_entity_mention_count: 2,
    affected_files: ['route_plan.json', 'outputs/poi_details/day2_金顶.json', 'final_guide.md'],
    note: '保留项目级待处理。',
  },
  entities: [
    {
      entity: '十方普贤圣像',
      occurrence_count: 2,
      occurrences: [
        {
          json_pointer: '/days/1/pois/0/brief',
          field_label: '简介',
          subject: '金顶',
          current_value: '十方普贤圣像和金顶云海',
          excerpt: '十方普贤圣像和金顶云海',
        },
        {
          json_pointer: '/days/1/pois/0/needs_detail_reason',
          field_label: '深度玩法判断',
          subject: '金顶',
          current_value: '含十方普贤圣像等景观',
          excerpt: '含十方普贤圣像等景观',
        },
      ],
      current_sources: [{
        ref: 'project-evidence-old',
        platform: 'project_evidence',
        author: '',
        source_url: '',
        excerpts: ['雷洞坪停车场改造公告'],
        origin: 'embedded_snapshot',
        attachment_scope: 'review_scope',
        relation: 'no_exact_mention',
      }],
      candidate_evidence: [{
        ref: '峨眉山-004-c001',
        text: '华藏寺、十方普贤圣像、峨眉金顶齐聚于此。',
        retrieval_method: 'exact_entity+bm25',
        retrieval_score: 3.2,
        already_attached: false,
      }],
      downstream_evidence: [{
        file: 'outputs/poi_details/day2_金顶.json',
        poi_name: '金顶',
        day: 2,
        claim_id: 'poi-claim-current',
        claim_text: '十方普贤圣像是峨眉山的标志性景观。',
        json_pointer: '/routes/0/route_segments/0/highlight',
        current_value: '欣赏十方普贤圣像',
        validation_status: 'passed',
        passed_scope_count: 3,
        failed_scope_count: 0,
        checked_at: '2026-09-01T16:08:29Z',
        evidence_excerpts: [{
          ref: '峨眉山-004-c006',
          text: '宏伟的十方普贤圣像庄严肃穆。',
          mentions_entity: true,
          origin: 'embedded_snapshot',
        }],
      }],
      final_guide_occurrences: [{
        file: 'final_guide.md',
        line: 40,
        excerpt: '串联十方普贤圣像、华藏寺。',
        match_mode: 'text_match',
        claim_linked: false,
        block_id: null,
        section: '',
        upstream_claim_ids: [],
      }],
      stale_evidence_state: true,
      verification_state: 'stale_failure',
      content_updated_at: '2026-09-01T13:07:00Z',
      latest_checked_at: '2026-09-01T12:59:00Z',
      historical_failures: [{
        claim_id: 'route-claim-old',
        checked_claim_text: '旧的十方普贤圣像表述',
        target_path: 'pois.0.brief',
        json_pointer: '/days/1/pois/0/brief',
        scope: 'entity_identity',
        reason: '旧引用未提到该实体。',
        checked_at: '2026-09-01T12:59:00Z',
        checked_before_content_update: true,
        current_claim_present: false,
        match_status: 'same_field_stale_claim',
      }],
      resolution_state: 'verified_downstream',
      recommendation: '回填证据后仅重核当前对象。',
    },
    {
      entity: '华藏寺',
      occurrence_count: 1,
      occurrences: [],
      current_sources: [],
      candidate_evidence: [],
      downstream_evidence: [],
      final_guide_occurrences: [],
      stale_evidence_state: true,
      verification_state: 'stale_failure',
      content_updated_at: '',
      latest_checked_at: '',
      historical_failures: [],
      resolution_state: 'needs_evidence',
      recommendation: '需补充证据。',
    },
  ],
}

describe('MissingEntityReviewPanel', () => {
  it('展示去重实体、字段定位和过期历史状态', () => {
    render(<MemoryRouter><MissingEntityReviewPanel context={context} projectId="emeishan-v1" /></MemoryRouter>)

    expect(screen.getByText('2 个唯一实体 · 3 个字段位置')).toBeInTheDocument()
    expect(screen.getByText(/历史判断已过期/)).toBeInTheDocument()
    expect(screen.getByText('/days/1/pois/0/brief')).toBeInTheDocument()
    expect(screen.getByText(/原文未直接提及/)).toBeInTheDocument()
  })

  it('将下游通过、素材命中和最终攻略传播分开表达', () => {
    render(<MemoryRouter><MissingEntityReviewPanel context={context} projectId="emeishan-v1" /></MemoryRouter>)

    expect(screen.getByText('下游 Claim 已核验')).toBeInTheDocument()
    expect(screen.getByText(/不会自动让 Route 通过/)).toBeInTheDocument()
    expect(screen.getByText('素材库精确命中')).toBeInTheDocument()
    expect(screen.getByText('最终攻略：已传播（文本命中）')).toBeInTheDocument()
  })
})
