import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api } from './client'
import type { CollectPlatform, KeyPoolGroup, MimoChannel, UserNoteInput } from './types'

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: api.listProjects,
    refetchInterval: (query) => (
      query.state.data?.some((project) => ['queued', 'running'].includes(project.latest_job_status ?? ''))
        ? 2000
        : 30000
    ),
  })
}

export function useExperiments() {
  return useQuery({ queryKey: ['experiments'], queryFn: api.listExperiments })
}

export function useComparisonPairs() {
  return useQuery({ queryKey: ['experiment-pairs'], queryFn: api.listComparisonPairs })
}

export function useExperiment(projectId: string) {
  return useQuery({
    queryKey: ['experiment', projectId],
    queryFn: () => api.getExperiment(projectId),
    enabled: !!projectId,
  })
}

export function useReindexExperiments() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: api.reindexExperiments,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['experiments'] })
      await queryClient.invalidateQueries({ queryKey: ['experiment-pairs'] })
      await queryClient.invalidateQueries({ queryKey: ['experiment'] })
    },
  })
}

export function useIssueTypeStats() {
  return useQuery({ queryKey: ['issue-type-stats'], queryFn: api.getIssueTypeStats })
}

export function useProject(projectId: string) {
  return useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.getProject(projectId),
    enabled: !!projectId,
  })
}

export function useReviewFindings(projectId: string, includeClosed = true) {
  return useQuery({
    queryKey: ['review-findings', projectId, includeClosed],
    queryFn: () => api.listReviewFindings(projectId, includeClosed),
    enabled: !!projectId,
  })
}

export function useReviewQueue(projectId: string, includeClosed = true) {
  return useQuery({
    queryKey: ['review-queue', projectId, includeClosed],
    queryFn: () => api.listReviewQueue(projectId, includeClosed),
    enabled: !!projectId,
  })
}

export function useReviewState(projectId: string) {
  return useQuery({
    queryKey: ['review-state', projectId],
    queryFn: () => api.getReviewState(projectId),
    enabled: !!projectId,
  })
}

