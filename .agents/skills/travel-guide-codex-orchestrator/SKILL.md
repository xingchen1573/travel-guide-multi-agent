---
name: travel-guide-codex-orchestrator
description: Generate and validate a traceable autonomous route draft or fixed-route travel guide through a configured travel runtime using Codex-native orchestration and necessary specialist agents. Do not use for post-generation finding review.
---

# Travel Guide Codex Orchestrator

Use Codex as the supervisor while preserving the project's existing business roles, evidence rules, Workspace ownership, and deterministic quality gates. Do not call `run_plan()` or execute the LangGraph main graph.

## Portable Runtime Setup

The installer creates a user-level Skill containing a generated snapshot of the
current runtime and Kernel package. The installed copy runs from any working
directory without importing code from the source checkout. Its configured Python
environment must remain available; place that environment outside the repository
before removing the checkout. The repository remains the only maintained source;
update an installation by rebuilding it with `--replace`.

Run from the canonical repository with a Python environment that already has the
project dependencies:

```bash
python .agents/skills/travel-guide-codex-orchestrator/scripts/install_skill.py \
  --runtime-python /absolute/path/to/python
```

Add `--install-dependencies` only when that Python environment should be modified
by `pip`. The installer never copies `.env`, credentials, Projects, runtime state,
or the material library. It stores writable state under
`${CODEX_HOME:-$HOME/.codex}/travel-guide-runtime` by default.

The verified installed-bundle boundary is currently `full_plan_route_only`
through draft writing and deterministic structure validation. Live evidence
gates and the downstream full-guide path remain checkout-backed until they have
their own clean-install acceptance evidence.

- An installed bundle records its runtime code, Python, and writable state paths
  in `.travel-guide-runtime.json`. CLI flags and environment variables override
  that file.
- `TRAVEL_GUIDE_RUNTIME_ROOT` relocates writable `projects/`, `data/`, and
  `runtime/` state; it does not copy existing state.
- `TRAVEL_GUIDE_PYTHON` selects another Python environment. An installed bundle
  keeps the exact virtual-environment launcher path.
- `TRAVEL_GUIDE_REPO_ROOT` and `--repo-root` remain development compatibility
  options for using a checkout directly.
- Run `doctor` before the first business command. It is read-only and checks
  the Skill bundle, runtime files, role contracts, Python, and runtime imports.
  Project data, materials, credentials, and live services remain the existing
  `preflight-*` commands' responsibility.

For a user-level installation, set the Skill-local bridge path once:

```bash
BRIDGE="${CODEX_HOME:-$HOME/.codex}/skills/travel-guide-codex-orchestrator/scripts/travel_guide_bridge.py"
python "$BRIDGE" doctor
```

For project-local development without installation, use:

```bash
BRIDGE=".agents/skills/travel-guide-codex-orchestrator/scripts/travel_guide_bridge.py"
```

## Start

1. Identify the Skill path, installed or checkout runtime, Project ID, and run mode.
2. Run the read-only `doctor` command. Do not continue while it reports `blocked`.
3. For an existing Project, run the read-only `resume-plan` command before loading role references or taking any action. Use its `next_actions` to select the minimum role reference and explain the recovery point and degradations to the user. Do not treat Trace history as current business state.
4. For a new fixed-route Project, identify the source Project and target experiment Project. A formal comparison source must already contain an immutable `material_baseline.json` created during shared case preparation. `preflight-fixed-route` never creates or refreshes that baseline. Run `preflight-fixed-route --live`, require `ready=true`, then use `prepare-fixed-route`. Grade A runs must add `--require-embedding`; add `--require-comparison-sidecars` to both commands and record the requested experiment grade and host model during preparation.
5. Read [references/workflow.md](references/workflow.md). The experimental route-only path supports requirements-to-draft generation and stops before user confirmation. In an installed bundle, the supervising Codex may perform this first Route task directly from the bundled Route reference; project-level custom Agent loading is not required for this route-only milestone.
6. Read [references/shared-contract.md](references/shared-contract.md) before any write.
7. Read [references/evidence-policy.md](references/evidence-policy.md) before using materials, Web, or Amap evidence.
8. Read only the role reference needed for the current stage.
9. Use the Skill-local `scripts/travel_guide_bridge.py` for project preparation, recovery planning, structured writes, validation, delivery finalization, and central trace events.

