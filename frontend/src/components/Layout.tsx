import { useEffect, useLayoutEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Icon } from './Icon'
import { Sidebar } from './Sidebar'

export function Layout() {
  const { pathname } = useLocation()
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  useEffect(() => {
    function containNestedWheel(event: WheelEvent) {
      if (!(event.target instanceof Element) || event.deltaY === 0) return
      const scroller = event.target.closest<HTMLElement>('.scroll-boundary-y')
      if (!scroller) return

      const maxScrollTop = scroller.scrollHeight - scroller.clientHeight
      if (maxScrollTop <= 1) return

      const multiplier = event.deltaMode === WheelEvent.DOM_DELTA_LINE
        ? 16
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
          ? scroller.clientHeight
          : 1
      const deltaY = event.deltaY * multiplier
      const nextScrollTop = scroller.scrollTop + deltaY
      const crossesTop = deltaY < 0 && nextScrollTop <= 0
      const crossesBottom = deltaY > 0 && nextScrollTop >= maxScrollTop

      if (!crossesTop && !crossesBottom) return

      event.preventDefault()
      event.stopPropagation()
      scroller.scrollTop = crossesTop ? 0 : maxScrollTop
    }

    document.addEventListener('wheel', containNestedWheel, { capture: true, passive: false })
    return () => document.removeEventListener('wheel', containNestedWheel, { capture: true })
  }, [])

  useLayoutEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
    setMobileNavOpen(false)
  }, [pathname])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      {mobileNavOpen && (
        <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true" aria-label="工作台导航">
          <button
            type="button"
            aria-label="关闭导航背景"
            className="absolute inset-0 bg-black/25"
            onClick={() => setMobileNavOpen(false)}
          />
          <Sidebar
            mobile
            onNavigate={() => setMobileNavOpen(false)}
            onClose={() => setMobileNavOpen(false)}
          />
        </div>
      )}
      <main className="flex-1 min-w-0 w-full">
        <header className="md:hidden sticky top-0 z-30 h-13 px-4 bg-surface/95 border-b border-border flex items-center gap-3 backdrop-blur-sm">
          <button
            type="button"
            onClick={() => setMobileNavOpen(true)}
            aria-label="打开导航"
            title="打开导航"
            className="w-8 h-8 inline-flex items-center justify-center text-ink-2 hover:text-ink hover:bg-bg-inset rounded-md"
          >
            <Icon name="menu" className="w-5 h-5" />
          </button>
          <div className="w-7 h-7 rounded-md bg-brand text-white flex items-center justify-center text-xs font-bold">旅</div>
          <b className="text-sm">攻略中枢</b>
        </header>
        <div className="px-4 py-4 sm:px-6 md:px-7 md:py-6 max-w-[1180px] mx-auto">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
