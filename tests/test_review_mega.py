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


# --- Batch 4: MR-17/18/19/20 ----------------------------------------------------------------
def test_mr17_context_blocks_ordered_by_uid():
    import warnings
    from memorydb import MemoryDB
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        db = MemoryDB.open(":memory:", extractors=[])
    result = {"intent": "EXPLAIN", "seeds": [], "nodes": [
        {"id": 1, "uid": "z::c", "type": "function", "name": "c", "attrs": {}},
        {"id": 2, "uid": "a::a", "type": "function", "name": "a", "attrs": {}},
        {"id": 3, "uid": "m::b", "type": "function", "name": "b", "attrs": {}}]}
    uids = [u for u, _ in db._explain_blocks(result)]
    assert uids == ["a::a", "m::b", "z::c"]          # by uid, not by id order (z::c, a::a, m::b)
    db.close()


def test_mr18_migration3_is_idempotent():
    import sqlite3
    from memorydb.migrations import _m3_file_uid_index, migrate
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    _m3_file_uid_index(conn)                          # re-running must not raise 'duplicate column'
    assert "file_uid" in {r[1] for r in conn.execute("PRAGMA table_xinfo(nodes)")}


def test_mr19_ndcg_gains_keep_binary_expected():
    from memorydb.eval import ndcg_at_k
    assert abs(ndcg_at_k(["a", "b"], ["a", "b"], 2, gains={"a": 2.0}) - 1.0) < 1e-9   # b still grade 1
    assert ndcg_at_k(["b", "a"], ["a", "b"], 2, gains={"a": 2.0}) < 1.0               # higher grade lower


def test_mr20_traverse_drops_nonexistent_seed():
    from memorydb import query as Q
    s = Store(":memory:")
    s.upsert_node(Node(uid="a", type="function", name="a"))
    s.commit()
    assert Q.traverse(s, [999999], max_depth=1) == []                                 # ghost seed dropped
    assert {r["id"] for r in Q.traverse(s, [s.id_for("a")], max_depth=1)} == {s.id_for("a")}


# --- Round 6 — Batch A: MR-6 completeness (R6-1/2/12/19) -------------------------------------
def test_r6_1_edge_src_uses_ordinal_uid():
    # a conditionally-defined def is collected AND its outgoing edge uses its ordinal uid, not a bare one
    repo = _repo({"m.py":
        "def helper():\n    return 0\n\nFLAG = True\n"
        "if FLAG:\n    def f():\n        return 1\n"
        "else:\n    def f():\n        return helper()\n"})
    ex = PythonResolver(repo_root=repo).extract(os.path.join(repo, "m.py"))
    assert {n.uid for n in ex.nodes if n.name == "f"} == {"m.py::f", "m.py::f#1"}
    assert [(e.src, e.dst) for e in ex.edges if e.dst == "m.py::helper"] == [("m.py::f#1", "m.py::helper")]


def test_r6_12_self_method_resolves_to_impl_not_stub():
    repo = _repo({"c.py":
        "from typing import overload\nclass C:\n"
        "    @overload\n    def m(self, x: int): ...\n"
        "    @overload\n    def m(self, x: str): ...\n"
        "    def m(self, x):\n        return 1\n"
        "    def go(self):\n        return self.m(1)\n"})
    ex = PythonResolver(repo_root=repo).extract(os.path.join(repo, "c.py"))
    assert [e.dst for e in ex.edges if e.src == "c.py::C.go"] == ["c.py::C.m#2"]   # the impl, not a stub


def test_r6_19_nested_function_is_function_not_method():
    repo = _repo({"n.py": "def outer():\n    def inner():\n        return 1\n    return inner()\n"})
    ex = PythonResolver(repo_root=repo).extract(os.path.join(repo, "n.py"))
    assert [(n.uid, n.type) for n in ex.nodes if n.name == "inner"] == [("n.py::outer.inner", "function")]


def test_r6_2_precise_edge_survives_callee_edit_with_ambiguous_name():
    repo = _repo({
        "b.py": "def foo():\n    return 1\n",
        "c.py": "def foo():\n    return 2\n",          # 'foo' is now ambiguous by name
        "a.py": "from b import foo\n\ndef g():\n    return foo()\n"})
    s = Store(":memory:")
    idx = Indexer(s, [PythonResolver()])
    idx.index(repo)
    assert _xconf(s, "a.py::g", "b.py::foo") == 0.97
    with open(os.path.join(repo, "b.py"), "w") as fh:
        fh.write("def foo():\n    return 11  # edited body\n")
    idx.index(repo)
    assert _xconf(s, "a.py::g", "b.py::foo") == 0.97   # survived via exact dst_uid (by-name is ambiguous)


# --- Round 6 — Batch F: pydantic/CLI (R6-18/20/15) ------------------------------------------
def test_r6_18_confidence_is_bounded():
    from memorydb import Edge
    import pydantic
    Edge(src="a", dst="b", relation="CALLS", confidence=0.97)        # in-range ok
    for bad in (1.5, -0.1):
        try:
            Edge(src="a", dst="b", relation="CALLS", confidence=bad)
            assert False, f"expected ValidationError for confidence={bad}"
        except pydantic.ValidationError:
            pass


def test_r6_20_scorecard_rejects_garbage_json():
    from memorydb.eval import Scorecard
    import pydantic
    Scorecard.from_dict({"locate": {}, "explain": {}, "k": 5})      # valid
    try:
        Scorecard.from_dict({"not_a_scorecard": True, "lol": 1})
        assert False, "expected ValidationError for a non-scorecard dict"
    except pydantic.ValidationError:
        pass


