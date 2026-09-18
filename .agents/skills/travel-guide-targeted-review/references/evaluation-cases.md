# Evaluation Cases

Use isolated fixtures or frozen project copies. Never forward-test by applying a patch to a live travel project.

## Required cases

1. **Wrong attached citation, supporting alternative exists**
   - Expected: `repair_reference` or `add_source`.
   - Failure: delete the place or mark the whole claim unsupported without reading alternatives.

2. **Place supported, fixed duration unsupported**
   - Expected: `partial + narrow`, retain the place and use flexible wording.
   - Failure: keep the same duration after saying it lacks evidence.

3. **Content and citation supported**
   - Expected: `supported + keep`.

4. **Historical state residue only**
   - Expected: `sync_status`, no route rerun.

5. **No sufficient local or web evidence**
   - Expected: `ambiguous`, defer or request evidence.
   - Failure: invent facts or remove a useful point by default.

6. **Reliable evidence contradicts a narrow detail**
   - Expected: `contradicted`, change only the conflicting detail.

7. **Route structure is invalid**
   - Expected: `rerun` with the smallest agent/day scope.

8. **Partial model response**
   - Input scope: 12 items; output: 3 items.
   - Expected: validator rejection or merge into the full 12-item projection.

9. **Provider failure**
   - Input: 504, timeout, invalid JSON, or auth failure.
   - Expected: execution failure; finding disposition unchanged.

10. **System defect discovered**
    - Expected: a structured system observation with suggested fix and regression test, while the content claim remains independently reviewed.

## Acceptance signals

- 100% scope coverage and target-path alignment.
- Every evidence ref is traceable to the bundle or trace.
- No business file mutation during review.
- No destination-specific rules in the skill.
- Human reviewers can decide each claim independently.
