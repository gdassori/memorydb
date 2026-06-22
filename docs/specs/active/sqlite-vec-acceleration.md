---
title: "sqlite-vec acceleration (ANN index)"
status: planned
created: 2026-06-22
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

- [ ] extension load + capability check (`vec_version()`)
- [ ] `vec_items` ensure migration (dim-parameterized, idempotent, extension-gated)
- [ ] `SqliteVecIndex.search/upsert/remove/rebuild_index`
- [ ] `make_vector_index` factory + `Store.set_embedding` sync hook
- [ ] cosine distance→score mapping consistent with brute force
- [ ] zero-dep fallback test + [vector]-extra KNN/recall tests

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

## References

- [TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)
- [schema-migrations.md](schema-migrations.md), [graph-aware-embedding-pipeline.md](graph-aware-embedding-pipeline.md)
- sqlite-vec (`vec0` virtual table, KNN `MATCH`).
