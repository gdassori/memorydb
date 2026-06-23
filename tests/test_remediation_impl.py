"""Regression tests for the 2026-06-22 implementation-review remediation
(docs/specs/adversarial-review-2026-06-22-impl.md). Zero-dep except the two cases gated on [code]."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import BruteForceVectorIndex, HashingEmbedder, Indexer, Node, Store  # noqa: E402
from memorydb.adapters.code import Extraction  # noqa: E402
from memorydb.embedding_pipeline import EmbeddingPipeline  # noqa: E402
from memorydb.eval import ndcg_at_k  # noqa: E402


class FakeExtractor:
    def __init__(self):
        self.repo_root = "."

    def handles(self, path):
        return path.endswith(".fake")

    def lang_of(self, path):
        return "fake"

    def extract(self, path):
        rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
        return Extraction(nodes=[Node(uid=f"{rel}::s", type="function", name="s",
                                      body="s", attrs={"file_uid": rel})])


def _write(d, name, text="v1"):
    p = os.path.join(d, name)
    with open(p, "w") as fh:
        fh.write(text)
    return p


# --- I2: embedder returning too few vectors must not silently drop / falsely embed ----------
def test_short_embedder_does_not_lose_embeddings():
    s = Store(":memory:")
    for uid in ("a", "b", "c"):
        s.upsert_node(Node(uid=uid, type="function", name=uid, body=uid))
    s.commit()

    class ShortEmbedder:
        def embed(self, texts):
            return [[1.0]] * (len(texts) - 1)   # one short

    rep = EmbeddingPipeline(s, ShortEmbedder()).refresh()
    assert rep.embedded == 0 and rep.failed == 3            # batch failed, not partially "succeeded"
    assert s.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert len(s.dirty_nodes()) == 3                        # all still dirty → retried next time


# --- I13: negative k must clamp to empty, not slice off the tail ----------------------------
def test_vector_search_negative_k_is_empty():
    s = Store(":memory:")
    s.upsert_node(Node(uid="a", type="function", name="a"))
    s.set_embedding(s.id_for("a"), [1.0, 0.0])
    s.commit()
    idx = BruteForceVectorIndex(s)
    assert idx.search([1.0, 0.0], k=-1) == []
    assert idx.search([1.0, 0.0], k=0) == []
    assert len(idx.search([1.0, 0.0], k=5)) == 1


# --- I16: an empty gains dict must fall back to binary relevance, not zero the case ----------
def test_ndcg_empty_gains_falls_back_to_binary():
    assert ndcg_at_k(["a", "b"], ["a", "b"], 5, gains={}) == 1.0
    assert ndcg_at_k(["a", "b"], ["a", "b"], 5, gains=None) == 1.0


# --- I8 / I12: the indexed generated column + partial dirty index exist ----------------------
def test_schema_has_file_uid_column_and_indexes():
    s = Store(":memory:")
    # table_xinfo (not table_info) lists VIRTUAL generated columns.
    cols = [r[1] for r in s.conn.execute("PRAGMA table_xinfo(nodes)")]
    assert "file_uid" in cols
    idx_names = {r[1] for r in s.conn.execute("PRAGMA index_list(nodes)")}
    assert "idx_nodes_file_uid" in idx_names and "idx_nodes_dirty" in idx_names
    # the generated column actually reflects attrs.$.file_uid
    s.upsert_node(Node(uid="x::f", type="function", name="f", attrs={"file_uid": "x"}))
    s.commit()
    assert s.conn.execute("SELECT file_uid FROM nodes WHERE uid='x::f'").fetchone()[0] == "x"


# --- I5: symlinked files must not be read through their target (escape the indexed root) -----
def test_symlinked_file_is_skipped():
    with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as secret:
        _write(repo, "a.fake")
        target = _write(secret, "secret.fake")
        link = os.path.join(repo, "linked.fake")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            print("skip test_symlinked_file_is_skipped: symlinks unsupported here")
            return
        s = Store(":memory:")
        Indexer(s, [FakeExtractor()]).index(repo)
        assert s.id_for("a.fake") is not None            # the real file indexed
        assert s.id_for("linked.fake") is None           # the symlink skipped
        assert s.id_for("linked.fake::s") is None


# --- I10: file nodes persist mtime (the recency signal for FILTER/ranker, C5) ----------------
def test_file_node_persists_mtime():
    with tempfile.TemporaryDirectory() as repo:
        _write(repo, "a.fake")
        s = Store(":memory:")
        Indexer(s, [FakeExtractor()]).index(repo)
        nid = s.id_for("a.fake")
        attrs = s.get_nodes([nid])[0]["attrs"]
        assert "mtime" in attrs and isinstance(attrs["mtime"], (int, float))


# --- [code]-gated regressions ---------------------------------------------------------------
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_language_pack  # noqa: F401
    from memorydb.adapters.code import CodeAdapter
    HAVE_CODE = True
except Exception:
    HAVE_CODE = False


def test_deeply_nested_file_does_not_abort_index():
    if not HAVE_CODE:
        print("skip test_deeply_nested_file_does_not_abort_index: [code] not installed")
        return
    with tempfile.TemporaryDirectory() as repo:
        _write(repo, "deep.py", "def f():\n    return " + "(" * 3000 + "1" + ")" * 3000 + "\n")
        _write(repo, "good.py", "def g():\n    return 1\n")
        s = Store(":memory:")
        rep = Indexer(s, [CodeAdapter()], HashingEmbedder()).index(repo)   # must not raise
        assert s.id_for("good.py::g") is not None        # the healthy file still indexed
        assert rep.files_indexed == 2


def test_same_name_methods_not_resolved_to_wrong_class():
    if not HAVE_CODE:
        print("skip test_same_name_methods_not_resolved_to_wrong_class: [code] not installed")
        return
    src = (
        "class A:\n"
        "    def send(self):\n"
        "        return 1\n\n"
        "class B:\n"
        "    def send(self):\n"
        "        return 2\n"
        "    def go(self):\n"
        "        return self.send()\n"
    )
    with tempfile.TemporaryDirectory() as repo:
        _write(repo, "m.py", src)
        s = Store(":memory:")
        Indexer(s, [CodeAdapter()], HashingEmbedder()).index(repo)
        # B.go must NOT get a high-confidence edge to A.send (the wrong class). With two same-name
        # defs the name is ambiguous → demoted to pending → globally unresolvable → no edge at all.
        wrong = s.conn.execute(
            "SELECT COUNT(*) FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
            "WHERE a.uid='m.py::B.go' AND b.uid='m.py::A.send'"
        ).fetchone()[0]
        assert wrong == 0


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        try:
            fn()
            print(f"ok  {name}")
        except Exception as e:  # noqa
            import traceback
            print(f"FAIL {name}: {e}")
            traceback.print_exc()
    print("done")
