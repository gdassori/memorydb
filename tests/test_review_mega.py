"""Regression tests for the 2026-06-23 mega-review fixes (MR-1..MR-23).
See docs/specs/adversarial-review-2026-06-23-mega.md. All zero-dep (PythonResolver is stdlib)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import Indexer, Node, Store  # noqa: E402
from memorydb.adapters.code import Extraction  # noqa: E402
from memorydb.adapters.code.python_resolver import PythonResolver  # noqa: E402


def _repo(files: dict) -> str:
    d = tempfile.mkdtemp()
    for name, text in files.items():
        sub = os.path.join(d, os.path.dirname(name))
        if os.path.dirname(name):
            os.makedirs(sub, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            fh.write(text)
    return d


def _xconf(s, src, dst):
    row = s.conn.execute(
        "SELECT e.confidence FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
        "WHERE a.uid=? AND b.uid=? AND e.relation='CALLS'", (src, dst)).fetchone()
    return row[0] if row else None


# --- MR-1: a hostile/deeply-nested .py must not abort the whole index -----------------------
def test_mr1_deep_file_does_not_abort_index():
    repo = _repo({
        "deep.py": "x = " + "(" * 2500 + "1" + ")" * 2500 + "\n",   # deep AST -> RecursionError risk
        "good.py": "def g():\n    return 1\n",
    })
    s = Store(":memory:")
    rep = Indexer(s, [PythonResolver()]).index(repo)                 # must not raise
    assert s.id_for("good.py::g") is not None                       # healthy file still indexed
    assert rep.files_indexed == 2


def test_mr1_extractor_exception_is_isolated():
    class Boom:
        repo_root = "."
        def handles(self, p): return p.endswith(".py")
        def lang_of(self, p): return "python"
        def extract(self, p): raise RuntimeError("hostile extractor")

    repo = _repo({"a.py": "def a():\n    return 1\n"})
    s = Store(":memory:")
    rep = Indexer(s, [Boom(), PythonResolver()]).index(repo)         # Boom must not abort the run
    assert s.id_for("a.py::a") is not None and rep.files_indexed == 1


# --- MR-2: index() is atomic (one transaction) + force re-index -----------------------------
def test_mr2_index_rolls_back_on_error():
    repo = _repo({"a.py": "def a():\n    return 1\n"})
    s = Store(":memory:")
    idx = Indexer(s, [PythonResolver()])
    idx._resolve_pending = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash mid-index"))
    try:
        idx.index(repo)
        assert False, "expected the injected error to propagate"
    except RuntimeError:
        pass
    # the whole run rolled back — nothing committed, so a re-index is clean (no durable sha skip-token)
    assert s.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0


def test_mr2_force_reindexes_everything():
    repo = _repo({"a.py": "def a():\n    return 1\n", "b.py": "def b():\n    return 2\n"})
    s = Store(":memory:")
    idx = Indexer(s, [PythonResolver()])
    idx.index(repo)
    assert idx.index(repo).files_skipped == 2                       # unchanged -> skipped
    assert idx.index(repo, force=True).files_indexed == 2           # force ignores the sha skip


# --- MR-3: editing a callee body must NOT downgrade a precise cross-file edge ----------------
class FakeCoarse:
    """Emits ONLY the coarse 0.6 by-name pending for a.py::g -> foo (like the tree-sitter adapter)."""
    repo_root = "."
    def handles(self, p): return p.endswith(".py")
    def lang_of(self, p): return "python"
    def extract(self, p):
        rel = os.path.relpath(p, self.repo_root).replace(os.sep, "/")
        if os.path.basename(rel) == "a.py":
            return Extraction(pending=[("a.py::g", "foo", "CALLS", 0.6)])
        return Extraction()


def test_mr3_callee_edit_keeps_precise_confidence():
    repo = _repo({"b.py": "def foo():\n    return 1\n", "a.py": "from b import foo\n\ndef g():\n    return foo()\n"})
    s = Store(":memory:")
    idx = Indexer(s, [PythonResolver(), FakeCoarse()])              # precise 0.97 + coarse 0.6
    idx.index(repo)
    assert _xconf(s, "a.py::g", "b.py::foo") == 0.97                # precise wins initially
    # edit ONLY the callee's body (foo still defined) — the unchanged caller is skipped
    with open(os.path.join(repo, "b.py"), "w") as fh:
        fh.write("def foo():\n    return 99  # body changed\n")
    idx.index(repo)
    assert _xconf(s, "a.py::g", "b.py::foo") == 0.97                # was the MR-3 bug: downgraded to 0.6


# --- MR-4: embeddings stored unit-normalized; cosine still correct --------------------------
def test_mr4_embeddings_stored_normalized_and_cosine_correct():
    from memorydb import BruteForceVectorIndex
    from memorydb.vector import unpack
    s = Store(":memory:")
    s.upsert_node(Node(uid="a", type="function", name="a"))
    s.set_embedding(s.id_for("a"), [3.0, 4.0])                   # norm 5 -> stored as [0.6, 0.8]
    v = list(unpack(s.conn.execute("SELECT vector FROM embeddings").fetchone()[0]))
    assert abs((v[0] ** 2 + v[1] ** 2) ** 0.5 - 1.0) < 1e-6     # unit norm
    score = BruteForceVectorIndex(s).search([3.0, 4.0], k=1)[0][0]
    assert abs(score - 1.0) < 1e-6                               # same direction -> cosine 1.0


# --- MR-12: a mixed-dimension corpus only scores the query's dimension (no zip-truncation) ---
def test_mr12_mixed_dim_search_skips_mismatched():
    from memorydb import BruteForceVectorIndex
    s = Store(":memory:")
    s.upsert_node(Node(uid="d2", type="function", name="d2"))
    s.upsert_node(Node(uid="d3", type="function", name="d3"))
    s.set_embedding(s.id_for("d2"), [1.0, 0.0])                 # dim 2
    s.set_embedding(s.id_for("d3"), [1.0, 0.0, 0.0])           # dim 3
    res = BruteForceVectorIndex(s).search([1.0, 0.0], k=10)    # dim-2 query
    assert [s.get_nodes([nid])[0]["uid"] for _, nid in res] == ["d2"]


# --- MR-5: streaming refresh embeds all dirty nodes and clears the flag ----------------------
def test_mr5_streaming_embeds_all_and_clears_dirty():
    from memorydb import HashingEmbedder
    from memorydb.embedding_pipeline import EmbeddingPipeline
    s = Store(":memory:")
    for i in range(5):
        s.upsert_node(Node(uid=f"n{i}", type="function", name=f"n{i}", body="x"))
    s.commit()
    assert len(s.dirty_node_ids()) == 5
    rep = EmbeddingPipeline(s, HashingEmbedder(), batch_size=2).refresh()
    assert rep.embedded == 5 and rep.batches == 3               # 5 / batch 2 = 3 batches
    assert s.dirty_node_ids() == []
    assert s.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 5


# --- Batch 3: PythonResolver correctness cluster (MR-6..MR-13) -------------------------------
def _pyedges(repo, name):
    ex = PythonResolver(repo_root=repo).extract(os.path.join(repo, name))
    return ex


def test_mr6_overload_ordinal_uids_and_impl_preferred():
    repo = _repo({"m.py":
        "from typing import overload\n"
        "@overload\ndef process(x: int): ...\n"
        "@overload\ndef process(x: str): ...\n"
        "def process(x):\n    return x\n\n"
        "def caller():\n    return process(1)\n"})
    ex = _pyedges(repo, "m.py")
    assert [n.uid for n in ex.nodes if n.name == "process"] == \
        ["m.py::process", "m.py::process#1", "m.py::process#2"]      # #ordinal disambiguation (parity)
    assert next(e for e in ex.edges if e.src == "m.py::caller").dst == "m.py::process#2"  # real impl


def test_mr7_package_init_import_resolves():
    repo = _repo({"pkg/__init__.py": "def shared():\n    return 1\n",
                  "main.py": "from pkg import shared\n\ndef go():\n    return shared()\n"})
    s = Store(":memory:")
    Indexer(s, [PythonResolver()]).index(repo)
    assert _xconf(s, "main.py::go", "pkg/__init__.py::shared") == 0.97


def test_mr8_nested_def_shadowing_no_module_edge():
    repo = _repo({"m.py":
        "def helper():\n    return 0\n\n"
        "def outer():\n    def helper():\n        return 1\n    return helper()\n"})
    ex = _pyedges(repo, "m.py")
    assert not any(e.src == "m.py::outer" and e.dst == "m.py::helper" for e in ex.edges)


def test_mr9_sibling_scope_union_prevents_wrong_edge():
    repo = _repo({"m.py":
        "def helper():\n    return 0\n\n"
        "FLAG = True\n"
        "if FLAG:\n    def f():\n        helper = lambda: 1\n        return helper()\n"
        "else:\n    def f():\n        return helper()\n"})
    ex = _pyedges(repo, "m.py")
    assert not any(e.dst == "m.py::helper" and e.src.startswith("m.py::f") for e in ex.edges)


def test_mr10_dotted_import_binds_top_level():
    repo = _repo({"pkg/sub.py": "def run():\n    return 1\n",
                  "main.py": "import pkg.sub\n\ndef go():\n    return pkg.run()\n"})
    s = Store(":memory:")
    Indexer(s, [PythonResolver()]).index(repo)
    assert _xconf(s, "main.py::go", "pkg/sub.py::run") is None       # binds top-level `pkg`, not pkg.sub


def test_mr11_from_dot_import_submodule_resolves():
    repo = _repo({"pkg/__init__.py": "", "pkg/util.py": "def helper():\n    return 1\n",
                  "pkg/main.py": "from . import util\n\ndef run():\n    return util.helper()\n"})
    s = Store(":memory:")
    Indexer(s, [PythonResolver()]).index(repo)
    assert _xconf(s, "pkg/main.py::run", "pkg/util.py::helper") == 0.95


def test_mr13_escaping_relative_import_no_edge():
    repo = _repo({"x.py": "def f():\n    return 1\n",
                  "root.py": "from ..x import f\n\ndef g():\n    return f()\n"})
    s = Store(":memory:")
    Indexer(s, [PythonResolver()]).index(repo)
    assert _xconf(s, "root.py::g", "x.py::f") is None                # `from ..x` escapes -> no wrong edge


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
