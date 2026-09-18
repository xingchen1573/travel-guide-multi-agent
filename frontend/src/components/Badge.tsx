import type { ReactNode } from 'react'
import { BADGE_CLASSES, BADGE_DOT_CLASSES, type BadgeVariant } from '../lib/status'

export function Badge({ variant, children }: { variant: BadgeVariant; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11.5px] font-semibold whitespace-nowrap ${BADGE_CLASSES[variant]}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${BADGE_DOT_CLASSES[variant]}`} />
      {children}
    </span>
  )
}
