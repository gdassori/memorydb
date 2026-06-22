# Why these choices — MemoryDB design rationale

> The long-form reasoning behind **MemoryDB**: an embedded knowledge substrate (relational + graph + vectors)
> for giving a local LLM memory and code understanding. Decisions are formalized as
> [Technical Decisions](decisions/) (TD-001 … TD-008) and the v0 build is tracked in
> [specs/active/v0-substrate.md](specs/active/v0-substrate.md).
>
> **The thesis in 3 lines:**
> 1. We were inspired by **AkasicDB** (unified vector+graph+relational), but its core innovation — the
>    **Traversal-Join cost model** — solves a *distributed-scale* bottleneck an **embedded** DB does not have.
> 2. So MemoryDB is a small **SQLite-based substrate** + **Python orchestration**, not a query engine.
> 3. The real leverage for code is **representation**: a deterministic symbol graph + graph-aware embeddings,
>    with **intent routing** so exact questions ("where is X used?") hit the graph, not the vectors.

---

## The decisions

| TD | Decision | Status |
|----|----------|--------|
| [TD-001](decisions/TD-001-embedded-substrate-not-distributed-tj.md) | Embedded substrate, **not** a distributed DBMS — drop the Traversal-Join cost model | accepted |
| [TD-002](decisions/TD-002-ports-and-adapters-generic-substrate.md) | Ports-and-adapters: a generic `Node`/`Edge`/`Vector` core; Code & Memory are adapters | accepted |
| [TD-003](decisions/TD-003-sqlite-single-store-recursive-cte.md) | SQLite is the whole store; recursive-CTE traversal; NetworkX on-demand only | accepted |
| [TD-004](decisions/TD-004-zero-dep-core-bruteforce-vectors.md) | Zero-dependency core; brute-force vectors default, `sqlite-vec` optional accelerator | accepted |
| [TD-005](decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md) | Multilang via tree-sitter; coarse name-based edges carry low `confidence` | accepted |
| [TD-006](decisions/TD-006-graph-aware-embeddings-staleness.md) | Graph-aware (node-context) embeddings with staleness tracking | accepted |
| [TD-007](decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md) | Intent-routed retrieval; the "TJ operator" is orchestration, not a planner | accepted |
| [TD-008](decisions/TD-008-defer-temporal-confidence-ontology-reflection.md) | Defer temporal/confidence machinery, ontology, reflection — keep the columns | proposed |
| [TD-009](decisions/TD-009-versioned-identity-for-temporal-history.md) | Temporal history in separate tables; `nodes`/`edges` keep a UNIQUE current version | accepted |

## The architecture in one picture

```
            ADAPTERS (on top)        CodeAdapter        MemoryAdapter (later)
                                     tree-sitter        entities/episodic/...
                                          │  emits generic Node/Edge
   PORTS (injected) ──────────────────────┼───────────────────────────────
   Embedder            ┌───────────────────▼──────────  SUBSTRATE CORE ─────────┐
   IntentClassifier    │ store    schema / upserts / staleness (embed_dirty)    │
   Extractor           │ query    vector_search · traverse (recursive CTE) · sql │
                       │ vector   brute-force default | sqlite-vec accelerator   │
                       │ planner  intent → LOCATE / EXPLAIN / FILTER  (= "TJ")   │
                       └─────────────────────────────────────────────────────────┘
                                    SQLite  (+ optional sqlite-vec)
```

See [research/akasicdb-2026.md](research/akasicdb-2026.md) for what AkasicDB actually is and why we diverge.
