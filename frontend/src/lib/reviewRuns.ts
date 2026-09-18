import type { ReviewRunStatus } from '../api/types'

export type ModelReviewProgressTone = 'idle' | 'active' | 'attention' | 'failed' | 'complete'

export function reviewProgressPresentation(options: {
  selectedStatus: ReviewRunStatus | null
  hasRecommendation: boolean
  hasHistoricalCompletion: boolean
  hasSuccessfulRun: boolean
  hasFailedRun: boolean
}): { label: string; tone: ModelReviewProgressTone } {
  const { selectedStatus, hasRecommendation, hasHistoricalCompletion, hasSuccessfulRun, hasFailedRun } = options
  if (selectedStatus === 'queued') return { label: '排队中', tone: 'active' }
  if (selectedStatus === 'running') return { label: '运行中', tone: 'active' }
  if (selectedStatus === 'validating') return { label: '校验中', tone: 'active' }
  if (selectedStatus === 'failed') return { label: '执行失败', tone: 'failed' }
  if (selectedStatus === 'interrupted') return { label: '已中断', tone: 'failed' }
  if (selectedStatus === 'cancelled') return { label: '已取消', tone: 'failed' }
  if (selectedStatus === 'waiting_user' || selectedStatus === 'waiting_external') {
    return { label: '等你处理', tone: 'attention' }
  }
  if (hasRecommendation) return { label: '已有结果', tone: 'complete' }
  if (selectedStatus === 'succeeded') return { label: '已完成', tone: 'complete' }
  if (hasHistoricalCompletion || hasSuccessfulRun) return { label: '历史完成', tone: 'complete' }
  if (hasFailedRun) return { label: '执行失败', tone: 'failed' }
  return { label: '待开始', tone: 'idle' }
}

export function summarizeReviewRunError(error: string): string {
  const raw = error.trim()
  const lowered = raw.toLowerCase()
  if (
    lowered.includes('challenges.cloudflare.com')
    || lowered.includes('_cf_chl_opt')
    || (lowered.includes('<title>just a moment...</title>') && lowered.includes('enable javascript'))
  ) {
    const ray = raw.match(/cRay:\s*['"]([^'"]+)/)?.[1]
    return `上游模型接口触发 Cloudflare 人机验证，未返回模型结果${ray ? `（Ray ID: ${ray}）` : ''}。请稍后重试或切换模型渠道。`
  }
  if (lowered.includes('<html') || lowered.includes('<!doctype html')) {
    return '上游模型服务返回了网页错误响应，未生成复核结果。'
  }
  const compact = raw.replace(/\s+/g, ' ') || '未知技术错误'
  return compact.length > 600 ? `${compact.slice(0, 600)}…` : compact
}
