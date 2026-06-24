# MemoryDB implementation specs — backlog & index

Detailed implementation specs for MemoryDB, grounded in the [Technical Decisions](../decisions/)
and the v0 substrate. Every spec follows [`_TEMPLATE.md`](_TEMPLATE.md). Lifecycle:
`planned → active → completed | rejected` (mirrors the `docs/specs/{active,completed,rejected}` dirs
and the `agentic spec` tooling).

> Reading order for implementers: the substrate ([active/v0-substrate.md](active/v0-substrate.md)) is the
> spine; everything below builds on its `Store` / `query` / ports surface.

## 1. Core & storage

| Spec | Scope | TDs | Status |
|------|-------|-----|--------|
| [v0-substrate](active/v0-substrate.md) | SQLite store, recursive-CTE traversal, brute-force vectors, intent planner | TD-002..007 | active |
| [code-adapter-treesitter](active/code-adapter-treesitter.md) | Multilang symbol + coarse-edge extraction via tree-sitter | TD-005 | completed |
| [python-precise-resolver](active/python-precise-resolver.md) | High-confidence Python edges via `ast`/`symtable` | TD-005 | completed |
| [schema-migrations](active/schema-migrations.md) | Schema versioning + forward migrations | TD-003 | completed |
| [indexer-ingestion-pipeline](active/indexer-ingestion-pipeline.md) | Directory walk, incremental (mtime/hash) re-index, deletions, batching | TD-003, TD-005 | completed |

## 2. Embeddings & vectors

| Spec | Scope | TDs | Status |
|------|-------|-----|--------|
| [graph-aware-embedding-pipeline](active/graph-aware-embedding-pipeline.md) | Neighborhood serialization + `embed_dirty`-driven re-embedding | TD-006 | completed |
| [sqlite-vec-acceleration](active/sqlite-vec-acceleration.md) | `SqliteVecIndex` + `vec0` virtual table behind the `VectorIndex` interface | TD-004 | completed |

## 3. Retrieval & ranking

| Spec | Scope | TDs | Status |
|------|-------|-----|--------|
| [llm-intent-classifier](active/llm-intent-classifier.md) | Pluggable LLM router + `FILTER` path + entity extraction | TD-007 | completed |
| [graph-algorithms-networkx](active/graph-algorithms-networkx.md) | On-demand subgraph → PageRank/centrality scoring | TD-003 | planned |
| [hybrid-ranker](active/hybrid-ranker.md) | Fuse vector score + centrality + confidence + recency into one ranking | TD-006, TD-007 | planned |
| [context-builder-packing](active/context-builder-packing.md) | Subgraph → token-budgeted, LLM-ready context | TD-007 | completed |

## 4. API & tooling

| Spec | Scope | TDs | Status |
|------|-------|-----|--------|
| [public-api-facade](active/public-api-facade.md) | Top-level `MemoryDB` class & ergonomic query API | TD-002 | completed |
| [cli](active/cli.md) | `memorydb index / query / locate / explain / status / reembed` command line | — | completed |
| [eval-harness](active/eval-harness.md) | Retrieval-quality benchmarks (LOCATE precision, EXPLAIN relevance) | TD-007 | completed |

## 5. Second product & deferred (north-star)

| Spec | Scope | TDs | Status |
|------|-------|-----|--------|
| [memory-adapter-agent-memory](active/memory-adapter-agent-memory.md) | Entities + episodic/semantic/procedural memory adapter | TD-002, TD-008 | planned |
| [concept-ontology-layer](active/concept-ontology-layer.md) | Auto-extracted concept nodes over the symbol graph | TD-008 | planned |
| [temporal-confidence-machinery](active/temporal-confidence-machinery.md) | Temporal validity queries + confidence decay/calibration | TD-008 | planned |
| [reflection-daemon](active/reflection-daemon.md) | Periodic clustering → concept synthesis ("grow your own ontology") | TD-008 | planned |

---

**Build sequence (suggested):** schema-migrations → code-adapter-treesitter → indexer-ingestion-pipeline →
graph-aware-embedding-pipeline → public-api-facade → cli → (sqlite-vec-acceleration, python-precise-resolver) →
llm-intent-classifier → graph-algorithms-networkx → hybrid-ranker → context-builder-packing → eval-harness →
memory-adapter-agent-memory → concept-ontology-layer → temporal-confidence-machinery → reflection-daemon.

## Review status (2026-06-22)

All findings from [adversarial-review-2026-06-22.md](adversarial-review-2026-06-22.md) are remediated in-doc (every
affected spec carries a dated **Review remediation** section; the v0 code fixes are implemented + tested). Added
[TD-009](../decisions/TD-009-versioned-identity-for-temporal-history.md) (versioned identity) to unblock the temporal
track. **Revised build order:** pull **eval-harness earlier** — right after `public-api-facade` / `cli` — so the
confidence tiers (TD-005) and ranker weights are validated before later specs depend on them; the temporal track
(`temporal-confidence-machinery` → `reflection-daemon`) now follows TD-009.
