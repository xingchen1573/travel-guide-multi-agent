# Minimum Supervision State Capsule

Read this reference when starting supervision, resuming after interruption, or recovering after context compaction.

## Purpose

The capsule preserves the minimum information required to make the next supervision decision. It is not a replacement for Workspace, checkpoints, traces, or business artifacts, and it must not contain hidden chain-of-thought.

```yaml
objective: "The concrete result the workflow must deliver"
acceptance_checks:
  - "Observable verification required before completion"
invariants:
  - "Business structures, hashes, boundaries, or user choices that must remain unchanged"
latest_user_policy:
  waiting: "How patient the supervisor should be"
  intervention: "User-defined conditions for intervention"
  resource_limits: "Known token, call, time, or cost boundaries; unknown when not measured"
run_identity:
  project: "Project or workspace identifier"
  task_or_process: "Task ID, process/session ID, or graph run"
current_state:
  stage: "Current graph node or workflow stage"
  work_unit: "Current target, branch, day, item, or claim"
  observable_progress: "Latest evidenced advancement and timestamp when available"
confirmed_findings:
  - "Evidence-backed facts or root causes"
applied_repairs:
  - "Authorized changes and their focused verification"
unresolved:
  - "Remaining failures, degradations, approvals, or hypotheses"
recovery:
  checkpoint: "Nearest valid checkpoint or completed artifact"
  resume_entry: "Exact supported resume action"
next_allowed_action: "Wait, inspect, isolate, repair, resume, degrade, or request input"
```

## Source Priority

Reconstruct the capsule from current external state:

```text
current Workspace or business store -> what exists now
validator or delivery contract       -> whether it is valid
checkpoint or task status            -> where execution can continue
recent trace and logs                 -> what happened and why
audit or change record                -> confirmed diagnosis and repair history
```

Do not treat an old trace event as current business state when a newer Workspace artifact or checkpoint exists.

## Persistence Rules

- Prefer an existing run-status, Workspace, checkpoint, trace, or audit record.
- Keep the capsule in working context when that is sufficient.
- Create or update a dedicated state file only when durable recovery is otherwise unavailable and the user has authorized file changes.
- Preserve the latest failed tool arguments and concise validation error when they are needed for recovery; discard redundant older copies that only pollute context.
- Mark unavailable measurements as unknown instead of estimating them.
- After compaction or handoff, verify the capsule against external state before acting.
