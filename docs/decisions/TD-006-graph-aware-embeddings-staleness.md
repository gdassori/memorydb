---
id: TD-006
title: "Graph-aware (node-context) embeddings, with staleness tracking"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [embeddings, knowledge-graph, retrieval, staleness]
---

# TD-006: Graph-aware (node-context) embeddings with staleness tracking

## Context

Embedding raw source (`def send_notification(): ...`) captures *syntax*, not *role*. For code retrieval the useful signal is the symbol's **role in the graph** — who calls it, what it calls, what it writes, where it lives.

## Decision

Embed a **serialized neighborhood** of each node — name, signature, docstring, incoming/outgoing edges, path — **not** the raw body. Because a node's embedding now depends on its neighbors, when an edge changes the dependent embeddings go **stale**; the substrate tracks this with an **`embed_dirty`** flag (set on node/edge upsert, cleared on (re)embed). Division of labor: the **adapter** decides *how* to serialize a neighborhood; the **framework** provides `embed()`; the **substrate** decides *what* and *when* to (re)embed.

Example serialization:
```
send_notification  (function, services/notifications.py)
signature: (user_id, message, channel) -> NotificationLog
docstring: Send a single notification to a user
called_by: MassNotificationJob, RetryWorker
calls: RedisQueue.push, PushProvider.send
writes: NotificationLog
```

## Rationale

This matches "what is X's role / how does X fit" queries far better than source text, and the serialization is **human-readable** — ideal for feeding an LLM. Honest framing: this is **node-context embedding**, not learned KG embeddings (TransE/node2vec) — simpler, no training step, and better for LLM consumption. The staleness flag is **real value a bare SQLite wrapper does not give** ([TD-002](TD-002-ports-and-adapters-generic-substrate.md)).

## Consequences

- **Positive:** retrieval ranks by role, not surface syntax; re-embedding is incremental (only `embed_dirty` nodes).
- **Negative:** changing a hub node cascades staleness to its neighbors. Bound the cost by batching dirty re-embeds; the cost is embedding calls, not correctness.

## Review note (2026-06-22)

Clarification on staleness: under the code uid scheme (`relpath::qualname`) a *rename* changes the uid, so for
code it is a delete+add, **not** an in-place rename — the "mark depth-1 neighbors on rename" cascade therefore
applies to **agent-memory entities** (stable uids), not code symbols. `upsert_edge` marks both endpoints dirty;
the serializer reads a node's neighbors via the new `query.node_neighborhood` helper
([graph-aware-embedding-pipeline.md](../specs/active/graph-aware-embedding-pipeline.md)).

## Alternatives Considered

### Embed the raw source body
Rejected: role-blind; near-duplicate bodies collide, and a symbol's importance (its edges) is invisible.

### Learned KG embeddings (TransE / node2vec)
Deferred: requires a training step, is not human-readable, and is overkill for v0. Revisit only if node-context embeddings plateau.
