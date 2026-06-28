"""GraphView tests (graph-algorithms-networkx spec, TD-003).

Zero-dep tests (pure-Python degree fallback + the ImportError gate) run with core deps only. The NetworkX
path is gated per-test with ``@graph`` (NOT a module-level importorskip — that would also skip the zero-dep
tests). Mirrors the per-test gating convention in test_vector_index.py.
"""
from __future__ import annotations

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


def test_networkx_absent_gates_only_the_nx_methods(monkeypatch):
    # Simulate the [graph] extra missing: nx methods raise a clear ImportError; degree stays zero-dep.
    def boom():
        raise ImportError("no networkx")
    monkeypatch.setattr(G, "_require_networkx", boom)
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
    store.close()


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
def test_whole_graph_pagerank_and_ceiling_degrade():
    store, ids = build()
    # sg=None -> whole-graph PageRank (under the ceiling) ranks the hub on top.
    pr = GraphView(store).pagerank()
    assert pr[ids["NotificationService"]] == max(pr.values())
    # a tiny ceiling forces the cheap degree-centrality degrade (no exception, a warning + degree scores).
    degraded = GraphView(store, node_ceiling=2).pagerank()
    assert degraded == pytest.approx(GraphView(store)._global_degree())
    store.close()


@graph
def test_global_graph_raises_above_ceiling():
    store, _ = build()
    with pytest.raises(ValueError, match="ceiling"):
        GraphView(store, node_ceiling=2)._global_graph()
    store.close()
