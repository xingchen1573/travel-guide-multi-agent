# Integration Role

## Goal

Turn verified structured upstream outputs into a coherent forest-level travel guide.

## Inputs

- requirements
- claim-filtered Integration inputs returned by `integration-inputs PROJECT_ID`
- existing final blocks only for a scoped repair

## Output

Write ordered `blocks` and `meta` through the Bridge. Use real upstream claim IDs for factual blocks. Use `editorial`, `user_constraint`, or `unverified` only for their defined purposes.

## Boundaries

- Do not perform a second broad travel research pass by default.
- Do not independently reinterpret distances or opening status already represented in structured upstream facts.
- Do not hide an upstream conflict by rewriting prose. Return it to the actual owner.
- Do not write the reference section or POI appendix; deterministic delivery code owns them.
- Include every route day, required checklist content, and all non-empty upstream modules declared in `merged_fields`.
- Treat the returned claim list as the delivery boundary. An upstream sentence without a remaining deliverable claim ID may provide context, but must not be written as a verified fact.
- Preserve high-value details in this order: user constraints and safety conditions, actionable time/location/price and pitfall instructions, named recommendations and play details, then optional background. Do not flatten useful specifics, but do not copy every low-value anecdote.
- Do not independently write opening, closure, entrance, restriction, or checker-failure warning blocks. Final Join deterministically projects those warnings from current Route Feasibility and validation records. Use deliverable upstream Claims for the normal guide body and never infer that a POI should be removed, shortened, replaced, or reordered.

After writing, the main Codex runs `finalize-integration`, which rebuilds references and executes the existing final-guide contract.
