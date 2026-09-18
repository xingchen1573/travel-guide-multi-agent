// 跟src/api/routers/projects.py、materials.py返回的JSON形状一一对应。

export interface MustVisitItem {
  name: string
  day: number | null
}

export interface StyleInterpretation {
  poi_density: string | null
  stay_duration: string | null
  needs_detail_bias: string | null
  flexible_time: string | null
}

export interface ProjectSummary {
  project_id: string
  topic: string | null
  destination: string | null
  days: number | null
  style: string | null
  budget: string | null
  notes: string | null
  // docs/ARCHITECTURE.md第18节：用户主导路线设定，只读展示。
  must_visit: MustVisitItem[]
  style_interpretation: StyleInterpretation | null
  state: string
  updated_at: string
  has_pending_conflicts: boolean
  pending_conflict_count?: number
  has_open_findings?: boolean
  has_run_failure: boolean
  latest_job_status?: ExecutionStatus | null
  latest_job_health_status?: ExecutionHealthStatus | null
  observed_run_status?: ExecutionStatus | null
  delivery_status?: string
  route_risk_status?: string
  poi_detail_expected?: number
  poi_detail_completed?: number
  completion_mode?: 'completed' | 'completed_with_degradation' | null
  force_finalized_count?: number
  review_findings_total?: number
  review_findings_open?: number
  review_findings_needs_rerun?: number
}

export interface ProjectDetail extends ProjectSummary {
  progress: {
    state: string
    agents: Record<string, string>
    skeleton_confirmed: boolean
    material_checked: boolean
    updated_at: string
  }
}

export interface IssueTypeStat {
  issue_type: string
  count: number
}

export interface SourceRef {
  platform: string
  author?: string
  doc_id?: string
  chunk_id?: string
  source_url?: string
  scope?: string
}

export interface ReferenceEntry extends SourceRef {
  cited_in?: string[]
  excerpt?: string
}

export interface FinalGuideWarning {
  day: number
  poi_name: string
  warning_type:
    | 'operational_uncertainty'
    | 'operational_restriction'
    | 'verification_incomplete'
  message: string
  evidence_refs: string[]
}

export interface FinalGuideMeta {
  version?: number
  merged_fields?: string[]
  unresolved_conflicts?: string[]
  checklist_included?: boolean
  references?: ReferenceEntry[]
  warnings?: FinalGuideWarning[]
  contract_validation?: Record<string, unknown>
  body_source?: 'legacy_markdown' | 'blocks_v1'
  blocks_schema_version?: string
  blocks_count?: number
  mapped_claim_count?: number
  updated_at?: string
}

export interface FinalGuideClaimSnapshot {
  claim_id: string
  source_path: string
  usage_key?: string
  claim_text: string
  claim_type?: string
  support_mode: RouteClaimSupportMode
  evidence_refs?: SourceRef[]
}

export interface FinalGuideBlock {
  block_id: string
  section: string
  markdown: string
  mapping_mode: 'upstream' | 'editorial' | 'user_constraint' | 'unverified'
  mapping_note?: string
  upstream_claim_ids?: string[]
  claim_snapshots?: FinalGuideClaimSnapshot[]
}

export interface FinalGuideBlocks {
  schema_version: string
  blocks: FinalGuideBlock[]
  rendered_body_sha256: string
  created_at?: string
  updated_at?: string
}

export interface RoutePoi {
  name: string
  brief?: string
  needs_detail?: boolean
  needs_detail_reason?: string
}

export type RouteClaimSupportMode =
  | 'direct'
  | 'amap'
  | 'derived'
  | 'estimate'
  | 'user_constraint'
  | 'unverified'

export interface EvidenceClaimEvidenceSnapshot {
  source: SourceRef
  excerpt?: string
  observed_at?: string
  source_kind?: string
}

export interface EvidenceClaim {
  claim_id: string
  target_path: string
  subject?: string
  claim_text: string
  claim_type?: string
  support_mode: RouteClaimSupportMode
  claimed_entities?: string[]
  evidence_refs?: SourceRef[]
  evidence_snapshots?: EvidenceClaimEvidenceSnapshot[]
  derived_from?: string[]
  provenance_mode?: 'live' | 'reconstructed'
}

export type RouteClaimEvidenceSnapshot = EvidenceClaimEvidenceSnapshot
export type RouteClaim = EvidenceClaim

export interface RouteDay {
  day: number
  city: string
  area?: string
  pois: RoutePoi[]
  notes?: string
  sources?: SourceRef[]
  claims?: RouteClaim[]
}

export interface RoutePlan {
  version: number
  days: RouteDay[]
  amap_verified: boolean
}

export interface RouteMapSelectedEntity {
  provider_id?: string
  provider_name?: string
  address?: string
  city?: string
  district?: string
}

export interface RouteMapEntity {
  original_name: string
  status: 'resolved' | 'ambiguous' | 'unresolved' | string
  confidence?: number
  selected?: RouteMapSelectedEntity | null
}

export interface RouteMapValidation {
  status: 'passed' | 'needs_confirmation' | 'failed' | string
  days: Array<{
    day: number
    entities?: RouteMapEntity[]
  }>
}

export interface LifestyleItem {
  day: number
  recommendation: string
  sources?: SourceRef[]
  claimed_entities?: string[]
  claims?: EvidenceClaim[]
}

export interface LifestyleOutput {
  version: number
  food: { items: LifestyleItem[] }
  outfit: { items: LifestyleItem[] }
  lodging: { items: LifestyleItem[] }
  play: { items: LifestyleItem[] }
}

export interface PitfallItem {
  field: string
  day: number
  target_recommendation: string
  warning?: string
  sources?: SourceRef[]
  claimed_entities?: string[]
  claims?: EvidenceClaim[]
}

