"""The retrieval planner — MemoryDB's analogue of AkasicDB's Traversal-Join, but as plain
Python orchestration, not a cost-based operator (TD-001, TD-007).

Routes by intent:
  LOCATE  -> exact graph lookup (no vectors)
  EXPLAIN -> vector seed -> graph expansion -> subgraph
  FILTER  -> SQL over attributes (adapter-specific; stub in the substrate)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from pydantic import BaseModel, Field

from . import query as Q
from .filters import build_filter_query
from .models import Intent
from .vector import BruteForceVectorIndex

_LOG = logging.getLogger(__name__)

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


class IntentResult(BaseModel):
    """The LLM router's structured verdict: an :class:`Intent` plus the bits the planner routes on —
    a LOCATE ``symbol`` (name or uid), free-text ``entities`` (concept seeds), and a FILTER ``filters``
    dict (allowlisted in :mod:`memorydb.filters`). ``confidence`` is validated to ``[0, 1]`` so an
    out-of-range model reply is treated as a parse failure and falls back to the regex classifier."""

    intent: Intent
    symbol: Optional[str] = None
    entities: list = Field(default_factory=list)
    filters: dict = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of the model's text (tolerating a ``\\`\\`\\`json`` fence or
    surrounding prose). Raises if there is none — the caller treats that as a fallback trigger."""
    t = (text or "").strip()
    if t.startswith("```"):                       # ```json … ``` fence -> drop the fences/label
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        raise ValueError("no JSON object in LLM output")
    return json.loads(t[i:j + 1])


class LLMIntentClassifier:
    """Injectable LLM router (llm-intent-classifier spec; TD-002/TD-007). Wraps an :class:`LLMClient`
    port; on ANY failure (timeout, bad JSON, schema/range violation) it falls back to the regex
    ``DefaultIntentClassifier`` and never raises to the caller. ``analyze`` returns the rich
    :class:`IntentResult`; ``classify`` satisfies the :class:`~memorydb.ports.IntentClassifier` port.

    Two safety downgrades to EXPLAIN (the safe richer path): a low-confidence reply (``< 0.5``) and a
    LOCATE whose ``symbol`` does not exist (``symbol_exists`` callback — the planner wires it to the
    store, keeping this class store-free per TD-002). Results are cached by query string."""

    _SYSTEM = (
        "Classify a code-search query. Return ONLY JSON, no prose:\n"
        '{"intent":"LOCATE|EXPLAIN|FILTER","symbol":str|null,"entities":[str],'
        '"filters":{"type":str?,"lang":str?,"path_glob":str?,"since":str?},"confidence":0..1}\n'
        "LOCATE = find where a named symbol is used/defined. EXPLAIN = understand how something works. "
        "FILTER = list symbols matching structured attributes (type/lang/path/recency).\n"
        "Examples:\n"
        'Q: "where is DeviceNotificationService used?" '
        'A: {"intent":"LOCATE","symbol":"DeviceNotificationService","entities":[],"filters":{},"confidence":0.96}\n'
        'Q: "how do mass notifications work?" '
        'A: {"intent":"EXPLAIN","symbol":null,"entities":["mass notification"],"filters":{},"confidence":0.9}\n'
        'Q: "show me Go functions in pkg/queue changed since 2026-06-15" '
        'A: {"intent":"FILTER","symbol":null,"entities":[],'
        '"filters":{"type":"function","lang":"go","path_glob":"pkg/queue/*","since":"2026-06-15"},"confidence":0.92}'
    )

    def __init__(self, client, fallback=None, cache=None,
                 symbol_exists: Optional[Callable[[str], bool]] = None) -> None:
        self.client = client
        self.fallback = fallback or DefaultIntentClassifier()
        self.cache = {} if cache is None else cache
        self.symbol_exists = symbol_exists

    def classify(self, query: str) -> Intent:
        return self.analyze(query).intent

    def analyze(self, query: str) -> IntentResult:
        if query in self.cache:
            return self.cache[query]
        result = self._analyze_uncached(query)
        self.cache[query] = result
        return result

    def _analyze_uncached(self, query: str) -> IntentResult:
        try:
            data = _extract_json(self.client.complete(self._SYSTEM, query))
            result = IntentResult(**data)
        except Exception as exc:                  # any failure -> regex fallback, never raise (spec)
            _LOG.debug("LLM intent classify failed (%s); using regex fallback", exc)
            return IntentResult(intent=self.fallback.classify(query))
        if result.confidence < 0.5:               # ambiguous -> safe richer path
            result = result.model_copy(update={"intent": Intent.EXPLAIN})
        if (result.intent is Intent.LOCATE and result.symbol
                and self.symbol_exists is not None and not self.symbol_exists(result.symbol)):
            result = result.model_copy(update={"intent": Intent.EXPLAIN})   # hallucinated symbol
        return result


