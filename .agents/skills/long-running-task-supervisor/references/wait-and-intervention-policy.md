# Wait and Intervention Policy

Read this reference when a workflow is slow, repetitive, expensive, or appears inactive.

## Decide From Progress, Not Anxiety

Treat the workflow as advancing when at least one meaningful signal changes:

- a model or tool call completes successfully;
- a new work unit, target, day, branch, or validation item is processed;
- checkpoint, task, process, or run status advances;
- a relevant artifact or trace receives new valid content;
- a planned provider retry or backoff step is in progress;
- the workflow narrows an unresolved set or produces new evidence.

The following signals require inspection but do not independently prove a stall:

- a long wall-clock duration;
- no final file yet;
- a long model call or reasoning phase;
- high input-token use or context compaction;
- several searches before a write;
- an Agent that appears slower than another model or prior run.

## Intervention Ladder

Use the least disruptive level supported by evidence:

1. **Wait:** Progress is visible or an agreed retry is active.
2. **Inspect:** Read current task/process status, recent trace, checkpoint, and affected artifact.
3. **Remind:** When an Agent is gathering more evidence without converging, summarize the remaining decision and remind it that repair may mean splitting, narrowing, reclassifying, or submitting, not only searching. Leave the final business decision to the Agent unless a contract requires one action.
4. **Isolate:** Preserve the live run and replay the smallest failing context separately.
5. **Repair:** Make the smallest general fix that the evidence supports and the user authorized.
6. **Recover:** Resume from the nearest valid checkpoint or incomplete work unit.
7. **Degrade or stop:** Use an existing explicit degradation path, request human action, or report failure when retries are exhausted or an invariant cannot be preserved.

## Evidence That Supports Intervention

Intervention is justified when one or more of these is confirmed:

- the process or task exited and will not recover automatically;
- the same deterministic contract or tool error repeats with the same relevant input;
- provider retry policy is exhausted;
- the same work unit repeats materially equivalent actions without new evidence or state;
- an invariant, confirmed structure, or allowed write boundary is violated;
- the user-defined cost, token, call, or time boundary is reached;
- required output is missing while the workflow incorrectly reports completion;
- the runtime is waiting for approval or user input that cannot be supplied autonomously.

## Waiting Mechanics

- Prefer event-based waits, task wait tools, or the existing process session over frequent polling.
- After a timeout with unchanged state, back off rather than issuing the same inspection rapidly.
- A timeout from a wait operation means no new observed event during that window; it does not necessarily mean the underlying task failed.
- An empty wait result is an observation result, not permission to finish the supervising turn. While the process or a decided recovery remains active, continue supervision and use commentary rather than a final response.
- Do not inject `Ctrl-C`, terminate the session, or otherwise convert an uncertain slow call into an artificial failure. Let the runtime return its own success or error unless another intervention condition in this policy is already proven.
- When the user explicitly says not to interrupt slow work, preserve that policy unless the task exits, hits a hard failure, violates an invariant, requires permission, or crosses a user-defined boundary.
