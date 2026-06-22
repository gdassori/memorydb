# Adversarial review — TDs & specs (2026-06-22)

> Method: each TD and spec read against the **actual v0 source** (ground truth) and against each other
> (cross-document consistency), plus **live SQLite probes** to confirm/refute the riskiest claims. Severity:
> 🔴 breaks as written · 🟠 real gap/inconsistency · 🟡 minor · ✅ holds.

## Probe evidence (run against v0)

| # | Probe | Result | Implication |
|---|-------|--------|-------------|
| 1 | `traverse` on a 2-cycle A↔B, depth 5 | finite `[A,B]` | recursive CTE terminates on cycles ✅ |
| 2 | `references_to("send")` with two files defining `send` | returns **both** | LOCATE is name-ambiguous (C4) |
| 3 | insert second row with an existing `uid` | `IntegrityError: UNIQUE` | temporal supersede impossible (C1) |
| 4 | `'pkg/q/x.py::F' GLOB 'pkg/q/*'` | `1` (match) | `path_glob` matches across `/` (recursive) |
| 5 | `upsert_edge('c1','missing',CALLS)` | `KeyError` | coarse name-only edges have no home (C2) |

---

## Critical / cross-cutting findings

### C1 🔴 Temporal "supersede" contradicts `nodes.uid UNIQUE`
`temporal-confidence-machinery` (and the contradiction handling in `memory-adapter`) require keeping multiple
time-versions of the same fact: "close the old row (`valid_to`), insert a new open row." But `schema.sql` has
`uid TEXT NOT NULL UNIQUE` (probe 3: a duplicate uid is rejected). **The central temporal mechanism cannot exist
against the current schema.** This also falsifies part of TD-008's premise that "the reserved *columns* make the
future cheap" — temporal needs a **schema/identity change**, not just columns.
**Fix:** version identity — e.g. `node_versions(node_id, valid_from, valid_to, …)` with `nodes` holding the
current row, or a composite identity `(uid, version)` and a partial unique index on the *open* row only. Requires a
migration + a corrective TD.

### C2 🔴 Coarse "global-name" edges (TD-005 tier 0.3) are unrepresentable
`code-adapter` says the bare-name tier is "deferred to the Indexer's pass 2," but `Extractor.extract(path)` returns
concrete `Edge`s whose `dst` is a **uid**, and `store.upsert_edge` raises `KeyError` for a missing endpoint (probe 5).
There is **no representation** for "an edge to a name I can't resolve yet," so the 0.3 tier cannot be produced and
pass-2 buffering has nothing to persist.
**Fix:** add a pending/unresolved-edge representation — e.g. a `pending_edges(src_id, dst_name, relation, confidence)`
table the indexer drains in pass 2, or extend `Edge`/`upsert_edge` with a `dst_kind="name"` path. Touches models,
schema, code-adapter, indexer.

### C3 🔴 vec0 dimension is hardcoded and known too late
`sqlite-vec-acceleration` and `schema-migrations` create `vec_items … embedding float[768]`, but the embedding
dimension is **configurable** (`HashingEmbedder` default 64; real models 384/768/1024/1536). Worse, migrations run in
`Store.__init__` — **before any embedding exists**, so the dim is unknown. And `public-api-facade`'s default embedder
(dim 64) + `[vector]` ⇒ dim mismatch.
**Fix:** do **not** create `vec0` in a static migration. Create it lazily on first `set_embedding` (dim now known),
persist the dim in a `meta` table, and `rebuild_index()` on change. Update all three specs.

### C4 🟠 LOCATE is not "exact" — it resolves by bare name
`references_to` matches `tgt.name = :name OR tgt.uid = :name`; probe 2 shows `"send"` returns callers of **every**
`send` across the repo. TD-007/v0-substrate sell LOCATE as exact; it is exact *given a uid*, but the query→symbol step
(regex `_symbol`) yields a bare, ambiguous name.
**Fix:** resolve queries to a **uid** (the LLM classifier can; the regex default cannot). Have `references_to` accept a
uid, and the planner group results by target uid (or disambiguate). Document the limitation explicitly.

### C5 🟠 The symbol→file association is assumed but unspecified
`llm-intent-classifier` (FILTER `since`) and `hybrid-ranker` (recency) need the owning file's `mtime`; `context-builder`
needs the path. The path is derivable from the uid prefix ✅, but `mtime` lives on the `file` node and **no spec defines
the symbol→file access path** (the `file --DECLARES--> symbol` edge from the indexer is never traversed by these SQLs;
the `JOIN file f …` in the FILTER builder is undefined).
**Fix:** denormalize `file_uid` (and `mtime`) into symbol `attrs`, or define a single helper join. Update FILTER + ranker.

### C6 🟠 vec0 metric vs brute-force cosine
`BruteForceVectorIndex` computes **cosine** (normalizes per query). `vec0` defaults to **L2**. The spec hand-waves "state
the metric." If unaddressed, the two backends rank differently → the fallback is not transparent.
**Fix:** pin cosine for `vec0` (column `distance=cosine` if the pinned sqlite-vec supports it, else store L2-normalized
vectors) and add a cross-backend agreement test.

