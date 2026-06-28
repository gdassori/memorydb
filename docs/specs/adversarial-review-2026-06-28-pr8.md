# Mega adversarial review — 2026-06-28 (PR #8, `feat/graph-algorithms-networkx`)

5 finder lenses (algorithm/math · edge-cases · API/spec/consumer · test-quality · perf/SQL) → dedup → self-verified
the headline finding end-to-end. Scope: the new `src/memorydb/graph.py` (`GraphView`) + `tests/test_graph.py` +
the `__init__` export + spec, against `query.py`/`store.py`/`planner.py`/`vector.py` conventions and the
[hybrid-ranker](active/hybrid-ranker.md) consumer contract.

> **Verdict:** the core numerics are *correct* — `_pagerank_power` matches a NetworkX-style power-iteration
> reference bit-for-bit across dangling/self-loop/all-dangling cases (sums to 1), and `_degree_centrality_raw`
> matches `nx.degree_centrality` exactly. This is NOT "ship as-is", though: there is **one genuine High-severity
> correctness bug** — `shortest_path`'s `direction` parameter is wired into subgraph *construction* but never into
> the *path search*, so `direction="both"`/`"in"` silently return `None` for paths that demonstrably exist,
> directly contradicting the docstring (verified end-to-end). The rest is a coherent cluster of **Medium** items
> that will bite the *next* spec (hybrid-ranker) rather than today: the consumer has no single nx-or-fallback
> entry point and can't even obtain a `GraphView` from the `MemoryDB` facade; an unnecessary uid→id round-trip
> makes every subgraph build do a `SELECT *` (pulls `body`); whole-graph `betweenness`/`closeness` are gated only
> by the *node* ceiling (O(V·E) → effective hang); `communities()` ordering isn't pinned (the codebase is
> otherwise fastidious about determinism); and two test-integrity gaps mean the absent-`[graph]` safety and the
> lazy-import guarantee aren't actually exercised. All High/Medium findings reproduced or traced to specific lines.

## Confirmed findings

### P8-1 — High/correctness — `shortest_path(direction in {"both","in"})` silently returns `None` for paths that exist
**Location:** `src/memorydb/graph.py:178-193` — `direction` is threaded into `subgraph(...)` (`:187`, controls the
node set) but the search at `:191` always runs **directed** `nx.shortest_path` over the DiGraph; docstring promise
at `:181-182` ("`direction='both'` treats edges as undirected for reachability").

`direction` decides *which nodes are pulled into the subgraph*, not how the path is searched. For `direction="both"`
the node set is correct but `nx.shortest_path(sg, src, dst)` follows edge orientation, so an undirected-only
connection yields `NetworkXNoPath` → `None`. Reproduced end-to-end (`a→hub←b`):

```
shortest_path(a, b, direction="both") -> None      # docstring promises [a, hub, b]
nx.shortest_path(sg.to_undirected(), a, b) -> [1, 3, 2]   # the path is right there
shortest_path(z, x, direction="in")   -> None      # chain x→y→z; in-reachability requested, path exists
```

Only the default `direction="out"` is correct, which is why the one shortest-path test (default direction) passes.
**Fix:** make the search match the build — for `"both"` search `sg.to_undirected()`, for `"in"` search
`sg.reverse(copy=False)`; or drop the `direction` knob and document `shortest_path` as out-only (then remove the
undirected promise from the docstring). Add tests for `"both"` and `"in"` (see P8-9).

### P8-2 — Medium/test-integrity — the absent-`[graph]` gate and the lazy-import guarantee are not actually tested
**Location:** `tests/test_graph.py:97-113` (`test_networkx_absent_gates_only_the_nx_methods`); the missing
lazy-import test; spec claim at `graph-algorithms-networkx.md:106` ("Lazy import … asserted in tests").

