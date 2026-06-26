"""QueryEmbeddingCache tests (TD-011) — zero-dep."""
from __future__ import annotations

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


def test_wrong_dim_put_ignored():
    c = QueryEmbeddingCache("m", dim=2)
    c.put("q", [1.0, 2.0, 3.0])                      # dim 3 != 2 -> ignored, not poisoned
    assert c.get("q") is None


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
