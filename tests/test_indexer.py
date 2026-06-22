"""Indexer tests. The FakeExtractor tests are zero-dep; the end-to-end test needs the [code] extra."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import HashingEmbedder, Indexer, Node, RetrievalPlanner, Store  # noqa: E402
from memorydb.adapters.code import Extraction  # noqa: E402 (import has no tree-sitter dep)


# --- a deterministic fake extractor (zero-dep) -----------------------------
class FakeExtractor:
    """a.fake declares g() which calls foo(); b.fake declares foo(). Cross-file by name."""

    def __init__(self):
        self.repo_root = "."

    def handles(self, path):
        return path.endswith(".fake")

    def lang_of(self, path):
        return "fake"

    def extract(self, path):
        rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
        base = os.path.basename(rel)
        if base == "a.fake":
            return Extraction(
                nodes=[Node(uid=f"{rel}::g", type="function", name="g", body="g", attrs={"file_uid": rel})],
                edges=[],
                pending=[(f"{rel}::g", "foo", "CALLS", 0.6)],
            )
        if base == "b.fake":
            return Extraction(
                nodes=[Node(uid=f"{rel}::foo", type="function", name="foo", body="foo", attrs={"file_uid": rel})],
            )
        return Extraction()


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w") as fh:
        fh.write(text)
    return p


def test_index_resolves_cross_file_pending():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.fake", "v1")
        _write(d, "b.fake", "v1")
        s = Store(":memory:")
        rep = Indexer(s, [FakeExtractor()], HashingEmbedder()).index(d)
        assert rep.files_indexed == 2 and rep.nodes_upserted == 2
        # pending CALLS foo resolved globally to the single b.fake::foo
        assert rep.edges_upserted == 1 and rep.edges_unresolved == 0
        assert s.id_for("a.fake::g") is not None and s.id_for("b.fake::foo") is not None
        assert rep.embedded >= 1


def test_incremental_skip_and_change():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.fake", "v1")
        _write(d, "b.fake", "v1")
        s = Store(":memory:")
        idx = Indexer(s, [FakeExtractor()], HashingEmbedder())
        idx.index(d)
        rep2 = idx.index(d)                       # nothing changed
        assert rep2.files_indexed == 0 and rep2.files_skipped == 2
        _write(d, "a.fake", "v2-changed")          # touch one file's content
        rep3 = idx.index(d)
        assert rep3.files_indexed == 1 and rep3.files_skipped == 1


def test_delete_cascades():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.fake", "v1")
        bp = _write(d, "b.fake", "v1")
        s = Store(":memory:")
        idx = Indexer(s, [FakeExtractor()], HashingEmbedder())
        idx.index(d)
        edges_before = s.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert edges_before == 1
        os.remove(bp)
        rep = idx.index(d)
        assert rep.files_deleted == 1
        assert s.id_for("b.fake::foo") is None                  # symbol gone
        assert s.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0  # edge cascaded


# --- end-to-end with the real tree-sitter CodeAdapter ----------------------
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_language_pack  # noqa: F401
    from memorydb.adapters.code import CodeAdapter
    HAVE_CODE = True
except Exception:
    HAVE_CODE = False


@pytest.mark.skipif(not HAVE_CODE, reason="[code] extra not installed")
def test_end_to_end_python_index_and_locate():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "b.py", "def foo():\n    return 1\n")
        _write(d, "a.py", "from b import foo\n\ndef g():\n    return foo()\n")
        s = Store(":memory:")
        rep = Indexer(s, [CodeAdapter()], HashingEmbedder()).index(d)
        assert rep.files_indexed == 2
        assert s.id_for("a.py::g") is not None and s.id_for("b.py::foo") is not None
        # cross-file call a.py::g -> b.py::foo resolved from the pending 'foo'
        row = s.conn.execute(
            "SELECT COUNT(*) FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
            "WHERE a.uid='a.py::g' AND b.uid='b.py::foo' AND e.relation='CALLS'"
        ).fetchone()[0]
        assert row == 1
        # end-to-end retrieval: who calls foo?
        res = RetrievalPlanner(s, HashingEmbedder()).retrieve("where is foo used?")
        assert res["intent"] == "LOCATE"
        assert "a.py::g" in {r["src_uid"] for r in res["references"]}


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        try:
            fn()
            print(f"ok  {name}")
        except Exception as e:  # noqa
            print(f"FAIL {name}: {e}")
    print("done")
