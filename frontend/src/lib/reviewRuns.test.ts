import { describe, expect, it } from 'vitest'
import { reviewProgressPresentation, summarizeReviewRunError } from './reviewRuns'

describe('reviewProgressPresentation', () => {
  it('shows a selected failed run as failed even when run history exists', () => {
    expect(reviewProgressPresentation({
      selectedStatus: 'failed',
      hasRecommendation: false,
      hasHistoricalCompletion: false,
      hasSuccessfulRun: false,
      hasFailedRun: true,
    })).toEqual({ label: '执行失败', tone: 'failed' })
  })

  it('only reports historical completion when a result really completed', () => {
    expect(reviewProgressPresentation({
      selectedStatus: null,
      hasRecommendation: false,
      hasHistoricalCompletion: false,
      hasSuccessfulRun: true,
      hasFailedRun: true,
    })).toEqual({ label: '历史完成', tone: 'complete' })
  })
})

describe('summarizeReviewRunError', () => {
  it('replaces a Cloudflare challenge page with a concise provider error', () => {
    const html = '<!DOCTYPE html><title>Just a moment...</title><script>window._cf_chl_opt={cRay: \'abc123\'}</script><div>Enable JavaScript</div><a href="https://challenges.cloudflare.com">challenge</a>'
    const summary = summarizeReviewRunError(html)

    expect(summary).toContain('Cloudflare 人机验证')
    expect(summary).toContain('abc123')
    expect(summary).not.toContain('<script>')
  })
})
