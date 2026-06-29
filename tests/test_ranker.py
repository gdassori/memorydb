"""HybridRanker tests (hybrid-ranker spec, TD-006/TD-007).

Zero-dep: ``HashingEmbedder`` + the degree-centrality fallback. Most tests force the degree path
(``_networkx_available -> False``) so they are deterministic and identical with or without the ``[graph]``
extra installed; one ``@graph``-gated test checks the real PageRank path. Recency is pinned via ``now=``.
"""
from __future__ import annotations

import math
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

def _hub_fixture():
    """An IN-degree hub: H is called by c1/c2/c3, L by c1 only. H dominates centrality under BOTH degree
    AND real PageRank (PageRank elevates *called-by-many*, not *calls-many* — P9-1). Vector/confidence/
    recency are held equal (identical body+file, all edges conf 1.0) so only centrality decides."""
    store = Store(":memory:")
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": _NOW - _DAY}))
    for uid in ("H", "L", "c1", "c2", "c3"):
        store.upsert_node(Node(uid=uid, type="function", name=uid, body="same body text",
                               attrs={"file_uid": "f.py"}))
    for c in ("c1", "c2", "c3"):
        store.upsert_edge(c, "H", Rel.CALLS)        # H called by 3 -> high in-degree & PageRank
    store.upsert_edge("c1", "L", Rel.CALLS)         # L called by 1
    for uid in ("H", "L", "c1", "c2", "c3"):
        _emb(store, uid, "same body text")
    store.commit()
    return store, store.id_for("H"), store.id_for("L")


def _assert_hub_wins_on_centrality(store, H, L):
    emb = HashingEmbedder(dim=64)
    scored = HybridRanker(store).rank([H, L], emb.embed(["same body text"])[0], depth=2, now=_NOW)
    by_id = {s.node_id: s for s in scored}
    assert by_id[H].breakdown["vector"] == pytest.approx(by_id[L].breakdown["vector"])          # equal cosine
    assert by_id[H].breakdown["confidence"] == pytest.approx(by_id[L].breakdown["confidence"])   # equal conf
    assert by_id[H].breakdown["recency"] == pytest.approx(by_id[L].breakdown["recency"])         # equal recency
    assert by_id[H].breakdown["centrality"] > by_id[L].breakdown["centrality"]                   # only diff
    assert scored[0].node_id == H


def test_hub_outranks_leaf_on_centrality_degree(force_degree):
    store, H, L = _hub_fixture()
    _assert_hub_wins_on_centrality(store, H, L)         # zero-dep degree-centrality path


@graph
def test_hub_outranks_leaf_on_centrality_pagerank():
    # SAME fixture on the REAL PageRank path (no force_degree): the in-degree hub still dominates
    # (regression for P9-1 — the old out-only hub tied the leaf under PageRank).
    store, H, L = _hub_fixture()
    _assert_hub_wins_on_centrality(store, H, L)


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
    # Feed in DESCENDING id order: a stable sort would keep that order, so passing only if the result is
    # ascending proves the explicit node_id tie-break actually fires (P9-2 — the old ascending input was a
    # tautology that survived deleting the tie-break).
    order = [s.node_id for s in HybridRanker(store).rank(sorted(ids, reverse=True), emb.embed(["same"])[0], now=_NOW)]
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
    res["ranking"] = [ids["log"], ids["send"], ids["q"], ids["p"], ids["Job"]]    # forced order
    ranked = ContextBuilder().build(res, budget_tokens=10_000)
    # Assert the FULL order is honored (not just the head — the proxy also heads with 'log', so a
    # head-only check couldn't distinguish ranking from proxy — P9-3).
    assert ranked.uids == ["log", "send", "q", "p", "Job"]
    # ...and it genuinely diverges from the seed/depth proxy (drop ranking -> the fallback path).
    res.pop("ranking"); res.pop("scored", None)
    proxy = ContextBuilder().build(res, budget_tokens=10_000)
    assert proxy.uids != ranked.uids                     # the ranking branch actually changed the order


def test_planner_degrades_to_unranked_on_ranker_error(force_degree):
    # P9-13/F10: a ranker hiccup must NOT break EXPLAIN — it degrades to an unranked result, no exception.
    from memorydb import RetrievalPlanner

    class BoomRanker:
        def rank(self, *a, **k):
            raise RuntimeError("kaboom")

    store, ids, emb = build()
    res = RetrievalPlanner(store, emb, ranker=BoomRanker()).explain("how do notifications work", depth=2)
    assert res["intent"] == "EXPLAIN" and res["nodes"]
    assert "ranking" not in res and "scored" not in res   # degraded cleanly


# --- P9-13: signal-extractor coverage gaps ------------------------------------------------------------

def test_confidence_varies_with_edge_confidence(force_degree):
    # A node touched only by high-confidence edges outscores one touched only by low-confidence edges.
    store = Store(":memory:")
    for uid in ("hi", "lo", "x", "y"):
        store.upsert_node(Node(uid=uid, type="function", name=uid, body="b"))
    store.upsert_edge("x", "hi", Rel.CALLS, confidence=0.9)     # incoming edge to `hi`
    store.upsert_edge("y", "lo", Rel.CALLS, confidence=0.2)     # incoming edge to `lo`
    store.commit()
    hi, lo = store.id_for("hi"), store.id_for("lo")
    scored = {s.node_id: s for s in HybridRanker(store).rank([hi, lo], [0.0] * 64, now=_NOW)}
    assert scored[hi].breakdown["confidence"] == pytest.approx(0.15 * 0.9)   # mean of one 0.9 edge
    assert scored[lo].breakdown["confidence"] == pytest.approx(0.15 * 0.2)   # dst-arm picks up incoming edge
    store.close()


