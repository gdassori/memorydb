---
title: "On-demand graph algorithms via NetworkX"
status: completed
created: 2026-06-22
completed: 2026-06-28
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
ranker ([hybrid-ranker.md](../active/hybrid-ranker.md)) come from real graph structure, computed lazily and cheaply.

## Background & constraints

SQLite + recursive CTEs handle reachability/traversal ([TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)),
but iterative algorithms (PageRank, betweenness) want an in-memory graph. NetworkX is an **optional `[graph]`
extra**; the core stays zero-dep. We always materialize a *bounded* subgraph (never the whole graph) to keep it fast.

## Data model & interfaces

```python
class GraphView:
    def __init__(self, store, node_ceiling=50_000, edge_ceiling=250_000, path_ceiling=2_000) -> None: ...
    def subgraph(self, seed_ids, depth: int = 2, relations=None, direction="both") -> "nx.DiGraph": ...
    def pagerank(self, sg=None, *, alpha=0.85, max_iter=100, tol=1e-6, weight="weight") -> dict[int, float]: ...
    def centrality(self, sg=None, kind="degree") -> dict[int, float]: ...   # degree|betweenness|closeness
    def shortest_path(self, src_id, dst_id, *, max_depth=6, direction="out") -> list[int] | None: ...
    def communities(self, sg=None) -> list[set[int]]: ...
    # ranking entry point + zero-dep fallback (the hybrid ranker calls centrality_scores)
    def centrality_scores(self, seed_ids, depth=2, relations=None, direction="both",
                          prefer="pagerank") -> dict[int, float]: ...   # nx PageRank, degrades to degree
    def degree_centrality(self, seed_ids, depth=2, relations=None, direction="both") -> dict[int, float]: ...
```

`shortest_path` is **bounded** (searches a subgraph seeded from `src_id`, depth `max_depth`); `direction`
drives both the node set and the search (`out`/`in`/`both`). Whole-graph (`sg=None`) PageRank degrades to
degree centrality above `node_ceiling`/`edge_ceiling`; whole-graph betweenness/closeness (O(V·E)) **raise**
above the tight `path_ceiling` — pass a bounded subgraph. `centrality_scores` is the one-call ranker entry
point: real PageRank when `[graph]` is present, degrades *internally* to the degree fallback when it is not.

`subgraph` reuses `query.traverse` to get the node id set, then loads the induced edges via
`query.subgraph_edges_by_id` (integer endpoints — no uid round-trip), building an `nx.DiGraph` with
`relation`/`confidence` as edge attributes.

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

- [x] `GraphView.subgraph` building an `nx.DiGraph` from `traverse` + `subgraph_edges`
- [x] `pagerank` / `centrality` / `shortest_path` / `communities` wrappers
- [x] pure-Python degree-centrality fallback (zero-dep)
- [x] node-count ceiling + degrade-to-degree behavior
- [x] zero-dep + [graph]-extra tests

## Implementation notes (2026-06-28)

Landed as [`src/memorydb/graph.py`](../../../src/memorydb/graph.py) (`GraphView`), tested by
[`tests/test_graph.py`](../../../tests/test_graph.py) (16 cases; 6 zero-dep + 10 `[graph]`-gated).

- **uid↔id:** `subgraph_edges` returns endpoints as *uid*; the view maps them back to integer `node_id`s
  (via `store.get_nodes`) so every score is keyed by `node_id` as specified — `query.py` was reused unchanged.
- **PageRank is pure-Python power iteration** (`_pagerank_power`), not `nx.pagerank`: NetworkX ≥3 routes
  `pagerank` through SciPy, which the lightweight `[graph]` extra (`networkx` only) deliberately does not
  pull in. The other algorithms (betweenness/closeness/communities/shortest_path) are NetworkX's own
  pure-Python implementations, so `[graph] = ["networkx>=3.0"]` covers everything as declared.
