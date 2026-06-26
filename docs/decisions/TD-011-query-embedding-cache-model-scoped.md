---
id: TD-011
title: "Query embeddings are cached in-memory (with an optional binary dump), scoped to the embedding model not the store"
status: accepted
date: 2026-06-26
supersedes: null
superseded_by: null
tags: [embeddings, cache, performance, retrieval, ports]
---

# TD-011: In-memory query-embedding cache, model-scoped

## Context

Every EXPLAIN / `ask` query re-embeds the query text in the planner
([planner.explain](../../src/memorydb/planner.py): `qvec = self.embedder.embed([query])[0]`). With the
default `HashingEmbedder` that is cheap, but with a **real model** (the production case — Carbon injects
the `Embedder` port, [TD-002](TD-002-ports-and-adapters-generic-substrate.md)) the embed call is the
**dominant per-query cost**: a forward pass or a network round-trip. Repeated questions, pagination over
the same query, an eval-harness sweep, or an agent re-asking all pay it every time.

Crucially, the query→vector mapping is a **pure function of `(embedding model, query text)`**. It does
**not** depend on the store: the same model maps `"where do we handle retries?"` to the same vector
regardless of which graph it is searched against. The intent classifier already caches its verdict by
query string ([TD-007](TD-007-intent-routed-retrieval-tj-is-orchestration.md), `LLMIntentClassifier.cache`);
the query embedding is the other recomputed-every-call piece.

## Decision

Add a small **in-memory** cache mapping `query text → embedding vector`, with these properties:

- **Model-scoped, NOT store-scoped.** The cache is keyed/validated by the **embedding model identity**
  (`model` id + `dim`). A model change invalidates it. It is *not* keyed by the DB — the same
  `query → vector` is valid for any store that uses that model, so the cache is a property of the
  **model**, not the data.
- **Clearable and rewritable.** `clear()` empties it; entries can be overwritten and the whole cache can
  be replaced/injected (pre-warm, share, or reset on demand).
- **Bounded.** Oldest-evicted (insertion order), a fixed `max_entries`, mirroring the intent cache — caps
  memory on a long-running process.
- **Optionally persisted as a compact binary dump.** Beyond the in-memory map, the cache can be flushed
  to / loaded from a single **flat binary file** — a direct dump of the hashmap, **no `pickle`/`marshal`/
  JSON**. A cold start becomes warm. The file is **model-validated** (a header records `model_id` + `dim`;
  a mismatch — or a short/corrupt read — is ignored, never loaded), so persistence does **not** weaken the
  model-scoping. The node-embedding BLOBs remain the authoritative *substrate* vectors
  ([TD-004](TD-004-zero-dep-core-bruteforce-vectors.md)); this file is a **disposable** hot-query
  accelerator (delete it any time → at worst a cold start).

- **Keyed by `sha256(query)`, not the raw text.** The lookup hashes the query to a fixed **32-byte**
  digest; that digest is the map key and the on-disk key. This gives fixed-width records (a clean,
  fixed-stride binary dump — no per-key length prefix), keeps the **raw queries off disk** (only hashes
  persist), and reuses the substrate's existing sha256 convention (the indexer hashes file content the
  same way). sha256 collisions are cryptographically negligible.

```python
class QueryEmbeddingCache:
    def __init__(self, model_id: str, dim: int | None, max_entries: int = 512) -> None: ...
    def get(self, query: str) -> list[float] | None: ...     # hashes query -> sha256; None on miss/mismatch
    def put(self, query: str, vec: list[float]) -> None: ... # rewritable; key = sha256(query)
    def clear(self) -> None: ...
    def load(self, path: str) -> int: ...                    # warm from a binary dump (model-validated)
    def dump(self, path: str) -> int: ...                    # flush the hashmap to the binary file
    # On an embedder whose (model_id, dim) != the cache's, the cache resets — never returns a
    # vector from a different model.
```

## Persistence format (binary dump)