Inspect the portable CLI without importing the host runtime:

```bash
python "$BRIDGE" --help
```

Resume an existing Project without model, Web, Amap, Workspace writes, Trace writes, or automatic execution:

```bash
python "$BRIDGE" resume-plan PROJECT_ID --expected-route-sha ROUTE_SHA256
```

## Supervisor Contract

- Run commands that may reach Embedding, model Providers, live Web/Tavily, Amap, remote validators, or other external APIs through the Codex Host's already authorized external network boundary from their first attempt. Repository code and delegated roles must not elevate themselves or bypass user approval. Include `network_execution_mode=host_approved_external` in each affected specialist task package.
- If a command was accidentally attempted in a restricted sandbox and produced DNS or connection refusal, preserve and compare any Workspace side effects before rerunning the identical request once through the authorized external boundary. When the external rerun succeeds, record a `retry` Trace event with `status=sandbox_network_boundary_recovered` plus sanitized `original_outcome` and `external_outcome`; this is not a Provider fallback. A confirmed sandbox DNS failure must not enter same-path Provider retries; only an external-boundary failure proceeds to the configured finite Provider retry policy.
- Read Workspace files as the source of current business state.
- Keep the confirmed route structure hash and check it after downstream stages.
- Keep the frozen base material corpus and Prompt fingerprint unchanged. Project-local supplements may change only the target Project overlay and must have Research Ledger and supplement audit records.
- Delegate only when a role boundary, parallel benefit, or failure isolation justifies it.
- Always fan out `needs_detail=true` POIs as independent POI Detail tasks.
- Keep the Harness task or Agent ID returned by every delegation. Before interrupting or taking over a delegated task, inspect its Harness-visible status at least once; absence of a Workspace artifact is not proof that the task is stuck.
- The main Codex is the only writer of central delegation trace events. Record the task ID, start time, observable pre-interrupt status, and takeover reason. Do not ask specialist Agents to write heartbeats or the central trace.
- Return validation issues to the role that owns the failed artifact. Preserve structured Claim repair fields verbatim instead of summarizing them into prose, so the specialist receives the exact failed Claim and allowed repair scope.
- Treat `workspace/research_runs.jsonl` as the proof of material, Web, Project Evidence, Amap, and supplement activity. An Agent summary is not a substitute for a ledger row.
- Do not accept `routes=[]` for a `needs_detail=true` POI as passed or degraded-success. Return it to the same POI role for bounded exact-target repair; if it remains empty, record `generation_failed/write_contract_failure` with human review. `force_finalized` is only for non-empty content with unresolved citation issues.
- After two unsuccessful repair rounds, stop automatic retries and request human review or an explicit degradation decision.
- When the user explicitly keeps a confirmed route after reviewing non-technical Map or Feasibility findings, persist that decision through `accept-route-risks`. Never use it for provider, parsing, stream, or tool failures, and never rewrite the original findings as passed.
- Record observable actions and summaries in the central trace; never record hidden reasoning.
- Treat improvement observations as hypotheses, not confirmed defects or permission to modify the production graph.

## Supported Boundaries

V0 covers:

```text
confirmed route input
-> route quality gate
-> Lifestyle
-> Pitfall
-> Integration
-> POI Detail fan-out
-> deterministic final delivery
```

It does not cover requirements interviewing, route generation, culture Q&A, site/poster generation, Feishu, or travel companion behavior.

The experimental `full_plan_route_only` path covers:

```text
saved requirements only
-> dependency preflight
-> isolated Project preparation
-> Codex Route role generates a complete route draft
-> route structure, entity, Amap, citation, and feasibility checks
-> stop at skeleton confirmation
```

It must not import a source `route_plan.json`, read source downstream outputs, mark the skeleton confirmed, or start Lifestyle and later stages.

## Role References

- Route quality or future route work: [references/roles/route.md](references/roles/route.md)
- Lifestyle: [references/roles/lifestyle.md](references/roles/lifestyle.md)
- Pitfall: [references/roles/pitfall.md](references/roles/pitfall.md)
- Integration: [references/roles/integration.md](references/roles/integration.md)
- POI Detail: [references/roles/poi-detail.md](references/roles/poi-detail.md)