export interface PitfallOutput {
  version: number
  food: { items: PitfallItem[] }
  outfit: { items: PitfallItem[] }
  lodging: { items: PitfallItem[] }
  play: { items: PitfallItem[] }
}

export type EvidenceCheckResult = 'pass' | 'fail' | 'inconclusive' | 'not_checked' | 'error'

export interface EvidenceCheckEvidence {
  chunk_id: string
  role: 'cited' | 'checker_supplement'
  platform?: string
  author?: string
  doc_id?: string
  source_url?: string
  excerpt?: string
}

export interface EvidenceCheck {
  target_type: string
  target_key: string
  target_text: string
  scope: 'entity_identity' | 'substantive_claim' | 'source_availability' | string
  result: EvidenceCheckResult
  method: string
  reason: string
  cited_sources?: SourceRef[]
  cited_chunk_ids?: string[]
  checked_chunk_ids?: string[]
  evidence?: EvidenceCheckEvidence[]
  missing_entities?: string[]
  checked_at?: string
  observation_mode?: 'live' | 'backfill_current_state' | string
  field?: string
  day?: number
  recommendation?: string
  warning?: string
  city?: string
  area?: string
  pois?: string[]
  claim_id?: string
  claim_text?: string
  claim_type?: string
  support_mode?: RouteClaimSupportMode | string
  target_path?: string
  verification_fingerprint?: string
}

export interface EvidenceChecks {
  version: number
  updated_at?: string
  route: EvidenceCheck[]
  lifestyle: EvidenceCheck[]
  pitfall: EvidenceCheck[]
  poi_detail: EvidenceCheck[]
}

export interface RouteFeasibilityEvidence {
  title?: string
  url?: string
  excerpt?: string
  source_type?: string
  observed_at?: string
}

export interface RouteFeasibilityCheck {
  check_id: string
  day: number
  target: string
  target_type: 'poi' | 'road' | 'schedule'
  category: string
  status: 'passed' | 'warning' | 'blocked' | 'unknown'
  summary?: string
  evidence?: RouteFeasibilityEvidence[]
  checked_at?: string
  technical_error?: string | null
}

export interface RouteFeasibilityReport {
  overall_status: 'passed' | 'warning' | 'blocked' | 'unknown'
  checks: RouteFeasibilityCheck[]
  force_finalized?: boolean
}

export interface ProjectOutputs {
  route_plan: RoutePlan | null
  route_map_validation?: RouteMapValidation | null
  route_feasibility?: RouteFeasibilityReport | null
  lifestyle: LifestyleOutput | null
  pitfall: PitfallOutput | null
  poi_details?: PoiDetailFile[] | null
  evidence_checks?: EvidenceChecks | null
  final_guide: string | null
  final_guide_meta?: FinalGuideMeta | null
  final_guide_blocks?: FinalGuideBlocks | null
}

export interface TimelineEvent {
  type: 'write' | 'conflict' | 'review_finding'
  timestamp: string
  actor?: string
  reported_by?: string
  action?: string
  target?: string | Record<string, unknown>
  summary?: string
  reason?: string
  // citation核验类冲突的结构化明细（route/lifestyle/pitfall三条核验循环才会传），
  // 每条字段视来源不同而不同（day/city/area 或 field/day/recommendation/warning），
  // 一律含missing_entities/reason。旧记录/非citation冲突没有这个字段，前端需回退到reason整句展示。
  issues?: Array<Record<string, unknown>>
  status?: 'pending' | 'resolved' | 'force_finalized' | 'open' | 'closed' | 'in_review'
  disposition?: string
  verification_status?: string
  finding_id?: string
  problem_type?: string
  retry_count?: number
  id?: string
  created_at?: string
}

export type FindingDisposition = 'resolved' | 'false_positive' | 'deferred' | 'needs_rerun'
export type FindingRootCause = 'content' | 'evidence' | 'data' | 'model' | 'code' | 'presentation' | 'policy'

export interface ReviewFinding {
  finding_id: string
  project_id: string
  baseline_version: string
  target: Record<string, unknown>
  problem_type: string
  disposition: FindingDisposition | string
  root_cause: FindingRootCause | string
  severity: string
  rerun_cost: string
  evidence_refs: string[]
  before: unknown
  after: unknown
  repair_action: string
  verification_method: string
  verification_status: string
  verification_actor_type: string
  verification_actor_id: string
  needs_rerun: boolean
  status: string
  patch_id: string | null
  recommendation_id: string | null
  occurrences: number
  revision: number
  notes: string
  created_at: string
  updated_at: string
  actor_id: string
  source_review_id: string | null
  issue_summary: string
  review_question: string
  observed_value: unknown
  canonical_key: string
  finding_kind: 'content_review' | 'state_residue' | 'consistency' | 'derived_mirror' | string
  observed_fingerprint: string
  resolved_fingerprint: string
  superseded_by: string | null
}

export interface ReviewQueueGroup {
  group_id: string
  primary_finding_id: string
  related_finding_ids: string[]
  title: string
  summary: string
  object_label: string
  finding_kind: string
  root_cause: string
  effective_status: string
  severity: string
  model_status: string
  decision_progress: { decided: number; total: number }
  mirror_count: number
  technical: Record<string, unknown>
}

export interface ReviewClaimEvidence {
  claim: string
  target_path?: string
  original_text: string
  evidence_refs: string[]
  evidence_excerpt: string
  verdict: string
  action: string
  content_action?: 'keep' | 'narrow' | 'rerun' | null
  evidence_action?: 'keep' | 'add_source' | 'repair_reference' | 'sync_status' | null
  proposed_text: string
  rationale: string
  confidence: string
}

