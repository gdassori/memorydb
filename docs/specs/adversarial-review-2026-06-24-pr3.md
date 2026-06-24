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

## Second round — re-review of the fixes (2026-06-24)

A follow-up mega review (9 lenses targeting *the fixes themselves* → 3 skeptics/finding, default-refuted +
reachability-gated → 11 raised / 10 survived) caught regressions the PR3 fixes introduced — the codebase's
recurring "each fix adds a regression" pattern. All fixed + regression-tested (`test_rr_*`); suite **143 green**;
fuzz (budgets −5..399 × 9 markdown vectors, EXPLAIN+LOCATE) → **0 invariant violations, 0 injection escapes**.

| ID | Sev | Summary | Status |
|----|-----|---------|--------|
| C2 | **High** | `_loc()`/`file_uid` reached the EXPLAIN card header **unsanitized** — a newline in a filename forges a header/fence/phantom-Relationships (PR3-3 missed the `_loc` path) | ✅ `_safe(_loc(node))` in the header |
| C7/C9 | Low (regression) | `_safe("")` returned a lone `\` (`"" in "#>*-+\|="` is `True`) → every card with no docstring/signature (the common case) rendered a spurious `\` / `` `\` `` line | ✅ non-empty guard `if t and t[:1] in _LEADING` |
| C3 | Medium | LOCATE `src_file` (src_uid prefix) interpolated unsanitized → newline forges a fake reference row (PR3-7 covered name/relation only) | ✅ `_safe(raw_file)` |
| C4 | Medium | `_safe` didn't neutralize `~~~`/`___` fences/rules (only backticks stripped) | ✅ added `~ _` to `_LEADING` |
| C6 | Nit | `cards[].calls` (clip-then-set) deduped differently from the rendered `→ calls:` (set-then-clip) | ✅ both clip-then-set-then-sort |
| C8 | Nit | tiny-budget LOCATE header byte-cut left an unbalanced `**authen` fragment | ✅ emit the plain symbol, no `**` markup |
| C10 | Nit (spec) | PR3-1's `card_budget<=0` drop-all contradicted spec line 89 ("emit one truncated card") | ✅ spec amended — invariant beats "always emit one card" |
| C1 | Low/nuance | per-field `…` caps clip without setting `truncated` | ✅ documented two-tier loss (field `…` vs budget `dropped`/`truncated`); not conflated |

Refuted: `node['type']` unsanitized (type is ours — function/class/…, not source-derived; 2/3 called it unreachable).

## Third round — re-review of the round-2 fixes (2026-06-24)

A third mega review (9 lenses over the C-round + run-aware `_safe` fixes → 3 reachability-gated skeptics) →
**8 raised / 8 survived / 0 refuted**, 3 root causes (1 Medium, 2 Low). Verdict: *not converged* — and again one
finding was a **regression of the previous round's own fix** (RR2-1). All fixed + regression-tested (`test_rr2_*`);
suite **148 green**; fuzz (budgets −3..249 + 1000 + 5000 × 16 markdown vectors incl. `=`/`==`/`_ _ _`/`[ref]:`/`<`,
EXPLAIN+LOCATE) → **0 invariant violations, 0 injection escapes**.

| ID | Sev | Summary | Status |
|----|-----|---------|--------|
| RR2-1 | Medium (regression of 8ce22ea) | the run-aware rewrite dropped `=` from the single-char set, re-opening a **setext-H1** forge (a `=`/`==` docstring under the signature line renders `<h1>`); `===` stayed covered but 1–2-char runs were exposed | ✅ `=` back in `_LEAD1` (never a valid identifier start → no snake_case over-reach) |
| RR2-2 | Low | the run check was contiguous-prefix-only → spaced rules `_ _ _`, link-ref `[ref]:`, leading `<` HTML passed through | ✅ `_safe` now also escapes spaced rules (line of only `-_*=+`/space, ≥3 markers), `[label]:` link-refs, and leading `<`. Ordered lists (`1.`) **deliberately not escaped** (benign + common in real docstrings; LLM-only sink) |
| RR2-3 | Low | the C8 unbalanced-markup fix was applied to LOCATE only; the EXPLAIN single-card byte-cut sliced through a `` `signature` `` wrapper leaving a dangling backtick | ✅ drop the dangling backtick after the cut (count made even; `used ≤ budget` preserved) |

`_safe` verified **idempotent** (`sym` is `_safe`-d then re-handled in the LOCATE fallback). Completeness-critic
notes carried forward: the threat model is **LLM-only** (spec lines 14–16) — no strict-CommonMark renderer is in the
loop, which is why the `<hr>`/`<ol>`/link-ref vectors are Low.

## Fourth round — broad convergence check (2026-06-24)

A fourth review deliberately **broadened beyond markdown-injection** (which was by now heavily fuzzed) to
budget arithmetic, determinism, robustness on degenerate planner results, the planner/facade/CLI integration
contract, the adapter caps, and whether the round-2 fixes regressed → **2 raised / 1 confirmed / 1 refuted**.
The broad surface yielded **no new reachable defect** — only one Low determinism nit. Suite **149 green**.

| ID | Sev | Summary | Status |
|----|-----|---------|--------|
| RR3-1 | Low (determinism) | the `_relationships` edge sort key `(-conf, src, dst)` omitted `relation`, so equal-confidence edges between the same pair (`class A(B): x = B()` → `A INHERITS B` + `A CALLS B`, both conf 1.0) rendered in SQLite-plan-dependent order — same class as MR-17 (node path) but on the edge path | ✅ appended `relation` to the key; also added `ORDER BY` tiebreaks to `query.references_to` (LOCATE) and `query.subgraph_edges` so the source is plan-independent too |

**Refuted (independently re-verified by me, not just the panel):** "the `**Relationships**` header is unaccounted
in `used_tokens`, so the rendered text exceeds budget at small budgets." Empirically **false** — a sweep of
budgets 1..299 over a relationships-rendering subgraph found **0 cases** of `count(text) > budget_tokens`: the
`_SAFETY = 0.9` margin absorbs the ~5-token header. No change made (the meta-pattern warns against gratuitous fixes).

**Not raised (documented as unreachable):** `ContextBuilder.build()` raises `KeyError`/`TypeError` on hand-built
results missing `id`/`uid`/`relation` or with `confidence=None`, but the real `api.py`/`cli.py` path is safe —
`get_nodes()` guarantees `int id` + `uid` and `subgraph_edges()`/`references_to()` draw from `NOT NULL` columns.
Worth guarding only if `build()` is ever promoted to a public entry point for arbitrary dicts.

### Convergence

Severity trajectory across rounds: **High (C2) → Medium (RR2-1) → Low (RR3-1)**, with the finding count falling
8 → 1 and the broad round-4 audit surfacing nothing outside the (now-fuzzed) sanitization surface. The cascade has
**converged**: remaining theoretical items are LLM-only-sink markdown nits (documented trade-offs) and
unreachable defensive-coding gaps. PR #3 is clean.