### C7 🟡 `enable_load_extension` may be unavailable
Capability detection in `make_vector_index` must also handle Python builds where `enable_load_extension` is missing/
disabled (raises `AttributeError`/`OperationalError`), not only a missing extension file. (Verified present on this host.)

---

## TDs, one by one

- **TD-001** ✅ Sound. 🟡 "95% via plain orchestration" glosses over **join order** (vector-first vs filter-first matters
  even embedded); partly mitigated since FILTER does SQL-first. Add a sentence acknowledging order-sensitivity.
- **TD-002** ✅ Sound and now exercised by the specs. The "thin-wrapper" risk is real but answered by the planner +
  staleness. No change.
- **TD-003** ✅ Recursive CTE verified to terminate on cycles (probe 1). 🟡 Make explicit that any NetworkX/PageRank
  score cache is *derived/rebuildable* so it doesn't read as "NetworkX as truth."
- **TD-004** ✅ Good default. 🟡 brute force recomputes per-vector norms every query (O(n·d)); precompute norms. The
  ~1e5 ceiling is asserted, **unbenchmarked**.
- **TD-005** ✅ direction. 🟠 tier values (0.9/0.6/0.3) are **unvalidated**, and LOCATE's "≥0.9 = trustworthy" is
  **circular** (only the still-unbuilt eval-harness could validate). 🔴 inherits C2 (the 0.3 tier is unrepresentable).
- **TD-006** ✅. 🟠 doc drift: the TD doesn't mention the rename-staleness case its own pipeline spec introduces — and
  see the rename contradiction under `graph-aware-embedding-pipeline`.
- **TD-007** ✅ routing principle. 🟠 weakened by C4 (LOCATE ambiguity). The regex `_symbol` picks the **last**
  identifier-ish token → brittle ("…referenced in BarBaz" picks `BarBaz`). Acknowledged via the LLM classifier, but
  the default is weak.
- **TD-008** ✅ as a deferral decision. 🔴 its premise that the reserved *columns* suffice is **partly false** (C1: temporal
  needs versioned identity, a schema change). Update the TD to say so.

## Specs, one by one

- **v0-substrate** ✅ implemented + green. 🟠 inherits C4 (the "exact LOCATE" claim).
- **code-adapter-treesitter** 🔴 C2. 🟠 `relpath::qualname` + `#N` overload suffix is **order-dependent** → unstable
  uids across re-parses → churn/duplicate symbols. 🟡 reusing language-pack *tags* queries won't uniformly capture
  CALLS/IMPORTS; expect more hand-written `.scm`.
- **python-precise-resolver** ✅ strong idea. 🔴 **supersession bug:** it relies on "higher confidence wins on upsert,"
  but `store.upsert_edge` does `ON CONFLICT … DO UPDATE SET confidence=excluded.confidence` — an unconditional
  overwrite. A later coarse pass would **downgrade** a precise edge. Needs `confidence = max(old, new)` (and ordering
  guarantees). 🟡 cross-module needs the global table (acknowledged).
- **schema-migrations** 🟠 C3 (vec0 dim). 🟡 test text says "v0 schema + `user_version=1`," but a real v0 DB is
  `user_version=0`; re-running migration 1 over an existing v0 DB is safe only via `IF NOT EXISTS` — state it.
- **indexer-ingestion-pipeline** 🔴 C2 (unresolved edges have nowhere to live). 🔴 **delete-cascade fallacy:** "delete the
  `file` node → its symbols cascade" is **false** — FK cascade deletes a node's *edges*, but symbols are not FK-children
  of the file node, so deleting the file leaves orphan symbols. Fix: give symbols a `file_id` FK (or delete symbols
  explicitly by file association). 🟠 pass-2 buffering ties to C2.
- **graph-aware-embedding-pipeline** ✅ mostly. 🟠 needs a "node neighborhood" query (in+out edges with neighbor names)
  that v0's `query.py` lacks — add it. 🔴 **rename contradiction:** under the code uid scheme `relpath::qualname`, a
  rename **changes the uid** → it is delete+add, not a rename, so the "mark depth-1 neighbors on rename" machinery is
  **moot for code** (relevant only to agent-memory entities). Reconcile with TD-006.
- **sqlite-vec-acceleration** 🟠 C3, C6, C7. 🟡 node delete must remove the vec row (vec0 has no FK cascade) — easy to desync;
  lean on `rebuild_index` as backstop.
- **llm-intent-classifier** 🟠 C5 (`JOIN file f` undefined). 🟠 `since` compares `json_extract(…'$.mtime') >= '2026-06-15'`
  — pin the mtime **format** (ISO string for lexical compare; epoch numbers break the text compare via type affinity).
  ✅ injection guard + hallucinated-symbol downgrade are good.
- **context-builder-packing** ✅ solid. 🟡 chars/4 token heuristic under-counts code (punctuation-dense) → budget overrun;
  add an explicit safety margin and document the risk.