export interface ReviewExecutor {
  executor_id: 'standard_model' | 'codex_skill' | string
  name: string
  description: string
  enabled: boolean
  execution_mode: 'server' | 'bundle_bridge'
  cost_level: 'low' | 'medium' | 'high' | 'unknown'
  supports_tools: boolean
  supports_web: boolean
}

export type ExecutionStatus = 'queued' | 'running' | 'validating' | 'waiting_user' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
export type ExecutionHealthStatus = 'healthy' | 'stalled' | 'orphaned'
export type ReviewRunStatus = ExecutionStatus | 'waiting_external'

export interface ReviewRun {
  run_id: string
  project_id: string
  finding_id: string
  executor_id: string
  comparison_group_id: string | null
  channel: string
  bundle_sha256: string
  baseline_file_sha256: string
  baseline_finding_revision: number
  status: ReviewRunStatus
  stage: string
  progress_message: string
  recommendation_id: string | null
  artifact_paths: Record<string, string>
  metrics: Record<string, unknown>
  error: string | null
  status_reason?: string | null
  health_status?: ExecutionHealthStatus
  owner_instance_id?: string | null
  last_heartbeat_at?: string | null
  last_progress_at?: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  promoted_at?: string | null
  superseded_at?: string | null
}

export interface ReviewProposedOperation {
  item_key: string
  operation: 'replace' | 'add' | 'remove'
  path: string
  proposed_value: unknown
  reason: string
  requires_human_confirmation: boolean
}

export interface ReviewComparison {
  comparison_group_id: string
  bundle_sha256: string
  same_baseline: boolean
  runs: ReviewRun[]
  recommendations: Record<string, ReviewRecommendation | null>
  items: Array<{
    target_path: string
    claims: Record<string, ReviewClaimEvidence>
    aligned: boolean
  }>
}

export interface ReviewRecommendation {
  recommendation_id: string
  finding_id: string
  decision: string
  confidence: string
  risk_level: string
  summary: string
  proposed_text: string
  claim_reviews?: ReviewClaimEvidence[]
  proposed_operations?: ReviewProposedOperation[]
  evidence_refs: string[]
  uncertainties: string[]
  recommended_action: string
  model_name: string
  model_group?: string
  model_provider?: string
  context_version?: string
  source_run_id?: string | null
  created_at: string
}

export interface ReviewPatchOperation {
  op: 'replace' | 'add' | 'remove'
  path: string
  value: unknown
  before: unknown
  before_exists: boolean
}

export interface ReviewPatch {
  patch_id: string
  project_id: string
  finding_id: string
  file: string
  base_sha256: string
  operations: ReviewPatchOperation[]
  status: string
  impact_class: string
  cascade_plan: Record<string, unknown>
}

export interface ReviewValidationScope {
  project_id: string
  patch_ids: string[]
  patch_count: number
  lifestyle_items: Array<[string, number]>
  pitfall_items: Array<[string, number]>
  poi_files: string[]
  route_days: number[]
  requires_integration: boolean
  route_structure_patch_ids: string[]
  unresolved_patch_ids: string[]
  can_start: boolean
}

export interface ReviewValidationStatus {
  task_type: string
  key: string
  status: ExecutionStatus
  health_status?: ExecutionHealthStatus
  status_reason?: string | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  log: string[]
  validation_status?: 'passed' | 'failed'
  issue_count?: number
  scope?: ReviewValidationScope
  site_output_path?: string
}

export interface ReviewDecisionFinalization {
  finding: ReviewFinding
  outcome: 'closed' | 'awaiting_apply' | 'deferred' | 'needs_rerun'
  patch_id: string | null
  effective_status: string
  message: string
}

export type ReviewItemDisposition = 'accepted' | 'keep_original' | 'edited' | 'deferred' | 'needs_rerun'

export interface ReviewItemDecision {
  decision_id: string
  project_id: string
  finding_id: string
  recommendation_id: string
  item_key: string
  item_index: number
  item_kind: 'claim' | 'legacy_suggestion'
  item_label: string
  disposition: ReviewItemDisposition
  edited_text: string
  edited_value: unknown
  has_edited_value: boolean
  notes: string
  actor_type: string
  actor_id: string
  created_at: string
}

export interface ReviewRecommendationHistoryEntry {
  recommendation: ReviewRecommendation
  is_current: boolean
  lifecycle_status: 'current' | 'superseded' | 'candidate'
  decision_count: number
  total_items: number
  run_id: string | null
  run_status: string | null
  executor_id: string | null
  promoted_at: string | null
  superseded_at: string | null
}

export interface ReviewEvidenceGroups {
  finding_refs: string[]
  current_references: Array<Record<string, unknown>>
  attached_sources: Array<Record<string, unknown>>
  source_excerpts: Array<Record<string, unknown>>
  verification_failures: Array<Record<string, unknown>>
  historical_reviews: Array<Record<string, unknown>>
}

