# Round-6 review — 2026-06-23 (regression + coverage-gaps)

7 lenses → 3 skeptics/finding. Raised 30, confirmed 24, refuted 6.

> Remediation held but asymmetric (MR-6 #ordinal reached NODE not EDGE/RESOLVE/incremental: R6-1/R6-2/R6-12). New Highs: broken non-Python extraction (Go/Rust/JS/TS), quadratic EXPLAIN, dotted-LOCATE bug. Concurrency contested. 23 findings.

## Confirmed findings (titles / location / fix; synth compressed descriptions)

### R6-1 — High/ — MR-6: precise edges use a BARE src uid, mis-attributing duplicate-qualname-def calls to the wrong ordinal [High 3/3]
**Location:** python_resolver.py:202 vs :164  
**Fix:** _collect_defs records a qual->ordinal-uid map consumed positionally in _walk_edges.

### R6-2 — High/ — MR-6 x MR-3: callee edit permanently drops a 0.97 cross-file CALLS edge into a duplicate-qualname target [High 3/3]
**Location:** indexer.py:274/:312/:320-324  
**Fix:** Persist the resolved dst UID or disambiguate by the caller's import target file.

### R6-3 — High/ — Go: struct/interface/type declarations never extracted; entire Go type graph absent [High 3/3]
**Location:** adapters/code/__init__.py:50/:202-212  
**Fix:** Target type_spec instead of type_declaration.

### R6-4 — High/ — JS/TS: arrow/function-expression functions never extracted; nested-arrow calls mis-attributed to the ancestor at 0.9 [High 3/3]
**Location:** adapters/code/__init__.py:38-47  
**Fix:** Handle variable_declarator/lexical_declaration arrow values named from the LHS identifier.

### R6-5 — High/ — JS/TS: no INHERITS edges; _base_names reads the Python-only superclasses field [High 3/3]
**Location:** adapters/code/__init__.py:242-247  
**Fix:** Make _base_names language-aware (class_heritage + extends_type_clause).

### R6-6 — High/ — Go: method receiver dropped; methods misclassified as function, collide on common names [High 3/3]
**Location:** adapters/code/__init__.py:49/:154/:202-206  
**Fix:** Read receiver, prepend its type to the qualname, set kind=method.

### R6-7 — High/ — Rust: impl-for-trait blocks named after the TRAIT not the implementing type [High 3/3]
**Location:** adapters/code/__init__.py:55/:202-212  
**Fix:** Name the owner from child_by_field_name('type'); emit INHERITS to the trait.

### R6-8 — High/ — subgraph_edges quadratic, reachable from public explain/ask/context; ~27-55s on a hub at depth 2 [High 3/3]
**Location:** query.py:109-121; planner.py:92  
**Fix:** Materialize ids into a TEMP table with an integer PK and JOIN on it (~300-800x).

### R6-9 — High/ — Natural dotted/qualified LOCATE queries return ZERO references for an indexed symbol with real callers [High 3/3]
**Location:** planner.py:23/:59-67/:96-103; query.py:82  
**Fix:** Append a token's bare last component as a candidate; add ':' handling.

### R6-10 — High/ — Concurrent first-open crashes at PRAGMA journal_mode=WAL; busy_timeout not honored; connection leaked [High 2/3, 1 refute->Low]
**Location:** store.py:21-27 (line 25)  
**Fix:** Set busy_timeout, pre-check/retry WAL conversion, close self.conn on failure.

### R6-11 — Medium/ — MR-2 whole-run index() txn holds the WAL write lock the entire index; a second concurrent writer crashes after 5s [Medium 2H/1M/1 refute->Low]
**Location:** indexer.py:89-160 (:157); store.py:21  
**Fix:** Large busy_timeout/app retry, BEGIN IMMEDIATE, or advisory file lock.

### R6-12 — Medium/ — _resolve self-method/module-def TARGETS ignore #ordinal; resolve to the FIRST @overload-stub or shadowed def [Medium 3/3; absorbs F2]
**Location:** python_resolver.py:229-230/:242-243  
**Fix:** Track class_methods/module_defs as name->effective (last non-stub) ordinal uid.

### R6-13 — Medium/ — LOCATE grounds on a query stopword/interrogative that names a real symbol, returning the WRONG symbol [Medium 3/3]
**Location:** planner.py:53-67/:96-103/:18-21  
**Fix:** Add a stopword deny-list; drop _LOCATE-consumed keywords before grounding.

### R6-14 — Medium/ — 3-case sample eval suite non-discriminating; EXPLAIN scores 1.0/1.0/1.0 for a garbage query because k>=corpus [Medium 2/3, 1 refute->Low]
**Location:** eval/suites/sample/cases.jsonl; eval/cli.py:23  
**Fix:** Grow corpus so k<corpus; add distractor/graded-gains/dotted/stopword cases.

### R6-15 — Medium/ — --json on an empty/missing DB emits zero stdout bytes (breaks JSON consumers) while exiting 0 [Medium 3/3]
**Location:** cli.py:128-132/:159-160/:175-176/:195-196  
**Fix:** Emit a valid empty JSON document on stdout when args.json and _no_data().

### R6-16 — Low/ — Rust: each impl block emits a duplicate class node for the same type (Point + Point#1) [Low 3/3]
**Location:** adapters/code/__init__.py:55/:134-167  
**Fix:** Treat impl_item as a scope pushing the impl's TARGET type, not a class node.

### R6-17 — Low/ — Go: unaliased import package paths not captured, downgrading cross-package calls from 0.6 to 0.3 [Low 3/3]
**Location:** adapters/code/__init__.py:249-259/:168-169  
**Fix:** Derive the package name (last path segment) from the import_spec string literal.

### R6-18 — Medium/ — Node/Edge confidence and Edge.weight have no [0,1] bound; a bogus >1.0 confidence permanently dominates the MAX upsert [Medium 2/3, 1 refute->Low]
**Location:** models.py:53/:75-76  
**Fix:** Add Field(ge=0.0,le=1.0) to Node/Edge.confidence and Field(ge=0.0) to Edge.weight.

### R6-19 — Low/ — JS/TS: nested (inner) functions misclassified as method [Low 2/3, 1 refute->none]
**Location:** adapters/code/__init__.py:154; python_resolver.py:166  
**Fix:** Classify as method only when the enclosing scope is a class; in BOTH files.

### R6-20 — Low/ — memorydb-eval compare accepts garbage JSON as a Scorecard and exits 0 with empty deltas [Low 3/3]
**Location:** eval/cli.py:62-72; eval/__init__.py:101-113  
**Fix:** Add ConfigDict(extra='forbid') to Scorecard.

### R6-21 — Low/ — MR-7 _existing_dst doubles the dst id_for lookup (3 per edge not 2) [Low 2/3, 1 refute->none]
**Location:** indexer.py:326-347; store.py:91-92  
**Fix:** Return (uid,id) from _existing_dst into a lower-level upsert, or memoize id_for.

### R6-22 — Medium/ — HashingEmbedder makes EXPLAIN seeds weak (dim=64 collisions); relevant=0.0/rank-last headline is FALSE on realistic input [Medium 2/3, 1 refute->none]
**Location:** embedders.py:14-30  
**Fix:** Drop ~0-cosine seeds; sub-token identifiers; raise dim above 64.

### R6-23 — Low/ — TS interface members (method_signature) not extracted; the new_expression fallback works (fragility note) [Low 2/3, 1 refute->none]
**Location:** adapters/code/__init__.py:43-47/:261-284  
**Fix:** Add method_signature to TS func_types; read child_by_field_name('constructor').

## Coverage gaps

- ORDINAL CONSISTENCY: no audit of every uid-producing site (INHERITS base, star-imports, _existing_dst rewrite).
- NON-PYTHON END-TO-END: verified at extract() only; downstream EXPLAIN/LOCATE for a real repo unmeasured.
- OTHER LANGUAGES/GRAMMAR DRIFT: java/c/cpp unchecked; grammar-version drift unassessed.
- CONCURRENCY BEYOND TWO WRITERS: reader staleness, WAL checkpoint growth, crash recovery, daemon contention.
- DATA INTEGRITY OVER CYCLES: pending_edges growth, orphan rows, embed_dirty lifecycle, dedup attr loss.
- SCALE: vector full-scan at 1e5 symbols, embedding throughput, index() memory.
- REAL RETRIEVAL: no real semantic embedder or hybrid-ranker evaluation.
- INPUT VALIDATION: Node.type enum, Edge.relation, uid format, CLI bounds.
- MIGRATIONS: forward-migration under partial failure not re-verified.

## Priorities

- R6-1+R6-2+R6-12: one MR-6 ordinal-consistency change (edge SRC, MR-3 persistence, edge TARGET).
- R6-3+R6-6: Go type extraction (type_spec) and Go method receivers.
- R6-5+R6-4: language-aware _base_names and JS/TS arrow extraction.
- R6-8: TEMP-table integer-PK join for subgraph_edges.
- R6-9+R6-13: dotted-suffix LOCATE grounding plus a stopword deny-list.
- R6-7: name Rust impl_item owners from the implementing type.
- R6-10+R6-11: busy_timeout, WAL retry, close-on-failure; document single-writer.
- R6-15+R6-14+R6-18+R6-20: empty-DB --json, eval k<corpus, confidence bounds, Scorecard extra='forbid'.
---

## Remediation status — all 24 confirmed findings fixed

| Findings | Commit | Theme |
|----------|--------|-------|
| R6-1, R6-2, R6-12, R6-19 | `ba0538b` | Complete the MR-6 ordinal fix: edge-src uid, exact dst_uid persistence (migration 5), self-method target, method-vs-function kind; collect conditionally-defined defs |
| R6-18, R6-20, R6-15, R6-21 | `bce0570` | confidence [0,1] bound; Scorecard extra=forbid; empty-DB --json valid; single id_for/edge |
| R6-8 | `b4779a5` | subgraph_edges TEMP-table integer-PK join (27-55s -> ms) |
| R6-9, R6-13, R6-14, R6-22 | `e1d4277` | dotted-LOCATE grounding; stopword deny-list; eval corpus > k; HashingEmbedder sub-tokens + drop ~0-cosine seeds |
| R6-10, R6-11 | `3e36a7d` | busy_timeout, WAL only for files, close-on-failure; single-writer waits not crashes |
| R6-3, R6-4, R6-5, R6-6, R6-7, R6-16, R6-17, R6-23 | (this batch) | Multilang CodeAdapter: Go type_spec + method receivers + imports; JS/TS arrows + class_heritage inheritance + TS interface methods; Rust impl-as-scope + INHERITS, no dup |

Regression coverage: `tests/test_review_mega.py` (one+ test per finding; multilang gated on `[code]`). Suite green (113).
The completeness-critic gaps (java/c/cpp, multi-writer beyond two, large-repo benchmark, real semantic embedder) remain future scope.
