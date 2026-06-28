"""HybridRanker tests (hybrid-ranker spec, TD-006/TD-007).

Zero-dep: ``HashingEmbedder`` + the degree-centrality fallback. Most tests force the degree path
(``_networkx_available -> False``) so they are deterministic and identical with or without the ``[graph]``
extra installed; one ``@graph``-gated test checks the real PageRank path. Recency is pinned via ``now=``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import HashingEmbedder, HybridRanker, Node, RankWeights, Rel, Store  # noqa: E402
from memorydb import graph as G  # noqa: E402

_NOW = 1_700_000_000.0
_DAY = 86_400.0


@pytest.fixture
def force_degree(monkeypatch):
    """Force the zero-dep degree-centrality path so tests don't depend on whether networkx is installed."""
    monkeypatch.setattr(G, "_networkx_available", lambda: False)


def _have_networkx() -> bool:
    try:
        import networkx  # noqa: F401
        return True
    except ImportError:
        return False


graph = pytest.mark.skipif(not _have_networkx(), reason="[graph] extra (networkx) not installed")


def _emb(store, uid, text, dim=64):
    store.set_embedding(store.id_for(uid), HashingEmbedder(dim=dim).embed([text])[0], model="hashing")


# A hub called by Job, calling three leaves; one file with a known mtime.
def build(mtime=_NOW - _DAY):
    store = Store(":memory:")
    bodies = {
        "Job": "job triggers mass notifications",
        "send": "send a notification to a user via the queue",
        "q": "redis backed queue",
        "p": "push provider gateway",
        "log": "persistent notification log",
    }
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": mtime}))
    for uid, body in bodies.items():
        store.upsert_node(Node(uid=uid, type="function", name=uid, body=body, attrs={"file_uid": "f.py"}))
    for s, d in (("Job", "send"), ("send", "q"), ("send", "p"), ("send", "log")):
        store.upsert_edge(s, d, Rel.CALLS)
    for uid, body in bodies.items():
        _emb(store, uid, body)
    store.commit()
    ids = {uid: store.id_for(uid) for uid in bodies}
    emb = HashingEmbedder(dim=64)
    return store, ids, emb


# --- core scoring -------------------------------------------------------------------------------------

def test_hub_outranks_leaf_on_centrality(force_degree):
    # Hold vector/confidence/recency equal (identical body+file+edge-conf), so only centrality decides.
    store = Store(":memory:")
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": _NOW - _DAY}))
    for uid in ("H", "L", "x", "y", "z"):
        store.upsert_node(Node(uid=uid, type="function", name=uid, body="same body text",
                               attrs={"file_uid": "f.py"}))
    # H is a hub (degree 3); L is a leaf (degree 1); all edges confidence 1.0 so mean-incident conf ties.
    for d in ("x", "y", "z"):
        store.upsert_edge("H", d, Rel.CALLS)
    store.upsert_edge("L", "x", Rel.CALLS)
    for uid in ("H", "L", "x", "y", "z"):
        _emb(store, uid, "same body text")
    store.commit()
    H, L = store.id_for("H"), store.id_for("L")
    emb = HashingEmbedder(dim=64)
    scored = HybridRanker(store).rank([H, L], emb.embed(["same body text"])[0], depth=2, now=_NOW)
    by_id = {s.node_id: s for s in scored}
    assert by_id[H].breakdown["vector"] == pytest.approx(by_id[L].breakdown["vector"])      # equal cosine
    assert by_id[H].breakdown["confidence"] == pytest.approx(by_id[L].breakdown["confidence"])  # equal conf
    assert by_id[H].breakdown["recency"] == pytest.approx(by_id[L].breakdown["recency"])    # equal recency
    assert by_id[H].breakdown["centrality"] > by_id[L].breakdown["centrality"]              # hub wins here
    assert scored[0].node_id == H


def test_recency_breaks_ties(force_degree):
    # Two structurally/semantically identical nodes in different-mtime files: the newer file wins.
    store = Store(":memory:")
    store.upsert_node(Node(uid="new.py", type="file", name="new.py", body="", attrs={"mtime": _NOW - _DAY}))
    store.upsert_node(Node(uid="old.py", type="file", name="old.py", body="", attrs={"mtime": _NOW - 100 * _DAY}))
    store.upsert_node(Node(uid="A", type="function", name="A", body="identical", attrs={"file_uid": "new.py"}))
    store.upsert_node(Node(uid="B", type="function", name="B", body="identical", attrs={"file_uid": "old.py"}))
    _emb(store, "A", "identical")
    _emb(store, "B", "identical")
    store.commit()
    A, B = store.id_for("A"), store.id_for("B")
    emb = HashingEmbedder(dim=64)
    scored = HybridRanker(store).rank([A, B], emb.embed(["identical"])[0], depth=2, now=_NOW)
    by_id = {s.node_id: s for s in scored}
    assert by_id[A].breakdown["vector"] == pytest.approx(by_id[B].breakdown["vector"])
    assert by_id[A].breakdown["centrality"] == pytest.approx(by_id[B].breakdown["centrality"])  # both neutral
    assert by_id[A].breakdown["recency"] > by_id[B].breakdown["recency"]                       # newer wins
    assert scored[0].node_id == A


def test_breakdown_sums_to_score(force_degree):
    store, ids, emb = build()
    scored = HybridRanker(store).rank(list(ids.values()), emb.embed(["how do notifications work"])[0], now=_NOW)
    assert scored
    for s in scored:
        assert s.score == pytest.approx(sum(s.breakdown.values()))
        assert set(s.breakdown) == {"vector", "centrality", "confidence", "recency"}


