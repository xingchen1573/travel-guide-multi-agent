# Evidence Policy

## Evidence layers

**Current content:** the text or JSON under review. It is not self-supporting evidence.

**Direct reference:** a source currently attached to the field or nearby object. It counts only when its original chunk was successfully read.

**Alternative local candidate:** a destination-wide retrieval result. It is a candidate until the model reads the excerpt and confirms the supported scope.

**Web evidence:** a page or search result actually opened or returned by a tool. Record the exact reference and excerpt in the trace.

**History:** prior failures, model recommendations, and review events. They explain why the finding exists but do not prove the current claim.

## Source priority

For time-sensitive facts, prefer:

1. Government or attraction official sources.
2. Amap or another authoritative transport/location source for its supported field.
3. Current verified web sources.
4. Local UGC material for experience and route suggestions.
5. Third-party travel platforms and blogs as supporting context only.

Do not label third-party platforms, blogs, local media, or UGC as official.

## Missing evidence

Missing evidence is not automatically an error. Choose among:

- retain cautious advice when it is clearly framed as flexible planning guidance;
- narrow an exact duration or absolute statement;
- add or repair a source when another read source supports the claim;
- defer when evidence remains insufficient;
- rerun only when the underlying route or agent reasoning must change.

## Evidence excerpts

Use a short verbatim excerpt from the source actually used. Do not paraphrase the excerpt field. Put interpretation in `rationale`.

For a genuine negative search, use a clear marker such as `没有检索到相关素材` and record the exact search query as an evidence read.