export function useReviewValidation(projectId: string) {
  const queryClient = useQueryClient()
  const previewQuery = useQuery({
    queryKey: ['review-validation-preview', projectId],
    queryFn: () => api.getReviewValidationPreview(projectId),
    enabled: !!projectId,
  })
  const statusQuery = useQuery({
    queryKey: ['review-validation-status', projectId],
    queryFn: () => api.getReviewValidationStatus(projectId),
    enabled: !!projectId,
    refetchInterval: (query) => (
      ['queued', 'running'].includes(query.state.data?.status ?? '') ? 1500 : false
    ),
  })
  const mutation = useMutation({
    mutationFn: () => api.startReviewValidation(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['review-validation-status', projectId] })
    },
  })
  const finished = ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(statusQuery.data?.status ?? '')

  useEffect(() => {
    if (!finished) return
    queryClient.invalidateQueries({ queryKey: ['review-validation-preview', projectId] })
    queryClient.invalidateQueries({ queryKey: ['review-findings', projectId] })
    queryClient.invalidateQueries({ queryKey: ['review-queue', projectId] })
    queryClient.invalidateQueries({ queryKey: ['review-state', projectId] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished, projectId])

  return {
    preview: previewQuery.data,
    status: statusQuery.data,
    start: mutation.mutateAsync,
    isStarting: mutation.isPending,
    error: mutation.error || statusQuery.error || previewQuery.error,
  }
}

export function useReviewRecommendation(projectId: string, findingId: string, recommendationId: string | null) {
  return useQuery({
    queryKey: ['review-recommendation', projectId, findingId, recommendationId],
    queryFn: () => api.getReviewRecommendation(projectId, findingId),
    enabled: !!projectId && !!findingId && !!recommendationId,
    retry: false,
  })
}

export function useReviewRecommendationById(projectId: string, recommendationId: string | null) {
  return useQuery({
    queryKey: ['review-recommendation-by-id', projectId, recommendationId],
    queryFn: () => api.getReviewRecommendationById(projectId, recommendationId ?? ''),
    enabled: !!projectId && !!recommendationId,
    retry: false,
  })
}

export function useReviewRecommendationHistory(projectId: string, findingId: string) {
  return useQuery({
    queryKey: ['review-recommendation-history', projectId, findingId],
    queryFn: () => api.listReviewRecommendationHistory(projectId, findingId),
    enabled: !!projectId && !!findingId,
  })
}

export function useReviewExecutors() {
  return useQuery({ queryKey: ['review-executors'], queryFn: api.listReviewExecutors })
}

export function useReviewRuns(projectId: string, findingId: string) {
  return useQuery({
    queryKey: ['review-runs', projectId, findingId],
    queryFn: () => api.listReviewRuns(projectId, findingId),
    enabled: !!projectId && !!findingId,
    refetchInterval: (query) => (
      query.state.data?.some((run) => ['queued', 'running', 'validating'].includes(run.status)) ? 1500 : false
    ),
  })
}

export function useReviewComparison(projectId: string, comparisonGroupId: string | null) {
  return useQuery({
    queryKey: ['review-comparison', projectId, comparisonGroupId],
    queryFn: () => api.getReviewComparison(projectId, comparisonGroupId ?? ''),
    enabled: !!projectId && !!comparisonGroupId,
    refetchInterval: (query) => (
      query.state.data?.runs.some((run) => ['queued', 'running', 'validating'].includes(run.status)) ? 1500 : false
    ),
  })
}

export function useReviewContext(projectId: string, findingId: string) {
  return useQuery({
    queryKey: ['review-context', projectId, findingId],
    queryFn: () => api.getReviewContext(projectId, findingId),
    enabled: !!projectId && !!findingId,
  })
}

export function useReviewItemDecisions(projectId: string, findingId: string, recommendationId: string | null) {
  return useQuery({
    queryKey: ['review-item-decisions', projectId, findingId, recommendationId],
    queryFn: () => api.getReviewItemDecisions(projectId, findingId, recommendationId ?? ''),
    enabled: !!projectId && !!findingId && !!recommendationId,
  })
}

export function useGlobalOptimizationCandidates(threshold = 3) {
  return useQuery({
    queryKey: ['global-optimization-candidates', threshold],
    queryFn: () => api.getGlobalOptimizationCandidates(threshold),
  })
}

export function useImprovementSignals(projectId: string) {
  return useQuery({
    queryKey: ['improvement-signals', projectId],
    queryFn: () => api.listImprovementSignals(projectId),
    enabled: !!projectId,
  })
}

export function useQualityMetrics() {
  return useQuery({
    queryKey: ['quality-metrics'],
    queryFn: api.getQualityMetrics,
  })
}

export function useOptimizationRegressionCases(candidateId: string | null) {
  return useQuery({
    queryKey: ['optimization-regression-cases', candidateId],
    queryFn: () => api.listOptimizationRegressionCases(candidateId ?? ''),
    enabled: !!candidateId,
  })
}

export function useOptimizationRegressionRuns(candidateId: string | null) {
  return useQuery({
    queryKey: ['optimization-regression-runs', candidateId],
    queryFn: () => api.listOptimizationRegressionRuns(candidateId ?? ''),
    enabled: !!candidateId,
    refetchInterval: (query) => (
      query.state.data?.some((run) => ['queued', 'running'].includes(run.status)) ? 1500 : false
    ),
  })
}

export function useReviewEvalScenarios() {
  return useQuery({
    queryKey: ['review-eval-scenarios'],
    queryFn: api.listReviewEvalScenarios,
  })
}

export function useGlobalReviewFindings(includeClosed = true) {
  return useQuery({
    queryKey: ['global-review-findings', includeClosed],
    queryFn: () => api.getGlobalReviewFindings(includeClosed),
  })
}

export function useProjectOutputs(projectId: string) {
  return useQuery({
    queryKey: ['project-outputs', projectId],
    queryFn: () => api.getProjectOutputs(projectId),
    enabled: !!projectId,
  })
}

export function useProjectTimeline(projectId: string) {
  return useQuery({
    queryKey: ['project-timeline', projectId],
    queryFn: () => api.getProjectTimeline(projectId),
    enabled: !!projectId,
  })
}

export function useMaterialTopics() {
  return useQuery({ queryKey: ['material-topics'], queryFn: api.listMaterialTopics })
}

export function useDeleteMaterialTopic() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (topic: string) => api.deleteMaterialTopic(topic),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['material-topics'] })
      queryClient.invalidateQueries({ queryKey: ['material-topic-trash'] })
    },
  })
}

