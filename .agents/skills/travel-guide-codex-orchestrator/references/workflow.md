# Workflow

## Command Setup

All examples use the Skill-local script selected by `BRIDGE`. For a user-level
installation:

```bash
BRIDGE="${CODEX_HOME:-$HOME/.codex}/skills/travel-guide-codex-orchestrator/scripts/travel_guide_bridge.py"
python "$BRIDGE" doctor
```

Create or update that installation from the canonical repository:

```bash
python .agents/skills/travel-guide-codex-orchestrator/scripts/install_skill.py \
  --runtime-python /absolute/path/to/python
```

The generated bundle records its own runtime code, Python, and writable state
paths. It does not import code from the repository at execution time, but its
configured Python environment must remain available. Put that environment outside
the repository before removing the checkout. Re-run the installer with `--replace`
after updating the canonical source. Add
`--install-dependencies` only when the selected Python environment should be
modified by `pip`. The clean-install acceptance currently covers the
`full_plan_route_only` draft and deterministic structure check; keep live gates
and downstream full-guide runs on a configured checkout for now.

`TRAVEL_GUIDE_REPO_ROOT`, `TRAVEL_GUIDE_PYTHON`, and
`TRAVEL_GUIDE_RUNTIME_ROOT` remain explicit overrides. A project-local checkout
may still load its specialist Agent definitions; an installed route-only run may
instead keep the Route task in the supervising Codex after reading the Route
reference. `doctor` verifies files and imports only; it does not replace
Project-specific `preflight-*` checks.

## Resume an Existing Project

Before continuing an existing Project, derive its state from the current Workspace:

```bash
python "$BRIDGE" resume-plan PROJECT_ID --expected-route-sha ROUTE_SHA256
```

`resume-plan` is read-only. It does not call models, Web, or Amap; append Trace; update validation metadata; create recovery files; start Agents; retry work; or advance `progress.json`. Workspace artifacts and deterministic validators determine current status. Trace is read only when a later audit needs historical execution context.

Resume statuses are `missing`, `blocked`, `needs_validation`, `needs_repair`, `completed`, `completed_with_degradation`, and `stale`. Follow `next_actions` in dependency order. For POI Detail, preserve completed Manifest branches and delegate only missing or failed targets. A POI-only change returns a Join/final-contract action rather than rerunning Integration.

## Trace Protocol

The stable central events are `run_start`, `stage_start`, `stage_completed`, `agent_delegated`, `agent_completed`, `validation_result`, `test_result`, `user_confirmation`, `retry`, `observation`, and `run_completed`. Use repeated `agent_delegated` events for fan-out and attach Integration-input summaries to the relevant stage event instead of inventing new event names. The Bridge adds `timestamp`, `project_id`, current `route_structure_sha256`, and `trace_schema_version`; it appends new records only and never migrates or overwrites historical Trace.

Every `agent_delegated` event must include the exact non-empty `task_id` returned by the Harness, and its matching `agent_completed` event must reuse that same value. A role, POI name, or `agent_id` does not replace `task_id`; the Bridge rejects either lifecycle event when it is missing, and rejects `agent_completed` unless that Project trace already contains the matching delegation.

When an identical external-API request recovers after moving from a restricted
sandbox to the Host-authorized external boundary, append a `retry` event with
`status=sandbox_network_boundary_recovered` and sanitized `original_outcome`
and `external_outcome` fields. Do not use `observation`: that event remains
reserved for `status=hypothesis`.

## V0: fixed_route

During shared-case preparation, before either Harness begins its formal run, freeze the common material and Prompt denominator once:

```bash
python "$BRIDGE" freeze-material-baseline SOURCE_PROJECT_ID
```

This command is idempotent when the current corpus and Prompt fingerprint still match. It refuses to overwrite an existing drifted baseline. Do not run it to make a failed preflight green, and do not refresh it between the Codex and LangGraph runs.

