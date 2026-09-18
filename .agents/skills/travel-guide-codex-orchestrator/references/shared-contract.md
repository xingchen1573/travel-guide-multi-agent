# Shared Contract

## Sources of Truth

- `workspace/requirements.json`: user requirements.
- `workspace/route_plan.json`: route draft or confirmed route structure and route facts. `progress.skeleton_confirmed` distinguishes the two states.
- `workspace/route_entities.json`: Route-owned provider entity selections, candidates, administrative scope, coordinates, confidence, and route-structure binding.
- `workspace/route_execution.json`: Route-owned leg transport modes, mixed subsegments, and cross-day operational commitments.
- `workspace/material_baseline.json`: immutable formal-comparison corpus, Prompt, and model-boundary denominator.
- `workspace/research_runs.jsonl`: service-authored material, Web, Project Evidence, Amap, and supplement observations by work unit.
- `workspace/route_feasibility.json`: checker-owned operational findings.
- `workspace/outputs/lifestyle.json`: Lifestyle-owned recommendations.
- `workspace/outputs/pitfall.json`: Pitfall-owned warnings bound to Lifestyle.
- `workspace/outputs/poi_details/*.json`: one POI per independently writable file.
- `workspace/final_guide.blocks.json`: Integration-owned body and upstream claim mappings.
- `workspace/final_guide.md` and `final_guide.meta.json`: program-rendered delivery.

## Ownership

Roles must write through the Skill Bridge, which selects an existing actor-bound Workspace tool. Do not pass a caller-selected actor and do not write model outputs directly to Workspace files.

The confirmed route structure consists of day number, city, area, POI names, POI order, and `needs_detail` flags. Downstream roles may not alter it. Route notes, claims, and sources are facts-layer fields and may be repaired without changing structure.

Provider names and coordinates never replace the formal route label. `route_entities.json` and `route_execution.json` must carry the current route structure hash; stale sidecars are rejected by the write layer and ignored by checkers.

In `full_plan_route_only`, the Route role may create the initial complete structure while the Project is in `ROUTE_PLANNING`. After the write, the Bridge moves the Project to `SKELETON_CONFIRM` and keeps `skeleton_confirmed=false`; no downstream role may run in that state.

Every autonomously selected POI has two name-path Claims: `poi_identity` for deterministic identity checking and `poi_selection` for the complete selection rationale. A bare POI name is valid only for `poi_identity`.

## Completion Requirements

- Lifestyle contains exactly one item for each route day in each of `food`, `outfit`, `lodging`, and `play`.
- In a Grade A comparison, Lifestyle has a material research observation for every route day.
- Pitfall contains one item for every actual Lifestyle `(field, day)`, including legitimate empty warnings.
- Pitfall target text is rebound by the existing tool to the authoritative Lifestyle recommendation.
- In a Grade A comparison, every Pitfall item has a material research observation bound to the current Lifestyle recommendation SHA.
- Integration uses current upstream claim IDs and does not invent its own reference section.
- POI Detail writes only its assigned `(day, poi_name)` file.
- Every Grade A `needs_detail=true` target has an exact-target material research observation. `routes=[]` is never passed or degraded-success content; after bounded exact-target repair it remains an explicit `generation_failed/write_contract_failure` with human review. `force_finalized` applies only to non-empty content with unresolved citation issues.
- The main supervisor is the only writer of the central Codex trace and POI manifest finalization.

## Retry Boundary

Validation does not automatically rewrite content. Return the issue list to the owning role and request a local repair. When an issue identifies a Claim, preserve its structured repair fields verbatim, including the available `claim_id`, `claim_text`, `target_path`, `support_mode`, owner scope, and reason; do not replace them with a prose-only summary. The owning role must limit the repair to that reported scope and may split a mixed-support Claim without changing unrelated route or content structure. Stop after two unsuccessful repair rounds. A later explicit human decision may keep, revise, or degrade the result using existing project mechanisms.

An explicit decision to keep a confirmed route preserves the original Map and
Feasibility findings and marks the route stage as accepted with degradation.
Technical failures cannot be accepted as route risk. The route structure hash
must remain unchanged, and later delivery must continue to expose the
degradation rather than present the route gate as passed.