export interface MissingEntityReviewContext {
  schema_version: 'missing-entity-review-v1'
  entity_count: number
  occurrence_count: number
  raw_missing_entity_count: number
  stale_evidence_state: boolean
  stale_entity_count: number
  verification_state: 'current_failure' | 'stale_failure' | 'unassessed'
  stale_reason: string
  project_impact: {
    finding_status: string
    project_phase: string
    citation_status: string
    review_required: boolean
    review_queue_counted: boolean
    generation_blocked: boolean
    affects_project_state: boolean
    final_guide_affected: boolean
    final_guide_occurrence_count: number
    final_guide_entity_mention_count: number
    affected_files: string[]
    note: string
  }
  entities: Array<{
    entity: string
    occurrence_count: number
    occurrences: Array<{
      json_pointer: string
      field_label: string
      subject: string
      current_value: unknown
      excerpt: string
    }>
    current_sources: Array<{
      ref: string
      platform: string
      author: string
      source_url: string
      excerpts: string[]
      origin: string
      attachment_scope: 'review_scope'
      relation: 'mentions_entity' | 'no_exact_mention' | 'unreadable'
    }>
    candidate_evidence: Array<{
      ref: string
      text: string
      retrieval_method: 'exact_entity+bm25' | 'exact_entity'
      retrieval_score: number
      already_attached: boolean
    }>
    downstream_evidence: Array<{
      file: string
      poi_name: string
      day: number | null
      claim_id: string
      claim_text: string
      json_pointer: string | null
      current_value: unknown
      validation_status: 'passed' | 'failed' | 'unassessed'
      passed_scope_count: number
      failed_scope_count: number
      checked_at: string
      evidence_excerpts: Array<{
        ref: string
        text: string
        mentions_entity: boolean
        origin: 'embedded_snapshot' | 'material_library'
      }>
    }>
    final_guide_occurrences: Array<{
      file: 'final_guide.md'
      line: number
      excerpt: string
      match_mode: 'claim_mapping' | 'text_match'
      claim_linked: boolean
      block_id: string | null
      section: string
      upstream_claim_ids: string[]
    }>
    stale_evidence_state: boolean
    verification_state: 'current_failure' | 'stale_failure' | 'unassessed'
    content_updated_at: string
    latest_checked_at: string
    historical_failures: Array<{
      claim_id: string
      checked_claim_text: string
      target_path: string
      json_pointer: string | null
      scope: string
      reason: string
      checked_at: string
      checked_before_content_update: boolean
      current_claim_present: boolean
      match_status: 'same_field_stale_claim' | 'orphaned_claim'
    }>
    resolution_state: 'verified_downstream' | 'candidate_found' | 'needs_evidence' | 'no_longer_present'
    recommendation: string
  }>
}

export interface ManifestConsistencyContext {
  schema_version: 'manifest-consistency-v1'
  mismatch_kind: 'status' | 'missing_detail' | 'unresolved_target'
  manifest_index?: number
  day?: number
  poi_name?: string
  detail_file?: string
  manifest?: { status: string; updated_at: string; json_pointer: string }
  detail?: { status: string; updated_at: string; file: string }
  terminal_result?: { status: string; source: string; observed_at: string } | null
  validation_receipt?: { status: string; checked_at: string; artifact_sha256: string } | null
  current_claim_count?: number
  current_check_count?: number
  current_blocker_count?: number
  current_blockers?: Array<{
    claim_id: string
    claim_text: string
    target_path: string
    scope: string
    result: string
    reason: string
    checked_at: string
    evidence: Array<{ ref: string; excerpt: string }>
  }>
  authoritative_status?: string | null
  authority_basis?: string
  authority_reason?: string
  confidence?: string
  recommended_action?: string
  action_target?: string
  executable: boolean
  blocking_reason: string
  observed_fingerprint: string
  project_impact?: {
    needs_rerun: boolean
    generation_blocked: boolean
    final_guide_mention_count: number
    will_refresh: string[]
    note: string
  }
}

export interface ManifestConsistencyRepairResult {
  status: 'completed'
  changed: boolean
  action: string
  authoritative_status: string
  finding: ReviewFinding
  validation: Record<string, unknown>
  site_output_path: string
}

export interface ReviewContext {
  current_content: unknown
  evidence: Array<Record<string, unknown> | string>
  evidence_groups: ReviewEvidenceGroups
  missing_entity_context: MissingEntityReviewContext | null
  manifest_consistency_context: ManifestConsistencyContext | null
  verification_timeline: {
    source: 'retry_history_read_projection'
    source_hash: string
    round_count: number
    progression: Array<number | null>
    current_blocker_count: number | null
    current_claim_count: number | null
    current_source: 'latest_evidence_checks' | 'unassessed'
    rounds: Array<{
      history_index: number
      round: number
      issue_count: number | null
      raw_detail: string
      is_latest: boolean
      superseded: boolean
    }>
    other_events: Array<{
      history_index: number
      label: string
      raw_detail: string
    }>
  }
  origin_claims: Array<{
    agent_label: string
    day: number | null
    subject: string
    field_label: string
    claim_id: string
    claim_text: string
    claim_type: string
    support_mode: string
    target_path: string
    json_pointer: string | null
    current_value: unknown
    route_name: string
    point_name: string
    order: number | null
    issue_group_key: string
    claimed_entities: string[]
    missing_entities: string[]
    checks: Array<{
      scope: string
      result: string
      method: string
      reason: string
      missing_entities: string[]
      cited_source_count: number
      checked_at: string
    }>
    finding_scope_label: string
  }>
  review_item_contexts: Array<{
    item_key: string
    item_kind: 'claim' | 'legacy_suggestion'
    label: string
    target: {
      file: string | null
      json_pointer: string | null
      route_name: string | null
      point_name: string | null
    }
    current_value: unknown
    evidence_refs: string[]
    evidence_excerpts: Array<{ ref: string; text: string }>
    unresolved_evidence_refs: string[]
    evidence_origin: 'current_json_sources' | 'model_review'
    support_status: 'supported' | 'partial' | 'unsupported' | 'contradicted' | 'ambiguous' | 'unknown'
    support_basis: 'current_model_review' | 'historical_failure' | 'unassessed'
    related_evidence_candidates: Array<{ ref: string; text: string; matched_terms: string[] }>
    library_evidence_candidates: Array<{
      ref: string
      text: string
      matched_terms: string[]
      retrieval_method: 'bm25'
      retrieval_score: number
      same_document: boolean
    }>
    alternative_evidence_found: boolean
    model_evidence_excerpt: string
    failure_points: Array<{ round: number; detail: string }>
    coverage: 'ready' | 'partial' | 'insufficient' | 'unmapped'
    ready_for_human_review: boolean
  }>
  summary: Record<string, unknown>
}

