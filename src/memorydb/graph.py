"""On-demand graph algorithms over a *bounded* subgraph (TD-003).

``GraphView`` loads a subgraph from the SQLite ``edges`` table into a NetworkX ``DiGraph`` on demand, to
run algorithms (PageRank, centrality, shortest paths, communities) that recursive CTEs do not express well
— **without ever making NetworkX the source of truth** (TD-003: SQLite stays authoritative; this view is
read-only and ephemeral). We always materialize a *bounded* subgraph (seeded + depth-limited) so cost
scales with the subgraph, not the whole DB.

NetworkX is the optional ``[graph]`` extra and is imported **lazily** — ``import memorydb`` stays zero-dep,
and degree centrality has a tiny pure-Python fallback (:func:`_degree_centrality_raw` /
:meth:`GraphView.degree_centrality`) so basic ranking still works with no extra installed. Scores are keyed
by integer ``node_id``. :meth:`GraphView.centrality_scores` is the one-call entry point the hybrid ranker
uses — it returns ``{node_id: score}`` from real PageRank when ``[graph]`` is present and *degrades
internally* to the degree fallback when it is not (so callers never branch on the extra).
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from . import query

_LOG = logging.getLogger(__name__)

# Whole-graph (``sg=None``) algorithms are guarded by ceilings: above them we degrade (PageRank) or raise
# rather than build/run over a huge graph (these are unbenchmarked — the eval harness measures where
# subgraph-local must take over). PageRank/build are bounded by node AND edge count; path-based centralities
# (betweenness/closeness) are O(V·E) and far costlier, so they get a much tighter ceiling of their own.
_GLOBAL_NODE_CEILING = 50_000
_GLOBAL_EDGE_CEILING = 250_000
_PATH_CENTRALITY_CEILING = 2_000


def _require_networkx():
    """Import NetworkX lazily, raising a clear, actionable ImportError if the ``[graph]`` extra is absent."""
    try:
        import networkx as nx  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "GraphView needs the optional [graph] extra (NetworkX): pip install 'memorydb[graph]'. "
            "Degree centrality has a zero-dep fallback (GraphView.degree_centrality / centrality(kind='degree') "
            "/ centrality_scores(..., prefer='degree')); PageRank, betweenness/closeness, communities and "
            "shortest_path require NetworkX."
        ) from e
    return nx


def _pagerank_power(nodes: Sequence[int], out_adj: dict, alpha: float = 0.85,
                    max_iter: int = 100, tol: float = 1.0e-6) -> dict[int, float]:
    """Weighted PageRank via pure-Python power iteration — no numpy/scipy (modern ``nx.pagerank`` dispatches
    to scipy, which the lightweight ``[graph]`` extra deliberately does not pull in). ``out_adj`` maps each
    node to a list of ``(neighbor, weight)``; dangling nodes (no positive out-weight) redistribute their
    mass uniformly, matching NetworkX's semantics. Deterministic; scores sum to 1."""
    n = len(nodes)
    if n == 0:
        return {}
    wsum = {v: sum(w for _, w in out_adj.get(v, ()) if w > 0.0) for v in nodes}
    dangling = [v for v in nodes if wsum[v] <= 0.0]
    teleport = (1.0 - alpha) / n
    x = {v: 1.0 / n for v in nodes}
    for _ in range(max_iter):
        xlast = x
        dangle = alpha * sum(xlast[v] for v in dangling) / n
        x = {v: teleport + dangle for v in nodes}
        for v in nodes:
            s = wsum[v]
            if s <= 0.0:
                continue
            share = alpha * xlast[v] / s
            for m, w in out_adj[v]:
                if w > 0.0:
                    x[m] += share * w
        if sum(abs(x[v] - xlast[v]) for v in nodes) < n * tol:
            break
    total = sum(x.values()) or 1.0
    return {v: x[v] / total for v in nodes}