Run the dependency preflight before creating the target Project:

```bash
python "$BRIDGE" preflight-fixed-route SOURCE_PROJECT_ID TARGET_PROJECT_ID --live \
  --require-comparison-sidecars --require-embedding
```

Do not continue unless `ready=true`. The report computes requirements and route
structure hashes, verifies the already frozen material and Prompt baseline, and
distinguishes configured, live-passed, failed, and host-managed channels. Run
this command through the Codex Host's already authorized external network
boundary; `--require-embedding` is mandatory for Grade A and remains optional
for compatibility in other fixed-route runs.

Prepare a new isolated Project from a source Project:

```bash
python "$BRIDGE" prepare-fixed-route SOURCE_PROJECT_ID TARGET_PROJECT_ID \
  --require-comparison-sidecars \
  --comparison-grade A \
  --model gpt-5.6-sol
```

Record the returned `route_structure_sha256`. The preparation step imports only requirements and the confirmed route. It does not copy downstream outputs, prior evidence checks, conflicts, Agent logs, final guide, or legacy project notes.

If route validation returns only business findings and the user explicitly
decides to keep the confirmed structure, persist the decision without changing
the finding statuses:

```bash
python "$BRIDGE" accept-route-risks PROJECT_ID --expected-route-sha ROUTE_SHA256 \
  --note "用户已知情并明确保留固定路线"
```

This command is legal only after an explicit human decision. It sets the
existing report-level `force_finalized` marker, records a `user_confirmation`
Trace event, and preserves every original `warning` or `blocked` check. It
refuses technical failures and stale route hashes.

## Experimental: full_plan_route_only

Run the dependency preflight before creating the target Project. Unlike fixed-route preflight, this path must not require or inspect a source route:

```bash
python "$BRIDGE" preflight-route-only SOURCE_PROJECT_ID TARGET_PROJECT_ID --live
```

Run this live preflight through the same Codex Host-authorized external network
boundary used by fixed-route live commands.

Prepare an isolated Project containing only the saved requirements:

```bash
python "$BRIDGE" prepare-route-only SOURCE_PROJECT_ID TARGET_PROJECT_ID
```

Delegate one Route role task. It may read the target requirements, current global materials, and evidence newly produced for the target Project. It must not read any source route or downstream output. Write the complete draft through the Bridge:

```bash
python "$BRIDGE" write-route-plan PROJECT_ID --input /tmp/route_plan.json
```

The Bridge validates the day structure before writing and moves the Project to `SKELETON_CONFIRM` with `skeleton_confirmed=false`. Run route validation after writing. The experiment ends with the draft and validation reports; do not proxy-confirm the route and do not enter Lifestyle.

Close the experiment without advancing the business state:

```bash
python "$BRIDGE" finalize-route-only PROJECT_ID
```

The finalizer rejects confirmed skeletons and any downstream output. It also requires the latest independent `structure`, `citation`, `map`, and `feasibility` checker receipts to be current; Citation, Map, and Feasibility receipts must be live and technically complete. It updates only the experiment manifest so a deliberately stopped route-only run is not left as `in_progress`.

## Stages

