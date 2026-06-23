# Round-8 review (light) — 2026-06-23

3 lenses → 1 skeptic. Candidates 13, confirmed 13 (deduped to 11), refuted 0.

> Round-8 LIGHT review at c256fbf. All 13 survivors reproduced end-to-end; all true positives. Dedup to 11 (R8-5 merges REGRESSION-5+MULTILANG-1; R8-4 merges REGRESSION-3+END-TO-END-2). Verdict: round-7 did NOT fully hold. Two genuine silent High regressions the all-in-memory 117-test suite misses: (1) in-place mutation of shipped migration 5 crashes any persistent store at user_version=5 on first cross-file edge; (2) by-name fallback in _resolve_pending rebinds a vanished precise edge to a wrong-file same-named symbol at 0.97. Both crash/corrupt on-disk usage. Rest minor: a LOCATE hijack and JS/JSX field-arrow drop (Medium), plus Low coarse-v1 extraction/provenance polish. Repros at /tmp/repro_migration.py, repro_rebind.py, repro_hijack2.py, repro_adapters.py, repro_provenance.py.

## Confirmed findings — ALL FIXED

| ID | Sev | Title |
|----|-----|-------|
| R8-1 | High | Migration 5 mutated in-place; existing v5 stores never get pending_edges.source and crash on fi |
| R8-2 | High | by-name fallback rebinds a vanished precise edge to a wrong-file same-named symbol at 0.97 (MR- |
| R8-3 | Medium | LOCATE verbs (used/calls/references) pollute candidates and hijack grounding onto a same-named  |
| R8-4 | Medium | R7-8 class-field arrow methods work for TypeScript but are silently dropped for JavaScript/JSX |
| R8-5 | Low | Strict greater-than in upsert_edge flips self-method edge provenance to treesitter on the 0.9 t |
| R8-6 | Low | TS ambient function_signature (declare function, or function in declare module/namespace) not e |
| R8-7 | Low | TS declare module with a string-literal name leaks quotes into qualname and uid |
| R8-8 | Low | Rust mod blocks are not scopes: nested-module functions flatten and ordinal-disambiguate unstab |
| R8-9 | Low | Rust impl of a trait for an array, slice, primitive or pointer type yields a spurious self-INHE |
| R8-10 | Low | JS/TS computed property method names captured verbatim, yielding garbage unmatchable names |
| R8-11 | Low | Go interface method sets, embedded-struct INHERITS, and type aliases not modeled |

R8-1/R8-2 were genuine High regressions from round-7 (migration mutated in-place; the R7-7 by-name fallback re-introduced MR-10). R8-11 (Go interface method sets / embedded structs / type aliases) documented as out-of-scope for the coarse v1 adapter. Regression tests in tests/test_review_mega.py. Suite green (122).