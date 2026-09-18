// 状态字符串取自 src/workspace/schema.py 的 ProjectPhase 枚举，纯函数，方便Vitest单测。

export type BadgeVariant = 'ok' | 'warn' | 'pending' | 'brand' | 'danger'

const STATE_VARIANTS: Record<string, BadgeVariant> = {
  需求收集中: 'pending',
  骨架确认中: 'pending',
  路线规划中: 'warn',
  并行调研中: 'warn',
  整合中: 'warn',
  树木生成中: 'brand',
  完成: 'ok',
}

export function projectStateVariant(state: string): BadgeVariant {
  return STATE_VARIANTS[state] ?? 'pending'
}

// 真实字符串取自 src/workspace/schema.py 的 ConflictRecord.status（默认"pending"，非"open"）。
const CONFLICT_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  pending: 'pending',
  resolved: 'ok',
  force_finalized: 'danger',
}

export function conflictStatusVariant(status: string): BadgeVariant {
  return CONFLICT_STATUS_VARIANTS[status] ?? 'pending'
}

export const BADGE_CLASSES: Record<BadgeVariant, string> = {
  ok: 'bg-ok-soft text-ok',
  warn: 'bg-warn-soft text-[#8a6417]',
  pending: 'bg-pending-soft text-ink-2',
  brand: 'bg-brand-soft text-brand-dark',
  danger: 'bg-danger-soft text-danger',
}

export const BADGE_DOT_CLASSES: Record<BadgeVariant, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  pending: 'bg-pending',
  brand: 'bg-brand',
  danger: 'bg-danger',
}

// 飞书worker心跳状态（src/channels/feishu/health.py::get_worker_health），见FeishuOpsPage。
const FEISHU_HEALTH_VARIANTS: Record<string, BadgeVariant> = {
  healthy: 'ok',
  stale: 'warn',
  not_running: 'danger',
}

export function feishuHealthVariant(status: string): BadgeVariant {
  return FEISHU_HEALTH_VARIANTS[status] ?? 'pending'
}

const EXECUTION_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  queued: 'pending',
  running: 'brand',
  validating: 'brand',
  waiting_user: 'warn',
  waiting_external: 'warn',
  succeeded: 'ok',
  failed: 'danger',
  cancelled: 'pending',
  interrupted: 'danger',
}

export const EXECUTION_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  validating: '校验中',
  waiting_user: '等你处理',
  waiting_external: '等你处理',
  succeeded: '已完成',
  failed: '执行失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

export function executionStatusVariant(status: string, healthStatus?: string): BadgeVariant {
  if (status === 'running' && healthStatus === 'stalled') return 'warn'
  return EXECUTION_STATUS_VARIANTS[status] ?? 'pending'
}

export function executionStatusLabel(status: string, healthStatus?: string): string {
  if (status === 'running' && healthStatus === 'stalled') return '进展停滞'
  return EXECUTION_STATUS_LABELS[status] ?? status
}
