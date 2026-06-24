# Adversarial review — PR #3 (context-builder-packing)

**Date:** 2026-06-24 · **Scope:** the new `src/memorydb/context.py` (~194 lines) + its facade wiring
(`api.py` `context()` / `ask(as_context=True)`) + spec. **Method:** multi-agent mega review — 8 finder
lenses over the new code → dedup → 3 perspective-diverse skeptics per finding (refute, default-refuted)
→ synthesis + completeness critic.

**Result:** 25 candidates raised → **14 confirmed / 11 refuted**, deduped to **7 root causes**. **No Highs
survive.** Verdict: *solid, single well-structured module; minor issues only*. The packing/reserve math is
sound for all realistic budgets (a 3000-trial fuzz found no violation outside budget 0, since fixed). All 7
are remediated on `feat/context-builder-packing` with `test_pr3_*` regressions; full suite 136 green.

## Confirmed findings & status

| ID | Sev | Category | Summary | Status |
|----|-----|----------|---------|--------|
| PR3-1 | Medium | invariant | `used_tokens > budget_tokens` on small/zero/negative budgets — worst & unclamped on the LOCATE header path | ✅ fixed (`build` clamps both routes; LOCATE header inside the ceiling; EXPLAIN first-card guarded on `card_budget>0`) — fuzzed 0 violations |
| PR3-2 | Medium | spec | single oversized card byte-cut in but reported `truncated=False`/`dropped=0` (silent truncation) | ✅ fixed (`card_truncated` OR-ed into `truncated`) |
| PR3-3 | Medium | security | source-derived signature/docstring/name interpolated into markdown with zero escaping → header/provenance/edge spoofing in LLM context | ✅ fixed (`_safe()`: newline-collapse + backtick-strip + leading-marker escape + cap) |
| PR3-4 | Low | spec | `cards` was uid-only, not the spec's structured form | ✅ fixed (`_card_dict` populates name/type/file/line/signature/docstring/calls/called_by) |
| PR3-5 | Low | perf | Relationships block sorted the whole edge list even when the reserve holds 0 lines | ✅ fixed (early-return before sort; filter to included-included first) |
| PR3-6 | Low | security | signature/docstring uncapped at extraction (body was `[:2000]`) — asymmetric attacker-controlled size | ✅ fixed (`[:512]` at extraction in both adapters + render-time cap) |
| PR3-7 | Low | security | LOCATE reference line interpolated `src_name`/`relation` unescaped | ✅ fixed (folded into `_safe()`) |

## Refuted / non-issues (representative of the 11)

- **ZeroDivisionError on empty seeds** (completeness critic): false alarm — `{sid: 1 - i/len(seeds) ...}` never
  evaluates `len(seeds)` when `seeds == []` (empty comprehension). Verified empirically.
- Various restatements of the budget-invariant facet (F4/F5/F7/F10/F15/F20) merged into PR3-1; the truncation
  restatements (F9/F14/F23) merged into PR3-2 — converged severities, no independent defects.

## Verification

- `tests/test_context.py`: `test_pr3_1_invariant_degenerate_budgets`, `test_pr3_2_single_oversized_card_flags_truncated`,
  `test_pr3_3_markdown_injection_neutralized`, `test_pr3_4_cards_are_structured`, `test_pr3_7_locate_reference_line_sanitized`.
- Invariant fuzz: budgets −10..599 × symbol lengths {1, 8, 80, 400}, EXPLAIN + LOCATE → **0 violations**.
- Full suite: **136 passed**.
