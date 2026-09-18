import { useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

interface TooltipPosition {
  left: number
  top?: number
  bottom?: number
}

export function Tooltip({ label, content, children }: { label: string; content: ReactNode; children: ReactNode }) {
  const id = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [position, setPosition] = useState<TooltipPosition | null>(null)
  const open = hovered || focused

  useLayoutEffect(() => {
    if (!open) return

    const updatePosition = () => {
      const trigger = triggerRef.current
      if (!trigger) return
      const rect = trigger.getBoundingClientRect()
      const tooltipWidth = Math.min(320, window.innerWidth - 24)
      const left = Math.min(
        Math.max(12, rect.left + rect.width / 2 - tooltipWidth / 2),
        window.innerWidth - tooltipWidth - 12,
      )
      const spaceBelow = window.innerHeight - rect.bottom
      const placeAbove = spaceBelow < 180 && rect.top > spaceBelow
      setPosition(placeAbove
        ? { left, bottom: window.innerHeight - rect.top + 8 }
        : { left, top: rect.bottom + 8 })
    }

    updatePosition()
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [open])

  return <>
    <button
      ref={triggerRef}
      type="button"
      aria-label={label}
      aria-describedby={open ? id : undefined}
      className="inline-flex cursor-help items-center rounded-full text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onClick={() => {
        setFocused(true)
        triggerRef.current?.focus()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          setFocused(false)
          setHovered(false)
          triggerRef.current?.blur()
        }
      }}
    >
      {children}
    </button>
    {open && position && createPortal(
      <span
        id={id}
        role="tooltip"
        style={position}
        className="pointer-events-none fixed z-50 w-[min(20rem,calc(100vw-1.5rem))] border border-border-strong bg-surface px-3 py-2.5 text-left text-[11px] font-normal leading-relaxed text-ink-2 shadow-lg"
      >
        {content}
      </span>,
      document.body,
    )}
  </>
}