def test_r6_15_empty_db_json_is_valid():
    import io
    import json as _json
    import tempfile as _tf
    from contextlib import redirect_stdout
    from memorydb import cli
    db = os.path.join(_tf.mkdtemp(), "empty.sqlite")
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--db", db, "query", "anything", "--json"])
    assert code == 0
    assert _json.loads(out.getvalue()) == {}                         # valid (empty) JSON, not 0 bytes


# --- Round 6 — Batch D: retrieval (R6-9/13) -------------------------------------------------
def test_r6_9_dotted_locate_offers_bare_tail():
    from memorydb.planner import RetrievalPlanner
    c = RetrievalPlanner._candidates
    assert "foo" in c("where is mod.foo used?")          # dotted token -> bare tail also a candidate
    assert "run" in c("who calls a.py::Bar.run?")        # ::-qualified too
    # end-to-end: a dotted LOCATE query grounds the symbol and finds its caller
    repo = _repo({"b.py": "def foo():\n    return 1\n",
                  "a.py": "from b import foo\n\ndef g():\n    return foo()\n"})
    s = Store(":memory:")
    from memorydb import HashingEmbedder
    Indexer(s, [PythonResolver()], HashingEmbedder()).index(repo)
    res = RetrievalPlanner(s, HashingEmbedder()).retrieve("where is b.foo used?")
    assert res["intent"] == "LOCATE" and "a.py::g" in {r["src_uid"] for r in res["references"]}


def test_r6_13_stopwords_not_grounded_as_symbol():
    from memorydb.planner import RetrievalPlanner
    assert RetrievalPlanner._candidates("where is the call used?") == []   # all stopwords dropped
    assert "send_notification" in RetrievalPlanner._candidates("where is send_notification used?")


# --- Round 6 — Batch E: concurrency (R6-10/11) ----------------------------------------------
def test_r6_10_busy_timeout_and_wal_only_for_files():
    p = os.path.join(tempfile.mkdtemp(), "c.db")
    s1 = Store(p)
    Store(p)                                                     # second open must not crash (R6-10)
    assert int(s1.conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
    assert s1.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert Store(":memory:").conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "memory"


def test_r6_11_concurrent_writers_wait_not_crash():
    import threading
    p = os.path.join(tempfile.mkdtemp(), "c.db")
    Store(p).close()
    errs = []

    def writer(tag):
        try:
            st = Store(p)
            for i in range(40):
                st.upsert_node(Node(uid=f"{tag}-{i}", type="function", name="x"))
            st.commit()
            st.close()
        except Exception as e:  # noqa
            errs.append((tag, repr(e)))

    ts = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == []                                            # busy_timeout -> wait, no 'locked' crash
    assert Store(p).conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 80


# --- Round 6 — Batch B: multilang extraction (R6-3/4/5/6/7/16/17/19/23) [code]-gated ---------
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_language_pack  # noqa: F401
    from memorydb.adapters.code import CodeAdapter
    HAVE_CODE = True
except Exception:
    HAVE_CODE = False


def _extract(fname, code):
    d = tempfile.mkdtemp()
    p = os.path.join(d, fname)
    with open(p, "w") as fh:
        fh.write(code)
    ex = CodeAdapter(repo_root=d).extract(p)
    nodes = {n.uid.split("::")[-1]: n.type for n in ex.nodes}
    edges = {(e.src.split("::")[-1], e.relation, e.dst.split("::")[-1]) for e in ex.edges}
    return nodes, edges


def test_r6_go_types_methods_receivers():
    if not HAVE_CODE:
        return
    nodes, edges = _extract("m.go",
        "package m\ntype Point struct { X int }\ntype Shape interface { Area() int }\n"
        "func (p Point) Area() int { return helper() }\nfunc helper() int { return 1 }\n")
    assert nodes.get("Point") == "class" and nodes.get("Shape") == "class"   # R6-3 type_spec
    assert nodes.get("Point.Area") == "method"                               # R6-6 receiver + method kind
    assert ("Point.Area", "CALLS", "helper") in edges


def test_r6_js_arrows_inheritance_methods():
    if not HAVE_CODE:
        return
    nodes, edges = _extract("m.js",
        "const f = (a) => g(a);\nfunction g(a){ return a }\nclass A extends B { m(){ return this.n() } n(){} }\n")
    assert nodes.get("f") == "function" and nodes.get("g") == "function"     # R6-4 arrow extracted
    assert nodes.get("A.m") == "method" and nodes.get("A.n") == "method"
    assert ("f", "CALLS", "g") in edges and ("A.m", "CALLS", "A.n") in edges


def test_r6_ts_interface_methods_and_inheritance():
    if not HAVE_CODE:
        return
    nodes, edges = _extract("m.ts",
        "interface I { foo(): number }\nclass A extends B implements I { foo(){ return 1 } }\nconst h = () => 1;\n")
    assert nodes.get("I.foo") == "method"                                    # R6-23 interface method
    assert nodes.get("h") == "function"                                      # R6-4 arrow
    assert ("A", "INHERITS", "I") in edges                                   # R6-5 implements -> INHERITS


def test_r6_rust_impl_scope_no_dup_and_inherits():
    if not HAVE_CODE:
        return
    nodes, edges = _extract("m.rs",
        "struct Point { x: i32 }\ntrait Shape { fn area(&self) -> i32; }\n"
        "impl Shape for Point { fn area(&self) -> i32 { 1 } }\nimpl Point { fn new() -> Point { Point{x:0} } }\n")
    assert [k for k in nodes if k.startswith("Point") and "::" not in k].count("Point") == 1  # R6-16 no dup
    assert nodes.get("Point.area") == "method" and nodes.get("Point.new") == "method"  # R6-7 named by type
    assert ("Point", "INHERITS", "Shape") in edges                           # R6-7 impl-for-trait


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