A single flat file, written by dumping the hashmap key-by-key — **no object serializer** (`pickle` /
`marshal` / JSON). Because keys are fixed-size sha256 digests and values are fixed-size float32 vectors,
every record is the same width, so the file is a header + a tight array of records:

```
  magic    "MQEC"              4 bytes            (MemoryDB Query Embedding Cache)
  version  u8                  1 byte
  dim      u32  LE             vector dimension D
  mlen     u16  LE             length of model_id
  model    mlen UTF-8          the embedding model id  (the scoping key)
  count    u32  LE             number of records
  count × record  (fixed stride = 32 + 4*D bytes):
     key     32 bytes          sha256(query)  (raw digest, not hex)
     vector  D × float32 LE    array('f').tobytes()  ==  the substrate's pack()
```

- **Load** validates `magic`/`version`, then requires `model == embedder.model` and `dim == embedder.dim`;
  on any mismatch — or a short/corrupt/truncated read — the file is **ignored** and the cache starts empty
  (never a crash, never a cross-model vector). Records can even be `mmap`-ed / random-accessed thanks to the
  fixed stride.
- **Dump** is an O(n) append of records to a temp file, then an atomic rename — so a crash mid-write never
  corrupts a good cache.
- Reuses [`pack`/`unpack`](../../src/memorydb/vector.py) for the float32 vectors (the same layout as the
  `embeddings` BLOBs), so there is exactly one vector encoding in the codebase.

- The `RetrievalPlanner` consults it wherever it embeds a query (currently `explain()`), recording the
  injected embedder's `model`/`dim`; on a mismatch it clears and rebuilds.
- The facade exposes `MemoryDB.clear_query_cache()` and accepts an injected cache, so one cache can be
  **shared across `MemoryDB` instances that use the same model** — the store-independence made concrete.

## Rationale

- **Why model-bound, not DB-bound.** The embedding depends only on `(model, text)`; the graph/store is
  irrelevant to it. Binding the cache to the DB would (a) fragment hits across stores sharing a model and
  (b) add a dimension that encodes nothing real. Binding to the **model** is exactly the correctness
  boundary: a different model produces different vectors and must never reuse a cached one. (This is the
  refinement that motivated the TD — the first instinct to also key on the DB was dropped.)
- **Why the primary structure is an in-memory map (and the dump is disposable, outside the store).** The
  hot path is a dict lookup; the binary dump only *warms* it. The dump is deliberately **not** in the
  SQLite store — it would bloat it and add a staleness/migration surface — so the `embeddings` BLOBs stay
  the substrate's single authoritative vector source ([TD-004](TD-004-zero-dep-core-bruteforce-vectors.md)).
  The dump file is recomputable and safe to delete (worst case: a cold start), which is exactly why a raw,
  model-validated binary blob is enough — no schema, no migration, no durability guarantee needed.
- **Why clearable/rewritable.** A caller switching models, trimming memory, or pre-warming a known query
  set needs explicit control; a black-box cache would fight those.
- **Why bounded.** Unbounded query caches leak in long-running agents; oldest-eviction is the same cheap
  policy already used for the intent cache.
- **Why a raw binary dump (not `pickle`/JSON), keyed by sha256.** The values are fixed-width float32
  vectors and the keys are fixed-width sha256 digests, so a flat **fixed-stride** file is the most compact
  (`32 + 4·D` bytes/record, zero Python-object overhead), the simplest (no object graph), version-stable,
  and **safe** (no arbitrary-code-execution on load, unlike `pickle`). It is literally the hashmap written
  out record-by-record. Hashing the key also keeps raw queries off disk.

## Consequences

- Repeated / paginated / swept queries skip the embedder — the expensive forward pass or API call is paid
  **once per distinct query per model**.
- A model swap must invalidate the cache; the model tag does this automatically, and `clear()` is the
  manual escape hatch. (`_check_embedder_compat` already warns that a model change makes *stored*
  embeddings stale — TD-006; this aligns the query side.)
- Determinism preserved: same `(model, query)` → same vector. A non-deterministic embedder (rare;
  inference-time dropout) would serve its first cached result for a query — documented as the price of
  the cache, and a caller can `clear()` or not inject a cache if undesired.