Two gaps that together hollow out the central "zero-dep core" claim:
- The gate test does `monkeypatch.setattr(G, "_require_networkx", boom)` — it **replaces** the function, so the real
  body at `graph.py:29-39` (the `import networkx` failure, the actionable `pip install 'memorydb[graph]'` message,
  the `raise … from e` chaining) is never executed. It asserts `pytest.raises(ImportError)` with **no `match=`**.
  The `# pragma: no cover - exercised via monkeypatch in tests` at `graph.py:33` is misleading — the monkeypatch
  bypasses that line, it doesn't exercise it. A broken/blank message would pass.
- No test asserts `networkx not in sys.modules` after `import memorydb`, despite the spec saying it's "asserted in
  tests" (the property holds — verified — but only a throwaway shell one-liner ever checked it).

**Fix:** drive the *real* function with `monkeypatch.setitem(sys.modules, "networkx", None)` then
`pytest.raises(ImportError, match=r"memorydb\[graph\]")`, and remove the misleading pragma. Add a subprocess test
asserting `import memorydb` leaves `networkx` out of `sys.modules` (a subprocess is needed — the test session
itself imports networkx).

### P8-3 — Medium/perf — the uid→id round-trip is structurally unnecessary; `get_nodes` does `SELECT *` (pulls `body`) just to build a uid→id map
**Location:** `src/memorydb/graph.py:115-122` and `:216-221` (`store.get_nodes(ids)` → uid→id map); cost in
`store.py:165-172` (`SELECT * FROM nodes …`, materializes `body` + `json.loads(attrs)` per row); the avoidable
double `nodes` join in `query.subgraph_edges` (`query.py:126-130`).

The edges table stores **integer** `src`/`dst`. `subgraph_edges` joins `nodes` twice purely to emit `uid` strings,
then `GraphView` calls `get_nodes(ids)` to turn those uids *back* into the integer ids it already had from
`traverse` — a full id→uid→id round-trip on every `subgraph()`/`degree_centrality()` call, dragging the `body`
column (function/class source, up to 2 KB/row) and an `attrs` JSON-decode through memory only to read one `uid`.
**Fix:** add `query.subgraph_edges_by_id(store, ids)` returning integer endpoints (drop the two `nodes` joins);
have the graph path use it and delete the `get_nodes` call entirely. Leave the existing uid-returning
`subgraph_edges` for the human-readable consumers (`planner`/`context`).

### P8-4 — Medium/consumer-ergonomics — no single "centrality by seeds, nx-or-fallback" entry; `GraphView` unreachable from the facade; two confusingly-named degree methods
**Location:** `graph.py:135` (`pagerank(sg)`), `:157` (`centrality(sg, kind)`), `:208` (`degree_centrality(seeds)`);
`api.py` (no graph accessor); [hybrid-ranker.md:45,64-67,84-85].

The documented consumer wants one signal — "centrality of these candidate ids, real PageRank if `[graph]` is
present, degree fallback if not." GraphView forces it into two incompatible shapes: `pagerank(sg)` takes a *built
`nx.DiGraph`* (and `subgraph()` itself raises `ImportError` without the extra, so the no-extra branch can't even
build one), while `degree_centrality(seeds)` takes *seed ids* and rebuilds the subgraph internally. So every ranker
call site must branch on extra-presence with different argument types. Compounding it: (a) `GraphView` is exported
from `memorydb` but the `MemoryDB` facade exposes no `graph_view` (every other port — embedder, classifier,
vector_index, planner — is injectable/reachable); the ranker-in-planner integration will have to bypass the facade.
(b) `centrality(sg, kind="degree")` and `degree_centrality(seeds, depth)` are two public degree entry points with
near-identical names and different inputs, and `degree_centrality` isn't in the spec's interface block.
**Fix:** add a seed-based convenience that degrades internally, e.g. `GraphView.centrality_scores(seed_ids, depth,
prefer="pagerank")` → `{node_id: float}` (PageRank over the built subgraph when nx is present, else
`degree_centrality`); add a `graph_view` property (lazy `GraphView(self._store)`) and `graph_view=` injection on
`MemoryDB.open()`/the planner; document `degree_centrality` in the spec interface (or fold it into `centrality`).
This is the diverge-from-`make_vector_index` point too (that factory degrades *internally*; GraphView pushes the
degrade onto the caller).