def test_missing_signals_are_safe(force_degree):
    # A node with NO embedding and NO file/mtime must not crash: vector 0, recency neutral 0.5.
    store = Store(":memory:")
    store.upsert_node(Node(uid="bare", type="function", name="bare", body="no embedding no file"))
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": _NOW - _DAY}))
    store.upsert_node(Node(uid="rich", type="function", name="rich", body="rich", attrs={"file_uid": "f.py"}))
    _emb(store, "rich", "rich")
    store.commit()
    bare, rich = store.id_for("bare"), store.id_for("rich")
    emb = HashingEmbedder(dim=64)
    scored = HybridRanker(store).rank([bare, rich], emb.embed(["rich"])[0], now=_NOW)
    by_id = {s.node_id: s for s in scored}
    assert by_id[bare].breakdown["vector"] == 0.0                       # no embedding -> 0
    assert by_id[bare].breakdown["recency"] == pytest.approx(0.15 * 0.5)  # no mtime -> neutral 0.5 * weight
    assert len(scored) == 2 and {s.node_id for s in scored} == {bare, rich}


def test_deterministic_order(force_degree):
    store, ids, emb = build()
    qv = emb.embed(["notifications"])[0]
    r = HybridRanker(store)
    a = [s.node_id for s in r.rank(list(ids.values()), qv, now=_NOW)]
    b = [s.node_id for s in r.rank(list(ids.values()), qv, now=_NOW)]
    assert a == b


def test_single_candidate(force_degree):
    store, ids, emb = build()
    scored = HybridRanker(store).rank([ids["send"]], emb.embed(["x"])[0], now=_NOW)
    assert len(scored) == 1 and scored[0].node_id == ids["send"]
    assert scored[0].breakdown["centrality"] == pytest.approx(0.25 * 0.5)   # min-max single -> neutral 0.5


def test_all_equal_scores_stable_by_id(force_degree):
    # Identical nodes (same body, file, no edges) -> equal scores -> stable order by node_id.
    store = Store(":memory:")
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": _NOW - _DAY}))
    for uid in ("n1", "n2", "n3"):
        store.upsert_node(Node(uid=uid, type="function", name=uid, body="same", attrs={"file_uid": "f.py"}))
        _emb(store, uid, "same")
    store.commit()
    ids = [store.id_for(u) for u in ("n1", "n2", "n3")]
    emb = HashingEmbedder(dim=64)
    order = [s.node_id for s in HybridRanker(store).rank(ids, emb.embed(["same"])[0], now=_NOW)]
    assert order == sorted(ids)                          # all-equal -> ascending node_id tie-break


def test_empty_candidates():
    store, _, emb = build()
    assert HybridRanker(store).rank([], emb.embed(["x"])[0]) == []


# --- weights ------------------------------------------------------------------------------------------

def test_weights_normalize_when_sum_not_one(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="memorydb.ranker"):
        w = RankWeights(vector=2.0, centrality=1.0, confidence=1.0, recency=1.0)   # sum 5
    assert w.vector + w.centrality + w.confidence + w.recency == pytest.approx(1.0)
    assert w.vector == pytest.approx(0.4)                # 2/5
    assert "normaliz" in caplog.text.lower()


def test_weights_reject_nonpositive_sum():
    with pytest.raises(ValueError):
        RankWeights(vector=0.0, centrality=0.0, confidence=0.0, recency=0.0)


def test_weights_change_ranking(force_degree):
    # vector-only weights -> ranking is pure cosine order (centrality/conf/recency contribute nothing).
    store, ids, emb = build()
    qv = emb.embed(["redis backed queue"])[0]            # most similar to the "q" leaf
    w = RankWeights(vector=1.0, centrality=0.0, confidence=0.0, recency=0.0)
    scored = HybridRanker(store, weights=w).rank(list(ids.values()), qv, now=_NOW)
    assert scored[0].node_id == ids["q"]                 # the semantically-closest node wins outright
    assert all(s.breakdown["centrality"] == 0.0 for s in scored)


# --- [graph] path -------------------------------------------------------------------------------------

@graph
def test_pagerank_path_ranks_hub():
    # Without forcing the fallback, centrality comes from real PageRank — the hub still ranks top.
    store, ids, emb = build()
    scored = HybridRanker(store).rank(list(ids.values()), emb.embed(["how do notifications work"])[0], now=_NOW)
    assert scored[0].node_id == ids["send"]
    assert scored[0].breakdown["centrality"] == pytest.approx(0.25)   # hub = max -> min-max 1.0 * weight


# --- planner / context integration --------------------------------------------------------------------

def test_planner_explain_emits_ranking(force_degree):
    from memorydb import RetrievalPlanner
    store, ids, emb = build()
    res = RetrievalPlanner(store, emb).explain("how do notifications work", depth=2)
    assert res["intent"] == "EXPLAIN"
    assert "ranking" in res and set(res["ranking"]) == set(r["id"] for r in res["nodes"])  # every node ranked
    assert [s["node_id"] for s in res["scored"]] == res["ranking"]                          # aligned
    assert all(set(s["breakdown"]) == {"vector", "centrality", "confidence", "recency"} for s in res["scored"])


def test_context_builder_prefers_ranking(force_degree):
    from memorydb import ContextBuilder, RetrievalPlanner
    store, ids, emb = build()
    res = RetrievalPlanner(store, emb).explain("how do notifications work", depth=2)
    # Force a known order and confirm the builder packs the first-ranked node's card first.
    res["ranking"] = [ids["log"], ids["send"], ids["q"], ids["p"], ids["Job"]]
    ctx = ContextBuilder().build(res, budget_tokens=10_000)
    assert ctx.uids and ctx.uids[0] == "log"            # the builder honored result["ranking"]
