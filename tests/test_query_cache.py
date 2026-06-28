"""QueryEmbeddingCache tests (TD-011) — zero-dep."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import MemoryDB, Node, QueryEmbeddingCache, RetrievalPlanner, Store  # noqa: E402


class _CountingEmbedder:
    """Deterministic embedder that counts embed() calls, so a cache hit is observable."""
    model = "counting-v1"
    dim = 4

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return [[float(len(t)), 1.0, 0.0, 0.0] for t in texts]


# --- the cache unit ----------------------------------------------------------

def test_get_put_and_sha256_keyed():
    c = QueryEmbeddingCache("m", dim=3)
    assert c.get("hello") is None
    c.put("hello", [1.0, 2.0, 3.0])
    assert c.get("hello") == [1.0, 2.0, 3.0]
    # keyed by sha256(query): a different query misses; identical query hits
    assert c.get("HELLO") is None and len(c) == 1


def test_put_is_rewritable():
    c = QueryEmbeddingCache("m", dim=2)
    c.put("q", [1.0, 0.0])
    c.put("q", [0.0, 1.0])                           # overwrite
    assert c.get("q") == [0.0, 1.0] and len(c) == 1


def test_wrong_dim_put_adopts_real_dim():
    # the REAL embedding's length wins over a stale advertised/loaded dim (T11-5/6): adopt + drop stale
    c = QueryEmbeddingCache("m", dim=2)
    c.put("old", [1.0, 2.0])
    c.put("q", [1.0, 2.0, 3.0])
    assert c.dim == 3 and c.get("q") == [1.0, 2.0, 3.0] and c.get("old") is None


def test_reconcile_clears_on_model_or_dim_mismatch():
    c = QueryEmbeddingCache("model-A", dim=4)
    c.put("q", [1.0, 2.0, 3.0, 4.0])
    c.reconcile("model-A", 4)                        # same identity -> kept
    assert c.get("q") is not None
    c.reconcile("model-B", 4)                        # different model -> cleared + re-tagged
    assert len(c) == 0 and c.model_id == "model-B"
    c.put("q", [9.0, 9.0, 9.0, 9.0])
    c.reconcile("model-B", 8)                        # different dim -> cleared
    assert len(c) == 0 and c.dim == 8


def test_bounded_oldest_evicted():
    c = QueryEmbeddingCache("m", dim=1, max_entries=3)
    for i in range(5):
        c.put(f"q{i}", [float(i)])
    assert len(c) == 3 and c.get("q0") is None and c.get("q4") == [4.0]   # oldest gone, newest kept


def test_clear():
    c = QueryEmbeddingCache("m", dim=1)
    c.put("q", [1.0])
    c.clear()
    assert len(c) == 0 and c.get("q") is None


# --- binary dump / load ------------------------------------------------------

def test_dump_load_roundtrip(tmp_path):
    c = QueryEmbeddingCache("model-x", dim=4)
    for i in range(10):
        c.put(f"query {i}", [float(i), 0.5, -1.0, 2.0])
    p = str(tmp_path / "cache.mqec")
    assert c.dump(p) == 10
    c2 = QueryEmbeddingCache("model-x", dim=4)
    assert c2.load(p) == 10
    assert c2.get("query 7") == c.get("query 7") and c2.get("query 7") is not None


def test_load_rejects_wrong_model(tmp_path):
    c = QueryEmbeddingCache("model-a", dim=2)
    c.put("q", [1.0, 2.0])
    p = str(tmp_path / "c.mqec")
    c.dump(p)
    other = QueryEmbeddingCache("model-b", dim=2)          # different model
    assert other.load(p) == 0 and len(other) == 0          # ignored, never cross-model


def test_load_rejects_wrong_dim(tmp_path):
    c = QueryEmbeddingCache("m", dim=4)
    c.put("q", [1.0, 2.0, 3.0, 4.0])
    p = str(tmp_path / "c.mqec")
    c.dump(p)
    other = QueryEmbeddingCache("m", dim=2)
    assert other.load(p) == 0


def test_load_merges_keeps_live_entries(tmp_path):
    c = QueryEmbeddingCache("m", dim=2)
    c.put("alpha", [1.0, 2.0])
    p = str(tmp_path / "c.mqec")
    c.dump(p)
    c.put("beta", [3.0, 4.0])                        # live, not in the dump
    other = QueryEmbeddingCache("m", dim=2)
    other.put("gamma", [5.0, 6.0])                   # a live entry already in the target cache
    assert other.load(p) == 1                        # one record merged in
    assert other.get("alpha") == [1.0, 2.0] and other.get("gamma") == [5.0, 6.0]   # both kept


def test_load_rejects_same_length_bitflip(tmp_path):
    c = QueryEmbeddingCache("m", dim=2)
    c.put("q", [1.0, 2.0])
    p = str(tmp_path / "c.mqec")
    c.dump(p)
    data = bytearray(Path(p).read_bytes())
    data[-1] ^= 0xFF                                 # flip a byte in the last float (same length)
    Path(p).write_bytes(data)
    assert QueryEmbeddingCache("m", dim=2).load(p) == 0   # CRC32 catches it


def test_dump_is_little_endian(tmp_path):
    import struct as _s
    c = QueryEmbeddingCache("m", dim=1)
    c.put("q", [1.0])
    p = str(tmp_path / "c.mqec")
    c.dump(p)
    blob = Path(p).read_bytes()
    assert blob[-4:] == _s.pack("<f", 1.0)          # vector payload is little-endian on disk


def test_surrogate_query_does_not_crash():
    c = QueryEmbeddingCache("m", dim=1)
    c.put("bad\udc80surrogate", [1.0])              # lone surrogate must not raise
    assert c.get("bad\udc80surrogate") == [1.0]


def test_dump_cleans_up_tmp_on_error(tmp_path):
    import pytest
    c = QueryEmbeddingCache("m", dim=2)
    c.put("q", [1.0, 2.0])
    target = tmp_path / "adir"
    target.mkdir()                                  # path is a directory -> os.replace fails
    with pytest.raises(Exception):
        c.dump(str(target))
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".mqec-")]
    assert leftovers == []                          # no orphaned temp file


def test_model_id_too_long_raises(tmp_path):
    import pytest
    c = QueryEmbeddingCache("x" * 70000, dim=1)
    c.put("q", [1.0])
    with pytest.raises(ValueError):
        c.dump(str(tmp_path / "c.mqec"))


def test_load_missing_or_corrupt_is_ignored(tmp_path):
    c = QueryEmbeddingCache("m", dim=2)
    assert c.load(str(tmp_path / "nope.mqec")) == 0       # missing -> 0, no crash
    bad = tmp_path / "bad.mqec"
    bad.write_bytes(b"NOTMQEC garbage")
    assert c.load(str(bad)) == 0                           # bad magic -> 0
    # truncated: dump then chop a byte
    c.put("q", [1.0, 2.0]); p = str(tmp_path / "c.mqec"); c.dump(p)
    data = Path(p).read_bytes()
    Path(p).write_bytes(data[:-1])
    assert QueryEmbeddingCache("m", dim=2).load(p) == 0    # truncated -> ignored


# --- planner / facade integration --------------------------------------------

def test_planner_caches_query_embedding():
    store = Store(":memory:")
    store.upsert_node(Node(uid="a.py::f", type="function", name="f"))
    nid = store.id_for("a.py::f")
    emb = _CountingEmbedder()
    store.set_embedding(nid, emb.embed(["f"])[0]); store.commit()
    emb.calls = 0
    planner = RetrievalPlanner(store, emb)
    planner.explain("same question")
    planner.explain("same question")                      # cache hit -> no re-embed
    assert emb.calls == 1
    planner.explain("a different question")
    assert emb.calls == 2                                  # distinct query -> one more embed
    store.close()


def test_planner_reconciles_injected_cross_model_cache():
    """An injected/shared cache tagged for a DIFFERENT model must be reconciled (cleared) before use, so
    explain() never serves a cross-model query vector (T11-1)."""
    store = Store(":memory:")
    store.upsert_node(Node(uid="a.py::f", type="function", name="f"))
    nid = store.id_for("a.py::f")
    emb = _CountingEmbedder()
    store.set_embedding(nid, emb.embed(["f"])[0]); store.commit(); emb.calls = 0
    poisoned = QueryEmbeddingCache("OTHER-MODEL", dim=4)
    poisoned.put("how does retry work", [99.0, 99.0, 99.0, 99.0])   # a cross-model vector
    planner = RetrievalPlanner(store, emb, query_cache=poisoned)
    planner.explain("how does retry work")
    assert emb.calls == 1                       # cleared the cross-model entry -> the real embedder ran
    assert poisoned.model_id == "counting-v1"   # re-tagged to the planner's embedder
    store.close()


def test_concurrent_dumps_no_crash(tmp_path):
    import threading
    c = QueryEmbeddingCache("m", dim=2)
    for i in range(50):
        c.put(f"q{i}", [float(i), 0.0])
    p = str(tmp_path / "shared.mqec")
    errors: list = []

    def worker():
        for _ in range(30):
            try:
                c.dump(p)
            except Exception as e:               # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []                          # unique temp per writer -> no FileNotFoundError race
    assert QueryEmbeddingCache("m", dim=2).load(p) == 50   # the shared file is a valid, complete dump


def test_facade_clear_and_dump_load(tmp_path):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        db = MemoryDB.open(":memory:", embedder=_CountingEmbedder())
    db.ask("how does retry work", k=1)                    # populates the query cache via explain
    p = str(tmp_path / "q.mqec")
    assert db.dump_query_cache(p) >= 0                     # callable; persists the cache
    db.clear_query_cache()                                # clearable
    assert db.load_query_cache(p) >= 0                     # reloadable (model-validated)
    db.close()
