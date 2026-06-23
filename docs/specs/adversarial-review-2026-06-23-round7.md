# Round-7 review (light) — 2026-06-23

3 lenses → 1 skeptic. Candidates 8, confirmed 8, refuted 0.

> Solid overall: after six prior rounds plus heavy remediation, most round-6 fixes held and the verdict is "minor + new-area issues only" — with two genuine regressions worth calling out plainly. I confirmed all 8 survivors against the live code and the real tree_sitter_language_pack grammars (v1.10.1); none were refuted. Re-ranking summary: I promoted R7-1 (stopword filter) to High — the survivor rated it Medium, but get/set/call/use are among the most common method names in real code and the recall failure is silent with no rescue path, so it sits alongside R7-2 (Rust generic-impl) as the two High-impact items. Two clear regressions from round-6: R7-1 (commit e1d4277, where the R6-9 bare-tail and R6-13 stopword fixes mutually defeat each other) and R7-7/strand (commit bce0570, precise dst_uid now exclusive). The remaining new-area issues are multilang extraction gaps in the 6a1c62e rewrite: R7-2 (Rust generic impl loses scoping + INHERITS, High), and TS/JS gaps for abstract classes (R7-4), namespaces (R7-5), enums (R7-6), and class-field arrow methods (R7-8). R7-3 is a deterministic but metadata-only provenance corruption (no live consumer reads edges.source, so ranking/eval are unaffected). No crashes, data loss, or security issues found. Three findings touch the same TS LangSpec (planner.py:28-33 for R7-1; adapters/code/__init__.py:43-45 for R7-4/R7-6) and could be fixed together cheaply.

## Confirmed findings — ALL FIXED

| ID | Sev | Title | Fixed |
|----|-----|-------|-------|
| R7-1 | High | Stopword deny-list makes LOCATE unable to ground any symbol named get/set/use/call/flow/wo | ✅ |
| R7-2 | High | Rust generic impl (impl<T> Trait for Type<T>) loses method scoping and the INHERITS edge | ✅ |
| R7-3 | Medium | R6-2 re-resolve path overwrites every precise cross-file edge's source provenance with har | ✅ |
| R7-4 | Medium | TypeScript abstract classes and their methods are not extracted | ✅ |
| R7-5 | Medium | TypeScript namespace/module members lose their scope: wrong qualnames and cross-namespace  | ✅ |
| R7-6 | Low | TypeScript enum declarations are not extracted | ✅ |
| R7-7 | Low | R6-2 precise dst_uid binding strands a runtime-valid cross-file edge when the callee moves | ✅ |
| R7-8 | Low | JS/TS class-field arrow methods (handleClick = (e) => {...}) are silently dropped | ✅ |

All 8 fixed in one commit (round-7 batch). R7-1/R7-2 were genuine regressions from round-6 commits e1d4277/6a1c62e; R7-3/R7-7 from ba0538b's R6-2 path; R7-4/5/6/8 TS coverage gaps from 6a1c62e. Regression tests in tests/test_review_mega.py. Suite green (117).