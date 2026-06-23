---
title: "Public API facade — the MemoryDB class"
status: completed
created: 2026-06-22
completed: 2026-06-22
author: claude
related_tds: [TD-002]
components: [api, store, planner, indexer]
---

# Public API facade — `MemoryDB`

> A single ergonomic entry point that wires the substrate, an adapter, an embedder, the indexer, and the
> planner together — so users write `db.index(path)` / `db.ask("…")` instead of assembling the parts. Thin
> orchestration over the existing pieces ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)).

## Goal

`MemoryDB.open(path, embedder=...)` returns a configured instance; `index`, `ask`, `locate`, `explain`, and
`context` cover the common flows. Done = the README quickstart works through this facade, and every dependency
(embedder, classifier, vector index) is overridable.

## Background & constraints

v0 exposes `Store`, `RetrievalPlanner`, `query`, `HashingEmbedder` separately. The facade must not hide the
ports ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)) — it composes them with sane
defaults and lets callers inject real ones. It owns no new storage logic.

## Data model & interfaces

```python
class MemoryDB:
    @classmethod
    def open(cls, path: str = ":memory:", *, embedder=None, extractors=None,
             classifier=None, vector_index=None) -> "MemoryDB": ...

    # ingestion
    def index(self, root: str) -> "IndexReport": ...
    def refresh_embeddings(self) -> "EmbedReport": ...

    # retrieval
    def ask(self, query: str, *, k: int = 5, depth: int = 2, as_context: bool = False,
            budget_tokens: int = 2000): ...        # routes by intent (LOCATE/EXPLAIN/FILTER)
    def locate(self, symbol: str) -> list[dict]: ...
    def explain(self, query: str, *, k=5, depth=2) -> dict: ...
    def context(self, query: str, *, budget_tokens=2000) -> "ContextResult": ...

    # low-level escape hatches
    @property
    def store(self): ...
    @property
    def planner(self): ...
    def close(self) -> None: ...
```

Defaults: `embedder = HashingEmbedder()` (with a loud note to swap for a real model), `vector_index =
make_vector_index(store)` ([sqlite-vec-acceleration.md](sqlite-vec-acceleration.md)), `classifier =
DefaultIntentClassifier()`, `extractors = ExtractorRegistry.default()`.

## Algorithm / step-by-step

1. `open` constructs the `Store` (runs migrations), builds the vector index (ANN if available), the embedding
   pipeline, the indexer, and the planner — injecting any provided ports.
2. `index(root)` delegates to the `Indexer`, then `refresh_embeddings()`.
3. `ask(query)` runs the planner; if `as_context`, pipes the result through `ContextBuilder`.
4. `locate`/`explain`/`context` are typed conveniences over the planner + builder.

**Worked example:**
```python
db = MemoryDB.open("repo.db", embedder=MyEmbedder())
db.index("~/src/orbital")
print(db.ask("where is send_notification used?"))     # LOCATE
print(db.context("how do notifications work?"))         # packed EXPLAIN
```

## What changes

| File | Change |
|------|--------|
| `src/memorydb/api.py` | **New** — `MemoryDB` facade |
| `src/memorydb/__init__.py` | **Modify** — export `MemoryDB` as the headline symbol |
| `README.md` | **Modify** — quickstart uses `MemoryDB` |

## Edge cases & failure modes

- **No embedder provided:** default `HashingEmbedder` + a one-time warning (not production-quality).
- **`ask` before `index`:** empty results, not an error.
- **Closing twice / use-after-close:** guard with a clear error.
- **`:memory:` reuse across processes:** documented as single-process only.

## Test plan

Zero-dep:

- `test_open_index_ask` — open `:memory:`, index a fixture via a fake extractor, `ask` LOCATE/EXPLAIN.
- `test_ports_overridable` — inject a fake classifier/embedder and assert they are used.
- `test_context_budget` — `context()` respects the token budget.
- `test_defaults_present` — `open()` with no args yields a working instance.

## Performance & scale

Pure delegation; no overhead beyond the underlying components. Construction is cheap; `index`/`ask` costs are
those of the indexer/planner.

## Tasks

- [x] `MemoryDB.open` wiring with injectable ports + sane defaults
- [x] `index` / `refresh_embeddings` / `ask` / `locate` / `explain` / `context`
- [x] export from `__init__`; update README quickstart
- [x] zero-dep facade tests

## Implementation notes (2026-06-22)

- `src/memorydb/api.py` — `MemoryDB` facade, `ExtractorRegistry.default()` (degrades to no extractor
  when the `[code]` extra is absent), and `ContextResult` + a **placeholder** token-budgeted packer
  (`_pack_*`, ≈4 chars/token). The dedicated [context-builder-packing](context-builder-packing.md)
  spec will supersede the packer behind the same `ContextResult` shape.
- Added `make_vector_index(store)` (vector.py) — returns `SqliteVecIndex` when buildable, else
  `BruteForceVectorIndex`; forward-compatible with the sqlite-vec spec.
- Promoted the planner's intent handlers to public `locate()` / `explain()` so the facade composes
  them without reaching into privates; `retrieve()` dispatches to them.
- `Store.get_meta` / `set_meta` back the C3 dim/model compatibility check; `open()` records
  `embed_model` / `embed_dim` and warns on a mismatch.
- `index()` builds the Indexer with `embedder=None` and embeds via `refresh_embeddings()` so
  embedding happens in exactly one place (spec step 2).

## Open questions

- **Sync vs async API**? **Lean** sync for v1 (embedded, single-process); an async wrapper later if needed.
- **Config object vs kwargs** for `open`? **Lean** kwargs now; a `MemoryDBConfig` dataclass if options grow.

## Risks

- **Facade hiding the ports** would undercut [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)
  → keep `store`/`planner` escape hatches and keep every default overridable.

## Review remediation (2026-06-22)

- **Embedder/vec dim (C3):** with `[vector]` active, `vec_items` is built at the embedder's dim on the first embedding,
  and `open()` records that dim in `meta`. The default `HashingEmbedder` (dim 64) and a real model must not share a
  store without a `rebuild_index` — `open()` should warn on a dim/model mismatch.
- **Return type:** `ask(as_context=True)` returns a `ContextResult`; `as_context=False` returns the raw result dict —
  document the union (or split into `ask`/`ask_context`).

## References

- [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)
- [cli.md](cli.md), [context-builder-packing.md](context-builder-packing.md), [indexer-ingestion-pipeline.md](indexer-ingestion-pipeline.md)
