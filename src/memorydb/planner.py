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
        symbol = self._symbol(query)
        return {
            "intent": "LOCATE",
            "symbol": symbol,
            "references": Q.references_to(self.store, symbol),
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
    def _symbol(query: str) -> str:
        toks = _IDENT.findall(query)
        # Prefer identifier-shaped tokens (CamelCase / snake_case / dotted) over plain words.
        cands = [t for t in toks if any(c.isupper() for c in t) or "_" in t or "." in t]
        if cands:
            return cands[-1]
        return toks[-1] if toks else ""