- **Centrality:** `betweenness`/`closeness` run *unweighted* — our edge `weight` is a confidence
  (similarity), not a distance, so feeding it to path-based centralities would invert them. Degree routes
  through the zero-dep `_degree_centrality_raw` so the `[graph]` and no-extra paths return identical scores.
- **Ceiling:** whole-graph (`sg=None`) PageRank above `node_ceiling` (50k) **or** `edge_ceiling` (250k) warns
  and degrades to the cheap degree fallback (built straight from SQL); `_global_graph` raises above either for
  the NetworkX kinds; the O(V·E) path-centralities raise above the tighter `path_ceiling` (2k) — see the
  2026-06-28 remediation below.
- **Lazy import:** `import memorydb` never imports NetworkX (asserted by a subprocess test); only the nx-backed
  methods do.

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

## Mega-review remediation (2026-06-28)

From the post-merge mega review ([adversarial-review-2026-06-28-pr8.md](../adversarial-review-2026-06-28-pr8.md)),
all confirmed findings remediated in [`graph.py`](../../../src/memorydb/graph.py) + tests
([test_graph.py](../../../tests/test_graph.py), now 30 cases):

- **P8-1 (High, fixed):** `shortest_path`'s `direction` is now wired into the *search*, not just subgraph
  construction — `both` searches the undirected view, `in` the reversed view, `out` the directed graph. Was
  silently returning `None` for existing `both`/`in` paths. Regression test added.
- **P8-2 (fixed):** the ImportError gate now drives the *real* `_require_networkx` with NetworkX hidden via
  `sys.modules` (asserting the actionable message), and a subprocess test asserts `import memorydb` never loads
  NetworkX — closing the gap behind the "asserted in tests" claim.
- **P8-3 (fixed):** added `query.subgraph_edges_by_id` (integer endpoints) and dropped the id→uid→id round-trip;
  `subgraph`/`degree_centrality` no longer do a `SELECT *` (`get_nodes`) just to remap uids.
- **P8-4 (fixed):** added `centrality_scores(seed_ids, …, prefer=)` — the one-call ranker entry that degrades
  *internally* (PageRank-or-degree) — and a `graph_view` property + `open(graph_view=…)` injection on the
  `MemoryDB` facade, so the ranker reaches centrality without branching on the extra.
- **P8-5 (fixed):** path-based centralities get a tight `path_ceiling`; whole-graph builds add an `edge_ceiling`
  (the node ceiling alone didn't bound O(E)/O(V·E)).
- **P8-6 (fixed):** `communities()` returns a deterministic order (largest-first, then sorted members); whole-graph
  SQL reads got `ORDER BY`.
- **P8-7 (fixed):** `shortest_path(n, n)` returns `None` for a non-existent node (mirrors traverse's MR-20).
- **P8-9 (fixed):** added coverage for direction `both`/`in`, the `max_depth` bound, `_global_adjacency`
  max-confidence collapse, pagerank `alpha`/`max_iter`/determinism, self-loop through a real `Store`, disconnected
  components, the path/edge ceilings, int-key consumer fit, and node id `0`; the circular degrade assertion now
  also asserts the warning fired and that the degrade differs from true PageRank.

A second **re-review round** (regression hunt on the remediation, see the doc's "Re-review (round 2)") confirmed
the code regression-free and closed test gaps it surfaced (R2-1..R2-5): a *vacuous* communities-determinism test
(single-community fixture) replaced with a two-clique one; result-level coverage for `centrality_scores`' PageRank
branch and the `MemoryDB.graph_view` facade; `centrality_scores` now probes `_networkx_available()` instead of
swallowing every `ImportError`, and validates/case-folds `prefer`. Suite: 259 green.

## References

- [TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)
- [hybrid-ranker.md](../active/hybrid-ranker.md), [v0-substrate.md](../active/v0-substrate.md)
- NetworkX (DiGraph, pagerank, centrality).
