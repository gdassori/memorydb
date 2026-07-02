---
title: "Temporal validity & confidence machinery"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-008, TD-009]
components: [store, query]
---

# Temporal & confidence machinery

> Activate the reserved metadata columns (`valid_from`/`valid_to`/`confidence`) into real behavior: time-aware
> queries ("what was true in May 2026?"), confidence decay, and contradiction handling. Deferred in v0 per
> [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md); this spec is the plan for when it lands.

## Goal

Nodes/edges can be queried *as of* a time, low-confidence/old facts can decay or be filtered, and contradictory
facts coexist with the latest winning by default. Done = `recall("where did Guido live in 2024?")` returns the
2024 fact even after a 2026 update, and stale low-confidence edges can be pruned.

## Background & constraints

The columns exist since v0 ([TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md)); only
the machinery was deferred because it taxes every write and is partly a research problem (calibration). Most relevant
to agent memory ([memory-adapter-agent-memory.md](../completed/memory-adapter-agent-memory.md)); for code, `confidence` is already
used statically by the ranker/LOCATE ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)).

## Data model & interfaces

```python
# query.py additions
def as_of(store, ids, when: str): ...                 # filter nodes/edges valid at `when`
def supersede(store, uid: str, new_node: Node, at: str) -> int:  # close old validity, open new
    ...
def decay_confidence(store, half_life_days: float, floor: float = 0.0) -> int: ...   # batch update
def prune(store, max_age_days: float, max_confidence: float) -> int: ...             # delete stale+weak
```

Validity model (bitemporal-lite): `valid_from`/`valid_to` are ISO timestamps; an open interval has
`valid_to IS NULL`. Superseding a fact sets the old row's `valid_to = at` and inserts a new open row.

## Algorithm / step-by-step

1. **as_of(when):** add `AND (valid_from IS NULL OR valid_from <= :when) AND (valid_to IS NULL OR valid_to > :when)`
   to node/edge selects (a query modifier the planner can opt into).
2. **supersede:** within a transaction, close the current open version (`valid_to = at`) and insert the new version
   (`valid_from = at`, `valid_to = NULL`) — preserves history, no destructive overwrite.
3. **decay:** periodically `confidence = max(floor, confidence * 0.5**(age_days/half_life))` for facts (not for
   precise code edges); age from `valid_from`/`mtime`.
4. **prune:** delete rows below `max_confidence` and older than `max_age_days` (a compaction step the reflection
   daemon can call).
5. **contradiction:** keep both; default queries pick the highest-confidence currently-valid row; surface conflicts on demand.

**Worked example:** "Guido lives in Bangkok" (valid_from 2024) then `supersede(..., "lives in Italy", at=2026-06)`
→ `as_of("2024-07")` returns Bangkok; `as_of("2026-07")` returns Italy; history intact.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/query.py` | **Modify** — `as_of`, validity filters, `supersede` |
| `src/memorydb/temporal.py` | **New** — `decay_confidence`, `prune`, validity helpers |
| `src/memorydb/planner.py` | **Modify** — optional `as_of`/confidence-floor params on retrieval |

## Edge cases & failure modes

- **Naive vs tz-aware timestamps:** store ISO-8601 UTC; reject ambiguous inputs.
- **Decaying code edges:** exclude precise/structural edges from decay (only agent-memory facts decay).
- **Open-interval overlaps** after a buggy supersede: a consistency check + repair.
- **Pruning something still referenced:** prune nodes only when no high-confidence edge depends on them.

## Test plan

Zero-dep (timestamps passed explicitly — no wall-clock in tests):

- `test_as_of_returns_historical` — supersede a fact, `as_of` past/future returns the right version.
- `test_supersede_preserves_history` — old row closed, new row open, both present.
- `test_decay_lowers_confidence` — given an age, confidence drops by the half-life formula.
- `test_prune_removes_stale_weak` — old + low-confidence rows deleted; recent/strong kept.
- `test_code_edges_not_decayed` — structural edges untouched by decay.

## Performance & scale

`as_of` is an index-friendly predicate (add an index on `valid_from`/`valid_to` if needed). decay/prune are batch
jobs (off the query path), ideally run by the reflection daemon. History growth is bounded by pruning.

## Tasks

- [ ] validity filters + `as_of` query modifier (+ optional index)
- [ ] `supersede` (close old / open new) in a transaction
- [ ] `decay_confidence` + `prune` batch ops (exclude structural edges)
- [ ] planner `as_of` / confidence-floor options
- [ ] zero-dep tests with explicit timestamps

## Open questions

- **Full bitemporal** (separate transaction-time vs valid-time) or valid-time only? **Lean** valid-time only for v1.
- **Confidence calibration** (are 0.6 edges right 60% of the time?) — needs labeled data from the eval harness. **Lean**
  treat confidence as ordinal for now; calibrate later.

## Risks

- **Write tax** on every fact (supersede instead of update) → only agent-memory uses it; code path stays simple.
- **Clock/timezone bugs** → enforce UTC ISO-8601; pass time in (never `Date.now()` in core logic).

## Review remediation (2026-06-22)

**Corrected by [TD-009](../../decisions/TD-009-versioned-identity-for-temporal-history.md) — the §"supersede" above
cannot work as written:** `nodes.uid` is `UNIQUE` (verified: a duplicate-uid insert raises `IntegrityError`), so you
cannot keep two time-versions under the same uid. Instead:

- **supersede(uid, new, at):** in one transaction, copy the current `nodes` row into **`node_history`** with
  `valid_to = at`, then **UPDATE the live row in place** (`valid_from = at`, `valid_to = NULL`). The live `nodes` row
  keeps its UNIQUE uid.
- **as_of(t):** the live row when `valid_from <= t`, else the `node_history` row whose `[valid_from, valid_to)`
  contains `t` (union live + history).
- `decay_confidence` / `prune` operate on **facts only** and skip structural code edges. The interface signatures above
  stand; only the storage mechanism changes (history table, not duplicate uids).

## References

- [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md), [TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)
- [memory-adapter-agent-memory.md](../completed/memory-adapter-agent-memory.md), [reflection-daemon.md](reflection-daemon.md)
