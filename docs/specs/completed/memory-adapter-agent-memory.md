---
title: "MemoryAdapter — agent memory (episodic / semantic / procedural)"
status: completed
created: 2026-06-22
completed: 2026-06-30
author: claude
related_tds: [TD-002, TD-008, TD-009]
components: [adapters/memory]
---

# MemoryAdapter — agent memory

> The second product on the same substrate ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)):
> a long-lived "external brain" for an agent — entities, relations, and three memory tiers (episodic, semantic,
> procedural) — proving the substrate generalizes beyond code.

## Goal

`MemoryAdapter` lets an agent `remember(...)`, `relate(...)`, and `recall(query)` over the same `Store`/planner,
with provenance and (later) temporal/confidence metadata. Done = the notification-style retrieval flows work for
*facts about the world/user*, not just code symbols, with no change to the substrate core.

## Background & constraints

The substrate is domain-agnostic ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)); this
adapter maps memory concepts onto generic `Node`/`Edge`. It uses the metadata columns reserved in v0
(`source`, `valid_from`/`valid_to`, `confidence`) — but the heavy *machinery* (decay, temporal queries) is its
own deferred spec ([temporal-confidence-machinery.md](../active/temporal-confidence-machinery.md)), per
[TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md).

## Data model & interfaces

```python
class MemoryAdapter:
    def __init__(self, store, embedder) -> None: ...
    def remember(self, text: str, *, kind: str = "episodic", entities: list[str] = (),
                 source: str = "chat", at: str | None = None, confidence: float = 1.0) -> int: ...
    def relate(self, src: str, relation: str, dst: str, *, confidence: float = 1.0, source=None) -> None: ...
    def entity(self, name: str, type: str = "Entity", **attrs) -> int: ...
    def recall(self, query: str, *, kinds=("episodic","semantic","procedural"), k=8) -> dict: ...
```

