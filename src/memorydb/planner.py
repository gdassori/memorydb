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
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.:]*")

# Pure query glue — interrogatives / articles / prepositions / auxiliaries that are NEVER identifiers.
# Dropping these stops a question word from being grounded as the target (R6-13). The LOCATE/EXPLAIN
# verbs (use/call/get/set/work/flow/reference/invoke/...) are deliberately NOT here: they are common
# real method names, so we keep them and let the index grounding (WHERE name=:t) reject non-matches —
# otherwise a symbol literally named `get`/`call` could never be located (R7-1).
_STOPWORDS = frozenset({
    "where", "who", "which", "what", "when", "whose", "how", "why", "is", "are", "was", "were", "be",
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "do", "does", "did", "this",
    "that", "these", "those", "it", "its", "by", "with", "as",
})

# The LOCATE/EXPLAIN verbs: kept as groundable candidates (a symbol can be named `get`/`call`) but
# demoted to last-resort so a query verb never out-ranks the real target on length (R8-3).
_VERBS = frozenset({
    "use", "used", "uses", "call", "calls", "called", "reference", "references", "invoke", "invokes",
    "get", "set", "work", "works", "flow", "explain", "describe", "overview", "from",
})


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
            return self.locate(query)
        if intent is Intent.FILTER:
            return {"intent": "FILTER", "note": "FILTER routing is adapter-specific; not in the v0 substrate."}
        return self.explain(query, k=k, depth=depth)

    # --- intent handlers (public: the facade routes to these directly) -----
    def locate(self, query: str) -> dict:
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

    def explain(self, query: str, k: int = 5, depth: int = 2) -> dict:
        qvec = self.embedder.embed([query])[0]
        # Drop seeds with no feature overlap (cosine ~0): a query that matches nothing should seed on
        # nothing, not on arbitrary near-orthogonal vectors (R6-22).
        seeds = [node_id for score, node_id in self.index.search(qvec, k=k) if score > 1e-9]
        reached = Q.traverse(self.store, seeds, max_depth=depth, direction="both")
        ids = [r["id"] for r in reached]
        return {
            "intent": "EXPLAIN",
            "seeds": seeds,
            "depths": {r["id"]: r["depth"] for r in reached},   # for the context builder's ranking
            "nodes": self.store.get_nodes(ids),
            "edges": Q.subgraph_edges(self.store, ids),
        }

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _candidates(query: str) -> list[str]:
        """Identifier tokens ordered best-first: identifier-shaped (CamelCase/snake/dotted) before
        plain words, longest first. Stopwords (incl. the LOCATE/EXPLAIN verbs) are dropped so a question
        word that names a real symbol isn't grounded as the target (R6-13). For a dotted/qualified token
        (``mod.foo`` / ``a.py::foo``) the bare last component is also offered, since a symbol's ``name``
        is just the last segment (R6-9)."""
        toks = _IDENT.findall(query)
        cands: list[str] = []
        seen: set = set()
        for t in toks:
            for c in (t, t.rsplit("::", 1)[-1].rsplit(".", 1)[-1]):   # the token, then its bare tail
                if c and c.lower() not in _STOPWORDS and c not in seen:
                    seen.add(c)
                    cands.append(c)
        shaped = [t for t in cands if any(ch.isupper() for ch in t) or "_" in t or "." in t or ":" in t]
        rest = [t for t in cands if t not in shaped]
        # Within the plain bucket, demote the LOCATE/EXPLAIN verbs to LAST resort: they stay locatable
        # (so a symbol named `get`/`call` resolves) but never beat the real target on length (R8-3).
        plain = [t for t in rest if t.lower() not in _VERBS]
        verbs = [t for t in rest if t.lower() in _VERBS]
        return (sorted(shaped, key=len, reverse=True) + sorted(plain, key=len, reverse=True)
                + sorted(verbs, key=len, reverse=True))