1. Append `run_start` and `stage_start` trace events.
2. Validate the route. Use `--live` only for an approved real run because it may call models and Web tools. The first run is full; later retries pass only unresolved `check_id` values.
3. Run Lifestyle and require at least one `material-context` observation for every route day, then require all four fields for every route day.
4. Validate Lifestyle and return issues only to the Lifestyle owner.
5. Run Pitfall against the current Lifestyle output. Retrieve materials for every concrete `(field, day)` recommendation; Pitfall has no Web, Project Evidence, or supplement permission.
6. Validate complete `(field, day)` coverage, current recommendation-SHA binding, material research coverage, and citations.
7. Run `integration-inputs PROJECT_ID`, then run Integration only over that claim-filtered delivery view plus requirements. Do not feed the role raw evidence or failed claim IDs.
8. Finalize Integration so the program-owned reference section is rebuilt before contract validation.
9. Collect `needs_detail=true` targets and delegate one independent POI task per target. Run them in parallel when the Harness permits. Every target must first have an exact-target `material-context` observation.
10. Validate each POI independently. A failed POI must not erase successful POIs. A `routes=[]` result is always content-empty: return it to the same POI role for bounded exact-target repair, and record `generation_failed/write_contract_failure` plus human review if it remains empty. Never use `force_finalized` to bless empty content.
11. Join POI results, rebuild the manifest and reference section, and deterministically project current POI operational findings into the program-owned appendix and `final_guide.meta.json.warnings`; then run the final delivery contract. Warnings do not alter the fixed route.
12. Append `run_completed` with output paths, failures, degradations, cost observations, and hypotheses.

For every delegated task, retain the Harness task or Agent ID. Before interruption or main-Codex takeover, inspect the Harness-visible status once and record only what is observable. Waiting for user approval is not a task failure, and lack of a Workspace write does not prove lack of progress.

## Bridge Commands

```bash
# Write a stage payload produced by the owning role.
python "$BRIDGE" write PROJECT_ID lifestyle --input /tmp/lifestyle.json
python "$BRIDGE" write PROJECT_ID pitfall --input /tmp/pitfall.json
python "$BRIDGE" write PROJECT_ID integration --input /tmp/integration.json
python "$BRIDGE" write PROJECT_ID poi_detail --day 2 --poi-name 武侯祠 --input /tmp/wuhouci.json

# Patch route facts only. The confirmed structure SHA is checked before and after.
python "$BRIDGE" write-route-facts PROJECT_ID --day 2 \
  --expected-route-sha SHA256 --input /tmp/route_day2_facts.json

# Persist canonical provider entity choices and route execution intent.
python "$BRIDGE" write-route-entities PROJECT_ID --input /tmp/route_entities.json
python "$BRIDGE" write-route-execution PROJECT_ID --input /tmp/route_execution.json

# Validate without external model/Web calls.
python "$BRIDGE" validate PROJECT_ID lifestyle --expected-route-sha SHA256

# Derive the next recovery action without modifying the Project.
python "$BRIDGE" resume-plan PROJECT_ID --expected-route-sha SHA256

# Shared, claim-filtered input for the Integration role.
python "$BRIDGE" integration-inputs PROJECT_ID

# Shared UGC research. Results jointly rank the frozen global core and this
# Project's supplement overlay. Project Evidence is a separate explicit channel.
python "$BRIDGE" material-context PROJECT_ID \
  --stage lifestyle --day 1 --query "青海湖 穿搭"
python "$BRIDGE" material-context PROJECT_ID \
  --stage pitfall --field food --day 1 --query "当前Food推荐的真实体验风险"
python "$BRIDGE" material-context PROJECT_ID \
  --stage poi_detail --day 1 --poi-name 青海湖 --query "青海湖 景区内部游览路线"

# Explicitly retrieve frozen Web evidence from the current work unit. This
# does not occupy UGC top_k and is unavailable to Pitfall.
python "$BRIDGE" project-evidence-context PROJECT_ID \
  --stage poi_detail --day 1 --poi-name 青海湖 --query "青海湖 当前开放 预约"

# Current Web discovery and exact-URL Project Evidence freezing. These are not
# available to Pitfall.
python "$BRIDGE" web-search PROJECT_ID \
  --stage poi_detail --day 1 --poi-name 青海湖 --query "青海湖 官方 游览路线"
python "$BRIDGE" append-project-evidence PROJECT_ID \
  --stage poi_detail --day 1 --poi-name 青海湖 \
  --summary "官方页面中的当前游览路线事实" --source-url EXACT_RETURNED_URL

# Route-only entity resolution and driving measurements. These reuse the
# production Route Amap tools and record target-Project research/provider logs.
python "$BRIDGE" amap-resolve-route-entity PROJECT_ID \
  --poi-name 武侯祠 --city-hint 成都
python "$BRIDGE" amap-driving-distance PROJECT_ID \
  --poi-a 武侯祠 --poi-b 锦里古街 --city 成都

# Inspect auditable work-unit research coverage.
python "$BRIDGE" research-status PROJECT_ID

# Bounded Bilibili supplement after a current failed validation only. Limits
# are fixed in production: Project total 10, one trigger 2, candidate pool 25.
python "$BRIDGE" supplement-material-gap PROJECT_ID \
  --stage poi_detail --day 1 --poi-name 青海湖 \
  --expected-route-sha SHA256 --expected-artifact-sha ARTIFACT_SHA256

# Approved live validation.
python "$BRIDGE" validate PROJECT_ID route --live --expected-route-sha SHA256

# Explicit human decision to keep a confirmed route with business findings.
python "$BRIDGE" accept-route-risks PROJECT_ID \
  --expected-route-sha SHA256 --note "用户已知情并保留原路线"

# Run one Route checker independently. Repeat --route-check to compose a subset.
python "$BRIDGE" validate PROJECT_ID route --live \
  --expected-route-sha SHA256 --route-check map
python "$BRIDGE" validate PROJECT_ID route --live \
  --expected-route-sha SHA256 --route-check citation --route-check feasibility

# Scoped route-feasibility retry. Repeat --check-id for each unresolved item.
python "$BRIDGE" validate PROJECT_ID route --live \
  --expected-route-sha SHA256 --route-check feasibility \
  --check-id CHECK_ID_1 --check-id CHECK_ID_2

# Program-owned delivery steps.
python "$BRIDGE" finalize-integration PROJECT_ID
python "$BRIDGE" finalize-poi PROJECT_ID --results /tmp/poi_results.json
```

