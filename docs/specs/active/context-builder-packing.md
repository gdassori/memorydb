---
title: "Context builder & token-budgeted packing"
status: planned
created: 2026-06-22
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

@dataclass
class ContextResult:
    text: str
    cards: list[dict]            # structured form (for non-markdown consumers)
    used_tokens: int
    dropped: int                 # nodes that did not fit (reported, not hidden)

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

- **Budget < one card:** emit the single highest-ranked card, truncated to budget, `dropped = n-1`.
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

- [ ] `TokenCounter` port + `HeuristicCounter`
- [ ] node ranking (score+depth+confidence) with deterministic tie-break
- [ ] greedy packer with reserved relationships budget + `dropped` accounting
- [ ] markdown + structured-dict renderers with `file:line` provenance
- [ ] LOCATE "used at" rendering
- [ ] zero-dep tests (budget / ordering / provenance / determinism / dropped)

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

## References

- [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md), [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)
- [hybrid-ranker.md](hybrid-ranker.md), [public-api-facade.md](public-api-facade.md)
