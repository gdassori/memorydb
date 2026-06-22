---
id: TD-001
title: "MemoryDB is an embedded substrate, not a distributed DBMS — drop the Traversal-Join cost model"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [architecture, scope, akasicdb, embedded]
---

# TD-001: Embedded substrate, not a distributed DBMS

## Context

AkasicDB (KAIST / GraphAI, "Chimera" technology) unifies vector + graph + relational into a **single physical execution plan** via the **Traversal-Join (TJ) operator** and one unified cost model, claiming up to 20× latency and +78% accuracy for enterprise RAG ([research/akasicdb-2026.md](../research/akasicdb-2026.md)). The seductive read is *"MemoryDB needs a TJ operator too."*

## Decision

MemoryDB is an **in-process, single-file embedded library** (Python over SQLite). We do **NOT** build a cost-based query planner or a TJ physical operator. We compose vector + graph + relational retrieval in **Python orchestration** (see [TD-007](TD-007-intent-routed-retrieval-tj-is-orchestration.md)).

## Rationale

The TJ operator's real win is killing the **network/IPC boundary** of "query 3 databases separately and join at the application layer" at enterprise/distributed scale. In an embedded single-process DB the "application-layer join" is **already in-process over local memory** — that boundary does not exist, so a cost-based fusion has almost nothing to recover. We capture ~95% of the user-facing value (one call → vector + graph + SQL together) with orchestration code, and forgo only the part that pays off at a scale we do not target. The leverage for *code* is representation, not the query engine ([TD-006](TD-006-graph-aware-embeddings-staleness.md)).

## Consequences

- **Positive:** tiny, dependency-light, debuggable; the "TJ operator" becomes a readable Python function; no planner/cost-model engineering; effort goes into representation and retrieval routing instead.
- **Negative:** no cross-modal cost optimization. If we ever reach >~10⁶ nodes with latency-critical fused queries, this is the TD to revisit.

## Alternatives Considered

### Build a real unified planner + cost model (a true TJ operator)
Rejected: months of database-systems engineering to optimize a boundary the embedded use case does not have.

### Use an external server DBMS (Neo4j + pgvector, or AkasicDB itself)
Rejected: defeats the goal of being embedded inside our Python inference framework; adds operational weight, deployment, and network hops.
