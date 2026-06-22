"""The retrieval planner — MemoryDB's analogue of AkasicDB's Traversal-Join, but as plain
Python orchestration, not a cost-based operator (TD-001, TD-007).

Routes by intent:
  LOCATE  -> exact graph lookup (no vectors)
  EXPLAIN -> vector seed -> graph expansion -> subgraph
  FILTER  -> SQL over attributes (adapter-specific; stub in the substrate)
"""
from __future__ import annotations

import re
from typing import Optional

from . import query as Q
from .models import Intent
from .vector import BruteForceVectorIndex

_LOCATE = re.compile(
    r"\b(where|who|which)\b.*\b(use|used|uses|call|calls|called|reference|references|invoke|invokes)\b",
    re.I,
)
_EXPLAIN = re.compile(r"\b(how|why|explain|describe|overview|work|works|flow)\b", re.I)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


class DefaultIntentClassifier:
    """Cheap regex router. Ambiguous queries fall through to EXPLAIN (the richer path)."""

    def classify(self, query: str) -> Intent:
        if _LOCATE.search(query):
            return Intent.LOCATE
        if _EXPLAIN.search(query):
            return Intent.EXPLAIN
        return Intent.EXPLAIN


class RetrievalPlanner:
    def __init__(self, store, embedder, index=None, classifier=None) -> None:
        self.store = store
        self.embedder = embedder
        self.index = index or BruteForceVectorIndex(store)
        self.classifier = classifier or DefaultIntentClassifier()

    def retrieve(self, query: str, k: int = 5, depth: int = 2) -> dict:
        intent = self.classifier.classify(query)
        if intent is Intent.LOCATE:
            return self._locate(query)
        if intent is Intent.FILTER:
            return {"intent": "FILTER", "note": "FILTER routing is adapter-specific; not in the v0 substrate."}
        return self._explain(query, k=k, depth=depth)

    # --- intent handlers ---------------------------------------------------
    def _locate(self, query: str) -> dict:
        # Ground the bare query against the index: try each identifier-shaped token and pick the
        # first that actually names a symbol. This drops stopwords ("where"/"used") without a stop
        # list and makes the regex default far less brittle than "take the last token" (TD-007).
        symbol = ""
        matched_uids: list[str] = []
        for tok in self._candidates(query):
            rows = self.store.conn.execute(
                "SELECT uid FROM nodes WHERE name = :t OR uid = :t", {"t": tok}
            ).fetchall()
            if rows:
                symbol = tok
                matched_uids = [r[0] for r in rows]
                break
        refs = Q.references_to(self.store, symbol) if symbol else []
        # A bare name can match several symbols (methods named `send` in different classes); report
        # the ambiguity explicitly rather than silently merging (C4). A uid from the LLM classifier
        # yields exactly one match.
        by_target: dict = {}
        for r in refs:
            by_target.setdefault(r["target_uid"], []).append(r)
        return {
            "intent": "LOCATE",
            "symbol": symbol,
            "matched_uids": matched_uids,
            "ambiguous": len(matched_uids) > 1,
            "references": refs,
            "by_target": by_target,
        }

    def _explain(self, query: str, k: int, depth: int) -> dict:
        qvec = self.embedder.embed([query])[0]
        seeds = [node_id for _, node_id in self.index.search(qvec, k=k)]
        reached = Q.traverse(self.store, seeds, max_depth=depth, direction="both")
        ids = [r["id"] for r in reached]
        return {
            "intent": "EXPLAIN",
            "seeds": seeds,
            "nodes": self.store.get_nodes(ids),
            "edges": Q.subgraph_edges(self.store, ids),
        }

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _candidates(query: str) -> list[str]:
        """Identifier tokens ordered best-first: identifier-shaped (CamelCase/snake/dotted) before
        plain words, longest first. The caller grounds these against the index (see ``_locate``)."""
        toks = _IDENT.findall(query)
        shaped = [t for t in toks if any(c.isupper() for c in t) or "_" in t or "." in t]
        rest = [t for t in toks if t not in shaped]
        return sorted(shaped, key=len, reverse=True) + sorted(rest, key=len, reverse=True)
