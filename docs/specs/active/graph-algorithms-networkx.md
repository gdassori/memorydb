---
title: "On-demand graph algorithms via NetworkX"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-003]
components: [query, graph]
---

# On-demand graph algorithms (NetworkX)

> Load a *subgraph* from SQLite into NetworkX **on demand** to run algorithms (PageRank, centrality, shortest
> paths) that recursive CTEs do not express well — without ever making NetworkX the source of truth
> ([TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)).

## Goal

`GraphView(store).subgraph(seed_ids, depth).pagerank()` (and centrality/paths) returns scores keyed by
`node_id`, computed over a bounded subgraph pulled from the edges table. Done = ranking signals for the hybrid
ranker ([hybrid-ranker.md](hybrid-ranker.md)) come from real graph structure, computed lazily and cheaply.

## Background & constraints

SQLite + recursive CTEs handle reachability/traversal ([TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)),
but iterative algorithms (PageRank, betweenness) want an in-memory graph. NetworkX is an **optional `[graph]`
extra**; the core stays zero-dep. We always materialize a *bounded* subgraph (never the whole graph) to keep it fast.

## Data model & interfaces

```python
class GraphView:
    def __init__(self, store) -> None: ...
    def subgraph(self, seed_ids, depth: int = 2, relations=None, direction="both") -> "nx.DiGraph": ...
    def pagerank(self, sg=None, **kw) -> dict[int, float]: ...
    def centrality(self, sg=None, kind="degree") -> dict[int, float]: ...   # degree|betweenness|closeness
    def shortest_path(self, src_id: int, dst_id: int) -> list[int] | None: ...
    def communities(self, sg=None) -> list[set[int]]: ...
```

`subgraph` reuses `query.traverse` to get the node id set, then loads the induced edges via
`query.subgraph_edges`, building an `nx.DiGraph` with `relation`/`confidence` as edge attributes.

## Algorithm / step-by-step

1. `ids = [r["id"] for r in query.traverse(store, seed_ids, depth, relations, direction)]`.
2. `edges = query.subgraph_edges(store, ids)` → add nodes + edges (weight = `confidence`) to an `nx.DiGraph`.
3. Run the requested NetworkX algorithm on that bounded graph; return `{node_id: score}`.
4. (Whole-graph variant) for global PageRank, stream all edges in chunks into a DiGraph — guarded by a node-count
   ceiling, else recommend the subgraph variant.

**Worked example:** seeds = a hot module's symbols, depth 2 → ~40-node subgraph → PageRank surfaces the
`NotificationService` hub as the highest-scored node, feeding the ranker.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/graph.py` | **New** — `GraphView` (optional `[graph]` extra; import NetworkX lazily) |
| `src/memorydb/query.py` | **Reuse** — `traverse`, `subgraph_edges` (no change) |
| `pyproject.toml` | **Already** declares `[graph] = ["networkx>=3.0"]` |

## Edge cases & failure modes

- **`[graph]` extra missing:** `GraphView` raises a clear ImportError on first algorithm call (degree centrality
  has a tiny pure-Python fallback so basic ranking still works zero-dep).
- **Disconnected / single-node subgraph:** algorithms return trivial scores; never raise.
- **Huge subgraph:** node-count ceiling → fall back to degree centrality (cheap) + a warning.
- **Self-loops / parallel relations:** collapse multi-edges by max confidence for scoring.

## Test plan

- **Zero-dep:** `test_degree_fallback` — pure-Python degree centrality over a built subgraph matches expected.
- **[graph] extra (marked):** `test_pagerank_ranks_hub` — the notification graph → `send_notification`/`NotificationService`
  rank above leaves; `test_shortest_path`; `test_subgraph_bounds` (respects depth).

## Performance & scale

Cost scales with subgraph size, not the whole DB — bounded by `depth`/`relations`. PageRank on a few-hundred-node
subgraph is sub-millisecond. Whole-graph algorithms are gated behind a ceiling with a documented cost.

## Tasks

- [ ] `GraphView.subgraph` building an `nx.DiGraph` from `traverse` + `subgraph_edges`
- [ ] `pagerank` / `centrality` / `shortest_path` / `communities` wrappers
- [ ] pure-Python degree-centrality fallback (zero-dep)
- [ ] node-count ceiling + degrade-to-degree behavior
- [ ] zero-dep + [graph]-extra tests

## Open questions

- **Cache subgraph scores** per (seeds, depth)? **Lean** no for v1 (cheap); add an LRU if the ranker calls it hot.
- **Global PageRank precompute** as a periodic job writing scores onto nodes? **Lean** defer; subgraph-local is
  enough for retrieval ranking.

## Risks

- **Treating NetworkX as truth** would violate [TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)
  → `GraphView` is read-only and ephemeral; it never writes back except via an explicit, documented score-cache job.

## Review remediation (2026-06-22)

The whole-graph PageRank ceiling is **unbenchmarked** — keep it strictly behind the node-count limit (degrade to the
cheap degree-centrality fallback above it) and let the eval harness ([eval-harness.md](../completed/eval-harness.md)) measure where
the subgraph-local variant must take over. Cached scores are derived/rebuildable, per the
[TD-003 review note](../../decisions/TD-003-sqlite-single-store-recursive-cte.md).

## References

- [TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)
- [hybrid-ranker.md](hybrid-ranker.md), [v0-substrate.md](v0-substrate.md)
- NetworkX (DiGraph, pagerank, centrality).