class RetrievalPlanner:
    def __init__(self, store, embedder, index=None, classifier=None) -> None:
        self.store = store
        self.embedder = embedder
        self.index = index or BruteForceVectorIndex(store)
        self.classifier = classifier or DefaultIntentClassifier()
        # Wire the LLM router's hallucination guard to the store without making the classifier import
        # it (TD-002): any analyze-capable classifier with no symbol_exists set gets the store check.
        if getattr(self.classifier, "analyze", None) and getattr(self.classifier, "symbol_exists", True) is None:
            self.classifier.symbol_exists = self._symbol_exists

    def retrieve(self, query: str, k: int = 5, depth: int = 2) -> dict:
        # A rich classifier (LLM router) exposes analyze() -> IntentResult with symbol/filters; the
        # plain regex port only has classify() -> Intent. Route on whichever is available.
        if callable(getattr(self.classifier, "analyze", None)):
            result = self.classifier.analyze(query)
            if result.intent is Intent.LOCATE:
                return self.locate(query, symbol=result.symbol)
            if result.intent is Intent.FILTER:
                return self._filter(result, k=k)
            return self.explain(query, k=k, depth=depth)
        intent = self.classifier.classify(query)
        if intent is Intent.LOCATE:
            return self.locate(query)
        if intent is Intent.FILTER:
            return {"intent": "FILTER", "filters": {}, "nodes": [], "matched_ids": [], "dropped_keys": [],
                    "note": "classify-only classifier returned FILTER with no filter predicates."}
        return self.explain(query, k=k, depth=depth)

    # --- intent handlers (public: the facade routes to these directly) -----
    def locate(self, query: str, symbol: Optional[str] = None) -> dict:
        # Ground the bare query against the index: try each identifier-shaped token and pick the
        # first that actually names a symbol. This drops stopwords ("where"/"used") without a stop
        # list and makes the regex default far less brittle than "take the last token" (TD-007). An
        # LLM-supplied ``symbol`` (name or uid) is tried FIRST — a uid resolves to exactly one target,
        # collapsing the ambiguity grouping to a single match (spec C4).
        found = ""
        matched_uids: list[str] = []
        candidates = ([symbol] if symbol else []) + self._candidates(query)
        for tok in candidates:
            rows = self.store.conn.execute(
                "SELECT uid FROM nodes WHERE name = :t OR uid = :t", {"t": tok}
            ).fetchall()
            if rows:
                found = tok
                matched_uids = [r[0] for r in rows]
                break
        refs = Q.references_to(self.store, found) if found else []
        # A bare name can match several symbols (methods named `send` in different classes); report
        # the ambiguity explicitly rather than silently merging (C4). A uid from the LLM classifier
        # yields exactly one match.
        by_target: dict = {}
        for r in refs:
            by_target.setdefault(r["target_uid"], []).append(r)
        return {
            "intent": "LOCATE",
            "symbol": found,
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

    def _filter(self, result: "IntentResult", k: int = 5) -> dict:
        """FILTER: an allowlisted, parameterized SQL query over symbol attributes (no injection — every
        value is bound). Returns the matched nodes (file nodes excluded), in deterministic uid order."""
        sql, params, dropped = build_filter_query(result.filters, limit=None)
        if dropped:
            _LOG.debug("FILTER dropped unsupported/empty keys: %s", dropped)
        if sql is None:                           # nothing usable -> clean empty result (spec)
            return {"intent": "FILTER", "filters": result.filters, "nodes": [], "matched_ids": [],
                    "dropped_keys": dropped, "note": "no usable filter predicate"}
        ids = [r[0] for r in self.store.conn.execute(sql, params).fetchall()]
        nodes = sorted(self.store.get_nodes(ids), key=lambda n: n["uid"])   # get_nodes() is unordered
        return {"intent": "FILTER", "filters": result.filters, "nodes": nodes,
                "matched_ids": ids, "dropped_keys": dropped}

    def _symbol_exists(self, symbol: str) -> bool:
        """Does a non-file node match ``symbol`` by name or uid? Backs the LLM router's hallucination
        guard (a LOCATE on a symbol absent from the graph downgrades to EXPLAIN)."""
        if not symbol:
            return False
        row = self.store.conn.execute(
            "SELECT 1 FROM nodes WHERE (name = :t OR uid = :t) AND type != 'file' LIMIT 1",
            {"t": symbol},
        ).fetchone()
        return row is not None

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
