# Mega adversarial review — 2026-06-29 (PR #9, `feat/hybrid-ranker`)

4 finder lenses (scoring-math/SQL · edge-cases/robustness · integration/consistency · test-quality) over the new
`ranker.py` + the `planner.py`/`context.py` integration. Two lenses mutation-tested the suite in an isolated
worktree; the headline test-integrity finding was reproduced directly. (The scoring-math lens was interrupted, but
the robustness lens covered the same math — half-life, mtime, confidence-mean, weights.)

> **Verdict:** the fusion mechanics are sound — `breakdown` sums to `score`, the min-max neutral-0.5 guard, the
> dedup, mixed-dim cosine, the one-embed shared qvec, the additive `ranking`/`scored` keys and the context
> builder's backward-compatible preference are all correct and verified. But the review surfaced **three real
> test-integrity defects** (a headline test that is *false* on the production PageRank path, a tautological
> tie-break test, and an integration test that can't tell the ranker from the old proxy) and a coherent cluster
> of **integration gaps** where the ranker reaches the context builder but **not** the facade-sharing, the eval
> harness, or reproducible scoring — i.e. the spec's "tunable, eval-comparable" promise is only half-wired. Plus
> a TD-010 consistency miss (dataclasses where the codebase uses pydantic) and two robustness nicks.

## Confirmed findings

### P9-1 — High/test — `test_hub_outranks_leaf_on_centrality` is FALSE on the real PageRank path (only passes under the forced degree fallback)
**Location:** `tests/test_ranker.py` (`test_hub_outranks_leaf_on_centrality`, fixture builds H→{x,y,z}, L→x).
Reproduced directly: with real PageRank (the production path when `[graph]` is installed), a hub with only
**out-edges** gets **no** rank boost — `H` and `L` both score 0.1493 → min-maxed to a 0.5 **tie**, so `H` only
"wins" by the id tie-break, not centrality. The test passes solely because `force_degree` swaps in degree
centrality (where out-degree counts). The module docstring's "identical with or without `[graph]`" and the spec's
worked example ("`send_notification` wins on centrality") only hold because a *real* hub has **in-edges**
(`send ← Job`). **Fix:** give the test hub an in-edge (callers → hub) so it dominates under PageRank too, and add
a non-`force_degree` `@graph` variant asserting the hub tops on the real path. This also documents the actual
semantics: PageRank elevates *called-by-many*, not *calls-many*.

### P9-2 — High/test — `test_all_equal_scores_stable_by_id` is a tautology
**Location:** `tests/test_ranker.py` (`test_all_equal_scores_stable_by_id`). Node ids are created and passed in
ascending order; Python's stable sort preserves input order regardless of the `node_id` secondary key. Mutation
test: deleting `, s.node_id` from the sort key **survives** the suite. **Fix:** pass the candidates in *descending*
id order and assert the result is `sorted(ids)` — that actually exercises the tie-break.

### P9-3 — High/test — `test_context_builder_prefers_ranking` can't distinguish "honored ranking" from "used proxy"
**Location:** `tests/test_ranker.py` (`test_context_builder_prefers_ranking`). For the fixture the seed/depth proxy
*also* puts `log` first, so asserting `uids[0] == "log"` passes even when the builder ignores `result["ranking"]`
(mutation-confirmed). **Fix:** assert the *full* order and pick a forced ranking whose order differs from the
proxy's, so the assertion fails if the ranking branch is dropped.

### P9-4 — Medium/integration — the facade builds a `graph_view` but never threads it to the planner (two GraphViews per store; injection only half-wired)
**Location:** `src/memorydb/api.py` (the `RetrievalPlanner(...)` construction omits `graph_view=`/`ranker=`) vs the
`graph_view` property; `planner.py:_hybrid_ranker` then lazily builds its **own** `GraphView(store)`. The spec says
"the planner and `MemoryDB` accept an injectable `graph_view`/`ranker`" — the planner ctor params exist but the
facade doesn't use them, so an injected `db.graph_view` is **not** the one the ranker uses. Not a correctness bug
(GraphView is stateless), but it defeats the stated sharing and any future GraphView caching. **Fix:** build one
default `GraphView(store)` in `MemoryDB.__init__` and pass it to the planner; add `ranker=` to `open()`/`__init__`
threaded through, so the injection story is end-to-end.

### P9-5 — Medium/integration — the eval harness ignores `result["ranking"]`, so it never measures the ranker
**Location:** `src/memorydb/eval/__init__.py` (`_explain_ranking` computes its own "seeds first, then by uid" order
and never reads `result.get("ranking")`). The spec's whole premise is that the eval harness compares ranking
run-over-run / grid-searches weights — but the EXPLAIN metrics don't reflect the hybrid ranker at all. **Fix:**
`_explain_ranking` prefers `result["ranking"]` (mapped id→uid) when present, else the existing proxy — mirroring
`context.py`. (Factor the prefer-ranking-else-proxy into one shared helper so context and eval can't drift.)

### P9-6 — Medium/integration — production EXPLAIN ranking is non-reproducible across wall-clock time
**Location:** `planner.explain` calls `rank(ids, qvec, depth=depth)` with no `now=`, so `_recencies` uses
`time.time()`. Recency (weight 0.15) — and at near-ties, the **order** — drifts as wall-clock advances; two eval
runs days apart can differ for reasons unrelated to a code/weight change, undercutting P9-5 and the codebase's
determinism norm (RR3-1). The `now=` param was added *for* reproducibility, but the production path doesn't use it.
**Fix:** default `now` to the **corpus's newest mtime** (one cheap `SELECT MAX(json_extract(attrs,'$.mtime'))`),
falling back to wall-clock when there are no mtimes — deterministic, corpus-relative recency (newest file = 1.0).

### P9-7 — Medium/consistency — `RankWeights`/`Scored` are plain `@dataclass`, violating TD-010 (pydantic domain models)
**Location:** `ranker.py` (`@dataclass RankWeights`, `@dataclass Scored`). TD-010 (accepted) makes domain models
pydantic `BaseModel`s with `Field(ge=…, le=…)` range validation — and explicitly names "score/confidence" models
as the case. `RankWeights` hand-rolls range/sum validation in `__post_init__`; `Scored` holds a `score` and is a
public export consumers will expect to `model_dump()` (the planner already pre-converts `scored` to dicts — a
tell). **Fix:** make both `BaseModel`s (`Field(ge=0)` weights, a `model_validator` that normalizes+warns and
rejects a non-positive sum); the planner emits `s.model_dump()`.

### P9-8 — Medium/robustness — a malformed `attrs.mtime` (non-numeric) crashes `rank()` for the whole candidate set
**Location:** `ranker.py:_mtimes` (`float(r[1])`). A file/symbol node with `attrs={"mtime":"not-a-date"}` raises
`ValueError`, aborting ranking for the entire query (the planner's `try/except` degrades to *unranked*, silently
losing the signal). `attrs` is repo-controlled. **Fix:** parse each mtime defensively (`try/except → None` →
neutral 0.5 for that node).

### P9-9 — Low/robustness — `half_life_days` mutated to 0/negative after construction breaks recency
**Location:** `ranker.py` (`__init__` coerces non-positive to 30.0 once; `_recencies` divides by the mutable
attribute). `r.half_life_days = 0` → `ZeroDivisionError`; negative → recency grows >1 (older ranks higher).
Reachable only by mutating the public attr post-construction (e.g. an eval grid-search). **Fix:** compute the
effective half-life (`> 0 else 30.0`) at point of use.

## Lower / accepted

- **P9-10 (Low):** a non-existent candidate id is scored with neutral recency 0.5 and occupies a slot. Unreachable
  via the planner (`traverse` filters to real nodes); only a direct `rank()` caller hits it. Optionally intersect
  `ids` with existing nodes. *Accept for now (documented edge).* 
- **P9-11 (Low):** a self-loop is counted twice in the `_confidences` mean (src arm + dst arm). Small skew, weight
  0.15. *Optionally exclude `src == dst`.*
- **P9-12 (Low):** negative individual weights (with positive sum) are accepted silently → a node can be penalized
  for being central. *Optionally warn on a negative component.*
- **P9-13 (Med/test-coverage):** add tests for the paths with **no** coverage — confidence with *varying* (non-1.0)
  values + an incoming-only edge, a custom `half_life_days`, a symbol's *denormalized* `attrs.mtime` (the C5
  first-COALESCE arm), the negative-cosine clamp, the planner degrade path, dedupe, and dim-mismatch.
- **P9-14 (Low/doc):** the spec "What changes" table is stale — it lists `_explain` (the impl modified public
  `explain`) and omits `context.py`, `__init__.py`, and the `api.py`/`planner.py` ctor injection; `scored` is an
  undocumented return-contract key. Update the table.

## Confirmed correct (verified, not assumed)
One embed → shared qvec for seeding *and* ranking; the broad `except` degrade matches the repo idiom and is
caught by `test_planner_explain_emits_ranking`; context.py is backward-compatible (LOCATE / ranking-less EXPLAIN
→ the old proxy, all 21 context tests pass) and its unranked-node sink has a uid tie-break; id↔node_id are ints
throughout; min-max guard, breakdown=weighted-sum, dedup, mixed-dim cosine (dim filter), `centrality_scores`
restricted to `ids` (no neighbor leak), zero/empty/wrong-dim query_vec → vector 0 (no crash), node id 0
first-class, 4 batched queries (no N+1), `RankWeights` sum guards. The edge-confidence "double count" worry in the
builder is intended (the ranker folds confidence in; the builder's `_W_CONF` would double it).

## Remediation order
Fix-now: **P9-1/2/3** (test integrity) + **P9-8/9** (robustness) — small, self-contained.
Wire the loop: **P9-4** (facade→planner share) + **P9-5** (eval prefers ranking) + **P9-6** (reproducible `now`).
Consistency/doc: **P9-7** (pydantic) + **P9-13** (coverage) + **P9-14** (spec table). Accept P9-10/11/12 with notes.