export function useMaterialEntries(topic: string) {
  return useQuery({
    queryKey: ['material-entries', topic],
    queryFn: () => api.getMaterialEntries(topic),
    enabled: !!topic,
  })
}

/** 素材软删除：当前库、统计和素材回收站都需要同步刷新。 */
export function useDeleteMaterial(topic: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (docId: string) => api.deleteMaterial(topic, docId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['material-topics'] })
      queryClient.invalidateQueries({ queryKey: ['material-entries', topic] })
      queryClient.invalidateQueries({ queryKey: ['material-entity-stats', topic] })
      queryClient.invalidateQueries({ queryKey: ['material-stats', topic] })
      queryClient.invalidateQueries({ queryKey: ['material-insights', topic] })
      queryClient.invalidateQueries({ queryKey: ['material-trash'] })
    },
  })
}

/** 触发重跑 + 轮询任务状态直到success/failed。轮询范围只覆盖"这次会话里触发的重跑"，
 * 不在页面加载时就去猜是否有历史遗留的运行中任务——对应/grilling确认过的"后台任务+轮询"设计。 */
export function useRerunProject(projectId: string) {
  const queryClient = useQueryClient()
  const [watching, setWatching] = useState(false)

  const statusQuery = useQuery({
    queryKey: ['rerun-status', projectId],
    queryFn: () => api.getRerunStatus(projectId),
    enabled: watching,
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.status ?? '') ? 1500 : false),
  })

  const mutation = useMutation({
    mutationFn: () => api.rerunProject(projectId),
    onSuccess: () => {
      setWatching(true)
      queryClient.invalidateQueries({ queryKey: ['rerun-status', projectId] })
    },
  })

  const isDone = ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(statusQuery.data?.status ?? '')

  useEffect(() => {
    if (!isDone || !watching) return
    // 任务结束后停止轮询（refetchInterval已经返回false了），并刷新project详情/产出，
    // 让页面自动反映重跑后的新数据，不需要用户手动刷新。
    queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    queryClient.invalidateQueries({ queryKey: ['project-outputs', projectId] })
    queryClient.invalidateQueries({ queryKey: ['project-timeline', projectId] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDone, watching, projectId])

  return {
    trigger: mutation.mutate,
    triggerError: mutation.error,
    isTriggering: mutation.isPending,
    status: statusQuery.data,
  }
}

/** 机制B：Agent运行状态近实时轮询（当前project的进度页面打开时持续短间隔轮询，
 * 不区分是CLI还是后台API触发的执行——run_status.json是同一份文件）。*/
export function useRunStatus(projectId: string) {
  return useQuery({
    queryKey: ['run-status', projectId],
    queryFn: () => api.getRunStatus(projectId),
    enabled: !!projectId,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false),
  })
}

/** 软删除project：移入回收站，成功后project列表/回收站列表都要刷新。*/
export function useDeleteProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (projectId: string) => api.deleteProject(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      queryClient.invalidateQueries({ queryKey: ['trash'] })
    },
  })
}

/** 回收站列表：跟useProjects同款一次性拉取（回收站内容不需要高频轮询）。*/
export function useTrash() {
  return useQuery({ queryKey: ['trash'], queryFn: api.listTrash })
}

export function useRestoreProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (projectId: string) => api.restoreProject(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['trash'] })
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

export function usePermanentlyDeleteProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (projectId: string) => api.permanentlyDeleteProject(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['trash'] })
    },
  })
}

export function useMaterialTrash() {
  return useQuery({ queryKey: ['material-trash'], queryFn: api.listMaterialTrash })
}