def _degree_centrality_raw(node_ids: Sequence[int], edges) -> dict[int, float]:
    """Pure-Python degree centrality, matching ``networkx.degree_centrality`` semantics so the zero-dep
    fallback and the NetworkX path agree: total (in+out) degree over ``n-1``; a self-loop counts twice;
    a graph of <=1 node yields ``1.0`` (never a divide-by-zero). ``edges`` is an iterable of
    ``(src_id, dst_id)`` (already collapsed to unique pairs by the callers, mirroring a DiGraph)."""
    ids = list(dict.fromkeys(int(n) for n in node_ids))   # unique, insertion-ordered
    n = len(ids)
    if n <= 1:
        return {nid: 1.0 for nid in ids}
    scale = 1.0 / (n - 1)
    deg = {nid: 0 for nid in ids}
    for src, dst in edges:
        src, dst = int(src), int(dst)
        if src in deg:
            deg[src] += 1
        if dst in deg:
            deg[dst] += 1          # src == dst (self-loop) -> +2, matching nx
    return {nid: deg[nid] * scale for nid in ids}


class GraphView:
    """Read-only, ephemeral view that materializes bounded subgraphs from the store and runs graph
    algorithms over them. Never writes back (TD-003): SQLite is the single source of truth."""

    def __init__(self, store, node_ceiling: int = _GLOBAL_NODE_CEILING,
                 edge_ceiling: int = _GLOBAL_EDGE_CEILING,
                 path_ceiling: int = _PATH_CENTRALITY_CEILING) -> None:
        self.store = store
        self.node_ceiling = node_ceiling
        self.edge_ceiling = edge_ceiling
        self.path_ceiling = path_ceiling

    # --- subgraph construction --------------------------------------------
    def subgraph(self, seed_ids, depth: int = 2, relations=None, direction: str = "both"):
        """Induced subgraph reachable from ``seed_ids`` within ``depth`` hops as an ``nx.DiGraph``.

        Nodes are integer ``node_id``s (isolated/leaf seeds included); each edge carries ``relation`` and
        ``weight`` (= confidence). Parallel relations between the same ordered pair collapse to the
        max-confidence edge (TD-005 lets a precise edge dominate a coarse one). Reuses ``query.traverse``
        (node set) + ``query.subgraph_edges_by_id`` (induced edges, integer endpoints — no uid round-trip)."""
        nx = _require_networkx()
        ids = [r["id"] for r in query.traverse(self.store, seed_ids, depth, relations, direction)]
        g = nx.DiGraph()
        g.add_nodes_from(ids)
        if not ids:
            return g
        for e in query.subgraph_edges_by_id(self.store, ids):
            self._add_or_max_edge(g, e["src"], e["dst"], float(e["confidence"]), e["relation"])
        return g

    @staticmethod
    def _add_or_max_edge(g, s: int, d: int, conf: float, relation: str) -> None:
        if g.has_edge(s, d):
            if conf > g[s][d].get("weight", 0.0):   # collapse multi-edges by max confidence
                g[s][d]["weight"] = conf
                g[s][d]["relation"] = relation
        else:
            g.add_edge(s, d, weight=conf, relation=relation)

    # --- algorithms --------------------------------------------------------
    def pagerank(self, sg=None, *, alpha: float = 0.85, max_iter: int = 100,
                 tol: float = 1.0e-6, weight: str = "weight") -> dict[int, float]:
        """PageRank scores ``{node_id: score}`` over ``sg`` (a subgraph from :meth:`subgraph`). With
        ``sg=None`` runs over the **whole graph**, guarded by the node/edge ceilings: above either we warn
        and degrade to the cheap degree-centrality fallback (never an unbounded build). Edge ``weight`` (=
        confidence) is the transition weight. Pure-Python power iteration — needs no numpy/scipy."""
        if sg is None:
            nc, ec = self._node_count(), self._edge_count()
            if nc > self.node_ceiling or ec > self.edge_ceiling:
                _LOG.warning(
                    "whole-graph PageRank over %d nodes / %d edges exceeds the ceiling (%d nodes, %d edges); "
                    "degrading to the cheap degree-centrality fallback — pass a bounded subgraph "
                    "(GraphView.subgraph(seeds, depth)) for true PageRank.", nc, ec, self.node_ceiling,
                    self.edge_ceiling)
                return self._global_degree()
            nodes, out_adj = self._global_adjacency()
        else:
            nodes = list(sg.nodes())
            out_adj = {v: [] for v in nodes}
            for u, v, w in sg.out_edges(data=weight, default=1.0):
                out_adj[u].append((v, float(w)))
        return _pagerank_power(nodes, out_adj, alpha=alpha, max_iter=max_iter, tol=tol)

    def centrality(self, sg=None, kind: str = "degree") -> dict[int, float]:
        """Centrality ``{node_id: score}`` of ``kind`` in ``degree|betweenness|closeness``. ``degree`` is
        pure-Python (zero-dep, even without the ``[graph]`` extra); the others use NetworkX (unweighted —
        our ``weight`` is a *similarity*, not a distance, so feeding it to path-based centralities would
        invert them). ``sg=None`` → whole graph; the path-based kinds (O(V·E)) are bounded by the tight
        ``path_ceiling`` and **raise** above it (pass a bounded subgraph), since they have no cheap analogue."""
        kind = kind.lower()
        if kind == "degree":
            if sg is None:
                return self._global_degree()
            return _degree_centrality_raw(list(sg.nodes()), list(sg.edges()))
        nx = _require_networkx()
        if sg is None:
            count = self._node_count()
            if count > self.path_ceiling:
                raise ValueError(
                    f"whole-graph {kind} centrality over {count} nodes exceeds the path-centrality ceiling "
                    f"({self.path_ceiling}); it is O(V·E) — pass a bounded subgraph "
                    "(GraphView.subgraph(seeds, depth)) instead.")
            sg = self._global_graph()
        if sg.number_of_nodes() == 0:
            return {}
        if kind == "betweenness":
            return nx.betweenness_centrality(sg)
        if kind == "closeness":
            return nx.closeness_centrality(sg)
        raise ValueError(f"unknown centrality kind: {kind!r} (degree|betweenness|closeness)")

    def shortest_path(self, src_id: int, dst_id: int, *, max_depth: int = 6,
                      direction: str = "out") -> Optional[list[int]]:
        """Shortest path ``[src_id, ..., dst_id]`` (list of node ids), or ``None`` if there is none within
        ``max_depth`` hops. Bounded: searches a subgraph seeded from ``src_id`` (the whole graph is never
        loaded), so a path longer than ``max_depth`` reads as ``None``. ``direction`` controls BOTH the
        materialized node set and the search: ``'out'`` follows edge direction, ``'in'`` follows reversed
        edges, ``'both'`` treats edges as undirected."""
        src_id, dst_id = int(src_id), int(dst_id)
        if src_id == dst_id:
            # trivial path only for a node that actually exists — mirrors traverse's no-phantom-seed
            # contract (MR-20) rather than reporting a non-existent id as trivially reachable.
            return [src_id] if self._node_exists(src_id) else None
        nx = _require_networkx()
        sg = self.subgraph([src_id], depth=max_depth, direction=direction)
        if dst_id not in sg:
            return None
        # The search graph must match how the node set was gathered, else direction='both'/'in' would run a
        # directed search over a set picked undirected/reversed and miss real paths (P8-1).
        if direction == "both":
            search = sg.to_undirected(as_view=True)
        elif direction == "in":
            search = sg.reverse(copy=False)
        else:
            search = sg
        try:
            return nx.shortest_path(search, src_id, dst_id)
        except nx.NetworkXNoPath:
            return None

    def communities(self, sg=None) -> list[set[int]]:
        """Greedy-modularity communities (list of node-id sets) over the undirected projection of ``sg``
        (confidence as edge weight). ``sg=None`` → whole graph (ceiling-guarded). The list is returned in a
        **deterministic** order — largest community first, then by sorted members (the codebase pins
        ordering, RR3-1) — since NetworkX's own order is unstable across versions."""
        nx = _require_networkx()
        if sg is None:
            sg = self._global_graph()
        if sg.number_of_nodes() == 0:
            return []
        from networkx.algorithms.community import greedy_modularity_communities
        comms = greedy_modularity_communities(sg.to_undirected(), weight="weight")
        ordered = sorted((sorted(int(x) for x in c) for c in comms), key=lambda c: (-len(c), c))
        return [set(c) for c in ordered]

    # --- ranking entry point + zero-dep convenience -----------------------
    def centrality_scores(self, seed_ids, depth: int = 2, relations=None, direction: str = "both",
                          prefer: str = "pagerank") -> dict[int, float]:
        """One-call centrality signal ``{node_id: score}`` over the bounded subgraph seeded from
        ``seed_ids`` — the entry point for the hybrid ranker. Uses the best signal available and **degrades
        internally** (like ``make_vector_index``): real PageRank over the built subgraph when the ``[graph]``
        extra is present, else the zero-dep degree-centrality fallback — so callers never branch on the
        extra. ``prefer='degree'`` forces the zero-dep path even when NetworkX is installed."""
        if prefer == "degree":
            return self.degree_centrality(seed_ids, depth, relations, direction)
        try:
            sg = self.subgraph(seed_ids, depth, relations, direction)
        except ImportError:
            return self.degree_centrality(seed_ids, depth, relations, direction)
        return self.pagerank(sg)

    def degree_centrality(self, seed_ids, depth: int = 2, relations=None,
                          direction: str = "both") -> dict[int, float]:
        """Degree centrality over the bounded subgraph **without** building a NetworkX graph — the zero-dep
        ranking fallback for when the ``[graph]`` extra is absent (hybrid-ranker). Same bounded node set as
        :meth:`subgraph`; parallel relations collapse to a single pair (matching the DiGraph view)."""
        ids = [r["id"] for r in query.traverse(self.store, seed_ids, depth, relations, direction)]
        if not ids:
            return {}
        edges = {(e["src"], e["dst"]) for e in query.subgraph_edges_by_id(self.store, ids)}
        return _degree_centrality_raw(ids, edges)

    # --- whole-graph helpers (ceiling-guarded) ----------------------------
    def _node_count(self) -> int:
        return self.store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def _edge_count(self) -> int:
        return self.store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def _node_exists(self, node_id: int) -> bool:
        return self.store.conn.execute(
            "SELECT 1 FROM nodes WHERE id = ?", (int(node_id),)).fetchone() is not None

    def _global_graph(self):
        """Build the whole graph as an ``nx.DiGraph``. Guarded by the node AND edge ceilings so a huge DB
        raises a clear error (callers that can degrade — e.g. :meth:`pagerank` — check the ceilings first
        and never reach this). Edges come straight from the table (already integer ids)."""
        nx = _require_networkx()
        nc, ec = self._node_count(), self._edge_count()
        if nc > self.node_ceiling or ec > self.edge_ceiling:
            raise ValueError(
                f"whole-graph algorithm over {nc} nodes / {ec} edges exceeds the ceiling "
                f"({self.node_ceiling} nodes, {self.edge_ceiling} edges); pass a bounded subgraph "
                "(GraphView.subgraph(seeds, depth)) instead.")
        g = nx.DiGraph()
        g.add_nodes_from(r[0] for r in self.store.conn.execute("SELECT id FROM nodes ORDER BY id"))
        for src, dst, relation, conf in self.store.conn.execute(
            "SELECT src, dst, relation, confidence FROM edges ORDER BY src, dst, relation"
        ):
            self._add_or_max_edge(g, src, dst, float(conf), relation)
        return g

    def _global_degree(self) -> dict[int, float]:
        """Whole-graph degree centrality straight from SQL (no NetworkX) — the cheap degrade target above
        the ceiling. Parallel relations collapse to unique ``(src, dst)`` pairs."""
        ids = [r[0] for r in self.store.conn.execute("SELECT id FROM nodes ORDER BY id")]
        edges = {(s, d) for s, d in self.store.conn.execute("SELECT src, dst FROM edges")}
        return _degree_centrality_raw(ids, edges)

    def _global_adjacency(self):
        """Whole-graph weighted out-adjacency straight from SQL (no NetworkX) for pure-Python PageRank.
        Parallel relations collapse to the max-confidence edge per ``(src, dst)`` pair."""
        nodes = [r[0] for r in self.store.conn.execute("SELECT id FROM nodes ORDER BY id")]
        best: dict = {}
        for src, dst, conf in self.store.conn.execute("SELECT src, dst, confidence FROM edges"):
            k = (src, dst)
            c = float(conf)
            if c > best.get(k, -1.0):
                best[k] = c
        out_adj: dict = {v: [] for v in nodes}
        for (src, dst), c in best.items():
            if src in out_adj:
                out_adj[src].append((dst, c))
        return nodes, out_adj
