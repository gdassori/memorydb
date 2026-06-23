"""Public API facade — the ``MemoryDB`` class (public-api-facade spec; TD-002).

A single ergonomic entry point that wires the substrate, an adapter, an embedder, the indexer and
the planner together, so callers write ``db.index(path)`` / ``db.ask("…")`` instead of assembling the
parts. This is *thin orchestration* over the existing pieces — it owns no new storage logic and never
hides the ports: every default is overridable and ``store`` / ``planner`` stay reachable (TD-002).

The context-packing here is a deliberate placeholder: a small token-budgeted serializer so ``context``
/ ``ask(as_context=True)`` work end-to-end today. The dedicated ``context-builder-packing`` spec will
supersede ``_pack_*`` with a richer ContextBuilder (kept behind the same ``ContextResult`` shape).
"""
from __future__ import annotations

import warnings

from pydantic import BaseModel, Field

from . import query as Q
from .embedders import HashingEmbedder
from .embedding_pipeline import DefaultSerializer, EmbeddingPipeline, EmbedReport
from .indexer import IgnoreMatcher, Indexer, IndexReport
from .planner import DefaultIntentClassifier, RetrievalPlanner
from .store import Store
from .vector import make_vector_index


class ExtractorRegistry:
    """Builds the default set of extractors: the multilang tree-sitter ``CodeAdapter`` (when the
    ``[code]`` extra is installed) plus the stdlib ``PythonResolver`` (always). Python files get
    precise ast/symtable edges that supersede the coarse tree-sitter ones via MAX-confidence upsert;
    other languages get coarse edges only. With no ``[code]`` extra, Python is still fully handled."""

    @staticmethod
    def default() -> list:
        from .adapters.code.python_resolver import PythonResolver
        extractors: list = []
        try:
            from .adapters.code import CodeAdapter
            extractors.append(CodeAdapter())
        except NotImplementedError:
            warnings.warn(
                "MemoryDB: the [code] extra is not installed — only Python is indexed (precise "
                "ast/symtable resolver). For other languages run: pip install -e '.[code]' (tree-sitter).",
                stacklevel=2,
            )
        extractors.append(PythonResolver())  # stdlib, no extra required
        return extractors


class ContextResult(BaseModel):
    """A token-budgeted, LLM-ready packing of a retrieval result.

    ``truncated`` is True when the budget cut off content. ``used_tokens`` is an estimate (≈4 chars/
    token) over ``text``; it never exceeds ``budget_tokens``. Placeholder shape for the future
    ContextBuilder (context-builder-packing spec)."""

    text: str
    uids: list = Field(default_factory=list)
    used_tokens: int = 0
    budget_tokens: int = 0
    truncated: bool = False
    intent: str = ""


