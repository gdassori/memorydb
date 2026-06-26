---
title: "sqlite-vec acceleration (ANN index)"
status: completed
created: 2026-06-22
completed: 2026-06-25
author: claude
related_tds: [TD-004]
components: [vector, store, schema]
---

# sqlite-vec acceleration

> Implement `SqliteVecIndex` behind the existing `VectorIndex` interface so vector search scales past the
> brute-force ceiling, while keeping the float32 BLOB authoritative and degrading gracefully to brute force
> when the extension is absent ([TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)).

## Goal

`make_vector_index(store)` returns an ANN-backed index when `sqlite-vec` is installed (the `[vector]`
extra) and the pure-Python `BruteForceVectorIndex` otherwise — same `search(query_vec, k, types)` contract.
Done = identical API, transparent fallback, and a `vec0` index rebuildable from the authoritative BLOBs.

## Background & constraints

v0 ships `BruteForceVectorIndex` (exact, O(n)) and a `SqliteVecIndex` stub. `sqlite-vec` provides a `vec0`
virtual table with KNN over the same connection ([TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)).
The interface returns `list[(score, node_id)]`, score descending. Cosine is the metric (consistent with the
brute-force cosine) so results are comparable across backends.

## Data model & interfaces

```python
def make_vector_index(store, prefer_ann: bool = True) -> "VectorIndex":
    """SqliteVecIndex if the extension loads, else BruteForceVectorIndex (TD-004)."""

class SqliteVecIndex:
    def __init__(self, store, dim: int) -> None: ...        # loads ext, ensures vec_items(dim)
    def search(self, query_vec, k=10, types=None) -> list[tuple[float, int]]: ...
    def upsert(self, node_id: int, vector) -> None: ...     # called from Store.set_embedding
    def remove(self, node_id: int) -> None: ...
    def rebuild_index(self) -> int: ...                     # repopulate vec_items from embeddings
```

```sql
-- created by a migration (see schema-migrations.md), only when the extension is present
CREATE VIRTUAL TABLE vec_items USING vec0(
    node_id  integer primary key,
    embedding float[768]                 -- D fixed at creation
);
```

**Authority (resolves the v0 open question):** the `embeddings` BLOB is the **source of truth**;
`vec_items` is a derived, rebuildable index. `rebuild_index()` truncates and repopulates it from
`embeddings`. `Store.set_embedding` calls `index.upsert` so the two stay in sync incrementally.

## Algorithm / step-by-step