export function useRestoreMaterial() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ topic, docId }: { topic: string; docId: string }) => api.restoreMaterial(topic, docId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['material-trash'] })
      queryClient.invalidateQueries({ queryKey: ['material-topics'] })
      queryClient.invalidateQueries({ queryKey: ['material-entries', variables.topic] })
      queryClient.invalidateQueries({ queryKey: ['material-entity-stats', variables.topic] })
      queryClient.invalidateQueries({ queryKey: ['material-stats', variables.topic] })
      queryClient.invalidateQueries({ queryKey: ['material-insights', variables.topic] })
    },
  })
}

export function usePermanentlyDeleteMaterial() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ topic, docId }: { topic: string; docId: string }) =>
      api.permanentlyDeleteMaterial(topic, docId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['material-trash'] })
    },
  })
}

export function useMaterialTopicTrash() {
  return useQuery({ queryKey: ['material-topic-trash'], queryFn: api.listMaterialTopicTrash })
}

export function useRestoreMaterialTopic() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (topic: string) => api.restoreMaterialTopic(topic),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['material-topic-trash'] })
      queryClient.invalidateQueries({ queryKey: ['material-topics'] })
    },
  })
}

export function usePermanentlyDeleteMaterialTopic() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (topic: string) => api.permanentlyDeleteMaterialTopic(topic),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['material-topic-trash'] })
    },
  })
}

/** 素材详情弹窗：chunk文本 + raw原文，只在弹窗打开(enabled)时才请求。*/
export function useMaterialEntryDetail(topic: string, docId: string | null) {
  return useQuery({
    queryKey: ['material-entry-detail', topic, docId],
    queryFn: () => api.getMaterialEntryDetail(topic, docId as string),
    enabled: !!topic && !!docId,
  })
}

/** 跨全库实体提及次数统计，随topic切换重新拉取。*/
export function useMaterialEntityStats(topic: string) {
  return useQuery({
    queryKey: ['material-entity-stats', topic],
    queryFn: () => api.getMaterialEntityStats(topic),
    enabled: !!topic,
  })
}

/** 素材库统计画像（平台分布/内容年龄/核心占比/作者多样性/拒绝理由），现算不缓存
 * （见2026-08-22 grilling讨论），随topic切换重新拉取。*/
export function useMaterialStats(topic: string) {
  return useQuery({
    queryKey: ['material-stats', topic],
    queryFn: () => api.getMaterialStats(topic),
    enabled: !!topic,
  })
}

/** LLM共识挖掘产物，随topic切换重新拉取；不存在时返回null（还没生成过，不是错误）。*/
export function useMaterialInsights(topic: string) {
  return useQuery({
    queryKey: ['material-insights', topic],
    queryFn: () => api.getTopicInsights(topic),
    enabled: !!topic,
  })
}

/** 手动"重新生成洞察"，同步阻塞直到LLM调用完成返回，不走轮询（单次调用耗时量级
 * 在几秒到几十秒，不需要jobs.py那套异步任务框架）。*/