### P8-5 — Medium/scale — whole-graph `betweenness`/`closeness` (`sg=None`) are gated only by the node ceiling → O(V·E) hang; the ceiling guards nodes, not edges
**Location:** `graph.py:167-175` (`centrality(sg=None, kind="betweenness"|"closeness")` → `_global_graph()` →
unbounded Brandes/all-pairs-BFS); ceiling check `_node_count()` `:225-226` enforced in `_global_graph` `:234`.

`pagerank(sg=None)` has an escape hatch (above the ceiling it degrades to `_global_degree`, `:143-148`), but the
path-based centralities have none: below the 50k-node ceiling they run the full O(V·E) (betweenness) / O(V·(V+E))
(closeness) algorithm in pure-Python NetworkX. At V=50k with even modest fan-out that is ~10¹⁰ ops — an effective
hang, not a slow query. And the ceiling bounds **node** count while every global helper (`_global_graph`,
`_global_adjacency`, `_global_degree`) iterates **all edges**; a single high-degree hub blows past the intended
memory/time budget with the node guard satisfied. The spec itself calls the 50k figure unbenchmarked.
**Fix:** give path-centralities a separate, far lower bound (or refuse `sg=None` and require an explicit bounded
subgraph, or `k`-sample); add an edge-count guard alongside the node guard; document that 50k assumes bounded
average degree (and have the eval harness pick it from measured cost).

### P8-6 — Medium/determinism — `communities()` ordering isn't pinned; whole-graph SQL reads lack `ORDER BY`
**Location:** `graph.py:203-205` (`[set(c) for c in greedy_modularity_communities(...)]`); `:238-244`, `:256-258`
(`SELECT … FROM nodes` / `FROM edges` with no `ORDER BY`).

The codebase deliberately imposes total-order tie-breaks for plan-independent, churn-invariant output (RR3-1 notes
in `query.py:84-85,99,133`; the uid tie-break in `vector.py`). `communities()` returns NetworkX's community list in
internal order, as unordered `set`s — the list order is not stably defined across the nx ≥3.0 range the extra
allows, so two runs/versions can return the same partition in a different order with no sort. The global node/edge
selects likewise rely on SQLite physical row order; final PageRank scores are addition-order-robust, but bit-level
reproducibility (which this codebase insists on elsewhere) isn't guaranteed.
**Fix:** return communities deterministically, e.g. `sorted((sorted(c) for c in comms), key=lambda c: (-len(c), c))`;
add `ORDER BY id` / `ORDER BY src, dst` to the whole-graph reads; add a determinism note matching the RR3-1
convention. (`_pagerank_power` itself *is* deterministic — it iterates the `nodes` list order — so only the
nx-backed paths drift.)

### P8-7 — Low/contract — `shortest_path(n, n)` returns `[n]` for a node that does not exist
**Location:** `graph.py:185-186` — the `src_id == dst_id` short-circuit returns `[src_id]` before any DB/graph
contact. `gv.shortest_path(999, 999)` → `[999]` on a DB with no node 999 (verified). `query.traverse` was hardened
(MR-20, `query.py:62-63`) precisely so a non-existent seed is never reported as reached; this path bypasses that.
**Fix:** existence-check (`store.id_for`/a count) before the trivial return, else `None`. Low — only bites callers
that pass unvalidated ids.

