# Review Protocol

Use this protocol for every finding. Do not substitute a destination-specific checklist.

## 1. Freeze the baseline

Export the bundle and verify its SHA-256. Do not modify the target JSON during investigation. If the file hash or finding revision changes, export a new bundle.

## 2. Recover the complete scope

The bundle scope is the authoritative list for the current run. It is built from the most complete append-only recommendation and the latest claim for each target path. Do not drop items because a later model run returned only a subset.

## 3. Read the current content

For every scope item:

- preserve `target_path` exactly;
- copy `original_text` exactly from the bundle;
- identify the precise claim being tested;
- do not expand the review to unrelated route segments or days.

## 4. Investigate evidence

Check in this order:

1. Attached direct references that were successfully read.
2. Alternative candidates from the destination-wide material library.
3. A precise material query using the place and disputed attribute.
4. Web verification for time-sensitive facts or a valuable static claim still lacking local support.

Do not use broad queries such as a destination name plus “攻略”.

## 5. Decide claim by claim

Use these verdicts:

- `supported`: evidence directly supports the current wording.
- `partial`: evidence supports the place or value but not every detail.
- `unsupported`: the investigated sources do not support the material claim.
- `contradicted`: reliable evidence conflicts with the current wording or reference.
- `ambiguous`: evidence is incomplete, mixed, or cannot be safely resolved.

Use the smallest action:

- `keep`: current wording and evidence are adequate.
- `narrow`: retain the useful core and remove or condition unsupported specificity.
- `add_source`: current claim is supportable but lacks an attached source.
- `repair_reference`: replace a wrong citation with an actually read supporting source.
- `sync_status`: current content is already reasonable and only historical state is stale.
- `rerun`: route structure or agent reasoning must be regenerated.

## 6. Check internal consistency

Before returning:

- claim count equals scope count;
- order and target paths match the bundle;
- all evidence refs were actually read;
- evidence excerpts occur in the source material or recorded external read;
- “duration unsupported” does not retain the same fixed duration;
- third-party and UGC material is not called official;
- proposed changes are minimal and stay inside the current scope.

## 7. Stop correctly

Return a completed result only after all scope items are handled. If the model call fails, the scope is incomplete, or output cannot be validated, return an execution failure and stop. Do not convert technical failure into `rerun`, `false_positive`, or `resolved`.
