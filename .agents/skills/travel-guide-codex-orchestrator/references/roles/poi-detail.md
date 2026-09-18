# POI Detail Role

## Goal

Generate the internal visit route for one assigned `(day, poi_name)` whose route entry has `needs_detail=true`.

## Inputs

- assigned day, POI name, and `needs_detail_reason`
- user requirements and the POI's route-day context
- relevant materials and current Web evidence when needed

## Output

Write only the assigned POI file through the Bridge. Provide real route options when the evidence supports them. A non-empty route set with zero `recommended=true` routes is valid; `routes=[]` is not a successful deep-detail output and must follow the evidence-gap escalation. Multiple recommended routes are deterministically clamped to one by the existing Store.

The identity and factual attributes of a point need evidence, but the order in which supported points are arranged is a planning judgment and may use `estimate` or `derived`. It does not require an official source publishing the identical complete route. When no supported internal sub-point exists but UGC supports the POI-level experience, use the POI itself as one honest segment with flexible, non-numeric timing rather than inventing an entrance or returning an empty artifact.

Each route segment must contain ordered points and claim-level evidence for point name, highlight, and duration when those fields are factual.

Keep external facts and route-planning judgments in separate Claims. For example, an evidenced current opening fact may be `direct`, while "适合顺路游览" or "适合作为收尾" is a separate `derived` or `estimate` Claim. Every new Claim must explicitly include `support_mode`.

Use `claimed_entities` only for the concrete referents asserted by that Claim, such as a named POI, internal point, building, exhibit, or explicitly named route. Keep time, order, action, price, experience, and generic transport categories such as `景区直通车/观光车/接驳车` in `claim_text`; a `景区` prefix alone does not create a proper name. Do not manufacture compound entities such as `七彩丹霞北门观光车顺序` or `万马饮水时间`. Follow the production POI prompt for the complete business examples.

## Boundaries

- Do not modify route, Lifestyle, Pitfall, final guide, another POI file, or the manifest.
- One POI task may fail without affecting sibling POI tasks.
- Do not invent internal paths, entrances, exhibits, duration, or viewpoints.
- Do not treat a closure advisory, operational uncertainty, or absence of an official internal route as permission to delete, replace, skip, or return an empty fixed-route POI. Write the supported guide content; deterministic Join code owns the separate operational warning.
- Prefer one real route over multiple artificial variants.

## Evidence-Gap Escalation

1. Call `material-context` for the assigned POI with focused internal-route or experience queries. The system keeps the assigned POI name in the effective UGC query and returns global plus overlay chunks in one ranking.
2. When current or official operational evidence is needed, first use `project-evidence-context` to inspect evidence already frozen by this work unit. If it is insufficient, call `web-search`, then freeze an exact returned URL with `append-project-evidence` before citing it.
3. If a current failed validation still shows a true material gap, the supervisor may call `supplement-material-gap`; this role cannot change its limits.
4. Only the failed Claim or segment may be repaired after indexed supplementation. Consume the validator's exact structured issue fields, including the available `claim_id`, `claim_text`, `target_path`, `support_mode`, route or segment scope, and reason. Do not broaden a Claim-level issue into a full POI rewrite unless the issue itself proves the surrounding structure is invalid. `force_finalized` is available only for a non-empty artifact with unresolved citation issues after bounded repair. If the artifact is still empty, report `generation_failed` with `write_contract_failure` and human review required; never convert `content_empty` into a successful degradation.

Return the assigned target, output path, validation status, unresolved issues, and a concise execution summary. The main Codex records the central trace and performs the final Join.
