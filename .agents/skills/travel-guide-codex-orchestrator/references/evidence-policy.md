# Evidence Policy

## What Each Source Proves

- Global material chunks support stable travel value, experience details, local practices, and content ideas when the quoted chunk actually contains the claim.
- Project supplement chunks are Bilibili transcripts collected and indexed only for the current Project. They follow the same content-support rules as global chunks and never alter the frozen global corpus.
- Project evidence supports a current Web fact only when stored as valid `project-evidence-v2` with a traceable source URL and excerpt.
- Web search supports current operational facts such as opening status, reservation, quotas, last entry, closures, and current policy.
- Amap distance output supports the queried pair's observed route distance and duration. It does not prove attraction quality or opening status.
- User requirements support preferences and constraints, not external facts.

## Fixed Route Limitation

The V0成都 baseline inherits a previously confirmed route and its historical `amap_verified` flag. The isolated experiment does not copy old Amap logs. Unless the new run explicitly calls Amap again, describe the spatial order as an inherited fixed input, not as newly verified evidence.

The source Project's legacy `materials/note-000.json` is not valid current project evidence and must not be presented as a citable input.

## Route-Only Generation

The source Project contributes only its saved requirements. Its route, downstream outputs, Project evidence, Web logs, map reports, and audit conclusions are not evidence for the new draft.

Current global material chunks and evidence newly observed in the target Project may be used. Amap entity candidates and driving results support identity, sequence, distance, and duration only. Web evidence supports current operating constraints. User requirements support preferences and hard constraints, not external POI facts.

## Claim Rules

- Every newly written Claim must explicitly set `support_mode`; do not rely on a schema default to infer whether content is an external fact, a route-derived judgment, or an estimate.
- Keep Claims atomic by support semantics. An externally verifiable fact and a route-planning recommendation must not share one `direct` Claim. For example, write "the attraction is currently open" as `direct` with evidence, while "it fits the end of this day's route" is a separate `derived` or `estimate` Claim.
- `direct` claims require readable evidence refs.
- `amap` claims require actual Amap evidence IDs.
- General advice may be `estimate` when clearly presented as such.
- Unsupported concrete facts must be removed, narrowed, marked `unverified`, or deferred.
- A search failure, timeout, missing key, or empty result is a technical outcome, not evidence that a business fact is false.
- A material response with `scope_errors` is partial or failed research. Use any returned evidence normally, but never reinterpret the failed scope as proof that evidence does not exist.
- `global_core` and `project_supplement` form one logical UGC retrieval pool and share one final `top_k`; their physical scopes remain visible for audit. Project Evidence is never inserted into this UGC ranking.
- Retrieve Project Evidence explicitly and within the current Agent `log_scope`. Its existence cannot turn an empty or failed UGC lookup into material-research success.
- Embedding and BM25 are independent UGC modalities. If one fails, consume the other's results with `degraded` status; only a failure of both modalities is a UGC retrieval error.
- Do not convert candidate count into recall, API success into travel success, or offline evaluation into production ROI.
