---
name: long-running-task-supervisor
description: Supervise multi-hour or failure-prone agent workflows using observable progress, evidence-based intervention, failure isolation, and checkpoint-aware recovery. Use when the user explicitly asks Codex to watch, guard, resume, or stay with a long-running task; not for ordinary one-shot work.
---

# Long-Running Task Supervisor

Keep a long-running workflow aligned without interrupting healthy slow work or blindly waiting through a confirmed failure. This skill governs supervision decisions; it does not replace the domain workflow, create a new supervisor Agent, or grant permission to modify code or external systems.

## Establish the Contract

Before starting or resuming, identify:

- the requested outcome and acceptance checks;
- business invariants that must not change;
- the current run, task, Project, process, or checkpoint;
- the user's latest waiting, retry, cost, and intervention policy;
- the existing sources of runtime truth, such as Workspace state, checkpoints, task status, traces, logs, artifacts, and validators.

Preserve the contracts of any domain skill used with this skill. The domain skill decides what the workflow does; this skill decides how to observe, wait, intervene, and recover.

## Supervision Loop

1. Reconstruct the minimum supervision state described in [references/state-capsule.md](references/state-capsule.md). Prefer existing durable state; do not create a new state file unless the task lacks one and the user has authorized documentation changes.
2. Inspect observable progress before making a diagnosis. Never infer that a task is stuck only from elapsed time, high token use, slow reasoning, or the absence of a final artifact.
3. Continue waiting while calls, tools, checkpoints, artifacts, work units, or an agreed retry sequence are still advancing. Read [references/wait-and-intervention-policy.md](references/wait-and-intervention-policy.md) when a task is slow, repetitive, expensive, or appears inactive.
4. When evidence shows a failure, classify it before acting. Read [references/failure-and-recovery-protocol.md](references/failure-and-recovery-protocol.md) to separate transport, deterministic contract, action convergence, content validation, business-no-result, permission, and missing-artifact failures.
5. Preserve the failure site. Reproduce the smallest relevant context in isolation when the cause is uncertain, change one primary variable at a time, and distinguish confirmed defects from hypotheses.
6. Apply a minimal general repair only when the current request authorizes changes. Otherwise report the evidence and proposed repair without mutating the workflow.
7. Validate the repair at the narrowest useful level, then resume from the nearest valid checkpoint or completed artifact. Do not restart completed stages merely because a downstream unit failed.
8. Keep the supervision turn active while the supervised process, task, or an already-decided recovery action is still active. A wait call that returns no output or reaches its observation timeout is not a terminal state. Send progress through commentary and continue waiting, inspecting, backing off, or recovering; do not send a final response merely to close the current turn.
9. Finish only after an evidence-backed terminal state: completed, action required, explicitly degraded, or failed. Do not claim unattended completion, quality, cost, or stability beyond the recorded evidence.

## Guardrails

- When the execution contract marks an external API call as requiring a Host-authorized external network boundary, use that boundary from the first attempt. This skill does not grant elevation and repository code must not bypass user approval. If a restricted-sandbox DNS failure is later confirmed, do not multiply same-path Provider retries; follow the sandbox network-boundary recovery rule before applying Provider fallback.
- Time is a reason to inspect, not by itself a reason to stop.
- A missing Workspace artifact is not proof that an Agent or model call is stuck.
- Silence from the terminal, an unchanged high-level status file, or an empty wait result is not proof that the underlying work stopped. Check deeper durable signals when needed and keep the active session under supervision.
- Do not repeatedly poll unchanged state; wait through the runtime's event or session mechanism when available.
- Do not interrupt a delegated task before inspecting its harness-visible status.
- Do not send `Ctrl-C`, terminate a session, or abandon an active run by ending the supervision turn unless the intervention policy has reached an evidence-backed stop, degradation, approval, or recovery boundary.
- Do not broaden retry, token, or time limits beyond the user's policy. When no limit exists, choose a proportional stopping condition based on repeated identical failure or lack of new information, not a universal fixed threshold.
- Do not turn provider failures into content repair rounds, or content uncertainty into provider failures.
- Preserve completed work, current invariants, the latest failed submission, and the latest useful validation error across compaction and recovery.
- When an interrupt requires the user to choose among business outcomes, preserve it as action required. Never proxy, infer, pre-authorize, or auto-submit a choice such as keep, confirm, approve, or reject. Resume only after a new explicit user message supplies that choice; an AI-authored `human` flag or source parameter is not evidence of user authorization.
- Record observable facts, decisions, and results. Never persist hidden chain-of-thought or private reasoning content.
- Keep the user informed during long waits, but do not manufacture intervention merely to produce an update.
- A user progress question does not pause or cancel the supervised run. Answer with current evidence, then resume supervision unless the user explicitly asks to pause or stop.

## Combined Use

When another skill owns the business workflow:

```text
domain skill: stages, artifacts, validators, and business boundaries
this skill: observation, patience, intervention evidence, and recovery discipline
```

Use the domain workflow's existing checkpoint and trace mechanisms. Add adapters or scripts only after repeated cross-project evidence shows that a deterministic helper would remove real duplication.
