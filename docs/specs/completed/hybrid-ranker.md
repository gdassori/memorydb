---
title: "Hybrid ranker — fuse vector, graph, confidence & recency"
status: completed
created: 2026-06-22
completed: 2026-06-28
author: claude
related_tds: [TD-006, TD-007]
components: [planner, query, graph]
---

# Hybrid ranker

> Combine the signals MemoryDB already has — vector similarity, graph centrality, edge confidence, and recency
> — into one ranking, so retrieval surfaces the *structurally important and semantically relevant* nodes, not
> just the nearest vectors ([TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)).

## Goal

`HybridRanker.rank(candidates, query_vec)` returns candidates ordered by a transparent, weighted score. Done =
EXPLAIN results put hubs and high-confidence, recently-touched, semantically-close nodes on top; weights are
configurable and the contribution of each signal is inspectable.

## Background & constraints

Pure vector ranking is role-blind; pure graph ranking ignores the query. The fusion must be cheap (runs per
query over a bounded candidate set), explainable (return the per-signal breakdown), and degrade gracefully when
a signal is unavailable (e.g. no `[graph]` extra → centrality = degree fallback; no embedding → score 0).

## Data model & interfaces

```python
@dataclass
class RankWeights:
    vector: float = 0.45
    centrality: float = 0.25
    confidence: float = 0.15
    recency: float = 0.15

@dataclass
class Scored:
    node_id: int
    score: float
    breakdown: dict[str, float]      # per-signal contributions (explainability)

class HybridRanker:
    def __init__(self, store, graph_view=None, weights: RankWeights | None = None,
                 half_life_days: float = 30.0) -> None: ...
    def rank(self, candidate_ids: list[int], query_vec, depth: int = 2) -> list[Scored]: ...
```

## Scoring

For each candidate, normalize each signal to [0,1] then combine:
```
score = w.vector*cos_sim
      + w.centrality*pagerank_norm        # from GraphView over the candidate subgraph
      + w.confidence*mean_incident_conf   # avg edge confidence touching the node
      + w.recency*exp(-age_days/half_life) # file mtime from attrs
```
Normalization: min-max within the candidate set for centrality; cosine is already [-1,1]→[0,1]; recency is the
exponential decay above. `breakdown` keeps each term for debugging and the eval harness.

## Algorithm / step-by-step

1. Gather candidates (seeds + traversal expansion from the planner).
2. `cos_sim`: from the vector index scores (or compute against `query_vec`).
3. `centrality`: `GraphView(store).pagerank(subgraph)` ([graph-algorithms-networkx.md](graph-algorithms-networkx.md));
   fallback to degree centrality if `[graph]` absent.
4. `confidence`: mean confidence of edges incident to the node.
5. `recency`: from the owning `file` node's `attrs.mtime`.
6. Normalize, weight, sum → `Scored`; sort desc (deterministic tie-break by node_id).

