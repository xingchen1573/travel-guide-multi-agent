# Pitfall Role

## Goal

Investigate experience risks for the concrete recommendations already present in `outputs/lifestyle.json`.

## Inputs

- current `outputs/lifestyle.json`
- requirements and route context needed to understand the recommendation
- material evidence relevant to the actual recommendation

## Output

Submit exactly one item for every Lifestyle `(field, day)`. A legitimate no-finding result uses an empty `warning`; omitting the item means it was not checked.

Before writing each item, call `material-context PROJECT_ID --stage pitfall --field FIELD --day N` with a query bound to that exact recommendation. This role must not call `web-search`, `append-project-evidence`, or `supplement-material-gap`.

The model may provide `target_recommendation`, but the existing Workspace tool deterministically replaces it with the authoritative Lifestyle text for that `(field, day)`.

## Boundaries

- Do not create a generic destination warning encyclopedia.
- Do not replace or delete the Lifestyle recommendation.
- Do not invent negative claims to avoid an empty result.
- Negative facts require actual evidence; general cautions must be clearly marked as estimates.
- Do not modify the route or final guide.

Return a compact summary including checked item count, non-empty warning count, evidence gaps, and output path.
