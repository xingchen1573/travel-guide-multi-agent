import type {
  CollectPlatform,
  CollectStatus,
  DeleteMaterialResponse,
  DeleteMaterialTopicResponse,
  DeleteProjectResponse,
  EntityStat,
  ActivityStat,
  FeishuBinding,
  FeishuHealth,
  HeartbeatHistoryResponse,
  PushLogEntry,
  IssueTypeStat,
  MaterialEntry,
  MaterialEntryDetail,
  MaterialTrashActionResponse,
  MaterialTopicTrashActionResponse,
  MaterialStats,
  MaterialTopicSummary,
  KeyPoolGroup,
  KeyStatusEntry,
  MimoChannel,
  ModelConfig,
  ModelKeyTestResponse,
  PosterPromptDetail,
  PosterAssets,
  PresentationHistoryList,
  PresentationStatus,
  PoiDetailFile,
  ProjectDetail,
  ProjectOutputs,
  ProjectSummary,
  RegenerateInsightsStatus,
  RerunResponse,
  RerunStatus,
  RunStatus,
  TimelineEvent,
  TopicInsights,
  TrashActionResponse,
  TrashedMaterialSummary,
  TrashedMaterialTopicSummary,
  TrashedProjectSummary,
  UserNote,
  UserNoteInput,
  CascadePlan,
  ImprovementSignal,
  OptimizationCandidate,
  OptimizationRegressionCase,
  OptimizationRegressionRun,
  QualityMetrics,
  ReviewFinding,
  ReviewQueueGroup,
  ReviewContext,
  ManifestConsistencyRepairResult,
  ReviewDecisionFinalization,
  ReviewItemDecision,
  ReviewPatch,
  ReviewRecommendation,
  ReviewRecommendationHistoryEntry,
  ReviewExecutor,
  ReviewRun,
  ReviewComparison,
  ReviewState,
  ReviewValidationScope,
  ReviewValidationStatus,
  ExperimentDetail,
  ComparisonPair,
  ExperimentReindexResult,
  ExperimentSummary,
} from './types'

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init)
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new ApiError(resp.status, body.detail ?? `请求失败：${resp.status}`)
  }
  return resp.json() as Promise<T>
}

function postJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
}

// 不手动设Content-Type——让浏览器自己为FormData生成带boundary的multipart头，
// 手动设反而会因为缺少boundary参数导致后端解析失败。
function postForm<T>(path: string, formData: FormData): Promise<T> {
  return request<T>(path, { method: 'POST', body: formData })
}

