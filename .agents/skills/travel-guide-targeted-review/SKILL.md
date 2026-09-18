---
name: travel-guide-targeted-review
description: Review an existing travel-guide finding with current content, local material evidence, history, and optional web verification; classify false positives, resolvable issues, deferrals, and reruns, then produce a traceable minimal-change recommendation without regenerating the full itinerary. Use for post-generation travel-guide review and targeted repair planning, not initial itinerary creation.
---

# Travel Guide Targeted Review

Review an existing travel-guide finding without rerunning the full generation graph. Treat the project review store as the source of truth and keep the investigation read-only until a human explicitly approves later patch work.

## Start

1. Identify the project root, `project_id`, and `finding_id`.
2. Export a frozen bundle with `scripts/export_review_bundle.py`.
3. Read [references/review-protocol.md](references/review-protocol.md) before investigating.
4. Read [references/evidence-policy.md](references/evidence-policy.md) when judging sources or using web evidence.
5. Produce `travel-review-result-v1`, following [references/result-schema.md](references/result-schema.md).
6. Validate the result with `scripts/validate_review_result.py`.
7. Import it only when the user explicitly asks to record the recommendation. Importing must not apply a content patch.

Use the project Python environment when available:

```bash
.venv-mac/bin/python .agents/skills/travel-guide-targeted-review/scripts/export_review_bundle.py PROJECT_ID FINDING_ID --output /tmp/review_bundle.json
.venv-mac/bin/python .agents/skills/travel-guide-targeted-review/scripts/validate_review_result.py /tmp/review_bundle.json /tmp/review_result.json
```

## Operating Modes

**Direct repository mode:** inspect the bundle, current project files, exact local chunks, review history, code, and tests. Use this mode for Codex or another capable agent. Record every extra source actually read in `review_trace.evidence_reads`.

**Bundle mode:** give only the exported bundle to an external model. The model may use sources included in the bundle; any extra web evidence must be returned in `review_trace.evidence_reads` with its exact reference, excerpt, and source type.

Both modes must return the same result schema and pass the same deterministic validator.

## Non-Negotiable Boundaries

- Current JSON is the object under review, not evidence for itself.
- A missing statement in one attached chunk does not prove that the destination material library lacks evidence.
- Inspect destination-wide alternatives before declaring a claim unsupported.
- Historical failures and recommendations are hypotheses and audit history, not present-day facts.
- Preserve useful places and route value when only a duration, detail, or citation is unsupported.
- Do not invent prices, opening hours, shop names, transport rules, or route details.
- Do not apply patches, rerun generation agents, or rewrite the whole itinerary from this skill.
- Treat 401, 403, 504, timeouts, incomplete scope, and invalid JSON as execution failures, not business conclusions.
- Each scope item must produce exactly one claim review in the same order and at the same `target_path`.

## Result Quality

Prefer the smallest justified action: `keep`, `narrow`, `add_source`, `repair_reference`, `sync_status`, or `rerun`. Also report `content_action` and `evidence_action` separately so a valid place with a bad citation is not presented as wholly incorrect. Use `rerun` only when the content requires renewed route or agent reasoning, not when a citation can be repaired or wording can be narrowed.

Explain what was read, quote the short evidence excerpt actually used, and connect that excerpt to the verdict and proposed text. If the evidence remains insufficient, say so and defer; do not force convergence.

When the investigation reveals a recurring code, prompt, schema, evidence-pipeline, state-contract, or admin-UI issue, record it in `system_observations` using a stable `cause_key` and a concrete regression test. Treat it as a system hypothesis for separate human triage, not as a confirmed engineering fact and not as permission to modify the system.

`proposed_text` is the exact post-change body text shown to the human reviewer. Do not return a meta-level editing instruction. If `content_action=keep`, write `保持原文`; the rationale must not simultaneously request deleting, cancelling, narrowing, or rewriting body content. Citation-only changes belong in `evidence_action` and `evidence_refs`.

For realistic forward tests and known failure cases, read [references/evaluation-cases.md](references/evaluation-cases.md).
