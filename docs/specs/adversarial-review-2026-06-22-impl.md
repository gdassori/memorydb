# Adversarial implementation review — 2026-06-22 (round 2, post-implementation)

Four parallel adversarial agents (performance, security, correctness/consistency, spec-adherence) reviewed
**all implemented code** after v0-substrate → eval-harness (52 tests green). Every finding below was
substantiated against the real code with a repro. This is the *implementation* review; the earlier
[adversarial-review-2026-06-22.md](adversarial-review-2026-06-22.md) reviewed the *specs*.

Severities: **Critical** (data loss / contract break), **High**, **Medium**, **Low**.

## Findings register

| ID | Sev | Area | Location | Issue | Status |
|----|-----|------|----------|-------|--------|
| I1 | High | security/DoS | adapters/code/__init__.py:103,128 | Hostile deeply-nested source → uncaught `RecursionError` (`_extract_tree` is called *outside* `extract`'s try/except) aborts the whole index run | **fixed** |
| I2 | High | correctness | embedding_pipeline.py:92-102 | `_embed_batch` never checks `len(vecs)==len(batch)`; an embedder returning fewer vectors silently drops trailing nodes, marks them embedded, leaves them `embed_dirty=1` forever | **fixed** |
| I3 | High | correctness/incremental | indexer.py:98-125 | Cross-file *pending* edges only resolve when the **caller** file is re-extracted; if the callee appears/reappears in another (unchanged) file, the edge is permanently missing | **deferred** (needs design) |
| I4 | High | spec-adherence | vector.py:78-81 | C7 (`enable_load_extension` detection) marked resolved in the ledger but `make_vector_index` catches only `NotImplementedError` | **fixed** (broadened except) |
| I5 | Med | security | indexer.py:134-143,89 | Symlinked *files* are read outside the indexed root (info disclosure); `os.walk` only blocks symlinked dirs | **fixed** |
| I6 | Med | correctness | adapters/code/__init__.py:147,167 | Flat `local` name→uid map (first-wins) resolves same-name methods across classes to the wrong class at **confidence 0.9** | **fixed** (ambiguous → pending) |
| I7 | High | perf | vector.py:44-55 | `BruteForceVectorIndex.search` reloads+renorms+full-sorts every embedding per query | **partial** (heapq + SQL type filter + k clamp now; pre-norm/cache → sqlite-vec spec) |
| I8 | High | perf | indexer.py:168-173 | `_delete_file` filters on `json_extract(attrs,'$.file_uid')` → full table scan per changed file (O(files×nodes)) | **fixed** (indexed generated column, migration 3) |
| I9 | Med | perf | embedding_pipeline.py:92 / query.py:81 | Serialization issues 3 SELECTs/node, ignoring batching | **partial** (reuse fetched row now; batch neighbourhood fetch deferred) |
| I10 | Med | spec-adherence | indexer.py:103 | `mtime` promised on file nodes (C5, FILTER/ranker join key) but never persisted | **fixed** |
| I11 | Med | perf | store.py:25 | `synchronous` left at FULL under WAL | **fixed** (NORMAL) |
| I12 | Low-Med | perf | store.py:127 | `dirty_nodes` scans `nodes` on `embed_dirty` (no index) | **fixed** (partial index, migration 3) |
| I13 | Low | correctness | vector.py:55 | `search(k<0)` returns `scored[:-1]` (near-full leak) | **fixed** (clamp) |
| I14 | Low | correctness | vector.py:25 | `_cosine` zips mismatched dims → silent truncation (facade only warns) | **deferred** (covered by facade warn + I2 guard; full guard → sqlite-vec) |
| I15 | Low | consistency | indexer.py:103-130 | `IndexReport.embedded` counts file nodes; `nodes_upserted` counts only symbols | **deferred** (documented; file-node embedding policy is its own decision) |
| I16 | Low | correctness | eval/__init__.py:70-83 | `ndcg_at_k(gains={})` zeroes a labelled case | **fixed** (`if gains:` → binary fallback) |
| I17 | Low | perf | query.py:53-61 | `traverse` recursive CTE has no per-node fan-out cap (hub blow-up) | **deferred** (→ hybrid-ranker / context-builder) |
| I18 | Low | docs | docs/specs/README.md, frontmatter | 5 implemented specs still marked `planned`/`active`; CLI subcommand shorthand stale | **fixed** |
| I19 | Low | docs | code-adapter / schema-migrations / indexer / graph-aware specs | Spec prose lags impl (IMPORTS never emitted, 0.2 tier, vec0 prose, stale interface blocks) | **partial** (key ones noted; full doc sweep tracked) |

**Verified-clean (refuted false leads):** no SQL injection (all parameterized; `traverse` interpolates only the
constant `_EDGE_VIEW`/`rel_clause`); no `eval`/`exec`/`pickle`/`subprocess`; `EvalCase(**json.loads)` rejects bad
keys cleanly; directory-symlink recursion blocked; nDCG binary+graded math correct; planner `locate`/`explain`
dispatch correct (no caller uses the old `_`-prefixed names); migration BEGIN/rollback sound; use-after-close /
double-close / depth=0 / k>corpus all correct. `--embedder` arbitrary import is **operator-controlled** (Low,
documented trust assumption).

## Deferred items — rationale

- **I3 (cross-file pending re-resolution):** the correct fix persists unresolved pending edges in a table and
  retries them whenever symbol names appear/disappear — a real feature touching schema + indexer. Tracked as a
  follow-up task on [indexer-ingestion-pipeline](active/indexer-ingestion-pipeline.md); **note the upcoming
  [python-precise-resolver](active/python-precise-resolver.md) builds a global def table and can subsume this.**
- **I7/I14 vector overhaul:** TD-004 accepts brute-force O(n) for v0; pre-normalised vectors + ANN are the job of
  [sqlite-vec-acceleration](active/sqlite-vec-acceleration.md). Cheap algorithmic wins applied now.
- **I17 traverse cap:** fan-out budgeting belongs with [hybrid-ranker](active/hybrid-ranker.md) /
  [context-builder-packing](active/context-builder-packing.md).
- **I15 file-node embedding:** excluding `type='file'` nodes needs a dirty-flag policy (else they re-try forever);
  deferred as a deliberate decision, not a quick patch.