**Worked example:** query "notifications": `send_notification` wins (high cosine + high centrality), beating a
semantically-similar but leaf utility function that vectors alone would tie it with.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/ranker.py` | **New** — `HybridRanker`, `RankWeights`, `Scored` (pydantic, TD-010) |
| `src/memorydb/planner.py` | **Modify** — `explain` attaches `ranking`/`scored` (additive); ctor accepts injectable `graph_view`/`ranker` |
| `src/memorydb/context.py` | **Modify** — `_build_explain` prefers `result["ranking"]` when present (else the seed/depth proxy) |
| `src/memorydb/eval/__init__.py` | **Modify** — `_explain_ranking` prefers `result["ranking"]` so eval metrics measure the ranker |
| `src/memorydb/api.py` | **Modify** — `MemoryDB` shares one `GraphView` with the planner; `open(graph_view=, ranker=)` injection |
| `src/memorydb/__init__.py` | **Modify** — export `HybridRanker`, `RankWeights`, `Scored` |

## Edge cases & failure modes

- **No embedding for a node:** `cos_sim = 0` (still rankable by graph signals).
- **`[graph]` extra missing:** centrality via degree fallback (zero-dep).
- **No mtime:** recency term = neutral 0.5 (don't penalize unknown age).
- **Single candidate / all-equal signals:** stable order by node_id.
- **Weight misconfig (sum ≠ 1):** normalize weights at construction; warn.

## Test plan

Zero-dep (degree fallback + `HashingEmbedder`):

- `test_hub_outranks_leaf` — hub node with same cosine as a leaf ranks higher (centrality).
- `test_recency_breaks_ties` — equal vector+graph, newer file wins.
- `test_breakdown_sums` — `score ≈ sum(breakdown.values())` within float tolerance.
- `test_missing_signals_safe` — no embedding / no mtime → no crash, sensible order.
- `test_deterministic` — stable order across runs.

## Performance & scale

O(c) over the candidate set `c` plus one bounded PageRank; both small per query. Centrality dominates but is
sub-ms on a depth-2 subgraph. Weights/half-life are constants (tunable via the eval harness).

## Tasks

- [x] signal extractors (cosine, centrality, confidence, recency) with normalization
- [x] weighted fusion + `breakdown` + deterministic sort
- [x] degree-centrality fallback when `[graph]` is absent
- [x] planner integration in `_explain`
- [x] zero-dep tests (hub / recency / breakdown / missing-signals / determinism)

## Implementation notes (2026-06-28)

Landed as [`src/memorydb/ranker.py`](../../../src/memorydb/ranker.py) (`HybridRanker`, `RankWeights`,
`Scored`), tested by [`tests/test_ranker.py`](../../../tests/test_ranker.py) (14 cases; zero-dep + one
`[graph]`-gated PageRank-path test).

- **Centrality** comes from [`GraphView.centrality_scores(ids, depth)`](graph-algorithms-networkx.md) — real
  PageRank when `[graph]` is present, degree fallback otherwise — so the ranker never branches on the extra and
  stays zero-dep. Tests force the degree path (`_networkx_available → False`) to be env-independent.
- **`breakdown` holds the *weighted* contributions** (so `score == sum(breakdown.values())` exactly) — the
  explainability contract. `RankWeights` normalizes to sum 1 at construction (warns) and rejects a non-positive sum.
- **Cosine** is each candidate's stored unit-embedding dotted with the normalized query, clamped to `[0,1]`
  (a negative/orthogonal cosine, like a missing embedding, contributes 0 — consistent with the spec's "no
  embedding → 0"). Only same-dim vectors are scored (mirrors `BruteForceVectorIndex`).
- **Recency** reads the symbol's denormalized `attrs.mtime` else the owning file node's mtime via the indexed
  `file_uid` (C5), with `now` injectable so scoring is reproducible; unknown mtime → neutral 0.5.
- **Integration:** `RetrievalPlanner.explain` attaches `ranking` (ordered node_ids) + `scored` (breakdowns)
  **additively** — a ranker hiccup degrades to the unranked result, and the `ContextBuilder` prefers `ranking`
  when present (else its seed/depth proxy). LOCATE/FILTER are exact and bypass the ranker (TD-007). The planner
  and `MemoryDB` accept an injectable `graph_view`/`ranker` (else built lazily over the store).

## Open questions

- **Learn the weights** from eval data vs hand-tuned defaults? **Lean** hand-tuned for v1; expose them so the
  eval harness ([eval-harness.md](eval-harness.md)) can grid-search later.
- **Per-intent weights** (LOCATE vs EXPLAIN)? **Lean** EXPLAIN uses the full fusion; LOCATE bypasses the ranker
  (it is exact, [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)).

## Risks

- **Opaque ranking** erodes trust → always return `breakdown`; the API can expose it.
- **Over-weighting centrality** buries niche-but-correct hits → defaults keep vector as the largest weight.

## Review remediation (2026-06-22)

- **Recency source (C5):** read the owning file's mtime via the symbol's `attrs.file_uid` (or the denormalized
  `attrs.mtime`), not an undefined file join.
- **Normalization guard:** min-max over the candidate set **divides by zero** when there is a single candidate or all
  scores are equal — guard with `range == 0 → contribution 0.5` (neutral) and keep the deterministic uid tie-break.
  Add `test_single_candidate` and `test_all_equal_scores` to the plan.

## Mega-review remediation (2026-06-29)

From the post-merge mega review ([adversarial-review-2026-06-29-pr9.md](../adversarial-review-2026-06-29-pr9.md)),
all confirmed findings remediated (suite: 284 green, 25 ranker cases):

- **P9-1 (High, test):** the headline hub test used an *out-only* hub, which ties the leaf under real PageRank
  (PageRank elevates *called-by-many*, not *calls-many*) — it only passed via the forced degree fallback. Rebuilt
  with an **in-degree** hub (callers → hub) so it dominates under both, plus a non-forced `@graph` PageRank variant.
- **P9-2 / P9-3 (High, test):** the all-equal tie-break test was a tautology (ascending input + stable sort) — now
  feeds descending ids; the context-prefers-ranking test couldn't tell ranking from the proxy (both headed with the
  same node) — now asserts the full order *and* that it diverges from the proxy.
- **P9-4 (integration):** `MemoryDB` now builds one `GraphView` and shares it with the planner's ranker (no more two
  views per store); `open(graph_view=, ranker=)` makes the injection end-to-end.
- **P9-5 (integration):** the eval harness `_explain_ranking` now prefers `result["ranking"]`, so EXPLAIN metrics
  actually measure the hybrid ranker (was computing its own seed/uid order).
- **P9-6 (reproducibility):** `rank`'s `now` defaults to the **corpus's newest mtime** (deterministic, corpus-
  relative recency) instead of wall-clock, so ranking is reproducible run-over-run for the eval harness.
- **P9-7 (consistency):** `RankWeights`/`Scored` are now pydantic `BaseModel`s (TD-010) — `Field(ge=0)` rejects a
  negative weight (P9-12), the planner emits `Scored.model_dump()`.
- **P9-8 / P9-9 (robustness):** a malformed `attrs.mtime` degrades that node to neutral recency (was crashing the
  whole rank); the half-life is re-validated at point of use (tolerates a post-construction mutation to 0/negative).
- **P9-11:** a self-loop is counted once in the confidence mean (the dst arm excludes `src == dst`).
- **P9-13:** added coverage for varying edge confidence, custom half-life, a symbol's denormalized mtime, the
  negative-cosine clamp, dedupe, dim-mismatch, the planner degrade path, and the reproducible default `now`.
- **P9-10 (accepted):** a non-existent candidate id is scored neutral (unreachable via the planner; documented).

## References

- [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md), [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)
- [graph-algorithms-networkx.md](graph-algorithms-networkx.md), [context-builder-packing.md](context-builder-packing.md), [eval-harness.md](eval-harness.md)
