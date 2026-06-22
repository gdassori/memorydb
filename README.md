# MemoryDB

An **embedded knowledge substrate** — relational + graph + vectors in one SQLite file —
for giving a local LLM memory and real code understanding. Inspired by
[AkasicDB](docs/research/akasicdb-2026.md), but deliberately *not* a distributed query
engine: see [docs/why-these-choices.md](docs/why-these-choices.md).

## The thesis

1. AkasicDB's core innovation (the **Traversal-Join cost model**) solves a *distributed-scale*
   bottleneck an **embedded** DB doesn't have ([TD-001](docs/decisions/TD-001-embedded-substrate-not-distributed-tj.md)).
2. So MemoryDB is a small **SQLite substrate** + **Python orchestration**, not a query engine.
3. The leverage for code is **representation**: a deterministic symbol graph + graph-aware
   embeddings, with **intent routing** so `where is X used?` hits the graph, not the vectors.

## Architecture

A domain-agnostic core (`Node` / `Edge` / `Vector`) with injectable ports
(`Embedder`, `IntentClassifier`, `Extractor`); code and agent-memory are **adapters** on top
([TD-002](docs/decisions/TD-002-ports-and-adapters-generic-substrate.md)).

```
adapters/  -> CodeAdapter (tree-sitter)        MemoryAdapter (later)
core/      -> store · query (recursive CTE) · vector · planner (intent routing)
storage/   -> SQLite (+ optional sqlite-vec)
```

The **core has zero third-party dependencies** and runs out of the box
([TD-004](docs/decisions/TD-004-zero-dep-core-bruteforce-vectors.md)).

## Quickstart

```python
from memorydb import MemoryDB

# One facade wires the store, an extractor, the embedder, the indexer and the planner together.
# Defaults run out of the box; pass embedder=<your model> for production-quality retrieval.
db = MemoryDB.open("repo.db")            # or ":memory:" for an ephemeral, single-process store
db.index("~/src/orbital")               # walk -> extract symbols+edges -> graph-aware embeddings

db.ask("where is send_notification used?")   # -> LOCATE (exact graph, no vectors)
db.ask("how do notifications work?")         # -> EXPLAIN (vector seed + traversal)
db.context("how do notifications work?")     # -> token-budgeted, LLM-ready ContextResult

db.locate("send_notification")               # exact references list
db.store, db.planner                         # escape hatches to the raw substrate (TD-002)
```

Every port is overridable — `MemoryDB.open(path, embedder=…, extractors=…, classifier=…,
vector_index=…)` — and the lower-level `Store` / `RetrievalPlanner` / `Indexer` remain importable
for direct use.

## Status

v0 substrate + retrieval planner — see [docs/specs/active/v0-substrate.md](docs/specs/active/v0-substrate.md).
Optional extras: `pip install -e '.[vector]'` (sqlite-vec), `'.[code]'` (tree-sitter), `'.[graph]'` (networkx), `'.[dev]'` (pytest).

## Tests

```bash
python tests/test_substrate.py      # stdlib only, no installs
# or
pytest                              # needs the [dev] extra
```
