"""Vector index tests (sqlite-vec-acceleration spec). The factory-fallback test is zero-dep and always
runs; the ANN tests are gated on the ``[vector]`` extra (skipped when sqlite-vec is absent)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import BruteForceVectorIndex, Node, SqliteVecIndex, Store, make_vector_index  # noqa: E402


def _seed(store, vectors):
    """Insert nodes + embeddings; returns {name: node_id}. Vectors are stored unit-normalized by
    set_embedding, matching both index backends."""
    ids = {}
    for name, vec in vectors.items():
        store.upsert_node(Node(uid=f"m.py::{name}", type="function", name=name))
        nid = store.id_for(f"m.py::{name}")
        store.set_embedding(nid, vec)
        ids[name] = nid
    store.commit()
    return ids


# --- zero-dep: factory fallback (always runs) --------------------------------

def test_factory_returns_searchable_index():
    store = Store(":memory:")
    idx = make_vector_index(store)
    assert hasattr(idx, "search")                       # always usable, ANN or brute force
    store.close()


def test_factory_prefer_ann_false_is_brute_force():
    store = Store(":memory:")
    assert isinstance(make_vector_index(store, prefer_ann=False), BruteForceVectorIndex)
    store.close()


def test_factory_falls_back_when_extension_unavailable(monkeypatch):
    # Simulate the [vector] extra being absent -> SqliteVecIndex.__init__ raises ImportError -> brute force
    import memorydb.vector as V
    real_init = V.SqliteVecIndex.__init__

    def boom(self, store, dim=None):
        raise ImportError("no sqlite_vec")
    monkeypatch.setattr(V.SqliteVecIndex, "__init__", boom)
    store = Store(":memory:")
    assert isinstance(make_vector_index(store), BruteForceVectorIndex)
    monkeypatch.setattr(V.SqliteVecIndex, "__init__", real_init)
    store.close()


# --- [vector] extra: real vec0 ANN (skipped if sqlite-vec absent/unloadable) --
# Gate per-test (NOT a module-level importorskip, which would also skip the zero-dep fallback tests
# above). The flag reflects actual loadability — import succeeding but the extension failing to load
# (platform / no enable_load_extension) skips cleanly rather than erroring.

def _ann_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        s = Store(":memory:")
        try:
            SqliteVecIndex(s)
            return True
        finally:
            s.close()
    except Exception:
        return False


ann = pytest.mark.skipif(not _ann_available(), reason="sqlite-vec extension not available/loadable")

_VECTORS = {
    "alpha": [1.0, 0.0, 0.0, 0.0],
    "beta":  [0.9, 0.1, 0.0, 0.0],
    "gamma": [0.0, 1.0, 0.0, 0.0],
    "delta": [0.0, 0.0, 1.0, 0.0],
}


@ann
def test_upsert_and_search():
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    _seed(store, _VECTORS)                               # set_embedding -> idx.upsert
    hits = idx.search([1.0, 0.0, 0.0, 0.0], k=2)
    names = [store.get_nodes([nid])[0]["name"] for _s, nid in hits]
    assert names[0] == "alpha" and "beta" in names      # nearest two to the alpha direction
    assert hits[0][0] > hits[1][0]                       # score descending
    store.close()


@ann
def test_vec0_knn_matches_bruteforce():
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    _seed(store, _VECTORS)
    q = [0.8, 0.2, 0.0, 0.0]
    ann_ids = [nid for _s, nid in idx.search(q, k=3)]
    brute = [nid for _s, nid in BruteForceVectorIndex(store).search(q, k=3)]
    assert ann_ids == brute                             # exact agreement on this small, separable set
    # scores comparable across backends (cosine both): top score within float slack
    a0 = idx.search(q, k=1)[0][0]
    b0 = BruteForceVectorIndex(store).search(q, k=1)[0][0]
    assert abs(a0 - b0) < 1e-4
    store.close()


@ann
def test_rebuild_from_blobs():
    store = Store(":memory:")
    _seed(store, _VECTORS)                               # embeddings written, no index attached yet
    idx = SqliteVecIndex(store)                          # fresh index, vec_items empty
    assert idx.search([1.0, 0.0, 0.0, 0.0], k=1) == []  # nothing indexed
    n = idx.rebuild_index()                              # repopulate from authoritative BLOBs
    assert n == 4
    assert store.get_nodes([idx.search([1.0, 0.0, 0.0, 0.0], k=1)[0][1]])[0]["name"] == "alpha"
    store.close()


@ann
def test_search_excludes_deleted_node():
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    ids = _seed(store, _VECTORS)
    store.conn.execute("DELETE FROM nodes WHERE id = ?", (ids["alpha"],))   # embeddings cascade; vec row stays
    store.commit()
    hits = idx.search([1.0, 0.0, 0.0, 0.0], k=4)
    assert ids["alpha"] not in [nid for _s, nid in hits]    # stale vec row filtered by the nodes join
    store.close()


@ann
def test_types_filter():
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    store.upsert_node(Node(uid="m.py::fn", type="function", name="fn"))
    store.upsert_node(Node(uid="m.py::Cls", type="class", name="Cls"))
    store.set_embedding(store.id_for("m.py::fn"), [1.0, 0.0, 0.0, 0.0])
    store.set_embedding(store.id_for("m.py::Cls"), [0.99, 0.01, 0.0, 0.0])
    store.commit()
    hits = idx.search([1.0, 0.0, 0.0, 0.0], k=5, types=["class"])
    names = [store.get_nodes([nid])[0]["name"] for _s, nid in hits]
    assert names == ["Cls"]                              # type filter applied (function excluded)
    store.close()


@ann
def test_dim_change_via_rebuild_not_wipe():
    """A single wrong-dim upsert is a NO-OP (must not drop the whole index — P5-3); a real dim change
    is adopted by rebuild_index() reading the new-dim BLOBs."""
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    for name in ("a", "b"):
        store.upsert_node(Node(uid=f"m.py::{name}", type="function", name=name))
        store.set_embedding(store.id_for(f"m.py::{name}"), [1.0, 0.0, 0.0, 0.0])   # dim 4
    store.commit()
    assert idx.dim == 4 and store.conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()[0] == 2
    # switch model: re-embed both at dim 2. The single upserts are wrong-dim NO-OPS, so the dim-4 index
    # is NOT wiped; only the BLOBs change to dim 2.
    for name in ("a", "b"):
        store.set_embedding(store.id_for(f"m.py::{name}"), [1.0, 0.0])             # dim 2
    store.commit()
    assert idx.dim == 4                                                  # intact, not wiped by a wrong-dim row
    # rebuild adopts the new prevailing dim from the authoritative BLOBs
    assert idx.rebuild_index() == 2 and idx.dim == 2
    hits = idx.search([1.0, 0.0], k=2)
    assert {store.get_nodes([nid])[0]["name"] for _s, nid in hits} == {"a", "b"}
    store.close()


@ann
def test_p5_1_delete_notifies_index_no_starvation():
    """Deleting a node must drop its vec_items row (Store.index_remove), so a stale row can't starve
    k-NN nor contaminate via node-id reuse (P5-1)."""
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    ids = _seed(store, _VECTORS)
    store.index_remove([ids["alpha"]])                                   # the hook indexer._delete_file calls
    store.conn.execute("DELETE FROM nodes WHERE id = ?", (ids["alpha"],))
    store.commit()
    assert store.conn.execute("SELECT COUNT(*) FROM vec_items WHERE node_id = ?",
                              (ids["alpha"],)).fetchone()[0] == 0          # vec row actually gone
    hits = idx.search([1.0, 0.0, 0.0, 0.0], k=2)                          # not starved: 2 live seeds
    assert len(hits) == 2 and ids["alpha"] not in [nid for _s, nid in hits]
    store.close()


@ann
def test_p5_2_rollback_self_heals_no_crash():
    """A transaction rollback that discards the lazily-created vec_items must not poison the index: the
    cached dim is stale, but search returns [] (not a crash) and the next upsert self-heals (P5-2)."""
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    store.upsert_node(Node(uid="m.py::a", type="function", name="a"))
    store.commit()
    nid = store.id_for("m.py::a")
    try:
        with store.transaction():
            store.set_embedding(nid, [1.0, 0.0, 0.0, 0.0])               # lazily CREATEs vec_items, caches dim
            raise RuntimeError("boom")                                   # -> rollback drops the table
    except RuntimeError:
        pass
    assert idx.dim == 4                                                  # cached dim is now stale vs the DB
    assert idx.search([1.0, 0.0, 0.0, 0.0], k=1) == []                   # no 'no such table' crash
    store.set_embedding(nid, [1.0, 0.0, 0.0, 0.0])                       # self-heals (re-ensures the table)
    store.commit()
    assert idx.search([1.0, 0.0, 0.0, 0.0], k=1)[0][1] == nid
    store.close()


@ann
def test_p5_4_orthogonal_score_clamped_to_zero():
    """An orthogonal stored vector scores exactly 0.0 (clamped from the float32 ~3e-8), so the planner's
    >1e-9 seed filter drops it like brute force does (P5-4)."""
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    store.upsert_node(Node(uid="m.py::o", type="function", name="o"))
    store.set_embedding(store.id_for("m.py::o"), [0.0, 1.0, 0.0, 0.0])   # orthogonal to the query
    store.commit()
    hits = idx.search([1.0, 0.0, 0.0, 0.0], k=5)
    assert hits and hits[0][0] == 0.0                                    # exact 0, not 3.4e-8
    store.close()


@ann
def test_p5_5_types_filter_not_starved():
    """A rare requested type far from the query must not be starved by many nearer 'noise' nodes of
    another type — search escalates the over-fetch beyond k*4 (P5-5)."""
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    for i in range(20):                                                  # 20 functions packed near the query
        store.upsert_node(Node(uid=f"m.py::n{i}", type="function", name=f"n{i}"))
        store.set_embedding(store.id_for(f"m.py::n{i}"), [1.0, 0.001 * i, 0.0, 0.0])
    for i in range(2):                                                   # 2 classes farther away
        store.upsert_node(Node(uid=f"m.py::W{i}", type="class", name=f"W{i}"))
        store.set_embedding(store.id_for(f"m.py::W{i}"), [0.5, 0.5, 0.0, 0.0])
    store.commit()
    hits = idx.search([1.0, 0.0, 0.0, 0.0], k=2, types=["class"])        # k*4=8 nearest are all functions
    assert sorted(store.get_nodes([nid])[0]["name"] for _s, nid in hits) == ["W0", "W1"]
    store.close()


@ann
def test_p5_rebuild_picks_majority_dim():
    store = Store(":memory:")
    idx = SqliteVecIndex(store)                                          # not attached: set_embedding writes BLOBs only
    for i in range(3):
        store.upsert_node(Node(uid=f"m.py::a{i}", type="function", name=f"a{i}"))
        store.set_embedding(store.id_for(f"m.py::a{i}"), [1.0, 0.0, 0.0, 0.0])      # dim 4 (majority)
    store.upsert_node(Node(uid="m.py::stray", type="function", name="stray"))
    store.set_embedding(store.id_for("m.py::stray"), [1.0, 0.0])                    # dim 2 (stray)
    store.commit()
    assert idx.rebuild_index() == 3 and idx.dim == 4                     # majority dim, stray skipped
    store.close()


@ann
def test_p5r_large_k_no_silent_empty():
    """A k whose k*4 over-fetch exceeds vec0's hard 4096 KNN cap must not raise-and-swallow into [] —
    the over-fetch is capped at 4096 (re-review regression of P5-1/P5-5)."""
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    for i in range(1100):                                       # k=1025 -> k*4=4100 > 4096 before the cap
        store.upsert_node(Node(uid=f"m::n{i}", type="function", name=f"n{i}"))
        v = [0.0] * 8
        v[i % 8] = 1.0
        store.set_embedding(store.id_for(f"m::n{i}"), v)
    store.commit()
    assert len(idx.search([1.0, 0, 0, 0, 0, 0, 0, 0], k=1025)) == 1025   # not silently empty (was 0)
    store.close()


@ann
def test_p5r_clamp_scales_above_dense_noise_floor():
    """The snap-to-zero floor scales above the dense-orthogonal float32 noise (~1.2e-6 at dim>=768), so
    the orthogonal phantom-seed leak stays closed at high dim where a fixed 1e-6 floor leaked (P5-4)."""
    import math
    import random
    from memorydb.vector import _score_floor
    assert _score_floor(1024) > 1.2e-6                          # above the measured dense noise floor
    random.seed(11)
    dim = 768

    def _dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    a = [random.gauss(0, 1) for _ in range(dim)]
    na = math.sqrt(_dot(a, a)); a = [x / na for x in a]
    b = [random.gauss(0, 1) for _ in range(dim)]
    d = _dot(b, a); b = [x - d * ai for x, ai in zip(b, a)]     # Gram-Schmidt: b ⟂ a, dense
    nb = math.sqrt(_dot(b, b)); b = [x / nb for x in b]
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    store.upsert_node(Node(uid="m::o", type="function", name="o"))
    store.set_embedding(store.id_for("m::o"), b)
    store.set_embedding(store.id_for("m::o"), b)
    store.commit()
    assert idx.search(a, k=1)[0][0] == 0.0                      # orthogonal -> exact 0 (planner drops it)
    store.close()


@ann
def test_p5_facade_exposes_rebuild():
    import warnings
    from memorydb import MemoryDB
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        db = MemoryDB.open(":memory:")
    assert db.rebuild_vector_index() == 0                                # callable backstop, empty -> 0
    db.close()


@ann
def test_factory_uses_ann_when_available():
    store = Store(":memory:")
    assert isinstance(make_vector_index(store), SqliteVecIndex)          # the extra is installed here
    store.close()
