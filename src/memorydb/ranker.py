"""Hybrid ranker — fuse vector similarity, graph centrality, edge confidence & recency (TD-006, TD-007).

Pure vector ranking is role-blind; pure graph ranking ignores the query. :class:`HybridRanker` combines the
signals the substrate already has into ONE transparent, weighted score per candidate, so EXPLAIN surfaces the
*structurally important and semantically relevant* nodes — not just the nearest vectors. Every result carries a
per-signal ``breakdown`` (so the score is inspectable and the eval harness can tune the weights), and each
signal degrades gracefully: no embedding → vector 0, no ``[graph]`` extra → centrality via the degree fallback
(:meth:`GraphView.centrality_scores`), no file mtime → recency neutral. Zero-dep (stdlib + the core).
"""
from __future__ import annotations

import json
import logging
import math

from pydantic import BaseModel, Field, model_validator

from .vector import normalize, unpack

_LOG = logging.getLogger(__name__)


class RankWeights(BaseModel):
    """Per-signal weights (pydantic, TD-010). Vector stays the largest by default so centrality can't bury a
    niche-but-correct hit (Risks). Each weight is ``>= 0`` (a negative weight would *penalize* a signal —
    almost always a bug, so rejected); the set is normalized to sum 1 at construction (a misconfigured sum
    warns rather than silently skewing the fusion; a non-positive sum raises)."""
    vector: float = Field(default=0.45, ge=0.0)
    centrality: float = Field(default=0.25, ge=0.0)
    confidence: float = Field(default=0.15, ge=0.0)
    recency: float = Field(default=0.15, ge=0.0)

    @model_validator(mode="after")
    def _normalize(self) -> "RankWeights":
        total = self.vector + self.centrality + self.confidence + self.recency
        if total <= 0:
            raise ValueError("RankWeights must sum to a positive value")
        if abs(total - 1.0) > 1e-9:
            _LOG.warning("RankWeights sum to %.4f, normalizing to 1.0", total)
            self.vector /= total
            self.centrality /= total
            self.confidence /= total
            self.recency /= total
        return self


class Scored(BaseModel):
    """One ranked candidate (pydantic, TD-010). ``breakdown`` holds the *weighted* per-signal contributions,
    so ``score == sum(breakdown.values())`` (within float tolerance) — the explainability contract."""
    node_id: int
    score: float
    breakdown: dict = Field(default_factory=dict)


def _minmax(raw: dict, ids: list) -> dict:
    """Min-max a signal to [0,1] across the candidate set. A zero range (single candidate or all-equal)
    would divide by zero → return the neutral 0.5 for every node instead (spec normalization guard)."""
    vals = [raw.get(i, 0.0) for i in ids]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return {i: 0.5 for i in ids}
    span = hi - lo
    return {i: (raw.get(i, 0.0) - lo) / span for i in ids}


