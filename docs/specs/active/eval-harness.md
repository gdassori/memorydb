---
title: "Retrieval-quality evaluation harness"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-007, TD-005]
components: [eval]
---

# Eval harness

> Measure whether MemoryDB actually retrieves the right things: `LOCATE` precision/recall against ground
> truth, and `EXPLAIN` relevance against a labeled set. Without this, ranking/weight changes are guesswork
> ([TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)).

## Goal

`memorydb-eval run <suite>` indexes a fixture repo, runs labeled queries, and reports metrics
(LOCATE precision/recall/F1, EXPLAIN recall@k / MRR / nDCG). Done = a single command yields a scorecard, and
ranking changes ([hybrid-ranker.md](hybrid-ranker.md)) can be compared run-over-run.

## Background & constraints

`LOCATE` has objective ground truth (the call graph is deterministic — we *know* who calls X), so precision/recall
are exact. `EXPLAIN` is fuzzier → use a labeled relevance set (which nodes *should* appear for a question). Must
run zero-dep with the `HashingEmbedder` for CI, and optionally with a real embedder for realistic numbers.

## Data model & interfaces

```python
@dataclass
class EvalCase:
    query: str
    intent: str                 # LOCATE | EXPLAIN | FILTER
    expected_uids: list[str]    # ground-truth node uids (relevant set)

@dataclass
class Scorecard:
    locate: dict     # {precision, recall, f1}
    explain: dict    # {recall_at_k, mrr, ndcg}
    per_case: list[dict]

class Evaluator:
    def __init__(self, db) -> None: ...
    def run(self, cases: list[EvalCase], k: int = 10) -> Scorecard: ...
```

Suites live as YAML/JSON under `eval/suites/<name>/` with a `repo/` fixture + `cases.jsonl`.

## Metrics

- **LOCATE:** precision = |returned ∩ expected| / |returned|; recall = |∩| / |expected|; F1.
- **EXPLAIN:** recall@k, MRR (rank of first relevant), nDCG@k (graded by depth/centrality if labeled).
- **Aggregate** across cases (macro-average) + per-case rows for drill-down.

## Algorithm / step-by-step

1. Build the fixture DB: `MemoryDB.open(:memory:)`, index `suite/repo`.
2. For each `EvalCase`: run `db.ask` (or `locate`/`explain`), collect returned uids in rank order.
3. Compute the per-intent metrics vs `expected_uids`.
4. Aggregate → `Scorecard`; write JSON + a human table; optionally diff against a baseline scorecard.

**Worked example:** a 20-file fixture with 30 labeled queries → `LOCATE F1 0.94`, `EXPLAIN recall@10 0.81`,
`MRR 0.73`. Bumping `RankWeights.centrality` 0.25→0.35 → recall@10 0.83 (kept), MRR 0.71 (regressed) → revert.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/eval/__init__.py` | **New** — `Evaluator`, `EvalCase`, `Scorecard`, metrics |
| `src/memorydb/eval/cli.py` | **New** — `memorydb-eval` entry point (run/compare) |
| `eval/suites/sample/` | **New** — a small fixture repo + `cases.jsonl` |
| `pyproject.toml` | **Modify** — `[project.scripts] memorydb-eval = "memorydb.eval.cli:main"` |

## Edge cases & failure modes

- **Empty returned set:** precision defined as 0 (not div-by-zero); recall 0.
- **Expected uid not in index** (label drift after a fixture change): flagged as a broken case, excluded from aggregates with a warning.
- **Nondeterministic real embedder:** report mean ± std over N runs; CI uses the deterministic `HashingEmbedder`.
- **Coarse-edge false positives** inflating LOCATE returns: the harness reports precision separately at confidence thresholds (≥0.9 vs all) — ties back to [TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md).

## Test plan

Zero-dep:

- `test_metrics_math` — hand-built returned/expected sets → known precision/recall/F1/MRR/nDCG.
- `test_end_to_end_sample` — run the sample suite with `HashingEmbedder` → scorecard above a floor (regression guard).
- `test_baseline_compare` — compare two scorecards → correct deltas.

## Performance & scale

Linear in (cases × retrieval cost). The sample suite runs in CI in <1s with the hashing embedder. Larger suites
are opt-in and may use a real embedder.

## Tasks

- [ ] metrics (precision/recall/F1, recall@k, MRR, nDCG) as pure functions
- [ ] `Evaluator.run` over a suite; JSON + table output
- [ ] sample suite fixture (repo + labeled `cases.jsonl`)
- [ ] baseline compare (`memorydb-eval compare a.json b.json`)
- [ ] confidence-thresholded LOCATE precision (TD-005 tie-in)
- [ ] zero-dep tests (metric math / e2e sample / compare)

## Open questions

- **Where do EXPLAIN labels come from** (hand-labeled vs LLM-assisted)? **Lean** hand-label the small sample; LLM-assist
  for larger suites with human review.
- **Track scores over time** (commit a scorecard history)? **Lean** yes — a `eval/history/` JSON per tagged run.

## Risks

- **Overfitting weights to the sample suite** → keep suites diverse; report per-case, not just aggregates.
- **Stale labels** silently inflating/deflating scores → the broken-case detector + a label lint step.

## Review remediation (2026-06-22)

**Build-order:** this harness is the *only* validator for the TD-005 confidence tiers and the hybrid-ranker weights,
which several specs lean on. Pull it **earlier** — right after the indexer and a basic retrieval path — so those
values aren't "asserted but unvalidated" for long. Report LOCATE precision **split by confidence threshold** (≥0.9 vs
all) to expose coarse-edge false positives ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)).

## References

- [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md), [TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)
- [hybrid-ranker.md](hybrid-ranker.md), [public-api-facade.md](public-api-facade.md)