Every persisted validation also appends a stage-level record to
`workspace/validation_runs.jsonl`. This is the recovery source of truth when a
legal result contains zero Claims. Historical zero-Claim runs may be reconciled
from an existing passed Trace event with `reconcile-zero-claim-validation`;
that command never regenerates or rewrites business content.

Use the same Skill-local `BRIDGE` path throughout one run.

## Failure Routing

| Failure | Owner |
|---|---|
| Invalid or changed route structure | User confirmation or Route role |
| Route fact/citation issue | Route role in facts-only mode |
| Lifestyle coverage or citation issue | Lifestyle role |
| Pitfall coverage, binding, or citation issue | Pitfall role |
| Final prose or claim mapping issue | Integration role |
| Upstream conflict discovered during integration | The actual upstream owner |
| One POI failure | Only that POI task |
| Missing material research ledger row | The owning Route/Lifestyle/Pitfall/POI work unit |
| Confirmed material gap after failed validation | Shared supplement service for Route, Lifestyle, or that POI only |
| External tool unavailable | Mark unknown or stop the affected conclusion |

The main Codex supervises retries. The Bridge reports facts and does not implement another scheduler.

`supplement-material-gap` never accepts caller-selected thresholds and never writes the frozen global core. It checks the current route SHA, current artifact SHA, latest failed validation, collector dependencies, true-gap result, duplicate candidates, and Project budget. Candidate titles pass a deterministic destination/entity check and the existing light-model batch filter; downloaded transcripts must still pass full ingest relevance. Only `indexed` grants one local repair. `rejected`, `pending`, `blocked`, `failed`, and `budget_exhausted` remain auditable degradations and do not justify a whole-stage rerun.

When `--check-id` is present, the Bridge runs only a scoped feasibility retry and does not repeat unrelated checkers. Each executed checker records start/completion timestamps, duration, provider-call deltas, component status, and unresolved check IDs in `workspace/validation_runs.jsonl`. If the Route role changed notes, sources, claims, entities, or execution intent, validate only the affected checker inputs before the scoped feasibility retry.
