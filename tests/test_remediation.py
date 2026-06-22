"""Tests for the adversarial-review remediations (2026-06-22).

Covers: monotonic edge confidence (precise supersedes coarse, never downgrades),
the node_neighborhood query, and LOCATE name-ambiguity grouping (C4).
Zero third-party deps: `python tests/test_remediation.py` or `pytest`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import Node, Rel, RetrievalPlanner, Store, HashingEmbedder  # noqa: E402
from memorydb import query as Q  # noqa: E402


def _edge_conf(store, src, dst, rel):
    row = store.conn.execute(
        "SELECT e.confidence FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes t ON t.id=e.dst "
        "WHERE s.uid=? AND t.uid=? AND e.relation=?",
        (src, dst, rel),
    ).fetchone()
    return row[0] if row else None


def test_confidence_is_monotonic():
    s = Store(":memory:")
    for u in ("a", "b"):
        s.upsert_node(Node(uid=u, type="function", name=u))
    s.upsert_edge("a", "b", Rel.CALLS, confidence=0.3, source="treesitter")   # coarse first
    assert _edge_conf(s, "a", "b", Rel.CALLS) == 0.3
    s.upsert_edge("a", "b", Rel.CALLS, confidence=0.97, source="ast")          # precise supersedes
    assert _edge_conf(s, "a", "b", Rel.CALLS) == 0.97
    s.upsert_edge("a", "b", Rel.CALLS, confidence=0.5, source="treesitter")    # coarse must NOT downgrade
    assert _edge_conf(s, "a", "b", Rel.CALLS) == 0.97


def test_node_neighborhood():
    s = Store(":memory:")
    for u in ("svc", "caller", "queue"):
        s.upsert_node(Node(uid=u, type="function", name=u))
    s.upsert_edge("caller", "svc", Rel.CALLS)
    s.upsert_edge("svc", "queue", Rel.CALLS)
    nb = Q.node_neighborhood(s, s.id_for("svc"))
    assert {n["uid"] for n in nb["out"]} == {"queue"}
    assert {n["uid"] for n in nb["in"]} == {"caller"}


def test_locate_flags_ambiguity():
    s = Store(":memory:")
    for u in ("f1.py::send", "f2.py::send", "c1", "c2"):
        name = u.split("::")[-1]
        s.upsert_node(Node(uid=u, type="function", name=name))
    s.upsert_edge("c1", "f1.py::send", Rel.CALLS)
    s.upsert_edge("c2", "f2.py::send", Rel.CALLS)
    res = RetrievalPlanner(s, HashingEmbedder()).retrieve("where is send used?")
    assert res["intent"] == "LOCATE"
    assert res["ambiguous"] is True
    assert set(res["matched_uids"]) == {"f1.py::send", "f2.py::send"}


def test_locate_unambiguous_single_target():
    s = Store(":memory:")
    for u in ("pkg.mod::send_it", "caller"):
        s.upsert_node(Node(uid=u, type="function", name=u.split("::")[-1]))
    s.upsert_edge("caller", "pkg.mod::send_it", Rel.CALLS)
    res = RetrievalPlanner(s, HashingEmbedder()).retrieve("where is send_it used?")
    assert res["ambiguous"] is False
    assert res["matched_uids"] == ["pkg.mod::send_it"]


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        fn()
        print(f"ok  {name}")
    print(f"\nall green ({len(tests)} tests)")