export interface ReviewState {
  project_id: string
  ok: boolean
  force_finalized_paths: string[]
  contract_violation_paths: string[]
  manifest_mismatches: string[]
  checked_at: string
}

export interface OptimizationCandidate {
  candidate_id: string
  project_id?: string
  cause_key: string
  title: string
  symptom_summary: string
  root_cause: string
  affected_layer: string
  affected_layers: string[]
  problem_type: string
  finding_ids: string[]
  signal_ids: string[]
  project_ids?: string[]
  source_pair_ids: string[]
  occurrence_count: number
  active_count: number
  resolved_count: number
  false_positive_count: number
  deferred_count: number
  confidence: 'low' | 'medium' | 'high'
  severity: 'low' | 'medium' | 'high'
  status: 'new' | 'triaged' | 'planned' | 'implementing' | 'monitoring' | 'verified' | 'dismissed' | 'deferred'
  owner: string
  notes: string
  verification_summary: string
  target_scope: 'undecided' | 'shared_kernel' | 'langgraph_harness' | 'codex_skill_harness' | 'multiple'
  change_ref: string
  validation_pair_ids: string[]
  first_seen_at: string
  last_seen_at: string
  created_at: string
  migration_required: boolean
  suggested_fix: string
  suggested_test: string
  suggested_actions: string[]
  regression_plan: {
    original_findings: string[]
    cross_project_case_required: boolean
    counterexample_required: boolean
    suggested_tests: string[]
    fixed_eval_scenario_required: boolean
  }
  representative_signals: ImprovementSignal[]
}

export interface OptimizationRegressionCase {
  case_id: string
  candidate_id: string
  case_type: 'original_finding' | 'cross_project_finding' | 'unit_test' | 'counterexample' | 'fixed_eval'
  label: string
  project_id: string
  finding_id: string
  test_selector: string
  scenario_id: string
  expected_outcome: string
  config: Record<string, unknown>
  status: 'draft' | 'ready' | 'disabled'
  created_by: string
  created_at: string
  updated_at: string
}

export interface OptimizationRegressionCaseResult {
  case_id: string
  case_type: string
  label: string
  status: 'passed' | 'failed' | 'blocked' | 'skipped'
  summary: string
  duration_seconds: number
  details: Record<string, unknown>
}

export interface OptimizationRegressionRun {
  run_id: string
  candidate_id: string
  case_ids: string[]
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  stage: string
  progress_message: string
  results: OptimizationRegressionCaseResult[]
  prompt_fingerprint: Record<string, unknown>
  code_fingerprint: string
  artifact_paths: Record<string, string>
  advance_to_monitoring: boolean
  created_by: string
  error: string
  created_at: string
  started_at: string
  completed_at: string
}

