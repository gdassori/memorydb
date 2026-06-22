---
title: "Indexer & ingestion pipeline"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-003, TD-005, TD-006]
components: [store, adapters, indexer]
---

# Indexer & ingestion pipeline

> The `Indexer` turns a directory tree into the substrate: walk → extract (via an `Extractor`) →
> upsert nodes/edges into the `Store` ([TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md))
> → schedule embeddings ([TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)). It is
> **incremental** (re-index only what changed) and **resumable**.

## Goal

`Indexer(...).index(root)` builds/updates the graph for a repo, skipping unchanged files, deleting
symbols of removed files, and resolving cross-file edges. Done = re-running on an unchanged tree is a
no-op (0 re-parses, 0 re-embeds); changing one file touches only that file's symbols + their stale embeddings.

## Background & constraints

Edges need both endpoints to exist, and calls cross files, so a naive per-file upsert loses forward
references. The Store is single-writer (SQLite). Extraction is pluggable per language
([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)); the Indexer owns
*orchestration*, not parsing.

## Approach

A two-pass index: **pass 1** upserts all `file` and symbol nodes across every changed file and records a
content hash per file; **pass 2** upserts edges, resolving endpoints by `uid` against the now-complete
node set, buffering and reporting any still-unresolved edges. After upserts, the embedding hook re-embeds
`dirty_nodes()` in batches.

## Data model & interfaces

```python
class Indexer:
    def __init__(self, store, extractors: "ExtractorRegistry", embedder=None,
                 ignore: "IgnoreMatcher | None" = None, batch_files: int = 64) -> None: ...
    def index(self, root: str) -> "IndexReport": ...

@dataclass
class IndexReport:
    files_seen: int; files_indexed: int; files_skipped: int; files_deleted: int
    nodes_upserted: int; edges_upserted: int; edges_unresolved: int; embedded: int
```

A `file`-type `Node` carries `attrs = {sha256, mtime, lang}` and `uid = relpath`. Symbol nodes get an
edge `file --DECLARES--> symbol` so deletion cascades cleanly.

## Algorithm / step-by-step

1. **Discover:** walk `root`, apply `ignore` (gitignore-style), skip binaries/oversized files, resolve
   symlink policy. Produce the on-disk file list.
2. **Diff:** for each file compute `sha256`; compare to the stored `file` node's `attrs.sha256`.
   - unchanged → skip; changed/new → mark for re-index; in DB but not on disk → mark for delete.
3. **Delete:** remove `file` nodes for deleted/changed files → FK `ON DELETE CASCADE` drops their symbols
   and edges (and embeddings). Re-add the `file` node for changed files.
4. **Pass 1 (nodes):** for each changed file, `extractor.extract(path)` → upsert all nodes (one
   transaction per file or per `batch_files`). Store the new hash on the `file` node.
5. **Pass 2 (edges):** upsert edges by `uid`; if an endpoint is missing, buffer it; after all files,
   retry buffered edges once, then count the rest as `edges_unresolved` (logged, not fatal).
6. **Embed:** fetch `store.dirty_nodes()`, hand to the `EmbeddingPipeline`
   ([graph-aware-embedding-pipeline.md](graph-aware-embedding-pipeline.md)) in batches.

**Worked example:** index a 3-file repo (`a.py` calls `b.py`'s `f`). Pass 1 creates both files' symbols;
pass 2 resolves `a::g --CALLS--> b.py::f`. Edit `a.py` only → its `file` node hash differs → its symbols
dropped+re-extracted, `b.py` untouched; the new `a::g` embedding is re-computed, `b::f`'s is not.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/indexer.py` | **New** — `Indexer`, `IndexReport`, two-pass logic |
| `src/memorydb/adapters/code/registry.py` | **New/Modify** — `ExtractorRegistry` (ext → extractor) |
| `src/memorydb/ignore.py` | **New** — gitignore-style `IgnoreMatcher` |
| `src/memorydb/store.py` | **Modify (maybe)** — add `delete_file(uid)` convenience (cascade) |

## Edge cases & failure modes

- **Huge / binary files:** size + null-byte sniff → skip; counted in the report.
- **Parse failure:** record on the `file` node (`attrs.parse_error`), keep going.
- **Interrupted index:** hashes are written only after a file's symbols commit → a re-run resumes (changed
  files still differ; committed ones skip).
- **File changed language** (rename `.py`→`.go`): hash differs → old symbols cascade-deleted, new extractor runs.
- **Moved/renamed file:** old `uid` (relpath) disappears → delete; new appears → add. (Content-hash rename
  detection is an Open Question.)
- **Unresolved cross-file edge:** never fatal; surfaced in `IndexReport.edges_unresolved`.

## Test plan

Zero-dep with a **FakeExtractor** (returns canned nodes/edges per fixture path):

- `test_index_counts` — index a temp tree → expected node/edge counts; unresolved == 0.
- `test_incremental_skip` — re-index unchanged → `files_indexed == 0`.
- `test_change_one_file` — edit one fixture → only its symbols change; its nodes become `embed_dirty`.
- `test_delete_cascades` — remove a file → its nodes/edges/embeddings gone (FK cascade).
- `test_forward_reference` — file A references B defined later in iteration order → edge resolves in pass 2.

## Performance & scale

O(files) hashing + O(changed files) parsing; embeddings dominate cost (mitigated by only embedding dirty
nodes). Single writer; optional parallel *parsing* with a single writer thread is an Open Question. Comfortable
to tens of thousands of files for an embedded use case.

## Tasks

- [ ] directory walk + `IgnoreMatcher` + binary/size/symlink policy
- [ ] per-file sha256/mtime diff against `file` nodes; delete + cascade for removed/changed
- [ ] two-pass node-then-edge upsert with buffered unresolved edges + report
- [ ] embedding hook over `dirty_nodes()` in batches
- [ ] `IndexReport` + structured logging
- [ ] zero-dep tests with `FakeExtractor` (counts / incremental / change / delete / forward-ref)

## Open questions

- **Rename detection** by content hash (move edges instead of delete+add)? **Lean** defer; delete+add is
  correct, just loses history.
- **Parallel parsing** with a single writer thread/queue? **Lean** start single-threaded; add a worker pool
  only if indexing becomes the bottleneck (embedding usually is).

## Risks

- **Cascade deletes too much** if `file --DECLARES--> symbol` edges are wrong → test the cascade explicitly.
- **Hash thrash** on files with volatile content (generated) → ignore rules should exclude them.

## References

- [TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md), [TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md), [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)
- [code-adapter-treesitter.md](code-adapter-treesitter.md), [graph-aware-embedding-pipeline.md](graph-aware-embedding-pipeline.md)
