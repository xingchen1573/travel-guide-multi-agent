import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Drawer } from './Drawer'

describe('Drawer scroll containment', () => {
  it('marks the drawer as modal and contains its vertical scroll chain', () => {
    render(
      <Drawer open onClose={vi.fn()} title="问题详情">
        <div>很长的抽屉内容</div>
      </Drawer>,
    )

    const dialog = screen.getByRole('dialog', { name: '' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog.querySelector('.overflow-y-auto')).toHaveClass('scroll-boundary-y')
  })
})