export interface ImprovementSignal {
  signal_id: string
  project_id: string
  finding_id: string
  recommendation_id: string
  review_run_id: string
  source_type: string
  comparison_pair_id: string | null
  cause_key: string
  root_cause: string
  affected_layer: string
  problem_type: string
  hypothesis: string
  evidence_refs: string[]
  representative_targets: string[]
  suggested_fix: string
  suggested_test: string
  severity: 'low' | 'medium' | 'high'
  confidence: 'low' | 'medium' | 'high'
  migration_required: boolean
  regression_scope: string[]
  decision: 'pending' | 'accepted' | 'dismissed' | 'deferred' | 'merged' | 'reclassified'
  decision_reason: string
  decided_by: string
  baseline: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface QualityMetrics {
  project_count: number
  finding_count: number
  active_finding_count: number
  pending_signal_count: number
  accepted_signal_count: number
  dismissed_signal_count: number
  candidate_count: number
  monitoring_candidate_count: number
  verified_candidate_count: number
  accepted_signals_per_100_projects: number
  generated_at: string
}

export interface ExperimentEvidenceValue<T = number | string | boolean | string[]> {
  value: T | null
  value_kind: 'observed' | 'derived' | 'audited' | 'unknown'
  source_paths: string[]
  calculation_method: string | null
  missing_reason: string | null
}

export interface ExperimentRelation {
  source_project_id?: string
  target_project_id: string
  relation_type: 'rerun_of' | 'checkpoint_resume_of' | 'comparison_peer' | 'validates' | 'supersedes' | 'source_input'
  note?: string | null
}

export interface ExperimentSummary {
  project_id: string
  schema_version: string
  metrics_schema_version: string
  scenario_id: string
  experiment_version: string | null
  harness: 'langgraph' | 'codex_skill'
  destination: string | null
  trip_days: number | null
  route_sha: string | null
  model: string | null
  provider: string | null
  project_status: string | null
  overall_status?: string | null
  completion_mode: string | null
  started_at: string | null
  completed_at: string | null
  wall_clock_seconds: number | null
  comparison_grade: string | null
  source_fingerprint: string
  indexed_at: string
  relations: ExperimentRelation[]
}

export interface ComparisonPairCheck {
  status: 'match' | 'different' | 'unknown'
  codex: string | boolean | null
  langgraph: string | boolean | null
}

export interface ComparisonPairMember {
  project_id: string
  experiment_version: string | null
  harness: 'langgraph' | 'codex_skill'
  destination: string | null
  model: string | null
  provider: string | null
  project_status: string | null
  overall_status?: string | null
  completion_mode: string | null
  comparison_grade: string | null
}

export interface ComparisonPairFindingPreview {
  finding_id: string
  problem_type: string
  root_cause: string
  severity: 'low' | 'medium' | 'high' | string
  status: string
  summary: string
}

export interface ComparisonPairFindingSnapshot {
  project_id: string
  active_finding_count: number
  severity_counts: Record<'high' | 'medium' | 'low', number>
  problem_type_counts: Record<string, number>
  preview_findings: ComparisonPairFindingPreview[]
}

export interface ComparisonPairStageObservation {
  key: string
  label: string
  langgraph_status: string
  codex_status: string
  kind: 'different' | 'shared_attention'
}

export interface ComparisonPair {
  pair_id: string
  codex_project_id: string
  langgraph_project_id: string
  source_input_project_id: string | null
  comparison_scope: 'system' | 'harness_isolated'
  declared_grade: 'A' | 'B' | 'C'
  effective_grade: 'A' | 'B' | 'C'
  status: 'complete' | 'incomplete' | 'invalid'
  origin: string
  note: string | null
  evidence_refs: string[]
  comparability: {
    verdict: 'harness_isolated' | 'system_comparable' | 'incomplete' | 'not_comparable'
    checks: Record<'requirements' | 'route' | 'materials' | 'model' | 'provider', ComparisonPairCheck>
    reason_codes: string[]
  }
  indexed_at: string
  codex_experiment: ComparisonPairMember | null
  langgraph_experiment: ComparisonPairMember | null
  observed_issues: {
    langgraph: ComparisonPairFindingSnapshot
    codex: ComparisonPairFindingSnapshot
    active_finding_count: number
    stage_observation_count: number
    stage_observations: ComparisonPairStageObservation[]
  }
  source_candidates: OptimizationCandidate[]
  validation_candidates: OptimizationCandidate[]
}

export interface ExperimentMetrics {
  identity: {
    schema_version: string
    is_experiment: boolean
    project_id: string
    scenario_id: string
    experiment_version: string | null
    harness: 'langgraph' | 'codex_skill'
    destination: string | null
    trip_days: number | null
    route_sha: string | null
    requirements_sha: string | null
    project_status: string | null
    completion_mode: string | null
    comparison_grade: string | null
    model: string | null
    provider: string | null
    inherited_downstream_outputs: boolean | null
    extraction_time: string | null
  }
  outputs: Record<string, number | boolean>
  quality_contract: Record<string, number>
  route_gate: Record<string, unknown>
  status?: {
    overall: string
    delivery: string
    route_risk: string
    poi_completeness?: string
    recovery: string
    protocol: string
    attention?: string[]
  }
  execution: Record<string, ExperimentEvidenceValue>
  tool_usage: Record<string, ExperimentEvidenceValue>
  context: Record<string, ExperimentEvidenceValue>
  stages: Array<{ key: string; label: string; status: string; evidence_paths: string[] }>
  poi_details?: Array<{
    day: number
    poi_name: string
    generation_status: string
    citation_status: string
    content_depth: 'missing' | 'empty' | 'route_outline' | 'executable_route' | 'recommended_route'
    route_count: number
    recommended_route_count: number
    segment_count: number
    claim_count: number
  }>
  evidence_paths: string[]
  relations: ExperimentRelation[]
  missing_metrics: Array<{ group: string; field: string; reason: string }>
  source_fingerprint: string
}

export interface ExperimentDetail {
  metrics: ExperimentMetrics
  relations: ExperimentRelation[]
  comparison_pair_ids: string[]
  optimization_candidates: OptimizationCandidate[]
  indexed_at: string
}

export interface ExperimentReindexResult {
  added: number
  updated: number
  skipped: number
  failed: number
  indexed: number
  failures: Array<{ project_id: string; error: string }>
  relation_failures: Array<{ project_id: string; error: string; relation: unknown }>
  pairs: { registered: number; complete: number; incomplete: number; invalid: number }
  database_path: string
  indexed_at: string
}

export interface CascadePlan {
  project_id: string
  file: string
  impact_class: string
  risk_level: string
  target_days: number[]
  needs_human_approval: boolean
  actions: Array<{ action: string; label: string; scope: string; calls_llm: boolean; required: boolean }>
  explanation: string
}

export interface RerunResponse {
  project_id: string
  status: 'running'
}

export interface RerunStatus {
  status: ExecutionStatus
  health_status?: ExecutionHealthStatus
  status_reason?: string | null
  error: string | null
  started_at: string | null
  finished_at: string | null
}

// ---------- 回收站：Project/素材软删除+恢复+永久删除 ----------

export interface DeleteProjectResponse {
  project_id: string
  status: 'trashed'
}

export interface TrashedProjectSummary {
  project_id: string
  topic: string | null
  destination: string | null
  days: number | null
  state: string | null
  updated_at: string | null
}

export interface TrashActionResponse {
  project_id: string
  status: 'restored' | 'permanently_deleted'
}

export interface DeleteMaterialResponse {
  resource_type: 'material'
  topic: string
  doc_id: string
  title: string
  platform: string
  source_file: string
  chunk_count: number
  deleted_at: string
  status: 'trashed'
}

export interface TrashedMaterialSummary {
  resource_type: 'material'
  topic: string
  doc_id: string
  title: string
  platform: string
  source_file: string
  chunk_count: number
  deleted_at: string
}

export interface MaterialTrashActionResponse {
  topic: string
  doc_id: string
  status: 'restored' | 'permanently_deleted'
}

export interface DeleteMaterialTopicResponse {
  resource_type: 'material_topic'
  topic: string
  entry_count: number
  chunk_count: number
  raw_file_count: number
  trashed_material_count: number
  active_project_ids: string[]
  trashed_project_ids: string[]
  deleted_at: string
  status: 'trashed'
}

export interface TrashedMaterialTopicSummary {
  resource_type: 'material_topic'
  topic: string
  entry_count: number
  chunk_count: number
  raw_file_count: number
  trashed_material_count: number
  active_project_ids: string[]
  trashed_project_ids: string[]
  deleted_at: string
}

export interface MaterialTopicTrashActionResponse {
  topic: string
  status: 'restored' | 'permanently_deleted'
}

export interface MaterialTopicSummary {
  topic: string
  entry_count: number
  core_count: number
  chunk_count: number
  total_word_count: number
  active_project_ids?: string[]
  trashed_project_ids?: string[]
}

export interface MaterialEntry {
  doc_id: string
  topic?: string
  source_file?: string
  platform: string
  author?: string
  source_url?: string
  publish_date?: string
  title: string
  is_core: boolean
  entities: string[]
}

export interface MaterialChunk {
  chunk_id: string
  doc_id: string
  order: number
  text: string
}

export interface MaterialEntryDetail {
  entry: MaterialEntry
  chunks: MaterialChunk[]
  raw_text: string | null
}

export interface CollectStatus {
  task_type: string
  key: string
  status: ExecutionStatus
  health_status?: ExecutionHealthStatus
  status_reason?: string | null
  error: string | null
  started_at: string
  finished_at: string | null
  log: string[]
  result_summary?: string
}

// 采集时可显式限定平台（"all"/"bilibili"/"douyin"），后端工具层硬过滤，不是agent自己
// 从request文字猜（见管理后台采集对话框的显式平台选择器）。
export type CollectPlatform = 'all' | 'bilibili' | 'douyin'

// 跨全库统计某个实体一共被多少条素材提到过（GET /api/materials/{topic}/entity_stats）。
export interface EntityStat {
  entity: string
  count: number
  doc_ids: string[]
}

// 素材库统计画像（GET /api/materials/{topic}/stats，2026-08-22 grilling讨论新增）。
// word_count均为raw原文字符数口径，不含导读、不含chunk重叠部分，见后端注释。
export interface PlatformLongestEntry {
  doc_id: string
  title: string
  word_count: number
}

export interface PlatformBreakdownItem {
  platform: string
  count: number
  word_count: number
  percentage: number
  longest: PlatformLongestEntry | null
}

// bucket固定5档且顺序固定："近3个月"/"近半年"/"近1年"/"1年以上"/"未知"。by_platform
// 只在"未知"档展示——小红书采集路径完全没有publish_date来源，笼统合并成一个数字
// 会掩盖"这是整平台性缺口而非随机缺失"这件事。
export interface AgeDistributionBucket {
  bucket: string
  count: number
  by_platform: Record<string, number>
}

export interface CoreRatio {
  core_count: number
  non_core_count: number
}

export interface AuthorRankingEntry {
  author: string
  count: number
}

export interface AuthorRanking {
  top: AuthorRankingEntry[]
  other_author_count: number
  other_entry_count: number
}

export interface RejectionReasonEntry {
  source_file: string
  title: string
  reason: string
}

// too_short：ingest.py固定模板字符串判定，结构上可靠；irrelevant：LLM自由文本理由，
// 不做二次归类（关键词猜测容易分错）。
export interface RejectionStats {
  too_short_count: number
  irrelevant_count: number
  irrelevant_reasons: RejectionReasonEntry[]
}

export interface MaterialStats {
  topic: string
  entry_count: number
  total_word_count: number
  platform_breakdown: PlatformBreakdownItem[]
  age_distribution: AgeDistributionBucket[]
  core_ratio: CoreRatio
  author_ranking: AuthorRanking
  rejection_stats: RejectionStats
}

// LLM从素材库整体提炼的跨来源共识（GET /api/materials/{topic}/insights），见
// src/materials/insights.py。confidence: consensus=至少2个不同author提到，
// single_source=仅单一来源，前端需要做降级标注而非隐藏。
export interface InsightSourceRef {
  doc_id: string
  chunk_id: string
  author: string
  excerpt: string
}

export interface InsightItem {
  summary: string
  confidence: 'consensus' | 'single_source'
  sources: InsightSourceRef[]
}

export interface TopicInsights {
  topic: string
  generated_at: string
  model: string
  based_on_entry_count: number
  must_visit: InsightItem[]
  food: InsightItem[]
  pitfalls: InsightItem[]
  tips: InsightItem[]
  other: InsightItem[]
}

// 重新生成洞察（POST /api/materials/{topic}/insights/regenerate，2026-08-01改成
// 后台任务，跟重跑/采集/生成网站同款模式——之前是同步阻塞请求，用户切走页面再切回来
// 承载"生成中"状态的组件已经卸载，看起来像是"中断了"，改成后台任务后状态落在服务端，
// 轮询GET .../regenerate/status可查到真实进度）。
export interface RegenerateInsightsStatus {
  status: ExecutionStatus
  health_status?: ExecutionHealthStatus
  status_reason?: string | null
  error: string | null
  started_at: string
  finished_at: string | null
  log: string[]
  result?: TopicInsights
}


// ---------- 机制B：Agent运行状态 ----------

export interface RunStatus {
  node: string
  status: 'running' | 'waiting_user' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  health_status?: ExecutionHealthStatus
  status_reason?: string | null
  call_count: number
  started_at: string
  finished_at: string | null
  error: string | null
}

// ---------- 呈现层产出触发 ----------

export interface PresentationStatus {
  task_type: string
  key: string
  status: ExecutionStatus
  health_status?: ExecutionHealthStatus
  status_reason?: string | null
  error: string | null
  started_at: string
  finished_at: string | null
  log: string[]
  output_path?: string
}

// 版本历史（2026-07-26新增）：生成网站/海报前旧产物会被归档到
// workspace/history/<kind>/<timestamp>/，时间戳字符串本身就是排序键（新到旧）。
export type PresentationHistoryList = string[]

export interface PosterAssetEntry {
  available: boolean
  source: 'generated' | 'imported' | null
  path: string | null
}

export interface PosterAssets {
  overall: PosterAssetEntry
  days: Record<string, PosterAssetEntry>
}

export interface PosterPromptDetail {
  prompt: string
}

// ---------- 机制C：模型/Key运行时配置（full/light两组独立轮询+运行时覆盖） ----------

export type KeyPoolGroup = 'full' | 'light' | 'mimo' | 'cunai' | 'tavily' | 'poster'
export type MimoChannel = 'gptload' | 'token_plan' | 'legacy'

export interface KeyStatusEntry {
  index: number
  masked: string
  is_current: boolean
}

// full/light：支持模型名运行时切换的分组
export interface ModelGroupStatus {
  group: 'full' | 'light'
  label: string
  usage: string
  default_model: string
  effective_model: string
  override_active: boolean
  key_count: number
  current_key_index: number | null
  keys: KeyStatusEntry[]
}

// mimo：有key池CRUD，但没有模型名运行时切换（见docs/ARCHITECTURE.md第13.2节）
export interface FixedModelGroupStatus {
  group: 'mimo' | 'cunai'
  label: string
  usage: string
  default_model: string
  available_models: string[]
  key_count: number
  current_key_index: number | null
  keys: KeyStatusEntry[]
}

// tavily/poster：纯key池，连"模型"这个概念都不存在
export interface KeyPoolStatus {
  group: 'tavily' | 'poster'
  label: string
  usage: string
  key_count: number
  current_key_index: number | null
  keys: KeyStatusEntry[]
}

export interface OtherServiceKey {
  name: string
  usage: string
  configured: boolean
  key_count: number
  masked_keys: string[]
}

export interface PrimaryTravelModelStatus {
  label: string
  provider_group: 'gptload' | 'jiji'
  provider: string
  base_url: string
  model: string
  usage: string
  configured: boolean
  key_count: number
}

export interface ModelConfig {
  primary_travel: PrimaryTravelModelStatus
  groups: {
    full: ModelGroupStatus
    light: ModelGroupStatus
    cunai: FixedModelGroupStatus
    mimo: FixedModelGroupStatus
  }
  key_pools: {
    tavily: KeyPoolStatus
    poster: KeyPoolStatus
  }
  other_services: OtherServiceKey[]
}

// 手动可用性检测（POST /api/model_config/test，2026-07-26新增）：不测就不知道key/模型
// 现在真的能不能用，只在用户手动点击"测试"时才发起真实调用（有真实token成本）。
// 2026-08-01扩展到mimo/tavily/poster（poster不发起真实生成，ok为null表示"未测试"）。
export interface ModelKeyTestResult {
  index: number
  masked: string
  ok: boolean | null
  latency_ms: number | null
  error: string | null
  response_snippet: string | null
}

export interface ModelKeyTestResponse {
  group: KeyPoolGroup
  model: string | null
  results: ModelKeyTestResult[]
}

// ---------- 飞书运维可视化（群↔project绑定、worker心跳健康状态，2026-07-26新增） ----------

export interface FeishuBinding {
  chat_id: string
  project_id: string
  history: string[]
  awaiting: string | null
  project_exists: boolean
  topic: string | null
  destination: string | null
  state: string | null
  updated_at: string | null
}

export type FeishuHealthStatus = 'healthy' | 'stale' | 'not_running'

export interface FeishuHealth {
  status: FeishuHealthStatus
  pid: number | null
  started_at: string | null
  last_heartbeat: string | null
  seconds_since_heartbeat: number | null
}

// ---------- 推送日志/心跳历史/活跃度统计（2026-08-01新增，补上"仍是占位未做"的缺口） ----------

export interface PushLogEntry {
  timestamp: string
  chat_id: string
  project_id: string | null
  push_type: string
  success: boolean
  error: string | null
}

export interface HeartbeatHistoryEntry {
  timestamp: string
  pid: number
}

export interface AnomalyPeriod {
  started_at: string
  ended_at: string
  gap_seconds: number
}

export interface HeartbeatHistoryResponse {
  history: HeartbeatHistoryEntry[]
  anomalies: AnomalyPeriod[]
}

export interface ActivityStat {
  project_id: string | null
  chat_id: string
  push_count: number
  success_count: number
  failure_count: number
  last_push_at: string
}

// ---------- POI图片：管理后台人工上传真实照片（GET /api/projects/{id}/poi_details，
// 见src/api/routers/poi_photos.py），对应src/workspace/schema.py的PoiDetailFile ----------

export interface RouteSegment {
  order: number
  point_name: string
  highlight: string
  suggested_duration: string
  image_prompt: string
  sources: SourceRef[]
  claims?: EvidenceClaim[]
  uploaded_photo_path: string
}

export interface RouteOption {
  route_name: string
  suitable_for: string
  recommended: boolean
  recommend_reason: string | null
  route_segments: RouteSegment[]
}

export interface PoiDetailFile {
  poi_name: string
  day: number
  routes: RouteOption[]
  needs_detail_reason: string
  citation_status: string
  auto_collected: Record<string, unknown>[]
  retry_history: string[]
  uploaded_overview_photo_path: string
  created_at: string
  updated_at: string
}


// ---------- 用户备注/annotation layer（docs/ARCHITECTURE.md第21节，阶段2后台管理） ----------
// 对应src/workspace/schema.py的UserNote。day/poi_name两级可选绑定：只填day=天级；
// 都填=景点级；都缺（day=null, poi_name=""）=行程级（渲染到独立备注区）。

export interface UserNote {
  note_id: string
  day: number | null
  poi_name: string
  note_text: string
  source: 'web' | 'feishu' | 'admin'
  created_at: string
  superseded_by: string | null
  deleted: boolean
}

export interface UserNoteInput {
  note_text: string
  day: number | null
  poi_name: string
}