class HybridRanker:
    """Fuse vector + centrality + confidence + recency into one ranking over a bounded candidate set.
    ``graph_view`` is built lazily over ``store`` if not injected; weights and half-life are tunable."""

    def __init__(self, store, graph_view=None, weights: "RankWeights | None" = None,
                 half_life_days: float = 30.0) -> None:
        self.store = store
        self.graph_view = graph_view
        self.weights = weights or RankWeights()
        self.half_life_days = half_life_days if half_life_days > 0 else 30.0

    def rank(self, candidate_ids, query_vec, depth: int = 2, *, now: "float | None" = None) -> list[Scored]:
        """Rank ``candidate_ids`` by the weighted fusion; returns :class:`Scored` sorted best-first with a
        deterministic ``node_id`` tie-break. ``now`` (epoch seconds) pins the recency clock; when omitted it
        defaults to the corpus's newest mtime — so ranking is *reproducible* (deterministic, corpus-relative:
        the newest file scores recency 1.0) rather than drifting with wall-clock. Each signal is computed once
        over the whole set, then normalized."""
        ids = list(dict.fromkeys(int(i) for i in candidate_ids))   # unique, stable order
        if not ids:
            return []
        w = self.weights
        cos = self._cosines(ids, query_vec)
        cen = _minmax(self._centrality(ids, depth), ids)           # min-max within the candidate set
        conf = self._confidences(ids)
        rec = self._recencies(ids, now)
        scored: list[Scored] = []
        for i in ids:
            breakdown = {
                "vector": w.vector * cos.get(i, 0.0),               # no embedding -> 0
                "centrality": w.centrality * cen.get(i, 0.0),
                "confidence": w.confidence * conf.get(i, 0.0),      # no incident edges -> 0
                "recency": w.recency * rec.get(i, 0.5),             # no mtime -> neutral 0.5
            }
            scored.append(Scored(node_id=i, score=sum(breakdown.values()), breakdown=breakdown))
        scored.sort(key=lambda s: (-s.score, s.node_id))           # deterministic tie-break (uid≈id order)
        return scored

    # --- signal extractors -------------------------------------------------
    def _cosines(self, ids: list, query_vec) -> dict:
        """Cosine of each candidate's stored (unit) embedding against the query, clamped to [0,1] (a
        negative/orthogonal cosine, like a missing embedding, contributes 0). Only same-dim vectors are
        scored — a mixed-dim corpus would otherwise zip-truncate to garbage (mirrors BruteForceVectorIndex)."""
        q = normalize(list(query_vec))
        dim = len(q)
        rows = self.store.conn.execute(
            "SELECT node_id, vector FROM embeddings "
            "WHERE dim = :dim AND node_id IN (SELECT value FROM json_each(:ids))",
            {"dim": dim, "ids": json.dumps(ids)},
        ).fetchall()
        out: dict = {}
        for node_id, blob in rows:
            dot = sum(a * b for a, b in zip(q, unpack(blob)))       # both unit-normalized -> cosine
            out[node_id] = max(0.0, min(1.0, dot))
        return out

    def _centrality(self, ids: list, depth: int) -> dict:
        """Raw centrality per candidate from :meth:`GraphView.centrality_scores` (PageRank when ``[graph]``
        is present, degree fallback otherwise) over the depth-expanded candidate subgraph."""
        scores = self._graph().centrality_scores(ids, depth=depth)
        return {i: scores.get(i, 0.0) for i in ids}

    def _confidences(self, ids: list) -> dict:
        """Mean confidence of edges incident (in OR out) to each candidate — already in [0,1], used raw.
        A node with no incident edges is absent (→ 0 contribution). A self-loop is counted once (the dst arm
        excludes ``src == dst``), not double (P9-11)."""
        rows = self.store.conn.execute(
            "SELECT id, AVG(conf) AS mc FROM ("
            "  SELECT src AS id, confidence AS conf FROM edges WHERE src IN (SELECT value FROM json_each(:ids)) "
            "  UNION ALL "
            "  SELECT dst AS id, confidence AS conf FROM edges "
            "    WHERE dst IN (SELECT value FROM json_each(:ids)) AND src != dst "
            ") GROUP BY id",
            {"ids": json.dumps(ids)},
        ).fetchall()
        return {r[0]: float(r[1]) for r in rows if r[1] is not None}

    def _recencies(self, ids: list, now: "float | None") -> dict:
        """Exponential recency decay ``exp(-age_days / half_life)`` from the owning file's mtime. Unknown
        mtime → neutral 0.5 (don't penalize unknown age). ``now`` defaults to the corpus's newest mtime
        (reproducible) — see :meth:`rank`."""
        if now is None:
            now = self._default_now()
        hl = self.half_life_days if self.half_life_days > 0 else 30.0   # tolerate a post-construction mutation
        out: dict = {}
        for node_id, mtime in self._mtimes(ids).items():
            if mtime is None:
                out[node_id] = 0.5
            else:
                age_days = max(0.0, (now - mtime) / 86400.0)
                out[node_id] = math.exp(-age_days / hl)
        return out

    def _default_now(self) -> float:
        """The corpus's newest file mtime (deterministic recency clock), or wall-clock if no mtimes exist."""
        row = self.store.conn.execute(
            "SELECT MAX(json_extract(attrs, '$.mtime')) FROM nodes").fetchone()
        if row and row[0] is not None:
            try:
                return float(row[0])
            except (TypeError, ValueError):
                pass
        import time
        return time.time()

    def _mtimes(self, ids: list) -> dict:
        """``{node_id: epoch mtime | None}``. Reads the symbol's denormalized ``attrs.mtime`` if present,
        else the owning file node's ``attrs.mtime`` via the indexed ``file_uid`` (mirrors the FILTER
        builder's ``since`` join — Review remediation C5). A file node's own mtime is taken directly. A
        non-numeric mtime degrades to ``None`` (neutral) rather than crashing the whole rank (P9-8)."""
        rows = self.store.conn.execute(
            "SELECT n.id AS id, "
            "  COALESCE(json_extract(n.attrs, '$.mtime'), json_extract(f.attrs, '$.mtime')) AS mtime "
            "FROM nodes n LEFT JOIN nodes f ON f.uid = n.file_uid AND f.type = 'file' "
            "WHERE n.id IN (SELECT value FROM json_each(:ids))",
            {"ids": json.dumps(ids)},
        ).fetchall()
        out: dict = {}
        for node_id, mtime in rows:
            try:
                out[node_id] = float(mtime) if mtime is not None else None
            except (TypeError, ValueError):
                out[node_id] = None
        return out

    def _graph(self):
        if self.graph_view is None:
            from .graph import GraphView
            self.graph_view = GraphView(self.store)
        return self.graph_view
