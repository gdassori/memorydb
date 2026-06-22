---
title: "Reflection daemon — grow your own ontology"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-008, TD-006]
components: [reflection, concepts, temporal]
---

# Reflection daemon (north-star)

> A periodic background process that *reflects* on accumulated memory: cluster new nodes, propose concepts,
> compact and decay, repair the graph — so the store builds its own higher-level structure over time instead of
> drowning in disconnected chunks. The most forward-looking piece of [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md);
> explicitly a **north-star**, not v0.

## Goal

`Reflector(db).cycle()` runs one reflection pass: discover clusters → propose/accept concepts → compact episodic
memory → decay/prune → re-embed dirty. Done = after many `cycle()`s over a growing store, retrieval quality holds
(or improves) and the node count grows sub-linearly with raw inputs (compaction works).

## Background & constraints

This composes existing pieces — concept extraction ([concept-ontology-layer.md](concept-ontology-layer.md)),
temporal/confidence ([temporal-confidence-machinery.md](temporal-confidence-machinery.md)), graph algorithms
([graph-algorithms-networkx.md](graph-algorithms-networkx.md)), embeddings ([TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md))
— into a scheduled loop. It must be **idempotent**, **incremental** (only reflect on what changed since the last
cycle), and **safe** (never destroy load-bearing data; concepts are a rebuildable layer).

## Data model & interfaces

```python
class Reflector:
    def __init__(self, db, *, llm=None, since_cursor: str | None = None) -> None: ...
    def cycle(self) -> "ReflectionReport": ...     # one pass; safe to call repeatedly
    def schedule(self, every_seconds: int) -> None: ...   # optional in-process scheduler

@dataclass
class ReflectionReport:
    clusters: int; concepts_proposed: int; concepts_accepted: int
    episodes_compacted: int; nodes_pruned: int; reembedded: int
```

A `reflection_state` row stores the `since_cursor` (last processed node id / timestamp) for incrementality.

## Algorithm / step-by-step (one cycle)

1. **Scope:** select nodes created/changed since `since_cursor`.
2. **Cluster** them (package/name/embedding) → candidate concept clusters
   ([concept-ontology-layer.md](concept-ontology-layer.md)).
3. **Propose & verify** concepts via the LLM; accept high-confidence, queue the rest.
4. **Compact episodic memory:** summarize groups of related `Episode`s into a `Fact`/`Concept`, linking provenance
   back (don't delete episodes; mark them rolled-up).
5. **Decay & prune:** run `decay_confidence` + `prune` ([temporal-confidence-machinery.md](temporal-confidence-machinery.md))
   on facts only (never structural code edges).
6. **Re-embed:** `EmbeddingPipeline.refresh()` for everything newly dirty.
7. **Advance** `since_cursor`; write the `ReflectionReport`.

**Worked example:** after a week of indexing, a cycle clusters three `*NotificationService` symbols → proposes
"Notification Infrastructure" (accepted), summarizes 40 episodic "sent notification" events into one rolled-up
fact, prunes 12 stale low-confidence guesses, re-embeds 18 nodes.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/reflection.py` | **New** — `Reflector`, `ReflectionReport`, `since_cursor` state |
| `src/memorydb/migrations.py` | **Modify** — `reflection_state` table |
| `src/memorydb/cli.py` | **Modify** — `memorydb reflect [--once|--every N]` |

## Edge cases & failure modes

- **Empty/no changes since cursor:** cycle is a cheap no-op.
- **LLM unavailable:** skip proposal/compaction; still run decay/prune/re-embed (mechanical parts).
- **Crash mid-cycle:** each step commits independently and is idempotent; `since_cursor` advances only at the end →
  a re-run redoes at most one cycle's work safely.
- **Over-aggressive compaction** losing detail → keep source episodes (rolled-up flag), never hard-delete on compaction.
- **Runaway concept creation** → per-cycle caps + the eval harness as a regression gate.

## Test plan

Zero-dep with `FakeLLM` + `HashingEmbedder`, timestamps passed explicitly:

- `test_cycle_idempotent` — two cycles with no new data → second is a no-op.
- `test_incremental_scope` — only nodes after `since_cursor` are reflected on.
- `test_concept_creation` — a cluster → an accepted concept (via canned proposal).
- `test_compaction_preserves_provenance` — episodes summarized but still present (rolled-up), links intact.
- `test_decay_prune_safe` — facts decay/prune; structural code edges untouched.
- `test_crash_resume` — interrupt after step 4 → re-run completes without double work.

## Performance & scale

Designed to run **off the query path** (background/cron). Cost scales with *new* data per cycle, not the whole store.
Compaction + pruning keep long-term size sub-linear. LLM calls are the budget; batch and cap them per cycle.

## Tasks

- [ ] `reflection_state` (cursor) + `Reflector.cycle()` step pipeline
- [ ] incremental scoping since the cursor
- [ ] concept proposal/accept integration (caps)
- [ ] episodic compaction with provenance preservation
- [ ] decay/prune (facts only) + re-embed
- [ ] CLI `reflect` (once / every N) + optional in-process scheduler
- [ ] zero-dep tests (idempotent / incremental / concept / compaction / decay / crash-resume)

## Open questions

- **Scheduler**: in-process thread vs external cron calling `memorydb reflect --once`? **Lean** external cron for v1
  (simpler, crash-isolated); offer an in-process option later.
- **How much to compact** (aggressiveness knob): fixed thresholds vs learned? **Lean** conservative fixed thresholds,
  tuned via the eval harness.

## Risks

- **Silent knowledge loss** from compaction/pruning → never hard-delete source episodes; everything is reconstructable
  from provenance; the eval harness guards retrieval quality across cycles.
- **Concept/ontology drift** compounding over cycles → caps + verification + human review queue for low-confidence concepts.

## Review remediation (2026-06-22)

`since_cursor` must **not** be a raw `nodes.id`: re-indexing deletes+reinserts symbols with *new* ids, which would
reprocess them every cycle and never observe deletions. Use a monotonic **change cursor** instead — an `updated_at`
timestamp stamped on nodes (or a small append-only change log written by the indexer) — and pick up deletions from the
indexer's delete pass rather than inferring them from id gaps.

## References

- [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md), [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)
- [concept-ontology-layer.md](concept-ontology-layer.md), [temporal-confidence-machinery.md](temporal-confidence-machinery.md), [graph-algorithms-networkx.md](graph-algorithms-networkx.md)