- The cache is per-process; it is a hot-path optimization, not a store, so it is intentionally not shared
  across processes.

## Alternatives Considered

- **Persist query vectors in the store.** Rejected: store bloat + a staleness/migration surface; recompute
  on a cold start is cheap and keeps the BLOBs as the only persisted vectors.
- **DB-scoped cache** (key on store path + model). Rejected: fragments hits and encodes a dependency that
  does not exist — the query embedding is DB-independent. (The explicit course-correction behind this TD.)
- **A general `CachingEmbedder` wrapper** caching *all* `embed()` calls, including node embeddings at index
  time. Considered and kept as a possible building block, but node embeddings are unique graph-aware
  serializations (TD-006) with few repeat hits and are already persisted, so caching them mostly wastes
  memory; a **query-scoped** cache is the targeted win.
- **No cache (status quo).** Rejected: re-embedding every query is the dominant per-query cost with a real
  model, and the mapping is trivially cacheable.
- **Persist via `pickle`/`marshal`/JSON.** Rejected: `pickle` is an arbitrary-code-execution sink on load
  and version-fragile; JSON is bulky for float arrays. The fixed-stride sha256+float32 record file is
  smaller, simpler, and safe.

## Open question — where the dump file lives

The dump's *content* is model-scoped (validated by the header), but the *file path* still has to be chosen:

- **(a) model-keyed cache file** — e.g. `<cache_dir>/<model_id>.mqec`. Shared across every `MemoryDB` that
  uses that model → best hit rate, truest to "bound to the model, not the store".
- **(b) db sidecar** — e.g. `<db_path>.qcache` next to the store. Travels with the db, simplest lifecycle,
  but no cross-db sharing.

Both hold the same model-validated content. **Lean (a)** (model-keyed) to stay consistent with the
model-scoping; expose the path so a caller can choose (b) or an explicit location.

## Implementation (2026-06-26)

- [`src/memorydb/query_cache.py`](../../src/memorydb/query_cache.py) — `QueryEmbeddingCache`: sha256-keyed
  in-memory map, `get`/`put` (rewritable, bounded oldest-evicted), `clear`, and `dump`/`load` (the
  fixed-stride `MQEC` binary, atomic write, model-validated load — missing/corrupt/wrong-model/truncated →
  ignored, returns 0). Reuses `vector.pack`/`unpack` for the float32 vectors.
- [`planner.py`](../../src/memorydb/planner.py) — `RetrievalPlanner` takes an optional `query_cache`
  (lazily built from the embedder's `model`/`dim`; injectable to share across planners using the same
  model); `explain()` embeds via `_embed_query()`, a cache lookup before `embedder.embed`.
- [`api.py`](../../src/memorydb/api.py) — `MemoryDB.open(..., query_cache=…)` plus `clear_query_cache()`,
  `dump_query_cache(path)`, `load_query_cache(path)`. The **file location is the caller's choice** (explicit
  path), deferring the open question above rather than baking in (a) or (b).
- Tests: `tests/test_query_cache.py` (get/put/sha256-keying, rewrite, wrong-dim ignore, bound eviction,
  clear, dump/load round-trip, wrong-model/dim/corrupt/truncated rejection, planner cache-hit skips
  re-embed, facade clear/dump/load). Suite **212 green**.

## References

- [TD-002](TD-002-ports-and-adapters-generic-substrate.md) (the `Embedder` is an injected port — Carbon owns the model),
  [TD-004](TD-004-zero-dep-core-bruteforce-vectors.md) (BLOBs are the authoritative, only-persisted vectors),
  [TD-006](TD-006-graph-aware-embeddings-staleness.md) (model-change staleness for stored embeddings),
  [TD-007](TD-007-intent-routed-retrieval-tj-is-orchestration.md) (the intent cache this mirrors).
- Specs: [public-api-facade.md](../specs/completed/public-api-facade.md), [v0-substrate.md](../specs/active/v0-substrate.md).
