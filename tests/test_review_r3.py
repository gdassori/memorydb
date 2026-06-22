"""Regression tests for the round-3 review fixes (R3L-1..R3L-4). Zero-dep except the [code]-gated
faithful reproduction of R3L-1 on real Python."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import BruteForceVectorIndex, Indexer, Node, Store  # noqa: E402
from memorydb import query as Q  # noqa: E402
from memorydb.adapters.code import Extraction  # noqa: E402
from memorydb.query import _recursive_branches  # noqa: E402


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w") as fh:
        fh.write(text)
    return p


def _dirty(s, uid):
    return s.conn.execute("SELECT embed_dirty FROM nodes WHERE uid = ?", (uid,)).fetchone()[0]


# --- R3L-1: editing a callee file must NOT drop the cross-file edge -------------------------
class CalleeCallerExtractor:
    """a.fake defines `helper`; b.fake defines `caller` which calls helper (cross-file, by name)."""
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
            return Extraction(nodes=[Node(uid=f"{rel}::helper", type="function", name="helper",
                                          body="helper", attrs={"file_uid": rel})])
        if base == "b.fake":
            return Extraction(nodes=[Node(uid=f"{rel}::caller", type="function", name="caller",
                                          body="caller", attrs={"file_uid": rel})],
                              pending=[(f"{rel}::caller", "helper", "CALLS", 0.6)])
        return Extraction()


def _xedge(s):
    return s.conn.execute(
        "SELECT COUNT(*) FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
        "WHERE a.uid='b.fake::caller' AND b.uid='a.fake::helper'"
    ).fetchone()[0]


def test_editing_callee_file_keeps_cross_file_edge():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.fake", "v1")
        _write(d, "b.fake", "v1")
        s = Store(":memory:")
        idx = Indexer(s, [CalleeCallerExtractor()])
        idx.index(d)
        assert _xedge(s) == 1                         # caller -> helper resolved
        _write(d, "a.fake", "v2-changed-callee-only")  # edit ONLY the callee file
        idx.index(d)
        assert _xedge(s) == 1                         # was the R3L-1 bug: dropped to 0


def test_deleting_then_readding_callee_restores_edge():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.fake", "v1")
        bp = _write(d, "b.fake", "v1")  # noqa: F841
        s = Store(":memory:")
        idx = Indexer(s, [CalleeCallerExtractor()])
        idx.index(d)
        assert _xedge(s) == 1
        os.remove(os.path.join(d, "a.fake"))          # delete the callee file
        idx.index(d)
        assert _xedge(s) == 0                          # edge correctly gone (helper no longer exists)
        _write(d, "a.fake", "v1-again")                # re-add the callee
        idx.index(d)
        assert _xedge(s) == 1                          # pending row survived -> edge rebuilt


# --- R3L-2: traverse(both) reaches the union AND uses the edge indexes ----------------------
def test_traverse_both_correct_and_index_using():
    s = Store(":memory:")
    for u in ("a", "b", "c"):
        s.upsert_node(Node(uid=u, type="function", name=u))
    s.upsert_edge("a", "b", "CALLS")
    s.upsert_edge("b", "c", "CALLS")
    s.commit()
    b = s.id_for("b")
    reached = {r["id"] for r in Q.traverse(s, [b], max_depth=1, direction="both")}
    assert reached == {s.id_for("a"), b, s.id_for("c")}        # both directions, 1 hop
    assert {r["id"] for r in Q.traverse(s, [b], max_depth=1, direction="out")} == {b, s.id_for("c")}
    assert {r["id"] for r in Q.traverse(s, [b], max_depth=1, direction="in")} == {b, s.id_for("a")}

    # the rewritten 'both' must use idx_edges_src/dst, not a full edge-table scan (the R3L-2 cliff)
    sql = ("WITH RECURSIVE reach(id, depth) AS ( SELECT value, 0 FROM json_each(:seeds) UNION "
           + _recursive_branches("both", "") + " ) SELECT id, MIN(depth) FROM reach GROUP BY id")
    plan = " ".join(r[3] for r in s.conn.execute(
        "EXPLAIN QUERY PLAN " + sql, {"seeds": json.dumps([b]), "max_depth": 2}).fetchall())
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan
    assert "SCAN edges" not in plan                            # no full materialization


# --- R3L-3: deleting a file dirties the surviving neighbor on the far end of each edge -------
def test_delete_dirties_surviving_neighbor():
    s = Store(":memory:")
    s.upsert_node(Node(uid="x.fake", type="file", name="x.fake", attrs={"sha256": "-"}))
    s.upsert_node(Node(uid="y.fake", type="file", name="y.fake", attrs={"sha256": "-"}))
    s.upsert_node(Node(uid="x.fake::bar", type="function", name="bar", attrs={"file_uid": "x.fake"}))
    s.upsert_node(Node(uid="y.fake::foo", type="function", name="foo", attrs={"file_uid": "y.fake"}))
    s.upsert_edge("x.fake::bar", "y.fake::foo", "CALLS")        # bar CALLS foo
    s.set_embedding(s.id_for("x.fake::bar"), [1.0, 0.0])        # bar now clean
    s.commit()
    assert _dirty(s, "x.fake::bar") == 0
    Indexer(s, [])._delete_file("y.fake")                       # delete foo's file
    assert _dirty(s, "x.fake::bar") == 1                        # neighbor re-dirtied (was 0 before)


# --- R3L-4: vector search has a deterministic, churn-invariant uid tiebreak -----------------
def test_vector_search_uid_tiebreak_is_deterministic():
    s = Store(":memory:")
    for uid in ("c::z", "a::x", "b::y"):                        # inserted out of uid order
        s.upsert_node(Node(uid=uid, type="function", name=uid))
        s.set_embedding(s.id_for(uid), [1.0, 0.0])             # identical vectors -> tied scores
    s.commit()
    idx = BruteForceVectorIndex(s)
    uids = [s.get_nodes([nid])[0]["uid"] for _, nid in idx.search([1.0, 0.0], k=3)]
    assert uids == ["a::x", "b::y", "c::z"]                     # ties broken by uid, not row order
    top2 = [s.get_nodes([nid])[0]["uid"] for _, nid in idx.search([1.0, 0.0], k=2)]
    assert top2 == ["a::x", "b::y"]


# --- [code]-gated: faithful R3L-1 reproduction on real Python -------------------------------
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_language_pack  # noqa: F401
    from memorydb import HashingEmbedder
    from memorydb.adapters.code import CodeAdapter
    HAVE_CODE = True
except Exception:
    HAVE_CODE = False


def test_editing_callee_keeps_edge_real_python():
    if not HAVE_CODE:
        print("skip test_editing_callee_keeps_edge_real_python: [code] not installed")
        return
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.py", "def helper():\n    return 1\n")
        _write(d, "b.py", "from a import helper\n\ndef caller():\n    return helper()\n")
        s = Store(":memory:")
        idx = Indexer(s, [CodeAdapter()], HashingEmbedder())
        idx.index(d)

        def edge():
            return s.conn.execute(
                "SELECT COUNT(*) FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
                "WHERE a.uid='b.py::caller' AND b.uid='a.py::helper' AND e.relation='CALLS'"
            ).fetchone()[0]
        assert edge() == 1
        _write(d, "a.py", "def helper():\n    return 2  # edited callee only\n")
        idx.index(d)
        assert edge() == 1                                     # restored, not silently dropped


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