- **graph-algorithms-networkx** ✅. 🟡 whole-graph PageRank ceiling unbenchmarked; degree fallback is fine.
- **hybrid-ranker** 🟠 C5 (recency mtime). 🟠 min-max centrality normalization **divides by zero** for a single candidate
  / all-equal scores — the "stable order" claim needs a guard. 🟡 weight renormalization is good.
- **public-api-facade** 🟠 C3 (default `HashingEmbedder` dim 64 vs vec0). 🟡 `ask(as_context=True)` makes the return type a
  union — document it.
- **cli** ✅. 🟡 **default mismatch:** CLI `--db` defaults to `./memorydb.sqlite` (persistent) while the facade defaults to
  `:memory:` (ephemeral) — align or document.
- **eval-harness** ✅ and pivotal. 🟠 **build-order problem:** it is the *only* validator for TD-005 confidence tiers and the
  ranker weights, yet it sits near the end of the suggested order — pull it **earlier** so those claims aren't unvalidated
  for long. 🟡 nDCG grading basis is vague.
- **memory-adapter-agent-memory** 🟠 inherits C1 (contradiction "keep both" needs temporal versioning → uid wall). 🟡 two
  entity-creation paths (`entity()` vs `remember(entities=…)`) risk duplicates if normalization differs.
- **concept-ontology-layer** ✅ as deferred design. 🟡 a `Concept` node has no code-ish `attrs.signature`, so the
  `DefaultSerializer` must be type-aware (ties to the adapter-specific serializer in the embedding spec).
- **temporal-confidence-machinery** 🔴 C1 — its core `supersede` is impossible against the schema today; **highest-value fix.**
- **reflection-daemon** ✅ north-star. 🟠 `since_cursor` by node id breaks under delete+reinsert (re-indexed nodes get new ids →
  reprocessed; deletions invisible) — use a change log or timestamp cursor. Depends on C1/concepts.

---

## Remediation priority

1. **C1 / temporal identity** (corrective TD + migration) — blocks temporal + agent-memory contradiction handling.
2. **indexer delete-cascade fallacy** + **C2 unresolved edges** — both block a correct first real indexer.
3. **python-precise-resolver supersession** = `max(confidence)` in `upsert_edge` — small change, prevents silent downgrades.
4. **C3 vec0 dim** (lazy creation + dim in meta) — blocks `[vector]`.
5. **C4 LOCATE-by-uid** + **C5 symbol→file denormalization** — correctness/UX of the two headline query paths.
6. **C6 vec metric**, **C7 ext detection**, hybrid-ranker normalization guard, cli/facade default alignment, eval-harness reordering.

Net: the **design direction holds**; the failures are at the **seams** (identity/uniqueness, unresolved references, file
linkage, vector dim) — exactly where a spec-only pass tends to be optimistic. None invalidate a TD outright; TD-008 and a
few specs need corrective edits.

---

## Resolutions (2026-06-22)

All findings remediated the same day. Code fixes are implemented and covered by `tests/test_remediation.py` (suite
green); spec/TD fixes are in-doc (each affected spec has a **Review remediation** section).

| Finding | Resolution |
|---|---|
| C1 temporal vs `uid UNIQUE` | **[TD-009](../decisions/TD-009-versioned-identity-for-temporal-history.md)** added (history tables; current row keeps UNIQUE); `temporal-confidence-machinery` + TD-008 corrected |
| C2 unresolved coarse edges | `pending_edges` mechanism specified in `code-adapter-treesitter` + `indexer-ingestion-pipeline` |
| C3 vec0 dimension | lazy creation at the embedder's dim + dim in `meta`; `sqlite-vec-acceleration`, `schema-migrations`, `public-api-facade` updated |
| C4 LOCATE ambiguity | **code:** planner grounds the symbol against the index + returns `ambiguous`/`matched_uids`; TD-007 + v0-substrate updated |
| C5 symbol→file linkage | `attrs.file_uid` (+ mtime/lang) denormalization; `indexer`, `llm-intent-classifier`, `hybrid-ranker` updated |
| C6 vec metric | cosine pinned for vec0 + cross-backend agreement test |
| C7 `enable_load_extension` | detection/fallback covers missing-or-disabled |
| upsert downgrade bug | **code:** `upsert_edge` now `confidence = MAX(old, new)`; `python-precise-resolver` updated |
| indexer delete-cascade fallacy | delete-by-`attrs.file_uid`, not file-node FK cascade |
| code uid overload churn | deterministic `#N` suffix by byte offset |
| hybrid-ranker div-by-zero | normalization guard (`range == 0 → 0.5`) |
| node-neighborhood query | **code:** `query.node_neighborhood` added |
| cli/facade default mismatch | documented (CLI persistent, facade `:memory:`) |
| eval-harness ordering | pulled earlier in the build sequence |
| reflection cursor | timestamp/change-log cursor, not node id |

Net: every breaker is closed; the headline query paths (`LOCATE`/`EXPLAIN`) and the supersession invariant are now
enforced in **v0 code**, and the deferred tracks have a sound schema path via TD-009.