### P8-8 — Low/spec-drift — `shortest_path` silently widened the spec signature with a correctness-affecting default
**Location:** `graph.py:178-179` (`*, max_depth=6, direction="out"`) vs spec interface
`shortest_path(self, src_id, dst_id) -> list[int] | None` (`graph-algorithms-networkx.md:36`). The spec is marked
`completed` and its Implementation-notes were updated for uid↔id / power-iteration but not for these knobs.
`max_depth=6` is not mere ergonomics: a real path longer than 6 hops returns `None` (indistinguishable from "no
path"). **Fix:** update the spec signature + note the bounded-search semantics (and tie the `"out"`-only reality to
P8-1's resolution).

### P8-9 — Low/test-gaps — real coverage holes that let P8-1 (and others) slip
**Location:** `tests/test_graph.py`. Missing or weak: `shortest_path` `direction="both"`/`"in"` and the `max_depth`
bound (the gap that hid **P8-1**); `_global_adjacency` max-confidence collapse (only `_global_degree`'s pair-dedup
is tested, which is conf-agnostic); pagerank `alpha`/`max_iter`/`tol`/`weight` params and a run-twice determinism
assertion (the docstring promises "Deterministic" — untested); a self-loop pushed through a real `Store` →
`subgraph`/`degree_centrality` path (only the synthetic `_degree_centrality_raw` unit covers `+2`); disconnected
multi-component PageRank. Also the circular assertion `degraded == _global_degree()`
(`test_whole_graph_pagerank_and_ceiling_degrade:228`) compares `_global_degree()` to itself — it doesn't prove the
*degrade was triggered* (vs real PageRank) nor that the ceiling warning fired; assert `degraded != pagerank()` and
capture the warning. **Fix:** add the above; they're all cheap and several are zero-dep.

## Nits (track, don't block)

- `_log` (graph.py:21) vs `_LOG` in `vector.py`/`planner.py` — casing inconsistency.
- `communities` does a nested `from networkx.algorithms.community import …` (`graph.py:203`) after
  `_require_networkx()` — fine, minor style drift; could sit with the other lazy imports.
- `_subgraph_ids` is a connection-scoped TEMP table on the single shared `store.conn` — safe under today's
  synchronous, single-connection use; a footgun if concurrency is ever introduced. Worth a one-line note.
- `_pagerank_power` allocates a fresh `x` dict per iteration — negligible at subgraph scale; ping-pong two buffers
  only if the global path is taken seriously.
- node id `0` is never exercised (SQLite ids start at 1); a cheap `_degree_centrality_raw([0,1],[(0,1)])` unit would
  lock in that `0` is a first-class node id against any future truthiness check.

## Refuted / not-bugs (checked, deliberately keeping)

- **PageRank math:** correct. Bit-for-bit vs a power-iteration reference across dangling-sink, hub+dangling,
  all-dangling (→ uniform `1/n`), self-loop, and self-loop-only graphs (max diff ~1e-16); always sums to 1. The
  silent return of a non-converged iterate (where `nx.pagerank` would raise) is *better* for a ranking signal, and
  negative-weight filtering is unreachable (confidence ∈ (0,1]).
- **`subgraph` missing-endpoint `None`-skip** (`graph.py:118-120`): genuinely unreachable defensive code —
  `subgraph_edges` joins `_subgraph_ids` on *both* endpoints and `traverse` only returns real node ids, so no edge
  is ever dropped. Fine to keep.
- **TEMP-table cross-call contamination:** not a bug under synchronous single-connection use (DELETE+repopulate per
  call; `traverse`/`get_nodes` use `json_each`, never the temp table). See the nit above for the concurrency caveat.
- **Direct `store.conn.execute` SQL in graph.py:** matches the established convention (`planner`/`indexer`/`cli`),
  not a layering violation.
- **Degree-fallback vs PageRank scale mismatch:** the two fallbacks aren't monotonic transforms, so min-max within
  the ranker's candidate set can reorder depending on `[graph]` presence — inherent, the ranker normalizes; worth a
  doc note, not a code fix (folds into P8-4's "fallback is a coarser signal" documentation).
- **Node-count ceiling boundary / `kind` `.lower()` / `int()` coercion / parallel-relation max-collapse:** all
  verified correct.

## Suggested remediation order

Fix-now (small, self-contained): **P8-1** (the real bug) + its tests from **P8-9**, **P8-2**, **P8-7**.
Fix-with-the-ranker (shape the consumer surface before building on it): **P8-3**, **P8-4**, **P8-5**, **P8-6**.
Doc-only: **P8-8** + the scale/determinism notes.

---

# Re-review (round 2) — 2026-06-28 (remediation `54d3046..6f73b45`)

3 lenses (new control-flow · query+facade integration · test-quality) hunting **regressions introduced by the
P8 remediation**. Two lenses empirically verified by dropping the new tests onto the pre-remediation source and
running them (so "fails-on-buggy / passes-on-fixed" is measured, not argued).

> **Verdict:** the remediation **code is regression-free**. The P8-1 `shortest_path` direction fix is correct
> end-to-end for `out`/`in`/`both` (independent `nx.reverse`/`to_undirected` oracle agrees; `NodeNotFound` can't
> escape — `dst not in sg` pre-filters and views share the node set); the ceilings are correct at the boundaries
> (strict `>`, the `sg`-given path runs zero `COUNT(*)`); `subgraph_edges_by_id` is behavior-preserving vs the old
> uid path (identical edge sets) and the dropped `None`-guard is provably dead; the facade wiring is safe (no
> circular import, lazy-networkx intact, injection used-not-overwritten). The real defects are in the
> remediation's **own tests** — including, ironically, the P8-6 determinism fix being tested *vacuously*. All
> fixed in this round.

### R2-1 — Medium/test — `test_communities_order_is_deterministic` was vacuous → **fixed**
The `build()` fixture's depth-2 subgraph is a single community, so the ordering assertion was trivially satisfied
and *passed on the pre-sort code*. Replaced with two disconnected cliques (4 + 3) asserting the exact ordered list
`[[big], [small]]` — now fails on unsorted NetworkX order.

### R2-2 — Medium/test — `centrality_scores` PageRank branch had no result coverage → **fixed**
Both call sites avoided the primary path (one forced `prefer="degree"`, one ran under the absent-nx monkeypatch),
so the one-call hybrid-ranker entry never had its PageRank output validated. Added a test asserting
`centrality_scores([hub]) == pagerank(subgraph([hub]))`, ranks the hub top, and *differs* from the degree fallback.

### R2-3 — Medium/test — `MemoryDB.graph_view` facade entirely untested → **fixed**
Added `test_api.py` coverage: lazy build over `db.store`, instance caching, `RuntimeError` after `close()`, and an
injected `graph_view=` returned as-is (not overwritten).

### R2-4 — Low/robustness — `centrality_scores` swallowed *any* `ImportError` → **fixed**
The `try: subgraph(...) except ImportError: degree` caught every ImportError, so an *unrelated* broken import in
the subgraph/query path would be silently misread as "extra absent" (the same anti-pattern P8-2 criticised).
Replaced with a non-raising `_networkx_available()` probe; unrelated ImportErrors now propagate (new regression
test). Also `prefer` is now `.lower()`-folded and validated (`ValueError` on unknown), matching `centrality`'s
`kind`. Defensive: `MemoryDB.__init__` now sets `self._closed = False` first (N2 — so any property reading it is
well-defined regardless of attribute order).

### R2-5 — Low/test — remediation code paths with no coverage → **fixed**
Added tests for: the nx-DiGraph max-confidence collapse in `_add_or_max_edge` (only the SQL path was covered); a
successful whole-graph `_global_graph` build via `centrality(None, kind="betweenness")` (exercising the P8-6
`ORDER BY` build body + run-to-run determinism); and `query.subgraph_edges_by_id` directly (integer endpoints,
empty guard, out-of-set endpoint excluded).

### Confirmed regression-free (no fix needed)
PageRank/degree numerics; the `in`/`both` direction semantics; ceiling boundaries; the shared `_subgraph_ids`
TEMP-table (single-population per call, no interleave); facade import graph + lazy-networkx; injection threading.
**Suite after round 2: 259 green** (was 251). Benign non-findings: row-order differs between `subgraph_edges`
(uid-ordered) and `subgraph_edges_by_id` (id-ordered) — harmless, consumers are order-insensitive.
