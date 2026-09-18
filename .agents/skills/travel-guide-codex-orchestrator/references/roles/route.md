# Route Role

## V0 Responsibility

V0 does not generate a new route. Treat the imported confirmed route structure as a controlled input and run its quality gate.

Read:

- `requirements.json`
- `route_plan.json`
- relevant global material chunks
- newly generated Web or Amap evidence only when the current task actually calls those tools

Do not read the source Project's downstream outputs.

## Quality Gate

Check route structure and citations, then run Map and Web feasibility checks. Map consumes current `route_entities.json`, checks adjacent legs according to `route_execution.json`, and uses Amap driving only for `driving` or `taxi`. Feasibility checks operational status, reservation, opening windows, last entry, closures, road risks, and whether structured operational commitments already cover a requirement.

Facts-only repair may update `notes`, `sources`, and `claims`. It must not change days, city, area, POIs, order, or `needs_detail`. A structural change requires a proposal and user confirmation, which is outside V0 automatic execution.

## Experimental full_plan_route_only

Start from target Project requirements only. Use materials to identify worthwhile POIs, Amap to resolve entities and evaluate sequence and distance, and Web for high-impact current operational facts. Do not inspect any source Project route, downstream output, evidence check, or audit conclusion.

Write one complete draft through `write-route-plan`, including `route_entities` and `route_execution` when available, or write those sidecars immediately afterward through their dedicated Bridge commands. Use `amap_resolve_route_entity` to obtain the ranked candidates, administrative rejection reasons, provider ID, and coordinates before writing an entity selection. Preserve the distinction between the formal route label and provider query entity. Low-confidence, composite, or ambiguous entities remain explicit confirmation items rather than being silently collapsed.

Choose `transport_mode` per adjacent leg from the actual travel context. Scenic shuttle, cable car, walking, rail, public transit, and mixed legs must not be labeled as driving merely because Amap driving is available. A mixed leg must contain ordered subsegments.

For each autonomously selected POI, submit both a bare-name `poi_identity` Claim and a full-proposition `poi_selection` Claim explaining user fit or route function. Do not use `poi_identity` to carry the selection rationale.

Record cross-day actions such as advance reservation, ticket purchase, credential preparation, transfer confirmation, and departure-day recheck in `operational_commitments`, bound to the target `(day, poi_index)` even when the action is planned on another day.

The route draft must satisfy the requested day count, origin/transport constraints, pace and mobility constraints, must-visit requests, and budget direction. Quantitative or operational claims require real evidence; planning judgments may use `estimate`, `derived`, or `user_constraint` support modes as appropriate.

After the independent route checks, stop at `SKELETON_CONFIRM`. Citation, Map, and Feasibility may be rerun separately with `--route-check`; do not repeat completed checkers during a local repair. The Route role and supervisor must not set `skeleton_confirmed=true` or start downstream roles.
