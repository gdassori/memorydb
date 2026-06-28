"""GraphView tests (graph-algorithms-networkx spec, TD-003).

Zero-dep tests (pure-Python degree fallback + the ImportError gate) run with core deps only. The NetworkX
path is gated per-test with ``@graph`` (NOT a module-level importorskip — that would also skip the zero-dep
tests). Mirrors the per-test gating convention in test_vector_index.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import GraphView, Node, Rel, Store  # noqa: E402
from memorydb import graph as G  # noqa: E402
from memorydb.graph import _degree_centrality_raw  # noqa: E402


def _have_networkx() -> bool:
    try:
        import networkx  # noqa: F401
        return True
    except ImportError:
        return False


graph = pytest.mark.skipif(not _have_networkx(), reason="[graph] extra (networkx) not installed")


# An in-degree hub: four callers -> NotificationService -> two leaves. The hub accumulates rank from many
# sources (high PageRank) and lies on every caller->leaf path (high betweenness).
_NODES = ["Job", "Scheduler", "Webhook", "Retry", "NotificationService", "RedisQueue", "NotificationLog"]
_CALLERS = ["Job", "Scheduler", "Webhook", "Retry"]
_LEAVES = ["RedisQueue", "NotificationLog"]
_EDGES = [
    ("Job", "NotificationService", Rel.CALLS, 1.0),
    ("Scheduler", "NotificationService", Rel.CALLS, 1.0),
    ("Webhook", "NotificationService", Rel.CALLS, 1.0),
    ("Retry", "NotificationService", Rel.CALLS, 1.0),
    ("NotificationService", "RedisQueue", Rel.CALLS, 0.9),
    ("NotificationService", "NotificationLog", Rel.WRITES, 0.8),
]


def build():
    store = Store(":memory:")
    for uid in _NODES:
        store.upsert_node(Node(uid=uid, type="function", name=uid, body=uid))
    for src, dst, rel, conf in _EDGES:
        store.upsert_edge(src, dst, rel, confidence=conf)
    store.commit()
    ids = {uid: store.id_for(uid) for uid in _NODES}
    return store, ids


# --- zero-dep: pure-Python degree centrality + ImportError gate ----------------------------------------

def test_degree_centrality_raw_matches_formula():
    # n=3 ; edges a->b , b->c (self-loop on a counts twice).
    out = _degree_centrality_raw([1, 2, 3], [(1, 2), (2, 3), (1, 1)])
    # degrees: 1 -> out+selfx2 = 3 ; 2 -> in+out = 2 ; 3 -> in = 1 ; scale = 1/(3-1) = 0.5
    assert out == {1: 1.5, 2: 1.0, 3: 0.5}


def test_degree_centrality_raw_trivial_sizes():
    assert _degree_centrality_raw([], []) == {}
    assert _degree_centrality_raw([7], []) == {7: 1.0}      # <=1 node -> 1.0, never div-by-zero


def test_degree_fallback_is_zero_dep_and_ranks_hub():
    store, ids = build()
    gv = GraphView(store)
    scores = gv.degree_centrality([ids["NotificationService"]], depth=2)
    assert set(scores) == set(ids.values())                 # whole component reached
    hub = scores[ids["NotificationService"]]
    assert hub == max(scores.values())                      # degree-6 hub dominates
    assert all(hub > scores[ids[c]] for c in _CALLERS)
    store.close()


def test_degree_fallback_collapses_parallel_relations():
    # Two relations between the same pair must count as ONE edge (DiGraph view), not two.
    store = Store(":memory:")
    for uid in ("a", "b"):
        store.upsert_node(Node(uid=uid, type="function", name=uid, body=uid))
    store.upsert_edge("a", "b", Rel.CALLS)
    store.upsert_edge("a", "b", Rel.IMPORTS)
    store.commit()
    a, b = store.id_for("a"), store.id_for("b")
    scores = GraphView(store).degree_centrality([a], depth=1)
    assert scores == {a: 1.0, b: 1.0}                       # each has degree 1 over n-1=1
    store.close()


def test_require_networkx_real_path_raises_actionable_error(monkeypatch):
    # Drive the REAL _require_networkx with networkx hidden (not a stub of itself): `import networkx`
    # fails, so the actionable message + chaining are what's exercised (P8-2).
    monkeypatch.setitem(sys.modules, "networkx", None)   # makes `import networkx` raise ImportError
    with pytest.raises(ImportError, match=r"memorydb\[graph\]"):
        G._require_networkx()


def test_networkx_absent_gates_only_the_nx_methods(monkeypatch):
    # Simulate the [graph] extra missing: nx methods raise a clear ImportError; degree stays zero-dep.
    # Patch BOTH the raiser and the probe so centrality_scores degrades via the probe, not a swallowed error.
    def boom():
        raise ImportError("no networkx")
    monkeypatch.setattr(G, "_require_networkx", boom)
    monkeypatch.setattr(G, "_networkx_available", lambda: False)
    store, ids = build()
    gv = GraphView(store)
    with pytest.raises(ImportError):
        gv.subgraph([ids["Job"]])
    with pytest.raises(ImportError):
        gv.shortest_path(ids["Job"], ids["RedisQueue"])
    with pytest.raises(ImportError):
        gv.communities(None)
    # ...but the pure-Python fallbacks still work with no NetworkX.
    assert gv.degree_centrality([ids["Job"]], depth=2)
    assert gv.centrality(None, kind="degree")               # global degree, no nx
    # centrality_scores degrades INTERNALLY to the degree fallback (no caller branch, no raise).
    scores = gv.centrality_scores([ids["NotificationService"]], depth=2)
    assert scores[ids["NotificationService"]] == max(scores.values())
    store.close()


def test_import_memorydb_does_not_import_networkx():
    # Lazy-import guarantee (the zero-dep core promise): importing memorydb must NOT pull in networkx.
    # A subprocess is required because the test session itself imports networkx (P8-2).
    import subprocess
    r = subprocess.run(
        [sys.executable, "-c", "import sys, memorydb; assert 'networkx' not in sys.modules"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert r.returncode == 0, r.stderr


def test_degree_centrality_empty_seeds():
    store, _ = build()
    assert GraphView(store).degree_centrality([], depth=2) == {}
    store.close()


# --- [graph] extra: NetworkX algorithms ----------------------------------------------------------------

@graph
def test_subgraph_respects_depth_bound():
    store, ids = build()
    gv = GraphView(store)
    d1 = gv.subgraph([ids["Job"]], depth=1)
    d2 = gv.subgraph([ids["Job"]], depth=2)
    # depth 1 from Job (both directions): just Job + the hub it calls.
    assert set(d1.nodes()) == {ids["Job"], ids["NotificationService"]}
    # depth 2 expands through the hub to the other callers (in) and the leaves (out) -> whole component.
    assert set(d2.nodes()) == set(ids.values())
    # edge attributes carry relation + confidence(=weight).
    e = d2[ids["NotificationService"]][ids["RedisQueue"]]
    assert e["relation"] == Rel.CALLS and e["weight"] == pytest.approx(0.9)
    store.close()


@graph
def test_pagerank_ranks_hub_above_leaves_and_callers():
    store, ids = build()
    gv = GraphView(store)
    sg = gv.subgraph([ids["NotificationService"]], depth=2)
    pr = gv.pagerank(sg)
    assert set(pr) == set(ids.values())
    assert sum(pr.values()) == pytest.approx(1.0)           # a proper probability distribution
    hub = pr[ids["NotificationService"]]
    assert hub == max(pr.values())                          # the in-degree-4 hub is the top-ranked node
    assert all(hub > pr[ids[leaf]] for leaf in _LEAVES)
    assert all(pr[ids[leaf]] > pr[ids[c]] for leaf in _LEAVES for c in _CALLERS)
    store.close()


@graph
def test_betweenness_centrality_peaks_at_hub():
    store, ids = build()
    cen = GraphView(store).centrality(GraphView(store).subgraph([ids["Job"]], depth=2), kind="betweenness")
    hub = cen[ids["NotificationService"]]
    assert hub == max(cen.values())                         # every caller->leaf path crosses the hub
    assert hub > 0.0
    store.close()


@graph
def test_centrality_degree_matches_fallback_and_networkx():
    import networkx as nx
    store, ids = build()
    gv = GraphView(store)
    sg = gv.subgraph([ids["NotificationService"]], depth=2)
    via_view = gv.centrality(sg, kind="degree")
    assert via_view == pytest.approx(nx.degree_centrality(sg))            # parity with NetworkX
    assert via_view == pytest.approx(gv.degree_centrality([ids["NotificationService"]], depth=2))  # & fallback
    store.close()


@graph
def test_centrality_unknown_kind_raises():
    store, ids = build()
    sg = GraphView(store).subgraph([ids["Job"]], depth=2)
    with pytest.raises(ValueError):
        GraphView(store).centrality(sg, kind="eigenvector")
    store.close()


@graph
def test_shortest_path_directed_and_missing():
    store, ids = build()
    gv = GraphView(store)
    path = gv.shortest_path(ids["Job"], ids["RedisQueue"])
    assert path == [ids["Job"], ids["NotificationService"], ids["RedisQueue"]]
    assert gv.shortest_path(ids["Job"], ids["Job"]) == [ids["Job"]]       # trivial self path
    # a leaf is a sink: no directed path back out to a caller.
    assert gv.shortest_path(ids["RedisQueue"], ids["Job"]) is None
    store.close()


@graph
def test_communities_partition_covers_all_nodes():
    store, ids = build()
    gv = GraphView(store)
    comms = gv.communities(gv.subgraph([ids["NotificationService"]], depth=2))
    assert comms and all(isinstance(c, set) for c in comms)
    union = set().union(*comms)
    assert union == set(ids.values())                       # a partition of the node set
    store.close()


@graph
def test_empty_subgraph_algorithms_are_trivial():
    store, _ = build()
    gv = GraphView(store)
    empty = gv.subgraph([], depth=2)
    assert empty.number_of_nodes() == 0
    assert gv.pagerank(empty) == {}
    assert gv.centrality(empty, kind="betweenness") == {}
    assert gv.communities(empty) == []
    store.close()


@graph
def test_whole_graph_pagerank_and_ceiling_degrade(caplog):
    import logging
    store, ids = build()
    # sg=None -> whole-graph PageRank (under the ceiling) ranks the hub on top.
    true_pr = GraphView(store).pagerank()
    assert true_pr[ids["NotificationService"]] == max(true_pr.values())
    # a tiny ceiling forces the cheap degree-centrality degrade: warns, returns degree scores, and the
    # result must actually DIFFER from true PageRank (proves the degrade fired, not a circular check).
    with caplog.at_level(logging.WARNING, logger="memorydb.graph"):
        degraded = GraphView(store, node_ceiling=2).pagerank()
    assert "exceeds the ceiling" in caplog.text
    assert degraded == pytest.approx(GraphView(store)._global_degree())
    assert degraded != pytest.approx(true_pr)
    store.close()


@graph
def test_global_graph_raises_above_ceiling():
    store, _ = build()
    with pytest.raises(ValueError, match="ceiling"):
        GraphView(store, node_ceiling=2)._global_graph()
    with pytest.raises(ValueError, match="edges"):
        GraphView(store, edge_ceiling=2)._global_graph()           # edge guard, not just nodes (P8-5)
    store.close()


# --- P8 remediation: regression + coverage gaps --------------------------------------------------------

@graph
def test_shortest_path_direction_both_and_in():
    # P8-1 regression: direction must drive BOTH the node set and the search.
    store = Store(":memory:")
    for u in ("a", "b", "hub"):
        store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    store.upsert_edge("a", "hub", Rel.CALLS)
    store.upsert_edge("b", "hub", Rel.CALLS)            # a -> hub <- b : no directed a->b path
    store.commit()
    a, b, hub = store.id_for("a"), store.id_for("b"), store.id_for("hub")
    gv = GraphView(store)
    assert gv.shortest_path(a, b, direction="both") == [a, hub, b]   # undirected path exists
    assert gv.shortest_path(a, b, direction="out") is None           # no directed a->b
    # in-direction: a chain x->y->z, in-reachability from z reaches x.
    store2 = Store(":memory:")
    for u in ("x", "y", "z"):
        store2.upsert_node(Node(uid=u, type="function", name=u, body=u))
    store2.upsert_edge("x", "y", Rel.CALLS)
    store2.upsert_edge("y", "z", Rel.CALLS)
    store2.commit()
    x, y, z = store2.id_for("x"), store2.id_for("y"), store2.id_for("z")
    assert GraphView(store2).shortest_path(z, x, direction="in") == [z, y, x]
    store.close(); store2.close()


@graph
def test_shortest_path_max_depth_bound_bites():
    # a -> b -> c -> d ; the bound, not the graph, must cut off a too-deep path (P8-9).
    store = Store(":memory:")
    for u in ("a", "b", "c", "d"):
        store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    for s, d in (("a", "b"), ("b", "c"), ("c", "d")):
        store.upsert_edge(s, d, Rel.CALLS)
    store.commit()
    a, d = store.id_for("a"), store.id_for("d")
    gv = GraphView(store)
    assert gv.shortest_path(a, d, max_depth=2) is None               # d is 3 hops away
    assert gv.shortest_path(a, d, max_depth=3) == [a, store.id_for("b"), store.id_for("c"), d]
    store.close()


def test_shortest_path_nonexistent_same_node_is_none():
    # P8-7: the src==dst short-circuit must not report a phantom node as trivially reachable.
    store, ids = build()
    gv = GraphView(store)
    assert gv.shortest_path(999_999, 999_999) is None
    assert gv.shortest_path(ids["Job"], ids["Job"]) == [ids["Job"]]  # real node -> trivial path
    store.close()


@graph
def test_global_adjacency_keeps_max_confidence():
    # P8-9: whole-graph PageRank weights must use the MAX confidence per (src,dst) pair, not first/last.
    store = Store(":memory:")
    for u in ("a", "b"):
        store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    store.upsert_edge("a", "b", Rel.CALLS, confidence=0.3)
    store.upsert_edge("a", "b", Rel.IMPORTS, confidence=0.9)         # parallel relation, higher conf
    store.commit()
    a, b = store.id_for("a"), store.id_for("b")
    _, out_adj = GraphView(store)._global_adjacency()
    assert out_adj[a] == [(b, 0.9)]                                  # max wins, single collapsed edge
    store.close()


@graph
def test_pagerank_params_determinism_and_alpha():
    store, ids = build()
    gv = GraphView(store)
    sg = gv.subgraph([ids["NotificationService"]], depth=2)
    hub = ids["NotificationService"]
    # determinism: identical output across runs (exact equality, the docstring's promise).
    assert gv.pagerank(sg) == gv.pagerank(sg)
    # alpha concentrates mass on the structural hub: higher alpha -> higher hub rank.
    assert gv.pagerank(sg, alpha=0.5)[hub] < gv.pagerank(sg, alpha=0.95)[hub]
    # max_iter=1 (no convergence) still yields a valid probability distribution.
    one = gv.pagerank(sg, max_iter=1)
    assert sum(one.values()) == pytest.approx(1.0)
    store.close()


@graph
def test_self_loop_through_store_path():
    # P8-9: a real self-loop edge survives subgraph_edges_by_id, is retained in the DiGraph, and counts +2.
    store = Store(":memory:")
    for u in ("a", "b"):
        store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    store.upsert_edge("a", "b", Rel.CALLS)
    store.upsert_edge("a", "a", Rel.CALLS)              # self-loop
    store.commit()
    a, b = store.id_for("a"), store.id_for("b")
    gv = GraphView(store)
    sg = gv.subgraph([a], depth=1)
    assert sg.has_edge(a, a)                            # self-loop retained
    deg = gv.degree_centrality([a], depth=1)
    # a: out->b (1) + self-loop (2) = 3 ; b: in (1) ; scale 1/(2-1)=1
    assert deg == {a: 3.0, b: 1.0}
    store.close()


@graph
def test_pagerank_disconnected_components():
    # P8-9: two disconnected components; pagerank stays a single distribution summing to 1.
    store = Store(":memory:")
    for u in ("p", "q", "r", "s", "t"):
        store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    store.upsert_edge("p", "q", Rel.CALLS)                       # component 1
    store.upsert_edge("r", "s", Rel.CALLS)
    store.upsert_edge("s", "t", Rel.CALLS)                       # component 2
    store.commit()
    g = {u: store.id_for(u) for u in ("p", "q", "r", "s", "t")}
    pr = GraphView(store).pagerank()
    assert set(pr) == set(g.values())
    assert sum(pr.values()) == pytest.approx(1.0)
    assert pr[g["q"]] > pr[g["p"]]                              # sink accrues rank within its component
    assert pr[g["t"]] > pr[g["r"]]
    store.close()


@graph
def test_path_centrality_ceiling_raises():
    # P8-5: whole-graph betweenness/closeness are O(V*E) -> bounded by the tight path ceiling.
    store, _ = build()
    gv = GraphView(store, path_ceiling=2)
    with pytest.raises(ValueError, match="path-centrality ceiling"):
        gv.centrality(None, kind="betweenness")
    with pytest.raises(ValueError, match="path-centrality ceiling"):
        gv.centrality(None, kind="closeness")
    store.close()


@graph
def test_pagerank_edge_ceiling_degrades(caplog):
    import logging
    store, ids = build()
    with caplog.at_level(logging.WARNING, logger="memorydb.graph"):
        degraded = GraphView(store, edge_ceiling=2).pagerank()   # 6 edges > 2 -> degrade
    assert "edges" in caplog.text
    assert degraded == pytest.approx(GraphView(store)._global_degree())
    store.close()


@graph
def test_communities_order_is_deterministic():
    # P8-6 / R2-1: needs >=2 communities of DIFFERENT sizes to actually exercise the (-len, members) sort
    # (the single-community fixture made this assertion vacuous). Two disconnected cliques: 4 + 3.
    import itertools
    store = Store(":memory:")
    members = {"big": ["p", "q", "r", "s"], "small": ["a", "b", "c"]}
    for group in members.values():
        for u in group:
            store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    for group in members.values():
        for x, y in itertools.combinations(group, 2):     # fully-connected, both directions
            store.upsert_edge(x, y, Rel.CALLS)
            store.upsert_edge(y, x, Rel.CALLS)
    store.commit()
    ids = {u: store.id_for(u) for u in members["big"] + members["small"]}
    gv = GraphView(store)
    sg = gv.subgraph(list(ids.values()), depth=3)
    comms = gv.communities(sg)
    big = sorted(ids[u] for u in members["big"])
    small = sorted(ids[u] for u in members["small"])
    # exact, ordered: largest community first, then by sorted members (would FAIL on unsorted nx order).
    assert [sorted(c) for c in comms] == [big, small]
    assert gv.communities(sg) == comms                    # identical across runs
    store.close()


@graph
def test_centrality_scores_uses_pagerank_when_available():
    # R2-2: the primary ranker entry point's PageRank branch must actually run and be validated.
    store, ids = build()
    gv = GraphView(store)
    hub = ids["NotificationService"]
    scores = gv.centrality_scores([hub], depth=2)          # default prefer=pagerank, networkx present
    assert scores == pytest.approx(gv.pagerank(gv.subgraph([hub], depth=2)))
    assert scores[hub] == max(scores.values())
    # and it is the PageRank signal, not the degree fallback (the two differ on this graph).
    assert scores != pytest.approx(gv.centrality_scores([hub], depth=2, prefer="degree"))
    store.close()


def test_centrality_scores_prefer_validation_and_case():
    # R2-4: prefer is validated + case-insensitive (mirrors centrality's kind.lower()); both paths zero-dep.
    store, ids = build()
    gv = GraphView(store)
    with pytest.raises(ValueError, match="prefer"):
        gv.centrality_scores([ids["Job"]], prefer="bogus")
    assert gv.centrality_scores([ids["NotificationService"]], depth=2, prefer="DEGREE")  # case-folded
    store.close()


def test_centrality_scores_propagates_unrelated_import_error(monkeypatch):
    # R2-4: with networkx "available", an UNRELATED ImportError inside subgraph must propagate, not be
    # silently misread as "extra absent" and degraded to the fallback.
    monkeypatch.setattr(G, "_networkx_available", lambda: True)
    store, ids = build()
    gv = GraphView(store)
    monkeypatch.setattr(gv, "subgraph", lambda *a, **k: (_ for _ in ()).throw(ImportError("unrelated foo")))
    with pytest.raises(ImportError, match="unrelated"):
        gv.centrality_scores([ids["Job"]], depth=2)
    store.close()


@graph
def test_subgraph_collapses_parallel_relations_by_max_conf():
    # R2-5: the nx-DiGraph max-collapse branch of _add_or_max_edge (only the SQL path was covered).
    store = Store(":memory:")
    for u in ("a", "b"):
        store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    store.upsert_edge("a", "b", Rel.CALLS, confidence=0.3)
    store.upsert_edge("a", "b", Rel.IMPORTS, confidence=0.9)
    store.commit()
    a, b = store.id_for("a"), store.id_for("b")
    sg = GraphView(store).subgraph([a], depth=1)
    assert sg[a][b]["weight"] == pytest.approx(0.9)        # max confidence wins
    assert sg[a][b]["relation"] == Rel.IMPORTS
    store.close()


@graph
def test_whole_graph_build_betweenness_and_determinism():
    # R2-5: _global_graph successful build body (incl. P8-6 ORDER BY) runs to completion + is deterministic.
    store, ids = build()
    gv = GraphView(store)                                  # 7 nodes < path_ceiling
    cen = gv.centrality(None, kind="betweenness")          # builds the whole graph, runs Brandes
    assert cen[ids["NotificationService"]] == max(cen.values())
    assert gv.centrality(None, kind="betweenness") == cen  # identical across runs (ORDER BY pins it)
    store.close()


def test_subgraph_edges_by_id_integer_endpoints_and_filtering():
    # R2-5: the P8-3 by-id reader directly — integer endpoints, empty guard, out-of-set endpoint excluded.
    from memorydb import query as Q
    store = Store(":memory:")
    for u in ("a", "b", "c"):
        store.upsert_node(Node(uid=u, type="function", name=u, body=u))
    store.upsert_edge("a", "b", Rel.CALLS)
    store.upsert_edge("b", "c", Rel.CALLS)                 # c excluded from the id set -> edge dropped
    store.commit()
    a, b = store.id_for("a"), store.id_for("b")
    assert Q.subgraph_edges_by_id(store, []) == []
    rows = Q.subgraph_edges_by_id(store, [a, b])
    assert rows == [{"src": a, "dst": b, "relation": "CALLS", "confidence": 1.0}]
    assert all(isinstance(r["src"], int) and isinstance(r["dst"], int) for r in rows)
    store.close()


def test_centrality_scores_keys_are_int_node_ids():
    # Consumer fit (hybrid-ranker): scores keyed by int node_id on both the nx and fallback paths.
    store, ids = build()
    gv = GraphView(store)
    for scores in (gv.centrality_scores([ids["NotificationService"]], depth=2, prefer="degree"),):
        assert scores and all(isinstance(k, int) for k in scores)
        assert set(scores) <= set(ids.values())
    store.close()


def test_degree_centrality_raw_node_id_zero_is_first_class():
    # P8-9 nit: node id 0 is falsy; ensure it's treated as a real node, not skipped.
    out = _degree_centrality_raw([0, 1], [(0, 1)])
    assert out == {0: 1.0, 1: 1.0}
