import { fireEvent, render, screen } from '@testing-library/react'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { Layout } from './Layout'

function FirstPage() {
  return <Link to="/second">进入下一层</Link>
}

function ScrollBoundaryPage() {
  return <div data-testid="scroller" className="scroll-boundary-y overflow-y-auto"><div>长内容</div></div>
}

describe('Layout route scrolling', () => {
  it('opens and closes the mobile navigation drawer', () => {
    render(
      <MemoryRouter initialEntries={['/first']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="first" element={<FirstPage />} />
            <Route path="second" element={<p>下一层页面</p>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('button', { name: '打开导航' }))
    expect(screen.getByRole('dialog', { name: '工作台导航' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '关闭导航' }))
    expect(screen.queryByRole('dialog', { name: '工作台导航' })).not.toBeInTheDocument()
  })

  it('resets the viewport when navigating to another page', () => {
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)

    render(
      <MemoryRouter initialEntries={['/first']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="first" element={<FirstPage />} />
            <Route path="second" element={<p>下一层页面</p>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

    scrollTo.mockClear()
    fireEvent.click(screen.getByRole('link', { name: '进入下一层' }))

    expect(screen.getByText('下一层页面')).toBeInTheDocument()
    expect(scrollTo).toHaveBeenCalledWith({ top: 0, left: 0, behavior: 'auto' })
  })

  it('consumes wheel momentum that would cross a nested scroll boundary', () => {
    render(
      <MemoryRouter initialEntries={['/scroll']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="scroll" element={<ScrollBoundaryPage />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

    const scroller = screen.getByTestId('scroller')
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 100 })
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 300 })
    scroller.scrollTop = 180

    const crossingWheel = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 50 })
    scroller.dispatchEvent(crossingWheel)

    expect(crossingWheel.defaultPrevented).toBe(true)
    expect(scroller.scrollTop).toBe(200)
  })

  it('keeps normal wheel scrolling native while still inside the boundary', () => {
    render(
      <MemoryRouter initialEntries={['/scroll']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="scroll" element={<ScrollBoundaryPage />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

    const scroller = screen.getByTestId('scroller')
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 100 })
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 300 })
    scroller.scrollTop = 80

    const regularWheel = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 30 })
    scroller.dispatchEvent(regularWheel)

    expect(regularWheel.defaultPrevented).toBe(false)
  })
})