**Node types:** `Entity` (User, Company, Project…), `Episode` (a timestamped event/utterance), `Fact`
(semantic), `Procedure` (how-to). **Tiers** map to node `type` + an `attrs.tier`. **Relations:** `MENTIONS`,
`ABOUT`, `WORKS_ON`, `HAPPENED_AT`, `STEP_OF`, etc. (open vocabulary like code's `Rel`).

The three tiers ([TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md)):
- **Episodic** — "Yesterday Guido said X" (`Episode` nodes, `valid_from` = event time, `source` = chat/email).
- **Semantic** — "Guido created Spruned" (`Fact` nodes, high confidence, deduplicated).
- **Procedural** — "Deploy service: 1…2…3" (`Procedure` nodes with ordered `STEP_OF` edges).

## Algorithm / step-by-step

1. `remember(text, kind, entities, …)`: create the tier node (body=text, attrs.tier=kind, source, valid_from=at);
   upsert/link each entity (`MENTIONS`/`ABOUT` edges); mark `embed_dirty` (graph-aware embedding applies, TD-006).
2. `entity(name)`: upsert an `Entity` node (idempotent by name/uid).
3. `recall(query)`: planner EXPLAIN restricted to memory node types (vector seed → traverse entity/episode graph →
   subgraph); optionally fold in temporal/confidence weighting once that machinery lands.
4. Embeddings via the shared pipeline; serialization includes entity links and time.

**Worked example:** `remember("Guido moved to Bangkok in 2024", kind="semantic", entities=["Guido","Bangkok"])`
→ `Fact` node + `Guido --ABOUT--> Fact <--ABOUT-- Bangkok`. `recall("where does Guido live?")` → that Fact via the
entity subgraph.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/adapters/memory/__init__.py` | **New** — `MemoryAdapter`, memory node/relation vocab |
| `src/memorydb/adapters/memory/serializer.py` | **New** — neighborhood serializer for memory nodes (entities + time) |

## Edge cases & failure modes

- **Duplicate facts:** dedupe semantic facts by normalized text/entity set; bump confidence instead of duplicating.
- **Contradictions** ("lives in Bangkok" vs "lives in Italy"): keep both with time/confidence; resolution is the
  temporal/confidence spec's job — do not silently overwrite.
- **Unknown entity:** auto-create the `Entity` node at low confidence.
- **No embedder:** facts still stored; `recall` degrades to graph/entity lookup.

## Test plan

Zero-dep (`HashingEmbedder`):

- `test_remember_creates_links` — a fact with 2 entities → node + 2 `ABOUT` edges.
- `test_recall_via_entity` — `recall` finds a fact through its entity subgraph.
- `test_procedural_steps_ordered` — a procedure with `STEP_OF` edges recalls steps in order.
- `test_dedupe_semantic` — remembering the same fact twice → one node, higher confidence.

## Performance & scale

Same substrate costs as code. Memory graphs are typically smaller/denser; brute-force vectors fine. Long-lived
stores benefit from the deferred machinery (decay, compaction) to avoid unbounded growth.

## Tasks

- [x] memory node/relation vocabulary + tier model
- [x] `remember` / `relate` / `entity` / `recall`
- [x] memory neighborhood serializer (entities + time)
- [x] dedupe + contradiction-preserving storage
- [x] zero-dep tests (links / recall / procedural / dedupe)

## Implementation notes (2026-06-30)

Landed as [`src/memorydb/adapters/memory/__init__.py`](../../../src/memorydb/adapters/memory/__init__.py)
(`MemoryAdapter`) + [`serializer.py`](../../../src/memorydb/adapters/memory/serializer.py)
(`MemorySerializer`), tested by [`tests/test_memory_adapter.py`](../../../tests/test_memory_adapter.py)
(11 zero-dep cases). Accessed via its path (`from memorydb.adapters.memory import MemoryAdapter`), like
`CodeAdapter` — kept out of the top-level `__init__` to avoid a core↔adapter import cycle (TD-002).

- **Identity / dedupe:** entities are keyed `entity::{normalized-name}` (one idempotent path shared by
  `entity()` and the auto-create in `remember`/`relate` — the C2 remediation). Semantic/procedural nodes are
  content-keyed (`fact::`/`procedure::{sha1(normalized text)}`) so a re-`remember` dedupes and **reinforces**
  confidence toward 1.0 (`prev + (1-prev)/2`); an **episode** is keyed by `(text, time, source)` so the same
  utterance at a different time is a distinct event.
- **Contradictions are kept, not overwritten** (C1): "lives in Bangkok" vs "lives in Italy" are different text
  → two `Fact` nodes. Resolution stays the deferred temporal-confidence spec's job (TD-008/009); this adapter
  never silently supersedes.
- **Unknown entity** referenced by a mention/relation is auto-created at low confidence (0.5); an explicit
  `entity()` upgrades it to 1.0.
- **Serializer:** memory recall is about *content*, so `MemorySerializer` leads with the node body, then linked
  entity names + `valid_from`/`source` — unlike the code serializer which embeds a symbol's graph role.
- **`recall`** lazily flushes embeddings (`pipeline.refresh()`), vector-seeds restricted to the requested
  tiers' node types (+ `Entity` connectors), then expands over the entity graph via `query.traverse` — returning
  the same `{query, seeds, nodes, edges}` shape as the planner's EXPLAIN.
- **Procedural tier:** `remember(..., kind="procedural", steps=[…])` creates ordered `Step --STEP_OF-->
  Procedure` edges (order in the step's `attrs.order`); `steps_of(name)` reads them back in order. (The `steps=`
  kwarg + `steps_of` reader are a small, documented extension beyond the spec's interface block.)

## Open questions

- **Entity resolution** (coref across mentions): rule-based vs embedding-based merge? **Lean** rule-based by
  normalized name for v1; embedding merge later.
- **Where do episodes come from** (a chat hook vs explicit calls)? **Lean** explicit `remember()` API first; an
  ingestion hook later.

## Risks

- **Unbounded growth** of episodic memory → needs compaction/decay ([temporal-confidence-machinery.md](../active/temporal-confidence-machinery.md))
  and concepts ([concept-ontology-layer.md](../active/concept-ontology-layer.md)) to summarize; flagged, not solved here.

## Review remediation (2026-06-22)

- **Contradictions (C1):** "keep both with time/confidence" uses the temporal **history model** from
  [TD-009](../../decisions/TD-009-versioned-identity-for-temporal-history.md) — a superseded fact moves to
  `node_history` while the live row holds the current truth — **not** duplicate uids (which the schema forbids).
- **Single entity path:** route both `entity()` and `remember(entities=...)` through one idempotent upsert keyed on a
  normalized name, to avoid the two-path duplication risk.

## References

- [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md), [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md)
- [temporal-confidence-machinery.md](../active/temporal-confidence-machinery.md), [concept-ontology-layer.md](../active/concept-ontology-layer.md), [reflection-daemon.md](../active/reflection-daemon.md)