1. **Load:** `conn.enable_load_extension(True); sqlite_vec.load(conn)`; verify via `SELECT vec_version()`.
2. **Ensure table:** create `vec_items` at dimension `D` if missing (via the migration's idempotent ensure step).
3. **Upsert:** on `set_embedding(node_id, vec)`, `INSERT ... ON CONFLICT(node_id) DO UPDATE` into `vec_items`
   (vector serialized as the sqlite-vec float32 format).
4. **Search:** `SELECT node_id, distance FROM vec_items WHERE embedding MATCH :q AND k = :k ORDER BY distance`;
   convert distance→score (`score = 1 - cos_distance`); if `types` given, join to `nodes` and filter (over-fetch
   `k * f` then filter, or pre-filter candidate ids).
5. **Remove:** on node delete, `DELETE FROM vec_items WHERE node_id = ?` (or rely on a trigger / cascade rebuild).

## What changes

| File | Change |
|------|--------|
| `src/memorydb/vector.py` | **Modify** — implement `SqliteVecIndex`; add `make_vector_index()` |
| `src/memorydb/migrations.py` | **Modify** — idempotent `vec_items` ensure step (gated on extension) |
| `src/memorydb/store.py` | **Modify** — `set_embedding` notifies the active index (`upsert`); delete notifies `remove` |
| `pyproject.toml` | **Already** declares `[vector] = ["sqlite-vec>=0.1.0"]` |

## Edge cases & failure modes

- **Extension missing:** `make_vector_index` catches the load error → returns `BruteForceVectorIndex` (logged).
- **Dimension mismatch:** stored `D` ≠ embedding dim → `rebuild_index` after DROP at the new dim; reject single upserts of wrong dim.
- **Empty index:** `search` returns `[]`.
- **Node deleted but vec row remains:** `remove`/rebuild keeps it consistent; a periodic `rebuild_index` is the backstop.
- **Metric mismatch:** force cosine in both backends so scores are comparable; document if L2 is used instead.
- **`types` filter starves k:** over-fetch and filter, or push the filter down via a candidate id list.

## Test plan

- **Zero-dep (always runs):** `test_factory_fallback` — with the extension unavailable, `make_vector_index`
  returns `BruteForceVectorIndex` and search still works.
- **[vector] extra (marked/skipped if absent):** `test_vec0_knn_matches_bruteforce` — same data in both
  backends → top-k node ids agree (allowing ANN recall slack); `test_upsert_and_search`; `test_rebuild_from_blobs`;
  `test_dim_change_rebuild`.

## Performance & scale

Brute force is exact O(n) — fine to ~1e5 vectors; beyond that `vec0` ANN cuts query latency at the cost of
some recall. `rebuild_index` is O(n) and run rarely (dim/model change). Memory: `vec_items` duplicates vectors
(index), accepted for the speed.

## Tasks

- [x] extension load + capability check (`vec_version()`)
- [x] `vec_items` lazy ensure (dim-parameterized, idempotent) — see notes (migration superseded by C3)
- [x] `SqliteVecIndex.search/upsert/remove/rebuild_index`
- [x] `make_vector_index` factory + `Store.set_embedding` sync hook
- [x] cosine distance→score mapping consistent with brute force
- [x] zero-dep fallback test + [vector]-extra KNN/recall tests

## Implementation notes (2026-06-25)

- **No migration — lazy dim-correct creation (C3).** `vec_items` is created on the **first `upsert`** at the
  embedder's real dim (persisted in `meta['vec0_dim']`); migrations run before any embedding exists and can't
  know the dim, so the spec's migration step is superseded. A dim/model change recreates the table at the new
  dim (the authoritative BLOBs refill it on a full reembed); `rebuild_index()` is the explicit path.
- **Cosine via L2-on-normalized (C6).** Vectors are unit-normalized (as in `BruteForceVectorIndex`), so vec0's
  **default L2** distance `d` gives cosine `1 − d²/2` exactly — identical ranking and comparable scores across
  backends, with **no** dependency on a cosine-metric build of sqlite-vec. Cross-backend agreement is tested.
- **vec0 has no UPSERT.** `ON CONFLICT … DO UPDATE` raises `OperationalError: UPSERT not implemented for virtual
  table`; `upsert` is **DELETE-then-INSERT**. `rebuild_index` uses a plain INSERT into the freshly recreated table.
- **Delete sync via the search join.** `search` joins `vec_items → nodes`, so a deleted node's stale vec row
  (embeddings cascade on node delete; vec0 has no FK) is **inert** at query time and reclaimed by `rebuild_index`
  — the backstop against drift. `remove(node_id)` exists for explicit use.
- **Sync hook.** `Store.attach_index(index)` registers the active index; `set_embedding` calls `index.upsert`
  (guarded — a derived-index hiccup is logged, never breaks the authoritative BLOB write). A brute-force index
  (no `upsert`) is ignored. The facade wires `attach_index` after `make_vector_index`.
- **`types` filter** over-fetches `k×4` then filters in Python (keeps the vec0 `MATCH … AND k = ?` clause clean,
  avoids type-filter starving k). The query-dim guard and `max(0, k)` mirror the brute-force backend.
- **Fallback (C7).** `make_vector_index` returns `BruteForceVectorIndex` on `ImportError` (extra absent),
  `AttributeError` (no `enable_load_extension`), or `OperationalError`/`DatabaseError` (load failed). Verified:
  with sqlite-vec absent the full suite is green (186 passed, 7 ANN tests skipped); with it present, 193 green.
- **CI** now installs the `[vector]` extra so the vec0 path is exercised on 3.10/3.11/3.12 (gated tests skip
  cleanly if the extension can't load on a runner). Validated locally against **sqlite-vec 0.1.9**.

## Open questions

- **`types` filtering**: over-fetch-then-filter vs maintaining per-type vec tables? **Lean** over-fetch (`k*4`)
  for v1; per-type tables only if a few hot types dominate.
- **Auto-switch threshold** brute→ANN by row count, or always ANN when present? **Lean** always ANN when the
  extension is present (simpler), brute force only as fallback.

## Risks

- **vec_items drift** from `embeddings` → `rebuild_index` backstop + sync on every write; a test asserts agreement.
- **sqlite-vec API churn** (young project) → pin the version; isolate all extension calls in `SqliteVecIndex`.

## Review remediation (2026-06-22)

- **Lazy, dim-correct creation (C3):** never hardcode `float[768]`. Create `vec_items` on the **first `set_embedding`**
  at the embedder's actual dim; persist the dim in `meta(key,value)`; `rebuild_index()` (drop + recreate) on a
  dim/model change. Migrations cannot fix the dim because they run before any embedding exists.
- **Metric consistency (C6):** use **cosine** in `vec0` (column `distance=cosine` if the pinned sqlite-vec supports it,
  otherwise store L2-normalized vectors) so rankings match `BruteForceVectorIndex`'s cosine; add a cross-backend
  agreement test.
- **Capability detection (C7):** `make_vector_index` must fall back to brute force when `enable_load_extension` is
  **missing or disabled** (AttributeError / OperationalError), not only when the extension file is absent.
- **Delete sync:** node deletion must `DELETE FROM vec_items WHERE node_id = ?` (vec0 has no FK cascade); `rebuild_index`
  is the backstop against drift.

## Review remediation (2026-06-25 — PR #5 mega review)

An adversarial multi-agent review (27 raised → 23 confirmed / 4 refuted, all reproduced empirically against
sqlite-vec 0.1.9) confirmed the cosine mapping is **correct** (vec0 default `distance` is euclidean L2, so
`score = 1 − d²/2` reproduces cosine — verified on identical/orthogonal/opposite vectors) but found real defects,
now all fixed + regression-tested (`test_p5_*`); suite **199 green**:

- **P5-1 (High):** node deletion never called `index.remove`, so a stale `vec_items` row (a) starved k-NN (the
  nodes-join drops it *after* the KNN, leaving < k live seeds) and (b) on SQLite node-id reuse scored a *new* node
  by the *deleted* node's vector. Fix: `Store.index_remove()` hook + `Indexer._delete_file` captures the reaped ids
  and calls it; `search` now **always over-fetches `k×4`** (drift → recall slack, not empty); `rebuild_vector_index()`
  is the exposed backstop. Verified end-to-end (delete + re-index → 0 orphan rows, ANN seeds == brute force).
- **P5-2 (Medium):** a transaction rollback dropped the lazily-created `vec_items` while `self.dim` stayed cached
  → the next upsert hit a missing table (swallowed) and `search` crashed with *no such table*. Fix: `upsert`
  re-ensures the table and retries on `OperationalError` (self-heal); `search` returns `[]` instead of crashing.
- **P5-3 (Medium):** a single wrong-dim upsert called `_recreate` and DROPped the whole index. Fix: a wrong-dim
  upsert is a **no-op** (a dim change is corpus-wide); the new dim is adopted by `rebuild_index()` /
  `refresh_embeddings(full=True)` (which now rebuilds).
- **P5-4 (Medium):** the float32 round-trip gave orthogonal vectors a ~3.4e-8 score that passed the planner's
  `>1e-9` seed filter (a phantom seed). Fix: `search` snaps `|score| < 1e-6` to exact `0.0`, matching brute force.
- **P5-5 (Low):** a `types` filter over-fetched a fixed `k×4` then post-filtered, starving rare/far types. Fix:
  escalate the over-fetch (×4) until ≥ k typed matches or the KNN is exhausted.
- **Also:** `rebuild_index` builds at the **prevailing** (majority) dim, not `max(dim)`, warning on skipped off-dim
  rows; the backstop is exposed as `MemoryDB.rebuild_vector_index()`. Docs corrected: vec0 KNN in 0.1.x is **exact**
  (C-speed), not approximate.

Refuted: the max-dim rebuild facets (folded into the Low fix); a zero-length-embedding score mismatch (unreachable —
`set_embedding` never stores empty vectors); the "ANN/recall slack" wording (a doc nuance, fixed anyway).

## References

- [TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)
- [schema-migrations.md](schema-migrations.md), [graph-aware-embedding-pipeline.md](graph-aware-embedding-pipeline.md)
- sqlite-vec (`vec0` virtual table, KNN `MATCH`).
