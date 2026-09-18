import { describe, expect, it } from 'vitest'
import {
  conflictStatusVariant,
  executionStatusLabel,
  executionStatusVariant,
  feishuHealthVariant,
  projectStateVariant,
} from './status'

describe('projectStateVariant', () => {
  it('maps known ProjectPhase strings to their badge variant', () => {
    expect(projectStateVariant('需求收集中')).toBe('pending')
    expect(projectStateVariant('并行调研中')).toBe('warn')
    expect(projectStateVariant('树木生成中')).toBe('brand')
    expect(projectStateVariant('完成')).toBe('ok')
  })

  it('falls back to pending for an unknown state instead of throwing', () => {
    expect(projectStateVariant('未来新增的状态')).toBe('pending')
  })
})

describe('conflictStatusVariant', () => {
  it('maps conflict status values to their badge variant', () => {
    expect(conflictStatusVariant('pending')).toBe('pending')
    expect(conflictStatusVariant('resolved')).toBe('ok')
    expect(conflictStatusVariant('force_finalized')).toBe('danger')
  })

  it('falls back to pending for an unknown status', () => {
    expect(conflictStatusVariant('something_else')).toBe('pending')
  })
})

describe('feishuHealthVariant', () => {
  it('maps worker heartbeat status values to their badge variant', () => {
    expect(feishuHealthVariant('healthy')).toBe('ok')
    expect(feishuHealthVariant('stale')).toBe('warn')
    expect(feishuHealthVariant('not_running')).toBe('danger')
  })

  it('falls back to pending for an unknown status', () => {
    expect(feishuHealthVariant('something_else')).toBe('pending')
  })
})

describe('executionStatusVariant', () => {
  it('maps interrupted, waiting-user and stalled states', () => {
    expect(executionStatusVariant('interrupted')).toBe('danger')
    expect(executionStatusLabel('waiting_user')).toBe('等你处理')
    expect(executionStatusVariant('running', 'stalled')).toBe('warn')
    expect(executionStatusLabel('running', 'stalled')).toBe('进展停滞')
  })
})
