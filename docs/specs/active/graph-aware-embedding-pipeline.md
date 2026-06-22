---
title: "Graph-aware embedding pipeline"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-006, TD-002, TD-004]
components: [store, embedders, query]
---

# Graph-aware embedding pipeline

> Embed a node's **serialized neighborhood** (role in the graph), not its raw source, and re-embed only
> what went stale ([TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)). The substrate
> already maintains `embed_dirty`; this spec defines the serializer and the re-embedding driver.

## Goal

`EmbeddingPipeline(store, embedder).refresh()` brings every `embed_dirty` node's embedding up to date,
using a deterministic neighborhood serialization. Done = after `refresh()`, `dirty_nodes() == []` and each
node's vector reflects its current neighbors; adding an edge re-stales exactly its two endpoints.

## Background & constraints

Raw-source embeddings are role-blind; a symbol's edges are its meaning ([TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)).
The core is zero-dep ([TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)); the actual
`Embedder` is injected ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)). The
serialization must be **deterministic** (stable ordering) so embeddings are reproducible and diffable.

## Data model & interfaces

```python
from typing import Protocol

class NeighborhoodSerializer(Protocol):
    def serialize(self, store, node_id: int) -> str: ...

class DefaultSerializer:                      # core, language-agnostic
    def serialize(self, store, node_id) -> str: ...

class EmbeddingPipeline:
    def __init__(self, store, embedder, serializer: NeighborhoodSerializer | None = None,
                 batch_size: int = 128, model: str | None = None) -> None: ...
    def refresh(self) -> "EmbedReport": ...           # (re)embed all dirty nodes
    def reembed_all(self) -> "EmbedReport": ...        # e.g. after a model change
```

## Serialization format (exact, deterministic)

```
{name}  ({type}, {path})
signature: {attrs.signature}
docstring: {attrs.docstring first line}
calls: {sorted distinct out-edge dst names, relation=CALLS}
writes: {sorted out-edge dst names, relation=WRITES}
called_by: {sorted in-edge src names, relation=CALLS}
inherits: {sorted out-edge dst names, relation=INHERITS}
```
Empty sections are omitted. Names sorted lexicographically; relations emitted in a fixed order. Worked
example (the [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md) case):
```
send_notification  (function, services/notifications.py)
signature: (user_id, message, channel) -> NotificationLog
docstring: Send a single notification to a user
calls: PushProvider.send, RedisQueue.push
writes: NotificationLog
called_by: MassNotificationJob, RetryWorker
```

## Algorithm / step-by-step

1. `dirty = store.dirty_nodes()`; if empty, return.
2. For each batch of `batch_size`: `texts = [serializer.serialize(store, n["id"]) for n in batch]`.
3. `vectors = embedder.embed(texts)`; assert each `len(vec) == expected_dim`.
4. For each `(node, vec)`: `store.set_embedding(node_id, vec, model=self.model)` (clears `embed_dirty`).
5. On a batch error: retry once; on persistent failure, leave those nodes dirty (next `refresh` retries) and
   record them in the report.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/embedding_pipeline.py` | **New** — `EmbeddingPipeline`, `DefaultSerializer`, `EmbedReport` |
| `src/memorydb/adapters/code/__init__.py` | **Modify** — a `CodeSerializer` override (richer signature/docstring) |
| `src/memorydb/store.py` | **Modify (maybe)** — `mark_dirty(node_ids)` helper for rename cascades |

## Staleness model

`Store.upsert_edge` already marks **both endpoints** dirty (their neighborhoods changed). Additional case:
**renaming** a node changes its *name*, which appears in neighbors' serializations (`calls:`/`called_by:`).
Proposed policy: on a name change, mark depth-1 neighbors dirty too (`mark_dirty`). A full depth-1 cascade on
*every* edge change is heavier and usually unnecessary (the endpoints already cover structural change) — see
Open Questions.

## Edge cases & failure modes

- **No edges / no body:** serialize the header line only; still embeds (don't skip).
- **Wrong dim from embedder:** raise a clear error before writing (don't store mixed dims).
- **High-degree hub:** cap each section to top-K neighbors (by edge confidence then name) and note the cap.
- **Model change:** if `embeddings.model != self.model`, `reembed_all()` (mark all dirty first).
- **Empty dirty set:** `refresh()` is a no-op.

## Test plan

Zero-dep (`HashingEmbedder`):

- `test_refresh_clears_dirty` — build the notification graph, `refresh()` → `dirty_nodes() == []`.
- `test_edge_restales_endpoints` — add an edge → exactly its 2 endpoints dirty.
- `test_incremental_reembed` — `refresh()`, add one edge, `refresh()` → only the 2 endpoints re-embedded
  (assert via a counting fake embedder).
- `test_serialization_deterministic` — same graph → identical serialized string across runs.
- `test_rename_cascade` — rename a node → its depth-1 neighbors marked dirty.

## Performance & scale

Cost = embedding calls; structural work is cheap. Only dirty nodes embed, so steady-state re-index is small.
Batch size trades latency vs throughput. Hub-node capping bounds serialization length.

## Tasks

- [ ] `DefaultSerializer` with the exact deterministic format + hub cap
- [ ] `EmbeddingPipeline.refresh()` / `reembed_all()` with batching + retry + report
- [ ] dim assertion + model-change detection
- [ ] `CodeSerializer` override in the code adapter
- [ ] rename → depth-1 `mark_dirty` cascade
- [ ] zero-dep tests (clear / restale / incremental / determinism / rename)

## Open questions

- **Full depth-1 cascade on every edge change** vs endpoints-only + rename-only? **Lean** endpoints-only +
  rename-cascade — cheapest correct policy; revisit if retrieval quality shows neighbor drift.
- **Include neighbor *signatures*** in serialization (richer but longer)? **Lean** names only for v1.

## Risks

- **Cascade storms** when a hub node changes name → cap fan-out + batch; acceptable since renames are rare.
- **Nondeterministic embedders** (real models) break reproducibility tests → tests pin `HashingEmbedder`.

## Review remediation (2026-06-22)

- The serializer reads a node's edges via the now-implemented **`query.node_neighborhood(store, node_id)`** (in/out
  edges with neighbor uid/name/relation/confidence, deterministically ordered) — no ad-hoc SQL in the serializer.
- **Rename reconciliation:** under the code uid scheme (`relpath::qualname`) a rename **changes the uid**, so for code
  it is delete+add and the "depth-1 neighbors on rename" cascade is a **no-op for code** — it applies only to
  **agent-memory entities** with stable uids. The endpoints-dirty-on-edge-change rule (already in `Store.upsert_edge`)
  is what covers code.
- **Type-aware serialization:** non-code node types (e.g. `Concept`) have no `attrs.signature`; the serializer treats
  signature/docstring as optional so it generalizes across adapters.

## References

- [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md), [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md), [TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)
- [code-adapter-treesitter.md](code-adapter-treesitter.md), [sqlite-vec-acceleration.md](sqlite-vec-acceleration.md)
