---
title: "Context builder & token-budgeted packing"
status: completed
created: 2026-06-22
completed: 2026-06-23
author: claude
related_tds: [TD-007, TD-006]
components: [planner, query]
---

# Context builder & packing

> Turn a retrieval result (seeds, nodes, edges) into **LLM-ready context within a token budget** — packing
> *relationships*, not a bag of chunks ([TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)).
> This is the payoff over classic RAG: the model sees structure with provenance.

## Goal

`ContextBuilder().build(result, budget_tokens)` returns a deterministic, budget-respecting context string
(or dict) with one compact card per node, a relationship summary, and `file:line` provenance. Done = output
never exceeds budget, ordering is by relevance, and dropped content is reported (never silently truncated).

## Background & constraints

The planner's `_explain` returns `{seeds, nodes, edges}`; LOCATE returns `references`. The builder must be
deterministic (testable) and tokenizer-agnostic (a `TokenCounter` port; default heuristic). Graph-aware
embeddings ([TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)) mean nodes already carry
useful signature/docstring in `attrs`.

## Data model & interfaces

```python
from typing import Protocol

class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...

class HeuristicCounter:          # default, zero-dep: ~ len(text)/4
    def count(self, text) -> int: ...

class ContextResult(BaseModel):  # pydantic (TD-004)
    text: str
    cards: list[dict]            # structured form (name/type/file/line/signature/calls…) for non-markdown consumers
    uids: list[str]
    used_tokens: int             # never exceeds budget_tokens (which is clamped >= 0)
    budget_tokens: int
    dropped: int                 # nodes/refs that did not fit (reported, not hidden)
    truncated: bool              # some were dropped OR a single oversized card was byte-cut in

class ContextBuilder:
    def __init__(self, counter: TokenCounter | None = None, max_cards: int = 100) -> None: ...
    def build(self, result: dict, budget_tokens: int, fmt: str = "markdown") -> ContextResult: ...
```

## Card format (markdown)

```
### send_notification  ·  function  ·  services/notifications.py:10
`(user_id, message, channel) -> NotificationLog`
Send a single notification to a user.
→ calls: RedisQueue.push, PushProvider.send   ← called by: MassNotificationJob
```
Followed by a **Relationships** section rendering the subgraph as edges
(`MassNotificationJob --CALLS--> send_notification --WRITES--> NotificationLog`).

## Algorithm / step-by-step

1. **Score** each node: `rank = w_score*vector_score + w_depth*(1/(1+depth)) + w_conf*edge_confidence`
   (depth from the seeds; defaults `w_score=0.5, w_depth=0.3, w_conf=0.2`). Seeds rank highest.
2. **Sort** nodes by `rank` desc (stable; tie-break by uid for determinism). Cap at `max_cards`.
3. **Pack** greedily: reserve ~15% of budget for the Relationships summary; add cards while
   `used + count(card) <= budget - reserve`; stop and count the rest as `dropped`.
4. **Relationships:** render edges among the *included* nodes (from `subgraph_edges`), highest-confidence first,
   until the reserve is spent.
5. Return `ContextResult` (markdown `text` + structured `cards` + `used_tokens` + `dropped`).

