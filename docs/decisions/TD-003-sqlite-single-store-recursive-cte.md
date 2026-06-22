---
id: TD-003
title: "SQLite is the whole store: relational + graph + vectors, with recursive-CTE traversal"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [storage, sqlite, graph, recursive-cte, networkx]
---

# TD-003: SQLite as the single store (relational + graph + vectors)

## Context

MemoryDB needs three capabilities over the same entities: relational filters, a graph (edges + multi-hop traversal), and vector similarity. The classic answer is three systems (e.g. PostgreSQL + Neo4j + a vector store) stitched at the app layer.

## Decision

**One SQLite database** holds everything: `nodes`, `edges`, `embeddings` tables ([../../src/memorydb/schema.sql](../../src/memorydb/schema.sql)). Multi-hop traversal is done with `WITH RECURSIVE` **CTEs** in SQL ([../../src/memorydb/query.py](../../src/memorydb/query.py)). **NetworkX is loaded on demand** over a *fetched subgraph* for graph algorithms (centrality, PageRank, path scoring) — it is **never** the source of truth.

## Rationale

Embedded, single-file, durable, and every modality is **joinable in one query** (filter nodes by SQL, follow edges, rank by vector). SQLite 3.50 ships `json_each`, `RETURNING`, and recursive CTEs — enough to express seed→traverse→subgraph without an external graph engine. NetworkX-as-truth would mean a separate in-memory graph to persist and keep in sync; SQLite-as-truth + on-demand NetworkX avoids that.

## Consequences

- **Positive:** zero-ops, one file to back up; cross-modal joins are trivial; recursive CTE covers moderate-depth traversal well.
- **Negative:** very deep or very dense traversals can be slow in pure CTE. Mitigate with depth bounds, relation filters, and pulling the subgraph into NetworkX for heavier algorithms.

## Alternatives Considered

### Neo4j (or another graph server) for the graph
Rejected: a server process and operational weight; not embeddable in the Python framework ([TD-001](TD-001-embedded-substrate-not-distributed-tj.md)).

### NetworkX as the primary graph store
Rejected: in-memory, not durable, and cannot join against SQL filters or vectors in one query.

### DuckDB instead of SQLite
Considered (great analytics). SQLite chosen for `sqlite-vec` maturity, ubiquity, and loadable-extension support already confirmed on this host.