export function useRegenerateInsights(topic: string) {
  const queryClient = useQueryClient()

  // enabled不依赖"这次组件生命周期里触没触发过"（不像useRerunProject那样用一个
  // watching标志位）——离开这个页面再回来，组件是全新挂载的，任何"只在触发之后才轮询"
  // 的设计在这里都会显得"进度消失了"。改成挂载时就无条件查一次服务端真实任务状态：
  // 如果之前那次触发还在跑，这里立刻查到running并继续轮询；如果已经跑完，直接查到
  // success/failed，不会有"看起来像中断了"这种假象（2026-08-01真实用户反馈修复）。
  const statusQuery = useQuery({
    queryKey: ['regenerate-insights-status', topic],
    queryFn: () => api.getRegenerateInsightsStatus(topic),
    enabled: !!topic,
    retry: false, // 404（还没触发过）不用重试，直接当"无记录"处理
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.status ?? '') ? 1500 : false),
  })

  const mutation = useMutation({
    mutationFn: () => api.regenerateTopicInsights(topic),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['regenerate-insights-status', topic] })
    },
  })

  const isDone = ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(statusQuery.data?.status ?? '')

  useEffect(() => {
    if (!isDone) return
    queryClient.invalidateQueries({ queryKey: ['material-insights', topic] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDone, topic])

  return {
    mutate: mutation.mutate,
    isPending: mutation.isPending || ['queued', 'running'].includes(statusQuery.data?.status ?? ''),
    isError: mutation.isError || statusQuery.data?.status === 'failed',
    error: mutation.error ?? (statusQuery.data?.status === 'failed' ? new Error(statusQuery.data.error ?? '生成失败') : null),
  }
}

/** 触发素材采集 + 轮询任务状态直到success/failed，跟useRerunProject同款轮询模式，
 * 只是key用topic而不是project_id（素材库是跨project共享的全局库）。
 * trigger(requestText, platform)：platform默认"all"，透传给后端做工具级平台硬过滤。*/
export function useCollectMaterial(topic: string) {
  const queryClient = useQueryClient()
  const [watching, setWatching] = useState(false)

  const statusQuery = useQuery({
    queryKey: ['collect-status', topic],
    queryFn: () => api.getCollectStatus(topic),
    enabled: watching,
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.status ?? '') ? 1500 : false),
  })

  const mutation = useMutation({
    mutationFn: ({ requestText, platform }: { requestText: string; platform: CollectPlatform }) =>
      api.triggerCollectMaterial(topic, requestText, platform),
    onSuccess: () => {
      setWatching(true)
      queryClient.invalidateQueries({ queryKey: ['collect-status', topic] })
    },
  })

  const isDone = ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(statusQuery.data?.status ?? '')

  useEffect(() => {
    if (!isDone || !watching) return
    queryClient.invalidateQueries({ queryKey: ['material-topics'] })
    queryClient.invalidateQueries({ queryKey: ['material-entries', topic] })
    queryClient.invalidateQueries({ queryKey: ['material-entity-stats', topic] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDone, watching, topic])

  return {
    trigger: (requestText: string, platform: CollectPlatform = 'all') =>
      mutation.mutate({ requestText, platform }),
    triggerError: mutation.error,
    isTriggering: mutation.isPending,
    status: statusQuery.data,
  }
}

/** 触发生成网站/海报 + 轮询任务状态，跟useRerunProject/useCollectMaterial同款模式。
 * 完成后顺带刷新版本历史列表（生成前旧版本已被后端归档，历史列表需要跟着变长）。*/
export function useGenerateSite(projectId: string) {
  const queryClient = useQueryClient()
  const [watching, setWatching] = useState(false)

  const statusQuery = useQuery({
    queryKey: ['generate-site-status', projectId],
    queryFn: () => api.getGenerateSiteStatus(projectId),
    enabled: watching,
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.status ?? '') ? 1500 : false),
  })

  const mutation = useMutation({
    mutationFn: () => api.triggerGenerateSite(projectId),
    onSuccess: () => {
      setWatching(true)
      queryClient.invalidateQueries({ queryKey: ['generate-site-status', projectId] })
    },
  })

  const isDone = ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(statusQuery.data?.status ?? '')

  useEffect(() => {
    if (!isDone || !watching) return
    queryClient.invalidateQueries({ queryKey: ['site-history', projectId] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDone, watching, projectId])

  return {
    trigger: mutation.mutate,
    triggerError: mutation.error,
    isTriggering: mutation.isPending,
    status: statusQuery.data,
  }
}

export function useGeneratePoster(
  projectId: string,
  options: { scope?: 'overall' | 'day'; day?: number } = {},
) {
  const scope = options.scope ?? 'overall'
  const day = options.day
  const queryClient = useQueryClient()
  const [watching, setWatching] = useState(false)

  const statusQuery = useQuery({
    queryKey: ['generate-poster-status', projectId, scope, day],
    queryFn: () => api.getGeneratePosterStatus(projectId, scope, day),
    enabled: watching,
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.status ?? '') ? 1500 : false),
  })

  const mutation = useMutation({
    mutationFn: () => api.triggerGeneratePoster(projectId, scope, day),
    onSuccess: () => {
      setWatching(true)
      queryClient.invalidateQueries({ queryKey: ['generate-poster-status', projectId, scope, day] })
    },
  })

  const isDone = ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(statusQuery.data?.status ?? '')

  useEffect(() => {
    if (!isDone || !watching) return
    queryClient.invalidateQueries({ queryKey: ['poster-history', projectId, scope, day] })
    queryClient.invalidateQueries({ queryKey: ['poster-prompt', projectId, scope, day] })
    queryClient.invalidateQueries({ queryKey: ['poster-assets', projectId] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDone, watching, projectId, scope, day])

  return {
    trigger: mutation.mutate,
    triggerError: mutation.error,
    isTriggering: mutation.isPending,
    status: statusQuery.data,
  }
}

/** 版本历史：生成网站/海报前旧产物会被归档，这里列出历次时间戳供页面渲染历史列表。
 * 生成完成后由useGenerateSite/useGeneratePoster自动invalidate，不需要页面手动刷新。*/
export function useSiteHistory(projectId: string) {
  return useQuery({
    queryKey: ['site-history', projectId],
    queryFn: () => api.getSiteHistory(projectId),
    enabled: !!projectId,
  })
}

export function usePosterHistory(projectId: string, _options: { scope?: 'overall' | 'day'; day?: number } = {}) {
  const scope = _options.scope ?? 'overall'
  const day = _options.day
  return useQuery({
    queryKey: ['poster-history', projectId, scope, day],
    queryFn: () => api.getPosterHistory(projectId, scope, day),
    enabled: !!projectId,
  })
}

export function usePosterAssets(projectId: string) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.getPosterAssets>>>()
  useEffect(() => {
    let active = true
    if (!projectId) return () => { active = false }
    api.getPosterAssets(projectId).then((assets) => {
      if (active) setData(assets)
    }).catch(() => {
      // Imported assets are optional; a missing endpoint must not hide generated previews.
    })
    return () => { active = false }
  }, [projectId])
  return { data }
}

