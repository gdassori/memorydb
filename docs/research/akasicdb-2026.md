# AkasicDB — what it actually is (research note, June 2026)

> Source material behind [TD-001](../decisions/TD-001-embedded-substrate-not-distributed-tj.md). The point of this
> note is to separate the **real systems contribution** from the marketing, so we know what NOT to copy.

## One-line verdict

AkasicDB's defensible innovation is a **distributed/enterprise-scale performance optimization** (a fused
physical execution plan with a unified cost model). An **embedded, single-process** MemoryDB does not have the
bottleneck that optimization targets, so we deliberately do not replicate it.

## What it is

- A **"Unified Graph-Vector-Relational DBMS for Enterprise AI"** from Prof. **Min-Soo Kim**'s team at **KAIST**,
  commercialized by **GraphAI Co., Ltd.**; underlying research is branded **"Chimera"**. Demoed at SIGMOD as
  *"Demonstrating Omni RAG with a Unified Vector-Graph-Relational DBMS."*
- **"One dataset, two storage strategies":** data stored both for graph traversal and for relational queries, but
  presented as one logical DB.
- **The Traversal-Join (TJ) operator:** the signature feature. It unifies vector search + graph traversal + SQL
  filtering into **a single execution plan governed by one cost model**, instead of querying three systems and
  joining the results at the application layer.
- Marketing framing: *"the bottleneck in AI response speed is not the model, it's the query executor."*
- Positioned for **Omni RAG**: jointly use semantic similarity (vectors), relationships (graph), and structural
  filters (SQL) to find better evidence — claimed up to **20× speed** and **+78% accuracy** vs conventional RAG,
  by minimizing intermediate data transfers (fewer tokens, lower latency).

## Why the TJ operator is real but not for us

The TJ win comes from **eliminating the network/IPC boundary** of "3 separate databases + app-layer join" at
enterprise scale. MemoryDB is **in-process over local memory** — that boundary does not exist, so a cost-based
fusion has almost nothing to recover. We get ~95% of the *user-facing* value (one call → vector + graph + SQL)
with plain Python orchestration ([TD-007](../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)),
and skip exactly the 5% that only pays off at a scale we do not target
([TD-001](../decisions/TD-001-embedded-substrate-not-distributed-tj.md)).

## What we take instead

The leverage for **code** is **representation**, not the query engine: a deterministic symbol graph as the spine
([TD-005](../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)), graph-aware embeddings
([TD-006](../decisions/TD-006-graph-aware-embeddings-staleness.md)), and intent routing so exact questions hit the
graph, not the vectors ([TD-007](../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)). Their
"ontology" idea maps onto our deferred concept layer ([TD-008](../decisions/TD-008-defer-temporal-confidence-ontology-reflection.md)).

## Sources

- AkasicDB product page — https://graphai.io/en/product/akasicdb/
- ACM (SIGMOD companion), *Demonstrating Omni RAG…* — https://dl.acm.org/doi/10.1145/3788853.3801609
- TechXplore (Chimera / KAIST, June 2026) — https://techxplore.com/news/2026-06-generation-database-ai-hallucinations-accuracy.html
- EurekAlert / KAIST press — https://www.eurekalert.org/news-releases/1132514