export const api = {
  listExperiments: () => request<ExperimentSummary[]>('/api/experiments'),
  listComparisonPairs: () => request<ComparisonPair[]>('/api/experiments/pairs'),
  getComparisonPair: (pairId: string) =>
    request<ComparisonPair>(`/api/experiments/pairs/${encodeURIComponent(pairId)}`),
  getExperiment: (projectId: string) =>
    request<ExperimentDetail>(`/api/experiments/${encodeURIComponent(projectId)}`),
  reindexExperiments: () => postJson<ExperimentReindexResult>('/api/experiments/reindex'),
  experimentEvidenceUrl: (projectId: string, path: string) =>
    `/api/experiments/${encodeURIComponent(projectId)}/evidence?path=${encodeURIComponent(path)}`,
  listProjects: () => request<ProjectSummary[]>('/api/projects'),
  getIssueTypeStats: () => request<IssueTypeStat[]>('/api/projects/issue_type_stats'),
  getProject: (projectId: string) => request<ProjectDetail>(`/api/projects/${projectId}`),
  getProjectOutputs: (projectId: string) =>
    request<ProjectOutputs>(`/api/projects/${projectId}/outputs`),
  getProjectTimeline: (projectId: string) =>
    request<TimelineEvent[]>(`/api/projects/${projectId}/timeline`),
  rerunProject: (projectId: string) =>
    postJson<RerunResponse>(`/api/projects/${projectId}/rerun`),
  getRerunStatus: (projectId: string) =>
    request<RerunStatus>(`/api/projects/${projectId}/rerun/status`),
  getRunStatus: (projectId: string) =>
    request<RunStatus | null>(`/api/projects/${projectId}/run_status`),
  deleteProject: (projectId: string) =>
    request<DeleteProjectResponse>(`/api/projects/${projectId}`, { method: 'DELETE' }),
  listTrash: () => request<TrashedProjectSummary[]>('/api/trash'),
  restoreProject: (projectId: string) =>
    postJson<TrashActionResponse>(`/api/trash/${projectId}/restore`),
  permanentlyDeleteProject: (projectId: string) =>
    request<TrashActionResponse>(`/api/trash/${projectId}`, { method: 'DELETE' }),
  listMaterialTrash: () => request<TrashedMaterialSummary[]>('/api/trash/materials'),
  restoreMaterial: (topic: string, docId: string) =>
    postJson<MaterialTrashActionResponse>(
      `/api/trash/materials/${encodeURIComponent(topic)}/${encodeURIComponent(docId)}/restore`,
    ),
  permanentlyDeleteMaterial: (topic: string, docId: string) =>
    request<MaterialTrashActionResponse>(
      `/api/trash/materials/${encodeURIComponent(topic)}/${encodeURIComponent(docId)}`,
      { method: 'DELETE' },
    ),
  listMaterialTopicTrash: () => request<TrashedMaterialTopicSummary[]>('/api/trash/material-topics'),
  restoreMaterialTopic: (topic: string) =>
    postJson<MaterialTopicTrashActionResponse>(
      `/api/trash/material-topics/${encodeURIComponent(topic)}/restore`,
    ),
  permanentlyDeleteMaterialTopic: (topic: string) =>
    request<MaterialTopicTrashActionResponse>(
      `/api/trash/material-topics/${encodeURIComponent(topic)}`,
      { method: 'DELETE' },
    ),
  listMaterialTopics: () => request<MaterialTopicSummary[]>('/api/materials'),
  getMaterialEntries: (topic: string) =>
    request<MaterialEntry[]>(`/api/materials/${encodeURIComponent(topic)}`),
  getMaterialEntryDetail: (topic: string, docId: string) =>
    request<MaterialEntryDetail>(
      `/api/materials/${encodeURIComponent(topic)}/entries/${encodeURIComponent(docId)}`,
    ),
  deleteMaterial: (topic: string, docId: string) =>
    request<DeleteMaterialResponse>(
      `/api/materials/${encodeURIComponent(topic)}/entries/${encodeURIComponent(docId)}`,
      { method: 'DELETE' },
    ),
  deleteMaterialTopic: (topic: string) =>
    request<DeleteMaterialTopicResponse>(`/api/materials/${encodeURIComponent(topic)}`, {
      method: 'DELETE',
    }),
  triggerCollectMaterial: (topic: string, requestText: string, platform: CollectPlatform = 'all') =>
    postJson<{ topic: string; status: 'running' }>(
      `/api/materials/${encodeURIComponent(topic)}/collect`,
      { request: requestText, platform },
    ),
  getCollectStatus: (topic: string) =>
    request<CollectStatus>(`/api/materials/${encodeURIComponent(topic)}/collect/status`),
  getMaterialEntityStats: (topic: string) =>
    request<EntityStat[]>(`/api/materials/${encodeURIComponent(topic)}/entity_stats`),
  getMaterialStats: (topic: string) =>
    request<MaterialStats>(`/api/materials/${encodeURIComponent(topic)}/stats`),
  getTopicInsights: (topic: string) =>
    request<TopicInsights | null>(`/api/materials/${encodeURIComponent(topic)}/insights`),
  regenerateTopicInsights: (topic: string) =>
    postJson<{ topic: string; status: 'running' }>(
      `/api/materials/${encodeURIComponent(topic)}/insights/regenerate`,
    ),
  getRegenerateInsightsStatus: (topic: string) =>
    request<RegenerateInsightsStatus>(
      `/api/materials/${encodeURIComponent(topic)}/insights/regenerate/status`,
    ),
  triggerGenerateSite: (projectId: string) =>
    postJson<{ project_id: string; status: 'running' }>(`/api/projects/${projectId}/generate_site`),
  getGenerateSiteStatus: (projectId: string) =>
    request<PresentationStatus>(`/api/projects/${projectId}/generate_site/status`),
  getSiteHistory: (projectId: string) =>
    request<PresentationHistoryList>(`/api/projects/${projectId}/site/history`),
  triggerGeneratePoster: (projectId: string, scope: 'overall' | 'day' = 'overall', day?: number) =>
    scope === 'day'
      ? postJson<{ project_id: string; day: number; status: 'running' }>(
          `/api/projects/${projectId}/generate_daily_poster`,
          { day },
        )
      : postJson<{ project_id: string; status: 'running' }>(`/api/projects/${projectId}/generate_poster`),
  getGeneratePosterStatus: (projectId: string, scope: 'overall' | 'day' = 'overall', day?: number) =>
    request<PresentationStatus>(
      scope === 'day'
        ? `/api/projects/${projectId}/generate_daily_poster/${day}/status`
        : `/api/projects/${projectId}/generate_poster/status`,
    ),
  getPosterPrompt: (projectId: string, scope: 'overall' | 'day' = 'overall', day?: number) =>
    request<PosterPromptDetail>(
      scope === 'day' ? `/api/projects/${projectId}/poster/day/${day}/prompt` : `/api/projects/${projectId}/poster/prompt`,
    ),
  getPosterHistory: (projectId: string, scope: 'overall' | 'day' = 'overall', day?: number) =>
    request<PresentationHistoryList>(
      scope === 'day'
        ? `/api/projects/${projectId}/poster/day/${day}/history`
        : `/api/projects/${projectId}/poster/history`,
    ),
  getPosterHistoryPrompt: (
    projectId: string,
    timestamp: string,
    scope: 'overall' | 'day' = 'overall',
    day?: number,
  ) =>
    request<PosterPromptDetail>(
      scope === 'day'
        ? `/api/projects/${projectId}/poster/day/${day}/history/${encodeURIComponent(timestamp)}/prompt`
        : `/api/projects/${projectId}/poster/history/${encodeURIComponent(timestamp)}/prompt`,
    ),
  getPosterAssets: (projectId: string) =>
    request<PosterAssets>(`/api/projects/${projectId}/poster/assets`),
  getModelConfig: () => request<ModelConfig>('/api/model_config'),
  setModel: (model: string | null, group: 'full' | 'light' = 'full') =>
    request<{ group: string; effective_model: string }>('/api/model_config/model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, group }),
    }),
  testModelKeys: (group: KeyPoolGroup, keyIndex?: number) =>
    postJson<ModelKeyTestResponse>('/api/model_config/test', { group, key_index: keyIndex ?? null }),
  addModelKey: (group: KeyPoolGroup, key: string, channel?: MimoChannel) =>
    postJson<{ key_count: number; current_key_index: number | null; keys: KeyStatusEntry[] }>(
      `/api/model_config/${group}/keys`,
      { key, channel: channel ?? null },
    ),
  deleteModelKey: (group: KeyPoolGroup, index: number) =>
    request<{ key_count: number; current_key_index: number | null; keys: KeyStatusEntry[] }>(
      `/api/model_config/${group}/keys/${index}`,
      { method: 'DELETE' },
    ),
  listFeishuBindings: () => request<FeishuBinding[]>('/api/feishu/bindings'),
  getFeishuHealth: () => request<FeishuHealth>('/api/feishu/health'),
  getFeishuPushLog: (limit?: number) =>
    request<PushLogEntry[]>(`/api/feishu/push_log${limit ? `?limit=${limit}` : ''}`),
  getFeishuHeartbeatHistory: (limit?: number) =>
    request<HeartbeatHistoryResponse>(`/api/feishu/heartbeat_history${limit ? `?limit=${limit}` : ''}`),
  getFeishuActivityStats: () => request<ActivityStat[]>('/api/feishu/activity_stats'),
  getPoiDetails: (projectId: string) =>
    request<PoiDetailFile[]>(`/api/projects/${projectId}/poi_details`),
  uploadOverviewPhoto: (projectId: string, day: number, poiName: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return postForm<PoiDetailFile>(
      `/api/projects/${projectId}/poi_photos/${day}/${encodeURIComponent(poiName)}/overview`,
      form,
    )
  },
  deleteOverviewPhoto: (projectId: string, day: number, poiName: string) =>
    request<PoiDetailFile>(
      `/api/projects/${projectId}/poi_photos/${day}/${encodeURIComponent(poiName)}/overview`,
      { method: 'DELETE' },
    ),
  overviewPhotoUrl: (projectId: string, day: number, poiName: string, version: string) =>
    `/api/projects/${projectId}/poi_photos/${day}/${encodeURIComponent(poiName)}/overview?v=${encodeURIComponent(version)}`,
  uploadSegmentPhoto: (
    projectId: string,
    day: number,
    poiName: string,
    routeName: string,
    order: number,
    file: File,
  ) => {
    const form = new FormData()
    form.append('file', file)
    const params = new URLSearchParams({ route_name: routeName, order: String(order) })
    return postForm<PoiDetailFile>(
      `/api/projects/${projectId}/poi_photos/${day}/${encodeURIComponent(poiName)}/segment?${params}`,
      form,
    )
  },
  deleteSegmentPhoto: (
    projectId: string,
    day: number,
    poiName: string,
    routeName: string,
    order: number,
  ) => {
    const params = new URLSearchParams({ route_name: routeName, order: String(order) })
    return request<PoiDetailFile>(
      `/api/projects/${projectId}/poi_photos/${day}/${encodeURIComponent(poiName)}/segment?${params}`,
      { method: 'DELETE' },
    )
  },
  segmentPhotoUrl: (
    projectId: string,
    day: number,
    poiName: string,
    routeName: string,
    order: number,
    version: string,
  ) => {
    const params = new URLSearchParams({ route_name: routeName, order: String(order), v: version })
    return `/api/projects/${projectId}/poi_photos/${day}/${encodeURIComponent(poiName)}/segment?${params}`
  },
  // ---------- 用户备注/annotation layer（docs/ARCHITECTURE.md第21节，阶段2后台管理） ----------
  listUserNotes: (projectId: string) =>
    request<UserNote[]>(`/api/projects/${projectId}/user_notes`),
  createUserNote: (projectId: string, input: UserNoteInput) =>
    postJson<UserNote>(`/api/projects/${projectId}/user_notes`, input),
  updateUserNote: (projectId: string, noteId: string, input: UserNoteInput) =>
    request<UserNote>(`/api/projects/${projectId}/user_notes/${noteId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }),
  deleteUserNote: (projectId: string, noteId: string) =>
    request<{ note_id: string; deleted: boolean }>(
      `/api/projects/${projectId}/user_notes/${noteId}`,
      { method: 'DELETE' },
    ),
  listReviewFindings: (projectId: string, includeClosed = true) =>
    request<ReviewFinding[]>(
      `/api/projects/${projectId}/review-findings${includeClosed ? '' : '?include_closed=false'}`,
    ),
  listReviewQueue: (projectId: string, includeClosed = true) =>
    request<ReviewQueueGroup[]>(
      `/api/projects/${projectId}/review-queue?include_closed=${includeClosed}`,
    ),
  getReviewState: (projectId: string) =>
    request<ReviewState>(`/api/projects/${projectId}/review-state`),
  getOptimizationCandidates: (projectId: string, threshold = 3) =>
    request<OptimizationCandidate[]>(
      `/api/projects/${projectId}/optimization-candidates?threshold=${threshold}`,
    ),
  getGlobalReviewFindings: (includeClosed = true) =>
    request<ReviewFinding[]>(`/api/review-index/findings?include_closed=${includeClosed}`),
  getGlobalOptimizationCandidates: (threshold = 3) =>
    request<OptimizationCandidate[]>(`/api/review-index/optimization-candidates?threshold=${threshold}`),
  listImprovementSignals: (projectId: string, decision?: ImprovementSignal['decision']) =>
    request<ImprovementSignal[]>(
      `/api/projects/${projectId}/improvement-signals${decision ? `?decision=${encodeURIComponent(decision)}` : ''}`,
    ),
  decideImprovementSignal: (projectId: string, signalId: string, input: { decision: ImprovementSignal['decision']; reason?: string; cause_key?: string; root_cause?: string; affected_layer?: string; comparison_pair_id?: string | null }) =>
    postJson<ImprovementSignal>(`/api/projects/${projectId}/improvement-signals/${signalId}/decision`, {
      ...input,
      actor_id: 'admin_user',
    }),
  updateOptimizationCandidate: (candidateId: string, input: { status: OptimizationCandidate['status']; owner?: string; notes?: string; verification_summary?: string; target_scope?: OptimizationCandidate['target_scope']; change_ref?: string; validation_pair_ids?: string[] }) =>
    request<{ candidate_id: string; status: OptimizationCandidate['status'] }>(
      `/api/review-index/optimization-candidates/${candidateId}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...input, actor_id: 'admin_user' }),
      },
    ),
  listOptimizationRegressionCases: (candidateId: string) =>
    request<OptimizationRegressionCase[]>(
      `/api/review-index/optimization-candidates/${candidateId}/regression-cases`,
    ),
  createOptimizationRegressionCase: (
    candidateId: string,
    input: {
      case_type: OptimizationRegressionCase['case_type']
      label: string
      project_id?: string
      finding_id?: string
      test_selector?: string
      scenario_id?: string
      expected_outcome?: string
      config?: Record<string, unknown>
    },
  ) => postJson<OptimizationRegressionCase>(
    `/api/review-index/optimization-candidates/${candidateId}/regression-cases`,
    { ...input, actor_id: 'admin_user' },
  ),
  listOptimizationRegressionRuns: (candidateId: string) =>
    request<OptimizationRegressionRun[]>(
      `/api/review-index/optimization-candidates/${candidateId}/regression-runs`,
    ),
  startOptimizationRegressionRun: (
    candidateId: string,
    input: { case_ids: string[]; advance_to_monitoring?: boolean },
  ) => postJson<OptimizationRegressionRun>(
    `/api/review-index/optimization-candidates/${candidateId}/regression-runs`,
    { ...input, actor_id: 'admin_user' },
  ),
  getOptimizationRegressionRun: (runId: string) =>
    request<OptimizationRegressionRun>(`/api/review-index/regression-runs/${runId}`),
  listReviewEvalScenarios: () => request<string[]>('/api/review-index/eval-scenarios'),
  getQualityMetrics: () => request<QualityMetrics>('/api/review-index/quality-metrics'),
  previewReviewCascade: (projectId: string, file: string, target: Record<string, unknown> = {}, operations: unknown[] = []) =>
    postJson<CascadePlan>(`/api/projects/${projectId}/review-cascade/preview`, { file, target, operations }),
  recordReviewVerification: (projectId: string, findingId: string, input: { verification_status: string; verification_method: string; disposition: string; actor_type: string; actor_id: string; recommendation_id?: string; notes?: string }) =>
    postJson<ReviewFinding>(`/api/projects/${projectId}/review-findings/${findingId}/verification`, input),
  getReviewItemDecisions: (projectId: string, findingId: string, recommendationId: string) =>
    request<ReviewItemDecision[]>(`/api/projects/${projectId}/review-findings/${findingId}/item-decisions?recommendation_id=${encodeURIComponent(recommendationId)}`),
  recordReviewItemDecision: (projectId: string, findingId: string, input: { recommendation_id: string; item_key: string; item_index: number; item_kind: 'claim' | 'legacy_suggestion'; item_label: string; disposition: string; edited_text?: string; edited_value?: unknown; has_edited_value?: boolean; notes?: string; actor_type?: string; actor_id?: string }) =>
    postJson<ReviewItemDecision>(`/api/projects/${projectId}/review-findings/${findingId}/item-decisions`, input),
  finalizeReviewItemDecisions: (projectId: string, findingId: string, recommendationId: string) =>
    postJson<ReviewDecisionFinalization>(`/api/projects/${projectId}/review-findings/${findingId}/finalize`, { recommendation_id: recommendationId, actor_id: 'admin_user' }),
  getReviewPatch: (projectId: string, patchId: string) =>
    request<ReviewPatch>(`/api/projects/${projectId}/review-patches/${patchId}`),
  applyReviewPatch: (projectId: string, patchId: string) =>
    postJson<ReviewPatch>(`/api/projects/${projectId}/review-patches/${patchId}/apply`, { actor_id: 'admin_user' }),
  rejectReviewPatch: (projectId: string, patchId: string, reason = '') =>
    postJson<ReviewPatch>(`/api/projects/${projectId}/review-patches/${patchId}/reject`, { actor_id: 'admin_user', reason }),
  rollbackReviewPatch: (projectId: string, patchId: string) =>
    postJson<ReviewPatch>(`/api/projects/${projectId}/review-patches/${patchId}/rollback`, { actor_id: 'admin_user' }),
  getReviewValidationPreview: (projectId: string) =>
    request<ReviewValidationScope>(`/api/projects/${projectId}/review-validation/preview`),
  startReviewValidation: (projectId: string) =>
    postJson<{ status: 'queued'; scope: ReviewValidationScope }>(`/api/projects/${projectId}/review-validation/start`),
  getReviewValidationStatus: (projectId: string) =>
    request<ReviewValidationStatus | null>(`/api/projects/${projectId}/review-validation/status`),
  generateReviewRecommendation: (projectId: string, findingId: string, input: { current_content: unknown; evidence: string[]; light: boolean }) =>
    postJson<{ recommendation: ReviewRecommendation; result: Record<string, unknown> }>(`/api/projects/${projectId}/review-findings/${findingId}/recommendation`, input),
  getReviewRecommendation: (projectId: string, findingId: string) =>
    request<ReviewRecommendation>(`/api/projects/${projectId}/review-findings/${findingId}/recommendation`),
  listReviewRecommendationHistory: (projectId: string, findingId: string) =>
    request<ReviewRecommendationHistoryEntry[]>(`/api/projects/${projectId}/review-findings/${findingId}/recommendations`),
  getReviewRecommendationById: (projectId: string, recommendationId: string) =>
    request<ReviewRecommendation>(`/api/projects/${projectId}/review-recommendations/${recommendationId}`),
  getReviewContext: (projectId: string, findingId: string) =>
    request<ReviewContext>(`/api/projects/${projectId}/review-findings/${findingId}/context`),
  applyManifestConsistencyRepair: (projectId: string, findingId: string, observedFingerprint: string) =>
    postJson<ManifestConsistencyRepairResult>(`/api/projects/${projectId}/review-findings/${findingId}/deterministic-repair`, {
      observed_fingerprint: observedFingerprint,
      actor_id: 'admin_user',
    }),
  listReviewExecutors: () => request<ReviewExecutor[]>('/api/review-executors'),
  createReviewRuns: (projectId: string, findingId: string, mode: 'standard' | 'codex' | 'compare') =>
    postJson<ReviewRun[]>(`/api/projects/${projectId}/review-findings/${findingId}/runs`, { mode }),
  listReviewRuns: (projectId: string, findingId: string) =>
    request<ReviewRun[]>(`/api/projects/${projectId}/review-findings/${findingId}/runs`),
  getReviewRunBundle: (projectId: string, runId: string) =>
    request<Record<string, unknown>>(`/api/projects/${projectId}/review-runs/${runId}/bundle`),
  importReviewRunResult: (projectId: string, runId: string, result: Record<string, unknown>) =>
    postJson<ReviewRun>(`/api/projects/${projectId}/review-runs/${runId}/result`, { result }),
  promoteReviewRun: (projectId: string, runId: string, confirmReplaceDecisions = false) =>
    postJson<ReviewFinding>(`/api/projects/${projectId}/review-runs/${runId}/promote`, { actor_id: 'admin_user', confirm_replace_decisions: confirmReplaceDecisions }),
  getReviewComparison: (projectId: string, comparisonGroupId: string) =>
    request<ReviewComparison>(`/api/projects/${projectId}/review-comparisons/${comparisonGroupId}`),
}

export { ApiError }