/** 海报审计存档（poster_prompt.txt）：核对实际喂给生图模型的内容有没有被篡改。*/
export function usePosterPrompt(
  projectId: string,
  enabled: boolean,
  options: { scope?: 'overall' | 'day'; day?: number } = {},
) {
  const scope = options.scope ?? 'overall'
  const day = options.day
  return useQuery({
    queryKey: ['poster-prompt', projectId, scope, day],
    queryFn: () => api.getPosterPrompt(projectId, scope, day),
    enabled: enabled && !!projectId,
  })
}

export function usePosterHistoryPrompt(
  projectId: string,
  timestamp: string | null,
  options: { scope?: 'overall' | 'day'; day?: number } = {},
) {
  const scope = options.scope ?? 'overall'
  const day = options.day
  return useQuery({
    queryKey: ['poster-history-prompt', projectId, timestamp, scope, day],
    queryFn: () => api.getPosterHistoryPrompt(projectId, timestamp as string, scope, day),
    enabled: !!projectId && !!timestamp,
  })
}

export function useModelConfig() {
  return useQuery({ queryKey: ['model-config'], queryFn: api.getModelConfig })
}

export function useSetModel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ model, group }: { model: string | null; group: 'full' | 'light' }) =>
      api.setModel(model, group),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['model-config'] })
    },
  })
}

/** 手动可用性检测：不自动/定时跑，只在用户点击"测试"按钮时发起一次真实调用
 * （见docs/ISSUES.md，此前模型/Key页面从没验证过"配置的key现在真的能用"）。*/
export function useTestModelKeys() {
  return useMutation({
    mutationFn: ({ group, keyIndex }: { group: KeyPoolGroup; keyIndex?: number }) =>
      api.testModelKeys(group, keyIndex),
  })
}

/** 新增key到某个分组，实时生效（2026-08-01新增，推翻旧"不可编辑"政策）。*/
export function useAddModelKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      group,
      key,
      channel,
    }: {
      group: KeyPoolGroup
      key: string
      channel?: MimoChannel
    }) => api.addModelKey(group, key, channel),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['model-config'] })
    },
  })
}

export function useDeleteModelKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ group, index }: { group: KeyPoolGroup; index: number }) =>
      api.deleteModelKey(group, index),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['model-config'] })
    },
  })
}

