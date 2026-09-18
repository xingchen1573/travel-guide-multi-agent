import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { ProjectTabs } from './ProjectTabs'

function LocationProbe() {
  const location = useLocation()
  return (
    <div data-testid="location">
      {location.pathname}|{JSON.stringify(location.state)}
    </div>
  )
}

function renderTabs(initialEntry: string | { pathname: string; state?: unknown }) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="*" element={<><ProjectTabs projectId="chengdu-v1" /><LocationProbe /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ProjectTabs navigation hierarchy', () => {
  it('falls back to the project list when the overview was opened directly', () => {
    renderTabs('/projects/chengdu-v1')

    expect(screen.getByRole('link', { name: '返回Project 列表' })).toHaveAttribute('href', '/')
  })

  it('returns through the project overview and then the recorded source page', () => {
    renderTabs({
      pathname: '/projects/chengdu-v1',
      state: { projectParent: { to: '/feishu', label: '飞书运维' } },
    })

    expect(screen.getByRole('link', { name: '返回飞书运维' })).toHaveAttribute('href', '/feishu')

    fireEvent.click(screen.getByRole('link', { name: '质量中心' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/projects/chengdu-v1/review')
    expect(screen.getByRole('link', { name: '返回项目概览' })).toHaveAttribute('href', '/projects/chengdu-v1')

    fireEvent.click(screen.getByRole('link', { name: '返回项目概览' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/projects/chengdu-v1|')
    expect(screen.getByTestId('location')).toHaveTextContent('"to":"/feishu"')
    expect(screen.getByRole('link', { name: '返回飞书运维' })).toHaveAttribute('href', '/feishu')
  })
})