def _est_tokens(s: str) -> int:
    """Cheap, model-agnostic token estimate (≈4 chars/token). Good enough to enforce a budget without
    pulling in a tokenizer dependency; the ContextBuilder spec can swap in a real one."""
    return max(1, (len(s) + 3) // 4)


class MemoryDB:
    """The headline facade. Construct via :meth:`open`; ``index`` / ``ask`` / ``locate`` / ``explain``
    / ``context`` cover the common flows. Every port (embedder, extractors, classifier, vector index)
    is injectable, and :attr:`store` / :attr:`planner` are escape hatches to the raw substrate."""

    def __init__(self, store, embedder, extractors, classifier, vector_index) -> None:
        self._store = store
        self._embedder = embedder
        self._extractors = list(extractors)
        self._serializer = DefaultSerializer()
        self._pipeline = EmbeddingPipeline(store, embedder, serializer=self._serializer)
        # The indexer does graph ingestion only; embedding is owned by refresh_embeddings() so it
        # happens in exactly one place (avoids a double pass — the spec's index() step 2).
        self._indexer = Indexer(store, self._extractors, embedder=None, ignore=IgnoreMatcher())
        self._planner = RetrievalPlanner(store, embedder, index=vector_index, classifier=classifier)
        self._closed = False

    # --- construction ------------------------------------------------------
    @classmethod
    def open(cls, path: str = ":memory:", *, embedder=None, extractors=None,
             classifier=None, vector_index=None) -> "MemoryDB":
        """Open (or create) a MemoryDB at ``path`` with sane, overridable defaults.

        Defaults: ``HashingEmbedder`` (offline, NOT semantic-quality — pass a real model for
        production), ``ExtractorRegistry.default()``, ``DefaultIntentClassifier``, and
        ``make_vector_index`` (sqlite-vec when available, else brute force). ``path=":memory:"`` is
        single-process only."""
        store = Store(path)
        if embedder is None:
            warnings.warn(
                "MemoryDB is using the default HashingEmbedder — offline and deterministic but NOT "
                "semantic-quality. Pass embedder=<your model> for real retrieval.",
                stacklevel=2,
            )
            embedder = HashingEmbedder()
        if extractors is None:
            extractors = ExtractorRegistry.default()
        if classifier is None:
            classifier = DefaultIntentClassifier()
        if vector_index is None:
            vector_index = make_vector_index(store)
        db = cls(store, embedder, extractors, classifier, vector_index)
        db._check_embedder_compat()
        return db

    def _check_embedder_compat(self) -> None:
        """Guard against silently mixing embedders in one store (Review remediation C3). Records the
        embedder identity/dim in ``meta`` and warns if it changed since the store was last written —
        existing embeddings would be stale and the (future) vec0 dim would mismatch."""
        model_id = getattr(self._embedder, "model", None) or type(self._embedder).__name__
        dim = getattr(self._embedder, "dim", None)
        prev_model = self._store.get_meta("embed_model")
        prev_dim = self._store.get_meta("embed_dim")
        if prev_model is not None and prev_model != model_id:
            warnings.warn(
                f"MemoryDB: embedder changed ({prev_model!r} -> {model_id!r}); existing embeddings "
                "are stale. Call refresh_embeddings(full=True) to re-embed, or open a fresh store.",
                stacklevel=2,
            )
        if dim is not None and prev_dim is not None and str(dim) != str(prev_dim):
            warnings.warn(
                f"MemoryDB: embedding dim changed ({prev_dim} -> {dim}); vector search across mixed "
                "dims is invalid. Re-embed with refresh_embeddings(full=True).",
                stacklevel=2,
            )
        self._store.set_meta("embed_model", model_id)
        if dim is not None:
            self._store.set_meta("embed_dim", str(dim))
        self._store.commit()

    # --- ingestion ---------------------------------------------------------
    def index(self, root: str, *, embed: bool = True, force: bool = False) -> IndexReport:
        """Walk ``root``, extract symbols/edges into the substrate, then (re)embed dirty nodes.
        Incremental: unchanged files are skipped, deletions are reaped (see the Indexer). Pass
        ``embed=False`` to ingest the graph now and defer embedding to a later
        ``refresh_embeddings()`` (e.g. the CLI's ``--no-embed``); ``force=True`` re-indexes every file
        (ignores the sha256 skip — a recovery escape hatch)."""
        self._ensure_open()
        rep = self._indexer.index(root, force=force)
        if embed:
            rep.embedded = self.refresh_embeddings().embedded
        return rep

    def refresh_embeddings(self, *, full: bool = False) -> EmbedReport:
        """(Re)embed nodes whose neighborhood changed (TD-006). ``full=True`` re-embeds everything —
        use it after switching embedding models."""
        self._ensure_open()
        return self._pipeline.reembed_all() if full else self._pipeline.refresh()

    # --- retrieval ---------------------------------------------------------
    def ask(self, query: str, *, k: int = 5, depth: int = 2, as_context: bool = False,
            budget_tokens: int = 2000):
        """Route ``query`` by intent (LOCATE / EXPLAIN / FILTER) and return the result.

        Returns the raw planner dict by default; with ``as_context=True`` returns a
        :class:`ContextResult` (the union is intentional — see the spec's Review remediation)."""
        self._ensure_open()
        result = self._planner.retrieve(query, k=k, depth=depth)
        if as_context:
            return self._pack_result(result, budget_tokens)
        return result

    def locate(self, symbol: str) -> list:
        """Exact LOCATE: every reference (incoming edge) to ``symbol`` (matched by name or uid).
        Precise edges sort before coarse heuristic ones (TD-005)."""
        self._ensure_open()
        return Q.references_to(self._store, symbol)

    def explain(self, query: str, *, k: int = 5, depth: int = 2) -> dict:
        """Force the EXPLAIN path: vector seed → graph expansion → subgraph (nodes + edges)."""
        self._ensure_open()
        return self._planner.explain(query, k=k, depth=depth)

    def context(self, query: str, *, k: int = 5, depth: int = 2,
                budget_tokens: int = 2000) -> ContextResult:
        """Packed EXPLAIN: the retrieved subgraph rendered into a token-budgeted, LLM-ready string."""
        self._ensure_open()
        return self._pack_result(self.explain(query, k=k, depth=depth), budget_tokens)

    # --- packing (placeholder for context-builder-packing) -----------------
    def _pack_result(self, result: dict, budget_tokens: int) -> ContextResult:
        intent = result.get("intent", "")
        if intent == "LOCATE":
            blocks = self._locate_blocks(result)
        else:  # EXPLAIN (or anything subgraph-shaped); FILTER falls through to an empty pack
            blocks = self._explain_blocks(result)
        return self._pack_blocks(blocks, budget_tokens, intent)

    def _explain_blocks(self, result: dict):
        """One block per node, seeds first (best vector match), then the rest by id — the order the
        planner already considers most relevant."""
        nodes = {n["id"]: n for n in result.get("nodes", [])}
        seeds = [s for s in result.get("seeds", []) if s in nodes]
        ordered = seeds + [nid for nid in sorted(nodes) if nid not in set(seeds)]
        return [(nodes[nid]["uid"], self._render_node(nodes[nid])) for nid in ordered]

    @staticmethod
    def _locate_blocks(result: dict):
        sym = result.get("symbol") or "?"
        blocks = [(r["src_uid"], f"{r['src_uid']}  --{r['relation']}-->  {sym}"
                                  f"  (confidence {r['confidence']:.2f})")
                  for r in result.get("references", [])]
        return blocks or [("", f"No references to {sym!r}.")]

    @staticmethod
    def _render_node(node: dict) -> str:
        attrs = node.get("attrs") or {}
        path = attrs.get("file_uid") or node["uid"]
        loc = f":{attrs['start_line']}" if attrs.get("start_line") else ""
        head = f"## {node['name']}  ({node['type']}) — {path}{loc}"
        parts = [head]
        sig = (attrs.get("signature") or "").strip()
        if sig:
            parts.append(sig)
        body = (node.get("body") or "").strip()
        if body and body != sig:
            parts.append(body)
        return "\n".join(parts)

    @staticmethod
    def _pack_blocks(blocks, budget_tokens: int, intent: str) -> ContextResult:
        kept, uids, used, truncated = [], [], 0, False
        for uid, block in blocks:
            cost = _est_tokens(block)
            if used + cost > budget_tokens:
                truncated = True
                break
            kept.append(block)
            if uid:
                uids.append(uid)
            used += cost
        return ContextResult(
            text="\n\n".join(kept), uids=uids, used_tokens=used,
            budget_tokens=budget_tokens, truncated=truncated, intent=intent,
        )

    # --- escape hatches & lifecycle ----------------------------------------
    @property
    def store(self) -> Store:
        return self._store

    @property
    def planner(self) -> RetrievalPlanner:
        return self._planner

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("MemoryDB is closed; open() a new instance.")

    def close(self) -> None:
        if not self._closed:
            self._store.close()
            self._closed = True

    def __enter__(self) -> "MemoryDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
