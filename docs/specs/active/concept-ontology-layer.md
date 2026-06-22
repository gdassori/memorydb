---
title: "Concept / ontology layer"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-008, TD-006]
components: [concepts, query]
---

# Concept / ontology layer

> Add a layer of **concept nodes** above the concrete symbol/memory graph — "Mass Notification" →
> `IMPLEMENTED_BY` NotificationService, `STORES` NotificationPreference, `PRODUCES` PushNotification — so
> retrieval can reason at the level of ideas, not just identifiers. Deferred per
> [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md); this spec defines it for when it lands.

## Goal

A `Concept` node type + relations linking concepts to concrete nodes, plus an extractor that proposes concepts
from the indexed graph. Done = `EXPLAIN`-style questions ("how does mass notification work?") can seed on a
*concept* and expand to its implementations, and the eval harness shows improved EXPLAIN recall.

## Background & constraints

This is AkasicDB's "ontology" idea, mapped onto our substrate ([research/akasicdb-2026.md](../research/akasicdb-2026.md)).
Concepts are just `Node`s (`type="Concept"`) with edges to symbols/facts — no schema change beyond optional
`concepts`/`concept_edges` convenience tables (a migration, [schema-migrations.md](schema-migrations.md)). Extraction
is LLM-assisted and must be **proposed, then verified** (concepts are higher-confidence claims than coarse edges).

## Data model & interfaces

```python
class ConceptLayer:
    def __init__(self, store, llm=None) -> None: ...
    def add_concept(self, name: str, description: str = "") -> int: ...
    def link(self, concept: str, relation: str, target_uid: str, confidence: float = 0.8) -> None: ...
    def propose(self, scope_uids: list[str]) -> list["ConceptProposal"]: ...   # LLM over a cluster
    def accept(self, proposal: "ConceptProposal") -> None: ...

@dataclass
class ConceptProposal:
    name: str; description: str
    links: list[tuple[str, str]]   # (relation, target_uid)
    confidence: float
```

Relations: `IMPLEMENTED_BY`, `STORES`, `PRODUCES`, `USES`, `RELATED_TO` (the `Rel` constants already include
several). Concept↔concept edges (`IS_A`, `PART_OF`) form the ontology.

## Algorithm / step-by-step

1. **Cluster:** group concrete nodes (by file/package, by embedding cluster, or by name prefix —
   `MassNotificationService`, `PushNotificationService`, `EmailNotificationService`).
2. **Propose:** ask the injected LLM to name the shared concept + its links to cluster members → `ConceptProposal`.
3. **Verify:** check proposed `target_uid`s exist; drop unknowns; set confidence from agreement/coverage.
4. **Accept:** upsert the `Concept` node + `concept_edges`; mark it `embed_dirty` (it gets a graph-aware embedding too).
5. Concepts then participate in retrieval as first-class seeds.

**Worked example:** cluster {MassNotificationService, PushNotificationService, EmailNotificationService} → concept
"Notification Infrastructure" `IS_A` Notification, `IMPLEMENTED_BY` each service.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/concepts.py` | **New** — `ConceptLayer`, `ConceptProposal` |
| `src/memorydb/migrations.py` | **Modify** — optional `concepts`/`concept_edges` convenience tables |
| `src/memorydb/planner.py` | **Modify** — allow concept nodes as EXPLAIN seeds |

## Edge cases & failure modes

- **Hallucinated links** to non-existent symbols → verification drops them; low coverage → low confidence.
- **Duplicate/again-proposed concepts** → dedupe by normalized name; merge links.
- **Concept drift** as code changes → concepts inherit staleness via their edges (TD-006); re-propose periodically (the reflection daemon).
- **No LLM available:** manual `add_concept`/`link` still works; `propose` is disabled.

## Test plan

Zero-dep with a `FakeLLM` returning canned proposals:

- `test_accept_creates_concept_graph` — proposal → concept node + verified links.
- `test_verification_drops_unknown_targets` — proposal referencing a missing uid → that link dropped.
- `test_concept_seeds_explain` — EXPLAIN seeded on a concept expands to its implementations.
- `test_dedupe_concepts` — re-proposing merges, not duplicates.

## Performance & scale

Proposal is an LLM call per cluster (batched, offline/background). Storage is tiny (few concept nodes per cluster).
Retrieval gains: concept seeds shorten multi-hop EXPLAIN paths.

## Tasks

- [ ] `Concept` node type + concept relations + optional convenience tables (migration)
- [ ] manual `add_concept`/`link`
- [ ] LLM `propose` over clusters + verification + `accept`
- [ ] planner: concepts as EXPLAIN seeds
- [ ] zero-dep tests with `FakeLLM`

## Open questions

- **Clustering method** (package vs embedding vs name)? **Lean** start with package + name heuristics; add embedding
  clustering once it pays off in the eval harness.
- **Auto-accept threshold** vs human-in-the-loop? **Lean** auto-accept ≥0.85, queue the rest for review.

## Risks

- **Ontology bloat / wrong concepts** polluting retrieval → verification + confidence + the eval harness as a guardrail;
  keep concepts a *layer* (easy to rebuild), never load-bearing for LOCATE.

## References

- [TD-008](../../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md), [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)
- [reflection-daemon.md](reflection-daemon.md), [schema-migrations.md](schema-migrations.md), [research/akasicdb-2026.md](../research/akasicdb-2026.md)
