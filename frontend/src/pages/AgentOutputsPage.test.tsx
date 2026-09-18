import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AgentOutputsPage } from './AgentOutputsPage'
import * as hooks from '../api/hooks'
import type { ProjectOutputs } from '../api/types'

function renderPage(outputs: ProjectOutputs | null) {
  vi.spyOn(hooks, 'useProjectOutputs').mockReturnValue({
    data: outputs,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useProjectOutputs>)

  return render(
    <MemoryRouter initialEntries={['/projects/chengdu-v1/outputs']}>
      <Routes>
        <Route path="/projects/:projectId/outputs" element={<AgentOutputsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

const FULL_OUTPUTS: ProjectOutputs = {
  route_plan: {
    version: 2,
    amap_verified: false,
    days: [
      {
        day: 1,
        city: '成都',
        area: '武侯区',
        pois: [{ name: '武侯祠博物馆', brief: '三国文化', needs_detail: true }],
        notes: '早点去',
        sources: [{ platform: 'bilibili', doc_id: '成都-000' }],
      },
    ],
  },
  lifestyle: {
    version: 1,
    food: { items: [{ day: 1, recommendation: '推荐吃火锅', claimed_entities: ['某某火锅'] }] },
    outfit: { items: [{ day: 1, recommendation: '穿轻薄衣物' }] },
    lodging: { items: [] },
    play: { items: [] },
  },
  pitfall: {
    version: 1,
    food: { items: [{ field: 'food', day: 1, target_recommendation: '某某火锅', warning: '排队久' }] },
    outfit: { items: [] },
    lodging: { items: [] },
    play: { items: [] },
  },
  evidence_checks: {
    version: 1,
    route: [
      {
        target_type: 'route_day',
        target_key: 'route:day:1',
        target_text: '成都 武侯区：早点去 POIs: 武侯祠博物馆',
        scope: 'entity_identity',
        result: 'pass',
        method: 'literal_match',
        reason: '引用原文直接出现全部声明实体。',
        checked_chunk_ids: ['成都-000-c1'],
        evidence: [
          {
            chunk_id: '成都-000-c1',
            role: 'cited',
            platform: 'bilibili',
            doc_id: '成都-000',
            excerpt: '武侯祠博物馆是成都重要的三国文化景点。',
          },
        ],
      },
      {
        target_type: 'route_day',
        target_key: 'route:day:1',
        target_text: '成都 武侯区：早点去 POIs: 武侯祠博物馆',
        scope: 'substantive_claim',
        result: 'not_checked',
        method: 'out_of_scope',
        reason: '路线引用核验当前只核对 POI 地点名称；没有核验路线备注和 POI 简介中的具体事实。',
      },
    ],
    lifestyle: [
      {
        target_type: 'lifestyle_item',
        target_key: 'lifestyle:food:day:1:item:0',
        target_text: '推荐吃火锅',
        scope: 'entity_identity',
        result: 'pass',
        method: 'literal_match',
        reason: '引用原文直接出现全部声明实体。',
        checked_chunk_ids: ['food-c1'],
        evidence: [
          {
            chunk_id: 'food-c1',
            role: 'cited',
            platform: 'bilibili',
            doc_id: 'food-doc',
            excerpt: '某某火锅是当地常见的晚餐选择。',
          },
        ],
      },
    ],
    pitfall: [],
    poi_detail: [],
  },
  final_guide: '# Day 1\n武侯祠一日游',
}

describe('AgentOutputsPage', () => {
  it('renders the route tab by default with real-shaped data', () => {
    renderPage(FULL_OUTPUTS)
    expect(screen.getAllByText(/武侯祠博物馆/).length).toBeGreaterThan(0)
  })

  it('switches to lifestyle/pitfall/integration tabs without crashing', () => {
    renderPage(FULL_OUTPUTS)

    fireEvent.click(screen.getByRole('button', { name: '衣食住行' }))
    expect(screen.getAllByText(/推荐吃火锅/).length).toBeGreaterThan(0)
    expect(screen.queryByText('美食事实与证据')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '避雷' }))
    expect(screen.getAllByText(/排队久/).length).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole('button', { name: '整合产出' }))
    expect(screen.getByText(/武侯祠一日游/)).toBeInTheDocument()
  })

  it('shows source excerpts and real checker outcomes instead of inferring verification', () => {
    renderPage(FULL_OUTPUTS)
    expect(screen.getByText(/武侯祠博物馆是成都重要的三国文化景点/)).toBeInTheDocument()
    expect(screen.getByText('地点/实体对应')).toBeInTheDocument()
    expect(screen.getByText('具体说法是否受支持')).toBeInTheDocument()
    expect(screen.getByText('天级核验详情（供人工查看）')).toBeInTheDocument()
    expect(screen.getByText('天级汇总来源（兼容字段）')).toBeInTheDocument()
    expect(screen.getAllByText('核验问题').length).toBeGreaterThan(0)
    expect(screen.getAllByText('判断边界').length).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole('button', { name: '衣食住行' }))
    expect(screen.getByText(/某某火锅是当地常见的晚餐选择/)).toBeInTheDocument()
    expect(screen.queryByText('已核验')).not.toBeInTheDocument()
    expect(screen.getByText(/已执行 字面匹配/)).toBeInTheDocument()
  })

  it('does not present a partially covered route as fully checked', () => {
    renderPage(FULL_OUTPUTS)
    expect(screen.getByText('部分未核验')).toBeInTheDocument()
    expect(screen.getByText(/这份旧数据只有天级来源/)).toBeInTheDocument()
  })

  it('renders each route fact with its own evidence and checker lineage', () => {
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      route_plan: {
        ...FULL_OUTPUTS.route_plan!,
        days: [{
          ...FULL_OUTPUTS.route_plan!.days[0],
          claims: [
            {
              claim_id: 'route-claim-ticket',
              target_path: 'notes',
              subject: '武侯祠博物馆',
              claim_text: '武侯祠博物馆成人票为50元。',
              claim_type: 'ticket',
              support_mode: 'direct',
              claimed_entities: ['武侯祠博物馆'],
              evidence_refs: [{ platform: 'official', doc_id: 'ticket-doc', chunk_id: 'ticket-c1' }],
              evidence_snapshots: [{
                source: { platform: 'official', doc_id: 'ticket-doc', chunk_id: 'ticket-c1' },
                excerpt: '武侯祠博物馆全价门票50元/人。',
                observed_at: '2026-08-28T08:00:00Z',
                source_kind: 'project_web',
              }],
              provenance_mode: 'live',
            },
            {
              claim_id: 'route-claim-budget',
              target_path: 'notes',
              claim_text: '两人门票预算为100元。',
              claim_type: 'budget',
              support_mode: 'derived',
              derived_from: ['route-claim-ticket'],
              provenance_mode: 'live',
            },
          ],
        }],
      },
      evidence_checks: {
        ...FULL_OUTPUTS.evidence_checks!,
        route: [
          ...FULL_OUTPUTS.evidence_checks!.route,
          {
            target_type: 'route_claim',
            target_key: 'route:day:1:claim:route-claim-ticket',
            target_text: '武侯祠博物馆成人票为50元。',
            scope: 'substantive_claim',
            result: 'pass',
            method: 'literal_match',
            reason: '引用原文直接支持票价事实。',
            claim_id: 'route-claim-ticket',
            claim_text: '武侯祠博物馆成人票为50元。',
            claim_type: 'ticket',
            support_mode: 'direct',
            target_path: 'notes',
            checked_chunk_ids: ['ticket-c1'],
            evidence: [{
              chunk_id: 'ticket-c1',
              role: 'cited',
              platform: 'official',
              doc_id: 'ticket-doc',
              excerpt: '武侯祠博物馆全价门票50元/人。',
            }],
          },
          {
            target_type: 'route_claim',
            target_key: 'route:day:1:claim:route-claim-budget',
            target_text: '两人门票预算为100元。',
            scope: 'substantive_claim',
            result: 'pass',
            method: 'derived_support',
            reason: '该预算可由已列出的票价事实计算得到。',
            claim_id: 'route-claim-budget',
            claim_text: '两人门票预算为100元。',
            claim_type: 'budget',
            support_mode: 'derived',
            target_path: 'notes',
            checked_chunk_ids: ['route-claim-ticket'],
          },
        ],
      },
    }

    renderPage(outputs)
    expect(screen.getByText('关键事实与证据')).toBeInTheDocument()
    expect(screen.getByText('2 条 · 通过 2 · 异常 0 · 待判断 0')).toBeInTheDocument()

    fireEvent.click(screen.getByText('武侯祠博物馆成人票为50元。'))
    expect(screen.getAllByText(/武侯祠博物馆全价门票50元\/人/).length).toBeGreaterThan(0)
    expect(screen.getByText(/引用原文直接支持票价事实/)).toBeInTheDocument()

    fireEvent.click(screen.getByText('两人门票预算为100元。'))
    expect(screen.getAllByText(/route-claim-ticket/).length).toBeGreaterThan(0)
    expect(screen.getByText(/该预算可由已列出的票价事实计算得到/)).toBeInTheDocument()
  })

  it('ignores a replaced route claim failure when summarizing the current day', () => {
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      route_plan: {
        ...FULL_OUTPUTS.route_plan!,
        days: [{
          ...FULL_OUTPUTS.route_plan!.days[0],
          claims: [{
            claim_id: 'route-claim-current',
            target_path: 'notes',
            claim_text: '雷洞坪运营情况需出发前确认。',
            claim_type: 'direct',
            support_mode: 'direct',
            provenance_mode: 'live',
          }],
        }],
      },
      route_feasibility: {
        overall_status: 'warning',
        checks: [{
          check_id: 'route-warning-current',
          day: 1,
          target: '雷洞坪',
          target_type: 'poi',
          category: 'poi_operational_status',
          status: 'warning',
          summary: '临时乘车点和末班车时间需出发前确认。',
        }],
      },
      evidence_checks: {
        ...FULL_OUTPUTS.evidence_checks!,
        route: [{
          target_type: 'route_claim',
          target_key: 'route:day:1:claim:route-claim-old',
          target_text: '旧观光车班车声明',
          scope: 'entity_identity',
          result: 'fail',
          method: 'alias_resolution',
          reason: '旧声明的实体名称不一致。',
          claim_id: 'route-claim-old',
        }],
      },
    }

    renderPage(outputs)

    expect(screen.getByText('存在无法判断')).toBeInTheDocument()
    expect(screen.queryByText('校验有异常')).not.toBeInTheDocument()
    expect(screen.getByText('1 条 · 通过 0 · 异常 0 · 待判断 1')).toBeInTheDocument()
  })

  it('separates evidence facts from planning rationale and uses entity checks for identity claims', () => {
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      route_plan: {
        ...FULL_OUTPUTS.route_plan!,
        days: [{
          ...FULL_OUTPUTS.route_plan!.days[0],
          pois: [{ name: '文殊院', brief: '市区人文点', needs_detail: false }],
          claims: [
            {
              claim_id: 'route-claim-identity',
              target_path: 'pois.0.name',
              claim_text: '文殊院',
              claim_type: 'poi_identity',
              support_mode: 'direct',
              evidence_refs: [{ platform: 'xiaohongshu', doc_id: '成都-032', chunk_id: '成都-032-c000' }],
              evidence_snapshots: [{
                source: { platform: 'xiaohongshu', doc_id: '成都-032', chunk_id: '成都-032-c000' },
                excerpt: '素材中出现文殊院。',
                observed_at: '2026-09-08T10:00:00Z',
                source_kind: 'material',
              }],
              provenance_mode: 'live',
            },
            {
              claim_id: 'route-claim-selection',
              target_path: 'pois.0.name',
              claim_text: '将文殊院作为市区人文线起点。',
              claim_type: 'poi_selection',
              support_mode: 'estimate',
              provenance_mode: 'live',
            },
            {
              claim_id: 'route-claim-unverified',
              target_path: 'pois.0.brief',
              claim_text: '文殊院附近有特定小吃。',
              claim_type: 'fact',
              support_mode: 'unverified',
              provenance_mode: 'live',
            },
          ],
        }],
      },
      evidence_checks: {
        ...FULL_OUTPUTS.evidence_checks!,
        route: [
          {
            target_type: 'route_claim',
            target_key: 'route:day:1:claim:route-claim-identity',
            target_text: '文殊院',
            scope: 'source_availability',
            result: 'pass',
            method: 'chunk_lookup',
            reason: '引用 chunk 可读。',
            claim_id: 'route-claim-identity',
          },
          {
            target_type: 'route_claim',
            target_key: 'route:day:1:claim:route-claim-identity',
            target_text: '文殊院',
            scope: 'entity_identity',
            result: 'pass',
            method: 'literal_match',
            reason: '原文直接出现该地点。',
            claim_id: 'route-claim-identity',
          },
          {
            target_type: 'route_claim',
            target_key: 'route:day:1:claim:route-claim-identity',
            target_text: '文殊院',
            scope: 'substantive_claim',
            result: 'not_checked',
            method: 'out_of_scope',
            reason: '该条目只核对地点身份。',
            claim_id: 'route-claim-identity',
          },
        ],
      },
    }

    renderPage(outputs)
    expect(screen.getByText('1 条 · 通过 1 · 异常 0 · 待判断 0')).toBeInTheDocument()
    const identitySummary = screen.getAllByText('文殊院')
      .map((node) => node.closest('summary'))
      .find((node): node is HTMLElement => node !== null)
    expect(identitySummary).toHaveTextContent('通过')
    expect(identitySummary).toHaveTextContent('声明：原文支持')
    expect(identitySummary).not.toHaveTextContent('未检查')
    expect(screen.getByText('不适用')).toBeInTheDocument()
    expect(screen.getByText('待补证事实')).toBeInTheDocument()
    expect(screen.getByText('规划依据与用户要求')).toBeInTheDocument()
    expect(screen.getByText('1 条·无需外部核验')).toBeInTheDocument()
  })

  it('uses a later successful map resolution instead of leaving a POI identity in missing evidence', () => {
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      route_plan: {
        ...FULL_OUTPUTS.route_plan!,
        days: [{
          ...FULL_OUTPUTS.route_plan!.days[0],
          pois: [{ name: '海口钟楼', brief: '文化停留点', needs_detail: false }],
          claims: [{
            claim_id: 'route-claim-clocktower',
            target_path: 'pois.0.name',
            subject: '海口钟楼',
            claim_text: '海口钟楼',
            claim_type: 'poi_identity',
            support_mode: 'unverified',
            claimed_entities: ['海口钟楼'],
            provenance_mode: 'live',
          }],
        }],
      },
      route_map_validation: {
        status: 'passed',
        days: [{
          day: 1,
          entities: [{
            original_name: '海口钟楼',
            status: 'resolved',
            confidence: 0.9,
            selected: {
              provider_id: 'B0FFFEXRIQ',
              provider_name: '海口钟楼',
              address: '长堤路20号',
            },
          }],
        }],
      },
      evidence_checks: { ...FULL_OUTPUTS.evidence_checks!, route: [] },
    }

    renderPage(outputs)
    expect(screen.getByText('1 条 · 通过 1 · 异常 0 · 待判断 0')).toBeInTheDocument()
    expect(screen.getByText('高德实体已确认')).toBeInTheDocument()
    expect(screen.getByText(/B0FFFEXRIQ/)).toBeInTheDocument()
    expect(screen.queryByText('待补证事实')).not.toBeInTheDocument()
  })

  it('reuses claim evidence rendering for lifestyle and POI details', () => {
    const lifestyleClaim = {
      claim_id: 'lifestyle-claim-food',
      target_path: 'recommendation',
      claim_text: '某某火锅适合晚餐。',
      claim_type: 'food',
      support_mode: 'direct' as const,
      claimed_entities: ['某某火锅'],
      evidence_refs: [{ platform: 'bilibili', doc_id: 'food-doc', chunk_id: 'food-c1' }],
      evidence_snapshots: [{
        source: { platform: 'bilibili', doc_id: 'food-doc', chunk_id: 'food-c1' },
        excerpt: '某某火锅晚市营业，适合晚餐。',
        observed_at: '2026-08-28T08:00:00Z',
        source_kind: 'material',
      }],
      provenance_mode: 'live' as const,
    }
    const poiClaim = {
      claim_id: 'poi-claim-gate',
      target_path: 'highlight',
      claim_text: '从正门检票进入。',
      claim_type: 'highlight',
      support_mode: 'direct' as const,
      claimed_entities: ['正门'],
      evidence_refs: [{ platform: 'bilibili', doc_id: '成都-000', chunk_id: '成都-000-c1' }],
      evidence_snapshots: [{
        source: { platform: 'bilibili', doc_id: '成都-000', chunk_id: '成都-000-c1' },
        excerpt: '经典线从正门检票进入。',
        observed_at: '2026-08-28T08:00:00Z',
        source_kind: 'material',
      }],
      provenance_mode: 'live' as const,
    }
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      lifestyle: {
        ...FULL_OUTPUTS.lifestyle!,
        food: { items: [{ ...FULL_OUTPUTS.lifestyle!.food.items[0], claims: [lifestyleClaim] }] },
      },
      poi_details: [{
        poi_name: '武侯祠博物馆',
        day: 1,
        routes: [{
          route_name: '经典线',
          suitable_for: '首次到访',
          recommended: true,
          recommend_reason: '途经点顺路',
          route_segments: [{
            order: 1,
            point_name: '正门',
            highlight: '从正门检票进入',
            suggested_duration: '',
            image_prompt: '',
            sources: [],
            claims: [poiClaim],
            uploaded_photo_path: '',
          }],
        }],
        needs_detail_reason: '大型景点',
        citation_status: 'passed',
        auto_collected: [],
        retry_history: [],
        uploaded_overview_photo_path: '',
        created_at: '',
        updated_at: '',
      }],
      evidence_checks: {
        ...FULL_OUTPUTS.evidence_checks!,
        lifestyle: [{
          target_type: 'lifestyle_claim',
          target_key: 'lifestyle:food:day:1:item:0:claim:lifestyle-claim-food',
          target_text: '某某火锅适合晚餐。',
          scope: 'substantive_claim',
          result: 'pass',
          method: 'literal_match',
          reason: '原文直接支持。',
          claim_id: 'lifestyle-claim-food',
          claim_text: '某某火锅适合晚餐。',
          support_mode: 'direct',
          target_path: 'recommendation',
        }],
        poi_detail: [{
          target_type: 'poi_detail_claim',
          target_key: 'poi_detail:武侯祠博物馆:route:经典线:order:1:claim:poi-claim-gate',
          target_text: '从正门检票进入。',
          scope: 'substantive_claim',
          result: 'pass',
          method: 'literal_match',
          reason: '原文直接支持。',
          claim_id: 'poi-claim-gate',
          claim_text: '从正门检票进入。',
          support_mode: 'direct',
          target_path: 'highlight',
        }],
      },
    }

    renderPage(outputs)
    fireEvent.click(screen.getByRole('button', { name: '衣食住行' }))
    expect(screen.getByText('美食事实与证据')).toBeInTheDocument()
    expect(screen.getByText('某某火锅适合晚餐。')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'POI详情' }))
    expect(screen.getByText('途经点事实与证据')).toBeInTheDocument()
    expect(screen.getByText('从正门检票进入。')).toBeInTheDocument()
  })

  it('shows declared source usages and navigates back to the owning output', () => {
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      final_guide: '# Day 1\n武侯祠一日游\n\n## 📚 参考来源（程序生成，非模型自行撰写）\n\n| chunk |\n|---|\n| legacy-row |',
      final_guide_meta: {
        references: [
          {
            platform: 'bilibili',
            doc_id: '成都-000',
            chunk_id: '成都-000-c1',
            excerpt: '武侯祠博物馆是成都重要的三国文化景点。',
            cited_in: ['route:day:1', 'lifestyle:food:day:1:item:0'],
          },
        ],
      },
    }

    renderPage(outputs)
    fireEvent.click(screen.getByRole('button', { name: '整合产出' }))

    expect(screen.getByText('引用来源总表')).toBeInTheDocument()
    expect(screen.getByText('1条来源 · 2处声明使用')).toBeInTheDocument()
    expect(screen.queryByText('legacy-row')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Day1路线' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Day1美食' }))
    expect(screen.getAllByText(/推荐吃火锅/).length).toBeGreaterThan(0)
  })

  it('shows final-guide block mappings and navigates to the upstream claim owner', () => {
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      route_plan: {
        ...FULL_OUTPUTS.route_plan!,
        days: [{
          ...FULL_OUTPUTS.route_plan!.days[0],
          claims: [{
            claim_id: 'route-claim-wuhou',
            target_path: 'notes',
            claim_text: '上午游览武侯祠。',
            claim_type: 'schedule',
            support_mode: 'user_constraint',
          }],
        }],
      },
      final_guide_blocks: {
        schema_version: 'final-guide-blocks-v1',
        rendered_body_sha256: 'hash',
        blocks: [{
          block_id: 'guide-block-day1',
          section: 'Day1路线',
          markdown: '## Day 1\n\n上午游览武侯祠。',
          mapping_mode: 'upstream',
          upstream_claim_ids: ['route-claim-wuhou'],
          claim_snapshots: [{
            claim_id: 'route-claim-wuhou',
            source_path: 'route_plan.json:/days/0/claims/0',
            usage_key: 'route:day:1',
            claim_text: '上午游览武侯祠。',
            claim_type: 'schedule',
            support_mode: 'user_constraint',
          }],
        }],
      },
    }

    renderPage(outputs)
    fireEvent.click(screen.getByRole('button', { name: '整合产出' }))
    expect(screen.getByText('正文事实映射')).toBeInTheDocument()
    expect(screen.getByText('1个正文块 · 1条claim关联')).toBeInTheDocument()
    expect(screen.getByText('上游事实')).toBeInTheDocument()
    expect(screen.getByText('route_plan.json:/days/0/claims/0')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Day1路线' }))
    expect(screen.getAllByText(/上午游览武侯祠/).length).toBeGreaterThan(0)
  })

  it('renders POI detail segments as source-usage destinations', () => {
    const outputs: ProjectOutputs = {
      ...FULL_OUTPUTS,
      poi_details: [
        {
          poi_name: '武侯祠博物馆',
          day: 1,
          routes: [
            {
              route_name: '经典线',
              suitable_for: '首次到访',
              recommended: true,
              recommend_reason: '顺路',
              route_segments: [
                {
                  order: 1,
                  point_name: '正门',
                  highlight: '从正门检票进入',
                  suggested_duration: '10分钟',
                  image_prompt: '',
                  sources: [{ platform: 'bilibili', doc_id: '成都-000', chunk_id: '成都-000-c1' }],
                  uploaded_photo_path: '',
                },
              ],
            },
          ],
          needs_detail_reason: '大型景点',
          citation_status: 'passed',
          auto_collected: [],
          retry_history: [],
          uploaded_overview_photo_path: '',
          created_at: '',
          updated_at: '',
        },
      ],
    }

    renderPage(outputs)
    fireEvent.click(screen.getByRole('button', { name: 'POI详情' }))
    expect(screen.getByText('经典线')).toBeInTheDocument()
    expect(screen.getByText('正门')).toBeInTheDocument()
    expect(screen.getAllByText(/从正门检票进入/).length).toBeGreaterThan(0)
  })

  it('renders sensible empty states when every field is null', () => {
    renderPage({ route_plan: null, lifestyle: null, pitfall: null, final_guide: null })
    expect(screen.getByText('还没有生成路线规划。')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '衣食住行' }))
    expect(screen.getByText('还没有生成这部分内容。')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '整合产出' }))
    expect(screen.getByText('还没有生成最终攻略。')).toBeInTheDocument()
  })
})