// ---------- 飞书运维可视化（群↔project绑定、worker心跳健康状态，2026-07-26新增） ----------

/** 绑定关系不需要高频轮询——群↔project绑定只在新群第一条消息时才会新增，不是持续变化
 * 的数据，页面打开时拉一次即可，用户可以手动刷新页面看最新状态。*/
export function useFeishuBindings() {
  return useQuery({ queryKey: ['feishu-bindings'], queryFn: api.listFeishuBindings })
}

/** worker健康状态需要近实时反映"进程是否还活着"，用跟useRunStatus同款2秒轮询间隔——
 * worker心跳每20秒才更新一次，2秒轮询主要是为了让状态从stale/not_running恢复到healthy
 * 时用户能较快看到，不是因为心跳本身变化很快。*/
export function useFeishuHealth() {
  return useQuery({
    queryKey: ['feishu-health'],
    queryFn: api.getFeishuHealth,
    refetchInterval: 2000,
  })
}

/** 推送日志/心跳历史/活跃度统计（2026-08-01新增）：都是历史记录型数据，不是持续
 * 变化的实时状态，跟useFeishuBindings同款"拉一次，用户手动刷新页面看最新"模式，
 * 不用轮询。*/
export function useFeishuPushLog(limit?: number) {
  return useQuery({
    queryKey: ['feishu-push-log', limit],
    queryFn: () => api.getFeishuPushLog(limit),
  })
}

export function useFeishuHeartbeatHistory(limit?: number) {
  return useQuery({
    queryKey: ['feishu-heartbeat-history', limit],
    queryFn: () => api.getFeishuHeartbeatHistory(limit),
  })
}

export function useFeishuActivityStats() {
  return useQuery({ queryKey: ['feishu-activity-stats'], queryFn: api.getFeishuActivityStats })
}

// ---------- POI图片：管理后台人工上传真实照片 ----------

export function usePoiDetails(projectId: string) {
  return useQuery({
    queryKey: ['poi-details', projectId],
    queryFn: () => api.getPoiDetails(projectId),
    enabled: !!projectId,
  })
}

export function useUploadOverviewPhoto(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ day, poiName, file }: { day: number; poiName: string; file: File }) =>
      api.uploadOverviewPhoto(projectId, day, poiName, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['poi-details', projectId] })
    },
  })
}

export function useDeleteOverviewPhoto(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ day, poiName }: { day: number; poiName: string }) =>
      api.deleteOverviewPhoto(projectId, day, poiName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['poi-details', projectId] })
    },
  })
}

export function useUploadSegmentPhoto(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      day,
      poiName,
      routeName,
      order,
      file,
    }: {
      day: number
      poiName: string
      routeName: string
      order: number
      file: File
    }) => api.uploadSegmentPhoto(projectId, day, poiName, routeName, order, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['poi-details', projectId] })
    },
  })
}

export function useDeleteSegmentPhoto(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      day,
      poiName,
      routeName,
      order,
    }: {
      day: number
      poiName: string
      routeName: string
      order: number
    }) => api.deleteSegmentPhoto(projectId, day, poiName, routeName, order),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['poi-details', projectId] })
    },
  })
}

// ---------- 用户备注/annotation layer（docs/ARCHITECTURE.md第21节，阶段2后台管理） ----------
// 改/删后要触发重建site让备注在网页可见，跟网页表单提交后的重建路径保持一致
// （见run_generate_site.py的_render_user_note_form里fetch /generate_site的逻辑）。

export function useUserNotes(projectId: string) {
  return useQuery({
    queryKey: ['user-notes', projectId],
    queryFn: () => api.listUserNotes(projectId),
    enabled: !!projectId,
  })
}

export function useCreateUserNote(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: UserNoteInput) => api.createUserNote(projectId, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-notes', projectId] })
    },
  })
}

export function useUpdateUserNote(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ noteId, input }: { noteId: string; input: UserNoteInput }) =>
      api.updateUserNote(projectId, noteId, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-notes', projectId] })
    },
  })
}

export function useDeleteUserNote(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (noteId: string) => api.deleteUserNote(projectId, noteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-notes', projectId] })
    },
  })
}