def test_half_life_shapes_recency(force_degree):
    # age == half_life -> recency exp(-1); a shorter half-life decays an old file harder.
    store = Store(":memory:")
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": _NOW - 30 * _DAY}))
    store.upsert_node(Node(uid="n", type="function", name="n", body="b", attrs={"file_uid": "f.py"}))
    store.commit()
    n = store.id_for("n")
    long_hl = HybridRanker(store, half_life_days=30.0).rank([n], [0.0] * 64, now=_NOW)[0]
    short_hl = HybridRanker(store, half_life_days=5.0).rank([n], [0.0] * 64, now=_NOW)[0]
    assert long_hl.breakdown["recency"] == pytest.approx(0.15 * math.exp(-1.0))   # age 30d / hl 30d
    assert short_hl.breakdown["recency"] < long_hl.breakdown["recency"]            # shorter hl -> more decay
    store.close()


def test_recency_uses_symbol_denormalized_mtime(force_degree):
    # P9-13: the COALESCE *first* arm — a symbol's own attrs.mtime (no file_uid) — must be honored.
    store = Store(":memory:")
    store.upsert_node(Node(uid="n", type="function", name="n", body="b", attrs={"mtime": _NOW - _DAY}))
    store.commit()
    n = store.id_for("n")
    rec = HybridRanker(store).rank([n], [0.0] * 64, now=_NOW)[0].breakdown["recency"]
    assert rec == pytest.approx(0.15 * math.exp(-1.0 / 30.0))   # 1-day age via the node's OWN mtime
    store.close()


def test_negative_cosine_clamped_to_zero(force_degree):
    # A candidate whose stored embedding is anti-correlated with the query -> vector contribution 0.
    store = Store(":memory:")
    store.upsert_node(Node(uid="anti", type="function", name="anti", body="b"))
    store.commit()
    anti = store.id_for("anti")
    store.set_embedding(anti, [-1.0, 0.0, 0.0, 0.0], model="manual")   # opposite of the query below
    store.commit()
    scored = HybridRanker(store).rank([anti], [1.0, 0.0, 0.0, 0.0], now=_NOW)[0]
    assert scored.breakdown["vector"] == 0.0           # cosine -1 clamped to 0
    store.close()


def test_dedupe_and_dim_mismatch(force_degree):
    store, ids, emb = build()
    gv = HybridRanker(store)
    # duplicate candidate ids collapse to one Scored
    dup = gv.rank([ids["send"], ids["send"]], emb.embed(["x"])[0], now=_NOW)
    assert len(dup) == 1 and dup[0].node_id == ids["send"]
    # a wrong-dim query vector scores vector 0 for every candidate (no crash, no zip-truncation garbage)
    wrong = gv.rank(list(ids.values()), [0.1] * 999, now=_NOW)
    assert all(s.breakdown["vector"] == 0.0 for s in wrong)
    store.close()


def test_default_now_is_corpus_mtime_and_reproducible(force_degree):
    # P9-6: with no `now=`, recency is corpus-relative (newest file -> 1.0) and reproducible across calls.
    store = Store(":memory:")
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": _NOW}))
    store.upsert_node(Node(uid="n", type="function", name="n", body="b", attrs={"file_uid": "f.py"}))
    store.commit()
    n = store.id_for("n")
    r = HybridRanker(store)
    a = r.rank([n], [0.0] * 64)        # no now= -> defaults to corpus max mtime (_NOW)
    b = r.rank([n], [0.0] * 64)
    assert a[0].breakdown["recency"] == pytest.approx(0.15 * 1.0)   # newest file, age 0 -> recency 1.0
    assert a[0].score == pytest.approx(b[0].score)                  # reproducible, not wall-clock-dependent
    store.close()


def test_rank_does_not_crash_on_malformed_mtime(force_degree):
    # P9-8: a non-numeric attrs.mtime degrades that node to neutral recency, never raises.
    store = Store(":memory:")
    store.upsert_node(Node(uid="f.py", type="file", name="f.py", body="", attrs={"mtime": "not-a-date"}))
    store.upsert_node(Node(uid="n", type="function", name="n", body="b", attrs={"file_uid": "f.py"}))
    store.commit()
    n = store.id_for("n")
    scored = HybridRanker(store).rank([n], [0.0] * 64, now=_NOW)
    assert scored[0].breakdown["recency"] == pytest.approx(0.15 * 0.5)   # malformed -> neutral 0.5
    store.close()


def test_rankweights_rejects_negative_component():
    # P9-12: a negative weight (would penalize a signal) is rejected by the pydantic ge=0 bound.
    with pytest.raises(Exception):     # pydantic ValidationError (a ValueError subclass)
        RankWeights(vector=1.0, centrality=-0.5, confidence=0.3, recency=0.2)


def test_scored_is_pydantic_model_dumpable(force_degree):
    # P9-7: Scored is a pydantic BaseModel (model_dump) consistent with the rest of the codebase.
    store, ids, emb = build()
    s = HybridRanker(store).rank([ids["send"]], emb.embed(["x"])[0], now=_NOW)[0]
    d = s.model_dump()
    assert set(d) == {"node_id", "score", "breakdown"} and d["node_id"] == ids["send"]
    store.close()
