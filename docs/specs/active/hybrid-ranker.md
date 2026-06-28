---
title: "Hybrid ranker — fuse vector, graph, confidence & recency"
status: planned
created: 2026-06-22
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
3. `centrality`: `GraphView(store).pagerank(subgraph)` ([graph-algorithms-networkx.md](../completed/graph-algorithms-networkx.md));
   fallback to degree centrality if `[graph]` absent.
4. `confidence`: mean confidence of edges incident to the node.
5. `recency`: from the owning `file` node's `attrs.mtime`.
6. Normalize, weight, sum → `Scored`; sort desc (deterministic tie-break by node_id).

**Worked example:** query "notifications": `send_notification` wins (high cosine + high centrality), beating a
semantically-similar but leaf utility function that vectors alone would tie it with.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/ranker.py` | **New** — `HybridRanker`, `RankWeights`, `Scored` |
| `src/memorydb/planner.py` | **Modify** — `_explain` ranks expanded nodes via `HybridRanker` before context building |

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

- [ ] signal extractors (cosine, centrality, confidence, recency) with normalization
- [ ] weighted fusion + `breakdown` + deterministic sort
- [ ] degree-centrality fallback when `[graph]` is absent
- [ ] planner integration in `_explain`
- [ ] zero-dep tests (hub / recency / breakdown / missing-signals / determinism)

## Open questions

- **Learn the weights** from eval data vs hand-tuned defaults? **Lean** hand-tuned for v1; expose them so the
  eval harness ([eval-harness.md](../completed/eval-harness.md)) can grid-search later.
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

## References

- [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md), [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)
- [graph-algorithms-networkx.md](../completed/graph-algorithms-networkx.md), [context-builder-packing.md](../completed/context-builder-packing.md), [eval-harness.md](../completed/eval-harness.md)
