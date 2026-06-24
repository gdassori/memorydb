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
def test_dim_change_rebuild():
    store = Store(":memory:")
    idx = SqliteVecIndex(store)
    store.attach_index(idx)
    store.upsert_node(Node(uid="m.py::a", type="function", name="a"))
    store.set_embedding(store.id_for("m.py::a"), [1.0, 0.0, 0.0, 0.0])   # dim 4
    store.commit()
    assert idx.dim == 4
    store.upsert_node(Node(uid="m.py::b", type="function", name="b"))
    store.set_embedding(store.id_for("m.py::b"), [1.0, 0.0])             # dim 2 -> recreate at new dim
    store.commit()
    assert idx.dim == 2
    hits = idx.search([1.0, 0.0], k=5)                                   # only the dim-2 vector survives
    assert [store.get_nodes([nid])[0]["name"] for _s, nid in hits] == ["b"]
    store.close()


@ann
def test_factory_uses_ann_when_available():
    store = Store(":memory:")
    assert isinstance(make_vector_index(store), SqliteVecIndex)          # the extra is installed here
    store.close()
