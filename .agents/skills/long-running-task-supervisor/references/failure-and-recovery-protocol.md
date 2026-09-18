# Failure and Recovery Protocol

Read this reference after observable evidence indicates that waiting alone is no longer appropriate.

## Classify First

| Failure class | Typical evidence | Response |
|---|---|---|
| Sandbox network boundary | A remote DNS or connection refusal occurs in a restricted sandbox, while the identical Provider, model, configuration, and request is eligible for a Host-authorized external execution boundary | Preserve and compare any side effects, then rerun the identical request once through the already authorized external boundary. Classify external success as `sandbox_network_boundary` and append `retry/status=sandbox_network_boundary_recovered`; it is not Provider fallback. Confirmed sandbox DNS failures do not enter same-path Provider retries. Repository code must not elevate itself or bypass user approval. |
| Provider or transport | 429, 5xx, timeout, connection reset, channel unavailable | Apply the configured retry, backoff, or provider fallback. Preserve the work unit and do not count it as a content repair failure. |
| Deterministic contract | The same schema, path, validator, or tool-argument error repeats for equivalent input | Preserve the failing request and validation error, isolate a minimal reproduction, fix the shared contract or misleading feedback, and run a focused regression. |
| Action convergence | Repeated search or edits produce no new evidence, or the Agent does not submit after the task is sufficiently resolved | Inspect the unresolved items and give a concise decision reminder. Do not add another Agent or hard command unless the existing workflow requires it. |
| Content validation | A successful response contains unsupported, composite, contradictory, or incorrectly classified claims | Repair only the affected claims or work unit: split, narrow, reclassify, bind better evidence, or explicitly degrade. Avoid full-stage reruns when local repair is supported. |
| Business no result | Research completes without reliable evidence or a valid recommendation | Record unknown, omit unsupported specificity, or use the domain's explicit degradation path. Never fabricate success. |
| Permission or approval | Runtime reports approval required, user confirmation required, or inaccessible external state | Preserve the checkpoint and state what input or permission is required. Waiting is appropriate only if the approval may still arrive. |
| Missing required artifact | Manifest or contract requires an output that is absent after the responsible task ended | Mark the target action-required or generation-failed, then resume only that target if supported. Do not fabricate a placeholder to make the run appear complete. |

## Isolation Protocol

1. Freeze or preserve the current Project, run, task ID, checkpoint, relevant trace, latest failed submission, and latest useful validator output.
2. Extract only the failing input, active instructions, tool schema, affected artifact, and required business constraints.
3. Reproduce without changing the live run when practical.
4. Change one primary variable at a time, such as provider, prompt wording, tool feedback, parser, or validator.
5. Label the result as confirmed root cause, contributing factor, non-reproduction, or hypothesis.
6. Do not generalize one model response or one POI-specific failure into a universal rule without broader evidence.

## Repair and Validation

- Prefer a shared, deterministic correction when the defect is in a shared contract.
- Prefer prompt clarification when the model lacks a stable decision boundary but the contract is already correct.
- Prefer defensive parsing only when it has a narrow, unambiguous acceptance rule and still validates against the target schema.
- Keep retries finite and proportional to the failure mode.
- Run the smallest regression that proves the defect is fixed, then a broader relevant suite when the shared surface has meaningful blast radius.
- Preserve before-and-after evidence so later audits can distinguish the original failure from the repaired behavior.

## Recovery

Resume from the nearest valid state in this order:

1. incomplete claim, item, target, or work unit;
2. incomplete branch or specialist task;
3. current graph node or stage checkpoint;
4. full workflow only when earlier state is invalid or no durable recovery point exists.

Before resuming, confirm that completed artifacts remain valid, business invariants still hold, and the repair does not silently widen the write boundary.

When a provider response supplies an explicit `retry_after` or equivalent backoff duration, pass it to the owning Provider/Controller policy and do not resume before the resulting `next_retry_at`. If that delay exceeds the in-process wait budget, preserve the checkpoint and let the supervisor wait the remaining duration before using the same supported resume command; never shorten the provider delay by retrying early. Do not create a new Project or rerun completed stages solely because the transport call failed.
