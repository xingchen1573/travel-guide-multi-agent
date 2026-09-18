import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Icon } from './Icon'

export function Drawer({ open, onClose, title, subtitle, headerExtra, widthClassName = 'sm:max-w-[640px] lg:max-w-[1040px]', children }: {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  headerExtra?: ReactNode
  widthClassName?: string
  children: ReactNode
}) {
  const [mounted, setMounted] = useState(false)
  const [entered, setEntered] = useState(false)

  useEffect(() => {
    if (!open) return
    setMounted(true)
    const frame = requestAnimationFrame(() => setEntered(true))
    return () => cancelAnimationFrame(frame)
  }, [open])

  useEffect(() => {
    if (open) return
    setEntered(false)
    const timeout = setTimeout(() => setMounted(false), 200)
    return () => clearTimeout(timeout)
  }, [open])

  useEffect(() => {
    if (!open) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!mounted) return null

  return (
    <div className="fixed inset-0 z-50">
      <div
        className={`absolute inset-0 bg-black/30 transition-opacity duration-200 ${entered ? 'opacity-100' : 'opacity-0'}`}
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        className={`absolute inset-y-0 right-0 w-full bg-surface shadow-xl flex flex-col transition-transform duration-200 ease-out ${widthClassName} ${entered ? 'translate-x-0' : 'translate-x-full'}`}
      >
        <div className="shrink-0 border-b border-border px-5 py-3.5 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="font-semibold text-sm text-ink-1 leading-snug break-words">{title}</div>
            {subtitle && <div className="mt-0.5 text-xs text-ink-3">{subtitle}</div>}
          </div>
          <div className="shrink-0 flex items-center gap-2">
            {headerExtra}
            <button type="button" onClick={onClose} aria-label="关闭" className="rounded-md p-1.5 text-ink-3 hover:bg-bg-inset hover:text-ink-1">
              <Icon name="x" className="w-4.5 h-4.5" />
            </button>
          </div>
        </div>
        <div className="scroll-boundary-y min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  )
}
