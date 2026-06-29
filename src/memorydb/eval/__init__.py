"""Retrieval-quality evaluation harness (eval-harness spec; TD-007/005).

Measures whether MemoryDB retrieves the right things. LOCATE has objective ground truth (the call
graph is deterministic — we *know* who calls X) so precision/recall are exact; EXPLAIN is fuzzier and
scored against a labeled relevant set (recall@k / MRR / nDCG). Metrics are pure functions so they test
zero-dep; ``Evaluator`` runs labeled cases against a `MemoryDB`, and ``evaluate_suite`` wires the whole
loop (open → index → score) for the CLI.

LOCATE precision is reported twice — over *all* returned references and over only high-confidence
(≥0.9) ones — to expose coarse-edge false positives (TD-005).
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

HIGH_CONF = 0.9  # confidence floor for the "precise-only" LOCATE precision column (TD-005)


# --- metrics (pure functions) ----------------------------------------------
def _dedupe(seq) -> list:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def precision(returned, expected) -> float:
    """|returned ∩ expected| / |returned|. Empty returned → 0.0 (no div-by-zero)."""
    returned = _dedupe(returned)
    if not returned:
        return 0.0
    exp = set(expected)
    return sum(1 for r in returned if r in exp) / len(returned)


def recall(returned, expected) -> float:
    """|returned ∩ expected| / |expected|. Empty expected → 1.0 (nothing required)."""
    if not expected:
        return 1.0
    ret = set(returned)
    return sum(1 for e in set(expected) if e in ret) / len(set(expected))


def f1(p: float, r: float) -> float:
    return 0.0 if (p + r) == 0 else 2 * p * r / (p + r)


def recall_at_k(ranked, expected, k: int) -> float:
    if not expected:
        return 1.0
    top = set(_dedupe(ranked)[:k])
    return sum(1 for e in set(expected) if e in top) / len(set(expected))


def mrr(ranked, expected) -> float:
    """Reciprocal rank of the first relevant hit (1-based); 0 if none in ``ranked``."""
    exp = set(expected)
    for i, uid in enumerate(_dedupe(ranked), start=1):
        if uid in exp:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked, expected, k: int, gains: Optional[dict] = None) -> float:
    """nDCG@k. Binary relevance unless ``gains`` (uid → graded relevance) is provided; ``gains`` only
    *overrides* grades — an ``expected_uid`` absent from ``gains`` is still relevant at grade 1.0, and
    counts in both the DCG and the ideal pool (MR-19). An empty/None ``gains`` is pure binary (I16)."""
    exp = set(expected)

    def rel(uid):
        if gains and uid in gains:
            return float(gains[uid])
        return 1.0 if uid in exp else 0.0

    ranked = _dedupe(ranked)[:k]
    dcg = sum(rel(uid) / math.log2(i + 2) for i, uid in enumerate(ranked))
    ideal_pool = set(exp) | set(gains or {})            # every uid that has any positive relevance
    ideal = sorted((rel(u) for u in ideal_pool), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


# --- data model ------------------------------------------------------------
class EvalCase(BaseModel):
    # forbid extra keys so a malformed cases.jsonl line raises rather than silently dropping fields.
    model_config = ConfigDict(extra="forbid")

    query: str
    intent: str                       # LOCATE | EXPLAIN | FILTER
    expected_uids: list = Field(default_factory=list)
    gains: Optional[dict] = None      # optional graded relevance for nDCG


class Scorecard(BaseModel):
    # forbid extra keys so `memorydb-eval compare` rejects a non-scorecard JSON instead of silently
    # producing empty deltas (R6-20).
    model_config = ConfigDict(extra="forbid")

    locate: dict = Field(default_factory=dict)    # {precision, precision_high, recall, f1, n}
    explain: dict = Field(default_factory=dict)   # {recall_at_k, mrr, ndcg, n}
    per_case: list = Field(default_factory=list)
    broken: list = Field(default_factory=list)    # queries excluded for label drift
    k: int = 10

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict) -> "Scorecard":
        return cls.model_validate(d)


# --- evaluator -------------------------------------------------------------
class Evaluator:
    def __init__(self, db) -> None:
        self.db = db

    def run(self, cases: list, k: int = 10) -> Scorecard:
        loc_rows, exp_rows, per_case, broken = [], [], [], []
        for c in cases:
            intent = (c.intent or "").upper()
            if self._broken(c):                       # label drift: expected uid not in the index
                broken.append(c.query)
                per_case.append({"query": c.query, "intent": intent, "broken": True})
                continue
            if intent == "LOCATE":
                per_case.append(self._score_locate(c, loc_rows))
            elif intent == "EXPLAIN":
                per_case.append(self._score_explain(c, k, exp_rows))
            else:
                per_case.append({"query": c.query, "intent": intent, "skipped": "unsupported intent"})
        return Scorecard(
            locate=self._agg_locate(loc_rows),
            explain=self._agg_explain(exp_rows),
            per_case=per_case, broken=broken, k=k,
        )

    # --- per-intent scoring ------------------------------------------------
    def _score_locate(self, c, sink: list) -> dict:
        target = c.query
        refs = self.db.locate(target)
        returned = [r["src_uid"] for r in refs]
        returned_high = [r["src_uid"] for r in refs if r.get("confidence", 0.0) >= HIGH_CONF]
        p, ph, r = precision(returned, c.expected_uids), precision(returned_high, c.expected_uids), \
            recall(returned, c.expected_uids)
        row = {"query": c.query, "intent": "LOCATE", "precision": p, "precision_high": ph,
               "recall": r, "f1": f1(p, r), "returned": returned, "expected": c.expected_uids}
        sink.append(row)
        return row

    def _score_explain(self, c, k: int, sink: list) -> dict:
        result = self.db.explain(c.query, k=k)
        ranked = self._explain_ranking(result)
        rk = recall_at_k(ranked, c.expected_uids, k)
        mr = mrr(ranked, c.expected_uids)
        nd = ndcg_at_k(ranked, c.expected_uids, k, c.gains)
        row = {"query": c.query, "intent": "EXPLAIN", "recall_at_k": rk, "mrr": mr, "ndcg": nd,
               "returned": ranked[:k], "expected": c.expected_uids}
        sink.append(row)
        return row

    @staticmethod
    def _explain_ranking(result: dict) -> list:
        """Rank EXPLAIN nodes for scoring. Prefer the planner's hybrid ``ranking`` (vector+centrality+
        confidence+recency) when present, mapped id→uid — so the eval metrics actually measure the ranker
        (P9-5). Fall back to the seed-first/uid proxy otherwise (LOCATE-less results, or a degraded EXPLAIN
        with no ranking). Both orders are deterministic (the ranker pins ``now`` to the corpus mtime)."""
        by_id = {n["id"]: n["uid"] for n in result.get("nodes", [])}
        ranking = result.get("ranking")
        if ranking:
            ranked = [by_id[i] for i in ranking if i in by_id]
            # append any node missing from the ranking (defensive), uid-ordered for determinism
            rest = sorted(uid for nid, uid in by_id.items() if nid not in set(ranking))
            return _dedupe(ranked + rest)
        seeds = [by_id[i] for i in result.get("seeds", []) if i in by_id]
        seen = set(seeds)
        # Order the non-seed remainder by uid (churn-invariant), not by node id which the indexer's
        # delete+reinsert renumbers — keeps the ranking deterministic across re-index (R3L-4).
        rest = sorted(uid for uid in by_id.values() if uid not in seen)
        return _dedupe(seeds + rest)

    def _broken(self, c) -> bool:
        # A case is broken if any labeled expected uid is absent from the index (fixture/label drift).
        return any(self.db.store.id_for(uid) is None for uid in c.expected_uids)

    # --- aggregation (macro-average over cases) ----------------------------
    @staticmethod
    def _agg_locate(rows: list) -> dict:
        if not rows:
            return {"precision": 0.0, "precision_high": 0.0, "recall": 0.0, "f1": 0.0, "n": 0}
        n = len(rows)
        return {
            "precision": sum(r["precision"] for r in rows) / n,
            "precision_high": sum(r["precision_high"] for r in rows) / n,
            "recall": sum(r["recall"] for r in rows) / n,
            "f1": sum(r["f1"] for r in rows) / n,
            "n": n,
        }

    @staticmethod
    def _agg_explain(rows: list) -> dict:
        if not rows:
            return {"recall_at_k": 0.0, "mrr": 0.0, "ndcg": 0.0, "n": 0}
        n = len(rows)
        return {
            "recall_at_k": sum(r["recall_at_k"] for r in rows) / n,
            "mrr": sum(r["mrr"] for r in rows) / n,
            "ndcg": sum(r["ndcg"] for r in rows) / n,
            "n": n,
        }


# --- suite loading & end-to-end -------------------------------------------
def load_suite(path: str):
    """Load ``<suite>/cases.jsonl`` → ``(repo_dir, [EvalCase])``. Each JSONL line is one case."""
    repo = os.path.join(path, "repo")
    cases = []
    with open(os.path.join(path, "cases.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(EvalCase(**json.loads(line)))
    return repo, cases


def evaluate_suite(path: str, *, embedder=None, extractors=None, k: int = 10) -> Scorecard:
    """Open an in-memory MemoryDB, index ``<suite>/repo``, and score ``<suite>/cases.jsonl``."""
    from ..api import MemoryDB
    repo, cases = load_suite(path)
    db = MemoryDB.open(":memory:", embedder=embedder, extractors=extractors)
    try:
        db.index(repo)
        return Evaluator(db).run(cases, k=k)
    finally:
        db.close()


# --- baseline comparison ---------------------------------------------------
def compare(baseline: Scorecard, new: Scorecard) -> dict:
    """Per-metric deltas (new − baseline) for LOCATE and EXPLAIN aggregates."""
    def deltas(a: dict, b: dict) -> dict:
        keys = set(a) | set(b)
        return {key: (b.get(key, 0.0) - a.get(key, 0.0)) for key in sorted(keys) if key != "n"}
    return {"locate": deltas(baseline.locate, new.locate),
            "explain": deltas(baseline.explain, new.explain)}
