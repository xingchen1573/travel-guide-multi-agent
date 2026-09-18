# Result Schema

Return one JSON object using `travel-review-result-v1`.

```json
{
  "schema_version": "travel-review-result-v1",
  "bundle_sha256": "<bundle hash>",
  "recommendation": {
    "recommendation_id": "recommendation-draft",
    "finding_id": "<finding id>",
    "decision": "human_review",
    "confidence": "medium",
    "risk_level": "medium",
    "summary": "",
    "proposed_text": "",
    "claim_reviews": [
      {
        "claim": "",
        "target_path": "",
        "original_text": "<copy scope.original_text exactly>",
        "evidence_refs": ["<actually read ref>"],
        "evidence_excerpt": "<short source excerpt>",
        "verdict": "partial",
        "action": "narrow",
        "content_action": "narrow",
        "evidence_action": "repair_reference",
        "proposed_text": "<exact post-change text; use 保持原文 when content_action=keep>",
        "rationale": "<how the evidence supports the decision>",
        "confidence": "high"
      }
    ],
    "evidence_refs": [],
    "uncertainties": [],
    "recommended_action": "human_review",
    "model_name": "",
    "model_group": "",
    "model_provider": "",
    "context_version": "travel-review-skill-v1",
    "created_at": ""
  },
  "system_observations": [
    {
      "root_cause": "evidence",
      "problem_type": "reference_attachment_mismatch",
      "cause_key": "citation:reference_attachment_mismatch",
      "affected_layer": "evidence_pipeline",
      "summary": "当前引用失配后没有继续检索同目的地候选素材。",
      "evidence_refs": ["<actually read ref>"],
      "representative_targets": ["/routes/0/route_segments/0"],
      "suggested_fix": "在判定不支持前检索同文档相邻 chunk 和目的地候选素材。",
      "suggested_test": "当前引用失配但候选素材命中时应建议 repair_reference。",
      "severity": "high",
      "confidence": "high",
      "migration_required": false,
      "regression_scope": ["original_finding", "cross_project_case", "counterexample"],
      "is_systemic": true
    }
  ],
  "proposed_operations": [],
  "review_trace": {
    "mode": "direct_repo",
    "status": "completed",
    "model_name": "",
    "model_group": "",
    "model_provider": "",
    "started_at": "",
    "completed_at": "",
    "evidence_reads": [
      {
        "ref": "",
        "excerpt": "",
        "source_type": "official"
      }
    ],
    "tool_calls": [],
    "errors": []
  },
  "validation": {}
}
```

`action` is the backward-compatible primary action. New results must also split the decision into:

- `content_action`: `keep`, `narrow`, or `rerun`.
- `evidence_action`: `keep`, `add_source`, `repair_reference`, or `sync_status`.

A claim may need both dimensions, for example `content_action=narrow` and
`evidence_action=repair_reference`. This means the useful core remains, while both wording and
the attached source need a local correction. Do not collapse this into “the whole content is wrong.”

`proposed_text` must describe the exact post-change body text, not a meta instruction such as
“consider shortening this paragraph.” When `content_action=keep`, use `保持原文` and do not ask to
delete, cancel, narrow, or rewrite any wording or duration in `rationale`. Put citation-only work in
`evidence_action` and `evidence_refs`.

`proposed_operations` are review intentions, not executable patches. Every operation must reference an existing scope `item_key`, remain inside its target path, and keep `requires_human_confirmation=true`.

Use `system_observations` for code, data, evidence-index, state, or presentation problems discovered during review. Do not use it for ordinary content wording changes.

Each systemic observation is a hypothesis awaiting a separate human decision. It does not become a
global optimization candidate merely because the recommendation is accepted. Use a stable lowercase
`cause_key` in `namespace:problem` form, name the concrete `affected_layer`, cite only evidence actually
available in the bundle or `review_trace.evidence_reads`, and provide exact current scope targets or an
explicit `code:`, `system:`, `prompt:`, `schema:`, `api:`, `frontend:`, or `test:` target. High-severity
observations must include an executable regression test idea. Set `is_systemic=false` for useful local
notes that should remain in the result artifact but should not enter improvement-signal triage.
The field must be decided explicitly; omitted values are treated as `false`.

### `cause_key` granularity

`cause_key` names an evidence-backed, reusable failure mechanism, not the incident or the proposed
remedy. Reuse one key across observations only when the mechanism can recur across Projects, points to
the same system modification location, and can be verified by the same regression-test set. If the
modification location or regression scope differs, use a different key. Examples are guidance, not a
whitelist.

Do not include cities, POIs, Project IDs, Finding IDs, dates, run numbers, transient error text,
suggested fixes, or vendor attribution that the evidence has not established.

- Wrong: `chengdu:wuhouci_unknown_20260829`
  Right: `route_feasibility:structured_output_truncated`
  The wrong key records one incident; the right key names a reusable failure mechanism.
- Wrong: `model:content_error`
  Right: `integration:consumes_force_finalized_claim`
  The wrong key is too broad; the right key identifies a concrete mechanism and regression scope.
- Wrong: `fix:add_mimo_fallback`
  Right: `state:force_finalized_not_cleared_after_patch`
  A cause key describes the problem, not a preferred fix.
- Wrong: `amap:timeout_504`
  Right: `evidence_pipeline:timeout_treated_as_business_conclusion`
  The wrong key embeds transient text and unproven vendor blame. Use the right key only when evidence
  shows the reusable handling failure; otherwise set `is_systemic=false`.

Ordinary POI copy errors, one-off citation mistakes, and single network failures are not systemic by
default. Set `is_systemic=true` only when evidence ties the problem to reusable code, prompt, state,
evidence-chain, or delivery behavior. Even then, the observation is only a system hypothesis that
imports as a pending Improvement Signal and still requires human acceptance, merge, reclassification,
dismissal, or deferral; it is not a confirmed root cause.
