---
title: "v0 substrate + retrieval planner (and a multilang CodeAdapter)"
status: active
created: 2026-06-22
author: claude
related_tds: [TD-002, TD-003, TD-004, TD-005, TD-006, TD-007, TD-008]
components: [store, schema, models, query, vector, ports, planner, embedders, adapters/code]
---

# v0 substrate + retrieval planner

## Goal

Implement the **domain-agnostic substrate** ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)) over a single SQLite store ([TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)) with a **zero-dependency core** ([TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)), plus the **intent-routed retrieval planner** ([TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)). Definition of done: build a tiny graph, embed nodes, and answer `LOCATE` / `EXPLAIN` / `FILTER` — with **tests passing using only the Python stdlib**. The multilang `CodeAdapter` ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)) and real graph-aware embeddings ([TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)) land on top once the `[code]` extra is installed.

## Approach

`Store` owns a SQLite connection and the schema. `Node`/`Edge` are plain dataclasses; adapters upsert them. `query.py` holds the three primitives — `vector_search`, `traverse` (recursive CTE), `references_to` (graph-exact LOCATE) — plus `subgraph_edges`. `vector.py` provides the `VectorIndex` interface with a pure-Python `BruteForceVectorIndex` default and a stub for the `sqlite-vec` accelerator. `ports.py` declares the `Embedder` / `IntentClassifier` / `Extractor` Protocols. `planner.py` wires it together and routes by intent. `embedders.py` ships a deterministic, dependency-free `HashingEmbedder` so the substrate runs offline. The substrate maintains the `embed_dirty` staleness flag on every node/edge upsert.

## What Changes

| File | Change |
|------|--------|
| `src/memorydb/schema.sql` | **New** — `nodes`, `edges`, `embeddings` tables + indexes; metadata columns (`valid_from`/`valid_to`/`confidence`/`source`, `embed_dirty`) per [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md) |
| `src/memorydb/models.py` | **New** — `Node`, `Edge` dataclasses; `Intent` enum; `Rel` relation constants |
| `src/memorydb/store.py` | **New** — `Store`: connect/apply-schema, `upsert_node`, `upsert_edge` (by uid, marks endpoints dirty), `set_embedding`, `get_nodes`, `id_for`, `dirty_nodes`, `transaction()` |
| `src/memorydb/vector.py` | **New** — pack/unpack float32 BLOBs; `BruteForceVectorIndex` (default); `SqliteVecIndex` stub (optional `[vector]`) |
| `src/memorydb/query.py` | **New** — `vector_search`, `traverse` (recursive CTE, out/in/both + relation filter), `subgraph_edges`, `references_to` |
| `src/memorydb/ports.py` | **New** — `Embedder` / `IntentClassifier` / `Extractor` Protocols |
| `src/memorydb/planner.py` | **New** — `DefaultIntentClassifier` (regex) + `RetrievalPlanner.retrieve()` routing LOCATE/EXPLAIN/FILTER |
| `src/memorydb/embedders.py` | **New** — `HashingEmbedder` (deterministic, dependency-free placeholder) |
| `src/memorydb/__init__.py` | **New** — public exports |
| `src/memorydb/adapters/code/__init__.py` | **New (stub)** — placeholder for the tree-sitter `CodeAdapter` ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)) |
| `pyproject.toml` | **New** — package metadata + optional extras `[vector]`/`[code]`/`[graph]`/`[dev]` |
| `tests/test_substrate.py` | **New** — build a notification graph, embed, assert LOCATE/EXPLAIN/traverse/staleness |
| `README.md` | **New** — what MemoryDB is, the embedded thesis, quickstart |

## Tasks

- [x] `schema.sql` + `Store` (connect, schema, upserts, staleness on edge upsert)
- [x] `models.py` (`Node`, `Edge`, `Intent`, `Rel`)
- [x] `vector.py` (pack/unpack, `BruteForceVectorIndex`; `SqliteVecIndex` stub)
- [x] `query.py` (`traverse` recursive CTE, `references_to`, `subgraph_edges`, `vector_search`)
- [x] `ports.py` + `embedders.py` (`HashingEmbedder`)
- [x] `planner.py` (intent routing LOCATE/EXPLAIN/FILTER)
- [x] `pyproject.toml` + `__init__.py` + `README.md`
- [x] `tests/test_substrate.py` green with **zero third-party deps** (5 tests, script + pytest)
- [ ] **Pending `[code]` extra:** real `CodeAdapter` (tree-sitter-language-pack) — coarse multilang edges with `confidence < 1.0`
- [ ] **Pending `[code]` extra:** graph-aware neighborhood serialization driving re-embedding of `embed_dirty` nodes

## Open Questions

- **Symbol uid scheme:** fully-qualified name (`pkg.module.Class.method`) as the stable `uid`? Good for Python; for coarse multilang, how do we make it stable across re-index? Lean FQN + file-path fallback.
- **Vector storage when `sqlite-vec` is present:** keep the float32 BLOB in `embeddings` *and* mirror into a `vec0` virtual table, or make `vec0` authoritative? Lean: BLOB authoritative, `vec0` as an index rebuilt from it.
- **Intent classifier default:** how aggressive should the `LOCATE` regex be before it steals `EXPLAIN` traffic? Default ambiguous → `EXPLAIN`.
- **`traverse` direction default for EXPLAIN:** `both` (richer context) vs `out` (dependencies only). Lean `both`, depth 2.

## Risks

- **Recursive-CTE correctness/termination** with bidirectional expansion + relation filters. Mitigate: dedup on `(id)` with `MIN(depth)`, hard depth bound, unit tests on a known graph.
- **Brute-force vector cost** at scale. Mitigate: interface boundary — swap to `sqlite-vec` via the `[vector]` extra ([TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)).
- **Coarse multilang edges mislabeled as truth.** Mitigate: `confidence < 1.0` + planner downweighting ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)); never surface heuristic edges as exact in `LOCATE`.

## Review remediation (2026-06-22)

Post-review hardening landed in v0 (tested): `Store.upsert_edge` keeps **confidence monotonic** (`MAX(old, new)`) so a
precise edge is never downgraded; `query.node_neighborhood` was added for the embedding serializer; and `LOCATE` now
**grounds the symbol against the index and reports ambiguity** (`ambiguous` / `matched_uids`) — so "exact LOCATE" means
exact *given a resolved uid*, with bare-name collisions surfaced rather than silently merged (C4). See
[adversarial-review-2026-06-22.md](../adversarial-review-2026-06-22.md).
