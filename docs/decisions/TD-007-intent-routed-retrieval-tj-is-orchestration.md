---
id: TD-007
title: "Intent-routed retrieval; the \"Traversal-Join\" is orchestration, not a cost-based planner"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [retrieval, query-routing, intent, planner]
---

# TD-007: Intent-routed retrieval; the TJ analogue is orchestration

## Context

`"Where is DeviceNotificationService used?"` has an **exact** answer the graph already knows — firing vector search at it is worse than useless. `"How does the notification system work?"` needs a fuzzy entry point *plus* structure. One retrieval strategy cannot serve both.

## Decision

A `RetrievalPlanner` ([../../src/memorydb/planner.py](../../src/memorydb/planner.py)) **routes by intent** via a pluggable `IntentClassifier`:

- **`LOCATE`** → graph-exact: incoming edges to the named symbol, **no vectors**.
- **`EXPLAIN`** → `vector_search` seed → recursive-CTE `traverse` (depth ≈ 2) → return the subgraph.
- **`FILTER`** → SQL over node/file attributes (optionally reranked by vectors).

This composition function **is** MemoryDB's analogue of AkasicDB's Traversal-Join operator — but it is **readable Python orchestration, not a cost-based physical operator** ([TD-001](TD-001-embedded-substrate-not-distributed-tj.md)).

## Rationale

Vectors are used only as **GPS for fuzzy natural language**; determinism is used wherever determinism exists. Keeping the classifier behind a port lets the inference framework swap the default regex router for an LLM-based one without touching the planner.

## Consequences

- **Positive:** exact answers stay exact; vector noise never pollutes `LOCATE`; the planner is trivially unit-testable.
- **Negative:** the default regex classifier will misroute some queries. Mitigate by making it injectable and defaulting **ambiguous → `EXPLAIN`** (the safe, richer path).

## Review note (2026-06-22)

LOCATE is exact *with respect to the graph*, but the query→symbol step is only as good as the extracted symbol.
The regex default now **grounds candidate tokens against the index** (accepting a token only if it names a real
symbol) and **reports ambiguity** when a bare name matches several symbols (`ambiguous` / `matched_uids` in the
LOCATE result). A uid from the LLM classifier ([llm-intent-classifier.md](../specs/active/llm-intent-classifier.md))
disambiguates fully.

## Alternatives Considered

### Vector-first for everything (classic RAG)
Rejected: this is the core mistake of vector-only code retrieval — it cannot answer "where is X used" with the precision the graph already has.

### One fused cross-modal query (a real TJ operator)
Rejected per [TD-001](TD-001-embedded-substrate-not-distributed-tj.md): the cost-model payoff requires a scale/topology embedded MemoryDB does not target.
