import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { TimelinePage } from './TimelinePage'
import * as hooks from '../api/hooks'
import type { TimelineEvent } from '../api/types'

const EVENTS: TimelineEvent[] = [
  {
    type: 'write',
    timestamp: '2026-07-24T10:00:00Z',
    actor: 'route_agent',
    target: 'route_plan.json',
    summary: '更新路线规划',
  },
  {
    type: 'conflict',
    timestamp: '2026-07-24T10:05:00Z',
    reported_by: 'citation_checker',
    target: 'route_agent',
    reason: '缺失依据',
    status: 'pending',
    retry_count: 0,
  },
  {
    type: 'conflict',
    timestamp: '2026-07-24T10:10:00Z',
    reported_by: 'citation_checker',
    target: 'lifestyle_agent.food',
    reason: '引用核验未通过（共2处）：food.day1「钟水饺」缺失专有名词[\'钟水饺\']：原文片段中未出现该名词；food.day2「番茄煎蛋面」缺失专有名词[\'番茄煎蛋面\']：原文片段中未出现该名词',
    status: 'force_finalized',
    retry_count: 3,
    issues: [
      { field: 'food', day: 1, recommendation: '钟水饺', missing_entities: ['钟水饺'], reason: '原文片段中未出现该名词' },
      { field: 'food', day: 2, recommendation: '番茄煎蛋面', missing_entities: ['番茄煎蛋面'], reason: '原文片段中未出现该名词' },
    ],
  },
  {
    // changelog里的write事件没有结构化issues字段（那是conflict记录独有的），核验失败的
    // "共N处"汇总文案只能靠原始summary字符串渲染——这是截图里实际卡住的路径。
    type: 'write',
    timestamp: '2026-07-24T10:15:00Z',
    actor: 'citation_checker',
    target: 'conflicts.jsonl',
    summary:
      '引用核验未通过（共2处）：outfit.day3「黑独山」缺失专有名词[\'水墨丹青\']：原文片段中未提及穿搭建议；lodging.day4「冷湖」缺失专有名词[\'火星营地\']：原文片段中未提及酒店预约细节',
  },
  {
    // 真实案例：一次核验打回26/27处问题，全部展开会把时间线卡片撑得极长——超过
    // 折叠阈值时应该只显示前几条+展开按钮。
    type: 'write',
    timestamp: '2026-07-24T10:20:00Z',
    actor: 'citation_checker',
    target: 'conflicts.jsonl',
    summary: `引用核验未通过（共7处）：${Array.from({ length: 7 }, (_, i) => `outfit.day${i}「问题${i}」缺失专有名词['实体${i}']：原因${i}`).join('；')}`,
  },
]

function renderPage() {
  vi.spyOn(hooks, 'useProjectTimeline').mockReturnValue({
    data: EVENTS,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useProjectTimeline>)

  return render(
    <MemoryRouter initialEntries={['/projects/chengdu-v1/timeline']}>
      <Routes>
        <Route path="/projects/:projectId/timeline" element={<TimelinePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('TimelinePage', () => {
  it('renders the real "pending" conflict status as its Chinese label, not the raw string', () => {
    renderPage()
    expect(screen.getByText('待处理')).toBeInTheDocument()
    expect(screen.queryByText('pending')).not.toBeInTheDocument()
  })

  it('filters to conflict-only events', () => {
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: '仅冲突/复核' }))
    expect(screen.queryByText('更新路线规划')).not.toBeInTheDocument()
    expect(screen.getByText('缺失依据')).toBeInTheDocument()
  })

  it('renders structured issues as separate lines instead of one run-on paragraph', () => {
    renderPage()
    expect(screen.getByText(/food\.day1/)).toBeInTheDocument()
    expect(screen.getByText(/food\.day2/)).toBeInTheDocument()
    expect(
      screen.queryByText(/引用核验未通过（共2处）：food\.day1「钟水饺」/),
    ).not.toBeInTheDocument()
  })

  it('splits a write-event summary string into separate lines by the "；" separator', () => {
    renderPage()
    expect(screen.getByText(/outfit\.day3「黑独山」/)).toBeInTheDocument()
    expect(screen.getByText(/lodging\.day4「冷湖」/)).toBeInTheDocument()
    expect(
      screen.queryByText(/引用核验未通过（共2处）：outfit\.day3「黑独山」缺失专有名词\['水墨丹青'\]：原文片段中未提及穿搭建议；lodging\.day4/),
    ).not.toBeInTheDocument()
  })

  it('collapses a long issue list behind an expand button instead of showing everything at once', () => {
    renderPage()
    // 7条问题，超过折叠阈值——只有前几条应该直接可见
    expect(screen.getByText(/问题0/)).toBeInTheDocument()
    expect(screen.queryByText(/问题6/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('展开剩余 2 条 ▼'))
    expect(screen.getByText(/问题6/)).toBeInTheDocument()

    fireEvent.click(screen.getByText('收起 ▲'))
    expect(screen.queryByText(/问题6/)).not.toBeInTheDocument()
  })
})