**Worked example:** notification subgraph, budget 400 tokens → 5 cards + a 4-edge relationships block, `dropped=0`,
`used_tokens≈360`.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/context.py` | **New** — `ContextBuilder`, `HeuristicCounter`, `ContextResult` |
| `src/memorydb/planner.py` | **Modify (optional)** — `retrieve(..., as_context=True)` convenience returning packed context |

## Edge cases & failure modes

- **Budget < one card (but > 0):** emit the single highest-ranked card, byte-cut to fit the budget,
  `dropped = n-1`, `truncated = True`.
- **Budget too small to hold *anything*** (`card_budget <= 0`, e.g. `budget_tokens` 0/1): drop all nodes,
  `dropped = n`, `truncated = True`, empty `text`. The `used_tokens <= budget_tokens` invariant takes
  precedence over the "always emit one card" rule — you cannot emit a non-empty card within a 0-token
  budget (re-review C10).
- **Oversized subgraph:** `max_cards` hard cap; `dropped` reports the remainder (explicit, per the no-silent-truncation rule).
- **Missing body/docstring:** render header + signature only.
- **LOCATE result** (references, not nodes): a dedicated compact "used at" list rendering.
- **Empty result:** return empty text with `used_tokens=0`.

## Test plan

Zero-dep (`HeuristicCounter`):

- `test_respects_budget` — `used_tokens <= budget` for various budgets.
- `test_ordering` — seeds and high-score nodes appear before low-score ones.
- `test_provenance_present` — every card has `file:line`.
- `test_deterministic` — same input → byte-identical output.
- `test_reports_dropped` — tiny budget → `dropped > 0` and it is reported, not silently cut.

## Performance & scale

O(n log n) sort + O(n) packing over the (capped) retrieved set — tiny. Token counting is the only repeated
op; the heuristic is O(len). Real tokenizers are pluggable when exactness matters.

## Tasks

- [x] `TokenCounter` port + `HeuristicCounter`
- [x] node ranking (score+depth+confidence) with deterministic tie-break
- [x] greedy packer with reserved relationships budget + `dropped` accounting
- [x] markdown + structured-dict renderers with `file:line` provenance
- [x] LOCATE "used at" rendering
- [x] zero-dep tests (budget / ordering / provenance / determinism / dropped)

## Implementation notes (2026-06-23)

- `src/memorydb/context.py` — `ContextBuilder`, `HeuristicCounter` (≈chars/4), `TokenCounter` port,
  `ContextResult` (pydantic: text, cards, uids, used_tokens, budget_tokens, dropped, truncated, intent).
- **Ranking** uses the documented `0.5·vector + 0.3·1/(1+depth) + 0.2·edge_confidence`; the vector term
  is the seed-rank proxy (`1 - i/len(seeds)`) since the result carries ranked seeds, and `depth` comes
  from a new `result["depths"]` map (planner.explain now emits it). Tie-break by uid (churn-invariant).
- **Packing** is greedy under a `budget × 0.9` safety margin (the heuristic under-counts code), with a
  15% reserve for the **Relationships** block (highest-confidence edges among the *included* nodes).
  `dropped`/`truncated` make any overflow explicit (no silent truncation).
- **Facade integration:** replaced the placeholder `_pack_*` in `api.py` — `MemoryDB.context()` and
  `ask(as_context=True)` now delegate to a `ContextBuilder`; `ContextResult` is re-exported.
- **Deferred (open questions):** source-body snippets for top-K seed cards (kept to the card format for
  v1 determinism); adaptive vs fixed reserve ratio (fixed 15%, tunable via the eval harness).

## Open questions

- **Reserve ratio** for the relationships block (fixed 15% vs adaptive)? **Lean** fixed for v1, tune via the eval harness.
- **Include source snippets** (the actual code body) for top cards if budget allows? **Lean** yes for the top-K seeds only.

## Risks

- **Heuristic token count drift** vs the real model → allow injecting the model's tokenizer; keep a safety margin.
- **Over-packing relationships** crowding out cards → the reserve cap bounds it.

## Review remediation (2026-06-22)

The `chars/4` heuristic **under-counts** punctuation-dense code, risking budget overruns. Apply a **safety margin**
(pack to `budget × 0.9`), expose the model's real tokenizer via the `TokenCounter` port, and always report
`used_tokens` so an overrun is visible rather than silent. Provenance (`file:line`) is derived from the uid prefix +
`attrs.start_line`, which the code adapter guarantees.

## Review remediation (2026-06-24 — PR #3 mega review)

An adversarial multi-agent review (25 candidates → 14 confirmed / 11 refuted, **no Highs**; full report in
[adversarial-review-2026-06-24-pr3.md](../adversarial-review-2026-06-24-pr3.md)) found seven small, well-scoped
defects in the new module — all now fixed + regression-tested (`test_pr3_*` in `tests/test_context.py`):

- **PR3-1 (Medium, invariant):** `used_tokens` could exceed `budget_tokens` on degenerate budgets. LOCATE counted
  its header unconditionally (overflow below header cost, unbounded with symbol length) and — unlike EXPLAIN —
  skipped the `max(0, budget)` clamp. **Fix:** clamp *both* routes in `build()`; account the LOCATE header inside
  the `budget×0.9` ceiling (truncate the header if it alone overflows); guard the EXPLAIN first-card branch on
  `card_budget > 0`. Fuzzed −10..599 × 4 symbol lengths → 0 violations.
- **PR3-2 (Medium, spec):** a single oversized card byte-cut *in* reported `truncated=False` (aliased to
  `dropped>0`, which is 0 for n=1) — silent truncation. **Fix:** a `card_truncated` flag OR-ed into `truncated`.
- **PR3-3 (Medium, security):** source-derived signature/docstring/name were interpolated into the markdown with
  no escaping — an attacker-controlled indexed repo could spoof headers, fake `file:line` provenance, and inject
  phantom Relationships into LLM-consumed context. **Fix:** `_safe()` — newline-collapse, backtick-strip, escape a
  leading structural marker, length-cap. Applied to EXPLAIN cards and the LOCATE list (PR3-7).
- **PR3-4 (Low):** `cards` was uid-only. **Fix:** populated with the structured fields (`name/type/file/line/
  signature/docstring/calls/called_by`).
- **PR3-5 (Low, perf):** the Relationships block sorted the full edge list even when the reserve held zero lines.
  **Fix:** early-return before the sort; filter to included-included edges first.
- **PR3-6 (Low, security):** signature/docstring were uncapped at extraction (body was `[:2000]`). **Fix:** `[:512]`
  caps at extraction (both the Python resolver and the tree-sitter adapter) plus a render-time field cap.
- **PR3-7 (Low, security):** the LOCATE reference line interpolated `src_name`/`relation` unescaped — folded into
  the PR3-3 `_safe()` sanitization.

The completeness critic's flagged "potential ZeroDivisionError on empty seeds" was a **false alarm** (the `1 - i/len(seeds)`
division lives inside a comprehension that does not iterate when `seeds == []`) — verified.

## References

- [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md), [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)
- [hybrid-ranker.md](hybrid-ranker.md), [public-api-facade.md](public-api-facade.md)
