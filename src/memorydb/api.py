"""Public API facade — the ``MemoryDB`` class (public-api-facade spec; TD-002).

A single ergonomic entry point that wires the substrate, an adapter, an embedder, the indexer and
the planner together, so callers write ``db.index(path)`` / ``db.ask("…")`` instead of assembling the
parts. This is *thin orchestration* over the existing pieces — it owns no new storage logic and never
hides the ports: every default is overridable and ``store`` / ``planner`` stay reachable (TD-002).

``context`` / ``ask(as_context=True)`` delegate to the :class:`~memorydb.context.ContextBuilder`
(context-builder-packing spec) for token-budgeted, relationship-aware packing.
"""
from __future__ import annotations

import warnings

from . import query as Q
from .context import ContextBuilder, ContextResult
from .embedders import HashingEmbedder
from .embedding_pipeline import DefaultSerializer, EmbeddingPipeline, EmbedReport
from .graph import GraphView
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




class MemoryDB:
    """The headline facade. Construct via :meth:`open`; ``index`` / ``ask`` / ``locate`` / ``explain``
    / ``context`` cover the common flows. Every port (embedder, extractors, classifier, vector index)
    is injectable, and :attr:`store` / :attr:`planner` are escape hatches to the raw substrate."""

    def __init__(self, store, embedder, extractors, classifier, vector_index, query_cache=None,
                 graph_view=None) -> None:
        self._store = store
        self._graph_view = graph_view   # lazily built on first access if not injected (TD-003 / graph spec)
        self._embedder = embedder
        self._extractors = list(extractors)
        self._serializer = DefaultSerializer()
        self._pipeline = EmbeddingPipeline(store, embedder, serializer=self._serializer)
        # The indexer does graph ingestion only; embedding is owned by refresh_embeddings() so it
        # happens in exactly one place (avoids a double pass — the spec's index() step 2).
        self._indexer = Indexer(store, self._extractors, embedder=None, ignore=IgnoreMatcher())
        store.attach_index(vector_index)   # set_embedding keeps the (vec0) index in sync (sqlite-vec-acceleration)
        self._planner = RetrievalPlanner(store, embedder, index=vector_index, classifier=classifier,
                                         query_cache=query_cache)
        self._builder = ContextBuilder()
        self._closed = False

    # --- construction ------------------------------------------------------
    @classmethod
    def open(cls, path: str = ":memory:", *, embedder=None, extractors=None,
             classifier=None, vector_index=None, query_cache=None, graph_view=None) -> "MemoryDB":
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
        db = cls(store, embedder, extractors, classifier, vector_index, query_cache=query_cache,
                 graph_view=graph_view)
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
        use it after switching embedding models. A full reembed may change the embedding dim, so it
        rebuilds the derived ANN index from the new BLOBs afterwards (re-review P5-3)."""
        self._ensure_open()
        rep = self._pipeline.reembed_all() if full else self._pipeline.refresh()
        if full:
            self.rebuild_vector_index()
        return rep

    def rebuild_vector_index(self) -> int:
        """Rebuild the derived ANN index (vec0) from the authoritative ``embeddings`` BLOBs — the drift
        backstop after deletes, a dim/model change, or a crash. No-op (returns 0) for the brute-force
        backend, which reads the BLOBs directly (re-review P5-1)."""
        self._ensure_open()
        idx = getattr(self._planner, "index", None)
        return idx.rebuild_index() if hasattr(idx, "rebuild_index") else 0

    # --- query-embedding cache (TD-011) ------------------------------------
    def clear_query_cache(self) -> None:
        """Empty the in-memory query-embedding cache (e.g. after switching models or to free memory)."""
        self._ensure_open()
        self._planner.clear_query_cache()

    def dump_query_cache(self, path: str) -> int:
        """Flush the query-embedding cache to a compact, model-validated binary file at ``path`` (a hot-
        query accelerator — disposable, safe to delete). Returns the record count. The caller chooses the
        location (e.g. a model-keyed cache file shared across DBs, or a db sidecar — TD-011)."""
        self._ensure_open()
        return self._planner.query_cache.dump(path)

    def load_query_cache(self, path: str) -> int:
        """Warm the query-embedding cache from a dump at ``path`` — model-validated, so a missing/corrupt/
        wrong-model file is ignored (returns 0). Returns the records loaded."""
        self._ensure_open()
        return self._planner.query_cache.load(path)

    # --- retrieval ---------------------------------------------------------
    def ask(self, query: str, *, k: int = 5, depth: int = 2, as_context: bool = False,
            budget_tokens: int = 2000):
        """Route ``query`` by intent (LOCATE / EXPLAIN / FILTER) and return the result.

        Returns the raw planner dict by default; with ``as_context=True`` returns a
        :class:`ContextResult` (the union is intentional — see the spec's Review remediation)."""
        self._ensure_open()
        result = self._planner.retrieve(query, k=k, depth=depth)
        if as_context:
            return self._builder.build(result, budget_tokens)
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
        """Packed EXPLAIN: the retrieved subgraph rendered into a token-budgeted, LLM-ready context
        (cards + a Relationships block, with file:line provenance) via the ContextBuilder."""
        self._ensure_open()
        return self._builder.build(self.explain(query, k=k, depth=depth), budget_tokens)

    # --- escape hatches & lifecycle ----------------------------------------
    @property
    def store(self) -> Store:
        return self._store

    @property
    def planner(self) -> RetrievalPlanner:
        return self._planner

    @property
    def graph_view(self) -> GraphView:
        """On-demand graph algorithms over the substrate (PageRank/centrality/paths — TD-003). Lazily
        built over :attr:`store` on first access (or injected via ``open(graph_view=…)``); the hybrid
        ranker reaches centrality through ``graph_view.centrality_scores(seed_ids)``."""
        self._ensure_open()
        if self._graph_view is None:
            self._graph_view = GraphView(self._store)
        return self._graph_view

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
